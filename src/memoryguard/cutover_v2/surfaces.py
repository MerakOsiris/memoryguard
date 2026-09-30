"""Pure public surface contracts shared by cutover adapters and native ports.

GUI operations deliberately have one structured source of truth.  Compatibility
sets such as ``GUI_METHOD_NAMES`` and ``GUI_MUTATION_NAMES`` are projections of
that registry; callers must not maintain independent allowlists or mutation
classifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


MCP_TOOL_NAMES = frozenset({
    "memoryguard_audit", "memoryguard_explain", "memoryguard_list_sources",
    "memoryguard_scan_summary", "memoryguard_neuron_graph", "memoryguard_import_preview",
    "memoryguard_memory_read", "memoryguard_memory_search", "memoryguard_memory_write",
    "memoryguard_memory_update", "memoryguard_memory_delete", "memoryguard_memory_status",
    "memoryguard_memory_merge_safe", "memoryguard_memory_merge_safe_preview",
    "memoryguard_context_bootstrap", "memoryguard_rule_feedback", "memoryguard_rule_create_auto",
    "memoryguard_rule_decision_read", "memoryguard_rule_undo", "memoryguard_rule_scope_stats",
    "memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_rule_merge_safe", "memoryguard_rule_merge_safe_preview",
    "memoryguard_binding_create", "memoryguard_binding_list", "memoryguard_external_mcp_list",
    "memoryguard_external_mcp_import", "memoryguard_extract_memories", "memoryguard_accept_candidates",
    "memoryguard_semantic_check", "memoryguard_provider_install", "memoryguard_resolve_group",
    "memoryguard_list_pending_enrichments", "memoryguard_apply_enrichments", "memoryguard_enrichment_status",
    "memoryguard_build_and_enrich", "memoryguard_canonical_status", "memoryguard_diagnostics_snapshot",
    "memoryguard_projection_status", "memoryguard_runtime_processes",
    "memoryguard_history_search", "memoryguard_history_timeline", "memoryguard_history_read",
    "memoryguard_history_extract_preview", "memoryguard_history_list_sessions",
    "memoryguard_history_export", "memoryguard_history_delete",
    "memoryguard_knowledge_list", "memoryguard_knowledge_search", "memoryguard_knowledge_read",
    "memoryguard_knowledge_book", "memoryguard_knowledge_candidates",
    "memoryguard_codegraph_graph",
    "memoryguard_codegraph_query", "memoryguard_codegraph_path", "memoryguard_codegraph_explain",
    "memoryguard_codegraph_affected", "memoryguard_codegraph_update", "memoryguard_codegraph_status",
    "memoryguard_capabilities", "memoryguard_invoke",
    "memoryguard_knowledge_add", "memoryguard_knowledge_reingest",
    "memoryguard_knowledge_rebuild_smart", "memoryguard_knowledge_remove",
    "memoryguard_knowledge_restore", "memoryguard_knowledge_purge_deleted",
    "memoryguard_knowledge_update_settings", "memoryguard_knowledge_candidate_review",
    "memoryguard_task_status", "memoryguard_task_list", "memoryguard_task_cancel",
    "memoryguard_codegraph_build_bound",
})

# Default MCP discovery is intentionally limited to the durable, day-to-day
# memory contract.  ``MCP_TOOL_NAMES`` remains the complete callable V2
# registry so installed clients can keep using previously advertised advanced
# operations during the compatibility window.
MCP_DEFAULT_PUBLIC_TOOL_NAMES = (
    "memoryguard_context_bootstrap",
    "memoryguard_memory_search",
    "memoryguard_memory_read",
    "memoryguard_memory_write",
    "memoryguard_memory_update",
    "memoryguard_memory_delete",
    "memoryguard_memory_status",
    "memoryguard_audit",
    "memoryguard_explain",
    "memoryguard_capabilities",
    "memoryguard_invoke",
)

# Static reviewed snapshot. New GUI registrations default to unavailable to
# MCP until they are deliberately added here.
_MCP_BROKER_APPROVED_GUI_NAMES = frozenset("""
accept_candidates add_source apply_build apply_enrichments apply_memoryguard_gc apply_plan
archive_agent_dir archive_memory_group backfill_local_history bind_agent bind_agents_to_shared_group
build_projection cancel_build_projection check_binding_drift clear_memory_group close_stale_conflict
codegraph_status commit_selection commit_shared_memory_governance create_build_plan
create_child_exception create_import create_rule_exception create_rule_from_text delete_archived_agent
delete_history delete_memory delete_projection delete_quarantine detect_external_mcp discover_agents
discover_local_history_sources dissolve_shared_group edit_memory ensure_personal_memory_group
enter_multi_agent_mode exit_multi_agent_mode export_history export_memory_group extract_preview
extract_preview_by_path generate_plan get_agent_data get_api_method_registry get_audit get_auto_actions
get_build_progress get_codegraph_graph get_conflicts get_enrichment_status get_global_memory_status
get_governance_scope get_governance_scope_state get_governance_snapshot get_host_enrichment_guide
get_host_hook_status get_memory get_memory_ir get_memory_neuron_graph get_memory_source_map
get_memory_status get_neuron_graph get_projection_source_map get_quarantine get_raw_memory
get_recent_events get_request_status get_residual_cleanup get_rule_auto_scope_metrics
get_rule_scope_options get_sandbox_status get_selection_tree get_shared_group_preview
get_source_file_content get_storage_overview get_supersede_chain get_supersede_decisions
get_usage_telemetry history_extract_preview history_read history_timeline import_external_mcp_entries
import_native_memories_to_group install_shared_group_mcp_redirects knowledge_add knowledge_book
knowledge_candidate_review knowledge_candidate_targets knowledge_candidates_list knowledge_deleted_list
knowledge_job_status knowledge_list knowledge_purge_deleted knowledge_read knowledge_rebuild_smart
knowledge_reingest knowledge_remove knowledge_restore knowledge_search knowledge_update_settings
leave_shared_group_to_personal list_agent_candidates list_agents list_archived_agents list_bindings
list_cleanup_history list_external_mcp_servers list_history list_history_sessions list_host_llm_agents
list_memory list_memory_versions list_native_memory_releases list_pending_enrichments
list_pending_requests list_publish_targets list_releases list_rule_cockpit list_rule_decisions
list_rule_exceptions list_rule_match_receipts list_rules_habits list_share_groups list_sources
lock_memory mark_agent_uninstalled neuron_decide plan_memoryguard_gc preview_effective_rules
preview_external_mcp_import preview_import preview_source publish_reconstructed_memory
read_rule_decision release_quarantine remove_source resolve_conflict restore_archived_agent
restore_memory revoke_rule_exception rollback_memory rollback_native_memory_release rollback_release
run_audit scan_sources search_history search_memory set_governance_scope set_host_hook_mode
set_memory_injection_policy set_projection_source_enabled start_build_projection submit_rule_feedback
sync_usage_telemetry unbind_agent undo_change undo_rule_decision uninstall_host_hook unlock_memory
unmark_agent_uninstalled update_rule_audience update_rule_body verify_release
""".split())
_MCP_BROKER_UNAVAILABLE = {
    "pick_path": "desktop_only",
    "choose_publish_target_path": "desktop_only",
    "open_agent_folder": "desktop_only",
    "call_readonly": "bridge_protocol_only",
    "request_mutation": "bridge_protocol_only",
    "submit_request": "bridge_protocol_only",
    "list_codegraph_projects": "desktop_admin_only",
    "build_codegraph": "desktop_admin_only",
    "set_codegraph_automation": "desktop_admin_only",
}

# Headless MCP entrypoints that reuse a narrow, bound GUI business operation.
# This is deliberately a mapping rather than a generic GUI reflector: callers
# may only reach entries reviewed for an MCP-issued native capability.
MCP_GUI_BRIDGE_OPERATIONS: Mapping[str, str] = {
    "memoryguard_knowledge_add": "knowledge_add",
    "memoryguard_knowledge_reingest": "knowledge_reingest",
    "memoryguard_knowledge_rebuild_smart": "knowledge_rebuild_smart",
    "memoryguard_knowledge_remove": "knowledge_remove",
    "memoryguard_knowledge_restore": "knowledge_restore",
    "memoryguard_knowledge_purge_deleted": "knowledge_purge_deleted",
    "memoryguard_knowledge_update_settings": "knowledge_update_settings",
    "memoryguard_knowledge_candidate_review": "knowledge_candidate_review",
    "memoryguard_task_status": "get_request_status",
    "memoryguard_task_list": "list_pending_requests",
    "memoryguard_task_cancel": "cancel_build_projection",
}


def resolve_mcp_broker_invocation(
    payload: Mapping[str, object],
    *,
    tool_names: Iterable[str] = MCP_TOOL_NAMES,
    mutation_names: Iterable[str] = (),
    gui_names: Iterable[str] = (),
    gui_mutation_names: Iterable[str] = (),
) -> tuple[str, str, dict[str, object]]:
    """Validate broker envelope and return one registered target call.

    The broker is an explicit selector over known MCP operations.  It is never
    a reflective path to native handlers or desktop methods.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("broker_arguments_required")
    target = str(payload.get("operation") or "").strip()
    if not target:
        raise ValueError("broker_operation_required")
    allowed = frozenset(str(name) for name in tool_names)
    allowed_gui = frozenset(str(name) for name in gui_names)
    if target == "memoryguard_invoke":
        raise ValueError("broker_recursion_forbidden")
    if target in allowed:
        surface = "mcp"
    elif target in allowed_gui:
        surface = "gui"
    else:
        raise ValueError("broker_operation_unknown")
    raw_arguments = payload.get("arguments", {})
    if not isinstance(raw_arguments, Mapping):
        raise ValueError("broker_arguments_invalid")
    arguments = dict(raw_arguments)
    if target in (frozenset(str(name) for name in mutation_names) | frozenset(str(name) for name in gui_mutation_names)):
        if payload.get("confirmed") is not True:
            raise ValueError("confirmation_required")
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 256:
            raise ValueError("idempotency_key_required")
        for field, value in (("confirmed", True), ("idempotency_key", key.strip())):
            existing = arguments.get(field)
            if existing not in (None, "", value):
                raise ValueError("broker_argument_conflict")
            if existing in (None, ""):
                arguments[field] = value
    return surface, target, arguments


