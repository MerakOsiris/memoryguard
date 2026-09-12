from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from memoryguard.usage_telemetry import (
    get_usage_summary,
    record_conversion_event,
    sync_usage_telemetry,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _codex_token_row(timestamp: str, input_tokens: int, output_tokens: int) -> dict:
    total = input_tokens + output_tokens
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 2,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 1,
                    "total_tokens": total,
                }
            },
        },
    }


def test_sync_is_idempotent_and_keeps_measured_tokens_separate(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    grok_home = tmp_path / "grok"
    codex_row = _codex_token_row("2026-08-28T01:02:03Z", 100, 20)
    codex_usage = codex_row["payload"]["info"]["last_token_usage"]
    codex_usage.pop("cached_input_tokens")
    codex_usage["input_tokens_details"] = {
        "cached_tokens": 0,
        "cache_write_tokens": 0,
    }
    _write_jsonl(
        codex_home / "sessions/2026/08/28/rollout-abc123.jsonl",
        [codex_row],
    )
    _write_jsonl(
        grok_home / "logs/unified.jsonl",
        [
            {
                "ts": "2026-08-28T01:02:04Z",
                "msg": "shell.turn.inference_done",
                "ctx": {"prompt_tokens": 50, "cached_prompt_tokens": 5, "completion_tokens": 10},
            }
        ],
    )

    first = sync_usage_telemetry(
        tmp_path / "workspace",
        codex_home=codex_home,
        grok_home=grok_home,
        now_utc="2026-08-28T02:00:00Z",
    )
    second = sync_usage_telemetry(
        tmp_path / "workspace",
        codex_home=codex_home,
        grok_home=grok_home,
        now_utc="2026-08-28T02:01:00Z",
    )
    assert first["inserted"] == 2
    assert second["inserted"] == 0

    summary = get_usage_summary(tmp_path / "workspace", window_days=7, now_utc="2026-08-28T02:02:00Z")
    assert summary["schema_version"] == 2
    assert summary["window_days"] == 7
    assert summary["summary"]["measured_input"] == 150
    assert summary["summary"]["measured_cached_input"] == 5
    assert summary["summary"]["measured_cache_write_input"] == 0
    assert summary["summary"]["measured_output"] == 30
    # Grok reports prompt/completion fields, not a provider total.  Keep the
    # verified fields separate rather than presenting their sum as one.
    assert summary["summary"]["measured_total"] == 120
    assert summary["summary"]["measured_derived_total"] == 180
    assert summary["summary"]["measured_provider_total_event_count"] == 1
    assert summary["summary"]["measured_derived_total_event_count"] == 2
    assert summary["summary"]["measured_total_coverage"] == {
        "provider_reported": "partial",
        "input_output_derived": "complete",
        "measured_event_count": 2,
    }
    assert summary["summary"]["measured_cache_coverage"] == {
        "cache_read": "complete",
        "cache_write": "partial",
        "measured_event_count": 2,
    }
    assert summary["summary"]["measured_event_count"] == 2
    assert summary["summary"]["estimated_baseline_units"] is None
    assert summary["summary"]["estimated_delivered_units"] is None
    assert summary["summary"]["estimated_ratio"] is None
    assert {row["program"] for row in summary["rows"]} == {"codex", "grok"}
    assert all("abc123" not in json.dumps(row) for row in summary["rows"])


def test_conversion_event_uses_deterministic_basis_for_ratio(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="codex",
        program="Codex",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=100,
        delivered_units=40,
        measurement_basis="mg_deterministic_unit",
        event_id="conversion-1",
    )
    record_conversion_event(
        workspace,
        provider="codex",
        program="Codex",
        observed_at_utc="2026-08-28T01:01:00Z",
        baseline_units=10,
        delivered_units=5,
        measurement_basis="other_unit",
        event_id="conversion-2",
    )
    summary = get_usage_summary(workspace, window_days=30, now_utc="2026-08-28T02:00:00Z")
    assert summary["summary"]["conversion_count"] == 2
    assert summary["summary"]["estimated_baseline_units"] == 100
    assert summary["summary"]["estimated_delivered_units"] == 40
    assert summary["summary"]["estimated_saved_units"] == 60
    assert summary["summary"]["estimated_ratio"] == 0.6
    assert summary["summary"]["savings_ratio"] == 0.6
    assert summary["summary"]["measured_total"] is None


def test_old_usage_schema_migrates_cache_write_and_sync_remains_idempotent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    database = workspace / ".memoryguard" / "usage_telemetry.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        # Pre-cache-write v2 schema: migration must add only new nullable field.
        connection.execute(
            """
            CREATE TABLE usage_events (
                event_key TEXT PRIMARY KEY,
                event_kind TEXT NOT NULL,
                provider TEXT NOT NULL,
                program TEXT NOT NULL,
                agent_stable_key TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                source_generation INTEGER NOT NULL DEFAULT 0,
                source_offset INTEGER,
                source_ordinal INTEGER,
                observed_at_utc TEXT NOT NULL,
                measurement_basis TEXT NOT NULL,
                input_tokens INTEGER,
                cached_input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_output_tokens INTEGER,
                total_tokens INTEGER,
                baseline_units INTEGER,
                delivered_units INTEGER,
                conversion_count INTEGER NOT NULL DEFAULT 0,
                share_group_hash TEXT,
                project_ref_hash TEXT,
                scope_kind TEXT NOT NULL DEFAULT 'host'
            )
            """
        )
        connection.commit()

    codex_home = tmp_path / "codex"
    row = _codex_token_row("2026-08-28T01:00:00Z", 12, 3)
    row["payload"]["info"]["last_token_usage"]["cache_write_tokens"] = 7
    _write_jsonl(
        codex_home / "sessions/2026/08/28/rollout-cache-write.jsonl",
        [row],
    )

    first = sync_usage_telemetry(
        workspace,
        codex_home=codex_home,
        grok_home=tmp_path / "grok",
        now_utc="2026-08-28T02:00:00Z",
    )
    second = sync_usage_telemetry(
        workspace,
        codex_home=codex_home,
        grok_home=tmp_path / "grok",
        now_utc="2026-08-28T02:01:00Z",
    )

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    with sqlite3.connect(database) as connection:
        columns = {item[1] for item in connection.execute("PRAGMA table_info(usage_events)")}
        stored = connection.execute(
            "SELECT cache_write_input_tokens FROM usage_events"
        ).fetchone()[0]
    assert "cache_write_input_tokens" in columns
    assert stored == 7

    summary = get_usage_summary(workspace, window_days=7, now_utc="2026-08-28T02:02:00Z")
    assert summary["summary"]["measured_cache_write_input"] == 7
    assert summary["summary"]["measured_event_count"] == 1


def test_truncated_source_rotates_generation_without_duplicate(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions/2026/08/28/rollout-rotate.jsonl"
    _write_jsonl(rollout, [_codex_token_row("2026-08-28T01:00:00Z", 100000, 20000)])
    workspace = tmp_path / "workspace"
    first = sync_usage_telemetry(
        workspace,
        codex_home=codex_home,
        grok_home=tmp_path / "grok",
        now_utc="2026-08-28T01:01:00Z",
    )
    assert first["inserted"] == 1
    _write_jsonl(rollout, [_codex_token_row("2026-08-28T02:00:00Z", 2, 1)])
    second = sync_usage_telemetry(
        workspace,
        codex_home=codex_home,
        grok_home=tmp_path / "grok",
        now_utc="2026-08-28T02:01:00Z",
    )
    assert second["inserted"] == 1
    summary = get_usage_summary(workspace, window_days=7, now_utc="2026-08-28T03:00:00Z")
    assert summary["summary"]["measured_total"] == 120003


def test_contract_exposes_unavailable_agents_and_does_not_store_sensitive_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="Grok",
        program="Grok",
        agent_stable_key="grok",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=8,
        delivered_units=4,
        measurement_basis="mg_deterministic_unit",
        technical_source="C:/secret/account/path",
        event_id="conversion-sensitive",
    )
    summary = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        agent_roster=["grok", "claude", "cursor", "trae"],
    )
    assert {agent["program"] for agent in summary["agents"]} >= {"grok", "claude", "cursor", "trae"}
    assert summary["summary"]["available_agent_count"] == 1
    assert summary["summary"]["unavailable_agent_count"] == 3
    db_path = workspace / ".memoryguard" / "usage_telemetry.sqlite"
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
        values = [str(row) for row in connection.execute("SELECT * FROM usage_events")]
    assert "body" not in columns
    assert "account" not in columns
    assert all("secret" not in value and "account/path" not in value for value in values)


