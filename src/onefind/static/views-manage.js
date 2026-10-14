// Manage panel (side sheet): health, maintenance, backups and restore,
// collection info and portability, collections, clearing, preferences.

import { api, itemsOf } from "./api.js";
import { button, byId, copyText, el, setButtonLabel } from "./dom.js";
import {
  baseName, formatBytes, formatDateTime, formatInteger, plural, relativeTime, truncate,
} from "./format.js";
import { icon } from "./icons.js";
import { actions, on, state } from "./store.js";
import { confirmDialog, openDialog, promptDialog } from "./dialogs.js";
import { setSingleKeysEnabled, singleKeysEnabled } from "./keyboard.js";
import { setReducedMotion, setTheme, themePref, userReducedMotion } from "./theme.js";
import { announce, toast, toastError } from "./toast.js";
import { changeCounts, changesSentence } from "./views-idle.js";
import { parentPath, rootChangeMessage } from "./views-index.js";

const dialog = () => byId("manage-dialog");

let data = { collections: null, backups: [], changes: null, changesState: "idle" };
let statsSignature = "";

/* ---------- small builders ---------- */

function row(label, value, extra = null) {
  return el("div", { class: "info-row" }, el("dt", { text: label }), el("dd", {}, value, extra));
}

function block(...children) {
  return el("div", { class: "sheet-block" }, ...children);
}

function copyLine(text) {
  return el("p", { class: "section-copy", text });
}

/** The result box for an action; re-renders keep the last result on screen. */
function resultBox(id) {
  return byId(id) || el("div", { class: "result-box", id, attrs: { "aria-live": "polite" }, hidden: true });
}

function showResult(id, tone, title, lines = [], extra = []) {
  const box = byId(id);
  if (!box) return;
  box.dataset.tone = tone;
  box.replaceChildren(
    icon(tone === "ok" ? "check-circle" : tone === "warn" ? "alert" : "info", "icon result-icon"),
    el("div", {},
      el("p", { class: "result-box-title", text: title }),
      lines.length ? el("ul", { class: "result-lines" }, lines.map((line) => el("li", { text: line }))) : null,
      ...extra),
  );
  box.hidden = false;
}

/**
 * Mark a button busy while an async action runs and show a working label.
 * It uses aria-disabled rather than `disabled`, so keyboard focus stays on
 * the button instead of falling to the page.
 */
async function busy(node, label, task) {
  if (node.getAttribute("aria-disabled") === "true") return undefined;
  const original = node.querySelector("span:last-of-type")?.textContent;
  node.setAttribute("aria-disabled", "true");
  node.setAttribute("aria-busy", "true");
  setButtonLabel(node, label);
  try {
    return await task();
  } finally {
    if (node.isConnected) {
      node.removeAttribute("aria-disabled");
      node.removeAttribute("aria-busy");
      if (original) setButtonLabel(node, original);
    }
  }
}

/** A button that can be found again after its section re-renders (see keepingFocus). */
function keyed(node, key) {
  node.dataset.focusKey = key;
  return node;
}

function activeName() {
  return state.stats?.collection?.name || data.collections?.active || null;
}

function activeEntry() {
  return itemsOf(data.collections, "collections").find((item) => item.active) || null;
}

/**
 * Re-render a section without dropping keyboard focus: the focused button
 * is found again by its data-focus-key (or its label) in the new content;
 * failing that, focus goes to the section itself.
 */
function keepingFocus(containerId, render) {
  const container = byId(containerId);
  const active = document.activeElement;
  const inside = Boolean(container?.contains(active));
  const key = inside ? active.dataset?.focusKey || null : null;
  const label = inside && active.tagName === "BUTTON" ? active.textContent.trim() : null;
  render();
  if (!inside || container.contains(document.activeElement)) return;
  const buttons = [...container.querySelectorAll("button")];
  const again = (key && buttons.find((node) => node.dataset.focusKey === key))
    || (label && buttons.find((node) => node.textContent.trim() === label));
  if (again) again.focus({ preventScroll: true });
  else (container.closest(".sheet-section") || container).focus({ preventScroll: true });
}

/* ---------- health ---------- */

