// Тесты чистых функций Diff View:  node --test tests/js/
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const D = require("../../app/static/js/diff.js");

const party = (name) => ({ name, address: null, country: null, tax_id: null });
const header = {
  declaration_type: "ИМ 40", customs_office: null, exporter: party("Beta GmbH"), importer: party("ООО «Альфа»"),
  declarant: party("ООО «Гамма»"), contract_number: "AB-17", contract_date: null, delivery_terms: "DAP",
  delivery_place: null, currency: "EUR", country_of_dispatch: null, country_of_destination: null, transport_mode: null,
};
const item = (no, extra) => ({
  item_no: no, description: "x", article: null, hs_code: "8482100009", hs_code_basis: "document",
  reference_item_no: null, country_of_origin: "DE", quantity: 1, unit: "шт", packages: 1, gross_weight_kg: 1,
  net_weight_kg: 1, unit_price: 1, total_value: 1, source_document: null, source_quote: null, ...extra,
});
const shipment = (total) => ({
  invoice_numbers: ["A"], invoice_date: null, transport_document: null, vehicle_id: null,
  total_invoice_value: total, total_packages: null, total_gross_weight_kg: null, total_net_weight_kg: null,
});

test("compareValues: статусы и разница для чисел", () => {
  assert.equal(D.compareValues("a", "a").status, "same");
  assert.equal(D.compareValues("ООО  Альфа ", "ООО Альфа").status, "same"); // пробелы не считаются изменением
  assert.equal(D.compareValues(null, "").status, "same");
  assert.equal(D.compareValues(null, "x").status, "added");
  assert.equal(D.compareValues("x", null).status, "removed");
  const changed = D.compareValues(100, 150, "number");
  assert.equal(changed.status, "changed");
  assert.deepEqual(changed.delta, { abs: 50, pct: 50 });
  assert.equal(D.compareValues(4800, "4800.0", "number").status, "same");
});

test("alignItems: пары только по reference_item_no, а не по порядку", () => {
  const rows = D.alignItems(
    [item(1), item(2)],
    [item(1, { reference_item_no: 2 }), item(2)],
  );
  assert.deepEqual(rows.map((r) => [r.kind, r.ref?.item_no ?? null, r.cur?.item_no ?? null]), [
    ["matched", 2, 1],
    ["new", null, 2],
    ["removed", 1, null],
  ]);
});

test("buildDiff: изменение постоянного реквизита помечается как unexpected", () => {
  const reference = { header, shipment: shipment(100), items: [item(1)] };
  const proposed = structuredClone(reference);
  proposed.header.importer.name = "ООО «Другая фирма»";
  proposed.shipment.total_invoice_value = 250;
  proposed.items = [item(1, { reference_item_no: 1, total_value: 5 })];

  const diff = D.buildDiff(reference, proposed);
  assert.equal(diff.header.find((r) => r.path === "importer.name").status, "unexpected");
  assert.equal(diff.summary.headerChanged, 1);
  assert.equal(diff.summary.shipmentChanged, 1);
  assert.equal(diff.summary.itemsMatched, 1);
  assert.equal(diff.summary.itemCellsChanged, 1);
});

test("parseInput: запятая как десятичный разделитель, ошибки ввода", () => {
  assert.deepEqual(D.parseInput("1 234,5", "number"), { ok: true, value: 1234.5 });
  assert.deepEqual(D.parseInput("", "number"), { ok: true, value: null });
  assert.equal(D.parseInput("abc", "number").ok, false);
  assert.equal(D.parseInput("2.5", "int").ok, false);
  assert.deepEqual(D.parseInput("A-1, A-2", "list"), { ok: true, value: ["A-1", "A-2"] });
});

test("formatDelta", () => {
  assert.equal(D.formatDelta({ abs: -12.5, pct: -10 }), "−12.5 (−10.0%)");
  assert.equal(D.formatDelta(null), "");
});
