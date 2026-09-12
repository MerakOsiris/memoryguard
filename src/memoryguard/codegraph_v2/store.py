"""Persistent, metadata-only V2 CodeGraph store.

This store is intentionally boring SQLite.  It reuses the repository's V2
layout, connection and transaction helpers, but owns a separate graph schema
with immutable revisions and a small projection outbox.  Source text is never
accepted by the public API and no table has a source-body column.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..storage.database import execute_sql_script, open_database, open_database_snapshot
from ..storage.layout import WorkspaceV2Layout
from ..rule_scope import canonical_project_ref
from ..storage.schema import initialize_database
from ..storage.transaction import transaction
from .models import (
    AffectedQuery,
    CODEGRAPH_SCHEMA_MARKER,
    CODEGRAPH_SCHEMA_VERSION,
    CodeGraphError,
    CodeGraphPathError,
    CodeGraphSchemaError,
    CodeGraphScope,
    CodeGraphScopeError,
    Edge,
    OutboxEvent,
    Revision,
    SourceFile,
    Symbol,
    UNKNOWN,
    UnknownLedgerEntry,
    normalize_provenance,
    stable_digest,
    stable_id,
    validate_metadata,
)


_AUX_TABLES = frozenset(
    {
        "codegraph_schema_meta",
        "graph_scopes",
        "source_files",
        "revisions",
        "symbols",
        "edges",
        "affected_queries",
        "checkpoints",
        "outbox",
        "migration_map",
        "unknown_ledger",
        "source_tombstones",
    }
)

CODEGRAPH_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS codegraph_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_scopes (
    scope_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    trusted_context INTEGER NOT NULL DEFAULT 1 CHECK (trusted_context IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, agent_instance_id, project_ref, provider, share_group_id, runtime_role)
);
CREATE TABLE IF NOT EXISTS source_files (
    file_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    source_role TEXT NOT NULL DEFAULT 'production',
    provenance TEXT NOT NULL DEFAULT 'production',
    revision_id TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope_id, path),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    revision_number INTEGER NOT NULL CHECK(revision_number >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(file_id, content_hash, source_revision),
    FOREIGN KEY(file_id) REFERENCES source_files(file_id),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS symbols (
    symbol_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'symbol',
    signature TEXT NOT NULL DEFAULT '',
    symbol_hash TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0 CHECK(line_start >= 0),
    line_end INTEGER NOT NULL DEFAULT 0 CHECK(line_end >= 0),
    provenance TEXT NOT NULL DEFAULT 'production',
    source_map_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(file_id, revision_id, name, kind, symbol_hash, line_start, line_end),
    FOREIGN KEY(file_id) REFERENCES source_files(file_id),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id),
    FOREIGN KEY(revision_id) REFERENCES revisions(revision_id)
);
CREATE TABLE IF NOT EXISTS edges (
    edge_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'related',
    context TEXT NOT NULL DEFAULT '',
    provenance TEXT NOT NULL DEFAULT 'production',
    source_location TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    weight REAL NOT NULL DEFAULT 1.0,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(scope_id, revision_id, from_id, to_id, relation, context, provenance, source_location),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id),
    FOREIGN KEY(revision_id) REFERENCES revisions(revision_id)
);
CREATE INDEX IF NOT EXISTS idx_codegraph_edges_to ON edges(scope_id, to_id, active);
CREATE INDEX IF NOT EXISTS idx_codegraph_edges_from ON edges(scope_id, from_id, active);
CREATE TABLE IF NOT EXISTS affected_queries (
    query_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    start_id TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK(depth >= 0),
    result_limit INTEGER NOT NULL CHECK(result_limit >= 1),
    relation_filter TEXT NOT NULL DEFAULT '',
    provenance_filter TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '[]',
    result_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(scope_id, start_id, depth, result_limit, relation_filter, provenance_filter, result_digest),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    sequence INTEGER NOT NULL DEFAULT 0 CHECK(sequence >= 0),
    digest TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(scope_id, domain),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','projected','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    projected_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE INDEX IF NOT EXISTS idx_codegraph_outbox_pending ON outbox(status, sequence);
CREATE TABLE IF NOT EXISTS migration_map (
    map_id TEXT PRIMARY KEY,
    source_db TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_pk TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'mapped',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_db, source_table, source_pk, target_type)
);
CREATE TABLE IF NOT EXISTS unknown_ledger (
    ledger_id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'BLOCKED',
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(source_ref, code, detail)
);
CREATE TABLE IF NOT EXISTS source_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    revision_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(file_id, reason),
    FOREIGN KEY(file_id) REFERENCES source_files(file_id),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
-- Compatibility read aliases; callers must still use CodeGraphStore writes.
CREATE VIEW IF NOT EXISTS codegraph_outbox AS SELECT * FROM outbox;
CREATE VIEW IF NOT EXISTS codegraph_checkpoints AS SELECT * FROM checkpoints;
"""

CODEGRAPH_REFRESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_fingerprints (
    fingerprint_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(scope_id, path),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS affected_receipts (
    receipt_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    start_ids_json TEXT NOT NULL DEFAULT '[]',
    result_json TEXT NOT NULL DEFAULT '[]',
    digest TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 2,
    result_limit INTEGER NOT NULL DEFAULT 32,
    provenance_filter TEXT NOT NULL DEFAULT 'production',
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0,1)),
    created_at TEXT NOT NULL,
    consumed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
