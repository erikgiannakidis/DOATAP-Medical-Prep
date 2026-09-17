"""Structural safeguards for source extraction and unverified answer handling."""

import json
from pathlib import Path
import unittest

from scripts.parse_question_bank import parse_pages


HEADER = "ΜΑΘΗΜΑ: ΑΝΑΤΟΜΙΑ\n\n"
QUESTION = "Ποια πρόταση ισχύει;\nA) ένα\nB) δύο\nC) τρία\nD) τέσσερα\nE) πέντε\n"


class ParserTests(unittest.TestCase):
    def test_options_continue_across_page_without_creating_new_question(self):
        questions, report = parse_pages([
            HEADER + "Ποια πρόταση ισχύει;\nA) μεγάλο\n",
            "\n \nκείμενο\nB) δύο\nC) τρία\nD) τέσσερα\nE) πέντε\n",
        ], "source.pdf")
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["options"]["A"], "μεγάλο κείμενο")
        self.assertEqual(questions[0]["source_page"], 1)
        self.assertEqual(questions[0]["status"], "READY")
        self.assertEqual(report["questions_spanning_pages"], 1)

    def test_question_at_next_page_does_not_get_appended_to_previous_option(self):
        questions, report = parse_pages([
            HEADER + QUESTION,
            "\n\n" + QUESTION.replace("Ποια πρόταση", "Ποια άλλη πρόταση"),
        ], "source.pdf")
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0]["options"]["E"], "πέντε")
        self.assertEqual(questions[1]["text"], "Ποια άλλη πρόταση ισχύει;")
        self.assertEqual(questions[1]["source_page"], 2)
        self.assertEqual(report["page_boundary_question_splits"], 1)

    def test_transition_page_keeps_preceding_anatomy_question_in_anatomy(self):
        questions, _ = parse_pages([
            HEADER + QUESTION.replace("D) τέσσερα\nE) πέντε\n", ""),
            "\nD) τέσσερα\nE) πέντε\n\nΜΑΘΗΜΑ: ΦΥΣΙΟΛΟΓΙΑ\n\n" + QUESTION,
        ], "source.pdf")
        self.assertEqual([q["discipline"] for q in questions], ["ANATOMY", "PHYSIOLOGY"])

    def test_missing_option_is_quarantined_without_invention(self):
        questions, _ = parse_pages([HEADER + QUESTION.replace("E) πέντε\n", "")], "source.pdf")
        self.assertEqual(questions[0]["status"], "NEEDS_REVIEW")
        self.assertEqual(list(questions[0]["options"]), list("ABCD"))
        self.assertIsNone(questions[0]["correct_option"])
        self.assertEqual(questions[0]["answer_status"], "UNVERIFIED")

    def test_exact_duplicates_are_retained_but_later_copy_is_quarantined(self):
        questions, report = parse_pages([HEADER + QUESTION + "\n" + QUESTION], "source.pdf")
        self.assertEqual([q["status"] for q in questions], ["READY", "NEEDS_REVIEW"])
        self.assertNotEqual(questions[0]["id"], questions[1]["id"])
        self.assertEqual(report["exact_duplicates"][0]["duplicate_of"], questions[0]["id"])

    def test_unexplained_source_text_fails_instead_of_being_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "Unparsed non-header"):
            parse_pages([HEADER + "Χαμένο κείμενο\n\n" + QUESTION], "source.pdf")

    def test_ambiguous_boundary_does_not_become_ready(self):
        questions, report = parse_pages([HEADER + QUESTION + QUESTION], "source.pdf")
        self.assertTrue(all(q["status"] == "NEEDS_REVIEW" for q in questions))
        self.assertIn("AMBIGUOUS_QUESTION_BOUNDARY", report["review_reason_counts"])

    def test_short_stable_ascii_ids_and_original_text(self):
        first, _ = parse_pages([HEADER + QUESTION], "source.pdf")
        second, _ = parse_pages([HEADER + QUESTION], "source.pdf")
        self.assertEqual(first, second)
        self.assertTrue(first[0]["id"].isascii())
        self.assertLessEqual(len(first[0]["id"]), 24)
        self.assertEqual(first[0]["original_text"], QUESTION.rstrip("\n"))

    def test_source_images_require_review(self):
        questions, _ = parse_pages([HEADER + QUESTION], "source.pdf", image_pages=[1])
        self.assertEqual(questions[0]["status"], "NEEDS_REVIEW")

    def test_missing_diagram_reference_is_quarantined_without_embedded_image(self):
        questions, report = parse_pages([
            HEADER + QUESTION.replace("Ποια πρόταση ισχύει;", "Η εικονιζόμενη δράση στο σχήμα οφείλεται σε;")
        ], "source.pdf")
        self.assertEqual(questions[0]["status"], "NEEDS_REVIEW")
        self.assertIn("VISUAL_REFERENCE_REQUIRES_REVIEW", report["review_reason_counts"])

    def test_clinical_presentation_does_not_require_a_picture(self):
        questions, _ = parse_pages([
            HEADER + QUESTION.replace("Ποια πρόταση ισχύει;", "Ποια είναι η κλινική εικόνα του ασθενούς;")
        ], "source.pdf")
        self.assertEqual(questions[0]["status"], "READY")

    def test_imported_dataset_retains_quarantine_and_has_no_answer_key(self):
        path = Path(__file__).resolve().parents[1] / "data/questions.jsonl"
        if not path.exists():
            self.skipTest("Run the PDF importer first")
        questions = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(len(questions), 3395)
        self.assertEqual(len({q["id"] for q in questions}), len(questions))
        for question in questions:
            self.assertIsNone(question["correct_option"])
            self.assertEqual(question["answer_status"], "UNVERIFIED")
            if question["status"] == "READY":
                self.assertEqual(list(question["options"]), list("ABCDE"))
                self.assertTrue(question["text"].strip())
                self.assertTrue(all(question["options"].values()))
        self.assertEqual(sum(q["status"] == "NEEDS_REVIEW" for q in questions), 22)
        spanning = next(q for q in questions if q["text"].startswith("Χειρουργός πραγματοποιεί το χειρισμό Pringle"))
        self.assertEqual(spanning["discipline"], "ANATOMY")
        self.assertEqual(spanning["source_page"], 396)
        self.assertEqual(spanning["options"]["E"], "Το C+D")


if __name__ == "__main__":
    unittest.main()
