"""Shadow V2 rule storage.

This module is deliberately independent from the legacy rule modules.  It owns
the ``.memoryguard/rules/rules.db`` staging database and exposes a small,
transactional API used by the rules migration reader.  Runtime callers must
not use this store until the V2 manifest explicitly enables it.

Evidence text is never persisted here.  Rule facts append a compact reference
to ``rule_evidence_outbox`` in the same SQLite transaction; an evidence
projector can consume those events in a separate database transaction.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

from ..governance_lock import WorkspaceGovernanceLock
from ..storage.database import connect_database
from ..rule_binding import RuleBinding, validate_binding_scope
from ..rule_definition import RuleDefinition, build_definition
from ..storage.layout import WorkspaceV2Layout


RULES_SCHEMA_VERSION = 2
RULES_SCHEMA_MARKER = "memoryguard-v2-phase2-rules"

# Classification is provenance, but historical reconciliation still needs one
# deterministic canonical head.  Keep this ordering shared with the legacy
# reconciliation adapter; it must be applied before confidence/revision/ID so
# a lexical ID cannot make an ordinary fact outrank a durable procedure.
_CANONICAL_RULE_KIND_PRIORITY = {
    "episode": 0,
    "fact": 1,
    "project": 2,
    "preference": 3,
    "procedure": 4,
    "correction": 5,
}

# ``RuleDefinition`` keeps modality as provenance, but modality also selects
# the runtime injection channel.  Automatic historical folds therefore leave
# equal/equivalent bodies on opposite channels in place.  An explicit merge
# may fold a relevant duplicate into a mandatory canonical head, never the
# reverse, and never by demoting the mandatory obligation.  Within one
# channel this ordering still selects the deterministic canonical head.
_CANONICAL_RULE_STRENGTH_PRIORITY = {
    "unknown": -1,
    "observation": 0,
    "suggestion": 1,
    "recommended": 2,
    "should": 3,
    "must": 4,
    "mandatory": 4,
    "required": 4,
}


def canonical_rule_kind_priority(value: Any) -> int:
    """Return the stable cross-kind canonicalization priority."""

    return int(_CANONICAL_RULE_KIND_PRIORITY.get(str(value or "").casefold(), 1))


def canonical_rule_strength_priority(value: Any) -> int:
    """Return the stable priority used for canonical strength selection."""

    return int(_CANONICAL_RULE_STRENGTH_PRIORITY.get(str(value or "").casefold(), 0))


def _canonical_rule_strength(value: Any) -> str:
    normalized = str(value or "").casefold().strip()
    if normalized in {"mandatory", "required"}:
        return "must"
    if normalized not in {
        "must", "should", "recommended", "suggestion", "observation",
    }:
        return "observation"
    return normalized


def _runtime_injection_layer(definition: RuleDefinition) -> str:
    """Map stored strength/maturity onto the runtime injection channel."""

    strength = _canonical_rule_strength(definition.rule_strength)
    maturity = str(definition.maturity_state or "").casefold()
    return "mandatory" if strength == "must" or maturity == "trusted" else "relevant"


def _canonical_definition_metadata(
    definitions: Sequence[RuleDefinition],
) -> dict[str, Any]:
    """Choose canonical metadata from all members of one safe topic fold.

    The old winner ordering preferred kind before strength, so a procedure
    carrying an observation could accidentally downgrade a MUST fact.  Keep
    the strongest modality first, then the most durable kind, then confidence
    and revision as deterministic tie-breakers.  These fields are metadata;
    semantic safety is still decided solely by the shared claim composer.
    """

    if not definitions:
        return {
            "rule_strength": "observation",
            "rule_kind": "fact",
            "confidence": 0.0,
            "maturity_state": "observing",
        }
    strength = max(
        definitions,
        key=lambda definition: (
            canonical_rule_strength_priority(definition.rule_strength),
            _canonical_rule_strength(definition.rule_strength),
        ),
    )
    kind = max(
        definitions,
        key=lambda definition: (
            canonical_rule_kind_priority(definition.rule_kind),
            str(definition.rule_kind or ""),
        ),
    )
    maturity_order = {
        "observing": 0,
        "candidate": 1,
        "validated": 2,
        "trusted": 3,
    }
    maturity = max(
        definitions,
        key=lambda definition: (
            maturity_order.get(str(definition.maturity_state or "").casefold(), 0),
            float(definition.confidence or 0.0),
            int(definition.revision or 0),
            str(definition.definition_id),
        ),
    )
    return {
        "rule_strength": _canonical_rule_strength(strength.rule_strength),
        "rule_kind": str(kind.rule_kind or "fact"),
        "confidence": max(float(definition.confidence or 0.0) for definition in definitions),
        "maturity_state": str(maturity.maturity_state or "observing"),
    }


def _compose_canonical_bodies(bodies: Sequence[str]) -> Any:
    """Call the shared claim composer at the V2 storage boundary."""

    from ..runtime_v2.canonical_claims import compose_canonical_bodies

    return compose_canonical_bodies([str(body or "") for body in bodies])


def _claims_share_canonical_topic(left: str, right: str) -> bool:
    """Use the canonical claim module's shared topic predicate."""

    from ..runtime_v2.canonical_claims import claims_related

    return bool(claims_related(str(left or ""), str(right or "")))


def _canonical_relation_kind(left: str, right: str) -> str:
    """Return the shared governance relation without making provenance a gate."""

    from ..runtime_v2.governance_semantics import classify_governance_relation

    return str(
        classify_governance_relation(str(left or ""), str(right or "")).kind
        or ""
    ).casefold()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_digest(value: Any) -> str:
    """Stable SHA-256 digest for JSON-compatible values."""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def canonical_migration_source_id(source_id: Any, metadata_json: Any = None) -> str:
    """Resolve a migration-map source occurrence to its canonical identity.

    Conflict-preserving migrations use a deterministic ``#conflict-<digest>``
    suffix to keep the sibling map row's natural key unique.  New rows record
    the authoritative occurrence explicitly in metadata; old shadow rows may
    only have the suffix, so retain that compatibility fallback.  Evidence
    that a suffix really represents a preserved sibling is validated by the
    migration validator, not by this pure identity helper.
    """

    raw = str(source_id or "")
    metadata = _json_object(metadata_json)
    for field in ("canonical_source_id", "original_source_id", "canonical_decision_id", "original_decision_id"):
        candidate = str(metadata.get(field) or "")
        if candidate:
            return candidate
    marker = "#conflict-"
    return raw.split(marker, 1)[0] if marker in raw else raw


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _exec_script_atomic(conn: sqlite3.Connection, script: str) -> None:
    """Execute DDL without sqlite3.executescript's implicit commit."""

    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            conn.execute(statement)
    if buffer.strip():
        raise sqlite3.OperationalError("incomplete rules schema statement")


