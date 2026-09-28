from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from memoryguard.storage.database import connect_database, execute_sql_script, open_database
from memoryguard.storage.layout import LayoutError, WorkspaceV2Layout
from memoryguard.storage.schema import (
    SCHEMA_MARKER,
    SchemaError,
    initialize_all,
    initialize_database,
    initialize_domain,
)
from memoryguard.storage.transaction import Transaction, TransactionError, transaction
from memoryguard.system.manifest import ManifestError, ManifestManager, ManifestState


def test_snapshot_reads_uncheckpointed_wal_without_copying_live_shm(tmp_path, monkeypatch):
    from memoryguard.storage import database

    path = tmp_path / "live.db"
    live = connect_database(path)
    try:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE sample(value TEXT)")
        live.execute("INSERT INTO sample VALUES ('committed WAL value')")
        live.commit()
        before = {suffix: Path(str(path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        original_copy = database.shutil.copy2

        def copy_with_windows_shm_lock(source, target):
            if str(source).endswith("-shm"):
                raise PermissionError("WinError 33: SQLite owns the shared-memory locks")
            return original_copy(source, target)

        monkeypatch.setattr(database.shutil, "copy2", copy_with_windows_shm_lock)
        with database.open_database_snapshot(path) as snapshot:
            assert snapshot.execute("SELECT value FROM sample").fetchone()[0] == "committed WAL value"
        assert {suffix: Path(str(path) + suffix).read_bytes() for suffix in before} == before
    finally:
        live.close()
        database.release_database_snapshots()


def test_layout_paths_are_contained_and_exact(tmp_path: Path):
    layout = WorkspaceV2Layout(tmp_path)
    assert layout.runtime_db == tmp_path / ".memoryguard" / "runtime" / "runtime.db"
    assert layout.scenario_db.name == "scenario.db"
    assert layout.profile_db.name == "profile.db"
    assert layout.manifest_db.name == "manifest.db"
    assert all(layout.contains(path) for path in layout.all_db_paths)
    with pytest.raises(LayoutError):
        layout.assert_contained(tmp_path / "outside.db")


def test_all_databases_have_marker_and_foreign_keys(tmp_path: Path):
    layout = WorkspaceV2Layout(tmp_path)
    initialize_all(layout)
    for domain, path in layout.iter_db_paths():
        with open_database(path) as conn:
            rows = conn.execute("SELECT domain, version, marker FROM schema_meta").fetchall()
            assert rows and all(row[1] == 1 and row[2] == SCHEMA_MARKER for row in rows)
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_readonly_schema_probe_never_migrates(tmp_path: Path):
    layout = WorkspaceV2Layout(tmp_path)
    layout.ensure_dirs()
    path = layout.runtime_db
    initialize_database(path, "runtime", layout=layout)
    before = path.read_bytes()
    marker = initialize_database(path, "runtime", readonly=True)
    assert marker["marker"] == SCHEMA_MARKER
    assert path.read_bytes() == before


def test_transaction_failure_rolls_back_every_statement(tmp_path: Path):
    path = tmp_path / "tx.db"
    with open_database(path) as conn:
        conn.execute("CREATE TABLE t (value TEXT)")
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO t(value) VALUES ('one')")
                raise RuntimeError("boom")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_nested_transaction_does_not_commit_outer_implicitly(tmp_path: Path):
    path = tmp_path / "nested.db"
    with open_database(path) as conn:
        conn.execute("CREATE TABLE t (value TEXT)")
        with transaction(conn):
            conn.execute("INSERT INTO t(value) VALUES ('outer')")
            with transaction(conn):
                conn.execute("INSERT INTO t(value) VALUES ('inner')")
            assert conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_manual_transaction_commit_is_not_repeated_on_exit(tmp_path: Path):
    path = tmp_path / "manual.db"
    with open_database(path) as conn:
        conn.execute("CREATE TABLE t (value TEXT)")
        tx = Transaction(conn)
        with tx:
            conn.execute("INSERT INTO t(value) VALUES ('one')")
            tx.commit()
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_execute_sql_script_does_not_break_rollback(tmp_path: Path):
    path = tmp_path / "script.db"
    with open_database(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            with transaction(conn):
                execute_sql_script(
                    conn,
                    "CREATE TABLE should_rollback (id INTEGER);\n"
                    "CREATE TABLE should_rollback (id INTEGER);\n",
                )
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone() is None


def test_manifest_state_machine_and_idempotency(tmp_path: Path):
    manager = ManifestManager(tmp_path)
    manager.layout.ensure_dirs()
    assert manager.current().state is ManifestState.V1_ACTIVE
    with pytest.raises(ManifestError):
        manager.transition(ManifestState.V2_READY)
    building = manager.transition(ManifestState.V2_BUILDING, migration_id="m1")
    assert building.generation == 1
    assert manager.transition(ManifestState.V2_BUILDING).generation == 1
    manager.record_checkpoint({"inventory": "complete"}, migration_id="m1")
    manager.record_checkpoint({"content": "complete"}, migration_id="m1")
    with pytest.raises(ManifestError):
        manager.record_checkpoint({"inventory": "different"}, migration_id="m1")
    with pytest.raises(ManifestError):
        manager.transition(ManifestState.V2_ACTIVE, migration_id="m2", error="too soon")
    ready = manager.transition(
        ManifestState.V2_READY,
        migration_id="m1",
        source_digest="source",
        target_digest="target",
        manifest_digest="manifest",
        digests={"validator_passed": True, "checkpoints": {"integrity": "ok"}},
    )
    assert ready.generation == 2
    manager.transition(ManifestState.V2_ACTIVE)
    manager.fail(error="migration failed", migration_id="m4")
    assert manager.current().state is ManifestState.V1_ACTIVE
    assert manager.current().last_error == "migration failed"


def test_manifest_corrupt_json_and_ready_activation_fail_closed(tmp_path: Path):
    manager = ManifestManager(tmp_path)
    manager.transition(ManifestState.V2_BUILDING, migration_id="json-batch")
    with open_database(manager.db_path) as conn:
        conn.execute("UPDATE manifest SET digests_json='{' WHERE manifest_id='workspace'")
        conn.commit()
    with pytest.raises(ManifestError):
        manager.current()
    with pytest.raises(ManifestError):
        manager.activate_v2()


def test_future_schema_is_not_downgraded_or_mutated(tmp_path: Path):
    layout = WorkspaceV2Layout(tmp_path)
    initialize_all(layout)
    with open_database(layout.runtime_db) as conn:
        conn.execute("UPDATE schema_meta SET version=99, marker='future-marker'")
        conn.execute("PRAGMA user_version=99")
        conn.commit()
    before = layout.runtime_db.read_bytes()
    with pytest.raises(SchemaError):
        initialize_database(layout.runtime_db, "runtime", layout=layout)
    assert layout.runtime_db.read_bytes() == before
    with open_database(layout.runtime_db) as conn:
        row = conn.execute("SELECT version, marker FROM schema_meta").fetchone()
        assert tuple(row) == (99, "future-marker")


def test_v2_write_rejects_domain_symlink(tmp_path: Path):
    layout = WorkspaceV2Layout(tmp_path)
    layout.root.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    try:
        (layout.root / "runtime").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
    with pytest.raises(LayoutError):
        initialize_domain(layout, "runtime")


def test_manifest_pointers_are_persistent_and_immutable(tmp_path: Path):
    manager = ManifestManager(tmp_path)
    record = manager.begin(
        migration_id="pointer-batch",
        workspace_source_pointer=str(tmp_path),
        global_source_pointer=str(tmp_path / "data-home" / "knowledge" / "knowledge.db"),
        data_home_root=str(tmp_path / "data-home"),
    )
    assert record.workspace_source_pointer == str(tmp_path)
    assert manager.current().data_home_root == str(tmp_path / "data-home")
    with pytest.raises(ManifestError):
        manager.transition(
            ManifestState.V2_BUILDING,
            migration_id="pointer-batch",
            workspace_source_pointer=str(tmp_path / "other"),
            global_source_pointer=str(tmp_path / "data-home" / "knowledge" / "knowledge.db"),
            data_home_root=str(tmp_path / "data-home"),
        )


def test_failed_migration_id_cannot_be_reused(tmp_path: Path):
    manager = ManifestManager(tmp_path)
    manager.begin(migration_id="failed-batch")
    manager.fail(error="injected failure", migration_id="failed-batch")
    with pytest.raises(ManifestError):
        manager.begin(migration_id="failed-batch")
    assert manager.begin(migration_id="new-batch").migration_id == "new-batch"


def test_manifest_pointer_sentinel_and_containment_rules(tmp_path: Path):
    manager = ManifestManager(tmp_path)
    sentinel = manager.begin(
        migration_id="sentinel-batch",
        workspace_source_pointer=str(tmp_path),
        global_source_pointer="NOT_CONFIGURED",
        data_home_root="NOT_CONFIGURED",
    )
    assert sentinel.global_source_pointer == "NOT_CONFIGURED"
    assert sentinel.data_home_root == "NOT_CONFIGURED"

    other = ManifestManager(tmp_path / "other")
    with pytest.raises(ManifestError):
        other.begin(
            migration_id="mixed-sentinel",
            workspace_source_pointer=str(tmp_path / "other"),
            global_source_pointer="NOT_CONFIGURED",
            data_home_root=str(tmp_path / "data-home"),
        )
    with pytest.raises(ManifestError):
        other.begin(
            migration_id="relative-pointer",
            workspace_source_pointer="relative-workspace",
            global_source_pointer="NOT_CONFIGURED",
            data_home_root="NOT_CONFIGURED",
        )
    with pytest.raises(ManifestError):
        other.begin(
            migration_id="escaped-global",
            workspace_source_pointer=str(tmp_path / "other"),
            global_source_pointer=str(tmp_path / "outside" / "knowledge.db"),
            data_home_root=str(tmp_path / "data-home"),
        )
