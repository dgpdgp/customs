"""Печатает DDL таблиц SQLite, сгенерированный из ORM-моделей (app/models.py).

    python scripts/dump_schema.py > docs/schema.sql
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects import sqlite  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app import models  # noqa: E402, F401  (регистрирует таблицы)
from app.database import Base  # noqa: E402

print("-- Схема БД (SQLite). Сгенерировано: python scripts/dump_schema.py > docs/schema.sql")
print("-- Источник истины — app/models.py; таблицы создаются автоматически при старте приложения.\n")
for table in Base.metadata.sorted_tables:
    ddl = str(CreateTable(table).compile(dialect=sqlite.dialect())).strip()
    print("\n".join(line.rstrip() for line in ddl.splitlines()) + ";\n")
    for index in sorted(table.indexes, key=lambda i: i.name or ""):
        print(str(CreateIndex(index).compile(dialect=sqlite.dialect())).strip() + ";")
    print()
