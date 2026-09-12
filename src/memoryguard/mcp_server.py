"""MCP stdio server（spec §1.2, §4.2）。

让宿主通过 MCP stdio 调用 MemoryGuard，获得结构化结果。
首期实现 MCP 2024-11-05 基础协议子集:
- initialize: 能力协商
- tools/list: 返回面向日常记忆工作的精简默认工具集
- tools/call: 执行工具，返回结构化结果

纯标准库实现，不依赖 MCP SDK。无网络。
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import json
import hashlib
import inspect
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import __version__ as PACKAGE_VERSION
from .cutover_v2.surfaces import (
    GUI_OPERATION_SPECS,
    GUI_MUTATION_NAMES,
    MCP_BROKER_GUI_ADMIN_NAMES,
    MCP_BROKER_GUI_EXCLUDED,
    MCP_BROKER_GUI_HOST_BOUND_NAMES,
    MCP_BROKER_GUI_METHOD_NAMES,
    MCP_DEFAULT_PUBLIC_TOOL_NAMES,
    MCP_GUI_BRIDGE_OPERATIONS,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
    resolve_mcp_broker_invocation,
)
from .runtime_v2.public_safety import (
    safe_error_code,
    safe_exception_diagnostic,
    sanitize_public_payload,
    v2_upgrade_payload,
)
from .response_budget import (
    BindingRevision,
    DEFAULT_RESPONSE_BUDGET_BYTES,
    GLOBAL_RESPONSE_STORE,
    ResponseReferenceError,
    ResponseScope,
    compact_success_envelope,
    digest as response_digest,
    json_bytes as response_json_bytes,
    json_text as response_json_text,
    paged_summary,
    unavailable_summary,
)


# MCP mutation classification has one source of truth.  This gate controls
# trusted-context injection, runtime lease checks, and V2_READY blocking, so a
# local copy can silently leave a newly registered writer on a weaker path.
_MUTATING_TOOLS = MCP_MUTATION_NAMES

# Tools that physically write SQLite state even though their business role is
# mostly read.  They must pass the runtime split-brain lease, but they should
# not be treated as governance mutations by the write-degraded gate.
_DB_WRITING_TOOLS = _MUTATING_TOOLS | {
    "memoryguard_context_bootstrap",
}

# Canonical-rule readiness protects memory/rule mutations.  Host integration
# repair and binding administration must remain available while canonical rule
# projection is degraded; otherwise a stale provider config can deadlock its
# own repair path.
_CANONICAL_GATED_TOOLS = _MUTATING_TOOLS - {
    "memoryguard_provider_install",
    "memoryguard_binding_create",
}


# ---------------------------------------------------------------------------
# Req9: governance-degraded read-only diagnostics
# ---------------------------------------------------------------------------
# When governance is degraded the MCP layer blocks every tool except these
# four read-only diagnostics; mutations and normal tools get a structured
# refusal (never a raised exception, so the MCP response shape stays intact).
_DEGRADED_WHITELIST = frozenset({
    "memoryguard_canonical_status",
    "memoryguard_diagnostics_snapshot",
    "memoryguard_projection_status",
    "memoryguard_runtime_processes",
})

# Fixed read-only SQL for the diagnostics snapshot.  Never user-supplied; the
# snapshot accepts no arbitrary SQL and no arbitrary file paths.
_DIAGNOSTIC_JOBS_BY_STATUS_SQL = (
    "SELECT status AS status, COUNT(*) AS count "
    "FROM rule_reconciliation_jobs WHERE share_group_id = ? "
    "GROUP BY status ORDER BY status"
)
_DIAGNOSTIC_CANONICAL_STATE_SQL = (
    "SELECT share_group_id, activation_status, canonical_digest, read_path, "
    "activated_at, updated_at FROM rule_canonical_state "
    "WHERE share_group_id = ? ORDER BY share_group_id"
)
_DIAGNOSTIC_PROJECTION_SQL = (
    "SELECT scope_id, projection_lag, projection_error "
    "FROM rule_projection_state WHERE scope_id = ? ORDER BY scope_id"
)
_DIAGNOSTIC_SOURCE_LINKS_SQL = (
    "SELECT COUNT(*) AS count FROM rule_source_links WHERE share_group_id = ?"
)
_DIAGNOSTIC_BINDINGS_SQL = (
    "SELECT COUNT(*) AS count FROM rule_bindings WHERE share_group_id = ?"
)

# Lock probe deadline: short enough not to stall the MCP loop, long enough to
# distinguish real contention from a transient filesystem hiccup.
_LOCK_PROBE_TIMEOUT = 0.3


# ---------------------------------------------------------------------------
# MCP 协议常量
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "memoryguard"
SERVER_VERSION = PACKAGE_VERSION

from .mcp_catalog import (
    _FULL_TOOLS,
    _CAPABILITY_QUERY_ALIASES,
    _DEFAULT_TOOL_ANNOTATIONS,
    _TASK_MCP_OPERATIONS,
    TOOL_DEFINITIONS,
    CALLABLE_TOOL_NAMES,
    CALLABLE_TOOLS,
    TOOLS,
    mcp_capability_catalog,
)
def _mcp_error(message: str, *, code: str = "") -> dict[str, Any]:
    """Return a compact MCP error without reflecting caller/exception text."""
    raw = str(message or "").strip()
    lower = raw.casefold()
    risky = any(token in lower for token in (
        "password", "secret", "token", "api_key", "sqlite", "select ",
        "insert ", "update ", "delete ", "drop ", "traceback", "\\", "/",
    )) or len(raw) > 240
    # Keep established human-readable validation text when it contains no
    # path/SQL/secret material; arbitrary exception/path text becomes a stable
    # code instead.
    safe_text = raw if raw and not risky and all(ord(ch) < 128 for ch in raw) else ""
    stable = safe_error_code(code or raw.replace(" ", "_"), "request_failed")
    return {
        "content": [{"type": "text", "text": f"error: {safe_text or stable}"}],
        "isError": True,
        "code": stable,
    }


def _mcp_json_error(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return a spec-shaped CallToolResult for machine-readable failures.

    MCP CallToolResult only guarantees ``content`` and ``isError`` at the
    top level.  All diagnostics remain inside the protocol-shaped payload.
    """
    safe_payload = sanitize_public_payload(dict(payload), error_code="request_failed")
    result: dict[str, Any] = {
        "content": [{
            "type": "text",
            "text": json.dumps(safe_payload, ensure_ascii=False, indent=2),
        }],
        "isError": True,
    }
    return result


