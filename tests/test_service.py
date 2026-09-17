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


def answer_remaining(service, user_id, session_id, option=0):
    answered = []
    while current := service.current_question(user_id, session_id):
        result = service.answer(user_id, session_id, current["id"], option)
        assert result["accepted"] is True
        answered.append(current)
    return answered


def test_study_covers_entire_ready_bank_once_including_final_partial_batch(service, tmp_path):
    # IDs deliberately run opposite to page order.
    records = [question(f"q{52 - i:02}", source_page=i + 1,
                        discipline="anatomy" if i < 30 else "physiology") for i in range(53)]
    import_questions(service, tmp_path, *records, question("quarantined", status="NEEDS_REVIEW"))
    user_id = create_user(service)
    covered, sizes = [], []
    while session := service.start_study_session(user_id):
        assert session["discipline"] == "ALL"
        sizes.append(session["total"])
        covered.extend(answer_remaining(service, user_id, session["id"]))
    assert sizes == [25, 25, 3]
    assert len({q["id"] for q in covered}) == 53
    assert [q["source_page"] for q in covered] == list(range(1, 54))
    progress = service.progress(user_id)
    assert progress["total"] == progress["answered_unique"] == progress["total_attempts"] == 53
    assert progress["remaining"] == progress["latest_correct"] == progress["latest_wrong"] == 0
    assert progress["latest_unverified"] == 53
    assert {row["discipline"]: row["answered_unique"] for row in progress["disciplines"]} == {
        "anatomy": 30, "physiology": 23}


def test_assignment_is_not_coverage_and_abandoned_answers_still_count(service, tmp_path):
    import_questions(service, tmp_path, *(question(f"q{i}", source_page=i) for i in range(1, 4)))
    user_id = create_user(service)
    first = service.start_study_session(user_id, "ALL")
    assert service.progress(user_id)["answered_unique"] == 0
    assert service.progress(user_id)["remaining"] == 3
    service.answer(user_id, first["id"], "q1", 0)
    assert service.progress(user_id)["answered_unique"] == 1  # still ACTIVE
    second = service.start_study_session(user_id, "anatomy")
    assert service.get_session(user_id, first["id"])["status"] == "ABANDONED"
    assert service.progress(user_id)["answered_unique"] == 1
    assert second["total"] == 2
    assert service.current_question(user_id, second["id"])["id"] == "q2"
    assert service.question_history(user_id)["items"][0]["question_id"] == "q1"
    assert service.answer(user_id, first["id"], "q2", 0)["accepted"] is False
    assert [q["id"] for q in answer_remaining(service, user_id, second["id"])] == ["q2", "q3"]


def test_repeats_keep_unique_coverage_and_use_latest_outcome(service, tmp_path):
    import_questions(service, tmp_path,
                     question("official", answer_status="OFFICIAL", correct_option="A"),
                     question("reviewed", answer_status="MEDICAL_REVIEWED", correct_option="A"),
                     question("unknown"))
    user_id = create_user(service)
    first = service.start_study_session(user_id)
    answer_remaining(service, user_id, first["id"], option=0)
    assert service.progress(user_id)["latest_correct"] == 2
    repeated = service.start_session(user_id, "anatomy")
    answer_remaining(service, user_id, repeated["id"], option=1)
    progress = service.progress(user_id)
    assert progress["total"] == progress["answered_unique"] == 3
    assert progress["total_attempts"] == 6
    assert progress["remaining"] == progress["latest_correct"] == 0
    assert progress["latest_wrong"] == 2
    assert progress["latest_unverified"] == 1
    assert all(row["attempt_count"] == 2 for row in service.question_history(user_id)["items"])
    attempts = service.question_attempts(user_id, "official")
    assert attempts["total"] == 2
    assert [(item["selected_option"], item["correct"]) for item in attempts["items"]] == [(1, False), (0, True)]
    assert all(item["correct_option"] == 0 and item["answer_status"] == "OFFICIAL"
               and item["answered_at"] for item in attempts["items"])
    unknown = service.question_attempts(user_id, "unknown")["items"]
    assert all(item["correct"] is None and item["correct_option"] is None for item in unknown)


def test_coverage_and_question_histories_are_personal(service, tmp_path):
    import_questions(service, tmp_path, question("q1", source_page=1), question("q2", source_page=2))
    owner, other = create_user(service), create_user(service, 200)
    first = service.start_study_session(owner)
    service.answer(owner, first["id"], "q1", 0)
    assert service.progress(owner)["answered_unique"] == 1
    assert service.progress(other)["answered_unique"] == service.progress(other)["total_attempts"] == 0
    assert service.question_history(other)["items"] == []
    assert service.question_attempts(other, "q1")["items"] == []
    second = service.start_study_session(other)
    assert second["total"] == 2
    assert service.current_question(other, second["id"])["id"] == "q1"


