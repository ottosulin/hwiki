"""The qmd backend keeps excluded folders out of qmd's own index.

These tests run the real ``qmd`` CLI (BM25 only, so no models are downloaded)
against a throwaway config dir and cache (``QMD_CONFIG_DIR``, ``XDG_CACHE_HOME``).
They are skipped when qmd is not installed; point ``HWIKI_QMD_BINARY`` at it
if it is not on ``PATH``.

"Never indexed" is checked on the index files themselves: the excluded text
must not appear anywhere in qmd's SQLite files, not just in search results.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import _hwiki_path  # noqa: F401
import pytest

import sys

from hwiki.search import QmdBackend, _globs_for_qmd, qmd_ignore_patterns
from hwiki.store import WikiStore

ALLOWED = "allowedwombat"
RAW = "rawpangolin"
PRIVATE = "privatenarwhal"
INDEX = "hwiki-test"


def qmd_binary() -> str:
    binary = os.environ.get("HWIKI_QMD_BINARY") or shutil.which("qmd") or ""
    if not binary or not Path(binary).exists() and not shutil.which(binary):
        pytest.skip("qmd is not installed (set HWIKI_QMD_BINARY)")
    return binary


@pytest.fixture
def qmd_env(tmp_path, monkeypatch):
    config_dir = tmp_path / "qmd-config"
    cache_dir = tmp_path / "qmd-cache"
    monkeypatch.setenv("QMD_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.delenv("INDEX_PATH", raising=False)
    return {"config": config_dir, "cache": cache_dir, "binary": qmd_binary()}


@pytest.fixture
def wiki(tmp_path):
    root = tmp_path / "wiki"
    (root / "notes").mkdir(parents=True)
    (root / "raw").mkdir()
    (root / "private").mkdir()
    (root / "notes" / "a.md").write_text(f"# Allowed\n\nThe {ALLOWED} lives here.\n", encoding="utf-8")
    (root / "raw" / "b.md").write_text(f"# Raw\n\nThe {RAW} is third-party.\n", encoding="utf-8")
    (root / "private" / "c.md").write_text(f"# Private\n\nThe {PRIVATE} is private.\n", encoding="utf-8")
    return root


def index_bytes(cache_dir: Path) -> bytes:
    files = sorted((cache_dir / "qmd").glob(f"{INDEX}.sqlite*"))
    assert files, "qmd created no index files"
    return b"".join(f.read_bytes() for f in files)


def raw_qmd_search(binary: str, term: str) -> list:
    """Ask qmd directly, bypassing hwiki's query-time filter."""
    proc = subprocess.run([binary, "--index", INDEX, "search", term, "--json"],
                          capture_output=True, text=True, timeout=60, check=False)
    text = proc.stdout.strip()
    return json.loads(text[text.find("["):]) if "[" in text else []


def backend(store: WikiStore, binary: str) -> QmdBackend:
    return QmdBackend(store, collection="wiki", binary=binary, mode="search", timeout=30, index=INDEX)


def test_excluded_folders_are_never_indexed_by_qmd(qmd_env, wiki):
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()

    data = index_bytes(qmd_env["cache"])
    assert ALLOWED.encode() in data
    assert RAW.encode() not in data
    assert PRIVATE.encode() not in data
    assert raw_qmd_search(qmd_env["binary"], RAW) == []
    assert raw_qmd_search(qmd_env["binary"], PRIVATE) == []
    assert [h.path for h in backend(store, qmd_env["binary"]).search(ALLOWED)] == ["notes/a.md"]


def test_newly_excluded_folder_is_purged_from_the_index(qmd_env, wiki):
    before = WikiStore(wiki, untrusted_dirs=[], exclude=[])
    backend(before, qmd_env["binary"]).sync()
    assert PRIVATE.encode() in index_bytes(qmd_env["cache"])

    after = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(after, qmd_env["binary"]).sync()

    data = index_bytes(qmd_env["cache"])
    assert PRIVATE.encode() not in data
    assert RAW.encode() not in data
    assert ALLOWED.encode() in data


