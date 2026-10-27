// No-results and error states: honest copy plus a next step.

import { button, byId } from "./dom.js";
import { icon } from "./icons.js";
import { truncate } from "./format.js";
import { actions, activeFilterCount } from "./store.js";

const EMPTY_COPY = {
  lexical: (q) => `No document contains every word in “${q}”. Keyword search needs the exact words, so try fewer or different words, or search by meaning.`,
  semantic: (q) => `Nothing in this collection is close in meaning to “${q}”. Try describing it another way, or use Keyword search for exact names and codes.`,
  hybrid: (q) => `Neither the words nor the meaning of “${q}” matched a document closely enough. Try fewer words or a broader description.`,
};

function editSearch() {
  const input = byId("search-input");
  input.focus();
  input.select();
}

export function renderEmpty({ query, mode, strictCount = null }) {
  const quoted = truncate(query, 80);
  // strictCount is reported when keyword search also tried partial matches,
  // so an empty keyword result means none of the words appear anywhere.
  let copy = mode === "lexical" && strictCount !== null
    ? `No document contains any of the words in “${quoted}”. Check the spelling, try other words, or search by meaning.`
    : (EMPTY_COPY[mode] || EMPTY_COPY.hybrid)(quoted);
  const filters = activeFilterCount();
  if (filters) copy += " Your filters may also be hiding results.";
  byId("empty-copy").textContent = copy;

  const buttons = [];
  for (const target of ["semantic", "hybrid"]) {
    if (target !== mode && actions.canUseMode?.(target)) {
      buttons.push(button(target === "semantic" ? "Search by meaning" : "Try Hybrid", {
        variant: buttons.length ? "secondary" : "primary",
        iconName: target === "semantic" ? "sparkle" : "blend",
        onClick: () => actions.switchMode?.(target),
      }));
    }
  }
  if (mode !== "lexical" && actions.canUseMode?.("lexical") && !buttons.length) {
    buttons.push(button("Search exact words", { variant: "secondary", iconName: "search", onClick: () => actions.switchMode?.("lexical") }));
  }
  if (filters) {
    buttons.push(button("Clear filters", { variant: buttons.length ? "secondary" : "primary", iconName: "x", onClick: () => actions.clearFilters?.() }));
  }
  buttons.push(button("Edit search", { variant: "ghost", iconName: "pencil", onClick: editSearch }));
  byId("empty-actions").replaceChildren(...buttons);
}

const ERROR_TITLES = {
  offline: "OneFind is not responding",
  no_index: "No index ready",
  no_embeddings: "Meaning search is not set up yet",
  model_unavailable: "The local model could not load",
  validation_error: "That search could not be read",
  request_failed: "Search could not finish",
};

const OFFLINE_COPY = "OneFind's local service stopped responding. Start it again with “onefind serve”, then try again. Your query and your collection are safe.";

const ACTIONS = {
  retry: { label: "Try again", icon: "refresh", run: () => actions.retrySearch?.() },
  reindex: { label: "Update index", icon: "refresh", run: () => actions.reindex?.() },
  index: { label: "Index a folder", icon: "folder", run: (trigger) => actions.openIndexWizard?.({ trigger }) },
  embed: { label: "Build vectors", icon: "sparkle", run: () => actions.addVectors?.() },
  switch_mode: { label: "Use Keyword search", icon: "search", run: () => actions.switchMode?.("lexical") },
  repair: { label: "Check and repair", icon: "wrench", run: (trigger) => actions.openManage?.({ section: "manage-maintenance", trigger }) },
  open_collection: { label: "Open a collection", icon: "database", run: (trigger) => actions.openCollectionPrompt?.(trigger) },
};

/** Pick the next action for an error: the server's hint first, then sensible defaults. */
export function errorAction(error) {
  if (error?.action && ACTIONS[error.action]) return error.action;
  if (error?.code === "no_embeddings") return "switch_mode";
  if (error?.code === "no_index") return "index";
  if (error?.retryable || error?.code === "offline") return "retry";
  return null;
}

function setTone(tone) {
  const card = byId("error-card");
  card.classList.toggle("state-error", tone === "error");
  card.classList.toggle("state-neutral", tone !== "error");
  byId("error-icon").replaceChildren(icon(tone === "error" ? "alert" : "stop"));
}

export function renderError(error) {
  const code = error?.code || "request_failed";
  const offline = code === "offline";
  setTone("error");
  byId("error-title").textContent = ERROR_TITLES[code] || ERROR_TITLES.request_failed;
  byId("error-message").textContent = offline ? OFFLINE_COPY : error?.message || "Something went wrong while searching.";
  const primary = errorAction(error);
  const buttons = [];
  if (primary) {
    const spec = ACTIONS[primary];
    buttons.push(button(spec.label, { variant: "primary", iconName: spec.icon, onClick: (event) => spec.run(event.currentTarget) }));
  }
  if (primary !== "retry") {
    buttons.push(button("Try again", { variant: buttons.length ? "secondary" : "primary", iconName: "refresh", onClick: () => actions.retrySearch?.() }));
  }
  // Manage cannot help while the service is down.
  if (primary !== "repair" && !offline) {
    buttons.push(button("Open Manage", { variant: "ghost", iconName: "settings", onClick: (event) => actions.openManage?.({ trigger: event.currentTarget }) }));
  }
  byId("error-actions").replaceChildren(...buttons);
  document.body.dataset.offline = String(offline);
}

/** A search the user stopped: neutral, with a one-click way to run it again. */
export function renderCancelled() {
  setTone("neutral");
  byId("error-title").textContent = "Search stopped";
  byId("error-message").textContent = "Your query is still in the search box. Run it again when you are ready, or change it first.";
  byId("error-actions").replaceChildren(
    button("Search again", { variant: "primary", iconName: "search", onClick: () => actions.retrySearch?.() }),
    button("Edit search", { variant: "ghost", iconName: "pencil", onClick: editSearch }),
  );
  document.body.dataset.offline = "false";
}
