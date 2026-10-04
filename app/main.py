"""FastAPI-приложение: авторизация, загрузка документов, Diff View, экспорт.

HTML-страницы (Jinja2 + Tailwind) и JSON API работают поверх одних и тех же
сервисов. Запуск: `uvicorn app.main:app --reload` (подробности в README.md).

Маршруты
  Страницы:  GET  /  /login  /register  /dashboard  /jobs/{id}  /healthz
             POST /login  /register  /logout  /jobs (загрузка файлов)
             GET  /jobs/{id}/export  /jobs/{id}/export.json  (скачивание)
  JSON API:  POST /api/auth/register  /api/auth/login  /api/auth/logout
             GET  /api/auth/me  /api/jobs  /api/jobs/{id}
             POST /api/jobs/{id}/approve  /api/jobs/{id}/retry
"""

import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR, get_settings
from app.database import get_db, init_db
from app.limits import LimitExceeded, LoginThrottle, check_llm_quota, login_throttle
from app.models import DeclarationJob, GenerationLog, JobStatus, User
from app.schemas import (
    ApproveRequest,
    DeclarationData,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.security import (
    COOKIE_NAME,
    LoginRequired,
    authenticate,
    create_session_token,
    create_user,
    get_current_user,
    get_optional_user,
    require_user_page,
    revoke_session,
)
from app.services import exporter, validation
from app.services.parsers import EXCEL_EXTENSIONS, PDF_EXTENSIONS
from app.services.pipeline import process_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

APP_DIR = BASE_DIR / "app"
settings = get_settings()

# Допустимые форматы для каждого поля формы загрузки
REFERENCE_EXTENSIONS = PDF_EXTENSIONS | EXCEL_EXTENSIONS | {".xml"}
COMMERCIAL_EXTENSIONS = PDF_EXTENSIONS | EXCEL_EXTENSIONS | {".csv"}
TEMPLATE_EXTENSIONS = {".xml", ".xlsx", ".xlsm", ".txt", ".csv"}
MAX_COMMERCIAL_FILES = 20


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Customs Declaration Assistant",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
    openapi_url="/openapi.json" if settings.enable_api_docs else None,
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.globals["registration_enabled"] = lambda: settings.registration_enabled
templates.env.globals["invite_required"] = lambda: settings.invite_required
templates.env.globals["STATUS_LABELS"] = {
    JobStatus.PROCESSING: "обработка",
    JobStatus.REVIEW: "на проверке",
    JobStatus.APPROVED: "утверждено",
    JobStatus.EXPORTED: "выгружено",
    JobStatus.FAILED: "ошибка",
}

DbSession = Annotated[Session, Depends(get_db)]
PageUser = Annotated[User, Depends(require_user_page)]
ApiUser = Annotated[User, Depends(get_current_user)]


@app.exception_handler(LoginRequired)
async def _login_required_handler(request: Request, _exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=status.HTTP_303_SEE_OTHER)


# =====================================================================
#  Вспомогательные функции
# =====================================================================

def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,  # недоступна из JavaScript — защита от кражи токена через XSS
        samesite="lax",  # не отправляется с POST-запросами с чужих сайтов — защита от CSRF
        secure=settings.cookie_secure,
    )


