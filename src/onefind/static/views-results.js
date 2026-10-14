// Result cards, "why this result", weaker-match grouping, summary line,
// saving and exporting. All document text is rendered as text nodes.

import { api } from "./api.js";
import {
  append, button, byId, copyText, debounce, downloadText, el, highlightedText, setButtonLabel,
} from "./dom.js";
import {
  fileName, folderLabel, formatDate, formatInteger, modeLabel, pathParts, PRECISION_LABELS, plural, truncate,
} from "./format.js";
import { icon } from "./icons.js";
import { capturePositions, flip, stagger } from "./motion.js";
import { actions, activeFilterCount, emit, filtersPayload, on, state } from "./store.js";
import { confirmDialog, promptDialog, setupFloatingMenu, withNetworkConfirm } from "./dialogs.js";
import { announce, toast, toastError } from "./toast.js";
import { renderStageTrail } from "./views-loading.js";

export const CONFIDENCE = {
  strong: { label: "Strong match", level: 3 },
  moderate: { label: "Good match", level: 2 },
  weak: { label: "Weak match", level: 1 },
};

const cardData = new WeakMap(); // article -> normalized result
let renderedKeys = new Set();
let cardMenu = null; // the shared "More" menu

/* ---------- normalisation ---------- */

function normalizeSegments(value) {
  if (value && typeof value === "object" && Array.isArray(value.segments)) {
    return value.segments
      .filter((segment) => segment && typeof segment.text === "string")
      .slice(0, 120)
      .map((segment) => ({ text: segment.text.slice(0, 4000), highlight: Boolean(segment.highlight) }));
  }
  if (typeof value === "string") return [{ text: value.slice(0, 4000), highlight: false }];
  return [];
}

const stringList = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === "string") : []);

export function normalizeResult(raw, index = 0) {
  const data = raw && typeof raw === "object" ? raw : {};
  const score = data.score && typeof data.score === "object" ? data.score : { value: data.score, metric: "score" };
  const explain = data.explain && typeof data.explain === "object" ? data.explain : null;
  const confidence = [data.confidence, explain?.confidence].find((value) => Object.hasOwn(CONFIDENCE, value || "")) || null;
  const docId = typeof data.doc_id === "string" ? data.doc_id : "";
  const source = typeof data.source === "string" && data.source ? data.source : docId;
  return {
    rank: Number.isInteger(data.rank) ? data.rank : index + 1,
    docId,
    title: typeof data.title === "string" && data.title.trim() ? data.title : fileName(source) || docId || "Untitled",
    source,
    folder: typeof data.folder === "string" ? data.folder : null,
    ext: typeof data.ext === "string" ? data.ext : null,
    modifiedAt: typeof data.modified_at === "string" ? data.modified_at : null,
    tags: stringList(data.tags),
    bookmarked: Boolean(data.bookmarked),
    duplicates: stringList(data.duplicates),
    confidence,
    explain,
    snippet: normalizeSegments(data.snippet),
    score: Number.isFinite(Number(score.value)) ? Number(score.value) : null,
    metric: typeof score.metric === "string" ? score.metric : "score",
  };
}

export function normalizeResponse(payload) {
  if (!payload || !Array.isArray(payload.results)) throw new Error("The search service sent an unexpected response.");
  const meta = payload.meta && typeof payload.meta === "object" ? payload.meta : {};
  const results = payload.results.map(normalizeResult);
  const weak = results.filter((result) => result.confidence === "weak");
  return {
    results,
    strong: results.filter((result) => result.confidence !== "weak"),
    weak,
    meta: {
      mode: typeof meta.mode === "string" ? meta.mode : state.mode,
      precision: typeof meta.precision === "string" ? meta.precision : null,
      tookMs: Number(meta.took_ms),
      stages: Array.isArray(meta.stages) ? meta.stages : [],
      abstained: Boolean(meta.abstained) || (results.length > 0 && weak.length === results.length),
      weakCount: Number.isFinite(Number(meta.weak_count)) ? Number(meta.weak_count) : weak.length,
      // Keyword leg topped up with partial matches (lexical_match="auto");
      // strictCount = notes that contained every keyword, stopwords aside (null when unknown).
      relaxed: Boolean(meta.relaxed),
      strictCount: Number.isInteger(meta.strict_count) ? meta.strict_count : null,
    },
  };
}

