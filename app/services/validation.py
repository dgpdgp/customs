"""Детерминированная проверка ответа LLM (второй рубеж защиты от галлюцинаций).

LLM может ошибиться, даже следуя промпту. Поэтому всё, что можно проверить
кодом, проверяется кодом:
  * цитата-источник каждой позиции действительно есть в тексте документа;
  * числа позиции (количество, стоимость, веса) встречаются в документах;
  * код ТН ВЭД корректного формата и действительно взят из документа
    или из указанной позиции эталона;
  * суммы позиций сходятся с итогами инвойса / упаковочного листа;
  * обязательные поля заполнены.
Результат — список Issue, который декларант видит в Diff View.
"""

import re
from decimal import Decimal, InvalidOperation

from app.i18n import t
from app.schemas import DeclarationData, GoodsItem, Issue, ShipmentData
from app.services.parsers import ParsedDocument

# Подписи полей берутся из переводов (app/i18n.py, ключи field.*)
NUMERIC_ITEM_FIELDS = ("quantity", "packages", "gross_weight_kg", "net_weight_kg", "unit_price", "total_value")
REQUIRED_ITEM_FIELDS = (
    "description", "quantity", "unit", "total_value", "net_weight_kg", "gross_weight_kg", "country_of_origin",
)
MONEY_TOLERANCE = Decimal("0.01")
WEIGHT_TOLERANCE_RATIO = Decimal("0.005")  # 0,5 %: веса в упаковочных листах часто округлены построчно

_QUOTE_CHARS = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "'": '"', " ": " ", "|": " "})
_NUMBER_SPAN = re.compile(r"\d[\d\s .,]*\d|\d")


# ---------- Вспомогательные функции ----------

def normalize_text(text: str) -> str:
    """Нормализация для поиска цитат: регистр, кавычки, пробелы, разделители ячеек."""
    return " ".join(text.translate(_QUOTE_CHARS).casefold().split())


def _number_variants(token: str) -> set[Decimal]:
    """Все разумные прочтения числа: «1 234,56», «1,234.56», «1.234,56», «4800»."""
    token = token.replace(" ", "").replace(" ", "").strip(".,")
    if not token:
        return set()
    candidates: set[str] = set()
    if "," in token and "." in token:
        decimal_sep = "," if token.rfind(",") > token.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        candidates.add(token.replace(thousands_sep, "").replace(decimal_sep, "."))
    elif "," in token or "." in token:
        sep = "," if "," in token else "."
        head, _, tail = token.rpartition(sep)
        if token.count(sep) > 1:
            candidates.add(token.replace(sep, ""))  # 1,234,567 — разделители тысяч
        else:
            candidates.add(f"{head}.{tail}")  # десятичная дробь
            if len(tail) == 3:
                candidates.add(head + tail)  # «1,234» может быть и тысячами
    else:
        candidates.add(token)

    result = set()
    for candidate in candidates:
        try:
            result.add(Decimal(candidate))
        except InvalidOperation:
            continue
    return result


def numbers_in_text(text: str) -> set[Decimal]:
    numbers: set[Decimal] = set()
    for match in _NUMBER_SPAN.finditer(text):
        span = match.group()
        numbers |= _number_variants(span)
        # «3 2280» может быть двумя соседними ячейками — учитываем и отдельные части.
        for part in span.split():
            numbers |= _number_variants(part)
    return numbers


def _to_decimal(value: float | int) -> Decimal:
    return Decimal(str(value))


def _issue(severity: str, message: str, item_no: int | None = None, field: str | None = None) -> Issue:
    return Issue(severity=severity, message=message, item_no=item_no, field=field, source="validator")


# ---------- Проверки ----------

def check_provenance(items: list[GoodsItem], documents: list[ParsedDocument]) -> list[Issue]:
    """Цитаты и числа позиций должны присутствовать в коммерческих документах."""
    issues: list[Issue] = []
    docs_by_name = {doc.name: doc for doc in documents}
    text_docs = [doc for doc in documents if doc.has_text_layer]
    normalized = {doc.name: normalize_text(doc.text) for doc in text_docs}
    all_numbers: set[Decimal] = set()
    for doc in text_docs:
        all_numbers |= numbers_in_text(doc.text)
    has_scans = any(not doc.has_text_layer for doc in documents)

    for item in items:
        source = docs_by_name.get(item.source_document or "")
        if item.source_document and source is None:
            issues.append(_issue("warning", t("val.source_doc_missing", doc=item.source_document),
                                 item.item_no, "source_document"))

        if source is not None and not source.has_text_layer:
            issues.append(_issue("info", t("val.source_is_scan", doc=source.name), item.item_no, "source_quote"))
            continue  # текста нет — автоматическая сверка невозможна

        if not item.source_quote:
            issues.append(_issue("error", t("val.quote_missing"), item.item_no, "source_quote"))
        else:
            quote = normalize_text(item.source_quote)
            if not any(quote in text for text in normalized.values()):
                issues.append(_issue("error", t("val.quote_not_found", quote=item.source_quote[:80]),
                                     item.item_no, "source_quote"))
            elif source is not None and quote not in normalized[source.name]:
                issues.append(_issue("warning", t("val.quote_other_doc", doc=source.name),
                                     item.item_no, "source_document"))

        if has_scans:
            continue  # число могло быть взято со скана — не помечаем ложными предупреждениями
        for field in NUMERIC_ITEM_FIELDS:
            value = getattr(item, field)
            if value is not None and _to_decimal(value) not in all_numbers:
                issues.append(_issue("warning", t("val.number_not_in_docs", label=t(f"field.{field}"), value=value),
                                     item.item_no, field))
    return issues


