from dataclasses import replace
from pathlib import Path

from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore
from memoryguard.codegraph_v2.graphify_adapter import EXPORT_FORMAT, GraphifyExportAdapter


def test_migrated_scope_remains_readable_and_updatable_without_crossing_acl(tmp_path: Path, monkeypatch) -> None:
    store = CodeGraphStore(tmp_path)
    scope = CodeGraphScope(str(tmp_path), "agent", str(tmp_path / "repo"), "codex", "group", "root")
    export = {
        "format": EXPORT_FORMAT, "complete": True, "graphify_version": "test",
        "source_digest": "first",
        "files": [{"id": "main", "path": "src/main.py", "content_hash": "first",
                   "language": "python", "source_role": "production", "provenance": "production"}],
        "nodes": [{"id": "run", "file": "main", "name": "run", "kind": "function",
                   "source_location": "L1", "provenance": "production"}],
        "edges": [],
    }
    # Model a migration that kept the original key while normalizing ACL paths.
    with monkeypatch.context() as patch:
        patch.setattr(store, "_scope_id", lambda scope: "scope-before-migration")
        GraphifyExportAdapter(store).project(export, scope=scope, full_snapshot=True)

    reopened = CodeGraphStore(tmp_path, initialize=False)
    assert reopened.counts(scope=scope)["active_source_files"] == 1
    assert reopened.query_symbols("run", scope=scope)
    assert reopened._scope_id(scope) == "scope-before-migration"
    for field, value in (("agent_instance_id", "other"), ("share_group_id", "other"),
                         ("project_ref", str(tmp_path / "other")), ("provider", "other"),
                         ("runtime_role", "other")):
        assert reopened.query_symbols("run", scope=replace(scope, **{field: value})) == ()

    export["source_digest"] = "second"
    export["files"][0]["content_hash"] = "second"
    GraphifyExportAdapter(reopened).project(export, scope=scope, full_snapshot=True)
    assert reopened.list_source_files(scope=scope)[0].content_hash == "second"
    with reopened.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_scopes").fetchone()[0] == 1
