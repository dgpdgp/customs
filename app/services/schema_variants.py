"""Варианты схемы ответа ИИ — чтобы найти схему, которую примет строгий режим Claude API.

На настоящем API строгая схема (вариант full) отклоняется ошибкой «compiled grammar is too large»,
хотя в ней нет объединений типов и необязательных полей. Сайт тогда работает в запасном режиме
(схема в тексте запроса, ответ проверяет Pydantic). Здесь собраны варианты, которые уменьшают
грамматику разными способами. Вариант выбирается настройкой LLM_SCHEMA_VARIANT (или в админ-панели)
и проверяется на API командой `python scripts/check_llm_schema.py --variant all`.

  full — текущая схема (WireResult). По умолчанию: поведение сайта прежнее.
  a    — та же структура без description и title. По документации Claude API смена описаний не
         сбрасывает кеш скомпилированной грамматики, то есть описания в неё, вероятно, не входят;
         вариант нужен, чтобы проверить это на практике.
  b    — меньше полей: у позиций нет item_no (номер = место в массиве) и unit_price,
         в отгрузке эталона — только номера документов и дата инвойса.
  c    — два вызова: (1) эталон, (2) новая поставка и позиции; каждая схема примерно вдвое меньше.
  d    — позиции таблицей: строка позиции — массив строк в фиксированном порядке колонок,
         поэтому имён полей позиций в грамматике нет.
  e    — c и d вместе: два вызова, позиции таблицей (самые маленькие схемы).

Ответ любого варианта сводится к одному и тому же WireResult, дальше всё как обычно (wire_to_result).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, Field

from app.i18n import t
from app.services.wire import (
    Count,
    HsBasis,
    Number,
    Text,
    TextList,
    WireHeader,
    WireIssue,
    WireNewItem,
    WireReference,
    WireReferenceItem,
    WireResult,
    WireShipment,
    _Wire,
)

DEFAULT_VARIANT = "full"


class VariantError(ValueError):
    """Ответ модели нельзя однозначно свести к WireResult (например, строка таблицы не той длины)."""


@dataclass(frozen=True)
class Part:
    """Один вызов модели в рамках варианта."""

    name: str  # all | reference | new
    model: type[BaseModel]
    schema: dict
    instruction: str = ""  # добавка к инструкции пользователя для этого вызова
    with_reference: bool = True  # передавать документ эталона
    with_commercial: bool = True  # передавать новые документы
    # Что передать следующим вызовам (например, извлечённый эталон для второго шага)
    context: Callable[[BaseModel], str] | None = None


@dataclass(frozen=True)
class Variant:
    key: str
    summary: str  # одна строка для scripts/check_llm_schema.py и документации
    parts: tuple[Part, ...]
    combine: Callable[[dict[str, BaseModel]], WireResult] = field(repr=False)


def _strip_keys(node, keys: frozenset[str]):
    """Убирает служебные ключи схемы (description, title), но не поля с такими именами
    (у позиции есть поле description — оно остаётся)."""
    if isinstance(node, list):
        return [_strip_keys(v, keys) for v in node]
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key in ("properties", "$defs"):
            result[key] = {name: _strip_keys(child, keys) for name, child in value.items()}
        elif key not in keys:
            result[key] = _strip_keys(value, keys)
    return result


# ---------- full и a ----------

FULL_SCHEMA = anthropic.transform_schema(WireResult)


def _single(parsed: dict[str, BaseModel]) -> WireResult:
    return parsed["all"]


FULL = Variant("full", "текущая схема, один вызов", (Part("all", WireResult, FULL_SCHEMA),), _single)
NO_DESCRIPTIONS = Variant(
    "a", "та же схема без description и title",
    (Part("all", WireResult, _strip_keys(FULL_SCHEMA, frozenset({"description", "title"}))),), _single,
)


# ---------- b: меньше полей ----------

class CompactReferenceItem(_Wire):
    description: Text
    article: Text
    hs_code: Text = Field(description="Код ТН ВЭД, только цифры")
    country_of_origin: Text
    quantity: Number
    unit: Text
    packages: Number
    gross_weight_kg: Number
    net_weight_kg: Number
    total_value: Number


class CompactNewItem(CompactReferenceItem):
    hs_code_basis: HsBasis = Field(description="Откуда код: из нового документа / из совпадающей позиции эталона / "
                                               "не найден")
    reference_item_no: Count = Field(description="Место позиции эталона в массиве (с 1), 0 — нет соответствия")
    source_document: Text = Field(description="Имя документа-источника (атрибут name)")
    source_quote: Text = Field(description="Дословный фрагмент строки документа, до 200 символов")


class CompactReferenceShipment(_Wire):
    invoice_numbers: TextList
    invoice_date: Text
    transport_document: Text


class CompactReference(_Wire):
    header: WireHeader
    shipment: CompactReferenceShipment
    items: list[CompactReferenceItem]


class CompactResult(_Wire):
    reference: CompactReference
    new_shipment: WireShipment
    new_items: list[CompactNewItem]
    issues: list[WireIssue]


_COMPACT_INSTRUCTION = (
    "Формат ответа сокращён: у позиций нет полей item_no и unit_price. Номер позиции — её место "
    "в массиве, начиная с 1: по нему нумеруй item_no в issues и reference_item_no. В shipment эталона — "
    "только номера документов и дата инвойса."
)


def _combine_compact(parsed: dict[str, BaseModel]) -> WireResult:
    result: CompactResult = parsed["all"]
    reference_items = [WireReferenceItem(item_no=n, unit_price="", **item.model_dump())
                       for n, item in enumerate(result.reference.items, start=1)]
    new_items = [WireNewItem(item_no=n, unit_price="", **item.model_dump())
                 for n, item in enumerate(result.new_items, start=1)]
    shipment = WireShipment(vehicle_id="", total_invoice_value="", total_packages="", total_gross_weight_kg="",
                            total_net_weight_kg="", **result.reference.shipment.model_dump())
    return WireResult(
        reference=WireReference(header=result.reference.header, shipment=shipment, items=reference_items),
        new_shipment=result.new_shipment, new_items=new_items, issues=result.issues,
    )


COMPACT = Variant(
    "b", "меньше полей: без item_no и unit_price у позиций, короткая отгрузка эталона",
    (Part("all", CompactResult, anthropic.transform_schema(CompactResult), _COMPACT_INSTRUCTION),),
    _combine_compact,
)


# ---------- c: два вызова ----------

class ReferencePart(_Wire):
    reference: WireReference
    issues: list[WireIssue]


class NewPart(_Wire):
    new_shipment: WireShipment
    new_items: list[WireNewItem]
    issues: list[WireIssue]


_SPLIT_REFERENCE_INSTRUCTION = (
    "Это шаг 1 из 2: здесь только эталонная декларация, новых документов нет. Выполни только шаг 1 "
    "(reference) и верни issues к эталону с item_no = 0. Новую поставку и позиции не заполняй — "
    "их извлекут на шаге 2."
)
_SPLIT_NEW_INSTRUCTION = (
    "Это шаг 2 из 2. Эталон уже извлечён на шаге 1 и передан в теге <reference_extracted> (JSON); "
    "сам документ эталона здесь не передаётся. Выполни шаги 2–4 (new_shipment, new_items, сверка "
    "реквизитов) и сопоставляй новые позиции с позициями из <reference_extracted>."
)


def _combine_split(parsed: dict[str, BaseModel]) -> WireResult:
    reference: ReferencePart = parsed["reference"]
    new: NewPart = parsed["new"]
    return WireResult(reference=reference.reference, new_shipment=new.new_shipment, new_items=new.new_items,
                      issues=[*reference.issues, *new.issues])


SPLIT = Variant(
    "c", "два вызова: сначала эталон, потом новые позиции",
    (
        Part("reference", ReferencePart, anthropic.transform_schema(ReferencePart), _SPLIT_REFERENCE_INSTRUCTION,
             with_commercial=False, context=lambda part: part.reference.model_dump_json()),
        Part("new", NewPart, anthropic.transform_schema(NewPart), _SPLIT_NEW_INSTRUCTION, with_reference=False),
    ),
    _combine_split,
)


# ---------- d: позиции таблицей ----------

REFERENCE_COLUMNS = tuple(WireReferenceItem.model_fields)
NEW_COLUMNS = (*REFERENCE_COLUMNS, *(name for name in WireNewItem.model_fields if name not in REFERENCE_COLUMNS))
Row = list[Text]


class TableReference(_Wire):
    header: WireHeader
    shipment: WireShipment
    items: list[Row] = Field(description=f"Позиции эталона: каждая — массив из {len(REFERENCE_COLUMNS)} строк "
                                         f"в порядке колонок: {', '.join(REFERENCE_COLUMNS)}")


class TableResult(_Wire):
    reference: TableReference
    new_shipment: WireShipment
    new_items: list[Row] = Field(description=f"Новые позиции: каждая — массив из {len(NEW_COLUMNS)} строк "
                                             f"в порядке колонок: {', '.join(NEW_COLUMNS)}")
    issues: list[WireIssue]


_TABLE_INSTRUCTION = (
    "Позиции передай таблицей: каждая позиция — массив строк, ровно по одному значению на колонку, "
    "в таком порядке. Позиции эталона (reference.items): " + ", ".join(REFERENCE_COLUMNS) + ". "
    "Новые позиции (new_items): " + ", ".join(NEW_COLUMNS) + ". Пустое значение — пустая строка, "
    "номера (item_no, reference_item_no) — цифрами в строке. Не пропускай и не добавляй колонки."
)


def _rows(rows: list[list[str]], columns: tuple[str, ...], model: type[_Wire], where: str) -> list:
    items = []
    for number, row in enumerate(rows, start=1):
        if len(row) != len(columns):
            # Сдвиг колонок перепутал бы вес со стоимостью — такой ответ не принимаем
            raise VariantError(t("llm.bad_row", where=where, n=number, got=len(row), expected=len(columns)))
        items.append(model.model_validate(dict(zip(columns, row, strict=True))))
    return items


def _combine_table(parsed: dict[str, BaseModel]) -> WireResult:
    result: TableResult = parsed["all"]
    return WireResult(
        reference=WireReference(
            header=result.reference.header, shipment=result.reference.shipment,
            items=_rows(result.reference.items, REFERENCE_COLUMNS, WireReferenceItem, "reference.items"),
        ),
        new_shipment=result.new_shipment,
        new_items=_rows(result.new_items, NEW_COLUMNS, WireNewItem, "new_items"),
        issues=result.issues,
    )


TABLE = Variant(
    "d", "позиции таблицей (массивы строк), один вызов",
    (Part("all", TableResult, anthropic.transform_schema(TableResult), _TABLE_INSTRUCTION),),
    _combine_table,
)


# ---------- e: два вызова + таблицы (самая маленькая грамматика) ----------

class TableReferencePart(_Wire):
    reference: TableReference
    issues: list[WireIssue]


class TableNewPart(_Wire):
    new_shipment: WireShipment
    new_items: list[Row] = TableResult.model_fields["new_items"]
    issues: list[WireIssue]


def _combine_split_table(parsed: dict[str, BaseModel]) -> WireResult:
    reference: TableReferencePart = parsed["reference"]
    new: TableNewPart = parsed["new"]
    return WireResult(
        reference=WireReference(
            header=reference.reference.header, shipment=reference.reference.shipment,
            items=_rows(reference.reference.items, REFERENCE_COLUMNS, WireReferenceItem, "reference.items"),
        ),
        new_shipment=new.new_shipment,
        new_items=_rows(new.new_items, NEW_COLUMNS, WireNewItem, "new_items"),
        issues=[*reference.issues, *new.issues],
    )


SPLIT_TABLE = Variant(
    "e", "c + d: два вызова, позиции таблицей",
    (
        Part("reference", TableReferencePart, anthropic.transform_schema(TableReferencePart),
             f"{_SPLIT_REFERENCE_INSTRUCTION} {_TABLE_INSTRUCTION}", with_commercial=False,
             context=lambda part: part.reference.model_dump_json()),
        Part("new", TableNewPart, anthropic.transform_schema(TableNewPart),
             f"{_SPLIT_NEW_INSTRUCTION} {_TABLE_INSTRUCTION}", with_reference=False),
    ),
    _combine_split_table,
)


VARIANTS: dict[str, Variant] = {v.key: v for v in (FULL, NO_DESCRIPTIONS, COMPACT, SPLIT, TABLE, SPLIT_TABLE)}
VARIANT_KEYS = tuple(VARIANTS)


def get(key: str | None) -> Variant:
    variant = VARIANTS.get((key or DEFAULT_VARIANT).strip().lower())
    if variant is None:
        raise VariantError(t("llm.unknown_variant", variant=key, choices=", ".join(VARIANT_KEYS)))
    return variant


# ---------- Размер схемы (для проверки и тестов, без запросов к API) ----------

def stats(schema: dict) -> dict:
    """Числа, которые влияют на грамматику: поля с раскрытием $ref, глубина, объединения и т. д."""
    defs = schema.get("$defs", {})
    result = {"bytes": len(json.dumps(schema, ensure_ascii=False).encode()), "fields": 0, "objects": 0,
              "arrays": 0, "enums": 0, "unions": 0, "optional": 0, "max_depth": 0, "description_chars": 0}

    def walk(node: dict, depth: int) -> None:
        if "$ref" in node:
            node = defs[node["$ref"].split("/")[-1]]
        result["max_depth"] = max(result["max_depth"], depth)
        result["description_chars"] += len(node.get("description", ""))
        if "anyOf" in node or isinstance(node.get("type"), list):
            result["unions"] += 1
        if "enum" in node:
            result["enums"] += 1
        if node.get("type") == "object":
            props = node.get("properties", {})
            result["objects"] += 1
            result["fields"] += len(props)
            result["optional"] += len(set(props) - set(node.get("required", [])))
            for child in props.values():
                walk(child, depth + 1)
        elif node.get("type") == "array":
            result["arrays"] += 1
            walk(node["items"], depth + 1)

    walk(schema, 0)
    return result
