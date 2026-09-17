"""Startup settings without printing credentials."""

from dataclasses import dataclass
import os
from pathlib import Path
import re

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    bank_path: Path
    token: str | None
    allowed_user_ids: frozenset[int]

    @classmethod
    def from_env(cls, *, require_token: bool = True):
        load_dotenv(override=False)
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("Set DATABASE_URL in the environment or local .env file.")
        token = os.getenv("BOT_TOKEN", "").strip() or None
        if require_token and (not token or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token)):
            raise ValueError("Set a valid BOT_TOKEN in the environment or local .env file.")
        try:
            allowed = frozenset(int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
        except ValueError:
            raise ValueError("ALLOWED_USER_IDS must be comma-separated numeric Telegram IDs.") from None
        if any(x <= 0 for x in allowed):
            raise ValueError("ALLOWED_USER_IDS must contain positive Telegram IDs.")
        return cls(database_url, Path(os.getenv("QUESTION_BANK_PATH", "data/questions.jsonl")), token, allowed)