/* ---------- card pieces ---------- */

function confidenceBadge(confidence) {
  const info = CONFIDENCE[confidence];
  if (!info) return null;
  const bars = el("span", { class: "signal", attrs: { "aria-hidden": "true" } }, el("i"), el("i"), el("i"));
  return el("span", { class: "confidence", dataset: { level: confidence } },
    bars, el("span", { text: info.label }));
}

export function sourceLine(result) {
  const parts = pathParts(result.source);
  const line = el("p", { class: "result-path", title: result.source || undefined });
  line.append(icon(parts.length > 1 ? "folder" : "file", "icon path-icon"));
  const visible = parts.length > 4 ? ["…", ...parts.slice(-4)] : parts;
  visible.forEach((part, index) => {
    if (index > 0) line.append(el("span", { class: "path-sep", text: "›", attrs: { "aria-hidden": "true" } }));
    const isFile = index === visible.length - 1;
    line.append(el(isFile ? "strong" : "span", { class: isFile ? "path-file" : "path-part", text: part }));
  });
  return line;
}

function metaLine(result) {
  const items = [
    el("span", { class: "meta-item meta-bookmark", hidden: !result.bookmarked }, icon("bookmark"), "Bookmarked"),
  ];
  if (result.modifiedAt) {
    items.push(el("span", { class: "meta-item" }, icon("clock"), `Modified ${formatDate(result.modifiedAt)}`));
  }
  if (result.ext) items.push(el("span", { class: "meta-item", text: result.ext.replace(/^\./, "").toUpperCase() }));
  if (result.tags.length) {
    items.push(el("ul", { class: "tag-list", attrs: { "aria-label": "Tags" } },
      result.tags.slice(0, 6).map((tag) => el("li", { class: "tag", text: `#${tag}` }))));
  }
  return el("div", { class: "result-meta" }, items);
}

function duplicateNote(result) {
  if (!result.duplicates.length) return null;
  const names = result.duplicates.slice(0, 2).map((id) => fileName(id) || id);
  const more = result.duplicates.length - names.length;
  const text = `Same text also appears in ${names.join(", ")}${more > 0 ? ` and ${plural(more, "other")}` : ""}.`;
  return el("p", { class: "dup-note" }, icon("copy"), text);
}

function termChips(label, terms, tone) {
  if (!terms.length) return null;
  return el("div", { class: "why-terms" },
    el("span", { class: "why-label", text: label }),
    el("ul", { class: "term-list" }, terms.map((term) => el("li", { class: `term term-${tone}`, text: term }))),
  );
}

function legLines(result, mode) {
  const ex = result.explain || {};
  const lines = [];
  const lexicalRank = Number(ex.lexical_rank);
  const semanticRank = Number(ex.semantic_rank);
  if (mode !== "semantic") {
    lines.push(Number.isFinite(lexicalRank) && ex.lexical_rank !== null
      ? `Keyword search ranked it #${lexicalRank}.`
      : "Keyword search did not find it.");
  }
  if (mode !== "lexical") {
    const similarity = Number(ex.semantic_score);
    lines.push(Number.isFinite(semanticRank) && ex.semantic_rank !== null
      ? `Meaning search ranked it #${semanticRank}${Number.isFinite(similarity) ? ` (similarity ${similarity.toFixed(2)})` : ""}.`
      : "Meaning search did not find it.");
  }
  return lines;
}

