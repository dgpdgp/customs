"""Сводка для админ-панели: база данных, расход на ИИ, состояние сервера."""

import os
import platform
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import openai
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.i18n import t
from app.limits import LLM_REQUEST_EVENT
from app.models import DeclarationJob, GenerationLog, JobStatus, User

STARTED_AT = time.time()

# Цены Claude API, $ за 1 млн токенов (вход, выход) — по прайсу Anthropic на 25.09.2026.
# Это оценка: точный расход — в консоли Anthropic (Spend this month).
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _price(model: str | None) -> tuple[float, float] | None:
    if not model:
        return None
    # Самое длинное совпадение: «claude-opus-5-5» не должно попасть в «claude-opus-5»
    for name in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if model.startswith(name):
            return PRICES_PER_MTOK[name]
    return None


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    price = _price(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def usage_by_model(db: Session, since: datetime | None) -> list[dict]:
    query = select(GenerationLog.model, func.count(GenerationLog.id), func.sum(GenerationLog.input_tokens),
                   func.sum(GenerationLog.output_tokens)).where(GenerationLog.event == "llm_extract")
    if since is not None:
        query = query.where(GenerationLog.created_at >= since)
    rows = []
    for model, calls, tokens_in, tokens_out in db.execute(query.group_by(GenerationLog.model)):
        tokens_in, tokens_out = tokens_in or 0, tokens_out or 0
        rows.append({"model": model or "—", "calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                     "cost": estimate_cost(model, tokens_in, tokens_out)})
    return rows


def overview(db: Session) -> dict:
    now = datetime.now(UTC)

    def count(query) -> int:
        return db.scalar(query) or 0

    def llm_requests(hours: int) -> int:
        return count(select(func.count(GenerationLog.id)).where(
            GenerationLog.event == LLM_REQUEST_EVENT, GenerationLog.created_at >= now - timedelta(hours=hours)))

    jobs_by_status = dict(db.execute(select(DeclarationJob.status, func.count(DeclarationJob.id))
                                     .group_by(DeclarationJob.status)).all())
    usage_30d = usage_by_model(db, now - timedelta(days=30))
    usage_all = usage_by_model(db, None)
    costs_30d = [row["cost"] for row in usage_30d]
    return {
        "users_total": count(select(func.count(User.id))),
        "users_active": count(select(func.count(User.id)).where(User.is_active.is_(True))),
        "admins": count(select(func.count(User.id)).where(User.is_admin.is_(True))),
        "jobs_total": sum(jobs_by_status.values()),
        "jobs_by_status": {status: jobs_by_status.get(status, 0) for status in (
            JobStatus.PROCESSING, JobStatus.REVIEW, JobStatus.APPROVED, JobStatus.EXPORTED, JobStatus.FAILED)},
        "llm_24h": llm_requests(24),
        "llm_7d": llm_requests(24 * 7),
        "llm_30d": llm_requests(24 * 30),
        "usage_30d": usage_30d,
        "usage_all": usage_all,
        "cost_30d": sum(c for c in costs_30d if c is not None),
        "cost_30d_partial": any(c is None for c in costs_30d),
        "errors_24h": count(select(func.count(GenerationLog.id)).where(
            GenerationLog.status == "error", GenerationLog.created_at >= now - timedelta(hours=24))),
        "recent_errors": db.scalars(select(GenerationLog).where(GenerationLog.status == "error")
                                    .order_by(GenerationLog.id.desc()).limit(10)).all(),
    }


def _dir_stats(path: Path) -> tuple[int, int]:
    files = size = 0
    if path.exists():
        for root, _dirs, names in os.walk(path):
            for name in names:
                try:
                    size += (Path(root) / name).stat().st_size
                    files += 1
                except OSError:
                    continue
    return files, size


def system_info() -> dict:
    settings = get_settings()
    db_path = Path(settings.database_url.removeprefix("sqlite:///")) if settings.database_url.startswith(
        "sqlite:///") else None
    upload_files, upload_size = _dir_stats(settings.upload_dir)
    disk = shutil.disk_usage(settings.upload_dir if settings.upload_dir.exists() else Path.cwd())
    key = settings.anthropic_api_key if settings.llm_provider == "anthropic" else settings.openai_api_key
    return {
        "version": os.environ.get("APP_VERSION") or "—",
        "uptime_seconds": int(time.time() - STARTED_AT),
        "python": platform.python_version(),
        "db_size": db_path.stat().st_size if db_path and db_path.exists() else None,
        "upload_files": upload_files,
        "upload_size": upload_size,
        "disk_free": disk.free,
        "disk_total": disk.total,
        "provider": settings.llm_provider,
        "model": settings.active_model,
        "key_configured": bool(key) or (settings.llm_provider == "anthropic" and bool(os.environ.get(
            "ANTHROPIC_API_KEY"))),
    }


def check_llm_key() -> tuple[bool, str]:
    """Проверка ключа без затрат: у Anthropic — подсчёт токенов (бесплатный метод API),
    у OpenAI-совместимых — список моделей. Ответ модели не генерируется."""
    settings = get_settings()
    try:
        if settings.llm_provider == "openai":
            if not settings.openai_api_key:
                return False, t("llm.no_key", var="OPENAI_API_KEY")
            client = openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)
            client.models.list()
            return True, t("admin.check.ok", model=settings.openai_model or "—")
        from app.services import llm

        client = llm._client()
        if client.api_key is None and client.auth_token is None and client.credentials is None:
            return False, t("llm.no_key", var="Anthropic API (ANTHROPIC_API_KEY)")
        client.messages.count_tokens(model=settings.llm_model, messages=[{"role": "user", "content": "ping"}])
        return True, t("admin.check.ok", model=settings.llm_model)
    except (anthropic.AuthenticationError, openai.AuthenticationError):
        return False, t("admin.check.bad_key")
    except (anthropic.NotFoundError, openai.NotFoundError):
        return False, t("admin.check.no_model", model=settings.active_model)
    except (anthropic.PermissionDeniedError, openai.PermissionDeniedError):
        return False, t("llm.no_access", model=settings.active_model)
    except (anthropic.APIConnectionError, openai.APIConnectionError):
        return False, t("llm.no_connection", provider=settings.llm_provider)
    except (anthropic.APIStatusError, openai.APIStatusError) as exc:
        return False, t("llm.api_error", provider=settings.llm_provider, status=exc.status_code)
