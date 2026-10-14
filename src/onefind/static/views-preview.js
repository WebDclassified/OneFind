// Document preview: a side pane on wide screens, a slide-over drawer on
// medium and narrow ones. Full text with find-in-document highlighting.

import { api } from "./api.js";
import { button, byId, el, setButtonLabel } from "./dom.js";
import {
  fileName, folderLabel, formatBytes, formatDate, formatInteger, readablePath,
} from "./format.js";
import { enter, leave, withViewTransition } from "./motion.js";
import { actions, emit, on, state } from "./store.js";
import { announce, toast, toastError } from "./toast.js";
import { markPreviewed, openResult, removeFromIndex } from "./views-results.js";

const WIDE_QUERY = window.matchMedia("(min-width: 1100px)");
const MAX_MARKS = 2000;
const STOPWORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "i", "in", "is", "it", "of", "on",
  "or", "that", "the", "to", "was", "what", "when", "where", "which", "who", "why", "with",
]);

let controller = null;
let currentDoc = null;
let returnTarget = null;
let marks = [];
let markIndex = -1;
let queryTerms = [];
let findTimer = 0;
let cancelLeave = null; // an unfinished drawer exit animation

const pane = () => byId("preview");

export function isPreviewOpen() {
  return !pane().hidden && !pane().classList.contains("is-leaving");
}

/** The preview sits beside the results (wide windows), not over them. */
export function isSidePreview() {
  return isPreviewOpen() && WIDE_QUERY.matches;
}

/** Words from the query worth highlighting in the document. */
export function termsFromQuery(query) {
  const words = String(query || "").toLowerCase().match(/[\p{L}\p{N}][\p{L}\p{N}'_-]*/gu) || [];
  return [...new Set(words.filter((word) => word.length > 1 && !STOPWORDS.has(word)))].slice(0, 12);
}

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Split text into [{text, highlight}] using a phrase or a list of words. */
export function findSegments(text, { phrase = "", words = [] } = {}) {
  let pattern = null;
  if (phrase) pattern = new RegExp(escapeRegExp(phrase), "giu");
  else if (words.length) pattern = new RegExp(`(?<![\\p{L}\\p{N}])(?:${words.map(escapeRegExp).join("|")})`, "giu");
  if (!pattern) return [{ text, highlight: false }];
  const segments = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (!match[0]) continue;
    if (match.index > last) segments.push({ text: text.slice(last, match.index), highlight: false });
    segments.push({ text: match[0], highlight: true });
    last = match.index + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), highlight: false });
  return segments;
}

function paragraphs(body) {
  return String(body || "").replace(/\r\n?/g, "\n").split(/\n{2,}/).map((part) => part.replace(/^\n+|\n+$/g, ""))
    .filter((part) => part.trim());
}

/** Whitespace-collapsed, lower-case text for loose comparisons. */
const looseText = (text) => String(text || "").replace(/\s+/g, " ").trim().toLowerCase();

/**
 * The paragraph that holds the result's snippet. `snippet` is the result's
 * [{text, highlight}] segments (or plain text). A passage can span several
 * paragraphs, so each highlighted word is looked up with a little of the
 * text around it, the widest context first.
 */
export function snippetParagraphIndex(paragraphTexts, snippet) {
  const loose = paragraphTexts.map(looseText);
  const find = (needle) => (needle.length >= 4 ? loose.findIndex((text) => text.includes(needle)) : -1);
  if (Array.isArray(snippet)) {
    const segments = snippet.filter((segment) => typeof segment?.text === "string");
    for (let i = 0; i < segments.length; i += 1) {
      if (!segments[i].highlight) continue;
      const before = (segments[i - 1]?.text || "").split("…").pop().slice(-25);
      const after = (segments[i + 1]?.text || "").split("…")[0].slice(0, 25);
      const word = segments[i].text;
      for (const needle of [before + word + after, word + after, before + word]) {
        const index = find(looseText(needle));
        if (index >= 0) return index;
      }
    }
    snippet = segments.map((segment) => segment.text).join("");
  }
  const pieces = String(snippet || "").split("…").map(looseText).filter((piece) => piece.length >= 8);
  const needle = pieces.sort((a, b) => b.length - a.length)[0]?.slice(0, 60);
  return needle ? find(needle) : -1;
}

