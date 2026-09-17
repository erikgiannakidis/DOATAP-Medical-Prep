"""Exercise Telegram flows against real persistent services, without network calls."""

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from telegram import Chat, Message, Update, User

from doatap.bot import BotUI, TokenRedactionFilter, build_application, result_text, split_message
from doatap.i18n import LANGUAGES, TEXT
from doatap.service import create_service


def make_update(data=None, telegram_id=123, chat_type="private", language="ru"):
    message = SimpleNamespace(reply_text=AsyncMock())
    query = None if data is None else SimpleNamespace(
        data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id, language_code=language),
        effective_chat=SimpleNamespace(type=chat_type), effective_message=message,
        callback_query=query,
    )


def last_render(update):
    call = (update.callback_query.edit_message_text.call_args if update.callback_query
            else update.effective_message.reply_text.call_args)
    return call.args[0], call.kwargs.get("reply_markup")


@pytest.fixture
def service(tmp_path):
    bank = tmp_path / "bank.jsonl"
    records = [
        {"id": f"a25_{index:016x}", "discipline": "ANATOMY",
         "text": f"Ποια είναι η σωστή απάντηση {index};",
         "original_text": f"Ποια είναι η σωστή απάντηση {index};\nΑ. Πρώτη\nΒ. Δεύτερη",
         "options": ["Πρώτη", "Δεύτερη"], "status": "READY",
         "answer_status": "UNVERIFIED", "source_page": 1, "source_year": 2025,
         "source_document": "Επίσημη πηγή.pdf"}
        for index in range(10)
    ]
    bank.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records), encoding="utf-8")
    service = create_service(f"sqlite:///{tmp_path / 'bot.db'}")
    service.import_bank(bank)
    return service


async def click(ui, data, telegram_id=123):
    update = make_update(data, telegram_id)
    await ui.callback(update, None)
    update.callback_query.answer.assert_awaited_once()
    return update


def callbacks(update):
    return [item.callback_data for row in last_render(update)[1].inline_keyboard for item in row]


@pytest.fixture
def study_service(service, tmp_path):
    records = []
    for subject, count in (("PHYSIOLOGY", 12), ("PHARMACOLOGY", 8)):
        for index in range(count):
            records.append({
                "id": f"{subject[:3]}_{index}", "discipline": subject,
                "text": f"Ερώτηση {subject} {index};", "original_text": "audit block",
                "options": ["Επιλογή πρώτη", "Επιλογή δεύτερη"], "status": "READY",
                "answer_status": "UNVERIFIED", "source_document": "Επίσημη πηγή.pdf",
                "source_page": index + 2, "source_year": 2025,
            })
    path = tmp_path / "more.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    service.import_bank(path)
    return service


async def complete_via_ui(ui, service, uid, session_id, option=0):
    seen = []
    while question := service.current_question(uid, session_id):
        seen.append(question["id"])
        await click(ui, f"answer:{session_id}:{question['id']}:{option}")
    return seen


@pytest.mark.asyncio
async def test_complete_first_run_with_unverified_result_and_history(service):
    ui = BotUI(service)
    update = make_update()
    await ui.start(update, None)
    text, keyboard = last_render(update)
    assert "Choose a language" in text
    assert {row[0].callback_data for row in keyboard.inline_keyboard} == {"lang:ru", "lang:en", "lang:el"}

    update = await click(ui, "lang:ru")
    assert "Быстрый тест" in str(last_render(update)[1])
    await click(ui, "quick")
    update = await click(ui, "subject:ANATOMY")
    assert [b.text for b in last_render(update)[1].inline_keyboard[0]] == ["10", "25", "50"]
    update = await click(ui, "begin:ANATOMY:10")
    assert "Вопрос 1/10" in last_render(update)[0]
    uid = service.upsert_user(123)["id"]
    session = service.resume_session(uid)

    for index in range(10):
        question = service.current_question(uid, session["id"])
        update = await click(ui, f"answer:{session['id']}:{question['id']}:0")
        feedback, keyboard = last_render(update)
        assert "UNVERIFIED" in feedback
        assert "Верно." not in feedback and "неверный" not in feedback
        if index < 9:
            assert keyboard.inline_keyboard[0][0].text == "Следующий вопрос"
            await click(ui, keyboard.inline_keyboard[0][0].callback_data)

    assert service.resume_session(uid) is None
    update = await click(ui, f"result:{session['id']}")
    text, _ = last_render(update)
    assert "Тест завершён" in text and "UNVERIFIED): 10" in text
    assert "Оценка не рассчитана" in text and "%" not in text
    update = await click(ui, "history")
    assert "Уникальных вопросов отвечено: 10/10" in last_render(update)[0]
    assert "Без проверенного ключа: 10" in last_render(update)[0]
    assert "ql:ALL:0" in str(last_render(update)[1])
    update = await click(ui, "sessions:0")
    assert f"result:{session['id']}" in str(last_render(update)[1])
    assert service.history(uid)[0]["answered"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["el", "ru", "en"])
