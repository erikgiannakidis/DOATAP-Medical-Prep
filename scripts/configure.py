"""Create a local .env through a hidden prompt; never print the token."""

from getpass import getpass
import os
from pathlib import Path
import re
import secrets


def main():
    target = Path(".env")
    if target.exists():
        raise SystemExit(".env already exists. Edit it locally; this script will not overwrite it.")
    token = getpass("BotFather token (hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise SystemExit("Invalid token format; no file was written.")
    password = secrets.token_hex(24)
    body = (f"BOT_TOKEN={token}\nPOSTGRES_PASSWORD={password}\n"
            f"DATABASE_URL=postgresql+psycopg://doatap:{password}@db:5432/doatap\n"
            "QUESTION_BANK_PATH=data/questions.jsonl\nALLOWED_USER_IDS=\n")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    print("Created .env with mode 0600. The token was not displayed.")


if __name__ == "__main__":
    main()
