from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from memoryguard import __version__
from memoryguard.cli import main as cli_main
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.system.manifest import ManifestManager, ManifestState


def _legacy_0_6_2_fixture(root: Path) -> Path:
    """Create the smallest real on-disk V1 shape emitted by 0.6.2."""

    group_db = root / ".memoryguard" / "shared-memory" / "shared-team" / "memory.db"
    group_db.parent.mkdir(parents=True, exist_ok=True)
    body = "keep this V1 memory"
    with sqlite3.connect(group_db) as conn:
        conn.execute(
            "CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, supersedes TEXT, "
            "provenance TEXT, agent_instance_id TEXT, created_at TEXT, updated_at TEXT, canonical_hash TEXT, "
            "dedup_domain TEXT)"
        )
        conn.execute(
            "CREATE TABLE rule_assignments(memory_id TEXT, target_type TEXT, target_id TEXT, project_ref TEXT, "
            "effect TEXT, priority_override INTEGER, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memory-1",
                body,
                "fact",
                "active",
                0.9,
                1,
                "always",
                2,
                "[]",
                "[]",
                "agent-1",
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
                hashlib.sha256(body.encode()).hexdigest(),
                "relevant",
            ),
        )
        conn.commit()
    # sqlite3.Connection's context manager commits/rolls back but does not
    # close the handle.  Windows cannot retire the migrated V1 source while
    # this fixture-owned connection remains open.
    conn.close()

    binding = root / ".memoryguard" / "agent-bindings" / "binding-1.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps(
            {
                "binding_id": "binding-1",
                "agent_instance_id": "agent-1",
                "share_group_id": "shared-team",
                "mcp_server_name": "memoryguard",
                "native_memory_mode": "observed",
                "status": "active",
                "redirect_paths": [],
                "bound_at": "2026-08-01T00:00:00+00:00",
                "last_drift_check": "",
            }
        ),
        encoding="utf-8",
    )
    return binding


def _run_upgrade(root: Path, *, apply: bool = False, confirm: str | None = None, capsys=None):
    args = ["upgrade", "--workspace", str(root), "--data-home", str(root)]
    if apply:
        args.append("--apply")
    else:
        args.append("--preview")
    if confirm is not None:
        args.extend(["--confirm", confirm])
    code = cli_main(args)
    output = json.loads(capsys.readouterr().out)
    return code, output


def test_public_upgrade_0_6_2_global_workspace_is_zero_write_then_explicitly_active(tmp_path: Path, capsys) -> None:
    assert __version__ == "0.7.13"
    root = tmp_path / "global-memoryguard-home"
    binding = _legacy_0_6_2_fixture(root)
    before_binding = binding.read_bytes()
    manifest_path = WorkspaceV2Layout(root).manifest_db

    code, preview = _run_upgrade(root, capsys=capsys)
    assert code == 0
    assert preview["status"] == "PREVIEW"
    assert preview["ok"] is True
    assert preview["writes_performed"] is False
    assert preview["workspace_equals_data_home"] is True
    assert preview["state"] == ManifestState.V1_ACTIVE.value
    assert not manifest_path.exists()
    assert binding.read_bytes() == before_binding

    code, ready_report = _run_upgrade(root, apply=True, capsys=capsys)
    assert code == 0, ready_report
    assert ready_report["status"] == ManifestState.V2_READY.value
    assert ready_report["ok"] is True
    assert ready_report["activation_required"] is True
    assert ready_report["stages"]["prepare"]["status"] == "PASS"
    assert ready_report["stages"]["gui_control"]["status"] == "PASS"
    assert ready_report["stages"]["verify"]["status"] == "PASS"
    assert ready_report["stages"]["activate"]["status"] == "PENDING_CONFIRMATION"

    manager = ManifestManager(root)
    ready = manager.current()
    assert ready.state is ManifestState.V2_READY
    assert GroupControlService(root).active_binding_for_agent("agent-1")["share_group_id"] == "shared-team"
    with sqlite3.connect(WorkspaceV2Layout(root).memory_db) as conn:
        assert conn.execute("SELECT body FROM atoms WHERE memory_id='memory-1'").fetchone()[0] == "keep this V1 memory"

    code, blocked = _run_upgrade(root, apply=True, confirm="not-V2_ACTIVE", capsys=capsys)
    assert code == 2
    assert blocked["code"] == "activation_confirmation_mismatch"
    assert manager.current().state is ManifestState.V2_READY

    code, active_report = _run_upgrade(root, apply=True, confirm="V2_ACTIVE", capsys=capsys)
    assert code == 0, active_report
    assert active_report["status"] == ManifestState.V2_ACTIVE.value
    assert active_report["v2_active"] is True
    active = manager.current()
    assert active.state is ManifestState.V2_ACTIVE

    code, replay = _run_upgrade(root, apply=True, capsys=capsys)
    assert code == 0
    assert replay["status"] == ManifestState.V2_ACTIVE.value
    assert replay["code"] == "already_active"
    assert replay["generation"] == active.generation
    assert manager.current().generation == active.generation


def test_bare_public_upgrade_completes_verified_activation(tmp_path: Path, capsys) -> None:
    root = tmp_path / "one-command-upgrade"
    _legacy_0_6_2_fixture(root)

    code = cli_main([
        "upgrade", "--workspace", str(root), "--data-home", str(root),
    ])
    report = json.loads(capsys.readouterr().out)

    assert code == 0, report
    assert report["status"] == ManifestState.V2_ACTIVE.value
    assert report["v2_active"] is True
    assert report["stages"]["verify"]["status"] == "PASS"
    assert report["stages"]["activate"]["status"] == "PASS"
    assert ManifestManager(root).current().state is ManifestState.V2_ACTIVE


