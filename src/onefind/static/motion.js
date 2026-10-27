// One motion language for the whole app. CSS owns the curves and
// durations (--ease-out, --dur-1/2/3); this module only decides *what*
// animates and keeps everything instant in reduced-motion mode.

export const STAGGER_STEP = 30;
export const STAGGER_MAX = 5;

export function reducedMotion() {
  return document.documentElement.dataset.motion === "reduced"
    || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function cssDuration(name, fallback) {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const value = Number.parseFloat(raw);
  if (!Number.isFinite(value)) return fallback;
  return raw.endsWith("ms") ? value : value * 1000;
}

/** The shared easing curve, read from the CSS token. */
export function cssEase() {
  return getComputedStyle(document.documentElement).getPropertyValue("--ease-out").trim() || "ease-out";
}

export const durations = () => ({
  short: cssDuration("--dur-1", 120),
  medium: cssDuration("--dur-2", 200),
  long: cssDuration("--dur-3", 320),
});

/** Restart a CSS enter animation by toggling a class. */
export function enter(element, className = "is-entering") {
  if (!element || reducedMotion()) return;
  element.classList.remove(className);
  // Force a style flush so the animation restarts.
  void element.offsetWidth;
  element.classList.add(className);
  element.addEventListener("animationend", () => element.classList.remove(className), { once: true });
}

/**
 * Play an exit animation (a CSS class), then call `done`. Instant in
 * reduced-motion mode. Returns a function that cancels the exit.
 */
export function leave(element, className, done) {
  if (!element || reducedMotion() || typeof element.getAnimations !== "function") {
    done?.();
    return () => {};
  }
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    window.clearTimeout(fallback);
    element.classList.remove(className);
    done?.();
  };
  element.classList.add(className);
  element.addEventListener("animationend", finish, { once: true });
  const fallback = window.setTimeout(finish, durations().long + 80);
  return () => {
    if (finished) return;
    finished = true;
    window.clearTimeout(fallback);
    element.removeEventListener("animationend", finish);
    element.classList.remove(className);
  };
}

/**
 * Staggered entry: the first STAGGER_MAX items enter one after another,
 * the rest arrive together with the last animated one.
 */
export function stagger(elements, { className = "enter", step = STAGGER_STEP, max = STAGGER_MAX } = {}) {
  if (reducedMotion()) return;
  elements.forEach((element, index) => {
    const delay = Math.min(index, max) * step;
    element.style.setProperty("--stagger", `${delay}ms`);
    element.classList.add(className);
  });
}

/** Record element positions keyed by data-key, for a FLIP reorder. */
export function capturePositions(elements, keyOf) {
  const positions = new Map();
  for (const element of elements) positions.set(keyOf(element), element.getBoundingClientRect().top);
  return positions;
}

/** Glide elements that moved from their previous position to the new one. */
export function flip(elements, before, keyOf) {
  if (reducedMotion() || !before.size || typeof Element.prototype.animate !== "function") return;
  const { medium } = durations();
  for (const element of elements) {
    const previousTop = before.get(keyOf(element));
    if (previousTop === undefined) continue;
    const delta = previousTop - element.getBoundingClientRect().top;
    if (Math.abs(delta) < 2) continue;
    element.animate(
      [{ transform: `translateY(${delta}px)` }, { transform: "translateY(0)" }],
      { duration: medium, easing: cssEase() },
    );
  }
}

/**
 * Apply a layout change (opening the side preview) as one smooth morph
 * instead of a one-frame jump, where the browser supports view transitions.
 * `update` may run asynchronously (on the next frame).
 */
export function withViewTransition(update) {
  if (reducedMotion() || typeof document.startViewTransition !== "function") {
    update();
    return null;
  }
  try {
    const transition = document.startViewTransition(update);
    // A skipped transition rejects these promises; that is not an error here.
    for (const promise of [transition.ready, transition.finished, transition.updateCallbackDone]) {
      promise?.catch?.(() => {});
    }
    return transition;
  } catch {
    update();
    return null;
  }
}

/** Page-load settle: header → search → panels fade in with a small rise. */
export function settlePage() {
  const root = document.documentElement;
  if (reducedMotion()) {
    root.classList.add("is-settled");
    return;
  }
  window.requestAnimationFrame(() => root.classList.add("is-settled"));
}
