"""Domain regression tests; optionally run against an isolated PostgreSQL schema."""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text

from doatap.db import init_db, make_engine, make_session_factory
from doatap.models import Answer, Question
from doatap.service import BotService, create_service


@pytest.fixture
def service(tmp_path):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url:
        # Never drop shared tables: all test objects live in a fresh namespace.
        base_engine = make_engine(database_url)
        if base_engine.dialect.name != "postgresql":
            pytest.fail("TEST_DATABASE_URL must point to a PostgreSQL test database")
        schema = "doatap_test_" + uuid.uuid4().hex
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = base_engine.execution_options(schema_translate_map={None: schema})
    else:
        base_engine = engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
        schema = None
    init_db(engine)
    yield BotService(make_session_factory(engine))
    if schema:
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    base_engine.dispose()


def question(qid="q1", **changes):
    return {
        "id": qid, "discipline": "anatomy", "text": "Example question?",
        "original_text": "Original question with A and B options",
        "options": {"A": "First", "B": "Second"},
        "source_page": 1, "source_year": 2024, "source_document": "example.pdf",
        "status": "READY", "answer_status": "UNVERIFIED", "correct_option": None,
        **changes,
    }


def import_questions(service, tmp_path, *records):
    path = tmp_path / "bank.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return service.import_bank(path)


def create_user(service, telegram_id=100):
    return service.upsert_user(telegram_id)["id"]


def test_existing_user_keeps_language(service):
    first = service.upsert_user(100)
    assert first["created"] is True
    service.set_language(first["id"], "ru")
    again = service.upsert_user(100)
    assert again["id"] == first["id"]
    assert again["created"] is False
    assert again["language"] == "ru"


def test_import_is_idempotent_and_preserves_reviewed_content(service, tmp_path):
    verified = question(answer_status="MEDICAL_REVIEWED", correct_option="B")
    assert import_questions(service, tmp_path, verified) == {"inserted": 1, "updated": 0, "skipped": 0}
    assert import_questions(service, tmp_path, verified)["skipped"] == 1
    changed = question(text="Changed question", options={"A": "New first", "B": "New second"})
    import_questions(service, tmp_path, changed)
    with service.session_factory() as db:
        saved = db.get(Question, "q1")
        assert saved.answer_status == "MEDICAL_REVIEWED"
        assert saved.correct_option == 1
        assert saved.text == verified["text"]
        assert saved.options == ["First", "Second"]


def test_import_unverified_key_is_ignored_and_validation_is_atomic(service, tmp_path):
    import_questions(service, tmp_path, question(correct_option="A"))
    with service.session_factory() as db:
        assert db.get(Question, "q1").correct_option is None
    with pytest.raises(ValueError, match="line 2"):
        import_questions(service, tmp_path, question("q2"), question("q3", options=["only one"]))
    with service.session_factory() as db:
        assert db.get(Question, "q2") is None


def test_only_ready_questions_available_and_short_bank_is_explicit(service, tmp_path):
    import_questions(service, tmp_path, question(), question("q2", status="NEEDS_REVIEW"))
    assert service.disciplines() == [{"discipline": "anatomy", "count": 1}]
    user_id = create_user(service)
    session = service.start_session(user_id, "anatomy", 10)
    assert session["total"] == 1
    assert session["requested_count"] == 10
    with pytest.raises(ValueError):
        service.start_session(user_id, "anatomy", 5)
    with pytest.raises(ValueError):
        service.start_session(user_id, "missing", 10)
    assert service.resume_session(user_id)["id"] == session["id"]


def test_unverified_answers_never_enter_score_denominator(service, tmp_path):
    import_questions(
        service, tmp_path,
        question("unknown", correct_option="A"),
        question("official", answer_status="OFFICIAL", correct_option="A"),
        question("reviewed", answer_status="MEDICAL_REVIEWED", correct_option="B"),
    )
    user_id = create_user(service)
    session = service.start_session(user_id, "anatomy")
    while current := service.current_question(user_id, session["id"]):
        assert "correct_option" not in current
        result = service.answer(user_id, session["id"], current["id"], 0)
        assert result["accepted"] is True
        if current["id"] == "unknown":
            assert result["feedback"]["correct"] is None
            assert result["feedback"]["correct_option"] is None
    summary = service.summary(user_id, session["id"])
    assert summary["status"] == "COMPLETED"
    assert summary["answered"] == 3
    assert summary["unverified"] == 1
    assert summary["scored_total"] == 2
    assert summary["correct"] == 1
    assert summary["percent"] == 50.0
    assert "passed" not in summary
    assert service.resume_session(user_id) is None


