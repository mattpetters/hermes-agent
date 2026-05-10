"""Agent-level tests for progressive tool loading lifecycle."""

import json
import types

from run_agent import AIAgent


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _agent_stub() -> AIAgent:
    agent = AIAgent.__new__(AIAgent)
    agent._tool_search_active = True
    agent.tools = [_schema("tool_search"), _schema("tool_details")]
    agent.valid_tool_names = {"tool_search", "tool_details"}
    agent._loaded_tools = {}
    agent._pinned_tool_names = {"tool_search", "tool_details"}
    agent._tool_evict_after = 10
    agent._api_call_count = 7
    agent.quiet_mode = True
    agent.log_prefix = ""
    agent._vprint = lambda *args, **kwargs: None
    return agent


class TestDynamicToolLoading:
    def test_ingests_tool_search_result_into_active_tool_surface(self):
        agent = _agent_stub()
        result = json.dumps({"tools": [_schema("read_file")]})

        loaded = agent._ingest_tool_search_result("tool_search", result)

        assert loaded == ["read_file"]
        assert "read_file" in agent.valid_tool_names
        assert agent.tools[-1]["function"]["name"] == "read_file"
        assert agent._loaded_tools == {"read_file": 7}

    def test_does_not_duplicate_already_loaded_tool(self):
        agent = _agent_stub()
        agent.tools.append(_schema("read_file"))
        agent.valid_tool_names.add("read_file")
        result = json.dumps({"tools": [_schema("read_file")]})

        assert agent._ingest_tool_search_result("tool_details", result) == []
        assert [tool["function"]["name"] for tool in agent.tools].count("read_file") == 1

    def test_tracks_loaded_tool_usage(self):
        agent = _agent_stub()
        agent._loaded_tools = {"read_file": 1}

        assert agent._ingest_tool_search_result("read_file", "{}") == []
        assert agent._loaded_tools["read_file"] == 7

    def test_auto_loads_deferred_direct_call(self, monkeypatch):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("read_file", "file", _schema("read_file")["function"], lambda args: "{}")
        monkeypatch.setattr("run_agent._tool_registry", registry, raising=False)
        agent = _agent_stub()
        tool_call = types.SimpleNamespace(function=types.SimpleNamespace(name="read_file"))

        assert agent._auto_load_deferred_tool_call(tool_call) is True
        assert "read_file" in agent.valid_tool_names
        assert agent._loaded_tools["read_file"] == 7


class TestToolEviction:
    def test_evicts_stale_non_pinned_loaded_tools(self):
        agent = _agent_stub()
        agent.tools.extend([_schema("read_file"), _schema("web_search"), _schema("terminal")])
        agent.valid_tool_names.update({"read_file", "web_search", "terminal"})
        agent._loaded_tools = {"read_file": 0, "web_search": 6, "terminal": 0}
        agent._pinned_tool_names.add("terminal")
        agent._api_call_count = 12

        evicted = agent._evict_stale_tools()

        assert evicted == ["read_file"]
        assert "read_file" not in agent.valid_tool_names
        assert "web_search" in agent.valid_tool_names
        assert "terminal" in agent.valid_tool_names
        assert "read_file" not in agent._loaded_tools
