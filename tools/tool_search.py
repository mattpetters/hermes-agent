"""Tool search meta-tools for progressive disclosure of tool schemas."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from tools.registry import ToolRegistry, registry as default_registry

_META_TOOL_NAMES = {"tool_search", "tool_details"}
_WORD_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(value: Any) -> list[str]:
    """Tokenize nested schema/catalog metadata into simple lowercase terms."""
    if value is None:
        return []
    if isinstance(value, dict):
        terms: list[str] = []
        for key, item in value.items():
            terms.extend(_tokenize(key))
            terms.extend(_tokenize(item))
        return terms
    if isinstance(value, (list, tuple, set)):
        terms = []
        for item in value:
            terms.extend(_tokenize(item))
        return terms
    text = str(value).lower().replace("_", " ")
    return [match.group(0) for match in _WORD_RE.finditer(text)]


def _score_tool(query_terms: Counter[str], catalog_entry: dict, definition: dict) -> int:
    """Return a simple relevance score over name, description, and schema terms."""
    searchable = Counter(_tokenize(catalog_entry)) + Counter(_tokenize(definition.get("function", {})))
    score = 0
    for term, weight in query_terms.items():
        if term in searchable:
            score += weight * searchable[term]
    return score


def search_tool_schemas(
    args: dict,
    *,
    registry: ToolRegistry = default_registry,
) -> str:
    """Search available tools and return full schemas for relevant matches."""
    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        limit = int(args.get("limit", 8))
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 50))

    query_terms = Counter(_tokenize(query))
    matches: list[tuple[int, str, dict]] = []
    for entry in registry.get_catalog():
        name = entry.get("name", "")
        if name in _META_TOOL_NAMES:
            continue
        definition = registry.get_single_definition(name)
        if not definition:
            continue
        score = _score_tool(query_terms, entry, definition)
        if score <= 0:
            continue
        matches.append((score, name, definition))

    matches.sort(key=lambda item: (-item[0], item[1]))
    tools = [definition for _, _, definition in matches[:limit]]
    return json.dumps({"query": query, "tools": tools})


def get_tool_details(
    args: dict,
    *,
    registry: ToolRegistry = default_registry,
) -> str:
    """Return the exact full schema for one named tool."""
    name = str(args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required", "tools": []})
    if name in _META_TOOL_NAMES:
        return json.dumps({"error": f"Meta-tool cannot be loaded: {name}", "tools": []})
    definition = registry.get_single_definition(name)
    if not definition:
        return json.dumps({"error": f"Tool not found or unavailable: {name}", "tools": []})
    return json.dumps({"tools": [definition]})


def register_tool_search(*, registry: ToolRegistry = default_registry) -> None:
    """Register tool_search/tool_details meta-tools if not already present."""
    if registry.get_entry("tool_search") is None:
        registry.register(
            name="tool_search",
            toolset="_tool_search",
            schema={
                "description": "Search available Hermes tools and load full schemas for relevant matches before using them.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms describing the needed capability."},
                        "limit": {"type": "integer", "description": "Maximum number of matching tools to load."},
                    },
                    "required": ["query"],
                },
            },
            handler=lambda args, **kwargs: search_tool_schemas(args, registry=registry),
            description="Search available Hermes tools and return full schemas.",
        )
    if registry.get_entry("tool_details") is None:
        registry.register(
            name="tool_details",
            toolset="_tool_search",
            schema={
                "description": "Load the full schema for one exact Hermes tool name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Exact tool name to load."},
                    },
                    "required": ["name"],
                },
            },
            handler=lambda args, **kwargs: get_tool_details(args, registry=registry),
            description="Load one exact Hermes tool schema.",
        )
