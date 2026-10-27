// Filter bar fed by GET /api/facets: folder, file type, modified date, tags.
// The pickers open from the Filters button; active filters always show as
// removable chips. Any change re-runs the search.

import { api } from "./api.js";
import { byId, el } from "./dom.js";
import { folderLabel, formatInteger, plural } from "./format.js";
import { icon } from "./icons.js";
import { enter, reducedMotion } from "./motion.js";
import { actions, activeFilterCount, DATE_PRESETS, emptyFilters, state } from "./store.js";
import { announce } from "./toast.js";

/** Long folder paths read best from the end: "… › Workshops › Q3". */
export function shortFolderLabel(folder) {
  const label = folderLabel(folder);
  const parts = label.split(" › ");
  return parts.length > 2 ? `… › ${parts.slice(-2).join(" › ")}` : label;
}

const FILTER_KINDS = {
  folders: { label: "Folder", format: folderLabel },
  exts: { label: "Type", format: (ext) => ext.replace(/^\./, "").toUpperCase() },
  tags: { label: "Tag", format: (tag) => `#${tag}` },
};

let previousChipKeys = new Set();
let facetsController = null;

function option(value, label, title) {
  return el("option", { value, text: label, title: title || undefined });
}

function fillSelect(select, placeholder, entries) {
  select.replaceChildren(option("", placeholder), ...entries.map(([value, label, title]) => option(value, label, title)));
  select.value = "";
  select.disabled = entries.length === 0;
}

function facetEntries(list, key, format, selected, full = null) {
  return (Array.isArray(list) ? list : [])
    .filter((item) => item && typeof item[key] === "string" && !selected.includes(item[key]))
    .slice(0, 200)
    .map((item) => [item[key], `${format(item[key])} (${formatInteger(item.count)})`, full ? full(item[key]) : null]);
}

function renderSelects() {
  const facets = state.facets || {};
  const { filters } = state;
  fillSelect(byId("filter-folder"), "Folder",
    facetEntries(facets.folders, "folder", shortFolderLabel, filters.folders, folderLabel));
  fillSelect(byId("filter-type"), "File type", facetEntries(facets.types, "ext", FILTER_KINDS.exts.format, filters.exts));
  const tags = facetEntries(facets.tags, "tag", FILTER_KINDS.tags.format, filters.tags);
  fillSelect(byId("filter-tag"), "Tag", tags);
  byId("filter-tag-wrap").hidden = tags.length === 0 && filters.tags.length === 0;
  byId("filter-date").value = filters.modified;
  byId("filter-date").disabled = !facets.modified || facets.modified.max === null;
}

function chip(key, label, onRemove) {
  const node = el("li", { class: "chip-item", dataset: { key } },
    el("button", {
      type: "button",
      class: "chip chip-filter",
      attrs: { "aria-label": `Remove filter ${label}` },
      on: { click: onRemove },
    }, el("span", { text: label }), icon("x", "icon chip-x")),
  );
  return node;
}

function renderChips() {
  const { filters } = state;
  const chips = [];
  for (const [kind, spec] of Object.entries(FILTER_KINDS)) {
    for (const value of filters[kind]) {
      chips.push(chip(`${kind}:${value}`, `${spec.label}: ${spec.format(value)}`, () => removeFilter(kind, value)));
    }
  }
  if (filters.modified !== "any") {
    const key = `modified:${filters.modified}`;
    chips.push(chip(key, `Modified: ${DATE_PRESETS[filters.modified].label}`, () => {
      const index = chipIndex(key);
      state.filters.modified = "any";
      filtersChanged("Date filter removed");
      focusAfterChipRemoval(Math.max(0, index));
    }));
  }
  const list = byId("filter-chips");
  list.replaceChildren(...chips);
  byId("filter-bar").classList.toggle("has-chips", chips.length > 0);
  if (!reducedMotion()) {
    for (const node of chips) if (!previousChipKeys.has(node.dataset.key)) node.classList.add("chip-enter");
  }
  previousChipKeys = new Set(chips.map((node) => node.dataset.key));
  list.hidden = chips.length === 0;

  const count = activeFilterCount();
  byId("filters-clear").hidden = count === 0;
  const badge = byId("filters-count");
  badge.textContent = String(count);
  badge.hidden = count === 0;
  byId("filters-toggle").setAttribute("aria-label", count ? `Filters, ${plural(count, "active filter")}` : "Filters");
}

export function renderFilters() {
  renderSelects();
  renderChips();
}

