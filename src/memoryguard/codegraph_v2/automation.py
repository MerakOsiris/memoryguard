"""Project automation policy and short-lived, detached initial graph builder.

Only trusted transport projects or GUI-authorized sources enter ``ensure_graph``.
The worker survives a Hook exit; policy and jobs are serialized across processes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
from typing import Any

from ..governance_lock import WorkspaceGovernanceLock
from ..rule_scope import canonical_project_ref
from ..runtime_lease import _pid_alive
from .models import CodeGraphScope
from .store import CodeGraphStore, _assert_no_reparse


def _path(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / ".memoryguard" / "codegraph" / "automation.json"


def _read(workspace: str | Path) -> dict[str, Any]:
    path = _path(workspace)
    _assert_no_reparse(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "groups": {}, "jobs": {}}
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("codegraph_automation_policy_invalid")
    if not isinstance(value.get("groups"), dict) or not isinstance(value.get("jobs"), dict):
        raise ValueError("codegraph_automation_policy_invalid")
    for item in value["groups"].values():
        if not isinstance(item, dict) or type(item.get("default_enabled")) is not bool:
            raise ValueError("codegraph_automation_policy_invalid")
        if not isinstance(item.get("projects"), dict) or any(type(v) is not bool for v in item["projects"].values()):
            raise ValueError("codegraph_automation_policy_invalid")
    if any(not isinstance(job, dict) for job in value["jobs"].values()):
        raise ValueError("codegraph_automation_policy_invalid")
    return value


def _save(workspace: str | Path, value: dict[str, Any]) -> None:
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix("." + uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _policy(value: dict[str, Any], group: str, project: str = "") -> dict[str, Any]:
    item = value["groups"].get(group, {"default_enabled": True, "projects": {}})
    override = item["projects"].get(canonical_project_ref(project))
    return {"policy_status": "ok", "default_enabled": item["default_enabled"],
            "project_override": override,
            "enabled": item["default_enabled"] if override is None else override}


def policy(workspace: str | Path, group: str, project: str = "") -> dict[str, Any]:
    try:
        return _policy(_read(workspace), group, project)
    except (OSError, ValueError, RuntimeError):
        return {"policy_status": "unavailable", "default_enabled": False,
                "project_override": None, "enabled": False,
                "error": "codegraph_automation_policy_unavailable"}


def set_policy(workspace: str | Path, group: str, enabled: bool, project: str = "") -> dict[str, Any]:
    if not group or type(enabled) is not bool:
        raise ValueError("codegraph_automation_policy_invalid")
    with WorkspaceGovernanceLock(workspace):
        value = _read(workspace)  # Invalid/unreadable settings must never reset to ON.
        item = value["groups"].setdefault(group, {"default_enabled": True, "projects": {}})
        if project:
            item["projects"][canonical_project_ref(project)] = enabled
        else:
            item["default_enabled"] = enabled
        _save(workspace, value)
    return policy(workspace, group, project)


def scope_for(workspace: str | Path, group: str, project: str) -> CodeGraphScope:
    return CodeGraphScope.from_value({"workspace_id": str(Path(workspace).resolve()),
        "share_group_id": group, "agent_instance_id": "", "project_ref": canonical_project_ref(project),
        "provider": "graphify", "runtime_role": "", "trusted_context": True})


def known_projects(workspace: str | Path, group: str) -> list[str]:
    try:
        return sorted({job["project"] for job in _read(workspace)["jobs"].values()
                       if job.get("group") == group and job.get("project")
                       and Path(job["project"]).resolve() not in {Path.home().resolve(), Path(workspace).resolve()}})
    except (OSError, ValueError, RuntimeError):
        return []


def status(workspace: str | Path, group: str, project: str, *, built: bool = False) -> dict[str, Any]:
    result = policy(workspace, group, project)
    result["build_status"] = "ready" if built else "not_built"
    if result["policy_status"] != "ok":
        result["build_status"] = "blocked"
        return result
    try:
        key = scope_for(workspace, group, project).digest
        job = _read(workspace)["jobs"].get(key, {})
        state = job.get("status", "")
        if state in {"queued", "building"}:
            if _pid_alive(job.get("pid")) and time.time() - float(job.get("started", 0)) < 1800:
                result["build_status"] = state
            else:
                result.update(build_status="failed", error="codegraph_worker_interrupted")
        elif state and not built:
            result["build_status"] = state
        if job.get("error") and not built:
            result["error"] = job["error"]
    except (OSError, ValueError, RuntimeError):
        result.update(build_status="blocked", error="codegraph_automation_state_unavailable")
    return result


def ensure_graph(workspace: str | Path, group: str, project: str, *, agent: str = "", provider: str = "graphify", runtime_role: str = "", retry: bool = False) -> dict[str, Any]:
    """Called after transport/source authorization, never with task text paths."""
    if not group or not project or not Path(project).is_absolute():
        return {"build_status": "blocked", "error": "codegraph_trusted_project_required"}
    root = Path(project).expanduser()
    _assert_no_reparse(root)
    root = root.resolve()
    if not root.is_dir() or root in {Path(root.anchor), Path.home().resolve(), Path(workspace).resolve()}:
        return {"build_status": "blocked", "error": "codegraph_project_invalid"}
    if agent:
        from .refresh import _has_active_binding
        if not _has_active_binding(Path(workspace), {"agent_instance_id": agent, "share_group_id": group}):
            return {"build_status": "blocked", "error": "binding_unavailable"}
    scope = scope_for(workspace, group, str(root))
    current = status(workspace, group, str(root))
    # Read only metadata to recognize existing GUI/migrated graphs. A known
    # empty completed graph is also final, avoiding repeated empty scans.
    db = Path(workspace) / ".memoryguard" / "codegraph" / "codegraph.db"
    if db.is_file():
        store = CodeGraphStore(workspace, initialize=False)
        nearest = () if store._preflight() in {"fresh", "needs_aux"} else store.nearest_scopes(
            project_ref=scope.project_ref, share_group_id=group,
            agent_instance_id=agent, provider=provider, runtime_role=runtime_role, limit=1)
        if nearest:
            return status(workspace, group, nearest[0].project_ref, built=True)
    with WorkspaceGovernanceLock(workspace):
        current = status(workspace, group, str(root))
        if not current["enabled"] or current["build_status"] in {"queued", "building", "ready"}:
            return current
        if current["build_status"] in {"failed", "blocked", "no_source"} and not retry:
            return current
        value = _read(workspace)
        key = scope.digest
        job = {"group": group, "project": str(root), "agent": agent,
               "status": "queued", "started": time.time(), "run_id": uuid4().hex}
        value["jobs"][key] = job
        _save(workspace, value)
        args = [sys.executable, "-m", "memoryguard.codegraph_v2.automation", str(Path(workspace).resolve()), key, job["run_id"]]
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                   "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            options["start_new_session"] = True
        try:
            child = subprocess.Popen(args, **options)
            job["pid"] = child.pid
        except OSError:
            job.update(status="failed", error="codegraph_worker_start_failed")
        _save(workspace, value)
    return status(workspace, group, str(root))


def agent_notice(workspace: str | Path, context: dict[str, Any], *, start: bool = True) -> dict[str, Any]:
    group, project, agent = (str(context.get(k) or "") for k in ("share_group_id", "project_ref", "agent_instance_id"))
    try:
        result = ensure_graph(workspace, group, project, agent=agent,
            provider=str(context.get("provider") or "graphify"), runtime_role=str(context.get("runtime_role") or "")) if start and agent and project else status(workspace, group, project)
    except Exception:
        result = {"build_status": "blocked", "error": "codegraph_automation_unavailable", "enabled": False}
    state = result.get("build_status", "not_built")
    result["message"] = (f"CodeGraph: {state}; 自动建图{'开启' if result.get('enabled') else '关闭'}。"
        '可用 memoryguard_capabilities(query="codegraph") 发现图谱工具，'
        '再用 memoryguard_invoke(operation="memoryguard_codegraph_status", arguments={}) 查询；'
        'query/path/affected 可查询结构、路径和影响范围。')
    return result


def _run(workspace: str, key: str, run_id: str) -> None:
    try:
        with WorkspaceGovernanceLock(workspace):
            value = _read(workspace)
            job = value["jobs"].get(key, {})
            if job.get("run_id") != run_id:
                return
            group, project = job["group"], job["project"]
            if not _policy(value, group, project)["enabled"]:
                job["status"] = "not_built"
                _save(workspace, value)
                return
            job["status"] = "building"
            _save(workspace, value)
        from ..graphify_core import CODE_EXTENSIONS, collect_files, export_repository, provenance_for_path
        from .graphify_adapter import GraphifyExportAdapter
        root = Path(project)
        _assert_no_reparse(root)
        candidates = collect_files(root, follow_symlinks=False, root=root)
        paths = [p for p in candidates if p.suffix.lower() in CODE_EXTENSIONS
                 and provenance_for_path(p.relative_to(root).as_posix()) == "production"]
        if not paths:
            with WorkspaceGovernanceLock(workspace):
                value = _read(workspace)
                current = value["jobs"].get(key, {})
                if current.get("run_id") == run_id:
                    current["status"] = "no_source"
                    _save(workspace, value)
            return
        if len(paths) > 10_000:
            raise ValueError("graphify export exceeds file limit")
        export = export_repository(root, paths=paths, complete=True, parallel=False, max_files=10_000)
        with WorkspaceGovernanceLock(workspace):
            value = _read(workspace)
            current = value["jobs"].get(key, {})
            if current.get("run_id") != run_id:
                return
            from .refresh import _has_active_binding
            from ..system.manifest import ManifestManager, ManifestState
            bound = not job.get("agent") or _has_active_binding(Path(workspace),
                {"agent_instance_id": job["agent"], "share_group_id": group})
            active = ManifestManager(workspace).current().state is ManifestState.V2_ACTIVE
            if _policy(value, group, project)["enabled"] and bound and active:
                GraphifyExportAdapter(CodeGraphStore(workspace)).project(export,
                    scope=scope_for(workspace, group, project), full_snapshot=True)
                current["status"] = "ready"
            else:
                current["status"] = "not_built"
            _save(workspace, value)
    except Exception as exc:
        with WorkspaceGovernanceLock(workspace):
            value = _read(workspace)
            job = value["jobs"].get(key, {})
            if job.get("run_id") == run_id:
                known_errors = {
                    "graphify export exceeds file limit": "codegraph_source_file_limit",
                    "graphify_source_body_forbidden": "graphify_source_body_forbidden",
                    "graphify node provenance must inherit its source file": "graphify_node_provenance_mismatch",
                    "graphify edge provenance must inherit its source node": "graphify_edge_provenance_mismatch",
                }
                code = known_errors.get(str(exc), "codegraph_build_failed:" + type(exc).__name__)
                job.update(status="failed", error=code)
                _save(workspace, value)


if __name__ == "__main__":
    _run(*sys.argv[1:])
