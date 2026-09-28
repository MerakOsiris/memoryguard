from __future__ import annotations

import copy
from datetime import datetime
import gc
import json
import os
import pickle
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import MappingProxyType
import weakref

import pytest

from memoryguard.cutover_v2 import V2RuntimeFacade
from memoryguard.cutover_v2.facade import _GenerationPort, get_v2_runtime_facade
from memoryguard.cutover_v2.surfaces import (
    CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES,
    MCP_TOOL_NAMES,
)
from memoryguard.runtime_v2.native_ports import (
    NativeContextEnvelope,
    NativeContextError,
    NativePortError,
    NativeV2RuntimePort,
    bind_native_test_capability,
    bind_native_test_services,
    bind_native_transport_context,
    resolve_native_transport_context,
)


def test_gui_history_structured_selector_request_survives_native_binding() -> None:
    from memoryguard.runtime_v2.native_ports import _payload, _phase9_gui_payload

    session = _phase9_gui_payload(
        "gui", "history_read",
        _payload([{"session_id": "session-1", "limit": 100, "offset": 0}]),
    )
    assert session == {"session_id": "session-1", "limit": 100, "offset": 0}

    turn = _phase9_gui_payload(
        "gui", "history_read",
        _payload([{"turn_id": "turn-1", "limit": 1, "offset": 0}]),
    )
    assert turn == {"turn_id": "turn-1", "limit": 1, "offset": 0}


def _trusted_native_context(tmp_path: Path) -> dict[str, object]:
    from memoryguard.access_context import AccessContext

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
        workspace_id=str(tmp_path),
        share_group_id="group-bound",
    )


def _trusted_server_admin_context(tmp_path: Path) -> dict[str, object]:
    from memoryguard.access_context import AccessContext
    from memoryguard.desktop_executor import SERVER_ADMIN_AGENT_ID, SERVER_ADMIN_GROUP_ID

    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=SERVER_ADMIN_AGENT_ID,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="server-admin-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id=SERVER_ADMIN_GROUP_ID,
    )


def _prepare_native_memory_workspace(tmp_path: Path) -> None:
    from memoryguard.evidence.store import EvidenceStore
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.memory.store import MemoryAtomStore

    MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)


def test_native_ports_fresh_import_is_lazy_and_does_not_initialize_storage(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(root / "src"), env.get("PYTHONPATH", "")) if part
    )
    code = (
        "from pathlib import Path; "
        "from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort; "
        f"workspace=Path({str(tmp_path)!r}); NativeV2RuntimePort(workspace); "
        "assert not (workspace / '.memoryguard').exists()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_native_scoped_read_services_are_read_only_and_scope_isolated(tmp_path: Path):
    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    pending = tmp_path / ".memoryguard" / "enrichments" / "pending.jsonl"
    pending.parent.mkdir(parents=True)
    pending.write_text(
        '{"task_id":"t1","memory_id":"m1","scope":{"mode":"share_group","share_group_id":"g1"},"status":"pending","input":{"body":"scoped"}}\n',
        encoding="utf-8",
    )
    before = pending.read_bytes()
    context = bind_native_transport_context(
        __import__("memoryguard.access_context", fromlist=["AccessContext"]).AccessContext(
            trusted_agent_id="agent-a", is_admin=False, strict_binding=True,
            allow_anon=False, session_id="s", session_source="transport", session_trusted=True,
        ),
        workspace_id=str(tmp_path), share_group_id="g1",
    )
    other = bind_native_transport_context(
        __import__("memoryguard.access_context", fromlist=["AccessContext"]).AccessContext(
            trusted_agent_id="agent-b", is_admin=False, strict_binding=True,
            allow_anon=False, session_id="s2", session_source="transport", session_trusted=True,
        ),
        workspace_id=str(tmp_path), share_group_id="g2",
    )
    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    visible = port.dispatch_mcp("memoryguard_list_pending_enrichments", {}, context=context, generation=1)
    hidden = port.dispatch_mcp("memoryguard_list_pending_enrichments", {}, context=other, generation=1)
    status = port.dispatch_mcp("memoryguard_enrichment_status", {}, context=context, generation=1)
    # V2 reads intentionally ignore the retired V1 pending.jsonl queue.  With
    # no V2 Content Plane present the stable result is NO_SOURCE/zero for every
    # scope, and the legacy file remains byte-for-byte untouched.
    assert visible["ok"] and visible["data"]["pending_count"] == 0
    assert visible["data"]["status"] == "NO_SOURCE"
    assert hidden["ok"] and hidden["data"]["pending_count"] == 0
    assert status["ok"] and status["data"]["pending"] == 0
    assert status["data"]["status"] == "NO_SOURCE"
    assert pending.read_bytes() == before
    assert not (tmp_path / ".memoryguard" / "agent-bindings").exists()


def test_native_gui_status_aliases_are_read_only_and_scope_neutral(tmp_path: Path):
    """GUI compatibility aliases stay native, bounded, and non-creating."""

    port = NativeV2RuntimePort(tmp_path)
    first_context = _context(tmp_path)
    second_context = {
        **first_context,
        "agent_instance_id": "other-agent",
        "share_group_id": "other-group",
    }
    aliases = ("get_global_memory_status", "get_api_method_registry")
    before = sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if path.is_file()
    )

    first = {
        name: port.dispatch_gui(name, {}, context=first_context, generation=1)
        for name in aliases
    }
    second = {
        name: port.dispatch_gui(name, {}, context=second_context, generation=1)
        for name in aliases
    }
    repeated = {
        name: port.dispatch_gui(name, {}, context=first_context, generation=1)
        for name in aliases
    }

    assert first["get_global_memory_status"]["ok"] is True
    memory_status = first["get_global_memory_status"]["data"]
    assert memory_status["status"] == "NO_SOURCE"
    assert memory_status["available"] is False
    assert memory_status["total_records"] == 0
    registry = first["get_api_method_registry"]
    assert registry["ok"] is True
    assert registry["data"]["schema"] == "v2-native-coverage-1"
    assert len(registry["data"]["registry_digest"]) == 64
    assert "db_path" not in registry["data"]
    # Scope is now a real bounded V2 status input, so different trusted
    # tenants receive distinct scope projections while repeated calls remain
    # deterministic and non-creating.
    assert first == repeated
    assert second["get_global_memory_status"]["data"]["scope"]["share_group_id"] == "other-group"
    assert second["get_api_method_registry"] == first["get_api_method_registry"]
    assert sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if path.is_file()
    ) == before
    assert not (tmp_path / ".memoryguard").exists()


def test_native_control_plane_reads_and_cli_probes_are_noop(tmp_path: Path):
    """Control-plane aliases never create storage, paths, or child processes."""

    port = NativeV2RuntimePort(tmp_path)
    first_context = _context(tmp_path)
    second_context = {
        **first_context,
        "agent_instance_id": "other-agent",
        "share_group_id": "other-group",
    }
    gui_aliases = (
        "get_sandbox_status",
        "get_host_enrichment_guide",
        "list_host_llm_agents",
    )
    cli_aliases = ("gui", "open", "desktop")

    first = {
        name: port.dispatch_gui(name, {}, context=first_context, generation=1)
        for name in gui_aliases
    }
    second = {
        name: port.dispatch_gui(name, {}, context=second_context, generation=1)
        for name in gui_aliases
    }
    repeated = {
        name: port.dispatch_gui(name, {}, context=first_context, generation=1)
        for name in gui_aliases
    }
    cli = {
        name: port.dispatch_cli(name, {}, generation=1)
        for name in cli_aliases
    }

    assert first["get_sandbox_status"]["ok"] is True
    assert isinstance(first["get_sandbox_status"]["data"]["sandbox"], bool)
    guide = first["get_host_enrichment_guide"]
    assert guide["ok"] is True
    assert guide["data"]["mode"] == "host_enrichment_queue"
    assert guide["data"]["pending_count"] == 0
    agents = first["list_host_llm_agents"]
    assert agents["ok"] is True
    data = agents["data"]
    # 只暴露真实可执行引擎，绝无合成「host」行，也不泄露本地可执行路径。
    assert all(str(item.get("agent") or "") != "host" for item in data["agents"])
    assert all("cli" not in item for item in data["agents"])
    assert data["primary"] == (data["agents"][0]["agent"] if data["agents"] else "")
    assert first == second == repeated
    for name in ("gui", "open"):
        result = cli[name]
        assert result["ok"] is True
        assert result["data"]["host_action"] == name
        assert result["data"]["gated_by"] == "v2_native_manifest"
    assert cli["desktop"]["ok"] is False
    assert cli["desktop"]["code"] == "v2_state_provider_required"

    assert not (tmp_path / ".memoryguard").exists()

    gui_entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["gui"]["entries"]
    }
    cli_entries = {
        item["name"]: item
        for item in port.coverage()["surfaces"]["cli"]["entries"]
    }
    for name in gui_aliases:
        assert gui_entries[name]["status"] == "implemented"
    for name in cli_aliases:
        assert cli_entries[name]["status"] == "implemented"
    assert gui_entries["get_build_progress"]["status"] == "implemented"
    assert gui_entries["get_request_status"]["status"] == "implemented"
    assert gui_entries["list_pending_requests"]["status"] == "implemented"
    assert all(item["status"] != "retired" for item in gui_entries.values())