function filtersChanged(message) {
  renderFilters();
  if (message) announce(message);
  if (byId("search-input").value.trim()) actions.runSearch?.({ record: true });
}

function addFilter(kind, value) {
  if (!value || state.filters[kind].includes(value)) return;
  state.filters[kind] = [...state.filters[kind], value];
  filtersChanged(`Filter added: ${FILTER_KINDS[kind].label} ${FILTER_KINDS[kind].format(value)}`);
}

/** After a chip is removed, focus its neighbour (or the Filters button), not the top of the page. */
function focusAfterChipRemoval(index) {
  const chips = [...byId("filter-chips").querySelectorAll(".chip-filter")];
  const target = chips[index] || chips[index - 1] || byId("filters-toggle");
  target.focus({ preventScroll: true });
}

function chipIndex(key) {
  return [...byId("filter-chips").children].findIndex((node) => node.dataset.key === key);
}

function removeFilter(kind, value) {
  const index = chipIndex(`${kind}:${value}`);
  state.filters[kind] = state.filters[kind].filter((item) => item !== value);
  filtersChanged(`Filter removed: ${FILTER_KINDS[kind].label} ${FILTER_KINDS[kind].format(value)}`);
  focusAfterChipRemoval(Math.max(0, index));
}

export function clearFilters() {
  state.filters = emptyFilters();
  filtersChanged("Filters cleared");
}

/** Replace folder filters with one folder ("Filter to this folder"). */
export function filterToFolder(folder) {
  state.filters.folders = [folder];
  filtersChanged(`Showing results in ${folderLabel(folder)} only`);
}

export function setFilters(filters) {
  state.filters = { ...emptyFilters(), ...filters };
  renderFilters();
}

function setFilterBarOpen(open) {
  const bar = byId("filter-bar");
  bar.dataset.open = String(open);
  byId("filters-toggle").setAttribute("aria-expanded", String(open));
  if (open) enter(bar.querySelector(".filter-controls"), "view-enter");
}

/**
 * Apply a <select> filter without firing on every arrow key: Chrome on
 * Windows sends "change" for each ArrowDown on a closed select, so a
 * keyboard change waits for Enter or for focus to leave the select.
 */
function onFilterSelect(select, apply) {
  let fromKeys = false;
  let pending = false;
  const commit = () => {
    if (!pending) return;
    pending = false;
    apply(select.value);
  };
  select.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    } else if (event.key === " " || event.key === "F4" || event.altKey) {
      fromKeys = false; // opens the option list; a choice made there applies at once
    } else if (event.key !== "Tab" && event.key !== "Escape") {
      fromKeys = true;
    }
  });
  select.addEventListener("pointerdown", () => {
    fromKeys = false;
  });
  select.addEventListener("change", () => {
    pending = true;
    if (!fromKeys) commit();
  });
  select.addEventListener("blur", () => {
    commit();
    fromKeys = false;
  });
}

export async function loadFacets() {
  facetsController?.abort();
  facetsController = new AbortController();
  try {
    state.facets = await api.facets(facetsController.signal);
  } catch (error) {
    if (error?.name === "AbortError") return;
    state.facets = null;
  }
  const facets = state.facets;
  const hasFacets = Boolean(facets) && (facets.folders?.length || facets.types?.length || facets.tags?.length);
  byId("filter-bar").hidden = !hasFacets && activeFilterCount() === 0;
  byId("filters-toggle").hidden = !hasFacets;
  renderFilters();
}

export function initFilters() {
  onFilterSelect(byId("filter-folder"), (value) => addFilter("folders", value));
  onFilterSelect(byId("filter-type"), (value) => addFilter("exts", value));
  onFilterSelect(byId("filter-tag"), (value) => addFilter("tags", value));
  onFilterSelect(byId("filter-date"), (value) => {
    const next = Object.hasOwn(DATE_PRESETS, value) ? value : "any";
    if (next === state.filters.modified) return;
    state.filters.modified = next;
    filtersChanged(next === "any" ? "Date filter removed" : `Showing ${DATE_PRESETS[next].label.toLowerCase()}`);
  });
  byId("filters-clear").addEventListener("click", clearFilters);
  byId("filters-toggle").addEventListener("click", () => {
    setFilterBarOpen(byId("filter-bar").dataset.open !== "true");
  });
  byId("filters-toggle").hidden = true;
  Object.assign(actions, { clearFilters, filterToFolder, setFilters, loadFacets });
}
