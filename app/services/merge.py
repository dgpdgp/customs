"""Сборка черновика новой декларации из ответа LLM.

Ключевая гарантия: реквизиты (header) копируются из эталона кодом, а не
генерируются моделью, — фирма, брокер и контракт гарантированно остаются те же.
"""

from collections import OrderedDict
from decimal import Decimal

from app.i18n import t
from app.schemas import DeclarationData, GoodsItem, Issue, LLMExtractionResult


def build_proposed(result: LLMExtractionResult, group_by_hs: bool = False) -> tuple[DeclarationData, list[Issue]]:
    """Возвращает черновик и замечания LLM (с номерами позиций после перенумерации)."""
    items = [item.model_copy(update={"item_no": index}) for index, item in enumerate(result.new_items, start=1)]
    # LLM ссылается на позиции по своим item_no — переводим в новую нумерацию.
    renumber = {orig.item_no: index for index, orig in enumerate(result.new_items, start=1)}

    extra_issues: list[Issue] = []
    if group_by_hs:
        items, group_map, extra_issues = group_items_by_hs_code(items)
        renumber = {orig: group_map[new] for orig, new in renumber.items()}

    proposed = DeclarationData(
        header=result.reference.header.model_copy(deep=True),
        shipment=result.new_shipment,
        items=items,
    )
    llm_issues = [
        Issue(
            severity=i.severity,
            item_no=renumber.get(i.item_no) if i.item_no is not None else None,
            field=i.field,
            message=i.message,
            source="llm",
        )
        for i in result.issues
    ]
    return proposed, llm_issues + extra_issues


def _sum(values: list[float | int | None]) -> float | None:
    """Сумма без погрешностей float; None, если хоть одно значение неизвестно."""
    if any(v is None for v in values):
        return None
    total = sum((Decimal(str(v)) for v in values), Decimal(0))
    return float(total)


def group_items_by_hs_code(items: list[GoodsItem]) -> tuple[list[GoodsItem], dict[int, int], list[Issue]]:
    """Объединяет позиции с одинаковыми кодом ТН ВЭД и страной происхождения
    (так часто требуют правила заполнения: одна позиция декларации = один код).

    Арифметику выполняет код, а не LLM. Позиции без кода не объединяются.
    Возвращает: новые позиции, карту «старый item_no -> новый item_no», замечания.
    """
    groups: OrderedDict[tuple, list[GoodsItem]] = OrderedDict()
    for item in items:
        key = (item.hs_code, item.country_of_origin) if item.hs_code else ("__single__", item.item_no)
        groups.setdefault(key, []).append(item)

    grouped: list[GoodsItem] = []
    mapping: dict[int, int] = {}
    issues: list[Issue] = []
    for new_no, members in enumerate(groups.values(), start=1):
        for member in members:
            mapping[member.item_no] = new_no
        first = members[0]
        if len(members) == 1:
            grouped.append(first.model_copy(update={"item_no": new_no}))
            continue

        units = {m.unit for m in members}
        same_unit = len(units) == 1
        if not same_unit:
            issues.append(Issue(severity="warning", item_no=new_no, field="quantity", source="validator",
                                message=t("merge.units_differ", units=", ".join(map(str, units)))))
        descriptions = list(OrderedDict.fromkeys(m.description for m in members if m.description))
        articles = list(OrderedDict.fromkeys(m.article for m in members if m.article))
        grouped.append(
            GoodsItem(
                item_no=new_no,
                description="; ".join(descriptions) or None,
                article=", ".join(articles) or None,
                hs_code=first.hs_code,
                hs_code_basis=first.hs_code_basis,
                reference_item_no=first.reference_item_no,
                country_of_origin=first.country_of_origin,
                quantity=_sum([m.quantity for m in members]) if same_unit else None,
                unit=first.unit if same_unit else None,
                packages=(int(p) if (p := _sum([m.packages for m in members])) is not None else None),
                gross_weight_kg=_sum([m.gross_weight_kg for m in members]),
                net_weight_kg=_sum([m.net_weight_kg for m in members]),
                unit_price=None,  # у объединённой позиции единой цены нет
                total_value=_sum([m.total_value for m in members]),
                source_document=first.source_document,
                source_quote=first.source_quote,
            )
        )
        issues.append(Issue(severity="info", item_no=new_no, source="validator",
                            message=t("merge.merged", nos=", ".join(str(m.item_no) for m in members))))
    return grouped, mapping, issues
