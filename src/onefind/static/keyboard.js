// Keyboard-first flow: "/" and Ctrl/Cmd+K focus search, arrows or j/k move
// across result cards, single-letter actions on the focused card, Esc
// closes the preview and then clears the search, "?" lists shortcuts.

import { byId, isTypingTarget } from "./dom.js";
import { anyDialogOpen, anyMenuOpen, openDialog } from "./dialogs.js";
import { actions, state, storage } from "./store.js";
import { cardAction, focusCard, moveFocus, visibleCards } from "./views-results.js";

const CARD_KEYS = { o: "open", c: "copy", b: "bookmark", e: "explain" };
const SINGLE_KEYS_KEY = "onefind.singleKeys";

/** Single-character shortcuts (/, ?, j, k, o, c, b, e) can be turned off in Preferences (WCAG 2.1.4). */
export function singleKeysEnabled() {
  return storage.get(SINGLE_KEYS_KEY) !== "off";
}

export function setSingleKeysEnabled(enabled) {
  if (enabled) storage.remove(SINGLE_KEYS_KEY);
  else storage.set(SINGLE_KEYS_KEY, "off");
}

/** Result navigation only applies where results are the subject: not from header buttons or menus. */
function inResultsContext(target) {
  if (!(target instanceof Element) || target === document.body || target === document.documentElement) return true;
  return Boolean(target.closest("#results-pane, #preview"));
}

function focusSearch(select = false) {
  const input = byId("search-input");
  input.focus();
  if (select) input.select();
}

function focusedCard(target) {
  return target instanceof Element && target.classList.contains("result-card") ? target : null;
}

function handleEscape(event) {
  if (actions.closePreview?.()) {
    event.preventDefault();
    return;
  }
  const input = byId("search-input");
  if (input.value) {
    // Stop the browser's own search-field clearing so ours runs once.
    event.preventDefault();
    actions.clearSearch?.();
  } else if (document.activeElement !== input && state.view !== "onboarding") {
    focusSearch();
  }
}

function onKeyDown(event) {
  if (event.defaultPrevented || event.isComposing) return;
  const { key, target } = event;
  const modifier = event.ctrlKey || event.metaKey;
  if (modifier && !event.altKey && key.toLowerCase() === "k") {
    if (anyDialogOpen()) return;
    event.preventDefault();
    focusSearch(true);
    return;
  }
  if (anyDialogOpen() || anyMenuOpen()) return; // dialogs handle their own keys
  if (modifier || event.altKey) return;

  if (key === "Escape") {
    handleEscape(event);
    return;
  }

  const typing = isTypingTarget(target);
  if (target === byId("search-input") && key === "ArrowDown" && state.view === "results") {
    event.preventDefault();
    moveFocus(1);
    return;
  }
  if (typing) return;
  const singleKey = key.length === 1;
  if (singleKey && !singleKeysEnabled()) return;

  if (key === "/") {
    event.preventDefault();
    focusSearch(true);
    return;
  }
  if (key === "?") {
    event.preventDefault();
    openDialog(byId("shortcuts-dialog"), { trigger: document.activeElement });
    return;
  }

  const card = focusedCard(target);
  const insideResults = Boolean(target instanceof Element && target.closest("#view-results"));
  if (key === "ArrowDown" || key === "ArrowUp" || key === "j" || key === "k") {
    if (state.view !== "results" || !inResultsContext(target)) return;
    // Inside the preview the arrows scroll the document; j and k still move between results.
    if ((key === "ArrowDown" || key === "ArrowUp") && target instanceof Element && target.closest("#preview")) return;
    // Arrow keys on buttons inside a card keep their usual meaning elsewhere.
    if ((key === "ArrowDown" || key === "ArrowUp") && !card && insideResults) return;
    event.preventDefault();
    moveFocus(key === "ArrowDown" || key === "j" ? 1 : -1);
    return;
  }
  if (!card) return;
  if (key === "Home" || key === "End") {
    event.preventDefault();
    const cards = visibleCards();
    focusCard(key === "Home" ? cards[0] : cards[cards.length - 1]);
  } else if (key === "Enter" || key === " ") {
    event.preventDefault();
    cardAction(card, "preview");
  } else if (CARD_KEYS[key.toLowerCase()]) {
    event.preventDefault();
    cardAction(card, CARD_KEYS[key.toLowerCase()]);
  }
}

export function initKeyboard() {
  document.addEventListener("keydown", onKeyDown);
  byId("shortcuts-button").addEventListener("click", (event) => {
    openDialog(byId("shortcuts-dialog"), { trigger: event.currentTarget });
  });
}
