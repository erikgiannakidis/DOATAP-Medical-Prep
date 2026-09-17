"""Telegram presentation layer. Persistent user/session state belongs to BotService."""

from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .i18n import LANGUAGES, translate as t
from .translation import translation_links

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


def feedback_text(feedback: dict[str, Any], language: str) -> str:
    """A key can be shown only when it belonged to this verified attempt."""
    if (feedback["verified"] and type(feedback.get("correct_option")) is int
            and type(feedback.get("correct")) is bool):
        return t(language, "correct" if feedback["correct"] else "incorrect",
                 answer=chr(65 + feedback["correct_option"]), status=feedback["answer_status"])
    return t(language, "unverified")


def answer_marker(attempt: dict[str, Any]) -> str:
    if not attempt["verified"] or attempt.get("correct") is None:
        return "?"
    return "✓" if attempt["correct"] else "✗"


def pagination(language: str, page: dict[str, Any], *prefix: object,
               previous: str = "previous", more: str = "more") -> list[list[InlineKeyboardButton]]:
    row = []
    if page["page"] > 0:
        row.append(button(t(language, previous), *prefix, page["page"] - 1))
    if page["page"] + 1 < page["pages"]:
        row.append(button(t(language, more), *prefix, page["page"] + 1))
    return [row] if row else []


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
        rows.extend([[button(t(lang, "study"), "bank")],
                     [button(t(lang, "quick"), "quick")],
                     [button(t(lang, "my_progress"), "progress"), button(t(lang, "history"), "history")],
                     [button(t(lang, "favorites"), "favorites", 0), button(t(lang, "language"), "language")]])
        await self.render(update, (notice + "\n\n" if notice else "") + t(lang, "home"), rows)

    async def show_subjects(self, update: Update, language: str) -> None:
        counts = {entry["discipline"]: entry["count"] for entry in self.service.disciplines()}
        rows = [[button(f"{t(language, subject)} ({counts.get(subject, 0)})", "subject", subject)]
                for subject in SUBJECTS]
        rows.append([button(t(language, "menu"), "menu")])
        await self.render(update, t(language, "subject"), rows)

    async def show_bank(self, update: Update, user: dict[str, Any]) -> None:
        lang = user["language"]
        progress = self.service.progress(user["id"])
        counts = {entry["discipline"]: entry for entry in progress["disciplines"]}
        rows = [[button(t(lang, "study_scope", subject=t(lang, "ALL"), remaining=progress["remaining"]),
                        "study", "ALL")]]
        for subject in SUBJECTS:
            rows.append([button(t(lang, "study_scope", subject=t(lang, subject),
                                  remaining=counts.get(subject, {}).get("remaining", 0)), "study", subject)])
        rows.extend([[button(t(lang, "my_progress"), "progress")], [button(t(lang, "menu"), "menu")]])
        await self.render(update, t(lang, "study_intro"), rows)

    async def start_study(self, update: Update, user: dict[str, Any], scope: str, *,
                          replace=False, expected_session_id: int | None = None) -> None:
        uid, lang = user["id"], user["language"]
        progress = self.service.progress(uid)
        selected = progress if scope == "ALL" else next(
            (entry for entry in progress["disciplines"] if entry["discipline"] == scope),
            {"total": 0, "remaining": 0})
        if not selected["remaining"]:
            text = t(lang, "study_done", subject=t(lang, scope)) if selected["total"] else t(lang, "unavailable")
            await self.render(update, text, [[button(t(lang, "my_progress"), "progress")],
                                              [button(t(lang, "menu"), "menu")]])
            return
        active = self.service.resume_session(uid)
        if replace and (not active or active["id"] != expected_session_id):
            if not active:
                await self.show_menu(update, user, t(lang, "stale"))
                return
            replace = False
        if active and not replace:
            await self.render(update, t(lang, "study_replace", **active), [
                [button(t(lang, "resume", **active), "continue", active["id"])],
                [button(t(lang, "study_start"), "study_replace", scope, active["id"])],
                [button(t(lang, "menu"), "menu")]])
            return
        session = self.service.start_study_session(uid, discipline=scope, count=25)
        if session is None:
            await self.show_menu(update, user, t(lang, "study_done", subject=t(lang, scope)))
        else:
            await self.show_question(update, user, session["id"])

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
        header = t(lang, "question", subject=t(lang, question["discipline"]),
                   number=session["answered"] + 1, total=session["total"])
        saved = any(item["id"] == qid for item in self.service.favorites(uid))
        answers = [button(chr(65 + index), "answer", session_id, qid, index)
                   for index in range(len(question["options"]))]
        rows = [answers[index:index + 5] for index in range(0, len(answers), 5)]
        rows.extend([[button(t(lang, "favorite_remove" if saved else "favorite_add"), "save", session_id, qid)],
                     [button(t(lang, "translate"), "trq", session_id, qid)],
                     [button(t(lang, "menu"), "menu")]])
        await self.render(update, header + "\n\n" + question_text(question, lang), rows)

    async def show_result(self, update: Update, user: dict[str, Any], session_id: int) -> None:
        summary = self.service.summary(user["id"], session_id)
        rows = []
        if summary["status"] == "ACTIVE":
            rows.append([button(t(user["language"], "resume", **summary), "continue", session_id)])
        else:
            rows.append([button(t(user["language"], "study_next"), "study", summary["discipline"])])
        rows.append([button(t(user["language"], "my_progress"), "progress")])
        rows.append([button(t(user["language"], "menu"), "menu")])
        await self.render(update, result_text(summary, user["language"]), rows)

    async def show_history(self, update: Update, user: dict[str, Any]) -> None:
        # Preserve already-sent "history" callbacks while expanding the view.
        await self.show_progress(update, user)

    async def show_progress(self, update: Update, user: dict[str, Any]) -> None:
        uid, lang = user["id"], user["language"]
        progress = self.service.progress(uid)
        lines = [t(lang, "coverage", **progress), ""]
        rows = []
        active = self.service.resume_session(uid)
        if active:
            rows.append([button(t(lang, "resume", **active), "continue", active["id"])])
        if progress["remaining"]:
            rows.append([button(t(lang, "study_start"), "study", "ALL")])
        rows.append([button(t(lang, "answer_history"), "ql", "ALL", 0)])
        for entry in progress["disciplines"]:
            if entry["discipline"] not in SUBJECTS:
                continue
            label = t(lang, "coverage_subject", subject=t(lang, entry["discipline"]), **entry)
            lines.append(label)
            rows.append([button(label, "ql", entry["discipline"], 0)])
        lines.extend(["", t(lang, "coverage_note")])
        rows.extend([[button(t(lang, "session_history"), "sessions", 0)],
                     [button(t(lang, "menu"), "menu")]])
        await self.render(update, "\n".join(lines), rows)

    async def show_sessions(self, update: Update, user: dict[str, Any], page: int = 0) -> None:
        lang = user["language"]
        data = self.service.session_history(user["id"], page=page, page_size=PAGE_SIZE)
        rows = [[button(f"#{entry['id']} · {t(lang, entry['discipline'])} · {entry['answered']}/{entry['total']}",
                        "result", entry["id"])] for entry in data["items"]]
        rows.extend(pagination(lang, data, "sessions"))
        rows.extend([[button(t(lang, "my_progress"), "progress")], [button(t(lang, "menu"), "menu")]])
        text = (t(lang, "tests_page", page=data["page"] + 1, pages=data["pages"])
                if data["items"] else t(lang, "empty_history"))
        await self.render(update, text, rows)

    async def show_answers(self, update: Update, user: dict[str, Any], scope: str, page: int) -> None:
        lang = user["language"]
        data = self.service.question_history(user["id"], discipline=None if scope == "ALL" else scope,
                                             page=page, page_size=PAGE_SIZE)
        rows = []
        for entry in data["items"]:
            stem = entry["snapshot"]["text"].replace("\n", " ")[:45]
            label = f"{answer_marker(entry)} {chr(65 + entry['selected_option'])} · ×{entry['attempt_count']} · {stem}"
            rows.append([button(label, "qa", scope, data["page"], entry["question_id"], 0)])
        rows.extend(pagination(lang, data, "ql", scope))
        if not data["items"]:
            text = t(lang, "empty_answers")
            rows.append([button(t(lang, "study_start"), "study", scope)])
        else:
            text = t(lang, "answered_title", subject=t(lang, scope), total=data["total"],
                     page=data["page"] + 1, pages=data["pages"]) + "\n\n" + t(lang, "answer_legend")
        rows.extend([[button(t(lang, "my_progress"), "progress")], [button(t(lang, "menu"), "menu")]])
        await self.render(update, text, rows)

    async def show_attempt(self, update: Update, user: dict[str, Any], scope: str,
                           list_page: int, question_id: str, page: int) -> None:
        lang = user["language"]
        # One attempt per page preserves the exact question/options snapshot if
        # a question's reviewed key or wording changed between attempts.
        data = self.service.question_attempts(user["id"], question_id, page=page, page_size=1)
        if not data["items"]:
            raise LookupError("No own attempt for this question")
        attempt = data["items"][0]
        snapshot = attempt["snapshot"]
        title = t(lang, "attempt_title", subject=t(lang, snapshot["discipline"]),
                  number=data["total"] - data["page"], total=data["total"],
                  date=str(attempt["answered_at"]).replace("T", " "))
        if data["page"] == 0:
            title += "\n" + t(lang, "latest_attempt")
        selected = attempt["selected_option"]
        choice = f"{chr(65 + selected)}. {snapshot['options'][selected]}"
        feedback = {**attempt, "answer_status": snapshot["answer_status"],
                    "correct_option": snapshot.get("correct_option")}
        text = "\n\n".join([title, question_text(snapshot, lang),
                              t(lang, "selected_answer", answer=choice), feedback_text(feedback, lang)])
        rows = pagination(lang, data, "qa", scope, list_page, question_id,
                          previous="newer_attempt", more="older_attempt")
        rows.extend([[button(t(lang, "translate"), "trh", scope, list_page, question_id, data["page"])],
                     [button(t(lang, "back"), "ql", scope, list_page)],
                     [button(t(lang, "my_progress"), "progress")]])
        await self.render(update, text, rows)

    async def show_translation(self, update: Update, user: dict[str, Any],
                               question: dict[str, Any], *back: object) -> None:
        lang = user["language"]
        links = translation_links(question)
        rows = [[InlineKeyboardButton(t(lang, "translate_ru"), url=links["ru"])],
                [InlineKeyboardButton(t(lang, "translate_en"), url=links["en"])],
                [button(t(lang, "back"), *back)]]
        await self.render(update, t(lang, "translation_note"), rows)

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
        elif action == "bank" and not args:
            await self.show_bank(update, user)
        elif action == "study" and len(args) == 1 and args[0] in ("ALL", *SUBJECTS):
            await self.start_study(update, user, args[0])
        elif action == "study_replace" and len(args) in (1, 2) and args[0] in ("ALL", *SUBJECTS):
            await self.start_study(update, user, args[0], replace=True,
                                  expected_session_id=int(args[1]) if len(args) == 2 else None)
        elif action == "subject" and len(args) == 1 and args[0] in SUBJECTS:
            available = next((item["count"] for item in self.service.disciplines()
                              if item["discipline"] == args[0]), 0)
            rows = [[button(str(count), "begin", args[0], count) for count in (10, 25, 50)]] if available else []
            rows.append([button(t(lang, "back"), "quick")])
            await self.render(update, t(lang, "count", subject=t(lang, args[0]), available=available)
                              if available else t(lang, "unavailable"), rows)
        elif (action in ("begin", "replace") and len(args) in (2, 3) and args[0] in SUBJECTS
              and args[1] in ("10", "25", "50") and (action == "replace" or len(args) == 2)):
            active = self.service.resume_session(uid)
            confirmed = (action == "replace" and len(args) == 3 and active
                         and active["id"] == int(args[2]))
            if action == "replace" and not active:
                await self.show_menu(update, user, t(lang, "stale"))
            elif active and not confirmed:
                await self.render(update, t(lang, "replace", **active), [
                    [button(t(lang, "resume", **active), "continue", active["id"])],
                    [button(t(lang, "start_new"), "replace", args[0], args[1], active["id"])],
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
            text += "\n\n" + feedback_text(feedback, lang)
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
        elif action == "progress" and not args:
            await self.show_progress(update, user)
        elif action == "sessions" and len(args) == 1:
            await self.show_sessions(update, user, int(args[0]))
        elif action == "ql" and len(args) == 2 and args[0] in ("ALL", *SUBJECTS):
            await self.show_answers(update, user, args[0], int(args[1]))
        elif action == "qa" and len(args) == 4 and args[0] in ("ALL", *SUBJECTS):
            await self.show_attempt(update, user, args[0], int(args[1]), args[2], int(args[3]))
        elif action == "trq" and len(args) == 2:
            session_id, qid = int(args[0]), args[1]
            question = self.service.current_question(uid, session_id)
            if not question or question["id"] != qid:
                raise LookupError("Question is no longer current")
            await self.show_translation(update, user, question, "continue", session_id)
        elif action == "trh" and len(args) == 4 and args[0] in ("ALL", *SUBJECTS):
            scope, list_page, qid, page = args[0], int(args[1]), args[2], int(args[3])
            data = self.service.question_attempts(uid, qid, page=page, page_size=1)
            if not data["items"]:
                raise LookupError("No own attempt for translation")
            await self.show_translation(update, user, data["items"][0]["snapshot"],
                                        "qa", scope, list_page, qid, data["page"])
        elif action == "trf" and len(args) == 2:
            page, qid = int(args[0]), args[1]
            question = next((item for item in self.service.favorites(uid) if item["id"] == qid), None)
            if question is None:
                raise LookupError("Favorite no longer present")
            await self.show_translation(update, user, question, "favorite", page, qid)
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
                    [button(t(lang, "translate"), "trf", page, qid)],
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
