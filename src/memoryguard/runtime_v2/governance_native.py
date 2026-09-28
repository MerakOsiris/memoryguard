"""V2-native governance query/command service for GUI operations.

Governance state is derived from V2 Memory atoms, supersession edges and the
GovernanceV2 decision ledger.  No ManagedStore, SharedMemoryStore, MemoryIR or
legacy quarantine/conflict file is imported.  Mutations always go through
GovernanceV2 so every state change has a compensating decision receipt and a
same-transaction decision outbox event.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from ..governance_v2 import GovernanceV2, V2Decision, V2MutationContext
from ..memory.store import MemoryAtom, MemoryAtomStore, MemoryReadScope
from ..sensitive_content import redact_sensitive_content
from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from .native_ports import NativeContextError, resolve_native_transport_context


class GovernanceNativeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "governance_operation_failed")
        super().__init__(self.code)


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(item) for item in parts).encode("utf-8")).hexdigest()


def _safe_preview(body: str, *, limit: int = 120) -> str:
    compact = str(body or "").strip().replace("\r", " ").replace("\n", " ")
    # Supersession decisions are a public GUI/native read.  The preview is
    # still useful after the same named secret redaction used by the other V2
    # public surfaces; no raw credential may cross this serialization seam.
    return redact_sensitive_content(compact)[: max(1, int(limit))]


def _masked_preview(body: str) -> str:
    length = len(str(body or ""))
    if length <= 0:
        return "••••"
    return "•" * min(max(length, 4), 24)


# Conflict members are deliberately stricter than ordinary memory reads.
# ``conflicted`` and ``low_confidence`` are the two states emitted by the
# organizer while a conflict is waiting for an administrator; an atom in any
# other state must not become a keeper merely because it still has a row.
_CONFLICT_LIVE_STATUSES = frozenset({"active", "conflicted", "low_confidence"})
_CONFLICT_STALE_STATUSES = frozenset({
    "deleted", "superseded", "rejected", "quarantined", "shadowed", "missing",
})
_CONFLICT_REASON_LABELS = {
    "canonical_composition_conflict": "相关记忆的内容主张互相矛盾，需选择保留版本",
    "explicit_composition_conflict": "相关记忆被明确标记为内容主张冲突，需选择保留版本",
    "automatic_semantic_conflict": "相关记忆的语义内容存在冲突，需选择保留版本",
    "same logical fact disagrees": "同一事实的内容主张不一致，需选择保留版本",
}


def _conflict_reason_label(reason: str) -> str:
    raw = str(reason or "").strip()
    if not raw:
        return "相关记忆的内容主张存在差异，需选择保留版本"
    return _CONFLICT_REASON_LABELS.get(raw.casefold(), _CONFLICT_REASON_LABELS.get(raw, _safe_preview(raw, limit=240)))


def _ambiguous_member_ids(
    stamped: Sequence[MemoryAtom],
    buckets: Mapping[str, Sequence[MemoryAtom]],
) -> set[str]:
    """Ids whose bucket holds more than one atom. Unknown ids are not ambiguous."""
    ids = {str(atom.memory_id) for atom in stamped if str(atom.memory_id or "").strip()}
    for atom in stamped:
        ids.update(_peer_ids(dict(atom.metadata or {})))
    return {
        member_id for member_id in ids
        if len(buckets.get(member_id, ())) > 1
    }


def _peer_ids(metadata: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in ("conflict_peer_ids", "conflict_member_ids", "member_ids"):
        peers = metadata.get(key)
        if isinstance(peers, str):
            peers = [peers]
        if isinstance(peers, (list, tuple, set)):
            found.update(str(peer).strip() for peer in peers if str(peer).strip())
    return found


def _conflict_member_state(status: str, *, available: bool) -> tuple[bool, str]:
    normalized = str(status or "missing").strip().casefold() or "missing"
    if not available:
        return False, "成员记录已不存在，且没有可恢复的历史正文"
    if normalized in _CONFLICT_LIVE_STATUSES:
        return True, "当前成员仍有效，可作为保留版本"
    if normalized == "deleted":
        return False, "成员已软删除，仅保留历史快照"
    if normalized == "superseded":
        return False, "成员已被更新版本替代，仅保留历史快照"
    if normalized == "rejected":
        return False, "成员已被拒绝，不可作为保留版本"
    if normalized == "quarantined":
        return False, "成员处于隔离状态，不可作为保留版本"
    if normalized == "shadowed":
        return False, "成员已被其他版本覆盖，不可作为保留版本"
    if normalized in _CONFLICT_STALE_STATUSES:
        return False, "成员当前已失效，不可作为保留版本"
    return False, "成员当前状态不可作为保留版本"


class GovernanceNativeService:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()

    @staticmethod
    def _require_admin(trusted: Mapping[str, Any]) -> None:
        """Require the immutable transport-issued administrator capability."""
        try:
            authority = resolve_native_transport_context(trusted)
        except NativeContextError as exc:
            raise GovernanceNativeError("admin_capability_required") from exc
        if not (
            bool(authority.admin)
            and authority.session_trusted is True
            and bool(authority.session_id)
            and authority.session_source.casefold() in {"host", "transport"}
        ):
            raise GovernanceNativeError("admin_capability_required")

    @staticmethod
    def _context(workspace: Path, trusted: Mapping[str, Any]) -> V2MutationContext:
        group = str(trusted.get("share_group_id") or "").strip()
        actor = str(trusted.get("agent_instance_id") or "").strip()
        if not group:
            raise GovernanceNativeError("share_group_id_required")
        if not actor:
            raise GovernanceNativeError("trusted_agent_required")
        try:
            return V2MutationContext(
                workspace_id=str(workspace),
                share_group_id=group,
                agent_instance_id=actor,
                project_ref=str(trusted.get("project_ref") or ""),
                provider=str(trusted.get("provider") or ""),
                runtime_role=str(trusted.get("runtime_role") or ""),
                actor=actor,
                admin=bool(trusted.get("admin") or trusted.get("is_admin")),
                authority="admin" if bool(trusted.get("admin") or trusted.get("is_admin")) else "manual",
            )
        except Exception as exc:
            raise GovernanceNativeError("governance_context_invalid") from exc

    @staticmethod
    def _read_scope(workspace: Path, trusted: Mapping[str, Any]) -> MemoryReadScope:
        group = str(trusted.get("share_group_id") or "").strip()
        if not group:
            raise GovernanceNativeError("share_group_id_required")
        # The process-issued group is the authorization boundary.  Empty
        # optional dimensions intentionally read all members in that one group;
        # they are not browser-controlled wildcards.
        return MemoryReadScope(
            workspace_id=str(workspace),
            share_group_id=group,
            admin=bool(trusted.get("admin") or trusted.get("is_admin")),
        )

    @property
    def _memory_db_path(self) -> Path:
        return WorkspaceV2Layout(self.workspace).memory_db

    def _memory(self, *, write: bool = False) -> MemoryAtomStore:
        try:
            return MemoryAtomStore(self.workspace, readonly=not write)
        except FileNotFoundError as exc:
            raise GovernanceNativeError("memory_db_missing") from exc

    def _governance(self) -> GovernanceV2:
        memory = self._memory(write=True)
        return GovernanceV2(self.workspace, memory_store=memory)

    @property
    def _decision_ledger_path(self) -> Path:
        return self.workspace / ".memoryguard" / "governance_v2" / "decisions.db"

    def _read_decisions(self) -> list[V2Decision]:
        """Read an existing governance ledger without creating any V2 state."""
        path = self._decision_ledger_path
        if not path.is_file():
            return []
        try:
            with open_database(path, readonly=True) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    ).fetchall()
                }
                if "decisions" not in tables:
                    return []
                rows = conn.execute(
                    "SELECT decision_id,operation,target_json,reason,confidence,undo_hash,"
                    "context_json,before_json,after_json,status,created_at,idempotency_key,request_fingerprint "
                    "FROM decisions ORDER BY created_at,decision_id"
                ).fetchall()
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise GovernanceNativeError("governance_ledger_read_failed") from exc
        result: list[V2Decision] = []
        for row in rows:
            try:
                target = json.loads(str(row[2] or "{}"))
                context = json.loads(str(row[6] or "{}"))
                before = json.loads(str(row[7] or "{}"))
                after = json.loads(str(row[8] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise GovernanceNativeError("governance_ledger_invalid") from exc
            if not all(isinstance(item, Mapping) for item in (target, context, before, after)):
                raise GovernanceNativeError("governance_ledger_invalid")
            result.append(V2Decision(
                decision_id=str(row[0]),
                operation=str(row[1]),
                target=dict(target),
                reason=str(row[3] or ""),
                confidence=float(row[4] if row[4] is not None else 1.0),
                undo_hash=str(row[5] or ""),
                context=dict(context),
                before=dict(before),
                after=dict(after),
                status=str(row[9] or "applied"),
                created_at=str(row[10] or ""),
                idempotency_key=str(row[11] or ""),
                request_fingerprint=str(row[12] or ""),
            ))
        return result

    def _atoms(self, trusted: Mapping[str, Any], *, status: str | None = None) -> list[MemoryAtom]:
        if not self._memory_db_path.is_file():
            return []
        return self._memory().list_atoms(
            scope=self._read_scope(self.workspace, trusted),
            status=status,
            include_building=True,
        )

    def _caller_read_scope(self, trusted: Mapping[str, Any]) -> MemoryReadScope | None:
        """Scope matching memory_read for this caller.

        Group-plane listing already returns every admin-visible atom. A
        non-admin memory_read still uses the caller agent. Conflict snapshots
        must see those same rows, and must not see another agent's audience.
        """
        if bool(trusted.get("admin") or trusted.get("is_admin")):
            return None
        agent = str(trusted.get("agent_instance_id") or "").strip()
        project = str(trusted.get("project_ref") or "").strip()
        provider = str(trusted.get("provider") or "").strip()
        runtime = str(trusted.get("runtime_role") or "").strip()
        group = str(trusted.get("share_group_id") or "").strip()
        if not group or not any((agent, project, provider, runtime)):
            return None
        return MemoryReadScope(
            workspace_id=str(self.workspace),
            share_group_id=group,
            agent_instance_id=agent,
            project_ref=project,
            provider=provider,
            runtime_role=runtime,
            admin=False,
        )

    def _conflict_sources(
        self, trusted: Mapping[str, Any],
    ) -> tuple[list[MemoryAtom], dict[str, list[MemoryAtom]]]:
        atoms = list(self._atoms(trusted))
        scope = self._caller_read_scope(trusted)
        if scope is not None and self._memory_db_path.is_file():
            seen = {atom.atom_id for atom in atoms}
            for atom in self._memory().list_atoms(scope=scope, include_building=True):
                if atom.atom_id not in seen:
                    atoms.append(atom)
                    seen.add(atom.atom_id)
        buckets: dict[str, list[MemoryAtom]] = {}
        for atom in atoms:
            for key in (str(atom.memory_id or "").strip(), str(atom.atom_id or "").strip()):
                if not key:
                    continue
                found = buckets.setdefault(key, [])
                if all(item.atom_id != atom.atom_id for item in found):
                    found.append(atom)
        return atoms, buckets

    def _conflict_membership(
        self, trusted: Mapping[str, Any], group_id: str,
    ) -> tuple[list[MemoryAtom], list[MemoryAtom], dict[str, list[MemoryAtom]]] | None:
        """Stamped rows of this group, plus unique in-scope peers.

        A peer that still belongs to another group is visible for the live
        count and the audit list.  It is not a row this group may rewrite.
        Several atoms sharing one memory_id are ambiguous: none is chosen.
        """
        atoms, buckets = self._conflict_sources(trusted)
        stamped: list[MemoryAtom] = []
        peer_ids: set[str] = set()
        seen: set[str] = set()
        for atom in atoms:
            metadata = dict(atom.metadata or {})
            if str(metadata.get("conflict_group_id") or "").strip() != group_id:
                continue
            if atom.atom_id in seen:
                continue
            seen.add(atom.atom_id)
            stamped.append(atom)
            peer_ids.update(_peer_ids(metadata))
        if not stamped:
            return None
        referenced: list[MemoryAtom] = []
        for member_id in sorted(peer_ids):
            matches = [item for item in buckets.get(member_id, ()) if item.atom_id not in seen]
            unique: list[MemoryAtom] = []
            for item in matches:
                if any(existing.atom_id == item.atom_id for existing in unique):
                    continue
                unique.append(item)
            if len(unique) != 1:
                continue
            referenced.append(unique[0])
            seen.add(unique[0].atom_id)
        return stamped, referenced, buckets

    def _find_atom(self, identifier: str, trusted: Mapping[str, Any]) -> MemoryAtom:
        value = str(identifier or "").strip()
        if not value:
            raise GovernanceNativeError("memory_id_required")
        atoms = self._atoms(trusted)
        prefix = value[6:] if value.startswith("claim-") else ""
        matches = [
            atom for atom in atoms
            if atom.memory_id == value
            or atom.atom_id == value
            or (prefix and atom.memory_id.startswith(prefix))
            or _digest(atom.memory_id) == value
        ]
        if len(matches) != 1:
            raise GovernanceNativeError("memory_not_found" if not matches else "memory_identifier_ambiguous")
        return matches[0]

    def recent_events(self, trusted: Mapping[str, Any], *, limit: int = 100) -> dict[str, Any]:
        ctx = self._context(self.workspace, trusted)
        decisions = self._read_decisions()
        visible = [
            item for item in decisions
            if str((item.context or {}).get("workspace_id") or "") == str(self.workspace)
            and str((item.context or {}).get("share_group_id") or "") == ctx.share_group_id
        ]
        visible = visible[-max(1, min(int(limit or 100), 500)):]
        events = [
            {
                "event_id": item.decision_id,
                "decision_id": item.decision_id,
                "action": item.operation,
                "operation": item.operation,
                "target": dict(item.target or {}),
                "reason": item.reason,
                "confidence": item.confidence,
                "status": item.status,
                "created_at": item.created_at,
                "actor": str((item.context or {}).get("actor") or ""),
                "agent_instance_id": str(
                    (item.context or {}).get("agent_instance_id")
                    or (item.context or {}).get("actor")
                    or ""
                ),
                "share_group_id": str((item.context or {}).get("share_group_id") or ""),
                "project_ref": str((item.context or {}).get("project_ref") or ""),
                "provider": str((item.context or {}).get("provider") or ""),
                "authority": str((item.context or {}).get("authority") or ""),
            }
            for item in visible
        ]
        return {"ok": True, "status": "succeeded", "events": events, "total": len(events)}

    def auto_actions(self, trusted: Mapping[str, Any], *, limit: int = 100) -> dict[str, Any]:
        events = self.recent_events(trusted, limit=max(int(limit or 100), 100))["events"]
        rows = [
            event for event in events
            if event.get("authority") in {"auto", "system"}
            or str(event.get("action") or "").startswith("auto_")
        ][: max(1, min(int(limit or 100), 500))]
        return {"ok": True, "status": "succeeded", "actions": rows, "total": len(rows)}

    def supersede_decisions(self, trusted: Mapping[str, Any], *, limit: int = 100) -> dict[str, Any]:
        scope = self._read_scope(self.workspace, trusted)
        if not self._memory_db_path.is_file():
            return {"ok": True, "status": "succeeded", "decisions": [], "total": 0}
        memory = self._memory()
        atoms = {atom.atom_id: atom for atom in memory.list_atoms(scope=scope, include_building=True)}
        if not atoms:
            return {"ok": True, "status": "succeeded", "decisions": [], "total": 0}
        ids = sorted(atoms)
        placeholders = ",".join("?" for _ in ids)
        with memory._connection() as conn:
            rows = conn.execute(
                f"SELECT old_atom_id,new_atom_id,reason,source_ref,created_at FROM supersession_edges "
                f"WHERE old_atom_id IN ({placeholders}) AND new_atom_id IN ({placeholders}) "
                "ORDER BY created_at DESC,edge_id DESC LIMIT ?",
                (*ids, *ids, max(1, min(int(limit or 100), 500))),
            ).fetchall()
        decisions = []
        for row in rows:
            old = atoms.get(str(row[0]))
            new = atoms.get(str(row[1]))
            if old is None or new is None:
                continue
            decisions.append({
                "old_memory_id": old.memory_id,
                "new_memory_id": new.memory_id,
                "old_content_preview": _safe_preview(old.body),
                "new_content_preview": _safe_preview(new.body),
                "reason": str(row[2] or ""),
                "source_ref": str(row[3] or ""),
                "created_at": str(row[4] or ""),
            })
        return {"ok": True, "status": "succeeded", "decisions": decisions, "total": len(decisions)}

    def conflicts(self, trusted: Mapping[str, Any]) -> dict[str, Any]:
        """Return self-contained, safe conflict snapshots for the GUI.

        Conflict membership is a historical fact.  Do not derive it only from
        currently live atoms: a tombstoned member still explains why the
        conflict existed, and organizer metadata may retain a peer ID after a
        compatibility import.  The atom body is already retained by the V2
        tombstone path; only a bounded redacted preview crosses this API.
        """
        atoms, buckets = self._conflict_sources(trusted)
        groups: dict[str, dict[str, Any]] = {}
        for atom in atoms:
            metadata = dict(atom.metadata or {})
            group_id = str(metadata.get("conflict_group_id") or "").strip()
            if not group_id:
                continue
            group = groups.setdefault(group_id, {"atoms": {}, "member_ids": set()})
            group["atoms"][atom.memory_id] = atom
            group["member_ids"].add(atom.memory_id)
            # Organizer stamps memory_id peers. Older rows may store an
            # atom_id. Resolve either against in-scope atoms. An unknown id
            # stays missing; do not chase a successor or another agent's row.
            group["member_ids"].update(_peer_ids(metadata))

        result = []
        for group_id, grouped in sorted(groups.items()):
            group_atoms = grouped["atoms"]
            members = [group_atoms[memory_id] for memory_id in sorted(group_atoms)]
            metadata_values = [dict(atom.metadata or {}) for atom in members]
            # A resolved keeper keeps conflict metadata for auditability.  Do
            # not resurrect that group merely because its deleted losers are
            # still retained as tombstones.
            if any(
                str(metadata.get("conflict_status") or "").strip().casefold() == "resolved"
                for metadata in metadata_values
            ):
                continue
            metadata = next(
                (item for item in metadata_values if item.get("conflict_status") or item.get("conflict_reason")),
                {},
            )
            raw_status = str(metadata.get("conflict_status") or "unresolved").strip().casefold() or "unresolved"
            raw_reason = str(metadata.get("conflict_reason") or "conflicting governed memory records").strip()
            member_ids = sorted(grouped["member_ids"])
            member_details = []
            shown_ids: list[str] = []
            seen_atoms: set[str] = set()
            for memory_id in member_ids:
                matches: list[MemoryAtom] = []
                for item in buckets.get(memory_id, ()):
                    if any(existing.atom_id == item.atom_id for existing in matches):
                        continue
                    matches.append(item)
                if len(matches) > 1:
                    shown_ids.append(memory_id)
                    member_details.append({
                        "memory_id": memory_id,
                        "body": "",
                        "preview": "",
                        "body_preview": "",
                        "kind": "",
                        "status": "ambiguous",
                        "selectable": False,
                        "live": False,
                        "available": False,
                        "missing": False,
                        "history_available": False,
                        "snapshot_status": "snapshot_unavailable",
                        "reason": "同一 memory_id 对应多条原子，不能选择其中一条删除",
                        "revision": None,
                        "created_at": "",
                        "updated_at": "",
                    })
                    continue
                atom = matches[0] if matches else None
                if atom is not None:
                    if atom.atom_id in seen_atoms:
                        continue
                    seen_atoms.add(atom.atom_id)
                    shown_id = str(atom.memory_id or memory_id)
                else:
                    shown_id = memory_id
                shown_ids.append(shown_id)
                available = atom is not None
                member_status = str(atom.status if atom is not None else "missing").strip().casefold() or "missing"
                selectable, member_reason = _conflict_member_state(member_status, available=available)
                body = str(atom.body or "") if atom is not None else ""
                preview = _safe_preview(body)
                member_details.append({
                    "memory_id": shown_id,
                    # ``body`` is intentionally the same bounded, redacted
                    # preview; the raw atom body never crosses this seam.
                    "body": preview,
                    "preview": preview,
                    "body_preview": preview,
                    "kind": str(atom.kind or "fact") if atom is not None else "",
                    "status": member_status,
                    "selectable": bool(selectable),
                    "live": bool(selectable),
                    "available": bool(available),
                    "missing": not available,
                    "history_available": bool(available and body),
                    "snapshot_status": "snapshot_available" if available else "snapshot_unavailable",
                    "reason": member_reason,
                    "revision": int(atom.revision) if atom is not None else None,
                    "created_at": str(atom.created_at or "") if atom is not None else "",
                    "updated_at": str(atom.updated_at or "") if atom is not None else "",
                })
            live_count = sum(1 for item in member_details if item["live"])
            can_resolve = live_count >= 2
            status = raw_status
            if raw_status in {"unresolved", "pending", "open"} and not can_resolve:
                status = "stale"
            invalid_reason = "" if can_resolve else (
                "冲突成员已失效或缺失；至少需要 2 条仍有效的记忆才能解决。"
            )
            if _ambiguous_member_ids(list(group_atoms.values()), buckets):
                can_resolve = False
                status = "ambiguous"
                invalid_reason = "同一 memory_id 对应多条原子，不能解决或关闭。"
            created_candidates = [
                str(item.get("conflict_created_at") or item.get("created_at") or "")
                for item in metadata_values
            ] + [str(item.get("created_at") or "") for item in member_details]
            created_candidates = [item for item in created_candidates if item]
            result.append({
                "group_id": group_id,
                "member_ids": shown_ids,
                "members": member_details,
                "member_details": member_details,
                "live_member_count": live_count,
                "selectable_member_ids": [item["memory_id"] for item in member_details if item["selectable"]],
                "can_resolve": can_resolve,
                "invalid_reason": invalid_reason,
                "status": status,
                "source_status": raw_status,
                "reason": _conflict_reason_label(raw_reason),
                "reason_code": _safe_preview(raw_reason, limit=240),
                "raw_reason": _safe_preview(raw_reason, limit=240),
                "created_at": min(created_candidates) if created_candidates else "",
            })
        actionable_total = sum(1 for item in result if item["can_resolve"])
        stale_total = sum(1 for item in result if item["status"] == "stale")
        return {
            "ok": True,
            "status": "succeeded",
            "conflicts": result,
            # ``total`` is the unclosed queue size. Keep legacy aliases while
            # exposing each user-facing disposition explicitly.
            "total": len(result),
            "unresolved_total": len(result),
            "history_total": len(result),
            "actionable_total": actionable_total,
            "selectable_total": actionable_total,
            "closable_stale_total": stale_total,
        }

    def quarantine(self, trusted: Mapping[str, Any]) -> dict[str, Any]:
        entries = []
        for atom in self._atoms(trusted, status="quarantined"):
            metadata = dict(atom.metadata or {})
            quarantine_id = str(metadata.get("quarantine_id") or ("quarantine-" + _digest(atom.atom_id)[:24]))
            entries.append({
                "quarantine_id": quarantine_id,
                "memory_id": atom.memory_id,
                "masked_preview": _masked_preview(atom.body),
                "reason": str(metadata.get("quarantine_reason") or "manual quarantine"),
                "detected_pattern": str(metadata.get("detected_pattern") or "manual"),
                "quarantined_at": str(metadata.get("quarantined_at") or atom.updated_at or atom.created_at),
                "released": False,
            })
        entries.sort(key=lambda item: (item["quarantined_at"], item["quarantine_id"]), reverse=True)
        return {"ok": True, "status": "succeeded", "quarantine": entries, "total": len(entries)}

    def memory_summary(self, trusted: Mapping[str, Any]) -> dict[str, Any]:
        atoms = self._atoms(trusted)
        by_status: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        for atom in atoms:
            by_status[atom.status] = by_status.get(atom.status, 0) + 1
            by_kind[atom.kind] = by_kind.get(atom.kind, 0) + 1
        return {
            "ok": True,
            "status": "succeeded",
            "coverage": {
                "total": len(atoms),
                "by_status": by_status,
                "by_kind": by_kind,
            },
            "records": [
                {
                    "memory_id": atom.memory_id,
                    "body": atom.body,
                    "kind": atom.kind,
                    "status": atom.status,
                    "confidence": atom.confidence,
                    "locked": bool(atom.locked),
                    "priority": atom.priority,
                    "injection_policy": atom.injection_policy,
                    "revision": atom.revision,
                }
                for atom in atoms
            ],
        }

    def memory_ir_summary(self, trusted: Mapping[str, Any]) -> dict[str, Any]:
        summary = self.memory_summary(trusted)
        return {
            "ok": True,
            "status": "succeeded",
            "record_count": summary["coverage"]["total"],
            "coverage": summary["coverage"],
            "records": [
                {
                    "memory_id": row["memory_id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "confidence": row["confidence"],
                    "revision": row["revision"],
                }
                for row in summary["records"]
            ],
        }

    def _update_atom(
        self,
        atom: MemoryAtom,
        trusted: Mapping[str, Any],
        *,
        status: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        reason: str,
        operation_key: str,
        governance: Any | None = None,
    ) -> tuple[MemoryAtom, Any]:
        governance = governance or self._governance()
        ctx = self._context(self.workspace, trusted)
        updated = replace(
            atom,
            status=str(status or atom.status),
            metadata=dict(metadata if metadata is not None else atom.metadata or {}),
        )
        try:
            return governance.put_atom(
                updated,
                context=ctx,
                reason=reason,
                confidence=1.0,
                idempotency_key=operation_key,
            )
        except Exception as exc:
            raise GovernanceNativeError(str(getattr(exc, "code", "") or "governance_memory_update_failed")) from exc

    def resolve_conflict(
        self,
        group_id: str,
        keep_memory_id: str,
        trusted: Mapping[str, Any],
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        group = str(group_id or "").strip()
        keep = str(keep_memory_id or "").strip()
        if not group or not keep:
            raise GovernanceNativeError("conflict_resolution_payload_required")
        # This is an administrative mutation.  Check the process-issued
        # capability before touching the conflict/group namespace so an
        # unprivileged caller cannot use lookup results as an oracle.
        self._require_admin(trusted)
        membership = self._conflict_membership(trusted, group)
        if membership is None:
            raise GovernanceNativeError("conflict_group_not_found")
        stamped, referenced, buckets = membership
        if _ambiguous_member_ids(stamped, buckets):
            raise GovernanceNativeError("conflict_member_ambiguous")
        live_stamped = [atom for atom in stamped if atom.status in _CONFLICT_LIVE_STATUSES]
        live_referenced = [atom for atom in referenced if atom.status in _CONFLICT_LIVE_STATUSES]
        members = live_stamped + live_referenced
        keeper = next((atom for atom in members if atom.memory_id == keep), None)
        if keeper is None or len(members) < 2:
            # Match the read surface: stale groups are visible for audit, but
            # their historical IDs may never be used to trigger a mutation.
            raise GovernanceNativeError("conflict_group_stale")
        # An always-injected rule is a protected governance input.  A generic
        # conflict resolution must never silently tombstone it.  Keep this
        # check before creating a decision or mutating any member so an absent
        # or rejected confirmation is fully fail-closed.
        keeper_stamped = any(atom.atom_id == keeper.atom_id for atom in stamped)
        losers = [
            atom for atom in (*live_stamped, *live_referenced)
            if atom.atom_id != keeper.atom_id
        ]
        protected_losers = [
            atom.memory_id
            for atom in losers
            if str(atom.injection_policy or "").strip().casefold() == "always"
        ]
        if protected_losers and confirmed is not True:
            raise GovernanceNativeError("conflict_always_rule_protected")
        governance = self._governance()
        ctx = self._context(self.workspace, trusted)
        decisions: list[str] = []
        deleted: list[str] = []
        try:
            # put_atom has no undo. One publication transaction covers the
            # resolved mark, every loser tombstone, and the keeper update.
            with governance.atomic_memory():
                for atom in losers:
                    stamped_loser = any(item.atom_id == atom.atom_id for item in stamped)
                    if stamped_loser and not keeper_stamped:
                        # Tombstone copies metadata. Mark this group's row
                        # resolved first so the queue clears without rewriting
                        # the external peer's old group.
                        meta = dict(atom.metadata or {})
                        meta["conflict_status"] = "resolved"
                        meta["conflict_resolution"] = "kept_peer"
                        atom, decision = self._update_atom(
                            atom,
                            trusted,
                            metadata=meta,
                            reason=f"resolve conflict {group}: keep peer {keeper.memory_id}",
                            operation_key=f"conflict:{group}:resolve-stamped:{atom.atom_id}:{atom.revision}",
                            governance=governance,
                        )
                        decisions.append(decision.decision_id)
                    _removed, decision = governance.tombstone(
                        atom.memory_id,
                        context=ctx,
                        reason=f"resolve conflict {group}: superseded by {keeper.memory_id}",
                        confidence=1.0,
                        idempotency_key=f"conflict:{group}:delete:{atom.atom_id}:{atom.revision}",
                    )
                    decisions.append(decision.decision_id)
                    deleted.append(atom.memory_id)
                if keeper_stamped:
                    keeper_meta = dict(keeper.metadata or {})
                    keeper_meta["conflict_status"] = "resolved"
                    keeper_meta["conflict_resolution"] = "kept"
                    _persisted, decision = self._update_atom(
                        keeper,
                        trusted,
                        metadata=keeper_meta,
                        reason="resolve conflict: keep selected memory",
                        operation_key=f"conflict:{group}:keep:{keeper.atom_id}:{keeper.revision}",
                        governance=governance,
                    )
                    decisions.append(decision.decision_id)
        except GovernanceNativeError:
            raise
        except Exception as exc:
            raise GovernanceNativeError("conflict_resolution_failed") from exc
        return {
            "ok": True,
            "status": "succeeded",
            "group_id": group,
            "keep_memory_id": keeper.memory_id,
            "deleted_memory_ids": deleted,
            "decision_ids": decisions,
        }

    def close_stale_conflict(
        self,
        group_id: str,
        trusted: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Close a conflict that no longer has two recoverable members.

        A stale conflict is not recoverable resolution: no winner is chosen
        and no deleted/missing atom is resurrected.  We record the closure on
        one deterministic in-scope anchor atom through GovernanceV2, so the
        queue is removed while the decision ledger retains the reason and
        member inventory for audit/rollback review.
        """
        group = str(group_id or "").strip()
        if not group:
            raise GovernanceNativeError("conflict_group_id_required")
        self._require_admin(trusted)

        membership = self._conflict_membership(trusted, group)
        if membership is None:
            raise GovernanceNativeError("conflict_group_not_found")
        stamped, referenced, buckets = membership
        if _ambiguous_member_ids(stamped, buckets):
            raise GovernanceNativeError("conflict_member_ambiguous")
        live_stamped = [atom for atom in stamped if atom.status in _CONFLICT_LIVE_STATUSES]
        live_referenced = [atom for atom in referenced if atom.status in _CONFLICT_LIVE_STATUSES]
        if len(live_stamped) + len(live_referenced) >= 2:
            raise GovernanceNativeError("conflict_group_actionable")
        # Closure is written only onto this group's stamped rows. A referenced
        # peer can make the group actionable, and is recorded for audit, but
        # its own group metadata is left untouched.
        if live_stamped:
            anchor = max(
                live_stamped,
                key=lambda atom: (int(atom.revision), str(atom.updated_at), atom.memory_id),
            )
        else:
            anchor = max(
                stamped,
                key=lambda atom: (int(atom.revision), str(atom.updated_at), atom.memory_id),
            )
        member_ids = sorted({atom.memory_id for atom in stamped} | {atom.memory_id for atom in referenced})
        anchor_meta = dict(anchor.metadata or {})
        anchor_meta.update({
            "conflict_status": "resolved",
            "conflict_resolution": "stale_closed",
            "conflict_close_reason": "no two recoverable conflict members remain",
            "conflict_closed_member_ids": member_ids,
        })
        try:
            _persisted, decision = self._update_atom(
                anchor,
                trusted,
                metadata=anchor_meta,
                reason=f"close stale conflict {group}: no two recoverable members remain",
                operation_key=f"conflict:{group}:close-stale:{anchor.atom_id}:{anchor.revision}",
            )
        except GovernanceNativeError:
            raise
        except Exception as exc:
            raise GovernanceNativeError("stale_conflict_close_failed") from exc
        return {
            "ok": True,
            "status": "closed",
            "closure_kind": "stale_unrecoverable",
            "group_id": group,
            "closed_memory_ids": [anchor.memory_id],
            "member_ids": member_ids,
            "decision_ids": [decision.decision_id],
        }

    def _find_quarantine(self, quarantine_id: str, trusted: Mapping[str, Any]) -> MemoryAtom:
        value = str(quarantine_id or "").strip()
        if not value:
            raise GovernanceNativeError("quarantine_id_required")
        matches = []
        for atom in self._atoms(trusted, status="quarantined"):
            metadata = dict(atom.metadata or {})
            candidate = str(metadata.get("quarantine_id") or ("quarantine-" + _digest(atom.atom_id)[:24]))
            if candidate == value:
                matches.append(atom)
        if len(matches) != 1:
            raise GovernanceNativeError("quarantine_not_found")
        return matches[0]

    def release_quarantine(self, quarantine_id: str, trusted: Mapping[str, Any]) -> dict[str, Any]:
        self._require_admin(trusted)
        atom = self._find_quarantine(quarantine_id, trusted)
        metadata = dict(atom.metadata or {})
        metadata["quarantine_released"] = True
        metadata["governance_action"] = "release_quarantine"
        persisted, decision = self._update_atom(
            atom,
            trusted,
            status="active",
            metadata=metadata,
            reason="release quarantined memory",
            operation_key=f"quarantine:release:{atom.atom_id}:{atom.revision}",
        )
        return {
            "ok": True,
            "status": "succeeded",
            "quarantine_id": str(quarantine_id),
            "memory_id": persisted.memory_id,
            "decision_id": decision.decision_id,
        }

    def delete_quarantine(self, quarantine_id: str, trusted: Mapping[str, Any]) -> dict[str, Any]:
        self._require_admin(trusted)
        atom = self._find_quarantine(quarantine_id, trusted)
        governance = self._governance()
        ctx = self._context(self.workspace, trusted)
        try:
            persisted, decision = governance.tombstone(
                atom.memory_id,
                context=ctx,
                reason="delete quarantined memory",
                confidence=1.0,
                idempotency_key=f"quarantine:delete:{atom.atom_id}:{atom.revision}",
            )
        except Exception as exc:
            raise GovernanceNativeError("quarantine_delete_failed") from exc
        return {
            "ok": True,
            "status": "succeeded",
            "quarantine_id": str(quarantine_id),
            "memory_id": persisted.memory_id,
            "decision_id": decision.decision_id,
        }

    def neuron_decide(
        self,
        node_id: str,
        action: str,
        reason: str,
        trusted: Mapping[str, Any],
        *,
        target_scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_admin(trusted)
        atom = self._find_atom(node_id, trusted)
        action_name = str(action or "").strip().casefold()
        if action_name not in {"accept", "exclude", "quarantine", "supersede", "merge", "rescope"}:
            raise GovernanceNativeError("neuron_action_invalid")
        reason_text = str(reason or f"neuron {action_name}").strip()[:1024] or f"neuron {action_name}"
        if action_name == "rescope":
            if not isinstance(target_scope, Mapping):
                raise GovernanceNativeError("rescope_target_required")
            # Scope identity changes are binding/control-plane operations, not a
            # mutable field on an existing MemoryAtom.  The GUI must provide a
            # target group/agent through the group command path.
            raise GovernanceNativeError("rescope_use_group_scope_operation")

        metadata = dict(atom.metadata or {})
        metadata["governance_action"] = action_name
        if action_name == "quarantine":
            quarantine_id = str(metadata.get("quarantine_id") or ("quarantine-" + _digest(atom.atom_id)[:24]))
            metadata.update({
                "quarantine_id": quarantine_id,
                "quarantine_reason": reason_text,
                "detected_pattern": str(metadata.get("detected_pattern") or "manual"),
                "quarantined_at": atom.updated_at or atom.created_at,
                "quarantine_released": False,
            })
            new_status = "quarantined"
        elif action_name == "exclude":
            new_status = "rejected"
        elif action_name == "supersede":
            new_status = "superseded"
        else:
            # ``merge`` on the current GUI is confirmation of an already
            # clustered claim anchor, not a two-record merge request.  V2 keeps
            # the canonical atom active and records the decision receipt.
            new_status = "active"
            if action_name == "merge":
                metadata["merge_confirmed"] = True
        persisted, decision = self._update_atom(
            atom,
            trusted,
            status=new_status,
            metadata=metadata,
            reason=reason_text,
            operation_key=f"neuron:{action_name}:{atom.atom_id}:{atom.revision}",
        )
        return {
            "ok": True,
            "status": "succeeded",
            "memory_id": persisted.memory_id,
            "target_id": persisted.memory_id,
            "action": action_name,
            "memory_status": persisted.status,
            "decision_id": decision.decision_id,
            "revision": persisted.revision,
        }

    def outbox_status(self, trusted: Mapping[str, Any]) -> dict[str, Any]:
        ctx = self._context(self.workspace, trusted)
        scope_digest = hashlib.sha256(
            json.dumps({
                "workspace_id": ctx.workspace_id,
                "share_group_id": ctx.share_group_id,
                "agent_instance_id": ctx.agent_instance_id,
                "project_ref": ctx.project_ref,
                "provider": ctx.provider,
                "runtime_role": ctx.runtime_role,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        path = self._decision_ledger_path
        if not path.is_file():
            return {"ok": True, "status": "succeeded", "outbox": {}}
        try:
            with open_database(path, readonly=True) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    ).fetchall()
                }
                if "decision_outbox" not in tables:
                    rows = []
                else:
                    rows = conn.execute(
                        "SELECT status,COUNT(*) FROM decision_outbox WHERE scope_digest=? GROUP BY status",
                        (scope_digest,),
                    ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise GovernanceNativeError("governance_outbox_read_failed") from exc
        counts = {str(row[0]): int(row[1]) for row in rows}
        return {"ok": True, "status": "succeeded", "outbox": counts}


__all__ = ["GovernanceNativeError", "GovernanceNativeService"]
