# OneFind — Product Vision, Real-Life Use Case, and Full Feature Specification

> **Purpose:** A complete end-to-end feature and UX specification for turning OneFind from a strong retrieval reproduction into a polished, professional local search tool for real daily use.
> **Scope:** Product direction, real-life use cases, engine features, UI/UX behavior, animation and process design, data/portability, accessibility, and implementation-ready acceptance criteria.
> **Out of scope for this document:** Implementation code, API keys, hosted models, and any external AI service. This project stays local-first and model-free by default.

---

## 1. Product vision

OneFind should feel like a **professional local search application** that helps a real person find their own notes, documents, and local knowledge quickly, privately, and without cloud dependency.

The product is not just “hybrid search in SQLite.” The product is:

- a **fast, private local search tool** for personal notes and local documents,
- a **single-file portable collection** the user can back up, move, and reopen,
- a **calm, keyboard-friendly interface** that feels complete rather than experimental,
- a **trustworthy engine** that explains what matched and why, instead of only showing a ranked list,
- a **professional process experience** where every stage of a search feels intentional, responsive, and animated in a polished way.

The guiding product principle:

> Make local search feel like a finished tool for real content, not like a demo of a retrieval technique.

---

## 2. Real-life use cases to optimize for

### 2.1 Primary target: personal notes and local knowledge base

A user has a folder of Markdown/text notes, daily logs, meeting notes, saved ideas, reference snippets, and project drafts. They want to:

- find a note by meaning, not only exact wording,
- find a note by keyword when they remember a term,
- browse recent notes and recent searches,
- open the file after finding it,
- trust that the content stays local and private,
- keep the index in one portable file.

This is the strongest default persona because the corpus already exists for most users and the pain is real.

### 2.2 Secondary target: local document archive

A user keeps reference material, policies, saved articles, research notes, specs, or project docs locally and wants:

- search across many documents,
- filter by folder, source, or date where possible,
- see meaningful snippets instead of cryptic fragments,
- maintain the index as the archive grows and changes.

### 2.3 Tertiary target: project workspace search

A user has one or more project folders with notes, drafts, logs, and reference files and wants quick local lookup without setting up a separate search server.

### 2.4 What OneFind is not trying to be

- Not an internet-scale search engine.
- Not a multi-user authenticated cloud service.
- Not a GPU-dependent pipeline.
- Not a hosted API consumer.

This matters because the product should proudly fit its strengths: small/medium local corpora, privacy, simplicity, and portability.

---

## 3. Product principles

1. **Search first, setup second.** The user should feel the product is a search tool immediately.
2. **Real content over synthetic content.** Defaults, demos, and UI language should resemble real notes and documents.
3. **Plain state.** Indexed, stale, missing vectors, no results, error — each should be understandable and actionable.
4. **Fast and calm.** Local tools win when they feel responsive and never noisy.
5. **Explain the match.** Users should see why a result appeared.
6. **Actionable results.** Finding is only half the job; opening, copying, filtering, and saving matter too.
7. **Portability is a feature.** One collection file should be easy to back up, move, and reopen.
8. **Graceful degradation.** If semantic vectors are missing, lexical still works and the UI explains the next step.
9. **Honest boundaries.** The tool should not pretend to be something it is not.

---

## 4. Core engine feature spec

### 4.1 Indexing and corpus management

**Goal:** Make ingestion and index maintenance feel like a normal app workflow, not a one-time script.

#### Supported inputs
- Folders of supported text files.
- Standalone text files.
- Local JSONL datasets when relevant.

#### Canonical document identity
- Use stable document IDs so the same document can be updated without losing identity.
- Keep source attribution visible in the UI so the user knows where a result came from.

#### Indexing workflow
- Pick a folder or file.
- Show indexing progress: files found, documents processed, and when relevant, embedding/vector build progress.
- After indexing, report:
  - documents indexed,
  - vectors present or missing,
  - health status,
  - approximate collection size,
  - whether semantic/hybrid is available.

#### Incremental and partial maintenance
- Support reindexing only what changed when possible.
- Support reindexing a subfolder or selected files when the user asks.
- Support excluding paths or file patterns the user does not want indexed.
- Support removing a document from the index without deleting the source file.

#### Multiple collections
- Allow more than one named collection/profile.
- Examples: “Notes,” “Work,” “Project X.”
- Make switching collections easy and visible.

#### Dedup and near-dup awareness
- Detect exact duplicate document IDs and handle them cleanly.
- Optionally highlight near-duplicate content so the user can understand repeated material.

