from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from memoryguard.content import ContentReadScope, ContentStore
from memoryguard.knowledge_v2.command import KnowledgeV2CommandService
from memoryguard.runtime_v2.task_coordinator import TaskCoordinatorError
from memoryguard.runtime_v2.working_memory import RuntimeScope, RuntimeStore


def _scope(workspace: Path) -> ContentReadScope:
    return ContentReadScope(
        namespace_id="knowledge-test-namespace",
        workspace_id=str(workspace.resolve()),
        agent_instance_id="agent-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
    )


def _context() -> dict[str, object]:
    return {
        "agent_instance_id": "agent-a",
        "project_ref": "",
        "provider": "gui",
        "share_group_id": "group-a",
        "runtime_role": "gui",
        "admin": True,
    }


def _runtime_scope(workspace: Path) -> RuntimeScope:
    return RuntimeScope(
        workspace_id=str(workspace.resolve()),
        agent_instance_id="agent-a",
        project_ref=str(workspace.resolve()),
        share_group_id="group-a",
        provider="gui",
        runtime_scope="gui",
    )


def _wait(service: KnowledgeV2CommandService, workspace: Path, run_id: str) -> dict:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        value = service.tasks.status(run_id, _runtime_scope(workspace))
        if value.get("status") in {"succeeded", "failed", "cancelled"}:
            return value
        time.sleep(0.02)
    raise AssertionError("knowledge task did not finish")


