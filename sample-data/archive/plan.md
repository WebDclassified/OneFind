# Archive Database Decompression Plan

The archive team will move the legacy catalog to a new retrieval service in three stages. First, a read-only export verifies record counts, identifiers, and checksums. Second, sample queries compare relevance and ordering. Third, the service is switched during a maintenance window, with a rollback copy retained for seven days.

The old system remains frozen after the move. Quarterly checks confirm that exports, source snapshots, and query evaluations are still reproducible. No live user records are copied into the demo environment; only the synthetic catalog appears in this example.
