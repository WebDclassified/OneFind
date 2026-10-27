// OneFind web app entry point. Wires the modules together, loads the
// collection status, and keeps it fresh. Every view builds its DOM with
// createElement/textContent and swaps content with replaceChildren, so
// untrusted document text is never parsed as HTML.

import { api } from "./api.js";
import { byId } from "./dom.js";
import { folderLabel, plural } from "./format.js";
import { settlePage } from "./motion.js";
import {
  actions, autoSyncFinished, canSearch, collectionStatus, emit, emptyFilters, on, state,
} from "./store.js";
import { setupDialog, setupMenu } from "./dialogs.js";
import { initTheme } from "./theme.js";
import { toast } from "./toast.js";
import { showView } from "./view.js";
import { initFilters, loadFacets, renderFilters } from "./views-filters.js";
import { initHeader } from "./views-header.js";
import { forgetChanges, initIdle, recentDocumentTitles, refreshIdle, renderOnboarding } from "./views-idle.js";
import { initIndexWizard, jobRunning, resetJobQueue } from "./views-index.js";
import { initKeyboard } from "./keyboard.js";
import { initManage } from "./views-manage.js";
import { closePreview, initPreview } from "./views-preview.js";
import { initResults, resetRenderedKeys, syncBookmarkButtons } from "./views-results.js";
import {
  applyQuery, clearSearch, configureControls, hasQuery, initSearch, readUrlState, showSuggestions,
} from "./search.js";

const POLL_IDLE_MS = 15000;
const POLL_JOB_MS = 1500;
const POLL_AUTOSYNC_MS = 5000; // auto-sync on: notice finished syncs promptly
const USER_IDLE_MS = 8000; // no key or pointer input for this long = the person is not busy

let statsController = null;
let pollTimer = 0;
let pollDelay = 0;
let previousStatus = null;
let lastSync = { collection: null, syncs: null };
let lastInputAt = Date.now();

/* ---------- stats ---------- */

function homeView() {
  if (!canSearch()) {
    renderOnboarding();
    return "onboarding";
  }
  return "idle";
}

function applyStats(stats) {
  state.stats = stats;
  configureControls();
  emit("stats", stats);
  const status = collectionStatus(stats);
  const statusChanged = status !== previousStatus;
  previousStatus = status;
  if (state.view === "onboarding" && hasQuery() && canSearch()) {
    // A search typed while the first index was running runs now that notes are ready.
    actions.runSearch?.({ record: true });
  } else if (!hasQuery() && ["idle", "onboarding", "typing", "starting"].includes(state.view)) {
    const target = homeView();
    if (target !== state.view || statusChanged) showView(target);
    if (target === "onboarding") renderOnboarding();
  }
  if (stats.job && !["succeeded", "failed", "cancelled"].includes(stats.job.status) && !jobRunning()) {
    actions.watchJob?.(stats.job);
  }
  noticeAutoSync(stats);
}

/* ---------- auto-sync ---------- */

function userIsIdle() {
  return Date.now() - lastInputAt > USER_IDLE_MS && document.visibilityState === "visible";
}

function noticeAutoSync(stats) {
  const { finished, next } = autoSyncFinished(lastSync, stats);
  lastSync = next;
  if (finished) onAutoSynced(stats.auto_sync);
}

/** Quietly bring every view up to date after auto-sync changed the index. */
async function onAutoSynced(sync) {
  const idle = userIsIdle();
  await Promise.all([loadFacets(), state.view === "idle" ? refreshIdle() : null]);
  if (byId("manage-dialog").open) actions.refreshManage?.({ changes: true });
  const dialogOpen = Boolean(document.querySelector("dialog[open]"));
  // Re-run visible results only when the person is not reading or typing:
  // cards moving under the pointer would be disruptive.
  if (idle && state.view === "results" && hasQuery() && !state.previewDocId && !dialogOpen) {
    actions.runSearch?.({ record: false, deferRecord: false });
  }
  const changes = sync?.last_changes || {};
  const count = ["added", "updated", "removed"].reduce((sum, key) => sum + (Number(changes[key]) || 0), 0);
  if (idle && count && !dialogOpen) {
    toast(`Index updated · ${plural(count, "note")} changed`, { tone: "info", duration: 2500 });
  }
}

/** Fetch /api/stats; on failure mark the app offline without losing the query. */
export async function refreshStats() {
  statsController?.abort();
  const controller = new AbortController();
  statsController = controller;
  try {
    const stats = await api.stats(controller.signal);
    applyStats(stats && typeof stats === "object" ? stats : { ready: false });
  } catch (error) {
    if (error?.name === "AbortError") return state.stats;
    applyStats({ offline: true, ready: false, documents: 0, modes: [], precisions: [] });
  } finally {
    if (statsController === controller) statsController = null;
    schedulePoll();
  }
  return state.stats;
}

/**
 * Schedule the next /api/stats refresh. Job updates arrive every 400 ms and
 * pass `keepBusy`: they shorten a pending idle wait, but never push back a
 * busy-rate refresh that is already due, or stats would not refresh (and a
 * search typed during the first index would not run) until the job ended.
 */
