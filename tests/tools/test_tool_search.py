"""Tests for tool_search/tool_details progressive loading meta-tools."""

import json

from tools.registry import ToolRegistry


def _schema(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Filesystem path"},
            },
        },
    }


class TestToolSearchMetaTools:
    def test_search_returns_relevant_full_schemas_ranked_by_metadata(self):
        from tools.tool_search import search_tool_schemas

        registry = ToolRegistry()
        registry.register("read_file", "file", _schema("read_file", "Read file contents"), lambda args: "{}")
        registry.register("write_file", "file", _schema("write_file", "Write file contents"), lambda args: "{}")
        registry.register("web_search", "web", _schema("web_search", "Search web pages"), lambda args: "{}")

        result = json.loads(search_tool_schemas({"query": "read filesystem file", "limit": 2}, registry=registry))

        assert result["query"] == "read filesystem file"
        assert [tool["function"]["name"] for tool in result["tools"]] == ["read_file", "write_file"]
        assert result["tools"][0]["function"]["parameters"]["properties"]["path"]["description"] == "Filesystem path"

    def test_search_returns_error_for_blank_query(self):
        from tools.tool_search import search_tool_schemas

        result = json.loads(search_tool_schemas({"query": "   "}, registry=ToolRegistry()))

        assert result == {"error": "query is required"}

    def test_search_excludes_meta_tools_from_results(self):
        from tools.tool_search import register_tool_search, search_tool_schemas

        registry = ToolRegistry()
        register_tool_search(registry=registry)
        registry.register("read_file", "file", _schema("read_file", "Read file contents"), lambda args: "{}")

        result = json.loads(search_tool_schemas({"query": "tool search read", "limit": 10}, registry=registry))

        assert [tool["function"]["name"] for tool in result["tools"]] == ["read_file"]

    def test_details_returns_exact_schema_or_clear_error(self):
        from tools.tool_search import get_tool_details

        registry = ToolRegistry()
        registry.register("read_file", "file", _schema("read_file", "Read file contents"), lambda args: "{}")

        found = json.loads(get_tool_details({"name": "read_file"}, registry=registry))
        missing = json.loads(get_tool_details({"name": "missing"}, registry=registry))

        assert [tool["function"]["name"] for tool in found["tools"]] == ["read_file"]
        assert missing == {"error": "Tool not found or unavailable: missing", "tools": []}

    def test_register_tool_search_is_idempotent(self):
        from tools.tool_search import register_tool_search

        registry = ToolRegistry()
        register_tool_search(registry=registry)
        register_tool_search(registry=registry)

        definitions = registry.get_definitions({"tool_search", "tool_details"}, quiet=True)

        assert sorted(tool["function"]["name"] for tool in definitions) == ["tool_details", "tool_search"]
