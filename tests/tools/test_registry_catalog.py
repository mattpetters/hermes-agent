"""Tests for compact tool catalogs used by progressive tool loading."""

from tools.registry import ToolRegistry


def _schema(description: str, *, properties: dict | None = None) -> dict:
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
        },
    }


class TestRegistryCatalog:
    def test_catalog_contains_only_compact_available_metadata(self):
        registry = ToolRegistry()
        registry.register(
            name="read_file",
            toolset="file",
            schema=_schema("Read a text file", properties={"path": {"type": "string"}}),
            handler=lambda args: "{}",
        )
        registry.register(
            name="web_search",
            toolset="web",
            schema=_schema("Search the web"),
            handler=lambda args: "{}",
            check_fn=lambda: False,
        )

        catalog = registry.get_catalog({"read_file", "web_search"})

        assert catalog == [
            {
                "name": "read_file",
                "description": "Read a text file",
                "toolset": "file",
            }
        ]
        assert "parameters" not in catalog[0]

    def test_catalog_is_sorted_and_can_include_all_tools(self):
        registry = ToolRegistry()
        registry.register("z_tool", "z", _schema("Zed"), lambda args: "{}")
        registry.register("a_tool", "a", _schema("Alpha"), lambda args: "{}")

        catalog = registry.get_catalog()

        assert [entry["name"] for entry in catalog] == ["a_tool", "z_tool"]

    def test_single_definition_applies_dynamic_overrides_and_availability(self):
        registry = ToolRegistry()
        registry.register(
            name="delegate_task",
            toolset="delegation",
            schema=_schema("Old description"),
            handler=lambda args: "{}",
            dynamic_schema_overrides=lambda: {"description": "Fresh description"},
        )
        registry.register(
            name="unavailable",
            toolset="web",
            schema=_schema("Nope"),
            handler=lambda args: "{}",
            check_fn=lambda: False,
        )

        definition = registry.get_single_definition("delegate_task")

        assert definition == {
            "type": "function",
            "function": {
                "name": "delegate_task",
                "description": "Fresh description",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        assert registry.get_single_definition("unavailable") is None
        assert registry.get_single_definition("missing") is None