/** Scroll the preview so `node` sits in the middle, without moving focus or the page. */
function centerInPreview(node) {
  for (const box of [byId("preview-body"), pane()]) {
    if (!box || box.scrollHeight <= box.clientHeight + 1) continue;
    const target = node.getBoundingClientRect();
    const frame = box.getBoundingClientRect();
    box.scrollTop += target.top - frame.top - (box.clientHeight - target.height) / 2;
    return;
  }
}

/**
 * Render the document text with highlights. `jumpToMatch` (a document just
 * opened from a result) lands on the passage the result was found in: the
 * first highlight in the snippet's paragraph, else the first highlight.
 */
function renderBody({ jumpToMatch = false } = {}) {
  const container = byId("preview-body");
  const phrase = byId("preview-find").value.trim();
  const options = phrase ? { phrase } : { words: queryTerms };
  let count = 0;
  const texts = paragraphs(currentDoc?.body);
  const blocks = texts.map((text) => {
    const node = el("p", { class: "preview-paragraph" });
    for (const segment of findSegments(text, options)) {
      if (segment.highlight && count < MAX_MARKS) {
        node.append(el("mark", { text: segment.text }));
        count += 1;
      } else {
        node.append(document.createTextNode(segment.text));
      }
    }
    return node;
  });
  if (!blocks.length) blocks.push(el("p", { class: "preview-empty", text: "This document has no text." }));
  container.replaceChildren(...blocks);
  marks = [...container.querySelectorAll("mark")];
  markIndex = -1;
  container.scrollTop = 0;
  if (jumpToMatch && currentDoc) {
    const result = (state.lastResponse?.results || []).find((item) => item.docId === currentDoc.doc_id);
    const paragraph = result ? blocks[snippetParagraphIndex(texts, result.snippet)] : null;
    const target = paragraph?.querySelector("mark") || marks[0] || paragraph || null;
    if (target) {
      if (target.tagName === "MARK") {
        markIndex = marks.indexOf(target);
        target.classList.add("is-current");
      }
      // After layout: the pane may have only just been shown.
      window.requestAnimationFrame(() => {
        if (target.isConnected) centerInPreview(target);
      });
    }
  }
  updateFindCount(phrase);
}

function updateFindCount(phrase) {
  const label = byId("preview-find-count");
  const total = marks.length;
  if (!phrase && !queryTerms.length) label.textContent = "";
  else if (!total) label.textContent = phrase ? "No matches" : "";
  else if (markIndex >= 0) label.textContent = `${markIndex + 1} of ${formatInteger(total)}`;
  else label.textContent = phrase ? `${formatInteger(total)} found` : `${formatInteger(total)} search ${total === 1 ? "match" : "matches"}`;
  byId("preview-find-prev").disabled = total === 0;
  byId("preview-find-next").disabled = total === 0;
}

function stepMark(step) {
  if (!marks.length) return;
  marks[markIndex]?.classList.remove("is-current");
  markIndex = (markIndex + step + marks.length) % marks.length;
  const mark = marks[markIndex];
  mark.classList.add("is-current");
  mark.scrollIntoView({ block: "center", behavior: "auto" });
  updateFindCount(byId("preview-find").value.trim());
}

function metaRow(label, value) {
  if (!value) return null;
  return el("div", { class: "meta-row" }, el("dt", { text: label }), el("dd", { text: value }));
}

function renderMeta(doc) {
  const tags = Array.isArray(doc.tags) ? doc.tags.map((tag) => `#${tag}`).join(" ") : "";
  const duplicates = Array.isArray(doc.duplicates) && doc.duplicates.length
    ? doc.duplicates.slice(0, 3).map((id) => fileName(id) || id).join(", ") + (doc.duplicates.length > 3 ? "…" : "")
    : "";
  byId("preview-meta").replaceChildren(...[
    metaRow("Folder", typeof doc.folder === "string" ? folderLabel(doc.folder) : ""),
    metaRow("Modified", formatDate(doc.modified_at || doc.mtime)),
    metaRow("Type", doc.ext ? doc.ext.replace(/^\./, "").toUpperCase() : ""),
    metaRow("Size", Number.isFinite(Number(doc.size_bytes)) && doc.size_bytes !== null ? formatBytes(doc.size_bytes) : ""),
    metaRow("Tags", tags),
    metaRow("Same text in", duplicates),
  ].filter(Boolean));
}

