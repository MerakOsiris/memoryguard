from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore, stable_id
from memoryguard.codegraph_v2.store import CODEGRAPH_AUX_SCHEMA
from memoryguard.codegraph_v2.graphify_adapter import EXPORT_FORMAT, GraphifyCapability, GraphifyExportAdapter, GraphifyExportError
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.storage.database import execute_sql_script, open_database
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_database


def _scope(root: Path) -> CodeGraphScope:
    return CodeGraphScope(str(root), "agent", "project", "codex", "group", "gui")


def _export() -> dict:
    return {
        "format": EXPORT_FORMAT,
        "complete": True,
        "graphify_version": "phase9-test",
        "source_digest": "deadbeef",
        "files": [
            {"id": "gui-file", "path": "src/gui.py", "content_hash": "hash-gui", "language": "python", "source_role": "production", "provenance": "production"},
            {"id": "runtime-file", "path": "src/runtime.py", "content_hash": "hash-runtime", "language": "python", "source_role": "production", "provenance": "production"},
            {"id": "fixture-file", "path": "tests/fixture.py", "content_hash": "hash-fixture", "language": "python", "source_role": "fixture", "provenance": "fixture"},
        ],
        "nodes": [
            {"id": "control", "file": "gui-file", "name": "添加知识", "kind": "control", "source_location": "L10", "provenance": "production", "semantic_kind": "gui_control", "source_map": {"host_symbol": "PAGE_HTML", "region_id": "r1"}},
            {"id": "handler", "file": "gui-file", "name": "addBook", "kind": "function", "source_location": "L20", "provenance": "production", "semantic_kind": "handler"},
            {"id": "api", "file": "gui-file", "name": "knowledge_add", "kind": "api", "source_location": "L30", "provenance": "production", "semantic_kind": "api_method"},
            {"id": "surface", "file": "runtime-file", "name": "GuiOperationSpec:knowledge_add", "kind": "surface", "source_location": "L40", "provenance": "production", "semantic_kind": "surface_spec"},
            {"id": "native", "file": "runtime-file", "name": "gui_knowledge_command", "kind": "native", "source_location": "L50", "provenance": "production", "semantic_kind": "native_handler"},
            {"id": "fixture", "file": "fixture-file", "name": "fake_handler", "kind": "function", "source_location": "L1", "provenance": "fixture", "semantic_kind": "handler"},
        ],
        "edges": [
            {"source": "control", "target": "handler", "relation": "references", "context": "control_handler", "source_location": "L10", "provenance": "production", "semantic_kind": "control_handler"},
            {"source": "handler", "target": "api", "relation": "references", "context": "handler_api", "source_location": "L20", "provenance": "production", "semantic_kind": "handler_api"},
            {"source": "api", "target": "surface", "relation": "references", "context": "api_surface", "source_location": "L30", "provenance": "production", "semantic_kind": "api_surface"},
            {"source": "surface", "target": "native", "relation": "references", "context": "api_surface", "source_location": "L40", "provenance": "production", "semantic_kind": "api_surface"},
            {"source": "fixture", "target": "native", "relation": "references", "context": "handler_api", "source_location": "L1", "provenance": "fixture", "semantic_kind": "handler_api"},
        ],
    }


def test_graphify_adapter_rejects_raw_legacy_graph_and_missing_provenance(tmp_path: Path) -> None:
    adapter = GraphifyExportAdapter(CodeGraphStore(tmp_path))
    with pytest.raises(GraphifyExportError, match="graphify_export"):
        adapter.project({"nodes": [], "links": []}, scope=_scope(tmp_path))
    payload = _export()
    payload["files"][0].pop("provenance")
    with pytest.raises(GraphifyExportError, match="hash/provenance metadata"):
        adapter.project(payload, scope=_scope(tmp_path))


def test_graphify_adapter_rejects_source_body_fields(tmp_path: Path) -> None:
    adapter = GraphifyExportAdapter(CodeGraphStore(tmp_path))
    payload = _export()
    payload["nodes"][0]["body"] = "print('must not persist')"
    with pytest.raises(GraphifyExportError, match="graphify_source_body_forbidden"):
        adapter.project(payload, scope=_scope(tmp_path))


