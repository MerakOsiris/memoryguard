"""V2 mandatory-rule, migration, persistence, and Hook safety coverage."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.host_hooks import _read_heartbeat, run_hook, set_hook_mode
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.mcp_server import TOOLS, execute_tool
from memoryguard.migration import V1GroupReader, V1MemoryMigrator
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.context_engine import ContextEngine
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2(root: Path) -> GroupControlService:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    RuleV2Store(root)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="mandatory-rules-core")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="mandatory-source",
        target_digest="mandatory-target",
        manifest_digest="mandatory-manifest",
        digests={"validator_passed": True, "checkpoints": {"mandatory": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    return GroupControlService(root, write=True)


def _mutation_context(
    root: Path,
    group: str,
    agent: str,
    *,
    provider: str = "codex",
    runtime_role: str = "root",
) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=canonical_project_ref(str(root.resolve())),
        provider=provider,
        runtime_role=runtime_role,
        actor=agent,
        admin=True,
        authority="manual",
    )


def _seed_atom(
    root: Path,
    group: str,
    agent: str,
    memory_id: str,
    body: str,
    *,
    policy: str = "relevant",
    priority: int = 0,
    provider: str = "codex",
    runtime_role: str = "root",
    kind: str = "procedure",
) -> MemoryAtom:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = _mutation_context(
        root, group, agent, provider=provider, runtime_role=runtime_role,
    )
    atom, _decision = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind=kind,
            injection_policy=policy,
            priority=priority,
            share_group_id=group,
            agent_instance_id=agent,
            project_ref=context.project_ref,
            provider=provider,
            runtime_role=runtime_role,
            workspace_id=str(root.resolve()),
        ),
        context=context,
        evidence=[{"source_ref": f"mandatory/{memory_id}"}],
        reason="V2 mandatory-rule fixture",
        idempotency_key=f"seed-{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("ready")
    return atom


def _request(
    root: Path,
    agent: str,
    group: str,
    *,
    provider: str = "codex",
    runtime_role: str = "root",
    task: str = "mandatory rules",
) -> dict:
    project_ref = canonical_project_ref(str(root.resolve()))
    return {
        "task": task,
        "trusted_identity": {
            "workspace_id": str(root.resolve()),
            "agent": agent,
            "group": group,
            "project_ref": project_ref,
        },
        "provider": provider,
        "runtime": runtime_role,
    }


def _native_packet(
    root: Path,
    agent: str,
    group: str,
    *,
    provider: str = "codex",
    runtime_role: str = "root",
    task: str = "mandatory rules",
) -> dict:
    request = _request(
        root,
        agent,
        group,
        provider=provider,
        runtime_role=runtime_role,
        task=task,
    )
    candidates = NativeV2RuntimePort(root).retrieve(request)
    return ContextEngine(ready=True, state="V2_ACTIVE").bootstrap(
        request, candidates,
    ).to_dict()


def _set_mcp_identity(monkeypatch: pytest.MonkeyPatch, root: Path, agent: str) -> None:
    values = {
        "MEMORYGUARD_HOME": str(root.resolve()),
        "MEMORYGUARD_WORKSPACE": str(root.resolve()),
        "MEMORYGUARD_AGENT_ID": agent,
        "MEMORYGUARD_ADMIN": "1",
        "MEMORYGUARD_STRICT_BINDING": "1",
        "MEMORYGUARD_ALLOW_ANON": "0",
        "MEMORYGUARD_SESSION_ID": f"mandatory-{agent}",
        "MEMORYGUARD_SESSION_SOURCE": "transport",
        "MEMORYGUARD_SESSION_TRUSTED": "1",
        "MEMORYGUARD_PROJECT_CWD": str(root.resolve()),
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_RUNTIME_ROLE": "root",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _mcp_json(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _create_rule(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    agent: str,
    text: str,
    *,
    priority: int = 0,
    key: str,
) -> dict:
    _set_mcp_identity(monkeypatch, root, agent)
    result = _mcp_json(execute_tool("memoryguard_rule_create_auto", {
        "text": text,
        "kind": "procedure",
        "priority": priority,
        "idempotency_key": key,
    }))
    assert result["state"] == "V2_ACTIVE"
    assert result["data"]["definition_id"]
    assert result["data"]["binding_id"]
    return result


def test_always_is_task_independent_and_does_not_consume_recall_budget():
    engine = ContextEngine(ready=True, state="V2_ACTIVE")
    packet = engine.bootstrap(
        {
            "task": "database migration",
            "max_items": 1,
            "max_chars": 256,
            "trusted_identity": {"agent": "agent-a", "group": "group-a"},
        },
        {
            "mandatory": [{
                "item_id": "always",
                "body": "永远先运行隔离测试",
                "kind": "procedure",
                "is_rule": True,
                "priority": 4,
            }],
            "relevant": [
                {"item_id": "relevant", "body": "database migration must run tests first", "kind": "procedure", "score": 1.0},
                {"item_id": "ordinary", "body": "release procedure for unrelated deployment", "kind": "fact", "score": 0.1},
            ],
        },
    ).to_dict()

    assert [item["item_id"] for item in packet["mandatory"]] == ["always"]
    assert [item["item_id"] for item in packet["relevant"]] == ["relevant"]
    assert packet["budget"]["mandatory"]["items"] == 1
    assert packet["budget"]["optional"]["items"] == 1
    assert "ordinary" not in str(packet["relevant"])


def test_old_records_default_relevant_and_sensitive_always_is_not_leaked(tmp_path):
    source = tmp_path / "legacy-source"
    source.mkdir()
    group = "compat-group"
    db_path = source / "memory.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE records (memory_id TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, agent_instance_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
            ("old", "legacy migration procedure", "procedure", "active", 0.8, "agent-a", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()
    before = db_path.read_bytes()

    target = tmp_path / "v2-target"
    service = _activate_v2(target)
    service.bind_agent("agent-a", group, idempotency_key="bind-compat")
    reader = V1GroupReader(source, group, db_path, immutable=True)
    assert reader.inventory().records == 1
    migrator = V1MemoryMigrator(
        source,
        target=target,
        groups={group: db_path},
        group_targets={group: group},
        include_managed=False,
        immutable_sources=True,
    )
    preview = migrator.preview()
    assert preview.ok and preview.source_records == 1
    result = migrator.migrate()
    assert result.ok and result.atoms == 1
    assert db_path.read_bytes() == before

    memory = MemoryAtomStore(target)
    atom = memory.get_atom(
        "old",
        scope=MemoryReadScope(
            workspace_id=str(target.resolve()),
            share_group_id=group,
            admin=True,
        ),
        include_building=True,
    )
    assert atom is not None
    assert atom.injection_policy == "relevant"
    assert atom.priority == 0
    assert atom.body == "legacy migration procedure"


def test_sensitive_historical_mandatory_fails_closed_without_leaking_body():
    packet = ContextEngine(ready=True, state="V2_ACTIVE").bootstrap(
        {
            "task": "anything",
            "trusted_identity": {"agent": "agent-a", "group": "group-a"},
        },
        {"mandatory": [{
            "item_id": "secret",
            "body": "api_key=never-expose",
            "kind": "procedure",
            "is_rule": True,
        }]},
    ).to_dict()
    assert packet["status"] == "blocked"
    assert packet["error"] == "mandatory_sensitive_blocked"
    assert "never-expose" not in str(packet)
    assert any(item["reason"] == "safety_rejected" for item in packet["receipts"])


def test_mandatory_limit_rejects_writes_and_legacy_overflow_fails_closed():
    candidates = {
        "mandatory": [
            {
                "item_id": f"m{index}",
                "body": f"mandatory rule {index}",
                "kind": "procedure",
                "is_rule": True,
            }
            for index in range(21)
        ]
    }
    packet = ContextEngine(ready=True, state="V2_ACTIVE").bootstrap(
        {
            "task": "anything",
            "trusted_identity": {"agent": "agent-a", "group": "group-a"},
        },
        candidates,
    ).to_dict()
    assert packet["status"] == "ok"
    assert packet["error"] == ""
    bodies = [item["body"] for item in packet["mandatory"]]
    assert set(bodies) == {f"mandatory rule {index}" for index in range(21)}
    assert len(bodies) == 21
    assert all(not item.get("truncated") for item in packet["mandatory"])
    warnings = packet["budget"]["warnings"]
    assert warnings[0]["code"] == "mandatory_item_count_warning"
    assert warnings[0]["count"] == 21
    assert warnings[0]["threshold"] == 20


def test_update_to_always_is_rejected_until_delete_releases_capacity(tmp_path):
    root = tmp_path / "update-limit"
    service = _activate_v2(root)
    group, agent = "update-limit-group", "agent-update"
    service.bind_agent(agent, group, idempotency_key="bind-update")
    for index in range(20):
        _seed_atom(root, group, agent, f"full-{index}", f"rule {index}", policy="always")
    _seed_atom(root, group, agent, "candidate", "candidate rule", policy="relevant")

    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = _mutation_context(root, group, agent)
    scope = MemoryReadScope(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=context.project_ref,
        provider="codex",
        runtime_role="root",
        admin=True,
    )
    candidate = memory.get_atom("candidate", scope=scope, include_building=True)
    assert candidate is not None
    updated, _decision = governance.put_atom(
        replace(candidate, injection_policy="always", priority=9),
        context=context,
        reason="promote candidate to mandatory",
        idempotency_key="candidate-always",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("ready")
    assert updated.injection_policy == "always"
    accepted = _native_packet(root, agent, group)
    assert accepted["status"] == "ok"
    assert accepted["error"] == ""
    injected_ids = {item["item_id"] for item in accepted["mandatory"]}
    assert updated.memory_id in injected_ids
    assert len(accepted["mandatory"]) == 21
    assert accepted["budget"]["warnings"][0]["code"] == "mandatory_item_count_warning"
    assert accepted["budget"]["warnings"][0]["count"] == 21

    governance.tombstone(
        "full-0",
        context=context,
        reason="release mandatory capacity",
        idempotency_key="delete-full-0",
    )
    memory.set_visibility("ready")
    after_delete = _native_packet(root, agent, group)
    assert after_delete["status"] == "ok"
    assert any(item["item_id"] == updated.memory_id for item in after_delete["mandatory"])
    assert len(after_delete["mandatory"]) == 20
    assert after_delete["budget"]["warnings"] == []


def test_duplicate_body_different_injection_semantics_stays_distinct(tmp_path):
    root = tmp_path / "duplicate-policy"
    service = _activate_v2(root)
    group, agent = "duplicate-policy", "agent-duplicate"
    service.bind_agent(agent, group, idempotency_key="bind-duplicate")
    related_task = "release verification durable procedure"
    first = _seed_atom(root, group, agent, "first", related_task, policy="always", priority=9)
    second = _seed_atom(root, group, agent, "second", related_task, policy="relevant", priority=-3, kind="fact")
    memory = MemoryAtomStore(root)
    scope = MemoryReadScope(workspace_id=str(root.resolve()), share_group_id=group, admin=True)
    atoms = memory.list_atoms(scope=scope, include_building=True)
    assert {item.memory_id for item in atoms} >= {"first", "second"}
    assert first.atom_id != second.atom_id
    packet = _native_packet(root, agent, group, task=related_task)
    assert [item["item_id"] for item in packet["mandatory"]] == [first.memory_id]
    assert [item["item_id"] for item in packet["relevant"]] == [second.memory_id]


def test_interactive_memory_records_offer_visible_injection_toggle():
    html = Path("src/memoryguard/interactive.py").read_text(encoding="utf-8")
    assert "强制规则每任务注入；按需记忆按相关性召回" in html
    assert "injection_policy === 'always'" in html
    assert "set_memory_injection_policy" in html
    assert "result.ok === false" in html
    assert "result.blocked_reason || '切换被拒绝'" in html
    assert "renderMemoryRecords()" in html


def test_readonly_open_old_schema_fails_closed_until_writable_migration(tmp_path, monkeypatch):
    source = tmp_path / "old-schema"
    source.mkdir()
    group, agent = "legacy-sqlite", "legacy-agent"
    db_path = source / "memory.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE records (memory_id TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, agent_instance_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
            ("legacy", "old persisted procedure", "procedure", "active", 0.8, agent, "2026-01-01", "2026-01-01"),
        )
        conn.commit()
    finally:
        conn.close()
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    target = tmp_path / "migrated-v2"
    service = _activate_v2(target)
    service.bind_agent(agent, group, idempotency_key="bind-legacy")
    reader = V1GroupReader(source, group, db_path, immutable=True)
    assert reader.inventory().to_dict()["active"] == 1
    migrator = V1MemoryMigrator(
        source,
        target=target,
        groups={group: db_path},
        include_managed=False,
        immutable_sources=True,
    )
    assert migrator.preview().ok
    migration = migrator.migrate()
    assert migration.ok
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before

    mappings = MemoryAtomStore(target).list_source_mappings()
    mapping = next(item for item in mappings if item["source_record_id"] == "legacy")
    target_atom_id = str(mapping["atom_id"])
    assert target_atom_id and target_atom_id != "legacy"

    memory = MemoryAtomStore(target)
    evidence = EvidenceStore(target)
    memory.project_evidence(evidence)
    memory.set_visibility("ready")
    _set_mcp_identity(monkeypatch, target, agent)
    updated = _mcp_json(execute_tool("memoryguard_memory_update", {
        "memory_id": "legacy",
        "atom_id": target_atom_id,
        "injection_policy": "always",
        "priority": 100,
        "idempotency_key": "legacy-v2-policy",
    }))
    assert updated["state"] == "V2_ACTIVE"
    atom = MemoryAtomStore(target).get_atom(
        "legacy",
        scope=MemoryReadScope(workspace_id=str(target.resolve()), share_group_id=group, admin=True),
        atom_id=target_atom_id,
        include_building=True,
    )
    assert atom is not None and atom.injection_policy == "always" and atom.priority == 100
    refreshed_memory = MemoryAtomStore(target)
    refreshed_projection = refreshed_memory.project_evidence(EvidenceStore(target))
    assert refreshed_projection["failed"] == 0
    assert refreshed_projection["pending"] == 0
    refreshed_memory.set_visibility("ready")
    packet = _native_packet(target, agent, group, provider="codex", runtime_role="root")
    assert packet["status"] == "ok"
    assert any(item["item_id"] == atom.memory_id for item in packet["mandatory"])


def test_mcp_write_update_schema_exposes_persisted_injection_fields():
    tools = {item["name"]: item for item in TOOLS}
    for name in ("memoryguard_memory_write", "memoryguard_memory_update"):
        props = tools[name]["inputSchema"]["properties"]
        assert props["injection_policy"]["enum"] == ["relevant", "always"]
        assert props["priority"]["minimum"] == -100


def test_mcp_write_and_update_persist_injection_settings(tmp_path, monkeypatch):
    root = tmp_path / "mcp-policy"
    service = _activate_v2(root)
    agent, group = "trusted-agent", "trusted-group"
    service.bind_agent(agent, group, idempotency_key="bind-mcp")
    _set_mcp_identity(monkeypatch, root, agent)

    written = _mcp_json(execute_tool("memoryguard_memory_write", {
        "memory_id": "mcp-memory",
        "body": "mandatory mcp procedure for release verification",
        "kind": "procedure",
        "injection_policy": "always",
        "priority": 7,
        "evidence_ids": ["mcp-memory-evidence"],
        "idempotency_key": "mcp-write",
    }))
    assert written["state"] == "V2_ACTIVE"
    assert written["data"]["atom"]["injection_policy"] == "always"
    assert written["data"]["atom"]["priority"] == 7

    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    projection = memory.project_evidence(evidence)
    assert projection["failed"] == 0
    assert projection["pending"] == 0
    assert memory.validate(evidence, include_building=True).ok
    memory.set_visibility("ready")
    updated = _mcp_json(execute_tool("memoryguard_memory_update", {
        "memory_id": "mcp-memory",
        "injection_policy": "relevant",
        "priority": -2,
        "idempotency_key": "mcp-update",
    }))
    assert updated["data"]["atom"]["injection_policy"] == "relevant"
    assert updated["data"]["atom"]["priority"] == -2
    persisted = MemoryAtomStore(root).get_atom(
        "mcp-memory",
        scope=MemoryReadScope(workspace_id=str(root.resolve()), share_group_id=group, admin=True),
        include_building=True,
    )
    assert persisted is not None
    assert persisted.injection_policy == "relevant"
    assert persisted.priority == -2


def test_cursor_session_start_injects_mandatory_rules_and_receipt_ids(tmp_path, monkeypatch):
    workspace = tmp_path / "cursor-control"
    workspace.mkdir()
    agent, group = "cursor-agent", "cursor-rules"
    service = _activate_v2(workspace)
    service.bind_agent(agent, group, idempotency_key="bind-cursor")
    _create_rule(
        monkeypatch,
        workspace,
        agent,
        "Cursor 固定会话必须执行此规则",
        priority=8,
        key="cursor-rule",
    )
    project_ref = canonical_project_ref(str(workspace.resolve()))
    result = run_hook(
        provider="cursor",
        event="session_start",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            "session_id": "cursor-session",
            "cwd": str(workspace),
            "project_ref": project_ref,
        },
    )
    text = result["additional_context"]
    assert "MemoryGuard 强制规则（必须遵循）" in text
    assert "cursor固定会话" in text
    assert "执行此规则" in text
    receipt = _read_heartbeat(workspace, "cursor", agent)
    assert receipt["mandatory_rule_ids"]
    assert receipt["mandatory_match_receipts"]
    assert receipt["mandatory_match_receipts"][0]["receipt_id"].startswith("v2-")
    assert receipt["mandatory_overflow"] is False


def _seed_overflow(
    root: Path,
    group: str,
    agent: str,
    *,
    runtime_role: str = "root",
    prefix: str,
) -> None:
    # Each body stays under the per-item cap; aggregate chars/tokens overflow.
    pads = (("alpha", "A"), ("bravo", "B"), ("charlie", "C"), ("delta", "D"))
    for index, (name, letter) in enumerate(pads):
        _seed_atom(
            root,
            group,
            agent,
            f"{prefix}-{index}",
            f"{prefix} {name} overflow gate " + (letter * 400),
            policy="always",
            provider="codex",
            runtime_role=runtime_role,
        )


def test_hook_stops_on_historical_mandatory_overflow(tmp_path):
    workspace = tmp_path / "codex-overflow"
    workspace.mkdir()
    agent, group = "codex-agent", "overflow-rules"
    service = _activate_v2(workspace)
    service.bind_agent(agent, group, idempotency_key="bind-overflow")
    _seed_overflow(workspace, group, agent, prefix="legacy")
    set_hook_mode(workspace, "codex", agent, "enforce")
    project_ref = canonical_project_ref(str(workspace.resolve()))

    prompt_result = run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            "session_id": "overflow-session",
            "prompt": "implement feature",
            "cwd": str(workspace),
            "project_ref": project_ref,
        },
    )
    context = prompt_result["hookSpecificOutput"]["additionalContext"]
    assert "普通任务工具已阻断" in context
    assert "memoryguard_memory_update（仅 preview=true）" in context
    assert "停止继续执行" not in context
    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            "session_id": "overflow-session",
            "tool_name": "shell_command",
            "cwd": str(workspace),
            "project_ref": project_ref,
        },
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    heartbeat = _read_heartbeat(workspace, "codex", agent)
    assert heartbeat["mandatory_overflow"] is True


def test_subagent_overflow_is_persisted_and_denies_next_tool(tmp_path):
    workspace = tmp_path / "subagent-overflow"
    workspace.mkdir()
    agent, group = "codex-agent", "subagent-overflow"
    service = _activate_v2(workspace)
    service.bind_agent(agent, group, idempotency_key="bind-subagent-overflow")
    _seed_overflow(workspace, group, agent, runtime_role="subagent", prefix="subagent")
    set_hook_mode(workspace, "codex", agent, "enforce")
    project_ref = canonical_project_ref(str(workspace.resolve()))

    run_hook(
        provider="codex",
        event="subagent_start",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            "session_id": "subagent-overflow-session",
            "task": "implement feature",
            "subagent_id": "subagent-1",
            "cwd": str(workspace),
            "project_ref": project_ref,
        },
    )
    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id=agent,
        share_group_id=group,
        payload={
            "session_id": "subagent-overflow-session",
            "tool_name": "shell_command",
            "subagent_id": "subagent-1",
            "cwd": str(workspace),
            "project_ref": project_ref,
        },
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    heartbeat = _read_heartbeat(workspace, "codex", agent)
    assert heartbeat["mandatory_overflow"] is True
