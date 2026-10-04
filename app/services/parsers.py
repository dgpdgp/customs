"""Извлечение текста из загруженных файлов (PDF, Excel, CSV, XML, TXT).

Каждый файл превращается в ParsedDocument: имя + текст, который увидит LLM.
Этот же текст потом используется валидатором, чтобы проверить, что цитаты
и числа в ответе модели действительно присутствуют в документах.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pdfplumber
from defusedxml import ElementTree as SafeET
from pypdf import PdfReader

logger = logging.getLogger(__name__)

PDF_EXTENSIONS = {".pdf"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}
TEXT_EXTENSIONS = {".csv", ".txt", ".xml"}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | EXCEL_EXTENSIONS | TEXT_EXTENSIONS

# Если в PDF меньше этого числа символов на страницу, считаем его сканом без
# текстового слоя и отправляем в LLM сам PDF (Claude читает сканы напрямую).
MIN_CHARS_PER_PDF_PAGE = 40


class ParseError(Exception):
    pass


@dataclass
class ParsedDocument:
    name: str  # исходное имя файла — по нему LLM ссылается на источник
    kind: str  # pdf | excel | csv | xml | text
    text: str
    has_text_layer: bool = True  # False — скан, текст недоступен для автоматической сверки
    pdf_bytes: bytes | None = None  # заполняется только для сканов


def parse_file(path: Path, display_name: str) -> ParsedDocument:
    ext = path.suffix.lower()
    try:
        if ext in PDF_EXTENSIONS:
            return _parse_pdf(path, display_name)
        if ext in EXCEL_EXTENSIONS:
            return _parse_excel(path, display_name)
        if ext == ".csv":
            return _parse_csv(path, display_name)
        if ext == ".xml":
            return _parse_xml(path, display_name)
        if ext == ".txt":
            return ParsedDocument(display_name, "text", _read_text(path))
    except ParseError:
        raise
    except Exception as exc:  # повреждённый файл, неподдерживаемая кодировка и т. п.
        logger.exception("Не удалось разобрать %s", display_name)
        raise ParseError(f"Не удалось прочитать файл «{display_name}»: {exc}") from exc
    raise ParseError(f"Формат файла «{display_name}» не поддерживается")


# ---------- PDF ----------

def _parse_pdf(path: Path, name: str) -> ParsedDocument:
    pages: list[str] = []
    content_chars = 0  # непробельные символы текстового слоя (без служебных заголовков)
    try:
        with pdfplumber.open(path) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                page_text = page.extract_text() or ""
                content_chars += len("".join(page_text.split()))
                parts = [f"=== Страница {number} ===", page_text]
                # Таблицы отдельно: в них строки инвойса сохраняют разбивку по колонкам.
                for t_index, table in enumerate(page.extract_tables(), start=1):
                    parts.append(f"--- Таблица {t_index} (стр. {number}) ---")
                    parts.extend(_join_row(row) for row in table if any(cell for cell in row))
                pages.append("\n".join(parts))
    except Exception:
        # pdfplumber не справился (нестандартный PDF) — пробуем pypdf.
        logger.warning("pdfplumber не смог прочитать %s, пробуем pypdf", name, exc_info=True)
        pages, content_chars = [], 0
        for number, page in enumerate(PdfReader(str(path)).pages, start=1):
            page_text = page.extract_text() or ""
            content_chars += len("".join(page_text.split()))
            pages.append(f"=== Страница {number} ===\n{page_text}")

    if content_chars < MIN_CHARS_PER_PDF_PAGE * max(len(pages), 1):
        return ParsedDocument(name, "pdf", "", has_text_layer=False, pdf_bytes=path.read_bytes())
    return ParsedDocument(name, "pdf", "\n\n".join(pages))


# ---------- Excel / CSV ----------

def _parse_excel(path: Path, name: str) -> ParsedDocument:
    # header=None: не угадываем заголовки — в инвойсах шапка часто занимает несколько строк.
    # Для .xlsx pandas читает сохранённые значения формул (а не сами формулы).
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
    blocks = [f"=== Лист «{sheet}» ===\n{_frame_to_text(frame)}" for sheet, frame in sheets.items()]
    return ParsedDocument(name, "excel", "\n\n".join(blocks))


def _parse_csv(path: Path, name: str) -> ParsedDocument:
    frame = pd.read_csv(path, header=None, dtype=object, sep=None, engine="python", encoding_errors="replace")
    return ParsedDocument(name, "csv", _frame_to_text(frame))


def _frame_to_text(frame: pd.DataFrame) -> str:
    """Строки таблицы в вид `R12: ячейка | ячейка | ...` (номер строки как в Excel).

    Пустые ячейки внутри строки сохраняются, иначе значения сдвинутся в соседние
    колонки (например, количество окажется под заголовком «Код ТН ВЭД»).
    """
    lines = []
    for index, row in frame.iterrows():
        cells = [_format_cell(v) for v in row.tolist()]
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            lines.append(f"R{index + 1}: " + " | ".join(cells))
    return "\n".join(lines)


def _format_cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # 4800.0 -> "4800", как в документе
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return " ".join(str(value).split())


def _join_row(row: list[str | None]) -> str:
    return " | ".join(" ".join((cell or "").split()) for cell in row)


# ---------- XML / текст ----------

def _parse_xml(path: Path, name: str) -> ParsedDocument:
    raw = path.read_bytes()
    # defusedxml защищает от XXE и «billion laughs»; здесь только проверяем, что XML корректен.
    try:
        SafeET.fromstring(raw)
    except SafeET.ParseError as exc:
        raise ParseError(f"Файл «{name}» не является корректным XML: {exc}") from exc
    # LLM хорошо читает XML как есть — передаём исходный текст без преобразований.
    return ParsedDocument(name, "xml", _decode(raw))


def _read_text(path: Path) -> str:
    return _decode(path.read_bytes())


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