function renderActions(doc) {
  const asResult = { docId: doc.doc_id, title: doc.title || doc.doc_id };
  const bookmark = button(doc.bookmarked ? "Bookmarked" : "Bookmark", {
    iconName: "bookmark",
    className: "bookmark-button",
    attrs: { "aria-pressed": String(Boolean(doc.bookmarked)) },
    onClick: async () => {
      const next = !doc.bookmarked;
      try {
        if (next) await api.addBookmark(doc.doc_id);
        else await api.removeBookmark(doc.doc_id);
        doc.bookmarked = next;
        emit("bookmark", { docId: doc.doc_id, bookmarked: next });
        toast(next ? "Bookmarked" : "Bookmark removed");
      } catch (error) {
        toastError(error, "The bookmark could not be saved.");
      }
    },
  });
  bookmark.dataset.docId = doc.doc_id;
  const items = [
    button("Open", { variant: "secondary", iconName: "open", onClick: () => openResult(asResult) }),
    button("Show in folder", { iconName: "folder", onClick: () => openResult(asResult, { reveal: true }) }),
    bookmark,
  ];
  if (typeof doc.folder === "string") {
    items.push(button("Filter to this folder", { iconName: "filter", onClick: () => actions.filterToFolder?.(doc.folder) }));
  }
  items.push(button("Remove from index…", {
    iconName: "trash",
    className: "preview-remove",
    title: "Stop showing this note in search. The file is not deleted.",
    onClick: (event) => removeFromIndex(asResult, { trigger: event.currentTarget }),
  }));
  byId("preview-actions").replaceChildren(...items);
}

function renderDocument(doc) {
  currentDoc = doc;
  byId("preview-title").textContent = doc.title || fileName(doc.source) || doc.doc_id;
  byId("preview-path").textContent = readablePath(doc.source || doc.doc_id);
  byId("preview-path").title = doc.source || doc.doc_id;
  renderMeta(doc);
  renderActions(doc);
  const notice = byId("preview-notice");
  notice.hidden = !doc.truncated;
  notice.textContent = doc.truncated ? "This is a long document, so only the beginning is shown. Open the file to read all of it." : "";
  renderBody({ jumpToMatch: true });
}

function renderLoading() {
  byId("preview-title").textContent = "Loading…";
  byId("preview-path").textContent = "";
  byId("preview-meta").replaceChildren();
  byId("preview-actions").replaceChildren();
  byId("preview-notice").hidden = true;
  byId("preview-find-count").textContent = "";
  byId("preview-body").replaceChildren(
    ...[0, 1, 2, 3].map(() => el("span", { class: "skeleton-line", attrs: { "aria-hidden": "true" } })),
  );
}

function renderMissing(error) {
  currentDoc = null;
  byId("preview-title").textContent = error?.status === 404 ? "Document not found" : "Preview unavailable";
  byId("preview-body").replaceChildren(el("p", {
    class: "preview-empty",
    text: error?.status === 404
      ? "This document is no longer in the index. Updating the index will tidy this up."
      : error?.message || "The document could not be loaded.",
  }));
}

function setDrawerMode(open) {
  const drawer = open && !WIDE_QUERY.matches;
  byId("preview-scrim").hidden = !drawer;
  // As a drawer the preview behaves like a modal dialog: everything behind
  // it is inert (header and footer too) and the page does not scroll.
  for (const node of [".app-header", ".search-area", "#results-pane", ".app-footer"]) {
    document.querySelector(node).inert = drawer;
  }
  document.body.classList.toggle("has-drawer", drawer);
  const aside = pane();
  if (drawer) {
    aside.setAttribute("role", "dialog");
    aside.setAttribute("aria-modal", "true");
  } else {
    aside.removeAttribute("role");
    aside.removeAttribute("aria-modal");
  }
}

/**
 * Show a document. `focus: false` keeps keyboard focus where it is (arrowing
 * through results with the side preview open updates it in place).
 */
