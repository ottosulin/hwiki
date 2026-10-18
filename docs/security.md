# Security model

hwiki is designed for wikis that hold sensitive material. Auto-injected recall goes
straight into the agent's context, which makes it the most valuable target for
prompt injection, so the design starts from that.

| Control | Implementation |
|---|---|
| **Untrusted-zone isolation** | `untrusted_dirs` (default `raw/`) are excluded from indexing, recall and direct reads, enforced in three independent places (see below). |
| **Write containment** | The agent writes only under `memory_dir`. Curated pages are never modified. |
| **Read-only background contexts** | When Hermes reports an `agent_context` of `cron`, `subagent` or `flush`, the write tool is not offered, `wiki_memory_write` refuses, and built-in memory mirroring is suppressed. Recall stays on. |
| **Path-traversal defence** | Every path is resolved and checked for containment inside the wiki root; anything escaping it is rejected. |
| **Secret guard** | Detectors for AWS and Scaleway keys, GitHub and Slack tokens, Google API keys, `sk-` style keys, PEM private keys, JWTs and inline `password=` values. Applied to `wiki_memory_write`, built-in memory mirroring and migration imports. |
| **Query-syntax safety** | Every search token is quoted, so FTS5 operators in user input are treated as data. Fuzz-tested. |
| **No network** | The default `fts5` backend makes no outbound calls. |
| **No transcript ingestion** | Conversations are never written automatically; only deliberate facts land in the wiki. |
| **Bounded recall** | `prefetch_max_chars` caps injected context, and Hermes hard-times-out prefetch. |
| **Concurrency** | File locking around appends; degrades to unlocked rather than losing a write. |

## The untrusted zone

Third-party material (customer documents, vendor advisories, clipped web pages) is
where injected instructions live. Keep it in `raw/`:

1. The store never yields untrusted paths, so they never enter the index.
2. Page loading refuses them, so `wiki_memory_read` cannot open them.
3. With the qmd backend, hwiki uses a dedicated qmd index and writes its collection
   with an `ignore:` list built from `untrusted_dirs` and `exclude`, so qmd never
   reads, indexes or embeds those files; when a file leaves the collection (deleted
   or newly excluded), `qmd cleanup` drops its records, content and vectors.
   Regression tests check the index files themselves, including symlinks pointing
   into an excluded folder.
4. Every qmd result is still re-checked against the same rules before it is
   surfaced, in case a glob means slightly less to qmd than to hwiki. A regression
   test covers it.

To read a raw source deliberately, use the normal file tools. That is an explicit,
visible action rather than silent injection.

## Residual risk

Any page under the indexed globs is treated as trusted and may be injected. Do not
copy an untrusted document into the curated area. Put it in `raw/` and write a
summary into the wiki yourself: that summarising step is the sanitisation.
