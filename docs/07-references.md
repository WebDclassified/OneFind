# 07 · References

Project: OneFind · Version: 1.1 · Curated sources behind the implementation.

## The paper being reproduced
- Source paper: https://arxiv.org/abs/2608.24060 — *"SQLite is Enough: Lexical, Semantic, and Hybrid Search with scrydb"* (cs.IR; MIT-licensed reference implementation)
- Upstream API used as behavioral reference only (we reimplement, ADR-1).

## Methods & building blocks
- BEIR benchmark: https://arxiv.org/abs/2104.08663 · datasets https://github.com/beir-cellar/beir
- MTEB (baseline source for our embedder): https://arxiv.org/abs/2210.07316 · leaderboard https://huggingface.co/spaces/mteb/leaderboard
- FTS5 full-text (BM25): https://www.sqlite.org/fts5.html
- sqlite-vec vector search: https://github.com/asg017/sqlite-vec
- Reciprocal Rank Fusion — Cormack et al. 2009: https://dl.acm.org/doi/10.1145/1571941.1572114
- sentence-transformers / all-MiniLM-L6-v2: https://www.sbert.net/
- ranx (IR evaluation): https://github.com/aminediro/ranx

## Spec-system practices (from the six-document guide)
- Atlassian — requirements & PRDs: https://www.atlassian.com/agile/product-management/requirements
- Microsoft Learn — functional/technical design docs: https://learn.microsoft.com/en-us/dynamics365/guidance/patterns/create-functional-technical-design-document
- Azure — Architecture Decision Records: https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record
- Azure — architecture diagrams: https://learn.microsoft.com/en-us/azure/well-architected/architect-role/design-diagrams
- W3C — WCAG 2.2: https://www.w3.org/TR/WCAG22/
- OWASP — ASVS: https://owasp.org/www-project-application-security-verification-standard/
- OWASP — Authorization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- Microsoft — database design basics: https://support.microsoft.com/en-us/office/database-design-basics-eb2159cf-1e30-401a-8084-bd4f9c9ca1f5
- Agile Manifesto: https://agilemanifesto.org/

## Extra links
- Design Document Forum discussion: https://news.ycombinator.com/item?id=44779428
- Adobe — user flow diagrams: https://business.adobe.com/au/blog/basics/how-to-make-a-user-flow-diagram

> Note: templates and references are starting points, not substitutes for engineering judgment; depth scales with risk (this project: low-risk local tooling → lean docs are appropriate).