def test_native_graph_and_semantic_reads_are_scoped_and_no_body_leak(tmp_path: Path):
    from memoryguard.access_context import AccessContext
    from memoryguard.memory.store import MemoryAtomStore
    from memoryguard.runtime_v2.projection_build import ProjectionBuildService
    from memoryguard.projection_v2 import ProjectionReadScope
    from _publish_helpers import seed_atom

    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound", is_admin=False, strict_binding=True,
            allow_anon=False, session_id="graph-session", session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-bound",
        project_ref=str(tmp_path.resolve()),
        provider="codex",
        runtime_role="root",
        sensitivity="normal",
        policy_class="private",
    )
    missing = NativeV2RuntimePort(tmp_path)
    before = list(tmp_path.rglob("*"))
    graph_missing = missing.dispatch_mcp(
        "memoryguard_neuron_graph", {}, context=context, generation=1,
    )
    assert graph_missing["ok"] is True
    assert graph_missing["data"]["status"] == "NO_SOURCE"
    semantic_missing = missing.dispatch_mcp(
        "memoryguard_semantic_check", {"text": "hello"}, context=context, generation=1,
    )
    assert semantic_missing["ok"] is False
    assert semantic_missing["code"] in {"v2_store_unavailable", "v2_schema_missing"}
    assert list(tmp_path.rglob("*")) == before

    seed_atom(
        tmp_path,
        "native-graph-memory",
        "private body must not enter graph",
        agent_id="agent-bound",
        share_group_id="group-bound",
        provider="codex",
        runtime_role="root",
    )
    scope = ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-bound",
        project_ref=str(tmp_path.resolve()),
        provider="codex",
        share_group_id="group-bound",
        sensitivity="normal",
        policy_class="private",
    )
    ProjectionBuildService(tmp_path).build(
        mode="reconstructed", scope=scope, runtime_role="root",
    )
    # Native semantic reads accept an explicitly validated read-only store;
    # the seeded atom is visible only to this exact trusted scope.
    memory_store = MemoryAtomStore(tmp_path)
    readonly_memory = MemoryAtomStore(tmp_path, readonly=True)
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(stores={"memory": readonly_memory}),
    )
    graph = port.dispatch_mcp(
        "memoryguard_neuron_graph", {}, context=context, generation=1,
    )
    assert graph["ok"] is True
    assert graph["data"]["status"] == "READY"
    assert any(node.get("memory_id") == "native-graph-memory" for node in graph["data"]["nodes"])
    encoded_graph = json.dumps(graph, ensure_ascii=False)
    assert "body" not in encoded_graph and "private body" not in encoded_graph

    semantic = port.dispatch_mcp(
        "memoryguard_semantic_check", {"text": "hello", "threshold": 0.8},
        context=context, generation=1,
    )
    assert semantic["ok"] is True
    assert semantic["data"]["duplicates"] == []
    assert semantic["data"]["checked_against"] == 1
    assert not (tmp_path / "legacy").exists()
    del memory_store

    other = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="other-agent", is_admin=False, strict_binding=True,
            allow_anon=False, session_id="other-session", session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path.resolve()),
        share_group_id="other-group",
        project_ref=str(tmp_path.resolve()),
        provider="codex",
        runtime_role="root",
        sensitivity="normal",
        policy_class="private",
    )
    isolated = port.dispatch_mcp(
        "memoryguard_neuron_graph", {}, context=other, generation=1,
    )
    assert isolated["ok"] is True
    assert isolated["data"]["nodes"] == []


def test_cli_hook_mutation_mints_trusted_context_when_outer_cli_omits_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from memoryguard import access_context
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2 import group_native

    access = AccessContext(
        trusted_agent_id="cli-agent",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="cli-session",
        session_source="transport",
        session_trusted=True,
    )
    monkeypatch.setattr(access_context, "load_access_context", lambda: access)

    class BindingService:
        def __init__(self, workspace, *, write):
            assert Path(workspace).resolve() == tmp_path.resolve()
            assert write is False

        def active_binding_for_agent(self, agent_instance_id):
            assert agent_instance_id == "cli-agent"
            return {"share_group_id": "selected-group"}

    monkeypatch.setattr(group_native, "GroupControlService", BindingService)

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    changed = port.dispatch_cli(
        "hooks",
        {"action": "mode", "provider": "codex", "mode": "enforce"},
        context={},
        generation=1,
        # The native classifier must not allow a mutating hooks action to be
        # downgraded by the outer adapter's default mutation=False.
        mutation=False,
        state="V2_ACTIVE",
    )
    assert changed["ok"] is True, changed
    assert changed["data"]["host_action"] == "hooks"

    status = port.dispatch_cli(
        "hooks", {"action": "status"}, context={}, generation=1,
        mutation=False, state="V2_ACTIVE",
    )
    assert status["ok"] is True, status
    assert status["data"]["host_action"] == "hooks"


def test_native_audit_is_real_ro_receipt_without_report_writes(tmp_path: Path):
    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    context = _trusted_native_context(tmp_path)
    before = sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    first = port.dispatch_mcp(
        "memoryguard_audit", {}, context=context, generation=1,
    )
    second = port.dispatch_mcp(
        "memoryguard_audit", {}, context=context, generation=1,
    )
    assert first["ok"] is True
    assert first["status"] == "ok"
    assert first["data"]["status"] == "BLOCKED"
    assert first["data"]["blocked"] is True
    assert "row_hash" not in json.dumps(first, ensure_ascii=False)

    def stable_receipt(receipt: dict) -> dict:
        data = receipt["data"]
        generated_at = data.get("generated_at")
        completed_at = data.get("completed_at")
        assert isinstance(generated_at, str) and generated_at
        assert isinstance(completed_at, str) and completed_at
        assert generated_at == completed_at
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        stable = copy.deepcopy(receipt)
        stable["data"].pop("generated_at", None)
        stable["data"].pop("completed_at", None)
        return stable

    assert stable_receipt(first) == stable_receipt(second)
    assert sorted(
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if path.is_file()
    ) == before
    assert not (tmp_path / ".memoryguard" / "reports").exists()


def _context(tmp_path: Path) -> dict[str, str]:
    return {
        "workspace_id": str(tmp_path),
        "agent_instance_id": "agent-bound",
        "share_group_id": "group-bound",
        "project_ref": "project-bound",
        "provider": "codex",
        "runtime_role": "root",
    }


