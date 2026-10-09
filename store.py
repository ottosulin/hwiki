"""Filesystem model for a markdown wiki used as agent memory.

Pure stdlib. Responsible for:

* enumerating wiki pages (with include/exclude globs and a hard-excluded
  ``untrusted`` zone),
* parsing YAML-ish frontmatter without requiring PyYAML,
* extracting Obsidian ``[[wikilinks]]`` and building a link graph,
* chunking pages into heading-scoped sections for retrieval,
* appending durable notes into the *memory zone* under a write lock,
* refusing to persist anything that looks like a secret.

Design rule: this module NEVER writes outside ``<root>/<memory_dir>``.
The curated wiki is read-only to the memory provider — automatic writes
must not corrupt hand-maintained synthesis pages.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:  # POSIX advisory locking; absent on Windows.
    import fcntl
except Exception:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def parse_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """Split ``text`` into (frontmatter dict, body).

    Uses PyYAML when importable, otherwise a deliberately small parser that
    handles the subset this wiki uses: ``key: scalar``, ``key: [a, b]`` and
    ``key:`` followed by ``- item`` lines. Unknown constructs degrade to the
    raw string rather than raising — a malformed page must never break recall.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end():]

    try:  # Prefer a real YAML parser when the host has one.
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}, body
    except Exception:
        pass

    data: Dict[str, object] = {}
    current_key: Optional[str] = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current_key:
            existing = data.get(current_key)
            item = stripped[2:].strip().strip("'\"")
            if isinstance(existing, list):
                existing.append(item)
            else:
                data[current_key] = [item]
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        current_key = key
        if not value:
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [p.strip().strip("'\"") for p in inner.split(",") if p.strip()]
        else:
            data[key] = value.strip("'\"")
    return data, body


def _as_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def slugify(value: str, *, fallback: str = "note") -> str:
    """Filesystem-safe, lowercase, hyphenated slug."""
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[\s_]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:80] or fallback


# ---------------------------------------------------------------------------
# Secret guard
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: Sequence[Tuple[str, re.Pattern]] = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Scaleway secret key", re.compile(r"\bSCW[A-Z0-9]{8}[A-Z0-9-]{3,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("inline credential", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b"
        r"\s*[:=]\s*[\"']?[^\s\"',;]{8,}"
    )),
)


def scan_for_secrets(text: str) -> List[str]:
    """Return human-readable labels for any secret-shaped content found."""
    return [label for label, pattern in _SECRET_PATTERNS if pattern.search(text or "")]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    """A heading-scoped slice of a page — the unit of retrieval."""

    path: str
    heading: str
    text: str
    order: int


@dataclass
class Page:
    path: str                      # wiki-root-relative POSIX path
    abspath: Path
    title: str
    body: str
    frontmatter: Dict[str, object] = field(default_factory=dict)
    mtime: float = 0.0
    size: int = 0

    @property
    def slug(self) -> str:
        return Path(self.path).stem

    @property
    def spine(self) -> str:
        return _as_str(self.frontmatter.get("spine"))

    @property
    def doctype(self) -> str:
        return _as_str(self.frontmatter.get("type"))

    @property
    def status(self) -> str:
        return _as_str(self.frontmatter.get("status"))

    @property
    def updated(self) -> str:
        return _as_str(self.frontmatter.get("updated"))

    def links(self) -> List[str]:
        """Outbound ``[[wikilink]]`` targets, de-duplicated, order preserved."""
        seen: Dict[str, None] = {}
        for match in _WIKILINK_RE.finditer(self.body):
            target = match.group(1).strip()
            if target:
                seen.setdefault(target, None)
        return list(seen)

    def chunks(self, max_chars: int = 1200) -> List[Chunk]:
        """Split the body on H2/H3 boundaries, hard-wrapping long sections."""
        sections: List[Tuple[str, List[str]]] = [(self.title, [])]
        for line in self.body.splitlines():
            heading = _HEADING_RE.match(line)
            if heading and len(heading.group(1)) <= 3:
                sections.append((heading.group(2).strip(), []))
            else:
                sections[-1][1].append(line)

        out: List[Chunk] = []
        for heading, lines in sections:
            text = "\n".join(lines).strip()
            if not text:
                continue
            if len(text) <= max_chars:
                out.append(Chunk(self.path, heading, text, len(out)))
                continue
            buf: List[str] = []
            length = 0
            for para in text.split("\n\n"):
                if length + len(para) > max_chars and buf:
                    out.append(Chunk(self.path, heading, "\n\n".join(buf), len(out)))
                    buf, length = [], 0
                buf.append(para)
                length += len(para) + 2
            if buf:
                out.append(Chunk(self.path, heading, "\n\n".join(buf), len(out)))
        return out


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDES: Tuple[str, ...] = (
    ".git/*", ".obsidian/*", "node_modules/*", ".trash/*",
    "*/.git/*", "*/node_modules/*",
)


