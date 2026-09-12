"""Pure MCP tool metadata and capability catalog.

This module has no server, native runtime, storage, or transport imports.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from .cutover_v2.surfaces import (
    GUI_OPERATION_SPECS,
    MCP_BROKER_GUI_ADMIN_NAMES,
    MCP_BROKER_GUI_HOST_BOUND_NAMES,
    MCP_DEFAULT_PUBLIC_TOOL_NAMES,
    MCP_GUI_BRIDGE_OPERATIONS,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
)

_FULL_TOOLS = [
    {
        "name": "memoryguard_audit",
        "description": (
            "Use when checking local V2 reference integrity before repair or release. "
            "Do not use to read a memory record, modify data, or assess general Agent quality."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_explain",
        "description": (
            "Use when a memoryguard_audit finding_id needs its evidence, impact, and suggested repair. "
            "Do not use for generic memory lookup or to apply a repair."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string", "description": "finding id from audit"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "memoryguard_list_sources",
        "description": "List authorized sources (project directory, selected folders, Obsidian vaults). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_scan_summary",
        "description": "Run a read-only scan and return snapshot + coverage ledger. Proves scan completeness (unaccounted_count must be 0).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_neuron_graph",
        "description": "Read the scoped neuron graph projection (read-only). Requires explicit agent_instance_id or share_group_id. Returns {empty: true, reason: 'not_built'|'missing_governance_scope'|...}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "mode": {"type": "string", "description": "native | reconstructed (default: reconstructed)"},
                "agent_instance_id": {"type": "string", "description": "single-agent governance scope"},
                "share_group_id": {"type": "string", "description": "MCP shared-memory scope (mutually exclusive with agent)"},
            },
        },
    },
    {
        "name": "memoryguard_codegraph_graph",
        "description": (
            "Read one bounded scoped CodeGraph overview: symbol metadata, "
            "edges, and source-file references. Source bodies are never returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "codegraph_project_ref": {"type": "string", "description": "trusted CodeGraph project selector"},
                "codegraph_source_id": {"type": "string", "description": "trusted CodeGraph source selector"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "maximum graph nodes (default 100)"},
                "provenance": {"type": "string", "enum": ["production", "test", "fixture", "generated", "vendor", "unknown"], "description": "optional source provenance filter"},
            },
        },
    },
    {
        "name": "memoryguard_codegraph_query",
        "description": "Query scoped CodeGraph symbol metadata. Source bodies are never returned.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "codegraph_source_id": {"type": "string", "description": "bound source selector required when caller has no project_ref"},
                "query": {"type": "string"},
                "provenance": {"type": "string", "enum": ["production", "test", "fixture", "generated", "vendor", "unknown"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memoryguard_codegraph_path",
        "description": "Find one bounded directed path between two scoped CodeGraph symbols.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "codegraph_source_id": {"type": "string", "description": "bound source selector required when caller has no project_ref"},
                "start_id": {"type": "string"},
                "end_id": {"type": "string"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 32},
                "relation": {"type": "string"},
                "provenance": {"type": "string", "enum": ["production", "test", "fixture", "generated", "vendor", "unknown"]},
            },
            "required": ["start_id", "end_id"],
        },
    },
    {
        "name": "memoryguard_codegraph_explain",
        "description": "Explain one scoped CodeGraph symbol with metadata-only source map and bounded edges.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "codegraph_source_id": {"type": "string", "description": "bound source selector required when caller has no project_ref"},
                "symbol_id": {"type": "string"},
                "provenance": {"type": "string", "enum": ["production", "test", "fixture", "generated", "vendor", "unknown"]},
                "edge_limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["symbol_id"],
        },
    },
    {
        "name": "memoryguard_codegraph_affected",
        "description": "Return bounded reverse-impact metadata for one scoped CodeGraph symbol.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "codegraph_source_id": {"type": "string", "description": "bound source selector required when caller has no project_ref"},
                "start_id": {"type": "string"},
                "depth": {"type": "integer", "minimum": 0, "maximum": 32},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
                "relation": {"type": "string"},
                "provenance": {"type": "string", "enum": ["production", "test", "fixture", "generated", "vendor", "unknown"]},
            },
            "required": ["start_id"],
        },
    },
    {
        "name": "memoryguard_codegraph_update",
        "description": "Project a trusted MemoryGuard Graphify Core metadata export into scoped CodeGraph storage. Source bodies are rejected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "codegraph_source_id": {"type": "string", "description": "optional bound source selector for an exact projectless CodeGraph scope"},
                "export": {"type": "object"},
                "full_snapshot": {"type": "boolean"},
                "confirmed": {"type": "boolean"},
            },
            "required": ["export", "confirmed"],
        },
    },
    {
        "name": "memoryguard_codegraph_status",
        "description": "Report scoped CodeGraph counts and Graphify metadata-export capability without claiming production readiness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "codegraph_source_id": {"type": "string", "description": "bound source selector required when caller has no project_ref"},
            },
        },
    },
    {
        "name": "memoryguard_import_preview",
        "description": "Preview an offline import bundle (ChatGPT/Claude/Gemini/Generic). Read-only detection + inventory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "bundle path (file or dir)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["path"],
        },
    },
    # --- v3.2 memory backend tools ---
    {
        "name": "memoryguard_memory_read",
        "description": (
            "Use when an exact memory_id is already known and its governed record is needed. "
            "Do not use for discovery; use memoryguard_memory_search instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "memory record ID"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memoryguard_memory_search",
        "description": (
            "Use when finding governed memories by text and lifecycle status. "
            "Do not use when an exact memory_id is known; use memoryguard_memory_read instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query"},
                "status": {"type": "string", "enum": ["active", "low_confidence", "shadowed", "conflicted", "quarantined", "deleted"], "description": "lifecycle status filter; defaults to active"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "maximum results to return (default: 5 for conversation recall)"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
        },
    },
    {
        "name": "memoryguard_memory_write",
        "description": (
            "Use when user explicitly asks to retain a durable fact, preference, project decision, or procedure. "
            "Do not use for raw transcripts or temporary task notes. Writes locally and may organize duplicates or conflicts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "memory content"},
                "kind": {"type": "string", "enum": ["preference", "fact", "project", "procedure", "episode", "correction"], "description": "optional kind override; omit for native classification"},
                "injection_policy": {"type": "string", "enum": ["relevant", "always"], "default": "relevant", "description": "relevant participates in task recall; always is a mandatory rule"},
                "priority": {"type": "integer", "minimum": -100, "maximum": 100, "default": 0, "description": "stable ordering within the mandatory rule package"},
                "audience": {"type": "array", "description": "mandatory-rule assignments; omitted always defaults to the trusted current agent", "items": {"type": "object"}},
                "write_policy": {"type": "string", "description": "optional write policy; propose_only creates a low_confidence candidate, while omission uses automatic organization"},
                "metadata": {"type": "object", "description": "optional metadata from agent"},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to content, metadata, kind and policy"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["body"],
        },
    },
    {
        "name": "memoryguard_memory_update",
        "description": (
            "Use when owner must correct body, kind, recall policy, or priority of one known memory. "
            "Do not use to create a record, change lifecycle status, or modify another owner's memory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "memory record ID"},
                "atom_id": {"type": "string", "description": "V2 atom ID; use the source-mapping target when a migrated logical ID is ambiguous"},
                "body": {"type": "string", "description": "new body"},
                "kind": {"type": "string", "enum": ["preference", "fact", "project", "procedure", "episode", "correction"], "description": "replacement kind; omit to preserve current kind"},
                "injection_policy": {"type": "string", "enum": ["relevant", "always"], "description": "new injection policy"},
                "priority": {"type": "integer", "minimum": -100, "maximum": 100, "description": "new priority"},
                "audience": {"type": "array", "description": "replace mandatory-rule assignments; only allowed for always records", "items": {"type": "object"}},
                "idempotency_key": {"type": "string", "description": "optional retry key bound to this target and payload"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memoryguard_memory_delete",
        "description": (
            "Use when owner must remove one known memory from future recall. "
            "Do not use for irreversible erasure: this is a local soft-delete recorded as status=deleted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "memory record ID"},
                "idempotency_key": {"type": "string", "description": "required retry key bound to this target; makes repeated deletion safe"},
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
            "required": ["memory_id", "idempotency_key"],
        },
    },
    {
        "name": "memoryguard_memory_status",
        "description": (
            "Use when checking shared-memory availability, bound scope, total and active records, "
            "lifecycle and kind counts, and evidence-link count. "
            "Do not use to search or read individual memory content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_instance_id": {"type": "string", "description": "optional identity consistency check; trusted MCP environment is authoritative"},
            },
        },
    },
    {
        "name": "memoryguard_memory_merge_safe_preview",
        "description": (
            "Read-only preflight for memoryguard_memory_merge_safe. Resolves one "
            "explicit canonical/duplicate atom pair in the trusted share group, "
            "returns current atom revisions, policies, priorities, and relation "
            "safety, and does not write transactions, decisions, undo, or "
            "idempotency records. There is no force or bypass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canonical_memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active canonical memory_id in the trusted share group",
                },
                "canonical_atom_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active canonical atom_id in the trusted share group",
                },
                "duplicate_memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active duplicate memory_id in the same share group",
                },
                "duplicate_atom_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active duplicate atom_id in the same share group",
                },
                "workspace": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_memory_merge_safe",
        "description": (
            "Admin-only governed supersede of one active same-group duplicate "
            "memory atom into a stronger canonical atom. Reuses GovernanceV2."
            "supersede. Requires confirmed=true. There is no force or bypass; "
            "owner update/delete stay unchanged."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canonical_memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active canonical memory_id in the trusted share group",
                },
                "canonical_atom_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active canonical atom_id in the trusted share group",
                },
                "duplicate_memory_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active duplicate memory_id in the same share group",
                },
                "duplicate_atom_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active duplicate atom_id in the same share group",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "must be true; the mutation refuses any other value",
                },
                "expected_atom_revisions": {
                    "type": "object",
                    "description": "CAS map of involved atom or memory ids to current revisions",
                },
                "mutation_receipt": {
                    "type": "object",
                    "description": "native mutation receipt for this supersede transaction",
                    "properties": {
                        "receipt_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "id": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "workspace": {"type": "string"},
            },
            "required": [
                "confirmed",
                "expected_atom_revisions",
                "mutation_receipt",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_context_bootstrap",
        "description": (
            "Use when starting one new task to build bounded mandatory rules and relevant governed memory context. "
            "Do not use for exact record lookup or repeatedly within same task. Uses trusted binding and may mark one pending local CodeGraph receipt consumed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": "current task or request; required",
                },
                "project_hint": {
                    "type": "string",
                    "description": "optional project/repository hint used only for relevance",
                },
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 12,
                    "description": "maximum optional memories to include; mandatory rules use their separate budget",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 12000,
                    "default": 6000,
                    "description": "maximum characters for optional recalled content; mandatory rules use their separate budget",
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12000,
                    "description": "optional total-token budget forwarded to the V2 ContextEngine",
                },
                "read_path": {
                    "type": "string",
                    "enum": ["auto", "rule-intelligence"],
                    "default": "auto",
                    "description": "Phase5 canonical read path: auto uses "
                    "canonical only when the group is canonically ready, "
                    "otherwise the native compatibility read path; "
                    "rule-intelligence prefers the rule-intelligence layer, "
                    "deduplicating merged duplicates only after the "
                    "active/audience/exclude match",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_feedback",
        "description": (
            "Record explicit evidence for a mandatory-rule bootstrap match. "
            "This closes the loop for follow/violate/not_applicable/corrected decisions. "
            "One feedback is bound to one receipt_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "trusted identity check"},
                "receipt_id": {
                    "type": "string",
                    "description": "receipt_id returned by memoryguard_context_bootstrap",
                },
                "outcome": {
                    "type": "string",
                    "enum": [
                        "followed",
                        "violated",
                        "not_applicable",
                        "corrected",
                        "exception",
                        "ignored",
                    ],
                    "description": "observed outcome after bootstrap packet is shown",
                },
                "actor": {
                    "type": "string",
                    "description": (
                        "deprecated display actor id; source/authority are fixed by MCP "
                        "transport and never inferred from this value"
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "optional evidence/notes",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "confidence score 0-1",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "optional retry key bound to content and actor",
                },
            },
            # The caller cannot select the producer.  Older clients may still
            # send a display actor; when omitted the handler derives one from
            # the trusted transport identity.
            "required": ["receipt_id", "outcome"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_create_auto",
        "description": (
            "Create one mandatory rule from text. Automatic scope inference is fail-closed: "
            "only the trusted current agent or that agent plus the trusted project cwd are allowed. "
            "Broader scope requires explicit manual=true, an explicit scope object, and admin capability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "description": "rule text"},
                "kind": {"type": "string", "description": "optional preference|fact|project|procedure|episode|correction"},
                "priority": {"type": "integer", "minimum": -100, "maximum": 100, "default": 0},
                "scope": {"type": "object", "description": "optional explicit audience assignment; auto mode still rejects broad targets"},
                "manual": {"type": "boolean", "default": False, "description": "explicit human/admin declaration for broad scope"},
                "idempotency_key": {"type": "string"},
                "workspace": {"type": "string", "description": "workspace path (default: configured MemoryGuard workspace)"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_decision_read",
        "description": "Read one explainable rule lifecycle decision by decision_id. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string"},
                "workspace": {"type": "string"},
            },
            "required": ["decision_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_undo",
        "description": "Undo a V2 rule lifecycle mutation (including feedback/evidence compensation) using its persisted pre-rule undo_id. Requires the trusted actor or admin capability.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "undo_id": {"type": "string"},
                "decision_id": {"type": "string", "description": "optional decision id alias; resolved to its undo_id"},
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "stable retry key for the compensating V2 mutation",
                },
                "workspace": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_scope_stats",
        "description": "Read rule audience statistics and the automatic scope policy. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"workspace": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_capability_issue",
        "description": (
            "Issue one opaque, single-use rule-merge capability for a candidate "
            "proposal. Requires the trusted admin AccessContext. The raw token "
            "is returned once to the caller; persistent storage keeps only its hash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "ttl_seconds": {"type": "number", "exclusiveMinimum": 0, "default": 300},
                "mutation_receipt": {
                    "type": "object",
                    "description": "native mutation receipt; only its bounded receipt id participates in the request proof",
                    "properties": {
                        "receipt_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "id": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "recovery_secret": {
                    "type": "string",
                    "minLength": 43,
                    "pattern": "^[A-Za-z0-9_-]+$",
                    "description": "one-time base64url recovery secret; never persisted or returned by MCP",
                },
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "mutation_receipt", "idempotency_key", "recovery_secret"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_approve",
        "description": (
            "Approve one candidate rule-merge proposal with a server-issued "
            "single-use capability and trusted admin AccessContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "capability_token": {"type": "string"},
                "expected_definition_revisions": {"type": "object"},
                "mutation_receipt": {
                    "type": "object",
                    "description": "native mutation receipt for this approval transaction",
                    "properties": {
                        "receipt_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "id": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "capability_token", "expected_definition_revisions", "mutation_receipt", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_acknowledge",
        "description": (
            "Acknowledge first-merge risk with a server-issued single-use "
            "capability and trusted admin AccessContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "capability_token": {"type": "string"},
                "mutation_receipt": {
                    "type": "object",
                    "description": "native mutation receipt for this acknowledgement transaction",
                    "properties": {
                        "receipt_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "id": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "capability_token", "mutation_receipt", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_cooldown_clear",
        "description": (
            "Clear one rule-merge proposal cooldown with a server-issued "
            "single-use capability and trusted admin AccessContext."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal_id": {"type": "string"},
                "capability_token": {"type": "string"},
                "mutation_receipt": {
                    "type": "object",
                    "description": "native mutation receipt for this cooldown transaction",
                    "properties": {
                        "receipt_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "id": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "workspace": {"type": "string"},
            },
            "required": ["proposal_id", "capability_token", "mutation_receipt", "idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_safe_preview",
        "description": (
            "Read-only preflight for memoryguard_rule_merge_safe. Resolves the "
            "requested canonical/duplicate source or definition ids in the "
            "trusted share group, returns current definition revisions and pair "
            "safety, and does not write transactions, decisions, undo, "
            "settlement, or idempotency records. There is no force or bypass; "
            "composer/pair safety still decides mergeability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canonical_source_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active rule_source_links.memory_id in the trusted share group",
                },
                "canonical_definition_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active definition id in the trusted share group",
                },
                "duplicate_source_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    "description": "active duplicate source ids in the same share group",
                },
                "duplicate_definition_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    "description": "active duplicate definition ids in the same share group",
                },
                "workspace": {"type": "string"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_rule_merge_safe",
        "description": (
            "Admin-only source-aware fold of active same-group duplicate rules "
            "into one canonical definition. Reuses the V2 historical "
            "reconciliation transaction. Requires confirmed=true. There is no "
            "force or bypass; composer/pair safety still decides mergeability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "canonical_source_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active rule_source_links.memory_id in the trusted share group",
                },
                "canonical_definition_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                    "description": "active definition id in the trusted share group",
                },
                "duplicate_source_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    "description": "active duplicate source ids in the same share group",
                },
                "duplicate_definition_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    "description": "active duplicate definition ids in the same share group",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": "must be true; the mutation refuses any other value",
                },
                "expected_definition_revisions": {
                    "type": "object",
                    "description": "CAS map of involved definition or source ids to current revisions",
                },
                "mutation_receipt": {
                    "type": "object",
                    "description": "native mutation receipt for this merge transaction",
                    "properties": {
                        "receipt_id": {"type": "string", "minLength": 1, "maxLength": 256},
                        "id": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                },
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
                "workspace": {"type": "string"},
            },
            "required": [
                "confirmed",
                "expected_definition_revisions",
                "mutation_receipt",
                "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    # --- v3.2 agent binding tools ---
    {
        "name": "memoryguard_binding_create",
        "description": "Bind an agent instance to a share_group. Creates an AgentBinding record (active). Read-only listing is via binding_list; unbind goes through CLI/GUI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_instance_id": {"type": "string", "description": "agent instance ID to bind"},
                "share_group_id": {"type": "string", "description": "share group ID to bind the agent into"},
                "mcp_server_name": {"type": "string", "description": "MCP server name (default: memoryguard)"},
                "native_memory_mode": {"type": "string", "description": "native memory mode: observed|redirected|unsupported (default: observed)"},
                "redirect_paths": {"type": "array", "items": {"type": "string"}, "description": "optional native memory redirect paths"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["agent_instance_id", "share_group_id"],
        },
    },
    {
        "name": "memoryguard_binding_list",
        "description": "List existing AgentBinding records. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_inactive": {"type": "boolean", "description": "include inactive bindings (default: true)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    # --- v3.2 external MCP descriptor tools ---
    {
        "name": "memoryguard_external_mcp_list",
        "description": "List imported external MCP server descriptors and their resources. Descriptor-level only (no live MCP client discovery). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_external_mcp_import",
        "description": "Import a new external MCP descriptor (JSON). Classifies the server (L0-L4), persists it, and returns the detection result. Descriptor-level import only; does not call the live MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "descriptor_json": {"type": "string", "description": "JSON-encoded MCP descriptor {name|display_name, tools[], resources[], memory_entries[]}"},
                "server_id": {"type": "string", "description": "server ID (default: derived from descriptor name/display_name)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["descriptor_json"],
        },
    },
    # --- v3.2 document extraction tool (§8.5 两步流程) ---
    {
        "name": "memoryguard_extract_memories",
        "description": "Extract memory segments from a source file under an authorized source root (read-only preview). Returns candidate list with kind, risk_level, and preview. Does NOT write to shared memory. Use memoryguard_accept_candidates to write accepted candidates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "absolute or workspace-relative path to a source file under an authorized source root"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["source_path"],
        },
    },
    {
        "name": "memoryguard_accept_candidates",
        "description": "Accept extracted memory candidates through GovernanceEngine and write them to shared memory. Records governed automatic writes plus a DecisionEvent (action=accept_extract). Requires extract_id from a prior extract_memories call and explicit candidate_ids list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "extract_id": {"type": "string", "description": "extract_id returned by memoryguard_extract_memories preview"},
                "candidate_ids": {"type": "array", "items": {"type": "string"}, "description": "list of candidate_id values to accept (cannot be empty)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: default)"},
            },
            "required": ["extract_id", "candidate_ids"],
        },
    },
    # --- v3.2 semantic dedup tool ---
    {
        "name": "memoryguard_semantic_check",
        "description": "Check a new text against existing memories for semantic duplicates/conflicts (cross-lingual, paraphrase). Returns similar memories with similarity scores. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "new text to check"},
                "kind": {"type": "string", "description": "optional kind of the new memory, used for conflict detection"},
                "threshold": {"type": "number", "description": "similarity threshold (default: 0.85)"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: default)"},
            },
            "required": ["text"],
        },
    },
    # --- v3.2 provider adapter tool ---
    {
        "name": "memoryguard_provider_install",
        "description": "Install/repair the provider's global MCP, redirect rules, and supported user-level lifecycle Hook (Claude/Codex/Cursor; TRAE reports MCP+rules fallback). Ensures the trusted Agent has a personal binding unless an explicit shared binding already exists. Requires admin capability; idempotent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "description": "provider name: claude|codex|cursor|trae"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "trusted Agent identity (normally from MEMORYGUARD_AGENT_ID)"},
            },
            "required": ["provider"],
        },
    },
    # --- v3.2 agent group resolution tool ---
    {
        "name": "memoryguard_resolve_group",
        "description": "Resolve which share_group_id an agent should write to, based on its AgentBinding. Read-only. Agents should call this before memory_write to know their group.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_instance_id": {"type": "string", "description": "agent instance ID to resolve"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["agent_instance_id"],
        },
    },
    # --- v3.3 host AI enrichment tools ---
    {
        "name": "memoryguard_list_pending_enrichments",
        "description": "List pending memory enrichment tasks. Skill path: after build_and_enrich returns host_action_required, YOU (host agent) must classify+translate each task and call apply_enrichments — do not ask the user to pick a CLI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "limit": {"type": "integer", "description": "max tasks to return (default: 50)"},
                "agent_instance_id": {"type": "string", "description": "filter by agent scope (optional)"},
                "share_group_id": {"type": "string", "description": "filter by share group scope (optional)"},
            },
        },
    },
    {
        "name": "memoryguard_apply_enrichments",
        "description": "Apply host-agent enrichment results to the V2 memory plane. Each result: task_id, kind, title, body, confidence. After YOU enrich pending tasks, call this then memoryguard_build_and_enrich again to refresh the graph.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "results": {
                    "type": "array",
                    "description": "enrichment results to apply",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                            "kind": {"type": "string", "description": "preference|fact|project|procedure|episode|correction"},
                            "title": {"type": "string", "description": "translated/organized title"},
                            "body": {"type": "string", "description": "translated/organized body"},
                            "confidence": {"type": "number", "description": "0.0-1.0"},
                            "rationale": {"type": "string"},
                        },
                        "required": ["task_id", "kind", "title", "body"],
                    },
                },
                "agent_instance_id": {"type": "string", "description": "scope filter (optional)"},
                "share_group_id": {"type": "string", "description": "share group scope (optional)"},
            },
            "required": ["results"],
        },
    },
    {
        "name": "memoryguard_enrichment_status",
        "description": "Check enrichment queue status: pending/applied counts. Primary enrich happens inside build_projection; use this to see residuals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "filter by agent scope (optional)"},
                "share_group_id": {"type": "string", "description": "filter by share group (optional)"},
            },
        },
    },
    # --- v3.3 build projection + auto enrich ---
    {
        "name": "memoryguard_build_and_enrich",
        "description": "Build memory projection. Default enrich_mode=host: YOU are the LLM. If pending_tasks / host_action_required, immediately classify+translate, call apply_enrichments, then call this again. Multi-agent GUI may pass enrich_mode=cli with a chosen Agent CLI. Do not require a separate AI-整理 button.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "agent_instance_id": {"type": "string", "description": "agent instance ID for scoped projection"},
                "mode": {"type": "string", "description": "projection mode: reconstructed (default) or native"},
                "share_group_id": {"type": "string", "description": "share group ID (optional)"},
                "enrich_mode": {"type": "string", "description": "host (default) | cli | auto | heuristic"},
                "llm_agent": {"type": "string", "description": "CLI agent id when enrich_mode=cli (codex|claude|cursor|…)"},
                "llm_cli": {"type": "string", "description": "CLI path when enrich_mode=cli"},
            },
        },
    },
    # --- Req9: governance-degraded read-only diagnostics ---
    {
        "name": "memoryguard_canonical_status",
        "description": (
            "Read-only canonical reconciliation status for a share_group_id: "
            "canonical_ready, failures, checks, read_path. Always allowed, "
            "even when governance is degraded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: resolved binding or default)"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_diagnostics_snapshot",
        "description": (
            "Read-only governance diagnostics snapshot JSON: reconciliation jobs by status, "
            "canonical activation, projection, source links, bindings. Snapshot uses "
            "sqlite3.Connection.backup(); never copies DB/WAL files and accepts no "
            "arbitrary SQL or file paths. Always allowed, even when governance is degraded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: resolved binding or default)"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_projection_status",
        "description": (
            "Read-only projection status (projection_lag / projection_error / scopes) "
            "for a group. Always allowed, even when governance is degraded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
                "share_group_id": {"type": "string", "description": "share group ID (default: resolved binding or default)"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memoryguard_runtime_processes",
        "description": (
            "Read-only runtime process facts: current pid, memoryguard_version, "
            "code_fingerprint, control_workspace, database_paths, runtime lease status. "
            "Always allowed, even when governance is degraded."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "additionalProperties": False,
        },
    },
]

# V2-native history and knowledge surfaces are described locally so importing
# the MCP entrypoint cannot pull retired storage adapters into the process.
_FULL_TOOLS.extend([
    {
        "name": "memoryguard_history_search",
        "description": "Search trusted local history by query. Returns bounded identifiers and summaries, never raw turn bodies.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "required full-text query"},
            "scope": {"type": "object", "description": "optional trusted history scope hint"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        }, "required": ["query"]},
    },
    {
        "name": "memoryguard_history_timeline",
        "description": "Read a bounded metadata-only timeline around one authorized history turn. Does not write long-term memory.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "authorized session identifier"},
            "anchor_turn_id": {"type": "string", "description": "authorized anchor turn identifier"},
            "scope": {"type": "object", "description": "optional trusted history scope hint"},
            "radius": {"type": "integer", "minimum": 0, "maximum": 50, "default": 4},
        }, "required": ["session_id", "anchor_turn_id"]},
    },
    {
        "name": "memoryguard_history_read",
        "description": "Read one explicitly selected authorized history session or turn. Raw content is returned only on this explicit read path.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "read exactly one session; mutually exclusive with turn_id"},
            "turn_id": {"type": "string", "description": "read exactly one turn; mutually exclusive with session_id"},
            "scope": {"type": "object", "description": "optional trusted history scope hint"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 250, "default": 100},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        }},
    },
    {
        "name": "memoryguard_history_extract_preview",
        "description": "Preview possible long-term-memory candidates from one authorized history session. Read-only; never writes memory.",
        "inputSchema": {"type": "object", "properties": {
            "session_id": {"type": "string", "description": "authorized session identifier"},
            "turn_ids": {"type": "array", "items": {"type": "string"}, "description": "optional selected turns"},
            "scope": {"type": "object", "description": "optional trusted history scope hint"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
        }, "required": ["session_id"]},
    },
])

# Broker and bound headless operations intentionally live in the normal MCP
# definition ledger.  The capability catalog below reads this exact data; it
# never maintains a second, hand-written parameter schema.
_FULL_TOOLS.extend([
    {
        "name": "memoryguard_capabilities",
        "description": "Discover registered MCP operations and every GUI operation with its current headless availability. Reads only V2 manifest state and registry metadata; requires no memory binding or source access. Use operation/query plus offset pagination; request include_schema=true only for a selected page.",
        "inputSchema": {"type": "object", "properties": {
            "domain": {"type": "string", "description": "optional capability domain, such as knowledge, codegraph, or runtime"},
            "query": {"type": "string", "description": "optional English or Chinese search text"},
            "operation": {"type": "string", "description": "optional exact MCP or GUI operation name"},
            "include_schema": {"type": "boolean", "default": False, "description": "include MCP JSON Schemas for this page"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "page size"},
            "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "zero-based page offset"},
        }},
    },
    {
        "name": "memoryguard_invoke",
        "description": "Invoke one explicitly registered MCP operation found through memoryguard_capabilities. Never accepts native handler or GUI method names. Mutating targets require confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "operation": {"type": "string", "description": "registered MCP operation name from capability catalog"},
            "arguments": {"type": "object", "description": "arguments for that registered operation"},
            "confirmed": {"type": "boolean", "description": "required for a mutating target"},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256, "description": "required retry key for a mutating target"},
        }, "required": ["operation", "arguments"]},
    },
    {
        "name": "memoryguard_knowledge_add",
        "description": "Start a bound background knowledge import. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "authorized local source path"},
            "title": {"type": "string", "description": "optional book title"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["path", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_reingest",
        "description": "Start a bound background reingest for one knowledge book. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "book_id": {"type": "string", "description": "book identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["book_id", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_rebuild_smart",
        "description": "Start a bound smart rebuild for one knowledge book. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "book_id": {"type": "string", "description": "book identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["book_id", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_remove",
        "description": "Remove one book from bound knowledge scope. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "book_id": {"type": "string", "description": "book identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["book_id", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_restore",
        "description": "Restore one bound knowledge deletion. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "deletion_id": {"type": "string", "description": "deletion receipt identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["deletion_id", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_purge_deleted",
        "description": "Permanently purge one bound knowledge deletion. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "deletion_id": {"type": "string", "description": "deletion receipt identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["deletion_id", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_update_settings",
        "description": "Update settings for one bound knowledge book. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "book_id": {"type": "string", "description": "book identifier"},
            "settings": {"type": "object", "description": "book settings object"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["book_id", "settings", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_knowledge_candidate_review",
        "description": "Review one bound knowledge candidate. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "candidate_id": {"type": "string", "description": "candidate identifier"},
            "decision": {"type": "string", "description": "review decision"},
            "target_group_id": {"type": "string", "description": "optional in-scope target group"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["candidate_id", "decision", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_task_status",
        "description": "Read status for one task in caller's bound scope.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "task run identifier"},
        }, "required": ["run_id"]},
    },
    {
        "name": "memoryguard_task_list",
        "description": "List pending tasks in caller's bound scope.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100, "description": "maximum pending tasks"},
        }},
    },
    {
        "name": "memoryguard_task_cancel",
        "description": "Cancel one task in caller's bound scope. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string", "description": "task run identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["run_id", "confirmed", "idempotency_key"]},
    },
    {
        "name": "memoryguard_codegraph_build_bound",
        "description": "Start CodeGraph build for one already-bound directory source. Uses caller's exact Agent/provider/runtime scope; no group-wide project enumeration. Requires confirmed=true and idempotency_key.",
        "inputSchema": {"type": "object", "properties": {
            "source_id": {"type": "string", "description": "bound directory source identifier"},
            "confirmed": {"type": "boolean", "description": "must be true"},
            "idempotency_key": {"type": "string", "description": "nonempty retry key"},
        }, "required": ["source_id", "confirmed", "idempotency_key"]},
    },
])
_FULL_TOOLS.extend([
    {
        "name": "memoryguard_history_list_sessions",
        "description": "List the trusted Agent's local conversation-history sessions. Read-only; raw text is not returned.",
        "inputSchema": {"type": "object", "properties": {
            "scope": {"type": "object"}, "limit": {"type": "integer"},
            "offset": {"type": "integer"}, "extracted": {"type": "boolean"},
            "date_from": {"type": "string"}, "date_to": {"type": "string"},
        }},
    },
    {
        "name": "memoryguard_history_export",
        "description": "Export explicitly selected sessions owned by the trusted Agent. This is raw-history evidence, not long-term memory.",
        "inputSchema": {"type": "object", "properties": {
            "session_ids": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "object"},
        }, "required": ["session_ids"]},
    },
    {
        "name": "memoryguard_history_delete",
        "description": "Permanently delete explicitly selected raw-history sessions for the trusted Agent. Requires confirmed=true; never deletes long-term memories.",
        "inputSchema": {"type": "object", "properties": {
            "session_ids": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "object"}, "invalidate_evidence": {"type": "boolean"},
            "confirmed": {"type": "boolean"},
        }, "required": ["session_ids", "confirmed"]},
    },
])

_FULL_TOOLS.extend([
    {
        "name": "memoryguard_knowledge_list",
        "description": "List bounded reference-only knowledge occurrences in the trusted V2 knowledge scope. Never returns source bodies or writes data.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "namespace_id": {"type": "string", "description": "must exactly match the trusted knowledge namespace"},
            "sensitivity": {"type": "string", "description": "must exactly match trusted sensitivity"},
            "policy_class": {"type": "string", "description": "must exactly match trusted policy class"},
        }, "required": ["namespace_id", "sensitivity", "policy_class"]},
    },
    {
        "name": "memoryguard_knowledge_search",
        "description": "Search reference-only V2 knowledge occurrences in the trusted scope. Returns references and summaries, never source bodies or writes data.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "required search text"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "namespace_id": {"type": "string", "description": "must exactly match the trusted knowledge namespace"},
            "sensitivity": {"type": "string", "description": "must exactly match trusted sensitivity"},
            "policy_class": {"type": "string", "description": "must exactly match trusted policy class"},
        }, "required": ["query", "namespace_id", "sensitivity", "policy_class"]},
    },
    {
        "name": "memoryguard_knowledge_read",
        "description": "Read one reference-only V2 knowledge occurrence by occurrence_id in the trusted scope. Source body text is never returned and no data is written.",
        "inputSchema": {"type": "object", "properties": {
            "occurrence_id": {"type": "string", "description": "required occurrence identifier from a knowledge result"},
            "namespace_id": {"type": "string", "description": "must exactly match the trusted knowledge namespace"},
            "sensitivity": {"type": "string", "description": "must exactly match trusted sensitivity"},
            "policy_class": {"type": "string", "description": "must exactly match trusted policy class"},
        }, "required": ["occurrence_id", "namespace_id", "sensitivity", "policy_class"]},
    },
    {
        "name": "memoryguard_knowledge_book",
        "description": "Filter reference-only V2 knowledge occurrences by optional book or occurrence identifier. Returns references only; never source bodies or writes data.",
        "inputSchema": {"type": "object", "properties": {
            "book_id": {"type": "string", "description": "optional book or occurrence identifier"},
            "query": {"type": "string", "description": "optional reference filter"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "namespace_id": {"type": "string", "description": "must exactly match the trusted knowledge namespace"},
            "sensitivity": {"type": "string", "description": "must exactly match trusted sensitivity"},
            "policy_class": {"type": "string", "description": "must exactly match trusted policy class"},
        }, "required": ["namespace_id", "sensitivity", "policy_class"]},
    },
    {
        "name": "memoryguard_knowledge_candidates",
        "description": "List reference-only V2 knowledge-review candidates in the trusted scope. Candidate approval remains a governed GUI command; this tool never writes data.",
        "inputSchema": {"type": "object", "properties": {
            "status": {"type": "string", "description": "candidate status (default pending)"},
            "query": {"type": "string", "description": "optional candidate summary/reference filter"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
            "namespace_id": {"type": "string", "description": "must exactly match the trusted knowledge namespace"},
            "sensitivity": {"type": "string", "description": "must exactly match trusted sensitivity"},
            "policy_class": {"type": "string", "description": "must exactly match trusted policy class"},
        }, "required": ["namespace_id", "sensitivity", "policy_class"]},
    },
])

# ``MCP_TOOL_NAMES`` is the one full callable-name ledger shared with V2
# dispatch.  The definitions below remain the source of JSON schema and copy;
# fail at import if either ledger drifts instead of silently exposing a broken
# or undiscoverable operation.
TOOL_DEFINITIONS = {
    str(item["name"]): item
    for item in _FULL_TOOLS
    if isinstance(item, dict) and isinstance(item.get("name"), str)
}
if len(TOOL_DEFINITIONS) != len(_FULL_TOOLS):
    raise RuntimeError("mcp_tool_definition_duplicate")
CALLABLE_TOOL_NAMES = frozenset(TOOL_DEFINITIONS)
if CALLABLE_TOOL_NAMES != MCP_TOOL_NAMES:
    missing_definitions = sorted(MCP_TOOL_NAMES - CALLABLE_TOOL_NAMES)
    extra_definitions = sorted(CALLABLE_TOOL_NAMES - MCP_TOOL_NAMES)
    raise RuntimeError(
        "mcp_tool_registry_drift:"
        f"missing={','.join(missing_definitions)};extra={','.join(extra_definitions)}"
    )
if not set(MCP_DEFAULT_PUBLIC_TOOL_NAMES) <= CALLABLE_TOOL_NAMES:
    raise RuntimeError("mcp_default_tool_not_callable")

# These annotations describe actual public effects, not V2 readiness gates.
# Bootstrap consumes one pending local CodeGraph receipt when present; all
# other read tools below use read-only stores/adapters.
_DEFAULT_TOOL_ANNOTATIONS = {
    "memoryguard_context_bootstrap": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "memoryguard_memory_search": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_memory_read": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_memory_write": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "memoryguard_memory_update": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    "memoryguard_memory_delete": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_memory_status": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_audit": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_explain": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_capabilities": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "memoryguard_invoke": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
}
if set(_DEFAULT_TOOL_ANNOTATIONS) != set(MCP_DEFAULT_PUBLIC_TOOL_NAMES):
    raise RuntimeError("mcp_default_tool_annotation_drift")
for _tool_name, _annotations in _DEFAULT_TOOL_ANNOTATIONS.items():
    TOOL_DEFINITIONS[_tool_name]["annotations"] = _annotations

# Complete catalog supports older direct calls.  ``TOOLS`` alone is the
# current discovery surface returned by tools/list for new MCP clients.
CALLABLE_TOOLS = tuple(_FULL_TOOLS)
TOOLS = [TOOL_DEFINITIONS[name] for name in MCP_DEFAULT_PUBLIC_TOOL_NAMES]


_CAPABILITY_QUERY_ALIASES = {
    "knowledge_add": "知识库导入 知识库添加 knowledge import ingest",
    "knowledge_reingest": "知识库重新导入 reingest",
    "knowledge_rebuild_smart": "知识库智能重建 smart rebuild",
    "knowledge_remove": "知识库删除 remove",
    "knowledge_restore": "知识库恢复 restore",
    "knowledge_purge_deleted": "知识库永久删除 purge",
    "knowledge_update_settings": "知识库设置 settings",
    "knowledge_candidate_review": "知识库候选审核 review",
    "build_codegraph": "构建代码图 codegraph build graphify",
    "memoryguard_codegraph_build_bound": "构建代码图 codegraph build graphify",
    "get_build_progress": "任务进度 task progress",
    "get_request_status": "任务进度 task status",
    "list_pending_requests": "任务列表 task list",
    "cancel_build_projection": "任务取消 cancel task",
    "memoryguard_task_status": "任务进度 task status",
    "memoryguard_task_list": "任务列表 task list",
    "memoryguard_task_cancel": "任务取消 cancel task",
    "get_conflicts": "冲突治理 conflict governance",
    "resolve_conflict": "冲突治理 解决冲突 conflict resolution",
    "close_stale_conflict": "冲突治理 关闭冲突 conflict close",
    "get_governance_snapshot": "冲突治理 governance conflict",
    "memoryguard_memory_merge_safe": "冲突治理 合并 conflict merge",
    "memoryguard_memory_merge_safe_preview": "冲突治理 合并预览 conflict merge preview",
}
_TASK_MCP_OPERATIONS = frozenset({
    "memoryguard_knowledge_add", "memoryguard_knowledge_reingest",
    "memoryguard_knowledge_rebuild_smart", "memoryguard_codegraph_build_bound",
})

# This is deliberately a broker extension, not a twelfth default tool or a
# native handler.  It can only page a response reference issued by this MCP
# process; the server validates its original read again before every page.
_RESPONSE_READ_OPERATION = {
    "name": "memoryguard_response_read",
    "surface": "mcp",
    "domain": "broker",
    "availability": "registered",
    "broker_invocable": True,
    "kind": "read",
    "mutation": False,
    "parameters": ["response_ref", "fields", "offset", "limit"],
    "required": ["response_ref"],
    "confirmation": "none",
    "idempotency": "none",
    "direct_confirmation": "not_callable_directly",
    "direct_idempotency": "not_callable_directly",
    "execution": "sync",
    "task_receipt": "",
    "cancel_operation": "",
    "description": (
        "Read one UTF-8 page from a bounded response reference. Private references "
        "revalidate the original read under the current trusted binding and session; "
        "changed, deleted, sensitive, revoked, or expired results are refused."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "response_ref": {"type": "string", "description": "opaque response reference id"},
            "fields": {
                "type": "array", "items": {"type": "string"},
                "description": "optional business field names or object-only JSON Pointers for one JSON payload",
            },
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 4, "maximum": 4096, "default": 3000},
        },
        "required": ["response_ref"],
        "additionalProperties": False,
    },
}


def _capability_domain(name: str) -> str:
    normalized = name.removeprefix("memoryguard_")
    if normalized == "response_read":
        return "broker"
    for domain in ("knowledge", "codegraph", "memory", "rule", "history", "binding", "projection"):
        if normalized.startswith(domain + "_") or normalized == domain:
            return domain
    if normalized.startswith("task_"):
        return "runtime"
    if normalized in {"capabilities", "invoke"}:
        return "broker"
    return "mcp"


def _catalog_mcp_item(name: str, *, include_schema: bool) -> dict[str, Any]:
    definition = TOOL_DEFINITIONS[name]
    schema = definition.get("inputSchema")
    input_schema = deepcopy(schema) if isinstance(schema, Mapping) else {"type": "object", "properties": {}}
    properties = input_schema.get("properties") if isinstance(input_schema, Mapping) else {}
    required = input_schema.get("required", ()) if isinstance(input_schema, Mapping) else ()
    mutation = name in MCP_MUTATION_NAMES
    item = {
        "name": name,
        "surface": "mcp",
        "domain": _capability_domain(name),
        "availability": "registered",
        "broker_invocable": name != "memoryguard_invoke",
        "kind": "mutation" if mutation else "read",
        "mutation": mutation,
        "parameters": list(properties) if isinstance(properties, Mapping) else [],
        "required": list(required) if isinstance(required, list) else [],
        "confirmation": "broker_required" if mutation else "none",
        "idempotency": "broker_required" if mutation else "none",
        "direct_confirmation": "required" if "confirmed" in set(required or ()) else "handler_defined",
        "direct_idempotency": "required" if "idempotency_key" in set(required or ()) else "handler_defined",
        "execution": "task" if name in _TASK_MCP_OPERATIONS else "sync",
        "task_receipt": "job_id" if name in _TASK_MCP_OPERATIONS else "",
        "cancel_operation": "memoryguard_task_cancel" if name in _TASK_MCP_OPERATIONS else "",
        "description": str(definition.get("description") or ""),
    }
    if include_schema:
        item["input_schema"] = input_schema
    return item


def _catalog_gui_item(name: str, operation: Any) -> dict[str, Any]:
    bridge = next((mcp for mcp, gui in MCP_GUI_BRIDGE_OPERATIONS.items() if gui == name), name)
    availability = str(operation.mcp_broker)
    if name == "build_codegraph":
        bridge = "memoryguard_codegraph_build_bound"
        conditions = ["bound directory source", "MCP caller scope"]
    elif name == "list_codegraph_projects":
        bridge = ""
        conditions = ["server-admin GUI capability", "entrypoint=gui"]
    elif availability == "headless_broker":
        conditions = ["bound MCP context"]
        if name in MCP_BROKER_GUI_ADMIN_NAMES:
            conditions.append("native admin capability")
        if name in MCP_BROKER_GUI_HOST_BOUND_NAMES:
            conditions.append("host integration available")
    elif availability == "desktop_only":
        conditions = ["desktop GUI host"]
    elif availability == "bridge_protocol_only":
        conditions = ["SafeBridge desktop protocol"]
    else:
        conditions = ["not reviewed for MCP broker"]
    return {
        "name": name,
        "surface": "gui",
        "domain": str(operation.domain),
        "availability": availability,
        "broker_operation": bridge,
        "conditions": conditions,
        "parameters": list(operation.parameters),
        "confirmation": str(operation.confirmation),
        "idempotency": str(operation.idempotency),
        "execution": str(operation.execution),
        "task_receipt": "job_id" if operation.execution == "task" else "",
        "cancel_operation": str(operation.cancel_operation),
        "canonical_name": str(operation.canonical_name),
        "native_handler": str(operation.native_handler),
    }


def mcp_capability_catalog(args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return registry metadata only; no binding, store, or source access."""

    values = args if isinstance(args, Mapping) else {}
    domain = str(values.get("domain") or "").strip().casefold()
    query = str(values.get("query") or "").strip().casefold()
    operation_name = str(values.get("operation") or "").strip()
    offset = values.get("offset", 0)
    limit = values.get("limit", 20)
    include_schema = values.get("include_schema", False)
    if type(offset) is not int or type(limit) is not int or type(include_schema) is not bool:
        raise ValueError("capability_pagination_invalid")
    if offset < 0 or limit < 1 or limit > 100:
        raise ValueError("capability_pagination_invalid")
    entries: list[dict[str, Any]] = [
        _catalog_mcp_item(name, include_schema=include_schema) for name in sorted(TOOL_DEFINITIONS)
    ]
    response_read = deepcopy(_RESPONSE_READ_OPERATION)
    if not include_schema:
        response_read.pop("input_schema", None)
    entries.append(response_read)
    entries.extend(
        _catalog_gui_item(name, operation)
        for name, operation in sorted(GUI_OPERATION_SPECS.items())
    )
    if domain:
        entries = [item for item in entries if str(item.get("domain") or "").casefold() == domain]
    if operation_name:
        entries = [item for item in entries if item.get("name") == operation_name]
    if query:
        chunks = [
            part.casefold() for part in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", query)
        ]
        terms = [query, *chunks]
        def matches(item: Mapping[str, Any]) -> bool:
            text = " ".join(str(value) for value in (
                item.get("name"), item.get("domain"), item.get("canonical_name"),
                item.get("description"), _CAPABILITY_QUERY_ALIASES.get(str(item.get("name") or ""), ""),
            )).casefold()
            return any(term and term in text for term in terms)
        entries = [item for item in entries if matches(item)]
    total = len(entries)
    page = entries[offset:offset + limit]
    next_offset = offset + len(page)
    return {
        "items": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset if next_offset < total else None,
    }


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------




__all__ = ['_FULL_TOOLS', 'TOOL_DEFINITIONS', 'CALLABLE_TOOL_NAMES', 'CALLABLE_TOOLS', 'TOOLS', 'mcp_capability_catalog']
