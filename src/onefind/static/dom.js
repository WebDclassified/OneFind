// Small DOM builders. Every piece of text goes through textContent or
// text nodes, so untrusted document content can never become markup.

import { icon } from "./icons.js";

export const byId = (id) => document.getElementById(id);

/**
 * Create an element.
 *   el("button", { class: "button", type: "button", text: "Open",
 *                  attrs: { "aria-label": "Open file" }, on: { click } }, child…)
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "attrs") setAttrs(node, value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "on") {
      for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
    } else node[key] = value;
  }
  append(node, children);
  return node;
}

export function setAttrs(node, attrs) {
  for (const [name, value] of Object.entries(attrs)) {
    if (value === false || value === null || value === undefined) node.removeAttribute(name);
    else node.setAttribute(name, value === true ? "" : String(value));
  }
}

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** A button with an icon and a visible label (or an aria-label when compact). */
export function button(label, { iconName, variant = "ghost", onClick, compact = false, attrs = {},
  className = "", title, disabled = false } = {}) {
  const classes = ["button", `button-${variant}`];
  if (compact) classes.push("button-compact");
  if (className) classes.push(className);
  const node = el("button", {
    type: "button",
    class: classes.join(" "),
    disabled,
    // Icon-only buttons get a tooltip so sighted mouse users can read the label too.
    title: title || (compact ? label : undefined),
    attrs: compact ? { "aria-label": label, ...attrs } : attrs,
    on: onClick ? { click: onClick } : undefined,
  });
  if (iconName) node.append(icon(iconName));
  const text = el("span", { text: label });
  if (compact) text.className = "sr-only";
  node.append(text);
  return node;
}

/** Replace a button's visible label without touching its icon. */
export function setButtonLabel(node, label) {
  const span = node.querySelector("span:last-of-type");
  if (span) span.textContent = label;
}

/** Text with <mark> highlights from [{text, highlight}] segments. */
export function highlightedText(segments) {
  const fragment = document.createDocumentFragment();
  for (const segment of segments) {
    if (segment.highlight) fragment.append(el("mark", { text: segment.text }));
    else fragment.append(document.createTextNode(segment.text));
  }
  return fragment;
}

export function isTypingTarget(target) {
  if (!(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) {
    return !["checkbox", "radio", "button", "submit", "reset", "range"].includes(target.type);
  }
  return false;
}

/** Copy text to the clipboard, falling back to a hidden textarea. */
export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  const area = el("textarea", { value: text, readOnly: true, class: "clipboard-helper" });
  area.setAttribute("aria-hidden", "true");
  document.body.append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

/** Save text as a file through a Blob object URL. */
export function downloadText(filename, text, type = "text/plain") {
  const blob = new Blob([text], { type: `${type};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: filename, class: "sr-only" });
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function debounce(fn, wait) {
  let timer = 0;
  const debounced = (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), wait);
  };
  debounced.cancel = () => window.clearTimeout(timer);
  return debounced;
}

export const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
