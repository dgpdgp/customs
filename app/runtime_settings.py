"""Настройки и ключи, которые администратор меняет в панели без перезапуска сайта.

Как это устроено:
  * исходные значения берутся из .env / окружения (app/config.py);
  * значение, сохранённое в панели, лежит в таблице app_settings и перекрывает исходное;
  * при старте и при каждом сохранении значение записывается прямо в объект настроек
    (get_settings()), поэтому весь код сразу видит новое значение;
  * «Сбросить» удаляет запись из таблицы и возвращает значение из .env.

Секреты (ключи API) хранятся в базе зашифрованными ключом, производным от SECRET_KEY:
копия базы без .env ключей не раскрывает. Если SECRET_KEY поменяется, сохранённые
в панели ключи прочитать будет нельзя — их нужно ввести заново.
"""

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.i18n import LANGUAGES, t
from app.models import AdminAuditLog, AppSetting, User
from app.services.schema_variants import VARIANT_KEYS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingDef:
    key: str
    kind: str  # text | secret | int | bool | choice
    group: str  # llm | access | limits | interface
    choices: tuple[str, ...] = ()
    min_value: int = 0


EDITABLE: tuple[SettingDef, ...] = (
    SettingDef("llm_provider", "choice", "llm", ("anthropic", "openai")),
    SettingDef("anthropic_api_key", "secret", "llm"),
    SettingDef("llm_model", "text", "llm"),
    SettingDef("llm_effort", "choice", "llm", ("low", "medium", "high", "xhigh", "max")),
    SettingDef("llm_fallbacks", "bool", "llm"),
    SettingDef("llm_schema_variant", "choice", "llm", VARIANT_KEYS),
    SettingDef("openai_api_key", "secret", "llm"),
    SettingDef("openai_base_url", "text", "llm"),
    SettingDef("openai_model", "text", "llm"),
    SettingDef("openai_max_tokens", "int", "llm"),
    SettingDef("registration_enabled", "bool", "access"),
    SettingDef("registration_invite_code", "text", "access"),
    SettingDef("allowed_emails", "text", "access"),
    SettingDef("max_llm_requests_per_user_per_day", "int", "limits"),
    SettingDef("max_llm_requests_per_day", "int", "limits"),
    SettingDef("login_max_failures", "int", "limits"),
    SettingDef("login_lockout_minutes", "int", "limits", min_value=1),
    SettingDef("max_upload_mb", "int", "limits", min_value=1),
    SettingDef("default_language", "choice", "interface", tuple(LANGUAGES)),
    SettingDef("hs_code_length", "int", "interface", min_value=1),
)
BY_KEY = {d.key: d for d in EDITABLE}
GROUPS = ("llm", "access", "limits", "interface")

# Значения из .env до применения переопределений — к ним возвращает «Сбросить»
_baseline: dict[str, Any] = {}


# ---------- Шифрование секретов ----------

