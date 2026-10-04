"""Таблица для проверки грузинского перевода носителем языка.

    python scripts/ka_review.py > docs/ka-review.md

Строки берутся из app/i18n.py. Риск — грубая оценка по словам в русском тексте:
таможенные термины ошибочно перевести опаснее всего, затем сообщения об ошибках и проверках.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.i18n import _RAW  # noqa: E402

# Таможенные и юридически значимые термины (в нижнем регистре, по началу слова)
CUSTOMS_TERMS = (
    "деклар", "тн вэд", "таможен", "эталон", "инвойс", "упаковоч", "брутто", "нетто", "происхожд", "граф",
    "курс", "пошлин", "налог", "asycuda", "процедур", "отправит", "получат", "брокер", "инкотермс", "мест",
    "код", "валют", "контракт", "стоимост", "позици", "вес", "склад", "коносамент", "документ", "импорт",
    "экспорт", "поставк", "страна", "единиц",
)
MESSAGE_PREFIXES = ("val.", "llm.", "asy.", "export.", "parse.", "job.", "wire.", "reg.", "auth.", "limit.")
RISK_ORDER = {"высокий": 0, "средний": 1, "низкий": 2}


def risk(key: str, ru: str) -> str:
    text = ru.lower()
    if any(term in text for term in CUSTOMS_TERMS):
        return "высокий"
    if key.startswith(MESSAGE_PREFIXES) or "ошибк" in text:
        return "средний"
    return "низкий"


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def main() -> None:
    rows = sorted(((risk(k, ru), k, ru, ka) for k, (ru, _en, ka) in _RAW.items()),
                  key=lambda r: (RISK_ORDER[r[0]], r[1]))
    counts = {level: sum(1 for r in rows if r[0] == level) for level in RISK_ORDER}
    print("# Проверка грузинского перевода\n")
    print("Файл создан командой `python scripts/ka_review.py > docs/ka-review.md` из `app/i18n.py`.")
    print("Грузинские тексты писал Claude — носитель языка их не проверял. Риск — грубая оценка: "
          "**высокий** — таможенные термины (декларация, код ТН ВЭД, графа, курс, процедура…), "
          "**средний** — сообщения об ошибках и проверках, **низкий** — кнопки и подписи.\n")
    print("Как прислать исправления: в столбце «грузинский» поправьте текст (или выпишите «ключ → новый текст») "
          "и передайте разработчику; переводы лежат в `app/i18n.py`, третий элемент каждой строки. "
          "Фигурные скобки `{…}` — подстановки, их не переводить и не удалять.\n")
    print(f"Всего строк: {len(rows)} (высокий риск — {counts['высокий']}, средний — {counts['средний']}, "
          f"низкий — {counts['низкий']}).\n")
    print("| ключ | русский | грузинский | риск |")
    print("|---|---|---|---|")
    for level, key, ru, ka in rows:
        print(f"| `{key}` | {cell(ru)} | {cell(ka)} | {level} |")


if __name__ == "__main__":
    main()
