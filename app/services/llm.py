"""Вызов LLM для извлечения и замены товарных позиций.

Основной провайдер — Claude (Anthropic). Дополнительно поддерживаются OpenAI и сервисы
с OpenAI-совместимым API (LLM_PROVIDER=openai). Промпт, схема ответа и проверка
результата одинаковы для всех провайдеров.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from xml.sax.saxutils import quoteattr

import anthropic
import openai
from pydantic import ValidationError

from app.config import get_settings
from app.schemas import LLMExtractionResult
from app.services.parsers import ParsedDocument
from app.services.prompts import SYSTEM_PROMPT, USER_INSTRUCTION
from app.services.wire import WireResult, wire_to_result

logger = logging.getLogger(__name__)

# JSON Schema ответа строится из Pydantic-модели. transform_schema приводит её
# к требованиям структурированного вывода API (additionalProperties: false и т. д.).
# Проводной формат без объединений типов — см. app/services/wire.py (лимиты строгой схемы).
OUTPUT_SCHEMA = anthropic.transform_schema(WireResult)
SCHEMA_INSTRUCTION = (
    "\n\nВерни ответ строго одним JSON-объектом по этой JSON Schema, без пояснений до и после. "
    "Неизвестные значения — пустая строка, номера позиций без соответствия — 0.\n"
    + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)
)
# Признаки ответа API «схема слишком сложная для строгого режима»
_SCHEMA_TOO_COMPLEX_MARKERS = ("grammar", "too complex", "too many optional", "union types", "schema is too")
# Если API однажды отклонил строгую схему, до перезапуска не тратим на неё время.
_strict_schema_rejected = False

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


def build_user_content(reference: ParsedDocument, commercial: list[ParsedDocument]) -> list[dict]:
    """Собирает сообщение пользователя: документы в XML-тегах + короткая инструкция.

    Сканы (PDF без текстового слоя) передаются как document-блоки — Claude читает их
    как изображения, — а в текстовой части на них остаётся ссылка по имени.
    """
    settings = get_settings()
    blocks: list[dict] = []

    def render(doc: ParsedDocument, tag: str) -> str:
        if not doc.has_text_layer:
            if doc.pdf_bytes is None or len(doc.pdf_bytes) > PDF_MAX_BYTES:
                raise LLMError(f"Скан «{doc.name}» слишком большой для отправки в LLM (лимит 32 МБ)")
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

    parts = [render(reference, "reference_declaration"), "<commercial_documents>"]
    parts += [render(doc, "document") for doc in commercial]
    parts += ["</commercial_documents>", "", USER_INSTRUCTION]
    text = "\n".join(parts)

    if len(text) > settings.llm_max_input_chars:
        # Не обрезаем молча: потеря строк инвойса = потерянные товары в декларации.
        raise LLMError(
            f"Документы слишком большие ({len(text):,} символов, лимит {settings.llm_max_input_chars:,}). "
            "Разделите поставку на несколько задач или увеличьте LLM_MAX_INPUT_CHARS."
        )

    # Документы-сканы идут перед текстом: так модель сначала «видит» их.
    blocks.append({"type": "text", "text": text})
    return blocks


def extract_declaration(
    reference: ParsedDocument, commercial: list[ParsedDocument]
) -> tuple[LLMExtractionResult, LLMCallInfo]:
    """Один вызов модели: эталон + новые документы -> структурированный JSON."""
    provider = get_settings().llm_provider
    if provider == "anthropic":
        text, info = _call_anthropic(reference, commercial)
    elif provider == "openai":
        text, info = _call_openai_compatible(reference, commercial)
    else:
        raise LLMError(f"Неизвестный LLM_PROVIDER «{provider}»: допустимо anthropic или openai")

    result = parse_model_output(text)
    logger.info("LLM: %s", json.dumps(info.__dict__))
    return result, info


def parse_model_output(text: str) -> LLMExtractionResult:
    """JSON модели -> внутренние модели. Терпим к обёртке ```json и тексту вокруг JSON
    (в запасном режиме без строгой схемы)."""
    candidates = [_strip_code_fence(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    error: ValidationError | None = None
    for candidate in candidates:
        try:
            return wire_to_result(WireResult.model_validate_json(candidate))
        except ValidationError as exc:
            error = exc
    logger.error("Ответ LLM не прошёл валидацию: %s\n%s", error, text[:2000])
    raise LLMError("Модель вернула данные в неожиданном формате, повторите обработку") from error


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


def _call_anthropic(reference: ParsedDocument, commercial: list[ParsedDocument]) -> tuple[str, LLMCallInfo]:
    """Сначала строгий структурированный вывод; если API отклоняет схему как слишком
    сложную, повторяем без неё (схема в тексте запроса, ответ проверяет Pydantic)."""
    global _strict_schema_rejected
    if not _strict_schema_rejected:
        try:
            return _anthropic_request(reference, commercial, strict=True)
        except _SchemaRejected as exc:
            _strict_schema_rejected = True
            logger.warning("API отклонил строгую схему (%s) — переходим на JSON по инструкции", exc)
    return _anthropic_request(reference, commercial, strict=False)


class _SchemaRejected(Exception):
    pass


def _anthropic_request(
    reference: ParsedDocument, commercial: list[ParsedDocument], *, strict: bool
) -> tuple[str, LLMCallInfo]:
    settings = get_settings()
    content = build_user_content(reference, commercial)
    output_config: dict = {"effort": settings.llm_effort}
    if strict:
        output_config["format"] = {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    else:
        content[-1] = {"type": "text", "text": content[-1]["text"] + SCHEMA_INSTRUCTION}
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
        raise LLMError("На сервере не задан ключ Anthropic API (ANTHROPIC_API_KEY). Обратитесь к администратору.")

    started = time.monotonic()
    try:
        # Стриминг обязателен при большом max_tokens: иначе HTTP-запрос упрётся в таймаут.
        with client.beta.messages.stream(**request) as stream:
            message = stream.get_final_message()
    except anthropic.AuthenticationError as exc:
        raise LLMError("Неверный ключ Anthropic API (ANTHROPIC_API_KEY)") from exc
    except anthropic.PermissionDeniedError as exc:
        raise LLMError("У ключа API нет доступа к модели " + settings.llm_model) from exc
    except anthropic.RateLimitError as exc:
        raise LLMError("Превышен лимит запросов к Anthropic API, повторите позже") from exc
    except anthropic.BadRequestError as exc:
        if strict and _is_schema_too_complex(exc):
            raise _SchemaRejected(exc.message) from exc
        raise LLMError(f"Запрос отклонён API: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise LLMError(f"Ошибка Anthropic API ({exc.status_code}), повторите позже") from exc
    except anthropic.APIConnectionError as exc:
        raise LLMError("Нет соединения с Anthropic API") from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    # stop_reason проверяем до чтения ответа: при отказе или обрезке JSON может быть неполным.
    if message.stop_reason == "refusal":
        raise LLMError("Модель отказалась обрабатывать документы. Проверьте содержимое файлов.")
    if message.stop_reason == "max_tokens":
        raise LLMError("Ответ модели обрезан: документов слишком много. Увеличьте LLM_MAX_TOKENS.")

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
        raise LLMError("На сервере не задан ключ OPENAI_API_KEY. Обратитесь к администратору.")
    return openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url or None)


def _call_openai_compatible(
    reference: ParsedDocument, commercial: list[ParsedDocument]
) -> tuple[str, LLMCallInfo]:
    """Chat Completions API: OpenAI и сервисы с таким же интерфейсом (адрес — OPENAI_BASE_URL).

    Сначала просим ответ строго по JSON Schema; если сервис такой режим не поддерживает
    (ошибка 400), повторяем в простом JSON-режиме со схемой в тексте запроса. Ответ в любом
    случае проверяется той же Pydantic-моделью, что и для Claude.
    """
    settings = get_settings()
    if not settings.openai_model:
        raise LLMError("Не задана модель OPENAI_MODEL. Обратитесь к администратору.")
    scans = [d.name for d in (reference, *commercial) if not d.has_text_layer]
    if scans:
        raise LLMError(f"Файл «{scans[0]}» — скан без текста. Сканы обрабатываются только через Claude "
                       "(LLM_PROVIDER=anthropic); либо загрузите PDF с текстовым слоем или Excel.")
    user_text = build_user_content(reference, commercial)[-1]["text"]
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
            "json_schema": {"name": "declaration_extraction", "schema": OUTPUT_SCHEMA, "strict": True},
        },
    }
    json_mode_request = {
        **base,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text + SCHEMA_INSTRUCTION},
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
        raise LLMError("Неверный ключ OPENAI_API_KEY") from exc
    except openai.PermissionDeniedError as exc:
        raise LLMError(f"У ключа нет доступа к модели {settings.openai_model}") from exc
    except openai.NotFoundError as exc:
        raise LLMError(f"Модель «{settings.openai_model}» не найдена: "
                       "проверьте OPENAI_MODEL и OPENAI_BASE_URL") from exc
    except openai.RateLimitError as exc:
        raise LLMError("Превышен лимит запросов или закончились деньги на балансе провайдера") from exc
    except openai.BadRequestError as exc:
        raise LLMError(f"Запрос отклонён API: {exc.message}") from exc
    except openai.APIStatusError as exc:
        raise LLMError(f"Ошибка API провайдера ({exc.status_code}), повторите позже") from exc
    except openai.APIConnectionError as exc:
        raise LLMError("Нет соединения с API провайдера (проверьте OPENAI_BASE_URL)") from exc
    duration_ms = int((time.monotonic() - started) * 1000)

    if not response.choices:
        raise LLMError("Провайдер вернул пустой ответ, повторите обработку")
    choice = response.choices[0]
    if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
        raise LLMError("Модель отказалась обрабатывать документы. Проверьте содержимое файлов.")
    if choice.finish_reason == "length":
        raise LLMError("Ответ модели обрезан: увеличьте OPENAI_MAX_TOKENS или разделите поставку.")

    usage = response.usage
    info = LLMCallInfo(
        model=response.model or settings.openai_model,
        input_tokens=(usage.prompt_tokens if usage else 0) or 0,
        output_tokens=(usage.completion_tokens if usage else 0) or 0,
        duration_ms=duration_ms,
    )
    return choice.message.content or "", info