#### Acceptance criteria
- Indexing shows meaningful progress, not only a final success message.
- The user can tell whether vectors are present and whether hybrid search is live.
- Empty folders, unsupported files, and missing paths produce clear guidance.
- Reindexing is understandable and does not feel dangerous.

---

### 4.2 Retrieval behavior

**Goal:** Make retrieval useful for how people actually remember things.

#### Modes
- **Keyword/lexical:** term-based matching with strong ranking.
- **Semantic:** meaning-based matching when vectors are available.
- **Hybrid:** combined ranking chosen as the default for note-like content.

#### Precision options
- Where vector storage supports it, allow precision choices such as float/int8/binary.
- Present these as advanced options, not as the main interface.

#### Result ranking and clarity
- Strongest matches first.
- Deterministic, stable ordering for the same query and index state.
- Result count control.
- Optional deeper candidate pool when the user wants broader exploration.

#### Snippets and context
- Show meaningful snippet context around the match.
- Avoid brittle fragments that confuse the user.
- When possible, give enough surrounding context that the user can decide whether the result is useful before opening it.

#### Filtering and refinement
- Filter by source path/folder where applicable.
- Filter by file type where applicable.
- Filter by date metadata where available.
- Filter by user tags/metadata where supported.

#### Saved and recent queries
- Keep a recent query history so repeat searches are fast.
- Allow saving useful queries for later.

#### “Why this result” explainability
- Show what contributed to the match:
  - which terms matched in lexical mode,
  - that semantic similarity drove the result in semantic mode,
  - that hybrid combined both in hybrid mode,
  - which precision was used when relevant.
- Keep the explanation short and readable.

#### No-match and abstention behavior
- If there are no results, explain why in plain language.
- Suggest next actions: check spelling, try different terms, switch mode, reindex if needed.
- If the query truly has no relevant content, do not force a misleading result.

#### Acceptance criteria
- Hybrid is the sensible default for note-like corpora.
- Results feel stable and understandable.
- Snippets help the user decide, not confuse them.
- No-result and low-confidence states are honest and useful.

---

### 4.3 Performance and scalability behavior

**Goal:** Keep the tool feeling fast and practical as the corpus grows.

#### Responsive search
- Debounce input appropriately.
- Cancel stale requests when a newer query is typed.
- Show intermediate state quickly instead of leaving the UI frozen.

#### Indexing performance
- Batch work appropriately during indexing.
- Show progress for long operations.
- Do not make the user guess whether the app is working.

#### Vector and memory behavior
- Do not load unnecessary data into memory.
- Use bounded approaches for large vector workloads where the engine already supports them.
- Keep the UI responsive during heavier operations.

#### Scaling expectations
- Be honest about corpus size comfort zones.
- If a collection is getting large, surface that clearly and suggest practical actions.

#### Acceptance criteria
- Typing a query feels live and responsive.
- Long operations show progress and do not appear dead.
- The product communicates practical limits without alarm.

---

### 4.4 Reliability, health, and maintenance

**Goal:** Make the app feel safe to keep around long term.

#### Health view
- Show document count, vector count, consistency state, and overall readiness.
- Make health understandable, not just numerical.

#### Graceful degradation
- If vectors are missing, lexical search still works.
- If hybrid is requested but unavailable, explain what is missing and how to enable it.
- If the index seems stale or inconsistent, tell the user plainly.

#### Maintenance actions
- Backup the collection file.
- Restore or open a backup copy.
- Reindex a folder or the current collection.
- Reset/clear the current collection with confirmation.
- Integrity check or repair flow where appropriate.
- Compact or clean up when useful.

#### Error handling
- Translate internal errors into human actions.
- Do not expose raw internal details in the main UI unless the user specifically wants diagnostics.
- Provide retry or next-step guidance where possible.

#### Acceptance criteria
- The user can understand the health of a collection at a glance.
- Maintenance actions are safe and clearly labeled.
- Errors lead to actions, not dead ends.

---

## 5. UI/UX specification

### 5.1 Overall feel

The UI should feel like a **professional local app**, not a research prototype. The tone is:

- calm,
- fast,
- clear,
- spacious but not empty,
- trustworthy,
- designed around the user’s content.

The interface should be self-explanatory on first launch and efficient for repeat use.

---

### 5.2 Main screen layout

#### Top area
- App identity and current collection name.
- Collection switcher when multiple collections exist.
- Health/status line: indexed, stale, vectors missing, ready, etc.

