from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore
from memoryguard.codegraph_v2.graphify_adapter import EXPORT_FORMAT, GraphifyExportAdapter
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeContextError, NativeV2RuntimePort, bind_native_transport_context
from memoryguard.runtime_v2.source_control import SourceControlService
from memoryguard.rule_scope import canonical_project_ref


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 1}


def _context(root: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="native-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root),
        share_group_id="group-bound",
    )


def _projectless_context(root: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="projectless-source-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root),
        share_group_id="group-bound",
        provider="codex",
        runtime_role="root",
    )


def _export() -> dict:
    return {
        "format": EXPORT_FORMAT,
        "complete": True,
        "graphify_version": "phase9-test",
        "source_digest": "digest-v1",
        "files": [
            {"id": "gui", "path": "src/gui.py", "content_hash": "gui-hash", "language": "python", "source_role": "production", "provenance": "production"},
            {"id": "native", "path": "src/native.py", "content_hash": "native-hash", "language": "python", "source_role": "production", "provenance": "production"},
            {"id": "fixture", "path": "tests/fake.py", "content_hash": "fixture-hash", "language": "python", "source_role": "fixture", "provenance": "fixture"},
        ],
        "nodes": [
            {"id": "control", "file": "gui", "name": "添加知识", "kind": "control", "source_location": "L10", "provenance": "production", "semantic_kind": "gui_control", "source_map": {"host_symbol": "PAGE_HTML", "region_id": "r1"}},
            {"id": "handler", "file": "gui", "name": "addBook", "kind": "function", "source_location": "L20", "provenance": "production", "semantic_kind": "handler"},
            {"id": "api", "file": "gui", "name": "knowledge_add", "kind": "api", "source_location": "L30", "provenance": "production", "semantic_kind": "api_method"},
            {"id": "native", "file": "native", "name": "gui_knowledge_command", "kind": "native", "source_location": "L40", "provenance": "production", "semantic_kind": "native_handler"},
            {"id": "fixture-node", "file": "fixture", "name": "fake_handler", "kind": "function", "source_location": "L1", "provenance": "fixture", "semantic_kind": "handler"},
        ],
        "edges": [
            {"source": "control", "target": "handler", "relation": "references", "context": "control_handler", "source_location": "L10", "provenance": "production"},
            {"source": "handler", "target": "api", "relation": "references", "context": "handler_api", "source_location": "L20", "provenance": "production"},
            {"source": "api", "target": "native", "relation": "references", "context": "api_surface", "source_location": "L30", "provenance": "production"},
            {"source": "fixture-node", "target": "native", "relation": "references", "context": "handler_api", "source_location": "L1", "provenance": "fixture"},
        ],
    }


