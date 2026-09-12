"""Deterministic, read-only long-term-memory context bootstrap.

This module does not inspect or replace the host's current conversation.  It
selects a bounded packet from an already-open, trusted SharedMemoryStore.
"""

from __future__ import annotations

import json

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auto_organizer import SECRET_PATTERNS
from .schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)
from .rule_scope import effective_assignments, normalize_assignment
from .rule_scope import canonical_project_ref
from .runtime_v2.governance_semantics import (
    classify_governance_relation,
    governance_scope_key,
)
from .runtime_v2.context_budget import (
    ContextBudget,
    DeterministicTokenCounter,
    MANDATORY_ITEM_WARNING_THRESHOLD,
)
from .rule_read_path import (
    MODE_LEGACY,
    MODE_RULE_INTELLIGENCE,
    resolve_read_path_mode,
)

# Mandatory injection limits live in runtime_v2.context_budget.  Optional
# recall still uses the local request ceilings below.
_MANDATORY_BUDGET = ContextBudget()
_MANDATORY_TOKEN_COUNTER = DeterministicTokenCounter()
MANDATORY_MAX_ITEMS = MANDATORY_ITEM_WARNING_THRESHOLD
MANDATORY_MAX_CHARS = _MANDATORY_BUDGET.mandatory_max_chars


DEFAULT_MAX_ITEMS = 12
DEFAULT_MAX_CHARS = 6000
MAX_ITEMS_LIMIT = 20
MAX_CHARS_LIMIT = 12000
PER_ITEM_CHAR_LIMIT = 1600
PREFERENCE_MAX_ITEMS = 5


def knowledge_reference_candidates(
    workspace: str,
    *,
    namespace_id: str,
    agent_instance_id: str,
    project_ref: str,
    provider: str,
    share_group_id: str,
    sensitivity: str,
    policy_class: str,
    workspace_id: str | None = None,
    query: str = "",
    limit: int = 6,
) -> tuple[dict[str, str], ...]:
    """Read V2 knowledge as exact-ACL, reference-only bootstrap candidates.

    This helper intentionally has no legacy-store fallback.  Missing or
    incomplete identity is a deny result, not a wildcard query.
    """
    values = {
        "namespace_id": namespace_id,
        "workspace_id": workspace_id or str(Path(workspace).resolve()),
        "agent_instance_id": agent_instance_id,
        "project_ref": project_ref,
        "provider": provider,
        "share_group_id": share_group_id,
        "sensitivity": sensitivity,
        "policy_class": policy_class,
    }
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in values.values()
    ):
        return ()
    from .content.store import ContentReadScope, ContentStore
    from .knowledge_v2.adapter import KnowledgeV2Adapter
    from .storage.layout import WorkspaceV2Layout

    layout = WorkspaceV2Layout(workspace)
    if not layout.content_db.is_file():
        return ()
    scope = ContentReadScope(**values)
    adapter = KnowledgeV2Adapter(
        ContentStore(workspace, initialize=False),
        namespace_id=namespace_id,
    )
    return tuple(adapter.read(scope, query=query, limit=limit))


def _trusted_group_members(
    workspace: str | Path,
    *,
    agent_instance_id: str,
    share_group_id: str,
) -> tuple[str, ...]:
    """Resolve a read-only group audience from persisted V2 bindings."""

    from .runtime_v2.group_native import GroupControlService

    agent = str(agent_instance_id or "").strip()
    group = str(share_group_id or "").strip()
    if not agent or not group:
        return ()
    try:
        service = GroupControlService(workspace, write=False)
        binding = service.active_binding_for_agent(agent)
        # Native context already authenticated exact workspace/agent/group.
        # Group-control bindings add shared-member expansion, but absence of
        # a binding must not erase the caller's own personal history/graph
        # scope during early V2 bootstrap.
        if not binding or str(binding.get("share_group_id") or "") != group:
            return (agent,)
        members = tuple(sorted({
            str(item.get("agent_instance_id") or "")
            for item in service.list_bindings(include_inactive=False).get("bindings", [])
            if str(item.get("share_group_id") or "") == group
            and str(item.get("agent_instance_id") or "")
        }))
        return members or (agent,)
    except Exception:
        return ()


def history_reference_candidates(
    workspace: str | Path,
    *,
    agent_instance_id: str,
    project_ref: str,
    provider: str,
    share_group_id: str,
    query: str = "",
    limit: int = 6,
) -> tuple[dict[str, str], ...]:
    """Read only bounded V2 history summaries as reference-only candidates."""

    project = canonical_project_ref(project_ref)
    agent = str(agent_instance_id or "").strip()
    group = str(share_group_id or "").strip()
    provider_value = str(provider or "").strip().casefold()
    members = _trusted_group_members(
        workspace,
        agent_instance_id=agent,
        share_group_id=group,
    )
    if not project or not provider_value or not members:
        return ()
    try:
        from .runtime_v2.history_store import ContentHistoryStore, V2HistoryScope

        scope = V2HistoryScope(
            agent_instance_id=agent,
            project_ref=project,
            provider=provider_value,
            share_group_id=group,
            authorized_agent_ids=members,
            shared_read=len(members) > 1,
        )
        store = ContentHistoryStore(workspace, readonly=True)
        return tuple(store.summary_references(scope, query=query, limit=limit))
    except Exception:
        # Missing/partial content V2 is a safe omission, never a fallback to
        # raw session files or the retired history store.
        return ()


