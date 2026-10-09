"""Configuration loading for the wiki memory provider.

This lives in its own module for a specific reason: Hermes loads a plugin's
``cli.py`` through ``discover_plugin_cli_commands()``, which registers
``_hermes_user_memory.<name>`` as an EMPTY synthetic package shell and
deliberately does NOT execute the plugin's ``__init__.py``. Any
``from . import <symbol>`` in ``cli.py`` therefore fails with::

    ImportError: cannot import name 'load_config'
                 from '_hermes_user_memory.hwiki' (unknown location)

Real sibling submodules DO resolve (the shell carries
``submodule_search_locations``), so shared helpers must live in a module like
this one and be imported as ``from .config import load_config``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "hwiki.json"
# Pre-1.0 installs (provider name "wiki") wrote this file; still read as a fallback.
LEGACY_CONFIG_FILENAME = "wiki-memory.json"
INDEX_FILENAME = "hwiki-index.sqlite"

DEFAULTS: Dict[str, Any] = {
    "wiki_path": "",
    "backend": "fts5",              # fts5 | qmd  (qmd implies hybrid + fts5 fallback)
    "memory_dir": "wiki/memory",
    "untrusted_dirs": ["raw"],
    "include": ["**/*.md"],
    "exclude": [],
    "index_path": "",               # defaults to $HERMES_HOME/hwiki-index.sqlite
    "prefetch_limit": 4,
    "prefetch_max_chars": 1400,
    "auto_recall": True,
    "min_query_chars": 8,
    "reindex_interval_s": 60,
    "qmd_binary": "qmd",
    "qmd_collection": "hwiki",
    "qmd_mode": "search",           # search | vsearch | query
    "qmd_timeout_s": 6.0,
    "max_hits_per_page": 2,         # ranking partition: per-page chunk cap
    "demote_paths": ["log.md", "wiki/memory/builtin-mirror.md"],  # secondary pages
    "fetch_multiplier": 4,          # candidate over-fetch factor
    "mirror_builtin_memory": True,
    "mirror_page": "builtin-mirror",
}

_ENV_OVERRIDES = {
    "HWIKI_PATH": "wiki_path",
    "HWIKI_BACKEND": "backend",
    "HWIKI_MEMORY_DIR": "memory_dir",
    "HWIKI_QMD_BINARY": "qmd_binary",
    "HWIKI_QMD_MODE": "qmd_mode",
}

# Pre-1.0 names, honoured when the HWIKI_* equivalent is unset.
_LEGACY_ENV_OVERRIDES = {
    "WIKI_MEMORY_PATH": "wiki_path",
    "WIKI_MEMORY_BACKEND": "backend",
    "WIKI_MEMORY_DIR": "memory_dir",
    "WIKI_MEMORY_QMD_BINARY": "qmd_binary",
    "WIKI_MEMORY_QMD_MODE": "qmd_mode",
}


def resolve_hermes_home(explicit: Optional[str] = None) -> str:
    """Best-effort resolution of the active (profile-scoped) HERMES_HOME."""
    if explicit:
        return str(explicit)
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def load_config(hermes_home: Optional[str] = None) -> Dict[str, Any]:
    """Merge defaults <- ``$HERMES_HOME/wiki-memory.json`` <- environment."""
    config = dict(DEFAULTS)
    home = resolve_hermes_home(hermes_home)

    path = Path(home) / CONFIG_FILENAME
    if not path.is_file() and (Path(home) / LEGACY_CONFIG_FILENAME).is_file():
        path = Path(home) / LEGACY_CONFIG_FILENAME
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                config.update(data)
        except Exception as exc:
            logger.warning("hwiki: could not read %s: %s", path, exc)

    for overrides in (_LEGACY_ENV_OVERRIDES, _ENV_OVERRIDES):  # new names applied last, so they win
        for env_var, key in overrides.items():
            value = os.environ.get(env_var)
            if value:
                config[key] = value

    config["_hermes_home"] = home
    return config


def expand_path(path_value: str, hermes_home: str = "") -> str:
    """Expand ``$HERMES_HOME`` and ``~`` in a configured path."""
    if not path_value:
        return ""
    text = str(path_value)
    if hermes_home:
        text = text.replace("$HERMES_HOME", hermes_home)
        text = text.replace("${HERMES_HOME}", hermes_home)
    return str(Path(text).expanduser())


def save_config_file(values: Dict[str, Any], hermes_home: str) -> Path:
    """Persist non-secret config, preserving keys the wizard didn't ask about."""
    path = Path(hermes_home) / CONFIG_FILENAME
    existing: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
    existing.update({k: v for k, v in values.items() if not k.startswith("_")})
    if "untrusted_dirs" not in existing:
        existing["untrusted_dirs"] = list(DEFAULTS["untrusted_dirs"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return path


def default_index_path(hermes_home: str = "") -> str:
    """``$HERMES_HOME/hwiki-index.sqlite`` (disposable; rebuilt on demand)."""
    base = Path(hermes_home) if hermes_home else Path(resolve_hermes_home())
    return str(base / INDEX_FILENAME)


def build_runtime(config: Optional[Dict[str, Any]] = None):
    """Return ``(config, WikiStore, backend)`` from configuration.

    Shared by the provider and the CLI so both resolve paths, exclusions and
    backend selection identically. Returns ``(config, None, None)`` when
    ``wiki_path`` is unset or missing.
    """
    from .store import WikiStore
    from .search import build_backend

    config = config or load_config()
    home = str(config.get("_hermes_home") or "")
    root = expand_path(str(config.get("wiki_path") or ""), home)
    if not root or not Path(root).is_dir():
        return config, None, None

    store = WikiStore(
        root,
        include=config.get("include") or ["**/*.md"],
        exclude=config.get("exclude") or [],
        untrusted_dirs=config.get("untrusted_dirs") or [],
        memory_dir=str(config.get("memory_dir") or "wiki/memory"),
    )

    index_path = expand_path(str(config.get("index_path") or ""), home) or default_index_path(home)

    backend = build_backend(
        store,
        backend=str(config.get("backend") or "fts5"),
        db_path=index_path,
        qmd_binary=str(config.get("qmd_binary") or "qmd"),
        qmd_collection=str(config.get("qmd_collection") or "hwiki"),
        qmd_mode=str(config.get("qmd_mode") or "search"),
        qmd_timeout=float(config.get("qmd_timeout_s") or 6.0),
        max_hits_per_page=int(config.get("max_hits_per_page") or 2),
        demote_paths=list(config.get("demote_paths") or []),
        fetch_multiplier=int(config.get("fetch_multiplier") or 4),
    )
    return config, store, backend