def test_graphify_projection_round_trip_and_production_queries(tmp_path: Path) -> None:
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    result = GraphifyExportAdapter(store).project(_export(), scope=scope)
    assert len(result.files) == 3
    assert len(result.symbols) >= 6
    assert len(result.edges) == 5

    add_book = store.query_symbols("addBook", scope=scope, provenance="production")
    assert [item.name for item in add_book] == ["addBook"]
    control = store.query_symbols("添加知识", scope=scope, provenance="production")[0]
    native = store.query_symbols("gui_knowledge_command", scope=scope, provenance="production")[0]
    fixture = store.query_symbols("fake_handler", scope=scope, provenance="fixture")[0]

    path = store.path_query(control.symbol_id, native.symbol_id, scope=scope, provenance="production", max_depth=8)
    assert path["found"] is True
    assert path["hops"] == 4
    assert fixture.symbol_id not in path["path"]

    explanation = store.explain_symbol(control.symbol_id, scope=scope, provenance="production")
    assert explanation["symbol"]["source_map"]["host_symbol"] == "PAGE_HTML"
    assert explanation["file"]["source_role"] == "production"
    assert explanation["edges"][0]["context"] == "control_handler"

    affected = store.affected_query(native.symbol_id, scope=scope, provenance="production", depth=8)
    assert control.symbol_id in affected.result_ids
    assert fixture.symbol_id not in affected.result_ids
    assert affected.provenance_filter == "production"
    # Read-only graph queries are physically zero-write; query IDs/digests are
    # deterministic return receipts, not rows appended to the authoritative DB.
    with store.connection() as conn:
        receipt = conn.execute("SELECT provenance_filter,result_digest FROM affected_queries WHERE query_id=?", (affected.query_id,)).fetchone()
    assert receipt is None


def test_graphify_duplicate_db_identity_maps_all_external_ids_and_canonical_edges(tmp_path: Path) -> None:
    payload = _export()
    duplicate = dict(next(item for item in payload["nodes"] if item["id"] == "handler"))
    duplicate["id"] = "handler-duplicate"
    payload["nodes"].append(duplicate)
    payload["edges"].append(
        {
            "source": "handler-duplicate",
            "target": "api",
            "relation": "references",
            "context": "duplicate_handler_api",
            "source_location": "L21",
            "provenance": "production",
        }
    )

    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    result = GraphifyExportAdapter(store).project(payload, scope=scope, full_snapshot=True)
    assert result.counts["symbols"] == len(payload["nodes"]) - 1
    assert result.counts["edges"] == len(payload["edges"])

    canonical = store.resolve_external_symbol_id("handler", scope=scope)
    duplicate_canonical = store.resolve_external_symbol_id("handler-duplicate", scope=scope)
    assert canonical and duplicate_canonical == canonical
    with store.connection() as conn:
        mappings = conn.execute(
            "SELECT COUNT(*) FROM migration_map WHERE source_db='graphify' AND source_table='symbol_external_id' AND target_type='symbol'",
        ).fetchone()[0]
    assert mappings == len(payload["nodes"])
    assert all(edge.from_id != "handler-duplicate" and edge.to_id != "handler-duplicate" for edge in store.list_edges(scope=scope))


def test_graphify_full_snapshot_failure_rolls_back_everything_including_outbox(tmp_path: Path) -> None:
    payload = _export()
    payload["edges"].append(
        {
            "source": "missing-node",
            "target": "api",
            "relation": "references",
            "provenance": "production",
        }
    )
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)

    with pytest.raises(GraphifyExportError, match="graphify_edge_endpoint_unknown"):
        GraphifyExportAdapter(store).project(payload, scope=scope, full_snapshot=True)

    counts = store.counts(scope=scope)
    assert counts["source_files"] == 0
    assert counts["symbols"] == 0
    assert counts["edges"] == 0
    assert counts["outbox"] == 0


def _native_context(root: Path, *, agent: str = "agent", group: str = "group"):
    from memoryguard.access_context import AccessContext

    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"codegraph-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root),
        share_group_id=group,
        project_ref="project",
        provider="codex",
        runtime_role="gui",
    )


