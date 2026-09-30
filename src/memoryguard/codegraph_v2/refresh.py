"""Trusted incremental CodeGraph refresh after host file mutations.

Triggered only from a successful PostToolUse file write with host-provided
absolute paths.  No watcher, daemon, or full rebuild.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from ..graphify_core import CODE_EXTENSIONS, export_repository, provenance_for_path
from ..graphify_core.engine import _MAX_FILE_BYTES, _NOISE_DIRS
from ..rule_scope import canonical_project_ref
from .graphify_adapter import GraphifyExportAdapter
from .models import CodeGraphScope
from .store import CodeGraphStore, normalize_relative_path


AFFECTED_DEPTH = 2
AFFECTED_LIMIT = 32
AFFECTED_PROVENANCE = "production"
_FILE_TOOLS = frozenset({
    "write", "edit", "multiedit", "notebookedit", "applypatch", "apply_patch",
    "strreplace", "delete", "delete_file", "create_file", "move_file",
})
_PATH_KEYS = (
    "file_path", "path", "target_path", "notebook_path", "new_path", "old_path",
)
_MAX_REFRESH_PATHS = 64
_MAX_RELATED_FILES = 32
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _tool_basename(tool_name: str) -> str:
    value = str(tool_name or "").replace("\\", "/")
    value = value.split("/")[-1]
    value = value.split(".")[-1]
    if "__" in value:
        value = value.split("__")[-1]
    return value.strip().casefold().replace("-", "_")


def is_file_mutation_tool(tool_name: str) -> bool:
    name = _tool_basename(tool_name)
    if name in _FILE_TOOLS:
        return True
    compact = name.replace("_", "")
    return compact in {item.replace("_", "") for item in _FILE_TOOLS}


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _payload_cwd(payload: Mapping[str, Any] | None) -> Path | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("cwd", "workspace_path", "workspace", "project_root"):
        raw = str(payload.get(key) or "").strip()
        if raw:
            try:
                path = Path(raw).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if path.is_dir():
                return path
    return None


def _collect_payload_paths(payload: Mapping[str, Any] | None, tool_input: Any) -> list[str]:
    """Read only structured host path fields; never parse free-form text."""

    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            values.append(value.strip())

    def add_path_value(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in _PATH_KEYS:
                add(value.get(key))
            return
        add(value)

    if isinstance(payload, Mapping):
        for key in ("changed_files", "file_paths", "paths"):
            item = payload.get(key)
            if isinstance(item, (list, tuple)):
                for value in item:
                    add_path_value(value)
            else:
                add_path_value(item)
    mapping = tool_input if isinstance(tool_input, Mapping) else {}
    for key in _PATH_KEYS:
        add(mapping.get(key))
    edits = mapping.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, Mapping):
                for key in _PATH_KEYS:
                    add(edit.get(key))
    files = mapping.get("files")
    if isinstance(files, list):
        for item in files:
            add_path_value(item)
    return list(dict.fromkeys(values))[:_MAX_REFRESH_PATHS]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_fingerprint(path: Path) -> dict[str, Any]:
    info = path.stat()
    mtime_ns = int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)))
    return {
        "path": path,
        "mtime_ns": mtime_ns,
        "size": int(info.st_size),
        "content_hash": _sha256_file(path),
    }


def _is_ignored(relative: str) -> bool:
    parts = tuple(part for part in relative.replace("\\", "/").split("/") if part)
    if any(part.casefold() in _NOISE_DIRS for part in parts):
        return True
    return provenance_for_path(relative) in {"generated", "vendor"}


def _relative_to_root(root: Path, path: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        return None
    if not relative or relative.startswith("../"):
        return None
    return relative


def _source_root_for_cwd(store: CodeGraphStore, cwd: Path, context: Mapping[str, Any]) -> tuple[Path, CodeGraphScope] | None:
    wanted = canonical_project_ref(str(cwd))
    nearest = store.nearest_scopes(
        project_ref=wanted,
        share_group_id=str(context.get("share_group_id") or ""),
        agent_instance_id=str(context.get("agent_instance_id") or ""),
        provider=str(context.get("provider") or "graphify"),
        runtime_role=str(context.get("runtime_role") or ""),
        limit=1,
    )
    if not nearest:
        return None
    scope = nearest[0]
    root = Path(str(scope.project_ref or cwd)).expanduser()
    if not root.is_absolute():
        root = cwd
    try:
        root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        root = cwd
    if root.is_file():
        root = root.parent
    if not root.is_dir():
        root = cwd
    return root, scope


def _has_active_binding(workspace: Path, context: Mapping[str, Any]) -> bool:
    """Refresh only for current host identity's authoritative active binding."""

    agent = str(context.get("agent_instance_id") or "").strip()
    group = str(context.get("share_group_id") or "").strip()
    if not agent or not group:
        return False
    try:
        from ..runtime_v2.group_native import GroupControlService

        binding = GroupControlService(workspace, write=False).active_binding_for_agent(agent)
    except Exception:
        return False
    return bool(binding and str(binding.get("share_group_id") or "") == group)


