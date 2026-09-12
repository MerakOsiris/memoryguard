"""Workspace-local working-memory runtime plane.

This module stores short-lived task execution state only.  It deliberately
does not import or write the MemoryAtom, Rules, Evidence, or Content stores.
Events are append-only; mutable task rows and heads are query snapshots used
for fast recovery, while the event/checkpoint history remains authoritative
for replay within the runtime plane.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..storage.database import execute_sql_script, open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import initialize_database
from ..storage.transaction import transaction


RUNTIME_V2_SCHEMA_VERSION = 1
RUNTIME_V2_SCHEMA_MARKER = "memoryguard-v2-phase4-runtime"
_UNKNOWN = "__UNKNOWN__"
_MAX_JSON_BYTES = 64 * 1024
_MAX_DEPTH = 8
_MAX_PAGE_SIZE = 1000

_RUN_STATES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
_NODE_STATES = frozenset({"pending", "running", "succeeded", "failed", "skipped"})
_RUN_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_NODE_TRANSITIONS = {
    "pending": frozenset({"running", "skipped"}),
    "running": frozenset({"succeeded", "failed", "skipped"}),
    "failed": frozenset({"running"}),
    "succeeded": frozenset(),
    "skipped": frozenset(),
}

_FORBIDDEN_KEYS = frozenset(
    {
        "body", "raw", "raw_output", "raw_content", "content", "text",
        "transcript", "full_transcript", "conversation", "history", "history_ref", "payload",
        "output", "response", "stdout", "stderr", "bytes",
        "tool_output", "tool_outputs", "control", "controls",
        "authority", "admin", "permission", "permissions", "capability",
        "capabilities", "role", "roles", "effect", "effects", "acl",
        "policy", "scope", "workspace", "workspace_id", "agent_instance_id",
        "project_ref", "share_group_id", "runtime_scope",
    }
)
_CONTROL_TOKENS = frozenset(
    {
        "raw", "body", "content", "text", "transcript", "conversation", "history",
        "payload", "output", "response", "stdout", "stderr", "bytes", "tool_output",
        "authority", "admin", "permission", "capability", "role", "effect",
        "acl", "policy", "scope", "workspace", "runtime", "control",
    }
)
_FORBIDDEN_TEXT_TOKENS = frozenset(
    {
        "raw", "body", "content", "text", "transcript", "conversation", "history",
        "payload", "output", "response", "stdout", "stderr", "bytes", "tool_output",
        "authority", "admin", "permission", "capability", "role", "effect", "acl",
        "policy", "scope", "control",
    }
)
_ALLOWED_SCALAR_KEYS = frozenset(
    {
        "provider", "provider_id", "output_hash", "response_digest",
    }
)
_CONTROL_VALUE_KEYS = frozenset({"task_type", "stage", "operation", "event_type", "code"})
_CONTROL_VALUE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.:-")


class RuntimeV2Error(RuntimeError):
    """Working-memory input, scope, or mutation failed closed."""


class RuntimeSchemaError(RuntimeV2Error):
    """Runtime V2 marker/schema is unsupported or unsafe."""


class RuntimeScopeError(RuntimeV2Error):
    """A caller attempted to read or mutate another runtime scope."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _norm_key(value: Any) -> str:
    if type(value) is not str:
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _forbidden_key(value: Any) -> bool:
    if type(value) is not str:
        return True
    key = _norm_key(value)
    if key in _ALLOWED_SCALAR_KEYS:
        return False
    return key in _FORBIDDEN_KEYS or any(
        key.startswith(f"{token}_") or key.endswith(f"_{token}")
        for token in _CONTROL_TOKENS
    )


def _forbidden_text(value: str) -> bool:
    """Reject sensitive/control tokens in scalar content, not just field names."""

    normalized = value.strip().lower().replace("-", "_").replace(".", "_").replace("/", "_")
    parts = tuple(part for part in normalized.split("_") if part)
    for token in _FORBIDDEN_TEXT_TOKENS:
        token_parts = tuple(part for part in token.split("_") if part)
        if len(token_parts) == 1 and token_parts[0] in parts:
            return True
        width = len(token_parts)
        if width > 1 and any(parts[index:index + width] == token_parts for index in range(len(parts) - width + 1)):
            return True
    return False


def _scalar_text(
    value: Any,
    *,
    label: str,
    default: str = "",
    max_bytes: int = 4096,
    reject_content: bool = True,
) -> str:
    """Accept only explicit scalar policy; never stringify containers/objects."""

    if value is None:
        return default
    if type(value) not in (str, int, bool):
        raise RuntimeV2Error(f"{label} must be a bounded string/int/bool scalar")
    if isinstance(value, str) and value == "" and default == "":
        return default
    text = value if isinstance(value, str) else str(value)
    if not text or "\r" in text or "\n" in text:
        raise RuntimeV2Error(f"{label} must be a non-empty single-line scalar")
    if len(text.encode("utf-8")) > max_bytes:
        raise RuntimeV2Error(f"{label} exceeds scalar length limit")
    if reject_content and _forbidden_text(text):
        raise RuntimeV2Error(f"{label} contains forbidden raw/control content")
    return text


def _scalar_int(value: Any, *, label: str, minimum: int = -(2**31), maximum: int = 2**31 - 1) -> int:
    """Accept only bounded integer/bool scalars; never invoke int(custom)."""

    if type(value) not in (int, bool):
        raise RuntimeV2Error(f"{label} must be a bounded integer scalar")
    number = int(value)
    if number < minimum or number > maximum:
        raise RuntimeV2Error(f"{label} is outside the bounded integer range")
    return number


def _safe_json(value: Mapping[str, Any] | None, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeV2Error(f"{label} must be a JSON object")

    def walk(item: Any, depth: int, path: str) -> Any:
        if depth > _MAX_DEPTH:
            raise RuntimeV2Error(f"{label} exceeds nesting limit at {path}")
        if isinstance(item, Mapping):
            if type(item) is not dict:
                raise RuntimeV2Error(f"{label} contains unsupported mapping at {path}")
            result: dict[str, Any] = {}
            for key, child in item.items():
                if type(key) is not str:
                    raise RuntimeV2Error(f"{label} field names must be strings")
                name = key
                if _forbidden_key(name):
                    raise RuntimeV2Error(f"{label} contains forbidden field: {name}")
                result[name] = walk(child, depth + 1, f"{path}.{name}" if path else name)
            return result
        if type(item) in (list, tuple):
            return [walk(child, depth + 1, f"{path}[{index}]") for index, child in enumerate(item)]
        if item is None:
            return item
        if type(item) is str:
            if len(item.encode("utf-8")) > 4096 or "\r" in item or "\n" in item:
                raise RuntimeV2Error(f"{label} contains an unbounded string at {path}")
            leaf = _norm_key(path.rsplit(".", 1)[-1].split("[", 1)[0])
            if leaf in _CONTROL_VALUE_KEYS:
                normalized_control = item.strip().lower()
                if (
                    not normalized_control
                    or len(normalized_control.encode("utf-8")) > 256
                    or any(char not in _CONTROL_VALUE_CHARS for char in normalized_control)
                ):
                    raise RuntimeV2Error(f"{label} contains invalid control metadata at {path}")
                return normalized_control
            if _forbidden_text(item):
                raise RuntimeV2Error(f"{label} contains forbidden raw/control content at {path}")
            return item
        if type(item) in (int, bool):
            return item
        raise RuntimeV2Error(f"{label} contains unsupported value at {path}")

    result = walk(value, 0, "")
    assert isinstance(result, dict)
    if len(_json(result).encode("utf-8")) > _MAX_JSON_BYTES:
        raise RuntimeV2Error(f"{label} exceeds 64 KiB")
    return result


def _assert_no_reparse(path: str | Path) -> None:
    current = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise RuntimeV2Error(f"cannot inspect runtime path: {current}") from exc
        if exists:
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise RuntimeV2Error(f"cannot inspect runtime path: {current}") from exc
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise RuntimeV2Error(f"runtime path contains symlink/reparse component: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


@dataclass(frozen=True)
class RuntimeScope:
    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    share_group_id: str = ""
    provider: str = ""
    runtime_scope: str = "default"

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_id", "agent_instance_id", "project_ref", "share_group_id",
            "provider", "runtime_scope",
        ):
            value = getattr(self, field_name)
            if value is None:
                raise ValueError(f"{field_name} must be explicit")
            if type(value) not in (str, int, bool):
                raise ValueError(f"{field_name} must be a scalar")
            normalized = _scalar_text(value, label=field_name, reject_content=False)
            object.__setattr__(self, field_name, normalized)
        if not self.workspace_id:
            raise ValueError("runtime workspace is required")

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.workspace_id,
            self.agent_instance_id,
            self.project_ref,
            self.share_group_id,
            self.provider,
            self.runtime_scope,
        )


