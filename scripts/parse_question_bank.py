#!/usr/bin/env python3
"""Extract the supplied DOATAP PDF without inventing text or an answer key.

READY means structurally usable, never medically verified. ``original_text``
contains the full extracted question block, including option markers and line
breaks; ``text`` is the whitespace-normalized question stem. The source PDF is
authoritative when its text layer contains typos or broken typography.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
import unicodedata


OPTION = re.compile(r"^\s*([A-E])\)\s*(.*)$")
HEADINGS = {
    "ΜΑΘΗΜΑ: ΑΝΑΤΟΜΙΑ": "ANATOMY",
    "ΜΑΘΗΜΑ: ΦΥΣΙΟΛΟΓΙΑ": "PHYSIOLOGY",
    "ΜΑΘΗΜΑ: ΦΑΡΜΑΚΟΛΟΓΙΑ": "PHARMACOLOGY",
}
EXPECTED_OPTIONS = list("ABCDE")
DEFAULT_PDF = Path("materials/ΠΡΟΚΛΙΝΙΚΕΣ-ΕΠΙΣΤΗΜΕΣ(1).pdf")


@dataclass(frozen=True)
class SourceLine:
    page: int
    number: int
    text: str


def normalize(text: str) -> str:
    """Only collapse whitespace; retain spelling, punctuation, and symbols."""
    return " ".join(text.split())


def has_visual_reference(text: str) -> bool:
    """Quarantine diagram/table dependencies without confusing clinical signs.

    Greek 'κλινική εικόνα' (clinical presentation) and 'θεραπευτικό σχήμα'
    (treatment regimen) do not by themselves refer to an omitted illustration.
    """
    plain = "".join(char for char in unicodedata.normalize("NFD", text.casefold())
                    if not unicodedata.combining(char))
    return bool(re.search(
        r"\b(?:εικονιζ|απεικονιζ)\w*|\bπινακ\w*|\bγραφημα\b|"
        r"\bδιαγραμμα(?:τοσ|τα)?\b|\b(?:στο|του) σχημα(?:τοσ)?\b|"
        r"\b(?:παρακατω|ακολουθη|ακολουθο|παραπανω) (?:εικονα|σχημα)\b",
        plain,
    ))


def paragraphs(pages: Iterable[str]) -> list[list[SourceLine]]:
    """Preserve paragraphs across page breaks and their physical PDF pages.

    The source has empty top-margin lines on every page. Those are not question
    separators: options and stems regularly continue onto the following page.
    Actual trailing/interior empty lines are preserved as paragraph separators.
    """
    result: list[list[SourceLine]] = []
    current: list[SourceLine] = []
    for page, text in enumerate(pages, 1):
        content_started = False
        for number, line in enumerate(text.splitlines(), 1):
            if not content_started and not line.strip():
                continue
            content_started = True
            if not line.strip():
                if current:
                    result.append(current)
                    current = []
            else:
                current.append(SourceLine(page, number, line))
    if current:
        result.append(current)
    return result


def split_page_joined_questions(
    lines: list[SourceLine],
) -> tuple[list[list[SourceLine]], list[str]]:
    """Separate complete option groups joined only by a PDF page boundary.

    There is no semantic guessing: a new A marker, an earlier E marker, and
    exactly one page boundary between E and the next A are all required.
    Ambiguous blocks remain quarantined rather than silently dropping text.
    """
    markers = [(i, match.group(1)) for i, line in enumerate(lines)
               if (match := OPTION.match(line.text))]
    starts = [i for i, key in markers if key == "A"]
    cuts = []
    for start in starts[1:]:
        prior = [(i, key) for i, key in markers if i < start]
        if not prior or prior[-1][1] != "E":
            return [lines], ["AMBIGUOUS_QUESTION_BOUNDARY"]
        previous_end = prior[-1][0]
        candidates = [i for i in range(previous_end + 1, start)
                      if lines[i].page != lines[i - 1].page]
        if len(candidates) != 1:
            return [lines], ["AMBIGUOUS_QUESTION_BOUNDARY"]
        cuts.append(candidates[0])
    limits = [0, *cuts, len(lines)]
    return [lines[left:right] for left, right in zip(limits, limits[1:])], []


def parse_pages(
    pages: Iterable[str],
    source_document: str,
    source_year: int = 2025,
    image_pages: Iterable[int] = (),
) -> tuple[list[dict], dict]:
    """Return all extracted records plus a reproducible quality report."""
    pages = list(pages)
    images = set(image_pages)
    questions = []
    reviews = []
    duplicates = []
    headings = []
    ignored = []
    seen = {}
    discipline = None
    page_splits = 0
    cross_page_records = 0
    for paragraph in paragraphs(pages):
        heading = normalize("\n".join(line.text for line in paragraph))
        if heading in HEADINGS:
            discipline = HEADINGS[heading]
            headings.append({"discipline": discipline,
                             "source_page": paragraph[0].page})
            continue
        if heading == "ΕΝΟΤΗΤΑ «ΠΡΟΚΛΙΝΙΚΕΣ ΕΠΙΣΤΗΜΕΣ»":
            continue
        if discipline is None:
            raise ValueError("Question content precedes a recognized discipline heading")
        if not any(OPTION.match(line.text) for line in paragraph):
            ignored.append({"source_page": paragraph[0].page, "text": heading})
            continue
        pieces, boundary_issues = split_page_joined_questions(paragraph)
        page_splits += len(pieces) - 1
        for lines in pieces:
            stem = []
            options: dict[str, list[str]] = {}
            sequence = []
            current = None
            issues = list(boundary_issues)
            for line in lines:
                match = OPTION.match(line.text)
                if match:
                    current = match.group(1)
                    sequence.append(current)
                    options.setdefault(current, []).append(match.group(2))
                elif current is None:
                    stem.append(line.text)
                else:
                    options[current].append(line.text)
            text = normalize("\n".join(stem))
            normalized_options = {
                key: normalize("\n".join(value)) for key, value in options.items()
            }
            original_text = "\n".join(line.text for line in lines)
            if sequence != EXPECTED_OPTIONS:
                issues.append("OPTION_SEQUENCE_" + "".join(sequence))
            if not text:
                issues.append("MISSING_QUESTION_STEM")
            if not any("\u0370" <= ch <= "\u03ff" or "\u1f00" <= ch <= "\u1fff"
                       for ch in text):
                issues.append("NO_GREEK_IN_STEM")
            if any(not value for value in normalized_options.values()):
                issues.append("EMPTY_OPTION")
            if "\ufffd" in original_text or "\x00" in original_text:
                issues.append("INVALID_TEXT_CHARACTER")
            if has_visual_reference(text):
                issues.append("VISUAL_REFERENCE_REQUIRES_REVIEW")
            involved_pages = sorted({line.page for line in lines})
            if images.intersection(involved_pages):
                issues.append("SOURCE_PAGE_HAS_IMAGE")
            if len(involved_pages) > 1:
                cross_page_records += 1
            identity = json.dumps(
                [source_document, source_year, discipline, lines[0].page,
                 lines[0].number, original_text], ensure_ascii=False,
                separators=(",", ":"),
            )
            question_id = "a25_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
            fingerprint = json.dumps([discipline, text, normalized_options],
                                     ensure_ascii=False, sort_keys=True)
            if fingerprint in seen:
                issues.append("EXACT_DUPLICATE")
                duplicates.append({"id": question_id, "duplicate_of": seen[fingerprint],
                                   "source_page": lines[0].page})
            else:
                seen[fingerprint] = question_id
            questions.append({
                "id": question_id,
                "discipline": discipline,
                "text": text,
                "original_text": original_text,
                "options": normalized_options,
                "source_page": lines[0].page,
                "source_year": source_year,
                "source_document": source_document,
                "status": "NEEDS_REVIEW" if issues else "READY",
                "answer_status": "UNVERIFIED",
                "correct_option": None,
            })
            if issues:
                reviews.append({"id": question_id, "source_page": lines[0].page,
                                "discipline": discipline, "reasons": issues})
    if ignored:
        # A changed PDF layout must not silently omit any unexplained paragraphs.
        raise ValueError(f"Unparsed non-header paragraphs: {ignored!r}")
    ids = [question["id"] for question in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("Question ID collision")
    report = {
        "source_document": source_document,
        "source_year": source_year,
        "page_count": len(pages),
        "total_questions": len(questions),
        "status_counts": dict(Counter(question["status"] for question in questions)),
        "discipline_counts": {
            name: dict(Counter(question["status"] for question in questions
                               if question["discipline"] == name))
            for name in HEADINGS.values()
        },
        "answer_status_counts": {"UNVERIFIED": len(questions)},
        "option_count_distribution": dict(Counter(str(len(question["options"]))
                                                   for question in questions)),
        "source_option_marker_counts": dict(Counter(
            match.group(1) for page in pages for line in page.splitlines()
            if (match := OPTION.match(line)))),
        "discipline_headings": headings,
        "page_boundary_question_splits": page_splits,
        "questions_spanning_pages": cross_page_records,
        "source_pages_with_images": sorted(images),
        "review_reason_counts": dict(Counter(reason for review in reviews
                                              for reason in review["reasons"])),
        "review_records": reviews,
        "exact_duplicates": duplicates,
        "normalization": "Whitespace collapse only; no spelling, meaning, or answer corrections.",
        "original_text_definition": "Full extracted question block including options and line breaks.",
        "limitations": [
            "READY certifies structural checks only, not medical accuracy or a correct answer.",
            "No official answer key was supplied; every correct_option is null.",
            "The source year 2025 is supplied by the user, not established from PDF metadata.",
            "Source text-layer typos, mixed Latin/Greek letters, and broken words remain unchanged.",
            "Four-option records are quarantined conservatively; no missing fifth answer was invented.",
            "Later exact duplicates are retained for provenance but excluded from tests.",
            "Questions referring to illustrations, diagrams, or tables are quarantined until reviewed.",
            "Visual sample review does not constitute a complete manual audit of 896 pages.",
        ],
    }
    return questions, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, nargs="?", default=DEFAULT_PDF)
    parser.add_argument("--output", type=Path, default=Path("data/questions.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("data/import_report.json"))
    args = parser.parse_args()
    from pypdf import PdfReader, __version__ as pypdf_version

    reader = PdfReader(args.pdf)
    pages = [page.extract_text() or "" for page in reader.pages]
    image_pages = [number for number, page in enumerate(reader.pages, 1) if len(page.images)]
    questions, report = parse_pages(pages, args.pdf.name, image_pages=image_pages)
    report["source_sha256"] = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    report["extractor"] = {"name": "pypdf", "version": pypdf_version}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n"
                                   for q in questions), encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                      ("total_questions", "status_counts", "discipline_counts")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
