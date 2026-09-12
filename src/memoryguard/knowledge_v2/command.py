"""Native V2 command service for Knowledge Library mutations.

The Content Plane is the only store that owns source bodies.  The knowledge
SQLite database stores book/document metadata and references only; it never
receives document text.  Long-running ingestion/rebuild work is coordinated by
RuntimeStore through :class:`TaskCoordinator` so GUI reloads recover durable
status and cancellation has one owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterable, Mapping

from ..content.store import ContentReadScope, ContentStore, stable_id
from ..governance_v2 import GovernanceV2, V2MutationContext
from ..knowledge_parser import CODE_EXTENSIONS, SUPPORTED_EXTENSIONS, parse_content
from ..memory.store import MemoryAtom, MemoryAtomStore
from ..runtime_v2.task_coordinator import TaskCoordinator, TaskExecution
from ..runtime_v2.working_memory import RuntimeScope
from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import initialize_database
from ..storage.transaction import transaction
from .service import (
    KNOWLEDGE_CANDIDATE_META,
    KNOWLEDGE_CANDIDATE_SCHEMA,
    KNOWLEDGE_CANDIDATE_SCHEMA_VERSION,
    KNOWLEDGE_CANDIDATE_TABLE,
)


_ALLOWED_SETTINGS = frozenset(
    {
        "remote_embedding_allowed",
        "remote_query_embedding_allowed",
        "auto_extract_memory",
        "vector_enabled",
    }
)
_SUPPORTED_EXTENSIONS = frozenset(SUPPORTED_EXTENSIONS) | frozenset(CODE_EXTENSIONS)


class KnowledgeV2CommandError(RuntimeError):
    """Stable command failure that is safe to cross GUI/native boundaries."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "knowledge_command_failed")
        super().__init__(self.code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: object) -> str:
    payload = "\x1f".join(str(item) for item in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if name.casefold() in {"body", "text", "content", "raw", "payload", "source_body", "document_body"}:
            raise KnowledgeV2CommandError("knowledge_metadata_body_forbidden")
        if isinstance(item, Mapping):
            result[name] = _metadata(item)
        elif isinstance(item, (list, tuple)):
            clean: list[Any] = []
            for child in item:
                if isinstance(child, Mapping):
                    clean.append(_metadata(child))
                elif isinstance(child, (str, int, float, bool)) or child is None:
                    clean.append(child)
                else:
                    raise KnowledgeV2CommandError("knowledge_metadata_invalid")
            result[name] = clean
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[name] = item
        else:
            raise KnowledgeV2CommandError("knowledge_metadata_invalid")
    if len(_json(result).encode("utf-8")) > 64 * 1024:
        raise KnowledgeV2CommandError("knowledge_metadata_too_large")
    return result


def _scope_digest(scope: ContentReadScope) -> str:
    return _digest(
        scope.namespace_id,
        scope.workspace_id,
        scope.agent_instance_id,
        scope.project_ref,
        scope.provider,
        scope.share_group_id,
        scope.sensitivity,
        scope.policy_class,
    )


def _assert_no_reparse(path: Path) -> None:
    current = Path(os.path.abspath(os.fspath(path.expanduser())))
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise KnowledgeV2CommandError("knowledge_source_unavailable") from exc
        if exists:
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise KnowledgeV2CommandError("knowledge_source_unavailable") from exc
            if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise KnowledgeV2CommandError("knowledge_source_reparse_forbidden")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _safe_source(value: Any) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise KnowledgeV2CommandError("knowledge_source_path_required")
    text = os.fspath(value).strip()
    if not text or "\x00" in text:
        raise KnowledgeV2CommandError("knowledge_source_path_required")
    path = Path(text).expanduser()
    _assert_no_reparse(path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise KnowledgeV2CommandError("knowledge_source_not_found") from exc
    if not (resolved.is_file() or resolved.is_dir()):
        raise KnowledgeV2CommandError("knowledge_source_not_supported")
    return resolved


def _iter_source_files(path: Path) -> tuple[Path, Path, tuple[Path, ...]]:
    if path.is_file():
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            raise KnowledgeV2CommandError("knowledge_source_type_unsupported")
        return path.parent, path, (path,)
    root = path
    files: list[Path] = []
    try:
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                continue
            _assert_no_reparse(candidate)
            files.append(candidate.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise KnowledgeV2CommandError("knowledge_source_scan_failed") from exc
    files.sort(key=lambda item: item.relative_to(root).as_posix().casefold())
    return root, root, tuple(files)


def _media_type(path: Path) -> str:
    return str(SUPPORTED_EXTENSIONS.get(path.suffix.lower()) or CODE_EXTENSIONS.get(path.suffix.lower()) or "")


@dataclass(frozen=True)
class _Asset:
    asset_id: str
    title: str
    source_ref: str
    status: str
    metadata: Mapping[str, Any]
    updated_at: str


class KnowledgeV2CommandService:
    """V2-only Knowledge mutations backed by ContentStore and RuntimeStore."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        tasks: TaskCoordinator | None = None,
    ) -> None:
        self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self._owns_tasks = tasks is None
        self.tasks = tasks or TaskCoordinator(self.workspace)

    def close(self) -> None:
        if self._owns_tasks:
            self.tasks.shutdown(timeout=5.0)

    @property
    def knowledge_db(self) -> Path:
        return self.layout.knowledge_db

    def _ensure_schema(self) -> None:
        self.layout.ensure_dirs()
        initialize_database(self.knowledge_db, "knowledge", layout=self.layout)
        with open_database(self.knowledge_db) as conn:
            with transaction(conn):
                from ..storage.database import execute_sql_script

                execute_sql_script(conn, KNOWLEDGE_CANDIDATE_SCHEMA)
                conn.execute(
                    f"INSERT INTO {KNOWLEDGE_CANDIDATE_META}(key,value) VALUES('version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(KNOWLEDGE_CANDIDATE_SCHEMA_VERSION),),
                )

    @staticmethod
    def _runtime_scope(scope: ContentReadScope, context: Mapping[str, Any]) -> RuntimeScope:
        return RuntimeScope(
            workspace_id=scope.workspace_id,
            agent_instance_id=scope.agent_instance_id,
            project_ref=scope.project_ref,
            share_group_id=scope.share_group_id,
            provider=scope.provider,
            runtime_scope=str(context.get("runtime_role") or "gui"),
        )

    def _asset(self, asset_id: str, scope: ContentReadScope, *, include_deleted: bool = True) -> _Asset | None:
        if not self.knowledge_db.is_file():
            return None
        with open_database(self.knowledge_db, readonly=True) as conn:
            row = conn.execute(
                "SELECT asset_id,title,source_ref,status,metadata_json,updated_at FROM knowledge_assets WHERE asset_id=?",
                (str(asset_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            meta = json.loads(str(row[4] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        if not isinstance(meta, Mapping) or str(meta.get("scope_digest") or "") != _scope_digest(scope):
            return None
        status = str(row[3] or "")
        if not include_deleted and status != "active":
            return None
        return _Asset(str(row[0]), str(row[1] or ""), str(row[2] or ""), status, dict(meta), str(row[5] or ""))

    def _documents(self, asset_id: str) -> tuple[dict[str, Any], ...]:
        if not self.knowledge_db.is_file():
            return ()
        with open_database(self.knowledge_db, readonly=True) as conn:
            rows = conn.execute(
                "SELECT document_id,path,title,status,metadata_json FROM knowledge_documents WHERE asset_id=? ORDER BY path,document_id",
                (str(asset_id),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                meta = json.loads(str(row[4] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                meta = {}
            result.append(
                {
                    "document_id": str(row[0]),
                    "path": str(row[1]),
                    "title": str(row[2] or ""),
                    "status": str(row[3] or ""),
                    "metadata": dict(meta) if isinstance(meta, Mapping) else {},
                }
            )
        return tuple(result)

    def _upsert_asset(
        self,
        *,
        asset_id: str,
        title: str,
        source_ref: str,
        scope: ContentReadScope,
        metadata: Mapping[str, Any],
        status: str = "active",
    ) -> None:
        self._ensure_schema()
        now = _now()
        clean = _metadata({**dict(metadata), "scope_digest": _scope_digest(scope), "namespace_id": scope.namespace_id})
        with open_database(self.knowledge_db) as conn:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO knowledge_assets(asset_id,asset_type,title,source_ref,status,policy_class,metadata_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET title=excluded.title,source_ref=excluded.source_ref,status=excluded.status,policy_class=excluded.policy_class,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (asset_id, "source", title, source_ref, status, scope.policy_class, _json(clean), now, now),
                )

    def _ingest(
        self,
        source: Path,
        *,
        title: str,
        scope: ContentReadScope,
        execution: TaskExecution,
        asset_id: str | None = None,
    ) -> Mapping[str, Any]:
        root, source_ref_path, files = _iter_source_files(source)
        scope_key = _scope_digest(scope)
        asset_id = asset_id or stable_id("knowledge-asset", scope_key, str(source_ref_path))
        source_id = stable_id("knowledge-source", scope_key, str(source_ref_path))
        previous = {item["path"]: item for item in self._documents(asset_id)}
        content = ContentStore(
            self.workspace,
            workspace_id=scope.workspace_id,
            trust_domain="knowledge",
            sensitivity=scope.sensitivity,
            retention_authority="knowledge",
        )
        namespace = content.ensure_namespace(
            namespace_id=scope.namespace_id,
            workspace_id=scope.workspace_id,
            trust_domain="knowledge",
            sensitivity=scope.sensitivity,
            retention_authority="knowledge",
        )
        content.upsert_source_connector(
            source_id=source_id,
            provider=scope.provider,
            source_type="file" if source.is_file() else "directory",
            external_root_key=str(source_ref_path),
            workspace_id=scope.workspace_id,
            enabled=True,
        )
        self._upsert_asset(
            asset_id=asset_id,
            title=(title.strip() if isinstance(title, str) and title.strip() else source_ref_path.name),
            source_ref=str(source_ref_path),
            scope=scope,
            metadata={"source_id": source_id, "settings": dict((self._asset(asset_id, scope) or _Asset("", "", "", "", {}, "")).metadata.get("settings") or {})},
        )

        total = len(files)
        seen_paths: set[str] = set()
        written_occurrences = 0
        skipped = 0
        for index, file_path in enumerate(files):
            execution.check_cancelled()
            relative = file_path.relative_to(root).as_posix()
            if source.is_file():
                relative = file_path.name
            seen_paths.add(relative)
            try:
                raw = file_path.read_bytes()
            except OSError as exc:
                raise KnowledgeV2CommandError("knowledge_source_read_failed") from exc
            execution.check_cancelled()
            media_type = _media_type(file_path)
            parsed = parse_content(raw, relative_path=relative, media_type=media_type)
            if parsed is None:
                skipped += 1
                continue
            file_hash = hashlib.sha256(raw).hexdigest()
            object_id = stable_id("knowledge-object", source_id, relative)
            occurrence_ids: list[str] = []
            blocks = [block for block in parsed.blocks if str(block.text or "").strip()]
            with open_database(content.db_path) as conn:
                with transaction(conn):
                    for ordinal, block in enumerate(blocks):
                        execution.check_cancelled()
                        occurrence_id = content.upsert_occurrence(
                            source_object_id=object_id,
                            occurrence_key=f"block:{ordinal}",
                            text=block.text,
                            namespace_id=namespace.namespace_id,
                            source_id=source_id,
                            source_kind="knowledge",
                            external_object_key=relative,
                            object_type="document_block",
                            source_revision=file_hash,
                            ordinal=ordinal,
                            locator={
                                "title": file_path.stem,
                                "section": str(block.heading_text or "")[:240],
                                "line_start": int(block.line_start),
                                "line_end": int(block.line_end),
                                "block_type": str(block.block_type or "paragraph")[:64],
                            },
                            content_role="knowledge",
                            sensitivity=scope.sensitivity,
                            workspace_id=scope.workspace_id,
                            agent_instance_id=scope.agent_instance_id,
                            project_ref=scope.project_ref,
                            share_group_id=scope.share_group_id,
                            policy_class=scope.policy_class,
                            provider=scope.provider,
                            access_scope={"namespace_id": scope.namespace_id},
                            active=True,
                            conn=conn,
                        )
                        occurrence_ids.append(occurrence_id)
                    old_ids = [str(item) for item in (previous.get(relative, {}).get("metadata", {}).get("occurrence_ids") or [])]
                    for old_id in old_ids:
                        if old_id not in occurrence_ids:
                            content.tombstone_occurrence(old_id, reason="knowledge_reingest_removed_block", scan_id=execution.run_id, conn=conn)
            written_occurrences += len(occurrence_ids)
            document_id = stable_id("knowledge-document", asset_id, relative)
            metadata = _metadata(
                {
                    "content_hash": file_hash,
                    "occurrence_ids": occurrence_ids,
                    "source_id": source_id,
                    "media_type": media_type,
                    "scope_digest": scope_key,
                }
            )
            now = _now()
            with open_database(self.knowledge_db) as conn:
                with transaction(conn):
                    conn.execute(
                        "INSERT INTO knowledge_documents(document_id,asset_id,path,title,status,metadata_json,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(asset_id,path) DO UPDATE SET title=excluded.title,status='active',metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                        (document_id, asset_id, relative, file_path.name, "active", _json(metadata), now, now),
                    )
            execution.progress(
                int(((index + 1) / max(1, total)) * 90),
                "ingesting",
                item_count=index + 1,
            )

        # Files deleted from the source are tombstoned, not physically deleted.
        for relative, old in previous.items():
            if relative in seen_paths or str(old.get("status") or "") != "active":
                continue
            execution.check_cancelled()
            old_ids = [str(item) for item in (old.get("metadata", {}).get("occurrence_ids") or [])]
            with open_database(content.db_path) as conn:
                with transaction(conn):
                    for occurrence_id in old_ids:
                        content.tombstone_occurrence(
                            occurrence_id,
                            reason="knowledge_source_file_deleted",
                            scan_id=execution.run_id,
                            conn=conn,
                        )
            with open_database(self.knowledge_db) as conn:
                with transaction(conn):
                    conn.execute("UPDATE knowledge_documents SET status='deleted',updated_at=? WHERE document_id=?", (_now(), old["document_id"]))

        execution.progress(95, "projecting", item_count=total)
        self._rebuild_reference_projection(asset_id, scope)
        return {
            "asset_id": asset_id,
            "book_id": asset_id,
            "file_count": total,
            "occurrence_count": written_occurrences,
            "skipped": skipped,
            "source_digest": _digest(source_id, *(item["metadata"].get("content_hash", "") for item in self._documents(asset_id))),
        }

    def _rebuild_reference_projection(self, asset_id: str, scope: ContentReadScope) -> dict[str, Any]:
        content = ContentStore(self.workspace, initialize=False)
        documents = self._documents(asset_id)
        active_refs: set[str] = set()
        with open_database(content.db_path) as conn:
            with transaction(conn):
                for document in documents:
                    if document["status"] != "active":
                        continue
                    for occurrence_id in document["metadata"].get("occurrence_ids") or []:
                        occurrence_id = str(occurrence_id)
                        if not occurrence_id:
                            continue
                        active_refs.add(occurrence_id)
                        row = conn.execute(
                            "SELECT blob_id FROM content_occurrences WHERE occurrence_id=? AND active=1",
                            (occurrence_id,),
                        ).fetchone()
                        if row is None:
                            continue
                        conn.execute(
                            "INSERT INTO knowledge_records(record_id,source_table,source_pk,record_type,content_blob_id,status,derived_status,metadata_json) "
                            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_table,source_pk,record_type) DO UPDATE SET content_blob_id=excluded.content_blob_id,status='active',derived_status=excluded.derived_status,metadata_json=excluded.metadata_json",
                            (
                                stable_id("knowledge-record", asset_id, occurrence_id),
                                "knowledge_documents",
                                occurrence_id,
                                "knowledge_reference",
                                str(row[0] or ""),
                                "active",
                                "CANONICAL",
                                _json({"asset_id": asset_id, "scope_digest": _scope_digest(scope)}),
                            ),
                        )
                rows = conn.execute(
                    "SELECT record_id,source_pk FROM knowledge_records WHERE record_type='knowledge_reference' AND metadata_json LIKE ?",
                    (f'%"asset_id":"{asset_id}"%',),
                ).fetchall()
                for row in rows:
                    if str(row[1]) not in active_refs:
                        conn.execute("UPDATE knowledge_records SET status='deleted',derived_status='STALE' WHERE record_id=?", (str(row[0]),))
        return {"asset_id": asset_id, "reference_count": len(active_refs)}

    def add(self, payload: Mapping[str, Any], *, scope: ContentReadScope, context: Mapping[str, Any]) -> dict[str, Any]:
        source = _safe_source(payload.get("path"))
        title = str(payload.get("title") or "").strip()
        task_scope = self._runtime_scope(scope, context)
        key = str(payload.get("idempotency_key") or f"knowledge-add:{_scope_digest(scope)}:{source}:{title}").strip()
        result = self.tasks.start_scope_exclusive(
            operation="knowledge_source_add",
            key=key,
            scope=task_scope,
            worker=lambda execution: self._ingest(source, title=title, scope=scope, execution=execution),
            request={"path": source, "title": title},
        )
        result["operation"] = "knowledge_source_add"
        return result

    def reingest(self, payload: Mapping[str, Any], *, scope: ContentReadScope, context: Mapping[str, Any]) -> dict[str, Any]:
        book_id = str(payload.get("book_id") or "").strip()
        asset = self._asset(book_id, scope, include_deleted=False)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_book_not_found")
        source = _safe_source(asset.source_ref)
        task_scope = self._runtime_scope(scope, context)
        key = str(payload.get("idempotency_key") or f"knowledge-reingest:{book_id}:{asset.updated_at}").strip()
        result = self.tasks.start_scope_exclusive(
            operation="knowledge_reingest",
            key=key,
            scope=task_scope,
            worker=lambda execution: self._ingest(source, title=asset.title, scope=scope, execution=execution, asset_id=book_id),
            request={"book_id": book_id, "source": source, "asset_updated_at": asset.updated_at},
        )
        result["operation"] = "knowledge_reingest"
        return result

    def rebuild(self, payload: Mapping[str, Any], *, scope: ContentReadScope, context: Mapping[str, Any]) -> dict[str, Any]:
        book_id = str(payload.get("book_id") or "").strip()
        asset = self._asset(book_id, scope, include_deleted=False)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_book_not_found")
        task_scope = self._runtime_scope(scope, context)
        key = str(payload.get("idempotency_key") or f"knowledge-rebuild:{book_id}:{asset.updated_at}").strip()

        def worker(execution: TaskExecution) -> Mapping[str, Any]:
            execution.progress(20, "reading_references")
            execution.check_cancelled()
            result = self._rebuild_reference_projection(book_id, scope)
            execution.progress(90, "reference_projection_complete")
            metadata = dict(asset.metadata)
            metadata["index_generation"] = int(metadata.get("index_generation") or 0) + 1
            metadata["last_rebuild_digest"] = _digest(book_id, result["reference_count"], metadata["index_generation"])
            self._upsert_asset(
                asset_id=book_id,
                title=asset.title,
                source_ref=asset.source_ref,
                scope=scope,
                metadata=metadata,
            )
            return result

        result = self.tasks.start_scope_exclusive(
            operation="knowledge_rebuild_smart",
            key=key,
            scope=task_scope,
            worker=worker,
            request={"book_id": book_id, "asset_updated_at": asset.updated_at},
        )
        result["operation"] = "knowledge_rebuild_smart"
        return result

    def remove(self, payload: Mapping[str, Any], *, scope: ContentReadScope) -> dict[str, Any]:
        book_id = str(payload.get("book_id") or "").strip()
        asset = self._asset(book_id, scope, include_deleted=False)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_book_not_found")
        content = ContentStore(self.workspace, initialize=False)
        tombstones: list[str] = []
        for document in self._documents(book_id):
            if document["status"] != "active":
                continue
            for occurrence_id in document["metadata"].get("occurrence_ids") or []:
                tombstones.append(content.tombstone_occurrence(str(occurrence_id), reason="knowledge_book_deleted", scan_id=book_id))
        deletion_id = stable_id("knowledge-deletion", book_id, asset.updated_at)
        meta = dict(asset.metadata)
        meta.update({"deletion_id": deletion_id, "tombstone_ids": tombstones, "deleted_at": _now()})
        self._upsert_asset(asset_id=book_id, title=asset.title, source_ref=asset.source_ref, scope=scope, metadata=meta, status="deleted")
        with open_database(self.knowledge_db) as conn:
            with transaction(conn):
                conn.execute("UPDATE knowledge_documents SET status='deleted',updated_at=? WHERE asset_id=?", (_now(), book_id))
        return {"ok": True, "status": "succeeded", "operation": "knowledge_remove", "data": {"book_id": book_id, "deletion_id": deletion_id, "tombstones": len(tombstones)}, "receipt": {"deletion_id": deletion_id}}

    def _asset_for_deletion(self, deletion_id: str, scope: ContentReadScope) -> _Asset | None:
        if not self.knowledge_db.is_file():
            return None
        with open_database(self.knowledge_db, readonly=True) as conn:
            rows = conn.execute("SELECT asset_id FROM knowledge_assets WHERE status='deleted' ORDER BY asset_id").fetchall()
        for row in rows:
            asset = self._asset(str(row[0]), scope, include_deleted=True)
            if asset is not None and str(asset.metadata.get("deletion_id") or "") == deletion_id:
                return asset
        return None

    def restore(self, payload: Mapping[str, Any], *, scope: ContentReadScope) -> dict[str, Any]:
        deletion_id = str(payload.get("deletion_id") or "").strip()
        asset = self._asset_for_deletion(deletion_id, scope)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_deletion_not_found")
        content = ContentStore(self.workspace, initialize=False)
        restored = 0
        for tombstone_id in asset.metadata.get("tombstone_ids") or []:
            result = content.restore_tombstone(str(tombstone_id))
            restored += int(result.get("restored_occurrence", 0))
        meta = dict(asset.metadata)
        meta.pop("deletion_id", None)
        meta.pop("tombstone_ids", None)
        meta.pop("deleted_at", None)
        meta["restored_at"] = _now()
        self._upsert_asset(asset_id=asset.asset_id, title=asset.title, source_ref=asset.source_ref, scope=scope, metadata=meta, status="active")
        with open_database(self.knowledge_db) as conn:
            with transaction(conn):
                conn.execute("UPDATE knowledge_documents SET status='active',updated_at=? WHERE asset_id=?", (_now(), asset.asset_id))
        self._rebuild_reference_projection(asset.asset_id, scope)
        return {"ok": True, "status": "succeeded", "operation": "knowledge_restore", "data": {"book_id": asset.asset_id, "restored": restored}, "receipt": {"deletion_id": deletion_id}}

    def purge(self, payload: Mapping[str, Any], *, scope: ContentReadScope) -> dict[str, Any]:
        deletion_id = str(payload.get("deletion_id") or "").strip()
        asset = self._asset_for_deletion(deletion_id, scope)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_deletion_not_found")
        content = ContentStore(self.workspace, initialize=False)
        purged = 0
        for tombstone_id in asset.metadata.get("tombstone_ids") or []:
            result = content.purge_tombstone(str(tombstone_id))
            purged += int(result.get("released_holds", 0))
        with open_database(self.knowledge_db) as conn:
            with transaction(conn):
                conn.execute("UPDATE knowledge_assets SET status='purged',updated_at=? WHERE asset_id=?", (_now(), asset.asset_id))
                conn.execute("UPDATE knowledge_documents SET status='purged',updated_at=? WHERE asset_id=?", (_now(), asset.asset_id))
        return {"ok": True, "status": "succeeded", "operation": "knowledge_purge_deleted", "data": {"book_id": asset.asset_id, "released_holds": purged}, "receipt": {"deletion_id": deletion_id}}

    def update_settings(self, payload: Mapping[str, Any], *, scope: ContentReadScope) -> dict[str, Any]:
        book_id = str(payload.get("book_id") or "").strip()
        settings = payload.get("settings")
        if not isinstance(settings, Mapping):
            raise KnowledgeV2CommandError("knowledge_settings_required")
        unknown = set(str(key) for key in settings) - _ALLOWED_SETTINGS
        if unknown:
            raise KnowledgeV2CommandError("knowledge_settings_unknown")
        asset = self._asset(book_id, scope, include_deleted=False)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_book_not_found")
        clean_settings = {str(key): bool(value) for key, value in settings.items()}
        meta = dict(asset.metadata)
        meta["settings"] = {**dict(meta.get("settings") or {}), **clean_settings}
        self._upsert_asset(asset_id=asset.asset_id, title=asset.title, source_ref=asset.source_ref, scope=scope, metadata=meta)
        return {"ok": True, "status": "succeeded", "operation": "knowledge_update_settings", "data": {"book_id": book_id, "settings": clean_settings}, "receipt": {}}

    def list_books(self, *, scope: ContentReadScope) -> dict[str, Any]:
        """Return scoped Knowledge book metadata without source bodies/absolute paths."""
        if not self.knowledge_db.is_file():
            return {"ok": True, "status": "succeeded", "operation": "knowledge_list", "data": {"books": [], "total": 0}}
        scope_key = _scope_digest(scope)
        with open_database(self.knowledge_db, readonly=True) as conn:
            rows = conn.execute(
                "SELECT asset_id,title,status,metadata_json,updated_at FROM knowledge_assets "
                "WHERE status='active' ORDER BY title,asset_id"
            ).fetchall()
        books: list[dict[str, Any]] = []
        for row in rows:
            try:
                meta = json.loads(str(row[3] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                meta = {}
            if not isinstance(meta, Mapping) or str(meta.get("scope_digest") or "") != scope_key:
                continue
            documents = self._documents(str(row[0]))
            active_docs = [item for item in documents if item["status"] == "active"]
            occurrence_count = sum(
                len(item["metadata"].get("occurrence_ids") or [])
                for item in active_docs
            )
            settings = dict(meta.get("settings") or {}) if isinstance(meta.get("settings"), Mapping) else {}
            books.append({
                "book_id": str(row[0]),
                "title": str(row[1] or ""),
                "status": str(row[2] or "active"),
                "file_count": len(active_docs),
                "chunk_count": occurrence_count,
                "chapter_count": 0,
                "index_generation": int(meta.get("index_generation") or 0),
                "settings": {key: bool(settings.get(key, False)) for key in sorted(_ALLOWED_SETTINGS)},
                "updated_at": str(row[4] or ""),
            })
        return {"ok": True, "status": "succeeded", "operation": "knowledge_list", "data": {"books": books, "total": len(books)}}

    def book_info(self, book_id: str, *, scope: ContentReadScope) -> dict[str, Any]:
        asset = self._asset(str(book_id or "").strip(), scope, include_deleted=False)
        if asset is None:
            raise KnowledgeV2CommandError("knowledge_book_not_found")
        documents = self._documents(asset.asset_id)
        docs: list[dict[str, Any]] = []
        occurrence_count = 0
        for item in documents:
            if item["status"] != "active":
                continue
            ids = [str(value) for value in (item["metadata"].get("occurrence_ids") or []) if str(value)]
            occurrence_count += len(ids)
            docs.append({
                "document_id": item["document_id"],
                "relative_path": item["path"],
                "title": item["title"],
                "status": item["status"],
                "chunk_count": len(ids),
                "occurrence_ids": ids[:200],
                "content_hash": str(item["metadata"].get("content_hash") or ""),
                "media_type": str(item["metadata"].get("media_type") or ""),
            })
        settings = dict(asset.metadata.get("settings") or {}) if isinstance(asset.metadata.get("settings"), Mapping) else {}
        return {
            "ok": True,
            "status": "succeeded",
            "operation": "knowledge_book",
            "data": {
                "book_id": asset.asset_id,
                "title": asset.title,
                "status": asset.status,
                "file_count": len(docs),
                "chunk_count": occurrence_count,
                "documents": docs,
                "settings": {key: bool(settings.get(key, False)) for key in sorted(_ALLOWED_SETTINGS)},
                "updated_at": asset.updated_at,
            },
        }

    def read_occurrence(self, occurrence_id: str, *, scope: ContentReadScope) -> dict[str, Any]:
        ident = str(occurrence_id or "").strip()
        if not ident:
            raise KnowledgeV2CommandError("knowledge_occurrence_required")
        content = ContentStore(self.workspace, initialize=False)
        occurrence = content.get_occurrence(ident, scope)
        if occurrence is None:
            raise KnowledgeV2CommandError("knowledge_occurrence_not_found")
        blob = content.get_blob(occurrence.blob_id, scope)
        if blob is None:
            raise KnowledgeV2CommandError("knowledge_occurrence_not_found")
        return {
            "ok": True,
            "status": "succeeded",
            "operation": "knowledge_read",
            "data": {
                "occurrence_id": ident,
                "text": blob.text,
                "hash": blob.canonical_hash,
                "char_count": blob.char_count,
                "byte_count": blob.byte_count,
            },
        }

    def deleted(self, *, scope: ContentReadScope) -> dict[str, Any]:
        if not self.knowledge_db.is_file():
            return {"ok": True, "status": "succeeded", "operation": "knowledge_deleted_list", "data": {"items": [], "total": 0}}
        with open_database(self.knowledge_db, readonly=True) as conn:
            rows = conn.execute("SELECT asset_id FROM knowledge_assets WHERE status='deleted' ORDER BY updated_at DESC,asset_id").fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            asset = self._asset(str(row[0]), scope, include_deleted=True)
            if asset is None:
                continue
            items.append({"book_id": asset.asset_id, "title": asset.title, "deletion_id": str(asset.metadata.get("deletion_id") or ""), "deleted_at": str(asset.metadata.get("deleted_at") or "")})
        return {"ok": True, "status": "succeeded", "operation": "knowledge_deleted_list", "data": {"items": items, "total": len(items)}}

    def candidate_targets(self, *, scope: ContentReadScope) -> dict[str, Any]:
        rules_db = self.layout.rules_db
        groups: list[dict[str, Any]] = []
        if rules_db.is_file():
            try:
                with open_database(rules_db, readonly=True) as conn:
                    rows = conn.execute(
                        "SELECT share_group_id,COUNT(DISTINCT owner_agent_id) FROM rule_bindings WHERE status='active' AND share_group_id<>'' GROUP BY share_group_id ORDER BY share_group_id"
                    ).fetchall()
                for row in rows:
                    group_id = str(row[0] or "")
                    if group_id:
                        groups.append({"share_group_id": group_id, "member_count": int(row[1] or 0), "active": group_id == scope.share_group_id})
            except sqlite3.Error:
                groups = []
        if scope.share_group_id and all(item["share_group_id"] != scope.share_group_id for item in groups):
            groups.append({"share_group_id": scope.share_group_id, "member_count": 0, "active": True})
            groups.sort(key=lambda item: item["share_group_id"])
        return {"ok": True, "status": "succeeded", "operation": "knowledge_candidate_targets", "data": {"groups": groups, "total": len(groups)}}

    def review_candidate(self, payload: Mapping[str, Any], *, scope: ContentReadScope, context: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_schema()
        candidate_id = str(payload.get("candidate_id") or "").strip()
        decision = str(payload.get("decision") or "").strip().casefold()
        normalized = {"approve": "approved", "reject": "rejected", "keep": "pending"}.get(decision, decision)
        if normalized not in {"approved", "rejected", "pending"}:
            raise KnowledgeV2CommandError("knowledge_candidate_decision_invalid")
        with open_database(self.knowledge_db, readonly=True) as conn:
            row = conn.execute(f"SELECT * FROM {KNOWLEDGE_CANDIDATE_TABLE} WHERE candidate_id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KnowledgeV2CommandError("knowledge_candidate_not_found")
        row_map = dict(row)
        if any(str(row_map.get(key) or "") != str(getattr(scope, key)) for key in ("namespace_id", "workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "sensitivity", "policy_class")):
            raise KnowledgeV2CommandError("knowledge_candidate_not_found")
        if normalized in {"pending", "rejected"}:
            with open_database(self.knowledge_db) as conn:
                with transaction(conn):
                    conn.execute(f"UPDATE {KNOWLEDGE_CANDIDATE_TABLE} SET status=?,updated_at=? WHERE candidate_id=?", (normalized, _now(), candidate_id))
            return {"ok": True, "status": "succeeded", "operation": "knowledge_candidate_review", "data": {"candidate_id": candidate_id, "status": normalized, "synced_memory_id": ""}, "receipt": {}}

        occurrence_id = str(row_map.get("source_occurrence_id") or "")
        content = ContentStore(self.workspace, initialize=False)
        occurrence = content.get_occurrence(occurrence_id, scope)
        if occurrence is None:
            raise KnowledgeV2CommandError("knowledge_candidate_source_unavailable")
        blob = content.get_blob(occurrence.blob_id, scope)
        if blob is None or not blob.text:
            raise KnowledgeV2CommandError("knowledge_candidate_source_unavailable")
        target_group = str(payload.get("target_group_id") or scope.share_group_id).strip()
        if target_group != scope.share_group_id:
            raise KnowledgeV2CommandError("knowledge_candidate_target_outside_scope")
        mutation = V2MutationContext(
            workspace_id=scope.workspace_id,
            share_group_id=scope.share_group_id,
            agent_instance_id=scope.agent_instance_id,
            project_ref=scope.project_ref,
            provider=scope.provider,
            runtime_role=str(context.get("runtime_role") or "gui"),
            actor=str(context.get("agent_instance_id") or "gui"),
            admin=bool(context.get("admin") or context.get("is_admin")),
            authority="manual",
        )
        memory = MemoryAtomStore(self.workspace, readonly=False)
        governance = GovernanceV2(self.workspace, memory_store=memory)
        evidence_id = stable_id(
            "knowledge-candidate-evidence",
            candidate_id,
            occurrence_id,
            str(row_map.get("content_hash") or blob.canonical_hash),
        )
        evidence_payload = {
            "evidence_id": evidence_id,
            "evidence_type": "reference",
            "source_ref": occurrence_id,
            "revision": str(row_map.get("content_hash") or ""),
            "digest": str(row_map.get("content_hash") or blob.canonical_hash),
            "authority": "governance",
            "status": "valid",
            "metadata": {"candidate_id": candidate_id},
        }
        memory_id = "knowledge-" + _digest(candidate_id, scope.share_group_id)[:24]
        atom = MemoryAtom(
            memory_id=memory_id,
            body=blob.text,
            kind="fact",
            status="active",
            confidence=0.8,
            injection_policy="relevant",
            agent_instance_id=scope.agent_instance_id,
            share_group_id=scope.share_group_id,
            project_ref=scope.project_ref,
            provider=scope.provider,
            runtime_role=str(context.get("runtime_role") or "gui"),
            workspace_id=scope.workspace_id,
            provenance=[{"source": "knowledge_candidate", "candidate_id": candidate_id, "occurrence_id": occurrence_id}],
            metadata={"candidate_id": candidate_id, "source_occurrence_id": occurrence_id},
        )
        persisted, decision_receipt = governance.put_atom(
            atom,
            context=mutation,
            evidence=[evidence_payload],
            source_mappings=[{"source_domain": "knowledge", "source_ref": occurrence_id, "source_record_id": candidate_id, "digest": str(row_map.get("content_hash") or blob.canonical_hash)}],
            reason="knowledge candidate approved",
            confidence=1.0,
            idempotency_key=f"knowledge-candidate:{candidate_id}",
        )
        projection = memory.project_evidence(governance.evidence)
        if int(projection.get("failed", 0)) or int(projection.get("pending", 0)):
            raise KnowledgeV2CommandError("knowledge_candidate_evidence_projection_failed")
        if evidence_id not in memory.evidence_ids_for_atom(persisted.atom_id):
            raise KnowledgeV2CommandError("knowledge_candidate_evidence_projection_missing")
        memory.set_visibility("active", atom_ids=[persisted.atom_id])
        with open_database(self.knowledge_db) as conn:
            with transaction(conn):
                conn.execute(f"UPDATE {KNOWLEDGE_CANDIDATE_TABLE} SET status='approved',updated_at=? WHERE candidate_id=?", (_now(), candidate_id))
        return {"ok": True, "status": "succeeded", "operation": "knowledge_candidate_review", "data": {"candidate_id": candidate_id, "status": "approved", "synced_memory_id": persisted.memory_id}, "receipt": {"decision_id": decision_receipt.decision_id, "evidence_id": evidence_id}}

    def task_status(self, payload: Mapping[str, Any], *, scope: ContentReadScope, context: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            raise KnowledgeV2CommandError("task_run_id_required")
        return self.tasks.status(run_id, self._runtime_scope(scope, context))

    def dispatch(self, operation: str, payload: Mapping[str, Any] | None = None, *, scope: ContentReadScope, context: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload or {})
        name = str(operation or "").strip()
        if name == "knowledge_source_add":
            return self.add(data, scope=scope, context=context)
        if name == "knowledge_reingest":
            return self.reingest(data, scope=scope, context=context)
        if name == "knowledge_rebuild_smart":
            return self.rebuild(data, scope=scope, context=context)
        if name == "knowledge_remove":
            return self.remove(data, scope=scope)
        if name == "knowledge_restore":
            return self.restore(data, scope=scope)
        if name == "knowledge_purge_deleted":
            return self.purge(data, scope=scope)
        if name == "knowledge_update_settings":
            return self.update_settings(data, scope=scope)
        if name == "knowledge_candidate_review":
            return self.review_candidate(data, scope=scope, context=context)
        if name == "knowledge_list":
            return self.list_books(scope=scope)
        if name == "knowledge_book":
            return self.book_info(str(data.get("book_id") or ""), scope=scope)
        if name == "knowledge_read":
            return self.read_occurrence(str(data.get("occurrence_id") or ""), scope=scope)
        if name == "knowledge_deleted_list":
            return self.deleted(scope=scope)
        if name == "knowledge_candidate_targets":
            return self.candidate_targets(scope=scope)
        if name == "task_status":
            return self.task_status(data, scope=scope, context=context)
        raise KnowledgeV2CommandError("knowledge_operation_unknown")


__all__ = ["KnowledgeV2CommandError", "KnowledgeV2CommandService"]
