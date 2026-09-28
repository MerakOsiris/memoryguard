"""Mandatory injection budget: count is a warning, chars/tokens fail closed."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from memoryguard.context_bootstrap import build_context_packet
from memoryguard.host_hooks import _read_heartbeat, run_hook, set_hook_mode
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import settle_native_canonical_snapshot
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.context_budget import (
    ContextBudget,
    MANDATORY_ITEM_COUNT_WARNING,
    MANDATORY_ITEM_WARNING_THRESHOLD,
)
from memoryguard.runtime_v2.context_engine import ContextEngine
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.schema_v3 import MemoryKind, SharedMemoryStatus
from memoryguard.access_context import AccessContext
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.rule_scope import canonical_project_ref


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _engine_packet(bodies: list[str], *, budget: ContextBudget | None = None) -> dict:
    engine = ContextEngine(ready=True, state="V2_ACTIVE", budget=budget)
    return engine.bootstrap(
        {
            "task": "budget contract",
            "trusted_identity": {"agent": "agent-a", "group": "group-a"},
        },
        {
            "mandatory": [
                {
                    "item_id": f"m{index}",
                    "body": body,
                    "kind": "procedure",
                    "is_rule": True,
                }
                for index, body in enumerate(bodies)
            ]
        },
    ).to_dict()


def _count_warning(packet: dict) -> dict | None:
    warnings = (packet.get("budget") or {}).get("warnings") or []
    for item in warnings:
        if item.get("code") == MANDATORY_ITEM_COUNT_WARNING:
            return item
    return None


def _legacy_store(bodies: list[str], workspace: Path):
    records = [
        SimpleNamespace(
            memory_id=f"legacy-{index}",
            body=body,
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            priority=10,
            locked=False,
            confidence=0.9,
            created_at="2026-08-14T00:00:00+00:00",
            updated_at="2026-08-14T00:00:00+00:00",
            dedup_domain=f"always:{index}",
        )
        for index, body in enumerate(bodies)
    ]

    return SimpleNamespace(
        workspace=workspace,
        group_id="group-a",
        list_records=lambda: list(records),
        list_rule_assignments=lambda: [],
        get_active_version_id=lambda: "budget-contract",
    )


def _activate_v2(root: Path) -> GroupControlService:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    RuleV2Store(root)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="budget-contract")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="budget-source",
        target_digest="budget-target",
        manifest_digest="budget-manifest",
        digests={"validator_passed": True, "checkpoints": {"budget": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    return GroupControlService(root, write=True)


def _seed_atom(root: Path, group: str, agent: str, memory_id: str, body: str, *, policy: str = "always") -> None:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=canonical_project_ref(str(root.resolve())),
        provider="codex",
        runtime_role="root",
        actor=agent,
        admin=True,
        authority="manual",
    )
    governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind="procedure",
            injection_policy=policy,
            share_group_id=group,
            agent_instance_id=agent,
            project_ref=context.project_ref,
            provider="codex",
            runtime_role="root",
            workspace_id=str(root.resolve()),
        ),
        context=context,
        evidence=[{"source_ref": f"budget/{memory_id}"}],
        reason="budget contract fixture",
        idempotency_key=f"seed-{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("ready")


def test_twenty_one_and_twenty_five_short_rules_warn_without_truncation():
    for count in (21, 25):
        bodies = [f"keep unique deploy gate {index}" for index in range(count)]
        packet = _engine_packet(bodies)
        assert packet["status"] == "ok"
        assert packet["error"] == ""
        assert {item["body"] for item in packet["mandatory"]} == set(bodies)
        assert len(packet["mandatory"]) == count
        assert all(not item.get("truncated") for item in packet["mandatory"])
        warning = _count_warning(packet)
        assert warning is not None
        assert warning["count"] == count
        assert warning["threshold"] == MANDATORY_ITEM_WARNING_THRESHOLD
        assert warning["governance_action"] == "rule_merge"
        assert "健康阈值" in warning["message"]


def test_count_warning_is_isolated_per_agent_scope(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    for index in range(25):
        definition = store.upsert_definition(build_definition(
            f"agent a unique gate {index}", kind="procedure", rule_strength="must",
        ))
        store.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id="team",
            target_type="agent",
            target_id="a",
            owner_agent_id="a",
            binding_id=f"a-{index}",
        ))
    other = store.upsert_definition(build_definition(
        "agent b unique gate", kind="procedure", rule_strength="must",
    ))
    store.upsert_binding(build_binding(
        other.definition_id,
        share_group_id="team",
        target_type="agent",
        target_id="b",
        owner_agent_id="b",
        binding_id="b-first",
    ))
    for definition in store.list_definitions(status="active"):
        store.upsert_source_link(
            source_kind="test-governed-rule",
            share_group_id="team",
            memory_id=f"src:{definition.definition_id}",
            source_ref=f"ref:{definition.definition_id}",
            original_definition_id=definition.definition_id,
            canonical_definition_id=definition.definition_id,
            status="active",
        )
        store.record_evidence_ref({
            "evidence_id": f"ev:{definition.definition_id}",
            "definition_id": definition.definition_id,
            "source_rule_id": f"src:{definition.definition_id}",
            "share_group_id": "team",
            "evidence_ref": f"ref:{definition.definition_id}",
            "content_digest": definition.semantic_hash,
        })
    settle_native_canonical_snapshot(tmp_path, "team", store=store)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())

    def bootstrap(agent: str) -> dict:
        return port.dispatch_mcp(
            "memoryguard_context_bootstrap",
            {"task": "unrelated work"},
            context=bind_native_transport_context(
                AccessContext(
                    trusted_agent_id=agent,
                    is_admin=True,
                    strict_binding=True,
                    allow_anon=False,
                    session_id=f"budget-{agent}",
                    session_source="transport",
                    session_trusted=True,
                ),
                workspace_id=str(tmp_path.resolve()),
                share_group_id="team",
                project_ref="p",
                provider="codex",
                runtime_role="terra",
            ),
            generation=7,
            state="V2_ACTIVE",
        )

    a_result = bootstrap("a")
    assert a_result["ok"] is True
    a_packet = a_result["data"]
    assert len(a_packet["mandatory"]) == 25
    assert _count_warning(a_packet)["count"] == 25
    b_result = bootstrap("b")
    assert b_result["ok"] is True
    b_packet = b_result["data"]
    assert [item["body"] for item in b_packet["mandatory"]] == [other.canonical_text]
    assert _count_warning(b_packet) is None


def test_aggregate_overflow_returns_blocked_with_no_partial_packet():
    bodies = [
        f"{name} overflow gate " + (letter * 400)
        for name, letter in (("alpha", "A"), ("bravo", "B"), ("charlie", "C"))
    ]
    packet = _engine_packet(bodies)
    assert packet["status"] == "blocked"
    assert packet["error"] == "mandatory_budget_exceeded"
    assert packet["mandatory"] == []
    assert "unique overflow gate 0" not in str(packet)
    assert _count_warning(packet) is None


def test_sensitive_and_per_item_invalid_still_block():
    sensitive = _engine_packet(["sk-1234567890abcdef"])
    assert sensitive["status"] == "blocked"
    assert sensitive["error"] == "mandatory_sensitive_blocked"
    assert sensitive["mandatory"] == []
    assert "sk-1234567890abcdef" not in str(sensitive)

    oversized = _engine_packet(["y" * 801])
    assert oversized["status"] == "blocked"
    assert oversized["error"] == "mandatory_item_limit_exceeded"
    assert oversized["mandatory"] == []
    assert ("y" * 40) not in str(oversized)


def test_duplicate_consolidation_happens_before_warning_and_count():
    unique = [f"keep unique deploy gate {index}" for index in range(21)]
    duplicates = ["keep unique deploy gate 0"] * 8
    packet = _engine_packet(unique + duplicates)
    assert packet["status"] == "ok"
    assert len(packet["mandatory"]) == 21
    assert _count_warning(packet)["count"] == 21

    collapsed = _engine_packet(["keep the same deploy gate"] * 25)
    assert collapsed["status"] == "ok"
    assert len(collapsed["mandatory"]) == 1
    assert _count_warning(collapsed) is None


def test_legacy_and_context_engine_agree_on_count_warning(tmp_path: Path) -> None:
    bodies = [f"keep unique deploy gate {index}" for index in range(21)]
    native = _engine_packet(bodies)
    legacy = build_context_packet(
        _legacy_store(bodies, tmp_path),
        task="use default tools",
        max_items=12,
        max_chars=6000,
        read_path="legacy",
    )
    assert native["status"] == "ok"
    assert legacy["status"] == "ok"
    assert native["error"] == ""
    assert legacy["error"] == ""
    assert {item["body"] for item in native["mandatory"]} == set(bodies)
    assert {item["body"] for item in legacy["context_packet"]["mandatory_items"]} == set(bodies)
    assert len(native["mandatory"]) == len(legacy["context_packet"]["mandatory_items"]) == 21
    assert all(item.get("truncated") is False for item in legacy["context_packet"]["mandatory_items"])
    native_warning = _count_warning(native)
    legacy_warning = _count_warning(legacy)
    assert native_warning["code"] == legacy_warning["code"] == MANDATORY_ITEM_COUNT_WARNING
    assert native_warning["count"] == legacy_warning["count"] == 21
    assert native_warning["threshold"] == legacy_warning["threshold"] == 20


def test_cross_layer_same_body_still_coexists():
    packet = ContextEngine(ready=True, state="V2_ACTIVE").bootstrap(
        {
            "task": "release verification durable procedure",
            "trusted_identity": {"agent": "agent-a", "group": "group-a"},
        },
        {
            "mandatory": [{
                "item_id": "must",
                "body": "shared release process",
                "kind": "procedure",
                "is_rule": True,
                "injection_policy": "always",
            }],
            "relevant": [{
                "item_id": "obs",
                "body": "shared release process",
                "kind": "fact",
                "injection_policy": "relevant",
            }],
        },
    ).to_dict()
    assert [item["item_id"] for item in packet["mandatory"]] == ["must"]
    assert [item["item_id"] for item in packet["relevant"]] == ["obs"]
    assert _count_warning(packet) is None


def test_diagnostics_stay_open_and_high_risk_write_stays_closed_on_overflow(tmp_path: Path) -> None:
    workspace = tmp_path / "overflow-repair"
    workspace.mkdir()
    agent, group = "codex-agent", "overflow-rules"
    service = _activate_v2(workspace)
    service.bind_agent(agent, group, idempotency_key="bind-overflow")
    for index, (name, letter) in enumerate((("alpha", "A"), ("bravo", "B"), ("charlie", "C"), ("delta", "D"))):
        _seed_atom(
            workspace,
            group,
            agent,
            f"overflow-{index}",
            f"{name} overflow gate " + (letter * 400),
        )
    set_hook_mode(workspace, "codex", agent, "enforce")
    project_ref = canonical_project_ref(str(workspace.resolve()))
    payload = {
        "session_id": "overflow-repair",
        "cwd": str(workspace),
        "project_ref": project_ref,
    }
    prompt = run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={**payload, "prompt": "implement feature"},
    )
    assert "普通任务工具已阻断" in prompt["hookSpecificOutput"]["additionalContext"]
    assert "memoryguard_context_bootstrap" in prompt["hookSpecificOutput"]["additionalContext"]
    assert "停止继续执行" not in prompt["hookSpecificOutput"]["additionalContext"]

    diagnostic = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            **payload,
            "tool_name": "memoryguard_diagnostics_snapshot",
            "tool_input": {},
        },
    )
    assert diagnostic == {}

    denied_write = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            **payload,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {"text": "should stay blocked"},
        },
    )
    assert denied_write["hookSpecificOutput"]["permissionDecision"] == "deny"
    heartbeat = _read_heartbeat(workspace, "codex", agent)
    assert heartbeat["mandatory_overflow"] is True
