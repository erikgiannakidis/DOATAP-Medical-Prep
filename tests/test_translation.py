from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from doatap.translation import (
    MAX_TRANSLATION_CHARACTERS,
    question_translation_text,
    translation_links,
    translation_url,
)


def test_translation_contains_complete_question_and_choices_but_no_answer_metadata():
    question = {
        "id": "private-question-id",
        "text": "Ποια είναι η σωστή επιλογή;\nNa+ & K+ < 5% # ?",
        "options": ["Άλφα + βήτα", "Γάμμα & δέλτα", "🫀 Καρδιά", "A=B", "Κανένα"],
        "original_text": "Raw duplicate options must not be translated",
        "correct_option": 3,
        "selected_option": 1,
        "user_id": "private-user-id",
        "answer_status": "OFFICIAL",
        "source_document": "private-source.pdf",
    }
    original = deepcopy(question)
    expected = ("Ποια είναι η σωστή επιλογή;\nNa+ & K+ < 5% # ?\n\n"
                "A. Άλφα + βήτα\nB. Γάμμα & δέλτα\nC. 🫀 Καρδιά\nD. A=B\nE. Κανένα")
    links = translation_links(question)
    assert set(links) == {"ru", "en"}
    for language, url in links.items():
        parsed = urlsplit(url)
        assert (parsed.scheme, parsed.netloc, parsed.path, parsed.fragment) == (
            "https", "translate.google.com", "/", "",
        )
        assert parse_qs(parsed.query) == {
            "sl": ["el"], "tl": [language], "text": [expected], "op": ["translate"],
        }
        assert url.isascii()
    assert question == original


def test_long_greek_question_is_not_truncated_to_callback_or_message_limits():
    # A 1,000-character Greek stem becomes a URL of more than 6,000 bytes.
    # URL buttons do not use Telegram's 64-byte callback_data field.
    question = {"text": "Ἄλφα " * 200, "options": ["Πρώτη", "Τελευταία & # ?"]}
    expected = question_translation_text(question)
    for url in translation_links(question).values():
        assert len(url.encode("ascii")) > 4096
        assert parse_qs(urlsplit(url).query)["text"] == [expected]


def test_google_text_limit_is_explicit_and_never_silently_truncates():
    boundary = "α" * MAX_TRANSLATION_CHARACTERS
    assert parse_qs(urlsplit(translation_url(boundary, "ru")).query)["text"] == [boundary]
    with pytest.raises(ValueError, match="text limit"):
        translation_url(boundary + "β", "ru")


@pytest.mark.parametrize("language", ["el", "de", "", "ru&sl=en"])
def test_unknown_translation_languages_are_rejected(language):
    with pytest.raises(ValueError, match="language"):
        translation_url("Ερώτηση", language)


@pytest.mark.parametrize("question", [
    {"text": "", "options": ["Άλφα"]},
    {"text": "Ερώτηση", "options": "Άλφα"},
    {"text": "Ερώτηση", "options": {"A": "Άλφα"}},
    {"text": "Ερώτηση", "options": [None]},
    {"text": "Ερώτηση", "options": [" "]},
    {"text": "Ερώτηση", "options": []},
])
def test_malformed_question_does_not_create_misleading_translation(question):
    with pytest.raises(ValueError):
        translation_links(question)
