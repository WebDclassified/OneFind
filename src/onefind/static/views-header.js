// Header: collection switcher menu, plain-language status pill, theme toggle.

import { api, itemsOf } from "./api.js";
import { byId, el } from "./dom.js";
import { formatInteger, plural, truncate } from "./format.js";
import { icon } from "./icons.js";
import { actions, collectionStatus, on, state } from "./store.js";
import { promptDialog, setupMenu } from "./dialogs.js";
import { cycleTheme, themePref } from "./theme.js";
import { announce, toast, toastError } from "./toast.js";
import { jobPercent } from "./views-index.js";

const THEME_TEXT = { system: "System", light: "Light", dark: "Dark" };
const THEME_ICONS = { system: "monitor", light: "sun", dark: "moon" };

let collectionMenu = null;
let lastStatusLabel = "";

/**
 * The collection's auto-sync status in a small, UI-ready shape, or null when
 * auto-sync is off (or the server does not report it).
 */
export function autoSyncInfo(stats) {
  const sync = stats?.auto_sync;
  if (!sync || typeof sync !== "object" || !sync.enabled) return null;
  const pending = sync.pending && typeof sync.pending === "object" ? sync.pending : {};
  const count = ["new", "changed", "deleted"].reduce((sum, key) => sum + (Number(pending[key]) || 0), 0);
  const state = ["idle", "scanning", "syncing", "paused", "error"].includes(sync.state) ? sync.state : "idle";
  return { state, count, message: typeof sync.message === "string" ? sync.message : "", busy: state === "syncing" || (state === "scanning" && count > 0) };
}

/** Pill label/tone for a ready collection while auto-sync is on (null: leave the pill alone). */
export function autoSyncPill(info) {
  if (!info) return null;
  if (info.busy) {
    return {
      label: info.count ? `Syncing ${plural(info.count, "change")}…` : "Syncing…",
      tone: "busy",
      detail: "Auto-sync is updating the index with changes in your folders. Search keeps working.",
      sync: "",
      quiet: true,
    };
  }
  if (info.state === "error") return { sync: "Auto-sync problem", tone: "warn", detail: info.message || "Auto-sync hit a problem and will try again." };
  if (info.state === "paused") return { sync: "Auto-sync paused", detail: info.message || "Auto-sync is paused." };
  return { sync: "Auto-sync on" };
}

/** Pill label, tone and sentence for the current stats (and running job). */
export function describeStatus(stats, job = stats?.job) {
  const status = collectionStatus(stats);
  const detail = typeof stats?.status_detail === "string" && stats.status_detail ? stats.status_detail : null;
  const running = job && !["succeeded", "failed", "cancelled"].includes(job.status);
  if (status === "connecting") return { label: "Connecting", tone: "muted", detail: "Connecting to the local search service." };
  if (status === "offline") return { label: "Offline", tone: "danger", detail: "OneFind's local service is not responding." };
  if (status === "indexing" || running) {
    const percent = jobPercent(job);
    const rebuilding = job?.kind === "rebuild_vectors";
    const doing = rebuilding ? "Rebuilding" : "Indexing";
    return {
      // "100%" while the job is still saving reads as stuck; leave the number off then.
      label: percent === null || percent >= 100 ? doing : `${doing} ${percent}%`,
      tone: "busy",
      detail: detail || (rebuilding
        ? "Rebuilding vectors for long notes. Search keeps working while it runs."
        : "Indexing is in progress. Search keeps working with what is already indexed."),
    };
  }
  if (status === "needs_attention") {
    return { label: "Needs attention", tone: "warn", detail: detail || "The index needs a check. Open Manage to repair it." };
  }
  if (status === "no_index") return { label: "No index", tone: "muted", detail: detail || "No index is ready yet. Index a folder to start." };
  if (status === "empty") return { label: "No index", tone: "muted", detail: detail || "This collection is empty. Index a folder to add documents." };
  const missing = Number(stats?.vectors_missing) || 0;
  const base = missing > 0 && Number(stats?.vectors) > 0
    ? { label: "Vectors missing", tone: "warn", detail: detail || `${formatInteger(missing)} documents have no vectors yet, so meaning search may miss them.` }
    : {
      label: "Ready",
      tone: "ok",
      detail: detail || (Number(stats?.vectors) > 0 ? "Ready. Search by keyword or meaning." : "Ready for keyword search."),
    };
  const sync = autoSyncPill(autoSyncInfo(stats));
  if (!sync) return base;
  if (sync.label) return sync;
  return {
    ...base,
    tone: base.tone === "ok" && sync.tone ? sync.tone : base.tone,
    detail: sync.detail ? `${base.detail} ${sync.detail}` : `${base.detail} Auto-sync keeps the index up to date.`,
    sync: sync.sync,
  };
}

export function renderStatus(job) {
  const info = describeStatus(state.stats, job ?? state.stats?.job);
  const pill = byId("status-pill");
  pill.dataset.tone = info.tone;
  byId("status-label").textContent = info.label;
  const syncNote = byId("status-sync");
  syncNote.textContent = info.sync ? `· ${info.sync}` : "";
  syncNote.hidden = !info.sync;
  byId("status-detail").textContent = info.detail;
  pill.title = info.detail;
  // The name starts with the visible text (WCAG 2.5.3) and says what a click does.
  const opens = info.tone === "busy" && !info.quiet ? "show progress" : info.label === "Offline" ? "check again" : "show health";
  const visibleText = info.sync ? `${info.label} · ${info.sync}` : info.label;
  pill.setAttribute("aria-label", `${visibleText}. Status: ${opens}`);
  // Announce meaningful changes, not every percentage tick, and never the
  // background upkeep of auto-sync (it would interrupt on every saved note).
  if (!info.quiet) {
    const coarse = info.label.replace(/\d+%/, "");
    if (lastStatusLabel && coarse !== lastStatusLabel) announce(`Status: ${info.label}. ${info.detail}`);
    lastStatusLabel = coarse;
  }
  renderCollectionName();
}

