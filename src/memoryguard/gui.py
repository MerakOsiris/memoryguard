"""原生桌面窗口 GUI（spec §6.1 第3步的桌面形态）。

依赖策略冲突解决（PREFERENCES §6）:
- spec §1.3/§10 要求零依赖、无供应链风险
- 用户需求要求原生桌面窗口，需 pywebview（第三方）
- 选择: pywebview 作为可选依赖 `memoryguard[gui]`，Core 本体保持零依赖。
  未安装时 open 自动降级到 localhost/HTML/文本。

能力降级链（spec §6.1）:
1. 桌面原生窗口 (pywebview 已装)  -- 本模块
2. localhost 浏览器窗口 (标准库 http.server)  -- 本模块
3. 静态 HTML 文件 (webbrowser.open file://)  -- cli.py
4. 结构化文本 + JSON 路径  -- cli.py
"""

from __future__ import annotations

import http.server
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Mapping


_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
)


class BuildCancelled(Exception):
    """用户取消构建投影。"""


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------


def has_native_gui() -> bool:
    """探测 pywebview 是否可用（可选依赖）。"""
    try:
        import webview  # noqa: F401

        return True
    except ImportError:
        return False


def _window_icon_path() -> Path | None:
    """Return the packaged raster icon derived from the UI brand orb."""
    suffix = ".ico" if sys.platform == "win32" else ".png"
    path = Path(__file__).parent / "static" / f"memoryguard-icon{suffix}"
    return path if path.is_file() else None


def _set_windows_app_user_model_id() -> bool:
    """Give hosted pythonw windows an independent Windows taskbar identity."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return (
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MemoryGuard.Desktop",
            )
            == 0
        )
    except (AttributeError, OSError):
        return False


def _start_webview(webview) -> None:
    """Start pywebview with the MemoryGuard window/taskbar icon."""
    icon_path = _window_icon_path()
    if icon_path:
        webview.start(icon=str(icon_path))
    else:
        webview.start()


def _install_windows_restore_recovery(window, url: str) -> None:
    """Recover a WinForms/WebView2 surface after a real minimize cycle.

    WebView2 can keep its process and localhost connection alive while losing
    the rendered surface after a WinForms window is restored.  Microsoft
    recommends hiding the WebView container while its host is minimized.  Do
    that first, then show/refresh and navigate once the host is visible again.
    Limit the recovery to a real minimize cycle so ordinary resize/maximize
    events never reload the application.
    """
    if os.name != "nt":
        return
    events = getattr(window, "events", None)
    minimized_event = getattr(events, "minimized", None)
    restored_event = getattr(events, "restored", None)
    if minimized_event is None or restored_event is None:
        return

    state_lock = threading.Lock()
    recovery_state: dict[str, Any] = {
        "generation": 0,
        "minimized": False,
        "scheduled_generation": None,
        "closed": False,
    }

    def _set_renderer_visible(visible: bool) -> bool:
        native = getattr(window, "native", None)
        control = getattr(native, "webview", None)
        if native is None or control is None:
            return False

        def _apply() -> None:
            control.Visible = bool(visible)
            if visible:
                control.BringToFront()
                control.Invalidate()
                control.Update()
                control.Refresh()
                native.Invalidate()
                native.Update()

        try:
            if bool(getattr(control, "InvokeRequired", False)):
                from System import Action

                control.Invoke(Action(_apply))
            else:
                _apply()
            return True
        except Exception:
            return False

    def _mark_minimized() -> None:
        # Keep renderer visibility and lifecycle state in one critical
        # section.  A delayed restore callback can no longer race a second
        # minimize and make the WebView visible again behind a minimized host.
        with state_lock:
            if recovery_state["closed"]:
                return
            recovery_state["generation"] += 1
            recovery_state["minimized"] = True
            recovery_state["scheduled_generation"] = None
            _set_renderer_visible(False)

    def _mark_closed() -> None:
        with state_lock:
            recovery_state["closed"] = True
            recovery_state["minimized"] = False
            recovery_state["scheduled_generation"] = None
            recovery_state["generation"] += 1

    def _recover_after_restore(*_dimensions) -> None:
        with state_lock:
            if recovery_state["closed"] or not recovery_state["minimized"]:
                return
            generation = int(recovery_state["generation"])
            if recovery_state["scheduled_generation"] == generation:
                return
            recovery_state["scheduled_generation"] = generation

        def _reload_surface() -> None:
            # Hold the lifecycle lock through the UI recovery.  If a second
            # minimize arrives concurrently it runs immediately afterwards
            # and wins by hiding the renderer with a newer generation.
            with state_lock:
                if (
                    recovery_state["closed"]
                    or not recovery_state["minimized"]
                    or recovery_state["generation"] != generation
                    or recovery_state["scheduled_generation"] != generation
                ):
                    return
                try:
                    _set_renderer_visible(True)
                    window.load_url(str(url))
                except Exception:
                    # Window recovery is best-effort.  The localhost server
                    # and binding state remain authoritative if renderer died.
                    pass
                finally:
                    recovery_state["minimized"] = False
                    recovery_state["scheduled_generation"] = None

        # WinForms reports ``restored`` before WebView2 has recreated its
        # composition target on some machines.  Waiting for the subsequent
        # resize turn avoids navigating into the still-minimized surface.
        timer = threading.Timer(0.25, _reload_surface)
        timer.daemon = True
        timer.start()

    minimized_event += _mark_minimized
    restored_event += _recover_after_restore
    resized_event = getattr(events, "resized", None)
    if resized_event is not None:
        resized_event += _recover_after_restore
    closed_event = getattr(events, "closed", None)
    if closed_event is not None:
        closed_event += _mark_closed


# ---------------------------------------------------------------------------
# 1. 桌面原生窗口
# ---------------------------------------------------------------------------


def open_native_window(html_content: str, title: str = "MemoryGuard") -> int:
    """用 pywebview 弹原生桌面窗口加载 HTML 内容。

    返回退出码：0 成功，3 不可用需回退。
    阻塞调用：窗口关闭前不返回。
    """
    if not has_native_gui():
        return 3
    import webview

    _set_windows_app_user_model_id()
    # pywebview 加载 HTML 字符串，无需临时文件、无需 HTTP server
    webview.create_window(
        title=title,
        html=html_content,
        width=1440,
        height=900,
        min_size=(800, 600),
    )
    _start_webview(webview)
    return 0


# ---------------------------------------------------------------------------
# 2. localhost 浏览器窗口（降级路径）
# ---------------------------------------------------------------------------

import json as _json
from urllib.parse import urlparse, unquote
from .interactive import render_interactive_html


def _find_free_port() -> int:
    """绑定 127.0.0.1 随机端口（spec §1.3 安全要求）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _render_v2_knowledge_html(book_id: str = "") -> str:
    """Render the V2-only product knowledge surface."""
    from .knowledge_gui import render_book_detail_html, render_bookshelf_html

    return render_book_detail_html(book_id) if book_id else render_bookshelf_html()