/** A registered folder that no longer exists: locate its new place, or remove it. */
function missingRootRow(root) {
  const label = baseName(root.root) || root.root;
  const id = Number(root.root_id);
  return el("li", { class: "missing-root" },
    el("span", { class: "mono-path", text: root.root, title: root.root }),
    el("span", { class: "missing-root-actions" },
      keyed(button("Locate…", {
        variant: "secondary",
        iconName: "folder-open",
        title: `Choose where “${label}” is now. Its notes are kept, not indexed twice.`,
        onClick: (event) => actions.openIndexWizard?.({
          trigger: event.currentTarget,
          path: parentPath(root.root),
          relocateRoot: Number.isInteger(id) ? id : null,
        }),
      }), `locate:${root.root_id}`),
      keyed(button("Remove from collection", {
        variant: "ghost",
        iconName: "trash",
        onClick: (event) => removeRootFlow(root, event.currentTarget),
      }), `remove-root:${root.root_id}`),
    ),
  );
}

async function removeRootFlow(root, trigger) {
  const ok = await confirmDialog({
    title: "Remove this folder from the collection?",
    message: `OneFind stops looking for “${root.root}” and its notes leave search. Files on disk are not touched. If the folder only moved, use “Locate…” instead to keep its notes.`,
    confirmLabel: "Remove folder",
    danger: true,
    trigger,
  });
  if (!ok) return;
  try {
    await api.removeRoot(Number(root.root_id));
    toast("Folder removed from the collection");
  } catch (error) {
    toast(rootChangeMessage(error, "The folder could not be removed from the collection."), { tone: "danger" });
    return;
  }
  await loadChanges();
  actions.onIndexComplete?.({ status: "succeeded" });
}

function changesBlock() {
  if (data.changesState === "loading") return copyLine("Checking your folders for changes…");
  if (data.changesState === "error" || !data.changes) {
    return copyLine("Changes could not be checked. The folders may be unavailable.");
  }
  const roots = Array.isArray(data.changes.roots) ? data.changes.roots : [];
  if (!roots.length) return copyLine("No folders are recorded for this collection yet.");
  const sentence = data.changes.stale ? changesSentence(data.changes) : "";
  const missing = roots.filter((root) => root.exists === false);
  const wrap = el("div", { class: "changes-block" },
    el("p", { class: "section-copy" },
      icon(sentence ? "refresh" : "check-circle", "icon inline-icon"),
      sentence || "Everything is up to date with your folders."),
  );
  if (missing.length) {
    wrap.append(el("p", { class: "section-copy warn-text", text: `${plural(missing.length, "folder")} could not be found: ${missing.map((root) => root.root).join(", ")}` }));
    wrap.append(el("p", { class: "section-copy", text: "“Update index” skips missing folders, and their notes stay searchable. If a folder moved, locate its new place; if it is gone for good, remove it." }));
    wrap.append(el("ul", { class: "missing-roots" }, missing.map(missingRootRow)));
  }
  if (sentence) {
    const counts = changeCounts(data.changes);
    wrap.append(button("Update index", {
      variant: "primary", iconName: "refresh",
      onClick: (event) => actions.reindex?.({}, { trigger: event.currentTarget }),
      title: `${counts.new} new, ${counts.changed} changed, ${counts.deleted} deleted`,
    }));
  }
  return wrap;
}

function renderHealth() {
  const stats = state.stats || {};
  const documents = Number(stats.documents) || 0;
  const vectors = Number(stats.vectors) || 0;
  const missing = Number(stats.vectors_missing) || 0;
  const vectorText = vectors
    ? missing ? `${formatInteger(vectors)} (${formatInteger(missing)} documents missing vectors)` : `${formatInteger(vectors)}, meaning search is on`
    : "None. Keyword search only.";
  const ok = stats.health?.ok !== false;
  const lastIndexed = stats.last_indexed_at
    ? `${formatDateTime(stats.last_indexed_at)} (${relativeTime(stats.last_indexed_at)})`
    : "Not recorded";
  byId("manage-health-content").replaceChildren(
    el("p", { class: "health-sentence", dataset: { tone: ok ? "ok" : "warn" } },
      icon(ok ? "check-circle" : "alert", "icon inline-icon"),
      stats.status_detail || (ok ? "This collection is healthy." : "This collection needs attention.")),
    el("dl", { class: "info-list" },
      row("Documents", formatInteger(documents)),
      row("Vectors", vectorText),
      row("Consistency", ok ? "Index and documents agree" : "Something is out of sync. Run an integrity check."),
      row("Last indexed", lastIndexed),
      row("Size", formatBytes(stats.db_bytes), stats.size_note ? el("span", { class: "info-note", text: stats.size_note }) : null),
    ),
    changesBlock(),
  );
}