export function explainPanel(result, meta) {
  const ex = result.explain;
  const mode = ex?.mode || meta.mode;
  const panel = el("div", { class: "why-panel" });
  panel.append(el("h4", { class: "why-title", text: "Why this result" }));
  const reasons = stringList(ex?.reasons);
  if (reasons.length) {
    panel.append(el("ul", { class: "why-reasons" }, reasons.map((reason) => el("li", { text: reason }))));
  } else {
    panel.append(el("p", { class: "why-copy", text: `Ranked #${result.rank} by ${modeLabel(mode).toLowerCase()} search.` }));
  }
  append(panel, [
    termChips("Matched words", stringList(ex?.matched_terms), "matched"),
    termChips("Not found", stringList(ex?.missing_terms), "missing"),
  ]);
  if (ex) {
    panel.append(el("ul", { class: "why-legs" }, legLines(result, mode).map((line) => el("li", { text: line }))));
  }
  const facts = [`Mode: ${modeLabel(mode)}`];
  const precision = ex?.precision || meta.precision;
  if (mode !== "lexical" && precision) facts.push(`Vector precision: ${PRECISION_LABELS[precision] || precision}`);
  if (result.score !== null) facts.push(`${result.metric}: ${result.score.toFixed(4)}`);
  panel.append(el("p", { class: "why-facts", text: facts.join(" · ") }));
  return panel;
}

/* ---------- actions ---------- */

export function snippetText(result) {
  return result.snippet.map((segment) => segment.text).join("").replace(/\s+/g, " ").trim();
}

export async function openResult(result, { reveal = false } = {}) {
  try {
    const opened = await withNetworkConfirm((confirm) => api.openDocument(result.docId, reveal, confirm));
    if (opened === null) return;
    toast(reveal ? `Showing “${truncate(result.title, 40)}” in its folder` : `Opened “${truncate(result.title, 40)}”`);
    emit("activity");
  } catch (error) {
    if (error.code === "file_missing" || error.action === "reindex") {
      toast("That file is no longer where it was indexed.", {
        tone: "warn",
        action: { label: "Update index", onClick: () => actions.reindex?.() },
      });
    } else if (error.code === "open_disabled") {
      toast("Opening files is turned off for this server. Use Preview instead.", { tone: "warn" });
    } else {
      toastError(error, "The file could not be opened.");
    }
  }
}

export async function copyResult(result) {
  const text = snippetText(result) || result.title;
  const ok = await copyText(text);
  toast(ok ? "Snippet copied" : "Copy failed. Select the text in the preview instead.", { tone: ok ? "success" : "warn" });
}

export async function toggleBookmark(result) {
  const next = !result.bookmarked;
  try {
    if (next) await api.addBookmark(result.docId);
    else await api.removeBookmark(result.docId);
    result.bookmarked = next;
    emit("bookmark", { docId: result.docId, bookmarked: next });
    toast(next ? "Bookmarked" : "Bookmark removed");
  } catch (error) {
    toastError(error, "The bookmark could not be saved.");
  }
}

/**
 * Take a document out of the index (the file on disk stays). Shared by the
 * card menu and the preview; emits "document-removed" on success.
 */
export async function removeFromIndex(result, { trigger } = {}) {
  if (!result?.docId) return false;
  const ok = await confirmDialog({
    title: "Remove from index?",
    message: `“${truncate(result.title || result.docId, 80)}” will no longer appear in search. The file on disk is not touched. Updating the index for its folder adds it back unless you exclude it in the index options.`,
    confirmLabel: "Remove from index",
    danger: true,
    trigger,
  });
  if (!ok) return false;
  try {
    await api.removeDocuments([result.docId]);
  } catch (error) {
    toastError(error, "The note could not be removed from the index.");
    return false;
  }
  emit("document-removed", { docId: result.docId });
  toast("Removed from index");
  announce(`${result.title || "The note"} was removed from the index.`);
  // Counts, facets, home lists and the change report all describe the index.
  actions.onIndexComplete?.({ status: "succeeded" });
  return true;
}

