/**
 * Diff View: сравнение эталонной декларации (слева) и новой (справа).
 *
 * Логика разделена на две части:
 *   1. Чистые функции сравнения (normalize, compareValues, alignItems, buildDiff) —
 *      без DOM, покрыты тестами в tests/js/diff.test.mjs.
 *   2. Отрисовка (renderDiffView) — строит таблицы через DOM API. Все данные
 *      вставляются через textContent: значения пришли из документов и LLM,
 *      поэтому innerHTML с ними не используется (защита от XSS).
 *
 * Статусы строк:
 *   same        — значение не изменилось
 *   changed     — изменилось (жёлтая подсветка, для чисел показывается разница)
 *   added       — было пусто, стало заполнено (зелёная)
 *   removed     — было заполнено, стало пусто (красная)
 *   unexpected  — изменился постоянный реквизит (красная, требует внимания)
 */
(function (root) {
  "use strict";

  // ---------- Описание полей ----------

  const PARTY_FIELDS = [["name", "наименование"], ["address", "адрес"], ["country", "страна"], ["tax_id", "ИНН / TIN"]];
  const partyFields = (key, title) => PARTY_FIELDS.map(([f, l]) => [`${key}.${f}`, `${title}: ${l}`, "text"]);

  const HEADER_FIELDS = [
    ["declaration_type", "Тип декларации", "text"],
    ["customs_office", "Таможенный пост", "text"],
    ...partyFields("exporter", "Отправитель"),
    ...partyFields("importer", "Получатель"),
    ...partyFields("declarant", "Декларант / брокер"),
    ["contract_number", "Контракт №", "text"],
    ["contract_date", "Дата контракта", "text"],
    ["delivery_terms", "Инкотермс", "text"],
    ["delivery_place", "Пункт поставки", "text"],
    ["currency", "Валюта", "text"],
    ["country_of_dispatch", "Страна отправления", "text"],
    ["country_of_destination", "Страна назначения", "text"],
    ["transport_mode", "Вид транспорта", "text"],
  ];

  const SHIPMENT_FIELDS = [
    ["invoice_numbers", "Инвойсы", "list"],
    ["invoice_date", "Дата инвойса", "text"],
    ["transport_document", "Транспортный документ", "text"],
    ["vehicle_id", "Номер ТС", "text"],
    ["total_invoice_value", "Сумма по инвойсу", "number"],
    ["total_packages", "Мест всего", "int"],
    ["total_gross_weight_kg", "Брутто итого, кг", "number"],
    ["total_net_weight_kg", "Нетто итого, кг", "number"],
  ];

  const ITEM_FIELDS = [
    ["description", "Описание", "text"],
    ["article", "Артикул", "text"],
    ["hs_code", "Код ТН ВЭД", "text"],
    ["country_of_origin", "Страна происхождения", "text"],
    ["quantity", "Количество", "number"],
    ["unit", "Единица", "text"],
    ["packages", "Мест", "int"],
    ["gross_weight_kg", "Брутто, кг", "number"],
    ["net_weight_kg", "Нетто, кг", "number"],
    ["unit_price", "Цена за ед.", "number"],
    ["total_value", "Стоимость", "number"],
  ];

  const HS_BASIS_LABELS = {
    document: "код из документа",
    reference_match: "код по эталону",
    not_found: "код не найден",
  };

  // ---------- Чистые функции ----------

  function getPath(obj, path) {
    return path.split(".").reduce((acc, key) => (acc == null ? undefined : acc[key]), obj);
  }

  function setPath(obj, path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    const target = keys.reduce((acc, key) => (acc[key] ??= {}), obj);
    target[last] = value;
  }

  /** Приводит значение к сравнимому виду: пустые значения -> null, пробелы схлопнуты. */
  function normalize(value) {
    if (value === undefined || value === null) return null;
    if (Array.isArray(value)) return value.length ? value.map(normalize).join(", ") : null;
    if (typeof value === "number") return value;
    const text = String(value).replace(/\s+/g, " ").trim();
    return text === "" ? null : text;
  }

  function asNumber(value) {
    if (typeof value === "number") return value;
    if (typeof value !== "string" || value.trim() === "") return null;
    const n = Number(value.replace(/\s/g, "").replace(",", "."));
    return Number.isFinite(n) ? n : null;
  }

  /** Сравнивает старое и новое значение и возвращает статус и разницу для чисел. */
  function compareValues(oldValue, newValue, type = "text") {
    const a = normalize(oldValue);
    const b = normalize(newValue);
    if (a === null && b === null) return { status: "same", delta: null };
    if (a === null) return { status: "added", delta: null };
    if (b === null) return { status: "removed", delta: null };

    if (type === "number" || type === "int") {
      const x = asNumber(a);
      const y = asNumber(b);
      if (x !== null && y !== null) {
        if (Math.abs(x - y) < 1e-9) return { status: "same", delta: null };
        const diff = y - x;
        return { status: "changed", delta: { abs: diff, pct: x !== 0 ? (diff / Math.abs(x)) * 100 : null } };
      }
    }
    return { status: String(a) === String(b) ? "same" : "changed", delta: null };
  }

  /**
   * Сопоставляет новые позиции с позициями эталона.
   * Пара образуется только по reference_item_no (тот же товар, найденный LLM),
   * а не по порядку: товары в новой поставке другие, и сравнение «по номеру
   * строки» подсветило бы всё подряд и скрыло бы реальные совпадения.
   */
  function alignItems(referenceItems, newItems) {
    const byNo = new Map((referenceItems || []).map((item) => [item.item_no, item]));
    const used = new Set();
    const rows = (newItems || []).map((item, index) => {
      const ref = item.reference_item_no != null ? byNo.get(item.reference_item_no) : undefined;
      if (ref) used.add(ref.item_no);
      return { kind: ref ? "matched" : "new", ref: ref || null, cur: item, index };
    });
    for (const ref of referenceItems || []) {
      if (!used.has(ref.item_no)) rows.push({ kind: "removed", ref, cur: null, index: null });
    }
    return rows;
  }

  function diffFields(oldObj, newObj, fields, { constant = false } = {}) {
    return fields.map(([path, label, type]) => {
      const oldValue = oldObj ? getPath(oldObj, path) : null;
      const newValue = newObj ? getPath(newObj, path) : null;
      const result = compareValues(oldValue, newValue, type);
      // Постоянный реквизит не должен меняться — любое изменение помечаем особо.
      const status = constant && result.status !== "same" ? "unexpected" : result.status;
      return { path, label, type, oldValue, newValue, status, delta: result.delta };
    });
  }

  /** Полный дифф двух деклараций + сводка для шапки страницы. */
  function buildDiff(reference, proposed) {
    const header = diffFields(reference.header, proposed.header, HEADER_FIELDS, { constant: true });
    const shipment = diffFields(reference.shipment, proposed.shipment, SHIPMENT_FIELDS);
    const items = alignItems(reference.items, proposed.items).map((row) => {
      let cells = diffFields(row.ref, row.cur, ITEM_FIELDS);
      if (row.kind === "new") cells = cells.map((c) => ({ ...c, status: c.newValue == null ? "same" : "added" }));
      if (row.kind === "removed") cells = cells.map((c) => ({ ...c, status: c.oldValue == null ? "same" : "removed" }));
      return { ...row, cells };
    });
    return { header, shipment, items, summary: summarize(header, shipment, items) };
  }

  function summarize(header, shipment, items) {
    const changed = (rows) => rows.filter((r) => r.status !== "same").length;
    return {
      headerChanged: changed(header),
      shipmentChanged: changed(shipment),
      itemsMatched: items.filter((r) => r.kind === "matched").length,
      itemsNew: items.filter((r) => r.kind === "new").length,
      itemsRemoved: items.filter((r) => r.kind === "removed").length,
      itemCellsChanged: items.filter((r) => r.kind === "matched").reduce((n, r) => n + changed(r.cells), 0),
    };
  }

  /** Разбор значения, введённого декларантом. Возвращает {ok, value}. */
  function parseInput(raw, type) {
    const text = raw.trim();
    if (type === "list") return { ok: true, value: text ? text.split(",").map((s) => s.trim()).filter(Boolean) : [] };
    if (text === "") return { ok: true, value: null };
    if (type === "number" || type === "int") {
      const n = asNumber(text);
      if (n === null || (type === "int" && !Number.isInteger(n))) return { ok: false, value: null };
      return { ok: true, value: n };
    }
    return { ok: true, value: text };
  }

  function formatValue(value, type) {
    if (value === null || value === undefined || value === "") return "";
    if (type === "list") return Array.isArray(value) ? value.join(", ") : String(value);
    if (type === "number" && typeof value === "number") return String(Math.round(value * 1000) / 1000);
    return String(value);
  }

  function formatDelta(delta) {
    if (!delta) return "";
    const sign = delta.abs > 0 ? "+" : "−";
    const abs = Math.round(Math.abs(delta.abs) * 1000) / 1000;
    const pct = delta.pct === null ? "" : ` (${sign}${Math.abs(delta.pct).toFixed(1)}%)`;
    return `${sign}${abs}${pct}`;
  }

  // ---------- Отрисовка ----------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function issuesIndex(issues) {
    const index = new Map();
    for (const issue of issues || []) {
      const key = `${issue.item_no ?? ""}|${issue.field ?? ""}`;
      if (!index.has(key)) index.set(key, []);
      index.get(key).push(issue);
    }
    return index;
  }

  function allSameNote(tbody) {
    const tr = el("tr", "diff-all-same");
    const td = el("td", "", "Изменений нет — все значения совпадают с эталоном");
    td.colSpan = 3;
    tr.append(td);
    tbody.append(tr);
  }

  function sectionTable(title, subtitle) {
    const section = el("section", "diff-section");
    const head = el("div", "diff-section-head");
    head.append(el("h2", "diff-section-title", title));
    if (subtitle) head.append(el("p", "diff-section-subtitle", subtitle));
    section.append(head);
    const table = el("table", "diff-table");
    const thead = el("thead");
    const tr = el("tr");
    tr.append(el("th", "diff-col-label", "Поле"), el("th", "diff-col-old", "Эталон (старая декларация)"),
      el("th", "diff-col-new", "Новая декларация"));
    thead.append(tr);
    const tbody = el("tbody");
    table.append(thead, tbody);
    const wrapper = el("div", "diff-table-wrap");
    wrapper.append(table);
    section.append(wrapper);
    return { section, tbody };
  }

  /**
   * Строка «поле | старое | новое». Новое значение редактируемое (если задан target):
   * при вводе пересчитываем статус строки на месте, без перерисовки всей страницы.
   */
  function fieldRow(cell, { target, constant, issues, editable, onEdit, extra }) {
    const tr = el("tr", `diff-row diff-${cell.status}`);
    const label = el("td", "diff-label", cell.label);
    const oldTd = el("td", "diff-old", formatValue(cell.oldValue, cell.type) || "—");
    const newTd = el("td", "diff-new");
    const deltaEl = el("span", "diff-delta", formatDelta(cell.delta));

    if (editable) {
      const input = el(cell.path === "description" ? "textarea" : "input", "diff-input");
      input.value = formatValue(cell.newValue, cell.type);
      if (cell.type === "number" || cell.type === "int") input.inputMode = "decimal";
      input.setAttribute("aria-label", cell.label);
      input.addEventListener("input", () => {
        const parsed = parseInput(input.value, cell.type);
        input.classList.toggle("diff-invalid", !parsed.ok);
        if (parsed.ok) {
          setPath(target, cell.path, parsed.value);
          const result = compareValues(cell.oldValue, parsed.value, cell.type);
          const status = constant && result.status !== "same" ? "unexpected" : result.status;
          tr.className = `diff-row diff-${status}` + (tr.dataset.hasError ? " diff-has-error" : "");
          deltaEl.textContent = formatDelta(result.delta);
          tr.dispatchEvent(new CustomEvent("diff-edit", { detail: { value: parsed.value } }));
        }
        onEdit(input, parsed.ok);
      });
      newTd.append(input);
    } else {
      newTd.append(el("span", "", formatValue(cell.newValue, cell.type) || "—"));
    }
    newTd.append(deltaEl);
    if (extra) newTd.append(extra);

    for (const issue of issues || []) {
      newTd.append(el("div", `diff-issue diff-issue-${issue.severity}`, issue.message));
      if (issue.severity === "error") {
        tr.classList.add("diff-has-error");
        tr.dataset.hasError = "1";
      }
    }
    tr.append(label, oldTd, newTd);
    return tr;
  }

  function renderSummary(container, summary, issues) {
    container.replaceChildren();
    const errors = (issues || []).filter((i) => i.severity === "error").length;
    const warnings = (issues || []).filter((i) => i.severity === "warning").length;
    const chips = [
      [summary.headerChanged === 0 ? "ok" : "bad",
        summary.headerChanged === 0 ? "Реквизиты совпадают с эталоном" : `Изменено реквизитов: ${summary.headerChanged}`],
      ["info", `Изменено полей поставки: ${summary.shipmentChanged}`],
      ["info", `Позиций сопоставлено с эталоном: ${summary.itemsMatched}`],
      [summary.itemsNew ? "warn" : "info", `Новых позиций без аналога в эталоне: ${summary.itemsNew}`],
      ["info", `Позиций эталона нет в поставке: ${summary.itemsRemoved}`],
      [errors ? "bad" : "ok", `Ошибок: ${errors}`],
      [warnings ? "warn" : "ok", `Предупреждений: ${warnings}`],
    ];
    for (const [tone, text] of chips) container.append(el("span", `diff-chip diff-chip-${tone}`, text));
  }

  /**
   * Рисует Diff View.
   * @param {HTMLElement} container — куда рисовать
   * @param {object} options
   *   reference — эталон (DeclarationData), proposed — редактируемая копия черновика,
   *   issues — замечания, summaryContainer — элемент для сводки,
   *   editable — разрешить правки, onValidityChange(hasInvalid) — для кнопки «Утвердить».
   */
  function renderDiffView(container, options) {
    const { reference, proposed, issues = [], summaryContainer, editable = true, onValidityChange } = options;
    const diff = buildDiff(reference, proposed);
    const index = issuesIndex(issues);
    const invalidInputs = new Set();

    const refreshSummary = () => {
      if (summaryContainer) renderSummary(summaryContainer, buildDiff(reference, proposed).summary, issues);
    };
    const onEdit = (input, ok) => {
      if (ok) invalidInputs.delete(input); else invalidInputs.add(input);
      if (onValidityChange) onValidityChange(invalidInputs.size > 0);
      if (ok) refreshSummary();
    };
    // Замечания, уже показанные рядом с полями; остальные выводятся отдельным блоком.
    const renderedKeys = new Set();
    const take = (key) => {
      renderedKeys.add(key);
      return index.get(key) || [];
    };

    container.replaceChildren();

    // 1. Постоянные реквизиты: должны совпадать
    const head = sectionTable("Реквизиты и условия поставки", "Переносятся из эталона и должны совпадать полностью");
    for (const cell of diff.header) {
      head.tbody.append(fieldRow(cell, {
        target: proposed.header, constant: true, editable, onEdit,
        issues: take(`|header.${cell.path}`),
      }));
    }
    allSameNote(head.tbody);
    container.append(head.section);

    // 2. Данные поставки: ожидаемо меняются
    const ship = sectionTable("Данные поставки", "Берутся из новых инвойсов и упаковочных листов");
    for (const cell of diff.shipment) {
      ship.tbody.append(fieldRow(cell, {
        target: proposed.shipment, editable, onEdit,
        issues: take(`|shipment.${cell.path}`),
      }));
    }
    allSameNote(ship.tbody);
    container.append(ship.section);

    // 3. Товарные позиции
    const goods = sectionTable("Товарные позиции", "Слева — сопоставленная позиция эталона (тот же товар), справа — новая позиция");
    for (const row of diff.items) {
      const titleRow = el("tr", `diff-item-title diff-item-${row.kind}`);
      const titleTd = el("td");
      titleTd.colSpan = 3;
      const title = row.kind === "removed"
        ? `Позиция эталона №${row.ref.item_no} — нет в новой поставке`
        : row.kind === "matched"
          ? `Позиция №${row.cur.item_no} ↔ позиция эталона №${row.ref.item_no}`
          : `Позиция №${row.cur.item_no} — новый товар, аналога в эталоне нет`;
      titleTd.append(el("strong", "", title));
      if (row.cur && row.cur.source_document) {
        const quote = row.cur.source_quote ? ` — «${row.cur.source_quote}»` : "";
        titleTd.append(el("div", "diff-source", `Источник: ${row.cur.source_document}${quote}`));
      }
      if (row.cur) {
        // Замечания к позиции в целом и к полям, которых нет в таблице (источник, сопоставление)
        for (const field of ["", "source_quote", "source_document", "reference_item_no", "hs_code_basis"]) {
          for (const issue of take(`${row.cur.item_no}|${field}`)) {
            titleTd.append(el("div", `diff-issue diff-issue-${issue.severity}`, issue.message));
          }
        }
      }
      titleRow.append(titleTd);
      goods.tbody.append(titleRow);

      for (const cell of row.cells) {
        let extra = null;
        if (cell.path === "hs_code" && row.cur) {
          const basis = HS_BASIS_LABELS[row.cur.hs_code_basis] || "";
          extra = el("span", `diff-basis diff-basis-${row.cur.hs_code_basis}`, basis);
        }
        const tr = fieldRow(cell, {
          target: row.cur, editable: editable && row.kind !== "removed", onEdit, extra,
          issues: row.cur ? take(`${row.cur.item_no}|${cell.path}`) : null,
        });
        if (extra) {
          // Код, исправленный декларантом, помечаем как ручной ввод
          const original = { text: extra.textContent, className: extra.className, value: cell.newValue };
          tr.addEventListener("diff-edit", (event) => {
            const manual = normalize(event.detail.value) !== normalize(original.value);
            extra.textContent = manual ? "введён вручную" : original.text;
            extra.className = manual ? "diff-basis diff-basis-manual" : original.className;
          });
        }
        goods.tbody.append(tr);
      }
    }
    container.append(goods.section);

    // 4. Замечания без привязки к конкретному полю
    const orphan = (issues || []).filter((issue) => !renderedKeys.has(`${issue.item_no ?? ""}|${issue.field ?? ""}`));
    if (orphan.length) {
      const box = el("section", "diff-section");
      box.append(el("h2", "diff-section-title", "Общие замечания"));
      for (const issue of orphan) {
        const where = [issue.item_no != null ? `позиция №${issue.item_no}` : "", issue.field || ""].filter(Boolean);
        const prefix = where.length ? `${where.join(", ")}: ` : "";
        box.append(el("div", `diff-issue diff-issue-${issue.severity}`, prefix + issue.message));
      }
      container.prepend(box);
    }

    refreshSummary();
    return diff;
  }

  const api = {
    HEADER_FIELDS, SHIPMENT_FIELDS, ITEM_FIELDS,
    getPath, setPath, normalize, compareValues, alignItems, buildDiff, parseInput, formatValue, formatDelta,
    renderDiffView,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api; // Node (тесты)
  else root.DiffView = api; // браузер
})(typeof window !== "undefined" ? window : globalThis);