def _resolve_workspace(args: dict[str, Any]) -> Path:
    """Resolve the configured MemoryGuard control workspace."""
    from .data_home import resolve_runtime_data_home

    # MCP ``workspace`` is a project/scope hint, not a control-plane selector.
    # The transport must stay on the one user-level V2 data home even when a
    # caller supplies an empty or legacy project path.
    del args
    return resolve_runtime_data_home()


def _resolve_memory_workspace(args: dict[str, Any]) -> Path:
    """Resolve the sole V2 control plane; workspace is only a project hint."""
    from .access_context import clear_runtime_connection_override
    from .data_home import is_v2_data_home, resolve_runtime_data_home
    from .workspace_resolver import resolve_workspace

    clear_runtime_connection_override()
    del args
    # A host may explicitly provision an isolated V2 workspace through the
    # trusted environment.  Let the canonical resolver reject known legacy
    # project control trees, but do not require a manifest here: injected V2
    # facades and test/operator bootstrap flows can legitimately create the
    # domain databases before the manifest is available.  A request payload's
    # ``workspace`` never selects the control plane.
    configured_workspace = os.environ.get("MEMORYGUARD_WORKSPACE", "").strip()
    if configured_workspace:
        return resolve_runtime_data_home(Path(configured_workspace).expanduser().resolve())

    # An explicit canonical Data Home outranks cwd discovery. Without this
    # guard, launching from a project nested under another V2 workspace can
    # silently switch the MCP control plane away from MEMORYGUARD_HOME.
    configured_home = os.environ.get("MEMORYGUARD_HOME", "").strip()
    if configured_home:
        return resolve_runtime_data_home()

    # Bare MCP launches still get the resolver's bounded V2 discovery (for
    # example, a V2 child beside a legacy parent).  The resolver falls back to
    # cwd when it finds no V2 candidate; that cwd is a project hint, not a
    # control plane, so only accept it when it carries a V2 manifest.
    cwd = Path.cwd().resolve()
    if not is_v2_data_home(cwd):
        discovered = resolve_workspace(cwd=cwd)
        if is_v2_data_home(discovered):
            return discovered
    return resolve_runtime_data_home()


def _get_share_group_id(
    args: dict[str, Any],
    workspace: Path | None = None,
    *,
    strict: bool | None = None,
) -> tuple[str, str | None]:
    """Resolve the active V2 binding; client-selected groups are ignored."""
    from .access_context import load_access_context
    from .runtime_v2.group_native import GroupControlError, GroupControlService

    if strict is None:
        strict = os.environ.get("MEMORYGUARD_STRICT_BINDING", "") == "1"
    ws = (workspace or _resolve_memory_workspace(args)).resolve()
    ctx = load_access_context()
    claimed = str(args.get("agent_instance_id", "") or "").strip()
    agent_id = claimed or str(getattr(ctx, "trusted_agent_id", "") or "").strip()
    if not agent_id:
        if strict:
            return "", "missing agent_instance_id; strict binding mode requires it"
        return "default", None
    try:
        binding = GroupControlService(ws, write=False).active_binding_for_agent(agent_id)
    except GroupControlError as exc:
        if strict:
            return "", "v2_binding_unavailable:" + safe_error_code(
                getattr(exc, "code", ""), "group_control_failed",
            )
        return "default", None
    except PermissionError as exc:
        if strict:
            errno = getattr(exc, "errno", None)
            suffix = f":errno_{errno}" if isinstance(errno, int) and errno >= 0 else ""
            return "", "v2_binding_unavailable:permission_denied" + suffix
        return "default", None
    except OSError as exc:
        if strict:
            errno = getattr(exc, "errno", None)
            suffix = f":errno_{errno}" if isinstance(errno, int) and errno >= 0 else ""
            return "", "v2_binding_unavailable:os_error" + suffix
        return "default", None
    except Exception as exc:
        if strict:
            return "", f"v2_binding_unavailable:{type(exc).__name__}"
        return "default", None
    if not binding:
        if strict:
            return "", f"agent '{agent_id}' has no active binding"
        return "default", None
    group_id = str(binding.get("share_group_id", "") or "").strip()
    if not group_id:
        return "", "active V2 binding has no share_group_id"
    return group_id, None


def _redact_secret(body: str) -> tuple[str, str]:
    """统一 secret 检测+脱敏。返回 (safe_body, secret_pattern)。

    secret_pattern 非空表示检测到 secret(已脱敏)。
    所有写入路径(write/update/edit/import/accept)必须调用此函数。
    """
    from .auto_organizer import SECRET_PATTERNS
    safe_body = body
    secret_hit = ""
    for pattern in SECRET_PATTERNS:
        if pattern.search(body):
            secret_hit = pattern.pattern[:50]
            break
    if secret_hit:
        for pattern in SECRET_PATTERNS:
            safe_body = pattern.sub("[REDACTED]", safe_body)
    return (safe_body, secret_hit)


def _resolve_access(
    args: dict[str, Any],
    workspace: Path,
) -> tuple[str | None, str | None, "AccessContext | None"]:
    """Resolve trusted V2 identity and active binding."""
    from .access_context import load_access_context

    ctx = load_access_context()
    claimed_agent = str(args.get("agent_instance_id", "") or "")
    agent_id, err = ctx.resolve_agent(claimed_agent)
    if err:
        return None, err, ctx
    args["agent_instance_id"] = agent_id
    group_id, binding_err = _get_share_group_id(
        args, workspace, strict=ctx.strict_binding,
    )
    if binding_err:
        return None, binding_err, ctx
    if not group_id:
        return None, "no share_group_id resolved; access denied", ctx
    maintenance_marker = (
        workspace / ".memoryguard" / "shared-memory" / group_id / ".maintenance"
    )
    if maintenance_marker.exists():
        return None, "memory group is in maintenance", ctx
    return group_id, None, ctx


