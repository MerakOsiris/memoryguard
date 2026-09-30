"""User-level host hooks for deterministic MemoryGuard takeover.

The Skill is the installer/trigger.  Enforcement lives in host lifecycle hooks
because a Skill only runs while it is active.  This module keeps host-specific
configuration behind one small interface and keeps runtime receipts free of
prompt or memory bodies.
"""

from __future__ import annotations

_V2_READ_STATES = frozenset({"V2_READY", "V2_ACTIVE"})

import argparse
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .runtime_v2.public_safety import v2_upgrade_message, v2_upgrade_payload


HOOK_VERSION = "1"
HOOK_MARKER = "memoryguard.host_hooks"
HOOK_MODES = {"enforce", "observe", "paused"}
_RUNTIME_DIR = "hook-runtime"
_MAX_RECEIPT_AGE_DAYS = 30


@dataclass
class _PathThreadLockEntry:
    lock: threading.Lock
    users: int = 0


_PATH_THREAD_LOCKS: dict[str, _PathThreadLockEntry] = {}
_PATH_THREAD_LOCKS_GUARD = threading.Lock()
# A state transaction may call the normal atomic writer while already holding
# the same cross-process sidecar lock.  Keep that nested path re-entrant for
# this thread; other processes still serialize on the sidecar file.
_CROSS_PROCESS_HELD = threading.local()
_COMPACT_REMINDER = (
    "压缩前发现尚未沉淀的长期记忆候选；继续工作前用 "
    "memoryguard_memory_write 萃取保存，不得保存整段对话。"
)

_EVENT_NAMES = {
    "session_start": "SessionStart",
    "subagent_start": "SubagentStart",
    "user_prompt": "UserPromptSubmit",
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "pre_compact": "PreCompact",
    "stop": "Stop",
}

_CURSOR_EVENT_NAMES = {
    "session_start": "sessionStart",
    "user_prompt": "beforeSubmitPrompt",
    "pre_tool": "preToolUse",
    "post_tool": "postToolUse",
    "pre_compact": "preCompact",
    "stop": "stop",
}

_DURABLE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bremember\b",
        r"\bfrom now on\b",
        r"\bmy preference\b",
        r"\bdefault to\b",
        r"\bnever again\b",
        r"记住",
        r"以后(?:都|默认|不要|必须)",
        r"从今以后",
        r"默认(?:使用|走|采用)",
        r"我(?:喜欢|偏好|习惯)",
        r"不要再",
        r"长期规则",
        r"更正[：:]",
    )
)

_NATIVE_PATH_PATTERNS = (
    re.compile(r"/\.codex/memories(?:/|$)", re.IGNORECASE),
    re.compile(r"/\.claude/projects/.+/memory(?:/|$)", re.IGNORECASE),
    re.compile(r"/\.cursor/memories(?:/|$)", re.IGNORECASE),
    re.compile(r"/\.trae(?:-cn)?/.*/memory(?:/|$)", re.IGNORECASE),
)

_SHELL_WRITE_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:rm|mv|cp|mkdir|del|move|copy|"
    r"remove-item|move-item|copy-item|new-item|set-content|add-content|out-file)"
    r"(?:[\s;&|]|$)|(?:>>?|2>)",
    re.IGNORECASE,
)



def _repair_json_invalid_escapes(text: str) -> str:
    """Turn illegal JSON backslash escapes into escaped backslashes."""
    out: list[str] = []
    i = 0
    n = len(text)
    hexdigits = "0123456789abcdefABCDEF"
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in '"\\/bfnrt':
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            if nxt == "u" and i + 5 < n and all(c in hexdigits for c in text[i + 2:i + 6]):
                out.append(text[i:i + 6])
                i += 6
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)

_LAST_STDIN_META: dict[str, Any] = {}


def _record_stdin_meta(**fields: Any) -> None:
    _LAST_STDIN_META.clear()
    _LAST_STDIN_META.update(fields)


def read_hook_stdin_json(stdin_buffer=None) -> dict[str, Any]:
    """Decode hook stdin JSON.

    Always read bytes and decode with utf-8-sig so Cursor BOM stdin and
    Codex plain UTF-8 both parse. Windows hosts sometimes deliver tool
    paths with raw backslashes (L:\workdir). Those are invalid JSON
    escapes and used to crash PreToolUse fail-closed. Repair only after
    json.loads fails.

    Read the binary buffer before any stdin reconfigure. Cursor already
    launches this command with ``python -X utf8``; reconfiguring stdin
    first can drain the wrapper and leave ``buffer.read()`` empty.
    """
    # memoryguard-windows-json-escape-repair
    raw = b""
    source = "empty"
    if stdin_buffer is None:
        raw = sys.stdin.buffer.read(2_000_000)
        if raw and raw.strip():
            source = "buffer"
        else:
            leftover = ""
            try:
                leftover = sys.stdin.read(2_000_000)
            except Exception:
                leftover = ""
            if leftover and leftover.strip():
                raw = leftover.encode("utf-8", errors="surrogatepass")
                source = "text"
    else:
        raw = stdin_buffer.read(2_000_000)
        source = "buffer" if raw and raw.strip() else "empty"
    _record_stdin_meta(
        nbytes=len(raw or b""),
        source=source,
        head_hex=(raw or b"")[:24].hex(),
        raw_hex=(raw or b"")[:800].hex(),
        repair=False,
    )
    if not raw or not raw.strip():
        return {}
    from .encoding_guard import decode_hook_bytes

    text = decode_hook_bytes(raw, source="hook_stdin")
    data = _loads_hook_stdin_object(text)
    if data is not None:
        _LAST_STDIN_META.pop("raw_hex", None)
        return data
    repaired = _repair_json_invalid_escapes(text)
    _LAST_STDIN_META["repair"] = True
    data = _loads_hook_stdin_object(repaired)
    if data is not None:
        _LAST_STDIN_META.pop("raw_hex", None)
        return data
    stripped = _neutralize_broken_hook_path_fields(repaired)
    _LAST_STDIN_META["neutralized"] = stripped != repaired
    data = _loads_hook_stdin_object(stripped)
    if data is not None:
        _LAST_STDIN_META.pop("raw_hex", None)
        return data
    salvaged = _salvage_hook_tool_payload(repaired)
    if salvaged:
        _LAST_STDIN_META["salvaged"] = True
        _LAST_STDIN_META.pop("raw_hex", None)
        return salvaged
    print(json.dumps({
        "memoryguard_hook_diagnostic": "stdin_json_repair_failed",
        "error": _LAST_STDIN_META.get("error", "unparsed"),
    }, ensure_ascii=True), file=sys.stderr)
    return {}


def _loads_hook_stdin_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        pos = int(getattr(exc, "pos", 0) or 0)
        _LAST_STDIN_META["error"] = str(exc)[:200]
        _LAST_STDIN_META["error_pos"] = pos
        _LAST_STDIN_META["around"] = text[max(0, pos - 80): pos + 80]
        return None
    if not isinstance(data, dict):
        raise ValueError("hook input must be a JSON object")
    return data


def _neutralize_broken_hook_path_fields(text: str) -> str:
    """Drop host path arrays Cursor sometimes emits with a truncated quote."""
    return re.sub(
        r'"workspace_roots"\s*:\s*\[[^\]]*\]',
        '"workspace_roots":[]',
        text,
        count=1,
    )


def _salvage_hook_tool_payload(text: str) -> dict[str, Any]:
    """Recover tool identity when the host JSON envelope is truncated."""
    data: dict[str, Any] = {}
    for key in (
        "tool_name",
        "toolName",
        "conversation_id",
        "session_id",
        "generation_id",
        "hook_event_name",
    ):
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
        if match:
            data[key] = json.loads(f'"{match.group(1)}"')
    match = re.search(r'"tool_input"\s*:\s*\{', text)
    if match:
        blob = _slice_balanced_object(text, match.end() - 1)
        if blob:
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                try:
                    parsed = json.loads(_repair_json_invalid_escapes(blob))
                except json.JSONDecodeError:
                    parsed = None
            if isinstance(parsed, dict):
                data["tool_input"] = parsed
    inferred = _infer_hook_result_from_text(text)
    if inferred is not None:
        data.setdefault("tool_output", inferred)
    return data


def _infer_hook_result_from_text(text: str) -> dict[str, Any] | None:
    """Best-effort status from a truncated Cursor ``tool_output`` blob."""
    window = text
    marker = re.search(
        r'"(?:tool_output|tool_result|tool_response|result)"\s*:\s*',
        text,
    )
    if marker:
        window = text[marker.end(): marker.end() + 8000]
    error_at = _search_span(r'\\*"isError\\*"\s*:\s*true', window)
    ok_false = _search_span(r'\\*"ok\\*"\s*:\s*false', window)
    ok_true = _search_span(r'\\*"ok\\*"\s*:\s*true', window)
    if error_at is not None and (ok_true is None or error_at < ok_true):
        return {"isError": True}
    if ok_false is not None and (ok_true is None or ok_false < ok_true):
        return {"ok": False, "status": "error"}
    ready = _search_span(r'\\*"ready\\*"\s*:\s*true', window) is not None
    status_ok = _search_span(r'\\*"status\\*"\s*:\s*\\*"ok\\*"', window) is not None
    if ok_true is not None or ready or status_ok:
        return {"ok": True, "status": "ok", "ready": True}
    return None


def _search_span(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    return match.start() if match else None


def _slice_balanced_object(text: str, start: int) -> str:
    depth = 0
    in_str = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _effective_agent_context(
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
    *,
    event: str,
):
    """Build scope from host event identity, never a prompt-supplied role."""
    from .schema_v3 import EffectiveAgentContext
    from .rule_scope import canonical_project_ref
    session_id = str(
        payload.get("session_id")
        or payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("sessionId")
        or payload.get("thread_id")
        or ""
    ).strip()
    context_hash = str(payload.get("context_hash") or "").strip()
    child_agent_id = _host_subagent_id(payload, agent_instance_id)
    return EffectiveAgentContext(
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        provider=provider,
        project_ref=canonical_project_ref(
            payload.get("project_ref")
            or payload.get("projectRef")
            or payload.get("cwd")
        ),
        # Codex sends the child identifier as ``agent_id`` on child tool
        # events.  It is a runtime identity, never a replacement for the
        # configured/bound MemoryGuard Agent.  Preserve it in the trusted
        # runtime context so child rule scope is stable across lifecycle and
        # tool events.
        runtime_role="subagent" if child_agent_id else "root",
        runtime_agent_id=child_agent_id,
        parent_agent_id=agent_instance_id if child_agent_id else "",
        session_id=session_id,
        context_hash=context_hash,
    )


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]


@contextmanager
def _thread_lock_for_sidecar(lock_path: Path):
    """Queue this process's threads and retire unused path entries."""
    key = os.path.normcase(os.path.abspath(os.fspath(lock_path)))
    with _PATH_THREAD_LOCKS_GUARD:
        entry = _PATH_THREAD_LOCKS.get(key)
        if entry is None:
            entry = _PathThreadLockEntry(threading.Lock())
            _PATH_THREAD_LOCKS[key] = entry
        entry.users += 1

    acquired = False
    try:
        entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _PATH_THREAD_LOCKS_GUARD:
            current = _PATH_THREAD_LOCKS.get(key)
            if current is entry:
                entry.users -= 1
                if entry.users == 0:
                    del _PATH_THREAD_LOCKS[key]


