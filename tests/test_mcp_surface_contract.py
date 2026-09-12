"""Default MCP discovery stays small while legacy direct calls remain routed."""

from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from memoryguard import mcp_server
from memoryguard.cutover_v2.surfaces import GUI_METHOD_NAMES, MCP_MUTATION_NAMES, MCP_TOOL_NAMES
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort


CORE_TOOL_NAMES = frozenset({
    "memoryguard_context_bootstrap",
    "memoryguard_memory_search",
    "memoryguard_memory_read",
    "memoryguard_memory_write",
    "memoryguard_memory_update",
    "memoryguard_memory_delete",
    "memoryguard_memory_status",
    "memoryguard_audit",
    "memoryguard_explain",
    "memoryguard_capabilities",
    "memoryguard_invoke",
})


CORE_TOOL_ANNOTATIONS = {
    "memoryguard_context_bootstrap": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "memoryguard_memory_search": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_memory_read": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_memory_write": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "memoryguard_memory_update": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "memoryguard_memory_delete": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_memory_status": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_audit": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_explain": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_capabilities": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "memoryguard_invoke": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
}


CORE_DESCRIPTION_BOUNDARIES = {
    "memoryguard_context_bootstrap": ("use when", "do not use"),
    "memoryguard_memory_search": ("use when", "do not use"),
    "memoryguard_memory_read": ("use when", "do not use"),
    "memoryguard_memory_write": ("use when", "do not use"),
    "memoryguard_memory_update": ("use when", "do not use"),
    "memoryguard_memory_delete": ("use when", "do not use"),
    "memoryguard_memory_status": ("use when", "do not use"),
    "memoryguard_audit": ("use when", "do not use"),
    "memoryguard_explain": ("use when", "do not use"),
    "memoryguard_capabilities": ("discover", "requires no"),
    "memoryguard_invoke": ("invoke", "never accepts"),
}


def _payload(result: dict[str, object]) -> dict[str, object]:
    return json.loads(result["content"][0]["text"])  # type: ignore[index]


def test_default_tools_list_is_exact_core_with_real_schemas():
    listed = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })["result"]["tools"]
    assert {item["name"] for item in listed} == CORE_TOOL_NAMES
    assert len(listed) == len(CORE_TOOL_NAMES)
    assert all(item["description"].strip() for item in listed)
    assert all(item["inputSchema"].get("type") == "object" for item in listed)
    assert all("properties" in item["inputSchema"] for item in listed)


def test_default_tools_describe_real_boundaries_annotations_and_parameters():
    listed = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })["result"]["tools"]
    by_name = {item["name"]: item for item in listed}

    assert set(by_name) == set(CORE_TOOL_ANNOTATIONS)
    for name, expected in CORE_TOOL_ANNOTATIONS.items():
        tool = by_name[name]
        assert tool.get("annotations") == expected
        assert "outputSchema" not in tool  # result remains text CallToolResult, not fake structuredContent
        description = tool["description"].casefold()
        assert all(marker in description for marker in CORE_DESCRIPTION_BOUNDARIES[name])
        properties = tool["inputSchema"]["properties"]
        assert all(
            isinstance(spec.get("description"), str) and spec["description"].strip()
            for spec in properties.values()
        )

    assert by_name["memoryguard_context_bootstrap"]["inputSchema"]["properties"]["max_items"]["description"]
    assert by_name["memoryguard_context_bootstrap"]["inputSchema"]["properties"]["max_chars"]["description"]
    assert by_name["memoryguard_memory_write"]["inputSchema"]["properties"]["kind"]["enum"] == [
        "preference", "fact", "project", "procedure", "episode", "correction",
    ]
    assert by_name["memoryguard_memory_update"]["inputSchema"]["properties"]["kind"]["enum"] == [
        "preference", "fact", "project", "procedure", "episode", "correction",
    ]
    assert "status" not in by_name["memoryguard_memory_update"]["inputSchema"]["properties"]
    delete_schema = by_name["memoryguard_memory_delete"]["inputSchema"]
    assert "idempotency_key" in delete_schema["required"]


def test_audit_read_only_contract_matches_registry_and_mcp_annotation():
    listed = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })["result"]["tools"]
    audit = next(item for item in listed if item["name"] == "memoryguard_audit")
    assert "memoryguard_audit" not in MCP_MUTATION_NAMES
    assert NativeV2RuntimePort._MCP_HANDLERS["memoryguard_audit"][2] is False
    assert audit["annotations"]["readOnlyHint"] is True


def test_callable_catalog_matches_v2_registry_and_keeps_codegraph_internal(tmp_path):
    assert {item["name"] for item in mcp_server.CALLABLE_TOOLS} == set(MCP_TOOL_NAMES)
    assert "memoryguard_codegraph_graph" in mcp_server.CALLABLE_TOOL_NAMES
    assert "memoryguard_codegraph_graph" not in {item["name"] for item in mcp_server.TOOLS}
    assert set(mcp_server.TOOL_DEFINITIONS) == set(MCP_TOOL_NAMES)
    handlers = NativeV2RuntimePort._MCP_HANDLERS
    assert set(handlers) - set(MCP_TOOL_NAMES) == {
        "memoryguard_asset_status", "memoryguard_skill_status",
    }
    assert set(NativeV2RuntimePort(tmp_path).surface_registry["gui"]) == set(GUI_METHOD_NAMES)


def test_legacy_hidden_tool_still_dispatches_with_machine_readable_deprecation(
    monkeypatch, tmp_path,
):
    class _Facade:
        def state_snapshot(self):
            return {"state": "V2_ACTIVE", "generation": 1}

        def dispatch_mcp(self, name, args, *, context, snapshot=None):
            return {"ok": True, "name": name, "path": "v2"}

    @dataclass
    class _Context:
        agent_instance_id: str = "agent"
        share_group_id: str = "group"
        provider: str = "codex"
        project_ref: str = "project"
        runtime_role: str = "root"

    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: _Facade())
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("group", None, None))
    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))

    result = mcp_server.execute_tool("memoryguard_history_search", {"query": "release"})
    assert result.get("isError") is not True
    payload = _payload(result)
    assert payload["deprecated"] is True
    assert payload["deprecation"]["code"] == "mcp_tool_not_listed"
    assert payload["name"] == "memoryguard_history_search"


@pytest.mark.parametrize("outcome", ("business_error", "exception"))
def test_legacy_hidden_tool_keeps_deprecation_when_dispatch_fails(
    monkeypatch, tmp_path, outcome,
):
    class _Facade:
        def state_snapshot(self):
            return {"state": "V2_ACTIVE", "generation": 1}

        def dispatch_mcp(self, name, args, *, context, snapshot=None):
            if outcome == "exception":
                raise RuntimeError("test dispatch failure")
            return {"ok": False, "error": "v2_policy_denied", "code": "v2_policy_denied"}

    @dataclass
    class _Context:
        agent_instance_id: str = "agent"
        share_group_id: str = "group"
        provider: str = "codex"
        project_ref: str = "project"
        runtime_role: str = "root"

    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: _Facade())
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("group", None, None))
    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))

    result = mcp_server.execute_tool("memoryguard_history_search", {"query": "release"})
    assert result["isError"] is True
    assert result["deprecated"] is True
    assert result["deprecation"]["code"] == "mcp_tool_not_listed"
    if outcome == "business_error":
        assert _payload(result)["deprecation"]["code"] == "mcp_tool_not_listed"
