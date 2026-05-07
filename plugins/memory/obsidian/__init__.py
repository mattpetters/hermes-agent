"""Read-only Obsidian vault memory provider.

File-backed persistent recall for a local Obsidian vault. This provider treats
Obsidian as a human-readable long-term memory layer while keeping writes out of
scope: it can search/read notes and inject compact relevant snippets, but it
never creates, edits, moves, or deletes vault files.

Configuration:
  OBSIDIAN_VAULT_PATH              vault path (default: ~/vaults/vault-one if it exists)
  OBSIDIAN_MEMORY_MAX_CHARS        prefetch context cap (default: 1800)
  OBSIDIAN_MEMORY_SEARCH_LIMIT     default search result count (default: 5)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

_DEFAULT_MAX_CHARS = 1800
_DEFAULT_SEARCH_LIMIT = 5
_MAX_READ_CHARS = 20000
_HUGE_FILE_BYTES = 512_000

_EXCLUDED_DIRS = {
    ".git",
    ".obsidian",
    ".claude",
    ".trash",
    "node_modules",
    "_attachments",
}

_PRIORITY_PREFIXES = (
    "mocs/",
    "3-resources/wiki/",
    "3-resources/raw/",
    "daily/",
    "1-projects/",
    "2-areas/",
    "3-resources/",
)

SEARCH_SCHEMA = {
    "name": "obsidian_search",
    "description": (
        "Search the configured Obsidian vault read-only. Prioritizes MOCs and "
        "3-resources/wiki, excludes .claude worktrees/caches, and returns compact snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "limit": {"type": "integer", "description": "Maximum results (default 5, max 20)."},
            "scope": {
                "type": "string",
                "description": "Optional vault-relative folder scope, e.g. '3-resources/wiki' or 'mocs'.",
            },
        },
        "required": ["query"],
    },
}

READ_SCHEMA = {
    "name": "obsidian_read",
    "description": (
        "Read a vault-relative markdown note from Obsidian read-only. Large files "
        "are truncated unless max_chars is increased. Never use for paths under .claude."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Vault-relative note path."},
            "max_chars": {"type": "integer", "description": "Maximum characters to return (default 12000, max 20000)."},
        },
        "required": ["path"],
    },
}

RECENT_SCHEMA = {
    "name": "obsidian_recent",
    "description": "List recently modified markdown notes in the vault, excluding .claude and other caches.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum results (default 10, max 50)."},
            "scope": {"type": "string", "description": "Optional vault-relative folder scope."},
        },
        "required": [],
    },
}

ALL_TOOL_SCHEMAS = [SEARCH_SCHEMA, READ_SCHEMA, RECENT_SCHEMA]


@dataclass
class SearchResult:
    path: str
    title: str
    score: int
    snippet: str
    size: int
    modified: float


class ObsidianMemoryProvider(MemoryProvider):
    """Read-only local Obsidian provider for compact vault recall."""

    def __init__(self) -> None:
        self._vault: Optional[Path] = None
        self._max_chars = _DEFAULT_MAX_CHARS
        self._search_limit = _DEFAULT_SEARCH_LIMIT
        self._session_id = ""

    @property
    def name(self) -> str:
        return "obsidian"

    def is_available(self) -> bool:
        vault = self._resolve_vault_path()
        return bool(vault and vault.is_dir())

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._vault = self._resolve_vault_path()
        self._max_chars = _positive_int(os.getenv("OBSIDIAN_MEMORY_MAX_CHARS"), _DEFAULT_MAX_CHARS)
        self._search_limit = _positive_int(os.getenv("OBSIDIAN_MEMORY_SEARCH_LIMIT"), _DEFAULT_SEARCH_LIMIT)

    def system_prompt_block(self) -> str:
        if not self._vault:
            return ""
        return (
            "Obsidian memory provider is active in read-only mode. "
            f"Vault: {self._vault}. Search/read compact durable context from "
            "mocs/ and 3-resources/wiki/ before raw or project notes. Exclude .claude/. "
            "Do not write, move, rename, or delete vault files through this provider."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or not self._vault:
            return ""
        results = self._search(query, limit=self._search_limit)
        if not results:
            return ""
        lines = ["Relevant Obsidian vault context (read-only):"]
        used = len(lines[0]) + 1
        for r in results:
            item = f"- {r.path}: {r.snippet}"
            if used + len(item) + 1 > self._max_chars:
                break
            lines.append(item)
            used += len(item) + 1
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return ALL_TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "obsidian_search":
            return self._handle_search(args)
        if tool_name == "obsidian_read":
            return self._handle_read(args)
        if tool_name == "obsidian_recent":
            return self._handle_recent(args)
        return tool_error(f"Unknown Obsidian memory tool: {tool_name}", success=False)

    def _resolve_vault_path(self) -> Optional[Path]:
        configured = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
        candidates = []
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.append(Path.home() / "vaults" / "vault-one")
        candidates.append(Path.home() / "Documents" / "Obsidian Vault")
        for p in candidates:
            if p.is_dir():
                return p.resolve()
        return None

    def _handle_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("query is required", success=False)
        limit = min(max(_positive_int(args.get("limit"), self._search_limit), 1), 20)
        scope = str(args.get("scope") or "").strip()
        results = self._search(query, limit=limit, scope=scope)
        return json.dumps({
            "success": True,
            "vault": str(self._vault) if self._vault else "",
            "query": query,
            "results": [r.__dict__ for r in results],
        }, ensure_ascii=False)

    def _handle_read(self, args: Dict[str, Any]) -> str:
        rel = str(args.get("path") or "").strip()
        if not rel:
            return tool_error("path is required", success=False)
        max_chars = min(max(_positive_int(args.get("max_chars"), 12000), 1000), _MAX_READ_CHARS)
        path = self._safe_note_path(rel)
        if path is None:
            return tool_error("path must be a vault-relative markdown file outside excluded directories", success=False)
        if not path.exists() or not path.is_file():
            return tool_error(f"note not found: {rel}", success=False)
        text = path.read_text(errors="replace")
        truncated = len(text) > max_chars
        content = text[:max_chars]
        return json.dumps({
            "success": True,
            "path": self._rel(path),
            "size": path.stat().st_size,
            "truncated": truncated,
            "content": content,
        }, ensure_ascii=False)

    def _handle_recent(self, args: Dict[str, Any]) -> str:
        limit = min(max(_positive_int(args.get("limit"), 10), 1), 50)
        scope = str(args.get("scope") or "").strip()
        notes = []
        for p in self._iter_notes(scope=scope):
            st = p.stat()
            notes.append({"path": self._rel(p), "modified": st.st_mtime, "size": st.st_size})
        notes.sort(key=lambda x: x["modified"], reverse=True)
        return json.dumps({"success": True, "results": notes[:limit]}, ensure_ascii=False)

    def _search(self, query: str, *, limit: int, scope: str = "") -> List[SearchResult]:
        terms = _terms(query)
        if not terms:
            return []
        results: List[SearchResult] = []
        for p in self._iter_notes(scope=scope):
            try:
                st = p.stat()
                if st.st_size <= 0:
                    continue
                text = p.read_text(errors="replace")
            except Exception:
                continue
            rel = self._rel(p)
            title = _title_from_note(rel, text)
            hay_title = title.lower()
            hay_path = rel.lower()
            hay_text = _strip_frontmatter(text).lower()
            score = 0
            for term in terms:
                if term in hay_title:
                    score += 30
                if term in hay_path:
                    score += 20
                count = hay_text.count(term)
                if count:
                    score += min(count, 8) * 3
            score += _priority_bonus(rel)
            if score <= _priority_bonus(rel):
                continue
            results.append(SearchResult(
                path=rel,
                title=title,
                score=score,
                snippet=_snippet(text, terms),
                size=st.st_size,
                modified=st.st_mtime,
            ))
        results.sort(key=lambda r: (r.score, r.modified), reverse=True)
        return results[:limit]

    def _iter_notes(self, scope: str = "") -> Iterable[Path]:
        if not self._vault:
            return []
        root = self._vault
        if scope:
            scoped = self._safe_dir_path(scope)
            if scoped is None:
                return []
            root = scoped
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not d.startswith(".")]
            for filename in filenames:
                if not filename.endswith(".md"):
                    continue
                p = Path(dirpath) / filename
                if self._is_excluded(p):
                    continue
                yield p

    def _safe_note_path(self, rel: str) -> Optional[Path]:
        if not self._vault:
            return None
        rel = rel.lstrip("/")
        p = (self._vault / rel).resolve()
        try:
            p.relative_to(self._vault)
        except ValueError:
            return None
        if p.suffix != ".md" or self._is_excluded(p):
            return None
        return p

    def _safe_dir_path(self, rel: str) -> Optional[Path]:
        if not self._vault:
            return None
        rel = rel.strip().lstrip("/").rstrip("/")
        p = (self._vault / rel).resolve()
        try:
            p.relative_to(self._vault)
        except ValueError:
            return None
        if self._is_excluded(p):
            return None
        return p if p.is_dir() else None

    def _is_excluded(self, p: Path) -> bool:
        if not self._vault:
            return True
        try:
            parts = p.relative_to(self._vault).parts
        except ValueError:
            return True
        return any(part in _EXCLUDED_DIRS or part.startswith(".") for part in parts)

    def _rel(self, p: Path) -> str:
        if not self._vault:
            return str(p)
        return p.relative_to(self._vault).as_posix()


def _positive_int(value: Any, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except Exception:
        return default


def _terms(query: str) -> List[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", query) if len(t) > 1]


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :]
    return text


def _title_from_note(rel: str, text: str) -> str:
    for line in _strip_frontmatter(text).splitlines()[:40]:
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip() or Path(rel).stem
    return Path(rel).stem


def _priority_bonus(rel: str) -> int:
    for idx, prefix in enumerate(_PRIORITY_PREFIXES):
        if rel.startswith(prefix):
            return max(0, 18 - idx * 2)
    return 0


def _snippet(text: str, terms: List[str], width: int = 360) -> str:
    body = re.sub(r"\s+", " ", _strip_frontmatter(text)).strip()
    if not body:
        return ""
    lower = body.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) != -1]
    if positions:
        start = max(0, min(positions) - width // 4)
    else:
        start = 0
    snippet = body[start : start + width].strip()
    if start > 0:
        snippet = "…" + snippet
    if start + width < len(body):
        snippet += "…"
    return snippet


def register_memory_provider() -> MemoryProvider:
    return ObsidianMemoryProvider()


# Discovery fallback: plugins.memory._load_provider_from_dir also instantiates
# top-level MemoryProvider subclasses when present.
