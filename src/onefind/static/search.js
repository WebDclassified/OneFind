// Search controller: input handling, debounced live search, stale-request
// cancellation, streamed stages with a plain-request fallback, and the
// state transitions idle → typing → loading → results / empty / error.

import { api, StreamUnavailable, streamSearch } from "./api.js";
import { button, byId, debounce, el } from "./dom.js";
import { truncate } from "./format.js";
import {
  actions, availableModes, availablePrecisions, canSearch, collectionStatus, emit, filtersPayload, MODES,
  PRECISIONS, RESULT_COUNTS, state,
} from "./store.js";
import { enter } from "./motion.js";
import { announce, toast } from "./toast.js";
import { setBusy, showView } from "./view.js";
import {
  cancelPipeline, MAX_SETTLE_MS, settlePipeline, showSlowNotice, stageEvent, startPipeline,
} from "./views-loading.js";
import { renderResults } from "./views-results.js";
import { renderCancelled, renderEmpty, renderError } from "./views-states.js";

export const DEBOUNCE_MS = 250;
export const MIN_LIVE_CHARS = 2;
const LOADING_DELAY_MS = 120; // fast searches never flash a loading state
const SLOW_MS = 8000; // refreshes keep the dimmed results until then
const HISTORY_COMMIT_MS = 1500;
const TYPING_CUE_MS = 900; // the readiness cue fades once typing pauses

const MODE_NOTES = {
  lexical: "Keyword finds the exact words, names and codes you type.",
  semantic: "Meaning finds notes about the same idea, even when the words differ.",
  hybrid: "Hybrid combines exact keywords with related meaning. Best for most searches.",
};

const PRECISION_NOTES = {
  float: "Full precision gives the most accurate meaning search.",
  int8: "Int8 uses compact 8-bit vectors: a little faster, nearly as accurate.",
  binary: "Binary uses 1-bit vectors: the fastest and least precise option.",
};

export const PLACEHOLDERS = {
  ready: "Search your notes by keyword or meaning.",
  keywordOnly: "Search your notes by keyword.",
  noIndex: "Index a folder to start searching your notes.",
  indexing: "Indexing your notes… search opens as soon as the first ones are saved.",
};
// Phones cut the long placeholders off mid-word.
const SHORT_PLACEHOLDERS = {
  ready: "Search your notes",
  keywordOnly: "Search your notes",
  noIndex: "Index a folder to start",
  indexing: "Indexing your notes…",
};
const NARROW_QUERY = window.matchMedia("(max-width: 480px)");

let controller = null;
let requestSeq = 0;
let loadingTimer = 0;
let slowTimer = 0;
let historyTimer = 0;
let loadingShownAt = 0;
let typingTimer = 0;
let modelSeen = false; // a meaning search already ran in this page, so the model is warm

const input = () => byId("search-input");
const live = debounce(() => runSearch({ record: false }), DEBOUNCE_MS);

/* ---------- controls ---------- */

function radios(name) {
  return [...document.querySelectorAll(`input[name="${name}"]`)];
}

function checkRadio(name, value) {
  for (const radio of radios(name)) radio.checked = radio.value === value;
}

export function canUseMode(mode) {
  return availableModes().has(mode);
}

function modeNote() {
  const note = byId("mode-note");
  const stats = state.stats;
  const modes = availableModes();
  const parts = [];
  if (stats && Number(stats.documents) > 0 && !modes.has("semantic")) {
    parts.push("Meaning search needs vectors. Build them from Manage → Reindex with vectors. ");
    parts.push(button("Build vectors", { variant: "link", onClick: () => actions.addVectors?.() }));
    note.classList.add("has-action");
  } else {
    note.classList.remove("has-action");
    parts.push(MODE_NOTES[state.mode] || MODE_NOTES.hybrid);
    const missing = Number(stats?.vectors_missing) || 0;
    if (missing > 0 && state.mode !== "lexical") {
      parts.push(` ${missing === 1 ? "One document has" : `${missing} documents have`} no vectors yet.`);
    }
  }
  note.replaceChildren(...parts.map((part) => (typeof part === "string" ? document.createTextNode(part) : part)));
}

function precisionNote() {
  const group = byId("precision-group");
  const lexical = state.mode === "lexical";
  group.disabled = lexical || availablePrecisions().size === 0;
  byId("precision-help").textContent = lexical
    ? "Precision applies to Meaning and Hybrid search."
    : availablePrecisions().size === 0
      ? "Precision options appear once this collection has vectors."
      : PRECISION_NOTES[state.precision] || PRECISION_NOTES.float;
}

