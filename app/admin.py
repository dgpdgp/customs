"""Админ-панель: /admin.

Разделы: обзор (база, расход на ИИ, сервер), настройки и ключи, пользователи,
задачи, журналы. Доступ — только у пользователей с is_admin (остальным 404).
Все формы защищены CSRF-токеном, действия записываются в admin_audit_log.
"""

import shutil
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import runtime_settings
from app.config import get_settings
from app.database import get_db
from app.i18n import t
from app.models import AdminAuditLog, DeclarationJob, GenerationLog, JobStatus, User, UserSession
from app.runtime_settings import audit
from app.security import create_user, hash_password, require_admin_page, validate_password_strength, verify_csrf
from app.services import admin_stats
from app.services.pipeline import process_job
from app.web import templates

router = APIRouter(prefix="/admin", include_in_schema=False)

DbSession = Annotated[Session, Depends(get_db)]
Admin = Annotated[User, Depends(require_admin_page)]
Csrf = Annotated[str, Form(alias="csrf")]

LOG_EVENTS = ("parse", "llm_request", "llm_extract", "validate", "approve", "export", "error")


def _render(request: Request, template: str, user: User, tab: str, code: int = 200, **context):
    return templates.TemplateResponse(
        request, template, {"user": user, "tab": tab, "notice": request.query_params.get("ok"), **context},
        status_code=code,
    )


def _back(url: str, notice: str) -> RedirectResponse:
    return RedirectResponse(f"{url}?ok={notice}", status_code=status.HTTP_303_SEE_OTHER)


def _human_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def _datetime(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


templates.env.filters["human_size"] = _human_size
templates.env.filters["dt"] = _datetime


# ---------- Обзор ----------

@router.get("", response_class=HTMLResponse)
def overview(request: Request, db: DbSession, user: Admin):
    return _render(request, "admin/overview.html", user, "overview",
                   stats=admin_stats.overview(db), system=admin_stats.system_info())


# ---------- Настройки и ключи ----------

def _settings_page(request: Request, db: Session, user: User, code: int = 200, **context):
    return _render(request, "admin/settings.html", user, "settings", code,
                   groups=runtime_settings.rows_for_display(db), **context)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DbSession, user: Admin):
    return _settings_page(request, db, user)


@router.post("/settings/{key}")
def save_setting(request: Request, key: str, db: DbSession, user: Admin, csrf: Csrf,
                 value: Annotated[str, Form()] = ""):
    verify_csrf(request, csrf)
    if key not in runtime_settings.BY_KEY:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    try:
        runtime_settings.set_value(db, key, value, user)
    except ValueError as exc:
        return _settings_page(request, db, user, status.HTTP_400_BAD_REQUEST, error_key=key, error=str(exc))
    return _back("/admin/settings", "saved")


@router.post("/settings/{key}/reset")
def reset_setting(request: Request, key: str, db: DbSession, user: Admin, csrf: Csrf):
    verify_csrf(request, csrf)
    if key not in runtime_settings.BY_KEY:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    runtime_settings.reset_value(db, key, user)
    return _back("/admin/settings", "reset")


@router.post("/check-key", response_class=HTMLResponse)
def check_key(request: Request, db: DbSession, user: Admin, csrf: Csrf):
    verify_csrf(request, csrf)
    ok, message = admin_stats.check_llm_key()
    audit(db, user, "llm.check_key", "ok" if ok else message[:200])
    db.commit()
    return _settings_page(request, db, user, check_ok=ok, check_message=message)


# ---------- Пользователи ----------

def _users_page(request: Request, db: Session, user: User, code: int = 200, **context):
    jobs_count = dict(db.execute(select(DeclarationJob.user_id, func.count(DeclarationJob.id))
                                 .group_by(DeclarationJob.user_id)).all())
    users = db.scalars(select(User).order_by(User.id)).all()
    return _render(request, "admin/users.html", user, "users", code, users=users, jobs_count=jobs_count, **context)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: DbSession, user: Admin):
    return _users_page(request, db, user)


@router.post("/users")
def create_user_admin(request: Request, db: DbSession, user: Admin, csrf: Csrf,
                      email: Annotated[str, Form()], password: Annotated[str, Form()],
                      full_name: Annotated[str, Form()] = "", is_admin: Annotated[bool, Form()] = False):
    verify_csrf(request, csrf)
    try:
        address = TypeAdapter(EmailStr).validate_python(email.strip())
        created = create_user(db, address, password, full_name or None)
    except ValidationError:
        return _users_page(request, db, user, status.HTTP_400_BAD_REQUEST, error=t("reg.bad_email"))
    except ValueError as exc:
        return _users_page(request, db, user, status.HTTP_400_BAD_REQUEST, error=str(exc))
    if is_admin:
        created.is_admin = True
    audit(db, user, "user.create", f"{created.email}{' (admin)' if created.is_admin else ''}")
    db.commit()
    return _back("/admin/users", "user_created")


def _target(db: Session, admin: User, user_id: int, *, allow_self: bool = False) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if target.id == admin.id and not allow_self:
        # Защита от того, чтобы администратор случайно заблокировал или разжаловал сам себя
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("admin.err.self"))
    return target


