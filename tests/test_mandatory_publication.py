"""Replacement budgets and recovery must never publish a half-updated set."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import multiprocessing

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.governance_v2 import V2MutationContext
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 1}


def context(root, *, agent="owner", project="project-a", runtime="root", admin=False):
    return bind_native_transport_context(
        AccessContext(trusted_agent_id=agent, is_admin=admin, strict_binding=True,
                      allow_anon=False, session_id="publication-test", session_source="transport", session_trusted=True),
        workspace_id=str(root), share_group_id="team", project_ref=project,
        provider="codex", runtime_role=runtime,
    )


@pytest.fixture
def port(tmp_path):
    MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    return NativeV2RuntimePort(tmp_path, state_provider=Manifest())


def call(port, root, operation, payload, **scope):
    return port.dispatch_mcp("memoryguard_" + operation, payload, context=context(root, **scope), generation=1)


def write(port, root, memory_id, body, **extra):
    result = call(port, root, "memory_write", {
        "memory_id": memory_id, "body": body, "kind": "procedure", "injection_policy": "always", **extra,
    })
    assert result["ok"], result
    return result["data"]["atom"]


def body(name, length):
    return name + " " + name[0].upper() * (length - len(name) - 1)


def package(port, root, **scope):
    result = call(port, root, "context_bootstrap", {"task": "unrelated work"}, **scope)
    assert result["ok"], result
    return result["data"]


def persisted_state(root):
    """Test fixture DBs only: compare all durable domain/receipt rows."""
    result = {}
    for path in sorted((root / ".memoryguard").rglob("*.db")):
        with sqlite3.connect(path) as conn:
            result[str(path.relative_to(root))] = tuple(conn.iterdump())
    return result


def seed_historical(root, memory_id, text, *, agent="owner", project="project-a", runtime="root", supersedes=()):
    """Old stored states are seeded through governance, never live user data."""
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    ctx = V2MutationContext(workspace_id=str(root), share_group_id="team", agent_instance_id=agent,
                            project_ref=project, provider="codex", runtime_role=runtime,
                            actor="historical-fixture", admin=True)
    atom, _ = governance.put_atom(MemoryAtom(
        memory_id=memory_id, body=text, kind="procedure", injection_policy="always",
        workspace_id=str(root), share_group_id="team", agent_instance_id=agent,
        project_ref=project, provider="codex", runtime_role=runtime, supersedes=list(supersedes),
        metadata={"owner_agent_id": agent, "audience": {
            "source": "native_v2", "target_type": "agent", "target_id": agent,
        }},
    ), context=ctx, evidence=[{"source_ref": f"fixture:{memory_id}"}], idempotency_key=f"seed:{memory_id}")
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[atom.atom_id])
    return atom


def test_gui_canonical_body_recovery_versions_budget_and_scope(port, tmp_path):
    from memoryguard.rule_binding import build_binding
    from memoryguard.rule_definition import build_definition
    from memoryguard.rules.v2_store import RuleV2Store
    from memoryguard.rule_reconciliation import settle_native_canonical_snapshot

    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition(body("overflow", 1050), kind="procedure", rule_strength="must"))
    did = definition.definition_id
    rules.upsert_binding(build_binding(did, share_group_id="team", target_type="group", target_id="team", owner_agent_id="owner"))
    rules.upsert_source_link(source_kind="fixture", share_group_id="team", memory_id="source", source_ref="fixture:source",
                             original_definition_id=did, canonical_definition_id=did, status="active")
    rules.record_evidence_ref({"evidence_id": "original-evidence", "definition_id": did,
        "source_rule_id": "source", "share_group_id": "team", "evidence_ref": "fixture:source",
        "content_digest": definition.semantic_hash})
    settle_native_canonical_snapshot(tmp_path, "team", store=rules)
    assert not call(port, tmp_path, "context_bootstrap", {"task": "unrelated work"})["ok"]

    def edit(text, revision=1, admin=True):
        return port.dispatch_gui("update_rule_body", {"definition_id": did, "body": text, "expected_revision": revision},
                                 context=context(tmp_path, admin=admin), generation=1)

    before = persisted_state(tmp_path)
    assert edit("Safe shorter rule.", admin=False)["code"] == "admin_capability_required"
    assert edit("Safe shorter rule.", revision=9)["code"] == "revision_conflict"
    assert persisted_state(tmp_path) == before
    result = edit("Safe shorter rule.")
    assert result["ok"], result
    assert result["data"]["budget"]["packages_checked"] > 0
    for agent in ("owner", "peer"):
        assert [item["body"] for item in package(port, tmp_path, agent=agent)["mandatory"]] == ["Safe shorter rule."]
    current = rules.get_definition(did)
    assert current.revision == 2
    assert rules._read(lambda conn: conn.execute("SELECT content_digest FROM rule_evidence_refs WHERE evidence_id='original-evidence'").fetchone()[0]) == definition.semantic_hash
    assert rules._read(lambda conn: conn.execute("SELECT COUNT(*) FROM rule_definition_versions WHERE definition_id=?", (did,)).fetchone()[0]) >= 1
    before = persisted_state(tmp_path)
    assert edit(body("overflow", 1050), revision=2)["code"] == "mandatory_item_limit_exceeded"
    assert persisted_state(tmp_path) == before
    rules.upsert_binding(build_binding(did, share_group_id="other", target_type="group", target_id="other", owner_agent_id="other"))
    before = persisted_state(tmp_path)
    assert edit("Must not affect another group.", revision=2)["code"] == "rule_shared_definition_requires_scoped_replacement"
    assert persisted_state(tmp_path) == before


def test_publication_reuses_catalog_but_checks_each_scope_and_refreshes_next_request(port, tmp_path, monkeypatch):
    from memoryguard.rule_binding import build_binding
    from memoryguard.rule_definition import build_definition
    from memoryguard.rules.v2_store import RuleV2Store
    import memoryguard.rule_reconciliation as reconciliation

    original = seed_historical(tmp_path, "alpha", "Use release checks.")
    seed_historical(tmp_path, "peer", "Use peer checks.", agent="peer", project="project-b", runtime="worker")
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition("Canonical release rule.", kind="procedure", rule_strength="must"))
    rules.upsert_binding(build_binding(definition.definition_id, share_group_id="team", target_type="agent", target_id="owner", owner_agent_id="owner"))
    rules.upsert_binding(build_binding(definition.definition_id, share_group_id="team", target_type="project", target_id="project-b", project_ref="project-b", effect="exclude", owner_agent_id="owner"))

    calls = []
    real_status = reconciliation.canonical_reconciliation_status

    def counted(*args, **kwargs):
        calls.append(1)
        return real_status(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "canonical_reconciliation_status", counted)
    request = {"memory_id": "alpha", "body": original.body, "expected_revision": original.revision,
               "idempotency_key": "catalog-first", "reason": "catalog regression", "preview": True}
    first = call(port, tmp_path, "memory_update", request)
    assert first["ok"], first
    assert first["data"]["budget"]["packages_checked"] == 2  # included and excluded scopes
    assert len(calls) <= 4  # not once per Cartesian coordinate

    # A new request must observe catalog changes even on the same runtime port.
    large = rules.upsert_definition(build_definition("Canonical extra " + "Z" * 1000, kind="procedure", rule_strength="must"))
    rules.upsert_binding(build_binding(large.definition_id, share_group_id="team", target_type="project", target_id="project-b", project_ref="project-b", owner_agent_id="owner"))
    before = persisted_state(tmp_path)
    second = call(port, tmp_path, "memory_update", {**request, "idempotency_key": "catalog-second"})
    assert second["code"] == "mandatory_item_limit_exceeded", second
    assert persisted_state(tmp_path) == before


def test_repeated_explicit_replacement_keeps_one_identity_and_exact_body(port, tmp_path):
    original = write(port, tmp_path, "deploy", "Use alpha deployment checks.")
    for index, text in enumerate(("Use beta deployment checks.", "Check deploy.", "Use gamma deployment checks.")):
        result = call(port, tmp_path, "memory_update", {
            "memory_id": "deploy", "body": text, "idempotency_key": f"replace-{index}",
        }, project=f"project-{index}", runtime=f"worker-{index}")
        assert result["ok"], result
        assert result["data"]["atom"]["atom_id"] == original["atom_id"]
        assert [item["body"] for item in package(port, tmp_path)["mandatory"]] == [text]


def test_same_audience_dedup_ignores_task_and_model_provenance(port, tmp_path):
    payload = {"body": "Always verify release approval.", "kind": "procedure", "injection_policy": "always"}
    ids = []
    for index in range(4):
        result = call(port, tmp_path, "memory_write", {**payload, "idempotency_key": f"same-{index}"},
                      project=f"project-{index}", runtime=f"model-{index}")
        assert result["ok"], result
        ids.append(result["data"]["atom"]["atom_id"])
    assert len(set(ids)) == 1
    assert len(package(port, tmp_path)["mandatory"]) == 1
    # Another owner's private audience must remain independent.
    peer = call(port, tmp_path, "memory_write", payload, agent="peer")
    assert peer["ok"], repr(peer)
    assert peer["data"]["atom"]["atom_id"] != ids[0]


def test_replacement_counts_new_head_once_and_rejects_actual_final_overflow(port, tmp_path):
    write(port, tmp_path, "alpha", body("alpha", 450))
    write(port, tmp_path, "bravo", body("bravo", 450))
    accepted = call(port, tmp_path, "memory_update", {"memory_id": "alpha", "body": body("alpha", 500)})
    assert accepted["ok"], accepted  # 500 + 450, never 450 + 500 + 450
    before = persisted_state(tmp_path)
    rejected = call(port, tmp_path, "memory_update", {
        "memory_id": "alpha", "body": body("alpha", 600), "idempotency_key": "overflow-retry",
    })
    assert rejected["code"] == "mandatory_budget_exceeded"
    assert persisted_state(tmp_path) == before
    assert sum(len(item["body"]) for item in package(port, tmp_path)["mandatory"]) == 950
    shrink = call(port, tmp_path, "memory_update", {"memory_id": "bravo", "body": body("bravo", 300)})
    assert shrink["ok"], shrink
    retry = call(port, tmp_path, "memory_update", {
        "memory_id": "alpha", "body": body("alpha", 600), "idempotency_key": "overflow-retry",
    })
    assert retry["ok"], retry  # failed validation did not poison the retry key


def test_related_replacements_validate_final_set_and_preview_rolls_back(port, tmp_path):
    alpha = write(port, tmp_path, "alpha", body("alpha", 400))
    bravo = write(port, tmp_path, "bravo", body("bravo", 400))
    request = {
        "memory_id": "alpha", "body": body("alpha", 700), "expected_revision": alpha["revision"],
        "related_updates": [{"memory_id": "bravo", "body": body("bravo", 200), "expected_revision": bravo["revision"]}],
        "idempotency_key": "related", "reason": "replace both rules together",
    }
    before = persisted_state(tmp_path)
    preview = call(port, tmp_path, "memory_update", {**request, "preview": True})
    assert preview["ok"], preview
    assert preview["data"]["committed"] is False
    assert preview["data"]["receipt"] is None
    assert persisted_state(tmp_path) == before
    result = call(port, tmp_path, "memory_update", request)
    assert result["ok"], result
    assert result["data"]["committed"] is True
    assert sum(len(item["body"]) for item in package(port, tmp_path)["mandatory"]) == 900
    applied = persisted_state(tmp_path)
    replay = call(port, tmp_path, "memory_update", request)
    assert replay["ok"] and replay["data"]["idempotent_replay"] is True
    assert persisted_state(tmp_path) == applied


@pytest.mark.parametrize("failure", ["projection", "publication", "receipt"])
def test_failure_rolls_back_body_revisions_evidence_and_decisions(port, tmp_path, monkeypatch, failure):
    write(port, tmp_path, "alpha", "Use alpha release checks.")
    governance = port._governance_boundary()
    before = persisted_state(tmp_path)
    target, name = {
        "projection": (governance.evidence, "project_batch"),
        "publication": (governance.memory, "set_visibility"),
        "receipt": (governance, "_record"),
    }[failure]
    original = getattr(target, name)

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(target, name, fail_after_write)
    result = call(port, tmp_path, "memory_update", {"memory_id": "alpha", "body": "Use bravo release checks."})
    assert result["ok"] is False
    assert persisted_state(tmp_path) == before
    assert package(port, tmp_path)["mandatory"][0]["body"] == "Use alpha release checks."


def test_related_replacements_reject_stale_and_foreign_targets(port, tmp_path):
    alpha = write(port, tmp_path, "alpha", "Use alpha deployment checks.")
    peer = call(port, tmp_path, "memory_write", {"memory_id": "peer", "body": "Use peer checks.", "injection_policy": "always"}, agent="peer")
    assert peer["ok"]
    base = {"memory_id": "alpha", "body": "Use bravo deployment checks.", "expected_revision": alpha["revision"],
            "reason": "repair", "idempotency_key": "repair"}
    before = persisted_state(tmp_path)
    stale = call(port, tmp_path, "memory_update", {**base, "expected_revision": 99, "related_updates": []})
    assert stale["code"] == "memory_revision_conflict"
    foreign = call(port, tmp_path, "memory_update", {**base, "related_updates": [
        {"memory_id": "peer", "body": "Replace foreign rule.", "expected_revision": 1},
    ]}, admin=True)
    assert foreign["ok"] is False
    assert persisted_state(tmp_path) == before


def test_shared_update_checks_other_members_private_package(port, tmp_path):
    shared = call(port, tmp_path, "memory_write", {
        "memory_id": "shared", "body": body("alpha", 400), "injection_policy": "always", "audience": {"target_type": "group"},
    }, admin=True)
    assert shared["ok"], shared
    peer = call(port, tmp_path, "memory_write", {
        "memory_id": "peer", "body": body("bravo", 400), "injection_policy": "always",
    }, agent="peer")
    assert peer["ok"], peer
    before = persisted_state(tmp_path)
    rejected = call(port, tmp_path, "memory_update", {"memory_id": "shared", "body": body("alpha", 700)}, admin=True)
    assert rejected["code"] == "mandatory_budget_exceeded"
    assert persisted_state(tmp_path) == before


def test_concurrent_replacements_never_leave_duplicate_heads(port, tmp_path):
    write(port, tmp_path, "alpha", "Use initial deploy checks.")

    def replace(index):
        return call(port, tmp_path, "memory_update", {"memory_id": "alpha", "body": f"Use release version {index} checks."},
                    project=f"project-{index}", runtime=f"worker-{index}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(replace, range(4)))
    assert all(item["ok"] for item in results), results
    packet = package(port, tmp_path)
    assert len(packet["mandatory"]) == 1
    assert packet["mandatory"][0]["body"] in {f"Use release version {index} checks." for index in range(4)}


def test_historical_duplicate_predecessors_are_retired_on_explicit_replacement(port, tmp_path):
    original = "Always verify release approval."
    seed_historical(tmp_path, "copy-a", original, project="old-project", runtime="old-model")
    seed_historical(tmp_path, "copy-b", original, project="another-project", runtime="another-model")
    result = call(port, tmp_path, "memory_update", {"memory_id": "copy-a", "body": "Verify release."})
    assert result["ok"], result
    assert result["data"]["atom"]["body"] == "Verify release."
    assert [item["body"] for item in package(port, tmp_path)["mandatory"]] == ["Verify release."]
    retired = call(port, tmp_path, "memory_read", {"memory_id": "copy-b"})
    assert retired["data"]["status"] == "superseded"


def test_failure_retiring_duplicate_rolls_back_entire_replacement(port, tmp_path, monkeypatch):
    for name in ("copy-a", "copy-b"):
        seed_historical(tmp_path, name, "Always verify release approval.")
    governance = port._governance_boundary()
    before = persisted_state(tmp_path)
    original = governance.supersede

    def fail_after_supersede(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected linked replacement failure")

    monkeypatch.setattr(governance, "supersede", fail_after_supersede)
    failed = call(port, tmp_path, "memory_update", {"memory_id": "copy-a", "body": "Verify release."})
    assert failed["ok"] is False
    assert persisted_state(tmp_path) == before


def test_bootstrap_uses_explicit_successor_even_when_old_text_is_unrelated(port, tmp_path):
    seed_historical(tmp_path, "old", body("alpha", 600))
    seed_historical(tmp_path, "new", body("bravo", 600), supersedes=["old"])
    assert [item["body"] for item in package(port, tmp_path)["mandatory"]] == [body("bravo", 600)]


def test_recovery_replaces_overflowing_package_without_relaxing_limits(port, tmp_path):
    alpha = seed_historical(tmp_path, "alpha", body("alpha", 600))
    bravo = seed_historical(tmp_path, "bravo", body("bravo", 600))
    blocked = call(port, tmp_path, "context_bootstrap", {"task": "unrelated work"})
    assert blocked["ok"] is False and blocked["error"] == "mandatory_budget_exceeded"
    request = {"memory_id": "alpha", "body": body("alpha", 400), "expected_revision": alpha.revision,
               "related_updates": [{"memory_id": "bravo", "body": body("bravo", 400), "expected_revision": bravo.revision}],
               "reason": "repair historical replacement overflow", "idempotency_key": "recover"}
    repaired = call(port, tmp_path, "memory_update", request)
    assert repaired["ok"], repaired
    assert sum(len(item["body"]) for item in package(port, tmp_path)["mandatory"]) == 800


def _process_writer(root, start, results, name):
    instance = NativeV2RuntimePort(root, state_provider=Manifest())
    if not start.wait(20):
        return
    result = call(instance, root, "memory_write", {
        "memory_id": name, "body": body(name, 550), "injection_policy": "always",
    })
    results.put((result["ok"], result.get("code")))


def test_separate_mcp_processes_cannot_both_admit_an_overflow(port, tmp_path):
    mp = multiprocessing.get_context("spawn")
    start, results = mp.Event(), mp.Queue()
    workers = [mp.Process(target=_process_writer, args=(tmp_path, start, results, name)) for name in ("alpha", "bravo")]
    try:
        for worker in workers:
            worker.start()
        start.set()
        replies = [results.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
        assert sorted(replies, key=lambda item: item[0]) == [(False, "mandatory_budget_exceeded"), (True, None)]
        assert len(package(port, tmp_path)["mandatory"]) == 1
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()


def test_canonical_rule_mirror_is_suppressed_by_logical_id(port, tmp_path):
    from memoryguard.rules.v2_store import RuleV2Store
    from memoryguard.rule_definition import build_definition
    from memoryguard.rule_binding import build_binding

    seed_historical(tmp_path, "mirror", body("alpha", 600))
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition(body("bravo", 600), kind="procedure", rule_strength="must"))
    rules.upsert_binding(build_binding(definition.definition_id, share_group_id="team", target_type="agent",
                                       target_id="owner", owner_agent_id="owner"))
    rules.upsert_source_link(source_kind="fixture", share_group_id="team", memory_id="mirror",
                             source_ref="fixture:mirror", original_definition_id=definition.definition_id,
                             canonical_definition_id=definition.definition_id, status="active")
    packet = package(port, tmp_path)
    assert [item["body"] for item in packet["mandatory"]] == [definition.canonical_text]


@pytest.mark.parametrize("predecessor_status", ["active", "alias"])
def test_canonical_successor_suppresses_predecessor_and_both_memory_mirrors(port, tmp_path, predecessor_status):
    from dataclasses import replace
    from memoryguard.rules.v2_store import RuleV2Store
    from memoryguard.rule_definition import build_definition
    from memoryguard.rule_binding import build_binding

    rules = RuleV2Store(tmp_path)
    head = rules.upsert_definition(build_definition(body("bravo", 600), kind="procedure", rule_strength="must"))
    old = rules.upsert_definition(replace(
        build_definition(body("alpha", 600), kind="procedure", rule_strength="must"),
        status=predecessor_status, superseded_by=head.definition_id,
    ))
    for name, definition in (("old-mirror", old), ("head-mirror", head)):
        seed_historical(tmp_path, name, definition.canonical_text)
        rules.upsert_binding(build_binding(definition.definition_id, share_group_id="team", target_type="agent",
                                           target_id="owner", owner_agent_id="owner"))
        rules.upsert_source_link(source_kind="fixture", share_group_id="team", memory_id=name,
                                 source_ref="fixture:" + name, original_definition_id=definition.definition_id,
                                 canonical_definition_id=definition.definition_id, status="active")
    assert [item["body"] for item in package(port, tmp_path)["mandatory"]] == [head.canonical_text]
    # Canonical governance belongs to the GUI; updating a stale mirror must
    # not report successful recovery while bootstrap still injects old text.
    before = persisted_state(tmp_path)
    revision = call(port, tmp_path, "memory_read", {"memory_id": "head-mirror"})["data"]["revision"]
    result = call(port, tmp_path, "memory_update", {
        "memory_id": "head-mirror", "body": "A shorter replacement.", "expected_revision": revision,
        "preview": True, "reason": "repair", "idempotency_key": "mirror-repair",
    })
    assert result["code"] == "canonical_rule_governance_required", result
    assert persisted_state(tmp_path) == before
    gui_result = port.dispatch_gui("edit_memory", {"memory_id": "head-mirror", "body": "A shorter replacement."},
                                  context=context(tmp_path, admin=True), generation=1)
    assert gui_result["code"] == "canonical_rule_governance_required", gui_result
    assert persisted_state(tmp_path) == before


@pytest.mark.parametrize("bad_field", [{"status": "active"}, {"admin": True}, {"share_group_id": "other"}])
def test_related_update_rejects_lifecycle_and_scope_controls(port, tmp_path, bad_field):
    first = write(port, tmp_path, "alpha", "Use alpha checks.")
    second = write(port, tmp_path, "bravo", "Use bravo checks.")
    before = persisted_state(tmp_path)
    result = call(port, tmp_path, "memory_update", {
        "memory_id": "alpha", "body": "Updated alpha checks.", "expected_revision": first["revision"],
        "related_updates": [{"memory_id": "bravo", "expected_revision": second["revision"], **bad_field}],
        "reason": "repair", "idempotency_key": "invalid-controls",
    })
    assert not result["ok"]
    assert persisted_state(tmp_path) == before


def test_recovery_update_cannot_restore_deleted_memory(port, tmp_path):
    write(port, tmp_path, "alpha", "Use alpha checks.")
    deleted = call(port, tmp_path, "memory_delete", {"memory_id": "alpha", "idempotency_key": "delete-alpha"})
    assert deleted["ok"], deleted
    before = persisted_state(tmp_path)
    restored = call(port, tmp_path, "memory_update", {"memory_id": "alpha", "status": "active"})
    assert restored["code"] == "memory_lifecycle_governance_required"
    assert persisted_state(tmp_path) == before


def test_nested_boundary_keeps_receipts_in_governance_database(port, tmp_path):
    governance = port._governance_boundary()
    with governance.atomic_memory():
        nested = GovernanceV2(tmp_path, memory_store=governance.memory, evidence_store=governance.evidence)
        write(port, tmp_path, "alpha", "Use alpha checks.")
        assert nested.list_decisions()
    assert governance.list_decisions()
    with sqlite3.connect(governance.memory.db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables.intersection({"decisions", "request_ledger", "decision_outbox"})


def test_native_publication_does_not_reinitialize_validated_stores(port, tmp_path, monkeypatch):
    def unexpected_initialization(*args):
        raise AssertionError("a live native runtime must not rebuild validated schemas")

    monkeypatch.setattr(MemoryAtomStore, "_init_schema", unexpected_initialization)
    monkeypatch.setattr(EvidenceStore, "_init_schema", unexpected_initialization)
    write(port, tmp_path, "alpha", "Always verify deployment approval.")
