from __future__ import annotations

import json

import pytest

from memoryguard import mcp_server
from memoryguard.response_budget import (
    DEFAULT_RESPONSE_BUDGET_BYTES,
    ResponseReferenceError,
    ResponseStore,
    json_bytes,
    paged_summary,
)


def _paged_store() -> tuple[ResponseStore, str, dict[str, object]]:
    envelope = {
        "status": "succeeded",
        "content": [{"type": "text", "text": "中文🙂 / utf-8 boundary"}],
    }
    store = ResponseStore()
    response_ref = store.put(envelope, scope=None, binding=None, public=True)["id"]
    return store, response_ref, envelope


def test_utf8_pages_reassemble_and_reject_non_boundary_offsets() -> None:
    store, response_ref, envelope = _paged_store()
    expected = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

    offset = 0
    pages: list[str] = []
    while True:
        result = store.read(response_ref, scope=None, binding=None, offset=offset, limit=7)
        assert result["page_bytes"] > 0
        pages.append(result["page"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]

    assert "".join(pages) == expected

    encoded = expected.encode("utf-8")
    continuation = next(
        index for index, byte in enumerate(encoded) if byte & 0b1100_0000 == 0b1000_0000
    )
    with pytest.raises(ResponseReferenceError, match="response_page_invalid"):
        store.read(response_ref, scope=None, binding=None, offset=continuation, limit=7)


def test_utf8_limit_too_small_for_current_codepoint_fails_without_stalled_page() -> None:
    store, response_ref, envelope = _paged_store()
    encoded = json_bytes(envelope)
    emoji_offset = encoded.index("🙂".encode("utf-8"))

    with pytest.raises(ResponseReferenceError, match="response_page_invalid"):
        store.read(response_ref, scope=None, binding=None, offset=emoji_offset, limit=3)


def test_oversized_summary_keeps_receipt_status_and_ref_with_structured_content() -> None:
    escaped_id = ("界🙂\x00\r\n" * 64)[:256]
    items = [{"memory_id": f"{index}-{escaped_id}"} for index in range(20)]
    envelope = {
        "id": "operation-1",
        "status": "succeeded",
        "receipt_id": "receipt-1",
        "content": [{
            "type": "text",
            "text": json.dumps({"items": items}, ensure_ascii=False, indent=2),
        }],
        "structuredContent": {"status": "succeeded", "items": items},
    }
    response_ref = {
        "id": "response-ref-1",
        "digest": "digest-1",
        "total_bytes": 100_000,
        "expires_in_seconds": 300,
        "validation": "binding_session_replay",
    }

    result = paged_summary(envelope, response_ref)
    body = json.loads(result["content"][0]["text"])

    assert len(json_bytes(result)) <= DEFAULT_RESPONSE_BUDGET_BYTES
    assert body["delivery"]["status"] == "paged"
    assert body["response_ref"]["id"] == response_ref["id"]
    assert body["identifiers"]["id"] == envelope["id"]
    assert body["identifiers"]["status"] == envelope["status"]
    assert body["identifiers"]["receipt_id"] == envelope["receipt_id"]
    assert result["structuredContent"] == body


def test_response_read_caps_double_escaped_page_without_creating_ref(monkeypatch, tmp_path) -> None:
    class _SpyResponseStore(ResponseStore):
        def __init__(self) -> None:
            super().__init__()
            self.put_calls = 0

        def put(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.put_calls += 1
            return super().put(*args, **kwargs)

    long_key = "\\" * 2040
    long_value = "\\" * 2040
    payload = {long_key: long_value}
    envelope = {
        "content": [{
            "type": "text",
            "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }],
    }
    store = _SpyResponseStore()
    monkeypatch.setattr(mcp_server, "GLOBAL_RESPONSE_STORE", store)
    response_ref = store.put(envelope, scope=None, binding=None, public=True)["id"]

    selected = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    offset = selected.index(b"\\\\")
    assert len(json_bytes([long_key])) < 4096

    result = mcp_server._read_response_reference(
        {
            "arguments": {
                "response_ref": response_ref,
                "fields": [long_key],
                "offset": offset,
                "limit": 4096,
            }
        },
        tmp_path,
    )

    assert len(json_bytes(result)) <= DEFAULT_RESPONSE_BUDGET_BYTES
    assert result.get("isError") is True
    error = json.loads(result["content"][0]["text"])
    assert isinstance(error.get("code"), str) and error["code"]
    assert "response_ref" not in error
    assert store.put_calls == 1