/* ---------- maintenance ---------- */

async function runCheck(event) {
  await busy(event.currentTarget, "Checking…", async () => {
    try {
      const report = await api.check();
      const problems = Array.isArray(report?.problems) ? report.problems : [];
      const suggestions = Array.isArray(report?.suggestions) ? report.suggestions : [];
      if (report?.ok) {
        showResult("maint-result", "ok", "No problems found. The index is consistent.", suggestions);
      } else {
        showResult("maint-result", "warn", "Some problems were found.", [...problems, ...suggestions], [
          keyed(button("Repair", { variant: "primary", iconName: "wrench", onClick: runRepair }), "repair"),
        ]);
      }
    } catch (error) {
      showResult("maint-result", "warn", error?.message || "The integrity check could not run.");
    }
  });
}

/** Plain sentences for Index.repair()'s counters. */
export function repairLines(report) {
  const count = (key) => (Number.isFinite(Number(report[key])) ? Number(report[key]) : 0);
  const lines = [];
  if (count("fts_rebuilt")) lines.push(`Rebuilt the keyword index for ${plural(count("fts_rebuilt"), "note")}.`);
  if (count("chunks_fixed")) lines.push(`Fixed ${plural(count("chunks_fixed"), "note record")}.`);
  if (count("orphan_rows_removed")) lines.push(`Removed ${plural(count("orphan_rows_removed"), "leftover index row")}.`);
  if (count("orphan_vectors_removed")) lines.push(`Removed ${plural(count("orphan_vectors_removed"), "vector")} of deleted notes.`);
  if (count("vectors_embedded")) lines.push(`Built vectors for ${plural(count("vectors_embedded"), "note")}.`);
  if (count("vectors_missing")) {
    lines.push(`${plural(count("vectors_missing"), "note")} still ${count("vectors_missing") === 1 ? "has" : "have"} no vectors. Use “Reindex with vectors”.`);
  }
  if (!lines.length) lines.push("Nothing needed fixing.");
  return lines;
}

async function runRepair(event) {
  await busy(event.currentTarget, "Repairing…", async () => {
    try {
      const report = await api.repair();
      const lines = repairLines(report || {});
      const fine = report?.ok !== false;
      showResult("maint-result", fine ? "ok" : "warn",
        fine ? "Repair finished." : "Repair finished, but some problems remain. Restore a backup if search still misbehaves.", lines);
      toast("Repair finished");
      actions.refreshStats?.();
    } catch (error) {
      showResult("maint-result", "warn", error?.message || "Repair could not finish.");
    }
  });
}

async function runCompact(event) {
  await busy(event.currentTarget, "Compacting…", async () => {
    try {
      const report = await api.compact();
      const before = Number(report?.before);
      const after = Number(report?.after);
      const saved = Number.isFinite(before) && Number.isFinite(after) ? Math.max(0, before - after) : null;
      showResult("compact-result", "ok", saved !== null
        ? `Compacted: ${formatBytes(before)} → ${formatBytes(after)} (${formatBytes(saved)} freed).`
        : "Compacted.");
      actions.refreshStats?.();
    } catch (error) {
      showResult("compact-result", "warn", error?.message || "Compacting could not finish.");
    }
  });
}

async function fullRebuild(event) {
  const trigger = event.currentTarget;
  const ok = await confirmDialog({
    title: "Rebuild the whole index?",
    message: "Every file is read and indexed again. This takes longer than an update, but search keeps working with the current index until it finishes.",
    confirmLabel: "Rebuild",
    trigger,
  });
  if (ok) actions.reindex?.({ full: true }, { trigger });
}

