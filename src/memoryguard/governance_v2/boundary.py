"""Governed V2 mutation facade.

This module is intentionally the only high-level writer used by Phase-3
callers.  It performs scope checks before delegating to the domain stores and
keeps a compact, body-free decision ledger for compensating undo.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import secrets
import time
from typing import Any, Mapping, Sequence

from ..evidence import Evidence, EvidenceLink, EvidenceStore
from ..memory import MemoryAtom, MemoryAtomStore
from .context import V2ContextError, V2GovernanceError, V2MutationContext, V2ScopeError


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class V2Decision:
    decision_id: str
    operation: str
    target: Mapping[str, Any]
    reason: str
    confidence: float
    undo_hash: str
    context: Mapping[str, Any]
    status: str = "applied"
    created_at: str = ""
    before: Mapping[str, Any] = None  # type: ignore[assignment]
    after: Mapping[str, Any] = None  # type: ignore[assignment]
    idempotency_key: str = ""
    request_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "operation": self.operation,
            "target": dict(self.target),
            "reason": self.reason,
            "confidence": self.confidence,
            "undo_hash": self.undo_hash,
            "context": dict(self.context),
            "status": self.status,
            "created_at": self.created_at,
            "before": dict(self.before or {}),
            "after": dict(self.after or {}),
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.request_fingerprint,
        }

    as_dict = to_dict


@dataclass(frozen=True)
class _RequestClaim:
    """Durable request fence held by one mutation attempt."""

    actor: str
    key: str
    token: str


class V2GovernanceBoundary:
    """Context-checked writer over MemoryAtomStore and EvidenceStore."""

    def __init__(self, workspace: str | Path, *, memory_store: MemoryAtomStore | None = None, evidence_store: EvidenceStore | None = None) -> None:
        self.workspace = Path(workspace).expanduser().absolute()
        self.memory = memory_store or MemoryAtomStore(self.workspace)
        self.evidence = evidence_store or EvidenceStore(self.workspace)
        self.ledger_path = self.workspace / ".memoryguard" / "governance_v2" / "decisions.db"
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_ledger()

    def _connect_ledger(self, *, timeout: float = 30.0) -> sqlite3.Connection:
        from .atomic import ledger_connection

        return ledger_connection(self.ledger_path) or sqlite3.connect(self.ledger_path, timeout=timeout)

    @contextmanager
    def atomic_memory(self):
        """Keep related mutations, publication and their receipts indivisible."""
        from .atomic import memory_publication

        with memory_publication(self.memory.db_path, self.ledger_path, self.evidence.db_path) as owner:
            yield owner

    def _init_ledger(self) -> None:
        from .atomic import ledger_connection

        # The publication owner already initialized the attached ledger.
        # Unqualified CREATE TABLE on its borrowed connection would create
        # shadow ledger tables in the memory database instead.
        if ledger_connection(self.ledger_path) is not None:
            return
        conn = self._connect_ledger()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS decisions(decision_id TEXT PRIMARY KEY,operation TEXT NOT NULL,target_json TEXT NOT NULL,reason TEXT NOT NULL,confidence REAL NOT NULL,undo_hash TEXT NOT NULL,context_json TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,idempotency_key TEXT NOT NULL DEFAULT '',request_fingerprint TEXT NOT NULL DEFAULT '')"
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(decisions)")}
            if "idempotency_key" not in columns:
                conn.execute("ALTER TABLE decisions ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
            if "request_fingerprint" not in columns:
                conn.execute("ALTER TABLE decisions ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''")
            # Claims live separately from applied decisions.  A claimed row is
            # intentionally never taken over: a crashed writer leaves a
            # durable fence and retries fail closed instead of mutating twice.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS request_ledger(actor TEXT NOT NULL,idempotency_key TEXT NOT NULL,operation TEXT NOT NULL,context_json TEXT NOT NULL,request_fingerprint TEXT NOT NULL,decision_id TEXT, state TEXT NOT NULL,claim_token TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,failure_code TEXT NOT NULL DEFAULT '',PRIMARY KEY(actor,idempotency_key))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS decision_outbox(event_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL UNIQUE,operation TEXT NOT NULL,target_digest TEXT NOT NULL,scope_digest TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','projected','failed')),attempts INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,projected_at TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_governance_decision_outbox_status ON decision_outbox(status,created_at,event_id)"
            )
            # Upgrade an older decisions-only ledger so its receipts remain
            # replayable after enabling the request fence.
            for row in conn.execute("SELECT decision_id,operation,context_json,idempotency_key,request_fingerprint,status,created_at FROM decisions WHERE idempotency_key <> ''"):
                try:
                    context = json.loads(str(row[2]))
                    actor = str(context.get("actor") or "")
                except Exception:
                    actor = ""
                if not actor:
                    continue
                created = str(row[6] or _now())
                conn.execute(
                    "INSERT OR IGNORE INTO request_ledger(actor,idempotency_key,operation,context_json,request_fingerprint,decision_id,state,claim_token,created_at,updated_at,failure_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (actor, str(row[3]), str(row[1]), str(row[2]), str(row[4] or ""), str(row[0]), "applied", "", created, created, ""),
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _context(value: V2MutationContext | Mapping[str, Any]) -> V2MutationContext:
        return V2MutationContext.from_value(value)

    @staticmethod
    def _scope(context: V2MutationContext) -> dict[str, Any]:
        return context.to_dict()

    @staticmethod
    def _atom_snapshot(atom: MemoryAtom | None) -> dict[str, Any]:
        if atom is None:
            return {}
        # Never persist source body in the governance ledger.
        return {
            "atom_id": atom.atom_id,
            "memory_id": atom.memory_id,
            "status": atom.status,
            "kind": atom.kind,
            "confidence": atom.confidence,
            "locked": bool(atom.locked),
            "injection_policy": atom.injection_policy,
            "priority": atom.priority,
            "canonical_hash": atom.canonical_hash,
            "dedup_domain": atom.dedup_domain,
            "supersedes": list(atom.supersedes),
            "provenance": list(atom.provenance),
            "metadata": dict(atom.metadata),
            "revision": atom.revision,
            "visibility": atom.visibility,
            "workspace_id": atom.workspace_id,
            "agent_instance_id": atom.agent_instance_id,
            "share_group_id": atom.share_group_id,
            "project_ref": atom.project_ref,
            "provider": atom.provider,
            "runtime_role": atom.runtime_role,
        }

    def _find_atom(self, atom_id: str, context: V2MutationContext) -> MemoryAtom | None:
        return self.memory._get_atom_scoped(atom_id, MemoryAtomStoreScope(context), include_building=True, atom_id=atom_id if len(atom_id) > 30 else "")

    @staticmethod
    def _request_identity(
        operation: str,
        payload: Mapping[str, Any],
        context: V2MutationContext,
        idempotency_key: str | None,
    ) -> tuple[str, str]:
        """Return a durable request key/fingerprint independent of post-state.

        ``put`` callers can take different internal branches for the same
        user request (for example, create versus explicit-memory-id replay).
        Their governance ``reason`` is a decision annotation selected by that
        branch, not request intent, so it must not change the idempotency
        fingerprint.  The remaining payload plus the trusted mutation scope
        is stable request identity; changed body/evidence/atom fields still
        produce a conflict for the same key.
        """

        stable_payload = dict(payload)
        if operation == "put":
            stable_payload.pop("reason", None)
        trusted_scope = {
            "workspace_id": context.workspace_id,
            "share_group_id": context.share_group_id,
            "agent_instance_id": context.agent_instance_id,
            "project_ref": context.project_ref,
            "provider": context.provider,
            "runtime_role": context.runtime_role,
        }
        fingerprint = _digest({
            "operation": operation,
            "payload": stable_payload,
            "trusted_scope": trusted_scope,
        })
        supplied = str(idempotency_key or "").strip()
        key = supplied or _digest({"operation": operation, "caller": context.actor, "request_fingerprint": fingerprint})
        return key, fingerprint

    def _find_request(self, context: V2MutationContext, operation: str, key: str, fingerprint: str) -> V2Decision | None:
        conn = self._connect_ledger()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE idempotency_key=? AND json_extract(context_json,'$.actor')=? ORDER BY created_at, decision_id",
                (str(key), context.actor),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        row = rows[0]
        decision = self._decision_from_row(row)
        if decision.operation != operation or decision.context != context.to_dict():
            raise V2GovernanceError("idempotency request conflict")
        if decision.request_fingerprint and decision.request_fingerprint != fingerprint:
            raise V2GovernanceError("idempotency request conflict")
        return decision

    def _claim_request(self, context: V2MutationContext, operation: str, key: str, fingerprint: str) -> tuple[V2Decision | None, _RequestClaim | None]:
        """Claim an idempotent request before touching a domain store.

        SQLite's durable primary key is the cross-process fence.  Active
        callers briefly wait for the owner to publish its receipt, while a
        stale/crashed claim remains fail-closed after the bounded wait.
        """

        normalized_key = str(key)
        context_json = _json(context.to_dict())
        deadline = time.monotonic() + 2.0
        while True:
            token = secrets.token_hex(16)
            conn = self._connect_ledger()
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM request_ledger WHERE actor=? AND idempotency_key=?",
                    (context.actor, normalized_key),
                ).fetchone()
                if row is None:
                    now = _now()
                    conn.execute(
                        "INSERT INTO request_ledger(actor,idempotency_key,operation,context_json,request_fingerprint,decision_id,state,claim_token,created_at,updated_at,failure_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (context.actor, normalized_key, operation, context_json, str(fingerprint), None, "claimed", token, now, now, ""),
                    )
                    conn.commit()
                    return None, _RequestClaim(context.actor, normalized_key, token)

                existing_context = str(row["context_json"] or "")
                if str(row["operation"]) != operation or existing_context != context_json or str(row["request_fingerprint"] or "") != str(fingerprint):
                    conn.rollback()
                    raise V2GovernanceError("idempotency request conflict")
                state = str(row["state"] or "")
                if state == "applied":
                    decision_id = str(row["decision_id"] or "")
                    existing = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone() if decision_id else None
                    if existing is None:
                        conn.rollback()
                        raise V2GovernanceError("idempotency receipt unavailable")
                    decision = self._decision_from_row(existing)
                    conn.commit()
                    return decision, None
                # Failed and claimed requests are never replayed by applying
                # the domain mutation again.  Claimed requests get a short
                # grace period so simultaneous callers receive the receipt.
                if state != "claimed":
                    conn.rollback()
                    raise V2GovernanceError("idempotency request rejected")
                conn.rollback()
            finally:
                conn.close()
            if time.monotonic() >= deadline:
                raise V2GovernanceError("idempotency request in flight")
            time.sleep(0.01)

    def _fail_request(self, context: V2MutationContext, claim: _RequestClaim, code: str = "request_failed") -> None:
        """Seal a handled failure; unhandled process crashes stay claimed."""

        conn = self._connect_ledger()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE request_ledger SET state='failed',failure_code=?,updated_at=? WHERE actor=? AND idempotency_key=? AND state='claimed' AND claim_token=?",
                (str(code), _now(), claim.actor, claim.key, claim.token),
            ).rowcount
            if changed != 1:
                raise V2GovernanceError("idempotency request fence rejected")
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _decision_from_row(row: sqlite3.Row | Sequence[Any]) -> V2Decision:
        def value(name: str, index: int, default: Any = "") -> Any:
            try:
                return row[name]  # type: ignore[index]
            except (IndexError, KeyError, TypeError):
                return row[index]  # type: ignore[index]

        return V2Decision(
            str(value("decision_id", 0)),
            str(value("operation", 1)),
            json.loads(value("target_json", 2)),
            str(value("reason", 3)),
            float(value("confidence", 4)),
            str(value("undo_hash", 5)),
            json.loads(value("context_json", 6)),
            str(value("status", 9)),
            str(value("created_at", 10)),
            json.loads(value("before_json", 7)),
            json.loads(value("after_json", 8)),
            str(value("idempotency_key", 11, "") or ""),
            str(value("request_fingerprint", 12, "") or ""),
        )

    def _record(
        self,
        operation: str,
        target: Mapping[str, Any],
        context: V2MutationContext,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        reason: str,
        confidence: float,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        claim: _RequestClaim | None = None,
    ) -> V2Decision:
        reason, confidence = self._decision_args(reason, confidence)
        if not idempotency_key:
            idempotency_key = _digest({"operation": operation, "actor": context.actor, "target": dict(target), "before": dict(before), "after": dict(after)})
        if not request_fingerprint:
            request_fingerprint = _digest({"operation": operation, "target": dict(target), "before": dict(before), "after": dict(after)})
        undo_hash = _digest({"operation": operation, "target": dict(target), "before": dict(before), "after": dict(after)})
        decision_id = _digest({"operation": operation, "actor": context.actor, "workspace_id": context.workspace_id, "share_group_id": context.share_group_id, "idempotency_key": str(idempotency_key)})
        now = _now()
        conn = self._connect_ledger()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO decisions(decision_id,operation,target_json,reason,confidence,undo_hash,context_json,before_json,after_json,status,created_at,idempotency_key,request_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id) DO NOTHING",
                (decision_id, operation, _json(target), reason, confidence, undo_hash, _json(context.to_dict()), _json(before), _json(after), "applied", now, str(idempotency_key), str(request_fingerprint)),
            )
            existing = conn.execute("SELECT * FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if existing is None:
                raise RuntimeError("decision insert did not produce a row")
            existing_decision = self._decision_from_row(existing)
            if existing_decision.operation != operation or existing_decision.target != dict(target) or existing_decision.context != context.to_dict() or existing_decision.request_fingerprint != str(request_fingerprint):
                raise V2GovernanceError("idempotency request conflict")
            event_id = _digest({"event": "governance.decision", "decision_id": decision_id})
            conn.execute(
                "INSERT INTO decision_outbox(event_id,decision_id,operation,target_digest,scope_digest,status,attempts,created_at) VALUES(?,?,?,?,?,'pending',0,?) ON CONFLICT(decision_id) DO NOTHING",
                (
                    event_id,
                    decision_id,
                    operation,
                    _digest(dict(target)),
                    _digest({
                        "workspace_id": context.workspace_id,
                        "share_group_id": context.share_group_id,
                        "agent_instance_id": context.agent_instance_id,
                        "project_ref": context.project_ref,
                        "provider": context.provider,
                        "runtime_role": context.runtime_role,
                    }),
                    now,
                ),
            )
            if claim is not None:
                request = conn.execute(
                    "SELECT state,claim_token,decision_id FROM request_ledger WHERE actor=? AND idempotency_key=?",
                    (claim.actor, claim.key),
                ).fetchone()
                if request is None or str(request[0]) != "claimed" or str(request[1]) != claim.token:
                    raise V2GovernanceError("idempotency request fence rejected")
                changed = conn.execute(
                    "UPDATE request_ledger SET state='applied',decision_id=?,updated_at=?,failure_code='' WHERE actor=? AND idempotency_key=? AND state='claimed' AND claim_token=?",
                    (decision_id, now, claim.actor, claim.key, claim.token),
                ).rowcount
                if changed != 1:
                    raise V2GovernanceError("idempotency request fence rejected")
            conn.commit()
            return existing_decision
        finally:
            conn.close()

    @staticmethod
    def _decision_args(reason: str, confidence: float) -> tuple[str, float]:
        normalized = str(reason or "").strip()
        if not normalized:
            raise V2ContextError("decision reason is required")
        value = float(confidence)
        if not 0.0 <= value <= 1.0:
            raise V2ContextError("decision confidence must be between 0 and 1")
        return normalized, value

    @staticmethod
    def _check_evidence_subject(memory: MemoryAtomStore, subject_type: str, subject_id: str, context: V2MutationContext) -> None:
        if str(subject_type).casefold() in {"atom", "memory"}:
            atom = memory._get_atom_scoped(str(subject_id), MemoryAtomStoreScope(context), include_building=True, atom_id=subject_id if len(subject_id) > 30 else "")
            if atom is None:
                raise V2ScopeError("evidence subject is outside context")

    def _compensate_atom_failure(self, persisted: MemoryAtom, before: MemoryAtom | None, context: V2MutationContext) -> None:
        """Undo a fact mutation when the independent decision ledger fails."""

        if before is None:
            self.memory.delete(persisted.memory_id, scope=self._scope(context), reason="governance ledger failure")
        else:
            self.memory.put_atom(before, context=context)

    @staticmethod
    def _normalize_atom(atom: MemoryAtom | Mapping[str, Any], context: V2MutationContext) -> MemoryAtom:
        item = MemoryAtom.from_value(atom)
        for field, value in (
            ("workspace_id", context.workspace_id),
            ("share_group_id", context.share_group_id),
            ("agent_instance_id", context.agent_instance_id),
            ("project_ref", context.project_ref),
            ("provider", context.provider),
            ("runtime_role", context.runtime_role),
        ):
            if not getattr(item, field):
                if context.is_automatic and field == "agent_instance_id":
                    raise V2ScopeError("automatic mutation requires an explicit agent_instance_id")
                if context.is_automatic and field == "project_ref" and context.project_ref:
                    raise V2ScopeError("automatic mutation requires an explicit project_ref")
                setattr(item, field, value)
        context.check_scope(
            workspace_id=item.workspace_id,
            share_group_id=item.share_group_id,
            agent_instance_id=item.agent_instance_id,
            project_ref=item.project_ref,
            provider=item.provider,
            runtime_role=item.runtime_role,
        )
        return item

    def _resolve_logical_atom(
        self,
        item: MemoryAtom,
        context: V2MutationContext,
    ) -> MemoryAtom | None:
        """Resolve the logical V2 record before claiming a mutation.

        The store repeats this lookup inside its write transaction.  This
        boundary-side preflight makes the governance decision and undo
        snapshot refer to the canonical atom when a caller supplies a fresh
        atom ID for an existing ``(share_group_id, memory_id)`` record.
        """

        existing = self.memory._get_atom_unscoped(
            item.memory_id,
            share_group_id=item.share_group_id,
            include_building=True,
        )
        if existing is None:
            return None
        if not context.admin:
            try:
                context.check_scope(
                    workspace_id=existing.workspace_id,
                    share_group_id=existing.share_group_id,
                    agent_instance_id=existing.agent_instance_id,
                    project_ref=existing.project_ref,
                    provider=existing.provider,
                    runtime_role=existing.runtime_role,
                )
            except V2ScopeError:
                # Do not reveal an existing owner or a storage constraint.
                raise V2ScopeError("mutation logical record is outside context") from None
        item.atom_id = existing.atom_id
        return existing

    @staticmethod
    def _atom_request_payload(item: MemoryAtom) -> dict[str, Any]:
        value = item.to_dict()
        # Exclude storage-assigned state so a network retry made with the
        # returned atom still fingerprints the original put request.
        for key in ("atom_id", "revision", "visibility", "created_at", "updated_at"):
            value.pop(key, None)
        if not value.get("canonical_hash"):
            value["canonical_hash"] = _digest(value.get("body", ""))
        return value

    def put_atom(
        self,
        atom: MemoryAtom | Mapping[str, Any],
        *,
        context: V2MutationContext | Mapping[str, Any],
        evidence: Sequence[str | Mapping[str, Any]] | None = None,
        evidence_ids: Sequence[str] | None = None,
        source_mappings: Sequence[Mapping[str, Any]] | None = None,
        reason: str = "atom mutation",
        confidence: float = 1.0,
        idempotency_key: str | None = None,
        request_payload: Mapping[str, Any] | None = None,
    ) -> tuple[MemoryAtom, V2Decision]:
        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        item = self._normalize_atom(atom, ctx)
        before_atom = self._resolve_logical_atom(item, ctx)
        identity_payload = (
            {"request": dict(request_payload)}
            if request_payload is not None
            else {
                "atom": self._atom_request_payload(item),
                "evidence": list(evidence or ()),
                "evidence_ids": list(evidence_ids or ()),
                "source_mappings": [dict(value) for value in (source_mappings or ())],
                "reason": reason,
                "confidence": confidence,
            }
        )
        request_key, request_fingerprint = self._request_identity("put", identity_payload, ctx, idempotency_key)
        replay, claim = self._claim_request(ctx, "put", request_key, request_fingerprint)
        if replay is not None:
            current = self.memory._get_atom_unscoped(item.memory_id, share_group_id=item.share_group_id, include_building=True, atom_id=item.atom_id if item.atom_id else "")
            if current is None:
                raise V2GovernanceError("idempotency receipt has no corresponding atom")
            return current, replay
        try:
            if before_atom is None and not (evidence or evidence_ids):
                raise V2GovernanceError("new atom requires at least one evidence reference")
            persisted = self.memory.put_atom(item, evidence=evidence, evidence_ids=evidence_ids, source_mappings=source_mappings, context=ctx)
            decision = self._record("put", {"atom_id": persisted.atom_id, "memory_id": persisted.memory_id}, ctx, self._atom_snapshot(before_atom), self._atom_snapshot(persisted), reason=reason, confidence=confidence, idempotency_key=request_key, request_fingerprint=request_fingerprint, claim=claim)
        except Exception:
            if "persisted" in locals():
                try:
                    self._compensate_atom_failure(persisted, before_atom, ctx)
                except Exception:
                    pass
            if claim is not None:
                try:
                    self._fail_request(ctx, claim)
                except Exception:
                    pass
            raise
        return persisted, decision

    def record_deduplication(
        self,
        atom: MemoryAtom,
        *,
        context: V2MutationContext | Mapping[str, Any],
        request_payload: Mapping[str, Any] | None = None,
        reason: str = "native V2 automatic deduplication",
        confidence: float = 1.0,
        idempotency_key: str | None = None,
    ) -> V2Decision:
        """Record an idempotent no-op when a scoped atom is deduplicated.

        Deduplication is still a governed mutation event even though it does
        not rewrite the winning atom.  Keeping the request fence in the same
        V2 decision ledger makes retries and changed-payload reuse fail or
        replay deterministically instead of silently creating a duplicate.
        """

        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        visible = self.memory._get_atom_scoped(
            atom.memory_id,
            MemoryAtomStoreScope(ctx),
            include_building=True,
            atom_id=atom.atom_id if atom.atom_id else "",
        )
        if visible is None or visible.atom_id != atom.atom_id:
            raise V2ScopeError("deduplication candidate is outside context")
        target = {"atom_id": atom.atom_id, "memory_id": atom.memory_id}
        request = {"candidate": target, "request": dict(request_payload or {})}
        request_key, request_fingerprint = self._request_identity("deduplicate", request, ctx, idempotency_key)
        replay, claim = self._claim_request(ctx, "deduplicate", request_key, request_fingerprint)
        if replay is not None:
            return replay
        try:
            decision = self._record(
                "deduplicate",
                target,
                ctx,
                self._atom_snapshot(visible),
                self._atom_snapshot(visible),
                reason=reason,
                confidence=confidence,
                idempotency_key=request_key,
                request_fingerprint=request_fingerprint,
                claim=claim,
            )
        except Exception:
            if claim is not None:
                try:
                    self._fail_request(ctx, claim)
                except Exception:
                    pass
            raise
        return decision

    update_atom = put_atom

    def tombstone(self, memory_id: str, *, context: V2MutationContext | Mapping[str, Any], reason: str, confidence: float = 1.0, idempotency_key: str | None = None) -> tuple[MemoryAtom, V2Decision]:
        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        request_key, request_fingerprint = self._request_identity("tombstone", {"memory_id": memory_id, "reason": reason, "confidence": confidence}, ctx, idempotency_key)
        replay, claim = self._claim_request(ctx, "tombstone", request_key, request_fingerprint)
        if replay is not None:
            current = self.memory._get_atom_scoped(memory_id, MemoryAtomStoreScope(ctx), include_building=True)
            if current is None:
                raise V2GovernanceError("idempotency receipt has no corresponding atom")
            return current, replay
        try:
            before = self.memory._get_atom_scoped(memory_id, MemoryAtomStoreScope(ctx), include_building=True)
            if before is None:
                raise KeyError(memory_id)
            persisted = self.memory.delete(memory_id, scope=self._scope(ctx), reason=reason)
            decision = self._record("tombstone", {"atom_id": persisted.atom_id, "memory_id": persisted.memory_id}, ctx, self._atom_snapshot(before), self._atom_snapshot(persisted), reason=reason, confidence=confidence, idempotency_key=request_key, request_fingerprint=request_fingerprint, claim=claim)
        except Exception:
            if "persisted" in locals():
                try:
                    self.memory.put_atom(before, context=ctx)
                except Exception:
                    pass
            if claim is not None:
                try:
                    self._fail_request(ctx, claim)
                except Exception:
                    pass
            raise
        return persisted, decision

    def restore(
        self,
        memory_id: str,
        *,
        context: V2MutationContext | Mapping[str, Any],
        reason: str,
        confidence: float = 1.0,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryAtom, V2Decision]:
        """Govern a restore of one atom and its supersession descendants."""

        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        request_key, request_fingerprint = self._request_identity(
            "restore",
            {"memory_id": str(memory_id), "reason": reason, "confidence": confidence},
            ctx,
            idempotency_key,
        )
        replay, claim = self._claim_request(ctx, "restore", request_key, request_fingerprint)
        if replay is not None:
            current = self.memory._get_atom_scoped(
                memory_id,
                MemoryAtomStoreScope(ctx),
                include_building=True,
            )
            if current is None:
                raise V2GovernanceError("idempotency receipt has no corresponding atom")
            return current, replay

        before_target = self.memory._get_atom_scoped(
            memory_id,
            MemoryAtomStoreScope(ctx),
            include_building=True,
        )
        if before_target is None:
            if claim is not None:
                self._fail_request(ctx, claim)
            raise KeyError(memory_id)
        before_atoms = {
            atom.atom_id: atom
            for atom in self.memory.list_atoms(
                scope=ctx.to_dict(),
                include_building=True,
            )
        }
        try:
            restored, shadowed = self.memory.restore(
                memory_id,
                context=ctx,
                reason=reason,
            )
            shadowed_ids = [item.atom_id for item in shadowed]
            before = {
                "target": self._atom_snapshot(before_target),
                "descendants": {
                    atom_id: self._atom_snapshot(before_atoms[atom_id])
                    for atom_id in shadowed_ids
                    if atom_id in before_atoms
                },
            }
            after = {
                "target": self._atom_snapshot(restored),
                "descendants": {
                    item.atom_id: self._atom_snapshot(item)
                    for item in shadowed
                },
            }
            target = {
                "atom_id": restored.atom_id,
                "memory_id": restored.memory_id,
                "descendants_shadowed": [item.memory_id for item in shadowed],
            }
            decision = self._record(
                "restore",
                target,
                ctx,
                before,
                after,
                reason=reason,
                confidence=confidence,
                idempotency_key=request_key,
                request_fingerprint=request_fingerprint,
                claim=claim,
            )
        except Exception:
            # The memory-domain mutation is atomic.  If receipt publication
            # fails after that commit, compensate only the lifecycle fields
            # touched by this restore; bodies and evidence remain intact.
            try:
                for atom_id, snapshot in {
                    before_target.atom_id: self._atom_snapshot(before_target),
                    **{
                        atom_id: self._atom_snapshot(atom)
                        for atom_id, atom in before_atoms.items()
                        if atom_id != before_target.atom_id and atom.status == "active"
                    },
                }.items():
                    current = self.memory._get_atom_scoped(
                        atom_id,
                        MemoryAtomStoreScope(ctx),
                        include_building=True,
                        atom_id=atom_id,
                    )
                    if current is None:
                        continue
                    current.status = str(snapshot.get("status") or current.status)
                    current.locked = bool(snapshot.get("locked", current.locked))
                    current.supersedes = list(snapshot.get("supersedes") or current.supersedes)
                    current.provenance = list(snapshot.get("provenance") or current.provenance)
                    current.metadata = dict(snapshot.get("metadata") or current.metadata)
                    current.visibility = str(snapshot.get("visibility") or current.visibility)
                    self.memory.put_atom(current, context=ctx)
            except Exception:
                pass
            if claim is not None:
                try:
                    self._fail_request(ctx, claim)
                except Exception:
                    pass
            raise
        return restored, decision

    def supersede(self, old: str, new: str, *, context: V2MutationContext | Mapping[str, Any], reason: str, confidence: float = 1.0, source_ref: str = "", idempotency_key: str | None = None) -> V2Decision:
        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        request_key, request_fingerprint = self._request_identity("supersede", {"old": old, "new": new, "reason": reason, "confidence": confidence, "source_ref": source_ref}, ctx, idempotency_key)
        replay, claim = self._claim_request(ctx, "supersede", request_key, request_fingerprint)
        if replay is not None:
            return replay
        try:
            old_before = self.memory._get_atom_scoped(old, MemoryAtomStoreScope(ctx), include_building=True, atom_id=old if len(old) > 30 else "")
            new_before = self.memory._get_atom_scoped(new, MemoryAtomStoreScope(ctx), include_building=True, atom_id=new if len(new) > 30 else "")
            if old_before is None or new_before is None:
                raise KeyError("supersession atom not found")
            self.memory.supersede(old, new, scope=self._scope(ctx), reason=reason, source_ref=source_ref)
            old_after = self.memory._get_atom_scoped(old, MemoryAtomStoreScope(ctx), include_building=True, atom_id=old if len(old) > 30 else "")
            new_after = self.memory._get_atom_scoped(new, MemoryAtomStoreScope(ctx), include_building=True, atom_id=new if len(new) > 30 else "")
            decision = self._record("supersede", {"old": old, "new": new}, ctx, {"old": self._atom_snapshot(old_before), "new": self._atom_snapshot(new_before)}, {"old": self._atom_snapshot(old_after), "new": self._atom_snapshot(new_after)}, reason=reason, confidence=confidence, idempotency_key=request_key, request_fingerprint=request_fingerprint, claim=claim)
        except Exception:
            try:
                self.memory.put_atom(old_before, context=ctx)
                self.memory.put_atom(new_before, context=ctx)
            except Exception:
                pass
            if claim is not None:
                try:
                    self._fail_request(ctx, claim)
                except Exception:
                    pass
            raise
        return decision

    def put_evidence(self, *, context: V2MutationContext | Mapping[str, Any], reason: str, confidence: float = 1.0, **kwargs: Any) -> tuple[Evidence, V2Decision]:
        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        evidence = self.evidence.put_evidence(context=ctx, **kwargs)
        decision = self._record("evidence.put", {"evidence_id": evidence.evidence_id}, ctx, {}, {"evidence_id": evidence.evidence_id, "digest": evidence.digest, "authority": evidence.authority, "status": evidence.status}, reason=reason, confidence=confidence)
        return evidence, decision

    def link(self, evidence: str | Evidence | Mapping[str, Any], subject_type: str, subject_id: str, *, context: V2MutationContext | Mapping[str, Any], relation: str = "supports", metadata: Mapping[str, Any] | None = None, reason: str, confidence: float = 1.0) -> tuple[EvidenceLink, V2Decision]:
        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        if ctx.is_automatic and str(subject_type).casefold() not in {"atom", "memory"}:
            raise V2ScopeError("automatic evidence writes may only target scoped atoms")
        self._check_evidence_subject(self.memory, subject_type, subject_id, ctx)
        link = self.evidence.link(evidence, subject_type, subject_id, relation, metadata=metadata, context=ctx)
        decision = self._record("evidence.link", {"evidence_id": link.evidence_id, "subject_type": subject_type, "subject_id": subject_id, "relation": relation}, ctx, {}, {"link_id": link.link_id}, reason=reason, confidence=confidence)
        return link, decision

    def unlink(self, evidence_id: str, subject_type: str, subject_id: str, *, context: V2MutationContext | Mapping[str, Any], relation: str = "supports", reason: str, confidence: float = 1.0, idempotency_key: str | None = None) -> tuple[int, V2Decision]:
        ctx = self._context(context)
        reason, confidence = self._decision_args(reason, confidence)
        request_key, request_fingerprint = self._request_identity("evidence.unlink", {"evidence_id": evidence_id, "subject_type": subject_type, "subject_id": subject_id, "relation": relation, "reason": reason, "confidence": confidence}, ctx, idempotency_key)
        replay, claim = self._claim_request(ctx, "evidence.unlink", request_key, request_fingerprint)
        if replay is not None:
            return int((replay.before or {}).get("removed", 0)), replay
        removed = 0
        try:
            if ctx.is_automatic and str(subject_type).casefold() not in {"atom", "memory"}:
                raise V2ScopeError("automatic evidence writes may only target scoped atoms")
            self._check_evidence_subject(self.memory, subject_type, subject_id, ctx)
            removed = self.evidence.unlink(evidence_id, subject_type, subject_id, relation, context=ctx)
            decision = self._record("evidence.unlink", {"evidence_id": evidence_id, "subject_type": subject_type, "subject_id": subject_id, "relation": relation}, ctx, {"removed": removed}, {"removed": 0}, reason=reason, confidence=confidence, idempotency_key=request_key, request_fingerprint=request_fingerprint, claim=claim)
        except Exception:
            if removed:
                try:
                    self.evidence.link(evidence_id, subject_type, subject_id, relation, context=ctx)
                except Exception:
                    pass
            if claim is not None:
                try:
                    self._fail_request(ctx, claim)
                except Exception:
                    pass
            raise
        return removed, decision

    def list_decisions(self) -> list[V2Decision]:
        conn = self._connect_ledger()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM decisions ORDER BY created_at, decision_id").fetchall()
        finally:
            conn.close()
        return [self._decision_from_row(row) for row in rows]

    def undo(self, decision_id: str, *, context: V2MutationContext | Mapping[str, Any], reason: str, confidence: float = 1.0) -> V2Decision:
        ctx = self._context(context)
        decisions = {item.decision_id: item for item in self.list_decisions()}
        decision = decisions.get(str(decision_id))
        if decision is None:
            raise KeyError(decision_id)
        if decision.status != "applied":
            raise V2GovernanceError("decision is already compensated")
        ctx.check_workspace(self.workspace)
        target = dict(decision.target)
        if decision.operation == "tombstone":
            atom_id = str(target["atom_id"])
            current = self.memory._get_atom_scoped(atom_id, MemoryAtomStoreScope(ctx), include_building=True, atom_id=atom_id if len(atom_id) > 30 else "")
            if current is None or _digest(self._atom_snapshot(current)) != _digest(decision.after):
                raise V2GovernanceError("undo hash/state guard rejected")
            current.status = str(decision.before.get("status", current.status))
            current.metadata = dict(decision.before.get("metadata") or current.metadata)
            restored = self.memory.put_atom(current, context=ctx)
            result = self._record("undo", target, ctx, self._atom_snapshot(current), self._atom_snapshot(restored), reason=reason, confidence=confidence)
        elif decision.operation == "supersede":
            old_id, new_id = str(target["old"]), str(target["new"])
            old = self.memory._get_atom_scoped(old_id, MemoryAtomStoreScope(ctx), include_building=True, atom_id=old_id if len(old_id) > 30 else "")
            new = self.memory._get_atom_scoped(new_id, MemoryAtomStoreScope(ctx), include_building=True, atom_id=new_id if len(new_id) > 30 else "")
            if old is None or new is None:
                raise V2GovernanceError("undo state guard rejected")
            expected = decision.after or {}
            if _digest(self._atom_snapshot(old)) != _digest(expected.get("old") or {}) or _digest(self._atom_snapshot(new)) != _digest(expected.get("new") or {}):
                raise V2GovernanceError("undo hash/state guard rejected")
            old_before = self._atom_snapshot(old)
            new_before = self._atom_snapshot(new)
            old.status = str((decision.before.get("old") or {}).get("status", old.status))
            new.supersedes = list((decision.before.get("new") or {}).get("supersedes", new.supersedes))
            self.memory.put_atom(old, context=ctx)
            self.memory.put_atom(new, context=ctx)
            result = self._record("undo", target, ctx, {"old": old_before, "new": new_before}, {"old": self._atom_snapshot(old), "new": self._atom_snapshot(new)}, reason=reason, confidence=confidence)
        else:
            raise V2GovernanceError(f"undo is not supported for {decision.operation}")
        conn = self._connect_ledger()
        try:
            conn.execute("UPDATE decisions SET status=? WHERE decision_id=? AND status='applied'", ("compensated", decision.decision_id))
            conn.commit()
        finally:
            conn.close()
        return result


class _MemoryScopeAdapter:
    def __init__(self, context: V2MutationContext) -> None:
        self.share_group_id = context.share_group_id
        self.workspace_id = context.workspace_id
        self.agent_instance_id = context.agent_instance_id
        self.project_ref = context.project_ref
        self.provider = context.provider
        self.runtime_role = context.runtime_role
        self.admin = bool(context.admin)


MemoryAtomStoreScope = _MemoryScopeAdapter
GovernanceV2 = V2GovernanceBoundary
MutationBoundary = V2GovernanceBoundary


__all__ = ["GovernanceV2", "MutationBoundary", "V2Decision", "V2GovernanceBoundary"]
