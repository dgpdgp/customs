"""Модульные тесты парсеров, валидатора и экспорта."""

from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas import DeclarationData
from app.services import exporter
from app.services.parsers import ParseError, parse_file
from app.services.validation import normalize_text, numbers_in_text
from tests.fake_llm import fake_extraction_result

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def test_excel_parser_keeps_empty_cells_in_place():
    doc = parse_file(SAMPLES / "invoice_INV-2026-118.xlsx", "invoice.xlsx")
    # пустая ячейка «HS code» сохранена, количество не съехало в колонку кода
    assert "R9: 1 | BRG-6205 | Ball bearing 6205-2RS |  | 600 | pcs | 4.6 | 2760" in doc.text


def test_xml_parser_blocks_entity_expansion(tmp_path):
    bomb = tmp_path / "bomb.xml"
    bomb.write_text('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]><r>&b;</r>')
    with pytest.raises(ParseError):
        parse_file(bomb, "bomb.xml")


def test_unsupported_extension(tmp_path):
    path = tmp_path / "file.docx"
    path.write_bytes(b"x")
    with pytest.raises(ParseError):
        parse_file(path, "file.docx")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Итого 1 234,56 EUR", Decimal("1234.56")),
        ("Total 1,234.56", Decimal("1234.56")),
        ("Сумма 1.234,56", Decimal("1234.56")),
        ("вес 4800", Decimal("4800")),
        ("qty | 3 2280", Decimal("2280")),  # соседние ячейки PDF, слипшиеся через пробел
        ("weight 21,85 kg", Decimal("21.85")),
    ],
)
def test_numbers_in_text(text, expected):
    assert expected in numbers_in_text(text)


def test_normalize_text_ignores_cell_separators_and_quotes():
    assert normalize_text("ООО «Альфа» | BRG-6205  |  600") == normalize_text('ооо "альфа" brg-6205 600')


def test_xml_export_escapes_special_characters():
    data = DeclarationData.model_validate(fake_extraction_result().reference.model_dump())
    data.header.importer.name = 'ООО "Рога & Копыта" <test>'
    content, filename, media_type = exporter.render_export(data, None, "decl")
    assert b"&amp; \xd0\x9a" in content and b"&lt;test&gt;" in content
    assert filename == "decl.xml" and media_type == "application/xml"


def test_template_sandbox_blocks_python_internals(tmp_path):
    evil = tmp_path / "evil.xml"
    evil.write_text("<x>{{ header.__class__.__mro__[1].__subclasses__() }}</x>")
    data = DeclarationData.model_validate(fake_extraction_result().reference.model_dump())
    with pytest.raises(exporter.TemplateRenderError, match="unsafe"):  # SecurityError песочницы Jinja2
        exporter.render_export(data, evil, "decl")


def test_template_syntax_error_is_reported(tmp_path):
    broken = tmp_path / "broken.xml"
    broken.write_text("<x>{% for item in items %}</x>")
    data = DeclarationData.model_validate(fake_extraction_result().reference.model_dump())
    with pytest.raises(exporter.TemplateRenderError):
        exporter.render_export(data, broken, "decl")
