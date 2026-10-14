// Native <dialog> helpers: focus returns to the trigger on close, plus
// confirm (optionally typed) and prompt dialogs, and an accessible menu.

import { byId } from "./dom.js";
import { leave } from "./motion.js";

const returnTargets = new WeakMap();
const openMenus = new Set();
const modalStack = []; // open modal dialogs, last = topmost

/** The topmost open modal dialog, or null. */
export function topDialog() {
  while (modalStack.length && !modalStack[modalStack.length - 1].open) modalStack.pop();
  return modalStack[modalStack.length - 1] || null;
}

/**
 * Keep the toast region usable: a modal dialog makes the rest of the page
 * inert and covers it, so toasts live inside the topmost dialog while one
 * is open, and back in <body> otherwise.
 */
export function placeToasts() {
  const region = byId("toasts");
  if (!region) return;
  const host = topDialog() || document.body;
  if (region.parentElement !== host) host.append(region);
}

/**
 * Return focus after a dialog closes. A trigger that was re-rendered while
 * the dialog was open is found again by its data-focus-key; otherwise focus
 * lands in the dialog underneath (if any) instead of falling to <body>.
 */
function restoreFocus(dialog) {
  const saved = returnTargets.get(dialog);
  returnTargets.delete(dialog);
  const target = saved?.node;
  const usable = (node) => node instanceof HTMLElement && node.isConnected && !node.closest("[hidden]")
    && !node.closest("dialog:not([open])") && !node.disabled;
  if (usable(target)) {
    target.focus({ preventScroll: true });
    return;
  }
  if (saved?.key) {
    const again = [...document.querySelectorAll("[data-focus-key]")].find((node) => node.dataset.focusKey === saved.key);
    if (usable(again)) {
      again.focus({ preventScroll: true });
      return;
    }
  }
  const below = topDialog();
  if (below) {
    const first = below.querySelector("[data-focus-fallback], .dialog-head [data-close], button:not([disabled]), [tabindex='-1']");
    (first || below).focus({ preventScroll: true });
  }
}

/** Wire close buttons ([data-close]), backdrop clicks, and focus return. */
export function setupDialog(dialog) {
  if (!dialog || dialog.dataset.ready) return;
  dialog.dataset.ready = "1";
  dialog.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) {
      closeDialog(dialog);
      return;
    }
    // A click on the ::backdrop targets the dialog element itself.
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      const outside = event.clientX < rect.left || event.clientX > rect.right
        || event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) closeDialog(dialog);
    }
  });
  dialog.addEventListener("close", () => {
    dialog.classList.remove("is-open");
    const index = modalStack.indexOf(dialog);
    if (index >= 0) modalStack.splice(index, 1);
    placeToasts();
    restoreFocus(dialog);
  });
}

export function openDialog(dialog, { trigger } = {}) {
  if (!dialog) return;
  setupDialog(dialog);
  const active = trigger || document.activeElement;
  if (!dialog.open) {
    returnTargets.set(dialog, { node: active, key: active?.dataset?.focusKey || null });
    closeAllMenus();
    dialog.showModal();
    modalStack.push(dialog);
    placeToasts();
  }
  dialog.classList.add("is-open");
}

/**
 * Close a dialog. `restoreFocus: false` drops the saved trigger first, for
 * callers that move focus somewhere else themselves (the close event fires
 * later and would otherwise pull focus back to the trigger).
 */
export function closeDialog(dialog, value = "", { restoreFocus: restore = true } = {}) {
  if (!dialog?.open) return;
  if (!restore) returnTargets.delete(dialog);
  dialog.close(value);
}

export function anyDialogOpen() {
  return Boolean(document.querySelector("dialog[open]"));
}

/**
 * Ask for confirmation. With `typeToConfirm`, the confirm button stays
 * disabled until the exact text is typed. Resolves to true / false.
 */
