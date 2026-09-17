"""Run the bot, or check/import the question bank without contacting Telegram."""

import argparse
import logging
import sys

from doatap.config import Settings


def main():
    parser = argparse.ArgumentParser(description="DOATAP Medical Prep")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check", "import"])
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP client URLs include the Telegram token.
    for name in ("httpx", "httpcore", "telegram", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.from_env(require_token=args.command == "run")
        from doatap.service import create_service
        service = create_service(settings.database_url)
        if not settings.bank_path.is_file():
            raise ValueError("Question bank is missing. Run scripts/parse_question_bank.py first; see README.")
        imported = service.import_bank(settings.bank_path)
        available = service.disciplines()
        if not available or not any(d["count"] >= 10 for d in available):
            raise ValueError("Question bank does not contain enough READY questions for a 10-question test.")
        print(f"Bank import: {imported}")
        for entry in available:
            print(f"{entry['discipline']}: {entry['count']} available questions")
        if args.command != "run":
            print("Database and bank are ready. Telegram was not contacted.")
            return 0
        from doatap.bot import build_application
        application = build_application(settings.token, service, allowed_user_ids=set(settings.allowed_user_ids) or None)
        application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)
        return 0
    except (ValueError, OSError) as exc:
        # Our configuration messages contain field names, never field values.
        if isinstance(exc, ValueError) and exc.__class__ is ValueError:
            print(f"Startup failed: {exc}", file=sys.stderr)
        else:
            print(f"Startup failed ({type(exc).__name__}). Check configuration and access.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Connection errors may contain credentials; emit only the error class.
        print(f"Startup failed ({type(exc).__name__}). Check database, network and bot settings.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