function renderCollectionName() {
  const name = state.stats?.collection?.name;
  byId("collection-name").textContent = name ? truncate(name, 32) : state.stats?.offline ? "Not connected" : "No collection";
}

function renderTheme() {
  const pref = themePref();
  byId("theme-label").textContent = THEME_TEXT[pref];
  byId("theme-toggle").setAttribute("aria-label", `Theme: ${pref === "system" ? "follow system" : THEME_TEXT[pref].toLowerCase()}. Change theme`);
  byId("theme-icon").replaceChildren(icon(THEME_ICONS[pref]));
}

/* ---------- collection flows ---------- */

export async function switchCollection(name) {
  if (name === state.stats?.collection?.name) return;
  try {
    await api.activateCollection(name);
    await actions.onCollectionChanged?.();
    toast(`Switched to “${truncate(name, 40)}”`);
  } catch (error) {
    toastError(error, "That collection could not be opened.");
  }
}

export async function createCollectionFlow(trigger) {
  const name = await promptDialog({
    title: "New collection",
    label: "Collection name",
    placeholder: "Work notes",
    hint: "A collection is one index file. Use separate collections for separate folders, like Notes and Work.",
    confirmLabel: "Create",
    validate: (value) => (value.length > 64 ? "Use 64 characters or fewer." : /[\p{L}\p{N}]/u.test(value) ? null : "Use at least one letter or number."),
    trigger,
  });
  if (!name) return;
  try {
    await api.createCollection(name);
    await api.activateCollection(name);
    await actions.onCollectionChanged?.({ quiet: true });
    toast(`Created “${truncate(name, 40)}”`);
    actions.openIndexWizard?.({ trigger });
  } catch (error) {
    toastError(error, error?.code === "name_taken" ? "A collection with that name already exists." : "The collection could not be created.");
  }
}

export async function openCollectionPrompt(trigger) {
  const path = await promptDialog({
    title: "Open an existing collection",
    label: "Collection file (.db)",
    placeholder: "D:\\Backups\\notes.db",
    message: "A collection is a single .db file. Use this to reopen one you moved from another computer or restored from a backup.",
    hint: "Paste the full path to the file.",
    confirmLabel: "Open collection",
    validate: (value) => (/\.db$/i.test(value) ? null : "Collection files end in .db."),
    trigger,
  });
  if (!path) return;
  try {
    const entry = await api.openCollection(path);
    await actions.onCollectionChanged?.();
    toast(`Opened “${truncate(entry?.name || path, 40)}”`);
  } catch (error) {
    toastError(error, "That file could not be opened as a collection.");
  }
}

async function buildCollectionMenu() {
  const menu = byId("collection-menu");
  let collections = [];
  let failed = false;
  try {
    collections = itemsOf(await api.collections(), "collections");
  } catch {
    failed = true;
  }
  const items = collections.map((item) => el("button", {
    type: "button",
    class: "menu-item",
    attrs: { role: "menuitemradio", "aria-checked": String(Boolean(item.active)) },
    disabled: item.exists === false,
    title: item.exists === false ? "The collection file is missing" : undefined,
    on: { click: () => switchCollection(item.name) },
  },
  el("span", { class: "menu-check", attrs: { "aria-hidden": "true" } }, item.active ? icon("check") : null),
  el("span", { class: "menu-text" },
    el("span", { class: "menu-primary", text: truncate(item.name, 40) }),
    el("span", {
      class: "menu-secondary",
      text: item.exists === false ? "File missing" : `${formatInteger(item.documents ?? 0)} documents`,
    }),
  )));
  if (failed) items.push(el("p", { class: "menu-note", text: "Collections are not available on this server." }));
  const action = (label, iconName, handler) => el("button", {
    type: "button", class: "menu-item", attrs: { role: "menuitem" }, on: { click: handler },
  }, el("span", { class: "menu-check", attrs: { "aria-hidden": "true" } }, icon(iconName)), el("span", { class: "menu-text", text: label }));
  const trigger = byId("collection-button");
  menu.replaceChildren(
    el("p", { class: "menu-heading", text: "Collections" }),
    el("div", { class: "menu-scroll" }, ...items),
    // Always reachable, however many collections there are.
    el("div", { class: "menu-actions" },
      el("hr", { class: "menu-sep" }),
      action("New collection…", "plus", () => createCollectionFlow(trigger)),
      action("Open existing collection file…", "folder-open", () => openCollectionPrompt(trigger)),
      action("Manage collections…", "settings", () => actions.openManage?.({ section: "manage-collections", trigger })),
    ),
  );
}

export function initHeader() {
  collectionMenu = setupMenu(byId("collection-button"), byId("collection-menu"), { onOpen: buildCollectionMenu });
  byId("theme-toggle").addEventListener("click", () => {
    const next = cycleTheme();
    toast(`Theme: ${THEME_TEXT[next]}`, { tone: "info", duration: 1800 });
  });
  byId("status-pill").addEventListener("click", (event) => {
    const status = collectionStatus();
    if (status === "indexing" || actions.jobRunning?.()) actions.showJobProgress?.({ trigger: event.currentTarget });
    else if (status === "offline") actions.refreshStats?.();
    else actions.openManage?.({ section: "manage-health", trigger: event.currentTarget });
  });
  on("theme", renderTheme);
  on("stats", () => renderStatus());
  on("job", (job) => renderStatus(job));
  renderTheme();
  Object.assign(actions, { switchCollection, createCollectionFlow, openCollectionPrompt });
  return collectionMenu;
}
