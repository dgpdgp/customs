"""ORM-модели (схема БД).

users             — учётные записи декларантов
user_sessions     — выданные JWT (по jti): позволяет выйти из системы и отозвать токен
declaration_jobs  — одна «сессия обработки»: загруженные файлы, черновик и утверждённые данные
generation_logs   — журнал вызовов LLM, валидации и экспорта (аудит, расход токенов)

DDL для SQLite: docs/schema.sql (генерируется скриптом scripts/dump_schema.py).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # хранится в нижнем регистре
    password_hash: Mapped[str] = mapped_column(String(255))  # bcrypt, пароль в открытом виде не хранится
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    jobs: Mapped[list["DeclarationJob"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # claim "jti" в JWT
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    user: Mapped[User] = relationship(back_populates="sessions")


class JobStatus:
    PROCESSING = "processing"  # файлы загружены, идёт разбор и вызов LLM
    REVIEW = "review"  # черновик готов, ждёт проверки декларантом (Diff View)
    APPROVED = "approved"  # декларант утвердил данные
    EXPORTED = "exported"  # итоговый файл сформирован хотя бы раз
    FAILED = "failed"  # ошибка разбора файлов или LLM — см. error_message


class DeclarationJob(Base):
    __tablename__ = "declaration_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PROCESSING, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    # Загруженные файлы: [{"role": "reference"|"commercial"|"template",
    #                       "original_name": ..., "stored_path": ..., "size": ...}]
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # Параметры обработки, выбранные пользователем (например, группировка по коду ТН ВЭД)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    reference_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # эталон, как его прочитала LLM
    proposed_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # черновик новой декларации
    approved_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # данные, утверждённые человеком
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # замечания LLM и валидатора

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="jobs")
    logs: Mapped[list["GenerationLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")

    def file_by_role(self, role: str) -> list[dict[str, Any]]:
        return [f for f in self.files if f["role"] == role]


class GenerationLog(Base):
    __tablename__ = "generation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("declaration_jobs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # parse | llm_extract | validate | approve | export | error
    event: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok | error
    model: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[DeclarationJob] = relationship(back_populates="logs")