def test_registry_is_complete_and_digest_is_stable(tmp_path):
    port = NativeV2RuntimePort(tmp_path)
    coverage = port.coverage()
    assert coverage["schema"] == "v2-native-coverage-1"
    assert len(coverage["registry_digest"]) == 64
    assert set(coverage["surfaces"]) == {"mcp", "gui", "cli", "hook"}
    assert {item["name"] for item in coverage["surfaces"]["mcp"]["entries"]} == set(MCP_TOOL_NAMES)
    assert {item["name"] for item in coverage["surfaces"]["gui"]["entries"]} == set(GUI_METHOD_NAMES)
    assert {item["name"] for item in coverage["surfaces"]["cli"]["entries"]} == set(CLI_COMMAND_NAMES)
    assert coverage["registry_digest"] == NativeV2RuntimePort(tmp_path).coverage()["registry_digest"]
    assert coverage["surfaces"]["gui"]["retired"] == 0
    assert coverage["counts"]["neutral-read"] == 0
    assert coverage["complete"] is (coverage["counts"]["blocker"] == 0)
    assert coverage["production_complete"] is (
        coverage["counts"]["blocker"] == 0
        and coverage["counts"]["neutral-read"] == 0
    )
    # Phase-11 acceptance requires every canonical GUI operation to resolve to
    # a native handler. A missing handler is a blocker, never a retired success.
    # Keep these counts aligned with canonical GUI registry expansion.
    assert coverage["surfaces"]["gui"]["total"] == 171
    assert coverage["surfaces"]["gui"]["implemented"] == 171
    assert coverage["surfaces"]["gui"]["blocker"] == 0
    gui_by_name = {
        item["name"]: item for item in coverage["surfaces"]["gui"]["entries"]
    }
    assert gui_by_name["codegraph_status"]["status"] == "implemented"
    assert gui_by_name["codegraph_status"]["mutation"] is False
    for name in (
        "edit_memory", "lock_memory", "unlock_memory",
        "set_memory_injection_policy", "restore_memory", "delete_memory",
    ):
        assert gui_by_name[name]["status"] == "implemented"
    assert gui_by_name["rollback_memory"]["status"] == "implemented"
    assert gui_by_name["rollback_memory"]["reason"] == ""


def test_context_spoof_is_rejected_before_native_handler(tmp_path):
    calls: list[dict] = []
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_services({"memoryguard_memory_status": lambda payload, **kwargs: calls.append(payload) or {"ok": True}}),
    )
    result = port.dispatch_mcp(
        "memoryguard_memory_status",
        {"agent_instance_id": "attacker"},
        context=_context(tmp_path),
        generation=3,
    )
    assert result["code"] == "context_identity_spoof"
    assert not calls


def test_native_transport_resolver_rejects_sentinel_only_and_tampered_association(tmp_path):
    bound = _trusted_native_context(tmp_path)
    authority = bound.bound_context
    assert resolve_native_transport_context(bound) is authority
    assert resolve_native_transport_context(dict(bound)) is authority
    assert resolve_native_transport_context(copy.copy(bound)) is authority

    sentinel_only = {
        "__native_transport_capability": bound["__native_transport_capability"],
        "admin": True,
    }
    with pytest.raises(NativeContextError, match="trusted_context_capability_required"):
        resolve_native_transport_context(sentinel_only)

    tampered = NativeContextEnvelope(authority)
    tampered["__native_bound_context"] = object()
    with pytest.raises(NativeContextError, match="trusted_context_capability_required"):
        resolve_native_transport_context(tampered)

    class EnvelopeSubclass(NativeContextEnvelope):
        pass

    with pytest.raises(NativeContextError, match="trusted_context_capability_required"):
        resolve_native_transport_context(EnvelopeSubclass(authority))
    with pytest.raises(NativeContextError, match="trusted_context_capability_required"):
        resolve_native_transport_context(MappingProxyType(dict(bound)))


def test_wrapper_to_dict_cannot_supply_native_authority_for_read_or_mutation(tmp_path):
    """P1: untrusted wrappers must not turn ``to_dict`` into an authority seam."""

    class Rules:
        def __init__(self):
            self.calls = []

        def upsert_binding(self, value, **kwargs):
            self.calls.append(dict(value))
            return value

    class Wrapper:
        def __init__(self, envelope):
            self.envelope = envelope

        def to_dict(self):
            forged = dict(self.envelope)
            forged.update({
                "admin": True,
                "is_admin": True,
                "agent_instance_id": "victim-agent",
                "share_group_id": "victim-group",
                "session_id": "attacker-session",
                "session_source": "host",
                "session_trusted": True,
            })
            return forged

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    calls = []
    rules = Rules()
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(
            services={
                "memoryguard_memory_status": lambda payload, **kwargs: calls.append(kwargs["context"]) or {"ok": True},
            },
            stores={"rules": rules},
        ),
        state_provider=Manifest(),
    )
    wrapper = Wrapper(_trusted_native_context(tmp_path))
    read = port.dispatch_mcp(
        "memoryguard_memory_status", {}, context=wrapper, generation=1,
    )
    assert read["code"] == "trusted_context_capability_required"
    assert calls == []

    mutation = port.dispatch_mcp(
        "memoryguard_binding_create",
        {"binding_id": "wrapper-attack", "definition_id": "d1", "target_type": "system"},
        context=wrapper,
        generation=1,
    )
    assert mutation["code"] == "trusted_context_capability_required"
    assert rules.calls == []


def test_native_bound_context_registry_is_weak_and_shrinks_after_gc(tmp_path):
    from memoryguard.runtime_v2 import native_ports

    baseline = len(native_ports._NATIVE_BOUND_CONTEXTS)
    envelopes = [
        _trusted_native_context(tmp_path)
        for _ in range(32)
    ]
    references = [weakref.ref(envelope.bound_context) for envelope in envelopes]
    assert len(native_ports._NATIVE_BOUND_CONTEXTS) >= baseline + 32
    del envelopes
    gc.collect()
    assert all(reference() is None for reference in references)
    # Weak registries may also release baseline contexts owned by prior tests
    # once this explicit collection runs.  The invariant is no retained growth
    # from this batch, not that unrelated garbage must stay alive.
    assert len(native_ports._NATIVE_BOUND_CONTEXTS) <= baseline


def test_plain_governed_injection_is_rejected_and_coverage_cannot_promote(tmp_path):
    with pytest.raises(NativePortError, match="service_injection_capability"):
        NativeV2RuntimePort(tmp_path, services={"memoryguard_memory_write": lambda *_a, **_k: {}})
    with pytest.raises(NativePortError, match="store_injection_capability"):
        NativeV2RuntimePort(tmp_path, memory_store=object())

    # An explicit capability is only a test seam; it does not alter the
    # registry/readiness claim for any mutation surface.
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(
            services={"memoryguard_history_read": lambda *_a, **_k: {"forged": True}},
        ),
    )
    entry = next(item for item in port.coverage()["surfaces"]["mcp"]["entries"] if item["name"] == "memoryguard_history_read")
    # History is now a production builtin; a generic test service cannot
    # promote or replace it, but the registry remains implemented.
    assert entry["status"] == "implemented"
    result = port.dispatch_mcp(
        "memoryguard_history_read",
        {"session_id": "missing"},
        context=_context(tmp_path),
        generation=3,
    )
    assert result["ok"] is True
    assert result["data"]["neutral"] is True


@pytest.mark.parametrize(
    "argument",
    [
        "memory_store", "evidence_store", "governance", "rule_store",
        "asset_store", "skill_store", "knowledge_adapter", "content_store",
        "codegraph_store", "projection_store",
    ],
)
def test_direct_store_and_governance_overrides_are_rejected(argument, tmp_path):
    with pytest.raises(NativePortError, match="native_.*injection|native_governance_injection"):
        NativeV2RuntimePort(tmp_path, **{argument: object()})


def test_governed_service_override_never_runs_even_with_private_capability(tmp_path):
    _prepare_native_memory_workspace(tmp_path)

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    calls: list[dict] = []
    with pytest.raises(NativePortError, match="native_mutation_service_override_forbidden"):
        NativeV2RuntimePort(
            tmp_path,
            state_provider=Manifest(),
            services=bind_native_test_services(
                {"memoryguard_memory_write": lambda payload, **kwargs: calls.append(dict(kwargs)) or {"forged": True}},
            ),
        )
    assert calls == []


def test_schema_lease_blocks_path_replacement_before_writable_constructor(tmp_path, monkeypatch):
    _prepare_native_memory_workspace(tmp_path)

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    db = tmp_path / ".memoryguard" / "memory" / "memory.db"
    original_identity = port._file_identity
    identity_calls = 0

    def replaced_identity(path):
        nonlocal identity_calls
        identity_calls += 1
        value = original_identity(path)
        # The second check observes a replacement between preflight and the
        # writable constructor.  No Store constructor should be reached.
        return (*value[:3], value[3] + 1) if identity_calls >= 2 else value

    monkeypatch.setattr(port, "_file_identity", replaced_identity)
    result = port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "replace", "body": "x", "idempotency_key": "replace-1"},
        context=_trusted_native_context(tmp_path),
        generation=1,
    )
    assert result["code"] == "v2_schema_replaced"
    assert db.stat().st_size > 0