def codegraph_reference_candidates(
    workspace: str | Path,
    *,
    agent_instance_id: str,
    project_ref: str,
    share_group_id: str,
    provider: str = "",
    runtime_role: str = "",
    query: str = "",
    limit: int = 1,
) -> tuple[dict[str, Any], ...]:
    """Read bounded, trusted CodeGraph references relevant to current task.

    This is metadata-only.  It never builds a graph or traverses arbitrary
    edges.  The aggregate status remains available as a light signal; symbol
    references are added only when task terms match indexed production
    metadata.  A pending incremental receipt is represented as a high-priority
    reference so ContextEngine budget/receipt handling decides whether it was
    delivered.
    """

    project = canonical_project_ref(project_ref)
    agent = str(agent_instance_id or "").strip()
    group = str(share_group_id or "").strip()
    provider_value = str(provider or "").strip().casefold()
    if not project or not agent or not group or not provider_value:
        return ()
    members = _trusted_group_members(
        workspace,
        agent_instance_id=agent,
        share_group_id=group,
    )
    if not members:
        return ()
    try:
        from .codegraph_v2.store import CodeGraphStore

        store = CodeGraphStore(workspace, initialize=False)
        rows = store.reference_metadata(
            project_ref=project,
            share_group_id=group,
            provider=provider_value,
            agent_instance_id=agent,
            runtime_role=runtime_role,
            limit=limit,
        )
        references: list[dict[str, Any]] = []
        seen_references: set[tuple[str, str]] = set()

        def add_reference(value: dict[str, Any]) -> None:
            identity = (str(value.get("ref") or ""), str(value.get("hash") or ""))
            if not identity[0] and not identity[1]:
                return
            if identity in seen_references:
                return
            seen_references.add(identity)
            references.append(value)

        for row in rows:
            counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
            files = row.get("files") if isinstance(row.get("files"), list) else []
            file_parts: list[str] = []
            for file in files[:8]:
                if not isinstance(file, dict):
                    continue
                path = str(file.get("path") or "")
                digest = str(file.get("hash") or "")
                symbols = file.get("symbols") if isinstance(file.get("symbols"), list) else []
                names = [
                    str(symbol.get("name") or "")
                    for symbol in symbols[:12]
                    if isinstance(symbol, dict) and str(symbol.get("name") or "")
                ]
                detail = " ".join(item for item in (path, digest, *names) if item)
                if detail:
                    file_parts.append(detail[:240])
            file_suffix = f"; indexed: {'; '.join(file_parts)}" if file_parts else ""
            material = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = stable_hash("codegraph-reference", material)
            references.append({
                "summary": (
                    f"CodeGraph metadata for {row.get('project_ref', '')}: "
                    f"{int(counts.get('source_files', 0))} files, "
                    f"{int(counts.get('symbols', 0))} symbols, "
                    f"{int(counts.get('edges', 0))} edges{file_suffix}"
                ),
                "ref": f"codegraph:{row.get('scope_id', '')}",
                "hash": digest,
                "trust": "reference_only",
                "source": "native-v2-codegraph",
                "priority": 0,
            })

        # Resolve the same nearest trusted scopes used by the aggregate query.
        # Scope resolution enforces workspace/group/agent/provider boundaries;
        # no caller-controlled scope is accepted from task text.
        scopes = store.nearest_scopes(
            project_ref=project,
            share_group_id=group,
            agent_instance_id=agent,
            provider=provider_value,
            runtime_role=runtime_role,
            limit=limit,
        )
        terms = sorted(
            (item for item in _tokens(query) if len(item) >= 2),
            key=lambda item: (-len(item), item),
        )[:8]
        seen_symbols: set[str] = set()
        pending_receipt: dict[str, Any] | None = None
        for scope in scopes:
            files_by_id: dict[str, str] | None = None
            for term in terms:
                try:
                    symbols = store.query_symbols(
                        term,
                        scope=scope,
                        provenance="production",
                        limit=max(1, min(limit, 8)),
                    )
                except Exception:
                    # An aggregate status is still safe and useful when a
                    # symbol query is unavailable or an old schema is partial.
                    continue
                for symbol in symbols:
                    if symbol.symbol_id in seen_symbols:
                        continue
                    seen_symbols.add(symbol.symbol_id)
                    source_map = symbol.source_map if isinstance(symbol.source_map, dict) else {}
                    path = str(source_map.get("path") or "")
                    if not path:
                        try:
                            if files_by_id is None:
                                files_by_id = {
                                    item.file_id: item.path
                                    for item in store.list_source_files(scope=scope)
                                }
                            path = str(files_by_id.get(symbol.file_id) or "")
                        except Exception:
                            path = ""
                    location = f"L{symbol.line_start}"
                    if symbol.line_end and symbol.line_end != symbol.line_start:
                        location += f"-L{symbol.line_end}"
                    summary = (
                        f"CodeGraph symbol {symbol.name} ({symbol.kind}) "
                        f"at {path}:{location}"
                    )
                    add_reference({
                        "summary": summary[:1200],
                        "ref": f"codegraph:symbol:{symbol.symbol_id}",
                        "hash": stable_hash(
                            "codegraph-symbol",
                            symbol.symbol_id,
                            symbol.symbol_hash,
                            path,
                            location,
                        ),
                        "trust": "reference_only",
                        "source": "native-v2-codegraph",
                        "priority": 20,
                    })

            # Receipt delivery is one item per bootstrap across all eligible
            # scopes.  Scopes are already deterministically nearest-first; the
            # first valid pending receipt wins so one packet cannot expose
            # several receipts while native dispatch consumes only one.
            if pending_receipt is None:
                try:
                    candidate_receipt = store.peek_affected_receipt(scope=scope)
                except Exception:
                    candidate_receipt = None
                if candidate_receipt:
                    receipt_id = str(candidate_receipt.get("receipt_id") or "")
                    digest = str(candidate_receipt.get("digest") or "")
                    if receipt_id and digest:
                        pending_receipt = candidate_receipt

        if pending_receipt:
            receipt = pending_receipt
            receipt_id = str(receipt.get("receipt_id") or "")
            digest = str(receipt.get("digest") or "")
            result_count = len(receipt.get("result_ids") or ())
            start_count = len(receipt.get("start_ids") or ())
            result_ids = tuple(
                str(item).strip()
                for item in (receipt.get("result_ids") or ())
                if str(item).strip()
            )[:32]
            start_ids = tuple(
                str(item).strip()
                for item in (receipt.get("start_ids") or ())
                if str(item).strip()
            )[:8]
            affected_ids = ", ".join(result_ids or start_ids)
            if len(affected_ids) > 760:
                affected_ids = affected_ids[:757] + "..."
            add_reference({
                "summary": (
                    f"CodeGraph affected update: {result_count} symbols "
                    f"from {start_count} changed roots, depth "
                    f"{int(receipt.get('depth') or 0)}; changed/affected IDs: "
                    f"{affected_ids}"
                ),
                "ref": f"codegraph:affected:{receipt_id}",
                "hash": digest,
                "trust": "reference_only",
                "source": "native-v2-codegraph",
                "priority": 100,
            })
        return tuple(references)
    except Exception:
        return ()


