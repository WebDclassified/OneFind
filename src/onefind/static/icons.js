// Stroke icons on a 24px grid. Every icon is decorative (aria-hidden);
// controls always carry a visible text label or an aria-label.

const SVG_NS = "http://www.w3.org/2000/svg";

const CIRCLE = "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z";

const PATHS = {
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m20 20-4.2-4.2"],
  x: ["M6 6l12 12", "M18 6 6 18"],
  "chevron-down": ["m6 9 6 6 6-6"],
  "chevron-right": ["m9 6 6 6-6 6"],
  "chevron-left": ["m15 6-6 6 6 6"],
  "arrow-up": ["M12 19V5", "m5 12 7-7 7 7"],
  "arrow-right": ["M5 12h14", "m13 6 6 6-6 6"],
  folder: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
  file: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5"],
  open: ["M14 4h6v6", "M20 4l-9 9", "M19 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h4"],
  eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"],
  copy: [
    "M9 9h10a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1z",
    "M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
  ],
  bookmark: ["M6 3h12v18l-6-4-6 4z"],
  filter: ["M3 5h18l-7 8v6l-4 2v-8z"],
  help: [CIRCLE, "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.7", "M12 17h.01"],
  check: ["m5 12.5 4.5 4.5L19 7"],
  "check-circle": [CIRCLE, "m8 12.5 3 3 5-6"],
  alert: ["M12 3 21.5 20h-19z", "M12 10v4", "M12 17h.01"],
  info: [CIRCLE, "M12 11v5", "M12 8h.01"],
  refresh: ["M20 11a8 8 0 0 0-14.7-4.4L4 8", "M4 4v4h4", "M4 13a8 8 0 0 0 14.7 4.4L20 16", "M20 20v-4h-4"],
  sun: [
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z",
    "M12 2v2", "M12 20v2", "m4.9 4.9 1.4 1.4", "m17.7 17.7 1.4 1.4",
    "M2 12h2", "M20 12h2", "m4.9 19.1 1.4-1.4", "m17.7 6.3 1.4-1.4",
  ],
  moon: ["M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"],
  monitor: ["M3 5h18v11H3z", "M8 20h8", "M12 16v4"],
  plus: ["M12 5v14", "M5 12h14"],
  database: [
    "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z",
    "M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6",
    "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  ],
  download: ["M12 4v11", "m7 10 5 5 5-5", "M5 20h14"],
  star: ["m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"],
  clock: [CIRCLE, "M12 7v5l3 2"],
  trash: ["M4 7h16", "M10 11v6", "M14 11v6", "m6 7 1 13h10l1-13", "M9 7V4h6v3"],
  shield: ["M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"],
  sparkle: ["m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"],
  stop: [CIRCLE, "m9 9 6 6", "m15 9-6 6"],
  tag: ["M3 12V4h8l10 10-8 8z", "M7.5 7.5h.01"],
  wrench: ["M15 4a5 5 0 0 0-4.6 6.9L4 17.3V20h2.7l6.4-6.4A5 5 0 0 0 20 9l-3 3-3-1-1-3z"],
  compress: ["M4 14h6v6", "M20 10h-6V4", "m14 10 7-7", "m3 21 7-7"],
  pencil: ["M4 20h4L19 9l-4-4L4 16z", "m13.5 6.5 4 4"],
  history: ["M3 12a9 9 0 1 0 3-6.7L3 8", "M3 3v5h5", "M12 7v5l3 2"],
  upload: ["M12 20V9", "m7 14 5-5 5 5", "M5 4h14"],
  settings: [
    "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
    "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  ],
  "folder-open": ["M3 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v1", "M3 7v10a2 2 0 0 0 2 2h12.5a2 2 0 0 0 1.9-1.4L21.5 12a1 1 0 0 0-1-1.3H8.6a2 2 0 0 0-1.9 1.4L4.4 19"],
  "rotate-ccw": ["M3 12a9 9 0 1 0 2.6-6.4L3 8", "M3 3v5h5"],
  "rotate-cw": ["M21 12a9 9 0 1 1-2.6-6.4L21 8", "M21 3v5h-5"],
  more: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  blend: ["M9 6a6 6 0 1 0 0 12 6 6 0 0 0 0-12z", "M15 6a6 6 0 1 0 0 12 6 6 0 0 0 0-12z"],
};

/**
 * Build an inline SVG icon. Returns an element that is hidden from
 * assistive technology; pair it with visible text or an aria-label.
 */
export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.setAttribute("class", className);
  for (const d of PATHS[name] || PATHS.info) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

export function hasIcon(name) {
  return Object.hasOwn(PATHS, name);
}