@pytest.mark.parametrize(
    "service_result",
    [
        {"ok": False, "status": "error", "error": "denied", "detail": "extra"},
        {"ok": True, "status": "blocked", "reason": "gate", "detail": "extra"},
        {"status": "failed", "code": "failed_code", "detail": "extra"},
    ],
)
def test_native_result_promotes_failure_envelope_even_with_extra_fields(tmp_path, service_result):
    result = NativeV2RuntimePort._result(
        "mcp", "memoryguard_memory_status", service_result, generation=1,
    )
    assert result["ok"] is False
    assert result["status"] in {"error", "blocked", "failed"}
    assert result["path"] == "v2"
    assert result["detail"] == "extra"


def test_history_surface_is_a_native_neutral_read_when_unbound(tmp_path):
    port = NativeV2RuntimePort(tmp_path)
    result = port.dispatch_mcp(
        "memoryguard_history_read", {}, context=_context(tmp_path), generation=1,
    )
    assert result["status"] == "ok"
    assert result["data"]["neutral"] is True


def test_hook_bootstrap_uses_one_native_route(tmp_path):
    calls: list[tuple] = []

    class Engine:
        def bootstrap(self, request, candidates):
            calls.append((request, candidates))
            return {"ready": True, "items": []}

    port = NativeV2RuntimePort(tmp_path, context_engine=Engine())
    result = port.bootstrap_hook(
        {"task": "hello"}, {"candidates": []}, context=_context(tmp_path), generation=5,
    )
    assert result["ok"] is True
    assert result["path"] == "v2"
    assert result["generation"] == 5
    assert len(calls) == 1


def test_facade_active_routes_native_once_and_ready_blocks_write(tmp_path):
    class Manifest:
        def __init__(self, state):
            self.state = state
            self.calls = 0

        def current(self):
            self.calls += 1
            return {"state": self.state, "generation": 7}

    calls: list[tuple] = []
    native = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_services({
            "memoryguard_memory_status": lambda payload, **kwargs: calls.append((payload, kwargs)) or {"healthy": True},
        }),
    )
    manifest = Manifest("V2_ACTIVE")
    facade = V2RuntimeFacade(manifest=manifest, v2=native, workspace=str(tmp_path))
    result = facade.dispatch_mcp("memoryguard_memory_status", {}, context=_context(tmp_path))
    assert result["path"] == "v2" and result["ok"] is True
    assert len(calls) == 1 and manifest.calls == 1

    ready = V2RuntimeFacade(manifest=Manifest("V2_READY"), v2=native, workspace=str(tmp_path))
    denied = ready.dispatch_mcp("memoryguard_memory_write", {"memory_id": "m", "body": "x"}, context=_context(tmp_path))
    assert denied["code"] == "v2_not_active"


def test_missing_or_partial_store_never_creates_or_heals(tmp_path):
    port = NativeV2RuntimePort(tmp_path)
    result = port.dispatch_mcp("memoryguard_memory_status", {}, context=_context(tmp_path), generation=1)
    assert result["ok"] is True
    assert result["data"]["status"] == "NO_SOURCE"
    assert result["data"]["available"] is False
    assert result["data"]["total_records"] == 0
    assert not (tmp_path / ".memoryguard").exists()

    db = tmp_path / ".memoryguard" / "memory" / "memory.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")
    before = db.stat().st_size
    result = port.dispatch_mcp("memoryguard_memory_status", {}, context=_context(tmp_path), generation=1)
    assert result["ok"] is False
    assert result["code"] in {"v2_schema_missing", "v2_store_schema_invalid", "v2_memory_status_unavailable"}
    assert db.stat().st_size == before


def test_native_memory_mutation_preflights_empty_schema_before_store_init(tmp_path):
    db = tmp_path / ".memoryguard" / "memory" / "memory.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"")

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    result = port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "m", "body": "x", "idempotency_key": "k", "evidence": [{"source_ref": "s", "digest": "d"}]},
        context=_trusted_native_context(tmp_path),
        generation=1,
    )
    assert result["code"] == "v2_schema_missing"
    assert db.stat().st_size == 0


def test_native_memory_mutation_blocks_future_and_partial_schema(tmp_path):
    from memoryguard.memory.store import MemoryAtomStore

    MemoryAtomStore(tmp_path)
    memory_db = tmp_path / ".memoryguard" / "memory" / "memory.db"
    with sqlite3.connect(memory_db) as conn:
        conn.execute("UPDATE memory_schema_meta SET version=99 WHERE domain='memory'")
        conn.commit()

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    future = port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "m", "body": "x", "idempotency_key": "k"},
        context=_trusted_native_context(tmp_path),
        generation=1,
    )
    assert future["code"] == "v2_schema_future"

    # A fresh empty file is classified as missing; a marker-bearing file
    # without required tables/columns is a stable partial blocker.
    partial_root = tmp_path / "partial"
    MemoryAtomStore(partial_root)
    partial_db = partial_root / ".memoryguard" / "memory" / "memory.db"
    with sqlite3.connect(partial_db) as conn:
        conn.execute("DROP TABLE atom_deltas")
        conn.commit()
    partial_port = NativeV2RuntimePort(partial_root, state_provider=Manifest())
    partial = partial_port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "m", "body": "x", "idempotency_key": "k"},
        context=_trusted_native_context(partial_root),
        generation=1,
    )
    assert partial["code"] == "v2_schema_partial"