EMBED_MODEL_FILE = "hf_ggml-org_embeddinggemma-300M-Q8_0.gguf"


def embed_models_dir() -> Path:
    """Opt-in: a folder that already holds qmd's embedding model (no download in tests)."""
    folder = os.environ.get("HWIKI_QMD_EMBED_MODELS", "")
    if not folder:
        pytest.skip("set HWIKI_QMD_EMBED_MODELS to a folder holding " + EMBED_MODEL_FILE)
    model = Path(folder).expanduser() / EMBED_MODEL_FILE
    if not model.is_file():
        pytest.skip(f"{model} not found")
    return model


def vector_state(cache_dir: Path, rel_path: str) -> dict:
    """What qmd's index holds for one document: its rows and raw embedding bytes."""
    import sqlite3

    db = sqlite3.connect(str(cache_dir / "qmd" / f"{INDEX}.sqlite"))
    try:
        row = db.execute("SELECT hash FROM documents WHERE path = ?", (rel_path,)).fetchone()
        doc_hash = row[0] if row else None
        state = {"document": row is not None, "content_vectors": 0, "vec_rowids": [], "vectors": []}
        if not doc_hash:
            return state
        state["content_vectors"] = db.execute(
            "SELECT COUNT(*) FROM content_vectors WHERE hash = ?", (doc_hash,)).fetchone()[0]
        size = db.execute("SELECT size FROM vectors_vec_chunks LIMIT 1").fetchone()[0]
        # sqlite-vec's vec0 table keeps ids in a plain shadow table and vectors in
        # fixed-size chunk blobs, so both can be read without loading the extension.
        rows = db.execute(
            "SELECT chunk_id, chunk_offset FROM vectors_vec_rowids WHERE substr(id, 1, ?) = ?",
            (len(doc_hash) + 1, doc_hash + "_")).fetchall()
        for chunk_id, offset in rows:
            blob = db.execute("SELECT vectors FROM vectors_vec_vector_chunks00 WHERE rowid = ?",
                              (chunk_id,)).fetchone()[0]
            width = len(blob) // size
            state["vec_rowids"].append(offset)
            state["vectors"].append(bytes(blob[offset * width:(offset + 1) * width]))
        return state
    finally:
        db.close()


def test_newly_excluded_folder_is_purged_from_embeddings(qmd_env, wiki):
    """Opt-in (real embedding model): exclusion also removes vectors, not just rows.

    Index and embed everything, then exclude ``private/`` and sync again. The
    excluded document's rows must be gone, and its raw embedding bytes must not
    remain anywhere in qmd's database files.
    """
    model = embed_models_dir()
    models = qmd_env["cache"] / "qmd" / "models"
    models.mkdir(parents=True)
    (models / EMBED_MODEL_FILE).symlink_to(model)

    def vsearch_backend(store):
        return QmdBackend(store, collection="wiki", binary=qmd_env["binary"], mode="vsearch",
                          timeout=120, index=INDEX)

    vsearch_backend(WikiStore(wiki, untrusted_dirs=[], exclude=[])).sync()
    before = vector_state(qmd_env["cache"], "private/c.md")
    assert before["document"] and before["content_vectors"] >= 1
    assert before["vectors"] and all(any(v) for v in before["vectors"])
    assert vector_state(qmd_env["cache"], "notes/a.md")["vectors"]

    vsearch_backend(WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])).sync()

    after = vector_state(qmd_env["cache"], "private/c.md")
    assert after == {"document": False, "content_vectors": 0, "vec_rowids": [], "vectors": []}
    data = index_bytes(qmd_env["cache"])
    for vector in before["vectors"]:
        assert vector not in data
    assert PRIVATE.encode() not in data
    assert vector_state(qmd_env["cache"], "notes/a.md")["vectors"]  # allowed vectors kept


