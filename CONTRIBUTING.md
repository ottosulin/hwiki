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

## Pull requests

Add a test for every behaviour change, ideally one that fails before your change.
Keep fixtures generic: no real names, companies, credentials or private notes.