function renderMaintenance() {
  const stats = state.stats || {};
  const vectors = Number(stats.vectors) || 0;
  const missing = Number(stats.vectors_missing) || 0;
  const needVectors = vectors === 0 || missing > 0;
  const trigger = (run) => (event) => run(event.currentTarget);
  byId("manage-maintenance-content").replaceChildren(
    block(
      el("h4", { class: "block-title", text: "Index" }),
      copyLine("Update reads only new and changed files. A full rebuild re-reads everything. Add a folder to search it in this collection too."),
      el("div", { class: "button-row" },
        keyed(button("Update index", { variant: "secondary", iconName: "refresh", onClick: trigger((node) => actions.reindex?.({}, { trigger: node })) }), "update-index"),
        keyed(button("Add a folder…", { variant: "secondary", iconName: "plus", onClick: trigger((node) => actions.openIndexWizard?.({ trigger: node })) }), "add-folder"),
        keyed(button("Full rebuild", { variant: "secondary", iconName: "rotate-cw", onClick: fullRebuild }), "full-rebuild"),
        needVectors
          ? keyed(button("Reindex with vectors", {
            variant: "secondary",
            iconName: "sparkle",
            onClick: trigger((node) => actions.reindex?.({ embed: true }, { trigger: node })),
            title: "Build vectors so you can search by meaning",
          }), "reindex-vectors")
          : null,
      ),
      needVectors ? null : el("p", { class: "status-line", dataset: { tone: "ok" } },
        icon("check-circle", "icon inline-icon"), "Vectors complete: every document can be found by meaning."),
    ),
    block(
      el("h4", { class: "block-title", text: "Integrity" }),
      copyLine("Check that the search index matches your documents, and repair it if it does not."),
      el("div", { class: "button-row" },
        keyed(button("Check integrity", { variant: "secondary", iconName: "check-circle", onClick: runCheck }), "check"),
      ),
      resultBox("maint-result"),
    ),
    block(
      el("h4", { class: "block-title", text: "Compact" }),
      copyLine("Reclaims unused space in the collection file after large deletions or rebuilds."),
      el("div", { class: "button-row" },
        keyed(button("Compact", { variant: "secondary", iconName: "compress", onClick: runCompact }), "compact"),
      ),
      resultBox("compact-result"),
    ),
  );
}

/* ---------- backups ---------- */

async function backupNow(event) {
  await busy(event.currentTarget, "Backing up…", async () => {
    try {
      const backup = await api.backup();
      toast(`Backup created (${formatBytes(backup?.bytes)})`);
      await loadBackups();
      keepingFocus("manage-backups-content", renderBackups);
    } catch (error) {
      toastError(error, "The backup could not be created.");
    }
  });
}

async function restoreAsNew(item, trigger) {
  const name = await promptDialog({
    title: "Restore as a new collection",
    label: "Name for the restored collection",
    value: `${activeName() || "Collection"} (restored)`,
    hint: "Your current collection stays exactly as it is.",
    confirmLabel: "Restore",
    trigger,
  });
  if (!name) return;
  try {
    await api.restore({ backup_path: item.path, mode: "new", name });
    await actions.onCollectionChanged?.();
    toast(`Restored as “${truncate(name, 40)}”`);
  } catch (error) {
    toastError(error, "The backup could not be restored.");
  }
}

async function restoreReplace(item, trigger) {
  const name = activeName();
  if (!name) return;
  const ok = await confirmDialog({
    title: "Replace the current collection?",
    message: `“${name}” will be replaced with the backup from ${formatDateTime(item.created_at) || "this file"}. A backup of the current state is made first, so you can undo this.`,
    confirmLabel: "Replace collection",
    danger: true,
    typeToConfirm: name,
    trigger,
  });
  if (!ok) return;
  try {
    await api.restore({ backup_path: item.path, mode: "replace", confirm_name: name });
    await actions.onCollectionChanged?.();
    toast("Collection restored from backup");
  } catch (error) {
    toastError(error, "The backup could not be restored.");
  }
}

function backupItem(item) {
  const choices = el("div", { class: "restore-choices", hidden: true },
    button("Restore as a new collection", { variant: "secondary", iconName: "plus", onClick: (event) => restoreAsNew(item, event.currentTarget) }),
    button("Replace current…", { variant: "danger-ghost", iconName: "rotate-ccw", onClick: (event) => restoreReplace(item, event.currentTarget) }),
  );
  const toggle = button("Restore…", {
    variant: "ghost",
    iconName: "history",
    attrs: { "aria-expanded": "false" },
    onClick: () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      choices.hidden = !open;
    },
  });
  return el("li", { class: "backup-item" },
    el("div", { class: "backup-main" },
      el("p", { class: "backup-date", text: formatDateTime(item.created_at) || "Backup" }),
      el("p", { class: "backup-meta", text: `${formatBytes(item.bytes)}${item.collection ? ` · ${item.collection}` : ""}` }),
      el("p", { class: "mono-path", text: item.path || "" }),
    ),
    toggle,
    choices,
  );
}