#### Primary search area
- A single, prominent search box.
- Placeholder text that changes with state:
  - before indexing: “Index a folder to start searching your notes.”
  - after indexing: “Search your notes by keyword or meaning.”
- Search mode/precision controls placed nearby but not dominant.
- A clear button to clear the query and return to idle state.

#### Middle area
- Results list when a query is active.
- Empty state when there is no query and no contextual content to show.
- Recent searches and recent documents when idle and useful.

#### Bottom or side panel
- Optional filters: source/folder, file type, date, tags.
- Optional “why this result” explanation for the focused result.
- Optional result actions: open file, copy snippet, save/bookmark, export selection.

#### Result card design
Each result should show:
- Title or document name.
- Source path/filename in a readable form.
- Date or other available metadata when useful.
- A snippet with the matched context.
- A short score or relevance indicator when helpful.
- Action controls: open, copy, save, filter, explain.

#### Idle state
When no query is active, the screen should still feel complete:
- Current collection status.
- Recent searches.
- Recent documents.
- Quick start action: index a folder, open a collection, or switch collections.

#### No-index state
If no collection is loaded or indexed:
- Plain message: “No index ready.”
- Action: “Index a folder” or “Open a collection.”
- Do not show fake results or empty search space without context.

---

### 5.3 Search workflow states

Every search should pass through clear states:

1. **Idle** — no active query.
2. **Typing/debouncing** — user is typing; UI is ready but not yet submitted.
3. **Loading** — query is being processed.
4. **Results** — results are shown.
5. **No results** — honest empty state with guidance.
6. **Error** — something went wrong; message and next action are shown.

The UI must never feel like it forgot what the user was doing between these states.

---

### 5.4 Result interaction

- Clicking a result opens the file or shows more context.
- Keyboard navigation should move through results.
- Focus management should be clean and predictable.
- Actions on a result should be easy to discover but not intrusive.
- Highlighting in snippets should be readable and not garish.

---

### 5.5 Accessibility

- Strong text contrast and readable type scale.
- Clear focus indicators.
- Keyboard accessible controls and results.
- Labels plus icons, not icons alone.
- Resizable text and tolerant layouts.
- Minimal motion by default; respect user motion preferences.
- Accessible names for interactive elements.
- Avoid relying only on color to communicate meaning.

---

### 5.6 Dark and light modes

- Provide both dark and light themes.
- Make both feel finished, not like afterthoughts.
- Use color subtly for state and emphasis.
- Ensure snippet highlights remain readable in both themes.
- Respect system preference and allow manual override if appropriate.

---

### 5.7 Window and layout behavior

- Layout should adapt to window resizing.
- Side panels and filters should collapse or relocate gracefully.
- Mobile or narrow-window layouts should remain usable.
- The app should not break or overlap when resized.

---

## 6. Animation and process experience specification

This section is the heart of the “professional software” request. The goal is not flashy motion; it is **communicative, polished motion** that makes the search process feel deliberate and alive.

---

### 6.1 Animation philosophy

Animations should do four jobs:

1. **Explain state change.** The user should always understand what is happening.
2. **Connect steps.** Movement should show how one stage leads to the next.
3. **Reassure.** Loading and processing should feel controlled and deliberate.
4. **Stay restrained.** Motion should be smooth, quick, and professional, not decorative for its own sake.

General rules:
- Prefer short, purposeful transitions over long spectacle.
- Keep enter/exit motion consistent across the app.
- Use the same easing language everywhere.
- Avoid motion that delays the user.
- Respect reduced-motion preferences.
- Never animate so much that results feel delayed or playful in an unprofessional way.

---

### 6.2 Idle → typing → search flow

#### Idle state entrance
- The home screen elements should settle in gently on load.
- Collection status, search box, and sidebar/panel should appear in a logical order.
- The motion should be subtle: a small fade and upward/slight slide settle, not a dramatic reveal.

#### Typing state
- As the user types, the search box should feel immediate.
- Optional micro-feedback while typing: a very subtle readiness cue, not a loading spinner.
- The interface should look prepared for results without pretending results are already available.

#### Query submission
- When a query is submitted, the interface should transition cleanly into loading.
- The search box can keep focus; the results area transitions from idle content to loading state.
- Avoid abrupt disappearance of previous content unless that is the clearest choice for the situation.

---

### 6.3 Loading and processing animation

Loading should communicate: “We are working on your search, this is normal, and results are coming.”

