// Toasts for action feedback and a polite live region for state changes.

import { placeToasts } from "./dialogs.js";
import { byId, el } from "./dom.js";
import { icon } from "./icons.js";
import { reducedMotion } from "./motion.js";

const TONE_ICONS = { success: "check-circle", info: "info", warn: "alert", danger: "alert" };
const MAX_TOASTS = 3;

let announceTimer = 0;
const timers = new WeakMap(); // toast -> dismiss timer

/** Announce a short message to screen readers without moving focus. */
export function announce(message) {
  const region = byId("announcer");
  if (!region) return;
  window.clearTimeout(announceTimer);
  region.textContent = "";
  announceTimer = window.setTimeout(() => {
    region.textContent = message;
  }, 40);
}

function dismiss(node) {
  if (!node.isConnected || node.classList.contains("is-leaving")) return;
  if (reducedMotion()) {
    node.remove();
    return;
  }
  node.classList.add("is-leaving");
  node.addEventListener("animationend", () => node.remove(), { once: true });
  window.setTimeout(() => node.remove(), 600);
}

function startTimer(node, timeout) {
  window.clearTimeout(timers.get(node));
  if (timeout === Infinity) return;
  timers.set(node, window.setTimeout(() => dismiss(node), timeout));
}

/**
 * Show a toast. `action` = { label, onClick } adds a button; a toast with
 * an action stays until it is used or dismissed, so keyboard and screen
 * reader users have time to reach it. Repeating a message that is still
 * on screen restarts its timer instead of stacking a copy.
 */
export function toast(message, { tone = "success", action = null, duration } = {}) {
  const region = byId("toasts");
  if (!region) return null;
  placeToasts();
  const timeout = duration ?? (action ? Infinity : tone === "danger" ? 6000 : 3600);
  const existing = [...region.querySelectorAll(".toast:not(.is-leaving)")]
    .find((node) => node.dataset.message === message && node.dataset.tone === tone && !action);
  if (existing) {
    startTimer(existing, timeout);
    return existing;
  }
  const node = el("div", {
    class: `toast toast-${tone}`,
    dataset: { message, tone },
    // Problems interrupt; everything else waits its turn.
    attrs: tone === "danger" ? { role: "alert" } : {},
  },
  icon(TONE_ICONS[tone] || "info", "icon toast-icon"),
  el("p", { class: "toast-text", text: message }),
  );
  if (action) {
    node.append(el("button", {
      type: "button",
      class: "button button-link toast-action",
      text: action.label,
      on: {
        click: () => {
          dismiss(node);
          action.onClick();
        },
      },
    }));
  }
  node.append(el("button", {
    type: "button",
    class: "toast-close",
    attrs: { "aria-label": "Dismiss notification" },
    on: { click: () => dismiss(node) },
  }, icon("x")));

  while (region.children.length >= MAX_TOASTS) region.firstElementChild.remove();
  region.append(node);
  startTimer(node, timeout);
  node.addEventListener("pointerenter", () => window.clearTimeout(timers.get(node)));
  node.addEventListener("focusin", () => window.clearTimeout(timers.get(node)));
  node.addEventListener("pointerleave", () => startTimer(node, timeout === Infinity ? Infinity : 2000));
  return node;
}

/** Toast an API error in plain language. */
export function toastError(error, fallback = "That did not work.") {
  const message = error?.message || fallback;
  return toast(message, { tone: "danger" });
}
