"""Search backends for the wiki memory provider.

Two interchangeable backends behind one interface:

``Fts5Backend``
    SQLite FTS5 over heading-scoped chunks. Pure stdlib, no models, no
    network, single-digit-millisecond queries. Auto-reindexes incrementally
    when page mtimes change. This is the default and the always-available
    fallback.

``QmdBackend``
    Shells out to the ``qmd`` CLI (github.com/tobi/qmd) for hybrid
    BM25 + local-embedding retrieval with reranking. Better on conceptual
    ("what did we decide about X") queries, at the cost of a node runtime,
    ~1.6 GB of GGUF models, and much higher latency.

``HybridBackend`` runs FTS5 first and merges qmd results when qmd answers
within its latency budget, so recall degrades to FTS5 instead of failing.

Everything returns ``SearchHit`` objects so the provider is backend-agnostic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .store import WikiStore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3


@dataclass
class SearchHit:
    path: str
    title: str
    heading: str
    snippet: str
    score: float = 0.0
    backend: str = ""
    meta: Dict[str, str] = field(default_factory=dict)
    links: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "path": self.path,
            "title": self.title,
            "snippet": self.snippet,
            "score": round(self.score, 4),
            "backend": self.backend,
        }
        if self.heading:
            out["heading"] = self.heading
        if self.meta:
            out.update(self.meta)
        out["links"] = list(self.links)
        return out


# ---------------------------------------------------------------------------
# FTS5
# ---------------------------------------------------------------------------

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "do", "does", "did", "what", "which", "who", "how",
    "why", "when", "where", "our", "we", "you", "i", "it", "this", "that",
    "with", "about", "from", "have", "has", "can", "should", "would", "please",
}


def build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Every token is quoted (so ``:`` / ``-`` / ``.`` can't be read as FTS5
    operators — the classic injection-shaped crash) and given a prefix
    wildcard. Tokens are OR-ed so partial matches still return something.
    """
    tokens = [t.lower() for t in _FTS_TOKEN_RE.findall(query or "")]
    meaningful = [t for t in tokens if t not in _STOPWORDS and len(t) > 1]
    chosen = (meaningful or tokens)[:12]
    if not chosen:
        return ""
    return " OR ".join(f'"{t}"*' for t in chosen)


