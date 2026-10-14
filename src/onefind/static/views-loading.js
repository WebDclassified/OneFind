// The search pipeline: real engine stages streamed from /api/search/stream,
// shown as a sequence that resolves one step at a time, plus a skeleton of
// result cards. Display pacing is capped so results are never held back.

import { byId, el } from "./dom.js";
import { icon } from "./icons.js";
import { formatMs } from "./format.js";
import { reducedMotion } from "./motion.js";

/** Engine stage name -> displayed step. embed + semantic read as one step. */
const STAGE_GROUPS = {
  parse: "parse",
  lexical: "lexical",
  embed: "meaning",
  semantic: "meaning",
  fuse: "fuse",
  rerank: "rerank",
  build: "build",
};

export const STEP_LABELS = {
  model: "Loading the local model",
  parse: "Understanding query",
  lexical: "Keyword match",
  meaning: "Meaning match",
  fuse: "Combining",
  rerank: "Refining order",
  build: "Preparing results",
};

const STEP_ORDER = ["model", "parse", "lexical", "meaning", "fuse", "rerank", "build"];

const PREDICTED = {
  lexical: ["parse", "lexical", "build"],
  semantic: ["parse", "meaning", "build"],
  hybrid: ["parse", "lexical", "meaning", "fuse", "build"],
};

const STEP_INTERVAL = 70; // ms between visual step resolutions
export const MAX_SETTLE_MS = 300; // cap on any artificial display time

let steps = new Map(); // step -> { node, status, target, ms }
let queue = [];
let drainTimer = 0;

export function stepFor(stageName) {
  return STAGE_GROUPS[stageName] || null;
}

/** Group raw stage timings ([{name, ms}]) into displayed steps, in order. */
export function summarizeStages(stages) {
  const totals = new Map();
  for (const stage of Array.isArray(stages) ? stages : []) {
    const step = stepFor(stage?.name);
    if (!step) continue;
    totals.set(step, (totals.get(step) || 0) + (Number(stage.ms) || 0));
  }
  return STEP_ORDER.filter((step) => totals.has(step)).map((step) => ({
    step,
    label: STEP_LABELS[step],
    ms: totals.get(step),
  }));
}

function stepNode(step) {
  return el("li", { class: "pipeline-step", dataset: { step, status: "pending" } },
    el("span", { class: "pipeline-marker", attrs: { "aria-hidden": "true" } }, icon("check", "icon pipeline-check")),
    el("span", { class: "pipeline-label", text: STEP_LABELS[step] }),
    el("span", { class: "pipeline-ms" }),
    el("span", { class: "pipeline-status sr-only", text: "waiting" }),
  );
}

function ensureStep(step) {
  if (steps.has(step)) return steps.get(step);
  const entry = { node: stepNode(step), status: "pending", target: "pending", ms: 0 };
  steps.set(step, entry);
  const list = byId("pipeline");
  // Insert in canonical order so late-arriving steps (rerank) land correctly.
  const position = STEP_ORDER.indexOf(step);
  const before = [...list.children].find((child) => STEP_ORDER.indexOf(child.dataset.step) > position);
  list.insertBefore(entry.node, before || null);
  return entry;
}

function paint(step, status, ms) {
  const entry = ensureStep(step);
  if (entry.status === "done" && status !== "done") return;
  entry.status = status;
  entry.node.dataset.status = status;
  entry.node.querySelector(".pipeline-status").textContent = status === "done" ? "done" : status === "active" ? "in progress" : "waiting";
  if (status === "done") entry.node.querySelector(".pipeline-ms").textContent = formatMs(ms);
}

function drain() {
  drainTimer = 0;
  const next = queue.shift();
  if (!next) return;
  paint(next.step, next.status, next.ms);
  if (queue.length) drainTimer = window.setTimeout(drain, reducedMotion() ? 0 : STEP_INTERVAL);
}

const RANK = { pending: 0, active: 1, done: 2 };

function schedule(update) {
  // Each step only ever moves forward (pending -> active -> done), and a
  // status already on its way is not queued twice.
  const entry = steps.get(update.step);
  if (entry) {
    if (RANK[update.status] <= RANK[entry.target]) {
      if (update.status === "done") {
        const queued = queue.find((item) => item.step === update.step && item.status === "done");
        if (queued) queued.ms = update.ms;
      }
      return;
    }
    entry.target = update.status;
  }
  queue.push(update);
  if (!drainTimer) drainTimer = window.setTimeout(drain, 0);
}

