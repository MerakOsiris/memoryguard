"""data_home：知识书库统一存储目录解析。

知识书库数据库落 data_home（默认 MEMORYGUARD_HOME 或用户数据目录）下的
knowledge/knowledge.db。工作区（MEMORYGUARD_WORKSPACE）不是数据目录，
知识书库不落到任何目标项目目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# 环境变量名
_ENV_DATA_HOME = "MEMORYGUARD_HOME"

# These are project-local control artifacts from the pre-V2 layout.  The
# runtime only uses them as a negative safety check: it never opens, parses,
# or imports their contents.  Migration code may still inspect the exact
# paths as a source after the control plane has been selected.
_PROJECT_CONTROL_ARTIFACTS = frozenset(
    {
        "agent-bindings",
        "agent_bindings",
        "config.json",
        "config.local.json",
        # governance.lock is also the V2 cross-process synchronization file.
        # Treating its presence as a legacy-project marker makes a freshly
        # configured Data Home fall back elsewhere immediately after its first
        # mutation, splitting the control plane across two roots.
        "managed-memory",
        "managed_memory",
        "manifest.json",
        "shared-memory",
        "shared_memory",
    }
)
_V2_MANIFEST = Path(".memoryguard") / "system" / "manifest.db"


class ControlHomeResolutionError(ValueError):
    """Bare control commands found conflicting verified provider control homes."""

    code = "stable_control_home_ambiguous"

    def __init__(self, candidates: tuple[Path, ...]) -> None:
        self.candidates = tuple(candidates)
        detail = ", ".join(str(path) for path in self.candidates)
        super().__init__(
            "multiple verified MemoryGuard control homes; pass --data-home explicitly"
            + (f": {detail}" if detail else "")
        )

    def to_payload(self, *, surface: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "blocked",
            "code": self.code,
            "error": self.code,
            "surface": surface,
            "message": str(self),
            "candidates": [str(path) for path in self.candidates],
        }


def _user_data_dir() -> Path:
    """跨平台用户数据目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "MemoryGuard"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MemoryGuard"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "MemoryGuard"


def _raw_data_home(explicit: str | Path | None = None) -> Path:
    """Resolve the configured path without applying runtime safety checks."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).expanduser().resolve()
    env_home = os.environ.get(_ENV_DATA_HOME, "").strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return _user_data_dir().resolve()


def is_v2_data_home(path: str | Path) -> bool:
    """Return whether *path* has the V2 control manifest marker."""
    root = Path(path).expanduser().resolve()
    return (root / _V2_MANIFEST).is_file()


def _has_project_control_artifacts(path: str | Path) -> bool:
    """Detect a known project-local control tree without reading its data."""
    control_root = Path(path).expanduser().resolve() / ".memoryguard"
    if not control_root.is_dir():
        return False
    return any((control_root / name).exists() for name in _PROJECT_CONTROL_ARTIFACTS)


def _safe_default_data_home() -> Path:
    """Return the configured V2 control root, avoiding a project tree."""
    configured = _raw_data_home()
    if is_v2_data_home(configured) or not _has_project_control_artifacts(configured):
        return configured

    fallback = _user_data_dir().resolve()
    # A configured path that looks like a legacy project is never a runtime
    # control root.  The user-level fallback remains the only safe destination
    # even when an older installation left V1 artifacts there as well; runtime
    # callers may create/use its V2 layout, while migration code is the only
    # caller allowed to inspect those legacy paths.
    if fallback != configured:
        return fallback
    return configured


def resolve_data_home(explicit: str | Path | None = None) -> Path:
    """解析统一存储目录。

    优先级：显式参数 > MEMORYGUARD_HOME 环境变量 > 用户数据目录。
    工作区（MEMORYGUARD_WORKSPACE）不是数据目录，知识书库不落到工作区。
    """
    if explicit is not None and str(explicit).strip():
        candidate = Path(explicit).expanduser().resolve()
        if is_v2_data_home(candidate) or not _has_project_control_artifacts(candidate):
            return candidate
        return _safe_default_data_home()
    return _safe_default_data_home()


def resolve_deployment_home() -> Path:
    """Keep installed runtimes and scratch space with the MG deployment."""
    package = Path(__file__).resolve().parent
    for parent in package.parents:
        if parent.name == ".memoryguard":
            return parent.parent
    if package.parent.name == "src":
        return package.parent.parent
    from .provider_adapters import _live_source_root

    source = _live_source_root()
    if source is not None and source.is_dir():
        return source.resolve()
    if sys.prefix != sys.base_prefix:
        return Path(sys.prefix).resolve().parent
    return package.parent


def resolve_cache_home(workspace: str | Path | None = None) -> Path:
    """Project scratch follows the project; global scratch follows MG."""
    owner = Path(workspace).expanduser().resolve() if workspace else None
    if owner is None or owner == resolve_runtime_data_home():
        owner = resolve_deployment_home()
    return owner / ".memoryguard" / "cache"


def resolve_runtime_data_home(
    explicit: str | Path | None = None,
    *,
    discover_stable_control_home: bool = False,
) -> Path:
    """Resolve the sole V2 data plane used by production runtime surfaces.

    ``MEMORYGUARD_WORKSPACE`` and the current directory are intentionally not
    consulted here.  An explicit project-local legacy tree is rejected and
    falls back to the configured user data home; an explicit V2/isolated root
    remains available for tests and operator-managed V2 installations.  Bare
    program-level CLI controls may additionally opt into a narrowly verified
    provider lookup when their default global home is empty or unbound.
    """
    resolved = resolve_data_home(explicit)
    explicit_or_env = bool(
        (explicit is not None and str(explicit).strip())
        or os.environ.get(_ENV_DATA_HOME, "").strip()
    )
    if not discover_stable_control_home or explicit_or_env:
        return resolved

    # Provider configuration is evidence only for bare program controls.  It
    # never affects MCP/runtime data-plane selection and never examines cwd.
    from .provider_adapters import (
        _has_active_control_binding,
        discover_verified_codex_control_homes,
    )

    candidates = set(discover_verified_codex_control_homes())
    # A V2 default is authoritative only when it still has an active binding.
    # Include that live home in the same candidate set so a second verified
    # provider home fails closed instead of silently splitting the control
    # plane.  An empty V2 default remains recoverable to the unique provider
    # home discovered above.
    if is_v2_data_home(resolved) and _has_active_control_binding(resolved):
        candidates.add(resolved)
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        raise ControlHomeResolutionError(
            tuple(sorted(candidates, key=lambda path: str(path).casefold()))
        )
    return resolved


def knowledge_db_path(data_home: Path | None = None) -> Path:
    """知识书库数据库路径。"""
    base = data_home or resolve_data_home()
    return base / "knowledge" / "knowledge.db"


def ensure_dirs(data_home: Path | None = None) -> Path:
    """确保 data_home 知识目录存在，返回 data_home。"""
    base = data_home or resolve_data_home()
    (base / "knowledge").mkdir(parents=True, exist_ok=True)
    return base