class Fts5Backend:
    """Incremental SQLite FTS5 index over wiki chunks."""

    name = "fts5"

    def __init__(self, store: WikiStore, db_path: os.PathLike | str) -> None:
        self.store = store
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._last_sync = 0.0

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        self._migrate(conn)
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.executescript(
                "DROP TABLE IF EXISTS chunks;"
                "DROP TABLE IF EXISTS pages;"
            )
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
                path   TEXT PRIMARY KEY,
                title  TEXT,
                spine  TEXT,
                type   TEXT,
                status TEXT,
                updated TEXT,
                mtime  REAL,
                size   INTEGER
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                path UNINDEXED,
                title,
                heading,
                body,
                spine UNINDEXED,
                tokenize='porter unicode61'
            );
            """
        )
        conn.commit()

    def is_available(self) -> bool:
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            conn.close()
            return True
        except Exception:
            return False

    # -- indexing ----------------------------------------------------------

    def sync(self, *, force: bool = False) -> Dict[str, int]:
        """Reindex changed pages. Returns counts of added/updated/removed."""
        with self._lock:
            conn = self._connect()
            known = {
                row["path"]: (row["mtime"], row["size"])
                for row in conn.execute("SELECT path, mtime, size FROM pages")
            }
            seen: set[str] = set()
            added = updated = 0

            for page in self.store.iter_pages():
                seen.add(page.path)
                prior = known.get(page.path)
                if not force and prior and abs(prior[0] - page.mtime) < 1e-6 and prior[1] == page.size:
                    continue
                conn.execute("DELETE FROM chunks WHERE path = ?", (page.path,))
                rows = [
                    (page.path, page.title, chunk.heading, chunk.text, page.spine)
                    for chunk in page.chunks()
                ]
                if rows:
                    conn.executemany(
                        "INSERT INTO chunks (path, title, heading, body, spine) "
                        "VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                conn.execute(
                    "INSERT INTO pages (path, title, spine, type, status, updated, mtime, size) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET title=excluded.title, spine=excluded.spine, "
                    "type=excluded.type, status=excluded.status, updated=excluded.updated, "
                    "mtime=excluded.mtime, size=excluded.size",
                    (page.path, page.title, page.spine, page.doctype, page.status,
                     page.updated, page.mtime, page.size),
                )
                if prior:
                    updated += 1
                else:
                    added += 1

            removed = 0
            for path in set(known) - seen:
                conn.execute("DELETE FROM pages WHERE path = ?", (path,))
                conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
                removed += 1

            conn.commit()
            self._last_sync = time.time()
            return {"added": added, "updated": updated, "removed": removed}

    def count(self) -> Dict[str, int]:
        conn = self._connect()
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"pages": pages, "chunks": chunks}

    # -- query -------------------------------------------------------------

    def search(self, query: str, *, limit: int = 6, spine: str = "") -> List[SearchHit]:
        match = build_match_query(query)
        if not match:
            return []
        conn = self._connect()
        sql = (
            "SELECT path, title, heading, "
            "  snippet(chunks, 3, '', '', ' … ', 24) AS snip, "
            "  bm25(chunks, 0.0, 4.0, 2.0, 1.0) AS rank, spine "
            "FROM chunks WHERE chunks MATCH ? "
        )
        params: List[object] = [match]
        if spine:
            sql += "AND spine = ? "
            params.append(spine)
        sql += "ORDER BY rank LIMIT ?"
        params.append(int(limit))
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            logger.debug("FTS5 query failed (%s) for %r", exc, match)
            return []

        hits: List[SearchHit] = []
        for row in rows:
            # bm25() is negative-better; map to a positive relevance score.
            hits.append(SearchHit(
                path=row["path"],
                title=row["title"] or "",
                heading=row["heading"] or "",
                snippet=" ".join((row["snip"] or "").split()),
                score=max(0.0, -float(row["rank"])),
                backend=self.name,
            ))
        return hits

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


# ---------------------------------------------------------------------------
# qmd
# ---------------------------------------------------------------------------

class QmdBackend:
    """Optional hybrid retrieval via the ``qmd`` CLI.

    ``mode`` selects the qmd subcommand:
      ``search``  BM25 only, no models  (~0.3s)
      ``vsearch`` vector only           (~3s warm, needs embed models)
      ``query``   hybrid + rerank       (best quality, slowest, ~2-3s warm)
    """

    name = "qmd"

    def __init__(
        self,
        store: WikiStore,
        *,
        collection: str = "hwiki",
        binary: str = "qmd",
        mode: str = "search",
        timeout: float = 6.0,
    ) -> None:
        self.store = store
        self.collection = collection
        self.binary = binary
        self.mode = mode if mode in {"search", "vsearch", "query"} else "search"
        self.timeout = float(timeout)
        self._registered = False
        self._lock = threading.RLock()

    def resolve_binary(self) -> Optional[str]:
        return shutil.which(self.binary) or (self.binary if Path(self.binary).is_file() else None)

    def is_available(self) -> bool:
        return self.resolve_binary() is not None

    def _run(self, args: Sequence[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        binary = self.resolve_binary()
        if binary is None:
            raise FileNotFoundError(f"{self.binary} not on PATH")
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        return subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout if timeout is not None else self.timeout,
            env=env,
            check=False,
        )

    def ensure_collection(self) -> bool:
        """Register the wiki as a qmd collection once per process."""
        with self._lock:
            if self._registered:
                return True
            try:
                listing = self._run(["collection", "list"], timeout=20)
                if self.collection not in (listing.stdout or ""):
                    self._run(
                        ["collection", "add", str(self.store.root), "--name", self.collection],
                        timeout=180,
                    )
                self._registered = True
                return True
            except Exception as exc:
                logger.debug("qmd collection setup failed: %s", exc)
                return False

    def sync(self, *, force: bool = False) -> Dict[str, int]:
        """Re-index (and optionally re-embed) the qmd collection."""
        if not self.ensure_collection():
            return {}
        try:
            self._run(["update"], timeout=300)
            if self.mode in {"vsearch", "query"}:
                self._run(["embed", "-c", self.collection], timeout=900)
        except Exception as exc:
            logger.debug("qmd sync failed: %s", exc)
        return {}

    def search(self, query: str, *, limit: int = 6, spine: str = "") -> List[SearchHit]:
        query = (query or "").strip()
        if not query or not self.ensure_collection():
            return []
        # Over-fetch: untrusted hits are dropped below, and qmd has no
        # server-side exclusion, so ask for headroom to still fill `limit`.
        args = [self.mode, query, "--json", "--limit", str(max(int(limit) * 3, int(limit) + 5)),
                "--collection", self.collection]
        try:
            proc = self._run(args)
        except subprocess.TimeoutExpired:
            logger.debug("qmd %s timed out after %.1fs", self.mode, self.timeout)
            return []
        except Exception as exc:
            logger.debug("qmd %s failed: %s", self.mode, exc)
            return []
        if proc.returncode != 0:
            logger.debug("qmd exited %s: %s", proc.returncode, (proc.stderr or "")[:200])
            return []
        return self._parse(proc.stdout or "", limit)

    def _is_permitted(self, rel: str) -> bool:
        """Trust boundary for qmd results.

        ``qmd`` indexes whatever directory it was pointed at and offers no
        per-query exclusion, so an untrusted page (``raw/``) WILL come back in
        its result set. Auto-injected recall is the highest-value
        prompt-injection target, so every qmd hit is re-checked against the
        store's own include/exclude/untrusted rules before it is surfaced.
        Verified necessary: without this, ``raw/vendor-notes.md``
        leaks into recall.
        """
        if not rel or rel.startswith(("/", "..")):
            return False
        # pylint: disable=protected-access
        if self.store._is_untrusted(rel) or self.store._excluded(rel):
            return False
        try:
            abspath = self.store.abspath(rel)
        except ValueError:
            return False
        return abspath.is_file()

    def _parse(self, stdout: str, limit: int) -> List[SearchHit]:
        payload = _extract_json(stdout)
        if payload is None:
            return []
        if isinstance(payload, dict):
            payload = payload.get("results") or payload.get("hits") or []
        if not isinstance(payload, list):
            return []

        hits: List[SearchHit] = []
        dropped = 0
        for item in payload:
            if len(hits) >= limit:
                break
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("file") or item.get("path") or "")
            rel = self._to_relpath(raw_path)
            if not self._is_permitted(rel):
                dropped += 1
                continue
            hits.append(SearchHit(
                path=rel,
                title=str(item.get("title") or ""),
                heading="",
                snippet=" ".join(str(item.get("snippet") or item.get("text") or "").split())[:600],
                score=float(item.get("score") or 0.0),
                backend=self.name,
                meta={"docid": str(item["docid"])} if item.get("docid") else {},
            ))
        if dropped:
            logger.debug("qmd: filtered %d untrusted/unknown result(s)", dropped)
        return hits

    def _to_relpath(self, raw: str) -> str:
        """Normalise ``qmd://collection/rel/path.md`` and absolute paths."""
        if raw.startswith("qmd://"):
            trimmed = raw[len("qmd://"):]
            _, _, rest = trimmed.partition("/")
            return rest or trimmed
        try:
            return self.store.relpath(raw)
        except Exception:
            return raw