def test_conversion_scope_filters_without_hiding_host_measurements(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="codex",
        program="Codex",
        share_group_id="shared-one",
        project_ref="project-one",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=100,
        delivered_units=40,
        event_id="scope-one",
    )
    record_conversion_event(
        workspace,
        provider="codex",
        program="Codex",
        share_group_id="shared-two",
        project_ref="project-two",
        observed_at_utc="2026-08-28T01:01:00Z",
        baseline_units=900,
        delivered_units=800,
        event_id="scope-two",
    )
    codex_home = tmp_path / "codex"
    _write_jsonl(
        codex_home / "sessions/2026/08/28/rollout-host.jsonl",
        [_codex_token_row("2026-08-28T01:02:00Z", 30, 5)],
    )
    sync_usage_telemetry(workspace, codex_home=codex_home, grok_home=tmp_path / "grok")

    summary = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        share_group_id="shared-one",
        project_ref="project-one",
        agent_roster=[{"provider": "codex", "program": "Codex"}],
    )
    assert summary["summary"]["measured_total"] == 35
    assert summary["summary"]["estimated_baseline_units"] == 100
    assert summary["summary"]["estimated_delivered_units"] == 40
    assert summary["summary"]["estimated_saved_units"] == 60
    assert summary["scope"]["share_group_id"] == "shared-one"
    assert summary["scope"]["project_ref"] == "project-one"
    with sqlite3.connect(workspace / ".memoryguard" / "usage_telemetry.sqlite") as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_events)")}
        stored = " ".join(str(row) for row in connection.execute("SELECT * FROM usage_events"))
    assert {"share_group_hash", "project_ref_hash", "scope_kind"} <= columns
    assert "shared-one" not in stored and "project-one" not in stored