def _fernet() -> Fernet:
    digest = hashlib.sha256(("customs-app-settings:" + get_settings().secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str | None:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def mask(secret: str | None) -> str:
    """sk-ant-api03-…AB12: достаточно, чтобы узнать ключ, но не чтобы им воспользоваться."""
    if not secret:
        return ""
    if len(secret) <= 12:
        return "•" * len(secret)
    return f"{secret[:7]}…{secret[-4:]}"


# ---------- Разбор значений из формы ----------

def parse_value(defn: SettingDef, raw: str) -> Any:
    raw = raw.strip()
    if defn.kind == "int":
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(t("admin.err.int")) from None
        if value < defn.min_value:
            raise ValueError(t("admin.err.min", n=defn.min_value))
        return value
    if defn.kind == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    if defn.kind == "choice":
        if raw not in defn.choices:
            raise ValueError(t("admin.err.choice", choices=", ".join(defn.choices)))
        return raw
    if defn.kind == "secret" and not raw:
        raise ValueError(t("admin.err.secret_empty"))
    if defn.key == "registration_invite_code" and raw and len(raw) < 8:
        raise ValueError(t("admin.err.invite_short"))
    return raw


def _serialize(defn: SettingDef, value: Any) -> str:
    if defn.kind == "bool":
        return "true" if value else "false"
    if defn.kind == "secret":
        return encrypt(str(value))
    return str(value)


def _deserialize(defn: SettingDef, stored: str) -> Any:
    if defn.kind == "secret":
        return decrypt(stored)
    if defn.kind == "int":
        return int(stored)
    if defn.kind == "bool":
        return stored == "true"
    return stored


# ---------- Применение ----------

def _remember_baseline() -> None:
    if _baseline:
        return
    settings = get_settings()
    for defn in EDITABLE:
        _baseline[defn.key] = getattr(settings, defn.key)


def _after_change(key: str) -> None:
    if key == "anthropic_api_key":
        from app.services import llm  # локальный импорт: llm зависит от настроек

        llm._client.cache_clear()  # клиент SDK кешируется вместе со старым ключом


def apply_overrides(db: Session) -> list[str]:
    """При старте: записывает значения из app_settings в объект настроек.
    Возвращает ключи, которые не удалось применить (например, секрет после смены SECRET_KEY)."""
    _remember_baseline()
    settings = get_settings()
    failed = []
    for row in db.scalars(select(AppSetting)):
        defn = BY_KEY.get(row.key)
        if defn is None:
            continue
        try:
            value = _deserialize(defn, row.value)
        except ValueError:
            value = None
        if value is None:
            logger.warning("Настройку %s из админ-панели прочитать не удалось — используется значение из .env",
                           row.key)
            failed.append(row.key)
            continue
        setattr(settings, row.key, value)
        _after_change(row.key)
    return failed


def audit(db: Session, user: User | None, action: str, detail: str | None = None) -> None:
    db.add(AdminAuditLog(user_id=user.id if user else None, action=action, detail=detail))


def set_value(db: Session, key: str, raw: str, user: User) -> None:
    """Проверяет, сохраняет и сразу применяет значение. ValueError — понятная ошибка ввода."""
    _remember_baseline()
    defn = BY_KEY[key]
    value = parse_value(defn, raw)
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = _serialize(defn, value)
    row.updated_by_id = user.id
    row.updated_at = datetime.now(UTC)
    db.add(row)
    audit(db, user, "setting.update", f"{key} = {'***' if defn.kind == 'secret' else value}")
    db.commit()
    setattr(get_settings(), key, value)
    _after_change(key)


def reset_value(db: Session, key: str, user: User) -> None:
    _remember_baseline()
    row = db.get(AppSetting, key)
    if row is not None:
        db.delete(row)
    audit(db, user, "setting.reset", key)
    db.commit()
    setattr(get_settings(), key, _baseline[key])
    _after_change(key)


def rows_for_display(db: Session) -> dict[str, list[dict]]:
    """Строки для страницы настроек, по группам. Секреты — только в замаскированном виде."""
    _remember_baseline()
    settings = get_settings()
    stored = {row.key: row for row in db.scalars(select(AppSetting))}
    users = {u.id: u.email for u in db.scalars(select(User).where(
        User.id.in_([r.updated_by_id for r in stored.values() if r.updated_by_id])))}
    groups: dict[str, list[dict]] = {g: [] for g in GROUPS}
    for defn in EDITABLE:
        value = getattr(settings, defn.key)
        row = stored.get(defn.key)
        groups[defn.group].append({
            "key": defn.key,
            "kind": defn.kind,
            "choices": defn.choices,
            "value": mask(value) if defn.kind == "secret" else value,
            "is_set": bool(value),
            "source": "panel" if row is not None else "env",
            "updated_at": row.updated_at if row else None,
            "updated_by": users.get(row.updated_by_id) if row else None,
            "min_value": defn.min_value,
        })
    return groups


def ensure_admins(db: Session) -> None:
    """Пользователи из ADMIN_EMAILS получают права администратора (при старте)."""
    emails = get_settings().admin_email_set
    if not emails:
        return
    for user in db.scalars(select(User).where(User.email.in_(emails), User.is_admin.is_(False))):
        user.is_admin = True
    db.commit()
