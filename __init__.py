"""Wiki memory provider — a markdown wiki as Hermes Agent long-term memory.

Replaces opaque database-backed memory with a human-readable, git-versionable
markdown wiki (Obsidian-compatible). The agent recalls from the wiki before
each turn and files durable facts into a dedicated memory zone.

Design commitments
------------------
1. **The curated wiki is read-only to the agent.** Automatic writes land only
   under ``memory_dir``. Hand-maintained synthesis pages are never mutated.
2. **Untrusted zones are never read.** ``untrusted_dirs`` (default ``raw/``)
   holds third-party source material and is excluded from indexing and recall,
   because auto-injected context is the highest-value prompt-injection target.
3. **Recall must be fast and must never block a turn.** Hermes hard-timeouts
   ``prefetch()`` at 8s; the default FTS5 backend answers in single-digit ms
   and all work happens behind a bounded lock.
4. **Degrade, never fail.** No qmd, no node, no PyYAML, no wiki directory —
   every one of those is a warning plus reduced function, never an exception.

Config lives in ``$HERMES_HOME/hwiki.json``; see README.md.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - offline testing without Hermes installed
    class MemoryProvider:  # type: ignore[no-redef]
        """Minimal shim so the module imports outside a Hermes checkout."""

try:
    from tools.registry import tool_error
except Exception:  # pragma: no cover
    def tool_error(message: str) -> str:  # type: ignore[misc]
        return json.dumps({"success": False, "error": message})

from .store import WikiStore, scan_for_secrets, slugify
from .search import build_backend
from .config import (
    CONFIG_FILENAME,
    DEFAULTS,
    build_runtime,
    default_index_path,
    expand_path as _expand,
    load_config,
    resolve_hermes_home,
    save_config_file,
)

logger = logging.getLogger(__name__)

__all__ = [
    "WikiMemoryProvider", "register", "load_config", "build_runtime",
    "CONFIG_FILENAME", "DEFAULTS",
]


# ---------------------------------------------------------------------------
# Tool schemas  (bare OpenAI function shape — the manager wraps them)
# ---------------------------------------------------------------------------

WIKI_SEARCH_SCHEMA = {
    "name": "wiki_memory_search",
    "description": (
        "Search long-term memory: the markdown knowledge wiki. Use this BEFORE "
        "answering any question that durable notes may already cover (past "
        "decisions, people, systems, compliance posture, prior incidents, "
        "vendor choices, recurring answers).\n\n"
        "Returns ranked page excerpts with their wiki paths. Follow up with "
        "wiki_memory_read to load a full page when an excerpt looks relevant. "
        "Each result also lists that page's outbound [[wikilinks]] as "
        "follow-up hints — read a linked page when the excerpt is partial."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language question or keywords.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 6, max 20).",
            },
            "spine": {
                "type": "string",
                "description": "Optional frontmatter 'spine' filter (e.g. compliance, product).",
            },
        },
        "required": ["query"],
    },
}

WIKI_READ_SCHEMA = {
    "name": "wiki_memory_read",
    "description": (
        "Read a wiki page in full by path, slug, or [[wikilink]] name. Also "
        "returns the page's frontmatter, outbound links and backlinks so you "
        "can traverse the knowledge graph."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "page": {
                "type": "string",
                "description": "Page path ('wiki/concepts/le-chonk.md'), slug ('le-chonk'), or wikilink target.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Truncate the body at this many characters (default 8000).",
            },
        },
        "required": ["page"],
    },
}

WIKI_WRITE_SCHEMA = {
    "name": "wiki_memory_write",
    "description": (
        "File a durable fact into long-term memory. Writes a dated bullet into "
        "the wiki's memory zone — the curated wiki pages are never modified.\n\n"
        "WRITE when: the user states a preference or correction, a decision is "
        "made with a rationale, you learn a stable fact about the environment, "
        "or an interpretation should be reused next time.\n"
        "DO NOT write: secrets, tokens, credentials, personal data, task "
        "progress, or anything stale within a week.\n\n"
        "Group related facts by passing the same 'page'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact, as one self-contained declarative sentence.",
            },
            "page": {
                "type": "string",
                "description": "Memory page to append to (e.g. 'decisions', 'vendors'). Defaults to 'journal'.",
            },
            "topic": {
                "type": "string",
                "description": "Human-readable title used when the page is created.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags appended as #hashtags.",
            },
            "source": {
                "type": "string",
                "description": "Optional provenance (URL, ticket key, document id).",
            },
        },
        "required": ["content"],
    },
}

WIKI_STATUS_SCHEMA = {
    "name": "wiki_memory_status",
    "description": (
        "Inspect memory health: wiki root, active search backend, page/chunk "
        "counts, memory-zone pages, and knowledge-graph hygiene (dangling "
        "wikilinks, orphan pages). Use when asked what the agent remembers, "
        "or to debug recall."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lint": {
                "type": "boolean",
                "description": "Include dangling-wikilink and orphan-page report.",
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class WikiMemoryProvider(MemoryProvider):
    """Markdown wiki as Hermes long-term memory."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._config = config or load_config()
        self._store: Optional[WikiStore] = None
        self._backend = None
        self._session_id = ""
        self._hermes_home = str(self._config.get("_hermes_home") or "")
        self._lock = threading.RLock()
        self._last_index = 0.0
        self._pending: Dict[str, str] = {}      # session_id -> prefetched context
        self._writes_this_session = 0
        self._init_error = ""
        self._agent_context = "primary"
        self._read_only = False

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "hwiki"

    def is_available(self) -> bool:
        """True when a wiki root is configured and present. No network."""
        raw = str(self._config.get("wiki_path") or "")
        if not raw:
            return False
        home = self._hermes_home or os.environ.get("HERMES_HOME", "")
        return Path(_expand(raw, home)).is_dir()

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        home = kwargs.get("hermes_home") or self._hermes_home
        self._hermes_home = str(home or "")

        # Non-primary agent contexts (cron, subagent, flush) get READ-ONLY
        # memory. The ABC is explicit that letting them write corrupts the
        # store — a nightly cron would file its own system prompt as a
        # "durable fact" and quietly poison the wiki. Recall stays enabled so
        # scheduled jobs still benefit from what the primary agent learned.
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        self._read_only = self._agent_context != "primary"
        if self._read_only:
            logger.info(
                "wiki memory: agent_context=%s — recall enabled, writes disabled",
                self._agent_context,
            )

        raw_path = _expand(str(self._config.get("wiki_path") or ""), self._hermes_home)
        if not raw_path:
            self._init_error = (
                "wiki_path is not set — run `hermes memory setup` or set "
                f"wiki_path in {Path(self._hermes_home) / CONFIG_FILENAME}"
            )
            logger.warning("wiki memory: %s", self._init_error)
            return

        root = Path(raw_path)
        if not root.is_dir():
            self._init_error = f"wiki root does not exist: {root}"
            logger.warning("wiki memory: %s", self._init_error)
            return

        self._store = WikiStore(
            root,
            include=self._config.get("include") or ["**/*.md"],
            exclude=self._config.get("exclude") or [],
            untrusted_dirs=self._config.get("untrusted_dirs") or [],
            memory_dir=str(self._config.get("memory_dir") or "wiki/memory"),
        )

        index_path = (_expand(str(self._config.get("index_path") or ""), self._hermes_home)
                      or default_index_path(self._hermes_home))

        self._backend = build_backend(
            self._store,
            backend=str(self._config.get("backend") or "fts5"),
            db_path=index_path,
            qmd_binary=str(self._config.get("qmd_binary") or "qmd"),
            qmd_collection=str(self._config.get("qmd_collection") or "hwiki"),
            qmd_mode=str(self._config.get("qmd_mode") or "search"),
            qmd_timeout=float(self._config.get("qmd_timeout_s") or 6.0),
            max_hits_per_page=int(self._config.get("max_hits_per_page") or 2),
            demote_paths=list(self._config.get("demote_paths") or []),
            fetch_multiplier=int(self._config.get("fetch_multiplier") or 4),
        )

        try:
            stats = self._backend.sync()
            self._last_index = time.time()
            logger.info(
                "wiki memory: indexed %s (backend=%s) %s",
                root, self._backend.name, stats,
            )
        except Exception as exc:
            self._init_error = f"initial index failed: {exc}"
            logger.warning("wiki memory: %s", self._init_error)

    def shutdown(self) -> None:
        with self._lock:
            backend = self._backend
            self._backend = None
        if backend is not None and hasattr(backend, "close"):
            try:
                backend.close()
            except Exception as exc:
                logger.debug("wiki memory: close failed: %s", exc)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id
        if kwargs.get("reset"):
            self._writes_this_session = 0
        self._pending.pop(new_session_id, None)

    # The remaining ABC hooks are deliberate no-ops. They are declared
    # explicitly (rather than inherited) so the plugin behaves identically
    # when imported outside a Hermes checkout, where MemoryProvider is a shim.

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """No per-turn bookkeeping needed — recall is index-driven."""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """No end-of-session extraction.

        Auto-summarising a transcript into the wiki would degrade it into a
        chat log. Durable facts are filed deliberately via wiki_memory_write.
        """

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Contribute nothing to the compression summary."""
        return ""

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "",
                      **kwargs) -> None:
        """Subagent output is not auto-filed; the parent decides what matters."""

    def backup_paths(self) -> List[str]:
        """The wiki usually lives outside HERMES_HOME — declare it for backups."""
        raw = _expand(str(self._config.get("wiki_path") or ""), self._hermes_home)
        return [raw] if raw and Path(raw).is_dir() else []

    # -- indexing ----------------------------------------------------------

    def _maybe_reindex(self, *, force: bool = False) -> None:
        """Refresh the index at most once per ``reindex_interval_s``."""
        if self._backend is None:
            return
        interval = float(self._config.get("reindex_interval_s") or 60)
        if not force and (time.time() - self._last_index) < interval:
            return
        if not self._lock.acquire(blocking=False):
            return  # another thread is already syncing
        try:
            self._backend.sync()
            self._last_index = time.time()
        except Exception as exc:
            logger.debug("wiki memory: reindex failed: %s", exc)
        finally:
            self._lock.release()

    # -- system prompt -----------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._init_error:
            return (
                "# Wiki Memory\n"
                f"INACTIVE — {self._init_error}\n"
                "Long-term recall is unavailable this session; say so rather than "
                "claiming you have no memory of a topic."
            )
        if self._backend is None or self._store is None:
            return ""
        try:
            counts = self._backend.count()
        except Exception:
            counts = {"pages": 0, "chunks": 0}
        memory_dir = self._store.memory_dir
        return (
            "# Wiki Memory\n"
            f"Long-term memory is a markdown wiki at `{self._store.root}` "
            f"({counts.get('pages', 0)} pages indexed, backend={self._backend.name}).\n"
            "- `wiki_memory_search` — search it BEFORE answering anything durable notes may cover.\n"
            "- `wiki_memory_read` — open a page in full and follow its [[wikilinks]].\n"
            "- `wiki_memory_write` — file durable facts (preferences, decisions, "
            "stable environment facts). Never secrets or task progress.\n"
            f"Automatic writes land in `{memory_dir}/`; curated pages are edited by hand only."
        )

    # -- recall ------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._config.get("auto_recall", True):
            return ""
        cached = self._pending.pop(session_id or self._session_id, "")
        if cached:
            return cached
        return self._recall(query)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Warm the next turn's recall in the background."""
        if not self._config.get("auto_recall", True) or self._backend is None:
            return
        key = session_id or self._session_id

        def _run() -> None:
            try:
                self._maybe_reindex()
                result = self._recall(query)
                if result:
                    self._pending[key] = result
            except Exception as exc:
                logger.debug("wiki memory: queue_prefetch failed: %s", exc)

        threading.Thread(target=_run, daemon=True, name="wiki-memory-prefetch").start()

    def _recall(self, query: str) -> str:
        if self._backend is None:
            return ""
        query = (query or "").strip()
        if len(query) < int(self._config.get("min_query_chars") or 8):
            return ""
        self._maybe_reindex()
        try:
            hits = self._backend.search(
                query, limit=int(self._config.get("prefetch_limit") or 4)
            )
        except Exception as exc:
            logger.debug("wiki memory: recall failed: %s", exc)
            return ""
        if not hits:
            return ""

        budget = int(self._config.get("prefetch_max_chars") or 1400)
        lines = ["## Wiki Memory (recalled)"]
        used = 0
        for hit in hits:
            label = f"{hit.title or hit.path}"
            if hit.heading and hit.heading != hit.title:
                label += f" › {hit.heading}"
            snippet = hit.snippet[: max(0, budget - used)]
            if not snippet:
                break
            lines.append(f"- **{label}** (`{hit.path}`): {snippet}")
            used += len(snippet)
            if used >= budget:
                break
        lines.append("_Use wiki_memory_read for the full page before relying on an excerpt._")
        return "\n".join(lines)

    # -- writes ------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None) -> None:
        """No automatic transcript ingestion.

        Conversation dumps would bury curated synthesis under noise and make
        the wiki unreadable to a human — the whole point of this backend. The
        agent files facts explicitly via ``wiki_memory_write``.
        """
        return

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror built-in MEMORY.md/USER.md additions into the wiki."""
        if action != "add" or not content:
            return
        if self._read_only:
            return  # cron/subagent builtin writes must not reach the wiki
        if not self._config.get("mirror_builtin_memory", True):
            return
        if self._store is None:
            return
        if scan_for_secrets(content):
            logger.warning("wiki memory: skipped mirroring a secret-shaped builtin entry")
            return
        # This signature declares `metadata`, so MemoryManager's
        # _provider_memory_write_metadata_mode() resolves to "keyword" mode and
        # passes provenance (write_origin, session_id, platform, tool_name...).
        origin = ""
        if metadata:
            origin = str(metadata.get("write_origin") or metadata.get("platform") or "")
        try:
            self._store.append_note(
                content,
                page=str(self._config.get("mirror_page") or "builtin-mirror"),
                topic="Built-in memory mirror",
                source=f"builtin:{target}" + (f" via {origin}" if origin else ""),
            )
        except Exception as exc:
            logger.debug("wiki memory: mirror failed: %s", exc)

    # -- tools -------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = [WIKI_SEARCH_SCHEMA, WIKI_READ_SCHEMA, WIKI_WRITE_SCHEMA, WIKI_STATUS_SCHEMA]
        if self._read_only:
            # Don't advertise a tool we will refuse — a cron/subagent run
            # shouldn't waste turns discovering the write path is closed.
            schemas = [s for s in schemas if s is not WIKI_WRITE_SCHEMA]
        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._store is None or self._backend is None:
            return tool_error(
                self._init_error or "wiki memory is not initialized (no wiki_path configured)"
            )
        try:
            if tool_name == "wiki_memory_search":
                return self._tool_search(args)
            if tool_name == "wiki_memory_read":
                return self._tool_read(args)
            if tool_name == "wiki_memory_write":
                return self._tool_write(args)
            if tool_name == "wiki_memory_status":
                return self._tool_status(args)
        except KeyError as exc:
            return tool_error(f"missing required argument: {exc}")
        except ValueError as exc:
            return tool_error(str(exc))
        except Exception as exc:
            logger.exception("wiki memory: tool %s failed", tool_name)
            return tool_error(f"{tool_name} failed: {exc}")
        return tool_error(f"unknown tool: {tool_name}")

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("query is required")
        limit = max(1, min(int(args.get("limit") or 6), 20))
        self._maybe_reindex()
        hits = self._backend.search(query, limit=limit, spine=str(args.get("spine") or ""))
        return json.dumps({
            "success": True,
            "query": query,
            "backend": self._backend.name,
            "count": len(hits),
            "results": [hit.to_dict() for hit in hits],
            "hint": "Call wiki_memory_read for the full page before relying on a snippet.",
        }, ensure_ascii=False)

    def _tool_read(self, args: Dict[str, Any]) -> str:
        reference = str(args.get("page") or "").strip()
        if not reference:
            return tool_error("page is required")
        page = self._store.resolve_page(reference)
        if page is None:
            return tool_error(f"page not found: {reference}")
        max_chars = max(200, min(int(args.get("max_chars") or 8000), 40000))
        body = page.body
        truncated = len(body) > max_chars
        _, backlinks = self._store.link_graph()
        return json.dumps({
            "success": True,
            "path": page.path,
            "title": page.title,
            "frontmatter": {k: v for k, v in page.frontmatter.items()},
            "links": page.links(),
            "backlinks": sorted(set(backlinks.get(page.slug, []))),
            "truncated": truncated,
            "content": body[:max_chars],
        }, ensure_ascii=False, default=str)

    def _tool_write(self, args: Dict[str, Any]) -> str:
        if self._read_only:
            return tool_error(
                f"wiki memory is read-only in agent_context='{self._agent_context}'. "
                "Only the primary agent may write durable facts."
            )
        content = str(args.get("content") or "").strip()
        if not content:
            return tool_error("content is required")
        tags = args.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        result = self._store.append_note(
            content,
            topic=str(args.get("topic") or ""),
            page=str(args.get("page") or ""),
            tags=list(tags),
            source=str(args.get("source") or ""),
            today=date.today().isoformat(),
        )
        self._writes_this_session += 1
        self._maybe_reindex(force=True)
        return json.dumps({
            "success": True,
            "path": result["path"],
            "created_page": result["created"],
            "duplicate": result["duplicate"],
            "note": ("An equivalent fact was already recorded; nothing appended."
                     if result["duplicate"] else "Fact filed to long-term memory."),
        }, ensure_ascii=False)

    def _tool_status(self, args: Dict[str, Any]) -> str:
        self._maybe_reindex()
        counts = self._backend.count() if hasattr(self._backend, "count") else {}
        memory_root = self._store.memory_root()
        memory_pages = (
            sorted(p.name for p in memory_root.glob("*.md")) if memory_root.is_dir() else []
        )
        payload: Dict[str, Any] = {
            "success": True,
            "wiki_root": str(self._store.root),
            "backend": self._backend.name,
            "indexed": counts,
            "memory_dir": self._store.memory_dir,
            "memory_pages": memory_pages,
            "untrusted_dirs_excluded": list(self._store.untrusted_dirs),
            "writes_this_session": self._writes_this_session,
        }
        if args.get("lint"):
            dangling = self._store.dangling_links()
            _out, backlinks = self._store.link_graph()
            orphans = sorted(
                page.slug for page in self._store.iter_pages()
                if not backlinks.get(page.slug) and page.slug != "index"
            )
            payload["lint"] = {
                "dangling_links": dangling,
                "orphan_pages": orphans[:50],
                "orphan_count": len(orphans),
            }
        return json.dumps(payload, ensure_ascii=False)

    # -- setup -------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Minimal wizard: only what the user must decide."""
        return [
            {
                "key": "wiki_path",
                "description": "Absolute path to your markdown wiki root",
                "required": True,
                "default": str(Path.home() / "wiki"),
            },
            {
                "key": "backend",
                "description": "Search backend (fts5 = stdlib, fast; qmd = hybrid semantic, needs qmd CLI)",
                "default": "fts5",
                "choices": ["fts5", "qmd"],
            },
            {
                "key": "memory_dir",
                "description": "Wiki-relative folder the agent may write to",
                "default": "wiki/memory",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        path = save_config_file(values, hermes_home)
        print(f"\n  hwiki config written to {path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Called by the Hermes memory plugin discovery system."""
    ctx.register_memory_provider(WikiMemoryProvider(config=load_config()))
