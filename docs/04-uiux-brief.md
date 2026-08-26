# 04 · UI/UX Brief

Project: OneFind reproduction · Version: v0.1 (draft) · Status: Proposed
Scope: two surfaces — CLI output style (primary) and a minimal local demo page (Phase 6).

## Design direction

Three adjectives: **calm, dense, precise.**
Should feel like: a well-made database tool — numbers aligned, nothing decorative.
Must not feel like: a marketing site or a toy demo.

## CLI rules (primary surface)

- Progress over silence: any operation >1 s shows progress (docs indexed / configs done).
- Results as aligned tables: rank, id, score, truncated snippet; scores to 4 decimals.
- Errors: `error:` prefix, one-line cause + one-line fix; never stack traces unless `--verbose`.
- No emoji in default output; color optional and auto-disabled when not a TTY.

## Demo page tokens

| Token | Value |
|---|---|
| Surface | `#FFFFFF`, secondary `#F6F7F9` |
| Text | `#111827` primary, `#6B7280` secondary |
| Primary action | Indigo `#4F46E5` (white text) |
| Danger | `#DC2626` |
| Focus ring | 2px indigo outline offset 1px |
| Type | system-ui stack; results body 14px, monospace for scores/ids |
| Spacing | 4px base scale; cards radius 12px, inputs 8px |

## Component rules

- Buttons: one primary per view; mode toggle is a segmented control (radio semantics).
- Query input: always visible at top; `/` focuses it from anywhere.
- Results list: max width ~72ch; snippet highlights `<mark>` with yellow-100 background.
- Loading: input disabled + inline spinner; skeleton rows after 300 ms (respects reduced-motion).
- Empty/no-results/error states exactly as specified in doc 03 table.

## Responsive & accessibility

- Breakpoint: single column below 720px; toggle wraps above input.
- Keyboard: full tab order; Enter submits; `/` shortcut; visible focus everywhere.
- Contrast target: WCAG 2.2 AA for all text (≥4.5:1).
- Results count announced via `aria-live="polite"`.
- Touch targets ≥44px on mobile.

## References → principles (not clones)

Linear-style restraint (density + calm), Google-search familiarity for the query box, terminal-inspired monospace scores. Do not copy any product's assets or layout wholesale.
