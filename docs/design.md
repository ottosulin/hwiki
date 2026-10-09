# Design notes

## Principles

1. **The curated wiki is read-only to the agent.** Automatic writes land only in the
   memory zone, so they can never corrupt hand-maintained pages, and every agent
   write is reviewable in one `git diff`.
2. **Untrusted zones are never read** by memory tools (see [security.md](security.md)).
3. **Recall must be fast and must never block a turn.** Hermes times out prefetch;
   FTS5 answers in milliseconds and work happens behind a bounded lock.
4. **Degrade, never fail.** A missing qmd, Node, PyYAML or wiki folder produces a
   warning and reduced function, never an exception.

## Architecture

```
Hermes MemoryManager
  ├─ system_prompt_block()  "memory is a wiki at <path>, N pages"
  ├─ prefetch(query)        top excerpts injected before each turn
  ├─ queue_prefetch(query)  warms the next turn in the background
  ├─ handle_tool_call(...)  wiki_memory_{search,read,write,status}
  └─ on_memory_write(...)   mirrors built-in memory additions
          │
   WikiMemoryProvider (__init__.py)      config.py: shared by provider and CLI
          ├─ WikiStore (store.py)        frontmatter, wikilinks, chunking,
          │                              secret guard, locked appends
          └─ Backend (search.py)         Fts5Backend (default), QmdBackend,
                                         HybridBackend (RRF fusion)
```

`cli.py` imports shared helpers from `config.py`, not from the package. Hermes loads
a plugin's CLI with the plugin registered as an empty package (its `__init__.py` is
not executed), so `from . import X` would fail there. `tests/test_cli_load.py`
reproduces that loading path.

## Why heading-scoped chunks

Injecting whole pages would blow the context budget and bury the relevant paragraph.
Chunking on H1 to H3 keeps excerpts small, self-describing and citable as
`page › heading`.

## Why FTS5 by default

| Backend | Query latency | Extra dependencies | Disk |
|---|---|---|---|
| `fts5` | under 1 ms (50 pages), 5 to 10 ms (2,000 pages) | none (stdlib) | about 1.5x the corpus |
| `qmd search` (BM25) | 200 to 300 ms | Node 22 + qmd | no models |
| `qmd vsearch` (vector) | seconds, slow first call | + models | about 0.3 GB |
| `qmd query` (hybrid + rerank) | 2 to 3 s warm | + models | about 1.6 GB |

A full FTS5 rebuild of a 2,000-file, 7.6 MB corpus takes about a quarter of a second.
There is no scale reason to leave FTS5 below several thousand pages; the reason to
add qmd is retrieval quality on inconsistent vocabulary. On a curated wiki, where
pages share consistent terms, BM25 over heading chunks answers conversational
questions well, and in testing it answered some that qmd's own BM25 mode missed.

### Alternatives considered

| Option | Why not |
|---|---|
| grep / ripgrep | No ranking, chunking or snippets. |
| sentence-transformers | Hundreds of MB of dependencies in the gateway process. |
| fastembed / sqlite-vec | Lighter, but still need an embedding stack and model download. |
| tantivy / whoosh | Keyword engines like FTS5, so no quality gain for an extra dependency. |
| Hosted embeddings | Sends the private knowledge base to a third party. |
| MCP note servers | Provide tools but no prefetch hook, so no automatic recall. |

## Why no automatic transcript ingestion

Writing every turn would turn the wiki into a chat log and remove what makes it
better than a database: a human can read it. `sync_turn()` is deliberately a no-op.

## Why no trust scores

Trust scores approximate "is this still true". A wiki answers that with `updated:`
frontmatter, git history and a person reading the page, all of which are legible in
a way a number is not.

## Migrating from holographic memory

`hermes hwiki migrate` reads a holographic `memory_store.db` read-only and files each
fact into the memory zone, one page per category. It introspects the source schema
rather than assuming it, keeps original dates, converts linked entities to
`#hashtags`, skips duplicates, and blocks secret-shaped facts. `--min-trust` filters
low-confidence facts; trust scores are not written into the wiki.

Recommended sequence: install hwiki while keeping the old provider active, run
`migrate --dry-run`, import, curate the imported pages into proper notes, then
switch `memory.provider` and restart. Keep the old database as a rollback.
