"""Local, privacy-preserving token/conversion telemetry.

This module deliberately keeps two measurement bases separate:

* provider events are measured host-reported token counts (Codex/Grok);
* conversion events are estimated MemoryGuard deterministic units.

The latter is the only basis on which a savings ratio is emitted.  No
conversation body, account name, raw source path, or instance id is stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 2
DEFAULT_WINDOW_DAYS = 7
SUPPORTED_WINDOWS = frozenset({7, 30})
DETERMINISTIC_BASIS = "mg_deterministic_unit"
_ALIAS_MAP = {
    "openai": "codex",
    "codex": "codex",
    "xai": "grok",
    "grok": "grok",
    "anthropic": "claude",
    "claude": "claude",
    "claude-code": "claude",
    "cursor": "cursor",
    "trae": "trae",
}
_SENSITIVE_NAME_WORDS = (
    "secret", "token", "password", "credential", "private", "account",
)
_JSONL_HEAD_BYTES = 4096
_JSONL_HEAD_FINGERPRINT_BYTES = 64
_SQL_RETRIES = 3
_SQL_RETRY_DELAY = 0.02
_UNSUPPORTED_HOSTS = ("claude", "cursor", "trae")
_SYNC_REASON_CODES = frozenset({
    "source_not_detected",
    "host_does_not_report_tokens",
    "sync_failed",
    "not_synced",
})
_PUBLIC_SYNC_STATUSES = frozenset({
    "success",
    "source_not_found",
    "host_not_supported",
    "error",
    "no_measured_source",
    "unavailable",
    "not_synced",
})
_PUBLIC_MEASUREMENT_STATES = frozenset({
    "measured",
    "estimated",
    "mixed",
    "unavailable",
})
_COMPLETED_SYNC_STATUSES = frozenset({
    "success",
    "source_not_found",
    "host_not_supported",
})


def telemetry_db_path(workspace: str | Path) -> Path:
    """Return the workspace-local telemetry database path."""

    return Path(workspace).expanduser().resolve() / ".memoryguard" / "usage_telemetry.sqlite"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: str | datetime | None) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: str | datetime | None = None) -> str:
    return _parse_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if converted >= 0 else None


def _safe_units(value: Any) -> int | None:
    return _safe_int(value)


def _safe_sync_status(value: Any) -> str:
    status = str(value or "").strip().casefold()
    return status if status in _PUBLIC_SYNC_STATUSES else "unavailable"


def _safe_sync_reason(value: Any, *, default: str = "not_synced") -> str:
    reason = str(value or "").strip().casefold()
    return reason if reason in _SYNC_REASON_CODES else default


def _safe_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return _iso_utc(str(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _safe_agent_name(value: Any) -> str:
    """Return a bounded provider/program slug, never an account or path."""

    raw = _text(value)
    if (
        not raw
        or any(char in raw for char in ("/", "\\", "\x00", "\r", "\n"))
        or any(word in raw for word in _SENSITIVE_NAME_WORDS)
    ):
        return "unknown"
    alias = _ALIAS_MAP.get(raw)
    if alias:
        return alias
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-")[:64]
    return slug or "unknown"


def _agent_identity(provider: Any, program: Any) -> tuple[str, str, str]:
    provider_name = _safe_agent_name(provider)
    program_name = _safe_agent_name(program) if _text(program) else provider_name
    # Stable grouping intentionally excludes account, instance, session, and
    # task identifiers.  The program/provider pair is the public identity.
    return provider_name, program_name, f"{provider_name}:{program_name}"


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "surrogatepass")).hexdigest()


def _scope_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    return _digest(f"memoryguard-scope-v1|{text}") if text else None


def _canonical(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _source_hash(path: str | Path) -> str:
    # Canonicalizing before hashing makes a Junction/symlink one source.
    return _digest(_canonical(path).as_posix())


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _execute_retry(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[Any, ...] = (),
) -> sqlite3.Cursor:
    for attempt in range(_SQL_RETRIES):
        try:
            return connection.execute(statement, parameters)
        except sqlite3.OperationalError as exc:
            locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
            if not locked or attempt == _SQL_RETRIES - 1:
                raise
            time.sleep(_SQL_RETRY_DELAY * (attempt + 1))
    raise RuntimeError("sqlite_retry_exhausted")  # pragma: no cover


def _connect(workspace: str | Path) -> sqlite3.Connection:
    path = telemetry_db_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=2.0)
    connection.row_factory = sqlite3.Row
    _execute_retry(connection, "PRAGMA foreign_keys=ON")
    _execute_retry(connection, "PRAGMA busy_timeout=2000")
    try:
        _execute_retry(connection, "PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # Read-only or legacy files can reject WAL.  The busy timeout remains
        # active, and the caller receives the same data contract.
        pass
    _ensure_schema(connection)
    return connection


_READ_REQUIRED_COLUMNS = {
    "usage_events": frozenset({
        "event_key", "event_kind", "provider", "program", "agent_stable_key",
        "observed_at_utc", "measurement_basis", "input_tokens", "output_tokens",
        "total_tokens", "baseline_units", "delivered_units", "conversion_count",
        "share_group_hash", "project_ref_hash",
    }),
    "usage_sync_state": frozenset({
        "provider", "status", "last_success_at", "last_error_at", "last_error",
        "inserted_count", "rotated_count", "source_count",
    }),
}


class _TelemetryReadUnavailable(RuntimeError):
    pass


def _connect_readonly(workspace: str | Path) -> sqlite3.Connection:
    """Open only a complete existing telemetry schema without mutating it."""

    path = telemetry_db_path(workspace)
    if not path.is_file():
        raise _TelemetryReadUnavailable("telemetry_database_missing")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0)
        connection.row_factory = sqlite3.Row
        _execute_retry(connection, "PRAGMA busy_timeout=2000")
        for table, required_columns in _READ_REQUIRED_COLUMNS.items():
            columns = {
                row[1]
                for row in _execute_retry(connection, f"PRAGMA table_info({table})")
            }
            if not required_columns <= columns:
                raise _TelemetryReadUnavailable("telemetry_schema_unavailable")
    except _TelemetryReadUnavailable:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise _TelemetryReadUnavailable("telemetry_database_unavailable") from exc
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS usage_events (
            event_key TEXT PRIMARY KEY,
            event_kind TEXT NOT NULL CHECK (event_kind IN ('measured', 'conversion')),
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
            cache_write_input_tokens INTEGER,
            output_tokens INTEGER,
            reasoning_output_tokens INTEGER,
            total_tokens INTEGER,
            baseline_units INTEGER,
            delivered_units INTEGER,
            conversion_count INTEGER NOT NULL DEFAULT 0,
            share_group_hash TEXT,
            project_ref_hash TEXT,
            scope_kind TEXT NOT NULL DEFAULT 'host'
        );
        CREATE INDEX IF NOT EXISTS usage_events_observed_idx
            ON usage_events (observed_at_utc);
        CREATE INDEX IF NOT EXISTS usage_events_agent_idx
            ON usage_events (provider, program, observed_at_utc);
        CREATE TABLE IF NOT EXISTS usage_cursors (
            source_hash TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_generation INTEGER NOT NULL DEFAULT 0,
            byte_offset INTEGER NOT NULL DEFAULT 0,
            line_ordinal INTEGER NOT NULL DEFAULT 0,
            source_size INTEGER NOT NULL DEFAULT 0,
            source_head_hash TEXT,
            source_signature TEXT,
            updated_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS usage_sync_state (
            provider TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            last_error TEXT,
            inserted_count INTEGER NOT NULL DEFAULT 0,
            rotated_count INTEGER NOT NULL DEFAULT 0,
            source_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    event_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(usage_events)")
    }
    for name, declaration in (
        ("cache_write_input_tokens", "INTEGER"),
        ("share_group_hash", "TEXT"),
        ("project_ref_hash", "TEXT"),
        ("scope_kind", "TEXT NOT NULL DEFAULT 'host'"),
    ):
        if name not in event_columns:
            connection.execute(f"ALTER TABLE usage_events ADD COLUMN {name} {declaration}")
    cursor_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(usage_cursors)")
    }
    for name, declaration in (
        ("source_size", "INTEGER NOT NULL DEFAULT 0"),
        ("source_head_hash", "TEXT"),
        ("source_signature", "TEXT"),
    ):
        if name not in cursor_columns:
            connection.execute(f"ALTER TABLE usage_cursors ADD COLUMN {name} {declaration}")
    # v1 conversion rows had no scope metadata.  Mark them as legacy
    # conversions so a scoped query cannot accidentally treat them as host
    # measurements; unscoped compatibility reads still include them.
    connection.execute(
        "UPDATE usage_events SET scope_kind = 'conversion' "
        "WHERE event_kind = 'conversion' AND (scope_kind IS NULL OR scope_kind = 'host')"
    )


def _set_sync_state(
    connection: sqlite3.Connection,
    *,
    provider: str,
    status: str,
    now_utc: str,
    inserted: int = 0,
    rotated: int = 0,
    sources: int = 0,
    error: str | None = None,
) -> None:
    safe_error = None
    if error:
        safe_error = error if error in _SYNC_REASON_CODES else _safe_agent_name(error)
    if status in _COMPLETED_SYNC_STATUSES:
        _execute_retry(
            connection,
            """
            INSERT INTO usage_sync_state
              (provider, status, last_success_at, last_error_at, last_error,
               inserted_count, rotated_count, source_count)
            VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
              status=excluded.status,
              last_success_at=excluded.last_success_at,
              last_error_at=NULL,
              last_error=excluded.last_error,
              inserted_count=excluded.inserted_count,
              rotated_count=excluded.rotated_count,
              source_count=excluded.source_count
            """,
            (provider, status, now_utc, None if status == "success" else (safe_error or status),
             inserted, rotated, sources),
        )
    else:
        _execute_retry(
            connection,
            """
            INSERT INTO usage_sync_state
              (provider, status, last_success_at, last_error_at, last_error,
               inserted_count, rotated_count, source_count)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
              status=excluded.status,
              last_error_at=excluded.last_error_at,
              last_error=excluded.last_error
            """,
            (provider, status, now_utc, safe_error or "sync_failed", inserted, rotated, sources),
        )


def _sync_state(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = _execute_retry(
        connection,
        "SELECT * FROM usage_sync_state ORDER BY provider",
    ).fetchall()
    providers: dict[str, dict[str, Any]] = {}
    for row in rows:
        # Legacy databases are untrusted input.  Keep only the bounded public
        # provider identity and reason codes; never echo raw adapter values.
        provider = _safe_agent_name(row["provider"])
        status = _safe_sync_status(row["status"])
        providers[provider] = {
            "status": status,
            "last_success_at": _safe_timestamp(row["last_success_at"]),
            "last_error_at": _safe_timestamp(row["last_error_at"]),
            "last_error": (
                None if status == "success"
                else _safe_sync_reason(row["last_error"], default=(
                    "sync_failed" if row["last_error"] else "not_synced"
                ))
            ),
            "inserted_count": _safe_int(row["inserted_count"]) or 0,
            "rotated_count": _safe_int(row["rotated_count"]) or 0,
            "source_count": _safe_int(row["source_count"]) or 0,
        }
    successes = [item["last_success_at"] for item in providers.values() if item["last_success_at"]]
    statuses = {item["status"] for item in providers.values()}
    if "error" in statuses:
        overall = "error"
    elif "success" in statuses:
        overall = "success"
    elif not providers:
        overall = "unavailable"
    else:
        overall = "no_measured_source"
    return {
        "status": overall,
        "last_success_at": max(successes) if successes else None,
        "last_error_at": max(
            (item["last_error_at"] for item in providers.values() if item["last_error_at"]),
            default=None,
        ),
        "last_error": next(
            (item["last_error"] for item in reversed(list(providers.values())) if item["last_error"]),
            None,
        ),
        "providers": providers,
    }


def _cursor(connection: sqlite3.Connection, source_hash: str) -> sqlite3.Row | None:
    return _execute_retry(
        connection,
        "SELECT * FROM usage_cursors WHERE source_hash = ?", (source_hash,)
    ).fetchone()


def _upsert_cursor(
    connection: sqlite3.Connection,
    *,
    source_hash: str,
    provider: str,
    source_kind: str,
    generation: int,
    offset: int,
    ordinal: int,
    source_size: int,
    source_head_hash: str,
    source_signature: str,
    now_utc: str,
) -> None:
    _execute_retry(
        connection,
        """
        INSERT INTO usage_cursors
          (source_hash, provider, source_kind, source_generation, byte_offset,
           line_ordinal, source_size, source_head_hash, source_signature, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_hash) DO UPDATE SET
          provider=excluded.provider,
          source_kind=excluded.source_kind,
          source_generation=excluded.source_generation,
          byte_offset=excluded.byte_offset,
          line_ordinal=excluded.line_ordinal,
          source_size=excluded.source_size,
          source_head_hash=excluded.source_head_hash,
          source_signature=excluded.source_signature,
          updated_at_utc=excluded.updated_at_utc
        """,
        (
            source_hash, provider, source_kind, generation, offset, ordinal,
            source_size, source_head_hash, source_signature, now_utc,
        ),
    )


def _insert_event(connection: sqlite3.Connection, event: Mapping[str, Any]) -> bool:
    columns = (
        "event_key", "event_kind", "provider", "program", "agent_stable_key",
        "source_kind", "source_hash", "source_generation", "source_offset",
        "source_ordinal", "observed_at_utc", "measurement_basis", "input_tokens",
        "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
        "reasoning_output_tokens",
        "total_tokens", "baseline_units", "delivered_units", "conversion_count",
        "share_group_hash", "project_ref_hash", "scope_kind",
    )
    values = tuple(event.get(column) for column in columns)
    cursor = _execute_retry(
        connection,
        f"INSERT OR IGNORE INTO usage_events ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    return cursor.rowcount == 1


def _event_from_usage(
    *,
    provider: str,
    program: str,
    source_kind: str,
    source_hash: str,
    generation: int,
    offset: int,
    ordinal: int,
    observed_at_utc: str,
    usage: Mapping[str, Any],
) -> dict[str, Any] | None:
    input_tokens = _safe_int(usage.get("input_tokens"))

    def _usage_int(*keys: str) -> int | None:
        for key in keys:
            if key in usage:
                value = _safe_int(usage.get(key))
                if value is not None:
                    return value
        return None

    details = usage.get("input_tokens_details")
    details = details if isinstance(details, Mapping) else {}
    cached_input_tokens = _usage_int("cached_input_tokens", "cache_read_input_tokens")
    if cached_input_tokens is None:
        cached_input_tokens = next(
            (
                _safe_int(details.get(key))
                for key in ("cached_tokens", "cache_read_tokens")
                if key in details and _safe_int(details.get(key)) is not None
            ),
            None,
        )
    cache_write_input_tokens = _usage_int("cache_write_input_tokens", "cache_write_tokens")
    if cache_write_input_tokens is None:
        cache_write_input_tokens = _safe_int(details.get("cache_write_tokens"))
    output_tokens = _safe_int(usage.get("output_tokens"))
    reasoning = _safe_int(usage.get("reasoning_output_tokens"))
    total = _safe_int(usage.get("total_tokens"))
    if input_tokens is None and output_tokens is None and total is None:
        return None
    event_key = _digest(
        f"measured|{source_hash}|{generation}|{offset}|{ordinal}"
    )
    _, _, stable_key = _agent_identity(provider, program)
    return {
        "event_key": event_key,
        "event_kind": "measured",
        "provider": provider,
        "program": program,
        "agent_stable_key": stable_key,
        "source_kind": source_kind,
        "source_hash": source_hash,
        "source_generation": generation,
        "source_offset": offset,
        "source_ordinal": ordinal,
        "observed_at_utc": observed_at_utc,
        "measurement_basis": "provider_reported_token",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "baseline_units": None,
        "delivered_units": None,
        "conversion_count": 0,
        "share_group_hash": None,
        "project_ref_hash": None,
        "scope_kind": "host",
    }


def _codex_usage(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = record.get("payload")
    if not isinstance(payload, Mapping) or _text(payload.get("type")) != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return None
    # last_token_usage is per-turn.  total_token_usage is cumulative and must
    # not be summed across a rollout.
    usage = info.get("last_token_usage")
    return usage if isinstance(usage, Mapping) else None


def _grok_usage(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if _text(record.get("msg")) != "shell.turn.inference_done":
        return None
    context = record.get("ctx")
    if not isinstance(context, Mapping):
        return None
    prompt = _safe_int(context.get("prompt_tokens"))
    completion = _safe_int(context.get("completion_tokens"))
    if prompt is None and completion is None:
        return None
    return {
        "input_tokens": prompt,
        "cached_input_tokens": _safe_int(context.get("cached_prompt_tokens")),
        "output_tokens": completion,
        "reasoning_output_tokens": _safe_int(context.get("reasoning_tokens")),
    }


def _source_metadata(path: Path, size: int) -> tuple[str, str]:
    """Return opaque head/signature hashes used to detect in-place rotation."""

    with path.open("rb") as handle:
        head = handle.read(_JSONL_HEAD_BYTES)
        if size > _JSONL_HEAD_BYTES:
            handle.seek(max(0, size - _JSONL_HEAD_BYTES))
            tail = handle.read(_JSONL_HEAD_BYTES)
        else:
            tail = head
    head_hash = _digest(head[:_JSONL_HEAD_FINGERPRINT_BYTES].hex())
    signature = _digest(f"{size}|{head.hex()}|{tail.hex()}")
    return head_hash, signature


def _sync_jsonl(
    connection: sqlite3.Connection,
    path: Path,
    *,
    provider: str,
    program: str,
    source_kind: str,
    usage_reader: Any,
    now_utc: str,
) -> tuple[int, int]:
    """Consume one JSONL source, returning ``(inserted, rotated)``."""

    try:
        with path.open("rb") as size_handle:
            size_handle.seek(0, 2)
            size = size_handle.tell()
    except OSError:
        return 0, 0
    source_hash = _source_hash(path)
    try:
        head_hash, signature = _source_metadata(path, size)
    except OSError:
        return 0, 0
    old = _cursor(connection, source_hash)
    generation = int(old["source_generation"]) if old else 0
    offset = int(old["byte_offset"]) if old else 0
    ordinal = int(old["line_ordinal"]) if old else 0
    old_size = int(old["source_size"] or 0) if old else 0
    old_head_hash = str(old["source_head_hash"] or "") if old else ""
    old_signature = str(old["source_signature"] or "") if old else ""
    rotated = 0
    rotated_in_place = bool(old) and (
        offset > size
        or (old_head_hash and old_head_hash != head_hash)
        or (old_size == size and old_signature and old_signature != signature)
    )
    if rotated_in_place:
        generation += 1
        offset = 0
        ordinal = 0
        rotated = 1
    inserted = 0
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                raw = handle.readline()
                if not raw:
                    offset = handle.tell()
                    break
                complete_line = raw.endswith(b"\n")
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A writer may have flushed half a JSON object.  Leave
                    # cursor at line start so the completed line is consumed
                    # on the next scan.
                    if not complete_line:
                        offset = line_offset
                        break
                    offset = handle.tell()
                    ordinal += 1
                    continue
                offset = handle.tell()
                ordinal += 1
                if not isinstance(record, Mapping):
                    continue
                usage = usage_reader(record)
                if not usage:
                    continue
                observed = record.get("timestamp", record.get("ts"))
                if observed is None or not str(observed).strip():
                    continue
                try:
                    observed_at = _iso_utc(observed)
                except (TypeError, ValueError, OverflowError):
                    continue
                event = _event_from_usage(
                    provider=provider,
                    program=program,
                    source_kind=source_kind,
                    source_hash=source_hash,
                    generation=generation,
                    offset=line_offset,
                    ordinal=ordinal,
                    observed_at_utc=observed_at,
                    usage=usage,
                )
                if event and _insert_event(connection, event):
                    inserted += 1
    except OSError:
        return inserted, rotated
    _upsert_cursor(
        connection,
        source_hash=source_hash,
        provider=provider,
        source_kind=source_kind,
        generation=generation,
        offset=offset,
        ordinal=ordinal,
        source_size=size,
        source_head_hash=head_hash,
        source_signature=signature,
        now_utc=now_utc,
    )
    return inserted, rotated


def _codex_roots(codex_home: str | Path | None) -> list[Path]:
    """Resolve Codex session homes from the explicit path or known discovery."""

    if codex_home is not None:
        return [_canonical(codex_home)]
    try:
        from .agent_locator import current_codex_home, discover_codex_homes

        found = [path for path in discover_codex_homes() if path.is_dir()]
        if found:
            return found
        return [_canonical(current_codex_home())]
    except Exception:
        return [_canonical(Path.home() / ".codex")]


def _grok_root(grok_home: str | Path | None) -> Path:
    if grok_home is not None:
        return _canonical(grok_home)
    env = str(os.environ.get("GROK_HOME", "") or "").strip()
    if env:
        return _canonical(env)
    return _canonical(Path.home() / ".grok")


def _state_rollouts(codex_home: Path) -> list[Path]:
    """Discover rollouts from Codex state without persisting state paths."""

    state_path = codex_home / "state_5.sqlite"
    candidates: list[Path] = []
    if state_path.is_file():
        try:
            with sqlite3.connect(f"file:{state_path.as_posix()}?mode=ro", uri=True) as state:
                tables = {
                    row[0]
                    for row in state.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if "threads" in tables:
                    for (raw_path,) in state.execute(
                        "SELECT rollout_path FROM threads WHERE rollout_path IS NOT NULL"
                    ):
                        if not raw_path:
                            continue
                        candidate = Path(str(raw_path)).expanduser()
                        if not candidate.is_absolute():
                            candidate = codex_home / candidate
                        candidate = _canonical(candidate)
                        if _within(candidate, codex_home) and candidate.is_file():
                            candidates.append(candidate)
        except (OSError, sqlite3.Error):
            pass
    # state_5 is authoritative when it has usable rollout rows.  Filesystem
    # discovery is only a fallback for a host that has no projected rollout.
    if candidates:
        return sorted(set(candidates), key=lambda path: path.as_posix())
    candidates.extend(_canonical(path) for path in (codex_home / "sessions").rglob("rollout-*.jsonl"))
    return sorted({path for path in candidates if path.is_file()}, key=lambda path: path.as_posix())


def sync_codex(
    workspace: str | Path,
    *,
    codex_home: str | Path | None = None,
    now_utc: str | datetime | None = None,
) -> dict[str, Any]:
    """Incrementally ingest measured token events from Codex rollouts."""

    roots = _codex_roots(codex_home)
    now = _iso_utc(now_utc)
    inserted = rotated = sources = 0
    with _connect(workspace) as connection:
        try:
            for root in roots:
                for path in _state_rollouts(root):
                    sources += 1
                    added, was_rotated = _sync_jsonl(
                        connection,
                        path,
                        provider="codex",
                        program="codex",
                        source_kind="codex_rollout",
                        usage_reader=_codex_usage,
                        now_utc=now,
                    )
                    inserted += added
                    rotated += was_rotated
            status = "success" if sources else "source_not_found"
            _set_sync_state(
                connection, provider="codex", status=status, now_utc=now,
                inserted=inserted, rotated=rotated, sources=sources,
                error=None if sources else "source_not_detected",
            )
        except Exception as exc:
            _set_sync_state(
                connection, provider="codex", status="error", now_utc=now,
                inserted=inserted, rotated=rotated, sources=sources,
                error=type(exc).__name__,
            )
            connection.commit()
            return {
                "inserted": inserted, "rotated": rotated, "sources": sources,
                "status": "error", "error": type(exc).__name__,
            }
        connection.commit()
    return {
        "inserted": inserted, "rotated": rotated, "sources": sources,
        "status": "success" if sources else "source_not_found",
    }


def _grok_logs(grok_home: Path) -> list[Path]:
    logs_root = grok_home / "logs"
    if not logs_root.is_dir():
        return []
    return sorted(
        {_canonical(path) for path in logs_root.rglob("*.jsonl") if path.is_file()},
        key=lambda path: path.as_posix(),
    )


def sync_grok(
    workspace: str | Path,
    *,
    grok_home: str | Path | None = None,
    now_utc: str | datetime | None = None,
) -> dict[str, Any]:
    """Incrementally ingest measured inference events from Grok logs."""

    root = _grok_root(grok_home)
    now = _iso_utc(now_utc)
    inserted = rotated = sources = 0
    with _connect(workspace) as connection:
        try:
            for path in _grok_logs(root):
                sources += 1
                added, was_rotated = _sync_jsonl(
                    connection,
                    path,
                    provider="grok",
                    program="grok",
                    source_kind="grok_log",
                    usage_reader=_grok_usage,
                    now_utc=now,
                )
                inserted += added
                rotated += was_rotated
            status = "success" if sources else "source_not_found"
            _set_sync_state(
                connection, provider="grok", status=status, now_utc=now,
                inserted=inserted, rotated=rotated, sources=sources,
                error=None if sources else "source_not_detected",
            )
        except Exception as exc:
            _set_sync_state(
                connection, provider="grok", status="error", now_utc=now,
                inserted=inserted, rotated=rotated, sources=sources,
                error=type(exc).__name__,
            )
            connection.commit()
            return {
                "inserted": inserted, "rotated": rotated, "sources": sources,
                "status": "error", "error": type(exc).__name__,
            }
        connection.commit()
    return {
        "inserted": inserted, "rotated": rotated, "sources": sources,
        "status": "success" if sources else "source_not_found",
    }


def sync_usage_telemetry(
    workspace: str | Path,
    *,
    codex_home: str | Path | None = None,
    grok_home: str | Path | None = None,
    now_utc: str | datetime | None = None,
) -> dict[str, Any]:
    """Run all available provider adapters once using cursor/generation state."""

    now = _iso_utc(now_utc)
    codex = sync_codex(workspace, codex_home=codex_home, now_utc=now)
    grok = sync_grok(workspace, grok_home=grok_home, now_utc=now)
    with _connect(workspace) as connection:
        for provider in _UNSUPPORTED_HOSTS:
            _set_sync_state(
                connection,
                provider=provider,
                status="host_not_supported",
                now_utc=now,
                error="host_does_not_report_tokens",
            )
        connection.commit()
        state = _sync_state(connection)
    errors = [
        result.get("error")
        for result in (codex, grok)
        if result.get("status") == "error" or result.get("error")
    ]
    return {
        "inserted": codex["inserted"] + grok["inserted"],
        "rotated": codex["rotated"] + grok["rotated"],
        "sources": codex["sources"] + grok["sources"],
        "codex_inserted": codex["inserted"],
        "grok_inserted": grok["inserted"],
        "status": "error" if errors else state["status"],
        "sync_state": state,
    }


def record_conversion_event(
    workspace: str | Path,
    *,
    provider: str,
    program: str,
    share_group_id: str | None = None,
    project_ref: str | None = None,
    agent_stable_key: str | None = None,
    observed_at_utc: str | datetime | None = None,
    baseline_units: int | None = None,
    delivered_units: int | None = None,
    measurement_basis: str = DETERMINISTIC_BASIS,
    event_id: str | None = None,
    source_cursor: str | None = None,
    source_generation: int = 0,
    technical_source: str | None = None,
) -> dict[str, Any]:
    """Persist one idempotent Hook/native conversion event.

    ``event_id``, ``source_cursor`` and ``technical_source`` are hashed before
    persistence.  They are de-duplication inputs, never public telemetry data.
    """

    provider_name, program_name, stable_key = _agent_identity(provider, program)
    del agent_stable_key  # caller ids are not stable across accounts/sessions
    share_group_hash = _scope_hash(share_group_id)
    project_ref_hash = _scope_hash(project_ref)
    baseline = _safe_units(baseline_units)
    delivered = _safe_units(delivered_units)
    try:
        generation = max(0, int(source_generation))
    except (TypeError, ValueError):
        generation = 0
    observed = _iso_utc(observed_at_utc)
    source_hash = _digest(technical_source or f"{provider_name}:{program_name}:conversion")
    dedupe_seed = event_id or source_cursor or f"{observed}|{baseline}|{delivered}|{measurement_basis}"
    event_key = _digest(
        f"conversion|{provider_name}|{program_name}|{source_hash}|{generation}|"
        f"{share_group_hash or ''}|{project_ref_hash or ''}|{dedupe_seed}"
    )
    event = {
        "event_key": event_key,
        "event_kind": "conversion",
        "provider": provider_name,
        "program": program_name,
        "agent_stable_key": stable_key,
        "source_kind": "conversion_hook",
        "source_hash": source_hash,
        "source_generation": generation,
        "source_offset": None,
        "source_ordinal": None,
        "observed_at_utc": observed,
        "measurement_basis": _text(measurement_basis) or DETERMINISTIC_BASIS,
        "input_tokens": None,
        "cached_input_tokens": None,
        "cache_write_input_tokens": None,
        "output_tokens": None,
        "reasoning_output_tokens": None,
        "total_tokens": None,
        "baseline_units": baseline,
        "delivered_units": delivered,
        "conversion_count": 1,
        "share_group_hash": share_group_hash,
        "project_ref_hash": project_ref_hash,
        "scope_kind": "conversion",
    }
    with _connect(workspace) as connection:
        inserted = _insert_event(connection, event)
        connection.commit()
    return {
        "ok": True,
        "inserted": inserted,
        "event_key": event_key,
        "measurement_state": "estimated",
        "measurement_basis": event["measurement_basis"],
    }


write_conversion_event = record_conversion_event


def _sum_or_none(values: Iterable[Any]) -> int | None:
    cleaned = [value for value in (_safe_int(item) for item in values) if value is not None]
    return sum(cleaned) if cleaned else None


def _empty_metrics() -> dict[str, Any]:
    return {
        "estimated_baseline_units": None,
        "estimated_delivered_units": None,
        "estimated_saved_units": None,
        "estimated_ratio": None,
        "savings_ratio": None,
        "measured_input": None,
        "measured_cached_input": None,
        "measured_cache_write_input": None,
        "measured_output": None,
        "measured_derived_total": None,
        "measured_total": None,
        "measured_provider_total_event_count": 0,
        "measured_derived_total_event_count": 0,
        "measured_total_coverage": {
            "provider_reported": "none",
            "input_output_derived": "none",
            "measured_event_count": 0,
        },
        "measured_cache_coverage": {
            "cache_read": "unavailable",
            "cache_write": "unavailable",
            "measured_event_count": 0,
        },
        "conversion_count": 0,
        "measured_event_count": 0,
    }


def _metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
    result = _empty_metrics()
    measured = [row for row in rows if row["event_kind"] == "measured"]
    estimated = [row for row in rows if row["event_kind"] == "conversion" and row["measurement_basis"] == DETERMINISTIC_BASIS]
    result["measured_input"] = _sum_or_none(row["input_tokens"] for row in measured)
    columns = set(measured[0].keys()) if measured else set()
    if "cached_input_tokens" in columns:
        result["measured_cached_input"] = _sum_or_none(
            row["cached_input_tokens"] for row in measured
        )
    if "cache_write_input_tokens" in columns:
        result["measured_cache_write_input"] = _sum_or_none(
            row["cache_write_input_tokens"] for row in measured
        )
    result["measured_output"] = _sum_or_none(row["output_tokens"] for row in measured)
    result["measured_total"] = _sum_or_none(row["total_tokens"] for row in measured)
    result["measured_event_count"] = len(measured)
    provider_total_rows = [
        row for row in measured if _safe_int(row["total_tokens"]) is not None
    ]
    derived_total_rows = [
        row for row in measured
        if _safe_int(row["input_tokens"]) is not None
        and _safe_int(row["output_tokens"]) is not None
    ]
    result["measured_provider_total_event_count"] = len(provider_total_rows)
    result["measured_derived_total_event_count"] = len(derived_total_rows)
    result["measured_derived_total"] = (
        sum(
            _safe_int(row["input_tokens"]) + _safe_int(row["output_tokens"])
            for row in derived_total_rows
        )
        if derived_total_rows else None
    )
    result["measured_total_coverage"] = {
        "provider_reported": (
            "complete" if measured and len(provider_total_rows) == len(measured)
            else "partial" if provider_total_rows else "none"
        ),
        "input_output_derived": (
            "complete" if measured and len(derived_total_rows) == len(measured)
            else "partial" if derived_total_rows else "none"
        ),
        "measured_event_count": len(measured),
    }
    result["measured_cache_coverage"] = {
        "cache_read": (
            "complete" if measured and all(row["cached_input_tokens"] is not None for row in measured)
            else "partial" if any(row["cached_input_tokens"] is not None for row in measured)
            else "unavailable"
        ) if "cached_input_tokens" in columns else "unavailable",
        "cache_write": (
            "complete" if measured and all(row["cache_write_input_tokens"] is not None for row in measured)
            else "partial" if any(row["cache_write_input_tokens"] is not None for row in measured)
            else "unavailable"
        ) if "cache_write_input_tokens" in columns else "unavailable",
        "measured_event_count": len(measured),
    }
    result["conversion_count"] = sum(
        _safe_int(row["conversion_count"]) or 0
        for row in rows if row["event_kind"] == "conversion"
    )
    baseline = _sum_or_none(row["baseline_units"] for row in estimated)
    delivered = _sum_or_none(row["delivered_units"] for row in estimated)
    result["estimated_baseline_units"] = baseline
    result["estimated_delivered_units"] = delivered
    if baseline is not None and delivered is not None:
        result["estimated_saved_units"] = baseline - delivered
        if baseline > 0:
            ratio = (baseline - delivered) / baseline
            result["estimated_ratio"] = ratio
            result["savings_ratio"] = ratio
    return result


def _measurement_state(rows: list[sqlite3.Row]) -> str:
    measured = any(row["event_kind"] == "measured" for row in rows)
    estimated = any(
        row["event_kind"] == "conversion"
        and row["measurement_basis"] == DETERMINISTIC_BASIS
        for row in rows
    )
    if measured and estimated:
        return "mixed"
    if measured:
        return "measured"
    if estimated:
        return "estimated"
    return "unavailable"


def _host_measurement_fields(
    provider: str,
    sync_state: Mapping[str, Any],
) -> tuple[str, str]:
    providers = sync_state.get("providers") if isinstance(sync_state, Mapping) else None
    item = providers.get(provider) if isinstance(providers, Mapping) else None
    if not isinstance(item, Mapping) or not item.get("status"):
        return "not_synced", "not_synced"
    status = _safe_sync_status(item.get("status"))
    reason = _safe_sync_reason(item.get("last_error"), default="not_synced")
    if status == "success":
        return status, ""
    return status, reason or status


def _event_identity(row: sqlite3.Row) -> tuple[str, str, str]:
    """Normalize legacy event identities before exposing them through a read."""

    return _agent_identity(row["provider"], row["program"])


def _roster_entries(agent_roster: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if agent_roster is None:
        return {}
    if isinstance(agent_roster, Mapping):
        source: Iterable[Any] = [
            {"agent_key": key, "program": value}
            for key, value in agent_roster.items()
        ]
    elif isinstance(agent_roster, (str, bytes)):
        source = [agent_roster]
    else:
        try:
            source = iter(agent_roster)
        except TypeError:
            source = []
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in source:
        if isinstance(item, Mapping):
            provider = item.get("provider") or item.get("provider_name") or item.get("program")
            program = item.get("program") or item.get("program_name") or provider
            display_name = item.get("display_name") or item.get("agent_name") or item.get("name")
        else:
            provider = item
            program = item
            display_name = None
        provider_name, program_name, stable_key = _agent_identity(provider, program)
        key = (provider_name, program_name)
        entry = {
            "provider": provider_name,
            "program": program_name,
            "agent_stable_key": stable_key,
            "agent_key": stable_key,
        }
        if display_name:
            # Display labels are bounded and path-free; identity fields remain
            # the controlled slugs used for grouping and persistence.
            label = re.sub(r"[^\w .-]+", "", str(display_name)).strip()[:128]
            if label:
                entry["display_name"] = label
        result.setdefault(key, entry)
    return result


def _event_in_scope(
    row: sqlite3.Row,
    *,
    share_group_hash: str | None,
    project_ref_hash: str | None,
) -> bool:
    if row["event_kind"] == "measured":
        return True  # host measurement, never attributed to a project scope
    if share_group_hash is None and project_ref_hash is None:
        return True
    return (
        (share_group_hash is None or row["share_group_hash"] == share_group_hash)
        and (project_ref_hash is None or row["project_ref_hash"] == project_ref_hash)
    )


def _validate_window(window_days: int) -> int:
    try:
        value = int(window_days)
    except (TypeError, ValueError):
        value = DEFAULT_WINDOW_DAYS
    if value not in SUPPORTED_WINDOWS:
        raise ValueError("window_days must be 7 or 30")
    return value


def _date_range(anchor: date, days: int) -> list[date]:
    return [anchor - timedelta(days=days - 1 - index) for index in range(days)]


def get_usage_summary(
    workspace: str | Path,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now_utc: str | datetime | None = None,
    share_group_id: str = "",
    project_ref: str = "",
    agent_key: str | None = None,
    agent_roster: Any = None,
) -> dict[str, Any]:
    """Read the stable GUI contract without mixing measurement bases."""

    days = _validate_window(window_days)
    anchor = _parse_utc(now_utc)
    start = anchor.date() - timedelta(days=days - 1)
    end_at = _iso_utc(anchor)
    share_group_hash = _scope_hash(share_group_id)
    project_ref_hash = _scope_hash(project_ref)
    try:
        with _connect_readonly(workspace) as connection:
            raw_rows = _execute_retry(
                connection,
                """
                SELECT * FROM usage_events
                WHERE observed_at_utc >= ? AND observed_at_utc <= ?
                ORDER BY observed_at_utc, event_key
                """,
                (f"{start.isoformat()}T00:00:00.000Z", end_at),
            ).fetchall()
            sync_state = _sync_state(connection)
    except _TelemetryReadUnavailable:
        raw_rows = []
        sync_state = {"status": "unavailable", "providers": {}}
    rows = [
        row for row in raw_rows
        if _event_in_scope(
            row,
            share_group_hash=share_group_hash,
            project_ref_hash=project_ref_hash,
        )
    ]
    selected_key = _text(agent_key)
    if selected_key:
        rows = [
            row for row in rows
            if _event_identity(row)[2] == selected_key
        ]
    overall = _metrics(rows)
    roster = _roster_entries(agent_roster)
    roster_was_empty = not roster
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    daily_grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        provider, program, _ = _event_identity(row)
        key = (provider, program)
        grouped.setdefault(key, []).append(row)
        day_key = (row["observed_at_utc"][:10], *key)
        daily_grouped.setdefault(day_key, []).append(row)
    for key in grouped:
        roster.setdefault(
            key,
            {
                "provider": key[0],
                "program": key[1],
                "agent_stable_key": f"{key[0]}:{key[1]}",
                "agent_key": f"{key[0]}:{key[1]}",
            },
        )
    if roster_was_empty:
        for provider_name in (sync_state.get("providers") or {}):
            slug = _safe_agent_name(provider_name)
            roster.setdefault(
                (slug, slug),
                {
                    "provider": slug,
                    "program": slug,
                    "agent_stable_key": f"{slug}:{slug}",
                    "agent_key": f"{slug}:{slug}",
                },
            )

    if selected_key:
        roster = {
            key: item for key, item in roster.items()
            if item["agent_stable_key"] == selected_key or item["agent_key"] == selected_key
        }

    agents: list[dict[str, Any]] = []
    for key in sorted(roster):
        agent_rows = grouped.get(key, [])
        item = dict(roster[key])
        host_status, host_reason = _host_measurement_fields(key[0], sync_state)
        item.update(
            {
                "measurement_state": _measurement_state(agent_rows),
                "host_measurement_status": host_status,
                "host_measurement_reason": host_reason,
                **_metrics(agent_rows),
            }
        )
        agents.append(item)

    series: list[dict[str, Any]] = []
    for day in _date_range(anchor.date(), days):
        day_rows = [row for row in rows if row["observed_at_utc"][:10] == day.isoformat()]
        series.append({"date": day.isoformat(), **_metrics(day_rows)})

    # One row per date and agent.  Host measurements and conversion estimates
    # remain separate fields; neither contributes to the other basis.
    table_rows: list[dict[str, Any]] = []
    for (day, provider, program), agent_rows in sorted(daily_grouped.items()):
        host_status, host_reason = _host_measurement_fields(provider, sync_state)
        table_rows.append(
            {
                "date": day,
                "provider": provider,
                "program": program,
                "agent_stable_key": f"{provider}:{program}",
                "agent_key": f"{provider}:{program}",
                "measurement_state": _measurement_state(agent_rows),
                "host_measurement_status": host_status,
                "host_measurement_reason": host_reason,
                "window_days": days,
                **_metrics(agent_rows),
            }
        )

    state_values = {_measurement_state(grouped[key]) for key in grouped if grouped[key]}
    if not state_values:
        measurement_state = "unavailable"
    elif state_values == {"measured"}:
        measurement_state = "measured"
    elif state_values == {"estimated"}:
        measurement_state = "estimated"
    else:
        measurement_state = "separate"

    return {
        "schema_version": SCHEMA_VERSION,
        "window_days": days,
        "generated_at_utc": _iso_utc(anchor),
        "status": "available" if rows else "unavailable",
        "empty_reason": (
            None if rows else (
                "not_synced" if not (sync_state.get("providers") or {}) else (
                    "no_measured_source" if sync_state.get("status") in {
                        "no_measured_source", "source_not_found", "host_not_supported",
                    } else "no_events"
                )
            )
        ),
        "measurement_state": measurement_state,
        "scope": {
            "share_group_id": str(share_group_id or ""),
            "project_ref": str(project_ref or ""),
            "conversion_scope": "filtered" if share_group_hash or project_ref_hash else "all",
            "host_measurement_scope": "host-wide",
        },
        "sync_state": sync_state,
        "measurement_notice": (
            "Provider token totals are measured host reports. MemoryGuard baseline/delivered "
            "units are deterministic estimates; they are not billing-token equivalents. "
            "Cache-read and cache-write fields are reported only when the host source provides "
            "them; missing fields remain unavailable. "
            "Savings ratio is shown only for conversion events using mg_deterministic_unit. "
            "Unavailable providers are not treated as zero usage."
        ),
        "summary": {
            **overall,
            "available_agent_count": sum(1 for agent in agents if agent["measurement_state"] != "unavailable"),
            "unavailable_agent_count": sum(1 for agent in agents if agent["measurement_state"] == "unavailable"),
            "measurement_state": measurement_state,
        },
        "series": series,
        "agents": agents,
        "rows": table_rows,
    }


__all__ = [
    "DETERMINISTIC_BASIS",
    "get_usage_summary",
    "record_conversion_event",
    "sync_codex",
    "sync_grok",
    "sync_usage_telemetry",
    "telemetry_db_path",
    "write_conversion_event",
]