@dataclass(frozen=True)
class GuiOperationSpec:
    """Canonical contract for one public desktop/localhost GUI operation."""

    public_name: str
    canonical_name: str
    domain: str
    kind: str  # read | mutation
    execution: str  # sync | task
    native_handler: str
    parameters: tuple[str, ...] = ()
    cancel_operation: str = ""
    idempotency: str = "none"
    confirmation: str = "none"
    mcp_broker: str = "not_reviewed"

    def __post_init__(self) -> None:
        if not self.public_name or not self.canonical_name or not self.domain or not self.native_handler:
            raise ValueError("GUI operation requires public/canonical/domain/native handler")
        if self.kind not in {"read", "mutation"}:
            raise ValueError("GUI operation kind must be read or mutation")
        if self.execution not in {"sync", "task"}:
            raise ValueError("GUI operation execution must be sync or task")
        if self.execution == "task" and self.kind != "mutation" and self.canonical_name != "task_status":
            raise ValueError("only mutation operations may start tasks")
        if self.mcp_broker not in {"headless_broker", "desktop_only", "desktop_admin_only", "bridge_protocol_only", "not_reviewed"}:
            raise ValueError("invalid GUI MCP broker exposure")

    @property
    def mutation(self) -> bool:
        return self.kind == "mutation"

    def to_dict(self) -> dict[str, object]:
        return {
            "public_name": self.public_name,
            "canonical_name": self.canonical_name,
            "domain": self.domain,
            "kind": self.kind,
            "execution": self.execution,
            "native_handler": self.native_handler,
            "parameters": list(self.parameters),
            "cancel_operation": self.cancel_operation,
            "idempotency": self.idempotency,
            "confirmation": self.confirmation,
            "mcp_broker": self.mcp_broker,
        }