_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS rules_schema_meta (
    schema_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    marker TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_definitions (
    definition_id TEXT PRIMARY KEY,
    rule_key TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    canonical_text TEXT NOT NULL DEFAULT '',
    normalized_intent TEXT NOT NULL DEFAULT '',
    rule_kind TEXT NOT NULL DEFAULT 'workflow',
    polarity TEXT NOT NULL DEFAULT 'positive',
    semantic_hash TEXT NOT NULL DEFAULT '',
    parameter_schema TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    state TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 1.0,
    revision INTEGER NOT NULL DEFAULT 1,
    rule_strength TEXT NOT NULL DEFAULT 'observation',
    maturity_state TEXT NOT NULL DEFAULT 'observing',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_v2_rule_definitions_semantic
    ON rule_definitions(semantic_hash);
CREATE TABLE IF NOT EXISTS rule_definition_versions (
    version_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
);
CREATE TABLE IF NOT EXISTS rule_bindings (
    binding_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    share_group_id TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include',
    priority INTEGER NOT NULL DEFAULT 0,
    owner_agent_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'manual',
    authorization TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
);
CREATE INDEX IF NOT EXISTS idx_v2_rule_bindings_scope
    ON rule_bindings(share_group_id, target_type, target_id, project_ref);
CREATE TABLE IF NOT EXISTS rule_binding_contributions (
    contribution_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL,
    definition_id TEXT NOT NULL,
    share_group_id TEXT NOT NULL DEFAULT '',
    source_memory_id TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    legacy_assignment_hash TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT 'agent',
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include',
    priority INTEGER NOT NULL DEFAULT 0,
    owner_agent_id TEXT NOT NULL DEFAULT '',
    audience_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    status TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (binding_id) REFERENCES rule_bindings(binding_id),
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id),
    UNIQUE (share_group_id, source_memory_id, legacy_assignment_hash)
);
CREATE INDEX IF NOT EXISTS idx_v2_binding_contrib_binding
    ON rule_binding_contributions(binding_id);
CREATE TABLE IF NOT EXISTS rule_source_links (
    source_link_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL DEFAULT 'shared_memory',
    share_group_id TEXT NOT NULL DEFAULT '',
    memory_id TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    original_definition_id TEXT NOT NULL DEFAULT '',
    canonical_definition_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE (source_kind, share_group_id, memory_id, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_v2_source_links_definition
    ON rule_source_links(canonical_definition_id);
CREATE TABLE IF NOT EXISTS rule_exceptions (
    exception_id TEXT PRIMARY KEY,
    parent_rule_id TEXT NOT NULL DEFAULT '',
    child_exception_id TEXT NOT NULL DEFAULT '',
    parent_rule TEXT NOT NULL DEFAULT '',
    child_exception TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    rollback_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    UNIQUE (parent_rule_id, child_exception_id, source_ref)
);
CREATE TABLE IF NOT EXISTS rule_decisions (
    decision_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL DEFAULT '',
    owner_agent_id TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    undo_id TEXT NOT NULL DEFAULT '',
    target_ids_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_receipt_refs (
    receipt_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    task_hash TEXT NOT NULL DEFAULT '',
    selection_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
);
CREATE TABLE IF NOT EXISTS rule_feedback_refs (
    feedback_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL DEFAULT '',
    definition_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    authority INTEGER NOT NULL DEFAULT 0,
    evidence_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (receipt_id) REFERENCES rule_receipt_refs(receipt_id)
);
CREATE TABLE IF NOT EXISTS rule_runtime_feedback_refs (
    feedback_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    receipt_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_effective_feedback_projection (
    receipt_id TEXT PRIMARY KEY,
    effective_feedback_id TEXT NOT NULL DEFAULT '',
    definition_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    positive_evidence_ref TEXT NOT NULL DEFAULT '',
    negative_evidence_ref TEXT NOT NULL DEFAULT '',
    projection_digest TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_agent_reputation (
    agent_id TEXT PRIMARY KEY,
    success_rate REAL NOT NULL DEFAULT 0.0,
    rule_accuracy REAL NOT NULL DEFAULT 0.0,
    violation_rate REAL NOT NULL DEFAULT 0.0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    feedback_quality REAL NOT NULL DEFAULT 0.0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_project_profile (
    project_ref TEXT PRIMARY KEY,
    production_level REAL NOT NULL DEFAULT 0.0,
    criticality REAL NOT NULL DEFAULT 0.0,
    owner_verified INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_runtime_stats (
    stats_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    followed INTEGER NOT NULL DEFAULT 0,
    violated INTEGER NOT NULL DEFAULT 0,
    not_applicable INTEGER NOT NULL DEFAULT 0,
    exception_count INTEGER NOT NULL DEFAULT 0,
    distinct_sessions INTEGER NOT NULL DEFAULT 0,
    distinct_projects INTEGER NOT NULL DEFAULT 0,
    last_observed_at TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS rule_evidence_contributions (
    contribution_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    independence_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'evidence',
    polarity TEXT NOT NULL DEFAULT 'positive',
    authority INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    receipt_id TEXT NOT NULL DEFAULT '',
    feedback_id TEXT NOT NULL DEFAULT '',
    source_evidence_id TEXT NOT NULL DEFAULT '',
    source_memory_id TEXT NOT NULL DEFAULT '',
    source_ids_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_evidence_effective (
    effective_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    independence_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'evidence',
    winner_contribution_id TEXT NOT NULL DEFAULT '',
    polarity TEXT NOT NULL DEFAULT 'positive',
    authority INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_governance_capabilities (
    capability_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL DEFAULT '',
    principal TEXT NOT NULL DEFAULT '',
    scope_json TEXT NOT NULL DEFAULT '{}',
    issued_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT '',
    consumed_at TEXT NOT NULL DEFAULT '',
    token_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS rule_governance_capability_consumptions (
    consumption_id TEXT PRIMARY KEY,
    capability_id TEXT NOT NULL DEFAULT '',
    proposal_id TEXT NOT NULL DEFAULT '',
    consumed_by TEXT NOT NULL DEFAULT '',
    consumed_at TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS rule_merge_proposals (
    proposal_id TEXT PRIMARY KEY,
    definition_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate',
    evidence_digest TEXT NOT NULL DEFAULT '',
    negative_digest TEXT NOT NULL DEFAULT '',
    binding_digest TEXT NOT NULL DEFAULT '',
    runtime_digest TEXT NOT NULL DEFAULT '',
    assessment_digest TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_merge_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL DEFAULT '',
    canonical_definition_id TEXT NOT NULL DEFAULT '',
    merged_definition_ids_json TEXT NOT NULL DEFAULT '[]',
    before_bindings_json TEXT NOT NULL DEFAULT '[]',
    after_bindings_json TEXT NOT NULL DEFAULT '[]',
    source_digest TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'merged',
    undo_state_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    undone_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_merge_approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL DEFAULT '',
    approved_by TEXT NOT NULL DEFAULT '',
    capability_id TEXT NOT NULL DEFAULT '',
    expected_revisions_json TEXT NOT NULL DEFAULT '{}',
    approval_scope TEXT NOT NULL DEFAULT 'merge',
    created_at TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_merge_native_requests (
    request_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    operation TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'committed')),
    result_json TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_merge_native_requests_status
    ON rule_merge_native_requests(status);
CREATE TABLE IF NOT EXISTS rule_negative_evidence_refs (
    evidence_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    receipt_id TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS rule_evidence_refs (
    evidence_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    receipt_id TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS rule_definition_aliases (
    old_definition_id TEXT PRIMARY KEY,
    new_definition_id TEXT NOT NULL DEFAULT '',
    migration_decision_id TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_canonical_state (
    scope_id TEXT PRIMARY KEY,
    share_group_id TEXT NOT NULL DEFAULT '',
    activation_status TEXT NOT NULL DEFAULT 'shadow',
    canonical_digest TEXT NOT NULL DEFAULT '',
    read_path TEXT NOT NULL DEFAULT 'legacy',
    source_digest TEXT NOT NULL DEFAULT '',
    effective_digest TEXT NOT NULL DEFAULT '',
    runtime_digest TEXT NOT NULL DEFAULT '',
    assessment_digest TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_reconciliation_jobs (
    job_id TEXT PRIMARY KEY,
    share_group_id TEXT NOT NULL DEFAULT '',
    migration_id TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT 'model',
    status TEXT NOT NULL DEFAULT 'pending_model',
    source_digest TEXT NOT NULL DEFAULT '',
    canonical_digest_before TEXT NOT NULL DEFAULT '',
    canonical_digest_after TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_projection_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT '',
    last_event_id TEXT NOT NULL DEFAULT '',
    projection_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_idempotency_fences (
    fence_id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL DEFAULT '',
    memory_id TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',
    decision_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    UNIQUE (share_group_id, key)
);
CREATE INDEX IF NOT EXISTS idx_v2_idempotency_fences_source
    ON rule_idempotency_fences(share_group_id, source_ref);
CREATE TABLE IF NOT EXISTS rule_idempotency_fence_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL DEFAULT '',
    original_fence_id TEXT NOT NULL DEFAULT '',
    conflict_fence_id TEXT NOT NULL DEFAULT '',
    payload_digest TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PRESERVED',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_decision_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    original_decision_id TEXT NOT NULL DEFAULT '',
    sibling_decision_id TEXT NOT NULL DEFAULT '',
    payload_digest TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'PRESERVED',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_migration_map (
    map_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    target_table TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    source_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'migrated',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (migration_id, source_kind, source_group_id, source_table, source_id, target_table)
);
CREATE TABLE IF NOT EXISTS rule_domain_outbox (
    event_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    payload_digest TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    consumed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_evidence_outbox (
    event_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL DEFAULT '',
    evidence_id TEXT NOT NULL DEFAULT '',
    definition_id TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL DEFAULT '',
    polarity TEXT NOT NULL DEFAULT 'positive',
    source_kind TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    consumed_at TEXT NOT NULL DEFAULT '',
    UNIQUE (migration_id, evidence_id, polarity)
);
CREATE INDEX IF NOT EXISTS idx_v2_evidence_outbox_unconsumed
    ON rule_evidence_outbox(consumed_at);
CREATE TABLE IF NOT EXISTS rule_unknown_columns_ledger (
    ledger_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_group_id TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    source_row_id TEXT NOT NULL DEFAULT '',
    column_name TEXT NOT NULL DEFAULT '',
    value_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'NOT_MIGRATED',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE (migration_id, source_path, source_table, source_row_id, column_name)
);
CREATE TABLE IF NOT EXISTS rule_unknown_column_anomalies (
    anomaly_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL DEFAULT '',
    column_name TEXT NOT NULL DEFAULT '',
    legacy_ledger_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PRESERVED',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
);
"""


class EvidenceSink(Protocol):
    """Structured sink accepted by :class:`EvidenceProjector`."""

    def write(self, reference: Mapping[str, Any]) -> Any: ...


class EvidenceProjectionError(RuntimeError):
    """Evidence sink failed; rules staging remains committed and unconsumed."""


class RuleV2Store:
    """Transactional shadow store for V2 rule facts."""

    def __init__(self, workspace: str | Path, *, read_only: bool = False, initialize: bool = True):
        self.workspace = Path(workspace).expanduser().resolve()
        self.layout = WorkspaceV2Layout(self.workspace)
        self.root = self.workspace / ".memoryguard" / "rules"
        self.db_path = self.root / "rules.db"
        self.read_only = bool(read_only)
        self._lock = WorkspaceGovernanceLock(self.workspace)
        self._state = threading.local()
        if self.read_only or not initialize:
            if not self.db_path.is_file():
                raise FileNotFoundError(f"rules database not found: {self.db_path}")
            self.layout.assert_database_path(self.db_path, "rules")
            with self._db() as conn:
                self._check_marker(conn)
            return
        # Existing files are inspected through a read-only URI before any
        # writable connector, DDL, WAL pragma, or directory mutation.  Only
        # the explicit Phase-1 rules schema is an accepted upgrade source.
        if self.db_path.exists():
            self.layout.assert_database_path(self.db_path, "rules")
            self._preflight_existing()
        # Exact V2 containment also rejects a symlink/reparse-point rules
        # directory before any mkdir or SQLite write can follow it.
        self.layout.assert_database_path(self.db_path, "rules")
        self.root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # Compatibility spellings used by early V2 drafts.
    Store = None  # assigned after class definition

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        conn = connect_database(self.db_path, readonly=readonly, timeout=5.0 if readonly else 10.0)
        return conn

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect(readonly=True)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise PermissionError("rules_store_read_only")
        active = getattr(self._state, "conn", None)
        if active is not None:
            yield active
            return
        with self._lock:
            conn = self._connect()
            self._state.conn = conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._state.conn = None
                conn.close()

    _tx = transaction

    def _active(self) -> sqlite3.Connection | None:
        return getattr(self._state, "conn", None)

    def _init_db(self) -> None:
        with self.transaction() as conn:
            # Phase-1 may already have created the two minimal rules tables.
            # Add columns before this module's indexes are parsed; SQLite
            # rejects ``CREATE INDEX ... semantic_hash`` when the old table
            # has not yet received that column.
            self._upgrade_legacy_tables(conn)
            _exec_script_atomic(conn, _SCHEMA)
            self._upgrade_legacy_tables(conn)
            conn.execute(
                "INSERT INTO rules_schema_meta(schema_id,version,marker,updated_at) "
                "VALUES ('rules',?,?,?) ON CONFLICT(schema_id) DO UPDATE SET "
                "version=excluded.version,marker=excluded.marker,updated_at=excluded.updated_at",
                (RULES_SCHEMA_VERSION, RULES_SCHEMA_MARKER, _now()),
            )

    def _check_marker(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT schema_id, version, marker FROM rules_schema_meta ORDER BY schema_id"
        ).fetchall()
        if len(rows) != 1 or str(rows[0]["schema_id"]) != "rules":
            raise RuntimeError("rules_schema_marker_missing_or_unknown")
        row = rows[0]
        if row is None:
            # A Phase-1 rules DB can be upgraded only by writable staging.
            raise RuntimeError("rules_schema_upgrade_required")
        try:
            version = int(row["version"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("rules_schema_invalid_version") from exc
        if version > RULES_SCHEMA_VERSION:
            raise RuntimeError("rules_schema_future_version")
        if version != RULES_SCHEMA_VERSION:
            raise RuntimeError("rules_schema_unsupported_version")
        if str(row["marker"]) != RULES_SCHEMA_MARKER:
            raise RuntimeError("rules_schema_marker_mismatch")

    def _preflight_existing(self) -> None:
        """Fail closed before mutation for unknown/future/old schemas."""

        try:
            with self._db() as conn:
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "rules_schema_meta" in tables:
                    self._check_marker(conn)
                    row = conn.execute("SELECT version, marker FROM rules_schema_meta WHERE schema_id='rules'").fetchone()
                    if int(row["version"]) != RULES_SCHEMA_VERSION or str(row["marker"]) != RULES_SCHEMA_MARKER:
                        raise RuntimeError("unsupported_rules_schema_marker")
                    return
                # Explicit, known Phase-1 source.  No marker guessing from
                # arbitrary tables is allowed; a missing marker is accepted
                # only for an empty SQLite file (fresh bootstrap).
                if not tables and int(conn.execute("PRAGMA user_version").fetchone()[0] or 0) == 0:
                    return
                if "schema_meta" not in tables:
                    raise RuntimeError("rules_schema_marker_missing")
                rows = conn.execute("SELECT domain, version, marker FROM schema_meta").fetchall()
                rules_rows = [row for row in rows if str(row["domain"]) == "rules"]
                if len(rules_rows) != 1 or int(rules_rows[0]["version"]) != 1 or str(rules_rows[0]["marker"]) != "memoryguard-v2-phase1":
                    raise RuntimeError("unsupported_rules_source_schema")
                if int(conn.execute("PRAGMA user_version").fetchone()[0] or 0) not in {0, 1}:
                    raise RuntimeError("unsupported_rules_source_version")
        except RuntimeError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise RuntimeError(f"rules_schema_preflight_failed: {exc}") from exc

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}

    def _upgrade_legacy_tables(self, conn: sqlite3.Connection) -> None:
        """Additive upgrade for Phase-1's minimal rules tables."""

        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "rule_definitions" not in tables or "rule_bindings" not in tables:
            return
        definitions = {
            "rule_key": "TEXT NOT NULL DEFAULT ''", "text": "TEXT NOT NULL DEFAULT ''",
            "canonical_text": "TEXT NOT NULL DEFAULT ''", "normalized_intent": "TEXT NOT NULL DEFAULT ''",
            "rule_kind": "TEXT NOT NULL DEFAULT 'workflow'", "polarity": "TEXT NOT NULL DEFAULT 'positive'",
            "semantic_hash": "TEXT NOT NULL DEFAULT ''", "parameter_schema": "TEXT NOT NULL DEFAULT '{}'",
            "status": "TEXT NOT NULL DEFAULT 'active'", "state": "TEXT NOT NULL DEFAULT 'active'",
            "confidence": "REAL NOT NULL DEFAULT 1.0", "revision": "INTEGER NOT NULL DEFAULT 1",
            "rule_strength": "TEXT NOT NULL DEFAULT 'observation'", "maturity_state": "TEXT NOT NULL DEFAULT 'observing'",
            "created_at": "TEXT NOT NULL DEFAULT ''", "updated_at": "TEXT NOT NULL DEFAULT ''",
            "superseded_by": "TEXT NOT NULL DEFAULT ''",
        }
        for name, sql_type in definitions.items():
            if name not in self._columns(conn, "rule_definitions"):
                conn.execute(f'ALTER TABLE rule_definitions ADD COLUMN "{name}" {sql_type}')
        conn.execute(
            "UPDATE rule_definitions SET canonical_text=CASE WHEN canonical_text='' THEN text ELSE canonical_text END, "
            "status=CASE WHEN status='' THEN state ELSE status END, "
            "updated_at=CASE WHEN updated_at='' THEN created_at ELSE updated_at END, "
            "rule_key=CASE WHEN rule_key='' THEN definition_id ELSE rule_key END"
        )
        # A Phase-1 unique constraint omitted share_group_id.  New databases
        # are created without that constraint; migration source identities are
        # still preserved through one contribution per assignment.
        binding_cols = self._columns(conn, "rule_bindings")
        additions = {
            "share_group_id": "TEXT NOT NULL DEFAULT ''", "provider": "TEXT NOT NULL DEFAULT ''",
            "runtime_role": "TEXT NOT NULL DEFAULT ''", "owner_agent_id": "TEXT NOT NULL DEFAULT ''",
            "created_by": "TEXT NOT NULL DEFAULT 'manual'", "authorization": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'active'", "revision": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, sql_type in additions.items():
            if name not in binding_cols:
                conn.execute(f'ALTER TABLE rule_bindings ADD COLUMN "{name}" {sql_type}')
        # Phase-1's contribution table only retained ``audience_json``.  The
        # direct scope columns are authoritative in the Rule Intelligence
        # snapshot and must be durable in V2 rather than being ledgered as
        # unknown columns.  Add them for existing shadow databases and
        # backfill from the historical audience payload when available.
        if "rule_binding_contributions" in tables:
            contribution_additions = {
                "target_type": "TEXT NOT NULL DEFAULT 'agent'",
                "target_id": "TEXT NOT NULL DEFAULT ''",
                "project_ref": "TEXT NOT NULL DEFAULT ''",
                "provider": "TEXT NOT NULL DEFAULT ''",
                "runtime_role": "TEXT NOT NULL DEFAULT ''",
                "effect": "TEXT NOT NULL DEFAULT 'include'",
                "priority": "INTEGER NOT NULL DEFAULT 0",
                "owner_agent_id": "TEXT NOT NULL DEFAULT ''",
                "revision": "INTEGER NOT NULL DEFAULT 1",
            }
            existing_columns = self._columns(conn, "rule_binding_contributions")
            # ``ALTER TABLE ... ADD COLUMN`` materializes the declared
            # default for every legacy row.  Keep the set of columns that
            # were actually absent so only those migration defaults can be
            # replaced from the historical audience payload.  Columns that
            # already existed belong to the V2 snapshot and may contain an
            # intentional value (including an empty/zero value).
            added_columns = set(contribution_additions) - existing_columns
            for name, sql_type in contribution_additions.items():
                if name not in existing_columns:
                    conn.execute(f'ALTER TABLE rule_binding_contributions ADD COLUMN "{name}" {sql_type}')
            rows = conn.execute(
                "SELECT contribution_id,audience_json,target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,revision FROM rule_binding_contributions"
            ).fetchall()
            for row in rows:
                audience = _json_object(row["audience_json"])
                updates: dict[str, Any] = {}
                for name, aliases in {
                    "target_type": ("target_type",),
                    "target_id": ("target_id",),
                    "project_ref": ("project_ref",),
                    "provider": ("provider",),
                    "runtime_role": ("runtime_role",),
                    "effect": ("effect",),
                    "priority": ("priority", "priority_override"),
                    "owner_agent_id": ("owner_agent_id",),
                    "revision": ("revision",),
                }.items():
                    # Existing V2 columns are authoritative.  A column added
                    # above contains only SQLite's migration default and may
                    # be losslessly backfilled from ``audience_json``.
                    if name not in added_columns:
                        continue
                    for alias in aliases:
                        if alias in audience and audience[alias] not in (None, ""):
                            updates[name] = int(audience[alias] or 0) if name in {"priority", "revision"} else str(audience[alias])
                            break
                if updates:
                    assignments = ",".join(f'"{key}"=?' for key in updates)
                    conn.execute(
                        f"UPDATE rule_binding_contributions SET {assignments} WHERE contribution_id=?",
                        (*updates.values(), row["contribution_id"]),
                    )
        table_sql = str(conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_bindings'").fetchone()[0] or "")
        if "UNIQUE (definition_id" in table_sql or "UNIQUE(definition_id" in table_sql:
            # Phase-1's table made the audience unique without group/source.
            # Rebuild once so two legacy groups can retain equal audience
            # identities as separate migrated contributions.
            conn.execute("DROP TABLE IF EXISTS rule_bindings__v2")
            conn.execute(
                "CREATE TABLE rule_bindings__v2 (binding_id TEXT PRIMARY KEY, definition_id TEXT NOT NULL, share_group_id TEXT NOT NULL DEFAULT '', target_type TEXT NOT NULL, target_id TEXT NOT NULL DEFAULT '', project_ref TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '', runtime_role TEXT NOT NULL DEFAULT '', effect TEXT NOT NULL DEFAULT 'include', priority INTEGER NOT NULL DEFAULT 0, owner_agent_id TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT 'manual', authorization TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id))"
            )
            conn.execute(
                "INSERT INTO rule_bindings__v2(binding_id,definition_id,share_group_id,target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,created_by,authorization,status,revision,created_at,updated_at) SELECT binding_id,definition_id,share_group_id,target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,created_by,authorization,status,revision,created_at,updated_at FROM rule_bindings"
            )
            conn.execute("DROP TABLE rule_bindings")
            conn.execute("ALTER TABLE rule_bindings__v2 RENAME TO rule_bindings")

    def _write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self.transaction() as conn:
            return fn(conn)

    def _read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        active = self._active()
        if active is not None:
            return fn(active)
        with self._db() as conn:
            return fn(conn)

    # ---- definitions -------------------------------------------------
    @staticmethod
    def _definition(value: RuleDefinition | Mapping[str, Any]) -> RuleDefinition:
        if isinstance(value, RuleDefinition):
            return value
        raw = dict(value)
        if not raw.get("definition_id"):
            return build_definition(str(raw.get("canonical_text", raw.get("text", raw.get("body", "")))), kind=raw.get("rule_kind", "workflow"))
        return RuleDefinition.from_dict(raw)

    @staticmethod
    def _definition_row(row: sqlite3.Row) -> RuleDefinition:
        return RuleDefinition(
            definition_id=str(row["definition_id"]), canonical_text=str(row["canonical_text"] or row["text"] or ""),
            normalized_intent=str(row["normalized_intent"] or ""), rule_kind=str(row["rule_kind"] or "workflow"),
            polarity=str(row["polarity"] or "positive"), semantic_hash=str(row["semantic_hash"] or ""),
            parameter_schema=str(row["parameter_schema"] or "{}"), status=str(row["status"] or row["state"] or "active"),
            confidence=float(row["confidence"] if row["confidence"] is not None else 1.0),
            revision=int(row["revision"] or 1), rule_strength=str(row["rule_strength"] or "observation"),
            maturity_state=str(row["maturity_state"] or "observing"), created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""), superseded_by=str(row["superseded_by"] or ""),
        )

    def upsert_definition(self, value: RuleDefinition | Mapping[str, Any]) -> RuleDefinition:
        definition = self._definition(value)
        p = definition.to_dict()
        now = p.get("updated_at") or p.get("created_at") or _now()
        def op(conn: sqlite3.Connection) -> RuleDefinition:
            row = conn.execute("SELECT * FROM rule_definitions WHERE definition_id=?", (p["definition_id"],)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO rule_definitions(definition_id,rule_key,text,canonical_text,normalized_intent,rule_kind,polarity,semantic_hash,parameter_schema,status,state,confidence,revision,rule_strength,maturity_state,created_at,updated_at,superseded_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["definition_id"], p["definition_id"], p["canonical_text"], p["canonical_text"], p["normalized_intent"], p["rule_kind"], p["polarity"], p["semantic_hash"], p["parameter_schema"], p["status"], p["status"], p["confidence"], p["revision"], p["rule_strength"], p["maturity_state"], p.get("created_at") or now, now, p.get("superseded_by", "")),
                )
            else:
                # Immutable identity fields cannot silently change on rerun.
                if str(row["canonical_text"] or row["text"] or "") not in {"", p["canonical_text"]}:
                    raise ValueError("definition_identity_mismatch")
                conn.execute(
                    "UPDATE rule_definitions SET canonical_text=?,text=?,normalized_intent=?,rule_kind=?,polarity=?,semantic_hash=?,parameter_schema=?,status=?,state=?,confidence=?,maturity_state=?,superseded_by=?,updated_at=? WHERE definition_id=?",
                    (p["canonical_text"], p["canonical_text"], p["normalized_intent"], p["rule_kind"], p["polarity"], p["semantic_hash"], p["parameter_schema"], p["status"], p["status"], p["confidence"], p["maturity_state"], p.get("superseded_by", ""), now, p["definition_id"]),
                )
            return self._definition_row(conn.execute("SELECT * FROM rule_definitions WHERE definition_id=?", (p["definition_id"],)).fetchone())
        return self._write(op)

    def get_definition(self, definition_id: str) -> RuleDefinition | None:
        return self._read(lambda c: (lambda row: self._definition_row(row) if row else None)(c.execute("SELECT * FROM rule_definitions WHERE definition_id=?", (definition_id,)).fetchone()))

    def list_definitions(self, status: str | None = None) -> list[RuleDefinition]:
        def op(conn: sqlite3.Connection) -> list[RuleDefinition]:
            sql, params = "SELECT * FROM rule_definitions", []
            if status:
                sql += " WHERE status=?"; params.append(status)
            return [self._definition_row(row) for row in conn.execute(sql + " ORDER BY definition_id", params).fetchall()]
        return self._read(op)

    def record_definition_version(self, definition_id: str, *, snapshot: Mapping[str, Any] | None = None, reason: str = "", actor: str = "", source_ref: str = "", version_id: str = "") -> str:
        version_id = version_id or stable_digest((definition_id, snapshot or {}, reason, actor, source_ref))
        self._write(lambda c: c.execute("INSERT OR IGNORE INTO rule_definition_versions(version_id,definition_id,revision,snapshot_json,reason,actor,source_ref,created_at) VALUES (?,?,?,?,?,?,?,?)", (version_id, definition_id, int((snapshot or {}).get("revision", 1)), _json(snapshot or {}), reason, actor, source_ref, _now())))
        return version_id

    # ---- bindings ----------------------------------------------------
    @staticmethod
    def _binding(value: RuleBinding | Mapping[str, Any]) -> RuleBinding:
        if isinstance(value, RuleBinding):
            return validate_binding_scope(value)
        return validate_binding_scope(RuleBinding.from_dict(dict(value)))

    @staticmethod
    def _binding_row(row: sqlite3.Row) -> RuleBinding:
        return RuleBinding(
            binding_id=str(row["binding_id"]), definition_id=str(row["definition_id"]), share_group_id=str(row["share_group_id"] or ""),
            target_type=str(row["target_type"] or "agent"), target_id=str(row["target_id"] or ""), project_ref=str(row["project_ref"] or ""),
            provider=str(row["provider"] or ""), runtime_role=str(row["runtime_role"] or ""), effect=str(row["effect"] or "include"), priority=int(row["priority"] or 0),
            owner_agent_id=str(row["owner_agent_id"] or ""), created_by=str(row["created_by"] or "manual"), authorization=str(row["authorization"] or ""),
            status=str(row["status"] or "active"), revision=int(row["revision"] or 1), created_at=str(row["created_at"] or ""), updated_at=str(row["updated_at"] or ""),
        )

    def upsert_binding(self, value: RuleBinding | Mapping[str, Any], *, contribution: Mapping[str, Any] | None = None) -> RuleBinding:
        binding = self._binding(value); p = binding.to_dict(); now = p.get("updated_at") or _now()
        def op(conn: sqlite3.Connection) -> RuleBinding:
            conn.execute(
                "INSERT INTO rule_bindings(binding_id,definition_id,share_group_id,target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,created_by,authorization,status,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE SET definition_id=excluded.definition_id,share_group_id=excluded.share_group_id,target_type=excluded.target_type,target_id=excluded.target_id,project_ref=excluded.project_ref,provider=excluded.provider,runtime_role=excluded.runtime_role,effect=excluded.effect,priority=excluded.priority,owner_agent_id=excluded.owner_agent_id,created_by=excluded.created_by,authorization=excluded.authorization,status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at",
                (p["binding_id"], p["definition_id"], p["share_group_id"], p["target_type"], p["target_id"], p["project_ref"], p["provider"], p["runtime_role"], p["effect"], p["priority"], p["owner_agent_id"], p["created_by"], p["authorization"], p["status"], p["revision"], p.get("created_at") or now, now),
            )
            if contribution is not None:
                self._upsert_contribution_conn(conn, {**dict(contribution), "binding_id": p["binding_id"], "definition_id": p["definition_id"], "share_group_id": p["share_group_id"]})
            return self._binding_row(conn.execute("SELECT * FROM rule_bindings WHERE binding_id=?", (p["binding_id"],)).fetchone())
        return self._write(op)

    def list_bindings(self, *, definition_id: str | None = None, share_group_id: str | None = None, status: str | None = None) -> list[RuleBinding]:
        def op(conn: sqlite3.Connection) -> list[RuleBinding]:
            clauses, params = ["1=1"], []
            for col, value in (("definition_id", definition_id), ("share_group_id", share_group_id), ("status", status)):
                if value is not None: clauses.append(col + "=?"); params.append(value)
            return [self._binding_row(r) for r in conn.execute("SELECT * FROM rule_bindings WHERE " + " AND ".join(clauses) + " ORDER BY binding_id", params).fetchall()]
        return self._read(op)

    def _upsert_contribution_conn(self, conn: sqlite3.Connection, value: Mapping[str, Any]) -> str:
        p = dict(value)
        audience = _json_object(p.get("audience", p.get("audience_json", {})))

        def scope_value(name: str, *aliases: str, default: Any = "") -> Any:
            # Direct columns are authoritative when present (including an
            # explicit priority=0); legacy callers may only provide the
            # historical audience JSON, so fall back to its aliases.
            if name in p and p[name] not in (None, ""):
                return p[name]
            for alias in (name, *aliases):
                if alias in p and p[alias] not in (None, ""):
                    return p[alias]
                if alias in audience and audience[alias] not in (None, ""):
                    return audience[alias]
            return default

        scope = {
            "target_type": str(scope_value("target_type", default="agent") or "agent"),
            "target_id": str(scope_value("target_id", default="") or ""),
            "project_ref": str(scope_value("project_ref", default="") or ""),
            "provider": str(scope_value("provider", default="") or ""),
            "runtime_role": str(scope_value("runtime_role", default="") or ""),
            "effect": str(scope_value("effect", default="include") or "include"),
            "priority": int(scope_value("priority", "priority_override", default=0) or 0),
            "owner_agent_id": str(scope_value("owner_agent_id", default="") or ""),
            "revision": int(scope_value("revision", default=1) or 1),
        }
        contribution_id = str(p.get("contribution_id") or stable_digest((p.get("share_group_id", ""), p.get("source_memory_id", ""), p.get("legacy_assignment_hash", ""))))
        now = str(p.get("updated_at") or p.get("created_at") or _now())
        natural = (str(p.get("share_group_id", "")), str(p.get("source_memory_id", "")), str(p.get("legacy_assignment_hash", "")))
        existing_natural = conn.execute(
            "SELECT * FROM rule_binding_contributions WHERE share_group_id=? AND source_memory_id=? AND legacy_assignment_hash=?",
            natural,
        ).fetchone()
        if existing_natural is not None:
            candidate = {
                "binding_id": p.get("binding_id", ""), "definition_id": p.get("definition_id", ""),
                "source_revision": p.get("source_revision", ""), "audience_json": _json(audience),
                **scope,
                "active": int(bool(p.get("active", True))), "status": p.get("status", "active"),
            }
            mismatches = [(column, existing_natural[column], value) for column, value in candidate.items() if existing_natural[column] != value]
            if mismatches:
                raise ValueError(f"immutable rule_binding_contributions natural-key conflict: {mismatches}")
            return str(existing_natural["contribution_id"])
        conn.execute(
            "INSERT INTO rule_binding_contributions(contribution_id,binding_id,definition_id,share_group_id,source_memory_id,source_revision,legacy_assignment_hash,target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,audience_json,active,status,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(contribution_id) DO UPDATE SET binding_id=excluded.binding_id,definition_id=excluded.definition_id,share_group_id=excluded.share_group_id,source_memory_id=excluded.source_memory_id,source_revision=excluded.source_revision,legacy_assignment_hash=excluded.legacy_assignment_hash,target_type=excluded.target_type,target_id=excluded.target_id,project_ref=excluded.project_ref,provider=excluded.provider,runtime_role=excluded.runtime_role,effect=excluded.effect,priority=excluded.priority,owner_agent_id=excluded.owner_agent_id,audience_json=excluded.audience_json,active=excluded.active,status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at",
            (contribution_id, p.get("binding_id", ""), p.get("definition_id", ""), p.get("share_group_id", ""), p.get("source_memory_id", ""), p.get("source_revision", ""), p.get("legacy_assignment_hash", ""), scope["target_type"], scope["target_id"], scope["project_ref"], scope["provider"], scope["runtime_role"], scope["effect"], scope["priority"], scope["owner_agent_id"], _json(audience), int(bool(p.get("active", True))), p.get("status", "active"), scope["revision"], p.get("created_at", now), now),
        )
        return contribution_id

    def upsert_binding_contribution(self, value: Mapping[str, Any]) -> str:
        return str(self._write(lambda c: self._upsert_contribution_conn(c, value)))

    def list_binding_contributions(self, *, share_group_id: str | None = None, source_memory_id: str | None = None, active: bool | None = None) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses, params = ["1=1"], []
            for col, value in (("share_group_id", share_group_id), ("source_memory_id", source_memory_id)):
                if value is not None: clauses.append(col + "=?"); params.append(value)
            if active is not None: clauses.append("active=?"); params.append(int(active))
            return [_row_dict(r) for r in conn.execute("SELECT * FROM rule_binding_contributions WHERE " + " AND ".join(clauses) + " ORDER BY contribution_id", params).fetchall()]
        return self._read(op)

    # ---- generic reference rows -------------------------------------
    def _insert_generic(self, table: str, key: str, value: Mapping[str, Any], *, conn: sqlite3.Connection | None = None) -> str:
        """Insert an immutable reference row, accepting exact replays only.

        ``INSERT OR REPLACE`` is unsafe for migration/ledger rows: a changed
        payload deletes the old row before inserting the replacement.  On a
        conflict we compare every supplied column and reject any difference;
        an identical replay is a no-op.
        """

        payload = dict(value)
        ident = str(payload.get(key) or stable_digest((table, payload)))

        def insert_exact(target: sqlite3.Connection) -> str:
            columns = self._columns(target, table)
            payload[key] = ident
            allowed = [name for name in payload if name in columns and name != key]
            # Generic tables use JSON fields; retain unknown source columns in
            # the metadata_json field rather than silently dropping them.
            if "metadata_json" in columns:
                extras = {name: payload[name] for name in payload if name not in columns}
                if extras:
                    payload["metadata_json"] = _json({**_json_object(payload.get("metadata_json", {})), "unknown": extras})
                if "metadata_json" not in allowed:
                    allowed.append("metadata_json")
            cols = [key, *allowed]
            vals = [payload.get(col, "") for col in cols]
            quoted_table = '"' + table.replace('"', '""') + '"'
            quoted_cols = ",".join('"' + col.replace('"', '""') + '"' for col in cols)
            sql = f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES ({','.join('?' for _ in cols)})"
            try:
                target.execute(sql, vals)
            except sqlite3.IntegrityError:
                existing = target.execute(
                    f"SELECT * FROM {quoted_table} WHERE \"{key.replace(chr(34), chr(34) * 2)}\"=?",
                    (ident,),
                ).fetchone()
                if existing is None:
                    # A different unique constraint (rather than this key)
                    # was hit.  Never turn it into a replacement.
                    raise
                mismatches = [
                    (col, existing[col], val)
                    for col, val in zip(cols, vals)
                    if existing[col] != val
                ]
                if mismatches:
                    raise ValueError(f"immutable {table} conflict for {key}={ident}: {mismatches}")
                return ident
            return ident

        if conn is None:
            return str(self._write(insert_exact))
        return insert_exact(conn)

    def _insert_natural_exact(
        self,
        table: str,
        key: str,
        natural_columns: tuple[str, ...],
        value: Mapping[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Insert immutable row while honoring surrogate+natural uniqueness.

        Legacy partial rows may have a different surrogate PK.  If natural
        identity and every supplied immutable field match, reuse old PK and
        metadata verbatim; divergent payload fails closed.
        """
        payload = dict(value)

        def op(target: sqlite3.Connection) -> str:
            where = " AND ".join(f'"{column}"=?' for column in natural_columns)
            natural_values = tuple(payload.get(column, "") for column in natural_columns)
            existing = target.execute(f'SELECT * FROM "{table}" WHERE {where}', natural_values).fetchone()
            if existing is not None:
                supplied_key = str(payload.get(key) or "")
                mismatches = []
                for column, candidate in payload.items():
                    if column == key or column not in existing.keys():
                        continue
                    if existing[column] != candidate:
                        mismatches.append((column, existing[column], candidate))
                if mismatches:
                    raise ValueError(f"immutable {table} natural-key conflict: {mismatches}")
                return str(existing[key])
            return self._insert_generic(table, key, payload, conn=target)

        return str(self._write(op)) if conn is None else str(op(conn))

    def upsert_source_link(self, **value: Any) -> str:
        value = dict(value); value.setdefault("source_link_id", stable_digest((value.get("source_kind", "shared_memory"), value.get("share_group_id", ""), value.get("memory_id", ""), value.get("source_ref", ""))))
        return self._insert_natural_exact("rule_source_links", "source_link_id", ("source_kind", "share_group_id", "memory_id", "source_ref"), value)

    def get_source_link(
        self, share_group_id: str, memory_id: str,
    ) -> dict[str, Any] | None:
        """Read one native source link using the canonical V2 table."""
        row = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_source_links WHERE share_group_id=? AND memory_id=? "
            "ORDER BY updated_at DESC, source_link_id DESC LIMIT 1",
            (str(share_group_id or ""), str(memory_id or "")),
        ).fetchone())
        return None if row is None else _row_dict(row)

    def list_source_links(
        self,
        *,
        share_group_id: str | None = None,
        status: str | None = None,
        canonical_definition_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List native source links without exposing evidence as ownership."""
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses, params = ["1=1"], []
            for column, value in (
                ("share_group_id", share_group_id),
                ("status", status),
                ("canonical_definition_id", canonical_definition_id),
            ):
                if value is not None:
                    clauses.append(column + "=?")
                    params.append(value)
            rows = conn.execute(
                "SELECT * FROM rule_source_links WHERE "
                + " AND ".join(clauses)
                + " ORDER BY share_group_id, memory_id, source_link_id",
                params,
            ).fetchall()
            return [_row_dict(row) for row in rows]
        return self._read(op)

    def resolve_canonical(self, definition_id: str) -> str:
        """Follow native alias/supersession rows without reviving old heads."""
        current = str(definition_id or "")
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            definition = self.get_definition(current)
            if definition is None:
                break
            status = str(getattr(definition, "status", "") or "").casefold()
            successor = str(getattr(definition, "superseded_by", "") or "")
            if not successor:
                alias = self._read(lambda conn: conn.execute(
                    "SELECT new_definition_id FROM rule_definition_aliases "
                    "WHERE old_definition_id=?",
                    (current,),
                ).fetchone())
                successor = str(alias[0] or "") if alias is not None else ""
            if status in {"alias", "merged", "superseded"} and successor:
                current = successor
                continue
            break
        return current

    def upsert_exception(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("exception_id", stable_digest((value.get("parent_rule_id", value.get("parent_rule", "")), value.get("child_exception_id", value.get("child_exception", "")), value.get("source_ref", ""))))
        return self._insert_natural_exact("rule_exceptions", "exception_id", ("parent_rule_id", "child_exception_id", "source_ref"), value)

    def record_decision(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("decision_id", stable_digest((value.get("source_ref", ""), value.get("action", ""), value.get("created_at", ""))))
        return self._insert_generic("rule_decisions", "decision_id", value)

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_decisions WHERE decision_id=?", (str(decision_id or ""),),
        ).fetchone())
        return None if row is None else {str(key): row[key] for key in row.keys()}

    def get_decision_by_undo(self, undo_id: str) -> dict[str, Any] | None:
        row = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_decisions WHERE undo_id=? ORDER BY created_at DESC, decision_id DESC LIMIT 1",
            (str(undo_id or ""),),
        ).fetchone())
        return None if row is None else {str(key): row[key] for key in row.keys()}

    def record_receipt(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("receipt_id", stable_digest((value.get("source_ref", ""), value.get("source_rule_id", ""))))
        return self._insert_generic("rule_receipt_refs", "receipt_id", value)

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        row = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_receipt_refs WHERE receipt_id=?", (str(receipt_id or ""),),
        ).fetchone())
        return None if row is None else {str(key): row[key] for key in row.keys()}

    def record_feedback(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("feedback_id", stable_digest((value.get("source_ref", ""), value.get("receipt_id", ""), value.get("outcome", ""))))
        return self._insert_generic("rule_feedback_refs", "feedback_id", value)

    def list_feedback(
        self,
        *,
        receipt_id: str | None = None,
        definition_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read immutable feedback rows without crossing the V2 store boundary."""

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses, params = ["1=1"], []
            for column, value in (("receipt_id", receipt_id), ("definition_id", definition_id)):
                if value is not None:
                    clauses.append(column + "=?")
                    params.append(str(value))
            rows = conn.execute(
                "SELECT * FROM rule_feedback_refs WHERE " + " AND ".join(clauses) +
                " ORDER BY created_at, feedback_id",
                params,
            ).fetchall()
            return [_row_dict(row) for row in rows]

        return self._read(op)

    def record_runtime_feedback(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("feedback_id", stable_digest((value.get("source_ref", ""), value.get("receipt_id", ""))))
        return self._insert_generic("rule_runtime_feedback_refs", "feedback_id", value)

    def record_agent_reputation(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("agent_id", stable_digest(value)); return self._insert_generic("rule_agent_reputation", "agent_id", value)

    def record_project_profile(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("project_ref", "unknown"); return self._insert_generic("rule_project_profile", "project_ref", value)

    def record_runtime_stats(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("stats_id", stable_digest(value)); return self._insert_generic("rule_runtime_stats", "stats_id", value)

    def record_evidence_contribution(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("contribution_id", stable_digest(value)); return self._insert_generic("rule_evidence_contributions", "contribution_id", value)

    def record_evidence_effective(self, value: Mapping[str, Any]) -> str:
        """Upsert one rebuildable effective-evidence projection.

        Contributions and feedback are immutable evidence.  The effective
        row is intentionally mutable: undo/deactivation must be able to
        restore the next deterministic runner-up without rewriting history.
        """

        item = dict(value)
        definition_id = str(item.get("definition_id") or "")
        independence_key = str(item.get("independence_key") or "")
        kind = str(item.get("kind") or "evidence")
        item.setdefault("effective_id", stable_digest(("effective", definition_id, independence_key)))
        item.setdefault("updated_at", _now())

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                "INSERT INTO rule_evidence_effective(effective_id,definition_id,independence_key,kind,winner_contribution_id,polarity,authority,confidence,observed_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(effective_id) DO UPDATE SET definition_id=excluded.definition_id,independence_key=excluded.independence_key,kind=excluded.kind,winner_contribution_id=excluded.winner_contribution_id,polarity=excluded.polarity,authority=excluded.authority,confidence=excluded.confidence,observed_at=excluded.observed_at,updated_at=excluded.updated_at",
                (
                    str(item.get("effective_id") or ""), definition_id,
                    independence_key, kind,
                    str(item.get("winner_contribution_id") or ""),
                    str(item.get("polarity") or "positive"),
                    int(item.get("authority", 0) or 0),
                    float(item.get("confidence", 1.0) if item.get("confidence") is not None else 1.0),
                    str(item.get("observed_at") or ""), str(item.get("updated_at") or _now()),
                ),
            )
            conn.execute(
                "DELETE FROM rule_evidence_effective WHERE definition_id=? AND independence_key=? AND kind=? AND effective_id<>?",
                (definition_id, independence_key, kind, str(item.get("effective_id") or "")),
            )
            return str(item.get("effective_id") or "")

        return str(self._write(op))

    def list_evidence_contributions(
        self,
        *,
        definition_id: str | None = None,
        independence_key: str | None = None,
        kind: str | None = None,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses, params = ["1=1"], []
            for column, value in (
                ("definition_id", definition_id),
                ("independence_key", independence_key),
                ("kind", kind),
            ):
                if value is not None:
                    clauses.append(column + "=?")
                    params.append(str(value))
            if active is not None:
                clauses.append("active=?")
                params.append(int(bool(active)))
            rows = conn.execute(
                "SELECT * FROM rule_evidence_contributions WHERE " + " AND ".join(clauses) +
                " ORDER BY definition_id, independence_key, kind, contribution_id",
                params,
            ).fetchall()
            return [_row_dict(row) for row in rows]

        return self._read(op)

    def deactivate_evidence_contribution(self, contribution_id: str, *, updated_at: str = "") -> bool:
        """Deactivate one contribution while preserving its immutable row."""

        def op(conn: sqlite3.Connection) -> bool:
            changed = conn.execute(
                "UPDATE rule_evidence_contributions SET active=0,updated_at=? WHERE contribution_id=? AND active=1",
                (str(updated_at or _now()), str(contribution_id or "")),
            ).rowcount
            return bool(changed)

        return bool(self._write(op))

    def rebuild_evidence_effective(
        self,
        *,
        definition_id: str,
        independence_key: str,
        kind: str = "evidence",
        updated_at: str = "",
    ) -> dict[str, Any] | None:
        """Rebuild one effective group using a deterministic trusted winner.

        Legacy/migrated rows without a trust marker remain eligible.  Native
        rows explicitly marked ``session_trusted=false`` are retained for
        audit but cannot affect narrowing/effective state.
        """

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            rows = conn.execute(
                "SELECT * FROM rule_evidence_contributions WHERE definition_id=? AND independence_key=? AND kind=? AND active=1",
                (str(definition_id), str(independence_key), str(kind)),
            ).fetchall()
            eligible: list[sqlite3.Row] = []
            for row in rows:
                metadata = _json_object(row["metadata_json"])
                trust = metadata.get("session_trusted")
                if trust is not None and str(trust).casefold() in {"0", "false", "no", "untrusted"}:
                    continue
                eligible.append(row)
            winner = max(
                eligible,
                key=lambda row: (
                    int(row["authority"] or 0),
                    float(row["confidence"] if row["confidence"] is not None else 0.0),
                    str(row["observed_at"] or ""),
                    str(row["contribution_id"] or ""),
                ),
                default=None,
            )
            effective_id = stable_digest(("effective", str(definition_id), str(independence_key)))
            if winner is None:
                conn.execute(
                    "DELETE FROM rule_evidence_effective WHERE definition_id=? AND independence_key=? AND kind=?",
                    (str(definition_id), str(independence_key), str(kind)),
                )
                return None
            now = str(updated_at or _now())
            conn.execute(
                "INSERT INTO rule_evidence_effective(effective_id,definition_id,independence_key,kind,winner_contribution_id,polarity,authority,confidence,observed_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(effective_id) DO UPDATE SET definition_id=excluded.definition_id,independence_key=excluded.independence_key,kind=excluded.kind,winner_contribution_id=excluded.winner_contribution_id,polarity=excluded.polarity,authority=excluded.authority,confidence=excluded.confidence,observed_at=excluded.observed_at,updated_at=CASE WHEN rule_evidence_effective.winner_contribution_id=excluded.winner_contribution_id THEN rule_evidence_effective.updated_at ELSE excluded.updated_at END",
                (
                    effective_id, str(definition_id), str(independence_key), str(kind),
                    str(winner["contribution_id"] or ""), str(winner["polarity"] or "positive"),
                    int(winner["authority"] or 0), float(winner["confidence"] if winner["confidence"] is not None else 1.0),
                    str(winner["observed_at"] or ""), now,
                ),
            )
            conn.execute(
                "DELETE FROM rule_evidence_effective WHERE definition_id=? AND independence_key=? AND kind=? AND effective_id<>?",
                (str(definition_id), str(independence_key), str(kind), effective_id),
            )
            row = conn.execute(
                "SELECT * FROM rule_evidence_effective WHERE effective_id=?", (effective_id,),
            ).fetchone()
            return None if row is None else _row_dict(row)

        active = self._active()
        if active is not None:
            return op(active)
        return self._write(op)

    def get_effective_feedback_projection(self, receipt_id: str) -> dict[str, Any] | None:
        row = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_effective_feedback_projection WHERE receipt_id=?",
            (str(receipt_id or ""),),
        ).fetchone())
        return None if row is None else _row_dict(row)

    def upsert_effective_feedback_projection(self, value: Mapping[str, Any]) -> str:
        item = dict(value)
        receipt_id = str(item.get("receipt_id") or "")
        if not receipt_id:
            raise ValueError("receipt_id is required")
        projection_digest = str(item.get("projection_digest") or stable_digest({key: value for key, value in item.items() if key != "projection_digest"}))
        now = str(item.get("updated_at") or _now())

        def op(conn: sqlite3.Connection) -> str:
            conn.execute(
                "INSERT INTO rule_effective_feedback_projection(receipt_id,effective_feedback_id,definition_id,outcome,positive_evidence_ref,negative_evidence_ref,projection_digest,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(receipt_id) DO UPDATE SET effective_feedback_id=excluded.effective_feedback_id,definition_id=excluded.definition_id,outcome=excluded.outcome,positive_evidence_ref=excluded.positive_evidence_ref,negative_evidence_ref=excluded.negative_evidence_ref,projection_digest=excluded.projection_digest,updated_at=excluded.updated_at",
                (
                    receipt_id, str(item.get("effective_feedback_id") or ""),
                    str(item.get("definition_id") or ""), str(item.get("outcome") or ""),
                    str(item.get("positive_evidence_ref") or ""),
                    str(item.get("negative_evidence_ref") or ""),
                    projection_digest, now,
                ),
            )
            return receipt_id

        return str(self._write(op))

    def rebuild_effective_feedback_projection(self, receipt_id: str, *, updated_at: str = "") -> dict[str, Any] | None:
        """Rebuild the per-receipt projection from trusted feedback rows."""

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            rows = conn.execute(
                "SELECT * FROM rule_feedback_refs WHERE receipt_id=? ORDER BY created_at, feedback_id",
                (str(receipt_id or ""),),
            ).fetchall()
            eligible: list[sqlite3.Row] = []
            for row in rows:
                metadata = _json_object(row["metadata_json"])
                trust = metadata.get("session_trusted")
                if trust is not None and str(trust).casefold() in {"0", "false", "no", "untrusted"}:
                    continue
                if str(row["outcome"] or "").casefold() in {"", "ignored", "unobserved"}:
                    continue
                eligible.append(row)
            winner = max(
                eligible,
                key=lambda row: (
                    int(row["authority"] or 0),
                    float(_json_object(row["metadata_json"]).get("confidence", 0.0) or 0.0),
                    str(row["created_at"] or ""),
                    str(row["feedback_id"] or ""),
                ),
                default=None,
            )
            if winner is None:
                conn.execute(
                    "DELETE FROM rule_effective_feedback_projection WHERE receipt_id=?",
                    (str(receipt_id or ""),),
                )
                return None
            contribution = conn.execute(
                "SELECT contribution_id,polarity FROM rule_evidence_contributions WHERE feedback_id=? AND active=1 ORDER BY contribution_id LIMIT 1",
                (str(winner["feedback_id"] or ""),),
            ).fetchone()
            contribution_id = str(contribution["contribution_id"] or "") if contribution is not None else ""
            polarity = str(contribution["polarity"] or "") if contribution is not None else ""
            now = str(updated_at or _now())
            projection = {
                "receipt_id": str(receipt_id),
                "effective_feedback_id": str(winner["feedback_id"] or ""),
                "definition_id": str(winner["definition_id"] or ""),
                "outcome": str(winner["outcome"] or ""),
                "positive_evidence_ref": contribution_id if polarity == "positive" else "",
                "negative_evidence_ref": contribution_id if polarity == "negative" else "",
                "updated_at": now,
            }
            projection["projection_digest"] = stable_digest(projection)
            conn.execute(
                "INSERT INTO rule_effective_feedback_projection(receipt_id,effective_feedback_id,definition_id,outcome,positive_evidence_ref,negative_evidence_ref,projection_digest,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(receipt_id) DO UPDATE SET effective_feedback_id=excluded.effective_feedback_id,definition_id=excluded.definition_id,outcome=excluded.outcome,positive_evidence_ref=excluded.positive_evidence_ref,negative_evidence_ref=excluded.negative_evidence_ref,projection_digest=excluded.projection_digest,updated_at=excluded.updated_at",
                (
                    projection["receipt_id"], projection["effective_feedback_id"],
                    projection["definition_id"], projection["outcome"],
                    projection["positive_evidence_ref"], projection["negative_evidence_ref"],
                    projection["projection_digest"], projection["updated_at"],
                ),
            )
            return projection

        active = self._active()
        if active is not None:
            return op(active)
        return self._write(op)

    def record_governance_capability(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("capability_id", stable_digest(value)); return self._insert_generic("rule_governance_capabilities", "capability_id", value)

    def record_governance_capability_consumption(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("consumption_id", stable_digest(value)); return self._insert_generic("rule_governance_capability_consumptions", "consumption_id", value)

    def record_effective_projection(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("projection_digest", stable_digest(value)); value.setdefault("receipt_id", stable_digest((value.get("definition_id", ""), value.get("effective_feedback_id", ""))))
        return self._insert_generic("rule_effective_feedback_projection", "receipt_id", value)

    def record_merge_proposal(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("proposal_id", stable_digest(value)); return self._insert_generic("rule_merge_proposals", "proposal_id", value)

    def record_merge_decision(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("decision_id", stable_digest(value)); return self._insert_generic("rule_merge_decisions", "decision_id", value)

    def record_merge_approval(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("approval_id", stable_digest(value)); return self._insert_generic("rule_merge_approvals", "approval_id", value)

    def upsert_merge_native_request(self, value: Mapping[str, Any]) -> str:
        """Migrate/replay one durable native RuleMerge request fence.

        Request identity/fingerprint/operation/schema are immutable.  Only the
        lifecycle projection (pending -> committed, result, updated_at) may
        advance, matching the production native transaction ledger.
        """
        item = dict(value)
        key = str(item.get("request_key") or "").strip()
        if not key:
            raise ValueError("rule_merge_native_request_key_required")
        fingerprint = str(item.get("request_fingerprint") or "")
        operation = str(item.get("operation") or "")
        schema_version = int(item.get("schema_version", 2) or 2)
        status = str(item.get("status") or "pending")
        if status not in {"pending", "committed"}:
            raise ValueError("rule_merge_native_request_status_invalid")

        def op(conn: sqlite3.Connection) -> str:
            existing = conn.execute(
                "SELECT request_fingerprint,operation,schema_version,status,result_json FROM rule_merge_native_requests WHERE request_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                immutable = (fingerprint, operation, schema_version)
                observed = (str(existing[0] or ""), str(existing[1] or ""), int(existing[2] or 0))
                if observed != immutable:
                    raise ValueError("immutable rule_merge_native_requests conflict")
                if str(existing[3] or "") == "committed" and status != "committed":
                    return key
                conn.execute(
                    "UPDATE rule_merge_native_requests SET status=?,result_json=?,updated_at=? WHERE request_key=?",
                    (status, str(item.get("result_json") or ""), str(item.get("updated_at") or item.get("created_at") or ""), key),
                )
                return key
            conn.execute(
                "INSERT INTO rule_merge_native_requests(request_key,request_fingerprint,operation,schema_version,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (key, fingerprint, operation, schema_version, status, str(item.get("result_json") or ""), str(item.get("created_at") or ""), str(item.get("updated_at") or item.get("created_at") or "")),
            )
            return key

        return str(self._write(op))

    def record_evidence_ref(self, value: Mapping[str, Any], *, negative: bool = False) -> str:
        value = dict(value); value.setdefault("evidence_id", stable_digest(value)); return self._insert_generic("rule_negative_evidence_refs" if negative else "rule_evidence_refs", "evidence_id", value)

    def record_alias(self, old_definition_id: str, new_definition_id: str, *, migration_decision_id: str = "", source_ref: str = "") -> str:
        """Record a definition alias idempotently.

        Alias rows are immutable migration evidence.  A replay of the same
        migration must not fail merely because wall-clock ``created_at`` moved;
        the original timestamp is part of the preserved evidence, not a new
        mutable value.
        """
        existing = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_definition_aliases WHERE old_definition_id=?",
            (old_definition_id,),
        ).fetchone())
        if existing is not None:
            expected = {
                "new_definition_id": new_definition_id,
                "source_ref": source_ref,
            }
            mismatches = [
                (key, existing[key], value)
                for key, value in expected.items()
                if key in existing.keys() and str(existing[key]) != str(value)
            ]
            if mismatches:
                raise ValueError(f"immutable rule_definition_aliases conflict: {mismatches}")
            return str(existing["old_definition_id"])
        return self._insert_generic("rule_definition_aliases", "old_definition_id", {
            "old_definition_id": old_definition_id,
            "new_definition_id": new_definition_id,
            "migration_decision_id": migration_decision_id,
            "source_ref": source_ref,
            "created_at": _now(),
        })

    @staticmethod
    def _binding_identity_row(row: sqlite3.Row) -> tuple[str, ...]:
        """Return the audience identity used when folding bindings.

        Priority is a policy weight, not audience scope.  Excluding it here
        lets equal audiences with different priorities fold to one binding;
        the merge path raises that binding to the maximum priority first.
        """

        return (
            str(row["target_type"] or ""), str(row["target_id"] or ""),
            str(row["project_ref"] or ""), str(row["provider"] or "").casefold(),
            str(row["runtime_role"] or "").casefold(),
            str(row["effect"] or "include").casefold(),
        )

    def _canonicalize_binding_policy_conn(
        self,
        conn: sqlite3.Connection,
        winner_definition_id: str,
        loser_definition_id: str,
    ) -> None:
        """Raise canonical binding policy without dropping provenance rows."""

        winner_bindings = conn.execute(
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active' "
            "ORDER BY binding_id",
            (winner_definition_id,),
        ).fetchall()
        loser_bindings = conn.execute(
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active' "
            "ORDER BY binding_id",
            (loser_definition_id,),
        ).fetchall()
        winner_by_scope = {
            self._binding_identity_row(row): row for row in winner_bindings
        }
        now = _now()
        for loser in loser_bindings:
            winner = winner_by_scope.get(self._binding_identity_row(loser))
            if winner is None:
                continue
            winner_priority = max(
                int(winner["priority"] or 0), int(loser["priority"] or 0),
            )
            if winner_priority != int(winner["priority"] or 0):
                conn.execute(
                    "UPDATE rule_bindings SET priority=?,revision=revision+1,"
                    "updated_at=? WHERE binding_id=?",
                    (winner_priority, now, str(winner["binding_id"])),
                )

    def _migrate_legacy_definition_conn(
        self,
        conn: sqlite3.Connection,
        old_definition_id: str,
        new_definition_id: str,
        *,
        migration_decision_id: str = "",
        source_rule_id: str = "",
    ) -> list[tuple[str, ...]] | None:
        """Move one V2 definition inside caller transaction.

        V2 has no legacy sidecar evidence tables.  Repoint every native
        definition-owned projection in this transaction, preserve old
        bindings as superseded rows, and append an immutable alias.  Alias
        existence is the replay fence: a second call performs no writes.
        """
        if old_definition_id == new_definition_id:
            return None
        old = conn.execute(
            "SELECT * FROM rule_definitions WHERE definition_id=?",
            (old_definition_id,),
        ).fetchone()
        if old is None or str(old["status"] or "").casefold() in {
            "alias", "merged", "superseded",
        }:
            return None
        alias = conn.execute(
            "SELECT new_definition_id FROM rule_definition_aliases "
            "WHERE old_definition_id=?",
            (old_definition_id,),
        ).fetchone()
        if alias is not None:
            if str(alias[0] or "") != str(new_definition_id):
                raise ValueError("immutable rule_definition_aliases conflict")
            return None
        if conn.execute(
            "SELECT 1 FROM rule_definitions WHERE definition_id=?",
            (new_definition_id,),
        ).fetchone() is None:
            raise ValueError("canonical_definition_missing")

        now = _now()
        old_bindings = conn.execute(
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active' "
            "ORDER BY binding_id",
            (old_definition_id,),
        ).fetchall()
        new_bindings = conn.execute(
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active' "
            "ORDER BY binding_id",
            (new_definition_id,),
        ).fetchall()
        removed_audiences = [self._binding_identity_row(row) for row in old_bindings]
        new_by_audience = {
            self._binding_identity_row(row): row for row in new_bindings
        }

        for old_binding in old_bindings:
            old_binding_id = str(old_binding["binding_id"])
            target = new_by_audience.get(self._binding_identity_row(old_binding))
            target_id = str(target["binding_id"]) if target is not None else ""
            contributions = conn.execute(
                "SELECT * FROM rule_binding_contributions WHERE binding_id=? "
                "ORDER BY contribution_id",
                (old_binding_id,),
            ).fetchall()
            for contribution in contributions:
                contribution_id = str(contribution["contribution_id"])
                if target_id:
                    existing = conn.execute(
                        "SELECT contribution_id FROM rule_binding_contributions "
                        "WHERE share_group_id=? AND source_memory_id=? "
                        "AND legacy_assignment_hash=? AND contribution_id<>?",
                        (
                            str(contribution["share_group_id"] or ""),
                            str(contribution["source_memory_id"] or ""),
                            str(contribution["legacy_assignment_hash"] or ""),
                            contribution_id,
                        ),
                    ).fetchone()
                    if existing is not None:
                        # Keep both provenance rows, but only one contributes
                        # to active readback.
                        conn.execute(
                            "UPDATE rule_binding_contributions SET definition_id=?, "
                            "active=0,status='superseded',updated_at=? "
                            "WHERE contribution_id=?",
                            (new_definition_id, now, contribution_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE rule_binding_contributions SET definition_id=?, "
                            "binding_id=?,updated_at=? WHERE contribution_id=?",
                            (new_definition_id, target_id, now, contribution_id),
                        )
                else:
                    conn.execute(
                        "UPDATE rule_binding_contributions SET definition_id=?, "
                        "updated_at=? WHERE contribution_id=?",
                        (new_definition_id, now, contribution_id),
                    )
            if target_id:
                conn.execute(
                    "UPDATE rule_bindings SET status='superseded',revision=revision+1,"
                    "updated_at=? WHERE binding_id=?",
                    (now, old_binding_id),
                )
            else:
                conn.execute(
                    "UPDATE rule_bindings SET definition_id=?,revision=revision+1,"
                    "updated_at=? WHERE binding_id=?",
                    (new_definition_id, now, old_binding_id),
                )

        # All native evidence/projection rows remain auditable; only their
        # canonical owner changes.  Tables are schema-owned, so explicit SQL
        # makes missing/unknown columns fail closed inside this transaction.
        for table in (
            "rule_evidence_refs", "rule_negative_evidence_refs",
            "rule_evidence_contributions", "rule_evidence_effective",
            "rule_effective_feedback_projection", "rule_evidence_outbox",
            "rule_receipt_refs", "rule_feedback_refs",
            "rule_runtime_feedback_refs", "rule_runtime_stats",
        ):
            conn.execute(
                f"UPDATE {table} SET definition_id=? WHERE definition_id=?",
                (new_definition_id, old_definition_id),
            )
        conn.execute(
            "UPDATE rule_source_links SET canonical_definition_id=?,updated_at=? "
            "WHERE canonical_definition_id=?",
            (new_definition_id, now, old_definition_id),
        )
        conn.execute(
            "UPDATE rule_definitions SET status='alias',superseded_by=?,"
            "revision=revision+1,updated_at=? WHERE definition_id=?",
            (new_definition_id, now, old_definition_id),
        )
        conn.execute(
            "INSERT INTO rule_definition_aliases(old_definition_id,new_definition_id,"
            "migration_decision_id,source_ref,created_at) VALUES(?,?,?,?,?)",
            (
                old_definition_id, new_definition_id,
                str(migration_decision_id or ""),
                f"reconciliation:{migration_decision_id}" if migration_decision_id else "",
                now,
            ),
        )
        return removed_audiences

    def migrate_legacy_definition(
        self,
        old_definition_id: str,
        new_definition_id: str,
        *,
        migration_decision_id: str = "",
        source_rule_id: str = "",
    ) -> list[tuple[str, ...]] | None:
        """Atomically alias one native V2 definition to another."""
        return self._write(lambda conn: self._migrate_legacy_definition_conn(
            conn, old_definition_id, new_definition_id,
            migration_decision_id=migration_decision_id,
            source_rule_id=source_rule_id,
        ))

    @staticmethod
    def _reconciliation_primary_keys() -> dict[str, str]:
        return {
            "rule_definitions": "definition_id",
            "rule_bindings": "binding_id",
            "rule_binding_contributions": "contribution_id",
            "rule_evidence_refs": "evidence_id",
            "rule_negative_evidence_refs": "evidence_id",
            "rule_evidence_contributions": "contribution_id",
            "rule_evidence_effective": "effective_id",
            "rule_effective_feedback_projection": "receipt_id",
            "rule_evidence_outbox": "event_id",
            "rule_receipt_refs": "receipt_id",
            "rule_feedback_refs": "feedback_id",
            "rule_runtime_feedback_refs": "feedback_id",
            "rule_runtime_stats": "stats_id",
            "rule_source_links": "source_link_id",
            "rule_definition_versions": "version_id",
            "rule_definition_aliases": "old_definition_id",
            "rule_decisions": "decision_id",
        }

    def _reconciliation_before_conn(
        self,
        conn: sqlite3.Connection,
        old_definition_id: str,
    ) -> dict[str, dict[str, dict[str, Any]]]:
        """Capture exact rows touched by one native alias migration."""
        rows: dict[str, dict[str, dict[str, Any]]] = {}

        def add(table: str, query: str, params: tuple[Any, ...]) -> None:
            primary = self._reconciliation_primary_keys()[table]
            selected = conn.execute(query, params).fetchall()
            rows[table] = {
                str(row[primary]): _row_dict(row) for row in selected
            }

        add(
            "rule_definitions",
            "SELECT * FROM rule_definitions WHERE definition_id=?",
            (old_definition_id,),
        )
        add(
            "rule_bindings",
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
            (old_definition_id,),
        )
        binding_ids = tuple(rows["rule_bindings"])
        if binding_ids:
            marks = ",".join("?" for _ in binding_ids)
            add(
                "rule_binding_contributions",
                f"SELECT * FROM rule_binding_contributions WHERE binding_id IN ({marks})",
                binding_ids,
            )
        else:
            rows["rule_binding_contributions"] = {}
        for table in (
            "rule_evidence_refs", "rule_negative_evidence_refs",
            "rule_evidence_contributions", "rule_evidence_effective",
            "rule_effective_feedback_projection", "rule_evidence_outbox",
            "rule_receipt_refs", "rule_feedback_refs",
            "rule_runtime_feedback_refs", "rule_runtime_stats",
        ):
            add(
                table,
                f"SELECT * FROM {table} WHERE definition_id=?",
                (old_definition_id,),
            )
        add(
            "rule_source_links",
            "SELECT * FROM rule_source_links WHERE canonical_definition_id=?",
            (old_definition_id,),
        )
        rows["rule_definition_versions"] = {}
        rows["rule_definition_aliases"] = {}
        rows["rule_decisions"] = {}
        return rows

    def _reconciliation_after_conn(
        self,
        conn: sqlite3.Connection,
        before: Mapping[str, Mapping[str, Mapping[str, Any]]],
        created: Mapping[str, Sequence[str]],
    ) -> dict[str, Any]:
        primary_keys = self._reconciliation_primary_keys()
        delta: dict[str, Any] = {"updated": {}, "created": {}}
        for table, prior_rows in before.items():
            primary = primary_keys[table]
            pairs = []
            for row_id, prior in prior_rows.items():
                current = conn.execute(
                    f"SELECT * FROM {table} WHERE {primary}=?", (row_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError(f"reconciliation_delta_row_missing:{table}:{row_id}")
                pairs.append({"before": dict(prior), "after": _row_dict(current)})
            if pairs:
                delta["updated"][table] = pairs
        for table, row_ids in created.items():
            primary = primary_keys[table]
            created_rows = []
            for row_id in row_ids:
                current = conn.execute(
                    f"SELECT * FROM {table} WHERE {primary}=?", (row_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError(f"reconciliation_delta_created_missing:{table}:{row_id}")
                created_rows.append(_row_dict(current))
            if created_rows:
                delta["created"][table] = created_rows
        return delta

    @staticmethod
    def _reconciliation_scope_digest(scope: Mapping[str, Any]) -> str:
        return stable_digest(scope)

    @staticmethod
    def _cross_audience_scope_compatible(
        left_scope: Mapping[str, Any],
        right_scope: Mapping[str, Any],
    ) -> bool:
        """Allow audience fan-out without crossing project/runtime scope."""

        def non_audience_scope(scope: Mapping[str, Any]) -> set[tuple[str, ...]]:
            projected: set[tuple[str, ...]] = set()
            for item in scope.get("bindings", ()):
                share_group, target_type, target_id, project_ref, provider, runtime_role, effect = item
                normalized_type = str(target_type or "").casefold()
                # Group/agent target_id is the audience dimension.  A project
                # target_id identifies the project scope itself and must stay
                # in the compatibility key.
                project_target = str(target_id or "") if normalized_type == "project" else ""
                projected.add((
                    str(share_group or ""), normalized_type, project_target,
                    str(project_ref or ""), str(provider or "").casefold(),
                    str(runtime_role or "").casefold(), str(effect or "").casefold(),
                ))
            return projected

        return non_audience_scope(left_scope) == non_audience_scope(right_scope)

    def _reconciliation_scope_conn(
        self,
        conn: sqlite3.Connection,
        definition_id: str,
    ) -> dict[str, Any]:
        bindings = conn.execute(
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active' "
            "ORDER BY share_group_id,binding_id",
            (definition_id,),
        ).fetchall()
        # Audience identity is a set: repeated active bindings for the same
        # audience are provenance/policy duplicates, not a distinct scope.
        binding_scope = {
            (
                str(row["share_group_id"] or ""),
                *self._binding_identity_row(row),
            )
            for row in bindings
        }
        # Source links are provenance, not audience.  Their source_kind,
        # source_ref, multiplicity, and status must never make equal active
        # bindings look like different scopes.
        return {
            "bindings": sorted(binding_scope),
        }

    def _reconciliation_decision_conn(
        self,
        conn: sqlite3.Connection,
        payload: Mapping[str, Any],
    ) -> str:
        """Insert or reactivate deterministic reconciliation decision."""
        item = dict(payload)
        decision_id = str(item["decision_id"])
        existing = conn.execute(
            "SELECT metadata_json FROM rule_decisions WHERE decision_id=?",
            (decision_id,),
        ).fetchone()
        if existing is None:
            return self.record_decision(item)
        metadata = _json_object(existing["metadata_json"])
        if str(metadata.get("undo_state") or "") != "undone":
            raise ValueError("immutable reconciliation decision conflict")
        prior_undo = metadata.get("undo_history", [])
        if not isinstance(prior_undo, list):
            prior_undo = [prior_undo]
        prior_undo.append(metadata)
        item_metadata = _json_object(item.get("metadata_json", {}))
        item_metadata["undo_history"] = prior_undo
        item["metadata_json"] = _json(item_metadata)
        columns = (
            "actor", "rule_id", "action", "before_hash", "after_hash",
            "before_json", "after_json", "reason", "undo_id",
            "metadata_json", "source_ref",
        )
        conn.execute(
            "UPDATE rule_decisions SET "
            + ",".join(f"{column}=?" for column in columns)
            + " WHERE decision_id=?",
            tuple(item.get(column, "") for column in columns) + (decision_id,),
        )
        return decision_id

    def _update_composed_definition_conn(
        self,
        conn: sqlite3.Connection,
        winner: RuleDefinition,
        body: str,
        *,
        canonical_metadata: Mapping[str, Any] | None = None,
    ) -> RuleDefinition:
        """Rebuild and atomically persist one canonical definition body."""

        composed_body = str(body or "").strip()
        if not composed_body:
            raise ValueError("canonical_composed_body_empty")
        metadata = dict(canonical_metadata or {})
        canonical_kind = str(metadata.get("rule_kind") or winner.rule_kind or "fact")
        canonical_strength = str(
            metadata.get("rule_strength") or winner.rule_strength or "observation"
        )
        canonical_confidence = float(
            metadata.get("confidence")
            if metadata.get("confidence") is not None
            else (winner.confidence or 0.0)
        )
        rebuilt = build_definition(
            composed_body,
            kind=canonical_kind,
            confidence=canonical_confidence,
            created_at=winner.created_at,
            rule_strength=canonical_strength,
        )
        now = _now()
        conn.execute(
            "UPDATE rule_definitions SET text=?,canonical_text=?,"
            "normalized_intent=?,semantic_hash=?,parameter_schema=?,"
            "polarity=?,rule_kind=?,confidence=?,revision=?,status='active',"
            "state='active',maturity_state=?,superseded_by='',updated_at=? "
            "WHERE definition_id=?",
            (
                composed_body,
                rebuilt.canonical_text,
                rebuilt.normalized_intent,
                rebuilt.semantic_hash,
                rebuilt.parameter_schema,
                # The composed body is the canonical claim surface.  Recompute
                # polarity from it so a stale historical projection cannot
                # relabel a mixed positive/safety topic by winner accident.
                str(rebuilt.polarity),
                canonical_kind,
                canonical_confidence,
                int(winner.revision or 1) + 1,
                str(metadata.get("maturity_state") or winner.maturity_state or rebuilt.maturity_state),
                now,
                winner.definition_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM rule_definitions WHERE definition_id=?",
            (winner.definition_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("canonical_winner_missing_after_composition")
        return self._definition_row(row)

    def reconcile_historical_duplicates(
        self,
        share_group_id: str,
        *,
        actor: str = "memoryguard-reconciliation",
        definition_ids: Iterable[str] | None = None,
        canonical_definition_id: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Fold safe historical duplicates in production V2 storage.

        One transaction covers selection, versions, ownership moves, alias,
        receipt, and rollback decision.  Same-audience compatibility is
        claim/topic-level through the shared composer; cross-audience folds
        are limited to exact/equivalent, non-expanding claims.  Polarity,
        strength, and noisy intent projections are canonicalized metadata,
        not duplicate gates.

        ``definition_ids`` optionally pins the candidate set so an explicit
        merge cannot fold an unrequested pair.  ``canonical_definition_id``
        refuses to write when ranking would pick a different head.
        ``dry_run`` reuses the same selection and pair-safety function on a
        read-only connection and never opens a write transaction.
        """
        group = str(share_group_id or "").strip()
        if not group:
            raise ValueError("share_group_id_required")
        requested_filter = definition_ids
        required_canonical = str(canonical_definition_id or "").strip()
        preview_only = bool(dry_run)

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            links = [
                _row_dict(row) for row in conn.execute(
                    "SELECT * FROM rule_source_links WHERE share_group_id=? "
                    "AND status='active' ORDER BY source_link_id",
                    (group,),
                ).fetchall()
            ]
            links_by_definition: dict[str, list[dict[str, Any]]] = {}
            for link in links:
                definition_id = str(
                    link.get("canonical_definition_id")
                    or link.get("original_definition_id") or ""
                )
                if definition_id:
                    links_by_definition.setdefault(definition_id, []).append(link)

            reachable_ids = {
                str(row["definition_id"] or "")
                for row in conn.execute(
                    "SELECT DISTINCT definition_id FROM rule_bindings "
                    "WHERE share_group_id=? AND status='active' AND effect='include'",
                    (group,),
                ).fetchall()
            }
            reachable_ids.update(links_by_definition)
            definition_rows = [
                row for row in conn.execute(
                    "SELECT * FROM rule_definitions WHERE status='active' "
                    "ORDER BY definition_id"
                ).fetchall()
                if str(row["definition_id"] or "") in reachable_ids
            ]
            if requested_filter is not None:
                if isinstance(requested_filter, (str, bytes)):
                    raise ValueError("rule_merge_definition_ids_required")
                try:
                    requested_ids = {
                        str(item or "").strip()
                        for item in requested_filter
                        if str(item or "").strip()
                    }
                except TypeError as exc:
                    raise ValueError("rule_merge_definition_ids_required") from exc
                if not requested_ids:
                    raise ValueError("rule_merge_definition_ids_required")
                present = {str(row["definition_id"] or "") for row in definition_rows}
                if requested_ids - present:
                    raise ValueError("rule_merge_definition_not_found")
                definition_rows = [
                    row for row in definition_rows
                    if str(row["definition_id"] or "") in requested_ids
                ]
                if required_canonical and required_canonical not in requested_ids:
                    raise ValueError("rule_merge_canonical_mismatch")
            definitions = [self._definition_row(row) for row in definition_rows]
            definition_bodies = {
                str(row["definition_id"]): str(
                    row["text"] or row["canonical_text"] or ""
                ).strip()
                for row in definition_rows
            }

            # Build conservative pairwise-safe clusters.  Exact compatibility
            # keys miss additive claims; a transitive semantic cluster can also
            # swallow a conflict.  Every pair must pass the shared topic
            # composer plus the hard audience-scope gate.  Automatic
            # reconciliation still keeps equal/equivalent claims on opposite
            # injection channels; an explicit mandatory head may absorb a
            # relevant exact/equivalent/update duplicate.  Additive topic
            # members still fold and canonicalize stored metadata.
            clusters: list[list[RuleDefinition]] = []
            required_canonical_layer = ""
            if required_canonical:
                for item in definitions:
                    if item.definition_id == required_canonical:
                        required_canonical_layer = _runtime_injection_layer(item)
                        break

            def _safe_relevant_into_mandatory(
                left: RuleDefinition,
                right: RuleDefinition,
                relation_kind: str,
            ) -> bool:
                if not required_canonical or required_canonical_layer != "mandatory":
                    return False
                if relation_kind not in {"exact", "equivalent", "update"}:
                    return False
                layers = {
                    _runtime_injection_layer(left),
                    _runtime_injection_layer(right),
                }
                return layers == {"mandatory", "relevant"}

            def pair_result(
                left: RuleDefinition, right: RuleDefinition,
            ) -> Any | None:
                left_scope = self._reconciliation_scope_conn(
                    conn, left.definition_id,
                )
                right_scope = self._reconciliation_scope_conn(
                    conn, right.definition_id,
                )
                same_scope = left_scope == right_scope
                left_share_groups = {
                    str(item[0] or "")
                    for item in left_scope.get("bindings", ())
                }
                right_share_groups = {
                    str(item[0] or "")
                    for item in right_scope.get("bindings", ())
                }
                same_share_groups = left_share_groups == right_share_groups
                left_body = definition_bodies.get(
                    left.definition_id, left.canonical_text,
                )
                right_body = definition_bodies.get(
                    right.definition_id, right.canonical_text,
                )
                topic_related = _claims_share_canonical_topic(left_body, right_body)
                relation_kind = _canonical_relation_kind(left_body, right_body)
                layers_differ = (
                    _runtime_injection_layer(left)
                    != _runtime_injection_layer(right)
                )
                safe_relevant_fold = _safe_relevant_into_mandatory(
                    left, right, relation_kind,
                )
                if (
                    layers_differ
                    and relation_kind in {"exact", "equivalent", "update"}
                    and not safe_relevant_fold
                ):
                    return None
                result = _compose_canonical_bodies([
                    left_body,
                    right_body,
                ])
                if (
                    not topic_related
                    or getattr(result, "rejected_conflicts", ())
                    or getattr(result, "rejected_unrelated", ())
                    or not getattr(result, "claims", ())
                ):
                    return None
                if layers_differ and not safe_relevant_fold:
                    left_count = len(
                        _compose_canonical_bodies([left_body]).claims or (),
                    )
                    right_count = len(
                        _compose_canonical_bodies([right_body]).claims or (),
                    )
                    if len(result.claims) <= max(left_count, right_count):
                        return None
                if same_scope:
                    # Same audience may safely retain additive claims within
                    # one topic, subject to the composer and topic predicate.
                    return result if topic_related else None

                # Across different audiences, only an exact/equivalent claim
                # may be canonicalized.  Compare the composed claim count to
                # each single-source composition so the fold cannot expand one
                # audience's content into another audience's additive topic.
                if not same_share_groups:
                    return None
                if not self._cross_audience_scope_compatible(left_scope, right_scope):
                    return None
                if relation_kind not in {"exact", "equivalent"}:
                    return None
                left_single = _compose_canonical_bodies([left_body])
                right_single = _compose_canonical_bodies([right_body])
                if (
                    left_single.rejected_conflicts
                    or left_single.rejected_unrelated
                    or right_single.rejected_conflicts
                    or right_single.rejected_unrelated
                    or not left_single.claims
                    or not right_single.claims
                    or len(result.claims) > max(
                        len(left_single.claims), len(right_single.claims),
                    )
                ):
                    return None
                return result

            for definition in definitions:
                chosen: list[RuleDefinition] | None = None
                for cluster in clusters:
                    candidate = [*cluster, definition]
                    if all(
                        pair_result(left, right) is not None
                        for index, left in enumerate(candidate)
                        for right in candidate[index + 1:]
                    ):
                        chosen = cluster
                        break
                if chosen is None:
                    clusters.append([definition])
                else:
                    chosen.append(definition)

            merged = 0
            conflicts = 0
            details: list[dict[str, Any]] = []
            rejected_pairs: set[tuple[str, str]] = set()
            # Preserve an explicit diagnostic for same-topic siblings that
            # fail a scope/semantic policy.  They are intentionally kept in
            # separate clusters, so counting only inside the merge loop would
            # lose the conflict entirely.
            for index, left in enumerate(definitions):
                for right in definitions[index + 1:]:
                    rough_same_intent = _claims_share_canonical_topic(
                        definition_bodies.get(
                            left.definition_id, left.canonical_text,
                        ),
                        definition_bodies.get(
                            right.definition_id, right.canonical_text,
                        ),
                    )
                    if rough_same_intent and pair_result(left, right) is None:
                        pair_key = tuple(sorted((left.definition_id, right.definition_id)))
                        rejected_pairs.add(pair_key)
                        conflicts += 1
                        left_scope = self._reconciliation_scope_conn(
                            conn, left.definition_id,
                        )
                        right_scope = self._reconciliation_scope_conn(
                            conn, right.definition_id,
                        )
                        details.append({
                            "status": "preserved_conflict",
                            "winner": left.definition_id,
                            "candidate": right.definition_id,
                            "reason": (
                                "scope_mismatch"
                                if left_scope != right_scope
                                else "governance_conflict"
                            ),
                            "winner_scope_digest": self._reconciliation_scope_digest(left_scope),
                            "candidate_scope_digest": self._reconciliation_scope_digest(right_scope),
                        })
            for candidates in sorted(
                clusters,
                key=lambda items: tuple(item.definition_id for item in items),
            ):
                if len(candidates) < 2:
                    continue
                ordered = sorted(
                    candidates,
                    key=lambda definition: (
                        -canonical_rule_strength_priority(definition.rule_strength),
                        -canonical_rule_kind_priority(definition.rule_kind),
                        -float(definition.confidence or 0.0),
                        -int(definition.revision or 0),
                        str(definition.definition_id),
                    ),
                )
                winner = ordered[0]
                if required_canonical and winner.definition_id != required_canonical:
                    raise ValueError("rule_merge_canonical_mismatch")
                for loser in ordered[1:]:
                    winner_scope = self._reconciliation_scope_conn(
                        conn, winner.definition_id,
                    )
                    loser_scope = self._reconciliation_scope_conn(
                        conn, loser.definition_id,
                    )
                    pair = pair_result(winner, loser)
                    if pair is None:
                        pair_key = tuple(sorted((winner.definition_id, loser.definition_id)))
                        if pair_key not in rejected_pairs:
                            conflicts += 1
                            rejected_pairs.add(pair_key)
                        details.append({
                            "status": "preserved_conflict",
                            "winner": winner.definition_id,
                            "candidate": loser.definition_id,
                            "reason": (
                                "scope_mismatch"
                                if loser_scope != winner_scope
                                else "governance_conflict"
                            ),
                            "winner_scope_digest": self._reconciliation_scope_digest(winner_scope),
                            "candidate_scope_digest": self._reconciliation_scope_digest(loser_scope),
                        })
                        continue
                    relation_kind = _canonical_relation_kind(
                        definition_bodies.get(
                            winner.definition_id, winner.canonical_text,
                        ),
                        definition_bodies.get(
                            loser.definition_id, loser.canonical_text,
                        ),
                    )
                    if preview_only:
                        composed = _compose_canonical_bodies([
                            definition_bodies.get(
                                winner.definition_id, winner.canonical_text,
                            ),
                            definition_bodies.get(
                                loser.definition_id, loser.canonical_text,
                            ),
                        ])
                        if (
                            composed.rejected_conflicts
                            or composed.rejected_unrelated
                            or not composed.claims
                        ):
                            raise ValueError("canonical_composition_rejected")
                        definition_bodies[winner.definition_id] = str(
                            composed.body or ""
                        ).strip()
                        merged += 1
                        details.append({
                            "status": "merged",
                            "winner": winner.definition_id,
                            "merged": loser.definition_id,
                            "relation": relation_kind,
                            "reason": "historical_duplicate_fold",
                        })
                        continue
                    decision_id = stable_digest((
                        "historical-reconciliation", group,
                        winner.definition_id, loser.definition_id,
                    ))
                    source_ref = f"reconciliation:{decision_id}"
                    winner_before = self._reconciliation_before_conn(
                        conn, winner.definition_id,
                    )
                    loser_before = self._reconciliation_before_conn(
                        conn, loser.definition_id,
                    )
                    before_delta = {
                        table: {
                            **winner_before.get(table, {}),
                            **loser_before.get(table, {}),
                        }
                        for table in set(winner_before) | set(loser_before)
                    }
                    source_rule_id = str(
                        links_by_definition.get(loser.definition_id, [{}])[0]
                        .get("memory_id", "")
                    )
                    winner_snapshot = winner.to_dict()
                    loser_snapshot = loser.to_dict()
                    winner_version_id = stable_digest((
                        "historical-reconciliation-winner-version", decision_id,
                    ))
                    loser_version_id = stable_digest((
                        "historical-reconciliation-loser-version", decision_id,
                    ))
                    receipt_id = stable_digest((
                        "historical-reconciliation-receipt", decision_id,
                    ))
                    self.record_definition_version(
                        winner.definition_id,
                        snapshot=winner_snapshot,
                        reason="historical_duplicate_fold_winner",
                        actor=actor,
                        source_ref=source_ref,
                        version_id=winner_version_id,
                    )
                    self.record_definition_version(
                        loser.definition_id,
                        snapshot=loser_snapshot,
                        reason="historical_duplicate_fold_loser",
                        actor=actor,
                        source_ref=source_ref,
                        version_id=loser_version_id,
                    )
                    composed = _compose_canonical_bodies([
                        definition_bodies.get(
                            winner.definition_id, winner.canonical_text,
                        ),
                        definition_bodies.get(
                            loser.definition_id, loser.canonical_text,
                        ),
                    ])
                    if (
                        composed.rejected_conflicts
                        or composed.rejected_unrelated
                        or not composed.claims
                    ):
                        raise ValueError("canonical_composition_rejected")
                    winner = self._update_composed_definition_conn(
                        conn,
                        winner,
                        str(composed.body or ""),
                        canonical_metadata=_canonical_definition_metadata(
                            (winner, loser),
                        ),
                    )
                    definition_bodies[winner.definition_id] = str(
                        composed.body or ""
                    ).strip()
                    self._canonicalize_binding_policy_conn(
                        conn, winner.definition_id, loser.definition_id,
                    )
                    migrated = self._migrate_legacy_definition_conn(
                        conn, loser.definition_id, winner.definition_id,
                        migration_decision_id=decision_id,
                        source_rule_id=source_rule_id,
                    )
                    if migrated is None:
                        continue
                    self.record_receipt({
                        "receipt_id": receipt_id,
                        "source_ref": source_ref,
                        "source_rule_id": loser.definition_id,
                        "definition_id": winner.definition_id,
                        "share_group_id": group,
                        "metadata_json": _json({
                            "status": "accepted",
                            "reason": "historical_duplicate_fold",
                        }),
                    })
                    delta = self._reconciliation_after_conn(
                        conn,
                        before_delta,
                        {
                            "rule_definition_versions": [
                                winner_version_id, loser_version_id,
                            ],
                            "rule_definition_aliases": [loser.definition_id],
                            "rule_receipt_refs": [receipt_id],
                        },
                    )
                    decision_payload = {
                        "decision_id": decision_id,
                        "actor": actor,
                        "rule_id": loser.definition_id,
                        "action": "historical_duplicate_fold",
                        "before_hash": stable_digest({
                            "winner": winner_snapshot,
                            "loser": loser_snapshot,
                        }),
                        "after_hash": stable_digest({
                            "definition_id": winner.definition_id,
                            "body": composed.body,
                        }),
                        "before_json": _json({
                            "winner": winner_snapshot,
                            "loser": loser_snapshot,
                        }),
                        "after_json": _json({
                            "definition_id": winner.definition_id,
                            "body": composed.body,
                            "claims": list(composed.claims),
                        }),
                        "reason": "historical_duplicate_fold",
                        "undo_id": decision_id,
                        "source_ref": source_ref,
                        "metadata_json": _json({
                            "reconciliation": True,
                            "undo_state": "active",
                            "share_group_id": group,
                            "winner_definition_id": winner.definition_id,
                            "loser_definition_id": loser.definition_id,
                            "source_scope": winner_scope,
                        "inverse_delta": delta,
                            "winner_before": winner_snapshot,
                            "loser_before": loser_snapshot,
                        }),
                    }
                    self._reconciliation_decision_conn(conn, decision_payload)
                    merged += 1
                    details.append({
                        "status": "merged",
                        "winner": winner.definition_id,
                        "merged": loser.definition_id,
                        "decision_id": decision_id,
                        "undo_id": decision_id,
                        "relation": relation_kind,
                        "reason": "historical_duplicate_fold",
                    })
            return {
                "share_group_id": group,
                "merged_count": merged,
                "conflict_count": conflicts,
                "details": details,
            }

        if preview_only:
            return self._read(op)
        return self._write(op)

    def undo_historical_duplicate(
        self,
        decision_id: str,
        *,
        share_group_id: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        """Apply persisted inverse delta for one historical fold.

        Current rows must still equal recorded ``after`` rows.  This narrow
        optimistic check prevents undo from overwriting later evidence or
        governance changes.  All restoration/deletion/decision marking stays
        in one transaction.
        """
        token = str(decision_id or "").strip()
        if not token:
            raise ValueError("historical_reconciliation_decision_required")

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM rule_decisions WHERE decision_id=?",
                (token,),
            ).fetchone()
            if row is None:
                raise ValueError("historical_reconciliation_decision_not_found")
            if str(row["action"] or "") != "historical_duplicate_fold":
                raise ValueError("historical_reconciliation_action_invalid")
            metadata = _json_object(row["metadata_json"])
            if not metadata.get("reconciliation"):
                raise ValueError("historical_reconciliation_inverse_missing")
            expected_group = str(metadata.get("share_group_id") or "")
            requested_group = str(share_group_id or "")
            if requested_group and requested_group != expected_group:
                raise ValueError("historical_reconciliation_scope_mismatch")
            if metadata.get("undo_state") == "undone":
                return {
                    "status": "undone",
                    "already_undone": True,
                    "decision_id": token,
                }
            delta = metadata.get("inverse_delta")
            if not isinstance(delta, Mapping):
                raise ValueError("historical_reconciliation_inverse_missing")
            primary_keys = self._reconciliation_primary_keys()

            def same_row(current: sqlite3.Row, expected: Mapping[str, Any]) -> bool:
                return all(current[key] == value for key, value in expected.items())

            # Validate every optimistic after-image before changing anything.
            for table, pairs in _json_object(delta.get("updated", {})).items():
                if table not in primary_keys or not isinstance(pairs, list):
                    raise ValueError("historical_reconciliation_inverse_invalid")
                primary = primary_keys[table]
                for pair in pairs:
                    if not isinstance(pair, Mapping):
                        raise ValueError("historical_reconciliation_inverse_invalid")
                    after = pair.get("after")
                    if not isinstance(after, Mapping):
                        raise ValueError("historical_reconciliation_inverse_invalid")
                    row_id = str(after.get(primary) or "")
                    current = conn.execute(
                        f"SELECT * FROM {table} WHERE {primary}=?", (row_id,),
                    ).fetchone()
                    if current is None or not same_row(current, after):
                        raise RuntimeError(
                            f"historical_reconciliation_external_change:{table}:{row_id}"
                        )
            for table, created_rows in _json_object(delta.get("created", {})).items():
                if table not in primary_keys or not isinstance(created_rows, list):
                    raise ValueError("historical_reconciliation_inverse_invalid")
                primary = primary_keys[table]
                for expected in created_rows:
                    if not isinstance(expected, Mapping):
                        raise ValueError("historical_reconciliation_inverse_invalid")
                    row_id = str(expected.get(primary) or "")
                    current = conn.execute(
                        f"SELECT * FROM {table} WHERE {primary}=?", (row_id,),
                    ).fetchone()
                    if current is None or not same_row(current, expected):
                        raise RuntimeError(
                            f"historical_reconciliation_external_change:{table}:{row_id}"
                        )

            # Remove only merge-generated identity/ledger rows.  Decision row
            # stays as an immutable undo audit marker and replay fence.
            for table, created_rows in _json_object(delta.get("created", {})).items():
                if table == "rule_decisions":
                    continue
                primary = primary_keys[table]
                for expected in created_rows:
                    conn.execute(
                        f"DELETE FROM {table} WHERE {primary}=?",
                        (str(expected[primary]),),
                    )
            for table, pairs in _json_object(delta.get("updated", {})).items():
                primary = primary_keys[table]
                for pair in pairs:
                    before = dict(pair["before"])
                    row_id = str(before[primary])
                    assignments = [key for key in before if key != primary]
                    conn.execute(
                        f"UPDATE {table} SET "
                        + ",".join(f"{key}=?" for key in assignments)
                        + f" WHERE {primary}=?",
                        tuple(before[key] for key in assignments) + (row_id,),
                    )
            metadata["undo_state"] = "undone"
            metadata["undo_actor"] = str(actor or "")
            metadata["undone_at"] = _now()
            conn.execute(
                "UPDATE rule_decisions SET metadata_json=? WHERE decision_id=?",
                (_json(metadata), token),
            )
            return {"status": "undone", "decision_id": token}

        return self._write(op)

    def record_canonical_state(self, value: Mapping[str, Any]) -> str:
        value = dict(value)
        # Historical writers used BLOCKED as a diagnostic/readiness state even
        # when the native generation was valid but not activated.  The native
        # status contract treats that state as a readable shadow generation;
        # retain BLOCKED for legacy-path rows, but normalize canonical paths so
        # the status endpoint is not spuriously blocked by activation metadata.
        path = str(value.get("read_path") or "").strip().casefold().replace("_", "-")
        if (
            str(value.get("activation_status") or "").casefold() == "blocked"
            and path in {
                "rule-intelligence", "ruleintelligence", "rule-intelligence-v2",
                "rule-v2", "v2", "native", "native-v2", "nativev2", "rules",
            }
        ):
            value["activation_status"] = "shadow"
        value.setdefault(
            "scope_id",
            stable_digest((value.get("share_group_id", ""), value.get("source_ref", ""))),
        )
        return self._insert_generic("rule_canonical_state", "scope_id", value)

    def record_reconciliation_job(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("job_id", stable_digest(value)); return self._insert_generic("rule_reconciliation_jobs", "job_id", value)

    def record_projection_checkpoint(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("checkpoint_id", stable_digest(value)); return self._insert_generic("rule_projection_checkpoints", "checkpoint_id", value)

    def get_idempotency_fence(self, share_group_id: str, key: str) -> dict[str, Any] | None:
        row = self._read(lambda conn: conn.execute(
            "SELECT * FROM rule_idempotency_fences WHERE share_group_id=? AND key=?",
            (str(share_group_id or ""), str(key or "")),
        ).fetchone())
        return None if row is None else {str(name): row[name] for name in row.keys()}

    def record_idempotency_fence(self, value: Mapping[str, Any]) -> str:
        value = dict(value)
        value.setdefault("fence_id", stable_digest((value.get("share_group_id", ""), value.get("key", ""))))
        natural = (str(value.get("share_group_id", "")), str(value.get("key", "")))
        existing = self._read(lambda conn: conn.execute("SELECT * FROM rule_idempotency_fences WHERE share_group_id=? AND key=?", natural).fetchone())
        if existing is not None and str(existing["fence_id"]) != str(value["fence_id"]):
            raise ValueError(f"immutable rule_idempotency_fences natural-key conflict for key={natural[1]}")
        return self._insert_generic("rule_idempotency_fences", "fence_id", value)

    def record_idempotency_fence_anomaly(self, value: Mapping[str, Any]) -> str:
        """Record a preserved immutable-fence conflict without replacing either row."""
        value = dict(value)
        value.setdefault("anomaly_id", stable_digest((
            value.get("migration_id", ""), value.get("source_group_id", ""),
            value.get("source_key", ""), value.get("payload_digest", ""),
        )))
        return self._insert_generic("rule_idempotency_fence_anomalies", "anomaly_id", value)

    def record_decision_anomaly(self, value: Mapping[str, Any]) -> str:
        """Record a preserved cross-table decision identity conflict."""
        value = dict(value)
        value.setdefault("anomaly_id", stable_digest((
            value.get("migration_id", ""), value.get("source_group_id", ""),
            value.get("source_table", ""), value.get("original_decision_id", ""),
            value.get("payload_digest", ""),
        )))
        return self._insert_generic("rule_decision_anomalies", "anomaly_id", value)

    def record_migration_map(self, value: Mapping[str, Any]) -> str:
        value = dict(value)
        natural = (
            str(value.get("migration_id", "")), str(value.get("source_kind", "")),
            str(value.get("source_group_id", "")), str(value.get("source_table", "")),
            str(value.get("source_id", "")), str(value.get("target_table", "")),
        )

        def op(conn: sqlite3.Connection) -> str:
            existing = conn.execute(
                "SELECT map_id,target_id FROM rule_migration_map WHERE migration_id=? AND source_kind=? AND source_group_id=? AND source_table=? AND source_id=? AND target_table=?",
                natural,
            ).fetchone()
            if existing is not None:
                old_target = str(existing[1] or "")
                new_target = str(value.get("target_id", "") or "")
                if old_target != new_target:
                    raise ValueError(f"immutable rule_migration_map conflict for source_id={natural[4]}: target_id {old_target!r} != {new_target!r}")
                # Resume partial/legacy rows verbatim; do not rewrite old
                # metadata or mint a map_id from changed source bytes.
                return str(existing[0])
            value.setdefault("map_id", stable_digest(value))
            return self._insert_generic("rule_migration_map", "map_id", value, conn=conn)

        return str(self._write(op))

    def record_unknown_column(self, value: Mapping[str, Any]) -> str:
        value = dict(value); value.setdefault("ledger_id", stable_digest(value)); return self._insert_natural_exact("rule_unknown_columns_ledger", "ledger_id", ("migration_id", "source_path", "source_table", "source_row_id", "column_name"), value)

    def record_unknown_column_anomaly(self, value: Mapping[str, Any]) -> str:
        value = dict(value)
        # Include the occurrence identity when available.  A column-level
        # anomaly key collapses duplicate source rows and makes preservation
        # evidence non-auditable; the ledger itself is occurrence-bound.
        value.setdefault("anomaly_id", stable_digest((value.get("migration_id", ""), value.get("source_path", ""), value.get("source_table", ""), value.get("column_name", ""), value.get("legacy_ledger_id", ""), value.get("source_row_id", ""))))
        return self._insert_generic("rule_unknown_column_anomalies", "anomaly_id", value)

    # ---- outboxes ----------------------------------------------------
    def append_domain_outbox(self, value: Mapping[str, Any]) -> str:
        value = dict(value)
        value.setdefault("event_id", stable_digest(value))
        value.setdefault("payload_digest", stable_digest(value.get("payload_json", {})))

        def op(conn: sqlite3.Connection) -> str:
            event_id = str(value["event_id"])
            row = conn.execute("SELECT * FROM rule_domain_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is not None:
                # A later migration batch may carry a new migration_id for the
                # same immutable source event.  Preserve the original batch
                # marker while rejecting any actual event mutation.
                immutable = ("event_type", "source_kind", "source_group_id", "source_ref", "payload_digest", "payload_json", "created_at")
                mismatches = [(col, row[col], value.get(col, "")) for col in immutable if row[col] != value.get(col, "")]
                if mismatches:
                    raise ValueError(f"immutable rule_domain_outbox conflict for event_id={event_id}: {mismatches}")
                return event_id
            return self._insert_generic("rule_domain_outbox", "event_id", value, conn=conn)

        return str(self._write(op))

    def append_evidence_outbox(self, value: Mapping[str, Any]) -> str:
        value = dict(value)
        # The evidence event identity is source/evidence scoped, not batch
        # scoped.  A later migration that discovers another group must replay
        # the same source evidence without minting a duplicate event.
        value.setdefault("event_id", stable_digest((value.get("evidence_id", ""), value.get("polarity", "positive"), value.get("evidence_ref", ""), value.get("content_digest", ""))))

        def op(conn: sqlite3.Connection) -> str:
            natural = (str(value.get("migration_id", "")), str(value.get("evidence_id", "")), str(value.get("polarity", "positive")))
            natural_row = conn.execute("SELECT * FROM rule_evidence_outbox WHERE migration_id=? AND evidence_id=? AND polarity=?", natural).fetchone()
            if natural_row is not None and str(natural_row["event_id"]) != str(value["event_id"]):
                immutable = ("evidence_id", "definition_id", "evidence_ref", "content_digest", "polarity", "source_kind", "source_group_id", "payload_json")
                mismatches = [(col, natural_row[col], value.get(col, "")) for col in immutable if natural_row[col] != value.get(col, "")]
                if mismatches:
                    raise ValueError(f"immutable rule_evidence_outbox natural-key conflict: {mismatches}")
                return str(natural_row["event_id"])
            event_id = str(value["event_id"])
            row = conn.execute("SELECT * FROM rule_evidence_outbox WHERE event_id=?", (event_id,)).fetchone()
            if row is not None:
                immutable = ("evidence_id", "definition_id", "evidence_ref", "content_digest", "polarity", "source_kind", "source_group_id", "payload_json")
                mismatches = [(col, row[col], value.get(col, "")) for col in immutable if row[col] != value.get(col, "")]
                if mismatches:
                    raise ValueError(f"immutable rule_evidence_outbox conflict for event_id={event_id}: {mismatches}")
                return event_id
            return self._insert_generic("rule_evidence_outbox", "event_id", value, conn=conn)

        return str(self._write(op))

    def list_evidence_outbox(self, *, migration_id: str | None = None, unconsumed: bool = True) -> list[dict[str, Any]]:
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            clauses, params = ["1=1"], []
            if migration_id is not None: clauses.append("migration_id=?"); params.append(migration_id)
            if unconsumed: clauses.append("(consumed_at='' OR consumed_at IS NULL)")
            return [_row_dict(r) for r in conn.execute("SELECT * FROM rule_evidence_outbox WHERE " + " AND ".join(clauses) + " ORDER BY event_id", params).fetchall()]
        return self._read(op)

    def mark_evidence_consumed(self, event_id: str, *, consumed_at: str | None = None) -> None:
        self._write(lambda c: c.execute("UPDATE rule_evidence_outbox SET consumed_at=? WHERE event_id=?", (consumed_at or _now(), event_id)))

    def mark_evidence_consumed_batch(self, event_ids: Sequence[str], *, consumed_at: str | None = None) -> int:
        """Mark bounded evidence batch consumed in one rules transaction."""
        ids = [str(item) for item in event_ids if str(item)]
        if not ids:
            return 0
        stamp = consumed_at or _now()
        def op(conn: sqlite3.Connection) -> int:
            placeholders = ",".join("?" for _ in ids)
            cur = conn.execute(f"UPDATE rule_evidence_outbox SET consumed_at=? WHERE event_id IN ({placeholders}) AND (consumed_at='' OR consumed_at IS NULL)", (stamp, *ids))
            return int(cur.rowcount)
        return int(self._write(op))

    def metrics(self) -> dict[str, Any]:
        names = ("rule_definitions", "rule_bindings", "rule_binding_contributions", "rule_evidence_refs", "rule_negative_evidence_refs", "rule_evidence_outbox", "rule_unknown_columns_ledger", "rule_unknown_column_anomalies", "rule_agent_reputation", "rule_project_profile", "rule_runtime_stats", "rule_evidence_contributions", "rule_evidence_effective", "rule_governance_capabilities", "rule_idempotency_fences", "rule_idempotency_fence_anomalies", "rule_decision_anomalies")
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            result = {name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]) for name in names}
            result["definitions"] = result["rule_definitions"]; result["bindings"] = result["rule_bindings"]
            result["binding_contributions"] = result["rule_binding_contributions"]
            result["unconsumed_evidence_outbox"] = int(conn.execute("SELECT COUNT(*) FROM rule_evidence_outbox WHERE consumed_at='' OR consumed_at IS NULL").fetchone()[0])
            result["system_auto_expansion"] = int(conn.execute("SELECT COUNT(*) FROM rule_bindings WHERE target_type IN ('system','group','provider','runtime_role') AND created_by IN ('auto','backfill')").fetchone()[0])
            return result
        return self._read(op)

    def integrity(self) -> dict[str, Any]:
        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            integrity = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
            foreign = [_row_dict(r) for r in conn.execute("PRAGMA foreign_key_check").fetchall()]
            return {"integrity": integrity, "foreign_keys": foreign, "ok": integrity == ["ok"] and not foreign}
        return self._read(op)


RuleV2Store.Store = RuleV2Store
RuleStore = RuleV2Store


class EvidenceProjector:
    """Idempotent projector from rules outbox to an external evidence sink."""

    def __init__(self, store: RuleV2Store, sink: EvidenceSink | Callable[[Mapping[str, Any]], Any]):
        self.store = store
        self.sink = sink

    def _write(self, reference: Mapping[str, Any]) -> Any:
        if callable(self.sink):
            return self.sink(reference)
        writer = getattr(self.sink, "write", None) or getattr(self.sink, "append", None) or getattr(self.sink, "project", None)
        if not callable(writer):
            raise TypeError("evidence sink must be callable or expose write/append/project")
        return writer(reference)

    @staticmethod
    def _reference_to_evidence_event(reference: Mapping[str, Any]) -> dict[str, Any]:
        source_ref = str(reference.get("source_ref") or reference.get("evidence_ref") or "")
        if not source_ref:
            raise ValueError("rule evidence reference has no source_ref")
        forbidden = {"body", "raw_content", "content", "text", "transcript", "full_transcript", "evidence", "payload", "payload_json"}
        metadata = {str(key): value for key, value in reference.items() if str(key).casefold() not in forbidden and str(key) not in {"migration_id", "event_id", "created_at", "updated_at", "consumed_at", "evidence_id", "source_ref", "evidence_ref", "content_digest", "digest", "revision", "observed_at", "authority", "status", "subject_type", "subject_id", "definition_id", "relation", "link_metadata"}}
        evidence_id = str(reference.get("evidence_id") or "")
        return {
            "event_id": str(reference.get("event_id") or evidence_id or stable_digest(reference)),
            "aggregate_id": str(reference.get("definition_id") or evidence_id or "migration"),
            "payload": {
                "evidence": {
                    "evidence_id": evidence_id,
                    "source_ref": source_ref,
                    "revision": str(reference.get("revision") or reference.get("observed_at") or ""),
                    "digest": str(reference.get("content_digest") or reference.get("digest") or ""),
                    "authority": "rule_migration",
                    "status": "valid",
                    "metadata": metadata,
                },
                "subject_type": str(reference.get("subject_type") or "rule"),
                "subject_id": str(reference.get("subject_id") or reference.get("definition_id") or evidence_id or "migration"),
                "relation": str(reference.get("relation") or "supports"),
                "link_metadata": dict(reference.get("link_metadata") or {}),
            },
        }

    def _write_batch(self, references: Sequence[Mapping[str, Any]]) -> bool:
        """Use sink batch API or adapt coordinator EvidenceStore closure."""
        batch_writer = getattr(self.sink, "write_batch", None) or getattr(self.sink, "append_batch", None) or getattr(self.sink, "project_batch", None)
        if callable(batch_writer):
            batch_writer(list(references))
            return True
        if not callable(self.sink):
            return False
        # Coordinator supplies a closure around EvidenceStore.  Reuse its
        # idempotent one-transaction projector without changing coordinator
        # wiring or public sink semantics.
        try:
            closure = inspect.getclosurevars(self.sink).nonlocals
        except (TypeError, ValueError):
            closure = {}
        evidence_store = next((value for value in closure.values() if callable(getattr(value, "project_batch", None))), None)
        if evidence_store is None:
            return False
        evidence_store.project_batch([self._reference_to_evidence_event(reference) for reference in references])
        return True

    def project(self, *, migration_id: str | None = None, limit: int | None = None) -> dict[str, Any]:
        rows = self.store.list_evidence_outbox(migration_id=migration_id, unconsumed=True)
        if limit is not None: rows = rows[: max(0, int(limit))]
        consumed = 0
        batch_size = 100
        for offset in range(0, len(rows), batch_size):
            batch_rows = rows[offset : offset + batch_size]
            references = []
            for row in batch_rows:
                payload = _json_object(row.get("payload_json"))
                ref = {key: value for key, value in row.items() if key not in {"payload_json", "consumed_at"}}
                ref.update(payload)
                references.append(ref)
            try:
                used_batch = self._write_batch(references)
                if not used_batch:
                    for reference in references:
                        self._write(reference)
                # Mark consumed only after whole sink batch succeeds.  A
                # failure here is surfaced uniformly and leaves rows pending.
                marked = self.store.mark_evidence_consumed_batch([str(row["event_id"]) for row in batch_rows])
            except Exception as exc:
                raise EvidenceProjectionError(str(exc)) from exc
            consumed += marked
        return {"seen": len(rows), "consumed": consumed, "pending": len(self.store.list_evidence_outbox(migration_id=migration_id, unconsumed=True))}


__all__ = [
    "EvidenceProjectionError", "EvidenceProjector", "EvidenceSink", "RULES_SCHEMA_MARKER", "RULES_SCHEMA_VERSION",
    "RuleStore", "RuleV2Store", "canonical_migration_source_id", "stable_digest",
]
