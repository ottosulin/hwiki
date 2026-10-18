"""Background prefetch must run under the caller's contextvars.

Hermes keeps profile isolation (the active ``HERMES_HOME``, the per-turn secret
scope) in contextvars, and its memory-provider contract requires background jobs
to start through ``agent.memory_provider.spawn_context_thread``. A bare
``threading.Thread`` starts with an empty context, so a prefetch queued under a
non-default profile would resolve the default one.

The Hermes-backed tests need a Hermes source checkout; point ``HWIKI_HERMES_SRC``
at it (for example ``~/.hermes/hermes-agent``). Without it they are skipped and
the fallback path is still tested.
"""

from __future__ import annotations

import contextvars
import importlib
import os
import sys
import threading
from pathlib import Path

import _hwiki_path  # noqa: F401
import pytest

import hwiki as plugin
from hwiki import WikiMemoryProvider
from hwiki import config as cfg

PROBE: contextvars.ContextVar[str] = contextvars.ContextVar("hwiki_test_probe", default="unset")
THREAD_NAME = "wiki-memory-prefetch"


@pytest.fixture
def hermes(monkeypatch):
    """Import Hermes modules from ``HWIKI_HERMES_SRC``; undo the import state afterwards."""
    src = os.environ.get("HWIKI_HERMES_SRC", "")
    if not src or not Path(src, "agent", "memory_provider.py").is_file():
        pytest.skip("set HWIKI_HERMES_SRC to a Hermes source checkout")
    src_root = Path(src).resolve()
    before = set(sys.modules)
    monkeypatch.syspath_prepend(str(src_root))

    def load(name: str):
        module = importlib.import_module(name)
        origin = Path(module.__file__ or "").resolve()
        if src_root not in origin.parents:
            pytest.fail(f"{name} was imported from {origin}, not from HWIKI_HERMES_SRC")
        return module

    yield load
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


@pytest.fixture
def provider(tmp_path, monkeypatch):
    for var in list(cfg._ENV_OVERRIDES) + list(cfg._LEGACY_ENV_OVERRIDES):
        monkeypatch.delenv(var, raising=False)
    wiki = tmp_path / "wiki"
    (wiki / "wiki").mkdir(parents=True)
    (wiki / "wiki" / "release.md").write_text(
        "# Release process\n\nTag the release, then publish the notes.\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    config = dict(cfg.DEFAULTS, wiki_path=str(wiki), _hermes_home=str(home))
    prov = WikiMemoryProvider(config=config)
    prov.initialize("sess-1", hermes_home=str(home))
    assert prov._backend is not None
    yield prov
    prov.shutdown()


def _capture_in_prefetch_thread(provider, monkeypatch, read):
    """Queue a prefetch, wait for its worker to finish, and return what ``read()`` saw there."""
    seen: dict = {}
    reached = threading.Event()
    real_search = provider._backend.search

    def recording_search(*args, **kwargs):
        seen["value"] = read()
        seen["thread"] = threading.current_thread()
        reached.set()
        return real_search(*args, **kwargs)

    monkeypatch.setattr(provider._backend, "search", recording_search)
    provider.queue_prefetch("how does the release process work", session_id="sess-1")
    try:
        assert reached.wait(5), "prefetch thread never reached the backend"
    finally:
        for thread in threading.enumerate():
            if thread.name == THREAD_NAME:
                thread.join(5)
    assert not seen["thread"].is_alive()
    return seen


def test_prefetch_thread_inherits_context_without_hermes_helper(provider, monkeypatch):
    monkeypatch.setattr(plugin, "_hermes_spawn_context_thread", None, raising=False)
    token = PROBE.set("caller")
    try:
        seen = _capture_in_prefetch_thread(provider, monkeypatch, PROBE.get)
    finally:
        PROBE.reset(token)
    assert seen["thread"].name == THREAD_NAME
    assert seen["thread"].daemon
    assert seen["value"] == "caller"


def test_prefetch_thread_uses_hermes_helper(provider, monkeypatch, hermes):
    memory_provider = hermes("agent.memory_provider")
    calls = []
    real = memory_provider.spawn_context_thread

    def tracking(target, **kwargs):
        calls.append(kwargs.get("name"))
        return real(target, **kwargs)

    monkeypatch.setattr(plugin, "_hermes_spawn_context_thread", tracking, raising=False)
    token = PROBE.set("caller")
    try:
        seen = _capture_in_prefetch_thread(provider, monkeypatch, PROBE.get)
    finally:
        PROBE.reset(token)
    assert calls == [THREAD_NAME]
    assert seen["value"] == "caller"


def test_prefetch_thread_sees_callers_profile_home(provider, monkeypatch, tmp_path, hermes):
    memory_provider = hermes("agent.memory_provider")
    hermes_constants = hermes("hermes_constants")
    monkeypatch.setattr(
        plugin, "_hermes_spawn_context_thread", memory_provider.spawn_context_thread, raising=False
    )
    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    token = hermes_constants.set_hermes_home_override(profile_home)
    try:
        seen = _capture_in_prefetch_thread(provider, monkeypatch, hermes_constants.get_hermes_home)
    finally:
        hermes_constants.reset_hermes_home_override(token)
    assert Path(seen["value"]) == profile_home
