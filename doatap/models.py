"""Durable users, question bank and immutable practice-session snapshots."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    language: Mapped[str] = mapped_column(String(8), default="el", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    discipline: Mapped[str] = mapped_column(String(150), index=True)
    text: Mapped[str] = mapped_column(Text)
    original_text: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON)
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_year: Mapped[int | None] = mapped_column(Integer)
    source_document: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="NEEDS_REVIEW")
    answer_status: Mapped[str] = mapped_column(String(24), default="UNVERIFIED")
    correct_option: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("status IN ('READY', 'NEEDS_REVIEW')"),
        CheckConstraint("answer_status IN ('UNVERIFIED', 'OFFICIAL', 'MEDICAL_REVIEWED')"),
        CheckConstraint("answer_status = 'UNVERIFIED' OR correct_option IS NOT NULL"),
    )


class PracticeSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    discipline: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    requested_count: Mapped[int] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer)
    answered_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'COMPLETED', 'ABANDONED')"),
        CheckConstraint("answered_count >= 0 AND answered_count <= total"),
        Index(
            "one_active_session_per_user", "user_id", unique=True,
            sqlite_where=text("status = 'ACTIVE'"),
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )


class SessionQuestion(Base):
    __tablename__ = "session_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (
        UniqueConstraint("session_id", "question_id"),
        UniqueConstraint("session_id", "position"),
    )


class Answer(Base):
    __tablename__ = "answers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    question_id: Mapped[str] = mapped_column(String(100))
    option: Mapped[int] = mapped_column(Integer)
    verified: Mapped[bool]
    correct: Mapped[bool | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("session_id", "question_id"),)


class Favorite(Base):
    __tablename__ = "favorites"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    question_id: Mapped[str] = mapped_column(ForeignKey("questions.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
