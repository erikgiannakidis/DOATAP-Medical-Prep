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


if __name__ == "__main__":
    main()
