// Shared application state, a tiny event bus, and an action registry.
// Modules register their entry points in `actions` so views can trigger
// each other without import cycles.

export const MODES = ["lexical", "semantic", "hybrid"];
export const PRECISIONS = ["float", "int8", "binary"];
export const RESULT_COUNTS = [5, 10, 20, 30];
export const DATE_PRESETS = {
  any: { label: "Any time", days: null },
  "7d": { label: "Past 7 days", days: 7 },
  "30d": { label: "Past 30 days", days: 30 },
  year: { label: "Past year", days: 365 },
};

export const state = {
  stats: null,
  facets: null,
  mode: "hybrid",
  precision: "float",
  k: 10,
  filters: emptyFilters(),
  view: "idle",
  lastResponse: null,
  lastRequest: null,
  previewDocId: null,
};

export const actions = {};

const listeners = new Map();

export function on(event, handler) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(handler);
  return () => listeners.get(event)?.delete(handler);
}

export function emit(event, detail) {
  for (const handler of listeners.get(event) || []) {
    try {
      handler(detail);
    } catch (error) {
      console.error(`OneFind: "${event}" handler failed`, error);
    }
  }
}

export function emptyFilters() {
  return { folders: [], exts: [], tags: [], modified: "any" };
}

export function activeFilterCount(filters = state.filters) {
  return filters.folders.length + filters.exts.length + filters.tags.length + (filters.modified !== "any" ? 1 : 0);
}

/** Filters in the request shape of SearchFilters.from_dict, or null. */
export function filtersPayload(filters = state.filters, now = Date.now()) {
  const payload = {};
  if (filters.folders.length) payload.folders = [...filters.folders];
  if (filters.exts.length) payload.exts = [...filters.exts];
  if (filters.tags.length) payload.tags = [...filters.tags];
  const days = DATE_PRESETS[filters.modified]?.days;
  if (days) {
    // Round to the minute so repeated searches send identical filters.
    payload.modified_after = Math.floor((now / 1000 - days * 86400) / 60) * 60;
  }
  return Object.keys(payload).length ? payload : null;
}

/**
 * Restore UI filters from a stored filters dict (saved queries, history).
 * A date preset is stored as an absolute `modified_after`, so it is read
 * back against `recordedAt` (when the entry was saved, an ISO string):
 * measured from today, a "Past 7 days" search saved last week would come
 * back as "Past 30 days", and widen again on every re-run.
 */
export function filtersFromPayload(payload, recordedAt = null, now = Date.now()) {
  const filters = emptyFilters();
  if (!payload || typeof payload !== "object") return filters;
  const strings = (value) => (Array.isArray(value) ? value.filter((item) => typeof item === "string") : []);
  filters.folders = strings(payload.folders);
  filters.exts = strings(payload.exts);
  filters.tags = strings(payload.tags);
  const after = Number(payload.modified_after);
  if (Number.isFinite(after) && after > 0) {
    const recorded = Date.parse(recordedAt || "");
    const reference = Number.isFinite(recorded) ? recorded : now;
    const days = (reference / 1000 - after) / 86400;
    filters.modified = days <= 8 ? "7d" : days <= 31 ? "30d" : "year";
  }
  return filters;
}

export function availableModes(stats = state.stats) {
  const modes = Array.isArray(stats?.modes) ? stats.modes.filter((mode) => MODES.includes(mode)) : [];
  return new Set(modes);
}

export function availablePrecisions(stats = state.stats) {
  const values = Array.isArray(stats?.precisions) ? stats.precisions.filter((p) => PRECISIONS.includes(p)) : [];
  return new Set(values);
}

/** Overall collection status, using the v1.2 `status` field when present. */
export function collectionStatus(stats = state.stats) {
  if (!stats) return "connecting";
  if (stats.offline) return "offline";
  if (typeof stats.status === "string" && stats.status) return stats.status;
  if (stats.ready) return stats.health?.ok === false ? "needs_attention" : "ready";
  return stats.documents === 0 && stats.health ? "empty" : "no_index";
}

/**
 * True when a newer auto-sync run finished for the same collection since the
 * previous stats (`syncs` counts runs that changed something).
 */
export function autoSyncFinished(previous, stats) {
  const sync = stats?.auto_sync;
  const syncs = Number(sync?.syncs);
  const collection = stats?.collection?.name ?? null;
  if (!Number.isFinite(syncs)) return { finished: false, next: { collection, syncs: null } };
  const finished = previous?.collection === collection && Number.isFinite(previous?.syncs) && syncs > previous?.syncs;
  return { finished, next: { collection, syncs } };
}

export function canSearch(stats = state.stats) {
  if (!stats || stats.offline) return false;
  if (stats.ready) return true;
  return Number(stats.documents) > 0;
}

/** localStorage wrapped in try/catch: private windows can throw. */
export const storage = {
  get(key, fallback = null) {
    try {
      const value = window.localStorage.getItem(key);
      return value === null ? fallback : value;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // storage unavailable; preference lasts for this page only
    }
  },
  remove(key) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // ignore
    }
  },
};