@contextmanager
def _cross_process_path_lock(path: Path, timeout_seconds: float = 3.0):
    """Serialize atomic replacements across Hook processes.

    Windows denies replacing a destination while another process still has a
    transient handle.  A sidecar byte lock prevents our own writers racing;
    bounded replace retries cover short-lived antivirus/indexer handles.
    """
    lock_path = path.with_name(f".{path.name}.memoryguard.lock")
    key = os.path.normcase(os.path.abspath(os.fspath(lock_path)))
    held = getattr(_CROSS_PROCESS_HELD, "paths", None)
    if held is None:
        held = set()
        _CROSS_PROCESS_HELD.paths = held
    if key in held:
        yield
        return
    with _thread_lock_for_sidecar(lock_path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd: int | None = None
        deadline = time.monotonic() + timeout_seconds
        try:
            while lock_fd is None:
                try:
                    lock_fd = os.open(
                        lock_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                    os.write(lock_fd, f"{os.getpid()} {_now_iso()}".encode("ascii"))
                except (FileExistsError, PermissionError):
                    # Recover only genuinely stale crash remnants.  Normal
                    # writers hold the lock for milliseconds and are never
                    # unlinked by a waiter.
                    try:
                        stale = time.time() - lock_path.stat().st_mtime > 30.0
                        if stale:
                            lock_path.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out acquiring hook runtime lock: {path}")
                    time.sleep(0.01)
            held.add(key)
            try:
                yield
            finally:
                held.discard(key)
        finally:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                for attempt in range(5):
                    try:
                        lock_path.unlink(missing_ok=True)
                        break
                    except PermissionError:
                        if attempt == 4:
                            break
                        time.sleep(0.01 * (attempt + 1))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _cross_process_path_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.memoryguard-",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(7):
                try:
                    os.replace(tmp, path)
                    break
                except OSError as exc:
                    retryable = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}
                    if not retryable or attempt == 6:
                        raise
                    time.sleep(0.01 * (2 ** attempt))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _load_json_config(path: Path, *, strict: bool) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if strict:
            raise ValueError(f"invalid JSON hook config: {path}: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"invalid JSON hook config root: {path}")
        return {}
    return data


def _hook_tool_result(payload: dict[str, Any]) -> Any:
    """Read Cursor/Codex tool results, including JSON-string ``tool_output``."""
    for key in (
        "tool_result",
        "result",
        "tool_output",
        "output",
        "tool_response",
        "response",
    ):
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in "{[":
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    try:
                        return json.loads(_repair_json_invalid_escapes(text))
                    except json.JSONDecodeError:
                        return value
            return value
        return value
    return None


def _coerce_tool_result_status(payload: Any) -> tuple[bool | None, str | None]:
    """Return ``(ok, reason)`` for tool execution feedback.

    ``ok`` is ``True``/``False`` for explicit success/failure; ``None`` means
    cannot determine from current payload.
    """
    if payload is None:
        return None, None

    if isinstance(payload, dict):
        if "isError" in payload:
            is_error = payload.get("isError")
            if isinstance(is_error, bool):
                return (False, "isError=true") if is_error else (True, None)
        for key in ("error", "error_code", "message"):
            if payload.get(key):
                if key == "error":
                    if isinstance(payload[key], (dict, list, str)):
                        return False, "error present"
                else:
                    return False, f"{key}={payload[key]!r}"
        ok_value = payload.get("ok")
        if isinstance(ok_value, bool):
            return (True, None) if ok_value else (False, "ok=false")
        status_value = str(payload.get("status") or "").strip().casefold()
        if status_value in {"error", "failed", "failure"}:
            return False, f"status={payload.get('status')}"
        if status_value in {"ok", "success", "succeeded"}:
            return True, None
        if payload.get("ready") is True:
            return True, None
        positive_signals = ("memory_id", "decision_id", "version_id", "record")
        if any(sig in payload for sig in positive_signals):
            return True, None
        content = payload.get("content")
        if isinstance(content, list) and content:
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    parsed = json.loads(text)
                except Exception:
                    continue
                status, reason = _coerce_tool_result_status(parsed)
                if status is not None:
                    return status, reason
        return None, None

    if isinstance(payload, list):
        unknown_count = 0
        for item in payload:
            status, reason = _coerce_tool_result_status(item)
            if status is not None:
                return status, reason
            unknown_count += 1
        if unknown_count:
            return None, None

    return None, None


def _write_json_config(path: Path, data: dict[str, Any]) -> None:
    if not data:
        path.unlink(missing_ok=True)
        return
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _command(
    provider: str,
    event: str,
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
    *,
    windows: bool | None = None,
    runtime_python: str | Path | None = None,
) -> str:
    # A generated handler must execute the same interpreter that launched the
    # provider MCP.  Falling back to the process interpreter is safe for an
    # ordinary wheel install, while the explicit snapshot path prevents PATH
    # drift from mixing a global 0.7.3 hook with a newer MCP snapshot.
    python = str(runtime_python or sys.executable).strip() or sys.executable
    argv = [
        python,
        "-X",
        "utf8",
        "-m",
        HOOK_MARKER,
        "run",
        "--provider",
        provider,
        "--event",
        event,
        "--workspace",
        str(workspace),
        "--agent-id",
        agent_instance_id,
        "--share-group-id",
        share_group_id,
        "--managed-by",
        "memoryguard",
    ]
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def _is_our_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command", "") or "")
    return HOOK_MARKER in command and "--managed-by" in command


def _command_option(command: str, option: str) -> str:
    """Read one generated hook command option without executing its shell text."""
    match = re.search(
        rf"{re.escape(option)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        command,
    )
    if match is None:
        return ""
    return next((str(value) for value in match.groups() if value), "")


def _generated_handler_binding(handler: Any) -> tuple[str, str, str, str] | None:
    """Return generated handler identity: provider, agent, group, workspace.

    ``HOOK_MARKER`` plus ``--managed-by memoryguard`` identifies a generated
    MemoryGuard command.  All three bound arguments must be present before a
    handler can be removed; malformed or user-owned commands stay untouched.
    """
    if not _is_our_handler(handler):
        return None
    for key in ("command", "commandWindows"):
        command = str(handler.get(key, "") or "") if isinstance(handler, dict) else ""
        if not command:
            continue
        if _command_option(command, "--managed-by") != "memoryguard":
            continue
        provider = _command_option(command, "--provider").lower()
        agent_id = _command_option(command, "--agent-id")
        group_id = _command_option(command, "--share-group-id")
        workspace = _command_option(command, "--workspace")
        if provider and agent_id and group_id and workspace:
            return (provider, agent_id, group_id, workspace)
    return None


def _generated_binding_events(
    data: dict[str, Any],
    *,
    provider: str,
    workspace: Path,
) -> dict[tuple[str, str], set[str]]:
    """Index generated hook events by exact provider/agent/group/workspace."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {}
    indexed: dict[tuple[str, str], set[str]] = {}

    def collect(event_name: str, handler: Any) -> None:
        binding = _generated_handler_binding(handler)
        if binding is None:
            return
        bound_provider, agent_id, group_id, bound_workspace = binding
        try:
            same_workspace = Path(bound_workspace).expanduser().resolve() == workspace
        except (OSError, RuntimeError, ValueError):
            same_workspace = False
        if bound_provider != provider or not same_workspace:
            return
        indexed.setdefault((agent_id, group_id), set()).add(str(event_name))

    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if _generated_handler_binding(entry) is not None:
                collect(str(event_name), entry)
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for handler in entry["hooks"]:
                collect(str(event_name), handler)
    return indexed


def _remove_generated_bindings(
    data: dict[str, Any],
    *,
    provider: str,
    workspace: Path,
    targets: set[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    """Remove only generated handlers matching one requested agent/group.

    Handles both nested Claude/Codex and direct Cursor hook config shapes.
    A handler must match provider, agent id and group id together, preventing
    a dissolved binding from deleting another active binding on same provider.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    removed: list[tuple[str, str, str]] = []

    def keep(handler: Any) -> bool:
        binding = _generated_handler_binding(handler)
        if binding is None:
            return True
        bound_provider, agent_id, group_id, bound_workspace = binding
        if (
            bound_provider != provider
            or Path(bound_workspace).expanduser().resolve() != workspace
            or (agent_id, group_id) not in targets
        ):
            return True
        removed.append((bound_provider, agent_id, group_id))
        return False

    for event_name in list(hooks):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        kept_entries: list[Any] = []
        for entry in entries:
            if _generated_handler_binding(entry) is not None:
                if keep(entry):
                    kept_entries.append(entry)
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            nested = [handler for handler in entry["hooks"] if keep(handler)]
            if nested:
                copied = dict(entry)
                copied["hooks"] = nested
                kept_entries.append(copied)
        if kept_entries:
            hooks[event_name] = kept_entries
        else:
            hooks.pop(event_name, None)
    if not hooks:
        data.pop("hooks", None)
    return removed


def _owned_hook_hash(data: dict[str, Any]) -> str:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return ""
    owned: list[dict[str, Any]] = []
    for event_name in sorted(hooks):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if _is_our_handler(entry):
                owned.append({"event": event_name, "handler": entry})
                continue
            if not isinstance(entry, dict):
                continue
            handlers = entry.get("hooks")
            if not isinstance(handlers, list):
                continue
            owned_handlers = [
                handler for handler in handlers if _is_our_handler(handler)
            ]
            if owned_handlers:
                owned.append({
                    "event": event_name,
                    "group": {
                        key: value
                        for key, value in entry.items()
                        if key != "hooks"
                    },
                    "handlers": owned_handlers,
                })
    if not owned:
        return ""
    serialized = json.dumps(
        owned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _owned_agent_ids(data: dict[str, Any]) -> set[str]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return set()
    handlers: list[dict[str, Any]] = []
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if _is_our_handler(entry):
                handlers.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks")
            if isinstance(nested, list):
                handlers.extend(
                    handler for handler in nested if _is_our_handler(handler)
                )
    result: set[str] = set()
    pattern = re.compile(
        r"""--agent-id(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s]+))"""
    )
    for handler in handlers:
        command = str(handler.get("commandWindows", "") or "")
        if not command:
            command = str(handler.get("command", "") or "")
        match = pattern.search(command)
        if match:
            result.add(next(value for value in match.groups() if value))
    return result


def _binding_plane_for_workspace(workspace: Path) -> str:
    """Return the authoritative binding plane for the current cutover state.

    V1_ACTIVE/V2_BUILDING are retired for host binding resolution.  They remain
    distinguishable for diagnostics, but callers must not use that result to
    construct or query a retired binding plane.
    """
    from .system.manifest import ManifestManager

    state = ManifestManager(workspace).current().state
    marker = str(getattr(state, "value", state) or "").strip().upper()
    if marker in {"V1_ACTIVE", "V2_BUILDING"}:
        raise ValueError(
            f"v2_upgrade_required: {v2_upgrade_message(marker, surface='Hook')}"
        )
    if marker in {"V2_READY", "V2_ACTIVE"}:
        return "v2"
    raise ValueError(
        "v2_manifest_state_unavailable: "
        f"{v2_upgrade_message('UNKNOWN', surface='Hook')}"
    )


def _validate_binding(
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
) -> None:
    if not agent_instance_id:
        raise ValueError("agent_instance_id is required for hook installation")
    if not share_group_id:
        raise ValueError("share_group_id is required for hook installation")

    plane = _binding_plane_for_workspace(workspace)
    if plane != "v2":
        raise ValueError(v2_upgrade_message(plane, surface="Hook"))

    from .runtime_v2.group_native import GroupControlService

    binding = GroupControlService(workspace, write=False).active_binding_for_agent(
        agent_instance_id
    )
    matched = (
        binding is not None
        and str(binding.get("status") or "") == "active"
        and str(binding.get("share_group_id") or "") == str(share_group_id)
    )

    if not matched:
        raise ValueError(
            "active binding not found for "
            f"agent_instance_id={agent_instance_id!r}, "
            f"share_group_id={share_group_id!r}"
        )


@dataclass(frozen=True)
class HookCapability:
    provider: str
    supported: bool
    config_file: str
    events: tuple[str, ...]
    context_mode: str
    native_write_guard: str
    stop_guard: str
    requires_restart: bool
    requires_trust: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["events"] = list(self.events)
        data["notes"] = list(self.notes)
        return data


class HostHookAdapter:
    """Small interface; host JSON shape and runtime semantics stay inside."""

    provider = "base"

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()

    def config_path(self) -> Path:
        raise NotImplementedError

    def capability(self) -> HookCapability:
        raise NotImplementedError

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def install(
        self,
        *,
        agent_instance_id: str,
        share_group_id: str,
        mode: str = "enforce",
        runtime_python: str | Path | None = None,
    ) -> dict[str, Any]:
        capability = self.capability()
        if not capability.supported:
            return {
                "provider": self.provider,
                "supported": False,
                "configured": False,
                "status": "unsupported",
                "runtime_verified": False,
                "capability": capability.to_dict(),
            }
        _validate_binding(self.workspace, agent_instance_id, share_group_id)
        path = self.config_path()
        data = _load_json_config(path, strict=True)
        data = self._remove_owned(data)
        data = self._add_owned(
            data, agent_instance_id, share_group_id,
            runtime_python=runtime_python,
        )
        _write_json_config(path, data)
        set_hook_mode(
            self.workspace,
            self.provider,
            agent_instance_id,
            mode,
        )
        result = self.status(agent_instance_id=agent_instance_id)
        result["restart_required"] = capability.requires_restart
        result["trust_required"] = capability.requires_trust
        return result

    def uninstall(self) -> dict[str, Any]:
        capability = self.capability()
        if not capability.supported:
            return {
                "provider": self.provider,
                "supported": False,
                "configured": False,
                "status": "unsupported",
                "runtime_verified": False,
                "capability": capability.to_dict(),
            }
        path = self.config_path()
        data = _load_json_config(path, strict=True)
        data = self._remove_owned(data)
        _write_json_config(path, data)
        return {
            "provider": self.provider,
            "supported": True,
            "configured": False,
            "status": "not_configured",
            "runtime_verified": False,
            "config_file": str(path),
            "capability": capability.to_dict(),
        }

    def status(self, *, agent_instance_id: str = "") -> dict[str, Any]:
        capability = self.capability()
        path = self.config_path()
        if not capability.supported:
            return {
                "provider": self.provider,
                "supported": False,
                "configured": False,
                "status": "unsupported",
                "runtime_verified": False,
                "config_file": str(path),
                "capability": capability.to_dict(),
            }
        data = _load_json_config(path, strict=False)
        owned_events = self._owned_events(data)
        expected_events = set(capability.events)
        configured = bool(expected_events) and expected_events.issubset(
            owned_events
        )
        partial = bool(owned_events) and not configured
        resolved_agent_id = agent_instance_id
        if not resolved_agent_id:
            configured_agent_ids = _owned_agent_ids(data)
            if len(configured_agent_ids) == 1:
                resolved_agent_id = next(iter(configured_agent_ids))
        heartbeat = _read_heartbeat(
            self.workspace, self.provider, resolved_agent_id,
        ) if resolved_agent_id else {}
        runtime_verified = configured and _heartbeat_is_current(
            heartbeat,
            _owned_hook_hash(data),
        )
        if runtime_verified:
            status = "operational"
        elif configured:
            status = "configured_pending_runtime"
        elif partial:
            status = "drifted"
        else:
            status = "not_configured"
        return {
            "provider": self.provider,
            "supported": True,
            "configured": configured,
            "installed": configured,
            "drifted": partial,
            "status": status,
            "runtime_verified": runtime_verified,
            "config_file": str(path),
            "last_seen_at": heartbeat.get("at"),
            "last_event": heartbeat.get("event"),
            "last_error": heartbeat.get("error"),
            "agent_instance_id": resolved_agent_id,
            "mode": (
                get_hook_mode(self.workspace, self.provider, resolved_agent_id)
                if resolved_agent_id else "unknown"
            ),
            "capability": capability.to_dict(),
        }

    def _owned_events(self, data: dict[str, Any]) -> set[str]:
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return set()
        owned: set[str] = set()
        for event_name, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if _is_our_handler(entry):
                    owned.add(str(event_name))
                    break
                if isinstance(entry, dict):
                    handlers = entry.get("hooks", [])
                    if isinstance(handlers, list) and any(
                        _is_our_handler(handler) for handler in handlers
                    ):
                        owned.add(str(event_name))
                        break
        return owned


class _NestedJsonHookAdapter(HostHookAdapter):
    """Claude/Codex hooks.json/settings.json nested event shape."""

    def _event_name(self, event: str) -> str:
        return _EVENT_NAMES[event]

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return data
        for event_name in list(hooks):
            groups = hooks.get(event_name)
            if not isinstance(groups, list):
                continue
            kept_groups: list[Any] = []
            for group in groups:
                if not isinstance(group, dict):
                    kept_groups.append(group)
                    continue
                handlers = group.get("hooks")
                if not isinstance(handlers, list):
                    kept_groups.append(group)
                    continue
                kept_handlers = [
                    handler for handler in handlers
                    if not _is_our_handler(handler)
                ]
                if kept_handlers:
                    copied = dict(group)
                    copied["hooks"] = kept_handlers
                    kept_groups.append(copied)
            if kept_groups:
                hooks[event_name] = kept_groups
            else:
                hooks.pop(event_name, None)
        if not hooks:
            data.pop("hooks", None)
        return data

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
        *,
        runtime_python: str | Path | None = None,
    ) -> dict[str, Any]:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("invalid hook config: 'hooks' must be an object")
        for event in (
            "session_start",
            "subagent_start",
            "user_prompt",
            "pre_tool",
            "post_tool",
            "pre_compact",
            "stop",
        ):
            handler: dict[str, Any] = {
                "type": "command",
                "command": _command(
                    self.provider,
                    event,
                    self.workspace,
                    agent_instance_id,
                    share_group_id,
                    windows=False,
                    runtime_python=runtime_python,
                ),
                "timeout": 15,
            }
            if self.provider == "codex":
                handler["commandWindows"] = _command(
                    self.provider,
                    event,
                    self.workspace,
                    agent_instance_id,
                    share_group_id,
                    windows=True,
                    runtime_python=runtime_python,
                )
            if (
                self.provider == "codex"
                and event in {
                    "session_start",
                    "subagent_start",
                    "user_prompt",
                }
            ):
                handler["additionalContextLimit"] = 1800
            hooks.setdefault(self._event_name(event), []).append({
                "hooks": [handler],
            })
        return data


class ClaudeHookAdapter(_NestedJsonHookAdapter):
    provider = "claude"

    def config_path(self) -> Path:
        configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        root = Path(configured).expanduser() if configured else Path.home() / ".claude"
        return root / "settings.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=True,
            config_file=str(self.config_path()),
            events=tuple(_EVENT_NAMES.values()),
            context_mode="per_turn_additional_context",
            native_write_guard="pre_tool_deny_visible_writes",
            stop_guard="single_continuation_for_durable_candidate",
            requires_restart=True,
            requires_trust=False,
            notes=("user settings apply to all projects and subagents",),
        )


class CodexHookAdapter(_NestedJsonHookAdapter):
    provider = "codex"

    def __init__(self, workspace: str | Path):
        super().__init__(workspace)
        self._config_home: Path | None = None

    def set_config_home(self, config_home: str | Path | None) -> None:
        self._config_home = (
            Path(config_home).expanduser().resolve() if config_home else None
        )

    def config_path(self) -> Path:
        if self._config_home is not None:
            return self._config_home / "hooks.json"
        from .agent_locator import current_codex_home

        return current_codex_home() / "hooks.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=True,
            config_file=str(self.config_path()),
            events=tuple(_EVENT_NAMES.values()),
            context_mode="per_turn_additional_context",
            native_write_guard="pre_tool_deny_visible_writes",
            stop_guard="single_continuation_for_durable_candidate",
            requires_restart=True,
            requires_trust=True,
            notes=("open /hooks once after install to trust the exact hook hash",),
        )


class CursorHookAdapter(HostHookAdapter):
    provider = "cursor"

    def config_path(self) -> Path:
        return Path.home() / ".cursor" / "hooks.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=True,
            config_file=str(self.config_path()),
            events=tuple(_CURSOR_EVENT_NAMES.values()),
            context_mode="session_context_plus_first_tool_bootstrap",
            native_write_guard="pre_tool_deny_visible_writes",
            stop_guard="single_followup_for_durable_candidate",
            requires_restart=False,
            requires_trust=False,
            notes=(
                "beforeSubmitPrompt cannot inject dynamic context",
                "pure no-tool replies rely on the alwaysApply MemoryGuard rule",
            ),
        )

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return data
        for event_name in list(hooks):
            entries = hooks.get(event_name)
            if not isinstance(entries, list):
                continue
            kept = [entry for entry in entries if not _is_our_handler(entry)]
            if kept:
                hooks[event_name] = kept
            else:
                hooks.pop(event_name, None)
        if not hooks:
            data.pop("hooks", None)
        return data

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
        *,
        runtime_python: str | Path | None = None,
    ) -> dict[str, Any]:
        data.setdefault("version", 1)
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("invalid hook config: 'hooks' must be an object")
        for event in (
            "session_start",
            "user_prompt",
            "pre_tool",
            "post_tool",
            "pre_compact",
            "stop",
        ):
            entry: dict[str, Any] = {
                "command": _command(
                    self.provider,
                    event,
                    self.workspace,
                    agent_instance_id,
                    share_group_id,
                    runtime_python=runtime_python,
                ),
                # Cursor deadline includes host shell startup and queueing.
                "timeout": 30,
            }
            if event == "pre_tool":
                entry["failClosed"] = True
            if event == "stop":
                entry["loop_limit"] = 1
            hooks.setdefault(_CURSOR_EVENT_NAMES[event], []).append(entry)
        return data


class TraeHookAdapter(HostHookAdapter):
    provider = "trae"

    def config_path(self) -> Path:
        return Path.home() / ".trae" / "hooks.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=False,
            config_file="",
            events=(),
            context_mode="mcp_and_rules_only",
            native_write_guard="unavailable",
            stop_guard="unavailable",
            requires_restart=False,
            requires_trust=False,
            notes=("no verified official user-level lifecycle hook seam",),
        )

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        return data

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        return data


HOOK_ADAPTERS: dict[str, type[HostHookAdapter]] = {
    "claude": ClaudeHookAdapter,
    "claude-code": ClaudeHookAdapter,
    "codex": CodexHookAdapter,
    "cursor": CursorHookAdapter,
    "trae": TraeHookAdapter,
}


def _current_hook_hash(workspace: Path, provider: str) -> str:
    adapter_cls = HOOK_ADAPTERS.get(provider)
    if adapter_cls is None:
        return ""
    adapter = adapter_cls(workspace)
    data = _load_json_config(adapter.config_path(), strict=False)
    return _owned_hook_hash(data)


