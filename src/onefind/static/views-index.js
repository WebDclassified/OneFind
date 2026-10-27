// Index wizard: choose a folder (browser backed by GET /api/fs/list),
// start a background job, follow it (GET /api/jobs/{id} every 400 ms),
// then confirm readiness with a summary. Also runs reindex jobs for Manage
// and "Rebuild vectors" jobs (kind "rebuild_vectors"), which share the
// progress stepper with their own step labels.

import { api } from "./api.js";
import { button, byId, el } from "./dom.js";
import { baseName, formatInteger, plural, truncate } from "./format.js";
import { icon } from "./icons.js";
import { enter, reducedMotion } from "./motion.js";
import { actions, emit, state, storage } from "./store.js";
import { closeDialog, confirmDialog, openDialog, withNetworkConfirm } from "./dialogs.js";
import { announce, toast } from "./toast.js";

const POLL_MS = 400;
const ALL_DONE_HOLD_MS = 350; // let every step show its check before the summary
const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
const LAST_FOLDER_KEY = "onefind.lastFolder";
const STEPS = [
  { name: "scan", label: "Scan files" },
  { name: "index", label: "Index text" },
  { name: "vectors", label: "Build vectors" },
  { name: "ready", label: "Ready" },
];
/** Step labels of a "rebuild vectors" job (same step names as indexing). */
const REBUILD_LABELS = { scan: "Find long notes", index: "Split long notes", vectors: "Build vectors", ready: "Ready" };
const PHASE_STEP = { scan: "scan", read: "index", write: "index", prune: "index", model: "vectors", embed: "vectors", done: "ready" };

const dialog = () => byId("index-dialog");

let listing = null;
let fsController = null;
let currentJob = null;
let pollTimer = 0;
let pollFailures = 0;
let newCollection = false;
let rootQueue = []; // further roots to update after the current job
let queueOptions = {};
let queueCollection = null; // the collection the queued roots belong to
let finishTimer = 0;
let excludeEdited = false; // an untouched exclude field keeps the folder's saved patterns
let pickedFile = null; // a single file chosen in the browser (or typed) instead of the folder
let relocateRootId = null; // "Locate…": the missing folder whose new place is being chosen
let savedRoots = []; // the collection's folders and their saved exclude patterns (GET /api/roots)
let excludePrefilled = null; // the folder whose saved patterns fill the exclude field, if any

/* ---------- helpers ---------- */

/** Breadcrumbs for Windows ("C:\\Users\\me") and POSIX ("/home/me") paths. */
export function crumbsFor(path) {
  const value = String(path || "");
  if (!value) return [];
  const sep = value.includes("\\") ? "\\" : "/";
  const parts = value.split(/[\\/]+/).filter(Boolean);
  const crumbs = [];
  let acc = "";
  if (value.startsWith("/")) {
    acc = "/";
    crumbs.push({ label: "/", path: "/" });
  }
  parts.forEach((part, index) => {
    if (index === 0 && /^[A-Za-z]:$/.test(part)) acc = `${part}${sep}`;
    else acc = acc.endsWith(sep) || !acc ? `${acc}${part}` : `${acc}${sep}${part}`;
    crumbs.push({ label: part, path: acc });
  });
  return crumbs;
}

export function parseExclude(text) {
  return [...new Set(String(text || "").split(/[\n,]+/).map((item) => item.trim()).filter(Boolean))].slice(0, 100);
}

/**
 * The POST /api/index body for the wizard. `exclude` is sent only when the
 * user edited the field: a missing value keeps the patterns stored for the
 * folder, while a list (even an empty one) replaces them. A single file
 * never sends patterns, which would replace its parent folder's.
 */
export function wizardIndexBody({ path, file = false, excludeText = "", excludeEdited: edited = false, embed = true, autoSync, collection }) {
  const body = { path, embed: embed ? null : false, prune: true, full: false };
  if (edited && !file) body.exclude = parseExclude(excludeText);
  // A folder picked on purpose is indexed whole, even when only one of its
  // files was indexed before (the server's default refreshes just that file).
  if (!file) body.scope = "folder";
  // "Keep this collection up to date automatically" (saved in the collection).
  if (typeof autoSync === "boolean") body.auto_sync = autoSync;
  // The collection this was meant for: the server refuses (409
  // collection_changed) when another one has become active meanwhile.
  if (typeof collection === "string" && collection) body.collection = collection;
  return body;
}

/** Folder paths compared loosely: trailing separators, slash style, and case on Windows. */
export function samePath(left, right) {
  const norm = (value) => String(value || "").trim().replace(/[\\/]+$/, "").replace(/\\/g, "/");
  const a = norm(left);
  const b = norm(right);
  if (!a || !b) return false;
  if (a === b) return true;
  const windows = /^[A-Za-z]:/.test(a) || /^[A-Za-z]:/.test(b);
  return windows && a.toLowerCase() === b.toLowerCase();
}

/**
 * The exclude patterns saved for `path` when it is one of the collection's
 * folders (GET /api/roots), else null. Only the folder itself counts: a
 * subfolder is indexed as part of its folder, with the folder's patterns.
 */
export function savedExcludesFor(roots, path) {
  const root = (Array.isArray(roots) ? roots : []).find((item) => item && samePath(item.path, path));
  if (!root) return null;
  return Array.isArray(root.exclude) ? root.exclude.filter((item) => typeof item === "string" && item) : [];
}

/**
 * Initial state of the wizard's auto-sync box: on for a new or still empty
 * collection, otherwise whatever the collection already uses.
 */
export function wizardAutoSyncDefault(stats, { newCollection: makeNew = false } = {}) {
  if (makeNew || !stats?.collection || !(Number(stats?.documents) > 0)) return true;
  return Boolean(stats?.auto_sync?.enabled);
}

/** A file the wizard can index on its own: supported text, or a .jsonl dataset (the server marks both). */
export function isPickableFile(entry) {
  return entry?.kind === "file" && Boolean(entry.supported);
}

