export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];

export function text(tag, value, klass = "") {
  const element = document.createElement(tag);
  element.textContent = value;
  if (klass) element.className = klass;
  return element;
}

export function button(label, handler, klass = "") {
  const element = text("button", label, klass);
  element.type = "button";
  element.onclick = handler;
  return element;
}
