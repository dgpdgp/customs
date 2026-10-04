"""Экспорт в «родной» XML ASYCUDA World — в формат файлов, которые ASYCUDA выгружает сама.

Зачем отдельный модуль. Обычный экспорт (exporter.py) подставляет данные в плейсхолдеры {{ … }}.
В файле, выгруженном из ASYCUDA, плейсхолдеров нет, поэтому раньше такой файл возвращался
без изменений и новые позиции в него не попадали. Здесь файл ASYCUDA служит основой:

  * всё, что вне <Item> (таможня, процедура, участники, транспорт, склад, банк…), остаётся
    как в основе — это постоянная часть декларации, как и задумано;
  * пустые поля шапки (если основа — чистый бланк) заполняются из утверждённой шапки,
    заполненные — не трогаются;
  * блоки <Item> строятся заново по утверждённым позициям. Образец — позиция эталона, которой
    соответствует новая (reference_item_no), иначе первая позиция основы: из образца берутся
    процедура, вид упаковки, метод оценки и прочие поля, которых нет в инвойсах;
  * итоги (позиции, места, вес, стоимость, листы) пересчитываются;
  * в прилагаемых документах номера инвойса и транспортного документа эталона заменяются
    на новые (только при точном совпадении номера).

Чего модуль не делает, и это проверяет декларант в ASYCUDA: налоги (их считает ASYCUDA),
курс валюты (по умолчанию — из основы; актуальный можно указать при скачивании), блок
предыдущей декларации <Prev_decl> и прочие документы — они копируются из основы как есть.
"""

import copy
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

import pycountry
from defusedxml import ElementTree as SafeET

from app.i18n import t
from app.schemas import DeclarationData, GoodsItem, Party

ROOT_TAG = "ASYCUDA"
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
HS_MAIN_DIGITS = 8  # Commodity_code — 8 цифр, остальное — Precision_1 (так в выгрузке ASYCUDA)
ITEMS_ON_FIRST_FORM, ITEMS_PER_EXTRA_FORM = 1, 3  # бланк ЕАД: 1 позиция на основном листе, 3 — на добавочных

# Затраты из ведомости таможенной стоимости: (строка позиции, строка всей декларации, знак)
COST_LINES = (
    ("item_external_freight", "Gs_external_freight", 1),
    ("item_internal_freight", "Gs_internal_freight", 1),
    ("item_insurance", "Gs_insurance", 1),
    ("item_other_cost", "Gs_other_cost", 1),
    ("item_deduction", "Gs_deduction", -1),
)


class AsycudaError(Exception):
    pass


@dataclass
class Base:
    path: Path
    is_reference: bool  # основа — сам файл эталона (тогда reference_item_no указывает на его позиции)


@dataclass
class _Context:
    root: ET.Element
    rate: Decimal | None
    total_foreign: Decimal | None
    countries: dict[str, tuple[str, str]]
    notes: list[str] = field(default_factory=list)


# ---------- Определение формата и выбор основы ----------

def _load(path: Path) -> ET.Element:
    try:
        return SafeET.fromstring(path.read_bytes())
    except Exception as exc:  # битый XML, XXE и т. п. — defusedxml бросает разные исключения
        raise AsycudaError(t("asy.bad_xml", error=exc)) from exc


def is_asycuda_file(path: Path | None) -> bool:
    """XML, выгруженный из ASYCUDA: корень <ASYCUDA> и нет разметки Jinja2 (иначе это шаблон)."""
    if path is None or path.suffix.lower() != ".xml" or not path.is_file():
        return False
    raw = path.read_bytes()
    if b"{{" in raw or b"{%" in raw:
        return False
    try:
        return _load(path).tag == ROOT_TAG
    except AsycudaError:
        return False


def _has_goods(path: Path) -> bool:
    return any(_text(item.find("Tarification/HScode/Commodity_code")) for item in _load(path).findall("Item"))


def choose_base(template: Path | None, references: list[Path]) -> Base | None:
    """Какой файл ASYCUDA взять за основу. None — экспорт обычным способом (шаблон Jinja2 / Excel)."""
    reference = next((p for p in references if is_asycuda_file(p)), None)
    if template is not None:
        if not is_asycuda_file(template):
            return None
        # Чистый бланк из ASYCUDA означает «нужен формат ASYCUDA»: данные шапки тогда берём из эталона
        if reference is not None and not _has_goods(template):
            return Base(reference, is_reference=True)
        return Base(template, is_reference=False)
    return Base(reference, is_reference=True) if reference is not None else None


