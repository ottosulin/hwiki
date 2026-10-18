"""``staging_path``: agent writes land outside the wiki, and the wiki is never written.

Every test snapshots the whole wiki tree (paths, sizes, mtimes) before and after,
so "never written" is checked on disk rather than inferred from return values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import _hwiki_path  # noqa: F401
import pytest

from hwiki import WikiMemoryProvider
from hwiki import config as cfg

TOKEN = "quokkaplanet"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in list(cfg._ENV_OVERRIDES) + list(cfg._LEGACY_ENV_OVERRIDES):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def wiki(tmp_path):
    root = tmp_path / "wiki"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "release.md").write_text(
        "# Release process\n\nTag the release, then publish the notes.\n", encoding="utf-8"
    )
    return root


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            stat = path.lstat()
            out[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return out


def make_provider(tmp_path, wiki, **overrides):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = dict(cfg.DEFAULTS, wiki_path=str(wiki), _hermes_home=str(home))
    config.update(overrides)
    provider = WikiMemoryProvider(config=config)
    provider.initialize("sess-1", hermes_home=str(home))
    return provider


def call(provider, tool, **args):
    return json.loads(provider.handle_tool_call(tool, args))


def test_write_goes_to_staging_and_wiki_is_untouched(tmp_path, wiki):
    staging = tmp_path / "staging"
    before = snapshot(wiki)  # before initialize(): start-up must not write the wiki either
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))

    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.",
               page="decisions")

    assert res["success"] is True
    assert res["staged"] is True
    assert Path(res["path"]) == staging.resolve() / "decisions.md"
    assert TOKEN in (staging / "decisions.md").read_text(encoding="utf-8")
    assert snapshot(wiki) == before
    assert not (wiki / "wiki" / "memory").exists()
    provider.shutdown()


def test_duplicate_staged_fact_is_not_appended_twice(tmp_path, wiki):
    staging = tmp_path / "staging"
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    res = call(provider, "wiki_memory_write", content=f"the {TOKEN}   release ships on fridays.")
    assert res["duplicate"] is True
    assert (staging / "journal.md").read_text(encoding="utf-8").count(TOKEN) == 1
    provider.shutdown()


def test_secret_is_refused_in_staging_too(tmp_path, wiki):
    staging = tmp_path / "staging"
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    assert call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")["staged"]
    before = snapshot(staging)
    res = call(provider, "wiki_memory_write",
               content="deploy key ghp_" + "a" * 36 + " for the bot")
    assert res.get("success") is not True
    assert snapshot(staging) == before
    provider.shutdown()


def test_staged_page_symlinked_into_the_wiki_is_not_written(tmp_path, wiki):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "journal.md").symlink_to(wiki / "wiki" / "concepts" / "release.md")
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))

    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")

    assert res.get("success") is not True
    assert "symlink" in res["error"]
    assert snapshot(wiki) == before
    provider.shutdown()


def test_staged_lockfile_symlinked_into_the_wiki_is_not_written(tmp_path, wiki):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "journal.md.lock").symlink_to(wiki / "wiki" / "concepts" / "release.md")
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    assert res.get("success") is not True
    assert snapshot(wiki) == before
    provider.shutdown()


def test_staging_folder_replaced_by_a_symlink_after_start_is_refused(tmp_path, wiki):
    staging = tmp_path / "staging"
    staging.mkdir()
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    staging.rmdir()
    staging.symlink_to(wiki / "wiki", target_is_directory=True)

    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    provider.on_memory_write("add", "memory", f"User prefers the {TOKEN} theme.")

    assert res.get("success") is not True
    assert snapshot(wiki) == before
    provider.shutdown()


def test_staging_parent_swapped_between_check_and_open_is_refused(tmp_path, wiki, monkeypatch):
    """The folder is swapped after validation but before the write opens it."""
    parent = tmp_path / "parent"
    staging = parent / "staging"
    staging.mkdir(parents=True)
    decoy = wiki / "wiki" / "decoy"
    (decoy / "staging").mkdir(parents=True)
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    store = provider._store
    checked = store.staging_root

    def validate_then_swap():
        parent.rename(tmp_path / "parent-old")
        parent.symlink_to(decoy, target_is_directory=True)
        return checked  # the check already passed with the real folder

    monkeypatch.setattr(store, "_resolve_staging", validate_then_swap)
    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")

    assert (tmp_path / "parent").is_symlink(), "the swap did not happen"
    assert res.get("success") is not True
    assert snapshot(wiki) == before
    provider.shutdown()


def test_index_inside_the_wiki_is_refused_when_staging(tmp_path, wiki):
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(tmp_path / "staging"),
                             index_path=str(wiki / "hwiki-index.sqlite"))
    assert "index_path" in provider.system_prompt_block()
    assert snapshot(wiki) == before
    provider.shutdown()

    config = dict(cfg.DEFAULTS, wiki_path=str(wiki), staging_path=str(tmp_path / "staging"),
                  index_path=str(wiki / "i.sqlite"), _hermes_home=str(tmp_path))
    with pytest.raises(ValueError, match="index_path"):
        cfg.build_runtime(config)
    assert snapshot(wiki) == before


def test_blank_staging_path_is_refused(tmp_path, wiki):
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path="   ")
    assert "blank" in provider.system_prompt_block()
    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    assert res.get("success") is not True
    assert snapshot(wiki) == before
    provider.shutdown()


def test_backup_paths_include_staging_outside_hermes_home(tmp_path, wiki):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "staging"
    outside.mkdir()
    inside = home / "hwiki-staging"
    inside.mkdir()
    base = dict(cfg.DEFAULTS, wiki_path=str(wiki), _hermes_home=str(home))

    provider = WikiMemoryProvider(config=dict(base, staging_path=str(outside)))
    assert provider.backup_paths() == [str(wiki), str(outside)]  # before initialize()

    provider = WikiMemoryProvider(config=dict(base, staging_path="$HERMES_HOME/hwiki-staging"))
    assert provider.backup_paths() == [str(wiki)]  # HERMES_HOME is backed up anyway


def test_builtin_memory_mirror_goes_to_staging(tmp_path, wiki):
    staging = tmp_path / "staging"
    before = snapshot(wiki)  # before initialize(): start-up must not write the wiki either
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))

    provider.on_memory_write("add", "memory", f"User prefers the {TOKEN} theme.")

    assert TOKEN in (staging / "builtin-mirror.md").read_text(encoding="utf-8")
    assert snapshot(wiki) == before
    provider.shutdown()


def test_staged_fact_is_not_recalled(tmp_path, wiki):
    staging = tmp_path / "staging"
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")

    found = call(provider, "wiki_memory_search", query=TOKEN)
    assert found["count"] == 0
    assert TOKEN not in provider.prefetch(f"when does the {TOKEN} release ship")
    # the curated page is still recalled
    assert call(provider, "wiki_memory_search", query="release process")["count"] >= 1
    provider.shutdown()


def test_status_prompt_and_schema_name_the_staging_folder(tmp_path, wiki):
    staging = tmp_path / "staging"
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))
    call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")

    status = call(provider, "wiki_memory_status")
    assert status["staging_path"] == str(staging.resolve())
    assert status["memory_pages"] == ["journal.md"]

    prompt = provider.system_prompt_block()
    assert str(staging.resolve()) in prompt
    assert "wiki/memory" not in prompt

    write_schema = next(s for s in provider.get_tool_schemas() if s["name"] == "wiki_memory_write")
    assert "staging folder outside the wiki" in write_schema["description"]
    provider.shutdown()


@pytest.mark.parametrize("inside", ["wiki/memory", "", "wiki"])
def test_staging_inside_the_wiki_is_refused(tmp_path, wiki, inside):
    staging = wiki / inside if inside else wiki
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(staging))

    assert "outside the wiki root" in provider.system_prompt_block()
    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    assert res.get("success") is not True
    assert snapshot(wiki) == before
    provider.shutdown()


def test_staging_symlink_into_the_wiki_is_refused(tmp_path, wiki):
    link = tmp_path / "looks-outside"
    link.symlink_to(wiki / "wiki", target_is_directory=True)
    before = snapshot(wiki)
    provider = make_provider(tmp_path, wiki, staging_path=str(link))

    assert "outside the wiki root" in provider.system_prompt_block()
    call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    assert snapshot(wiki) == before
    provider.shutdown()


def test_relative_staging_path_is_refused(tmp_path, wiki):
    provider = make_provider(tmp_path, wiki, staging_path="staging")
    assert "absolute path" in provider.system_prompt_block()
    provider.shutdown()


def test_staging_path_expands_hermes_home_and_env_override(tmp_path, wiki, monkeypatch):
    provider = make_provider(tmp_path, wiki, staging_path="$HERMES_HOME/hwiki-staging")
    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    assert Path(res["path"]).parent == (tmp_path / "home" / "hwiki-staging").resolve()
    provider.shutdown()

    monkeypatch.setenv("HWIKI_STAGING_PATH", str(tmp_path / "from-env"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert cfg.load_config()["staging_path"] == str(tmp_path / "from-env")


def test_build_runtime_rejects_staging_inside_the_wiki(tmp_path, wiki):
    config = dict(cfg.DEFAULTS, wiki_path=str(wiki), staging_path=str(wiki / "x"),
                  _hermes_home=str(tmp_path))
    with pytest.raises(ValueError, match="outside the wiki root"):
        cfg.build_runtime(config)


def test_without_staging_writes_still_go_to_memory_dir(tmp_path, wiki):
    provider = make_provider(tmp_path, wiki)
    res = call(provider, "wiki_memory_write", content=f"The {TOKEN} release ships on Fridays.")
    assert res["staged"] is False
    assert res["path"] == "wiki/memory/journal.md"
    assert TOKEN in (wiki / "wiki" / "memory" / "journal.md").read_text(encoding="utf-8")
    provider.shutdown()