def test_native_codegraph_update_query_path_and_scope_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    CodeGraphStore(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11})
    context = _native_context(tmp_path)
    updated = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"confirmed": True, "export": _export(), "full_snapshot": True},
        context=context,
        generation=11,
        mutation=True,
    )
    assert updated["ok"] is True, updated
    queried = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "addBook", "provenance": "production"},
        context=context,
        generation=11,
    )
    assert queried["ok"] is True, queried
    symbols = queried.get("data", queried)["symbols"]
    assert [item["name"] for item in symbols] == ["addBook"]

    hidden = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "addBook", "provenance": "production"},
        context=_native_context(tmp_path, agent="other", group="other-group"),
        generation=11,
    )
    assert hidden["ok"] is True and hidden.get("data", hidden)["count"] == 0

    rejected_export = _export()
    rejected_export["nodes"][0]["body"] = "do not store me"
    rejected = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"confirmed": True, "export": rejected_export, "full_snapshot": True},
        context=context,
        generation=11,
        mutation=True,
    )
    assert rejected["ok"] is False and rejected["code"] == "graphify_source_body_forbidden"

    monkeypatch.setattr(
        GraphifyCapability,
        "detect",
        classmethod(lambda cls: GraphifyCapability(True, "0.9.39", "graphify", False, "graphify_metadata_export_unavailable")),
    )
    status = port.dispatch_mcp("memoryguard_codegraph_status", {}, context=context, generation=11)
    assert status["ok"] is True, status
    status_data = status.get("data", status)
    assert status_data["update_ready"] is False
    assert status_data["capability_error"] == "graphify_metadata_export_unavailable"


def _v1_aux_schema() -> str:
    """Derive the last published aux-v1 DDL from the v2 schema contract."""
    schema = CODEGRAPH_AUX_SCHEMA
    schema = schema.replace("    source_role TEXT NOT NULL DEFAULT 'production',\n    provenance TEXT NOT NULL DEFAULT 'production',\n", "")
    schema = schema.replace("    provenance TEXT NOT NULL DEFAULT 'production',\n    source_map_json TEXT NOT NULL DEFAULT '{}',\n    metadata_json TEXT NOT NULL DEFAULT '{}',\n", "", 1)
    schema = schema.replace("    context TEXT NOT NULL DEFAULT '',\n    provenance TEXT NOT NULL DEFAULT 'production',\n    source_location TEXT NOT NULL DEFAULT '',\n    metadata_json TEXT NOT NULL DEFAULT '{}',\n", "")
    schema = schema.replace(
        "    UNIQUE(scope_id, revision_id, from_id, to_id, relation, context, provenance, source_location),\n",
        "    UNIQUE(scope_id, revision_id, from_id, to_id, relation),\n",
    )
    schema = schema.replace("    provenance_filter TEXT NOT NULL DEFAULT '',\n", "")
    schema = schema.replace(
        "    UNIQUE(scope_id, start_id, depth, result_limit, relation_filter, provenance_filter, result_digest),\n",
        "    UNIQUE(scope_id, start_id, depth, result_limit, relation_filter, result_digest),\n",
    )
    return schema


