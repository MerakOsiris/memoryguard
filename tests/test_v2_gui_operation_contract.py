from __future__ import annotations

from memoryguard.cutover_v2.gui_contract import visible_gui_methods, visible_registry_issues
from memoryguard.cutover_v2.surfaces import (
    GUI_METHOD_NAMES,
    GUI_MUTATION_NAMES,
    GUI_OPERATION_SPECS,
)
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort
from memoryguard.security import ALL_ALLOWED_METHODS, MUTATION_API_METHODS, READONLY_API_METHODS


def test_gui_operation_registry_is_single_truth_source() -> None:
    assert len(GUI_OPERATION_SPECS) == 172
    assert {"list_codegraph_projects", "codegraph_status", "build_codegraph"} <= set(GUI_OPERATION_SPECS)
    assert GUI_METHOD_NAMES == frozenset(GUI_OPERATION_SPECS)
    assert len(GUI_MUTATION_NAMES) == 77
    assert MUTATION_API_METHODS == GUI_MUTATION_NAMES
    assert ALL_ALLOWED_METHODS == GUI_METHOD_NAMES
    assert READONLY_API_METHODS == GUI_METHOD_NAMES - GUI_MUTATION_NAMES

    for name, spec in GUI_OPERATION_SPECS.items():
        assert spec.public_name == name
        assert spec.canonical_name
        assert spec.domain
        assert spec.kind in {"read", "mutation"}
        assert spec.execution in {"sync", "task"}
        assert spec.native_handler
        assert spec.mutation is (name in GUI_MUTATION_NAMES)
        if spec.execution == "task":
            assert spec.cancel_operation == "task_cancel"


def test_embedded_gui_literal_methods_are_known_to_registry() -> None:
    visible = visible_gui_methods()
    assert visible
    assert visible <= GUI_METHOD_NAMES
    assert not [item for item in visible_registry_issues() if item["code"] == "visible_gui_method_unknown"]


def test_projection_build_task_api_contract_exposes_run_id_engine_and_exact_cancel() -> None:
    start = GUI_OPERATION_SPECS["start_build_projection"]
    cancel = GUI_OPERATION_SPECS["cancel_build_projection"]
    status = GUI_OPERATION_SPECS["get_build_progress"]

    assert start.execution == "task"
    assert start.cancel_operation == "task_cancel"
    assert {"mode", "scope", "llm_agent", "llm_cli", "enrich_mode"} <= set(start.parameters)
    assert cancel.execution == "sync"
    assert tuple(cancel.parameters) == ("run_id", "confirmed")
    assert tuple(status.parameters) == ("run_id",)


def test_gui_native_registry_never_uses_retired_status(tmp_path) -> None:
    entries = NativeV2RuntimePort(tmp_path).coverage()["surfaces"]["gui"]["entries"]
    assert len(entries) == 172
    assert all(item["status"] in {"implemented", "blocker"} for item in entries)
    assert all(item["status"] != "retired" for item in entries)
    assert all(item["canonical_name"] for item in entries)
    assert all(item["domain"] for item in entries)
    assert all(item["execution"] in {"sync", "task"} for item in entries)


def test_visible_gui_issues_are_only_real_unimplemented_handlers(tmp_path) -> None:
    coverage = NativeV2RuntimePort(tmp_path).coverage()
    entries = {item["name"]: item for item in coverage["surfaces"]["gui"]["entries"]}
    issues = visible_registry_issues(entries)
    for issue in issues:
        entry = entries[issue["name"]]
        assert issue["code"] == "visible_gui_method_not_implemented"
        assert entry["status"] == "blocker"
    assert coverage["production_complete"] is (not issues and coverage["counts"]["blocker"] == 0)
