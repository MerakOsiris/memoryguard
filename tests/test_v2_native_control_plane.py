from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, admin: bool = False):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="session-a",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id="group-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    )


def test_native_provider_install_reuses_bound_v2_identity_without_v1_group_creation(tmp_path: Path, monkeypatch):
    from memoryguard import provider_adapters
    from memoryguard.runtime_v2.group_native import GroupControlService

    fixture_home = tmp_path / "native-provider-home"
    fixture_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fixture_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    GroupControlService(tmp_path, write=True).bind_agents(["agent-a"], share_group_id="group-a")
    GroupControlService(tmp_path, write=True).record_provider_identity("codex", "agent-a", "group-a")
    calls: list[dict] = []

    def fake_install(self, workspace="", share_group_id="default", agent_instance_id="", global_scope=False):
        calls.append({
            "workspace": str(workspace),
            "share_group_id": share_group_id,
            "agent_instance_id": agent_instance_id,
            "global_scope": global_scope,
        })
        return {
            "status": "configured",
            "restart_required": True,
            "runtime_verified": False,
            "binding_id": "binding-a",
            "hook_configured": True,
            "hook_runtime_verified": False,
            "warnings": [],
            "mcp_config_file": "C:/sensitive/path/config.toml",
        }

    monkeypatch.setattr(provider_adapters.CodexAdapter, "install", fake_install)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())

    denied = port.dispatch_mcp(
        "memoryguard_provider_install", {"provider": "codex"},
        context=_context(tmp_path, admin=False), generation=7, state="V2_ACTIVE",
    )
    assert denied["ok"] is False
    assert denied["code"] == "admin_capability_required"
    assert calls == []

    result = port.dispatch_mcp(
        "memoryguard_provider_install", {"provider": "codex"},
        context=_context(tmp_path, admin=True), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"] == {
        "provider": "codex",
        "status": "configured",
        "restart_required": True,
        "runtime_verified": False,
        "binding_id": "binding-a",
        "hook_configured": True,
        "hook_runtime_verified": False,
        "warnings": [],
    }
    assert calls == [{
        "workspace": str(tmp_path.resolve()),
        "share_group_id": "group-a",
        "agent_instance_id": "agent-a",
        "global_scope": True,
    }]
    assert not (tmp_path / ".memoryguard" / "shared-memory").exists()
    assert "config.toml" not in repr(result)


def test_native_explain_uses_current_v2_reference_audit_not_legacy_report(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    audit = port.dispatch_mcp(
        "memoryguard_audit", {}, context=context, generation=7, state="V2_ACTIVE",
    )
    assert audit["ok"] is True, audit
    blockers = audit["data"].get("blockers") or []
    assert blockers, audit
    finding_id = blockers[0]["finding_id"]

    explained = port.dispatch_mcp(
        "memoryguard_explain", {"finding_id": finding_id},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert explained["ok"] is True, explained
    data = explained["data"]
    assert data["finding_id"] == finding_id
    assert data["evidence"] == {"source": "v2_reference_audit", "read_only": True}
    assert data["impact"]
    assert data["suggestion"]
    assert data["confidence"] == 1.0
    assert not (tmp_path / ".memoryguard" / "reports" / "report.json").exists()
