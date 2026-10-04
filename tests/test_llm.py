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
