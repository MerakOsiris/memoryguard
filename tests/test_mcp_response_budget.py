from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard import mcp_server
from memoryguard.mcp_catalog import TOOLS, mcp_capability_catalog
from memoryguard.response_budget import (
    BindingRevision,
    DEFAULT_RESPONSE_BUDGET_BYTES,
    ResponseReferenceError,
    ResponseScope,
    ResponseStore,
    json_bytes,
)


_CONTEXT = {
    "workspace_id": "workspace",
    "share_group_id": "group-a",
    "agent_instance_id": "agent-a",
    "project_ref": "project-a",
    "provider": "codex",
    "runtime_role": "root",
    "session_id": "session-a",
    "session_source": "host",
    "session_trusted": True,
    "context_hash": "context-a",
    "namespace_id": "",
    "sensitivity": "",
    "policy_class": "",
}
_BINDING = BindingRevision("binding-a", 7, "group-a", "agent-a")


def _oversized_envelope(*, structured: bool) -> dict[str, object]:
    result: dict[str, object] = {
        "content": [{"type": "text", "text": json.dumps({"memory_id": "m-1", "body": "x" * 30_000}, indent=2)}],
        "status": "succeeded",
    }
    if structured:
        result["structuredContent"] = {"body": "y" * 30_000, "receipt_id": "receipt-1"}
    return result


