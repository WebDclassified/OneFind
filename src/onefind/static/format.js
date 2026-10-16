// Formatting helpers that turn engine values into plain language.

const numberFormat = new Intl.NumberFormat();

export const MODE_LABELS = { lexical: "Keyword", semantic: "Meaning", hybrid: "Hybrid" };
export const PRECISION_LABELS = { float: "Full", int8: "Int8", binary: "Binary" };

export function modeLabel(mode) {
  return MODE_LABELS[mode] || "Search";
}

export function formatInteger(value) {
  const number = Number(value);
  return Number.isFinite(number) ? numberFormat.format(number) : "—";
}

export function plural(count, singular, pluralForm = `${singular}s`) {
  const number = Number(count) || 0;
  return `${formatInteger(number)} ${number === 1 ? singular : pluralForm}`;
}

export function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = "B";
  for (const candidate of units) {
    size /= 1024;
    unit = candidate;
    if (size < 1024) break;
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${unit}`;
}

export function formatMs(value) {
  const ms = Number(value);
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

/** Accepts an ISO string or epoch seconds; returns a Date or null. */
export function toDate(value) {
  if (value === null || value === undefined || value === "") return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(String(value));
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value) {
  const date = toDate(value);
  if (!date) return "";
  const sameYear = date.getFullYear() === new Date().getFullYear();
  return date.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(sameYear ? {} : { year: "numeric" }),
  });
}

export function formatDateTime(value) {
  const date = toDate(value);
  if (!date) return "";
  return date.toLocaleString(undefined, {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function relativeTime(value, now = Date.now()) {
  const date = toDate(value);
  if (!date) return "";
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return formatDate(date.toISOString());
}

/** Split a stored source path into readable parts: ["notes", "work", "plan.md"]. */
export function pathParts(source) {
  if (typeof source !== "string" || !source) return [];
  return source.split(/[\\/]+/).filter(Boolean);
}

export function readablePath(source, maxParts = 4) {
  const parts = pathParts(source);
  if (parts.length <= maxParts) return parts.join(" › ");
  return ["…", ...parts.slice(-maxParts)].join(" › ");
}

export function fileName(source) {
  const parts = pathParts(source);
  return parts.length ? parts[parts.length - 1] : "";
}

export function folderLabel(folder) {
  if (folder === "" || folder === "." || folder === "/") return "Top level";
  return pathParts(folder).join(" › ") || String(folder);
}

export function truncate(text, max) {
  const value = String(text ?? "");
  if (value.length <= max) return value;
  return `${value.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

/** Last component of a filesystem path for labels ("C:\\Users\\me\\Notes" -> "Notes"). */
export function baseName(path) {
  const parts = pathParts(path);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}