def test_native_codegraph_canonical_operations_and_production_filter(tmp_path: Path) -> None:
    # The native mutation path never initializes a missing CodeGraph implicitly.
    CodeGraphStore(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent("agent-bound", "group-bound", idempotency_key="test-codegraph-binding")
    context = _context(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())

    denied = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": False},
        context=context,
        generation=1,
    )
    assert denied["ok"] is False
    assert denied["code"] == "confirmation_required"

    updated = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": True, "full_snapshot": True},
        context=context,
        generation=1,
    )
    assert updated["ok"] is True
    assert updated["data"]["status"] == "UPDATED"
    assert updated["data"]["counts"]["source_files"] == 3

    add_book = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "addBook", "provenance": "production"},
        context=context,
        generation=1,
    )
    assert add_book["ok"] is True
    assert [item["name"] for item in add_book["data"]["symbols"]] == ["addBook"]

    control = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "添加知识", "provenance": "production"},
        context=context,
        generation=1,
    )["data"]["symbols"][0]
    native = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "gui_knowledge_command", "provenance": "production"},
        context=context,
        generation=1,
    )["data"]["symbols"][0]

    path = port.dispatch_mcp(
        "memoryguard_codegraph_path",
        {"start_id": control["symbol_id"], "end_id": native["symbol_id"], "provenance": "production", "max_depth": 8},
        context=context,
        generation=1,
    )
    assert path["ok"] is True
    assert path["data"]["found"] is True
    assert path["data"]["hops"] == 3

    explain = port.dispatch_mcp(
        "memoryguard_codegraph_explain",
        {"symbol_id": control["symbol_id"], "provenance": "production"},
        context=context,
        generation=1,
    )
    assert explain["ok"] is True
    assert explain["data"]["symbol"]["source_map"]["host_symbol"] == "PAGE_HTML"

    affected = port.dispatch_mcp(
        "memoryguard_codegraph_affected",
        {"start_id": native["symbol_id"], "provenance": "production", "depth": 8},
        context=context,
        generation=1,
    )
    assert affected["ok"] is True
    assert control["symbol_id"] in affected["data"]["result_ids"]
    fixture = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "fake_handler", "provenance": "fixture"},
        context=context,
        generation=1,
    )["data"]["symbols"][0]
    assert fixture["symbol_id"] not in affected["data"]["result_ids"]

    graph = port.dispatch_mcp(
        "memoryguard_codegraph_graph",
        {"provenance": "production", "limit": 100},
        context=context,
        generation=1,
    )
    assert graph["ok"] is True
    assert all(node.get("provenance") == "production" for node in graph["data"]["nodes"])
    assert all(edge.get("provenance") == "production" for edge in graph["data"]["edges"])

    try:
        port._codegraph_status({}, context)
    except Exception as exc:
        pytest.fail(repr(exc))
    status = port.dispatch_mcp("memoryguard_codegraph_status", {}, context=context, generation=1)
    assert status["ok"] is True, status.get("code")
    assert "production_complete" not in status["data"]
    assert "graphify" in status["data"]
    incremental = status["data"]["incremental"]
    assert incremental["supported"] is True
    assert incremental["built_scope"] is True
    assert incremental["active_binding"] is True
    assert incremental["enabled"] is True
    assert incremental["queue_depth"] == 0
    if status["data"]["update_ready"] is False:
        assert status["data"]["capability_error"]


def test_projectless_source_selector_closes_build_query_status_update_graph_loop(tmp_path: Path) -> None:
    source_root = tmp_path / "outside-project"
    source_root.mkdir()
    (source_root / "entry.py").write_text(
        "def built_from_source():\n    return 1\n",
        encoding="utf-8",
    )
    CodeGraphStore(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent(
        "agent-bound", "group-bound", idempotency_key="projectless-binding",
    )
    source = SourceControlService(tmp_path).add(
        str(source_root),
        "selected_directory",
        {"admin": True, "agent_instance_id": "agent-bound"},
    )
    context = _projectless_context(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())

    built = port.dispatch_mcp(
        "memoryguard_codegraph_build_bound",
        {"source_id": source["source_id"], "confirmed": True, "idempotency_key": "projectless-build"},
        context=context,
        generation=1,
    )
    assert built["ok"] is True, built
    build_data = built.get("data", built)
    job_id = build_data["job_id"]
    task_scope = port._gui_task_scope(context)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        task = port._task_service().status(job_id, task_scope)
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    else:
        pytest.fail("projectless CodeGraph build did not finish")
    assert task["status"] == "succeeded", task

    selected = {"codegraph_source_id": source["source_id"]}
    queried = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {**selected, "query": "built_from_source", "provenance": "production"},
        context=context,
        generation=1,
    )
    assert queried["ok"] is True, queried
    assert [item["name"] for item in queried["data"]["symbols"]] == ["built_from_source"]
    status = port.dispatch_mcp(
        "memoryguard_codegraph_status", selected, context=context, generation=1,
    )
    assert status["ok"] is True, status
    assert status["data"]["counts"]["source_files"] == 1

    updated = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {**selected, "confirmed": True, "export": _export(), "full_snapshot": True},
        context=context,
        generation=1,
    )
    assert updated["ok"] is True, updated
    assert updated["data"]["scope_digest"] == queried["data"]["scope_digest"]
    graph = port.dispatch_mcp(
        "memoryguard_codegraph_graph",
        {**selected, "provenance": "production", "limit": 100},
        context=context,
        generation=1,
    )
    assert graph["ok"] is True, graph
    assert any(node.get("label") == "addBook" for node in graph["data"]["nodes"])


def test_native_codegraph_status_keeps_incremental_pending_without_graph_or_binding(tmp_path: Path) -> None:
    CodeGraphStore(tmp_path)
    status = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_codegraph_status",
        {},
        context=_context(tmp_path),
        generation=1,
    )

    assert status["ok"] is True, status
    incremental = status["data"]["incremental"]
    assert incremental["supported"] is True
    assert incremental["built_scope"] is False
    assert incremental["active_binding"] is False
    assert incremental["enabled"] is False
    assert incremental["queue_depth"] == 0


