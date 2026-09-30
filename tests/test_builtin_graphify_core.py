from __future__ import annotations

from pathlib import Path
import re
import sys

from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore
from memoryguard.codegraph_v2.graphify_adapter import GraphifyCapability, GraphifyExportAdapter
from memoryguard.cutover_v2.gui_contract import codegraph_gui_issues
from memoryguard.graphify_core import CORE_VERSION, EXPORT_FORMAT, export_repository


ROOT = Path(__file__).resolve().parents[1]


def test_parse_diagnostic_remains_metadata_and_does_not_block_other_files(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("def good(): return 1\n", encoding="utf-8")
    export = export_repository(tmp_path, parallel=False)
    assert export["diagnostics"] and export["diagnostics"][0]["diagnostic_code"] == "python_parse_failed"
    scope = CodeGraphScope(str(tmp_path), "agent", str(tmp_path), "codex", "group")
    store = CodeGraphStore(tmp_path)
    GraphifyExportAdapter(store).project(export, scope=scope)
    assert store.query_symbols("good", scope=scope)


def test_graphify_core_is_in_tree_and_no_graphifyy_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dependency_block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert "graphifyy" not in dependency_block.casefold()
    assert "graphify @" not in dependency_block.casefold()
    assert (ROOT / "src/memoryguard/graphify_core/LICENSE.graphify.txt").is_file()
    assert (ROOT / "src/memoryguard/graphify_core/NOTICE.md").is_file()


def test_graphify_capability_is_builtin_not_path_cli() -> None:
    capability = GraphifyCapability.detect()
    assert capability.available is True
    assert capability.metadata_export is True
    assert capability.executable == ""
    assert capability.version == CORE_VERSION


def test_builtin_export_is_body_free_and_emits_gui_semantic_chain(tmp_path: Path) -> None:
    interactive = tmp_path / "interactive.py"
    surfaces = tmp_path / "surfaces.py"
    native = tmp_path / "native_ports.py"
    interactive.write_text(
        '''PAGE_HTML = r"""<html><body>
<button onclick="addBook()">Add book</button>
<script>
async function addBook() { return await callApi('knowledge_add'); }
</script></body></html>"""
''',
        encoding="utf-8",
    )
    surfaces.write_text(
        '''def _add(*args, **kwargs): pass
_add("knowledge_add", "knowledge_source_add", "knowledge", "mutation", "gui_knowledge_command")
''',
        encoding="utf-8",
    )
    native.write_text(
        '''class Port:
    def _gui_knowledge_command(self, payload, context):
        return {"ok": True}
''',
        encoding="utf-8",
    )

    payload = export_repository(
        tmp_path,
        paths=[interactive, surfaces, native],
        parallel=False,
    )
    assert payload["format"] == EXPORT_FORMAT
    assert payload["graphify_version"] == CORE_VERSION
    assert len(payload["files"]) == 3
    assert {item["semantic_kind"] for item in payload["nodes"]} >= {
        "gui_control", "handler", "api_method", "surface_spec", "native_handler",
    }
    contexts = {item["context"] for item in payload["edges"]}
    assert {"control_handler", "handler_api", "api_surface", "surface_native"} <= contexts
    rendered = repr(payload).casefold()
    assert "return await callapi" not in rendered
    assert "<html><body>" not in rendered

    store = CodeGraphStore(tmp_path / "store")
    scope = CodeGraphScope(
        str((tmp_path / "store").resolve()), "", str(tmp_path.resolve()),
        "graphify-core", "group", "gui",
    )
    GraphifyExportAdapter(store).project(payload, scope=scope)
    assert codegraph_gui_issues(store, scope, root=tmp_path) == ()


def test_importing_builtin_core_does_not_require_external_graphify_module() -> None:
    # The implementation modules themselves never import the external package.
    core_root = ROOT / "src/memoryguard/graphify_core"
    imports = "\n".join(path.read_text(encoding="utf-8") for path in core_root.glob("*.py"))
    assert re.search(r"(?m)^\s*(?:from|import)\s+graphify(?:\.|\s|$)", imports) is None
    assert "memoryguard.graphify_core" in sys.modules
    assert "graphify" not in sys.modules


def test_builtin_python_semantics_keep_rationale_imports_and_type_uses(tmp_path: Path) -> None:
    models = tmp_path / "models.py"
    service = tmp_path / "service.py"
    models.write_text(
        '''class Payload:
    """Typed request carried between the parser and service."""


def helper() -> Payload:
    """Return one payload for the service."""
    return Payload()
''',
        encoding="utf-8",
    )
    service.write_text(
        '''"""Service boundary rationale."""
from models import Payload, helper


def run(value: Payload) -> Payload:
    """Delegate construction to the imported helper."""
    return helper()
''',
        encoding="utf-8",
    )

    payload = export_repository(tmp_path, paths=[models, service], parallel=False)
    relations = {item["relation"] for item in payload["edges"]}
    contexts = {item["context"] for item in payload["edges"]}
    kinds = {item["kind"] for item in payload["nodes"]}
    assert "rationale" in kinds
    assert {"rationale_for", "imports", "imports_from", "uses", "calls"} <= relations
    assert {"parameter_type", "return_type", "import", "call"} <= contexts