def test_bare_upgrade_with_explicit_data_home_never_reads_or_retires_ambient_legacy_source(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    ambient = tmp_path / "ambient-legacy-project"
    binding = _legacy_0_6_2_fixture(ambient)
    legacy_db = ambient / ".memoryguard" / "shared-memory" / "shared-team" / "memory.db"
    before_binding = binding.read_bytes()
    before_db = legacy_db.read_bytes()
    nested_cwd = ambient / "nested" / "project"
    nested_cwd.mkdir(parents=True)
    source = tmp_path / "explicit-v1-source"
    _legacy_0_6_2_fixture(source)
    target = tmp_path / "canonical-v2-data-home"
    source_db = source / ".memoryguard" / "shared-memory" / "shared-team" / "memory.db"
    before_source_db = source_db.read_bytes()

    monkeypatch.chdir(nested_cwd)
    monkeypatch.delenv("MEMORYGUARD_HOME", raising=False)

    def unexpected_discovery(*_args, **_kwargs):
        raise AssertionError("explicit --data-home must not inspect ambient migration sources")

    monkeypatch.setattr("memoryguard.cli.discover_migration_source", unexpected_discovery)
    monkeypatch.setattr(
        "memoryguard.data_home.resolve_runtime_data_home",
        lambda: target.resolve(),
    )

    preview_code = cli_main(["upgrade", "--data-home", str(source), "--preview"])
    preview = json.loads(capsys.readouterr().out)

    assert preview_code == 0, preview
    preview_hashes = preview["stages"]["preflight"]["detail"]["prepare"]["build"]["source_hashes"]
    assert preview_hashes["validator:memory:shared-team"] == hashlib.sha256(before_source_db).hexdigest()
    assert not target.exists()
    assert source_db.read_bytes() == before_source_db
    assert binding.read_bytes() == before_binding
    assert legacy_db.read_bytes() == before_db

    code = cli_main(["upgrade", "--data-home", str(source)])
    report = json.loads(capsys.readouterr().out)

    assert code == 0, report
    assert report["workspace"] == str(target.resolve())
    assert report["data_home"] == str(source.resolve())
    assert report["status"] == ManifestState.V2_ACTIVE.value
    assert report["v2_active"] is True
    assert ManifestManager(target).current().state is ManifestState.V2_ACTIVE
    assert GroupControlService(target).active_binding_for_agent("agent-1")["share_group_id"] == "shared-team"
    with sqlite3.connect(WorkspaceV2Layout(target).memory_db) as conn:
        assert conn.execute("SELECT body FROM atoms WHERE memory_id='memory-1'").fetchone()[0] == "keep this V1 memory"

    assert binding.is_file()
    assert legacy_db.is_file()
    assert binding.read_bytes() == before_binding
    assert legacy_db.read_bytes() == before_db


def test_public_upgrade_control_failure_stays_ready_and_never_activates(tmp_path: Path, capsys) -> None:
    root = tmp_path / "global-memoryguard-home"
    binding = _legacy_0_6_2_fixture(root)
    code, ready = _run_upgrade(root, apply=True, capsys=capsys)
    assert code == 0, ready
    assert ManifestManager(root).current().state is ManifestState.V2_READY

    changed = json.loads(binding.read_text(encoding="utf-8"))
    changed["share_group_id"] = "changed-group"
    binding.write_text(json.dumps(changed), encoding="utf-8")

    code, failed = _run_upgrade(root, apply=True, confirm="V2_ACTIVE", capsys=capsys)
    assert code == 2
    assert failed["stage"] == "gui_control"
    assert failed["code"] == "idempotency_key_reused"
    assert failed["activation_required"] is True
    assert ManifestManager(root).current().state is ManifestState.V2_READY


def test_public_upgrade_repairs_missing_v1_bindings_after_premature_v2_activation(
    tmp_path: Path, capsys
) -> None:
    """V2_ACTIVE is not healthy when legacy Agent bindings were never projected."""

    root = tmp_path / "global-memoryguard-home"
    _legacy_0_6_2_fixture(root)
    code, ready = _run_upgrade(root, apply=True, capsys=capsys)
    assert code == 0, ready
    code, active = _run_upgrade(root, apply=True, confirm="V2_ACTIVE", capsys=capsys)
    assert code == 0, active

    control = WorkspaceV2Layout(root).manifest_db
    with sqlite3.connect(control) as conn:
        conn.execute("DELETE FROM agent_group_bindings")
        conn.execute(
            "DELETE FROM group_operation_receipts WHERE operation='migrate_legacy_agent_bindings'"
        )
        conn.execute("DELETE FROM group_outbox WHERE event_type='migrate_legacy_agent_bindings'")
        conn.commit()
    assert GroupControlService(root).list_bindings(include_inactive=True)["total"] == 0

    code, preview = _run_upgrade(root, capsys=capsys)
    assert code == 2
    assert preview["code"] == "active_control_repair_required"
    assert preview["writes_performed"] is False
    assert preview["detail"]["missing_binding_ids"] == ["binding-1"]

    code, repaired = _run_upgrade(root, apply=True, capsys=capsys)
    assert code == 0, repaired
    assert repaired["status"] == ManifestState.V2_ACTIVE.value
    assert repaired["code"] == "active_control_repaired"
    assert repaired["writes_performed"] is True
    binding = GroupControlService(root).active_binding_for_agent("agent-1")
    assert binding is not None
    assert binding["share_group_id"] == "shared-team"
