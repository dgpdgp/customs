"""Формирование запроса к Claude API и разбор ответа (клиент подменён, сеть не используется)."""

import json
from types import SimpleNamespace

import pytest

from app.schemas import LLMExtractionResult
from app.services import llm
from app.services.parsers import ParsedDocument, parse_file
from tests.fake_llm import fake_extraction_result


def make_pdf(path, text: str | None) -> None:
    """Минимальный PDF из одной страницы; text=None — страница без текста (как скан)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))


def test_pdf_with_text_layer(tmp_path):
    path = tmp_path / "invoice.pdf"
    make_pdf(path, "INVOICE INV-77 Ball bearing 6205-2RS qty 600 amount 2760.00 EUR total 2760.00")
    doc = parse_file(path, "invoice.pdf")
    assert doc.has_text_layer and "INV-77" in doc.text and "=== Страница 1 ===" in doc.text


def test_scanned_pdf_goes_to_llm_as_document_block(tmp_path):
    path = tmp_path / "scan.pdf"
    make_pdf(path, None)
    scan = parse_file(path, "scan.pdf")
    assert not scan.has_text_layer and scan.pdf_bytes

    reference = ParsedDocument("ref.xml", "xml", "<Declaration/>")
    content = llm.build_user_content(reference, [scan])
    assert content[0]["type"] == "document" and content[0]["title"] == "scan.pdf"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert content[-1]["type"] == "text"
    assert '<document name="scan.pdf" format="pdf">' in content[-1]["text"]


def test_oversized_input_is_rejected_not_truncated(monkeypatch):
    monkeypatch.setattr(llm.get_settings(), "llm_max_input_chars", 100)
    with pytest.raises(llm.LLMError, match="слишком большие"):
        llm.build_user_content(ParsedDocument("ref.xml", "xml", "x" * 200), [])


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


def fake_client(message, captured: dict, api_key="test-key"):
    def stream(**kwargs):
        captured.update(kwargs)
        return FakeStream(message)

    return SimpleNamespace(api_key=api_key, auth_token=None, credentials=None,
                           beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def make_message(stop_reason="end_turn", text=None):
    text = text if text is not None else fake_extraction_result().model_dump_json()
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1234, output_tokens=567),
    )


DOCS = (ParsedDocument("ref.xml", "xml", "<Declaration/>"), [ParsedDocument("inv.xlsx", "excel", "R1: a | b")])


def test_request_shape_and_parsed_result(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "_client", lambda: fake_client(make_message(), captured))
    result, info = llm.extract_declaration(*DOCS)

    assert isinstance(result, LLMExtractionResult) and len(result.new_items) == 4
    assert (info.input_tokens, info.output_tokens) == (1234, 567)
    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"]["effort"] == "high"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["fallbacks"] == "default" and captured["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in captured  # у Opus 5.5 рассуждения всегда включены, глубину задаёт effort
    schema = json.dumps(captured["output_config"]["format"]["schema"])
    assert '"additionalProperties": false' in schema


def test_fallbacks_can_be_disabled(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm.get_settings(), "llm_fallbacks", False)
    monkeypatch.setattr(llm, "_client", lambda: fake_client(make_message(), captured))
    llm.extract_declaration(*DOCS)
    assert "fallbacks" not in captured and "betas" not in captured


@pytest.mark.parametrize(
    ("stop_reason", "text", "error"),
    [
        ("refusal", "", "отказалась"),
        ("max_tokens", '{"reference": ', "обрезан"),
        ("end_turn", '{"unexpected": true}', "неожиданном формате"),
    ],
)
def test_bad_responses_raise_readable_errors(monkeypatch, stop_reason, text, error):
    monkeypatch.setattr(llm, "_client", lambda: fake_client(make_message(stop_reason, text), {}))
    with pytest.raises(llm.LLMError, match=error):
        llm.extract_declaration(*DOCS)


def test_missing_api_key_gives_readable_error(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "_client", lambda: fake_client(make_message(), captured, api_key=None))
    with pytest.raises(llm.LLMError, match="не задан ключ Anthropic API"):
        llm.extract_declaration(*DOCS)
    assert not captured  # запрос не отправлялся


def test_real_sdk_client_without_credentials(monkeypatch):
    """Настоящий клиент SDK без ключа: проверка срабатывает до сетевого запроса."""
    import anthropic

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    client = anthropic.Anthropic()
    if client.credentials is not None:
        pytest.skip("на машине настроен профиль `ant auth login`")
    monkeypatch.setattr(llm, "_client", lambda: client)
    with pytest.raises(llm.LLMError, match="не задан ключ"):
        llm.extract_declaration(*DOCS)


# ---------- Сложность схемы и запасной режим ----------

def _schema_stats(schema: dict) -> tuple[int, int, int]:
    """(объединения типов, необязательные поля, поля с раскрытием $ref) — как их считает API."""
    defs = schema.get("$defs", {})
    stats = {"unions": 0, "optional": 0, "props": 0}

    def walk(node):
        if "$ref" in node:
            node = defs[node["$ref"].split("/")[-1]]
        if "anyOf" in node or isinstance(node.get("type"), list):
            stats["unions"] += 1
        if node.get("type") == "object":
            props = node.get("properties", {})
            stats["optional"] += len(set(props) - set(node.get("required", [])))
            stats["props"] += len(props)
            for child in props.values():
                walk(child)
        elif node.get("type") == "array":
            walk(node["items"])

    walk(schema)
    return stats["unions"], stats["optional"], stats["props"]


def test_output_schema_fits_structured_output_limits():
    """Документированные лимиты строгой схемы Claude: ≤16 полей с объединением типов
    (anyOf / ["x","null"]) и ≤24 необязательных поля. Наш проводной формат держит оба на нуле.
    Порог по числу полей — защита от незаметного разрастания схемы."""
    unions, optional, props = _schema_stats(llm.OUTPUT_SCHEMA)
    assert unions == 0
    assert optional == 0
    assert props <= 90


def bad_request(message: str):
    import anthropic
    import httpx2

    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError(message, response=response, body=None)


def test_falls_back_to_prompt_json_when_schema_rejected(monkeypatch):
    monkeypatch.setattr(llm, "_strict_schema_rejected", False)
    calls = []
    results = [bad_request("The compiled grammar is too large. Simplify your tool schemas"),
               make_message(text="Вот результат:\n" + fake_extraction_result().model_dump_json())]

    def stream(**kwargs):
        calls.append(kwargs)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeStream(result)

    client = SimpleNamespace(api_key="k", auth_token=None, credentials=None,
                             beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    monkeypatch.setattr(llm, "_client", lambda: client)

    result, _ = llm.extract_declaration(*DOCS)
    assert len(result.new_items) == 4
    assert "format" in calls[0]["output_config"]
    assert "format" not in calls[1]["output_config"]
    assert "JSON Schema" in calls[1]["messages"][0]["content"][-1]["text"]

    # Следующая обработка сразу идёт без строгой схемы — без лишнего отклонённого запроса
    results.append(make_message())
    llm.extract_declaration(*DOCS)
    assert "format" not in calls[2]["output_config"]
    monkeypatch.setattr(llm, "_strict_schema_rejected", False)


def test_other_bad_requests_are_not_retried(monkeypatch):
    monkeypatch.setattr(llm, "_strict_schema_rejected", False)

    def stream(**kwargs):
        raise bad_request("prompt is too long: 1200000 tokens > 1000000 maximum")

    client = SimpleNamespace(api_key="k", auth_token=None, credentials=None,
                             beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    monkeypatch.setattr(llm, "_client", lambda: client)
    with pytest.raises(llm.LLMError, match="prompt is too long"):
        llm.extract_declaration(*DOCS)
    assert llm._strict_schema_rejected is False


def test_wire_format_strings_are_converted():
    """Ответ в проводном формате: числа строками, пустые строки, 0 вместо null."""
    wire = json.loads(fake_extraction_result().model_dump_json())
    item = wire["new_items"][0]
    item.update(total_value="2 760,00", gross_weight_kg="", reference_item_no=0, source_quote="")
    wire["new_items"][1]["quantity"] = "около двухсот"
    wire["issues"][0]["item_no"] = 0
    wire["issues"][0]["field"] = ""

    result = llm.parse_model_output(json.dumps(wire))
    first = result.new_items[0]
    assert first.total_value == 2760.0
    assert first.gross_weight_kg is None and first.reference_item_no is None and first.source_quote is None
    assert result.new_items[1].quantity is None
    assert any("около двухсот" in i.message and i.item_no == 2 for i in result.issues)
    assert result.issues[0].item_no is None and result.issues[0].field is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2760", 2760.0), ("2760.50", 2760.5), ("2 760,50", 2760.5), ("1.234,56", 1234.56),
     ("1,234.56", 1234.56), ("", None), ("abc", None)],
)
def test_parse_number(text, expected):
    from app.services.wire import parse_number

    assert parse_number(text)[0] == expected