function updatePlaceholder() {
  const modes = availableModes();
  const key = !canSearch() ? (collectionStatus() === "indexing" ? "indexing" : "noIndex")
    : modes.has("semantic") ? "ready" : "keywordOnly";
  input().placeholder = (NARROW_QUERY.matches ? SHORT_PLACEHOLDERS : PLACEHOLDERS)[key];
}

/** Enable/disable modes and precisions for the loaded collection. */
export function configureControls() {
  const modes = availableModes();
  for (const radio of radios("mode")) {
    radio.disabled = !modes.has(radio.value);
    const label = radio.closest("label");
    label.classList.toggle("is-disabled", radio.disabled);
    label.title = radio.disabled && radio.value !== "lexical"
      ? "Meaning search needs vectors. Build them from Manage → Reindex with vectors."
      : "";
  }
  if (!modes.has(state.mode)) {
    state.mode = modes.has("hybrid") ? "hybrid" : modes.has("lexical") ? "lexical" : "hybrid";
  }
  checkRadio("mode", state.mode);

  const precisions = availablePrecisions();
  for (const radio of radios("precision")) {
    radio.disabled = !precisions.has(radio.value);
    radio.closest("label").classList.toggle("is-disabled", radio.disabled);
  }
  if (precisions.size && !precisions.has(state.precision)) {
    state.precision = precisions.has("float") ? "float" : [...precisions][0];
  }
  checkRadio("precision", state.precision);
  byId("result-count").value = String(state.k);
  modeNote();
  precisionNote();
  updatePlaceholder();
}

/* ---------- URL state ---------- */

export function readUrlState(search = window.location.search) {
  const params = new URLSearchParams(search);
  const mode = params.get("mode");
  const precision = params.get("precision");
  const k = Number(params.get("k"));
  return {
    query: (params.get("q") || "").trim().slice(0, 512),
    mode: MODES.includes(mode) ? mode : null,
    precision: PRECISIONS.includes(precision) ? precision : null,
    k: RESULT_COUNTS.includes(k) ? k : null,
  };
}

function writeUrlState(query) {
  if (!window.history?.replaceState) return;
  const url = new URL(window.location.href);
  for (const key of ["q", "mode", "precision", "k"]) url.searchParams.delete(key);
  if (query) {
    url.searchParams.set("q", query);
    url.searchParams.set("mode", state.mode);
    if (state.mode !== "lexical") url.searchParams.set("precision", state.precision);
    url.searchParams.set("k", String(state.k));
  }
  window.history.replaceState(null, "", url);
}

/* ---------- loading lifecycle ---------- */

function clearTimers() {
  window.clearTimeout(loadingTimer);
  window.clearTimeout(slowTimer);
  loadingTimer = 0;
  slowTimer = 0;
}

function showLoading() {
  setBusy(true);
  showView("loading");
  loadingShownAt = performance.now();
}

/**
 * A new search shows the pipeline after a short delay (fast searches never
 * flash it). A refresh of visible results (filter, mode or count change, a
 * refined query) keeps the old results dimmed instead, and falls back to
 * the pipeline only when the search turns out to be slow.
 */
function beginLoading(refreshing) {
  clearTimers();
  loadingShownAt = 0;
  const loadModel = !modelSeen && state.mode !== "lexical" && state.stats?.model_loaded === false;
  startPipeline(state.mode, { loadModel });
  byId("search-field").classList.remove("is-typing");
  setBusy(true, { refreshing });
  if (!refreshing) loadingTimer = window.setTimeout(showLoading, LOADING_DELAY_MS);
  slowTimer = window.setTimeout(() => {
    if (state.view !== "loading") showLoading();
    showSlowNotice(true);
    announce("Still working on your search.");
  }, SLOW_MS);
}

async function finishLoading(stages) {
  window.clearTimeout(loadingTimer);
  loadingTimer = 0;
  if (state.view !== "loading" || !loadingShownAt) {
    cancelPipeline();
    return;
  }
  // The remaining steps resolve one by one (capped at MAX_SETTLE_MS),
  // however long the pipeline has already been on screen.
  await settlePipeline(stages, MAX_SETTLE_MS);
}

async function fetchResults(body, signal, streamed) {
  const onEvent = (event) => {
    stageEvent(event);
    if (event.status === "done") streamed.push({ name: event.name, ms: event.ms });
  };
  try {
    return await streamSearch(body, { signal, onEvent });
  } catch (error) {
    if (!(error instanceof StreamUnavailable)) throw error;
    return api.search(body, signal);
  }
}

function scheduleHistoryCommit(body, count) {
  window.clearTimeout(historyTimer);
  historyTimer = window.setTimeout(() => {
    if (input().value.trim() !== body.query || state.mode !== body.mode) return;
    api.recordHistory({
      query: body.query,
      mode: body.mode,
      precision: body.mode === "lexical" ? null : body.precision,
      filters: body.filters || null,
      result_count: count,
    }).then(() => emit("history"), () => {});
  }, HISTORY_COMMIT_MS);
}