def consume_codegraph_affected_receipt(
    workspace: str | Path,
    *,
    scope: Any,
    receipt_id: str = "",
) -> dict[str, Any] | None:
    """Consume one already-bounded incremental CodeGraph receipt.

    This path never builds, updates, or traverses CodeGraph.  Its only write
    is marking one persisted receipt consumed, which makes injection finite.
    Caller owns trusted scope resolution.
    """

    try:
        from .codegraph_v2.store import CodeGraphStore

        store = CodeGraphStore(workspace, initialize=False)
        expected = str(receipt_id or "").strip()
        # The current transport scope may be a caller/provider tuple while a
        # GUI Graphify build is persisted at trusted group scope.  Resolve
        # only store-owned nearest scopes for the same project/group and try
        # the exact receipt ID; never accept a scope from request payload.
        scopes: list[Any] = [scope]
        if expected:
            try:
                scope_value = scope.to_dict() if hasattr(scope, "to_dict") else dict(scope)
                nearest = store.nearest_scopes(
                    project_ref=str(scope_value.get("project_ref") or ""),
                    share_group_id=str(scope_value.get("share_group_id") or ""),
                    agent_instance_id=str(scope_value.get("agent_instance_id") or ""),
                    provider=str(scope_value.get("provider") or ""),
                    runtime_role=str(scope_value.get("runtime_role") or ""),
                    limit=4,
                )
                scopes.extend(nearest)
            except Exception:
                pass
        seen_scopes: set[str] = set()
        for candidate_scope in scopes:
            try:
                scope_key = str(getattr(candidate_scope, "digest", "")) or repr(candidate_scope)
                if scope_key in seen_scopes:
                    continue
                seen_scopes.add(scope_key)
                receipt = store.consume_affected_receipt(
                    scope=candidate_scope,
                    receipt_id=expected,
                )
                if receipt:
                    return receipt
            except Exception:
                continue
        return None
    except Exception:
        return None


def unified_reference_candidates(
    workspace: str | Path,
    *,
    agent_instance_id: str,
    project_ref: str,
    provider: str,
    share_group_id: str,
    namespace_id: str = "",
    sensitivity: str = "",
    policy_class: str = "",
    runtime_role: str = "",
    query: str = "",
    limit: int = 6,
) -> tuple[dict[str, str], ...]:
    """Return one deterministic, reference-only retrieval stream.

    Knowledge, history and CodeGraph stay separate at source, then merge in
    fixed channel order. Each adapter is fail-closed and may independently
    return no data. No turn/blob/source body crosses this boundary.
    """

    channels: tuple[tuple[str, tuple[dict[str, str], ...]], ...] = (
        (
            "knowledge",
            knowledge_reference_candidates(
                str(workspace),
                namespace_id=str(namespace_id or ""),
                workspace_id=str(Path(workspace).resolve()),
                agent_instance_id=str(agent_instance_id or ""),
                project_ref=canonical_project_ref(project_ref),
                provider=str(provider or ""),
                share_group_id=str(share_group_id or ""),
                sensitivity=str(sensitivity or ""),
                policy_class=str(policy_class or ""),
                query=query,
                limit=limit,
            ),
        ),
        (
            "history",
            history_reference_candidates(
                workspace,
                agent_instance_id=agent_instance_id,
                project_ref=project_ref,
                provider=provider,
                share_group_id=share_group_id,
                query=query,
                limit=limit,
            ),
        ),
        (
            "codegraph",
            codegraph_reference_candidates(
                workspace,
                agent_instance_id=agent_instance_id,
                project_ref=project_ref,
                share_group_id=share_group_id,
                provider=provider,
                runtime_role=runtime_role,
                query=query,
                limit=min(limit, 4),
            ),
        ),
    )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for channel, values in channels:
        for value in values:
            if not isinstance(value, dict):
                continue
            summary = str(value.get("summary") or "").strip()[:1200]
            ref = str(value.get("ref") or "").strip()[:512]
            digest = str(value.get("hash") or "").strip()[:256]
            if not summary and not ref and not digest:
                continue
            identity = (summary, ref, digest)
            if identity in seen:
                continue
            seen.add(identity)
            # Internal source marker is consumed by ContextEngine before the
            # public reference renderer strips all non-reference fields.
            result.append({
                "summary": summary,
                "ref": ref,
                "hash": digest,
                "trust": "reference_only",
                "source": f"native-v2-{channel}",
                "id": f"{channel}:{ref or digest}",
            })
    return tuple(result)

_REDACTED_MARKER = re.compile(r"\[REDACTED(?::[^\]]+)?\]", re.IGNORECASE)
_WORD_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]*")
_HAN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_RELEVANT_KINDS = {
    MemoryKind.PROCEDURE,
    MemoryKind.PROJECT,
    MemoryKind.CORRECTION,
    MemoryKind.FACT,
}
_KIND_ORDER = {
    MemoryKind.PREFERENCE: 0,
    MemoryKind.CORRECTION: 1,
    MemoryKind.PROCEDURE: 2,
    MemoryKind.PROJECT: 3,
    MemoryKind.FACT: 4,
}


@dataclass(frozen=True)
class _Candidate:
    record: SharedMemoryRecord
    reason: str
    relevance: int