def test_unverified_only_summary_has_no_percentage(service, tmp_path):
    import_questions(service, tmp_path, question())
    user_id = create_user(service)
    session = service.start_session(user_id, "anatomy")
    service.answer(user_id, session["id"], "q1", 0)
    summary = service.summary(user_id, session["id"])
    assert summary["scored_total"] == 0
    assert summary["percent"] is None


def test_duplicate_stale_and_invalid_answers_do_not_advance(service, tmp_path):
    import_questions(service, tmp_path, question("q1"), question("q2"))
    user_id = create_user(service)
    session = service.start_session(user_id, "anatomy")
    first = service.current_question(user_id, session["id"])["id"]
    other = "q2" if first == "q1" else "q1"
    stale = service.answer(user_id, session["id"], other, 0)
    assert stale["reason"] == "stale"
    assert stale["session"]["answered"] == 0
    with pytest.raises(ValueError, match="Invalid answer option"):
        service.answer(user_id, session["id"], first, 2)
    assert service.get_session(user_id, session["id"])["answered"] == 0
    accepted = service.answer(user_id, session["id"], first, 0)
    duplicate = service.answer(user_id, session["id"], first, 1)
    assert accepted["accepted"] is True
    assert duplicate["accepted"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["feedback"]["selected_option"] == 0
    assert duplicate["session"]["answered"] == 1
    assert service.current_question(user_id, session["id"])["id"] == other


def test_old_session_callback_and_cross_user_access_cannot_change_progress(service, tmp_path):
    import_questions(service, tmp_path, question())
    owner = create_user(service)
    other = create_user(service, 200)
    previous = service.start_session(owner, "anatomy")
    active = service.start_session(owner, "anatomy")
    old = service.answer(owner, previous["id"], "q1", 0)
    assert old["accepted"] is False
    assert old["session"]["status"] == "ABANDONED"
    for method, args in (
        (service.get_session, (other, active["id"])),
        (service.current_question, (other, active["id"])),
        (service.answer, (other, active["id"], "q1", 0)),
        (service.summary, (other, active["id"])),
        (service.finish_session, (other, active["id"])),
    ):
        with pytest.raises(PermissionError):
            method(*args)
    assert service.get_session(owner, active["id"])["answered"] == 0
    assert service.history(other) == []


def test_session_keeps_question_and_verification_snapshot(service, tmp_path):
    import_questions(service, tmp_path, question())
    user_id = create_user(service)
    session = service.start_session(user_id, "anatomy")
    before = service.current_question(user_id, session["id"])
    import_questions(service, tmp_path, question(
        text="New text", options={"A": "New option A", "B": "New option B"},
        answer_status="OFFICIAL", correct_option="B"))
    assert service.current_question(user_id, session["id"]) == before
    result = service.answer(user_id, session["id"], "q1", 1)
    assert result["feedback"]["verified"] is False
    assert service.summary(user_id, session["id"])["percent"] is None
    new_session = service.start_session(user_id, "anatomy")
    assert service.current_question(user_id, new_session["id"])["text"] == "New text"
    assert service.answer(user_id, new_session["id"], "q1", 1)["feedback"]["correct"] is True


def test_resume_history_and_favorites_survive_service_recreation(service, tmp_path):
    import_questions(service, tmp_path, question("q1"), question("q2"))
    user_id = create_user(service)
    other = create_user(service, 200)
    session = service.start_session(user_id, "anatomy")
    current = service.current_question(user_id, session["id"])
    service.answer(user_id, session["id"], current["id"], 0)
    assert service.toggle_favorite(user_id, "q1") is True
    restarted = BotService(service.session_factory)
    assert restarted.resume_session(user_id)["answered"] == 1
    assert [q["id"] for q in restarted.favorites(user_id)] == ["q1"]
    assert restarted.favorites(other) == []
    summary = restarted.finish_session(user_id, session["id"])
    assert summary["answered"] == 1
    assert summary["total"] == 2
    assert restarted.history(user_id)[0]["id"] == session["id"]
    assert restarted.toggle_favorite(user_id, "q1") is False
    assert restarted.favorites(user_id) == []


def test_simultaneous_duplicate_callbacks_accept_exactly_once(service, tmp_path):
    import_questions(service, tmp_path, question())
    user_id = create_user(service)
    session = service.start_session(user_id, "anatomy")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.answer(user_id, session["id"], "q1", 0), range(16)))
    assert sum(result["accepted"] for result in results) == 1
    assert service.summary(user_id, session["id"])["answered"] == 1
    with service.session_factory() as db:
        assert len(list(db.scalars(select(Answer)))) == 1


def test_create_service_honors_environment_database_url(monkeypatch, tmp_path):
    path = tmp_path / "configured.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    service = create_service()
    user = service.upsert_user(1234, "ru")
    restored = create_service().upsert_user(1234)
    assert path.exists()
    assert restored["id"] == user["id"]
    assert restored["language"] == "ru"
