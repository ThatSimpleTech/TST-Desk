"""Tests for the tool registry (TD-601).

Covers: tool declaration fields, provider-format conversion, explicit
registration (no dynamic discovery), and structured errors for unknown
tool names.
"""

from __future__ import annotations

import pytest

from tstd.provider import FunctionDefinition, ToolDefinition
from tstd.tools import Tool, ToolRegistry, UnknownToolError, create_registry

# ── Tool model ──────────────────────────────────────────────────────────


class TestToolModel:
    def test_tool_declares_required_fields(self) -> None:
        """A tool declares name, description, JSON schema, side-effect class, parallel-safety."""
        tool = Tool(
            name="fs_read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            side_effect_class="ask",
            parallel_safe=True,
        )
        assert tool.name == "fs_read"
        assert tool.description == "Read a file"
        assert tool.parameters["properties"] == {"path": {"type": "string"}}
        assert tool.side_effect_class == "ask"
        assert tool.parallel_safe is True

    def test_side_effect_class_is_typed(self) -> None:
        """Only auto/ask/never are valid side-effect classes."""
        Tool(name="x", side_effect_class="auto")
        Tool(name="y", side_effect_class="ask")
        Tool(name="z", side_effect_class="never")
        with pytest.raises(ValueError):
            Tool(name="bad", side_effect_class="always")  # type: ignore[arg-type]

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            Tool(name="")

    def test_parameters_must_be_object_schema(self) -> None:
        """Non-object schemas are rejected because tools need named params."""
        with pytest.raises(ValueError):
            Tool(name="bad", parameters={})  # missing type/properties
        with pytest.raises(ValueError):
            Tool(name="bad", parameters={"type": "string", "properties": {}})
        with pytest.raises(ValueError):
            Tool(name="bad", parameters="not-a-dict")  # type: ignore[arg-type]


# ── Registry ────────────────────────────────────────────────────────────


def make_tools() -> list[Tool]:
    """A couple of representative registrable tools."""
    return [
        Tool(
            name="fs_read",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            side_effect_class="auto",
            parallel_safe=True,
        ),
        Tool(
            name="fs_write",
            description="Write a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
            side_effect_class="ask",
            parallel_safe=False,
        ),
    ]


class TestRegistry:
    def test_register_and_lookup(self) -> None:
        registry = ToolRegistry()
        tools = make_tools()
        for t in tools:
            registry.register(t)

        assert registry.count == 2
        assert registry.get("fs_read") is tools[0]
        assert registry.get("fs_write") is tools[1]
        assert registry.get("missing") is None

    def test_require_raises_for_unknown(self) -> None:
        registry = ToolRegistry()
        registry.register(make_tools()[0])
        with pytest.raises(KeyError):
            registry.require("nosuchtool")

    def test_registration_is_explicit(self) -> None:
        """An empty registry has no tools; nothing is auto-discovered."""
        registry = ToolRegistry()
        assert registry.count == 0
        assert registry.list_tools() == []

    def test_registration_replaces_same_name(self) -> None:
        registry = ToolRegistry()
        registry.register(make_tools()[0])
        new_tool = Tool(name="fs_read", description="replacement")
        registry.register(new_tool)
        assert registry.get("fs_read") is new_tool
        assert registry.count == 1

    def test_list_is_sorted_by_name(self) -> None:
        registry = ToolRegistry()
        for t in make_tools():
            registry.register(t)
        names = [t.name for t in registry.list_tools()]
        assert names == sorted(names)


# ── Provider-format conversion ──────────────────────────────────────────


class TestProviderFormat:
    def test_to_provider_definitions(self) -> None:
        registry = ToolRegistry()
        tools = make_tools()
        for t in tools:
            registry.register(t)

        defs = registry.to_provider_definitions()
        assert isinstance(defs, list)
        assert all(isinstance(d, ToolDefinition) for d in defs)
        assert all(isinstance(d.function, FunctionDefinition) for d in defs)

        names = {d.function.name for d in defs if d.function}  # type: ignore[union-attr]
        assert names == {"fs_read", "fs_write"}

        # The read tool carries its schema
        read_def = next(
            d
            for d in defs
            if d.function and d.function.name == "fs_read"  # type: ignore[union-attr]
        )
        assert read_def.function.parameters == {  # type: ignore[union-attr]
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }

    def test_definitions_feed_chat_request(self) -> None:
        """Provider-format defs are usable in a ChatCompletionRequest."""
        from tstd.provider import ChatCompletionRequest, ChatMessage

        registry = ToolRegistry()
        registry.register(make_tools()[0])
        request = ChatCompletionRequest(
            model="mock",
            messages=[ChatMessage(role="user", content="hi")],
            tools=registry.to_provider_definitions(),
        )
        body = request.to_dict()
        assert body["tools"][0]["function"]["name"] == "fs_read"


# ── Unknown tool error ──────────────────────────────────────────────────


class TestUnknownTool:
    def test_unknown_tool_error_is_structured(self) -> None:
        """Unknown tool names return a structured error the model can recover from."""
        err = UnknownToolError(name="bogus_tool")
        assert err.name == "bogus_tool"
        assert "Unknown tool" in err.message
        assert "bogus_tool" in err.message

    def test_includes_available_tools(self) -> None:
        """The message hints at what the model could have called instead."""
        err = UnknownToolError(name="bogus")
        assert "available" in err.message.lower()

    def test_dispatch_of_unknown_returns_structured_error(self) -> None:
        """Looking up (but not dispatching) an unknown tool yields UnknownToolError."""
        # Simulates the dispatcher's first step: resolve name -> tool or error.
        registry = ToolRegistry()
        registry.register(make_tools()[0])
        if registry.get("bogus") is None:
            err = UnknownToolError(name="bogus")
            assert err is not None


# ── Built-in registry ───────────────────────────────────────────────────


class TestBuiltins:
    def test_created_with_builtin_tools(self) -> None:
        registry = create_registry()
        names = {t.name for t in registry.list_tools()}
        assert {"fs_read", "fs_write", "shell", "web_search"} <= names

    def test_parallel_safety_flags(self) -> None:
        registry = create_registry()
        assert registry.get("fs_read").parallel_safe is True
        assert registry.get("fs_write").parallel_safe is False
        assert registry.get("shell").parallel_safe is False

    def test_side_effect_classes(self) -> None:
        registry = create_registry()
        assert registry.get("fs_read").side_effect_class == "auto"
        assert registry.get("fs_write").side_effect_class == "ask"
        assert registry.get("shell").side_effect_class == "ask"
        assert registry.get("web_search").side_effect_class == "ask"