def queue_host_file_refresh(
    workspace: str | Path,
    *,
    payload: Mapping[str, Any] | None = None,
    tool_name: str = "",
    tool_input: Any = None,
    tool_result: Any = None,
    host_event: str = "",
    trusted_host: bool = False,
) -> dict[str, Any]:
    """Queue and drain incremental refresh after a successful host file mutation."""
    from ..host_hooks import _coerce_tool_result_status

    success, _reason = _coerce_tool_result_status(tool_result)
    if trusted_host is not True or host_event != "post_tool":
        return {"status": "ignored", "reason": "untrusted_host_event"}
    if success is not True:
        return {"status": "ignored", "reason": "tool_not_confirmed"}
    if not is_file_mutation_tool(tool_name):
        return {"status": "ignored", "reason": "non_file_tool"}
    root = Path(workspace).expanduser().resolve()
    db_path = root / ".memoryguard" / "codegraph" / "codegraph.db"
    if not db_path.is_file():
        return {"status": "ignored", "reason": "codegraph_project_not_built"}
    store = CodeGraphStore(root, initialize=False)
    cwd = _payload_cwd(payload) or Path(str((payload or {}).get("cwd") or "") or root)
    try:
        cwd = cwd.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return {"status": "ignored", "reason": "cwd_invalid"}
    context = {
        "share_group_id": str((payload or {}).get("share_group_id") or ""),
        "agent_instance_id": str((payload or {}).get("agent_instance_id") or ""),
        "provider": str((payload or {}).get("provider") or "graphify"),
        "runtime_role": "",
    }
    if not _has_active_binding(root, context):
        return {"status": "ignored", "reason": "binding_unavailable"}
    located = _source_root_for_cwd(store, cwd, context)
    if located is None:
        return {"status": "ignored", "reason": "codegraph_project_not_built"}
    source_root, scope = located
    from .automation import policy
    automation = policy(root, scope.share_group_id, scope.project_ref)
    if not automation["enabled"]:
        return {"status": "ignored", "reason": automation.get("error", "codegraph_automation_disabled")}
    raw_paths = _collect_payload_paths(payload, tool_input)
    relative_paths: list[str] = []
    for raw in raw_paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            continue
        try:
            resolved = candidate.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        relative = _relative_to_root(source_root, resolved)
        if relative is None or _is_ignored(relative):
            continue
        suffix = Path(relative).suffix.lower()
        if suffix not in CODE_EXTENSIONS:
            # Non-source mutations and deletions never enter CodeGraph queue.
            continue
        relative_paths.append(normalize_relative_path(relative))
    if not relative_paths:
        return {"status": "ignored", "reason": "no_in_scope_paths"}
    pending = store.enqueue_refresh_paths(
        list(dict.fromkeys(relative_paths))[:_MAX_REFRESH_PATHS],
        scope=scope,
    )
    return drain_refresh_queue(store, scope=scope, source_root=source_root, queued=pending)


