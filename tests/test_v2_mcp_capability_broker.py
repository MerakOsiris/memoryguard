from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import memoryguard.mcp_server as mcp_server
from memoryguard.cutover_v2 import surfaces


@pytest.fixture(autouse=True)
def _isolated_v2_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))


@dataclass(frozen=True)
class _TrustedContext:
    agent_instance_id: str = "bound-agent"
    share_group_id: str = "bound-group"
    provider: str = "codex"
    project_ref: str = "bound-project"
    runtime_role: str = "root"


class _StatefulFacade:
    """Small stateful transport seam; records real broker dispatch decisions."""

    def __init__(self, state: str = "V2_ACTIVE", *, fail_dispatch: bool = False):
        self.state = state
        self.fail_dispatch = fail_dispatch
        self.mcp_calls: list[tuple[str, dict, object]] = []
        self.gui_calls: list[tuple[str, dict, object]] = []
        self.shutdown_calls: list[float] = []

    def state_snapshot(self) -> dict[str, object]:
        return {"state": self.state, "generation": 1}

    @staticmethod
    def _context_values(context: object) -> dict[str, str]:
        return {
            key: str(getattr(context, key, "") or "")
            for key in ("agent_instance_id", "share_group_id", "provider", "project_ref", "runtime_role")
        }

    def dispatch_mcp(self, name, args, *, context=None, snapshot=None):
        self.mcp_calls.append((name, dict(args), context))
        if self.fail_dispatch:
            raise RuntimeError("fixture dispatch failure")
        return {
            "ok": True,
            "path": "v2",
            "data": {
                "surface": "mcp",
                "name": name,
                "arguments": dict(args),
                "context": self._context_values(context),
            },
        }

    def dispatch_gui(self, name, args, *, mutation=None, context=None, snapshot=None):
        self.gui_calls.append((name, dict(args), context))
        if self.fail_dispatch:
            raise RuntimeError("fixture dispatch failure")
        return {
            "ok": True,
            "path": "v2",
            "data": {
                "surface": "gui",
                "name": name,
                "arguments": dict(args),
                "context": self._context_values(context),
            },
        }

    def shutdown(self, *, timeout: float = 5.0):
        self.shutdown_calls.append(float(timeout))
        return {"ok": True, "owned_workers_stopped": True}


def _install_facade(monkeypatch: pytest.MonkeyPatch, facade: _StatefulFacade) -> None:
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(
        mcp_server,
        "_trusted_context_for_v2",
        lambda args, workspace: (_TrustedContext(), None),
    )


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _call(
    monkeypatch: pytest.MonkeyPatch,
    facade: _StatefulFacade,
    name: str,
    arguments: dict | None = None,
) -> dict:
    _install_facade(monkeypatch, facade)
    return mcp_server.execute_tool(name, dict(arguments or {}))


def test_tools_list_exposes_only_default_tools_and_broker_is_destructive():
    response = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = response["result"]["tools"]
    assert len(tools) == 11
    names = {tool["name"] for tool in tools}
    assert {"memoryguard_capabilities", "memoryguard_invoke"} <= names
    invoke = next(tool for tool in tools if tool["name"] == "memoryguard_invoke")
    assert invoke["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }


def test_capability_catalog_is_short_by_default_and_supports_schema_exact_query(monkeypatch):
    facade = _StatefulFacade()
    result = _call(monkeypatch, facade, "memoryguard_capabilities")
    catalog = _payload(result)
    assert result.get("isError") is not True
    assert catalog["offset"] == 0
    assert catalog["limit"] == 20
    assert len(catalog["items"]) <= 20
    assert all("input_schema" not in item for item in catalog["items"])
    assert not facade.mcp_calls and not facade.gui_calls

    exact = _payload(_call(
        monkeypatch,
        facade,
        "memoryguard_capabilities",
        {"operation": "memoryguard_task_cancel", "include_schema": True},
    ))
    assert exact["total"] == 1
    item = exact["items"][0]
    assert item["name"] == "memoryguard_task_cancel"
    assert item["input_schema"]["required"] == ["run_id", "confirmed", "idempotency_key"]

    conflict = _payload(_call(
        monkeypatch, facade, "memoryguard_capabilities", {"query": "冲突治理"},
    ))
    conflict_names = {item["name"] for item in conflict["items"]}
    assert {"get_conflicts", "resolve_conflict", "close_stale_conflict"} <= conflict_names

    cancellation = _payload(_call(
        monkeypatch, facade, "memoryguard_capabilities", {"query": "任务取消"},
    ))
    assert "memoryguard_task_cancel" in {item["name"] for item in cancellation["items"]}


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": 0},
        {"offset": True},
        {"limit": 1.0},
    ],
)
def test_capability_catalog_rejects_zero_bool_and_float_pagination(monkeypatch, arguments):
    result = _call(monkeypatch, _StatefulFacade(), "memoryguard_capabilities", arguments)
    payload = _payload(result)
    assert result["isError"] is True
    assert payload["code"] == "capability_pagination_invalid"