function renderBackups() {
  const items = data.backups;
  byId("manage-backups-content").replaceChildren(
    copyLine("A backup is a complete copy of this collection file: the index, your history, saved searches and bookmarks."),
    el("div", { class: "button-row" }, keyed(button("Back up now", { variant: "primary", iconName: "download", onClick: backupNow }), "backup-now")),
    items.length
      ? el("ul", { class: "backup-list", attrs: { "aria-label": "Backups" } }, items.slice(0, 20).map(backupItem))
      : copyLine("No backups yet."),
  );
}

async function loadBackups() {
  try {
    data.backups = itemsOf(await api.backups());
  } catch {
    data.backups = [];
  }
}

/* ---------- collections ---------- */

function renderCollectionInfo() {
  const entry = activeEntry();
  const name = activeName();
  const path = entry?.db || "";
  const children = [
    el("h4", { class: "block-title", text: name ? `This collection: ${name}` : "This collection" }),
  ];
  if (path) {
    children.push(el("div", { class: "path-row" },
      el("p", { class: "mono-path", text: path }),
      button("Copy path", {
        variant: "ghost",
        iconName: "copy",
        onClick: async () => {
          const ok = await copyText(path);
          toast(ok ? "Path copied" : "Copy failed", { tone: ok ? "success" : "warn" });
        },
      }),
    ));
  }
  children.push(
    copyLine("This collection is one portable file. It holds the search index and the text of your documents, along with your history, saved searches and bookmarks."),
    copyLine("To move it to another computer, make a backup (or copy the .db file while OneFind is closed), copy it across, then choose “Open existing collection file” there. Search and preview work straight away."),
    copyLine("What does not travel: your notes folders themselves. Opening files from results and updating the index both need those folders at the same location on the new computer. If they live somewhere else there, don’t add them again as a new folder, or every note is indexed a second time: use “Locate…” next to the missing folder in Health, or remove the old folder from the collection first and then add the new location."),
  );
  byId("manage-collection-content").replaceChildren(block(...children));
}

async function renameFlow(item, trigger) {
  const newName = await promptDialog({
    title: "Rename collection",
    label: "New name",
    value: item.name,
    confirmLabel: "Rename",
    trigger,
  });
  if (!newName || newName === item.name) return;
  try {
    await api.renameCollection(item.name, newName);
    toast(`Renamed to “${truncate(newName, 40)}”`);
    await actions.refreshStats?.();
    await refreshManage();
    // The renamed row was rebuilt: put focus back on its Rename button.
    const again = [...byId("manage-collections-content").querySelectorAll("[data-focus-key]")]
      .find((node) => node.dataset.focusKey === `rename:${newName}`);
    again?.focus({ preventScroll: true });
  } catch (error) {
    toastError(error, "The collection could not be renamed.");
  }
}

async function removeFlow(item, deleteFile, trigger) {
  const ok = await confirmDialog(deleteFile
    ? {
      title: "Delete this collection file?",
      message: `The file for “${item.name}” will be permanently deleted. Your notes folder is not touched. Backups are kept.`,
      confirmLabel: "Delete file",
      danger: true,
      typeToConfirm: item.name,
      trigger,
    }
    : {
      title: "Remove from the list?",
      message: `“${item.name}” will be removed from OneFind's list. The file stays on disk and you can open it again later.`,
      confirmLabel: "Remove from list",
      trigger,
    });
  if (!ok) return;
  try {
    await api.removeCollection(item.name, deleteFile, deleteFile ? item.name : null);
    toast(deleteFile ? "Collection file deleted" : "Removed from the list");
    if (item.active) await actions.onCollectionChanged?.();
    else await refreshManage();
  } catch (error) {
    toastError(error, "The collection could not be removed.");
  }
}

