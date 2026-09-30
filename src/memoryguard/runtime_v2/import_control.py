"""V2 GUI source-preview and conversation-import orchestration.

The only durable destination for imported/raw conversation bodies is the V2
Content Plane.  This module never calls ImportAdapter.archive_history(),
ConversationHistoryStore, MemoryIR, ManagedStore, or SharedMemoryStore.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from ..adapters import ChatGPTImportAdapter, GenericImportAdapter, safe_extract_zip
from ..content.conversation_sync import ConversationEvent, ConversationSync
from ..content.store import ContentStore, stable_id
from ..data_home import resolve_cache_home
from .source_control import SourceControlError, SourceControlService
from .task_coordinator import TaskExecution


_MAX_IMPORT_FILES = 2_000
_MAX_IMPORT_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_IMPORT_FILE_BYTES = 32 * 1024 * 1024
_MAX_PREVIEW_BYTES = 256 * 1024
_MAX_MESSAGE_CHARS = 500_000
_BATCH_EVENTS = 250
_BATCH_CHARS = 900_000


class ImportControlError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "import_control_failed")
        super().__init__(self.code)


def _digest(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(item) for item in parts).encode("utf-8")).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x0400
    )


def _media_type(path: Path) -> str:
    ext = path.suffix.casefold()
    return {
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".toml": "application/toml",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }.get(ext, "text/plain")


@dataclass(frozen=True)
class _BundleInventory:
    files: tuple[Path, ...]
    total_bytes: int
    digest: str


class ImportControlService:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.sources = SourceControlService(self.workspace)

    def _scope(self, value: Mapping[str, Any]) -> dict[str, str]:
        scope = {
            "workspace_id": str(value.get("workspace_id") or ""),
            "agent_instance_id": str(value.get("agent_instance_id") or ""),
            "project_ref": str(value.get("project_ref") or ""),
            "share_group_id": str(value.get("share_group_id") or ""),
            "provider": str(value.get("provider") or ""),
            "sensitivity": str(value.get("sensitivity") or "normal"),
            "policy_class": str(value.get("policy_class") or "private"),
        }
        if not scope["workspace_id"] or not scope["agent_instance_id"] or not scope["share_group_id"]:
            raise ImportControlError("import_scope_required")
        if Path(scope["workspace_id"]).expanduser().resolve() != self.workspace:
            raise ImportControlError("import_scope_invalid")
        return scope

    @staticmethod
    def _bundle_path(raw: str | Path) -> Path:
        text = str(raw or "").strip()
        if not text:
            raise ImportControlError("import_path_required")
        path = Path(text).expanduser()
        if not path.is_absolute():
            raise ImportControlError("import_path_must_be_absolute")
        try:
            if not path.exists():
                raise ImportControlError("import_path_not_found")
            if _is_reparse(path):
                raise ImportControlError("import_reparse_point_blocked")
            return path.resolve()
        except OSError as exc:
            raise ImportControlError("import_path_unavailable") from exc

    @staticmethod
    def _inventory_bundle(path: Path) -> _BundleInventory:
        files: list[Path] = []
        total = 0
        if path.is_file():
            files = [path]
        elif path.is_dir():
            try:
                for candidate in path.rglob("*"):
                    if _is_reparse(candidate):
                        raise ImportControlError("import_reparse_point_blocked")
                    if not candidate.is_file():
                        continue
                    files.append(candidate)
                    if len(files) > _MAX_IMPORT_FILES:
                        raise ImportControlError("import_file_limit_exceeded")
            except OSError as exc:
                raise ImportControlError("import_inventory_failed") from exc
        else:
            raise ImportControlError("import_path_unsupported")
        if not files:
            raise ImportControlError("import_bundle_empty")
        digest = hashlib.sha256()
        root = path if path.is_dir() else path.parent
        for candidate in sorted(files, key=lambda item: str(item).casefold()):
            try:
                size = int(candidate.stat().st_size)
            except OSError as exc:
                raise ImportControlError("import_inventory_failed") from exc
            if size > _MAX_IMPORT_FILE_BYTES:
                raise ImportControlError("import_file_too_large")
            total += size
            if total > _MAX_IMPORT_TOTAL_BYTES:
                raise ImportControlError("import_total_size_exceeded")
            relative = candidate.relative_to(root) if path.is_dir() else Path(candidate.name)
            digest.update(str(relative).replace("\\", "/").encode("utf-8"))
            digest.update(b"\0")
            try:
                with candidate.open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
            except OSError as exc:
                raise ImportControlError("import_read_failed") from exc
        return _BundleInventory(tuple(files), total, digest.hexdigest())

    @staticmethod
    def _detect(path: Path):
        for adapter in (ChatGPTImportAdapter(), GenericImportAdapter()):
            detected = adapter.detect(path)
            if detected.supported:
                return adapter, detected
        raise ImportControlError("unsupported_bundle_format")

    def preview_bundle(self, path: str) -> dict[str, Any]:
        target = self._bundle_path(path)
        inventory = self._inventory_bundle(target)
        adapter, detected = self._detect(target)
        names: list[str] = []
        root = target if target.is_dir() else target.parent
        for candidate in inventory.files[:50]:
            relative = candidate.relative_to(root) if target.is_dir() else Path(candidate.name)
            names.append(str(relative).replace("\\", "/")[:512])
        return {
            "ok": True,
            "status": "succeeded",
            "provider": str(detected.provider),
            "confidence": float(detected.confidence),
            "notes": str(detected.notes or "")[:512],
            "inventory": {
                "file_count": len(inventory.files),
                "total_bytes": inventory.total_bytes,
                "files": names,
                "bundle_digest": inventory.digest,
            },
            "destination": "v2_content_plane",
            "writes_long_term_memory": False,
        }

    def source_memory_summary(self, context: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.sources.raw_summary(context)
        except SourceControlError as exc:
            raise ImportControlError(exc.code) from exc

    def source_content_preview(
        self,
        source_id: str,
        relative_path: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            result = self.sources.content_preview(source_id, relative_path, context)
        except SourceControlError as exc:
            raise ImportControlError(exc.code) from exc
        result["read_only"] = True
        return result

    @staticmethod
    def _event_batches(events: Sequence[ConversationEvent]) -> Iterable[list[ConversationEvent]]:
        current: list[ConversationEvent] = []
        chars = 0
        for event in events:
            size = len(str(event.content))
            if size > _MAX_MESSAGE_CHARS:
                raise ImportControlError("import_message_too_large")
            if current and (len(current) >= _BATCH_EVENTS or chars + size > _BATCH_CHARS):
                yield current
                current = []
                chars = 0
            current.append(event)
            chars += size
        if current:
            yield current

    def import_bundle(
        self,
        path: str,
        *,
        scope: Mapping[str, Any],
        execution: TaskExecution | None = None,
    ) -> dict[str, Any]:
        trusted = self._scope(scope)
        target = self._bundle_path(path)
        if execution is not None:
            execution.progress(5, "inventory")
            execution.check_cancelled()
        original_inventory = self._inventory_bundle(target)
        path_digest = _digest(str(target).casefold())
        temporary: tempfile.TemporaryDirectory[str] | None = None
        parse_target = target
        try:
            if target.is_file() and target.suffix.casefold() == ".zip":
                cache = resolve_cache_home(self.workspace) / "imports"
                cache.mkdir(parents=True, exist_ok=True)
                temporary = tempfile.TemporaryDirectory(prefix="memoryguard-import-", dir=cache)
                parse_target = Path(temporary.name)
                try:
                    safe_extract_zip(
                        target,
                        parse_target,
                        max_files=_MAX_IMPORT_FILES,
                        max_total_size=_MAX_IMPORT_TOTAL_BYTES,
                        max_ratio=50,
                    )
                except (OSError, ValueError) as exc:
                    raise ImportControlError("import_zip_rejected") from exc
                self._inventory_bundle(parse_target)
            adapter, detected = self._detect(parse_target)
            if execution is not None:
                execution.progress(18, "parse")
                execution.check_cancelled()
            conversations = adapter.parse(parse_target)
            events: list[ConversationEvent] = []
            for conversation in conversations:
                for index, message in enumerate(conversation.messages):
                    role = str(message.get("role") or "").strip().casefold()
                    if role not in {"user", "assistant"}:
                        continue
                    body = str(message.get("content") or "")
                    if not body.strip():
                        continue
                    created = str(message.get("created_at") or "")
                    event_id = "import-event-" + _digest(
                        detected.provider,
                        conversation.conv_id,
                        index,
                        created,
                        role,
                    )
                    events.append(ConversationEvent(
                        external_object_key=str(conversation.conv_id),
                        content=body,
                        role=role,
                        ordinal=index,
                        event_id=event_id,
                        source_revision=original_inventory.digest,
                        title=str(conversation.title or "")[:512],
                        provider=trusted["provider"] or "gui",
                        workspace_id=trusted["workspace_id"],
                        agent_instance_id=trusted["agent_instance_id"],
                        project_ref=(str(conversation.project_ref or "") or trusted["project_ref"]),
                        share_group_id=trusted["share_group_id"],
                        sensitivity=trusted["sensitivity"],
                        policy_class=trusted["policy_class"],
                        metadata={
                            "source_provider": str(detected.provider),
                            "import_bundle_digest": original_inventory.digest,
                            "project_source": str(getattr(conversation, "project_source", "unknown") or "unknown"),
                        },
                        locator={"message_index": index},
                    ))
            if not events:
                raise ImportControlError("import_no_conversation_events")
            if execution is not None:
                execution.progress(30, "sync", item_count=len(events))
                execution.check_cancelled()
            content = ContentStore(
                self.workspace,
                workspace_id=trusted["workspace_id"],
                trust_domain="conversation-import",
                sensitivity=trusted["sensitivity"],
                retention_authority="workspace",
            )
            sync = ConversationSync(content)
            source_id = stable_id(
                "gui-import-source", str(detected.provider), path_digest
            )
            run = sync.begin_sync(
                source_id,
                owner_id="gui-import:" + trusted["agent_instance_id"],
                provider=str(detected.provider),
                source_type="conversation_import",
                external_root_key="bundle:" + path_digest,
                workspace_id=trusted["workspace_id"],
            )
            cursor = ""
            applied = 0
            changed = 0
            try:
                batches = list(self._event_batches(events))
                for batch_index, batch in enumerate(batches):
                    if execution is not None:
                        execution.check_cancelled()
                    result = sync.stage_batch(
                        run,
                        batch,
                        max_turns=_BATCH_EVENTS,
                        max_chars=_BATCH_CHARS,
                        continuation_cursor=cursor,
                        coverage_status="covered",
                        owner_id=run.owner_id,
                    )
                    cursor = result.continuation_cursor
                    applied += int(result.applied)
                    changed += int(result.changed)
                    if execution is not None:
                        execution.progress(
                            min(85, 30 + int(55 * (batch_index + 1) / max(1, len(batches)))),
                            "sync",
                            item_count=applied,
                        )
                finished = sync.finish_sync(
                    run,
                    status="complete",
                    continuation_cursor=cursor,
                    owner_id=run.owner_id,
                )
            except Exception as exc:
                try:
                    sync.finish_sync(
                        run,
                        status="failed",
                        error_code=str(getattr(exc, "code", "") or type(exc).__name__).casefold()[:128],
                        owner_id=run.owner_id,
                    )
                except Exception:
                    pass
                raise
            if finished.state != "complete":
                raise ImportControlError("import_sync_incomplete")
            return {
                "status": "succeeded",
                "provider": str(detected.provider),
                "conversation_count": len(conversations),
                "turn_count": applied,
                "changed_turn_count": changed,
                "extract_candidate_count": 0,
                "memory_record_count": 0,
                "written_to_ir": False,
                "written_to_history": True,
                "history_agent_instance_id": trusted["agent_instance_id"],
                "source_id": source_id,
                "source_revision": finished.revision,
                "manifest_digest": finished.manifest_digest,
                "coverage_digest": finished.coverage_digest,
                "storage": "v2_content_plane",
            }
        except ImportControlError:
            raise
        except Exception as exc:
            raise ImportControlError("import_execution_failed") from exc
        finally:
            if temporary is not None:
                temporary.cleanup()


__all__ = ["ImportControlError", "ImportControlService"]
