# Contributing

Issues and pull requests are welcome.

## Ground rules

- The runtime must stay standard-library only. Optional integrations (like qmd)
  must degrade gracefully when absent.
- The default backend must make no network calls.
- Never weaken the trust boundary: untrusted folders stay out of the index, recall
  and reads, and the agent writes only to the memory folder.
- Keep `cli.py` importing from `.config`, not from the package (see
  [docs/design.md](docs/design.md)).

## Running the tests

```bash
python3 tests/test_store_search.py
python3 tests/test_provider.py
python3 tests/test_cli_load.py
python3 -m pytest tests/          # requires pytest and PyYAML
```

The scripts need no Hermes install and no network. If you have Hermes installed,
also run:

```bash
hermes plugins validate .
```

Set `HWIKI_BENCH_PATH=/path/to/a/wiki` to additionally index and time a real wiki
in `tests/test_store_search.py`.

`tests/test_qmd_index.py` runs the real qmd CLI (BM25 only, no model downloads) in
a throwaway config and cache directory. It runs when `qmd` is on `PATH`, or when
`HWIKI_QMD_BINARY` points at it, and is skipped otherwise. One test also embeds
with the real model to check that exclusions remove vectors, not only rows. It is
opt-in, and never downloads a model: set `HWIKI_QMD_EMBED_MODELS` to a folder that
already holds `hf_ggml-org_embeddinggemma-300M-Q8_0.gguf` (qmd keeps it in
`~/.cache/qmd/models` after a first `qmd embed`). It takes about 20 seconds.

## Pull requests

Add a test for every behaviour change, ideally one that fails before your change.
Keep fixtures generic: no real names, companies, credentials or private notes.