function collectionRow(item) {
  const facts = [item.exists === false ? "File missing" : plural(item.documents ?? 0, "document")];
  if (Number(item.db_bytes)) facts.push(formatBytes(item.db_bytes));
  const actionsRow = el("div", { class: "row-actions" });
  if (!item.active && item.exists !== false) {
    actionsRow.append(button("Switch", { variant: "secondary", onClick: () => actions.switchCollection?.(item.name) }));
  }
  // Visible words, not bare icons: one of these deletes a file. The accessible
  // names start with the visible label and add which collection they act on.
  actionsRow.append(
    keyed(button("Rename", {
      iconName: "pencil",
      attrs: { "aria-label": `Rename ${item.name}` },
      onClick: (event) => renameFlow(item, event.currentTarget),
    }), `rename:${item.name}`),
    keyed(button("Remove", {
      iconName: "x",
      attrs: { "aria-label": `Remove ${item.name} from the list` },
      title: "Remove from OneFind's list. The file stays on disk.",
      onClick: (event) => removeFlow(item, false, event.currentTarget),
    }), `remove:${item.name}`),
    keyed(button("Delete file", {
      variant: "danger-ghost",
      iconName: "trash",
      attrs: { "aria-label": `Delete file for ${item.name}` },
      onClick: (event) => removeFlow(item, true, event.currentTarget),
    }), `delete:${item.name}`),
  );
  return el("li", { class: "collection-row", dataset: { active: String(Boolean(item.active)) } },
    el("div", { class: "collection-row-main" },
      el("p", { class: "collection-row-name" }, el("span", { text: item.name }), item.active ? el("span", { class: "badge", text: "Active" }) : null),
      el("p", { class: "backup-meta", text: facts.join(" · ") }),
    ),
    actionsRow,
  );
}

function renderCollections() {
  const list = itemsOf(data.collections, "collections");
  const content = [
    el("h4", { class: "block-title", text: "All collections" }),
  ];
  if (list.length) content.push(el("ul", { class: "collection-list" }, list.map(collectionRow)));
  else content.push(copyLine(data.collections ? "No collections yet." : "Collections are not available on this server."));
  content.push(el("div", { class: "button-row" },
    button("New collection", { variant: "secondary", iconName: "plus", onClick: (event) => actions.createCollectionFlow?.(event.currentTarget) }),
    button("Open existing collection file…", { variant: "ghost", iconName: "folder-open", onClick: (event) => actions.openCollectionPrompt?.(event.currentTarget) }),
  ));
  const name = activeName();
  if (name) {
    content.push(el("div", { class: "danger-zone" },
      el("h4", { class: "block-title", text: "Clear this collection" }),
      copyLine("Removes every document, vector, search history entry, saved search and bookmark from this collection. Your files are not touched."),
      button("Clear collection…", {
        variant: "danger-ghost",
        iconName: "trash",
        onClick: async (event) => {
          const ok = await confirmDialog({
            title: `Clear “${name}”?`,
            message: "Everything in this collection is removed. Your notes folder is not touched. Consider making a backup first.",
            confirmLabel: "Clear collection",
            danger: true,
            typeToConfirm: name,
            trigger: event.currentTarget,
          });
          if (!ok) return;
          try {
            await api.clear(name);
            toast("Collection cleared");
            await actions.onCollectionChanged?.({ quiet: true });
          } catch (error) {
            toastError(error, "The collection could not be cleared.");
          }
        },
      }),
    ));
  }
  byId("manage-collections-content").replaceChildren(block(...content));
}

/* ---------- preferences ---------- */

function renderPrefs() {
  for (const radio of document.querySelectorAll('input[name="theme-pref"]')) radio.checked = radio.value === themePref();
  byId("pref-reduce-motion").checked = userReducedMotion();
  byId("pref-single-keys").checked = singleKeysEnabled();
}

/* ---------- lifecycle ---------- */

function renderAll() {
  const name = activeName();
  // The collection name keeps its own capitalisation inside the uppercase eyebrow.
  byId("manage-collection").replaceChildren("Collection", ...(name ? [" · ", el("span", { class: "eyebrow-name", text: name })] : []));
  keepingFocus("manage-body", () => {
    renderHealth();
    renderMaintenance();
    renderBackups();
    renderCollectionInfo();
    renderCollections();
    renderPrefs();
  });
}

