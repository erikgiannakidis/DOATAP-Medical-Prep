"""Exercise the full imported bank in a disposable DB, without Telegram access."""

import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doatap.bot import button, question_text, result_text, split_message
from doatap.service import create_service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bank", nargs="?", type=Path, default=Path("data/questions.jsonl"))
    parser.add_argument("--full-coverage", action="store_true",
                        help="Also answer every ready question through successive study batches")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="doatap-smoke-") as directory:
        service = create_service(f"sqlite:///{Path(directory) / 'smoke.db'}")
        imported = service.import_bank(args.bank)
        user = service.upsert_user(999999001, "ru")
        sessions = answered = 0
        for discipline in service.disciplines():
            for count in (10, 25, 50):
                session = service.start_session(user["id"], discipline["discipline"], count)
                assert session["total"] == count, "Not enough questions for the advertised test size"
                seen = set()
                for _ in range(count):
                    question = service.current_question(user["id"], session["id"])
                    assert question and question["id"] not in seen
                    seen.add(question["id"])
                    for language in ("ru", "el", "en"):
                        rendered = question_text(question, language)
                        assert question["text"] in rendered
                        assert "".join(split_message(rendered)) == rendered
                    button("A", "answer", session["id"], question["id"], 0)
                    result = service.answer(user["id"], session["id"], question["id"], 0)
                    assert result["accepted"]
                    answered += 1
                summary = service.summary(user["id"], session["id"])
                assert summary["answered"] == count and summary["status"] == "COMPLETED"
                assert summary["unverified"] + summary["scored_total"] == count
                if not summary["scored_total"]:
                    assert summary["percent"] is None
                for language in ("ru", "el", "en"):
                    assert result_text(summary, language)
                sessions += 1
        print(f"Imported {imported['inserted']} records; completed {sessions} sessions / {answered} answers in 3 UI languages.")
        if args.full_coverage:
            learner = service.upsert_user(999999002, "ru")
            uid = learner["id"]
            total = sum(item["count"] for item in service.disciplines())
            seen, batches = set(), 0
            while session := service.start_study_session(uid):
                assert 1 <= session["total"] <= 25
                while question := service.current_question(uid, session["id"]):
                    assert question["id"] not in seen, "Study mode repeated an answered question"
                    seen.add(question["id"])
                    result = service.answer(uid, session["id"], question["id"], 0)
                    assert result["accepted"]
                batches += 1
            progress = service.progress(uid)
            assert len(seen) == progress["answered_unique"] == total
            assert progress["remaining"] == 0 and progress["total_attempts"] == total
            assert sum(progress[key] for key in ("latest_correct", "latest_wrong", "latest_unverified")) == total
            history, page = set(), 0
            while True:
                entries = service.question_history(uid, page=page, page_size=100)
                history.update(item["question_id"] for item in entries["items"])
                if page + 1 >= entries["pages"]:
                    break
                page += 1
            assert history == seen, "Some answered questions are missing from personal history"
            newcomer = service.upsert_user(999999003, "el")
            assert service.progress(newcomer["id"])["remaining"] == total
            assert service.question_history(newcomer["id"])["total"] == 0
            print(f"Full coverage: {total} unique questions in {batches} batches; complete paginated history; separate user untouched.")


if __name__ == "__main__":
    main()
