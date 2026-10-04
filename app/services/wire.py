"""Формат ответа LLM («проводной» формат) и его перевод во внутренние модели.

Почему отдельные модели, а не DeclarationData из app/schemas.py.
Структурированный вывод Claude компилирует JSON Schema в грамматику и ограничивает
её сложность: не больше 16 полей с объединением типов (anyOf, ["string", "null"])
и не больше 24 необязательных полей на запрос. Во внутренних моделях почти каждое
поле может быть пустым (None), то есть было бы объединением типов, — такую схему API
отклоняет с ошибкой «compiled grammar is too large».

Поэтому в проводном формате:
  * все поля обязательные и без объединений типов;
  * неизвестное значение — пустая строка "", для номеров позиций — 0;
  * числа передаются строками и переводятся в float кодом (с проверкой формата).

Модели терпимы к отклонениям (null вместо "", число вместо строки): это нужно
для запасного режима без строгой схемы, где формат держится только промптом.
Тест tests/test_llm.py::test_output_schema_fits_structured_output_limits следит,
чтобы схема не стала снова слишком сложной.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.schemas import (
    DeclarationData,
    DeclarationHeader,
    GoodsItem,
    LLMExtractionResult,
    LLMIssue,
    Party,
    ShipmentData,
)

HS_BASIS = ("document", "reference_match", "not_found")
SEVERITIES = ("error", "warning", "info")


# ---------- Терпимое приведение типов ----------

def _as_text(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    return value


def _as_int(value: Any) -> Any:
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        try:
            return int(float(value.strip().replace(",", ".")))
        except ValueError:
            return 0
    return value


def _as_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return value


def _choice(options: tuple[str, ...], default: str):
    return lambda value: value if value in options else default


Text = Annotated[str, BeforeValidator(_as_text)]
Number = Annotated[str, BeforeValidator(_as_text), Field(description="Число строкой, точка — десятичный разделитель; "
                                                                     "пустая строка, если значения нет")]
Count = Annotated[int, BeforeValidator(_as_int)]
TextList = Annotated[list[Text], BeforeValidator(_as_list)]


class _Wire(BaseModel):
    # Лишние ключи игнорируем: в запасном режиме модель может добавить что-то своё.
    model_config = ConfigDict(extra="ignore")


class WireParty(_Wire):
    name: Text = Field(description="Наименование точно как в документе")
    address: Text
    country: Text
    tax_id: Text = Field(description="ИНН / TIN / EORI, если указан")


class WireHeader(_Wire):
    declaration_type: Text = Field(description="Тип / режим декларации, например 'ИМ 40'")
    customs_office: Text
    exporter: WireParty
    importer: WireParty
    declarant: WireParty = Field(description="Декларант / таможенный брокер")
    contract_number: Text
    contract_date: Text
    delivery_terms: Text = Field(description="Код Инкотермс, например 'DAP'")
    delivery_place: Text
    currency: Text = Field(description="Код валюты ISO")
    country_of_dispatch: Text
    country_of_destination: Text
    transport_mode: Text


class WireShipment(_Wire):
    invoice_numbers: TextList
    invoice_date: Text
    transport_document: Text
    vehicle_id: Text
    total_invoice_value: Number
    total_packages: Number
    total_gross_weight_kg: Number
    total_net_weight_kg: Number


class WireReferenceItem(_Wire):
    item_no: Count = Field(description="Номер позиции эталона")
    description: Text
    article: Text
    hs_code: Text = Field(description="Код ТН ВЭД, только цифры")
    country_of_origin: Text
    quantity: Number
    unit: Text
    packages: Number
    gross_weight_kg: Number
    net_weight_kg: Number
    unit_price: Number
    total_value: Number


class WireNewItem(WireReferenceItem):
    item_no: Count = Field(description="Порядковый номер новой позиции, начиная с 1")
    hs_code_basis: Annotated[
        Literal["document", "reference_match", "not_found"],
        BeforeValidator(_choice(HS_BASIS, "not_found")),
    ] = Field(description="Откуда код: из нового документа / из совпадающей позиции эталона / не найден")
    reference_item_no: Count = Field(description="Номер соответствующей позиции эталона, 0 — нет соответствия")
    source_document: Text = Field(description="Имя документа-источника (атрибут name)")
    source_quote: Text = Field(description="Дословный фрагмент строки документа, до 200 символов")


class WireReference(_Wire):
    header: WireHeader
    shipment: WireShipment
    items: list[WireReferenceItem]


class WireIssue(_Wire):
    severity: Annotated[Literal["error", "warning", "info"], BeforeValidator(_choice(SEVERITIES, "warning"))]
    item_no: Count = Field(description="Номер новой позиции, 0 — замечание не к позиции")
    field: Text = Field(description="Имя поля позиции или путь вида header.importer.name; пусто, если не к полю")
    message: Text


class WireResult(_Wire):
    reference: WireReference = Field(description="Эталонная декларация, извлечённая как есть")
    new_shipment: WireShipment = Field(description="Данные новой поставки из коммерческих документов")
    new_items: list[WireNewItem] = Field(description="Новые товарные позиции, по одной на строку инвойса")
    issues: list[WireIssue] = Field(description="Пропуски, расхождения и неоднозначности")


# ---------- Перевод во внутренние модели ----------

def parse_number(text: str) -> tuple[float | None, bool]:
    """«2760», «2 760,50», «1,234.5» -> float. Возвращает (значение, удалось_ли)."""
    raw = text.strip().replace(" ", "").replace(" ", "")
    if not raw:
        return None, True
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
        raw = raw.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in raw:
        raw = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    try:
        return float(raw), True
    except ValueError:
        return None, False


class _Converter:
    def __init__(self) -> None:
        self.issues: list[LLMIssue] = []

    def text(self, value: str) -> str | None:
        value = value.strip()
        return value or None

    def number(self, value: str, where: str, item_no: int | None, field: str) -> float | None:
        result, ok = parse_number(value)
        if not ok:
            self.issues.append(LLMIssue(severity="warning", item_no=item_no, field=field,
                                        message=f"Не удалось распознать число «{value}» ({where})"))
        return result

    def integer(self, value: str, where: str, item_no: int | None, field: str) -> int | None:
        result = self.number(value, where, item_no, field)
        if result is None:
            return None
        if not result.is_integer():
            self.issues.append(LLMIssue(severity="warning", item_no=item_no, field=field,
                                        message=f"Ожидалось целое число, получено «{value}» ({where})"))
            return None
        return int(result)

    def party(self, p: WireParty) -> Party:
        return Party(name=self.text(p.name), address=self.text(p.address),
                     country=self.text(p.country), tax_id=self.text(p.tax_id))

    def header(self, h: WireHeader) -> DeclarationHeader:
        return DeclarationHeader(
            declaration_type=self.text(h.declaration_type), customs_office=self.text(h.customs_office),
            exporter=self.party(h.exporter), importer=self.party(h.importer), declarant=self.party(h.declarant),
            contract_number=self.text(h.contract_number), contract_date=self.text(h.contract_date),
            delivery_terms=self.text(h.delivery_terms), delivery_place=self.text(h.delivery_place),
            currency=self.text(h.currency), country_of_dispatch=self.text(h.country_of_dispatch),
            country_of_destination=self.text(h.country_of_destination), transport_mode=self.text(h.transport_mode),
        )

    def shipment(self, s: WireShipment, prefix: str) -> ShipmentData:
        def num(field: str) -> float | None:
            return self.number(getattr(s, field), prefix, None, f"{prefix}.{field}")

        return ShipmentData(
            invoice_numbers=[n.strip() for n in s.invoice_numbers if n.strip()],
            invoice_date=self.text(s.invoice_date),
            transport_document=self.text(s.transport_document),
            vehicle_id=self.text(s.vehicle_id),
            total_invoice_value=num("total_invoice_value"),
            total_packages=self.integer(s.total_packages, prefix, None, f"{prefix}.total_packages"),
            total_gross_weight_kg=num("total_gross_weight_kg"),
            total_net_weight_kg=num("total_net_weight_kg"),
        )

    def item(self, it: WireReferenceItem, *, reference: bool) -> GoodsItem:
        # Замечания к позициям эталона не привязываем к номеру: item_no в замечаниях — номер новой позиции.
        issue_no = None if reference else it.item_no
        where = f"позиция эталона №{it.item_no}" if reference else f"позиция №{it.item_no}"

        def num(field: str) -> float | None:
            field_ref = f"reference.items.{it.item_no}.{field}" if reference else field
            return self.number(getattr(it, field), where, issue_no, field_ref)

        extra: dict[str, Any] = {"hs_code_basis": "document", "reference_item_no": None,
                                 "source_document": None, "source_quote": None}
        if isinstance(it, WireNewItem):
            extra = {
                "hs_code_basis": it.hs_code_basis,
                "reference_item_no": it.reference_item_no or None,
                "source_document": self.text(it.source_document),
                "source_quote": self.text(it.source_quote),
            }
        return GoodsItem(
            item_no=it.item_no,
            description=self.text(it.description),
            article=self.text(it.article),
            hs_code=self.text(it.hs_code),
            country_of_origin=self.text(it.country_of_origin),
            quantity=num("quantity"),
            unit=self.text(it.unit),
            packages=self.integer(it.packages, where, issue_no,
                                  f"reference.items.{it.item_no}.packages" if reference else "packages"),
            gross_weight_kg=num("gross_weight_kg"),
            net_weight_kg=num("net_weight_kg"),
            unit_price=num("unit_price"),
            total_value=num("total_value"),
            **extra,
        )


def wire_to_result(wire: WireResult) -> LLMExtractionResult:
    """Проводной формат -> внутренние модели. Нераспознанные числа становятся
    пустыми значениями и попадают в замечания, а не теряются молча."""
    c = _Converter()
    reference = DeclarationData(
        header=c.header(wire.reference.header),
        shipment=c.shipment(wire.reference.shipment, "reference.shipment"),
        items=[c.item(it, reference=True) for it in wire.reference.items],
    )
    new_shipment = c.shipment(wire.new_shipment, "shipment")
    new_items = [c.item(it, reference=False) for it in wire.new_items]
    issues = [
        LLMIssue(severity=i.severity, item_no=i.item_no or None, field=c.text(i.field), message=i.message)
        for i in wire.issues
        if i.message.strip()
    ]
    return LLMExtractionResult(reference=reference, new_shipment=new_shipment, new_items=new_items,
                               issues=issues + c.issues)