def _revoke_sessions(db: Session, target: User) -> None:
    for session in db.scalars(select(UserSession).where(UserSession.user_id == target.id,
                                                        UserSession.revoked_at.is_(None))):
        session.revoked_at = datetime.now(UTC)


@router.post("/users/{user_id}/toggle-active")
def toggle_active(request: Request, user_id: int, db: DbSession, user: Admin, csrf: Csrf):
    verify_csrf(request, csrf)
    target = _target(db, user, user_id)
    target.is_active = not target.is_active
    if not target.is_active:
        _revoke_sessions(db, target)
    audit(db, user, "user.unblock" if target.is_active else "user.block", target.email)
    db.commit()
    return _back("/admin/users", "user_updated")


@router.post("/users/{user_id}/toggle-admin")
def toggle_admin(request: Request, user_id: int, db: DbSession, user: Admin, csrf: Csrf):
    verify_csrf(request, csrf)
    target = _target(db, user, user_id)
    target.is_admin = not target.is_admin
    audit(db, user, "user.make_admin" if target.is_admin else "user.remove_admin", target.email)
    db.commit()
    return _back("/admin/users", "user_updated")


@router.post("/users/{user_id}/password")
def set_password(request: Request, user_id: int, db: DbSession, user: Admin, csrf: Csrf,
                 password: Annotated[str, Form()]):
    verify_csrf(request, csrf)
    target = _target(db, user, user_id, allow_self=True)
    try:
        validate_password_strength(password)
    except ValueError as exc:
        return _users_page(request, db, user, status.HTTP_400_BAD_REQUEST, error=str(exc))
    target.password_hash = hash_password(password)
    _revoke_sessions(db, target)  # старые входы с прежним паролем больше не действуют
    audit(db, user, "user.password", target.email)
    db.commit()
    return _back("/admin/users", "password_changed")


# ---------- Задачи ----------

@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, db: DbSession, user: Admin, status_filter: str = ""):
    query = select(DeclarationJob).order_by(DeclarationJob.id.desc()).limit(200)
    if status_filter:
        query = query.where(DeclarationJob.status == status_filter)
    jobs = db.scalars(query).all()
    emails = {u.id: u.email for u in db.scalars(select(User))}
    return _render(request, "admin/jobs.html", user, "jobs", jobs=jobs, emails=emails, status_filter=status_filter,
                   statuses=(JobStatus.PROCESSING, JobStatus.REVIEW, JobStatus.APPROVED, JobStatus.EXPORTED,
                             JobStatus.FAILED))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int, db: DbSession, user: Admin):
    job = db.get(DeclarationJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    logs = db.scalars(select(GenerationLog).where(GenerationLog.job_id == job.id).order_by(GenerationLog.id)).all()
    usage = [log for log in logs if log.event == "llm_extract"]
    cost = sum((admin_stats.estimate_cost(log.model, log.input_tokens or 0, log.output_tokens or 0) or 0)
               for log in usage)
    return _render(request, "admin/job.html", user, "jobs", job=job, logs=logs, owner=db.get(User, job.user_id),
                   cost=cost if usage else None)


@router.post("/jobs/{job_id}/retry")
def job_retry(request: Request, job_id: int, background_tasks: BackgroundTasks, db: DbSession, user: Admin,
              csrf: Csrf):
    verify_csrf(request, csrf)
    job = db.get(DeclarationJob, job_id)
    if job is None or job.status != JobStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, t("job.retry_only_failed"))
    job.status = JobStatus.PROCESSING
    job.error_message = None
    audit(db, user, "job.retry", f"#{job.id}")
    db.commit()
    background_tasks.add_task(process_job, job.id)
    return _back(f"/admin/jobs/{job.id}", "job_retried")


@router.post("/jobs/{job_id}/delete")
def job_delete(request: Request, job_id: int, db: DbSession, user: Admin, csrf: Csrf):
    """Удаляет задачу, её журнал и загруженные файлы (документы клиента) с диска."""
    verify_csrf(request, csrf)
    job = db.get(DeclarationJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(status.HTTP_409_CONFLICT, t("admin.err.job_processing"))
    shutil.rmtree(get_settings().upload_dir / str(job.user_id) / str(job.id), ignore_errors=True)
    audit(db, user, "job.delete", f"#{job.id} ({len(job.files)} files)")
    db.delete(job)
    db.commit()
    return _back("/admin/jobs", "job_deleted")


# ---------- Журналы ----------

@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: DbSession, user: Admin, event: str = "", status_filter: str = ""):
    query = select(GenerationLog).order_by(GenerationLog.id.desc()).limit(300)
    if event:
        query = query.where(GenerationLog.event == event)
    if status_filter:
        query = query.where(GenerationLog.status == status_filter)
    emails = {u.id: u.email for u in db.scalars(select(User))}
    audit_rows = db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(200)).all()
    return _render(request, "admin/logs.html", user, "logs", logs=db.scalars(query).all(), emails=emails,
                   audit_rows=audit_rows, events=LOG_EVENTS, event=event, status_filter=status_filter)
