"""Проверяет, принимает ли Claude API строгую схему ответа (стоит доли цента).

    python scripts/check_llm_schema.py
    # в Docker: docker compose exec <сервис> python scripts/check_llm_schema.py

Отправляет крошечный запрос с той же JSON Schema, что и сайт. Схема компилируется
до генерации, поэтому ответ «принята» или «отклонена» приходит сразу. Если схема
отклонена, сайт всё равно работает — в запасном режиме (JSON по инструкции).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services.llm import OUTPUT_SCHEMA  # noqa: E402


def main() -> None:
    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key \
        else anthropic.Anthropic()
    try:
        message = client.messages.create(
            model=settings.llm_model,
            max_tokens=64,  # ответ не нужен: важно только, скомпилируется ли схема
            messages=[{"role": "user", "content": "Это проверка схемы. Верни любой пример."}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
    except anthropic.BadRequestError as exc:
        print(f"Схема ОТКЛОНЕНА: {exc.message}")
        print("Сайт будет работать в запасном режиме (JSON по инструкции).")
        sys.exit(1)
    except anthropic.APIError as exc:
        sys.exit(f"Не удалось проверить: {exc}")
    print(f"Схема ПРИНЯТА моделью {message.model} (stop_reason={message.stop_reason}, "
          f"входных токенов: {message.usage.input_tokens}, выходных: {message.usage.output_tokens})")


if __name__ == "__main__":
    main()