function buildSkeleton(count = 3) {
  const items = [];
  for (let i = 0; i < count; i += 1) {
    // Shaped like a real card (title, path, snippet, footer) so results land without a jump.
    items.push(el("li", { class: "skeleton-card" },
      el("span", { class: "skeleton-line skeleton-title" }),
      el("span", { class: "skeleton-line skeleton-path" }),
      el("span", { class: "skeleton-line skeleton-gap" }),
      el("span", { class: "skeleton-line" }),
      el("span", { class: "skeleton-line skeleton-short" }),
      el("span", { class: "skeleton-line skeleton-foot" }),
    ));
  }
  byId("skeleton-list").replaceChildren(...items);
}

/** Reset the pipeline for a new search, predicting the steps for `mode`. */
export function startPipeline(mode, { title = "Searching your notes", loadModel = false } = {}) {
  cancelPipeline();
  steps = new Map();
  byId("pipeline").replaceChildren();
  byId("pipeline-title").textContent = title;
  byId("slow-notice").hidden = true;
  // The first meaning search loads the model before any stage starts.
  if (loadModel) ensureStep("model");
  for (const step of PREDICTED[mode] || PREDICTED.hybrid) ensureStep(step);
  const first = loadModel ? "model" : "parse";
  steps.get(first).target = "active";
  paint(first, "active", 0);
  buildSkeleton();
}

/** Drop queued step updates (the search ended before the pipeline was shown). */
export function cancelPipeline() {
  window.clearTimeout(drainTimer);
  drainTimer = 0;
  queue = [];
}

/** Feed one streamed stage event: {name, status: "start"|"done", ms}. */
export function stageEvent(event) {
  const step = stepFor(event?.name);
  if (!step) return;
  const entry = ensureStep(step);
  const model = steps.get("model");
  if (model && model.target !== "done") schedule({ step: "model", status: "done", ms: 0 });
  if (event.status === "start") {
    // Starting a later step means every earlier step has finished.
    for (const [other, info] of steps) {
      if (STEP_ORDER.indexOf(other) < STEP_ORDER.indexOf(step) && info.target === "active") {
        schedule({ step: other, status: "done", ms: info.ms });
      }
    }
    schedule({ step, status: "active", ms: 0 });
    return;
  }
  entry.ms += Number(event.ms) || 0;
  // "embed" finishing is only half of the meaning step.
  if (event.name === "embed") return;
  schedule({ step, status: "done", ms: entry.ms });
}

/** Mark predicted steps that never ran as done-or-removed before results. */
function finalize(stages) {
  const seen = new Set(summarizeStages(stages).map((item) => item.step));
  for (const [step, entry] of steps) {
    if (step === "model") {
      if (entry.target !== "done") schedule({ step, status: "done", ms: 0 });
      continue;
    }
    if (entry.status !== "done" && seen.size && !seen.has(step)) {
      entry.node.remove();
      steps.delete(step);
      queue = queue.filter((item) => item.step !== step);
    }
  }
  for (const item of summarizeStages(stages)) {
    const entry = steps.get(item.step);
    if (entry && entry.target !== "done") schedule({ step: item.step, status: "done", ms: item.ms });
  }
}

/**
 * Let the remaining queued steps resolve on screen, but never for longer
 * than `budget` ms. Resolves once everything is painted.
 */
export async function settlePipeline(stages, budget = MAX_SETTLE_MS) {
  finalize(stages);
  if (reducedMotion() || budget <= 0) {
    window.clearTimeout(drainTimer);
    drainTimer = 0;
    while (queue.length) drain();
    return;
  }
  const deadline = performance.now() + budget;
  while (queue.length && performance.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 30));
  }
  window.clearTimeout(drainTimer);
  drainTimer = 0;
  while (queue.length) drain();
}

export function showSlowNotice(visible) {
  byId("slow-notice").hidden = !visible;
}

/**
 * Per-step timings for the results header ("34 ms" opens them). Shown only
 * on request: after results arrive they are detail, not news.
 */
export function renderStageTrail(stages, totalMs = null) {
  const trail = byId("stage-trail");
  const summary = summarizeStages(stages);
  const items = summary.map((item) => el("li", { class: "trail-step" },
    icon("check", "icon trail-check"),
    el("span", { class: "trail-label", text: item.label }),
    el("span", { class: "trail-ms", text: formatMs(item.ms) }),
  ));
  trail.replaceChildren(...items);
  const details = byId("stage-details");
  const total = Number.isFinite(totalMs) ? totalMs : summary.reduce((sum, item) => sum + item.ms, 0);
  details.hidden = items.length === 0 && !Number.isFinite(totalMs);
  const label = byId("stage-total");
  label.textContent = formatMs(total) || "Timing";
  label.setAttribute("aria-label", `Search took ${formatMs(total) || "a moment"}. Show the steps`);
  if (!items.length) details.open = false;
}