@dataclass
class HookBindingCleanup:
    """Compensation receipt for exact host-config mutations during dissolve."""

    snapshots: list[tuple[Path, dict[str, Any]]] = field(default_factory=list)
    removed: dict[tuple[str, str, str], int] = field(default_factory=dict)

    def restore(self) -> None:
        for path, data in reversed(self.snapshots):
            _write_json_config(path, data)

    def public_result(self) -> dict[str, Any]:
        bindings = [
            {
                "provider": provider,
                "agent_instance_id": agent_id,
                "share_group_id": group_id,
                "handler_count": count,
            }
            for (provider, agent_id, group_id), count in sorted(self.removed.items())
        ]
        return {
            "binding_count": len(bindings),
            "handler_count": sum(item["handler_count"] for item in bindings),
            "bindings": bindings,
        }


class HostHookManager:
    """Deep module: install, remove, inspect, and execute every host hook."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()

    def adapter(self, provider: str) -> HostHookAdapter:
        normalized = (provider or "").strip().lower()
        adapter_cls = HOOK_ADAPTERS.get(normalized)
        if adapter_cls is None:
            raise ValueError(
                f"unknown hook provider {provider!r}; "
                "supported: claude|codex|cursor|trae"
            )
        return adapter_cls(self.workspace)

    def install(
        self,
        provider: str,
        *,
        agent_instance_id: str,
        share_group_id: str,
        mode: str = "enforce",
        reconcile_trust: bool = False,
        config_home: str | Path | None = None,
        runtime_python: str | Path | None = None,
    ) -> dict[str, Any]:
        normalized = (provider or "").strip().lower()
        adapter = self.adapter(provider)
        if config_home is not None and hasattr(adapter, "set_config_home"):
            adapter.set_config_home(config_home)
        result = adapter.install(
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            mode=mode,
            runtime_python=runtime_python,
        )
        if (
            normalized == "codex"
            and reconcile_trust
            and result.get("configured")
        ):
            from .codex_hook_trust import reconcile_codex_memoryguard_hooks

            trust = reconcile_codex_memoryguard_hooks(
                cwd=self.workspace,
                enabled=True,
            )
            result["hook_trust"] = trust
            result["trust_required"] = not bool(trust.get("verified"))
        return result

    def uninstall(self, provider: str) -> dict[str, Any]:
        return self.adapter(provider).uninstall()

    def remove_generated_bindings(
        self,
        bindings: Iterable[Mapping[str, Any]],
    ) -> "HookBindingCleanup":
        """Remove dissolved-group handlers by exact generated binding identity.

        This intentionally never calls ``uninstall(provider)``: provider
        configuration can contain other active MemoryGuard bindings.
        """
        targets = {
            (str(item.get("agent_instance_id") or "").strip(), str(item.get("share_group_id") or "").strip())
            for item in bindings
            if str(item.get("agent_instance_id") or "").strip()
            and str(item.get("share_group_id") or "").strip()
        }
        cleanup = HookBindingCleanup()
        if not targets:
            return cleanup
        adapters = (
            ("claude", ClaudeHookAdapter),
            ("codex", CodexHookAdapter),
            ("cursor", CursorHookAdapter),
        )
        try:
            for provider, adapter_cls in adapters:
                adapter = adapter_cls(self.workspace)
                path = adapter.config_path()
                # A malformed existing host config cannot prove target Hooks
                # absent.  Fail closed so dissolve never reports success while
                # a stale generated command may still target its shared group.
                # ``_load_json_config`` still returns {} for a missing file.
                data = _load_json_config(path, strict=True)
                original = json.loads(json.dumps(data))
                removed = _remove_generated_bindings(
                    data,
                    provider=provider,
                    workspace=self.workspace,
                    targets=targets,
                )
                if not removed:
                    continue
                cleanup.snapshots.append((path, original))
                _write_json_config(path, data)
                for identity in removed:
                    cleanup.removed[identity] = cleanup.removed.get(identity, 0) + 1
        except Exception:
            cleanup.restore()
            raise
        return cleanup

    def retire_legacy_generated_bindings(
        self,
        bindings: Iterable[Mapping[str, Any]],
        *,
        active_workspace: str | Path,
    ) -> "HookBindingCleanup":
        """Remove obsolete V1-generated bindings without deleting valid V2 hooks.

        Hooks that still point at a retired project workspace are always stale.
        When an in-place upgrade promotes that same directory to the V2 Data
        Home, a complete current-event binding is preserved; only partial/
        legacy registrations are retired. User-owned handlers never match the
        generated binding parser and are therefore untouched.
        """
        targets = {
            (str(item.get("agent_instance_id") or "").strip(), str(item.get("share_group_id") or "").strip())
            for item in bindings
            if str(item.get("agent_instance_id") or "").strip()
            and str(item.get("share_group_id") or "").strip()
        }
        cleanup = HookBindingCleanup()
        if not targets:
            return cleanup
        active_root = Path(active_workspace).expanduser().resolve()
        same_control_root = active_root == self.workspace
        adapters = (
            ("claude", ClaudeHookAdapter),
            ("codex", CodexHookAdapter),
            ("cursor", CursorHookAdapter),
        )
        try:
            for provider, adapter_cls in adapters:
                adapter = adapter_cls(self.workspace)
                path = adapter.config_path()
                data = _load_json_config(path, strict=True)
                removable = set(targets)
                if same_control_root:
                    event_map = _generated_binding_events(
                        data, provider=provider, workspace=self.workspace,
                    )
                    expected = set(adapter.capability().events)
                    removable = {
                        target for target in targets
                        if not expected.issubset(event_map.get(target, set()))
                    }
                if not removable:
                    continue
                original = json.loads(json.dumps(data))
                removed = _remove_generated_bindings(
                    data,
                    provider=provider,
                    workspace=self.workspace,
                    targets=removable,
                )
                if not removed:
                    continue
                cleanup.snapshots.append((path, original))
                _write_json_config(path, data)
                for identity in removed:
                    cleanup.removed[identity] = cleanup.removed.get(identity, 0) + 1
        except Exception:
            cleanup.restore()
            raise
        return cleanup

    def _attach_codex_trust_status(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if not result.get("configured"):
            return result
        from .codex_hook_trust import inspect_codex_memoryguard_hooks

        trust = inspect_codex_memoryguard_hooks(cwd=self.workspace)
        result["hook_trust"] = trust
        verified = bool(trust.get("verified"))
        result["trust_required"] = not verified
        if not verified:
            result["runtime_verified"] = False
            result["status"] = "configured_untrusted"
        return result

    def status(
        self,
        provider: str = "",
        *,
        agent_instance_id: str = "",
        inspect_trust: bool = False,
    ) -> dict[str, Any]:
        if provider:
            normalized = (provider or "").strip().lower()
            result = self.adapter(provider).status(
                agent_instance_id=agent_instance_id,
            )
            if normalized == "codex" and inspect_trust:
                return self._attach_codex_trust_status(result)
            return result
        statuses: list[dict[str, Any]] = []
        for adapter_cls in (
            ClaudeHookAdapter,
            CodexHookAdapter,
            CursorHookAdapter,
            TraeHookAdapter,
        ):
            result = adapter_cls(self.workspace).status(
                agent_instance_id=agent_instance_id,
            )
            if adapter_cls is CodexHookAdapter and inspect_trust:
                result = self._attach_codex_trust_status(result)
            statuses.append(result)
        return {
            "providers": statuses,
            "configured_count": sum(
                bool(item.get("configured")) for item in statuses
            ),
            "operational_count": sum(
                bool(item.get("runtime_verified")) for item in statuses
            ),
        }


def _policy_path(workspace: Path) -> Path:
    return workspace / ".memoryguard" / _RUNTIME_DIR / "policy.json"


def set_hook_mode(
    workspace: str | Path,
    provider: str,
    agent_instance_id: str,
    mode: str,
) -> dict[str, Any]:
    normalized = (mode or "").strip().lower()
    if normalized not in HOOK_MODES:
        raise ValueError(f"invalid hook mode: {mode!r}")
    root = Path(workspace).expanduser().resolve()
    path = _policy_path(root)
    data = _load_json_config(path, strict=True)
    key = f"{provider.lower()}:{agent_instance_id}"
    data[key] = {
        "mode": normalized,
        "updated_at": _now_iso(),
    }
    _write_json_config(path, data)
    return {
        "provider": provider.lower(),
        "agent_instance_id": agent_instance_id,
        "mode": normalized,
    }


def get_hook_mode(
    workspace: str | Path,
    provider: str,
    agent_instance_id: str,
) -> str:
    root = Path(workspace).expanduser().resolve()
    data = _load_json_config(_policy_path(root), strict=False)
    item = data.get(f"{provider.lower()}:{agent_instance_id}", {})
    mode = str(item.get("mode", "enforce") or "enforce").lower()
    return mode if mode in HOOK_MODES else "enforce"


def _runtime_root(workspace: Path) -> Path:
    return workspace / ".memoryguard" / _RUNTIME_DIR


def _state_path(workspace: Path, provider: str, session_id: str) -> Path:
    return (
        _runtime_root(workspace)
        / "state"
        / provider
        / f"{_short_hash(session_id)}.json"
    )


def _heartbeat_path(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
) -> Path:
    return (
        _runtime_root(workspace)
        / "heartbeat"
        / f"{provider}-{_short_hash(agent_instance_id)}.json"
    )


def _load_state(workspace: Path, provider: str, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    return _load_json_config(
        _state_path(workspace, provider, session_id),
        strict=False,
    )


def _save_state(
    workspace: Path,
    provider: str,
    session_id: str,
    state: dict[str, Any],
) -> None:
    if not session_id:
        return
    state = dict(state)
    state["updated_at"] = _now_iso()
    try:
        _write_json_config(_state_path(workspace, provider, session_id), state)
    except Exception as exc:
        _emit_runtime_write_diagnostic("mandatory_state_write_failed", provider, "state", exc)
        raise


def _update_state(
    workspace: Path,
    provider: str,
    session_id: str,
    *,
    updates: Mapping[str, Any] | None = None,
    mutator: Any = None,
) -> dict[str, Any]:
    """Apply a small state mutation under the per-session sidecar lock.

    The lock intentionally covers only the local JSON read/modify/write.  It
    must never surround the native bootstrap call or other host I/O.
    """
    if not session_id:
        return {}
    path = _state_path(workspace, provider, session_id)
    with _cross_process_path_lock(path):
        state = _load_json_config(path, strict=False)
        if callable(mutator):
            mutator(state)
        if updates:
            state.update(dict(updates))
        _save_state(workspace, provider, session_id, state)
        return state


def _record_heartbeat(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    *,
    event: str,
    error: str = "",
    mandatory_rule_ids: list[str] | None = None,
    mandatory_match_receipts: list[dict[str, Any]] | None = None,
    mandatory_overflow: bool = False,
) -> bool:
    path = _heartbeat_path(workspace, provider, agent_instance_id)
    try:
        previous = _load_json_config(path, strict=False)
        payload = {
            "provider": provider,
            "agent_hash": _short_hash(agent_instance_id),
            "hook_version": HOOK_VERSION,
            "hook_hash": _current_hook_hash(workspace, provider),
            "event": event,
            "at": _now_iso(),
            "error": (error or "")[:500],
            "mandatory_rule_ids": list(mandatory_rule_ids or []),
            "mandatory_match_receipts": list(mandatory_match_receipts or []),
            "mandatory_overflow": bool(mandatory_overflow),
        }
        if isinstance(previous.get("history_archive"), dict):
            payload["history_archive"] = previous["history_archive"]
        _write_json_config(path, payload)
        return True
    except Exception as exc:
        _emit_runtime_write_diagnostic("heartbeat_write_failed", provider, event, exc)
        return False


def _emit_runtime_write_diagnostic(kind: str, provider: str, event: str, exc: Exception) -> None:
    print(json.dumps({
        "memoryguard_hook_diagnostic": kind,
        "provider": provider,
        "event": event,
        "error_type": type(exc).__name__,
    }, ensure_ascii=True), file=sys.stderr)


def derive_host_provider(payload: dict[str, Any]) -> str:
    """Provider identity derived from the payload shape alone ("" = unknown).

    ``run_hook`` stamps the provider from argv today without checking the
    payload, so a misconfigured hook can attribute one host's session to
    another (the observed dual-write: Cursor sessions archived as ``claude``).
    Payload shape gives an independent, conservative signal -- Cursor wraps
    lifecycle events in a nested envelope (``event.name`` / ``session`` dict),
    Claude Code emits a top-level ``hook_event_name``, and Codex carries no
    marker (argv stays authoritative there).  Empty string = unknown.
    """
    if not isinstance(payload, dict):
        return ""
    event = payload.get("event")
    if isinstance(event, dict) and isinstance(event.get("name"), str):
        return "cursor"
    if isinstance(payload.get("session"), dict):
        return "cursor"
    hook_event_name = payload.get("hook_event_name")
    if isinstance(hook_event_name, str) and hook_event_name.strip():
        return "claude"
    return ""


def _record_history_diagnostic(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    diagnostic: dict[str, Any],
) -> bool:
    """Attach non-content history coverage data to the hook receipt.

    Runtime receipts are intentionally safe to inspect.  They contain IDs,
    hashes, limits and failure categories only -- never the prompt or model
    response that was archived in the separate history SQLite database.
    """
    path = _heartbeat_path(workspace, provider, agent_instance_id)
    try:
        receipt = _load_json_config(path, strict=False)
        receipt["history_archive"] = dict(diagnostic)
        receipt["at"] = _now_iso()
        _write_json_config(path, receipt)
        return True
    except Exception as exc:
        _emit_runtime_write_diagnostic("history_receipt_write_failed", provider, "history", exc)
        return False


def _codex_active_thread_ids(payload: dict[str, Any]) -> set[str]:
    """Read an optional host-provided active-thread allowlist.

    ``CODEX_THREAD_ID`` remains the only trusted root identity.  The active
    list is merely a conservative no-touch boundary when Codex exposes it in
    a Stop payload or environment; malformed values are ignored.
    """
    from .codex_subagent_reconcile import _active_ids

    values: list[Any] = []
    for key in (
        "active_thread_ids",
        "active_threads",
        "active_subagent_thread_ids",
    ):
        raw = payload.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        elif isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(","))
    return _active_ids(values)


def _record_codex_reconcile_diagnostic(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    result: dict[str, Any],
) -> None:
    """Merge only a sanitized reconciliation summary into the heartbeat."""
    path = _heartbeat_path(workspace, provider, agent_instance_id)
    try:
        from .codex_subagent_reconcile import sanitize_reconcile_result

        receipt = _load_json_config(path, strict=False)
        receipt["codex_subagent_reconcile"] = sanitize_reconcile_result(result)
        receipt["at"] = _now_iso()
        _write_json_config(path, receipt)
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "codex_reconcile_receipt_write_failed", provider, "stop", exc
        )


def _codex_lifecycle_lease_id(payload: dict[str, Any]) -> str:
    """Return a local-only lease identity for Codex MCP cohort ownership.

    ``CODEX_THREAD_ID`` can be inherited by a nested Codex invocation.  Treat
    it as fresh only when it agrees with the Hook's own session identity; a
    mismatch falls back to the existing opaque session hash.  This validation
    is lifecycle-local and never weakens the stricter SQLite reconciliation
    authority, which still requires the host environment value directly.
    """
    from .codex_subagent_reconcile import trusted_codex_thread_id

    session = _session_id(payload)
    trusted = trusted_codex_thread_id()
    if trusted:
        session_root = session.split(":subagent:", 1)[0]
        if session == "unknown-session" or session_root == trusted:
            return f"thread:{trusted}"
    if not session or session == "unknown-session":
        return ""
    return f"session:{_short_hash(session)}"


def _best_effort_codex_mcp_lifecycle(
    *,
    event: str,
    workspace: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run the optional Codex MCP compatibility shim without affecting Hook output."""
    try:
        from .codex_mcp_lifecycle import handle_codex_mcp_lifecycle

        lease_id = _codex_lifecycle_lease_id(payload)
        if not lease_id:
            return {}
        host_thread_id = _verified_codex_hook_thread_id(workspace, payload)
        return handle_codex_mcp_lifecycle(
            event=event,
            workspace=workspace,
            thread_id=lease_id,
            host_thread_id=host_thread_id,
        )
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "codex_mcp_lifecycle_degraded", "codex", event, exc
        )
        return {}