async def test_question_content_stays_greek_and_has_no_duplicate_options(service, language):
    uid = service.upsert_user(123, language)["id"]
    session = service.start_session(uid, "ANATOMY", 10)
    question = service.current_question(uid, session["id"])
    ui = BotUI(service)
    update = await click(ui, f"continue:{session['id']}")
    text, keyboard = last_render(update)
    assert question["text"] in text
    assert text.count("Πρώτη") == text.count("Δεύτερη") == 1
    assert "Επίσημη πηγή.pdf" in text
    assert [item.text for item in keyboard.inline_keyboard[0]] == ["A", "B"]
    for row in keyboard.inline_keyboard:
        for item in row:
            assert len(item.callback_data.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_resume_and_favorites_survive_recreated_ui(service):
    uid = service.upsert_user(123, "ru")["id"]
    session = service.start_session(uid, "ANATOMY", 10)
    question = service.current_question(uid, session["id"])
    await click(BotUI(service), f"save:{session['id']}:{question['id']}")
    await click(BotUI(service), f"answer:{session['id']}:{question['id']}:0")
    ui = BotUI(service)
    update = make_update()
    await ui.menu(update, None)
    assert "Продолжить (1/10)" in str(last_render(update)[1])
    update = await click(ui, f"continue:{session['id']}")
    assert "Вопрос 2/10" in last_render(update)[0]
    update = await click(ui, "favorites:0")
    assert question["text"][:55] in str(last_render(update)[1])
    update = await click(ui, f"favorite:0:{question['id']}")
    assert question["text"] in last_render(update)[0]
    await click(ui, f"remove:0:{question['id']}")
    assert service.favorites(uid) == []


@pytest.mark.asyncio
async def test_duplicate_stale_and_foreign_callbacks_do_not_advance(service):
    uid = service.upsert_user(123, "ru")["id"]
    session = service.start_session(uid, "ANATOMY", 10)
    question = service.current_question(uid, session["id"])
    callback = f"answer:{session['id']}:{question['id']}:0"
    ui = BotUI(service)
    foreign = await click(ui, callback, telegram_id=456)
    assert "Ποια" not in last_render(foreign)[0]
    assert service.get_session(uid, session["id"])["answered"] == 0
    await click(ui, callback)
    update = await click(ui, callback)
    assert "устарела" in last_render(update)[0]
    assert service.get_session(uid, session["id"])["answered"] == 1
    await click(ui, f"save:{session['id']}:{question['id']}")
    assert service.favorites(uid) == []


@pytest.mark.asyncio
async def test_starting_new_test_requires_explicit_replacement(service):
    uid = service.upsert_user(123, "en")["id"]
    first = service.start_session(uid, "ANATOMY", 10)
    ui = BotUI(service)
    update = await click(ui, "begin:ANATOMY:25")
    assert "unfinished test" in last_render(update)[0]
    assert service.resume_session(uid)["id"] == first["id"]
    await click(ui, f"replace:ANATOMY:25:{first['id']}")
    assert service.resume_session(uid)["id"] != first["id"]
    assert service.get_session(uid, first["id"])["status"] == "ABANDONED"


@pytest.mark.asyncio
async def test_group_and_uninvited_users_never_access_service():
    class ForbiddenService:
        def upsert_user(self, *args):
            raise AssertionError("Must not touch persistence for rejected users")

    ui = BotUI(ForbiddenService(), {123})
    group = make_update("quick", chat_type="group")
    await ui.callback(group, None)
    group.callback_query.answer.assert_awaited_once()
    assert "личный чат" in last_render(group)[0]
    rejected = await click(ui, "quick", telegram_id=999)
    assert "приглашённым" in last_render(rejected)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["answer:bad:q:0", "lang:xx", "answer:1", "arbitrary", "x" * 65])