_GUI_OPERATIONS: dict[str, GuiOperationSpec] = {}


def _add(
    public_name: str,
    canonical_name: str,
    domain: str,
    kind: str,
    native_handler: str,
    *,
    execution: str = "sync",
    parameters: Iterable[str] = (),
    cancel_operation: str = "",
    idempotency: str | None = None,
    confirmation: str | None = None,
    mcp_broker: str | None = None,
) -> None:
    if public_name in _GUI_OPERATIONS:
        raise ValueError(f"duplicate GUI operation: {public_name}")
    mutation = kind == "mutation"
    _GUI_OPERATIONS[public_name] = GuiOperationSpec(
        public_name=public_name,
        canonical_name=canonical_name,
        domain=domain,
        kind=kind,
        execution=execution,
        native_handler=native_handler,
        parameters=tuple(parameters),
        cancel_operation=cancel_operation,
        idempotency=idempotency or ("required" if mutation else "none"),
        confirmation=confirmation or ("required" if mutation else "none"),
        mcp_broker=mcp_broker or (
            "headless_broker" if public_name in _MCP_BROKER_APPROVED_GUI_NAMES
            else _MCP_BROKER_UNAVAILABLE.get(public_name, "not_reviewed")
        ),
    )


def _same(
    names: Iterable[str],
    domain: str,
    kind: str,
    handler: str,
    *,
    execution: str = "sync",
) -> None:
    for name in names:
        _add(name, name, domain, kind, handler, execution=execution)


# Bridge / host shell.  The transport aliases remain public but never become a
# second request queue or an authority source.
_add("call_readonly", "bridge_read", "bridge", "read", "bridge_transport", parameters=("method", "args"))
_add("request_mutation", "bridge_mutation", "bridge", "mutation", "bridge_transport", parameters=("method", "args"))
_add("get_api_method_registry", "gui_registry", "bridge", "read", "coverage")
_add("get_sandbox_status", "sandbox_status", "bridge", "read", "sandbox_status")
_add("pick_path", "pick_path", "host", "read", "gui_pick_path", parameters=("for_files",))

# Audit / plan.
_same(("get_audit", "run_audit"), "audit", "read", "reference_audit")
_add("generate_plan", "audit_plan_preview", "audit", "read", "gui_audit_plan", parameters=("finding_id",))
_add("apply_plan", "audit_plan_apply", "audit", "mutation", "gui_audit_plan", parameters=("plan_id",))
_add("undo_change", "audit_plan_undo", "audit", "mutation", "gui_audit_plan", parameters=("change_id",))
_add("get_storage_overview", "storage_overview", "maintenance", "read", "diagnostics_snapshot")

# Source / extraction / import.
_add("list_sources", "source_list", "content", "read", "list_sources")
_add("preview_source", "source_preview", "content", "read", "import_preview", parameters=("path", "source_type"))
_add("scan_sources", "source_scan", "content", "read", "scan_summary")
_add("add_source", "source_add", "content", "mutation", "gui_source_add", parameters=("path", "source_type", "display_name", "confirmed"))
_add("remove_source", "source_remove", "content", "mutation", "gui_source_remove", parameters=("source_id", "confirmed"))
_add("get_source_file_content", "source_content_preview", "content", "read", "gui_import_query", parameters=("source_id", "relative_path"))
_add("extract_preview", "extract_preview", "content", "read", "gui_extract_preview", parameters=("root_id", "relative_path", "max_segments"))
_add("extract_preview_by_path", "extract_preview_by_path", "content", "read", "gui_extract_by_path", parameters=("source_path",))
_add("accept_candidates", "extract_candidates_accept", "content", "mutation", "accept_candidates", parameters=("extract_id", "candidate_ids"))
_add("preview_import", "import_preview", "import", "read", "gui_import_query", parameters=("path",))
_add("create_import", "import_create", "import", "mutation", "gui_import_control", execution="task", parameters=("path", "confirmed", "agent_instance_id", "project_ref", "share_group_id"), cancel_operation="task_cancel")
_add("get_raw_memory", "source_memory_summary", "content", "read", "gui_import_query")
_add("get_memory_ir", "memory_ir_summary", "content", "read", "gui_governance_query")