#### Search loading state
- Show a calm, continuous indicator in the results area.
- Prefer a refined pulse, progress ring, or structured skeleton over a frantic spinner.
- If the engine can report stages, show them in order:
  - parsing the query,
  - running the lexical leg,
  - running the semantic leg when applicable,
  - fusing results when applicable,
  - building the result cards.

#### Stage-based processing visualization
If the engine exposes stages, animate them as a **sequential process**:

1. Query stage appears and resolves.
2. Next stage appears and resolves.
3. Final results stage resolves into results.

This should feel like a professional pipeline, not a random animation. Each stage should:
- appear smoothly,
- finish cleanly,
- not overlap in a confusing way,
- leave enough time to read if the user is watching.

#### Skeleton/results placeholder
While loading, the results area can show a refined skeleton:
- result card shapes,
- snippet lines,
- source line,
- subtle shimmer or pulse if appropriate,
- no fake content that might be mistaken for real results.

#### Loading fail-safe
If loading takes longer than expected:
- keep the indicator calm,
- do not repeatedly flash new animations,
- allow cancellation if the UI supports it,
- preserve the query so the user can retry easily.

---

### 6.4 Results arrival animation

When results arrive, they should feel like a **natural resolution**, not a sudden dump.

#### Staged result entry
- Results can enter in a controlled sequence or small group sequence.
- Each result card fades/slides into place with consistent timing.
- Do not animate every item as if it were equally important; keep motion subtle.

#### Content settling
- Snippet highlights can appear after the card has settled, so the UI reads as “result arrived, then highlights activate.”
- Score/relevance indicators can fade in.
- Metadata can appear in a second subtle pass.

#### Empty-to-results transition
- If the previous state was “no results” or idle, the new results should replace that state cleanly.
- Avoid jarring layout shifts; reserve space and transition gracefully.

#### If results are later/refreshed
- If results update due to filters or re-entry, animate only what changed where possible.
- Keep updates restrained: fade, relocate, or reorder smoothly rather than redrawing everything chaotically.

---

### 6.5 No-results and error animation

#### No results
- Show the no-results state with a calm transition from loading.
- Use clear copy: “No matches found,” plus practical guidance.
- Keep the search box available and focused so the user can try again immediately.

#### Error state
- Transition from loading to error without shocking motion.
- Show a clear error message and next action: retry, change mode, reindex, check health, etc.
- Keep the query intact when possible so the user does not have to retype.

---

### 6.6 Indexing and setup animations

Indexing and vector building are longer operations, so the animation needs to communicate **progress and completion**, not just activity.

#### Index setup flow
- Step 1: choose folder/file/collection.
- Step 2: indexing progress.
- Step 3: vector/build progress when applicable.
- Step 4: readiness confirmation.

#### Progress visualization
- Use a clear progress indicator with meaningful labels.
- Show countable progress when possible: files processed, documents indexed, vectors built.
- If a stage is waiting on another, show that relationship visually rather than only with text.

#### Completion state
- On completion, the UI should settle into a “ready” state with a calm confirmation animation.
- The confirmation should be brief and professional: a subtle check, status change, or gentle transition into the now-ready search screen.

#### Interrupted or failed indexing
- If indexing is interrupted or fails, show a clear state and next action.
- Keep the previous collection state intact where possible; do not leave the UI in an ambiguous half-state.

---

### 6.7 Micro-interactions and polish

These small details make the app feel professional:

- Buttons and controls have a crisp press/passive state change.
- Focus moves logically after actions.
- Panels open/close with smooth, consistent motion.
- Filters activate with a short clear transition.
- Recent searches and results reorder calmly when changed.
- Tooltip/help text appears with subtle motion and disappears cleanly.
- Scroll and layout changes should not feel jumpy.

---

### 6.8 Reduced-motion and accessibility exceptions

- Respect system or user reduced-motion settings.
- In reduced-motion mode, keep state changes clear but remove decorative timing.
- Ensure the process is still understandable without animation — animation should enhance, not carry, the explanation.

---

### 6.9 Animation acceptance criteria

- The search process feels staged and intentional.
- Loading communicates progress, not mystery.
- Results appear with controlled, professional motion.
- Indexing/ready flows feel complete and reassuring.
- All motion remains subtle, consistent, and accessible.
- The app still works perfectly without animation.

---

## 7. Real-life workflow features

### 7.1 Opening and acting on results
- Open result in the system’s default handler or chosen editor.
- Open result to a relevant location if the app can identify one.
- Copy snippet or copy result summary.
- Save/bookmark result or query for later.
- Export a small selection of results when useful.