function present(payload, query, stages) {
  const refresh = state.view === "results";
  const view = renderResults(payload, { query, refresh, stages });
  if (!view.results.length) {
    renderEmpty({ query, mode: view.meta.mode, strictCount: view.meta.strictCount });
    showView("empty");
    announce("No matches found.");
  } else {
    showView("results");
  }
  if (state.mode !== "lexical") {
    modelSeen = true;
    if (state.stats) state.stats.model_loaded = true;
  }
  return view;
}

/* ---------- public API ---------- */

function goHome() {
  if (canSearch()) {
    showView("idle");
    return;
  }
  actions.renderOnboarding?.();
  showView("onboarding");
}

/**
 * Run the query in the search box now. `record` = add it to recent searches.
 * Live keystroke searches are recorded later, once the user pauses
 * (`deferRecord`, on by default when `record` is false); a search that came
 * from the page URL passes `deferRecord: false` so it is never recorded on
 * its own.
 */
export async function runSearch({ record = true, deferRecord = !record } = {}) {
  live.cancel();
  window.clearTimeout(historyTimer);
  const query = input().value.trim();
  if (!query) {
    clearSearch({ focus: false });
    return;
  }
  if (!canSearch() && state.stats?.offline) {
    // The service may be back (a retry after it stopped): ask before giving up.
    await actions.refreshStats?.();
  }
  if (state.stats?.offline) {
    // The service is down: say so (with the query kept), not "index a folder".
    renderError({ code: "offline", retryable: true, action: "retry" });
    showView("error");
    announce("OneFind is not responding.");
    return;
  }
  if (!canSearch()) {
    goHome();
    if (collectionStatus() === "indexing" || actions.jobRunning?.()) {
      // The query stays in the box and runs once the first notes are saved (app.js).
      toast("Still indexing. Your search runs as soon as the first notes are ready.", {
        tone: "info",
        duration: 6000,
        action: { label: "Show progress", onClick: () => actions.showJobProgress?.() },
      });
    } else {
      toast("Index a folder to start searching.", {
        tone: "info",
        action: { label: "Index a folder", onClick: () => actions.openIndexWizard?.() },
      });
    }
    return;
  }

  controller?.abort();
  const current = new AbortController();
  controller = current;
  const seq = ++requestSeq;
  writeUrlState(query);
  byId("suggestions").hidden = true;

  const body = { query, mode: state.mode, precision: state.precision, k: state.k, explain: true, record };
  const filters = filtersPayload();
  if (filters) body.filters = filters;
  state.lastRequest = body;
  beginLoading(state.view === "results");

  try {
    const streamed = [];
    const payload = await fetchResults(body, current.signal, streamed);
    if (seq !== requestSeq) return;
    // Streamed timings include the final "build" step; plain responses carry meta.stages.
    const stages = streamed.length ? streamed : payload?.meta?.stages;
    await finishLoading(stages);
    if (seq !== requestSeq) return;
    const view = present(payload, query, stages);
    emit("searched", { query, record });
    if (!record && deferRecord) scheduleHistoryCommit(body, view.strong.length);
  } catch (error) {
    if (error?.name === "AbortError" || seq !== requestSeq) return;
    // Let the status pill say so right away instead of at the next poll.
    if (error?.code === "offline") actions.refreshStats?.();
    renderError(error);
    showView("error");
    announce(`Search could not finish. ${error?.message || ""}`.trim());
  } finally {
    if (seq === requestSeq) {
      controller = null;
      clearTimers();
      setBusy(false);
    }
  }
}

/** Stop a running search without showing any state for it. */
function abortInFlight() {
  if (!controller) return false;
  controller.abort();
  controller = null;
  // The aborted runSearch sees a newer sequence and skips its own cleanup.
  requestSeq += 1;
  clearTimers();
  cancelPipeline();
  setBusy(false);
  return true;
}

export function cancelSearch() {
  if (!controller) return;
  controller.abort();
  controller = null;
  requestSeq += 1;
  clearTimers();
  cancelPipeline();
  setBusy(false);
  // Stopping on purpose is not a failure: a calm state, the query kept, focus in the box.
  renderCancelled();
  showView("error");
  input().focus({ preventScroll: true });
  announce("Search stopped. Your query is still in the search box.");
}

export function clearSearch({ focus = true } = {}) {
  live.cancel();
  window.clearTimeout(historyTimer);
  controller?.abort();
  controller = null;
  requestSeq += 1;
  clearTimers();
  setBusy(false);
  input().value = "";
  byId("clear-button").hidden = true;
  byId("search-field").classList.remove("is-typing");
  writeUrlState("");
  state.lastResponse = null;
  goHome();
  emit("cleared");
  if (focus) input().focus();
  announce("Search cleared.");
}