def _seed_v1_codegraph(root: Path) -> dict[str, str]:
    layout = WorkspaceV2Layout(root)
    layout.ensure_dirs()
    initialize_database(layout.codegraph_db, "codegraph", layout=layout)
    with open_database(layout.codegraph_db) as conn:
        execute_sql_script(conn, _v1_aux_schema())
        conn.execute("INSERT INTO codegraph_schema_meta(key,value) VALUES('version','1')")
        legacy_scope = CodeGraphScope(str(root), "agent", "project", "codex", "group", "gui")
        scope_id = stable_id("scope", *legacy_scope.as_tuple())
        file_id = "legacy-file"
        revision_id = "legacy-revision"
        conn.execute(
            "INSERT INTO graph_scopes(scope_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,runtime_role,trusted_context,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (scope_id, str(root), "agent", "project", "codex", "group", "gui", 1, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO source_files(file_id,scope_id,source_id,path,content_hash,source_revision,language,revision_id,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, scope_id, "legacy-source", "src/legacy.py", "legacy-hash", "r1", "python", revision_id, 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO revisions(revision_id,file_id,scope_id,content_hash,source_revision,revision_number,created_at) VALUES(?,?,?,?,?,?,?)",
            (revision_id, file_id, scope_id, "legacy-hash", "r1", 1, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO symbols(symbol_id,file_id,scope_id,revision_id,name,kind,signature,symbol_hash,line_start,line_end,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy-a", file_id, scope_id, revision_id, "legacyA", "function", "", "ha", 1, 2, 1, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO symbols(symbol_id,file_id,scope_id,revision_id,name,kind,signature,symbol_hash,line_start,line_end,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy-b", file_id, scope_id, revision_id, "legacyB", "function", "", "hb", 3, 4, 1, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO edges(edge_id,scope_id,revision_id,from_id,to_id,relation,weight,active,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("legacy-edge", scope_id, revision_id, "legacy-a", "legacy-b", "references", 1.0, 1, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    return {"scope_id": scope_id, "file_id": file_id, "revision_id": revision_id}


def _codegraph_artifacts(root: Path) -> dict[str, bytes]:
    base = root / ".memoryguard" / "codegraph"
    if not base.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(base.glob("*"))
        if path.is_file()
    }


def test_codegraph_v1_read_is_zero_write_and_native_update_migrates_preserving_rows(tmp_path: Path) -> None:
    _seed_v1_codegraph(tmp_path)
    context = _native_context(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11})
    before = _codegraph_artifacts(tmp_path)
    denied = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "legacyA", "provenance": "production"},
        context=context,
        generation=11,
    )
    assert denied["ok"] is False and denied["code"] == "codegraph_schema_upgrade_required"
    assert _codegraph_artifacts(tmp_path) == before

    updated = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"confirmed": True, "export": _export(), "full_snapshot": False},
        context=context,
        generation=11,
        mutation=True,
    )
    assert updated["ok"] is True, updated
    with open_database(WorkspaceV2Layout(tmp_path).codegraph_db, readonly=True, immutable=True) as conn:
        assert conn.execute("SELECT value FROM codegraph_schema_meta WHERE key='version'").fetchone()[0] == "2"
        source_cols = {row[1] for row in conn.execute("PRAGMA table_info(source_files)")}
        symbol_cols = {row[1] for row in conn.execute("PRAGMA table_info(symbols)")}
        edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
        assert {"source_role", "provenance"} <= source_cols
        assert {"provenance", "source_map_json", "metadata_json"} <= symbol_cols
        assert {"context", "provenance", "source_location", "metadata_json"} <= edge_cols
        assert conn.execute("SELECT COUNT(*) FROM source_files WHERE file_id='legacy-file'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM symbols WHERE symbol_id IN ('legacy-a','legacy-b')").fetchone()[0] == 2
        row = conn.execute("SELECT context,provenance,source_location FROM edges WHERE edge_id='legacy-edge'").fetchone()
        assert tuple(row) == ("", "production", "")


def test_codegraph_queries_are_physically_zero_write_and_future_aux_fails_closed(tmp_path: Path) -> None:
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    GraphifyExportAdapter(store).project(_export(), scope=scope)
    control = store.query_symbols("添加知识", scope=scope, provenance="production")[0]
    native = store.query_symbols("gui_knowledge_command", scope=scope, provenance="production")[0]
    before = _codegraph_artifacts(tmp_path)
    store.query_symbols("addBook", scope=scope, provenance="production")
    store.path_query(control.symbol_id, native.symbol_id, scope=scope, provenance="production", max_depth=8)
    store.explain_symbol(control.symbol_id, scope=scope, provenance="production")
    store.affected_query(native.symbol_id, scope=scope, provenance="production", depth=8)
    assert _codegraph_artifacts(tmp_path) == before

    with open_database(WorkspaceV2Layout(tmp_path).codegraph_db) as conn:
        conn.execute("UPDATE codegraph_schema_meta SET value='99' WHERE key='version'")
        conn.commit()
    future_before = _codegraph_artifacts(tmp_path)
    future = CodeGraphStore(tmp_path, initialize=False)
    with pytest.raises(Exception, match="unsupported codegraph schema marker"):
        future._preflight()
    assert _codegraph_artifacts(tmp_path) == future_before


def test_edge_identity_keeps_context_and_source_location(tmp_path: Path) -> None:
    payload = _export()
    payload["edges"].append({"source": "control", "target": "handler", "relation": "references", "context": "alternate_context", "source_location": "L11", "provenance": "production"})
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    GraphifyExportAdapter(store).project(payload, scope=scope)
    control = store.query_symbols("添加知识", scope=scope)[0]
    edges = [edge for edge in store.list_edges(scope=scope) if edge.from_id == control.symbol_id]
    assert {(edge.context, edge.source_location) for edge in edges} >= {("control_handler", "L10"), ("alternate_context", "L11")}
