from pathlib import Path

import pytest

from memoryguard.codegraph_v2.store import CodeGraphError, CodeGraphStore
from memoryguard.storage import database
from memoryguard import data_home


def test_repeated_graph_reads_and_preflight_do_not_copy_database(tmp_path, monkeypatch):
    store = CodeGraphStore(tmp_path)
    live = database.connect_database(store.db_path)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("CREATE TABLE read_probe(value TEXT)")
    live.execute("INSERT INTO read_probe VALUES ('committed WAL')")
    live.commit()

    def no_copy(*args, **kwargs):
        pytest.fail("ordinary CodeGraph reads must not copy the database")

    monkeypatch.setattr(database.shutil, "copy2", no_copy)
    try:
        for _ in range(5):
            reader = CodeGraphStore(tmp_path)
            with reader.connection() as conn:
                assert conn.execute("SELECT value FROM read_probe").fetchone()[0] == "committed WAL"
                with pytest.raises(database.sqlite3.OperationalError):
                    conn.execute("INSERT INTO read_probe VALUES ('forbidden')")
        assert not list(tmp_path.rglob("memoryguard-db-read-*"))
    finally:
        live.close()


def test_private_snapshot_stays_beside_source_and_is_released(tmp_path):
    source = tmp_path / ".memoryguard" / "runtime" / "runtime.db"
    with database.open_database(source) as conn:
        conn.execute("CREATE TABLE probe(value TEXT)")
        conn.execute("INSERT INTO probe VALUES ('original')")
        conn.commit()
    before = source.read_bytes()
    for _ in range(3):
        with database.open_database_snapshot(source) as conn:
            snapshot = Path(conn.execute("PRAGMA database_list").fetchone()[2])
            assert snapshot.is_relative_to(tmp_path / ".memoryguard" / "cache")
            assert conn.execute("SELECT value FROM probe").fetchone()[0] == "original"
        assert not snapshot.exists()
    assert source.read_bytes() == before


def test_overlapping_private_snapshots_survive_source_updates(tmp_path):
    source = tmp_path / "runtime.db"
    with database.open_database(source) as live:
        live.execute("CREATE TABLE probe(value INTEGER)")
        live.execute("INSERT INTO probe VALUES (1)")
        live.commit()
        with database.open_database_snapshot(source) as first:
            live.execute("INSERT INTO probe VALUES (2)")
            live.commit()
            with database.open_database_snapshot(source) as second:
                assert second.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 2
                assert first.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1


def test_graph_read_rejects_overlapping_checkpoint(tmp_path):
    store = CodeGraphStore(tmp_path)
    assert not Path(str(store.db_path) + "-wal").exists()
    with pytest.raises(CodeGraphError, match="changed_during_read"):
        with store.connection() as reader:
            reader.execute("SELECT COUNT(*) FROM graph_scopes").fetchone()
            with database.open_database(store.db_path) as writer:
                writer.execute("CREATE TABLE overlapping_checkpoint(value INTEGER)")
                writer.commit()


def test_global_scratch_follows_deployment_instead_of_control_data(tmp_path, monkeypatch):
    control = tmp_path / "control-data"
    deployment = tmp_path / "mg-deployment"
    monkeypatch.setattr(data_home, "resolve_runtime_data_home", lambda: control)
    monkeypatch.setattr(data_home, "resolve_deployment_home", lambda: deployment)
    source = control / ".memoryguard" / "system" / "manifest.db"
    with database.open_database(source) as conn:
        conn.execute("CREATE TABLE probe(value INTEGER)")
        conn.commit()
    with database.open_database_snapshot(source) as conn:
        scratch = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        assert scratch.is_relative_to(deployment / ".memoryguard" / "cache")
    assert not scratch.exists()
    assert not list(control.rglob("memoryguard-db-read-*"))


def test_default_runtime_snapshot_follows_source_project(tmp_path, monkeypatch):
    from memoryguard.provider_adapters import prepare_provider_mcp_launch, _venv_python

    source = tmp_path / "mg-project"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='agent-memguard'\n", encoding="utf-8")
    monkeypatch.delenv("MEMORYGUARD_RUNTIME_PYTHON", raising=False)
    captured = []

    def build(*, snapshot_root, source_root):
        captured.append(snapshot_root)
        python = _venv_python(snapshot_root)
        python.parent.mkdir(parents=True)
        python.write_text("fixture", encoding="utf-8")
        return str(python)

    result = prepare_provider_mcp_launch(
        mutate=True, source_root=source, builder=build,
        origin={"install_kind": "editable", "editable": True},
    )
    assert result["ok"]
    assert captured[0].parent == source / ".memoryguard" / "mcp-runtime"


def test_installer_scratch_is_scoped_and_cleaned(tmp_path, monkeypatch):
    import subprocess
    from memoryguard.provider_adapters import _run_snapshot_command

    monkeypatch.setattr(data_home, "resolve_cache_home", lambda: tmp_path / "cache")
    captured = []

    def run(argv, **kwargs):
        env = kwargs["env"]
        temporary = Path(env["TEMP"])
        assert temporary.is_relative_to(tmp_path / "cache") and temporary.is_dir()
        assert env["TMP"] == env["TMPDIR"] == env["TEMP"]
        assert env["PIP_NO_CACHE_DIR"] == "1"
        captured.append(temporary)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    _run_snapshot_command(["fixture-installer"])
    assert not captured[0].exists()