def test_dynamic_roster_has_no_phantom_agents_and_rows_are_date_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="Grok",
        program="Grok",
        observed_at_utc="2026-08-27T01:00:00Z",
        baseline_units=20,
        delivered_units=5,
        event_id="grok-roster",
    )
    summary = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        agent_roster=[
            {"provider": "codex", "program": "Codex"},
            {"provider": "grok", "program": "Grok"},
            {"provider": "claude", "program": "Claude"},
        ],
    )
    assert {agent["program"] for agent in summary["agents"]} == {"codex", "grok", "claude"}
    assert {agent["measurement_state"] for agent in summary["agents"]} == {"estimated", "unavailable"}
    assert all(row["date"] == "2026-08-27" and row["program"] == "grok" for row in summary["rows"])
    assert all("measurement_state" in row for row in summary["rows"])
    assert len(summary["series"]) == 7


def test_half_jsonl_line_does_not_advance_cursor(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions/2026/08/28/rollout-partial.jsonl"
    first_row = json.dumps(_codex_token_row("2026-08-28T01:00:00Z", 10, 2)) + "\n"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(first_row, encoding="utf-8")
    workspace = tmp_path / "workspace"
    first = sync_usage_telemetry(workspace, codex_home=codex_home, grok_home=tmp_path / "grok")
    assert first["inserted"] == 1
    second_row = (json.dumps(_codex_token_row("2026-08-28T02:00:00Z", 3, 1)) + "\n").encode()
    split = len(second_row) // 2
    with rollout.open("ab") as handle:
        handle.write(second_row[:split])
    second = sync_usage_telemetry(workspace, codex_home=codex_home, grok_home=tmp_path / "grok")
    assert second["inserted"] == 0
    with sqlite3.connect(workspace / ".memoryguard" / "usage_telemetry.sqlite") as connection:
        cursor = connection.execute("SELECT byte_offset FROM usage_cursors").fetchone()[0]
    assert cursor < len(rollout.read_bytes())
    with rollout.open("ab") as handle:
        handle.write(second_row[split:])
    third = sync_usage_telemetry(workspace, codex_home=codex_home, grok_home=tmp_path / "grok")
    assert third["inserted"] == 1


def test_state_rollouts_are_authoritative_over_glob_fallback(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    selected = codex_home / "selected.jsonl"
    fallback = codex_home / "sessions/2026/08/28/rollout-fallback.jsonl"
    _write_jsonl(selected, [_codex_token_row("2026-08-28T01:00:00Z", 10, 2)])
    _write_jsonl(fallback, [_codex_token_row("2026-08-28T01:01:00Z", 20, 2)])
    with sqlite3.connect(codex_home / "state_5.sqlite") as state:
        state.execute("CREATE TABLE threads (rollout_path TEXT)")
        state.execute("INSERT INTO threads VALUES (?)", (str(selected),))
        state.commit()
    result = sync_usage_telemetry(tmp_path / "workspace", codex_home=codex_home, grok_home=tmp_path / "grok")
    assert result["sources"] == 1
    assert result["inserted"] == 1


def test_sync_exposes_state_and_sqlite_uses_wal(tmp_path: Path) -> None:
    result = sync_usage_telemetry(tmp_path / "workspace", codex_home=tmp_path / "codex", grok_home=tmp_path / "grok")
    assert result["sync_state"]["status"] == "no_measured_source"
    assert result["sync_state"]["providers"]["codex"]["status"] == "source_not_found"
    assert result["sync_state"]["providers"]["claude"]["status"] == "host_not_supported"
    assert result["sync_state"]["last_success_at"]
    with sqlite3.connect(tmp_path / "workspace" / ".memoryguard" / "usage_telemetry.sqlite") as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000


def test_legacy_sync_state_is_publicly_whitelisted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="codex",
        program="codex",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=8,
        delivered_units=4,
        event_id="legacy-sync-state",
    )
    database = workspace / ".memoryguard" / "usage_telemetry.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO usage_sync_state "
            "(provider, status, last_success_at, last_error_at, last_error, "
            "inserted_count, rotated_count, source_count) VALUES (?, ?, ?, ?, ?, 0, 0, 0)",
            (
                r"C:\\Users\\private\\account\\provider",
                "totally-unknown-status",
                r"C:\\Users\\private\\success",
                r"C:\\Users\\private\\error",
                r"RuntimeError: token=/secret/account",
            ),
        )
        connection.commit()

    summary = get_usage_summary(workspace, now_utc="2026-08-28T02:00:00Z")
    encoded = json.dumps(summary, ensure_ascii=False)
    assert r"C:\\Users\\private" not in encoded
    assert "RuntimeError" not in encoded
    assert "/secret/account" not in encoded
    assert summary["sync_state"]["providers"] == {
        "unknown": {
            "status": "unavailable",
            "last_success_at": None,
            "last_error_at": None,
            "last_error": "sync_failed",
            "inserted_count": 0,
            "rotated_count": 0,
            "source_count": 0,
        }
    }