# External MCP.
_same(("list_external_mcp_servers",), "external_mcp", "read", "external_mcp_list")
_add("preview_external_mcp_import", "external_mcp_preview", "external_mcp", "read", "external_mcp_preview", parameters=("server_id", "descriptor"))
_add("detect_external_mcp", "external_mcp_detect", "external_mcp", "read", "external_mcp_detect", parameters=("server_id", "descriptor"))
_add("import_external_mcp_entries", "external_mcp_import", "external_mcp", "mutation", "external_mcp_import", parameters=("descriptor_json", "server_id"))

# Memory reads and governed writes.
_same(("list_memory",), "memory", "read", "memory_list")
_add("get_memory", "memory_get", "memory", "read", "memory_read", parameters=("memory_id", "share_group_id"))
_same(("search_memory",), "memory", "read", "memory_search")
_same(("get_memory_status",), "memory", "read", "memory_status")
_add("get_global_memory_status", "memory_global_status", "memory", "read", "memory_global_status")
_add("list_memory_versions", "memory_versions", "memory", "read", "memory_versions", parameters=("share_group_id", "limit", "offset"))
_add("get_supersede_chain", "memory_supersede_chain", "memory", "read", "memory_supersede_chain", parameters=("memory_id", "share_group_id"))
_add("get_memory_source_map", "memory_source_map", "memory", "read", "gui_memory_source_map")
_add("edit_memory", "memory_edit", "memory", "mutation", "gui_memory_edit", parameters=("memory_id", "body"))
_add("lock_memory", "memory_lock", "memory", "mutation", "gui_memory_lock", parameters=("memory_id",))
_add("unlock_memory", "memory_unlock", "memory", "mutation", "gui_memory_unlock", parameters=("memory_id",))
_add("set_memory_injection_policy", "memory_policy", "memory", "mutation", "gui_memory_policy", parameters=("memory_id", "injection_policy", "priority"))
_add("restore_memory", "memory_restore", "memory", "mutation", "gui_memory_restore", parameters=("memory_id",))
_add("delete_memory", "memory_delete", "memory", "mutation", "gui_memory_delete", parameters=("memory_id",))
_add("rollback_memory", "memory_rollback", "memory", "mutation", "gui_memory_rollback", parameters=("version_id",))

# Evidence/governance views and commands.
_same(("get_governance_snapshot",), "governance", "read", "diagnostics_snapshot")
for _name in ("get_recent_events", "get_auto_actions", "get_conflicts", "get_quarantine", "get_supersede_decisions"):
    _add(_name, _name.removeprefix("get_"), "governance", "read", "gui_governance_query", parameters=("share_group_id",))
_add("resolve_conflict", "conflict_resolve", "governance", "mutation", "gui_governance_command", parameters=("conflict_group_id", "keep_id", "share_group_id", "confirmed"))
_add("close_stale_conflict", "conflict_close_stale", "governance", "mutation", "gui_governance_command", parameters=("conflict_group_id", "group_id", "share_group_id"))
_add("release_quarantine", "quarantine_release", "governance", "mutation", "gui_governance_command", parameters=("quarantine_id", "share_group_id"))
_add("delete_quarantine", "quarantine_delete", "governance", "mutation", "gui_governance_command", parameters=("quarantine_id", "share_group_id"))
_add("neuron_decide", "neuron_decide", "governance", "mutation", "gui_governance_command", parameters=("node_id", "action", "reason", "confirmed", "scope", "agent_instance_id", "share_group_id"))

# Rules.
_same(("list_rules_habits", "list_rule_cockpit"), "rules", "read", "gui_rule_snapshot")
_same(("list_rule_decisions",), "rules", "read", "gui_rule_decisions")
_same(("read_rule_decision",), "rules", "read", "rule_decision_read")
_same(("get_rule_auto_scope_metrics",), "rules", "read", "rule_scope_stats")
_same(("get_rule_scope_options",), "rules", "read", "gui_rule_scope_options")
_add(
    "preview_effective_rules",
    "preview_effective_rules",
    "rules",
    "read",
    "gui_rule_effective",
    parameters=("agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role"),
)
_add("list_rule_match_receipts", "rule_receipts", "rules", "read", "gui_rule_receipts", parameters=("share_group_id", "memory_id", "agent_instance_id", "limit"))
_add("list_rule_exceptions", "rule_exceptions", "rules", "read", "gui_rule_exceptions", parameters=("share_group_id", "parent_rule"))
_add("update_rule_audience", "rule_audience_update", "rules", "mutation", "gui_rule_audience_update", parameters=("memory_id", "assignments", "share_group_id", "injection_policy", "priority", "confirmed"))
_add("update_rule_body", "rule_body_update", "rules", "mutation", "gui_rule_body_update", parameters=("definition_id", "body", "expected_revision", "share_group_id"))
_add("create_rule_from_text", "rule_create", "rules", "mutation", "gui_rule_create", parameters=("text",))
_add("submit_rule_feedback", "rule_feedback", "rules", "mutation", "gui_rule_feedback", parameters=("receipt_id", "outcome", "evidence", "confidence"))
_add("undo_rule_decision", "rule_undo", "rules", "mutation", "gui_rule_undo", parameters=("decision_id", "share_group_id", "confirmed", "metadata"))
_add("create_child_exception", "rule_exception_create", "rules", "mutation", "gui_rule_exception", parameters=("parent_rule", "child_rule", "priority", "reason", "share_group_id", "confirmed"))
_add("create_rule_exception", "rule_exception_create", "rules", "mutation", "gui_rule_exception", parameters=("parent_rule", "child_rule", "priority", "reason", "share_group_id", "confirmed"))
_add("revoke_rule_exception", "rule_exception_revoke", "rules", "mutation", "gui_rule_exception", parameters=("exception_id", "share_group_id", "confirmed"))