export async function openPreview(docId, { trigger, focus = true } = {}) {
  if (!docId) return;
  const wasOpen = isPreviewOpen();
  cancelLeave?.();
  cancelLeave = null;
  returnTarget = trigger || (wasOpen ? returnTarget : document.activeElement);
  state.previewDocId = docId;
  markPreviewed(docId);
  queryTerms = termsFromQuery(state.lastResponse?.query || byId("search-input").value);
  byId("preview-find").value = "";
  renderLoading();
  const show = () => {
    pane().hidden = false;
    document.body.classList.add("has-preview");
    setDrawerMode(true);
    if (focus) pane().focus({ preventScroll: true });
  };
  if (wasOpen) show();
  else if (WIDE_QUERY.matches) {
    // The results column narrows to make room: morph instead of jumping.
    withViewTransition(show);
  } else {
    show();
    enter(pane(), "preview-enter");
  }

  controller?.abort();
  controller = new AbortController();
  try {
    const doc = await api.document(docId, controller.signal);
    if (state.previewDocId !== docId) return;
    renderDocument(doc || {});
    announce(`Previewing ${byId("preview-title").textContent}`);
  } catch (error) {
    if (error?.name === "AbortError") return;
    renderMissing(error);
  }
}

export function closePreview({ restoreFocus = true } = {}) {
  if (!isPreviewOpen()) return false;
  controller?.abort();
  state.previewDocId = null;
  currentDoc = null;
  markPreviewed(null);
  const target = returnTarget;
  returnTarget = null;
  const hide = () => {
    pane().hidden = true;
    document.body.classList.remove("has-preview");
    setDrawerMode(false);
  };
  if (WIDE_QUERY.matches) {
    withViewTransition(hide);
  } else {
    // The drawer slides away; the page behind becomes usable right away.
    setDrawerMode(false);
    byId("preview-scrim").hidden = false;
    const scrim = byId("preview-scrim");
    scrim.classList.add("is-leaving");
    const stop = leave(pane(), "is-leaving", () => {
      cancelLeave = null;
      scrim.classList.remove("is-leaving");
      hide();
    });
    if (!pane().hidden) {
      cancelLeave = () => {
        stop();
        scrim.classList.remove("is-leaving");
      };
    }
  }
  if (restoreFocus && target instanceof HTMLElement && target.isConnected) target.focus({ preventScroll: true });
  return true;
}

export function initPreview() {
  byId("preview-close").addEventListener("click", () => closePreview());
  byId("preview-scrim").addEventListener("click", () => closePreview());
  byId("preview-find").addEventListener("input", () => {
    window.clearTimeout(findTimer);
    findTimer = window.setTimeout(renderBody, 120);
  });
  byId("preview-find").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      window.clearTimeout(findTimer);
      if (!marks.length) renderBody();
      stepMark(event.shiftKey ? -1 : 1);
    }
  });
  byId("preview-find-next").addEventListener("click", () => stepMark(1));
  byId("preview-find-prev").addEventListener("click", () => stepMark(-1));
  WIDE_QUERY.addEventListener?.("change", () => {
    if (isPreviewOpen()) setDrawerMode(true);
  });
  // A Tab press can never leave the drawer for the inert page behind it.
  pane().addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || WIDE_QUERY.matches) return;
    const focusable = [...pane().querySelectorAll("button:not([disabled]), input, [href], [tabindex='0']")]
      .filter((node) => node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === pane())) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  on("document-removed", ({ docId }) => {
    if (state.previewDocId !== docId) return;
    const target = returnTarget;
    // The results view (if any) has already moved focus to a neighbour card.
    const hadFocus = pane().contains(document.activeElement);
    closePreview({ restoreFocus: false });
    if (!hadFocus) return;
    const card = target instanceof HTMLElement && target.isConnected ? target.closest(".result-card") : null;
    (card && card.dataset.docId !== docId ? card : byId("search-input")).focus({ preventScroll: true });
  });
  on("bookmark", ({ docId, bookmarked }) => {
    if (currentDoc?.doc_id === docId) currentDoc.bookmarked = bookmarked;
    for (const node of byId("preview-actions").querySelectorAll(".bookmark-button")) {
      node.setAttribute("aria-pressed", String(bookmarked));
      setButtonLabel(node, bookmarked ? "Bookmarked" : "Bookmark");
    }
  });
  Object.assign(actions, { openPreview, closePreview, isSidePreview });
}
