"""Small synchronous domain API; each mutation uses one database transaction."""

import json
import random
from pathlib import Path

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .db import init_db, make_engine, make_session_factory
from .models import Answer, Favorite, PracticeSession, Question, SessionQuestion, User, utcnow

VERIFIED = {"OFFICIAL", "MEDICAL_REVIEWED"}
LANGUAGES = {"el", "ru", "en"}


def _question_dict(question: Question) -> dict:
    return {key: getattr(question, key) for key in (
        "id", "discipline", "text", "original_text", "options", "source_page",
        "source_year", "source_document", "status", "answer_status", "correct_option",
    )}


def _public_question(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != "correct_option"}


def _normalize_question(record: dict) -> dict:
    required = ("id", "discipline", "text", "options")
    if any(not record.get(key) for key in required):
        raise ValueError("Question requires id, discipline, text and options")
    options = record["options"]
    correct_option = record.get("correct_option")
    if isinstance(options, dict):
        labels = sorted(options)
        if any(label not in "ABCDE" or len(label) != 1 for label in labels):
            raise ValueError("Option labels must be A through E")
        if correct_option is not None and correct_option in labels:
            correct_option = labels.index(correct_option)
        options = [options[label] for label in labels]
    if not isinstance(options, list) or not 2 <= len(options) <= 5:
        raise ValueError("Each question needs 2–5 options")
    if any(not isinstance(option, str) or not option.strip() for option in options):
        raise ValueError("Options must be nonempty strings")
    status = record.get("status", "NEEDS_REVIEW")
    answer_status = record.get("answer_status", "UNVERIFIED")
    if status not in {"READY", "NEEDS_REVIEW"}:
        raise ValueError("Invalid question status")
    if answer_status not in VERIFIED | {"UNVERIFIED"}:
        raise ValueError("Invalid answer status")
    if answer_status == "UNVERIFIED":
        correct_option = None
    elif type(correct_option) is not int or not 0 <= correct_option < len(options):
        raise ValueError("Verified question requires a valid correct_option")
    qid = str(record["id"])
    if len(qid) > 100:
        raise ValueError("Question id exceeds 100 characters")
    return {
        "id": qid, "discipline": str(record["discipline"]), "text": record["text"],
        "original_text": record.get("original_text") or record["text"],
        "options": options, "source_page": record.get("source_page"),
        "source_year": record.get("source_year"),
        "source_document": record.get("source_document"), "status": status,
        "answer_status": answer_status, "correct_option": correct_option,
    }