# Knowledge.  Read service remains read-only; all writes route through a
# separate V2 command service and Content Plane ingestion.
_add("knowledge_list", "knowledge_list", "knowledge", "read", "knowledge_read", parameters=("query", "limit"))
_add("knowledge_search", "knowledge_search", "knowledge", "read", "knowledge_read", parameters=("query", "limit"))
_add("knowledge_read", "knowledge_read", "knowledge", "read", "knowledge_read", parameters=("occurrence_id",))
_add("knowledge_book", "knowledge_book", "knowledge", "read", "knowledge_book", parameters=("book_id", "query", "limit"))
_add("knowledge_candidates_list", "knowledge_candidates", "knowledge", "read", "knowledge_candidates", parameters=("book_id", "status"))
_add("knowledge_candidate_targets", "knowledge_candidate_targets", "knowledge", "read", "gui_knowledge_query")
_add("knowledge_deleted_list", "knowledge_deleted_list", "knowledge", "read", "gui_knowledge_query")
_add("knowledge_job_status", "task_status", "runtime", "read", "gui_task_status", parameters=("run_id",))
_add("knowledge_add", "knowledge_source_add", "knowledge", "mutation", "gui_knowledge_command", execution="task", parameters=("path", "title"), cancel_operation="task_cancel")
_add("knowledge_reingest", "knowledge_reingest", "knowledge", "mutation", "gui_knowledge_command", execution="task", parameters=("book_id",), cancel_operation="task_cancel")
_add("knowledge_rebuild_smart", "knowledge_rebuild_smart", "knowledge", "mutation", "gui_knowledge_command", execution="task", parameters=("book_id",), cancel_operation="task_cancel")
_add("knowledge_remove", "knowledge_remove", "knowledge", "mutation", "gui_knowledge_command", parameters=("book_id",))
_add("knowledge_restore", "knowledge_restore", "knowledge", "mutation", "gui_knowledge_command", parameters=("deletion_id",))
_add("knowledge_purge_deleted", "knowledge_purge_deleted", "knowledge", "mutation", "gui_knowledge_command", parameters=("deletion_id",))
_add("knowledge_update_settings", "knowledge_update_settings", "knowledge", "mutation", "gui_knowledge_command", parameters=("book_id", "settings"))
_add("knowledge_candidate_review", "knowledge_candidate_review", "knowledge", "mutation", "gui_knowledge_command", parameters=("candidate_id", "decision", "target_group_id"))

# Projection / task lifecycle.
_same(("get_neuron_graph", "get_memory_neuron_graph"), "projection", "read", "projection_graph")
_add("get_codegraph_graph", "codegraph_graph", "codegraph", "read", "codegraph_graph", parameters=("request",))
_add("list_codegraph_projects", "codegraph_projects", "codegraph", "read", "codegraph_projects")
_add("codegraph_status", "codegraph_status", "codegraph", "read", "codegraph_status", parameters=("request",))
_add("build_codegraph", "codegraph_build", "codegraph", "mutation", "codegraph_build", execution="task", parameters=("source_id", "confirmed"), cancel_operation="task_cancel")
_add("set_codegraph_automation", "codegraph_automation", "codegraph", "mutation", "codegraph_automation", parameters=("request",))
_add("get_projection_source_map", "projection_source_map", "projection", "read", "gui_projection_query", parameters=("scope", "agent_instance_id", "share_group_id", "mode"))
_add("get_build_progress", "task_status", "runtime", "read", "gui_task_status", parameters=("run_id",))
_add("build_projection", "projection_build", "projection", "mutation", "gui_projection_command", execution="task", parameters=("confirmed", "mode", "scope", "agent_instance_id", "share_group_id", "progress", "llm_agent", "llm_cli", "enrich_mode"), cancel_operation="task_cancel")
_add("start_build_projection", "projection_build", "projection", "mutation", "gui_projection_command", execution="task", parameters=("confirmed", "mode", "scope", "agent_instance_id", "share_group_id", "llm_agent", "llm_cli", "enrich_mode"), cancel_operation="task_cancel")
_add("cancel_build_projection", "task_cancel", "runtime", "mutation", "gui_task_cancel", parameters=("run_id", "confirmed"))
_add("delete_projection", "projection_delete", "projection", "mutation", "gui_projection_command", parameters=("confirmed", "mode", "scope", "agent_instance_id", "share_group_id"))
_add("set_projection_source_enabled", "projection_source_toggle", "projection", "mutation", "gui_projection_command", parameters=("root_id", "enabled", "scope", "agent_instance_id"))

