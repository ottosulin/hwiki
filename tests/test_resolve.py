"""resolve_page: a wrong-folder path falls back to its unique slug.

Wiki links are written without a folder (``[[release-process]]``), so an agent
that guesses the folder (``wiki/concepts/`` vs ``wiki/guides/``) asks for a path
that does not exist. A bare slug always resolved; a ``.md`` path that missed
returned None without trying the slug. Now it falls back to the filename slug,
but only when exactly one page has it.

Run: python -m pytest tests/test_resolve.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import _hwiki_path  # noqa: F401,E402  (registers the repo root as package `hwiki`)

from hwiki.store import WikiStore  # noqa: E402


def _page(root: Path, rel: str, title: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\n---\n\n# {title}\n\nbody of {rel}\n", encoding="utf-8")


@pytest.fixture()
def store(tmp_path: Path) -> WikiStore:
    _page(tmp_path, "wiki/guides/release-process.md", "Release process")
    _page(tmp_path, "wiki/concepts/glossary.md", "Glossary")
    _page(tmp_path, "wiki/concepts/dup.md", "Dup in concepts")
    _page(tmp_path, "wiki/it/dup.md", "Dup in it")
    _page(tmp_path, "raw/release-process-notes.md", "Raw import")
    return WikiStore(tmp_path, untrusted_dirs=("raw",), memory_dir="wiki/memory")


def test_wrong_folder_path_falls_back_to_unique_slug(store: WikiStore) -> None:
    page = store.resolve_page("wiki/concepts/release-process.md")
    assert page is not None
    assert page.path == "wiki/guides/release-process.md"


def test_exact_path_still_wins(store: WikiStore) -> None:
    assert store.resolve_page("wiki/concepts/glossary.md").path == "wiki/concepts/glossary.md"


def test_bare_slug_and_wikilink_still_resolve(store: WikiStore) -> None:
    assert store.resolve_page("release-process").path == "wiki/guides/release-process.md"
    assert store.resolve_page("[[release-process]]").path == "wiki/guides/release-process.md"


def test_ambiguous_slug_is_not_guessed(store: WikiStore) -> None:
    """Two pages named dup.md: a wrong-folder path must not silently pick one."""
    assert store.resolve_page("wiki/guides/dup.md") is None


def test_unknown_page_still_none(store: WikiStore) -> None:
    assert store.resolve_page("wiki/concepts/no-such-page.md") is None