def check_hs_codes(
    items: list[GoodsItem], reference: DeclarationData, documents: list[ParsedDocument], hs_code_length: int
) -> list[Issue]:
    issues: list[Issue] = []
    reference_items = {ref.item_no: ref for ref in reference.items}
    docs_by_name = {doc.name: doc for doc in documents}

    for item in items:
        if item.reference_item_no is not None and item.reference_item_no not in reference_items:
            issues.append(_issue("warning", t("val.ref_item_missing", no=item.reference_item_no),
                                 item.item_no, "reference_item_no"))

        code = item.hs_code
        if not code:
            issues.append(_issue("error", t("val.hs_missing"), item.item_no, "hs_code"))
            continue
        if not code.isdigit() or len(code) != hs_code_length:
            issues.append(_issue("error", t("val.hs_format", code=code, n=hs_code_length), item.item_no, "hs_code"))

        if item.hs_code_basis == "reference_match":
            ref = reference_items.get(item.reference_item_no or -1)
            if ref is None or ref.hs_code != code:
                ref_code = (ref.hs_code if ref else None) or t("val.hs_ref_absent")
                issues.append(_issue("error", t("val.hs_ref_mismatch", code=code, no=item.reference_item_no,
                                                ref_code=ref_code), item.item_no, "hs_code"))
        elif item.hs_code_basis == "document":
            source = docs_by_name.get(item.source_document or "")
            texts = [source.text] if source is not None and source.has_text_layer else [
                d.text for d in documents if d.has_text_layer]
            if texts and not any(code in re.sub(r"[\s.]", "", text) for text in texts):
                issues.append(_issue("warning", t("val.hs_not_in_doc", code=code), item.item_no, "hs_code"))
        elif item.hs_code_basis == "not_found":
            issues.append(_issue("warning", t("val.hs_basis_conflict"), item.item_no, "hs_code"))
    return issues


def check_totals(shipment: ShipmentData, items: list[GoodsItem]) -> list[Issue]:
    """Сверка сумм позиций с итогами, напечатанными в инвойсе и упаковочном листе."""
    issues: list[Issue] = []
    checks = [
        ("total_value", "total_invoice_value", None),
        ("net_weight_kg", "total_net_weight_kg", WEIGHT_TOLERANCE_RATIO),
        ("gross_weight_kg", "total_gross_weight_kg", WEIGHT_TOLERANCE_RATIO),
        ("packages", "total_packages", None),
    ]
    for item_field, total_field, ratio in checks:
        label = t(f"field.{total_field}")
        values = [getattr(item, item_field) for item in items]
        total = getattr(shipment, total_field)
        if total is None:
            issues.append(_issue("info", t("val.total_missing", label=label), field=f"shipment.{total_field}"))
            continue
        if any(v is None for v in values):
            issues.append(_issue("warning", t("val.total_incomplete", label=label), field=f"shipment.{total_field}"))
            continue
        items_sum = sum((_to_decimal(v) for v in values), Decimal(0))
        tolerance = max(_to_decimal(total) * ratio, MONEY_TOLERANCE) if ratio else MONEY_TOLERANCE
        if abs(items_sum - _to_decimal(total)) > tolerance:
            severity = "error" if item_field == "total_value" else "warning"
            issues.append(_issue(severity, t("val.total_mismatch", label=label, sum=f"{items_sum.normalize():f}",
                                             total=total), field=f"shipment.{total_field}"))

    for item in items:
        if item.net_weight_kg is not None and item.gross_weight_kg is not None \
                and item.net_weight_kg > item.gross_weight_kg:
            issues.append(_issue("error", t("val.net_gt_gross", net=item.net_weight_kg, gross=item.gross_weight_kg),
                                 item.item_no, "net_weight_kg"))
    return issues


def check_required(items: list[GoodsItem]) -> list[Issue]:
    issues = []
    for item in items:
        for field in REQUIRED_ITEM_FIELDS:
            if getattr(item, field) in (None, ""):
                message = t("val.required_empty", label=t(f"field.{field}"))
                issues.append(_issue("warning", message, item.item_no, field))
    return issues


def check_reference(reference: DeclarationData) -> list[Issue]:
    """Ключевые реквизиты должны найтись в эталоне — иначе их нечем заполнить."""
    issues = []
    for path in ("exporter", "importer", "declarant"):
        if not getattr(reference.header, path).name:
            issues.append(_issue("warning", t("val.ref_party_missing", label=t(f"party.{path}")),
                                 field=f"header.{path}.name"))
    if not reference.items:
        issues.append(_issue("warning", t("val.ref_no_items")))
    return issues


def run_all(
    reference: DeclarationData,
    proposed: DeclarationData,
    commercial_docs: list[ParsedDocument],
    hs_code_length: int,
) -> list[Issue]:
    """Полная проверка черновика после ответа LLM."""
    return [
        *check_reference(reference),
        *check_provenance(proposed.items, commercial_docs),
        *check_hs_codes(proposed.items, reference, commercial_docs, hs_code_length),
        *check_totals(proposed.shipment, proposed.items),
        *check_required(proposed.items),
    ]


def run_structural(reference: DeclarationData, approved: DeclarationData, hs_code_length: int) -> list[Issue]:
    """Повторная проверка данных, утверждённых человеком (без сверки цитат —
    правки декларанта источником не подтверждаются)."""
    issues = [
        *check_totals(approved.shipment, approved.items),
        *check_required(approved.items),
    ]
    for item in approved.items:
        code = item.hs_code or ""
        if not code.isdigit() or len(code) != hs_code_length:
            issues.append(_issue("error", t("val.hs_format", code=code, n=hs_code_length), item.item_no, "hs_code"))
    if approved.header != reference.header:
        issues.append(_issue("warning", t("val.header_changed"), field="header"))
    return issues
