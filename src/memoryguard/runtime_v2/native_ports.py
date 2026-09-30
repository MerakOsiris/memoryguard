"""Native V2 runtime ports and surface registry.

The cutover facade owns the manifest state machine.  This module owns the
production-side dispatch table: it binds the public MCP/GUI/CLI/Hook names to
V2 stores/services, and returns an explicit activation blocker for every name
that has not yet been migrated.  It deliberately does not import a legacy
store, transport server, GUI implementation, or host-hook implementation.

The port is dependency-injected and lazy.  A read-only call never creates a
missing database or repairs a partial schema; a write call only opens a V2
store after a read-only schema preflight has succeeded.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
import sqlite3
from threading import Lock, RLock
import weakref
from pathlib import Path
from typing import Any, Callable, Mapping

from ..cutover_v2.surfaces import (
    CLI_COMMAND_NAMES,
    GUI_MUTATION_NAMES,
    GUI_METHOD_NAMES,
    GUI_OPERATION_SPECS,
    GUI_MUTATION_NAMES,
    MCP_BROKER_GUI_METHOD_NAMES,
    MCP_GUI_BRIDGE_OPERATIONS,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
    get_gui_operation_spec,
    resolve_mcp_broker_invocation,
)
from ..rule_read_path import normalize_canonical_read_path
from ..rule_scope import canonical_project_ref
from ..storage.layout import WorkspaceV2Layout
from ..storage.database import connect_database, open_database


_HOOK_NAMES = frozenset({"bootstrap_hook"})
_NATIVE_CONTEXT_CAPABILITY = object()
# Native authority is intentionally process-local.  The registry is an
# identity table (rather than an equality check) so shallow/deep copies,
# hand-written lookalikes and deserialised values cannot become capabilities.
# Keep only weak references so request-scoped envelopes do not pin every
# authority issued by a long-running MCP/GUI host forever.  The envelope (or
# another active caller) remains the strong owner for the duration of a call.
_NATIVE_BOUND_CONTEXTS: weakref.WeakValueDictionary[int, "NativeBoundContext"] = weakref.WeakValueDictionary()
# A separate process-local token gates dependency injection.  The transport
# capability above authenticates request identity; this token authenticates
# test/host wiring and must never be accepted from a plain Mapping/JSON value.
_NATIVE_INJECTION_CAPABILITY = object()
# Only the GUI mutation adapter can hold this process-local marker.  It lets
# the state-preserving path retain an existing V2 atom's complete metadata
# without accepting a forgeable JSON flag from MCP callers.
_GUI_STATE_PRESERVATION_CAPABILITY = object()
_NATIVE_MEMORY_LOCK_GUARD = Lock()
_NATIVE_MEMORY_LOCKS: dict[tuple[str, str], RLock] = {}


def _native_memory_mutation_lock(workspace: str | Path, share_group_id: str) -> RLock:
    key = (str(Path(workspace).expanduser().resolve()), str(share_group_id))
    with _NATIVE_MEMORY_LOCK_GUARD:
        lock = _NATIVE_MEMORY_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _NATIVE_MEMORY_LOCKS[key] = lock
        return lock


_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "workspace_id": ("workspace_id", "workspace"),
    "agent_instance_id": ("agent_instance_id", "agent_id", "agent", "trusted_agent_id", "trusted_agent"),
    "share_group_id": ("share_group_id", "group_id", "group", "trusted_group_id", "trusted_group"),
    "project_ref": ("project_ref", "project_id", "project", "trusted_project_ref", "trusted_project"),
    "provider": ("provider", "trusted_provider"),
    "runtime_role": ("runtime_role", "runtime", "trusted_runtime_role", "trusted_runtime"),
}
_IDENTITY_PAYLOAD_KEYS = frozenset(
    alias for aliases in _IDENTITY_ALIASES.values() for alias in aliases
) | frozenset(
    {
        "runtime_agent_id",
        "parent_agent_id",
        "session_id",
        "session_source",
        "session_trusted",
        "context_hash",
        "trusted_identity",
        "trusted_context",
        "identity",
        "entrypoint",
    }
)

_PHASE9_GUI_HANDLERS = frozenset({
    "history_search", "history_timeline", "history_read",
    "history_extract_preview", "history_list_sessions", "history_export",
    "history_delete", "list_sources", "import_preview", "scan_summary",
    "memory_versions", "memory_supersede_chain",
})
_PHASE9_GUI_READ_HANDLERS = _PHASE9_GUI_HANDLERS - {"history_delete"}

# These GUI fields select the business target displayed/acted on by the page.
# They are never a second authorization source: projection/release handlers
# always derive exact scope from the process-issued NativeBoundContext.
_GUI_BUSINESS_SELECTOR_OPERATIONS = frozenset({
    "get_governance_snapshot",
    "get_neuron_graph", "get_memory_neuron_graph", "get_codegraph_graph",
    "get_projection_source_map", "build_projection", "start_build_projection",
    "cancel_build_projection", "delete_projection", "set_projection_source_enabled",
    "list_native_memory_releases", "list_releases", "list_publish_targets",
    "choose_publish_target_path", "create_build_plan", "apply_build",
    "publish_reconstructed_memory", "verify_release", "rollback_release",
    "rollback_native_memory_release",
})
_GUI_BUSINESS_SELECTOR_KEYS = frozenset({
    "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role",
    "scope",
})

# Agent discovery/lifecycle reads are guarded by the process-issued GUI
# capability, but they do not read a MemoryGuard group.  Requiring a group
# here made an unbound/residual Agent impossible to inspect, which in turn
# trapped the GUI in ``active_binding_required`` before the user could choose
# an existing group or enable a personal layer.
_GUI_AGENT_READ_HANDLERS = frozenset({"gui_agent_query"})
_GUI_HISTORY_SESSION_SELECTOR_OPERATIONS = frozenset({
    "history_read", "history_timeline", "history_extract_preview",
})

# These MCP reads intentionally expose only aggregate/availability
# diagnostics.  They must remain usable before a host Agent binding exists;
# all scoped reads and every mutation still require the process-issued native
# context below.
_NEUTRAL_MCP_READS = frozenset({
    "memoryguard_canonical_status",
    "memoryguard_diagnostics_snapshot",
    "memoryguard_projection_status",
    "memoryguard_runtime_processes",
    "memoryguard_capabilities",
})


# Native mutation is allowed to open a writable store only after this
# read-only contract has passed.  The phase-2 stores intentionally keep their
# marker/version in a domain-specific metadata table (SQLite user_version is
# still zero in existing workspaces), so preflight checks both metadata rows
# and the concrete tables/columns used by GovernanceV2.
_NATIVE_SCHEMA_SPECS: Mapping[str, Mapping[str, Any]] = {
    "memory": {
        "meta": (
            ("schema_meta", "memory", "memoryguard-v2-phase1"),
            ("memory_schema_meta", "memory", "memoryguard-v2-phase2-memory"),
        ),
        "tables": {
            "atoms": {"atom_id", "memory_id", "body", "kind", "status", "confidence", "locked", "injection_policy", "priority", "canonical_hash", "dedup_domain", "supersedes_json", "provenance_json", "metadata_json", "revision", "visibility", "created_at", "updated_at", "workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role"},
            "atom_revisions": {"revision_id", "atom_id", "revision", "body", "status", "canonical_hash", "revision_digest", "metadata_json", "created_at"},
            "atom_deltas": {"delta_id", "atom_id", "from_revision", "to_revision", "delta_json", "created_at"},
            "supersession_edges": {"edge_id", "old_atom_id", "new_atom_id", "reason", "source_ref", "created_at"},
            "scope_acl": {"acl_id", "atom_id", "workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role", "effect", "metadata_json", "created_at"},
            "source_mappings": {"mapping_id", "atom_id", "source_domain", "source_ref", "source_record_id", "source_revision", "digest", "provenance_json", "created_at"},
            "domain_outbox": {"event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"},
            "outbox_checkpoints": {"domain", "last_sequence", "updated_at"},
            "evidence_projection_receipts": {"event_id", "evidence_id", "projected_at", "error_json"},
            "domain_state": {"domain", "state", "generation", "updated_at", "metadata_json"},
        },
    },
    "evidence": {
        "meta": (
            ("schema_meta", "evidence", "memoryguard-v2-phase1"),
            ("evidence_schema_meta", "evidence", "memoryguard-v2-phase2-evidence"),
        ),
        "tables": {
            "evidence": {"evidence_id", "evidence_type", "source_ref", "revision", "digest", "authority", "status", "metadata_json", "observed_at", "created_at"},
            "evidence_links": {"link_id", "evidence_id", "subject_type", "subject_id", "relation", "metadata_json", "created_at"},
            "migration_map": {"map_id", "source_domain", "source_ref", "source_id", "target_type", "target_id", "metadata_json", "created_at"},
            "domain_outbox": {"event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"},
            "outbox_checkpoints": {"domain", "last_sequence", "updated_at"},
            "audit_refs": {"audit_id", "source_ref", "digest", "metadata_json", "created_at"},
        },
    },
}
_GOVERNANCE_LEDGER_TABLE = "decisions"
_GOVERNANCE_LEDGER_COLUMNS = frozenset({
    "decision_id", "operation", "target_json", "reason", "confidence", "undo_hash",
    "context_json", "before_json", "after_json", "status", "created_at",
    "idempotency_key", "request_fingerprint",
})


class NativePortError(RuntimeError):
    """Stable, non-leaking native-port failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "native_port_error")
        self.detail = str(detail or "")
        super().__init__(self.code)


class NativeContextError(NativePortError):
    """The trusted identity/context boundary rejected a request."""


class _Weakrefable:
    """Compatibility base adding ``__weakref__`` on Python 3.10."""

    __slots__ = ("__weakref__",)


@dataclass(frozen=True, slots=True)
class NativeBoundContext(_Weakrefable):
    """Immutable, process-issued authority used by native transports.

    Public fields are a compatibility projection only.  Native authorization
    reads the exact object registered by :func:`bind_native_transport_context`;
    mutating/copying a surrounding mapping never changes these values.  The
    private issuer token and identity registry also make pickle/hand-crafted
    instances fail closed.
    """

    workspace_id: str
    share_group_id: str
    agent_instance_id: str
    project_ref: str
    provider: str
    runtime_role: str
    admin: bool
    is_admin: bool
    session_id: str
    session_source: str
    session_trusted: bool
    runtime_agent_id: str = ""
    parent_agent_id: str = ""
    context_hash: str = ""
    entrypoint: str = ""
    # Keep the private issuer in its historical positional slot.  New scope
    # fields follow it so old in-process constructor call sites remain
    # compatible while still requiring the issuer sentinel.
    _issuer: object = field(default=None, repr=False, compare=False)
    # Knowledge reads have a narrower ACL tuple than the general runtime
    # identity.  Keep it on the process-issued authority so a request cannot
    # broaden namespace/sensitivity/policy selectors by editing its payload.
    # These remain optional for non-Knowledge surfaces for compatibility;
    # Knowledge handlers require all three explicitly.
    namespace_id: str = ""
    sensitivity: str = ""
    policy_class: str = ""

    def __post_init__(self) -> None:
        if self._issuer is not _NATIVE_CONTEXT_CAPABILITY:
            raise NativeContextError("trusted_context_capability_required")
        object.__setattr__(self, "workspace_id", _text(self.workspace_id))
        object.__setattr__(self, "share_group_id", _text(self.share_group_id))
        object.__setattr__(self, "agent_instance_id", _text(self.agent_instance_id))
        object.__setattr__(self, "project_ref", _text(self.project_ref))
        object.__setattr__(self, "provider", _text(self.provider))
        object.__setattr__(self, "runtime_role", _text(self.runtime_role))
        object.__setattr__(self, "session_id", _text(self.session_id))
        object.__setattr__(self, "session_source", _text(self.session_source).casefold())
        object.__setattr__(self, "runtime_agent_id", _text(self.runtime_agent_id))
        object.__setattr__(self, "parent_agent_id", _text(self.parent_agent_id))
        object.__setattr__(self, "context_hash", _text(self.context_hash))
        object.__setattr__(self, "entrypoint", _text(self.entrypoint))
        object.__setattr__(self, "namespace_id", _text(self.namespace_id))
        object.__setattr__(self, "sensitivity", _text(self.sensitivity))
        object.__setattr__(self, "policy_class", _text(self.policy_class))
        _NATIVE_BOUND_CONTEXTS[id(self)] = self

    def __copy__(self) -> "NativeBoundContext":  # pragma: no cover - attack guard
        raise TypeError("native bound context is not copyable")

    def __deepcopy__(self, memo: dict[int, object]) -> "NativeBoundContext":  # pragma: no cover - attack guard
        raise TypeError("native bound context is not copyable")

    def __reduce_ex__(self, protocol: int) -> Any:  # pragma: no cover - attack guard
        raise TypeError("native bound context is not serializable")

    def to_dict(self) -> dict[str, Any]:
        """Return a public projection; never include issuer/authority token."""
        return {
            "workspace_id": self.workspace_id,
            "share_group_id": self.share_group_id,
            "agent_instance_id": self.agent_instance_id,
            "trusted_agent_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "admin": bool(self.admin),
            "is_admin": bool(self.is_admin),
            "session_id": self.session_id,
            "session_source": self.session_source,
            "session_trusted": bool(self.session_trusted),
            "runtime_agent_id": self.runtime_agent_id,
            "parent_agent_id": self.parent_agent_id,
            "context_hash": self.context_hash,
            "entrypoint": self.entrypoint,
            "namespace_id": self.namespace_id,
            "sensitivity": self.sensitivity,
            "policy_class": self.policy_class,
        }


class NativeContextEnvelope(dict[str, Any]):
    """Mutable compatibility mapping carrying an immutable authority object."""

    __slots__ = ("_bound_context",)

    def __init__(self, bound_context: NativeBoundContext) -> None:
        if not _is_valid_bound_context(bound_context):
            raise NativeContextError("trusted_context_capability_required")
        super().__init__(bound_context.to_dict())
        # Mapping copies used by MCP/GUI retain this exact object.  The key is
        # stripped by public serializers and is never an authorization input.
        self["__native_bound_context"] = bound_context
        self["__native_transport_capability"] = _NATIVE_CONTEXT_CAPABILITY
        object.__setattr__(self, "_bound_context", bound_context)

    @property
    def bound_context(self) -> NativeBoundContext:
        return self._bound_context

    def __copy__(self) -> "NativeContextEnvelope":
        # A shallow envelope copy still carries the original authority; it is
        # not a forged capability.  Deep copy/pickle fail via bound context.
        return NativeContextEnvelope(self._bound_context)

    def __deepcopy__(self, memo: dict[int, object]) -> "NativeContextEnvelope":  # pragma: no cover - attack guard
        raise TypeError("native bound context is not copyable")


def _is_valid_bound_context(value: Any) -> bool:
    return (
        # Exact type is intentional: subclasses can override properties or
        # lifecycle hooks and therefore are not process-issued authorities.
        type(value) is NativeBoundContext
        and _NATIVE_BOUND_CONTEXTS.get(id(value)) is value
        and value._issuer is _NATIVE_CONTEXT_CAPABILITY
    )


def resolve_native_transport_context(context: Any) -> NativeBoundContext:
    """Resolve the canonical process-issued authority for a native context.

    The compatibility envelope may be copied/dictified, but every accepted
    mapping must retain the exact bound object *and* the private process token.
    Public identity fields are never consulted.  Sentinel-only mappings,
    forged lookalikes, stale/GC'd objects, and envelopes whose private object
    was tampered with all fail closed.
    """

    if type(context) is NativeContextEnvelope:
        authority = context.bound_context
        if context.get("__native_bound_context") is not authority:
            raise NativeContextError("trusted_context_capability_required")
    elif type(context) is dict:
        authority = context.get("__native_bound_context")
    elif type(context) is NativeBoundContext:
        # Internal callers may pass the authority directly; it is still
        # checked against the process registry and issuer below.
        authority = context
    else:
        raise NativeContextError("trusted_context_capability_required")
    if context is not authority and type(context) is not NativeBoundContext:
        if type(context) not in {dict, NativeContextEnvelope} or context.get("__native_transport_capability") is not _NATIVE_CONTEXT_CAPABILITY:
            raise NativeContextError("trusted_context_capability_required")
    if not _is_valid_bound_context(authority):
        raise NativeContextError("trusted_context_capability_required")
    return authority


@dataclass
class _SchemaLease:
    """Read-only schema/identity lease held across writable Store init."""

    domain: str
    path: Path
    identity: tuple[int, int, int, int]
    connection: sqlite3.Connection

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass

    def __enter__(self) -> "_SchemaLease":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False


@dataclass(frozen=True)
class SurfaceSpec:
    """One explicit public operation entry."""

    name: str
    status: str  # implemented | neutral-read | retired | blocker
    handler: str
    mutation: bool = False
    reason: str = ""
    canonical_name: str = ""
    domain: str = ""
    execution: str = "sync"

    def __post_init__(self) -> None:
        if self.status not in {"implemented", "neutral-read", "retired", "blocker"}:
            raise ValueError("invalid native surface status")
        if self.status == "retired" and not self.reason:
            raise ValueError("retired native surface requires an explicit reason")
        if not self.handler:
            raise ValueError("native surface handler is required")
        if self.execution not in {"sync", "task"}:
            raise ValueError("native surface execution must be sync or task")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "handler": self.handler,
            "mutation": bool(self.mutation),
            "reason": self.reason,
            "canonical_name": self.canonical_name or self.name,
            "domain": self.domain,
            "execution": self.execution,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, NativeBoundContext):
        # Authority objects are process-local and must never enter RPC output.
        return "<native-bound-context>"
    if isinstance(value, Mapping):
        return {
            str(k): _plain(v)
            for k, v in value.items()
            if not str(k).startswith("__native_")
        }
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain(to_dict())
        except Exception:
            return str(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        try:
            return _plain(as_dict())
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items() if str(k) != "func"}
    if isinstance(value, (list, tuple)):
        # GUI/CLI compatibility callers pass one business object positionally.
        # Normalize that shape before semantic handlers inspect fields; retain
        # ``args`` for genuine multi-positional calls.
        if len(value) == 1 and isinstance(value[0], Mapping):
            return {str(k): v for k, v in value[0].items() if str(k) != "func"}
        return {"args": list(value)}
    try:
        return {str(k): v for k, v in vars(value).items() if str(k) != "func"}
    except TypeError as exc:
        raise NativePortError("invalid_native_arguments") from exc


def _phase9_gui_payload(surface: str, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bind historical positional GUI args from the canonical operation spec.

    Browser-selected identity/scope values are transport compatibility only and
    are discarded after binding.  Authorization is supplied exclusively by the
    process-issued ``NativeBoundContext``.
    """

    if surface != "gui":
        return dict(payload)
    operation = get_gui_operation_spec(name)
    if operation is None:
        return dict(payload)

    def strip_business_selectors(values: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(values)
        if name in _GUI_BUSINESS_SELECTOR_OPERATIONS:
            for key in _GUI_BUSINESS_SELECTOR_KEYS:
                result.pop(key, None)
        return result

    values = payload.get("args")
    if not isinstance(values, (list, tuple)):
        # Historical SafeBridge flattens a single mapping positional argument.
        # ``scope_set`` is the one explicit business-scope mutation.  All
        # projection/release selectors are compatibility input and are
        # discarded below before the trusted scope is derived.
        if operation.canonical_name == "scope_set" and operation.parameters == ("requested_scope",):
            return {"requested_scope": dict(payload)}
        result = strip_business_selectors(payload)
        # ``group_id`` is a historical share-group identity alias globally,
        # but the stale-conflict command also accepts it as the conflict
        # group's business key.  Normalize that one operation before the
        # identity gate so a conflict id cannot be mistaken for a scope claim.
        if name == "close_stale_conflict" and result.get("group_id"):
            if not _text(result.get("conflict_group_id")):
                result["conflict_group_id"] = result["group_id"]
            result.pop("group_id", None)
        return result
    args = list(values)
    result = {
        key: args[index]
        for index, key in enumerate(operation.parameters)
        if index < len(args)
    }
    # Preserve identity-shaped compatibility arguments long enough for
    # ``_context`` to reject a browser/transport spoof.  The checked dispatch
    # removes them only after that comparison; they never reach a handler as
    # an authorization source.  Explicit business target selectors use names
    # such as ``target_group_id`` and remain ordinary payload fields.
    for key in ("scope", "identity"):
        result.pop(key, None)
    # Import bundles are scoped exclusively by the process-issued GUI
    # authority.  These three historical positional selectors are retained
    # in the public signature for compatibility, but must be ignored rather
    # than treated as a second identity assertion (older GUI callers pass
    # stale/browser values here).
    if name == "create_import":
        for key in ("agent_instance_id", "project_ref", "share_group_id"):
            result.pop(key, None)
    # Historical read() uses an empty placeholder for whichever selector is
    # not active.  NativeHistoryService distinguishes empty from omitted.
    if name == "history_read":
        for key in ("session_id", "turn_id"):
            if result.get(key) == "":
                result[key] = None
    if name == "close_stale_conflict" and result.get("group_id"):
        # See the mapping branch above: this is a business conflict id, not
        # the caller's share-group identity alias.
        if not _text(result.get("conflict_group_id")):
            result["conflict_group_id"] = result["group_id"]
        result.pop("group_id", None)
    return strip_business_selectors(result)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bind_native_transport_context(
    access_context: Any,
    *,
    workspace_id: str = "",
    share_group_id: str = "",
    project_ref: str = "",
    provider: str = "",
    runtime_role: str = "",
    runtime_agent_id: str = "",
    parent_agent_id: str = "",
    context_hash: str = "",
    entrypoint: str = "",
    namespace_id: str = "",
    sensitivity: str = "",
    policy_class: str = "",
) -> NativeContextEnvelope:
    """Issue a process-local capability envelope for a trusted transport.

    Native mutation handlers never accept a plain identity mapping as proof of
    provenance.  Hosts call this helper with their process-created
    ``AccessContext`` object; the private sentinel cannot be reproduced by
    JSON/RPC payloads.  Knowledge callers additionally provide the exact
    ``namespace_id``, ``sensitivity`` and ``policy_class`` tuple here.  The
    returned mapping is still safe to pass through existing facade signatures.
    """
    try:
        from ..access_context import AccessContext
    except Exception as exc:  # pragma: no cover - package import failure
        raise NativeContextError("trusted_context_capability_unavailable") from exc
    # Do not accept lookalike/subclass contexts whose properties could be
    # overridden after the host's trust decision.
    if type(access_context) is not AccessContext:
        raise NativeContextError("trusted_context_capability_required")
    authority = NativeBoundContext(
        workspace_id=_text(workspace_id),
        share_group_id=_text(share_group_id),
        agent_instance_id=_text(access_context.trusted_agent_id),
        project_ref=_text(project_ref),
        provider=_text(provider),
        runtime_role=_text(runtime_role),
        admin=bool(access_context.is_admin),
        is_admin=bool(access_context.is_admin),
        session_id=_text(access_context.session_id),
        session_source=_text(access_context.session_source),
        session_trusted=bool(access_context.session_trusted),
        runtime_agent_id=_text(runtime_agent_id),
        parent_agent_id=_text(parent_agent_id),
        context_hash=_text(context_hash),
        entrypoint=_text(entrypoint),
        namespace_id=_text(namespace_id),
        sensitivity=_text(sensitivity),
        policy_class=_text(policy_class),
        _issuer=_NATIVE_CONTEXT_CAPABILITY,
    )
    return NativeContextEnvelope(authority)


@dataclass(frozen=True)
class _NativeInjectionCapability:
    """Process-local opt-in envelope for tests/host adapters.

    This object is intentionally not serializable as a capability: callers
    must receive it from one of the helpers below in this process.  The public
    native factory never creates one and therefore never enables injection.
    """

    token: object
    services: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    stores: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.token is not _NATIVE_INJECTION_CAPABILITY:
            raise NativePortError("native_injection_capability_required")


def bind_native_test_capability(
    *,
    services: Mapping[str, Callable[..., Any]] | None = None,
    stores: Mapping[str, Any] | None = None,
    **named_stores: Any,
) -> _NativeInjectionCapability:
    """Issue an explicit process-local injection capability for tests.

    Production factories do not call this helper.  ``named_stores`` accepts
    domain names such as ``memory_store``/``evidence_store`` to keep existing
    constructor call sites readable while retaining an unforgeable envelope.
    """

    normalized: dict[str, Any] = {str(key): value for key, value in dict(stores or {}).items()}
    for key, value in named_stores.items():
        if key.endswith("_store"):
            normalized[key[:-6]] = value
    return _NativeInjectionCapability(
        _NATIVE_INJECTION_CAPABILITY,
        services=dict(services or {}),
        stores=normalized,
    )


def bind_native_test_services(services: Mapping[str, Callable[..., Any]]) -> _NativeInjectionCapability:
    """Convenience wrapper for explicit service injection in tests."""

    return bind_native_test_capability(services=services)


def bind_native_test_store(domain: str, store: Any) -> _NativeInjectionCapability:
    """Convenience wrapper for one explicit, schema-validated test store."""

    return bind_native_test_capability(stores={str(domain): store})


_RISK_SEVERITY_LABELS = {
    "critical": "极高风险",
    "high": "高风险",
    "medium": "中风险",
    "low": "低风险",
    "info": "提示",
}
_RISK_RULE_LABELS = {
    "forbidden_path": "禁止访问路径",
    "sensitive_path": "敏感路径",
    "unsafe_path": "不安全路径",
    "forbidden_write": "禁止写入",
    "unsafe_write": "不安全写入",
    "scope_violation": "范围越界",
    "cross_scope_write": "跨范围写入",
    "missing_evidence": "缺少证据",
    "stale_reference": "引用已过期",
    "reference_audit_blocker": "引用审计阻断",
    "duplicate_memory": "重复记忆",
    "conflict": "规则冲突",
    "quarantine": "隔离风险",
    "unverified_change": "变更未验证",
    "missing_test": "缺少测试",
    "schema_unreadable": "架构不可读",
    "partial_schema": "架构不完整",
    "unknown_authoritative_table": "未知权威表",
    "unknown_authoritative_column": "未知权威列",
    "integrity_check": "完整性检查失败",
    "foreign_key_check": "外键检查失败",
    "missing_database": "缺少数据库",
    "storage_unreadable": "存储不可读",
    "manifest_not_ready": "清单尚未就绪",
    "manifest_unreadable": "清单不可读",
}
_RISK_DIMENSION_LABELS = {
    "integrity": "数据完整性",
    "consistency": "一致性",
    "effectiveness": "有效性",
    "security": "安全性",
    "freshness": "内容新鲜度",
    "maintainability": "可维护性",
    "recoverability": "可恢复性",
    "scope": "适用范围",
    "provenance": "来源可信度",
    "correctness": "正确性",
    "reliability": "可靠性",
    "audit": "审计",
    "reference audit": "引用审计",
}
_RISK_SURFACE_LABELS = {
    "runtime": "运行时",
    "storage": "存储",
    "filesystem": "文件系统",
    "source": "数据源",
    "api": "接口",
    "gui": "管理界面",
    "bridge": "本地桥接",
    "governance": "治理层",
    "memory": "记忆",
    "evidence": "证据",
}


def _risk_label(value: Any, labels: Mapping[str, str], unknown: str) -> str:
    key = _text(value).casefold()
    return labels.get(key, unknown)


def _readable_risk_finding(value: Mapping[str, Any], *, default_rule_id: str = "reference_audit_blocker") -> dict[str, Any]:
    """Add GUI-safe risk labels while retaining every raw finding field."""

    row = dict(value)
    rule_id = _text(row.get("rule_id") or row.get("code") or default_rule_id)
    severity = _text(row.get("severity") or "high").casefold()
    dimension = _text(row.get("dimension") or "integrity").casefold()
    surface = _text(row.get("surface") or "storage").casefold()
    rule_label = _risk_label(rule_id, _RISK_RULE_LABELS, "未知风险")
    severity_label = _risk_label(severity, _RISK_SEVERITY_LABELS, "未知严重度")
    dimension_label = _risk_label(dimension, _RISK_DIMENSION_LABELS, "未知维度")
    surface_label = _risk_label(surface, _RISK_SURFACE_LABELS, "未知来源")
    row.setdefault("rule_id", rule_id)
    row.setdefault("severity", severity)
    row.setdefault("dimension", dimension)
    row.setdefault("surface", surface)
    row.setdefault("title", rule_label)
    row.setdefault("title_zh", rule_label)
    row.setdefault("type_label", rule_label)
    row.setdefault("severity_label", severity_label)
    row.setdefault("dimension_label", dimension_label)
    row.setdefault("surface_label", surface_label)
    summary = _text(row.get("summary") or row.get("message"))
    if not summary:
        domain = _text(row.get("domain"))
        table = _text(row.get("table"))
        target = " / ".join(item for item in (domain, table) if item)
        summary = f"{rule_label}：{target}" if target else rule_label
    row.setdefault("summary", summary)
    row.setdefault("evidence_summary", summary)
    return row


class NativeV2RuntimePort:
    """Single native V2 dispatch port for all four public surfaces.

    ``services`` may provide operation-specific callables keyed by either
    ``"surface:name"`` or just ``name``.  The callable receives the sanitized
    payload as its first argument and may accept keyword-only ``context``,
    ``generation`` and ``mutation``.  Supplying a callable is useful for a
    host-specific V2 service while retaining one auditable registry.
    """

    supports_rule_mutation_context = True
    # The cutover facade may bind its trusted manifest provider when it owns
    # this port.  Direct callers must inject one explicitly; a caller-supplied
    # ``state=V2_ACTIVE`` value is never sufficient for a mutation.
    requires_state_provider = True

    # Operations with a concrete V2 implementation in this repository.  GUI
    # names below are aliases to these semantic handlers; all other names are
    # explicit blockers rather than accidental success paths.
    _MCP_HANDLERS: Mapping[str, tuple[str, str, bool]] = {
        # Phase 9 native evidence/source/diagnostics services. These are
        # production builtins (never generic ``services=`` overrides). Audit
        # uses ReferenceAudit(mode="ro") and is therefore a read operation.
        "memoryguard_audit": ("reference_audit", "implemented", False),
        "memoryguard_explain": ("explain", "implemented", False),
        "memoryguard_list_sources": ("list_sources", "implemented", True),
        "memoryguard_scan_summary": ("scan_summary", "implemented", True),
        "memoryguard_import_preview": ("import_preview", "implemented", False),
        "memoryguard_runtime_processes": ("runtime_processes", "implemented", False),
        "memoryguard_history_search": ("history_search", "implemented", False),
        "memoryguard_history_timeline": ("history_timeline", "implemented", False),
        "memoryguard_history_read": ("history_read", "implemented", False),
        "memoryguard_history_extract_preview": ("history_extract_preview", "implemented", False),
        "memoryguard_history_list_sessions": ("history_list_sessions", "implemented", False),
        "memoryguard_history_export": ("history_export", "implemented", False),
        "memoryguard_history_delete": ("history_delete", "implemented", True),
        "memoryguard_memory_read": ("memory_read", "implemented", False),
        "memoryguard_memory_search": ("memory_search", "implemented", False),
        "memoryguard_memory_status": ("memory_status", "implemented", False),
        "memoryguard_memory_write": ("memory_write", "implemented", True),
        "memoryguard_memory_update": ("memory_update", "implemented", True),
        "memoryguard_memory_delete": ("memory_delete", "implemented", True),
        "memoryguard_memory_merge_safe": ("memory_merge_safe", "implemented", True),
        "memoryguard_memory_merge_safe_preview": ("memory_merge_safe_preview", "implemented", False),
        "memoryguard_context_bootstrap": ("context_bootstrap", "implemented", False),
        "memoryguard_rule_create_auto": ("rule_create_auto", "implemented", True),
        "memoryguard_rule_feedback": ("rule_feedback", "implemented", True),
        "memoryguard_rule_undo": ("rule_undo", "implemented", True),
        "memoryguard_rule_decision_read": ("rule_decision_read", "implemented", False),
        "memoryguard_rule_scope_stats": ("rule_scope_stats", "implemented", False),
        "memoryguard_binding_list": ("binding_list", "implemented", False),
        "memoryguard_binding_create": ("binding_create", "implemented", True),
        "memoryguard_rule_merge_capability_issue": ("rule_merge_capability_issue", "implemented", True),
        "memoryguard_rule_merge_approve": ("rule_merge_approve", "implemented", True),
        "memoryguard_rule_merge_acknowledge": ("rule_merge_acknowledge", "implemented", True),
        "memoryguard_rule_merge_cooldown_clear": ("rule_merge_cooldown_clear", "implemented", True),
        "memoryguard_rule_merge_safe": ("rule_merge_safe", "implemented", True),
        "memoryguard_rule_merge_safe_preview": ("rule_merge_safe_preview", "implemented", False),
        "memoryguard_extract_memories": ("extract_memories", "implemented", True),
        "memoryguard_accept_candidates": ("accept_candidates", "implemented", True),
        "memoryguard_list_pending_enrichments": ("list_pending_enrichments", "implemented", False),
        "memoryguard_apply_enrichments": ("apply_enrichments", "implemented", True),
        "memoryguard_enrichment_status": ("enrichment_status", "implemented", False),
        "memoryguard_build_and_enrich": ("build_and_enrich", "implemented", True),
        "memoryguard_resolve_group": ("resolve_group", "implemented", False),
        "memoryguard_external_mcp_list": ("external_mcp_list", "implemented", False),
        "memoryguard_external_mcp_import": ("external_mcp_import", "implemented", True),
        "memoryguard_projection_status": ("projection_status", "implemented", False),
        "memoryguard_canonical_status": ("canonical_status", "implemented", False),
        "memoryguard_diagnostics_snapshot": ("diagnostics_snapshot", "implemented", False),
        "memoryguard_knowledge_list": ("knowledge_read", "implemented", False),
        "memoryguard_knowledge_search": ("knowledge_read", "implemented", False),
        "memoryguard_knowledge_read": ("knowledge_read", "implemented", False),
        "memoryguard_knowledge_book": ("knowledge_book", "implemented", False),
        "memoryguard_knowledge_candidates": ("knowledge_candidates", "implemented", False),
        "memoryguard_codegraph_graph": ("codegraph_graph", "implemented", False),
        "memoryguard_neuron_graph": ("projection_graph", "implemented", False),
        "memoryguard_codegraph_query": ("codegraph_query", "implemented", False),
        "memoryguard_codegraph_path": ("codegraph_path", "implemented", False),
        "memoryguard_codegraph_explain": ("codegraph_explain", "implemented", False),
        "memoryguard_codegraph_affected": ("codegraph_affected", "implemented", False),
        "memoryguard_codegraph_update": ("codegraph_update", "implemented", True),
        "memoryguard_capabilities": ("mcp_capabilities", "implemented", False),
        "memoryguard_invoke": ("mcp_invoke", "implemented", False),
        "memoryguard_knowledge_add": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_reingest": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_rebuild_smart": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_remove": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_restore": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_purge_deleted": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_update_settings": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_knowledge_candidate_review": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_task_status": ("mcp_gui_bridge", "implemented", False),
        "memoryguard_task_list": ("mcp_gui_bridge", "implemented", False),
        "memoryguard_task_cancel": ("mcp_gui_bridge", "implemented", True),
        "memoryguard_codegraph_build_bound": ("codegraph_build_bound", "implemented", True),
        "memoryguard_semantic_check": ("semantic_check", "implemented", False),
        "memoryguard_provider_install": ("provider_install", "implemented", True),
        "memoryguard_codegraph_status": ("codegraph_status", "implemented", False),
        "memoryguard_asset_status": ("asset_status", "neutral-read", False),
        "memoryguard_skill_status": ("skill_status", "neutral-read", False),
    }
    _GUI_HANDLERS: Mapping[str, tuple[str, str, bool]] = {
        # Phase 9 GUI aliases to the same production native services used by
        # MCP.  ``delete_history`` is intentionally mutation-gated by the
        # canonical GUI ledger (V2_ACTIVE/CAS + durable receipt/idempotency).
        "list_history_sessions": ("history_list_sessions", "implemented", False),
        "search_history": ("history_search", "implemented", False),
        "history_timeline": ("history_timeline", "implemented", False),
        "history_read": ("history_read", "implemented", False),
        "history_extract_preview": ("history_extract_preview", "implemented", False),
        "export_history": ("history_export", "implemented", False),
        "delete_history": ("history_delete", "implemented", "delete_history" in GUI_MUTATION_NAMES),
        "list_sources": ("list_sources", "implemented", False),
        "preview_source": ("import_preview", "implemented", False),
        "scan_sources": ("scan_summary", "implemented", False),
        "preview_import": ("import_preview", "implemented", False),
        "list_external_mcp_servers": ("external_mcp_list", "implemented", False),
        "preview_external_mcp_import": ("external_mcp_preview", "implemented", False),
        "detect_external_mcp": ("external_mcp_detect", "implemented", False),
        "list_memory": ("memory_list", "implemented", False),
        "get_memory": ("memory_read", "implemented", False),
        "search_memory": ("memory_search", "implemented", False),
        "get_memory_status": ("memory_status", "implemented", False),
        "list_memory_versions": ("memory_versions", "implemented", False),
        "get_supersede_chain": ("memory_supersede_chain", "implemented", False),
        # Global status is a separate trusted-admin aggregate, not a scoped
        # status alias.  Non-admin callers are rejected before the aggregate
        # store is opened so group existence cannot be probed.
        "get_global_memory_status": ("memory_global_status", "implemented", False),
        "list_bindings": ("binding_list", "implemented", False),
        "get_governance_snapshot": ("governance_snapshot", "implemented", False),
        # The GUI registry endpoint is a read-only native coverage view.  It
        # exposes no legacy security store, paths, or contents.
        "get_api_method_registry": ("coverage", "implemented", False),
        "get_governance_scope": ("scope_echo", "implemented", False),
        "get_governance_scope_state": ("scope_echo", "implemented", False),
        "get_host_hook_status": ("hook_status", "implemented", False),
        "get_sandbox_status": ("sandbox_status", "implemented", False),
        "get_host_enrichment_guide": ("host_enrichment_guide", "implemented", False),
        "list_host_llm_agents": ("host_llm_agents", "implemented", False),
        "get_neuron_graph": ("projection_graph", "implemented", False),
        "get_memory_neuron_graph": ("projection_graph", "implemented", False),
        "get_codegraph_graph": ("codegraph_graph", "implemented", False),
        "list_codegraph_projects": ("codegraph_projects", "implemented", False),
        "build_codegraph": ("codegraph_build", "implemented", True),
        "set_codegraph_automation": ("codegraph_automation", "implemented", True),
        "list_pending_enrichments": ("list_pending_enrichments", "implemented", False),
        "get_enrichment_status": ("enrichment_status", "implemented", False),
        "get_audit": ("reference_audit", "implemented", False),
        "run_audit": ("reference_audit", "implemented", False),
        "get_storage_overview": ("diagnostics_snapshot", "implemented", False),
        "list_history": ("history_list_sessions", "implemented", False),
        "accept_candidates": ("accept_candidates", "implemented", True),
        "apply_enrichments": ("apply_enrichments", "implemented", True),
        "extract_preview": ("gui_extract_preview", "implemented", True),
        "extract_preview_by_path": ("gui_extract_by_path", "implemented", True),
        "create_rule_from_text": ("gui_rule_create", "implemented", True),
        "submit_rule_feedback": ("gui_rule_feedback", "implemented", True),
        "undo_rule_decision": ("gui_rule_undo", "implemented", True),
        "import_external_mcp_entries": ("external_mcp_import", "implemented", True),
        "knowledge_book": ("knowledge_book", "implemented", False),
        "knowledge_candidates_list": ("knowledge_candidates", "implemented", False),
        "call_readonly": ("bridge_transport", "implemented", False),
        "request_mutation": ("bridge_transport", "implemented", True),
        "pick_path": ("gui_pick_path", "implemented", False),
        "add_source": ("gui_source_add", "implemented", True),
        "remove_source": ("gui_source_remove", "implemented", True),
        "get_memory_source_map": ("gui_memory_source_map", "implemented", False),
        "list_share_groups": ("gui_groups", "implemented", False),
        # SafeBridge supplies a process-issued native capability, so these
        # writes can use the same GovernanceV2 mutation ledger as MCP.
        "edit_memory": ("gui_memory_edit", "implemented", True),
        "lock_memory": ("gui_memory_lock", "implemented", True),
        "unlock_memory": ("gui_memory_unlock", "implemented", True),
        "set_memory_injection_policy": ("gui_memory_policy", "implemented", True),
        "restore_memory": ("gui_memory_restore", "implemented", True),
        "delete_memory": ("gui_memory_delete", "implemented", True),
        "list_rules_habits": ("gui_rule_snapshot", "implemented", False),
        "list_rule_decisions": ("gui_rule_decisions", "implemented", False),
        "read_rule_decision": ("rule_decision_read", "implemented", False),
        "get_rule_auto_scope_metrics": ("rule_scope_stats", "implemented", False),
        "list_rule_cockpit": ("gui_rule_snapshot", "implemented", False),
        "get_rule_scope_options": ("gui_rule_scope_options", "implemented", False),
        "preview_effective_rules": ("gui_rule_effective", "implemented", False),
        "list_rule_match_receipts": ("gui_rule_receipts", "implemented", False),
        "list_rule_exceptions": ("gui_rule_exceptions", "implemented", False),
        "update_rule_audience": ("gui_rule_audience_update", "implemented", True),
        "update_rule_body": ("gui_rule_body_update", "implemented", True),
        "knowledge_list": ("knowledge_read", "implemented", False),
        "knowledge_search": ("knowledge_read", "implemented", False),
        "knowledge_read": ("knowledge_read", "implemented", False),
    }

    _CLI_HANDLERS: Mapping[str, tuple[str, str, bool]] = {
        "audit": ("cli_audit", "implemented", False),
        "explain": ("cli_explain", "implemented", False),
        "scan": ("cli_scan", "implemented", False),
        "source": ("cli_source", "implemented", False),
        "provider": ("cli_provider", "implemented", True),
        "hooks": ("cli_hooks", "implemented", False),
        "groups": ("cli_groups", "implemented", False),
        "mcp-status": ("cli_mcp_status", "implemented", False),
        "doctor": ("cli_doctor", "implemented", False),
        "runtime": ("cli_runtime", "implemented", False),
        # Host UI/executor actions run only after this native manifest gate.
        "gui": ("cli_gui", "implemented", False),
        "open": ("cli_open", "implemented", False),
        "desktop": ("cli_desktop", "implemented", True),
        # MaintenanceRuntimePort owns authoritative V2 storage mutation.
        "storage": ("maintenance", "implemented", False),
        # V1 report-patch/import/GC workflows have explicit V2 replacements.
        "plan": ("retired", "retired", False),
        "apply": ("retired", "retired", True),
        "verify": ("retired", "retired", False),
        "undo": ("retired", "retired", True),
        "import": ("retired", "retired", False),
        "gc": ("retired", "retired", False),
    }
    _CLI_RETIRED_REASONS: Mapping[str, str] = {
        "plan": "legacy report patch planning is replaced by V2 ReferenceAudit and readiness remediation",
        "apply": "legacy source-file patch apply is replaced by explicit V2 domain governance/maintenance operations",
        "verify": "legacy report-file verification is replaced by V2 validator, ReferenceAudit and readiness evidence",
        "undo": "legacy backup-file undo is replaced by V2 domain receipts, compensating mutations and maintenance rollback",
        "import": "legacy offline import staging is replaced by V2 Content/History/Knowledge ingestion surfaces",
        "gc": "legacy artifact GC is replaced by the V2 storage lease/sweep/compact maintenance command",
    }

    def __init__(
        self,
        workspace: str | Path,
        *,
        maintenance_port: Any = None,
        context_engine: Any = None,
        recall_planner: Any = None,
        memory_store: Any = None,
        evidence_store: Any = None,
        governance: Any = None,
        rule_store: Any = None,
        asset_store: Any = None,
        skill_store: Any = None,
        knowledge_adapter: Any = None,
        content_store: Any = None,
        codegraph_store: Any = None,
        projection_store: Any = None,
        rule_merge_store: Any = None,
        services: Mapping[str, Callable[..., Any]] | _NativeInjectionCapability | None = None,
        state_provider: Any = None,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.layout = WorkspaceV2Layout(self.workspace)
        self.maintenance_port = maintenance_port
        self.context_engine = context_engine
        self.recall_planner = recall_planner
        injection_services: Mapping[str, Callable[..., Any]] = {}
        injection_stores: Mapping[str, Any] = {}
        if isinstance(services, _NativeInjectionCapability):
            injection_services = services.services
            injection_stores = services.stores
        elif services is not None:
            # A plain Mapping is indistinguishable from a JSON/RPC payload and
            # therefore cannot carry a native service capability.  Even
            # neutral reads must opt into the explicit process-local seam.
            raise NativePortError("native_service_injection_capability_required")
        if governance is not None and not isinstance(governance, _NativeInjectionCapability):
            # Keep a stable governance-specific code for callers while still
            # requiring explicit capability wrapping for test seams.
            raise NativePortError("native_governance_injection_forbidden")
        store_arguments = {
            "memory": memory_store,
            "evidence": evidence_store,
            "governance": governance,
            "rules": rule_store,
            "assets": asset_store,
            "skills": skill_store,
            "knowledge": knowledge_adapter,
            "content": content_store,
            "codegraph": codegraph_store,
            "projection": projection_store,
            "rule_merge": rule_merge_store,
        }
        # No raw store/facade object may enter the native boundary.  Explicit
        # test seams must carry the process-local injection capability, even
        # for read-only domains; this keeps constructor call sites from
        # silently becoming mutation bypasses as handlers evolve.
        injected_stores = {
            domain: self._unwrap_store_injection(domain, value, injection_stores)
            for domain, value in store_arguments.items()
        }
        self._stores: dict[str, Any] = {
            **injected_stores,
        }
        # Named store wrappers can be supplied through the generic capability;
        # direct store kwargs are deliberately rejected above.
        for domain in ("memory", "evidence"):
            if self._stores.get(domain) is not None:
                self._stores[domain] = self._validate_injected_store(domain, self._stores[domain])
        self._services = self._validate_service_injection(injection_services, capability=isinstance(services, _NativeInjectionCapability))
        # These adapters are deliberately constructed by the production native
        # port itself rather than accepted through the generic service DI seam.
        # Their implementations are lazy/side-effect-free for reads and return
        # stable NO_SOURCE/neutral/blocked envelopes when backing state is
        # missing or unavailable.
        self._history_service: Any = None
        self._source_read_service: Any = None
        self._import_preview_service: Any = None
        self._runtime_diagnostics_service: Any = None
        self._external_mcp_service: Any = None
        self._rule_merge_service: Any = None
        self._rule_lifecycle_service: Any = None
        self._extraction_service: Any = None
        self._knowledge_service: Any = None
        self._knowledge_command_service: Any = None
        self._task_coordinator: Any = None
        self._projection_build_service: Any = None
        self._release_service: Any = None
        self._group_control_service: Any = None
        self._agent_native_service: Any = None
        self._governance_native_service: Any = None
        self._hook_control_service: Any = None
        self._import_control_service: Any = None
        self._history_control_service: Any = None
        self._audit_plan_service: Any = None
        self._native_service_init_errors: dict[str, str] = {}
        self._governance_boundary_instance: Any = None
        self._governance_boundary_lock = RLock()
        self.state_provider = state_provider
        if self.context_engine is None:
            # The production port owns the default V2 context engine.  It is
            # deliberately wired to this port's native V2 retrieval seam so
            # construction never falls back to a retired bootstrap module.
            from .context_engine import ContextEngine

            self.context_engine = ContextEngine(
                retriever=self,
                planner=self.recall_planner,
                ready=False,
                state="V2_BUILDING",
            )
        self._registry = self._make_registry()

    @staticmethod
    def _unwrap_store_injection(domain: str, value: Any, capability_stores: Mapping[str, Any]) -> Any:
        if value is None:
            return capability_stores.get(domain)
        if not isinstance(value, _NativeInjectionCapability):
            raise NativePortError("native_store_injection_capability_required")
        injected = value.stores.get(domain)
        if injected is None:
            raise NativePortError("native_store_injection_domain_required")
        return injected

    @classmethod
    def _validate_service_injection(
        cls,
        services: Mapping[str, Callable[..., Any]],
        *,
        capability: bool,
    ) -> dict[str, Callable[..., Any]]:
        result = dict(services)
        # No service override may replace a mutation boundary.  Test/host
        # injection is deliberately limited to non-mutating reads; maintenance
        # writes have their own private ``maintenance_port`` capability.
        mutating_names = set(MCP_MUTATION_NAMES)
        mutating_names.update(
            name for name, (_, _, mutation) in cls._MCP_HANDLERS.items() if mutation
        )
        mutating_names.update(
            handler for _, (handler, _, mutation) in cls._MCP_HANDLERS.items() if mutation
        )
        mutating_names.update(
            name for name, (_, _, mutation) in cls._GUI_HANDLERS.items() if mutation
        )
        mutating_names.update(
            handler for _, (handler, _, mutation) in cls._GUI_HANDLERS.items() if mutation
        )
        mutating_names.update({"maintenance", "binding_create", "rule_create", "rule_update", "rule_delete"})
        for key in result:
            operation = str(key).split(":", 1)[-1]
            if operation in mutating_names:
                raise NativePortError("native_mutation_service_override_forbidden")
        for key, fn in result.items():
            if not callable(fn):
                raise NativePortError("native_service_callable_required")
        return result

    def _validate_injected_store(self, domain: str, store: Any) -> Any:
        """Validate an explicitly injected read-only store against this DB."""

        if store is None:
            return None
        if getattr(store, "readonly", None) is not True and getattr(store, "read_only", None) is not True:
            raise NativePortError("readonly_store_capability_required")
        expected = self.layout.db_paths(domain)[0]
        actual = getattr(store, "db_path", None) or getattr(store, "path", None)
        if actual is None or Path(actual).expanduser().resolve() != expected.resolve():
            raise NativePortError("injected_store_identity_mismatch")
        # Re-run the same read-only validator used by lazy builtins.  This is
        # intentionally done before the store becomes reachable by a handler.
        self._preflight_domain(domain)
        return store

    # ---- registry / evidence -------------------------------------------------
    def _make_registry(self) -> dict[str, dict[str, SurfaceSpec]]:
        registry: dict[str, dict[str, SurfaceSpec]] = {"mcp": {}, "gui": {}, "cli": {}, "hook": {}}
        for name in sorted(MCP_TOOL_NAMES):
            handler, status, mutation = self._MCP_HANDLERS.get(
                name, ("unsupported", "blocker", name in MCP_MUTATION_NAMES),
            )
            reason = "" if status != "blocker" else "v2 operation has no native V2 service"
            registry["mcp"][name] = SurfaceSpec(name, status, handler, mutation, reason)
        for name, operation in sorted(GUI_OPERATION_SPECS.items()):
            handler = operation.native_handler
            # Keep the canonical GUI surface table compatible while routing
            # this legacy snapshot name to its explicit GUI contract.
            if name == "get_governance_snapshot":
                handler = "governance_snapshot"
            mutation = operation.mutation
            # The operation registry is the only GUI truth source.  A method
            # is implemented only when its canonical native handler resolves;
            # missing handlers are blockers, never a retired success class.
            available = self._service("gui", name, handler) or self._builtin(handler)
            status = "implemented" if callable(available) else "blocker"
            reason = "" if status == "implemented" else "v2 GUI canonical handler is not activated"
            registry["gui"][name] = SurfaceSpec(
                name,
                status,
                handler,
                mutation,
                reason,
                canonical_name=operation.canonical_name,
                domain=operation.domain,
                execution=operation.execution,
            )
        for name in sorted(CLI_COMMAND_NAMES):
            handler, status, mutation = self._CLI_HANDLERS.get(
                name, ("unsupported", "blocker", False),
            )
            if status == "blocker":
                reason = "v2 CLI command is not activated"
            elif status == "retired":
                reason = str(self._CLI_RETIRED_REASONS.get(name) or "legacy CLI operation retired in V2")
            else:
                reason = ""
            registry["cli"][name] = SurfaceSpec(name, status, handler, mutation, reason)
        registry["hook"]["bootstrap_hook"] = SurfaceSpec(
            "bootstrap_hook", "implemented", "hook_bootstrap", False,
        )
        return registry

    @property
    def surface_registry(self) -> Mapping[str, Mapping[str, SurfaceSpec]]:
        return {surface: dict(entries) for surface, entries in self._registry.items()}

    def registry(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            surface: {name: spec.to_dict() for name, spec in entries.items()}
            for surface, entries in self._registry.items()
        }

    def coverage(self) -> dict[str, Any]:
        surfaces: dict[str, Any] = {}
        canonical: dict[str, Any] = {}
        for surface in ("mcp", "gui", "cli", "hook"):
            entries = [self._registry[surface][name].to_dict() for name in sorted(self._registry[surface])]
            counts = {status: sum(item["status"] == status for item in entries) for status in ("implemented", "neutral-read", "retired", "blocker")}
            surfaces[surface] = {"total": len(entries), **counts, "entries": entries}
            canonical[surface] = entries
        digest = hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
        total = sum(item["total"] for item in surfaces.values())
        counts = {status: sum(item[status] for item in surfaces.values()) for status in ("implemented", "neutral-read", "retired", "blocker")}
        # ``complete`` is the historical blocker-only compatibility field.
        # ``neutral-read`` is diagnostic-only and therefore excluded from the
        # production readiness claim carried by ``production_complete``.
        complete = counts["blocker"] == 0
        production_complete = complete and counts["neutral-read"] == 0
        return {
            "schema": "v2-native-coverage-1",
            "registry_digest": digest,
            "coverage_digest": digest,
            "surfaces": surfaces,
            "counts": {"total": total, **counts},
            "complete": complete,
            "production_complete": production_complete,
        }

    @property
    def coverage_digest(self) -> str:
        return str(self.coverage()["registry_digest"])

    @property
    def registry_digest(self) -> str:
        """Stable digest alias used by activation evidence assemblers."""
        return self.coverage_digest

    # ---- stable envelopes ----------------------------------------------------
    @staticmethod
    def _error(surface: str, name: str, code: str, *, status: str = "error", generation: int | None = None, reason: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "status": status,
            "surface": surface,
            "name": name,
            "path": "v2",
            "code": code,
            "error": code,
        }
        if generation is not None:
            payload["generation"] = generation
        if reason:
            payload["reason"] = reason
        return payload

    @staticmethod
    def _result(surface: str, name: str, data: Any, *, generation: int, status: str = "ok") -> dict[str, Any]:
        # Existing V2 service ports (notably MaintenanceRuntimePort) already
        # return a transport envelope.  Preserve its data at the facade's
        # top-level instead of nesting ``data.data`` and losing stable CLI
        # fields such as ``candidate_count``.
        if isinstance(data, Mapping):
            payload = dict(_plain(data))
            raw_status = _text(payload.get("status")).casefold()
            failed = (
                payload.get("ok") is False
                or raw_status in {"error", "blocked", "failed"}
                or bool(payload.get("error"))
            )
            if failed:
                # A service envelope may contain arbitrary diagnostic fields.
                # Failure classification must happen before the legacy
                # transport-envelope shape check; otherwise ``ok=false`` plus
                # one extra field gets nested as data and the outer call looks
                # successful.
                payload.update({"surface": surface, "name": name, "path": "v2", "generation": generation})
                payload["ok"] = False
                payload["status"] = raw_status if raw_status in {"error", "blocked", "failed"} else "error"
                payload.setdefault("code", payload.get("error") or "v2_native_handler_failed")
                payload.setdefault("error", payload.get("code"))
                return payload
            # Canonical GUI operations already return the unified operation
            # envelope. Preserve operation/task/receipt at the top level so
            # SafeBridge, localhost HTTP, pywebview and direct native calls
            # observe the same business shape; transport metadata is additive.
            if "operation" in payload and raw_status in {
                "accepted", "queued", "running", "succeeded", "cancelled", "ok",
            }:
                payload.update({"surface": surface, "name": name, "path": "v2", "generation": generation})
                payload["ok"] = True
                return payload
            # Existing V2 service ports (notably MaintenanceRuntimePort) already
            # return a transport envelope.  Preserve its data at the facade's
            # top-level instead of nesting ``data.data`` and losing stable CLI
            # fields such as ``candidate_count``.
            if set(data).issubset({"ok", "status", "data", "code", "error", "reason"}) and "data" in data:
                payload.update({"surface": surface, "name": name, "path": "v2", "generation": generation})
                payload.setdefault("status", status)
                payload.setdefault("ok", payload.get("status") == "ok")
                return payload
            # Fixed availability responses are intentionally diagnostic-only.
            # Keep the business data shape stable while making the outer
            # transport status explicit for readiness/coverage consumers.
            diagnostic_only = payload.get("available") is True and set(payload).issubset({"available", "diagnostic_only"})
            return {
                "ok": True,
                "status": "diagnostic_only" if diagnostic_only else status,
                "surface": surface,
                "name": name,
                "path": "v2",
                "generation": generation,
                "data": payload,
            }
        return {
            "ok": status == "ok",
            "status": status,
            "surface": surface,
            "name": name,
            "path": "v2",
            "generation": generation,
            "data": _plain(data),
        }

    # ---- identity and scope --------------------------------------------------
    @staticmethod
    def _mapping_context(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if type(value) is NativeContextEnvelope or type(value) is NativeBoundContext:
            authority = resolve_native_transport_context(value)
            return {
                **authority.to_dict(),
                "__native_bound_context": authority,
                "__native_transport_capability": _NATIVE_CONTEXT_CAPABILITY,
            }
        if type(value) is dict:
            raw = dict(value)
            if "__native_bound_context" in raw:
                authority = resolve_native_transport_context(raw)
                # The public projection is advisory.  Always replace it with
                # the process-issued immutable authority values.
                return {
                    **authority.to_dict(),
                    "__native_bound_context": authority,
                    "__native_transport_capability": _NATIVE_CONTEXT_CAPABILITY,
                }
            return raw
        # Native context security must never consult an arbitrary object's
        # ``to_dict``/attributes: wrappers can return a forged authority map.
        raise NativeContextError("trusted_context_capability_required")

    @staticmethod
    def _resolve_identity(raw: Mapping[str, Any], canonical: str) -> str:
        values = [_text(raw[key]) for key in _IDENTITY_ALIASES[canonical] if key in raw and _text(raw[key])]
        if len(set(values)) > 1:
            raise NativeContextError("context_identity_conflict")
        return values[0] if values else ""

    def _context(self, raw: Any, payload: Mapping[str, Any] | None, *, required: bool, allow_partial: bool = False) -> dict[str, Any]:
        source = self._mapping_context(raw)
        if not source:
            if required:
                raise NativeContextError("native_context_required")
            return {}
        context: dict[str, Any] = {}
        for canonical in _IDENTITY_ALIASES:
            context[canonical] = self._resolve_identity(source, canonical)
        workspace_id = context["workspace_id"] or self.workspace
        if canonical_project_ref(workspace_id) != canonical_project_ref(self.workspace):
            raise NativeContextError("context_workspace_mismatch")
        context["workspace_id"] = self.workspace
        if required and not context["agent_instance_id"]:
            raise NativeContextError("context_identity_required")
        if required and not allow_partial and not context["share_group_id"]:
            raise NativeContextError("context_identity_required")
        # A request body can carry business data, but never a second identity.
        for key, value in (payload or {}).items():
            if key not in _IDENTITY_PAYLOAD_KEYS or value in (None, ""):
                continue
            if key in {"trusted_identity", "trusted_context", "identity"}:
                raise NativeContextError("context_identity_spoof")
            canonical = next((name for name, aliases in _IDENTITY_ALIASES.items() if key in aliases), "")
            expected = context.get(canonical, "") if canonical else ""
            if expected:
                if canonical == "project_ref":
                    matches = canonical_project_ref(value) == canonical_project_ref(expected)
                else:
                    matches = _text(value) == expected
                if not matches:
                    raise NativeContextError("context_identity_spoof")
        # Keep provenance fields available to ContextEngine and governance
        # stores, but they are copied from the trusted context only.
        for key in (
            "admin", "is_admin", "authority", "automatic", "session_id",
            "session_source", "session_trusted", "context_hash",
            "runtime_agent_id", "parent_agent_id", "namespace_id",
            "sensitivity", "policy_class", "entrypoint",
        ):
            if key in source:
                context[key] = source[key]
        # MaintenanceRuntimePort uses a process-local capability object.  It
        # is not an identity field and must survive this normalization intact;
        # callers cannot forge it by serializing a guessed value.
        for key, value in source.items():
            if str(key).startswith("__"):
                context[str(key)] = value
        context.setdefault("actor", context.get("agent_instance_id", ""))
        context.setdefault("authority", "admin" if bool(context.get("admin")) else "manual")
        # Maintenance transport still consumes its historical trusted-agent
        # spelling.  Mirror the canonical value; it is derived, never read
        # from an untrusted payload.
        context.setdefault("trusted_agent_id", context.get("agent_instance_id", ""))
        context["trusted_identity"] = {
            key: value for key, value in context.items() if key in {
                "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role", "workspace_id",
            }
        }
        return context

    def _scope(self, context: Mapping[str, Any]) -> dict[str, Any]:
        if not context.get("workspace_id") or not context.get("share_group_id"):
            raise NativeContextError("context_scope_required")
        return {
            "workspace_id": self.workspace,
            "share_group_id": context.get("share_group_id", ""),
            "agent_instance_id": context.get("agent_instance_id", ""),
            "project_ref": context.get("project_ref", ""),
            "provider": context.get("provider", ""),
            "runtime_role": context.get("runtime_role", ""),
            # Preserve the separately authenticated admin capability on read
            # scopes.  Without it, migrated atoms with intentionally empty
            # project/provider/runtime fields disappear behind the caller's
            # narrower transport scope during MCP update/read.
            "admin": self._trusted_admin(context),
        }

    def _mutation_context(self, context: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(context)
        scope.update({
            "actor": context.get("actor") or scope.get("agent_instance_id"),
            "admin": bool(context.get("admin", context.get("is_admin", False))),
            "authority": str(context.get("authority") or ("admin" if context.get("admin") else "manual")),
        })
        return scope

    def _normalize_memory_audience(
        self,
        raw: Any,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Normalize a memory audience against immutable transport identity.

        Atom owner columns remain the trusted writer's exact scope so
        GovernanceV2 can authorize the mutation.  The normalized marker is
        then used by the V2 memory read path for project/group visibility.
        No caller-supplied agent, group, or project is allowed to replace the
        transport context.
        """

        if raw is None:
            requested: dict[str, Any] = {}
        elif isinstance(raw, str):
            requested = {"target_type": raw}
        elif isinstance(raw, Mapping):
            requested = {str(key): value for key, value in raw.items()}
        else:
            raise NativePortError("invalid_memory_audience")

        target_type = _text(
            requested.get("target_type")
            or requested.get("type")
            or requested.get("mode")
        ).casefold()
        if target_type in {"share_group", "shared_group"}:
            target_type = "group"
        if not target_type:
            target_type = "agent"
        if target_type not in {"agent", "agent_project", "project", "group"}:
            if target_type in {"system", "provider", "runtime", "runtime_role", "runtime-role"}:
                raise NativePortError("admin_scope_required")
            raise NativePortError("invalid_memory_audience")

        trusted_agent = _text(context.get("agent_instance_id"))
        trusted_group = _text(context.get("share_group_id"))
        trusted_project = _text(context.get("project_ref"))
        canonical_trusted_project = canonical_project_ref(trusted_project)
        requested_id = _text(
            requested.get("target_id")
            or requested.get("id")
            or requested.get("agent_instance_id")
            or requested.get("agent_id")
        )
        requested_project = _text(
            requested.get("project_ref")
            or requested.get("project_id")
            or (requested_id if target_type == "project" else "")
        )
        if requested_project and canonical_project_ref(requested_project) != canonical_trusted_project:
            raise NativePortError("other_project_scope_denied")

        if target_type == "agent":
            target_id = requested_id or trusted_agent
            if target_id != trusted_agent and not self._trusted_admin(context):
                raise NativePortError("other_agent_scope_denied")
            return {
                "source": "native_v2",
                "target_type": "agent",
                "target_id": target_id,
                # Audience types are semantic contracts, not bags of optional
                # filters. Agent scope follows the agent across projects,
                # providers and runtime roles; use agent_project for narrowing.
                "project_ref": "",
                "provider": "",
                "runtime_role": "",
                "effect": "include",
            }

        if target_type == "agent_project":
            target_id = requested_id or trusted_agent
            if target_id != trusted_agent and not self._trusted_admin(context):
                raise NativePortError("other_agent_scope_denied")
            if not canonical_trusted_project:
                raise NativePortError("project_scope_required")
            return {
                "source": "native_v2",
                "target_type": "agent_project",
                "target_id": target_id,
                "project_ref": canonical_trusted_project,
                "provider": "",
                "runtime_role": "",
                "effect": "include",
            }

        if target_type == "project":
            if not canonical_trusted_project:
                raise NativePortError("project_scope_required")
            return {
                "source": "native_v2",
                "target_type": "project",
                "target_id": canonical_trusted_project,
                "project_ref": canonical_trusted_project,
                "provider": "",
                "runtime_role": "",
                "effect": "include",
            }

        # A group audience expands visibility beyond the writing Agent.  Keep
        # that capability admin-only, and compare the group after the trusted
        # capability gate so a caller cannot probe foreign-group existence.
        if not self._trusted_admin(context):
            raise NativePortError("admin_scope_required")
        target_id = requested_id or trusted_group
        if target_id != trusted_group:
            raise NativePortError("other_group_scope_denied")
        return {
            "source": "native_v2",
            "target_type": "group",
            "target_id": trusted_group,
            "project_ref": "",
            "provider": _text(requested.get("provider")),
            "runtime_role": _text(requested.get("runtime_role") or requested.get("runtime")),
            "effect": "include",
        }

    @staticmethod
    def _trusted_admin(context: Mapping[str, Any]) -> bool:
        """Return true only for an admin AccessContext with trusted session provenance.

        ``is_admin`` alone is a client-controlled mapping when callers invoke
        the native port directly.  AccessContext serialisation carries the
        immutable session tuple; requiring it here keeps governance writes
        fail-closed even when the transport wrapper is bypassed.
        """
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError:
            return False
        # Read authority fields from the immutable object, never the mutable
        # compatibility mapping surrounding it.
        is_admin = bool(authority.admin)
        session_id = authority.session_id
        source = authority.session_source.casefold()
        return bool(
            is_admin
            and authority.session_trusted is True
            and session_id
            and source in {"host", "transport"}
        )

    def _bind_cli_transport_context(self, context: Any) -> Any:
        """Mint trusted CLI scope when outer CLI adapter omitted its envelope.

        CLI command flags are not identity.  The native route may recover a
        missing envelope from process-owned AccessContext plus the active
        binding for that agent; a non-empty plain mapping remains untrusted and
        is never upgraded.
        """
        if context is not None and (not isinstance(context, Mapping) or bool(context)):
            return context
        try:
            from ..access_context import AccessContext, load_access_context
            from .group_native import GroupControlService

            access_context = load_access_context()
            if type(access_context) is not AccessContext:
                raise NativeContextError("native_context_required")
            agent_instance_id = _text(access_context.trusted_agent_id)
            if not agent_instance_id:
                raise NativeContextError("native_context_required")
            binding = GroupControlService(self.workspace, write=False).active_binding_for_agent(
                agent_instance_id,
            )
            share_group_id = _text((binding or {}).get("share_group_id"))
            if not share_group_id:
                raise NativeContextError("native_context_required")
            return bind_native_transport_context(
                access_context,
                workspace_id=self.workspace,
                share_group_id=share_group_id,
                runtime_role="cli",
                entrypoint="cli",
            )
        except NativeContextError:
            return context
        except Exception:
            return context

    # ---- lazy stores ---------------------------------------------------------
    @staticmethod
    def _schema_blocker(kind: str, path: Path, *, detail: str = "") -> NativePortError:
        """Return a stable, non-leaking schema preflight blocker."""

        return NativePortError(kind, detail or str(path.name))

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)), int(stat.st_size), int(stat.st_mtime_ns))

    def _validate_schema_connection(self, domain: str, path: Path, conn: sqlite3.Connection) -> None:
        """Validate one already-open read-only SQLite connection."""

        spec = _NATIVE_SCHEMA_SPECS.get(domain)
        if spec is None:
            raise NativePortError("v2_schema_spec_unavailable")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables:
            raise self._schema_blocker("v2_schema_missing", path)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version > 1:
            raise self._schema_blocker("v2_schema_future", path)
        if user_version not in {0, 1}:
            raise self._schema_blocker("v2_schema_invalid", path)
        for table, expected_domain, expected_marker in spec["meta"]:
            if table not in tables:
                raise self._schema_blocker("v2_schema_partial", path)
            rows = conn.execute(f"SELECT domain, version, marker FROM {table}").fetchall()
            if not rows:
                raise self._schema_blocker("v2_schema_partial", path)
            if len(rows) != 1:
                raise self._schema_blocker("v2_schema_invalid", path)
            row_domain, row_version, row_marker = str(rows[0][0]), int(rows[0][1]), str(rows[0][2])
            if row_version > 1:
                raise self._schema_blocker("v2_schema_future", path)
            if row_domain != expected_domain or row_marker != expected_marker or row_version != 1:
                raise self._schema_blocker("v2_schema_invalid", path)
        for table, required_columns in spec["tables"].items():
            if table not in tables:
                raise self._schema_blocker("v2_schema_partial", path)
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required_columns <= columns:
                raise self._schema_blocker("v2_schema_partial", path)

    def _open_schema_lease(self, domain: str) -> _SchemaLease:
        """Open/validate RO DB and hold it through Store construction."""

        paths = self.layout.db_paths(domain)
        assert isinstance(paths, tuple)
        path = paths[0]
        self.layout.assert_database_path(path, domain)
        if not path.is_file() or path.stat().st_size == 0:
            raise self._schema_blocker("v2_schema_missing", path)
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_database(path, readonly=True)
            self._validate_schema_connection(domain, path, conn)
            return _SchemaLease(domain, path, self._file_identity(path), conn)
        except NativePortError:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            raise self._schema_blocker("v2_schema_invalid", path) from exc

    def _assert_schema_lease(self, lease: _SchemaLease) -> None:
        try:
            if self._file_identity(lease.path) != lease.identity:
                raise self._schema_blocker("v2_schema_replaced", lease.path)
            self._validate_schema_connection(lease.domain, lease.path, lease.connection)
        except NativePortError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise self._schema_blocker("v2_schema_invalid", lease.path) from exc

    def _preflight_domain(self, domain: str) -> Path:
        """Read-only validate one phase-2 database without Store init."""

        with self._open_schema_lease(domain) as lease:
            return lease.path

    def _preflight_governance_ledger(self) -> Path:
        """Validate the durable GovernanceV2 idempotency/receipt ledger."""

        path = self.layout.root / "governance_v2" / "decisions.db"
        if not path.is_file() or path.stat().st_size == 0:
            raise self._schema_blocker("v2_governance_ledger_missing", path)
        with self._open_governance_lease(path) as lease:
            return lease.path

    def _open_governance_lease(self, path: Path) -> _SchemaLease:
        """Open and hold the GovernanceV2 ledger RO across boundary init."""

        conn: sqlite3.Connection | None = None
        try:
            conn = connect_database(path, readonly=True)
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if _GOVERNANCE_LEDGER_TABLE not in tables:
                raise self._schema_blocker("v2_governance_ledger_partial", path)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(decisions)")}
            if not _GOVERNANCE_LEDGER_COLUMNS <= columns:
                raise self._schema_blocker("v2_governance_ledger_partial", path)
            return _SchemaLease("governance", path, self._file_identity(path), conn)
        except NativePortError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            if conn is not None:
                conn.close()
            raise self._schema_blocker("v2_governance_ledger_invalid", path) from exc

    def _assert_governance_lease(self, lease: _SchemaLease) -> None:
        try:
            # GovernanceV2 legitimately grows/updates this SQLite file while
            # another native writer is in flight.  Size and mtime therefore
            # are not replacement signals here; device/inode still fail
            # closed if the ledger itself is swapped underneath the lease.
            if self._file_identity(lease.path)[:2] != lease.identity[:2]:
                raise self._schema_blocker("v2_governance_ledger_replaced", lease.path)
            tables = {str(row[0]) for row in lease.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {str(row[1]) for row in lease.connection.execute("PRAGMA table_info(decisions)")}
            if _GOVERNANCE_LEDGER_TABLE not in tables or not _GOVERNANCE_LEDGER_COLUMNS <= columns:
                raise self._schema_blocker("v2_governance_ledger_partial", lease.path)
        except NativePortError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise self._schema_blocker("v2_governance_ledger_invalid", lease.path) from exc

    def _preflight_memory_governance(self) -> None:
        self._preflight_domain("memory")
        self._preflight_domain("evidence")
        self._preflight_governance_ledger()

    def _assert_existing_db(self, domain: str) -> Path:
        paths = self.layout.db_paths(domain)
        assert isinstance(paths, tuple)
        path = paths[0]
        self.layout.assert_database_path(path, domain)
        if not path.is_file():
            raise NativePortError("v2_store_unavailable")
        return path

    def _domain_store(self, domain: str, *, write: bool = False) -> Any:
        key = f"{domain}:{'write' if write else 'read'}"
        if key in self._stores and self._stores[key] is not None:
            return self._stores[key]
        injected = self._stores.get(domain)
        # Every injected store is a read-only test seam.  Writes always use
        # builtins constructed after schema/ledger leases; an explicit
        # capability cannot substitute a mutation path for any domain.
        if injected is not None and not write:
            self._stores[key] = injected
            return injected
        lease: _SchemaLease | None = None
        if domain in _NATIVE_SCHEMA_SPECS:
            lease = self._open_schema_lease(domain)
        else:
            self._assert_existing_db(domain)
        value: Any
        try:
            if lease is not None:
                self._assert_schema_lease(lease)
            if domain == "memory":
                from ..memory.store import MemoryAtomStore
                value = MemoryAtomStore(self.workspace, readonly=not write, initialize=False)
            elif domain == "evidence":
                from ..evidence.store import EvidenceStore
                value = EvidenceStore(self.workspace, readonly=not write, initialize=False)
            elif domain == "rules":
                from ..rules.v2_store import RuleV2Store
                value = RuleV2Store(self.workspace, read_only=not write, initialize=False)
            elif domain == "assets":
                from ..assets_v2.store import AssetStore
                value = AssetStore(self.workspace, readonly=not write, initialize=False)
            elif domain == "skills":
                from ..skills_v2.store import SkillStore
                value = SkillStore(self.workspace, readonly=not write)
            elif domain == "content":
                from ..content.store import ContentStore
                value = ContentStore(self.workspace, initialize=False)
            elif domain == "codegraph":
                from ..codegraph_v2.store import CodeGraphStore
                value = CodeGraphStore(self.workspace, initialize=False)
                # CodeGraphStore's writable constructor is intentionally not
                # used here.  Run its read-only schema preflight explicitly so
                # partial/future databases fail closed before graph reads.
                preflight = getattr(value, "_preflight", None)
                if not callable(preflight):
                    raise NativePortError("v2_codegraph_schema_unavailable")
                codegraph_state = preflight()
                if codegraph_state == "v1":
                    if not write:
                        raise NativePortError("codegraph_schema_upgrade_required")
                    migrate = getattr(value, "_migrate_v1_to_v2", None)
                    if not callable(migrate):
                        raise NativePortError("codegraph_schema_upgrade_unavailable")
                    migrate()
                elif codegraph_state in {"fresh", "needs_aux"}:
                    if not write:
                        raise NativePortError("v2_codegraph_schema_unavailable")
                    ensure_aux = getattr(value, "_ensure_aux_schema", None)
                    if not callable(ensure_aux):
                        raise NativePortError("v2_codegraph_schema_unavailable")
                    ensure_aux()
                elif codegraph_state != "current":
                    raise NativePortError("v2_codegraph_schema_unavailable")
            elif domain == "projection":
                from ..projection_v2.store import ProjectionStore
                value = ProjectionStore(self.workspace, initialize=False)
            else:
                raise NativePortError("v2_store_unavailable")
            if lease is not None:
                self._assert_schema_lease(lease)
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_store_schema_invalid") from exc
        finally:
            # The lease intentionally spans the constructor above and the
            # second identity/schema check below; it is never held beyond the
            # resulting store's own lifecycle.
            if lease is not None:
                lease.close()
        self._stores[key] = value
        return value

    def _service(self, surface: str, name: str, handler: str) -> Callable[..., Any] | None:
        # Memory/evidence writes are the security boundary.  Even an explicit
        # test capability may not replace the builtin GovernanceV2 route.
        if handler in {
            "memory_write", "memory_update", "memory_delete",
            "memory_merge_safe", "memory_merge_safe_preview",
            # Phase 9 services are production builtins.  A test/host service
            # mapping must not be able to replace the real history/source/
            # import/diagnostics implementation or promote coverage by DI.
            "list_sources", "scan_summary", "import_preview", "runtime_processes",
            "history_search", "history_timeline", "history_read",
            "history_extract_preview", "history_list_sessions", "history_export",
            "history_delete",
            "external_mcp_list", "external_mcp_detect", "external_mcp_preview", "external_mcp_import",
            "knowledge_book", "knowledge_candidates",
            "extract_memories", "accept_candidates", "list_pending_enrichments",
            "apply_enrichments", "enrichment_status", "build_and_enrich", "resolve_group",
            "sandbox_status", "host_enrichment_guide", "host_llm_agents",
            "coverage", "projection_graph", "codegraph_graph", "codegraph_query", "codegraph_path", "codegraph_explain",
            "codegraph_affected", "codegraph_update", "codegraph_status", "semantic_check", "reference_audit", "provider_install",
            "rule_create_auto", "rule_feedback", "rule_undo", "rule_decision_read", "rule_scope_stats",
        }:
            return None
        return self._services.get(f"{surface}:{name}") or self._services.get(name) or self._services.get(handler)

    @staticmethod
    def _map_governance_error(error: Exception) -> NativePortError:
        """Translate governance failures to stable native transport codes."""

        if isinstance(error, sqlite3.Error):
            code = int(getattr(error, "sqlite_errorcode", 0) or 0) & 0xff
            if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return NativePortError("v2_governance_busy")
            return NativePortError("v2_governance_storage_error")
        try:
            from ..governance_v2.context import V2ContextError, V2ScopeError
            from ..governance_v2.boundary import V2GovernanceError
        except Exception:  # pragma: no cover - package import failure
            V2ContextError = V2ScopeError = V2GovernanceError = ()  # type: ignore[assignment]
        if isinstance(error, V2ScopeError):
            return NativePortError("mutation_scope_rejected")
        if isinstance(error, V2ContextError):
            return NativePortError("mutation_context_rejected")
        if isinstance(error, V2GovernanceError):
            message = str(error).casefold()
            if "idempotency" in message or "request" in message:
                return NativePortError("idempotency_conflict")
            return NativePortError("v2_governance_rejected")
        return NativePortError("v2_governance_rejected")

    def _governance_boundary(self) -> Any:
        """Return GovernanceV2 only after all native write preflights pass."""

        with self._governance_boundary_lock:
            if self._governance_boundary_instance is not None:
                return self._governance_boundary_instance
            from ..governance_v2 import GovernanceV2

            memory = self._domain_store("memory", write=True)
            evidence = self._domain_store("evidence", write=True)
            ledger_path = self.layout.root / "governance_v2" / "decisions.db"
            if not ledger_path.is_file() or ledger_path.stat().st_size == 0:
                raise NativePortError("v2_governance_ledger_missing")
            ledger = self._open_governance_lease(ledger_path)
            try:
                self._assert_governance_lease(ledger)
                value = GovernanceV2(self.workspace, memory_store=memory, evidence_store=evidence)
                self._assert_governance_lease(ledger)
            except NativePortError:
                raise
            except Exception as exc:
                raise NativePortError("v2_governance_unavailable") from exc
            finally:
                ledger.close()
            # Keep this separate from the injectable ``_stores`` mapping: a
            # test/host capability must never replace the production writer.
            self._governance_boundary_instance = value
            self._stores["governance"] = value
            return value

    # ---- built-in semantic handlers -----------------------------------------
    @staticmethod
    def _row_value(row: Any, key: str, default: Any = "") -> Any:
        if isinstance(row, Mapping):
            return row.get(key, default)
        return getattr(row, key, default)

    @staticmethod
    def _canonical_rule_id(store: Any, definition_id: Any) -> str:
        value = _text(definition_id)
        resolver = getattr(store, "resolve_canonical", None)
        if not value or not callable(resolver):
            return value
        try:
            resolved = _text(resolver(value)) or value
            # ``resolve_canonical`` intentionally ignores a malformed active
            # predecessor. Native reads still need to honor its explicit
            # successor marker, otherwise one partially migrated row leaks
            # beside its canonical head.
            getter = getattr(store, "get_definition", None)
            if callable(getter):
                seen: set[str] = set()
                while resolved and resolved not in seen:
                    seen.add(resolved)
                    definition = getter(resolved)
                    successor = _text(
                        definition.get("superseded_by")
                        if isinstance(definition, Mapping)
                        else getattr(definition, "superseded_by", "")
                    ) if definition is not None else ""
                    if not successor or successor == resolved:
                        break
                    resolved = successor
            return resolved
        except Exception:
            # A partial rules store must not make neutral reads unavailable;
            # retaining original ID still keeps projection deterministic.
            return value

    def _active_rule_source_memory_ids(self, context: Mapping[str, Any]) -> set[str]:
        """Return memory IDs represented by active canonical rule rows."""

        group = _text(context.get("share_group_id"))
        if not group or not self.layout.rules_db.is_file():
            return set()
        try:
            rules = self._domain_store("rules")
            definitions = list(rules.list_definitions(status="active"))
            canonical_ids = {
                self._canonical_rule_id(rules, self._row_value(item, "definition_id"))
                for item in definitions
            }
            canonical_ids.discard("")
            links_reader = getattr(rules, "list_source_links", None)
            if callable(links_reader):
                links = links_reader(share_group_id=group, status="active")
            else:
                links = []
            return {
                _text(self._row_value(link, "memory_id"))
                for link in links
                if _text(self._row_value(link, "memory_id"))
                and self._canonical_rule_id(
                    rules, self._row_value(link, "canonical_definition_id"),
                ) in canonical_ids
            }
        except Exception:
            return set()

    def _canonical_memory_rows(
        self,
        rows: list[Any] | tuple[Any, ...],
        context: Mapping[str, Any],
        *,
        status: Any = "active",
        suppress_rule_sources: bool = True,
    ) -> list[Any]:
        """Collapse active memory aliases/supersession chains by IDs.

        Body text is intentionally never used as identity. Distinct conflict
        rows therefore remain visible, while a canonical rule source wins over
        its raw memory mirror in cross-category GUI/MCP projections.
        """

        source_memory_ids = self._active_rule_source_memory_ids(context) if suppress_rule_sources else set()
        candidates: list[tuple[str, str, Any]] = []
        superseded_ids: set[str] = set()
        for row in rows:
            row_status = _text(self._row_value(row, "status")) or "active"
            if status and row_status.casefold() != _text(status).casefold():
                continue
            memory_id = _text(self._row_value(row, "memory_id"))
            atom_id = _text(self._row_value(row, "atom_id"))
            if not memory_id and not atom_id:
                continue
            identity = memory_id or atom_id
            supersedes = self._row_value(row, "supersedes", ())
            if isinstance(supersedes, (list, tuple, set)):
                superseded_ids.update(_text(item) for item in supersedes if _text(item))
            metadata = self._row_value(row, "metadata", {})
            if not isinstance(metadata, Mapping):
                metadata = {}
            canonical_id = next(
                (
                    _text(self._row_value(row, key))
                    or _text(metadata.get(key))
                    for key in ("canonical_memory_id", "canonical_id", "canonical_atom_id")
                    if _text(self._row_value(row, key)) or _text(metadata.get(key))
                ),
                identity,
            )
            candidates.append((identity, canonical_id, row))

        selected: dict[str, tuple[tuple[Any, ...], Any]] = {}
        for identity, canonical_id, row in candidates:
            if identity in superseded_ids or identity in source_memory_ids:
                continue
            revision = self._row_value(row, "revision", 0)
            try:
                revision_key = int(revision or 0)
            except (TypeError, ValueError):
                revision_key = 0
            rank = (
                revision_key,
                _text(self._row_value(row, "updated_at")),
                _text(self._row_value(row, "created_at")),
                _text(self._row_value(row, "atom_id")),
                identity,
            )
            current = selected.get(canonical_id)
            if current is None or rank > current[0]:
                selected[canonical_id] = (rank, row)

        # Keep source/store ordering stable after selecting one row per ID.
        return [
            row for _identity, canonical_id, row in candidates
            if selected.get(canonical_id, (None, None))[1] is row
        ]

    def _memory_read(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        store = self._domain_store("memory")
        scope = self._scope(context)
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        result = store.get_atom(memory_id, scope=scope, atom_id=_text(payload.get("atom_id")))
        if result is None:
            # Native writes are durable before the asynchronous evidence
            # projection promotes them from ``building`` to ``active``.  An
            # exact, already-authorized read must still observe that write;
            # keep building rows out of list/search by limiting this fallback
            # to explicit native audience markers (or trusted admin scope).
            building = store.get_atom(
                memory_id,
                scope=scope,
                atom_id=_text(payload.get("atom_id")),
                include_building=True,
            )
            if building is not None:
                from ..memory.store import MemoryAtomStore

                audience = MemoryAtomStore._native_audience(building)
                if self._trusted_admin(context) or audience is not None:
                    result = building
        return result

    def _require_memory_owner(self, atom: Any, context: Mapping[str, Any]) -> None:
        """Keep audience visibility separate from mutation ownership.

        A trusted admin may read the whole bound group, but that read
        capability must not become an owner override for memory mutations.
        GovernanceV2 keeps its separate explicit admin path for internal
        corrections; the public native memory surface remains owner-scoped.
        """

        metadata = getattr(atom, "metadata", {})
        owner = _text(metadata.get("owner_agent_id")) if isinstance(metadata, Mapping) else ""
        owner = owner or _text(getattr(atom, "agent_instance_id", ""))
        if not owner or owner != _text(context.get("agent_instance_id")):
            # Do not distinguish an audience-visible atom from a missing one.
            raise NativePortError("memory_not_found")

    def _memory_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        store = self._domain_store("memory")
        status = payload.get("status") or "active"
        rows = store.list_atoms(scope=self._scope(context), status=status)
        return self._canonical_memory_rows(rows, context, status=status)

    def _memory_search(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        store = self._domain_store("memory")
        query = _text(payload.get("query") or payload.get("text") or payload.get("q")).casefold()
        status = payload.get("status") or "active"
        search = getattr(store, "search", None)
        if callable(search):
            try:
                values = search(
                    query=query,
                    scope=self._scope(context),
                    limit=payload.get("limit"),
                    status=status,
                )
            except TypeError:
                # Preserve compatibility with narrow read-only test/host
                # adapters that predate the explicit status selector.
                values = search(
                    query=query,
                    scope=self._scope(context),
                    limit=payload.get("limit"),
                )
            return self._canonical_memory_rows(values, context, status=status)
        values = store.list_atoms(scope=self._scope(context), status=status)
        if not query:
            return self._canonical_memory_rows(values, context, status=status)
        filtered = [item for item in values if query in _text(self._row_value(item, "body")).casefold()]
        return self._canonical_memory_rows(filtered, context, status=status)

    def _memory_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        if not _text(context.get("share_group_id")):
            return {
                "available": self.layout.memory_db.is_file(),
                "status": "UNBOUND",
                "scope": {"share_group_id": "", "agent_instance_id": "", "project_ref": ""},
                "total_records": 0,
                "active_count": 0,
                "status_counts": {},
                "kind_counts": {},
                "evidence_link_count": 0,
            }
        scope = self._scope(context)
        if not self.layout.memory_db.is_file():
            return {
                "available": False,
                "status": "NO_SOURCE",
                "scope": {
                    "share_group_id": scope.get("share_group_id", ""),
                    "agent_instance_id": scope.get("agent_instance_id", ""),
                    "project_ref": scope.get("project_ref", ""),
                },
                "total_records": 0,
                "active_count": 0,
                "status_counts": {},
                "kind_counts": {},
                "evidence_link_count": 0,
            }
        try:
            store = self._domain_store("memory")
            atoms = list(store.list_atoms(scope=scope, include_building=False))
            by_status: dict[str, int] = {}
            by_kind: dict[str, int] = {}
            evidence_links = 0
            for atom in atoms:
                status = str(getattr(atom, "status", "") or "unknown")
                kind = str(getattr(atom, "kind", "") or "unknown")
                by_status[status] = by_status.get(status, 0) + 1
                by_kind[kind] = by_kind.get(kind, 0) + 1
                evidence_links += len(store.evidence_ids_for_atom(atom.atom_id))
            return {
                "available": True,
                "scope": {
                    "share_group_id": scope.get("share_group_id", ""),
                    "agent_instance_id": scope.get("agent_instance_id", ""),
                    "project_ref": scope.get("project_ref", ""),
                },
                "total_records": len(atoms),
                "active_count": int(by_status.get("active", 0)),
                "status_counts": by_status,
                "kind_counts": by_kind,
                "evidence_link_count": evidence_links,
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_memory_status_unavailable") from exc

    def _memory_global_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Return Meitner's authoritative V2 global group/memory aggregate."""

        del payload
        if (self.layout.memory_db.is_file() or self.layout.manifest_db.is_file()) and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        try:
            from .group_native import GroupControlService

            service = self._group_service(write=False)
            if not isinstance(service, GroupControlService):
                raise NativePortError("v2_group_control_unavailable")
            result = dict(service.get_global_memory_status())
            if not self.layout.memory_db.is_file() and not self.layout.manifest_db.is_file():
                result.setdefault("scope", {
                    "share_group_id": _text(context.get("share_group_id")),
                    "agent_instance_id": _text(context.get("agent_instance_id")),
                    "project_ref": _text(context.get("project_ref")),
                })
            return result
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_memory_global_status_unavailable"
            ) from exc

    def _memory_versions(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read redacted V2 revision metadata for one exact transport scope."""
        store = self._domain_store("memory")
        reader = getattr(store, "list_revisions", None)
        if not callable(reader):
            raise NativePortError("v2_store_schema_invalid")
        return {
            "versions": reader(
                scope=self._scope(context),
                memory_id=_text(payload.get("memory_id") or payload.get("id")),
                atom_id=_text(payload.get("atom_id")),
                limit=payload.get("limit", 100),
            ),
        }

    def _memory_supersede_chain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read direct V2 supersession edges without exposing atom bodies."""
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        store = self._domain_store("memory")
        reader = getattr(store, "supersede_chain", None)
        if not callable(reader):
            raise NativePortError("v2_store_schema_invalid")
        result = reader(memory_id, scope=self._scope(context))
        if result is None:
            raise NativePortError("memory_not_found")
        return result

    def _mandatory_snapshot(self, context: Mapping[str, Any]) -> list[Any]:
        from .mandatory_publication import snapshot

        return snapshot(self, context)

    def _validate_mandatory_publication(self, context: Mapping[str, Any], before: list[Any]) -> dict[str, Any]:
        from .mandatory_publication import MandatoryPublicationError, validate

        try:
            return validate(self, context, before)
        except MandatoryPublicationError as exc:
            raise NativePortError(exc.code) from exc

    def _memory_write(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        scope = self._scope(context)
        with _native_memory_mutation_lock(self.workspace, scope["share_group_id"]):
            governance = self._governance_boundary()
            with governance.atomic_memory() as owner:
                before = self._mandatory_snapshot(context) if owner else []
                result = self._memory_write_unlocked(payload, context, **kwargs)
                if owner:
                    self._validate_mandatory_publication(context, before)
                return result

    def _memory_write_unlocked(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        clean = dict(payload)
        # The public MCP create contract requires only ``body``.  The
        # organizer owns generated logical IDs and fallback retry keys;
        # explicit IDs/keys remain available for callers that need stable
        # cross-request identity.
        if "body" not in clean:
            raise NativePortError("memory_payload_required")
        idempotency_key = _text(clean.pop("idempotency_key", ""))
        gui_state_marker = clean.pop("_native_gui_state_capability", None)
        preserve_gui_state = gui_state_marker is _GUI_STATE_PRESERVATION_CAPABILITY
        gui_state_action = _text(clean.pop("_native_gui_state_action", ""))
        if preserve_gui_state:
            # This private flag is generated only after the GUI admin gate;
            # organizer treats it as an internal complete-state update.
            clean["_preserve_state"] = True
            clean["_preserve_provenance"] = gui_state_action in {"lock", "unlock", "policy"}
        trusted_owner = _text(clean.pop("_trusted_owner_agent_id", ""))
        evidence = clean.pop("evidence", None)
        evidence_ids = clean.pop("evidence_ids", None)
        source_mappings = clean.pop("source_mappings", None)
        reason = _text(clean.pop("reason", "")) or "native memory write"
        requested_audience = clean.pop("audience", None)
        requested_scope = clean.pop("scope", None)
        metadata = clean.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, Mapping):
            raise NativePortError("invalid_memory_metadata")
        metadata = dict(metadata)
        if requested_audience is None:
            requested_audience = requested_scope
        if requested_audience is None and isinstance(metadata.get("audience"), Mapping):
            requested_audience = metadata.get("audience")
        if not preserve_gui_state:
            audience = self._normalize_memory_audience(requested_audience, context)
            # The audience marker is rewritten from trusted context on every
            # public update, so stale/forged metadata cannot survive a
            # mutation.  GUI state updates have already loaded the complete
            # atom and are deliberately metadata-preserving.
            metadata["audience"] = audience
            metadata["owner_agent_id"] = trusted_owner or _text(context.get("agent_instance_id"))
        clean["metadata"] = metadata
        # Atom confidence and governance-decision confidence are different
        # facts.  Keep ``confidence`` on the MemoryAtom; transport adapters
        # may supply a separate internal decision_confidence.
        decision_confidence = clean.pop("decision_confidence", 1.0)
        scope = self._scope(context)
        clean.update({key: value for key, value in scope.items() if value})
        # Public memory writes use the V2 automatic-organization service as
        # their sole mutation boundary.  The writable store and GovernanceV2
        # engine are constructed only after their native schema/lease
        # preflights; no caller-provided object can replace either one.
        try:
            from ..memory.store import MemoryReadScope
            from .dedup import V2SemanticDeduplicator
            from ..auto_organizer import AutoOrganizer

            dedup_scope = MemoryReadScope(
                workspace_id=self.workspace,
                share_group_id=scope["share_group_id"],
                # Semantic candidate lookup is group-scoped, not caller-
                # scoped.  The trusted caller identity remains in the
                # organizer mutation context/provenance; putting it here
                # would make same-group agents unable to see one another's
                # duplicate candidates.
                agent_instance_id="",
                project_ref="",
                provider="",
                runtime_role="",
                # This is a private, body-consuming candidate lookup inside
                # the already trusted group boundary.  It needs the store's
                # minimal group-wide read capability so native audience ACLs
                # do not hide another member's candidate; it is never exposed
                # as the caller's authorization or returned as content.
                admin=True,
            )
            deduplicator = V2SemanticDeduplicator(
                self._domain_store("memory"),
                dedup_scope,
            )
            memory_store = self._domain_store("memory", write=True)
            governance_v2 = self._governance_boundary()
            organizer = AutoOrganizer(
                self.workspace,
                scope["share_group_id"],
                store=memory_store,
                engine=governance_v2,
                deduplicator=deduplicator,
            )
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_memory_organization_unavailable") from exc
        # Reinsert transport fields consumed above.  They are now service
        # inputs, while identity/scope fields remain the values derived from
        # the bound native context rather than request payload claims.
        clean["idempotency_key"] = idempotency_key
        clean["reason"] = reason
        clean["decision_confidence"] = decision_confidence
        if evidence is not None:
            clean["evidence"] = evidence
        if evidence_ids is not None:
            clean["evidence_ids"] = evidence_ids
        if source_mappings is not None:
            clean["source_mappings"] = source_mappings
        bound_v2_context = self._mutation_context(context)
        try:
            result = organizer.service.write(
                clean,
                context=bound_v2_context,
            )
        except Exception as exc:
            code = _text(getattr(exc, "code", ""))
            if code:
                raise NativePortError(code) from exc
            raise self._map_governance_error(exc) from exc
        if not isinstance(result, Mapping) or "atom" not in result:
            raise NativePortError("v2_memory_organization_invalid")
        response = dict(result)
        atom = response["atom"]
        atom_status = _text(
            getattr(atom, "status", "")
            if not isinstance(atom, Mapping)
            else atom.get("status", "")
        ).casefold()
        atom_id = _text(
            getattr(atom, "atom_id", "")
            if not isinstance(atom, Mapping)
            else atom.get("atom_id", "")
        )
        # A successful public write must become injectable in the same
        # operation.  Leaving an accepted atom in ``building`` makes the MCP
        # return success while bootstrap silently omits it forever: there is
        # no background promoter for ordinary memory writes.  Evidence is
        # projected and verified before exposure; a failure stays fail-closed
        # and is retried by the caller's idempotency key.
        if atom_status == "active" and atom_id:
            try:
                projection = memory_store.project_evidence(governance_v2.evidence)
                if int(projection.get("failed", 0)) or int(projection.get("pending", 0)):
                    raise RuntimeError("evidence_projection_incomplete")
                memory_store.set_visibility("active", atom_ids=[atom_id])
            except Exception as exc:
                raise NativePortError("v2_memory_publication_failed") from exc
            if not isinstance(atom, Mapping):
                response["atom"] = replace(atom, visibility="active")
            else:
                response["atom"] = {**atom, "visibility": "active"}
            response["publication"] = {
                "visibility": "active",
                "evidence_projected": int(projection.get("projected", 0)),
            }
        response.setdefault("actions", [])
        response.setdefault("mutation_kind", "created")
        response.setdefault("receipt", None)
        response["deduplicated"] = response.get("mutation_kind") == "deduplicated"
        return response

    def _memory_update(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if "status" in payload and payload.get("_native_gui_state_capability") is not _GUI_STATE_PRESERVATION_CAPABILITY:
            raise NativePortError("memory_lifecycle_governance_required")
        if "related_updates" in payload or "preview" in payload:
            return self._memory_update_related(payload, context)
        scope = self._scope(context)
        with _native_memory_mutation_lock(self.workspace, scope["share_group_id"]):
            governance = self._governance_boundary()
            with governance.atomic_memory() as owner:
                before = self._mandatory_snapshot(context) if owner else []
                result = self._memory_update_unlocked(payload, context, **kwargs)
                if owner:
                    self._validate_mandatory_publication(context, before)
                return result

    def _memory_update_related(self, payload: Mapping[str, Any], context: Mapping[str, Any]) -> Any:
        """Owner-bound recovery through the existing MCP update entrance.

        All revisions are checked before any mutation.  A preview executes the
        real write/publication/validation path and rolls it back.  The request
        receipt is in the same transaction as every related replacement.
        """
        related = payload.get("related_updates", [])
        preview = payload.get("preview", False)
        if type(preview) is not bool or not isinstance(related, list) or len(related) > 19:
            raise NativePortError("invalid_related_updates")
        key = _text(payload.get("idempotency_key"))
        reason = _text(payload.get("reason"))
        if not key or not reason:
            raise NativePortError("related_update_key_and_reason_required")
        fields = {"memory_id", "atom_id", "body", "kind", "injection_policy", "priority", "audience", "expected_revision"}
        if "status" in payload:
            raise NativePortError("invalid_related_updates")
        primary = {name: value for name, value in payload.items() if name in fields}
        updates = [primary] + related
        for item in updates:
            if not isinstance(item, Mapping) or set(item) - fields:
                raise NativePortError("invalid_related_updates")
            if not _text(item.get("memory_id")) or type(item.get("expected_revision")) is not int or item["expected_revision"] < 1:
                raise NativePortError("related_update_revision_required")
        if len({_text(item["memory_id"]) for item in updates}) != len(updates):
            raise NativePortError("duplicate_update_target")

        class PreviewComplete(Exception):
            def __init__(self, result: dict[str, Any]) -> None:
                self.result = result

        governance = self._governance_boundary()
        mutation_context = governance._context(self._mutation_context(context))
        scope = self._scope(context)
        try:
            with _native_memory_mutation_lock(self.workspace, scope["share_group_id"]), governance.atomic_memory():
                current = []
                for item in updates:
                    atom = self._memory_read(item, context)
                    if atom is None:
                        raise NativePortError("memory_not_found")
                    self._require_memory_owner(atom, context)
                    current.append(atom)
                request_key, fingerprint = governance._request_identity(
                    "update_related", {"updates": updates, "reason": reason}, mutation_context, "related:" + key,
                )
                replay, claim = governance._claim_request(mutation_context, "update_related", request_key, fingerprint)
                if replay is not None:
                    return {"committed": True, "idempotent_replay": True, "receipt": replay.to_dict()}
                if any(atom.revision != item["expected_revision"] for atom, item in zip(current, updates)):
                    raise NativePortError("memory_revision_conflict")
                before = self._mandatory_snapshot(context)
                results = []
                for index, item in enumerate(updates):
                    clean = {name: value for name, value in item.items() if name != "expected_revision"}
                    clean["idempotency_key"] = f"related:{key}:{index}"
                    results.append(self._memory_update_unlocked(clean, context))
                budget = self._validate_mandatory_publication(context, before)
                updated = [self._memory_read(item, context) for item in updates]
                receipt = governance._record(
                    "update_related", {"memory_ids": [item["memory_id"] for item in updates]}, mutation_context,
                    {"atoms": [governance._atom_snapshot(atom) for atom in current]},
                    {"atoms": [governance._atom_snapshot(atom) for atom in updated]},
                    reason=reason, confidence=1.0, idempotency_key=request_key,
                    request_fingerprint=fingerprint, claim=claim,
                )
                result = {"committed": not preview, "preview": preview, "budget": budget,
                          "updates": results, "receipt": receipt.to_dict() if not preview else None}
                if preview:
                    # Never return durable-looking per-item receipts for a
                    # transaction intentionally rolled back by preview.
                    result["updates"] = [{"memory_id": atom.memory_id, "revision": atom.revision} for atom in updated]
                    raise PreviewComplete(result)
                return result
        except PreviewComplete as done:
            return done.result
        except NativePortError:
            raise
        except Exception as exc:
            raise self._map_governance_error(exc) from exc

    def _memory_update_unlocked(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        clean = dict(payload)
        memory_id = _text(clean.get("memory_id") or clean.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        existing = self._memory_read({"memory_id": memory_id, "atom_id": clean.get("atom_id", "")}, context)
        if existing is None:
            # Mutation lookup is deliberately existence-neutral: a missing
            # logical/atom ID must not be distinguishable from a record the
            # caller is not allowed to mutate.  Non-admin reads hide foreign
            # rows, so use the same neutral code for both cases.  A trusted
            # admin read can see same-group foreign rows; its owner rejection
            # below uses v2_governance_rejected, so missing admin targets use
            # that same code to preserve neutrality.
            raise NativePortError(
                "v2_governance_rejected"
                if self._trusted_admin(context)
                else "memory_not_found"
            )
        try:
            self._require_memory_owner(existing, context)
        except NativePortError as exc:
            if _text(getattr(exc, "code", "")) == "memory_not_found":
                raise NativePortError("v2_governance_rejected") from exc
            raise
        if (clean.get("_native_gui_state_capability") is not _GUI_STATE_PRESERVATION_CAPABILITY
                or clean.get("_native_gui_state_action") == "edit"):
            self._require_unlinked_memory(existing, context)
        expected_revision = clean.pop("expected_revision", None)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != getattr(existing, "revision", None)
        ):
            raise NativePortError("memory_revision_conflict")
        if hasattr(existing, "to_dict"):
            merged = dict(existing.to_dict())
        elif isinstance(existing, Mapping):
            merged = dict(existing)
        else:
            raise NativePortError("v2_memory_read_invalid")
        # Storage-assigned fields are allowed as compatibility input but the
        # writer will recompute/persist the new revision.  Explicit request
        # fields override the complete prior atom so partial GUI/MCP updates
        # never reset kind/confidence/metadata/scope to constructor defaults.
        merged.update(clean)
        merged["memory_id"] = memory_id
        metadata = getattr(existing, "metadata", {})
        if isinstance(metadata, Mapping):
            owner = _text(metadata.get("owner_agent_id")) or _text(getattr(existing, "agent_instance_id", ""))
            if owner:
                merged["_trusted_owner_agent_id"] = owner
        return self._memory_write(merged, context)

    def _require_unlinked_memory(self, atom: Any, context: Mapping[str, Any]) -> None:
        """Canonical rule mirrors cannot be repaired through a memory update.

        Editing the mirror alone would report success while bootstrap still
        uses the canonical rule's unchanged body. Preserve both stores and
        require the governed GUI operation instead. Read errors fail closed.
        """
        if not self.layout.rules_db.is_file():
            return
        from ..rule_reconciliation import canonical_reconciliation_status

        readiness = canonical_reconciliation_status(self.workspace, context["share_group_id"])
        if readiness.get("failures") == ["rule_intelligence_not_initialized"]:
            return
        rules = self._domain_store("rules")
        memory = self._domain_store("memory")
        source_ids = {atom.memory_id} | {
            _text(row.get("source_record_id"))
            for row in memory.list_source_mappings(atom_id=atom.atom_id)
        }
        links = rules.list_source_links(share_group_id=context["share_group_id"], status="active")
        if any(_text(self._row_value(link, "memory_id")) in source_ids for link in links):
            raise NativePortError("canonical_rule_governance_required")

    def _memory_delete(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        idempotency_key = _text(payload.get("idempotency_key"))
        if not idempotency_key:
            raise NativePortError("idempotency_key_required")
        reason = _text(payload.get("reason")) or "native memory delete"
        existing = self._memory_read({"memory_id": memory_id}, context)
        if existing is None:
            raise NativePortError("memory_not_found")
        self._require_memory_owner(existing, context)
        governance = self._governance_boundary()
        try:
            persisted, receipt = governance.tombstone(
                memory_id,
                context=self._mutation_context(context),
                reason=reason,
                confidence=payload.get("confidence", 1.0),
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise self._map_governance_error(exc) from exc
        return {"atom": persisted, "receipt": receipt}

    def _gui_memory_key(self, action: str, payload: Mapping[str, Any], context: Mapping[str, Any]) -> str:
        del context
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        supplied = _text(payload.get("idempotency_key"))
        if supplied:
            return "gui:" + supplied
        fingerprint = {
            key: value for key, value in payload.items()
            if key not in {"idempotency_key", "actor", "admin", "is_admin"}
        }
        return "gui:" + hashlib.sha256(
            _canonical_json({"action": action, "memory_id": memory_id, "payload": fingerprint}).encode("utf-8")
        ).hexdigest()

    def _gui_memory_update(self, action: str, payload: Mapping[str, Any], context: Mapping[str, Any]) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        clean: dict[str, Any] = {"memory_id": _text(payload.get("memory_id") or payload.get("id"))}
        if not clean["memory_id"]:
            raise NativePortError("memory_id_required")
        if action == "edit":
            if "body" not in payload or not isinstance(payload.get("body"), str):
                raise NativePortError("memory_body_required")
            body = str(payload.get("body") or "")
            try:
                from ..auto_organizer import SECRET_PATTERNS
                for pattern in SECRET_PATTERNS:
                    body = pattern.sub("[REDACTED]", body)
            except Exception:
                pass
            clean["body"] = body
        elif action == "lock":
            clean["locked"] = True
        elif action == "unlock":
            clean["locked"] = False
        elif action == "policy":
            policy = _text(payload.get("injection_policy"))
            if policy not in {"relevant", "always"}:
                raise NativePortError("invalid_injection_policy")
            clean["injection_policy"] = policy
            raw_priority = payload.get("priority", 0)
            if isinstance(raw_priority, bool):
                raise NativePortError("invalid_priority")
            try:
                priority = int(raw_priority)
            except (TypeError, ValueError) as exc:
                raise NativePortError("invalid_priority") from exc
            if not -100 <= priority <= 100:
                raise NativePortError("invalid_priority")
            clean["priority"] = priority
        elif action == "restore":
            clean["status"] = "active"
        else:
            raise NativePortError("unknown_memory_mutation")
        clean["reason"] = f"native GUI memory {action}"
        clean["decision_confidence"] = 1.0
        clean["_native_gui_state_action"] = action
        clean["idempotency_key"] = self._gui_memory_key(
            action,
            {**clean, "idempotency_key": payload.get("idempotency_key", "")},
            context,
        )
        clean["_native_gui_state_capability"] = _GUI_STATE_PRESERVATION_CAPABILITY
        return self._memory_update(clean, context)

    def _gui_memory_edit(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("edit", payload, context)

    def _gui_memory_lock(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("lock", payload, context)

    def _gui_memory_unlock(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("unlock", payload, context)

    def _gui_memory_policy(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("policy", payload, context)

    def _gui_memory_restore(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        reason = _text(payload.get("reason")) or "native GUI memory restore"
        idempotency_key = self._gui_memory_key(
            "restore",
            {"memory_id": memory_id, "idempotency_key": payload.get("idempotency_key", "")},
            context,
        )
        governance = self._governance_boundary()
        try:
            persisted, receipt = governance.restore(
                memory_id,
                context=self._mutation_context(context),
                reason=reason,
                confidence=payload.get("confidence", 1.0),
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise self._map_governance_error(exc) from exc
        return {"atom": persisted, "receipt": receipt}

    def _gui_memory_delete(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        clean = {"memory_id": _text(payload.get("memory_id") or payload.get("id"))}
        if not clean["memory_id"]:
            raise NativePortError("memory_id_required")
        clean["reason"] = "native GUI memory delete"
        clean["confidence"] = 1.0
        clean["idempotency_key"] = self._gui_memory_key(
            "delete",
            {**clean, "idempotency_key": payload.get("idempotency_key", "")},
            context,
        )
        return self._memory_delete(clean, context)

    def _gui_memory_rollback(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        version_id = _text(payload.get("version_id") or payload.get("revision_id"))
        if not version_id:
            raise NativePortError("version_id_required")
        scope = self._scope(context)
        store = self._domain_store("memory", write=True)
        try:
            with open_database(self.layout.memory_db, readonly=True) as conn:
                clauses = ["r.revision_id=?", "a.workspace_id=?", "a.share_group_id=?"]
                params: list[Any] = [version_id, self.workspace, scope.get("share_group_id", "")]
                for column, value in (
                    ("a.agent_instance_id", scope.get("agent_instance_id", "")),
                    ("a.project_ref", scope.get("project_ref", "")),
                    ("a.provider", scope.get("provider", "")),
                    ("a.runtime_role", scope.get("runtime_role", "")),
                ):
                    if value:
                        clauses.append(column + "=?")
                        params.append(value)
                row = conn.execute(
                    "SELECT r.atom_id,r.revision,a.memory_id,a.revision AS current_revision "
                    "FROM atom_revisions r JOIN atoms a ON a.atom_id=r.atom_id WHERE " + " AND ".join(clauses),
                    params,
                ).fetchone()
            if row is None:
                raise NativePortError("revision_not_found")
            restored = store.replay_revision(str(row[0]), int(row[1]))
            if restored is None:
                raise NativePortError("revision_not_found")
            governance = self._governance_boundary()
            persisted, receipt = governance.put_atom(
                restored,
                context=self._mutation_context(context),
                reason="native GUI memory revision rollback",
                confidence=1.0,
                idempotency_key="gui:rollback:" + hashlib.sha256(
                    f"{version_id}\0{int(row[3])}".encode("utf-8")
                ).hexdigest(),
            )
            return {"atom": persisted, "receipt": receipt, "version_id": version_id}
        except NativePortError:
            raise
        except Exception as exc:
            raise self._map_governance_error(exc) from exc

    def _context_bootstrap(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        engine = self.context_engine
        if engine is None:
            raise NativePortError("v2_context_engine_unavailable")
        # ``V2RuntimeFacade.bootstrap_hook`` uses the generic dispatch shape
        # ``{"request": event, "payload": request}``; direct callers pass a
        # request mapping as the first argument.  Normalize both without
        # treating an event label as a context request.
        nested_request = payload.get("request")
        nested_payload = payload.get("payload")
        if isinstance(nested_request, Mapping):
            request = dict(nested_request)
        elif isinstance(nested_payload, Mapping):
            request = dict(nested_payload)
        else:
            request = dict(payload)
        for key, value in payload.items():
            if key not in {"request", "payload"}:
                request.setdefault(key, value)
        request["trusted_identity"] = dict(context.get("trusted_identity") or {})
        request.update({
            "workspace_id": context.get("workspace_id", "") or self.workspace,
            "agent_instance_id": context.get("agent_instance_id", ""),
            "project_ref": context.get("project_ref", ""),
            "share_group_id": context.get("share_group_id", ""),
            "provider": context.get("provider", ""),
            "runtime_role": context.get("runtime_role", ""),
            "namespace_id": context.get("namespace_id", ""),
            "sensitivity": context.get("sensitivity", ""),
            "policy_class": context.get("policy_class", ""),
        })
        candidates = request.pop("candidates", None)
        # Keep the engine's readiness/state projection synchronized with the
        # same trusted manifest provider that gates this native dispatch.  The
        # engine remains usable in shadow mode while V2 is building, but it
        # must never claim active readiness from a constructor default.
        if self.state_provider is not None:
            try:
                current_state, _current_generation = self._provider_snapshot(self.state_provider)
                if hasattr(engine, "state"):
                    engine.state = _text(current_state).upper()
                if hasattr(engine, "ready"):
                    engine.ready = _text(current_state).upper() == "V2_ACTIVE"
            except Exception:
                if hasattr(engine, "state"):
                    engine.state = "UNKNOWN"
                if hasattr(engine, "ready"):
                    engine.ready = False
        fn = getattr(engine, "bootstrap", None) or getattr(engine, "build_context", None)
        if not callable(fn):
            raise NativePortError("v2_context_engine_unavailable")
        packet = fn(request, candidates)
        payload = packet.to_dict() if hasattr(packet, "to_dict") else dict(packet)
        if payload.get("status") == "ok":
            from ..codegraph_v2.automation import agent_notice
            payload["codegraph"] = agent_notice(self.workspace, dict(context),
                start=bool(getattr(engine, "ready", False)) and getattr(engine, "state", "") == "V2_ACTIVE")
        try:
            from ..context_bootstrap import consume_codegraph_affected_receipt

            # The receipt is offered to ContextEngine as a reference-only
            # candidate.  Consume only when the active packet reports that
            # exact candidate as a successful hit; budget rejection, blocked
            # bootstrap, shadow state, or re-entry must leave it pending.
            receipt_id = ""
            if str(payload.get("status") or "") == "ok":
                for item in payload.get("receipts") or ():
                    if not isinstance(item, Mapping) or item.get("hit") is not True:
                        continue
                    if str(item.get("layer") or "") != "reference_only":
                        continue
                    item_id = _text(item.get("item_id"))
                    marker = "codegraph:affected:"
                    if marker in item_id:
                        receipt_id = item_id.rsplit(marker, 1)[-1]
                        break
            if receipt_id:
                receipt = consume_codegraph_affected_receipt(
                    self.workspace,
                    scope=self._codegraph_scope(context),
                    receipt_id=receipt_id,
                )
                if receipt:
                    payload["codegraph_affected"] = receipt
        except Exception:
            pass
        return payload

    def _retrieve_v2_rules(
        self,
        *,
        group: str,
        scope_public: Mapping[str, Any],
        result: dict[str, Any],
        catalog: dict[Any, Any] | None = None,
        rules_store: Any = None,
    ) -> dict[str, set[str]]:
        """Read V2 rules and return source records represented by layer.

        A migrated ``always`` memory can remain in the memory domain while its
        canonical rule is also available from the rules domain.  Once that
        rule actually matches the current trusted scope, the canonical/compat
        rule is the runtime representation for that source record; callers use
        the returned IDs to suppress the memory shadow so one governed source
        consumes context budget only once.
        """

        from ..rule_reconciliation import canonical_reconciliation_status

        # Publication checks many audience partitions against the same catalog.
        # Only the caller's current request may reuse these reads; scope matching
        # below still runs independently for every partition.
        if catalog is None:
            catalog = {}

        def cached(key: tuple, read_value: Any) -> Any:
            scoped_key = (str(self.workspace), group, *key)
            if scoped_key not in catalog:
                catalog[scoped_key] = read_value()
            return catalog[scoped_key]

        readiness = cached(("readiness",), lambda: canonical_reconciliation_status(self.workspace, group, store=rules_store))
        failures = readiness.get("failures") or []
        # ``initialize_all`` creates the Phase-1 rules placeholder before the
        # optional V2 rule-intelligence schema is installed.  That is a valid
        # no-rules state; do not construct RuleV2Store against the placeholder
        # (it has no rules_schema_meta) and turn ordinary memory retrieval into
        # context_build_failed.  Once the V2 schema exists, all read errors
        # remain fail-closed below.
        if failures == ["rule_intelligence_not_initialized"]:
            return {"mandatory": set(), "relevant": set()}
        if failures == ["native_canonical_status_unavailable"]:
            raise RuntimeError("canonical status unavailable")
        canonical_ready = bool(readiness.get("canonical_ready"))
        rules = rules_store if rules_store is not None else self._domain_store("rules")
        definitions = cached(("definitions",), lambda: rules.list_definitions(status="active"))

        def canonical_id(value: Any) -> str:
            return cached(("head", _text(value)), lambda: self._canonical_rule_id(rules, value))
        represented_source_ids: dict[str, set[str]] = {
            "mandatory": set(),
            "relevant": set(),
        }
        links_by_definition: dict[str, list[dict[str, Any]]] = {}
        read = getattr(rules, "_read", None)
        if callable(read):
            source_links = cached(("links",), lambda: read(lambda conn: [
                dict(row) for row in conn.execute(
                    "SELECT * FROM rule_source_links "
                    "WHERE share_group_id=? AND status='active' "
                    "ORDER BY source_link_id",
                    (group,),
                ).fetchall()
            ]))
            for link in source_links:
                definition_id = canonical_id(link.get("canonical_definition_id"))
                if definition_id:
                    links_by_definition.setdefault(definition_id, []).append(link)
        for definition in definitions:
            if canonical_id(definition.definition_id) != definition.definition_id:
                continue
            bindings = cached(("bindings", definition.definition_id), lambda: rules.list_bindings(
                definition_id=definition.definition_id,
                share_group_id=group,
                status="active",
            ))
            includes = [
                item for item in bindings
                if _text(getattr(item, "effect", "include")).casefold() != "exclude"
            ]
            excludes = [
                item for item in bindings
                if _text(getattr(item, "effect", "include")).casefold() == "exclude"
            ]
            matched = [item for item in includes if self._binding_matches_context(item, scope_public)]
            if not matched:
                continue
            if any(self._binding_matches_context(item, scope_public) for item in excludes):
                continue

            source = "native-v2-rule-compat"
            source_ref = f"v2:rule:{_text(definition.definition_id)}"
            evidence_ref = source_ref
            evidence_digest = _text(getattr(definition, "semantic_hash", ""))
            matched_source_links = links_by_definition.get(
                _text(definition.definition_id), []
            )
            if canonical_ready:
                source = "native-v2-rule"
                if not callable(read):
                    raise RuntimeError("native rule provenance unavailable")
                link = matched_source_links[0] if matched_source_links else {}
                memory_id = _text(link.get("memory_id"))
                if not memory_id:
                    raise RuntimeError("native rule source link unavailable")
                evidence_rows = cached(("evidence", definition.definition_id, memory_id), lambda: read(lambda conn: [
                    dict(row) for row in conn.execute(
                        "SELECT * FROM rule_evidence_refs "
                        "WHERE share_group_id=? AND definition_id=? "
                        "AND source_rule_id=? ORDER BY evidence_id",
                        (group, definition.definition_id, memory_id),
                    ).fetchall()
                ]))
                evidence = evidence_rows[0] if evidence_rows else {}
                evidence_ref = _text(evidence.get("evidence_ref")) or _text(evidence.get("evidence_id"))
                if not evidence_ref:
                    raise RuntimeError("native rule evidence unavailable")
                source_ref = _text(link.get("source_ref")) or memory_id
                evidence_digest = _text(evidence.get("content_digest")) or evidence_digest

            layer = (
                "mandatory"
                if _text(definition.rule_strength).casefold() == "must"
                or _text(definition.maturity_state).casefold() == "trusted"
                else "relevant"
            )
            represented_source_ids[layer].update(
                _text(link.get("memory_id"))
                for link in matched_source_links
                if _text(link.get("memory_id"))
            )
            result[layer].append({
                "item_id": _text(definition.definition_id),
                "body": _text(definition.canonical_text),
                "kind": _text(definition.rule_kind) or "rule",
                "layer": layer,
                "source": source,
                "source_ref": source_ref,
                "evidence": {"id": evidence_ref, "digest": evidence_digest},
                "scope": dict(scope_public),
                "priority": max((int(getattr(item, "priority", 0) or 0) for item in matched), default=0),
                "score": float(getattr(definition, "confidence", 0.0) or 0.0),
                "is_rule": True,
                "rule_strength": _text(getattr(definition, "rule_strength", "")),
                "status": _text(definition.status) or "active",
            })
        return represented_source_ids

    def _reference_candidates(
        self,
        *,
        request: Any,
        agent: str,
        scope: Any,
        group: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Read optional V2 reference channels independently.

        Knowledge, history and CodeGraph are optional context enrichments. A
        failure in one channel must not discard native memory/rule candidates
        or hide which channel was unavailable. The adapters return only safe
        reference fields; this method adds bounded source identity for the
        ContextEngine dedup/receipt contract.
        """

        from ..context_bootstrap import (
            codegraph_reference_candidates,
            history_reference_candidates,
            knowledge_reference_candidates,
        )

        namespace_id = _text(getattr(request, "namespace_id", ""))
        sensitivity = _text(getattr(request, "sensitivity", ""))
        policy_class = _text(getattr(request, "policy_class", ""))
        project = _text(scope.project_ref)
        provider = _text(scope.provider)
        runtime_role = _text(scope.runtime_role)
        query = _text(getattr(request, "task", ""))
        limit = max(1, min(int(getattr(request, "max_items", None) or 6), 20))
        calls: tuple[tuple[str, Callable[[], Any]], ...] = (
            (
                "knowledge",
                lambda: knowledge_reference_candidates(
                    self.workspace,
                    namespace_id=namespace_id,
                    workspace_id=self.workspace,
                    agent_instance_id=agent,
                    project_ref=project,
                    provider=provider,
                    share_group_id=group,
                    sensitivity=sensitivity,
                    policy_class=policy_class,
                    query=query,
                    limit=limit,
                ),
            ),
            (
                "history",
                lambda: history_reference_candidates(
                    self.workspace,
                    agent_instance_id=agent,
                    project_ref=project,
                    provider=provider,
                    share_group_id=group,
                    query=query,
                    limit=limit,
                ),
            ),
            (
                "codegraph",
                lambda: codegraph_reference_candidates(
                    self.workspace,
                    agent_instance_id=agent,
                    project_ref=project,
                    provider=provider,
                    share_group_id=group,
                    runtime_role=runtime_role,
                    query=query,
                    limit=min(limit, 4),
                ),
            ),
        )
        references: list[dict[str, Any]] = []
        omissions: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for channel, call in calls:
            try:
                values = call()
            except Exception:
                omissions.append({
                    "layer": "reference_only",
                    "reason": f"{channel}_source_unavailable",
                })
                continue
            if not isinstance(values, (list, tuple)):
                omissions.append({
                    "layer": "reference_only",
                    "reason": f"{channel}_source_unavailable",
                })
                continue
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                summary = _text(value.get("summary"))[:1200]
                reference = _text(value.get("ref"))[:512]
                digest = _text(value.get("hash"))[:256]
                if not summary and not reference and not digest:
                    continue
                identity = (summary, reference, digest)
                if identity in seen:
                    continue
                seen.add(identity)
                references.append({
                    "summary": summary,
                    "ref": reference,
                    "hash": digest,
                    "trust": "reference_only",
                    "source": f"native-v2-{channel}",
                    "id": f"{channel}:{reference or digest}",
                    "priority": (
                        int(value.get("priority"))
                        if isinstance(value.get("priority"), int)
                        and not isinstance(value.get("priority"), bool)
                        else 0
                    ),
                })
        return references, omissions

    def retrieve(self, request: Any) -> dict[str, Any]:
        """Retrieve bounded candidates from native V2 memory/rule stores.

        This is the default :class:`ContextEngine` retrieval port.  It only
        reads the exact trusted group/Agent/project scope and returns an empty
        neutral result when V2 storage is absent or not yet ready; no legacy
        bootstrap or conversation source is consulted.
        """
        from .context_engine import ContextRequest
        from ..memory.store import MemoryReadScope

        req = ContextRequest.from_mapping(request)
        group = _text(req.share_group_id or req.group)
        agent = _text(req.agent_instance_id or req.agent)
        if not group or not agent:
            return {
                "mandatory": [], "relevant": [], "knowledge": [],
                "reference_only": [],
                "omissions": [{"layer": "reference_only", "reason": "scope_omitted"}],
            }
        scope = MemoryReadScope(
            workspace_id=self.workspace,
            share_group_id=group,
            agent_instance_id=agent,
            project_ref=_text(req.project_ref or req.project),
            provider=_text(req.provider),
            runtime_role=_text(req.runtime_role or req.runtime),
        )
        result: dict[str, Any] = {
            "mandatory": [], "relevant": [], "knowledge": [], "reference_only": [],
            "omissions": [],
        }
        memory_source_ids: dict[str, set[str]] = {}
        scope_public = {
            "workspace_id": self.workspace,
            "agent_instance_id": agent,
            "share_group_id": group,
            "project_ref": scope.project_ref,
            "provider": scope.provider,
            "runtime_role": scope.runtime_role,
        }
        try:
            if self.layout.memory_db.is_file():
                memory = self._domain_store("memory")
                atoms = memory.list_atoms(
                    scope=scope,
                    status="active",
                    include_building=False,
                )
                atoms = self._canonical_memory_rows(
                    atoms, scope_public, suppress_rule_sources=False,
                )
                mandatory_atoms = [
                    atom for atom in atoms
                    if _text(getattr(atom, "injection_policy", "relevant")).casefold() == "always"
                ]
                relevant_atoms: list[Any] = []
                memory_query = " ".join(
                    value for value in (
                        _text(getattr(req, "task", "")),
                        _text(getattr(req, "project_hint", "")),
                    )
                    if value
                )
                max_items = getattr(req, "max_items", None)
                if max_items != 0 and memory_query:
                    try:
                        relevant_atoms = memory.search(
                            memory_query,
                            scope=scope,
                            status="active",
                            limit=max_items,
                        )
                        if not isinstance(relevant_atoms, (list, tuple)):
                            relevant_atoms = []
                            result["omissions"].append({
                                "layer": "relevant",
                                "reason": "retrieval_omitted",
                            })
                    except Exception:
                        relevant_atoms = []
                        result["omissions"].append({
                            "layer": "relevant",
                            "reason": "retrieval_omitted",
                        })
                else:
                    result["omissions"].append({
                        "layer": "relevant",
                        "reason": "retrieval_omitted",
                    })
                atoms = list(mandatory_atoms)
                seen_atom_ids = {
                    _text(getattr(atom, "atom_id", ""))
                    for atom in atoms
                    if _text(getattr(atom, "atom_id", ""))
                }
                for atom in relevant_atoms:
                    policy = _text(getattr(atom, "injection_policy", "relevant")).casefold()
                    atom_id = _text(getattr(atom, "atom_id", ""))
                    if policy == "always" or (atom_id and atom_id in seen_atom_ids):
                        continue
                    atoms.append(atom)
                    if atom_id:
                        seen_atom_ids.add(atom_id)
                for atom in atoms:
                    policy = _text(getattr(atom, "injection_policy", "relevant")).casefold()
                    layer = "mandatory" if policy == "always" else "relevant"
                    kind = _text(getattr(atom, "kind", "fact")) or "fact"
                    storage_atom_id = _text(getattr(atom, "atom_id", "")) or _text(getattr(atom, "memory_id", ""))
                    memory_id = _text(getattr(atom, "memory_id", "")) or storage_atom_id
                    evidence_ids = list(getattr(memory, "evidence_ids_for_atom", lambda *_: [])(storage_atom_id) or [])
                    mappings = list(getattr(memory, "list_source_mappings", lambda **_: [])(atom_id=storage_atom_id) or [])
                    mapping = mappings[0] if mappings else {}
                    memory_source_ids[memory_id] = {memory_id} | {
                        _text(item.get("source_record_id"))
                        for item in mappings
                        if _text(item.get("source_record_id"))
                    }
                    source_ref = _text(mapping.get("source_ref")) or memory_id
                    evidence_ref = _text(evidence_ids[0] if evidence_ids else "") or source_ref
                    result[layer].append({
                        "item_id": memory_id, "memory_id": memory_id,
                        "body": _text(getattr(atom, "body", "")),
                        "kind": kind, "layer": layer, "source": "native-v2-memory",
                        "source_ref": source_ref,
                        "evidence": {"id": evidence_ref, "digest": _text(getattr(atom, "canonical_hash", ""))},
                        "scope": dict(scope_public),
                        "priority": int(getattr(atom, "priority", 0) or 0),
                        "score": float(getattr(atom, "confidence", 0.0) or 0.0),
                        "is_rule": layer == "mandatory" or kind.casefold() in {"rule", "procedure", "instruction"},
                        "status": _text(getattr(atom, "status", "active")) or "active",
                    })
            if self.layout.rules_db.is_file():
                represented_sources = self._retrieve_v2_rules(
                    group=group,
                    scope_public=scope_public,
                    result=result,
                )
                if any(represented_sources.values()):
                    for layer in ("mandatory", "relevant"):
                        replacement_sources = set(represented_sources["mandatory"])
                        if layer == "relevant":
                            replacement_sources.update(represented_sources["relevant"])
                        result[layer] = [
                            item for item in result[layer]
                            if item.get("source") != "native-v2-memory"
                            or not (
                                memory_source_ids.get(_text(item.get("item_id")), set())
                                & replacement_sources
                            )
                        ]
            # Optional reference channels fail independently. Their bounded
            # diagnostics remain visible to ContextEngine; native memory and
            # canonical/compatibility rule candidates stay intact.
            references, omissions = self._reference_candidates(
                request=req,
                agent=agent,
                scope=scope,
                group=group,
            )
            result["reference_only"].extend(references)
            result["omissions"].extend(omissions)
        except Exception as exc:
            raise RuntimeError("native_v2_retrieval_failed") from exc
        return result

    def _rule_decision_read(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Decisions are persisted without a share-group scope in the current
        # V2 schema.  Do not turn this neutral-read surface into a global
        # history/count oracle; callers receive only fixed availability until
        # a scoped decision service is provided.
        del payload, context
        return {"available": True}

    def _rule_scope_stats(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Rule definitions are global canonical rows and the store's fallback
        # stats API has no complete audience filter.  Fixed availability is
        # the fail-closed neutral response until a scoped stats service exists.
        del payload, context
        return {"available": True}

    def _binding_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._stores.get("rules") or self._stores.get("governance")
        if service is None:
            service = self._domain_store("rules")
        fn = getattr(service, "list_bindings", None)
        if not callable(fn):
            raise NativePortError("v2_binding_service_unavailable")
        return fn(
            definition_id=_text(payload.get("definition_id")) or None,
            share_group_id=_text(payload.get("share_group_id")) or context.get("share_group_id"),
            status=_text(payload.get("status")) or None,
        )

    def _binding_create(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        # Rule stores injected through the explicit test capability are
        # read-only seams.  Binding creation must always resolve the builtin
        # writer after its schema preflight.
        service = self._domain_store("rules", write=True)
        fn = getattr(service, "upsert_binding", None)
        if not callable(fn):
            raise NativePortError("v2_binding_service_unavailable")
        clean = dict(payload)
        # Drop all caller-controlled authorization/ownership aliases before
        # deriving the canonical audit fields below.  Unknown fields are not
        # harmless: custom Rules services may persist them or treat them as
        # policy inputs even when the built-in store ignores them.
        for key in (
            "created_by", "owner", "owner_id", "owner_agent", "owner_agent_id",
            "authorization", "authority", "admin", "is_admin",
        ):
            clean.pop(key, None)
        clean.update(self._scope(context))
        # Binding governance fields are derived from the trusted admin
        # capability.  A request body must not be able to self-identify as a
        # manual/admin actor or assign ownership to another agent.
        clean["created_by"] = "admin"
        clean["owner_agent_id"] = context.get("agent_instance_id", "")
        clean["authorization"] = f"native-admin:{context.get('session_id', '')}"
        return fn(clean)

    def _task_service(self) -> Any:
        if self._task_coordinator is None:
            try:
                from .task_coordinator import TaskCoordinator

                self._task_coordinator = TaskCoordinator(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_task_service_unavailable") from exc
        return self._task_coordinator

    def _knowledge_command(self) -> Any:
        if self._knowledge_command_service is None:
            try:
                from ..knowledge_v2.command import KnowledgeV2CommandService

                self._knowledge_command_service = KnowledgeV2CommandService(
                    self.workspace,
                    tasks=self._task_service(),
                )
            except Exception as exc:
                raise NativePortError("v2_knowledge_command_unavailable") from exc
        return self._knowledge_command_service

    def _projection_service(self) -> Any:
        if self._projection_build_service is None:
            try:
                from .projection_build import ProjectionBuildService

                self._projection_build_service = ProjectionBuildService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_projection_build_service_unavailable") from exc
        return self._projection_build_service

    def _build_projection_worker(
        self,
        scope: Any,
        mode: str,
        runtime_role: str,
        context: Mapping[str, Any],
        *,
        engine: Mapping[str, Any] | None,
        deterministic: bool,
        execution: Any,
    ) -> Mapping[str, Any]:
        """Run one governed projection build inside a TaskCoordinator worker.

        A selected, real Agent CLI participates through the existing governed
        enrichment path (stage -> list_pending -> batch_enrich_via_cli ->
        apply_enrichments) before the canonical projection build.  Deterministic
        mode skips enrichment and truthfully reports ``llm_used=False``; an Agent
        CLI that fails to enrich fails the task rather than silently claiming
        LLM organization.
        """
        from ..host_agent_backend import batch_enrich_via_cli
        from .projection_build import ProjectionBuildError, ProjectionBuildService

        # Gate the task before any optional Agent enrichment.  An empty source
        # set is a failed build input, never a successful empty projection.
        source_summary = dict(
            ProjectionBuildService(self.workspace).source_map(scope=scope).get("summary") or {}
        )
        if int(source_summary.get("buildable_atom_count") or 0) <= 0:
            raise ProjectionBuildError("no_projection_sources")

        llm_used = False
        llm_engine = ""
        if not deterministic and engine is not None:
            execution.progress(2, "engine")
            enrichment = self._native_service("extraction")
            if enrichment is None:
                raise ProjectionBuildError(
                    self._native_service_init_errors.get("extraction")
                    or "v2_extraction_service_unavailable"
                )

            # The desktop transport is deliberately bound to the server-admin
            # bridge, while ``scope`` is the persisted business selection.  A
            # real extraction service must therefore use the already-validated
            # projection scope directly; dispatching with the bridge context
            # would stage/enrich the admin group (usually zero records).  Test
            # doubles and older injected services retain the dispatch fallback.
            build_for_scope = getattr(enrichment, "build_and_enrich_projection", None)
            pending_for_scope = getattr(enrichment, "list_pending_projection", None)
            apply_for_scope = getattr(enrichment, "apply_enrichments_projection", None)
            scoped_enrichment = all(callable(item) for item in (build_for_scope, pending_for_scope, apply_for_scope))

            def enrichment_result(operation: str, payload: Mapping[str, Any]) -> Any:
                if scoped_enrichment:
                    if operation == "memoryguard_build_and_enrich":
                        return build_for_scope(payload, scope=scope)
                    if operation == "memoryguard_list_pending_enrichments":
                        return pending_for_scope(payload, scope=scope)
                    if operation == "memoryguard_apply_enrichments":
                        return apply_for_scope(payload, scope=scope)
                return self._service_result(enrichment, operation, payload, context=context)

            enrichment_result("memoryguard_build_and_enrich", {})
            enriched_count = 0
            while True:
                execution.check_cancelled()
                pending = enrichment_result("memoryguard_list_pending_enrichments", {"limit": 100})
                tasks = list((pending or {}).get("tasks") or [])
                if not tasks:
                    break
                execution.progress(6, "enrich", item_count=enriched_count + len(tasks))
                results = batch_enrich_via_cli(
                    tasks,
                    agent=str(engine["agent"]),
                    cli_path=str(engine["cli"]),
                    workspace=self.workspace,
                    execution=execution,
                )
                if not results:
                    raise ProjectionBuildError("llm_cli_enrichment_failed")
                expected_ids = [str(item.get("task_id") or "") for item in tasks]
                actual_ids = [str(item.get("task_id") or "") for item in results]
                if (
                    not all(expected_ids)
                    or len(set(expected_ids)) != len(expected_ids)
                    or len(actual_ids) != len(expected_ids)
                    or set(actual_ids) != set(expected_ids)
                ):
                    raise ProjectionBuildError("llm_cli_enrichment_incomplete")
                applied = enrichment_result("memoryguard_apply_enrichments", {"results": results})
                if int((applied or {}).get("applied") or 0) != len(tasks):
                    raise ProjectionBuildError("llm_cli_enrichment_failed")
                llm_used = True
                llm_engine = str(engine["agent"])
                enriched_count += len(tasks)
            # Re-evaluate after all batches.  This proves the governed queue is
            # terminal before projection rather than assuming one page was all.
            refreshed = enrichment_result("memoryguard_build_and_enrich", {})
            if list((refreshed or {}).get("pending_tasks") or []):
                raise ProjectionBuildError("llm_cli_enrichment_incomplete")
        else:
            execution.progress(2, "engine")

        return ProjectionBuildService(self.workspace).build(
            scope=scope,
            mode=mode,
            runtime_role=runtime_role,
            llm_provider=llm_engine or "deterministic",
            llm_used=llm_used,
            llm_engine=llm_engine,
            execution=execution,
        )

    def _release_v2_service(self) -> Any:
        if self._release_service is None:
            try:
                from .projection_build import V2ReleaseService

                self._release_service = V2ReleaseService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_release_service_unavailable") from exc
        return self._release_service

    def _group_service(self, *, write: bool = False) -> Any:
        if self._group_control_service is None or write:
            try:
                from .group_native import GroupControlService

                service = GroupControlService(self.workspace, write=bool(write))
                if self._group_control_service is None:
                    self._group_control_service = service
                return service
            except Exception as exc:
                raise NativePortError("v2_group_control_unavailable") from exc
        return self._group_control_service

    def _agent_service(self) -> Any:
        if self._agent_native_service is None:
            try:
                from .agent_native import AgentNativeService

                self._agent_native_service = AgentNativeService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_agent_service_unavailable") from exc
        return self._agent_native_service

    def _governance_native(self) -> Any:
        if self._governance_native_service is None:
            try:
                from .governance_native import GovernanceNativeService

                self._governance_native_service = GovernanceNativeService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_governance_service_unavailable") from exc
        return self._governance_native_service

    def _hook_control(self) -> Any:
        if self._hook_control_service is None:
            try:
                from .hook_control import HookControlService

                self._hook_control_service = HookControlService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_hook_control_unavailable") from exc
        return self._hook_control_service

    def _import_control(self) -> Any:
        if self._import_control_service is None:
            try:
                from .import_control import ImportControlService

                self._import_control_service = ImportControlService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_import_control_unavailable") from exc
        return self._import_control_service

    def _history_control(self) -> Any:
        if self._history_control_service is None:
            try:
                from .history_control import HistoryControlService

                self._history_control_service = HistoryControlService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_history_control_unavailable") from exc
        return self._history_control_service

    def _audit_plan(self) -> Any:
        if self._audit_plan_service is None:
            try:
                from .audit_plan import AuditPlanService

                self._audit_plan_service = AuditPlanService(self.workspace)
            except Exception as exc:
                raise NativePortError("v2_audit_plan_unavailable") from exc
        return self._audit_plan_service

    def _gui_projection_scope(self, context: Mapping[str, Any]) -> Any:
        authority = self._gui_authority(context)
        from .projection_build import projection_scope_from_context

        # The desktop bridge deliberately remains the server-admin principal.
        # Its transport identity is an ACL capability, not the Agent whose
        # business scope the GUI selected.  Resolve that selection from the
        # persisted control plane and derive a read scope from the validated
        # record; never promote browser selectors or mutate the bound context.
        is_server_admin_bridge = bool(authority.admin) and _text(authority.agent_instance_id) == "memoryguard-server-admin"
        if is_server_admin_bridge:
            from ..projection_v2 import ProjectionReadScope

            try:
                state = self._group_service(write=False).scope_state(
                    _text(authority.agent_instance_id),
                    admin=True,
                )
            except Exception as exc:
                raise NativePortError("governance_scope_unavailable") from exc
            if not isinstance(state, Mapping) or state.get("empty") is not False:
                raise NativePortError("governance_scope_empty")
            persisted = state.get("scope")
            if not isinstance(persisted, Mapping):
                raise NativePortError("governance_scope_invalid")
            mode = _text(persisted.get("mode")).casefold()
            group_id = _text(persisted.get("share_group_id"))
            if not group_id:
                raise NativePortError("governance_scope_invalid")
            if mode == "agent":
                agent_id = _text(persisted.get("agent_instance_id"))
                if not agent_id:
                    raise NativePortError("governance_scope_invalid")
                # An Agent bound to a shared group consumes the group's one
                # canonical memory plane.  ``agent_instance_id`` on an atom is
                # writer/provenance metadata, not an audience partition.  If
                # we keep the selected Agent as a read filter here, a shared
                # group appears empty whenever another member wrote the active
                # records.  Personal groups remain Agent-scoped.
                active_binding = state.get("active_binding")
                if (
                    isinstance(active_binding, Mapping)
                    and _text(active_binding.get("group_kind")).casefold() == "shared"
                ):
                    agent_id = ""
            elif mode == "share_group":
                # Blank agent/project/provider fields intentionally mean all
                # active members and all their V2 atoms within this group.
                agent_id = ""
            else:
                raise NativePortError("governance_scope_invalid")
            try:
                return ProjectionReadScope(
                    workspace_id=self.workspace,
                    agent_instance_id=agent_id,
                    project_ref="",
                    provider="",
                    share_group_id=group_id,
                    sensitivity=_text(authority.sensitivity) or "normal",
                    policy_class=_text(authority.policy_class) or "private",
                )
            except (TypeError, ValueError) as exc:
                raise NativePortError("governance_scope_invalid") from exc

        try:
            return projection_scope_from_context(self.workspace, authority.to_dict())
        except Exception as exc:
            code = _text(getattr(exc, "code", "")) or "projection_scope_required"
            raise NativePortError(code) from exc

    def _gui_knowledge_scope(self, context: Mapping[str, Any]) -> Any:
        """Create exact Knowledge ACL scope from the process-issued GUI capability."""
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc
        values = {
            "namespace_id": _text(authority.namespace_id),
            "workspace_id": self.workspace,
            "agent_instance_id": _text(authority.agent_instance_id),
            "project_ref": _text(authority.project_ref),
            "provider": _text(authority.provider),
            "share_group_id": _text(authority.share_group_id),
            "sensitivity": _text(authority.sensitivity),
            "policy_class": _text(authority.policy_class),
        }
        if not all(values.values()):
            raise NativePortError("knowledge_scope_required")
        from ..content.store import ContentReadScope

        try:
            return ContentReadScope(**values)
        except (TypeError, ValueError) as exc:
            raise NativePortError("knowledge_scope_required") from exc

    def _knowledge_scope(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Any:
        """Bind a request to the exact scope issued by native capability.

        The three Knowledge ACL selectors are deliberately not inferred from
        payload defaults.  A process-issued ``NativeBoundContext`` must carry
        each selector and the request must repeat the exact same values.  This
        keeps a valid capability from being replayed against another namespace
        or broader sensitivity/policy class while preserving optional scope
        fields for non-Knowledge handlers.
        """

        # Knowledge is a scoped content read, not a neutral identity echo.
        # Resolve the process-issued authority here (before inspecting any
        # selectors or touching the service) so a plain identity mapping,
        # ``to_dict`` projection, or other lookalike can never become a read
        # capability.  Keep the stable native error used by the mutation and
        # Phase 9 scoped-read gates.
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc

        capability_scope = tuple(
            _text(getattr(authority, key))
            for key in ("namespace_id", "sensitivity", "policy_class")
        )
        if not all(capability_scope):
            raise NativePortError("knowledge_scope_required")
        request_scope = tuple(
            _text(payload.get(key))
            for key in ("namespace_id", "sensitivity", "policy_class")
        )
        if not all(request_scope):
            raise NativePortError("knowledge_scope_required")
        if request_scope != capability_scope:
            raise NativePortError("knowledge_scope_mismatch")
        namespace, sensitivity, policy_class = capability_scope
        from ..content.store import ContentReadScope

        try:
            return ContentReadScope(
                namespace_id=namespace,
                workspace_id=self.workspace,
                agent_instance_id=_text(context.get("agent_instance_id")),
                project_ref=_text(context.get("project_ref")),
                provider=_text(context.get("provider")),
                share_group_id=_text(context.get("share_group_id")),
                sensitivity=sensitivity,
                policy_class=policy_class,
            )
        except (TypeError, ValueError) as exc:
            raise NativePortError("knowledge_scope_required") from exc

    @staticmethod
    def _knowledge_service_result(
        service: Any,
        operation: str,
        payload: Mapping[str, Any],
        *,
        scope: Any,
    ) -> Any:
        dispatch = getattr(service, "dispatch", None) or getattr(service, "call", None)
        if not callable(dispatch):
            raise NativePortError("v2_knowledge_service_unavailable")
        try:
            result = dispatch(operation, payload, scope=scope)
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_knowledge_service_failed") from exc
        if not isinstance(result, Mapping):
            raise NativePortError("v2_knowledge_service_failed")
        if result.get("ok") is False or _text(result.get("status")).casefold() in {"error", "blocked", "failed"}:
            code = _text(result.get("code") or result.get("error"))
            raise NativePortError(code or "v2_knowledge_service_failed")
        return result

    def _knowledge_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        public_name: str = "",
        **_: Any,
    ) -> Any:
        # Validate the native scope before resolving/constructing any service;
        # GUI scope comes from the process-issued SafeBridge capability while
        # MCP/native callers retain exact selector-echo validation.
        scope = self._gui_knowledge_scope(context) if public_name.startswith("knowledge_") else self._knowledge_scope(payload, context)
        service = self._native_service("knowledge")
        if service is None:
            raise NativePortError("v2_knowledge_service_unavailable")
        # Pass the canonical selectors to the service, not the caller's
        # possibly conflicting values.  Authorization has already happened at
        # this native boundary and the service receives a stable exact scope.
        bound_payload = dict(payload)
        bound_payload.update({
            "namespace_id": scope.namespace_id,
            "sensitivity": scope.sensitivity,
            "policy_class": scope.policy_class,
        })
        return self._knowledge_service_result(service, operation, bound_payload, scope=scope)

    def _knowledge_book(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        public_name: str = "",
        **kwargs: Any,
    ) -> Any:
        if public_name == "knowledge_book":
            scope = self._gui_knowledge_scope(context)
            try:
                return self._knowledge_command().book_info(
                    _text(payload.get("book_id")), scope=scope,
                )
            except Exception as exc:
                raise NativePortError(
                    _text(getattr(exc, "code", "")) or "v2_knowledge_book_failed"
                ) from exc
        return self._knowledge_operation("memoryguard_knowledge_book", payload, context, public_name=public_name, **kwargs)

    def _knowledge_candidates(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        public_name: str = "",
        **kwargs: Any,
    ) -> Any:
        # GUI candidate reads use the process-issued capability scope.  MCP
        # candidate reads retain exact selector validation in
        # ``_knowledge_operation``; the public operation name distinguishes
        # those two native contracts.
        return self._knowledge_operation(
            "memoryguard_knowledge_candidates",
            payload,
            context,
            public_name=public_name,
            **kwargs,
        )

    def _knowledge_read(self, payload: Mapping[str, Any], context: Mapping[str, Any], *, public_name: str = "", **_: Any) -> Any:
        # GUI list/read are product-facing contracts over the V2 metadata/body
        # service. MCP/native recall remains reference-only below.
        if public_name in {"knowledge_list", "knowledge_read"}:
            scope = self._gui_knowledge_scope(context)
            service = self._knowledge_command()
            try:
                if public_name == "knowledge_list":
                    return service.list_books(scope=scope)
                return service.read_occurrence(
                    _text(payload.get("occurrence_id")), scope=scope,
                )
            except Exception as exc:
                raise NativePortError(
                    _text(getattr(exc, "code", "")) or "v2_knowledge_read_failed"
                ) from exc
        # Search intentionally remains reference-only: a fuzzy query must not
        # become a bulk body export surface.
        scope = self._gui_knowledge_scope(context) if public_name.startswith("knowledge_") else self._knowledge_scope(payload, context)
        adapter = self._stores.get("knowledge")
        if adapter is None:
            content = self._stores.get("content")
            if content is None:
                content = self._domain_store("content")
            from ..knowledge_v2.adapter import KnowledgeV2Adapter
            adapter = KnowledgeV2Adapter(content, namespace_id=scope.namespace_id)
            self._stores["knowledge"] = adapter
        occurrence_id = _text(payload.get("occurrence_id")) or None
        rows = adapter.read(scope, query=_text(payload.get("query")), limit=payload.get("limit", 100), occurrence_id=occurrence_id)
        if public_name == "knowledge_search":
            return {
                "ok": True,
                "status": "succeeded",
                "results": [
                    {
                        "occurrence_id": str(item.get("ref") or ""),
                        "summary": str(item.get("summary") or ""),
                        "hash": str(item.get("hash") or ""),
                        "trust": "reference_only",
                    }
                    for item in rows
                ],
                "total": len(rows),
                "reference_only": True,
            }
        return rows

    def _gui_task_scope(self, context: Mapping[str, Any]) -> Any:
        try:
            resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc
        from .task_coordinator import TaskCoordinator

        return TaskCoordinator.scope_from_context(self.workspace, context)

    @staticmethod
    def _gui_task_key(operation: str, payload: Mapping[str, Any]) -> str:
        explicit = _text(payload.get("idempotency_key") or payload.get("request_id"))
        if explicit:
            return explicit
        # No browser identity participates.  A nonce makes this one user action
        # distinct from later intentional reruns; callers that need retry
        # deduplication supply an explicit request/idempotency key.
        import time

        digest = hashlib.sha256(
            (str(operation) + "\x1f" + _canonical_json(dict(payload)) + "\x1f" + str(time.time_ns())).encode("utf-8")
        ).hexdigest()
        return "gui-" + digest

    def _gui_task_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        run_id = _text(payload.get("run_id") or payload.get("job_id") or payload.get("request_id"))
        if not run_id:
            raise NativePortError("task_run_id_required")
        result = self._task_service().status(run_id, self._gui_task_scope(context))
        if not result.get("ok"):
            raise NativePortError(_text((result.get("error") or {}).get("code")) or "task_not_found")
        task = dict(result.get("task") or {})
        return {
            **result,
            "job_id": run_id,
            "phase": task.get("stage", result.get("status")),
            "processed": task.get("progress", 0),
            "total": 100,
        }

    def _gui_task_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._task_service().list_pending(
            self._gui_task_scope(context),
            limit=payload.get("limit", 100),
        )

    def _gui_task_cancel(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if payload.get("confirmed") is not True:
            raise NativePortError("task_cancel_confirmation_required")
        run_id = _text(payload.get("run_id") or payload.get("job_id") or payload.get("request_id"))
        scope = self._gui_task_scope(context)
        if not run_id:
            # No run id yet: resolve the exact trusted scope's active
            # projection_build run.  Exactly one => cancel it; zero or
            # ambiguous => fail closed instead of issuing an empty no-op.
            active = self._task_service().active_runs(scope, operation="projection_build")
            if not active:
                raise NativePortError("no_active_projection_build")
            if len(active) > 1:
                raise NativePortError("ambiguous_active_projection_build")
            run_id = active[0]
        result = self._task_service().cancel(run_id, scope, timeout=5.0)
        if not result.get("ok"):
            raise NativePortError(_text((result.get("error") or {}).get("code")) or "task_cancel_failed")
        return result

    def _gui_usage_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        self._gui_authority(context)
        if operation != "usage_telemetry":
            raise NativePortError("unknown_usage_query")
        window_days = payload.get("window_days", 7)
        if type(window_days) is not int or window_days not in {7, 30}:
            raise NativePortError("invalid_usage_window")
        agent_key = _text(payload.get("agent_key"))
        share_group_id = _text(context.get("share_group_id"))
        # Token page defaults to the trusted share group.  The current GUI
        # workspace/project_ref is not an implicit conversion filter.
        project_ref = ""

        # The usage backend must receive the trusted, current binding roster.
        # Do not use the global discovery list here: it includes installed but
        # unbound products and historical endpoints, which would create
        # phantom filter options and leak scope metadata into this view.
        roster: list[dict[str, Any]] = []
        try:
            from ..agent_mapping import normalize_program_identity

            binding_result = self._group_service(write=False).list_bindings(
                include_inactive=False,
            )
            bindings = binding_result.get("bindings", []) if isinstance(binding_result, Mapping) else []
            by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for binding in bindings:
                if not isinstance(binding, Mapping):
                    continue
                if _text(binding.get("share_group_id") or binding.get("group_id")) != share_group_id:
                    continue
                raw_provider = _text(
                    binding.get("provider")
                    or binding.get("provider_identity_provider")
                    or binding.get("canonical_program_id")
                    or binding.get("program_id")
                    or binding.get("mcp_server_name")
                )
                raw_program = _text(
                    binding.get("canonical_program_id")
                    or binding.get("program_id")
                    or binding.get("program")
                    or raw_provider
                )
                identity = normalize_program_identity(
                    raw_provider or raw_program,
                    mcp_name=_text(binding.get("mcp_server_name")),
                    mcp_display_name=_text(binding.get("display_name") or binding.get("canonical_display_name")),
                )
                provider = _text(identity.get("provider")) or raw_provider or "unknown"
                program = _text(identity.get("program_id")) or raw_program or "unknown"
                key = (provider, program)
                item = by_key.setdefault(
                    key,
                    {
                        "provider": provider,
                        "program": program,
                        "display_name": (
                            _text(binding.get("display_name"))
                            or _text(binding.get("canonical_display_name"))
                            or _text(identity.get("display_name"))
                            or program
                        ),
                        "agent_key": f"{provider}:{program}",
                        "agent_instance_ids": [],
                        "binding_ids": [],
                    },
                )
                instance_id = _text(binding.get("agent_instance_id") or binding.get("endpoint_id"))
                binding_id = _text(binding.get("binding_id"))
                if instance_id and instance_id not in item["agent_instance_ids"]:
                    item["agent_instance_ids"].append(instance_id)
                if binding_id and binding_id not in item["binding_ids"]:
                    item["binding_ids"].append(binding_id)
            roster = sorted(by_key.values(), key=lambda item: (item["provider"], item["program"]))
        except Exception:
            # A roster enrichment failure must not make measured data vanish.
            # The backend still receives trusted scope and can expose observed
            # rows; it must not be replaced with a hardcoded agent catalog.
            roster = []
        try:
            from ..usage_telemetry import get_usage_summary

            summary = dict(
                get_usage_summary(
                    self.workspace,
                    window_days=window_days,
                    share_group_id=share_group_id,
                    project_ref=project_ref,
                    agent_key=agent_key or None,
                    agent_roster=roster,
                )
            )
        except ValueError as exc:
            raise NativePortError("invalid_usage_window") from exc
        except Exception as exc:
            raise NativePortError("usage_telemetry_unavailable") from exc

        def with_agent_key(item: Any) -> dict[str, Any]:
            row = dict(item) if isinstance(item, Mapping) else {}
            key = _text(row.get("agent_key") or row.get("agent_stable_key"))
            row["agent_key"] = key
            return row

        agents = [with_agent_key(item) for item in summary.get("agents", [])]
        rows = [with_agent_key(item) for item in summary.get("rows", [])]
        summary["agents"] = agents
        summary["rows"] = rows
        summary["share_group_id"] = share_group_id
        summary["project_ref"] = project_ref
        summary["agent_filter"] = agent_key
        summary["summary_scope"] = "agent" if agent_key else "share_group"
        return {"ok": True, "data": summary}

    def _gui_usage_sync(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **_: Any,
    ) -> Any:
        del payload
        self._gui_authority(context)
        try:
            from ..usage_telemetry import (
                _safe_agent_name,
                _safe_sync_status,
                sync_usage_telemetry,
            )

            result = sync_usage_telemetry(self.workspace)
        except Exception as exc:
            raise NativePortError("usage_telemetry_sync_failed") from exc
        if not isinstance(result, Mapping):
            raise NativePortError("usage_telemetry_sync_failed")

        state = result.get("sync_state")
        state_map = state if isinstance(state, Mapping) else {}
        providers = state_map.get("providers")
        provider_map = providers if isinstance(providers, Mapping) else {}

        def count(value: Any) -> int:
            return value if type(value) is int and value >= 0 else 0

        safe_providers = {
            _safe_agent_name(name): {
                "status": _safe_sync_status(item.get("status")),
                "inserted": count(item.get("inserted_count")),
                "rotated": count(item.get("rotated_count")),
                "sources": count(item.get("source_count")),
            }
            for name, item in provider_map.items()
            if _text(name) and isinstance(item, Mapping)
        }
        return {
            "ok": True,
            "data": {
                "status": _safe_sync_status(result.get("status")),
                "inserted": count(result.get("inserted")),
                "rotated": count(result.get("rotated")),
                "sources": count(result.get("sources")),
                "providers": safe_providers,
            },
        }

    def _gui_knowledge_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        scope = self._gui_knowledge_scope(context)
        service = self._knowledge_command()
        try:
            return service.dispatch(operation, payload, scope=scope, context=context)
        except Exception as exc:
            code = _text(getattr(exc, "code", ""))
            raise NativePortError(code or "v2_knowledge_command_failed") from exc

    def _gui_knowledge_command(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        scope = self._gui_knowledge_scope(context)
        service = self._knowledge_command()
        try:
            result = service.dispatch(operation, payload, scope=scope, context=context)
        except Exception as exc:
            code = _text(getattr(exc, "code", ""))
            raise NativePortError(code or "v2_knowledge_command_failed") from exc
        if operation in {"knowledge_source_add", "knowledge_reingest", "knowledge_rebuild_smart"}:
            task = dict(result.get("task") or {}) if isinstance(result, Mapping) else {}
            if isinstance(result, Mapping):
                return {
                    **dict(result),
                    "accepted": bool(result.get("accepted", True)),
                    "job_id": task.get("run_id", ""),
                    "deferred": True,
                }
        return result

    @staticmethod
    def _mcp_mutation_proof(payload: Mapping[str, Any]) -> None:
        """Require the same explicit retry proof on direct bridge calls."""

        if payload.get("confirmed") is not True:
            raise NativePortError("confirmation_required")
        key = _text(payload.get("idempotency_key"))
        if not key or len(key) > 256:
            raise NativePortError("idempotency_key_required")

    def _mcp_capabilities(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **_: Any,
    ) -> Any:
        """Direct-native counterpart of registry-only MCP discovery."""

        del context
        from ..mcp_catalog import mcp_capability_catalog

        try:
            return mcp_capability_catalog(payload)
        except ValueError as exc:
            raise NativePortError(_text(exc) or "capability_pagination_invalid") from exc

    def _mcp_invoke(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        **_: Any,
    ) -> Any:
        """Dispatch one registered MCP target through its ordinary native gate."""

        try:
            target_surface, target, arguments = resolve_mcp_broker_invocation(
                payload,
                tool_names=MCP_TOOL_NAMES,
                mutation_names=MCP_MUTATION_NAMES,
                gui_names=MCP_BROKER_GUI_METHOD_NAMES,
                gui_mutation_names=GUI_MUTATION_NAMES,
            )
        except ValueError as exc:
            raise NativePortError(_text(exc) or "broker_operation_invalid") from exc
        dispatch = self.dispatch_mcp if target_surface == "mcp" else self.dispatch_gui
        nested = dispatch(target, arguments, context=context, generation=generation, state=state)
        if nested.get("ok") is False:
            raise NativePortError(_text(nested.get("code") or nested.get("error")) or "broker_target_failed")
        return nested.get("data", nested)

    def _mcp_gui_bridge(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        public_name: str = "",
        **_: Any,
    ) -> Any:
        """Reuse only explicitly mapped, scope-safe GUI business commands."""

        gui_name = MCP_GUI_BRIDGE_OPERATIONS.get(public_name)
        operation = get_gui_operation_spec(gui_name or "")
        if operation is None:
            raise NativePortError("broker_operation_unknown")
        if operation.mutation:
            self._mcp_mutation_proof(payload)
        if operation.native_handler == "gui_knowledge_command":
            return self._gui_knowledge_command(
                payload, context, operation=operation.canonical_name,
            )
        if operation.native_handler == "gui_task_status":
            return self._gui_task_status(payload, context)
        if operation.native_handler == "gui_task_list":
            return self._gui_task_list(payload, context)
        if operation.native_handler == "gui_task_cancel":
            return self._gui_task_cancel(payload, context)
        raise NativePortError("broker_operation_unavailable")

    def _gui_projection_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        scope = self._gui_projection_scope(context)
        try:
            if operation == "projection_source_map":
                return self._projection_service().source_map(scope=scope)
            raise NativePortError("unknown_projection_query")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_projection_query_failed") from exc

    def _gui_projection_command(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        scope = self._gui_projection_scope(context)
        service = self._projection_service()
        try:
            if operation == "projection_source_toggle":
                return service.set_source_enabled(
                    _text(payload.get("root_id") or payload.get("source_id")),
                    bool(payload.get("enabled")),
                    scope=scope,
                )
            if operation == "projection_delete":
                if payload.get("confirmed") is not True:
                    raise NativePortError("projection_confirmation_required")
                return service.delete(scope=scope, mode=_text(payload.get("mode")) or "reconstructed")
            if operation == "projection_build":
                if payload.get("confirmed") is not True:
                    raise NativePortError("projection_confirmation_required")
                coordinator = self._task_service()
                task_scope = self._gui_task_scope(context)
                key = self._gui_task_key(operation, payload)
                mode = _text(payload.get("mode")) or "reconstructed"
                # A trusted desktop/admin bridge is not an Agent filter.  The
                # persisted business scope above controls Agent/group ACL;
                # admin builds must not re-introduce the bridge runtime role as
                # a hidden atom filter.
                bridge_authority = self._gui_authority(context)
                is_server_admin_bridge = (
                    bool(bridge_authority.admin)
                    and _text(bridge_authority.agent_instance_id) == "memoryguard-server-admin"
                )
                runtime_role = "" if is_server_admin_bridge else _text(context.get("runtime_role"))
                # The browser may only name an engine id; the executable path is
                # resolved again from the fresh allowlist, never from llm_cli.
                llm_agent_id = _text(payload.get("llm_agent")).strip()
                enrich_mode = _text(payload.get("enrich_mode")) or "auto"
                engine: dict[str, Any] | None = None
                if llm_agent_id and llm_agent_id not in {"host", "auto", "none", "deterministic"}:
                    engine = self._resolve_engine_id(llm_agent_id)
                    if engine is None:
                        raise NativePortError("llm_engine_unavailable")
                deterministic = engine is None or enrich_mode in {"deterministic", "host"}

                def worker(execution: Any) -> Mapping[str, Any]:
                    return self._build_projection_worker(
                        scope,
                        mode,
                        runtime_role,
                        context,
                        engine=engine,
                        deterministic=deterministic,
                        execution=execution,
                    )

                accepted = coordinator.start_scope_exclusive(
                    operation="projection_build",
                    key=key,
                    scope=task_scope,
                    worker=worker,
                    goal="background_task",
                    request=payload,
                )
                task = dict(accepted.get("task") or {})
                return {
                    **accepted,
                    "accepted": True,
                    "job_id": task.get("run_id", ""),
                    "deferred": True,
                }
            raise NativePortError("unknown_projection_command")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_projection_command_failed") from exc

    def _gui_release_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        scope = self._gui_projection_scope(context)
        service = self._release_v2_service()
        try:
            if operation == "release_list":
                return service.list_releases(scope=scope, limit=int(payload.get("limit") or 100))
            if operation == "release_targets":
                return service.list_targets(scope=scope)
            if operation == "release_target_choose":
                return {
                    "ok": True,
                    "host_action": "choose_publish_target_path",
                    "kind": _text(payload.get("kind")) or "file",
                }
            if operation == "release_verify":
                target = service.resolve_target(
                    scope=scope,
                    target_path=_text(payload.get("target_path")),
                    target_root_id=_text(payload.get("target_root_id")),
                )
                return service.verify(
                    _text(payload.get("release_id")),
                    str(target),
                    scope=scope,
                )
            raise NativePortError("unknown_release_query")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_release_query_failed") from exc

    def _gui_release_command(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        scope = self._gui_projection_scope(context)
        service = self._release_v2_service()
        try:
            if operation == "release_plan_create":
                target = service.resolve_target(
                    scope=scope,
                    target_path=_text(payload.get("target_path")),
                    target_root_id=_text(payload.get("target_root_id")),
                )
                return service.create_plan(
                    str(target),
                    scope=scope,
                    llm_provider=_text(payload.get("llm_provider")) or "deterministic",
                    mode=_text(payload.get("mode")) or "reconstructed",
                    runtime_role=_text(context.get("runtime_role")),
                )
            if operation in {"release_apply", "release_publish"}:
                if payload.get("confirmed") is not True:
                    raise NativePortError("release_confirmation_required")
                target = service.resolve_target(
                    scope=scope,
                    target_path=_text(payload.get("target_path") or payload.get("target_file")),
                    target_root_id=_text(payload.get("target_root_id")),
                )
                plan_id = _text(payload.get("plan_id"))
                if operation == "release_publish":
                    plan = service.create_plan(
                        str(target),
                        scope=scope,
                        llm_provider="deterministic",
                        mode="reconstructed",
                    )
                    plan_id = _text(plan.get("plan_id"))
                if not plan_id:
                    raise NativePortError("release_plan_required")
                coordinator = self._task_service()
                task_scope = self._gui_task_scope(context)
                key = self._gui_task_key("release_apply", payload)

                def worker(execution: Any) -> Mapping[str, Any]:
                    receipt = service.apply(
                        plan_id,
                        str(target),
                        scope=scope,
                        execution=execution,
                        confirmed=True,
                        runtime_role=_text(context.get("runtime_role")),
                    )
                    # RuntimeStore result_ref is intentionally authority/path
                    # hostile.  Persist only stable release references here;
                    # the full immutable receipt lives in projection_ledger.
                    return {
                        "release_id": _text(receipt.get("release_id")),
                        "plan_id": _text(receipt.get("plan_id")),
                        "target_digest": _text(receipt.get("target_digest")),
                        "projection_id": _text(receipt.get("projection_id")),
                        "projection_digest": _text(receipt.get("projection_digest")),
                    }

                accepted = coordinator.start(
                    operation="release_apply",
                    idempotency_key=key,
                    scope=task_scope,
                    worker=worker,
                    goal="background_task",
                )
                task = dict(accepted.get("task") or {})
                return {
                    **accepted,
                    "accepted": True,
                    "plan_id": plan_id,
                    "job_id": task.get("run_id", ""),
                    "deferred": True,
                }
            if operation == "release_rollback":
                if payload.get("confirmed") is not True:
                    raise NativePortError("release_confirmation_required")
                target_path = _text(payload.get("target_path"))
                target_root_id = _text(payload.get("target_root_id"))
                target = (
                    service.resolve_target(
                        scope=scope,
                        target_path=target_path,
                        target_root_id=target_root_id,
                    )
                    if target_path or target_root_id
                    else ""
                )
                return service.rollback(
                    _text(payload.get("release_id")),
                    str(target),
                    scope=scope,
                    confirmed=True,
                    force=bool(payload.get("force")),
                )
            raise NativePortError("unknown_release_command")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_release_command_failed") from exc

    @staticmethod
    def _gui_authority(context: Mapping[str, Any]) -> Any:
        try:
            return resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc

    def _gui_agent_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        self._gui_authority(context)
        service = self._agent_service()
        try:
            if operation == "discover_agents":
                return service.discover_agents()
            if operation == "get_selection_tree":
                return service.get_selection_tree(_text(payload.get("instance_id")))
            if operation == "get_agent_data":
                return service.get_agent_data(_text(payload.get("instance_id")))
            if operation == "list_agent_candidates":
                return service.list_candidates(
                    include_uninstalled=bool(payload.get("include_uninstalled", False)),
                    include_stale=bool(payload.get("include_stale", True)),
                    include_unknown=bool(payload.get("include_unknown", True)),
                )
            if operation == "list_archived_agents":
                return service.list_archives()
            if operation == "list_cleanup_history":
                return service.cleanup_history()
            if operation == "list_agents":
                return service.list_agents()
            if operation == "get_residual_cleanup":
                return service.residual_cleanup(
                    instance_id=_text(payload.get("instance_id")),
                    candidate_id=_text(payload.get("candidate_id")),
                )
            if operation == "open_agent_folder":
                dir_path = _text(payload.get("dir_path"))
                candidate_id = _text(payload.get("candidate_id"))
                if dir_path.startswith("agent-residual-"):
                    dir_path = service.resolve_residual_path(candidate_id, dir_path)
                return service.open_folder(
                    dir_path=dir_path,
                    candidate_id=candidate_id,
                )
            raise NativePortError("unknown_agent_query")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_agent_query_failed") from exc

    def _agent_candidate_id(self, service: Any, payload: Mapping[str, Any]) -> str:
        candidate = _text(payload.get("candidate_id"))
        if candidate:
            return candidate
        product = _text(payload.get("product")).casefold()
        if not product:
            raise NativePortError("candidate_id_required")
        rows = service.list_candidates(include_uninstalled=True).get("candidates", [])
        matches = [item for item in rows if _text(item.get("product")).casefold() == product]
        if len(matches) != 1:
            raise NativePortError("candidate_id_required")
        return _text(matches[0].get("candidate_id"))

    def _gui_agent_command(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        authority = self._gui_authority(context)
        if not bool(authority.admin):
            raise NativePortError("admin_capability_required")
        service = self._agent_service()
        try:
            if operation == "mark_agent_uninstalled":
                candidate = self._agent_candidate_id(service, payload)
                return service.mark_uninstalled(
                    candidate,
                    product=_text(payload.get("product")),
                    dir_path=_text(payload.get("dir_path")),
                    reason=_text(payload.get("reason")),
                )
            if operation == "unmark_agent_uninstalled":
                candidate = self._agent_candidate_id(service, payload)
                return service.unmark_uninstalled(candidate, product=_text(payload.get("product")))
            if operation == "archive_agent_dir":
                candidate = self._agent_candidate_id(service, payload)
                dir_path = _text(payload.get("dir_path"))
                if dir_path.startswith("agent-residual-"):
                    dir_path = service.resolve_residual_path(candidate, dir_path)
                return service.archive(
                    candidate,
                    dir_path=dir_path,
                    reason=_text(payload.get("reason")),
                    dry_run=bool(payload.get("dry_run")),
                )
            if operation == "restore_archived_agent":
                return service.restore(_text(payload.get("archive_id")))
            if operation == "delete_archived_agent":
                return service.delete_archive(_text(payload.get("archive_id")))
            raise NativePortError("unknown_agent_command")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_agent_command_failed") from exc

    def _gui_group_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        authority = self._gui_authority(context)
        service = self._group_service(write=False)
        try:
            if operation == "agent_binding_list":
                return service.list_bindings(include_inactive=bool(payload.get("include_inactive", True)))
            if operation == "group_list":
                return service.list_groups()
            if operation == "binding_drift":
                return service.check_drift(_text(payload.get("binding_id")))
            if operation == "group_preview":
                return service.group_preview(_text(payload.get("target_group_id")))
            if operation == "scope_get":
                state = service.scope_state(
                    _text(authority.agent_instance_id),
                    admin=bool(authority.admin),
                )
                state["principal_agent_instance_id"] = _text(authority.agent_instance_id)
                state.setdefault("active_binding", None)
                state.setdefault("members", [])
                state["options"] = service.list_groups().get("groups", [])
                return state
            raise NativePortError("unknown_group_query")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_group_query_failed") from exc

    @staticmethod
    def _confirmation(payload: Mapping[str, Any]) -> None:
        if payload.get("confirmed") is not True:
            raise NativePortError("confirmation_required")

    def _gui_group_command(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        authority = self._gui_authority(context)
        # All binding/group mutations below are administrative.  Check the
        # process-issued capability before constructing the writable service;
        # that service may open/inspect the control-plane namespace.
        admin_operations = {
            "bind_agent",
            "bind_agents_to_shared_group",
            "unbind_agent",
            "ensure_personal_memory_group",
            "leave_shared_group_to_personal",
            "dissolve_shared_group",
            "export_memory_group",
            "clear_memory_group",
            "archive_memory_group",
            "install_shared_group_mcp_redirects",
            "import_native_memories_to_group",
            "commit_shared_memory_governance",
            "enter_multi_agent_mode",
            "exit_multi_agent_mode",
        }
        if operation in admin_operations and not bool(authority.admin):
            raise NativePortError("admin_capability_required")
        service = self._group_service(write=True)
        trusted = authority.to_dict()
        try:
            if operation == "scope_set":
                requested = payload.get("requested_scope")
                if not isinstance(requested, Mapping):
                    raise NativePortError("governance_scope_required")
                return service.set_scope(
                    _text(authority.agent_instance_id),
                    requested,
                    admin=bool(authority.admin),
                )
            if operation == "commit_selection":
                self._confirmation(payload)
                selected = payload.get("selected")
                if not isinstance(selected, (list, tuple)):
                    raise NativePortError("selection_required")
                return self._agent_service().commit_selection(
                    _text(payload.get("instance_id")),
                    selected,
                )
            if operation == "bind_agent":
                return service.bind_agent(
                    _text(payload.get("target_agent_id")),
                    _text(payload.get("target_group_id")),
                    mcp_server_name=_text(payload.get("mcp_server_name")) or "memoryguard",
                    native_memory_mode=_text(payload.get("native_memory_mode")) or "observed",
                    redirect_paths=payload.get("redirect_paths") if isinstance(payload.get("redirect_paths"), (list, tuple)) else (),
                )
            if operation == "bind_agents_to_shared_group":
                agents = payload.get("target_agent_ids")
                if not isinstance(agents, (list, tuple)):
                    raise NativePortError("agent_instance_ids_required")
                return service.bind_agents(
                    agents,
                    share_group_id=_text(payload.get("target_group_id")),
                    mcp_server_name=_text(payload.get("mcp_server_name")) or "memoryguard",
                    native_memory_modes=payload.get("native_memory_modes") if isinstance(payload.get("native_memory_modes"), Mapping) else {},
                    redirect_paths=payload.get("redirect_paths") if isinstance(payload.get("redirect_paths"), Mapping) else {},
                )
            if operation == "unbind_agent":
                return service.unbind(_text(payload.get("binding_id")))
            if operation == "ensure_personal_memory_group":
                self._confirmation(payload)
                return service.ensure_personal(_text(payload.get("target_agent_id")))
            if operation == "leave_shared_group_to_personal":
                self._confirmation(payload)
                return service.leave_to_personal(_text(payload.get("target_agent_id")))
            if operation == "dissolve_shared_group":
                self._confirmation(payload)
                return service.dissolve(_text(payload.get("target_group_id")))
            if operation == "export_memory_group":
                self._confirmation(payload)
                return service.export_group(_text(payload.get("target_group_id")))
            if operation == "clear_memory_group":
                self._confirmation(payload)
                return service.clear_group(_text(payload.get("target_group_id")), trusted=trusted)
            if operation == "archive_memory_group":
                self._confirmation(payload)
                return service.archive_group(_text(payload.get("target_group_id")))
            if operation == "install_shared_group_mcp_redirects":
                self._confirmation(payload)
                return service.install_redirects(_text(payload.get("target_group_id")))
            if operation == "import_native_memories_to_group":
                self._confirmation(payload)
                agents = payload.get("target_agent_ids")
                return service.import_native_memories(
                    _text(payload.get("target_group_id")),
                    agent_instance_ids=agents if isinstance(agents, (list, tuple)) else None,
                    trusted=trusted,
                )
            if operation == "commit_shared_memory_governance":
                self._confirmation(payload)
                return service.commit_governance(
                    _text(payload.get("target_group_id")),
                    reason=_text(payload.get("reason")),
                    trusted=trusted,
                )
            if operation == "enter_multi_agent_mode":
                return service.set_mode("multi_agent_shared_mcp")
            if operation == "exit_multi_agent_mode":
                return service.set_mode("single_agent")
            raise NativePortError("unknown_group_command")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "v2_group_command_failed") from exc

    def _gui_governance_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        service = self._governance_native()
        try:
            if operation == "recent_events":
                return service.recent_events(context, limit=int(payload.get("limit") or 100))
            if operation == "auto_actions":
                return service.auto_actions(context, limit=int(payload.get("limit") or 100))
            if operation == "supersede_decisions":
                return service.supersede_decisions(context, limit=int(payload.get("limit") or 100))
            if operation == "conflicts":
                return service.conflicts(context)
            if operation == "quarantine":
                return service.quarantine(context)
            if operation == "memory_ir_summary":
                return service.memory_ir_summary(context)
            raise NativePortError("unknown_governance_query")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_governance_query_failed"
            ) from exc

    def _gui_governance_command(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        # Governance mutations must not instantiate or query the native
        # governance service until the process-issued admin capability has
        # been verified.  This keeps all resource names fail-closed.
        if operation in {
            "conflict_resolve",
            "conflict_close_stale",
            "quarantine_release",
            "quarantine_delete",
            "neuron_decide",
        } and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        service = self._governance_native()
        try:
            if operation == "conflict_resolve":
                return service.resolve_conflict(
                    _text(payload.get("conflict_group_id") or payload.get("group_id")),
                    _text(payload.get("keep_id") or payload.get("keep_memory_id")),
                    context,
                    confirmed=payload.get("confirmed") is True,
                )
            if operation == "conflict_close_stale":
                return service.close_stale_conflict(
                    _text(payload.get("conflict_group_id") or payload.get("group_id")),
                    context,
                )
            if operation == "quarantine_release":
                return service.release_quarantine(
                    _text(payload.get("quarantine_id")), context,
                )
            if operation == "quarantine_delete":
                return service.delete_quarantine(
                    _text(payload.get("quarantine_id")), context,
                )
            if operation == "neuron_decide":
                if payload.get("confirmed") is not True:
                    raise NativePortError("governance_confirmation_required")
                target_scope = payload.get("scope")
                return service.neuron_decide(
                    _text(payload.get("node_id")),
                    _text(payload.get("action")),
                    _text(payload.get("reason")),
                    context,
                    target_scope=(target_scope if isinstance(target_scope, Mapping) else None),
                )
            raise NativePortError("unknown_governance_command")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_governance_command_failed"
            ) from exc

    def _gui_audit_plan(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        service = self._audit_plan()
        try:
            if operation == "audit_plan_preview":
                return service.generate(_text(payload.get("finding_id")))
            if operation == "audit_plan_apply":
                if not self._trusted_admin(context):
                    raise NativePortError("admin_capability_required")
                return service.apply(_text(payload.get("plan_id")))
            if operation == "audit_plan_undo":
                if not self._trusted_admin(context):
                    raise NativePortError("admin_capability_required")
                return service.undo(_text(payload.get("change_id")))
            raise NativePortError("unknown_audit_plan_operation")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_audit_plan_failed"
            ) from exc

    def _gui_history_control(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        service = self._history_control()
        if operation == "history_source_discover":
            try:
                return service.discover()
            except Exception as exc:
                raise NativePortError(
                    _text(getattr(exc, "code", "")) or "v2_history_discovery_failed"
                ) from exc
        if operation != "history_backfill":
            raise NativePortError("unknown_history_control_operation")
        task_scope = self._gui_task_scope(context)
        continuation = payload.get("continuation")
        if continuation is not None and not isinstance(continuation, Mapping):
            raise NativePortError("history_continuation_invalid")
        progress = service.discover()
        key = self._gui_task_key(
            "history_backfill",
            {
                "progress_token": _text(progress.get("progress_token")),
                "continuation": dict(continuation) if isinstance(continuation, Mapping) else {},
            },
        )

        def worker(execution: Any) -> Mapping[str, Any]:
            try:
                result = service.backfill(
                    execution=execution,
                    continuation=(dict(continuation) if isinstance(continuation, Mapping) else None),
                )
            except Exception as exc:
                raise NativePortError(
                    _text(getattr(exc, "code", "")) or "history_backfill_failed"
                ) from exc
            return {
                "operation": "history_backfill",
                "status": "succeeded",
                "code": "ok",
                # Keep both the canonical task counters and the historical GUI
                # field names.  The browser waits for this result_ref; it must
                # never infer zero from the accepted/deferred envelope.
                "session_count": int(result.get("imported") or 0),
                "imported": int(result.get("imported") or 0),
                "skipped": int(result.get("skipped") or 0),
                "turn_count": int(result.get("turn_count") or 0),
                "changed_turn_count": int(result.get("changed_turn_count") or 0),
                "processed_files": int(result.get("processed_files") or 0),
                "processed_size": int(result.get("processed_bytes") or 0),
                "partial_count": int(result.get("partial") or 0),
                "errors": int(result.get("errors") or 0),
                "error_count": int(result.get("errors") or 0),
                "remaining_files": int(result.get("remaining_files") or 0),
                "remaining_fresh_files": int(result.get("remaining_fresh_files") or 0),
                "retryable_failed_files": int(result.get("retryable_failed_files") or 0),
                "pending_binding": list(result.get("pending_binding") or []),
                "pending_binding_count": len(result.get("pending_binding") or []),
                "continuation": result.get("continuation"),
                "memory_record_count": 0,
            }

        accepted = self._task_service().start(
            operation="history_backfill",
            idempotency_key=key,
            scope=task_scope,
            worker=worker,
            goal="background_task",
        )
        task = dict(accepted.get("task") or {})
        return {
            **accepted,
            "accepted": True,
            "job_id": task.get("run_id", ""),
            "deferred": True,
            "writes_long_term_memory": False,
        }

    def _gui_import_query(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        service = self._import_control()
        try:
            if operation == "import_preview":
                return service.preview_bundle(_text(payload.get("path")))
            if operation == "source_memory_summary":
                return service.source_memory_summary(context)
            if operation == "source_content_preview":
                return service.source_content_preview(
                    _text(payload.get("source_id") or payload.get("root_id")),
                    _text(payload.get("relative_path")),
                    context,
                )
            raise NativePortError("unknown_import_query")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_import_query_failed"
            ) from exc

    def _gui_import_control(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        if operation != "import_create":
            raise NativePortError("unknown_import_control_operation")
        if payload.get("confirmed") is not True:
            raise NativePortError("import_confirmation_required")
        path = _text(payload.get("path"))
        if not path:
            raise NativePortError("import_path_required")
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc
        scope = {
            "workspace_id": self.workspace,
            "agent_instance_id": _text(authority.agent_instance_id),
            "project_ref": _text(authority.project_ref),
            "share_group_id": _text(authority.share_group_id),
            "provider": _text(authority.provider) or "gui",
            "sensitivity": _text(authority.sensitivity) or "normal",
            "policy_class": _text(authority.policy_class) or "private",
        }
        service = self._import_control()
        task_scope = self._gui_task_scope(context)
        key = self._gui_task_key(
            "import_create",
            {"path_digest": hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()},
        )

        def worker(execution: Any) -> Mapping[str, Any]:
            try:
                result = service.import_bundle(path, scope=scope, execution=execution)
            except NativePortError:
                raise
            except Exception as exc:
                raise NativePortError(
                    _text(getattr(exc, "code", "")) or "import_execution_failed"
                ) from exc
            return {
                "operation": "import_create",
                "status": "succeeded",
                "code": "ok",
                "provider": _text(result.get("provider")),
                "source_id": _text(result.get("source_id")),
                "source_revision": int(result.get("source_revision") or 0),
                "manifest_digest": _text(result.get("manifest_digest")),
                "coverage_digest": _text(result.get("coverage_digest")),
                "session_count": int(result.get("conversation_count") or 0),
                "turn_count": int(result.get("turn_count") or 0),
                "changed_turn_count": int(result.get("changed_turn_count") or 0),
                "memory_record_count": 0,
            }

        accepted = self._task_service().start(
            operation="import_create",
            idempotency_key=key,
            scope=task_scope,
            worker=worker,
            goal="background_task",
        )
        task = dict(accepted.get("task") or {})
        return {
            **accepted,
            "accepted": True,
            "job_id": task.get("run_id", ""),
            "deferred": True,
            "writes_long_term_memory": False,
        }

    def _gui_maintenance_control(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        generation: int,
        **_: Any,
    ) -> Any:
        """Expose Reference-Audit planning and guarded Blob sweep to the GUI."""
        from ..maintenance_v2.reference_audit import ReferenceAudit

        def plan() -> dict[str, Any]:
            try:
                public = ReferenceAudit(self.workspace, mode="ro").audit().to_public_dict()
            except Exception as exc:
                raise NativePortError("v2_maintenance_plan_unavailable") from exc
            plan_payload = {
                "candidate_digest": _text(public.get("candidate_digest")),
                "candidate_count": int(public.get("candidate_count") or 0),
                "registry_digest": _text(public.get("registry_digest")),
                "manifest_generation": public.get("manifest_generation"),
                "blocker_codes": list(public.get("blocker_codes") or []),
                "blocked": bool(public.get("blocked")),
            }
            plan_id = "gc-plan-" + hashlib.sha256(
                _canonical_json(plan_payload).encode("utf-8")
            ).hexdigest()
            return {
                "ok": True,
                "status": "succeeded",
                "operation": "maintenance_plan",
                "plan": {"plan_id": plan_id, **plan_payload},
                "dry_run": True,
                "age_parameters_ignored": True,
            }

        if operation == "maintenance_plan":
            return plan()
        if operation != "maintenance_apply":
            raise NativePortError("unknown_maintenance_control_operation")
        if payload.get("confirmed") is not True:
            raise NativePortError("maintenance_confirmation_required")

        initial_plan = plan()
        plan_data = dict(initial_plan.get("plan") or {})
        if plan_data.get("blocked"):
            raise NativePortError("maintenance_plan_blocked")
        task_scope = self._gui_task_scope(context)
        idempotency_key = self._gui_task_key(
            "maintenance_apply",
            {
                "plan_id": plan_data.get("plan_id", ""),
                "candidate_digest": plan_data.get("candidate_digest", ""),
                "generation": generation,
            },
        )

        def worker(execution: Any) -> Mapping[str, Any]:
            execution.progress(5, "audit")
            execution.check_cancelled()
            fresh = plan()
            fresh_data = dict(fresh.get("plan") or {})
            if fresh_data.get("blocked"):
                raise NativePortError("maintenance_plan_blocked")
            if fresh_data.get("candidate_digest") != plan_data.get("candidate_digest"):
                raise NativePortError("maintenance_plan_stale")
            execution.progress(20, "lease")
            execution.check_cancelled()
            from ..maintenance_v2.runtime_port import (
                MaintenanceRuntimePort,
                bind_maintenance_transport_context,
            )

            runtime = self.maintenance_port
            if runtime is None:
                runtime = MaintenanceRuntimePort(self.workspace)
                self.maintenance_port = runtime
            maint_context = bind_maintenance_transport_context({
                "trusted_agent_id": _text(context.get("agent_instance_id")),
                "session_id": _text(context.get("session_id")) or "gui-maintenance",
            })
            lease = runtime.dispatch(
                "cli",
                "storage",
                {"action": "lease-acquire", "ttl_seconds": 300},
                context=maint_context,
                generation=generation,
                mutation=True,
            )
            if not lease.get("ok"):
                raise NativePortError(_text(lease.get("code") or lease.get("error")) or "maintenance_lease_failed")
            lease_data = lease.get("data") if isinstance(lease.get("data"), Mapping) else {}
            lease_id = _text(lease_data.get("lease_id"))
            if not lease_id:
                raise NativePortError("maintenance_lease_missing")
            try:
                execution.progress(45, "sweep", cancellable=False)
                result = runtime.dispatch(
                    "cli",
                    "storage",
                    {
                        "action": "sweep",
                        "apply": True,
                        "lease_id": lease_id,
                        "request_key": "gui-gc:" + _text(plan_data.get("plan_id")),
                    },
                    context=maint_context,
                    generation=generation,
                    mutation=True,
                )
                if not result.get("ok"):
                    raise NativePortError(_text(result.get("code") or result.get("error")) or "maintenance_sweep_failed")
                data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
                return {
                    "operation": "maintenance_apply",
                    "status": "succeeded",
                    "code": "ok",
                    "candidate_count": int(data.get("candidate_count") or 0),
                    "swept_count": int(data.get("swept_count") or 0),
                    "skipped_count": int(data.get("skipped_count") or 0),
                    "final_digest": _text(data.get("final_digest")),
                }
            finally:
                runtime.dispatch(
                    "cli",
                    "storage",
                    {"action": "lease-release", "lease_id": lease_id},
                    context=maint_context,
                    generation=generation,
                    mutation=True,
                )

        accepted = self._task_service().start(
            operation="maintenance_apply",
            idempotency_key=idempotency_key,
            scope=task_scope,
            worker=worker,
            goal="background_task",
        )
        task = dict(accepted.get("task") or {})
        return {
            **accepted,
            "accepted": True,
            "plan_id": plan_data.get("plan_id", ""),
            "job_id": task.get("run_id", ""),
            "deferred": True,
        }

    def _gui_request_compat(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        **_: Any,
    ) -> Any:
        """Map the retired RequestQueue contract directly onto TaskRun.

        Task-native operations return their existing run instead of creating a
        wrapper task.  Synchronous mutations execute inside one TaskCoordinator
        worker so legacy clients can still poll ``get_request_status`` without
        preserving a second request queue or a second authorization boundary.
        """
        method = _text(payload.get("method"))
        args = payload.get("args")
        if not method:
            raise NativePortError("request_method_required")
        target = get_gui_operation_spec(method)
        if target is None or not target.mutation:
            raise NativePortError("request_target_not_mutation")
        if method in {"submit_request", "request_mutation", "call_readonly"}:
            raise NativePortError("request_target_recursive")
        if not isinstance(args, (list, tuple, Mapping)) and args is not None:
            raise NativePortError("request_arguments_invalid")
        target_args: Any = [] if args is None else args

        if target.execution == "task":
            nested = self.dispatch_gui(
                method,
                target_args,
                context=context,
                generation=generation,
                mutation=True,
                state=state,
            )
            if not nested.get("ok"):
                raise NativePortError(_text(nested.get("code") or nested.get("error")) or "request_target_failed")
            task = nested.get("task")
            if not isinstance(task, Mapping):
                data = nested.get("data")
                task = data.get("task") if isinstance(data, Mapping) else None
            if not isinstance(task, Mapping) or not _text(task.get("run_id")):
                raise NativePortError("request_target_task_missing")
            return {
                "ok": True,
                "status": str(nested.get("status") or "accepted"),
                "operation": target.canonical_name,
                "task": dict(task),
                "request": {"request_id": _text(task.get("run_id")), "status": _text(task.get("state"))},
                "deferred": True,
            }

        task_scope = self._gui_task_scope(context)
        key = self._gui_task_key("request_mutation:" + target.canonical_name, {"method": method, "args": target_args})

        def worker(execution: Any) -> Mapping[str, Any]:
            execution.progress(10, "dispatch")
            execution.check_cancelled()
            nested = self.dispatch_gui(
                method,
                target_args,
                context=context,
                generation=generation,
                mutation=True,
                state=state,
            )
            if not nested.get("ok"):
                raise NativePortError(_text(nested.get("code") or nested.get("error")) or "request_target_failed")
            execution.progress(90, "receipt", cancellable=False)
            return {
                "operation": target.canonical_name,
                "status": "succeeded",
                "code": "ok",
            }

        accepted = self._task_service().start(
            operation="request_mutation",
            idempotency_key=key,
            scope=task_scope,
            worker=worker,
            goal="background_task",
        )
        task = dict(accepted.get("task") or {})
        return {
            **accepted,
            "accepted": True,
            "request": {"request_id": task.get("run_id", ""), "status": task.get("state", "")},
            "job_id": task.get("run_id", ""),
            "deferred": True,
        }

    @staticmethod
    def _audit_finding_id(code: str, domain: str, table: str) -> str:
        return "v2-" + hashlib.sha256(f"{code}\0{domain}\0{table}".encode("utf-8")).hexdigest()[:16]

    def _reference_audit(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        public_name: str = "",
        **_: Any,
    ) -> Any:
        """Run the native V2 reference audit in strict read-only mode."""

        del payload, context
        try:
            from ..maintenance_v2.reference_audit import ReferenceAudit

            result = ReferenceAudit(self.workspace, mode="ro").audit()
            public = result.to_public_dict()
            blockers: list[dict[str, Any]] = []
            for item in public.get("blockers", []):
                if not isinstance(item, Mapping):
                    continue
                row = dict(item)
                row["finding_id"] = self._audit_finding_id(
                    str(item.get("code") or ""),
                    str(item.get("domain") or ""),
                    str(item.get("table") or ""),
                )
                blockers.append(_readable_risk_finding(row))
            public["blockers"] = blockers
            # Stamp this successful read-only execution. get_audit and
            # run_audit both re-run the audit; there is no cached receipt.
            completed_at = datetime.now(timezone.utc).isoformat()
            public["audit_state"] = "completed"
            public["generated_at"] = completed_at
            public["completed_at"] = completed_at
            return {"ok": True, "status": "ok", "data": public}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_reference_audit_unavailable") from exc

    def _explain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del context
        finding_id = _text(payload.get("finding_id"))
        if not finding_id:
            raise NativePortError("finding_id_required")
        try:
            from ..maintenance_v2.reference_audit import ReferenceAudit

            result = ReferenceAudit(self.workspace, mode="ro").audit()
            blocker = next(
                (
                    item for item in result.blockers
                    if finding_id in {
                        item.code,
                        self._audit_finding_id(item.code, item.domain, item.table),
                    }
                ),
                None,
            )
            if blocker is None:
                raise NativePortError("finding_not_found")
            code = str(blocker.code or "")
            if "schema" in code:
                suggestion = "repair or migrate the blocked V2 schema, then rerun reference audit and readiness"
            elif "reference" in code or "orphan" in code:
                suggestion = "repair the broken V2 reference or restore its authoritative target, then rerun the audit"
            elif "unknown" in code:
                suggestion = "classify the unknown authoritative data and add an explicit migration/disposition before promotion"
            else:
                suggestion = "resolve this V2 reference-audit blocker and rerun validator/readiness"
            return _readable_risk_finding({
                "finding_id": self._audit_finding_id(blocker.code, blocker.domain, blocker.table),
                "code": code,
                "domain": str(blocker.domain or ""),
                "table": str(blocker.table or ""),
                "evidence": {"source": "v2_reference_audit", "read_only": True},
                "impact": "blocks proof of V2 reference integrity and therefore activation readiness",
                "suggestion": suggestion,
                "confidence": 1.0,
                "fixable": True,
            })
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_explain_unavailable") from exc

    @staticmethod
    def _is_server_admin_gui_authority(authority: Any) -> bool:
        return bool(getattr(authority, "admin", False)) and (
            _text(getattr(authority, "agent_instance_id", "")) == "memoryguard-server-admin"
            and _text(getattr(authority, "entrypoint", "")).casefold() == "gui"
        )

    def _codegraph_gui_project_rows(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        """List CodeGraph projects visible to the trusted global GUI group.

        The browser never supplies a group/Agent identity here.  The native
        authority fixes the active share group; project paths are merely local
        business selectors inside that already-authorized group.
        """
        authority = resolve_native_transport_context(context)
        if not self._is_server_admin_gui_authority(authority):
            raise NativePortError("admin_capability_required")
        group_id = _text(authority.share_group_id)
        if not group_id:
            raise NativePortError("codegraph_group_scope_required")
        source_projects: dict[str, dict[str, Any]] = {}
        try:
            from .source_control import SourceControlService
            source_service = SourceControlService(self.workspace)
            for source in source_service.list_sources(context).get("sources", ()):
                if not isinstance(source, Mapping):
                    continue
                source_id = _text(source.get("source_id"))
                if not source_id or _text(source.get("state")) != "READY":
                    continue
                try:
                    root, source_kind = source_service.resolve_root(source_id, context)
                except Exception:
                    continue
                if source_kind not in {"selected_directory", "obsidian_vault"} or not root.is_dir():
                    continue
                canonical = canonical_project_ref(root)
                if not canonical:
                    continue
                source_projects[canonical] = {
                    "scope_id": "", "source_id": source_id,
                    "project_ref": str(root), "project_key": canonical,
                    "label": _text(source.get("display_name")) or root.name or "Code project",
                    "agent_instance_id": "", "provider": "graphify", "runtime_role": "",
                    "file_count": 0, "symbol_count": 0, "built": False,
                    "authorized_source": True,
                }
        except Exception:
            source_projects = {}
        from ..codegraph_v2.automation import known_projects
        for project in known_projects(self.workspace, group_id):
            canonical = canonical_project_ref(project)
            source_projects.setdefault(canonical, {
                "scope_id": "", "source_id": "", "project_ref": project, "project_key": canonical,
                "label": Path(project).name, "agent_instance_id": "", "provider": "graphify",
                "runtime_role": "", "file_count": 0, "symbol_count": 0, "built": False,
                "authorized_source": False,
            })
        if not self.layout.codegraph_db.is_file():
            return [source_projects[key] for key in sorted(source_projects)]
        from ..codegraph_v2.store import CodeGraphStore
        if CodeGraphStore(self.workspace, initialize=False)._preflight() in {"fresh", "needs_aux"}:
            return [source_projects[key] for key in sorted(source_projects)]
        try:
            with open_database(self.layout.codegraph_db, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT g.scope_id,g.project_ref,g.agent_instance_id,g.provider,g.runtime_role,"
                    "(SELECT COUNT(*) FROM source_files f WHERE f.scope_id=g.scope_id AND f.active=1) file_count,"
                    "(SELECT COUNT(*) FROM symbols s WHERE s.scope_id=g.scope_id AND s.active=1) symbol_count "
                    "FROM graph_scopes g "
                    "WHERE g.workspace_id=? AND g.share_group_id=? AND g.project_ref<>'' "
                    "ORDER BY g.project_ref,g.agent_instance_id,g.provider,g.runtime_role",
                    (self.workspace, group_id),
                ).fetchall()
        except Exception as exc:
            raise NativePortError("codegraph_project_list_failed") from exc
        by_project: dict[str, dict[str, Any]] = dict(source_projects)
        for row in rows:
            project_ref = _text(row[1])
            canonical = canonical_project_ref(project_ref)
            if not canonical:
                continue
            candidate = {
                "scope_id": _text(row[0]),
                "source_id": "",
                "project_ref": project_ref,
                "project_key": canonical,
                "label": Path(project_ref).name or project_ref,
                "agent_instance_id": _text(row[2]),
                "provider": _text(row[3]),
                "runtime_role": _text(row[4]),
                "file_count": int(row[5] or 0),
                "symbol_count": int(row[6] or 0),
                "built": True,
                "authorized_source": False,
            }
            existing = by_project.get(canonical)
            if existing and _text(existing.get("source_id")):
                candidate["source_id"] = _text(existing.get("source_id"))
                candidate["label"] = _text(existing.get("label")) or candidate["label"]
                candidate["authorized_source"] = True
            # Prefer the group-level Graphify scope created by the desktop
            # builder over older Agent-scoped imports of the same repository.
            preferred = not candidate["agent_instance_id"] and candidate["provider"] == "graphify"
            existing_preferred = bool(existing and existing.get("built") and not existing["agent_instance_id"] and existing["provider"] == "graphify")
            if existing is None or not existing.get("built") or (preferred and not existing_preferred):
                by_project[canonical] = candidate
        return [by_project[key] for key in sorted(by_project)]

    def _codegraph_scope(
        self,
        context: Mapping[str, Any],
        *,
        codegraph_project_ref: str = "",
        codegraph_source_id: str = "",
    ) -> Any:
        try:
            from ..codegraph_v2 import CodeGraphScope

            authority = resolve_native_transport_context(context)
            if self._is_server_admin_gui_authority(authority):
                projects = self._codegraph_gui_project_rows(context)
                requested_source = _text(codegraph_source_id)
                requested = canonical_project_ref(codegraph_project_ref)
                if requested_source:
                    matches = [item for item in projects if _text(item.get("source_id")) == requested_source]
                    if not matches:
                        raise NativePortError("codegraph_source_not_found")
                    selected = matches[0]
                elif requested:
                    matches = [item for item in projects if item["project_key"] == requested]
                    if not matches:
                        raise NativePortError("codegraph_project_not_found")
                    selected = matches[0]
                elif len(projects) == 1:
                    selected = projects[0]
                elif not projects:
                    raise NativePortError("codegraph_project_not_built")
                else:
                    raise NativePortError("codegraph_project_required")
                # A GUI-built CodeGraph is canonical for the whole governed
                # group+project, not one Agent/provider/runtime instance.
                if _text(selected.get("source_id")):
                    return CodeGraphScope.from_value({
                        "workspace_id": self.workspace,
                        "share_group_id": authority.share_group_id,
                        "agent_instance_id": "",
                        "project_ref": selected["project_key"],
                        "provider": "graphify",
                        "runtime_role": "",
                        "trusted_context": True,
                    })
                return CodeGraphScope.from_value({
                    "workspace_id": self.workspace,
                    "share_group_id": authority.share_group_id,
                    "agent_instance_id": selected["agent_instance_id"],
                    "project_ref": selected["project_ref"],
                    "provider": selected["provider"],
                    "runtime_role": selected["runtime_role"],
                    "trusted_context": True,
                })
            requested_source = _text(codegraph_source_id)
            if requested_source:
                from .source_control import SourceControlError, SourceControlService

                try:
                    project, source_kind = SourceControlService(self.workspace).resolve_root(
                        requested_source, context,
                    )
                except SourceControlError as exc:
                    raise NativePortError(exc.code) from exc
                if source_kind not in {"selected_directory", "obsidian_vault"} or not project.is_dir():
                    raise NativePortError("codegraph_directory_source_required")
                canonical = canonical_project_ref(project)
                if not canonical:
                    raise NativePortError("codegraph_project_path_invalid")
                trusted_project = canonical_project_ref(_text(authority.project_ref))
                if trusted_project and not self._codegraph_project_contains(trusted_project, canonical):
                    raise NativePortError("codegraph_project_scope_mismatch")
                return CodeGraphScope.from_value({
                    "workspace_id": self.workspace,
                    "share_group_id": authority.share_group_id,
                    "agent_instance_id": authority.agent_instance_id,
                    "project_ref": canonical,
                    "provider": authority.provider,
                    "runtime_role": authority.runtime_role,
                    "trusted_context": True,
                })
            # GUI Graphify builds are persisted as trusted group-level scopes,
            # often for a repository nested below the caller's current
            # project.  Exact Agent/provider lookup would return a valid but
            # empty scope and hide that indexed repository.  Resolve nearest
            # populated scope from the trusted group; payload selectors never
            # participate in this decision.
            requested = canonical_project_ref(authority.project_ref)
            if requested and self.layout.codegraph_db.is_file():
                try:
                    store = self._domain_store("codegraph")
                    nearest = store.nearest_scopes(
                        project_ref=requested,
                        share_group_id=_text(authority.share_group_id),
                        agent_instance_id=_text(authority.agent_instance_id),
                        provider=_text(authority.provider),
                        runtime_role=_text(authority.runtime_role),
                        limit=1,
                    )
                    if nearest:
                        return nearest[0]
                except NativePortError as exc:
                    # A V1 graph cannot be opened through the read-only
                    # compatibility path.  A confirmed native update must
                    # still reach ``_domain_store(..., write=True)`` so its
                    # existing V1->V2 migration can run.  Keep every other
                    # preflight error fail-closed.
                    if exc.code != "codegraph_schema_upgrade_required":
                        raise
                except Exception:
                    # Missing/partial graph data must retain ordinary native
                    # status/query behavior; exact trusted scope below gives
                    # a bounded zero result without broadening access.
                    pass
            return CodeGraphScope.from_value({
                "workspace_id": self.workspace,
                "share_group_id": authority.share_group_id,
                "agent_instance_id": authority.agent_instance_id,
                "project_ref": authority.project_ref,
                "provider": authority.provider,
                "runtime_role": authority.runtime_role,
                "trusted_context": True,
            })
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("codegraph_trusted_scope_required") from exc

    @staticmethod
    def _codegraph_project_contains(parent: str, child: str) -> bool:
        try:
            return Path(child).resolve().is_relative_to(Path(parent).resolve())
        except (OSError, ValueError):
            return False

    def _codegraph_projects(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        projects = self._codegraph_gui_project_rows(context)
        from ..codegraph_v2 import automation
        authority = resolve_native_transport_context(context)
        group = _text(authority.share_group_id)
        for row in projects:
            row["automation"] = automation.status(self.workspace, group, row["project_ref"], built=row.get("built", False))
        try:
            from ..codegraph_v2.graphify_adapter import GraphifyCapability
            capability = GraphifyCapability.detect().to_dict()
        except Exception:
            capability = {"available": False, "metadata_export": False, "code": "graphify_status_unavailable"}
        return {
            "status": "READY" if projects else "NO_SOURCE",
            "projects": projects,
            "total": len(projects),
            "graphify": capability,
            "build_ready": bool(capability.get("available") and capability.get("metadata_export")),
            "automation": automation.policy(self.workspace, group),
        }

    def _codegraph_automation(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        authority = resolve_native_transport_context(context)
        if not self._is_server_admin_gui_authority(authority):
            raise NativePortError("admin_capability_required")
        from ..codegraph_v2 import automation
        group = _text(authority.share_group_id)
        request = payload.get("request", payload)
        if not isinstance(request, Mapping) or type(request.get("enabled")) is not bool or not group:
            raise NativePortError("codegraph_automation_policy_invalid")
        project = ""
        if not request.get("default_policy") is True:
            requested = canonical_project_ref(_text(request.get("codegraph_project_ref")))
            projects = self._codegraph_projects({}, context)["projects"]
            selected = next((row for row in projects if canonical_project_ref(row["project_ref"]) == requested), None)
            if selected is None:
                raise NativePortError("codegraph_project_not_found")
            project = selected["project_ref"]
        try:
            result = automation.set_policy(self.workspace, group, request["enabled"], project)
            if project and result["enabled"]:
                result = (automation.status(self.workspace, group, project, built=True)
                          if selected.get("built") else automation.ensure_graph(self.workspace, group, project, retry=True))
            return {"ok": True, "automation": result}
        except (OSError, ValueError, RuntimeError) as exc:
            raise NativePortError("codegraph_automation_policy_unavailable") from exc

    def _codegraph_build(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if payload.get("confirmed") is not True:
            raise NativePortError("confirmation_required")
        authority = resolve_native_transport_context(context)
        if not self._is_server_admin_gui_authority(authority):
            raise NativePortError("admin_capability_required")
        group_id = _text(authority.share_group_id)
        if not group_id:
            raise NativePortError("codegraph_group_scope_required")
        source_id = _text(payload.get("source_id") or payload.get("project_path"))
        if not source_id:
            raise NativePortError("codegraph_source_id_required")
        try:
            from ..codegraph_v2 import CodeGraphScope
            from ..codegraph_v2.store import _assert_no_reparse
            from ..codegraph_v2.graphify_adapter import GraphifyCapability
            from .source_control import SourceControlService, SourceControlError

            try:
                project, source_kind = SourceControlService(self.workspace).resolve_root(source_id, context)
            except SourceControlError as exc:
                raise NativePortError(exc.code) from exc
            if source_kind not in {"selected_directory", "obsidian_vault"} or not project.is_dir():
                raise NativePortError("codegraph_directory_source_required")
            _assert_no_reparse(project)
            canonical = canonical_project_ref(project)
            if not canonical:
                raise NativePortError("codegraph_project_path_invalid")
            capability = GraphifyCapability.detect()
            if not capability.available or not capability.metadata_export:
                raise NativePortError(capability.code or "graphify_metadata_export_unavailable")
            scope = CodeGraphScope.from_value({
                "workspace_id": self.workspace,
                "share_group_id": group_id,
                "agent_instance_id": "",
                "project_ref": canonical,
                "provider": "graphify",
                "runtime_role": "",
                "trusted_context": True,
            })
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("codegraph_project_path_invalid") from exc

        return self._start_codegraph_build(
            payload, context, source_id=source_id, project=project,
            canonical=canonical, scope=scope, operation="codegraph_build",
        )

    def _start_codegraph_build(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        source_id: str,
        project: Path,
        canonical: str,
        scope: Any,
        operation: str,
    ) -> Any:
        """Queue metadata-only Graphify export after source/scope authorization."""

        task_scope = self._gui_task_scope(context)
        key = self._gui_task_key(
            operation,
            {"source_id": source_id, "project_ref": canonical, "idempotency_key": payload.get("idempotency_key")},
        )

        def worker(execution: Any) -> Mapping[str, Any]:
            execution.progress(5, "graphify_scan")
            execution.check_cancelled()
            try:
                from ..graphify_core import CODE_EXTENSIONS
                from ..graphify_core import collect_files
                from ..graphify_core import export_repository, provenance_for_path
                from ..codegraph_v2.graphify_adapter import GraphifyExportAdapter

                candidates = collect_files(project, follow_symlinks=False, root=project)
                source_files = [
                    path for path in candidates
                    if (path.suffix in CODE_EXTENSIONS or path.suffix.lower() in CODE_EXTENSIONS)
                    and provenance_for_path(path.relative_to(project).as_posix()) == "production"
                ]
                execution.progress(18, "graphify_extract", item_count=len(source_files))
                execution.check_cancelled()
                export = export_repository(project, paths=source_files, complete=True, parallel=False, max_files=10_000)
                execution.progress(70, "codegraph_import", item_count=len(export.get("nodes") or ()))
                execution.check_cancelled()
                store = self._domain_store("codegraph", write=True)
                imported = GraphifyExportAdapter(store).project(export, scope=scope, full_snapshot=True)
                execution.progress(96, "codegraph_save", item_count=int(imported.counts.get("symbols", 0)))
                return {
                    "operation": operation,
                    "status": "succeeded",
                    "code": "ok",
                    "project_label": project.name,
                    "source_id": source_id,
                    "graphify_version": imported.graphify_version,
                    "projection_digest": imported.projection_digest,
                    "counts": dict(imported.counts),
                    "diagnostic_count": len(export.get("diagnostics") or ()),
                }
            except NativePortError:
                raise
            except Exception as exc:
                raise NativePortError(
                    _text(getattr(exc, "code", "")) or "codegraph_build_failed"
                ) from exc

        accepted = self._task_service().start_scope_exclusive(
            operation=operation,
            scope=task_scope,
            worker=worker,
            goal="background_task",
            key=key,
            request={"source_id": source_id, "project_ref": canonical},
        )
        task = dict(accepted.get("task") or {})
        result = {
            **accepted,
            "job_id": task.get("run_id", ""),
            "deferred": True,
        }
        if accepted.get("accepted", True):
            result.update({"source_id": source_id, "project_ref": canonical})
        else:
            result.update({"requested_source_id": source_id, "requested_project_ref": canonical})
        return result

    def _codegraph_build_bound(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Build one source into exact MCP caller scope, never global GUI scope."""

        self._mcp_mutation_proof(payload)
        authority = resolve_native_transport_context(context)
        source_id = _text(payload.get("source_id"))
        if not source_id:
            raise NativePortError("codegraph_source_id_required")
        try:
            from ..codegraph_v2 import CodeGraphScope
            from ..codegraph_v2.store import _assert_no_reparse
            from ..codegraph_v2.graphify_adapter import GraphifyCapability
            from .source_control import SourceControlError, SourceControlService

            try:
                project, source_kind = SourceControlService(self.workspace).resolve_root(source_id, context)
            except SourceControlError as exc:
                raise NativePortError(exc.code) from exc
            if source_kind not in {"selected_directory", "obsidian_vault"} or not project.is_dir():
                raise NativePortError("codegraph_directory_source_required")
            _assert_no_reparse(project)
            canonical = canonical_project_ref(project)
            if not canonical:
                raise NativePortError("codegraph_project_path_invalid")
            trusted_project = canonical_project_ref(_text(authority.project_ref))
            if trusted_project and not self._codegraph_project_contains(trusted_project, canonical):
                raise NativePortError("codegraph_project_scope_mismatch")
            if not all((_text(authority.share_group_id), _text(authority.agent_instance_id), _text(authority.provider))):
                raise NativePortError("codegraph_trusted_scope_required")
            capability = GraphifyCapability.detect()
            if not capability.available or not capability.metadata_export:
                raise NativePortError(capability.code or "graphify_metadata_export_unavailable")
            scope = CodeGraphScope.from_value({
                "workspace_id": self.workspace,
                "share_group_id": authority.share_group_id,
                "agent_instance_id": authority.agent_instance_id,
                "project_ref": canonical,
                "provider": authority.provider,
                "runtime_role": authority.runtime_role,
                "trusted_context": True,
            })
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("codegraph_project_path_invalid") from exc
        return self._start_codegraph_build(
            payload, context, source_id=source_id, project=project,
            canonical=canonical, scope=scope, operation="codegraph_build_bound",
        )

    @staticmethod
    def _codegraph_bounded_int(value: Any, *, default: int, minimum: int, maximum: int, code: str) -> int:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            raise NativePortError(code)
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise NativePortError(code) from exc
        return max(minimum, min(parsed, maximum))

    def _codegraph_query(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        query = _text(payload.get("query") or payload.get("q"))
        if not query:
            raise NativePortError("codegraph_query_required")
        scope = self._codegraph_scope(
            context, codegraph_source_id=_text(payload.get("codegraph_source_id") or payload.get("source_id")),
        )
        limit = self._codegraph_bounded_int(payload.get("limit"), default=100, minimum=1, maximum=1000, code="codegraph_limit_invalid")
        provenance = _text(payload.get("provenance"))
        try:
            store = self._domain_store("codegraph")
            symbols = store.query_symbols(query, scope=scope, provenance=provenance, limit=limit)
            return {
                "scope_digest": scope.digest,
                "query": query,
                "provenance": provenance,
                "count": len(symbols),
                "symbols": [item.to_dict() for item in symbols],
            }
        except NativePortError:
            raise
        except ValueError as exc:
            raise NativePortError("codegraph_provenance_invalid") from exc
        except Exception as exc:
            raise NativePortError("codegraph_query_failed") from exc

    def _codegraph_path(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        start_id = _text(payload.get("start_id"))
        end_id = _text(payload.get("end_id"))
        if not start_id or not end_id:
            raise NativePortError("codegraph_path_endpoints_required")
        scope = self._codegraph_scope(
            context, codegraph_source_id=_text(payload.get("codegraph_source_id") or payload.get("source_id")),
        )
        depth = self._codegraph_bounded_int(payload.get("max_depth"), default=8, minimum=1, maximum=32, code="codegraph_depth_invalid")
        try:
            store = self._domain_store("codegraph")
            result = store.path_query(
                start_id,
                end_id,
                scope=scope,
                max_depth=depth,
                relation=_text(payload.get("relation")),
                provenance=_text(payload.get("provenance")),
            )
            return {"scope_digest": scope.digest, **dict(result)}
        except NativePortError:
            raise
        except ValueError as exc:
            raise NativePortError("codegraph_provenance_invalid") from exc
        except Exception as exc:
            raise NativePortError("codegraph_path_failed") from exc

    def _codegraph_explain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        symbol_id = _text(payload.get("symbol_id") or payload.get("id"))
        if not symbol_id:
            raise NativePortError("codegraph_symbol_id_required")
        scope = self._codegraph_scope(
            context, codegraph_source_id=_text(payload.get("codegraph_source_id") or payload.get("source_id")),
        )
        edge_limit = self._codegraph_bounded_int(payload.get("edge_limit"), default=50, minimum=1, maximum=200, code="codegraph_limit_invalid")
        try:
            store = self._domain_store("codegraph")
            result = store.explain_symbol(
                symbol_id,
                scope=scope,
                provenance=_text(payload.get("provenance")),
                edge_limit=edge_limit,
            )
            return {"scope_digest": scope.digest, **dict(result)}
        except NativePortError:
            raise
        except ValueError as exc:
            raise NativePortError("codegraph_provenance_invalid") from exc
        except Exception as exc:
            raise NativePortError("codegraph_explain_failed") from exc

    def _codegraph_affected(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        start_id = _text(payload.get("start_id") or payload.get("symbol_id"))
        if not start_id:
            raise NativePortError("codegraph_start_id_required")
        scope = self._codegraph_scope(
            context, codegraph_source_id=_text(payload.get("codegraph_source_id") or payload.get("source_id")),
        )
        depth = self._codegraph_bounded_int(payload.get("depth"), default=2, minimum=0, maximum=32, code="codegraph_depth_invalid")
        limit = self._codegraph_bounded_int(payload.get("limit"), default=100, minimum=1, maximum=10_000, code="codegraph_limit_invalid")
        try:
            store = self._domain_store("codegraph")
            result = store.affected_query(
                start_id,
                scope=scope,
                depth=depth,
                limit=limit,
                relation=_text(payload.get("relation")),
                provenance=_text(payload.get("provenance")),
            )
            return result.to_dict()
        except NativePortError:
            raise
        except ValueError as exc:
            raise NativePortError("codegraph_provenance_invalid") from exc
        except Exception as exc:
            raise NativePortError("codegraph_affected_failed") from exc

    def _codegraph_update(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if payload.get("confirmed") is not True:
            raise NativePortError("confirmation_required")
        export = payload.get("export")
        if not isinstance(export, Mapping):
            raise NativePortError("graphify_metadata_export_required")
        full_snapshot = payload.get("full_snapshot")
        if full_snapshot is not None and not isinstance(full_snapshot, bool):
            raise NativePortError("codegraph_full_snapshot_invalid")
        scope = self._codegraph_scope(
            context,
            codegraph_source_id=_text(payload.get("codegraph_source_id") or payload.get("source_id")),
        )
        try:
            from ..codegraph_v2.graphify_adapter import GraphifyCapabilityError, GraphifyExportAdapter, GraphifyExportError

            store = self._domain_store("codegraph", write=True)
            result = GraphifyExportAdapter(store).project(export, scope=scope, full_snapshot=full_snapshot)
            return {"scope_digest": scope.digest, "status": "UPDATED", **result.to_dict()}
        except NativePortError:
            raise
        except GraphifyCapabilityError as exc:
            raise NativePortError(_text(getattr(exc, "code", "")) or "graphify_metadata_export_unavailable") from exc
        except GraphifyExportError as exc:
            code = _text(getattr(exc, "code", ""))
            raise NativePortError(code if code and " " not in code else "graphify_metadata_export_invalid") from exc
        except Exception as exc:
            # Keep public diagnostics safe while preserving the exception
            # class that identifies unexpected DB/projection failures.  The
            # outer dispatcher must not erase every failure as one generic
            # codegraph_update_failed result.
            name = "".join(char for char in type(exc).__name__.casefold() if char.isalnum())
            raise NativePortError(f"codegraph_update_failed_{name or 'unknown'}") from exc

    def _codegraph_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if isinstance(payload.get("request"), Mapping):
            payload = {**dict(payload["request"]), **{k: v for k, v in payload.items() if k != "request"}}
        scope = self._codegraph_scope(
            context,
            codegraph_project_ref=_text(payload.get("codegraph_project_ref")),
            codegraph_source_id=_text(payload.get("codegraph_source_id") or payload.get("source_id")),
        )
        try:
            from ..codegraph_v2.graphify_adapter import GraphifyCapability
            from .group_native import GroupControlError, GroupControlService

            capability = GraphifyCapability.detect()
            from ..codegraph_v2.store import CodeGraphStore
            if CodeGraphStore(self.workspace, initialize=False)._preflight() in {"fresh", "needs_aux"}:
                from ..codegraph_v2.automation import status as automation_status
                return {"available": True, "scope_digest": scope.digest, "counts": {},
                    "graphify": capability.to_dict(), "update_ready": bool(capability.available and capability.metadata_export),
                    "automation": automation_status(self.workspace, scope.share_group_id, scope.project_ref),
                    "incremental": {"supported": True, "built_scope": False, "active_binding": False,
                                    "queue_depth": 0, "enabled": False}}
            store = self._domain_store("codegraph")
            counts = store.counts(scope=scope)
            scope_id = store._scope_id(scope)
            with store.connection() as conn:
                # Older, already-built CodeGraph databases predate the
                # incremental refresh tables.  Status is a read-only health
                # surface and must remain usable for those graphs; do not
                # migrate/create schema here.  Once the table exists, let
                # every SQLite error propagate so corruption/integrity
                # failures remain visible instead of being reported as an
                # empty queue.
                refresh_queue = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='refresh_queue' LIMIT 1",
                ).fetchone()
                queue_depth = 0 if refresh_queue is None else int(conn.execute(
                    "SELECT COUNT(*) FROM refresh_queue WHERE scope_id=?",
                    (scope_id,),
                ).fetchone()[0])
            authority = resolve_native_transport_context(context)
            group_id = _text(getattr(scope, "share_group_id", ""))
            try:
                if self._is_server_admin_gui_authority(authority):
                    bindings = GroupControlService(self.workspace, write=False).list_bindings(
                        include_inactive=False,
                    ).get("bindings", [])
                    active_binding = any(
                        _text(item.get("share_group_id")) == group_id
                        for item in bindings
                    )
                else:
                    binding = GroupControlService(self.workspace, write=False).active_binding_for_agent(
                        _text(getattr(authority, "agent_instance_id", "")),
                    )
                    active_binding = bool(binding and _text(binding.get("share_group_id")) == group_id)
            except GroupControlError as exc:
                # A workspace may have a CodeGraph DB before the control-plane
                # DB has ever been initialized.  Status is a read-only health
                # surface: report an inactive binding in that case, while
                # preserving every other control-plane/data-integrity failure.
                if str(exc) != "system_schema_marker_missing":
                    raise
                active_binding = False
            built_scope = bool(int(counts.get("active_source_files") or 0))
            from ..codegraph_v2.automation import status as automation_status
            automation = automation_status(self.workspace, group_id, scope.project_ref, built=built_scope)
            incremental = {
                # Trusted PostToolUse refresh is built into this native port;
                # enabled still requires a real graph and a current binding.
                "supported": True,
                "built_scope": built_scope,
                "active_binding": active_binding,
                "queue_depth": queue_depth,
                "enabled": bool(built_scope and active_binding and automation["enabled"]),
            }
            return {
                "available": True,
                "scope_digest": scope.digest,
                "counts": counts,
                "graphify": capability.to_dict(),
                "incremental": incremental,
                "automation": automation,
                "update_ready": bool(capability.available and capability.metadata_export),
                "capability_error": "" if capability.available and capability.metadata_export else (capability.code or "graphify_metadata_export_unavailable"),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("codegraph_status_failed") from exc

    def _codegraph_graph(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read bounded codegraph metadata for one exact trusted scope.

        CodeGraphStore exposes paths, symbols, hashes, and edges only; source
        bodies never enter this response.  Missing/partial/future schemas are
        hard errors from the store preflight and never trigger initialization.
        """

        request = payload.get("request")
        if isinstance(request, Mapping):
            payload = {
                **dict(request),
                **{key: value for key, value in payload.items() if key != "request"},
            }
        requested_project = _text(payload.get("codegraph_project_ref"))
        requested_source = _text(payload.get("codegraph_source_id") or payload.get("source_id"))
        try:
            graph_scope = self._codegraph_scope(
                context,
                codegraph_project_ref=requested_project,
                codegraph_source_id=requested_source,
            )
        except NativePortError as exc:
            if exc.code in {
                "codegraph_project_not_built",
                "codegraph_project_required",
                "codegraph_project_not_found",
                "codegraph_source_not_found",
            }:
                projects = self._codegraph_gui_project_rows(context)
                return {
                    "status": "PROJECT_REQUIRED" if projects else "NO_SOURCE",
                    "scope_digest": "",
                    "project_required": bool(projects),
                    "projects": projects,
                    "nodes": [],
                    "edges": [],
                    "node_count": 0,
                    "edge_count": 0,
                }
            raise
        try:
            from ..codegraph_v2.models import normalize_provenance

            from ..codegraph_v2.store import CodeGraphStore
            if CodeGraphStore(self.workspace, initialize=False)._preflight() in {"fresh", "needs_aux"}:
                return {"status": "NO_SOURCE", "scope_digest": graph_scope.digest,
                    "project_ref": graph_scope.project_ref, "nodes": [], "edges": [], "node_count": 0, "edge_count": 0}
            store = self._domain_store("codegraph")
            preflight = getattr(store, "_preflight", None)
            if callable(preflight):
                preflight()
            limit = self._codegraph_bounded_int(payload.get("limit"), default=100, minimum=1, maximum=500, code="codegraph_limit_invalid")
            provenance = _text(payload.get("provenance"))
            provenance_filter = normalize_provenance(provenance) if provenance else ""
            files = tuple(
                source for source in store.list_source_files(scope=graph_scope, active_only=True)
                if not provenance_filter or source.provenance == provenance_filter
            )
            if not files:
                return {
                    "status": "NO_SOURCE",
                    "scope_digest": graph_scope.digest,
                    "project_ref": graph_scope.project_ref,
                    "nodes": [],
                    "edges": [],
                    "node_count": 0,
                    "edge_count": 0,
                }
            def file_rank(source: Any) -> tuple[int, int, str]:
                path = str(source.path or "").replace("\\", "/").casefold()
                roots = ("src/", "app/", "apps/", "packages/", "lib/", "server/", "client/")
                priority = next((index for index, prefix in enumerate(roots) if path.startswith(prefix)), len(roots))
                return priority, path.count("/"), path

            ordered_files = sorted(files, key=file_rank)
            max_file_nodes = max(1, min(len(ordered_files), max(4, limit // 8)))
            bundles: list[tuple[Any, tuple[Any, ...]]] = []
            for source in ordered_files[:max_file_nodes]:
                try:
                    symbols = tuple(
                        symbol for symbol in store.get_symbols(source.file_id, scope=graph_scope)
                        if not provenance_filter or symbol.provenance == provenance_filter
                    )
                except Exception:
                    symbols = ()
                bundles.append((source, symbols))
            selected = bundles
            nodes: list[dict[str, Any]] = []
            selected_symbols: list[Any] = []
            for source, _symbols in selected:
                if len(nodes) >= limit:
                    break
                nodes.append({
                    "id": source.file_id,
                    "node_kind": "file",
                    "label": source.path,
                    "path": source.path,
                    "language": source.language,
                    "content_hash": source.content_hash,
                    "source_revision": source.source_revision,
                    "source_role": source.source_role,
                    "provenance": source.provenance,
                })

            symbol_budget = max(0, limit - len(nodes))
            depth = 0
            while symbol_budget > 0:
                added = 0
                for _source, symbols in selected:
                    if depth >= len(symbols) or symbol_budget <= 0:
                        continue
                    selected_symbols.append(symbols[depth])
                    symbol_budget -= 1
                    added += 1
                if not added:
                    break
                depth += 1
            for symbol in selected_symbols:
                nodes.append({
                    "id": symbol.symbol_id,
                    "node_kind": "symbol",
                    "label": symbol.name,
                    "kind": symbol.kind,
                    "signature": symbol.signature,
                    "file_id": symbol.file_id,
                    "line_start": symbol.line_start,
                    "line_end": symbol.line_end,
                    "provenance": symbol.provenance,
                    "source_map": dict(symbol.source_map),
                    "metadata": dict(symbol.metadata),
                })

            visible_ids = {node["id"] for node in nodes}
            edges: list[dict[str, Any]] = []
            for symbol in selected_symbols:
                if symbol.file_id not in visible_ids:
                    continue
                edge_id = "visual-contains-" + hashlib.sha256(
                    f"{symbol.file_id}:{symbol.symbol_id}".encode("utf-8")
                ).hexdigest()[:24]
                edges.append({
                    "id": edge_id,
                    "from_id": symbol.file_id,
                    "to_id": symbol.symbol_id,
                    "relation": "contains",
                    "context": "file_symbol",
                    "provenance": symbol.provenance,
                    "source_location": "",
                    "metadata": {"visual_only": True},
                    "weight": 1.0,
                })

            edge_budget = max(limit, min(2_000, limit * 4))
            raw_edges = sorted(
                store.list_edges(scope=graph_scope),
                key=lambda edge: (-float(edge.weight or 0.0), str(edge.edge_id)),
            )
            for edge in raw_edges:
                if len(edges) >= edge_budget:
                    break
                if provenance_filter and edge.provenance != provenance_filter:
                    continue
                if edge.from_id not in visible_ids or edge.to_id not in visible_ids:
                    continue
                edges.append({
                    "id": edge.edge_id,
                    "from_id": edge.from_id,
                    "to_id": edge.to_id,
                    "relation": edge.relation,
                    "context": edge.context,
                    "provenance": edge.provenance,
                    "source_location": edge.source_location,
                    "metadata": dict(edge.metadata),
                    "weight": edge.weight,
                })
            total_counts = store.counts(scope=graph_scope)
            displayed_file_count = sum(1 for node in nodes if node.get("node_kind") == "file")
            displayed_symbol_count = sum(1 for node in nodes if node.get("node_kind") == "symbol")
            return {
                "status": "READY",
                "scope_digest": graph_scope.digest,
                "project_ref": graph_scope.project_ref,
                "nodes": nodes,
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "displayed_file_count": displayed_file_count,
                "displayed_symbol_count": displayed_symbol_count,
                "total_counts": dict(total_counts),
                "truncated": (
                    int(total_counts.get("source_files", 0)) > displayed_file_count
                    or int(total_counts.get("symbols", 0)) > displayed_symbol_count
                    or int(total_counts.get("edges", 0)) > len(edges)
                ),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_codegraph_read_unavailable") from exc

    @staticmethod
    def _gui_rule_record(rule: Mapping[str, Any]) -> dict[str, Any]:
        """Project one canonical V2 rule into the legacy GUI read shape only.

        Rules V2 remains authoritative.  This adapter gives the existing rule
        cards their historical body/kind/audience fields without writing a
        second rule record or changing canonical storage.
        """
        row = dict(rule)
        binding_rows = [dict(item) for item in row.get("bindings", ()) if isinstance(item, Mapping)]
        assignments = [
            {
                "assignment_id": _text(item.get("binding_id")),
                "target_type": _text(item.get("target_type")),
                "target_id": _text(item.get("target_id")),
                "project_ref": _text(item.get("project_ref")),
                "effect": _text(item.get("effect")) or "include",
                "priority_override": item.get("priority"),
            }
            for item in binding_rows
        ]
        strength = _text(row.get("rule_strength")).casefold()
        body = _text(row.get("canonical_text"))
        priorities = [
            int(item.get("priority") or 0)
            for item in binding_rows
            if isinstance(item.get("priority"), int) and not isinstance(item.get("priority"), bool)
        ]
        row.update({
            "memory_id": _text(row.get("memory_id")) or _text(row.get("definition_id")),
            "title": body[:120],
            "body": body,
            "kind": _text(row.get("rule_kind")) or "fact",
            "status": "active",
            "injection_policy": "always" if strength in {"must", "mandatory", "required"} else "relevant",
            "priority": max(priorities) if priorities else 0,
            "assignments": assignments,
            "locked": False,
        })
        return row

    @staticmethod
    def _virtual_rule_bucket(rule: Mapping[str, Any]) -> tuple[str, str]:
        """Map one canonical V2 rule to the stable V1 graph taxonomy."""

        kind = _text(rule.get("rule_kind")).casefold()
        strength = _text(rule.get("rule_strength")).casefold()
        polarity = _text(rule.get("polarity")).casefold()
        if strength in {"must", "mandatory", "required"}:
            return "mandatory", "强制规则"
        if kind == "preference":
            return "preferences", "长期习惯与偏好"
        if kind in {"procedure", "workflow", "instruction"}:
            return "procedures", "工作流程"
        if kind in {"correction", "constraint"} or polarity == "negative":
            return "corrections", "纠错与禁忌"
        if kind in {"project", "decision"}:
            return "projects", "项目决策"
        return "preferences", "长期习惯与偏好"

    @staticmethod
    def _canonical_rule_detail_branches(rule: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Build optional, body-free detail branches for one canonical rule.

        Main graph keeps one canonical rule node per definition.  Branches are
        metadata-only and are emitted only when the canonical row already
        carries the corresponding field; absence never gets represented as a
        fabricated source, version, conflict, or exception.
        """
        branches: list[dict[str, Any]] = []
        source_ids = [
            _text(value) for value in (rule.get("source_memory_ids") or ())
            if _text(value)
        ]
        if source_ids:
            branches.append({
                "branch_type": "source",
                "label": "来源",
                "items": [{"memory_id": value} for value in source_ids],
            })

        classification = {
            key: rule.get(key)
            for key in ("rule_kind", "polarity", "rule_strength", "maturity_state")
            if rule.get(key) not in (None, "")
        }
        if classification:
            branches.append({
                "branch_type": "classification",
                "label": "自动分类",
                "items": [classification],
            })

        scopes: list[dict[str, Any]] = []
        for binding in (rule.get("bindings") or rule.get("assignments") or ()):
            if not isinstance(binding, Mapping):
                continue
            item = {
                key: binding.get(key)
                for key in ("assignment_id", "target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority_override")
                if binding.get(key) not in (None, "")
            }
            if item:
                scopes.append(item)
        if scopes:
            branches.append({"branch_type": "scope", "label": "适用范围", "items": scopes})

        revision = rule.get("revision")
        try:
            revision_number = int(revision)
        except (TypeError, ValueError):
            revision_number = 0
        versions = rule.get("versions") or rule.get("history_versions")
        if isinstance(versions, (list, tuple)) and versions:
            branches.append({
                "branch_type": "history",
                "label": "历史版本",
                "items": [dict(item) for item in versions if isinstance(item, Mapping)],
            })
        elif revision_number > 1:
            branches.append({
                "branch_type": "history",
                "label": "历史版本",
                "items": [{"revision": revision_number}],
            })

        for key, label in (("conflicts", "冲突"), ("exceptions", "例外"), ("negative_evidence", "冲突")):
            values = rule.get(key)
            if isinstance(values, (list, tuple)) and values:
                branches.append({
                    "branch_type": "conflict" if key != "exceptions" else "exception",
                    "label": label,
                    "items": [dict(item) if isinstance(item, Mapping) else {"value": item} for item in values],
                })
        return branches

    def _with_virtual_neuron_overlay(
        self,
        graph: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Restore V1's safe rule/history indexes over V2 canonical stores.

        The durable Memory Projection remains reference-only.  Rules are read
        from RuleV2Store and history contributes metadata only; raw conversation
        turns are never copied into the graph payload.
        """

        result = dict(graph or {})
        nodes = [dict(node) for node in (result.get("nodes") or []) if isinstance(node, Mapping)]
        edges = [dict(edge) for edge in (result.get("edges") or []) if isinstance(edge, Mapping)]
        node_ids = {_text(node.get("id")) for node in nodes if _text(node.get("id"))}
        if "main" not in node_ids:
            nodes.insert(0, {
                "id": "main", "parent_id": "", "node_kind": "root",
                "label": "记忆胞体", "derivation": "记忆胞体",
            })
            node_ids.add("main")
        edge_ids = {_text(edge.get("id")) for edge in edges if _text(edge.get("id"))}

        def add_node(node: Mapping[str, Any]) -> None:
            # Keep dict identity for category accumulators: rules/history counts
            # are filled after children are collected and must update the node
            # already present in ``nodes`` rather than a discarded copy.
            item = node if isinstance(node, dict) else dict(node)
            if item.get("virtual_category") and not item.get("record_kind"):
                item["record_kind"] = _text(item.get("virtual_category"))
            node_id = _text(item.get("id"))
            if node_id and node_id not in node_ids:
                nodes.append(item)
                node_ids.add(node_id)

        def add_edge(source: str, target: str) -> None:
            edge_id = f"virtual-index:{source}:{target}"
            if edge_id in edge_ids:
                return
            edges.append({
                "id": edge_id,
                "source": source,
                "target": target,
                "edge_type": "virtual_index",
                "virtual": True,
            })
            edge_ids.add(edge_id)

        # ---- rules / habits -------------------------------------------------
        rules_id = "virtual-rules-habits"
        rules_node = {
            "id": rules_id,
            "parent_id": "main",
            "node_kind": "virtual_category",
            "virtual_category": "rules_habits",
            "label": "规则与习惯",
            "kind": "rules_habits",
            "count": 0,
            "virtual": True,
        }
        add_node(rules_node)
        add_edge("main", rules_id)
        bucket_labels = {
            "mandatory": "强制规则",
            "preferences": "长期习惯与偏好",
            "procedures": "工作流程",
            "corrections": "纠错与禁忌",
            "projects": "项目决策",
        }
        rule_buckets: dict[str, list[Mapping[str, Any]]] = {key: [] for key in bucket_labels}
        try:
            snapshot = self._gui_rule_snapshot({}, context)
            for rule in snapshot.get("rules", ()) if isinstance(snapshot, Mapping) else ():
                if not isinstance(rule, Mapping):
                    continue
                bucket, _label = self._virtual_rule_bucket(rule)
                rule_buckets.setdefault(bucket, []).append(rule)
        except Exception as exc:
            rules_node["load_error"] = _text(getattr(exc, "code", "")) or "rules_overlay_unavailable"

        rule_total = 0
        for bucket, label in bucket_labels.items():
            all_rules = rule_buckets.get(bucket, [])
            rule_total += len(all_rules)
            bucket_id = f"{rules_id}:{bucket}"
            add_node({
                "id": bucket_id,
                "parent_id": rules_id,
                "node_kind": "virtual_bucket",
                "virtual_category": "rules_habits",
                "bucket": bucket,
                "label": label,
                "kind": bucket,
                "count": len(all_rules),
                "has_more": len(all_rules) > 50,
                "virtual": True,
            })
            add_edge(rules_id, bucket_id)
            for rule in all_rules[:50]:
                definition_id = _text(rule.get("definition_id"))
                if not definition_id:
                    continue
                body = _text(rule.get("canonical_text"))
                memory_id = _text(rule.get("memory_id"))
                ref_id = f"virtual-rule-ref:{bucket}:{definition_id}"
                status = (
                    "excluded" if bool(rule.get("excluded"))
                    else "effective" if bool(rule.get("effective"))
                    else _text(rule.get("maturity_state")) or "observing"
                )
                add_node({
                    "id": ref_id,
                    "parent_id": bucket_id,
                    "node_kind": "virtual_rule_ref",
                    "canonical": True,
                    "canonical_node_kind": "canonical_rule",
                    "virtual_category": "rules_habits",
                    "definition_id": definition_id,
                    "memory_id": memory_id,
                    "source_memory_ids": list(rule.get("source_memory_ids") or ()),
                    "kind": _text(rule.get("rule_kind")) or bucket,
                    "label": " ".join(body.split())[:96] or definition_id,
                    "body": body,
                    "status": status,
                    "revision": rule.get("revision"),
                    "polarity": _text(rule.get("polarity")),
                    "rule_strength": _text(rule.get("rule_strength")),
                    "maturity_state": _text(rule.get("maturity_state")),
                    "bindings": list(rule.get("bindings") or ()),
                    "effective": bool(rule.get("effective")),
                    "excluded": bool(rule.get("excluded")),
                    "detail_branches": self._canonical_rule_detail_branches(rule),
                    "virtual": True,
                })
                add_edge(bucket_id, ref_id)
        rules_node["count"] = rule_total

        # ---- conversation history -------------------------------------------
        history_id = "virtual-conversation-history"
        history_node = {
            "id": history_id,
            "parent_id": "main",
            "node_kind": "virtual_category",
            "virtual_category": "conversation_history",
            "label": "对话历史",
            "kind": "conversation_history",
            "count": 0,
            "virtual": True,
        }
        add_node(history_node)
        add_edge("main", history_id)
        try:
            from .group_native import GroupControlService
            from .history_store import ContentHistoryStore, V2HistoryAccessResolver, V2HistoryScope

            agent_id = _text(context.get("agent_instance_id"))
            group_id = _text(context.get("share_group_id"))
            history_scope: Any
            if group_id and self._trusted_admin(context):
                bindings = GroupControlService(self.workspace, write=False).list_bindings(include_inactive=False).get("bindings", [])
                members = tuple(sorted({
                    _text(item.get("agent_instance_id"))
                    for item in bindings
                    if _text(item.get("share_group_id")) == group_id
                    and _text(item.get("agent_instance_id"))
                }))
                if not members:
                    raise PermissionError("history_active_binding_required")
                history_scope = V2HistoryScope(
                    agent_instance_id=members[0],
                    share_group_id=group_id,
                    authorized_agent_ids=members,
                    shared_read=True,
                )
            else:
                history_scope = V2HistoryAccessResolver(self.workspace).resolve(agent_id, {
                    "project_ref": _text(context.get("project_ref")),
                    "provider": _text(context.get("provider")),
                })
            listing = ContentHistoryStore(self.workspace, readonly=True).list_sessions(history_scope, limit=50, offset=0)
            sessions = list(listing.get("sessions") or ())
            history_node["count"] = int(listing.get("total") or len(sessions))
            history_node["total"] = history_node["count"]
            history_node["has_more"] = history_node["count"] > len(sessions)
            history_node["project_groups"] = list(listing.get("project_groups") or ())
            for session in sessions:
                if not isinstance(session, Mapping):
                    continue
                session_id = _text(session.get("session_id"))
                project_key = _text(session.get("project_key"))
                owner = _text(session.get("owner_agent_instance_id") or session.get("agent_instance_id"))
                if not session_id or not project_key or not owner:
                    continue
                project_id = f"history-project:{project_key}"
                owner_id = "history-agent:" + hashlib.sha256(
                    f"{project_key}\x1f{owner}".encode("utf-8")
                ).hexdigest()[:20]
                add_node({
                    "id": project_id,
                    "parent_id": history_id,
                    "node_kind": "history_project",
                    "virtual_category": "conversation_history",
                    "project_key": project_key,
                    "project_ref": _text(session.get("project_ref")),
                    "project_status": _text(session.get("project_status")) or "unknown",
                    "project_parent": _text(session.get("project_parent")),
                    "label": _text(session.get("project_label")) or "未识别项目",
                    "kind": "project",
                    "virtual": True,
                })
                add_edge(history_id, project_id)
                add_node({
                    "id": owner_id,
                    "parent_id": project_id,
                    "node_kind": "history_agent",
                    "virtual_category": "conversation_history",
                    "owner_agent_instance_id": owner,
                    "label": owner,
                    "kind": "agent",
                    "virtual": True,
                })
                add_edge(project_id, owner_id)
                node_id = f"history-session:{session_id}"
                add_node({
                    "id": node_id,
                    "parent_id": owner_id,
                    "node_kind": "history_session",
                    "virtual_category": "conversation_history",
                    "session_id": session_id,
                    "title": _text(session.get("title")),
                    "label": _text(session.get("title")) or session_id[:8],
                    "owner_agent_instance_id": owner,
                    "provider": _text(session.get("provider")),
                    "project_key": project_key,
                    "project_ref": _text(session.get("project_ref")),
                    "project_status": _text(session.get("project_status")) or "unknown",
                    "created_at": _text(session.get("created_at")),
                    "imported_at": _text(session.get("imported_at")),
                    "summary": _text(session.get("summary")),
                    "turn_count": int(session.get("turn_count") or 0),
                    "evidence_count": int(session.get("evidence_count") or 0),
                    "kind": "session",
                    "virtual": True,
                })
                add_edge(owner_id, node_id)
        except Exception as exc:
            history_node["load_error"] = _text(getattr(exc, "code", "")) or "history_overlay_unavailable"

        result["nodes"] = nodes
        result["edges"] = edges
        result["base_empty"] = bool(result.get("empty") or result.get("base_empty"))
        result["virtual_overlay_available"] = True
        stats = dict(result.get("stats") or {})
        stats["node_count"] = len(nodes)
        stats["edge_count"] = len(edges)
        stats["rule_count"] = rule_total
        stats["history_session_count"] = int(history_node.get("count") or 0)
        result["stats"] = stats
        return result

    def _projection_virtual_overlay(
        self,
        graph: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Compose GUI-only rule/history references onto a Memory Projection.

        Durable ProjectionStore remains reference-only and Memory-only.  Rules
        and conversation history retain their independent V2 stores; this
        method merely joins authorized metadata at the final GUI read boundary.
        """

        result = dict(graph)
        nodes = [dict(item) for item in result.get("nodes", ()) if isinstance(item, Mapping)]
        edges = [dict(item) for item in result.get("edges", ()) if isinstance(item, Mapping)]
        node_ids = {_text(item.get("id")) for item in nodes if _text(item.get("id"))}
        edge_ids = {_text(item.get("id")) for item in edges if _text(item.get("id"))}

        def add_node(item: Mapping[str, Any]) -> None:
            value = dict(item)
            node_id = _text(value.get("id"))
            if not node_id or node_id in node_ids:
                return
            nodes.append(value)
            node_ids.add(node_id)

        def add_edge(source: str, target: str, *, edge_type: str = "virtual_index") -> None:
            if source not in node_ids or target not in node_ids:
                return
            edge_id = f"{edge_type}:{source}:{target}"
            if edge_id in edge_ids:
                return
            edges.append({
                "id": edge_id,
                "source": source,
                "target": target,
                "edge_type": edge_type,
            })
            edge_ids.add(edge_id)

        if "main" not in node_ids:
            add_node({
                "id": "main",
                "node_kind": "root",
                "label": "记忆胞体",
                "parent_id": "",
                "derivation": "记忆胞体",
            })

        # Rules/habits are authoritative in Rules V2.  Canonical text is safe
        # to show in this authorized GUI response, but is never persisted into
        # ProjectionStore.
        rule_error = ""
        try:
            rule_snapshot = self._gui_rule_snapshot({}, context)
            rule_rows = [
                dict(item) for item in rule_snapshot.get("rules", ())
                if isinstance(item, Mapping)
            ]
        except NativePortError as exc:
            rule_rows = []
            rule_error = exc.code
        rules_total = len(rule_rows)
        add_node({
            "id": "virtual-rules-habits",
            "node_kind": "virtual_category",
            "record_kind": "rules_habits",
            "virtual_category": "rules_habits",
            "kind": "rules_habits",
            "label": "规则与习惯",
            "parent_id": "main",
            "count": rules_total,
            "total": rules_total,
            "load_error": rule_error,
            "derivation": "记忆胞体 -> 规则与习惯",
        })
        add_edge("main", "virtual-rules-habits")
        for row in rule_rows[:100]:
            definition_id = _text(row.get("definition_id"))
            if not definition_id:
                continue
            body = _text(row.get("canonical_text"))
            memory_id = _text(row.get("memory_id"))
            bindings = [dict(item) for item in row.get("bindings", ()) if isinstance(item, Mapping)]
            node_id = "virtual-rule-ref:" + definition_id
            add_node({
                "id": node_id,
                "node_kind": "virtual_rule_ref",
                "canonical": True,
                "canonical_node_kind": "canonical_rule",
                "record_kind": "rules_habits",
                "virtual_category": "rules_habits",
                "parent_id": "virtual-rules-habits",
                "definition_id": definition_id,
                "memory_id": memory_id,
                "source_memory_ids": list(row.get("source_memory_ids") or ()),
                "label": (body.splitlines()[0][:160] if body else definition_id[:24]),
                "body": body[:6000],
                "kind": _text(row.get("rule_kind")) or "procedure",
                "status": _text(row.get("maturity_state")) or "active",
                "polarity": _text(row.get("polarity")),
                "rule_strength": row.get("rule_strength"),
                "revision": row.get("revision"),
                "bindings": bindings,
                "assignments": bindings,
                "effective": bool(row.get("effective")),
                "excluded": bool(row.get("excluded")),
                "detail_branches": self._canonical_rule_detail_branches(row),
                "injection_policy": "relevant",
                "priority": 0,
                "derivation": "记忆胞体 -> 规则与习惯 -> 规则",
            })
            add_edge("virtual-rules-habits", node_id)

        # Conversation history is authoritative in Content V2.  The graph gets
        # only session metadata/summary counters; raw turns remain behind the
        # explicit history_read permission path.
        history_error = ""
        history_listing: Mapping[str, Any] = {}
        try:
            history = self._native_service("history")
            if history is None:
                raise NativePortError("v2_history_service_unavailable")
            history_listing = self._service_result(
                history,
                "memoryguard_history_list_sessions",
                {"limit": 100, "offset": 0},
                context=context,
            )
        except NativePortError as exc:
            history_error = exc.code
            history_listing = {}
        sessions = [
            dict(item) for item in history_listing.get("sessions", ())
            if isinstance(item, Mapping)
        ]
        history_total = int(history_listing.get("total") or len(sessions))
        add_node({
            "id": "virtual-conversation-history",
            "node_kind": "virtual_category",
            "record_kind": "conversation_history",
            "virtual_category": "conversation_history",
            "kind": "conversation_history",
            "label": "对话历史",
            "parent_id": "main",
            "count": history_total,
            "total": history_total,
            "has_more": history_total > len(sessions),
            "load_error": history_error,
            "derivation": "记忆胞体 -> 对话历史",
        })
        add_edge("main", "virtual-conversation-history")

        project_nodes: dict[str, str] = {}
        agent_nodes: dict[tuple[str, str], str] = {}
        for session in sessions:
            session_id = _text(session.get("session_id"))
            if not session_id:
                continue
            project_key = _text(session.get("project_key")) or "unknown"
            project_id = "history-project:" + project_key
            if project_key not in project_nodes:
                project_nodes[project_key] = project_id
                add_node({
                    "id": project_id,
                    "node_kind": "history_project",
                    "record_kind": "conversation_history",
                    "virtual_category": "conversation_history",
                    "label": _text(session.get("project_label")) or "未标注项目",
                    "project_ref": _text(session.get("project_ref")),
                    "project_status": _text(session.get("project_status")),
                    "parent_id": "virtual-conversation-history",
                    "session_count": 0,
                })
                add_edge("virtual-conversation-history", project_id)
            project_node = next(item for item in nodes if item.get("id") == project_id)
            project_node["session_count"] = int(project_node.get("session_count") or 0) + 1

            agent_id = _text(session.get("agent_instance_id")) or "unknown"
            agent_key = (project_key, agent_id)
            agent_node_id = "history-agent:" + project_key + ":" + agent_id
            if agent_key not in agent_nodes:
                agent_nodes[agent_key] = agent_node_id
                add_node({
                    "id": agent_node_id,
                    "node_kind": "history_agent",
                    "record_kind": "conversation_history",
                    "virtual_category": "conversation_history",
                    "label": agent_id[:24],
                    "agent_instance_id": agent_id,
                    "parent_id": project_id,
                })
                add_edge(project_id, agent_node_id)

            session_node_id = "history-session:" + session_id
            add_node({
                "id": session_node_id,
                "node_kind": "history_session",
                "record_kind": "conversation_history",
                "virtual_category": "conversation_history",
                "parent_id": agent_node_id,
                "session_id": session_id,
                "title": _text(session.get("title")),
                "label": _text(session.get("title")) or _text(session.get("provider")) or "对话",
                "provider": _text(session.get("provider")),
                "project_ref": _text(session.get("project_ref")),
                "agent_instance_id": agent_id,
                "created_at": _text(session.get("created_at")),
                "imported_at": _text(session.get("imported_at")),
                "summary": _text(session.get("summary"))[:1000],
                "turn_count": int(session.get("turn_count") or 0),
                "evidence_count": int(session.get("evidence_count") or 0),
            })
            add_edge(agent_node_id, session_node_id)

        stats = dict(result.get("stats") or {})
        stats.setdefault("memory_node_count", int(stats.get("node_count") or 0))
        stats["virtual_rule_count"] = rules_total
        stats["history_session_count"] = history_total
        stats["node_count"] = len(nodes)
        stats["edge_count"] = len(edges)
        result.update({
            "nodes": nodes,
            "edges": edges,
            "stats": stats,
            "virtual_overlay_available": True,
        })
        return result

    def _projection_graph(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read Memory Core graph from Memory Projection only.

        GUI positional compatibility may place mode in ``args``; all scope and
        member selectors are business input and the trusted context remains the
        sole ACL source.
        """
        scope = self._gui_projection_scope(context)
        mode = _text(payload.get("mode"))
        args = payload.get("args")
        if not mode and isinstance(args, (list, tuple)) and args:
            mode = _text(args[0])
        try:
            service = self._projection_service()
            graph = dict(service.graph(
                mode=mode or "reconstructed",
                scope=scope,
            ))
            # Memory Core renders the build gate and source table from one
            # consistent scope snapshot.  Returning only the projection made
            # every unbuilt graph look source-less in the desktop UI even when
            # the selected shared group already contained governed memories.
            graph["source_map"] = service.source_map(scope=scope)
            graph["scope"] = {
                "workspace_id": str(scope.workspace_id),
                "agent_instance_id": str(scope.agent_instance_id or ""),
                "share_group_id": str(scope.share_group_id or ""),
                "project_ref": str(scope.project_ref or ""),
                "provider": str(scope.provider or ""),
            }
            if not graph.get("nodes"):
                summary = dict(graph["source_map"].get("summary") or {})
                graph["reason"] = (
                    "not_built"
                    if int(summary.get("buildable_atom_count") or 0) > 0
                    else "no_projection_sources"
                )
            if self._trusted_admin(context) and _text(context.get("entrypoint")).casefold() == "gui":
                return self._with_virtual_neuron_overlay(graph, context)
            return graph
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_projection_graph_unavailable",
            ) from exc

    def _provider_install(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Repair one provider integration without constructing V1 memory.

        Provider configuration is host control-plane state, not a V1/V2 data
        store.  The active AgentBinding is reused as the connection boundary;
        this route never creates a personal shared-memory group.
        """
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        provider = _text(payload.get("target_provider")).casefold()
        try:
            from ..provider_adapters import (
                ClaudeAdapter, CodexAdapter, CursorAdapter, TraeAdapter,
                resolve_provider_binding,
            )

            adapter_cls = {
                "claude": ClaudeAdapter,
                "codex": CodexAdapter,
                "cursor": CursorAdapter,
                "trae": TraeAdapter,
            }.get(provider)
            if adapter_cls is None:
                raise NativePortError("unknown_provider")
            control = self._group_service(write=True)
            binding = resolve_provider_binding(control, provider)
            result = adapter_cls(self.workspace).install(
                self.workspace,
                share_group_id=_text(binding.get("share_group_id")),
                agent_instance_id=_text(binding.get("agent_instance_id")),
                global_scope=True,
            )
            if not isinstance(result, Mapping):
                raise NativePortError("provider_install_failed")
            from ..agent_mapping import normalize_program_identity
            control.record_provider_identity(
                normalize_program_identity(provider)["program_id"],
                _text(binding.get("agent_instance_id")),
                _text(binding.get("share_group_id")),
            )
            # Do not expose absolute host config paths over the transport.
            return {
                "provider": provider,
                "status": str(result.get("status") or "configured"),
                "restart_required": bool(result.get("restart_required", True)),
                "runtime_verified": bool(result.get("runtime_verified", False)),
                "binding_id": str(result.get("binding_id") or ""),
                "hook_configured": bool(result.get("hook_configured", False)),
                "hook_runtime_verified": bool(result.get("hook_runtime_verified", False)),
                "warnings": list(result.get("warnings") or []),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("provider_install_failed") from exc

    def _semantic_check(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Compare input text against scoped V2 memory atoms without bodies out."""

        text = _text(payload.get("text"))
        if not text or len(text) > 8192:
            raise NativePortError("semantic_text_required")
        threshold_value = payload.get("threshold", 0.85)
        try:
            threshold = float(threshold_value)
        except (TypeError, ValueError) as exc:
            raise NativePortError("invalid_semantic_threshold") from exc
        if not 0.0 <= threshold <= 1.0:
            raise NativePortError("invalid_semantic_threshold")
        scope = self._scope(context)
        try:
            from ..semantic_dedup import HashBackend, cosine_similarity

            store = self._domain_store("memory")
            backend = HashBackend()
            new_vector = backend.embed_text(text)
            atoms = store.list_atoms(scope=scope, status="active")
            duplicates: list[dict[str, Any]] = []
            for atom in atoms:
                similarity = cosine_similarity(new_vector, backend.embed_text(str(atom.body or "")))
                if similarity < threshold:
                    continue
                kind = getattr(atom.kind, "value", str(atom.kind))
                duplicates.append({
                    "memory_id": atom.memory_id,
                    "similarity": similarity,
                    "kind": kind,
                })
            duplicates.sort(key=lambda item: (-float(item["similarity"]), str(item["memory_id"])))
            kind_filter = _text(payload.get("kind"))
            conflicts = [item for item in duplicates if kind_filter and item["kind"] != kind_filter]
            return {
                "duplicates": duplicates,
                "conflicts": conflicts,
                "checked_against": len(atoms),
                "threshold": threshold,
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_semantic_read_unavailable") from exc

    @staticmethod
    def _scoped_read_context(context: Mapping[str, Any]) -> tuple[str, str]:
        agent = _text(context.get("agent_instance_id"))
        group = _text(context.get("share_group_id"))
        if not agent and not group:
            raise NativePortError("native_scope_required")
        return agent, group

    def _extraction_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        service = self._native_service("extraction")
        if service is None:
            raise NativePortError("v2_extraction_service_unavailable")
        return self._service_result(service, operation, payload, context=context, **kwargs)

    def _extract_memories(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_extract_memories", payload, context, **kwargs)

    def _gui_extract_preview(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        root_id = _text(payload.get("root_id"))
        relative = _text(payload.get("relative_path"))
        if not root_id:
            raise NativePortError("source_root_required")
        return self._extract_memories({"source_id": root_id, "relative_path": relative}, context, **kwargs)

    def _gui_extract_by_path(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        source_path = _text(payload.get("source_path") or payload.get("path"))
        if not source_path:
            raise NativePortError("source_path_required")
        # Selection-tree paths are redacted at the GUI boundary.  The browser
        # may send back the server-issued source token plus the selected Agent
        # instance; resolve it against a fresh tree before extraction.
        if source_path.startswith("agent-source-"):
            extra = payload.get("args")
            instance_id = _text(payload.get("agent_instance_id"))
            if not instance_id and isinstance(extra, (list, tuple)) and extra:
                instance_id = _text(extra[0])
            if not instance_id:
                raise NativePortError("agent_instance_required")
            try:
                source_path = self._agent_service().resolve_source_path(instance_id, source_path)
            except Exception as exc:
                code = _text(getattr(exc, "code", "")) or "agent_source_not_found"
                raise NativePortError(code) from exc
        return self._extract_memories({"source_path": source_path}, context, **kwargs)

    def _accept_candidates(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_accept_candidates", payload, context, **kwargs)

    def _list_pending_enrichments(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not self.layout.content_db.is_file():
            return {
                "pending_count": 0,
                "tasks": [],
                "next_step": "classify/translate then call memoryguard_apply_enrichments",
                "storage": "v2_content_plane",
                "status": "NO_SOURCE",
            }
        return self._extraction_operation("memoryguard_list_pending_enrichments", payload, context, **kwargs)

    def _apply_enrichments(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_apply_enrichments", payload, context, **kwargs)

    def _enrichment_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not _text(context.get("share_group_id")):
            return {
                "pending": 0,
                "applied": 0,
                "other": 0,
                "total": 0,
                "mode": "v2_content_plane",
                "status": "UNBOUND",
            }
        if not self.layout.content_db.is_file():
            return {"pending": 0, "applied": 0, "other": 0, "total": 0, "mode": "v2_content_plane", "status": "NO_SOURCE"}
        return self._extraction_operation("memoryguard_enrichment_status", payload, context, **kwargs)

    def _build_and_enrich(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_build_and_enrich", payload, context, **kwargs)

    def _resolve_group(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        agent, _group = self._scoped_read_context(context)
        if not agent:
            raise NativePortError("native_agent_scope_required")
        try:
            binding = self._group_service(write=False).active_binding_for_agent(agent)
            if binding is None:
                return {"share_group_id": None, "binding_id": None, "native_memory_mode": None}
            return {
                "share_group_id": binding.get("share_group_id"),
                "binding_id": binding.get("binding_id"),
                "native_memory_mode": binding.get("native_memory_mode"),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_binding_read_unavailable") from exc

    def _status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Status surfaces are neutral reads.  Coverage is exposed separately
        # through the machine-readable ``coverage()`` provider; returning it
        # here would turn a tenant-neutral operation into a global count and
        # activation-state oracle.
        del payload, context
        return {"available": True}

    @staticmethod
    def _sandbox_status(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read sandbox flag without constructing any workspace service."""

        del payload, context
        try:
            from ..security import detect_sandbox_mode

            return {"sandbox": bool(detect_sandbox_mode())}
        except Exception as exc:
            raise NativePortError("v2_sandbox_status_unavailable") from exc

    def _host_enrichment_guide(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Return bounded host guidance using existing read-only enrichment status."""

        status = self._enrichment_status(payload, context)
        pending = status.get("pending", 0)
        applied = status.get("applied", 0)
        if type(pending) is not int or type(applied) is not int:
            raise NativePortError("v2_enrichment_read_unavailable")
        return {
            "ok": True,
            "mode": "host_enrichment_queue",
            "pending_count": pending,
            "applied_count": applied,
            "message": (
                f"host enrichment queue has {pending} pending items"
                if pending else "no pending enrichment items"
            ),
            "mcp_tools": [
                "memoryguard_build_and_enrich",
                "memoryguard_list_pending_enrichments",
                "memoryguard_apply_enrichments",
                "memoryguard_enrichment_status",
            ],
        }

    @staticmethod
    def _host_llm_agents(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Expose only real, currently executable host Agent CLIs.

        There is deliberately no synthetic ``host`` row: a GUI "skill" is not a
        synchronous executable LLM.  Each row carries a stable engine id, human
        name, capability mode, and a safe display hint -- never the full local
        executable path, which stays server-side for build dispatch.
        """

        del payload, context
        from ..host_agent_backend import detect_available_agents

        rows: list[dict[str, Any]] = []
        for agent in detect_available_agents() or []:
            engine_id = str(agent.get("agent") or "").strip()
            if not engine_id:
                continue
            rows.append({
                "agent": engine_id,
                "label": str(agent.get("label") or engine_id),
                "mode": "cli",
                "display": "本机 Agent CLI · 后台任务整理",
            })
        if not rows:
            return {
                "agents": [],
                "primary": "",
                "empty": True,
                "note": "未检测到可用的 Agent CLI（Cursor Agent / Codex / Claude Code / TRAE）",
            }
        return {
            "agents": rows,
            "primary": rows[0]["agent"],
            "empty": False,
        }

    @staticmethod
    def _resolve_engine_id(engine_id: str) -> dict[str, Any] | None:
        """Re-resolve a caller-supplied engine id against the fresh allowlist.

        The caller may only name an engine id; the concrete CLI path is always
        taken from :func:`detect_available_agents`, never from a browser payload.
        """
        from ..host_agent_backend import detect_available_agents

        requested = str(engine_id or "").strip()
        for agent in detect_available_agents() or []:
            if str(agent.get("agent") or "").strip() == requested:
                return dict(agent)
        return None

    def _canonical_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        group = _text(context.get("share_group_id"))
        if not group:
            # This is one of the four neutral diagnostics that may run before
            # an Agent binding exists.  Do not inspect a global canonical row;
            # report only that the scoped source is unavailable.
            return {
                "status": "NO_SOURCE",
                "share_group_id": "",
                "canonical_state": "unbound",
            }
        if not self.layout.rules_db.is_file():
            return {"status": "NO_SOURCE", "share_group_id": group, "canonical_state": "absent"}
        try:
            with open_database(self.layout.rules_db, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT scope_id,activation_status,read_path,canonical_digest,source_digest,effective_digest,runtime_digest,assessment_digest,policy_version,updated_at "
                    "FROM rule_canonical_state WHERE share_group_id=? ORDER BY updated_at DESC,scope_id DESC",
                    (group,),
                ).fetchall()
            if not rows:
                return {"status": "NO_SOURCE", "share_group_id": group, "canonical_state": "absent"}
            observed_read_path = _text(rows[0][2])
            selected = None
            selected_read_path = ""
            for candidate in rows:
                normalized_path = normalize_canonical_read_path(candidate[2])
                activation_status = _text(candidate[1]).casefold()
                if normalized_path == "rule-intelligence" and activation_status in {
                    "active", "canonical_ready", "ready", "shadow",
                }:
                    selected = candidate
                    selected_read_path = normalized_path
                    break
            if selected is None:
                return {
                    "status": "BLOCKED",
                    "share_group_id": group,
                    "canonical_state": "unavailable",
                    "read_path": "unknown",
                    "observed_read_path": observed_read_path,
                    "reason": "v2_canonical_read_path_unavailable",
                }
            result = {
                "status": "READY",
                "share_group_id": group,
                "canonical_state": str(selected[1] or ""),
                "read_path": selected_read_path,
                "canonical_digest": str(selected[3] or ""),
                "source_digest": str(selected[4] or ""),
                "effective_digest": str(selected[5] or ""),
                "runtime_digest": str(selected[6] or ""),
                "assessment_digest": str(selected[7] or ""),
                "policy_version": str(selected[8] or ""),
                "updated_at": str(selected[9] or ""),
                "observed_read_path": observed_read_path,
            }
            if observed_read_path and not normalize_canonical_read_path(observed_read_path):
                result["reason"] = "newer_invalid_canonical_state_ignored"
            return result
        except Exception as exc:
            raise NativePortError("v2_canonical_status_unavailable") from exc

    def _projection_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        if not _text(context.get("share_group_id")):
            return {
                "status": "UNBOUND",
                "scenario_heads": 0,
                "profile_heads": 0,
                "total_heads": 0,
            }
        scope = self._scope(context)
        result: dict[str, Any] = {"status": "READY", "scenario_heads": 0, "profile_heads": 0}
        configured = False
        try:
            for kind, path in (("scenario", self.layout.scenario_db), ("profile", self.layout.profile_db)):
                if not path.is_file():
                    continue
                configured = True
                clauses = ["a.workspace_id=?", "a.share_group_id=?"]
                params: list[Any] = [self.workspace, scope.get("share_group_id", "")]
                for column, value in (
                    ("a.agent_instance_id", scope.get("agent_instance_id", "")),
                    ("a.project_ref", scope.get("project_ref", "")),
                    ("a.provider", scope.get("provider", "")),
                ):
                    if value:
                        clauses.append(column + "=?")
                        params.append(value)
                with open_database(path, readonly=True) as conn:
                    count = int(conn.execute(
                        "SELECT COUNT(DISTINCT h.head_id) FROM projection_heads h "
                        "JOIN projection_acl a ON a.projection_id=h.current_projection_id "
                        "WHERE h.current_projection_id<>'' AND " + " AND ".join(clauses),
                        params,
                    ).fetchone()[0])
                result[f"{kind}_heads"] = count
            if not configured:
                result["status"] = "NO_SOURCE"
            result["total_heads"] = int(result["scenario_heads"]) + int(result["profile_heads"])
            return result
        except Exception as exc:
            raise NativePortError("v2_projection_status_unavailable") from exc

    def _diagnostics_snapshot(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return {
            "status": "READY",
            "memory": self._memory_status(payload, context),
            "canonical": self._canonical_status(payload, context),
            "projection": self._projection_status(payload, context),
            "enrichment": self._enrichment_status(payload, context),
        }

    def _governance_snapshot(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **_: Any,
    ) -> Any:
        """Return the bounded GUI governance status contract.

        The historical positional group selector is compatibility input only.
        The process-issued NativeBoundContext and its active binding are the
        sole scope authority; stale contexts never become group existence
        or record-count oracles.
        """
        del payload
        authority = self._gui_authority(context)
        agent_id = _text(authority.agent_instance_id)
        members: list[str] = []
        memory: dict[str, Any] = {
            "status": "UNBOUND",
            "available": False,
            "total_records": 0,
            "active_count": 0,
            "status_counts": {},
        }
        governance_state = "audit_only"
        group_id = ""

        try:
            groups = self._group_service(write=False)
            # Resolve the persisted GUI selection from the control plane.  The
            # server-admin authority is only allowed to validate that saved
            # selection; its own binding/group is never used as the scope.
            scope_state = groups.scope_state(agent_id, admin=bool(authority.admin)) if agent_id else {}
            binding = scope_state.get("active_binding") if isinstance(scope_state, Mapping) else None
            bound_group = _text((binding or {}).get("share_group_id"))
            persisted_scope = scope_state.get("scope") if isinstance(scope_state, Mapping) else None
            if (
                isinstance(binding, Mapping)
                and isinstance(persisted_scope, Mapping)
                and bound_group
                and scope_state.get("empty") is False
            ):
                members = sorted({
                    _text(item)
                    for item in (scope_state.get("members") or [])
                    if _text(item)
                })
                if not members:
                    raise NativePortError("governance_scope_empty")
                scoped_authority = authority.to_dict()
                # Keep the process-issued capability attached while changing
                # only the business selector.  _memory_status intentionally
                # derives its admin bit from this immutable capability rather
                # than trusting the copied mapping's ``admin`` field.
                for private_key in ("__native_bound_context", "__native_transport_capability"):
                    if private_key in context:
                        scoped_authority[private_key] = context[private_key]
                # Shared memory is one canonical group plane.  The binding's
                # Agent identifies the selected member, not an atom-owner
                # filter.  Personal groups keep their exact Agent scope.
                shared_group = _text(binding.get("group_kind")).casefold() == "shared"
                trusted_admin = self._trusted_admin(context)
                # A shared-group selection is an all-member memory-plane read
                # only for the trusted server-admin bridge.  Ordinary Agents
                # retain their selected-member ACL even when the control-plane
                # binding is marked shared.
                scoped_authority["agent_instance_id"] = (
                    ""
                    if shared_group and trusted_admin
                    else _text(binding.get("agent_instance_id"))
                )
                scoped_authority["trusted_agent_id"] = scoped_authority["agent_instance_id"]
                scoped_authority["share_group_id"] = bound_group
                scoped_authority["admin"] = bool(shared_group and trusted_admin)
                scoped_authority["is_admin"] = bool(shared_group and trusted_admin)
                memory = dict(self._memory_status({}, scoped_authority))
                governance_state = "active_governance"
                group_id = bound_group
        except Exception:
            # A diagnostics endpoint remains renderable when its control plane
            # is unavailable; it must not fall back to caller-selected data.
            pass

        conflict_count = 0
        quarantine_count = 0
        if governance_state == "active_governance":
            try:
                governance = self._governance_native()
                trusted = authority.to_dict()
                trusted["agent_instance_id"] = _text((binding or {}).get("agent_instance_id"))
                trusted["trusted_agent_id"] = trusted["agent_instance_id"]
                trusted["share_group_id"] = group_id
                conflicts = governance.conflicts(trusted)
                quarantine = governance.quarantine(trusted)
                # ``total`` includes stale historical groups so the queue can
                # explain them.  The overview KPI is an actionable metric;
                # only fall back for pre-snapshot compatibility responses.
                if "actionable_total" in conflicts:
                    conflict_count = int(conflicts.get("actionable_total") or 0)
                else:
                    conflict_count = int(conflicts.get("total") or len(conflicts.get("conflicts", [])))
                quarantine_count = int(quarantine.get("total") or len(quarantine.get("quarantine", [])))
            except Exception:
                # Missing governance data is a stable empty queue, not a raw
                # exception or record-bearing diagnostic.
                pass

        active_count = int(
            memory.get("active_count")
            or (memory.get("status_counts") or {}).get("active", 0)
            or 0
        )
        return {
            "status": {"active_count": max(0, active_count)},
            "conflicts": {"count": max(0, conflict_count)},
            "quarantine": {"count": max(0, quarantine_count), "items": []},
            "rollback_ready": 0,
            "has_events": False,
            "latest_event": None,
            "latest_supersede": None,
            "governance_state": governance_state,
            "scope_state": governance_state,
            "group": {
                "share_group_id": group_id,
                "members": members,
                "member_count": len(members),
            },
            "memory": memory,
        }

    def _scope_echo(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Return the trusted GUI scope without exposing an absolute workspace."""
        del payload
        return {
            "available": True,
            "agent_instance_id": _text(context.get("agent_instance_id")),
            "share_group_id": _text(context.get("share_group_id")),
            "project_ref": _text(context.get("project_ref")),
            "provider": _text(context.get("provider")),
            "runtime_role": _text(context.get("runtime_role")),
            "admin": bool(context.get("admin", context.get("is_admin", False))),
        }

    def _hook_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._hook_control()
        try:
            return service.status(
                provider=_text(payload.get("target_provider") or payload.get("provider")),
                target_agent_id=_text(payload.get("target_agent_id")),
            )
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_hook_status_failed"
            ) from exc

    def _gui_host_control(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **_: Any,
    ) -> Any:
        if payload.get("confirmed") is not True:
            raise NativePortError("hook_confirmation_required")
        service = self._hook_control()
        try:
            if operation == "host_hook_mode_set":
                return service.set_mode(
                    _text(payload.get("target_provider") or payload.get("provider")),
                    _text(payload.get("target_agent_id")),
                    _text(payload.get("mode")),
                    admin=self._trusted_admin(context),
                )
            if operation == "host_hook_uninstall":
                return service.uninstall(
                    _text(payload.get("target_provider") or payload.get("provider")),
                    admin=self._trusted_admin(context),
                )
            raise NativePortError("unknown_hook_control_operation")
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_hook_control_failed"
            ) from exc

    def _domain_status(self, domain: str, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if domain == "projection":
            return self._projection_status(payload, context)
        # Non-projection compatibility statuses remain bounded availability
        # envelopes until their public surfaces are promoted separately.
        del domain, payload, context
        return {"available": True}

    # ---- Phase 9 native service adapters ------------------------------------
    def _native_service(self, kind: str) -> Any:
        """Lazily construct one production native service.

        Construction errors are recorded as bounded codes and converted to a
        stable native blocker by the operation wrapper.  In particular, a
        missing history DB/config must not be repaired merely by constructing
        the port.
        """

        attr = f"_{kind}_service"
        current = getattr(self, attr)
        if current is not None:
            return current
        if kind in self._native_service_init_errors:
            return None
        try:
            if kind == "history":
                from .history_native import NativeHistoryService

                current = NativeHistoryService(self.workspace)
            elif kind in {"source_read", "import_preview"}:
                from .source_control import SourceControlService

                current = SourceControlService(self.workspace)
            elif kind == "runtime_diagnostics":
                from .safe_services import RuntimeDiagnosticsService

                current = RuntimeDiagnosticsService(self.workspace)
            elif kind == "external_mcp":
                from .external_mcp_native import NativeExternalMCPService

                current = NativeExternalMCPService(self.workspace)
            elif kind == "rule_merge":
                from .rule_merge_native import NativeRuleMergeService

                injected = self._stores.get("rule_merge")
                capability = (
                    bind_native_test_capability(rule_merge_store=injected)
                    if injected is not None else None
                )
                current = NativeRuleMergeService(
                    self.workspace,
                    rule_store=capability,
                    state_provider=self.state_provider,
                )
            elif kind == "rule_lifecycle":
                from .rule_lifecycle_native import NativeRuleLifecycleService

                current = NativeRuleLifecycleService(self.workspace)
            elif kind == "extraction":
                from .extraction_native import NativeExtractionEnrichmentService

                current = NativeExtractionEnrichmentService(
                    self.workspace,
                    content_store=self._domain_store("content", write=True),
                    memory_store=self._domain_store("memory", write=True),
                    governance=self._governance_boundary(),
                )
            elif kind == "knowledge":
                from ..knowledge_v2.service import KnowledgeV2ReadonlyService

                current = KnowledgeV2ReadonlyService(self.workspace)
            else:  # pragma: no cover - registry controls this value
                self._native_service_init_errors[kind] = "v2_native_service_unavailable"
                return None
        except NativePortError as exc:
            # Preserve the stable blocker code without exposing constructor
            # details such as absolute paths.  Callers can then surface the
            # real remediation instead of collapsing every failure to a
            # generic service-unavailable message.
            self._native_service_init_errors[kind] = exc.code
            return None
        except Exception:
            # Do not expose constructor exception details (which can include
            # absolute paths).  Service operation wrappers emit the stable
            # kind-specific code below.
            self._native_service_init_errors[kind] = f"v2_{kind}_service_unavailable"
            return None
        setattr(self, attr, current)
        return current

    @staticmethod
    def _service_result(service: Any, operation: str, payload: Mapping[str, Any], *, context: Mapping[str, Any], **kwargs: Any) -> Any:
        """Invoke a native service and unwrap its transport envelope.

        ``NativeV2RuntimePort`` owns the outer V2 envelope.  Builtin services
        retain their stable ``status/service/code`` fields inside the returned
        data while service failures become the port's bounded ``NativePortError``.
        """

        dispatch = getattr(service, "dispatch", None) or getattr(service, "call", None)
        if not callable(dispatch):
            raise NativePortError("v2_native_service_unavailable")
        try:
            result = dispatch(operation, payload, context=context, **kwargs)
        except NativePortError:
            raise
        except Exception as exc:
            # Service implementations already expose stable error envelopes;
            # an unexpected exception is intentionally collapsed at this seam.
            raise NativePortError("v2_native_service_failed") from exc
        if not isinstance(result, Mapping):
            raise NativePortError("v2_native_service_failed")
        if result.get("ok") is False or _text(result.get("status")).casefold() in {"error", "blocked", "failed"}:
            code = _text(result.get("code") or result.get("error"))
            raise NativePortError(code or "v2_native_service_failed")
        # NativeHistoryService returns {ok,status,operation,data}; preserve
        # only the business data so the native port adds one canonical envelope.
        if "data" in result and isinstance(result.get("data"), Mapping):
            return result["data"]
        return result

    def _history_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        mutation: bool = False,
        **_: Any,
    ) -> Any:
        service = self._native_service("history")
        if service is None:
            raise NativePortError("v2_history_service_unavailable")
        return self._service_result(
            service,
            operation,
            payload,
            context=context,
            generation=generation,
            state=state,
            mutation=mutation,
        )

    @staticmethod
    def _history_neutral(handler: str) -> dict[str, Any]:
        operation = handler.removeprefix("history_")
        if operation == "search":
            return {"query": "", "results": [], "limit": 0, "offset": 0, "neutral": True}
        if operation == "timeline":
            return {"session_id": "", "anchor_turn_id": "", "radius": 0, "turns": [], "neutral": True}
        if operation == "read":
            return {"turn": None, "session": None, "turns": [], "neutral": True}
        if operation == "extract_preview":
            return {"session_id": "", "candidates": [], "written_to_long_term_memory": False, "neutral": True}
        if operation == "list_sessions":
            return {"sessions": [], "project_groups": [], "total": 0, "limit": 0, "offset": 0, "neutral": True}
        if operation == "export":
            return {"format": "memoryguard-history-v1", "sessions": [], "neutral": True}
        return {"neutral": True}

    @staticmethod
    def _source_neutral(handler: str) -> dict[str, Any]:
        if handler == "list_sources":
            return {"ok": True, "status": "NO_SOURCE", "service": "source_read", "sources": [], "total": 0}
        return {"ok": True, "status": "NO_SOURCE", "service": "source_read", "coverage": {"candidate_count": 0}, "roots": []}

    def _list_sources(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        return self._service_result(service, "list_sources", payload, context=context)

    def _scan_summary(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        return self._service_result(service, "scan_summary", payload, context=context)

    def _import_preview(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Import preview resolves configured roots and therefore is scoped even
        # though the operation is read-only.  Require the process-issued
        # authority before allowing the service to inspect a requested path;
        # missing/forged bindings fail closed without a path oracle.
        try:
            resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc
        service = self._native_service("import_preview")
        if service is None:
            raise NativePortError("v2_import_preview_service_unavailable")
        return self._service_result(service, "preview_path", payload, context=context)

    def _runtime_processes(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._native_service("runtime_diagnostics")
        if service is None:
            raise NativePortError("v2_runtime_diagnostics_service_unavailable")
        # Diagnostics summary is available without a binding, but admin detail
        # is only enabled by the immutable authority.  Strip forged mapping
        # admin flags while preserving the canonical authority projection.
        try:
            authority = resolve_native_transport_context(context)
            runtime_context: Mapping[str, Any] = {
                "admin": bool(authority.admin),
                "is_admin": bool(authority.is_admin),
            }
        except NativeContextError:
            runtime_context = {}
        return self._service_result(service, "memoryguard_runtime_processes", payload, context=runtime_context)

    def _external_mcp_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        service = self._native_service("external_mcp")
        if service is None:
            raise NativePortError("v2_external_mcp_service_unavailable")
        return self._service_result(service, operation, payload, context=context, **kwargs)

    def _external_mcp_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "memoryguard_external_mcp_list", payload, context, **kwargs,
        )

    def _external_mcp_detect(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "detect_external_mcp", payload, context, **kwargs,
        )

    def _external_mcp_preview(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "preview_external_mcp_import", payload, context, **kwargs,
        )

    def _external_mcp_import(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "memoryguard_external_mcp_import", payload, context, **kwargs,
        )

    def _rule_merge_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        mutation: bool = True,
        **_: Any,
    ) -> Any:
        service = self._native_service("rule_merge")
        if service is None:
            raise NativePortError("v2_rule_merge_service_unavailable")
        return self._service_result(
            service,
            operation,
            payload,
            context=context,
            generation=generation,
            state=state,
            mutation=False if operation == "merge_safe_preview" else mutation,
        )

    def _memory_merge_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        mutation: bool = True,
        **_: Any,
    ) -> Any:
        from .memory_merge_native import NativeMemoryMergeService

        write = operation != "merge_safe_preview"
        if write:
            self._preflight_memory_governance()
        service = NativeMemoryMergeService(
            self.workspace,
            memory_store=self._domain_store("memory", write=write),
            governance=self._governance_boundary() if write else None,
            state_provider=self.state_provider,
        )
        if write:
            scope = self._scope(context)
            with _native_memory_mutation_lock(self.workspace, scope["share_group_id"]):
                return self._service_result(
                    service,
                    operation,
                    payload,
                    context=context,
                    generation=generation,
                    state=state,
                    mutation=mutation,
                )
        return self._service_result(
            service,
            operation,
            payload,
            context=context,
            generation=generation,
            state=state,
            mutation=False,
        )

    @staticmethod
    def _binding_matches_context(binding: Any, context: Mapping[str, Any]) -> bool:
        target_type = _text(getattr(binding, "target_type", "")).casefold()
        target_id = _text(getattr(binding, "target_id", ""))
        project_ref = _text(getattr(binding, "project_ref", ""))
        provider = _text(getattr(binding, "provider", "")).casefold()
        runtime_role = _text(getattr(binding, "runtime_role", "")).casefold()
        if target_type == "system":
            return True
        if target_type == "group":
            return target_id in {"", _text(context.get("share_group_id"))}
        if target_type == "agent":
            return target_id == _text(context.get("agent_instance_id"))
        if target_type == "project":
            return canonical_project_ref(project_ref or target_id) == canonical_project_ref(context.get("project_ref"))
        if target_type == "agent_project":
            return (
                target_id == _text(context.get("agent_instance_id"))
                and canonical_project_ref(project_ref) == canonical_project_ref(context.get("project_ref"))
            )
        if target_type == "provider":
            return target_id.casefold() == _text(context.get("provider")).casefold()
        if target_type == "runtime_role":
            return target_id.casefold() == _text(context.get("runtime_role")).casefold()
        return False

    def _gui_rule_scope_options(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        admin = self._trusted_admin(context)
        target_types = ["agent", "agent_project"]
        if admin:
            target_types.extend(["group", "project", "provider", "runtime_role", "system"])

        current_agent = _text(context.get("agent_instance_id"))
        current_project = _text(context.get("project_ref"))
        current_provider = _text(context.get("provider"))
        current_runtime_role = _text(context.get("runtime_role"))
        current_group = _text(context.get("share_group_id"))

        binding_rows: list[Mapping[str, Any]] = []
        try:
            group_service = self._group_service(write=False)
            binding_result = group_service.list_bindings(include_inactive=False) if group_service is not None else {}
            if isinstance(binding_result, Mapping):
                binding_rows = [row for row in (binding_result.get("bindings") or ()) if isinstance(row, Mapping)]
        except Exception:
            binding_rows = []
        # Rule scope editing is bound to the current trusted request.  Do not
        # expose the global agent registry or unrelated active bindings here:
        # those are for Agent management/discovery, not rule target selection.
        discovered: dict[str, dict[str, Any]] = {}
        if current_agent:
            discovered[current_agent] = {
                "provider": current_provider,
                "project_ref": current_project,
            }
            # Rule target selection is still restricted to the trusted current
            # Agent, but its display metadata should come from the same
            # read-only registry used by the Agent UI.  Without this merge the
            # scope editor only had provider/project context and rendered
            # ``codex · project`` even when a readable program name existed.
            try:
                listed = self._agent_service().list_agents()
                candidates = listed.get("agents", ()) if isinstance(listed, Mapping) else ()
                for candidate in candidates:
                    if not isinstance(candidate, Mapping):
                        continue
                    candidate_id = _text(
                        candidate.get("instance_id")
                        or candidate.get("agent_instance_id")
                        or candidate.get("id")
                    )
                    if candidate_id == current_agent:
                        discovered[current_agent].update(candidate)
                        break
            except Exception:
                # Metadata is best effort; the trusted context remains enough
                # to render a safe fallback and does not affect authorization.
                pass

        def project_label(value: Any) -> str:
            text = _text(value).replace("\\", "/").rstrip("/")
            return text.rsplit("/", 1)[-1] if text else ""

        def identity_label(agent_id: str, row: Mapping[str, Any]) -> str:
            def readable(value: Any) -> str:
                text = _text(value)
                return text if text and text != agent_id else ""

            alias = next((readable(row.get(key)) for key in ("user_alias", "alias", "user_label") if readable(row.get(key))), "")
            discovered_name = next((readable(row.get(key)) for key in ("display_name", "product", "label", "name") if readable(row.get(key))), "")
            provider = readable(row.get("provider") or row.get("product"))
            project = readable(project_label(row.get("project_ref") or row.get("project")))
            base = alias or discovered_name
            if not base and provider and project:
                base = f"{provider} · {project}"
            if not base:
                suffix = agent_id[-4:] if len(agent_id) > 4 else ""
                base = f"未知助手 · {suffix}" if suffix else "未知助手"
            if agent_id == current_agent:
                base += "（当前）"
            return base

        agents: list[dict[str, Any]] = []
        for agent_id in sorted(discovered):
            row = discovered[agent_id]
            project_ref = _text(row.get("project_ref") or row.get("project"))
            last_seen = next((_text(row.get(key)) for key in ("last_seen", "last_activity_at", "mtime_iso", "updated_at") if _text(row.get(key))), "")
            agents.append({
                "id": agent_id,
                "label": identity_label(agent_id, row),
                "product": _text(row.get("product") or row.get("provider")),
                "current": agent_id == current_agent,
                "last_seen": last_seen,
                "project_ref": project_ref,
            })

        def option(value: Any, label: Any = "", **extra: Any) -> dict[str, Any]:
            item_id = _text(value)
            if not item_id:
                return {}
            return {"id": item_id, "label": _text(label) or item_id, **extra}

        group_ids = {current_group}
        project_refs = {current_project}
        providers = {current_provider}
        runtime_roles = {current_runtime_role}
        for binding in binding_rows:
            group_ids.add(_text(binding.get("share_group_id")))
            project_refs.add(_text(binding.get("project_ref")))
            providers.add(_text(binding.get("provider")))
            runtime_roles.add(_text(binding.get("runtime_role")))
        groups = [option(group, f"共享组 · …{group[-4:]}", current=group == current_group) for group in sorted(group_ids) if group]
        projects = [option(project, "当前项目" if project == current_project else project_label(project) or project, current=project == current_project) for project in sorted(project_refs) if project]
        provider_options = [option(provider, provider) for provider in sorted(providers) if provider]
        role_options = [option(role, role) for role in sorted(runtime_roles) if role]
        return {
            "target_types": target_types,
            "agents": agents,
            "groups": groups,
            "projects": projects,
            "providers": provider_options,
            "runtime_roles": role_options,
            "current_agent_id": current_agent,
            "current_project_ref": current_project,
            "current_project_available": bool(current_project),
            "connected_agent_count": len(agents),
            "automatic_scope_policy": ["agent", "agent_project"],
        }

    def _gui_rule_snapshot(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        if not self.layout.rules_db.is_file():
            return {"status": "NO_SOURCE", "rules": [], "effective": [], "excluded": [], "total": 0}
        try:
            store = self._domain_store("rules")
            group = _text(context.get("share_group_id"))
            bindings = [item for item in store.list_bindings(share_group_id=group, status="active")]
            raw_definitions = list(store.list_definitions(status="active"))
            definitions: dict[str, Any] = {}
            for item in raw_definitions:
                raw_id = _text(self._row_value(item, "definition_id"))
                canonical_id = self._canonical_rule_id(store, raw_id)
                if not canonical_id:
                    continue
                # Exact canonical head wins over an alias projection. Ties
                # resolve by revision, update time, then ID for determinism.
                try:
                    revision = int(getattr(item, "revision", 0) or 0)
                except (TypeError, ValueError):
                    revision = 0
                rank = (
                    0 if raw_id == canonical_id else 1,
                    -revision,
                    _text(self._row_value(item, "updated_at")),
                    raw_id,
                )
                previous = definitions.get(canonical_id)
                if previous is None:
                    definitions[canonical_id] = (rank, item)
                elif rank < previous[0]:
                    definitions[canonical_id] = (rank, item)
            definitions = {
                canonical_id: value[1]
                for canonical_id, value in definitions.items()
            }
            by_definition: dict[str, list[Any]] = {}
            for binding in bindings:
                canonical_id = self._canonical_rule_id(
                    store, self._row_value(binding, "definition_id"),
                )
                if canonical_id:
                    by_definition.setdefault(canonical_id, []).append(binding)
            source_memories: dict[str, list[str]] = {}
            links_reader = getattr(store, "list_source_links", None)
            if callable(links_reader):
                source_links = links_reader(share_group_id=group, status="active")
            else:
                with open_database(self.layout.rules_db, readonly=True) as conn:
                    source_links = [
                        {"memory_id": memory_id, "canonical_definition_id": definition_id}
                        for memory_id, definition_id in conn.execute(
                            "SELECT memory_id,canonical_definition_id FROM rule_source_links "
                            "WHERE share_group_id=? ORDER BY canonical_definition_id,memory_id",
                            (group,),
                        ).fetchall()
                    ]
            for link in source_links:
                mid = _text(self._row_value(link, "memory_id"))
                did = self._canonical_rule_id(
                    store, self._row_value(link, "canonical_definition_id"),
                )
                if mid and did in definitions:
                    source_memories.setdefault(did, []).append(mid)
            source_memories = {
                did: list(dict.fromkeys(values))
                for did, values in source_memories.items()
            }
            rules: list[dict[str, Any]] = []
            effective: list[dict[str, Any]] = []
            excluded: list[dict[str, Any]] = []
            for definition_id, scoped in sorted(by_definition.items()):
                definition = definitions.get(definition_id)
                if definition is None:
                    continue
                visible_bindings = [item for item in scoped if item.owner_agent_id in {"", _text(context.get("agent_instance_id"))} or self._trusted_admin(context)]
                if not visible_bindings:
                    continue
                includes = [item for item in visible_bindings if item.effect != "exclude" and self._binding_matches_context(item, context)]
                excludes = [item for item in visible_bindings if item.effect == "exclude" and self._binding_matches_context(item, context)]
                row = {
                    "definition_id": definition_id,
                    "canonical_text": definition.canonical_text,
                    "rule_kind": definition.rule_kind,
                    "polarity": definition.polarity,
                    "rule_strength": definition.rule_strength,
                    "maturity_state": definition.maturity_state,
                    "revision": definition.revision,
                    "bindings": [item.to_dict() for item in visible_bindings],
                    "memory_id": (source_memories.get(definition.definition_id) or [""])[0],
                    "source_memory_ids": list(source_memories.get(definition.definition_id) or ()),
                    "effective": bool(includes and not excludes),
                    "excluded": bool(excludes),
                }
                rules.append(row)
                if excludes:
                    excluded.append(row)
                elif includes:
                    effective.append(row)
            buckets: dict[str, list[dict[str, Any]]] = {
                "mandatory": [],
                "preferences": [],
                "procedures": [],
                "corrections": [],
                "projects": [],
            }
            for row in rules:
                bucket, _label = self._virtual_rule_bucket(row)
                buckets.setdefault(bucket, []).append(self._gui_rule_record(row))
            return {
                "status": "READY",
                "rules": rules,
                "buckets": buckets,
                "effective": effective,
                "excluded": excluded,
                "total": len(rules),
                "scope_options": self._gui_rule_scope_options({}, context),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_rule_snapshot_unavailable") from exc

    def _gui_rule_effective(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        snapshot = self._gui_rule_snapshot(payload, context, **kwargs)
        return {
            "status": snapshot.get("status", "READY"),
            "context": {
                "agent_instance_id": _text(context.get("agent_instance_id")),
                "share_group_id": _text(context.get("share_group_id")),
                "project_ref": _text(context.get("project_ref")),
                "provider": _text(context.get("provider")),
                "runtime_role": _text(context.get("runtime_role")),
            },
            "effective": list(snapshot.get("effective") or []),
            "excluded": list(snapshot.get("excluded") or []),
            "unavailable": [item for item in snapshot.get("rules", []) if not item.get("effective") and not item.get("excluded")],
        }

    def _rule_allowed_ids(self, conn: sqlite3.Connection, context: Mapping[str, Any]) -> set[str]:
        group = _text(context.get("share_group_id"))
        rows = conn.execute(
            "SELECT definition_id FROM rule_bindings WHERE share_group_id=?",
            (group,),
        ).fetchall()
        allowed = {str(row[0]) for row in rows if str(row[0] or "")}
        links = conn.execute(
            "SELECT memory_id,canonical_definition_id FROM rule_source_links WHERE share_group_id=?",
            (group,),
        ).fetchall()
        for row in links:
            if str(row[0] or ""):
                allowed.add(str(row[0]))
            if str(row[1] or ""):
                allowed.add(str(row[1]))
        return allowed

    def _gui_rule_decisions(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        raw_limit = payload.get("limit", 50)
        try:
            limit = max(1, min(int(raw_limit or 50), 200))
        except (TypeError, ValueError) as exc:
            raise NativePortError("invalid_limit") from exc
        if not self.layout.rules_db.is_file():
            return {"decisions": [], "total": 0, "status": "NO_SOURCE"}
        try:
            with open_database(self.layout.rules_db, readonly=True) as conn:
                allowed = self._rule_allowed_ids(conn, context)
                rows = conn.execute("SELECT * FROM rule_decisions ORDER BY created_at DESC,decision_id LIMIT ?", (limit * 4,)).fetchall()
            values: list[dict[str, Any]] = []
            agent = _text(context.get("agent_instance_id"))
            admin = self._trusted_admin(context)
            for row in rows:
                item = {str(key): row[key] for key in row.keys()}
                owner = _text(item.get("owner_agent_id") or item.get("actor"))
                rule_id = _text(item.get("rule_id"))
                if not admin and owner not in {"", agent}:
                    continue
                if not admin and not owner and rule_id not in allowed:
                    continue
                # Decision bodies are hash/JSON state, not source text; still
                # keep the public view compact and bounded.  Auto-scope facts
                # are persisted inside reason JSON, so project them into stable
                # fields instead of forcing every GUI client to parse it.
                reason_text = _text(item.get("reason"))
                reason_data: Mapping[str, Any] = {}
                try:
                    decoded_reason = json.loads(reason_text) if reason_text else {}
                    if isinstance(decoded_reason, Mapping):
                        reason_data = decoded_reason
                except (TypeError, ValueError, json.JSONDecodeError):
                    reason_data = {}
                assignment = reason_data.get("assignment")
                assignment = assignment if isinstance(assignment, Mapping) else {}
                scope_type = _text(assignment.get("target_type"))
                if not scope_type:
                    scope_type = "project" if _text(assignment.get("project_ref")) else "agent"
                confidence_value = reason_data.get("scope_confidence", item.get("confidence"))
                try:
                    scope_confidence = max(0.0, min(1.0, float(confidence_value)))
                except (TypeError, ValueError):
                    scope_confidence = 0.0
                public = {
                    key: item.get(key) for key in (
                        "decision_id", "actor", "owner_agent_id", "rule_id", "action",
                        "before_hash", "after_hash", "reason", "confidence", "undo_id",
                        "target_ids_json", "created_at",
                    )
                }
                public.update({
                    "scope_reason": _text(reason_data.get("scope_reason")) or reason_text,
                    "scope_confidence": scope_confidence,
                    "scope_type": scope_type or "agent",
                    "object_type": "rule",
                })
                values.append(public)
                if len(values) >= limit:
                    break
            return {"decisions": values, "total": len(values), "status": "READY"}
        except Exception as exc:
            raise NativePortError("v2_rule_decisions_unavailable") from exc

    def _gui_rule_receipts(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self.layout.rules_db.is_file():
            return {"receipts": [], "total": 0, "status": "NO_SOURCE"}
        try:
            limit = max(1, min(int(payload.get("limit", 50) or 50), 200))
            clauses = ["share_group_id=?"]
            params: list[Any] = [_text(context.get("share_group_id"))]
            memory_id = _text(payload.get("memory_id"))
            if memory_id:
                clauses.append("source_rule_id=?")
                params.append(memory_id)
            if not self._trusted_admin(context):
                clauses.append("agent_instance_id=?")
                params.append(_text(context.get("agent_instance_id")))
            params.append(limit)
            with open_database(self.layout.rules_db, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT receipt_id,definition_id,source_rule_id,share_group_id,agent_instance_id,project_ref,session_id,task_hash,selection_digest,created_at "
                    "FROM rule_receipt_refs WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC,receipt_id LIMIT ?",
                    params,
                ).fetchall()
            values = [
                {
                    "receipt_id": str(row[0]), "definition_id": str(row[1] or ""),
                    "source_rule_id": str(row[2] or ""), "share_group_id": str(row[3] or ""),
                    "agent_instance_id": str(row[4] or ""), "project_ref": str(row[5] or ""),
                    "session_id": str(row[6] or ""), "task_hash": str(row[7] or ""),
                    "selection_digest": str(row[8] or ""), "created_at": str(row[9] or ""),
                }
                for row in rows
            ]
            return {"receipts": values, "total": len(values), "status": "READY"}
        except Exception as exc:
            raise NativePortError("v2_rule_receipts_unavailable") from exc

    def _gui_rule_exceptions(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self.layout.rules_db.is_file():
            return {"exceptions": [], "total": 0, "status": "NO_SOURCE"}
        parent = _text(payload.get("parent_rule"))
        try:
            with open_database(self.layout.rules_db, readonly=True) as conn:
                allowed = self._rule_allowed_ids(conn, context)
                rows = conn.execute("SELECT * FROM rule_exceptions WHERE active=1 ORDER BY created_at,exception_id").fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                item = {str(key): row[key] for key in row.keys()}
                parent_id = _text(item.get("parent_rule_id") or item.get("parent_rule"))
                if parent_id not in allowed:
                    continue
                if parent and parent not in {parent_id, _text(item.get("parent_rule"))}:
                    continue
                values.append({
                    key: item.get(key) for key in (
                        "exception_id", "parent_rule_id", "child_exception_id", "priority",
                        "reason", "active", "created_at", "updated_at",
                    )
                })
            return {"exceptions": values, "total": len(values), "status": "READY"}
        except Exception as exc:
            raise NativePortError("v2_rule_exceptions_unavailable") from exc

    def _gui_rule_body_update(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Edit the injected definition, retaining source evidence and versions."""
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        definition_id = _text(payload.get("definition_id"))
        body = _text(payload.get("body"))
        expected = payload.get("expected_revision")
        if not definition_id or not body:
            raise NativePortError("rule_body_required")
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise NativePortError("expected_revision_required")
        from .mandatory_publication import snapshot, validate, MandatoryPublicationError
        from ..rule_reconciliation import settle_native_canonical_snapshot

        group = _text(context.get("share_group_id"))
        store = self._domain_store("rules", write=True)
        governance = self._governance_boundary()
        try:
            # Serialize against memory publications as well as rule writers.
            with _native_memory_mutation_lock(self.workspace, group), governance.atomic_memory():
                with store.transaction() as conn:
                    definition = store.get_definition(definition_id)
                    bindings = store.list_bindings(definition_id=definition_id)
                    groups = {item.share_group_id for item in bindings}
                    groups.update(str(row[0]) for row in conn.execute(
                        "SELECT DISTINCT share_group_id FROM rule_source_links WHERE canonical_definition_id=?",
                        (definition_id,),
                    ))
                    if group not in groups or definition is None:
                        raise NativePortError("rule_definition_not_found")
                    if groups != {group}:
                        raise NativePortError("rule_shared_definition_requires_scoped_replacement")
                    if definition.status != "active" or definition.superseded_by:
                        raise NativePortError("rule_definition_not_active")
                    if definition.revision != expected:
                        raise NativePortError("revision_conflict")
                    before = snapshot(self, context)
                    old = definition.to_dict()
                    store.record_definition_version(definition_id, snapshot=old,
                        reason="GUI body edit", actor=_text(context.get("agent_instance_id")))
                    store._update_composed_definition_conn(conn, definition, body)
                    # Rule identity normalization is for hashes/search only;
                    # injection must preserve the editor's readable claim.
                    conn.execute("UPDATE rule_definitions SET canonical_text=?,text=? WHERE definition_id=?",
                                 (body, body, definition_id))
                    updated = store.get_definition(definition_id)
                    after = updated.to_dict()
                    decision_id = hashlib.sha256(_canonical_json({
                        "action": "gui_rule_body_update", "group": group, "before": old, "after": after,
                    }).encode("utf-8")).hexdigest()
                    store.record_decision({
                        "decision_id": decision_id, "rule_id": definition_id,
                        "actor": _text(context.get("agent_instance_id")),
                        "owner_agent_id": _text(context.get("agent_instance_id")),
                        "action": "rule_body_update", "before_json": _canonical_json(old),
                        "after_json": _canonical_json(after), "before_hash": definition.semantic_hash,
                        "after_hash": updated.semantic_hash, "reason": "GUI body edit", "confidence": 1.0,
                        "metadata_json": _canonical_json({"share_group_id": group}),
                        "source_ref": "native-v2:gui:rule_body_update",
                    })
                    # Historical evidence digests refer to the historical sources;
                    # the new claim is anchored by this immutable version/decision.
                    settle_native_canonical_snapshot(self.workspace, group, store=store, reconcile=False)
                    budget = validate(self, context, before, rules_store=store)
            return {"ok": True, "definition_id": definition_id, "revision": updated.revision,
                    "decision_id": decision_id, "budget": budget}
        except MandatoryPublicationError as exc:
            raise NativePortError(exc.code) from exc
        except NativePortError:
            raise
        except Exception as exc:
            raise self._map_governance_error(exc) from exc

    def _gui_rule_audience_update(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        rule_id = _text(payload.get("memory_id") or payload.get("definition_id") or payload.get("rule_id"))
        assignments = payload.get("assignments")
        if not rule_id or not isinstance(assignments, list):
            raise NativePortError("rule_audience_payload_required")
        policy = _text(payload.get("injection_policy") or "always")
        if policy not in {"always", "relevant"}:
            raise NativePortError("invalid_injection_policy")
        if policy == "relevant" and payload.get("confirmed") is not True:
            raise NativePortError("confirmation_required")
        if policy == "relevant" and assignments:
            raise NativePortError("relevant_rule_cannot_have_assignments")
        if policy == "always" and not assignments:
            raise NativePortError("always_rule_requires_include_audience")
        try:
            store = self._domain_store("rules", write=True)
            definition_id = rule_id if store.get_definition(rule_id) is not None else ""
            if not definition_id:
                with open_database(self.layout.rules_db, readonly=True) as conn:
                    row = conn.execute(
                        "SELECT canonical_definition_id FROM rule_source_links WHERE share_group_id=? AND memory_id=? ORDER BY updated_at DESC LIMIT 1",
                        (_text(context.get("share_group_id")), rule_id),
                    ).fetchone()
                definition_id = str(row[0] or "") if row is not None else ""
            if not definition_id or store.get_definition(definition_id) is None:
                raise NativePortError("rule_definition_not_found")
            from ..rule_binding import RuleBinding
            group = _text(context.get("share_group_id"))
            existing = store.list_bindings(definition_id=definition_id, share_group_id=group)
            before = [item.to_dict() for item in existing]
            # Validate and materialize the complete replacement set before
            # opening the mutation transaction.  In particular, an unknown
            # target must not deactivate the previous binding as a side
            # effect of a rejected request.
            candidate_bindings: list[RuleBinding] = []
            if policy == "always":
                options = self._gui_rule_scope_options({}, context)
                allowed_types = set(options["target_types"])
                for raw in assignments:
                    if not isinstance(raw, Mapping):
                        raise NativePortError("invalid_rule_assignment")
                    target_type = _text(raw.get("target_type")).casefold()
                    target_id = _text(raw.get("target_id"))
                    project_ref = _text(raw.get("project_ref"))
                    effect = _text(raw.get("effect") or "include").casefold()
                    if target_type not in allowed_types or effect not in {"include", "exclude"}:
                        raise NativePortError("invalid_rule_assignment")
                    trusted_values = {
                        "agent": _text(context.get("agent_instance_id")),
                        "group": group,
                        "project": _text(context.get("project_ref")),
                        "provider": _text(context.get("provider")),
                        "runtime_role": _text(context.get("runtime_role")),
                    }
                    allowed_agent_ids = {trusted_values["agent"]} if trusted_values["agent"] else set()
                    if target_type in {"agent", "agent_project"} and self._trusted_admin(context):
                        try:
                            bindings = self._group_service().list_bindings(include_inactive=False).get("bindings", [])
                            allowed_agent_ids.update(
                                _text(item.get("agent_instance_id"))
                                for item in bindings
                                if isinstance(item, Mapping)
                                and _text(item.get("share_group_id")) == group
                                and _text(item.get("agent_instance_id"))
                            )
                        except NativePortError:
                            pass
                    if target_type == "agent" and target_id not in allowed_agent_ids:
                        raise NativePortError("unknown_agent_target")
                    if target_type == "group" and target_id not in {"", trusted_values["group"]}:
                        raise NativePortError("unknown_group_target")
                    if target_type == "project":
                        project_ref = canonical_project_ref(project_ref or target_id)
                        target_id = ""
                        if project_ref != canonical_project_ref(trusted_values["project"]):
                            raise NativePortError("unknown_project_target")
                    if target_type == "agent_project":
                        project_ref = canonical_project_ref(project_ref)
                        if target_id not in allowed_agent_ids or project_ref != canonical_project_ref(trusted_values["project"]):
                            raise NativePortError("unknown_agent_project_target")
                    if target_type == "provider" and target_id.casefold() != trusted_values["provider"].casefold():
                        raise NativePortError("unknown_provider_target")
                    if target_type == "runtime_role" and target_id.casefold() != trusted_values["runtime_role"].casefold():
                        raise NativePortError("unknown_runtime_role_target")
                    if target_type == "system" and (target_id or project_ref):
                        raise NativePortError("system_target_must_be_empty")
                    priority = int(raw.get("priority", payload.get("priority", 0)) or 0)
                    binding_id = hashlib.sha256(_canonical_json({
                        "definition_id": definition_id, "group": group, "target_type": target_type,
                        "target_id": target_id, "project_ref": project_ref, "provider": target_id if target_type == "provider" else "",
                        "runtime_role": target_id if target_type == "runtime_role" else "", "effect": effect, "priority": priority,
                    }).encode("utf-8")).hexdigest()[:32]
                    binding = RuleBinding(
                        binding_id=binding_id,
                        definition_id=definition_id,
                        share_group_id=group,
                        target_type=target_type,
                        target_id=target_id,
                        project_ref=project_ref,
                        provider=target_id if target_type == "provider" else "",
                        runtime_role=target_id if target_type == "runtime_role" else "",
                        effect=effect,
                        priority=priority,
                        owner_agent_id=_text(context.get("agent_instance_id")),
                        created_by="admin",
                        authorization=f"native-gui:{_text(context.get('session_id'))}",
                        status="active",
                    )
                    candidate_bindings.append(binding)
            # Preserve history by deactivating prior bindings rather than
            # deleting permission evidence.  All replacement writes and the
            # decision record share one V2 rules transaction; a failure in any
            # candidate therefore rolls back the whole audience mutation.
            with store.transaction():
                for binding in existing:
                    if binding.status == "active":
                        store.upsert_binding(RuleBinding.from_dict({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1}))
                created = [store.upsert_binding(binding).to_dict() for binding in candidate_bindings]
                after = created
                decision_id = hashlib.sha256(_canonical_json({"operation": "gui_rule_audience_update", "definition_id": definition_id, "before": before, "after": after}).encode("utf-8")).hexdigest()
                store.record_decision({
                    "decision_id": decision_id,
                    "actor": _text(context.get("agent_instance_id")),
                    "owner_agent_id": _text(context.get("agent_instance_id")),
                    "rule_id": definition_id,
                    "action": "rule_audience_update",
                    "before_hash": hashlib.sha256(_canonical_json(before).encode("utf-8")).hexdigest(),
                    "after_hash": hashlib.sha256(_canonical_json(after).encode("utf-8")).hexdigest(),
                    "before_json": _canonical_json(before),
                    "after_json": _canonical_json(after),
                    "reason": "native GUI rule audience update",
                    "confidence": 1.0,
                    "undo_id": "",
                    "target_ids_json": _canonical_json([item.get("binding_id") for item in after]),
                    "metadata_json": _canonical_json({"injection_policy": policy}),
                    "source_ref": "native-v2:gui:rule_audience_update",
                })
                # Snapshot publication shares this SQLite transaction.  A
                # missing provenance gate or publication fault rolls the
                # audience replacement back instead of returning a false
                # failure after partially committing the new bindings.
                from ..rule_reconciliation import settle_native_canonical_snapshot
                settle_native_canonical_snapshot(
                    self.workspace, group, store=store,
                )
            return {"ok": True, "definition_id": definition_id, "injection_policy": policy, "bindings": after, "decision_id": decision_id}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_rule_audience_update_failed") from exc

    def _gui_rule_create(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        text = _text(payload.get("text"))
        if not text:
            raise NativePortError("rule_text_required")
        clean = {
            "text": text,
            "idempotency_key": _text(payload.get("idempotency_key")) or "gui-rule-create:" + hashlib.sha256(
                _canonical_json({"text": text, "agent": context.get("agent_instance_id", ""), "project": context.get("project_ref", "")}).encode("utf-8")
            ).hexdigest(),
        }
        return self._rule_lifecycle_operation("rule_create_auto", clean, context, **kwargs)

    def _gui_rule_feedback(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        clean = {
            "receipt_id": _text(payload.get("receipt_id")),
            "outcome": _text(payload.get("outcome")),
            "evidence": str(payload.get("evidence") or ""),
            "confidence": payload.get("confidence", 1.0),
        }
        clean["idempotency_key"] = _text(payload.get("idempotency_key")) or "gui-rule-feedback:" + hashlib.sha256(
            _canonical_json({"receipt_id": clean["receipt_id"], "outcome": clean["outcome"], "evidence": clean["evidence"], "agent": context.get("agent_instance_id", "")}).encode("utf-8")
        ).hexdigest()
        return self._rule_lifecycle_operation("rule_feedback", clean, context, **kwargs)

    def _gui_rule_undo(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        decision_id = _text(payload.get("decision_id"))
        if not decision_id:
            raise NativePortError("decision_id_required")
        return self._rule_lifecycle_operation("rule_undo", {"decision_id": decision_id}, context, **kwargs)

    def _gui_rule_exception(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        operation: str = "",
        **kwargs: Any,
    ) -> Any:
        if payload.get("confirmed") is not True:
            raise NativePortError("confirmation_required")
        if operation == "rule_exception_revoke":
            exception_id = _text(payload.get("exception_id"))
            if not exception_id:
                raise NativePortError("exception_id_required")
            return self._rule_lifecycle_operation(
                "rule_exception_revoke",
                {"exception_id": exception_id},
                context,
                **kwargs,
            )
        parent = _text(payload.get("parent_rule") or payload.get("parent_rule_id"))
        child = _text(payload.get("child_rule") or payload.get("child_exception") or payload.get("text"))
        if not parent or not child:
            raise NativePortError("rule_exception_payload_required")
        return self._rule_lifecycle_operation(
            "rule_exception_create",
            {
                "parent_rule": parent,
                "child_rule": child,
                "priority": payload.get("priority", 0),
                "reason": _text(payload.get("reason")),
                "idempotency_key": _text(payload.get("idempotency_key")) or "gui-rule-exception:" + hashlib.sha256(
                    _canonical_json({
                        "parent": parent,
                        "child": child,
                        "priority": payload.get("priority", 0),
                        "agent": context.get("agent_instance_id", ""),
                        "project": context.get("project_ref", ""),
                    }).encode("utf-8")
                ).hexdigest(),
            },
            context,
            **kwargs,
        )

    def _rule_lifecycle_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        service = self._native_service("rule_lifecycle")
        if service is None:
            raise NativePortError("v2_rule_lifecycle_service_unavailable")
        return self._service_result(
            service, operation, payload, context=context, **kwargs,
        )

    @staticmethod
    def _bridge_transport(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return {"transport": "SafeBridgeApi", "available": True}

    @staticmethod
    def _gui_pick_path(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del context
        return {"host_action": "pick_path", "for_files": bool(payload.get("for_files", False)), "gated_by": "v2_native_manifest"}

    def _gui_source_add(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        return self._service_result(service, "source_add", payload, context=context)

    def _gui_source_remove(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        return self._service_result(service, "source_remove", payload, context=context)

    def _gui_memory_source_map(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        scope = self._scope(context)
        if not self.layout.memory_db.is_file():
            return {"status": "NO_SOURCE", "mappings": [], "total": 0}
        clauses = ["a.workspace_id=?", "a.share_group_id=?"]
        params: list[Any] = [self.workspace, scope.get("share_group_id", "")]
        for column, value in (
            ("a.agent_instance_id", scope.get("agent_instance_id", "")),
            ("a.project_ref", scope.get("project_ref", "")),
            ("a.provider", scope.get("provider", "")),
            ("a.runtime_role", scope.get("runtime_role", "")),
        ):
            if value:
                clauses.append(column + "=?")
                params.append(value)
        try:
            with open_database(self.layout.memory_db, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT a.memory_id,m.source_domain,m.source_ref,m.source_record_id,m.source_revision,m.digest,m.provenance_json "
                    "FROM source_mappings m JOIN atoms a ON a.atom_id=m.atom_id WHERE " + " AND ".join(clauses) + " ORDER BY a.memory_id,m.mapping_id",
                    params,
                ).fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                source_ref = str(row[2] or "")
                if source_ref and (Path(source_ref).is_absolute() or source_ref.startswith("\\\\")):
                    source_ref = "source:" + hashlib.sha256(source_ref.encode("utf-8", "replace")).hexdigest()[:24]
                values.append({
                    "memory_id": str(row[0] or ""),
                    "source_domain": str(row[1] or ""),
                    "source_ref": source_ref,
                    "source_record_id": str(row[3] or ""),
                    "source_revision": str(row[4] or ""),
                    "digest": str(row[5] or ""),
                    "provenance": _plain(json.loads(str(row[6] or "{}"))),
                })
            return {"status": "READY", "mappings": values, "total": len(values)}
        except Exception as exc:
            raise NativePortError("v2_memory_source_map_unavailable") from exc

    def _gui_groups(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        del payload, kwargs
        if (self.layout.memory_db.is_file() or self.layout.manifest_db.is_file()) and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        try:
            service = self._group_service(write=False)
            result = dict(service.list_share_groups())
            if not self.layout.memory_db.is_file() and not self.layout.manifest_db.is_file():
                result.setdefault("scope", {
                    "share_group_id": _text(context.get("share_group_id")),
                    "agent_instance_id": _text(context.get("agent_instance_id")),
                    "project_ref": _text(context.get("project_ref")),
                })
            return result
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError(
                _text(getattr(exc, "code", "")) or "v2_group_control_unavailable"
            ) from exc

    def _cli_audit(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._reference_audit(payload, context, **kwargs)

    def _cli_explain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._explain(payload, context, **kwargs)

    def _cli_scan(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._scan_summary(payload, context, **kwargs)

    @staticmethod
    def _cli_host_action(name: str) -> dict[str, Any]:
        return {"host_action": name, "gated_by": "v2_native_manifest"}

    def _cli_source(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("action")).casefold()
        if action not in {"list", "add", "remove", "preview"}:
            raise NativePortError("invalid_source_action")
        if action in {"add", "remove"} and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("source")

    def _cli_provider(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if _text(payload.get("action")).casefold() != "repair":
            raise NativePortError("invalid_provider_action")
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("provider")

    def _cli_hooks(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("action")).casefold()
        if action not in {"status", "install", "ensure", "uninstall", "mode"}:
            raise NativePortError("invalid_hooks_action")
        if action != "status" and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("hooks")

    def _cli_groups(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("action")).casefold()
        if action == "migrate":
            raise NativePortError("v2_groups_migrate_retired")
        if action != "list":
            raise NativePortError("invalid_groups_action")
        if not self.layout.rules_db.is_file():
            return {"status": "NO_SOURCE", "groups": [], "total": 0}
        try:
            store = self._domain_store("rules")
            bindings = list(store.list_bindings(status="active"))
            if not self._trusted_admin(context):
                group = _text(context.get("share_group_id"))
                bindings = [item for item in bindings if _text(getattr(item, "share_group_id", "")) == group]
            grouped: dict[str, dict[str, Any]] = {}
            for binding in bindings:
                gid = _text(getattr(binding, "share_group_id", ""))
                if not gid:
                    continue
                item = grouped.setdefault(gid, {"share_group_id": gid, "binding_count": 0, "definition_ids": set()})
                item["binding_count"] += 1
                item["definition_ids"].add(_text(getattr(binding, "definition_id", "")))
            values = [
                {
                    "share_group_id": gid,
                    "binding_count": int(item["binding_count"]),
                    "definition_count": len(item["definition_ids"]),
                }
                for gid, item in sorted(grouped.items())
            ]
            return {"status": "READY", "groups": values, "total": len(values)}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_groups_status_unavailable") from exc

    def _cli_workspace_health(self, *, state: Any = None, generation: Any = None) -> dict[str, Any]:
        """Return non-tenant CLI health when no Agent binding is available.

        ``doctor`` and ``mcp-status`` are installation/control-plane diagnostics.
        A terminal process may legitimately have no host-issued Agent binding,
        especially immediately after activation.  In that case expose only
        workspace-level availability and manifest/coverage state: never record
        counts, memory bodies, group IDs, or any cross-tenant existence oracle.
        """
        coverage = self.coverage()
        return {
            "status": "READY",
            "scope_status": "UNBOUND",
            "manifest": {
                "state": _text(getattr(state, "value", state)),
                "generation": generation if type(generation) is int else None,
            },
            "domains": {
                "memory": self.layout.memory_db.is_file(),
                "rules": self.layout.rules_db.is_file(),
                "evidence": self.layout.evidence_db.is_file(),
                "content": self.layout.content_db.is_file(),
                "runtime": self.layout.runtime_db.is_file(),
            },
            "native_coverage": {
                "counts": dict(coverage.get("counts") or {}),
                "production_complete": bool(coverage.get("production_complete")),
            },
        }

    def _cli_mcp_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not context.get("share_group_id"):
            health = self._cli_workspace_health(
                state=kwargs.get("state"), generation=kwargs.get("generation")
            )
            return {
                **health,
                "available": bool(health["domains"].get("memory")),
                "memory_status": "READY" if health["domains"].get("memory") else "NO_SOURCE",
            }
        return self._memory_status(payload, context, **kwargs)

    def _runtime_lease_health(self) -> dict[str, Any]:
        """Read-only split-brain probe for diagnostics.

        A stale process from a previous build makes every write fail while the
        manifest, domains and coverage all still look healthy, so doctor has to
        report it as a first-class check rather than leaving it visible only in
        an MCP error payload.
        """
        try:
            from ..runtime_lease import runtime_lease_status, describe_split_brain

            status = runtime_lease_status(self.workspace)
            described = describe_split_brain(status, control_workspace=self.workspace)
            return {
                "split_brain": described["split_brain"],
                "conflicting_pids": described["pids"],
                "live": len(status.get("live") or []),
                "stale": len(status.get("stale") or []),
                "remedy": described["remedy"],
            }
        except Exception as exc:  # diagnostics must never break the command
            return {"split_brain": False, "error": type(exc).__name__}

    def _cli_doctor(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not context.get("share_group_id"):
            health = self._cli_workspace_health(
                state=kwargs.get("state"), generation=kwargs.get("generation")
            )
            return {**health, "runtime_lease": self._runtime_lease_health()}
        return {
            "status": "READY",
            "scope_status": "BOUND",
            "diagnostics": self._diagnostics_snapshot(payload, context, **kwargs),
            "runtime_lease": self._runtime_lease_health(),
            "native_coverage": {
                "counts": dict(self.coverage().get("counts") or {}),
                "production_complete": bool(self.coverage().get("production_complete")),
            },
        }

    def _cli_runtime(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("runtime_command")).casefold()
        if action not in {"status", "reap"}:
            raise NativePortError("invalid_runtime_action")
        # Reaping terminates OS processes: admin capability, never ambient.
        if action == "reap" and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("runtime")

    def _cli_gui(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return self._cli_host_action("gui")

    def _cli_open(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return self._cli_host_action("open")

    def _cli_desktop(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return self._cli_host_action("desktop")

    def _maintenance(self, payload: Mapping[str, Any], context: Mapping[str, Any], *, generation: int, mutation: bool, **_: Any) -> Any:
        if self.maintenance_port is None:
            raise NativePortError("v2_maintenance_service_unavailable")
        fn = getattr(self.maintenance_port, "dispatch", None) or getattr(self.maintenance_port, "call", None)
        if not callable(fn):
            raise NativePortError("v2_maintenance_service_unavailable")
        return fn("cli", "storage", dict(payload), context=context, generation=generation, mutation=mutation)

    def _store(self) -> Any:
        """Compatibility seam for maintenance recovery/replay tests.

        The business implementation remains in ``MaintenanceRuntimePort``;
        this method only exposes its already-lazy store for callers that need
        to inspect a job receipt after an injected fault.
        """
        if self.maintenance_port is None:
            raise NativePortError("v2_maintenance_service_unavailable")
        fn = getattr(self.maintenance_port, "_store", None)
        if not callable(fn):
            raise NativePortError("v2_maintenance_service_unavailable")
        return fn()

    def _builtin(self, handler: str) -> Callable[..., Any] | None:
        methods: dict[str, Callable[..., Any]] = {
            "list_sources": self._list_sources,
            "scan_summary": self._scan_summary,
            "import_preview": self._import_preview,
            "runtime_processes": self._runtime_processes,
            "external_mcp_list": self._external_mcp_list,
            "external_mcp_detect": self._external_mcp_detect,
            "external_mcp_preview": self._external_mcp_preview,
            "external_mcp_import": self._external_mcp_import,
            "history_search": lambda p, context, **k: self._history_operation("search", p, context, **k),
            "history_timeline": lambda p, context, **k: self._history_operation("timeline", p, context, **k),
            "history_read": lambda p, context, **k: self._history_operation("read", p, context, **k),
            "history_extract_preview": lambda p, context, **k: self._history_operation("extract_preview", p, context, **k),
            "history_list_sessions": lambda p, context, **k: self._history_operation("list_sessions", p, context, **k),
            "history_export": lambda p, context, **k: self._history_operation("export", p, context, **k),
            "history_delete": lambda p, context, **k: self._history_operation("delete", p, context, **k),
            "memory_read": self._memory_read,
            "memory_list": self._memory_list,
            "memory_search": self._memory_search,
            "memory_status": self._memory_status,
            "memory_global_status": self._memory_global_status,
            "memory_versions": self._memory_versions,
            "memory_supersede_chain": self._memory_supersede_chain,
            "coverage": lambda p, context, **k: self.coverage(),
            "memory_write": self._memory_write,
            "memory_update": self._memory_update,
            "memory_delete": self._memory_delete,
            "memory_merge_safe": lambda p, context, **k: self._memory_merge_operation("merge_safe", p, context, **k),
            "memory_merge_safe_preview": lambda p, context, **k: self._memory_merge_operation("merge_safe_preview", p, context, **k),
            "gui_memory_edit": self._gui_memory_edit,
            "gui_memory_lock": self._gui_memory_lock,
            "gui_memory_unlock": self._gui_memory_unlock,
            "gui_memory_policy": self._gui_memory_policy,
            "gui_memory_restore": self._gui_memory_restore,
            "gui_memory_delete": self._gui_memory_delete,
            "gui_memory_rollback": self._gui_memory_rollback,
            "context_bootstrap": self._context_bootstrap,
            "hook_bootstrap": self._context_bootstrap,
            "rule_create_auto": lambda p, context, **k: self._rule_lifecycle_operation("rule_create_auto", p, context, **k),
            "rule_feedback": lambda p, context, **k: self._rule_lifecycle_operation("rule_feedback", p, context, **k),
            "rule_undo": lambda p, context, **k: self._rule_lifecycle_operation("rule_undo", p, context, **k),
            "rule_decision_read": lambda p, context, **k: self._rule_lifecycle_operation("rule_decision_read", p, context, **k),
            "rule_scope_stats": lambda p, context, **k: self._rule_lifecycle_operation("rule_scope_stats", p, context, **k),
            "gui_rule_create": self._gui_rule_create,
            "gui_rule_feedback": self._gui_rule_feedback,
            "gui_rule_undo": self._gui_rule_undo,
            "gui_rule_exception": self._gui_rule_exception,
            "gui_rule_snapshot": self._gui_rule_snapshot,
            "gui_rule_effective": self._gui_rule_effective,
            "gui_rule_scope_options": self._gui_rule_scope_options,
            "gui_rule_decisions": self._gui_rule_decisions,
            "gui_rule_receipts": self._gui_rule_receipts,
            "gui_rule_exceptions": self._gui_rule_exceptions,
            "gui_rule_audience_update": self._gui_rule_audience_update,
            "gui_rule_body_update": self._gui_rule_body_update,
            "binding_list": self._binding_list,
            "binding_create": self._binding_create,
            "rule_merge_capability_issue": lambda p, context, **k: self._rule_merge_operation("capability_issue", p, context, **k),
            "rule_merge_approve": lambda p, context, **k: self._rule_merge_operation("approve", p, context, **k),
            "rule_merge_acknowledge": lambda p, context, **k: self._rule_merge_operation("acknowledge", p, context, **k),
            "rule_merge_cooldown_clear": lambda p, context, **k: self._rule_merge_operation("cooldown_clear", p, context, **k),
            "rule_merge_safe": lambda p, context, **k: self._rule_merge_operation("merge_safe", p, context, **k),
            "rule_merge_safe_preview": lambda p, context, **k: self._rule_merge_operation("merge_safe_preview", p, context, **k),
            "extract_memories": self._extract_memories,
            "gui_extract_preview": self._gui_extract_preview,
            "gui_extract_by_path": self._gui_extract_by_path,
            "accept_candidates": self._accept_candidates,
            "list_pending_enrichments": self._list_pending_enrichments,
            "apply_enrichments": self._apply_enrichments,
            "enrichment_status": self._enrichment_status,
            "build_and_enrich": self._build_and_enrich,
            "resolve_group": self._resolve_group,
            "knowledge_read": self._knowledge_read,
            "knowledge_book": self._knowledge_book,
            "knowledge_candidates": self._knowledge_candidates,
            "mcp_capabilities": self._mcp_capabilities,
            "mcp_invoke": self._mcp_invoke,
            "mcp_gui_bridge": self._mcp_gui_bridge,
            "gui_knowledge_query": self._gui_knowledge_query,
            "gui_knowledge_command": self._gui_knowledge_command,
            "gui_projection_query": self._gui_projection_query,
            "gui_projection_command": self._gui_projection_command,
            "gui_release_query": self._gui_release_query,
            "gui_release_command": self._gui_release_command,
            "gui_governance_query": self._gui_governance_query,
            "gui_governance_command": self._gui_governance_command,
            "gui_agent_query": self._gui_agent_query,
            "gui_agent_command": self._gui_agent_command,
            "gui_group_query": self._gui_group_query,
            "gui_group_command": self._gui_group_command,
            "gui_task_status": self._gui_task_status,
            "gui_task_list": self._gui_task_list,
            "gui_task_cancel": self._gui_task_cancel,
            "gui_request_compat": self._gui_request_compat,
            "gui_maintenance_control": self._gui_maintenance_control,
            "gui_import_query": self._gui_import_query,
            "gui_import_control": self._gui_import_control,
            "gui_history_control": self._gui_history_control,
            "gui_audit_plan": self._gui_audit_plan,
            "gui_usage_query": self._gui_usage_query,
            "gui_usage_sync": self._gui_usage_sync,
            "reference_audit": self._reference_audit,
            "explain": self._explain,
            "projection_graph": self._projection_graph,
            "codegraph_graph": self._codegraph_graph,
            "codegraph_projects": self._codegraph_projects,
            "codegraph_build": self._codegraph_build,
            "codegraph_automation": self._codegraph_automation,
            "codegraph_build_bound": self._codegraph_build_bound,
            "codegraph_query": self._codegraph_query,
            "codegraph_path": self._codegraph_path,
            "codegraph_explain": self._codegraph_explain,
            "codegraph_affected": self._codegraph_affected,
            "codegraph_update": self._codegraph_update,
            "codegraph_status": self._codegraph_status,
            "semantic_check": self._semantic_check,
            "provider_install": self._provider_install,
            "status": self._status,
            "sandbox_status": self._sandbox_status,
            "host_enrichment_guide": self._host_enrichment_guide,
            "host_llm_agents": self._host_llm_agents,
            "diagnostics_snapshot": self._diagnostics_snapshot,
            "governance_snapshot": self._governance_snapshot,
            "scope_echo": self._scope_echo,
            "hook_status": self._hook_status,
            "gui_host_control": self._gui_host_control,
            "bridge_transport": self._bridge_transport,
            "gui_pick_path": self._gui_pick_path,
            "gui_source_add": self._gui_source_add,
            "gui_source_remove": self._gui_source_remove,
            "gui_memory_source_map": self._gui_memory_source_map,
            "gui_groups": self._gui_groups,
            "cli_audit": self._cli_audit,
            "cli_explain": self._cli_explain,
            "cli_scan": self._cli_scan,
            "cli_source": self._cli_source,
            "cli_provider": self._cli_provider,
            "cli_hooks": self._cli_hooks,
            "cli_groups": self._cli_groups,
            "cli_mcp_status": self._cli_mcp_status,
            "cli_doctor": self._cli_doctor,
            "cli_runtime": self._cli_runtime,
            "cli_gui": self._cli_gui,
            "cli_open": self._cli_open,
            "cli_desktop": self._cli_desktop,
            "maintenance": self._maintenance,
            "projection_status": self._projection_status,
            "canonical_status": self._canonical_status,
            "diagnostics_status": lambda p, context, **k: self._status(p, context, **k),
            "asset_status": lambda p, context, **k: self._domain_status("assets", p, context, **k),
            "skill_status": lambda p, context, **k: self._domain_status("skills", p, context, **k),
        }
        return methods.get(handler)

    def _call(
        self,
        fn: Callable[..., Any],
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        mutation: bool,
        state: Any = None,
        operation: str = "",
        public_name: str = "",
    ) -> Any:
        try:
            params = inspect.signature(fn).parameters
            accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())
        except (TypeError, ValueError):
            params, accepts_kwargs = {}, False
        if (mutation or context) and not accepts_kwargs and "context" not in params:
            raise NativePortError("v2_context_capability_required")
        kwargs = {
            key: value
            for key, value in {
                "context": context,
                "generation": generation,
                "mutation": mutation,
                "state": state,
            }.items()
            if accepts_kwargs or key in params
        }
        # Canonical operation metadata is business dispatch input, not generic
        # transport context.  Only handlers that explicitly declare these
        # parameters receive them; forwarding through an arbitrary **kwargs
        # wrapper can duplicate a fixed operation argument downstream.
        if "operation" in params:
            kwargs["operation"] = operation
        if "public_name" in params:
            kwargs["public_name"] = public_name
        return fn(payload, **kwargs)

    @staticmethod
    def _provider_snapshot(provider: Any) -> tuple[str, int | None]:
        """Read one trusted manifest snapshot from an injected provider."""
        value = provider
        current = getattr(provider, "current", None)
        if callable(current):
            value = current()
        elif callable(provider):
            value = provider()
        if isinstance(value, Mapping):
            state = value.get("state")
            generation = value.get("generation")
        else:
            state = getattr(value, "state", None)
            generation = getattr(value, "generation", None)
        state_text = _text(getattr(state, "value", state)).upper()
        return state_text, generation if type(generation) is int else None

    def _validated_dispatch_state(
        self,
        surface: str,
        name: str,
        generation: Any,
        mutation: bool,
        state: Any,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Enforce state/generation at the native boundary.

        V2RuntimeFacade supplies a trusted immutable state snapshot.  Direct
        native callers must either provide that snapshot or inject a
        ``state_provider``; mutations without either capability are refused.
        """
        if type(generation) is not int or generation < 0:
            return self._error(surface, name, "invalid_manifest_generation"), state
        effective_state = state
        provider_generation: int | None = None
        # Mutations must always compare against a trusted provider snapshot.
        # A state value supplied in an RPC payload is only a CAS hint and may
        # not bootstrap a direct native write by itself.
        if mutation and self.state_provider is None:
            return self._error(surface, name, "v2_state_provider_required", generation=generation), state
        if (mutation or effective_state is None) and self.state_provider is not None:
            try:
                provider_state, provider_generation = self._provider_snapshot(self.state_provider)
                if effective_state is not None:
                    supplied_state = _text(getattr(effective_state, "value", effective_state)).upper()
                    if supplied_state and supplied_state != provider_state:
                        return self._error(surface, name, "manifest_state_mismatch", generation=generation), state
                effective_state = provider_state
            except Exception:
                return self._error(surface, name, "v2_manifest_state_unavailable", generation=generation), state
        if mutation and provider_generation is None:
            return self._error(surface, name, "v2_manifest_generation_unavailable", generation=generation), effective_state
        if effective_state is None:
            if mutation:
                return self._error(surface, name, "v2_state_provider_required", generation=generation), effective_state
            return None, effective_state
        state_text = _text(getattr(effective_state, "value", effective_state)).upper()
        if state_text not in {"V2_READY", "V2_ACTIVE"}:
            return self._error(surface, name, "v2_manifest_state_unavailable", generation=generation), effective_state
        if provider_generation is not None and provider_generation != generation:
            return self._error(surface, name, "manifest_generation_mismatch", generation=generation), effective_state
        if mutation and state_text != "V2_ACTIVE":
            return self._error(surface, name, "v2_not_active", generation=generation), effective_state
        return None, effective_state

    def _validate_dispatch_state(
        self,
        surface: str,
        name: str,
        generation: Any,
        mutation: bool,
        state: Any,
    ) -> dict[str, Any] | None:
        """Compatibility wrapper returning only the state-gate error."""

        error, _effective_state = self._validated_dispatch_state(
            surface, name, generation, mutation, state,
        )
        return error

    @staticmethod
    def _classify_mutation(
        surface: str,
        name: str,
        args: Any,
        explicit: bool,
        spec: SurfaceSpec,
    ) -> bool:
        """Keep direct ports from downgrading a known CLI storage write."""
        if spec.mutation or explicit:
            return True
        if surface == "cli" and spec.handler == "maintenance":
            try:
                action = _text(_payload(args).get("action")).casefold()
            except NativePortError:
                return True
            # Maintenance audit/report are read-only.  Every other action is
            # conservatively treated as a write, independent of caller flags.
            return action not in {"audit", "report"}
        if surface == "cli":
            try:
                action = _text(_payload(args).get("action")).casefold()
            except NativePortError:
                return True
            if spec.handler == "cli_hooks":
                return action != "status"
            if spec.handler == "cli_source":
                return action in {"add", "remove"}
        return False

    def _dispatch_checked(
        self,
        surface: str,
        name: str,
        args: Any,
        *,
        context: Any,
        generation: Any,
        mutation: bool,
        state: Any = None,
    ) -> dict[str, Any]:
        if type(generation) is not int or generation < 0:
            return self._error(surface, name, "invalid_manifest_generation")
        spec = self._registry.get(surface, {}).get(name)
        if spec is None:
            return self._error(surface, name, "unknown_surface_operation")
        if spec.status == "blocker":
            return self._error(surface, name, "v2_operation_not_implemented", status="blocked", generation=generation, reason=spec.reason)
        if spec.status == "retired":
            return self._error(surface, name, "v2_operation_retired", status="retired", generation=generation, reason=spec.reason)
        effective_mutation = self._classify_mutation(
            surface,
            name,
            args,
            bool(mutation),
            spec,
        )
        try:
            raw_payload = _phase9_gui_payload(surface, name, _payload(args))
            # ``provider`` is normally a protected identity selector.  For the
            # provider-install control-plane surface it is instead the target
            # integration name. Rename it before identity validation so the
            # trusted caller provider remains immutable and unambiguous.
            if surface == "mcp" and name == "memoryguard_provider_install" and "provider" in raw_payload:
                raw_payload = dict(raw_payload)
                raw_payload["target_provider"] = raw_payload.pop("provider")
            neutral_mcp_read = (
                surface == "mcp"
                and not effective_mutation
                and name in _NEUTRAL_MCP_READS
            )
            context_payload = raw_payload
            if surface == "mcp" and "workspace" in raw_payload:
                # MCP workspace is a project hint resolved into the trusted
                # context by mcp_server.  It is not a second workspace
                # authority and must not trip spoof checks against the V2
                # control-plane workspace.  workspace_id remains protected.
                context_payload = dict(raw_payload)
                context_payload.pop("workspace", None)
            trusted = self._context(
                context,
                context_payload,
                required=(
                    effective_mutation
                    or surface in {"gui", "hook"}
                    or (surface == "mcp" and not neutral_mcp_read)
                ),
                allow_partial=(
                    surface == "cli" and spec.handler == "maintenance"
                ) or (
                    surface == "gui" and spec.handler in _PHASE9_GUI_READ_HANDLERS
                ) or (
                    surface == "gui" and spec.handler in _GUI_AGENT_READ_HANDLERS
                ) or (
                    # Agent/group control-plane operations bootstrap and
                    # repair bindings themselves.  They require a process-
                    # issued native capability, but they must not require an
                    # already-selected memory share group or the first binding
                    # could never be created.  The handlers below still enforce
                    # admin authority for every mutation.
                    surface == "gui" and spec.handler in {
                        "gui_agent_query", "gui_agent_command",
                        "gui_group_query", "gui_group_command",
                        "gui_maintenance_control",
                    }
                ) or neutral_mcp_read,
            )
            # MaintenanceRuntimePort owns a separate private CLI capability
            # and validates it at its own boundary.  Do not require the MCP/
            # GUI native sentinel here for that one delegated handler; all
            # other mutations must carry the native transport capability.
            maintenance_route = surface == "cli" and spec.handler == "maintenance"
            if effective_mutation and not maintenance_route:
                resolve_native_transport_context(trusted)
            # GUI Phase 9 reads are scoped services too.  SafeBridge supplies
            # the process-issued bound context; plain identity mappings must
            # not become an authorization substitute (even for reads).
            if surface == "gui" and spec.handler in _PHASE9_GUI_HANDLERS:
                try:
                    authority = resolve_native_transport_context(trusted)
                except NativeContextError as exc:
                    raise NativePortError("trusted_context_capability_required") from exc
                if (
                    spec.handler in _PHASE9_GUI_READ_HANDLERS
                    and not authority.share_group_id
                ):
                    # Missing/ambiguous GUI binding is intentionally neutral;
                    # no source/history existence oracle is exposed.
                    if spec.handler.startswith("history_"):
                        return self._result(
                            surface,
                            name,
                            self._history_neutral(spec.handler),
                            generation=generation,
                        )
                    if spec.handler in {"list_sources", "scan_summary"}:
                        return self._result(
                            surface,
                            name,
                            self._source_neutral(spec.handler),
                            generation=generation,
                        )
                    if spec.handler in {"memory_versions", "memory_supersede_chain"}:
                        raise NativePortError("trusted_context_capability_required")
                    raise NativePortError("no_source")
            # Do not forward identity claims from the transport payload.  The
            # trusted context above is the only source of scope/identity.
            clean = {
                key: value for key, value in raw_payload.items()
                if key not in _IDENTITY_PAYLOAD_KEYS
                or (
                    key == "session_id"
                    and surface == "gui"
                    and name in _GUI_HISTORY_SESSION_SELECTOR_OPERATIONS
                )
            }
            fn = self._service(surface, name, spec.handler) or self._builtin(spec.handler)
            if fn is None:
                raise NativePortError("v2_operation_not_implemented")
            result = self._call(
                fn,
                clean,
                trusted,
                generation=generation,
                mutation=effective_mutation,
                state=state,
                operation=spec.canonical_name or name,
                public_name=name,
            )
            return self._result(surface, name, result, generation=generation)
        except NativePortError as exc:
            return self._error(surface, name, exc.code, generation=generation)
        except Exception:
            return self._error(surface, name, "v2_native_handler_failed", generation=generation)

    # ---- public dispatch -----------------------------------------------------
    def dispatch(self, surface: str, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        surface_key = _text(surface).casefold()
        operation = str(name or "")
        spec = self._registry.get(surface_key, {}).get(operation)
        if spec is None:
            return self._error(surface_key, operation, "unknown_surface_operation")
        # Dispatch handlers normalize and consume transport fields.  Snapshot
        # the complete request before mutation classification so direct native
        # callers receive the same no-alias guarantee as MCP execute_tool.
        try:
            request_args = deepcopy(args)
        except Exception:
            return self._error(
                surface_key,
                operation,
                "invalid_native_arguments",
            )
        # Classify command sub-actions before the state/CAS gate.  Otherwise a
        # caller could label a mutating maintenance action as read-only and
        # reach the handler without a trusted provider snapshot.
        effective_mutation = self._classify_mutation(
            surface_key,
            operation,
            request_args,
            bool(mutation),
            spec,
        )
        state_error, effective_state = self._validated_dispatch_state(
            surface_key, operation, generation, effective_mutation, state,
        )
        if state_error is not None:
            return state_error
        if surface_key == "cli" and effective_mutation:
            spec = self._registry.get(surface_key, {}).get(operation)
            if spec is not None and spec.handler != "maintenance":
                context = self._bind_cli_transport_context(context)
        return self._dispatch_checked(
            surface_key,
            operation,
            request_args,
            context=context,
            generation=generation,
            mutation=effective_mutation,
            state=effective_state,
        )

    call = dispatch

    def bootstrap_hook(self, request: Any = None, payload: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        args = _payload(payload)
        if request is not None:
            args.setdefault("request", request)
        state_error, effective_state = self._validated_dispatch_state(
            "hook", "bootstrap_hook", generation, bool(mutation), state,
        )
        if state_error is not None:
            return state_error
        return self._dispatch_checked(
            "hook",
            "bootstrap_hook",
            args,
            context=context,
            generation=generation,
            mutation=mutation,
            state=effective_state,
        )

    bootstrap = bootstrap_hook

    # Surface-specific aliases make direct host integration explicit while all
    # calls still pass through the same registry/security path.
    def dispatch_mcp(self, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        return self.dispatch("mcp", name, args, context=context, generation=generation, mutation=mutation, state=state)

    def dispatch_gui(self, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        return self.dispatch("gui", name, args, context=context, generation=generation, mutation=mutation, state=state)

    def dispatch_cli(self, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        return self.dispatch("cli", name, args, context=context, generation=generation, mutation=mutation, state=state)

    def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Stop every worker owned by this native port before GUI/process exit."""
        stopped = True
        if self._task_coordinator is not None:
            try:
                result = self._task_coordinator.shutdown(timeout=float(timeout))
                stopped = bool(result.get("ok", True)) if isinstance(result, Mapping) else True
            except Exception:
                stopped = False
        return {"ok": stopped, "owned_workers_stopped": stopped}

    close = shutdown


NativeRuntimePort = NativeV2RuntimePort

__all__ = [
    "NativeBoundContext", "NativeContextEnvelope", "NativeContextError", "NativePortError", "NativeRuntimePort", "NativeV2RuntimePort",
    "SurfaceSpec", "bind_native_transport_context", "resolve_native_transport_context", "bind_native_test_capability",
    "bind_native_test_services", "bind_native_test_store",
]