def _extract_json(text: str):
    """Parse JSON from CLI stdout that may carry progress/banner lines."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Hybrid
# ---------------------------------------------------------------------------

class HybridBackend:
    """FTS5 always; qmd merged in when it responds in time."""

    name = "hybrid"

    def __init__(self, fts: Fts5Backend, qmd: QmdBackend) -> None:
        self.fts = fts
        self.qmd = qmd

    def is_available(self) -> bool:
        return self.fts.is_available()

    def sync(self, *, force: bool = False) -> Dict[str, int]:
        stats = self.fts.sync(force=force)
        if self.qmd.is_available():
            try:
                self.qmd.sync(force=force)
            except Exception as exc:
                logger.debug("hybrid: qmd sync skipped: %s", exc)
        return stats

    def count(self) -> Dict[str, int]:
        return self.fts.count()

    def search(self, query: str, *, limit: int = 6, spine: str = "") -> List[SearchHit]:
        primary = self.fts.search(query, limit=limit, spine=spine)
        if not self.qmd.is_available():
            return primary
        try:
            secondary = self.qmd.search(query, limit=limit, spine=spine)
        except Exception:
            secondary = []
        if not secondary:
            return primary
        return reciprocal_rank_fusion([primary, secondary], limit=limit)

    def close(self) -> None:
        self.fts.close()


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], *, limit: int = 6, k: int = 60
) -> List[SearchHit]:
    """Merge ranked lists with RRF, de-duplicating on (path, heading)."""
    scored: Dict[tuple, float] = {}
    best: Dict[tuple, SearchHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            key = (hit.path, hit.heading)
            scored[key] = scored.get(key, 0.0) + 1.0 / (k + rank + 1)
            existing = best.get(key)
            if existing is None or len(hit.snippet) > len(existing.snippet):
                best[key] = hit
    ordered = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    out: List[SearchHit] = []
    for key, score in ordered:
        hit = best[key]
        out.append(SearchHit(
            path=hit.path, title=hit.title, heading=hit.heading,
            snippet=hit.snippet, score=score, backend="hybrid", meta=hit.meta,
        ))
    return out


# ---------------------------------------------------------------------------
# Ranking partition
# ---------------------------------------------------------------------------

class RankingPartitionBackend:
    """Deterministic post-ranking partition (Meta "second brain" pattern).

    Two rules, applied to the wrapped backend's candidate list in score order:

    1. Per-(page, heading) cap — duplicate physical chunks under one heading
       are collapsed to the best-scoring row (they are the same logical chunk
       split by the 1200-char chunker; surfacing two carries zero extra
       information), and at most ``max_hits_per_page`` distinct headings per
       page. Stops one long page from monopolising the ``limit`` slots on
       multi-topic queries.
    2. Demotion — paths in ``demote_paths`` (append-only logs, mirrors of
       content injected elsewhere) are secondary: they only fill slots after
       distinct curated pages are exhausted, and the per-page cap applies to
       them as well (distinct headings still pass). They are NOT excluded:
       an answer that lives only in a log must stay reachable, it just must
       not displace curated pages while any remain.

    Fails open: with no demote paths and no cap pressure the output equals
    the inner backend's top-``limit``. Pure post-processing on already
    fetched candidates — no extra queries, microseconds of work.
    """

    def __init__(
        self,
        inner,
        *,
        max_hits_per_page: int = 2,
        demote_paths: Optional[Sequence[str]] = None,
        fetch_multiplier: int = 4,
    ) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "partition")
        self.max_hits_per_page = max(1, int(max_hits_per_page))
        self.fetch_multiplier = max(1, int(fetch_multiplier))
        self.demote_paths = {
            p.strip("/") for p in (demote_paths or []) if p and p.strip()
        }

    def _is_demoted(self, rel: str) -> bool:
        rel = (rel or "").strip("/")
        if rel in self.demote_paths:
            return True
        # Suffix match so a relocated log (e.g. archive/log.md) keeps its
        # secondary status instead of silently un-demoting.
        return any(rel.endswith("/" + p) for p in self.demote_paths)

    def search(self, query: str, *, limit: int = 6, spine: str = "") -> List[SearchHit]:
        pool = self._inner.search(
            query, limit=int(limit) * self.fetch_multiplier, spine=spine,
        )
        if not pool:
            return pool

        # Collapse duplicate physical rows sharing one (path, heading):
        # keep the best-scoring row per logical chunk.
        best_by_cell: Dict[tuple, SearchHit] = {}
        for hit in pool:
            cell = (hit.path, hit.heading)
            incumbent = best_by_cell.get(cell)
            if incumbent is None or hit.score > incumbent.score:
                best_by_cell[cell] = hit
        deduped = sorted(
            best_by_cell.values(), key=lambda h: h.score, reverse=True,
        )

        primary: List[SearchHit] = []
        secondary: List[SearchHit] = []
        per_page: Dict[str, int] = {}
        for hit in deduped:
            bucket = secondary if self._is_demoted(hit.path) else primary
            if bucket is primary:
                if per_page.get(hit.path, 0) >= self.max_hits_per_page:
                    continue  # page budget spent; still eligible via demoted pages
                per_page[hit.path] = per_page.get(hit.path, 0) + 1
            bucket.append(hit)
            if len(primary) + len(secondary) >= int(limit) * self.fetch_multiplier:
                break

        chosen: List[SearchHit] = []
        page_count: Dict[str, int] = {}
        # Pass 1: curated candidates first (cap applied), demoted last.
        for hit in primary:
            if page_count.get(hit.path, 0) >= self.max_hits_per_page:
                continue
            page_count[hit.path] = page_count.get(hit.path, 0) + 1
            chosen.append(hit)
        # Pass 2: demoted backfill. The per-page cap applies here too so a
        # log-heavy query cannot fill every slot from one secondary page
        # while another secondary heading matched (log-only answers stay
        # reachable: distinct log headings still pass the cap).
        for hit in secondary:
            if len(chosen) >= int(limit):
                break
            if page_count.get(hit.path, 0) >= self.max_hits_per_page:
                continue
            page_count[hit.path] = page_count.get(hit.path, 0) + 1
            chosen.append(hit)

        if len(chosen) < int(limit) and len(primary) < int(limit):
            # Under-fill: relax the per-page cap for curated pages so the
            # slot budget is not wasted when few pages match. Iterate the
            # DEDUPED list, never the raw pool, or collapsed duplicate rows
            # leak back in and defeat the dedupe rule.
            chosen_cells = {(h.path, h.heading) for h in chosen}
            for hit in deduped:
                if len(chosen) >= int(limit):
                    break
                if self._is_demoted(hit.path):
                    continue
                cell = (hit.path, hit.heading)
                if cell in chosen_cells:
                    continue
                chosen_cells.add(cell)
                chosen.append(hit)
        return chosen[: int(limit)]

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)


def build_backend(
    store: WikiStore,
    *,
    backend: str,
    db_path: os.PathLike | str,
    qmd_binary: str = "qmd",
    qmd_collection: str = "hwiki",
    qmd_mode: str = "search",
    qmd_timeout: float = 6.0,
    max_hits_per_page: int = 2,
    demote_paths: Optional[Sequence[str]] = None,
    fetch_multiplier: int = 4,
):
    """Construct the configured backend, degrading to FTS5 when qmd is absent.

    Layering, outermost first::

        RankingPartitionBackend   deterministic per-page cap + demotion
        _LinkEnrichingBackend     attaches outbound [[wikilinks]] to every hit
        Fts5Backend / HybridBackend

    Every layer is wrapped so the provider, CLI and bench treat the result
    identically (same ``SearchHit`` interface).
    """
    fts = Fts5Backend(store, db_path)
    wanted = (backend or "fts5").lower()
    if wanted == "fts5":
        core = fts
    else:
        qmd = QmdBackend(
            store, collection=qmd_collection, binary=qmd_binary,
            mode=qmd_mode, timeout=qmd_timeout,
        )
        if not qmd.is_available():
            logger.warning(
                "wiki memory: backend '%s' requested but '%s' is not on PATH; "
                "falling back to FTS5", wanted, qmd_binary,
            )
            core = fts
        else:
            core = HybridBackend(fts, qmd)  # qmd alone would lose the safety net
    partition = RankingPartitionBackend(
        core,
        max_hits_per_page=max_hits_per_page,
        demote_paths=demote_paths,
        fetch_multiplier=fetch_multiplier,
    )
    return _LinkEnrichingBackend(store, partition)


_LINK_CAP = 8


def attach_links(store: WikiStore, hits: List[SearchHit]) -> None:
    """Fill ``hit.links`` per page, cached by path for one search call."""
    cache: Dict[str, List[str]] = {}
    for hit in hits:
        if hit.path not in cache:
            cache[hit.path] = store.links_for(hit.path, cap=_LINK_CAP)
        hit.links = cache[hit.path]


class _LinkEnrichingBackend:
    """Thin decorator: every ``search()`` result carries the page's outbound links.

    Delegates all other attributes (sync/count/close/is_available) to the
    wrapped backend, so the provider, CLI and bench treat it identically.
    Ranking, query building and prefetch output are untouched: ``links`` is
    added after retrieval and callers that ignore it are byte-identical.
    """

    def __init__(self, store: WikiStore, inner) -> None:
        self._store = store
        self._inner = inner
        self.name = inner.name

    def search(self, query: str, *, limit: int = 6, spine: str = "") -> List[SearchHit]:
        hits = self._inner.search(query, limit=limit, spine=spine)
        attach_links(self._store, hits)
        return hits

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)
