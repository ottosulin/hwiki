"""Public identity of the provider: name, config file, env vars, index, qmd collection.

Hermes resolves a memory provider by its install directory name
(``$HERMES_HOME/plugins/hwiki``) and the provider's ``name`` must match
``memory.provider``. The ``hermes <name>`` CLI tree uses the same name.
Older installs used the ``wiki`` name with ``wiki-memory.json`` and
``WIKI_MEMORY_*`` env vars; those keep working as fallbacks.
"""

from __future__ import annotations

import json
from pathlib import Path

import _hwiki_path  # noqa: F401
import pytest
import yaml

from hwiki import WikiMemoryProvider
from hwiki import config as cfg

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in list(cfg._ENV_OVERRIDES) + list(cfg._LEGACY_ENV_OVERRIDES):
        monkeypatch.delenv(var, raising=False)


def test_provider_name_matches_manifest():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == "hwiki"
    assert WikiMemoryProvider(config=dict(cfg.DEFAULTS)).name == "hwiki"


def test_config_file_and_index_names(tmp_path):
    assert cfg.CONFIG_FILENAME == "hwiki.json"
    wiki = tmp_path / "wiki"
    (wiki / "wiki").mkdir(parents=True)
    cfg.save_config_file({"wiki_path": str(wiki)}, str(tmp_path))
    assert (tmp_path / "hwiki.json").is_file()
    _, _, backend = cfg.build_runtime(cfg.load_config(str(tmp_path)))
    assert cfg.default_index_path(str(tmp_path)).endswith("hwiki-index.sqlite")
    assert backend is not None
    assert cfg.DEFAULTS["qmd_collection"] == "hwiki"


def test_legacy_config_file_is_still_read(tmp_path):
    (tmp_path / "wiki-memory.json").write_text(json.dumps({"wiki_path": "/legacy/wiki"}))
    assert cfg.load_config(str(tmp_path))["wiki_path"] == "/legacy/wiki"


def test_new_config_file_wins_over_legacy(tmp_path):
    (tmp_path / "wiki-memory.json").write_text(json.dumps({"wiki_path": "/legacy/wiki"}))
    (tmp_path / "hwiki.json").write_text(json.dumps({"wiki_path": "/new/wiki"}))
    assert cfg.load_config(str(tmp_path))["wiki_path"] == "/new/wiki"


def test_env_overrides_new_and_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_MEMORY_PATH", "/legacy/env")
    assert cfg.load_config(str(tmp_path))["wiki_path"] == "/legacy/env"
    monkeypatch.setenv("HWIKI_PATH", "/new/env")
    assert cfg.load_config(str(tmp_path))["wiki_path"] == "/new/env"