def test_gui_registry_has_only_reviewed_headless_broker_entries():
    assert len(surfaces.GUI_OPERATION_SPECS) == 172
    assert len(surfaces.MCP_BROKER_GUI_METHOD_NAMES) == 163
    assert len(surfaces.MCP_BROKER_GUI_EXCLUDED) == 9
    excluded_reasons = {
        name: surfaces.GUI_OPERATION_SPECS[name].mcp_broker
        for name in surfaces.MCP_BROKER_GUI_EXCLUDED
    }
    assert excluded_reasons == {
        "build_codegraph": "desktop_admin_only",
        "set_codegraph_automation": "desktop_admin_only",
        "call_readonly": "bridge_protocol_only",
        "choose_publish_target_path": "desktop_only",
        "list_codegraph_projects": "desktop_admin_only",
        "open_agent_folder": "desktop_only",
        "pick_path": "desktop_only",
        "request_mutation": "bridge_protocol_only",
        "submit_request": "bridge_protocol_only",
    }

    for name in surfaces.MCP_BROKER_GUI_METHOD_NAMES:
        payload = {"operation": name, "arguments": {}}
        if name in surfaces.GUI_MUTATION_NAMES:
            payload.update({"confirmed": True, "idempotency_key": f"test-{name}"})
        surface, target, args = surfaces.resolve_mcp_broker_invocation(
            payload,
            gui_names=surfaces.MCP_BROKER_GUI_METHOD_NAMES,
            gui_mutation_names=surfaces.GUI_MUTATION_NAMES,
        )
        assert (surface, target) == ("gui", name)
        assert (
            args.get("confirmed") is True
            if name in surfaces.GUI_MUTATION_NAMES
            else "confirmed" not in args
        )


def test_new_unreviewed_gui_spec_is_not_broker_reachable(monkeypatch, tmp_path):
    temporary = surfaces.GuiOperationSpec(
        public_name="temporary_unreviewed",
        canonical_name="temporary_unreviewed",
        domain="temporary",
        kind="read",
        execution="sync",
        native_handler="temporary_unreviewed",
    )
    monkeypatch.setitem(surfaces.GUI_OPERATION_SPECS, temporary.public_name, temporary)
    facade = _StatefulFacade()
    result = _call(
        monkeypatch,
        facade,
        "memoryguard_invoke",
        {"operation": temporary.public_name, "arguments": {}},
    )
    assert _payload(result)["code"] == "broker_operation_unknown"
    assert not facade.gui_calls and not facade.mcp_calls


def test_invoke_resolves_mcp_and_gui_targets_with_trusted_context(monkeypatch):
    facade = _StatefulFacade()
    mcp_result = _call(
        monkeypatch,
        facade,
        "memoryguard_invoke",
        {
            "operation": "memoryguard_memory_status",
            "arguments": {"agent_instance_id": "attacker", "share_group_id": "attacker-group"},
        },
    )
    assert mcp_result.get("isError") is not True
    assert facade.mcp_calls[0][0] == "memoryguard_memory_status"
    assert facade.mcp_calls[0][1] == {}
    assert facade.mcp_calls[0][2] == _TrustedContext()
    assert _payload(mcp_result)["data"]["context"]["agent_instance_id"] == "bound-agent"

    gui_result = _call(
        monkeypatch,
        facade,
        "memoryguard_invoke",
        {"operation": "get_memory_status", "arguments": {}},
    )
    assert gui_result.get("isError") is not True
    assert facade.gui_calls[-1][0] == "get_memory_status"
    assert facade.gui_calls[-1][2] == _TrustedContext()
    assert _payload(gui_result)["data"]["surface"] == "gui"


@pytest.mark.parametrize(
    ("operation", "arguments", "code"),
    [
        ("missing_operation", {}, "broker_operation_unknown"),
        ("memoryguard_invoke", {}, "broker_recursion_forbidden"),
    ],
)
def test_invoke_rejects_unknown_and_recursive_targets(monkeypatch, operation, arguments, code):
    facade = _StatefulFacade()
    result = _call(
        monkeypatch,
        facade,
        "memoryguard_invoke",
        {"operation": operation, "arguments": arguments},
    )
    assert _payload(result)["code"] == code
    assert not facade.mcp_calls and not facade.gui_calls


def test_invoke_rejects_forged_outer_identity(monkeypatch):
    facade = _StatefulFacade()

    def reject_forged(args, workspace):
        if args.get("agent_instance_id") == "attacker":
            return None, "agent_binding_mismatch", None
        return "bound-group", None, None

    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    # Keep the production trusted-context builder in the path; only the
    # connection-owned resolver is a minimal stateful fixture here.
    monkeypatch.setattr(mcp_server, "_resolve_access", reject_forged)
    result = mcp_server.execute_tool(
        "memoryguard_invoke",
        {
            "operation": "get_memory_status",
            "arguments": {},
            "agent_instance_id": "attacker",
        },
    )
    assert _payload(result)["code"] == "agent_binding_mismatch"
    assert not facade.gui_calls


