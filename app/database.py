"""Подключение к SQLite через SQLAlchemy 2.x."""

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

engine = create_engine(
    settings.database_url,
    # SQLite по умолчанию запрещает использовать соединение из другого потока,
    # а FastAPI выполняет sync-эндпоинты и фоновые задачи в пуле потоков.
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")  # SQLite не проверяет FK без этой настройки
    cursor.execute("PRAGMA journal_mode=WAL")  # чтение не блокируется записью фоновой задачи
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI-зависимость: одна сессия БД на запрос."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # Для MVP создаём таблицы при старте. В продакшене — миграции Alembic.
    from app import models  # noqa: F401  (регистрирует модели в Base.metadata)

    Base.metadata.create_all(bind=engine)
