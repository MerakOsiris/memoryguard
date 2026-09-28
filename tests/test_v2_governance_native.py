from __future__ import annotations

from pathlib import Path
import sqlite3

from memoryguard.access_context import AccessContext
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom
from memoryguard.runtime_v2.governance_native import GovernanceNativeService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _mutation_context(workspace: Path) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="agent-a",
        authority="admin",
        admin=True,
    )


def _native_context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="governance-native-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-governance-native",
        sensitivity="normal",
        policy_class="private",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _seed(workspace: Path) -> GovernanceV2:
    boundary = GovernanceV2(workspace)
    context = _mutation_context(workspace)
    for memory_id, body, metadata in (
        ("quarantine-me", "private governed memory", {}),
        (
            "conflict-keep",
            "preferred fact",
            {
                "conflict_group_id": "conflict-group-1",
                "conflict_status": "unresolved",
                "conflict_reason": "same logical fact disagrees",
            },
        ),
        (
            "conflict-drop",
            "stale fact",
            {
                "conflict_group_id": "conflict-group-1",
                "conflict_status": "unresolved",
                "conflict_reason": "same logical fact disagrees",
            },
        ),
    ):
        boundary.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                share_group_id="group-a",
                agent_instance_id="agent-a",
                project_ref=str(workspace.resolve()),
                provider="gui",
                runtime_role="gui",
                metadata=metadata,
            ),
            context=context,
            evidence=[{
                "source_ref": f"test:{memory_id}",
                "digest": f"digest-{memory_id}",
            }],
            reason=f"seed {memory_id}",
        )
    return boundary


