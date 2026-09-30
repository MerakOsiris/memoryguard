from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from memoryguard.codegraph_v2 import automation as auto
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.codegraph_v2.refresh import queue_host_file_refresh
from memoryguard.host_hooks import _render_context
from test_codegraph_incremental_refresh import _bind_refresh_agent


def test_policy_persists_project_override_group_isolation_and_corruption(tmp_path):
    project = str(tmp_path / "repo")
    assert auto.policy(tmp_path, "one", project)["enabled"]
    auto.set_policy(tmp_path, "one", False)
    assert not auto.policy(tmp_path, "one", project)["enabled"]
    assert auto.policy(tmp_path, "two", project)["enabled"]
    auto.set_policy(tmp_path, "one", True, project)
    assert auto.policy(tmp_path, "one", project)["project_override"] is True
    assert not auto.policy(tmp_path, "one", str(tmp_path / "other"))["enabled"]
    auto._path(tmp_path).write_text('{"version":1,"groups":null}', encoding="utf-8")
    assert auto.policy(tmp_path, "one", project)["enabled"] is False
    import pytest
    with pytest.raises(ValueError):
        auto.set_policy(tmp_path, "one", True)
    assert auto._path(tmp_path).read_text(encoding="utf-8") == '{"version":1,"groups":null}'


def test_detached_first_build_survives_hook_exit_then_toggle_controls_refresh(tmp_path):
    _bind_refresh_agent(tmp_path)
    project = tmp_path / "repo"
    project.mkdir()
    source = project / "main.py"
    source.write_text("def first():\n    return 1\n", encoding="utf-8")
    # The parent process exits immediately, as a real Hook does. Do not mock
    # Popen: an in-process daemon would fail this test.
    program = "from memoryguard.codegraph_v2.automation import agent_notice; import json,sys; print(json.dumps(agent_notice(sys.argv[1],dict(share_group_id='group-a',agent_instance_id='codex-agent',project_ref=sys.argv[2]))))"
    result = subprocess.run([sys.executable, "-c", program, str(tmp_path), str(project)],
                            capture_output=True, text=True, check=True, timeout=20)
    notice = json.loads(result.stdout)
    assert notice["build_status"] in {"queued", "building", "ready"}, notice
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = auto.status(tmp_path, "group-a", str(project))
        if status["build_status"] not in {"queued", "building"}:
            break
        time.sleep(.1)
    assert status["build_status"] == "ready", status
    scope = auto.scope_for(tmp_path, "group-a", str(project))
    store = CodeGraphStore(tmp_path, initialize=False)
    assert store.query_symbols("first", scope=scope)
    assert not store.query_symbols("first", scope=auto.scope_for(tmp_path, "other", str(project)))
    snapshot = auto._path(tmp_path).read_text(encoding="utf-8")
    assert auto.ensure_graph(tmp_path, "group-a", str(project), agent="codex-agent")["build_status"] == "ready"
    assert snapshot == auto._path(tmp_path).read_text(encoding="utf-8")
    auto.set_policy(tmp_path, "group-a", False, str(project))
    source.write_text("def second():\n    return 2\n", encoding="utf-8")
    kwargs = dict(payload={"cwd": str(project), "agent_instance_id": "codex-agent", "share_group_id": "group-a"},
                  tool_name="Write", tool_input={"file_path": str(source)}, tool_result={"ok": True},
                  host_event="post_tool", trusted_host=True)
    assert queue_host_file_refresh(tmp_path, **kwargs)["reason"] == "codegraph_automation_disabled"
    assert store.query_symbols("first", scope=scope)
    auto.set_policy(tmp_path, "group-a", True, str(project))
    assert queue_host_file_refresh(tmp_path, **kwargs)["status"] == "updated"
    assert store.query_symbols("second", scope=scope)
    notice = auto.agent_notice(tmp_path, {"share_group_id": "group-a", "agent_instance_id": "codex-agent", "project_ref": str(project)})
    rendered = _render_context({"context_packet": {"items": []}, "codegraph": notice})
    assert "CodeGraph: ready" in rendered and "memoryguard_capabilities" in rendered and "memoryguard_invoke" in rendered


