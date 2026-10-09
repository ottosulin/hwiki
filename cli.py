"""``hermes hwiki ...`` CLI for the hwiki memory provider.

Discovered by ``discover_plugin_cli_commands()`` when ``memory.provider`` is
``hwiki``.

IMPORTANT — do not "simplify" the imports below to ``from . import X``.
Hermes loads this file with the plugin's parent registered as an EMPTY
synthetic package (``__init__.py`` is never executed), so importing a symbol
from the package itself raises::

    ImportError: cannot import name 'load_config'
                 from '_hermes_user_memory.hwiki' (unknown location)

Sibling submodules (``from .config import ...``) resolve correctly because the
package shell carries ``submodule_search_locations``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from .config import CONFIG_FILENAME, build_runtime, load_config


def _load():
    """Resolve (config, store, backend), printing guidance if unconfigured."""
    config, store, backend = build_runtime(load_config())
    if store is None:
        raw = config.get("wiki_path") or "(unset)"
        print(f"\n  wiki_path is not set or does not exist: {raw}")
        print(f"  Config file: {Path(config.get('_hermes_home', '')) / CONFIG_FILENAME}")
        print("  Run `hermes memory setup` and select 'hwiki'.\n")
    return config, store, backend


def _cmd_status() -> None:
    config, store, backend = _load()
    if store is None:
        return
    backend.sync()
    counts = backend.count() if hasattr(backend, "count") else {}
    memory_root = store.memory_root()
    pages = sorted(p.name for p in memory_root.glob("*.md")) if memory_root.is_dir() else []
    print("\nWiki memory\n" + "-" * 46)
    print(f"  Root:      {store.root}")
    print(f"  Backend:   {backend.name}")
    print(f"  Indexed:   {counts.get('pages', 0)} pages / {counts.get('chunks', 0)} chunks")
    print(f"  Memory:    {store.memory_dir}  ({len(pages)} page(s))")
    for name in pages:
        print(f"               - {name}")
    print(f"  Untrusted: {', '.join(store.untrusted_dirs) or '(none)'} (never indexed)")
    print()


def _cmd_index() -> None:
    config, store, backend = _load()
    if store is None:
        return
    stats = backend.sync(force=True)
    counts = backend.count() if hasattr(backend, "count") else {}
    print(f"\n  Reindexed: {stats}")
    print(f"  Now holding {counts.get('pages', 0)} pages / {counts.get('chunks', 0)} chunks\n")


def _cmd_search(query: str, limit: int) -> None:
    config, store, backend = _load()
    if store is None:
        return
    if not query.strip():
        print("\n  Usage: hermes hwiki search <terms>\n")
        return
    backend.sync()
    hits = backend.search(query, limit=limit)
    if not hits:
        print(f"\n  No matches for {query!r}\n")
        return
    print()
    for i, hit in enumerate(hits, 1):
        label = hit.title or hit.path
        if hit.heading and hit.heading != hit.title:
            label += f" › {hit.heading}"
        print(f"  {i}. {label}  [{hit.score:.3f} {hit.backend}]")
        print(f"     {hit.path}")
        print(f"     {hit.snippet[:220]}\n")


def _cmd_lint() -> None:
    config, store, backend = _load()
    if store is None:
        return
    dangling = store.dangling_links()
    _out, backlinks = store.link_graph()
    orphans = [p.slug for p in store.iter_pages()
               if not backlinks.get(p.slug) and p.slug != "index"]
    print("\nWiki lint\n" + "-" * 46)
    if dangling:
        print(f"  Dangling wikilinks ({sum(len(v) for v in dangling.values())}):")
        for source, targets in sorted(dangling.items()):
            print(f"    {source} -> {', '.join(targets)}")
    else:
        print("  Dangling wikilinks: none")
    print(f"  Orphan pages ({len(orphans)}): {', '.join(sorted(orphans)[:20]) or 'none'}")
    print()


def _cmd_config() -> None:
    config = load_config()
    home = config.pop("_hermes_home", "")
    print(f"\n  Config file: {Path(home) / CONFIG_FILENAME}")
    print(json.dumps(config, indent=2))
    print()


def _table_columns(conn: sqlite3.Connection, table: str) -> list:
    """Return column names for a table, or [] if it doesn't exist."""
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def _read_holographic_facts(conn: sqlite3.Connection) -> list:
    """Read facts from a holographic store, adapting to its actual schema.

    The real schema uses ``fact_id`` as the primary key (NOT ``id``) and also
    carries ``retrieval_count`` / ``helpful_count`` / timestamps. Older or
    hand-modified databases vary, so every column is probed via
    ``PRAGMA table_info`` instead of assumed — a hardcoded SELECT fails the
    whole migration with ``no such column``.
    """
    columns = _table_columns(conn, "facts")
    if not columns:
        raise sqlite3.Error("no 'facts' table in this database")
    if "content" not in columns:
        raise sqlite3.Error(f"'facts' table has no 'content' column (found: {', '.join(columns)})")

    pk = next((c for c in ("fact_id", "id", "rowid") if c in columns), None)

    select = ["content"]
    select.append("category" if "category" in columns else "'general' AS category")
    select.append("tags" if "tags" in columns else "'' AS tags")
    select.append("COALESCE(trust_score, 0.5) AS trust" if "trust_score" in columns
                  else "0.5 AS trust")
    select.append("COALESCE(created_at, '') AS created_at" if "created_at" in columns
                  else "'' AS created_at")
    select.append(f"{pk} AS pk" if pk else "NULL AS pk")

    order = f" ORDER BY {pk}" if pk else ""
    rows = conn.execute(f"SELECT {', '.join(select)} FROM facts{order}").fetchall()

    # Entity links make far better wiki tags than the mostly-empty tags column.
    entities: dict = {}
    if pk and _table_columns(conn, "entities") and _table_columns(conn, "fact_entities"):
        try:
            for row in conn.execute(
                "SELECT fe.fact_id AS fid, e.name AS name "
                "FROM fact_entities fe JOIN entities e ON e.entity_id = fe.entity_id"
            ).fetchall():
                entities.setdefault(row["fid"], []).append(row["name"])
        except sqlite3.Error:
            entities = {}

    out = []
    for row in rows:
        out.append({
            "content": (row["content"] or "").strip(),
            "category": (row["category"] or "general").strip() or "general",
            "tags": row["tags"] or "",
            "trust": float(row["trust"] or 0.0),
            "created_at": str(row["created_at"] or "")[:10],
            "entities": entities.get(row["pk"], []),
        })
    return out