def test_deleted_file_is_purged_from_the_index(qmd_env, wiki):
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()
    (wiki / "notes" / "a.md").unlink()
    backend(store, qmd_env["binary"]).sync()
    assert ALLOWED.encode() not in index_bytes(qmd_env["cache"])


def test_symlinks_into_an_excluded_folder_are_not_indexed(qmd_env, wiki):
    (wiki / "notes" / "dir-link").symlink_to(wiki / "private", target_is_directory=True)
    (wiki / "notes" / "file-link.md").symlink_to(wiki / "private" / "c.md")
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()

    assert PRIVATE.encode() not in index_bytes(qmd_env["cache"])
    assert raw_qmd_search(qmd_env["binary"], PRIVATE) == []


def test_default_qmd_index_is_never_touched(qmd_env, wiki):
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()
    assert (qmd_env["config"] / f"{INDEX}.yml").is_file()
    assert not (qmd_env["config"] / "index.yml").exists()
    assert not (qmd_env["cache"] / "qmd" / "index.sqlite").exists()


def test_project_local_qmd_config_is_not_adopted(qmd_env, wiki, tmp_path, monkeypatch):
    project = tmp_path / "some-project"
    (project / ".qmd").mkdir(parents=True)
    (project / ".qmd" / "index.yml").write_text(
        f"collections:\n  wiki:\n    path: {json.dumps(str(wiki))}\n    pattern: \"**/*.md\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()

    assert not (project / ".qmd" / "index.sqlite").exists()
    assert PRIVATE.encode() not in index_bytes(qmd_env["cache"])


def test_index_config_keeps_other_keys(qmd_env, wiki):
    pytest.importorskip("yaml")
    import yaml

    qmd_env["config"].mkdir(parents=True)
    path = qmd_env["config"] / f"{INDEX}.yml"
    path.write_text("models:\n  embed: hf:example/model.gguf\ncollections:\n  stale:\n    path: /tmp\n",
                    encoding="utf-8")
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    qmd = backend(store, qmd_env["binary"])
    assert qmd.ensure_collection()

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["models"] == {"embed": "hf:example/model.gguf"}
    assert list(data["collections"]) == ["wiki"]
    assert data["collections"]["wiki"]["path"] == str(wiki.resolve())
    assert "raw/**" in data["collections"]["wiki"]["ignore"]


TRICKY = {
    # path: (contents token, excluded by hwiki?)
    "notes/a.md": ALLOWED,
    "private/team-secret.md": "teamsecretlynx",      # exclude private*.md, * crosses /
    "privatex.md": "privatexibex",                   # exclude private*.md
    "notes/a/b.md": "slashqmark",                    # exclude notes/a?b.md, ? matches /
    "notes/axb.md": "plainqmark",                    # exclude notes/a?b.md
    "raw(foo)/x.md": "parenraw",                     # untrusted raw(foo)
    "raw{a,b}/y.md": "braceraw",                     # untrusted raw{a,b}
    "rawa/z.md": "notbraceexpanded",                 # NOT excluded: braces are literal
    "deep/x/99 - Local Private/p.md": "nestedprivate",  # exclude *99 - Local Private/*
    "keep/notes.md": "keepnotes",                    # not excluded
    "cls/a.md": "classliteralcaret",                 # exclude cls/[^a].md: ^ is literal in fnmatch
    "cls/b.md": "classb",                            # NOT excluded
    "neg/zy.md": "negatedclass",                     # exclude neg/[!x]y.md
    "neg/xy.md": "negatedkeep",                      # NOT excluded
    "negy.md/a.md": "unrelated",                     # NOT excluded
    "neg/y.md": "negatedslash",                      # exclude neg[!x]y.md: [!x] matches /
}
TRICKY_EXCLUDE = ["private*.md", "notes/a?b.md", "*99 - Local Private/*", "cls/[^a].md",
                  "neg/[!x]y.md", "neg[!x]y.md"]
TRICKY_UNTRUSTED = ["raw(foo)", "raw{a,b}"]


def test_qmd_never_indexes_what_hwiki_excludes(qmd_env, tmp_path):
    root = tmp_path / "tricky"
    for rel, token in TRICKY.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"# Page\n\nThe {token} is here.\n", encoding="utf-8")
    store = WikiStore(root, untrusted_dirs=TRICKY_UNTRUSTED, exclude=TRICKY_EXCLUDE)
    hwiki_paths = {store.relpath(p) for p in store.iter_paths()}
    backend(store, qmd_env["binary"]).sync()

    data = index_bytes(qmd_env["cache"])
    for rel, token in TRICKY.items():
        if rel in hwiki_paths:
            assert token.encode() in data, f"{rel} should be indexed by qmd"
        else:
            assert token.encode() not in data, f"{rel} is excluded by hwiki but qmd indexed it"
    # sanity: the fixture really exercises exclusions both ways
    assert "rawa/z.md" in hwiki_paths and "private/team-secret.md" not in hwiki_paths
    assert "cls/b.md" in hwiki_paths and "cls/a.md" not in hwiki_paths
    assert "neg/xy.md" in hwiki_paths and "neg/y.md" not in hwiki_paths


