"""Provider adapters: 把宿主 Agent 的原生记忆机制重定向到 MemoryGuard MCP。

每个 adapter 做三件事：
1. 写入宿主特定的指令文件，明确告诉 Agent 调用 memoryguard_memory_write MCP 工具记录记忆，
   不要用原生记忆机制（编辑 CLAUDE.md/AGENTS.md/.cursorrules 等）。
2. 生成/更新宿主的 MCP 配置，让它能启动 MemoryGuard MCP 服务器。
3. 明确请求全局接管时，通过 HostHookManager 安装该宿主已验证的用户级 Hook。

设计：
- install() 幂等：重复调用不产生重复配置（指令用标记段落替换，MCP 配置用 JSON key 覆盖）
- uninstall() 干净移除 install() 写入的内容（只动 memoryguard 段落/key，不碰用户其他内容）
- status() 区分 configured 与 runtime_verified；写入配置不等于宿主已经连接
- Hook 配置由 host_hooks 深模块统一拥有；Provider adapter 不复制宿主 Hook 细节
- 纯标准库，不引入新依赖
- 有 workspace 时优先项目级配置，避免不同项目的 Agent 身份互相覆盖
- 无 workspace 时才使用 Path.home() / 环境变量指向的用户级配置
- MCP 启动命令用当前解释器或不可变 runtime snapshot 的 python -X utf8 -m memoryguard.mcp_server
- 官方 Codex/provider 安装在写入 MCP 配置前选择已安装的非 editable wheel，或按当前打包源码内容键（Python 与 package data，忽略 cache/bytecode）选择/原子构建 snapshot；诊断不改用户安装
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from . import toml_compat as tomllib
from pathlib import Path
from typing import Any, Mapping

from .runtime_lease import inspect_distribution_origin
from .runtime_v2.public_safety import v2_upgrade_payload


# ===========================================================================
# 常量
# ===========================================================================

MCP_SERVER_NAME = "memoryguard"
MCP_MODULE = "memoryguard.mcp_server"
MCP_UTF8_ARGS = ["-X", "utf8", "-m", MCP_MODULE]
MCP_RUNTIME_DIRNAME = "mcp-runtime"
_RUNTIME_PYTHON_ENV = "MEMORYGUARD_RUNTIME_PYTHON"
_SNAPSHOT_KEY_LEN = 16
_SNAPSHOT_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "node_modules",
        "graphify-out",
        MCP_RUNTIME_DIRNAME,
    }
)
_SNAPSHOT_SKIP_SUFFIXES = frozenset({".pyc", ".pyo"})
_SNAPSHOT_ROOT_NAMES = frozenset(
    {"pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in"}
)


def _binding_plane_for_workspace(workspace: str | Path) -> str:
    """Require the V2 system control plane for production provider setup."""
    from .system.manifest import ManifestManager

    try:
        current = ManifestManager(Path(workspace)).current()
        state = current.get("state", current.get("status", "")) if isinstance(current, dict) else current.state
    except Exception as exc:
        raise ValueError("v2_manifest_state_unavailable") from exc
    marker = str(getattr(state, "value", state) or "").strip().upper()
    if marker in {"V2_READY", "V2_ACTIVE"}:
        return "v2"
    if marker in {"V1_ACTIVE", "V2_BUILDING"}:
        raise ValueError(v2_upgrade_payload(marker, surface="Provider")["code"])
    raise ValueError("v2_manifest_state_unavailable")


def _require_provider_state(workspace: str | Path, *, mutation: bool) -> None:
    """Apply the provider operation's V2 read/write gate."""
    plane = _binding_plane_for_workspace(workspace)
    if not mutation:
        return
    from .system.manifest import ManifestManager

    try:
        current = ManifestManager(Path(workspace)).current()
        raw_state = current.get("state", current.get("status", "")) if isinstance(current, dict) else current.state
        marker = str(getattr(raw_state, "value", raw_state) or "").strip().upper()
    except Exception as exc:
        # Test/control-plane shims may provide the already-validated V2 plane
        # without a persisted manifest.  The normal helper above still fails
        # closed for real unknown/unreadable manifests.
        if plane == "v2":
            return
        raise ValueError("v2_manifest_state_unavailable") from exc
    if marker == "V2_READY":
        raise ValueError("v2_not_active")
    if marker == "V2_ACTIVE":
        return
    if marker == "V1_ACTIVE" and plane == "v2":
        return
    raise ValueError("v2_manifest_state_unavailable")

# 指令文件中使用的标记（HTML 注释，Markdown/MDC 都安全）
_BEGIN_MARKER = "<!-- BEGIN memoryguard:provider-redirect -->"
_END_MARKER = "<!-- END memoryguard:provider-redirect -->"

# Codex TOML 配置使用的标记（# 注释）
_TOML_BEGIN = "# BEGIN memoryguard:provider-redirect"
_TOML_END = "# END memoryguard:provider-redirect"
_TOML_TABLE_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


# 共用指令正文：告诉 Agent 所有记忆操作走 MCP 工具
def _instruction_body(share_group_id: str = "default") -> str:
    gid = share_group_id or "default"
    return f"""\
## MemoryGuard 记忆重定向（正式接管）

长期记忆的**唯一真相源**是 MemoryGuard MCP 共享库（share_group_id=`{gid}`）。
不要使用各 Agent 原生记忆文件或 GUI 内置记忆；所有读写必须走 MCP。
宿主支持时，用户级 MemoryGuard Hook 会自动注入限额上下文、拦截可见的原生
记忆写入，并在明确的长期候选遗漏时续跑一次；不得绕过或删除该 Hook。

### 记录记忆
调用 `memoryguard_memory_write` 工具：
- `body`（必填）：记忆内容
- `kind`（可选）：preference|fact|project|procedure|episode|correction，留空则自动分类
- `injection_policy`（可选）：默认 `relevant`。仅当用户明确要求“规则/必须/默认长期遵循/强制”时写 `always`；普通事实、偏好和 procedure 仍写 `relevant`，不得把所有 procedure 自动设为强制。
- `priority`（可选）：`always` 规则的有界排序整数，默认 0。
- 不要传 `agent_instance_id` 或覆盖 `share_group_id`；MCP 连接已可信绑定到当前 Agent 和共享组

### 搜索 / 读取
- `memoryguard_context_bootstrap`：新任务开始时一次性加载有预算的长期记忆上下文
- `memoryguard_memory_search`：按 query、status、limit 搜索 governed memories；不支持 kind 或 semantic 过滤。已知 memory_id 时不要用于发现，改用 `memoryguard_memory_read`
- `memoryguard_memory_read`：按 memory_id 读取单条
- `memoryguard_memory_status`：查看共享组状态

### 选择性回忆与对话注入
- 每个新任务优先调用一次 `memoryguard_context_bootstrap`，传入当前 `task`；同一任务不得重复调用
- Claude/Codex Hook 已提供本轮 bootstrap 上下文时不要重复调用；Cursor 以 Hook 的首次工具门控为准
- bootstrap 只补充长期记忆/长期规则；宿主当前对话上下文保持原样，不替换、不重复注入
- bootstrap 先注入独立预算的强制规则包，再召回相关记忆；强制包敏感或超限会失败封闭，只阻断普通任务工具。诊断、读取、memoryguard_memory_update 的 preview=true，以及 memoryguard_context_bootstrap 仍可用。普通工具要等受控治理完成且真实 bootstrap 成功后才解除。
- 仅在 bootstrap 后仍需精确治理查询时调用 `memoryguard_memory_search`
- 历史对话文件只是可选来源，必须先萃取为长期记忆；禁止把历史对话全文注入当前任务
- 需要精确原文时再用 `memoryguard_memory_read` 读取命中的单条记录

### 更新 / 删除
- `memoryguard_memory_update`：更新已知记忆的 body / kind / injection_policy / priority / audience；memory_id 必填，atom_id 仅在迁移 ID 歧义时使用；不支持 status
- `memoryguard_memory_delete`：软删除
- 不要用 `memoryguard_memory_update` 恢复 deleted 记录；删除是软删除，恢复或强制规则治理走 MemoryGuard GUI 治理流程，不要绕过治理路径。

### 规则
- 不要为了"记住"而编辑 CLAUDE.md / AGENTS.md / .cursorrules 等指令文件
- 不要把记忆写入 ~/.codex/memories、~/.claude/projects/*/memory 等本地文件
- 所有记忆操作都走 MCP，由 MemoryGuard 面板治理（去重、冲突、隔离、版本、supersede）
"""


_INSTRUCTION_BODY = _instruction_body()


# ===========================================================================
# 工具函数
# ===========================================================================


