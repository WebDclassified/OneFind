// Home screen when no query is active: collection status, recent and saved
// searches, recent documents and bookmarks. Also the first-run welcome.

import { api, itemsOf } from "./api.js";
import { button, byId, el } from "./dom.js";
import {
  formatBytes, formatInteger, modeLabel, plural, readablePath, relativeTime, truncate,
} from "./format.js";
import { icon } from "./icons.js";
import { stagger } from "./motion.js";
import { actions, canSearch, collectionStatus, filtersFromPayload, state } from "./store.js";
import { confirmDialog } from "./dialogs.js";
import { toast, toastError } from "./toast.js";

const changesCache = new Map(); // collection name -> changes payload (null while pending/failed)
let idleData = { history: [], saved: [], docs: [], bookmarks: [] };
const shownKeys = new Map(); // list id -> keys already on screen (only new rows animate)

const GET_STARTED = [
  ["search", "Type a few words, or describe an idea in your own words."],
  ["arrow-right", "Press / to jump to search and ↓ to move through results."],
  ["bookmark", "Bookmark a result or save a search to keep it on this screen."],
];

/* ---------- status card ---------- */

function statRow(label, value, detail) {
  return el("div", { class: "stat" },
    el("dt", { text: label }),
    el("dd", {}, el("strong", { text: value }), detail ? el("span", { class: "stat-detail", text: detail }) : null),
  );
}

function vectorSummary(stats) {
  const vectors = Number(stats.vectors) || 0;
  if (!vectors) return ["None", "Keyword search only"];
  const missing = Number(stats.vectors_missing) || 0;
  if (missing) return [formatInteger(vectors), `${formatInteger(missing)} missing`];
  // With passages a long note has several vectors: say how many notes they cover.
  const vectorNotes = Number(stats.health?.vectors);
  const covers = Number.isFinite(vectorNotes) && vectorNotes > 0 && vectors > vectorNotes
    ? `For ${plural(vectorNotes, "note")} · meaning search on` : "Meaning search on";
  return [formatInteger(vectors), covers];
}

export function renderStatusCard() {
  const card = byId("idle-status");
  const stats = state.stats;
  if (!stats) {
    card.replaceChildren();
    return;
  }
  const name = stats.collection?.name || "This collection";
  const [vectorValue, vectorDetail] = vectorSummary(stats);
  const lastIndexed = stats.last_indexed_at ? relativeTime(stats.last_indexed_at) : "—";
  const children = [
    el("div", { class: "card-head" },
      el("div", { class: "status-heading" },
        icon("database", "icon card-icon"),
        el("h2", { class: "card-title", id: "idle-status-title", text: name }),
      ),
    ),
    el("p", { class: "status-sentence", text: stats.status_detail || defaultDetail(stats) }),
    el("dl", { class: "stat-grid" },
      statRow("Documents", formatInteger(stats.documents)),
      statRow("Vectors", vectorValue, vectorDetail),
      statRow("Last indexed", lastIndexed),
      statRow("Size", formatBytes(stats.db_bytes)),
    ),
  ];
  if (stats.size_note) children.push(el("p", { class: "status-note", text: stats.size_note }));
  children.push(el("div", { class: "changes-line", id: "idle-changes", hidden: true }));
  card.replaceChildren(...children);
  checkChanges();
}

function defaultDetail(stats) {
  const modes = new Set(stats.modes || []);
  return modes.has("semantic")
    ? "Ready. Search by keyword or meaning."
    : "Ready for keyword search. Build vectors to search by meaning.";
}

async function checkChanges() {
  const name = state.stats?.collection?.name || "default";
  if (!byId("idle-changes") || collectionStatus() !== "ready") return;
  if (changesCache.has(name)) {
    renderChanges(changesCache.get(name));
    return;
  }
  changesCache.set(name, null);
  try {
    const changes = await api.changes();
    changesCache.set(name, changes);
    renderChanges(changes);
  } catch {
    // stale check is optional; leave the line hidden
  }
}