# Release / build plan.
_add("list_native_memory_releases", "release_list", "release", "read", "gui_release_query", parameters=("scope", "agent_instance_id"))
_add("list_releases", "release_list", "release", "read", "gui_release_query")
_add("list_publish_targets", "release_targets", "release", "read", "gui_release_query", parameters=("scope", "agent_instance_id"))
_add("choose_publish_target_path", "release_target_choose", "release", "read", "gui_release_query", parameters=("kind",))
_add("create_build_plan", "release_plan_create", "release", "mutation", "gui_release_command", parameters=("target_path", "scope", "agent_instance_id", "target_root_id"))
_add("apply_build", "release_apply", "release", "mutation", "gui_release_command", execution="task", parameters=("plan_id", "confirmed", "target_path", "scope", "agent_instance_id", "target_root_id"), cancel_operation="task_cancel")
_add("publish_reconstructed_memory", "release_publish", "release", "mutation", "gui_release_command", execution="task", parameters=("target_file", "confirmed", "use_distilled", "scope", "agent_instance_id", "target_root_id"), cancel_operation="task_cancel")
_add("verify_release", "release_verify", "release", "read", "gui_release_query", parameters=("release_id", "target_path", "scope", "agent_instance_id", "target_root_id"))
_add("rollback_release", "release_rollback", "release", "mutation", "gui_release_command", parameters=("release_id", "confirmed", "target_path", "scope", "agent_instance_id", "target_root_id"))
_add("rollback_native_memory_release", "release_rollback", "release", "mutation", "gui_release_command", parameters=("release_id", "force", "confirmed", "scope", "agent_instance_id", "target_root_id"))

# Agent discovery / lifecycle.  Target identifiers are business selectors, not
# authorization claims; caller authority remains NativeBoundContext.
_add("discover_agents", "discover_agents", "agent", "read", "gui_agent_query")
_add("get_selection_tree", "get_selection_tree", "agent", "read", "gui_agent_query", parameters=("instance_id",))
_add("get_agent_data", "get_agent_data", "agent", "read", "gui_agent_query", parameters=("instance_id",))
_add("list_agent_candidates", "list_agent_candidates", "agent", "read", "gui_agent_query", parameters=("include_uninstalled", "include_stale", "include_unknown"))
_add("list_archived_agents", "list_archived_agents", "agent", "read", "gui_agent_query")
_add("list_cleanup_history", "list_cleanup_history", "agent", "read", "gui_agent_query")
_add("list_agents", "list_agents", "agent", "read", "gui_agent_query")
_add("get_residual_cleanup", "get_residual_cleanup", "agent", "read", "gui_agent_query", parameters=("instance_id", "candidate_id"))
_add("open_agent_folder", "open_agent_folder", "agent", "read", "gui_agent_query", parameters=("dir_path", "candidate_id"))
_add("mark_agent_uninstalled", "mark_agent_uninstalled", "agent", "mutation", "gui_agent_command", parameters=("product", "dir_path", "reason", "candidate_id"))
_add("unmark_agent_uninstalled", "unmark_agent_uninstalled", "agent", "mutation", "gui_agent_command", parameters=("product", "candidate_id"))
_add("archive_agent_dir", "archive_agent_dir", "agent", "mutation", "gui_agent_command", parameters=("product", "dir_path", "reason", "candidate_id", "dry_run", "allowed_data_paths"))
_add("restore_archived_agent", "restore_archived_agent", "agent", "mutation", "gui_agent_command", parameters=("archive_id",))
_add("delete_archived_agent", "delete_archived_agent", "agent", "mutation", "gui_agent_command", parameters=("archive_id",))

# Binding / groups / scope.  GUI list_bindings is Agent/group membership, not
# the Rules-domain binding list used by the MCP rule surface.
_add("list_bindings", "agent_binding_list", "binding", "read", "gui_group_query", parameters=("include_inactive",))
_add("list_share_groups", "group_list", "binding", "read", "gui_group_query")
_add("check_binding_drift", "binding_drift", "binding", "read", "gui_group_query", parameters=("binding_id",))
_add("get_shared_group_preview", "group_preview", "binding", "read", "gui_group_query", parameters=("target_group_id",))
_add("get_governance_scope", "scope_get", "binding", "read", "gui_group_query")
_add("get_governance_scope_state", "scope_get", "binding", "read", "gui_group_query")
_add("set_governance_scope", "scope_set", "binding", "mutation", "gui_group_command", parameters=("requested_scope",))
_add("commit_selection", "commit_selection", "binding", "mutation", "gui_group_command", parameters=("instance_id", "selected", "confirmed"))
_add("bind_agent", "bind_agent", "binding", "mutation", "gui_group_command", parameters=("target_agent_id", "target_group_id", "mcp_server_name", "native_memory_mode", "redirect_paths"))
_add("bind_agents_to_shared_group", "bind_agents_to_shared_group", "binding", "mutation", "gui_group_command", parameters=("target_agent_ids", "target_group_id", "mcp_server_name", "native_memory_modes", "redirect_paths", "allow_empty_group_creation"))
_add("unbind_agent", "unbind_agent", "binding", "mutation", "gui_group_command", parameters=("binding_id",))
_add("ensure_personal_memory_group", "ensure_personal_memory_group", "binding", "mutation", "gui_group_command", parameters=("target_agent_id", "confirmed"))
_add("leave_shared_group_to_personal", "leave_shared_group_to_personal", "binding", "mutation", "gui_group_command", parameters=("target_agent_id", "confirmed"))
_add("dissolve_shared_group", "dissolve_shared_group", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "confirmed", "archive_data"))
_add("export_memory_group", "export_memory_group", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "confirmed"))
_add("clear_memory_group", "clear_memory_group", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "confirmed"))
_add("archive_memory_group", "archive_memory_group", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "confirmed"))
_add("install_shared_group_mcp_redirects", "install_shared_group_mcp_redirects", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "confirmed"))
_add("import_native_memories_to_group", "import_native_memories_to_group", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "target_agent_ids", "confirmed"))
_add("commit_shared_memory_governance", "commit_shared_memory_governance", "binding", "mutation", "gui_group_command", parameters=("target_group_id", "reason", "confirmed"))
_add("enter_multi_agent_mode", "enter_multi_agent_mode", "binding", "mutation", "gui_group_command")
_add("exit_multi_agent_mode", "exit_multi_agent_mode", "binding", "mutation", "gui_group_command")

