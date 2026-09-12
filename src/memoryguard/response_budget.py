"""Bounded, replay-validated MCP response paging.

Snapshots stay in process memory only.  They are never a second data store:
private entries are readable only by the exact current session/binding scope
and are revalidated by the caller before every page is returned.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import time
from typing import Any, Callable, Mapping


DEFAULT_RESPONSE_BUDGET_BYTES = 24_000
DEFAULT_PAGE_BYTES = 3_000
MIN_PAGE_BYTES = 4
MAX_PAGE_BYTES = 4_096
MAX_RESPONSE_FIELDS_BYTES = 4_096
RESPONSE_REF_TTL_SECONDS = 300
MAX_RESPONSE_REFS = 16
MAX_RESPONSE_SNAPSHOT_BYTES = 512_000


class ResponseReferenceError(ValueError):
    """Stable failure for opaque response-reference reads."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def json_text(value: Any) -> str:
    """Return compact UTF-8 JSON used for response size accounting."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_bytes(value: Any) -> bytes:
    return json_text(value).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def compact_success_envelope(result: Mapping[str, Any]) -> dict[str, Any]:
    """Minify JSON text blocks without dropping or inventing content blocks."""

    compacted = deepcopy(dict(result))
    content = compacted.get("content")
    if not isinstance(content, list):
        return compacted
    blocks: list[Any] = []
    for block in content:
        if not isinstance(block, Mapping) or not isinstance(block.get("text"), str):
            blocks.append(block)
            continue
        updated = dict(block)
        try:
            parsed = json.loads(updated["text"])
        except (TypeError, ValueError, json.JSONDecodeError):
            blocks.append(updated)
            continue
        if isinstance(parsed, (dict, list)):
            updated["text"] = json_text(parsed)
        blocks.append(updated)
    compacted["content"] = blocks
    return compacted


def envelope_identifiers(envelope: Mapping[str, Any]) -> dict[str, str | int | bool | None]:
    """Keep bounded operation identifiers visible when a body moves to a ref."""

    wanted = {
        "id", "ok", "status", "code", "accepted", "deferred", "operation",
        "job_id", "run_id", "memory_id", "receipt_id", "decision_id", "undo_id",
    }
    found: dict[str, str | int | bool | None] = {}
    queue: list[tuple[str, Any]] = [("", envelope)]
    while queue and len(found) < 20:
        path, value = queue.pop(0)
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = str(key)
                child_path = f"{path}.{name}" if path else name
                if (
                    name in wanted or name.endswith("_id")
                ) and (child is None or isinstance(child, (str, int, bool))) and len(str(child or "")) <= 256:
                    found[child_path] = child
                elif name == "text" and isinstance(child, str):
                    try:
                        parsed = json.loads(child)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if isinstance(parsed, (Mapping, list, tuple)):
                        queue.append((path, parsed))
                elif isinstance(child, (Mapping, list, tuple)):
                    queue.append((child_path, child))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value[:32]):
                if isinstance(child, (Mapping, list, tuple)):
                    queue.append((f"{path}[{index}]", child))
    return found


@dataclass(frozen=True)
class ResponseScope:
    """Exact context fields that make a private page non-transferable."""

    values: tuple[tuple[str, str], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ResponseScope":
        keys = (
            "workspace_id", "share_group_id", "agent_instance_id", "project_ref",
            "provider", "runtime_role", "session_id", "session_source",
            "session_trusted", "context_hash", "namespace_id", "sensitivity",
            "policy_class",
        )
        return cls(tuple((key, str(value.get(key) or "")) for key in keys))


@dataclass(frozen=True)
class BindingRevision:
    """Active binding identity/version checked before every private page."""

    binding_id: str
    revision: int
    share_group_id: str
    agent_instance_id: str


@dataclass
class _ResponseEntry:
    response_id: str
    document: str
    document_digest: str
    expires_at: float
    scope: ResponseScope | None
    binding: BindingRevision | None
    public: bool
    revalidate: Callable[[], str | None] | None


class ResponseStore:
    """Small process-local response cache; no disk persistence or replay writes."""

    def __init__(
        self,
        *,
        ttl_seconds: int = RESPONSE_REF_TTL_SECONDS,
        max_entries: int = MAX_RESPONSE_REFS,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, _ResponseEntry] = {}
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            del self._entries[key]
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            for key, _entry in sorted(self._entries.items(), key=lambda item: item[1].expires_at)[:overflow]:
                del self._entries[key]

    def put(
        self,
        envelope: Mapping[str, Any],
        *,
        scope: ResponseScope | None,
        binding: BindingRevision | None,
        public: bool = False,
        revalidate: Callable[[], str | None] | None = None,
    ) -> dict[str, Any]:
        if not public and (scope is None or binding is None or revalidate is None):
            raise ResponseReferenceError("response_ref_unverifiable")
        document = json_text(envelope)
        document_size = len(document.encode("utf-8"))
        if document_size > MAX_RESPONSE_SNAPSHOT_BYTES:
            raise ResponseReferenceError("response_ref_too_large")
        response_id = secrets.token_urlsafe(24)
        entry = _ResponseEntry(
            response_id=response_id,
            document=document,
            document_digest=hashlib.sha256(document.encode("utf-8")).hexdigest(),
            expires_at=time.monotonic() + self.ttl_seconds,
            scope=scope,
            binding=binding,
            public=public,
            revalidate=revalidate,
        )
        with self._lock:
            self._prune()
            self._entries[response_id] = entry
            self._prune()
        return {
            "id": response_id,
            "digest": entry.document_digest,
            "total_bytes": document_size,
            "expires_in_seconds": self.ttl_seconds,
            "validation": "public_registry" if public else "binding_session_replay",
        }

    def is_public(self, response_id: str) -> bool:
        with self._lock:
            self._prune()
            entry = self._entries.get(response_id)
            if entry is None:
                raise ResponseReferenceError("response_ref_expired")
            return entry.public

    def read(
        self,
        response_id: str,
        *,
        scope: ResponseScope | None,
        binding: BindingRevision | None,
        fields: Any = None,
        offset: Any = 0,
        limit: Any = DEFAULT_PAGE_BYTES,
    ) -> dict[str, Any]:
        if not isinstance(response_id, str) or not response_id:
            raise ResponseReferenceError("response_ref_invalid")
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not MIN_PAGE_BYTES <= limit <= MAX_PAGE_BYTES
        ):
            raise ResponseReferenceError("response_page_invalid")
        if fields is not None and (
            not isinstance(fields, list) or any(not isinstance(item, str) or not item for item in fields)
        ):
            raise ResponseReferenceError("response_fields_invalid")
        if fields is not None and len(json_bytes(fields)) > MAX_RESPONSE_FIELDS_BYTES:
            # ``fields`` is returned in every page receipt. Bound it before
            # serializing so a valid request cannot make its own response
            # exceed the public envelope cap.
            raise ResponseReferenceError("response_fields_invalid")
        with self._lock:
            self._prune()
            entry = self._entries.get(response_id)
        if entry is None:
            raise ResponseReferenceError("response_ref_expired")
        if not entry.public and (entry.scope != scope or entry.binding != binding):
            raise ResponseReferenceError("response_ref_access_denied")
        if entry.revalidate is not None:
            code = entry.revalidate()
            if code:
                with self._lock:
                    self._entries.pop(response_id, None)
                raise ResponseReferenceError(code)
        try:
            document_value = json.loads(entry.document)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:  # defensive
            raise ResponseReferenceError("response_ref_invalid") from exc
        document_kind = "mcp_envelope"
        if fields is not None:
            # Field selection deliberately targets business data, not the MCP
            # wrapper. Multi-content/non-JSON results remain lossless through
            # ordinary paging and reject this narrower operation explicitly.
            content = document_value.get("content") if isinstance(document_value, Mapping) else None
            if (
                not isinstance(content, list)
                or len(content) != 1
                or not isinstance(content[0], Mapping)
                or not isinstance(content[0].get("text"), str)
            ):
                raise ResponseReferenceError("response_fields_unavailable")
            try:
                payload = json.loads(content[0]["text"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ResponseReferenceError("response_fields_unavailable") from exc
            if not isinstance(payload, Mapping):
                raise ResponseReferenceError("response_fields_unavailable")
            selected_payload: dict[str, Any] = {}
            for selector in fields:
                parts = (
                    [part.replace("~1", "/").replace("~0", "~") for part in selector[1:].split("/")]
                    if selector.startswith("/") else [selector]
                )
                if not parts or any(not part for part in parts):
                    raise ResponseReferenceError("response_fields_invalid")
                current: Any = payload
                for part in parts:
                    if not isinstance(current, Mapping) or part not in current:
                        raise ResponseReferenceError("response_field_unknown")
                    current = current[part]
                target = selected_payload
                for part in parts[:-1]:
                    existing = target.get(part)
                    if existing is None:
                        existing = {}
                        target[part] = existing
                    if not isinstance(existing, dict):
                        raise ResponseReferenceError("response_fields_invalid")
                    target = existing
                target[parts[-1]] = current
            document_value = selected_payload
            document_kind = "payload_fields"
        selected = json_text(document_value)
        selected_bytes = selected.encode("utf-8")
        if offset > len(selected_bytes):
            raise ResponseReferenceError("response_page_invalid")
        if offset < len(selected_bytes) and selected_bytes[offset] & 0b1100_0000 == 0b1000_0000:
            raise ResponseReferenceError("response_page_invalid")
        end = min(len(selected_bytes), offset + limit)
        while end > offset:
            try:
                page = selected_bytes[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            page = ""
        return {
            "ok": True,
            "response_ref": {
                "id": entry.response_id,
                "digest": entry.document_digest,
                "validation": "public_registry" if entry.public else "binding_session_replay",
            },
            "fields": list(fields) if fields is not None else None,
            "document_kind": document_kind,
            "encoding": "json-utf8",
            "offset": offset,
            "page": page,
            "page_bytes": end - offset,
            "total_bytes": len(selected_bytes),
            "next_offset": end if end < len(selected_bytes) else None,
        }


GLOBAL_RESPONSE_STORE = ResponseStore()


def _summary_result(body: Mapping[str, Any], *, structured: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": json_text(body)}]}
    if structured:
        result["structuredContent"] = dict(body)
    return result


def _bounded_summary(
    envelope: Mapping[str, Any],
    delivery: Mapping[str, Any],
    *,
    budget_bytes: int,
) -> dict[str, Any]:
    """Make response metadata fit its own fully serialized envelope budget."""

    delivery_payload = dict(delivery)
    response_ref = delivery_payload.pop("response_ref", None)
    body: dict[str, Any] = {"ok": True, "delivery": delivery_payload}
    if isinstance(response_ref, Mapping):
        body["response_ref"] = dict(response_ref)
    if envelope.get("deprecated") is True:
        body["deprecated"] = True
        deprecation = envelope.get("deprecation")
        if isinstance(deprecation, Mapping):
            body["deprecation"] = dict(deprecation)
    structured = "structuredContent" in envelope
    candidates = envelope_identifiers(envelope)
    kept: dict[str, str | int | bool | None] = {}
    for key, value in candidates.items():
        trial = dict(body)
        trial["identifiers"] = {**kept, key: value}
        if len(json_bytes(_summary_result(trial, structured=structured))) > budget_bytes:
            continue
        kept[key] = value
    if kept:
        body["identifiers"] = kept
    result = _summary_result(body, structured=structured)
    if len(json_bytes(result)) > budget_bytes:  # defensive: receipt metadata is fixed and tiny
        raise ResponseReferenceError("response_summary_too_large")
    return result


def paged_summary(
    envelope: Mapping[str, Any],
    response_ref: Mapping[str, Any],
    *,
    budget_bytes: int = DEFAULT_RESPONSE_BUDGET_BYTES,
) -> dict[str, Any]:
    """Replace an oversized success envelope with a small, explicit receipt."""

    delivery: dict[str, Any] = {
        "status": "paged",
        "code": "response_budget_exceeded",
        "budget_bytes": budget_bytes,
        "response_ref": dict(response_ref),
    }
    return _bounded_summary(envelope, delivery, budget_bytes=budget_bytes)


def unavailable_summary(
    envelope: Mapping[str, Any],
    *,
    code: str,
    budget_bytes: int = DEFAULT_RESPONSE_BUDGET_BYTES,
) -> dict[str, Any]:
    """State that a read completed but its oversized body cannot be replayed."""

    return _bounded_summary(envelope, {
        "status": "unavailable",
        "code": code,
        "budget_bytes": budget_bytes,
        "action": "narrow_query",
    }, budget_bytes=budget_bytes)


__all__ = [
    "BindingRevision", "DEFAULT_PAGE_BYTES", "DEFAULT_RESPONSE_BUDGET_BYTES",
    "GLOBAL_RESPONSE_STORE", "ResponseReferenceError", "ResponseScope",
    "compact_success_envelope", "digest", "json_bytes", "json_text", "paged_summary",
    "unavailable_summary",
]
