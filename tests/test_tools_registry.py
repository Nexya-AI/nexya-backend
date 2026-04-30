"""
Tests du registry des tools LLM + format OpenAI.

Couvre :
- ToolDefinition.to_openai_tool() produit le format natif.
- ToolRegistry.register/get/all/names/build_openai_tools.
- clear + reset_tool_registry_for_tests.
- register_planner_tools() expose bien 4 tools.
"""

from __future__ import annotations

from app.ai.tools import (
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    reset_tool_registry_for_tests,
)
from app.ai.tools.base import get_tool_registry
from app.ai.tools.planner_tools import (
    build_planner_tools,
    register_planner_tools,
)


def _make_tool(name: str = "foo") -> ToolDefinition:
    async def _handler(user, db, args):
        return ToolResult(success=True, data={"ok": name})

    return ToolDefinition(
        name=name,
        description=f"Tool {name}",
        parameters_schema={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
        handler=_handler,
    )


def test_tool_to_openai_format_has_function_wrapper():
    tool = _make_tool("my_tool")
    payload = tool.to_openai_tool()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "my_tool"
    assert payload["function"]["description"] == "Tool my_tool"
    assert payload["function"]["parameters"]["type"] == "object"
    assert "x" in payload["function"]["parameters"]["properties"]


def test_registry_register_and_get():
    reg = ToolRegistry()
    tool = _make_tool("alpha")
    reg.register(tool)
    assert reg.get("alpha") is tool
    assert reg.get("absent") is None
    assert reg.names() == ["alpha"]
    assert reg.all() == [tool]


def test_registry_build_openai_tools_payload():
    reg = ToolRegistry()
    reg.register(_make_tool("alpha"))
    reg.register(_make_tool("beta"))
    tools = reg.build_openai_tools()
    assert len(tools) == 2
    names = [t["function"]["name"] for t in tools]
    assert names == ["alpha", "beta"]


def test_registry_override_warns_but_replaces():
    reg = ToolRegistry()
    reg.register(_make_tool("x"))
    new_tool = _make_tool("x")
    reg.register(new_tool)
    assert reg.get("x") is new_tool


def test_registry_clear():
    reg = ToolRegistry()
    reg.register(_make_tool("x"))
    reg.clear()
    assert reg.all() == []


def test_reset_for_tests_empties_singleton():
    reset_tool_registry_for_tests()
    reg = get_tool_registry()
    assert reg.all() == []


def test_register_planner_tools_registers_four():
    reset_tool_registry_for_tests()
    reg = get_tool_registry()
    register_planner_tools(reg)
    names = set(reg.names())
    assert names == {"create_task", "list_tasks", "update_task", "pause_task"}


def test_build_planner_tools_have_valid_schemas():
    tools = build_planner_tools()
    assert len(tools) == 4
    for tool in tools:
        assert tool.description  # non-vide
        assert tool.parameters_schema["type"] == "object"
        assert "properties" in tool.parameters_schema
