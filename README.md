# hwiki: a markdown wiki as Hermes Agent memory

**Long-term memory you can open in Obsidian.** hwiki turns a folder of markdown
files into [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory.
The agent recalls from it before every turn and files new facts into it. You read,
edit and reorganise the same files in Obsidian (or any editor), and track every
change in git.

No database to trust blindly, no cloud service, no extra dependencies: Python's
standard library and a local SQLite FTS5 index that answers in milliseconds.

## Why hwiki

| Typical agent memory | hwiki |
|---|---|
| Opaque rows or vectors | Plain markdown pages you can read |
| Fixing a wrong memory needs a tool call | Fix it in your editor |
| No history | `git log`, `git diff`, `git blame` |
| A pile of facts | Pages, headings, `[[wikilinks]]`, frontmatter, backlinks |
| Data leaves the machine (hosted options) | Local files, local index, zero network calls |

### Work on the same knowledge base as your agent

The wiki is the shared artifact, and you and the agent are co-authors:

- **You curate in Obsidian.** Write pages, link them, restructure folders. hwiki
  re-indexes changes automatically, so the agent sees your edits within a minute.
- **The agent recalls what you wrote.** Before each turn the most relevant
  heading-sized excerpts are injected, with page and section, so answers are
  grounded in your notes and citable.
- **The agent writes to a staging area, never over your pages.** New facts land as
  dated bullets in `wiki/memory/`. You review them in Obsidian, promote what
  matters into proper pages, and delete the rest. One `git diff` shows everything
  the agent added.
- **The graph works for both of you.** `[[wikilinks]]` and backlinks render in
  Obsidian's graph view and let the agent follow links instead of re-searching.

## Install

```bash
hermes plugins install https://github.com/ottosulin/hwiki
hermes memory setup            # choose "hwiki", point it at your wiki folder
hermes gateway restart         # or restart the CLI
hermes hwiki status            # verify: root, backend, page count
```

Or configure it non-interactively:

```bash
hermes config set memory.provider hwiki
cat > ~/.hermes/hwiki.json <<'JSON'
{ "wiki_path": "~/Obsidian/MyVault" }
JSON
```

Any folder of markdown works, including an existing Obsidian vault. Only one
memory provider can be active at a time.

## How it works

```
my-wiki/
├── wiki/              indexed and recalled, human-curated
│   ├── concepts/ ...
│   └── memory/        the ONLY place the agent writes (dated bullets)
└── raw/               never indexed, never recalled, never readable by memory tools
```

- **Recall:** heading-scoped chunks from SQLite FTS5 (BM25). Sub-10 ms on a
  2,000-page wiki. Optional [qmd](https://github.com/tobi/qmd) hybrid semantic
  search, with automatic fallback to FTS5 if qmd is missing or slow.
- **Writes:** append-only to `memory_dir`, de-duplicated, with a secret guard that
  refuses API keys, tokens and private keys.
- **Untrusted sources:** put third-party documents in `raw/`. They never reach
  auto-injected context, which is where prompt-injection attacks are most
  dangerous. Summarise them into `wiki/` yourself.
- **Background agents are read-only:** in cron, subagent and flush contexts the
  write tool is not offered, so automated runs cannot pollute your wiki.

## Agent tools

| Tool | Purpose |
|---|---|
| `wiki_memory_search` | Ranked excerpts across the wiki (optional frontmatter filter) |
| `wiki_memory_read` | Full page by path, slug or `[[wikilink]]`, with links and backlinks |
| `wiki_memory_write` | Append a dated fact to a page in the memory zone |
| `wiki_memory_status` | Index stats; with `lint`, dangling links and orphan pages |

## CLI

```bash
hermes hwiki status              # root, backend, counts
hermes hwiki search "release process"
hermes hwiki index               # force a full reindex
hermes hwiki lint                # dangling wikilinks, orphan pages
hermes hwiki config              # resolved configuration
hermes hwiki migrate --dry-run   # import facts from the holographic provider
```

## Configuration

`$HERMES_HOME/hwiki.json`. Only `wiki_path` is required.

| Key | Default | Description |
|---|---|---|
| `wiki_path` | | Wiki root. `~` and `$HERMES_HOME` expand. |
| `backend` | `fts5` | `fts5`, or `qmd` (hybrid with FTS5 fallback) |
| `memory_dir` | `wiki/memory` | The only folder the agent may write to |
| `untrusted_dirs` | `["raw"]` | Never indexed, recalled or readable |
| `prefetch_limit` | `4` | Excerpts injected per turn |
| `prefetch_max_chars` | `1400` | Cap on injected characters per turn |
| `auto_recall` | `true` | `false` = tools only |

Environment overrides: `HWIKI_PATH`, `HWIKI_BACKEND`, `HWIKI_MEMORY_DIR`,
`HWIKI_QMD_BINARY`, `HWIKI_QMD_MODE`. All options, qmd tuning and Docker setup:
[docs/configuration.md](docs/configuration.md).

## Privacy and security

- **No network access** with the default `fts5` backend. Your notes never leave
  the machine. The optional qmd backend runs local models.
- **Data locations:** your wiki folder (which you choose) plus
  `$HERMES_HOME/hwiki.json` and `$HERMES_HOME/hwiki-index.sqlite` (disposable,
  rebuilt on demand).
- Writes are confined to `memory_dir`, path traversal is rejected, and search
  input is treated as data, never as query syntax.

Details: [docs/security.md](docs/security.md). Design rationale and benchmarks:
[docs/design.md](docs/design.md).

## Development

```bash
python3 tests/test_store_search.py   # store, chunking, search, trust boundary
python3 tests/test_provider.py       # full MemoryProvider contract
python3 tests/test_cli_load.py       # CLI loaded exactly as Hermes loads it
python3 -m pytest tests/             # naming, resolution (needs pytest + PyYAML)
hermes plugins validate .            # Hermes catalog admission checks
```

Contributions welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
