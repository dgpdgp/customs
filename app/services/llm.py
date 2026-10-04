"""Вызов LLM для извлечения и замены товарных позиций.

Основной провайдер — Claude (Anthropic). Дополнительно поддерживаются OpenAI и сервисы
с OpenAI-совместимым API (LLM_PROVIDER=openai). Промпт, схема ответа и проверка
результата одинаковы для всех провайдеров.

Схема ответа выбирается настройкой LLM_SCHEMA_VARIANT (app/services/schema_variants.py):
вариант может состоять из одного или двух вызовов модели, результат всегда сводится к WireResult.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar
from xml.sax.saxutils import quoteattr

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.i18n import LLM_ISSUE_LANGUAGE, get_lang, t
from app.schemas import LLMExtractionResult
from app.services import schema_variants
from app.services.parsers import ParsedDocument
from app.services.prompts import SYSTEM_PROMPT, USER_INSTRUCTION
from app.services.schema_variants import Part, Variant
from app.services.wire import WireResult, wire_to_result

logger = logging.getLogger(__name__)

# JSON Schema ответа строится из Pydantic-модели. transform_schema приводит её
# к требованиям структурированного вывода API (additionalProperties: false и т. д.).
# Проводной формат без объединений типов — см. app/services/wire.py (лимиты строгой схемы).
OUTPUT_SCHEMA = schema_variants.FULL_SCHEMA


def schema_instruction(schema: dict) -> str:
    """Схема в тексте запроса — для запасного режима без строгого структурированного вывода."""
    return (
        "\n\nВерни ответ строго одним JSON-объектом по этой JSON Schema, без пояснений до и после. "
        "Неизвестные значения — пустая строка, номера позиций без соответствия — 0.\n"
        + json.dumps(schema, ensure_ascii=False)
    )


SCHEMA_INSTRUCTION = schema_instruction(OUTPUT_SCHEMA)
# Признаки ответа API «схема слишком сложная для строгого режима»
_SCHEMA_TOO_COMPLEX_MARKERS = ("grammar", "too complex", "too many optional", "union types", "schema is too")
# Признак ответа API «на счёте закончились деньги» (400 invalid_request_error)
_NO_CREDIT_MARKERS = ("credit balance",)
# Схемы (вариант:вызов), которые API однажды отклонил: до перезапуска не тратим на них время.
_rejected_schemas: set[str] = set()

ModelT = TypeVar("ModelT", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
PDF_MAX_BYTES = 32 * 1024 * 1024  # лимит размера запроса с PDF в Claude API


class LLMError(Exception):
    """Ошибка, понятная пользователю (показывается на странице задачи)."""


@dataclass
class LLMCallInfo:
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


@lru_cache
def _client() -> anthropic.Anthropic:
    key = get_settings().anthropic_api_key
    # Без явного ключа SDK сам найдёт ANTHROPIC_API_KEY или профиль `ant auth login`.
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def build_user_content(
    reference: ParsedDocument,
    commercial: list[ParsedDocument],
    *,
    part: Part | None = None,
    reference_extracted: str | None = None,
) -> list[dict]:
    """Собирает сообщение пользователя: документы в XML-тегах + короткая инструкция.

    Сканы (PDF без текстового слоя) передаются как document-блоки — Claude читает их
    как изображения, — а в текстовой части на них остаётся ссылка по имени.
    part — вызов варианта схемы (какие документы передавать и что добавить к инструкции);
    reference_extracted — эталон, извлечённый предыдущим вызовом (вариант из двух вызовов).
    """
    settings = get_settings()
    blocks: list[dict] = []

    def render(doc: ParsedDocument, tag: str) -> str:
        if not doc.has_text_layer:
            if doc.pdf_bytes is None or len(doc.pdf_bytes) > PDF_MAX_BYTES:
                raise LLMError(t("llm.scan_too_big", name=doc.name))
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(doc.pdf_bytes).decode(),
                    },
                    "title": doc.name,
                }
            )
            body = "[скан без текстового слоя — содержимое в приложенном PDF с таким же названием]"
        else:
            body = doc.text
        return f"<{tag} name={quoteattr(doc.name)} format={quoteattr(doc.kind)}>\n{body}\n</{tag}>"

    parts = []
    if part is None or part.with_reference:
        parts.append(render(reference, "reference_declaration"))
    if reference_extracted is not None:
        parts.append(f"<reference_extracted>\n{reference_extracted}\n</reference_extracted>")
    if part is None or part.with_commercial:
        parts += ["<commercial_documents>", *(render(doc, "document") for doc in commercial),
                  "</commercial_documents>"]
    parts += ["", USER_INSTRUCTION]
    if part is not None and part.instruction:
        parts.append(part.instruction)
    parts.append(LLM_ISSUE_LANGUAGE[get_lang()])
    text = "\n".join(parts)

    if len(text) > settings.llm_max_input_chars:
        # Не обрезаем молча: потеря строк инвойса = потерянные товары в декларации.
        raise LLMError(t("llm.input_too_big", size=f"{len(text):,}", limit=f"{settings.llm_max_input_chars:,}"))

    # Документы-сканы идут перед текстом: так модель сначала «видит» их.
    blocks.append({"type": "text", "text": text})
    return blocks


def extract_declaration(
    reference: ParsedDocument, commercial: list[ParsedDocument]
) -> tuple[LLMExtractionResult, LLMCallInfo]:
    """Эталон + новые документы -> структурированный JSON (один или два вызова — по варианту схемы)."""
    settings = get_settings()
    provider = settings.llm_provider
    if provider not in ("anthropic", "openai"):
        raise LLMError(t("llm.unknown_provider", provider=provider))
    try:
        variant = schema_variants.get(settings.llm_schema_variant)
    except schema_variants.VariantError as exc:
        raise LLMError(str(exc)) from exc

    parsed: dict[str, BaseModel] = {}
    infos: list[LLMCallInfo] = []
    context: str | None = None
    for part in variant.parts:
        content = build_user_content(reference, commercial, part=part, reference_extracted=context)
        if provider == "anthropic":
            text, info = _call_anthropic(content, variant, part)
        else:
            text, info = _call_openai_compatible(content, part, [reference, *commercial])
        infos.append(info)
        parsed[part.name] = _parse_json(text, part.model)
        if part.context is not None:
            context = part.context(parsed[part.name])

    try:
        result = wire_to_result(variant.combine(parsed))
    except schema_variants.VariantError as exc:
        raise LLMError(str(exc)) from exc
    info = LLMCallInfo(
        model=infos[-1].model,
        input_tokens=sum(i.input_tokens for i in infos),
        output_tokens=sum(i.output_tokens for i in infos),
        duration_ms=sum(i.duration_ms for i in infos),
    )
    logger.info("LLM (%s, вызовов: %d): %s", variant.key, len(infos), json.dumps(info.__dict__))
    return result, info


def parse_model_output(text: str) -> LLMExtractionResult:
    """JSON модели (вариант full) -> внутренние модели."""
    return wire_to_result(_parse_json(text, WireResult))


def _parse_json(text: str, model: type[ModelT]) -> ModelT:
    """Терпим к обёртке ```json и тексту вокруг JSON (в запасном режиме без строгой схемы)."""
    candidates = [_strip_code_fence(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    error: ValidationError | None = None
    for candidate in candidates:
        try:
            return model.model_validate_json(candidate)
        except ValidationError as exc:
            error = exc
    logger.error("Ответ LLM не прошёл валидацию: %s\n%s", error, text[:2000])
    raise LLMError(t("llm.bad_format")) from error


def _strip_code_fence(text: str) -> str:
    """Некоторые модели оборачивают JSON в ```json … ``` даже в JSON-режиме."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text


# ---------- Claude (Anthropic) ----------

def _is_schema_too_complex(exc: anthropic.BadRequestError) -> bool:
    message = str(exc.message).lower()
    return any(marker in message for marker in _SCHEMA_TOO_COMPLEX_MARKERS)


def _is_no_credit(exc: anthropic.BadRequestError) -> bool:
    message = str(exc.message).lower()
    return any(marker in message for marker in _NO_CREDIT_MARKERS)


def _call_anthropic(content: list[dict], variant: Variant, part: Part) -> tuple[str, LLMCallInfo]:
    """Сначала строгий структурированный вывод; если API отклоняет схему как слишком
    сложную, повторяем без неё (схема в тексте запроса, ответ проверяет Pydantic)."""
    schema_key = f"{variant.key}:{part.name}"
    if schema_key not in _rejected_schemas:
        try:
            return _anthropic_request(content, part, strict=True)
        except _SchemaRejected as exc:
            _rejected_schemas.add(schema_key)
            logger.warning("API отклонил строгую схему %s (%s) — переходим на JSON по инструкции", schema_key, exc)
    return _anthropic_request(content, part, strict=False)


class _SchemaRejected(Exception):
    pass


def _anthropic_request(content: list[dict], part: Part, *, strict: bool) -> tuple[str, LLMCallInfo]:
    settings = get_settings()
    output_config: dict = {"effort": settings.llm_effort}
    if strict:
        output_config["format"] = {"type": "json_schema", "schema": part.schema}
    else:
        content = [*content[:-1], {"type": "text", "text": content[-1]["text"] + schema_instruction(part.schema)}]
    request: dict = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content}],
        "output_config": output_config,
    }
    if settings.llm_fallbacks:
        # Если модель откажется отвечать, API сам повторит запрос на рекомендованной модели.
        request["betas"] = [FALLBACK_BETA]
        request["fallbacks"] = "default"

    client = _client()
    if client.api_key is None and client.auth_token is None and client.credentials is None:
        # Без проверки SDK упал бы с TypeError, и пользователь увидел бы «внутреннюю ошибку».
        raise LLMError(t("llm.no_key", var="Anthropic API (ANTHROPIC_API_KEY)"))

    started = time.monotonic()
    try:
        # Стриминг обязателен при большом max_tokens: иначе HTTP-запрос упрётся в таймаут.
        with client.beta.messages.stream(**request) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise LLMError(t("llm.bad_key", var="Anthropic API (ANTHROPIC_API_KEY)")) from exc
    except anthropic.PermissionDeniedError as exc:
        raise LLMError(t("llm.no_access", model=settings.llm_model)) from exc
    except anthropic.RateLimitError as exc:
        raise LLMError(t("llm.rate_limit", provider="Anthropic API")) from exc
    except anthropic.BadRequestError as exc:
        if _is_no_credit(exc):
            raise LLMError(t("llm.no_credit")) from exc
        if strict and _is_schema_too_complex(exc):
            raise _SchemaRejected(exc.message) from exc
        raise LLMError(t("llm.rejected", detail=exc.message)) from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(t("llm.api_error", provider="Anthropic", status=exc.status_code)) from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError(t("llm.no_connection", provider="Anthropic")) from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    # stop_reason проверяем до чтения ответа: при отказе или обрезке JSON может быть неполным.
    if message.stop_reason == "refusal":
        raise LLMError(t("llm.refusal"))
    if message.stop_reason == "max_tokens":
        raise LLMError(t("llm.truncated", var="LLM_MAX_TOKENS"))

    text = "".join(block.text for block in message.content if block.type == "text")
    info = LLMCallInfo(
        model=message.model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        duration_ms=duration_ms,
    )
    return text, info


# ---------- OpenAI и OpenAI-совместимые сервисы ----------

def _openai_client() -> openai.OpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise LLMError(t("llm.no_key", var="OPENAI_API_KEY"))
    return openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)


def _call_openai_compatible(
    content: list[dict], part: Part, documents: list[ParsedDocument]
) -> tuple[str, LLMCallInfo]:
    """Chat Completions API: OpenAI и сервисы с таким же интерфейсом (адрес — OPENAI_BASE_URL).

    Сначала просим ответ строго по JSON Schema; если сервис такой режим не поддерживает
    (ошибка 400), повторяем в простом JSON-режиме со схемой в тексте запроса. Ответ в любом
    случае проверяется той же Pydantic-моделью, что и для Claude.
    """
    settings = get_settings()
    if not settings.openai_model:
        raise LLMError(t("llm.no_model"))
    scans = [d.name for d in documents if not d.has_text_layer]
    if scans:
        raise LLMError(t("llm.scan_unsupported", name=scans[0]))
    user_text = content[-1]["text"]
    client = _openai_client()

    base: dict = {"model": settings.openai_model}
    if settings.openai_max_tokens:
        # Официальный API OpenAI для новых моделей принимает max_completion_tokens,
        # совместимые сервисы обычно — max_tokens.
        key = "max_tokens" if settings.openai_base_url else "max_completion_tokens"
        base[key] = settings.openai_max_tokens
    strict_request = {
        **base,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "declaration_extraction", "schema": part.schema, "strict": True},
        },
    }
    json_mode_request = {
        **base,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text + schema_instruction(part.schema)},
        ],
        "response_format": {"type": "json_object"},
    }

    started = time.monotonic()
    try:
        try:
            response = client.chat.completions.create(**strict_request)
        except openai.BadRequestError:
            logger.warning("Сервис отклонил режим JSON Schema, повторяем в JSON-режиме", exc_info=True)
            response = client.chat.completions.create(**json_mode_request)
    except openai.AuthenticationError as exc:
        raise LLMError(t("llm.bad_key", var="OPENAI_API_KEY")) from exc
    except openai.PermissionDeniedError as exc:
        raise LLMError(t("llm.no_access", model=settings.openai_model)) from exc
    except openai.NotFoundError as exc:
        raise LLMError(t("llm.model_not_found", model=settings.openai_model)) from exc
    except openai.RateLimitError as exc:
        raise LLMError(t("llm.quota")) from exc
    except openai.BadRequestError as exc:
        raise LLMError(t("llm.rejected", detail=exc.message)) from exc
    except openai.APIStatusError as exc:
        raise LLMError(t("llm.api_error", provider="OpenAI-compatible", status=exc.status_code)) from exc
    except openai.APIConnectionError as exc:
        raise LLMError(t("llm.no_connection", provider="OpenAI-compatible (OPENAI_BASE_URL)")) from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    if not response.choices:
        raise LLMError(t("llm.empty"))
    choice = response.choices[0]
    if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
        raise LLMError(t("llm.refusal"))
    if choice.finish_reason == "length":
        raise LLMError(t("llm.truncated", var="OPENAI_MAX_TOKENS"))

    usage = response.usage
    info = LLMCallInfo(
        model=response.model or settings.openai_model,
        input_tokens=(usage.prompt_tokens if usage else 0) or 0,
        output_tokens=(usage.completion_tokens if usage else 0) or 0,
        duration_ms=duration_ms,
    )
    return choice.message.content or "", info
