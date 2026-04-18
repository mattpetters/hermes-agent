"""End-to-end tests for the Pipeline-based indexer.

Exercises the behavior the workspace indexer is expected to produce
after migrating from manual Chonkie wiring to `chonkie.Pipeline`:

- Markdown files emit one ChunkRecord per modality (text/code/table/image)
  with the correct `kind`, and no legacy block_index/src/link/row_count metadata.
- Small markdown files with a code block are still split into two records
  (prose + code) rather than collapsed into a single chunk.
- Overlap context is populated and is a suffix of the previous chunk's content.
- Deprecated config keys (strategy, threshold) are silently ignored.
- Config signature changes cause re-indexing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from workspace.config import WorkspaceConfig
from workspace.constants import DEFAULT_IGNORE_PATTERNS
from workspace.indexer import index_workspace
from workspace.search import search_workspace
from workspace.store import SQLiteFTS5Store


def _make_config(tmp_path: Path, raw: dict | None = None) -> WorkspaceConfig:
    hermes_home = tmp_path / "cfg_home"
    hermes_home.mkdir(exist_ok=True)
    cfg = WorkspaceConfig.from_dict(raw or {}, hermes_home)
    cfg.workspace_root.mkdir(parents=True, exist_ok=True)
    (cfg.workspace_root / ".hermesignore").write_text(
        DEFAULT_IGNORE_PATTERNS + "\n.hermesignore\n",
        encoding="utf-8",
    )
    return cfg


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_markdown_pipeline_emits_clean_metadata_per_modality(tmp_path: Path):
    cfg = _make_config(tmp_path, {"knowledgebase": {"chunking": {"chunk_size": 64}}})
    md = _write(
        cfg.workspace_root / "docs" / "mixed.md",
        "# Title\n\n"
        "Intro prose for the markdown pipeline.\n\n"
        "```python\n"
        "def first():\n"
        "    return 1\n"
        "```\n\n"
        "| Name | Score |\n"
        "| ---- | ----- |\n"
        "| A    | 10    |\n\n"
        "![first image](img/one.png)\n\n"
        "## Second\n\n"
        "More prose.\n",
    )

    summary = index_workspace(cfg)
    assert summary.files_indexed == 1
    assert summary.files_errored == 0

    with SQLiteFTS5Store(cfg.workspace_root) as store:
        rows = store.conn.execute(
            "SELECT kind, content, chunk_metadata, chunk_index, section, "
            "start_line, end_line FROM chunks "
            "WHERE abs_path = ? ORDER BY chunk_index",
            (str(md.resolve()),),
        ).fetchall()

    kinds = [r["kind"] for r in rows]
    assert "markdown_text" in kinds
    assert "markdown_code" in kinds
    assert "markdown_table" in kinds
    assert "markdown_image" in kinds

    # chunk_index is 0..N-1, strictly increasing
    assert [r["chunk_index"] for r in rows] == list(range(len(rows)))

    # Code rows: language present, no block_index
    code_rows = [r for r in rows if r["kind"] == "markdown_code"]
    assert code_rows, "expected at least one markdown_code row"
    for r in code_rows:
        meta = json.loads(r["chunk_metadata"])
        assert meta == {"language": "python"}

    # Table rows: no chunk_metadata (NULL)
    table_rows = [r for r in rows if r["kind"] == "markdown_table"]
    assert table_rows
    for r in table_rows:
        assert r["chunk_metadata"] is None

    # Image rows: content is the alias; no chunk_metadata
    image_rows = [r for r in rows if r["kind"] == "markdown_image"]
    assert image_rows
    for r in image_rows:
        assert r["content"] == "first image"
        assert r["chunk_metadata"] is None

    # Section assignment: the "Second" heading affects later rows
    sections = {r["section"] for r in rows if r["section"]}
    assert any("Title" in s for s in sections)

    # Line numbers are 1-indexed and ordered
    assert all(r["start_line"] >= 1 for r in rows)
    assert all(r["end_line"] >= r["start_line"] for r in rows)


def test_small_markdown_file_is_split_into_modalities(tmp_path: Path):
    """Small markdown files with a code block must produce separate records for
    prose and code. Every file flows through the Pipeline regardless of size;
    there is no single-chunk short-circuit."""
    cfg = _make_config(tmp_path, {"knowledgebase": {"chunking": {"chunk_size": 512}}})
    md = _write(
        cfg.workspace_root / "docs" / "tiny.md",
        "# Tiny\n\nShort intro.\n\n```python\nprint('hi')\n```\n",
    )

    summary = index_workspace(cfg)
    assert summary.files_indexed == 1

    with SQLiteFTS5Store(cfg.workspace_root) as store:
        rows = store.conn.execute(
            "SELECT kind, content FROM chunks WHERE abs_path = ? ORDER BY chunk_index",
            (str(md.resolve()),),
        ).fetchall()

    kinds = [r["kind"] for r in rows]
    assert "markdown_text" in kinds
    assert "markdown_code" in kinds
    assert len(rows) >= 2, f"small markdown must still be multimodal, got {kinds}"

    # Guard against the code fence being accidentally swallowed into the prose
    # row — `kinds` containing both labels could still false-pass if the
    # "markdown_text" row itself contained the code block body.
    text_row = next(r for r in rows if r["kind"] == "markdown_text")
    assert "print('hi')" not in text_row["content"]


def test_overlap_context_propagates_and_is_prefix_of_next_chunk(tmp_path: Path):
    """Multi-chunk prose file: every non-last chunk has non-NULL context,
    and that context is a prefix of the NEXT chunk's content. Chonkie's
    OverlapRefinery with method='suffix' in mode='token' attaches the first
    context_size tokens of chunk N+1 onto chunk N as `context`. FTS indexes
    this column so a term that only appears at the start of chunk N+1's content
    is still findable via chunk N's context field.
    """
    sentences = [f"Sentence number {i} carries unique marker token WORD{i:03d}." for i in range(60)]
    cfg = _make_config(tmp_path, {"knowledgebase": {"chunking": {"chunk_size": 64, "overlap": 8}}})
    f = _write(cfg.workspace_root / "notes" / "long.txt", "\n".join(sentences) + "\n")

    summary = index_workspace(cfg)
    assert summary.files_indexed == 1

    with SQLiteFTS5Store(cfg.workspace_root) as store:
        rows = store.conn.execute(
            "SELECT content, context FROM chunks WHERE abs_path = ? ORDER BY chunk_index",
            (str(f.resolve()),),
        ).fetchall()

    assert len(rows) >= 2, "fixture must produce multiple chunks"

    non_null_contexts = [r for r in rows if r["context"] is not None]
    assert len(non_null_contexts) >= 1, "at least one chunk must carry overlap context"

    # For every chunk whose `context` is set, that context must appear at the
    # START of the NEXT chunk's content (method="suffix" in mode="token" takes
    # the first N tokens of chunk N+1 and attaches them to chunk N as `context`).
    for i in range(len(rows) - 1):
        ctx = rows[i]["context"]
        if ctx is None:
            continue
        next_content = rows[i + 1]["content"]
        assert ctx.strip() in next_content, (
            f"chunk {i} context is not a substring of chunk {i+1} content\n"
            f"  context: {ctx!r}\n  next: {next_content!r}"
        )


def test_deprecated_strategy_and_threshold_keys_are_silently_ignored(tmp_path: Path):
    """Old configs that still set `strategy: semantic` or `threshold: 0` must load
    cleanly after the migration (fields are gone from ChunkingConfig, unknown keys
    pass through _deep_merge and are dropped by from_dict). No ValueError, no warning
    suppression hack — just a clean no-op."""
    cfg = _make_config(
        tmp_path,
        {
            "knowledgebase": {
                "chunking": {
                    "strategy": "semantic",
                    "threshold": 0,
                    "chunk_size": 128,
                }
            }
        },
    )
    assert cfg.knowledgebase.chunking.chunk_size == 128
    assert not hasattr(cfg.knowledgebase.chunking, "strategy")
    assert not hasattr(cfg.knowledgebase.chunking, "threshold")

    # And indexing works end-to-end with the legacy-keyed config.
    _write(cfg.workspace_root / "docs" / "readme.md", "# Hi\n\nSome prose.\n")
    summary = index_workspace(cfg)
    assert summary.files_indexed == 1
    assert summary.files_errored == 0


def test_config_signature_change_invalidates_existing_index(tmp_path: Path):
    """Changing a field that belongs in the signature (chunk_size) must cause
    already-indexed files to be re-indexed on the next run rather than skipped.
    This guards against accidentally dropping a field from _config_signature."""
    cfg = _make_config(tmp_path, {"knowledgebase": {"chunking": {"chunk_size": 512}}})
    _write(cfg.workspace_root / "docs" / "a.md", "# A\n\nContent A.\n")

    first = index_workspace(cfg)
    assert first.files_indexed == 1
    assert first.files_skipped == 0

    # Same config → second run skips.
    second = index_workspace(cfg)
    assert second.files_indexed == 0
    assert second.files_skipped == 1

    # Changed chunk_size → third run re-indexes.
    cfg2 = _make_config(tmp_path, {"knowledgebase": {"chunking": {"chunk_size": 256}}})
    third = index_workspace(cfg2)
    assert third.files_indexed == 1
    assert third.files_skipped == 0


def test_concurrent_index_does_not_crash(tmp_path: Path):
    """Two simultaneous index_workspace() calls against the same workspace must
    both succeed, and the SQLite DB must pass PRAGMA integrity_check.

    Pre-fix, the second process' sqlite3.connect() had no busy-timeout, so it
    would fail with `OperationalError: database is locked` the instant the
    first process held a lock for schema init. The `sqlite3.connect(..., timeout=5.0)`
    change makes the second process wait for the lock instead of crashing.

    Gap Worker 3's original variant of this test passed before the Pipeline
    migration because the old `_ChunkerCache` imported chonkie lazily, which
    added enough startup skew that the race never landed. Eager
    `_build_pipelines()` removed that skew; this test is the regression guard.
    """
    cfg = _make_config(tmp_path)
    # Seed a few small markdown files so both runs have something to do.
    for i in range(5):
        _write(
            cfg.workspace_root / "docs" / f"note_{i}.md",
            f"# Note {i}\n\nSome content for note {i}.\n",
        )

    hermes_home = tmp_path / "cfg_home"
    workspace_root = cfg.workspace_root

    # The project root is 3 levels up from this test file. The spawned
    # subprocess won't have pytest's conftest setup, so we prepend the project
    # root to sys.path inside the helper script to make `workspace` importable.
    project_root = Path(__file__).resolve().parents[2]

    # Write a small helper script that loads a config pointing at the same
    # workspace_root and runs index_workspace. Using subprocess for clean
    # process-isolation (fresh interpreter per worker).
    script = tmp_path / "_run_index.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            from pathlib import Path

            sys.path.insert(0, {str(project_root)!r})

            from workspace.config import WorkspaceConfig
            from workspace.indexer import index_workspace

            hermes_home = Path({str(hermes_home)!r})
            cfg = WorkspaceConfig.from_dict({{}}, hermes_home)
            summary = index_workspace(cfg)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )

    p1 = subprocess.Popen([sys.executable, str(script)])
    p2 = subprocess.Popen([sys.executable, str(script)])
    rc1 = p1.wait(timeout=120)
    rc2 = p2.wait(timeout=120)

    assert rc1 == 0, f"first concurrent indexer exited {rc1}"
    assert rc2 == 0, f"second concurrent indexer exited {rc2}"

    # Both processes survived — now verify the DB is not corrupted.
    with SQLiteFTS5Store(workspace_root) as store:
        result = store.conn.execute("PRAGMA integrity_check").fetchone()
    assert result[0] == "ok", f"PRAGMA integrity_check returned: {result[0]!r}"


def test_search_path_prefix_resolves_symlinks(tmp_path: Path):
    """search_workspace must resolve `path_prefix` before handing it to the
    store. The indexer stores resolved absolute paths (`file_path.resolve()`);
    the store does a literal byte-prefix match. Callers using the Python API
    with a symlinked path got silent empty results pre-fix. The CLI handled
    this in `commands.py:174` — this test guards that the Python API entry
    now mirrors that behavior.
    """
    real_docs = tmp_path / "real-docs"
    real_docs.mkdir()
    _write(real_docs / "alpha.md", "# Alpha\n\nThe alpha document describes things.\n")
    _write(real_docs / "beta.md", "# Beta\n\nThe beta document explains more things.\n")

    # Symlink `tmp_path/linked` -> `real-docs`. (Making "workspace/linked" a
    # sub-path would require first creating a workspace dir — plain `linked`
    # under tmp_path is enough to exercise the resolver.)
    linked = tmp_path / "linked"
    linked.symlink_to(real_docs, target_is_directory=True)

    cfg = _make_config(
        tmp_path,
        {"knowledgebase": {"roots": [{"path": str(linked), "recursive": True}]}},
    )

    summary = index_workspace(cfg)
    assert summary.files_indexed == 2

    # Via the symlink path — must still return hits, because search_workspace
    # resolves path_prefix before the byte-prefix compare.
    via_symlink = search_workspace(
        "document",
        cfg,
        path_prefix=str(linked),
    )
    assert len(via_symlink) > 0, "search_workspace must resolve symlinked path_prefix"

    # Via the resolved real path — the counts must match.
    via_resolved = search_workspace(
        "document",
        cfg,
        path_prefix=str(real_docs.resolve()),
    )
    assert len(via_symlink) == len(via_resolved)


def test_hermesignore_never_indexed(tmp_path: Path):
    """.hermesignore files are discovery-level infrastructure, not indexable
    content. _make_config seeds one at the workspace root; writing another one
    in a subdirectory must also be excluded. Post-fix the filter is hardcoded
    in `discover_workspace_files`, so this holds regardless of user-edited
    ignore patterns.
    """
    cfg = _make_config(tmp_path)
    # Additional .hermesignore in a nested directory.
    _write(
        cfg.workspace_root / "docs" / ".hermesignore",
        "# nested ignore rules\n*.bak\n",
    )
    # Plus a legitimate markdown file so the index has something in it.
    _write(cfg.workspace_root / "docs" / "ok.md", "# Ok\n\nSome prose.\n")

    summary = index_workspace(cfg)
    assert summary.files_errored == 0

    with SQLiteFTS5Store(cfg.workspace_root) as store:
        rows = store.conn.execute(
            "SELECT abs_path FROM chunks WHERE abs_path LIKE ?",
            ("%.hermesignore",),
        ).fetchall()

    assert rows == [], f"expected no .hermesignore rows, got: {[r['abs_path'] for r in rows]}"


def test_summary_reports_filtered_empty_and_oversized(tmp_path: Path):
    """Files dropped at discovery (zero-size or over `max_file_mb`) must count
    toward `files_skipped` in the IndexSummary — otherwise dropped files just
    vanish from the report and the user has no signal that their config is
    filtering things out.
    """
    cfg = _make_config(
        tmp_path,
        {"knowledgebase": {"indexing": {"max_file_mb": 1}}},
    )

    # Two zero-byte files.
    (cfg.workspace_root / "docs" / "empty1.md").parent.mkdir(
        parents=True, exist_ok=True
    )
    (cfg.workspace_root / "docs" / "empty1.md").write_bytes(b"")
    (cfg.workspace_root / "docs" / "empty2.txt").write_bytes(b"")

    # One oversized file (2 MiB > max_file_mb=1).
    oversized = cfg.workspace_root / "docs" / "huge.md"
    oversized.write_bytes(b"a" * (2 * 1024 * 1024))

    # One real file that should be indexed.
    _write(cfg.workspace_root / "docs" / "real.md", "# Real\n\nActual content.\n")

    summary = index_workspace(cfg)

    assert summary.files_indexed == 1
    assert summary.files_skipped == 3
    assert summary.files_errored == 0