def _effective_agent_context(
    args: dict[str, Any],
    group_id: str,
    *,
    access_context: Any = None,
):
    """Build effective scope from trusted environment and V2 binding."""
    from .access_context import effective_provider, load_access_context
    from .rule_scope import canonical_project_ref
    from .schema_v3 import EffectiveAgentContext

    if access_context is None:
        access_context = load_access_context()
    return EffectiveAgentContext(
        agent_instance_id=str(args.get("agent_instance_id", "") or ""),
        share_group_id=group_id,
        provider=effective_provider().strip().lower(),
        project_ref=canonical_project_ref(
            args.get("project_ref")
            or os.environ.get("MEMORYGUARD_PROJECT_CWD")
            or args.get("workspace")
            or os.getcwd()
        ),
        runtime_role=os.environ.get("MEMORYGUARD_RUNTIME_ROLE", "").strip(),
        runtime_agent_id=os.environ.get("MEMORYGUARD_RUNTIME_AGENT_ID", "").strip(),
        parent_agent_id=os.environ.get("MEMORYGUARD_PARENT_AGENT_ID", "").strip(),
        session_id=access_context.session_id,
        context_hash=os.environ.get("MEMORYGUARD_CONTEXT_HASH", "").strip(),
        session_trusted=access_context.session_trusted,
        session_source=access_context.session_source,
    )


_V2_STATES = frozenset({"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"})
_V2_READ_STATES = frozenset({"V2_READY", "V2_ACTIVE"})
_V2_FACADE_MISSING = object()
_v2_runtime_facade_factory: Any = None
# This is set only during serve_stdio.  It contains facades created by that
# stdio process, never facades owned by another host integration.
_stdio_owned_facades: list[Any] | None = None


def _remember_stdio_facade(facade: Any) -> None:
    if _stdio_owned_facades is not None and not any(item is facade for item in _stdio_owned_facades):
        _stdio_owned_facades.append(facade)

_V2_PAYLOAD_IDENTITY_KEYS = frozenset({
    "agent_instance_id", "share_group_id", "workspace", "provider",
    "project_ref", "runtime_role", "runtime_agent_id", "parent_agent_id",
    "session_id", "context_hash", "session_source", "session_trusted",
    "context", "access_context", "trusted_context", "trusted_identity", "identity",
})


def _load_v2_runtime_facade(workspace: Path) -> Any:
    """Load the native V2 facade; never fall back to retired storage."""
    factory = globals().get("_v2_runtime_facade_factory")
    if callable(factory):
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("v2_runtime_factory_signature_unavailable") from exc
        try:
            signature.bind(workspace)
        except TypeError:
            try:
                signature.bind(workspace=workspace)
            except TypeError as exc:
                raise RuntimeError("v2_runtime_factory_signature_unavailable") from exc
            return factory(workspace=workspace)
        return factory(workspace)

    from .cutover_v2.facade import get_v2_runtime_facade
    return get_v2_runtime_facade(str(workspace))


def _v2_state_from_value(value: Any) -> str:
    # Normalize injected snapshots through the guarded factory.  Do not
    # accept a hand-constructed RuntimeSnapshot or a mapping that advertises
    # an invalid generation/availability marker.
    try:
        from .cutover_v2.state import CutoverState, RuntimeSnapshot
        if isinstance(value, RuntimeSnapshot):
            if not value.trusted or not value.available:
                return "UNKNOWN"
            return value.state.value if value.generation >= 0 else "UNKNOWN"
        if isinstance(value, CutoverState):
            return "UNKNOWN"
        if isinstance(value, dict) and any(key in value for key in ("state", "manifest_state", "status", "marker")):
            snapshot = RuntimeSnapshot.from_value(value)
            if snapshot.available:
                return snapshot.state.value
            return "UNKNOWN"
        if hasattr(value, "state"):
            snapshot = RuntimeSnapshot.from_value(value)
            if snapshot.available:
                return snapshot.state.value
            return "UNKNOWN"
    except Exception:
        return "UNKNOWN"
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        value = enum_value
    # RuntimeSnapshot/CutoverState are the trusted object forms returned by
    # the Phase6 facade.  Normalize their state field before string fallback.
    object_state = getattr(value, "state", None)
    if object_state is not None and object_state is not value:
        return _v2_state_from_value(object_state)
    if isinstance(value, dict):
        for key in ("state", "manifest_state", "status", "marker"):
            if key in value:
                return _v2_state_from_value(value[key])
        for key in ("manifest", "snapshot"):
            if isinstance(value.get(key), dict):
                return _v2_state_from_value(value[key])
        return "UNKNOWN"
    marker = str(value or "").strip().upper()
    return marker if marker in _V2_STATES else "UNKNOWN"


def _v2_facade_state(facade: Any, workspace: Path | str = "") -> tuple[str, Any]:
    fn = getattr(facade, "state_snapshot", None)
    if not callable(fn):
        fn = getattr(facade, "status", None)
    if not callable(fn):
        return "UNKNOWN", None
    try:
        # The facade contract is zero-argument.  A legacy-compatible injected
        # port may expose a workspace argument; inspect before calling so the
        # manifest is still read exactly once.
        try:
            signature = inspect.signature(fn)
        except (TypeError, ValueError):
            return "UNKNOWN", None
        target = str(workspace or "")
        try:
            signature.bind()
        except TypeError:
            try:
                signature.bind(target)
            except TypeError:
                try:
                    signature.bind(workspace=target)
                except TypeError:
                    return "UNKNOWN", None
                value = fn(workspace=target)
            else:
                value = fn(target)
        else:
            value = fn()
    except Exception:
        return "UNKNOWN", None
    return _v2_state_from_value(value), value


