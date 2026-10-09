# Configuration

Configuration lives in `$HERMES_HOME/hwiki.json` (`$HERMES_HOME` is `~/.hermes`, or
`~/.hermes/profiles/<name>` for a profile). Only `wiki_path` is required.
`hermes memory setup` writes this file for you.

## All options

| Key | Default | Description |
|---|---|---|
| `wiki_path` | | **Required.** Wiki root. `$HERMES_HOME` and `~` expand. |
| `backend` | `fts5` | `fts5` or `qmd` (`qmd` builds a hybrid with FTS5 fallback). |
| `memory_dir` | `wiki/memory` | Wiki-relative folder the agent may write to. The only writable path. |
| `untrusted_dirs` | `["raw"]` | Never indexed, recalled or readable through memory tools. |
| `include` | `["**/*.md"]` | Root-relative globs to index. |
| `exclude` | `[]` | Root-relative globs to skip (`.git`, `node_modules`, `.obsidian` and similar are always skipped). |
| `index_path` | `$HERMES_HOME/hwiki-index.sqlite` | FTS5 index location. Disposable. |
| `prefetch_limit` | `4` | Excerpts auto-injected per turn. |
| `prefetch_max_chars` | `1400` | Hard cap on injected characters per turn. |
| `auto_recall` | `true` | `false` = tools only, no automatic injection. |
| `min_query_chars` | `8` | Skip recall for trivially short messages. |
| `reindex_interval_s` | `60` | Minimum seconds between incremental reindexes. |
| `max_hits_per_page` | `2` | Per-page chunk cap when ranking results. |
| `demote_paths` | `["log.md", "wiki/memory/builtin-mirror.md"]` | Pages ranked after primary results. |
| `mirror_builtin_memory` | `true` | Mirror built-in `memory` tool additions into the wiki. |
| `mirror_page` | `builtin-mirror` | Memory page receiving those mirrors. |
| `qmd_binary` | `qmd` | Path or name of the qmd executable. |
| `qmd_collection` | `hwiki` | qmd collection name. |
| `qmd_mode` | `search` | `search` (BM25), `vsearch` (vector), `query` (hybrid + rerank). |
| `qmd_timeout_s` | `6.0` | Per-query qmd budget before falling back to FTS5. |

### Environment overrides

Environment variables win over the file: `HWIKI_PATH`, `HWIKI_BACKEND`,
`HWIKI_MEMORY_DIR`, `HWIKI_QMD_BINARY`, `HWIKI_QMD_MODE`.

### Upgrading from the pre-1.0 `wiki` provider

Early versions used the provider name `wiki`, the config file `wiki-memory.json`
and `WIKI_MEMORY_*` environment variables. Those are still read as fallbacks, so
an existing setup keeps working. To switch over:

1. Install hwiki as above and set `memory.provider: hwiki`.
2. Rename `wiki-memory.json` to `hwiki.json` (optional; the old name is read when
   the new one is absent).
3. The index rebuilds automatically under its new name.

## Profiles

Each Hermes profile has its own `$HERMES_HOME`, so install and configure per
profile to scope memory. Profiles can point at different wikis or share one.

## Using an Obsidian vault

Point `wiki_path` at the vault root. Recommended layout inside the vault:

- Keep curated notes anywhere under the indexed globs.
- Set `memory_dir` to a folder you review regularly (for example `wiki/memory`).
- Keep clipped articles, PDFs and other third-party material in an untrusted
  folder (`raw/` by default).

The `.obsidian/` settings folder is never indexed. Obsidian's sync, git plugins and
backups work as normal because hwiki only reads your notes and appends to the
memory folder.

## qmd backend (optional)

[qmd](https://github.com/tobi/qmd) adds semantic and hybrid retrieval. It needs
Node.js 22 or newer, and its vector modes download local GGUF models (roughly 0.3 GB
for `vsearch`, 1.6 GB for `query`).

```bash
npm install -g @tobilu/qmd
hermes config set memory.provider hwiki
# then in hwiki.json: "backend": "qmd", "qmd_mode": "search" | "vsearch" | "query"
hermes hwiki index
```

Selecting `qmd` always builds a hybrid: FTS5 runs first and qmd results are fused
in with Reciprocal Rank Fusion only when qmd answers within `qmd_timeout_s`.
If qmd is missing, slow or broken, recall degrades to FTS5 instead of failing.

Use qmd when the wiki is large and its vocabulary is inconsistent, or when you need
conceptual matches that share no keywords with the query. On a curated wiki, FTS5
alone usually answers correctly.

## Docker

The provider runs inside the Hermes gateway process, so it only needs the wiki on
a writable, persistent volume.

```yaml
services:
  hermes:
    environment:
      HERMES_HOME: /hermes-home
      HWIKI_PATH: /wiki
    volumes:
      - ./my-wiki:/wiki:rw          # must be writable: the agent appends to memory_dir
      - hermes-home:/hermes-home    # config and index survive restarts
volumes:
  hermes-home:
```

Every official `python:*` image ships SQLite with FTS5. Check yours with:

```bash
python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"
```

For the qmd backend in a container:

- Use a base image with Node.js 22 or newer, and assert it in the build
  (`node --version && qmd --version`); an older Node only warns during install.
- Put the model cache (`XDG_CACHE_HOME`) on a named volume with at least 3 GB free,
  otherwise models re-download on every recreate or fail on a small tmpfs.
- Allow around 4 GB of memory for `vsearch` and `query` modes.

## Troubleshooting

**`hermes memory status` does not list hwiki.** The plugin must be at
`$HERMES_HOME/plugins/hwiki/`. Run `hermes config path` to see which
`$HERMES_HOME` is active.

**`hermes hwiki` is not a command.** CLI commands register only for the active
provider. Set `memory.provider: hwiki` and restart.

**Provider loads but is unavailable.** `wiki_path` is unset or not a directory.
`hermes hwiki config` shows what resolved.

**No recall.** Check `auto_recall`, and that the message is at least
`min_query_chars` long. Test retrieval with `hermes hwiki search "..."`.

**New edits are not found.** Agent writes reindex immediately; external edits are
picked up within `reindex_interval_s`. Force with `hermes hwiki index`.

**`database is locked`.** Two Hermes processes share one `index_path`. Give each
profile its own; the index is disposable.

**qmd is slow on the first query.** Vector modes load their models on first use.
Run a warm qmd daemon, raise `qmd_timeout_s`, or use `qmd_mode: search`. Timeouts
are not fatal: you get FTS5 results.
