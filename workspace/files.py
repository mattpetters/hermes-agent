"""File discovery and filtering for workspace indexing.

Iterates workspace roots, applies ignore patterns (via pathspec),
skips binary files and files over the size limit.

Ignore file precedence per root (first match wins):
  1. root/.hermesignore
  2. root/.gitignore
  3. Built-in default patterns
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from workspace.config import WorkspaceConfig
from workspace.constants import (
    BINARY_SUFFIXES,
    DEFAULT_IGNORE_PATTERNS,
    GITIGNORE_NAME,
    HERMESIGNORE_NAME,
)
from workspace.types import WorkspaceRoot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveryResult:
    files: list[tuple[str, Path]]
    complete: bool
    filtered_count: int = 0


def discover_workspace_files(config: WorkspaceConfig) -> DiscoveryResult:
    """Collect workspace files plus whether discovery completed across all roots."""
    max_bytes = config.knowledgebase.indexing.max_file_mb * 1024 * 1024

    all_roots = [
        WorkspaceRoot(path=str(config.workspace_root), recursive=True),
        *config.knowledgebase.roots,
    ]

    files: list[tuple[str, Path]] = []
    complete = True
    filtered_count = 0

    for root_spec in all_roots:
        root = Path(root_spec.path).expanduser().resolve()
        if not root.is_dir():
            log.warning("Workspace root does not exist: %s", root)
            complete = False
            continue

        ignore_spec = _load_ignore_spec(root)
        iterator = root.rglob("*") if root_spec.recursive else root.iterdir()

        try:
            paths = sorted(iterator)
        except OSError:
            log.warning("Failed to enumerate workspace root: %s", root, exc_info=True)
            complete = False
            continue

        for p in paths:
            if not p.is_file():
                continue
            if p.name == HERMESIGNORE_NAME:
                # The ignore-rules file is never itself an indexable artifact —
                # hardcoded so a user editing their .hermesignore to remove the
                # self-reference can't accidentally re-enable indexing of it.
                continue
            if p.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                size = p.stat().st_size
            except (FileNotFoundError, OSError):
                log.debug("File vanished during discovery: %s", p)
                continue
            if size > max_bytes:
                log.debug("Skipping oversized file: %s", p)
                filtered_count += 1
                continue
            if size == 0:
                filtered_count += 1
                continue
            if ignore_spec is not None and _is_ignored(p, root, ignore_spec):
                continue
            files.append((str(root), p))

    return DiscoveryResult(
        files=files, complete=complete, filtered_count=filtered_count
    )


def seed_hermesignore(workspace_root: Path) -> None:
    """Create .hermesignore in the workspace root if it doesn't exist."""
    ignore_file = workspace_root / HERMESIGNORE_NAME
    if not ignore_file.exists():
        ignore_file.write_text(DEFAULT_IGNORE_PATTERNS, encoding="utf-8")


def _load_ignore_spec(root: Path):
    """Load ignore patterns for a root: .hermesignore → .gitignore → defaults."""
    try:
        import pathspec
    except ImportError:
        log.warning("pathspec not installed — ignore patterns will not be applied")
        return None

    hermesignore = root / HERMESIGNORE_NAME
    if hermesignore.is_file():
        try:
            text = hermesignore.read_text(encoding="utf-8", errors="replace")
            return pathspec.PathSpec.from_lines("gitwildmatch", text.splitlines())
        except Exception:
            log.warning("Failed to parse %s", hermesignore, exc_info=True)

    gitignore = root / GITIGNORE_NAME
    if gitignore.is_file():
        try:
            text = gitignore.read_text(encoding="utf-8", errors="replace")
            return pathspec.PathSpec.from_lines("gitwildmatch", text.splitlines())
        except Exception:
            log.warning("Failed to parse %s", gitignore, exc_info=True)

    return pathspec.PathSpec.from_lines(
        "gitwildmatch", DEFAULT_IGNORE_PATTERNS.splitlines()
    )


def _is_ignored(path: Path, root: Path, spec) -> bool:
    rel = path.relative_to(root).as_posix()
    return spec.match_file(rel)
