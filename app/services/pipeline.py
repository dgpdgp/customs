"""Фоновая обработка задачи: разбор файлов -> LLM -> сборка черновика -> проверка."""

import logging
import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.limits import LLM_REQUEST_EVENT
from app.models import DeclarationJob, GenerationLog, JobStatus
from app.services import llm, validation
from app.services.merge import build_proposed
from app.services.parsers import ParsedDocument, ParseError, parse_file

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _log(db: Session, job: DeclarationJob, event: str, status: str = "ok", **fields) -> None:
    db.add(GenerationLog(job_id=job.id, user_id=job.user_id, event=event, status=status, **fields))


def _parse(job: DeclarationJob, role: str) -> list[ParsedDocument]:
    return [parse_file(Path(f["stored_path"]), f["original_name"]) for f in job.file_by_role(role)]


def process_job(job_id: int) -> None:
    """Запускается через BackgroundTasks; открывает собственную сессию БД."""
    settings = get_settings()
    db = SessionLocal()
    job = db.get(DeclarationJob, job_id)
    if job is None:
        db.close()
        return
    try:
        started = time.monotonic()
        reference_doc = _parse(job, "reference")[0]
        commercial_docs = _parse(job, "commercial")
        _log(db, job, "parse", duration_ms=int((time.monotonic() - started) * 1000),
             message=f"Документов: {1 + len(commercial_docs)}")

        # Запись до вызова: по ней считаются суточные лимиты (app/limits.py)
        _log(db, job, LLM_REQUEST_EVENT, model=settings.llm_model)
        db.commit()
        result, call = llm.extract_declaration(reference_doc, commercial_docs)
        _log(db, job, "llm_extract", model=call.model, input_tokens=call.input_tokens,
             output_tokens=call.output_tokens, duration_ms=call.duration_ms,
             message=f"Позиций эталона: {len(result.reference.items)}, новых: {len(result.new_items)}")

        proposed, issues = build_proposed(result, group_by_hs=bool(job.options.get("group_by_hs")))
        issues += validation.run_all(result.reference, proposed, commercial_docs, settings.hs_code_length)
        issues.sort(key=lambda i: (SEVERITY_ORDER[i.severity], i.item_no or 0))
        _log(db, job, "validate", message=f"Замечаний: {len(issues)} "
             f"(ошибок: {sum(i.severity == 'error' for i in issues)})")

        job.reference_data = result.reference.model_dump()
        job.proposed_data = proposed.model_dump()
        job.issues = [i.model_dump() for i in issues]
        job.status = JobStatus.REVIEW
        job.error_message = None
    except (ParseError, llm.LLMError) as exc:
        job.status = JobStatus.FAILED
        job.error_message = str(exc)
        _log(db, job, "error", status="error", message=str(exc))
    except Exception as exc:  # непредвиденная ошибка — не оставляем задачу «висеть» в processing
        logger.exception("Ошибка обработки задачи %s", job_id)
        job.status = JobStatus.FAILED
        job.error_message = "Внутренняя ошибка обработки. Подробности в логе сервера."
        _log(db, job, "error", status="error", message=repr(exc)[:2000])
    finally:
        db.commit()
        db.close()