class BotService:
    def __init__(self, session_factory: sessionmaker[Session]):
        self.session_factory = session_factory

    def upsert_user(self, telegram_id: int, language: str | None = None) -> dict:
        if language is not None and language not in LANGUAGES:
            raise ValueError("Unsupported language")
        with self.session_factory.begin() as db:
            user = db.scalar(select(User).where(User.telegram_id == telegram_id))
            created = user is None
            if user is None:
                user = User(telegram_id=telegram_id, language=language or "el")
                db.add(user)
                db.flush()
            elif language is not None:
                user.language = language
            return {"id": user.id, "telegram_id": user.telegram_id,
                    "language": user.language, "created": created}

    def set_language(self, user_id: int, language: str) -> dict:
        if language not in LANGUAGES:
            raise ValueError("Unsupported language")
        with self.session_factory.begin() as db:
            user = self._user(db, user_id)
            user.language = language
            return {"id": user.id, "telegram_id": user.telegram_id, "language": language}

    def import_bank(self, path: str | Path) -> dict:
        records = []
        seen = set()
        with Path(path).open(encoding="utf-8") as source:
            for lineno, line in enumerate(source, 1):
                if not line.strip():
                    continue
                try:
                    record = _normalize_question(json.loads(line))
                except (ValueError, TypeError, AttributeError) as exc:
                    raise ValueError(f"Invalid bank line {lineno}: {exc}") from exc
                if record["id"] in seen:
                    raise ValueError(f"Duplicate question id on bank line {lineno}")
                seen.add(record["id"])
                records.append(record)
        counts = {"inserted": 0, "updated": 0, "skipped": 0}
        with self.session_factory.begin() as db:
            for record in records:
                question = db.get(Question, record["id"])
                if question is None:
                    db.add(Question(**record))
                    counts["inserted"] += 1
                    continue
                if question.answer_status in VERIFIED:
                    # An import cannot silently replace a reviewed answer or attach
                    # it to changed text/options. Explicit review must edit the bank.
                    for key in ("text", "original_text", "options", "answer_status", "correct_option"):
                        record[key] = getattr(question, key)
                changed = any(getattr(question, key) != value for key, value in record.items())
                if changed:
                    for key, value in record.items():
                        setattr(question, key, value)
                    counts["updated"] += 1
                else:
                    counts["skipped"] += 1
        return counts

    def disciplines(self) -> list[dict]:
        with self.session_factory() as db:
            rows = db.execute(select(Question.discipline, func.count()).where(
                Question.status == "READY").group_by(Question.discipline).order_by(Question.discipline))
            return [{"discipline": discipline, "count": count} for discipline, count in rows]

    @staticmethod
    def _attempt_totals(user_id: int):
        """All saved answers count, including unfinished and abandoned sessions."""
        return (select(
            Answer.question_id.label("question_id"),
            func.max(Answer.id).label("latest_id"),
            func.count(Answer.id).label("attempt_count"),
        ).join(PracticeSession, PracticeSession.id == Answer.session_id)
          .where(PracticeSession.user_id == user_id)
          .group_by(Answer.question_id).subquery())

    def progress(self, user_id: int) -> dict:
        """Coverage and latest outcomes for the current READY bank, per user."""
        fields = ("total", "answered_unique", "remaining", "latest_correct",
                  "latest_wrong", "latest_unverified", "total_attempts")
        overall = dict.fromkeys(fields, 0)
        subjects = {}
        with self.session_factory() as db:
            self._user(db, user_id)
            attempts = self._attempt_totals(user_id)
            rows = db.execute(select(
                Question.discipline, attempts.c.attempt_count, Answer.verified, Answer.correct,
            ).outerjoin(attempts, attempts.c.question_id == Question.id)
             .outerjoin(Answer, Answer.id == attempts.c.latest_id)
             .where(Question.status == "READY").order_by(Question.discipline))
            for discipline, count, verified, correct in rows:
                subject = subjects.setdefault(discipline, {"discipline": discipline,
                                                           **dict.fromkeys(fields, 0)})
                for metrics in (overall, subject):
                    metrics["total"] += 1
                    if not count:
                        metrics["remaining"] += 1
                        continue
                    metrics["answered_unique"] += 1
                    metrics["total_attempts"] += count
                    bucket = ("latest_unverified" if not verified else
                              "latest_correct" if correct else "latest_wrong")
                    metrics[bucket] += 1
        return {**overall, "disciplines": list(subjects.values())}

    @staticmethod
    def _user(db: Session, user_id: int) -> User:
        user = db.get(User, user_id)
        if user is None:
            raise LookupError("User not found")
        return user

    @staticmethod
    def _owned(db: Session, user_id: int, session_id: int, lock: bool = False) -> PracticeSession:
        query = select(PracticeSession).where(PracticeSession.id == session_id)
        if lock:
            query = query.with_for_update()
        session = db.scalar(query)
        if session is None:
            raise LookupError("Session not found")
        if session.user_id != user_id:
            raise PermissionError("Session belongs to a different user")
        return session

    @staticmethod
    def _current(db: Session, session: PracticeSession) -> SessionQuestion | None:
        if session.status != "ACTIVE":
            return None
        return db.scalar(select(SessionQuestion).where(
            SessionQuestion.session_id == session.id,
            SessionQuestion.position == session.answered_count))

    def _session_dict(self, db: Session, session: PracticeSession) -> dict:
        current = self._current(db, session)
        return {
            "id": session.id, "status": session.status, "discipline": session.discipline,
            "total": session.total, "requested_count": session.requested_count,
            "answered": session.answered_count,
            "current_question_id": current.question_id if current else None,
            "created_at": session.created_at.isoformat(),
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        }

    def start_session(self, user_id: int, discipline: str, count: int = 10) -> dict:
        if type(count) is not int or count not in {10, 25, 50}:
            raise ValueError("Session size must be 10, 25 or 50")
        with self.session_factory.begin() as db:
            self._user(db, user_id)
            # Serialize starts for a given user on PostgreSQL.
            db.execute(select(User.id).where(User.id == user_id).with_for_update())
            available = list(db.scalars(select(Question).where(
                Question.discipline == discipline, Question.status == "READY")))
            if not available:
                raise ValueError("No ready questions for this discipline")
            selected = random.SystemRandom().sample(available, min(count, len(available)))
            return self._create_session(db, user_id, discipline, count, selected)

    def _create_session(self, db: Session, user_id: int, discipline: str,
                        count: int, selected: list[Question]) -> dict:
        db.execute(update(PracticeSession).where(
            PracticeSession.user_id == user_id, PracticeSession.status == "ACTIVE"
        ).values(status="ABANDONED", completed_at=utcnow()))
        session = PracticeSession(user_id=user_id, discipline=discipline,
                                  requested_count=count, total=len(selected))
        db.add(session)
        db.flush()
        for position, question in enumerate(selected):
            db.add(SessionQuestion(session_id=session.id, question_id=question.id,
                                   position=position, snapshot=_question_dict(question)))
        db.flush()
        return self._session_dict(db, session)

    def start_study_session(self, user_id: int, discipline: str | None = None,
                            count: int = 25) -> dict | None:
        """Take the next unseen questions; assignment alone never marks one seen.

        Source pages are ordered within documents, with stable IDs breaking ties
        on the same page. Exhaustion leaves an existing session untouched.
        """
        if type(count) is not int or not 1 <= count <= 50:
            raise ValueError("Study session size must be between 1 and 50")
        if discipline == "ALL":
            discipline = None
        with self.session_factory.begin() as db:
            self._user(db, user_id)
            db.execute(select(User.id).where(User.id == user_id).with_for_update())
            answered = (select(Answer.question_id)
                        .join(PracticeSession, PracticeSession.id == Answer.session_id)
                        .where(PracticeSession.user_id == user_id))
            query = select(Question).where(Question.status == "READY", ~Question.id.in_(answered))
            if discipline is not None:
                query = query.where(Question.discipline == discipline)
            selected = list(db.scalars(query.order_by(
                Question.source_document.asc().nulls_last(),
                Question.source_year.asc().nulls_last(),
                Question.source_page.asc().nulls_last(), Question.id,
            ).limit(count)))
            if not selected:
                return None
            return self._create_session(db, user_id, discipline or "ALL", count, selected)

    def resume_session(self, user_id: int) -> dict | None:
        with self.session_factory() as db:
            self._user(db, user_id)
            session = db.scalar(select(PracticeSession).where(
                PracticeSession.user_id == user_id, PracticeSession.status == "ACTIVE"))
            return self._session_dict(db, session) if session else None

    def get_session(self, user_id: int, session_id: int) -> dict:
        with self.session_factory() as db:
            return self._session_dict(db, self._owned(db, user_id, session_id))

    def current_question(self, user_id: int, session_id: int) -> dict | None:
        with self.session_factory() as db:
            session = self._owned(db, user_id, session_id)
            current = self._current(db, session)
            if current is None:
                return None
            return {**_public_question(current.snapshot), "position": current.position + 1,
                    "total": session.total}

    @staticmethod
    def _feedback(snapshot: dict, answer: Answer) -> dict:
        return {
            "verified": answer.verified, "correct": answer.correct,
            "correct_option": snapshot["correct_option"] if answer.verified else None,
            "answer_status": snapshot["answer_status"], "selected_option": answer.option,
        }

    def answer(self, user_id: int, session_id: int, question_id: str, option: int) -> dict:
        with self.session_factory.begin() as db:
            session = self._owned(db, user_id, session_id, lock=True)
            existing = db.scalar(select(Answer).where(
                Answer.session_id == session_id, Answer.question_id == question_id))
            if existing is not None:
                snapshot = db.scalar(select(SessionQuestion.snapshot).where(
                    SessionQuestion.session_id == session_id,
                    SessionQuestion.question_id == question_id))
                return {"accepted": False, "duplicate": True, "reason": "duplicate",
                        "feedback": self._feedback(snapshot, existing),
                        "session": self._session_dict(db, session)}
            if session.status != "ACTIVE":
                return {"accepted": False, "duplicate": False, "reason": "completed",
                        "feedback": None, "session": self._session_dict(db, session)}
            current = self._current(db, session)
            if current is None or current.question_id != question_id:
                return {"accepted": False, "duplicate": False, "reason": "stale",
                        "feedback": None, "session": self._session_dict(db, session)}
            snapshot = current.snapshot
            if type(option) is not int or not 0 <= option < len(snapshot["options"]):
                raise ValueError("Invalid answer option")
            answered_count = session.answered_count + 1
            finished = answered_count == session.total
            # Compare-and-swap also guards concurrent duplicate callbacks on SQLite.
            result = db.execute(update(PracticeSession).where(
                PracticeSession.id == session_id, PracticeSession.status == "ACTIVE",
                PracticeSession.answered_count == session.answered_count,
            ).values(answered_count=answered_count,
                     status="COMPLETED" if finished else "ACTIVE",
                     completed_at=utcnow() if finished else None)
              .execution_options(synchronize_session=False))
            if result.rowcount != 1:
                db.refresh(session)
                return {"accepted": False, "duplicate": False, "reason": "stale",
                        "feedback": None, "session": self._session_dict(db, session)}
            verified = snapshot["answer_status"] in VERIFIED and snapshot["correct_option"] is not None
            answer = Answer(session_id=session_id, question_id=question_id, option=option,
                            verified=verified,
                            correct=(option == snapshot["correct_option"]) if verified else None)
            db.add(answer)
            db.flush()
            db.refresh(session)
            return {"accepted": True, "duplicate": False, "reason": "accepted",
                    "feedback": self._feedback(snapshot, answer),
                    "session": self._session_dict(db, session)}

    def _summary(self, db: Session, session: PracticeSession) -> dict:
        answers = list(db.scalars(select(Answer).where(Answer.session_id == session.id)))
        scored_total = sum(answer.verified for answer in answers)
        correct = sum(answer.correct is True for answer in answers)
        return {**self._session_dict(db, session), "scored_total": scored_total,
                "correct": correct, "unverified": len(answers) - scored_total,
                "percent": round(100 * correct / scored_total, 1) if scored_total else None}

    def summary(self, user_id: int, session_id: int) -> dict:
        with self.session_factory() as db:
            return self._summary(db, self._owned(db, user_id, session_id))

    def finish_session(self, user_id: int, session_id: int) -> dict:
        with self.session_factory.begin() as db:
            session = self._owned(db, user_id, session_id, lock=True)
            if session.status == "ACTIVE":
                session.status = "COMPLETED"
                session.completed_at = utcnow()
                db.flush()
            return self._summary(db, session)

    def history(self, user_id: int, limit: int = 10) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        with self.session_factory() as db:
            self._user(db, user_id)
            sessions = db.scalars(select(PracticeSession).where(
                PracticeSession.user_id == user_id, PracticeSession.status != "ACTIVE"
            ).order_by(PracticeSession.id.desc()).limit(limit))
            return [self._summary(db, session) for session in sessions]

    @staticmethod
    def _page(total: int, page: int, page_size: int) -> dict:
        if type(page) is not int or page < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("Page must be nonnegative and page size between 1 and 100")
        pages = max(1, (total + page_size - 1) // page_size)
        return {"total": total, "page": min(page, pages - 1), "page_size": page_size, "pages": pages}

    @staticmethod
    def _answer_item(answer: Answer, snapshot: dict, attempt_count: int) -> dict:
        return {
            "id": answer.question_id, "question_id": answer.question_id,
            "answer_id": answer.id, "session_id": answer.session_id,
            "attempt_count": attempt_count, "selected_option": answer.option,
            "verified": answer.verified, "correct": answer.correct,
            "answer_status": snapshot["answer_status"],
            "correct_option": snapshot["correct_option"] if answer.verified else None,
            "answered_at": answer.created_at.isoformat(), "snapshot": snapshot,
        }

    def question_history(self, user_id: int, discipline: str | None = None,
                         page: int = 0, page_size: int = 10) -> dict:
        """One latest answer per question, with an immutable historical snapshot.

        Old questions remain available even if the current bank quarantines them.
        Discipline filtering uses the latest attempt's question snapshot.
        """
        with self.session_factory() as db:
            self._user(db, user_id)
            attempts = self._attempt_totals(user_id)
            query = (select(Answer, SessionQuestion.snapshot, attempts.c.attempt_count)
                     .join(attempts, Answer.id == attempts.c.latest_id)
                     .join(SessionQuestion, and_(
                         SessionQuestion.session_id == Answer.session_id,
                         SessionQuestion.question_id == Answer.question_id)))
            if discipline not in {None, "ALL"}:
                query = query.where(SessionQuestion.snapshot["discipline"].as_string() == discipline)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            pagination = self._page(total, page, page_size)
            rows = db.execute(query.order_by(Answer.id.desc()).offset(
                pagination["page"] * page_size).limit(page_size))
            return {**pagination, "items": [self._answer_item(*row) for row in rows]}

    def question_attempts(self, user_id: int, question_id: str,
                          page: int = 0, page_size: int = 10) -> dict:
        """Every saved attempt for one question; never expose another user's data."""
        with self.session_factory() as db:
            self._user(db, user_id)
            query = (select(Answer, SessionQuestion.snapshot)
                     .join(PracticeSession, PracticeSession.id == Answer.session_id)
                     .join(SessionQuestion, and_(
                         SessionQuestion.session_id == Answer.session_id,
                         SessionQuestion.question_id == Answer.question_id))
                     .where(PracticeSession.user_id == user_id, Answer.question_id == question_id))
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            pagination = self._page(total, page, page_size)
            rows = db.execute(query.order_by(Answer.id.desc()).offset(
                pagination["page"] * page_size).limit(page_size))
            return {**pagination, "items": [self._answer_item(answer, snapshot, total)
                                             for answer, snapshot in rows]}

    def session_history(self, user_id: int, page: int = 0, page_size: int = 10) -> dict:
        with self.session_factory() as db:
            self._user(db, user_id)
            query = select(PracticeSession).where(
                PracticeSession.user_id == user_id, PracticeSession.status != "ACTIVE")
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            pagination = self._page(total, page, page_size)
            sessions = db.scalars(query.order_by(PracticeSession.id.desc()).offset(
                pagination["page"] * page_size).limit(page_size))
            return {**pagination, "items": [self._summary(db, session) for session in sessions]}

    def toggle_favorite(self, user_id: int, question_id: str) -> bool:
        with self.session_factory.begin() as db:
            self._user(db, user_id)
            if db.get(Question, question_id) is None:
                raise LookupError("Question not found")
            favorite = db.get(Favorite, (user_id, question_id))
            if favorite is not None:
                db.delete(favorite)
                return False
            db.add(Favorite(user_id=user_id, question_id=question_id))
            return True

    def favorites(self, user_id: int) -> list[dict]:
        with self.session_factory() as db:
            self._user(db, user_id)
            questions = db.scalars(select(Question).join(Favorite).where(
                Favorite.user_id == user_id).order_by(Favorite.created_at.desc()))
            return [_public_question(_question_dict(question)) for question in questions]


def create_service(database_url: str | None = None) -> BotService:
    engine = make_engine(database_url)
    init_db(engine)
    return BotService(make_session_factory(engine))
