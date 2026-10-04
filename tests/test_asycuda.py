"""Экспорт в родной XML ASYCUDA World: файл из ASYCUDA — основа, позиции и итоги — новые."""

from decimal import Decimal
from pathlib import Path

import pytest
from defusedxml import ElementTree

from app.schemas import DeclarationData
from app.services import asycuda
from tests.conftest import SAMPLES

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE = FIXTURES / "asycuda_reference.xml"  # заполненная декларация, выгруженная из ASYCUDA
BLANK = FIXTURES / "asycuda_blank.xml"  # чистый бланк из ASYCUDA (заполнен только декларант)


def _party(name, address=None, tax_id=None):
    return {"name": name, "address": address, "country": None, "tax_id": tax_id}


def _item(no, **fields):
    item = {"item_no": no, "description": "Tobacco leaves", "article": None, "hs_code": "24012035000",
            "hs_code_basis": "reference_match", "reference_item_no": 1, "country_of_origin": "BR", "quantity": 500,
            "unit": "kg", "packages": 5, "gross_weight_kg": 550.0, "net_weight_kg": 500.0, "unit_price": 6.0,
            "total_value": 3000.0, "source_document": None, "source_quote": None}
    item.update(fields)
    return item


def _data(items, **shipment):
    data = {
        "header": {"declaration_type": None, "customs_office": None, "exporter": _party("NEW EXPORTER", "Rua 1"),
                   "importer": _party("შპს ახალი იმპორტი", "თბილისი", "200000099"),
                   "declarant": _party("Other declarant", None, "400000099"), "contract_number": None,
                   "contract_date": None, "delivery_terms": "CIF", "delivery_place": "ფოთი", "currency": "USD",
                   "country_of_dispatch": "BR", "country_of_destination": "GE", "transport_mode": None},
        "shipment": {"invoice_numbers": ["INV-NEW-7"], "invoice_date": "21.09.2026", "transport_document": "BL-NEW-7",
                     "vehicle_id": None, "total_invoice_value": None, "total_packages": None,
                     "total_gross_weight_kg": None, "total_net_weight_kg": None},
        "items": items,
    }
    data["shipment"].update(shipment)
    return DeclarationData.model_validate(data)


def _reference():
    """Эталон, как его прочитала LLM: номера инвойса и коносамента совпадают с графой 44 файла."""
    return _data([_item(1)], invoice_numbers=["INV-OLD-1"], transport_document="BL-OLD-1")


def _render(base_path, data, **kwargs):
    content, notes = asycuda.render(asycuda.Base(base_path, is_reference=True), data, **kwargs)
    return content, ElementTree.fromstring(content), notes


def _text(el, path):
    found = el.find(path)
    return None if found is None or found.find("null") is not None else found.text


# ---------- Выбор основы ----------

def test_detects_asycuda_files_only():
    assert asycuda.is_asycuda_file(REFERENCE)
    assert asycuda.is_asycuda_file(BLANK)
    assert not asycuda.is_asycuda_file(SAMPLES / "templates" / "asycuda_like_template.xml")  # шаблон Jinja2
    assert not asycuda.is_asycuda_file(SAMPLES / "reference_declaration.xml")  # другой корень
    assert not asycuda.is_asycuda_file(SAMPLES / "templates" / "declaration_template.xlsx")


def test_choose_base():
    jinja = SAMPLES / "templates" / "asycuda_like_template.xml"
    other_reference = SAMPLES / "reference_declaration.xml"
    # Заполненный файл ASYCUDA в поле «шаблон» — основа он сам
    assert asycuda.choose_base(REFERENCE, [other_reference]) == asycuda.Base(REFERENCE, is_reference=False)
    # Чистый бланк ASYCUDA + эталон из ASYCUDA — основа эталон (в бланке нет шапки и товаров)
    assert asycuda.choose_base(BLANK, [REFERENCE]) == asycuda.Base(REFERENCE, is_reference=True)
    assert asycuda.choose_base(BLANK, [other_reference]) == asycuda.Base(BLANK, is_reference=False)
    # Без шаблона: эталон из ASYCUDA -> результат в формате ASYCUDA
    assert asycuda.choose_base(None, [REFERENCE]) == asycuda.Base(REFERENCE, is_reference=True)
    # Шаблон Jinja2 / Excel или эталон не из ASYCUDA — обычный экспорт
    assert asycuda.choose_base(jinja, [REFERENCE]) is None
    assert asycuda.choose_base(None, [other_reference]) is None