def _trusted_context_for_v2(args: dict[str, Any], workspace: Path) -> tuple[Any | None, str | None]:
    """Build context from the active binding/environment, never payload claims."""
    # Keep identity claims in the resolver input so AccessContext can reject a
    # forged/mismatched principal.  After that consistency check, construct the
    # process-issued context from the resolved identity only; identity-looking
    # fields never reach the native business payload as a second authority.
    resolver_args = dict(args)
    try:
        group_id, error, access_context = _resolve_access(resolver_args, workspace)
    except Exception as exc:
        return None, f"trusted_context_unavailable:{type(exc).__name__}"
    if error or not group_id:
        return None, error or "trusted_context_unavailable"
    trusted_args = {
        key: value for key, value in resolver_args.items()
        if key not in _V2_PAYLOAD_IDENTITY_KEYS
    }
    # _resolve_access replaces the checked claim with the connection-owned
    # principal.  Carry that resolved value only for context construction.
    trusted_args["agent_instance_id"] = str(
        resolver_args.get("agent_instance_id") or ""
    )
    try:
        context_builder = _effective_agent_context
        try:
            context_params = inspect.signature(context_builder).parameters
        except (TypeError, ValueError):
            context_params = {}
        accepts_context_kw = "access_context" in context_params or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in context_params.values()
        )
        if accepts_context_kw:
            context = context_builder(
                trusted_args,
                group_id,
                access_context=access_context,
            )
        elif access_context is None:
            # Compatibility-only injected fakes from the pre-capability test
            # seam do not accept the new keyword.  A real resolver always
            # returns AccessContext; if this plain fallback reaches a native
            # mutation, NativeV2RuntimePort rejects it before writing.
            context = context_builder(trusted_args, group_id)
        else:
            return None, "trusted_context_unavailable"
        # The native port's mutation boundary requires a process-local
        # capability.  Preserve the real AccessContext from _resolve_access
        # rather than serializing EffectiveAgentContext into a forgeable dict.
        from .runtime_v2.native_ports import bind_native_transport_context

        if access_context is None:
            to_dict = getattr(context, "to_dict", None)
            if callable(to_dict):
                plain = to_dict()
            else:
                try:
                    plain = dict(vars(context))
                except TypeError:
                    plain = {}
            return (dict(plain), None) if isinstance(plain, Mapping) else (None, "trusted_context_unavailable")
        bound = bind_native_transport_context(
            access_context,
            workspace_id=str(workspace),
            share_group_id=group_id,
            project_ref=str(getattr(context, "project_ref", "") or ""),
            provider=str(getattr(context, "provider", "") or ""),
            runtime_role=str(getattr(context, "runtime_role", "") or ""),
            runtime_agent_id=str(getattr(context, "runtime_agent_id", "") or ""),
            parent_agent_id=str(getattr(context, "parent_agent_id", "") or ""),
            context_hash=str(getattr(context, "context_hash", "") or ""),
            entrypoint="mcp",
        )
        return bound, None
    except Exception as exc:
        return None, f"trusted_context_unavailable:{type(exc).__name__}"


_V2_PROVIDER_TARGETS = frozenset({"claude", "codex", "cursor", "trae"})

_V2_RULE_MERGE_TOOLS = frozenset({
    "memoryguard_rule_merge_capability_issue",
    "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge",
    "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_rule_merge_safe",
    "memoryguard_rule_merge_safe_preview",
})
_V2_MEMORY_MERGE_TOOLS = frozenset({
    "memoryguard_memory_merge_safe",
    "memoryguard_memory_merge_safe_preview",
})


