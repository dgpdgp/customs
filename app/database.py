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


# Колонки, добавленные после первой версии. create_all() создаёт только новые таблицы,
# поэтому в существующую базу такие колонки добавляем сами (ALTER TABLE ... ADD COLUMN безопасен
# для SQLite: данные не трогает). Для продакшена — миграции Alembic.
_ADDED_COLUMNS = [
    ("users", "is_admin", "BOOLEAN NOT NULL DEFAULT 0"),
]


def _add_missing_columns() -> None:
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    # Для MVP создаём таблицы при старте. В продакшене — миграции Alembic.
    from app import models  # noqa: F401  (регистрирует модели в Base.metadata)

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()