def _attach_terminal_cohort_reclaim(
    workspace: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Reclaim cohorts only for thread ids already proven terminal by Codex state."""

    terminal_ids = {
        str(value)
        for key in (
            "terminal_thread_ids",
            "archived_thread_ids",
            "closed_edge_ids",
            "missing_thread_ids",
        )
        for value in (result.get(key) or [])
        if str(value or "")
    }
    if not terminal_ids:
        result.setdefault("terminal_cohort_reclaim_status", "noop")
        result.setdefault("terminal_cohort_killed_count", 0)
        return result
    try:
        from .codex_mcp_lifecycle import reclaim_terminal_codex_threads

        reclaim = reclaim_terminal_codex_threads(
            workspace=workspace,
            thread_ids=sorted(terminal_ids),
        )
    except Exception as exc:
        result["terminal_cohort_reclaim_status"] = "degraded"
        result["terminal_cohort_reclaim_error"] = type(exc).__name__
        return result
    result["terminal_cohort_reclaim_status"] = str(reclaim.get("status") or "")
    result["terminal_cohort_killed_count"] = len(reclaim.get("killed_pids") or [])
    result["terminal_cohort_failed_count"] = len(reclaim.get("failed_pids") or [])
    result["terminal_cohort_shared_skip_count"] = len(
        reclaim.get("skipped_shared_thread_ids") or []
    )
    result["terminal_cohort_ambiguous_skip_count"] = len(
        reclaim.get("skipped_ambiguous_thread_ids") or []
    )
    return result


def _attach_indexed_terminal_cohort_reclaim(
    workspace: Path,
    result: dict[str, Any],
    *,
    protected_thread_ids: set[str],
) -> dict[str, Any]:
    """Reclaim idle root leases only after Codex archived/deleted their thread row."""

    try:
        from .codex_mcp_lifecycle import reclaim_indexed_terminal_codex_threads

        reclaim = reclaim_indexed_terminal_codex_threads(
            workspace=workspace,
            protected_thread_ids=protected_thread_ids,
        )
    except Exception as exc:
        result["indexed_terminal_reclaim_status"] = "degraded"
        result["indexed_terminal_reclaim_error"] = type(exc).__name__
        return result
    result["indexed_terminal_reclaim_status"] = str(reclaim.get("status") or "")
    result["indexed_terminal_reclaim_count"] = len(
        reclaim.get("reclaimed_thread_ids") or []
    )
    result["indexed_terminal_killed_count"] = len(reclaim.get("killed_pids") or [])
    result["indexed_terminal_failed_count"] = len(reclaim.get("failed_pids") or [])
    return result


def _best_effort_codex_reconcile(
    *,
    workspace: Path,
    agent_instance_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile Codex state without ever affecting the host hook result."""
    try:
        from .codex_subagent_reconcile import (
            codex_thread_matches_workspace,
            reconcile_closed_edge_rollout_activities,
            reconcile_codex_subagents,
            reconcile_global_codex_subagents,
            trusted_codex_thread_id,
        )

        # The environment is host-owned.  Never use payload ``thread_id`` as
        # a fallback because it can be prompt-controlled in synthetic hooks.
        root_thread_id = trusted_codex_thread_id()
        if not root_thread_id:
            return {}
        workspace_matches = codex_thread_matches_workspace(root_thread_id, workspace)
        active = _codex_active_thread_ids(payload) | {root_thread_id}
        result = reconcile_codex_subagents(
            root_thread_id,
            active_thread_ids=active,
            receipt_dir=workspace / ".memoryguard" / _RUNTIME_DIR / "codex-reconcile",
        )
        activity_result = reconcile_closed_edge_rollout_activities(
            result.get("closed_edges") or [],
            active_thread_ids=active,
            # This path runs only from Codex Stop: the current root turn has
            # reached a stable append boundary even though the thread remains
            # resumable and therefore appears in the active whitelist.
            allow_active_parent_ids={root_thread_id},
        )
        result["rollout_activity_appended_count"] = int(
            activity_result.get("appended_count") or 0
        )
        result["rollout_activity_status"] = str(
            activity_result.get("status") or ""
        )
        result = _attach_terminal_cohort_reclaim(workspace, result)
        global_result = (
            reconcile_global_codex_subagents(
                active_thread_ids=active,
                receipt_dir=workspace / ".memoryguard" / _RUNTIME_DIR / "codex-reconcile",
            )
            if workspace_matches
            else {
                "status": "skipped",
                "reason": "thread_workspace_mismatch",
                "degraded": False,
                "closed_edge_count": 0,
                "archived_thread_count": 0,
            }
        )
        if workspace_matches:
            global_activity = reconcile_closed_edge_rollout_activities(
                global_result.get("closed_edges") or [],
                active_thread_ids=active,
                allow_active_parent_ids={root_thread_id},
            )
            global_result["rollout_activity_appended_count"] = int(
                global_activity.get("appended_count") or 0
            )
            global_result["rollout_activity_status"] = str(
                global_activity.get("status") or ""
            )
        global_result = _attach_terminal_cohort_reclaim(workspace, global_result)
        global_result = _attach_indexed_terminal_cohort_reclaim(
            workspace,
            global_result,
            protected_thread_ids={root_thread_id},
        )
        # Root Stop reconciliation remains the primary receipt.  Aggregate the
        # terminal-event sweep counts without copying global thread IDs into a
        # workspace heartbeat.
        result["global_reconcile"] = True
        result["global_status"] = str(global_result.get("status") or "")
        result["global_degraded"] = bool(global_result.get("degraded"))
        result["closed_edge_count"] = int(result.get("closed_edge_count") or 0) + int(
            global_result.get("closed_edge_count") or 0
        )
        result["archived_thread_count"] = int(
            result.get("archived_thread_count") or 0
        ) + int(global_result.get("archived_thread_count") or 0)
        result["open_edge_count"] = int(global_result.get("open_edge_count") or 0)
        result["skipped_nonterminal_count"] = int(
            global_result.get("skipped_nonterminal_count") or 0
        )
        result["terminal_event_counts"] = dict(
            global_result.get("terminal_event_counts") or {}
        )
        result["terminal_cohort_killed_count"] = int(
            result.get("terminal_cohort_killed_count") or 0
        ) + int(global_result.get("terminal_cohort_killed_count") or 0)
        result["terminal_cohort_failed_count"] = int(
            result.get("terminal_cohort_failed_count") or 0
        ) + int(global_result.get("terminal_cohort_failed_count") or 0)
        _record_codex_reconcile_diagnostic(
            workspace, "codex", agent_instance_id, result
        )
        if result.get("degraded") or global_result.get("degraded"):
            _emit_runtime_write_diagnostic(
                "codex_subagent_reconcile_degraded", "codex", "stop",
                RuntimeError(
                    str(
                        global_result.get("reason")
                        or result.get("reason")
                        or global_result.get("status")
                        or result.get("status")
                    )
                ),
            )
        return result
    except Exception as exc:
        # Stop is an observational seam; a state DB failure must not suppress
        # MemoryGuard's own feedback/continuation path or brick the session.
        _emit_runtime_write_diagnostic(
            "codex_subagent_reconcile_failed", "codex", "stop", exc
        )
        return {
            "version": "1",
            "provider": "codex",
            "ok": False,
            "degraded": True,
            "status": "degraded",
            "reason": f"reconcile_failed:{type(exc).__name__}",
        }


def _verified_codex_hook_thread_id(
    workspace: Path,
    payload: dict[str, Any],
) -> str:
    """Authenticate a Codex Hook thread for lifecycle/terminal-sweep authority.

    ``workspace`` may be MemoryGuard's control directory rather than the
    Codex thread cwd. Prefer the Hook payload cwd/project path when present;
    otherwise keep the older workspace-bound check. A host-owned
    ``CODEX_THREAD_ID`` still has to belong to that effective cwd. If Codex
    omits it, the Hook session id must pass the same read-only state DB check.
    """

    try:
        from .codex_subagent_reconcile import (
            codex_thread_matches_workspace,
            trusted_codex_thread_id,
        )

        hook_cwd = str(
            payload.get("cwd")
            or payload.get("project_ref")
            or payload.get("projectRef")
            or workspace
            or ""
        ).strip()
        trusted = trusted_codex_thread_id()
        if (
            trusted
            and hook_cwd
            and codex_thread_matches_workspace(trusted, hook_cwd)
        ):
            return trusted
        session = _session_id(payload).split(":subagent:", 1)[0]
        if (
            session
            and session != "unknown-session"
            and hook_cwd
            and codex_thread_matches_workspace(session, hook_cwd)
        ):
            return session
    except Exception:
        return ""
    return ""


def _best_effort_codex_global_reconcile(
    *,
    workspace: Path,
    agent_instance_id: str,
    payload: dict[str, Any],
    event: str = "session_start",
) -> dict[str, Any]:
    """Repair terminal/orphan stale tasks without touching live branches."""

    try:
        from .codex_subagent_reconcile import (
            reconcile_closed_edge_rollout_activities,
            reconcile_global_codex_subagents,
        )

        root_thread_id = _verified_codex_hook_thread_id(workspace, payload)
        if not root_thread_id:
            return {}
        active = _codex_active_thread_ids(payload) | {root_thread_id}
        result = reconcile_global_codex_subagents(
            active_thread_ids=active,
            receipt_dir=workspace / ".memoryguard" / _RUNTIME_DIR / "codex-reconcile",
        )
        activity_result = reconcile_closed_edge_rollout_activities(
            result.get("closed_edges") or [],
            active_thread_ids=active,
        )
        result["rollout_activity_appended_count"] = int(
            activity_result.get("appended_count") or 0
        )
        result["rollout_activity_status"] = str(
            activity_result.get("status") or ""
        )
        result = _attach_terminal_cohort_reclaim(workspace, result)
        result = _attach_indexed_terminal_cohort_reclaim(
            workspace,
            result,
            protected_thread_ids={root_thread_id},
        )
        _record_codex_reconcile_diagnostic(
            workspace, "codex", agent_instance_id, result
        )
        if result.get("degraded"):
            _emit_runtime_write_diagnostic(
                "codex_subagent_global_reconcile_degraded",
                "codex",
                event,
                RuntimeError(str(result.get("reason") or result.get("status"))),
            )
        return result
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "codex_subagent_global_reconcile_failed",
            "codex",
            event,
            exc,
        )
        return {
            "version": "2",
            "provider": "codex",
            "ok": False,
            "degraded": True,
            "status": "degraded",
            "reason": f"reconcile_failed:{type(exc).__name__}",
        }


def _best_effort_codex_profile_repair(
    *,
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
) -> dict[str, Any]:
    """Repair the current CODEX_HOME profile to the canonical program identity."""
    if not str(os.environ.get("CODEX_HOME", "") or "").strip():
        return {}
    try:
        from .agent_locator import AgentLocator
        from .provider_adapters import (
            _record_codex_program_identity,
            _repair_discovered_codex_homes,
            _resolve_codex_canonical_identity,
        )
        from .runtime_v2.group_native import GroupControlService

        store = GroupControlService(workspace, write=False)
        instances, _ = AgentLocator(workspace).detect_instances()
        canonical, group, aliases = _resolve_codex_canonical_identity(store, instances)
        binding = None
        if canonical:
            binding = store.active_binding_for_agent(canonical)
        if binding is None:
            binding = store.active_binding_for_agent(agent_instance_id)
        if binding is None:
            return {}
        agent_id = str(binding.get("agent_instance_id") or canonical or agent_instance_id)
        group_id = str(binding.get("share_group_id") or group or share_group_id)
        replica = _repair_discovered_codex_homes(
            workspace,
            agent_instance_id=agent_id,
            share_group_id=group_id,
            binding_id=str(binding.get("binding_id") or ""),
        )
        try:
            recorded = _record_codex_program_identity(
                workspace,
                agent_instance_id=agent_id,
                share_group_id=group_id,
                aliases=list(aliases) + list(replica.get("aliases") or ()),
            )
            replica["aliases"] = list(recorded.get("aliases") or replica.get("aliases") or ())
        except Exception:
            pass
        return replica
    except Exception:
        return {}


def _read_heartbeat(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
) -> dict[str, Any]:
    if not agent_instance_id:
        return {}
    return _load_json_config(
        _heartbeat_path(workspace, provider, agent_instance_id),
        strict=False,
    )


def _heartbeat_is_current(
    heartbeat: dict[str, Any],
    hook_hash: str,
) -> bool:
    if not heartbeat or heartbeat.get("error"):
        return False
    if str(heartbeat.get("hook_version", "")) != HOOK_VERSION:
        return False
    if not hook_hash or str(heartbeat.get("hook_hash", "")) != hook_hash:
        return False
    try:
        timestamp = datetime.fromisoformat(str(heartbeat["at"]))
        age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
        return age.days <= _MAX_RECEIPT_AGE_DAYS
    except (KeyError, TypeError, ValueError):
        return False


def _session_id(payload: dict[str, Any]) -> str:
    base = str(
        payload.get("session_id")
        or payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("sessionId")
        or payload.get("thread_id")
        or "unknown-session"
    )
    subagent = str(
        payload.get("subagent_id")
        or payload.get("agent_id")
        or ""
    )
    return f"{base}:subagent:{subagent}" if subagent else base


def _host_subagent_id(payload: dict[str, Any], agent_instance_id: str) -> str:
    """Return the host child identity without accepting it as a bound Agent.

    Codex uses ``agent_id`` for a child on PreToolUse while other adapters use
    ``subagent_id``.  A root event may also contain the configured Agent ID;
    that is not a child identity and must remain a root scope.
    """
    explicit = str(payload.get("subagent_id") or "").strip()
    if explicit:
        return explicit
    candidate = str(payload.get("agent_id") or "").strip()
    return candidate if candidate and candidate != str(agent_instance_id) else ""


_V2_HOOK_UNTRUSTED_IDENTITY_KEYS = frozenset({
    "workspace_id", "workspace",
    "agent_instance_id", "agent_id", "agent", "trusted_agent_id", "trusted_agent",
    "share_group_id", "group_id", "group", "trusted_group_id", "trusted_group",
    "project_ref", "project_id", "project", "trusted_project_ref", "trusted_project",
    "provider", "trusted_provider",
    "runtime_role", "runtime", "trusted_runtime_role", "trusted_runtime",
    "runtime_agent_id", "parent_agent_id",
    "session_id", "conversation_id", "conversationId", "sessionId", "thread_id",
    "session_source", "session_trusted", "context_hash",
    "trusted_identity", "trusted_context", "identity", "entrypoint",
    # The subagent selector is host metadata too.  Its trusted projection is
    # issued by ``_effective_agent_context`` above, not forwarded as request
    # data to the native identity validator.
    "subagent_id",
})


def _v2_hook_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove host identity claims before native V2 dispatch.

    The Hook command arguments are the authority for provider/bound Agent/
    group.  Host payload identities are useful only to derive a separate
    runtime-child context and must never be treated as an RPC caller trying to
    select a MemoryGuard binding.
    """
    return {
        key: value for key, value in payload.items()
        if key not in _V2_HOOK_UNTRUSTED_IDENTITY_KEYS
    }


def _prompt(payload: dict[str, Any]) -> str:
    return str(
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("initial_prompt")
        or ""
    )


_HISTORY_PRIVATE_KEYS = (
    "private", "sensitive", "do_not_archive", "history_disabled",
    "memoryguard_history_disabled", "incognito",
)


def _history_opted_out(payload: dict[str, Any]) -> bool:
    """Honor only explicit host-supplied privacy/disable indicators."""
    containers = [payload]
    for key in ("metadata", "context", "conversation"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in _HISTORY_PRIVATE_KEYS:
            value = container.get(key)
            if value is True or str(value).strip().casefold() in {"1", "true", "yes", "on"}:
                return True
    return False


def _history_capture_enabled(workspace: Path) -> tuple[bool, str]:
    env_value = os.environ.get("MEMORYGUARD_HISTORY_ENABLED", "").strip().casefold()
    if env_value in {"0", "false", "no", "off"}:
        return False, "disabled_by_env"
    config = _load_json_config(
        workspace / ".memoryguard" / "history" / "config.json",
        strict=False,
    )
    if config.get("enabled") is False:
        return False, "disabled_by_config"
    return True, "enabled"


def _stable_history_event_id(payload: dict[str, Any], event: str) -> tuple[str, bool, str]:
    for key in ("turn_id", "message_id", "generation_id", "event_id"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return f"{event}:id:{key}:{str(value).strip()}", True, key
    # Some verified seams expose a monotonic sequence/offset but no opaque ID.
    for key in ("sequence", "sequence_id", "turn_index", "message_index", "offset"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return f"{event}:sequence:{key}:{str(value).strip()}", True, key
    return "", False, "unavailable"


def _contains_obvious_secret(content: str) -> bool:
    from .auto_organizer import SECRET_PATTERNS
    return any(pattern.search(content) for pattern in SECRET_PATTERNS)


def _hook_history_session_id(payload: dict[str, Any]) -> str:
    """Use a verified host session identity; never coalesce unknown chats."""
    base = str(payload.get("session_id") or payload.get("conversation_id") or "").strip()
    if not base:
        return ""
    subagent = str(payload.get("subagent_id") or payload.get("agent_id") or "").strip()
    return f"{base}:subagent:{subagent}" if subagent else base


def _archive_history_event(
    *,
    workspace: Path,
    provider: str,
    event: str,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Retired compatibility seam; history persistence is V2-native only."""
    return {
        "attempted": False,
        "archived": False,
        "reason": "v2_native_only",
        "event": event,
    }



def _durable_candidate(text: str) -> bool:
    value = (text or "").strip()
    return bool(value) and any(pattern.search(value) for pattern in _DURABLE_PATTERNS)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    return []


def _normalized_strings(value: Any) -> list[str]:
    return [
        item.replace("\\", "/").casefold()
        for item in _flatten_strings(value)
        if item
    ]


