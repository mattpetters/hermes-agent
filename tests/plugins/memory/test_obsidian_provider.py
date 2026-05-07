import json
from pathlib import Path

from plugins.memory.obsidian import ObsidianMemoryProvider
from plugins.memory import load_memory_provider


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_obsidian_provider_available_and_prefetch_prioritizes_wiki(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write(vault / "3-resources/wiki/concepts/hermes-context.md", "---\ntitle: Hermes Context\n---\n# Hermes Context\nHermes uses Obsidian wiki memory for compact context retrieval.")
    _write(vault / ".claude/worktrees/dup.md", "# Duplicate\nHermes Obsidian secret duplicate should be ignored.")
    _write(vault / "0-inbox/random.md", "# Random\nNothing useful.")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("OBSIDIAN_MEMORY_MAX_CHARS", "1000")

    provider = ObsidianMemoryProvider()
    assert provider.is_available()
    provider.initialize("session-1")

    context = provider.prefetch("How should Hermes use Obsidian memory?")
    assert "3-resources/wiki/concepts/hermes-context.md" in context
    assert "compact context retrieval" in context
    assert ".claude" not in context


def test_obsidian_search_read_recent_are_read_only_and_safe(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write(vault / "mocs/hermes-moc.md", "# Hermes MOC\nObsidian memory provider notes.")
    _write(vault / ".claude/worktrees/hermes-moc.md", "# Bad\nShould not be visible.")
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))

    provider = ObsidianMemoryProvider()
    provider.initialize("session-1")

    search = json.loads(provider.handle_tool_call("obsidian_search", {"query": "Obsidian memory"}))
    assert search["success"] is True
    paths = [r["path"] for r in search["results"]]
    assert paths == ["mocs/hermes-moc.md"]

    read = json.loads(provider.handle_tool_call("obsidian_read", {"path": "mocs/hermes-moc.md"}))
    assert read["success"] is True
    assert "Obsidian memory provider" in read["content"]

    blocked = json.loads(provider.handle_tool_call("obsidian_read", {"path": ".claude/worktrees/hermes-moc.md"}))
    assert blocked["success"] is False

    recent = json.loads(provider.handle_tool_call("obsidian_recent", {"limit": 10}))
    assert [r["path"] for r in recent["results"]] == ["mocs/hermes-moc.md"]


def test_obsidian_provider_discovery_loads_bundled_plugin():
    provider = load_memory_provider("obsidian")
    assert provider is not None
    assert provider.name == "obsidian"
    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "obsidian_search",
        "obsidian_read",
        "obsidian_recent",
    }