@dataclass(frozen=True)
class MutationContext:
    scope: RuntimeScope
    idempotency_key: str
    actor: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scope, RuntimeScope):
            raise ValueError("MutationContext requires RuntimeScope")
        key = _scalar_text(self.idempotency_key, label="idempotency_key", max_bytes=256)
        if not key:
            raise ValueError("idempotency_key is required and bounded")
        actor = _scalar_text(self.actor, label="actor", default="", max_bytes=256)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "actor", actor)


@dataclass(frozen=True)
class TaskRun:
    run_id: str
    task_type: str
    goal: str
    status: str
    importance: int
    scope: RuntimeScope
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    error: Mapping[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.status


@dataclass(frozen=True)
class TaskNode:
    node_id: str
    run_id: str
    node_type: str
    status: str
    goal: str
    depends: tuple[str, ...]
    blocker: Mapping[str, Any]
    result_ref: Mapping[str, Any]
    importance: int
    created_at: str
    refs: tuple[Mapping[str, Any], ...] = ()

    @property
    def state(self) -> str:
        return self.status


@dataclass(frozen=True)
class TaskEvent:
    event_id: str
    run_id: str
    node_id: str | None
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    idempotency_key: str
    created_at: str


@dataclass(frozen=True)
class WorkingCheckpoint:
    checkpoint_id: str
    run_id: str
    node_id: str | None
    checkpoint_key: str
    digest: str
    state: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class ToolRef:
    tool_ref_id: str
    run_id: str
    node_id: str | None
    provider: str
    tool_name: str
    request_digest: str
    response_digest: str
    metadata: Mapping[str, Any]
    created_at: str


RUNTIME_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_v2_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT DEFAULT NULL,
    event_seq INTEGER NOT NULL CHECK (event_seq >= 0),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id,node_id,event_seq),
    UNIQUE(idempotency_key),
    FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES task_nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_events_run ON task_events(run_id,event_seq);