def test_memory_policy_update_and_legacy_outbox_retry_preserve_evidence_identity(tmp_path: Path):
    """Mutable policy changes reuse source evidence; old failed rows remain retryable."""
    from memoryguard.evidence import EvidenceStore
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.memory.store import MemoryAtomStore

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    native = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    facade = V2RuntimeFacade(manifest=Manifest(), v2=native, workspace=str(tmp_path))
    context = _trusted_native_context(tmp_path)

    created = facade.dispatch_mcp(
        "memoryguard_memory_write",
        {
            "memory_id": "always-rule",
            "body": "Use the configured model for memory rules.",
            "kind": "procedure",
            "status": "active",
            "injection_policy": "always",
            "priority": 10,
            "visibility": "active",
            "idempotency_key": "create-always-rule",
        },
        context=context,
    )
    assert created["ok"] is True, created
    initial_read = facade.dispatch_mcp(
        "memoryguard_memory_read", {"memory_id": "always-rule"}, context=context,
    )
    assert initial_read["ok"] is True
    atom_id = initial_read["data"]["atom_id"]
    evidence_ids = memory.evidence_ids_for_atom(atom_id)
    assert len(evidence_ids) == 1
    evidence_id = evidence_ids[0]
    original_evidence = evidence._get_evidence_unscoped(evidence_id).to_dict()

    # Same body and source identity, but a changed mutable setting used to
    # reuse the evidence_id while changing its immutable metadata.
    policy_update = {
        "memory_id": "always-rule",
        "injection_policy": "relevant",
        "idempotency_key": "same-policy-retry",
    }
    changed = facade.dispatch_mcp(
        "memoryguard_memory_update", policy_update, context=context,
    )
    assert changed["ok"] is True, changed
    after_policy_read = facade.dispatch_mcp(
        "memoryguard_memory_read", {"memory_id": "always-rule"}, context=context,
    )
    assert after_policy_read["ok"] is True
    assert after_policy_read["data"]["injection_policy"] == "relevant"
    assert after_policy_read["data"]["priority"] == 10
    assert after_policy_read["data"]["visibility"] == "active"
    assert set(memory.evidence_ids_for_atom(atom_id)) == {evidence_id}
    assert evidence._get_evidence_unscoped(evidence_id).to_dict() == original_evidence

    # Model a failed pre-fix event: the same automatically-derived ID and
    # source evidence, with only its legacy injection_policy metadata changed.
    legacy_payload = dict(original_evidence)
    legacy_payload.pop("evidence_id", None)
    legacy_payload["metadata"] = {
        **dict(original_evidence["metadata"]),
        "injection_policy": "always",
    }
    legacy_event_id = memory.queue_evidence(
        legacy_payload,
        subject_type="atom",
        subject_id=atom_id,
        relation="supports",
        aggregate_id=atom_id,
    )
    # Also cover an old automatic row whose immutable metadata already stored
    # the previous policy: retrying it with the new policy must preserve it.
    historical = {
        "source_ref": "fixture:historical-policy",
        "digest": "historical-policy-digest",
        "authority": "observed",
        "status": "valid",
        "metadata": {
            "source_event_id": "historical-event",
            "agent_instance_id": "agent-bound",
            "share_group_id": "group-bound",
            "kind": "procedure",
            "classification_override": None,
            "injection_policy": "always",
        },
        "subject_type": "atom",
        "subject_id": atom_id,
    }
    evidence.project_batch([{
        "event_id": "seed-historical-policy-evidence",
        "aggregate_id": atom_id,
        "payload": historical,
    }])
    historical_evidence_id = evidence._coerce_evidence(historical).evidence_id
    historical_row = evidence._get_evidence_unscoped(historical_evidence_id).to_dict()
    historical_candidate = dict(historical)
    historical_candidate.pop("evidence_id", None)
    historical_candidate["metadata"] = {
        **historical["metadata"],
        "injection_policy": "relevant",
    }
    historical_failed_event_id = memory.queue_evidence(
        historical_candidate,
        subject_type="atom",
        subject_id=atom_id,
        relation="supports",
        aggregate_id=atom_id,
    )
    with sqlite3.connect(memory.db_path) as conn:
        conn.execute(
            "UPDATE domain_outbox SET status='failed', error_json=?, attempts=1 WHERE event_id=?",
            (json.dumps({"type": "ValueError", "message": "evidence_id conflict: synthetic"}), legacy_event_id),
        )
        conn.execute(
            "UPDATE domain_outbox SET status='failed', error_json=?, attempts=1 WHERE event_id=?",
            (json.dumps({"type": "ValueError", "message": "evidence_id conflict: synthetic"}), historical_failed_event_id),
        )

    # An unrelated real MCP write retries the old failed event in the normal
    # batch projector, then publishes its own new evidence in that same batch.
    unrelated = facade.dispatch_mcp(
        "memoryguard_memory_write",
        {
            "memory_id": "unrelated-rule",
            "body": "Keep independent rules in their own records.",
            "kind": "procedure",
            "status": "active",
            "injection_policy": "relevant",
            "priority": 1,
            "visibility": "active",
            "idempotency_key": "create-unrelated-rule",
        },
        context=context,
    )
    assert unrelated["ok"] is True, unrelated
    assert memory.pending_outbox(include_failed=True) == []
    assert evidence._get_evidence_unscoped(historical_evidence_id).to_dict() == historical_row
    unrelated_read = facade.dispatch_mcp(
        "memoryguard_memory_read", {"memory_id": "unrelated-rule"}, context=context,
    )
    assert unrelated_read["ok"] is True
    assert unrelated_read["data"]["body"] == "Keep independent rules in their own records."

    # A same-key repeat is idempotent and leaves the projected row untouched.
    revision = after_policy_read["data"]["revision"]
    replay = facade.dispatch_mcp(
        "memoryguard_memory_update", policy_update, context=context,
    )
    assert replay["ok"] is True, replay
    final_read = facade.dispatch_mcp(
        "memoryguard_memory_read", {"memory_id": "always-rule"}, context=context,
    )
    assert final_read["ok"] is True
    assert final_read["data"]["revision"] == revision
    assert final_read["data"]["injection_policy"] == "relevant"
    assert evidence._get_evidence_unscoped(evidence_id).to_dict() == original_evidence
    assert memory.pending_outbox(include_failed=True) == []

    # Compatibility is narrow: explicit IDs, trusted-scope changes, evidence
    # status changes, and malformed legacy policy values still conflict.
    def assert_projection_conflict(label: str, candidate: dict[str, object]) -> None:
        candidate.update({"subject_type": "atom", "subject_id": atom_id})
        event = {"event_id": "strict-" + label, "aggregate_id": atom_id, "payload": candidate}
        with pytest.raises(ValueError, match="evidence_id conflict:"):
            evidence.project_batch([event])

    explicit_policy_change = dict(original_evidence)
    explicit_policy_change["metadata"] = {**dict(original_evidence["metadata"]), "injection_policy": "relevant"}
    assert_projection_conflict("explicit-id", explicit_policy_change)

    wrong_scope = dict(original_evidence)
    wrong_scope.pop("evidence_id", None)
    wrong_scope["metadata"] = {**dict(original_evidence["metadata"]), "share_group_id": "other-scope"}
    assert_projection_conflict("scope", wrong_scope)

    wrong_status = dict(original_evidence)
    wrong_status.pop("evidence_id", None)
    wrong_status["status"] = "stale"
    assert_projection_conflict("status", wrong_status)

    invalid_legacy_policy = dict(original_evidence)
    invalid_legacy_policy.pop("evidence_id", None)
    invalid_legacy_policy["metadata"] = {
        **dict(original_evidence["metadata"]),
        "injection_policy": {"unexpected": "value"},
    }
    assert_projection_conflict("invalid-policy", invalid_legacy_policy)

    whitespace_id_evidence = {
        "evidence_id": " ",
        "source_ref": "fixture:whitespace-id",
        "digest": "whitespace-id-digest",
        "authority": "observed",
        "status": "valid",
        "metadata": {"injection_policy": "always"},
        "subject_type": "atom",
        "subject_id": atom_id,
    }
    evidence.project_batch([{
        "event_id": "strict-whitespace-create",
        "aggregate_id": atom_id,
        "payload": whitespace_id_evidence,
    }])
    whitespace_id_conflict = dict(whitespace_id_evidence)
    whitespace_id_conflict["metadata"] = {"injection_policy": "relevant"}
    assert_projection_conflict("whitespace-explicit-id", whitespace_id_conflict)


def test_native_memory_governance_receipt_replays_across_restart_and_conflicts_on_body(tmp_path):
    _prepare_native_memory_workspace(tmp_path)

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    request = {
        "memory_id": "native-retry",
        "body": "v1",
        "idempotency_key": "native-request-1",
        "evidence": [{"source_ref": "native-retry", "digest": "r1"}],
    }
    context = _trusted_native_context(tmp_path)
    first = NativeV2RuntimePort(tmp_path, state_provider=Manifest()).dispatch_mcp(
        "memoryguard_memory_write", request, context=context, generation=1,
    )
    second = NativeV2RuntimePort(tmp_path, state_provider=Manifest()).dispatch_mcp(
        "memoryguard_memory_write", request, context=context, generation=1,
    )
    assert first["ok"] is True and second["ok"] is True
    assert first["data"]["receipt"]["decision_id"] == second["data"]["receipt"]["decision_id"]
    conflict = NativeV2RuntimePort(tmp_path, state_provider=Manifest()).dispatch_mcp(
        "memoryguard_memory_write",
        {**request, "body": "v2"},
        context=context,
        generation=1,
    )
    assert conflict["code"] == "idempotency_conflict"


def test_production_factory_wires_native_port_without_eager_storage(tmp_path):
    facade = get_v2_runtime_facade(str(tmp_path))
    assert isinstance(facade.ports.v2, NativeV2RuntimePort)
    assert not (tmp_path / ".memoryguard").exists()


def test_initialized_memory_store_status_reaches_native_handler(tmp_path):
    from memoryguard.memory.store import MemoryAtomStore

    MemoryAtomStore(tmp_path)

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 3}

    facade = V2RuntimeFacade(
        manifest=Manifest(), v2=NativeV2RuntimePort(tmp_path), workspace=str(tmp_path),
    )
    result = facade.dispatch_mcp("memoryguard_memory_status", {}, context=_context(tmp_path))
    assert result["ok"] is True
    assert result["data"]["available"] is True
    assert result["data"]["total_records"] == 0
    assert result["data"]["status_counts"] == {}
    assert result["data"]["kind_counts"] == {}
    assert result["data"]["evidence_link_count"] == 0


