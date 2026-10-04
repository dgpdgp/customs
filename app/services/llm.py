"""Вызов Claude API для извлечения и замены товарных позиций."""

import base64
import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from xml.sax.saxutils import quoteattr

import anthropic
from pydantic import ValidationError

from app.config import get_settings
from app.schemas import LLMExtractionResult
from app.services.parsers import ParsedDocument
from app.services.prompts import SYSTEM_PROMPT, USER_INSTRUCTION

logger = logging.getLogger(__name__)

# JSON Schema ответа строится из Pydantic-модели. transform_schema приводит её
# к требованиям структурированного вывода API (additionalProperties: false и т. д.).
OUTPUT_SCHEMA = anthropic.transform_schema(LLMExtractionResult)

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
    settings = get_settings()
    request: dict = {
        "model": settings.llm_model,
        "max_tokens": settings.llm_max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": build_user_content(reference, commercial)}],
        "output_config": {
            "effort": settings.llm_effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
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
    try:
        result = LLMExtractionResult.model_validate_json(text)
    except ValidationError as exc:
        logger.error("Ответ LLM не прошёл валидацию: %s\n%s", exc, text[:2000])
        raise LLMError("Модель вернула данные в неожиданном формате, повторите обработку") from exc

    info = LLMCallInfo(
        model=message.model,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        duration_ms=duration_ms,
    )
    logger.info("LLM: %s", json.dumps(info.__dict__))
    return result, info
