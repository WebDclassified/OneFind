"use strict";

const byId = (id) => document.getElementById(id);
const dom = {
  form: byId("search-form"),
  input: byId("search-input"),
  clear: byId("clear-button"),
  submit: byId("search-button"),
  resultCount: byId("result-count"),
  modeExplainer: byId("mode-explainer"),
  precisionGroup: byId("precision-group"),
  serviceStatus: byId("service-status"),
  documentCount: byId("document-count"),
  vectorCount: byId("vector-count"),
  indexSize: byId("index-size"),
  examples: byId("examples"),
  resultsSection: byId("results-section"),
  resultsSummary: byId("results-summary"),
  liveStatus: byId("live-status"),
  resultsList: byId("results-list"),
  idle: byId("idle-state"),
  loading: byId("loading-state"),
  loadingTitle: byId("loading-title"),
  loadingCopy: byId("loading-copy"),
  noIndex: byId("no-index-state"),
  empty: byId("empty-state"),
  emptyCopy: byId("empty-copy"),
  error: byId("error-state"),
  errorMessage: byId("error-message"),
  retry: byId("retry-button"),
};

const state = {
  stats: null,
  mode: "hybrid",
  precision: "float",
  k: 10,
  requestId: 0,
  controller: null,
  ready: false,
  hasSearched: false,
};

const MODE_HELP = {
  lexical: "Keyword mode is tuned for record IDs, names, dates, codes, and exact phrases.",
  semantic: "Meaning mode compares related ideas, even when the wording changes.",
  hybrid: "Recommended: Hybrid combines exact keyword evidence with meaning-based retrieval.",
};

const PRECISION_HELP = {
  float: "Full precision is the recommended local setting.",
  int8: "Int8 uses quantized cosine similarity in a bounded local scan.",
  binary: "Binary uses one-bit sign vectors and Hamming distance.",
};

const STATES = ["idle", "loading", "noIndex", "empty", "error"];

function setHidden(element, hidden) {
  element.hidden = Boolean(hidden);
}

function selectedRadio(name) {
  return document.querySelector(`input[name="${name}"]:checked`);
}

function setState(name) {
  for (const stateName of STATES) {
    setHidden(dom[stateName], stateName !== name);
  }
  setHidden(dom.resultsList, name !== "results");
  dom.resultsSection.setAttribute("aria-busy", name === "loading" ? "true" : "false");
}

function setServiceStatus(label, status) {
  dom.serviceStatus.classList.remove("ready", "error");
  if (status) dom.serviceStatus.classList.add(status);
  const text = document.createTextNode(label);
  const dot = document.createElement("span");
  dot.className = "status-dot";
  dot.setAttribute("aria-hidden", "true");
  dom.serviceStatus.replaceChildren(dot, text);
}