/** Sum new/changed/deleted across roots from GET /api/index/changes. */
export function changeCounts(changes) {
  const totals = { new: 0, changed: 0, deleted: 0 };
  for (const root of Array.isArray(changes?.roots) ? changes.roots : []) {
    for (const key of Object.keys(totals)) totals[key] += Number(root?.[key]) || 0;
  }
  return totals;
}

export function changesSentence(changes) {
  const counts = changeCounts(changes);
  const parts = [];
  if (counts.new) parts.push(`${formatInteger(counts.new)} new`);
  if (counts.changed) parts.push(`${formatInteger(counts.changed)} changed`);
  if (counts.deleted) parts.push(`${formatInteger(counts.deleted)} deleted`);
  if (!parts.length) return "";
  const total = counts.new + counts.changed + counts.deleted;
  return `${parts.join(", ")} ${total === 1 ? "file" : "files"} since the last index.`;
}

function renderChanges(changes) {
  const line = byId("idle-changes");
  if (!line) return;
  const sentence = changes?.stale ? changesSentence(changes) : "";
  if (!sentence) {
    line.hidden = true;
    return;
  }
  line.replaceChildren(
    icon("refresh", "icon"),
    el("span", { text: sentence }),
    button("Update index", { variant: "link", onClick: () => actions.reindex?.() }),
  );
  line.hidden = false;
}

export function forgetChanges() {
  changesCache.clear();
}

/* ---------- lists ---------- */

function emptyItem(text) {
  return el("li", { class: "list-empty", text });
}

/**
 * Render a home-screen list. Rows already on screen stay still; only new
 * ones rise in, so a refresh never replays the whole list.
 */
function renderList(listId, items, build, emptyText, keyOf) {
  const list = byId(listId);
  const nodes = items.length ? items.map(build) : [emptyItem(emptyText)];
  list.replaceChildren(...nodes);
  const before = shownKeys.get(listId) || new Set();
  const keys = items.map(keyOf);
  if (items.length && before.size) stagger(nodes.filter((_, index) => !before.has(keys[index])), { step: 30, max: 5 });
  else if (items.length) stagger(nodes, { step: 30, max: 5 });
  shownKeys.set(listId, new Set(keys));
}

/** Hide a card whose list is empty (saved searches, bookmarks): no wall of empty boxes. */
function showCard(listId, visible) {
  const card = byId(listId).closest(".card");
  if (card) card.hidden = !visible;
}

/** After removing a row, focus the row that took its place (or the list's heading). */
function focusRow(listId, index) {
  const buttons = [...byId(listId).querySelectorAll(".list-button")];
  const target = buttons[index] || buttons[index - 1];
  if (target) {
    target.focus({ preventScroll: true });
    return;
  }
  byId("search-input").focus({ preventScroll: true });
}

