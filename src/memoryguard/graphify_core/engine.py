"""Deterministic structural extractor used by MemoryGuard CodeGraph.

The implementation is a MemoryGuard-maintained fork of the structural ideas in
Graphify 0.9.19.  It deliberately does not implement Graphify's CLI, wiki,
visualization, LLM, MCP, or report products.  It turns code into a bounded set
of body-free symbol/relationship rows that the MemoryGuard exporter can map to
CodeGraph V2.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


CODE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".go", ".rs", ".java", ".groovy", ".c", ".h", ".cc", ".cpp",
    ".cxx", ".hpp", ".hh", ".cs", ".kt", ".kts", ".scala", ".sc",
    ".php", ".swift", ".lua", ".rb", ".sh", ".bash", ".ps1", ".psm1",
    ".ex", ".exs", ".m", ".mm", ".jl", ".v", ".sv", ".f", ".f90",
    ".f95", ".f03", ".f08", ".zig", ".sql", ".tf", ".tfvars", ".json",
    ".dart", ".apex", ".cls", ".trigger", ".pas", ".pp", ".razor", ".cshtml",
})

_NOISE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".venv", "venv", "env",
    "node_modules", "bower_components", "vendor", "dist", "build", "coverage",
    ".coverage", ".cache", "graphify-out", ".memoryguard", "target", "bin", "obj",
    ".tmp", ".worktrees",
})

_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_SYMBOLS_PER_FILE = 20_000
_MAX_EDGES_PER_FILE = 100_000


@dataclass(frozen=True)
class _TsConfig:
    module: str
    language_fn: str = "language"
    class_types: frozenset[str] = frozenset()
    function_types: frozenset[str] = frozenset()
    call_types: frozenset[str] = frozenset()
    call_field: str = "function"
    name_fields: tuple[str, ...] = ("name",)
    fallback_name_types: tuple[str, ...] = (
        "identifier", "type_identifier", "property_identifier", "field_identifier",
        "simple_identifier", "name", "constant",
    )


_TS_CONFIG_BY_SUFFIX: dict[str, _TsConfig] = {
    ".js": _TsConfig("tree_sitter_javascript", class_types=frozenset({"class_declaration"}), function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}), call_types=frozenset({"call_expression", "new_expression"})),
    ".jsx": _TsConfig("tree_sitter_javascript", class_types=frozenset({"class_declaration"}), function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}), call_types=frozenset({"call_expression", "new_expression"})),
    ".mjs": _TsConfig("tree_sitter_javascript", class_types=frozenset({"class_declaration"}), function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}), call_types=frozenset({"call_expression", "new_expression"})),
    ".cjs": _TsConfig("tree_sitter_javascript", class_types=frozenset({"class_declaration"}), function_types=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}), call_types=frozenset({"call_expression", "new_expression"})),
    ".ts": _TsConfig("tree_sitter_typescript", "language_typescript", frozenset({"class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration", "type_alias_declaration"}), frozenset({"function_declaration", "generator_function_declaration", "method_definition", "method_signature"}), frozenset({"call_expression", "new_expression"})),
    ".tsx": _TsConfig("tree_sitter_typescript", "language_tsx", frozenset({"class_declaration", "abstract_class_declaration", "interface_declaration", "enum_declaration", "type_alias_declaration"}), frozenset({"function_declaration", "generator_function_declaration", "method_definition", "method_signature"}), frozenset({"call_expression", "new_expression"})),
    ".go": _TsConfig("tree_sitter_go", class_types=frozenset({"type_declaration"}), function_types=frozenset({"function_declaration", "method_declaration"}), call_types=frozenset({"call_expression"})),
    ".rs": _TsConfig("tree_sitter_rust", class_types=frozenset({"struct_item", "enum_item", "trait_item", "impl_item"}), function_types=frozenset({"function_item"}), call_types=frozenset({"call_expression"})),
    ".java": _TsConfig("tree_sitter_java", class_types=frozenset({"class_declaration", "interface_declaration", "record_declaration", "enum_declaration", "annotation_type_declaration"}), function_types=frozenset({"method_declaration", "constructor_declaration"}), call_types=frozenset({"method_invocation", "object_creation_expression"}), call_field="name"),
    ".groovy": _TsConfig("tree_sitter_groovy", class_types=frozenset({"class_declaration", "interface_declaration"}), function_types=frozenset({"method_declaration", "constructor_declaration"}), call_types=frozenset({"method_invocation"}), call_field="name"),
    ".c": _TsConfig("tree_sitter_c", class_types=frozenset({"struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".h": _TsConfig("tree_sitter_c", class_types=frozenset({"struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".cc": _TsConfig("tree_sitter_cpp", class_types=frozenset({"class_specifier", "struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".cpp": _TsConfig("tree_sitter_cpp", class_types=frozenset({"class_specifier", "struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".cxx": _TsConfig("tree_sitter_cpp", class_types=frozenset({"class_specifier", "struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".hpp": _TsConfig("tree_sitter_cpp", class_types=frozenset({"class_specifier", "struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".hh": _TsConfig("tree_sitter_cpp", class_types=frozenset({"class_specifier", "struct_specifier", "enum_specifier"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"})),
    ".cs": _TsConfig("tree_sitter_c_sharp", class_types=frozenset({"class_declaration", "interface_declaration", "enum_declaration", "struct_declaration", "record_declaration"}), function_types=frozenset({"method_declaration", "constructor_declaration"}), call_types=frozenset({"invocation_expression", "object_creation_expression"})),
    ".kt": _TsConfig("tree_sitter_kotlin", class_types=frozenset({"class_declaration", "object_declaration"}), function_types=frozenset({"function_declaration"}), call_types=frozenset({"call_expression"}), call_field=""),
    ".kts": _TsConfig("tree_sitter_kotlin", class_types=frozenset({"class_declaration", "object_declaration"}), function_types=frozenset({"function_declaration"}), call_types=frozenset({"call_expression"}), call_field=""),
    ".scala": _TsConfig("tree_sitter_scala", class_types=frozenset({"class_definition", "object_definition", "trait_definition"}), function_types=frozenset({"function_definition"}), call_types=frozenset({"call_expression"}), call_field=""),
    ".php": _TsConfig("tree_sitter_php", "language_php", frozenset({"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}), frozenset({"function_definition", "method_declaration"}), frozenset({"function_call_expression", "member_call_expression", "scoped_call_expression"})),
    ".swift": _TsConfig("tree_sitter_swift", class_types=frozenset({"class_declaration", "protocol_declaration", "struct_declaration", "enum_declaration"}), function_types=frozenset({"function_declaration", "init_declaration", "deinit_declaration", "subscript_declaration"}), call_types=frozenset({"call_expression"}), call_field=""),
    ".lua": _TsConfig("tree_sitter_lua", class_types=frozenset(), function_types=frozenset({"function_declaration"}), call_types=frozenset({"function_call"}), call_field="name"),
    ".rb": _TsConfig("tree_sitter_ruby", class_types=frozenset({"class", "module"}), function_types=frozenset({"method", "singleton_method"}), call_types=frozenset({"call"}), call_field="method"),
    ".sh": _TsConfig("tree_sitter_bash", class_types=frozenset(), function_types=frozenset({"function_definition"}), call_types=frozenset({"command"}), call_field="name"),
    ".bash": _TsConfig("tree_sitter_bash", class_types=frozenset(), function_types=frozenset({"function_definition"}), call_types=frozenset({"command"}), call_field="name"),
    ".ps1": _TsConfig("tree_sitter_powershell", class_types=frozenset({"class_statement"}), function_types=frozenset({"function_statement", "filter_statement"}), call_types=frozenset({"command"}), call_field=""),
    ".psm1": _TsConfig("tree_sitter_powershell", class_types=frozenset({"class_statement"}), function_types=frozenset({"function_statement", "filter_statement"}), call_types=frozenset({"command"}), call_field=""),
    ".ex": _TsConfig("tree_sitter_elixir", class_types=frozenset({"call"}), function_types=frozenset({"call"}), call_types=frozenset({"call"}), call_field=""),
    ".exs": _TsConfig("tree_sitter_elixir", class_types=frozenset({"call"}), function_types=frozenset({"call"}), call_types=frozenset({"call"}), call_field=""),
    ".m": _TsConfig("tree_sitter_objc", class_types=frozenset({"class_interface", "class_implementation", "protocol_declaration"}), function_types=frozenset({"method_definition", "function_definition"}), call_types=frozenset({"call_expression", "message_expression"})),
    ".mm": _TsConfig("tree_sitter_objc", class_types=frozenset({"class_interface", "class_implementation", "protocol_declaration"}), function_types=frozenset({"method_definition", "function_definition"}), call_types=frozenset({"call_expression", "message_expression"})),
    ".jl": _TsConfig("tree_sitter_julia", class_types=frozenset({"struct_definition", "abstract_definition", "primitive_definition"}), function_types=frozenset({"function_definition", "short_function_definition"}), call_types=frozenset({"call_expression"})),
    ".v": _TsConfig("tree_sitter_verilog", class_types=frozenset({"module_declaration", "interface_declaration", "class_declaration"}), function_types=frozenset({"function_declaration", "task_declaration"}), call_types=frozenset({"method_call", "system_tf_call"})),
    ".sv": _TsConfig("tree_sitter_verilog", class_types=frozenset({"module_declaration", "interface_declaration", "class_declaration"}), function_types=frozenset({"function_declaration", "task_declaration"}), call_types=frozenset({"method_call", "system_tf_call"})),
    ".f": _TsConfig("tree_sitter_fortran", class_types=frozenset({"module", "derived_type_definition"}), function_types=frozenset({"function", "subroutine"}), call_types=frozenset({"call_expression", "call_statement"}), call_field=""),
    ".f90": _TsConfig("tree_sitter_fortran", class_types=frozenset({"module", "derived_type_definition"}), function_types=frozenset({"function", "subroutine"}), call_types=frozenset({"call_expression", "call_statement"}), call_field=""),
    ".f95": _TsConfig("tree_sitter_fortran", class_types=frozenset({"module", "derived_type_definition"}), function_types=frozenset({"function", "subroutine"}), call_types=frozenset({"call_expression", "call_statement"}), call_field=""),
    ".zig": _TsConfig("tree_sitter_zig", class_types=frozenset({"ContainerDecl"}), function_types=frozenset({"FnProto"}), call_types=frozenset({"CallExpr"}), call_field=""),
}

_DECL_RE = re.compile(r"(?m)^\s*(?:export\s+)?(?:async\s+)?(?:function|class|interface|struct|enum|trait|def|fn|func|sub|procedure)\s+([A-Za-z_][A-Za-z0-9_$]*)")
_ARROW_RE = re.compile(r"(?m)^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s*)?(?:\([^\n]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>")
_CALL_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_CALL_STOP = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof", "typeof", "new", "super", "this"})


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8", "replace")).hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return path.name


def _node_id(relative: str, kind: str, qualified: str, line: int) -> str:
    return "graphify-" + _digest(relative, kind, qualified, int(line))


def _line(node: Any) -> int:
    try:
        return int(node.start_point[0]) + 1
    except Exception:
        return int(getattr(node, "lineno", 1) or 1)


def _end_line(node: Any) -> int:
    try:
        return int(node.end_point[0]) + 1
    except Exception:
        return int(getattr(node, "end_lineno", _line(node)) or _line(node))


def _source_location(start: int, end: int | None = None) -> str:
    end_value = int(end or start)
    return f"L{max(1, int(start))}" if end_value <= int(start) else f"L{max(1, int(start))}-L{end_value}"


def _decode_node(node: Any, source: bytes) -> str:
    try:
        return source[node.start_byte:node.end_byte].decode("utf-8", "replace")
    except Exception:
        return ""


def _identifier_from_text(value: str) -> str:
    tokens = re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", str(value or ""))
    return tokens[-1] if tokens else ""


def _ts_name(node: Any, source: bytes, config: _TsConfig) -> str:
    for field in config.name_fields:
        try:
            child = node.child_by_field_name(field)
        except Exception:
            child = None
        if child is not None:
            name = _identifier_from_text(_decode_node(child, source))
            if name:
                return name
    try:
        for child in node.named_children:
            if child.type in config.fallback_name_types:
                name = _identifier_from_text(_decode_node(child, source))
                if name:
                    return name
    except Exception:
        pass
    return ""


def _tree_sitter_module(name: str):
    """Load supported grammars through explicit static import edges."""
    if name == "tree_sitter_javascript":
        import tree_sitter_javascript as module
    elif name == "tree_sitter_typescript":
        import tree_sitter_typescript as module
    elif name == "tree_sitter_go":
        import tree_sitter_go as module
    elif name == "tree_sitter_rust":
        import tree_sitter_rust as module
    elif name == "tree_sitter_java":
        import tree_sitter_java as module
    elif name == "tree_sitter_groovy":
        import tree_sitter_groovy as module
    elif name == "tree_sitter_c":
        import tree_sitter_c as module
    elif name == "tree_sitter_cpp":
        import tree_sitter_cpp as module
    elif name == "tree_sitter_c_sharp":
        import tree_sitter_c_sharp as module
    elif name == "tree_sitter_kotlin":
        import tree_sitter_kotlin as module
    elif name == "tree_sitter_scala":
        import tree_sitter_scala as module
    elif name == "tree_sitter_php":
        import tree_sitter_php as module
    elif name == "tree_sitter_swift":
        import tree_sitter_swift as module
    elif name == "tree_sitter_lua":
        import tree_sitter_lua as module
    elif name == "tree_sitter_ruby":
        import tree_sitter_ruby as module
    elif name == "tree_sitter_bash":
        import tree_sitter_bash as module
    elif name == "tree_sitter_powershell":
        import tree_sitter_powershell as module
    elif name == "tree_sitter_elixir":
        import tree_sitter_elixir as module
    elif name == "tree_sitter_objc":
        import tree_sitter_objc as module
    elif name == "tree_sitter_julia":
        import tree_sitter_julia as module
    elif name == "tree_sitter_verilog":
        import tree_sitter_verilog as module
    elif name == "tree_sitter_fortran":
        import tree_sitter_fortran as module
    elif name == "tree_sitter_zig":
        import tree_sitter_zig as module
    else:
        return None
    return module


def _load_ts_language(config: _TsConfig):
    try:
        from tree_sitter import Language, Parser
        module = _tree_sitter_module(config.module)
        if module is None:
            return None, None
        fn = getattr(module, config.language_fn, None)
        if not callable(fn) and config.language_fn != "language":
            fn = getattr(module, "language", None)
        if not callable(fn):
            return None, None
        language = Language(fn())
        try:
            parser = Parser(language)
        except TypeError:
            parser = Parser()
            parser.language = language
        return parser, language
    except Exception:
        return None, None


def collect_files(target: str | Path, *, follow_symlinks: bool = False, root: str | Path | None = None) -> list[Path]:
    """Return supported source files without following project-external links."""
    base = Path(target).expanduser()
    containment = Path(root).expanduser() if root is not None else base
    try:
        containment = containment.resolve()
    except OSError:
        return []
    if base.is_file():
        try:
            candidate = base.resolve()
            candidate.relative_to(containment if containment.is_dir() else containment.parent)
        except (OSError, ValueError):
            return []
        return [candidate] if candidate.suffix.lower() in CODE_EXTENSIONS else []
    if not base.is_dir():
        return []
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=follow_symlinks):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name.casefold() not in _NOISE_DIRS]
        if not follow_symlinks:
            dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.suffix.lower() not in CODE_EXTENSIONS or path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(containment)
            except (OSError, ValueError):
                continue
            try:
                if resolved.stat().st_size > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            results.append(resolved)
    return sorted(dict.fromkeys(results), key=lambda item: str(item).casefold())


def _annotation_names(node: ast.AST | None) -> tuple[str, ...]:
    if node is None:
        return ()
    values: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            values.append(item.id)
        elif isinstance(item, ast.Attribute):
            values.append(item.attr)
    return tuple(dict.fromkeys(value for value in values if value and value not in {"None", "Any"}))


def _relative_python_module(relative: str, module: str | None, level: int) -> str:
    parts = list(Path(relative).with_suffix("").parts[:-1])
    if level > 0:
        remove = max(0, level - 1)
        if remove:
            parts = parts[:-remove] if remove < len(parts) else []
    else:
        parts = []
    if module:
        parts.extend(str(module).split("."))
    value = ".".join(part for part in parts if part)
    return value


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, *, path: Path, relative: str, file_id: str, callable_names: set[str]) -> None:
        self.path = path
        self.relative = relative
        self.file_id = file_id
        self.nodes: list[dict[str, Any]] = []
        self.pending_edges: list[dict[str, Any]] = []
        self.scope: list[tuple[str, str, str]] = []
        self.imported_names: dict[str, tuple[str, str]] = {}
        self.callable_names = set(callable_names)

    def _qualified(self, name: str) -> str:
        prefix = ".".join(item[0] for item in self.scope)
        return f"{prefix}.{name}" if prefix else name

    def _owner(self) -> str:
        return self.scope[-1][1] if self.scope else self.file_id

    def _target(self, name: str) -> dict[str, str]:
        module, original = self.imported_names.get(name, ("", name))
        return {"target_name": original or name, "target_module": module}

    def _uses(self, source: str, name: str, *, context: str, line: int) -> None:
        if not source or not name or name in _CALL_STOP:
            return
        self.pending_edges.append({
            "source": source,
            **self._target(name),
            "relation": "uses",
            "context": context,
            "source_location": _source_location(line),
        })

    def _add_rationale(self, owner_id: str, text: str, node: ast.AST) -> None:
        clean = re.sub(r"\s+", " ", str(text or "")).strip()
        if not clean:
            return
        doc_node = node
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                doc_node = first
        line = _line(doc_node)
        rationale_id = _node_id(self.relative, "rationale", _digest(owner_id, clean), line)
        self.nodes.append({
            "id": rationale_id,
            "label": clean[:1024],
            "qualified_name": f"rationale:{owner_id[-20:]}:{line}",
            "file_type": "rationale",
            "source_file": str(self.path),
            "source_location": _source_location(line, _end_line(doc_node)),
            "signature": "",
            "metadata": {"semantic_kind": ""},
        })
        self.pending_edges.append({
            "source": rationale_id,
            "target": owner_id,
            "relation": "rationale_for",
            "context": "",
            "source_location": _source_location(line),
        })

    def _add(self, name: str, kind: str, node: ast.AST, *, signature: str = "") -> str:
        qualified = self._qualified(name)
        start, end = _line(node), _end_line(node)
        node_id = _node_id(self.relative, kind, qualified, start)
        self.nodes.append({
            "id": node_id,
            "label": name,
            "qualified_name": qualified,
            "file_type": kind,
            "source_file": str(self.path),
            "source_location": _source_location(start, end),
            "signature": signature[:4096],
            "metadata": {"semantic_kind": kind},
        })
        parent_id = self._owner()
        if parent_id and parent_id != node_id:
            relation = "method" if kind == "method" and self.scope and self.scope[-1][2] == "class" else "contains"
            self.pending_edges.append({
                "source": parent_id,
                "target": node_id,
                "relation": relation,
                "context": "",
                "source_location": _source_location(start),
            })
        doc = ast.get_docstring(node, clean=True) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if doc:
            self._add_rationale(node_id, doc, node)
        return node_id

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        node_id = self._add(node.name, "class", node)
        for base in node.bases:
            try:
                name = _identifier_from_text(ast.unparse(base))
            except Exception:
                name = ""
            if name:
                self.pending_edges.append({
                    "source": node_id,
                    **self._target(name),
                    "relation": "inherits",
                    "context": "inheritance",
                    "source_location": _source_location(_line(node)),
                })
        self.scope.append((node.name, node_id, "class"))
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> Any:
        kind = "method" if self.scope and self.scope[-1][2] == "class" else "function"
        args = []
        all_args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        for arg in all_args:
            args.append(arg.arg)
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        node_id = self._add(node.name, kind, node, signature=f"{node.name}({', '.join(args)})")
        for arg in all_args:
            for name in _annotation_names(arg.annotation):
                self._uses(node_id, name, context="parameter_type", line=_line(arg.annotation or arg))
        if node.args.vararg and node.args.vararg.annotation:
            for name in _annotation_names(node.args.vararg.annotation):
                self._uses(node_id, name, context="parameter_type", line=_line(node.args.vararg.annotation))
        if node.args.kwarg and node.args.kwarg.annotation:
            for name in _annotation_names(node.args.kwarg.annotation):
                self._uses(node_id, name, context="parameter_type", line=_line(node.args.kwarg.annotation))
        for name in _annotation_names(node.returns):
            self._uses(node_id, name, context="return_type", line=_line(node.returns or node))
        self.scope.append((node.name, node_id, "function"))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return self._visit_function(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        module = _relative_python_module(self.relative, node.module, int(node.level or 0)) if node.level else str(node.module or "")
        if module:
            self.pending_edges.append({
                "source": self.file_id,
                "target_module": module,
                "relation": "imports_from",
                "context": "import",
                "source_location": _source_location(_line(node)),
            })
            if Path(self.relative).name == "__init__.py":
                self.pending_edges.append({
                    "source": self.file_id,
                    "target_module": module,
                    "relation": "re_exports",
                    "context": "export",
                    "source_location": _source_location(_line(node)),
                })
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.imported_names[local] = (module, alias.name)
            self.pending_edges.append({
                "source": self.file_id,
                "target_name": alias.name,
                "target_module": module,
                "relation": "imports",
                "context": "import",
                "source_location": _source_location(_line(node)),
            })

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            module = alias.name
            local = alias.asname or alias.name.split(".", 1)[0]
            self.imported_names[local] = (module, "")
            self.pending_edges.append({
                "source": self.file_id,
                "target_module": module,
                "relation": "imports",
                "context": "import",
                "source_location": _source_location(_line(node)),
            })

    def visit_Name(self, node: ast.Name) -> Any:
        if not isinstance(node.ctx, ast.Load) or not self.scope:
            return
        name = node.id
        if (
            name in self.imported_names
            or name in self.callable_names
            or (name[:1].isupper() if name else False)
            or name.isupper()
        ):
            self._uses(self._owner(), name, context="", line=_line(node))

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if isinstance(node.ctx, ast.Load) and self.scope and isinstance(node.value, ast.Name):
            imported = self.imported_names.get(node.value.id)
            if imported:
                self.pending_edges.append({
                    "source": self._owner(),
                    "target_name": node.attr,
                    "target_module": imported[0],
                    "relation": "uses",
                    "context": "getattr",
                    "source_location": _source_location(_line(node)),
                })
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if self.scope:
            target = ""
            target_module = ""
            value = node.func
            if isinstance(value, ast.Name):
                target = value.id
                imported = self.imported_names.get(value.id)
                if imported:
                    target_module, imported_name = imported
                    target = imported_name or target
            elif isinstance(value, ast.Attribute):
                target = value.attr
                if isinstance(value.value, ast.Name):
                    imported = self.imported_names.get(value.value.id)
                    if imported:
                        target_module = imported[0]
            if target and target not in _CALL_STOP:
                self.pending_edges.append({
                    "source": self._owner(),
                    "target_name": target,
                    "target_module": target_module,
                    "relation": "calls",
                    "context": "call",
                    "source_location": _source_location(_line(node)),
                })
            for argument in list(node.args) + [keyword.value for keyword in node.keywords]:
                argument_name = argument.id if isinstance(argument, ast.Name) else (argument.attr if isinstance(argument, ast.Attribute) else "")
                if (
                    argument_name
                    and argument_name not in _CALL_STOP
                    and (argument_name in self.imported_names or argument_name in self.callable_names or argument_name[:1].isupper())
                ):
                    self.pending_edges.append({
                        "source": self._owner(),
                        **self._target(argument_name),
                        "relation": "indirect_call",
                        "context": "argument",
                        "source_location": _source_location(_line(argument)),
                    })
        self.generic_visit(node)


def _extract_python(path: Path, root: Path) -> dict[str, Any]:
    relative = _safe_relative(path, root)
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return {"nodes": [], "pending_edges": [], "diagnostics": [{"code": "python_parse_failed", "error_type": type(exc).__name__, "path": relative}]}
    file_id = _node_id(relative, "file", relative, 1)
    file_node = {
        "id": file_id,
        "label": Path(relative).name,
        "qualified_name": relative,
        "file_type": "file",
        "source_file": str(path),
        "source_location": "L1",
        "signature": "",
        "metadata": {"semantic_kind": ""},
    }
    callable_names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    visitor = _PythonVisitor(path=path, relative=relative, file_id=file_id, callable_names=callable_names)
    module_doc = ast.get_docstring(tree, clean=True)
    if module_doc:
        visitor._add_rationale(file_id, module_doc, tree)
    visitor.visit(tree)
    nodes = [file_node, *visitor.nodes]
    return {"nodes": nodes[:_MAX_SYMBOLS_PER_FILE], "pending_edges": visitor.pending_edges[:_MAX_EDGES_PER_FILE], "diagnostics": []}


def _extract_tree_sitter(path: Path, root: Path, config: _TsConfig) -> dict[str, Any]:
    relative = _safe_relative(path, root)
    parser, _language = _load_ts_language(config)
    if parser is None:
        return _extract_regex(path, root, diagnostic="tree_sitter_language_unavailable")
    try:
        source = path.read_bytes()
        tree = parser.parse(source)
    except Exception as exc:
        return _extract_regex(path, root, diagnostic=f"tree_sitter_parse_failed:{type(exc).__name__}")
    nodes: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    scope: list[tuple[str, str, str]] = []

    def walk(node: Any) -> None:
        nonlocal nodes, pending
        is_class = node.type in config.class_types
        is_function = node.type in config.function_types
        pushed = False
        if (is_class or is_function) and len(nodes) < _MAX_SYMBOLS_PER_FILE:
            name = _ts_name(node, source, config)
            if name:
                parent_names = [item[0] for item in scope]
                qualified = ".".join(parent_names + [name])
                kind = "class" if is_class else ("method" if scope and scope[-1][2] == "class" else "function")
                start, end = _line(node), _end_line(node)
                node_id = _node_id(relative, kind, qualified, start)
                nodes.append({
                    "id": node_id, "label": name, "qualified_name": qualified,
                    "file_type": kind, "source_file": str(path),
                    "source_location": _source_location(start, end), "signature": "",
                    "metadata": {"semantic_kind": kind, "language_node_type": node.type},
                })
                if scope:
                    pending.append({"source": scope[-1][1], "target": node_id, "relation": "contains", "context": "member", "source_location": _source_location(start)})
                if is_class:
                    text = _decode_node(node, source)[:4000]
                    head = text.split("{", 1)[0]
                    for keyword in ("extends", "implements", ":"):
                        if keyword in head:
                            tail = head.split(keyword, 1)[1]
                            for base in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", tail)[:12]:
                                if base != name:
                                    pending.append({"source": node_id, "target_name": base, "relation": "inherits", "context": "inheritance", "source_location": _source_location(start)})
                            break
                scope.append((name, node_id, "class" if is_class else "function"))
                pushed = True
        if node.type in config.call_types and scope and len(pending) < _MAX_EDGES_PER_FILE:
            target_node = None
            if config.call_field:
                try:
                    target_node = node.child_by_field_name(config.call_field)
                except Exception:
                    target_node = None
            if target_node is None:
                try:
                    target_node = node.named_children[0] if node.named_children else None
                except Exception:
                    target_node = None
            target = _identifier_from_text(_decode_node(target_node or node, source))
            if target and target not in _CALL_STOP:
                pending.append({"source": scope[-1][1], "target_name": target, "relation": "calls", "context": "call", "source_location": _source_location(_line(node))})
        try:
            children = list(node.named_children)
        except Exception:
            children = []
        for child in children:
            walk(child)
        if pushed:
            scope.pop()

    walk(tree.root_node)
    return {"nodes": nodes, "pending_edges": pending, "diagnostics": []}


def _extract_regex(path: Path, root: Path, *, diagnostic: str = "") -> dict[str, Any]:
    relative = _safe_relative(path, root)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"nodes": [], "pending_edges": [], "diagnostics": [{"code": "source_read_failed", "error_type": type(exc).__name__, "path": relative}]}
    nodes: list[dict[str, Any]] = []
    by_name: dict[str, str] = {}
    for match in list(_DECL_RE.finditer(text)) + list(_ARROW_RE.finditer(text)):
        name = match.group(1)
        line = text.count("\n", 0, match.start()) + 1
        kind = "class" if re.search(r"\b(?:class|interface|struct|enum|trait)\s+" + re.escape(name), match.group(0)) else "function"
        node_id = _node_id(relative, kind, name, line)
        if node_id in {item["id"] for item in nodes}:
            continue
        nodes.append({
            "id": node_id, "label": name, "qualified_name": name,
            "file_type": kind, "source_file": str(path),
            "source_location": _source_location(line), "signature": "",
            "metadata": {"semantic_kind": kind, "fallback": True},
        })
        by_name.setdefault(name, node_id)
        if len(nodes) >= _MAX_SYMBOLS_PER_FILE:
            break
    pending: list[dict[str, Any]] = []
    if nodes:
        default_source = nodes[0]["id"]
        for match in _CALL_RE.finditer(text):
            name = match.group(1)
            if name in _CALL_STOP:
                continue
            line = text.count("\n", 0, match.start()) + 1
            pending.append({"source": default_source, "target_name": name, "relation": "calls", "context": "call", "source_location": _source_location(line)})
            if len(pending) >= _MAX_EDGES_PER_FILE:
                break
    diagnostics = [{"code": diagnostic, "path": relative}] if diagnostic else []
    return {"nodes": nodes, "pending_edges": pending, "diagnostics": diagnostics}


def _extract_file(path: Path, root: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".py", ".pyi"}:
        return _extract_python(path, root)
    config = _TS_CONFIG_BY_SUFFIX.get(suffix)
    if config is not None:
        return _extract_tree_sitter(path, root, config)
    return _extract_regex(path, root)


def _module_keys(relative: str) -> tuple[str, ...]:
    path = Path(str(relative or "").replace("\\", "/"))
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    values: list[str] = []
    if parts:
        values.append(".".join(parts))
        if parts[0] == "src" and len(parts) > 1:
            values.append(".".join(parts[1:]))
        if "memoryguard" in parts:
            index = parts.index("memoryguard")
            values.append(".".join(parts[index:]))
        if len(parts) > 1:
            values.append(".".join(parts[-2:]))
        values.append(parts[-1])
    return tuple(dict.fromkeys(value for value in values if value))


def _module_variants(value: str) -> tuple[str, ...]:
    text = str(value or "").strip(".")
    if not text:
        return ()
    values = [text]
    if text.startswith("src."):
        values.append(text[4:])
    if ".memoryguard." in f".{text}.":
        index = text.find("memoryguard.")
        if index >= 0:
            values.append(text[index:])
    parts = text.split(".")
    if len(parts) > 1:
        values.append(".".join(parts[-2:]))
    values.append(parts[-1])
    return tuple(dict.fromkeys(value for value in values if value))


def _resolve_edges(nodes: list[dict[str, Any]], pending: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_simple: dict[str, list[str]] = {}
    by_qualified: dict[str, str] = {}
    module_files: dict[str, str] = {}
    source_modules: dict[str, tuple[str, ...]] = {}
    node_ids = {str(node.get("id") or "") for node in nodes}
    for node in nodes:
        node_id = str(node.get("id") or "")
        label = str(node.get("label") or "")
        qualified = str(node.get("qualified_name") or label)
        if label:
            by_simple.setdefault(label, []).append(node_id)
        if qualified:
            by_qualified.setdefault(qualified, node_id)
        if str(node.get("file_type") or "") == "file":
            keys = _module_keys(qualified)
            source_modules[str(node.get("source_file") or "")] = keys
            for key in keys:
                module_files.setdefault(key, node_id)

    module_symbols: dict[tuple[str, str], list[str]] = {}
    for node in nodes:
        label = str(node.get("label") or "")
        if not label or str(node.get("file_type") or "") in {"file", "rationale"}:
            continue
        for module in source_modules.get(str(node.get("source_file") or ""), ()):
            module_symbols.setdefault((module, label), []).append(str(node.get("id") or ""))

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for raw in pending:
        source = str(raw.get("source") or "")
        target = str(raw.get("target") or "")
        target_name = str(raw.get("target_name") or "")
        target_module = str(raw.get("target_module") or "")
        if not target and target_module:
            module_variants = _module_variants(target_module)
            if target_name:
                for module in module_variants:
                    matches = module_symbols.get((module, target_name), ())
                    if len(matches) == 1:
                        target = matches[0]
                        break
            else:
                for module in module_variants:
                    target = module_files.get(module, "")
                    if target:
                        break
        if not target and target_name:
            exact = by_qualified.get(target_name)
            if exact:
                target = exact
            else:
                matches = by_simple.get(target_name, ())
                if len(matches) == 1:
                    target = matches[0]
        if not source or not target or source == target or source not in node_ids or target not in node_ids:
            continue
        relation = str(raw.get("relation") or "related")
        context = str(raw.get("context") or "")
        location = str(raw.get("source_location") or "")
        identity_location = "" if relation in {"calls", "uses", "indirect_call", "imports", "imports_from", "re_exports"} else location
        key = (source, target, relation, context, identity_location)
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "source": source,
            "target": target,
            "relation": relation,
            "context": context,
            "confidence": "EXTRACTED",
            "source_location": location,
            "weight": 1.0,
        })
    return edges


def extract(paths: Iterable[str | Path], *, root: str | Path | None = None, parallel: bool = True) -> dict[str, Any]:
    """Extract body-free symbols and structural relationships.

    ``parallel`` is accepted as part of the stable internal Graphify Core API.
    The current implementation deliberately runs deterministically in-process;
    CodeGraph task parallelism is owned by MemoryGuard's task coordinator.
    """
    del parallel
    normalized = [Path(item).expanduser().resolve() for item in paths]
    if root is None:
        if normalized:
            try:
                common = Path(os.path.commonpath([str(item.parent) for item in normalized]))
            except ValueError:
                common = normalized[0].parent
            root_path = common.resolve()
        else:
            root_path = Path.cwd().resolve()
    else:
        root_path = Path(root).expanduser().resolve()
    nodes: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for path in normalized:
        if path.suffix.lower() not in CODE_EXTENSIONS or not path.is_file():
            continue
        result = _extract_file(path, root_path)
        nodes.extend(result.get("nodes") or ())
        pending.extend(result.get("pending_edges") or ())
        diagnostics.extend(result.get("diagnostics") or ())
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id:
            node_map.setdefault(node_id, node)
    nodes = list(node_map.values())
    edges = _resolve_edges(nodes, pending)
    return {"nodes": nodes, "edges": edges, "diagnostics": diagnostics}


__all__ = ["CODE_EXTENSIONS", "collect_files", "extract"]