export function confirmDialog({
  title, message, confirmLabel = "Confirm", danger = false, typeToConfirm = null, trigger,
}) {
  const dialog = byId("confirm-dialog");
  const form = byId("confirm-form");
  const ok = byId("confirm-ok");
  const row = byId("confirm-type-row");
  const input = byId("confirm-type-input");
  byId("confirm-title").textContent = title;
  byId("confirm-message").textContent = message;
  ok.textContent = confirmLabel;
  ok.className = `button ${danger ? "button-danger" : "button-primary"}`;
  row.hidden = !typeToConfirm;
  input.value = "";
  if (typeToConfirm) {
    byId("confirm-type-label").textContent = `Type “${typeToConfirm}” to confirm`;
    ok.disabled = true;
  } else {
    ok.disabled = false;
  }

  return new Promise((resolve) => {
    let confirmed = false;
    const onInput = () => {
      ok.disabled = input.value.trim() !== typeToConfirm;
    };
    const onSubmit = (event) => {
      event.preventDefault();
      if (ok.disabled) return;
      confirmed = true;
      closeDialog(dialog);
    };
    const onClose = () => {
      form.removeEventListener("submit", onSubmit);
      input.removeEventListener("input", onInput);
      resolve(confirmed);
    };
    form.addEventListener("submit", onSubmit);
    input.addEventListener("input", onInput);
    dialog.addEventListener("close", onClose, { once: true });
    openDialog(dialog, { trigger });
    (typeToConfirm ? input : ok).focus();
  });
}

/** True for the server's "this is on a network location, continue?" answer. */
export function needsNetworkConfirm(error) {
  return error?.code === "confirmation_required" && error?.action === "confirm_network";
}

/**
 * Run `call(confirmNetwork)`; when the server asks before contacting another
 * computer, show its message and run it again with the confirmation. Resolves
 * to the call's result, or null when the person declines.
 */
export async function withNetworkConfirm(call, { trigger } = {}) {
  try {
    return await call(false);
  } catch (error) {
    if (!needsNetworkConfirm(error)) throw error;
    const ok = await confirmDialog({
      title: "Network location",
      message: error.message,
      confirmLabel: "Continue",
      trigger,
    });
    if (!ok) return null;
    return call(true);
  }
}

/**
 * Ask for a single value. `validate(value)` may return an error string.
 * Resolves to the trimmed value, or null when cancelled.
 */
export function promptDialog({
  title, label, value = "", placeholder = "", confirmLabel = "Save", hint = "", message = "",
  validate = null, trigger,
}) {
  const dialog = byId("prompt-dialog");
  const form = byId("prompt-form");
  const input = byId("prompt-input");
  const error = byId("prompt-error");
  const messageNode = byId("prompt-message");
  byId("prompt-title").textContent = title;
  byId("prompt-label").textContent = label;
  byId("prompt-hint").textContent = hint;
  byId("prompt-ok").textContent = confirmLabel;
  messageNode.textContent = message;
  messageNode.hidden = !message;
  input.value = value;
  input.placeholder = placeholder;
  error.hidden = true;
  input.removeAttribute("aria-invalid");

  return new Promise((resolve) => {
    let result = null;
    const onSubmit = (event) => {
      event.preventDefault();
      const candidate = input.value.trim();
      const problem = !candidate ? "Please enter a value." : validate?.(candidate);
      if (problem) {
        error.textContent = problem;
        error.hidden = false;
        input.setAttribute("aria-invalid", "true");
        input.focus();
        return;
      }
      result = candidate;
      closeDialog(dialog);
    };
    form.addEventListener("submit", onSubmit);
    dialog.addEventListener("close", () => {
      form.removeEventListener("submit", onSubmit);
      resolve(result);
    }, { once: true });
    openDialog(dialog, { trigger });
    input.focus();
    input.select();
  });
}

/* ---------- menus ---------- */

export function closeAllMenus(except = null) {
  for (const menu of [...openMenus]) if (menu !== except) menu.close(false);
}

/** Arrow keys, Home/End, Escape and Tab inside an open menu. */
function wireMenuKeys(menu, controller) {
  const items = () => [...menu.querySelectorAll('[role^="menuitem"]:not([disabled])')];
  menu.addEventListener("keydown", (event) => {
    const list = items();
    const index = list.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      list[(index + step + list.length) % list.length]?.focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      (event.key === "Home" ? list[0] : list[list.length - 1])?.focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      controller.close();
    } else if (event.key === "Tab") {
      controller.close(false);
    }
  });
  menu.addEventListener("click", (event) => {
    if (event.target.closest('[role^="menuitem"]')) controller.close(event.detail === 0);
  });
  return items;
}