async def test_malformed_callbacks_receive_safe_menu(service, payload):
    service.upsert_user(123, "ru")
    update = await click(BotUI(service), payload)
    assert "устарела" in last_render(update)[0]


def test_score_denominator_excludes_unverified():
    summary = {"status": "COMPLETED", "discipline": "ANATOMY", "answered": 10,
               "total": 10, "correct": 1, "scored_total": 2, "unverified": 8}
    text = result_text(summary, "en")
    assert "50.0%" in text and "1/2 correct" in text and "UNVERIFIED): 8" in text
    assert "pass" not in text.lower()


def test_split_preserves_long_unicode_content_within_telegram_limit():
    text = "🫀Καρδιά\n" * 1000
    parts = split_message(text)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)


def test_token_is_redacted_from_formatted_http_log_and_exception():
    token = "123456789:TEST_secret_Abc"
    record = logging.LogRecord("httpx", logging.ERROR, "", 0,
                               "POST %s", (f"https://api.telegram.org/bot{token}/getUpdates",),
                               (RuntimeError, RuntimeError(token), None))
    TokenRedactionFilter(token).filter(record)
    assert token not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_error_handler_does_not_expose_exception_details(caplog):
    token = "123456789:do_not_expose"
    update = Update(1, message=Message(
        1, datetime.now(timezone.utc), Chat(123, "private"),
        from_user=User(123, "Pilot", False, language_code="ru"), text="/menu"))
    ui = BotUI(None)
    ui.render = AsyncMock()
    with caplog.at_level(logging.ERROR):
        await ui.error(update, SimpleNamespace(error=RuntimeError(f"DB password and {token}")))
    assert token not in caplog.text and "DB password" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert token not in ui.render.call_args.args[1]
    assert "Произошла ошибка" in ui.render.call_args.args[1]


def test_application_builds_without_network_and_ui_translations_are_complete(service):
    application = build_application("123456789:TEST_only_not_a_real_token", service, {123})
    assert len(application.handlers[0]) == 4
    assert application.concurrent_updates == 1
    assert all(set(TEXT[language]) == set(TEXT["en"]) for language in LANGUAGES)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ru", "en", "el"])
async def test_study_entire_bank_in_25_question_batches_without_repeats(study_service, language):
    service = study_service
    uid = service.upsert_user(123, language)["id"]
    ui = BotUI(service)
    update = await click(ui, "menu")
    assert "bank" in callbacks(update) and "progress" in callbacks(update)
    update = await click(ui, "bank")
    assert {"study:ALL", "study:ANATOMY", "study:PHYSIOLOGY", "study:PHARMACOLOGY"}.issubset(callbacks(update))
    update = await click(ui, "study:ALL")
    first = service.resume_session(uid)
    assert first["discipline"] == "ALL" and first["total"] == 25
    seen = await complete_via_ui(ui, service, uid, first["id"])
    update = await click(ui, f"result:{first['id']}")
    assert "study:ALL" in callbacks(update)
    await click(ui, "study:ALL")
    second = service.resume_session(uid)
    assert second["total"] == 5
    seen += await complete_via_ui(ui, service, uid, second["id"])
    assert len(seen) == len(set(seen)) == 30
    update = await click(ui, "progress")
    text, _ = last_render(update)
    assert "30/30" in text
    progress = service.progress(uid)
    assert progress["remaining"] == progress["latest_correct"] == progress["latest_wrong"] == 0
    assert progress["latest_unverified"] == 30
    update = await click(ui, "study:ALL")
    assert service.resume_session(uid) is None
    assert "progress" in callbacks(update)