/** Newest first, one row per query (the same search run twice is one memory). */
export function uniqueHistory(items) {
  const seen = new Set();
  return items.filter((item) => {
    const key = String(item?.query || "").trim().toLowerCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function historyItem(item) {
  const query = String(item.query || "");
  const details = [modeLabel(item.mode)];
  if (Number.isFinite(Number(item.result_count))) details.push(plural(item.result_count, "result"));
  if (item.searched_at) details.push(relativeTime(item.searched_at));
  return el("li", { class: "list-row" },
    el("button", {
      type: "button",
      class: "list-button",
      on: {
        click: () => {
          actions.setFilters?.(filtersFromPayload(item.filters, item.searched_at));
          actions.applyQuery?.({ query, mode: item.mode, precision: item.precision });
        },
      },
    },
    icon("history", "icon list-icon"),
    el("span", { class: "list-text" },
      el("span", { class: "list-primary", text: truncate(query, 90) }),
      el("span", { class: "list-secondary", text: details.join(" · ") }),
    )),
  );
}

function savedItem(item) {
  const run = () => {
    // A replaced saved search keeps its created_at; prefer a newer stamp when the server sends one.
    actions.setFilters?.(filtersFromPayload(item.filters, item.updated_at || item.created_at));
    actions.applyQuery?.({ query: item.query, mode: item.mode, precision: item.precision });
  };
  const remove = async (event) => {
    const ok = await confirmDialog({
      title: "Remove saved search?",
      message: `“${truncate(item.name, 60)}” will be removed from your saved searches.`,
      confirmLabel: "Remove",
      trigger: event.currentTarget,
    });
    if (!ok) return;
    const index = idleData.saved.indexOf(item);
    try {
      await api.deleteSaved(item.id);
      toast("Saved search removed");
      await refreshIdle();
      focusRow("saved-list", index);
    } catch (error) {
      toastError(error);
    }
  };
  return el("li", { class: "list-row" },
    el("button", { type: "button", class: "list-button", on: { click: run } },
      icon("star", "icon list-icon"),
      el("span", { class: "list-text" },
        el("span", { class: "list-primary", text: truncate(item.name || item.query, 80) }),
        el("span", { class: "list-secondary", text: `${modeLabel(item.mode)} · “${truncate(item.query, 60)}”` }),
      )),
    button(`Remove saved search ${item.name || item.query}`, { compact: true, iconName: "trash", onClick: remove, className: "row-action" }),
  );
}

function docItem(item, { removable = false } = {}) {
  // updated_at is when the note was (re)indexed; modified_at is the file's own date.
  const when = item.last_opened_at
    ? `Opened ${relativeTime(item.last_opened_at)}`
    : item.created_at && removable ? `Saved ${relativeTime(item.created_at)}`
      : item.modified_at ? `Modified ${relativeTime(item.modified_at)}`
        : item.updated_at ? `Updated ${relativeTime(item.updated_at)}` : "";
  const path = readablePath(item.source || item.doc_id, 3);
  const row = el("li", { class: "list-row" },
    el("button", {
      type: "button",
      class: "list-button",
      on: { click: (event) => actions.openPreview?.(item.doc_id, { trigger: event.currentTarget }) },
    },
    icon(removable ? "bookmark" : "file", "icon list-icon"),
    el("span", { class: "list-text" },
      el("span", { class: "list-primary", text: truncate(item.title || item.doc_id, 80) }),
      el("span", { class: "list-secondary", text: [path, when].filter(Boolean).join(" · ") }),
    )),
  );
  if (removable) {
    row.append(button(`Remove bookmark ${item.title || item.doc_id}`, {
      compact: true,
      iconName: "x",
      className: "row-action",
      onClick: async () => {
        const index = idleData.bookmarks.indexOf(item);
        try {
          await api.removeBookmark(item.doc_id);
          toast("Bookmark removed");
          await refreshIdle();
          focusRow("bookmarks-list", index);
        } catch (error) {
          toastError(error);
        }
      },
    }));
  }
  return row;
}

function renderGetStarted() {
  byId("recent-title").textContent = "Get started";
  byId("recent-list").replaceChildren(...GET_STARTED.map(([iconName, text]) => el("li", { class: "tip-row" },
    icon(iconName, "icon list-icon"), el("span", { text }))));
  shownKeys.delete("recent-list");
}

function renderLists() {
  const history = uniqueHistory(idleData.history).slice(0, 6);
  if (history.length) {
    byId("recent-title").textContent = "Recent searches";
    renderList("recent-list", history, historyItem, "", (item) => `${item.query}`);
  } else {
    renderGetStarted();
  }
  byId("history-clear").hidden = history.length === 0;
  renderList("saved-list", idleData.saved.slice(0, 6), savedItem, "Save a search from its results to run it again in one click.",
    (item) => String(item.id ?? item.name));
  showCard("saved-list", idleData.saved.length > 0);
  renderList("docs-list", idleData.docs.slice(0, 6), (item) => docItem(item),
    "Documents you open, and recently changed ones, will appear here.", (item) => item.doc_id);
  renderList("bookmarks-list", idleData.bookmarks.slice(0, 6), (item) => docItem(item, { removable: true }),
    "Bookmark a result to keep it close at hand.", (item) => item.doc_id);
  showCard("bookmarks-list", idleData.bookmarks.length > 0);
}

async function settle(promise) {
  try {
    return await promise;
  } catch {
    return null;
  }
}

let idleSeq = 0;

/**
 * Reload the home screen lists. Missing endpoints simply show empty lists.
 * The newest call wins: a slow answer from before a collection switch must
 * not replace the new collection's lists.
 */
export async function refreshIdle() {
  const seq = ++idleSeq;
  renderStatusCard();
  if (!canSearch()) return;
  const [history, saved, docs, bookmarks] = await Promise.all([
    settle(api.history()),
    settle(api.saved()),
    settle(api.recentDocuments(8)),
    settle(api.bookmarks()),
  ]);
  if (seq !== idleSeq) return;
  idleData = {
    history: itemsOf(history),
    saved: itemsOf(saved),
    docs: itemsOf(docs),
    bookmarks: itemsOf(bookmarks),
  };
  renderLists();
}

export function recentDocumentTitles() {
  return idleData.docs.map((item) => item.title).filter(Boolean);
}

/* ---------- onboarding ---------- */

const ONBOARDING = {
  no_index: {
    eyebrow: "No index ready",
    title: "Search your notes privately, on this computer",
    copy: "Point OneFind at a folder of notes or documents. It builds a single index file you can search by keyword or by meaning. Nothing leaves this machine.",
  },
  empty: {
    eyebrow: "This collection is empty",
    title: "Add a folder to this collection",
    copy: "Add a folder of notes or documents to this collection. OneFind keeps everything in one local file and never sends your text anywhere.",
  },
  indexing: {
    eyebrow: "Indexing in progress",
    title: "Your notes are being indexed",
    copy: "Search becomes available as soon as the first files are indexed. You can follow the progress here.",
  },
  offline: {
    eyebrow: "Not connected",
    title: "OneFind's local service is not responding",
    copy: "Start it again with “onefind serve”, then try again. Your collection file is safe.",
  },
};

export function renderOnboarding() {
  const status = collectionStatus();
  const content = ONBOARDING[status] || ONBOARDING.no_index;
  const name = state.stats?.collection?.name;
  // A collection name keeps its own capitalisation inside the uppercase eyebrow.
  byId("welcome-eyebrow").replaceChildren(...(status === "empty" && name
    ? [el("span", { class: "eyebrow-name", text: `“${name}”` }), " is empty"]
    : [content.eyebrow]));
  byId("welcome-title").textContent = status === "empty" && name ? `Add a folder to “${truncate(name, 40)}”` : content.title;
  byId("welcome-copy").textContent = content.copy;
  const primary = byId("onboard-index");
  const secondary = byId("onboard-open");
  primary.querySelector("span").textContent = status === "offline" ? "Try again"
    : status === "indexing" ? "Show progress" : "Index a folder";
  primary.dataset.intent = status === "offline" ? "retry" : status === "indexing" ? "progress" : "index";
  secondary.hidden = status === "offline" || status === "indexing";
}

export function initIdle() {
  byId("history-clear").addEventListener("click", async (event) => {
    const ok = await confirmDialog({
      title: "Clear search history?",
      message: "Your recent searches in this collection will be removed. Saved searches and bookmarks stay.",
      confirmLabel: "Clear history",
      trigger: event.currentTarget,
    });
    if (!ok) return;
    try {
      await api.clearHistory();
      toast("Search history cleared");
      refreshIdle();
    } catch (error) {
      toastError(error);
    }
  });
  byId("onboard-index").addEventListener("click", (event) => {
    const intent = event.currentTarget.dataset.intent;
    if (intent === "retry") actions.refreshStats?.();
    else if (intent === "progress") actions.showJobProgress?.({ trigger: event.currentTarget });
    else actions.openIndexWizard?.({ trigger: event.currentTarget, newCollection: !state.stats?.collection?.name });
  });
  byId("onboard-open").addEventListener("click", (event) => actions.openCollectionPrompt?.(event.currentTarget));
  Object.assign(actions, { refreshIdle, renderOnboarding });
}