/** Fill the search box (and optionally mode, precision, count) and search. */
export function applyQuery({ query, mode, precision, k, record = true, deferRecord = !record }) {
  // Started from a home-screen list that is about to be replaced: keep focus somewhere useful.
  if (byId("results-pane").contains(document.activeElement)) input().focus({ preventScroll: true });
  if (mode && canUseMode(mode)) state.mode = mode;
  if (precision && availablePrecisions().has(precision)) state.precision = precision;
  if (k && RESULT_COUNTS.includes(Number(k))) state.k = Number(k);
  configureControls();
  input().value = query || "";
  byId("clear-button").hidden = !input().value;
  return runSearch({ record, deferRecord });
}

/**
 * Change the search mode and re-run the query. Buttons such as "Try Hybrid"
 * move focus to the search box; arrowing through the mode radios does not,
 * so the radio group keeps working from the keyboard.
 */
export function switchMode(mode, { focusInput = true } = {}) {
  if (!canUseMode(mode)) return;
  state.mode = mode;
  checkRadio("mode", mode);
  modeNote();
  precisionNote();
  if (input().value.trim()) runSearch({ record: true });
  if (focusInput) input().focus({ preventScroll: true });
}

export function hasQuery() {
  return Boolean(input().value.trim());
}

/** "Try:" chips with real queries from the user's own collection. */
export function showSuggestions(queries) {
  const container = byId("suggestions");
  const unique = [...new Set(queries.filter(Boolean))].slice(0, 3);
  if (!unique.length) {
    container.hidden = true;
    container.replaceChildren();
    return;
  }
  container.replaceChildren(
    el("span", { class: "suggestions-label", text: "Try" }),
    ...unique.map((query) => el("button", {
      type: "button",
      class: "chip chip-suggestion",
      text: truncate(query, 48),
      title: query,
      on: { click: () => applyQuery({ query }) },
    })),
  );
  container.hidden = false;
  enter(container);
}

function onInput() {
  const value = input().value;
  const query = value.trim();
  byId("clear-button").hidden = !value;
  window.clearTimeout(historyTimer);
  if (!query) {
    live.cancel();
    controller?.abort();
    requestSeq += 1;
    clearTimers();
    setBusy(false);
    writeUrlState("");
    byId("search-field").classList.remove("is-typing");
    goHome();
    return;
  }
  if (!canSearch()) return;
  showTypingCue();
  if (query.length < MIN_LIVE_CHARS) {
    live.cancel();
    // A search for the longer query may still be running: its results no
    // longer match the box, so drop it (results already on screen stay).
    abortInFlight();
    // The home screen stays put for a single character; a stale no-results,
    // error or loading view gives way to the "keep typing" hint.
    if (["empty", "error", "loading"].includes(state.view)) showView("typing");
    return;
  }
  live();
}

/** A brief readiness cue while keys are pressed (not a spinner). */
function showTypingCue() {
  const field = byId("search-field");
  field.classList.add("is-typing");
  window.clearTimeout(typingTimer);
  typingTimer = window.setTimeout(() => field.classList.remove("is-typing"), TYPING_CUE_MS);
}

export function initSearch() {
  byId("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch({ record: true });
  });
  input().addEventListener("input", onInput);
  byId("clear-button").addEventListener("click", () => clearSearch());
  byId("cancel-search").addEventListener("click", cancelSearch);

  for (const radio of radios("mode")) {
    radio.addEventListener("change", () => {
      if (radio.checked) switchMode(radio.value, { focusInput: false });
    });
  }
  for (const radio of radios("precision")) {
    radio.addEventListener("change", () => {
      if (!radio.checked) return;
      state.precision = radio.value;
      precisionNote();
      if (hasQuery() && state.mode !== "lexical") runSearch({ record: true });
    });
  }
  byId("result-count").addEventListener("change", (event) => {
    state.k = Number(event.target.value) || 10;
    if (hasQuery()) runSearch({ record: true });
  });

  NARROW_QUERY.addEventListener?.("change", updatePlaceholder);

  const optionsToggle = byId("options-toggle");
  optionsToggle.addEventListener("click", () => {
    const open = optionsToggle.getAttribute("aria-expanded") !== "true";
    optionsToggle.setAttribute("aria-expanded", String(open));
    byId("options-panel").hidden = !open;
    if (open) enter(byId("options-panel"));
  });

  Object.assign(actions, {
    runSearch,
    retrySearch: () => runSearch({ record: true }),
    applyQuery,
    switchMode,
    canUseMode,
    clearSearch,
  });
}