def test_usage_query_missing_database_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    summary = get_usage_summary(workspace, now_utc="2026-08-28T02:00:00Z")

    assert summary["status"] == "unavailable"
    assert summary["summary"]["measured_total"] is None
    assert not (workspace / ".memoryguard").exists()


def test_usage_query_old_schema_fails_closed_without_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    database = workspace / ".memoryguard" / "usage_telemetry.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE usage_events (event_key TEXT PRIMARY KEY)")
        connection.commit()

    summary = get_usage_summary(workspace, now_utc="2026-08-28T02:00:00Z")

    assert summary["status"] == "unavailable"
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {"usage_events"}


def test_host_sync_skips_invalid_timestamps_and_summary_excludes_future_events(
    tmp_path: Path, monkeypatch
) -> None:
    codex_home = tmp_path / "codex"
    missing_timestamp = _codex_token_row("2026-08-28T02:00:00Z", 40, 5)
    missing_timestamp.pop("timestamp")
    monkeypatch.setattr(
        "memoryguard.usage_telemetry._utc_now",
        lambda: datetime(2026, 8, 28, 2, tzinfo=timezone.utc),
    )
    _write_jsonl(
        codex_home / "sessions/2026/08/28/rollout-time-window.jsonl",
        [
            _codex_token_row("2026-08-28T01:00:00Z", 10, 2),
            _codex_token_row("not-a-timestamp", 20, 3),
            missing_timestamp,
            _codex_token_row("2026-08-28T03:00:00Z", 30, 4),
        ],
    )
    workspace = tmp_path / "workspace"

    sync_usage_telemetry(
        workspace,
        codex_home=codex_home,
        grok_home=tmp_path / "grok",
        now_utc="2026-08-28T02:00:00Z",
    )
    summary = get_usage_summary(workspace, now_utc="2026-08-28T02:00:00Z")

    assert summary["summary"]["measured_input"] == 10
    assert summary["summary"]["measured_output"] == 2
    assert summary["summary"]["measured_total"] == 12


