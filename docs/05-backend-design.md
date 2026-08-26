# 05 · Backend Design & Data Model

Project: scrydb reproduction · Version: v0.1 (draft) · Status: Proposed
Storage engine: one SQLite file containing relational tables, FTS5 index, and sqlite-vec virtual tables.

## Tables

### documents
| Field | Type / rule | Purpose |
|---|---|---|
| doc_id | TEXT, primary key | Stable id (BEIR corpus id or sha1 of path+content) |
| title | TEXT, not null, default '' | Display + lexical boost candidate |
| body | TEXT, not null | Full document text |
| source | TEXT | Origin path / dataset name |
| meta_json | TEXT (JSON) | Extensible metadata |
| created_at | TEXT, ISO-8601 UTC | Insertion time |
| updated_at | TEXT, ISO-8601 UTC | Last re-index change |

### chunks *(schema-ready; V1 eval uses whole docs = 1 chunk per doc)*
| Field | Type / rule | Purpose |
|---|---|---|
| row_id | INTEGER PRIMARY KEY AUTOINCREMENT | Join key to vec tables |
| doc_id | TEXT FK→documents ON DELETE CASCADE, indexed | Owner |
| seq | INTEGER | Order within doc; UNIQUE(doc_id, seq) |
| text | TEXT not null | Chunk text |

### fts (FTS5 virtual)
`CREATE VIRTUAL TABLE fts USING fts5(doc_id UNINDEXED, title, body, tokenize='porter unicode61')` — BM25 ranking via `bm25()`; content synced by ingest within the same transaction as documents.

### vec_float / vec_int8 / vec_bit (sqlite-vec virtual, per precision)
`USING vec0(embedding float[384] | int8[384] | bit[384])`, rowid = chunks.row_id.
Rationale: mirrors the paper's precision configurations directly; quantized variants are written once at index time, so queries pay no conversion cost.

### schema_meta
key/value: schema version, model name+dimension, embedding normalization flag. Open() refuses mismatched model/dimension with explicit error.

## Access matrix (single-user local tool)

| Resource / action | OS user (owner) | Other processes |
|---|---|---|
| Read DB | Allowed | Allowed if file permissions permit (SQLite file semantics) |
| Write/index | Allowed via CLI/API | Not supported; WAL mode enables concurrent reads during serve |
| Delete/reset | Explicit `reset --force` only | n/a |

No app-level roles exist; server-side rule = the API layer validates every parameter regardless of UI (see validation).

## Library API contract (Python)

```python
idx = Index.open("x.db")                      # checks schema_meta
idx.add_documents(iterable[Document])          # transactional batches of 256
hits = idx.search("vitamin B12", mode="hybrid", precision="float", rerank=False, k=10)
# hits: list[Hit(id, score, snippet)]  — deterministic tie-break by (−score, id)
```

Errors raise typed exceptions: `ExtensionMissingError(3)`, `SchemaMismatchError(3)`, `EmptyQueryError(2)`, `CorpusNotFoundError(4)`.

## HTTP API (`scrydb serve`, localhost only)

| Method/Path | Body → Response | Failure |
|---|---|---|
| POST `/api/search` | `{query, mode, precision?, k?}` → `{results[], took_ms}` | 400 invalid params; 503 index empty |
| GET `/api/stats` | counts, db size, model name | 500 engine error |
| POST `/api/reset` | confirm token required | 409 wrong token |

Validation: query ≤512 chars after trim; k ∈ [1,50] default 10; mode/precision enums enforced server-side. Idempotency: indexing upserts by doc_id; search is naturally idempotent.

## Indexes & query patterns

- FTS5 handles lexical lookups; `chunks(doc_id)` indexed; vec tables are their own index.
- Expected patterns: point queries by doc_id (highlighting), range scan none, joins chunks↔documents only.

## Retention, events, sensitive data

- Reset = delete `.db` file (documented); no background jobs/events/webhooks in V1.
- Backups: copy the single file (that IS the paper's archival story).
- Sensitive-data classification: user-supplied corpus may contain private text; README warns that `.db` files inherit it — never share without review.
