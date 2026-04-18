"""Workspace indexing pipeline.

Discovers files → checks content hash + config signature → dispatches to the
appropriate `chonkie.Pipeline` (markdown / code / plain) → iterates the
pipeline's modality-specific output into ChunkRecords → stores in SQLite FTS5.

One Pipeline per file kind is built per `index_workspace` call. Chonkie caches
component instances keyed by init kwargs, so components are fully reused across
files of the same kind within a run.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from chonkie import Pipeline

from workspace.config import ChunkingConfig, WorkspaceConfig
from workspace.constants import (
    CHUNKING_PLAN_VERSION,
    CODE_SUFFIXES,
    MARKDOWN_SUFFIXES,
    WORKSPACE_SUBDIRS,
    get_index_dir,
)
from workspace.files import discover_workspace_files, seed_hermesignore
from workspace.store import SQLiteFTS5Store
from workspace.types import ChunkRecord, FileRecord, IndexingError, IndexSummary

log = logging.getLogger(__name__)

_replace = dataclasses.replace

ProgressCallback = Callable[[int, int, str], None]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

_MAX_ERRORS = 50


def _require_chonkie() -> None:
    try:
        import chonkie  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "Chonkie is required for workspace indexing. "
            "Install it with: pip install hermes-agent[workspace]"
        )


def index_workspace(
    config: WorkspaceConfig,
    *,
    progress: ProgressCallback | None = None,
) -> IndexSummary:
    _require_chonkie()

    start = time.monotonic()
    ensure_workspace_dirs(config)
    config_sig = _config_signature(config)

    files_indexed = 0
    files_skipped = 0
    files_errored = 0
    chunks_created = 0
    errors: list[IndexingError] = []

    discovery = discover_workspace_files(config)
    files_skipped += discovery.filtered_count
    all_files = discovery.files
    total = len(all_files)
    disk_paths: set[str] = set()

    pipelines = _build_pipelines(config.knowledgebase.chunking)

    with SQLiteFTS5Store(config.workspace_root) as store:
        for i, (root_path, file_path) in enumerate(all_files):
            abs_path = str(file_path.resolve())
            disk_paths.add(abs_path)
            write_started = False

            if progress:
                progress(i + 1, total, abs_path)

            try:
                content_hash = _file_hash(file_path)
                existing = store.get_file_record(abs_path)
                if (
                    existing
                    and existing.content_hash == content_hash
                    and existing.config_signature == config_sig
                ):
                    files_skipped += 1
                    continue

                text = _read_file_text(file_path)
                if text is None:
                    files_errored += 1
                    _append_error(
                        errors,
                        IndexingError(
                            path=abs_path,
                            stage="read",
                            error_type="EncodingError",
                            message="Could not decode file with sufficient confidence",
                        ),
                    )
                    continue

                if not text.strip():
                    files_skipped += 1
                    continue

                suffix = file_path.suffix.lower()
                chunk_records = _process_file(abs_path, text, suffix, pipelines)

                stat = file_path.stat()
                record = FileRecord(
                    abs_path=abs_path,
                    root_path=root_path,
                    content_hash=content_hash,
                    config_signature=config_sig,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    indexed_at=datetime.now(tz=timezone.utc).isoformat(),
                    chunk_count=len(chunk_records),
                )

                # Replace a file's rows atomically so a failed rebuild never
                # destroys the previously indexed version of that file.
                store.conn.execute("SAVEPOINT workspace_file_update")
                write_started = True
                store.delete_chunks_for_file(abs_path)
                store.upsert_file(record)
                if chunk_records:
                    store.insert_chunks(chunk_records)
                store.conn.execute("RELEASE SAVEPOINT workspace_file_update")
                store.commit()
                write_started = False

                files_indexed += 1
                chunks_created += len(chunk_records)

            except Exception as exc:
                if write_started:
                    try:
                        store.conn.execute(
                            "ROLLBACK TO SAVEPOINT workspace_file_update"
                        )
                        store.conn.execute("RELEASE SAVEPOINT workspace_file_update")
                    except Exception:
                        log.warning(
                            "Failed to roll back workspace update for %s",
                            abs_path,
                            exc_info=True,
                        )
                files_errored += 1
                stage = "discover" if isinstance(exc, FileNotFoundError) else "store"
                _append_error(
                    errors,
                    IndexingError(
                        path=abs_path,
                        stage=stage,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    ),
                )
                log.warning("Failed to index %s: %s", abs_path, exc, exc_info=True)
                continue

        if discovery.complete:
            pruned = _prune_stale(store, disk_paths)
        else:
            pruned = 0
            log.warning(
                "Workspace discovery was incomplete; skipping stale prune for this run"
            )
        store.commit()

    elapsed = time.monotonic() - start
    return IndexSummary(
        files_indexed=files_indexed,
        files_skipped=files_skipped,
        files_pruned=pruned,
        files_errored=files_errored,
        chunks_created=chunks_created,
        duration_seconds=elapsed,
        errors=errors,
        errors_truncated=files_errored > _MAX_ERRORS,
    )


def _append_error(errors: list[IndexingError], error: IndexingError) -> None:
    if len(errors) < _MAX_ERRORS:
        errors.append(error)


def _read_file_text(path: Path) -> str | None:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        result = from_bytes(raw).best()
        if result is None or result.encoding is None:
            return None
        if result.coherence < 0.5:
            return None
        return str(result)
    except ImportError:
        log.debug("charset-normalizer not installed, skipping non-UTF8 file: %s", path)
        return None


def ensure_workspace_dirs(config: WorkspaceConfig) -> None:
    root = config.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    for sub in WORKSPACE_SUBDIRS:
        (root / sub).mkdir(exist_ok=True)
    get_index_dir(root).mkdir(parents=True, exist_ok=True)
    seed_hermesignore(root)


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------


def _build_pipelines(ch: ChunkingConfig) -> dict[str, Pipeline]:
    """Build one Pipeline per file kind, sharing overlap-refinery config.

    Chonkie's Pipeline caches component instances internally keyed by init
    kwargs, so constructing a pipeline once per indexing run is enough to
    get full reuse across files of the same kind.
    """
    overlap_kwargs = dict(
        tokenizer="word",
        context_size=ch.overlap,
        mode="token",
        method="suffix",
        merge=False,
    )
    return {
        "markdown": (
            Pipeline()
            .process_with("markdown", tokenizer="word")
            .chunk_with("recursive", tokenizer="word", chunk_size=ch.chunk_size)
            .refine_with("overlap", **overlap_kwargs)
        ),
        "code": (
            Pipeline()
            .chunk_with(
                "code",
                tokenizer="word",
                chunk_size=ch.chunk_size,
                language="auto",
            )
            .refine_with("overlap", **overlap_kwargs)
        ),
        "plain": (
            Pipeline()
            .chunk_with("recursive", tokenizer="word", chunk_size=ch.chunk_size)
            .refine_with("overlap", **overlap_kwargs)
        ),
    }


# ---------------------------------------------------------------------------
# File processing pipeline
# ---------------------------------------------------------------------------


def _process_file(
    abs_path: str,
    text: str,
    suffix: str,
    pipelines: dict[str, Pipeline],
) -> list[ChunkRecord]:
    if suffix in MARKDOWN_SUFFIXES:
        return _process_markdown(abs_path, text, pipelines)
    elif suffix in CODE_SUFFIXES:
        return _process_code(abs_path, text, pipelines)
    else:
        return _process_plain(abs_path, text, pipelines)


def _process_markdown(
    abs_path: str,
    text: str,
    pipelines: dict[str, Pipeline],
) -> list[ChunkRecord]:
    doc = pipelines["markdown"].run(texts=text)

    headings = _scan_headings(text)
    line_offsets = _build_line_offsets(text)
    candidates: list[ChunkRecord] = []

    for chunk in doc.chunks:
        if not chunk.text.strip():
            continue
        sc, ec = chunk.start_index, chunk.end_index
        candidates.append(
            ChunkRecord(
                chunk_id=_make_id(),
                abs_path=abs_path,
                chunk_index=0,
                content=chunk.text,
                token_count=chunk.token_count,
                start_line=_offset_to_line(line_offsets, sc),
                end_line=_offset_to_line(line_offsets, max(0, ec - 1)),
                start_char=sc,
                end_char=ec,
                section=_nearest_heading(headings, sc),
                kind="markdown_text",
                context=chunk.context,
            )
        )

    for code in doc.code:
        if not code.content.strip():
            continue
        sc, ec = code.start_index, code.end_index
        metadata = (
            json.dumps({"language": code.language}) if code.language else None
        )
        candidates.append(
            ChunkRecord(
                chunk_id=_make_id(),
                abs_path=abs_path,
                chunk_index=0,
                content=code.content,
                token_count=len(code.content.split()),
                start_line=_offset_to_line(line_offsets, sc),
                end_line=_offset_to_line(line_offsets, max(0, ec - 1)),
                start_char=sc,
                end_char=ec,
                section=_nearest_heading(headings, sc),
                kind="markdown_code",
                chunk_metadata=metadata,
            )
        )

    for table in doc.tables:
        if not table.content.strip():
            continue
        sc, ec = table.start_index, table.end_index
        candidates.append(
            ChunkRecord(
                chunk_id=_make_id(),
                abs_path=abs_path,
                chunk_index=0,
                content=table.content,
                token_count=len(table.content.split()),
                start_line=_offset_to_line(line_offsets, sc),
                end_line=_offset_to_line(line_offsets, max(0, ec - 1)),
                start_char=sc,
                end_char=ec,
                section=_nearest_heading(headings, sc),
                kind="markdown_table",
            )
        )

    for image in doc.images:
        if not image.alias:
            continue
        sc, ec = image.start_index, image.end_index
        candidates.append(
            ChunkRecord(
                chunk_id=_make_id(),
                abs_path=abs_path,
                chunk_index=0,
                content=image.alias,
                token_count=len(image.alias.split()),
                start_line=_offset_to_line(line_offsets, sc),
                end_line=_offset_to_line(line_offsets, max(0, ec - 1)),
                start_char=sc,
                end_char=ec,
                section=_nearest_heading(headings, sc),
                kind="markdown_image",
            )
        )

    candidates.sort(key=lambda c: c.start_char)
    return [_replace(c, chunk_index=i) for i, c in enumerate(candidates)]


def _process_code(
    abs_path: str,
    text: str,
    pipelines: dict[str, Pipeline],
) -> list[ChunkRecord]:
    doc = pipelines["code"].run(texts=text)
    line_offsets = _build_line_offsets(text)
    records: list[ChunkRecord] = []
    for i, chunk in enumerate(doc.chunks):
        sc, ec = chunk.start_index, chunk.end_index
        records.append(
            ChunkRecord(
                chunk_id=_make_id(),
                abs_path=abs_path,
                chunk_index=i,
                content=chunk.text,
                token_count=chunk.token_count,
                start_line=_offset_to_line(line_offsets, sc),
                end_line=_offset_to_line(line_offsets, max(0, ec - 1)),
                start_char=sc,
                end_char=ec,
                section=None,
                kind="code",
                chunk_metadata=None,
                context=chunk.context,
            )
        )
    return records


def _process_plain(
    abs_path: str,
    text: str,
    pipelines: dict[str, Pipeline],
) -> list[ChunkRecord]:
    doc = pipelines["plain"].run(texts=text)
    line_offsets = _build_line_offsets(text)
    records: list[ChunkRecord] = []
    for i, chunk in enumerate(doc.chunks):
        sc, ec = chunk.start_index, chunk.end_index
        records.append(
            ChunkRecord(
                chunk_id=_make_id(),
                abs_path=abs_path,
                chunk_index=i,
                content=chunk.text,
                token_count=chunk.token_count,
                start_line=_offset_to_line(line_offsets, sc),
                end_line=_offset_to_line(line_offsets, max(0, ec - 1)),
                start_char=sc,
                end_char=ec,
                section=None,
                kind="text",
                context=chunk.context,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Heading scanning and section assignment
# ---------------------------------------------------------------------------


def _scan_headings(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0).strip()) for m in _HEADING_RE.finditer(text)]


def _nearest_heading(headings: list[tuple[int, str]], char_offset: int) -> str | None:
    best = None
    for offset, heading in headings:
        if offset <= char_offset:
            best = heading
        else:
            break
    return best


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


_NEWLINE_RE = re.compile(r"\n")


def _build_line_offsets(text: str) -> list[int]:
    return [0] + [m.end() for m in _NEWLINE_RE.finditer(text)]


def _offset_to_line(offsets: list[int], char_offset: int) -> int:
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= char_offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _config_signature(config: WorkspaceConfig) -> str:
    ch = config.knowledgebase.chunking
    blob = json.dumps(
        {
            "chunk_size": ch.chunk_size,
            "overlap": ch.overlap,
            "overlap_mode": "token",
            "overlap_method": "suffix",
            "code_chunker": "production_v1",
            "chunking_plan_version": CHUNKING_PLAN_VERSION,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _make_id() -> str:
    return f"chnk_{uuid.uuid4().hex[:12]}"


def _prune_stale(store: SQLiteFTS5Store, disk_paths: set[str]) -> int:
    indexed = store.all_indexed_paths()
    stale = indexed - disk_paths
    for path in stale:
        store.delete_file(path)
    return len(stale)
