"""Ответ «идеальной» LLM для демо-файлов из samples/ (используется вместо API в тестах)."""

from app.schemas import (
    DeclarationData,
    DeclarationHeader,
    GoodsItem,
    LLMExtractionResult,
    LLMIssue,
    Party,
    ShipmentData,
)

INVOICE = "invoice_INV-2026-118.xlsx"


def _item(**fields) -> GoodsItem:
    defaults = dict(article=None, reference_item_no=None, country_of_origin="DE", unit="шт", packages=None,
                    gross_weight_kg=None, net_weight_kg=None, unit_price=None, source_document=INVOICE,
                    source_quote=None, hs_code_basis="document")
    return GoodsItem(**{**defaults, **fields})


def fake_extraction_result() -> LLMExtractionResult:
    header = DeclarationHeader(
        declaration_type="ИМ 40",
        customs_office="00101 — ЦЭД «Ташкент-товарный» (демо)",
        exporter=Party(name="Beta Maschinenbau GmbH", address="Industriestraße 12, 70565 Stuttgart",
                       country="DE", tax_id=None),
        importer=Party(name="ООО «Альфа Импорт»", address="г. Ташкент, ул. Демонстрационная, 1",
                       country="UZ", tax_id="300000001"),
        declarant=Party(name="ООО «Гамма Брокер»", address="г. Ташкент, ул. Таможенная, 5",
                        country="UZ", tax_id="300000002"),
        contract_number="AB-2025/17", contract_date="15.03.2025",
        delivery_terms="DAP", delivery_place="Tashkent", currency="EUR",
        country_of_dispatch="DE", country_of_destination="UZ", transport_mode="30 (автомобильный)",
    )
    reference = DeclarationData(
        header=header,
        shipment=ShipmentData(invoice_numbers=["INV-2025-342"], invoice_date="10.09.2025",
                              transport_document="CMR 0458812", vehicle_id="S 482 KL / SA 7731",
                              total_invoice_value=2965.0, total_packages=5, total_gross_weight_kg=75.6,
                              total_net_weight_kg=69.0),
        items=[
            _item(item_no=1, description="ПОДШИПНИК ШАРИКОВЫЙ РАДИАЛЬНЫЙ 6205-2RS, АРТ. BRG-6205", article="BRG-6205",
                  hs_code="8482100009", quantity=400, packages=3, gross_weight_kg=52.5, net_weight_kg=48.0,
                  unit_price=4.6, total_value=1840.0, source_document="reference_declaration.xml"),
            _item(item_no=2, description="РЕМЕНЬ ПРИВОДНОЙ КЛИНОВОЙ SPZ 1250, АРТ. BLT-SPZ1250",
                  article="BLT-SPZ1250", hs_code="4010320000", quantity=150, packages=2, gross_weight_kg=23.1,
                  net_weight_kg=21.0, unit_price=7.5, total_value=1125.0, source_document="reference_declaration.xml"),
        ],
    )
    new_items = [
        _item(item_no=1, description="ПОДШИПНИК ШАРИКОВЫЙ РАДИАЛЬНЫЙ 6205-2RS, АРТ. BRG-6205", article="BRG-6205",
              hs_code="8482100009", hs_code_basis="reference_match", reference_item_no=1, quantity=600, packages=3,
              gross_weight_kg=78.6, net_weight_kg=72.0, unit_price=4.6, total_value=2760.0,
              source_quote="1 | BRG-6205 | Ball bearing 6205-2RS | | 600 | pcs | 4.6 | 2760"),
        _item(item_no=2, description="РЕМЕНЬ ПРИВОДНОЙ КЛИНОВОЙ SPZ 1250, АРТ. BLT-SPZ1250", article="BLT-SPZ1250",
              hs_code="4010320000", hs_code_basis="reference_match", reference_item_no=2, quantity=200, packages=2,
              gross_weight_kg=30.8, net_weight_kg=28.0, unit_price=7.5, total_value=1500.0,
              source_quote="2 | BLT-SPZ1250 | V-belt SPZ 1250 | | 200 | pcs | 7.5 | 1500"),
        _item(item_no=3, description="Oil seal 35x52x7 NBR", article="SEAL-35527", hs_code=None,
              hs_code_basis="not_found", quantity=1000, packages=1, gross_weight_kg=10.4, net_weight_kg=9.5,
              unit_price=0.85, total_value=850.0,
              source_quote="3 | SEAL-35527 | Oil seal 35x52x7 NBR | | 1000 | pcs | 0.85 | 850"),
        _item(item_no=4, description="Hydraulic filter element 10 micron", article="FLT-0450", hs_code="8421230000",
              hs_code_basis="document", quantity=50, packages=2, gross_weight_kg=34.2, net_weight_kg=31.0,
              unit_price=12.4, total_value=620.0,
              source_quote="4 | FLT-0450 | Hydraulic filter element 10 micron | 8421230000 | 50 | pcs | 12.4 | 620"),
    ]
    return LLMExtractionResult(
        reference=reference,
        new_shipment=ShipmentData(invoice_numbers=["INV-2026-118"], invoice_date="22.09.2026",
                                  transport_document="CMR 0461190", vehicle_id="S 517 KL / SA 8840",
                                  total_invoice_value=5730.0, total_packages=8, total_gross_weight_kg=154.0,
                                  total_net_weight_kg=140.5),
        new_items=new_items,
        issues=[
            LLMIssue(severity="error", item_no=3, field="hs_code",
                     message="Товара SEAL-35527 нет в эталоне, код в документах не указан"),
            LLMIssue(severity="info", item_no=3, field="description",
                     message="Описание оставлено на английском: аналога в эталоне нет"),
        ],
    )
