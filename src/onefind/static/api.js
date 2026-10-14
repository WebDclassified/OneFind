// HTTP client for the local OneFind service (contract section 6).
// Every request carries the X-OneFind-Request CSRF header.

const CSRF_HEADER = { "X-OneFind-Request": "1" };

export class ApiError extends Error {
  constructor(message, { code = "request_failed", status = 0, retryable = false, action = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.retryable = retryable;
    this.action = action;
  }
}

/** The streaming endpoint is missing or the browser cannot read streams. */
export class StreamUnavailable extends Error {
  constructor(message = "Streaming search is unavailable") {
    super(message);
    this.name = "StreamUnavailable";
  }
}

const FALLBACK_MESSAGES = {
  404: "That item could not be found. It may have been moved or removed.",
  409: "That conflicts with something that already exists.",
  413: "That request is too large.",
  503: "The local search service is not ready yet.",
};

export function errorFromBody(body, status) {
  const detail = body?.error ?? body?.detail;
  let message = "";
  if (typeof detail === "string") message = detail;
  else if (detail && typeof detail.message === "string") message = detail.message;
  else if (Array.isArray(detail) && detail[0]?.msg) message = String(detail[0].msg);
  if (!message) {
    message = FALLBACK_MESSAGES[status]
      || (status >= 500 ? "The local search service could not complete the request." : "The request could not be completed.");
  }
  const code = (detail && typeof detail === "object" && !Array.isArray(detail) && detail.code) || "request_failed";
  return new ApiError(message, {
    code,
    status,
    retryable: Boolean(detail?.retryable) || status >= 500,
    action: typeof detail?.action === "string" ? detail.action : null,
  });
}

function offlineError() {
  return new ApiError("OneFind's local service is not responding. Check that it is still running.", {
    code: "offline",
    retryable: true,
    action: "retry",
  });
}

async function readJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function request(method, url, { body, signal, query } = {}) {
  const target = new URL(url, window.location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") target.searchParams.set(key, String(value));
  }
  const headers = { Accept: "application/json" };
  // GETs carry it too: the server requires it on endpoints that read the disk (/api/fs/list).
  Object.assign(headers, CSRF_HEADER);
  const init = { method, headers, signal };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(target.pathname + target.search, init);
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw offlineError();
  }
  const payload = await readJson(response);
  if (!response.ok) throw errorFromBody(payload, response.status);
  return payload;
}

const get = (url, options) => request("GET", url, options);
const post = (url, body = {}, options = {}) => request("POST", url, { ...options, body });
const del = (url, options) => request("DELETE", url, options);
const enc = encodeURIComponent;
/** doc ids may contain "/" (they are relative paths), so keep the slashes. */
const encPath = (value) => String(value).split("/").map(enc).join("/");

/** Normalise list endpoints that may answer {items: […]}, {jobs: […]} or […]. */
export function itemsOf(payload, key = "items") {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload[key])) return payload[key];
  return [];
}

