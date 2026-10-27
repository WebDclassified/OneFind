// Theme (light / dark / system) and the "Reduce motion" preference.

import { emit, storage } from "./store.js";

const THEME_KEY = "onefind.theme";
const MOTION_KEY = "onefind.motion";
export const THEMES = ["system", "light", "dark"];
const THEME_COLORS = { light: "#F7F6F3", dark: "#161514" };

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

let themePreference = "system";

export function themePref() {
  return themePreference;
}

export function resolvedTheme(pref = themePreference) {
  if (pref === "light" || pref === "dark") return pref;
  return darkQuery.matches ? "dark" : "light";
}

function applyTheme() {
  const theme = resolvedTheme();
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.dataset.themePref = themePreference;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", THEME_COLORS[theme]);
  emit("theme", { pref: themePreference, theme });
}

export function setTheme(pref) {
  themePreference = THEMES.includes(pref) ? pref : "system";
  if (themePreference === "system") storage.remove(THEME_KEY);
  else storage.set(THEME_KEY, themePreference);
  applyTheme();
}

export function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(themePreference) + 1) % THEMES.length];
  setTheme(next);
  return next;
}

export function userReducedMotion() {
  return storage.get(MOTION_KEY) === "reduced";
}

function applyMotion() {
  const reduced = userReducedMotion() || motionQuery.matches;
  document.documentElement.dataset.motion = reduced ? "reduced" : "full";
  emit("motion", { reduced });
}

export function setReducedMotion(reduced) {
  if (reduced) storage.set(MOTION_KEY, "reduced");
  else storage.remove(MOTION_KEY);
  applyMotion();
}

export function initTheme() {
  const stored = storage.get(THEME_KEY);
  themePreference = THEMES.includes(stored) ? stored : "system";
  applyTheme();
  applyMotion();
  darkQuery.addEventListener?.("change", () => {
    if (themePreference === "system") applyTheme();
  });
  motionQuery.addEventListener?.("change", applyMotion);
}