class WikiStore:
    """Read/write access to a markdown wiki rooted at ``root``.

    Parameters
    ----------
    root:
        Wiki root directory.
    include:
        Root-relative glob(s) selecting indexable pages. Defaults to ``**/*.md``.
    exclude:
        Root-relative glob(s) removed from the index.
    untrusted_dirs:
        Directories holding third-party source material. These are NEVER
        indexed and NEVER surfaced through prefetch — they are the natural
        home of prompt-injection payloads (customer PDFs, vendor advisories).
    memory_dir:
        Root-relative directory that the provider may write to.
    """

    def __init__(
        self,
        root: os.PathLike | str,
        *,
        include: Sequence[str] = ("**/*.md",),
        exclude: Sequence[str] = (),
        untrusted_dirs: Sequence[str] = ("raw",),
        memory_dir: str = "wiki/memory",
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.include = tuple(include) or ("**/*.md",)
        self.exclude = tuple(exclude) + DEFAULT_EXCLUDES
        self.untrusted_dirs = tuple(d.strip("/") for d in untrusted_dirs if d.strip("/"))
        self.memory_dir = memory_dir.strip("/")

    # -- discovery ---------------------------------------------------------

    def exists(self) -> bool:
        return self.root.is_dir()

    def _is_untrusted(self, rel: str) -> bool:
        return any(rel == d or rel.startswith(d + "/") for d in self.untrusted_dirs)

    def _excluded(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pattern) for pattern in self.exclude)

    def iter_paths(self) -> Iterator[Path]:
        """Yield absolute paths of every indexable page."""
        if not self.exists():
            return
        seen: set[Path] = set()
        for pattern in self.include:
            for abspath in self.root.glob(pattern):
                if not abspath.is_file() or abspath in seen:
                    continue
                try:
                    rel = abspath.resolve().relative_to(self.root).as_posix()
                except ValueError:
                    continue  # symlink escaping the root
                if self._is_untrusted(rel) or self._excluded(rel):
                    continue
                seen.add(abspath)
                yield abspath

    def relpath(self, abspath: os.PathLike | str) -> str:
        return Path(abspath).resolve().relative_to(self.root).as_posix()

    def abspath(self, rel: str) -> Path:
        """Resolve a root-relative path, refusing traversal outside the root."""
        candidate = (self.root / rel.lstrip("/")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"path escapes wiki root: {rel}")
        return candidate

    # -- reading -----------------------------------------------------------

    def load(self, abspath: os.PathLike | str) -> Optional[Page]:
        path = Path(abspath)
        try:
            stat = path.stat()
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        frontmatter, body = parse_frontmatter(text)
        title = _as_str(frontmatter.get("title"))
        if not title:
            for line in body.splitlines():
                heading = _HEADING_RE.match(line)
                if heading:
                    title = heading.group(2).strip()
                    break
        try:
            rel = self.relpath(path)
        except ValueError:
            return None
        return Page(
            path=rel,
            abspath=path,
            title=title or Path(rel).stem,
            body=body,
            frontmatter=frontmatter,
            mtime=stat.st_mtime,
            size=stat.st_size,
        )

    def load_rel(self, rel: str) -> Optional[Page]:
        """Load a root-relative page, refusing untrusted/excluded locations."""
        try:
            abspath = self.abspath(rel)
        except ValueError:
            return None
        try:
            normalized = self.relpath(abspath)
        except ValueError:
            return None
        if self._is_untrusted(normalized) or self._excluded(normalized):
            return None
        return self.load(abspath)

    def links_for(self, rel: str, *, cap: int = 8) -> List[str]:
        """Outbound ``[[wikilink]]`` targets of a page, capped.

        ``load_rel`` already refuses untrusted/excluded locations, so the
        raw/ boundary holds even if a backend ever returns a stray path.
        """
        page = self.load_rel(rel)
        if page is None:
            return []
        return page.links()[:cap]

    def iter_pages(self) -> Iterator[Page]:
        for abspath in self.iter_paths():
            page = self.load(abspath)
            if page is not None:
                yield page

    def resolve_page(self, reference: str) -> Optional[Page]:
        """Resolve a page by relative path, slug, wikilink target, or title."""
        reference = (reference or "").strip().strip("[]")
        if not reference:
            return None
        if reference.endswith(".md"):
            page = self.load_rel(reference)
            if page:
                return page
            # Wiki links carry no folder ([[release-process]]), so a guessed folder
            # (concepts/ vs guides/) misses. Fall back to the filename slug, but
            # only when exactly one page has it: never silently pick between twins.
            stem = Path(reference).stem
            matches = [p for p in self.iter_pages() if p.slug == stem]
            return matches[0] if len(matches) == 1 else None
        target = slugify(reference)
        fallback: Optional[Page] = None
        for page in self.iter_pages():
            if page.path == reference:
                return page
            if page.slug == reference or slugify(page.slug) == target:
                return page
            if fallback is None and slugify(page.title) == target:
                fallback = page
        return fallback

    def link_graph(self) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """Return (outlinks, backlinks) keyed by page slug."""
        outlinks: Dict[str, List[str]] = {}
        backlinks: Dict[str, List[str]] = {}
        slugs: Dict[str, str] = {}
        pages = list(self.iter_pages())
        for page in pages:
            slugs[slugify(page.slug)] = page.slug
            slugs.setdefault(slugify(page.title), page.slug)
        for page in pages:
            resolved: List[str] = []
            for raw in page.links():
                target = slugs.get(slugify(raw), raw)
                resolved.append(target)
                backlinks.setdefault(target, []).append(page.slug)
            outlinks[page.slug] = resolved
        return outlinks, backlinks

    def dangling_links(self) -> Dict[str, List[str]]:
        """Wikilinks pointing at pages that do not exist, keyed by source slug."""
        known = set()
        for page in self.iter_pages():
            known.add(slugify(page.slug))
            known.add(slugify(page.title))
        out: Dict[str, List[str]] = {}
        for page in self.iter_pages():
            missing = [t for t in page.links() if slugify(t) not in known]
            if missing:
                out[page.slug] = missing
        return out

    # -- writing (memory zone only) ---------------------------------------

    def memory_root(self) -> Path:
        return self.root / self.memory_dir

    def memory_page_path(self, slug: str) -> Path:
        return self.memory_root() / f"{slugify(slug)}.md"

    def append_note(
        self,
        content: str,
        *,
        topic: str = "",
        page: str = "",
        tags: Sequence[str] = (),
        source: str = "",
        today: Optional[str] = None,
        allow_secrets: bool = False,
    ) -> Dict[str, object]:
        """Append a dated bullet to a memory-zone page, creating it if needed.

        Returns a result dict with ``path``, ``created`` and ``duplicate``.
        Raises ``ValueError`` when the content looks like a secret.
        """
        content = " ".join((content or "").split())
        if not content:
            raise ValueError("content is empty")

        if not allow_secrets:
            found = scan_for_secrets(content)
            if found:
                raise ValueError(
                    "refusing to write apparent secret material into the wiki "
                    f"({', '.join(found)}); store a pointer to the secret's "
                    "location instead"
                )

        slug = slugify(page or topic or "journal", fallback="journal")
        target = self.memory_page_path(slug)
        target.parent.mkdir(parents=True, exist_ok=True)

        stamp = today or date.today().isoformat()
        suffix = ""
        if tags:
            suffix += " " + " ".join(f"#{slugify(t)}" for t in tags if str(t).strip())
        if source:
            suffix += f" _(source: {source})_"
        entry = f"- **{stamp}** — {content}{suffix}"

        with _FileLock(target):
            created = not target.exists()
            if created:
                header = (
                    "---\n"
                    f"title: {topic or slug.replace('-', ' ').title()}\n"
                    "spine: memory\n"
                    "type: memory\n"
                    "status: auto\n"
                    "source: agent-memory\n"
                    f"updated: {stamp}\n"
                    "---\n\n"
                    f"# {topic or slug.replace('-', ' ').title()}\n\n"
                    "> Auto-maintained by the `hwiki` memory provider. Promote durable\n"
                    "> conclusions into the curated wiki rather than editing in place.\n\n"
                    "## Log\n\n"
                )
                target.write_text(header + entry + "\n", encoding="utf-8")
                return {"path": self.relpath(target), "created": True, "duplicate": False}

            existing = target.read_text(encoding="utf-8", errors="replace")
            if _normalize(content) in _normalize(existing):
                return {"path": self.relpath(target), "created": False, "duplicate": True}

            if "\n## Log" not in existing:
                existing = existing.rstrip("\n") + "\n\n## Log\n"
            updated = existing.rstrip("\n") + "\n" + entry + "\n"
            updated = _bump_frontmatter_date(updated, stamp)
            target.write_text(updated, encoding="utf-8")

        return {"path": self.relpath(target), "created": False, "duplicate": False}

    def content_hash(self) -> str:
        """Cheap fingerprint of the corpus (paths + mtimes + sizes)."""
        digest = hashlib.sha1()
        for abspath in sorted(self.iter_paths()):
            try:
                stat = abspath.stat()
            except OSError:
                continue
            digest.update(str(abspath).encode())
            digest.update(f"{stat.st_mtime_ns}:{stat.st_size}".encode())
        return digest.hexdigest()


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _bump_frontmatter_date(text: str, stamp: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return text
    head = match.group(0)
    if re.search(r"^updated:.*$", head, re.MULTILINE):
        new_head = re.sub(r"^updated:.*$", f"updated: {stamp}", head, count=1, flags=re.MULTILINE)
    else:
        new_head = head.rstrip("\n")
        new_head = new_head[: new_head.rfind("---")] + f"updated: {stamp}\n---\n"
    return new_head + text[match.end():]


class _FileLock:
    """Best-effort advisory lock so concurrent turns can't interleave writes."""

    def __init__(self, target: Path, timeout: float = 5.0) -> None:
        self.lockfile = target.with_suffix(target.suffix + ".lock")
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "_FileLock":
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.lockfile, "a+")
        if fcntl is None:
            return self
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() >= deadline:
                    return self  # degrade to unlocked rather than lose the write
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._handle.close()
        except Exception:
            pass
        try:
            self.lockfile.unlink()
        except OSError:
            pass
