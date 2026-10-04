"""Ограничения для публичного сервера: перебор паролей и расход на LLM."""

import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import GenerationLog

LLM_REQUEST_EVENT = "llm_request"  # пишется в generation_logs перед каждым вызовом модели


class LimitExceeded(Exception):
    """Сообщение исключения показывается пользователю."""


class LoginThrottle:
    """Счётчик неудачных входов по email (в памяти процесса).

    Ключ — email, а не IP: за обратным прокси (Caddy, nginx) все запросы приходят
    с одного адреса, и блокировка по IP заблокировала бы всех сразу.
    Приложение работает в одном процессе, поэтому хранения в памяти достаточно.
    """

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float, window: float) -> deque[float]:
        attempts = self._failures[key]
        while attempts and now - attempts[0] > window:
            attempts.popleft()
        return attempts

    def check(self, email: str) -> None:
        settings = get_settings()
        if settings.login_max_failures <= 0:
            return
        window = settings.login_lockout_minutes * 60
        now = time.monotonic()
        with self._lock:
            attempts = self._prune(email.strip().lower(), now, window)
            if len(attempts) >= settings.login_max_failures:
                wait = max(1, int((window - (now - attempts[0])) // 60) + 1)
                raise LimitExceeded(f"Слишком много неудачных попыток входа. Повторите через {wait} мин.")

    def record_failure(self, email: str) -> None:
        with self._lock:
            self._failures[email.strip().lower()].append(time.monotonic())

    def reset(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email.strip().lower(), None)


login_throttle = LoginThrottle()


def check_llm_quota(db: Session, user_id: int) -> None:
    """Не даёт запустить обработку, если исчерпан суточный лимит вызовов LLM."""
    settings = get_settings()
    since = datetime.now(UTC) - timedelta(hours=24)
    base = select(func.count(GenerationLog.id)).where(
        GenerationLog.event == LLM_REQUEST_EVENT, GenerationLog.created_at >= since
    )
    per_user = settings.max_llm_requests_per_user_per_day
    if per_user > 0 and db.scalar(base.where(GenerationLog.user_id == user_id)) >= per_user:
        raise LimitExceeded(f"Достигнут лимит: {per_user} обработок за 24 часа для вашей учётной записи.")
    total = settings.max_llm_requests_per_day
    if total > 0 and db.scalar(base) >= total:
        raise LimitExceeded("Достигнут общий суточный лимит обработок на сервере. Повторите позже.")