export const api = {
  stats: (signal) => get("/api/stats", { signal }),
  facets: (signal) => get("/api/facets", { signal }),
  search: (body, signal) => post("/api/search", body, { signal }),

  collections: () => get("/api/collections"),
  createCollection: (name, dbPath) => post("/api/collections", dbPath ? { name, db_path: dbPath } : { name }),
  openCollection: (dbPath, name) => post("/api/collections/open", name ? { db_path: dbPath, name } : { db_path: dbPath }),
  activateCollection: (name) => post("/api/collections/active", { name }),
  renameCollection: (name, newName) => post("/api/collections/rename", { name, new_name: newName }),
  removeCollection: (name, deleteFile, confirmName) =>
    post("/api/collections/remove", { name, delete_file: Boolean(deleteFile), ...(confirmName ? { confirm_name: confirmName } : {}) }),

  listDir: (path, signal) => get("/api/fs/list", { query: { path }, signal }),
  /** confirmNetwork: the person agreed to index a folder on another computer. */
  startIndex: (body, confirmNetwork = false) =>
    post("/api/index", confirmNetwork ? { ...body, confirm_network: true } : body),
  jobs: (activeOnly = false) => get("/api/jobs", { query: activeOnly ? { active: 1 } : {} }),
  job: (id, signal) => get(`/api/jobs/${enc(id)}`, { signal }),
  cancelJob: (id) => post(`/api/jobs/${enc(id)}/cancel`),
  changes: (signal) => get("/api/index/changes", { signal }),
  /** Forget a folder of the collection; its notes leave the index unless deleteDocuments is false. */
  removeRoot: (rootId, { deleteDocuments = true } = {}) =>
    post("/api/roots/remove", { root_id: rootId, delete_documents: Boolean(deleteDocuments) }),
  /** Point a moved folder at its new place; every note keeps its id. */
  relocateRoot: (rootId, path) => post("/api/roots/relocate", { root_id: rootId, path }),

  document: (docId, signal) => get(`/api/documents/${encPath(docId)}`, { signal }),
  openDocument: (docId, reveal = false, confirmNetwork = false) =>
    post("/api/documents/open", { doc_id: docId, reveal, ...(confirmNetwork ? { confirm_network: true } : {}) }),
  removeDocuments: (docIds) => post("/api/documents/remove", { doc_ids: docIds }),

  history: () => get("/api/history"),
  recordHistory: (entry) => post("/api/history", entry),
  clearHistory: () => post("/api/history/clear"),
  saved: () => get("/api/saved"),
  saveQuery: (entry) => post("/api/saved", entry),
  deleteSaved: (id) => del(`/api/saved/${enc(id)}`),
  bookmarks: () => get("/api/bookmarks"),
  addBookmark: (docId) => post("/api/bookmarks", { doc_id: docId }),
  removeBookmark: (docId) => del(`/api/bookmarks/${encPath(docId)}`),
  recentDocuments: (limit = 8) => get("/api/recent-documents", { query: { limit } }),

  backup: () => post("/api/maintenance/backup"),
  backups: () => get("/api/backups"),
  restore: (body) => post("/api/maintenance/restore", body),
  check: () => post("/api/maintenance/check"),
  repair: () => post("/api/maintenance/repair"),
  compact: () => post("/api/maintenance/compact"),
  clear: (confirmName) => post("/api/maintenance/clear", { confirm_name: confirmName }),
};

function handleStreamLine(line, onEvent) {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    throw new StreamUnavailable("The search stream sent an unreadable line");
  }
  if (!message || typeof message !== "object") return null;
  if (message.type === "stage") {
    onEvent?.(message);
    return null;
  }
  if (message.type === "result") {
    const { type: _type, ...payload } = message;
    return payload;
  }
  if (message.type === "error") throw errorFromBody({ error: message.error }, Number(message.status) || 400);
  return null;
}

/**
 * POST /api/search/stream and read NDJSON line by line. Stage events go to
 * onEvent; resolves with the final search response. Throws StreamUnavailable
 * when the caller should fall back to POST /api/search.
 */
export async function streamSearch(body, { signal, onEvent } = {}) {
  let response;
  try {
    response = await fetch("/api/search/stream", {
      method: "POST",
      headers: {
        Accept: "application/x-ndjson",
        "Content-Type": "application/json",
        ...CSRF_HEADER,
      },
      body: JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new StreamUnavailable("The search stream could not be opened");
  }
  if ([404, 405, 406, 415, 501].includes(response.status)) throw new StreamUnavailable();
  if (!response.ok) throw errorFromBody(await readJson(response), response.status);
  if (!response.body?.getReader) throw new StreamUnavailable("This browser cannot read streams");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (error) {
        if (error?.name === "AbortError") throw error;
        throw new StreamUnavailable("The search stream was interrupted");
      }
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) {
          const result = handleStreamLine(line, onEvent);
          if (result) return result;
        }
        newline = buffer.indexOf("\n");
      }
    }
    buffer += decoder.decode();
    const tail = buffer.trim();
    if (tail) {
      const result = handleStreamLine(tail, onEvent);
      if (result) return result;
    }
  } finally {
    reader.releaseLock?.();
  }
  throw new StreamUnavailable("The search stream ended without results");
}