def drain_refresh_queue(
    store: CodeGraphStore,
    *,
    scope: CodeGraphScope,
    source_root: Path,
    queued: Iterable[str] | None = None,
) -> dict[str, Any]:
    lock = _lock_for(str(store.db_path) + ":" + store._scope_id(scope))
    if not lock.acquire(blocking=False):
        return {"status": "queued", "reason": "debounce"}
    paths: list[str] = []
    try:
        # One immediate follow-up batch captures writes that arrived while the
        # first bounded export ran.  More arrivals stay durable for the next
        # lifecycle event; no hook call can spin indefinitely.
        result: dict[str, Any] | None = None
        for batch in range(2):
            paths = list(store.drain_refresh_queue(scope=scope))
            if not paths and batch == 0:
                paths = list(queued or ())
            if not paths:
                break
            result = apply_incremental_refresh(
                store,
                scope=scope,
                source_root=source_root,
                relative_paths=paths,
            )
            paths = []
        if result is None:
            return {"status": "noop", "reason": "empty_queue"}
        return result
    except Exception:
        if paths:
            store.enqueue_refresh_paths(paths, scope=scope)
        raise
    finally:
        lock.release()


def apply_incremental_refresh(
    store: CodeGraphStore,
    *,
    scope: CodeGraphScope,
    source_root: Path,
    relative_paths: Iterable[str],
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Re-export/project only changed files; roll back graph + fingerprints together."""
    root = Path(source_root).expanduser().resolve()
    changed_files: list[Path] = []
    changed_paths: list[str] = []
    fingerprints: list[dict[str, Any]] = []
    deleted: list[str] = []
    unchanged = 0
    candidate_paths = list(dict.fromkeys(
        normalize_relative_path(relative)
        for relative in relative_paths
    ))[:_MAX_REFRESH_PATHS]
    for normalized in candidate_paths:
        absolute = (root / Path(normalized.replace("/", os.sep))).resolve()
        try:
            absolute.relative_to(root)
        except ValueError:
            continue
        if _is_ignored(normalized):
            continue
        suffix = Path(normalized).suffix.lower()
        if suffix not in CODE_EXTENSIONS:
            continue
        if not absolute.exists():
            deleted.append(normalized)
            continue
        if not absolute.is_file():
            continue
        if provenance_for_path(normalized) != "production":
            continue
        try:
            if int(absolute.stat().st_size) > int(_MAX_FILE_BYTES):
                continue
        except OSError:
            continue
        current = _stat_fingerprint(absolute)
        current["path"] = normalized
        previous = store.get_fingerprint(normalized, scope=scope)
        prior_hash = str((previous or {}).get("content_hash") or "")
        if not prior_hash:
            existing_files = [
                item for item in store.list_source_files(scope=scope)
                if item.path == normalized and item.active
            ]
            if existing_files:
                prior_hash = existing_files[0].content_hash
        if prior_hash and prior_hash == current["content_hash"]:
            fingerprints.append(current)
            unchanged += 1
            continue
        changed_files.append(absolute)
        changed_paths.append(normalized)
        fingerprints.append(current)
    if fail_at == "before_project":
        raise RuntimeError("injected codegraph refresh failure")
    if not changed_files and not deleted:
        if fingerprints:
            with store._write_transaction(scope) as (conn, checked, _scope_id, now):
                for row in fingerprints:
                    store._upsert_fingerprint_conn(
                        conn,
                        checked,
                        path=str(row["path"]),
                        mtime_ns=int(row["mtime_ns"]),
                        size=int(row["size"]),
                        content_hash=str(row["content_hash"]),
                        now=now,
                    )
        return {
            "status": "noop",
            "reason": "unchanged_content",
            "unchanged": unchanged,
            "revision_advanced": False,
        }
    # Capture bounded impact from the pre-change graph.  It is passed into
    # Graphify projection so graph, outbox, fingerprint, and one-shot receipt
    # share one transaction.  Bootstrap never re-runs an affected traversal.
    start_ids: list[str] = []
    result_ids: list[str] = []
    for path in list(dict.fromkeys([*changed_paths, *deleted]))[:_MAX_REFRESH_PATHS]:
        try:
            source = next(
                (
                    item for item in store.list_source_files(scope=scope)
                    if item.path == normalize_relative_path(path) and item.active
                ),
                None,
            )
        except Exception:
            source = None
        if source is None:
            continue
        for symbol in store.get_symbols(source.file_id, scope=scope)[:8]:
            if symbol.symbol_id not in start_ids:
                start_ids.append(symbol.symbol_id)
    seen: set[str] = set()
    for start_id in start_ids[:8]:
        try:
            query = store.affected_query(
                start_id,
                scope=scope,
                depth=AFFECTED_DEPTH,
                limit=AFFECTED_LIMIT,
                provenance=AFFECTED_PROVENANCE,
            )
        except Exception:
            continue
        for symbol_id in query.result_ids:
            if symbol_id not in seen:
                seen.add(symbol_id)
                result_ids.append(symbol_id)
            if len(result_ids) >= AFFECTED_LIMIT:
                break
        if len(result_ids) >= AFFECTED_LIMIT:
            break
    affected_receipt = {
        "start_ids": start_ids[:8],
        "result_ids": result_ids[:AFFECTED_LIMIT],
        "depth": AFFECTED_DEPTH,
        "limit": AFFECTED_LIMIT,
        "provenance": AFFECTED_PROVENANCE,
    }

    export = None
    if changed_files:
        related_paths = store.related_source_paths(
            changed_paths,
            scope=scope,
            limit=_MAX_RELATED_FILES,
        )
        export_paths = list(changed_files)
        for relative in related_paths:
            candidate = (root / Path(relative.replace("/", os.sep))).resolve()
            if candidate.is_file() and candidate not in export_paths:
                export_paths.append(candidate)
        export = export_repository(root, paths=export_paths, complete=False, parallel=False)
    else:
        export = {
            "format": "memoryguard-graphify-metadata-v1",
            "complete": False,
            "graphify_version": "",
            "source_digest": "",
            "files": [],
            "nodes": [],
            "edges": [],
        }
    if export.get("files"):
        imported = GraphifyExportAdapter(store).project(
            export,
            scope=scope,
            full_snapshot=False,
            extra_tombstones=deleted,
            fingerprints=fingerprints,
            affected_receipt=affected_receipt,
        )
        return {
            "status": "updated",
            "changed": len(changed_files),
            "deleted": len(deleted),
            "unchanged": unchanged,
            "revision_advanced": True,
            "receipt": "pending_once",
            "counts": imported.counts,
        }
    with store._write_transaction(scope) as (conn, checked, _scope_id, now):
        for path in deleted:
            store._tombstone_source_file_conn(conn, checked, path, reason="incremental_removed", now=now)
            try:
                store._delete_fingerprint_conn(conn, checked, path)
            except Exception:
                pass
        receipt = store._put_affected_receipt_conn(
            conn,
            checked,
            start_ids=affected_receipt["start_ids"],
            result_ids=affected_receipt["result_ids"],
            depth=int(affected_receipt["depth"]),
            limit=int(affected_receipt["limit"]),
            provenance=str(affected_receipt["provenance"]),
            now=now,
        )
    return {
        "status": "updated",
        "changed": 0,
        "deleted": len(deleted),
        "unchanged": unchanged,
        "revision_advanced": False,
        "receipt_id": receipt["receipt_id"],
    }
