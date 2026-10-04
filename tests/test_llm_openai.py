"""Провайдер OpenAI / OpenAI-совместимый (LLM_PROVIDER=openai). Сеть не используется."""

from types import SimpleNamespace

import httpx2
import openai
import pytest

from app.config import get_settings
from app.services import llm
from app.services.parsers import ParsedDocument
from tests.fake_llm import fake_extraction_result
from tests.test_llm import DOCS


@pytest.fixture
def openai_settings(monkeypatch):
    s = get_settings()
    for key, value in {"llm_provider": "openai", "openai_api_key": "sk-test", "openai_model": "test-model",
                       "openai_base_url": "", "openai_max_tokens": 0}.items():
        monkeypatch.setattr(s, key, value)
    return s


def make_response(content=None, finish_reason="stop", refusal=None):
    content = content if content is not None else fake_extraction_result().model_dump_json()
    return SimpleNamespace(
        model="test-model",
        choices=[SimpleNamespace(finish_reason=finish_reason,
                                 message=SimpleNamespace(content=content, refusal=refusal))],
        usage=SimpleNamespace(prompt_tokens=900, completion_tokens=300),
    )


def bad_request(message="response_format json_schema is not supported"):
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.example.com/v1/chat/completions"))
    return openai.BadRequestError(message, response=response, body=None)


def install_client(monkeypatch, *results):
    """Подменяет клиент: каждый вызов create() берёт следующий результат (ответ или исключение)."""
    calls = []
    queue = list(results)

    def create(**kwargs):
        calls.append(kwargs)
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_openai_client", lambda: client)
    return calls


def test_strict_json_schema_request(monkeypatch, openai_settings):
    calls = install_client(monkeypatch, make_response())
    result, info = llm.extract_declaration(*DOCS)

    assert len(result.new_items) == 4 and (info.input_tokens, info.output_tokens) == (900, 300)
    request = calls[0]
    assert request["model"] == "test-model"
    assert request["messages"][0] == {"role": "system", "content": llm.SYSTEM_PROMPT}
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert "max_tokens" not in request and "max_completion_tokens" not in request


def test_falls_back_to_json_mode_when_schema_unsupported(monkeypatch, openai_settings):
    fenced = "```json\n" + fake_extraction_result().model_dump_json() + "\n```"
    calls = install_client(monkeypatch, bad_request(), make_response(content=fenced))
    result, _ = llm.extract_declaration(*DOCS)

    assert len(result.new_items) == 4
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in calls[1]["messages"][1]["content"]


def test_max_tokens_parameter_name_depends_on_service(monkeypatch, openai_settings):
    monkeypatch.setattr(openai_settings, "openai_max_tokens", 32000)
    calls = install_client(monkeypatch, make_response())
    llm.extract_declaration(*DOCS)
    assert calls[0]["max_completion_tokens"] == 32000  # официальный API OpenAI

    monkeypatch.setattr(openai_settings, "openai_base_url", "https://api.example.com/v1")
    calls = install_client(monkeypatch, make_response())
    llm.extract_declaration(*DOCS)
    assert calls[0]["max_tokens"] == 32000  # совместимый сервис


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (make_response(finish_reason="length", content='{"reference": '), "обрезан"),
        (make_response(refusal="I can't help with that", content=None), "отказалась"),
        (make_response(finish_reason="content_filter", content=""), "отказалась"),
        (make_response(content='{"oops": 1}'), "неожиданном формате"),
    ],
)
def test_bad_responses(monkeypatch, openai_settings, response, error):
    install_client(monkeypatch, response)
    with pytest.raises(llm.LLMError, match=error):
        llm.extract_declaration(*DOCS)


def test_both_modes_rejected(monkeypatch, openai_settings):
    install_client(monkeypatch, bad_request(), bad_request("model does not support JSON mode"))
    with pytest.raises(llm.LLMError, match="does not support JSON mode"):
        llm.extract_declaration(*DOCS)


def test_missing_key_and_model(monkeypatch, openai_settings):
    monkeypatch.setattr(openai_settings, "openai_api_key", "")
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY"):
        llm.extract_declaration(*DOCS)
    monkeypatch.setattr(openai_settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(openai_settings, "openai_model", "")
    with pytest.raises(llm.LLMError, match="OPENAI_MODEL"):
        llm.extract_declaration(*DOCS)


def test_scans_are_rejected_with_explanation(monkeypatch, openai_settings):
    install_client(monkeypatch, make_response())
    scan = ParsedDocument("scan.pdf", "pdf", "", has_text_layer=False, pdf_bytes=b"%PDF")
    with pytest.raises(llm.LLMError, match="скан без текста"):
        llm.extract_declaration(DOCS[0], [scan])


def test_unknown_provider(monkeypatch, openai_settings):
    monkeypatch.setattr(openai_settings, "llm_provider", "gemini-direct")
    with pytest.raises(llm.LLMError, match="Неизвестный LLM_PROVIDER"):
        llm.extract_declaration(*DOCS)


def test_real_client_construction(monkeypatch, openai_settings):
    monkeypatch.setattr(openai_settings, "openai_base_url", "https://api.example.com/v1")
    client = llm._openai_client()
    assert str(client.base_url).startswith("https://api.example.com/v1")