# ---------- Сборка файла ----------

def test_header_kept_items_replaced_totals_recalculated():
    data = _data([_item(1), _item(2, description="Tobacco stems", article="ST-2", total_value=1000.5, packages=3,
                                  gross_weight_kg=310.0, net_weight_kg=300.0, quantity=300)])
    content, root, _notes = _render(REFERENCE, data)

    # Постоянная часть — без изменений, в том числе поля, которых нет в утверждённой шапке
    assert _text(root, "Traders/Consignee/Consignee_name") == "შპს დემო იმპორტი\nთბილისი, სადემონსტრაციო ქუჩა 1"
    assert _text(root, "Declarant/Declarant_code") == "200000001"
    assert _text(root, "Warehouse/Identification") == "WHS200000001/1"
    assert _text(root, "Transport/Border_office/Code") == "69501"

    items = root.findall("Item")
    assert len(items) == 2
    first, second = items
    assert _text(first, "Goods_description/Description_of_goods") == "Tobacco leaves"
    assert _text(second, "Goods_description/Description_of_goods") == "Tobacco stems ST-2"
    assert _text(first, "Tarification/HScode/Commodity_code") == "24012035"
    assert _text(first, "Tarification/HScode/Precision_1") == "000"
    assert _text(first, "Goods_description/Country_of_origin_code") == "76"
    assert _text(first, "Goods_description/Country_of_origin_region") == "BR"
    assert _text(second, "Packages/Number_of_packages") == "3"
    # Поля из образца (процедура, вид упаковки) сохранены
    assert _text(second, "Tarification/Extended_customs_procedure") == "7474"
    assert _text(second, "Packages/Kind_of_packages_code") == "15"

    # Стоимость: национальная = валюта × курс, статистическая > 0 (ошибка ASYCUDA из-за её отсутствия)
    assert _text(first, "Valuation_item/Item_Invoice/Amount_foreign_currency") == "3000"
    assert _text(first, "Valuation_item/Item_Invoice/Amount_national_currency") == "7879.8"
    assert Decimal(_text(second, "Valuation_item/Statistical_value")) == Decimal("1000.5") * Decimal("2.6266")
    assert Decimal(_text(second, "Valuation_item/Statistical_value")) > 0
    assert _text(second, "Valuation_item/Weight_itm/Gross_weight_itm") == "310"
    assert _text(first, "Valuation_item/Alpha_coeficient_of_apportionment") == "0.749906"

    # Итоги
    assert _text(root, "Property/Nbers/Total_number_of_items") == "2"
    assert _text(root, "Property/Forms/Total_number_of_forms") == "2"  # 1 позиция на листе 1, до 3 — на добавочном
    assert _text(root, "Property/Nbers/Total_number_of_packages") == "8"
    assert _text(root, "Valuation/Weight/Gross_weight") == "860"
    assert _text(root, "Valuation/Total/Total_invoice") == "4000.5"
    assert _text(root, "Valuation/Gs_Invoice/Amount_foreign_currency") == "4000.5"
    assert Decimal(_text(root, "Valuation/Total_CIF")) == Decimal("4000.5") * Decimal("2.6266")

    # Формат как у выгрузки ASYCUDA
    assert content.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\r\n<ASYCUDA>')
    assert b"\r\n<Item_tax_total/>\r\n" in content and b" />" not in content
    assert content.count(b"\n") == content.count(b"\r\n")


def test_exchange_rate_from_download_form():
    _content, root, _notes = _render(REFERENCE, _data([_item(1)]), exchange_rate=Decimal("2.7"))
    assert _text(root, "Valuation/Gs_Invoice/Currency_rate") == "2.7"
    assert _text(root, "Item/Valuation_item/Item_Invoice/Currency_rate") == "2.7"
    assert _text(root, "Item/Valuation_item/Statistical_value") == "8100"


def test_attached_documents_invoice_and_transport_replaced():
    data = _data([_item(1)], invoice_numbers=["INV-NEW-7", "INV-NEW-8"])
    _content, root, _notes = _render(REFERENCE, data, reference=_reference())
    docs = [(_text(d, "Attached_document_code"), _text(d, "Attached_document_reference"),
             _text(d, "Attached_document_date")) for d in root.find("Item").findall("Attached_documents")]
    # Инвойс и упаковочный лист с номером старого инвойса — по одному на каждый новый инвойс
    assert docs[:5] == [("003", "INV-NEW-7", "9/21/26"), ("003", "INV-NEW-8", "9/21/26"),
                        ("049", "INV-NEW-7", None), ("049", "INV-NEW-8", None), ("005", "BL-NEW-7", None)]
    # Документы, номера которых не совпадают с номерами эталона, не трогаем
    assert ("057", "APP-OLD-3", "7/31/26") in docs


