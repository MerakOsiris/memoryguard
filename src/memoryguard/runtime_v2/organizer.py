"""V2-native automatic memory organization.

The service is deliberately group-bound.  It reads candidates through an
explicit V2 admin read scope and mutates only through ``GovernanceV2``.  The
admin context is an internal capability of this already-bound service; the
caller identity is retained in provenance, never inferred from another
group.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import re
from threading import Lock, RLock
from typing import Any, Iterable, Mapping, Sequence

from ..governance_v2 import GovernanceV2, V2MutationContext
from ..memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from ..memory.store import stable_digest
from ..sensitive_content import SENSITIVE_PATTERNS
from .dedup import V2DedupMatch, V2SemanticDeduplicator, canonical_hash, canonical_text
from .canonical_claims import _heading, claims_related, compose_canonical_bodies, topic_affinity
from .governance_semantics import (
    GovernanceRelation,
    classify_governance_relation,
    governance_scope_key,
)
from .text_native import VALID_KINDS, classify_kind


_LOCK_GUARD = Lock()
_BODY_LOCKS: dict[str, RLock] = {}
_COMPOSITION_RELATIONS = frozenset({"exact", "equivalent", "update", "additive"})
_FORBIDDEN_METADATA_KEYS = frozenset({
    "body", "raw", "raw_content", "content", "text", "full_text",
    "document", "document_body", "document_text", "conversation",
    "conversation_body", "full_transcript", "raw_transcript", "transcript",
})


class OrganizationError(RuntimeError):
    """Stable failure code for the native organizer boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "organization_failed")
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for(workspace: Path, group: str, body: str) -> RLock:
    key = stable_digest({
        "workspace": str(workspace),
        "share_group_id": group,
        "body": canonical_text(body),
    })
    with _LOCK_GUARD:
        lock = _BODY_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _BODY_LOCKS[key] = lock
        return lock