def base_rate(base: Base) -> str:
    """Курс из основы — для подсказки на странице скачивания."""
    return _text(_load(base.path).find("Valuation/Gs_Invoice/Currency_rate"))


# ---------- Элементарные операции с узлами ----------

def _text(el: ET.Element | None) -> str:
    if el is None or any(child.tag == "null" for child in el):
        return ""
    return (el.text or "").strip()


def _set(el: ET.Element | None, value, *, numeric: bool = False) -> None:
    """Записывает значение. Пусто: строковые поля ASYCUDA пишет как <tag><null/></tag>, числовые — <tag/>."""
    if el is None:
        return
    if value in (None, "") and not _text(el):
        return  # и так пусто — сохраняем форму основы (<tag/> или <tag><null/></tag>)
    if numeric and value not in (None, "") and not list(el):
        current = _dec((el.text or "").strip())
        if current is not None and current == _dec(value):
            return  # число не изменилось — оставляем запись как в основе («0.0», а не «0»)
    for child in list(el):
        el.remove(child)
    if value is None or value == "":
        if numeric:
            el.text = None
        else:
            el.text = "\n"
            ET.SubElement(el, "null").tail = "\n"
        return
    el.text = _fmt(value) if isinstance(value, Decimal | int | float) else str(value)


def _set_path(parent: ET.Element, path: str, value, *, numeric: bool = False) -> None:
    _set(parent.find(path), value, numeric=numeric)


def _fill_if_empty(parent: ET.Element, path: str, value) -> None:
    el = parent.find(path)
    if el is not None and value not in (None, "") and not _text(el):
        _set(el, value)


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None


def _fmt(value: Decimal | int | float) -> str:
    """Число без экспоненты и лишних нулей: 19800.0 -> 19800, 20869.20 -> 20869.2."""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _sum(values: list) -> Decimal | None:
    """Сумма, только если известны все слагаемые (иначе итог был бы занижен)."""
    decimals = [_dec(v) for v in values]
    if not decimals or any(d is None for d in decimals):
        return None
    return sum(decimals, Decimal(0))


# ---------- Справочники ----------

def _country_index(root: ET.Element) -> dict[str, tuple[str, str]]:
    """Страны, уже встречающиеся в основе: «ბრაზილია» / «brazil» / «br» / «76» -> ("76", "BR")."""
    index: dict[str, tuple[str, str]] = {}
    pairs = [
        ("General_information/Country/Export/Export_country_code", "General_information/Country/Export/"
         "Export_country_region", "General_information/Country/Export/Export_country_name"),
        ("General_information/Country/Destination/Destination_country_code", "General_information/Country/"
         "Destination/Destination_country_region", "General_information/Country/Destination/Destination_country_name"),
    ]
    for code_path, region_path, name_path in pairs:
        code, region, name = (_text(root.find(p)) for p in (code_path, region_path, name_path))
        if code and region:
            for key in [code, region, *name.split("/")]:
                if key.strip():
                    index[key.strip().casefold()] = (code, region)
    for item in root.findall("Item"):
        code = _text(item.find("Goods_description/Country_of_origin_code"))
        region = _text(item.find("Goods_description/Country_of_origin_region"))
        if code and region:
            index.setdefault(code.casefold(), (code, region))
            index.setdefault(region.casefold(), (code, region))
    return index


def _country(value: str | None, index: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """(числовой код ISO 3166 без ведущих нулей, двухбуквенный код) — как в выгрузке ASYCUDA (76, BR)."""
    value = (value or "").strip()
    if not value:
        return None
    if value.casefold() in index:
        return index[value.casefold()]
    try:
        country = (pycountry.countries.get(numeric=value.zfill(3)) if value.isdigit()
                   else pycountry.countries.lookup(value))
    except LookupError:
        country = None
    if country is None:
        return None
    return str(int(country.numeric)), country.alpha_2


def _currency_numeric(code: str | None) -> str | None:
    code = (code or "").strip()
    if not code:
        return None
    if code.isdigit():
        return str(int(code))
    currency = pycountry.currencies.get(alpha_3=code.upper())
    return str(int(currency.numeric)) if currency is not None else None


_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
                 "%B %d %Y", "%b %d %Y")


def _parse_date(value: str | None) -> date | None:
    """Только однозначные форматы: 2026-05-21, 21.05.2026, 21 May 2026. «05/06/2026» не угадываем."""
    value = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _asycuda_date(value: date) -> str:
    return f"{value.month}/{value.day}/{value:%y}"  # как в выгрузке ASYCUDA: 5/21/26