def test_unknown_values_are_left_empty_not_guessed():
    data = _data([_item(1, hs_code="84821000", country_of_origin="Бразилия", total_value=None)])
    _content, root, notes = _render(REFERENCE, data)
    item = root.find("Item")
    assert _text(item, "Tarification/HScode/Commodity_code") == "84821000"
    assert _text(item, "Tarification/HScode/Precision_1") is None  # другой код — уточнение не выдумываем
    assert _text(item, "Goods_description/Country_of_origin_code") is None
    assert _text(item, "Valuation_item/Statistical_value") is None
    assert _text(root, "Valuation/Total_CIF") is None
    assert any("Бразилия" in note for note in notes)
    assert any("Prev_decl" in note for note in notes)


def test_blank_base_header_filled_from_approved_data():
    data = _data([_item(1, country_of_origin="Germany")])
    _content, root, notes = _render(BLANK, data)
    assert _text(root, "Traders/Consignee/Consignee_code") == "200000099"
    assert _text(root, "Traders/Consignee/Consignee_name") == "შპს ახალი იმპორტი\nთბილისი"
    assert _text(root, "Traders/Exporter/Exporter_name") == "NEW EXPORTER\nRua 1"
    # Декларант уже заполнен в бланке (это сама ASYCUDA) — не перезаписываем
    assert _text(root, "Declarant/Declarant_code") == "400000002"
    assert _text(root, "General_information/Country/Export/Export_country_code") == "76"
    assert _text(root, "Valuation/Gs_Invoice/Currency_code") == "840"
    assert _text(root, "Item/Goods_description/Country_of_origin_code") == "276"
    # В бланке нет курса — стоимость в лари не считаем и предупреждаем
    assert _text(root, "Item/Valuation_item/Statistical_value") is None
    assert any("курс" in note.lower() for note in notes)


def test_rate_parsing():
    assert asycuda.parse_rate("2,6266") == Decimal("2.6266")
    assert asycuda.parse_rate("  ") is None
    for bad in ("0", "-1", "abc", "NaN", "Infinity"):
        with pytest.raises(asycuda.AsycudaError):
            asycuda.parse_rate(bad)


def test_base_without_items_is_rejected(tmp_path):
    path = tmp_path / "no_items.xml"
    path.write_text('<?xml version="1.0"?><ASYCUDA><Property/></ASYCUDA>', encoding="utf-8")
    with pytest.raises(asycuda.AsycudaError):
        asycuda.render(asycuda.Base(path, is_reference=False), _data([_item(1)]))


# ---------- Через сайт ----------

def test_export_endpoint_uses_asycuda_reference(logged_in, fake_llm):
    files = [
        ("reference_file", ("declaration_old.xml", REFERENCE.read_bytes(), "application/xml")),
        ("commercial_files", ("invoice_INV-2026-118.xlsx", (SAMPLES / "invoice_INV-2026-118.xlsx").read_bytes(),
                              "application/octet-stream")),
    ]
    assert logged_in.post("/jobs", files=files, follow_redirects=False).status_code == 303
    page = logged_in.get("/jobs/1").text
    assert 'name="rate"' in page and 'placeholder="2.6266"' in page

    proposed = logged_in.get("/api/jobs/1").json()["proposed"]
    proposed["items"][2]["hs_code"] = "4016930001"
    assert logged_in.post("/api/jobs/1/approve", json={"data": proposed}).json()["status"] == "approved"

    assert logged_in.get("/jobs/1/export?rate=abc").status_code == 422
    response = logged_in.get("/jobs/1/export?rate=2.65")
    assert response.status_code == 200
    assert "declaration_1.xml" in response.headers["content-disposition"]
    root = ElementTree.fromstring(response.content)
    assert root.tag == "ASYCUDA"
    assert len(root.findall("Item")) == len(proposed["items"]) == 4
    assert _text(root, "Traders/Consignee/Consignee_code") == "200000001"  # шапка из файла ASYCUDA
    assert [_text(i, "Tarification/HScode/Commodity_code") for i in root.findall("Item")] == [
        "84821000", "40103200", "40169300", "84212300"]
    assert _text(root, "Valuation/Gs_Invoice/Currency_rate") == "2.65"
