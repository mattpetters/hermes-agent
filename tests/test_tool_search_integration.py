"""Integration tests for progressive tool loading helpers."""

import json


def _definition(name: str, description: str = "") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class TestShouldDeferTools:
    def test_auto_defers_only_when_tool_tokens_exceed_threshold(self):
        from model_tools import should_defer_tools

        assert should_defer_tools(5000, 32768, mode="auto", threshold=0.10) is True
        assert should_defer_tools(3276, 32768, mode="auto", threshold=0.10) is False
        assert should_defer_tools(3277, 32768, mode="auto", threshold=0.10) is True

    def test_always_never_and_zero_context_modes(self):
        from model_tools import should_defer_tools

        assert should_defer_tools(1, 999999, mode="always", threshold=0.10) is True
        assert should_defer_tools(999999, 1, mode="never", threshold=0.10) is False
        assert should_defer_tools(5000, 0, mode="auto", threshold=0.10) is False


class TestToolDefinitionDeferral:
    def test_deferred_definitions_expose_meta_and_pinned_tools_only(self, monkeypatch):
        import model_tools
        from tools.registry import ToolRegistry

        test_registry = ToolRegistry()
        test_registry.register("read_file", "file", _definition("read_file", "Read files"), lambda args: "{}")
        test_registry.register("write_file", "file", _definition("write_file", "Write files"), lambda args: "{}")
        test_registry.register("web_search", "web", _definition("web_search", "Search web"), lambda args: "{}")

        monkeypatch.setattr(model_tools, "registry", test_registry)
        monkeypatch.setattr(model_tools, "validate_toolset", lambda name: name == "test")
        monkeypatch.setattr(model_tools, "resolve_toolset", lambda name: {"read_file", "write_file", "web_search"})

        tools = model_tools.get_tool_definitions(
            enabled_toolsets=["test"],
            quiet_mode=True,
            deferred=True,
            pinned_tools=["read_file"],
        )

        assert [tool["function"]["name"] for tool in tools] == ["tool_details", "tool_search", "read_file"]
        assert [entry["name"] for entry in model_tools.get_deferred_catalog()] == [
            "read_file",
            "web_search",
            "write_file",
        ]
        assert model_tools.get_all_session_tool_names() == ["read_file", "web_search", "write_file"]
        assert model_tools.get_last_resolved_tool_names() == ["tool_details", "tool_search", "read_file"]


class TestBuildToolCatalogPrompt:
    def test_catalog_prompt_groups_by_toolset_and_explains_loading(self):
        from agent.prompt_builder import build_tool_catalog_prompt

        prompt = build_tool_catalog_prompt([
            {"name": "web_search", "description": "Search the web", "toolset": "web"},
            {"name": "read_file", "description": "Read a file", "toolset": "file"},
            {"name": "tool_search", "description": "meta", "toolset": "_tool_search"},
        ])

        assert "## Available Tools (load before use)" in prompt
        assert "tool_search(query)" in prompt
        assert "tool_details(name)" in prompt
        assert "  file:\n    - read_file: Read a file" in prompt
        assert "  web:\n    - web_search: Search the web" in prompt
        assert "tool_search: meta" not in prompt

    def test_empty_catalog_returns_empty_prompt(self):
        from agent.prompt_builder import build_tool_catalog_prompt

        assert build_tool_catalog_prompt([]) == ""