@pytest.mark.asyncio
async def test_study_replacement_preserves_answered_questions_and_resume(study_service):
    service = study_service
    uid = service.upsert_user(123, "ru")["id"]
    old = service.start_session(uid, "ANATOMY", 10)
    question = service.current_question(uid, old["id"])
    service.answer(uid, old["id"], question["id"], 0)
    ui = BotUI(service)
    update = await click(ui, "study:ALL")
    assert f"continue:{old['id']}" in callbacks(update)
    assert f"study_replace:ALL:{old['id']}" in callbacks(update)
    assert service.resume_session(uid)["id"] == old["id"]
    await click(ui, f"study_replace:ALL:{old['id']}")
    new = service.resume_session(uid)
    assert new["id"] != old["id"]
    current = service.current_question(uid, new["id"])
    await click(ui, f"answer:{new['id']}:{current['id']}:0")
    ui = BotUI(service)
    update = await click(ui, "menu")
    assert f"continue:{new['id']}" in callbacks(update)
    seen = [current["id"]] + await complete_via_ui(ui, service, uid, new["id"])
    assert question["id"] not in seen
    assert service.get_session(uid, old["id"])["status"] == "ABANDONED"
    assert service.progress(uid)["answered_unique"] == 26


@pytest.mark.asyncio
async def test_empty_question_history_starts_selected_unanswered_subject(study_service):
    uid = study_service.upsert_user(123, "ru")["id"]
    ui = BotUI(study_service)
    update = await click(ui, "ql:PHYSIOLOGY:0")
    assert "ещё не отвечали" in last_render(update)[0]
    assert "study:PHYSIOLOGY" in callbacks(update)
    await click(ui, "study:PHYSIOLOGY")
    session = study_service.resume_session(uid)
    assert session["discipline"] == "PHYSIOLOGY" and session["total"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "study"])
async def test_replayed_replacement_confirmation_preserves_new_active_session(service, mode):
    uid = service.upsert_user(123, "ru")["id"]
    old = service.start_session(uid, "ANATOMY", 10)
    ui = BotUI(service)
    initial = "begin:ANATOMY:25" if mode == "quick" else "study:ALL"
    confirm = f"replace:ANATOMY:25:{old['id']}" if mode == "quick" else f"study_replace:ALL:{old['id']}"
    update = await click(ui, initial)
    assert confirm in callbacks(update)
    await click(ui, confirm)
    new = service.resume_session(uid)
    assert new["id"] != old["id"]
    question = service.current_question(uid, new["id"])
    service.answer(uid, new["id"], question["id"], 0)
    update = await click(ui, confirm)
    assert service.resume_session(uid)["id"] == new["id"]
    assert service.resume_session(uid)["answered"] == 1
    assert f"continue:{new['id']}" in callbacks(update)
    # Old bot messages without an expected session ID must also reconfirm.
    legacy = "replace:ANATOMY:25" if mode == "quick" else "study_replace:ALL"
    await click(ui, legacy)
    assert service.resume_session(uid)["id"] == new["id"]


@pytest.mark.asyncio
async def test_history_paginates_questions_attempts_and_isolates_users(service):
    uid = service.upsert_user(123, "ru")["id"]
    ui = BotUI(service)
    for option in (0, 1):
        session = service.start_session(uid, "ANATOMY", 10)
        await complete_via_ui(ui, service, uid, session["id"], option)
    update = await click(ui, "history")
    assert "Уникальных вопросов отвечено: 10/10" in last_render(update)[0]
    assert "Попыток по текущему банку: 20" in last_render(update)[0]
    update = await click(ui, "ql:ALL:0")
    assert "ql:ALL:1" in callbacks(update)
    question_buttons = [item for row in last_render(update)[1].inline_keyboard for item in row
                        if item.callback_data.startswith("qa:")]
    assert len(question_buttons) == 8
    assert all(item.text.startswith("? B · ×2") for item in question_buttons)
    detail = question_buttons[0].callback_data
    qid = detail.split(":")[3]
    update = await click(ui, detail)
    text, _ = last_render(update)
    assert "Попытка 2/2" in text and "Ваш последний ответ" in text
    assert "Вы выбрали: B. Δεύτερη" in text
    assert "UNVERIFIED" in text and "неверный" not in text
    older = f"qa:ALL:0:{qid}:1"
    assert older in callbacks(update)
    update = await click(ui, older)
    assert "Попытка 1/2" in last_render(update)[0]
    assert "Вы выбрали: A. Πρώτη" in last_render(update)[0]
    assert "ql:ALL:0" in callbacks(update)
    update = await click(ui, "ql:ALL:1")
    assert len([item for item in callbacks(update) if item.startswith("qa:")]) == 2
    assert "ql:ALL:0" in callbacks(update)
    own_before = service.progress(uid)
    foreign = await click(ui, detail, telegram_id=456)
    assert "Ποια" not in last_render(foreign)[0]
    other_id = service.upsert_user(456, "ru")["id"]
    assert service.progress(other_id)["answered_unique"] == 0
    assert service.progress(uid) == own_before