/** Drop a removed document's cards; keyboard focus moves to a neighbour. */
function dropRemovedCards(docId) {
  const cards = visibleCards();
  const removed = [...document.querySelectorAll("#view-results .result-card")].filter((card) => card.dataset.docId === docId);
  if (!removed.length) return;
  const active = document.activeElement;
  const focusLost = !active || active === document.body || removed.some((card) => card.contains(active))
    || Boolean(byId("preview")?.contains(active));
  const index = cards.indexOf(removed[0]);
  const neighbour = cards.slice(index + 1).find((card) => !removed.includes(card))
    || cards.slice(0, Math.max(index, 0)).reverse().find((card) => !removed.includes(card)) || null;
  for (const card of removed) card.closest(".result-item")?.remove();
  renderedKeys.delete(docId);
  const last = state.lastResponse;
  if (last) {
    const keep = (result) => result.docId !== docId;
    last.results = last.results.filter(keep);
    last.strong = last.strong.filter(keep);
    last.weak = last.weak.filter(keep);
    byId("results-list").hidden = last.strong.length === 0;
    byId("weak-group").hidden = last.weak.length === 0;
    byId("weak-label").textContent = `Weaker matches (${last.weak.length})`;
    byId("results-summary").textContent = summaryText(last);
  }
  if (focusLost) {
    if (neighbour) focusCard(neighbour);
    else byId("search-input").focus({ preventScroll: true });
  }
}

function syncBookmarkButtons(docId, bookmarked) {
  for (const node of document.querySelectorAll(".bookmark-button")) {
    if (node.dataset.docId !== docId) continue;
    node.setAttribute("aria-pressed", String(bookmarked));
    setButtonLabel(node, bookmarked ? "Bookmarked" : "Bookmark");
  }
  for (const card of document.querySelectorAll(".result-card")) {
    const result = cardData.get(card);
    if (result?.docId !== docId) continue;
    result.bookmarked = bookmarked;
    const flag = card.querySelector(".meta-bookmark");
    if (flag) flag.hidden = !bookmarked;
  }
}

function toggleExplain(card) {
  const toggle = card.querySelector(".why-button");
  const panel = card.querySelector(".why-panel");
  if (!toggle || !panel) return;
  const open = toggle.getAttribute("aria-expanded") !== "true";
  toggle.setAttribute("aria-expanded", String(open));
  panel.hidden = !open;
  card.classList.toggle("is-explained", open);
}

/** Run a named action on a card (shared by buttons, the More menu and keyboard shortcuts). */
export function cardAction(card, name) {
  const result = cardData.get(card);
  if (!result) return;
  if (name === "preview") actions.openPreview?.(result.docId, { trigger: card });
  else if (name === "open") openResult(result);
  else if (name === "reveal") openResult(result, { reveal: true });
  else if (name === "copy") copyResult(result);
  else if (name === "bookmark") toggleBookmark(result);
  else if (name === "explain") toggleExplain(card);
  else if (name === "folder" && result.folder !== null) actions.filterToFolder?.(result.folder);
  else if (name === "remove") removeFromIndex(result, { trigger: card.querySelector(".more-button") || card });
}

function menuItem(label, iconName, onClick, { shortcut = null, attrs = {} } = {}) {
  return el("button", { type: "button", class: "menu-item", attrs: { role: "menuitem", ...attrs }, on: { click: onClick } },
    el("span", { class: "menu-check", attrs: { "aria-hidden": "true" } }, icon(iconName)),
    el("span", { class: "menu-text", text: label }),
    shortcut ? el("kbd", { class: "menu-kbd", text: shortcut, attrs: { "aria-hidden": "true" } }) : null,
  );
}