def test_inherited_index_path_does_not_redirect_qmd(qmd_env, wiki, tmp_path, monkeypatch):
    protected = tmp_path / "someone-elses.sqlite"
    protected.write_bytes(b"not a qmd database")
    monkeypatch.setenv("INDEX_PATH", str(protected))
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()
    assert protected.read_bytes() == b"not a qmd database"
    assert ALLOWED.encode() in index_bytes(qmd_env["cache"])


def failing_cleanup_wrapper(tmp_path: Path, real: str, flag: Path) -> str:
    """A qmd that fails `cleanup` while ``flag`` exists, and is the real qmd otherwise."""
    wrapper = tmp_path / "qmd-wrapper"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do if [ "$a" = cleanup ] && [ -e {json.dumps(str(flag))} ]; then exit 1; fi; done\n'
        f'exec {json.dumps(real)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def test_failed_cleanup_is_retried_until_it_succeeds(qmd_env, wiki, tmp_path):
    before = WikiStore(wiki, untrusted_dirs=[], exclude=[])
    backend(before, qmd_env["binary"]).sync()
    assert PRIVATE.encode() in index_bytes(qmd_env["cache"])

    flag = tmp_path / "fail-cleanup"
    flag.touch()
    wrapper = failing_cleanup_wrapper(tmp_path, qmd_env["binary"], flag)
    after = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(after, wrapper).sync()  # cleanup fails
    assert (qmd_env["config"] / f"{INDEX}.hwiki-cleanup-pending").exists()

    flag.unlink()
    backend(after, wrapper).sync()  # a new process: nothing removed now, but cleanup is owed
    assert not (qmd_env["config"] / f"{INDEX}.hwiki-cleanup-pending").exists()
    assert PRIVATE.encode() not in index_bytes(qmd_env["cache"])


def crashing_update_wrapper(tmp_path: Path, real: str, flag: Path) -> str:
    """A qmd whose `update` does its work and then fails while ``flag`` exists (a crash after commit)."""
    wrapper = tmp_path / "qmd-crash"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'{json.dumps(real)} "$@"; rc=$?\n'
        f'for a in "$@"; do if [ "$a" = update ] && [ -e {json.dumps(str(flag))} ]; then exit 1; fi; done\n'
        "exit $rc\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return str(wrapper)


def test_update_interrupted_after_removals_still_gets_its_cleanup(qmd_env, wiki, tmp_path):
    backend(WikiStore(wiki, untrusted_dirs=[], exclude=[]), qmd_env["binary"]).sync()
    assert PRIVATE.encode() in index_bytes(qmd_env["cache"])

    flag = tmp_path / "crash-update"
    flag.touch()
    wrapper = crashing_update_wrapper(tmp_path, qmd_env["binary"], flag)
    after = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(after, wrapper).sync()  # update deactivates the files, then "crashes"

    flag.unlink()
    backend(after, qmd_env["binary"]).sync()  # new process: config unchanged, 0 removed now
    assert PRIVATE.encode() not in index_bytes(qmd_env["cache"])
    assert not (qmd_env["config"] / f"{INDEX}.hwiki-cleanup-pending").exists()


