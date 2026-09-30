"""Body-free MemoryGuard CodeGraph export from the built-in Graphify Core."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .embedded import extract_embedded_python, provenance_for_path
from .engine import collect_files, extract


EXPORT_FORMAT = "memoryguard-graphify-metadata-v1"
CORE_VERSION = "0.9.19+memoryguard.core.2"
EXTERNAL_ID_SCHEMA = "memoryguard-core-v1"
_SOURCE_LOCATION = re.compile(r"^L(?P<start>\d+)(?:[-:](?:L)?(?P<end>\d+))?$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, value: Any) -> str:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return ""
    candidate = Path(value)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        relative = resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        return ""
    return relative if relative and not relative.startswith("../") else ""


def _language(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    return {
        ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
        ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
        ".go": "go", ".rs": "rust", ".java": "java", ".groovy": "groovy",
        ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
        ".hpp": "cpp", ".hh": "cpp", ".cs": "csharp", ".kt": "kotlin", ".kts": "kotlin",
        ".scala": "scala", ".sc": "scala", ".php": "php", ".swift": "swift",
        ".lua": "lua", ".rb": "ruby", ".sh": "bash", ".bash": "bash",
        ".ps1": "powershell", ".psm1": "powershell", ".ex": "elixir", ".exs": "elixir",
        ".m": "objective-c", ".mm": "objective-c++", ".jl": "julia", ".v": "verilog",
        ".sv": "systemverilog", ".f": "fortran", ".f90": "fortran", ".f95": "fortran",
        ".zig": "zig", ".sql": "sql", ".tf": "terraform", ".json": "json",
        ".dart": "dart", ".pas": "pascal", ".pp": "pascal", ".razor": "razor",
        ".cshtml": "razor",
    }.get(suffix, suffix.lstrip(".") or "unknown")


def _line_map(relative_path: str, source_location: str) -> dict[str, Any]:
    result: dict[str, Any] = {"path": relative_path}
    match = _SOURCE_LOCATION.match(str(source_location or "").strip())
    if match:
        result["line_start"] = int(match.group("start"))
        result["line_end"] = int(match.group("end") or match.group("start"))
    return result


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "semantic_kind", "host_symbol", "region_id", "virtual_document_id",
        "confidence", "language", "language_node_type", "fallback", "action",
        "placeholder", "dispatch_alias",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
    return result


def _external_symbol_id(value: Any) -> str:
    return f"{EXTERNAL_ID_SCHEMA}:{str(value or '').strip()}"


def _semantic_priority(row: Mapping[str, Any]) -> tuple[int, str, int]:
    kind = str(row.get("semantic_kind") or "")
    path = str((row.get("source_map") or {}).get("path") or "")
    line = int((row.get("source_map") or {}).get("line_start") or 0)
    if kind == "api_method":
        return (0 if path.endswith("surfaces.py") else 1, path, line)
    if kind == "native_handler":
        return (0 if path.endswith("native_ports.py") else 1, path, line)
    return (0, path, line)


def _merge_embedded(raw: dict[str, Any], files: Iterable[Path]) -> None:
    nodes = raw.setdefault("nodes", [])
    edges = raw.setdefault("edges", [])
    diagnostics = raw.setdefault("diagnostics", [])
    known_nodes = {str(item.get("id") or "") for item in nodes if isinstance(item, Mapping)}
    known_edges = {
        (str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("relation") or ""), str(item.get("context") or ""))
        for item in edges if isinstance(item, Mapping)
    }
    for path in files:
        if path.suffix.casefold() not in {".py", ".pyi"}:
            continue
        embedded = extract_embedded_python(path)
        for item in embedded.get("nodes", ()):
            if not isinstance(item, dict):
                continue
            node_id = str(item.get("id") or "")
            if node_id and node_id not in known_nodes:
                nodes.append(item)
                known_nodes.add(node_id)
        for item in embedded.get("edges", ()):
            if not isinstance(item, dict):
                continue
            key = (str(item.get("source") or ""), str(item.get("target") or ""), str(item.get("relation") or ""), str(item.get("context") or ""))
            if key not in known_edges:
                edges.append(item)
                known_edges.add(key)
        for diagnostic in embedded.get("diagnostics", ()):
            if isinstance(diagnostic, Mapping):
                diagnostics.append(dict(diagnostic))


def export_repository(
    root: str | Path,
    *,
    paths: Iterable[str | Path] | None = None,
    complete: bool = True,
    parallel: bool = True,
    max_files: int = 50_000,
) -> dict[str, Any]:
    """Export one repository as the stable MemoryGuard Graphify envelope.

    The output contains only relative source paths, hashes, symbol metadata,
    source maps and relationships.  Source bodies are never returned.
    """
    repo = Path(root).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError("repository root is not a directory")
    if paths is None:
        files = collect_files(repo, follow_symlinks=False, root=repo)
    else:
        files = []
        for raw in paths:
            candidate = Path(raw)
            candidate = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
            try:
                candidate.relative_to(repo)
            except ValueError as exc:
                raise ValueError("export path escapes repository root") from exc
            if candidate.is_file():
                files.append(candidate)
    files = sorted(dict.fromkeys(files), key=lambda item: item.as_posix().casefold())
    if not files:
        raise ValueError("graphify export has no source files")
    if len(files) > max(1, int(max_files)):
        raise ValueError("graphify export exceeds file limit")

    file_rows: list[dict[str, Any]] = []
    file_by_path: dict[str, dict[str, Any]] = {}
    for file_path in files:
        relative = file_path.relative_to(repo).as_posix()
        provenance = provenance_for_path(relative)
        item = {
            "id": "file:" + hashlib.sha256(relative.encode("utf-8")).hexdigest(),
            "path": relative,
            "content_hash": _sha256(file_path),
            "language": _language(relative),
            "source_role": provenance,
            "provenance": provenance,
        }
        file_rows.append(item)
        file_by_path[relative] = item

    extracted = extract(files, root=repo, parallel=bool(parallel))
    _merge_embedded(extracted, files)
    raw_nodes = list(extracted.get("nodes") or ())
    raw_edges = list(extracted.get("edges") or ())

    node_by_id: dict[str, dict[str, Any]] = {}
    node_file: dict[str, dict[str, Any]] = {}
    raw_to_external: dict[str, str] = {}
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            continue
        raw_node_id = str(node.get("id") or "").strip()
        if not raw_node_id:
            continue
        node_id = _external_symbol_id(raw_node_id)
        raw_to_external[raw_node_id] = node_id
        relative = _relative(repo, node.get("source_file"))
        file_item = file_by_path.get(relative)
        if file_item is None:
            continue
        provenance = str(node.get("provenance") or file_item["provenance"])
        source_location = str(node.get("source_location") or "")[:128]
        source_map = _line_map(relative, source_location)
        raw_source_map = node.get("source_map")
        if isinstance(raw_source_map, Mapping):
            for key in ("host_symbol", "region_id", "virtual_document_id", "line_start", "line_end"):
                value = raw_source_map.get(key)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    source_map[key] = value
        semantic_kind = str(node.get("semantic_kind") or (node.get("metadata") or {}).get("semantic_kind") or node.get("file_type") or "symbol")[:128]
        name = str(node.get("label") or node.get("name") or raw_node_id)[:2048]
        row = {
            "id": node_id,
            "file": file_item["id"],
            "name": name,
            "kind": str(node.get("file_type") or node.get("kind") or "symbol")[:128],
            "signature": str(node.get("signature") or "")[:4096],
            "source_location": source_location,
            "provenance": provenance,
            "semantic_kind": semantic_kind,
            "source_map": source_map,
            "metadata": _safe_metadata(node.get("metadata")),
        }
        row["metadata"].setdefault("semantic_kind", semantic_kind)
        existing = node_by_id.get(node_id)
        if existing is None or _semantic_priority(row) < _semantic_priority(existing):
            node_by_id[node_id] = row
            node_file[node_id] = file_item

    edge_rows: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, ...]] = set()
    for edge in raw_edges:
        if not isinstance(edge, Mapping):
            continue
        source = raw_to_external.get(str(edge.get("source") or "").strip(), "")
        target = raw_to_external.get(str(edge.get("target") or "").strip(), "")
        if source not in node_by_id or target not in node_by_id:
            continue
        source_item = node_file[source]
        provenance = str(edge.get("provenance") or source_item["provenance"])
        relation = str(edge.get("relation") or "related")[:128]
        context = str(edge.get("context") or "")[:256]
        source_location = str(edge.get("source_location") or "")[:128]
        identity = (source, target, relation, context, provenance, source_location)
        if identity in seen_edges:
            continue
        seen_edges.add(identity)
        semantic_kind = str(edge.get("semantic_kind") or (edge.get("metadata") or {}).get("semantic_kind") or context)[:128]
        metadata = _safe_metadata(edge.get("metadata"))
        metadata.setdefault("semantic_kind", semantic_kind)
        edge_rows.append({
            "source": source,
            "target": target,
            "relation": relation,
            "context": context,
            "source_file": source_item["path"],
            "source_location": source_location,
            "provenance": provenance,
            "semantic_kind": semantic_kind,
            "metadata": metadata,
            "weight": float(edge.get("weight", 1.0) or 1.0),
        })

    source_digest = hashlib.sha256(
        json.dumps(
            [(item["path"], item["content_hash"], item["provenance"]) for item in file_rows],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    diagnostics: list[dict[str, Any]] = []
    for item in extracted.get("diagnostics") or ():
        if not isinstance(item, Mapping):
            continue
        safe = {
            ("diagnostic_code" if key == "code" else key): value for key, value in item.items()
            if key in {"code", "error_type", "limit", "bytes", "count"}
            and isinstance(value, (str, int, bool))
        }
        if safe:
            diagnostics.append(safe)
    return {
        "format": EXPORT_FORMAT,
        "complete": bool(complete),
        "graphify_version": CORE_VERSION,
        "source_digest": source_digest,
        "files": file_rows,
        "nodes": list(node_by_id.values()),
        "edges": edge_rows,
        "diagnostics": diagnostics,
    }


__all__ = ["CORE_VERSION", "EXTERNAL_ID_SCHEMA", "EXPORT_FORMAT", "export_repository"]