CREATE TABLE IF NOT EXISTS task_heads (
    head_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    last_event_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
    updated_at TEXT NOT NULL,
    UNIQUE(run_id,node_id),
    FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_refs (
    ref_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL CHECK (ref_kind IN ('source','evidence')),
    ref_value TEXT NOT NULL,
    ref_hash TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports',
    created_at TEXT NOT NULL,
    UNIQUE(node_id,ref_kind,ref_value,ref_hash,relation),
    FOREIGN KEY(node_id) REFERENCES task_nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_refs_node ON task_refs(node_id);
CREATE TABLE IF NOT EXISTS working_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    node_id TEXT DEFAULT NULL,
    checkpoint_key TEXT NOT NULL,
    checkpoint_digest TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id,node_id,checkpoint_key,checkpoint_digest),
    FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES task_nodes(node_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_working_checkpoints_run ON working_checkpoints(run_id,created_at);
CREATE TABLE IF NOT EXISTS task_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    run_id TEXT NOT NULL,
    node_id TEXT DEFAULT NULL,
    payload_digest TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES task_nodes(node_id) ON DELETE CASCADE
);
"""


def _stable_id(prefix: str, *parts: object) -> str:
    prefix_text = _scalar_text(prefix, label="stable id prefix", reject_content=False)
    normalized_parts: list[str] = []
    for index, part in enumerate(parts):
        if part is None:
            normalized_parts.append("")
        else:
            normalized_parts.append(
                _scalar_text(part, label=f"stable id part {index}", reject_content=False)
            )
    value = "\x1f".join((prefix_text, *normalized_parts))
    return f"{prefix_text}-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class RuntimeStore:
    """SQLite runtime store with fail-closed marker and scope checks."""

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        readonly: bool = False,
        read_only: bool | None = None,
        initialize: bool = True,
    ) -> None:
        if read_only is not None:
            readonly = bool(read_only)
        if isinstance(workspace, WorkspaceV2Layout):
            _assert_no_reparse(workspace.workspace)
            self.layout = workspace
        else:
            _assert_no_reparse(workspace)
            self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.db_path = self.layout.runtime_db
        self.readonly = bool(readonly)
        self.available = self.db_path.is_file()
        if self.readonly:
            if self.available:
                state = self._preflight(self.db_path)
                if state != "current":
                    raise RuntimeSchemaError("read-only runtime requires an initialized Phase 4 marker")
            return
        if initialize:
            self.layout.ensure_dirs()
            state = self._preflight(self.db_path) if self.db_path.is_file() else "fresh"
            if state == "fresh":
                initialize_database(self.db_path, "runtime", layout=self.layout)
            self._ensure_schema()
            self.available = True

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _preflight(self, path: Path) -> str:
        try:
            with open_database(path, readonly=True) as conn:
                tables = self._tables(conn)
                if "runtime_v2_schema_meta" in tables:
                    rows = conn.execute("SELECT key,value FROM runtime_v2_schema_meta ORDER BY key").fetchall()
                    if len(rows) != 1 or str(rows[0][0]) != "version":
                        raise RuntimeSchemaError("unknown runtime V2 marker")
                    marker = str(rows[0][1])
                    if marker != str(RUNTIME_V2_SCHEMA_VERSION):
                        direction = "future" if marker.isdigit() and int(marker) > RUNTIME_V2_SCHEMA_VERSION else "unsupported"
                        raise RuntimeSchemaError(f"{direction} runtime V2 schema version: {marker!r}")
                    required = {"runtime_v2_schema_meta", "task_events", "task_heads", "task_refs", "working_checkpoints", "task_idempotency"}
                    missing = sorted(required - tables)
                    if missing:
                        raise RuntimeSchemaError("runtime V2 marker current but tables missing: " + ",".join(missing))
                    required_columns = {
                        "task_runs": {"goal", "importance", "workspace_id", "agent_instance_id", "project_ref", "share_group_id", "provider", "runtime_scope"},
                        "task_nodes": {"goal", "depends_json", "blocker_json", "result_ref_json", "importance"},
                    }
                    for table, columns in required_columns.items():
                        missing_columns = sorted(columns - self._columns(conn, table))
                        if missing_columns:
                            raise RuntimeSchemaError(f"runtime V2 marker current but columns missing in {table}: " + ",".join(missing_columns))
                # Base marker validation is strictly read-only.  A future or
                # unknown Phase-1 runtime marker must fail before any ALTER.
            initialize_database(path, "runtime", layout=self.layout, readonly=True)
            return "current" if "runtime_v2_schema_meta" in tables else "needs_extension"
        except RuntimeV2Error:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise RuntimeSchemaError(f"cannot preflight runtime DB: {path}") from exc

    def _ensure_schema(self) -> None:
        with open_database(self.db_path) as conn:
            with transaction(conn):
                for table, column, definition in (
                    ("task_runs", "goal", "TEXT NOT NULL DEFAULT ''"),
                    ("task_runs", "importance", "INTEGER NOT NULL DEFAULT 0"),
                    ("task_runs", "workspace_id", "TEXT NOT NULL DEFAULT ''"),
                    ("task_runs", "agent_instance_id", "TEXT NOT NULL DEFAULT ''"),
                    ("task_runs", "project_ref", "TEXT NOT NULL DEFAULT ''"),
                    ("task_runs", "share_group_id", "TEXT NOT NULL DEFAULT ''"),
                    ("task_runs", "provider", "TEXT NOT NULL DEFAULT ''"),
                    ("task_runs", "runtime_scope", "TEXT NOT NULL DEFAULT 'default'"),
                    ("task_nodes", "goal", "TEXT NOT NULL DEFAULT ''"),
                    ("task_nodes", "depends_json", "TEXT NOT NULL DEFAULT '[]'"),
                    ("task_nodes", "blocker_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("task_nodes", "result_ref_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("task_nodes", "importance", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in self._columns(conn, table):
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                execute_sql_script(conn, RUNTIME_V2_SCHEMA)
                conn.execute(
                    "INSERT INTO runtime_v2_schema_meta(key,value) VALUES('version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(RUNTIME_V2_SCHEMA_VERSION),),
                )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        with open_database(self.db_path, readonly=True) as conn:
            yield conn

    def _scope_ok(self, scope: RuntimeScope | None) -> bool:
        return isinstance(scope, RuntimeScope) and scope.workspace_id == str(self.workspace) and _UNKNOWN not in scope.as_tuple()

    def _require_mutation(self, mutation: MutationContext) -> None:
        if self.readonly:
            raise RuntimeV2Error("runtime store is read-only")
        if not self.db_path.is_file():
            raise RuntimeV2Error("runtime store is not initialized")
        if not isinstance(mutation, MutationContext) or not self._scope_ok(mutation.scope):
            raise RuntimeScopeError("explicit mutation scope does not match workspace")

    @staticmethod
    def _check_fail(fail_at: str | None, point: str) -> None:
        if fail_at == point:
            raise RuntimeV2Error(f"injected runtime failure at {point}")

    @staticmethod
    def _payload_digest(value: Any) -> str:
        return _digest(value)

    def _idempotency(
        self,
        conn: sqlite3.Connection,
        mutation: MutationContext,
        operation: str,
        run_id: str,
        node_id: str | None,
        payload: Any,
    ) -> tuple[sqlite3.Row | None, str]:
        digest = self._payload_digest(payload)
        row = conn.execute(
            "SELECT * FROM task_idempotency WHERE idempotency_key=?",
            (mutation.idempotency_key,),
        ).fetchone()
        if row is not None:
            if str(row["payload_digest"]) != digest or str(row["operation"]) != operation or str(row["run_id"]) != str(run_id) or (str(row["node_id"] or "") != str(node_id or "")):
                raise RuntimeV2Error("idempotency key reused with a different payload")
        return row, digest

    def _scope_sql(self, scope: RuntimeScope) -> tuple[str, ...]:
        return scope.as_tuple()

    def _run_row(self, conn: sqlite3.Connection, run_id: str, scope: RuntimeScope) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM task_runs WHERE run_id=? AND workspace_id=? AND agent_instance_id=? AND project_ref=? AND share_group_id=? AND provider=? AND runtime_scope=?",
            (str(run_id), *self._scope_sql(scope)),
        ).fetchone()

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> TaskRun:
        try:
            error = json.loads(str(row["error_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            error = {}
        scope = RuntimeScope(
            workspace_id=str(row["workspace_id"] or ""),
            agent_instance_id=str(row["agent_instance_id"] or ""),
            project_ref=str(row["project_ref"] or ""),
            share_group_id=str(row["share_group_id"] or ""),
            provider=str(row["provider"] or ""),
            runtime_scope=str(row["runtime_scope"] or "default"),
        )
        return TaskRun(
            run_id=str(row["run_id"]), task_type=str(row["task_type"]), goal=str(row["goal"] or ""),
            status=str(row["state"]), importance=int(row["importance"] or 0), scope=scope,
            created_at=str(row["created_at"]), started_at=str(row["started_at"] or ""),
            finished_at=str(row["finished_at"] or ""), error=error if isinstance(error, Mapping) else {},
        )

    def _node_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> TaskNode:
        try:
            depends = json.loads(str(row["depends_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            depends = []
        try:
            blocker = json.loads(str(row["blocker_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            blocker = {}
        try:
            result_ref = json.loads(str(row["result_ref_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result_ref = {}
        refs = conn.execute(
            "SELECT ref_kind,ref_value,ref_hash,relation FROM task_refs WHERE node_id=? ORDER BY ref_kind,ref_value,ref_hash,relation",
            (str(row["node_id"]),),
        ).fetchall()
        head = conn.execute(
            "SELECT state FROM task_heads WHERE run_id=? AND node_id=?",
            (str(row["run_id"]), str(row["node_id"])),
        ).fetchone()
        return TaskNode(
            node_id=str(row["node_id"]), run_id=str(row["run_id"]), node_type=str(row["node_type"]),
            status=str(head[0]) if head is not None else str(row["state"]), goal=str(row["goal"] or ""),
            depends=tuple(str(item) for item in depends if isinstance(item, str)),
            blocker=blocker if isinstance(blocker, Mapping) else {},
            result_ref=result_ref if isinstance(result_ref, Mapping) else {},
            importance=int(row["importance"] or 0), created_at=str(row["created_at"]),
            refs=tuple({"kind": str(ref[0]), "value": str(ref[1]), "hash": str(ref[2]), "relation": str(ref[3])} for ref in refs),
        )

    def _event(self, conn: sqlite3.Connection, event_id: str) -> TaskEvent:
        row = conn.execute("SELECT * FROM task_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise RuntimeV2Error("runtime event disappeared")
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return TaskEvent(
            event_id=str(row["event_id"]), run_id=str(row["run_id"]),
            node_id=str(row["node_id"]) if row["node_id"] is not None else None,
            sequence=int(row["event_seq"]), event_type=str(row["event_type"]),
            payload=payload if isinstance(payload, Mapping) else {},
            idempotency_key=str(row["idempotency_key"]), created_at=str(row["created_at"]),
        )

    def counts(self) -> dict[str, int]:
        if not self.db_path.is_file():
            return {"runs": 0, "nodes": 0, "events": 0, "refs": 0, "checkpoints": 0}
        with self.connection() as conn:
            return {
                "runs": int(conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]),
                "nodes": int(conn.execute("SELECT COUNT(*) FROM task_nodes").fetchone()[0]),
                "events": int(conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]),
                "refs": int(conn.execute("SELECT COUNT(*) FROM task_refs").fetchone()[0]),
                "checkpoints": int(conn.execute("SELECT COUNT(*) FROM working_checkpoints").fetchone()[0]),
            }

    def integrity_check(self) -> list[str]:
        if not self.db_path.is_file():
            return []
        with self.connection() as conn:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]

    def foreign_key_check(self) -> list[tuple[Any, ...]]:
        if not self.db_path.is_file():
            return []
        with self.connection() as conn:
            return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]

    def orphan_count(self) -> int:
        if not self.db_path.is_file():
            return 0
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM task_nodes n WHERE NOT EXISTS (SELECT 1 FROM task_refs r WHERE r.node_id=n.node_id)").fetchone()[0])

    def create_run(
        self,
        run_id: str,
        *,
        task_type: str,
        goal: str,
        importance: int,
        mutation: MutationContext,
        requested_by: str = "",
        request_fingerprint: str = "",
        fail_at: str | None = None,
    ) -> TaskRun:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        # ``task_type`` is server-selected control metadata (for example
        # ``history_backfill`` or ``content_import``), not user/source body.
        # Token-based body filtering here rejected legitimate canonical task
        # names and made durable GUI task routing impossible.
        task_type_text = _scalar_text(task_type, label="task_type", reject_content=False)
        safe_goal = _scalar_text(goal, label="goal", max_bytes=16 * 1024)
        importance_value = _scalar_int(importance, label="importance")
        requested_by_text = _scalar_text(requested_by, label="requested_by", default="")
        fingerprint = _scalar_text(
            request_fingerprint,
            label="request_fingerprint",
            default="",
            max_bytes=256,
            reject_content=False,
        )
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        if not run_id_text or not task_type_text:
            raise RuntimeV2Error("run_id and task_type are required")
        payload = {
            "run_id": run_id_text, "task_type": task_type_text, "goal": safe_goal,
            "importance": importance_value, "scope": mutation.scope.as_tuple(),
            "requested_by": requested_by_text,
        }
        if fingerprint:
            payload["request_fingerprint"] = fingerprint
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                idem, digest = self._idempotency(conn, mutation, "create_run", run_id_text, None, payload)
                if idem is not None:
                    row = self._run_row(conn, run_id_text, mutation.scope)
                    if row is None:
                        raise RuntimeV2Error("idempotency record has no run")
                    return self._run_from_row(row)
                existing = conn.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id_text,)).fetchone()
                if existing is not None:
                    if str(existing["task_type"]) != task_type_text or str(existing["goal"] or "") != safe_goal:
                        raise RuntimeV2Error("run_id reused with a different payload")
                    raise RuntimeScopeError("run exists outside requested runtime scope")
                conn.execute(
                    "INSERT INTO task_runs(run_id,task_type,state,requested_by,started_at,finished_at,error_json,created_at,goal,importance,workspace_id,agent_instance_id,project_ref,share_group_id,provider,runtime_scope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id_text, task_type_text, "queued", requested_by_text, "", "", "{}", now, safe_goal, importance_value, *mutation.scope.as_tuple()),
                )
                event_id = _stable_id("runtime-event", run_id_text, "", 0, mutation.idempotency_key)
                event_payload = {"task_type": task_type_text, "goal": safe_goal}
                if fingerprint:
                    event_payload["request_fingerprint"] = fingerprint
                conn.execute(
                    "INSERT INTO task_events(event_id,run_id,node_id,event_seq,event_type,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, run_id_text, None, 0, "run_created", _json(self._event_payload(event_payload)), mutation.idempotency_key, now),
                )
                conn.execute(
                    "INSERT INTO task_heads(head_id,run_id,node_id,state,generation,last_event_seq,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (_stable_id("runtime-head", run_id_text, ""), run_id_text, "", "queued", 0, 0, now),
                )
                conn.execute(
                    "INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mutation.idempotency_key, "create_run", run_id_text, None, digest, _json({"event_id": event_id}), now),
                )
                self._check_fail(fail_at_text, "after_event")
                row = self._run_row(conn, run_id_text, mutation.scope)
                assert row is not None
                return self._run_from_row(row)

    def request_fingerprint(self, run_id: str, scope: RuntimeScope) -> str:
        """Return only persisted request hash for one scoped background run."""
        if not self._scope_ok(scope) or not self.db_path.is_file():
            return ""
        run_id_text = _scalar_text(run_id, label="run_id")
        with self.connection() as conn:
            if self._run_row(conn, run_id_text, scope) is None:
                return ""
            row = conn.execute(
                "SELECT payload_json FROM task_events WHERE run_id=? AND node_id IS NULL "
                "AND event_type='run_created' ORDER BY event_seq ASC, event_id ASC LIMIT 1",
                (run_id_text,),
            ).fetchone()
        if row is None:
            return ""
        try:
            payload = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        return _scalar_text(
            payload.get("request_fingerprint") if isinstance(payload, Mapping) else "",
            label="request_fingerprint",
            default="",
            max_bytes=256,
            reject_content=False,
        )

    def add_node(
        self,
        run_id: str,
        node_id: str,
        *,
        node_type: str,
        goal: str = "",
        depends: Sequence[str] = (),
        dependencies: Sequence[str] | None = None,
        refs: Sequence[Mapping[str, Any]] = (),
        result_ref: Mapping[str, Any] | None = None,
        importance: int = 0,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> TaskNode:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        node_id_text = _scalar_text(node_id, label="node_id")
        node_type_text = _scalar_text(node_type, label="node_type")
        goal_text = _scalar_text(goal, label="goal", max_bytes=16 * 1024)
        importance_value = _scalar_int(importance, label="importance")
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        if not run_id_text or not node_id_text or not node_type_text:
            raise RuntimeV2Error("run_id, node_id, and node_type are required")
        if dependencies is not None:
            depends = dependencies
        normalized_depends = self._normalize_dependencies(depends, node_id=node_id_text)
        normalized_refs = self._normalize_refs(refs)
        if not normalized_refs:
            raise RuntimeV2Error("every task node requires at least one source/evidence ref")
        safe_result = _safe_json(result_ref if result_ref is not None else {}, label="result_ref")
        payload = {
            "run_id": run_id_text, "node_id": node_id_text, "node_type": node_type_text,
            "goal": goal_text, "depends": normalized_depends, "refs": normalized_refs,
            "result_ref": safe_result, "importance": importance_value,
        }
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                run = self._run_row(conn, run_id_text, mutation.scope)
                if run is None:
                    raise RuntimeV2Error("run missing or outside runtime scope")
                idem, digest = self._idempotency(conn, mutation, "add_node", run_id_text, node_id_text, payload)
                if idem is not None:
                    row = conn.execute("SELECT * FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone()
                    if row is None:
                        raise RuntimeV2Error("idempotency record has no node")
                    return self._node_from_row(conn, row)
                existing = conn.execute("SELECT * FROM task_nodes WHERE node_id=?", (node_id_text,)).fetchone()
                if existing is not None:
                    raise RuntimeV2Error("node_id already exists")
                for dep in normalized_depends:
                    dep_row = conn.execute("SELECT run_id FROM task_nodes WHERE node_id=?", (dep,)).fetchone()
                    if dep_row is None or str(dep_row[0]) != run_id_text:
                        raise RuntimeV2Error("dependency is missing or outside this run")
                if self._would_cycle(conn, run_id_text, node_id_text, normalized_depends):
                    raise RuntimeV2Error("task dependency cycle")
                conn.execute(
                    "INSERT INTO task_nodes(node_id,run_id,parent_node_id,node_type,state,input_json,output_json,error_json,created_at,goal,depends_json,blocker_json,result_ref_json,importance) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (node_id_text, run_id_text, None, node_type_text, "pending", _json({"goal": goal_text}), "{}", "{}", now, goal_text, _json(list(normalized_depends)), "{}", _json(safe_result), importance_value),
                )
                event_id = _stable_id("runtime-event", run_id_text, node_id_text, 0, mutation.idempotency_key)
                conn.execute(
                    "INSERT INTO task_events(event_id,run_id,node_id,event_seq,event_type,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, run_id_text, node_id_text, 0, "node_created", _json(self._event_payload({"node_type": node_type_text, "goal": goal_text})), mutation.idempotency_key, now),
                )
                conn.execute(
                    "INSERT INTO task_heads(head_id,run_id,node_id,state,generation,last_event_seq,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (_stable_id("runtime-head", run_id_text, node_id_text), run_id_text, node_id_text, "pending", 0, 0, now),
                )
                for ref in normalized_refs:
                    conn.execute(
                        "INSERT INTO task_refs(ref_id,node_id,ref_kind,ref_value,ref_hash,relation,created_at) VALUES(?,?,?,?,?,?,?)",
                        (_stable_id("runtime-ref", node_id_text, ref["kind"], ref["value"], ref["hash"], ref["relation"]), node_id_text, ref["kind"], ref["value"], ref["hash"], ref["relation"], now),
                    )
                conn.execute(
                    "INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mutation.idempotency_key, "add_node", run_id_text, node_id_text, digest, _json({"event_id": event_id}), now),
                )
                self._check_fail(fail_at_text, "after_refs")
                row = conn.execute("SELECT * FROM task_nodes WHERE node_id=?", (node_id_text,)).fetchone()
                assert row is not None
                return self._node_from_row(conn, row)

    def _normalize_refs(self, refs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
            raise RuntimeV2Error("task refs must be a finite sequence")
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for index, raw in enumerate(refs):
            if not isinstance(raw, Mapping):
                raise RuntimeV2Error("task ref must be an object")
            if any(type(key) is not str for key in raw):
                raise RuntimeV2Error("task ref field names must be strings")
            if any(_forbidden_key(key) for key in raw):
                raise RuntimeV2Error("task ref contains forbidden raw/control field")
            kind_raw = next((raw[name] for name in ("kind", "ref_kind") if name in raw and raw[name] is not None), None)
            value_raw = next((raw[name] for name in ("value", "ref_value", "id", "path") if name in raw and raw[name] is not None), None)
            digest_raw = next((raw[name] for name in ("hash", "ref_hash", "digest") if name in raw and raw[name] is not None), None)
            kind = _scalar_text(kind_raw, label="ref.kind")
            value = _scalar_text(
                value_raw,
                label="ref.value",
            )
            digest = _scalar_text(
                digest_raw,
                label="ref.hash",
            )
            relation = _scalar_text(raw.get("relation"), label="ref.relation", default="supports")
            if kind not in {"source", "evidence"} or not value or not digest:
                raise RuntimeV2Error("task refs require source/evidence kind, stable value, and hash")
            key = (kind, value, digest, relation)
            if key in seen:
                raise RuntimeV2Error("duplicate task ref")
            seen.add(key)
            result.append({"kind": kind, "value": value, "hash": digest, "relation": relation})
        return result

    @staticmethod
    def _normalize_dependencies(depends: Sequence[str], *, node_id: str) -> tuple[str, ...]:
        if isinstance(depends, (str, bytes)) or not isinstance(depends, Sequence):
            raise RuntimeV2Error("node dependencies must be a finite sequence")
        normalized: list[str] = []
        for index, item in enumerate(depends):
            normalized.append(_scalar_text(item, label=f"depends[{index}]"))
        result = tuple(normalized)
        if node_id in result or len(set(result)) != len(result):
            raise RuntimeV2Error("node dependencies contain a self/cycle candidate")
        return result

    @staticmethod
    def _event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        return _safe_json(payload, label="event payload")

    @staticmethod
    def _would_cycle(conn: sqlite3.Connection, run_id: str, node_id: str, depends: Sequence[str]) -> bool:
        graph: dict[str, tuple[str, ...]] = {}
        for row in conn.execute("SELECT node_id,depends_json FROM task_nodes WHERE run_id=?", (str(run_id),)).fetchall():
            try:
                raw = json.loads(str(row[1] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = []
            graph[str(row[0])] = tuple(str(item) for item in raw if isinstance(item, str))
        graph[str(node_id)] = tuple(str(item) for item in depends)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item: str) -> bool:
            if item in visiting:
                return True
            if item in visited:
                return False
            visiting.add(item)
            for parent in graph.get(item, ()):
                if visit(parent):
                    return True
            visiting.remove(item)
            visited.add(item)
            return False

        # Existing nodes are required to have been acyclic when appended; a
        # new edge can only introduce a cycle reachable from the new node.
        return visit(str(node_id))

    def transition(
        self,
        run_id: str,
        state: str | None = None,
        *,
        status: str | None = None,
        mutation: MutationContext,
        node_id: str | None = None,
        result_ref: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        fail_at: str | None = None,
    ) -> TaskEvent:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        node_id_text = _scalar_text(node_id, label="node_id", default="") if node_id is not None else ""
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        if state is not None and status is not None:
            state_text = _scalar_text(state, label="state")
            status_text = _scalar_text(status, label="status")
            if state_text != status_text:
                raise RuntimeV2Error("state and status conflict")
            state = state_text
        elif state is not None:
            state = _scalar_text(state, label="state")
        elif status is not None:
            state = _scalar_text(status, label="status")
        if state is None:
            raise RuntimeV2Error("transition state/status is required")
        normalized = state
        if not run_id_text:
            raise RuntimeV2Error("run_id is required")
        allowed_states = _NODE_STATES if node_id is not None else _RUN_STATES
        if normalized not in allowed_states:
            raise RuntimeV2Error(f"unknown runtime state: {state!r}")
        safe_result = _safe_json(result_ref if result_ref is not None else {}, label="result_ref")
        safe_error = _safe_json(error if error is not None else {}, label="error")
        payload = self._event_payload({"state": normalized, "result_ref": safe_result, "error": safe_error})
        with open_database(self.db_path) as conn:
            with transaction(conn):
                run = self._run_row(conn, run_id_text, mutation.scope)
                if run is None:
                    raise RuntimeV2Error("run missing or outside runtime scope")
                target_id = node_id_text
                if node_id is None:
                    head = conn.execute("SELECT state,last_event_seq,generation FROM task_heads WHERE run_id=? AND node_id=''", (run_id_text,)).fetchone()
                else:
                    head = conn.execute("SELECT state,last_event_seq,generation FROM task_heads WHERE run_id=? AND node_id=?", (run_id_text, node_id_text)).fetchone()
                    if conn.execute("SELECT 1 FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone() is None:
                        raise RuntimeV2Error("node missing or outside run")
                if head is None:
                    raise RuntimeV2Error("runtime head missing")
                idem, digest = self._idempotency(conn, mutation, "transition", run_id_text, node_id_text if node_id is not None else None, payload)
                if idem is not None:
                    return self._event(conn, str(json.loads(str(idem["result_json"]))["event_id"]))
                current = str(head[0])
                transition_map = _NODE_TRANSITIONS if node_id is not None else _RUN_TRANSITIONS
                if normalized not in transition_map.get(current, frozenset()):
                    raise RuntimeV2Error(f"invalid {current}->{normalized} transition")
                if node_id is not None and normalized == "running":
                    dep_row = conn.execute("SELECT depends_json FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone()
                    try:
                        dependencies = json.loads(str(dep_row[0] or "[]")) if dep_row is not None else []
                    except (TypeError, ValueError, json.JSONDecodeError):
                        dependencies = []
                    for dependency in dependencies if isinstance(dependencies, list) else []:
                        dep_head = conn.execute("SELECT state FROM task_heads WHERE run_id=? AND node_id=?", (run_id_text, dependency)).fetchone()
                        if dep_head is None or str(dep_head[0]) not in {"succeeded", "skipped"}:
                            raise RuntimeV2Error("node dependencies are not complete")
                if node_id is None and normalized == "succeeded":
                    pending_nodes = conn.execute("SELECT COUNT(*) FROM task_heads WHERE run_id=? AND node_id<>'' AND state NOT IN ('succeeded','skipped')", (run_id_text,)).fetchone()[0]
                    if int(pending_nodes) > 0:
                        raise RuntimeV2Error("run cannot succeed while nodes are incomplete")
                sequence = int(head[1]) + 1
                generation = int(head[2]) + 1
                now = _now()
                event_id = _stable_id("runtime-event", run_id_text, target_id, sequence, mutation.idempotency_key)
                event_type = "node_transition" if node_id is not None else "run_transition"
                conn.execute(
                    "INSERT INTO task_events(event_id,run_id,node_id,event_seq,event_type,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (event_id, run_id_text, node_id_text if node_id is not None else None, sequence, event_type, _json(payload), mutation.idempotency_key, now),
                )
                self._check_fail(fail_at_text, "after_event")
                conn.execute(
                    "UPDATE task_heads SET state=?,generation=?,last_event_seq=?,updated_at=? WHERE run_id=? AND node_id=?",
                    (normalized, generation, sequence, now, run_id_text, target_id),
                )
                if node_id is None:
                    started = now if normalized == "running" and not str(run["started_at"] or "") else str(run["started_at"] or "")
                    finished = now if normalized in {"succeeded", "failed", "cancelled"} else str(run["finished_at"] or "")
                    conn.execute("UPDATE task_runs SET state=?,started_at=?,finished_at=?,error_json=? WHERE run_id=?", (normalized, started, finished, _json(safe_error), run_id_text))
                else:
                    conn.execute("UPDATE task_nodes SET state=?,output_json=?,error_json=?,result_ref_json=? WHERE node_id=? AND run_id=?", (normalized, _json(safe_result), _json(safe_error), _json(safe_result), node_id_text, run_id_text))
                conn.execute(
                    "INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (mutation.idempotency_key, "transition", run_id_text, node_id_text if node_id is not None else None, digest, _json({"event_id": event_id}), now),
                )
                return self._event(conn, event_id)

    def add_blocker(
        self,
        run_id: str,
        node_id: str,
        blocker: Mapping[str, Any],
        *,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> TaskEvent:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        node_id_text = _scalar_text(node_id, label="node_id")
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        safe = _safe_json(blocker, label="blocker")
        payload = self._event_payload({"blocker": safe})
        with open_database(self.db_path) as conn:
            with transaction(conn):
                run = self._run_row(conn, run_id_text, mutation.scope)
                if run is None or conn.execute("SELECT 1 FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone() is None:
                    raise RuntimeV2Error("node missing or outside runtime scope")
                idem, digest = self._idempotency(conn, mutation, "add_blocker", run_id_text, node_id_text, payload)
                if idem is not None:
                    return self._event(conn, str(json.loads(str(idem["result_json"]))["event_id"]))
                head = conn.execute("SELECT last_event_seq,generation FROM task_heads WHERE run_id=? AND node_id=?", (run_id_text, node_id_text)).fetchone()
                if head is None:
                    raise RuntimeV2Error("runtime head missing")
                now = _now(); sequence = int(head[0]) + 1; event_id = _stable_id("runtime-event", run_id_text, node_id_text, sequence, mutation.idempotency_key)
                conn.execute("INSERT INTO task_events(event_id,run_id,node_id,event_seq,event_type,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, run_id_text, node_id_text, sequence, "blocker_added", _json(payload), mutation.idempotency_key, now))
                self._check_fail(fail_at_text, "after_event")
                conn.execute("UPDATE task_nodes SET blocker_json=? WHERE node_id=? AND run_id=?", (_json(safe), node_id_text, run_id_text))
                conn.execute("UPDATE task_heads SET generation=generation+1,last_event_seq=?,updated_at=? WHERE run_id=? AND node_id=?", (sequence, now, run_id_text, node_id_text))
                conn.execute("INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (mutation.idempotency_key, "add_blocker", run_id_text, node_id_text, digest, _json({"event_id": event_id}), now))
                return self._event(conn, event_id)

    def add_ref(
        self,
        run_id: str,
        node_id: str,
        ref: Mapping[str, Any],
        *,
        mutation: MutationContext,
        fail_at: str | None = None,
    ) -> TaskEvent:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        node_id_text = _scalar_text(node_id, label="node_id")
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        normalized = self._normalize_refs([ref])
        payload = self._event_payload({"ref": normalized[0]})
        with open_database(self.db_path) as conn:
            with transaction(conn):
                run = self._run_row(conn, run_id_text, mutation.scope)
                if run is None or conn.execute("SELECT 1 FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone() is None:
                    raise RuntimeV2Error("node missing or outside runtime scope")
                idem, digest = self._idempotency(conn, mutation, "add_ref", run_id_text, node_id_text, payload)
                if idem is not None:
                    return self._event(conn, str(json.loads(str(idem["result_json"]))["event_id"]))
                now = _now(); head = conn.execute("SELECT last_event_seq FROM task_heads WHERE run_id=? AND node_id=?", (run_id_text, node_id_text)).fetchone()
                if head is None:
                    raise RuntimeV2Error("runtime head missing")
                item = normalized[0]
                conn.execute("INSERT OR IGNORE INTO task_refs(ref_id,node_id,ref_kind,ref_value,ref_hash,relation,created_at) VALUES(?,?,?,?,?,?,?)", (_stable_id("runtime-ref", node_id_text, item["kind"], item["value"], item["hash"], item["relation"]), node_id_text, item["kind"], item["value"], item["hash"], item["relation"], now))
                sequence = int(head[0]) + 1; event_id = _stable_id("runtime-event", run_id_text, node_id_text, sequence, mutation.idempotency_key)
                conn.execute("INSERT INTO task_events(event_id,run_id,node_id,event_seq,event_type,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, run_id_text, node_id_text, sequence, "ref_added", _json(payload), mutation.idempotency_key, now))
                self._check_fail(fail_at_text, "after_event")
                conn.execute("UPDATE task_heads SET generation=generation+1,last_event_seq=?,updated_at=? WHERE run_id=? AND node_id=?", (sequence, now, run_id_text, node_id_text))
                conn.execute("INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (mutation.idempotency_key, "add_ref", run_id_text, node_id_text, digest, _json({"event_id": event_id}), now))
                return self._event(conn, event_id)

    def checkpoint(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        mutation: MutationContext,
        node_id: str | None = None,
        checkpoint_key: str = "default",
        fail_at: str | None = None,
    ) -> WorkingCheckpoint:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        node_id_text = _scalar_text(node_id, label="node_id", default="") if node_id is not None else ""
        checkpoint_key_text = _scalar_text(checkpoint_key, label="checkpoint_key")
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        safe = _safe_json(state, label="checkpoint")
        if not run_id_text or not checkpoint_key_text:
            raise RuntimeV2Error("checkpoint_key is required")
        payload = self._event_payload({"checkpoint_key": checkpoint_key_text, "state": safe})
        with open_database(self.db_path) as conn:
            with transaction(conn):
                run = self._run_row(conn, run_id_text, mutation.scope)
                if run is None or (node_id is not None and conn.execute("SELECT 1 FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone() is None):
                    raise RuntimeV2Error("run/node missing or outside runtime scope")
                idem, digest = self._idempotency(conn, mutation, "checkpoint", run_id_text, node_id_text if node_id is not None else None, payload)
                if idem is not None:
                    result = json.loads(str(idem["result_json"]))
                    row = conn.execute("SELECT * FROM working_checkpoints WHERE checkpoint_id=?", (str(result["checkpoint_id"]),)).fetchone()
                    if row is None:
                        raise RuntimeV2Error("idempotency record has no checkpoint")
                    return self._checkpoint_from_row(row)
                now = _now(); state_digest = _digest(safe); checkpoint_id = _stable_id("runtime-checkpoint", run_id_text, node_id_text, checkpoint_key_text, state_digest)
                conn.execute("INSERT OR IGNORE INTO working_checkpoints(checkpoint_id,run_id,node_id,checkpoint_key,checkpoint_digest,state_json,created_at) VALUES(?,?,?,?,?,?,?)", (checkpoint_id, run_id_text, node_id_text if node_id is not None else None, checkpoint_key_text, state_digest, _json(safe), now))
                self._check_fail(fail_at_text, "after_checkpoint")
                conn.execute("INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (mutation.idempotency_key, "checkpoint", run_id_text, node_id_text if node_id is not None else None, digest, _json({"checkpoint_id": checkpoint_id}), now))
                row = conn.execute("SELECT * FROM working_checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
                assert row is not None
                return self._checkpoint_from_row(row)

    def add_tool_ref(
        self,
        run_id: str,
        *,
        tool_name: str,
        provider: str,
        mutation: MutationContext,
        node_id: str | None = None,
        path_ref: str = "",
        output_hash: str = "",
        summary_ref: str = "",
        request_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
        raw_output: Any = None,
        fail_at: str | None = None,
    ) -> TaskEvent:
        self._require_mutation(mutation)
        run_id_text = _scalar_text(run_id, label="run_id")
        fail_at_text = _scalar_text(fail_at, label="fail_at", default="", reject_content=False)
        if raw_output is not None:
            raise RuntimeV2Error("raw tool output is not accepted; provide path/hash/summary refs")
        tool_name_text = _scalar_text(tool_name, label="tool_name")
        provider_text = _scalar_text(provider, label="provider")
        path_text = _scalar_text(path_ref, label="path_ref", default="") if path_ref is not None else ""
        output_hash_text = _scalar_text(output_hash, label="output_hash", default="") if output_hash is not None else ""
        summary_ref_text = _scalar_text(summary_ref, label="summary_ref", default="") if summary_ref is not None else ""
        request_digest_text = _scalar_text(request_digest, label="request_digest", default="") if request_digest is not None else ""
        node_id_text = _scalar_text(node_id, label="node_id", default="") if node_id is not None else ""
        if not tool_name_text or not provider_text:
            raise RuntimeV2Error("tool_name and provider are required")
        if mutation.scope.provider and provider_text != mutation.scope.provider:
            raise RuntimeScopeError("tool provider is outside runtime scope")
        if not path_text and not output_hash_text and not summary_ref_text:
            raise RuntimeV2Error("tool output requires a stable path, hash, or summary reference")
        safe_metadata = _safe_json(metadata if metadata is not None else {}, label="tool metadata")
        safe_path = self._stable_path(path_text) if path_text else ""
        payload = self._event_payload({
            "tool_name": tool_name_text, "provider": provider_text, "node_id": node_id_text,
            "path_ref": safe_path, "output_hash": output_hash_text, "summary_ref": summary_ref_text,
            "request_digest": request_digest_text, "metadata": safe_metadata,
        })
        with open_database(self.db_path) as conn:
            with transaction(conn):
                run = self._run_row(conn, run_id_text, mutation.scope)
                if run is None or (node_id is not None and conn.execute("SELECT 1 FROM task_nodes WHERE node_id=? AND run_id=?", (node_id_text, run_id_text)).fetchone() is None):
                    raise RuntimeV2Error("run/node missing or outside runtime scope")
                idem, digest = self._idempotency(conn, mutation, "add_tool_ref", run_id_text, node_id_text if node_id is not None else None, payload)
                if idem is not None:
                    return self._event(conn, str(json.loads(str(idem["result_json"]))["event_id"]))
                target_id = node_id_text
                head = conn.execute("SELECT last_event_seq FROM task_heads WHERE run_id=? AND node_id=?", (run_id_text, target_id)).fetchone()
                if head is None:
                    raise RuntimeV2Error("runtime head missing")
                now = _now(); sequence = int(head[0]) + 1; tool_ref_id = _stable_id("runtime-tool", run_id_text, node_id_text, mutation.idempotency_key)
                conn.execute(
                    "INSERT INTO tool_refs(tool_ref_id,run_id,node_id,provider,tool_name,request_digest,response_digest,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (tool_ref_id, run_id_text, node_id_text if node_id is not None else None, provider_text, tool_name_text, request_digest_text, output_hash_text, _json(self._event_payload({"path_ref": safe_path, "summary_ref": summary_ref_text, "metadata": safe_metadata})), now),
                )
                event_id = _stable_id("runtime-event", run_id_text, target_id, sequence, mutation.idempotency_key)
                conn.execute("INSERT INTO task_events(event_id,run_id,node_id,event_seq,event_type,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, run_id_text, node_id_text if node_id is not None else None, sequence, "tool_ref_added", _json(self._event_payload({"tool_ref_id": tool_ref_id, "path_ref": safe_path, "output_hash": output_hash_text, "summary_ref": summary_ref_text})), mutation.idempotency_key, now))
                self._check_fail(fail_at_text, "after_event")
                conn.execute("UPDATE task_heads SET generation=generation+1,last_event_seq=?,updated_at=? WHERE run_id=? AND node_id=?", (sequence, now, run_id_text, target_id))
                conn.execute("INSERT INTO task_idempotency(idempotency_key,operation,run_id,node_id,payload_digest,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (mutation.idempotency_key, "add_tool_ref", run_id_text, node_id_text if node_id is not None else None, digest, _json({"event_id": event_id, "tool_ref_id": tool_ref_id}), now))
                return self._event(conn, event_id)

    def _stable_path(self, value: str) -> str:
        raw = Path(value).expanduser()
        candidate = Path(os.path.abspath(os.fspath(raw if raw.is_absolute() else self.workspace / raw)))
        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise RuntimeV2Error("tool path escapes workspace") from exc
        _assert_no_reparse(candidate)
        return relative.as_posix()

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> WorkingCheckpoint:
        try:
            state = json.loads(str(row["state_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}
        return WorkingCheckpoint(str(row["checkpoint_id"]), str(row["run_id"]), str(row["node_id"]) if row["node_id"] is not None else None, str(row["checkpoint_key"]), str(row["checkpoint_digest"]), state if isinstance(state, Mapping) else {}, str(row["created_at"]))

    @staticmethod
    def _tool_ref_from_row(row: sqlite3.Row) -> ToolRef:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        return ToolRef(
            tool_ref_id=str(row["tool_ref_id"]), run_id=str(row["run_id"]),
            node_id=str(row["node_id"]) if row["node_id"] is not None else None,
            provider=str(row["provider"]), tool_name=str(row["tool_name"]),
            request_digest=str(row["request_digest"] or ""), response_digest=str(row["response_digest"] or ""),
            metadata=metadata if isinstance(metadata, Mapping) else {}, created_at=str(row["created_at"]),
        )

    def list_tool_refs(self, run_id: str, scope: RuntimeScope, *, node_id: str | None = None) -> tuple[ToolRef, ...]:
        if not self._scope_ok(scope) or not self.db_path.is_file():
            return ()
        run_id_text = _scalar_text(run_id, label="run_id")
        node_id_text = _scalar_text(node_id, label="node_id", default="") if node_id is not None else ""
        with self.connection() as conn:
            if self._run_row(conn, run_id_text, scope) is None:
                return ()
            if node_id is None:
                rows = conn.execute("SELECT * FROM tool_refs WHERE run_id=? ORDER BY created_at,tool_ref_id", (run_id_text,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tool_refs WHERE run_id=? AND node_id=? ORDER BY created_at,tool_ref_id", (run_id_text, node_id_text)).fetchall()
            return tuple(self._tool_ref_from_row(row) for row in rows)

    def load(self, run_id: str, scope: RuntimeScope) -> tuple[TaskRun, tuple[TaskNode, ...], tuple[WorkingCheckpoint, ...]] | None:
        if not self._scope_ok(scope):
            return None
        if not self.db_path.is_file():
            return None
        run_id_text = _scalar_text(run_id, label="run_id")
        with self.connection() as conn:
            row = self._run_row(conn, run_id_text, scope)
            if row is None:
                return None
            nodes = tuple(self._node_from_row(conn, item) for item in conn.execute("SELECT * FROM task_nodes WHERE run_id=? ORDER BY node_id", (run_id_text,)).fetchall())
            checkpoints = tuple(self._checkpoint_from_row(item) for item in conn.execute("SELECT * FROM working_checkpoints WHERE run_id=? ORDER BY created_at,checkpoint_id", (run_id_text,)).fetchall())
            return self._run_from_row(row), nodes, checkpoints

    def get_run(self, run_id: str, scope: RuntimeScope) -> TaskRun | None:
        """Return one scoped task run without exposing another runtime scope."""
        loaded = self.load(run_id, scope)
        return loaded[0] if loaded is not None else None

    def run_result_ref(self, run_id: str, scope: RuntimeScope) -> dict[str, Any]:
        """Return the latest run-level transition result reference.

        Run results live in immutable task events rather than duplicating a
        mutable result column on ``task_runs``.  This read reconstructs the
        terminal/current result without creating any runtime state.
        """
        if not self._scope_ok(scope) or not self.db_path.is_file():
            return {}
        run_id_text = _scalar_text(run_id, label="run_id")
        with self.connection() as conn:
            if self._run_row(conn, run_id_text, scope) is None:
                return {}
            rows = conn.execute(
                "SELECT payload_json FROM task_events WHERE run_id=? AND node_id IS NULL "
                "AND event_type='run_transition' ORDER BY event_seq DESC LIMIT 1",
                (run_id_text,),
            ).fetchone()
        if rows is None:
            return {}
        try:
            payload = json.loads(str(rows[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        result = payload.get("result_ref") if isinstance(payload, Mapping) else None
        return dict(result) if isinstance(result, Mapping) else {}

    def latest_checkpoint(
        self,
        run_id: str,
        scope: RuntimeScope,
        *,
        checkpoint_key: str = "progress",
    ) -> WorkingCheckpoint | None:
        """Return the latest scoped checkpoint for status/progress recovery."""
        if not self._scope_ok(scope) or not self.db_path.is_file():
            return None
        run_id_text = _scalar_text(run_id, label="run_id")
        key = _scalar_text(checkpoint_key, label="checkpoint_key")
        with self.connection() as conn:
            if self._run_row(conn, run_id_text, scope) is None:
                return None
            row = conn.execute(
                "SELECT * FROM working_checkpoints WHERE run_id=? AND node_id IS NULL "
                "AND checkpoint_key=? ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1",
                (run_id_text, key),
            ).fetchone()
            return self._checkpoint_from_row(row) if row is not None else None

    def list_runs(
        self,
        scope: RuntimeScope,
        *,
        states: Sequence[str] = (),
        task_types: Sequence[str] = (),
        limit: int = 100,
    ) -> tuple[TaskRun, ...]:
        """List scoped runs in deterministic newest-first order."""
        if not self._scope_ok(scope) or not self.db_path.is_file():
            return ()
        bounded = max(1, min(_scalar_int(limit, label="limit", minimum=1, maximum=2**31 - 1), _MAX_PAGE_SIZE))
        normalized_states = tuple(_scalar_text(item, label="state") for item in states)
        normalized_types = tuple(_scalar_text(item, label="task_type") for item in task_types)
        if any(item not in _RUN_STATES for item in normalized_states):
            raise RuntimeV2Error("unknown runtime state filter")
        clauses = [
            "workspace_id=?", "agent_instance_id=?", "project_ref=?",
            "share_group_id=?", "provider=?", "runtime_scope=?",
        ]
        params: list[Any] = list(scope.as_tuple())
        if normalized_states:
            clauses.append("state IN (" + ",".join("?" for _ in normalized_states) + ")")
            params.extend(normalized_states)
        if normalized_types:
            clauses.append("task_type IN (" + ",".join("?" for _ in normalized_types) + ")")
            params.extend(normalized_types)
        params.append(bounded)
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC, run_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return tuple(self._run_from_row(row) for row in rows)

    def list_nodes(self, run_id: str, scope: RuntimeScope, *, limit: int = 100, cursor: str | None = None) -> tuple[tuple[TaskNode, ...], str | None]:
        if not self._scope_ok(scope):
            return (), None
        if not self.db_path.is_file():
            return (), None
        run_id_text = _scalar_text(run_id, label="run_id")
        cursor_text = _scalar_text(cursor, label="cursor", default="") if cursor is not None else ""
        bounded = max(1, min(_scalar_int(limit, label="limit", minimum=1, maximum=2**31 - 1), _MAX_PAGE_SIZE))
        with self.connection() as conn:
            if self._run_row(conn, run_id_text, scope) is None:
                return (), None
            params: list[Any] = [run_id_text]
            predicate = ""
            if cursor_text:
                predicate = " AND node_id>?"
                params.append(cursor_text)
            params.append(bounded + 1)
            rows = conn.execute(f"SELECT * FROM task_nodes WHERE run_id=?{predicate} ORDER BY node_id LIMIT ?", tuple(params)).fetchall()
            has_more = len(rows) > bounded
            selected = rows[:bounded]
            next_cursor = str(selected[-1]["node_id"]) if has_more and selected else None
            return tuple(self._node_from_row(conn, row) for row in selected), next_cursor

    create_task_run = create_run
    create_node = add_node
    append_ref = add_ref
    save_checkpoint = checkpoint


WorkingMemoryStore = RuntimeStore
RuntimeMutationContext = MutationContext
WorkingMemoryScope = RuntimeScope


__all__ = [
    "MutationContext", "RuntimeMutationContext", "RuntimeScope", "WorkingMemoryScope",
    "RuntimeStore", "WorkingMemoryStore", "RuntimeV2Error",
    "RuntimeSchemaError", "RuntimeScopeError", "TaskEvent", "TaskNode",
    "TaskRun", "ToolRef", "WorkingCheckpoint", "RUNTIME_V2_SCHEMA_MARKER",
    "RUNTIME_V2_SCHEMA_VERSION",
]
