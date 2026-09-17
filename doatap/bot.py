"""Telegram presentation layer. Persistent user/session state belongs to BotService."""

from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .i18n import LANGUAGES, translate as t

LOGGER = logging.getLogger(__name__)
SUBJECTS = ("ANATOMY", "PHYSIOLOGY", "PHARMACOLOGY")
PAGE_SIZE = 8


class TokenRedactionFilter(logging.Filter):
    """Remove Telegram credentials even if an upstream library logs a request URL."""

    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token:
            message = message.replace(self.token, "[REDACTED]")
        record.msg = re.sub(r"bot\d{5,}:[A-Za-z0-9_-]+", "bot[REDACTED]", message)
        record.args = ()
        # Our own error handler deliberately logs error classes only. Do not let
        # upstream exception details append a token-bearing URL to HTTP logs.
        if record.name.startswith(("httpx", "httpcore", "telegram")):
            record.exc_info = None
            record.exc_text = None
        return True


def configure_safe_logging(token: str) -> None:
    redactor = TokenRedactionFilter(token)
    for name in ("httpx", "httpcore", "telegram", "telegram.ext.Application"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.addFilter(redactor)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


def button(label: str, *parts: object) -> InlineKeyboardButton:
    payload = ":".join(map(str, parts))
    if len(payload.encode("utf-8")) > 64:
        raise ValueError("Callback identifier exceeds Telegram limit")
    return InlineKeyboardButton(label, callback_data=payload)


def question_text(question: dict[str, Any], language: str) -> str:
    """Display the Greek stem and choices; original_text is the full audit block."""
    lines = [question["text"], ""]
    lines.extend(f"{chr(65 + index)}. {option}" for index, option in enumerate(question["options"]))
    if question.get("source_document") and question.get("source_page"):
        year = f", {question['source_year']}" if question.get("source_year") else ""
        lines.extend(["", t(language, "source", document=question["source_document"],
                            page=question["source_page"], year=year)])
    return "\n".join(lines)


def result_text(summary: dict[str, Any], language: str) -> str:
    status = summary["status"]
    title = "complete" if status == "COMPLETED" else "progress" if status == "ACTIVE" else "closed"
    text = t(language, "summary", title=t(language, title),
             subject=t(language, summary["discipline"]), answered=summary["answered"],
             total=summary["total"], correct=summary["correct"],
             scored_total=summary["scored_total"], unverified=summary["unverified"])
    if summary["scored_total"]:
        # Never use the overall question count as the score denominator.
        text += "\n" + t(language, "percentage", percent=100 * summary["correct"] / summary["scored_total"])
    else:
        text += "\n\n" + t(language, "no_score")
    return text


def split_message(text: str, limit: int = 3500) -> list[str]:
    """Preserve all source content and stay below Telegram's UTF-16 length limit."""
    result, current, size = [], [], 0
    for character in text:
        length = len(character.encode("utf-16-le")) // 2
        if size + length > limit:
            result.append("".join(current))
            current, size = [], 0
        current.append(character)
        size += length
    if current:
        result.append("".join(current))
    return result or [""]


class BotUI:
    def __init__(self, service: Any, allowed_user_ids: set[int] | None = None):
        self.service = service
        self.allowed_user_ids = allowed_user_ids

    async def render(self, update: Update, text: str,
                     rows: list[list[InlineKeyboardButton]] | None = None) -> None:
        markup = InlineKeyboardMarkup(rows) if rows else None
        parts = split_message(text)
        if update.callback_query and len(parts) == 1:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=None)
                return
            except BadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
                # Old messages may no longer be editable; keep the saved state usable.
        message = update.effective_message
        if message:
            for index, part in enumerate(parts):
                await message.reply_text(part, reply_markup=markup if index == len(parts) - 1 else None,
                                         parse_mode=None)

    async def identity(self, update: Update) -> dict[str, Any] | None:
        user, chat = update.effective_user, update.effective_chat
        if not user:
            return None
        language = user.language_code if user.language_code in LANGUAGES else "en"
        if not chat or chat.type != "private":
            await self.render(update, t(language, "private"))
            return None
        if self.allowed_user_ids is not None and user.id not in self.allowed_user_ids:
            await self.render(update, t(language, "restricted"))
            return None
        return self.service.upsert_user(user.id)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self.identity(update)
        if user:
            if user.get("created"):
                await self.show_language(update)
            else:
                await self.show_menu(update, user)

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = await self.identity(update)
        if user:
            await self.show_menu(update, user)

    async def language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.identity(update):
            await self.show_language(update)

    async def show_language(self, update: Update) -> None:
        await self.render(update, t("el", "choose_language"),
                          [[button(label, "lang", code)] for code, label in LANGUAGES.items()])

    async def show_menu(self, update: Update, user: dict[str, Any], notice: str = "") -> None:
        lang = user["language"]
        active = self.service.resume_session(user["id"])
        rows = []
        if active:
            rows.append([button(t(lang, "resume", **active), "continue", active["id"])])
        rows.extend([[button(t(lang, "quick"), "quick")],
                     [button(t(lang, "history"), "history"), button(t(lang, "favorites"), "favorites", 0)],
                     [button(t(lang, "language"), "language")]])
        await self.render(update, (notice + "\n\n" if notice else "") + t(lang, "home"), rows)

    async def show_subjects(self, update: Update, language: str) -> None:
        counts = {entry["discipline"]: entry["count"] for entry in self.service.disciplines()}
        rows = [[button(f"{t(language, subject)} ({counts.get(subject, 0)})", "subject", subject)]
                for subject in SUBJECTS]
        rows.append([button(t(language, "menu"), "menu")])
        await self.render(update, t(language, "subject"), rows)

    async def show_question(self, update: Update, user: dict[str, Any], session_id: int) -> None:
        uid, lang = user["id"], user["language"]
        session = self.service.get_session(uid, session_id)
        if session["status"] != "ACTIVE":
            await self.show_result(update, user, session_id)
            return
        question = self.service.current_question(uid, session_id)
        if question is None:
            self.service.finish_session(uid, session_id)
            await self.show_result(update, user, session_id)
            return
        qid = question["id"]
        header = t(lang, "question", subject=t(lang, session["discipline"]),
                   number=session["answered"] + 1, total=session["total"])
        saved = any(item["id"] == qid for item in self.service.favorites(uid))
        answers = [button(chr(65 + index), "answer", session_id, qid, index)
                   for index in range(len(question["options"]))]
        rows = [answers[index:index + 5] for index in range(0, len(answers), 5)]
        rows.extend([[button(t(lang, "favorite_remove" if saved else "favorite_add"), "save", session_id, qid)],
                     [button(t(lang, "menu"), "menu")]])
        await self.render(update, header + "\n\n" + question_text(question, lang), rows)

    async def show_result(self, update: Update, user: dict[str, Any], session_id: int) -> None:
        summary = self.service.summary(user["id"], session_id)
        rows = []
        if summary["status"] == "ACTIVE":
            rows.append([button(t(user["language"], "resume", **summary), "continue", session_id)])
        rows.append([button(t(user["language"], "menu"), "menu")])
        await self.render(update, result_text(summary, user["language"]), rows)

    async def show_history(self, update: Update, user: dict[str, Any]) -> None:
        lang = user["language"]
        entries = self.service.history(user["id"], limit=10)
        rows = [[button(f"#{entry['id']} · {t(lang, entry['discipline'])} · {entry['answered']}/{entry['total']}",
                        "result", entry["id"])] for entry in entries]
        rows.append([button(t(lang, "menu"), "menu")])
        await self.render(update, t(lang, "history_title" if entries else "empty_history"), rows)

    async def show_favorites(self, update: Update, user: dict[str, Any], page: int = 0) -> None:
        lang = user["language"]
        favorites = self.service.favorites(user["id"])
        pages = max(1, (len(favorites) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        rows = [[button(item["text"].replace("\n", " ")[:55],
                        "favorite", page, item["id"])]
                for item in favorites[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        navigation = []
        if page:
            navigation.append(button(t(lang, "previous"), "favorites", page - 1))
        if page < pages - 1:
            navigation.append(button(t(lang, "more"), "favorites", page + 1))
        if navigation:
            rows.append(navigation)
        rows.append([button(t(lang, "menu"), "menu")])
        text = t(lang, "favorites_title", page=page + 1, pages=pages) if favorites else t(lang, "empty_favorites")
        await self.render(update, text, rows)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        try:
            await query.answer()
        except TelegramError:
            # Expired queries still have their session/question checked below.
            pass
        user = await self.identity(update)
        if not user:
            return
        try:
            await self.dispatch(update, user, query.data)
        except (ValueError, LookupError, PermissionError, TypeError):
            await self.show_menu(update, user, t(user["language"], "stale"))

    async def dispatch(self, update: Update, user: dict[str, Any], payload: Any) -> None:
        if not isinstance(payload, str) or len(payload.encode("utf-8")) > 64:
            raise ValueError("Invalid callback")
        parts = payload.split(":")
        action, args = parts[0], parts[1:]
        uid, lang = user["id"], user["language"]
        if action == "menu" and not args:
            await self.show_menu(update, user)
        elif action == "language" and not args:
            await self.show_language(update)
        elif action == "lang" and len(args) == 1 and args[0] in LANGUAGES:
            user = self.service.set_language(uid, args[0])
            await self.show_menu(update, user)
        elif action == "quick" and not args:
            await self.show_subjects(update, lang)
        elif action == "subject" and len(args) == 1 and args[0] in SUBJECTS:
            available = next((item["count"] for item in self.service.disciplines()
                              if item["discipline"] == args[0]), 0)
            rows = [[button(str(count), "begin", args[0], count) for count in (10, 25, 50)]] if available else []
            rows.append([button(t(lang, "back"), "quick")])
            await self.render(update, t(lang, "count", subject=t(lang, args[0]), available=available)
                              if available else t(lang, "unavailable"), rows)
        elif action in ("begin", "replace") and len(args) == 2 and args[0] in SUBJECTS and args[1] in ("10", "25", "50"):
            active = self.service.resume_session(uid)
            if active and action == "begin":
                await self.render(update, t(lang, "replace", **active), [
                    [button(t(lang, "resume", **active), "continue", active["id"])],
                    [button(t(lang, "start_new"), "replace", *args)],
                    [button(t(lang, "menu"), "menu")]])
            else:
                session = self.service.start_session(uid, args[0], int(args[1]))
                await self.show_question(update, user, session["id"])
        elif action == "continue" and len(args) == 1:
            await self.show_question(update, user, int(args[0]))
        elif action == "answer" and len(args) == 3:
            session_id, qid, option = int(args[0]), args[1], int(args[2])
            result = self.service.answer(uid, session_id, qid, option)
            if not result["accepted"]:
                await self.show_menu(update, user, t(lang, "stale"))
                return
            feedback = result["feedback"]
            text = t(lang, "saved_answer", answer=chr(65 + option))
            if feedback["verified"] and feedback.get("correct_option") is not None:
                text += "\n\n" + t(lang, "correct" if feedback["correct"] else "incorrect",
                                     answer=chr(65 + feedback["correct_option"]), status=feedback["answer_status"])
            else:
                text += "\n\n" + t(lang, "unverified")
            session = result["session"]
            finished = session["answered"] >= session["total"]
            if finished:
                self.service.finish_session(uid, session_id)
            rows = [[button(t(lang, "results" if finished else "next"), "result" if finished else "continue", session_id)],
                    [button(t(lang, "menu"), "menu")]]
            await self.render(update, text, rows)
        elif action == "save" and len(args) == 2:
            session_id, qid = int(args[0]), args[1]
            question = self.service.current_question(uid, session_id)
            if not question or question["id"] != qid:
                raise ValueError("Stale question")
            self.service.toggle_favorite(uid, qid)
            await self.show_question(update, user, session_id)
        elif action == "history" and not args:
            await self.show_history(update, user)
        elif action == "result" and len(args) == 1:
            await self.show_result(update, user, int(args[0]))
        elif action == "favorites" and len(args) == 1:
            await self.show_favorites(update, user, int(args[0]))
        elif action in ("favorite", "remove") and len(args) == 2:
            page, qid = int(args[0]), args[1]
            question = next((item for item in self.service.favorites(uid) if item["id"] == qid), None)
            if question is None:
                raise LookupError("Favorite no longer present")
            if action == "remove":
                self.service.toggle_favorite(uid, qid)
                await self.show_favorites(update, user, page)
            else:
                await self.render(update, question_text(question, lang), [
                    [button(t(lang, "favorite_remove"), "remove", page, qid)],
                    [button(t(lang, "back"), "favorites", page)]])
        else:
            raise ValueError("Unknown callback")

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Exception messages can contain database URLs or HTTP request tokens.
        LOGGER.error("Bot update failed (%s)", type(context.error).__name__)
        if isinstance(update, Update):
            user = update.effective_user
            lang = user.language_code if user and user.language_code in LANGUAGES else "en"
            try:
                await self.render(update, t(lang, "error"), [[button(t(lang, "menu"), "menu")]])
            except TelegramError:
                LOGGER.warning("Could not deliver error notice")


def build_application(token: str, service: Any, allowed_user_ids: set[int] | None = None) -> Application:
    """Build a sequential long-polling application; the entry point runs polling."""
    configure_safe_logging(token)
    ui = BotUI(service, allowed_user_ids)
    application = Application.builder().token(token).concurrent_updates(False).build()
    application.add_handler(CommandHandler("start", ui.start))
    application.add_handler(CommandHandler("menu", ui.menu))
    application.add_handler(CommandHandler("language", ui.language))
    application.add_handler(CallbackQueryHandler(ui.callback))
    application.add_error_handler(ui.error)
    return application
