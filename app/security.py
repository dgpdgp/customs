"""Аутентификация: хеширование паролей (bcrypt) и JWT-сессии.

Токен передаётся в httpOnly-cookie `access_token` (для HTML-страниц)
или в заголовке `Authorization: Bearer <token>` (для API-клиентов).
Каждый JWT содержит `jti`, который хранится в таблице user_sessions,
поэтому выход из системы реально отзывает токен.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.i18n import t
from app.models import User, UserSession

ALGORITHM = "HS256"
COOKIE_NAME = "access_token"
BCRYPT_MAX_BYTES = 72  # bcrypt учитывает только первые 72 байта пароля

# Хеш-заглушка: при входе с несуществующим email всё равно выполняем bcrypt,
# чтобы по времени ответа нельзя было узнать, зарегистрирован ли адрес.
_DUMMY_HASH = bcrypt.hashpw(b"timing-attack-protection", bcrypt.gensalt()).decode()


class LoginRequired(Exception):
    """HTML-страница требует входа — обработчик в main.py делает редирект на /login."""


# ---------- Пароли ----------

def validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise ValueError(t("auth.pw_short"))
    if len(password.encode()) > BCRYPT_MAX_BYTES:
        raise ValueError(t("auth.pw_long"))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:  # пароль длиннее 72 байт или повреждённый хеш
        return False


def create_user(db: Session, email: str, password: str, full_name: str | None = None) -> User:
    """Создаёт пользователя. ValueError — слабый пароль или email уже занят."""
    validate_password_strength(password)
    user = User(email=email.strip().lower(), password_hash=hash_password(password), full_name=full_name or None)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError(t("auth.email_taken")) from None
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


# ---------- JWT-сессии ----------

def create_session_token(db: Session, user: User, request: Request | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_expire_minutes)
    token_id = secrets.token_hex(16)

    db.add(
        UserSession(
            user_id=user.id,
            token_id=token_id,
            expires_at=expires,
            ip_address=request.client.host if request and request.client else None,
            user_agent=(request.headers.get("user-agent", "")[:512] if request else None),
        )
    )
    user.last_login_at = now
    db.commit()

    payload = {"sub": str(user.id), "jti": token_id, "iat": now, "exp": expires}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(COOKIE_NAME)


def _resolve_user(request: Request, db: Session) -> tuple[User, UserSession] | None:
    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:  # подпись неверна, токен истёк или повреждён
        return None

    session = db.scalar(select(UserSession).where(UserSession.token_id == payload.get("jti")))
    if session is None or session.revoked_at is not None:
        return None
    if session.expires_at.replace(tzinfo=session.expires_at.tzinfo or UTC) <= datetime.now(UTC):
        return None
    user = session.user
    if not user.is_active or str(user.id) != payload.get("sub"):
        return None
    return user, session


def revoke_session(request: Request, db: Session) -> None:
    resolved = _resolve_user(request, db)
    if resolved:
        resolved[1].revoked_at = datetime.now(UTC)
        db.commit()


# ---------- FastAPI-зависимости ----------

def get_current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """Для JSON API: 401, если пользователь не вошёл."""
    resolved = _resolve_user(request, db)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=t("auth.required"),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return resolved[0]


def require_user_page(request: Request, db: Annotated[Session, Depends(get_db)]) -> User:
    """Для HTML-страниц: редирект на /login, если пользователь не вошёл."""
    resolved = _resolve_user(request, db)
    if resolved is None:
        raise LoginRequired()
    return resolved[0]


def get_optional_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User | None:
    resolved = _resolve_user(request, db)
    return resolved[0] if resolved else None
