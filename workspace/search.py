"""Workspace search API.

Thin wrapper around SQLiteFTS5Store.search() that handles config loading
and store lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from workspace.config import WorkspaceConfig
from workspace.store import SQLiteFTS5Store
from workspace.types import SearchResult


def search_workspace(
    query: str,
    config: WorkspaceConfig,
    *,
    limit: int | None = None,
    path_prefix: str | None = None,
    file_glob: str | None = None,
) -> list[SearchResult]:
    if limit is None:
        limit = config.knowledgebase.search.default_limit

    # Resolve symlinks + relative segments so the byte-prefix match in the store
    # aligns with the indexer, which stores resolved absolute paths
    # (`str(file_path.resolve())` in `indexer.py`). Mirrors what `commands.py`
    # already does for CLI search — without this, callers who hand in a
    # symlinked path via the Python API silently get zero hits.
    resolved_prefix = str(Path(path_prefix).resolve()) if path_prefix else None

    with SQLiteFTS5Store(config.workspace_root) as store:
        return store.search(
            query,
            limit=limit,
            path_prefix=resolved_prefix,
            file_glob=file_glob,
        )