def test_exhausted_study_does_not_close_existing_practice_session(service, tmp_path):
    import_questions(service, tmp_path, question())
    user_id = create_user(service)
    session = service.start_study_session(user_id)
    answer_remaining(service, user_id, session["id"])
    active = service.start_session(user_id, "anatomy")
    assert service.start_study_session(user_id, "anatomy") is None
    assert service.start_study_session(user_id, "ALL") is None
    assert service.resume_session(user_id)["id"] == active["id"]
    assert service.get_session(user_id, active["id"])["status"] == "ACTIVE"


def test_fresh_import_updates_ready_coverage_and_preserves_old_snapshot_history(service, tmp_path):
    original = question("old", text="Historical wording", source_page=1)
    import_questions(service, tmp_path, original)
    user_id = create_user(service)
    session = service.start_study_session(user_id)
    answer_remaining(service, user_id, session["id"])
    saved_history = service.question_attempts(user_id, "old")
    # A reviewed extraction can remove an old question from practice without
    # rewriting the answer, its original wording, or its session summary.
    import_questions(service, tmp_path,
                     question("old", text="Revised wording", status="NEEDS_REVIEW"),
                     question("new", source_page=2))
    progress = service.progress(user_id)
    assert progress["total"] == progress["remaining"] == 1
    assert progress["answered_unique"] == progress["total_attempts"] == 0
    assert service.question_attempts(user_id, "old") == saved_history
    assert service.question_history(user_id)["total"] == 1
    assert service.history(user_id)[0]["answered"] == 1
    new_session = service.start_study_session(user_id)
    assert service.current_question(user_id, new_session["id"])["id"] == "new"
    answer_remaining(service, user_id, new_session["id"])
    assert service.progress(user_id)["remaining"] == 0


def test_question_history_paginates_and_filters_snapshot_discipline(service, tmp_path):
    import_questions(service, tmp_path, *(question(f"q{i}", source_page=i,
                     discipline="anatomy" if i % 2 else "physiology") for i in range(7)))
    user_id = create_user(service)
    session = service.start_study_session(user_id)
    answer_remaining(service, user_id, session["id"])
    pages = [service.question_history(user_id, page=i, page_size=3) for i in range(3)]
    assert [page["total"] for page in pages] == [7, 7, 7]
    assert [len(page["items"]) for page in pages] == [3, 3, 1]
    assert [item["question_id"] for page in pages for item in page["items"]] == [f"q{i}" for i in range(6, -1, -1)]
    assert service.question_history(user_id, "anatomy")["total"] == 3
    assert service.question_history(user_id, "physiology")["total"] == 4
    assert service.question_history(user_id, page=99, page_size=3)["page"] == 2


def test_attempt_and_session_history_pagination_keeps_legacy_history(service, tmp_path):
    import_questions(service, tmp_path, question())
    user_id = create_user(service)
    session_ids = []
    for option in [0, 1, 0, 1, 0]:
        session = service.start_session(user_id, "anatomy")
        session_ids.append(session["id"])
        answer_remaining(service, user_id, session["id"], option)
    first = service.question_attempts(user_id, "q1", page=0, page_size=2)
    second = service.question_attempts(user_id, "q1", page=1, page_size=2)
    third = service.question_attempts(user_id, "q1", page=2, page_size=2)
    assert [len(page["items"]) for page in (first, second, third)] == [2, 2, 1]
    assert all(page["total"] == 5 and page["pages"] == 3 for page in (first, second, third))
    assert [item["session_id"] for page in (first, second, third) for item in page["items"]] == session_ids[::-1]
    history = service.session_history(user_id, page_size=2)
    assert history["items"] == service.history(user_id, limit=2)
    assert service.session_history(user_id, page=2, page_size=2)["items"][0]["id"] == session_ids[0]
    assert service.session_history(create_user(service, 200))["items"] == []


def test_study_filters_discipline_and_empty_progress_is_defined(service, tmp_path):
    user_id = create_user(service)
    assert service.progress(user_id) == {
        "total": 0, "answered_unique": 0, "remaining": 0, "latest_correct": 0,
        "latest_wrong": 0, "latest_unverified": 0, "total_attempts": 0, "disciplines": []}
    assert service.start_study_session(user_id) is None
    import_questions(service, tmp_path, question("a", discipline="anatomy"),
                     question("p", discipline="physiology"))
    session = service.start_study_session(user_id, "physiology")
    assert session["discipline"] == "physiology" and session["total"] == 1
    assert service.current_question(user_id, session["id"])["id"] == "p"
    for count in (0, -1, 51, True, "25"):
        with pytest.raises(ValueError):
            service.start_study_session(user_id, count=count)
    for method in (service.question_history, service.session_history):
        with pytest.raises(ValueError):
            method(user_id, page=-1)
    with pytest.raises(ValueError):
        service.question_attempts(user_id, "p", page_size=0)
    with pytest.raises(LookupError):
        service.progress(999999)