def test_qmd_default_index_name_is_refused(qmd_env, wiki):
    qmd_env["config"].mkdir(parents=True)
    default = qmd_env["config"] / "index.yml"
    original = "collections:\n  notes:\n    path: /somewhere/else\n    pattern: \"**/*.md\"\n"
    default.write_text(original, encoding="utf-8")
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    for name in ("index", "INDEX"):
        qmd = QmdBackend(store, collection="wiki", binary=qmd_env["binary"], index=name)
        assert qmd.is_available() is False
        assert qmd.sync() == {}
    assert default.read_text(encoding="utf-8") == original
    assert not (qmd_env["cache"] / "qmd" / "index.sqlite").exists()


def test_without_pyyaml_an_existing_config_is_not_overwritten(qmd_env, wiki, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    qmd_env["config"].mkdir(parents=True)
    path = qmd_env["config"] / f"{INDEX}.yml"
    original = "models:\n  embed: hf:example/model.gguf\n"
    path.write_text(original, encoding="utf-8")
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    qmd = backend(store, qmd_env["binary"])
    assert qmd.ensure_collection() is False
    assert path.read_text(encoding="utf-8") == original


def test_without_pyyaml_a_new_config_is_written_and_works(qmd_env, wiki, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    store = WikiStore(wiki, untrusted_dirs=["raw"], exclude=["private/*"])
    backend(store, qmd_env["binary"]).sync()
    data = index_bytes(qmd_env["cache"])
    assert ALLOWED.encode() in data and PRIVATE.encode() not in data and RAW.encode() not in data


@pytest.mark.parametrize("name", ["../other", "a/b", "", ".hidden", "x y", "-flag"])
def test_invalid_index_names_disable_qmd_without_touching_files(tmp_path, monkeypatch, wiki, name):
    monkeypatch.setenv("QMD_CONFIG_DIR", str(tmp_path / "cfg" / "qmd"))
    store = WikiStore(wiki, untrusted_dirs=["raw"])
    qmd = QmdBackend(store, collection="wiki", binary=sys.executable, index=name or "x y")
    assert qmd.is_available() is False
    assert qmd.ensure_collection() is False
    assert qmd.sync() == {}
    assert not (tmp_path / "cfg").exists()


@pytest.mark.parametrize("pattern, expected", [
    (".git/*", [".git/*", ".git/*/**/*"]),
    ("log.md", ["log.md"]),
    ("/top/x.md", ["top/x.md"]),
    ("a?b", ["a?b", "a/b"]),
    ("raw(x)/*", ["raw\\(x\\)/*", "raw\\(x\\)/*/**/*"]),
    ("x[0-9].md", ["x[0-9].md"]),
    ("x[^a].md", ["x[\\^a].md"]),
    ("x[!a].md", ["x[^a].md", "x/.md"]),
])
def test_exclude_glob_variants(pattern, expected):
    assert _globs_for_qmd(pattern) == expected


def test_many_wildcards_fall_back_to_the_directory():
    assert _globs_for_qmd("docs/a*b*c*d*e*f*g*h.md") == ["docs/**"]
    assert _globs_for_qmd("docs/[ab]/a*b*c*d*e*f*g*h.md") == ["docs/**"]
    assert _globs_for_qmd("*a*b*c*d*e*f*") == ["**"]


def test_ignore_patterns_cover_untrusted_and_default_excludes(tmp_path):
    store = WikiStore(tmp_path, untrusted_dirs=["raw", "vendor/docs"], exclude=["private/*"])
    patterns = qmd_ignore_patterns(store)
    for expected in ("raw/**", "vendor/docs/**", "private/*", "private/*/**/*", ".obsidian/*",
                     "*/**/*/.git/*"):
        assert expected in patterns