def test_binding_create_requires_trusted_admin_and_ignores_payload_authority(tmp_path):
    class Rules:
        def __init__(self):
            self.calls = []

        def upsert_binding(self, value, **kwargs):
            self.calls.append(dict(value))
            return value

    rules = Rules()

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(stores={"rules": rules}),
        state_provider=Manifest(),
    )
    payload = {
        "binding_id": "b1",
        "definition_id": "d1",
        "target_type": "system",
        "target_id": "",
        "created_by": "manual",
        "owner_agent_id": "victim",
        "owner": "victim",
        "authorization": "forged",
        "authority": "forged",
    }
    for claimed_admin in (False, True):
        denied = port.dispatch_mcp(
            "memoryguard_binding_create",
            payload,
            context={**_context(tmp_path), "admin": claimed_admin},
            generation=1,
            state="V2_ACTIVE",
        )
        assert denied["code"] == "trusted_context_capability_required"

    from memoryguard.access_context import AccessContext

    bound_non_admin = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="trusted-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-bound",
    )
    denied = port.dispatch_mcp(
        "memoryguard_binding_create",
        payload,
        context=bound_non_admin,
        generation=1,
        state="V2_ACTIVE",
    )
    assert denied["code"] == "admin_capability_required"
    assert rules.calls == []


def test_binding_create_uses_context_admin_identity_for_audit_fields(tmp_path):
    class Rules:
        def __init__(self):
            self.calls = []

        def upsert_binding(self, value, **kwargs):
            self.calls.append(dict(value))
            return value

    rules = Rules()

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    # Even an already-bound transport context cannot self-assert the
    # manifest state; direct mutation requires the trusted provider/CAS.  The
    # explicit test store is read-only and must never override the builtin
    # mutation route.
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(stores={"rules": rules}),
        state_provider=Manifest(),
    )
    from memoryguard.access_context import AccessContext

    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="trusted-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-bound",
        project_ref="project-bound",
        provider="codex",
        runtime_role="root",
    )
    result = port.dispatch_mcp(
        "memoryguard_binding_create",
        {
            "binding_id": "b1",
            "definition_id": "d1",
            "target_type": "system",
            "target_id": "",
            "created_by": "manual",
            "owner_agent_id": "victim",
            "owner": "victim",
            "authorization": "forged",
            "authority": "forged",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert rules.calls == []


def test_native_bound_context_public_mapping_attacks_never_write(tmp_path):
    """P0: only the issued immutable authority may authorize binding_create."""

    class Rules:
        def __init__(self):
            self.calls = []

        def upsert_binding(self, value, **kwargs):
            self.calls.append(dict(value))
            return value

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    rules = Rules()
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(stores={"rules": rules}),
        state_provider=Manifest(),
    )
    from memoryguard.access_context import AccessContext

    bound = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="trusted-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-bound",
    )
    request = {
        "binding_id": "attack",
        "definition_id": "definition",
        "target_type": "system",
        "target_id": "",
    }

    # Mutating the compatibility mapping cannot promote its private authority.
    public_mutation = dict(bound)
    public_mutation.update({
        "admin": True,
        "is_admin": True,
        "agent_instance_id": "victim",
        "share_group_id": "victim-group",
        "session_id": "attacker-session",
        "session_source": "host",
        "session_trusted": True,
    })
    denied = port.dispatch_mcp(
        "memoryguard_binding_create", request, context=public_mutation,
        generation=1,
    )
    assert denied["code"] == "admin_capability_required"

    # Sentinel-only/plain mappings and hand-written lookalikes are not native.
    sentinel_only = dict(public_mutation)
    sentinel_only.pop("__native_bound_context", None)
    denied = port.dispatch_mcp(
        "memoryguard_binding_create", request, context=sentinel_only,
        generation=1,
    )
    assert denied["code"] in {"trusted_context_capability_required", "context_identity_conflict"}

    class Lookalike:
        admin = True
        is_admin = True
        agent_instance_id = "victim"
        share_group_id = "victim-group"
        workspace_id = str(tmp_path)
        session_id = "attacker-session"
        session_source = "host"
        session_trusted = True

    denied = port.dispatch_mcp(
        "memoryguard_binding_create", request, context=Lookalike(),
        generation=1,
    )
    assert denied["code"] == "trusted_context_capability_required"

    # Envelope shallow copies retain the same non-admin authority; copying the
    # authority itself, deep-copy and pickle all fail closed.
    shallow = copy.copy(bound)
    denied = port.dispatch_mcp(
        "memoryguard_binding_create", request, context=shallow,
        generation=1,
    )
    assert denied["code"] == "admin_capability_required"
    with pytest.raises(TypeError):
        copy.copy(bound["__native_bound_context"])
    with pytest.raises(TypeError):
        copy.deepcopy(bound)
    with pytest.raises(TypeError):
        pickle.dumps(bound)
    assert rules.calls == []


def test_direct_mutation_requires_manifest_provider_and_generation_cas(tmp_path):
    class Manifest:
        def __init__(self, state="V2_ACTIVE", generation=7):
            self.state = state
            self.generation = generation

        def current(self):
            return {"state": self.state, "generation": self.generation}

    calls = []
    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    from memoryguard.access_context import AccessContext

    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="trusted-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-bound",
    )
    stale = port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "m1", "body": "x", "idempotency_key": "native-cas-1"},
        context=context,
        generation=6,
    )
    assert stale["code"] == "manifest_generation_mismatch"
    assert calls == []
    current = port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "m1", "body": "x", "idempotency_key": "native-cas-1"},
        context=context,
        generation=7,
    )
    # Governed memory writes never dispatch to an injected service.
    assert current["code"] == "v2_schema_missing"
    assert calls == []