def test_share_group_aggregates_conversions_across_project_refs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="codex",
        program="codex",
        share_group_id="shared-one",
        project_ref="H:/other/project-a",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=100,
        delivered_units=40,
        event_id="cross-a",
    )
    record_conversion_event(
        workspace,
        provider="codex",
        program="codex",
        share_group_id="shared-one",
        project_ref="H:/other/project-b",
        observed_at_utc="2026-08-28T01:01:00Z",
        baseline_units=50,
        delivered_units=20,
        event_id="cross-b",
    )
    grouped = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        share_group_id="shared-one",
    )
    assert grouped["status"] == "available"
    assert grouped["summary"]["estimated_baseline_units"] == 150
    assert grouped["summary"]["estimated_delivered_units"] == 60
    assert grouped["summary"]["conversion_count"] == 2
    assert grouped["summary"]["measured_total"] is None
    assert grouped["scope"]["project_ref"] == ""
    assert grouped["scope"]["conversion_scope"] == "filtered"
    one_project = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        share_group_id="shared-one",
        project_ref="H:/other/project-a",
    )
    assert one_project["summary"]["estimated_baseline_units"] == 100
    assert one_project["summary"]["conversion_count"] == 1


def test_estimated_without_measured_is_available_not_zero(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="codex",
        program="codex",
        share_group_id="shared-one",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=80,
        delivered_units=30,
        event_id="estimate-only",
    )
    summary = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        share_group_id="shared-one",
        agent_roster=[
            {"provider": "codex", "program": "codex"},
            {"provider": "claude", "program": "claude"},
        ],
    )
    assert summary["status"] == "available"
    assert summary["measurement_state"] == "estimated"
    assert summary["summary"]["estimated_baseline_units"] == 80
    assert summary["summary"]["estimated_delivered_units"] == 30
    assert summary["summary"]["estimated_saved_units"] == 50
    assert summary["summary"]["conversion_count"] == 1
    assert summary["summary"]["measured_total"] is None
    assert summary["summary"]["measured_input"] is None
    codex = next(agent for agent in summary["agents"] if agent["agent_key"] == "codex:codex")
    assert codex["measurement_state"] == "estimated"
    assert codex["conversion_count"] == 1
    assert summary["rows"][0]["measurement_state"] == "estimated"
    claude = next(agent for agent in summary["agents"] if agent["agent_key"] == "claude:claude")
    assert claude["measurement_state"] == "unavailable"
    assert claude["measured_total"] is None


