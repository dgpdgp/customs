"""Проверяет, принимает ли Claude API строгую схему ответа (доли цента за проверку).

    python scripts/check_llm_schema.py                  # вариант из настроек (LLM_SCHEMA_VARIANT)
    python scripts/check_llm_schema.py --variant all    # все варианты по очереди
    python scripts/check_llm_schema.py --variant d      # один вариант
    python scripts/check_llm_schema.py --stats          # только размеры схем, без запросов (бесплатно)
    python scripts/check_llm_schema.py --probe          # схема full по частям: какая часть не проходит
    python scripts/check_llm_schema.py --variant all --model claude-sonnet-5-5
    # в Docker: docker compose exec <сервис> python scripts/check_llm_schema.py --variant all

Отправляет крошечный запрос с той же JSON Schema, что и сайт. Схема компилируется до генерации,
поэтому ответ «принята» или «отклонена» приходит сразу; отклонённый запрос не оплачивается.
Варианты схем — app/services/schema_variants.py. Если схема отклонена, сайт всё равно работает —
в запасном режиме (JSON по инструкции).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import schema_variants  # noqa: E402

CONTROL_SCHEMA = {"type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"],
                  "additionalProperties": False}


def _targets(args) -> list[tuple[str, dict]]:
    """(подпись, схема) — что проверять."""
    if args.probe:
        full = schema_variants.FULL_SCHEMA
        defs = full["$defs"]
        targets = [("control (1 поле)", CONTROL_SCHEMA)]
        for name in full["properties"]:
            section = {**full, "properties": {name: full["properties"][name]}, "required": [name]}
            targets.append((f"full: только {name}", section))
        for name, definition in defs.items():
            targets.append((f"full: $defs/{name}", {**definition, "$defs": defs}))
        return targets
    keys = (schema_variants.VARIANT_KEYS if args.variant == "all"
            else [args.variant or get_settings().llm_schema_variant])
    targets = []
    for key in keys:
        variant = schema_variants.get(key)
        for part in variant.parts:
            label = key if len(variant.parts) == 1 else f"{key}:{part.name}"
            targets.append((label, part.schema))
    return targets


def _describe(label: str, schema: dict) -> str:
    s = schema_variants.stats(schema)
    return (f"{label:<28} {s['bytes']:>6} байт  полей {s['fields']:>3}  объектов {s['objects']:>2}  "
            f"массивов {s['arrays']}  глубина {s['max_depth']}  описания {s['description_chars']} симв.")


def _check(client, model: str, schema: dict) -> tuple[bool | None, str]:
    import anthropic

    try:
        message = client.messages.create(
            model=model,
            max_tokens=64,  # ответ не нужен: важно только, скомпилируется ли схема
            messages=[{"role": "user", "content": "Это проверка схемы. Верни любой пример."}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.BadRequestError as exc:
        return False, str(exc.message)
    except anthropic.APIError as exc:
        return None, str(exc)
    return True, (f"модель {message.model}, stop_reason={message.stop_reason}, "
                  f"токенов {message.usage.input_tokens}/{message.usage.output_tokens}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка строгой схемы ответа на Claude API")
    parser.add_argument("--variant", choices=[*schema_variants.VARIANT_KEYS, "all"],
                        help="вариант схемы (по умолчанию — из настроек)")
    parser.add_argument("--model", help="модель (по умолчанию — LLM_MODEL)")
    parser.add_argument("--stats", action="store_true", help="только размеры схем, без запросов к API")
    parser.add_argument("--probe", action="store_true", help="проверить схему full по частям")
    args = parser.parse_args(argv)

    if args.stats:
        for key, variant in schema_variants.VARIANTS.items():
            for part in variant.parts:
                label = key if len(variant.parts) == 1 else f"{key}:{part.name}"
                print(_describe(label, part.schema))
            print(f"   {variant.summary}")
        return 0

    import anthropic

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key \
        else anthropic.Anthropic()
    model = args.model or settings.llm_model
    print(f"Модель: {model}\n")
    accepted, failed = [], False
    for label, schema in _targets(args):
        print(_describe(label, schema))
        ok, detail = _check(client, model, schema)
        if ok:
            accepted.append(label)
            print(f"   ПРИНЯТА ({detail})")
        elif ok is False:
            print(f"   ОТКЛОНЕНА: {detail}")
        else:
            failed = True
            print(f"   НЕ УДАЛОСЬ ПРОВЕРИТЬ: {detail}")
    print("\nПриняты: " + (", ".join(accepted) if accepted else "ни одна"))
    if not args.probe:
        print("Вариант включается настройкой LLM_SCHEMA_VARIANT=<вариант> в .env или в админ-панели "
              "(«Вариант схемы ответа ИИ»). Для варианта из двух вызовов нужны обе части.")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
