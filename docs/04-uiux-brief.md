# 04 · UI/UX Specification

Project: OneFind · Version: 1.1 · Status: Implemented

## Design direction

Calm, precise, private, and fast. The interface should feel like a focused local database tool rather than a marketing site. It uses system fonts, CSS tokens, inline vector symbols, and no external assets.

## Visual system

| Token | Light | Dark role |
|---|---|---|
| Primary | `#5754d9` | `#a7a4ff` |
| Surface | `#ffffff` | `#191c28` |
| Background | `#f6f7fb` | `#10121a` |
| Main text | `#151827` | `#f4f5fa` |
| Secondary text | `#62697c` | `#b1b6c7` |
| Border | `#dfe2ec` | `#303646` |
| Highlight | warm yellow | readable dark contrast |

Radii range from 10–24 px. Shadows are restrained. Dark mode follows `prefers-color-scheme`; forced-colors and reduced-motion preferences are handled.

## Layout

1. Sticky compact header with product, on-device badge, and readiness status.
2. Hero with plain-language value proposition and index facts.
3. Prominent search panel with explicit Search and Clear actions.
4. Retrieval-mode and precision radio groups.
5. Result-count control and mode/precision explanation.
6. Example query chips.
7. Dedicated results region with summary and explicit states.
8. Native ordered result list with rank, title, metadata, snippet, and labeled score.

Maximum content width is 1060 px. The layout becomes single-column below 800 px and uses compact controls below 620 px.

## Interaction rules

- Form submission occurs only through the explicit Search action.
- Mode/precision changes rerun a nonblank query once.
- In-flight requests are aborted and guarded by a monotonically increasing request ID.
- Loading disables the input and exposes a truthful status.
- `/` focuses search outside editable controls; `Ctrl/Cmd+K` always focuses it; Escape clears a focused query.
- Capability-aware controls disable unsupported semantic modes and precisions.
- Current query/mode/precision/k are reflected in shareable URL query parameters and restored after capabilities load.
- A blank query returns to idle without sending a request.

## Content safety

- No response-derived `innerHTML` or `insertAdjacentHTML` is used.
- Titles, IDs, sources, snippets, errors, and score metrics use `textContent`.
- Highlights are rendered only from validated server-side segments.
- Untrusted angle brackets, event handlers, emoji, bidi text, and long unbroken strings remain visible text and wrap safely.

## Accessibility

- Native form, radio, fieldset, ordered-list, article, and heading semantics.
- Visible keyboard focus and minimum 40–54 px interactive targets.
- Concise live announcements rather than reading the whole result list.
- `aria-busy` during loading.
- Decorative SVGs and skeletons hidden from assistive technology.
- Status never relies on color alone.
- Touch and mobile layouts preserve 44 px controls where practical.

## Security headers

The server applies a restrictive same-origin CSP, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a restrictive browser Permissions Policy. HTML, CSS, and JavaScript are separate same-origin package assets.

## Result semantics

Raw scores are not described as confidence. Each result includes the backend metric name: BM25 score, cosine similarity, negative Hamming distance, or RRF fusion score. Sources are displayed as metadata rather than synthesized as links.

## Responsive and print behavior

Long text wraps with `overflow-wrap: anywhere`. Header and result metadata truncate safely where appropriate. Print output removes controls and preserves each result card without breaking across pages.