def _party_text(party: Party) -> str | None:
    """В ASYCUDA наименование и адрес — одно многострочное поле."""
    lines = [line for line in (party.name, party.address) if line]
    return "\n".join(lines) or None


# ---------- Шапка ----------

def _fill_header(ctx: _Context, header) -> None:
    """Дозаполняет пустые поля шапки (основа — чистый бланк). Заполненные поля основы не меняются."""
    root = ctx.root
    _fill_if_empty(root, "Traders/Exporter/Exporter_name", _party_text(header.exporter))
    _fill_if_empty(root, "Traders/Consignee/Consignee_code", header.importer.tax_id)
    _fill_if_empty(root, "Traders/Consignee/Consignee_name", _party_text(header.importer))
    _fill_if_empty(root, "Declarant/Declarant_code", header.declarant.tax_id)
    _fill_if_empty(root, "Declarant/Declarant_name", _party_text(header.declarant))
    _fill_if_empty(root, "Transport/Delivery_terms/Code", header.delivery_terms)
    _fill_if_empty(root, "Transport/Delivery_terms/Place", header.delivery_place)
    for prefix, value in (("Export/Export_country", header.country_of_dispatch),
                          ("Destination/Destination_country", header.country_of_destination)):
        codes = _country(value, ctx.countries)
        if codes:
            _fill_if_empty(root, f"General_information/Country/{prefix}_code", codes[0])
            _fill_if_empty(root, f"General_information/Country/{prefix}_region", codes[1])
            _fill_if_empty(root, f"General_information/Country/{prefix}_name", value)


# ---------- Позиции ----------

def _fill_hs(item: ET.Element, goods: GoodsItem) -> bool:
    """Код товара. Возвращает True, если тарифная строка та же, что в образце."""
    proto_code = _text(item.find("Tarification/HScode/Commodity_code"))
    proto_precision = _text(item.find("Tarification/HScode/Precision_1"))
    digits = re.sub(r"\D", "", goods.hs_code or "")
    if len(digits) > HS_MAIN_DIGITS:
        code, precision = digits[:HS_MAIN_DIGITS], digits[HS_MAIN_DIGITS:]
    else:
        # 8 цифр: уточнение известно, только если это та же строка тарифа, что в образце
        code, precision = digits, (proto_precision if digits and digits == proto_code else "")
    _set_path(item, "Tarification/HScode/Commodity_code", code)
    _set_path(item, "Tarification/HScode/Precision_1", precision)
    return bool(code) and code == proto_code and precision == proto_precision


def _fill_supplementary_units(item: ET.Element, goods: GoodsItem, same_tariff: bool) -> None:
    for index, unit in enumerate(item.findall("Tarification/Supplementary_unit")):
        if not same_tariff:
            # Доп. единица зависит от кода товара: для другого кода её укажет ASYCUDA / декларант
            _set_path(unit, "Suppplementary_unit_code", None)
            _set_path(unit, "Suppplementary_unit_name", None)
            _set_path(unit, "Suppplementary_unit_quantity", None, numeric=True)
        elif index == 0 and _text(unit.find("Suppplementary_unit_code")):
            _set_path(unit, "Suppplementary_unit_quantity", goods.quantity, numeric=True)