def _choose_publish_target_path(kind: str = "file") -> dict[str, Any]:
    """Open the native publish target chooser after V2 authorizes the action."""
    normalized = "folder" if kind == "folder" else "file"
    if sys.platform == "win32":
        try:
            if normalized == "folder":
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
                    "$d.Description = '选择写回记忆文件夹'; "
                    "$d.ShowNewFolderButton = $true; "
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                    "{ Write-Output $d.SelectedPath }"
                )
            else:
                script = (
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "$d = New-Object System.Windows.Forms.OpenFileDialog; "
                    "$d.Title = '选择写回记忆文件'; "
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                    "{ Write-Output $d.FileName }"
                )
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=300,
            )
            if completed.returncode != 0:
                return {"ok": False, "error": (completed.stderr or "target chooser failed").strip()}
            output = (completed.stdout or "").strip()
            selected = output.splitlines()[-1] if output else ""
            target = str(Path(selected) / "memory.md") if selected and normalized == "folder" else selected
            return {"ok": bool(target), "target_file": target}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if normalized == "folder":
            selected = filedialog.askdirectory(title="选择写回记忆文件夹")
            target = str(Path(selected) / "memory.md") if selected else ""
        else:
            selected = filedialog.askopenfilename(title="选择写回记忆文件")
            target = selected or ""
        root.destroy()
        return {"ok": bool(target), "target_file": target}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _pick_path_with_dialog(window: Any, for_files: bool = False) -> dict[str, Any]:
    """Open the local path picker only after the V2 bridge has gated it."""
    if window is None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = (
                filedialog.askopenfilename(title="选择导入文件")
                if for_files
                else filedialog.askdirectory(title="选择来源目录")
            )
            root.destroy()
        except Exception as exc:
            return {"error": f"path_picker_unavailable: {exc}"}
        if not selected:
            return {"error": "cancelled"}
        path = Path(selected)
        return {"path": str(path.resolve()), "is_directory": path.is_dir()}
    try:
        import webview

        if for_files:
            # pywebview expects one filter string per tuple item.  Passing a
            # pipe-joined string makes the Windows backend iterate characters
            # and fail with "A is not a valid file filter".
            file_types = (
                "All files (*.*)",
                "Zip files (*.zip)",
                "JSON files (*.json)",
                "JSONL files (*.jsonl)",
            )
            result = window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=file_types,
            )
        else:
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"error": "cancelled"}
        selected = result if isinstance(result, str) else result[0]
        path = Path(selected)
        return {
            "path": str(path.resolve()),
            "is_directory": path.is_dir() if path.exists() else not for_files,
        }
    except Exception as exc:
        return {"error": f"dialog failed: {exc}"}


def _dispatch_gui_api_call(bridge: "SafeBridgeApi", method: str, args: list[Any] | tuple[Any, ...] | None) -> Any:
    """One business dispatch path shared by localhost tests and HTTP handler."""
    from .cutover_v2.surfaces import GUI_MUTATION_NAMES
    from .security import is_mutation_method

    values = list(args or [])
    if is_mutation_method(method) or method in GUI_MUTATION_NAMES:
        return bridge.request_mutation(method, values)
    if method in {"get_sandbox_status", "get_api_method_registry", "pick_path"}:
        fn = getattr(bridge, method, None)
        if not callable(fn):
            return {"ok": False, "error": "method_not_implemented", "method": method}
        return fn(*values) if values else fn()
    return bridge.call_readonly(method, values)


