# 05 · Backend and Data Model

Project: OneFind · Version: 1.1 · Status: Implemented

## Storage model

One SQLite file contains:

```text
documents
chunks
fts (FTS5)
vec_float (sqlite-vec vec0)
schema_meta
```

## Tables

### `documents`

| Field | Type | Purpose |
|---|---|---|
| `doc_id` | TEXT PK | Canonical corpus ID |
| `title` | TEXT | Display and lexical text |
| `body` | TEXT | Full document text |
| `source` | TEXT | Relative path, JSONL source, or dataset origin |
| `meta_json` | TEXT | Optional JSON metadata |
| `created_at` | TEXT | UTC creation timestamp |
| `updated_at` | TEXT | UTC update timestamp |

File corpora use the relative path including suffix. JSONL corpora use `_id`/`id` or a deterministic line fallback. This prevents nested same-stem and same-directory extension collisions.

### `chunks`

One V1 chunk per document:

| Field | Type | Purpose |
|---|---|---|
| `row_id` | INTEGER PK | Stable vector key |
| `doc_id` | TEXT FK | Owning document, cascading delete |
| `seq` | INTEGER | Chunk order |
| `text` | TEXT | Chunk text |

### `fts`

Standalone FTS5 index with `doc_id UNINDEXED`, title, and body. Documents are explicitly resynchronized inside the write transaction.

### `vec_float`

```sql
CREATE VIRTUAL TABLE vec_float USING vec0(
  embedding float[384] distance_metric=cosine
);
```

Float KNN is native to sqlite-vec. Int8 and binary configurations are derived from these vectors in bounded application-side blocks; there are no misleading duplicate quantized tables.

### `schema_meta`

Stores schema version, model name/revision/dimension, deterministic int8 scale, and the evaluation manifest. The opener rejects unrelated databases and unsupported schema versions before initialization DDL.

## Write transactions

Each ingest batch:

1. upserts documents;
2. upserts stable chunks;
3. resynchronizes FTS rows;
4. encodes and validates vectors when an embedder is attached;
5. deletes/replaces vector rows;
6. commits once.

Any exception rolls back the whole batch. Lexical-only updates to an embedded index are rejected. Model attachment backfills missing vectors.

## Read paths

- **Lexical:** FTS5 BM25, strongest first, document-ID tie-break.
- **Float:** sqlite-vec cosine KNN.
- **Int8/binary:** 2,048-row vector blocks plus a bounded top-k candidate set.
- **Hybrid:** rank fusion over two leg results.
- **Rerank:** one query encoding and cosine scoring over the complete fused candidate pool.

SQLite `data_version` invalidates an in-process quantized cache after external writes.

## Health

`Index.health()` compares:

- document count;
- chunk count;
- FTS count;
- vector count;
- chunks missing vectors;
- orphan vector rows.

The web stats endpoint exposes health but not the absolute index path.

## Library API

```python
from onefind import Document, Index

with Index.open("demo.db") as index:
    index.add_documents([
        Document("note-1", "A body", title="A title", source="notes/a.md")
    ])
    index.attach_embedder(embedder)  # optional; backfills existing chunks
    hits = index.search(
        "a natural-language query",
        mode="hybrid",
        precision="float",
        rerank=True,
        candidate_depth=50,
    )
```

The same search function is also available as `onefind.search.search(index, ...)`.

## HTTP API

| Method/path | Contract |
|---|---|
| `GET /api/stats` | Readiness, capabilities, counts, size, model, health |
| `POST /api/search` | Validated query/mode/precision/k and safe result segments |
| `GET /health` | Service liveness |
| `POST /api/reset` | Disabled unless explicitly enabled with a startup token |

Search, model loading, and reset transitions share a lock. The API returns a stable error envelope and never exposes foreign absolute paths.

## Security and privacy

- FTS values use bound parameters and quoted tokens.
- Indexed snippets are converted to text/highlight segments; the UI never treats them as HTML.
- CSP and defensive browser headers are applied.
- Binding is loopback by default; remote binding requires explicit acknowledgment.
- No analytics, telemetry, or remote content requests exist.