def _mcp_command(
    *,
    runtime_python: str | Path | None = None,
    launch: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return MemoryGuard MCP argv for provider config.

    PyPI/wheel installs keep the current interpreter. Editable or local-source
    installs must select or build an immutable snapshot before config write.
    Repository tests keep using src and do not run pip.
    """
    if runtime_python:
        return _mcp_launch_argv(str(runtime_python))
    selected = dict(launch or prepare_provider_mcp_launch(mutate=True))
    if not selected.get("ok"):
        raise ValueError(str(selected.get("reason") or "mcp_runtime_unavailable"))
    return list(selected["argv"])


def _prepare_provider_runtime() -> dict[str, Any]:
    """Select one MCP runtime for an install transaction and its Hook set."""
    launch = prepare_provider_mcp_launch(mutate=True)
    if not launch.get("ok"):
        raise ValueError(str(launch.get("reason") or "mcp_runtime_unavailable"))
    runtime_python = str(launch.get("python") or "").strip()
    if not runtime_python or not Path(runtime_python).is_file():
        raise ValueError("mcp_runtime_unavailable")
    return dict(launch)


def _is_immutable_install(origin: Mapping[str, Any] | None) -> bool:
    if not isinstance(origin, Mapping):
        return False
    kind = str(origin.get("install_kind") or "")
    return kind == "installed" and origin.get("editable") is not True


def _venv_python(snapshot_root: Path) -> Path:
    if os.name == "nt":
        return Path(snapshot_root) / "venv" / "Scripts" / "python.exe"
    return Path(snapshot_root) / "venv" / "bin" / "python"


def _path_from_file_url(url: str) -> Path | None:
    raw = str(url or "").strip()
    if not raw.startswith("file:"):
        return None
    from urllib.parse import unquote, urlparse

    parsed = urlparse(raw)
    path = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path = f"//{parsed.netloc}{path}"
    if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path.lstrip("/")
    try:
        resolved = Path(path)
    except Exception:
        return None
    return resolved if str(resolved) else None


def _source_root_from_direct_url(direct_url: str | Mapping[str, Any] | None) -> Path | None:
    if isinstance(direct_url, Mapping):
        payload = dict(direct_url)
    else:
        text = str(direct_url or "").strip()
        if not text:
            return None
        try:
            loaded = json.loads(text)
        except Exception:
            return None
        if not isinstance(loaded, Mapping):
            return None
        payload = dict(loaded)
    return _path_from_file_url(str(payload.get("url") or ""))


def _live_source_root() -> Path | None:
    try:
        import importlib.metadata
        text = importlib.metadata.distribution("agent-memguard").read_text("direct_url.json")
    except Exception:
        return None
    root = _source_root_from_direct_url(text)
    if root is None:
        return None
    if root.is_file() and root.suffix == ".whl":
        return root
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        return root
    return root if root.exists() else None


def _explicit_runtime_python(snapshot_python: str | None = None) -> str:
    """Honor an explicit interpreter. Never overwritten by snapshot selection."""
    text = str(snapshot_python or "").strip()
    if text:
        path = Path(text).expanduser()
        if path.is_file():
            return str(path)
    explicit = os.environ.get(_RUNTIME_PYTHON_ENV, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path)
    return ""


def _snapshot_path_excluded(path: Path, rel_parts: tuple[str, ...]) -> bool:
    if any(part in _SNAPSHOT_SKIP_DIRS for part in rel_parts):
        return True
    return path.suffix in _SNAPSHOT_SKIP_SUFFIXES


def _iter_snapshot_source_files(source_root: Path) -> list[Path]:
    """Collect packaged runtime files for the snapshot content key.

    Package roots include every shippable file (Python, GUI JS/icons, licenses,
    and other package data). Build/cache/VCS directories and generated bytecode
    are skipped. Repo tests/docs/dist outside those roots are not scanned; only
    existing root packaging manifests are added besides the package trees.
    """
    root = Path(source_root)
    files: list[Path] = []
    for name in sorted(_SNAPSHOT_ROOT_NAMES):
        candidate = root / name
        if candidate.is_file():
            files.append(candidate)
    package_dirs = [root / "src" / "memoryguard", root / "memoryguard"]
    scanned = False
    for package_dir in package_dirs:
        if not package_dir.is_dir():
            continue
        scanned = True
        try:
            for path in package_dir.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    rel_parts = path.relative_to(package_dir).parts
                except (OSError, ValueError):
                    continue
                if _snapshot_path_excluded(path, rel_parts):
                    continue
                files.append(path)
        except OSError:
            continue
    if scanned:
        return files
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if any(part in _SNAPSHOT_SKIP_DIRS for part in rel_parts):
                continue
            if path.suffix == ".py" or path.name in _SNAPSHOT_ROOT_NAMES:
                files.append(path)
    except OSError:
        return files
    return files


def _source_snapshot_key(source_root: Path) -> str:
    """Stable content key for an editable/local source tree or wheel file.

    Directory keys hash deterministic relative paths and bytes of packaged
    runtime files plus root packaging manifests.
    """
    digest = hashlib.sha256()
    root = Path(source_root)
    if root.is_file():
        digest.update(b"file\x00")
        digest.update(root.name.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
        try:
            digest.update(root.read_bytes())
        except OSError:
            digest.update(b"unreadable")
        return digest.hexdigest()[:_SNAPSHOT_KEY_LEN]
    digest.update(b"dir\x00")
    files = _iter_snapshot_source_files(root)
    unique: dict[str, Path] = {}
    for path in files:
        try:
            unique[path.relative_to(root).as_posix()] = path
        except (OSError, ValueError):
            continue
    for rel, path in sorted(unique.items()):
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        digest.update(rel.encode("utf-8", "surrogatepass"))
        digest.update(b"\x00")
        digest.update(payload)
        digest.update(b"\x00")
    return digest.hexdigest()[:_SNAPSHOT_KEY_LEN]


def _snapshot_python_if_ready(snapshot_root: Path | None) -> str:
    if snapshot_root is None:
        return ""
    python = _venv_python(snapshot_root)
    return str(python) if python.is_file() else ""


def _run_snapshot_command(argv: list[str]) -> None:
    import subprocess
    from .data_home import resolve_cache_home

    # Build tools can emit a mix of UTF-8 and the Windows locale. Output is
    # diagnostic-only here; byte capture avoids reader-thread decode failures.
    cache = resolve_cache_home() / "runtime-build"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memoryguard-install-", dir=cache) as temporary:
        env = {**os.environ, "TMP": temporary, "TEMP": temporary, "TMPDIR": temporary,
               "PIP_NO_CACHE_DIR": "1"}
        completed = subprocess.run(argv, check=False, capture_output=True, env=env)
    if int(completed.returncode or 0) != 0:
        raise RuntimeError("editable_install_snapshot_failed")


def _clear_empty_snapshot_dest(snapshot_root: Path) -> None:
    if not snapshot_root.exists():
        return
    try:
        empty = not any(snapshot_root.iterdir())
    except OSError as exc:
        raise RuntimeError("editable_install_snapshot_failed") from exc
    if not empty:
        raise RuntimeError("editable_install_snapshot_failed")
    snapshot_root.rmdir()


def _build_runtime_snapshot(
    *,
    snapshot_root: Path,
    source_root: Path,
    runner: Any = None,
) -> str:
    """Atomically install a non-editable copy into snapshot_root/venv. Never uses -e.

    Builds in a sibling staging directory and only publishes snapshot_root after
    pip and origin.json succeed. Failure leaves snapshot_root untouched.
    """
    snapshot_root = Path(snapshot_root)
    source_root = Path(source_root)
    existing = _snapshot_python_if_ready(snapshot_root)
    if existing:
        return existing
    _clear_empty_snapshot_dest(snapshot_root)
    run = runner or _run_snapshot_command
    snapshot_root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".mg-snap-", dir=str(snapshot_root.parent))
    )
    published = False
    try:
        python = _venv_python(staging)
        run(
            [
                sys.executable,
                "-m",
                "venv",
                "--system-site-packages",
                str(staging / "venv"),
            ]
        )
        run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--upgrade",
                str(source_root),
            ]
        )
        marker = {
            "install_kind": "installed",
            "install_reason": "runtime_snapshot",
            "editable": False,
            "source_key": _source_snapshot_key(source_root),
        }
        (staging / "origin.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if not python.is_file():
            raise RuntimeError("editable_install_snapshot_failed")
        try:
            os.replace(str(staging), str(snapshot_root))
            published = True
        except OSError:
            reused = _snapshot_python_if_ready(snapshot_root)
            if reused:
                return reused
            raise RuntimeError("editable_install_snapshot_failed")
        return str(_venv_python(snapshot_root))
    finally:
        if not published:
            shutil.rmtree(staging, ignore_errors=True)


def _mcp_launch_argv(python: str) -> list[str]:
    return [str(python), *MCP_UTF8_ARGS]


def _in_repository_tests() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _unavailable_launch(result: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "install_kind": result["install_kind"],
        "install_reason": result["install_reason"],
        "editable": result["editable"],
        "snapshot": False,
        "mutated": False,
        "argv": _mcp_launch_argv(sys.executable),
        "python": sys.executable,
    }


def prepare_provider_mcp_launch(
    *,
    mutate: bool = False,
    origin: Mapping[str, Any] | None = None,
    snapshot_python: str | None = None,
    snapshot_root: str | Path | None = None,
    source_root: str | Path | None = None,
    direct_url: str | Mapping[str, Any] | None = None,
    builder: Any = None,
) -> dict[str, Any]:
    """Select an immutable MCP interpreter, or build a snapshot when allowed.

    Diagnostics must call this with ``mutate=False`` so the user install is
    never changed. Official provider install uses ``mutate=True``.
    Editable/local-source installs key the snapshot to current packaged source
    (Python and package data such as GUI JS/icons) and publish it atomically;
    unchanged source reuses the existing snapshot. Cache/bytecode is ignored.
    """
    inspected = dict(origin or inspect_distribution_origin())
    src = Path(source_root).expanduser() if source_root else _source_root_from_direct_url(direct_url)
    if src is None:
        src = _live_source_root()
    root = Path(snapshot_root).expanduser() if snapshot_root else None
    if root is None:
        try:
            from .data_home import resolve_deployment_home
            deployment = src if src is not None and src.is_dir() else resolve_deployment_home()
            root = deployment / ".memoryguard" / MCP_RUNTIME_DIRNAME
        except Exception:
            root = None
    # A non-editable wheel is already the trusted immutable runtime.  Ignore
    # any inherited snapshot override: that value can point at a previous
    # release and would split a repair between the installed package and an
    # old MCP/Hook interpreter.
    if _is_immutable_install(inspected):
        result: dict[str, Any] = {
            "ok": True,
            "python": sys.executable,
            "argv": _mcp_launch_argv(sys.executable),
            "install_kind": str(inspected.get("install_kind") or "unknown"),
            "install_reason": str(inspected.get("install_reason") or "metadata_unavailable"),
            "editable": bool(inspected.get("editable")),
            "snapshot": False,
            "mutated": False,
        }
        return result

    selected = _explicit_runtime_python(snapshot_python)
    argv_python = selected or sys.executable
    result: dict[str, Any] = {
        "ok": True,
        "python": argv_python,
        "argv": _mcp_launch_argv(argv_python),
        "install_kind": str(inspected.get("install_kind") or "unknown"),
        "install_reason": str(inspected.get("install_reason") or "metadata_unavailable"),
        "editable": bool(inspected.get("editable")),
        "snapshot": bool(selected),
        "mutated": False,
    }
    if selected:
        result["python"] = selected
        result["argv"] = _mcp_launch_argv(selected)
        return result
    if not mutate:
        result["ok"] = True
        result["reason"] = "editable_install_snapshot_required"
        return result
    if _in_repository_tests() and builder is None:
        # Repository tests import src via pytest pythonpath and must not pip.
        result["reason"] = "repository_tests_use_src"
        return result
    if src is None or root is None:
        return _unavailable_launch(result, "editable_install_source_unavailable")
    keyed_root = root / _source_snapshot_key(src)
    ready = _snapshot_python_if_ready(keyed_root)
    if ready:
        return {
            "ok": True,
            "python": ready,
            "argv": _mcp_launch_argv(ready),
            "install_kind": result["install_kind"],
            "install_reason": result["install_reason"],
            "editable": result["editable"],
            "snapshot": True,
            "mutated": False,
            "reason": "runtime_snapshot",
        }
    build = builder or _build_runtime_snapshot
    try:
        python = build(snapshot_root=keyed_root, source_root=src)
    except Exception:
        return _unavailable_launch(result, "editable_install_snapshot_failed")
    python_text = str(python or "").strip()
    if not python_text or not Path(python_text).is_file():
        return _unavailable_launch(result, "editable_install_snapshot_failed")
    return {
        "ok": True,
        "python": python_text,
        "argv": _mcp_launch_argv(python_text),
        "install_kind": result["install_kind"],
        "install_reason": result["install_reason"],
        "editable": result["editable"],
        "snapshot": True,
        "mutated": True,
        "reason": "runtime_snapshot",
    }


def _mcp_server_config(
    agent_instance_id: str = "",
    memoryguard_workspace: str | Path = "",
    provider: str = "",
    control_scope: str = "project",
    runtime_python: str | Path | None = None,
) -> dict[str, Any]:
    """返回 MemoryGuard MCP server 的配置片段（JSON 格式，Claude/Cursor 通用）。"""
    cmd = _mcp_command(runtime_python=runtime_python)
    config: dict[str, Any] = {
        "command": cmd[0],
        "args": cmd[1:],
    }
    env: dict[str, str] = {}
    if agent_instance_id:
        env["MEMORYGUARD_AGENT_ID"] = agent_instance_id
    if provider:
        env["MEMORYGUARD_PROVIDER"] = provider
    control_scope_value = (
        "global" if str(control_scope).strip().lower() == "global" else "project"
    )
    env["MEMORYGUARD_CONTROL_SCOPE"] = control_scope_value
    if memoryguard_workspace:
        resolved_workspace = str(Path(memoryguard_workspace).expanduser().resolve())
        env["MEMORYGUARD_WORKSPACE"] = resolved_workspace
        # Global MemoryGuard is one user-level control/data plane, not a
        # project workspace.  Persist its canonical Data Home explicitly so a
        # provider launched later (or from another repository) resolves the
        # same store even when the installer's shell environment is gone.
        if control_scope_value == "global":
            env["MEMORYGUARD_HOME"] = resolved_workspace
    # The user-level control plane must be able to repair its own provider
    # integration.  It is launched from user-owned MCP configuration and
    # already has a pinned agent/workspace identity; persist the matching
    # local control capability rather than leaving global installs unable to
    # invoke their only repair route.  Project-scoped servers stay unprivileged.
    if control_scope_value == "global":
        env["MEMORYGUARD_ADMIN"] = "1"
        env["MEMORYGUARD_SESSION_ID"] = (
            f"provider-{provider or 'memoryguard'}-{agent_instance_id or 'control'}"
        )
        env["MEMORYGUARD_SESSION_SOURCE"] = "host"
    if env:
        config["env"] = env
    return config


def _configured_codex_runtime_python(config_home: str | Path) -> str:
    """Read an existing absolute MCP interpreter without creating a snapshot."""
    path = Path(config_home).expanduser() / "config.toml"
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if text.strip() else {}
        server = ((data.get("mcp_servers") or {}).get(MCP_SERVER_NAME) or {})
        command = str(server.get("command") or "").strip()
        candidate = Path(command).expanduser()
        if candidate.is_file() and candidate.is_absolute():
            return str(candidate)
    except (OSError, TypeError, ValueError):
        pass
    return ""


def _mcp_toml_section(
    agent_instance_id: str = "",
    memoryguard_workspace: str | Path = "",
    provider: str = "codex",
    control_scope: str = "project",
    runtime_python: str | Path | None = None,
) -> str:
    """返回 MemoryGuard MCP server 的 TOML 配置段落（Codex 用）。

    用 json.dumps 产出合法的 TOML basic string（自动转义反斜杠）。
    """
    cmd = _mcp_command(runtime_python=runtime_python)
    command_str = json.dumps(cmd[0])  # TOML basic string 与 JSON string 语法兼容
    args_items = ", ".join(json.dumps(a) for a in cmd[1:])
    lines = [
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"command = {command_str}\n"
        f"args = [{args_items}]"
    ]
    env_items: list[str] = []
    if agent_instance_id:
        env_items.append(
            f"MEMORYGUARD_AGENT_ID = {json.dumps(agent_instance_id)}"
        )
    if provider:
        env_items.append(
            f"MEMORYGUARD_PROVIDER = {json.dumps(provider)}"
        )
    control_scope_value = (
        "global" if str(control_scope).strip().lower() == "global" else "project"
    )
    env_items.append("MEMORYGUARD_CONTROL_SCOPE = " + json.dumps(control_scope_value))
    if memoryguard_workspace:
        resolved = str(Path(memoryguard_workspace).expanduser().resolve())
        env_items.append(
            f"MEMORYGUARD_WORKSPACE = {json.dumps(resolved)}"
        )
        if control_scope_value == "global":
            env_items.append(
                f"MEMORYGUARD_HOME = {json.dumps(resolved)}"
            )
    if control_scope_value == "global":
        env_items.extend([
            "MEMORYGUARD_ADMIN = \"1\"",
            "MEMORYGUARD_SESSION_ID = " + json.dumps(
                f"provider-{provider or 'memoryguard'}-{agent_instance_id or 'control'}"
            ),
            "MEMORYGUARD_SESSION_SOURCE = \"host\"",
        ])
    if env_items:
        lines.append(f"\nenv = {{ {', '.join(env_items)} }}")
    return "".join(lines)


def _replace_section(text: str, begin_marker: str, end_marker: str,
                     section_content: str) -> str:
    """幂等替换标记之间的内容。标记不存在则追加新段落。"""
    begin_idx = text.find(begin_marker)
    end_idx = text.find(end_marker)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        before = text[:begin_idx]
        after = text[end_idx + len(end_marker):]
        before = before.rstrip("\n")
        after = after.lstrip("\n")
        parts = []
        if before:
            parts.append(before + "\n\n")
        parts.append(f"{begin_marker}\n{section_content}\n{end_marker}")
        if after:
            parts.append("\n" + after)
        return "".join(parts).rstrip("\n") + "\n"
    # 追加新段落
    stripped = text.rstrip()
    if stripped:
        return f"{stripped}\n\n{begin_marker}\n{section_content}\n{end_marker}\n"
    return f"{begin_marker}\n{section_content}\n{end_marker}\n"


def _remove_unmanaged_toml_table(text: str, table_name: str) -> str:
    """移除标记段外的旧 TOML table，保留其他 table 与独立注释。

    0.3.0 之前的 Codex 配置直接写 ``[mcp_servers.memoryguard]``，没有
    MemoryGuard 标记。升级时必须先迁移旧 table，否则追加新版标记段会形成
    重复 table，导致整个 Codex config.toml 无法解析。
    """
    target_header = f"[{table_name}]"
    result: list[str] = []
    managed = False
    removing = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == _TOML_BEGIN:
            managed = True

        header = stripped.split("#", 1)[0].strip()
        is_table_header = (
            header.startswith("[")
            and header.endswith("]")
            and len(header) >= 3
        )
        if is_table_header:
            removing = False
            if header == target_header and not managed:
                removing = True
                continue

        if removing:
            # 独立注释可能描述后续 table，保留；旧 table 的键值全部移除。
            if not stripped or stripped.startswith("#"):
                result.append(line)
            continue

        result.append(line)
        if stripped == _TOML_END:
            managed = False

    return "".join(result)


def _toml_table_name(line: str) -> str:
    """Return a normalized bare TOML table name, or an empty string."""
    match = _TOML_TABLE_HEADER.match(line)
    if match is None:
        return ""
    return ".".join(
        part.strip().strip("\"").strip("'").casefold()
        for part in match.group(1).split(".")
    )


def _toml_assignment(line: str) -> tuple[str, Any] | None:
    """Parse one standalone TOML assignment without accepting malformed TOML."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, raw = stripped.split("=", 1)
    key = key.strip().strip("\"").strip("'")
    if not key:
        return None
    try:
        value = tomllib.loads("value = " + raw)["value"]
    except (KeyError, tomllib.TOMLDecodeError):
        return None
    return key, value


def _memoryguard_env_blocks(text: str) -> list[dict[str, str]]:
    """Recover MemoryGuard env maps from valid or duplicate-invalid TOML.

    This is evidence extraction only.  Full TOML validation remains mandatory
    before a repaired document can be written.
    """
    target = f"mcp_servers.{MCP_SERVER_NAME}"
    blocks: list[dict[str, str]] = []
    active: dict[str, str] | None = None
    mode = ""
    for line in text.splitlines():
        header = _toml_table_name(line)
        if header:
            if header == target:
                active = {}
                blocks.append(active)
                mode = "server"
            elif header == target + ".env" and active is not None:
                mode = "env"
            else:
                active = None
                mode = ""
            continue
        if active is None:
            continue
        assignment = _toml_assignment(line)
        if assignment is None:
            continue
        key, value = assignment
        if mode == "server" and key == "env" and isinstance(value, dict):
            active.update({str(k): str(v) for k, v in value.items()})
        elif mode == "env":
            active[str(key)] = str(value)
    return blocks


def _codex_toml_has_memoryguard_table(text: str) -> bool:
    """True when a Codex config still names a memoryguard MCP table."""
    target = f"mcp_servers.{MCP_SERVER_NAME}"
    for line in text.splitlines():
        table = _toml_table_name(line)
        if table == target or table.startswith(target + "."):
            return True
        assignment = _toml_assignment(line)
        if assignment is None:
            continue
        key, value = assignment
        folded = str(key).casefold()
        if folded == MCP_SERVER_NAME and isinstance(value, dict):
            return True
        if folded == "mcp_servers" and isinstance(value, dict) and MCP_SERVER_NAME in value:
            return True
    return False


def _codex_managed_mcp_state(text: str) -> tuple[str, str]:
    """Classify one Codex config.toml MemoryGuard block.

    Returns ``(kind, agent_id)`` where kind is:
    - ``valid``: complete managed markers and a parseable agent id
    - ``absent``: no managed block and no leftover MemoryGuard table
    - ``invalid``: malformed, partial, or conflicting MemoryGuard remnant
    """
    begin_idx = text.find(_TOML_BEGIN)
    end_idx = text.find(_TOML_END)
    has_begin = begin_idx != -1
    has_end = end_idx != -1
    if has_begin or has_end:
        if not (has_begin and has_end and end_idx > begin_idx):
            return "invalid", ""
        try:
            data = tomllib.loads(text) if text.strip() else {}
        except tomllib.TOMLDecodeError:
            return "invalid", ""
        server = ((data.get("mcp_servers") or {}).get(MCP_SERVER_NAME) or {})
        env = server.get("env") or {} if isinstance(server, dict) else {}
        agent_id = (
            accepted_config_agent_id(env, "codex")
            if isinstance(env, dict)
            else ""
        )
        if not agent_id:
            return "invalid", ""
        return "valid", agent_id
    if _codex_toml_has_memoryguard_table(text):
        return "invalid", ""
    return "absent", ""


def _owned_memoryguard_env_table(lines: list[str]) -> bool:
    """Recognize an orphan env table only when all assignments are ours."""
    assignments = [item for item in (_toml_assignment(line) for line in lines[1:]) if item]
    return bool(assignments) and all(
        str(key).startswith("MEMORYGUARD_") for key, _value in assignments
    )


def _owned_memoryguard_inline_server(line: str) -> bool:
    """Recognize `[mcp_servers] memoryguard = { ... }` only when ours."""
    assignment = _toml_assignment(line)
    if assignment is None or str(assignment[0]).casefold() != MCP_SERVER_NAME:
        return False
    _key, value = assignment
    if not isinstance(value, dict):
        return False
    return MCP_MODULE in " ".join(str(item) for item in value.get("args") or ())


def _toml_inline_value(value: Any) -> str:
    """Serialize a narrow TOML inline value, refusing unknown value types."""
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_inline_value(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [
            f"{json.dumps(str(key))} = {_toml_inline_value(item)}"
            for key, item in value.items()
        ]
        return "{ " + ", ".join(parts) + " }"
    raise ValueError("cannot safely reconcile unsupported inline TOML value")


def _reconcile_root_inline_mcp_servers(line: str) -> list[str] | None:
    """Drop an owned server from root `mcp_servers = { ... }` safely."""
    assignment = _toml_assignment(line)
    if assignment is None or str(assignment[0]).casefold() != "mcp_servers":
        return None
    _key, servers = assignment
    if not isinstance(servers, dict) or MCP_SERVER_NAME not in servers:
        return None
    server = servers[MCP_SERVER_NAME]
    if not isinstance(server, dict) or MCP_MODULE not in " ".join(
        str(item) for item in server.get("args") or ()
    ):
        raise ValueError(
            "cannot safely reconcile duplicate "
            "[mcp_servers.memoryguard]: inline server is not MemoryGuard-owned"
        )
    remaining = {key: value for key, value in servers.items() if key != MCP_SERVER_NAME}
    normalized: list[str] = []
    for name, config in remaining.items():
        if not isinstance(config, dict):
            raise ValueError("cannot safely reconcile non-table mcp_servers entry")
        normalized.append(f"[mcp_servers.{json.dumps(str(name))}]\n")
        normalized.extend(
            f"{json.dumps(str(key))} = {_toml_inline_value(value)}\n"
            for key, value in config.items()
        )
        normalized.append("\n")
    return normalized


def _reconcile_memoryguard_toml_tables(text: str) -> str:
    """Remove all owned MemoryGuard tables before one canonical upsert.

    Duplicate TOML table errors must be recoverable only when every removed
    MemoryGuard table identifies this MCP module.  Unknown same-named tables
    and all non-MemoryGuard syntax remain fail-closed: the candidate document
    is parsed before any write and rejected if it is still invalid.
    """
    target = f"mcp_servers.{MCP_SERVER_NAME}"
    lines = [
        line for line in text.splitlines(keepends=True)
        if line.strip() not in {_TOML_BEGIN, _TOML_END}
    ]
    result: list[str] = []
    index = 0
    while index < len(lines):
        table = _toml_table_name(lines[index])
        if not table:
            replacement = _reconcile_root_inline_mcp_servers(lines[index])
            if replacement is None:
                result.append(lines[index])
            else:
                result.extend(replacement)
            index += 1
            continue
        if table == "mcp_servers":
            end = index + 1
            while end < len(lines) and not _toml_table_name(lines[end]):
                end += 1
            section = lines[index:end]
            result.append(section[0])
            for line in section[1:]:
                assignment = _toml_assignment(line)
                if assignment is None or str(assignment[0]).casefold() != MCP_SERVER_NAME:
                    result.append(line)
                    continue
                if not _owned_memoryguard_inline_server(line):
                    raise ValueError(
                        "cannot safely reconcile duplicate "
                        "[mcp_servers.memoryguard]: inline server is not MemoryGuard-owned"
                    )
            index = end
            continue
        if table != target and not table.startswith(target + "."):
            result.append(lines[index])
            index += 1
            continue

        end = index + 1
        if table == target:
            while end < len(lines):
                next_table = _toml_table_name(lines[end])
                if next_table and next_table != target and not next_table.startswith(target + "."):
                    break
                end += 1
            section = lines[index:end]
            owned = MCP_MODULE in "".join(section)
        else:
            while end < len(lines) and not _toml_table_name(lines[end]):
                end += 1
            section = lines[index:end]
            owned = table == target + ".env" and _owned_memoryguard_env_table(section)

        if not owned:
            raise ValueError(
                "cannot safely reconcile duplicate "
                "[mcp_servers.memoryguard]: section is not MemoryGuard-owned"
            )
        # Preserve standalone comments and blank lines.  They may document an
        # adjacent user table; all owned assignments and headers are removed.
        result.extend(
            line for line in section
            if not line.strip() or line.lstrip().startswith("#")
        )
        index = end
    return "".join(result)


def _upsert_memoryguard_toml(text: str, section: str) -> str:
    # Codex's trust writer can place hook-state tables before our end marker.
    # If the valid MCP table already matches, preserve those bytes and order.
    try:
        current = tomllib.loads(text).get("mcp_servers", {}).get(MCP_SERVER_NAME)
        expected = tomllib.loads(section)["mcp_servers"][MCP_SERVER_NAME]
        if current == expected and text.count(_TOML_BEGIN) == text.count(_TOML_END) == 1:
            return text
    except (ValueError, KeyError, TypeError):
        pass
    return _replace_section(_reconcile_memoryguard_toml_tables(text), _TOML_BEGIN, _TOML_END, section)


def _remove_section(text: str, begin_marker: str, end_marker: str) -> str:
    """移除标记之间的内容，返回剩余文本。"""
    begin_idx = text.find(begin_marker)
    end_idx = text.find(end_marker)
    if begin_idx == -1 or end_idx == -1 or end_idx <= begin_idx:
        return text
    before = text[:begin_idx]
    after = text[end_idx + len(end_marker):]
    result = before + after
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    if not result.strip():
        return ""
    return result.rstrip("\n") + "\n"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_text_for_update(path: Path) -> str:
    """更新配置时严格读取；已有文件损坏不能被当成空文件覆盖。"""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read existing config as UTF-8: {path}: {exc}") from exc


def _load_json_for_update(path: Path) -> dict[str, Any]:
    text = _read_text_for_update(path)
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON config: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid JSON config root (expected object): {path}")
    return data


def _validate_toml(text: str, path: Path) -> None:
    if not text.strip():
        return
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML config: {path}: {exc}") from exc


def _backup_toml_before_repair(path: Path, original: str, replacement: str) -> Path | None:
    """Create a durable pre-repair copy only for a semantic TOML mutation."""
    if original == replacement or not path.is_file():
        return None
    # An existing complete provider block is already a recoverable,
    # MemoryGuard-owned representation.  Its canonical reserialization must
    # not accumulate backups on every idempotent repair pass.
    if _TOML_BEGIN in original and _TOML_END in original:
        return None
    # Keep the first pre-repair snapshot as the recovery point.  Some Codex
    # hook reconciliation paths temporarily remove our marker before their
    # final canonical rewrite; creating another timestamped copy there would
    # make a byte-identical second repair look mutating.
    if any(path.parent.glob(path.name + ".memoryguard-provider-*.bak")):
        return None
    try:
        if tomllib.loads(original) == tomllib.loads(replacement):
            return None
    except tomllib.TOMLDecodeError:
        # A duplicate owned table is invalid by definition.  Preserve its
        # exact bytes before normalization; a later non-owned parse failure
        # never reaches this helper because validation happens first.
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(path.name + f".memoryguard-provider-{stamp}.bak")
    _atomic_write_bytes(backup, original.encode("utf-8"))
    return backup


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace，避免中途写坏配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.memoryguard-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _apply_file_transaction(updates: list[tuple[Path, str | None]]) -> None:
    """原子应用一组文本更新；任一步失败则恢复所有原文件。"""
    merged: dict[Path, str | None] = {}
    for path, content in updates:
        merged[path] = content
    ordered = list(merged.items())
    snapshots = {
        path: path.read_bytes() if path.exists() else None
        for path, _ in ordered
    }
    try:
        for path, content in ordered:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, content.encode("utf-8"))
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, _ in reversed(ordered):
            original = snapshots[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(path, original)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"provider config update failed: {exc}; rollback failed: "
                + "; ".join(rollback_errors)
            ) from exc
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _set_mcp_server(data: dict[str, Any], server_name: str, config: dict[str, Any]) -> None:
    data.setdefault("mcpServers", {})[server_name] = config


def _remove_mcp_server(data: dict[str, Any], server_name: str) -> bool:
    servers = data.get("mcpServers", {})
    if server_name in servers:
        del servers[server_name]
        if not servers:
            del data["mcpServers"]
        return True
    return False


def _has_mcp_server(data: dict[str, Any], server_name: str) -> bool:
    return server_name in data.get("mcpServers", {})


def _other_adapter_uses_project_agents_md(
    workspace: Path, *, excluding: str,
) -> bool:
    """AGENTS.md 是 Codex/TRAE 共用面；卸载单方时不得破坏另一方。"""
    if excluding != "codex":
        codex_text = _read_text(workspace / ".codex" / "config.toml")
        if (
            f"[mcp_servers.{MCP_SERVER_NAME}]" in codex_text
            or _TOML_BEGIN in codex_text
        ):
            return True
    if excluding != "trae":
        trae_data = _load_json(workspace / ".trae" / "mcp.json")
        if _has_mcp_server(trae_data, MCP_SERVER_NAME):
            return True
    return False


# ===========================================================================
# 基类
# ===========================================================================


class ProviderAdapter:
    """Provider adapter 基类契约。

    子类实现 install / uninstall / status，把宿主原生记忆重定向到 MemoryGuard MCP。
    遵循现有 adapters.py 的契约风格（raise NotImplementedError，不用 ABC）。
    """

    provider_name: str = "base"

    def _select_install_workspace(
        self, workspace: str | Path = "", *, global_scope: bool = False,
    ) -> str:
        """Select the control plane before validating bindings or writing config.

        Global host integrations must always bind to the stable per-user
        MemoryGuard data home.  A source checkout/project path is valid only
        for an explicit project-scoped integration.  This prevents an upgrade
        or repair launched from a repository checkout from pinning a global MCP
        back to that checkout again.
        """
        self._superseded_project_workspace: Path | None = None
        if global_scope:
            from .data_home import resolve_data_home

            selected = getattr(self, "_repair_data_home", None)
            data_home = (
                Path(selected).expanduser().resolve()
                if selected is not None
                else resolve_data_home()
            )
            if workspace:
                requested = Path(workspace).expanduser().resolve()
                if requested != data_home:
                    self._superseded_project_workspace = requested
            self.workspace = data_home
            self._has_workspace = False
            control_workspace = getattr(self, "_superseded_project_workspace", None) or self.workspace
            _require_provider_state(control_workspace, mutation=True)
            return "global"
        if workspace:
            self.workspace = Path(workspace).expanduser().resolve()
            self._has_workspace = True
        control_workspace = getattr(self, "_superseded_project_workspace", None) or self.workspace
        _require_provider_state(control_workspace, mutation=True)
        return "project"

    def _cleanup_superseded_project_override(self) -> list[str]:
        """Remove only MemoryGuard-owned project config after global takeover."""
        project = getattr(self, "_superseded_project_workspace", None)
        if not isinstance(project, Path):
            return []
        try:
            project_adapter = type(self)(project)
            project_adapter.uninstall()
            return [
                f"已移除被全局配置取代的项目级 MemoryGuard 覆盖：{project}"
            ]
        except Exception as exc:  # global config is already valid; report cleanup debt
            return [
                "全局 MemoryGuard 配置已修复，但项目级旧覆盖清理失败："
                f"{project}: {type(exc).__name__}: {exc}"
            ]

    def _find_binding(self, agent_instance_id: str = "",
                      share_group_id: str = "") -> tuple[str | None, str | None]:
        """Read the authoritative binding plane selected by cutover state."""
        try:
            # Global install rewrites ``self.workspace`` to the stable user
            # data home, but authorization still belongs to the explicit
            # control workspace that initiated the takeover.
            control_workspace = getattr(self, "_superseded_project_workspace", None) or self.workspace
            plane = _binding_plane_for_workspace(control_workspace)
            if plane != "v2":
                return None, None
            if not agent_instance_id:
                return None, None
            from .runtime_v2.group_native import GroupControlService

            binding = GroupControlService(control_workspace, write=False).active_binding_for_agent(
                agent_instance_id
            )
            if binding is None:
                return None, None
            if share_group_id and str(binding.get("share_group_id") or "") != str(share_group_id):
                return None, None
            return str(binding.get("binding_id") or "") or None, str(binding.get("status") or "") or None
        except ValueError as exc:
            if str(exc) in {"v2_upgrade_required", "v2_manifest_state_unavailable"}:
                raise
            return None, None
        except Exception:
            return None, None

    def _require_active_binding(self, agent_instance_id: str,
                                share_group_id: str) -> str:
        """安装前先验证真实身份和授权，禁止生成不可用的匿名 MCP 配置。"""
        if not agent_instance_id:
            raise ValueError("agent_instance_id is required for MCP installation")
        owner = foreign_hook_owner(agent_instance_id, self.provider_name)
        if owner:
            raise ValueError(f"provider_identity_conflict: {owner}")
        binding_id, binding_status = self._find_binding(
            agent_instance_id, share_group_id
        )
        if not binding_id or binding_status != "active":
            raise ValueError(
                f"active binding not found for agent_instance_id="
                f"{agent_instance_id!r}, share_group_id={share_group_id!r}"
            )
        return binding_id

    def _install_host_hook(
        self,
        *,
        enabled: bool,
        agent_instance_id: str,
        share_group_id: str,
        config_home: str | Path | None = None,
        runtime_python: str | Path | None = None,
    ) -> dict[str, Any]:
        """Install the user-level hook only for an explicit global takeover."""
        from .host_hooks import HostHookManager

        if not enabled:
            return {
                "provider": self.provider_name,
                "supported": True,
                "configured": False,
                "status": "not_requested",
                "runtime_verified": False,
            }
        manager = HostHookManager(self.workspace)
        try:
            return manager.install(
                self.provider_name,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                mode="enforce",
                reconcile_trust=self.provider_name == "codex",
                config_home=config_home,
                runtime_python=runtime_python,
            )
        except Exception as exc:
            return {
                "provider": self.provider_name,
                "supported": True,
                "configured": False,
                "status": "error",
                "runtime_verified": False,
                "error": str(exc),
            }

    def _configured_result(self, *, instruction_path: Path, mcp_path: Path,
                           binding_id: str,
                           warnings: list[str] | None = None,
                           hook: dict[str, Any] | None = None) -> dict[str, Any]:
        hook_result = dict(hook or {})
        result_warnings = list(warnings or [])
        if hook_result.get("status") == "error":
            result_warnings.append(
                f"Hook 安装失败：{hook_result.get('error', 'unknown error')}"
            )
        elif hook_result and not hook_result.get("supported", True):
            result_warnings.append(
                "该宿主没有已验证的用户级 Hook seam；当前仅安装 MCP + 规则重定向"
            )
        return {
            "provider": self.provider_name,
            "configured": True,
            "installed": True,  # 兼容旧 API：仅表示配置文件已安装
            "status": "configured",
            "restart_required": True,
            "runtime_verified": False,
            "instruction_file": str(instruction_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": binding_id,
            "hook": hook_result,
            "hook_configured": bool(hook_result.get("configured")),
            "hook_runtime_verified": bool(hook_result.get("runtime_verified")),
            "warnings": result_warnings,
        }

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def uninstall(self) -> dict[str, Any]:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError


# ===========================================================================
# ClaudeAdapter
# ===========================================================================


class ClaudeAdapter(ProviderAdapter):
    """Claude Code adapter。

    - 指令文件：<workspace>/CLAUDE.md（项目级）；无 workspace 时 ~/.claude/CLAUDE.md（用户级）
    - MCP 配置：<workspace>/.mcp.json（项目级）；全局用户级为 ~/.claude.json
    - 支持环境变量 CLAUDE_CONFIG_DIR 覆盖 ~/.claude/
    """

    provider_name = "claude"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    def _config_dir(self) -> Path:
        configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".claude"

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / "CLAUDE.md"
        return self._config_dir() / "CLAUDE.md"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".mcp.json"
        return Path.home() / ".claude.json"

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )
        runtime = _prepare_provider_runtime()
        runtime_python = str(runtime["python"])

        # 先生成并验证全部内容，再事务写入。
        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        body = _instruction_body(share_group_id)
        new_content = _replace_section(content, _BEGIN_MARKER, _END_MARKER, body)

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _set_mcp_server(
            data,
            MCP_SERVER_NAME,
            _mcp_server_config(
                agent_instance_id, self.workspace, "claude", control_scope,
                runtime_python=runtime_python,
            ),
        )
        new_mcp_content = json.dumps(data, ensure_ascii=False, indent=2)
        _apply_file_transaction([
            (instr_path, new_content),
            (mcp_path, new_mcp_content),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            runtime_python=runtime_python,
        )
        warnings = self._cleanup_superseded_project_override()
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()

        content = _read_text_for_update(instr_path)
        instruction_update: str | None = content if instr_path.exists() else None
        if _BEGIN_MARKER in content:
            new_content = _remove_section(content, _BEGIN_MARKER, _END_MARKER)
            instruction_update = new_content if new_content.strip() else None

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _remove_mcp_server(data, MCP_SERVER_NAME)
        mcp_update = (
            json.dumps(data, ensure_ascii=False, indent=2) if data else None
        )
        _apply_file_transaction([
            (instr_path, instruction_update),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        content = _read_text(instr_path)
        instruction_installed = _BEGIN_MARKER in content and _END_MARKER in content

        data = _load_json(mcp_path)
        mcp_configured = _has_mcp_server(data, MCP_SERVER_NAME)

        hook_path = (self.workspace / ".claude" if self._has_workspace else self._config_dir()) / "settings.json"
        agent_id = _mcp_env_agent_id(data, self.provider_name, hook_path)
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


# ===========================================================================
# CodexAdapter
# ===========================================================================


class CodexAdapter(ProviderAdapter):
    """Codex CLI adapter。

    - 指令文件：<workspace>/AGENTS.md（项目级）；无 workspace 时 $CODEX_HOME/AGENTS.md
    - MCP 配置：<workspace>/.codex/config.toml（项目级）；无 workspace 时 $CODEX_HOME/config.toml
      段落：[mcp_servers.memoryguard]
    Router account/profile directories are transport aliases of one Codex
    program identity; they never become a new MemoryGuard principal.
    """

    provider_name = "codex"

    def __init__(self, workspace: str | Path = "", *, config_home: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()
        self._config_home = (
            Path(config_home).expanduser().resolve() if config_home else None
        )

    def _codex_user_dir(self) -> Path:
        if self._config_home is not None:
            return self._config_home
        from .agent_locator import current_codex_home

        return current_codex_home()

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / "AGENTS.md"
        return self._codex_user_dir() / "AGENTS.md"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".codex" / "config.toml"
        return self._codex_user_dir() / "config.toml"

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )
        runtime = _prepare_provider_runtime()
        runtime_python = str(runtime["python"])

        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        body = _instruction_body(share_group_id)
        new_content = _replace_section(content, _BEGIN_MARKER, _END_MARKER, body)

        mcp_path = self._mcp_config_path()
        toml_content = _read_text_for_update(mcp_path)
        section = _mcp_toml_section(
            agent_instance_id, self.workspace, control_scope=control_scope,
            runtime_python=runtime_python,
        )
        new_toml = _upsert_memoryguard_toml(toml_content, section)
        _validate_toml(new_toml, mcp_path)
        _backup_toml_before_repair(mcp_path, toml_content, new_toml)
        _apply_file_transaction([
            (instr_path, new_content),
            (mcp_path, new_toml),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            config_home=None if self._has_workspace else self._codex_user_dir(),
            runtime_python=runtime_python,
        )
        warnings = (
            ["Codex 仅在用户信任该项目后加载项目级 .codex/config.toml"]
            if self._has_workspace else []
        )
        warnings.extend(self._cleanup_superseded_project_override())
        replica: dict[str, Any] = {}
        if global_scope:
            replica = _repair_discovered_codex_homes(
                self.workspace,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                binding_id=binding_id,
                skip_config_home=self._codex_user_dir(),
                homes=getattr(self, "_repair_codex_homes", None),
                runtime_python=runtime_python,
            )
            warnings.extend(str(item) for item in replica.get("warnings") or ())
            try:
                _record_codex_program_identity(
                    self.workspace,
                    agent_instance_id=agent_instance_id,
                    share_group_id=share_group_id,
                    aliases=replica.get("aliases") or (),
                )
            except Exception:
                pass
        from .agent_locator import current_codex_home

        global_path = current_codex_home() / "config.toml"
        if self._has_workspace and global_path != mcp_path:
            global_text = _read_text(global_path)
            if _TOML_BEGIN in global_text or (
                "[mcp_servers.memoryguard]" in global_text
            ):
                warnings.append(
                    "检测到旧用户级 MemoryGuard MCP 配置；项目配置优先，"
                    "验证项目连接后可移除旧全局条目"
                )
        result = self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )
        if replica.get("aliases"):
            result["aliases"] = list(replica.get("aliases") or ())
        if replica.get("warnings"):
            result["profile_repair_errors"] = list(replica["warnings"])
        return result

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()

        content = _read_text_for_update(instr_path)
        instruction_update: str | None = content if instr_path.exists() else None
        keep_shared_instruction = (
            self._has_workspace
            and _other_adapter_uses_project_agents_md(
                self.workspace, excluding=self.provider_name
            )
        )
        if _BEGIN_MARKER in content and not keep_shared_instruction:
            new_content = _remove_section(content, _BEGIN_MARKER, _END_MARKER)
            instruction_update = new_content if new_content.strip() else None

        mcp_path = self._mcp_config_path()
        toml_content = _read_text_for_update(mcp_path)
        mcp_update: str | None = toml_content if mcp_path.exists() else None
        if _TOML_BEGIN in toml_content:
            new_toml = _remove_section(toml_content, _TOML_BEGIN, _TOML_END)
            _validate_toml(new_toml, mcp_path)
            mcp_update = new_toml if new_toml.strip() else None
        _apply_file_transaction([
            (instr_path, instruction_update),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        content = _read_text(instr_path)
        instruction_installed = _BEGIN_MARKER in content and _END_MARKER in content

        toml_content = _read_text(mcp_path)
        block_state, agent_id = _codex_managed_mcp_state(toml_content)
        mcp_configured = block_state == "valid"
        identity_home = mcp_path.parent
        if not mcp_configured:
            agent_id = ""

        if self._has_workspace and block_state == "absent":
            from .agent_locator import current_codex_home

            global_home = current_codex_home()
            global_mcp = global_home / "config.toml"
            if global_mcp != mcp_path:
                global_state, global_agent_id = _codex_managed_mcp_state(
                    _read_text(global_mcp),
                )
                if global_state == "valid":
                    mcp_configured = True
                    agent_id = global_agent_id
                    identity_home = global_home
                    global_instr = global_home / "AGENTS.md"
                    global_text = _read_text(global_instr)
                    global_instruction = (
                        _BEGIN_MARKER in global_text and _END_MARKER in global_text
                    )
                    if not instruction_installed and global_instruction:
                        instr_path = global_instr
                    instruction_installed = (
                        instruction_installed or global_instruction
                    )

        hook_ids = _hook_agent_ids(identity_home / "hooks.json", "codex")
        if hook_ids and agent_id not in hook_ids:
            agent_id = ""
            mcp_configured = False
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


# ===========================================================================
# CursorAdapter
# ===========================================================================


class CursorAdapter(ProviderAdapter):
    """Cursor adapter。

    - 指令文件：<workspace>/.cursor/rules/memoryguard.mdc（新格式，MemoryGuard 专属文件）
      无 workspace 时 ~/.cursor/rules/memoryguard.mdc
    - MCP 配置：<workspace>/.cursor/mcp.json（项目级）；无 workspace 时 ~/.cursor/mcp.json
    """

    provider_name = "cursor"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".cursor" / "rules" / "memoryguard.mdc"
        return Path.home() / ".cursor" / "rules" / "memoryguard.mdc"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".cursor" / "mcp.json"
        return Path.home() / ".cursor" / "mcp.json"

    def _instruction_content(self, share_group_id: str = "default") -> str:
        """生成 MDC 文件完整内容（含 frontmatter + 标记段落）。"""
        frontmatter = (
            "---\n"
            "description: MemoryGuard memory redirect\n"
            "alwaysApply: true\n"
            "globs: []\n"
            "---\n"
        )
        body = _instruction_body(share_group_id)
        return (
            frontmatter
            + _BEGIN_MARKER + "\n"
            + body + "\n"
            + _END_MARKER + "\n"
        )

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )
        runtime = _prepare_provider_runtime()
        runtime_python = str(runtime["python"])

        instr_path = self._instruction_path()
        new_instruction = self._instruction_content(share_group_id)

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _set_mcp_server(
            data,
            MCP_SERVER_NAME,
            _mcp_server_config(
                agent_instance_id, self.workspace, "cursor", control_scope,
                runtime_python=runtime_python,
            ),
        )
        new_mcp_content = json.dumps(data, ensure_ascii=False, indent=2)
        _apply_file_transaction([
            (instr_path, new_instruction),
            (mcp_path, new_mcp_content),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            runtime_python=runtime_python,
        )
        warnings: list[str] = self._cleanup_superseded_project_override()
        global_path = Path.home() / ".cursor" / "mcp.json"
        if self._has_workspace and global_path != mcp_path:
            global_data = _load_json(global_path)
            if _has_mcp_server(global_data, MCP_SERVER_NAME):
                warnings.append(
                    "检测到旧用户级 MemoryGuard MCP 配置；请先验证项目级连接，"
                    "再从 Cursor 全局 MCP 设置移除旧条目"
                )
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _remove_mcp_server(data, MCP_SERVER_NAME)
        mcp_update = (
            json.dumps(data, ensure_ascii=False, indent=2) if data else None
        )
        _apply_file_transaction([
            (instr_path, None),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        instruction_installed = (
            instr_path.exists()
            and _BEGIN_MARKER in _read_text(instr_path)
        )

        data = _load_json(mcp_path)
        mcp_configured = _has_mcp_server(data, MCP_SERVER_NAME)

        agent_id = _mcp_env_agent_id(data, self.provider_name, mcp_path.parent / "hooks.json")
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


# ===========================================================================
# TRAE Adapter
# ===========================================================================


class TraeAdapter(ProviderAdapter):
    """TRAE IDE / TRAE CN adapter。

    - 指令文件：<workspace>/AGENTS.md（TRAE 支持项目/子仓库 AGENTS.md）
    - MCP 配置：<workspace>/.trae/mcp.json（TRAE 项目级 MCP）
    - 无 workspace 时回退到 TRAE 用户级 mcp.json 与 user_rules/

    `.trae-cn/mcps/` 是 TRAE 的运行时工具元数据缓存，不是可写安装入口。
    """

    provider_name = "trae"

    def __init__(self, workspace: str | Path = ""):
        self._has_workspace = bool(workspace)
        self.workspace = Path(workspace).resolve() if workspace else Path.home()

    @staticmethod
    def _user_mcp_config_path() -> Path:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            candidates = [
                Path(appdata) / "TRAE SOLO CN" / "User" / "mcp.json",
                Path(appdata) / "TRAE" / "User" / "mcp.json",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            return candidates[0]
        return Path.home() / ".trae" / "mcp.json"

    def _instruction_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / "AGENTS.md"
        return Path.home() / ".trae-cn" / "user_rules" / "memoryguard.md"

    def _mcp_config_path(self) -> Path:
        if self._has_workspace:
            return self.workspace / ".trae" / "mcp.json"
        return self._user_mcp_config_path()

    def install(self, workspace: str | Path = "", share_group_id: str = "default",
                agent_instance_id: str = "",
                global_scope: bool = False) -> dict[str, Any]:
        control_scope = self._select_install_workspace(
            workspace, global_scope=global_scope,
        )
        binding_id = self._require_active_binding(
            agent_instance_id, share_group_id
        )
        runtime = _prepare_provider_runtime()
        runtime_python = str(runtime["python"])

        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        body = _instruction_body(share_group_id)
        new_content = _replace_section(content, _BEGIN_MARKER, _END_MARKER, body)

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _set_mcp_server(
            data,
            MCP_SERVER_NAME,
            _mcp_server_config(
                agent_instance_id, self.workspace, "trae", control_scope,
                runtime_python=runtime_python,
            ),
        )
        new_mcp_content = json.dumps(data, ensure_ascii=False, indent=2)
        _apply_file_transaction([
            (instr_path, new_content),
            (mcp_path, new_mcp_content),
        ])
        hook = self._install_host_hook(
            enabled=global_scope,
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            runtime_python=runtime_python,
        )

        warnings: list[str] = self._cleanup_superseded_project_override()
        global_path = self._user_mcp_config_path()
        if self._has_workspace and global_path != mcp_path:
            global_data = _load_json(global_path)
            if _has_mcp_server(global_data, MCP_SERVER_NAME):
                warnings.append(
                    "检测到旧用户级 MemoryGuard MCP 配置；请先验证项目级连接，"
                    "再从 TRAE 全局 MCP 设置移除旧条目"
                )
        return self._configured_result(
            instruction_path=instr_path,
            mcp_path=mcp_path,
            binding_id=binding_id,
            warnings=warnings,
            hook=hook,
        )

    def uninstall(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=True)
        instr_path = self._instruction_path()
        content = _read_text_for_update(instr_path)
        instruction_update: str | None = content if instr_path.exists() else None
        keep_shared_instruction = (
            self._has_workspace
            and _other_adapter_uses_project_agents_md(
                self.workspace, excluding=self.provider_name
            )
        )
        if _BEGIN_MARKER in content and not keep_shared_instruction:
            new_content = _remove_section(content, _BEGIN_MARKER, _END_MARKER)
            instruction_update = new_content if new_content.strip() else None

        mcp_path = self._mcp_config_path()
        data = _load_json_for_update(mcp_path)
        _remove_mcp_server(data, MCP_SERVER_NAME)
        mcp_update = (
            json.dumps(data, ensure_ascii=False, indent=2) if data else None
        )
        _apply_file_transaction([
            (instr_path, instruction_update),
            (mcp_path, mcp_update),
        ])

        return {
            "provider": self.provider_name,
            "installed": False,
            "configured": False,
            "status": "not_configured",
            "instruction_file": str(instr_path),
            "mcp_config_file": str(mcp_path),
            "binding_id": None,
        }

    def status(self) -> dict[str, Any]:
        _require_provider_state(self.workspace, mutation=False)
        instr_path = self._instruction_path()
        mcp_path = self._mcp_config_path()

        content = _read_text(instr_path)
        instruction_installed = (
            _BEGIN_MARKER in content and _END_MARKER in content
        )
        data = _load_json(mcp_path)
        mcp_configured = _has_mcp_server(data, MCP_SERVER_NAME)
        agent_id = _mcp_env_agent_id(data, self.provider_name, mcp_path.parent / "hooks.json")
        binding_id, binding_status = self._find_binding(agent_id)
        configured = instruction_installed and mcp_configured and bool(agent_id)
        return {
            "installed": configured,
            "configured": configured,
            "status": "configured" if configured else "not_configured",
            "restart_required": configured,
            "runtime_verified": False,
            "instruction_file": str(instr_path),
            "mcp_configured": mcp_configured,
            "configured_agent_instance_id": agent_id,
            "binding_id": binding_id,
            "binding_status": binding_status,
        }


PROVIDER_ADAPTERS: dict[str, type[ProviderAdapter]] = {
    "claude-code": ClaudeAdapter,
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "trae": TraeAdapter,
}


def get_provider_adapter_class(product: str) -> type[ProviderAdapter] | None:
    """按规范化 product ID 返回自动安装适配器。"""
    return PROVIDER_ADAPTERS.get((product or "").strip().lower())


def _provider_tokens_match(declared: str, provider: str) -> bool:
    left = str(declared or "").strip().casefold().replace("_", "-")
    right = str(provider or "").strip().casefold().replace("_", "-")
    if not left:
        return True
    groups = (
        frozenset({"claude", "claude-code"}),
        frozenset({"codex"}),
        frozenset({"cursor"}),
        frozenset({"trae", "trae-cn"}),
        frozenset({"grok", "xai-grok"}),
    )
    for group in groups:
        if right in group:
            return left in group
    return left == right


def _hook_agent_ids(path: Path, provider: str) -> set[str]:
    """Agent ids in one generated hook file. Unreadable files contribute nothing."""
    from .host_hooks import _generated_handler_binding, _load_json_config

    try:
        data = _load_json_config(path, strict=False)
    except Exception:
        return set()
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return set()
    found: set[str] = set()
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            handlers = [entry]
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                handlers.extend(entry["hooks"])
            for handler in handlers:
                binding = _generated_handler_binding(handler)
                if binding is None:
                    continue
                bound_provider, agent_id, _group_id, _workspace = binding
                if agent_id and _provider_tokens_match(bound_provider, provider):
                    found.add(agent_id)
    return found


def _provider_hook_paths() -> tuple[tuple[str, Path], ...]:
    home = Path.home()
    return (
        ("claude", home / ".claude" / "settings.json"),
        ("codex", home / ".codex" / "hooks.json"),
        ("cursor", home / ".cursor" / "hooks.json"),
        ("trae", home / ".trae" / "hooks.json"),
    )


def accepted_config_agent_id(
    env: Mapping[str, Any],
    provider: str,
    hook_agent_ids: set[str] | None = None,
) -> str:
    """Accept a config agent id only for its own provider and hook evidence.

    A copied id whose provider field or generated hook belongs to another
    product is not an identity. Missing provider/hook evidence stays legacy.
    """
    agent_id = str((env or {}).get("MEMORYGUARD_AGENT_ID") or "").strip()
    if not agent_id:
        return ""
    if not _provider_tokens_match(str((env or {}).get("MEMORYGUARD_PROVIDER") or ""), provider):
        return ""
    if hook_agent_ids and agent_id not in hook_agent_ids:
        return ""
    return agent_id


def foreign_hook_owner(agent_id: str, provider: str) -> str:
    """Return another provider whose generated hook already binds this id."""
    token = str(agent_id or "").strip()
    if not token:
        return ""
    for owner, path in _provider_hook_paths():
        if _provider_tokens_match(owner, provider):
            continue
        if token in _hook_agent_ids(path, owner):
            return owner
    return ""


def _mcp_env_agent_id(data: Mapping[str, Any], provider: str, hook_path: Path) -> str:
    env = (
        data.get("mcpServers", {})
        .get(MCP_SERVER_NAME, {})
        .get("env", {})
        or {}
    )
    if not isinstance(env, dict):
        return ""
    return accepted_config_agent_id(env, provider, _hook_agent_ids(hook_path, provider))


def configured_provider_agent_ids() -> dict[str, set[str]]:
    """Exact endpoint evidence from known user-level MCP/Hook configurations.

    This reads configuration metadata only, never native memory or a store.
    A generated own-provider Hook wins over a copied, inconsistent MCP id.
    Conflicting claims without that evidence remain unresolved.
    """
    home = Path.home()
    result: dict[str, set[str]] = {}
    for provider, config_path, hook_path in (
        ("claude-code", home / ".claude.json", home / ".claude" / "settings.json"),
        ("cursor", home / ".cursor" / "mcp.json", home / ".cursor" / "hooks.json"),
        ("trae", TraeAdapter._user_mcp_config_path(), home / ".trae" / "hooks.json"),
    ):
        hook_ids = _hook_agent_ids(hook_path, provider)
        data = _load_json(config_path)
        agent_id = _mcp_env_agent_id(data, provider, hook_path)
        result[provider] = hook_ids | ({agent_id} if agent_id else set())
    result["codex"] = set(_collect_codex_configured_agent_ids())
    # mcporter hosts the installed DeepSeek harness connection. Only explicit
    # provider metadata from MemoryGuard server entries identifies it.
    servers = _load_json(home / ".mcporter" / "mcporter.json").get("mcpServers", {})
    if isinstance(servers, dict):
        for name, server in servers.items():
            if not str(name).startswith("memoryguard") or not isinstance(server, dict):
                continue
            env = server.get("env")
            if not isinstance(env, dict) or env.get("MEMORYGUARD_PROVIDER") != "deepseek":
                continue
            agent_id = accepted_config_agent_id(env, "deepseek")
            if agent_id:
                result.setdefault("deepseek", set()).add(agent_id)
    owners: dict[str, set[str]] = {}
    for provider, ids in result.items():
        for agent_id in ids:
            owners.setdefault(agent_id, set()).add(provider)
    return {provider: {value for value in ids if len(owners[value]) == 1}
            for provider, ids in result.items()}


def resolve_provider_binding(control: Any, provider: str) -> dict[str, Any]:
    """Resolve the target product's existing binding, never the caller's id."""
    from .agent_mapping import normalize_program_identity

    product = normalize_program_identity(provider)["program_id"]
    identities = configured_provider_agent_ids()
    candidates = set(identities.get(product, ()))
    recorded = control.provider_identity(product)
    if recorded:
        candidates.add(str(recorded.get("canonical_id") or ""))
    bindings = {}
    for agent_id in candidates:
        if not agent_id or foreign_hook_owner(agent_id, provider):
            continue
        if any(agent_id in ids for owner, ids in identities.items() if owner != product):
            continue
        binding = control.active_binding_for_agent(agent_id)
        if binding:
            bindings[str(binding["binding_id"])] = binding
    if len(bindings) != 1:
        raise ValueError("target_provider_identity_unresolved" if not bindings else "target_provider_identity_ambiguous")
    return next(iter(bindings.values()))


def _read_codex_profile_agent_id(config_home: Path) -> str:
    """Read MEMORYGUARD_AGENT_ID from one Codex home; empty if absent/unreadable."""
    config_path = Path(config_home) / "config.toml"
    hook_ids = _hook_agent_ids(Path(config_home) / "hooks.json", "codex")
    try:
        text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
        data = tomllib.loads(text) if text.strip() else {}
    except Exception:
        ids = {
            accepted_config_agent_id(block, "codex", hook_ids)
            for block in _memoryguard_env_blocks(
                config_path.read_text(encoding="utf-8", errors="replace")
            )
        }
        ids.discard("")
        return next(iter(ids)) if len(ids) == 1 else ""
    env = ((data.get("mcp_servers") or {}).get(MCP_SERVER_NAME) or {}).get("env") or {}
    if not isinstance(env, dict):
        return ""
    return accepted_config_agent_id(env, "codex", hook_ids)


def _read_codex_profile_control_hints(config_home: Path) -> set[Path]:
    """Extract explicit control-home hints from MemoryGuard-owned MCP env."""
    config_path = Path(config_home) / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    found: set[Path] = set()
    for env in _memoryguard_env_blocks(text):
        for name in (
            "MEMORYGUARD_HOME",
            "MEMORYGUARD_WORKSPACE",
            "MEMORYGUARD_CONTROL_WORKSPACE",
        ):
            raw = str(env.get(name) or "").strip()
            if not raw:
                continue
            try:
                found.add(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
    return found


def _read_codex_profile_config_bindings(config_home: Path) -> set[tuple[str, str]]:
    """Extract legacy MCP identity only as installation evidence."""
    config_path = Path(config_home) / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    found: set[tuple[str, str]] = set()
    hook_ids = _hook_agent_ids(Path(config_home) / "hooks.json", "codex")
    for env in _memoryguard_env_blocks(text):
        agent_id = accepted_config_agent_id(env, "codex", hook_ids)
        group_id = str(
            env.get("MEMORYGUARD_SHARE_GROUP_ID")
            or env.get("MEMORYGUARD_GROUP_ID")
            or ""
        ).strip()
        if agent_id and group_id:
            found.add((agent_id, group_id))
    return found


def _iter_codex_homes(*, include_default_router: bool = False) -> list[Path]:
    """Current CODEX_HOME first, then other discovered Router/user Codex roots."""
    from .agent_locator import current_codex_home, discover_codex_homes

    homes = list(discover_codex_homes(include_default_router=include_default_router))
    current = current_codex_home()
    if current not in homes:
        homes.insert(0, current)
    return homes


def _is_router_codex_home(home: Path) -> bool:
    return (
        home.name.casefold() == "codex-home"
        and home.parent.parent.name.casefold() == "profiles"
    )


def _codex_repair_homes() -> list[Path]:
    """Repair Router account profiles as one program identity when present."""
    from .agent_locator import current_codex_home

    homes = _iter_codex_homes()
    router_homes = [home for home in homes if _is_router_codex_home(home)]
    router_env = any(
        str(os.environ.get(name, "") or "").strip()
        for name in (
            "CODEXROUTER_DATA", "CODEX_ROUTER_DATA",
            "CODEXROUTER_HOME", "CODEX_ROUTER_HOME",
        )
    )
    if router_homes and (_is_router_codex_home(current_codex_home()) or router_env):
        return router_homes
    non_router_homes = [home for home in homes if not _is_router_codex_home(home)]
    return non_router_homes or homes


def _collect_codex_configured_agent_ids() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for home in _iter_codex_homes():
        agent_id = _read_codex_profile_agent_id(home)
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        found.append(agent_id)
    return found


def _read_codex_profile_hook_bindings(config_home: Path) -> list[tuple[str, str]]:
    """Read only MemoryGuard-owned Codex hook identities from one profile."""
    from .host_hooks import _generated_handler_binding, _load_json_config

    try:
        data = _load_json_config(Path(config_home) / "hooks.json", strict=False)
    except Exception:
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return []
    found: set[tuple[str, str]] = set()
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            handlers = [entry]
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                handlers.extend(entry["hooks"])
            for handler in handlers:
                binding = _generated_handler_binding(handler)
                if binding is None:
                    continue
                provider, agent_id, group_id, _workspace = binding
                if provider == "codex" and agent_id and group_id:
                    found.add((agent_id, group_id))
    return sorted(found)


def _read_codex_profile_hook_evidence(
    config_home: Path,
) -> set[tuple[str, str, Path]]:
    """Return generated Codex hook bindings with their pinned control home."""
    from .host_hooks import _generated_handler_binding, _load_json_config

    try:
        data = _load_json_config(Path(config_home) / "hooks.json", strict=False)
    except Exception:
        return set()
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return set()
    found: set[tuple[str, str, Path]] = set()
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            handlers = [entry]
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                handlers.extend(entry["hooks"])
            for handler in handlers:
                binding = _generated_handler_binding(handler)
                if binding is None:
                    continue
                provider, agent_id, group_id, workspace = binding
                if provider != "codex" or not agent_id or not group_id or not workspace:
                    continue
                try:
                    found.add((agent_id, group_id, Path(workspace).expanduser().resolve()))
                except (OSError, RuntimeError, ValueError):
                    continue
    return found


def _collect_codex_profile_hook_bindings() -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for home in _iter_codex_homes():
        found.update(_read_codex_profile_hook_bindings(home))
    return sorted(found)


def _verified_v2_control_state(path: Path) -> str:
    """Return a usable V2 manifest state without opening legacy data planes."""
    from .system.manifest import ManifestManager

    try:
        current = ManifestManager(path).current()
        state = current.get("state", current.get("status", "")) if isinstance(current, dict) else current.state
    except Exception:
        return ""
    marker = str(getattr(state, "value", state) or "").strip().upper()
    return marker if marker in {"V2_READY", "V2_ACTIVE"} else ""


def _has_active_control_binding(path: Path) -> bool:
    """Return whether a verified control home still owns a live binding.

    A V2 manifest alone is not provider-installation evidence: a newly
    created default directory can be V2-active while containing no agent or
    group.  Treating that empty root as authoritative is what lets Router
    account profiles split MCP and Hook state across two control planes.
    """
    try:
        from .runtime_v2.group_native import GroupControlService

        bindings = GroupControlService(path, write=False).list_bindings(
            include_inactive=False,
        ).get("bindings") or []
    except Exception:
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("agent_instance_id") or "").strip()
        and str(item.get("share_group_id") or "").strip()
        for item in bindings
    )


def _absolute_control_home(raw: object) -> Path | None:
    """Normalize one explicit provider control-home value, never a relative hint."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            return None
        return candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _read_codex_profile_mcp_control_evidence(
    config_home: Path,
) -> set[tuple[str, Path]]:
    """Read one valid MemoryGuard-owned Codex MCP config as install evidence.

    ``MEMORYGUARD_HOME`` is the only accepted control pointer.  Legacy
    ``MEMORYGUARD_WORKSPACE`` can describe a project, so it must not redirect a
    bare program control command.  A profile path or account alone is never
    an identity: each candidate retains its configured agent id for active
    binding verification below.
    """
    config_path = Path(config_home) / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if text.strip() else {}
    except Exception:
        return set()
    server = ((data.get("mcp_servers") or {}).get(MCP_SERVER_NAME) or {})
    if not isinstance(server, dict):
        return set()
    if MCP_MODULE not in " ".join(str(item) for item in server.get("args") or ()):
        return set()
    env = server.get("env") or {}
    if not isinstance(env, dict):
        return set()
    agent_id = accepted_config_agent_id(
        env, "codex", _hook_agent_ids(Path(config_home) / "hooks.json", "codex"),
    )
    candidate = _absolute_control_home(env.get("MEMORYGUARD_HOME"))
    return {(agent_id, candidate)} if agent_id and candidate is not None else set()


def _has_verified_codex_binding(
    control_home: Path,
    agent_id: str,
    expected_group_id: str = "",
) -> bool:
    """Accept installed-profile evidence only with its still-active binding."""
    if not _verified_v2_control_state(control_home):
        return False
    try:
        from .runtime_v2.group_native import GroupControlService

        binding = GroupControlService(control_home, write=False).active_binding_for_agent(
            agent_id,
        )
    except Exception:
        return False
    if binding is None:
        return False
    group_id = str(binding.get("share_group_id") or "").strip()
    return bool(group_id and (not expected_group_id or group_id == expected_group_id))


def discover_verified_codex_control_homes() -> tuple[Path, ...]:
    """Return verified global Codex control homes from installed provider state.

    Discovery is read-only and bounded to known Codex profile locations.  A
    candidate needs all of: an absolute MemoryGuard-owned MCP/home pointer (or
    generated hook pointer), a READY/ACTIVE V2 manifest, and a matching active
    agent binding.  Multiple profiles that verify one path collapse to one
    candidate; different paths stay distinct so callers can fail closed.
    """
    candidates: set[Path] = set()
    for profile_home in _iter_codex_homes(include_default_router=True):
        for agent_id, control_home in _read_codex_profile_mcp_control_evidence(
            profile_home,
        ):
            if _has_verified_codex_binding(control_home, agent_id):
                candidates.add(control_home)
        for agent_id, group_id, control_home in _read_codex_profile_hook_evidence(
            profile_home,
        ):
            if _has_verified_codex_binding(control_home, agent_id, group_id):
                candidates.add(control_home)
    return tuple(sorted(candidates, key=lambda path: str(path).casefold()))


def _select_verified_codex_control_home() -> Path:
    """Select exactly one V2 control home evidenced by existing Codex setup.

    A V1/default home is never repaired merely because it is the process
    default.  Old managed MCP env and generated hooks are candidates only once
    their target has an independently verified V2 manifest.  More than one
    verified target is an operator decision, not something repair may guess.
    """
    from .data_home import resolve_data_home

    candidates: dict[Path, set[str]] = {}

    def offer(raw: Path, source: str, *, allow_pending: bool = False) -> None:
        try:
            candidate = raw.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return
        state = _verified_v2_control_state(candidate)
        if state and (allow_pending or _has_active_control_binding(candidate)):
            candidates.setdefault(candidate, set()).add(f"{source}:{state}")

    default_home = resolve_data_home()
    # A V2 marker without an active binding is only an empty bootstrap root;
    # it must not outrank a Router profile that still points at the live
    # canonical plane.  If the default is itself active, include it in the
    # candidate set so multiple valid control homes still fail closed.
    offer(default_home, "default")

    for home in _iter_codex_homes():
        for candidate in _read_codex_profile_control_hints(home):
            offer(candidate, f"mcp:{home}")
        for _agent_id, _group_id, candidate in _read_codex_profile_hook_evidence(home):
            # A generated hook carries the exact agent/group pair needed
            # by the narrow bootstrap path below.  Permit that pending
            # candidate even before its active binding is recreated; a
            # plain MCP/home hint still requires an existing binding.
            offer(candidate, f"hook:{home}", allow_pending=True)

    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        raise ValueError("verified_v2_control_home_not_found")
    detail = ", ".join(sorted(str(path) for path in candidates))
    raise ValueError(f"verified_v2_control_home_ambiguous: {detail}")


def _bootstrap_codex_binding_from_verified_installation(
    binding_store: Any,
    data_home: Path,
) -> bool:
    """Bind one unambiguous prior Codex installation, then re-verify it.

    This recovery path is intentionally narrow: no request payload is read,
    no V1 store is inspected, and no identity is minted from Router paths.
    It accepts only one exact generated-hook/config/provider-record identity
    for the already selected V2 control home, and only when no active binding
    exists yet.
    """
    try:
        active = binding_store.list_bindings(include_inactive=False).get("bindings") or []
    except Exception:
        return False
    if active:
        return False

    candidates: set[tuple[str, str]] = set()
    recorded = binding_store.provider_identity("codex")
    if recorded:
        agent_id = str(recorded.get("canonical_id") or "").strip()
        group_id = str(recorded.get("share_group_id") or "").strip()
        if agent_id and group_id:
            candidates.add((agent_id, group_id))
    for home in _iter_codex_homes():
        candidates.update(_read_codex_profile_config_bindings(home))
        candidates.update(
            (agent_id, group_id)
            for agent_id, group_id, workspace in _read_codex_profile_hook_evidence(home)
            if workspace == data_home
        )
    if len(candidates) != 1:
        return False
    agent_id, group_id = next(iter(candidates))
    binding_store.bind_agent(
        agent_id,
        group_id,
        idempotency_key=f"codex-repair-bootstrap:{agent_id}:{group_id}",
    )
    return binding_store.active_binding_for_agent(agent_id) is not None


def _record_codex_program_identity(
    data_home: str | Path,
    *,
    agent_instance_id: str,
    share_group_id: str,
    aliases: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Persist Codex program identity; prior profile IDs stay aliases."""
    from .runtime_v2.group_native import GroupControlService

    return GroupControlService(data_home, write=True).record_provider_identity(
        "codex",
        str(agent_instance_id or "").strip(),
        str(share_group_id or "").strip(),
        aliases,
    )


def _codex_hooks_document(
    workspace: Path,
    config_home: Path,
    agent_instance_id: str,
    share_group_id: str,
    runtime_python: str | Path | None = None,
) -> str:
    from .host_hooks import CodexHookAdapter, _load_json_config

    adapter = CodexHookAdapter(workspace)
    adapter.set_config_home(config_home)
    data = _load_json_config(adapter.config_path(), strict=False)
    data = adapter._remove_owned(data)
    data = adapter._add_owned(
        data,
        agent_instance_id,
        share_group_id,
        runtime_python=runtime_python,
    )
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _write_codex_global_home(
    data_home: Path,
    config_home: Path,
    *,
    agent_instance_id: str,
    share_group_id: str,
    runtime_python: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically write MCP env, AGENTS.md, and hooks for one Codex home."""
    from .host_hooks import set_hook_mode

    selected_runtime_python = str(
        runtime_python or _configured_codex_runtime_python(config_home) or sys.executable
    )
    adapter = CodexAdapter(data_home, config_home=config_home)
    adapter._has_workspace = False
    adapter.workspace = Path(data_home).expanduser().resolve()
    instr_path = adapter._instruction_path()
    content = _read_text_for_update(instr_path)
    new_content = _replace_section(
        content, _BEGIN_MARKER, _END_MARKER, _instruction_body(share_group_id),
    )
    mcp_path = adapter._mcp_config_path()
    toml_content = _read_text_for_update(mcp_path)
    section = _mcp_toml_section(
        agent_instance_id, adapter.workspace, control_scope="global",
        runtime_python=selected_runtime_python,
    )
    new_toml = _upsert_memoryguard_toml(toml_content, section)
    _validate_toml(new_toml, mcp_path)
    backup = _backup_toml_before_repair(mcp_path, toml_content, new_toml)
    hooks_path = Path(config_home) / "hooks.json"
    hooks_text = _codex_hooks_document(
        adapter.workspace,
        Path(config_home),
        agent_instance_id,
        share_group_id,
        runtime_python=selected_runtime_python,
    )
    _apply_file_transaction([
        (instr_path, new_content),
        (mcp_path, new_toml),
        (hooks_path, hooks_text),
    ])
    set_hook_mode(adapter.workspace, "codex", agent_instance_id, "enforce")
    return {
        "config_home": str(Path(config_home)),
        "instruction_file": str(instr_path),
        "mcp_config_file": str(mcp_path),
        "hooks_file": str(hooks_path),
        "config_backup": str(backup) if backup else "",
        "agent_instance_id": agent_instance_id,
        "share_group_id": share_group_id,
    }


def _repair_discovered_codex_homes(
    data_home: str | Path,
    *,
    agent_instance_id: str,
    share_group_id: str,
    binding_id: str = "",
    skip_config_home: Path | None = None,
    homes: list[Path] | tuple[Path, ...] | None = None,
    runtime_python: str | Path | None = None,
) -> dict[str, Any]:
    """Repair every discovered Codex home to the same program identity."""
    from .agent_locator import current_codex_home

    homes = list(homes) if homes is not None else _iter_codex_homes()
    current = current_codex_home()
    written: list[dict[str, Any]] = []
    warnings: list[str] = []
    aliases: list[str] = []
    for home in homes:
        if skip_config_home is not None and home == skip_config_home:
            continue
        previous = _read_codex_profile_agent_id(home)
        if previous and previous != agent_instance_id:
            aliases.append(previous)
        for previous_agent_id, _previous_group_id in _read_codex_profile_hook_bindings(home):
            if previous_agent_id != agent_instance_id:
                aliases.append(previous_agent_id)
        try:
            written.append(_write_codex_global_home(
                Path(data_home),
                home,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                runtime_python=runtime_python,
            ))
        except Exception as exc:
            warnings.append(
                f"{home}: {type(exc).__name__}: {exc}"
            )
    return {
        "homes": written,
        "current_codex_home": str(current),
        "aliases": sorted(set(aliases)),
        "binding_id": binding_id,
        "warnings": warnings,
    }


def _resolve_codex_canonical_identity(
    binding_store: Any,
    instances: list[Any],
) -> tuple[str, str, list[str]]:
    """Reuse the installed Codex principal; never mint a profile-path identity."""
    configured = _collect_codex_configured_agent_ids()
    hook_bindings = _collect_codex_profile_hook_bindings()
    recorded = binding_store.provider_identity("codex")
    if recorded:
        binding = binding_store.active_binding_for_agent(recorded["canonical_id"])
        if binding is not None:
            canonical = recorded["canonical_id"]
            aliases = [
                item for item in {
                    *list(recorded.get("aliases") or ()),
                    *configured,
                }
                if item and item != canonical
            ]
            return (
                canonical,
                str(binding.get("share_group_id") or recorded.get("share_group_id") or ""),
                aliases,
            )

    matches: list[Any] = []
    for item in instances:
        cls = get_provider_adapter_class(getattr(item, "product", ""))
        if cls is not None and cls.provider_name == "codex":
            matches.append(item)

    aliases = {
        *configured,
        *(agent_id for agent_id, _group_id in hook_bindings),
    }
    candidates: set[tuple[str, str]] = set()

    def add_active_candidate(agent_id: str, expected_group_id: str = "") -> None:
        binding = binding_store.active_binding_for_agent(agent_id)
        if binding is None:
            return
        group_id = str(binding.get("share_group_id") or "")
        if expected_group_id and group_id != expected_group_id:
            return
        if group_id:
            candidates.add((str(binding.get("agent_instance_id") or agent_id), group_id))

    # A prior generated hook is installation evidence, not request data.  Its
    # identity is usable only when it still matches an active canonical binding.
    for agent_id, group_id in hook_bindings:
        add_active_candidate(agent_id, group_id)
    for agent_id in configured:
        add_active_candidate(agent_id)
    for instance in matches:
        add_active_candidate(instance.instance_id)

    if len(candidates) == 1:
        canonical, group_id = next(iter(candidates))
        return canonical, group_id, sorted(aliases - {canonical})
    return "", "", sorted(aliases)


def repair_global_provider_configs(
    providers: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Rebuild global provider integrations from canonical data-home bindings.

    This is the upgrade/repair entry point.  It never trusts a provider's
    existing AgentInstance id, share-group id, or MEMORYGUARD_WORKSPACE.  The
    current instances are rediscovered, then each provider is installed from
    the one active binding stored in the canonical user data home.
    """
    from .agent_locator import AgentLocator, current_codex_home

    data_home = _select_verified_codex_control_home()
    _require_provider_state(data_home, mutation=True)
    data_home.mkdir(parents=True, exist_ok=True)
    instances, _ = AgentLocator(data_home).detect_instances()
    from .runtime_v2.group_native import GroupControlService

    binding_store: Any = GroupControlService(data_home, write=True)

    requested: set[str] = set()
    for raw in providers or ():
        value = str(raw or "").strip().lower()
        if value in {"", "all", "*"}:
            continue
        cls = get_provider_adapter_class(value)
        if cls is None:
            raise ValueError(f"unsupported provider: {value}")
        requested.add(cls.provider_name)
    if not requested:
        requested = {"claude", "codex", "cursor", "trae"}

    by_provider: dict[str, list[Any]] = {}
    for instance in instances:
        cls = get_provider_adapter_class(instance.product)
        if cls is None or cls.provider_name not in requested:
            continue
        by_provider.setdefault(cls.provider_name, []).append(instance)

    repaired: list[dict[str, Any]] = []
    for provider in sorted(requested):
        if provider == "codex":
            repair_homes = _codex_repair_homes()
            agent_id, group_id, alias_ids = _resolve_codex_canonical_identity(
                binding_store, instances,
            )
            if not agent_id and _bootstrap_codex_binding_from_verified_installation(
                binding_store, data_home,
            ):
                agent_id, group_id, alias_ids = _resolve_codex_canonical_identity(
                    binding_store, instances,
                )
            matches = by_provider.get(provider, [])
            if not agent_id and len(matches) > 1:
                repaired.append({
                    "provider": provider,
                    "status": "error",
                    "reason": "multiple_provider_instances_detected",
                    "agent_instance_ids": sorted(item.instance_id for item in matches),
                })
                continue
            if not agent_id and not matches and not repair_homes:
                repaired.append({
                    "provider": provider,
                    "status": "skipped",
                    "reason": "provider_instance_not_detected",
                })
                continue
            if not agent_id and matches:
                agent_id = matches[0].instance_id
            binding = (
                binding_store.active_binding_for_agent(agent_id) if agent_id else None
            )
            binding_data = dict(binding) if isinstance(binding, dict) else None
            if binding_data is None:
                repaired.append({
                    "provider": provider,
                    "status": "error",
                    "reason": "active_binding_not_found",
                    "agent_instance_id": agent_id,
                })
                continue
            group_id = str(binding_data.get("share_group_id") or group_id or "")
            try:
                adapter = CodexAdapter(data_home, config_home=repair_homes[0] if repair_homes else "")
                # Selection above verified this V2 home from existing managed
                # Codex evidence.  Keep the generic installer from falling
                # back to an unrelated default (often a V1 legacy home).
                adapter._repair_data_home = data_home
                adapter._repair_codex_homes = tuple(repair_homes)
                result = adapter.install(
                    data_home,
                    share_group_id=group_id,
                    agent_instance_id=agent_id,
                    global_scope=True,
                )
                merged_aliases = [
                    item for item in {
                        *alias_ids,
                        *list(result.get("aliases") or ()),
                    }
                    if item and item != agent_id
                ]
                recorded = _record_codex_program_identity(
                    data_home,
                    agent_instance_id=agent_id,
                    share_group_id=group_id,
                    aliases=merged_aliases,
                )
                profile_errors = list(result.get("profile_repair_errors") or ())
                repaired.append({
                    "provider": provider,
                    "status": "partial" if profile_errors else "configured",
                    "agent_instance_id": agent_id,
                    "share_group_id": group_id,
                    "display_name": "Codex",
                    "current_codex_home": str(current_codex_home()),
                    "aliases": list(recorded.get("aliases") or merged_aliases),
                    "result": result,
                    **(
                        {"reason": "codex_profile_repair_failed", "profile_errors": profile_errors}
                        if profile_errors else {}
                    ),
                })
            except Exception as exc:
                repaired.append({
                    "provider": provider,
                    "status": "error",
                    "agent_instance_id": agent_id,
                    "share_group_id": group_id,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
            continue
        matches = by_provider.get(provider, [])
        if not matches:
            repaired.append({
                "provider": provider,
                "status": "skipped",
                "reason": "provider_instance_not_detected",
            })
            continue
        if len(matches) != 1:
            repaired.append({
                "provider": provider,
                "status": "error",
                "reason": "multiple_provider_instances_detected",
                "agent_instance_ids": sorted(item.instance_id for item in matches),
            })
            continue
        instance = matches[0]
        binding = binding_store.active_binding_for_agent(instance.instance_id)
        binding_data = dict(binding) if isinstance(binding, dict) else None
        if binding_data is None:
            repaired.append({
                "provider": provider,
                "status": "error",
                "reason": "active_binding_not_found",
                "agent_instance_id": instance.instance_id,
            })
            continue
        cls = get_provider_adapter_class(instance.product)
        if cls is None:  # guarded above; keep the write path explicit
            continue
        try:
            group_id = str(binding_data.get("share_group_id") or "")
            agent_id = str(binding_data.get("agent_instance_id") or instance.instance_id)
            result = cls(data_home).install(
                data_home,
                share_group_id=group_id,
                agent_instance_id=agent_id,
                global_scope=True,
            )
            from .agent_mapping import normalize_program_identity
            binding_store.record_provider_identity(
                normalize_program_identity(provider)["program_id"], agent_id, group_id,
            )
            repaired.append({
                "provider": provider,
                "status": "configured",
                "agent_instance_id": agent_id,
                "share_group_id": group_id,
                "result": result,
            })
        except Exception as exc:
            repaired.append({
                "provider": provider,
                "status": "error",
                "agent_instance_id": instance.instance_id,
                "share_group_id": str(binding_data.get("share_group_id") or ""),
                "reason": f"{type(exc).__name__}: {exc}",
            })

    configured = sum(item["status"] == "configured" for item in repaired)
    errors = sum(item["status"] == "error" for item in repaired)
    partial = sum(item["status"] == "partial" for item in repaired)
    skipped = sum(item["status"] == "skipped" for item in repaired)
    return {
        "ok": errors == 0 and partial == 0,
        "data_home": str(data_home),
        "configured": configured,
        "errors": errors,
        "partial": partial,
        "skipped": skipped,
        "providers": repaired,
        "restart_required": configured > 0 or partial > 0,
    }


def _main(argv: list[str] | None = None) -> int:
    """Small maintenance CLI used after upgrades and control-plane migration."""
    import argparse

    parser = argparse.ArgumentParser(description="Repair MemoryGuard provider integrations")
    sub = parser.add_subparsers(dest="command", required=True)
    repair = sub.add_parser(
        "repair", help="rebuild global provider configs from canonical bindings",
    )
    repair.add_argument(
        "providers", nargs="*", default=["all"],
        help="claude codex cursor trae, or all",
    )
    opts = parser.parse_args(argv)
    if opts.command == "repair":
        try:
            result = repair_global_provider_configs(opts.providers)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