def test_untrusted_missing_project_and_unbound_agent_cannot_build(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    assert auto.ensure_graph(tmp_path, "group-a", "")["error"] == "codegraph_trusted_project_required"
    assert auto.ensure_graph(tmp_path, "group-a", "relative")["error"] == "codegraph_trusted_project_required"
    assert auto.ensure_graph(tmp_path, "group-a", str(project), agent="missing")["error"] == "binding_unavailable"
    assert not auto._path(tmp_path).exists()
    assert auto.ensure_graph(tmp_path, "group-a", str(Path.home()))["error"] == "codegraph_project_invalid"


def test_disabled_after_queue_does_not_publish(tmp_path, monkeypatch):
    project = tmp_path / "repo"
    project.mkdir()
    (project / "main.py").write_text("def run(): pass\n", encoding="utf-8")
    class Child:
        pid = __import__("os").getpid()
    monkeypatch.setattr(auto.subprocess, "Popen", lambda *a, **k: Child())
    result = auto.ensure_graph(tmp_path, "group-a", str(project))
    assert result["build_status"] == "queued"
    key = auto.scope_for(tmp_path, "group-a", str(project)).digest
    job = auto._read(tmp_path)["jobs"][key]
    auto.set_policy(tmp_path, "group-a", False, str(project))
    auto._run(str(tmp_path), key, job["run_id"])
    assert not (tmp_path / ".memoryguard/codegraph/codegraph.db").exists()
    assert auto.status(tmp_path, "group-a", str(project))["build_status"] == "not_built"


def test_native_bootstrap_and_gui_share_pending_project_and_persisted_switch(tmp_path, monkeypatch):
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
    from memoryguard.runtime_v2.context_engine import ContextEngine
    from memoryguard.system.manifest import ManifestManager
    _bind_refresh_agent(tmp_path)
    project = tmp_path / "repo"
    project.mkdir()
    (project / "app.py").write_text("def app(): pass\n", encoding="utf-8")
    class Child:
        pid = __import__("os").getpid()
    monkeypatch.setattr(auto.subprocess, "Popen", lambda *a, **k: Child())
    manager = ManifestManager(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=manager,
        context_engine=ContextEngine(state="V2_ACTIVE", ready=True))
    def context(agent, admin=False):
        return bind_native_transport_context(AccessContext(trusted_agent_id=agent,
            is_admin=admin, strict_binding=True, allow_anon=False,
            session_id="auto-test", session_source="transport", session_trusted=True),
            workspace_id=str(tmp_path), share_group_id="group-a", project_ref=str(project),
            provider="gui" if admin else "codex", runtime_role="root", entrypoint="gui" if admin else "mcp")
    generation = manager.current().generation
    agent = context("codex-agent")
    result = port.dispatch_mcp("memoryguard_context_bootstrap", {"task": "inspect app"}, context=agent, generation=generation)
    assert result["ok"], result
    notice = result["data"]["codegraph"]
    assert notice["build_status"] == "queued", notice
    gui = context("memoryguard-server-admin", True)
    projects = port.dispatch_gui("list_codegraph_projects", [], context=gui, generation=generation)
    assert projects["ok"], projects
    assert projects["data"]["projects"][0]["automation"]["build_status"] == "queued"
    selector = {"codegraph_project_ref": str(project)}
    graph = port.dispatch_gui("get_codegraph_graph", [selector], context=gui, generation=generation)
    assert graph["ok"] and graph["data"]["status"] == "NO_SOURCE", graph
    request = {**selector, "enabled": False}
    denied = port.dispatch_gui("set_codegraph_automation", [request], context=agent, generation=generation)
    assert not denied["ok"]
    changed = port.dispatch_gui("set_codegraph_automation", [request], context=gui, generation=generation)
    assert changed["ok"], changed
    assert auto.policy(tmp_path, "group-a", str(project))["enabled"] is False
    outside = port.dispatch_gui("set_codegraph_automation", [{"codegraph_project_ref": str(tmp_path / "outside"), "enabled": True}], context=gui, generation=generation)
    assert not outside["ok"] and outside["code"] == "codegraph_project_not_found"


def test_no_code_and_temporary_code_are_not_failed_builds(tmp_path, monkeypatch):
    _bind_refresh_agent(tmp_path)
    project = tmp_path / "repo"
    (project / ".tmp").mkdir(parents=True)
    (project / ".tmp" / "fixture.py").write_text("def temporary(): pass\n", encoding="utf-8")
    class Child:
        pid = __import__("os").getpid()
    monkeypatch.setattr(auto.subprocess, "Popen", lambda *a, **k: Child())
    auto.ensure_graph(tmp_path, "group-a", str(project))
    key = auto.scope_for(tmp_path, "group-a", str(project)).digest
    job = auto._read(tmp_path)["jobs"][key]
    auto._run(str(tmp_path), key, job["run_id"])
    assert auto.status(tmp_path, "group-a", str(project))["build_status"] == "no_source"
    assert auto.ensure_graph(tmp_path, "group-a", str(project))["build_status"] == "no_source"


def test_gui_pending_job_cannot_hide_existing_agent_graph(tmp_path, monkeypatch):
    from test_v2_codegraph_native import _export
    from memoryguard.codegraph_v2 import CodeGraphScope
    from memoryguard.codegraph_v2.graphify_adapter import GraphifyExportAdapter
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
    _bind_refresh_agent(tmp_path)
    project = tmp_path / "repo"
    project.mkdir()
    class Child:
        pid = __import__("os").getpid()
    monkeypatch.setattr(auto.subprocess, "Popen", lambda *a, **k: Child())
    auto.ensure_graph(tmp_path, "group-a", str(project))
    legacy = CodeGraphScope(str(tmp_path), "codex-agent", str(project), "codex", "group-a", "")
    GraphifyExportAdapter(CodeGraphStore(tmp_path)).project(_export(), scope=legacy)
    context = bind_native_transport_context(AccessContext(trusted_agent_id="memoryguard-server-admin", is_admin=True,
        strict_binding=True, allow_anon=False),
        workspace_id=str(tmp_path), share_group_id="group-a", provider="gui", entrypoint="gui")
    rows = NativeV2RuntimePort(tmp_path)._codegraph_gui_project_rows(context)
    assert len(rows) == 1 and rows[0]["built"] and rows[0]["file_count"] == 3
