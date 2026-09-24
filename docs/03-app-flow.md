# 03 · Application Flow and State Map

Project: OneFind · Version: 1.1 · Status: Implemented

## Command surface

| Command | Success | Purpose |
|---|---:|---|
| `onefind check [--full] [--json]` | 0/3 | Verify engine and optional model |
| `onefind index PATH --db DB [--embed]` | 0/2/3/4 | Build or refresh an index |
| `onefind search QUERY --mode MODE` | 0/2/3/4 | Query an existing index |
| `onefind eval DATASET --db DB` | 0/2/4 | Build and evaluate a dataset |
| `onefind sweep-alpha DATASET --db DB` | 0/2/4 | Run the fusion extension |
| `onefind smoke --db DB` | 0/2/4 | Evaluate JSONL gold cases |
| `onefind serve --db DB` | server | Start the local web app |

Exit codes: `0` success, `2` usage/validation, `3` environment/model, `4` data/corpus.

## Primary journeys

### Build a local index

1. Validate runtime with `onefind check`.
2. Choose a folder, text file, or JSONL corpus.
3. Add `--embed` for semantic/hybrid retrieval.
4. Receive processed/total counts and storage model.
5. Re-run safely; file IDs and vector rows are upserted consistently.

Recovery: a failed batch rolls back. A lexical update to an embedded index is refused rather than leaving stale vectors.

### Search

1. Enter a nonblank query of at most 512 characters.
2. Choose keyword, semantic, or hybrid mode.
3. For semantic/hybrid, choose float, int8, or binary when supported.
4. Choose 5–30 UI results (CLI/API permit 1–50).
5. Receive ranked results with title, ID/source, safe snippet, score metric, and latency.

Recovery: empty query returns to idle; unsupported capabilities are disabled; service errors show a retry action.

### Evaluate

1. Acquire or load a local dataset.
2. Build into an isolated staging database.
3. Load the model and index selected documents.
4. Run every configuration and write a report.
5. Atomically publish the database and manifest.

Recovery: model, indexing, or metric failure leaves the existing target untouched and removes staging files.

### Use the web demo

1. Start `onefind serve --db DB`.
2. Fetch `/api/stats` to learn readiness and capabilities.
3. Disable unsupported modes/precisions in the UI.
4. Submit `/api/search` only after an explicit Search action.
5. Render response content through text nodes and structured highlight segments.
6. Cancel stale requests and replace them with the latest result.

## UI state machine

| State | Trigger | Display and controls |
|---|---|---|
| Connecting | App boot | Header status; controls disabled until stats load |
| Idle | Ready index, no active query | Search guidance and examples |
| Loading | Explicit submit | Disabled input, truthful model/search copy, skeletons |
| Results | Nonempty result list | Summary, timing, ordered result articles |
| Empty | Zero hits | Mode-specific recovery suggestions |
| No index | `ready=false` | Disabled search and local build instructions |
| Unavailable mode | No vectors/calibration | Capability-aware disabled controls |
| Error | Network/API/validation failure | Calm message and Retry |

The complete result list is not placed inside `aria-live`; a concise status region announces result count, loading, empty, and error transitions.

## API contracts

### `GET /api/stats`

Returns readiness, document/vector counts, database size, model/dimension, health, supported modes, and supported precisions. It does not expose the absolute index path.

### `POST /api/search`

```json
{
  "query": "nonblank text",
  "mode": "lexical | semantic | hybrid",
  "precision": "float | int8 | binary",
  "k": 10
}
```

The response contains `results` plus `meta` with count, mode, precision, total latency, and model-load latency. Errors use:

```json
{
  "error": {
    "code": "stable_machine_code",
    "message": "human-readable message",
    "retryable": false
  }
}
```

### `POST /api/reset`

Disabled by default. When explicitly enabled, a valid startup token is required. Search and reset transitions are serialized by a process lock.

## Redirects and persistence

There are no route redirects. The index is a local file. Search is naturally idempotent; writes use explicit transactions. The web application performs no analytics or background indexing.
