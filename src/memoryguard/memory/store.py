"""V2 MemoryAtom storage.

The memory domain is the durable fact plane.  It owns atom revisions, small
per-atom deltas, scope/ACL records, source mappings and an evidence projection
outbox.  Evidence itself is never written here; the outbox is the boundary
between the two SQLite domains.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import base64
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence

from ..storage.database import connect_database, open_database_snapshot
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import SCHEMA_MARKER as BASE_SCHEMA_MARKER
from ..storage.transaction import transaction
from ..rule_scope import canonical_project_ref


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_JSON_TYPE_KEY = "__memoryguard_type__"


def _json_safe(value: Any) -> Any:
    """Convert scalar values to deterministic JSON without dropping bytes.

    Legacy SQLite adapters can return BLOB columns as ``bytes``.  Persisting a
    repr (``b'...'``) loses the original type and direct ``json.dumps`` raises
    ``TypeError``.  Encode byte-like values with an explicit, reversible marker;
    callers can decode ``base64`` with the standard base64 codec when
    recovery is required.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            _JSON_TYPE_KEY: "bytes",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if isinstance(key, str):
                text = key
            elif isinstance(key, (bytes, bytearray, memoryview)):
                text = json.dumps(_json_safe(key), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            else:
                text = str(key)
            result[text] = _json_safe(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, set):
        items = [_json_safe(child) for child in value]
        return {_JSON_TYPE_KEY: "set", "items": sorted(items, key=lambda item: repr(item))}
    return value


def _json(value: Any) -> str:
    return json.dumps(_json_safe(value if value is not None else {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(value: Any) -> str:
    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_for(value: str | Path | WorkspaceV2Layout, domain: str) -> Path:
    if isinstance(value, WorkspaceV2Layout):
        layout = value
        candidate = layout.memory_db
    else:
        raw = Path(value).expanduser()
        if raw.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            candidate = Path(os.path.abspath(os.fspath(raw)))
            if candidate.parent.name != domain or candidate.parent.parent.name != WorkspaceV2Layout.ROOT_NAME:
                raise ValueError(f"{domain} database must be inside .memoryguard/{domain}")
            layout = _safe_layout(candidate.parent.parent.parent)
        else:
            layout = _safe_layout(raw)
            candidate = layout.memory_db
    # Resolve containment and reparse/symlink checks before every database
    # object is accepted.  Keep lexical candidate path so a symlink cannot be
    # silently followed before Layout performs its lstat checks.
    return layout.assert_database_path(candidate, domain)


def _safe_layout(root: str | Path) -> WorkspaceV2Layout:
    candidate = Path(os.path.abspath(os.fspath(root)))
    if candidate.exists() and WorkspaceV2Layout._is_reparse_or_symlink(candidate):
        raise ValueError(f"workspace cannot be a symlink or reparse point: {candidate}")
    return WorkspaceV2Layout(candidate)


def _validate_metadata_value(value: Any, *, label: str = "metadata", depth: int = 0) -> Any:
    """Validate one metadata/provenance JSON value recursively."""

    if depth > 8:
        raise ValueError(f"{label} exceeds maximum nesting depth")
    forbidden = _FORBIDDEN_METADATA_KEYS
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            text = str(key)
            if text.casefold() in forbidden:
                raise ValueError(f"{label} cannot contain source body field: {text}")
            result[text] = _validate_metadata_value(child, label=label, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_validate_metadata_value(child, label=label, depth=depth + 1) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(f"{label} contains non-JSON metadata value")


def _validate_metadata(value: Mapping[str, Any] | None, *, label: str = "metadata") -> dict[str, Any]:
    """Validate bounded metadata recursively; never carry source body text."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    result = _validate_metadata_value(value, label=label)
    assert isinstance(result, dict)
    encoded = _json(result)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError(f"{label} exceeds 64 KiB")
    return result


def _validate_metadata_tree(value: Any, *, label: str) -> Any:
    """Validate a metadata tree (including a provenance list) as one value."""

    result = _validate_metadata_value(value, label=label)
    encoded = _json(result)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError(f"{label} exceeds 64 KiB")
    return result


_FORBIDDEN_METADATA_KEYS = frozenset({
    "body", "raw", "raw_content", "content", "text", "full_text",
    "document", "document_body", "document_text", "conversation",
    "conversation_body", "full_transcript", "raw_transcript", "transcript",
    "raw_text", "source_text", "source_body", "original_content", "payload",
})


def _evidence_payload_authority(payload: Mapping[str, Any]) -> str:
    nested = payload.get("evidence")
    if isinstance(nested, Mapping):
        return str(nested.get("authority") or payload.get("authority") or "observed")
    return str(payload.get("authority") or "observed")


def _validate_evidence_authorities(payloads: Sequence[Mapping[str, Any]]) -> None:
    """Reuse the evidence-domain allowlist before any memory/outbox write."""

    from ..evidence.store import validate_authority

    for payload in payloads:
        validate_authority(_evidence_payload_authority(payload))


@dataclass(frozen=True)
class MemoryReadScope:
    """Explicit visibility scope required by public memory read methods."""

    share_group_id: str
    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    admin: bool = False

    def __post_init__(self) -> None:
        if not str(self.share_group_id):
            raise ValueError("read scope requires share_group_id")
        if not str(self.workspace_id):
            raise ValueError("read scope requires workspace_id")
        object.__setattr__(self, "project_ref", canonical_project_ref(self.project_ref))
        object.__setattr__(self, "provider", str(self.provider or "").strip().casefold())
        object.__setattr__(self, "runtime_role", str(self.runtime_role or "").strip().casefold())

    @classmethod
    def from_value(cls, value: "MemoryReadScope | Mapping[str, Any]") -> "MemoryReadScope":
        if isinstance(value, cls):
            return value
        return cls(
            share_group_id=str(value.get("share_group_id") or value.get("group_id") or ""),
            workspace_id=str(value.get("workspace_id") or ""),
            agent_instance_id=str(value.get("agent_instance_id") or ""),
            project_ref=str(value.get("project_ref") or ""),
            provider=str(value.get("provider") or ""),
            runtime_role=str(value.get("runtime_role") or ""),
            admin=bool(value.get("admin", value.get("is_admin", False))),
        )


@dataclass(frozen=True)
class MemoryMutationScope(MemoryReadScope):
    """Explicit scope required for destructive memory mutations."""


# Private identity token used only by the read-only V1 migration adapter.  It
# is deliberately not accepted as a public ``scope`` value, so callers cannot
# turn an unscoped delete/supersede into a mutation by guessing a string.
_MIGRATION_CAPABILITY = object()

# Short alias used by callers that refer to the capability generically.
MutationScope = MemoryMutationScope


@dataclass
class MemoryAtom:
    """Canonical V2 memory atom.

    ``memory_id`` is retained from V1.  ``scope`` (especially
    ``share_group_id``) is part of identity; the same memory_id in two groups
    therefore always produces two atom IDs.
    """

    memory_id: str
    body: str
    kind: str = "fact"
    status: str = "active"
    confidence: float = 0.5
    locked: bool = False
    injection_policy: str = "relevant"
    priority: int = 0
    canonical_hash: str = ""
    dedup_domain: str = "relevant"
    supersedes: list[str] = field(default_factory=list)
    provenance: list[Mapping[str, Any]] = field(default_factory=list)
    agent_instance_id: str = ""
    share_group_id: str = ""
    project_ref: str = ""
    provider: str = ""
    runtime_role: str = ""
    workspace_id: str = ""
    atom_id: str = ""
    revision: int = 1
    visibility: str = "building"
    created_at: str = ""
    updated_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.memory_id = str(self.memory_id)
        self.body = str(self.body)
        self.kind = str(self.kind or "fact")
        self.status = str(self.status or "active")
        self.confidence = float(self.confidence)
        self.priority = int(self.priority)
        self.project_ref = canonical_project_ref(self.project_ref)
        self.provider = str(self.provider or "").strip().casefold()
        self.runtime_role = str(self.runtime_role or "").strip().casefold()
        self.revision = max(1, int(self.revision))
        self.supersedes = [str(item) for item in self.supersedes]
        raw_provenance = list(self.provenance or [])
        if any(not isinstance(item, Mapping) for item in raw_provenance):
            raise ValueError("atom provenance entries must be JSON objects")
        self.provenance = [dict(item) for item in raw_provenance]
        if self.metadata is None:
            self.metadata = {}
        elif not isinstance(self.metadata, Mapping):
            raise ValueError("atom metadata must be a JSON object")
        else:
            self.metadata = dict(self.metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "atom_id": self.atom_id,
            "memory_id": self.memory_id,
            "body": self.body,
            "kind": self.kind,
            "status": self.status,
            "confidence": self.confidence,
            "locked": bool(self.locked),
            "injection_policy": self.injection_policy,
            "priority": self.priority,
            "canonical_hash": self.canonical_hash,
            "dedup_domain": self.dedup_domain,
            "supersedes": list(self.supersedes),
            "provenance": list(self.provenance),
            "agent_instance_id": self.agent_instance_id,
            "share_group_id": self.share_group_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "workspace_id": self.workspace_id,
            "revision": self.revision,
            "visibility": self.visibility,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    as_dict = to_dict

    @classmethod
    def from_value(cls, value: "MemoryAtom | Mapping[str, Any]", **overrides: Any) -> "MemoryAtom":
        if isinstance(value, MemoryAtom):
            data = value.to_dict()
        else:
            data = dict(value)
        data.update({key: item for key, item in overrides.items() if item is not None})
        return cls(
            memory_id=str(data.get("memory_id") or data.get("atom_key") or ""),
            body=str(data.get("body") or ""),
            kind=str(data.get("kind") or "fact"),
            status=str(data.get("status") or "active"),
            confidence=float(data.get("confidence", 0.5)),
            locked=bool(data.get("locked", False)),
            injection_policy=str(data.get("injection_policy") or "relevant"),
            priority=int(data.get("priority", 0)),
            canonical_hash=str(data.get("canonical_hash") or ""),
            dedup_domain=str(data.get("dedup_domain") or "relevant"),
            supersedes=list(data.get("supersedes") or []),
            provenance=list(data.get("provenance") or []),
            agent_instance_id=str(data.get("agent_instance_id") or ""),
            share_group_id=str(data.get("share_group_id") or data.get("group_id") or ""),
            project_ref=str(data.get("project_ref") or ""),
            provider=str(data.get("provider") or ""),
            runtime_role=str(data.get("runtime_role") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            atom_id=str(data.get("atom_id") or ""),
            revision=int(data.get("revision", 1)),
            visibility=str(data.get("visibility") or data.get("build_state") or "building"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class MemoryValidation:
    ok: bool
    atom_count: int
    evidence_count: int
    orphan_count: int
    outbox_pending: int
    scope_digest: str
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "atom_count": self.atom_count,
            "evidence_count": self.evidence_count,
            "orphan_count": self.orphan_count,
            "outbox_pending": self.outbox_pending,
            "scope_digest": self.scope_digest,
            "errors": list(self.errors),
        }

    as_dict = to_dict


class MemoryAtomStore:
    """SQLite memory atom store with a projection outbox."""

    SCHEMA_VERSION = 1
    SCHEMA_MARKER = "memoryguard-v2-phase2-memory"
    SCHEMA_META_TABLE = "memory_schema_meta"
    VISIBILITIES = frozenset({"building", "ready", "active", "hidden"})

    def __init__(
        self,
        workspace_or_path: str | Path | WorkspaceV2Layout,
        *,
        path: str | Path | None = None,
        readonly: bool = False,
        read_only: bool | None = None,
        initialize: bool = True,
    ) -> None:
        if read_only is not None:
            readonly = bool(read_only)
        value = path if path is not None else workspace_or_path
        if isinstance(value, WorkspaceV2Layout):
            self.layout = value
        else:
            candidate = Path(value).expanduser()
            if candidate.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                candidate = Path(os.path.abspath(os.fspath(candidate)))
                if candidate.parent.name != "memory" or candidate.parent.parent.name != WorkspaceV2Layout.ROOT_NAME:
                    raise ValueError("memory database must be inside .memoryguard/memory")
                self.layout = _safe_layout(candidate.parent.parent.parent)
            else:
                self.layout = _safe_layout(candidate)
        self.db_path = _path_for(path if path is not None else workspace_or_path, "memory")
        self.path = self.db_path
        self.readonly = bool(readonly)
        # Migration batches borrow one write connection for a whole source
        # group.  Keep this thread-local so ordinary governed writers remain
        # independent when a host performs a background shadow build.
        self._migration_state = threading.local()
        if self.readonly or not initialize:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            # Native runtime leases have already checked the complete schema.
            # Keep construction read-only, even for a later writable store:
            # opening RO/RW WAL handles together can leave Windows SQLite with
            # a read-only SHM mapping. Migrations belong to explicit startup.
            conn = self._checked_connect(readonly=True)
            try:
                self._check_schema_connection(conn)
            finally:
                conn.close()
        else:
            # Existing databases are inspected through mode=ro before any
            # writable SQLite handle is opened.  This matters on older SQLite
            # builds (notably the Python 3.10 CI runtime), where merely opening
            # a write-capable WAL connection and then rolling back can still
            # change the physical database image during close/checkpoint.
            # Future/unknown base markers therefore fail byte-stably.
            if self.db_path.is_file():
                self._preflight_write_schema()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._checked_connect(readonly=self.readonly)
        try:
            yield conn
        finally:
            conn.close()

    def _checked_connect(self, *, readonly: bool | None = None) -> sqlite3.Connection:
        self.layout.assert_database_path(self.db_path, "memory")
        from ..governance_v2.atomic import memory_connection

        borrowed = memory_connection(self.db_path)
        if borrowed is not None:
            return borrowed
        return connect_database(self.db_path, readonly=self.readonly if readonly is None else readonly)

    def _preflight_write_schema(self) -> None:
        """Reject unknown/future base metadata before any writable open."""

        with open_database_snapshot(self.db_path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "schema_meta" not in tables:
                if tables:
                    raise RuntimeError("memory base schema metadata is missing")
                return
            rows = conn.execute(
                "SELECT domain, version, marker FROM schema_meta"
            ).fetchall()
            if len(rows) != 1 or str(rows[0][0]) != "memory":
                raise RuntimeError("unsupported memory base schema metadata")
            version = int(rows[0][1])
            marker = str(rows[0][2])
            if version != self.SCHEMA_VERSION:
                raise RuntimeError("unsupported memory base schema version")
            if marker not in {BASE_SCHEMA_MARKER, self.SCHEMA_MARKER}:
                raise RuntimeError("unsupported memory base schema marker")
            if self.SCHEMA_META_TABLE in tables:
                phase_rows = conn.execute(
                    f"SELECT domain, version, marker FROM {self.SCHEMA_META_TABLE}"
                ).fetchall()
                if len(phase_rows) != 1 or str(phase_rows[0][0]) != "memory":
                    raise RuntimeError("unsupported memory phase2 schema metadata")
                if int(phase_rows[0][1]) != self.SCHEMA_VERSION or str(phase_rows[0][2]) != self.SCHEMA_MARKER:
                    raise RuntimeError("unsupported memory phase2 schema metadata")

    def _init_schema(self) -> None:
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                self._create_schema(conn)
        finally:
            conn.close()

    @classmethod
    def _create_schema(cls, conn: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE IF NOT EXISTS schema_meta (domain TEXT PRIMARY KEY, version INTEGER NOT NULL, marker TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS memory_schema_meta (domain TEXT PRIMARY KEY, version INTEGER NOT NULL, marker TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS atoms (atom_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5, locked INTEGER NOT NULL DEFAULT 0, injection_policy TEXT NOT NULL DEFAULT 'relevant', priority INTEGER NOT NULL DEFAULT 0, canonical_hash TEXT NOT NULL, dedup_domain TEXT NOT NULL DEFAULT 'relevant', supersedes_json TEXT NOT NULL DEFAULT '[]', provenance_json TEXT NOT NULL DEFAULT '[]', metadata_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1, visibility TEXT NOT NULL DEFAULT 'building', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT '', agent_instance_id TEXT NOT NULL DEFAULT '', share_group_id TEXT NOT NULL DEFAULT '', project_ref TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', runtime_role TEXT NOT NULL DEFAULT '', UNIQUE(share_group_id, memory_id))",
            # The scope columns are duplicated on atoms for efficient identity
            # filtering; scope_acl is the extensible ACL ledger.
            "ALTER TABLE atoms ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE atoms ADD COLUMN agent_instance_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE atoms ADD COLUMN share_group_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE atoms ADD COLUMN project_ref TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE atoms ADD COLUMN provider TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE atoms ADD COLUMN runtime_role TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_atoms_memory_scope ON atoms(share_group_id, memory_id)",
            "CREATE INDEX IF NOT EXISTS idx_atoms_visibility ON atoms(visibility, status)",
            "CREATE TABLE IF NOT EXISTS atom_revisions (revision_id TEXT PRIMARY KEY, atom_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, canonical_hash TEXT NOT NULL, revision_digest TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(atom_id, revision), FOREIGN KEY(atom_id) REFERENCES atoms(atom_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS atom_deltas (delta_id TEXT PRIMARY KEY, atom_id TEXT NOT NULL, from_revision INTEGER NOT NULL, to_revision INTEGER NOT NULL, delta_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(atom_id, from_revision, to_revision), FOREIGN KEY(atom_id) REFERENCES atoms(atom_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS supersession_edges (edge_id TEXT PRIMARY KEY, old_atom_id TEXT NOT NULL, new_atom_id TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(old_atom_id, new_atom_id), FOREIGN KEY(old_atom_id) REFERENCES atoms(atom_id) ON DELETE CASCADE, FOREIGN KEY(new_atom_id) REFERENCES atoms(atom_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS scope_acl (acl_id TEXT PRIMARY KEY, atom_id TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT '', agent_instance_id TEXT NOT NULL DEFAULT '', share_group_id TEXT NOT NULL DEFAULT '', project_ref TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', runtime_role TEXT NOT NULL DEFAULT '', effect TEXT NOT NULL DEFAULT 'allow', metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(atom_id, workspace_id, agent_instance_id, share_group_id, project_ref, provider, runtime_role), FOREIGN KEY(atom_id) REFERENCES atoms(atom_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS source_mappings (mapping_id TEXT PRIMARY KEY, atom_id TEXT NOT NULL, source_domain TEXT NOT NULL, source_ref TEXT NOT NULL, source_record_id TEXT NOT NULL DEFAULT '', source_revision TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '', provenance_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(atom_id, source_domain, source_ref, source_record_id), FOREIGN KEY(atom_id) REFERENCES atoms(atom_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS domain_outbox (event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','projected','failed')), attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, projected_at TEXT NOT NULL DEFAULT '', error_json TEXT NOT NULL DEFAULT '{}')",
            "CREATE INDEX IF NOT EXISTS idx_memory_outbox_status ON domain_outbox(status, sequence)",
            "CREATE TABLE IF NOT EXISTS outbox_checkpoints (domain TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS evidence_projection_receipts (event_id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, projected_at TEXT NOT NULL, error_json TEXT NOT NULL DEFAULT '{}', FOREIGN KEY(event_id) REFERENCES domain_outbox(event_id) ON DELETE CASCADE)",
            "CREATE TABLE IF NOT EXISTS domain_state (domain TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'BUILDING', generation INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')",
            # Keep storage/schema.py's Phase-1 marker in the shared table.
            # Older shadow builds wrote the Phase-2 marker there; known old
            # marker is repaired while unknown markers remain untouched and
            # are rejected by the read-only validator.
            "INSERT INTO schema_meta(domain, version, marker, updated_at) VALUES('memory', 1, 'memoryguard-v2-phase1', ?) ON CONFLICT(domain) DO NOTHING",
            "INSERT INTO memory_schema_meta(domain, version, marker, updated_at) VALUES('memory', 1, 'memoryguard-v2-phase2-memory', ?) ON CONFLICT(domain) DO NOTHING",
            "INSERT INTO outbox_checkpoints(domain, last_sequence, updated_at) VALUES('memory', 0, ?) ON CONFLICT(domain) DO NOTHING",
            "INSERT INTO domain_state(domain, state, generation, updated_at, metadata_json) VALUES('memory','BUILDING',0,?,'{}') ON CONFLICT(domain) DO NOTHING",
        )
        now = _now()
        for statement in statements:
            try:
                if "INSERT INTO schema_meta" in statement or "INSERT INTO memory_schema_meta" in statement or "INSERT INTO outbox_checkpoints" in statement or "VALUES('memory','BUILDING'" in statement:
                    conn.execute(statement, (now,))
                else:
                    conn.execute(statement)
            except sqlite3.OperationalError as exc:
                # ALTER TABLE is intentionally idempotent for databases that
                # were already bootstrapped by an earlier phase2 process.
                if statement.startswith("ALTER TABLE atoms ADD COLUMN") and "duplicate column name" in str(exc).lower():
                    continue
                raise

        # ``atoms_fts`` is a derived search index, not part of the memory
        # authority schema.  Keep it optional so a Python/SQLite build without
        # FTS5 can still read and write atoms through the LIKE fallback.
        try:
            for statement in (
                "CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts USING fts5(memory_id UNINDEXED, body, content='atoms', content_rowid='rowid', tokenize='trigram')",
                "CREATE TRIGGER IF NOT EXISTS atoms_fts_ai AFTER INSERT ON atoms BEGIN INSERT INTO atoms_fts(rowid, memory_id, body) VALUES (new.rowid, new.memory_id, new.body); END",
                "CREATE TRIGGER IF NOT EXISTS atoms_fts_ad AFTER DELETE ON atoms BEGIN INSERT INTO atoms_fts(atoms_fts, rowid, memory_id, body) VALUES ('delete', old.rowid, old.memory_id, old.body); END",
                "CREATE TRIGGER IF NOT EXISTS atoms_fts_au AFTER UPDATE ON atoms BEGIN INSERT INTO atoms_fts(atoms_fts, rowid, memory_id, body) VALUES ('delete', old.rowid, old.memory_id, old.body); INSERT INTO atoms_fts(rowid, memory_id, body) VALUES (new.rowid, new.memory_id, new.body); END",
                "INSERT INTO atoms_fts(atoms_fts) VALUES ('rebuild')",
            ):
                conn.execute(statement)
        except sqlite3.DatabaseError as exc:
            # FTS5 is an acceleration/recall projection.  Do not make the
            # canonical atom store unusable when the optional extension is
            # absent or an old/corrupt index cannot be rebuilt.
            message = str(exc).casefold()
            if not any(marker in message for marker in ("fts5", "no such module", "malformed", "fts")):
                raise

        # Validate shared Phase-1 metadata before allowing any write.  Only
        # the known early marker (version 1) may be repaired; future or
        # unknown metadata fails closed and transaction rollback preserves it.
        base_rows = conn.execute("SELECT domain, version, marker FROM schema_meta").fetchall()
        if len(base_rows) != 1 or str(base_rows[0][0]) != "memory":
            raise RuntimeError("unsupported memory base schema metadata")
        base_version, base_marker = int(base_rows[0][1]), str(base_rows[0][2])
        if base_version != cls.SCHEMA_VERSION:
            raise RuntimeError("unsupported memory base schema version")
        if base_marker == "memoryguard-v2-phase2-memory":
            conn.execute("UPDATE schema_meta SET marker=?, updated_at=? WHERE domain='memory'", (BASE_SCHEMA_MARKER, now))
        elif base_marker != BASE_SCHEMA_MARKER:
            raise RuntimeError("unsupported memory base schema marker")
        phase_row = conn.execute("SELECT version, marker FROM memory_schema_meta WHERE domain='memory'").fetchone()
        if phase_row is None or int(phase_row[0]) != cls.SCHEMA_VERSION or str(phase_row[1]) != cls.SCHEMA_MARKER:
            raise RuntimeError("unsupported memory phase2 schema metadata")

    @classmethod
    def _check_schema_connection(cls, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT version, marker FROM memory_schema_meta WHERE domain='memory'").fetchone()
        if row is None or int(row[0]) != cls.SCHEMA_VERSION or str(row[1]) != cls.SCHEMA_MARKER:
            raise RuntimeError("unsupported memory schema")
        tables = {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"atoms", "atom_revisions", "domain_outbox", "scope_acl", "source_mappings"} <= tables:
            raise RuntimeError("incomplete memory schema")

    def _check_schema(self) -> None:
        with self._connection() as conn:
            self._check_schema_connection(conn)

    @staticmethod
    def atom_id_for(memory_id: str, share_group_id: str = "", *, agent_instance_id: str = "", project_ref: str = "", provider: str = "", runtime_role: str = "") -> str:
        return stable_digest({"memory_id": str(memory_id), "share_group_id": str(share_group_id), "agent_instance_id": str(agent_instance_id), "project_ref": canonical_project_ref(project_ref), "provider": str(provider or "").strip().casefold(), "runtime_role": str(runtime_role or "").strip().casefold()})

    @staticmethod
    def _row_to_atom(row: sqlite3.Row) -> MemoryAtom:
        def load(value: Any, default: Any) -> Any:
            if value is None or value == "":
                return default
            try:
                return json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("stored atom JSON field is invalid") from exc
        provenance = load(row["provenance_json"], [])
        metadata = load(row["metadata_json"], {})
        if not isinstance(provenance, list) or any(not isinstance(item, Mapping) for item in provenance):
            raise ValueError("stored atom provenance must be a list of JSON objects")
        provenance = [
            _validate_metadata_tree(item, label="atom provenance")
            for item in provenance
        ]
        metadata = _validate_metadata(metadata, label="atom metadata")
        return MemoryAtom(
            atom_id=str(row["atom_id"]), memory_id=str(row["memory_id"]), body=str(row["body"] or ""), kind=str(row["kind"] or "fact"), status=str(row["status"] or "active"), confidence=float(row["confidence"] if row["confidence"] is not None else 0.5), locked=bool(row["locked"]), injection_policy=str(row["injection_policy"] or "relevant"), priority=int(row["priority"] or 0), canonical_hash=str(row["canonical_hash"] or ""), dedup_domain=str(row["dedup_domain"] or "relevant"), supersedes=load(row["supersedes_json"], []), provenance=provenance, agent_instance_id=str(row["agent_instance_id"] or ""), share_group_id=str(row["share_group_id"] or ""), project_ref=str(row["project_ref"] or ""), provider=str(row["provider"] or ""), runtime_role=str(row["runtime_role"] or ""), workspace_id=str(row["workspace_id"] or ""), revision=int(row["revision"] or 1), visibility=str(row["visibility"] or "building"), created_at=str(row["created_at"] or ""), updated_at=str(row["updated_at"] or ""), metadata=metadata,
        )

    def _coerce_atom(self, value: MemoryAtom | Mapping[str, Any]) -> MemoryAtom:
        atom = MemoryAtom.from_value(value)
        if not atom.memory_id:
            raise ValueError("memory_id is required")
        if not atom.canonical_hash:
            atom.canonical_hash = stable_digest(atom.body)
        atom.metadata = _validate_metadata(atom.metadata, label="atom metadata")
        provenance = _validate_metadata_tree(atom.provenance, label="atom provenance")
        if not isinstance(provenance, list) or any(not isinstance(item, Mapping) for item in provenance):
            raise ValueError("atom provenance must be a list of JSON objects")
        atom.provenance = [dict(item) for item in provenance]
        if not atom.workspace_id:
            atom.workspace_id = str(self.layout.workspace)
        if not atom.atom_id:
            atom.atom_id = self.atom_id_for(atom.memory_id, atom.share_group_id, agent_instance_id=atom.agent_instance_id, project_ref=atom.project_ref, provider=atom.provider, runtime_role=atom.runtime_role)
        if atom.visibility not in self.VISIBILITIES:
            raise ValueError(f"unsupported atom visibility: {atom.visibility!r}")
        if not atom.created_at:
            atom.created_at = _now()
        if not atom.updated_at:
            atom.updated_at = atom.created_at
        return atom

    def _authorize_v2_context(
        self,
        context: Any,
        atom: MemoryAtom,
        *,
        existing: MemoryAtom | None = None,
    ) -> Any:
        """Validate an optional V2 governance context at the store boundary."""

        if context is None:
            return None
        from ..governance_v2.context import V2MutationContext, V2ScopeError

        resolved = V2MutationContext.from_value(context)
        resolved.check_scope(
            workspace_id=self.layout.workspace,
            share_group_id=atom.share_group_id,
            agent_instance_id=atom.agent_instance_id,
            project_ref=atom.project_ref,
            provider=atom.provider,
            runtime_role=atom.runtime_role,
        )
        if existing is not None and not resolved.admin:
            # ``(share_group_id, memory_id)`` is the logical-record identity,
            # while ``atom_id`` is only the physical V2 row identity.  An
            # incoming atom can therefore pass the field-level check while
            # still attempting to replace another agent's logical record.
            # Keep this refusal deliberately neutral: callers must not learn
            # the existing owner's identity or SQLite constraint details.
            owner_fields = (
                "agent_instance_id",
                "project_ref",
                "provider",
                "runtime_role",
            )
            if any(
                str(getattr(existing, field) or "")
                != str(getattr(atom, field) or "")
                for field in owner_fields
            ):
                raise V2ScopeError("mutation logical record is outside context")
        return resolved

    def _next_sequence(self, conn: sqlite3.Connection) -> int:
        state_conn = getattr(self._migration_state, "conn", None)
        if state_conn is conn:
            sequence = getattr(self._migration_state, "sequence", None)
            if sequence is None:
                sequence = int(conn.execute("SELECT COALESCE(MAX(sequence),0) FROM domain_outbox").fetchone()[0] or 0)
            sequence += 1
            self._migration_state.sequence = sequence
            return sequence
        row = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM domain_outbox").fetchone()
        return int(row[0])

    @contextmanager
    def migration_batch(self) -> Iterator[sqlite3.Connection]:
        """Run one source-group migration on a single atomic connection.

        The migrator uses this only for V1 shadow writes.  It is deliberately
        private-in-practice (callers still need migration capabilities) and
        does not alter normal public write semantics.
        """
        if self.readonly:
            raise PermissionError("memory store is read-only")
        if getattr(self._migration_state, "conn", None) is not None:
            raise RuntimeError("nested memory migration batch")
        conn = self._checked_connect(readonly=False)
        self._migration_state.conn = conn
        self._migration_state.sequence = None
        try:
            with transaction(conn):
                yield conn
        finally:
            self._migration_state.conn = None
            self._migration_state.sequence = None
            conn.close()

    def _queue_event(self, conn: sqlite3.Connection, event_type: str, aggregate_id: str, payload: Mapping[str, Any], *, event_id: str = "") -> str:
        event_id = event_id or stable_digest({"event_type": event_type, "aggregate_id": aggregate_id, "payload": payload})
        conn.execute(
            "INSERT INTO domain_outbox(event_id,sequence,event_type,aggregate_id,payload_json,status,attempts,created_at) VALUES(?,?,?,?,?,'pending',0,?) ON CONFLICT(event_id) DO NOTHING",
            (event_id, self._next_sequence(conn), event_type, aggregate_id, _json(payload), _now()),
        )
        return event_id

    def _put_atom_impl(
        self,
        atom: MemoryAtom | Mapping[str, Any],
        *,
        evidence: Sequence[str | Mapping[str, Any]] | None = None,
        evidence_ids: Sequence[str] | None = None,
        source_mappings: Sequence[Mapping[str, Any]] | None = None,
        acl: Mapping[str, Any] | None = None,
        visibility: str | None = None,
        replace: bool = True,
        context: Any | None = None,
    ) -> MemoryAtom:
        if self.readonly:
            raise PermissionError("memory store is read-only")
        item = self._coerce_atom(MemoryAtom.from_value(atom, visibility=visibility) if visibility is not None else atom)
        self._authorize_v2_context(context, item)
        evidence_payload: list[dict[str, Any]] = []
        for value in evidence or ():
            if isinstance(value, Mapping):
                evidence_payload.append(dict(value))
            else:
                evidence_id = str(value)
                evidence_payload.append({
                    "evidence_id": evidence_id,
                    "source_ref": f"memoryguard:evidence:{evidence_id}",
                    "digest": stable_digest({"evidence_id": evidence_id}),
                    "authority": "observed",
                    "status": "valid",
                    "evidence_type": "reference",
                })
        for value in evidence_ids or ():
            evidence_id = str(value)
            evidence_payload.append({
                "evidence_id": evidence_id,
                "source_ref": f"memoryguard:evidence:{evidence_id}",
                "digest": stable_digest({"evidence_id": evidence_id}),
                "authority": "observed",
                "status": "valid",
                "evidence_type": "reference",
            })
        _validate_evidence_authorities(evidence_payload)
        # Evidence records are projected separately.  Keep only references in
        # memory.db; full evidence payloads are deliberately not stored here.
        migration_conn = getattr(self._migration_state, "conn", None)
        conn = migration_conn or self._checked_connect(readonly=False)
        try:
            with (nullcontext(conn) if migration_conn is not None else transaction(conn)):
                existing_by_id = conn.execute("SELECT * FROM atoms WHERE atom_id=?", (item.atom_id,)).fetchone()
                logical_existing = conn.execute(
                    "SELECT * FROM atoms WHERE share_group_id=? AND memory_id=? "
                    "AND agent_instance_id=? AND project_ref=? AND provider=? "
                    "AND runtime_role=? ORDER BY revision DESC, updated_at DESC LIMIT 1",
                    (
                        item.share_group_id,
                        item.memory_id,
                        item.agent_instance_id,
                        item.project_ref,
                        item.provider,
                        item.runtime_role,
                    ),
                ).fetchone()
                logical_atom = self._row_to_atom(logical_existing) if logical_existing is not None else None
                owner_atom = logical_atom
                if owner_atom is None and existing_by_id is not None:
                    owner_atom = self._row_to_atom(existing_by_id)
                self._authorize_v2_context(context, item, existing=owner_atom)
                # Resolve every update through the logical key.  This makes
                # admin corrections and retries converge on the existing
                # physical atom instead of attempting a second UNIQUE row.
                existing = logical_existing or existing_by_id
                if logical_existing is not None:
                    item.atom_id = str(logical_existing["atom_id"])
                if existing is not None and not replace:
                    return self._row_to_atom(existing)
                if existing is not None:
                    existing_visibility = str(existing["visibility"] or "")
                    visibility_matches = (
                        existing_visibility == item.visibility
                        or (
                            existing_visibility == "building"
                            and item.visibility in {"ready", "active"}
                            and bool(evidence_payload)
                            and str(existing["status"] or "") == "active"
                        )
                    )
                    same_payload = (
                        str(existing["body"] or "") == item.body
                        and str(existing["kind"] or "") == item.kind
                        and str(existing["status"] or "") == item.status
                        and float(existing["confidence"] if existing["confidence"] is not None else 0.5) == float(item.confidence)
                        and bool(existing["locked"]) == bool(item.locked)
                        and str(existing["injection_policy"] or "") == item.injection_policy
                        and int(existing["priority"] or 0) == int(item.priority)
                        and str(existing["canonical_hash"] or "") == item.canonical_hash
                        and str(existing["dedup_domain"] or "") == item.dedup_domain
                        and str(existing["supersedes_json"] or "[]") == _json(item.supersedes)
                        and str(existing["provenance_json"] or "[]") == _json(item.provenance)
                        and str(existing["metadata_json"] or "{}") == _json(item.metadata)
                        and str(existing["share_group_id"] or "") == item.share_group_id
                        and str(existing["memory_id"] or "") == item.memory_id
                        and str(existing["workspace_id"] or "") == item.workspace_id
                        and str(existing["agent_instance_id"] or "") == item.agent_instance_id
                        and str(existing["project_ref"] or "") == item.project_ref
                        and str(existing["provider"] or "") == item.provider
                        and str(existing["runtime_role"] or "") == item.runtime_role
                        and visibility_matches
                    )
                    if same_payload:
                        return self._row_to_atom(existing)
                publication_visibility = ""
                stage_visible_update = False
                if evidence_payload:
                    existing_visibility = str(existing["visibility"] or "") if existing is not None else ""
                    if existing is not None and existing_visibility in {"ready", "active"}:
                        stage_visible_update = True
                        publication_visibility = item.visibility if item.visibility in {"ready", "active"} else existing_visibility
                    elif item.visibility in {"ready", "active"} and str(item.status or "active") == "active":
                        publication_visibility = item.visibility
                        item.visibility = "building"
                    if publication_visibility:
                        for payload in evidence_payload:
                            payload["publication_visibility"] = publication_visibility
                revision = int(existing["revision"]) if existing is not None else 0
                if existing is not None:
                    max_rev = int(conn.execute("SELECT COALESCE(MAX(revision),0) FROM atom_revisions WHERE atom_id=?", (item.atom_id,)).fetchone()[0] or 0)
                    revision = max(revision, max_rev)
                if existing is not None and revision >= item.revision:
                    item.revision = revision + 1
                if not stage_visible_update:
                    self._upsert_atom_row(conn, item)
                rev_id = stable_digest({"atom_id": item.atom_id, "revision": item.revision})
                rev_digest = stable_digest({"body": item.body, "status": item.status, "canonical_hash": item.canonical_hash, "metadata": item.metadata})
                conn.execute(
                    "INSERT INTO atom_revisions(revision_id,atom_id,revision,body,status,canonical_hash,revision_digest,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(atom_id,revision) DO UPDATE SET body=excluded.body,status=excluded.status,canonical_hash=excluded.canonical_hash,revision_digest=excluded.revision_digest,metadata_json=excluded.metadata_json",
                    (rev_id,item.atom_id,item.revision,item.body,item.status,item.canonical_hash,rev_digest,_json(item.metadata),item.updated_at),
                )
                if existing is not None and int(existing["revision"]) != item.revision:
                    before = int(existing["revision"])
                    if stage_visible_update:
                        delta = item.to_dict()
                        delta["staged_acl"] = dict(acl or {})
                        delta["staged_source_mappings"] = [dict(value) for value in (source_mappings or ()) if isinstance(value, Mapping)]
                        self._write_scope_acl(conn, item, acl, persist=False)
                        self._write_source_mappings(conn, item, source_mappings, persist=False)
                    else:
                        delta = {"body": item.body, "status": item.status, "confidence": item.confidence, "locked": item.locked, "canonical_hash": item.canonical_hash, "metadata": dict(item.metadata)}
                    delta_id = stable_digest({"atom_id": item.atom_id, "from": before, "to": item.revision})
                    conn.execute("INSERT INTO atom_deltas(delta_id,atom_id,from_revision,to_revision,delta_json,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(atom_id,from_revision,to_revision) DO UPDATE SET delta_json=excluded.delta_json", (delta_id,item.atom_id,before,item.revision,_json(delta),item.updated_at))
                if not stage_visible_update:
                    self._write_scope_acl(conn, item, acl)
                    self._write_source_mappings(conn, item, source_mappings)
                for payload in evidence_payload:
                    payload.setdefault("subject_type", "atom")
                    payload.setdefault("subject_id", item.atom_id)
                    payload["publication_revision"] = int(item.revision)
                    checked_payload = _validate_metadata_tree(payload, label="evidence outbox payload")
                    if not isinstance(checked_payload, Mapping):
                        raise ValueError("evidence outbox payload must be a JSON object")
                    payload.clear()
                    payload.update(checked_payload)
                    if "metadata" in payload:
                        payload["metadata"] = _validate_metadata(payload.get("metadata"), label="evidence metadata")
                    if "link_metadata" in payload:
                        payload["link_metadata"] = _validate_metadata(payload.get("link_metadata"), label="evidence link metadata")
                    nested_evidence = payload.get("evidence")
                    if isinstance(nested_evidence, Mapping) and "metadata" in nested_evidence:
                        nested_evidence = dict(nested_evidence)
                        nested_evidence["metadata"] = _validate_metadata(nested_evidence.get("metadata"), label="evidence metadata")
                        payload["evidence"] = nested_evidence
                    self._queue_event(conn, "evidence.put_link", item.atom_id, payload)
                # An atom without evidence is allowed only while BUILDING; it
                # cannot be promoted/served until the projection validator sees
                # a valid evidence link.
        finally:
            if migration_conn is None:
                conn.close()
        return item

    def put_atom(
        self,
        atom: MemoryAtom | Mapping[str, Any],
        *,
        evidence: Sequence[str | Mapping[str, Any]] | None = None,
        evidence_ids: Sequence[str] | None = None,
        source_mappings: Sequence[Mapping[str, Any]] | None = None,
        acl: Mapping[str, Any] | None = None,
        visibility: str | None = None,
        replace: bool = True,
        context: Any | None = None,
    ) -> MemoryAtom:
        """Public governed atom write; an explicit context is mandatory."""

        if context is None:
            raise PermissionError("explicit V2 mutation context required")
        return self._put_atom_impl(
            atom,
            evidence=evidence,
            evidence_ids=evidence_ids,
            source_mappings=source_mappings,
            acl=acl,
            visibility=visibility,
            replace=replace,
            context=context,
        )

    def queue_evidence(
        self,
        evidence: Mapping[str, Any],
        *,
        subject_type: str = "migration",
        subject_id: str = "",
        relation: str = "supports",
        aggregate_id: str = "",
    ) -> str:
        """Queue a reference-only evidence projection in memory.db.

        This is used for governance/audit rows that do not map to one atom.
        The event participates in the same memory-domain transaction boundary;
        :meth:`project_evidence` performs the separate evidence-domain write.
        """
        if self.readonly:
            raise PermissionError("memory store is read-only")
        payload = dict(evidence)
        checked_payload = _validate_metadata_tree(payload, label="evidence outbox payload")
        if not isinstance(checked_payload, Mapping):
            raise ValueError("evidence outbox payload must be a JSON object")
        payload = dict(checked_payload)
        if "metadata" in payload:
            payload["metadata"] = _validate_metadata(payload.get("metadata"), label="evidence metadata")
        if "link_metadata" in payload:
            payload["link_metadata"] = _validate_metadata(payload.get("link_metadata"), label="evidence link metadata")
        payload.update({"subject_type": subject_type, "subject_id": subject_id or aggregate_id, "relation": relation})
        _validate_evidence_authorities([payload])
        migration_conn = getattr(self._migration_state, "conn", None)
        conn = migration_conn or self._checked_connect(readonly=False)
        try:
            with (nullcontext(conn) if migration_conn is not None else transaction(conn)):
                return self._queue_event(conn, "evidence.put_link", aggregate_id or str(subject_id or "migration"), payload)
        finally:
            if migration_conn is None:
                conn.close()

    def _queue_evidence_for_migration(self, evidence: Mapping[str, Any], *, subject_type: str = "migration", subject_id: str = "", relation: str = "supports", aggregate_id: str = "", capability: object) -> str:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        return self.queue_evidence(evidence, subject_type=subject_type, subject_id=subject_id, relation=relation, aggregate_id=aggregate_id)

    def rollback_scope(self, *, share_group_id: str = "", atom_ids: Sequence[str] = ()) -> int:
        """Compensating rollback for one failed shadow-build scope.

        A migrator calls this before any evidence projection occurs.  It is
        deliberately narrow: only the supplied atom IDs and their pending
        outbox events are removed, leaving unrelated groups untouched.
        """
        if self.readonly:
            raise PermissionError("memory store is read-only")
        ids = [str(value) for value in atom_ids if value]
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(f"DELETE FROM domain_outbox WHERE aggregate_id IN ({placeholders}) AND status='pending'", ids)
                    cur = conn.execute(f"DELETE FROM atoms WHERE atom_id IN ({placeholders})", ids)
                    return int(cur.rowcount)
                if share_group_id:
                    rows = conn.execute("SELECT atom_id FROM atoms WHERE share_group_id=?", (share_group_id,)).fetchall()
                    group_ids = [str(row[0]) for row in rows]
                    if group_ids:
                        placeholders = ",".join("?" for _ in group_ids)
                        conn.execute(f"DELETE FROM domain_outbox WHERE aggregate_id IN ({placeholders}) AND status='pending'", group_ids)
                        cur = conn.execute(f"DELETE FROM atoms WHERE atom_id IN ({placeholders})", group_ids)
                        return int(cur.rowcount)
                return 0
        finally:
            conn.close()

    def _scope_from_args(
        self,
        scope: MemoryReadScope | Mapping[str, Any] | None,
        *,
        share_group_id: str = "",
        agent_instance_id: str = "",
        project_ref: str = "",
        provider: str = "",
        runtime_role: str = "",
    ) -> MemoryReadScope | None:
        if scope is not None:
            if hasattr(scope, "to_dict") and callable(scope.to_dict):
                scope = scope.to_dict()
            resolved = MemoryReadScope.from_value(scope)
            if share_group_id and str(share_group_id) != resolved.share_group_id:
                raise ValueError("read scope share_group_id conflicts with argument")
            if os.path.abspath(os.fspath(Path(resolved.workspace_id).expanduser())) != os.path.abspath(os.fspath(self.layout.workspace)):
                raise PermissionError("read scope workspace_id does not match store workspace")
            return resolved
        if any((share_group_id, agent_instance_id, project_ref, provider, runtime_role)):
            raise ValueError("explicit read scope required; include workspace_id")
        return None

    def _get_atom_unscoped(self, memory_id: str, *, share_group_id: str = "", atom_id: str = "", include_building: bool = False) -> MemoryAtom | None:
        query = "SELECT * FROM atoms WHERE " + ("atom_id=?" if atom_id else "memory_id=?")
        params: list[Any] = [atom_id or memory_id]
        if share_group_id:
            query += " AND share_group_id=?"
            params.append(share_group_id)
        if not include_building:
            query += " AND visibility IN ('ready','active')"
        with self._connection() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row_to_atom(row) if row is not None else None

    def get_atom(
        self,
        memory_id: str,
        *,
        scope: MemoryReadScope | Mapping[str, Any] | None = None,
        share_group_id: str = "",
        atom_id: str = "",
        include_building: bool = False,
    ) -> MemoryAtom | None:
        resolved = self._scope_from_args(scope, share_group_id=share_group_id)
        # Unscoped reads are existence-neutral; callers must opt into an
        # explicit group/agent/project/provider/runtime scope.
        if resolved is None:
            return None
        return self._get_atom_scoped(memory_id, resolved, atom_id=atom_id, include_building=include_building)

    def _get_atom_scoped(self, memory_id: str, scope: MemoryReadScope, *, atom_id: str = "", include_building: bool = False) -> MemoryAtom | None:
        query = "SELECT * FROM atoms WHERE " + ("atom_id=?" if atom_id else "memory_id=?")
        params: list[Any] = [atom_id or memory_id, scope.workspace_id, scope.share_group_id]
        query += " AND workspace_id=? AND share_group_id=?"
        if not include_building:
            query += " AND visibility IN ('ready','active')"
        query += " ORDER BY revision DESC, updated_at DESC, created_at DESC, atom_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        for row in rows:
            atom = self._row_to_atom(row)
            if self._atom_visible_to_scope(atom, scope):
                return atom
            # Pre-audience V2 migration rows retained their writer Agent in
            # the owner column even though they belonged to the shared group
            # memory plane.  Match the existing list/read compatibility seam
            # for an explicitly group-only caller, while keeping all ordinary
            # Agent/project/provider/runtime reads fail-closed.
            if self._legacy_group_scope_visible(atom, scope):
                return atom
        return None

    @staticmethod
    def _native_audience(atom: MemoryAtom) -> dict[str, Any] | None:
        """Return only the native, validated audience marker, if present.

        Arbitrary user metadata is never treated as an ACL.  Native writes
        stamp ``source=native_v2`` after validating the target against the
        trusted transport context; legacy/migration metadata therefore falls
        back to the atom's exact owner scope.
        """

        metadata = atom.metadata if isinstance(atom.metadata, Mapping) else {}
        raw = metadata.get("audience")
        if not isinstance(raw, Mapping) or str(raw.get("source") or "") != "native_v2":
            return None
        target_type = str(raw.get("target_type") or "").strip().casefold()
        if target_type not in {"agent", "agent_project", "project", "group"}:
            return None
        effect = str(raw.get("effect") or "include").strip().casefold()
        if effect not in {"include", "exclude"}:
            return None
        return {
            "target_type": target_type,
            "target_id": str(raw.get("target_id") or ""),
            "project_ref": canonical_project_ref(raw.get("project_ref") or ""),
            "provider": str(raw.get("provider") or "").casefold(),
            "runtime_role": str(raw.get("runtime_role") or "").casefold(),
            "effect": effect,
        }

    @classmethod
    def _atom_visible_to_scope(cls, atom: MemoryAtom, scope: MemoryReadScope) -> bool:
        """Apply V2 agent/group/project audience visibility after group ACL."""

        if atom.workspace_id != scope.workspace_id or atom.share_group_id != scope.share_group_id:
            return False
        if scope.admin:
            return True
        audience = cls._native_audience(atom)
        if audience is None:
            # Old V2 rows have no audience marker.  Their persisted owner
            # columns are the trusted fallback: an agent-owned row must never
            # become visible merely because the caller omitted an optional
            # project/provider/runtime value.  A row without an agent owner is
            # group-scoped and may be read by any agent already admitted to
            # the exact group.
            persisted_agent = str(atom.agent_instance_id or "")
            if persisted_agent and persisted_agent != str(scope.agent_instance_id or ""):
                return False
            persisted_project = canonical_project_ref(atom.project_ref or "")
            requested_project = canonical_project_ref(scope.project_ref or "")
            if persisted_project and requested_project and persisted_project != requested_project:
                return False
            persisted_provider = str(atom.provider or "").casefold()
            requested_provider = str(scope.provider or "").casefold()
            if persisted_provider and requested_provider and persisted_provider != requested_provider:
                return False
            persisted_runtime = str(atom.runtime_role or "").casefold()
            requested_runtime = str(scope.runtime_role or "").casefold()
            if persisted_runtime and requested_runtime and persisted_runtime != requested_runtime:
                return False
            return True

        target_type = audience["target_type"]
        target_id = audience["target_id"]
        project = audience["project_ref"]
        matches = False
        if target_type == "agent":
            matches = target_id == scope.agent_instance_id
        elif target_type == "agent_project":
            matches = (
                target_id == scope.agent_instance_id
                and bool(project)
                and canonical_project_ref(project) == canonical_project_ref(scope.project_ref)
            )
        elif target_type == "project":
            matches = (
                bool(project)
                and canonical_project_ref(project) == canonical_project_ref(scope.project_ref)
            )
        elif target_type == "group":
            matches = target_id == scope.share_group_id
        # Audience type is the complete visibility contract. Optional fields
        # left behind by older native writers must not silently narrow an
        # ``agent`` or ``group`` audience into an undocumented compound scope.
        # Project matching is already explicit in ``agent_project`` and
        # ``project`` above; provider/runtime are ownership/provenance fields,
        # not public memory audience dimensions.
        if audience["effect"] == "exclude":
            return not matches
        return matches

    @classmethod
    def _legacy_group_scope_visible(cls, atom: MemoryAtom, scope: MemoryReadScope) -> bool:
        """Read an old atom through an explicit, exact group-only scope.

        Pre-audience V2 rows were written to the group memory plane while
        retaining the writer Agent in their owner columns.  A validated GUI
        shared-group projection represents that plane with an empty Agent and
        empty optional dimensions.  Keep that compatibility seam narrow:
        native audience markers are never widened here, and any non-empty
        Agent/project/provider/runtime value remains on the exact-owner path.
        The workspace/group predicate is still enforced before this helper is
        called by the public list path.
        """

        if any(
            str(value or "").strip()
            for value in (
                scope.agent_instance_id,
                scope.project_ref,
                scope.provider,
                scope.runtime_role,
            )
        ):
            return False
        metadata = atom.metadata if isinstance(atom.metadata, Mapping) else {}
        # A malformed or non-group audience marker must not be treated as a
        # wildcard.  Native group audiences are handled by the normal matcher.
        if isinstance(metadata.get("audience"), Mapping):
            return False
        return bool(atom.workspace_id == scope.workspace_id and atom.share_group_id == scope.share_group_id)

    def _list_atoms_unscoped(self, *, share_group_id: str | None = None, status: str | None = None, include_building: bool = False, visibility: str | None = None) -> list[MemoryAtom]:
        query = "SELECT * FROM atoms WHERE 1=1"
        params: list[Any] = []
        if not include_building:
            query += " AND visibility IN ('ready','active')"
        if visibility:
            query += " AND visibility=?"
            params.append(str(visibility))
        if share_group_id is not None:
            query += " AND share_group_id=?"
            params.append(str(share_group_id))
        if status:
            query += " AND status=?"
            params.append(str(status))
        query += " ORDER BY revision DESC, updated_at DESC, created_at DESC, atom_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_atom(row) for row in rows]

    def list_atoms(
        self,
        *,
        scope: MemoryReadScope | Mapping[str, Any] | None = None,
        share_group_id: str | None = None,
        agent_instance_id: str = "",
        project_ref: str = "",
        provider: str = "",
        runtime_role: str = "",
        status: str | None = None,
        include_building: bool = False,
        visibility: str | None = None,
    ) -> list[MemoryAtom]:
        resolved = self._scope_from_args(
            scope,
            share_group_id=str(share_group_id or ""),
            agent_instance_id=agent_instance_id,
            project_ref=project_ref,
            provider=provider,
            runtime_role=runtime_role,
        )
        if resolved is None:
            return []
        query = "SELECT * FROM atoms WHERE workspace_id=? AND share_group_id=?"
        params: list[Any] = [resolved.workspace_id, resolved.share_group_id]
        if status:
            query += " AND status=?"
            params.append(str(status))
        if not include_building:
            query += " AND visibility IN ('ready','active')"
        if visibility:
            query += " AND visibility=?"
            params.append(str(visibility))
        query += " ORDER BY created_at, atom_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        group_scope = not any(
            str(value or "").strip()
            for value in (
                resolved.agent_instance_id,
                resolved.project_ref,
                resolved.provider,
                resolved.runtime_role,
            )
        )
        visible: list[MemoryAtom] = []
        for row in rows:
            atom = self._row_to_atom(row)
            if self._atom_visible_to_scope(atom, resolved):
                visible.append(atom)
            elif group_scope and self._legacy_group_scope_visible(atom, resolved):
                visible.append(atom)
        return visible

    @staticmethod
    def _fts_query(query: str) -> str:
        """Return a quoted, harmless FTS5 MATCH expression.

        A user query is data, not an FTS expression.  Quoting each whitespace
        separated term prevents operators/column selectors from changing the
        query.  OR preserves recall when a query contains several keywords;
        the scope and status filters below still constrain every result.
        """

        parts = [part.strip() for part in str(query or "").split() if part.strip()]
        return " OR ".join('"' + part.replace('"', '""') + '"' for part in parts)

    def search(
        self,
        query: str,
        *,
        scope: MemoryReadScope | Mapping[str, Any] | None = None,
        status: str | None = "active",
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[MemoryAtom]:
        """Search visible atoms with FTS5, falling back to body matching.

        FTS5 is deliberately treated as a rebuildable projection.  Missing or
        malformed FTS support must not make memory unreadable, so all database
        MATCH failures fall back to the same scoped ``list_atoms`` read with a
        deterministic occurrence score.
        """

        text = str(query or "").strip()
        if not text:
            return []
        resolved = self._scope_from_args(scope)
        if resolved is None:
            return []
        try:
            requested_limit = int(limit) if limit is not None else 20
        except (TypeError, ValueError):
            requested_limit = 20
        requested_limit = max(1, min(requested_limit, 100))

        fts_query = self._fts_query(text)
        # The trigram tokenizer cannot match one/two-character terms.  LIKE
        # handles those queries (and Chinese text) without relying on a
        # tokenizer-specific extension.
        use_fts = bool(fts_query) and all(len(part) >= 3 for part in text.split())
        if use_fts:
            sql = (
                "SELECT a.*, bm25(atoms_fts) AS _rank "
                "FROM atoms_fts JOIN atoms AS a ON a.rowid=atoms_fts.rowid "
                "WHERE atoms_fts MATCH ? AND a.workspace_id=? AND a.share_group_id=?"
            )
            params: list[Any] = [fts_query, resolved.workspace_id, resolved.share_group_id]
            if status:
                sql += " AND a.status=?"
                params.append(str(status))
            sql += " AND a.visibility IN ('ready','active')"
            if kind:
                sql += " AND a.kind=?"
                params.append(str(kind))
            sql += " ORDER BY _rank ASC, a.created_at ASC, a.atom_id ASC"
            try:
                with self._connection() as conn:
                    rows = conn.execute(sql, params).fetchall()
                results: list[MemoryAtom] = []
                for row in rows:
                    atom = self._row_to_atom(row)
                    if self._atom_visible_to_scope(atom, resolved):
                        results.append(atom)
                        if len(results) >= requested_limit:
                            break
                return results
            except (sqlite3.DatabaseError, ValueError, KeyError):
                # Continue through the portable fallback below.  In
                # particular, stale FTS shadow tables should never hide a
                # valid canonical atom.
                pass

        candidates = self.list_atoms(scope=resolved, status=status)
        if kind:
            candidates = [atom for atom in candidates if atom.kind == str(kind)]
        lowered_terms = [part.casefold() for part in text.split() if part.strip()]
        scored: list[tuple[int, int, MemoryAtom]] = []
        for index, atom in enumerate(candidates):
            body = atom.body.casefold()
            occurrences = sum(body.count(term) for term in lowered_terms if term)
            if occurrences:
                scored.append((occurrences, index, atom))
        scored.sort(key=lambda item: (-item[0], item[1], item[2].atom_id))
        return [atom for _, _, atom in scored[:requested_limit]]

    def list_building_atoms(self, *, scope: MemoryReadScope | Mapping[str, Any] | None = None, **kwargs: Any) -> list[MemoryAtom]:
        kwargs["include_building"] = True
        kwargs["visibility"] = "building"
        return self.list_atoms(scope=scope, **kwargs)

    def update_atom(self, atom: MemoryAtom | Mapping[str, Any], **kwargs: Any) -> MemoryAtom:
        return self.put_atom(atom, **kwargs)

    edit = update_atom

    def _require_mutation_scope(
        self,
        scope: MemoryMutationScope | MemoryReadScope | Mapping[str, Any] | None,
        *,
        share_group_id: str = "",
    ) -> MemoryReadScope:
        if scope is None:
            raise PermissionError("explicit mutation scope required; include workspace_id and share_group_id")
        resolved = self._scope_from_args(scope, share_group_id=share_group_id)
        if resolved is None:
            raise PermissionError("explicit mutation scope required")
        return resolved

    def _delete_scoped(self, memory_id: str, scope: MemoryReadScope, *, reason: str = "") -> MemoryAtom:
        atom = self._get_atom_scoped(memory_id, scope, include_building=True)
        if atom is None:
            raise KeyError(memory_id)
        atom.status = "deleted"  # tombstone; body/history stays recoverable
        atom.metadata = {**atom.metadata, "tombstone_reason": reason}
        atom.updated_at = _now()
        return self._put_atom_impl(atom, visibility=atom.visibility)

    def delete(
        self,
        memory_id: str,
        *,
        scope: MemoryMutationScope | MemoryReadScope | Mapping[str, Any] | None = None,
        share_group_id: str = "",
        reason: str = "",
        context: Any | None = None,
    ) -> MemoryAtom:
        if context is not None:
            if scope is not None:
                raise ValueError("provide either context or scope, not both")
            scope = context
        resolved = self._require_mutation_scope(scope, share_group_id=share_group_id)
        return self._delete_scoped(memory_id, resolved, reason=reason)

    tombstone = delete

    def restore(
        self,
        memory_id: str,
        *,
        scope: MemoryMutationScope | MemoryReadScope | Mapping[str, Any] | None = None,
        share_group_id: str = "",
        reason: str = "",
        context: Any | None = None,
    ) -> tuple[MemoryAtom, list[MemoryAtom]]:
        """Restore one logical atom and shadow its active descendants atomically.

        Restore is a memory-domain lifecycle mutation, not a partial organizer
        update.  The target and every active descendant in its supersession
        graph receive revisions and local outbox events in one SQLite
        transaction.  GovernanceV2 owns the durable request/receipt fence
        around this method.
        """

        if self.readonly:
            raise PermissionError("memory store is read-only")
        if context is not None:
            if scope is not None:
                raise ValueError("provide either context or scope, not both")
            scope = context
        resolved = self._require_mutation_scope(scope, share_group_id=share_group_id)
        mutation_context = context if context is not None else resolved
        initial = self._get_atom_scoped(str(memory_id), resolved, include_building=True)
        if initial is None:
            raise KeyError(memory_id)

        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT * FROM atoms WHERE workspace_id=? AND share_group_id=?",
                    (resolved.workspace_id, resolved.share_group_id),
                ).fetchall()
                atoms = {
                    str(row["atom_id"]): self._row_to_atom(row)
                    for row in rows
                }
                target = atoms.get(initial.atom_id)
                if target is None or not self._atom_visible_to_scope(target, resolved):
                    raise KeyError(memory_id)

                edges = conn.execute(
                    "SELECT e.old_atom_id,e.new_atom_id "
                    "FROM supersession_edges e "
                    "JOIN atoms old ON old.atom_id=e.old_atom_id "
                    "JOIN atoms new ON new.atom_id=e.new_atom_id "
                    "WHERE old.workspace_id=? AND old.share_group_id=? "
                    "AND new.workspace_id=? AND new.share_group_id=?",
                    (
                        resolved.workspace_id,
                        resolved.share_group_id,
                        resolved.workspace_id,
                        resolved.share_group_id,
                    ),
                ).fetchall()
                descendants_by_id: dict[str, list[str]] = {}
                for row in edges:
                    descendants_by_id.setdefault(str(row["old_atom_id"]), []).append(str(row["new_atom_id"]))
                pending = list(descendants_by_id.get(target.atom_id, ()))
                descendant_ids: list[str] = []
                seen = {target.atom_id}
                while pending:
                    atom_id = pending.pop(0)
                    if atom_id in seen:
                        continue
                    seen.add(atom_id)
                    if atom_id in atoms and self._atom_visible_to_scope(atoms[atom_id], resolved):
                        descendant_ids.append(atom_id)
                        pending.extend(descendants_by_id.get(atom_id, ()))

                changed: list[tuple[MemoryAtom, str]] = []
                if target.status != "active" or not target.locked:
                    target.status = "active"
                    target.locked = True
                    changed.append((target, ""))
                for atom_id in descendant_ids:
                    descendant = atoms[atom_id]
                    if descendant.status != "active":
                        continue
                    descendant.status = "shadowed"
                    descendant.metadata = {
                        **dict(descendant.metadata),
                        "shadowed_by_restore": target.memory_id,
                    }
                    changed.append((descendant, target.memory_id))

                self._migration_state.conn = conn
                self._migration_state.sequence = None
                persisted: dict[str, MemoryAtom] = {}
                try:
                    for item, shadowed_by in changed:
                        before_revision = int(item.revision)
                        item.revision = before_revision + 1
                        item.updated_at = _now()
                        saved = self._put_atom_impl(item, context=mutation_context)
                        persisted[saved.atom_id] = saved
                        self._queue_event(
                            conn,
                            "atom.restore",
                            saved.atom_id,
                            {
                                "operation": "restore",
                                "memory_id": saved.memory_id,
                                "revision": saved.revision,
                                "status": saved.status,
                                "shadowed_by": shadowed_by,
                                "reason": str(reason or ""),
                            },
                        )
                finally:
                    self._migration_state.conn = None
                    self._migration_state.sequence = None

                restored = persisted.get(target.atom_id, target)
                shadowed = [
                    persisted[atom_id]
                    for atom_id in descendant_ids
                    if atom_id in persisted and persisted[atom_id].status == "shadowed"
                ]
                return restored, shadowed
        finally:
            conn.close()

    def _supersede_scoped(self, old: str, new: str, scope: MemoryReadScope, *, reason: str = "", source_ref: str = "") -> None:
        old_atom = self._get_atom_scoped(old, scope, include_building=True, atom_id=old if old and len(old) > 30 else "")
        if old_atom is None:
            old_atom = self._get_atom_scoped(old, scope, include_building=True)
        new_atom = self._get_atom_scoped(new, scope, include_building=True, atom_id=new if new and len(new) > 30 else "")
        if new_atom is None:
            new_atom = self._get_atom_scoped(new, scope, include_building=True)
        if old_atom is None or new_atom is None:
            raise KeyError("supersession atom not found")
        migration_conn = getattr(self._migration_state, "conn", None)
        conn = migration_conn or self._checked_connect(readonly=False)
        try:
            with (nullcontext(conn) if migration_conn is not None else transaction(conn)):
                edge_id = stable_digest({"old": old_atom.atom_id, "new": new_atom.atom_id})
                # A repeated supersede is an identical no-op.  This also
                # prevents adding duplicate revisions/deltas on retries.
                if conn.execute("SELECT 1 FROM supersession_edges WHERE old_atom_id=? AND new_atom_id=?", (old_atom.atom_id, new_atom.atom_id)).fetchone() is not None:
                    return
                now = _now()
                old_before = int(old_atom.revision)
                new_before = int(new_atom.revision)
                old_atom.status = "superseded"
                old_atom.revision = old_before + 1
                old_atom.updated_at = now
                if old_atom.memory_id not in new_atom.supersedes:
                    new_atom.supersedes.append(old_atom.memory_id)
                new_atom.revision = new_before + 1
                new_atom.updated_at = now
                # Keep both facts, their revision ledgers, deltas, and the
                # projection event in one memory-domain transaction.
                for item, before in ((old_atom, old_before), (new_atom, new_before)):
                    item = self._coerce_atom(item)
                    self._write_atom_row(conn, item)
                    rev_digest = stable_digest({"body": item.body, "status": item.status, "canonical_hash": item.canonical_hash, "metadata": item.metadata})
                    rev_id = stable_digest({"atom_id": item.atom_id, "revision": item.revision})
                    conn.execute(
                        "INSERT INTO atom_revisions(revision_id,atom_id,revision,body,status,canonical_hash,revision_digest,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(atom_id,revision) DO UPDATE SET body=excluded.body,status=excluded.status,canonical_hash=excluded.canonical_hash,revision_digest=excluded.revision_digest,metadata_json=excluded.metadata_json",
                        (rev_id, item.atom_id, item.revision, item.body, item.status, item.canonical_hash, rev_digest, _json(item.metadata), item.updated_at),
                    )
                    delta = {"operation": "supersede", "status": item.status, "supersedes": list(item.supersedes), "canonical_hash": item.canonical_hash, "metadata": dict(item.metadata)}
                    delta_id = stable_digest({"atom_id": item.atom_id, "from": before, "to": item.revision})
                    conn.execute(
                        "INSERT INTO atom_deltas(delta_id,atom_id,from_revision,to_revision,delta_json,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(atom_id,from_revision,to_revision) DO UPDATE SET delta_json=excluded.delta_json",
                        (delta_id, item.atom_id, before, item.revision, _json(delta), item.updated_at),
                    )
                    self._queue_event(conn, "atom.supersede", item.atom_id, {"operation": "supersede", "revision": item.revision, "status": item.status, "supersedes": list(item.supersedes), "reason": reason, "source_ref": source_ref})
                conn.execute("INSERT INTO supersession_edges(edge_id,old_atom_id,new_atom_id,reason,source_ref,created_at) VALUES(?,?,?,?,?,?)", (edge_id, old_atom.atom_id, new_atom.atom_id, reason, source_ref, now))
        finally:
            if migration_conn is None:
                conn.close()

    def supersede(
        self,
        old: str,
        new: str,
        *,
        scope: MemoryMutationScope | MemoryReadScope | Mapping[str, Any] | None = None,
        share_group_id: str = "",
        reason: str = "",
        source_ref: str = "",
        context: Any | None = None,
    ) -> None:
        if context is not None:
            if scope is not None:
                raise ValueError("provide either context or scope, not both")
            scope = context
        resolved = self._require_mutation_scope(scope, share_group_id=share_group_id)
        self._supersede_scoped(old, new, resolved, reason=reason, source_ref=source_ref)

    def _delete_for_migration(self, memory_id: str, *, share_group_id: str, reason: str = "", capability: object) -> MemoryAtom:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        scope = MemoryReadScope(share_group_id=str(share_group_id), workspace_id=str(self.layout.workspace))
        return self._delete_scoped(memory_id, scope, reason=reason)

    def _put_for_migration(
        self,
        atom: MemoryAtom | Mapping[str, Any],
        *,
        evidence: Sequence[str | Mapping[str, Any]] | None = None,
        evidence_ids: Sequence[str] | None = None,
        source_mappings: Sequence[Mapping[str, Any]] | None = None,
        capability: object,
    ) -> MemoryAtom:
        """Migration-only atom writer guarded by an identity capability."""

        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        return self._put_atom_impl(atom, evidence=evidence, evidence_ids=evidence_ids, source_mappings=source_mappings)

    def _supersede_for_migration(self, old: str, new: str, *, share_group_id: str, reason: str = "", source_ref: str = "", capability: object) -> None:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        scope = MemoryReadScope(share_group_id=str(share_group_id), workspace_id=str(self.layout.workspace))
        self._supersede_scoped(old, new, scope, reason=reason, source_ref=source_ref)

    def _write_atom_row(self, conn: sqlite3.Connection, item: MemoryAtom) -> None:
        conn.execute("UPDATE atoms SET status=?,supersedes_json=?,revision=?,updated_at=?,visibility=? WHERE atom_id=?", (item.status,_json(item.supersedes),item.revision,item.updated_at,item.visibility,item.atom_id))

    def _upsert_atom_row(self, conn: sqlite3.Connection, item: MemoryAtom) -> None:
        conn.execute(
            "INSERT INTO atoms(atom_id,memory_id,body,kind,status,confidence,locked,injection_policy,priority,canonical_hash,dedup_domain,supersedes_json,provenance_json,metadata_json,revision,visibility,created_at,updated_at,workspace_id,agent_instance_id,share_group_id,project_ref,provider,runtime_role) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(atom_id) DO UPDATE SET memory_id=excluded.memory_id,body=excluded.body,kind=excluded.kind,status=excluded.status,confidence=excluded.confidence,locked=excluded.locked,injection_policy=excluded.injection_policy,priority=excluded.priority,canonical_hash=excluded.canonical_hash,dedup_domain=excluded.dedup_domain,supersedes_json=excluded.supersedes_json,provenance_json=excluded.provenance_json,metadata_json=excluded.metadata_json,revision=excluded.revision,visibility=excluded.visibility,updated_at=excluded.updated_at,workspace_id=excluded.workspace_id,agent_instance_id=excluded.agent_instance_id,share_group_id=excluded.share_group_id,project_ref=excluded.project_ref,provider=excluded.provider,runtime_role=excluded.runtime_role",
            (item.atom_id,item.memory_id,item.body,item.kind,item.status,item.confidence,1 if item.locked else 0,item.injection_policy,item.priority,item.canonical_hash,item.dedup_domain,_json(item.supersedes),_json(item.provenance),_json(item.metadata),item.revision,item.visibility,item.created_at,item.updated_at,item.workspace_id,item.agent_instance_id,item.share_group_id,item.project_ref,item.provider,item.runtime_role),
        )

    def _write_scope_acl(
        self,
        conn: sqlite3.Connection,
        item: MemoryAtom,
        acl: Mapping[str, Any] | None = None,
        *,
        persist: bool = True,
    ) -> None:
        scope = {
            "workspace_id": item.workspace_id,
            "agent_instance_id": item.agent_instance_id,
            "share_group_id": item.share_group_id,
            "project_ref": item.project_ref,
            "provider": item.provider,
            "runtime_role": item.runtime_role,
            "effect": "allow",
        }
        if acl:
            scope.update({str(key): value for key, value in acl.items()})
        acl_id = stable_digest({"atom_id": item.atom_id, **scope})
        acl_metadata = _validate_metadata(
            {key: value for key, value in scope.items() if key not in {"workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role", "effect"}},
            label="ACL metadata",
        )
        if not persist:
            return
        conn.execute(
            "INSERT INTO scope_acl(acl_id,atom_id,workspace_id,agent_instance_id,share_group_id,project_ref,provider,runtime_role,effect,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(atom_id,workspace_id,agent_instance_id,share_group_id,project_ref,provider,runtime_role) DO UPDATE SET effect=excluded.effect,metadata_json=excluded.metadata_json",
            (acl_id, item.atom_id, str(scope.get("workspace_id") or ""), str(scope.get("agent_instance_id") or ""), str(scope.get("share_group_id") or ""), str(scope.get("project_ref") or ""), str(scope.get("provider") or ""), str(scope.get("runtime_role") or ""), str(scope.get("effect") or "allow"), _json(acl_metadata), item.updated_at),
        )

    def _write_source_mappings(
        self,
        conn: sqlite3.Connection,
        item: MemoryAtom,
        source_mappings: Sequence[Mapping[str, Any]] | None,
        *,
        persist: bool = True,
    ) -> None:
        for mapping in source_mappings or ():
            if not isinstance(mapping, Mapping):
                continue
            source_ref = str(mapping.get("source_ref") or "")
            if not source_ref:
                continue
            map_id = stable_digest({"atom_id": item.atom_id, "source_domain": mapping.get("source_domain", ""), "source_ref": source_ref, "source_record_id": mapping.get("source_record_id", "")})
            mapping_metadata = _validate_metadata(mapping.get("provenance") or mapping.get("metadata") or {}, label="source mapping metadata")
            if not persist:
                continue
            conn.execute(
                "INSERT INTO source_mappings(mapping_id,atom_id,source_domain,source_ref,source_record_id,source_revision,digest,provenance_json,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(atom_id,source_domain,source_ref,source_record_id) DO UPDATE SET source_revision=excluded.source_revision,digest=excluded.digest,provenance_json=excluded.provenance_json",
                (map_id, item.atom_id, str(mapping.get("source_domain") or ""), source_ref, str(mapping.get("source_record_id") or ""), str(mapping.get("source_revision") or mapping.get("revision") or ""), str(mapping.get("digest") or ""), _json(mapping_metadata), item.updated_at),
            )

    def _apply_staged_relationships(self, conn: sqlite3.Connection, item: MemoryAtom, delta: Mapping[str, Any] | None) -> None:
        if not isinstance(delta, Mapping) or ("staged_acl" not in delta and "staged_source_mappings" not in delta):
            return
        staged_acl = delta.get("staged_acl")
        self._write_scope_acl(conn, item, staged_acl if isinstance(staged_acl, Mapping) else None)
        staged_mappings = delta.get("staged_source_mappings")
        if isinstance(staged_mappings, Sequence) and not isinstance(staged_mappings, (str, bytes, bytearray)):
            self._write_source_mappings(conn, item, [item_map for item_map in staged_mappings if isinstance(item_map, Mapping)])

    def _apply_staged_revision(self, conn: sqlite3.Connection, atom_id: str, revision: int) -> None:
        """Promote a staged, already-evidenced revision onto the visible atom row."""

        atom_row = conn.execute("SELECT * FROM atoms WHERE atom_id=?", (atom_id,)).fetchone()
        if atom_row is None:
            return
        current = self._row_to_atom(atom_row)
        delta: Mapping[str, Any] | None = None
        delta_row = conn.execute(
            "SELECT delta_json FROM atom_deltas WHERE atom_id=? AND to_revision=?",
            (atom_id, int(revision)),
        ).fetchone()
        if delta_row is not None:
            try:
                loaded = json.loads(delta_row[0] or "{}")
            except (TypeError, ValueError):
                loaded = {}
            if isinstance(loaded, Mapping):
                delta = loaded
                if delta.get("atom_id") or delta.get("memory_id"):
                    staged = MemoryAtom.from_value(delta)
                    staged.atom_id = atom_id
                    staged.created_at = current.created_at or staged.created_at
                    staged.revision = int(revision)
                    persisted = self._coerce_atom(staged)
                    self._upsert_atom_row(conn, persisted)
                    self._apply_staged_relationships(conn, persisted, delta)
                    return
                if "body" in delta:
                    current.body = str(delta.get("body") or "")
                if "status" in delta:
                    current.status = str(delta.get("status") or current.status)
                if "confidence" in delta:
                    current.confidence = float(delta.get("confidence") if delta.get("confidence") is not None else current.confidence)
                if "locked" in delta:
                    current.locked = bool(delta.get("locked"))
                if "canonical_hash" in delta:
                    current.canonical_hash = str(delta.get("canonical_hash") or current.canonical_hash)
                if "metadata" in delta and isinstance(delta.get("metadata"), Mapping):
                    current.metadata = dict(delta.get("metadata") or {})
        rev_row = conn.execute(
            "SELECT body,status,canonical_hash,metadata_json FROM atom_revisions WHERE atom_id=? AND revision=?",
            (atom_id, int(revision)),
        ).fetchone()
        if rev_row is not None:
            current.body = str(rev_row[0] or "")
            current.status = str(rev_row[1] or current.status)
            current.canonical_hash = str(rev_row[2] or current.canonical_hash)
            try:
                revision_metadata = json.loads(rev_row[3] or "{}")
            except (TypeError, ValueError):
                revision_metadata = {}
            if isinstance(revision_metadata, Mapping):
                current.metadata = _validate_metadata(revision_metadata, label="atom revision metadata")
        current.revision = int(revision)
        current.updated_at = _now()
        persisted = self._coerce_atom(current)
        self._upsert_atom_row(conn, persisted)
        self._apply_staged_relationships(conn, persisted, delta)

    def _publish_evidenced_atoms(self, conn: sqlite3.Connection, events: Sequence[Mapping[str, Any]]) -> None:
        """Expose atoms whose evidence outbox for this aggregate is fully projected."""

        payloads_by_atom: dict[str, Mapping[str, Any]] = {}
        for event in events:
            if str(event.get("event_type") or "") != "evidence.put_link":
                continue
            atom_id = str(event.get("aggregate_id") or "")
            if not atom_id:
                continue
            payload = event.get("payload") or {}
            if isinstance(payload, Mapping):
                payloads_by_atom.setdefault(atom_id, payload)
        for atom_id in payloads_by_atom:
            pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM domain_outbox WHERE aggregate_id=? AND event_type='evidence.put_link' AND status IN ('pending','failed')",
                    (atom_id,),
                ).fetchone()[0]
            )
            if pending:
                continue
            row = conn.execute("SELECT * FROM atoms WHERE atom_id=?", (atom_id,)).fetchone()
            if row is None:
                continue
            max_rev = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM atom_revisions WHERE atom_id=?",
                    (atom_id,),
                ).fetchone()[0]
                or 0
            )
            current_rev = int(row["revision"] or 1)
            if max_rev > current_rev:
                self._apply_staged_revision(conn, atom_id, max_rev)
                row = conn.execute("SELECT * FROM atoms WHERE atom_id=?", (atom_id,)).fetchone()
                if row is None:
                    continue
            intended = str((payloads_by_atom.get(atom_id) or {}).get("publication_visibility") or "")
            current_vis = str(row["visibility"] or "")
            if intended in {"ready", "active"} and current_vis == "building":
                conn.execute(
                    "UPDATE atoms SET visibility=?,updated_at=? WHERE atom_id=?",
                    (intended, _now(), atom_id),
                )

    def set_visibility(self, visibility: str, *, atom_ids: Sequence[str] | None = None) -> int:
        if self.readonly:
            raise PermissionError("memory store is read-only")
        if visibility not in self.VISIBILITIES:
            raise ValueError(visibility)
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                if visibility in {"ready", "active"}:
                    pending = int(conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status IN ('pending','failed')").fetchone()[0])
                    if pending:
                        raise RuntimeError(f"cannot expose atoms while evidence outbox has {pending} outstanding event(s)")
                    if atom_ids:
                        placeholders = ",".join("?" for _ in atom_ids)
                        candidate_rows = conn.execute(f"SELECT atom_id FROM atoms WHERE atom_id IN ({placeholders})", tuple(atom_ids)).fetchall()
                    else:
                        candidate_rows = conn.execute("SELECT atom_id FROM atoms WHERE visibility IN ('building','ready')").fetchall()
                    missing = [str(row[0]) for row in candidate_rows if int(conn.execute("SELECT COUNT(*) FROM evidence_projection_receipts r JOIN domain_outbox o ON o.event_id=r.event_id WHERE o.aggregate_id=? AND o.event_type='evidence.put_link'", (str(row[0]),)).fetchone()[0]) == 0]
                    if missing:
                        raise RuntimeError("cannot expose atom(s) without projected evidence: " + ",".join(missing[:8]))
                if atom_ids:
                    placeholders = ",".join("?" for _ in atom_ids)
                    cur = conn.execute(f"UPDATE atoms SET visibility=?,updated_at=? WHERE atom_id IN ({placeholders})", (visibility,_now(),*atom_ids))
                else:
                    cur = conn.execute("UPDATE atoms SET visibility=?,updated_at=? WHERE visibility IN ('building','ready')", (visibility,_now()))
                if visibility in {"ready", "active"}:
                    conn.execute("UPDATE domain_state SET state=?,generation=generation+1,updated_at=? WHERE domain='memory'", (visibility.upper(),_now()))
                return int(cur.rowcount)
        finally:
            conn.close()

    promote = set_visibility

    def pending_outbox(self, *, limit: int | None = None, include_failed: bool = False) -> list[dict[str, Any]]:
        statuses = "('pending','failed')" if include_failed else "('pending')"
        query = f"SELECT * FROM domain_outbox WHERE status IN {statuses} ORDER BY sequence"
        params: list[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                raise ValueError("stored memory outbox payload is not valid JSON")
            payload = _validate_metadata_tree(payload, label="memory outbox payload")
            result.append({**dict(row), "payload": payload})
        return result

    def _mark_projected_batch(self, events: Sequence[Mapping[str, Any]], evidence_ids: Mapping[str, str]) -> int:
        """Commit memory receipts/statuses after evidence DB transaction."""
        if not events:
            return 0
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                now = _now()
                high_water = 0
                for event in events:
                    event_id = str(event["event_id"])
                    sequence = int(event["sequence"])
                    high_water = max(high_water, sequence)
                    if str(event.get("event_type") or "") == "evidence.put_link":
                        evidence_id = str(evidence_ids.get(event_id) or "")
                        if not evidence_id:
                            raise RuntimeError(f"evidence projector returned no evidence_id for {event_id}")
                        conn.execute(
                            "INSERT INTO evidence_projection_receipts(event_id,evidence_id,projected_at,error_json) VALUES(?,?,?,'{}') ON CONFLICT(event_id) DO UPDATE SET evidence_id=excluded.evidence_id,projected_at=excluded.projected_at,error_json='{}'",
                            (event_id, evidence_id, now),
                        )
                    conn.execute(
                        "UPDATE domain_outbox SET status='projected',projected_at=?,attempts=attempts+1,error_json='{}' WHERE event_id=?",
                        (now, event_id),
                    )
                if high_water:
                    conn.execute(
                        "UPDATE outbox_checkpoints SET last_sequence=?,updated_at=? WHERE domain='memory' AND last_sequence<?",
                        (high_water, now, high_water),
                    )
                self._publish_evidenced_atoms(conn, events)
            return len(events)
        finally:
            conn.close()

    def _mark_failed_batch(self, events: Sequence[Mapping[str, Any]], exc: BaseException) -> int:
        """Record failed evidence events without creating projection receipts."""
        if not events:
            return 0
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                error = _json({"type": type(exc).__name__, "message": str(exc)})
                for event in events:
                    conn.execute(
                        "UPDATE domain_outbox SET status='failed',attempts=attempts+1,error_json=? WHERE event_id=?",
                        (error, str(event["event_id"])),
                    )
            return len(events)
        finally:
            conn.close()

    def project_evidence(self, evidence_store: Any, *, limit: int | None = None) -> dict[str, int]:
        """Project pending memory outbox in bounded cross-database batches.

        Evidence transaction commits first.  Memory receipts/statuses commit in
        a second transaction, so a crash between them leaves pending events
        safely replayable against idempotent evidence rows.
        """
        # ``failed`` is an outstanding projection state, not a terminal one.
        # Evidence writes are idempotent, so a later invocation may safely
        # retry after a transient sink/schema failure has been corrected.
        events = self.pending_outbox(limit=limit, include_failed=True)
        projected = 0
        failed = 0
        batch_size = 100
        projector = getattr(evidence_store, "project_batch", None)
        if not callable(projector):
            raise RuntimeError("evidence projector requires project_batch API")
        for offset in range(0, len(events), batch_size):
            batch = events[offset : offset + batch_size]
            evidence_events = [event for event in batch if str(event.get("event_type") or "") == "evidence.put_link"]
            local_events = [event for event in batch if event not in evidence_events]
            if evidence_events:
                try:
                    evidence_ids = projector(evidence_events)
                except Exception as exc:
                    # Evidence transaction rolled back.  Mark only those rows
                    # failed; local memory events remain independently safe.
                    projected += self._mark_projected_batch(local_events, {})
                    failed += self._mark_failed_batch(evidence_events, exc)
                    continue
            else:
                evidence_ids = {}
            # If this transaction fails after evidence commit, leave memory
            # rows pending.  Next invocation replays evidence idempotently.
            projected += self._mark_projected_batch(batch, evidence_ids)
        return {"projected": projected, "failed": failed, "pending": len(self.pending_outbox(include_failed=True))}

    def evidence_ids_for_atom(self, atom_id: str) -> list[str]:
        with self._connection() as conn:
            rows = conn.execute("SELECT evidence_id FROM evidence_projection_receipts WHERE event_id IN (SELECT event_id FROM domain_outbox WHERE aggregate_id=? AND status='projected') ORDER BY evidence_id", (atom_id,)).fetchall()
        return [str(row[0]) for row in rows]

    def validate(self, evidence_store: Any | None = None, *, include_building: bool = True) -> MemoryValidation:
        atoms = self._list_atoms_unscoped(include_building=include_building)
        pending = self.pending_outbox(include_failed=True)
        errors: list[str] = []
        orphan = 0
        evidence_total = 0
        if evidence_store is not None:
            for atom in atoms:
                private_reader = getattr(evidence_store, "_list_for_subject_unscoped", None)
                links = private_reader("atom", atom.atom_id) if callable(private_reader) else evidence_store.list_for_subject("atom", atom.atom_id, scope={"workspace_id": str(self.layout.workspace), "subject_type": "atom", "subject_id": atom.atom_id})
                valid = [item for item in links if getattr(item, "status", "") == "valid"]
                evidence_total += len(valid)
                if not valid:
                    orphan += 1
                    errors.append(f"atom_without_valid_evidence:{atom.atom_id}")
        else:
            evidence_total = sum(len(self.evidence_ids_for_atom(atom.atom_id)) for atom in atoms)
        if pending:
            errors.append(f"outbox_not_drained:{len(pending)}")
        scope_rows = [atom.to_dict() | {"body": "", "provenance": []} for atom in atoms]
        scope_digest = stable_digest(sorted(scope_rows, key=lambda value: (value.get("share_group_id", ""), value.get("memory_id", ""), value.get("atom_id", ""))))
        return MemoryValidation(not errors and orphan == 0, len(atoms), evidence_total, orphan, len(pending), scope_digest, tuple(errors))

    def replay_revision(self, atom_id: str, revision: int | None = None) -> MemoryAtom | None:
        with self._connection() as conn:
            atom_row = conn.execute("SELECT * FROM atoms WHERE atom_id=?", (atom_id,)).fetchone()
            if atom_row is None:
                return None
            committed = int(atom_row["revision"] or 1)
            if str(atom_row["visibility"] or "") not in {"ready", "active"}:
                return None
            if revision is None:
                revision = committed
            elif int(revision) > committed or int(revision) < 1:
                return None
            row = conn.execute("SELECT * FROM atom_revisions WHERE atom_id=? AND revision=?", (atom_id, int(revision))).fetchone()
        if row is None:
            return None
        item = self._row_to_atom(atom_row)
        item.body = str(row["body"] or "")
        item.status = str(row["status"] or item.status)
        item.canonical_hash = str(row["canonical_hash"] or item.canonical_hash)
        item.revision = int(row["revision"])
        try:
            revision_metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            revision_metadata = {}
        item.metadata = _validate_metadata(revision_metadata, label="atom revision metadata")
        return item

    def revision_digest(self, atom_id: str, revision: int | None = None) -> str:
        item = self.replay_revision(atom_id, revision)
        if item is None:
            raise KeyError(atom_id)
        return stable_digest({"body": item.body, "status": item.status, "canonical_hash": item.canonical_hash, "metadata": item.metadata})

    def list_revisions(
        self,
        *,
        scope: MemoryReadScope | Mapping[str, Any] | None = None,
        memory_id: str = "",
        atom_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List redacted, scoped revision metadata without revision bodies.

        Revision history is append-only V2 state.  Keep this query explicitly
        scoped and select no body/metadata columns so GUI history cannot become
        a content read oracle.
        """
        resolved = self._scope_from_args(scope)
        if resolved is None:
            return []
        query = (
            "SELECT r.revision_id,r.atom_id,a.memory_id,r.revision,r.status,"
            "r.canonical_hash,r.revision_digest,r.created_at,a.share_group_id "
            "FROM atom_revisions r JOIN atoms a ON a.atom_id=r.atom_id "
            "WHERE r.revision <= a.revision AND a.visibility IN ('ready','active')"
        )
        params: list[Any] = []
        for column, value in (
            ("a.workspace_id", resolved.workspace_id),
            ("a.share_group_id", resolved.share_group_id),
            ("a.agent_instance_id", resolved.agent_instance_id),
            ("a.project_ref", resolved.project_ref),
            ("a.provider", resolved.provider),
            ("a.runtime_role", resolved.runtime_role),
        ):
            if resolved.admin and column not in {"a.workspace_id", "a.share_group_id"}:
                continue
            if column in {"a.workspace_id", "a.share_group_id"} or value:
                query += f" AND {column}=?"
                params.append(value)
        if memory_id:
            query += " AND a.memory_id=?"
            params.append(str(memory_id))
        if atom_id:
            query += " AND r.atom_id=?"
            params.append(str(atom_id))
        query += " ORDER BY r.created_at,r.atom_id,r.revision"
        bounded = max(1, min(int(limit or 100), 1000))
        query += " LIMIT ?"
        params.append(bounded)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "version_id": str(row["revision_id"]),
                "memory_id": str(row["memory_id"]),
                "atom_id": str(row["atom_id"]),
                "revision": int(row["revision"]),
                "status": str(row["status"] or ""),
                "canonical_hash": str(row["canonical_hash"] or ""),
                "revision_digest": str(row["revision_digest"] or ""),
                "created_at": str(row["created_at"] or ""),
                "share_group_id": str(row["share_group_id"] or ""),
            }
            for row in rows
        ]

    def supersede_chain(
        self,
        memory_id: str,
        *,
        scope: MemoryReadScope | Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return direct V2 supersession edges for one scoped memory ID.

        Only atom IDs and edge metadata leave the store.  Both endpoints must
        satisfy the same scope, preventing cross-group chain disclosure.
        """
        resolved = self._scope_from_args(scope)
        if resolved is None:
            return None
        atom = self.get_atom(str(memory_id), scope=resolved)
        if atom is None:
            return None
        predicates: list[str] = []
        params: list[Any] = []
        for alias in ("old", "new"):
            for column, value in (
                (f"{alias}.workspace_id", resolved.workspace_id),
                (f"{alias}.share_group_id", resolved.share_group_id),
                (f"{alias}.agent_instance_id", resolved.agent_instance_id),
                (f"{alias}.project_ref", resolved.project_ref),
                (f"{alias}.provider", resolved.provider),
                (f"{alias}.runtime_role", resolved.runtime_role),
            ):
                if resolved.admin and column.rsplit(".", 1)[-1] not in {"workspace_id", "share_group_id"}:
                    continue
                if column.rsplit(".", 1)[-1] in {"workspace_id", "share_group_id"} or value:
                    predicates.append(f"{column}=?")
                    params.append(value)
        query = (
            "SELECT e.old_atom_id,e.new_atom_id,old.memory_id AS old_memory_id,"
            "new.memory_id AS new_memory_id,e.reason,e.source_ref,e.created_at "
            "FROM supersession_edges e JOIN atoms old ON old.atom_id=e.old_atom_id "
            "JOIN atoms new ON new.atom_id=e.new_atom_id WHERE "
            + " AND ".join(predicates)
            + " AND (e.old_atom_id=? OR e.new_atom_id=?) ORDER BY e.created_at,e.edge_id"
        )
        params.extend([atom.atom_id, atom.atom_id])
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        supersedes = [str(row["old_memory_id"]) for row in rows if str(row["new_atom_id"]) == atom.atom_id]
        superseded_by = [str(row["new_memory_id"]) for row in rows if str(row["old_atom_id"]) == atom.atom_id]
        # Keep response shape stable and deterministic when duplicate edge
        # records are encountered in repaired/legacy databases.
        return {
            "memory_id": str(memory_id),
            "supersedes": list(dict.fromkeys(supersedes)),
            "superseded_by": list(dict.fromkeys(superseded_by)),
        }

    def list_source_mappings(self, *, atom_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM source_mappings"
        params: list[Any] = []
        if atom_id is not None:
            query += " WHERE atom_id=?"
            params.append(str(atom_id))
        query += " ORDER BY created_at, mapping_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                provenance = json.loads(row["provenance_json"] or "{}")
            except (TypeError, ValueError):
                raise ValueError("stored source mapping provenance is not valid JSON")
            result.append({**dict(row), "provenance": _validate_metadata(provenance, label="source mapping provenance")})
        return result

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            counts = {str(row[0]): int(row[1]) for row in conn.execute("SELECT visibility,COUNT(*) FROM atoms GROUP BY visibility")}
            revisions = int(conn.execute("SELECT COUNT(*) FROM atom_revisions").fetchone()[0])
            deltas = int(conn.execute("SELECT COUNT(*) FROM atom_deltas").fetchone()[0])
            pending = int(conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status='pending'").fetchone()[0])
        return {"atoms": sum(counts.values()), "visibility": counts, "revisions": revisions, "deltas": deltas, "outbox_pending": pending, "db_path": str(self.db_path)}


__all__ = ["MemoryAtom", "MemoryAtomStore", "MemoryReadScope", "MemoryMutationScope", "MutationScope", "MemoryValidation", "stable_digest"]
