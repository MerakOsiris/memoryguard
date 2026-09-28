"""Validate the final mandatory package before committing a publication.

Use the same audience, canonical-head and ContextEngine rules as bootstrap.
The Cartesian representatives cover every equality-based audience partition,
including a future/other agent or project.  Checking only the writer would
miss a shared rule overflowing another member's private package.
"""
from __future__ import annotations

from itertools import product
from typing import Any, Mapping

from ..memory import MemoryAtomStore, MemoryReadScope
from ..rule_scope import canonical_project_ref
from .context_engine import ContextEngine


class MandatoryPublicationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def snapshot(port: Any, context: Mapping[str, Any]) -> list[Any]:
    return port._domain_store("memory", write=True)._list_atoms_unscoped(
        share_group_id=str(context["share_group_id"]), include_building=True,
    )


def _always(atom: Any) -> bool:
    return atom.status == "active" and atom.injection_policy == "always"


def validate(port: Any, context: Mapping[str, Any], before: list[Any], *, rules_store: Any = None) -> dict[str, Any]:
    after = snapshot(port, context)
    old = {atom.atom_id: atom for atom in before}
    new = {atom.atom_id: atom for atom in after}
    changed = [
        atom for rows, other in ((old, new), (new, old))
        for key, atom in rows.items()
        if _always(atom) and (key not in other or atom.to_dict() != other[key].to_dict())
    ]
    if not changed and rules_store is None:
        return {"validated": True, "packages_checked": 0}
    fields = ("agent_instance_id", "project_ref", "provider", "runtime_role")
    values = {field: {"", str(context.get(field) or "")} for field in fields}
    for atom in before + after:
        if not _always(atom):
            continue
        for field in fields:
            value = str(getattr(atom, field, "") or "")
            values[field].add(canonical_project_ref(value) if field == "project_ref" else value)
        audience = MemoryAtomStore._native_audience(atom)
        if audience:
            if audience["target_type"] in {"agent", "agent_project"}:
                values["agent_instance_id"].add(audience["target_id"])
            if audience["project_ref"]:
                values["project_ref"].add(audience["project_ref"])
    if port.layout.rules_db.is_file():
        # The phase-1 placeholder has no bindings and is a valid empty store.
        from ..rule_reconciliation import canonical_reconciliation_status

        readiness = canonical_reconciliation_status(port.workspace, context["share_group_id"], store=rules_store)
        if readiness.get("failures") != ["rule_intelligence_not_initialized"]:
            rules = rules_store if rules_store is not None else port._domain_store("rules")
            for binding in rules.list_bindings(share_group_id=context["share_group_id"], status="active"):
                target = str(binding.target_type)
                if target in {"agent", "agent_project"}:
                    values["agent_instance_id"].add(str(binding.target_id))
                if target in {"project", "agent_project"}:
                    values["project_ref"].add(canonical_project_ref(binding.project_ref or binding.target_id))
                if target in {"provider", "runtime_role"}:
                    values[target].add(str(binding.target_id).casefold())
    for field in fields:
        other = "__memoryguard_budget_other__"
        while other in values[field]:
            other += "_"
        values[field].add(other)
    # Empty optional dimensions are real legacy wildcard scopes.  An empty
    # agent is not a valid bootstrap identity, so use the other partition.
    values["agent_instance_id"].discard("")
    count = 1
    for dimension in values.values():
        count *= len(dimension)
    if count > 100_000:
        raise MandatoryPublicationError("mandatory_scope_validation_limit")

    memory = port._domain_store("memory", write=True)
    mappings = {
        atom.memory_id: {atom.memory_id} | {
            str(row.get("source_record_id") or "")
            for row in memory.list_source_mappings(atom_id=atom.atom_id)
        }
        for atom in after if _always(atom)
    }
    configured = port.context_engine
    engine = ContextEngine(
        ready=True, state="V2_ACTIVE",
        budget=getattr(configured, "budget", None),
        token_counter=getattr(configured, "token_counter", None),
    )
    checked: set[tuple] = set()
    maximum = {"items": 0, "chars": 0, "tokens": 0}
    # One rule-catalog load for every audience cell.  Re-reading reconciliation,
    # definitions, bindings and evidence per coordinate repeated the same SQL
    # for packages that differ only by provenance dimensions.
    catalog: dict[Any, Any] = {}
    for coordinates in product(*(sorted(values[field]) for field in fields)):
        scope_dict = {
            "workspace_id": str(port.workspace),
            "share_group_id": str(context["share_group_id"]),
            **dict(zip(fields, coordinates)),
        }
        scope = MemoryReadScope(**scope_dict)
        if rules_store is None and not any(memory._atom_visible_to_scope(atom, scope) for atom in changed):
            continue
        atoms = port._canonical_memory_rows(
            [atom for atom in after if _always(atom) and memory._atom_visible_to_scope(atom, scope)],
            scope_dict, suppress_rule_sources=False,
        )
        candidates: dict[str, Any] = {"mandatory": [], "relevant": []}
        represented: set[str] = set()
        if port.layout.rules_db.is_file():
            represented = port._retrieve_v2_rules(
                group=scope.share_group_id, scope_public=scope_dict, result=candidates,
                catalog=catalog, rules_store=rules_store,
            )["mandatory"]
        candidates["mandatory"].extend({
            "item_id": atom.memory_id, "body": atom.body.strip(), "kind": atom.kind,
            "is_rule": True, "status": atom.status, "priority": atom.priority,
            "scope": scope_dict, "source": "native-v2-memory",
        } for atom in atoms if not (mappings[atom.memory_id] & represented))
        signature = tuple(sorted((item["item_id"], item["body"]) for item in candidates["mandatory"]))
        if signature in checked:
            continue
        checked.add(signature)
        packet = engine.bootstrap(
            {**scope_dict, "trusted_identity": {"agent": scope.agent_instance_id, "group": scope.share_group_id}},
            {"mandatory": candidates["mandatory"]},
        ).to_dict()
        if packet["status"] != "ok":
            # Do not disclose another audience's IDs, body or receipts.
            raise MandatoryPublicationError(str(packet.get("error") or "mandatory_validation_failed"))
        usage = packet.get("budget", {}).get("mandatory", {})
        for key in maximum:
            maximum[key] = max(maximum[key], int(usage.get(key, 0)))
    return {"validated": True, "packages_checked": len(checked), "maximum": maximum}