def test_budget_counts_content_and_structured_content_and_pages_exact_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_trusted_context_for_v2", lambda args, workspace: (_CONTEXT, None))
    monkeypatch.setattr(mcp_server, "_response_binding_revision", lambda workspace, context: _BINDING)
    monkeypatch.setattr(mcp_server, "_revalidate_response_read", lambda *args: None)

    result = mcp_server._budget_success_response(
        _oversized_envelope(structured=True),
        name="memoryguard_memory_search",
        args={"query": "release"},
        workspace=tmp_path,
    )

    assert len(json_bytes(result)) <= DEFAULT_RESPONSE_BUDGET_BYTES
    body = json.loads(result["content"][0]["text"])
    assert body["delivery"]["status"] == "paged"
    assert body["identifiers"]["content[0].memory_id"] == "m-1"
    assert result["structuredContent"] == body

    reference = body["response_ref"]["id"]
    offset = 0
    pages: list[str] = []
    while True:
        page = mcp_server.GLOBAL_RESPONSE_STORE.read(
            reference,
            scope=ResponseScope.from_mapping(_CONTEXT),
            binding=_BINDING,
            fields=["memory_id"],
            offset=offset,
            limit=4096,
        )
        pages.append(page["page"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(pages)) == {"memory_id": "m-1"}


def test_content_only_success_never_gains_structured_content(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_trusted_context_for_v2", lambda args, workspace: (_CONTEXT, None))
    monkeypatch.setattr(mcp_server, "_response_binding_revision", lambda workspace, context: _BINDING)
    monkeypatch.setattr(mcp_server, "_revalidate_response_read", lambda *args: None)

    result = mcp_server._budget_success_response(
        _oversized_envelope(structured=False),
        name="memoryguard_memory_search",
        args={"query": "release"},
        workspace=tmp_path,
    )

    assert "structuredContent" not in result
    assert json.loads(result["content"][0]["text"])["response_ref"]


def test_legacy_deprecation_is_preserved_before_final_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_trusted_context_for_v2", lambda args, workspace: (_CONTEXT, None))
    monkeypatch.setattr(mcp_server, "_response_binding_revision", lambda workspace, context: _BINDING)
    monkeypatch.setattr(mcp_server, "_revalidate_response_read", lambda *args: None)
    deprecated = mcp_server._hidden_tool_deprecation(
        _oversized_envelope(structured=False),
        "memoryguard_history_search",
    )

    result = mcp_server._budget_success_response(
        deprecated,
        name="memoryguard_history_search",
        args={"query": "release"},
        workspace=tmp_path,
    )

    body = json.loads(result["content"][0]["text"])
    assert body["deprecated"] is True
    assert body["deprecation"]["code"] == "mcp_tool_not_listed"
    assert len(json_bytes(result)) <= DEFAULT_RESPONSE_BUDGET_BYTES


def test_private_response_ref_fails_closed_for_other_principal_and_changed_read():
    owner = ResponseScope.from_mapping(_CONTEXT)
    store = ResponseStore()
    reference = store.put(
        {"content": [{"type": "text", "text": "private"}]},
        scope=owner,
        binding=_BINDING,
        revalidate=lambda: None,
    )["id"]
    other = dict(_CONTEXT, agent_instance_id="agent-b")
    with pytest.raises(ResponseReferenceError, match="response_ref_access_denied"):
        store.read(reference, scope=ResponseScope.from_mapping(other), binding=_BINDING)

    stale = store.put(
        {"content": [{"type": "text", "text": "old"}]},
        scope=owner,
        binding=_BINDING,
        revalidate=lambda: "response_ref_result_changed",
    )["id"]
    with pytest.raises(ResponseReferenceError, match="response_ref_result_changed"):
        store.read(stale, scope=owner, binding=_BINDING)


def test_compaction_preserves_small_existing_structured_contract():
    result = mcp_server._v2_result_envelope({
        "content": [{"type": "text", "text": json.dumps({"ok": True, "id": "one"}, indent=2)}],
        "structuredContent": {"ok": True, "id": "one"},
    })

    assert result["structuredContent"] == {"ok": True, "id": "one"}
    assert result["content"][0]["text"] == '{"ok":true,"id":"one"}'


def test_response_paging_is_rejected_before_write_dispatch():
    with pytest.raises(ValueError, match="response_pagination_read_only"):
        mcp_server._validate_response_paging_arguments("memoryguard_memory_write", {"response_fields": ["content"]})
    with pytest.raises(ValueError, match="response_pagination_read_only"):
        mcp_server._validate_response_paging_arguments(
            "memoryguard_invoke",
            {"operation": "memoryguard_memory_write", "arguments": {"response_offset": 0}},
        )
    mcp_server._validate_response_paging_arguments("memoryguard_memory_write", {"offset": 0})


def test_response_reader_rejects_oversized_field_receipt(monkeypatch, tmp_path):
    store = ResponseStore()
    response_ref = store.put(
        {"content": [{"type": "text", "text": '{"memory_id":"m-1"}'}]},
        scope=None,
        binding=None,
        public=True,
    )["id"]
    monkeypatch.setattr(mcp_server, "GLOBAL_RESPONSE_STORE", store)

    result = mcp_server._read_response_reference(
        {"arguments": {"response_ref": response_ref, "fields": ["x" * 5_000]}},
        tmp_path,
    )

    body = json.loads(result["content"][0]["text"])
    assert body["code"] == "response_fields_invalid"
    assert len(json_bytes(result)) <= DEFAULT_RESPONSE_BUDGET_BYTES


def test_native_large_memory_read_returns_revalidated_business_field_page(monkeypatch, tmp_path: Path):
    """Exercise the real MCP facade, native memory port, and response broker."""
    from memoryguard.evidence.store import EvidenceStore
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.memory import MemoryAtomStore
    from memoryguard.runtime_v2.group_native import GroupControlService
    from memoryguard.storage.layout import WorkspaceV2Layout
    from memoryguard.storage.schema import initialize_all
    from memoryguard.system.manifest import ManifestManager, ManifestState

    workspace = tmp_path.resolve()
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=EvidenceStore(workspace))
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="response-budget-native")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="response-budget-source",
        target_digest="response-budget-target",
        manifest_digest="response-budget-manifest",
        digests={"validator_passed": True, "checkpoints": {"mcp": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE

    agent = "response-budget-agent"
    group = "response-budget-group"
    GroupControlService(workspace, write=True).bind_agent(agent, group, idempotency_key="response-budget-bind")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "response-budget-session")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "transport")
    monkeypatch.setenv("MEMORYGUARD_SESSION_TRUSTED", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(workspace))

    memory_id = "response-budget-memory"
    written = mcp_server.execute_tool("memoryguard_memory_write", {
        "memory_id": memory_id,
        "body": "x" * 30_000,
        "kind": "fact",
        "audience": {"target_type": "group", "target_id": group},
        "idempotency_key": "response-budget-write",
    })
    assert written.get("isError") is not True, written
    read = mcp_server.execute_tool("memoryguard_memory_read", {"memory_id": memory_id})
    read_body = json.loads(read["content"][0]["text"])
    response_ref = read_body["response_ref"]["id"]

    page = mcp_server.execute_tool("memoryguard_invoke", {
        "operation": "memoryguard_response_read",
        "arguments": {
            "response_ref": response_ref,
            "fields": ["/data/memory_id"],
            "offset": 0,
            "limit": 4096,
        },
    })
    assert page.get("isError") is not True, page
    page_body = json.loads(page["content"][0]["text"])
    assert page_body["document_kind"] == "payload_fields"
    assert json.loads(page_body["page"]) == {"data": {"memory_id": memory_id}}

    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "response-budget-other-session")
    wrong_session = mcp_server.execute_tool("memoryguard_invoke", {
        "operation": "memoryguard_response_read",
        "arguments": {"response_ref": response_ref},
    })
    assert json.loads(wrong_session["content"][0]["text"])["code"] == "response_ref_access_denied"

    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "response-budget-session")
    updated = mcp_server.execute_tool("memoryguard_memory_update", {
        "memory_id": memory_id,
        "body": "y" * 30_001,
        "idempotency_key": "response-budget-update",
    })
    assert updated.get("isError") is not True, updated
    changed = mcp_server.execute_tool("memoryguard_invoke", {
        "operation": "memoryguard_response_read",
        "arguments": {"response_ref": response_ref},
    })
    assert json.loads(changed["content"][0]["text"])["code"] == "response_ref_result_changed"

    current_read = mcp_server.execute_tool("memoryguard_memory_read", {"memory_id": memory_id})
    current_ref = json.loads(current_read["content"][0]["text"])["response_ref"]["id"]
    deleted = mcp_server.execute_tool("memoryguard_memory_delete", {
        "memory_id": memory_id,
        "idempotency_key": "response-budget-delete",
    })
    assert deleted.get("isError") is not True, deleted
    stale = mcp_server.execute_tool("memoryguard_invoke", {
        "operation": "memoryguard_response_read",
        "arguments": {"response_ref": current_ref},
    })
    assert json.loads(stale["content"][0]["text"])["code"] in {
        "response_ref_expired",
        "response_ref_result_changed",
    }

    revocable_id = "response-budget-revocable"
    revocable_write = mcp_server.execute_tool("memoryguard_memory_write", {
        "memory_id": revocable_id,
        "body": "z" * 30_002,
        "kind": "fact",
        "audience": {"target_type": "group", "target_id": group},
        "idempotency_key": "response-budget-revocable-write",
    })
    assert revocable_write.get("isError") is not True, revocable_write
    revocable_read = mcp_server.execute_tool("memoryguard_memory_read", {"memory_id": revocable_id})
    revocable_ref = json.loads(revocable_read["content"][0]["text"])["response_ref"]["id"]
    binding_id = GroupControlService(workspace, write=False).active_binding_for_agent(agent)["binding_id"]
    GroupControlService(workspace, write=True).unbind(binding_id, idempotency_key="response-budget-unbind")
    revoked = mcp_server.execute_tool("memoryguard_invoke", {
        "operation": "memoryguard_response_read",
        "arguments": {"response_ref": revocable_ref},
    })
    assert json.loads(revoked["content"][0]["text"])["code"] == "response_ref_access_denied"


def test_response_reader_is_discoverable_only_through_broker_catalog():
    catalog = mcp_capability_catalog({"operation": "memoryguard_response_read", "include_schema": True})
    assert catalog["total"] == 1
    item = catalog["items"][0]
    assert item["broker_invocable"] is True
    assert item["input_schema"]["required"] == ["response_ref"]
    assert "memoryguard_response_read" not in {tool["name"] for tool in TOOLS}