# History.
_add("list_history_sessions", "history_list_sessions", "history", "read", "history_list_sessions", parameters=("scope", "limit", "offset", "extracted", "date_from", "date_to"))
_add("search_history", "history_search", "history", "read", "history_search", parameters=("query", "scope", "limit", "offset"))
_add("history_timeline", "history_timeline", "history", "read", "history_timeline", parameters=("session_id", "anchor_turn_id", "scope", "radius"))
_add("history_read", "history_read", "history", "read", "history_read", parameters=("session_id", "turn_id", "scope", "limit", "offset"))
_add("history_extract_preview", "history_extract_preview", "history", "read", "history_extract_preview", parameters=("session_id", "turn_ids", "scope", "limit"))
_add("export_history", "history_export", "history", "read", "history_export", parameters=("session_ids", "scope"))
_add("delete_history", "history_delete", "history", "mutation", "history_delete", parameters=("session_ids", "scope", "invalidate_evidence", "confirmed", "mutation_receipt", "idempotency_key"))
_add("list_history", "history_list_sessions", "history", "read", "history_list_sessions")
_add("discover_local_history_sources", "history_source_discover", "history", "read", "gui_history_control")
_add("backfill_local_history", "history_backfill", "history", "mutation", "gui_history_control", execution="task", parameters=("continuation",), cancel_operation="task_cancel")

# Enrichment.
_same(("list_pending_enrichments",), "enrichment", "read", "list_pending_enrichments")
_same(("get_enrichment_status",), "enrichment", "read", "enrichment_status")
_same(("get_host_enrichment_guide",), "enrichment", "read", "host_enrichment_guide")
_same(("list_host_llm_agents",), "enrichment", "read", "host_llm_agents")
_add("apply_enrichments", "enrichment_apply", "enrichment", "mutation", "apply_enrichments", parameters=("results",))

# Maintenance.
_add("plan_memoryguard_gc", "maintenance_plan", "maintenance", "read", "gui_maintenance_control", parameters=("older_than_days", "keep_releases", "keep_snapshots"))
_add("apply_memoryguard_gc", "maintenance_apply", "maintenance", "mutation", "gui_maintenance_control", execution="task", parameters=("confirmed", "older_than_days", "keep_releases", "keep_snapshots"), cancel_operation="task_cancel")

# Usage telemetry.
_add("get_usage_telemetry", "usage_telemetry", "usage", "read", "gui_usage_query", parameters=("window_days", "agent_key"))
_add("sync_usage_telemetry", "usage_telemetry_sync", "usage", "mutation", "gui_usage_sync")

# Hook / host modes.
_add("get_host_hook_status", "host_hook_status", "host", "read", "hook_status", parameters=("target_provider", "target_agent_id"))
_add("set_host_hook_mode", "host_hook_mode_set", "host", "mutation", "gui_host_control", parameters=("target_provider", "target_agent_id", "mode", "confirmed"))
_add("uninstall_host_hook", "host_hook_uninstall", "host", "mutation", "gui_host_control", parameters=("target_provider", "confirmed"))

# RequestQueue compatibility now maps to Runtime TaskRun, never a second queue.
_add("submit_request", "request_mutation", "runtime", "mutation", "gui_request_compat", parameters=("method", "args"))
_add("get_request_status", "task_status", "runtime", "read", "gui_task_status", parameters=("run_id",))
_add("list_pending_requests", "task_list", "runtime", "read", "gui_task_list")

