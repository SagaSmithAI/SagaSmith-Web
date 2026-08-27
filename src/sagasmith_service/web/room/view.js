import { text } from "/assets/components/dom.js";

export function num(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : value;
}

export function signed(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? `${numeric >= 0 ? "+" : ""}${numeric}` : num(value);
}

export function entries(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? Object.entries(value)
    : [];
}

export function appendDetails(root, rows) {
  const list = text("div", "", "detail-list");
  for (const [label, value] of rows) {
    if (value === undefined || value === null || value === "") continue;
    const row = text("div", "", "detail-row");
    row.append(text("span", label), text("span", String(value)));
    list.append(row);
  }
  root.append(list);
}