def _tokens(text: str) -> set[str]:
    value = text or ""
    tokens = {token.casefold() for token in _WORD_PATTERN.findall(value)}
    # Chinese single-character overlap is too weak for context injection.
    # Bigrams retain useful matching without network/tokenizer dependencies.
    for run in _HAN_PATTERN.findall(value):
        if len(run) == 1:
            tokens.add(run)
            continue
        tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _normalized_body(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _contains_sensitive_content(text: str) -> bool:
    """Fail closed: raw secret or any redaction placeholder is omitted."""
    if not text or _REDACTED_MARKER.search(text):
        return bool(_REDACTED_MARKER.search(text or ""))
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _sort_key(candidate: _Candidate) -> tuple[Any, ...]:
    record = candidate.record
    # Preference is always first.  Then relevance, locked manual overrides,
    # confidence, timestamp, and id make selection stable and explainable.
    return (
        _KIND_ORDER.get(record.kind, 99),
        -candidate.relevance,
        -int(record.locked),
        -float(record.confidence),
        record.updated_at or record.created_at or "",
        record.memory_id,
    )


def _mandatory_scope_key(
    record: SharedMemoryRecord,
    assignments_by_memory: dict[str, list[Any]],
    *,
    effective_context: EffectiveAgentContext,
    group_id: str,
    direct_compat: bool,
) -> tuple[Any, ...]:
    """Return full persisted audience identity for render-time collapse.

    Matching one Agent is not enough to prove two rules have the same audience:
    a group rule and an Agent overlay can both apply to that Agent while
    widening different future readers.  Collapse only identical assignment
    multisets.  Direct compatibility mode has no assignment plane, so its
    explicit group is the whole scope.
    """

    assignments = assignments_by_memory.get(record.memory_id, [])
    if direct_compat or not assignments:
        return governance_scope_key(
            metadata={
                "audience": {
                    "source": "native_v2",
                    "target_type": "group",
                    "target_id": group_id,
                    "share_group_id": group_id,
                },
            },
            share_group_id=group_id,
        )
    keys: list[tuple[str, str, str, str, str, str, str]] = []
    for assignment in assignments:
        keys.append(
            governance_scope_key(
                metadata={
                    "audience": {
                        "source": "native_v2",
                        "target_type": str(getattr(assignment, "target_type", "") or ""),
                        "target_id": str(getattr(assignment, "target_id", "") or ""),
                        "share_group_id": group_id,
                        "project_ref": str(getattr(assignment, "project_ref", "") or ""),
                        "provider": str(getattr(assignment, "provider", "") or ""),
                        "runtime_role": str(getattr(assignment, "runtime_role", "") or ""),
                        "effect": str(getattr(assignment, "effect", "include") or "include"),
                    },
                },
                share_group_id=group_id,
                project_ref=effective_context.project_ref,
                provider=effective_context.provider,
                runtime_role=effective_context.runtime_role,
            )
        )
    return tuple(sorted(keys))


def _collapse_mandatory_semantic_duplicates(
    records: list[SharedMemoryRecord],
    assignments_by_memory: dict[str, list[Any]],
    effective_priorities: dict[str, int],
    *,
    effective_context: EffectiveAgentContext,
    group_id: str,
    direct_compat: bool,
) -> tuple[list[SharedMemoryRecord], list[dict[str, str]]]:
    """Collapse safe same-scope rules before mandatory budget validation.

    This is read-path governance only: it does not delete, shadow, or lock
    records.  Reconciliation can later persist the same decision; until then,
    a temporary duplicate cannot exhaust the mandatory package and prevent
    ordinary recovery/approval calls.
    """

    def rank(record: SharedMemoryRecord) -> tuple[Any, ...]:
        return (
            -int(effective_priorities.get(record.memory_id, record.priority) or 0),
            -int(bool(record.locked)),
            -float(record.confidence or 0.0),
            str(record.memory_id or ""),
        )

    kept: list[SharedMemoryRecord] = []
    omissions: list[dict[str, str]] = []
    for record in sorted(records, key=rank):
        record_scope = _mandatory_scope_key(
            record,
            assignments_by_memory,
            effective_context=effective_context,
            group_id=group_id,
            direct_compat=direct_compat,
        )
        matched = False
        for index, existing in enumerate(kept):
            existing_scope = _mandatory_scope_key(
                existing,
                assignments_by_memory,
                effective_context=effective_context,
                group_id=group_id,
                direct_compat=direct_compat,
            )
            if existing_scope != record_scope:
                continue
            relation = classify_governance_relation(existing.body, record.body)
            if not relation.mergeable:
                continue
            if relation.winner == "right":
                kept[index] = record
                winner = record
            else:
                winner = existing
            omissions.append({
                "memory_id": str(record.memory_id if winner is existing else existing.memory_id),
                "canonical_memory_id": str(winner.memory_id),
                "reason": (
                    "governance_update_shadowed"
                    if relation.kind in {"update", "additive"}
                    else "governance_duplicate"
                ),
            })
            matched = True
            break
        if not matched:
            kept.append(record)
    return kept, omissions


def _folded_source_ids(store: Any, group_id: str) -> set[str]:
    """Return legacy sources folded by the latest reconciliation job.

    A saga may shadow sources before its final commit, and a completed job also
    shadows folded originals after the canonical layer is ready.  The legacy
    fallback must keep reading those rules in either state; otherwise a failed
    partial job or an explicit legacy read after ``canonical_ready`` silently
    empties rules that are supposed to remain available.
    """
    try:
        native_db = (
            Path(store.workspace).resolve()
            / ".memoryguard" / "rules" / "rules.db"
        )
        if native_db.is_file():
            from .rules.v2_store import RuleV2Store

            def _native_jobs(conn: Any) -> list[dict[str, Any]]:
                return [
                    dict(row) for row in conn.execute(
                        "SELECT * FROM rule_reconciliation_jobs "
                        "WHERE share_group_id=? ORDER BY created_at DESC, job_id DESC",
                        (group_id,),
                    ).fetchall()
                ]

            jobs = RuleV2Store(store.workspace, read_only=True)._read(_native_jobs)
        else:
            from .rule_merge_store import RuleMergeStore
            from .rule_reconciliation import RuleReconciliationStore

            jobs = RuleReconciliationStore(
                RuleMergeStore(store.workspace, read_only=True),
            ).list_jobs(share_group_id=group_id)
    except Exception:
        return set()
    try:
        latest = jobs[0] if jobs else None
        if not latest:
            return set()
        raw = latest.get("result_json") or ""
        if not raw:
            return set()
        plan = json.loads(raw)
    except Exception:
        return set()
    folded: set[str] = set()
    for bundle in plan.get("bundles", []) or []:
        for source_id in bundle.get("source_memory_ids", []) or []:
            folded.add(str(source_id))
    return folded


def build_context_packet(
    store: Any,
    *,
    task: str,
    project_hint: str = "",
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
    effective_context: EffectiveAgentContext | None = None,
    read_path: str = "auto",
) -> dict[str, Any]:
    """Build bounded long-term-memory context from a read-only trusted store.

    ``read_path`` controls the Phase5 canonical read path.  The default is
    ``auto``: it keeps using the legacy byte-for-byte path until a group is
    canonically ready, then serves the finalized canonical layer so a
    successful migration can never leave the default read with an empty rule
    set.  Explicit ``legacy`` forces the old path.  ``auto`` /
    ``rule-intelligence`` never switch the group to canonical by themselves
    (Req8): the canonical layer only engages
    when the group-level canonical activation is persisted
    (``rule_canonical_state`` ``activation_status == "active"``) *and*
    ``canonical_reconciliation_status`` reports ``canonical_ready``.  Any other
    outcome keeps ``effective_read_path`` on ``legacy`` and records why in
    ``fallback_reason``.  When canonical does engage, merged duplicates are
    deduplicated only *after* the active/audience/exclude match so a collapse
    can never under-expose a rule.
    """
    task = (task or "").strip()
    if not task:
        raise ValueError("task is required")
    if not 1 <= int(max_items) <= MAX_ITEMS_LIMIT:
        raise ValueError(f"max_items must be between 1 and {MAX_ITEMS_LIMIT}")
    if not 256 <= int(max_chars) <= MAX_CHARS_LIMIT:
        raise ValueError(f"max_chars must be between 256 and {MAX_CHARS_LIMIT}")

    direct_compat = effective_context is None
    if direct_compat:
        effective_context = EffectiveAgentContext("__direct__", store.group_id)
    else:
        if not effective_context.agent_instance_id:
            raise ValueError("effective agent context is required for mandatory rule bootstrap")
        if effective_context.share_group_id != store.group_id:
            raise ValueError("effective agent context share_group_id mismatch")
    project_ref = canonical_project_ref(effective_context.project_ref)
    session_id = str(effective_context.session_id or "").strip()
    context_hash = str(effective_context.context_hash or "").strip() or stable_hash(
        "rule-context",
        effective_context.agent_instance_id,
        effective_context.share_group_id,
        project_ref,
        effective_context.provider,
        effective_context.runtime_role,
        session_id,
    )
    task_hash = stable_hash(
        "rule-bootstrap", task, store.group_id, effective_context.agent_instance_id,
        project_ref, effective_context.provider, effective_context.runtime_role,
        session_id, context_hash,
    )
    query_tokens = _tokens(f"{task} {project_hint}")
    all_records = store.list_records()
    # Phase5 canonical read path (shadow by default: ``read_path`` and the env
    # fallback resolve to legacy unless explicitly opted in).  The mapping is
    # resolved up front but applied *after* active/audience/exclude matching
    # below, so a canonical collapse can never drop the one record that applies
    # to the current Agent/project.  Body text still comes from the legacy
    # record; old tables are untouched.
    canonical_mapping: dict[str, Any] | None = None
    # Req8 canonical gate.  ``requested_read_path`` is the caller's raw value;
    # ``effective_read_path`` only becomes "rule-intelligence" when the
    # group-level canonical activation is persisted AND reconciliation
    # readiness passes.  Any other outcome stays on legacy with
    # ``fallback_reason`` recording why (a legacy request is not a fallback).
    requested_read_path = read_path
    effective_read_path = "legacy"
    fallback_reason = ""
    canonical_definitions = 0
    canonical_ready = False
    read_path_summary: dict[str, Any] = {
        "mode": MODE_LEGACY,
        "canonical_definitions": 0,
        "records_before": 0,
        "records_after": 0,
        "deduplicated": 0,
    }
    if resolve_read_path_mode(read_path) != MODE_LEGACY:
        gate_pass = False
        try:
            from .rule_merge_store import RuleMergeStore
            from .rule_reconciliation import (
                RuleReconciliationStore,
                canonical_reconciliation_status,
            )

            rule_db = (
                store.workspace / ".memoryguard" / "rule-intelligence" / "memory.db"
            )
            if not rule_db.exists():
                fallback_reason = "canonical_not_initialized"
            else:
                rms = RuleMergeStore(store.workspace, read_only=True)
                activation = RuleReconciliationStore(rms).canonical_activation(
                    store.group_id,
                )
                status = canonical_reconciliation_status(
                    store.workspace, store.group_id, store=rms,
                )
                canonical_definitions = int(
                    status.get("checks", {}).get("canonical_definitions", 0) or 0
                )
                canonical_ready = bool(status.get("canonical_ready", False))
                if (
                    activation
                    and str(activation.get("activation_status", "") or "") == "active"
                    and canonical_ready
                ):
                    gate_pass = True
                elif not activation or str(
                    activation.get("activation_status", "") or ""
                ) != "active":
                    fallback_reason = "canonical_not_activated"
                else:
                    fallback_reason = "readiness_failed:" + ",".join(
                        status.get("failures") or []
                    )
        except Exception:
            canonical_ready = False
            canonical_definitions = 0
            fallback_reason = "evaluation_failed"
        if gate_pass:
            try:
                from .rule_read_path import RuleReadPath

                read = RuleReadPath(store.workspace, store.group_id)
                if not read.has_intelligence():
                    raise RuntimeError("intelligence projection missing")
                # Use the persisted group readiness shadow summary.  After a
                # successful generation, originals are legitimately shadowed;
                # recomputing the old legacy-vs-canonical diff over those
                # shadowed rows would falsely report permission drift.
                shadow_summary = (
                    status.get("checks", {}).get("shadow")
                    if isinstance(status, dict) else None
                )
                canonical_mapping = read.resolve_canonical_map(
                    known_memory_ids={r.memory_id for r in all_records},
                    legacy_store=store,
                    context=effective_context,
                    shadow_summary=shadow_summary,
                )
                if not isinstance(canonical_mapping, dict):
                    raise RuntimeError("canonical mapping did not resolve")
                effective_read_path = "rule-intelligence"
            except Exception:
                canonical_mapping = None  # keep the legacy path; canonical is advisory
                effective_read_path = "legacy"
                fallback_reason = "canonical_mapping_unavailable"
    # Canonical output records are internal projection data, not legacy
    # governance input.  Before readiness they must not leak into a fallback
    # packet, otherwise the "legacy" read path would silently switch behavior.
    packet_records = all_records
    folded_source_ids: set[str] = set()
    if effective_read_path != MODE_RULE_INTELLIGENCE:
        shadowed_non_canonical = {
            record.memory_id for record in all_records
            if (
                record.status == SharedMemoryStatus.SHADOWED
                and not str(
                    getattr(record, "dedup_domain", "") or ""
                ).startswith("canonical:")
            )
        }
        if shadowed_non_canonical:
            folded_source_ids = (
                _folded_source_ids(store, store.group_id)
                & shadowed_non_canonical
            )
        packet_records = [
            record for record in all_records
            if not str(getattr(record, "dedup_domain", "") or "").startswith(
                "canonical:"
            )
        ]
    omitted = {
        "non_active": 0,
        "sensitive": 0,
        "irrelevant": 0,
        "duplicate": 0,
        "budget": 0,
        "unsupported_kind": 0,
    }
    omitted_details: list[dict[str, str]] = []
    audit_actions: list[dict[str, str]] = []

    # Stage 1: a separate mandatory package.  It intentionally bypasses task
    # overlap and ordinary recall budgets, but remains status/safety/dedup
    # governed.  The raw active set is checked first: historical corruption is
    # a hard error, never a silent bootstrap truncation.
    # Public MCP and host hooks always pass a trusted context.  Keep the
    # in-process helper backward compatible for offline reports/tests only;
    # it is never reachable through the MCP capability boundary.
    all_assignments = store.list_rule_assignments()
    assignments_by_memory: dict[str, list[Any]] = {}
    malformed_assignments: set[str] = set()
    for assignment in all_assignments:
        assignments_by_memory.setdefault(assignment.memory_id, []).append(assignment)
        try:
            normalize_assignment(assignment)
        except ValueError:
            malformed_assignments.add(assignment.memory_id)
    raw_mandatory: list[SharedMemoryRecord] = []
    assignment_receipt: dict[str, list[dict[str, Any]]] = {
        "system": [], "group": [], "project": [], "agent": [], "role": [],
        "excluded": [], "skipped": [], "corrupt": [],
    }
    mandatory_receipt_refs: dict[str, list[str]] = {}
    effective_priorities: dict[str, int] = {}
    legacy_unscoped: list[str] = []
    scoped_corrupt: list[SharedMemoryRecord] = []
    for record in packet_records:
        if record.status != SharedMemoryStatus.ACTIVE:
            if (
                str(getattr(record, "dedup_domain", "") or "").startswith(
                    "canonical:"
                )
                or record.memory_id not in folded_source_ids
            ):
                continue
        record_assignments = assignments_by_memory.get(record.memory_id, [])
        if record.injection_policy not in {"always", "relevant"}:
            if direct_compat:
                assignment_receipt["corrupt"].append({
                    "memory_id": record.memory_id,
                    "reason": "corrupt_rule_governance_quarantine",
                })
                continue
            includes, excludes = effective_assignments(
                record_assignments, effective_context,
            )
            # A known audience only fail-closes its own packet.  A malformed
            # assignment cannot be safely targeted, so it is governed as a
            # quarantine condition and never injected anywhere.
            if includes or excludes:
                scoped_corrupt.append(record)
                assignment_receipt["corrupt"].append({
                    "memory_id": record.memory_id,
                    "reason": "corrupt_rule_matched_audience",
                    "assignment_ids": [item.assignment_id for item in includes + excludes],
                })
            elif record.memory_id in malformed_assignments or not record_assignments:
                assignment_receipt["corrupt"].append({
                    "memory_id": record.memory_id,
                    "reason": "corrupt_rule_governance_quarantine",
                })
            continue
        if record.injection_policy != "always":
            continue
        if direct_compat:
            includes, excludes = ([object()], [])
        else:
            includes, excludes = effective_assignments(
                assignments_by_memory.get(record.memory_id, []), effective_context,
            )
        if not record_assignments and not direct_compat:
            legacy_unscoped.append(record.memory_id)
            assignment_receipt["skipped"].append({
                "memory_id": record.memory_id,
                "reason": "legacy_unscoped_governance_required",
            })
            continue
        if excludes:
            for assignment in excludes:
                assignment_receipt["excluded"].append({
                    "assignment_id": assignment.assignment_id,
                    "memory_id": record.memory_id,
                    "target_type": assignment.target_type,
                    "target_id": assignment.target_id,
                    "project_ref": assignment.project_ref,
                    "base_priority": record.priority,
                    "effective_priority": record.priority,
                    "reason": "matched_exclude_precedence",
                })
            continue
        if not includes:
            for assignment in record_assignments:
                assignment_receipt["skipped"].append({
                    "assignment_id": assignment.assignment_id,
                    "memory_id": record.memory_id,
                    "reason": "audience_not_matched",
                })
            continue
        raw_mandatory.append(record)
        overrides = [
            item.priority_override for item in includes
            if hasattr(item, "priority_override")
            and item.priority_override is not None
        ]
        effective_priorities[record.memory_id] = (
            max(overrides) if overrides else record.priority
        )
        for assignment in includes:
            if not hasattr(assignment, "target_type"):
                continue
            mandatory_receipt_refs.setdefault(record.memory_id, []).append(
                assignment.assignment_id
            )
            bucket = {
                "runtime_role": "role", "agent_project": "project"
            }.get(
                assignment.target_type, assignment.target_type,
            )
            if bucket in assignment_receipt:
                assignment_receipt[bucket].append({
                    "assignment_id": assignment.assignment_id,
                    "memory_id": record.memory_id,
                    "target_type": assignment.target_type,
                    "target_id": assignment.target_id,
                    "project_ref": assignment.project_ref,
                    "effect": assignment.effect,
                    "base_priority": record.priority,
                    "effective_priority": effective_priorities[record.memory_id],
                    "reason": "matched_include",
                })
    # Canonical dedup of the *matched* mandatory set (post audience/exclude):
    # a merged duplicate collapses to its strongest representative, but only
    # among records that actually apply to this Agent/project.
    if canonical_mapping:
        from .rule_read_path import dedupe_records

        def _mandatory_key(record: Any) -> tuple:
            return (
                -int(effective_priorities.get(record.memory_id, record.priority) or 0),
                -int(1 if record.locked else 0),
                -float(record.confidence or 0.0),
                str(record.memory_id or ""),
            )

        read_path_summary["records_before"] += len(raw_mandatory)
        raw_mandatory = dedupe_records(
            raw_mandatory, canonical_mapping, key=_mandatory_key,
        )
        read_path_summary["records_after"] += len(raw_mandatory)

    # Canonical mapping may be unavailable during a recoverable reconciliation
    # window.  Still collapse only safe same-audience semantic duplicates
    # before locking mandatory budget.  This is a read projection; durable
    # source rows remain active and recoverable until reconciliation commits.
    raw_mandatory, semantic_omissions = _collapse_mandatory_semantic_duplicates(
        raw_mandatory,
        assignments_by_memory,
        effective_priorities,
        effective_context=effective_context,
        group_id=store.group_id,
        direct_compat=direct_compat,
    )
    for omission in semantic_omissions:
        omitted["duplicate"] += 1
        omitted_details.append({
            "memory_id": omission["memory_id"],
            "reason": omission["reason"],
            "canonical_memory_id": omission["canonical_memory_id"],
        })
        audit_actions.append({
            "action": "collapse",
            "target_id": omission["canonical_memory_id"],
            "old_id": omission["memory_id"],
            "reason": omission["reason"],
        })
    if semantic_omissions:
        read_path_summary["records_after"] = len(raw_mandatory)

    invalid_mandatory_priorities = [
        record for record in raw_mandatory
        if isinstance(effective_priorities.get(record.memory_id), bool)
        or not isinstance(effective_priorities.get(record.memory_id), int)
        or not -100 <= effective_priorities.get(record.memory_id, 0) <= 100
    ]
    sensitive_mandatory = [
        record for record in raw_mandatory
        if _contains_sensitive_content(record.body)
    ]
    oversized_mandatory = [
        record for record in raw_mandatory
        if _MANDATORY_BUDGET.mandatory_item_oversize(
            len(record.body or ""),
            _MANDATORY_TOKEN_COUNTER.count(record.body or ""),
        )
    ]
    mandatory_chars = sum(len(record.body or "") for record in raw_mandatory)
    mandatory_tokens = sum(
        _MANDATORY_TOKEN_COUNTER.count(record.body or "") for record in raw_mandatory
    )
    mandatory_error = ""
    if sensitive_mandatory:
        omitted["sensitive"] += len(sensitive_mandatory)
        omitted_details.extend(
            {"memory_id": record.memory_id, "reason": "mandatory_sensitive"}
            for record in sensitive_mandatory
        )
        mandatory_error = "mandatory_sensitive_blocked"
    elif invalid_mandatory_priorities or scoped_corrupt:
        mandatory_error = (
            "mandatory_rule_package_invalid: active always rules are sensitive, "
            "contain invalid settings, or have a corrupt matched rule"
        )
    elif oversized_mandatory:
        omitted_details.extend(
            {"memory_id": record.memory_id, "reason": "mandatory_item_limit"}
            for record in oversized_mandatory
        )
        mandatory_error = "mandatory_item_limit_exceeded"
    elif _MANDATORY_BUDGET.mandatory_aggregate_overflow(mandatory_chars, mandatory_tokens):
        mandatory_error = "mandatory_budget_exceeded"
    mandatory_overflow = bool(mandatory_error)
    if mandatory_overflow:
        omitted["mandatory_overflow"] = len(raw_mandatory) + len(invalid_mandatory_priorities)

    mandatory_items: list[dict[str, Any]] = []
    mandatory_ids: list[str] = []
    mandatory_seen: set[str] = set()
    mandatory_match_receipts: list[dict[str, Any]] = []
    if not mandatory_overflow:
        for record in sorted(
            raw_mandatory,
            key=lambda item: (
                -int(effective_priorities[item.memory_id]), -int(item.locked),
                -float(item.confidence), item.memory_id,
            ),
        ):
            if _contains_sensitive_content(record.body):
                omitted["sensitive"] += 1
                omitted_details.append({"memory_id": record.memory_id, "reason": "sensitive"})
                continue
            normalized = _normalized_body(record.body)
            if normalized in mandatory_seen:
                omitted["duplicate"] += 1
                omitted_details.append({"memory_id": record.memory_id, "reason": "duplicate"})
                continue
            mandatory_seen.add(normalized)
            mandatory_ids.append(record.memory_id)
            mandatory_items.append({
                "memory_id": record.memory_id,
                "kind": record.kind.value,
                "body": record.body.strip(),
                "reason": "mandatory_rule",
                "confidence": record.confidence,
                "manual_override": bool(record.locked),
                "priority": effective_priorities[record.memory_id],
                "base_priority": record.priority,
                "truncated": False,
            })
            mandatory_match_receipts.append(
                RuleMatchReceipt(
                    receipt_id=stable_hash(
                        "rule-bootstrap-receipt-v2",
                        store.group_id,
                        effective_context.agent_instance_id,
                        session_id,
                        effective_context.session_source,
                        context_hash,
                        task_hash,
                        record.memory_id,
                    ),
                    memory_id=record.memory_id,
                    share_group_id=store.group_id,
                    agent_instance_id=effective_context.agent_instance_id,
                    task_hash=task_hash,
                    task=task,
                    assignment_ids=mandatory_receipt_refs.get(record.memory_id, []),
                    selection_reason="mandatory_rule",
                    matcher_version="rule-bootstrap-v1",
                    confidence=float(record.confidence),
                    created_at=_now_iso(),
                    project_ref=project_ref,
                    provider=effective_context.provider,
                    runtime_role=effective_context.runtime_role,
                    session_id=session_id,
                    context_hash=context_hash,
                    session_trusted=effective_context.session_trusted,
                    session_source=effective_context.session_source,
                ).to_dict()
            )
    mandatory_warning = _MANDATORY_BUDGET.item_count_warning(len(mandatory_items))
    mandatory_warnings = [mandatory_warning] if mandatory_warning else []
    mandatory_used_chars = sum(len(item.get("body") or "") for item in mandatory_items)
    mandatory_used_tokens = sum(
        _MANDATORY_TOKEN_COUNTER.count(item.get("body") or "") for item in mandatory_items
    )

    # Stage 2: ordinary task-relevant recall.  Mandatory records never consume
    # these slots/characters and are not reconsidered as preferences.
    candidates: list[_Candidate] = []

    for record in packet_records:
        if record.status != SharedMemoryStatus.ACTIVE:
            omitted["non_active"] += 1
            continue
        if record.injection_policy == "always":
            continue
        if record.injection_policy != "relevant":
            omitted["unsupported_kind"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "invalid_injection_policy"})
            continue
        if _contains_sensitive_content(record.body):
            omitted["sensitive"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "sensitive"})
            continue
        if record.kind == MemoryKind.PREFERENCE:
            overlap = len(query_tokens & _tokens(record.body))
            reason = "long_term_preference"
            if overlap:
                reason = f"long_term_preference+task_overlap:{overlap}"
            candidates.append(_Candidate(record, reason, overlap))
            continue
        if record.kind not in _RELEVANT_KINDS:
            omitted["unsupported_kind"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "unsupported_kind"})
            continue
        overlap = len(query_tokens & _tokens(record.body))
        if overlap < 2:
            omitted["irrelevant"] += 1
            omitted_details.append({"memory_id": record.memory_id, "reason": "irrelevant"})
            continue
        candidates.append(_Candidate(record, f"task_overlap:{overlap}", overlap))

    candidates.sort(key=_sort_key)

    # Exact normalized-body dedup happens after ranking so manual/stronger
    # records win deterministically.
    unique: list[_Candidate] = []
    # Relevant recall and mandatory rules are distinct injection semantics;
    # equal prose may legitimately appear in both packets.
    seen_bodies: set[str] = set()
    for candidate in candidates:
        normalized = _normalized_body(candidate.record.body)
        if normalized in seen_bodies:
            omitted["duplicate"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "duplicate"})
            continue
        seen_bodies.add(normalized)
        unique.append(candidate)

    # Canonical dedup of the *matched* relevant set (post active/relevance): the
    # first (best-ranked) candidate per canonical definition survives.
    if canonical_mapping:
        from .rule_read_path import dedupe_records

        memory_to_definition = canonical_mapping.get("memory_to_definition") or {}
        read_path_summary["records_before"] += len(unique)

        def _relevant_key(candidate: _Candidate) -> tuple:
            return (0,)  # keep ranking order; first per definition wins

        deduped = dedupe_records(
            [c.record for c in unique], canonical_mapping, key=_relevant_key,
        )
        kept_ids = {r.memory_id for r in deduped}
        unique = [c for c in unique if c.record.memory_id in kept_ids]
        read_path_summary["records_after"] += len(unique)

    if canonical_mapping:
        from .rule_read_path import canonical_read_summary

        read_path_summary = canonical_read_summary(
            canonical_mapping,
            int(read_path_summary["records_before"]),
            int(read_path_summary["records_after"]),
        )
    # Req8: the summary reports the real group-level canonical definition count
    # (from reconciliation status), not only the definitions this packet mapped.
    read_path_summary["canonical_definitions"] = canonical_definitions

    preferences = [
        item for item in unique if item.record.kind == MemoryKind.PREFERENCE
    ]
    relevant = [
        item for item in unique if item.record.kind != MemoryKind.PREFERENCE
    ]
    preference_slots = min(PREFERENCE_MAX_ITEMS, max_items)
    if relevant:
        # Preserve at least one task-relevant slot when caller gives capacity.
        preference_slots = min(preference_slots, max(0, max_items - 1))
    preference_char_budget = max_chars
    if relevant:
        preference_char_budget = max_chars // 2

    items: list[dict[str, Any]] = []
    used_chars = 0

    def _select(candidate: _Candidate, char_ceiling: int) -> bool:
        nonlocal used_chars
        body = candidate.record.body.strip()
        remaining = min(max_chars - used_chars, char_ceiling)
        if remaining <= 0:
            return False
        item_limit = min(PER_ITEM_CHAR_LIMIT, remaining)
        selected_body = body[:item_limit]
        if not selected_body:
            return False
        item = {
            "memory_id": candidate.record.memory_id,
            "kind": candidate.record.kind.value,
            "body": selected_body,
            "reason": candidate.reason,
            "confidence": candidate.record.confidence,
            "manual_override": bool(candidate.record.locked),
            "truncated": len(selected_body) < len(body),
        }
        items.append(item)
        used_chars += len(selected_body)
        return True

    for index, candidate in enumerate(preferences):
        if index >= preference_slots:
            omitted["budget"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "budget"})
            continue
        if not _select(candidate, preference_char_budget - used_chars):
            omitted["budget"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "budget"})

    for candidate in relevant:
        if len(items) >= max_items:
            omitted["budget"] += 1
            omitted_details.append({"memory_id": candidate.record.memory_id, "reason": "budget"})
            continue
        if not _select(candidate, max_chars - used_chars):
            omitted["budget"] += 1

    # Stage 3: KAG 知识书库检索（独立预算，不占记忆名额）
    knowledge_items: list[dict[str, Any]] = []
    # Use only the V2 content plane.  Knowledge remains reference-only and is
    # filtered by the same exact trusted ACL tuple as memory/rule retrieval.
    try:
        knowledge_items.extend(knowledge_reference_candidates(
            getattr(store, "workspace", ""),
            namespace_id=str(getattr(effective_context, "namespace_id", "") or ""),
            workspace_id=str(getattr(effective_context, "workspace_id", "") or ""),
            agent_instance_id=str(effective_context.agent_instance_id or ""),
            project_ref=str(project_ref or ""),
            provider=str(effective_context.provider or ""),
            share_group_id=str(effective_context.share_group_id or ""),
            sensitivity=str(getattr(effective_context, "sensitivity", "") or ""),
            policy_class=str(getattr(effective_context, "policy_class", "") or ""),
            query=task,
            limit=6,
        ))
    except Exception:
        pass

    return {
        "context_packet": {
            "scope": "long_term_memory_only",
            "host_conversation": "unchanged_not_duplicated",
            "task": task,
            "project_hint": (project_hint or "").strip(),
            "items": items,
            "mandatory_items": mandatory_items,
            "knowledge_items": knowledge_items,
        },
        "share_group_id": store.group_id,
        "active_version": store.get_active_version_id(),
        "selection": {
            "policy": "mandatory_rules_then_active_preferences_then_task_relevant",
            "selected_count": len(items),
            "omitted": omitted,
            "omitted_details": omitted_details,
        },
        "mandatory_rule_ids": mandatory_ids,
        "mandatory_match_receipts": mandatory_match_receipts,
        "effective_agent": {
            "agent_instance_id": effective_context.agent_instance_id,
            "provider": effective_context.provider,
            "project_ref": effective_context.project_ref,
            "runtime_role": effective_context.runtime_role,
            "runtime_agent_id": effective_context.runtime_agent_id,
            "parent_agent_id": effective_context.parent_agent_id,
            "session_id": session_id,
            "context_hash": context_hash,
        },
        "assignment_receipt": assignment_receipt,
        "legacy_unscoped_rule_ids": legacy_unscoped,
        "read_path": read_path_summary,
        "requested_read_path": requested_read_path,
        "effective_read_path": effective_read_path,
        "fallback_reason": fallback_reason,
        "canonical_definitions": canonical_definitions,
        "canonical_ready": canonical_ready,
        "recalled_memory_ids": [item["memory_id"] for item in items],
        "mandatory_overflow": mandatory_overflow,
        "mandatory_invalid_reason": mandatory_error,
        "error": mandatory_error,
        "status": "blocked" if mandatory_error else "ok",
        "audit_actions": audit_actions,
        "budget": {
            "max_items": max_items,
            "used_items": len(items),
            "max_chars": max_chars,
            "used_chars": used_chars,
            "per_item_max_chars": PER_ITEM_CHAR_LIMIT,
            "preference_max_items": PREFERENCE_MAX_ITEMS,
            "preference_char_budget": preference_char_budget,
            "mandatory_max_items": _MANDATORY_BUDGET.mandatory_max_items,
            "mandatory_max_chars": _MANDATORY_BUDGET.mandatory_max_chars,
            "mandatory_max_tokens": _MANDATORY_BUDGET.mandatory_max_tokens,
            "mandatory_used_items": len(mandatory_items),
            "mandatory_used_chars": mandatory_used_chars,
            "mandatory_used_tokens": mandatory_used_tokens,
            "warnings": mandatory_warnings,
            "limits": _MANDATORY_BUDGET.to_dict(),
            "mandatory": {
                "items": len(mandatory_items),
                "chars": mandatory_used_chars,
                "tokens": mandatory_used_tokens,
            },
        },
    }
