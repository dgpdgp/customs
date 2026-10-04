"""Создание пользователя или смена пароля администратором сервера.

Нужен, когда регистрация на сайте закрыта (REGISTRATION_ENABLED=false):

    python scripts/create_user.py user@example.com --name "Иванов И.И."
    python scripts/create_user.py user@example.com --reset-password
    python scripts/create_user.py user@example.com --disable

Пароль запрашивается интерактивно и не попадает в историю команд.
В Docker: docker compose exec <сервис> python scripts/create_user.py user@example.com
"""

import argparse
import getpass
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import EmailStr, TypeAdapter, ValidationError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import User, UserSession  # noqa: E402
from app.security import create_user, hash_password, validate_password_strength  # noqa: E402


def ask_password() -> str:
    password = getpass.getpass("Пароль (минимум 8 символов): ")
    if password != getpass.getpass("Повторите пароль: "):
        sys.exit("Пароли не совпадают")
    try:
        validate_password_strength(password)
    except ValueError as exc:
        sys.exit(str(exc))
    return password


def revoke_sessions(db, user: User) -> None:
    now = datetime.now(UTC)
    for session in db.scalars(select(UserSession).where(UserSession.user_id == user.id,
                                                        UserSession.revoked_at.is_(None))):
        session.revoked_at = now


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление пользователями Customs Assistant")
    parser.add_argument("email")
    parser.add_argument("--name", help="ФИО (для нового пользователя)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--reset-password", action="store_true", help="сменить пароль существующего пользователя")
    action.add_argument("--disable", action="store_true", help="заблокировать вход и завершить все сессии")
    args = parser.parse_args()

    try:
        email = TypeAdapter(EmailStr).validate_python(args.email.strip()).lower()
    except ValidationError:
        sys.exit(f"Некорректный email: {args.email}")
    init_db()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == email))
        if args.disable or args.reset_password:
            if user is None:
                sys.exit(f"Пользователь {email} не найден")
            if args.disable:
                user.is_active = False
                revoke_sessions(db, user)
                db.commit()
                print(f"Пользователь {email} заблокирован, сессии завершены")
                return
            user.password_hash = hash_password(ask_password())
            user.is_active = True
            revoke_sessions(db, user)
            db.commit()
            print(f"Пароль пользователя {email} изменён, старые сессии завершены")
            return

        if user is not None:
            sys.exit(f"Пользователь {email} уже существует (смена пароля: --reset-password)")
        try:
            create_user(db, email, ask_password(), args.name)
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"Пользователь {email} создан")


if __name__ == "__main__":
    main()
