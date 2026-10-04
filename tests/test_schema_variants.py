"""Варианты схемы ответа ИИ: каждый укладывается в лимиты строгого режима и даёт тот же результат."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import llm, schema_variants
from app.services.wire import WireResult
from tests.fake_llm import fake_extraction_result
from tests.test_llm import DOCS, FakeStream, make_message

ROOT = Path(__file__).resolve().parent.parent


def _wire() -> dict:
    """Ответ «идеальной» модели в формате варианта full."""
    return WireResult.model_validate_json(fake_extraction_result().model_dump_json()).model_dump()


def _row(item: dict, columns) -> list[str]:
    return [str(item[c]) for c in columns]


def _payloads(key: str) -> list[dict]:
    """Ответы модели для варианта key — по одному на вызов."""
    wire = _wire()
    ref, new = wire["reference"], wire
    if key in ("full", "a"):
        return [wire]
    if key == "b":
        drop = ("item_no", "unit_price")
        return [{
            "reference": {"header": ref["header"],
                          "shipment": {k: ref["shipment"][k] for k in ("invoice_numbers", "invoice_date",
                                                                       "transport_document")},
                          "items": [{k: v for k, v in i.items() if k not in drop} for i in ref["items"]]},
            "new_shipment": new["new_shipment"],
            "new_items": [{k: v for k, v in i.items() if k not in drop} for i in new["new_items"]],
            "issues": new["issues"],
        }]
    ref_rows = [_row(i, schema_variants.REFERENCE_COLUMNS) for i in ref["items"]]
    new_rows = [_row(i, schema_variants.NEW_COLUMNS) for i in new["new_items"]]
    if key == "c":
        return [{"reference": ref, "issues": []},
                {"new_shipment": new["new_shipment"], "new_items": new["new_items"], "issues": new["issues"]}]
    if key == "d":
        return [{"reference": {**ref, "items": ref_rows}, "new_shipment": new["new_shipment"],
                 "new_items": new_rows, "issues": new["issues"]}]
    if key == "e":
        return [{"reference": {**ref, "items": ref_rows}, "issues": []},
                {"new_shipment": new["new_shipment"], "new_items": new_rows, "issues": new["issues"]}]
    raise AssertionError(key)


def _client(responses: list, calls: list):
    from types import SimpleNamespace

    def stream(**kwargs):
        calls.append(kwargs)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeStream(result)

    return SimpleNamespace(api_key="k", auth_token=None, credentials=None,
                           beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))


def _run(monkeypatch, key: str, responses: list, calls: list):
    monkeypatch.setattr(llm.get_settings(), "llm_schema_variant", key)
    monkeypatch.setattr(llm, "_rejected_schemas", set())
    monkeypatch.setattr(llm, "_client", lambda: _client(responses, calls))
    return llm.extract_declaration(*DOCS)


@pytest.mark.parametrize("key", schema_variants.VARIANT_KEYS)
def test_every_variant_fits_documented_limits(key):
    for part in schema_variants.get(key).parts:
        stats = schema_variants.stats(part.schema)
        assert stats["unions"] == 0 and stats["optional"] == 0, (key, part.name, stats)
        assert '"additionalProperties": false' in json.dumps(part.schema)


def test_variants_are_smaller_than_full():
    fields = {key: max(schema_variants.stats(p.schema)["fields"] for p in v.parts)
              for key, v in schema_variants.VARIANTS.items()}
    assert fields["b"] < fields["full"] and fields["c"] < fields["full"] and fields["d"] < fields["full"]
    assert fields["e"] < min(fields["c"], fields["d"])
    a = json.dumps(schema_variants.get("a").parts[0].schema)
    assert '"description": {' in a  # поле позиции «description» на месте
    assert '"description": "' not in a and '"title"' not in a  # а служебные описания убраны


@pytest.mark.parametrize("key", schema_variants.VARIANT_KEYS)
def test_every_variant_gives_the_same_result(monkeypatch, key):
    calls = []
    payloads = _payloads(key)
    result, info = _run(monkeypatch, key, [make_message(text=json.dumps(p)) for p in payloads], calls)

    expected = llm.parse_model_output(json.dumps(_wire())).model_dump()
    if key == "b":  # в сокращённом варианте этих полей нет
        for item in expected["reference"]["items"] + expected["new_items"]:
            item["unit_price"] = None
        for name in ("vehicle_id", "total_invoice_value", "total_packages", "total_gross_weight_kg",
                     "total_net_weight_kg"):
            expected["reference"]["shipment"][name] = None
    assert result.model_dump() == expected
    assert len(calls) == len(payloads)
    assert info.input_tokens == 1234 * len(payloads)  # расход суммируется по вызовам
    for call, part in zip(calls, schema_variants.get(key).parts, strict=True):
        assert call["output_config"]["format"]["schema"] == part.schema


def test_default_request_is_unchanged(monkeypatch):
    calls = []
    _run(monkeypatch, "full", [make_message()], calls)
    assert calls[0]["output_config"]["format"]["schema"] == llm.OUTPUT_SCHEMA
    text = calls[0]["messages"][0]["content"][-1]["text"]
    assert text.index("<reference_declaration") < text.index("<commercial_documents>")
    assert "<reference_extracted>" not in text and "шаг 1 из 2" not in text


def test_split_sends_reference_first_then_extracted_json(monkeypatch):
    calls = []
    _run(monkeypatch, "c", [make_message(text=json.dumps(p)) for p in _payloads("c")], calls)
    first = calls[0]["messages"][0]["content"][-1]["text"]
    second = calls[1]["messages"][0]["content"][-1]["text"]
    assert "<reference_declaration" in first and "<commercial_documents>" not in first
    assert "<reference_declaration" not in second and "<commercial_documents>" in second
    extracted = second.split("<reference_extracted>\n", 1)[1].split("\n</reference_extracted>", 1)[0]
    assert json.loads(extracted)["header"]["importer"]["name"] == "ООО «Альфа Импорт»"


def test_table_row_of_wrong_length_is_rejected(monkeypatch):
    payload = _payloads("d")[0]
    payload["new_items"][1] = payload["new_items"][1][:-1]  # модель пропустила колонку
    with pytest.raises(llm.LLMError, match="значений вместо 16"):
        _run(monkeypatch, "d", [make_message(text=json.dumps(payload))], [])


def test_unknown_variant_gives_readable_error(monkeypatch):
    with pytest.raises(llm.LLMError, match="Неизвестный вариант"):
        _run(monkeypatch, "zz", [], [])


def test_rejection_is_remembered_per_variant(monkeypatch):
    from tests.test_llm import bad_request

    calls = []
    grammar = bad_request("The compiled grammar is too large")
    _run(monkeypatch, "full", [grammar, make_message()], calls)
    assert "format" in calls[0]["output_config"] and "format" not in calls[1]["output_config"]
    assert llm._rejected_schemas == {"full:all"}
    # Другой вариант снова пробует строгий режим
    monkeypatch.setattr(llm.get_settings(), "llm_schema_variant", "d")
    monkeypatch.setattr(llm, "_client", lambda: _client([make_message(text=json.dumps(_payloads("d")[0]))], calls))
    llm.extract_declaration(*DOCS)
    assert "format" in calls[2]["output_config"]


def test_out_of_credit_message(monkeypatch):
    from tests.test_llm import bad_request

    error = bad_request("Your credit balance is too low to access the Anthropic API. "
                        "Please go to Plans & Billing to upgrade or purchase credits.")
    with pytest.raises(llm.LLMError, match="закончились деньги"):
        _run(monkeypatch, "full", [error], [])
    assert not llm._rejected_schemas  # это не отказ схемы


def test_check_script_stats_need_no_network():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_llm_schema.py"), "--stats"],
                            capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    for key in schema_variants.VARIANT_KEYS:
        assert any(line.startswith((f"{key} ", f"{key}:")) for line in lines), key
