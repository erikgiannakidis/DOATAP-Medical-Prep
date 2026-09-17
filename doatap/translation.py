"""Build user-opened translation links without calling an external service."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

TRANSLATION_LANGUAGES = ("ru", "en")
TRANSLATE_BASE_URL = "https://translate.google.com/"
# Google Translate's documented limit for text pasted into its web interface.
# Refuse oversized content rather than silently dropping question text or choices.
MAX_TRANSLATION_CHARACTERS = 5000


def question_translation_text(question: Mapping[str, Any]) -> str:
    """Use only the displayed Greek stem and ordered choices from a snapshot.

    A snapshot can also contain the key, a user's selected answer, source metadata
    and identifiers. None of those belong in the text sent to the translator.
    ``original_text`` already includes choices, so it must not be appended.
    """
    stem = question["text"]
    options = question["options"]
    if not isinstance(stem, str) or not stem.strip():
        raise ValueError("Translation requires a non-empty question")
    if (not isinstance(options, Sequence) or isinstance(options, (str, bytes))
            or not 1 <= len(options) <= 26
            or any(not isinstance(option, str) or not option.strip() for option in options)):
        raise ValueError("Translation requires ordered, non-empty answer choices")
    return stem + "\n\n" + "\n".join(
        f"{chr(65 + index)}. {option}" for index, option in enumerate(options)
    )


def translation_url(text: str, target_language: str) -> str:
    """Return a complete, correctly escaped Google Translate web link.

    This does not make a request. The browser opens the external service only
    when the learner presses the URL button. Greek remains the source language.
    """
    if target_language not in TRANSLATION_LANGUAGES:
        raise ValueError("Unsupported translation language")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Translation text is empty")
    if len(text) > MAX_TRANSLATION_CHARACTERS:
        raise ValueError("Question exceeds Google Translate's text limit")
    return TRANSLATE_BASE_URL + "?" + urlencode({
        "sl": "el", "tl": target_language, "text": text, "op": "translate",
    })


def translation_links(question: Mapping[str, Any]) -> dict[str, str]:
    """Return Russian and English links for one complete question snapshot."""
    text = question_translation_text(question)
    return {language: translation_url(text, language) for language in TRANSLATION_LANGUAGES}