/** The folder that holds `path` ("C:\\a\\b.md" -> "C:\\a", "/a/b.md" -> "/a"). */
export function parentPath(path) {
  const crumbs = crumbsFor(path);
  return crumbs.length > 1 ? crumbs[crumbs.length - 2].path : "";
}

/** An error from removing or relocating a folder; an older server has no such route. */
export function rootChangeMessage(error, fallback = "The folder could not be changed.") {
  const missingRoute = [404, 405].includes(error?.status) && error?.code === "request_failed";
  return missingRoute
    ? "This OneFind server cannot remove or move a collection's folders. Update OneFind, then try again."
    : error?.message || fallback;
}

/** File name without its extension, for a default collection name. */
export function stemName(path) {
  const name = baseName(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

/** Steps from the job, or derived from its phase for older servers. */
export function jobSteps(job) {
  if (Array.isArray(job?.steps) && job.steps.length) return job.steps;
  const active = PHASE_STEP[job?.phase] || "scan";
  const activeIndex = STEPS.findIndex((step) => step.name === active);
  return STEPS.map((step, index) => ({
    name: step.name,
    status: job?.status === "succeeded" ? "done"
      : index < activeIndex ? "done" : index === activeIndex ? "active" : "pending",
  }));
}

/** A drive root or the home folder: indexing it would read far more than notes. */
export function isBroadFolder(path, places = []) {
  const value = String(path || "").replace(/[\\/]+$/, "");
  if (!value || value === "/" || /^[A-Za-z]:$/.test(value)) return true;
  const home = (places || []).find((place) => place?.label === "Home")?.path;
  return Boolean(home) && value.toLowerCase() === String(home).replace(/[\\/]+$/, "").toLowerCase();
}

export function jobPercent(job) {
  const done = Number(job?.done);
  const total = Number(job?.total);
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

/** Step label for a job's step (indexing and rebuild jobs share step names). */
export function stepLabel(name, kind = "index") {
  if (kind === "rebuild_vectors" && REBUILD_LABELS[name]) return REBUILD_LABELS[name];
  return STEPS.find((item) => item.name === name)?.label || name;
}

export function jobCountText(job) {
  const done = Number(job?.done) || 0;
  const total = Number(job?.total);
  const hasTotal = Number.isFinite(total) && total > 0;
  if (job?.kind === "rebuild_vectors") {
    // Counts are notes, not files or vectors.
    switch (job?.phase) {
      case "scan": return hasTotal ? `${plural(total, "long note")} to update` : "Looking for long notes…";
      case "model": return "Loading the local model…";
      case "write":
      case "embed": return hasTotal ? `${formatInteger(done)} of ${plural(total, "note")}` : `${plural(done, "note")} updated`;
      case "done": return "Finishing…";
      default: return "Starting…";
    }
  }
  switch (job?.phase) {
    case "scan": return hasTotal ? `${formatInteger(done)} of ${plural(total, "file")} checked` : `Found ${plural(done, "file")}`;
    case "read":
    case "write": return hasTotal ? `${formatInteger(done)} of ${plural(total, "file")}` : `${plural(done, "file")} indexed`;
    case "model": return "Loading the local model…";
    case "embed": return hasTotal ? `${formatInteger(done)} of ${plural(total, "vector")}` : `${plural(done, "vector")} built`;
    case "prune": return "Tidying up removed files…";
    case "done": return "Finishing…";
    default: return "Starting…";
  }
}

function setStep(step) {
  for (const name of ["choose", "progress", "done", "failed"]) {
    byId(`index-step-${name}`).hidden = name !== step;
  }
  dialog().dataset.step = step;
  enter(byId(`index-step-${step}`), "view-enter");
}

function setFoot(...buttons) {
  byId("index-foot").replaceChildren(...buttons);
}

/** Keep keyboard focus inside the wizard when the content it was on is replaced. */
function focusIfLost(target) {
  const active = document.activeElement;
  if (!dialog().open || (active && active !== document.body && dialog().contains(active) && active.isConnected)) return;
  target?.focus({ preventScroll: true });
}

/* ---------- step 1: choose ---------- */

function renderPlaces(places) {
  const list = byId("fs-places");
  list.replaceChildren(...(places || []).map((place) => el("li", {},
    el("button", {
      type: "button",
      class: "place-button",
      title: place.path,
      attrs: { "aria-current": listing?.path === place.path ? "true" : false },
      on: { click: () => loadDir(place.path, { focus: "list" }) },
    }, icon("folder"), el("span", { text: place.label || baseName(place.path) })),
  )));
}

/**
 * The folder list is one Tab stop: arrows, Home/End and first letters move,
 * Enter opens, Backspace or Alt+ArrowUp goes up a folder.
 */
function onListKeydown(event) {
  const items = [...byId("fs-list").querySelectorAll(".browser-item")];
  const index = items.indexOf(document.activeElement);
  const move = (next) => {
    if (!items.length) return;
    event.preventDefault();
    const target = items[Math.max(0, Math.min(items.length - 1, next))];
    for (const item of items) item.tabIndex = item === target ? 0 : -1;
    target.focus();
  };
  if ((event.key === "Backspace" || (event.altKey && event.key === "ArrowUp")) && listing?.parent) {
    event.preventDefault();
    loadDir(listing.parent, { focus: "list" });
  } else if (event.key === "ArrowDown") move(index + 1);
  else if (event.key === "ArrowUp") move(index - 1);
  else if (event.key === "Home") move(0);
  else if (event.key === "End") move(items.length - 1);
  else if (event.key.length === 1 && /\S/.test(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) {
    const letter = event.key.toLowerCase();
    const order = [...items.slice(index + 1), ...items.slice(0, index + 1)];
    const hit = order.find((item) => item.textContent.trim().toLowerCase().startsWith(letter));
    if (hit) move(items.indexOf(hit));
  }
}

function renderListing(data, { focus = null } = {}) {
  listing = data;
  byId("fs-path").value = data.path || "";
  byId("fs-up").disabled = !data.parent;
  byId("fs-crumbs").replaceChildren(...crumbsFor(data.path).map((crumb, index, all) => el("li", {},
    index === all.length - 1
      ? el("span", { class: "crumb-current", text: crumb.label, attrs: { "aria-current": "location" } })
      : el("button", { type: "button", class: "crumb", text: crumb.label, on: { click: () => loadDir(crumb.path, { focus: "list" }) } }),
  )));
  // Long paths keep the current folder in view.
  const crumbs = byId("fs-crumbs").parentElement;
  crumbs.scrollLeft = crumbs.scrollWidth;

  const entries = Array.isArray(data.entries) ? data.entries : [];
  const dirs = entries.filter((entry) => entry.kind === "dir");
  const files = entries.filter(isPickableFile);
  const items = dirs.map((entry, index) => el("li", {},
    el("button", {
      type: "button",
      class: "browser-item",
      tabIndex: index === 0 ? 0 : -1,
      on: { click: () => loadDir(entry.path, { focus: "list" }) },
    }, icon("folder"), el("span", { text: entry.name }), icon("chevron-right", "icon browser-chevron")),
  ));
  for (const entry of files.slice(0, 25)) {
    items.push(el("li", {},
      el("button", {
        type: "button",
        class: "browser-item browser-file-button",
        tabIndex: items.length === 0 ? 0 : -1,
        title: `Index just ${entry.name}`,
        attrs: { "aria-pressed": String(pickedFile === entry.path) },
        dataset: { path: entry.path },
        on: { click: () => pickFile(entry.path) },
      }, icon("file"), el("span", { text: entry.name })),
    ));
  }
  if (files.length > 25) items.push(el("li", { class: "browser-more", text: `and ${plural(files.length - 25, "more file")}` }));
  if (!items.length) items.push(el("li", { class: "browser-more", text: "This folder is empty." }));
  byId("fs-list").replaceChildren(...items);

  byId("fs-count").textContent = folderCountText(data);
  renderPlaces(data.places);
  applySavedExcludes();
  updateStartButton();
  if (focus === "list") {
    // The button that was pressed is gone: continue from the new list.
    (byId("fs-list").querySelector(".browser-item") || byId("fs-list")).focus({ preventScroll: true });
  }
}

function setPathError(message) {
  const error = byId("fs-error");
  error.textContent = message || "";
  error.hidden = !message;
  if (message) byId("fs-path").setAttribute("aria-invalid", "true");
  else byId("fs-path").removeAttribute("aria-invalid");
}

/** Choose one file (or none) instead of the whole folder. */
function pickFile(path) {
  // Picking the selected file again goes back to the whole folder. A moved
  // folder ("Locate…") can only be pointed at a folder.
  pickedFile = path && path !== pickedFile && relocateRootId === null ? path : null;
  for (const node of byId("fs-list").querySelectorAll(".browser-file-button")) {
    node.setAttribute("aria-pressed", String(node.dataset.path === pickedFile));
  }
  updateStartButton();
}

async function loadDir(path, { focus = null, fallback = null, pick = null } = {}) {
  fsController?.abort();
  const controller = new AbortController();
  fsController = controller;
  // A newer loadDir (the person typed a path while the first listing loaded)
  // owns the browser now: this one must not follow up or render.
  const superseded = () => controller !== fsController;
  setPathError("");
  byId("fs-list").setAttribute("aria-busy", "true");
  try {
    const data = await api.listDir(path || undefined, controller.signal);
    if (superseded()) return;
    // First visit: open at Documents rather than the whole home folder.
    const documents = fallback && (data.places || []).find((place) => place?.label === fallback)?.path;
    if (documents && documents !== data.path) {
      await loadDir(documents, { focus });
      return;
    }
    // A typed file is matched to its entry in the listing (the server's spelling of the path).
    const wanted = pick ? baseName(pick).toLowerCase() : "";
    const entry = pick ? (Array.isArray(data.entries) ? data.entries : [])
      .find((item) => isPickableFile(item) && String(item.name).toLowerCase() === wanted) : null;
    pickedFile = relocateRootId === null && entry ? entry.path : null;
    renderListing(data, { focus });
    if (pickedFile) announce(`${entry.name} is selected. Indexing adds just this file.`);
    else if (pick && relocateRootId !== null) setPathError("That is a file. Choose the folder the notes moved to.");
    else if (pick) setPathError(`${baseName(pick)} is not a file OneFind can index. Choose a Markdown or text file, a .jsonl dataset, or a folder.`);
  } catch (problem) {
    if (problem?.name === "AbortError" || superseded()) return;
    const message = String(problem?.message || "");
    if (problem?.status === 404 && /is a file, not a folder/i.test(message) && !pick) {
      // A typed file path: show its folder with the file selected.
      const parent = parentPath(path);
      if (parent) {
        await loadDir(parent, { focus, pick: path });
        return;
      }
    }
    setPathError(problem?.status === 404
      ? message || "That folder does not exist. Check the path and try again."
      : message || "That folder could not be opened.");
  } finally {
    if (!superseded()) byId("fs-list").removeAttribute("aria-busy");
  }
}

function updateStartButton() {
  const start = byId("index-start");
  if (!start) return;
  const target = pickedFile || listing?.path || "";
  const name = target ? baseName(target) : "";
  start.disabled = !target;
  const broad = !pickedFile && isBroadFolder(listing?.path, listing?.places);
  start.querySelector("span").textContent = !name ? "Index this folder"
    : broad ? `Index all of “${truncate(name, 20)}”…` : `Index “${truncate(name, 28)}”`;
  start.title = target;
  byId("index-name").placeholder = (pickedFile ? stemName(pickedFile) : baseName(listing?.path)) || "Notes";
  const count = byId("fs-count");
  if (pickedFile) count.textContent = `Only “${baseName(pickedFile)}” will be indexed. Pick it again, or choose a folder, to index the whole folder.`;
  else if (listing) count.textContent = folderCountText(listing);
}

const EXCLUDE_HELP = "Comma or line separated patterns. Hidden folders and node_modules are always skipped. "
  + "Patterns you enter replace the ones saved for a folder you indexed before; leave the field alone to keep them.";

/**
 * Show the saved exclude patterns of an already indexed folder in the
 * Options field. They are only shown: an untouched field sends nothing, so
 * the server keeps them; editing the field replaces them.
 */
function applySavedExcludes() {
  const field = byId("index-exclude");
  const help = byId("exclude-help");
  const summary = byId("index-options-summary");
  if (excludeEdited) return; // the person's own text stays
  const path = relocateRootId === null ? listing?.path : null;
  const saved = path ? savedExcludesFor(savedRoots, path) : null;
  if (saved && saved.length) {
    field.value = saved.join(", ");
    excludePrefilled = path;
    help.textContent = `These are the patterns saved for “${truncate(baseName(path), 40)}”. Editing them replaces the saved patterns; leave them as they are to keep them. Hidden folders and node_modules are always skipped.`;
    if (summary) summary.textContent = `Options · ${plural(saved.length, "saved exclusion")}`;
    return;
  }
  if (excludePrefilled !== null) field.value = "";
  excludePrefilled = null;
  help.textContent = EXCLUDE_HELP;
  if (summary) summary.textContent = "Options";
}

async function loadSavedRoots() {
  try {
    const payload = await api.roots();
    savedRoots = Array.isArray(payload?.roots) ? payload.roots : [];
  } catch {
    savedRoots = []; // an older server: nothing to pre-fill
  }
  if (dialog().open && dialog().dataset.step === "choose") applySavedExcludes();
}

function folderCountText(data) {
  const supported = Number(data?.supported_files);
  if (!Number.isFinite(supported)) return "";
  return supported ? `${plural(supported, "supported file")} in this folder (Markdown, text, reStructuredText, Org).`
    : "No supported files directly in this folder. Subfolders are included when indexing.";
}

function showChoose() {
  setStep("choose");
  byId("index-eyebrow").textContent = newCollection ? "New collection" : "Step 1 of 2";
  byId("index-title").textContent = relocateRootId !== null ? "Choose the folder's new location" : "Choose a folder or file to index";
  byId("index-name-row").hidden = !newCollection;
  const hasVectors = Number(state.stats?.vectors) > 0;
  byId("index-embed-help").textContent = hasVectors
    ? "This collection already has vectors, so new and changed files get them too."
    : "Uses the local model when it is installed. Keyword search works either way.";
  const start = button("Index this folder", { variant: "primary", iconName: "arrow-right", onClick: startFromWizard });
  start.id = "index-start";
  setFoot(button("Cancel", { variant: "secondary", attrs: { "data-close": "" } }), start);
  updateStartButton();
}

function showNameError(message) {
  const error = byId("index-name-error");
  const input = byId("index-name");
  error.textContent = message || "";
  error.hidden = !message;
  if (message) {
    input.setAttribute("aria-invalid", "true");
    input.focus();
  } else {
    input.removeAttribute("aria-invalid");
  }
}

async function startFromWizard(event) {
  setPathError("");
  showNameError("");
  if (!listing?.path) return;
  const start = byId("index-start");
  const file = relocateRootId === null ? pickedFile : null; // a moved folder is relocated to a folder
  if (!file && isBroadFolder(listing.path, listing.places)) {
    // One click should never read a whole drive or home folder by accident.
    const ok = await confirmDialog({
      title: "Index this whole folder?",
      message: `“${listing.path}” holds far more than notes, so indexing it reads every supported file in every subfolder and can take a long time. Choosing a notes folder is usually better.`,
      confirmLabel: "Index everything",
      trigger: event?.currentTarget || start,
    });
    if (!ok) return;
  }
  start.disabled = true;
  try {
    if (newCollection) {
      const name = byId("index-name").value.trim() || (file ? stemName(file) : baseName(listing.path)) || "Notes";
      await api.createCollection(name);
      await api.activateCollection(name);
      newCollection = false;
      await actions.onCollectionChanged?.({ quiet: true });
    }
    if (relocateRootId !== null) {
      // Point the moved folder at its new place first: every note keeps its
      // id, and the update below only picks up what changed (adding the new
      // place as another folder would index every note a second time).
      try {
        await api.relocateRoot(relocateRootId, listing.path);
      } catch (problem) {
        throw Object.assign(new Error(rootChangeMessage(problem, "The folder could not be moved.")), { code: problem?.code });
      }
      relocateRootId = null;
    }
    const body = wizardIndexBody({
      path: file || listing.path,
      file: Boolean(file),
      excludeText: byId("index-exclude").value,
      excludeEdited,
      embed: byId("index-embed").checked,
      autoSync: byId("index-autosync").checked,
      collection: state.stats?.collection?.name || null,
    });
    const job = await withNetworkConfirm((confirm) => api.startIndex(body, confirm), { trigger: start });
    if (job === null) {
      start.disabled = false; // declined the network folder: stay on this step
      return;
    }
    storage.set(LAST_FOLDER_KEY, listing.path);
    resetJobQueue();
    trackJob(job);
  } catch (problem) {
    if (problem?.code === "name_taken") {
      showNameError("A collection with that name already exists. Choose another name.");
    } else {
      setPathError(problem?.message || "Indexing could not start.");
      byId("fs-path").focus();
    }
    start.disabled = false;
  }
}

/* ---------- steps 2-4: progress ---------- */

const STATUS_TEXT = { pending: "waiting", active: "in progress", done: "done", skipped: "skipped", failed: "failed" };

function stepItem(step, index, kind) {
  const label = stepLabel(step.name, kind);
  return el("li", { class: "stepper-step", dataset: { step: step.name, status: "pending" } },
    el("span", { class: "stepper-marker", attrs: { "aria-hidden": "true" } }, el("span", { text: String(index + 1) })),
    el("span", { class: "stepper-label", text: label }),
    el("span", { class: "stepper-status sr-only", text: STATUS_TEXT.pending }),
  );
}

/**
 * Update the stepper in place: polls arrive every 400 ms, and rebuilding it
 * would restart every pulse and replay every check mark. A step's check
 * "pops" once, when it first becomes done.
 */
function renderSteps(steps, kind = "index") {
  const list = byId("job-steps");
  const names = `${kind}:${steps.map((step) => step.name).join(",")}`;
  if (list.dataset.names !== names) {
    list.replaceChildren(...steps.map((step, index) => stepItem(step, index, kind)));
    list.dataset.names = names;
  }
  steps.forEach((step, index) => {
    const node = list.children[index];
    if (!node || node.dataset.status === step.status) return;
    const wasDone = node.dataset.status === "done";
    node.dataset.status = step.status;
    node.querySelector(".stepper-marker").replaceChildren(step.status === "done" ? icon("check")
      : step.status === "failed" ? icon("x") : el("span", { text: String(index + 1) }));
    const note = node.querySelector(".stepper-status");
    note.className = step.status === "skipped" ? "stepper-status stepper-note" : "stepper-status sr-only";
    note.textContent = step.status === "skipped" ? "Skipped" : STATUS_TEXT[step.status] || step.status;
    if (step.status === "done" && !wasDone) enter(node, "just-done");
  });
}

/** Steps as shown: while the model loads nothing is being scanned yet. */
function displaySteps(job) {
  const steps = jobSteps(job).map((step) => ({ ...step }));
  if (job?.phase === "model") {
    for (const step of steps) if (step.name === "scan" && step.status === "active") step.status = "pending";
  }
  return steps;
}

const isRebuild = (job) => job?.kind === "rebuild_vectors";

function renderProgress(job) {
  if (isRebuild(job)) {
    byId("index-eyebrow").textContent = "Maintenance";
    byId("index-title").textContent = "Rebuilding vectors";
  } else {
    byId("index-eyebrow").textContent = "Indexing";
    byId("index-title").textContent = job.root ? `Indexing “${truncate(baseName(job.root), 40)}”` : "Indexing";
  }
  renderSteps(displaySteps(job), isRebuild(job) ? "rebuild_vectors" : "index");
  const bar = byId("job-progress");
  let percent = jobPercent(job);
  let count = jobCountText(job);
  const running = !TERMINAL.has(job.status);
  let message = job.message || "";
  if (running && percent === 100 && ["read", "write", "embed", "prune", "done"].includes(job.phase)) {
    // A full bar with work still going looks stuck: say what is happening
    // instead (the last "Reading …" message is stale by now).
    percent = null;
    count = job.phase === "embed" || (job.embed && job.phase !== "prune") ? "Saving and building vectors…" : "Saving…";
    message = "";
  }
  bar.classList.toggle("is-indeterminate", percent === null);
  if (percent === null) bar.removeAttribute("aria-valuenow");
  else bar.setAttribute("aria-valuenow", String(percent));
  bar.style.setProperty("--progress", `${percent ?? 0}%`);
  byId("job-count").textContent = count;
  byId("job-message").textContent = message;
}

function showProgressStep(job) {
  const footKind = isRebuild(job) ? "rebuild" : "index";
  if (dialog().dataset.step !== "progress" || byId("index-foot").dataset.kind !== footKind) {
    if (dialog().dataset.step !== "progress") {
      setStep("progress");
      byId("job-steps").dataset.names = "";
    }
    setFoot(
      button("Close", { variant: "secondary", attrs: { "data-close": "" } }),
      button(isRebuild(job) ? "Cancel rebuild" : "Cancel indexing", { variant: "danger-ghost", iconName: "stop", onClick: cancelJob }),
    );
    byId("index-foot").dataset.kind = footKind;
    renderProgress(job);
    focusIfLost(byId("job-count"));
    return;
  }
  renderProgress(job);
}

async function cancelJob(event) {
  if (!currentJob) return;
  if (event?.currentTarget) event.currentTarget.disabled = true;
  rootQueue = [];
  try {
    const job = await api.cancelJob(currentJob.id);
    if (job) handleJobUpdate(job);
  } catch (problem) {
    toast(problem?.message || (isRebuild(currentJob) ? "The rebuild could not be cancelled." : "Indexing could not be cancelled."), { tone: "danger" });
  }
}

/** Only the counts that say something: a first index has nothing "unchanged". */
function summaryRows(result) {
  const rows = [
    ["Added", result.added],
    ["Updated", result.updated],
    ["Unchanged", result.unchanged],
    ["Removed", result.removed],
    ["Skipped", Array.isArray(result.skipped) ? result.skipped.length : result.skipped],
  ].filter(([, value]) => Number.isFinite(Number(value)));
  const shown = rows.filter(([, value]) => Number(value) > 0);
  return (shown.length ? shown : rows.slice(0, 1)).map(([label, value]) =>
    el("div", { class: "summary-item" }, el("dt", { text: label }), el("dd", { text: formatInteger(value) })));
}

/** Summary rows ([label, value]) of a finished "rebuild vectors" job. */
export function rebuildSummaryRows(result) {
  const rows = [
    ["Notes updated", result?.rebuilt],
    ["Passages", result?.passages],
    ["Vectors built", result?.vectors_embedded],
  ].filter(([, value]) => value !== null && value !== undefined && Number.isFinite(Number(value)));
  const shown = rows.filter(([, value]) => Number(value) > 0);
  return shown.length ? shown : rows.slice(0, 1);
}

function showRebuildDone(job) {
  const result = job.result || {};
  setStep("done");
  byId("index-foot").dataset.kind = "";
  byId("index-eyebrow").textContent = "Maintenance finished";
  byId("index-title").textContent = "Vectors rebuilt";
  const rebuilt = Number(result.rebuilt) || 0;
  const passages = Boolean(result.chunking) && typeof result.chunking === "object";
  byId("done-title").textContent = rebuilt ? `${plural(rebuilt, "note")} updated` : "Vectors are up to date";
  byId("done-copy").textContent = passages
    ? "Long notes are now searched passage by passage, so meaning search finds answers anywhere in them."
    : "Every note has one vector again.";
  byId("done-summary").replaceChildren(...rebuildSummaryRows(result).map(([label, value]) =>
    el("div", { class: "summary-item" }, el("dt", { text: label }), el("dd", { text: formatInteger(value) }))));
  byId("done-skipped").hidden = true;
  const startButton = button("Start searching", { variant: "primary", iconName: "search", onClick: startSearching });
  setFoot(button("Close", { variant: "secondary", attrs: { "data-close": "" } }), startButton);
  if (dialog().open) startButton.focus();
}

function showDone(job) {
  if (isRebuild(job)) {
    showRebuildDone(job);
    return;
  }
  const result = job.result || {};
  setStep("done");
  byId("index-foot").dataset.kind = "";
  byId("index-eyebrow").textContent = "Indexing finished";
  byId("index-title").textContent = "Ready to search";
  const total = Number(result.total_documents);
  // The job's own health report is current; state.stats is refreshed only
  // after this summary is shown, so it may still describe the old index.
  const vectors = Number(job.health?.vectors ?? state.stats?.vectors) > 0;
  byId("done-title").textContent = Number.isFinite(total) ? `${plural(total, "document")} indexed` : "Indexing finished";
  byId("done-copy").textContent = `Search them by keyword${vectors ? " and meaning" : ""}.`;
  byId("done-summary").replaceChildren(...summaryRows(result));
  const skipped = Array.isArray(result.skipped) ? result.skipped : [];
  byId("done-skipped").hidden = skipped.length === 0;
  byId("done-skipped-label").textContent = `Skipped files (${skipped.length})`;
  byId("done-skipped-list").replaceChildren(...skipped.slice(0, 200).map((item) =>
    el("li", {}, el("span", { class: "skipped-path", text: String(item.path || "") }), el("span", { class: "skipped-reason", text: String(item.reason || "") }))));
  const startButton = button("Start searching", { variant: "primary", iconName: "search", onClick: startSearching });
  setFoot(button("Close", { variant: "secondary", attrs: { "data-close": "" } }), startButton);
  if (dialog().open) startButton.focus();
}

function showRebuildFailed(job) {
  setStep("failed");
  byId("index-foot").dataset.kind = "";
  const cancelled = job.status === "cancelled";
  byId("index-eyebrow").textContent = cancelled ? "Cancelled" : "Stopped";
  byId("index-title").textContent = cancelled ? "Rebuilding vectors was cancelled" : "Vectors could not be rebuilt";
  byId("failed-title").textContent = cancelled ? "Stopped at your request" : "Something went wrong while rebuilding vectors";
  byId("failed-message").textContent = cancelled
    ? "Notes finished so far keep their new passages. Run it again any time to finish the rest."
    : job.error?.message || job.message || "The vectors could not be rebuilt.";
  byId("failed-safe").textContent = "Search keeps working: every note is either fully updated or unchanged.";
  byId("failed-safe").hidden = false;
  const retry = button(cancelled ? "Continue" : "Try again", {
    variant: "primary",
    iconName: "refresh",
    onClick: (event) => rebuildVectors({ chunking: "keep", trigger: event.currentTarget }),
  });
  setFoot(button("Close", { variant: "secondary", attrs: { "data-close": "" } }), retry);
  focusIfLost(retry);
}

function showFailed(job) {
  if (isRebuild(job)) {
    showRebuildFailed(job);
    return;
  }
  setStep("failed");
  byId("index-foot").dataset.kind = "";
  const cancelled = job.status === "cancelled";
  byId("index-eyebrow").textContent = cancelled ? "Cancelled" : "Stopped";
  byId("index-title").textContent = cancelled ? "Indexing was cancelled" : "Indexing could not finish";
  byId("failed-title").textContent = cancelled ? "Stopped at your request" : "Something went wrong while indexing";
  byId("failed-message").textContent = cancelled
    ? "Notes indexed before you cancelled stay searchable. Run it again any time to pick up where it left off."
    : job.error?.message || job.message || "The folder could not be indexed.";
  // Work is committed in batches, so "unchanged" would not be honest here.
  byId("failed-safe").textContent = "Notes that were already indexed stay searchable.";
  byId("failed-safe").hidden = cancelled;
  const primary = failedAction(job, cancelled);
  const retry = button("Try again", {
    variant: primary ? "secondary" : "primary",
    iconName: "refresh",
    onClick: () => {
      showChoose();
      if (job.root) loadDir(job.root, { focus: "list" });
    },
  });
  setFoot(button("Close", { variant: "secondary", attrs: { "data-close": "" } }), retry, ...(primary ? [primary] : []));
  focusIfLost(primary || retry);
}

/**
 * The next step the server suggests for a failed job (error.action), so
 * "Try again" is not the only way out of a failure that would repeat.
 */
export function failedActionKind(job, { cancelled = false, hasVectors = false } = {}) {
  if (cancelled) return null;
  const action = job?.error?.action;
  // Without vectors yet, indexing for keyword search avoids the model; once a
  // collection has vectors, new files always get them, so repair instead.
  if (action === "switch_mode") return hasVectors ? "repair" : "keyword";
  if (action === "repair") return "repair";
  if (action === "open_collection") return "open_collection";
  return null;
}

function failedAction(job, cancelled) {
  const hasVectors = Number(job.health?.vectors ?? state.stats?.vectors) > 0;
  const kind = failedActionKind(job, { cancelled, hasVectors });
  if (kind === "keyword") {
    return button("Index for keyword search", {
      variant: "primary",
      iconName: "search",
      title: "Index again without building vectors",
      onClick: async () => {
        showChoose();
        byId("index-embed").checked = false;
        if (job.root) await loadDir(job.root, { focus: "list" });
        byId("index-start")?.focus();
      },
    });
  }
  if (kind === "repair") {
    return button("Check and repair", {
      variant: "primary",
      iconName: "wrench",
      onClick: () => {
        closeDialog(dialog(), "", { restoreFocus: false });
        actions.openManage?.({ section: "manage-maintenance", trigger: byId("manage-button") });
      },
    });
  }
  if (kind === "open_collection") {
    return button("Open another collection", {
      variant: "primary",
      iconName: "database",
      onClick: () => {
        closeDialog(dialog(), "", { restoreFocus: false });
        actions.openCollectionPrompt?.(byId("collection-button"));
      },
    });
  }
  return null;
}

function startSearching() {
  // Focus goes to the search box, not back to whatever opened the wizard.
  closeDialog(dialog(), "", { restoreFocus: false });
  const input = byId("search-input");
  input.focus();
  actions.showSuggestions?.(actions.suggestQueries?.() || []);
}

/** The collection a queue was built for is still the active one. */
function queueStillValid() {
  return queueCollection === (state.stats?.collection?.name ?? null);
}

/** Forget the folders still waiting to be updated (e.g. after a collection switch). */
export function resetJobQueue() {
  rootQueue = [];
  queueOptions = {};
  queueCollection = null;
}

function onJobFinished(job) {
  const open = dialog().open;
  if (job.status === "succeeded" && rootQueue.length) {
    if (queueStillValid()) {
      startRootJob(rootQueue.shift(), queueOptions);
      return;
    }
    // Another collection is active now: its folders are not this queue's.
    resetJobQueue();
  }
  const rebuild = isRebuild(job);
  if (job.status === "succeeded") {
    const finish = () => {
      finishTimer = 0;
      showDone(job);
      announce(rebuild ? "Vectors rebuilt." : "Indexing finished. Your collection is ready.");
    };
    window.clearTimeout(finishTimer);
    if (open && dialog().dataset.step === "progress" && !reducedMotion()) {
      // Every step resolves to a check (and the bar fills) before the summary replaces them.
      renderSteps(jobSteps(job).map((step) => ({ ...step, status: step.status === "skipped" ? "skipped" : "done" })),
        rebuild ? "rebuild_vectors" : "index");
      byId("job-progress").classList.remove("is-indeterminate");
      byId("job-progress").style.setProperty("--progress", "100%");
      byId("job-count").textContent = "Done";
      finishTimer = window.setTimeout(finish, ALL_DONE_HOLD_MS);
    } else {
      finish();
    }
    if (!open) toast(rebuild ? "Vectors rebuilt" : "Index ready", { action: { label: "Search", onClick: () => byId("search-input").focus() } });
  } else {
    showFailed(job);
    const cancelled = job.status === "cancelled";
    const what = rebuild ? "Rebuilding vectors" : "Indexing";
    announce(cancelled ? `${what} cancelled.` : `${what} could not finish.`);
    if (!open) toast(cancelled ? `${what} cancelled` : `${what} could not finish`, { tone: cancelled ? "info" : "danger" });
  }
  actions.onIndexComplete?.(job);
}

function handleJobUpdate(job) {
  currentJob = job;
  emit("job", job);
  if (TERMINAL.has(job.status)) {
    window.clearTimeout(pollTimer);
    pollTimer = 0;
    onJobFinished(job);
  } else {
    showProgressStep(job);
  }
}

async function poll() {
  pollTimer = 0;
  if (!currentJob) return;
  try {
    const job = await api.job(currentJob.id);
    pollFailures = 0;
    handleJobUpdate(job);
  } catch (problem) {
    pollFailures += 1;
    if (problem?.status === 404 || pollFailures > 25) {
      handleJobUpdate({ ...currentJob, status: "failed", error: { message: problem?.status === 404 ? "The indexing job was lost. The server may have restarted." : "Lost contact with OneFind while indexing." } });
      return;
    }
  }
  if (currentJob && !TERMINAL.has(currentJob.status)) {
    pollTimer = window.setTimeout(poll, pollFailures ? Math.min(2000, POLL_MS * (pollFailures + 1)) : POLL_MS);
  }
}

/** Follow a job until it ends. Safe to call again with the same job. */
export function trackJob(job) {
  if (!job?.id) return;
  const sameJob = currentJob?.id === job.id && pollTimer;
  currentJob = job;
  pollFailures = 0;
  handleJobUpdate(job);
  if (!sameJob && !TERMINAL.has(job.status)) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, POLL_MS);
  }
}

export function jobRunning() {
  return Boolean(currentJob && !TERMINAL.has(currentJob.status));
}

/* ---------- reindex flows (used by Manage, errors, idle) ---------- */

async function startRootJob(root, options) {
  try {
    const job = await withNetworkConfirm((confirm) => api.startIndex({
      path: root,
      embed: options.embed ?? null,
      prune: true,
      full: Boolean(options.full),
      // A queued folder belongs to this collection only (409 collection_changed otherwise).
      ...(queueCollection ? { collection: queueCollection } : {}),
    }, confirm));
    if (job === null) {
      // Declined: skip this network folder and carry on with the rest.
      if (rootQueue.length && queueStillValid()) await startRootJob(rootQueue.shift(), options);
      else resetJobQueue();
      return;
    }
    trackJob(job);
  } catch (problem) {
    if (problem?.status === 404 && rootQueue.length && queueStillValid()) {
      // One folder went missing: say so and carry on with the others.
      toast(`Skipped “${truncate(baseName(root), 40)}”: ${problem.message || "the folder could not be found."}`, { tone: "warn" });
      await startRootJob(rootQueue.shift(), options);
      return;
    }
    resetJobQueue();
    // Never leave a placeholder (or a finished job's) progress step on screen.
    currentJob = null;
    const failed = {
      status: "failed",
      root,
      error: { message: problem?.message || "Indexing could not start.", action: problem?.action || null },
    };
    if (dialog().open) showFailed(failed);
    else toast(failed.error.message, { tone: "danger" });
    announce("Indexing could not start.");
    actions.onIndexComplete?.(failed);
  }
}

/**
 * Update every folder recorded for this collection, one job after another.
 * options: { full, embed }. Opens the wizard on the progress step.
 */
export async function reindex(options = {}, { trigger } = {}) {
  if (jobRunning()) {
    showJobProgress({ trigger });
    return;
  }
  let roots = [];
  let missing = [];
  try {
    const changes = await api.changes();
    ({ roots, missing } = splitRoots(changes));
  } catch {
    roots = [];
  }
  if (missing.length) {
    // A moved or unplugged folder must not block updating the others.
    const names = missing.map((root) => `“${truncate(baseName(root), 40)}”`).join(", ");
    toast(`${missing.length === 1 ? "Skipped a folder that" : `Skipped ${missing.length} folders that`} could not be found: ${names}. Remove or locate ${missing.length === 1 ? "it" : "them"} in Manage.`, {
      tone: "warn",
      action: byId("manage-dialog")?.open ? null : { label: "Manage", onClick: () => actions.openManage?.({ section: "manage-health" }) },
    });
  }
  if (!roots.length) {
    if (missing.length) return; // the toast already points to Manage
    toast("No folders are recorded for this collection yet. Choose one to index.", { tone: "info" });
    openIndexWizard({ trigger });
    return;
  }
  queueOptions = options;
  rootQueue = roots.slice(1);
  queueCollection = state.stats?.collection?.name ?? null;
  newCollection = false;
  openDialog(dialog(), { trigger });
  dialog().dataset.step = "";
  showProgressStep({ phase: "scan", status: "queued", root: roots[0], done: 0, total: null, message: "Starting…" });
  await startRootJob(roots[0], options);
}

/** Registered roots that still exist (to update) and the ones that went missing. */
export function splitRoots(changes) {
  const all = (Array.isArray(changes?.roots) ? changes.roots : []).filter((root) => typeof root?.root === "string" && root.root);
  return {
    roots: all.filter((root) => root.exists !== false).map((root) => root.root),
    missing: all.filter((root) => root.exists === false).map((root) => root.root),
  };
}

/**
 * Re-split long notes into passages and rebuild their vectors (a background
 * job followed in the same progress dialog as indexing). chunking: "keep"
 * (finish a pending rebuild), "default" (turn passage search on) or "off".
 */
export async function rebuildVectors({ chunking = "keep", trigger } = {}) {
  if (jobRunning()) {
    showJobProgress({ trigger });
    return;
  }
  resetJobQueue();
  newCollection = false;
  openDialog(dialog(), { trigger });
  dialog().dataset.step = "";
  showProgressStep({ kind: "rebuild_vectors", phase: "scan", status: "queued", root: null, done: 0, total: null, message: "Starting…" });
  try {
    const job = await api.rebuildVectors(chunking);
    trackJob(job);
  } catch (problem) {
    currentJob = null;
    const failed = {
      kind: "rebuild_vectors",
      status: "failed",
      root: null,
      error: { message: problem?.message || "The rebuild could not start.", action: problem?.action || null },
    };
    if (dialog().open) showFailed(failed);
    else toast(failed.error.message, { tone: "danger" });
    announce("Rebuilding vectors could not start.");
    actions.onIndexComplete?.(failed);
  }
}

export function showJobProgress({ trigger } = {}) {
  if (finishTimer) return;
  openDialog(dialog(), { trigger });
  if (currentJob) {
    if (TERMINAL.has(currentJob.status)) onJobFinished(currentJob);
    else showProgressStep(currentJob);
  } else {
    showChoose();
  }
}

export function openIndexWizard({ trigger, newCollection: makeNew = false, path, relocateRoot = null } = {}) {
  if (jobRunning()) {
    showJobProgress({ trigger });
    return;
  }
  newCollection = makeNew;
  relocateRootId = Number.isInteger(relocateRoot) ? relocateRoot : null;
  pickedFile = null;
  byId("index-name").value = "";
  byId("index-exclude").value = "";
  excludeEdited = false;
  excludePrefilled = null;
  savedRoots = [];
  applySavedExcludes();
  byId("index-embed").checked = true;
  byId("index-autosync").checked = wizardAutoSyncDefault(state.stats, { newCollection: makeNew });
  byId("fs-error").hidden = true;
  showNameError("");
  showChoose();
  openDialog(dialog(), { trigger });
  const start = path || listing?.path || storage.get(LAST_FOLDER_KEY) || "";
  loadDir(start, { fallback: start ? null : "Documents" });
  // A new collection has no folders yet; otherwise show an indexed folder's saved patterns.
  if (!makeNew && state.stats?.collection) loadSavedRoots();
  (newCollection ? byId("index-name") : byId("fs-path")).focus();
}

export function initIndexWizard() {
  byId("fs-up").addEventListener("click", () => {
    if (listing?.parent) loadDir(listing.parent, { focus: "list" });
  });
  byId("fs-list").addEventListener("keydown", onListKeydown);
  byId("index-exclude").addEventListener("input", () => {
    excludeEdited = true;
  });
  byId("fs-path-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = byId("fs-path").value.trim();
    if (value) loadDir(value);
  });
  Object.assign(actions, {
    openIndexWizard,
    showJobProgress,
    watchJob: trackJob,
    reindex: (options, extra) => reindex(options, extra),
    resetJobQueue,
    addVectors: () => reindex({ embed: true }),
    rebuildVectors: (options) => rebuildVectors(options),
    jobRunning,
  });
}