/** Secondary card actions live in one shared menu, so cards stay calm. */
function openCardMenu(card, trigger) {
  const result = cardData.get(card);
  if (!result || !cardMenu) return;
  const run = (name) => () => cardAction(card, name);
  const items = [
    menuItem("Copy snippet", "copy", run("copy"), { shortcut: "C", attrs: { "aria-keyshortcuts": "C" } }),
    menuItem(result.bookmarked ? "Remove bookmark" : "Bookmark", "bookmark", run("bookmark"), {
      shortcut: "B", attrs: { "aria-keyshortcuts": "B" },
    }),
    menuItem("Show in folder", "folder-open", run("reveal")),
  ];
  if (result.folder !== null) {
    items.push(menuItem("Filter to this folder", "filter", run("folder"), {
      attrs: { title: `Only show results in ${folderLabel(result.folder)}` },
    }));
  }
  items.push(menuItem("Remove from index…", "trash", run("remove"), {
    attrs: { title: "Stop showing this note in search. The file is not deleted." },
  }));
  cardMenu.openFor(trigger, items, { focusFirst: true });
}

function actionBar(result, card, panelId) {
  const run = (name) => () => cardAction(card, name);
  const action = (label, iconName, name, extra = {}) => {
    const node = button(label, { iconName, onClick: run(name), ...extra });
    node.dataset.action = name;
    return node;
  };
  const why = action("Explain", "help", "explain", {
    className: "why-button",
    title: "Why this result",
    attrs: { "aria-expanded": "false", "aria-controls": panelId, "aria-keyshortcuts": "E" },
  });
  const more = button("More", {
    iconName: "more",
    className: "more-button",
    attrs: { "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "card-menu" },
    onClick: (event) => openCardMenu(card, event.currentTarget),
  });
  more.dataset.action = "more";
  return el("div", { class: "result-actions", attrs: { role: "group", "aria-label": `Actions for ${result.title}` } },
    action("Open", "open", "open", { attrs: { "aria-keyshortcuts": "O" } }),
    action("Preview", "eye", "preview", { attrs: { "aria-keyshortcuts": "Enter" } }),
    why,
    more,
  );
}

let cardSerial = 0;

function buildCard(result, meta) {
  cardSerial += 1;
  const id = (part) => `result-${part}-${cardSerial}`;
  const card = el("article", {
    class: "result-card",
    tabIndex: -1,
    attrs: {
      "aria-labelledby": id("title"),
      "aria-keyshortcuts": "Enter O C B E",
    },
    dataset: { docId: result.docId, confidence: result.confidence || "none" },
  });
  cardData.set(card, result);

  const badge = confidenceBadge(result.confidence);
  if (badge) badge.id = id("conf");
  const path = sourceLine(result);
  path.id = id("path");
  card.append(el("div", { class: "result-head" },
    el("h3", { class: "result-title", id: id("title"), text: result.title }),
    badge,
  ), path);
  let snippet = null;
  if (result.snippet.length) {
    snippet = el("p", { class: "result-snippet", id: id("snippet") }, highlightedText(result.snippet));
    card.append(snippet);
  }
  // Screen readers hear how good the match is, where it lives and what it says.
  card.setAttribute("aria-describedby", [badge, path, snippet].filter(Boolean).map((node) => node.id).join(" "));
  append(card, [
    duplicateNote(result),
    el("div", { class: "result-foot" }, metaLine(result), actionBar(result, card, id("why"))),
  ]);
  const panel = explainPanel(result, meta);
  panel.id = id("why");
  panel.hidden = true;
  card.append(panel);

  card.addEventListener("click", (event) => {
    if (event.target.closest("button, a, input, select, .why-panel")) return;
    if (window.getSelection()?.toString()) return; // let people select snippet text
    focusCard(card);
    cardAction(card, "preview");
  });
  card.addEventListener("focusin", () => setRovingCard(card));
  return el("li", { class: "result-item", dataset: { key: result.docId } }, card);
}

/* ---------- roving focus ---------- */

export function visibleCards() {
  return [...document.querySelectorAll("#view-results .result-card")].filter((card) => card.offsetParent !== null);
}

/**
 * One Tab stop for the whole list: only the current card and its action
 * buttons are tabbable; arrows (or j/k) move between cards.
 */
function setRovingCard(active) {
  for (const card of document.querySelectorAll("#view-results .result-card")) {
    const current = card === active;
    card.tabIndex = current ? 0 : -1;
    card.classList.toggle("is-current", current);
    for (const node of card.querySelectorAll(".result-actions button")) node.tabIndex = current ? 0 : -1;
  }
}

/** Highlight the card whose document is open in the preview. */
export function markPreviewed(docId) {
  for (const card of document.querySelectorAll("#view-results .result-card")) {
    card.classList.toggle("is-previewed", Boolean(docId) && card.dataset.docId === docId);
  }
}

// With the side preview open, moving through results updates it (like Mail or Raycast).
const followPreview = debounce((card) => {
  if (card.isConnected && actions.isSidePreview?.()) {
    actions.openPreview?.(card.dataset.docId, { trigger: card, focus: false });
  }
}, 120);

export function focusCard(card, { follow = false } = {}) {
  if (!card) return;
  setRovingCard(card);
  card.focus({ preventScroll: true });
  card.scrollIntoView({ block: "nearest", behavior: "auto" });
  if (follow && card.dataset.docId !== state.previewDocId) followPreview(card);
}

export function moveFocus(step) {
  const cards = visibleCards();
  if (!cards.length) return false;
  let index = cards.indexOf(document.activeElement?.closest?.(".result-card"));
  if (index === -1 && state.previewDocId) {
    index = cards.findIndex((card) => card.dataset.docId === state.previewDocId);
  }
  if (index === 0 && step < 0) {
    // Up from the first result goes back to the search box.
    byId("search-input").focus();
    return true;
  }
  const next = index === -1 ? (step > 0 ? 0 : cards.length - 1) : Math.max(0, Math.min(cards.length - 1, index + step));
  focusCard(cards[next], { follow: true });
  return true;
}

/* ---------- rendering ---------- */

function summaryText(view) {
  const strong = view.strong.length;
  const weak = view.weak.length;
  const parts = view.meta.abstained
    ? ["No strong matches", `${formatInteger(weak)} weaker`]
    : [plural(strong, "result"), ...(weak ? [`${formatInteger(weak)} weaker`] : [])];
  parts.push(modeLabel(view.meta.mode));
  const filters = activeFilterCount();
  if (filters) parts.push(plural(filters, "filter"));
  return parts.join(" · ");
}

function setWeakOpen(open) {
  const toggle = byId("weak-toggle");
  toggle.setAttribute("aria-expanded", String(open));
  byId("weak-list").hidden = !open;
  byId("weak-group").classList.toggle("is-open", open);
  const show = byId("abstain-notice").querySelector(".abstain-toggle");
  if (show) {
    show.setAttribute("aria-expanded", String(open));
    setButtonLabel(show, open ? "Hide weaker matches" : `Show ${plural(byId("weak-list").children.length, "weaker match", "weaker matches")}`);
  }
}

function renderAbstain(view, query) {
  const notice = byId("abstain-notice");
  byId("weak-group").classList.toggle("is-abstained", view.meta.abstained);
  if (!view.meta.abstained) {
    notice.hidden = true;
    notice.replaceChildren();
    return;
  }
  // One control for the weaker list: the notice's own toggle replaces the group header.
  const show = button(`Show ${plural(view.weak.length, "weaker match", "weaker matches")}`, {
    variant: "secondary",
    iconName: "chevron-down",
    className: "abstain-toggle",
    attrs: { "aria-expanded": "false", "aria-controls": "weak-list" },
    onClick: () => {
      const open = byId("weak-list").hidden;
      setWeakOpen(open);
      if (open) moveFocus(1);
    },
  });
  const tips = el("div", { class: "notice-actions" }, show);
  if (view.meta.mode === "lexical" && actions.canUseMode?.("hybrid")) {
    tips.append(button("Try Hybrid", { variant: "ghost", iconName: "blend", onClick: () => actions.switchMode?.("hybrid") }));
  }
  if (activeFilterCount()) {
    tips.append(button("Clear filters", { variant: "ghost", iconName: "x", onClick: () => actions.clearFilters?.() }));
  }
  notice.replaceChildren(
    icon("info", "icon notice-icon"),
    el("div", { class: "notice-body" },
      el("p", { class: "notice-title", text: "No strong matches" }),
      el("p", { text: `Nothing in this collection closely matches “${truncate(query, 80)}”. The closest documents are below, but they may not be what you are looking for.` }),
      tips,
    ),
  );
  notice.hidden = false;
}

function fillList(list, results, meta, animate, before) {
  const items = results.map((result) => buildCard(result, meta));
  list.replaceChildren(...items);
  const fresh = items.filter((item) => !renderedKeys.has(item.dataset.key));
  const keyOf = (item) => item.dataset.key;
  if (animate === "all") stagger(items);
  else if (animate === "changed") {
    stagger(fresh);
    flip(items.filter((item) => renderedKeys.has(item.dataset.key)), before, keyOf);
  }
  return items;
}

/** Where keyboard focus is in the results, so a re-render can put it back. */
function focusAnchor() {
  const active = document.activeElement;
  const card = active?.closest?.("#view-results .result-card");
  if (!card) return null;
  return { docId: card.dataset.docId, action: active === card ? null : active.dataset?.action || null };
}

function restoreFocusAnchor(anchor) {
  if (!anchor || (document.activeElement && document.activeElement !== document.body)) return;
  const cards = [...document.querySelectorAll("#view-results .result-card")];
  const card = cards.find((node) => node.dataset.docId === anchor.docId) || visibleCards()[0];
  if (!card) return;
  setRovingCard(card);
  const target = anchor.action && card.dataset.docId === anchor.docId
    ? card.querySelector(`.result-actions [data-action="${anchor.action}"]`)
    : null;
  (target || card).focus({ preventScroll: true });
}

/**
 * Render a search response. `refresh` = the previous view already showed
 * results: only new cards animate and kept cards glide to their new place.
 */
export function renderResults(payload, { query, refresh = false, stages = null } = {}) {
  const view = normalizeResponse(payload);
  state.lastResponse = { ...view, query };
  const keyOf = (item) => item.dataset.key;
  const anchor = focusAnchor();
  const currentKey = document.querySelector("#view-results .result-card.is-current")?.dataset.docId || null;
  const before = refresh
    ? capturePositions(document.querySelectorAll("#view-results .result-item"), keyOf)
    : new Map();
  const animate = refresh ? "changed" : "all";

  const strongItems = fillList(byId("results-list"), view.strong, view.meta, animate, before);
  byId("results-list").hidden = view.strong.length === 0;
  const weakGroup = byId("weak-group");
  weakGroup.hidden = view.weak.length === 0;
  byId("weak-label").textContent = `Weaker matches (${view.weak.length})`;
  const weakItems = fillList(byId("weak-list"), view.weak, view.meta, "none", before);
  renderAbstain(view, query);
  setWeakOpen(false);

  renderedKeys = new Set([...strongItems, ...weakItems].map(keyOf));
  byId("results-summary").textContent = summaryText(view);
  // Saving or exporting "no strong matches" is not useful.
  byId("results-tools").hidden = view.meta.abstained;
  const note = byId("results-note");
  const partialOnly = view.meta.relaxed && view.meta.strictCount === 0 && view.meta.mode !== "semantic";
  note.textContent = partialOnly ? "No note contains all of your keywords, so notes with some of them are included." : "";
  note.hidden = !partialOnly;
  renderStageTrail(stages?.length ? stages : view.meta.stages, Number.isFinite(view.meta.tookMs) ? view.meta.tookMs : null);

  const cards = [...byId("view-results").querySelectorAll(".result-card")];
  setRovingCard(cards.find((card) => card.dataset.docId === currentKey) || cards[0] || null);
  markPreviewed(state.previewDocId);
  restoreFocusAnchor(anchor);

  announce(view.meta.abstained
    ? `No strong matches. ${plural(view.weak.length, "weaker match", "weaker matches")} available.`
    : `${summaryText(view)}.`);
  return view;
}

export function resetRenderedKeys() {
  renderedKeys = new Set();
}

/* ---------- save & export ---------- */

export async function saveCurrentSearch(trigger) {
  const last = state.lastResponse;
  if (!last?.query) return;
  const name = await promptDialog({
    title: "Save this search",
    label: "Name",
    value: truncate(last.query, 60),
    hint: "Saved searches appear on the home screen. They keep the mode and filters.",
    confirmLabel: "Save search",
    trigger,
  });
  if (!name) return;
  try {
    await api.saveQuery({
      name,
      query: last.query,
      mode: last.meta.mode,
      precision: last.meta.precision,
      filters: filtersPayload(),
    });
    toast("Search saved");
    emit("saved");
  } catch (error) {
    toastError(error, "The search could not be saved.");
  }
}

function exportRows(last) {
  return last.results.map((result) => ({
    rank: result.rank,
    doc_id: result.docId,
    title: result.title,
    source: result.source,
    folder: result.folder,
    modified_at: result.modifiedAt,
    tags: result.tags,
    confidence: result.confidence,
    score: result.score,
    snippet: snippetText(result),
  }));
}

export function exportMarkdown(last) {
  const lines = [
    `# Search results: “${last.query}”`,
    "",
    `${modeLabel(last.meta.mode)} search · ${plural(last.results.length, "result")} · exported ${new Date().toLocaleString()}`,
    "",
  ];
  last.results.forEach((result, index) => {
    const confidence = CONFIDENCE[result.confidence]?.label;
    lines.push(`${index + 1}. **${result.title.replace(/\*/g, "\\*")}**${confidence ? ` (${confidence})` : ""}`);
    const details = [result.source];
    if (result.modifiedAt) details.push(`modified ${formatDate(result.modifiedAt)}`);
    if (result.tags.length) details.push(result.tags.map((tag) => `#${tag}`).join(" "));
    lines.push(`   ${details.join(" · ")}`);
    const snippet = snippetText(result);
    if (snippet) lines.push(`   > ${snippet}`);
    lines.push("");
  });
  return lines.join("\n");
}

export function exportJson(last) {
  return JSON.stringify({
    query: last.query,
    mode: last.meta.mode,
    precision: last.meta.precision,
    filters: filtersPayload(),
    exported_at: new Date().toISOString(),
    results: exportRows(last),
  }, null, 2);
}

export function exportName(query, extension) {
  // Letters and digits of any script survive ("会議" -> onefind-会議.md).
  const slug = query.normalize("NFC").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "")
    .slice(0, 40) || "results";
  return `onefind-${slug}.${extension}`;
}

export function exportResults(format) {
  const last = state.lastResponse;
  if (!last?.results.length) return;
  if (format === "json") downloadText(exportName(last.query, "json"), exportJson(last), "application/json");
  else downloadText(exportName(last.query, "md"), exportMarkdown(last), "text/markdown");
  toast(`Exported ${plural(last.results.length, "result")}`);
}

export function initResults() {
  cardMenu = setupFloatingMenu(byId("card-menu"));
  on("document-removed", ({ docId }) => dropRemovedCards(docId));
  byId("weak-toggle").addEventListener("click", () => {
    setWeakOpen(byId("weak-toggle").getAttribute("aria-expanded") !== "true");
  });
  byId("save-search").addEventListener("click", (event) => saveCurrentSearch(event.currentTarget));
  for (const item of byId("export-menu").querySelectorAll("[data-export]")) {
    item.addEventListener("click", () => exportResults(item.dataset.export));
  }
}

export { syncBookmarkButtons };