def _safe_next(next_url: str | None) -> str:
    """Разрешаем редирект только на свои страницы (защита от open redirect)."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/dashboard"


class RegistrationClosed(ValueError):
    pass


# Неудачные попытки ввести код приглашения считаются вместе для всех адресов:
# иначе код можно было бы перебирать, меняя email.
register_throttle = LoginThrottle()
INVITE_THROTTLE_KEY = "__invite_code__"


def _register_user(
    db: Session, email: str, password: str, full_name: str | None, invite_code: str | None = None
) -> User:
    if not settings.can_register(email):
        raise RegistrationClosed("Регистрация закрыта. Учётную запись создаёт администратор сервера.")
    if settings.invite_required:
        # Перебор кода ограничен общим счётчиком неудачных попыток (LimitExceeded)
        register_throttle.check(INVITE_THROTTLE_KEY)
        if not settings.invite_code_valid(invite_code):
            register_throttle.record_failure(INVITE_THROTTLE_KEY)
            raise RegistrationClosed("Неверный код приглашения. Узнайте его у администратора сайта.")
    return create_user(db, email, password, full_name)


def _login_or_raise(db: Session, email: str, password: str) -> User | None:
    """Проверка пароля с ограничением числа неудачных попыток (LimitExceeded)."""
    login_throttle.check(email)
    user = authenticate(db, email, password)
    if user is None:
        login_throttle.record_failure(email)
    else:
        login_throttle.reset(email)
    return user


def _get_job(db: Session, user: User, job_id: int) -> DeclarationJob:
    job = db.get(DeclarationJob, job_id)
    # Чужие задачи отдаём как 404, чтобы не раскрывать факт их существования.
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return job


def _save_upload(upload: UploadFile, dest_dir: Path, role: str, allowed: set[str]) -> dict:
    """Сохраняет файл под случайным именем, проверяя расширение и размер."""
    original_name = Path(upload.filename or "").name  # отбрасываем путь, если браузер его прислал
    ext = Path(original_name).suffix.lower()
    if ext not in allowed:
        raise ValueError(f"Файл «{original_name}»: формат {ext or 'без расширения'} не подходит. "
                         f"Допустимо: {', '.join(sorted(allowed))}")
    dest = dest_dir / f"{role}_{uuid.uuid4().hex}{ext}"
    size = 0
    with dest.open("wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise ValueError(f"Файл «{original_name}» больше {settings.max_upload_mb} МБ")
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ValueError(f"Файл «{original_name}» пустой")
    return {"role": role, "original_name": original_name, "stored_path": str(dest), "size": size}


def _job_payload(job: DeclarationJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "error_message": job.error_message,
        "files": [{"role": f["role"], "name": f["original_name"]} for f in job.files],
        "options": job.options,
        "reference": job.reference_data,
        "proposed": job.proposed_data,
        "approved": job.approved_data,
        "issues": job.issues,
        "created_at": job.created_at.isoformat(),
    }


def _attachment_header(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Проверка живости для Docker / мониторинга."""
    return {"status": "ok"}


# =====================================================================
#  HTML: авторизация
# =====================================================================

@app.get("/", include_in_schema=False)
def index(user: Annotated[User | None, Depends(get_optional_user)]) -> RedirectResponse:
    return RedirectResponse("/dashboard" if user else "/login", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/register", response_class=HTMLResponse, include_in_schema=False)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None, "email": "", "full_name": ""})


@app.post("/register", include_in_schema=False)
def register_submit(
    request: Request,
    db: DbSession,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()],
    full_name: Annotated[str, Form()] = "",
    invite_code: Annotated[str, Form()] = "",
):
    def fail(message: str, code: int = status.HTTP_400_BAD_REQUEST):
        return templates.TemplateResponse(
            request, "register.html", {"error": message, "email": email, "full_name": full_name}, status_code=code
        )

    if password != password_confirm:
        return fail("Пароли не совпадают")
    try:
        data = RegisterRequest(email=email, password=password, full_name=full_name or None, invite_code=invite_code)
        user = _register_user(db, data.email, data.password, data.full_name, data.invite_code)
    except ValidationError:
        return fail("Некорректный email: введите адрес почты целиком, например name@gmail.com")
    except LimitExceeded as exc:
        return fail(str(exc), status.HTTP_429_TOO_MANY_REQUESTS)
    except ValueError as exc:
        return fail(str(exc))

    response = RedirectResponse("/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, create_session_token(db, user, request))
    return response


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, next: str | None = None):
    return templates.TemplateResponse(request, "login.html", {"error": None, "email": "", "next": next or ""})


@app.post("/login", include_in_schema=False)
def login_submit(
    request: Request,
    db: DbSession,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
):
    def fail(message: str, code: int):
        return templates.TemplateResponse(
            request, "login.html", {"error": message, "email": email, "next": next}, status_code=code
        )

    try:
        user = _login_or_raise(db, email, password)
    except LimitExceeded as exc:
        return fail(str(exc), status.HTTP_429_TOO_MANY_REQUESTS)
    if user is None:
        return fail("Неверный email или пароль", status.HTTP_401_UNAUTHORIZED)
    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, create_session_token(db, user, request))
    return response