@pytest.mark.asyncio
async def test_session_history_goes_beyond_first_ten(service):
    uid = service.upsert_user(123, "en")["id"]
    for _ in range(12):
        session = service.start_session(uid, "ANATOMY", 10)
        service.finish_session(uid, session["id"])
    ui = BotUI(service)
    update = await click(ui, "sessions:0")
    assert "sessions:1" in callbacks(update)
    update = await click(ui, "sessions:1")
    result_buttons = [item for item in callbacks(update) if item.startswith("result:")]
    assert len(result_buttons) == 4 and "result:1" in result_buttons


@pytest.mark.asyncio
async def test_verified_history_uses_attempt_key_and_latest_progress(service, tmp_path):
    record = {"id": "verified", "discipline": "ANATOMY", "text": "Επαληθευμένη ερώτηση;",
              "original_text": "Επαληθευμένη ερώτηση;", "options": ["Άλφα", "Βήτα"],
              "status": "READY", "answer_status": "MEDICAL_REVIEWED", "correct_option": 1}
    path = tmp_path / "verified.jsonl"
    path.write_text(json.dumps(record), encoding="utf-8")
    service.import_bank(path)
    uid = service.upsert_user(123, "ru")["id"]
    session = service.start_study_session(uid, count=25)
    while question := service.current_question(uid, session["id"]):
        service.answer(uid, session["id"], question["id"], 1)
    ui = BotUI(service)
    update = await click(ui, "progress")
    assert "Правильно (проверено): 1" in last_render(update)[0]
    assert "Без проверенного ключа: 10" in last_render(update)[0]
    update = await click(ui, "qa:ALL:0:verified:0")
    assert "Верно. Проверенный ответ: B (MEDICAL_REVIEWED)" in last_render(update)[0]


@pytest.mark.asyncio
async def test_translation_active_history_and_favorites_preserve_progress(service):
    uid = service.upsert_user(123, "ru")["id"]
    session = service.start_session(uid, "ANATOMY", 10)
    question = service.current_question(uid, session["id"])
    qid = question["id"]
    service.toggle_favorite(uid, qid)
    ui = BotUI(service)
    before = service.get_session(uid, session["id"])
    update = await click(ui, f"trq:{session['id']}:{qid}")
    text, keyboard = last_render(update)
    assert "Google Translate" in text and "машинный перевод" in text
    for index, language in enumerate(("ru", "en")):
        url = keyboard.inline_keyboard[index][0].url
        values = parse_qs(urlparse(url).query)
        assert values["tl"] == [language]
        assert question["text"] in values["text"][0]
        assert "A. Πρώτη" in values["text"][0] and "B. Δεύτερη" in values["text"][0]
    assert service.get_session(uid, session["id"]) == before
    assert f"continue:{session['id']}" in callbacks(update)
    update = await click(ui, f"trf:0:{qid}")
    assert f"favorite:0:{qid}" in callbacks(update)
    service.answer(uid, session["id"], qid, 1)
    before = service.progress(uid)
    update = await click(ui, f"trh:ALL:0:{qid}:0")
    assert f"qa:ALL:0:{qid}:0" in callbacks(update)
    assert service.progress(uid) == before
    update = await click(ui, f"trq:{session['id']}:{qid}")
    assert "устарела" in last_render(update)[0]
    for payload in (f"trq:{session['id']}:{qid}", f"trh:ALL:0:{qid}:0", f"trf:0:{qid}"):
        update = await click(ui, payload, telegram_id=456)
        assert not any(item.url for row in last_render(update)[1].inline_keyboard for item in row)