def _validate_v2_mcp_arguments(name: str, args: Mapping[str, Any]) -> None:
    """Validate public V2-only arguments without reflecting secret values.

    Native services remain the authority for business validation.  This seam
    exists so MCP advertises and enforces the native mutation proof contract
    before a handler is reached: every merge write has a receipt and retry
    key, while capability issuance additionally has a strict recovery secret.
    The secret is decoded only for shape validation and is never copied into a
    diagnostic or response payload.
    """

    if name in _V2_MEMORY_MERGE_TOOLS:
        if not isinstance(args, Mapping):
            raise ValueError("invalid_tool_arguments")
        if name == "memoryguard_memory_merge_safe_preview":
            return
        receipt = args.get("mutation_receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("mutation_receipt_required")
        receipt_id = receipt.get("receipt_id") or receipt.get("id")
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id.strip()) > 256:
            raise ValueError("mutation_receipt_required")
        idempotency_key = args.get("idempotency_key")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key.strip()) > 256
        ):
            raise ValueError("idempotency_key_required")
        if args.get("confirmed") is not True:
            raise ValueError("confirmation_required")
        revisions = args.get("expected_atom_revisions")
        if not isinstance(revisions, Mapping) or not revisions:
            raise ValueError("atom_revision_required")
        return

    if name not in _V2_RULE_MERGE_TOOLS:
        return
    if not isinstance(args, Mapping):
        raise ValueError("invalid_tool_arguments")

    if name == "memoryguard_rule_merge_safe_preview":
        return

    if name == "memoryguard_rule_merge_safe":
        receipt = args.get("mutation_receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("mutation_receipt_required")
        receipt_id = receipt.get("receipt_id") or receipt.get("id")
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id.strip()) > 256:
            raise ValueError("mutation_receipt_required")
        idempotency_key = args.get("idempotency_key")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key.strip()) > 256
        ):
            raise ValueError("idempotency_key_required")
        if args.get("confirmed") is not True:
            raise ValueError("confirmation_required")
        revisions = args.get("expected_definition_revisions")
        if not isinstance(revisions, Mapping) or not revisions:
            raise ValueError("definition_revision_required")
        return

    proposal_id = args.get("proposal_id")
    if not isinstance(proposal_id, str) or not proposal_id.strip() or len(proposal_id.strip()) > 256:
        raise ValueError("proposal_id_required")

    receipt = args.get("mutation_receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("mutation_receipt_required")
    receipt_id = receipt.get("receipt_id") or receipt.get("id")
    if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id.strip()) > 256:
        raise ValueError("mutation_receipt_required")

    idempotency_key = args.get("idempotency_key")
    if (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key.strip()) > 256
    ):
        raise ValueError("idempotency_key_required")

    if name != "memoryguard_rule_merge_capability_issue":
        capability_token = args.get("capability_token")
        if not isinstance(capability_token, str) or not capability_token.strip():
            raise ValueError("capability_token_required")
        if name == "memoryguard_rule_merge_approve":
            revisions = args.get("expected_definition_revisions")
            if not isinstance(revisions, Mapping) or not revisions:
                raise ValueError("proposal_revision_required")
        return

    secret = args.get("recovery_secret")
    if not isinstance(secret, str) or not secret or "=" in secret:
        raise ValueError("recovery_secret_invalid")
    if re.fullmatch(r"[A-Za-z0-9_-]+", secret) is None:
        raise ValueError("recovery_secret_invalid")
    padding = "=" * ((4 - len(secret) % 4) % 4)
    try:
        decoded = base64.b64decode(secret + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("recovery_secret_invalid") from None
    if len(decoded) < 32 or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != secret:
        raise ValueError("recovery_secret_invalid")


def _validate_v2_scope_arguments(name: str, args: Mapping[str, Any], context: Any) -> None:
    """Reject audience claims that disagree with the trusted V2 scope.

    Native rule creation accepts an explicit ``agent_project`` audience.  The
    public MCP boundary must validate both dimensions before dispatch: the
    native service normalizes the target id from trusted context, so checking
    only that normalized value would otherwise let a foreign project string
    survive into the persisted binding.
    """

    if name != "memoryguard_rule_create_auto" or not isinstance(args, Mapping):
        return
    raw_scope = args.get("scope", args.get("audience"))
    if not isinstance(raw_scope, Mapping):
        return
    target_type = str(raw_scope.get("target_type", raw_scope.get("type", "")) or "").strip().casefold()
    if target_type != "agent_project":
        return

    from .rule_scope import canonical_project_ref

    def trusted_value(key: str) -> str:
        if isinstance(context, Mapping):
            return str(context.get(key) or "")
        return str(getattr(context, key, "") or "")

    target_id = str(raw_scope.get("target_id", raw_scope.get("id", "")) or "").strip()
    if target_id and target_id != trusted_value("agent_instance_id"):
        raise ValueError("other_agent_scope_denied")
    requested_project = canonical_project_ref(raw_scope.get("project_ref") or raw_scope.get("target_id"))
    trusted_project = canonical_project_ref(trusted_value("project_ref"))
    if not requested_project or requested_project != trusted_project:
        raise ValueError("other_project_scope_denied")


def _v2_port_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return business args after removing client identity aliases.

    ``provider`` is normally an identity alias and is stripped.  The provider
    install tool is the exception: its schema uses provider as the explicit
    business target, so retain only a canonical allow-listed value.
    """
    payload = {
        key: value
        for key, value in dict(args).items()
        if key not in _V2_PAYLOAD_IDENTITY_KEYS
    }
    if name == "memoryguard_provider_install":
        provider = str(args.get("provider", "")).strip().casefold()
        if provider not in _V2_PROVIDER_TARGETS:
            raise ValueError("invalid_provider")
        payload["provider"] = provider
    return payload


def _v2_result_envelope(result: Any) -> dict[str, Any]:
    """Keep facade CallToolResult shape while compacting JSON text blocks."""
    if isinstance(result, dict) and "content" in result:
        if result.get("isError"):
            # Facade-provided error text is untrusted.  Preserve a structured
            # code where available; do not forward arbitrary exception/path
            # text through MCP content.
            payload = dict(result)
            code = safe_error_code(payload.get("code") or payload.get("error"), "v2_dispatch_failed")
            payload["code"] = code
            payload["error"] = code
            payload["content"] = [{"type": "text", "text": f"error: {code}"}]
            payload["isError"] = True
            return payload
        return compact_success_envelope(result)
    if isinstance(result, dict) and result.get("error"):
        payload = sanitize_public_payload(dict(result), error_code="v2_dispatch_failed")
        payload.setdefault("ok", False)
        return _mcp_json_error(payload)
    return {"content": [{"type": "text", "text": response_json_text(result)}]}


_RESPONSE_READ_OPERATION = "memoryguard_response_read"
_RESPONSE_PAGING_ARGUMENTS = frozenset({
    "response_ref", "response_fields", "response_offset", "response_limit",
})


def _response_context_scope(context: Any) -> ResponseScope:
    if isinstance(context, Mapping):
        return ResponseScope.from_mapping(context)
    return ResponseScope.from_mapping({
        key: getattr(context, key, "")
        for key in (
            "workspace_id", "share_group_id", "agent_instance_id", "project_ref",
            "provider", "runtime_role", "session_id", "session_source",
            "session_trusted", "context_hash", "namespace_id", "sensitivity",
            "policy_class",
        )
    })


def _response_binding_revision(workspace: Path, context: Any) -> BindingRevision:
    """Read one active binding row; never enumerate a group or data source."""

    if isinstance(context, Mapping):
        agent = str(context.get("agent_instance_id") or "")
        group = str(context.get("share_group_id") or "")
    else:
        agent = str(getattr(context, "agent_instance_id", "") or "")
        group = str(getattr(context, "share_group_id", "") or "")
    if not agent or not group:
        raise ResponseReferenceError("response_ref_access_denied")
    try:
        from .runtime_v2.group_native import GroupControlService

        binding = GroupControlService(workspace, write=False).active_binding_for_agent(agent)
    except Exception as exc:
        raise ResponseReferenceError("response_ref_access_denied") from exc
    if not isinstance(binding, Mapping) or str(binding.get("share_group_id") or "") != group:
        raise ResponseReferenceError("response_ref_access_denied")
    revision = binding.get("revision")
    if type(revision) is not int or revision < 1:
        raise ResponseReferenceError("response_ref_access_denied")
    binding_id = str(binding.get("binding_id") or "")
    if not binding_id:
        raise ResponseReferenceError("response_ref_access_denied")
    return BindingRevision(binding_id, revision, group, agent)


def _response_is_public_catalog(name: str, args: Mapping[str, Any]) -> bool:
    if name == "memoryguard_capabilities":
        return True
    return (
        name == "memoryguard_invoke"
        and str(args.get("operation") or "") == "memoryguard_capabilities"
    )


def _response_replayable_read(name: str, args: Mapping[str, Any]) -> bool:
    """Only ordinary reads may be re-dispatched during reference paging."""

    if name in _DB_WRITING_TOOLS or name == _RESPONSE_READ_OPERATION:
        return False
    if name != "memoryguard_invoke":
        return name not in _MUTATING_TOOLS
    target = str(args.get("operation") or "")
    if target == _RESPONSE_READ_OPERATION:
        return False
    return (
        target not in _MUTATING_TOOLS
        and target not in GUI_MUTATION_NAMES
        and target not in _DB_WRITING_TOOLS
    )


def _validate_response_paging_arguments(name: str, args: Mapping[str, Any]) -> None:
    """Reject response paging before a write reaches any handler."""

    if name in _MUTATING_TOOLS and any(key in args for key in _RESPONSE_PAGING_ARGUMENTS):
        raise ValueError("response_pagination_read_only")
    if name != "memoryguard_invoke":
        return
    target = str(args.get("operation") or "")
    nested = args.get("arguments")
    if target in _MUTATING_TOOLS or target in GUI_MUTATION_NAMES:
        if isinstance(nested, Mapping) and any(key in nested for key in _RESPONSE_PAGING_ARGUMENTS):
            raise ValueError("response_pagination_read_only")


def _revalidate_response_read(
    name: str,
    args: Mapping[str, Any],
    workspace: Path,
    expected_digest: str,
) -> str | None:
    """Re-run the original read internally; changed data never exposes a snapshot."""

    replayed = _v2_cutover_dispatch(name, deepcopy(dict(args)), workspace)
    if not isinstance(replayed, Mapping) or replayed.get("isError"):
        return "response_ref_expired"
    replayed = _hidden_tool_deprecation(dict(replayed), name)
    if response_digest(compact_success_envelope(replayed)) != expected_digest:
        return "response_ref_result_changed"
    return None


def _read_response_reference(args: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    """Serve one bounded response page through the broker-only extension."""

    nested = args.get("arguments")
    if not isinstance(nested, Mapping):
        return _mcp_json_error({"ok": False, "error": "broker_arguments_invalid", "code": "broker_arguments_invalid"})
    if set(nested) - {"response_ref", "fields", "offset", "limit"}:
        return _mcp_json_error({"ok": False, "error": "response_arguments_invalid", "code": "response_arguments_invalid"})
    response_ref = nested.get("response_ref")
    if not isinstance(response_ref, str):
        return _mcp_json_error({"ok": False, "error": "response_ref_invalid", "code": "response_ref_invalid"})
    try:
        public = GLOBAL_RESPONSE_STORE.is_public(response_ref)
        scope = None
        binding = None
        if not public:
            context, error = _trusted_context_for_v2(dict(args), workspace)
            if error or context is None:
                raise ResponseReferenceError("response_ref_access_denied")
            scope = _response_context_scope(context)
            binding = _response_binding_revision(workspace, context)
        page = GLOBAL_RESPONSE_STORE.read(
            response_ref,
            scope=scope,
            binding=binding,
            fields=nested.get("fields"),
            offset=nested.get("offset", 0),
            limit=nested.get("limit", 3000),
        )
    except ResponseReferenceError as exc:
        return _mcp_json_error({"ok": False, "error": exc.code, "code": exc.code})
    result = _v2_result_envelope(page)
    # Page text and selector names are JSON-encoded once more by the MCP
    # envelope. Verify the actual outbound CallToolResult, not source limits.
    # Never turn a page into another reference.
    if len(response_json_bytes(result)) > DEFAULT_RESPONSE_BUDGET_BYTES:
        return _mcp_json_error({
            "ok": False,
            "error": "response_page_too_large",
            "code": "response_page_too_large",
        })
    return result


def _budget_success_response(
    result: dict[str, Any],
    *,
    name: str,
    args: Mapping[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    """Apply response budget after every success wrapper has run."""

    if result.get("isError") or not _response_replayable_read(name, args):
        return compact_success_envelope(result)
    compacted = compact_success_envelope(result)
    if len(response_json_bytes(compacted)) <= DEFAULT_RESPONSE_BUDGET_BYTES:
        return compacted
    public = _response_is_public_catalog(name, args)
    expected_digest = response_digest(compacted)
    try:
        if public:
            scope = None
            binding = None
        else:
            context, error = _trusted_context_for_v2(dict(args), workspace)
            if error or context is None:
                raise ResponseReferenceError("response_ref_access_denied")
            scope = _response_context_scope(context)
            binding = _response_binding_revision(workspace, context)
        response_ref = GLOBAL_RESPONSE_STORE.put(
            compacted,
            scope=scope,
            binding=binding,
            public=public,
            revalidate=lambda: _revalidate_response_read(name, args, workspace, expected_digest),
        )
    except ResponseReferenceError as exc:
        # The read completed, but returning a giant body would violate the
        # public budget. State delivery failure without pretending the source
        # operation failed; writes/bootstrap never enter this branch.
        return unavailable_summary(compacted, code=exc.code)
    return paged_summary(compacted, response_ref)


def _hidden_tool_deprecation(result: dict[str, Any], name: str) -> dict[str, Any]:
    """Annotate every direct result for an undiscovered legacy tool.

    The MCP result payload is already JSON for every V2 mapping response.  Put
    the machine-readable notice beside that business payload; preserve the
    original CallToolResult shape and error state.  A top-level copy remains
    available to callers that do not parse text content.
    """
    if name in MCP_DEFAULT_PUBLIC_TOOL_NAMES or name not in CALLABLE_TOOL_NAMES:
        return result
    notice = {
        "deprecated": True,
        "code": "mcp_tool_not_listed",
        "message": "Advanced legacy tool remains callable but is not listed for new MCP clients.",
    }
    updated = dict(result)
    updated["deprecated"] = True
    updated["deprecation"] = notice
    content = updated.get("content")
    if not isinstance(content, list) or not content or not isinstance(content[0], Mapping):
        return updated
    text = content[0].get("text")
    if not isinstance(text, str):
        return updated
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return updated
    if not isinstance(payload, Mapping):
        return updated
    body = dict(payload)
    body["deprecated"] = True
    body["deprecation"] = notice
    first = dict(content[0])
    first["text"] = json.dumps(body, ensure_ascii=False, indent=2)
    updated["content"] = [first, *content[1:]]
    return updated


def _v2_compensate_evidence_after_undo(
    workspace: Path,
    context: Any,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the V2 evidence projection side of a native feedback undo.

    ``rule_lifecycle`` owns the immutable feedback and compensating decision.
    The MCP route completes that lifecycle by rebuilding the body-free V2
    effective-evidence projection in a second, immediate transaction.  The
    transaction is idempotent: replaying the same undo simply deactivates the
    same contribution and writes the same deterministic winner row.  No raw
    evidence value is selected, copied, or returned.
    """

    data = result.get("data")
    if not isinstance(data, Mapping):
        return dict(result)
    compensation = data.get("compensation")
    if not isinstance(compensation, Mapping):
        return dict(result)
    feedback_id = str(compensation.get("feedback_id") or "").strip()
    if not feedback_id:
        return dict(result)

    if isinstance(context, Mapping):
        share_group_id = str(context.get("share_group_id") or "")
    else:
        share_group_id = str(getattr(context, "share_group_id", "") or "")
    if not share_group_id:
        raise ValueError("v2_evidence_scope_required")

    from .rules.v2_store import RuleV2Store

    store = RuleV2Store(workspace)
    projections: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    with store.transaction() as conn:
        affected = conn.execute(
            "SELECT c.contribution_id,c.definition_id,c.independence_key "
            "FROM rule_evidence_contributions c "
            "JOIN rule_receipt_refs r ON r.receipt_id=c.receipt_id "
            "WHERE c.feedback_id=? AND r.share_group_id=?",
            (feedback_id, share_group_id),
        ).fetchall()
        for contribution_id, definition_id, independence_key in affected:
            conn.execute(
                "UPDATE rule_evidence_contributions SET active=0,updated_at=? "
                "WHERE contribution_id=?",
                (now, contribution_id),
            )
            candidate = conn.execute(
                "SELECT c.contribution_id,c.kind,c.polarity,c.authority,c.confidence,c.observed_at "
                "FROM rule_evidence_contributions c "
                "JOIN rule_receipt_refs r ON r.receipt_id=c.receipt_id "
                "WHERE c.definition_id=? AND c.independence_key=? AND c.active=1 "
                "AND r.share_group_id=? "
                "ORDER BY c.authority DESC,c.confidence DESC,c.observed_at DESC,c.contribution_id ASC "
                "LIMIT 1",
                (definition_id, independence_key, share_group_id),
            ).fetchone()
            conn.execute(
                "DELETE FROM rule_evidence_effective WHERE definition_id=? AND independence_key=?",
                (definition_id, independence_key),
            )
            projection = {
                "definition_id": str(definition_id),
                "independence_key": str(independence_key),
                "winner_contribution_id": "",
                "polarity": "",
            }
            if candidate is not None:
                winner_id, kind, polarity, authority, confidence, observed_at = candidate
                effective_id = hashlib.sha256(
                    f"native-v2-evidence-effective\x00{definition_id}\x00{independence_key}".encode("utf-8")
                ).hexdigest()
                conn.execute(
                    "INSERT INTO rule_evidence_effective("
                    "effective_id,definition_id,independence_key,kind,winner_contribution_id,"
                    "polarity,authority,confidence,observed_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        effective_id,
                        str(definition_id),
                        str(independence_key),
                        str(kind or "evidence"),
                        str(winner_id),
                        str(polarity),
                        int(authority or 0),
                        float(confidence or 0.0),
                        str(observed_at or ""),
                        now,
                    ),
                )
                projection.update({
                    "winner_contribution_id": str(winner_id),
                    "polarity": str(polarity),
                })
            projections.append(projection)

    updated = dict(result)
    updated_data = dict(data)
    updated_data["evidence_projection"] = projections
    updated["data"] = updated_data
    return updated


