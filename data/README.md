# Question bank

Generate `questions.jsonl` from your original PDF using `scripts/parse_question_bank.py`.
The generated data and PDF remain outside the public source repository. The local Docker build includes the generated bank.

`READY` means that parsing passed structural checks, not that the medical answer has been verified. All imported answer keys are `UNVERIFIED` with no correct answer assigned. Questions marked `NEEDS_REVIEW` are excluded from practice sessions.
