"""Reference-only V2 evidence store.

``evidence.db`` is a projection target.  It contains no conversation/document
body columns; callers persist source references, revisions, digests and small
metadata only.  Writes are idempotent so a memory-domain outbox can safely be
replayed after a crash.
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
from typing import Any, Iterator, Mapping, Sequence

from ..storage.database import connect_database, open_database_snapshot
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import SCHEMA_MARKER as BASE_SCHEMA_MARKER
from ..storage.transaction import transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    if isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_for(value: str | Path | WorkspaceV2Layout, domain: str) -> Path:
    if isinstance(value, WorkspaceV2Layout):
        layout = value
        candidate = layout.evidence_db
    else:
        raw = Path(value).expanduser()
        if raw.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            candidate = Path(os.path.abspath(os.fspath(raw)))
            if candidate.parent.name != domain or candidate.parent.parent.name != WorkspaceV2Layout.ROOT_NAME:
                raise ValueError(f"{domain} database must be inside .memoryguard/{domain}")
            layout = _safe_layout(candidate.parent.parent.parent)
        else:
            layout = _safe_layout(raw)
            candidate = layout.evidence_db
    return layout.assert_database_path(candidate, domain)


def _safe_layout(root: str | Path) -> WorkspaceV2Layout:
    candidate = Path(os.path.abspath(os.fspath(root)))
    if candidate.exists() and WorkspaceV2Layout._is_reparse_or_symlink(candidate):
        raise ValueError(f"workspace cannot be a symlink or reparse point: {candidate}")
    return WorkspaceV2Layout(candidate)


@dataclass(frozen=True)
class Evidence:
    """A reference-only evidence record.

    ``metadata`` is intentionally bounded to JSON metadata.  It must not be
    used to smuggle source text; the store rejects common full-text keys.
    """

    evidence_id: str = ""
    source_ref: str = ""
    revision: str = ""
    digest: str = ""
    authority: str = "observed"
    status: str = "valid"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evidence_type: str = "reference"
    created_at: str = ""

    @property
    def source_revision(self) -> str:
        return self.revision

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_ref": self.source_ref,
            "revision": self.revision,
            "source_revision": self.revision,
            "digest": self.digest,
            "authority": self.authority,
            "status": self.status,
            "metadata": dict(self.metadata),
            "evidence_type": self.evidence_type,
            "created_at": self.created_at,
        }

    as_dict = to_dict

    @classmethod
    def from_value(cls, value: "Evidence | Mapping[str, Any]", **overrides: Any) -> "Evidence":
        if isinstance(value, Evidence):
            data = value.to_dict()
        else:
            data = dict(value)
        if "source_revision" in data and "revision" not in data:
            data["revision"] = data["source_revision"]
        data.update(overrides)
        return cls(
            evidence_id=str(data.get("evidence_id") or ""),
            source_ref=str(data.get("source_ref") or ""),
            revision=str(data.get("revision") or ""),
            digest=str(data.get("digest") or ""),
            authority=str(data.get("authority") or "observed"),
            status=str(data.get("status") or "valid"),
            metadata=dict(data.get("metadata") or {}),
            evidence_type=str(data.get("evidence_type") or "reference"),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass(frozen=True)
class EvidenceLink:
    link_id: str
    evidence_id: str
    subject_type: str
    subject_id: str
    relation: str = "supports"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "evidence_id": self.evidence_id,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "relation": self.relation,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class EvidenceReadScope:
    """Least-privilege scope for evidence reads.

    Evidence rows contain source references and metadata, so an evidence ID is
    not sufficient authorization on its own.  Callers must identify the
    workspace and the exact subject link they are allowed to inspect.
    """

    workspace_id: str
    subject_type: str
    subject_id: str

    def __post_init__(self) -> None:
        if not str(self.workspace_id):
            raise ValueError("evidence read scope requires workspace_id")
        if not str(self.subject_type) or not str(self.subject_id):
            raise ValueError("evidence read scope requires subject_type and subject_id")

    @classmethod
    def from_value(cls, value: "EvidenceReadScope | Mapping[str, Any]") -> "EvidenceReadScope":
        if isinstance(value, cls):
            return value
        return cls(
            workspace_id=str(value.get("workspace_id") or ""),
            subject_type=str(value.get("subject_type") or value.get("subject_kind") or ""),
            subject_id=str(value.get("subject_id") or value.get("id") or ""),
        )


_FORBIDDEN_METADATA_KEYS = frozenset({
    "body", "raw", "raw_content", "content", "text", "full_text",
    "document", "document_body", "document_text", "conversation",
    "conversation_body", "full_transcript", "raw_transcript", "transcript",
    "raw_text", "source_text", "source_body", "original_content", "payload",
})

_ALLOWED_AUTHORITIES = frozenset({
    "observed", "legacy_record", "legacy_provenance", "legacy_governance_event",
    "rule_migration", "memory_migration", "content_migration", "governance",
    "audit", "system",
})


def validate_authority(authority: str | None) -> str:
    """Reject unknown evidence authorities before any durable mutation."""

    value = str(authority or "observed")
    if value not in _ALLOWED_AUTHORITIES:
        raise ValueError(f"unknown evidence authority: {value!r}")
    return value

# Module-private capability used by V1 migration adapters.  Public callers
# must use a V2MutationContext; a copied value/string cannot satisfy identity.
_MIGRATION_CAPABILITY = object()


def _walk_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ValueError("evidence metadata exceeds maximum nesting depth")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            text = str(key)
            if text.casefold() in _FORBIDDEN_METADATA_KEYS:
                raise ValueError("evidence metadata cannot contain source body fields: " + text)
            result[text] = _walk_metadata(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_walk_metadata(child, depth=depth + 1) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("evidence metadata contains non-JSON value")


class EvidenceStore:
    """SQLite evidence reference store.

    ``readonly=True`` opens an existing database through SQLite ``mode=ro``;
    missing paths fail instead of creating a new source.  Writable opens may
    create the target V2 database and its schema.
    """

    SCHEMA_VERSION = 1
    SCHEMA_MARKER = "memoryguard-v2-phase2-evidence"
    SCHEMA_META_TABLE = "evidence_schema_meta"
    ALLOWED_AUTHORITIES = _ALLOWED_AUTHORITIES
    VALID_STATUSES = frozenset({"valid", "stale", "superseded", "source_deleted", "invalidated"})
    validate_authority = staticmethod(validate_authority)

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
                if candidate.parent.name != "evidence" or candidate.parent.parent.name != WorkspaceV2Layout.ROOT_NAME:
                    raise ValueError("evidence database must be inside .memoryguard/evidence")
                self.layout = _safe_layout(candidate.parent.parent.parent)
            else:
                self.layout = _safe_layout(candidate)
        self.db_path = _path_for(path if path is not None else workspace_or_path, "evidence")
        self.path = self.db_path
        self.readonly = bool(readonly)
        if self.readonly or not initialize:
            if not self.db_path.is_file():
                raise FileNotFoundError(self.db_path)
            self._check_schema(readonly=True)
        else:
            # Preflight existing schema metadata through SQLite mode=ro before
            # any write-capable WAL connection exists.  Older SQLite builds
            # can otherwise alter the physical database image on close even
            # when the schema transaction itself rolls back.
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
        self.layout.assert_database_path(self.db_path, "evidence")
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
                    raise RuntimeError("evidence base schema metadata is missing")
                return
            rows = conn.execute(
                "SELECT domain, version, marker FROM schema_meta"
            ).fetchall()
            if len(rows) != 1 or str(rows[0][0]) != "evidence":
                raise RuntimeError("unsupported evidence base schema metadata")
            version = int(rows[0][1])
            marker = str(rows[0][2])
            if version != self.SCHEMA_VERSION:
                raise RuntimeError("unsupported evidence base schema version")
            if marker not in {BASE_SCHEMA_MARKER, self.SCHEMA_MARKER}:
                raise RuntimeError("unsupported evidence base schema marker")
            if self.SCHEMA_META_TABLE in tables:
                phase_rows = conn.execute(
                    f"SELECT domain, version, marker FROM {self.SCHEMA_META_TABLE}"
                ).fetchall()
                if len(phase_rows) != 1 or str(phase_rows[0][0]) != "evidence":
                    raise RuntimeError("unsupported evidence phase2 schema metadata")
                if int(phase_rows[0][1]) != self.SCHEMA_VERSION or str(phase_rows[0][2]) != self.SCHEMA_MARKER:
                    raise RuntimeError("unsupported evidence phase2 schema metadata")

    def _init_schema(self) -> None:
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                self._create_schema(conn)
        finally:
            conn.close()

    @classmethod
    def _create_schema(cls, conn: sqlite3.Connection) -> None:
        # Do not use executescript: a caller may own the surrounding
        # transaction and must be able to roll this DDL back.
        statements = (
            "CREATE TABLE IF NOT EXISTS schema_meta (domain TEXT PRIMARY KEY, version INTEGER NOT NULL, marker TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS evidence_schema_meta (domain TEXT PRIMARY KEY, version INTEGER NOT NULL, marker TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS evidence (evidence_id TEXT PRIMARY KEY, evidence_type TEXT NOT NULL DEFAULT 'reference', source_ref TEXT NOT NULL, revision TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL, authority TEXT NOT NULL DEFAULT 'observed', status TEXT NOT NULL DEFAULT 'valid' CHECK(status IN ('valid','stale','superseded','source_deleted','invalidated')), metadata_json TEXT NOT NULL DEFAULT '{}', observed_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS evidence_links (link_id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'supports', metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(evidence_id, subject_type, subject_id, relation), FOREIGN KEY(evidence_id) REFERENCES evidence(evidence_id) ON DELETE CASCADE)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_links_subject ON evidence_links(subject_type, subject_id)",
            "CREATE TABLE IF NOT EXISTS migration_map (map_id TEXT PRIMARY KEY, source_domain TEXT NOT NULL, source_ref TEXT NOT NULL, source_id TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, UNIQUE(source_domain, source_ref, source_id, target_type, target_id))",
            "CREATE INDEX IF NOT EXISTS idx_evidence_migration_source ON migration_map(source_domain, source_ref, source_id)",
            "CREATE TABLE IF NOT EXISTS domain_outbox (event_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL UNIQUE, event_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'projected' CHECK(status IN ('pending','projected','failed')), attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, projected_at TEXT NOT NULL DEFAULT '', error_json TEXT NOT NULL DEFAULT '{}')",
            "CREATE TABLE IF NOT EXISTS outbox_checkpoints (domain TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS audit_refs (audit_id TEXT PRIMARY KEY, source_ref TEXT NOT NULL, digest TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)",
            # Keep shared Phase-1 schema marker intact.  Repair databases
            # produced by the early shadow build, which put Phase-2 marker in
            # this table; unknown markers stay untouched and fail validation.
            "INSERT INTO schema_meta(domain, version, marker, updated_at) VALUES('evidence', 1, 'memoryguard-v2-phase1', ?) ON CONFLICT(domain) DO NOTHING",
            "INSERT INTO evidence_schema_meta(domain, version, marker, updated_at) VALUES('evidence', 1, 'memoryguard-v2-phase2-evidence', ?) ON CONFLICT(domain) DO NOTHING",
            "INSERT INTO outbox_checkpoints(domain, last_sequence, updated_at) VALUES('evidence', 0, ?) ON CONFLICT(domain) DO NOTHING",
        )
        now = _now()
        for index, statement in enumerate(statements):
            conn.execute(statement, (now,) if index in {len(statements) - 3, len(statements) - 2, len(statements) - 1} else ())
        # Validate shared Phase-1 metadata before allowing any write.  Repair
        # only known version-1 early shadow markers; future/unknown metadata
        # fails closed and transaction rollback keeps it byte-for-byte safe.
        base_rows = conn.execute("SELECT domain, version, marker FROM schema_meta").fetchall()
        if len(base_rows) != 1 or str(base_rows[0][0]) != "evidence":
            raise RuntimeError("unsupported evidence base schema metadata")
        base_version, base_marker = int(base_rows[0][1]), str(base_rows[0][2])
        if base_version != cls.SCHEMA_VERSION:
            raise RuntimeError("unsupported evidence base schema version")
        if base_marker == "memoryguard-v2-phase2-evidence":
            conn.execute("UPDATE schema_meta SET marker=?, updated_at=? WHERE domain='evidence'", (BASE_SCHEMA_MARKER, now))
        elif base_marker != BASE_SCHEMA_MARKER:
            raise RuntimeError("unsupported evidence base schema marker")
        phase_row = conn.execute("SELECT version, marker FROM evidence_schema_meta WHERE domain='evidence'").fetchone()
        if phase_row is None or int(phase_row[0]) != cls.SCHEMA_VERSION or str(phase_row[1]) != cls.SCHEMA_MARKER:
            raise RuntimeError("unsupported evidence phase2 schema metadata")
        # Phase-1 shipped ``source_revision`` and no link metadata column.
        # Preserve those rows while widening the tables for Phase-2 callers;
        # this migration is local to the evidence domain and remains inside
        # the caller's explicit transaction.
        evidence_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(evidence)")}
        if "evidence_type" not in evidence_columns:
            conn.execute("ALTER TABLE evidence ADD COLUMN evidence_type TEXT NOT NULL DEFAULT 'reference'")
        if "created_at" not in evidence_columns:
            conn.execute("ALTER TABLE evidence ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            if "observed_at" in evidence_columns:
                conn.execute("UPDATE evidence SET created_at=observed_at WHERE created_at='' ")
        if "observed_at" not in evidence_columns:
            conn.execute("ALTER TABLE evidence ADD COLUMN observed_at TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE evidence SET observed_at=created_at WHERE observed_at='' ")
        if "revision" not in evidence_columns:
            conn.execute("ALTER TABLE evidence ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
            if "source_revision" in evidence_columns:
                conn.execute("UPDATE evidence SET revision=source_revision WHERE revision=''")
        link_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(evidence_links)")}
        if "metadata_json" not in link_columns:
            conn.execute("ALTER TABLE evidence_links ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        authorities = {str(item[0]) for item in conn.execute("SELECT DISTINCT authority FROM evidence")}
        unknown = sorted(authorities - _ALLOWED_AUTHORITIES)
        if unknown:
            raise RuntimeError("unknown evidence authority: " + ",".join(unknown))

    @classmethod
    def _check_schema_connection(cls, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT version, marker FROM evidence_schema_meta WHERE domain='evidence'").fetchone()
        if row is None or int(row[0]) != cls.SCHEMA_VERSION or str(row[1]) != cls.SCHEMA_MARKER:
            raise RuntimeError("unsupported evidence schema")
        authorities = {str(item[0]) for item in conn.execute("SELECT DISTINCT authority FROM evidence")}
        unknown = sorted(authorities - _ALLOWED_AUTHORITIES)
        if unknown:
            raise RuntimeError("unknown evidence authority: " + ",".join(unknown))

    def _check_schema(self, *, readonly: bool = False) -> None:
        with self._connection() as conn:
            self._check_schema_connection(conn)

    @staticmethod
    def _validate_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("evidence metadata must be a JSON object")
        metadata = _walk_metadata(value)
        encoded = _json(metadata)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("evidence metadata exceeds 64 KiB")
        return metadata

    def _authorize_v2_context(self, context: Any) -> Any:
        if context is None:
            return None
        from ..governance_v2.context import V2MutationContext

        resolved = V2MutationContext.from_value(context)
        resolved.check_workspace(self.layout.workspace)
        return resolved

    @staticmethod
    def _next_sequence(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM domain_outbox").fetchone()
        return int(row[0])

    def _queue_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        aggregate_id: str,
        payload: Mapping[str, Any],
        *,
        event_id: str = "",
        _sequence: list[int] | None = None,
        _schema: str = "main",
    ) -> str:
        """Record an immutable evidence-domain event in the same transaction.

        Evidence is the projection target, so these local events are marked
        ``projected`` immediately.  The event identity is derived solely from
        the durable source/evidence/link identity and payload, never from a
        migration run or wall-clock value.
        """

        checked = _walk_metadata(dict(payload))
        if not isinstance(checked, Mapping):
            raise ValueError("evidence outbox payload must be a JSON object")
        event_id = event_id or _digest({"event_type": event_type, "aggregate_id": aggregate_id, "payload": checked})
        now = _now()
        if _schema not in {"main", "publication_evidence"}:
            raise ValueError("invalid evidence schema")
        existing_event = conn.execute(f"SELECT sequence FROM {_schema}.domain_outbox WHERE event_id=?", (event_id,)).fetchone()
        if existing_event is not None:
            sequence = int(existing_event[0])
        elif _sequence is None:
            sequence = int(conn.execute(f"SELECT COALESCE(MAX(sequence),0)+1 FROM {_schema}.domain_outbox").fetchone()[0])
        else:
            _sequence[0] += 1
            sequence = int(_sequence[0])
        conn.execute(
            f"INSERT INTO {_schema}.domain_outbox(event_id,sequence,event_type,aggregate_id,payload_json,status,attempts,created_at,projected_at) VALUES(?,?,?,?,?,'projected',1,?,?) ON CONFLICT(event_id) DO NOTHING",
            (event_id, sequence, str(event_type), str(aggregate_id), _json(checked), now, now),
        )
        row = conn.execute(f"SELECT sequence FROM {_schema}.domain_outbox WHERE event_id=?", (event_id,)).fetchone()
        if row is not None:
            conn.execute(
                f"UPDATE {_schema}.outbox_checkpoints SET last_sequence=?,updated_at=? WHERE domain='evidence' AND last_sequence<?",
                (int(row[0]), now, int(row[0])),
            )
        return event_id

    @staticmethod
    def _coerce_evidence(value: Evidence | Mapping[str, Any], **overrides: Any) -> Evidence:
        evidence = Evidence.from_value(value, **overrides)
        if not evidence.source_ref:
            raise ValueError("evidence.source_ref is required")
        if not evidence.digest:
            evidence = Evidence.from_value(evidence, digest=_digest({"source_ref": evidence.source_ref, "revision": evidence.revision}))
        if not evidence.evidence_id:
            evidence = Evidence.from_value(evidence, evidence_id=_digest({
                "source_ref": evidence.source_ref,
                "revision": evidence.revision,
                "digest": evidence.digest,
                "authority": evidence.authority,
            }))
        if evidence.status not in EvidenceStore.VALID_STATUSES:
            raise ValueError(f"unsupported evidence status: {evidence.status!r}")
        return evidence

    def _project_event_on_connection(
        self,
        conn: sqlite3.Connection,
        event: Mapping[str, Any],
        sequence: list[int],
        *,
        schema: str = "main",
    ) -> str:
        """Apply one memory ``evidence.put_link`` event on borrowed connection.

        This deliberately mirrors public put/link immutability checks without
        opening nested connections.  Caller owns transaction and retries the
        whole batch after a crash.
        """

        payload = dict(event.get("payload") or {})
        evidence_value = payload.get("evidence") or payload
        if not isinstance(evidence_value, (Evidence, Mapping)):
            raise ValueError("outbox evidence payload must be an object")
        explicit_evidence_id = (
            bool(str(evidence_value.get("evidence_id") or ""))
            if isinstance(evidence_value, Mapping)
            else bool(str(getattr(evidence_value, "evidence_id", "") or ""))
        )
        item = self._coerce_evidence(evidence_value)
        validate_authority(item.authority)
        item = Evidence.from_value(item, created_at=item.created_at or _now())
        meta = self._validate_metadata(item.metadata)
        existing_before = conn.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (item.evidence_id,)).fetchone()
        conn.execute(
            "INSERT INTO evidence(evidence_id,evidence_type,source_ref,revision,digest,authority,status,metadata_json,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(evidence_id) DO NOTHING",
            (item.evidence_id, item.evidence_type, item.source_ref, item.revision, item.digest, item.authority, item.status, _json(meta), item.created_at, item.created_at),
        )
        existing = conn.execute(
            "SELECT evidence_id,evidence_type,source_ref,revision,digest,authority,status,metadata_json,observed_at,created_at FROM evidence WHERE evidence_id=?",
            (item.evidence_id,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("evidence insert did not produce a row")
        try:
            existing_metadata = json.loads(existing[7] or "{}")
        except (TypeError, ValueError):
            existing_metadata = None
        metadata_matches = existing_metadata == meta
        if (
            not metadata_matches
            and not explicit_evidence_id
            and isinstance(existing_metadata, Mapping)
        ):
            # Only mutable atom policy from legacy automatic events is
            # ignored; the existing evidence row is never rewritten.
            missing_policy = object()
            existing_policy = existing_metadata.get("injection_policy", missing_policy)
            incoming_policy = meta.get("injection_policy", missing_policy)
            policy_value_is_legacy = lambda value: (
                value is missing_policy
                or isinstance(value, str) and value in {"relevant", "always"}
            )
            existing_source_metadata = dict(existing_metadata)
            incoming_source_metadata = dict(meta)
            existing_source_metadata.pop("injection_policy", None)
            incoming_source_metadata.pop("injection_policy", None)
            metadata_matches = (
                existing_source_metadata == incoming_source_metadata
                and existing_policy != incoming_policy
                and policy_value_is_legacy(existing_policy)
                and policy_value_is_legacy(incoming_policy)
            )
        if not (
            str(existing[1]) == item.evidence_type
            and str(existing[2]) == item.source_ref
            and str(existing[3]) == item.revision
            and str(existing[4]) == item.digest
            and str(existing[5]) == item.authority
            and str(existing[6]) == item.status
            and metadata_matches
        ):
            raise ValueError(f"evidence_id conflict: {item.evidence_id}")
        if existing_before is None:
            self._queue_event(
                conn,
                "evidence.put",
                item.evidence_id,
                {
                    "evidence_id": item.evidence_id,
                    "evidence_type": item.evidence_type,
                    "source_ref": item.source_ref,
                    "revision": item.revision,
                    "digest": item.digest,
                    "authority": item.authority,
                    "status": item.status,
                    "metadata": meta,
                },
                _sequence=sequence,
                _schema=schema,
            )

        subject_type = str(payload.get("subject_type") or "atom")
        subject_id = str(payload.get("subject_id") or event.get("aggregate_id") or "")
        relation = str(payload.get("relation") or "supports")
        if not subject_type or not subject_id:
            raise ValueError("subject_type and subject_id are required")
        link_meta = self._validate_metadata(payload.get("link_metadata") or {})
        link_id = _digest({"evidence_id": item.evidence_id, "subject_type": subject_type, "subject_id": subject_id, "relation": relation})
        before_link = conn.execute(
            "SELECT metadata_json FROM evidence_links WHERE evidence_id=? AND subject_type=? AND subject_id=? AND relation=?",
            (item.evidence_id, subject_type, subject_id, relation),
        ).fetchone()
        now = _now()
        conn.execute(
            "INSERT INTO evidence_links(link_id,evidence_id,subject_type,subject_id,relation,metadata_json,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(evidence_id,subject_type,subject_id,relation) DO UPDATE SET metadata_json=excluded.metadata_json",
            (link_id, item.evidence_id, subject_type, subject_id, relation, _json(link_meta), now),
        )
        if before_link is None or str(before_link[0] or "{}") != _json(link_meta):
            self._queue_event(
                conn,
                "evidence.link",
                item.evidence_id,
                {
                    "evidence_id": item.evidence_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "relation": relation,
                    "metadata": link_meta,
                },
                _sequence=sequence,
                _schema=schema,
            )
        return item.evidence_id

    def project_batch(self, events: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        """Project bounded memory events in one evidence transaction.

        Evidence and link rows are immutable/idempotent.  No memory status is
        touched here; caller commits memory receipts only after this returns.
        """

        if self.readonly:
            raise PermissionError("evidence store is read-only")
        if not events:
            return {}
        from ..governance_v2.atomic import evidence_connection

        borrowed = evidence_connection(self.db_path)
        schema = "publication_evidence" if borrowed is not None else "main"
        conn = borrowed or self._checked_connect(readonly=False)
        try:
            row = conn.execute(f"SELECT COALESCE(MAX(sequence),0) FROM {schema}.domain_outbox").fetchone()
            sequence = [int(row[0] or 0)]
            result: dict[str, str] = {}
            with transaction(conn):
                for event in events:
                    event_id = str(event.get("event_id") or "")
                    if not event_id:
                        raise ValueError("memory evidence event_id is required")
                    result[event_id] = self._project_event_on_connection(conn, event, sequence, schema=schema)
            return result
        finally:
            conn.close()

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> Evidence:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return Evidence(
            evidence_id=str(row["evidence_id"]),
            evidence_type=str(row["evidence_type"] or "reference"),
            source_ref=str(row["source_ref"] or ""),
            revision=str(row["revision"] or ""),
            digest=str(row["digest"] or ""),
            authority=str(row["authority"] or "observed"),
            status=str(row["status"] or "valid"),
            metadata=metadata if isinstance(metadata, Mapping) else {},
            created_at=str(row["created_at"] or ""),
        )

    def _put_evidence_impl(
        self,
        evidence: Evidence | Mapping[str, Any] | None = None,
        *,
        evidence_id: str = "",
        source_ref: str = "",
        revision: str = "",
        source_revision: str | None = None,
        digest: str = "",
        authority: str = "observed",
        status: str = "valid",
        metadata: Mapping[str, Any] | None = None,
        evidence_type: str = "reference",
        context: Any | None = None,
    ) -> Evidence:
        if self.readonly:
            raise PermissionError("evidence store is read-only")
        self._authorize_v2_context(context)
        if evidence is None:
            evidence = {
                "evidence_id": evidence_id,
                "source_ref": source_ref,
                "revision": source_revision if source_revision is not None else revision,
                "digest": digest,
                "authority": authority,
                "status": status,
                "metadata": metadata or {},
                "evidence_type": evidence_type,
            }
        else:
            evidence = Evidence.from_value(evidence, evidence_id=evidence_id or None) if evidence_id else evidence
            if source_ref or revision or source_revision is not None or digest or authority != "observed" or status != "valid" or metadata is not None or evidence_type != "reference":
                evidence = Evidence.from_value(evidence, **{
                    key: value for key, value in {
                        "source_ref": source_ref or None,
                        "revision": source_revision if source_revision is not None else (revision or None),
                        "digest": digest or None,
                        "authority": authority if authority != "observed" else None,
                        "status": status if status != "valid" else None,
                        "metadata": metadata if metadata is not None else None,
                        "evidence_type": evidence_type if evidence_type != "reference" else None,
                    }.items() if value is not None
                })
        item = self._coerce_evidence(evidence)
        validate_authority(item.authority)
        item = Evidence.from_value(item, created_at=item.created_at or _now())
        meta = self._validate_metadata(item.metadata)
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                existing_before = conn.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (item.evidence_id,)).fetchone()
                conn.execute(
                    "INSERT INTO evidence(evidence_id,evidence_type,source_ref,revision,digest,authority,status,metadata_json,observed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(evidence_id) DO NOTHING",
                    (item.evidence_id, item.evidence_type, item.source_ref, item.revision, item.digest, item.authority, item.status, _json(meta), item.created_at, item.created_at),
                )
                existing = conn.execute(
                    "SELECT evidence_id,evidence_type,source_ref,revision,digest,authority,status,metadata_json,observed_at,created_at FROM evidence WHERE evidence_id=?",
                    (item.evidence_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("evidence insert did not produce a row")
                try:
                    existing_metadata = json.loads(existing[7] or "{}")
                except (TypeError, ValueError):
                    existing_metadata = None
                identical = (
                    str(existing[1]) == item.evidence_type
                    and str(existing[2]) == item.source_ref
                    and str(existing[3]) == item.revision
                    and str(existing[4]) == item.digest
                    and str(existing[5]) == item.authority
                    and str(existing[6]) == item.status
                    and existing_metadata == meta
                )
                if not identical:
                    raise ValueError(f"evidence_id conflict: {item.evidence_id}")
                if existing_before is None:
                    self._queue_event(
                        conn,
                        "evidence.put",
                        item.evidence_id,
                        {
                            "evidence_id": item.evidence_id,
                            "evidence_type": item.evidence_type,
                            "source_ref": item.source_ref,
                            "revision": item.revision,
                            "digest": item.digest,
                            "authority": item.authority,
                            "status": item.status,
                            "metadata": meta,
                        },
                    )
                return self._row_to_evidence(existing)
        finally:
            conn.close()

    def put_evidence(
        self,
        evidence: Evidence | Mapping[str, Any] | None = None,
        *,
        evidence_id: str = "",
        source_ref: str = "",
        revision: str = "",
        source_revision: str | None = None,
        digest: str = "",
        authority: str = "observed",
        status: str = "valid",
        metadata: Mapping[str, Any] | None = None,
        evidence_type: str = "reference",
        context: Any | None = None,
    ) -> Evidence:
        """Public governed evidence write; explicit context is mandatory."""

        if context is None:
            raise PermissionError("explicit V2 mutation context required")
        return self._put_evidence_impl(
            evidence,
            evidence_id=evidence_id,
            source_ref=source_ref,
            revision=revision,
            source_revision=source_revision,
            digest=digest,
            authority=authority,
            status=status,
            metadata=metadata,
            evidence_type=evidence_type,
            context=context,
        )

    def _put_evidence_for_migration(self, evidence: Evidence | Mapping[str, Any] | None = None, *, capability: object, **kwargs: Any) -> Evidence:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        return self._put_evidence_impl(evidence, **kwargs)

    def _resolve_read_scope(self, scope: EvidenceReadScope | Mapping[str, Any] | None) -> EvidenceReadScope | None:
        if scope is None:
            return None
        resolved = EvidenceReadScope.from_value(scope)
        requested = os.path.abspath(os.fspath(Path(resolved.workspace_id).expanduser()))
        current = os.path.abspath(os.fspath(self.layout.workspace))
        if requested != current:
            raise PermissionError("evidence read scope workspace_id does not match store workspace")
        return resolved

    @staticmethod
    def _scope_matches_subject(scope: EvidenceReadScope, subject_type: str, subject_id: str) -> bool:
        return str(scope.subject_type) == str(subject_type) and str(scope.subject_id) == str(subject_id)

    def _get_evidence_unscoped(self, evidence_id: str) -> Evidence | None:
        """Trusted domain-internal read; public callers must provide a scope."""

        with self._connection() as conn:
            row = conn.execute("SELECT * FROM evidence WHERE evidence_id=?", (str(evidence_id),)).fetchone()
        return self._row_to_evidence(row) if row is not None else None

    def get_evidence(
        self,
        evidence_id: str,
        *,
        scope: EvidenceReadScope | Mapping[str, Any] | None = None,
    ) -> Evidence | None:
        """Return evidence only when the requested subject link authorizes it.

        An unscoped or unauthorized lookup is deliberately existence-neutral;
        it does not reveal source references, metadata, or whether the ID is
        present at all.
        """

        resolved = self._resolve_read_scope(scope)
        if resolved is None:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT e.* FROM evidence e JOIN evidence_links l ON l.evidence_id=e.evidence_id "
                "WHERE e.evidence_id=? AND l.subject_type=? AND l.subject_id=? LIMIT 1",
                (str(evidence_id), resolved.subject_type, resolved.subject_id),
            ).fetchone()
        return self._row_to_evidence(row) if row is not None else None

    def _link_impl(
        self,
        evidence: Evidence | Mapping[str, Any] | str,
        subject_type: str,
        subject_id: str | None = None,
        relation: str = "supports",
        *,
        metadata: Mapping[str, Any] | None = None,
        link_id: str = "",
        context: Any | None = None,
    ) -> EvidenceLink:
        if self.readonly:
            raise PermissionError("evidence store is read-only")
        self._authorize_v2_context(context)
        if isinstance(evidence, str):
            evidence_id = evidence
        else:
            putter = self.put_evidence if context is not None else self._put_evidence_impl
            evidence_id = putter(evidence, context=context).evidence_id if context is not None else putter(evidence).evidence_id
        # Accept link(evidence_id, "subject-id") as a convenience; the typed
        # three-argument form remains unambiguous for domain callers.
        if subject_id is None:
            subject_id, subject_type = subject_type, "atom"
        if not subject_type or not subject_id:
            raise ValueError("subject_type and subject_id are required")
        meta = self._validate_metadata(metadata)
        now = _now()
        lid = link_id or _digest({"evidence_id": evidence_id, "subject_type": subject_type, "subject_id": subject_id, "relation": relation})
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                if conn.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone() is None:
                    raise KeyError(f"unknown evidence: {evidence_id}")
                before_link = conn.execute(
                    "SELECT metadata_json FROM evidence_links WHERE evidence_id=? AND subject_type=? AND subject_id=? AND relation=?",
                    (evidence_id, str(subject_type), str(subject_id), str(relation)),
                ).fetchone()
                conn.execute(
                    "INSERT INTO evidence_links(link_id,evidence_id,subject_type,subject_id,relation,metadata_json,created_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(evidence_id,subject_type,subject_id,relation) DO UPDATE SET metadata_json=excluded.metadata_json",
                    (lid, evidence_id, str(subject_type), str(subject_id), str(relation), _json(meta), now),
                )
                if before_link is None or str(before_link[0] or "{}") != _json(meta):
                    self._queue_event(
                        conn,
                        "evidence.link",
                        evidence_id,
                        {
                            "evidence_id": evidence_id,
                            "subject_type": str(subject_type),
                            "subject_id": str(subject_id),
                            "relation": str(relation),
                            "metadata": meta,
                        },
                    )
        finally:
            conn.close()
        return EvidenceLink(lid, evidence_id, str(subject_type), str(subject_id), str(relation), meta, now)

    def link(
        self,
        evidence: Evidence | Mapping[str, Any] | str,
        subject_type: str,
        subject_id: str | None = None,
        relation: str = "supports",
        *,
        metadata: Mapping[str, Any] | None = None,
        link_id: str = "",
        context: Any | None = None,
    ) -> EvidenceLink:
        """Public governed link write; explicit context is mandatory."""

        if context is None:
            raise PermissionError("explicit V2 mutation context required")
        return self._link_impl(
            evidence,
            subject_type,
            subject_id,
            relation,
            metadata=metadata,
            link_id=link_id,
            context=context,
        )

    def _link_for_migration(self, evidence: Evidence | Mapping[str, Any] | str, subject_type: str, subject_id: str | None = None, relation: str = "supports", *, capability: object, **kwargs: Any) -> EvidenceLink:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        return self._link_impl(evidence, subject_type, subject_id, relation, **kwargs)

    def _unlink_impl(
        self,
        evidence: str,
        subject_type: str,
        subject_id: str | None = None,
        relation: str = "supports",
        *,
        context: Any | None = None,
    ) -> int:
        """Remove one subject link under an explicit mutation context."""

        if self.readonly:
            raise PermissionError("evidence store is read-only")
        self._authorize_v2_context(context)
        if subject_id is None:
            subject_id, subject_type = subject_type, "atom"
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                cur = conn.execute(
                    "DELETE FROM evidence_links WHERE evidence_id=? AND subject_type=? AND subject_id=? AND relation=?",
                    (str(evidence), str(subject_type), str(subject_id), str(relation)),
                )
                if cur.rowcount:
                    self._queue_event(
                        conn,
                        "evidence.unlink",
                        str(evidence),
                        {
                            "evidence_id": str(evidence),
                            "subject_type": str(subject_type),
                            "subject_id": str(subject_id),
                            "relation": str(relation),
                        },
                    )
                return int(cur.rowcount)
        finally:
            conn.close()

    def unlink(
        self,
        evidence: str,
        subject_type: str,
        subject_id: str | None = None,
        relation: str = "supports",
        *,
        context: Any | None = None,
    ) -> int:
        """Public governed unlink write; explicit context is mandatory."""

        if context is None:
            raise PermissionError("explicit V2 mutation context required")
        return self._unlink_impl(evidence, subject_type, subject_id, relation, context=context)

    def _unlink_for_migration(self, evidence: str, subject_type: str, subject_id: str | None = None, relation: str = "supports", *, capability: object) -> int:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        return self._unlink_impl(evidence, subject_type, subject_id, relation)

    def _list_for_subject_unscoped(
        self,
        subject_type: str,
        subject_id: str,
        *,
        relation: str | None = None,
        include_invalid: bool = False,
    ) -> list[Evidence]:
        query = "SELECT e.* FROM evidence e JOIN evidence_links l ON l.evidence_id=e.evidence_id WHERE l.subject_type=? AND l.subject_id=?"
        params: list[Any] = [str(subject_type), str(subject_id)]
        if relation is not None:
            query += " AND l.relation=?"
            params.append(str(relation))
        if not include_invalid:
            query += " AND e.status='valid'"
        query += " ORDER BY e.created_at, e.evidence_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def list_for_subject(
        self,
        subject_type: str,
        subject_id: str | None = None,
        *,
        relation: str | None = None,
        include_invalid: bool = False,
        scope: EvidenceReadScope | Mapping[str, Any] | None = None,
    ) -> list[Evidence]:
        if subject_id is None:
            subject_id, subject_type = subject_type, "atom"
        resolved = self._resolve_read_scope(scope)
        if resolved is None or not self._scope_matches_subject(resolved, str(subject_type), str(subject_id)):
            return []
        return self._list_for_subject_unscoped(
            str(subject_type),
            str(subject_id),
            relation=relation,
            include_invalid=include_invalid,
        )

    def _list_links_for_subject_unscoped(self, subject_type: str, subject_id: str) -> list[EvidenceLink]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM evidence_links WHERE subject_type=? AND subject_id=? ORDER BY created_at, link_id",
                (str(subject_type), str(subject_id)),
            ).fetchall()
        result: list[EvidenceLink] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            result.append(EvidenceLink(str(row["link_id"]), str(row["evidence_id"]), str(row["subject_type"]), str(row["subject_id"]), str(row["relation"]), metadata if isinstance(metadata, Mapping) else {}, str(row["created_at"] or "")))
        return result

    def list_links_for_subject(
        self,
        subject_type: str,
        subject_id: str | None = None,
        *,
        scope: EvidenceReadScope | Mapping[str, Any] | None = None,
    ) -> list[EvidenceLink]:
        if subject_id is None:
            subject_id, subject_type = subject_type, "atom"
        resolved = self._resolve_read_scope(scope)
        if resolved is None or not self._scope_matches_subject(resolved, str(subject_type), str(subject_id)):
            return []
        return self._list_links_for_subject_unscoped(str(subject_type), str(subject_id))

    def record_migration_map(
        self,
        source_domain: str,
        source_ref: str,
        source_id: str,
        target_type: str,
        target_id: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        status: str | None = None,
    ) -> str:
        if self.readonly:
            raise PermissionError("evidence store is read-only")
        map_id = _digest({"source_domain": source_domain, "source_ref": source_ref, "source_id": source_id, "target_type": target_type, "target_id": target_id})
        meta = self._validate_metadata(metadata)
        if status is not None:
            if "status" in meta and str(meta["status"]) != str(status):
                raise ValueError("migration map status conflicts with metadata")
            meta["status"] = str(status)
        conn = self._checked_connect(readonly=False)
        try:
            with transaction(conn):
                # One source identity has one immutable target mapping.  Do
                # this check before the insert so a replay that changes the
                # target (or source reference) cannot silently create a
                # second mapping under a different derived map_id.
                source_rows = conn.execute(
                    "SELECT map_id FROM migration_map WHERE source_domain=? AND source_ref=? AND source_id=?",
                    (str(source_domain), str(source_ref), str(source_id)),
                ).fetchall()
                for source_row in source_rows:
                    if str(source_row[0]) != map_id:
                        raise ValueError(f"migration map conflict: {map_id}")
                existing_before = conn.execute("SELECT 1 FROM migration_map WHERE map_id=?", (map_id,)).fetchone()
                conn.execute(
                    "INSERT INTO migration_map(map_id,source_domain,source_ref,source_id,target_type,target_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(map_id) DO NOTHING",
                    (map_id, str(source_domain), str(source_ref), str(source_id), str(target_type), str(target_id), _json(meta), _now()),
                )
                existing = conn.execute(
                    "SELECT source_domain,source_ref,source_id,target_type,target_id,metadata_json FROM migration_map WHERE map_id=?",
                    (map_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("migration map insert did not produce a row")
                try:
                    existing_metadata = json.loads(existing[5] or "{}")
                except (TypeError, ValueError):
                    existing_metadata = None
                identical = (
                    str(existing[0]) == str(source_domain)
                    and str(existing[1]) == str(source_ref)
                    and str(existing[2]) == str(source_id)
                    and str(existing[3]) == str(target_type)
                    and str(existing[4]) == str(target_id)
                    and existing_metadata == meta
                )
                if not identical:
                    raise ValueError(f"migration map conflict: {map_id}")
                if existing_before is None:
                    self._queue_event(
                        conn,
                        "evidence.migration_map",
                        map_id,
                        {
                            "map_id": map_id,
                            "source_domain": str(source_domain),
                            "source_ref": str(source_ref),
                            "source_id": str(source_id),
                            "target_type": str(target_type),
                            "target_id": str(target_id),
                            "metadata": meta,
                        },
                    )
        finally:
            conn.close()
        return map_id

    def _record_migration_map_for_migration(self, source_domain: str, source_ref: str, source_id: str, target_type: str, target_id: str, *, capability: object, metadata: Mapping[str, Any] | None = None, status: str | None = None) -> str:
        if capability is not _MIGRATION_CAPABILITY:
            raise PermissionError("invalid migration mutation capability")
        return self.record_migration_map(source_domain, source_ref, source_id, target_type, target_id, metadata=metadata, status=status)

    def list_migration_map(self, *, source_domain: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM migration_map"
        params: list[Any] = []
        if source_domain is not None:
            query += " WHERE source_domain=?"
            params.append(str(source_domain))
        query += " ORDER BY created_at, map_id"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            result.append({**dict(row), "metadata": metadata})
        return result

    def status(self) -> dict[str, Any]:
        with self._connection() as conn:
            evidence_count = int(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0])
            link_count = int(conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0])
            migration_count = int(conn.execute("SELECT COUNT(*) FROM migration_map").fetchone()[0])
            outbox_events = int(conn.execute("SELECT COUNT(*) FROM domain_outbox").fetchone()[0])
            outbox_pending = int(conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status='pending'").fetchone()[0])
        return {"evidence": evidence_count, "links": link_count, "migration_map": migration_count, "outbox_events": outbox_events, "outbox_pending": outbox_pending, "db_path": str(self.db_path)}


__all__ = ["Evidence", "EvidenceLink", "EvidenceReadScope", "EvidenceStore"]