def _targets_native_memory(tool_name: str, tool_input: Any) -> bool:
    normalized_tool = (tool_name or "").casefold()
    strings = _normalized_strings(tool_input)
    if "apply_patch" in normalized_tool:
        # Source text is not a destination. Inspect every patch file header,
        # including move targets, without treating example paths as writes.
        headers = [
            match.group(1).strip()
            for value in _flatten_strings(tool_input)
            for match in re.finditer(
                r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$",
                value, re.MULTILINE,
            )
        ]
        if headers:
            strings = _normalized_strings(headers)
    elif any(token in normalized_tool for token in ("write", "edit", "delete")):
        mapping = _coerce_mapping(tool_input)
        paths = [
            mapping[key] for key in (
                "path", "file_path", "filepath", "target_file", "paths",
                "old_path", "new_path", "source", "destination",
            ) if key in mapping
        ]
        if paths:
            strings = _normalized_strings(paths)
    strings = ["/" + value.lstrip("/") for value in strings]
    if not any(
        pattern.search(value)
        for value in strings
        for pattern in _NATIVE_PATH_PATTERNS
    ):
        return False
    if any(
        token in normalized_tool
        for token in ("write", "edit", "delete", "apply_patch")
    ):
        return True
    if normalized_tool in {"bash", "shell"}:
        return any(_SHELL_WRITE_PATTERN.search(value) for value in strings)
    return False


_MEMORYGUARD_MCP_PREFIXES = (
    "",
    "memoryguard:",
    "memoryguard/",
    "memoryguard-",
    "user-memoryguard:",
    "user-memoryguard/",
    "user-memoryguard-",
)


def _is_memoryguard_tool(tool_name: str, operation: str = "") -> bool:
    operation = operation.casefold()
    if not operation:
        return False
    expected = f"memoryguard_{operation}"
    value = (tool_name or "").casefold()
    if value in {
        expected,
        f"mcp__memoryguard__{expected}",
        f"mcp:{expected}",
    }:
        return True
    if value.startswith("mcp:") and value.endswith(expected):
        return value[4:-len(expected)] in _MEMORYGUARD_MCP_PREFIXES
    return False


_MEMORYGUARD_CURSOR_SERVER_NAMES = frozenset({
    "memoryguard",
    "user-memoryguard",
})


def _coerce_mapping(value: Any) -> dict[str, Any]:
    """Accept host-structured mappings, including JSON-string Cursor inputs."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 2 and text[0] == "{" and text[-1] == "}":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                try:
                    parsed = json.loads(_repair_json_invalid_escapes(text))
                except json.JSONDecodeError:
                    return {}
            if isinstance(parsed, dict):
                return parsed
    return {}


def _is_cursor_mcp_wrapper(tool_name: str) -> bool:
    raw = (tool_name or "").casefold()
    compact = raw.replace("_", "").replace("-", "").replace(":", "").replace(" ", "")
    return compact in {"callmcptool", "calldynamictool"}


def _cursor_mcp_wrapper_fields(tool_name: str, tool_input: Any) -> tuple[str, str]:
    """Return server/tool fields from a Cursor MCP wrapper.

    Cursor may send several aliases at once (``namespace`` plus
    ``serverName``, ``toolName`` plus wrapper ``name``). Trust the first
    MemoryGuard server alias and prefer ``toolName`` over generic ``name``.
    """
    if not _is_cursor_mcp_wrapper(tool_name):
        return "", ""
    mapping = _coerce_mapping(tool_input)
    arguments = _coerce_mapping(mapping.get("arguments"))
    containers = (mapping, arguments)

    servers: list[str] = []
    for container in containers:
        for key in ("serverName", "server_name", "server", "namespace"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                servers.append(value.strip())
    allowed = [
        item for item in servers
        if item.casefold() in _MEMORYGUARD_CURSOR_SERVER_NAMES
    ]
    unique_servers = {item.casefold() for item in servers}
    if allowed:
        server = allowed[0]
    elif len(unique_servers) == 1:
        server = servers[0]
    else:
        server = ""

    inner = ""
    for container in containers:
        for key in ("toolName", "tool_name"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                inner = value.strip()
                break
        if inner:
            break
    if not inner:
        for container in containers:
            value = container.get("name")
            if (
                isinstance(value, str)
                and value.strip()
                and "memoryguard_" in value.casefold()
            ):
                inner = value.strip()
                break
    return server, inner


def _cursor_mcp_inner_tool_name_unverified(tool_name: str, tool_input: Any) -> str:
    """Read a wrapper's host field for deny classification, never for trust."""
    _server, inner = _cursor_mcp_wrapper_fields(tool_name, tool_input)
    return inner


def _cursor_mcp_inner_tool_name(
    tool_name: str,
    tool_input: Any = None,
    *,
    operation: str,
) -> str:
    """Return an exact MemoryGuard operation from a verified Cursor wrapper."""
    server, inner = _cursor_mcp_wrapper_fields(tool_name, tool_input)
    expected = f"memoryguard_{operation.casefold()}"
    if (
        server.casefold() not in _MEMORYGUARD_CURSOR_SERVER_NAMES
        or inner.casefold() != expected
    ):
        return ""
    return expected


def _hook_tool_parts(payload: dict[str, Any]) -> tuple[str, Any]:
    """Read tool name/input from flat Cursor payloads and nested envelopes."""
    name = str(payload.get("tool_name") or payload.get("toolName") or "")
    raw_input = payload.get("tool_input", payload.get("toolInput"))
    if raw_input is None:
        raw_input = payload.get("arguments", payload.get("input"))
    tool = payload.get("tool")
    if isinstance(tool, str) and not name:
        name = tool
    if isinstance(tool, dict):
        name = name or str(
            tool.get("name") or tool.get("toolName") or tool.get("tool_name") or ""
        )
        if raw_input in (None, "", {}):
            raw_input = (
                tool.get("input")
                or tool.get("args")
                or tool.get("arguments")
                or tool.get("tool_input")
            )
    event = payload.get("event")
    if isinstance(event, dict):
        name = name or str(event.get("tool_name") or event.get("toolName") or "")
        ev_tool = event.get("tool")
        if isinstance(ev_tool, dict):
            name = name or str(
                ev_tool.get("name") or ev_tool.get("toolName") or ""
            )
            if raw_input in (None, "", {}):
                raw_input = (
                    ev_tool.get("input")
                    or ev_tool.get("args")
                    or ev_tool.get("arguments")
                )
    if not name:
        fallback = payload.get("name")
        if isinstance(fallback, str) and fallback.strip():
            name = fallback.strip()
    parsed = _coerce_mapping(raw_input)
    if parsed:
        raw_input = parsed
    return name, raw_input


def _is_memoryguard_bootstrap(tool_name: str, tool_input: Any = None) -> bool:
    if _is_memoryguard_tool(tool_name, "context_bootstrap"):
        return True
    return bool(
        _cursor_mcp_inner_tool_name(
            tool_name, tool_input, operation="context_bootstrap"
        )
    )


def _is_memoryguard_write(tool_name: str, tool_input: Any = None) -> bool:
    if _is_memoryguard_tool(tool_name, "memory_write"):
        return True
    return bool(
        _cursor_mcp_inner_tool_name(
            tool_name, tool_input, operation="memory_write"
        )
    )


_RECOVERY_TOOL_OPERATIONS = frozenset({
    "context_bootstrap",
    "memory_status", "memory_search", "memory_read", "memory_update", "memory_delete",
    "canonical_status", "projection_status", "diagnostics_snapshot", "runtime_processes",
    "audit", "explain", "semantic_check", "list_sources", "binding_list",
    "rule_scope_stats", "rule_decision_read", "rule_feedback", "rule_undo",
    "rule_merge_acknowledge", "rule_merge_approve", "rule_merge_capability_issue",
    "rule_merge_cooldown_clear",
})

_VERIFIED_MEMORYGUARD_OPERATIONS = _RECOVERY_TOOL_OPERATIONS | frozenset({
    "memory_write",
})


def _is_verified_memoryguard_tool(tool_name: str, tool_input: Any = None) -> bool:
    """Accept only explicitly supported MemoryGuard host tool identities."""
    return any(
        _is_memoryguard_tool(tool_name, operation)
        or _cursor_mcp_inner_tool_name(tool_name, tool_input, operation=operation)
        for operation in _VERIFIED_MEMORYGUARD_OPERATIONS
    )


def _is_memoryguard_recovery_tool(tool_name: str, tool_input: Any = None) -> bool:
    """Allow only bounded MemoryGuard repair surfaces through a broken hook.

    The MCP/native V2 boundary still performs its own state, identity and
    governance checks.  This helper merely prevents the host PreToolUse hook
    from making those repair APIs unreachable when the context package itself
    is the thing that needs repair.
    """

    for operation in _RECOVERY_TOOL_OPERATIONS:
        if _is_memoryguard_tool(tool_name, operation) or _cursor_mcp_inner_tool_name(
            tool_name, tool_input, operation=operation
        ):
            return True
    return False


def _best_effort_codegraph_file_refresh(
    *,
    workspace: Path,
    payload: dict[str, Any],
    tool_name: str,
    tool_input: Any,
    tool_result: Any,
) -> None:
    try:
        from .codegraph_v2.refresh import queue_host_file_refresh

        result = queue_host_file_refresh(
            workspace,
            payload=payload,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            host_event="post_tool",
            trusted_host=True,
        )
        if result.get("reason") == "codegraph_project_not_built":
            from .codegraph_v2.automation import ensure_graph
            ensure_graph(workspace, str(payload.get("share_group_id") or ""),
                str(payload.get("cwd") or ""), agent=str(payload.get("agent_instance_id") or ""),
                provider=str(payload.get("provider") or "graphify"), retry=True)
    except Exception:
        return


def _is_other_memory_write(tool_name: str, tool_input: Any = None) -> bool:
    """Block non-MemoryGuard MCP memory writers without substring trust."""
    parsed_input = _coerce_mapping(tool_input) or tool_input
    if _is_verified_memoryguard_tool(tool_name, parsed_input):
        return False
    direct = (tool_name or "").casefold()
    is_wrapper = _is_cursor_mcp_wrapper(tool_name)
    if is_wrapper and not isinstance(parsed_input, dict):
        # Cursor MCP calls must carry a structured wrapper. An opaque blob
        # cannot prove it is a read-only non-MemoryGuard operation.
        return True
    wrapped = _cursor_mcp_inner_tool_name_unverified(tool_name, parsed_input).casefold()
    candidates = [direct]
    if wrapped:
        candidates.append(wrapped)
    for value in candidates:
        is_mcp = is_wrapper or value.startswith("mcp__") or value.startswith("mcp:")
        is_write = any(
            token in value
            for token in ("write", "add", "create", "update", "delete", "store", "remember")
        )
        if is_mcp and "memory" in value and is_write:
            return True
    return False


def _load_store(
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
):
    raise RuntimeError(v2_upgrade_message("UNKNOWN", surface="Hook"))


def _budget_count_warning_message(packet: Mapping[str, Any] | None) -> str:
    if not isinstance(packet, Mapping):
        return ""
    budget = packet.get("budget")
    if not isinstance(budget, Mapping):
        nested = packet.get("context_packet")
        if isinstance(nested, Mapping):
            budget = nested.get("budget")
    if not isinstance(budget, Mapping):
        return ""
    for warning in budget.get("warnings") or ():
        if not isinstance(warning, Mapping):
            continue
        if str(warning.get("code") or "") != "mandatory_item_count_warning":
            continue
        message = str(warning.get("message") or "").strip()
        if message:
            return message
    return ""


_BUDGET_RECOVERY_OPERATIONS = frozenset({
    "context_bootstrap",
    "memory_status", "memory_search", "memory_read",
    "canonical_status", "projection_status", "diagnostics_snapshot", "runtime_processes",
    "audit", "explain", "semantic_check", "list_sources", "binding_list",
    "rule_scope_stats", "rule_decision_read",
})


def _tool_matches_operation(tool_name: str, tool_input: Any, operation: str) -> bool:
    return _is_memoryguard_tool(tool_name, operation) or bool(
        _cursor_mcp_inner_tool_name(tool_name, tool_input, operation=operation)
    )


def _is_update_preview(tool_input: Any) -> bool:
    """True only for an explicit non-committing memory_update preview."""
    mapping = _coerce_mapping(tool_input)
    candidates = [mapping, _coerce_mapping(mapping.get("arguments"))]
    return any(item.get("preview") is True for item in candidates)


def _is_budget_recovery_tool(tool_name: str, tool_input: Any = None) -> bool:
    """Tools that stay reachable while the mandatory package is over budget.

    Ordinary task tools stay blocked. Committing writes, deletes, and merge
    approvals stay blocked too. Diagnostics, reads, update preview, and
    bootstrap remain so a bound agent can inspect and re-bootstrap after the
    host finishes controlled governance.
    """
    for operation in _BUDGET_RECOVERY_OPERATIONS:
        if _tool_matches_operation(tool_name, tool_input, operation):
            return True
    return _tool_matches_operation(
        tool_name, tool_input, "memory_update"
    ) and _is_update_preview(tool_input)


def _mandatory_overflow_block_message(reason: str = "") -> str:
    detail = str(reason or "").strip() or "mandatory_budget_exceeded"
    return (
        "MemoryGuard 强制规则包超出预算，普通任务工具已阻断。"
        f"原因：{detail}。"
        "仍允许：memoryguard_context_bootstrap、"
        "memoryguard_memory_read、memoryguard_memory_search、memoryguard_memory_status、"
        "memoryguard_audit、memoryguard_explain、memoryguard_diagnostics_snapshot、"
        "memoryguard_memory_update（仅 preview=true）。"
        "普通任务工具保持阻断，直到受控治理完成且真实 bootstrap 成功。"
    )


def _render_context(packet: dict[str, Any]) -> str:
    context_packet = packet.get("context_packet", {})
    items = context_packet.get("items", [])
    mandatory_items = context_packet.get("mandatory_items", [])
    if packet.get("mandatory_overflow"):
        return _mandatory_overflow_block_message(
            str(packet.get("error") or packet.get("mandatory_invalid_reason") or "")
        )
    lines = [
        "[MemoryGuard 强制规则（必须遵循）]",
    ]
    if mandatory_items:
        for item in mandatory_items:
            body = " ".join(str(item.get("body", "")).split())
            if body:
                lines.append(f"- {item.get('kind', 'fact')}: {body}")
    else:
        lines.append("- 本轮没有生效的强制规则。")
    warning_message = _budget_count_warning_message(packet) or _budget_count_warning_message(
        context_packet if isinstance(context_packet, dict) else None
    )
    if warning_message:
        lines.append(f"- {warning_message}")
    lines.extend([
        "[MemoryGuard 相关长期记忆]",
        "仅用于补充长期规则/偏好/项目决策；当前宿主对话保持原样。",
    ])
    for item in items:
        body = " ".join(str(item.get("body", "")).split())
        if not body:
            continue
        lines.append(f"- {item.get('kind', 'fact')}: {body}")
    if len(lines) == 2:
        lines.append("- 本轮没有匹配的长期记忆。")
    lines.append(
        "长期记忆写入只使用 memoryguard_memory_write；"
        "不得写入宿主原生记忆文件。"
    )
    codegraph = packet.get("codegraph") or context_packet.get("codegraph") or {}
    if isinstance(codegraph, dict) and codegraph.get("message"):
        lines.append("[项目工具] " + str(codegraph["message"])[:600])
    return "\n".join(lines)


def _static_session_context(provider: str) -> str:
    base = (
        "MemoryGuard Hook 已启用。长期记忆与长期规则的唯一真相源是 "
        "MemoryGuard；当前对话上下文仍由宿主管理，不复制为长期记忆。"
    )
    if provider == "cursor":
        return (
            base
            + " Cursor 当前不能在 beforeSubmitPrompt 动态注入上下文；"
            "首次工具调用前必须先调用 memoryguard_context_bootstrap。"
        )
    return base


def _context_output(provider: str, event: str, text: str) -> dict[str, Any]:
    if provider == "cursor":
        if event == "session_start":
            return {"additional_context": text}
        if event == "user_prompt":
            return {"continue": True}
        if event == "pre_compact":
            return {"user_message": text}
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT_NAMES[event],
            "additionalContext": text,
        }
    }


