import { $ } from "/assets/components/dom.js";

let dismissTimer;

export function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(dismissTimer);
  dismissTimer = setTimeout(() => element.classList.remove("show"), 4000);
}