function formatInteger(value) {
  return Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "—";
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB"];
  let size = value;
  let unit = "B";
  for (const candidate of units) {
    size /= 1024;
    unit = candidate;
    if (size < 1024 || candidate === units.at(-1)) break;
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${unit}`;
}

function normalizeStats(payload) {
  if (!payload || typeof payload !== "object") throw new Error("Invalid stats response");
  const modes = Array.isArray(payload.modes) ? payload.modes : ["lexical"];
  const precisions = Array.isArray(payload.precisions) ? payload.precisions : [];
  return {
    ready: Boolean(payload.ready),
    documents: Number(payload.documents) || 0,
    vectors: Number(payload.vectors) || 0,
    dbBytes: Number(payload.db_bytes) || 0,
    model: typeof payload.model === "string" ? payload.model : null,
    modelDim: Number(payload.model_dim) || 0,
    health: payload.health && typeof payload.health === "object" ? payload.health : null,
    modes: modes.filter((mode) => ["lexical", "semantic", "hybrid"].includes(mode)),
    precisions: precisions.filter((value) => ["float", "int8", "binary"].includes(value)),
  };
}

function configureControls(stats) {
  const supportedModes = new Set(stats.modes);
  const supportedPrecisions = new Set(stats.precisions);
  for (const input of document.querySelectorAll('input[name="mode"]')) {
    input.disabled = !supportedModes.has(input.value);
    input.closest("label").title = input.disabled
      ? "This mode is unavailable for the loaded index."
      : "";
  }
  for (const input of document.querySelectorAll('input[name="precision"]')) {
    input.disabled = !supportedPrecisions.has(input.value);
    input.closest("label").title = input.disabled
      ? "This precision is unavailable for the loaded index."
      : "";
  }

  const preferredMode = supportedModes.has("hybrid")
    ? "hybrid"
    : supportedModes.has("semantic")
      ? "semantic"
      : "lexical";
  const modeInput = document.querySelector(`input[name="mode"][value="${preferredMode}"]`);
  if (modeInput) modeInput.checked = true;

  const preferredPrecision = supportedPrecisions.has("float")
    ? "float"
    : supportedPrecisions.values().next().value || "float";
  const precisionInput = document.querySelector(
    `input[name="precision"][value="${preferredPrecision}"]`,
  );
  if (precisionInput) precisionInput.checked = true;

  state.mode = preferredMode;
  state.precision = preferredPrecision;
  document.querySelector('input[name="mode"][value="hybrid"]')?.closest("label")
    ?.classList.toggle("recommended", preferredMode === "hybrid");
  updateModeUI();
  updateControlAvailability();
}

function updateControlAvailability() {
  const hasIndex = Boolean(state.stats?.ready);
  dom.input.disabled = !hasIndex;
  dom.submit.disabled = !hasIndex;
  dom.resultCount.disabled = !hasIndex;
  dom.clear.disabled = !hasIndex;
  for (const button of dom.examples.querySelectorAll("button")) button.disabled = !hasIndex;
}

function updateModeUI() {
  state.mode = selectedRadio("mode")?.value || "hybrid";
  state.precision = selectedRadio("precision")?.value || "float";
  state.k = Number(dom.resultCount.value) || 10;
  const semanticMode = state.mode !== "lexical";
  setHidden(dom.precisionGroup, !semanticMode);
  const help = semanticMode
    ? `${MODE_HELP[state.mode]} ${PRECISION_HELP[state.precision] || ""}`.trim()
    : MODE_HELP.lexical;
  dom.modeExplainer.textContent = help;
  for (const input of document.querySelectorAll('input[name="mode"]')) {
    input.closest("label")?.classList.toggle("selected", input.checked);
  }
  const hybridInput = document.querySelector('input[name="mode"][value="hybrid"]');
  const recommendation = hybridInput?.closest("label")?.querySelector("em");
  if (recommendation) recommendation.hidden = hybridInput.disabled;
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const error = new Error(readErrorMessage(body, response.status));
    error.code = readErrorCode(body);
    error.status = response.status;
    throw error;
  }
  return body;
}

function readErrorCode(body) {
  return body?.error?.code || body?.detail?.code || "request_failed";
}

function readErrorMessage(body, status) {
  const detail = body?.error || body?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail.message === "string") return detail.message;
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
  if (status === 404) return "The requested local endpoint was not found.";
  if (status >= 500) return "The local search service could not complete the request.";
  return "The request could not be completed.";
}

async function loadStats() {
  setServiceStatus("Connecting", null);
  try {
    const payload = await fetchJSON("/api/stats");
    const stats = normalizeStats(payload);
    state.stats = stats;
    state.ready = stats.ready;
    configureControls(stats);

    dom.documentCount.textContent = formatInteger(stats.documents);
    dom.vectorCount.textContent = formatInteger(stats.vectors);
    dom.indexSize.textContent = formatBytes(stats.dbBytes);

    if (!stats.ready) {
      setServiceStatus("No index", "error");
      setState("noIndex");
      dom.resultsSummary.textContent = "No local index · build one to depart";
      announce("No local index is loaded.");
      return false;
    } else {
      const healthLabel = stats.health?.ok === false ? "Index needs attention" : "Index ready";
      setServiceStatus(healthLabel, stats.health?.ok === false ? "error" : "ready");
      setState("idle");
      dom.resultsSummary.textContent = `Ready · ${formatInteger(stats.documents)} documents available`;
      announce(`${stats.documents} documents are available.`);
      return true;
    }
  } catch (error) {
    state.ready = false;
    updateControlAvailability();
    setServiceStatus("Unavailable", "error");
    showError("The local service statistics could not be loaded. Is the server still running?");
    return false;
  }
}

function announce(message) {
  dom.liveStatus.textContent = "";
  window.setTimeout(() => {
    dom.liveStatus.textContent = message;
  }, 20);
}

function showError(message) {
  dom.errorMessage.textContent = message;
  const noAnswerGuidance = document.querySelector(".no-answer-guidance");
  if (noAnswerGuidance) noAnswerGuidance.hidden = true;
  setState("error");
  dom.resultsSummary.textContent = "Request could not be completed.";
  announce("Search failed.");
}

function normalizeSnippet(value) {
  if (value && typeof value === "object" && Array.isArray(value.segments)) {
    const segments = value.segments
      .filter((segment) => segment && typeof segment.text === "string")
      .slice(0, 80)
      .map((segment) => ({
        text: segment.text.slice(0, 2000),
        highlight: Boolean(segment.highlight),
      }));
    if (segments.length) return segments;
  }
  if (typeof value === "string") return [{ text: value.slice(0, 2000), highlight: false }];
  return [];
}

function normalizeResult(raw, index) {
  if (!raw || typeof raw !== "object") throw new Error("Invalid result payload");
  const score = typeof raw.score === "object" && raw.score ? raw.score : { value: raw.score, metric: "score" };
  return {
    rank: Number.isInteger(raw.rank) ? raw.rank : index + 1,
    docId: typeof raw.doc_id === "string" ? raw.doc_id : "",
    title: typeof raw.title === "string" ? raw.title : "",
    source: typeof raw.source === "string" ? raw.source : "",
    snippet: normalizeSnippet(raw.snippet),
    score: Number.isFinite(Number(score.value)) ? Number(score.value) : 0,
    scoreMetric: typeof score.metric === "string" ? score.metric : "score",
  };
}

function renderSnippet(target, segments) {
  const fragment = document.createDocumentFragment();
  for (const segment of segments) {
    if (segment.highlight) {
      const mark = document.createElement("mark");
      mark.textContent = segment.text;
      fragment.append(mark);
    } else {
      fragment.append(document.createTextNode(segment.text));
    }
  }
  target.append(fragment);
}

function modeLabel(mode) {
  if (mode === "lexical") return "Keyword";
  if (mode === "semantic") return "Meaning";
  if (mode === "hybrid") return "Hybrid";
  return "Route";
}

function renderResults(results, meta) {
  const fragment = document.createDocumentFragment();
  results.forEach((result, index) => {
    const item = document.createElement("li");
    const card = document.createElement("article");
    card.className = "result-card";

    const rank = document.createElement("div");
    rank.className = "result-rank";
    rank.setAttribute("aria-label", `Result ${index + 1}`);
    rank.textContent = String(index + 1).padStart(2, "0");

    const body = document.createElement("div");
    body.className = "result-body";
    const route = document.createElement("div");
    route.className = "result-route";
    const routePath = document.createElement("span");
    routePath.textContent = "Query > Document > Route";
    const badge = document.createElement("span");
    badge.className = index === 0 ? "result-badge current" : "result-badge";
    badge.textContent = index === 0 ? "Current" : `Waypoint ${String(index + 1).padStart(2, "0")}`;
    route.append(routePath, badge);
    body.append(route);

    const title = document.createElement("h3");
    title.className = "result-title";
    title.textContent = result.title || result.docId || "Untitled document";
    body.append(title);

    const metadata = document.createElement("div");
    metadata.className = "result-meta";
    const channel = document.createElement("span");
    channel.textContent = `${modeLabel(meta.mode)} route`;
    metadata.append(channel);
    if (result.docId) {
      const docId = document.createElement("span");
      docId.textContent = result.docId;
      docId.title = result.docId;
      metadata.append(docId);
    }
    if (result.source && result.source !== result.docId) {
      const source = document.createElement("span");
      source.textContent = result.source;
      source.title = result.source;
      metadata.append(source);
    }
    body.append(metadata);

    if (result.snippet.length) {
      const snippet = document.createElement("p");
      snippet.className = "result-snippet";
      renderSnippet(snippet, result.snippet);
      body.append(snippet);
    }

    const score = document.createElement("div");
    score.className = "result-score";
    const scoreValue = document.createElement("strong");
    scoreValue.textContent = result.score.toFixed(4);
    const scoreMetric = document.createElement("small");
    scoreMetric.textContent = result.scoreMetric;
    score.append(scoreValue, scoreMetric);

    card.append(rank, body, score);
    item.append(card);
    fragment.append(item);
  });
  dom.resultsList.replaceChildren(fragment);
  setState("results");

  const resolvedMode = meta.mode.charAt(0).toUpperCase() + meta.mode.slice(1);
  dom.resultsSummary.textContent = `Route complete · ${results.length} result${results.length === 1 ? "" : "s"} · ${resolvedMode} · ${Math.round(meta.tookMs)} ms`;
  announce(`${results.length} results found.`);
}

function renderNoResults(mode) {
  const suggestions = {
    lexical: "Keyword mode requires every query term. Try a record ID, a shorter phrase, or fewer exact words.",
    semantic: "No document was close enough in meaning. Try a broader description or Hybrid mode.",
    hybrid: "No document matched both signals. Try fewer words or switch to Meaning mode.",
  };
  dom.emptyCopy.textContent = suggestions[mode] || suggestions.hybrid;
  const noAnswerGuidance = document.querySelector(".no-answer-guidance");
  if (noAnswerGuidance) {
    noAnswerGuidance.hidden = mode !== "semantic" && mode !== "hybrid";
  }
  setState("empty");
  dom.resultsSummary.textContent = "No close match · adjust route";
  announce("No matching documents found.");
}

function updateUrlState() {
  if (!window.history?.replaceState) return;
  const url = new URL(window.location.href);
  if (dom.input.value.trim()) {
    url.searchParams.set("q", dom.input.value.trim());
    url.searchParams.set("mode", state.mode);
    if (state.mode !== "lexical") url.searchParams.set("precision", state.precision);
    url.searchParams.set("k", String(state.k));
  } else {
    for (const key of ["q", "mode", "precision", "k"]) url.searchParams.delete(key);
  }
  window.history.replaceState(null, "", url);
}

async function submitSearch(event) {
  event?.preventDefault();
  const query = dom.input.value.trim();
  dom.clear.hidden = !dom.input.value;
  if (!query) {
    state.controller?.abort();
    updateUrlState();
    setState(state.stats?.ready ? "idle" : "noIndex");
    dom.resultsSummary.textContent = "Enter a query to begin.";
    dom.input.focus();
    return;
  }
  if (!state.stats?.ready) {
    setState("noIndex");
    return;
  }

  state.controller?.abort();
  const controller = new AbortController();
  state.controller = controller;
  const requestId = ++state.requestId;
  state.hasSearched = true;
  updateModeUI();
  updateUrlState();

  const firstSemanticLoad = state.mode !== "lexical" && !state.stats.modelLoaded;
  dom.loadingTitle.textContent = firstSemanticLoad
    ? "Preparing local meaning map…"
    : "Searching local route…";
  dom.loadingCopy.textContent = firstSemanticLoad
    ? "The first meaning search prepares the local embedding model."
    : `Ranking up to ${state.k} documents with ${state.mode} retrieval.`;
  const noAnswerGuidance = document.querySelector(".no-answer-guidance");
  if (noAnswerGuidance) noAnswerGuidance.hidden = true;
  setState("loading");
  dom.resultsSummary.textContent = "Routing through local index…";
  announce("Search started.");

  const started = performance.now();
  try {
    const payload = await fetchJSON("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        mode: state.mode,
        precision: state.precision,
        k: state.k,
      }),
      signal: controller.signal,
    });
    if (requestId !== state.requestId) return;
    if (!payload || !Array.isArray(payload.results)) throw new Error("Invalid search response");
    if (state.stats) state.stats.modelLoaded = true;
    const noAnswerGuidance = document.querySelector(".no-answer-guidance");
    if (noAnswerGuidance) noAnswerGuidance.hidden = true;
    const results = payload.results.map(normalizeResult);
    const elapsed = Number.isFinite(Number(payload.meta?.took_ms))
      ? Number(payload.meta.took_ms)
      : performance.now() - started;
    if (!results.length) {
      renderNoResults(state.mode);
    } else {
      renderResults(results, {
        mode: typeof payload.meta?.mode === "string" ? payload.meta.mode : state.mode,
        tookMs: elapsed,
      });
    }
  } catch (error) {
    if (error.name === "AbortError" || requestId !== state.requestId) return;
    showError(error.message || "The local search request failed.");
  } finally {
    if (requestId === state.requestId) {
      state.controller = null;
      dom.input.disabled = !state.stats?.ready;
      dom.submit.disabled = !state.stats?.ready;
    }
  }
}

function clearSearch() {
  state.controller?.abort();
  state.requestId += 1;
  dom.input.value = "";
  dom.clear.hidden = true;
  updateUrlState();
  setState(state.stats?.ready ? "idle" : "noIndex");
  dom.resultsSummary.textContent = "Enter a query to begin.";
  announce("Search cleared.");
  dom.input.focus();
}

dom.form.addEventListener("submit", submitSearch);
dom.clear.addEventListener("click", clearSearch);
dom.retry.addEventListener("click", () => submitSearch());
dom.resultCount.addEventListener("change", updateModeUI);
document.getElementById("advanced-settings")?.addEventListener("toggle", (event) => {
  if (event.currentTarget.open) event.currentTarget.classList.add("open");
  else event.currentTarget.classList.remove("open");
});

for (const input of document.querySelectorAll('input[name="mode"], input[name="precision"]')) {
  input.addEventListener("change", () => {
    const previousMode = state.mode;
    updateModeUI();
    if (dom.input.value.trim() && (state.mode !== previousMode || state.hasSearched)) {
      submitSearch();
    }
  });
}

dom.examples.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-query]");
  if (!button || button.disabled) return;
  const requestedMode = button.dataset.mode;
  if (requestedMode) {
    const modeInput = document.querySelector(
      `input[name="mode"][value="${requestedMode}"]`,
    );
    if (modeInput && !modeInput.disabled) modeInput.checked = true;
  }
  dom.input.value = button.dataset.query || "";
  dom.clear.hidden = false;
  updateModeUI();
  submitSearch();
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const editing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target?.isContentEditable;
  if (event.key === "/" && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault();
    dom.input.focus();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    dom.input.focus();
    dom.input.select();
  }
  if (event.key === "Escape" && document.activeElement === dom.input && dom.input.value) {
    clearSearch();
  }
});

async function bootstrap() {
  const ready = await loadStats();
  if (!ready) return;
  const params = new URLSearchParams(window.location.search);
  const query = (params.get("q") || "").trim();
  if (!query) return;

  const modeParam = params.get("mode");
  const precisionParam = params.get("precision");
  const kParam = params.get("k");
  const requestedMode = ["lexical", "semantic", "hybrid"].includes(modeParam) ? modeParam : null;
  const requestedPrecision = ["float", "int8", "binary"].includes(precisionParam)
    ? precisionParam
    : null;
  const requestedK = ["5", "10", "20", "30"].includes(kParam) ? kParam : null;
  const modeInput = requestedMode
    ? document.querySelector(`input[name="mode"][value="${requestedMode}"]`)
    : null;
  const precisionInput = requestedPrecision
    ? document.querySelector(`input[name="precision"][value="${requestedPrecision}"]`)
    : null;
  if (modeInput && !modeInput.disabled) modeInput.checked = true;
  if (precisionInput && !precisionInput.disabled) precisionInput.checked = true;
  dom.input.value = query;
  dom.clear.hidden = false;
  if (requestedK) dom.resultCount.value = requestedK;
  updateModeUI();
  await submitSearch();
}

bootstrap();