### 7.2 Recent and repeated use
- Recent searches.
- Recent documents.
- Saved queries.
- Quick re-run of a previous search.

### 7.3 Organization support
- Collection/profile switching.
- Folder/source awareness in results.
- Optional user tags/metadata.
- Exclusion rules for noise the user does not want searched.

### 7.4 Daily-use comfort
- Fast entry point to search.
- Keyboard-first flow.
- Stable layout and predictable behavior.
- Minimal setup for repeat use.

---

## 8. Data, portability, and privacy story in the UI

### 8.1 Single-file collection
- Make the “one collection file” idea visible and understandable.
- Show where the collection lives if the user wants to know.
- Let the user back up the collection easily.

### 8.2 Backup and restore
- Backup creates a copy the user can keep.
- Restore/open a backup should be simple and safe.
- The UI should explain what backup/restore does in plain language.

### 8.3 Privacy messaging
- Make it clear that the collection is local and private to the user’s machine.
- Do not imply cloud processing unless the product actually does that, which this specification does not assume.

### 8.4 Portability actions
- Move/copy a collection between machines.
- Reopen a collection from a new location.
- Understand what travels with the collection and what does not.

---

## 9. Onboarding and first-run experience

### 9.1 First launch
- Welcome the user with a short, clear explanation of what the app does.
- Offer the most common starting action first: index a folder.
- Show the current state honestly: no collection yet, or existing collection ready.

### 9.2 Guided first index
- If the user is new, guide them through:
  1. choosing a folder,
  2. starting indexing,
  3. waiting for progress,
  4. seeing the ready state,
  5. trying a sample search or their own query.

### 9.3 Empty-state teaching
- Use empty states to teach, not to stare.
- Suggest what to do next in one or two short lines.
- Keep suggestions actionable: index, search, switch collection, open a file.

---

## 10. Acceptance criteria for the whole product

### 10.1 Functional acceptance
- The user can index a local folder and search it.
- The user can switch between lexical, semantic, and hybrid where supported.
- The user can see whether vectors are available.
- The user can filter and refine results where data allows.
- The user can open, copy, and act on results.
- The user can back up, restore, reindex, and maintain a collection safely.
- The app degrades gracefully when vectors or other features are unavailable.

### 10.2 UX acceptance
- The app feels calm, fast, and complete.
- States are always clear: idle, typing, loading, results, no results, error.
- The search flow feels like a professional process, not a raw tool.
- Motion is purposeful, consistent, and accessible.
- Results feel like the user’s content, not a demo trick.

### 10.3 Real-life fit acceptance
- A real user with a folder of notes or local documents can use the app without conceptual effort.
- The app helps the user find, judge, and open content.
- The app is pleasant to return to, not just impressive once.

---

## 11. Suggested implementation order

### Phase 1 — Foundation for real use
- Clean collection/state model in the UI.
- Clear idle, loading, results, no-result, and error states.
- Basic search flow with smooth transitions.
- Health/readiness visibility.

### Phase 2 — Professional motion and process
- Staged search loading.
- Controlled result entry animations.
- Calm indexing progress and completion flow.
- Consistent motion language across the app.

### Phase 3 — Workflow completeness
- Recent searches and recent documents.
- Saved queries.
- Result actions: open, copy, save, export where appropriate.
- Filters and source awareness.

### Phase 4 — Polish and accessibility
- Dark/light themes.
- Keyboard flow and focus management.
- Reduced-motion support.
- Responsive layout and more resilient empty/error states.

### Phase 5 — Long-term care
- Backup/restore UX.
- Maintenance actions in the UI.
- Better scalability messaging.
- Stronger onboarding and first-run guidance.

---

## 12. What “best working project” means in practice

For this project to feel like the best version of itself, it should:

- solve a real daily searching problem for a real user,
- stay local, private, and portable by default,
- present hybrid search as a practical everyday tool,
- make the search process feel professional through motion and state design,
- make results actionable,
- make maintenance and portability easy,
- feel complete rather than experimental.

That combination is what turns a strong technical project into a genuinely useful one.

---

## 13. Notes for the implementing agent

- Treat animation as communication first, decoration second.
- Keep every state explainable without motion, then enhance with motion.
- Build the search process as a sequence the user can follow, not as isolated screens.
- Use real content-like examples in the UI and onboarding wherever possible.
- Make sure the user always knows: what is happening, what is ready, and what to do next.
- Keep the product honest about its local, small/medium, privacy-first nature.