CREATE TABLE IF NOT EXISTS refresh_queue (
    queue_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    path TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    UNIQUE(scope_id, path),
    FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id)
);
"""

_MAX_AFFECTED_RECEIPT_DEPTH = 4
_MAX_AFFECTED_RECEIPT_RESULTS = 64
_MAX_AFFECTED_RECEIPT_STARTS = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_metadata_json(value: Any, *, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CodeGraphSchemaError(f"stored {label} is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise CodeGraphSchemaError(f"stored {label} must be a JSON object")
    try:
        validate_metadata(parsed)
    except (TypeError, ValueError) as exc:
        raise CodeGraphSchemaError(f"stored {label} violates metadata contract") from exc
    return dict(parsed)


def _assert_no_reparse(path: str | Path) -> None:
    raw = Path(path).expanduser()
    current = Path(os.path.abspath(os.fspath(raw)))
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise CodeGraphPathError(f"cannot inspect workspace path: {current}") from exc
        if exists:
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                info = None
            except OSError as exc:
                raise CodeGraphPathError(f"cannot inspect workspace path: {current}") from exc
            if info is not None and (stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400)):
                raise CodeGraphPathError(f"workspace path cannot contain symlink or reparse point: {current}")
        if current.parent == current:
            return
        current = current.parent


def normalize_relative_path(value: str | Path) -> str:
    """Normalize source reference across provider/repository path spellings.

    Graphify exports may identify a file from repository root (``repo/src``)
    while native callers identify it from the indexed workspace root
    (``src``). Collapse ordinary parent segments, but still reject traversal
    above root and absolute paths.
    """

    raw = str(value).strip().replace("\\", "/")
    if not raw or "\x00" in raw or any(ord(char) < 32 for char in raw):
        raise CodeGraphPathError("source path is empty or contains control characters")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw) or raw.startswith("//"):
        raise CodeGraphPathError("source path must be workspace-relative")
    path = PurePosixPath(raw)
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise CodeGraphPathError("source path escapes workspace")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise CodeGraphPathError("source path escapes workspace")
    # Some indexers include their repository directory in every exported
    # relative path. It is a transport prefix, not part of file identity.
    if len(parts) > 1 and parts[0].casefold() in {"repo", "repository"}:
        parts = parts[1:]
    normalized = "/".join(parts)
    return normalized


def _safe_path_under_workspace(workspace: Path, relative_path: str) -> None:
    candidate = workspace / Path(relative_path.replace("/", os.sep))
    current = candidate
    while current != workspace and current.parent != current:
        if current.exists() or current.is_symlink():
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise CodeGraphPathError(f"cannot inspect source path: {current}") from exc
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise CodeGraphPathError(f"source path cannot contain symlink or reparse point: {current}")
        current = current.parent
    if current != workspace:
        raise CodeGraphPathError("source path is outside workspace")


def _scope_row(scope: CodeGraphScope) -> tuple[str, ...]:
    return (scope.workspace_id, scope.agent_instance_id, scope.project_ref, scope.provider, scope.share_group_id, scope.runtime_role)


class CodeGraphStore:
    """SQLite-backed CodeGraph with exact ACL and immutable revisions."""

    SCHEMA_VERSION = CODEGRAPH_SCHEMA_VERSION
    SCHEMA_MARKER = CODEGRAPH_SCHEMA_MARKER

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        workspace_id: str | None = None,
        initialize: bool = True,
        source_workspace: str | Path | None = None,
    ) -> None:
        if isinstance(workspace, WorkspaceV2Layout):
            raw = source_workspace
            if raw is not None:
                _assert_no_reparse(raw)
            self.layout = workspace
        else:
            _assert_no_reparse(workspace)
            self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.workspace_id = str(workspace_id or self.workspace)
        self.db_path = self.layout.codegraph_db
        if initialize:
            self.layout.ensure_dirs()
            state = self._preflight()
            if state == "v1":
                self._migrate_v1_to_v2()
                self._ensure_refresh_schema()
            elif state == "current":
                self._ensure_refresh_schema()
            else:
                initialize_database(self.db_path, "codegraph", layout=self.layout)
                self._ensure_aux_schema()

    def _preflight(self) -> str:
        if not self.db_path.is_file():
            return "fresh"
        try:
            with open_database_snapshot(self.db_path) as conn:
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                base = conn.execute("SELECT marker,version FROM schema_meta WHERE domain='codegraph' ORDER BY rowid LIMIT 1").fetchone() if "schema_meta" in tables else None
                user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if base is None or str(base[0]) != "memoryguard-v2-phase1" or int(base[1]) != 1 or user_version != 1:
                    raise CodeGraphSchemaError("unsupported phase-1 codegraph schema marker")
                aux = tables & _AUX_TABLES
                if "codegraph_schema_meta" not in tables:
                    if aux:
                        raise CodeGraphSchemaError("codegraph aux schema marker is missing")
                    return "needs_aux"
                rows = conn.execute("SELECT key,value FROM codegraph_schema_meta ORDER BY key").fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version":
                    raise CodeGraphSchemaError("unsupported codegraph schema marker")
                version = str(rows[0][1])
                if version == "1":
                    return "v1"
                if version != str(CODEGRAPH_SCHEMA_VERSION):
                    raise CodeGraphSchemaError("unsupported codegraph schema marker")
                missing = sorted(_AUX_TABLES - {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} - {"codegraph_schema_meta"})
                # Views are intentionally not part of the required write schema.
                if missing:
                    raise CodeGraphSchemaError("codegraph schema is incomplete: " + ",".join(missing))
            return "current"
        except CodeGraphError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise CodeGraphSchemaError(f"cannot inspect codegraph database: {self.db_path}") from exc

    def _ensure_aux_schema(self) -> None:
        with open_database(self.db_path) as conn:
            with transaction(conn):
                execute_sql_script(conn, CODEGRAPH_AUX_SCHEMA)
                execute_sql_script(conn, CODEGRAPH_REFRESH_SCHEMA)
                conn.execute(
                    "INSERT INTO codegraph_schema_meta(key,value) VALUES('version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(CODEGRAPH_SCHEMA_VERSION),),
                )

    def _ensure_refresh_schema(self) -> None:
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_refresh_schema_conn(conn)

    @staticmethod
    def _ensure_refresh_schema_conn(conn: sqlite3.Connection) -> None:
        execute_sql_script(conn, CODEGRAPH_REFRESH_SCHEMA)

    def _migrate_v1_to_v2(self) -> None:
        """Known additive CodeGraph aux migration; no source-body columns exist."""
        with open_database(self.db_path) as conn:
            with transaction(conn):
                row = conn.execute("SELECT value FROM codegraph_schema_meta WHERE key='version'").fetchone()
                if row is None or str(row[0]) != "1":
                    raise CodeGraphSchemaError("codegraph v1 migration precondition failed")
                source_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(source_files)")}
                for column, ddl in (
                    ("source_role", "ALTER TABLE source_files ADD COLUMN source_role TEXT NOT NULL DEFAULT 'production'"),
                    ("provenance", "ALTER TABLE source_files ADD COLUMN provenance TEXT NOT NULL DEFAULT 'production'"),
                ):
                    if column not in source_columns:
                        conn.execute(ddl)
                symbol_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(symbols)")}
                for column, ddl in (
                    ("provenance", "ALTER TABLE symbols ADD COLUMN provenance TEXT NOT NULL DEFAULT 'production'"),
                    ("source_map_json", "ALTER TABLE symbols ADD COLUMN source_map_json TEXT NOT NULL DEFAULT '{}'"),
                    ("metadata_json", "ALTER TABLE symbols ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"),
                ):
                    if column not in symbol_columns:
                        conn.execute(ddl)
                affected_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(affected_queries)")}
                if "provenance_filter" not in affected_columns:
                    conn.execute("ALTER TABLE affected_queries ADD COLUMN provenance_filter TEXT NOT NULL DEFAULT ''")

                edge_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(edges)")}
                if not {"context", "provenance", "source_location", "metadata_json"} <= edge_columns:
                    conn.execute("ALTER TABLE edges RENAME TO edges_v1")
                    conn.execute(
                        "CREATE TABLE edges ("
                        "edge_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,revision_id TEXT NOT NULL DEFAULT '',"
                        "from_id TEXT NOT NULL,to_id TEXT NOT NULL,relation TEXT NOT NULL DEFAULT 'related',"
                        "context TEXT NOT NULL DEFAULT '',provenance TEXT NOT NULL DEFAULT 'production',"
                        "source_location TEXT NOT NULL DEFAULT '',metadata_json TEXT NOT NULL DEFAULT '{}',"
                        "weight REAL NOT NULL DEFAULT 1.0,active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),"
                        "created_at TEXT NOT NULL,"
                        "UNIQUE(scope_id,revision_id,from_id,to_id,relation,context,provenance,source_location),"
                        "FOREIGN KEY(scope_id) REFERENCES graph_scopes(scope_id),"
                        "FOREIGN KEY(revision_id) REFERENCES revisions(revision_id))"
                    )
                    conn.execute(
                        "INSERT INTO edges(edge_id,scope_id,revision_id,from_id,to_id,relation,context,provenance,source_location,metadata_json,weight,active,created_at) "
                        "SELECT edge_id,scope_id,revision_id,from_id,to_id,relation,'','production','','{}',weight,active,created_at FROM edges_v1"
                    )
                    conn.execute("DROP TABLE edges_v1")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_codegraph_edges_to ON edges(scope_id,to_id,active)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_codegraph_edges_from ON edges(scope_id,from_id,active)")
                conn.execute("UPDATE codegraph_schema_meta SET value=? WHERE key='version'", (str(CODEGRAPH_SCHEMA_VERSION),))
        if self._preflight() != "current":
            raise CodeGraphSchemaError("codegraph v1 migration did not reach v2")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a physically read-only connection for inspection."""

        with open_database_snapshot(self.db_path) as conn:
            yield conn

    def _scope(self, scope: CodeGraphScope | Mapping[str, Any] | None, *, write: bool = False) -> CodeGraphScope:
        if scope is None:
            raise CodeGraphScopeError("explicit trusted CodeGraphScope is required")
        # A mapping is useful for exact read predicates, but it is not a
        # mutation capability.  Callers must carry the private, trusted
        # CodeGraphScope object for every write path.
        if write and not isinstance(scope, CodeGraphScope):
            raise CodeGraphScopeError("codegraph writes require a trusted CodeGraphScope capability")
        checked = CodeGraphScope.from_value(scope)
        if not checked.trusted_context:
            raise CodeGraphScopeError("untrusted context cannot access codegraph")
        if canonical_project_ref(checked.workspace_id) != canonical_project_ref(self.workspace_id):
            raise CodeGraphScopeError("workspace scope mismatch")
        # Persist and compare one canonical spelling across Windows drive
        # case, slash direction and provider/runtime case.  Share group and
        # Agent IDs remain exact security dimensions.
        checked = replace(
            checked,
            workspace_id=self.workspace_id,
            project_ref=canonical_project_ref(checked.project_ref),
            provider=str(checked.provider or "").strip().casefold(),
            runtime_role=str(checked.runtime_role or "").strip().casefold(),
        )
        if any(value == UNKNOWN for value in checked.as_tuple()):
            raise CodeGraphScopeError("unknown ACL scope is blocked")
        if write and not checked.trusted_context:
            raise CodeGraphScopeError("trusted context required for codegraph writes")
        return checked

    @staticmethod
    def _scope_id(scope: CodeGraphScope) -> str:
        return stable_id("scope", *scope.as_tuple())

    def _ensure_scope(self, conn: sqlite3.Connection, scope: CodeGraphScope) -> str:
        scope_id = self._scope_id(scope)
        now = _now()
        conn.execute(
            "INSERT INTO graph_scopes(scope_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,runtime_role,trusted_context,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope_id) DO NOTHING",
            (scope_id, *_scope_row(scope), int(scope.trusted_context), now),
        )
        return scope_id

    @staticmethod
    def _ensure_symbol_external_schema(conn: sqlite3.Connection) -> None:
        """Keep external-id provenance in the existing governed map table."""

        return None

    @contextmanager
    def _write_transaction(
        self,
        scope: CodeGraphScope | Mapping[str, Any],
    ) -> Iterator[tuple[sqlite3.Connection, CodeGraphScope, str, str]]:
        """Open one atomic graph write unit for adapter-level snapshots."""

        checked_scope = self._scope(scope, write=True)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                scope_id = self._ensure_scope(conn, checked_scope)
                self._ensure_symbol_external_schema(conn)
                self._ensure_refresh_schema_conn(conn)
                yield conn, checked_scope, scope_id, _now()

    @staticmethod
    def _source_file_from_row(row: sqlite3.Row, scope: CodeGraphScope) -> SourceFile:
        return SourceFile(
            source_id=str(row["source_id"]),
            path=str(row["path"]),
            content_hash=str(row["content_hash"]),
            scope=scope,
            source_revision=str(row["source_revision"]),
            language=str(row["language"]),
            source_role=str(row["source_role"]),
            provenance=str(row["provenance"]),
            active=bool(row["active"]),
            revision_id=str(row["revision_id"]),
            file_id=str(row["file_id"]),
        )

    @staticmethod
    def _symbol_from_row(row: sqlite3.Row, scope: CodeGraphScope) -> Symbol:
        return Symbol(
            symbol_id=str(row["symbol_id"]),
            file_id=str(row["file_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            signature=str(row["signature"]),
            symbol_hash=str(row["symbol_hash"]),
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            provenance=str(row["provenance"]),
            source_map=_load_metadata_json(row["source_map_json"], label="symbol source map"),
            metadata=_load_metadata_json(row["metadata_json"], label="symbol metadata"),
            scope=scope,
            revision_id=str(row["revision_id"]),
            active=bool(row["active"]),
        )

    def _record_symbol_external_id_conn(
        self,
        conn: sqlite3.Connection,
        scope: CodeGraphScope,
        revision_id: str,
        external_id: str,
        canonical_symbol_id: str,
        now: str,
    ) -> str:
        external = str(external_id or "").strip()
        if not external:
            raise CodeGraphError("external symbol id is required")
        scope_id = self._scope_id(scope)
        source_db = "graphify"
        source_table = "symbol_external_id"
        source_pk = f"{scope_id}:{revision_id}:{external}"
        source_hash = stable_digest({"external_id": external, "revision_id": str(revision_id)})
        existing = conn.execute(
            "SELECT source_hash,target_id FROM migration_map "
            "WHERE source_db=? AND source_table=? AND source_pk=? AND target_type='symbol'",
            (source_db, source_table, source_pk),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != source_hash or str(existing[1]) != str(canonical_symbol_id):
                raise CodeGraphError("external symbol id maps to multiple canonical symbols")
            return stable_id("codegraph-map", source_db, source_table, source_pk, "symbol")
        mapping_id = stable_id("codegraph-map", source_db, source_table, source_pk, "symbol")
        conn.execute(
            "INSERT INTO migration_map(map_id,source_db,source_table,source_pk,source_hash,target_id,target_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mapping_id, source_db, source_table, source_pk, source_hash, str(canonical_symbol_id), "symbol", "mapped", now, now),
        )
        return mapping_id

    def resolve_external_symbol_id(
        self,
        external_id: str,
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        revision_id: str = "",
    ) -> str | None:
        """Resolve one Graphify ID to its persisted canonical symbol ID."""

        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        source_prefix = f"{scope_id}:"
        sql = (
            "SELECT m.target_id,m.source_pk FROM migration_map m "
            "JOIN symbols s ON s.symbol_id=m.target_id "
            "WHERE m.source_db='graphify' AND m.source_table='symbol_external_id' "
            "AND m.target_type='symbol' AND m.source_pk LIKE ? AND s.active=1"
        )
        params: list[Any] = [source_prefix + "%"]
        if revision_id:
            sql += " AND m.source_pk LIKE ?"; params.append(f"{source_prefix}{revision_id}:%")
        sql += " ORDER BY s.revision_id DESC,m.target_id"
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        suffix = ":" + str(external_id)
        for row in rows:
            if str(row[1]).endswith(suffix):
                return str(row[0])
        return None

    external_symbol_id = resolve_external_symbol_id

    def _scope_from_row(self, row: sqlite3.Row) -> CodeGraphScope:
        return CodeGraphScope(
            workspace_id=str(row["workspace_id"]),
            agent_instance_id=str(row["agent_instance_id"]),
            project_ref=str(row["project_ref"]),
            provider=str(row["provider"]),
            share_group_id=str(row["share_group_id"]),
            runtime_role=str(row["runtime_role"]),
            trusted_context=bool(row["trusted_context"]),
        )

    def _check_source_path(self, path: str | Path) -> str:
        normalized = normalize_relative_path(path)
        _safe_path_under_workspace(self.workspace, normalized)
        return normalized

    @staticmethod
    def _coerce_symbol(value: Symbol | Mapping[str, Any], *, file_id: str, scope: CodeGraphScope, revision_id: str) -> Symbol:
        if isinstance(value, Symbol):
            if value.file_id != file_id:
                raise CodeGraphError("symbol belongs to another source file")
            if value.scope is not None and value.scope.as_tuple() != scope.as_tuple():
                raise CodeGraphScopeError("symbol ACL scope mismatch")
            if value.revision_id and value.revision_id != revision_id:
                raise CodeGraphError("symbol revision does not match source file revision")
            return replace(value, scope=scope, revision_id=revision_id)
        if not isinstance(value, Mapping):
            raise TypeError("symbol must be Symbol or mapping")
        validate_metadata(value)
        supplied_revision = str(value.get("revision_id") or "")
        if supplied_revision and supplied_revision != revision_id:
            raise CodeGraphError("symbol revision does not match source file revision")
        symbol_id = str(value.get("symbol_id") or value.get("id") or stable_id("symbol", file_id, revision_id, value.get("name", ""), value.get("kind", "symbol"), value.get("signature", ""), value.get("line_start", 0), value.get("line_end", 0)))
        return Symbol(
            symbol_id=symbol_id,
            file_id=file_id,
            name=str(value.get("name") or value.get("label") or ""),
            kind=str(value.get("kind") or value.get("symbol_kind") or "symbol"),
            signature=str(value.get("signature") or value.get("signature_text") or ""),
            symbol_hash=str(value.get("symbol_hash") or value.get("hash") or ""),
            line_start=int(value.get("line_start") or value.get("start_line") or 0),
            line_end=int(value.get("line_end") or value.get("end_line") or 0),
            provenance=normalize_provenance(value.get("provenance") or "production"),
            source_map=dict(value.get("source_map") or {}),
            metadata=dict(value.get("metadata") or {}),
            scope=scope,
            revision_id=str(value.get("revision_id") or revision_id),
            active=bool(value.get("active", True)),
        )

    @staticmethod
    def _coerce_edge(value: Edge | Mapping[str, Any], *, scope: CodeGraphScope, revision_id: str) -> Edge:
        if isinstance(value, Edge):
            if value.scope is not None and value.scope.as_tuple() != scope.as_tuple():
                raise CodeGraphScopeError("edge ACL scope mismatch")
            if value.revision_id and revision_id and value.revision_id != revision_id:
                raise CodeGraphError("edge revision does not match requested revision")
            return replace(value, scope=scope, revision_id=value.revision_id or revision_id)
        if not isinstance(value, Mapping):
            raise TypeError("edge must be Edge or mapping")
        validate_metadata(value)
        from_id = str(value.get("from_id") or value.get("from") or value.get("source") or "")
        to_id = str(value.get("to_id") or value.get("to") or value.get("target") or "")
        relation = str(value.get("relation") or value.get("edge_kind") or value.get("type") or "related")
        context = str(value.get("context") or "")
        provenance = normalize_provenance(value.get("provenance") or "production")
        source_location = str(value.get("source_location") or "")
        edge_id = str(value.get("edge_id") or value.get("id") or stable_id("edge", scope.digest, revision_id, from_id, to_id, relation, context, provenance, source_location))
        return Edge(
            edge_id=edge_id,
            from_id=from_id,
            to_id=to_id,
            relation=relation,
            scope=scope,
            revision_id=str(value.get("revision_id") or revision_id),
            context=context,
            provenance=provenance,
            source_location=source_location,
            metadata=dict(value.get("metadata") or {}),
            weight=float(value.get("weight", 1.0)),
            active=bool(value.get("active", True)),
        )

    def _upsert_source_file_conn(
        self,
        conn: sqlite3.Connection,
        checked_scope: CodeGraphScope,
        *,
        path_value: str | Path,
        content_hash: str,
        source_revision: str = "",
        language: str = "",
        source_id: str = "",
        source_role: str = "production",
        provenance: str = "production",
        active: bool = True,
        symbols: Sequence[Symbol | Mapping[str, Any]] = (),
        edges: Sequence[Edge | Mapping[str, Any]] = (),
        fail_at: str | None = None,
        now: str | None = None,
    ) -> SourceFile:
        """Upsert one file on caller-owned transaction connection."""

        normalized_path = self._check_source_path(path_value)
        digest = str(content_hash or "").strip()
        if not digest:
            raise ValueError("content_hash is required; source text is not accepted")
        source_revision = str(source_revision or "")
        language = str(language or "")
        source_id = str(source_id or "")
        source_role = normalize_provenance(source_role, field_name="source_role")
        provenance = normalize_provenance(provenance)
        scope_id = self._scope_id(checked_scope)
        file_id = stable_id("file", scope_id, normalized_path)
        timestamp = str(now or _now())
        self._ensure_scope(conn, checked_scope)
        existing = conn.execute("SELECT * FROM source_files WHERE file_id=?", (file_id,)).fetchone()
        if existing is not None and str(existing["scope_id"]) != scope_id:
            raise CodeGraphScopeError("source file scope collision")
        unchanged = (
            existing is not None
            and str(existing["content_hash"]) == digest
            and str(existing["source_revision"]) == source_revision
            and str(existing["language"]) == language
            and str(existing["source_role"]) == source_role
            and str(existing["provenance"]) == provenance
            and bool(existing["active"]) == bool(active)
        )
        revision_id = str(existing["revision_id"] or "") if unchanged else stable_id("revision", file_id, digest, source_revision)
        if not unchanged:
            revision_row = conn.execute(
                "SELECT revision_id,revision_number,content_hash,source_revision FROM revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO source_files(file_id,scope_id,source_id,path,content_hash,source_revision,language,source_role,provenance,revision_id,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(file_id) DO UPDATE SET source_id=excluded.source_id,path=excluded.path,content_hash=excluded.content_hash,source_revision=excluded.source_revision,language=excluded.language,source_role=excluded.source_role,provenance=excluded.provenance,active=excluded.active,updated_at=excluded.updated_at",
                (file_id, scope_id, source_id, normalized_path, digest, source_revision, language, source_role, provenance, "", int(active), timestamp, timestamp),
            )
            if revision_row is not None:
                if str(revision_row["content_hash"]) != digest or str(revision_row["source_revision"]) != source_revision:
                    raise CodeGraphError("immutable revision identity collision")
            else:
                max_row = conn.execute("SELECT COALESCE(MAX(revision_number),0) FROM revisions WHERE file_id=?", (file_id,)).fetchone()
                conn.execute(
                    "INSERT INTO revisions(revision_id,file_id,scope_id,content_hash,source_revision,revision_number,created_at) VALUES(?,?,?,?,?,?,?)",
                    (revision_id, file_id, scope_id, digest, source_revision, int(max_row[0]) + 1, timestamp),
                )
            conn.execute("UPDATE source_files SET revision_id=?,updated_at=? WHERE file_id=?", (revision_id, timestamp, file_id))
            if existing is not None:
                old_symbol_ids = [str(item[0]) for item in conn.execute("SELECT symbol_id FROM symbols WHERE file_id=? AND active=1", (file_id,)).fetchall()]
                if old_symbol_ids:
                    placeholders = ",".join("?" for _ in old_symbol_ids)
                    conn.execute(
                        f"UPDATE edges SET active=0 WHERE active=1 AND (from_id IN ({placeholders}) OR to_id IN ({placeholders}))",
                        (*old_symbol_ids, *old_symbol_ids),
                    )
                    conn.execute("UPDATE symbols SET active=0 WHERE file_id=? AND active=1", (file_id,))
            if fail_at == "after_file":
                raise CodeGraphError("injected codegraph failure after file")
            for symbol_value in symbols:
                symbol = self._coerce_symbol(symbol_value, file_id=file_id, scope=checked_scope, revision_id=revision_id)
                self._insert_symbol(conn, symbol, scope_id, timestamp)
            for edge_value in edges:
                edge = self._coerce_edge(edge_value, scope=checked_scope, revision_id=revision_id)
                self._insert_edge(conn, edge, scope_id, timestamp)
            self._append_outbox_conn(
                conn,
                checked_scope,
                "source_file.upsert" if active else "source_file.tombstone",
                file_id,
                stable_digest({"file_id": file_id, "revision_id": revision_id, "content_hash": digest, "active": bool(active)}),
                timestamp,
            )
        row = conn.execute(
            "SELECT f.*,r.revision_number FROM source_files f LEFT JOIN revisions r ON r.revision_id=f.revision_id WHERE f.file_id=?",
            (file_id,),
        ).fetchone()
        if row is None:
            raise CodeGraphError("source file insert failed")
        return self._source_file_from_row(row, checked_scope)

    def upsert_source_file(
        self,
        path: str | Path | SourceFile | Mapping[str, Any],
        content_hash: str | None = None,
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        source_revision: str = "",
        language: str = "",
        source_id: str = "",
        source_role: str = "production",
        provenance: str = "production",
        active: bool = True,
        symbols: Sequence[Symbol | Mapping[str, Any]] = (),
        edges: Sequence[Edge | Mapping[str, Any]] = (),
        fail_at: str | None = None,
    ) -> SourceFile:
        """Insert one file head and immutable revision idempotently."""

        checked_scope = self._scope(scope, write=True)
        if isinstance(path, SourceFile):
            source = path
            if source.scope.as_tuple() != checked_scope.as_tuple():
                raise CodeGraphScopeError("source file ACL scope mismatch")
            path_value = source.path
            content_hash = source.content_hash
            source_revision = source.source_revision
            language = source.language
            source_id = source.source_id
            source_role = source.source_role
            provenance = source.provenance
            active = source.active
        elif isinstance(path, Mapping):
            data = path
            validate_metadata(data)
            path_value = str(data.get("path") or data.get("relative_path") or "")
            content_hash = str(data.get("content_hash") or data.get("hash") or content_hash or "")
            source_revision = str(data.get("source_revision") or source_revision)
            language = str(data.get("language") or language)
            source_id = str(data.get("source_id") or source_id)
            source_role = normalize_provenance(data.get("source_role") or source_role, field_name="source_role")
            provenance = normalize_provenance(data.get("provenance") or provenance)
            active = bool(data.get("active", active))
            symbols = tuple(data.get("symbols") or symbols)
            edges = tuple(data.get("edges") or edges)
        else:
            path_value = str(path)
        normalized_path = self._check_source_path(path_value)
        digest = str(content_hash or "").strip()
        if not digest:
            raise ValueError("content_hash is required; source text is not accepted")
        source_revision = str(source_revision or "")
        language = str(language or "")
        source_id = str(source_id or "")
        source_role = normalize_provenance(source_role, field_name="source_role")
        provenance = normalize_provenance(provenance)
        scope_id = self._scope_id(checked_scope)
        file_id = stable_id("file", scope_id, normalized_path)
        now = _now()

        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                existing = conn.execute("SELECT * FROM source_files WHERE file_id=?", (file_id,)).fetchone()
                if existing is not None and str(existing["scope_id"]) != scope_id:
                    raise CodeGraphScopeError("source file scope collision")
                unchanged = existing is not None and str(existing["content_hash"]) == digest and str(existing["source_revision"]) == source_revision and str(existing["language"]) == language and str(existing["source_role"]) == source_role and str(existing["provenance"]) == provenance and bool(existing["active"]) == bool(active)
                revision_id = str(existing["revision_id"] or "") if unchanged else stable_id("revision", file_id, digest, source_revision)
                revision_number = int(existing["revision_number"]) if False else 0
                if not unchanged:
                    revision_row = conn.execute("SELECT revision_id,revision_number,content_hash,source_revision FROM revisions WHERE revision_id=?", (revision_id,)).fetchone()
                    # ``revisions.file_id`` is a real FK.  Establish the file
                    # head first (with an empty head pointer), then append the
                    # immutable revision and finally point the head at it.
                    conn.execute(
                        "INSERT INTO source_files(file_id,scope_id,source_id,path,content_hash,source_revision,language,source_role,provenance,revision_id,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET source_id=excluded.source_id,path=excluded.path,content_hash=excluded.content_hash,source_revision=excluded.source_revision,language=excluded.language,source_role=excluded.source_role,provenance=excluded.provenance,active=excluded.active,updated_at=excluded.updated_at",
                        (file_id, scope_id, source_id, normalized_path, digest, source_revision, language, source_role, provenance, "", int(active), now, now),
                    )
                    if revision_row is not None:
                        if str(revision_row["content_hash"]) != digest or str(revision_row["source_revision"]) != source_revision:
                            raise CodeGraphError("immutable revision identity collision")
                        revision_number = int(revision_row["revision_number"])
                    else:
                        max_row = conn.execute("SELECT COALESCE(MAX(revision_number),0) FROM revisions WHERE file_id=?", (file_id,)).fetchone()
                        revision_number = int(max_row[0]) + 1
                        conn.execute("INSERT INTO revisions(revision_id,file_id,scope_id,content_hash,source_revision,revision_number,created_at) VALUES(?,?,?,?,?,?,?)", (revision_id, file_id, scope_id, digest, source_revision, revision_number, now))
                    conn.execute("UPDATE source_files SET revision_id=?,updated_at=? WHERE file_id=?", (revision_id, now, file_id))
                    if existing is not None and not unchanged:
                        old_symbol_ids = [str(item[0]) for item in conn.execute("SELECT symbol_id FROM symbols WHERE file_id=? AND active=1", (file_id,)).fetchall()]
                        if old_symbol_ids:
                            placeholders = ",".join("?" for _ in old_symbol_ids)
                            conn.execute(f"UPDATE edges SET active=0 WHERE active=1 AND (from_id IN ({placeholders}) OR to_id IN ({placeholders}))", (*old_symbol_ids, *old_symbol_ids))
                            conn.execute("UPDATE symbols SET active=0 WHERE file_id=? AND active=1", (file_id,))
                    if fail_at == "after_file":
                        raise CodeGraphError("injected codegraph failure after file")
                    for symbol_value in symbols:
                        symbol = self._coerce_symbol(symbol_value, file_id=file_id, scope=checked_scope, revision_id=revision_id)
                        self._insert_symbol(conn, symbol, scope_id, now)
                    for edge_value in edges:
                        edge = self._coerce_edge(edge_value, scope=checked_scope, revision_id=revision_id)
                        self._insert_edge(conn, edge, scope_id, now)
                    self._append_outbox_conn(conn, checked_scope, "source_file.upsert" if active else "source_file.tombstone", file_id, stable_digest({"file_id": file_id, "revision_id": revision_id, "content_hash": digest, "active": bool(active)}), now)
                row = conn.execute("SELECT f.*,r.revision_number FROM source_files f LEFT JOIN revisions r ON r.revision_id=f.revision_id WHERE f.file_id=?", (file_id,)).fetchone()
                if row is None:
                    raise CodeGraphError("source file insert failed")
                return SourceFile(
                    source_id=str(row["source_id"]),
                    path=str(row["path"]),
                    content_hash=str(row["content_hash"]),
                    scope=checked_scope,
                    source_revision=str(row["source_revision"]),
                    language=str(row["language"]),
                    source_role=str(row["source_role"]),
                    provenance=str(row["provenance"]),
                    active=bool(row["active"]),
                    revision_id=str(row["revision_id"]),
                    file_id=str(row["file_id"]),
                )

    put_source_file = upsert_source_file
    upsert_file = upsert_source_file
    put_file = upsert_source_file

    def _insert_symbol(self, conn: sqlite3.Connection, symbol: Symbol, scope_id: str, now: str) -> str:
        revision = conn.execute(
            "SELECT file_id,scope_id FROM revisions WHERE revision_id=?",
            (symbol.revision_id,),
        ).fetchone()
        if revision is None or str(revision[0]) != symbol.file_id or str(revision[1]) != scope_id:
            raise CodeGraphError("symbol revision is not owned by its source file and scope")
        source = conn.execute(
            "SELECT scope_id FROM source_files WHERE file_id=?",
            (symbol.file_id,),
        ).fetchone()
        if source is None or str(source[0]) != scope_id:
            raise CodeGraphScopeError("symbol source file scope mismatch")
        symbol_hash = symbol.signature_hash
        identity = (
            symbol.file_id,
            symbol.revision_id,
            symbol.name,
            symbol.kind,
            symbol_hash,
            symbol.line_start,
            symbol.line_end,
        )
        canonical = conn.execute(
            "SELECT symbol_id,active FROM symbols "
            "WHERE file_id=? AND revision_id=? AND name=? AND kind=? "
            "AND symbol_hash=? AND line_start=? AND line_end=? "
            "ORDER BY symbol_id LIMIT 1",
            identity,
        ).fetchone()
        if canonical is not None:
            canonical_id = str(canonical["symbol_id"])
            if symbol.active and not bool(canonical["active"]):
                conn.execute("UPDATE symbols SET active=1 WHERE symbol_id=?", (canonical_id,))
            return canonical_id
        existing = conn.execute("SELECT file_id,scope_id,revision_id,name,kind,signature,symbol_hash,line_start,line_end,provenance,source_map_json,metadata_json,active FROM symbols WHERE symbol_id=?", (symbol.symbol_id,)).fetchone()
        values = (symbol.file_id, scope_id, symbol.revision_id, symbol.name, symbol.kind, symbol.signature, symbol_hash, symbol.line_start, symbol.line_end, symbol.provenance, _json(symbol.source_map), _json(symbol.metadata), int(symbol.active))
        if existing is not None:
            if tuple(str(item) for item in existing) != tuple(str(item) for item in values):
                raise CodeGraphError("immutable symbol identity collision")
            return symbol.symbol_id
        conn.execute("INSERT INTO symbols(symbol_id,file_id,scope_id,revision_id,name,kind,signature,symbol_hash,line_start,line_end,provenance,source_map_json,metadata_json,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (symbol.symbol_id, symbol.file_id, scope_id, symbol.revision_id, symbol.name, symbol.kind, symbol.signature, symbol_hash, symbol.line_start, symbol.line_end, symbol.provenance, _json(symbol.source_map), _json(symbol.metadata), int(symbol.active), now))
        return symbol.symbol_id

    def _put_symbols_conn(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        symbols: Sequence[Symbol | Mapping[str, Any]],
        *,
        checked_scope: CodeGraphScope,
        revision_id: str = "",
        external_ids: Sequence[str] | None = None,
        now: str | None = None,
    ) -> tuple[Symbol, ...]:
        scope_id = self._scope_id(checked_scope)
        timestamp = str(now or _now())
        file = conn.execute(
            "SELECT revision_id FROM source_files WHERE file_id=? AND scope_id=?",
            (str(file_id), scope_id),
        ).fetchone()
        if file is None:
            raise CodeGraphError("unknown source file")
        selected_revision = str(revision_id or file[0] or "")
        revision = conn.execute(
            "SELECT file_id,scope_id FROM revisions WHERE revision_id=?",
            (selected_revision,),
        ).fetchone()
        if revision is None or str(revision[0]) != str(file_id) or str(revision[1]) != scope_id:
            raise CodeGraphError("unknown source file revision")
        if external_ids is not None and len(external_ids) != len(symbols):
            raise CodeGraphError("external symbol id batch length mismatch")
        output: list[Symbol] = []
        for index, value in enumerate(symbols):
            symbol = self._coerce_symbol(value, file_id=str(file_id), scope=checked_scope, revision_id=selected_revision)
            canonical_id = self._insert_symbol(conn, symbol, scope_id, timestamp)
            if external_ids is not None:
                self._record_symbol_external_id_conn(
                    conn,
                    checked_scope,
                    selected_revision,
                    str(external_ids[index]),
                    canonical_id,
                    timestamp,
                )
            row = conn.execute("SELECT * FROM symbols WHERE symbol_id=?", (canonical_id,)).fetchone()
            if row is None:
                raise CodeGraphError("symbol insert failed")
            output.append(self._symbol_from_row(row, checked_scope))
        if output:
            self._append_outbox_conn(
                conn,
                checked_scope,
                "symbols.upsert",
                str(file_id),
                stable_digest([item.to_dict() for item in output]),
                timestamp,
            )
        return tuple(output)

    def put_symbols(self, file_id: str, symbols: Sequence[Symbol | Mapping[str, Any]], *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "") -> tuple[Symbol, ...]:
        checked_scope = self._scope(scope, write=True)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                self._ensure_symbol_external_schema(conn)
                return self._put_symbols_conn(conn, str(file_id), symbols, checked_scope=checked_scope, revision_id=revision_id)

    put_symbol_batch = put_symbols

    def _insert_edge(self, conn: sqlite3.Connection, edge: Edge, scope_id: str, now: str) -> str:
        # Endpoints must be metadata identities in this scope; no arbitrary
        # cross-tenant edge can be smuggled into the graph.
        revision = conn.execute("SELECT scope_id FROM revisions WHERE revision_id=?", (edge.revision_id,)).fetchone()
        if revision is None or str(revision[0]) != scope_id:
            raise CodeGraphError("edge revision is not present in the requested scope")
        from_row = conn.execute("SELECT symbol_id,scope_id,revision_id FROM symbols WHERE symbol_id=? AND scope_id=? AND active=1", (edge.from_id, scope_id)).fetchone()
        to_row = conn.execute("SELECT symbol_id,scope_id,revision_id FROM symbols WHERE symbol_id=? AND scope_id=? AND active=1", (edge.to_id, scope_id)).fetchone()
        # Self-loops still need a real endpoint; checking the two lookups
        # separately prevents the old count shortcut from creating orphans.
        if from_row is None or to_row is None:
            raise CodeGraphError("edge endpoint is not a symbol in the requested scope")
        # Edge revision belongs to the source-side extraction revision. Cross-file
        # targets may legitimately have another revision, but the source may not.
        if str(from_row["revision_id"] or "") != edge.revision_id:
            raise CodeGraphError("edge revision does not match source symbol revision")
        existing = conn.execute("SELECT scope_id,revision_id,from_id,to_id,relation,context,provenance,source_location,metadata_json,weight,active FROM edges WHERE edge_id=?", (edge.edge_id,)).fetchone()
        values = (scope_id, edge.revision_id, edge.from_id, edge.to_id, edge.relation, edge.context, edge.provenance, edge.source_location, _json(edge.metadata), edge.weight, int(edge.active))
        if existing is not None:
            existing_values = tuple(existing)
            if tuple(str(item) for item in existing_values[:-1]) != tuple(str(item) for item in values[:-1]):
                raise CodeGraphError("immutable edge identity collision")
            if edge.active and not bool(existing_values[-1]):
                conn.execute("UPDATE edges SET active=1 WHERE edge_id=?", (edge.edge_id,))
            return edge.edge_id
        conn.execute("INSERT INTO edges(edge_id,scope_id,revision_id,from_id,to_id,relation,context,provenance,source_location,metadata_json,weight,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (edge.edge_id, scope_id, edge.revision_id, edge.from_id, edge.to_id, edge.relation, edge.context, edge.provenance, edge.source_location, _json(edge.metadata), edge.weight, int(edge.active), now))
        return edge.edge_id

    def _put_edges_conn(
        self,
        conn: sqlite3.Connection,
        edges: Sequence[Edge | Mapping[str, Any]],
        *,
        checked_scope: CodeGraphScope,
        revision_id: str = "",
        now: str | None = None,
    ) -> tuple[Edge, ...]:
        scope_id = self._scope_id(checked_scope)
        timestamp = str(now or _now())
        output: list[Edge] = []
        for value in edges:
            edge = self._coerce_edge(
                value,
                scope=checked_scope,
                revision_id=revision_id or (value.revision_id if isinstance(value, Edge) else str(value.get("revision_id") or "")),
            )
            if not edge.revision_id:
                revision_rows = conn.execute(
                    "SELECT DISTINCT revision_id FROM symbols WHERE symbol_id IN (?,?) AND scope_id=? AND active=1 ORDER BY revision_id",
                    (edge.from_id, edge.to_id, scope_id),
                ).fetchall()
                if len(revision_rows) == 1 and str(revision_rows[0][0]):
                    edge = replace(edge, revision_id=str(revision_rows[0][0]))
                else:
                    raise CodeGraphError("edge revision is required and endpoints must agree")
            self._insert_edge(conn, edge, scope_id, timestamp)
            output.append(edge)
        if output:
            self._append_outbox_conn(
                conn,
                checked_scope,
                "edges.upsert",
                stable_id("edge-batch", checked_scope.digest, *[item.edge_id for item in output]),
                stable_digest([item.to_dict() for item in output]),
                timestamp,
            )
        return tuple(output)

    def put_edges(self, edges: Sequence[Edge | Mapping[str, Any]], *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "") -> tuple[Edge, ...]:
        checked_scope = self._scope(scope, write=True)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                return self._put_edges_conn(conn, edges, checked_scope=checked_scope, revision_id=revision_id)

    put_edge_batch = put_edges

    def get_source_file(self, file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> SourceFile | None:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM source_files WHERE file_id=? AND scope_id=?", (str(file_id), scope_id)).fetchone()
        if row is None:
            return None
        return SourceFile(source_id=str(row["source_id"]), path=str(row["path"]), content_hash=str(row["content_hash"]), scope=checked_scope, source_revision=str(row["source_revision"]), language=str(row["language"]), source_role=str(row["source_role"]), provenance=str(row["provenance"]), active=bool(row["active"]), revision_id=str(row["revision_id"]), file_id=str(row["file_id"]))

    read_source_file = get_source_file

    def list_source_files(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, active_only: bool = True) -> tuple[SourceFile, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM source_files WHERE scope_id=?" + (" AND active=1" if active_only else "") + " ORDER BY path,file_id"
        with self.connection() as conn:
            rows = conn.execute(sql, (scope_id,)).fetchall()
        return tuple(SourceFile(source_id=str(row["source_id"]), path=str(row["path"]), content_hash=str(row["content_hash"]), scope=checked_scope, source_revision=str(row["source_revision"]), language=str(row["language"]), source_role=str(row["source_role"]), provenance=str(row["provenance"]), active=bool(row["active"]), revision_id=str(row["revision_id"]), file_id=str(row["file_id"])) for row in rows)

    list_files = list_source_files

    def get_symbols(self, file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "") -> tuple[Symbol, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM symbols WHERE file_id=? AND scope_id=? AND active=1"
        params: list[Any] = [str(file_id), scope_id]
        if revision_id:
            sql += " AND revision_id=?"; params.append(str(revision_id))
        else:
            sql += " AND revision_id=(SELECT revision_id FROM source_files WHERE file_id=? AND scope_id=?)"
            params.extend((str(file_id), scope_id))
        sql += " ORDER BY line_start,line_end,name,symbol_id"
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(Symbol(symbol_id=str(row["symbol_id"]), file_id=str(row["file_id"]), name=str(row["name"]), kind=str(row["kind"]), signature=str(row["signature"]), symbol_hash=str(row["symbol_hash"]), line_start=int(row["line_start"]), line_end=int(row["line_end"]), provenance=str(row["provenance"]), source_map=_load_metadata_json(row["source_map_json"], label="symbol source map"), metadata=_load_metadata_json(row["metadata_json"], label="symbol metadata"), scope=checked_scope, revision_id=str(row["revision_id"]), active=bool(row["active"])) for row in rows)

    list_symbols = get_symbols

    def list_revisions(self, file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> tuple[Revision, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM revisions WHERE file_id=? AND scope_id=? ORDER BY revision_number,revision_id", (str(file_id), scope_id)).fetchall()
        return tuple(Revision(str(row["revision_id"]), str(row["file_id"]), str(row["content_hash"]), str(row["source_revision"]), int(row["revision_number"]), str(row["created_at"]), True) for row in rows)

    revisions = list_revisions

    def get_revision(self, revision_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> Revision | None:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM revisions WHERE revision_id=? AND scope_id=?", (str(revision_id), scope_id)).fetchone()
        if row is None:
            return None
        return Revision(str(row["revision_id"]), str(row["file_id"]), str(row["content_hash"]), str(row["source_revision"]), int(row["revision_number"]), str(row["created_at"]), True)

    read_revision = get_revision

    def list_edges(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, revision_id: str = "", relation: str = "") -> tuple[Edge, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM edges WHERE scope_id=? AND active=1"; params: list[Any] = [scope_id]
        if revision_id:
            sql += " AND revision_id=?"; params.append(str(revision_id))
        if relation:
            sql += " AND relation=?"; params.append(str(relation))
        sql += " ORDER BY from_id,to_id,relation,edge_id"
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(Edge(edge_id=str(row["edge_id"]), from_id=str(row["from_id"]), to_id=str(row["to_id"]), relation=str(row["relation"]), scope=checked_scope, revision_id=str(row["revision_id"]), context=str(row["context"]), provenance=str(row["provenance"]), source_location=str(row["source_location"]), metadata=_load_metadata_json(row["metadata_json"], label="edge metadata"), weight=float(row["weight"]), active=bool(row["active"])) for row in rows)

    edges = list_edges

    def _list_source_files_conn(
        self,
        conn: sqlite3.Connection,
        checked_scope: CodeGraphScope,
        *,
        active_only: bool = True,
    ) -> tuple[SourceFile, ...]:
        scope_id = self._scope_id(checked_scope)
        sql = "SELECT * FROM source_files WHERE scope_id=?" + (" AND active=1" if active_only else "") + " ORDER BY path,file_id"
        rows = conn.execute(sql, (scope_id,)).fetchall()
        return tuple(self._source_file_from_row(row, checked_scope) for row in rows)

    def _tombstone_source_file_conn(
        self,
        conn: sqlite3.Connection,
        checked_scope: CodeGraphScope,
        path_or_file_id: str,
        *,
        reason: str = "deleted",
        now: str | None = None,
    ) -> tuple[str, bool]:
        scope_id = self._scope_id(checked_scope)
        timestamp = str(now or _now())
        if "/" in str(path_or_file_id) or "\\" in str(path_or_file_id):
            path = self._check_source_path(path_or_file_id)
            row = conn.execute(
                "SELECT file_id,revision_id FROM source_files WHERE scope_id=? AND path=?",
                (scope_id, path),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT file_id,revision_id FROM source_files WHERE scope_id=? AND file_id=?",
                (scope_id, str(path_or_file_id)),
            ).fetchone()
        if row is None:
            return stable_id("tombstone", scope_id, str(path_or_file_id), reason), False
        file_id, revision_id = str(row[0]), str(row[1])
        tombstone_id = stable_id("tombstone", file_id, reason)
        symbol_ids = [
            str(item[0])
            for item in conn.execute(
                "SELECT symbol_id FROM symbols WHERE file_id=? AND scope_id=? AND active=1",
                (file_id, scope_id),
            ).fetchall()
        ]
        if symbol_ids:
            placeholders = ",".join("?" for _ in symbol_ids)
            conn.execute(
                f"UPDATE edges SET active=0 WHERE scope_id=? AND active=1 AND (from_id IN ({placeholders}) OR to_id IN ({placeholders}))",
                (scope_id, *symbol_ids, *symbol_ids),
            )
            conn.execute("UPDATE symbols SET active=0 WHERE file_id=? AND scope_id=? AND active=1", (file_id, scope_id))
        conn.execute("UPDATE source_files SET active=0,updated_at=? WHERE file_id=?", (timestamp, file_id))
        conn.execute(
            "INSERT INTO source_tombstones(tombstone_id,file_id,scope_id,reason,revision_id,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(file_id,reason) DO NOTHING",
            (tombstone_id, file_id, scope_id, str(reason), revision_id, timestamp),
        )
        self._append_outbox_conn(
            conn,
            checked_scope,
            "source_file.tombstone",
            file_id,
            stable_digest({"file_id": file_id, "revision_id": revision_id, "reason": str(reason)}),
            timestamp,
        )
        return tombstone_id, True

    def tombstone_source_file(self, path_or_file_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, reason: str = "deleted") -> str:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                if "/" in str(path_or_file_id) or "\\" in str(path_or_file_id):
                    path = self._check_source_path(path_or_file_id)
                    row = conn.execute("SELECT file_id,revision_id FROM source_files WHERE scope_id=? AND path=?", (scope_id, path)).fetchone()
                else:
                    row = conn.execute("SELECT file_id,revision_id FROM source_files WHERE scope_id=? AND file_id=?", (scope_id, str(path_or_file_id))).fetchone()
                if row is None:
                    # Deleting an unknown path is an existence-neutral no-op;
                    # callers receive a stable tombstone identity.
                    return stable_id("tombstone", scope_id, str(path_or_file_id), reason)
                file_id, revision_id = str(row[0]), str(row[1])
                tombstone_id = stable_id("tombstone", file_id, reason)
                symbol_ids = [str(item[0]) for item in conn.execute("SELECT symbol_id FROM symbols WHERE file_id=? AND scope_id=? AND active=1", (file_id, scope_id)).fetchall()]
                if symbol_ids:
                    placeholders = ",".join("?" for _ in symbol_ids)
                    conn.execute(f"UPDATE edges SET active=0 WHERE scope_id=? AND active=1 AND (from_id IN ({placeholders}) OR to_id IN ({placeholders}))", (scope_id, *symbol_ids, *symbol_ids))
                    conn.execute("UPDATE symbols SET active=0 WHERE file_id=? AND scope_id=? AND active=1", (file_id, scope_id))
                conn.execute("UPDATE source_files SET active=0,updated_at=? WHERE file_id=?", (now, file_id))
                conn.execute("INSERT INTO source_tombstones(tombstone_id,file_id,scope_id,reason,revision_id,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(file_id,reason) DO NOTHING", (tombstone_id, file_id, scope_id, str(reason), revision_id, now))
                self._append_outbox_conn(conn, checked_scope, "source_file.tombstone", file_id, stable_digest({"file_id": file_id, "revision_id": revision_id, "reason": str(reason)}), now)
                return tombstone_id

    delete_source_file = tombstone_source_file
    tombstone = tombstone_source_file

    @staticmethod
    def _provenance_filter(value: str) -> str:
        return normalize_provenance(value) if str(value or "").strip() else ""

    def affected_query(self, start_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, depth: int = 2, limit: int = 100, relation: str = "", provenance: str = "") -> AffectedQuery:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        bounded_depth = min(max(0, int(depth)), 32)
        bounded_limit = min(max(1, int(limit)), 10_000)
        relation_filter = str(relation or "")
        provenance_filter = self._provenance_filter(provenance)
        with self.connection() as conn:
            known_sql = "SELECT 1 FROM symbols WHERE symbol_id=? AND scope_id=? AND active=1"
            known_params: list[Any] = [str(start_id), scope_id]
            if provenance_filter:
                known_sql += " AND provenance=?"; known_params.append(provenance_filter)
            known = conn.execute(known_sql, known_params).fetchone()
            if known is None:
                raise CodeGraphError("affected query start_id is not a symbol in the requested scope")
            visited: set[str] = {str(start_id)}
            frontier = [str(start_id)]
            for _level in range(bounded_depth):
                if not frontier or len(visited) - 1 >= bounded_limit:
                    break
                placeholders = ",".join("?" for _ in frontier)
                sql = (
                    "SELECT e.from_id,e.to_id,e.relation FROM edges e "
                    "JOIN symbols sf ON sf.symbol_id=e.from_id AND sf.scope_id=e.scope_id AND sf.active=1 "
                    "JOIN symbols st ON st.symbol_id=e.to_id AND st.scope_id=e.scope_id AND st.active=1 "
                    f"WHERE e.scope_id=? AND e.active=1 AND e.to_id IN ({placeholders})"
                )
                params: list[Any] = [scope_id, *frontier]
                if relation_filter:
                    sql += " AND e.relation=?"; params.append(relation_filter)
                if provenance_filter:
                    sql += " AND e.provenance=? AND sf.provenance=? AND st.provenance=?"
                    params.extend((provenance_filter, provenance_filter, provenance_filter))
                sql += " ORDER BY e.from_id,e.to_id,e.relation,e.context,e.source_location,e.edge_id"
                next_frontier: list[str] = []
                for row in conn.execute(sql, params).fetchall():
                    candidate = str(row[0])
                    if candidate in visited:
                        continue
                    visited.add(candidate); next_frontier.append(candidate)
                    if len(visited) - 1 >= bounded_limit:
                        break
                frontier = sorted(next_frontier)
        result_ids = tuple(sorted(visited - {str(start_id)}))[:bounded_limit]
        result_digest = stable_digest((relation_filter, provenance_filter, result_ids))
        query_id = stable_id("affected", scope_id, start_id, bounded_depth, bounded_limit, relation_filter, provenance_filter, result_digest)
        now = _now()
        # Query receipts are deterministic return values only. A read must not
        # append to the authoritative CodeGraph DB, especially in V2_READY.
        return AffectedQuery(query_id=query_id, scope=checked_scope, start_id=str(start_id), depth=bounded_depth, limit=bounded_limit, relation_filter=relation_filter, provenance_filter=provenance_filter, result_ids=result_ids, digest=result_digest, created_at=now)

    query_affected = affected_query

    def affected(self, start_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, depth: int = 2, limit: int = 100, relation: str = "", provenance: str = "") -> list[str]:
        return list(self.affected_query(start_id, scope=scope, depth=depth, limit=limit, relation=relation, provenance=provenance).result_ids)

    find_affected = affected

    def query_symbols(self, query: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, provenance: str = "", limit: int = 100) -> tuple[Symbol, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        text = str(query or "").strip()
        if not text:
            raise CodeGraphError("codegraph query is required")
        bounded = min(max(1, int(limit)), 1000)
        provenance_filter = self._provenance_filter(provenance)
        sql = "SELECT s.* FROM symbols s JOIN source_files f ON f.file_id=s.file_id AND f.scope_id=s.scope_id WHERE s.scope_id=? AND s.active=1 AND f.active=1 AND (s.name LIKE ? OR s.signature LIKE ? OR s.kind LIKE ?)"
        params: list[Any] = [scope_id, f"%{text}%", f"%{text}%", f"%{text}%"]
        if provenance_filter:
            sql += " AND s.provenance=? AND f.provenance=?"; params.extend((provenance_filter, provenance_filter))
        sql += " ORDER BY CASE WHEN s.name=? THEN 0 WHEN s.name LIKE ? THEN 1 ELSE 2 END,s.name,s.symbol_id LIMIT ?"
        params.extend((text, f"{text}%", bounded))
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(
            Symbol(symbol_id=str(row["symbol_id"]), file_id=str(row["file_id"]), name=str(row["name"]), kind=str(row["kind"]), signature=str(row["signature"]), symbol_hash=str(row["symbol_hash"]), line_start=int(row["line_start"]), line_end=int(row["line_end"]), provenance=str(row["provenance"]), source_map=_load_metadata_json(row["source_map_json"], label="symbol source map"), metadata=_load_metadata_json(row["metadata_json"], label="symbol metadata"), scope=checked_scope, revision_id=str(row["revision_id"]), active=bool(row["active"]))
            for row in rows
        )

    query = query_symbols

    def path_query(self, start_id: str, end_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, max_depth: int = 8, relation: str = "", provenance: str = "") -> dict[str, Any]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        start, end = str(start_id), str(end_id)
        depth = min(max(1, int(max_depth)), 32)
        provenance_filter = self._provenance_filter(provenance)
        with self.connection() as conn:
            endpoint_sql = "SELECT symbol_id FROM symbols WHERE scope_id=? AND active=1 AND symbol_id IN (?,?)"
            endpoint_params: list[Any] = [scope_id, start, end]
            if provenance_filter:
                endpoint_sql += " AND provenance=?"
                endpoint_params.append(provenance_filter)
            required = {str(row[0]) for row in conn.execute(endpoint_sql, endpoint_params).fetchall()}
            if required != {start, end}:
                raise CodeGraphError("path endpoints are not symbols in the requested scope/provenance")
            queue: deque[tuple[str, tuple[str, ...], int]] = deque([(start, (start,), 0)])
            seen = {start}
            found: tuple[str, ...] = ()
            while queue:
                current, chain, level = queue.popleft()
                if level >= depth:
                    continue
                sql = "SELECT e.to_id FROM edges e JOIN symbols sf ON sf.symbol_id=e.from_id AND sf.scope_id=e.scope_id AND sf.active=1 JOIN symbols st ON st.symbol_id=e.to_id AND st.scope_id=e.scope_id AND st.active=1 WHERE e.scope_id=? AND e.active=1 AND e.from_id=?"
                params: list[Any] = [scope_id, current]
                if relation:
                    sql += " AND e.relation=?"; params.append(str(relation))
                if provenance_filter:
                    sql += " AND e.provenance=? AND sf.provenance=? AND st.provenance=?"; params.extend((provenance_filter, provenance_filter, provenance_filter))
                sql += " ORDER BY e.to_id,e.relation,e.context,e.source_location,e.edge_id"
                for row in conn.execute(sql, params).fetchall():
                    candidate = str(row[0])
                    next_chain = (*chain, candidate)
                    if candidate == end:
                        found = next_chain; queue.clear(); break
                    if candidate in seen:
                        continue
                    seen.add(candidate); queue.append((candidate, next_chain, level + 1))
        return {"start_id": start, "end_id": end, "path": list(found), "hops": max(0, len(found) - 1), "found": bool(found), "provenance": provenance_filter}

    path = path_query

    def explain_symbol(self, symbol_id: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, provenance: str = "", edge_limit: int = 50) -> dict[str, Any]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        provenance_filter = self._provenance_filter(provenance)
        with self.connection() as conn:
            sql = "SELECT s.*,f.path,f.language,f.source_role,f.provenance AS file_provenance FROM symbols s JOIN source_files f ON f.file_id=s.file_id WHERE s.symbol_id=? AND s.scope_id=? AND s.active=1 AND f.active=1"
            params: list[Any] = [str(symbol_id), scope_id]
            if provenance_filter:
                sql += " AND s.provenance=? AND f.provenance=?"; params.extend((provenance_filter, provenance_filter))
            row = conn.execute(sql, params).fetchone()
            if row is None:
                raise CodeGraphError("symbol not found in requested scope")
            limit = min(max(1, int(edge_limit)), 200)
            edge_sql = "SELECT * FROM edges WHERE scope_id=? AND active=1 AND (from_id=? OR to_id=?)"
            edge_params: list[Any] = [scope_id, str(symbol_id), str(symbol_id)]
            if provenance_filter:
                edge_sql += " AND provenance=?"; edge_params.append(provenance_filter)
            edge_sql += " ORDER BY relation,from_id,to_id,context,source_location,edge_id LIMIT ?"; edge_params.append(limit)
            edges = conn.execute(edge_sql, edge_params).fetchall()
        return {
            "symbol": {
                "symbol_id": str(row["symbol_id"]), "name": str(row["name"]), "kind": str(row["kind"]),
                "signature": str(row["signature"]), "line_start": int(row["line_start"]), "line_end": int(row["line_end"]),
                "provenance": str(row["provenance"]), "source_map": _load_metadata_json(row["source_map_json"], label="symbol source map"),
                "metadata": _load_metadata_json(row["metadata_json"], label="symbol metadata"),
            },
            "file": {"path": str(row["path"]), "language": str(row["language"]), "source_role": str(row["source_role"]), "provenance": str(row["file_provenance"])},
            "edges": [
                {"edge_id": str(edge["edge_id"]), "from_id": str(edge["from_id"]), "to_id": str(edge["to_id"]), "relation": str(edge["relation"]), "context": str(edge["context"]), "provenance": str(edge["provenance"]), "source_location": str(edge["source_location"]), "metadata": _load_metadata_json(edge["metadata_json"], label="edge metadata")}
                for edge in edges
            ],
        }

    explain = explain_symbol

    def _append_outbox_conn(self, conn: sqlite3.Connection, scope: CodeGraphScope, event_type: str, aggregate_id: str, payload_hash: str, now: str) -> str:
        scope_id = self._scope_id(scope)
        event_id = stable_id("codegraph-event", scope_id, event_type, aggregate_id, payload_hash)
        row = conn.execute("SELECT event_id FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is not None:
            return event_id
        sequence = int(conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM outbox").fetchone()[0])
        conn.execute("INSERT INTO outbox(event_id,scope_id,sequence,event_type,aggregate_id,payload_hash,status,attempts,error,created_at,projected_at) VALUES(?,?,?,?,?,?, 'pending',0,'',?, '')", (event_id, scope_id, sequence, str(event_type), str(aggregate_id), str(payload_hash), now))
        return event_id

    def append_outbox(self, event_type: str, aggregate_id: str, *, payload_hash: str = "", scope: CodeGraphScope | Mapping[str, Any] | None = None) -> OutboxEvent:
        checked_scope = self._scope(scope, write=True)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                event_id = self._append_outbox_conn(conn, checked_scope, event_type, aggregate_id, payload_hash, now)
                row = conn.execute("SELECT * FROM outbox WHERE event_id=?", (event_id,)).fetchone()
                assert row is not None
                return self._row_outbox(row, checked_scope)

    enqueue_outbox = append_outbox

    def _row_outbox(self, row: sqlite3.Row, scope: CodeGraphScope | None = None) -> OutboxEvent:
        if scope is None:
            scope = CodeGraphScope(workspace_id=str(row["workspace_id"]) if "workspace_id" in row.keys() else self.workspace_id)
        return OutboxEvent(event_id=str(row["event_id"]), scope=scope, event_type=str(row["event_type"]), aggregate_id=str(row["aggregate_id"]), payload_hash=str(row["payload_hash"]), sequence=int(row["sequence"]), status=str(row["status"]), attempts=int(row["attempts"]), error=str(row["error"]), created_at=str(row["created_at"]), projected_at=str(row["projected_at"]))

    def pending_outbox(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, limit: int | None = None, include_failed: bool = False) -> tuple[OutboxEvent, ...]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        statuses = "('pending','failed')" if include_failed else "('pending')"
        sql = f"SELECT * FROM outbox WHERE scope_id=? AND status IN {statuses} ORDER BY sequence"
        params: list[Any] = [scope_id]
        if limit is not None:
            sql += " LIMIT ?"; params.append(max(0, int(limit)))
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._row_outbox(row, checked_scope) for row in rows)

    list_pending_outbox = pending_outbox

    def drain_outbox(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None, projector: Any | None = None, limit: int | None = None) -> dict[str, int]:
        checked_scope = self._scope(scope, write=True)
        events = self.pending_outbox(scope=checked_scope, limit=limit, include_failed=True)
        projected: list[str] = []
        failed: list[tuple[str, str]] = []
        for event in events:
            try:
                if projector is not None:
                    callback = getattr(projector, "project", projector)
                    callback(event)
                projected.append(event.event_id)
            except Exception as exc:
                failed.append((event.event_id, f"{type(exc).__name__}:{exc}"))
        # Keep the durable state transition in one short transaction.  The
        # prior per-event connection loop could churn WAL sidecars on Windows
        # and leave a partially projected queue when a later write failed.
        # If this transaction cannot commit, no event is marked failed merely
        # because the storage layer was temporarily unavailable.
        if projected or failed:
            now = _now()
            with open_database(self.db_path) as conn:
                with transaction(conn):
                    for event_id in projected:
                        conn.execute(
                            "UPDATE outbox SET status='projected',attempts=attempts+1,projected_at=?,error='' "
                            "WHERE event_id=? AND status IN ('pending','failed')",
                            (now, event_id),
                        )
                    for event_id, error in failed:
                        conn.execute(
                            "UPDATE outbox SET status='failed',attempts=attempts+1,error=? "
                            "WHERE event_id=? AND status IN ('pending','failed')",
                            (error, event_id),
                        )
        remaining = self.pending_outbox(scope=checked_scope, include_failed=True)
        return {"projected": len(projected), "failed": len(failed), "pending": len(remaining)}

    project_outbox = drain_outbox

    def get_checkpoint(self, domain: str = "codegraph", *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> Checkpoint:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM checkpoints WHERE scope_id=? AND domain=?", (scope_id, str(domain))).fetchone()
        if row is None:
            return Checkpoint(stable_id("checkpoint", scope_id, domain), checked_scope, str(domain), 0, "", "")
        return Checkpoint(str(row["checkpoint_id"]), checked_scope, str(row["domain"]), int(row["sequence"]), str(row["digest"]), str(row["updated_at"]))

    checkpoint = get_checkpoint

    def save_checkpoint(self, domain: str, sequence: int, *, digest: str = "", scope: CodeGraphScope | Mapping[str, Any] | None = None) -> Checkpoint:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        sequence = max(0, int(sequence)); now = _now()
        checkpoint_id = stable_id("checkpoint", scope_id, domain)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                current = conn.execute("SELECT sequence,digest FROM checkpoints WHERE scope_id=? AND domain=?", (scope_id, str(domain))).fetchone()
                if current is not None and int(current[0]) == sequence and str(current[1]) != str(digest):
                    raise CodeGraphError("checkpoint digest is immutable at a sequence")
                conn.execute("INSERT INTO checkpoints(checkpoint_id,scope_id,domain,sequence,digest,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(scope_id,domain) DO UPDATE SET sequence=CASE WHEN excluded.sequence>checkpoints.sequence THEN excluded.sequence ELSE checkpoints.sequence END,digest=CASE WHEN excluded.sequence>=checkpoints.sequence THEN excluded.digest ELSE checkpoints.digest END,updated_at=CASE WHEN excluded.sequence>=checkpoints.sequence THEN excluded.updated_at ELSE checkpoints.updated_at END", (checkpoint_id, scope_id, str(domain), sequence, str(digest), now))
                row = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
                assert row is not None
                return Checkpoint(checkpoint_id, checked_scope, str(row["domain"]), int(row["sequence"]), str(row["digest"]), str(row["updated_at"]))

    update_checkpoint = save_checkpoint

    def _fingerprint_id(self, scope_id: str, path: str) -> str:
        return stable_id("fingerprint", scope_id, path)

    def get_fingerprint(self, path: str, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        normalized = self._check_source_path(path)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT path,mtime_ns,size,content_hash FROM source_fingerprints WHERE scope_id=? AND path=?",
                (scope_id, normalized),
            ).fetchone()
        if row is None:
            return None
        return {
            "path": str(row["path"]),
            "mtime_ns": int(row["mtime_ns"]),
            "size": int(row["size"]),
            "content_hash": str(row["content_hash"]),
        }

    def _upsert_fingerprint_conn(
        self,
        conn: sqlite3.Connection,
        checked_scope: CodeGraphScope,
        *,
        path: str,
        mtime_ns: int,
        size: int,
        content_hash: str,
        now: str | None = None,
    ) -> None:
        self._ensure_refresh_schema_conn(conn)
        scope_id = self._scope_id(checked_scope)
        normalized = self._check_source_path(path)
        fingerprint_id = self._fingerprint_id(scope_id, normalized)
        conn.execute(
            "INSERT INTO source_fingerprints(fingerprint_id,scope_id,path,mtime_ns,size,content_hash,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_id,path) DO UPDATE SET "
            "mtime_ns=excluded.mtime_ns,size=excluded.size,content_hash=excluded.content_hash,updated_at=excluded.updated_at",
            (fingerprint_id, scope_id, normalized, int(mtime_ns), int(size), str(content_hash), str(now or _now())),
        )

    def _delete_fingerprint_conn(
        self,
        conn: sqlite3.Connection,
        checked_scope: CodeGraphScope,
        path: str,
    ) -> None:
        self._ensure_refresh_schema_conn(conn)
        scope_id = self._scope_id(checked_scope)
        normalized = self._check_source_path(path)
        conn.execute(
            "DELETE FROM source_fingerprints WHERE scope_id=? AND path=?",
            (scope_id, normalized),
        )

    def enqueue_refresh_paths(
        self,
        paths: Sequence[str],
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        now = _now()
        queued: list[str] = []
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_scope(conn, checked_scope)
                self._ensure_refresh_schema_conn(conn)
                for raw in paths:
                    normalized = self._check_source_path(raw)
                    queue_id = stable_id("refresh-queue", scope_id, normalized)
                    conn.execute(
                        "INSERT INTO refresh_queue(queue_id,scope_id,path,queued_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(scope_id,path) DO UPDATE SET queued_at=excluded.queued_at",
                        (queue_id, scope_id, normalized, now),
                    )
                    queued.append(normalized)
                rows = conn.execute(
                    "SELECT path FROM refresh_queue WHERE scope_id=? ORDER BY path",
                    (scope_id,),
                ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def drain_refresh_queue(
        self,
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_refresh_schema_conn(conn)
                rows = conn.execute(
                    "SELECT path FROM refresh_queue WHERE scope_id=? ORDER BY path",
                    (scope_id,),
                ).fetchall()
                conn.execute("DELETE FROM refresh_queue WHERE scope_id=?", (scope_id,))
        return tuple(str(row[0]) for row in rows)

    def _put_affected_receipt_conn(
        self,
        conn: sqlite3.Connection,
        checked_scope: CodeGraphScope,
        *,
        start_ids: Sequence[str],
        result_ids: Sequence[str],
        depth: int,
        limit: int,
        provenance: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_refresh_schema_conn(conn)
        scope_id = self._scope_id(checked_scope)
        starts = tuple(dict.fromkeys(
            str(item).strip() for item in start_ids if str(item).strip()
        ))[:_MAX_AFFECTED_RECEIPT_STARTS]
        bounded_depth = min(max(0, int(depth)), _MAX_AFFECTED_RECEIPT_DEPTH)
        bounded_limit = min(
            max(1, int(limit)),
            _MAX_AFFECTED_RECEIPT_RESULTS,
        )
        results = tuple(dict.fromkeys(
            str(item).strip() for item in result_ids if str(item).strip()
        ))[:bounded_limit]
        provenance_filter = normalize_provenance(provenance or "production")
        digest = stable_digest((starts, results, bounded_depth, bounded_limit, provenance_filter))
        receipt_id = stable_id("affected-receipt", scope_id, digest, str(now or _now()))
        payload = {
            "receipt_id": receipt_id,
            "scope_id": scope_id,
            "start_ids": list(starts),
            "result_ids": list(results),
            "depth": bounded_depth,
            "limit": bounded_limit,
            "provenance": provenance_filter,
            "digest": digest,
        }
        conn.execute("DELETE FROM affected_receipts WHERE scope_id=? AND consumed=0", (scope_id,))
        conn.execute(
            "INSERT INTO affected_receipts(receipt_id,scope_id,start_ids_json,result_json,digest,depth,result_limit,provenance_filter,consumed,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            (
                receipt_id,
                scope_id,
                _json(list(starts)),
                _json(list(results)),
                digest,
                bounded_depth,
                bounded_limit,
                provenance_filter,
                str(now or _now()),
            ),
        )
        return payload

    def _affected_receipt_row(
        self,
        row: sqlite3.Row,
        *,
        scope_id: str,
        consumed: bool,
    ) -> dict[str, Any]:
        return {
            "receipt_id": str(row["receipt_id"]),
            "scope_id": scope_id,
            "start_ids": json.loads(str(row["start_ids_json"] or "[]")),
            "result_ids": json.loads(str(row["result_json"] or "[]")),
            "depth": int(row["depth"]),
            "limit": int(row["result_limit"]),
            "provenance": str(row["provenance_filter"]),
            "digest": str(row["digest"]),
            "consumed": bool(consumed),
        }

    def peek_affected_receipt(
        self,
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Read one pending affected receipt without changing its state."""
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='affected_receipts' LIMIT 1",
            ).fetchone()
            if table is None:
                return None
            row = conn.execute(
                "SELECT receipt_id,start_ids_json,result_json,digest,depth,result_limit,provenance_filter "
                "FROM affected_receipts WHERE scope_id=? AND consumed=0 "
                "ORDER BY created_at,receipt_id LIMIT 1",
                (scope_id,),
            ).fetchone()
        if row is None:
            return None
        return self._affected_receipt_row(row, scope_id=scope_id, consumed=False)

    def consume_affected_receipt(
        self,
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        receipt_id: str = "",
    ) -> dict[str, Any] | None:
        checked_scope = self._scope(scope, write=True)
        scope_id = self._scope_id(checked_scope)
        now = _now()
        with open_database(self.db_path) as conn:
            with transaction(conn):
                self._ensure_refresh_schema_conn(conn)
                clauses = ["scope_id=?", "consumed=0"]
                params: list[Any] = [scope_id]
                expected = str(receipt_id or "").strip()
                if expected:
                    clauses.append("receipt_id=?")
                    params.append(expected)
                row = conn.execute(
                    "SELECT receipt_id,start_ids_json,result_json,digest,depth,result_limit,provenance_filter "
                    "FROM affected_receipts WHERE " + " AND ".join(clauses) +
                    " ORDER BY created_at,receipt_id LIMIT 1",
                    params,
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE affected_receipts SET consumed=1,consumed_at=? WHERE receipt_id=? AND consumed=0",
                    (now, str(row["receipt_id"])),
                )
                changed = conn.execute("SELECT changes()").fetchone()
                if changed is None or int(changed[0]) != 1:
                    return None
        return self._affected_receipt_row(row, scope_id=scope_id, consumed=True)

    def related_source_paths(
        self,
        paths: Sequence[str],
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        limit: int = 32,
    ) -> tuple[str, ...]:
        """Return direct active graph neighbours for bounded re-export.

        Re-exporting an unchanged neighbour restores edges that originate in it
        after a changed file's old symbols are replaced.  This is deliberately
        one hop and bounded: never a hidden full build.
        """

        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        normalized = tuple(dict.fromkeys(
            self._check_source_path(path) for path in paths if str(path).strip()
        ))
        if not normalized:
            return ()
        bounded_limit = min(max(1, int(limit)), 128)
        placeholders = ",".join("?" for _ in normalized)
        with self.connection() as conn:
            changed_rows = conn.execute(
                "SELECT file_id FROM source_files WHERE scope_id=? AND active=1 AND path IN (" + placeholders + ")",
                (scope_id, *normalized),
            ).fetchall()
            changed_ids = tuple(str(row[0]) for row in changed_rows)
            if not changed_ids:
                return ()
            changed_placeholders = ",".join("?" for _ in changed_ids)
            rows = conn.execute(
                "SELECT DISTINCT CASE "
                "WHEN source_symbols.file_id IN (" + changed_placeholders + ") THEN target_symbols.file_id "
                "ELSE source_symbols.file_id END AS file_id "
                "FROM edges "
                "JOIN symbols AS source_symbols ON source_symbols.symbol_id=edges.from_id AND source_symbols.scope_id=edges.scope_id "
                "JOIN symbols AS target_symbols ON target_symbols.symbol_id=edges.to_id AND target_symbols.scope_id=edges.scope_id "
                "WHERE edges.scope_id=? AND edges.active=1 AND source_symbols.active=1 AND target_symbols.active=1 "
                "AND (source_symbols.file_id IN (" + changed_placeholders + ") "
                "OR target_symbols.file_id IN (" + changed_placeholders + ")) "
                "ORDER BY file_id LIMIT ?",
                (*changed_ids, scope_id, *changed_ids, *changed_ids, bounded_limit),
            ).fetchall()
            neighbour_ids = tuple(str(row[0]) for row in rows if str(row[0]) not in changed_ids)
            if not neighbour_ids:
                return ()
            neighbours = ",".join("?" for _ in neighbour_ids)
            path_rows = conn.execute(
                "SELECT path FROM source_files WHERE scope_id=? AND active=1 AND file_id IN (" + neighbours + ") ORDER BY path",
                (scope_id, *neighbour_ids),
            ).fetchall()
        return tuple(str(row[0]) for row in path_rows[:bounded_limit])

    def record_migration_map(self, source_db: str, source_table: str, source_pk: str, source_hash: str, *, target_id: str = "", target_type: str = "", status: str = "mapped") -> str:
        now = _now(); source_db = str(source_db); source_table = str(source_table); source_pk = str(source_pk); source_hash = str(source_hash)
        map_id = stable_id("codegraph-map", source_db, source_table, source_pk, target_type)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                row = conn.execute("SELECT source_hash,target_id,status FROM migration_map WHERE source_db=? AND source_table=? AND source_pk=? AND target_type=?", (source_db, source_table, source_pk, target_type)).fetchone()
                if row is not None:
                    if str(row[0]) != source_hash:
                        raise CodeGraphError("migration map source hash changed")
                    return map_id
                conn.execute("INSERT INTO migration_map(map_id,source_db,source_table,source_pk,source_hash,target_id,target_type,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (map_id, source_db, source_table, source_pk, source_hash, str(target_id), str(target_type), str(status), now, now))
        return map_id

    put_migration_map = record_migration_map

    def record_unknown(self, source_ref: str, code: str, detail: str = "", *, source_hash: str = "", status: str = "BLOCKED") -> str:
        now = _now(); ledger_id = stable_id("unknown", source_ref, code, detail)
        with open_database(self.db_path) as conn:
            with transaction(conn):
                conn.execute("INSERT INTO unknown_ledger(ledger_id,source_ref,code,detail,status,source_hash,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_ref,code,detail) DO NOTHING", (ledger_id, str(source_ref), str(code), str(detail), str(status), str(source_hash), now))
        return ledger_id

    record_unknown_ledger = record_unknown

    def list_unknown(self) -> tuple[UnknownLedgerEntry, ...]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM unknown_ledger ORDER BY source_ref,code,detail").fetchall()
        return tuple(UnknownLedgerEntry(str(row["ledger_id"]), str(row["source_ref"]), str(row["code"]), str(row["detail"]), str(row["status"]), str(row["source_hash"]), str(row["created_at"])) for row in rows)

    list_unknown_ledger = list_unknown

    def list_migration_map(self) -> tuple[dict[str, Any], ...]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM migration_map ORDER BY source_db,source_table,source_pk,target_type").fetchall()
        return tuple(dict(row) for row in rows)

    def counts(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> dict[str, int]:
        checked_scope = self._scope(scope)
        scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            counts = {
                "scopes": int(conn.execute("SELECT COUNT(*) FROM graph_scopes WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "source_files": int(conn.execute("SELECT COUNT(*) FROM source_files WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "active_source_files": int(conn.execute("SELECT COUNT(*) FROM source_files WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                "revisions": int(conn.execute("SELECT COUNT(*) FROM revisions WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "symbols": int(conn.execute("SELECT COUNT(*) FROM symbols WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                "edges": int(conn.execute("SELECT COUNT(*) FROM edges WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                "affected_queries": int(conn.execute("SELECT COUNT(*) FROM affected_queries WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "checkpoints": int(conn.execute("SELECT COUNT(*) FROM checkpoints WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "outbox": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=?", (scope_id,)).fetchone()[0]),
                "outbox_pending": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=? AND status='pending'", (scope_id,)).fetchone()[0]),
                "outbox_failed": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=? AND status='failed'", (scope_id,)).fetchone()[0]),
                "migration_map": int(conn.execute("SELECT COUNT(*) FROM migration_map").fetchone()[0]),
                "unknown_ledger": int(conn.execute("SELECT COUNT(*) FROM unknown_ledger").fetchone()[0]),
                "unknown_blocked": int(conn.execute("SELECT COUNT(*) FROM unknown_ledger WHERE status='BLOCKED'").fetchone()[0]),
                "tombstones": int(conn.execute("SELECT COUNT(*) FROM source_tombstones WHERE scope_id=?", (scope_id,)).fetchone()[0]),
            }
        return counts

    table_counts = counts

    def nearest_scopes(
        self,
        *,
        project_ref: str,
        share_group_id: str,
        agent_instance_id: str = "",
        provider: str = "",
        runtime_role: str = "",
        limit: int = 4,
    ) -> tuple[CodeGraphScope, ...]:
        """Resolve trusted graph scopes nearest to current project.

        CodeGraph builds made by the GUI are group-level Graphify scopes
        (empty agent/provider-independent identity), while MCP callers carry
        a trusted Agent/provider context.  Exact tuple lookup therefore misses
        an indexed child repository or a group-level build.  This read-only
        resolver keeps the group/workspace/trusted boundary fixed, admits only
        the current Agent or group-level Graphify scopes, then ranks exact and
        parent/child project matches deterministically.
        """

        current = canonical_project_ref(project_ref)
        group = str(share_group_id or "").strip()
        agent = str(agent_instance_id or "").strip()
        wanted_provider = str(provider or "").strip().casefold()
        wanted_runtime = str(runtime_role or "").strip().casefold()
        if not current or not group:
            return ()

        def distance(candidate: str) -> int | None:
            candidate = canonical_project_ref(candidate)
            if not candidate:
                return None
            if candidate == current:
                return 0
            left = candidate.rstrip("/")
            right = current.rstrip("/")
            if right.startswith(left + "/"):
                return len(right) - len(left)
            if left.startswith(right + "/"):
                return len(left) - len(right)
            return None

        ranked: list[tuple[tuple[Any, ...], CodeGraphScope]] = []
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT scope_id,workspace_id,agent_instance_id,project_ref,provider,"
                "share_group_id,runtime_role,trusted_context "
                "FROM graph_scopes WHERE LOWER(REPLACE(workspace_id,char(92),'/'))="
                "LOWER(REPLACE(?,char(92),'/')) AND share_group_id=? "
                "AND trusted_context=1 ORDER BY project_ref,scope_id",
                (self.workspace_id, group),
            ).fetchall()
            for row in rows:
                candidate_agent = str(row[2] or "")
                candidate_project = canonical_project_ref(row[3])
                candidate_provider = str(row[4] or "").casefold()
                candidate_runtime = str(row[6] or "").casefold()
                gap = distance(candidate_project)
                if gap is None:
                    continue
                group_graphify = (
                    not candidate_agent
                    and candidate_provider == "graphify"
                    and not candidate_runtime
                )
                exact_agent = (
                    bool(agent)
                    and candidate_agent == agent
                    and (not wanted_provider or candidate_provider == wanted_provider)
                    and (not wanted_runtime or candidate_runtime == wanted_runtime)
                )
                if not (group_graphify or exact_agent):
                    continue
                scope_id = str(row[0])
                data_count = int(conn.execute(
                    "SELECT COUNT(*) FROM source_files WHERE scope_id=? AND active=1",
                    (scope_id,),
                ).fetchone()[0])
                data_count += int(conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE scope_id=? AND active=1",
                    (scope_id,),
                ).fetchone()[0])
                try:
                    scope = CodeGraphScope(
                        # Store-owned workspace is canonical even when legacy
                        # rows used different drive-case or slash spelling.
                        workspace_id=self.workspace_id,
                        agent_instance_id=candidate_agent,
                        project_ref=candidate_project,
                        provider=candidate_provider,
                        share_group_id=str(row[5] or ""),
                        runtime_role=candidate_runtime,
                        trusted_context=True,
                    )
                except (TypeError, ValueError):
                    continue
                # Prefer populated exact scopes, then populated group-level
                # scopes, then the nearest empty scope.  This avoids an empty
                # compatibility row masking a real indexed child repository.
                rank = (
                    0 if data_count else 1,
                    0 if exact_agent else 1,
                    gap,
                    -len(candidate_project),
                    candidate_project,
                    scope_id,
                )
                ranked.append((rank, scope))
        ranked.sort(key=lambda item: item[0])
        return tuple(scope for _rank, scope in ranked[: max(1, min(int(limit), 8))])

    def orphan_count(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> int:
        checked_scope = self._scope(scope); scope_id = self._scope_id(checked_scope)
        with self.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM edges e LEFT JOIN revisions r ON r.revision_id=e.revision_id AND r.scope_id=e.scope_id LEFT JOIN symbols s1 ON s1.symbol_id=e.from_id AND s1.scope_id=e.scope_id AND s1.active=1 LEFT JOIN symbols s2 ON s2.symbol_id=e.to_id AND s2.scope_id=e.scope_id AND s2.active=1 WHERE e.scope_id=? AND e.active=1 AND (r.revision_id IS NULL OR s1.symbol_id IS NULL OR s2.symbol_id IS NULL)", (scope_id,)).fetchone()[0])

    def integrity_check(self) -> list[str]:
        with self.connection() as conn:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]

    def foreign_key_check(self) -> list[tuple[Any, ...]]:
        with self.connection() as conn:
            return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]

    def status(self, *, scope: CodeGraphScope | Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"db_path": str(self.db_path), "schema_marker": CODEGRAPH_SCHEMA_MARKER, "counts": self.counts(scope=scope), "integrity": self.integrity_check(), "foreign_keys": self.foreign_key_check(), "orphan": self.orphan_count(scope=scope)}

    def reference_metadata(
        self,
        *,
        project_ref: str,
        share_group_id: str,
        provider: str = "graphify",
        agent_instance_id: str = "",
        runtime_role: str = "",
        limit: int = 1,
    ) -> tuple[dict[str, Any], ...]:
        """Read nearest trusted repository metadata for bootstrap.

        This is intentionally not a general graph query.  It filters by the
        trusted workspace/group/provider tuple first, then selects a graph
        scope whose canonical project path is the nearest ancestor/descendant
        of the current project.  Only aggregate counts and bounded symbol/file
        metadata cross the context boundary; source bodies and edges do not.
        """

        current = canonical_project_ref(project_ref)
        group = str(share_group_id or "").strip()
        wanted_provider = str(provider or "graphify").strip().casefold()
        agent = str(agent_instance_id or "").strip()
        wanted_runtime = str(runtime_role or "").strip().casefold()
        if not current or not group or not wanted_provider:
            return ()

        def distance(candidate: str) -> int | None:
            if not candidate:
                return None
            if candidate == current:
                return 0
            left = candidate.rstrip("/")
            right = current.rstrip("/")
            if right.startswith(left + "/"):
                return len(right) - len(left)
            if left.startswith(right + "/"):
                return len(left) - len(right)
            return None

        selected: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        with self.connection() as conn:
            clauses = [
                "LOWER(REPLACE(workspace_id,char(92),'/'))=LOWER(REPLACE(?,char(92),'/'))",
                "share_group_id=?",
                "trusted_context=1",
                "(LOWER(provider)=? OR (agent_instance_id='' "
                "AND LOWER(provider)='graphify' AND runtime_role=''))",
            ]
            params: list[Any] = [self.workspace_id, group, wanted_provider]
            if agent:
                clauses.append(
                    "(agent_instance_id=? OR (agent_instance_id='' "
                    "AND LOWER(provider)='graphify' AND runtime_role=''))"
                )
                params.append(agent)
            if wanted_runtime:
                clauses.append("(LOWER(runtime_role)=? OR runtime_role='')")
                params.append(wanted_runtime)
            rows = conn.execute(
                "SELECT scope_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,runtime_role,trusted_context "
                "FROM graph_scopes WHERE " + " AND ".join(clauses) + " ORDER BY project_ref,scope_id",
                params,
            ).fetchall()
            for row in rows:
                candidate_project = canonical_project_ref(row["project_ref"])
                gap = distance(candidate_project)
                if gap is None:
                    continue
                scope_id = str(row["scope_id"])
                counts = {
                    "source_files": int(conn.execute("SELECT COUNT(*) FROM source_files WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                    "symbols": int(conn.execute("SELECT COUNT(*) FROM symbols WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                    "edges": int(conn.execute("SELECT COUNT(*) FROM edges WHERE scope_id=? AND active=1", (scope_id,)).fetchone()[0]),
                    "outbox_pending": int(conn.execute("SELECT COUNT(*) FROM outbox WHERE scope_id=? AND status='pending'", (scope_id,)).fetchone()[0]),
                }
                file_rows = conn.execute(
                    "SELECT file_id,path,content_hash,source_revision,language FROM source_files "
                    "WHERE scope_id=? AND active=1 ORDER BY path,file_id LIMIT 8",
                    (scope_id,),
                ).fetchall()
                files: list[dict[str, Any]] = []
                for file_row in file_rows:
                    file_id = str(file_row["file_id"])
                    symbol_rows = conn.execute(
                        "SELECT name,kind FROM symbols WHERE scope_id=? AND file_id=? "
                        "AND active=1 ORDER BY name,kind LIMIT 12",
                        (scope_id, file_id),
                    ).fetchall()
                    files.append({
                        "path": normalize_relative_path(str(file_row["path"])),
                        "hash": str(file_row["content_hash"] or ""),
                        "revision": str(file_row["source_revision"] or ""),
                        "language": str(file_row["language"] or ""),
                        "symbols": [
                            {"name": str(symbol["name"]), "kind": str(symbol["kind"])}
                            for symbol in symbol_rows
                        ],
                    })
                checkpoint = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0),COALESCE(MAX(digest),'') FROM checkpoints WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()
                item = {
                    "scope_id": scope_id,
                    "project_ref": candidate_project,
                    "provider": str(row["provider"] or wanted_provider).casefold(),
                    "share_group_id": group,
                    "runtime_role": str(row["runtime_role"] or ""),
                    "counts": counts,
                    "files": files,
                    "checkpoint_sequence": int(checkpoint[0] or 0),
                    "checkpoint_digest": str(checkpoint[1] or ""),
                }
                selected.append(((gap, -len(candidate_project), candidate_project, scope_id), item))
        selected.sort(key=lambda item: item[0])
        return tuple(item for _key, item in selected[: max(1, min(int(limit), 4))])


CodeGraphStorage = CodeGraphStore
PersistentCodeGraph = CodeGraphStore
SCHEMA_VERSION = CODEGRAPH_SCHEMA_VERSION
SCHEMA_MARKER = CODEGRAPH_SCHEMA_MARKER


__all__ = [
    "CODEGRAPH_AUX_SCHEMA",
    "CodeGraphStorage",
    "CodeGraphStore",
    "PersistentCodeGraph",
    "SCHEMA_MARKER",
    "SCHEMA_VERSION",
    "normalize_relative_path",
]