def test_direct_mutation_with_self_asserted_state_is_rejected(tmp_path):
    from memoryguard.access_context import AccessContext

    port = NativeV2RuntimePort(tmp_path)
    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="trusted-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="group-bound",
    )
    result = port.dispatch_mcp(
        "memoryguard_memory_write",
        {"memory_id": "m1", "body": "x"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["code"] == "v2_state_provider_required"


def test_injected_service_cannot_promote_blocker_into_production_coverage(tmp_path):
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_services({"memoryguard_history_read": lambda payload, **kwargs: {"forged": True}}),
    )
    coverage = port.coverage()
    entry = next(
        item for item in coverage["surfaces"]["mcp"]["entries"]
        if item["name"] == "memoryguard_history_read"
    )
    assert entry["status"] == "implemented"
    result = port.dispatch_mcp(
        "memoryguard_history_read", {}, context=_context(tmp_path), generation=1,
    )
    assert result["status"] == "ok"
    assert result["data"]["neutral"] is True


def test_cli_maintenance_requires_its_private_transport_capability(tmp_path):
    from memoryguard.maintenance_v2.runtime_port import (
        MaintenanceRuntimePort,
        bind_maintenance_transport_context,
    )

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    native = NativeV2RuntimePort(
        tmp_path,
        state_provider=Manifest(),
        maintenance_port=MaintenanceRuntimePort(tmp_path),
    )
    trusted = bind_maintenance_transport_context({"trusted_agent_id": "cli-agent"})
    allowed = native.dispatch_cli(
        "storage",
        {"action": "lease-acquire", "ttl_seconds": 30},
        context=trusted,
        generation=1,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert allowed["ok"] is True, allowed

    forged = native.dispatch_cli(
        "storage",
        {"action": "lease-acquire", "ttl_seconds": 30},
        context={"trusted_agent_id": "attacker"},
        generation=1,
        # The caller cannot downgrade a storage write to a read.
        mutation=False,
        state="V2_ACTIVE",
    )
    assert forged["ok"] is False
    assert forged["status"] == "error"

    # A valid maintenance capability cannot downgrade a mutating sub-action
    # and self-assert ACTIVE when the native port has no trusted state source.
    unbound_native = NativeV2RuntimePort(
        tmp_path / "unbound",
        maintenance_port=MaintenanceRuntimePort(tmp_path / "unbound"),
    )
    downgraded = unbound_native.dispatch_cli(
        "storage",
        {"action": "lease-acquire", "ttl_seconds": 30},
        context=trusted,
        generation=1,
        mutation=False,
        state="V2_ACTIVE",
    )
    assert downgraded["ok"] is False
    assert downgraded["code"] == "v2_state_provider_required"


def test_native_statuses_return_scoped_no_source_without_global_store_reads(tmp_path):
    class Exploding:
        def status(self):
            raise AssertionError("native status must not open unrelated aggregate store")

    # Native status routes report real scoped availability without opening an
    # unrelated injected aggregate store or creating missing V2 domains.
    port = NativeV2RuntimePort(
        tmp_path,
        services=bind_native_test_capability(stores={"codegraph": Exploding()}),
    )
    context = _context(tmp_path)
    memory = port.dispatch_mcp("memoryguard_memory_status", {}, context=context, generation=1)
    graph = port.dispatch_mcp("memoryguard_projection_status", {}, context=context, generation=1)
    canonical = port.dispatch_mcp("memoryguard_canonical_status", {}, context=context, generation=1)
    diagnostics = port.dispatch_mcp("memoryguard_diagnostics_snapshot", {}, context=context, generation=1)
    scope = port.dispatch_gui(
        "get_governance_scope", {}, context=_trusted_native_context(tmp_path), generation=1
    )
    for result in (memory, graph, canonical, diagnostics, scope):
        assert result["ok"] is True, result
        assert "db_path" not in result
        assert "atoms" not in result
    assert memory["data"]["status"] == "NO_SOURCE"
    assert memory["data"]["total_records"] == 0
    assert graph["data"]["status"] == "NO_SOURCE"
    assert canonical["data"]["status"] == "NO_SOURCE"
    assert diagnostics["data"]["status"] == "READY"
    assert diagnostics["data"]["memory"]["status"] == "NO_SOURCE"
    assert scope["data"]["empty"] is True
    assert scope["data"]["scope"] is None
    assert scope["data"]["principal_agent_instance_id"] == "agent-bound"
    assert scope["data"]["active_binding"] is None
    # Scope reads no longer echo caller-supplied group/project/provider values;
    # only V2 control-plane state is returned.
    assert "share_group_id" not in scope["data"]
    assert "project_ref" not in scope["data"]
    assert "provider" not in scope["data"]
    assert not (tmp_path / ".memoryguard").exists()


def test_cli_doctor_and_mcp_status_are_safe_without_agent_binding(tmp_path):
    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 11}

    _prepare_native_memory_workspace(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())

    doctor = port.dispatch_cli(
        "doctor", {}, context={}, generation=11, state="V2_ACTIVE",
    )
    status = port.dispatch_cli(
        "mcp-status", {}, context={}, generation=11, state="V2_ACTIVE",
    )

    for result in (doctor, status):
        assert result["ok"] is True, result
        data = result["data"]
        assert data["status"] == "READY"
        assert data["scope_status"] == "UNBOUND"
        assert data["manifest"] == {"state": "V2_ACTIVE", "generation": 11}
        assert data["native_coverage"]["production_complete"] is port.coverage()["production_complete"]
        assert data["native_coverage"]["counts"] == port.coverage()["counts"]
        assert "total_records" not in data
        assert "share_group_id" not in json.dumps(data, ensure_ascii=False)
    assert status["data"]["available"] is True
    assert status["data"]["memory_status"] == "READY"


def test_generation_port_dispatch_does_not_retry_type_error():
    calls = 0

    class Port:
        def dispatch(self, surface, name, args, **kwargs):
            nonlocal calls
            calls += 1
            raise TypeError("implementation failure")

    with pytest.raises(TypeError, match="implementation failure"):
        _GenerationPort(
            Port(), generation=1, state="V2_ACTIVE", facade=V2RuntimeFacade(),
        ).dispatch("gui", "get_audit", {}, context={})
    assert calls == 1


def test_gui_governance_snapshot_is_scoped_and_has_stable_contract(tmp_path: Path):
    from _publish_helpers import seed_atom
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("agent-bound", "group-bound")
    groups.set_scope(
        "agent-bound",
        {"mode": "agent", "agent_instance_id": "agent-bound"},
    )
    seed_atom(
        tmp_path,
        "snapshot-active",
        "bounded snapshot fixture",
        agent_id="agent-bound",
        share_group_id="group-bound",
    )

    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    result = port.dispatch_gui(
        "get_governance_snapshot",
        ["caller-selected-group-must-be-ignored"],
        context=_trusted_native_context(tmp_path),
        generation=1,
        state="V2_ACTIVE",
    )

    assert result["ok"] is True, result
    data = result["data"]
    assert data["governance_state"] == "active_governance"
    assert data["status"]["active_count"] == 1
    assert data["conflicts"]["count"] == 0
    assert data["quarantine"]["count"] == 0
    assert data["group"] == {
        "share_group_id": "group-bound",
        "members": ["agent-bound"],
        "member_count": 1,
    }
    assert "bounded snapshot fixture" not in json.dumps(data, ensure_ascii=False)


def test_gui_governance_snapshot_counts_only_actionable_conflicts(tmp_path: Path):
    from _publish_helpers import seed_atom
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("agent-bound", "group-bound")
    groups.set_scope(
        "agent-bound",
        {"mode": "agent", "agent_instance_id": "agent-bound"},
    )
    conflict_meta = {
        "conflict_group_id": "stale-overview-conflict",
        "conflict_status": "unresolved",
        "conflict_reason": "canonical_composition_conflict",
    }
    seed_atom(
        tmp_path, "stale-live", "stale live claim", agent_id="agent-bound",
        share_group_id="group-bound", metadata=conflict_meta,
    )
    seed_atom(
        tmp_path, "stale-deleted", "stale deleted claim", agent_id="agent-bound",
        share_group_id="group-bound", metadata=conflict_meta, status="deleted",
    )

    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    result = port.dispatch_gui(
        "get_governance_snapshot", [], context=_trusted_native_context(tmp_path),
        generation=1, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"]["conflicts"]["count"] == 0


def test_server_admin_scope_get_uses_selected_agent_binding(tmp_path: Path):
    from memoryguard.runtime_v2.group_native import GroupControlService
    from memoryguard.runtime_v2.group_native import personal_group_id

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("selected-agent", personal_group_id("selected-agent"))
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    context = _trusted_server_admin_context(tmp_path)

    selected = port.dispatch_gui(
        "set_governance_scope",
        [{"mode": "agent", "agent_instance_id": "selected-agent"}],
        context=context,
        generation=1,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert selected["ok"] is True, selected

    scope = port.dispatch_gui(
        "get_governance_scope_state",
        [],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert scope["ok"] is True, scope
    data = scope["data"]
    assert data["empty"] is False
    assert data["active_binding"]["agent_instance_id"] == "selected-agent"
    assert data["active_binding"]["share_group_id"] == personal_group_id("selected-agent")
    assert data["members"] == ["selected-agent"]


def test_server_admin_shared_scope_get_and_snapshot_use_active_members(tmp_path: Path):
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agents(
        ["selected-agent-a", "selected-agent-b"],
        share_group_id="selected-shared-group",
    )
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    context = _trusted_server_admin_context(tmp_path)

    selected = port.dispatch_gui(
        "set_governance_scope",
        [{"mode": "share_group", "share_group_id": "selected-shared-group"}],
        context=context,
        generation=1,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert selected["ok"] is True, selected

    scope = port.dispatch_gui(
        "get_governance_scope_state",
        [],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert scope["ok"] is True, scope
    scope_data = scope["data"]
    assert scope_data["empty"] is False
    assert scope_data["active_binding"]["agent_instance_id"] in {
        "selected-agent-a", "selected-agent-b",
    }
    assert scope_data["active_binding"]["agent_instance_id"] != "memoryguard-server-admin"
    assert scope_data["members"] == ["selected-agent-a", "selected-agent-b"]

    snapshot = port.dispatch_gui(
        "get_governance_snapshot",
        ["caller-selected-group-must-be-ignored"],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert snapshot["ok"] is True, snapshot
    data = snapshot["data"]
    assert data["governance_state"] == "active_governance"
    assert data["group"] == {
        "share_group_id": "selected-shared-group",
        "members": ["selected-agent-a", "selected-agent-b"],
        "member_count": 2,
    }


def test_server_admin_memberless_scope_is_empty_and_audit_only(tmp_path: Path):
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    binding = groups.bind_agent("selected-agent", "memberless-admin-group")
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    context = _trusted_server_admin_context(tmp_path)

    selected = port.dispatch_gui(
        "set_governance_scope",
        [{"mode": "share_group", "share_group_id": "memberless-admin-group"}],
        context=context,
        generation=1,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert selected["ok"] is True, selected
    groups.unbind(binding["binding_id"])

    scope = port.dispatch_gui(
        "get_governance_scope_state",
        [],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert scope["ok"] is True, scope
    assert scope["data"]["empty"] is True
    assert scope["data"]["scope"] is None
    assert scope["data"]["active_binding"] is None
    assert scope["data"]["members"] == []

    snapshot = port.dispatch_gui(
        "get_governance_snapshot",
        ["memberless-admin-group"],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert snapshot["ok"] is True, snapshot
    assert snapshot["data"]["governance_state"] == "audit_only"
    assert snapshot["data"]["group"] == {
        "share_group_id": "",
        "members": [],
        "member_count": 0,
    }


def test_stale_memberless_scope_is_rejected_and_snapshot_is_audit_only(tmp_path: Path):
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    bound = groups.bind_agent("agent-bound", "memberless-group")
    groups.set_scope(
        "agent-bound",
        {"mode": "share_group", "share_group_id": "memberless-group"},
    )
    groups.unbind(bound["binding_id"])

    scope = groups.scope_state("agent-bound")
    assert scope["empty"] is True
    assert scope["scope"] is None

    context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="stale-snapshot-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path),
        share_group_id="memberless-group",
    )
    result = NativeV2RuntimePort(tmp_path).dispatch_gui(
        "get_governance_snapshot",
        ["memberless-group"],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )

    assert result["ok"] is True, result
    data = result["data"]
    assert data["governance_state"] == "audit_only"
    assert data["status"]["active_count"] == 0
    assert data["conflicts"]["count"] == 0
    assert data["quarantine"]["count"] == 0
    assert data["group"] == {"share_group_id": "", "members": [], "member_count": 0}


def test_server_admin_shared_projection_scope_uses_group_atoms_and_empty_build_fails(tmp_path: Path):
    from _publish_helpers import seed_atom
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agents(["shared-agent-a", "shared-agent-b"], share_group_id="shared-projection-group")
    groups.set_scope(
        "memoryguard-server-admin",
        {"mode": "share_group", "share_group_id": "shared-projection-group"},
        admin=True,
    )
    context = _trusted_server_admin_context(tmp_path)
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )

    seed_atom(tmp_path, "shared-a", "shared A", agent_id="shared-agent-a", share_group_id="shared-projection-group")
    seed_atom(tmp_path, "shared-b", "shared B", agent_id="shared-agent-b", share_group_id="shared-projection-group")
    business_scope = port._gui_projection_scope(context)
    assert business_scope.agent_instance_id == ""
    assert business_scope.share_group_id == "shared-projection-group"

    source_map = port.dispatch_gui(
        "get_projection_source_map", [], context=context, generation=1, state="V2_ACTIVE",
    )
    assert source_map["ok"] is True, source_map
    summary = source_map["data"]["summary"]
    assert summary["selected_source_connectors"] == 0
    assert summary["governed_memory"] == 2
    assert summary["buildable_atom_count"] == 2
    assert all(item["entry_kind"] == "governed_memory" for item in source_map["data"]["entries"])

    graph_before_build = port.dispatch_gui(
        "get_memory_neuron_graph", ["reconstructed"],
        context=context, generation=1, state="V2_ACTIVE",
    )
    assert graph_before_build["ok"] is True, graph_before_build
    graph_data = graph_before_build["data"]
    assert graph_data["reason"] == "not_built"
    assert graph_data["scope"]["agent_instance_id"] == ""
    assert graph_data["scope"]["share_group_id"] == "shared-projection-group"
    assert graph_data["source_map"]["projection_kind"] == "shared_memory_projection"
    assert graph_data["source_map"]["summary"]["buildable_atom_count"] == 2

    accepted = port.dispatch_gui(
        "start_build_projection",
        [True, "reconstructed", {}, "browser-agent", "browser-group", "", "", "deterministic"],
        context=context, generation=1, mutation=True, state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    run_id = accepted["task"]["run_id"]
    final = {}
    for _ in range(500):
        final = port.dispatch_gui(
            "get_build_progress", [run_id], context=context, generation=1, state="V2_ACTIVE",
        )
        if final.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert final["status"] == "succeeded", final
    assert final["result_ref"]["atom_count"] == 2


def test_server_admin_selected_member_of_shared_group_reads_one_group_memory_plane(tmp_path: Path):
    from _publish_helpers import seed_atom
    from memoryguard.runtime_v2.group_native import GroupControlService

    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agents(["shared-agent-a", "shared-agent-b"], share_group_id="shared-projection-group")
    groups.set_scope(
        "memoryguard-server-admin",
        {"mode": "agent", "agent_instance_id": "shared-agent-a"},
        admin=True,
    )
    seed_atom(tmp_path, "shared-a", "shared A", agent_id="shared-agent-a", share_group_id="shared-projection-group")
    seed_atom(tmp_path, "shared-b", "shared B", agent_id="shared-agent-b", share_group_id="shared-projection-group")

    context = _trusted_server_admin_context(tmp_path)
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )

    business_scope = port._gui_projection_scope(context)
    assert business_scope.agent_instance_id == ""
    assert business_scope.share_group_id == "shared-projection-group"

    graph = port.dispatch_gui(
        "get_memory_neuron_graph", ["reconstructed"],
        context=context, generation=1, state="V2_ACTIVE",
    )
    assert graph["ok"] is True, graph
    assert graph["data"]["source_map"]["projection_kind"] == "shared_memory_projection"
    assert graph["data"]["source_map"]["summary"]["governed_memory"] == 2
    assert graph["data"]["source_map"]["summary"]["buildable_atom_count"] == 2

    snapshot = port.dispatch_gui(
        "get_governance_snapshot", [],
        context=context, generation=1, state="V2_ACTIVE",
    )
    assert snapshot["ok"] is True, snapshot
    assert snapshot["data"]["status"]["active_count"] == 2
    assert snapshot["data"]["group"]["member_count"] == 2

    empty_groups = GroupControlService(tmp_path, write=True)
    empty_groups.bind_agents(["empty-agent-a", "empty-agent-b"], share_group_id="empty-projection-group")
    empty_groups.set_scope(
        "memoryguard-server-admin",
        {"mode": "share_group", "share_group_id": "empty-projection-group"},
        admin=True,
    )
    empty = port.dispatch_gui(
        "start_build_projection",
        [True, "reconstructed", {}, "browser-agent", "empty-projection-group", "", "", "deterministic"],
        context=context, generation=1, mutation=True, state="V2_ACTIVE",
    )
    assert empty["ok"] is True, empty
    empty_run_id = empty["task"]["run_id"]
    empty_final = {}
    for _ in range(500):
        empty_final = port.dispatch_gui(
            "get_build_progress", [empty_run_id], context=context, generation=1, state="V2_ACTIVE",
        )
        if empty_final.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert empty_final["status"] == "failed", empty_final
    assert empty_final["error"]["code"] == "no_projection_sources"


def test_gui_rule_record_projects_v2_rule_into_existing_card_contract() -> None:
    row = NativeV2RuntimePort._gui_rule_record({
        "definition_id": "definition-a",
        "memory_id": "memory-a",
        "canonical_text": "Run focused tests before release.",
        "rule_kind": "procedure",
        "rule_strength": "must",
        "bindings": [{
            "binding_id": "binding-a",
            "target_type": "agent",
            "target_id": "agent-a",
            "project_ref": "",
            "effect": "include",
            "priority": 100,
        }],
    })

    assert row["body"] == "Run focused tests before release."
    assert row["kind"] == "procedure"
    assert row["status"] == "active"
    assert row["injection_policy"] == "always"
    assert row["priority"] == 100
    assert row["assignments"] == [{
        "assignment_id": "binding-a",
        "target_type": "agent",
        "target_id": "agent-a",
        "project_ref": "",
        "effect": "include",
        "priority_override": 100,
    }]