def test_read_invoke_allowed_in_ready_but_mutation_requires_proof_and_active_state(monkeypatch):
    ready = _StatefulFacade("V2_READY")
    read = _call(
        monkeypatch,
        ready,
        "memoryguard_invoke",
        {"operation": "get_memory_status", "arguments": {}},
    )
    assert read.get("isError") is not True
    assert [name for name, _args, _context in ready.gui_calls] == ["get_memory_status"]

    missing_confirmation = _call(
        monkeypatch,
        _StatefulFacade(),
        "memoryguard_invoke",
        {"operation": "delete_memory", "arguments": {}, "idempotency_key": "delete-1"},
    )
    assert _payload(missing_confirmation)["code"] == "confirmation_required"

    missing_key = _call(
        monkeypatch,
        _StatefulFacade(),
        "memoryguard_invoke",
        {"operation": "delete_memory", "arguments": {}, "confirmed": True},
    )
    assert _payload(missing_key)["code"] == "idempotency_key_required"

    ready_write_facade = _StatefulFacade("V2_READY")
    ready_write = _call(
        monkeypatch,
        ready_write_facade,
        "memoryguard_invoke",
        {
            "operation": "delete_memory",
            "arguments": {},
            "confirmed": True,
            "idempotency_key": "delete-ready",
        },
    )
    assert _payload(ready_write)["code"] == "v2_not_active"
    assert not ready_write_facade.gui_calls


def test_gui_mutation_conflicting_runtime_lease_blocks_dispatch(monkeypatch, tmp_path):
    facade = _StatefulFacade()
    _install_facade(monkeypatch, facade)
    monkeypatch.setattr(
        "memoryguard.runtime_lease.check_runtime_lease",
        lambda workspace, pid: {"granted": False, "conflicting": [{"pid": 4242}]},
    )
    result = mcp_server.execute_tool(
        "memoryguard_invoke",
        {
            "operation": "delete_memory",
            "arguments": {},
            "confirmed": True,
            "idempotency_key": "delete-lease",
        },
    )
    payload = _payload(result)
    assert payload["error"] == "runtime_split_brain"
    assert payload["restart_required"] is True
    assert not facade.gui_calls


def test_native_mcp_broker_dispatches_real_mcp_and_gui_targets(tmp_path):
    """The native broker must cross both dispatch boundaries with real state."""
    from memoryguard.access_context import AccessContext
    from memoryguard.memory.store import MemoryAtomStore
    from memoryguard.runtime_v2.group_native import GroupControlService
    from memoryguard.runtime_v2.native_ports import (
        NativeV2RuntimePort,
        bind_native_transport_context,
    )

    workspace = tmp_path.resolve()
    MemoryAtomStore(workspace)
    GroupControlService(workspace, write=True).bind_agent(
        "agent-a", "group-a", idempotency_key="broker-native-binding",
    )
    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="broker-native-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id="group-a",
        project_ref=str(workspace),
        provider="codex",
        runtime_role="mcp",
        entrypoint="mcp",
    )
    assert context.bound_context.entrypoint == "mcp"
    assert context.bound_context.agent_instance_id == "agent-a"

    port = NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 7},
    )
    capabilities = port.dispatch_mcp(
        "memoryguard_invoke",
        {
            "operation": "memoryguard_capabilities",
            "arguments": {
                "operation": "memoryguard_task_list",
                "include_schema": True,
            },
        },
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert capabilities["ok"] is True, capabilities
    catalog = capabilities["data"]
    assert catalog["total"] == 1
    assert catalog["items"][0]["name"] == "memoryguard_task_list"
    assert catalog["items"][0]["input_schema"]["type"] == "object"

    gui_status = port.dispatch_mcp(
        "memoryguard_invoke",
        {"operation": "get_memory_status", "arguments": {}},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert gui_status["ok"] is True, gui_status
    status = gui_status["data"]
    assert status["available"] is True
    assert status["total_records"] == 0
    assert status["scope"]["share_group_id"] == "group-a"
    assert status["scope"]["agent_instance_id"] == "agent-a"


@pytest.mark.parametrize("fail_dispatch", [False, True])
def test_stdio_eof_and_dispatch_exception_shutdown_only_owned_facade(
    monkeypatch, tmp_path, fail_dispatch,
):
    owned = _StatefulFacade(fail_dispatch=fail_dispatch)
    external = _StatefulFacade()
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: owned)
    monkeypatch.setattr(mcp_server, "_stdio_owned_facades", [external])
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "memoryguard_audit", "arguments": {}},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    assert mcp_server.serve_stdio() == 0
    assert owned.shutdown_calls and max(owned.shutdown_calls) <= 5.0
    assert external.shutdown_calls == []