async function loadChanges() {
  data.changesState = "loading";
  renderHealth();
  try {
    data.changes = await api.changes();
    data.changesState = "done";
  } catch {
    data.changes = null;
    data.changesState = "error";
  }
  if (dialog().open) renderHealth();
}

/** Reload collections and backups; `changes` also re-scans the folders. */
let manageSeq = 0;

export async function refreshManage({ changes = false } = {}) {
  const seq = ++manageSeq;
  const [collections] = await Promise.all([
    api.collections().catch(() => null),
    loadBackups(),
  ]);
  if (seq !== manageSeq) return; // a newer refresh (e.g. after a collection switch) owns the panel
  data.collections = collections;
  renderAll();
  if (changes) await loadChanges();
}

function scrollToSection(id) {
  const section = byId(id);
  if (!section) return;
  // Scroll only the sheet body; scrollIntoView would also scroll the dialog
  // and push its header out of view.
  byId("manage-body").scrollTo({ top: section.offsetTop, behavior: "auto" });
  section.focus({ preventScroll: true });
  markSection(id);
}

/** The section tab for the part of the sheet in view carries aria-current. */
function markSection(id) {
  for (const nav of dialog().querySelectorAll(".sheet-nav [data-section]")) {
    if (nav.dataset.section === id) nav.setAttribute("aria-current", "true");
    else nav.removeAttribute("aria-current");
  }
}

function watchSections() {
  if (typeof IntersectionObserver !== "function") return;
  const tabs = new Set([...dialog().querySelectorAll(".sheet-nav [data-section]")].map((nav) => nav.dataset.section));
  const visible = new Map();
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) visible.set(entry.target.id, entry.isIntersecting ? entry.boundingClientRect.top : null);
    // The topmost section that is (partly) in view wins.
    const current = [...visible].filter(([id, top]) => top !== null && tabs.has(id)).sort((a, b) => a[1] - b[1])[0];
    if (current) markSection(current[0]);
  }, { root: byId("manage-body"), rootMargin: "0px 0px -55% 0px" });
  for (const section of dialog().querySelectorAll(".sheet-section")) observer.observe(section);
}

export async function openManage({ section, trigger } = {}) {
  openDialog(dialog(), { trigger });
  renderAll();
  if (section) scrollToSection(section);
  else {
    byId("manage-body").scrollTo({ top: 0, behavior: "auto" });
    dialog().querySelector(".dialog-head [data-close]")?.focus();
  }
  await refreshManage();
  if (section) scrollToSection(section);
  loadChanges();
}

export function initManage() {
  for (const nav of dialog().querySelectorAll(".sheet-nav [data-section]")) {
    nav.addEventListener("click", () => scrollToSection(nav.dataset.section));
  }
  for (const radio of document.querySelectorAll('input[name="theme-pref"]')) {
    radio.addEventListener("change", () => {
      if (radio.checked) setTheme(radio.value);
    });
  }
  byId("pref-reduce-motion").addEventListener("change", (event) => {
    setReducedMotion(event.target.checked);
    announce(event.target.checked ? "Motion reduced." : "Motion restored.");
  });
  byId("pref-single-keys").addEventListener("change", (event) => {
    setSingleKeysEnabled(event.target.checked);
    announce(event.target.checked ? "Single-key shortcuts on." : "Single-key shortcuts off.");
  });
  watchSections();
  byId("manage-button").addEventListener("click", (event) => openManage({ trigger: event.currentTarget }));
  on("theme", renderPrefs);
  on("stats", (stats) => {
    // Background polls arrive every few seconds: re-render only when the
    // collection actually changed, so focus and results stay put.
    const signature = JSON.stringify([
      stats?.collection?.name, stats?.status, stats?.status_detail, stats?.documents, stats?.vectors,
      stats?.vectors_missing, stats?.db_bytes, stats?.last_indexed_at, stats?.health?.ok,
    ]);
    if (signature === statsSignature) return;
    statsSignature = signature;
    if (dialog().open) {
      keepingFocus("manage-health-content", renderHealth);
      keepingFocus("manage-maintenance-content", renderMaintenance);
    }
  });
  Object.assign(actions, { openManage, refreshManage });
}