def _v2_cutover_dispatch(name: str, args: dict[str, Any], workspace: Path) -> dict[str, Any] | None:
    """Route every MCP request through V2 or return a stable upgrade error."""
    facade = _load_v2_runtime_facade(workspace)
    if facade is _V2_FACADE_MISSING:
        return _mcp_json_error(v2_upgrade_payload("UNKNOWN", surface="MCP"))
    _remember_stdio_facade(facade)
    state, snapshot = _v2_facade_state(facade, workspace)
    if state not in _V2_READ_STATES:
        return _mcp_json_error(v2_upgrade_payload(state, surface="MCP"))
    if name not in CALLABLE_TOOL_NAMES:
        return _mcp_error(f"unknown tool: {name}")
    # Discovery is pure registry metadata: it remains useful when a local MCP
    # launch has no active binding or CodeGraph installation.  It still reads
    # the V2 manifest state above, so an unavailable/old control plane cannot
    # masquerade as a current capability catalog.
    if name == "memoryguard_capabilities":
        try:
            return _v2_result_envelope(mcp_capability_catalog(args))
        except ValueError as exc:
            code = safe_error_code(exc, "invalid_tool_arguments")
            return _mcp_json_error({"ok": False, "error": code, "code": code})
    try:
        _validate_response_paging_arguments(name, args)
    except ValueError as exc:
        code = safe_error_code(exc, "invalid_tool_arguments")
        return _mcp_json_error({"ok": False, "error": code, "code": code})
    broker_gui_target: tuple[str, dict[str, object]] | None = None
    if name == "memoryguard_invoke":
        if str(args.get("operation") or "") == _RESPONSE_READ_OPERATION:
            return _read_response_reference(args, workspace)
        try:
            target_surface, target, target_args = resolve_mcp_broker_invocation(
                args,
                tool_names=CALLABLE_TOOL_NAMES,
                mutation_names=_MUTATING_TOOLS,
                gui_names=MCP_BROKER_GUI_METHOD_NAMES,
                gui_mutation_names=GUI_MUTATION_NAMES,
            )
        except ValueError as exc:
            code = safe_error_code(exc, "invalid_tool_arguments")
            return _mcp_json_error({"ok": False, "error": code, "code": code})
        if target_surface == "mcp":
            return _v2_cutover_dispatch(target, deepcopy(target_args), workspace)
        broker_gui_target = (target, target_args)
        if state == "V2_READY" and target in GUI_MUTATION_NAMES:
            return _mcp_json_error({"ok": False, "error": "v2_not_active", "code": "v2_not_active"})
    # V2_READY permits reads/bootstrap only; mutations must never touch either
    # port. The imported mutation ledger is the sole classifier.
    if state == "V2_READY" and name in _MUTATING_TOOLS:
        return _mcp_json_error({"ok": False, "error": "v2_not_active", "code": "v2_not_active"})
    lease_error = _runtime_lease_guard(
        name,
        args,
        workspace,
        force_write=bool(
            broker_gui_target is not None
            and broker_gui_target[0] in GUI_MUTATION_NAMES
        ),
    )
    if lease_error is not None:
        return lease_error
    try:
        _validate_v2_mcp_arguments(name, args)
    except ValueError as exc:
        code = safe_error_code(exc, "invalid_tool_arguments")
        return _mcp_json_error({
            "ok": False,
            "error": code,
            "code": code,
        })
    dispatch = getattr(facade, "dispatch_mcp", None)
    if not callable(dispatch):
        return _mcp_json_error({"ok": False, "error": "v2_dispatch_unavailable", "code": "v2_dispatch_unavailable"})
    context, context_error = _trusted_context_for_v2(args, workspace)
    # Non-memory read tools may not have a binding; their V2 implementation can
    # still run without an identity.  Scope-sensitive tools fail closed.
    scoped = (
        name in _MUTATING_TOOLS
        or name.startswith("memoryguard_memory_")
        or name.startswith("memoryguard_rule_")
        or name.startswith("memoryguard_history_")
        or name.startswith("memoryguard_binding_")
        or name in {"memoryguard_context_bootstrap", "memoryguard_accept_candidates", "memoryguard_external_mcp_import"}
    )
    if context_error and (scoped or broker_gui_target is not None):
        return _mcp_json_error({"ok": False, "error": context_error, "code": context_error})
    try:
        _validate_v2_scope_arguments(name, args, context)
    except ValueError as exc:
        code = safe_error_code(exc, "invalid_tool_arguments")
        return _mcp_json_error({
            "ok": False,
            "error": code,
            "code": code,
        })
    try:
        params = inspect.signature(dispatch).parameters
        has_context = "context" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        accepts_snapshot = "snapshot" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        has_context = False
        accepts_snapshot = False
    if not has_context:
        return _mcp_json_error({"ok": False, "error": "v2_context_capability_required", "code": "v2_context_capability_required"})
    if broker_gui_target is not None:
        gui_name, gui_args = broker_gui_target
        gui_dispatch = getattr(facade, "dispatch_gui", None)
        if not callable(gui_dispatch):
            return _mcp_json_error({"ok": False, "error": "v2_gui_dispatch_unavailable", "code": "v2_gui_dispatch_unavailable"})
        try:
            gui_params = inspect.signature(gui_dispatch).parameters
            gui_accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in gui_params.values())
            gui_kwargs: dict[str, Any] = {"context": context}
            if "snapshot" in gui_params or gui_accepts_kwargs:
                gui_kwargs["snapshot"] = snapshot
            result = gui_dispatch(gui_name, deepcopy(gui_args), **gui_kwargs)
        except Exception as exc:
            return _mcp_json_error({
                "ok": False,
                "error": "v2_dispatch_failed",
                "code": "v2_dispatch_failed",
                "diagnostic": safe_exception_diagnostic(exc, code="v2_dispatch_failed"),
            })
        return _v2_result_envelope(result)
    try:
        port_args = _v2_port_args(name, args)
    except ValueError as exc:
        code = safe_error_code(exc, "invalid_tool_arguments")
        return _mcp_json_error({
            "ok": False,
            "error": code,
            "code": code,
            "diagnostic": safe_exception_diagnostic(exc, code=code),
        })
    try:
        kwargs: dict[str, Any] = {"context": context}
        # Phase6 facade consumes the immutable snapshot read above.  Passing
        # it is what enforces one manifest read per tools/call; older fakes
        # without the optional parameter remain compatible.
        if accepts_snapshot:
            kwargs["snapshot"] = snapshot
        # Identity is conveyed only through the trusted context.  Never hand
        # attacker-controlled aliases to the V2 port as a second authority.
        result = dispatch(name, port_args, **kwargs)
    except Exception as exc:
        return _mcp_json_error({
            "ok": False,
            "error": "v2_dispatch_failed",
            "code": "v2_dispatch_failed",
            "diagnostic": safe_exception_diagnostic(exc, code="v2_dispatch_failed"),
        })
    if name == "memoryguard_rule_undo" and isinstance(result, Mapping) and result.get("ok") is not False:
        try:
            result = _v2_compensate_evidence_after_undo(workspace, context, result)
        except Exception as exc:
            return _mcp_json_error({
                "ok": False,
                "error": "v2_evidence_projection_failed",
                "code": "v2_evidence_projection_failed",
                "diagnostic": safe_exception_diagnostic(exc, code="v2_evidence_projection_failed"),
            })
    return _v2_result_envelope(result)



