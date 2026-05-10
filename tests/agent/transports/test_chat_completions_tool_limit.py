"""Regression tests for Chat Completions tool cap handling."""

from dataclasses import replace

import pytest

from agent.transports import get_transport
from agent.transports.chat_completions import _RESPONSES_API_MAX_TOOLS
from providers import get_provider_profile


@pytest.fixture
def transport():
    import agent.transports.chat_completions  # noqa: F401
    return get_transport("chat_completions")


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class TestChatCompletionsToolLimit:
    def test_legacy_path_truncates_to_provider_limit_with_core_priority(self, transport):
        tools = [_tool(f"z_extra_{i}") for i in range(_RESPONSES_API_MAX_TOOLS + 5)]
        tools.append(_tool("read_file"))

        kw = transport.build_kwargs(
            model="gpt-5.4",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
        )

        names = [tool["function"]["name"] for tool in kw["tools"]]
        assert len(names) == _RESPONSES_API_MAX_TOOLS
        assert "read_file" in names

    def test_provider_profile_path_truncates_to_provider_limit(self, transport):
        profile = get_provider_profile("custom")
        profile = replace(profile, name="test-profile")
        tools = [_tool(f"z_extra_{i}") for i in range(_RESPONSES_API_MAX_TOOLS + 3)]
        tools.append(_tool("terminal"))

        kw = transport.build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=tools,
            provider_profile=profile,
        )

        names = [tool["function"]["name"] for tool in kw["tools"]]
        assert len(names) == _RESPONSES_API_MAX_TOOLS
        assert "terminal" in names
