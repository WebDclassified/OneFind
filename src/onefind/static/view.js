// Exactly one results-area view is visible at a time.

import { byId } from "./dom.js";
import { enter } from "./motion.js";
import { emit, state } from "./store.js";

export const VIEWS = ["idle", "onboarding", "typing", "loading", "results", "empty", "error"];

export function showView(name) {
  if (!VIEWS.includes(name)) return;
  const changed = state.view !== name;
  for (const view of VIEWS) {
    const node = byId(`view-${view}`);
    if (node) node.hidden = view !== name;
  }
  const head = byId("results-head");
  const headWasHidden = head.hidden;
  head.hidden = name !== "results";
  const pane = byId("results-pane");
  pane.dataset.view = name;
  pane.setAttribute("aria-busy", name === "loading" ? "true" : "false");
  // The loading view replaces dimmed results; it must never look disabled itself.
  if (name === "loading") pane.classList.remove("is-refreshing");
  // CSS hides controls that do nothing in a state (e.g. search modes before an index exists).
  document.body.dataset.view = name;
  state.view = name;
  if (changed) {
    // Result cards run their own staged entry, so the results view does not fade as a whole.
    if (name !== "results") enter(byId(`view-${name}`), "view-enter");
    else if (headWasHidden) enter(head, "fade-enter");
    if ((name === "idle" || name === "onboarding") && window.scrollY > 0) window.scrollTo({ top: 0 });
    emit("view", name);
  }
}

export function setBusy(busy, { refreshing = false } = {}) {
  const pane = byId("results-pane");
  pane.setAttribute("aria-busy", busy ? "true" : "false");
  pane.classList.toggle("is-refreshing", busy && refreshing);
}