def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch MCP exclusively through the V2 state gate."""
    workspace: Path | None = None
    request_args: dict[str, Any] = {}
    try:
        # Transport arguments are request-owned data.  Native handlers are
        # allowed to normalize/pop transport fields, but never on the
        # caller-owned object (including nested metadata/evidence values).
        request_args = deepcopy(args)
        workspace = _resolve_memory_workspace(request_args)
        result = _v2_cutover_dispatch(name, request_args, workspace)
    except Exception as exc:
        from .workspace_resolver import WorkspaceResolutionError

        if isinstance(exc, WorkspaceResolutionError):
            result = _mcp_json_error(exc.to_payload(surface="MCP"))
        else:
            payload = v2_upgrade_payload("UNKNOWN", surface="MCP")
            payload["diagnostic"] = safe_exception_diagnostic(
                exc, code="v2_manifest_state_unavailable",
            )
            result = _mcp_json_error(payload)
    if result is None:
        result = _mcp_json_error(v2_upgrade_payload("UNKNOWN", surface="MCP"))
    result = _hidden_tool_deprecation(result, name)
    if workspace is None:
        return compact_success_envelope(result) if not result.get("isError") else result
    return _budget_success_response(
        result,
        name=name,
        args=request_args,
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# JSON-RPC 处理
# ---------------------------------------------------------------------------


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理单个 JSON-RPC 请求，返回响应 dict（通知返回 None）。"""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "resources/templates/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resourceTemplates": []},
        }

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            result = execute_tool(name, args)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"tool execution failed: {e}"},
            }

    if method == "notifications/initialized":
        return None  # 通知，无响应

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _runtime_lease_guard(
    name: str,
    args: dict[str, Any],
    workspace: Path,
    *,
    force_write: bool = False,
) -> dict[str, Any] | None:
    """Fail-closed runtime split-brain guard for DB-writing tools (Req10).

    Tools that only read return ``None`` immediately.  For any tool that can
    write SQLite state (including bootstrap receipt persistence) the
    workspace's runtime lease is checked -- the first such call also acquires
    this process's lease.  When a live process already holds the same database
    set with a different memoryguard version / code fingerprint, the call is
    rejected with ``runtime_split_brain`` and ``restart_required=True``; the
    conflicting process is never killed.  Returns ``None`` when the lease is
    granted.
    """
    if not force_write and name not in _DB_WRITING_TOOLS:
        return None
    from .runtime_lease import check_runtime_lease

    result = check_runtime_lease(workspace, pid=os.getpid())
    if result.get("granted"):
        return None
    conflicting = result.get("conflicting", [])
    pids = sorted(str(c.get("pid", "")) for c in conflicting)
    text = (
        "runtime_split_brain: another live process holds this workspace with "
        "a different build; refusing to write. "
        f"restart_required=true; conflicting_pids={pids}"
    )
    return _mcp_json_error({
        "ok": False,
        "error": "runtime_split_brain",
        "restart_required": True,
        "conflicting": conflicting,
        "message": text,
    })


def serve_stdio() -> int:
    """MCP stdio 主循环。从 stdin 读 JSON-RPC，向 stdout 写响应。"""
    # MCP stdio 协议固定使用 UTF-8。Windows 中文系统的管道默认可能是
    # GBK；工具描述或记忆正文含中文时会让宿主无法解码整条 JSON-RPC。
    global _stdio_owned_facades
    previous_owned = _stdio_owned_facades
    owned: list[Any] = []
    _stdio_owned_facades = owned
    try:
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="strict")
        # Runtime state is gated per request by execute_tool. Startup only
        # configures encoding and never performs legacy recovery.
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as e:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}}
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
                continue
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(response_json_text(response) + "\n")
                sys.stdout.flush()
        return 0
    finally:
        _stdio_owned_facades = previous_owned
        for facade in owned:
            shutdown = getattr(facade, "shutdown", None) or getattr(facade, "close", None)
            if callable(shutdown):
                try:
                    shutdown(timeout=5.0)
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(serve_stdio())