function showMenu(menu) {
  menu.hidden = false;
  menu.classList.remove("is-leaving");
  menu.classList.add("is-open");
}

function hideMenu(menu, controller) {
  menu.classList.remove("is-open");
  controller.cancelLeave = leave(menu, "is-leaving", () => {
    controller.cancelLeave = null;
    if (!controller.isOpen()) menu.hidden = true;
  });
}

/**
 * Turn a button + [role=menu] container into a menu: click toggles,
 * arrow keys move between items, Escape closes and returns focus.
 * `onOpen()` may rebuild the menu items before it is shown.
 */
export function setupMenu(trigger, menu, { onOpen } = {}) {
  let open = false;
  const controller = {
    cancelLeave: null,
    isOpen: () => open,
    async open(focusFirst = true) {
      closeAllMenus(controller);
      if (onOpen) await onOpen();
      controller.cancelLeave?.();
      open = true;
      showMenu(menu);
      trigger.setAttribute("aria-expanded", "true");
      openMenus.add(controller);
      if (focusFirst) {
        const checked = menu.querySelector('[aria-checked="true"]');
        (checked || items()[0])?.focus();
      }
    },
    close(returnFocus = true) {
      if (!open) return;
      open = false;
      hideMenu(menu, controller);
      trigger.setAttribute("aria-expanded", "false");
      openMenus.delete(controller);
      if (returnFocus) trigger.focus();
    },
  };
  const items = wireMenuKeys(menu, controller);

  trigger.addEventListener("click", () => {
    if (controller.isOpen()) controller.close();
    else controller.open(true);
  });
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" && !controller.isOpen()) {
      event.preventDefault();
      controller.open(true);
    }
  });
  document.addEventListener("pointerdown", (event) => {
    if (controller.isOpen() && !menu.contains(event.target) && !trigger.contains(event.target)) {
      controller.close(false);
    }
  });
  return controller;
}

/**
 * One shared floating menu for per-item "More" buttons (result cards), so
 * re-rendered lists never leak a menu or a document listener per item.
 * `items` are menu item elements (role="menuitem…").
 */
export function setupFloatingMenu(menu) {
  let open = false;
  let trigger = null;
  const controller = {
    cancelLeave: null,
    isOpen: () => open,
    openFor(button, items, { focusFirst = true } = {}) {
      if (open && trigger === button) {
        controller.close();
        return;
      }
      closeAllMenus();
      controller.cancelLeave?.();
      trigger = button;
      menu.replaceChildren(...items);
      open = true;
      showMenu(menu);
      place(button);
      button.setAttribute("aria-expanded", "true");
      openMenus.add(controller);
      if (focusFirst) itemsOf()[0]?.focus();
    },
    close(returnFocus = true) {
      if (!open) return;
      open = false;
      hideMenu(menu, controller);
      trigger?.setAttribute("aria-expanded", "false");
      openMenus.delete(controller);
      if (returnFocus && trigger?.isConnected) trigger.focus();
    },
  };
  const itemsOf = wireMenuKeys(menu, controller);
  function place(button) {
    // Fixed position next to the trigger; flip above when there is no room below.
    const rect = button.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const gap = 6;
    const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
    const below = rect.bottom + gap + height <= window.innerHeight - 8;
    const top = below ? rect.bottom + gap : Math.max(8, rect.top - gap - height);
    menu.style.setProperty("left", `${Math.round(left)}px`);
    menu.style.setProperty("top", `${Math.round(top)}px`);
    menu.classList.toggle("is-above", !below);
  }
  document.addEventListener("pointerdown", (event) => {
    if (open && !menu.contains(event.target) && !trigger?.contains(event.target)) controller.close(false);
  });
  window.addEventListener("resize", () => controller.close(false));
  document.addEventListener("scroll", () => controller.close(false), { passive: true });
  return controller;
}

export function anyMenuOpen() {
  return openMenus.size > 0;
}