def _cmd_migrate(db_path: str, page: str, dry_run: bool, min_trust: float = 0.0) -> None:
    """Import facts from a holographic ``memory_store.db`` into the wiki."""
    config, store, backend = _load()
    if store is None:
        return

    source = Path(db_path).expanduser()
    if not source.is_file():
        print(f"\n  No such database: {source}\n")
        return

    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        facts = _read_holographic_facts(conn)
        conn.close()
    except sqlite3.Error as exc:
        print(f"\n  Could not read facts from {source}: {exc}\n")
        return

    facts = [f for f in facts if f["content"]]
    kept = [f for f in facts if f["trust"] >= min_trust]
    filtered = len(facts) - len(kept)

    print(f"\n  Found {len(facts)} fact(s) in {source}")
    if filtered:
        print(f"  {filtered} below --min-trust {min_trust} (will be skipped)")

    by_category: dict = {}
    for fact in kept:
        by_category.setdefault(fact["category"], []).append(fact)
    if by_category:
        print("  By category: " + ", ".join(
            f"{cat}={len(items)}" for cat, items in sorted(by_category.items())))

    if dry_run:
        print()
        for cat, items in sorted(by_category.items()):
            print(f"  --- {page}-{cat}.md  ({len(items)}) ---")
            for fact in items:
                ents = f"  [{', '.join(fact['entities'][:4])}]" if fact["entities"] else ""
                print(f"    [{fact['trust']:.1f}] {fact['content'][:110]}{ents}")
            print()
        print("  Dry run — nothing written. Re-run without --dry-run to import.\n")
        return

    stamp = date.today().isoformat()
    imported = skipped = blocked = 0
    for fact in kept:
        tags = [t.strip() for t in (fact["tags"] or "").split(",") if t.strip()]
        tags += fact["entities"][:5]
        try:
            result = store.append_note(
                fact["content"],
                page=f"{page}-{fact['category']}",
                topic=f"Imported: {fact['category']}",
                tags=tags,
                source="holographic-import",
                today=fact["created_at"] or stamp,
            )
            if result["duplicate"]:
                skipped += 1
            else:
                imported += 1
        except ValueError as exc:
            blocked += 1
            print(f"    BLOCKED (secret-shaped): {fact['content'][:70]}...  ({exc})")

    backend.sync(force=True)
    print(f"\n  Imported {imported}, skipped {skipped} duplicate(s), blocked {blocked}.")
    print(f"  Review and curate: {store.memory_root()}\n")


def wiki_command(args) -> None:
    """argparse dispatch target."""
    sub = getattr(args, "wiki_command", None)
    if sub == "status":
        _cmd_status()
    elif sub == "index":
        _cmd_index()
    elif sub == "search":
        _cmd_search(" ".join(getattr(args, "query", []) or []), int(getattr(args, "limit", 6) or 6))
    elif sub == "lint":
        _cmd_lint()
    elif sub == "config":
        _cmd_config()
    elif sub == "migrate":
        explicit = str(getattr(args, "db", "") or "")
        if not explicit:
            config = load_config()
            explicit = str(Path(config.get("_hermes_home") or "") / "memory_store.db")
        _cmd_migrate(
            explicit,
            getattr(args, "page", "imported") or "imported",
            bool(getattr(args, "dry_run", False)),
            float(getattr(args, "min_trust", 0.0) or 0.0),
        )
    else:
        print("\n  Usage: hermes hwiki <status|index|search|lint|migrate|config>\n")


def register_cli(subparser) -> None:
    """Build the ``hermes hwiki`` argparse tree."""
    subs = subparser.add_subparsers(dest="wiki_command")

    subs.add_parser("status", help="Show wiki root, backend and index counts")
    subs.add_parser("index", help="Force a full reindex")
    subs.add_parser("lint", help="Report dangling wikilinks and orphan pages")
    subs.add_parser("config", help="Show resolved configuration")

    search = subs.add_parser("search", help="Search wiki memory")
    search.add_argument("query", nargs="+", help="Search terms")
    search.add_argument("--limit", type=int, default=6, help="Max results (default 6)")

    migrate = subs.add_parser("migrate", help="Import a holographic memory_store.db")
    migrate.add_argument("--db", default="", help="Path to memory_store.db")
    migrate.add_argument("--page", default="imported", help="Memory page prefix")
    migrate.add_argument("--dry-run", action="store_true", help="Preview without writing")
    migrate.add_argument("--min-trust", type=float, default=0.0,
                         help="Skip facts below this trust score (default 0.0 = import all)")

    subparser.set_defaults(func=wiki_command)


if __name__ == "__main__":  # manual invocation: python3 -m wiki.cli status
    class _Args:
        wiki_command = sys.argv[1] if len(sys.argv) > 1 else "status"
        query = sys.argv[2:]
        limit = 6
        db = ""
        page = "imported"
        dry_run = "--dry-run" in sys.argv
    wiki_command(_Args())