function schedulePoll({ keepBusy = false } = {}) {
  const sync = state.stats?.auto_sync;
  const syncing = Boolean(sync?.enabled) && ["scanning", "syncing"].includes(sync?.state);
  const busy = jobRunning() || collectionStatus() === "indexing" || syncing;
  const delay = busy ? POLL_JOB_MS : sync?.enabled ? POLL_AUTOSYNC_MS : POLL_IDLE_MS;
  if (keepBusy && pollTimer && pollDelay <= delay) return;
  window.clearTimeout(pollTimer);
  pollDelay = delay;
  pollTimer = window.setTimeout(() => {
    pollTimer = 0;
    if (document.visibilityState === "visible") refreshStats();
    else schedulePoll();
  }, delay);
}

/* ---------- cross-module flows ---------- */

/** Real queries from this collection: recent document titles, tags, folders. */
export function suggestQueries() {
  const facets = state.facets || {};
  const fromTitles = recentDocumentTitles()
    .map((title) => title.split(/\s+/).slice(0, 4).join(" "))
    .filter((title) => title.length > 2);
  const fromTags = (facets.tags || []).slice(0, 2).map((item) => item.tag).filter(Boolean);
  const fromFolders = (facets.folders || [])
    .map((item) => item.folder)
    .filter((folder) => folder)
    .slice(0, 2)
    .map((folder) => folderLabel(folder).split(" › ").pop());
  const picks = [];
  for (const source of [fromTitles.slice(0, 2), fromTags, fromFolders, fromTitles.slice(2)]) {
    for (const query of source) if (!picks.includes(query)) picks.push(query);
  }
  return picks.slice(0, 3);
}

async function onCollectionChanged({ quiet = false } = {}) {
  // Folders queued for an update belong to the previous collection.
  resetJobQueue();
  lastSync = { collection: null, syncs: null };
  closePreview({ restoreFocus: false });
  clearSearch({ focus: false });
  state.filters = emptyFilters();
  state.facets = null;
  resetRenderedKeys();
  // Drop cards from the previous collection so they can never reappear.
  for (const id of ["results-list", "weak-list", "stage-trail"]) byId(id).replaceChildren();
  forgetChanges();
  renderFilters();
  previousStatus = null;
  await refreshStats();
  await Promise.all([loadFacets(), refreshIdle()]);
  if (byId("manage-dialog").open) actions.refreshManage?.();
  if (!quiet) byId("search-input").focus({ preventScroll: true });
}

async function onIndexComplete(job) {
  await refreshStats();
  if (job?.status === "succeeded") {
    forgetChanges();
    await Promise.all([loadFacets(), refreshIdle()]);
  }
  // An open Manage panel still shows the pre-index change scan and counts.
  if (byId("manage-dialog").open) actions.refreshManage?.({ changes: true });
}

/* ---------- bootstrap ---------- */

function initExportMenu() {
  setupMenu(byId("export-button"), byId("export-menu"));
}

function wireEvents() {
  on("bookmark", ({ docId, bookmarked }) => {
    syncBookmarkButtons(docId, bookmarked);
    if (state.view === "idle") refreshIdle();
  });
  for (const event of ["saved", "history", "activity"]) {
    on(event, () => {
      if (state.view === "idle") refreshIdle();
    });
  }
  on("view", (view) => {
    if (view === "idle") refreshIdle();
    if (view !== "idle" && view !== "onboarding") byId("suggestions").hidden = true;
  });
  on("job", (job) => {
    if (job && !["succeeded", "failed", "cancelled"].includes(job.status)) schedulePoll({ keepBusy: true });
  });
  for (const type of ["keydown", "pointerdown", "wheel"]) {
    document.addEventListener(type, () => {
      lastInputAt = Date.now();
    }, { passive: true, capture: true });
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshStats();
  });
  for (const dialog of document.querySelectorAll("dialog")) setupDialog(dialog);
}

async function bootstrap() {
  initTheme();
  initSearch();
  initFilters();
  initResults();
  initPreview();
  initIdle();
  initIndexWizard();
  initManage();
  initHeader();
  initKeyboard();
  initExportMenu();
  wireEvents();
  Object.assign(actions, { refreshStats, onCollectionChanged, onIndexComplete, suggestQueries, showSuggestions });

  // No home screen until the status is known: an idle screen that flips to
  // the welcome card a moment later reads as a glitch.
  state.view = "starting";
  settlePage();
  await refreshStats();
  if (state.view === "starting") showView(homeView());
  if (state.view === "onboarding") renderOnboarding();
  await loadFacets();

  const fromUrl = readUrlState();
  if (fromUrl.query && canSearch()) {
    // Any website can open this URL: show the results, but keep the query
    // out of the search history until the user acts on it (Enter, a mode
    // change, a retry), which records it then.
    await applyQuery({ ...fromUrl, record: false, deferRecord: false });
  } else {
    await refreshIdle();
  }
}

bootstrap().catch((error) => {
  console.error("OneFind failed to start", error);
  toast("OneFind could not start properly. Try reloading the page.", { tone: "danger" });
});
