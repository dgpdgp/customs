"""Настройки приложения.

Все значения читаются из переменных окружения или файла `.env`
(см. `.env.example`). Имена переменных совпадают с именами полей
в верхнем регистре: `secret_key` -> `SECRET_KEY`.
"""

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # --- Безопасность ---
    # Ключ подписи JWT. Если не задан, генерируется случайный при каждом
    # запуске (все сессии сбрасываются после перезапуска) — только для разработки.
    secret_key: str = ""
    access_token_expire_minutes: int = 8 * 60  # рабочая смена декларанта
    # True в продакшене за HTTPS: cookie с токеном не уйдёт по HTTP.
    cookie_secure: bool = False

    # --- Доступ к публичному серверу ---
    # Каждая загрузка — платный вызов LLM с вашего ключа. На публичном сервере
    # закройте регистрацию (REGISTRATION_ENABLED=false и scripts/create_user.py)
    # или ограничьте её списком адресов (ALLOWED_EMAILS=a@x.com,b@y.com).
    registration_enabled: bool = True
    allowed_emails: str = ""
    # Защита входа от перебора паролей: N неудачных попыток на email за окно.
    login_max_failures: int = 10
    login_lockout_minutes: int = 15
    # Swagger UI (/docs). На публичном сервере можно выключить.
    enable_api_docs: bool = True

    # --- Лимиты расходов на LLM (запросов за последние 24 часа; 0 — без лимита) ---
    max_llm_requests_per_user_per_day: int = 30
    max_llm_requests_per_day: int = 100

    # --- Хранилище ---
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    max_upload_mb: int = 25

    # --- LLM (Anthropic) ---
    # Если ключ пустой, SDK берёт ANTHROPIC_API_KEY / профиль `ant auth login`.
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    # Глубина рассуждений: low | medium | high | xhigh | max.
    # Для извлечения данных без ошибок используем high (по умолчанию у модели medium).
    llm_effort: str = "high"
    llm_max_tokens: int = 64000
    # Серверный фолбэк на другую модель, если основная откажется отвечать
    # (stop_reason = "refusal"). Работает только с прямым Claude API.
    llm_fallbacks: bool = True
    # Защита от случайной загрузки гигантских файлов: суммарный объём текста,
    # отправляемого в LLM. Текст никогда не обрезается молча — при превышении
    # пользователь получает понятную ошибку.
    llm_max_input_chars: int = 800_000

    # --- Правила проверки ---
    # Длина кода ТН ВЭД (ЕАЭС и Узбекистан — 10 знаков).
    hs_code_length: int = 10

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    def can_register(self, email: str) -> bool:
        if not self.registration_enabled:
            return False
        allowed = self.allowed_email_set
        return not allowed or email.strip().lower() in allowed


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if not settings.secret_key:
        logger.warning("SECRET_KEY не задан — используется временный ключ, сессии сбросятся после перезапуска")
        settings.secret_key = secrets.token_urlsafe(48)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if settings.database_url.startswith("sqlite:///"):
        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    return settings