def open_localhost_window(
    workspace: str,
    *,
    auto_open: bool = True,
    native_webview: bool = False,
    native_title: str = "MemoryGuard 治理面板",
) -> tuple[int, str]:
    """启动临时本地 HTTP server + JSON API，返回 (退出码, URL)。

    安全加固：
    - 无通配 CORS（同源 only）
    - 会话令牌验证
    - API 方法白名单
    - 变更 API 走 SafeBridge 的 V2 原生运行时
    """
    from .security import (
        generate_session_token,
        is_allowed_method,
        detect_sandbox_mode,
    )

    port = _find_free_port()
    if port == 0:
        return 3, ""

    session_token = generate_session_token()
    from .access_context import AccessContext
    from .desktop_executor import SERVER_ADMIN_AGENT_ID
    trusted_access_context = AccessContext(
        # Localhost is a server-owned transport.  A fixed process principal
        # gives the V2 port the required identity without trusting browser
        # payloads or ambient environment variables.
        trusted_agent_id=SERVER_ADMIN_AGENT_ID,
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id=session_token,
        session_source="transport",
        session_trusted=True,
    )
    # 网页环境绑定 127.0.0.1,等价于本地 GUI
    # 沙箱状态只读当前可信进程环境；GUI 不替调用方修改安全边界。
    is_sandbox = detect_sandbox_mode()
    # Ordinary GUI calls use SafeBridge as the sole readonly/mutation
    # entrance. The HTTP handler never dispatches directly to GovernanceApi.
    bridge = SafeBridgeApi(
        workspace,
        direct_mutations=not is_sandbox,
        _trusted_access_context=trusted_access_context,
    )

    def inject_runtime_context(html: str, *, session_token: str, sandbox: bool) -> str:
        """把会话令牌与沙箱标记注入任意 HTML 页面（知识书库等独立路由）。"""
        script = (
            "<script>"
            f"window.__MG_SESSION__={_json.dumps(session_token)};"
            f"window.__MG_SANDBOX__={str(sandbox).lower()};"
            "</script>"
        )
        return html.replace("</head>", script + "</head>")

    # 将 session_token 注入 HTML
    html = render_interactive_html()
    html = html.replace(
        "</head>",
        f'<script>window.__MG_SESSION__="{session_token}";'
        f'window.__MG_SANDBOX__={str(is_sandbox).lower()};</script></head>',
    )
    html_bytes = html.encode("utf-8")

    # 准备静态文件目录(cytoscape.min.js 等)
    import shutil as _shutil
    ui_dir = Path(workspace) / ".memoryguard" / "ui"
    ui_dir.mkdir(parents=True, exist_ok=True)
    static_src = Path(__file__).parent / "static" / "cytoscape.min.js"
    if static_src.exists():
        _shutil.copy2(static_src, ui_dir / "cytoscape.min.js")
    icon_src = Path(__file__).parent / "static" / "memoryguard-icon.png"
    if icon_src.exists():
        _shutil.copy2(icon_src, ui_dir / "memoryguard-icon.png")

    _MIME_TYPES = {
        ".js": "application/javascript",
        ".css": "text/css",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
            elif parsed.path == "/api/health":
                self._json_response(200, {"ok": True, "sandbox": is_sandbox})
            elif parsed.path == "/knowledge":
                # KB5 知识书库书架页（注入会话令牌与沙箱标记）
                html = _render_v2_knowledge_html()
                html = inject_runtime_context(html, session_token=session_token, sandbox=is_sandbox)
                page_bytes = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)
            elif parsed.path.startswith("/knowledge/book/"):
                # KB5 知识书库详情页
                book_id = unquote(parsed.path[len("/knowledge/book/"):])
                html = _render_v2_knowledge_html(book_id)
                html = inject_runtime_context(html, session_token=session_token, sandbox=is_sandbox)
                page_bytes = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_bytes)))
                self.end_headers()
                self.wfile.write(page_bytes)
            else:
                # 静态文件服务(cytoscape.min.js 等)
                req_path = parsed.path.lstrip("/")
                # 防路径穿越
                safe_name = Path(req_path).name
                file_path = ui_dir / safe_name
                if safe_name and file_path.exists() and file_path.is_file():
                    ext = file_path.suffix.lower()
                    mime = _MIME_TYPES.get(ext, "application/octet-stream")
                    data = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error(404)

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                self.send_error(404)
                return
            method = parsed.path[len("/api/"):]

            # 验证会话令牌
            token = self.headers.get("X-Session-Token", "")
            if token != session_token:
                self._json_response(403, {"error": "invalid_session_token"})
                return

            # 验证方法白名单（KB5 知识书库 API 走单独路由，跳过白名单）
            if not is_allowed_method(method):
                self._json_response(501, {"error": f"unknown method: {method}"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                args = _json.loads(body.decode("utf-8")) if body else []
            except Exception as e:
                self.send_error(400, str(e))
                return

            # HTTP owns only token/JSON transport. Every GUI business method
            # uses the same SafeBridge dispatcher as the pywebview surface.
            try:
                result = _dispatch_gui_api_call(bridge, method, args)
                self._json_response(200, result if result is not None else {})
            except _CLIENT_DISCONNECT_ERRORS:
                return
            except Exception:
                self._json_response(500, {"ok": False, "error": "gui_dispatch_failed"})

        def _json_response(self, status: int, data: dict) -> None:
            payload = _json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                # 无通配 CORS：同源 only
                self.end_headers()
                self.wfile.write(payload)
            except _CLIENT_DISCONNECT_ERRORS:
                # The browser may abort while the response is being written.
                # There is no second response to send and no server error to log.
                return

        def do_OPTIONS(self):  # noqa: N802
            # 同源 only，不需要 CORS 预检
            self.send_error(405)

        def log_message(self, *args):  # 静默
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"MemoryGuard GUI running at {url} (sandbox={is_sandbox})", flush=True)
    if native_webview:
        if not has_native_gui():
            server.server_close()
            return 3, ""
        import threading as _threading
        import webview

        server_thread = _threading.Thread(
            target=server.serve_forever,
            name="memoryguard-localhost",
            daemon=True,
        )
        server_thread.start()
        try:
            _set_windows_app_user_model_id()
            bridge = SafeBridgeApi(
                workspace,
                direct_mutations=True,
                _trusted_access_context=trusted_access_context,
            )
            window = webview.create_window(
                native_title,
                url=url,
                js_api=bridge,
                width=1440,
                height=900,
                min_size=(800, 600),
            )
            bridge._set_window(window)
            _install_windows_restore_recovery(window, url)
            _start_webview(webview)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        return 0, url
    if auto_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0, url


# ---------------------------------------------------------------------------
# 统一入口：按能力降级
# ---------------------------------------------------------------------------


def open_report_window(html_content: str, *, title: str = "MemoryGuard") -> int:
    """按 spec §6.1 降级链打开报告窗口。

    顺序: 桌面原生窗口 -> localhost 浏览器 -> (调用方继续降级到 HTML 文件)
    返回退出码：0 成功，3 需调用方继续降级。
    """
    if has_native_gui():
        return open_native_window(html_content, title=title)
    # 无 pywebview 时，由调用方决定是否走 localhost 还是直接 HTML 文件
    return 3


# ---------------------------------------------------------------------------
# 交互式治理面板（参考 merakagent Tab 布局，非平面报告）
# ---------------------------------------------------------------------------

_GUI_SOURCE_READS = frozenset({
    "list_sources", "scan_sources", "preview_source", "get_raw_memory",
    "get_source_file_content", "extract_preview", "extract_preview_by_path",
})

_GUI_PATH_KEYS = frozenset({
    "path", "root_path", "resolved_path", "relative_path", "absolute_path",
    "source_path", "workspace", "target_path", "manifest_path",
    "published_target_file", "sidecar_memory_md", "dir_path", "export_path",
    "canonical_store_path", "targets", "changed_paths", "backup_paths",
})
_GUI_ROUTE_PATHS = frozenset({"v2", "none", "native", "unknown"})


def _stable_gui_source_ref(share_group_id: str, root_id: str) -> str:
    digest = hashlib.sha256(
        f"{share_group_id}\x00{root_id}".encode("utf-8", "replace"),
    ).hexdigest()[:20]
    return f"source:{digest}"


def _stable_gui_path_descriptor(value: Any, share_group_id: str, key: str) -> dict[str, str]:
    """Describe a filesystem path without returning the path itself."""
    if value is None:
        raw = ""
    elif isinstance(value, Path):
        raw = str(value)
    elif isinstance(value, str):
        raw = value
    else:
        # Bytes and opaque values must never be coerced into a path-bearing
        # string; callers may rely on their original type/bytes.
        return {"ref": "path:opaque", "hash": "", "summary": "opaque"}
    if not raw:
        return {"ref": "path:empty", "hash": "", "summary": "empty"}
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
    scoped = hashlib.sha256(
        f"{share_group_id}\x00{key}\x00{raw}".encode("utf-8", "replace"),
    ).hexdigest()[:20]
    # A basename is useful for the UI, while never exposing parent segments.
    try:
        name = Path(raw).name
    except (OSError, ValueError):
        name = ""
    summary = name if name and name not in {".", ".."} else "path"
    return {"ref": f"path:{scoped}", "hash": digest, "summary": summary[:96]}


def _redact_gui_paths(value: Any, share_group_id: str = "", *, _key: str = "") -> Any:
    """Recursively redact path-bearing GUI output; bytes remain byte-for-byte."""
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if lowered in _GUI_PATH_KEYS:
                if isinstance(raw_value, (bytes, bytearray, memoryview)):
                    output[key] = raw_value
                    continue
                if isinstance(raw_value, (list, tuple)):
                    output[key] = [
                        _stable_gui_path_descriptor(item, share_group_id, lowered)
                        if not isinstance(item, (bytes, bytearray, memoryview)) else item
                        for item in raw_value
                    ]
                    continue
                if lowered == "path" and isinstance(raw_value, str) and raw_value.casefold() in _GUI_ROUTE_PATHS:
                    output[key] = raw_value
                else:
                    output[key] = _stable_gui_path_descriptor(raw_value, share_group_id, lowered)
            else:
                output[key] = _redact_gui_paths(raw_value, share_group_id, _key=lowered)
        return output
    if isinstance(value, list):
        return [_redact_gui_paths(item, share_group_id, _key=_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_gui_paths(item, share_group_id, _key=_key) for item in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return value
    return value


def _safe_gui_error(exc: BaseException, *, code: str = "gui_operation_failed") -> dict[str, Any]:
    from .runtime_v2.public_safety import safe_exception_diagnostic

    return {
        "ok": False,
        "error": code,
        "code": code,
        "diagnostic": safe_exception_diagnostic(exc, code=code),
    }


def _redact_gui_source_result(
    method: str,
    result: Any,
    share_group_id: str,
) -> dict[str, Any]:
    """Return source data with stable refs and no filesystem path fields."""
    if not isinstance(result, Mapping):
        return _redact_gui_paths({"data": result}, share_group_id)
    if method == "list_sources":
        safe: list[dict[str, Any]] = []
        for raw in result.get("sources", []) if isinstance(result.get("sources"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            root_id = str(raw.get("root_id", "") or "").strip()
            safe_root_id = (
                root_id
                if root_id and not root_id.startswith(("/", "\\"))
                and not (len(root_id) > 2 and root_id[1] == ":")
                else ("REDACTED" if root_id else "NO_SOURCE")
            )
            safe.append({
                "source_ref": _stable_gui_source_ref(share_group_id, root_id) if root_id else "NO_SOURCE",
                "root_id": safe_root_id,
                "type": str(raw.get("type", "") or ""),
                "scope": str(raw.get("scope", "") or ""),
            })
        return _redact_gui_paths({"sources": safe, "total": len(safe)}, share_group_id)
    if method == "scan_sources":
        coverage = result.get("coverage")
        safe = {
            key: value for key, value in result.items()
            if key not in {"path", "workspace", "source_path"}
        }
        safe["scope_ref"] = _stable_gui_source_ref(share_group_id, "scan")
        if isinstance(coverage, Mapping):
            safe["coverage"] = dict(coverage)
        return _redact_gui_paths(safe, share_group_id)
    if method == "get_raw_memory":
        safe_groups: list[dict[str, Any]] = []
        for raw in result.get("groups", []) if isinstance(result.get("groups"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            item = {
                key: value for key, value in raw.items()
                if key not in {"root_path", "path", "workspace", "source_path"}
            }
            root_id = str(raw.get("root_id", "") or "").strip()
            item["source_ref"] = _stable_gui_source_ref(share_group_id, root_id) if root_id else "NO_SOURCE"
            safe_groups.append(item)
        safe = {key: value for key, value in result.items() if key != "groups"}
        safe["groups"] = safe_groups
        return _redact_gui_paths(safe, share_group_id)
    # Other source methods may include a path in a nested implementation
    # response.  Keep the business payload but redact path-bearing keys.
    from .runtime_v2.public_safety import sanitize_public_payload

    return _redact_gui_paths(sanitize_public_payload(dict(result)), share_group_id)


_GUI_DIRECT_PARAMETER_NAMES: dict[str, tuple[str, ...]] = {
    "update_rule_body": ("definition_id", "body", "expected_revision", "share_group_id"),
    "search_memory": ("query", "share_group_id", "semantic", "limit"),
    "edit_memory": ("memory_id", "body", "share_group_id"),
    "lock_memory": ("memory_id", "share_group_id"),
    "unlock_memory": ("memory_id", "share_group_id"),
    "delete_memory": ("memory_id", "share_group_id"),
    "list_memory": ("status", "kind", "share_group_id", "limit"),
    "list_memory_versions": ("share_group_id",),
    "get_memory": ("memory_id", "share_group_id"),
    "get_memory_status": ("share_group_id",),
    "get_rule_scope_options": ("share_group_id",),
    "list_rules_habits": ("share_group_id",),
    "list_rule_cockpit": ("share_group_id",),
    "preview_effective_rules": (
        "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role",
    ),
    "update_rule_audience": (
        "memory_id", "assignments", "share_group_id", "injection_policy", "priority", "confirmed",
    ),
}

_NO_INJECTED_RESULT = object()

_GUI_AUTHORITY_CLAIM_KEYS = frozenset({
    "_admin_override",
    "admin_override",
    "admin",
    "is_admin",
    "authority",
    "trusted_agent_id",
    "session_id",
    "session_source",
    "session_trusted",
    "__native_transport_capability",
})


def _contains_gui_authority_claim(value: Any) -> bool:
    """Reject payload authority claims before sandbox deferral.

    GUI authorization comes from the process-issued transport context.  A
    browser payload that carries an authority-shaped field must not become a
    deferred request, because the trusted desktop executor could otherwise see
    an apparently authorized request after the original boundary was crossed.
    """
    if isinstance(value, Mapping):
        if any(str(key).casefold() in _GUI_AUTHORITY_CLAIM_KEYS for key in value):
            return True
        return any(_contains_gui_authority_claim(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_gui_authority_claim(item) for item in value)
    return False


def _bind_gui_call_args(
    name: str,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> list[Any]:
    """Bind direct-call compatibility arguments without dropping mixed inputs."""
    public_kwargs = {
        key: value for key, value in kwargs.items() if not str(key).startswith("_")
    }
    values = list(args)
    if not public_kwargs:
        # Private controls are never forwarded as authority, but their
        # presence must not invalidate otherwise valid positional arguments.
        return values

    parameter_names = _GUI_DIRECT_PARAMETER_NAMES.get(name)
    if parameter_names is None:
        from .cutover_v2.surfaces import GUI_OPERATION_SPECS

        spec = GUI_OPERATION_SPECS.get(name)
        parameter_names = tuple(spec.parameters) if spec is not None else ()

    payload: dict[str, Any] = {}
    if parameter_names:
        for index, value in enumerate(values):
            if index < len(parameter_names):
                payload[parameter_names[index]] = value
            else:
                payload.setdefault("args", []).append(value)
    elif values:
        payload["args"] = values
    payload.update(public_kwargs)
    return [payload]


class SafeBridgeApi:
    """受限桥接 API：pywebview js_api 的安全代理。

    只暴露两个真实方法（pywebview 可枚举）：
    - call_readonly(method, args): 只读方法，直接转发
    - request_mutation(method, args): 变更方法，进入 V2 原生运行时

    前端统一通过这两个方法调用，不再直接访问 method 属性。
    """

    def __init__(
        self,
        workspace: str,
        *,
        direct_mutations: bool = False,
        _trusted_access_context=None,
        _v2_port=None,
    ):
        self._workspace = workspace
        self._window = None
        # 原生桌面窗口本身即执行端，变更应直接执行，不被 IDE 沙箱启发式推迟
        self._direct_mutations = bool(direct_mutations)
        # Keep an explicit native/facade seam for focused tests; production
        # construction always goes through get_v2_runtime_facade.
        self._v2_port = _v2_port
        self._runtime_facade = None
        self._trusted_access_context = _trusted_access_context
        # Explicit fixture seam only. Production construction never assigns
        # an inner API; all real calls remain behind the V2 manifest gate.
        self._inner = None

    def __getattr__(self, name: str):
        """Keep direct desktop calls on the same V2 dispatch boundary."""
        if name.startswith("_"):
            raise AttributeError(name)
        from .cutover_v2.surfaces import GUI_METHOD_NAMES, GUI_MUTATION_NAMES

        if name not in GUI_METHOD_NAMES:
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            if any(
                str(key).casefold() in _GUI_AUTHORITY_CLAIM_KEYS
                for key in kwargs
            ):
                return {
                    "ok": False,
                    "error": "admin_capability_required",
                    "code": "admin_capability_required",
                }
            values = _bind_gui_call_args(name, args, kwargs)
            if name in GUI_MUTATION_NAMES:
                return self.request_mutation(name, values)
            return self.call_readonly(name, values)

        return invoke

    def _dispatch_injected_inner(
        self,
        method: str,
        args: list | None,
        *,
        mutation: bool,
    ) -> Any:
        """Call an explicitly injected fixture adapter, never a production fallback."""
        if not self._direct_mutations:
            return _NO_INJECTED_RESULT
        inner = self._inner
        fn = getattr(inner, method, None) if inner is not None else None
        if not callable(fn):
            return _NO_INJECTED_RESULT
        values = list(args or [])
        if mutation and not values:
            try:
                import inspect

                if "confirmed" in inspect.signature(fn).parameters:
                    return fn(confirmed=True)
            except (TypeError, ValueError):
                pass
        return fn(*values)

    def _get_v2_runtime(self):
        if self._runtime_facade is not None:
            return self._runtime_facade
        candidate = self._v2_port
        if (
            candidate is not None
            and callable(getattr(candidate, "dispatch_gui", None))
            and (
                callable(getattr(candidate, "state_snapshot", None))
                or callable(getattr(candidate, "status", None))
            )
        ):
            self._runtime_facade = candidate
        else:
            from .cutover_v2.facade import get_v2_runtime_facade

            self._runtime_facade = get_v2_runtime_facade(
                self._workspace,
                v2_port=candidate,
            )
        return self._runtime_facade

    @staticmethod
    def _state_value(value: Any) -> str:
        if isinstance(value, Mapping):
            value = value.get("state", value.get("status", value.get("marker", "")))
        else:
            value = getattr(value, "state_value", None) or getattr(value, "state", value)
        value = getattr(value, "value", value)
        return str(value or "UNKNOWN").strip().upper() or "UNKNOWN"

    def _runtime_snapshot(self, runtime) -> tuple[Any | None, str]:
        reader = getattr(runtime, "state_snapshot", None)
        if not callable(reader):
            reader = getattr(runtime, "status", None)
        if not callable(reader):
            return None, ""
        try:
            import inspect

            parameters = inspect.signature(reader).parameters.values()
            accepts_positional = any(
                item.kind in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                }
                for item in parameters
            )
            snapshot = reader(self._workspace) if accepts_positional else reader()
        except Exception:
            return None, "UNKNOWN"
        return snapshot, self._state_value(snapshot)

    @staticmethod
    def _invoke_v2_runtime(
        runtime,
        method: str,
        args: list | None,
        *,
        mutation: bool,
        context: Any = None,
        snapshot: Any = None,
    ) -> dict:
        dispatch = getattr(runtime, "dispatch_gui", None)
        if not callable(dispatch):
            raise RuntimeError("v2_runtime_gui_dispatch_unavailable")
        kwargs: dict[str, Any] = {"mutation": mutation}
        if context is not None:
            kwargs["context"] = context
        if snapshot is not None:
            kwargs["snapshot"] = snapshot
        try:
            import inspect

            parameters = inspect.signature(dispatch).parameters
            accepts_kwargs = any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters.values()
            )
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if accepts_kwargs or key in parameters
            }
        except (TypeError, ValueError):
            kwargs.pop("snapshot", None)
        result = dispatch(method, args or [], **kwargs)
        return result if isinstance(result, dict) else {"data": result}

    @staticmethod
    def _upgrade_for_state(state: str) -> dict[str, Any] | None:
        from .runtime_v2.public_safety import v2_upgrade_payload

        if state in {"V1_ACTIVE", "V2_BUILDING", "UNKNOWN"}:
            payload = v2_upgrade_payload(state, surface="GUI")
            payload.update({"path": "v2", "status": "blocked"})
            return payload
        return None

    def _dispatch_v2(self, method: str, args: list | None = None, *, mutation: bool) -> dict:
        runtime = self._get_v2_runtime()
        snapshot, state = self._runtime_snapshot(runtime)
        upgrade = self._upgrade_for_state(state)
        if upgrade is not None:
            return upgrade
        context = self._trusted_bridge_context()
        return self._invoke_v2_runtime(
            runtime,
            method,
            args,
            mutation=mutation,
            context=context or None,
            snapshot=snapshot,
        )

    def _trusted_bridge_context(self) -> object:
        """Return a native capability issued from the real AccessContext.

        Browser payloads are never merged into this mapping.  The V2 native
        port recognizes the process-local sentinel attached by
        ``bind_native_transport_context``; a plain identity dictionary is not
        accepted as mutation authority.  Read-only calls intentionally do
        not receive this capability (their scope remains payload-scoped), so
        a read port that lacks mutation-context support is not blocked.
        """
        ctx = self._trusted_access_context
        if ctx is None:
            return {}
        try:
            from .access_context import AccessContext
            if not isinstance(ctx, AccessContext):
                return {}
            from .runtime_v2.native_ports import bind_native_transport_context
            from .runtime_v2.group_native import GroupControlService
            from .desktop_executor import SERVER_ADMIN_AGENT_ID
            agent_id = str(ctx.trusted_agent_id or "").strip()
            groups = GroupControlService(self._workspace, write=False)
            if ctx.is_admin and agent_id == SERVER_ADMIN_AGENT_ID:
                # The server-owned desktop/localhost principal must be able to
                # perform control-plane bootstrap operations (for example,
                # creating the first shared binding) before any governance
                # scope exists.  A persisted admin scope, when present, is
                # still folded into the capability; an ordinary "no active
                # binding yet" state remains a valid *unscoped* admin
                # capability.  Unexpected control-plane failures stay
                # fail-closed.
                share_group_id, scope_error = self._trusted_admin_scope(groups)
                if scope_error == "active_binding_required":
                    share_group_id = ""
                elif scope_error:
                    return {}
            else:
                # AccessContext carries the connection principal but not its
                # governance group. Resolve exactly one active binding from
                # the V2 system control plane; GUI preferences are never
                # authorization. Ordinary callers keep this existing path.
                share_group_id = ""
                if agent_id:
                    active = groups.active_binding_for_agent(agent_id)
                    if active is not None:
                        share_group_id = str(active.get("share_group_id") or "")
            from .access_context import effective_provider
            import hashlib

            provider = str(effective_provider() or "gui").strip().casefold()
            project_ref = str(Path(self._workspace).resolve())
            namespace_seed = "\x1f".join((project_ref, agent_id, share_group_id, provider, "knowledge"))
            namespace_id = "knowledge-" + hashlib.sha256(namespace_seed.encode("utf-8")).hexdigest()[:32]
            return bind_native_transport_context(
                ctx,
                workspace_id=str(self._workspace),
                share_group_id=share_group_id,
                project_ref=project_ref,
                provider=provider,
                runtime_role="gui",
                entrypoint="gui",
                namespace_id=namespace_id,
                sensitivity="normal",
                policy_class="private",
            )
        except Exception:
            # A missing/invalid capability must fail closed in V2.  The
            # facade will return a structured context error rather than
            # silently retrying without provenance.
            return {}

    @staticmethod
    def _trusted_admin_scope(groups: Any) -> tuple[str, str]:
        """Resolve the persisted, service-validated desktop admin scope.

        The desktop transport principal is intentionally not a target agent
        and has no binding of its own.  Its selected scope is therefore the
        only trusted source for the bridge group.  ``scope_state(...,
        admin=True)`` is part of the GroupControlService contract: an older
        service that cannot perform this administrator validation must fail
        closed rather than silently reverting to a default or browser scope.
        """
        from .desktop_executor import SERVER_ADMIN_AGENT_ID

        try:
            state = groups.scope_state(SERVER_ADMIN_AGENT_ID, admin=True)
        except Exception:
            return "", "trusted_context_unavailable"
        if (
            not isinstance(state, Mapping)
            or state.get("ok") is not True
            or state.get("empty") is not False
        ):
            return "", "active_binding_required"
        scope = state.get("scope")
        if not isinstance(scope, Mapping):
            return "", "active_binding_required"

        mode = str(scope.get("mode") or "").strip()
        if mode == "agent":
            target_agent = str(scope.get("agent_instance_id") or "").strip()
            if not target_agent:
                return "", "active_binding_required"
            try:
                binding = groups.active_binding_for_agent(target_agent)
            except Exception:
                return "", "trusted_context_unavailable"
            if binding is None:
                return "", "active_binding_required"
            group_id = str(binding.get("share_group_id") or "").strip()
            return (group_id, "") if group_id else ("", "active_binding_required")

        if mode == "share_group":
            group_id = str(scope.get("share_group_id") or "").strip()
            if not group_id:
                return "", "active_binding_required"
            try:
                bindings = groups.list_bindings(include_inactive=False).get("bindings", [])
            except Exception:
                return "", "trusted_context_unavailable"
            if not any(
                isinstance(binding, Mapping)
                and str(binding.get("share_group_id") or "").strip() == group_id
                and str(binding.get("agent_instance_id") or "").strip()
                for binding in bindings
            ):
                return "", "active_binding_required"
            return group_id, ""

        return "", "active_binding_required"

    def _source_scope(self) -> tuple[str, str]:
        """Resolve exactly one active binding for source introspection."""
        ctx = self._trusted_access_context
        principal = str(getattr(ctx, "trusted_agent_id", "") or "").strip() if ctx is not None else ""
        if not principal:
            return "", "active_binding_required"
        try:
            from .access_context import AccessContext
            from .runtime_v2.group_native import GroupControlService
            from .desktop_executor import SERVER_ADMIN_AGENT_ID

            groups = GroupControlService(self._workspace, write=False)
            if (
                type(ctx) is AccessContext
                and bool(ctx.is_admin)
                and principal == SERVER_ADMIN_AGENT_ID
            ):
                return self._trusted_admin_scope(groups)
            binding = groups.active_binding_for_agent(principal)
        except Exception:
            return "", "trusted_context_unavailable"
        if binding is None:
            return "", "active_binding_required"
        return str(binding.get("share_group_id") or ""), ""

    def _dispatch_source_read(self, method: str, args: list | None = None) -> dict:
        try:
            runtime = self._get_v2_runtime()
            snapshot, state = self._runtime_snapshot(runtime)
            upgrade = self._upgrade_for_state(state)
            if upgrade is not None:
                return upgrade
            group_id, scope_error = self._source_scope()
            if scope_error:
                return {"ok": False, "error": scope_error, "code": scope_error}
            result = self._invoke_v2_runtime(
                runtime,
                method,
                args,
                mutation=False,
                context=self._trusted_bridge_context() or None,
                snapshot=snapshot,
            )
            return _redact_gui_source_result(method, result, group_id)
        except Exception as exc:
            return _safe_gui_error(exc, code="gui_source_read_failed")

    def _set_window(self, window) -> None:
        self._window = window

    def dispatch_api(self, method: str, args: list | None = None) -> dict:
        """Single pywebview business-dispatch entry shared with localhost HTTP."""
        try:
            result = _dispatch_gui_api_call(self, str(method), args or [])
            return result if isinstance(result, dict) else {"data": result}
        except Exception as exc:
            return _safe_gui_error(exc, code="gui_dispatch_failed")

    def call_readonly(self, method: str, args: list | None = None) -> dict:
        """调用只读方法。

        严格校验方法是否在只读注册表中，拒绝变更方法。
        """
        from .security import is_readonly_method

        if not is_readonly_method(method):
            return {"error": "not a readonly method: <redacted>", "code": "not_readonly"}

        injected = self._dispatch_injected_inner(method, args, mutation=False)
        if injected is not _NO_INJECTED_RESULT:
            return injected if isinstance(injected, dict) else {"data": injected}

        if method in _GUI_SOURCE_READS:
            return self._dispatch_source_read(method, args)

        try:
            result = self._dispatch_v2(method, args, mutation=False)
            data = result.get("data") if isinstance(result.get("data"), Mapping) else result
            if result.get("path") == "v2":
                if data.get("host_action") == "choose_publish_target_path":
                    return _choose_publish_target_path(str(data.get("kind") or "file"))
            return _redact_gui_paths(result, "")
        except Exception as exc:
            return _safe_gui_error(exc)

    def request_mutation(self, method: str, args: list | None = None) -> dict:
        """调用变更方法。

        统一交给 V2 原生运行时，由其执行状态、权限与 TaskRun 门控。
        """
        from .security import is_mutation_method
        from .cutover_v2.surfaces import GUI_MUTATION_NAMES

        if not (is_mutation_method(method) or method in GUI_MUTATION_NAMES):
            return {"error": "not a mutation method: <redacted>", "code": "not_mutation"}

        if _contains_gui_authority_claim(args or []):
            return {
                "ok": False,
                "error": "admin_capability_required",
                "code": "admin_capability_required",
            }

        # Sandbox callers may request a desktop-confirmed mutation, but they
        # must never reach a writable V2 service in-process.  Queue the exact
        # business request for the trusted desktop executor and return a
        # stable deferred envelope.
        from .security import RequestQueue, detect_sandbox_mode
        if not self._direct_mutations and detect_sandbox_mode():
            request = RequestQueue(self._workspace).submit(method, list(args or []))
            return {
                "ok": True,
                "status": "deferred",
                "code": "mutation_deferred",
                "deferred": True,
                "request": request.to_dict(),
                "message": "mutation deferred to the trusted desktop executor",
            }

        injected = self._dispatch_injected_inner(method, args, mutation=True)
        if injected is not _NO_INJECTED_RESULT:
            return injected if isinstance(injected, dict) else {"data": injected}

        try:
            return self._dispatch_v2(method, args, mutation=True)
        except Exception as exc:
            return _safe_gui_error(exc)

    def get_api_method_registry(self) -> dict:
        """返回 API 方法注册表，供前端动态加载。"""
        from .security import get_api_method_registry
        return get_api_method_registry()

    def get_sandbox_status(self) -> dict:
        """返回沙箱状态。原生 GUI 直接执行时对外报告非沙箱。"""
        from .security import detect_sandbox_mode

        return {
            "sandbox": detect_sandbox_mode(),
            "direct_mutations": self._direct_mutations,
        }

    def pick_path(self, for_files: bool = False) -> dict:
        """系统目录/文件选择器。"""
        try:
            result = self._dispatch_v2(
                "pick_path", [bool(for_files)], mutation=False,
            )
            data = result.get("data") if isinstance(result.get("data"), Mapping) else result
            if result.get("path") == "v2" and data.get("host_action") == "pick_path":
                return _pick_path_with_dialog(self._window, bool(for_files))
            return _redact_gui_paths(result, "")
        except Exception as exc:
            return _safe_gui_error(exc, code="gui_path_picker_failed")


def open_interactive_window(workspace: str, title: str = "MemoryGuard 治理面板") -> int:
    """打开交互式治理面板（非平面报告）。

    通过 pywebview js_api 暴露 SafeBridgeApi（受限桥接 API）：
    - 只读与变更方法统一转发到 V2 原生运行时
    - 不直接暴露完整 GovernanceApi 对象
    返回退出码：0 成功，3 pywebview 不可用。
    """
    if not has_native_gui():
        return 3
    rc, _ = open_localhost_window(
        workspace,
        auto_open=False,
        native_webview=True,
        native_title=title,
    )
    return rc


# External desktop callers retain the public name, but it now resolves to the
# same V2-only bridge used by both GUI transports.
GovernanceApi = SafeBridgeApi