def _fill_valuation(ctx: _Context, item: ET.Element, goods: GoodsItem) -> None:
    valuation = item.find("Valuation_item")
    if valuation is None:
        return
    _set_path(valuation, "Weight_itm/Gross_weight_itm", goods.gross_weight_kg, numeric=True)
    _set_path(valuation, "Weight_itm/Net_weight_itm", goods.net_weight_kg, numeric=True)

    foreign = _dec(goods.total_value)
    national = foreign * ctx.rate if foreign is not None and ctx.rate is not None else None
    invoice = valuation.find("Item_Invoice")
    if invoice is not None:
        _set_path(invoice, "Amount_foreign_currency", foreign, numeric=True)
        _set_path(invoice, "Amount_national_currency", national, numeric=True)
        currency = _text(ctx.root.find("Valuation/Gs_Invoice/Currency_code"))
        if currency:
            _set_path(invoice, "Currency_code", currency)
        _set_path(invoice, "Currency_rate", ctx.rate, numeric=True)

    # Доля позиции в стоимости — по ней распределяются общие затраты ведомости (фрахт, страховка…)
    alpha = foreign / ctx.total_foreign if foreign is not None and ctx.total_foreign else None
    total_cost = Decimal(0)
    for line, global_line, sign in COST_LINES:
        global_national = _dec(_text(ctx.root.find(f"Valuation/{global_line}/Amount_national_currency"))) or 0
        global_foreign = _dec(_text(ctx.root.find(f"Valuation/{global_line}/Amount_foreign_currency"))) or 0
        current = _dec(_text(valuation.find(f"{line}/Amount_national_currency")))
        if global_national == 0 and not current:
            continue  # затрат нет — строка остаётся как в образце
        share = (global_national * alpha) if alpha is not None else None
        _set_path(valuation, f"{line}/Amount_national_currency", share, numeric=True)
        _set_path(valuation, f"{line}/Amount_foreign_currency",
                  global_foreign * alpha if alpha is not None else None, numeric=True)
        total_cost = total_cost + sign * share if share is not None and total_cost is not None else None

    cif = national + total_cost if national is not None and total_cost is not None else None
    _set_path(valuation, "Total_cost_itm", total_cost, numeric=True)
    _set_path(valuation, "Total_CIF_itm", cif, numeric=True)
    _set_path(valuation, "Statistical_value", cif, numeric=True)
    _set_path(valuation, "Alpha_coeficient_of_apportionment",
              alpha.quantize(Decimal("0.000001")) if alpha is not None else None, numeric=True)


def _build_item(ctx: _Context, prototype: ET.Element, goods: GoodsItem) -> ET.Element:
    item = copy.deepcopy(prototype)
    n = goods.item_no
    _set_path(item, "Packages/Number_of_packages", goods.packages, numeric=True)
    same_tariff = _fill_hs(item, goods)
    _fill_supplementary_units(item, goods, same_tariff)

    description = goods.description or ""
    if goods.article and goods.article not in description:
        description = f"{description} {goods.article}".strip()
    _set_path(item, "Goods_description/Description_of_goods", description)

    codes = _country(goods.country_of_origin, ctx.countries)
    _set_path(item, "Goods_description/Country_of_origin_code", codes[0] if codes else None)
    _set_path(item, "Goods_description/Country_of_origin_region", codes[1] if codes else None)
    if goods.country_of_origin and not codes:
        ctx.notes.append(t("asy.note.country", n=n, value=goods.country_of_origin))

    _fill_valuation(ctx, item, goods)
    return item


def _replace_documents(item: ET.Element, reference: DeclarationData | None, data: DeclarationData) -> None:
    """Прилагаемые документы: номер инвойса и транспортного документа эталона -> новые.
    Заменяется только точное совпадение номера; остальные документы остаются как в основе."""
    if reference is None:
        return

    def norm(value: str | None) -> str:
        return (value or "").strip().casefold()

    old_invoices = {norm(n) for n in reference.shipment.invoice_numbers if norm(n)}
    old_transport = norm(reference.shipment.transport_document)
    new_invoices = [n for n in data.shipment.invoice_numbers if n.strip()]
    new_date = _parse_date(data.shipment.invoice_date)

    documents = item.findall("Attached_documents")
    for doc in documents:
        if old_transport and norm(_text(doc.find("Attached_document_reference"))) == old_transport:
            _set_path(doc, "Attached_document_reference", data.shipment.transport_document)
    if not new_invoices:
        return
    # Номер инвойса встречается у разных документов (инвойс, упаковочный лист) — каждый вид отдельно:
    # документов этого вида становится столько, сколько новых инвойсов
    groups: dict[str, list[ET.Element]] = {}
    for doc in documents:
        if norm(_text(doc.find("Attached_document_reference"))) in old_invoices:
            groups.setdefault(_text(doc.find("Attached_document_code")), []).append(doc)
    for group in groups.values():
        for extra in group[len(new_invoices):]:
            item.remove(extra)
        del group[len(new_invoices):]
        while len(group) < len(new_invoices):
            clone = copy.deepcopy(group[-1])
            item.insert(list(item).index(group[-1]) + 1, clone)
            group.append(clone)
        for doc, number in zip(group, new_invoices, strict=True):
            _set_path(doc, "Attached_document_reference", number)
            date_el = doc.find("Attached_document_date")
            if date_el is not None:
                _set(date_el, _asycuda_date(new_date) if new_date else None, numeric=True)


# ---------- Итоги ----------