def test_governance_read_queries_are_zero_write_without_v2_state(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _native_context(tmp_path)
    before = list(tmp_path.rglob("*"))

    for name in (
        "get_recent_events",
        "get_auto_actions",
        "get_conflicts",
        "get_quarantine",
        "get_supersede_decisions",
        "get_memory_ir",
    ):
        result = port.dispatch_gui(
            name,
            [],
            context=context,
            generation=11,
            state="V2_ACTIVE",
        )
        assert result["ok"] is True, (name, result)

    assert list(tmp_path.rglob("*")) == before
    assert not (tmp_path / ".memoryguard").exists()


def test_governance_native_quarantine_conflict_and_decision_outbox(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    port = _port(tmp_path)
    context = _native_context(tmp_path)

    quarantined = port.dispatch_gui(
        "neuron_decide",
        ["quarantine-me", "quarantine", "manual review", True, None, "", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert quarantined["ok"] is True, quarantined
    assert quarantined["data"]["memory_status"] == "quarantined"

    queue = port.dispatch_gui(
        "get_quarantine", ["group-a"],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert queue["ok"] is True, queue
    assert queue["data"]["total"] == 1
    entry = queue["data"]["quarantine"][0]
    assert entry["memory_id"] == "quarantine-me"
    assert "private governed memory" not in entry["masked_preview"]

    released = port.dispatch_gui(
        "release_quarantine", [entry["quarantine_id"], "group-a"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert released["ok"] is True, released
    assert port.dispatch_gui(
        "get_quarantine", [], context=context, generation=11, state="V2_ACTIVE"
    )["data"]["total"] == 0

    conflicts = port.dispatch_gui(
        "get_conflicts", ["group-a"], context=context, generation=11, state="V2_ACTIVE"
    )
    assert conflicts["ok"] is True, conflicts
    conflict = conflicts["data"]["conflicts"][0]
    assert conflicts["data"]["total"] == 1
    assert conflicts["data"]["history_total"] == 1
    assert conflicts["data"]["actionable_total"] == 1
    assert conflict["group_id"] == "conflict-group-1"
    assert conflict["member_ids"] == ["conflict-drop", "conflict-keep"]
    assert conflict["status"] == "unresolved"
    assert conflict["reason"] == "同一事实的内容主张不一致，需选择保留版本"
    assert conflict["reason_code"] == "same logical fact disagrees"
    assert conflict["can_resolve"] is True
    assert conflict["selectable_member_ids"] == ["conflict-drop", "conflict-keep"]
    assert {item["memory_id"] for item in conflict["members"]} == {"conflict-drop", "conflict-keep"}
    assert all(item["selectable"] is True and item["preview"] for item in conflict["members"])

    resolved = port.dispatch_gui(
        "resolve_conflict", ["conflict-group-1", "conflict-keep", "group-a"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert resolved["ok"] is True, resolved
    assert resolved["data"]["deleted_memory_ids"] == ["conflict-drop"]
    assert port.dispatch_gui(
        "get_conflicts", [], context=context, generation=11, state="V2_ACTIVE"
    )["data"]["total"] == 0

    memory = boundary.memory
    read_scope = _mutation_context(tmp_path).to_dict()
    kept = memory.get_atom("conflict-keep", scope=read_scope, include_building=True)
    dropped = memory.get_atom("conflict-drop", scope=read_scope, include_building=True)
    released_atom = memory.get_atom("quarantine-me", scope=read_scope, include_building=True)
    assert kept is not None and kept.metadata.get("conflict_status") == "resolved"
    assert dropped is not None and dropped.status == "deleted"
    assert released_atom is not None and released_atom.status == "active"

    recent = port.dispatch_gui(
        "get_recent_events", [], context=context, generation=11, state="V2_ACTIVE"
    )
    assert recent["ok"] is True, recent
    events = recent["data"]["events"]
    actions = [item["action"] for item in events]
    assert "put" in actions
    assert "tombstone" in actions
    assert all(item["agent_instance_id"] == "agent-a" for item in events)
    assert all(item["share_group_id"] == "group-a" for item in events)
    assert all(item["provider"] == "gui" for item in events)

    service = GovernanceNativeService(tmp_path)
    outbox = service.outbox_status(context)
    assert outbox["ok"] is True
    assert outbox["outbox"].get("pending", 0) >= 1

    with sqlite3.connect(service._decision_ledger_path) as conn:
        decision_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        outbox_count = conn.execute("SELECT COUNT(*) FROM decision_outbox").fetchone()[0]
    assert decision_count == outbox_count


def test_conflict_snapshot_keeps_tombstone_history_and_blocks_stale_resolution(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    context = _native_context(tmp_path)
    mutation = _mutation_context(tmp_path)
    boundary.tombstone(
        "conflict-drop",
        context=mutation,
        reason="removed while reviewing conflict",
    )
    keeper = boundary.memory.get_atom(
        "conflict-keep", scope=mutation.to_dict(), include_building=True,
    )
    assert keeper is not None
    boundary.put_atom(
        MemoryAtom(
            **{
                **keeper.to_dict(),
                "metadata": {
                    **dict(keeper.metadata),
                    "conflict_peer_ids": ["conflict-drop", "missing-history-member"],
                },
            }
        ),
        context=mutation,
        reason="retain conflict peer history",
    )

    port = _port(tmp_path)
    conflicts = port.dispatch_gui(
        "get_conflicts", ["group-a"], context=context, generation=11, state="V2_ACTIVE",
    )
    assert conflicts["ok"] is True, conflicts
    conflict = conflicts["data"]["conflicts"][0]
    assert conflict["status"] == "stale"
    assert conflict["can_resolve"] is False
    assert conflict["live_member_count"] == 1
    assert conflicts["data"]["total"] == 1
    assert conflicts["data"]["unresolved_total"] == 1
    assert conflicts["data"]["history_total"] == 1
    assert conflicts["data"]["actionable_total"] == 0
    assert conflicts["data"]["selectable_total"] == 0
    assert conflicts["data"]["closable_stale_total"] == 1
    details = {item["memory_id"]: item for item in conflict["members"]}
    assert details["conflict-drop"]["status"] == "deleted"
    assert details["conflict-drop"]["selectable"] is False
    assert details["conflict-drop"]["history_available"] is True
    assert details["conflict-drop"]["preview"] == "stale fact"
    assert details["missing-history-member"]["snapshot_status"] == "snapshot_unavailable"
    assert details["missing-history-member"]["selectable"] is False
    assert "恢复" in details["missing-history-member"]["reason"]

    blocked = port.dispatch_gui(
        "resolve_conflict", ["conflict-group-1", "conflict-keep", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "conflict_group_stale"


def test_conflict_resolution_requires_explicit_confirmation_for_always_loser(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    port = _port(tmp_path)
    context = _native_context(tmp_path)

    # Promote the losing member through the existing governance update path;
    # the resolver must treat an always-injected rule as protected.
    promoted = port.dispatch_gui(
        "set_memory_injection_policy",
        {"memory_id": "conflict-drop", "injection_policy": "always", "priority": 20},
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert promoted["ok"] is True, promoted

    service = GovernanceNativeService(tmp_path)
    before_decisions = [item.decision_id for item in service._read_decisions()]
    before_atoms = {
        memory_id: boundary.memory.get_atom(
            memory_id,
            scope=_mutation_context(tmp_path).to_dict(),
            include_building=True,
        )
        for memory_id in ("conflict-keep", "conflict-drop")
    }

    for confirmed in (None, False):
        args = ["conflict-group-1", "conflict-keep", "group-a"]
        if confirmed is not None:
            args.append(confirmed)
        blocked = port.dispatch_gui(
            "resolve_conflict",
            args,
            context=context,
            generation=11,
            mutation=True,
            state="V2_ACTIVE",
        )
        assert blocked["ok"] is False
        assert blocked["error"] == "conflict_always_rule_protected"
    assert [item.decision_id for item in service._read_decisions()] == before_decisions
    for memory_id, before in before_atoms.items():
        after = boundary.memory.get_atom(
            memory_id,
            scope=_mutation_context(tmp_path).to_dict(),
            include_building=True,
        )
        assert after is not None and before is not None
        assert after.to_dict() == before.to_dict()


def test_conflict_resolution_rejects_confirmed_always_removal_without_admin(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    port = _port(tmp_path)
    admin_context = _native_context(tmp_path)
    promoted = port.dispatch_gui(
        "set_memory_injection_policy",
        {"memory_id": "conflict-drop", "injection_policy": "always", "priority": 20},
        context=admin_context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert promoted["ok"] is True, promoted

    non_admin_context = bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="governance-native-user-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        project_ref=str(tmp_path.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-governance-native",
        sensitivity="normal",
        policy_class="private",
    )
    service = GovernanceNativeService(tmp_path)
    before_decisions = [item.decision_id for item in service._read_decisions()]
    before_atoms = {
        memory_id: boundary.memory.get_atom(
            memory_id,
            scope=_mutation_context(tmp_path).to_dict(),
            include_building=True,
        )
        for memory_id in ("conflict-keep", "conflict-drop")
    }

    blocked = port.dispatch_gui(
        "resolve_conflict",
        ["conflict-group-1", "conflict-keep", "group-a", True],
        context=non_admin_context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )

    assert blocked["ok"] is False
    assert blocked["error"] == "admin_capability_required"
    assert [item.decision_id for item in service._read_decisions()] == before_decisions
    for memory_id, before in before_atoms.items():
        after = boundary.memory.get_atom(
            memory_id,
            scope=_mutation_context(tmp_path).to_dict(),
            include_building=True,
        )
        assert after is not None and before is not None
        assert after.to_dict() == before.to_dict()


def test_conflict_resolution_allows_confirmed_admin_to_remove_always_loser(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    port = _port(tmp_path)
    context = _native_context(tmp_path)

    promoted = port.dispatch_gui(
        "set_memory_injection_policy",
        {"memory_id": "conflict-drop", "injection_policy": "always", "priority": 20},
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert promoted["ok"] is True, promoted

    resolved = port.dispatch_gui(
        "resolve_conflict",
        ["conflict-group-1", "conflict-keep", "group-a", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert resolved["ok"] is True, resolved
    assert resolved["data"]["deleted_memory_ids"] == ["conflict-drop"]
    dropped = boundary.memory.get_atom(
        "conflict-drop",
        scope=_mutation_context(tmp_path).to_dict(),
        include_building=True,
    )
    assert dropped is not None and dropped.status == "deleted"


def test_conflict_snapshot_redacts_sensitive_body_and_localizes_reason(tmp_path: Path) -> None:
    boundary = GovernanceV2(tmp_path)
    mutation = _mutation_context(tmp_path)
    for memory_id, body in (
        ("secret-a", "api_key=supersecret123456"),
        ("secret-b", "a second conflicting claim"),
    ):
        boundary.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                share_group_id="group-a",
                agent_instance_id="agent-a",
                project_ref=str(tmp_path.resolve()),
                provider="gui",
                runtime_role="gui",
                metadata={
                    "conflict_group_id": "secret-conflict",
                    "conflict_status": "unresolved",
                    "conflict_reason": "canonical_composition_conflict",
                },
            ),
            context=mutation,
            evidence=[{"source_ref": f"test:{memory_id}"}],
            reason="seed sensitive conflict",
        )
    result = _port(tmp_path).dispatch_gui(
        "get_conflicts", ["group-a"], context=_native_context(tmp_path),
        generation=11, state="V2_ACTIVE",
    )
    conflict = next(item for item in result["data"]["conflicts"] if item["group_id"] == "secret-conflict")
    assert conflict["reason"] == "相关记忆的内容主张互相矛盾，需选择保留版本"
    assert conflict["reason_code"] == "canonical_composition_conflict"
    assert all("supersecret123456" not in item["preview"] for item in conflict["members"])


def test_stale_conflict_can_be_closed_with_an_auditable_mutation(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    mutation = _mutation_context(tmp_path)
    boundary.tombstone(
        "conflict-drop",
        context=mutation,
        reason="removed while reviewing conflict",
    )

    service = GovernanceNativeService(tmp_path)
    trusted = _native_context(tmp_path)

    before = service.conflicts(trusted)
    assert before["actionable_total"] == 0
    assert before["conflicts"][0]["status"] == "stale"

    # Exercise public GUI dispatch, including the legacy ``group_id`` alias.
    # The stale-close action must be reachable through the same registry and
    # trusted route used by the panel, not only through an internal service
    # call.
    closed = _port(tmp_path).dispatch_gui(
        "close_stale_conflict",
        [None, "conflict-group-1", "group-a"],
        context=trusted,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )

    assert closed["ok"] is True, closed
    assert closed["data"]["status"] == "closed"
    assert closed["data"]["closure_kind"] == "stale_unrecoverable"
    assert closed["data"]["closed_memory_ids"] == ["conflict-keep"]
    assert closed["data"]["decision_ids"]
    assert service.conflicts(trusted)["total"] == 0

    kept = boundary.memory.get_atom(
        "conflict-keep", scope=mutation.to_dict(), include_building=True,
    )
    assert kept is not None
    assert kept.metadata["conflict_status"] == "resolved"
    assert kept.metadata["conflict_resolution"] == "stale_closed"


def test_all_deleted_conflict_can_be_closed_without_resurrection(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    mutation = _mutation_context(tmp_path)
    for memory_id in ("conflict-keep", "conflict-drop"):
        boundary.tombstone(memory_id, context=mutation, reason="removed before review")

    service = GovernanceNativeService(tmp_path)
    closed = service.close_stale_conflict("conflict-group-1", _native_context(tmp_path))

    assert closed["status"] == "closed"
    assert service.conflicts(_native_context(tmp_path))["total"] == 0
    for memory_id in ("conflict-keep", "conflict-drop"):
        atom = boundary.memory.get_atom(
            memory_id, scope=mutation.to_dict(), include_building=True,
        )
        assert atom is not None and atom.status == "deleted"


def test_referenced_resolved_peer_is_readable_but_not_rewritten(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    mutation = _mutation_context(tmp_path)
    context = _native_context(tmp_path)
    port = _port(tmp_path)
    workspace = str(tmp_path.resolve())

    def put(memory_id, body, metadata, *, status="active", runtime_role="gui", share_group_id="group-a", agent="agent-a", injection_policy="relevant"):
        ctx = V2MutationContext(
            workspace_id=workspace,
            share_group_id=share_group_id,
            agent_instance_id=agent,
            project_ref=workspace,
            provider="gui",
            runtime_role=runtime_role,
            actor=agent,
            authority="admin",
            admin=True,
        )
        boundary.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                status=status,
                injection_policy=injection_policy,
                share_group_id=share_group_id,
                agent_instance_id=agent,
                project_ref=workspace,
                provider="gui",
                runtime_role=runtime_role,
                metadata=metadata,
            ),
            context=ctx,
            evidence=[{
                "source_ref": f"test:{share_group_id}:{memory_id}:{runtime_role}",
                "digest": f"digest-{share_group_id}-{memory_id}-{runtime_role}",
            }],
            reason=f"seed {memory_id}",
            idempotency_key=f"seed:{share_group_id}:{memory_id}:{runtime_role}:{status}",
        )

    put("peer-kept", "peer body stays", {
        "conflict_group_id": "conflict-old-resolved",
        "conflict_status": "resolved",
        "conflict_resolution": "kept",
    })
    put("candidate-keep", "candidate keep body", {
        "conflict_group_id": "conflict-new-keep",
        "conflict_status": "unresolved",
        "conflict_reason": "same logical fact disagrees",
        "conflict_peer_ids": ["peer-kept", "missing-peer-id", "other-group-secret"],
    }, status="conflicted")
    put("candidate-close", "candidate close body", {
        "conflict_group_id": "conflict-new-close",
        "conflict_status": "unresolved",
        "conflict_reason": "same logical fact disagrees",
        "conflict_peer_ids": ["peer-kept"],
    }, status="conflicted")
    put("other-group-secret", "secret other group", {}, share_group_id="group-b", agent="agent-b")
    put("peer-loser", "peer loser body", {
        "conflict_group_id": "conflict-old-loser",
        "conflict_status": "resolved",
        "conflict_resolution": "kept",
    })
    put("candidate-own", "candidate own body", {
        "conflict_group_id": "conflict-keep-candidate",
        "conflict_status": "unresolved",
        "conflict_reason": "same logical fact disagrees",
        "conflict_peer_ids": ["peer-loser"],
    }, status="conflicted")
    put("peer-always", "always peer body", {}, injection_policy="always")
    put("candidate-always", "always candidate", {
        "conflict_group_id": "conflict-always",
        "conflict_status": "unresolved",
        "conflict_peer_ids": ["peer-always"],
    }, status="conflicted")
    def listed_groups():
        result = port.dispatch_gui(
            "get_conflicts", ["group-a"], context=context, generation=11, state="V2_ACTIVE",
        )
        assert result["ok"] is True, result
        return {item["group_id"]: item for item in result["data"]["conflicts"]}, result

    groups, payload = listed_groups()
    assert "conflict-old-resolved" not in groups
    assert "secret other group" not in str(payload)
    current = groups["conflict-new-keep"]
    details = {item["memory_id"]: item for item in current["members"]}
    assert details["peer-kept"]["snapshot_status"] == "snapshot_available"
    assert details["peer-kept"]["preview"] == "peer body stays"
    assert details["peer-kept"]["missing"] is False
    assert details["missing-peer-id"]["missing"] is True
    assert details["missing-peer-id"]["snapshot_status"] == "snapshot_unavailable"
    assert details["other-group-secret"]["missing"] is True
    assert details["other-group-secret"]["snapshot_status"] == "snapshot_unavailable"
    assert current["can_resolve"] is True
    blocked = port.dispatch_gui(
        "close_stale_conflict", [None, "conflict-new-keep", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "conflict_group_actionable"
    protected = port.dispatch_gui(
        "resolve_conflict", ["conflict-always", "candidate-always", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert protected["ok"] is False
    assert protected["error"] == "conflict_always_rule_protected"
    always_peer = boundary.memory.get_atom("peer-always", scope=mutation.to_dict(), include_building=True)
    assert always_peer is not None and always_peer.status == "active"

    owned = port.dispatch_gui(
        "resolve_conflict", ["conflict-keep-candidate", "candidate-own", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert owned["ok"] is True, owned
    groups, _payload = listed_groups()
    assert "conflict-keep-candidate" not in groups
    loser = boundary.memory.get_atom("peer-loser", scope=mutation.to_dict(), include_building=True)
    assert loser is not None and loser.status == "deleted"
    kept_candidate = boundary.memory.get_atom("candidate-own", scope=mutation.to_dict(), include_building=True)
    assert kept_candidate is not None
    assert kept_candidate.metadata["conflict_status"] == "resolved"
    assert kept_candidate.metadata["conflict_resolution"] == "kept"

    resolved = port.dispatch_gui(
        "resolve_conflict", ["conflict-new-keep", "peer-kept", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert resolved["ok"] is True, resolved
    groups, _payload = listed_groups()
    assert "conflict-new-keep" not in groups
    peer = boundary.memory.get_atom("peer-kept", scope=mutation.to_dict(), include_building=True)
    assert peer is not None
    assert peer.body == "peer body stays"
    assert peer.status == "active"
    assert peer.metadata["conflict_group_id"] == "conflict-old-resolved"
    assert peer.metadata["conflict_status"] == "resolved"
    assert peer.metadata["conflict_resolution"] == "kept"
    peer_before = (peer.revision, peer.status, peer.body, dict(peer.metadata))

    boundary.tombstone("candidate-close", context=mutation, reason="candidate removed")
    closed = port.dispatch_gui(
        "close_stale_conflict", [None, "conflict-new-close", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert closed["ok"] is True, closed
    groups, _payload = listed_groups()
    assert "conflict-new-close" not in groups
    peer = boundary.memory.get_atom("peer-kept", scope=mutation.to_dict(), include_building=True)
    assert peer is not None
    assert (peer.revision, peer.status, peer.body, dict(peer.metadata)) == peer_before


def test_resolve_tombstone_failure_rolls_back_resolved_mark(tmp_path: Path, monkeypatch) -> None:
    from memoryguard.governance_v2.boundary import V2GovernanceBoundary

    boundary = _seed(tmp_path)
    mutation = _mutation_context(tmp_path)
    context = _native_context(tmp_path)
    workspace = str(tmp_path.resolve())
    boundary.put_atom(
        MemoryAtom(
            memory_id="peer-kept",
            body="peer body stays",
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=workspace,
            provider="gui",
            runtime_role="gui",
            metadata={
                "conflict_group_id": "conflict-old-resolved",
                "conflict_status": "resolved",
                "conflict_resolution": "kept",
            },
        ),
        context=mutation,
        evidence=[{"source_ref": "test:peer-kept", "digest": "digest-peer-kept"}],
        reason="seed peer",
        idempotency_key="seed:rollback:peer",
    )
    boundary.put_atom(
        MemoryAtom(
            memory_id="candidate-keep",
            body="candidate keep body",
            status="conflicted",
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=workspace,
            provider="gui",
            runtime_role="gui",
            metadata={
                "conflict_group_id": "conflict-new-keep",
                "conflict_status": "unresolved",
                "conflict_peer_ids": ["peer-kept"],
            },
        ),
        context=mutation,
        evidence=[{"source_ref": "test:candidate-keep", "digest": "digest-candidate-keep"}],
        reason="seed candidate",
        idempotency_key="seed:rollback:candidate",
    )

    def fail_tombstone(self, *args, **kwargs):
        raise RuntimeError("injected tombstone failure")

    monkeypatch.setattr(V2GovernanceBoundary, "tombstone", fail_tombstone)
    refused = _port(tmp_path).dispatch_gui(
        "resolve_conflict", ["conflict-new-keep", "peer-kept", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert refused["ok"] is False
    assert refused["error"] == "conflict_resolution_failed"
    candidate = boundary.memory.get_atom("candidate-keep", scope=mutation.to_dict(), include_building=True)
    peer = boundary.memory.get_atom("peer-kept", scope=mutation.to_dict(), include_building=True)
    assert candidate is not None and peer is not None
    assert candidate.status == "conflicted"
    assert candidate.metadata["conflict_status"] == "unresolved"
    assert "conflict_resolution" not in candidate.metadata
    assert peer.status == "active"
    assert peer.metadata["conflict_group_id"] == "conflict-old-resolved"
    assert peer.metadata["conflict_resolution"] == "kept"


def test_ambiguous_peer_blocks_resolve_and_close(tmp_path: Path, monkeypatch) -> None:
    boundary = _seed(tmp_path)
    mutation = _mutation_context(tmp_path)
    context = _native_context(tmp_path)
    workspace = str(tmp_path.resolve())
    boundary.put_atom(
        MemoryAtom(
            memory_id="candidate-ambiguous",
            body="ambiguous candidate",
            status="conflicted",
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=workspace,
            provider="gui",
            runtime_role="gui",
            metadata={
                "conflict_group_id": "conflict-ambiguous",
                "conflict_status": "unresolved",
                "conflict_peer_ids": ["ambiguous-peer", "missing-peer-id"],
            },
        ),
        context=mutation,
        evidence=[{"source_ref": "test:candidate-ambiguous", "digest": "digest-candidate-ambiguous"}],
        reason="seed ambiguous candidate",
        idempotency_key="seed:ambiguous:candidate",
    )
    original = GovernanceNativeService._conflict_sources

    def stub(self, trusted):
        atoms, buckets = original(self, trusted)
        shared = dict(
            memory_id="ambiguous-peer",
            status="active",
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=workspace,
            provider="gui",
            runtime_role="gui",
        )
        buckets["ambiguous-peer"] = [
            MemoryAtom(atom_id="atom-low", body="ambiguous low", **shared),
            MemoryAtom(atom_id="atom-high", body="ambiguous high", revision=9, **shared),
        ]
        return atoms, buckets

    monkeypatch.setattr(GovernanceNativeService, "_conflict_sources", stub)
    port = _port(tmp_path)
    listed = port.dispatch_gui(
        "get_conflicts", ["group-a"], context=context, generation=11, state="V2_ACTIVE",
    )
    assert listed["ok"] is True, listed
    current = {item["group_id"]: item for item in listed["data"]["conflicts"]}["conflict-ambiguous"]
    details = {item["memory_id"]: item for item in current["members"]}
    assert details["ambiguous-peer"]["status"] == "ambiguous"
    assert details["ambiguous-peer"]["selectable"] is False
    assert details["missing-peer-id"]["missing"] is True
    assert details["missing-peer-id"]["snapshot_status"] == "snapshot_unavailable"
    assert current["can_resolve"] is False
    assert current["status"] == "ambiguous"
    refused = port.dispatch_gui(
        "resolve_conflict", ["conflict-ambiguous", "candidate-ambiguous", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert refused["ok"] is False
    assert refused["error"] == "conflict_member_ambiguous"
    closed = port.dispatch_gui(
        "close_stale_conflict", [None, "conflict-ambiguous", "group-a"],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert closed["ok"] is False
    assert closed["error"] == "conflict_member_ambiguous"
    candidate = boundary.memory.get_atom(
        "candidate-ambiguous", scope=mutation.to_dict(), include_building=True,
    )
    assert candidate is not None
    assert candidate.status == "conflicted"
    assert candidate.metadata["conflict_status"] == "unresolved"