GUI_OPERATION_SPECS: Mapping[str, GuiOperationSpec] = dict(sorted(_GUI_OPERATIONS.items()))
GUI_METHOD_NAMES = frozenset(GUI_OPERATION_SPECS)
GUI_MUTATION_NAMES = frozenset(name for name, spec in GUI_OPERATION_SPECS.items() if spec.mutation)
# Registry metadata is source-of-truth: an operation becomes brokerable only
# when its constructed spec carries reviewed headless exposure above.
MCP_BROKER_GUI_METHOD_NAMES = frozenset(
    name for name, spec in GUI_OPERATION_SPECS.items()
    if spec.mcp_broker == "headless_broker"
)
MCP_BROKER_GUI_EXCLUDED = frozenset(
    name for name, spec in GUI_OPERATION_SPECS.items()
    if spec.mcp_broker != "headless_broker"
)
MCP_BROKER_GUI_ADMIN_NAMES = frozenset({
    "edit_memory", "lock_memory", "unlock_memory", "set_memory_injection_policy",
    "restore_memory", "delete_memory", "rollback_memory", "add_source",
    "remove_source", "apply_plan", "undo_change", "resolve_conflict",
    "close_stale_conflict", "release_quarantine", "delete_quarantine",
    "neuron_decide", "mark_agent_uninstalled", "unmark_agent_uninstalled",
    "archive_agent_dir", "restore_archived_agent", "delete_archived_agent",
    "bind_agent", "bind_agents_to_shared_group", "unbind_agent",
    "ensure_personal_memory_group", "leave_shared_group_to_personal",
    "dissolve_shared_group", "export_memory_group", "clear_memory_group",
    "archive_memory_group", "install_shared_group_mcp_redirects",
    "import_native_memories_to_group", "commit_shared_memory_governance",
    "enter_multi_agent_mode", "exit_multi_agent_mode", "update_rule_audience", "update_rule_body",
    "set_host_hook_mode", "uninstall_host_hook",
})
MCP_BROKER_GUI_HOST_BOUND_NAMES = frozenset({
    "get_host_hook_status", "get_host_enrichment_guide", "list_host_llm_agents",
    "sync_usage_telemetry",
})
SAFE_BRIDGE_METHOD_NAMES = frozenset({
    "call_readonly", "request_mutation", "get_api_method_registry", "get_sandbox_status", "pick_path",
})


def get_gui_operation_spec(name: str) -> GuiOperationSpec | None:
    return GUI_OPERATION_SPECS.get(str(name or ""))


def gui_registry_payload() -> dict[str, dict[str, object]]:
    return {name: spec.to_dict() for name, spec in GUI_OPERATION_SPECS.items()}


CLI_COMMAND_NAMES = frozenset({
    "audit", "open", "explain", "plan", "apply", "verify", "undo", "source", "scan",
    "import", "provider", "gc", "storage", "gui", "desktop", "hooks", "mcp-status", "doctor", "groups",
    "runtime",
})

RULE_MUTATION_MCP_NAMES = frozenset({
    "memoryguard_rule_feedback", "memoryguard_rule_create_auto", "memoryguard_rule_undo",
    "memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_rule_merge_safe",
    "memoryguard_binding_create",
})
MCP_MUTATION_NAMES = frozenset({
    "memoryguard_list_sources", "memoryguard_scan_summary",
    "memoryguard_extract_memories", "memoryguard_build_and_enrich",
    "memoryguard_memory_write", "memoryguard_memory_update", "memoryguard_memory_delete",
    "memoryguard_memory_merge_safe",
    "memoryguard_binding_create", "memoryguard_external_mcp_import", "memoryguard_accept_candidates",
    "memoryguard_provider_install", "memoryguard_apply_enrichments", "memoryguard_history_delete",
    "memoryguard_codegraph_update",
    "memoryguard_knowledge_add", "memoryguard_knowledge_reingest",
    "memoryguard_knowledge_rebuild_smart", "memoryguard_knowledge_remove",
    "memoryguard_knowledge_restore", "memoryguard_knowledge_purge_deleted",
    "memoryguard_knowledge_update_settings", "memoryguard_knowledge_candidate_review",
    "memoryguard_task_cancel", "memoryguard_codegraph_build_bound",
    *RULE_MUTATION_MCP_NAMES,
})
MUTATING_MCP_TOOL_NAMES = MCP_MUTATION_NAMES
RULE_MUTATION_GUI_NAMES = frozenset(
    name for name, spec in GUI_OPERATION_SPECS.items()
    if spec.domain == "rules" and spec.mutation
) | frozenset({"neuron_decide"})

# Stable aliases used by native coverage and host code.
MUTATING_GUI_METHOD_NAMES = GUI_MUTATION_NAMES
GUI_MUTATION_METHOD_NAMES = GUI_MUTATION_NAMES


__all__ = [
    "GuiOperationSpec", "GUI_OPERATION_SPECS", "get_gui_operation_spec", "gui_registry_payload",
    "MCP_TOOL_NAMES", "MCP_DEFAULT_PUBLIC_TOOL_NAMES", "MCP_GUI_BRIDGE_OPERATIONS",
    "MCP_BROKER_GUI_METHOD_NAMES", "MCP_BROKER_GUI_EXCLUDED",
    "MCP_BROKER_GUI_ADMIN_NAMES", "MCP_BROKER_GUI_HOST_BOUND_NAMES",
    "resolve_mcp_broker_invocation", "SAFE_BRIDGE_METHOD_NAMES",
    "GUI_METHOD_NAMES", "CLI_COMMAND_NAMES",
    "RULE_MUTATION_MCP_NAMES", "MCP_MUTATION_NAMES",
    "MUTATING_MCP_TOOL_NAMES", "RULE_MUTATION_GUI_NAMES",
    "GUI_MUTATION_NAMES", "MUTATING_GUI_METHOD_NAMES", "GUI_MUTATION_METHOD_NAMES",
]