@app.post("/logout", include_in_schema=False)
def logout(request: Request, db: DbSession) -> RedirectResponse:
    revoke_session(request, db)
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response


# =====================================================================
#  HTML: рабочая область
# =====================================================================

def _render_dashboard(request: Request, db: Session, user: User, error: str | None = None, code: int = 200):
    jobs = db.scalars(
        select(DeclarationJob).where(DeclarationJob.user_id == user.id).order_by(DeclarationJob.id.desc()).limit(20)
    ).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "jobs": jobs,
            "error": error,
            "max_mb": settings.max_upload_mb,
            "reference_accept": ",".join(sorted(REFERENCE_EXTENSIONS)),
            "commercial_accept": ",".join(sorted(COMMERCIAL_EXTENSIONS)),
            "template_accept": ",".join(sorted(TEMPLATE_EXTENSIONS)),
        },
        status_code=code,
    )


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, db: DbSession, user: PageUser):
    return _render_dashboard(request, db, user)


@app.post("/jobs", include_in_schema=False)
def upload_documents(
    request: Request,
    background_tasks: BackgroundTasks,
    db: DbSession,
    user: PageUser,
    reference_file: Annotated[UploadFile, File(description="Эталонная декларация: PDF / XML / Excel")],
    commercial_files: Annotated[list[UploadFile], File(description="Инвойсы и упаковочные листы: PDF / Excel")],
    template_file: Annotated[UploadFile | None, File(description="Шаблон результата: XML / Excel")] = None,
    group_by_hs: Annotated[bool, Form()] = False,
):
    """Принимает файлы, создаёт задачу и запускает обработку в фоне."""
    commercial_files = [f for f in commercial_files if f.filename]
    if not reference_file.filename:
        return _render_dashboard(request, db, user, "Загрузите эталонную декларацию", 400)
    if not commercial_files:
        return _render_dashboard(request, db, user, "Загрузите хотя бы один инвойс или упаковочный лист", 400)
    if len(commercial_files) > MAX_COMMERCIAL_FILES:
        return _render_dashboard(request, db, user, f"Не больше {MAX_COMMERCIAL_FILES} коммерческих документов", 400)
    try:
        check_llm_quota(db, user.id)
    except LimitExceeded as exc:
        return _render_dashboard(request, db, user, str(exc), status.HTTP_429_TOO_MANY_REQUESTS)

    job = DeclarationJob(user_id=user.id, status=JobStatus.PROCESSING, options={"group_by_hs": group_by_hs})
    db.add(job)
    db.commit()

    job_dir = settings.upload_dir / str(user.id) / str(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        files = [_save_upload(reference_file, job_dir, "reference", REFERENCE_EXTENSIONS)]
        files += [_save_upload(f, job_dir, "commercial", COMMERCIAL_EXTENSIONS) for f in commercial_files]
        if template_file is not None and template_file.filename:
            files.append(_save_upload(template_file, job_dir, "template", TEMPLATE_EXTENSIONS))
    except ValueError as exc:
        db.delete(job)
        db.commit()
        shutil.rmtree(job_dir, ignore_errors=True)
        return _render_dashboard(request, db, user, str(exc), 400)

    job.files = files
    db.commit()
    background_tasks.add_task(process_job, job.id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
def review_page(request: Request, job_id: int, db: DbSession, user: PageUser):
    job = _get_job(db, user, job_id)
    return templates.TemplateResponse(request, "review.html", {"user": user, "job": job})


@app.get("/jobs/{job_id}/export", include_in_schema=False)
def export_file(job_id: int, db: DbSession, user: PageUser) -> Response:
    """Подставляет утверждённые данные в шаблон пользователя и отдаёт файл."""
    job = _get_job(db, user, job_id)
    if job.status not in (JobStatus.APPROVED, JobStatus.EXPORTED) or job.approved_data is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала проверьте и утвердите данные")

    template = job.file_by_role("template")
    template_path = Path(template[0]["stored_path"]) if template else None
    try:
        content, filename, media_type = exporter.render_export(
            DeclarationData.model_validate(job.approved_data), template_path, f"declaration_{job.id}"
        )
    except exporter.TemplateRenderError as exc:
        db.add(GenerationLog(job_id=job.id, user_id=user.id, event="export", status="error", message=str(exc)))
        db.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    job.status = JobStatus.EXPORTED
    db.add(GenerationLog(job_id=job.id, user_id=user.id, event="export", message=filename))
    db.commit()
    return Response(content, media_type=media_type, headers=_attachment_header(filename))


@app.get("/jobs/{job_id}/export.json", include_in_schema=False)
def export_json(job_id: int, db: DbSession, user: PageUser) -> Response:
    job = _get_job(db, user, job_id)
    if job.approved_data is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сначала проверьте и утвердите данные")
    content = json.dumps(job.approved_data, ensure_ascii=False, indent=2).encode()
    return Response(content, media_type="application/json",
                    headers=_attachment_header(f"declaration_{job.id}.json"))


# =====================================================================
#  JSON API
# =====================================================================

@app.post("/api/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, tags=["auth"])
def api_register(body: RegisterRequest, db: DbSession):
    try:
        user = _register_user(db, body.email, body.password, body.full_name, body.invite_code)
    except LimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except RegistrationClosed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return UserOut(id=user.id, email=user.email, full_name=user.full_name)


@app.post("/api/auth/login", response_model=TokenResponse, tags=["auth"])
def api_login(body: LoginRequest, request: Request, db: DbSession):
    try:
        user = _login_or_raise(db, body.email, body.password)
    except LimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль")
    return TokenResponse(access_token=create_session_token(db, user, request))


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT, tags=["auth"])
def api_logout(request: Request, db: DbSession, _user: ApiUser) -> Response:
    revoke_session(request, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/auth/me", response_model=UserOut, tags=["auth"])
def api_me(user: ApiUser):
    return UserOut(id=user.id, email=user.email, full_name=user.full_name)


@app.get("/api/jobs", tags=["jobs"])
def api_list_jobs(db: DbSession, user: ApiUser) -> list[dict]:
    jobs = db.scalars(
        select(DeclarationJob).where(DeclarationJob.user_id == user.id).order_by(DeclarationJob.id.desc()).limit(100)
    ).all()
    return [{"id": j.id, "status": j.status, "created_at": j.created_at.isoformat()} for j in jobs]


@app.get("/api/jobs/{job_id}", tags=["jobs"])
def api_get_job(job_id: int, db: DbSession, user: ApiUser) -> dict:
    """Статус задачи и данные для Diff View (страница опрашивает этот эндпоинт)."""
    return _job_payload(_get_job(db, user, job_id))


@app.post("/api/jobs/{job_id}/approve", tags=["jobs"])
def api_approve(job_id: int, body: ApproveRequest, db: DbSession, user: ApiUser) -> dict:
    """Сохраняет данные, проверенные (и при необходимости исправленные) декларантом."""
    job = _get_job(db, user, job_id)
    if job.status not in (JobStatus.REVIEW, JobStatus.APPROVED, JobStatus.EXPORTED):
        raise HTTPException(status.HTTP_409_CONFLICT, "Задача ещё не готова к утверждению")

    reference = DeclarationData.model_validate(job.reference_data)
    issues = validation.run_structural(reference, body.data, settings.hs_code_length)
    manual_edits = body.data.model_dump() != job.proposed_data

    job.approved_data = body.data.model_dump()
    job.status = JobStatus.APPROVED
    job.approved_at = datetime.now(UTC)
    db.add(GenerationLog(
        job_id=job.id, user_id=user.id, event="approve",
        message=("С ручными правками" if manual_edits else "Без правок") + f"; замечаний после проверки: {len(issues)}",
    ))
    db.commit()
    return {"status": job.status, "issues": [i.model_dump() for i in issues]}


@app.post("/api/jobs/{job_id}/retry", tags=["jobs"])
def api_retry(job_id: int, background_tasks: BackgroundTasks, db: DbSession, user: ApiUser) -> dict:
    """Повторная обработка (например, после временной ошибки LLM API)."""
    job = _get_job(db, user, job_id)
    if job.status != JobStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Повторить можно только задачу с ошибкой")
    try:
        check_llm_quota(db, user.id)
    except LimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    job.status = JobStatus.PROCESSING
    job.error_message = None
    db.commit()
    background_tasks.add_task(process_job, job.id)
    return {"status": job.status}