def test_knowledge_add_uses_content_plane_and_runtime_task(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    body = "# Private Heading\n\nThis exact body belongs only in content.db."
    (source / "notes.md").write_text(body, encoding="utf-8")

    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    accepted = service.add({"path": str(source), "title": "Book"}, scope=scope, context=_context())
    assert accepted["status"] in {"queued", "running", "succeeded"}
    run_id = accepted["task"]["run_id"]
    finished = _wait(service, tmp_path, run_id)
    assert finished["status"] == "succeeded", finished

    persisted = RuntimeStore(tmp_path, readonly=True).get_run(run_id, _runtime_scope(tmp_path))
    assert persisted is not None and persisted.status == "succeeded"

    with sqlite3.connect(service.knowledge_db) as conn:
        assets = conn.execute("SELECT asset_id,status,metadata_json FROM knowledge_assets").fetchall()
        docs = conn.execute("SELECT path,metadata_json FROM knowledge_documents").fetchall()
        assert len(assets) == 1
        assert assets[0][1] == "active"
        assert len(docs) == 1
        serialized = "\n".join(str(value) for row in (*assets, *docs) for value in row)
        assert body not in serialized
        meta = json.loads(docs[0][1])
        assert meta["occurrence_ids"]
        assert meta["content_hash"]

    content = ContentStore(tmp_path, initialize=False)
    with sqlite3.connect(content.db_path) as conn:
        texts = [str(row[0]) for row in conn.execute("SELECT text FROM content_blobs")]
    assert any("This exact body belongs only in content.db." in text for text in texts)

    service.close()


def test_knowledge_add_reuses_matching_request_and_rejects_reused_key(tmp_path: Path) -> None:
    first_source = tmp_path / "first"
    first_source.mkdir()
    (first_source / "a.md").write_text("# First\n\nbody", encoding="utf-8")
    other_source = tmp_path / "other"
    other_source.mkdir()
    (other_source / "b.md").write_text("# Other\n\nbody", encoding="utf-8")
    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    request = {"path": str(first_source), "title": "First", "idempotency_key": "knowledge-request"}

    accepted = service.add(request, scope=scope, context=_context())
    assert _wait(service, tmp_path, accepted["task"]["run_id"])["status"] == "succeeded"
    replay = service.add(request, scope=scope, context=_context())
    assert replay["reused"] is True
    assert replay["code"] == "idempotent_replay"
    assert replay["task"]["run_id"] == accepted["task"]["run_id"]

    with pytest.raises(TaskCoordinatorError, match="idempotency_key_reused"):
        service.add(
            {"path": str(other_source), "title": "Other", "idempotency_key": "knowledge-request"},
            scope=scope,
            context=_context(),
        )
    service.close()


def test_knowledge_reingest_tombstones_removed_blocks_without_copying_body(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file = source / "notes.md"
    file.write_text("# One\n\nfirst\n\n# Two\n\nsecond", encoding="utf-8")
    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    first = service.add({"path": str(source)}, scope=scope, context=_context())
    assert _wait(service, tmp_path, first["task"]["run_id"])["status"] == "succeeded"

    with sqlite3.connect(service.knowledge_db) as conn:
        book_id = str(conn.execute("SELECT asset_id FROM knowledge_assets").fetchone()[0])
        before_ids = json.loads(conn.execute("SELECT metadata_json FROM knowledge_documents").fetchone()[0])["occurrence_ids"]
    assert len(before_ids) >= 2

    file.write_text("# One\n\nfirst changed", encoding="utf-8")
    second = service.reingest({"book_id": book_id}, scope=scope, context=_context())
    assert _wait(service, tmp_path, second["task"]["run_id"])["status"] == "succeeded"

    content = ContentStore(tmp_path, initialize=False)
    with sqlite3.connect(content.db_path) as conn:
        inactive = conn.execute(
            "SELECT COUNT(*) FROM content_occurrences WHERE active=0 AND deleted_scan_id<>''"
        ).fetchone()[0]
    assert inactive >= 1
    service.close()


def test_knowledge_remove_restore_and_purge_preserve_blob_until_maintenance(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("durable knowledge body", encoding="utf-8")
    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    added = service.add({"path": str(source)}, scope=scope, context=_context())
    assert _wait(service, tmp_path, added["task"]["run_id"])["status"] == "succeeded"

    with sqlite3.connect(service.knowledge_db) as conn:
        book_id = str(conn.execute("SELECT asset_id FROM knowledge_assets").fetchone()[0])
    content = ContentStore(tmp_path, initialize=False)
    before_blobs = content.counts()["content_blobs"]

    removed = service.remove({"book_id": book_id}, scope=scope)
    deletion_id = removed["data"]["deletion_id"]
    assert service.deleted(scope=scope)["data"]["total"] == 1
    assert content.counts()["content_blobs"] == before_blobs

    restored = service.restore({"deletion_id": deletion_id}, scope=scope)
    assert restored["data"]["restored"] >= 1
    assert service.deleted(scope=scope)["data"]["total"] == 0

    removed_again = service.remove({"book_id": book_id}, scope=scope)
    deletion_id_2 = removed_again["data"]["deletion_id"]
    purged = service.purge({"deletion_id": deletion_id_2}, scope=scope)
    assert purged["data"]["released_holds"] >= 1
    assert content.counts()["content_blobs"] == before_blobs

    with sqlite3.connect(service.knowledge_db) as conn:
        assert conn.execute("SELECT status FROM knowledge_assets WHERE asset_id=?", (book_id,)).fetchone()[0] == "purged"
    service.close()


def test_knowledge_settings_and_reference_rebuild_do_not_read_source_again(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file = source / "a.txt"
    file.write_text("initial body", encoding="utf-8")
    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    added = service.add({"path": str(source)}, scope=scope, context=_context())
    assert _wait(service, tmp_path, added["task"]["run_id"])["status"] == "succeeded"
    with sqlite3.connect(service.knowledge_db) as conn:
        book_id = str(conn.execute("SELECT asset_id FROM knowledge_assets").fetchone()[0])

    settings = service.update_settings(
        {"book_id": book_id, "settings": {"vector_enabled": True}}, scope=scope
    )
    assert settings["data"]["settings"] == {"vector_enabled": True}

    # Rebuild is reference-only: make the source unavailable after ingestion.
    file.unlink()
    source.rmdir()
    rebuilt = service.rebuild({"book_id": book_id}, scope=scope, context=_context())
    assert _wait(service, tmp_path, rebuilt["task"]["run_id"])["status"] == "succeeded"

    with sqlite3.connect(service.knowledge_db) as conn:
        meta = json.loads(conn.execute("SELECT metadata_json FROM knowledge_assets WHERE asset_id=?", (book_id,)).fetchone()[0])
    assert meta["settings"]["vector_enabled"] is True
    assert meta["index_generation"] >= 1
    service.close()