def _fill_totals(ctx: _Context, data: DeclarationData, items: list[ET.Element]) -> None:
    root, goods = ctx.root, data.items
    count = len(goods)
    _set_path(root, "Property/Nbers/Total_number_of_items", count, numeric=True)
    extra_forms = math.ceil(max(count - ITEMS_ON_FIRST_FORM, 0) / ITEMS_PER_EXTRA_FORM)
    _set_path(root, "Property/Forms/Total_number_of_forms", 1 + extra_forms, numeric=True)

    packages = _sum([g.packages for g in goods])
    if packages is None:
        packages = _dec(data.shipment.total_packages)
    _set_path(root, "Property/Nbers/Total_number_of_packages", packages, numeric=True)

    gross = _sum([g.gross_weight_kg for g in goods])
    if gross is None:
        gross = _dec(data.shipment.total_gross_weight_kg)
    _set_path(root, "Valuation/Weight/Gross_weight", gross, numeric=True)
    _set_path(root, "Valuation/Total/Total_weight", gross, numeric=True)

    foreign = ctx.total_foreign
    _set_path(root, "Valuation/Total/Total_invoice", foreign, numeric=True)
    _set_path(root, "Valuation/Gs_Invoice/Amount_foreign_currency", foreign, numeric=True)
    _set_path(root, "Valuation/Gs_Invoice/Amount_national_currency",
              foreign * ctx.rate if foreign is not None and ctx.rate is not None else None, numeric=True)
    _set_path(root, "Valuation/Gs_Invoice/Currency_rate", ctx.rate, numeric=True)
    cif = _sum([_text(item.find("Valuation_item/Total_CIF_itm")) for item in items])
    _set_path(root, "Valuation/Total_CIF", cif, numeric=True)


# ---------- Сборка ----------

def _serialize(root: ET.Element) -> bytes:
    """Как выгружает ASYCUDA: объявление XML, без отступов, <tag/> без пробела, переводы строк CRLF."""
    root.tail = None
    body = ET.tostring(root, encoding="unicode").replace(" />", "/>")
    text = f"{XML_DECLARATION}\n{body}\n"
    return text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def render(base: Base, data: DeclarationData, *, reference: DeclarationData | None = None,
           exchange_rate: Decimal | None = None) -> tuple[bytes, list[str]]:
    """Возвращает (содержимое XML, замечания для декларанта)."""
    root = _load(base.path)
    if root.tag != ROOT_TAG:
        raise AsycudaError(t("asy.not_asycuda"))
    prototypes = root.findall("Item")
    if not prototypes:
        raise AsycudaError(t("asy.no_item"))
    if not data.items:
        raise AsycudaError(t("asy.no_goods"))

    rate = exchange_rate or _dec(_text(root.find("Valuation/Gs_Invoice/Currency_rate")))
    ctx = _Context(root=root, rate=rate if rate else None, total_foreign=None, countries=_country_index(root))
    total = _sum([g.total_value for g in data.items])
    ctx.total_foreign = total if total is not None else _dec(data.shipment.total_invoice_value)

    currency = _currency_numeric(data.header.currency)
    base_currency = _text(root.find("Valuation/Gs_Invoice/Currency_code"))
    if currency and not base_currency:
        _set_path(root, "Valuation/Gs_Invoice/Currency_code", currency)
    elif currency and base_currency and currency != base_currency:
        ctx.notes.append(t("asy.note.currency", new=data.header.currency, base=base_currency))
    if ctx.rate is None:
        ctx.notes.append(t("asy.note.no_rate"))

    _fill_header(ctx, data.header)

    position = list(root).index(prototypes[0])
    for prototype in prototypes:
        root.remove(prototype)
    items = []
    for goods in data.items:
        index = (goods.reference_item_no or 0) - 1
        prototype = prototypes[index] if base.is_reference and 0 <= index < len(prototypes) else prototypes[0]
        item = _build_item(ctx, prototype, goods)
        _replace_documents(item, reference, data)
        items.append(item)
    for offset, item in enumerate(items):
        root.insert(position + offset, item)

    _fill_totals(ctx, data, items)
    if root.find("Prev_decl") is not None:
        ctx.notes.append(t("asy.note.prev_decl"))
    return _serialize(root), ctx.notes


def parse_rate(raw: str | None) -> Decimal | None:
    """Курс из формы скачивания: «2,6266» и «2.6266» — одно и то же; пусто — курс из основы."""
    if raw is None or not raw.strip():
        return None
    rate = _dec(raw.strip())
    if rate is None or not rate.is_finite() or rate <= 0 or rate >= 100000:
        raise AsycudaError(t("asy.bad_rate"))
    return rate