def _normalize_feedback_receipts(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        receipt_id = str(item.get("receipt_id", "")).strip()
        if not receipt_id:
            continue
        normalized = {
            "receipt_id": receipt_id,
            "memory_id": str(item.get("memory_id", "") or "").strip(),
        }
        items.append(normalized)
    return items


def _persist_mandatory_match_receipts(
    *,
    store: Any,
    receipts: list[dict[str, Any]],
    provider: str,
    event: str,
) -> None:
    """Retired compatibility seam; native V2 owns receipt persistence."""
    return None


def _flush_pending_rule_feedback(
    *,
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    session_id: str,
    actor: str,
    trigger: str,
) -> None:
    """Retired compatibility seam; native V2 owns feedback persistence."""
    return None



def _deny_output(provider: str, reason: str) -> dict[str, Any]:
    if provider == "cursor":
        return {
            "permission": "deny",
            "user_message": reason,
            "agent_message": reason,
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _allow_output(provider: str, event: str) -> dict[str, Any]:
    if provider == "cursor" and event == "user_prompt":
        return {"continue": True}
    return {}


def _v2_upgrade_output(provider: str, event: str, state: str) -> dict[str, Any]:
    """Fail closed for non-V2 runtime work; Stop is lifecycle-only fail-open."""
    if event == "stop":
        return {}
    payload = v2_upgrade_payload(state, surface="Hook")
    reason = v2_upgrade_message(state, surface="Hook")
    if event == "pre_tool":
        result = _deny_output(provider, reason)
    elif provider == "cursor" and event == "user_prompt":
        result = {"continue": False}
    else:
        result = _context_output(provider, event, reason)
    result.update(payload)
    nested = result.get("hookSpecificOutput")
    if isinstance(nested, dict):
        nested.update({
            "code": payload["code"],
            "error": payload["error"],
            "state": payload["state"],
            "next_step": payload["next_step"],
        })
    return result


# ---------------------------------------------------------------------------
# Phase6 V2 host-hook seam
# ---------------------------------------------------------------------------

_V2_STATES = frozenset({"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"})
_V2_READ_STATES = frozenset({"V2_READY", "V2_ACTIVE"})
_V2_FACADE_MISSING = object()
_v2_runtime_facade_factory: Any = None


def _load_v2_runtime_facade(workspace: Path) -> Any:
    """Load the native V2 facade; never fall back to retired storage."""
    factory = globals().get("_v2_runtime_facade_factory")
    if callable(factory):
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("v2_runtime_factory_signature_unavailable") from exc
        try:
            signature.bind(workspace)
        except TypeError:
            try:
                signature.bind(workspace=workspace)
            except TypeError as exc:
                raise RuntimeError("v2_runtime_factory_signature_unavailable") from exc
            return factory(workspace=workspace)
        return factory(workspace)

    from .cutover_v2.facade import get_v2_runtime_facade
    return get_v2_runtime_facade(str(workspace))


def _v2_state(value: Any) -> str:
    # Host hooks consume an injected facade snapshot.  Require the same
    # trusted RuntimeSnapshot factory semantics as MCP; malformed generation,
    # unavailable/error envelopes, and hand-built snapshots fail closed.
    try:
        from .cutover_v2.state import CutoverState, RuntimeSnapshot
        if isinstance(value, RuntimeSnapshot):
            if not value.trusted or not value.available:
                return "UNKNOWN"
            return value.state.value if value.generation >= 0 else "UNKNOWN"
        if isinstance(value, CutoverState):
            return "UNKNOWN"
        if isinstance(value, dict) and any(key in value for key in ("state", "manifest_state", "status", "marker")):
            snapshot = RuntimeSnapshot.from_value(value)
            return snapshot.state.value if snapshot.available else "UNKNOWN"
        if hasattr(value, "state"):
            snapshot = RuntimeSnapshot.from_value(value)
            return snapshot.state.value if snapshot.available else "UNKNOWN"
    except Exception:
        return "UNKNOWN"
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        value = enum_value
    object_state = getattr(value, "state", None)
    if object_state is not None and object_state is not value:
        return _v2_state(object_state)
    if isinstance(value, dict):
        for key in ("state", "manifest_state", "status", "marker"):
            if key in value:
                return _v2_state(value[key])
        for key in ("manifest", "snapshot"):
            if isinstance(value.get(key), dict):
                return _v2_state(value[key])
        return "UNKNOWN"
    marker = str(value or "").strip().upper()
    return marker if marker in _V2_STATES else "UNKNOWN"


def _v2_facade_snapshot(facade: Any) -> tuple[str, Any]:
    fn = getattr(facade, "state_snapshot", None)
    if not callable(fn):
        fn = getattr(facade, "status", None)
    if not callable(fn):
        return "UNKNOWN", None
    try:
        value = fn()
        return _v2_state(value), value
    except Exception:
        return "UNKNOWN", None



def _v2_facade_snapshot(facade: Any) -> tuple[str, Any]:
    fn = getattr(facade, "state_snapshot", None)
    if not callable(fn):
        fn = getattr(facade, "status", None)
    if not callable(fn):
        return "UNKNOWN", None
    try:
        value = fn()
        return _v2_state(value), value
    except Exception:
        return "UNKNOWN", None


def _plain_hook_context(context: Any) -> dict[str, Any]:
    if isinstance(context, dict):
        return dict(context)
    try:
        value = asdict(context)
    except (TypeError, ValueError):
        try:
            value = dict(vars(context))
        except (TypeError, AttributeError):
            value = {}
    return dict(value) if isinstance(value, dict) else {}


def _v2_hook_context(
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
    event: str,
    workspace: Path,
) -> Any:
    context = _effective_agent_context(
        provider, agent_instance_id, share_group_id, payload, event=event,
    )
    plain = _plain_hook_context(context)
    # The plain compatibility seam is used by injected/test facades when a
    # process-local native capability cannot be issued.  Keep the same
    # authenticated scope shape there so compact/resume events cannot lose
    # workspace or trusted identity merely because their payload is sparse.
    plain.setdefault("workspace_id", str(workspace))
    plain["trusted_identity"] = {
        key: plain.get(key, "")
        for key in (
            "workspace_id", "agent_instance_id", "share_group_id",
            "project_ref", "provider", "runtime_role",
        )
        if plain.get(key, "")
    }
    try:
        from .access_context import AccessContext, load_access_context
        from .runtime_v2.group_native import GroupControlService
        from .runtime_v2.native_ports import bind_native_transport_context

        access = load_access_context()
        resolved_agent, error = access.resolve_agent(agent_instance_id)
        if error or resolved_agent != agent_instance_id:
            raise ValueError("trusted hook agent unavailable")
        binding = GroupControlService(workspace, write=False).active_binding_for_agent(
            agent_instance_id,
        )
        if (
            not binding
            or str(binding.get("status") or "") != "active"
            or str(binding.get("share_group_id") or "") != str(share_group_id)
        ):
            raise ValueError("active V2 hook binding unavailable")
        if type(access) is not AccessContext:
            raise ValueError("trusted hook context capability unavailable")
        return bind_native_transport_context(
            access,
            workspace_id=str(workspace),
            share_group_id=str(share_group_id),
            project_ref=str(getattr(context, "project_ref", "") or ""),
            provider=str(getattr(context, "provider", provider) or provider),
            runtime_role=str(getattr(context, "runtime_role", "") or ""),
            runtime_agent_id=str(getattr(context, "runtime_agent_id", "") or ""),
            parent_agent_id=str(getattr(context, "parent_agent_id", "") or ""),
            context_hash=str(getattr(context, "context_hash", "") or ""),
            entrypoint="hook",
        )
    except Exception:
        # Injected test facades may intentionally accept the serializable
        # context form.  A real native port rejects that form before mutation.
        return plain


def _hook_data(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = dict(result)
    embedded = data.get("data")
    if isinstance(embedded, dict):
        # The outer transport envelope is always ``status=ok`` when dispatch
        # itself succeeded.  The ContextPacket inside ``data`` owns the
        # semantic status/error (including mandatory overflow), so it must win
        # for those fields rather than being hidden by the transport status.
        data = {**data, **embedded}
    packet = data.get("packet") or data.get("context_packet")
    if not isinstance(packet, dict) and any(
        key in data for key in ("mandatory", "relevant", "receipts", "ready", "effective_agent")
    ):
        packet = {
            key: data[key]
            for key in (
                "mandatory", "relevant", "knowledge", "reference_only",
                "budget", "effective_agent", "receipts", "ready", "state",
                "status", "error",
            )
            if key in data
        }
    return data, packet if isinstance(packet, dict) else {}


def _verified_bootstrap_packet(result: Any, agent_instance_id: str) -> dict[str, Any] | None:
    """Require semantic V2 bootstrap success before clearing mandatory debt.

    A transport's isError=false (or a bare ok=true) does not prove that its
    context package passed validation. Only a complete response from the
    explicitly identified bootstrap operation for this bound agent does.
    """
    if not isinstance(result, dict) or result.get("isError") is True or result.get("ok") is False:
        return None
    if result.get("name") == "memoryguard_context_bootstrap" and result.get("ok") is True:
        packet = result.get("data")
        if (
            isinstance(packet, dict)
            and packet.get("status") == "ok" and not packet.get("error")
            and packet.get("state") == "V2_ACTIVE" and packet.get("ready") is True
            and not packet.get("mandatory_overflow") and not packet.get("mandatory_invalid_reason")
            and packet.get("effective_agent") == agent_instance_id
            and isinstance(packet.get("mandatory"), list)
            and isinstance(packet.get("receipts"), list)
            and isinstance(packet.get("budget"), dict)
            and isinstance(packet["budget"].get("mandatory"), dict)
        ):
            return packet
        return None
    content = result.get("content")
    if isinstance(content, list):
        # MCP returns one JSON text payload. Reject ambiguity instead of
        # selecting a successful fragment from a mixed/error response.
        texts = [item.get("text") for item in content if isinstance(item, dict) and item.get("type") == "text"]
        if len(texts) == 1 and isinstance(texts[0], str):
            try:
                return _verified_bootstrap_packet(json.loads(texts[0]), agent_instance_id)
            except (ValueError, TypeError):
                pass
    return None


_MANDATORY_FAIL_CLOSED_ERRORS = frozenset({
    "injection_policy_invalid",
    "injection_policy_layer_conflict",
    "mandatory_content_blocked",
    "mandatory_policy_requires_rule",
    "mandatory_sensitive_blocked",
    "mandatory_item_limit_exceeded",
    "mandatory_budget_exceeded",
    "mandatory_overflow",
    "mandatory_rule_package_invalid",
})
_BOOTSTRAP_RUNTIME_ERRORS = frozenset({
    "context_build_failed",
    "context_hash_missing",
    "unknown_runtime_state",
    "missing_trusted_identity",
    "context_budget_required",
    "v2_hook_bootstrap_failed",
})
_BOOTSTRAP_REQUIRED_EVENTS = frozenset({
    "session_start",
    "subagent_start",
    "user_prompt",
    "pre_tool",
})
_BOOTSTRAP_RETRY_CLAIM_TTL_SECONDS = 30.0
_NEUTRAL_CANONICAL_MARKERS = frozenset({
    "no_source", "absent", "empty", "not_configured",
})


def _error_code(value: Any) -> str:
    text = str(value or "").strip()
    if ":" in text:
        text = text.split(":", 1)[0].strip()
    return text


def _is_neutral_canonical_fallback(
    packet: Mapping[str, Any] | None = None,
    error: str = "",
) -> bool:
    markers = [_error_code(error).casefold()]
    if isinstance(packet, Mapping):
        for key in ("status", "canonical_state", "canonical_status", "error"):
            markers.append(str(packet.get(key) or "").strip().casefold())
    return any(marker in _NEUTRAL_CANONICAL_MARKERS for marker in markers if marker)


def _is_mandatory_overflow_error(
    error: str = "",
    packet: Mapping[str, Any] | None = None,
) -> bool:
    """True only for fail-closed mandatory package failures.

    Generic bootstrap/runtime errors such as ``context_build_failed`` and the
    canonical ``NO_SOURCE``/``absent`` fallback are not budget overflow.
    """
    if _is_neutral_canonical_fallback(packet, error):
        return False
    code = _error_code(error)
    packet_error = ""
    if isinstance(packet, Mapping):
        packet_error = _error_code(packet.get("error") or packet.get("mandatory_invalid_reason") or "")
        if _error_code(packet_error) in _BOOTSTRAP_RUNTIME_ERRORS or code in _BOOTSTRAP_RUNTIME_ERRORS:
            return False
        if str(packet.get("mandatory_overflow")).strip().casefold() in {"true", "1"}:
            if packet_error.startswith("mandatory_") or code.startswith("mandatory_"):
                return True
            if packet_error in _MANDATORY_FAIL_CLOSED_ERRORS or code in _MANDATORY_FAIL_CLOSED_ERRORS:
                return True
            if not packet_error and not code:
                return True
    if code in _BOOTSTRAP_RUNTIME_ERRORS or packet_error in _BOOTSTRAP_RUNTIME_ERRORS:
        return False
    if code.startswith("mandatory_") or packet_error.startswith("mandatory_"):
        return True
    return code in _MANDATORY_FAIL_CLOSED_ERRORS or packet_error in _MANDATORY_FAIL_CLOSED_ERRORS


def _bootstrap_failure_state(
    reason: str,
    packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    overflow = _is_mandatory_overflow_error(reason, packet)
    neutral = _is_neutral_canonical_fallback(packet, reason)
    if neutral and not overflow:
        return {
            "bootstrap_ok": True,
            "bootstrap_error": "",
            "context_hash": "",
            "mandatory_overflow": False,
            "mandatory_invalid_reason": "",
            "mandatory_match_receipts": [],
        }
    clipped = str(reason or "")[:500]
    return {
        "bootstrap_ok": False,
        "bootstrap_error": clipped,
        "context_hash": "",
        "mandatory_overflow": overflow,
        "mandatory_invalid_reason": clipped if overflow else "",
        "mandatory_match_receipts": [],
    }


def _record_conversion_usage(
    *,
    workspace: Path,
    provider: str,
    event: str,
    text: str,
    packet: Mapping[str, Any] | None,
    context_identity: Mapping[str, Any],
) -> None:
    """Best-effort numeric usage receipt after final Hook text exists.

    The recorder receives only deterministic counts and trusted scope
    metadata.  Telemetry failure must never change the already-built Hook
    response or expose prompt/memory text.
    """

    if not text or not isinstance(packet, Mapping):
        return
    usage = packet.get("usage")
    if not isinstance(usage, Mapping):
        budget = packet.get("budget")
        usage = budget.get("usage") if isinstance(budget, Mapping) else None
    if not isinstance(usage, Mapping):
        return
    measurement_basis = str(usage.get("measurement_basis") or "").strip()
    if measurement_basis and measurement_basis != "mg_deterministic_unit":
        return
    def _units(value: Any) -> int | None:
        return value if type(value) is int and value >= 0 else None

    candidate_body_units = _units(usage.get("candidate_body_units"))
    if candidate_body_units is None:
        candidate = usage.get("candidate_baseline")
        candidate_body_units = _units(
            candidate.get("tokens") if isinstance(candidate, Mapping) else None
        )
    delivered_body_units = _units(usage.get("delivered_body_units"))
    if delivered_body_units is None:
        delivered = usage.get("delivered", usage.get("rendered"))
        delivered_body_units = _units(
            delivered.get("tokens") if isinstance(delivered, Mapping) else None
        )
    # A packet from an older/foreign runtime may not expose body measurements.
    # Do not infer them from a total-looking field: that would create a false
    # savings event.  The final Hook text is the only source of wrapper size.
    if candidate_body_units is None or delivered_body_units is None:
        return
    # The Hook text is the provider-delivered payload, including its wrapper.
    # Code-point counting is the same deterministic counter used by the
    # ContextEngine and does not require retaining the text.
    delivered_total_units = len(text)
    formula_baseline_total_units = delivered_total_units + (
        candidate_body_units - delivered_body_units
    )
    # A malformed/legacy packet can claim more delivered body than it ever
    # considered.  Keep the persisted comparison non-negative while retaining
    # the exact shared-wrapper formula for normal packets.
    baseline_total_units = max(delivered_total_units, formula_baseline_total_units)
    wrapper_overhead_units = max(0, delivered_total_units - delivered_body_units)
    identity = "|".join(
        str(context_identity.get(key) or "")
        for key in ("provider", "runtime_role", "project_ref", "context_hash")
    )
    event_id = hashlib.sha256(
        f"{identity}|{event}|{hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()}".encode(
            "utf-8", "surrogatepass"
        )
    ).hexdigest()
    try:
        from .usage_telemetry import record_conversion_event

        record_conversion_event(
            workspace,
            provider=provider,
            program=provider,
            share_group_id=str(context_identity.get("share_group_id") or ""),
            project_ref=str(context_identity.get("project_ref") or ""),
            # The telemetry store's baseline/delivered columns represent the
            # final provider payload totals.  Body-level measurements stay in
            # the packet; the wrapper is intentionally not persisted as
            # prompt text or an untrusted zero estimate.
            baseline_units=max(0, baseline_total_units),
            delivered_units=delivered_total_units,
            measurement_basis="mg_deterministic_unit",
            event_id=event_id,
            source_cursor=event,
        )
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "usage_telemetry_record_failed", provider, event, exc
        )


def _mark_bootstrap_failure(
    *,
    workspace: Path,
    provider: str,
    session_id: str,
    event: str,
    reason: str,
    packet: Mapping[str, Any] | None = None,
) -> None:
    """Record a bounded bootstrap failure for events that gate execution.

    Ordinary stop failures are deliberately excluded: lifecycle shutdown must
    not create a new blocking debt for the next invocation.  An explicit
    mandatory overflow is the exception because it is already a fail-closed
    native decision that must survive until the next gated event.  The state
    write is the same short per-session atomic update used by the successful
    bootstrap path; it never surrounds native dispatch or other host I/O.
    """
    if event not in _BOOTSTRAP_REQUIRED_EVENTS and not (
        event == "stop" and _is_mandatory_overflow_error(reason, packet)
    ):
        return
    try:
        _update_state(
            workspace,
            provider,
            session_id,
            updates=_bootstrap_failure_state(reason, packet),
        )
    except Exception:
        # A failed ordinary pre-tool sidecar write must not brick the host.
        # The native packet remains authoritative for this invocation, so an
        # explicit mandatory overflow is still denied by the caller even when
        # its diagnostic state cannot be persisted.  Lifecycle/user-prompt
        # writes retain their existing fail-closed behavior.
        if event == "pre_tool":
            return
        raise


def _needs_bootstrap_recovery(state: Mapping[str, Any]) -> bool:
    """Return true for an ordinary stale/failed bootstrap in this session."""
    if (
        not state
        or state.get("mandatory_overflow")
        or state.get("canonical_fallback")
    ):
        return False
    if state.get("bootstrap_error") or state.get("bootstrap_ok") is False:
        return True
    if state.get("bootstrap_ok") is True:
        return not str(state.get("context_hash") or "").strip()
    return "context_hash" in state and not str(state.get("context_hash") or "").strip()


def _claim_bootstrap_recovery(
    workspace: Path,
    provider: str,
    session_id: str,
) -> bool:
    """Claim one same-turn recovery under the short state transaction lock."""
    claimed = False

    def mutator(state: dict[str, Any]) -> None:
        nonlocal claimed
        claimed_at = state.get("bootstrap_retry_claimed_at")
        try:
            claim_age = time.time() - float(claimed_at)
        except (TypeError, ValueError):
            # State written before claim timestamps existed is recoverable.
            claim_age = _BOOTSTRAP_RETRY_CLAIM_TTL_SECONDS
        stale_claim = claim_age >= _BOOTSTRAP_RETRY_CLAIM_TTL_SECONDS
        if (
            _needs_bootstrap_recovery(state)
            and (not state.get("bootstrap_retry_claimed") or stale_claim)
            and not state.get("mandatory_overflow")
        ):
            state["bootstrap_retry_claimed"] = True
            state["bootstrap_retry_claimed_at"] = time.time()
            claimed = True

    _update_state(workspace, provider, session_id, mutator=mutator)
    return claimed


def _clear_bootstrap_recovery_claim(
    workspace: Path,
    provider: str,
    session_id: str,
) -> None:
    _update_state(
        workspace,
        provider,
        session_id,
        updates={
            "bootstrap_retry_claimed": False,
            "bootstrap_retry_claimed_at": 0,
        },
    )


def _retryable_bootstrap_failure(
    reason: str,
    packet: Mapping[str, Any] | None = None,
) -> bool:
    """Retry only transient context/bootstrap failures, never policy failures."""
    if _is_mandatory_overflow_error(reason, packet):
        return False
    code = _error_code(reason).casefold()
    return (
        code in {value.casefold() for value in _BOOTSTRAP_RUNTIME_ERRORS}
        or code.startswith("v2_hook_dispatch_failed")
    )


def _normalize_native_v2_packet(
    packet: dict[str, Any],
    *,
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    session_id: str,
    event: str,
) -> dict[str, Any]:
    """Project a native ContextPacket onto the Hook receipt boundary.

    V2 deliberately does not expose the old rule-store receipt object.  Its
    bounded ContextEngine receipts are the authoritative match evidence.  The
    Hook keeps only opaque, deterministic IDs and public metadata, never rule
    bodies or raw evidence, so Cursor session injection and the next-tool gate
    share one fail-closed receipt shape.
    """
    if not any(key in packet for key in ("mandatory", "relevant", "receipts")):
        return packet
    normalized = dict(packet)
    mandatory = [item for item in packet.get("mandatory", []) if isinstance(item, dict)]
    relevant = [item for item in packet.get("relevant", []) if isinstance(item, dict)]
    normalized["mandatory_items"] = mandatory
    normalized["items"] = relevant
    rule_ids = [
        str(item.get("item_id") or item.get("memory_id") or "").strip()
        for item in mandatory
    ]
    rule_ids = [value for value in rule_ids if value]
    receipts: list[dict[str, Any]] = []
    for item in packet.get("receipts", []):
        if not isinstance(item, dict):
            continue
        if item.get("hit") is not True or str(item.get("layer") or "") != "mandatory":
            continue
        item_id = str(item.get("item_id") or item.get("memory_id") or "").strip()
        if not item_id:
            continue
        memory_id = str(item.get("memory_id") or item_id).strip()
        injection_policy = str(item.get("injection_policy") or "always").strip().casefold()
        priority = item.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            priority = 0
        scope = item.get("scope")
        public_scope = {}
        if isinstance(scope, dict):
            public_scope = {
                key: scope[key]
                for key in (
                    "workspace_id", "project_ref", "agent_instance_id",
                    "share_group_id", "provider", "runtime_role",
                )
                if key in scope and isinstance(scope[key], (str, int, bool, float))
            }
        receipt_id = "v2-" + _short_hash(json.dumps(
            {
                "workspace": str(workspace),
                "provider": provider,
                "agent": agent_instance_id,
                "group": share_group_id,
                "session": session_id,
                "event": event,
                "item": item_id,
                "memory_id": memory_id,
                "injection_policy": injection_policy,
                "priority": priority,
                "scope": public_scope,
                "digest": item.get("digest") or item.get("item_hash") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        receipts.append({
            "receipt_id": receipt_id,
            "memory_id": memory_id,
            "item_id": item_id,
            "layer": "mandatory",
            "source": "native-v2-context",
            "injection_policy": injection_policy,
            "priority": priority,
            "scope": public_scope,
        })
    normalized["mandatory_rule_ids"] = rule_ids
    normalized["mandatory_match_receipts"] = receipts
    error = str(packet.get("error") or "").strip()
    normalized["mandatory_overflow"] = _is_mandatory_overflow_error(error, packet)
    normalized["mandatory_invalid_reason"] = str(
        packet.get("mandatory_invalid_reason") or error or ""
    )
    return normalized


def _v2_hook_cutover(
    *,
    provider: str,
    event: str,
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
    session_id: str,
    payload: dict[str, Any],
    allow_same_turn_retry: bool = True,
) -> dict[str, Any]:
    """Route every Hook event through the V2 state gate and native hook port."""
    try:
        facade = _load_v2_runtime_facade(workspace)
    except Exception as exc:
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason=f"v2_hook_facade_load_failed:{type(exc).__name__}",
        )
        if event == "stop":
            return {}
        return _v2_upgrade_output(provider, event, "UNKNOWN")
    if facade is _V2_FACADE_MISSING:
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason="v2_hook_facade_missing",
        )
        if event == "stop":
            return {}
        return _v2_upgrade_output(provider, event, "UNKNOWN")
    state, snapshot = _v2_facade_snapshot(facade)
    if state not in _V2_READ_STATES:
        return _v2_upgrade_output(provider, event, state)
    if state == "V2_READY" and event == "stop":
        return {}

    hook = getattr(facade, "bootstrap_hook", None)
    if not callable(hook):
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason="v2_hook_capability_missing",
        )
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook capability unavailable; tool execution denied.")
        if event == "stop":
            return {}
        return _context_output(
            provider, event,
            "MemoryGuard V2 hook capability unavailable; bootstrap blocked.",
        )
    try:
        params = inspect.signature(hook).parameters
        has_context = "context" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        accepts_snapshot = "snapshot" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError) as exc:
        has_context = False
        accepts_snapshot = False
        capability_error = f"v2_hook_capability_invalid:{type(exc).__name__}"
    else:
        capability_error = ""
    if not has_context:
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason=capability_error or "v2_hook_context_capability_missing",
        )
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook context capability unavailable; tool execution denied.")
        if event == "stop":
            return {}
        return _context_output(
            provider, event,
            "MemoryGuard V2 hook context capability unavailable; bootstrap blocked.",
        )
    try:
        # Compaction/resume payloads are commonly sparse (for example they
        # carry only a session id and a trigger).  Reuse the last trusted
        # lifecycle scope for omitted project/session fields, while keeping
        # the hook arguments authoritative for agent/group/provider.
        context_payload = dict(payload)
        prior_state = _load_state(workspace, provider, session_id)
        prior_identity = prior_state.get("context_identity")
        if isinstance(prior_identity, dict):
            for key in ("project_ref", "projectRef", "cwd", "context_hash"):
                if not context_payload.get(key) and prior_identity.get(key):
                    context_payload[key] = prior_identity[key]
            if not any(
                context_payload.get(key)
                for key in ("session_id", "conversation_id", "conversationId", "sessionId", "thread_id")
            ) and prior_identity.get("session_id"):
                context_payload["session_id"] = prior_identity["session_id"]
        context = _v2_hook_context(
            provider, agent_instance_id, share_group_id, context_payload, event, workspace,
        )
        context_view = _plain_hook_context(context)
        context_identity = {
            "workspace_id": str(workspace),
            "agent_instance_id": agent_instance_id,
            "share_group_id": share_group_id,
            "provider": provider,
            "runtime_role": str(context_view.get("runtime_role") or "root"),
            "project_ref": str(context_view.get("project_ref") or ""),
            "session_id": str(context_view.get("session_id") or session_id or ""),
            "context_hash": str(context_view.get("context_hash") or ""),
        }


        kwargs: dict[str, Any] = {"context": context}
        if accepts_snapshot:
            kwargs["snapshot"] = snapshot
        result = hook(event, _v2_hook_request_payload(payload), **kwargs)
    except Exception as exc:
        reason = f"v2_hook_dispatch_failed:{type(exc).__name__}"
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason=reason,
        )
        _record_heartbeat(
            workspace, provider, agent_instance_id, event=event,
            error=reason,
        )
        if (
            event == "pre_tool"
            and allow_same_turn_retry
            and _retryable_bootstrap_failure(reason)
            and _claim_bootstrap_recovery(workspace, provider, session_id)
        ):
            retry_result = _v2_hook_cutover(
                provider=provider,
                event=event,
                workspace=workspace,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                session_id=session_id,
                payload=payload,
                allow_same_turn_retry=False,
            )
            retry_state = _load_state(workspace, provider, session_id)
            if (
                retry_state.get("bootstrap_ok")
                and retry_state.get("context_hash")
                and not retry_state.get("mandatory_overflow")
            ):
                _clear_bootstrap_recovery_claim(workspace, provider, session_id)
            return retry_result
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook failed; tool execution denied.")
        if event == "stop":
            return {}
        return _context_output(provider, event, "MemoryGuard V2 hook failed; bootstrap blocked.")

    if not isinstance(result, dict):
        reason = "v2 hook returned invalid envelope"
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason=reason,
        )
        _record_heartbeat(
            workspace, provider, agent_instance_id, event=event, error=reason,
            mandatory_overflow=False,
        )
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook returned an invalid envelope; bootstrap blocked.")
        if event == "stop":
            return {}
        return _context_output(provider, event, "MemoryGuard V2 hook returned an invalid envelope; bootstrap blocked.")

    data, packet = _hook_data(result)
    packet = _normalize_native_v2_packet(
        packet,
        workspace=workspace,
        provider=provider,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        session_id=session_id,
        event=event,
    )
    # Native facades normally provide a context hash.  A compatibility facade
    # can omit it, but a successful packet still needs a stable receipt so a
    # later event cannot look like a different/empty bootstrap context.
    if packet and not context_identity.get("context_hash"):
        context_identity["context_hash"] = _short_hash(json.dumps(
            {
                "workspace_id": context_identity["workspace_id"],
                "agent_instance_id": context_identity["agent_instance_id"],
                "share_group_id": context_identity["share_group_id"],
                "provider": context_identity["provider"],
                "runtime_role": context_identity["runtime_role"],
                "project_ref": context_identity["project_ref"],
                "session_id": context_identity["session_id"],
                "packet": "native-v2-context",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
    status = str(data.get("status", "") or "").strip().casefold()
    packet_status = str(packet.get("status", "") or "").strip().casefold()
    combined_error = str(
        data.get("error") or packet.get("error") or data.get("code") or ""
    ).strip()
    envelope_failed = (
        data.get("ok") is False
        or status in {"error", "blocked", "failed"}
        or packet_status in {"error", "blocked", "failed"}
        or bool(data.get("error"))
        or bool(packet.get("error"))
    )
    if not context_identity.get("context_hash"):
        envelope_failed = True
        combined_error = combined_error or "context_hash_missing"
    if envelope_failed and not _is_neutral_canonical_fallback(packet, combined_error):
        reason = combined_error or "v2_hook_bootstrap_failed"
        _mark_bootstrap_failure(
            workspace=workspace,
            provider=provider,
            session_id=session_id,
            event=event,
            reason=reason,
            packet=packet,
        )
        overflow = _is_mandatory_overflow_error(reason, packet)
        _record_heartbeat(
            workspace, provider, agent_instance_id, event=event,
            error=f"v2_hook_envelope_failed:{reason}",
            mandatory_overflow=overflow,
        )
        if (
            event == "pre_tool"
            and allow_same_turn_retry
            and _retryable_bootstrap_failure(reason, packet)
            and _claim_bootstrap_recovery(workspace, provider, session_id)
        ):
            retry_result = _v2_hook_cutover(
                provider=provider,
                event=event,
                workspace=workspace,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                session_id=session_id,
                payload=payload,
                allow_same_turn_retry=False,
            )
            retry_state = _load_state(workspace, provider, session_id)
            if (
                retry_state.get("bootstrap_ok")
                and retry_state.get("context_hash")
                and not retry_state.get("mandatory_overflow")
            ):
                _clear_bootstrap_recovery_claim(workspace, provider, session_id)
            return retry_result
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook failed; tool execution denied.")
        if event == "stop":
            return {}
        if overflow:
            return _context_output(
                provider,
                event,
                _render_context({"context_packet": packet, **packet}),
            )
        return _context_output(provider, event, "MemoryGuard V2 hook failed; bootstrap blocked.")

    if event in {"session_start", "subagent_start", "user_prompt", "pre_tool", "stop"}:
        receipts = packet.get(
            "mandatory_match_receipts",
            data.get("mandatory_match_receipts", []),
        )
        packet_error = str(packet.get("error") or data.get("error") or "").strip()
        overflow = _is_mandatory_overflow_error(packet_error, packet)
        blocked = packet_status in {"error", "blocked", "failed"}
        neutral = _is_neutral_canonical_fallback(packet, packet_error)
        prior_overflow = False
        if event == "pre_tool":
            prior_overflow = bool(
                _load_state(workspace, provider, session_id).get("mandatory_overflow")
            )
            overflow = overflow or prior_overflow
        bootstrap_ok = not overflow and (neutral or not (blocked or packet_error))
        # Cursor's beforeSubmitPrompt hook is only the conversation receipt
        # boundary.  Its first ordinary tool must remain locked until the
        # host has actually executed memoryguard_context_bootstrap through
        # CallMcpTool; otherwise a successful prompt hook would silently
        # bypass the provider's explicit bootstrap gate.
        if provider == "cursor" and event == "user_prompt":
            bootstrap_ok = False
            packet_error = packet_error or "cursor_bootstrap_required"
        state_updates: dict[str, Any] = {
            "bootstrap_ok": bootstrap_ok,
            "bootstrap_error": packet_error[:500] if not bootstrap_ok else "",
            "context_hash": (
                context_identity.get("context_hash", "")
                if bootstrap_ok
                else ""
            ),
            "mandatory_overflow": overflow,
            "mandatory_invalid_reason": (
                str(packet.get("mandatory_invalid_reason") or packet_error or "")[:500]
                if overflow
                else ""
            ),
            "mandatory_rule_ids": list(
                packet.get("mandatory_rule_ids", data.get("mandatory_rule_ids", [])) or []
            ),
            "mandatory_match_receipts": receipts if isinstance(receipts, list) else [],
            "context_identity": context_identity,
            "canonical_fallback": neutral,
        }
        if event in {"session_start", "subagent_start", "user_prompt"}:
            state_updates.update({
                "bootstrap_retry_claimed": False,
                "bootstrap_retry_claimed_at": 0,
                "bootstrap_pending": False,
            })
        if event == "user_prompt":
            state_updates.update({
                "prompt_hash": _short_hash(_prompt(payload)),
                "durable_candidate": _durable_candidate(_prompt(payload)),
                "write_seen": False,
                "stop_continued": False,
            })
        try:
            _update_state(
                workspace, provider, session_id,
                updates=state_updates,
            )
        except Exception:
            if event != "pre_tool":
                raise
            # The packet is still available for this call.  Do not let a
            # non-mandatory runtime sidecar failure block an ordinary tool;
            # mandatory overflow must remain fail-closed in this invocation.
            if overflow:
                return _deny_output(
                    provider,
                    _mandatory_overflow_block_message(
                        str(packet.get("error") or packet.get("mandatory_invalid_reason") or "")
                    ),
                )

    heartbeat_overflow = bool(
        packet.get("mandatory_overflow", data.get("mandatory_overflow", False))
    )
    if event == "pre_tool":
        # A pre-tool request may resolve a different runtime role than the
        # lifecycle event that discovered an overflow (notably a subagent
        # start followed by the parent host's tool gate).  Preserve the
        # session's fail-closed overflow receipt instead of overwriting it
        # with the empty packet from that unrelated scope.
        heartbeat_overflow = heartbeat_overflow or bool(
            _load_state(workspace, provider, session_id).get("mandatory_overflow")
        )
    _record_heartbeat(
        workspace, provider, agent_instance_id, event=event,
        error=str(packet.get("error", data.get("error", "")) or ""),
        mandatory_rule_ids=list(
            packet.get("mandatory_rule_ids", data.get("mandatory_rule_ids", [])) or []
        ),
        mandatory_match_receipts=(
            packet.get("mandatory_match_receipts", data.get("mandatory_match_receipts", []))
            if isinstance(packet.get("mandatory_match_receipts", data.get("mandatory_match_receipts", [])), list)
            else []
        ),
        mandatory_overflow=heartbeat_overflow,
    )

    direct_output = data.get("output") or data.get("host_output")
    final_text = ""
    if isinstance(direct_output, dict):
        # Native providers may return a fully formed host payload.  Keep this
        # boundary observational: only record a textual context field, and
        # never let telemetry alter or block the provider response.
        if provider == "cursor":
            final_text = str(direct_output.get("additional_context") or "")
        else:
            direct_hook = direct_output.get("hookSpecificOutput")
            if isinstance(direct_hook, Mapping):
                final_text = str(direct_hook.get("additionalContext") or "")
        final_output = direct_output
    else:
        text = str(data.get("text", "") or "")
        if not text and packet:
            text = _render_context({"context_packet": packet, **packet})
        if event == "session_start":
            final_text = _static_session_context(provider) + ("\n" + text if text else "")
            final_output = _context_output(provider, event, final_text)
        elif event == "subagent_start":
            final_text = _static_session_context(provider) + ("\n" + text if text else "")
            final_output = _context_output(provider, event, final_text)
        elif event == "user_prompt" and text:
            final_text = text
            final_output = _context_output(provider, event, text)
        else:
            return _allow_output(provider, event) if event == "user_prompt" else {}

    _record_conversion_usage(
        workspace=workspace,
        provider=str(context_identity.get("provider") or provider),
        event=event,
        text=final_text,
        packet=packet,
        context_identity=context_identity,
    )
    return final_output



def _run_hook_unlocked(
    *,
    provider: str,
    event: str,
    workspace: str | Path,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run one host Hook event through the V2 state gate."""
    normalized_provider = provider.strip().lower()
    if normalized_provider == "claude-code":
        normalized_provider = "claude"
    if normalized_provider not in {"claude", "codex", "cursor"}:
        return {}
    if event not in _EVENT_NAMES:
        raise ValueError(f"unknown hook event: {event}")

    root = Path(workspace).expanduser().resolve()
    session_id = _session_id(payload)
    mode = get_hook_mode(root, normalized_provider, agent_instance_id)

    # Codex thread cleanup is a host-owned lifecycle side effect.  Run it
    # before the V2 memory data-plane gate so stale terminal edges are repaired
    # even when the workspace still needs upgrade.  Stop remains observational
    # and best-effort; an upgrade response must not block host shutdown.
    if normalized_provider == "codex":
        if event in {"session_start", "user_prompt", "post_tool", "stop"}:
            _best_effort_codex_mcp_lifecycle(
                event=event,
                workspace=root,
                payload=payload,
            )
        if event in {"session_start", "user_prompt", "post_tool"}:
            _best_effort_codex_global_reconcile(
                workspace=root,
                agent_instance_id=agent_instance_id,
                payload=payload,
                event=event,
            )
        if event in {"session_start", "user_prompt"}:
            _best_effort_codex_profile_repair(
                workspace=root,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
            )
        if event == "stop":
            _best_effort_codex_reconcile(
                workspace=root,
                agent_instance_id=agent_instance_id,
                payload=payload,
            )

    # Paused mode is the maintenance escape hatch.  It must bypass the V2
    # data-plane entirely, otherwise a broken V2 gate can prevent operators
    # from pausing the very hook that needs repair.  Codex lifecycle cleanup
    # above remains best-effort and host-owned.
    if mode == "paused":
        return {}

    # Keep a narrow, explicit repair lane reachable even when the context
    # package or manifest gate is what is broken.  These operations still hit
    # the native MCP V2 state/identity/governance gates; only the host hook's
    # PreToolUse bootstrap is bypassed.  Ordinary shell/file/tool execution is
    # never included here and remains fail-closed.
    if event == "pre_tool":
        recovery_tool, recovery_input = _hook_tool_parts(payload)
        overflow_state = _load_state(root, normalized_provider, session_id)
        if overflow_state.get("mandatory_overflow") and mode == "enforce":
            if not _is_budget_recovery_tool(recovery_tool, recovery_input):
                return _deny_output(
                    normalized_provider,
                    _mandatory_overflow_block_message(
                        str(
                            overflow_state.get("mandatory_invalid_reason")
                            or overflow_state.get("bootstrap_error")
                            or ""
                        )
                    ),
                )
        if _is_memoryguard_recovery_tool(recovery_tool, recovery_input):
            # The recovery lane intentionally bypasses the V2 hook dispatch,
            # but a real Cursor CallMcpTool bootstrap must still satisfy the
            # per-conversation tool gate. PreToolUse can only record a pending
            # invocation; PostToolUse must verify its result before any later
            # tool can rely on bootstrap_ok.
            if _is_memoryguard_bootstrap(recovery_tool, recovery_input):
                def mark_bootstrap_pending(state: dict[str, Any]) -> None:
                    if not state.get("mandatory_overflow"):
                        state["bootstrap_pending"] = True
                _update_state(
                    root, normalized_provider, session_id,
                    mutator=mark_bootstrap_pending,
                )
            return {}

    pre_tool_recovery = False
    if event == "pre_tool":
        prior_state = _load_state(root, normalized_provider, session_id)
        if normalized_provider == "cursor":
            # Cursor's prompt hook only records the conversation boundary.
            # Its first ordinary tool must wait for the host to execute a
            # real CallMcpTool memoryguard_context_bootstrap.  In particular,
            # never let the generic same-turn recovery claim this state.
            is_subagent = bool(
                payload.get("subagent_id") or payload.get("agent_id")
            )
            if (
                prior_state
                and not prior_state.get("bootstrap_ok")
                and not is_subagent
            ):
                gate_tool, gate_input = _hook_tool_parts(payload)
                if (
                    _is_memoryguard_bootstrap(gate_tool, gate_input)
                    or _is_memoryguard_recovery_tool(gate_tool, gate_input)
                ):
                    pass
                elif prior_state.get("mandatory_overflow"):
                    return _deny_output(
                        normalized_provider,
                        _mandatory_overflow_block_message(
                            str(prior_state.get("mandatory_invalid_reason") or prior_state.get("bootstrap_error") or "")
                        ),
                    )
                else:
                    return _deny_output(
                        normalized_provider,
                        "CallMcpTool memoryguard_context_bootstrap must succeed before the first tool.",
                    )
        if _needs_bootstrap_recovery(prior_state):
            try:
                claimed = _claim_bootstrap_recovery(
                    root, normalized_provider, session_id,
                )
            except Exception:
                if normalized_provider == "cursor":
                    claimed = False
                else:
                    raise
            if claimed:
                # Existing bootstrap debt gets one native recovery dispatch.
                # Disable the post-failure retry here: this is already the
                # single same-turn attempt for this session state.
                pre_tool_recovery = True
            else:
                current_state = _load_state(root, normalized_provider, session_id)
                if (
                    current_state.get("bootstrap_ok")
                    and current_state.get("context_hash")
                    and not current_state.get("mandatory_overflow")
                ):
                    return {}
                if current_state.get("mandatory_overflow"):
                    return _deny_output(
                        normalized_provider,
                        _mandatory_overflow_block_message(
                            str(current_state.get("mandatory_invalid_reason") or current_state.get("bootstrap_error") or "")
                        ),
                    )
                return _deny_output(
                    normalized_provider,
                    "MemoryGuard 上下文加载不可用；请先调用 "
                    "memoryguard_context_bootstrap(task=当前用户请求) 完成恢复，"
                    "当前工具保持阻断。",
                )

    v2_result = _v2_hook_cutover(
        provider=normalized_provider,
        event=event,
        workspace=root,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        session_id=session_id,
        payload=payload,
        allow_same_turn_retry=not pre_tool_recovery,
    )

    # Retired/unknown states already carry the stable public error.  Return
    # before any local guard or compatibility helper can run.
    result_code = str(v2_result.get("code", "") or "")
    if result_code in {
        "v2_upgrade_required",
        "v2_manifest_state_unavailable",
    }:
        if normalized_provider == "codex" and event == "stop":
            return {}
        return v2_result

    if event == "pre_tool":
        state = _load_state(root, normalized_provider, session_id)
        if state.get("mandatory_overflow") and mode == "enforce":
            return _deny_output(
                normalized_provider,
                _mandatory_overflow_block_message(
                    str(state.get("mandatory_invalid_reason") or state.get("bootstrap_error") or "")
                ),
            )
        if state.get("bootstrap_error") and mode == "enforce":
            return _deny_output(
                normalized_provider,
                "MemoryGuard 上下文加载不可用，工具执行已安全停止。请检查绑定或 Hook 状态。",
            )
        tool_name, tool_input = _hook_tool_parts(payload)
        if _targets_native_memory(tool_name, tool_input):
            reason = (
                "MemoryGuard 已接管长期记忆：禁止 Agent 写入宿主原生记忆路径。"
                "请改用 memoryguard_memory_write；人工 GUI 删除/恢复不受影响。"
            )
            if mode == "enforce":
                return _deny_output(normalized_provider, reason)
        elif _is_other_memory_write(tool_name, tool_input):
            reason = (
                "检测到其他记忆 MCP 写入。正式接管模式只允许 "
                "MemoryGuard 作为长期记忆写入端。"
            )
            if mode == "enforce":
                return _deny_output(normalized_provider, reason)
        if normalized_provider == "cursor":
            if _is_memoryguard_bootstrap(tool_name, tool_input):
                def mark_cursor_bootstrap_pending(state: dict[str, Any]) -> None:
                    if not state.get("mandatory_overflow"):
                        state["bootstrap_pending"] = True
                _update_state(
                    root, normalized_provider, session_id,
                    mutator=mark_cursor_bootstrap_pending,
                )
                return v2_result
            is_subagent = bool(
                payload.get("subagent_id") or payload.get("agent_id")
            )
            if (
                mode == "enforce"
                and state
                and not state.get("bootstrap_ok")
                and not is_subagent
            ):
                return _deny_output(
                    normalized_provider,
                    "开始本轮工具操作前，先调用 "
                    "memoryguard_context_bootstrap(task=当前用户请求)。",
                )
        return v2_result

    if event == "post_tool":
        tool_name, tool_input = _hook_tool_parts(payload)
        tool_result = _hook_tool_result(payload)
        def update_post_tool_state(state: dict[str, Any]) -> None:
            if _is_memoryguard_bootstrap(tool_name, tool_input):
                success, reason = _coerce_tool_result_status(tool_result)
                state["bootstrap_pending"] = False
                verified_packet = _verified_bootstrap_packet(tool_result, agent_instance_id)
                package_clear = (
                    success is True
                    and verified_packet is not None
                    and not _is_mandatory_overflow_error(
                        verified_packet.get("error"), verified_packet
                    )
                )
                if package_clear:
                    normalized = _normalize_native_v2_packet(
                        verified_packet, workspace=root, provider=normalized_provider,
                        agent_instance_id=agent_instance_id, share_group_id=share_group_id,
                        session_id=session_id, event="post_tool",
                    )
                    state["mandatory_overflow"] = False
                    state["mandatory_invalid_reason"] = ""
                    state["mandatory_rule_ids"] = list(normalized.get("mandatory_rule_ids") or [])
                    state["mandatory_match_receipts"] = list(normalized.get("mandatory_match_receipts") or [])
                    state["context_hash"] = _short_hash(json.dumps(verified_packet, sort_keys=True, ensure_ascii=False))
                if success is True and not state.get("mandatory_overflow"):
                    state["bootstrap_ok"] = True
                    state["bootstrap_error"] = ""
                    state["context_hash"] = state.get("context_hash") or _short_hash(
                        "post-tool-bootstrap:" + session_id
                    )
                    state["bootstrap_retry_claimed"] = False
                    state["bootstrap_retry_claimed_at"] = 0
                elif success is False:
                    state["bootstrap_ok"] = False
                    state["context_hash"] = ""
                    state["bootstrap_error"] = "bootstrap_tool_failed"
                elif not state.get("bootstrap_ok"):
                    state["bootstrap_ok"] = False
                    state["context_hash"] = ""
                    state["bootstrap_error"] = "bootstrap_result_unverified"
            if _is_memoryguard_write(tool_name, tool_input):
                success, reason = _coerce_tool_result_status(tool_result)
                if success is True:
                    state["write_seen"] = True
                    state.pop("write_failed", None)
                    state.pop("write_error", None)
                elif success is False:
                    state["write_failed"] = True
                    state["write_error"] = reason or "memoryguard write tool reported failure"
        if _is_memoryguard_bootstrap(tool_name, tool_input) or _is_memoryguard_write(
            tool_name, tool_input
        ):
            _update_state(
                root, normalized_provider, session_id,
                mutator=update_post_tool_state,
            )
        _best_effort_codegraph_file_refresh(
            workspace=root,
            payload={
                **payload,
                "share_group_id": share_group_id,
                "agent_instance_id": agent_instance_id,
                "provider": normalized_provider,
            },
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
        )
        return v2_result

    if event == "pre_compact" and not v2_result:
        state = _load_state(root, normalized_provider, session_id)
        if state.get("durable_candidate") and not state.get("write_seen"):
            if normalized_provider == "codex":
                return {}
            return _context_output(
                normalized_provider, event, _COMPACT_REMINDER,
            )

    return v2_result


run_hook = _run_hook_unlocked



def _read_stdin_json() -> dict[str, Any]:
    """CLI entrypoint: binary utf-8-sig decode (Cursor BOM-safe)."""
    return read_hook_stdin_json()


def _configure_utf8_stdio() -> None:
    # Hook command already uses ``python -X utf8``. Reconfiguring stdin
    # here can drain the text wrapper and make buffer.read() return empty.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        errors = "backslashreplace" if name == "stderr" else "strict"
        reconfigure(encoding="utf-8", errors=errors)


_PRETOOL_REDACT_KEYS = frozenset({
    "authorization",
    "x-api-key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "password",
    "cookie",
})


def _redact_hook_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if lowered in _PRETOOL_REDACT_KEYS or (
                "api" in lowered and "key" in lowered
            ):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_hook_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_hook_payload(item) for item in value[:32]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…"
    return value


def _dump_last_pretool(
    workspace: str | Path,
    payload: dict[str, Any],
    *,
    name: str = "last_pretool.json",
) -> None:
    try:
        path = _runtime_root(Path(workspace)) / name
        blob = {
            "at": _now_iso(),
            "tool_name": str(
                payload.get("tool_name")
                or payload.get("toolName")
                or payload.get("name")
                or ""
            ),
            "keys": sorted(str(key) for key in payload),
            "stdin": dict(_LAST_STDIN_META),
            "payload": _redact_hook_payload(payload),
        }
        text = json.dumps(blob, ensure_ascii=False, indent=2)
        if len(text) > 16000:
            text = text[:16000] + "\n"
        _atomic_write_text(path, text + "\n")
    except Exception:
        return


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        payload = _read_stdin_json()
        _configure_utf8_stdio()
        if args.event in {"pre_tool", "post_tool"}:
            kind = "pretool" if args.event == "pre_tool" else "posttool"
            _dump_last_pretool(
                args.workspace,
                payload,
                name=f"last_{kind}-{args.provider}.json",
            )
        result = run_hook(
            provider=args.provider,
            event=args.event,
            workspace=args.workspace,
            agent_instance_id=args.agent_id,
            share_group_id=args.share_group_id,
            payload=payload,
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except BaseException as exc:  # noqa: BLE001 - a hook must never exit silently
        # BaseException, not Exception: a SystemExit raised deeper in the runtime
        # escaped this handler and left stdout empty.  The host reports that as
        # "hook returned no output" and, being fail-closed, blocks the tool with
        # no reason attached — which is indistinguishable from a hang.
        if isinstance(exc, SystemExit) and not exc.code:
            raise

        reason = f"MemoryGuard Hook 执行失败，已阻止本次工具调用：{exc}"
        try:
            from .runtime_lease import split_brain_hint

            hint = split_brain_hint(args.workspace)
            if hint.get("split_brain"):
                reason = (
                    hint["summary"]
                    + "\n最常见成因：升级后旧版本进程没有退出。"
                    + "\n修复：" + hint["remedy"] + "（或结束这些进程后重启宿主）"
                )
        except BaseException:
            pass

        # PreToolUse is the only fail-closed runtime event.  Other failures
        # surface through status/heartbeat without bricking the host session.
        if args.event == "pre_tool":
            try:
                sys.stdout.write(
                    json.dumps(_deny_output(args.provider, reason), ensure_ascii=False)
                )
            except BaseException:
                # Last resort still denies.  Writing "{}" here would read as
                # allow and silently disarm the gate on the very path that
                # exists because something already went wrong.
                sys.stdout.write(
                    json.dumps(_deny_output(args.provider, "MemoryGuard Hook 失败，已阻止本次工具调用"))
                )
            return 0
        sys.stderr.write(f"memoryguard hook error: {reason}\n")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m memoryguard.host_hooks")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--provider", required=True)
    run.add_argument("--event", required=True, choices=tuple(_EVENT_NAMES))
    run.add_argument("--workspace", required=True)
    run.add_argument("--agent-id", required=True)
    run.add_argument("--share-group-id", required=True)
    run.add_argument("--managed-by", default="")
    run.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