def test_native_codegraph_status_reads_legacy_graph_without_refresh_schema(tmp_path: Path) -> None:
    """Status remains read-only when an existing graph predates refresh tables."""
    store = CodeGraphStore(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent(
        "agent-bound", "group-bound", idempotency_key="legacy-codegraph-binding",
    )
    context = _context(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    updated = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": True, "full_snapshot": True},
        context=context,
        generation=1,
    )
    assert updated["ok"] is True

    # Simulate a graph built before incremental refresh support.  Keep the
    # immutable graph/schema tables and remove only the additive refresh set.
    with sqlite3.connect(store.db_path) as conn:
        conn.executescript(
            "DROP TABLE affected_receipts;"
            "DROP TABLE source_fingerprints;"
            "DROP TABLE refresh_queue;"
        )
    before = (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns)

    status = port.dispatch_mcp(
        "memoryguard_codegraph_status", {}, context=context, generation=1,
    )

    assert status["ok"] is True, status
    incremental = status["data"]["incremental"]
    assert incremental["supported"] is True
    assert incremental["built_scope"] is True
    assert incremental["active_binding"] is True
    assert incremental["enabled"] is True
    assert incremental["queue_depth"] == 0
    assert (store.db_path.read_bytes(), store.db_path.stat().st_mtime_ns) == before


def test_first_refresh_write_restores_missing_refresh_schema(tmp_path: Path) -> None:
    """The first trusted refresh write still creates the additive tables."""
    store = CodeGraphStore(tmp_path)
    scope = CodeGraphScope.from_value({
        "workspace_id": str(tmp_path.resolve()),
        "share_group_id": "group-bound",
        "agent_instance_id": "",
        "project_ref": canonical_project_ref(str(tmp_path.resolve())),
        "provider": "graphify",
        "runtime_role": "",
        "trusted_context": True,
    })
    GraphifyExportAdapter(store).project(_export(), scope=scope, full_snapshot=True)
    with sqlite3.connect(store.db_path) as conn:
        conn.executescript(
            "DROP TABLE affected_receipts;"
            "DROP TABLE source_fingerprints;"
            "DROP TABLE refresh_queue;"
        )

    legacy_store = CodeGraphStore(tmp_path, initialize=False)
    assert legacy_store.enqueue_refresh_paths(["src/gui.py"], scope=scope) == ("src/gui.py",)

    with sqlite3.connect(store.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
    assert {"affected_receipts", "source_fingerprints", "refresh_queue"} <= tables


def test_native_codegraph_update_rejects_raw_graphify_graph(tmp_path: Path) -> None:
    CodeGraphStore(tmp_path)
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": {"nodes": [], "links": []}, "confirmed": True},
        context=_context(tmp_path),
        generation=1,
    )
    assert result["ok"] is False
    assert result["code"] == "graphify_export_format_unsupported"


def test_gui_codegraph_graph_rejects_plain_forged_context(tmp_path: Path) -> None:
    CodeGraphStore(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    forged = {
        "workspace_id": str(tmp_path),
        "share_group_id": "group-bound",
        "agent_instance_id": "agent-bound",
        "project_ref": str(tmp_path),
        "provider": "codex",
        "runtime_role": "root",
        "trusted_context": True,
    }

    with pytest.raises(NativeContextError, match="trusted_context_capability_required"):
        port._codegraph_scope(forged)

    result = port.dispatch_gui(
        "get_codegraph_graph",
        [{"limit": 10}],
        context=forged,
        generation=1,
        state="V2_ACTIVE",
    )

    assert result["ok"] is False
    assert result["code"] == "trusted_context_capability_required"


def test_native_codegraph_update_keeps_safe_exception_diagnostic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    CodeGraphStore(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("internal detail must stay private")

    monkeypatch.setattr(GraphifyExportAdapter, "project", fail)
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": True},
        context=_context(tmp_path),
        generation=1,
    )
    assert result["ok"] is False
    assert result["code"] == "codegraph_update_failed_runtimeerror"
    assert "internal detail" not in str(result)
