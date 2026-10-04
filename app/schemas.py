"""Pydantic-схемы данных декларации.

Эти же модели используются:
  * для JSON Schema структурированного ответа LLM (output_config.format) —
    поэтому у полей нет значений по умолчанию: модель обязана вернуть каждый
    ключ явно, а неизвестное значение — как null;
  * для валидации данных, которые декларант утверждает в Diff View;
  * как контекст при заполнении шаблона экспорта.

Описания полей (description) попадают в JSON Schema и служат подсказкой для LLM.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Party(_Strict):
    name: str | None = Field(description="Наименование организации точно как в документе")
    address: str | None = Field(description="Адрес как в документе")
    country: str | None = Field(description="Страна (как в документе: название или код)")
    tax_id: str | None = Field(description="ИНН / TIN / EORI / иной идентификатор, если указан")


class DeclarationHeader(_Strict):
    """Постоянная часть: переносится из эталона без изменений."""

    declaration_type: str | None = Field(description="Тип / режим декларации, например 'ИМ 40'")
    customs_office: str | None = Field(description="Таможенный пост (код и/или название)")
    exporter: Party = Field(description="Отправитель / экспортёр (графа 2)")
    importer: Party = Field(description="Получатель / импортёр (графа 8)")
    declarant: Party = Field(description="Декларант / таможенный брокер (графа 14)")
    contract_number: str | None = Field(description="Номер внешнеторгового контракта")
    contract_date: str | None = Field(description="Дата контракта как в документе")
    delivery_terms: str | None = Field(description="Код Инкотермс, например 'DAP'")
    delivery_place: str | None = Field(description="Пункт поставки по Инкотермс")
    currency: str | None = Field(description="Валюта контракта/инвойса, код ISO (EUR, USD...)")
    country_of_dispatch: str | None = Field(description="Страна отправления")
    country_of_destination: str | None = Field(description="Страна назначения")
    transport_mode: str | None = Field(description="Вид транспорта на границе (код или название)")


class ShipmentData(_Strict):
    """Данные конкретной поставки: меняются от декларации к декларации."""

    invoice_numbers: list[str] = Field(description="Номера всех инвойсов поставки")
    invoice_date: str | None = Field(description="Дата инвойса как в документе")
    transport_document: str | None = Field(description="Номер CMR / AWB / накладной, если указан")
    vehicle_id: str | None = Field(description="Номер транспортного средства, если указан")
    total_invoice_value: float | None = Field(description="Итоговая сумма, только если явно напечатана")
    total_packages: int | None = Field(description="Общее количество мест, только если явно указано")
    total_gross_weight_kg: float | None = Field(description="Вес брутто итого, кг, только если явно указан")
    total_net_weight_kg: float | None = Field(description="Вес нетто итого, кг, только если явно указан")


class GoodsItem(_Strict):
    item_no: int = Field(description="Порядковый номер позиции, начиная с 1")
    description: str | None = Field(description="Описание товара")
    article: str | None = Field(description="Артикул / каталожный номер, если указан")
    hs_code: str | None = Field(description="Код ТН ВЭД, только цифры; null, если не найден")
    hs_code_basis: Literal["document", "reference_match", "not_found"] = Field(
        description="Откуда взят код: из нового документа / из совпадающей позиции эталона / не найден"
    )
    reference_item_no: int | None = Field(description="Номер соответствующей позиции эталона или null")
    country_of_origin: str | None = Field(description="Страна происхождения")
    quantity: float | None = Field(description="Количество")
    unit: str | None = Field(description="Единица измерения количества")
    packages: int | None = Field(description="Количество мест по этой позиции")
    gross_weight_kg: float | None = Field(description="Вес брутто, кг")
    net_weight_kg: float | None = Field(description="Вес нетто, кг")
    unit_price: float | None = Field(description="Цена за единицу")
    total_value: float | None = Field(description="Стоимость позиции из колонки суммы документа")
    source_document: str | None = Field(description="Имя документа-источника (атрибут name)")
    source_quote: str | None = Field(description="Дословный фрагмент строки документа, до 200 символов")


class DeclarationData(_Strict):
    header: DeclarationHeader
    shipment: ShipmentData
    items: list[GoodsItem]


Severity = Literal["error", "warning", "info"]


class LLMIssue(_Strict):
    severity: Severity = Field(description="error — требует исправления; warning — проверить; info — к сведению")
    item_no: int | None = Field(description="Номер новой товарной позиции, к которой относится замечание, или null")
    field: str | None = Field(
        description="Поле: для позиции — имя поля (hs_code, net_weight_kg...); "
        "иначе путь вида header.importer.name или shipment.total_gross_weight_kg; или null"
    )
    message: str = Field(description="Короткое пояснение на русском с конкретными значениями")


class LLMExtractionResult(_Strict):
    """Структура ответа LLM (передаётся в API как JSON Schema)."""

    reference: DeclarationData = Field(description="Эталонная декларация, извлечённая как есть")
    new_shipment: ShipmentData = Field(description="Данные новой поставки из коммерческих документов")
    new_items: list[GoodsItem] = Field(description="Новые товарные позиции, по одной на строку инвойса")
    issues: list[LLMIssue] = Field(description="Пропуски, расхождения и неоднозначности")


class Issue(BaseModel):
    """Замечание, которое видит декларант (от LLM или от детерминированного валидатора)."""

    severity: Severity
    item_no: int | None = None
    field: str | None = None
    message: str
    source: Literal["llm", "validator"] = "validator"


# ---------- Запросы и ответы API ----------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    invite_code: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str | None


class ApproveRequest(BaseModel):
    data: DeclarationData
