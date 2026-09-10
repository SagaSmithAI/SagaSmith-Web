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

// Keep one submission in flight without disabling the values FormData needs.
export async function withBusy(element, operation) {
  if (element.getAttribute("aria-busy") === "true") return;
  const controls = [...element.querySelectorAll("button[type=submit], button:not([type])")];
  const disabled = controls.map((control) => control.disabled);
  element.setAttribute("aria-busy", "true");
  controls.forEach((control) => { control.disabled = true; });
  try {
    return await operation();
  } finally {
    element.removeAttribute("aria-busy");
    controls.forEach((control, index) => { control.disabled = disabled[index]; });
  }
}

export function showLoadState(root, message, retry) {
  const status = text("div", "", "load-state");
  status.setAttribute("role", retry ? "alert" : "status");
  status.append(text("p", message, "muted"));
  if (retry) status.append(button("重试", retry));
  root.replaceChildren(status);
}