def test_roster_placeholder_does_not_overwrite_real_codex_row(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider="codex",
        program="codex",
        share_group_id="shared-one",
        project_ref="other-project",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=18140,
        delivered_units=14236,
        event_id="real-codex",
    )
    summary = get_usage_summary(
        workspace,
        window_days=7,
        now_utc="2026-08-28T02:00:00Z",
        share_group_id="shared-one",
        agent_roster=[
            {"provider": "codex", "program": "codex", "display_name": "Codex"},
            {"provider": "claude", "program": "claude", "display_name": "Claude"},
            {"provider": "cursor", "program": "cursor", "display_name": "Cursor"},
            {"provider": "trae", "program": "trae", "display_name": "Trae"},
        ],
    )
    by_key = {agent["agent_key"]: agent for agent in summary["agents"]}
    assert by_key["codex:codex"]["measurement_state"] == "estimated"
    assert by_key["codex:codex"]["estimated_baseline_units"] == 18140
    assert by_key["codex:codex"]["conversion_count"] == 1
    assert summary["rows"]
    assert all(row["agent_key"] == "codex:codex" for row in summary["rows"])
    assert all(row["measurement_state"] == "estimated" for row in summary["rows"])
    for key in ("claude:claude", "cursor:cursor", "trae:trae"):
        assert by_key[key]["measurement_state"] == "unavailable"
        assert by_key[key]["estimated_baseline_units"] is None
        assert by_key[key]["measured_total"] is None


def test_sync_records_provider_specific_status_instead_of_zero(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = sync_usage_telemetry(
        workspace,
        codex_home=tmp_path / "missing-codex",
        grok_home=tmp_path / "missing-grok",
        now_utc="2026-08-28T02:00:00Z",
    )
    providers = result["sync_state"]["providers"]
    assert providers["codex"]["status"] == "source_not_found"
    assert providers["grok"]["status"] == "source_not_found"
    assert providers["claude"]["status"] == "host_not_supported"
    assert providers["cursor"]["status"] == "host_not_supported"
    assert providers["trae"]["status"] == "host_not_supported"
    assert providers["claude"]["last_error"] == "host_does_not_report_tokens"
    assert result["sync_state"]["status"] != "success"
    summary = get_usage_summary(workspace, window_days=7, now_utc="2026-08-28T02:01:00Z")
    assert summary["status"] == "unavailable"
    by_provider = {
        agent["provider"]: agent
        for agent in summary["agents"]
        if agent["provider"] in {"claude", "cursor", "trae", "codex", "grok"}
    }
    # Empty event set still exposes provider-specific reasons, not a fake 0.
    for provider in ("claude", "cursor", "trae"):
        assert by_provider[provider]["host_measurement_status"] == "host_not_supported"
        assert by_provider[provider]["host_measurement_reason"] == "host_does_not_report_tokens"
        assert by_provider[provider]["measured_total"] is None
    assert by_provider["codex"]["host_measurement_status"] == "source_not_found"
    assert by_provider["codex"]["host_measurement_reason"] == "source_not_detected"


def test_sync_uses_discovered_codex_home_when_unspecified(tmp_path: Path, monkeypatch) -> None:
    from memoryguard import usage_telemetry as module

    discovered = tmp_path / "router-codex"
    _write_jsonl(
        discovered / "sessions/2026/08/28/rollout-discovered.jsonl",
        [_codex_token_row("2026-08-28T01:02:03Z", 11, 4)],
    )
    monkeypatch.setattr(module, "_codex_roots", lambda _home: [discovered])
    result = sync_usage_telemetry(
        tmp_path / "workspace",
        grok_home=tmp_path / "missing-grok",
        now_utc="2026-08-28T02:00:00Z",
    )
    assert result["codex_inserted"] == 1
    assert result["sync_state"]["providers"]["codex"]["status"] == "success"


def test_provider_and_program_are_safe_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    record_conversion_event(
        workspace,
        provider=r"C:\\secret\\account",
        program=r"../../private/token",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=8,
        delivered_units=4,
        event_id="unsafe-agent",
    )
    summary = get_usage_summary(workspace, window_days=7, now_utc="2026-08-28T02:00:00Z")
    assert all("\\" not in agent["provider"] and "/" not in agent["program"] for agent in summary["agents"])
    with sqlite3.connect(workspace / ".memoryguard" / "usage_telemetry.sqlite") as connection:
        stored = " ".join(str(row) for row in connection.execute("SELECT provider, program FROM usage_events"))
    assert "secret" not in stored and "private" not in stored and "token" not in stored