def _clean_tree(value: Any, *, key: str = "") -> Any:
    """Keep metadata JSON-safe and prevent a second body storage channel."""

    if key.casefold() in _FORBIDDEN_METADATA_KEYS:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if name.casefold() in _FORBIDDEN_METADATA_KEYS:
                continue
            cleaned = _clean_tree(raw_value, key=name)
            if cleaned is not None:
                result[name] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [_clean_tree(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _provenance_key(value: Mapping[str, Any]) -> str:
    return stable_digest({
        "source_ref": value.get("source_ref", ""),
        "source_event_id": value.get("source_event_id", ""),
        "agent_instance_id": value.get("agent_instance_id", ""),
        "digest": value.get("digest", ""),
    })


def _canonical_injection_policy(left: Any, right: Any) -> str:
    """Choose the strongest valid policy when folding duplicate evidence."""

    policies = {str(left or "").casefold(), str(right or "").casefold()}
    # Ordinary deduplication must never weaken an explicit always policy.
    return "always" if "always" in policies else "relevant"


# A canonical atom may receive the same durable fact through several native
# categories.  Classification is provenance, but the visible head still needs
# one deterministic category.  Correction is an explicit user override;
# procedure/preference/project are progressively more specific durable kinds
# than an unqualified fact or an episode.
_KIND_SPECIFICITY = {
    "episode": 0,
    "fact": 1,
    "project": 2,
    "preference": 3,
    "procedure": 4,
    "correction": 5,
}


def _kind_rank(kind: Any) -> int:
    return int(_KIND_SPECIFICITY.get(str(kind or "").casefold(), 1))


class V2MemoryOrganizer:
    """Group-scoped automatic write service for V2 memory atoms."""

    def __init__(
        self,
        workspace: str | Path,
        share_group_id: str,
        *,
        memory_store: MemoryAtomStore | None = None,
        governance: Any | None = None,
        rule_store: Any | None = None,
        rule_reconciliation: Any | None = None,
        deduplicator: Any | None = None,
        threshold: float = 0.85,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.share_group_id = str(share_group_id or "").strip()
        if not self.share_group_id:
            raise OrganizationError("share_group_id_required")
        self.store = memory_store or MemoryAtomStore(self.workspace)
        if not isinstance(self.store, MemoryAtomStore):
            raise TypeError("v2 organizer requires MemoryAtomStore")
        self.governance = governance or GovernanceV2(self.workspace, memory_store=self.store)
        if not callable(getattr(self.governance, "put_atom", None)):
            raise TypeError("v2 organizer requires GovernanceV2.put_atom")
        if not callable(getattr(self.governance, "supersede", None)):
            raise TypeError("v2 organizer requires GovernanceV2.supersede")
        self.rule_store = rule_store
        self.rule_reconciliation = rule_reconciliation
        self.scope = MemoryReadScope(
            workspace_id=str(self.workspace),
            share_group_id=self.share_group_id,
            admin=True,
        )
        self.deduplicator = deduplicator or V2SemanticDeduplicator(
            self.store, self.scope, threshold=threshold,
        )
        self.threshold = float(threshold)
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("semantic dedup threshold must be between 0 and 1")
        self._canonical_context = V2MutationContext(
            workspace_id=str(self.workspace),
            share_group_id=self.share_group_id,
            actor="organizer:" + stable_digest(self.share_group_id)[:24],
            admin=True,
            authority="system",
        )

    @staticmethod
    def service_contract() -> dict[str, Any]:
        """Minimal contract for a public ``memory_write`` adapter."""

        return {
            "input": {
                "body": "required string",
                "event_id": "stable source event id; optional when idempotency_key is supplied",
                "agent_instance_id": "caller identity",
                "share_group_id": "must equal the bound service group",
                "metadata": "JSON object without raw body fields",
                "kind": "optional V2 kind",
                "idempotency_key": "optional durable retry key",
            },
            "context": "V2MutationContext or equivalent trusted bound context",
            "output": {
                "atom": "persisted MemoryAtom",
                "actions": "body-free action list",
                "mutation_kind": "created|deduplicated|superseded|conflicted|quarantined",
                "receipt": "V2Decision.to_dict()",
                "governance_receipt": "unchanged|merged|updated|superseded|conflicted audit envelope",
            },
        }

    def _context(
        self,
        value: Any | None,
        *,
        agent_instance_id: str,
        project_ref: str,
        provider: str,
        runtime_role: str,
    ) -> V2MutationContext:
        if value is not None:
            context = V2MutationContext.from_value(value)
            if context.workspace_id != str(self.workspace):
                raise OrganizationError("organization_workspace_forbidden")
            if context.share_group_id != self.share_group_id:
                raise OrganizationError("organization_group_forbidden")
            if agent_instance_id and not context.admin and agent_instance_id != context.agent_instance_id:
                raise OrganizationError("organization_agent_forbidden")
            return context
        agent = str(agent_instance_id or "").strip()
        return V2MutationContext(
            workspace_id=str(self.workspace),
            share_group_id=self.share_group_id,
            agent_instance_id=agent,
            project_ref=str(project_ref or ""),
            provider=str(provider or ""),
            runtime_role=str(runtime_role or ""),
            actor="organizer-caller:" + (agent or "anonymous"),
            admin=True,
            authority="system",
        )

    @staticmethod
    def _secret_match(body: str, metadata: Mapping[str, Any]) -> str:
        for pattern in SENSITIVE_PATTERNS:
            if pattern.search(body):
                return pattern.pattern[:80]
        marker = metadata.get("_secret_detected")
        return str(marker or "")[:80]

    @staticmethod
    def _confidence(body: str, kind: str) -> float:
        value = 0.50
        if len(body.strip()) >= 12:
            value += 0.10
        if len(body.strip()) >= 40:
            value += 0.10
        if kind in {"procedure", "project", "correction"}:
            value += 0.05
        if len(body.strip()) < 4:
            value -= 0.20
        return max(0.05, min(0.95, value))

    @staticmethod
    def _is_correction(body: str, metadata: Mapping[str, Any], kind: str) -> bool:
        text = body.casefold()
        markers = ("correction", "actually", "update", "wrong", "instead", "更正", "纠正", "不对", "应该")
        return kind == "correction" or metadata.get("type") == "correction" or any(item in text for item in markers)

    @staticmethod
    def _is_conflict(body: str, candidate: MemoryAtom, kind: str, metadata: Mapping[str, Any]) -> bool:
        if metadata.get("conflict") is True or metadata.get("type") == "conflict":
            return True
        # Kind is provenance/classification, not semantic identity.  Cross-kind
        # observations may share one canonical node; polarity, parameters,
        # scope, and explicit conflict metadata remain the safety gates.
        text = canonical_text(body)
        old = canonical_text(candidate.body)
        preference_markers = ("prefer", "like", "preference", "偏好", "喜欢")
        return (
            any(item in text for item in preference_markers)
            and any(item in old for item in preference_markers)
            and text != old
        )

    @staticmethod
    def _classification_override(value: Any) -> bool:
        """Return whether a source explicitly overrides its classification."""

        if isinstance(value, Mapping):
            return bool(
                value.get("classification_override")
                or value.get("kind_override")
                or value.get("type") == "classification_override"
            )
        return False

    @classmethod
    def _select_canonical_kind(
        cls, candidate: MemoryAtom, prepared: Mapping[str, Any],
    ) -> str:
        """Choose one category without making the result arrival-order based."""

        candidate_kind = str(candidate.kind or "fact").casefold()
        incoming_kind = str(prepared.get("kind") or "fact").casefold()
        candidate_override = cls._classification_override(candidate.metadata)
        if not candidate_override:
            candidate_override = any(
                cls._classification_override(item)
                for item in candidate.provenance
            )
        incoming_override = bool(prepared.get("classification_override"))
        if incoming_override != candidate_override:
            return incoming_kind if incoming_override else candidate_kind
        candidate_rank = _kind_rank(candidate_kind)
        incoming_rank = _kind_rank(incoming_kind)
        if incoming_rank > candidate_rank:
            return incoming_kind
        return candidate_kind

    @staticmethod
    def _source_ref(data: Mapping[str, Any], metadata: Mapping[str, Any], event_id: str) -> str:
        return str(
            data.get("source_ref")
            or metadata.get("source_ref")
            or metadata.get("source_id")
            or "event:" + event_id
        )[:512]

    def _prepare(self, data: Mapping[str, Any], *, kind_override: str = "") -> dict[str, Any]:
        body = str(data.get("body") or data.get("raw_content") or "").strip()
        if not body:
            raise OrganizationError("memory_body_required")
        group = str(data.get("share_group_id") or data.get("group_id") or "").strip()
        if group and group != self.share_group_id:
            raise OrganizationError("organization_group_forbidden")
        metadata = _clean_tree(data.get("metadata") or {})
        if not isinstance(metadata, Mapping):
            raise OrganizationError("memory_metadata_invalid")
        metadata = dict(metadata)
        agent = str(data.get("agent_instance_id") or data.get("agent") or "").strip()
        source_ref = self._source_ref(data, metadata, str(data.get("event_id") or ""))
        event_id = str(data.get("event_id") or "").strip()
        if not event_id:
            event_id = "event-" + stable_digest({"body": canonical_text(body), "agent": agent, "source_ref": source_ref})[:32]
        requested_kind = str(kind_override or data.get("kind") or "").strip().casefold()
        kind = requested_kind or classify_kind(body, metadata)
        if kind not in VALID_KINDS:
            # An untrusted/invalid hint must not erase the native classifier's
            # semantic result.  MemoryAtom persists the prepared kind as-is,
            # so the fallback belongs here at the V2 organizer boundary.
            kind = classify_kind(body, metadata)
            if kind not in VALID_KINDS:
                kind = "fact"
        classification_override = bool(
            kind_override
            or data.get("kind_override")
            or self._classification_override(data)
            or self._classification_override(metadata)
        )
        policy = str(data.get("injection_policy") or metadata.get("injection_policy") or "relevant")
        priority = int(data.get("priority", metadata.get("priority", 0)) or 0)
        confidence = float(data.get("confidence") if data.get("confidence") is not None else self._confidence(body, kind))
        confidence = max(0.0, min(1.0, confidence))
        visibility = str(data.get("visibility") or "building").strip().casefold()
        if visibility not in {"building", "ready", "active", "hidden"}:
            raise OrganizationError("memory_visibility_invalid")
        digest = canonical_hash(body)
        provenance = {
            "source_ref": source_ref,
            "source_event_id": event_id,
            "agent_instance_id": agent,
            "share_group_id": self.share_group_id,
            "digest": digest,
            # Preserve each source classification as evidence; it is not part
            # of canonical identity and must not block cross-kind coalescing.
            "kind": kind,
            "classification_override": classification_override,
            "injection_policy": policy,
            "locator": str(metadata.get("locator") or "event")[:256],
            "source_revision": str(metadata.get("source_revision") or ""),
        }
        evidence = list(data.get("evidence") or ())
        # Native GUI lifecycle updates carry the complete existing atom and
        # deliberately preserve its provenance.  They must not synthesize a
        # fresh evidence record from the changed policy/lock metadata: the
        # evidence ID is content/source-stable and the evidence store treats
        # metadata changes under that ID as a conflict.  Body edits and public
        # writes still receive their normal automatic evidence below.
        if not evidence and not data.get("evidence_ids") and not data.get("_preserve_provenance"):
            evidence = [{
                "source_ref": source_ref,
                # Auto events without an explicit source all use "event:".
                # Their actor-bound event ID must participate in evidence
                # identity, or two private rules with equal text collide.
                "revision": event_id,
                "digest": digest,
                "authority": "observed",
                "metadata": {
                    "source_event_id": event_id,
                    "agent_instance_id": agent,
                    "share_group_id": self.share_group_id,
                    "kind": kind,
                    "classification_override": classification_override,
                },
            }]
        mappings = list(data.get("source_mappings") or ())
        if not mappings:
            mappings = [{
                "source_domain": "auto_organizer",
                "source_ref": source_ref,
                "source_record_id": event_id,
                "source_revision": str(metadata.get("source_revision") or ""),
                "digest": digest,
                "metadata": {
                    "agent_instance_id": agent,
                    "share_group_id": self.share_group_id,
                    "kind": kind,
                    "classification_override": classification_override,
                    "injection_policy": policy,
                },
            }]
        return {
            "body": body,
            "metadata": metadata,
            "agent_instance_id": agent,
            "project_ref": str(data.get("project_ref") or ""),
            "provider": str(data.get("provider") or ""),
            "runtime_role": str(data.get("runtime_role") or ""),
            "event_id": event_id,
            "source_ref": source_ref,
            "kind": kind,
            "classification_override": classification_override,
            "injection_policy": policy,
            "priority": priority,
            "confidence": confidence,
            # Explicit GUI updates carry the complete atom through the native
            # boundary.  Keep lifecycle state distinct from the organizer's
            # create-time status so a partial update cannot silently reset it.
            "status": str(data.get("status") or ""),
            "locked": data.get("locked") if "locked" in data else None,
            "preserve_provenance": bool(data.get("_preserve_provenance")),
            "visibility": visibility,
            "digest": digest,
            "provenance": provenance,
            "evidence": evidence,
            "evidence_ids": list(data.get("evidence_ids") or ()),
            "source_mappings": mappings,
            "memory_id": str(data.get("memory_id") or "").strip(),
            "atom_id": str(data.get("atom_id") or "").strip(),
            "idempotency_key": str(data.get("idempotency_key") or "").strip(),
            "write_policy": str(data.get("write_policy") or "auto_accept"),
            "secret_match": self._secret_match(body, metadata),
        }

    def _new_atom(self, prepared: Mapping[str, Any], *, status: str, supersedes: Sequence[str] = (), metadata: Mapping[str, Any] | None = None) -> MemoryAtom:
        body = str(prepared["body"])
        memory_id = str(prepared.get("memory_id") or "")
        if not memory_id:
            memory_id = "memory-" + stable_digest({
                "group": self.share_group_id,
                "body": canonical_text(body),
                "kind": prepared["kind"],
                # Logical identity is scoped identity.  The physical atom ID
                # already carries owner fields, but public receipts expose
                # memory_id; omitting audience here falsely presents two
                # private rules as one canonical record.
                "scope": self._scope_key_for_prepared(prepared),
            })[:40]
        atom_metadata = dict(prepared.get("metadata") or {})
        atom_metadata.update({
            "organizer_version": "v2",
            "source_event_id": prepared["event_id"],
            "source_ref": prepared["source_ref"],
        })
        if prepared.get("secret_match"):
            atom_metadata.update({
                "quarantine_id": "quarantine-" + stable_digest(prepared["event_id"])[:32],
                "quarantine_reason": "sensitive content detected",
                "detected_pattern": prepared["secret_match"],
                "quarantined_at": _now(),
            })
        if metadata:
            atom_metadata.update(dict(metadata))
        return MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind=str(prepared["kind"]),
            status=status,
            confidence=float(prepared["confidence"]),
            locked=bool(prepared.get("locked", False)),
            injection_policy=str(prepared["injection_policy"]),
            priority=int(prepared["priority"]),
            canonical_hash=str(prepared["digest"]),
            dedup_domain=str(prepared["injection_policy"]),
            supersedes=list(supersedes),
            provenance=[dict(prepared["provenance"])],
            # Keep the trusted writer scope on the atom for compatibility and
            # ownership checks.  Native audience metadata is the explicit
            # visibility expansion; provenance remains the complete list of
            # contributing agents after a cross-agent merge.
            agent_instance_id=str(prepared.get("agent_instance_id") or ""),
            share_group_id=self.share_group_id,
            project_ref=str(prepared.get("project_ref") or ""),
            provider=str(prepared.get("provider") or ""),
            runtime_role=str(prepared.get("runtime_role") or ""),
            workspace_id=str(self.workspace),
            visibility=str(prepared.get("visibility") or "building"),
            metadata=atom_metadata,
        )

    @staticmethod
    def _receipt(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if hasattr(value, "to_dict"):
            return dict(value.to_dict())
        if isinstance(value, Mapping):
            return dict(value)
        return {"value": str(value)}

    def _scope_key_for_prepared(self, prepared: Mapping[str, Any]) -> tuple[str, str, str, str, str, str, str]:
        return governance_scope_key(
            metadata=prepared.get("metadata") if isinstance(prepared.get("metadata"), Mapping) else {},
            agent_instance_id=str(prepared.get("agent_instance_id") or ""),
            share_group_id=self.share_group_id,
            project_ref=str(prepared.get("project_ref") or ""),
            provider=str(prepared.get("provider") or ""),
            runtime_role=str(prepared.get("runtime_role") or ""),
        )

    @staticmethod
    def _scope_key_for_atom(atom: MemoryAtom) -> tuple[str, str, str, str, str, str, str]:
        return governance_scope_key(
            metadata=atom.metadata,
            agent_instance_id=atom.agent_instance_id,
            share_group_id=atom.share_group_id,
            project_ref=atom.project_ref,
            provider=atom.provider,
            runtime_role=atom.runtime_role,
        )

    def _scope_memory_id(self, memory_id: str, prepared: Mapping[str, Any]) -> str:
        """Namespace caller IDs when same logical ID exists in another scope."""

        return str(memory_id) + "@scope-" + stable_digest({
            "scope": self._scope_key_for_prepared(prepared),
        })[:20]

    def _atoms_for_memory_id(self, memory_id: str) -> list[MemoryAtom]:
        return [
            atom for atom in self.store.list_atoms(
                scope=self.scope,
                include_building=True,
            )
            if atom.memory_id == str(memory_id)
        ]

    @staticmethod
    def _governance_receipt(
        action: str,
        *,
        relation: GovernanceRelation | None = None,
        target_id: str = "",
        old_id: str = "",
        native_receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        receipt = {
            "action": str(action),
            "target_id": str(target_id or ""),
            "old_id": str(old_id or ""),
        }
        if relation is not None:
            receipt["relation"] = relation.to_dict()
        if isinstance(native_receipt, Mapping):
            receipt["decision_id"] = str(native_receipt.get("decision_id") or "")
            receipt["operation"] = str(native_receipt.get("operation") or "")
            receipt["status"] = str(native_receipt.get("status") or "")
        return receipt

    @staticmethod
    def _idempotency_request(prepared: Mapping[str, Any]) -> dict[str, Any]:
        """Return request intent, excluding organizer-owned post-state."""

        metadata = dict(prepared.get("metadata") or {})
        # These fields are added while materializing or merging an atom.  They
        # must not make a retry look like a new request.
        for key in (
            "organizer_version",
            "source_event_id",
            "last_source_event_id",
            "quarantine_id",
            "quarantine_reason",
            "detected_pattern",
            "quarantined_at",
            "conflict_group_id",
            "conflict_peer_ids",
            "conflict_reason",
            "proposal_target_id",
            "manual_override_target_id",
            "idempotency_key",
        ):
            metadata.pop(key, None)
        return {
            "memory_id": str(prepared.get("memory_id") or ""),
            "body": str(prepared.get("body") or ""),
            "kind": str(prepared.get("kind") or ""),
            "injection_policy": str(prepared.get("injection_policy") or ""),
            "priority": int(prepared.get("priority") or 0),
            "confidence": float(prepared.get("confidence") or 0.0),
            "visibility": str(prepared.get("visibility") or ""),
            "event_id": str(prepared.get("event_id") or ""),
            "source_ref": str(prepared.get("source_ref") or ""),
            "metadata": metadata,
            "evidence": deepcopy(prepared.get("evidence") or []),
            "evidence_ids": list(prepared.get("evidence_ids") or []),
            "source_mappings": deepcopy(prepared.get("source_mappings") or []),
            "write_policy": str(prepared.get("write_policy") or ""),
        }

    def _scoped_key(self, key: str, prepared: Mapping[str, Any]) -> str:
        """Prevent retry-key reuse across audience partitions."""

        return str(key) + ":scope-" + stable_digest({
            "scope": self._scope_key_for_prepared(prepared),
        })[:20]

    @staticmethod
    def _composition_value(value: Any, name: str, default: Any = None) -> Any:
        """Read composer result fields without coupling to its concrete type."""

        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def _composition_claims(value: Any) -> tuple[str, ...]:
        """Parse claim boundaries without retaining claim text in metadata."""

        pieces = re.split(
            r"[\r\n]+|(?<=[。！？!?；;])\s*|(?<=[.!?])(?=\s|$)",
            str(value or ""),
        )
        claims: list[str] = []
        for piece in pieces:
            claim = re.sub(
                r"^\s*(?:(?:[-*+•◦▪▸‣])\s+|\[\d+\]\s+|\d+[.)]\s+)+",
                "",
                piece,
            ).strip()
            if claim and canonical_text(claim) not in {canonical_text(item) for item in claims}:
                claims.append(claim)
        return tuple(claims)

    @staticmethod
    def _composition_claim_maps(source: str, final: str) -> bool:
        """Return whether source claim can account for final claim surface."""

        if canonical_text(source) == canonical_text(final):
            return True
        relation = classify_governance_relation(source, final)
        return relation.kind in _COMPOSITION_RELATIONS

    @staticmethod
    def _composition_metadata(
        candidate: MemoryAtom,
        prepared: Mapping[str, Any],
        composition: Any,
        relation: GovernanceRelation,
    ) -> dict[str, Any]:
        """Record claim/source digests, never another copy of claim text."""

        existing = candidate.metadata.get("composition")
        previous = existing if isinstance(existing, Mapping) else {}
        previous_records = [
            dict(item)
            for item in (previous.get("claims", []) if isinstance(previous, Mapping) else [])
            if isinstance(item, Mapping)
        ]
        old_claims = V2MemoryOrganizer._composition_claims(candidate.body)
        incoming_claims = V2MemoryOrganizer._composition_claims(prepared.get("body"))
        claims = tuple(
            str(item).strip()
            for item in (V2MemoryOrganizer._composition_value(composition, "claims", ()) or ())
            if str(item).strip()
        )
        incoming_source = {
            "source_event_id": str(prepared.get("event_id") or ""),
            "source_ref": str(prepared.get("source_ref") or ""),
            "body_digest": str(prepared.get("digest") or canonical_hash(prepared.get("body", ""))),
            "source_role": "incoming_body",
        }
        records: list[dict[str, Any]] = []

        def add_record(value: Mapping[str, Any], final_digest: str, *, relation_name: str) -> None:
            record = {
                "claim_digest": final_digest,
                "relation": str(value.get("relation") or relation_name),
            }
            for key in ("source_event_id", "source_ref", "body_digest", "source_role"):
                if value.get(key) not in (None, ""):
                    record[key] = str(value[key])
            if not any(stable_digest(item) == stable_digest(record) for item in records):
                records.append(record)

        for final_claim in claims:
            final_digest = canonical_hash(final_claim)
            matching_old = [
                old_claim
                for old_claim in old_claims
                if V2MemoryOrganizer._composition_claim_maps(old_claim, final_claim)
            ]
            matching_incoming = [
                incoming_claim
                for incoming_claim in incoming_claims
                if V2MemoryOrganizer._composition_claim_maps(incoming_claim, final_claim)
            ]
            old_digests = {canonical_hash(old_claim) for old_claim in matching_old}
            old_records = [
                item
                for item in previous_records
                if str(item.get("claim_digest") or "") in old_digests
            ]
            for item in old_records:
                add_record(item, final_digest, relation_name="candidate_composition")
            if matching_old and not old_records:
                # Metadata may predate composition.  Provenance is fallback
                # evidence for candidate body only, never incoming evidence.
                for provenance in candidate.provenance:
                    if not isinstance(provenance, Mapping):
                        continue
                    fallback = {
                        "source_event_id": str(provenance.get("source_event_id") or ""),
                        "source_ref": str(provenance.get("source_ref") or ""),
                        "body_digest": str(provenance.get("digest") or ""),
                        "source_role": "candidate_body_provenance",
                    }
                    if any(
                        fallback[key]
                        for key in ("source_event_id", "source_ref", "body_digest")
                    ):
                        add_record(fallback, final_digest, relation_name="candidate_body_provenance")
            if matching_incoming:
                add_record(incoming_source, final_digest, relation_name=relation.kind)
        records.sort(key=lambda item: stable_digest(item))
        sources = []
        for item in records:
            source_item = {
                key: item[key]
                for key in ("source_event_id", "source_ref", "body_digest", "source_role")
                if key in item
            }
            if source_item and source_item not in sources:
                sources.append(source_item)
        composition_changed = bool(
            V2MemoryOrganizer._composition_value(composition, "changed", False)
        )
        # The shared composer renders every multi-input component as bullets.
        # An exact duplicate is still one canonical claim, so the organizer
        # preserves the already-persisted body surface instead of turning a
        # stable write into a formatting-only revision.
        if (
            relation.kind == "exact"
            and canonical_text(candidate.body) == canonical_text(prepared.get("body", ""))
        ):
            composition_changed = False
        return {
            "changed": composition_changed,
            "relation": relation.to_dict(),
            "claims": records,
            "sources": sources,
        }

    def _compose_candidate(
        self,
        candidate: MemoryAtom,
        prepared: Mapping[str, Any],
        relation: GovernanceRelation | None,
    ) -> Any | None:
        """Ask the shared claim composer whether two bodies share a topic."""

        if relation is None:
            return None
        result = compose_canonical_bodies([str(candidate.body or ""), str(prepared["body"])])
        return result

    @classmethod
    def _relation_from_composition(
        cls,
        candidate: MemoryAtom,
        prepared: Mapping[str, Any],
        relation: GovernanceRelation,
        composition: Any | None,
    ) -> GovernanceRelation:
        """Use canonical claim affinity to widen recall without weakening gates.

        ``classify_governance_relation`` compares complete bodies.  A body may
        contain several claims, so that result can be ``distinct`` (or a
        polarity conflict) even though the canonical claim composer proves the
        two bodies form one safe topic component.  The composer is the sole
        source of that wider relation: rejected conflict edges remain a hard
        conflict, and rejected unrelated claims remain separate records.
        """

        if composition is None:
            return relation
        metadata = prepared.get("metadata")
        explicit_conflict = isinstance(metadata, Mapping) and (
            metadata.get("conflict") is True or metadata.get("type") == "conflict"
        )
        rejected_conflicts = tuple(
            str(item).strip()
            for item in (cls._composition_value(composition, "rejected_conflicts", ()) or ())
            if str(item).strip()
        )
        rejected_unrelated = tuple(
            str(item).strip()
            for item in (cls._composition_value(composition, "rejected_unrelated", ()) or ())
            if str(item).strip()
        )
        claims = tuple(
            str(item).strip()
            for item in (cls._composition_value(composition, "claims", ()) or ())
            if str(item).strip()
        )
        candidate_claims = cls._composition_claims(candidate.body)
        incoming_claims = cls._composition_claims(prepared.get("body"))
        related_pairs = [
            (left, right)
            for left in candidate_claims
            for right in incoming_claims
            if claims_related(left, right)
        ]
        if explicit_conflict and relation.kind == "distinct":
            return GovernanceRelation("conflict", 1.0, "explicit_composition_conflict", "")
        if rejected_conflicts:
            # A broad polarity classifier can mark unrelated bodies as a
            # conflict.  Keep conflict only when the shared helper also finds
            # a strong topic edge; explicit contradiction edges remain hard
            # conflicts through the composer.
            affinity = max(
                (topic_affinity(left, right) for left in candidate_claims for right in incoming_claims),
                default=0.0,
            )
            if not related_pairs and affinity < 0.5:
                return GovernanceRelation(
                    "distinct",
                    float(relation.score),
                    "canonical_composition_unrelated",
                    "",
                )
            return GovernanceRelation(
                "conflict",
                max(float(relation.score), 0.8),
                "canonical_composition_conflict",
                "",
            )
        if rejected_unrelated:
            return GovernanceRelation(
                "distinct",
                float(relation.score),
                "canonical_composition_unrelated",
                "",
            )
        if claims and related_pairs and relation.kind in {"distinct", "conflict"}:
            affinity = max(
                (topic_affinity(left, right) for left, right in related_pairs),
                default=float(relation.score),
            )
            return GovernanceRelation(
                "additive",
                float(affinity),
                "canonical_topic_affinity",
                "",
            )
        return relation

    def _put(self, atom: MemoryAtom, prepared: Mapping[str, Any], *, reason: str, key: str) -> tuple[MemoryAtom, dict[str, Any] | None]:
        result = self.governance.put_atom(
            atom,
            context=self._canonical_context,
            evidence=prepared.get("evidence"),
            evidence_ids=prepared.get("evidence_ids"),
            source_mappings=prepared.get("source_mappings"),
            reason=reason,
            confidence=float(prepared["confidence"]),
            idempotency_key=self._scoped_key(key, prepared),
            request_payload=self._idempotency_request(prepared),
        )
        if isinstance(result, tuple):
            persisted = result[0]
            receipt = self._receipt(result[1] if len(result) > 1 else None)
        elif isinstance(result, Mapping):
            persisted = result.get("atom") or result.get("memory") or result
            receipt = self._receipt(result.get("receipt"))
        else:
            persisted, receipt = result, None
        if not isinstance(persisted, MemoryAtom):
            persisted = MemoryAtom.from_value(persisted)
        return persisted, receipt

    def _merge(
        self,
        candidate: MemoryAtom,
        prepared: Mapping[str, Any],
        *,
        composition: Any | None = None,
        relation: GovernanceRelation | None = None,
    ) -> tuple[MemoryAtom, list[dict[str, Any]], dict[str, Any] | None, bool]:
        incoming = dict(prepared["provenance"])
        incoming_key = _provenance_key(incoming)
        existing = {_provenance_key(item) for item in candidate.provenance}
        # A native update carries the existing logical memory_id.  It must be
        # an explicit governed revision even when the body is unchanged: the
        # injection policy and priority are mutable V2 fields, not dedup
        # metadata that may be silently retained from the prior revision.
        explicit_update = str(prepared.get("memory_id") or "") == candidate.memory_id
        if explicit_update:
            provenance = list(candidate.provenance)
            if not prepared.get("preserve_provenance") and incoming_key not in existing:
                provenance.append(incoming)
            canonical_kind = self._select_canonical_kind(candidate, prepared)
            status = str(prepared.get("status") or candidate.status)
            locked = candidate.locked if prepared.get("locked") is None else bool(prepared.get("locked"))
            metadata = (
                dict(candidate.metadata)
                if prepared.get("preserve_provenance")
                else {**dict(candidate.metadata), **dict(prepared.get("metadata") or {})}
            )
            updated = replace(
                candidate,
                body=str(prepared["body"]),
                kind=canonical_kind,
                provenance=provenance,
                status=status,
                locked=locked,
                injection_policy=str(prepared["injection_policy"]),
                dedup_domain=str(
                    prepared.get("dedup_domain") or prepared["injection_policy"]
                ),
                priority=int(prepared["priority"]),
                confidence=max(float(candidate.confidence), float(prepared["confidence"])),
                canonical_hash=str(prepared["digest"]),
                metadata=metadata,
            )
            key = str(prepared.get("idempotency_key") or "") or (
                "update:" + stable_digest({"atom": candidate.atom_id, "event": prepared["event_id"]})
            )
            persisted, receipt = self._put(
                updated,
                prepared,
                reason="native memory update",
                key=key,
            )
            return persisted, [{"action": "update", "target_id": candidate.memory_id}], receipt, False
        if incoming_key in existing:
            # Replay the original governed put request so callers receive
            # the durable receipt generated for the same idempotency key.
            # This remains a GovernanceV2 mutation boundary; it is not a
            # direct store shortcut or an unrecorded deduplication.
            replay_key = str(prepared.get("idempotency_key") or "")
            if not replay_key:
                replay_key = "create:" + stable_digest({
                    "group": self.share_group_id,
                    "event": prepared["event_id"],
                    "digest": prepared["digest"],
                })
            persisted, receipt = self._put(
                candidate,
                prepared,
                reason="automatic memory organization",
                key=replay_key,
            )
            return candidate if persisted is None else persisted, [{"action": "idempotent_replay", "target_id": candidate.memory_id}], receipt, True
        provenance = list(candidate.provenance) + [incoming]
        effective_policy = _canonical_injection_policy(
            candidate.injection_policy, prepared.get("injection_policy"),
        )
        canonical_kind = self._select_canonical_kind(candidate, prepared)
        merge_metadata = {
            **dict(candidate.metadata),
            "last_source_event_id": prepared["event_id"],
        }
        if canonical_kind != str(candidate.kind or "").casefold():
            merge_metadata["canonical_kind_reconciled"] = {
                "canonical": canonical_kind,
                "incoming": str(prepared.get("kind") or "fact").casefold(),
                "source_event_id": str(prepared.get("event_id") or ""),
            }
        incoming_policy = str(prepared.get("injection_policy") or "").casefold()
        if incoming_policy and incoming_policy != str(candidate.injection_policy or "").casefold():
            # Keep the canonical node singular, but leave a body-free audit
            # marker whenever duplicate evidence carried a different policy.
            merge_metadata["canonical_policy_reconciled"] = {
                "canonical": effective_policy,
                "incoming": incoming_policy,
                "source_event_id": str(prepared.get("event_id") or ""),
            }
        composed_body = str(candidate.body or "")
        composition_metadata: dict[str, Any] | None = None
        if composition is not None:
            candidate_body = self._composition_value(composition, "body", None)
            rejected_conflicts = tuple(
                str(item).strip()
                for item in (self._composition_value(composition, "rejected_conflicts", ()) or ())
                if str(item).strip()
            )
            if candidate_body and not rejected_conflicts:
                composed_body = str(candidate_body)
                # Exact duplicate writes must retain the canonical body's
                # original formatting.  Otherwise the claim composer changes
                # a single record into a bullet merely because a second
                # provenance source arrived, making direct body reads miss
                # the canonical record and needlessly rewriting its hash.
                if (
                    relation is not None
                    and relation.kind == "exact"
                    and canonical_text(candidate.body)
                    == canonical_text(prepared.get("body", ""))
                ):
                    composed_body = str(candidate.body or "")
            if relation is not None:
                composition_metadata = self._composition_metadata(
                    candidate, prepared, composition, relation,
                )
                merge_metadata["composition"] = composition_metadata
        updated = replace(
            candidate,
            body=composed_body,
            kind=canonical_kind,
            provenance=provenance,
            confidence=max(float(candidate.confidence), float(prepared["confidence"])),
            priority=max(int(candidate.priority), int(prepared["priority"])),
            injection_policy=effective_policy,
            # ``dedup_domain`` may carry a canonical/scope namespace on
            # migrated atoms; never replace it with the incoming policy.
            dedup_domain=candidate.dedup_domain,
            canonical_hash=canonical_hash(composed_body),
            metadata=merge_metadata,
        )
        key = "merge:" + stable_digest({"atom": candidate.atom_id, "provenance": incoming_key})
        persisted, receipt = self._put(updated, prepared, reason="automatic canonical provenance merge", key=key)
        return persisted, [{"action": "merge_provenance", "target_id": candidate.memory_id}], receipt, False

    @staticmethod
    def _native_shared_plane(metadata: Any) -> bool:
        audience = metadata.get("audience") if isinstance(metadata, Mapping) else None
        return (
            isinstance(audience, Mapping)
            and str(audience.get("source") or "").casefold() == "native_v2"
        )

    def _correction_relation(
        self,
        candidate: MemoryAtom,
        prepared: Mapping[str, Any],
    ) -> GovernanceRelation | None:
        """Match an explicit version/value correction to its old subject."""

        metadata = prepared.get("metadata")
        if not self._is_correction(
            str(prepared.get("body") or ""),
            metadata if isinstance(metadata, Mapping) else {},
            str(prepared.get("kind") or ""),
        ):
            return None
        if not self._native_shared_plane(candidate.metadata) or not self._native_shared_plane(metadata):
            return None
        marker = re.compile(
            r"^\s*(?:correction|actually|update|wrong|instead)\s*[:,-]?\s*",
            re.IGNORECASE,
        )
        version = re.compile(r"\b\d+(?:\.\d+)+\b")
        incoming_subject = version.sub("", marker.sub("", str(prepared.get("body") or "")))
        candidate_subject = version.sub("", str(candidate.body or ""))
        if canonical_text(incoming_subject) != canonical_text(candidate_subject):
            return None
        return GovernanceRelation(
            "update", 0.95, "explicit_correction_same_subject", "right",
        )

    def _find(self, prepared: Mapping[str, Any]) -> list[V2DedupMatch]:
        """Find merge/conflict candidates inside the exact governed audience.

        The embedding backend is intentionally not the authority here.  It is
        useful for recall, but governance needs deterministic scope + semantic
        classification so two agents in the same group cannot accidentally
        collapse their private rules into one record.
        """

        statuses = {
            "active", "low_confidence", "conflicted", "deleted",
            "quarantined", "shadowed",
        }
        incoming_scope = self._scope_key_for_prepared(prepared)
        body = str(prepared["body"])
        ranked: list[tuple[int, float, str, str, V2DedupMatch]] = []
        relation_order = {"exact": 0, "update": 1, "additive": 2, "equivalent": 3, "conflict": 4}
        candidate_reader = getattr(self.deduplicator, "candidates", None)
        atoms = (
            candidate_reader(statuses=statuses)
            if callable(candidate_reader)
            else [item.atom for item in self.deduplicator.find(body, threshold=0.0, statuses=statuses)]
        )

        def native_shared_plane(metadata: Any) -> bool:
            audience = metadata.get("audience") if isinstance(metadata, Mapping) else None
            return (
                isinstance(audience, Mapping)
                and str(audience.get("source") or "").casefold() == "native_v2"
            )

        def exact_cross_agent_scope(left: MemoryAtom) -> bool:
            """Allow exact provenance coalescing across same project audience.

            A shared group can receive the same rule from multiple agents.  The
            atom remains owned by its first writer, so broadening this check to
            semantic relations would turn private rules into one audience.  A
            canonical exact body is safe to merge only when all non-agent
            audience dimensions still match.
            """

            candidate_scope = self._scope_key_for_atom(left)
            if candidate_scope[0] != "agent" or incoming_scope[0] != "agent":
                return False
            if not native_shared_plane(left.metadata) or not native_shared_plane(
                prepared.get("metadata")
            ):
                return False
            # An unqualified native agent audience is private to that agent.
            # A non-empty runtime role is the explicit shared execution plane
            # used by native fan-in/correction callers; do not widen the
            # default public MCP audience merely because the body is exact.
            if not left.runtime_role or not prepared.get("runtime_role"):
                return False
            return (
                candidate_scope[2:] == incoming_scope[2:]
                and (left.project_ref, left.provider, left.runtime_role)
                == (prepared.get("project_ref"), prepared.get("provider"), prepared.get("runtime_role"))
            )

        def correction_relation(left: MemoryAtom) -> GovernanceRelation | None:
            """Match an explicit version/value correction to its old subject.

            Governance's normal classifier intentionally treats different
            lexical anchors as distinct.  A correction is a separate governed
            operation: after removing its marker and version-like values, the
            remaining subject must be identical before a supersession target is
            considered.
            """

            if not self._is_correction(
                body,
                prepared.get("metadata") if isinstance(prepared.get("metadata"), Mapping) else {},
                str(prepared.get("kind") or ""),
            ):
                return None
            if not native_shared_plane(left.metadata) or not native_shared_plane(
                prepared.get("metadata")
            ):
                return None
            marker = re.compile(
                r"^\s*(?:correction|actually|update|wrong|instead|鏇存|绾犳|涓嶅)"
                r"\s*[:：,，\-]?\s*",
                re.IGNORECASE,
            )
            version = re.compile(r"\b\d+(?:\.\d+)+\b")
            incoming_subject = version.sub("", marker.sub("", body))
            candidate_subject = version.sub("", str(left.body or ""))
            if canonical_text(incoming_subject) != canonical_text(candidate_subject):
                return None
            return GovernanceRelation(
                "update", 0.95, "explicit_correction_same_subject", "right",
            )

        for atom in atoms:
            relation = classify_governance_relation(atom.body, body)
            composition = self._compose_candidate(atom, prepared, relation)
            relation = self._relation_from_composition(
                atom, prepared, relation, composition,
            )
            correction = self._correction_relation(atom, prepared)
            if correction is not None:
                relation = correction
            if relation.kind not in relation_order:
                continue
            exact = relation.kind == "exact"
            same_scope = self._scope_key_for_atom(atom) == incoming_scope
            if not same_scope and not (
                (exact and exact_cross_agent_scope(atom))
                or (correction is not None and exact_cross_agent_scope(atom))
            ):
                continue
            if prepared.get("secret_match") and not exact:
                continue
            match = V2DedupMatch(atom=atom, similarity=float(relation.score), exact=exact)
            ranked.append((
                relation_order[relation.kind],
                -float(relation.score),
                atom.memory_id,
                atom.atom_id,
                match,
            ))
        ranked.sort(key=lambda item: item[:4])
        return [item[4] for item in ranked]

    def _supersede_candidate(
        self,
        candidate: MemoryAtom,
        prepared: Mapping[str, Any],
        *,
        reason: str,
        relation: GovernanceRelation,
        allow_policy_change: bool = False,
        composition: Any | None = None,
    ) -> tuple[MemoryAtom, dict[str, Any] | None]:
        # Semantic update/additive writes are new versions, but they are still
        # ordinary evidence unless explicitly marked as a correction.  Carry
        # forward the strongest policy/priority so a relevant rephrase cannot
        # silently weaken an always canonical node.
        effective_prepared = dict(prepared)
        effective_prepared["dedup_domain"] = candidate.dedup_domain
        if not allow_policy_change:
            policy = _canonical_injection_policy(
                candidate.injection_policy, prepared.get("injection_policy"),
            )
            effective_prepared["injection_policy"] = policy
            effective_prepared["priority"] = max(
                int(candidate.priority), int(prepared.get("priority") or 0),
            )
            if (
                policy != str(prepared.get("injection_policy") or "")
                or effective_prepared["priority"] != int(prepared.get("priority") or 0)
            ):
                effective_prepared["metadata"] = {
                    **dict(prepared.get("metadata") or {}),
                    "canonical_policy_preserved": policy,
                }
        if composition is not None:
            effective_prepared["metadata"] = {
                **dict(effective_prepared.get("metadata") or {}),
                "composition": self._composition_metadata(
                    candidate, prepared, composition, relation,
                ),
            }
        atom = self._new_atom(
            effective_prepared, status="active", supersedes=[candidate.memory_id],
        )
        persisted, receipt = self._put(
            atom,
            effective_prepared,
            reason=reason,
            key="supersede-put:" + stable_digest({
                "group": self.share_group_id,
                "old": candidate.atom_id,
                "event": prepared["event_id"],
                "relation": relation.kind,
            }),
        )
        self.governance.supersede(
            candidate.atom_id,
            persisted.atom_id,
            context=self._canonical_context,
            reason=reason,
            source_ref=str(prepared["source_ref"]),
            idempotency_key="supersede-edge:" + stable_digest({
                "old": candidate.atom_id,
                "new": persisted.atom_id,
            }),
        )
        refreshed = self.store.get_atom(
            persisted.memory_id,
            scope=self.scope,
            include_building=True,
        ) or persisted
        return refreshed, receipt

    def write(self, payload: Mapping[str, Any] | None = None, *, context: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        # Selection, replacement and retiring the predecessor must observe a
        # single serialized snapshot, including across MCP server processes.
        with self.governance.atomic_memory():
            before = self.store.list_atoms(scope=self.scope, include_building=True)
            result = self._write_atomic(payload, context=context, **kwargs)
            atom = result["atom"]
            if atom.status != "active":
                return result
            predecessor_ids = set(atom.supersedes)
            anchors = [old for old in before if old.atom_id == atom.atom_id or old.memory_id in predecessor_ids]
            if not anchors:
                anchors = [atom]
            duplicates = [
                old for old in before
                if old.atom_id != atom.atom_id and old.status == "active" and not old.locked
                and old.memory_id not in predecessor_ids
                and any(
                    self._scope_key_for_atom(old) == self._scope_key_for_atom(anchor)
                    and old.injection_policy == anchor.injection_policy
                    and classify_governance_relation(old.body, anchor.body).kind in {"exact", "equivalent"}
                    for anchor in anchors
                )
            ]
            if duplicates:
                # Publish the staged target *inside* this transaction before
                # adding supersession revisions.  Otherwise a relationship
                # revision could overwrite the staged replacement body.
                projection = self.store.project_evidence(self.governance.evidence)
                if projection.get("failed") or projection.get("pending"):
                    raise OrganizationError("v2_memory_publication_failed")
                for old in duplicates:
                    self.governance.supersede(
                        old.atom_id, atom.atom_id, context=self._canonical_context,
                        reason="consolidate duplicate replacement predecessors",
                        idempotency_key="replacement-duplicate:" + stable_digest({
                            "old": old.atom_id, "new": atom.atom_id, "revision": atom.revision,
                        }),
                    )
                    result["actions"].append({"action": "supersede_duplicate", "old_id": old.memory_id})
                result["atom"] = self.store.get_atom(atom.memory_id, scope=self.scope, include_building=True) or atom
            return result

    def _write_atomic(self, payload: Mapping[str, Any] | None = None, *, context: Any | None = None, **kwargs: Any) -> dict[str, Any]:
        # Organizer normalization is allowed to consume nested metadata,
        # evidence, and source mappings.  Keep caller-owned request data
        # immutable even when this service is called below NativePort.
        data = deepcopy(dict(payload or {}))
        data.update({
            key: deepcopy(value)
            for key, value in kwargs.items()
            if value is not None
        })
        prepared = self._prepare(data, kind_override=str(data.get("kind_override") or ""))
        caller = self._context(
            context,
            agent_instance_id=prepared["agent_instance_id"],
            project_ref=prepared["project_ref"],
            provider=prepared["provider"],
            runtime_role=prepared["runtime_role"],
        )
        # The service has a fixed group; caller context is checked above and
        # only contributes identity to provenance.  Mutations use the fixed
        # internal group capability, never a payload-selected group.
        del caller
        requested_memory_id = str(prepared.get("memory_id") or "").strip()
        explicit_update = False
        lock = _lock_for(self.workspace, self.share_group_id, str(prepared["body"]))
        with lock:
            matches = self._find(prepared)
            selected_match = matches[0] if matches else None
            candidate = selected_match.atom if selected_match else None
            requested_atom_id = str(prepared.get("atom_id") or "").strip()
            if requested_memory_id and requested_atom_id:
                requested = self.store.get_atom(
                    requested_memory_id,
                    scope=self.scope,
                    atom_id=requested_atom_id,
                    include_building=True,
                )
                if requested is not None:
                    # An explicit physical atom selector is authoritative for
                    # lifecycle updates, including migrated rows whose legacy
                    # provider/project fields are intentionally blank.
                    candidate = requested
                    selected_match = None
                    explicit_update = True
            if requested_memory_id and not explicit_update:
                requested = next(
                    (
                        atom for atom in self._atoms_for_memory_id(requested_memory_id)
                        if self._scope_key_for_atom(atom) == self._scope_key_for_prepared(prepared)
                    ),
                    None,
                )
                if requested is not None:
                    # An existing logical id is an explicit governed update.
                    # A new id must not hide an exact/semantic candidate found
                    # above; public writes may carry client ids and still
                    # deduplicate within the trusted group.
                    candidate = requested
                    selected_match = None
                    explicit_update = True
                elif any(
                    self._scope_key_for_atom(atom) != self._scope_key_for_prepared(prepared)
                    for atom in self._atoms_for_memory_id(requested_memory_id)
                ):
                    # A caller-provided ID is an update selector only inside
                    # its original audience.  Reusing it across agents,
                    # projects, providers, or runtime roles would collapse
                    # logical records even when semantic matching correctly
                    # rejected the candidate.
                    prepared = dict(prepared)
                    prepared["memory_id"] = self._scope_memory_id(
                        requested_memory_id,
                        prepared,
                    )
                else:
                    # A caller-provided logical ID is an explicit create when
                    # that ID is absent from the current audience.  An exact
                    # body is still a durable duplicate even when each retry
                    # carries a fresh source ID (for example concurrent host
                    # agents); only a broad semantic candidate must not
                    # consume the requested record.
                    if not (
                        selected_match is not None
                        and (
                            selected_match.exact
                            or self._is_correction(
                                str(prepared["body"]),
                                prepared.get("metadata")
                                if isinstance(prepared.get("metadata"), Mapping)
                                else {},
                                str(prepared.get("kind") or ""),
                            )
                        )
                    ):
                        candidate = None
                        selected_match = None
            elif candidate is None:
                candidate = None
            actions: list[dict[str, Any]] = [{
                "action": "classify",
                "kind": prepared["kind"],
                "confidence": prepared["confidence"],
            }]
            exact_candidate = bool(selected_match and selected_match.exact)
            correction_relation = (
                self._correction_relation(candidate, prepared)
                if candidate is not None else None
            )
            relation = (
                classify_governance_relation(candidate.body, str(prepared["body"]))
                if candidate is not None else None
            )
            composition = (
                self._compose_candidate(candidate, prepared, relation)
                if candidate is not None and relation is not None
                else None
            )
            if candidate is not None and relation is not None:
                relation = self._relation_from_composition(
                    candidate, prepared, relation, composition,
                )
            if correction_relation is not None:
                relation = correction_relation
            if explicit_update and candidate is not None:
                persisted, merge_actions, receipt, replay = self._merge(candidate, prepared)
                actions.extend(merge_actions)
                return {
                    "ok": True,
                    "status": persisted.status,
                    "atom": persisted,
                    "memory_id": persisted.memory_id,
                    "actions": actions,
                    "mutation_kind": "deduplicated",
                    "receipt": receipt,
                    "governance_receipt": self._governance_receipt(
                        "updated",
                        relation=relation,
                        target_id=persisted.memory_id,
                        old_id=candidate.memory_id,
                        native_receipt=receipt,
                    ),
                    "idempotent_replay": replay,
                }
            is_correction = self._is_correction(
                str(prepared["body"]), prepared["metadata"], str(prepared["kind"]),
            )
            if is_correction:
                composition = None
            composition_conflicts = tuple(
                str(item).strip()
                for item in (
                    self._composition_value(composition, "rejected_conflicts", ())
                    or ()
                )
                if str(item).strip()
            )
            composition_unrelated = tuple(
                str(item).strip()
                for item in (
                    self._composition_value(composition, "rejected_unrelated", ())
                    or ()
                )
                if str(item).strip()
            )
            if composition_unrelated and not composition_conflicts:
                # Composer rejected a separate claim.  Do not silently retain
                # only candidate body or merge provenance; persist incoming as
                # its own governed record through normal create path.
                candidate = None
                relation = GovernanceRelation(
                    "distinct",
                    float(relation.score) if relation is not None else 0.0,
                    "composer_rejected_unrelated",
                    "",
                )
                composition = None
            if (
                candidate is not None
                and not explicit_update
                and not is_correction
                and (candidate.locked or candidate.status in {"deleted", "quarantined", "shadowed"})
            ):
                suppression_key = "manual-suppressed:" + stable_digest({
                    "atom_id": candidate.atom_id,
                    "event_id": prepared["event_id"],
                    "body": prepared["body"],
                })
                decision = self.governance.record_deduplication(
                    candidate,
                    context=self._canonical_context,
                    request_payload={"event_id": prepared["event_id"], "body_digest": prepared["digest"]},
                    reason="manual override suppresses automatic duplicate",
                    confidence=float(prepared["confidence"]),
                    idempotency_key=suppression_key,
                )
                native_receipt = self._receipt(decision)
                actions.append({"action": "manual_override_suppressed", "target_id": candidate.memory_id})
                return {
                    "ok": True,
                    "status": candidate.status,
                    "atom": candidate,
                    "memory_id": candidate.memory_id,
                    "actions": actions,
                    "mutation_kind": "deduplicated",
                    "receipt": native_receipt,
                    "governance_receipt": self._governance_receipt(
                        "unchanged",
                        relation=relation,
                        target_id=candidate.memory_id,
                        native_receipt=native_receipt,
                    ),
                    "idempotent_replay": bool(
                        decision.status == "applied"
                        and decision.idempotency_key == suppression_key
                    ),
                }

            if candidate is not None and relation is not None:
                conflict = relation.kind == "conflict" or (
                    not exact_candidate
                    and correction_relation is None
                    and self._is_conflict(
                        str(prepared["body"]), candidate,
                        str(prepared["kind"]), prepared["metadata"],
                    )
                ) or bool(composition_conflicts)
                if conflict:
                    conflict_id = "conflict-" + stable_digest({
                        "group": self.share_group_id,
                        "scope": self._scope_key_for_prepared(prepared),
                        "members": sorted([
                            candidate.memory_id,
                            str(prepared.get("memory_id") or prepared["digest"]),
                        ]),
                    })[:32]
                    conflict_metadata = {
                        "conflict_group_id": conflict_id,
                        "conflict_peer_ids": [candidate.memory_id],
                        "conflict_reason": relation.reason or "automatic semantic conflict",
                    }
                    if composition_conflicts:
                        conflict_metadata["composition_rejected_conflict_digests"] = [
                            canonical_hash(item) for item in composition_conflicts
                        ]
                    if candidate.status != "conflicted" and not candidate.locked:
                        candidate, _peer_receipt = self._put(
                            replace(
                                candidate,
                                status="conflicted",
                                metadata={**dict(candidate.metadata), **conflict_metadata},
                            ),
                            prepared,
                            reason="automatic conflict marking",
                            key="conflict-peer:" + conflict_id,
                        )
                    atom = self._new_atom(
                        prepared,
                        status="conflicted" if not candidate.locked else "low_confidence",
                        metadata=conflict_metadata,
                    )
                    persisted, receipt = self._put(
                        atom,
                        prepared,
                        reason="automatic semantic conflict",
                        key="conflict:" + stable_digest({
                            "group": self.share_group_id,
                            "event": prepared["event_id"],
                        }),
                    )
                    actions.append({
                        "action": "conflict",
                        "conflict_group_id": conflict_id,
                        "old_ids": [candidate.memory_id],
                    })
                    return {
                        "ok": True,
                        "status": persisted.status,
                        "atom": persisted,
                        "memory_id": persisted.memory_id,
                        "actions": actions,
                        "mutation_kind": "conflicted",
                        "receipt": receipt,
                        "governance_receipt": self._governance_receipt(
                            "conflicted",
                            relation=relation,
                            target_id=persisted.memory_id,
                            old_id=candidate.memory_id,
                            native_receipt=receipt,
                        ),
                    }

                composition_claims = tuple(
                    self._composition_value(composition, "claims", ()) or ()
                )
                same_named_claim = bool(
                    len(composition_claims) == 1
                    and _heading(candidate.body)
                    and _heading(candidate.body) == _heading(str(prepared["body"]))
                )
                should_supersede = (
                    not candidate.locked
                    and not (
                        len(composition_claims) > 1
                        or (relation.kind == "update" and same_named_claim)
                    )
                    and (
                        is_correction
                        or (
                            relation.kind in {"update", "additive"}
                            and relation.winner == "right"
                        )
                    )
                )
                if should_supersede:
                    refreshed, receipt = self._supersede_candidate(
                        candidate,
                        prepared,
                        reason=(
                            "automatic correction supersede"
                            if is_correction
                            else "automatic semantic update supersede"
                        ),
                        relation=relation,
                        allow_policy_change=is_correction,
                        composition=composition,
                    )
                    actions.append({"action": "supersede", "old_id": candidate.memory_id})
                    return {
                        "ok": True,
                        "status": refreshed.status,
                        "atom": refreshed,
                        "memory_id": refreshed.memory_id,
                        "actions": actions,
                        "mutation_kind": "superseded",
                        "receipt": receipt,
                        "governance_receipt": self._governance_receipt(
                            "superseded",
                            relation=relation,
                            target_id=refreshed.memory_id,
                            old_id=candidate.memory_id,
                            native_receipt=receipt,
                        ),
                        "idempotent_replay": False,
                    }

                if is_correction and candidate.locked:
                    atom = self._new_atom(
                        prepared,
                        status="low_confidence",
                        metadata={"manual_override_target_id": candidate.memory_id},
                    )
                    persisted, receipt = self._put(
                        atom,
                        prepared,
                        reason="manual override conflict candidate",
                        key="manual-conflict:" + stable_digest({
                            "group": self.share_group_id,
                            "event": prepared["event_id"],
                        }),
                    )
                    actions.append({
                        "action": "manual_override_conflict_candidate",
                        "target_id": candidate.memory_id,
                    })
                    return {
                        "ok": True,
                        "status": persisted.status,
                        "atom": persisted,
                        "memory_id": persisted.memory_id,
                        "actions": actions,
                        "mutation_kind": "conflicted",
                        "receipt": receipt,
                        "governance_receipt": self._governance_receipt(
                            "conflicted",
                            relation=relation,
                            target_id=persisted.memory_id,
                            old_id=candidate.memory_id,
                            native_receipt=receipt,
                        ),
                        "idempotent_replay": False,
                    }

                persisted, merge_actions, receipt, replay = self._merge(
                    candidate,
                    prepared,
                    composition=composition,
                    relation=relation,
                )
                actions.extend(merge_actions)
                governance_action = (
                    "unchanged"
                    if replay
                    else (
                        "updated"
                        if relation.kind in {"update", "additive"}
                        else "merged"
                    )
                )
                return {
                    "ok": True,
                    "status": persisted.status,
                    "atom": persisted,
                    "memory_id": persisted.memory_id,
                    "actions": actions,
                    "mutation_kind": "deduplicated",
                    "receipt": receipt,
                    "governance_receipt": self._governance_receipt(
                        governance_action,
                        relation=relation,
                        target_id=persisted.memory_id,
                        old_id=candidate.memory_id,
                        native_receipt=receipt,
                    ),
                    "idempotent_replay": replay,
                }
            status = "quarantined" if prepared.get("secret_match") else ("low_confidence" if prepared["write_policy"] == "propose_only" or float(prepared["confidence"]) < 0.45 else "active")
            atom = self._new_atom(prepared, status=status)
            persisted, receipt = self._put(atom, prepared, reason="automatic memory organization", key=prepared["idempotency_key"] or "create:" + stable_digest({"group": self.share_group_id, "event": prepared["event_id"], "digest": prepared["digest"]}))
            action = "quarantine" if status == "quarantined" else ("create_low_confidence" if status == "low_confidence" else "create_active")
            actions.append({"action": action, "target_id": persisted.memory_id})
            return {"ok": True, "status": persisted.status, "atom": persisted, "memory_id": persisted.memory_id, "actions": actions, "mutation_kind": "quarantined" if status == "quarantined" else "created", "receipt": receipt, "idempotent_replay": False}

    def organize(self, event: Any, *, kind_override: str = "", write_policy: str = "auto_accept", context: Any | None = None) -> tuple[MemoryAtom, list[dict[str, Any]]]:
        payload = {
            "event_id": getattr(event, "event_id", ""),
            "agent_instance_id": getattr(event, "agent_instance_id", ""),
            "share_group_id": getattr(event, "share_group_id", ""),
            "body": getattr(event, "raw_content", getattr(event, "body", "")),
            "metadata": getattr(event, "metadata", {}) or {},
            "injection_policy": getattr(event, "injection_policy", "relevant"),
            "priority": getattr(event, "priority", 0),
            "write_policy": write_policy,
            "kind_override": kind_override,
        }
        result = self.write(payload, context=context)
        if hasattr(event, "auto_actions"):
            event.auto_actions = list(result["actions"])
        return result["atom"], list(result["actions"])

    def plan(self, event: Any, *, kind_override: str = "", write_policy: str = "auto_accept", context: Any | None = None) -> tuple[MemoryAtom, list[dict[str, Any]], str]:
        payload = {
            "event_id": getattr(event, "event_id", ""),
            "agent_instance_id": getattr(event, "agent_instance_id", ""),
            "share_group_id": getattr(event, "share_group_id", ""),
            "body": getattr(event, "raw_content", getattr(event, "body", "")),
            "metadata": getattr(event, "metadata", {}) or {},
            "write_policy": write_policy,
        }
        prepared = self._prepare({**payload, "kind": kind_override})
        self._context(context, agent_instance_id=prepared["agent_instance_id"], project_ref="", provider="", runtime_role="")
        matches = self._find(prepared)
        if matches:
            target = matches[0].atom
            if self._is_correction(str(prepared["body"]), prepared["metadata"], str(prepared["kind"])):
                return self._new_atom(prepared, status="active", supersedes=[target.memory_id]), [{"action": "supersede", "old_id": target.memory_id}], "superseded"
            return self._new_atom(prepared, status="low_confidence", metadata={"proposal_target_id": target.memory_id}), [{"action": "duplicate_proposal", "target_id": target.memory_id}], "deduplicated"
        status = "quarantined" if prepared.get("secret_match") else ("low_confidence" if write_policy == "propose_only" or prepared["confidence"] < 0.45 else "active")
        action = "quarantine" if status == "quarantined" else ("create_low_confidence" if status == "low_confidence" else "create_active")
        return self._new_atom(prepared, status=status), [{"action": action}], "quarantined" if status == "quarantined" else "created"

    def reconcile_rules(self, *args: Any, **kwargs: Any) -> Any:
        if self.rule_reconciliation is None:
            return {"ok": True, "status": "not_configured", "share_group_id": self.share_group_id}
        method = getattr(self.rule_reconciliation, "reconcile", None) or getattr(self.rule_reconciliation, "run", None)
        if not callable(method):
            raise OrganizationError("rule_reconciliation_invalid")
        kwargs.setdefault("share_group_id", self.share_group_id)
        return method(*args, **kwargs)


V2Organizer = V2MemoryOrganizer

__all__ = ["OrganizationError", "V2MemoryOrganizer", "V2Organizer"]
