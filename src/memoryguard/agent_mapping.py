"""v3.2 §3.2 补充：用户 HOME 目录下 .config 目录名 -> Agent 产品名映射表。

数据文件，不执行脚本。用于 AgentLocator.discover_candidates() 自动识别本机已安装的 Agent。
映射方向：~/.<dot_dir_name> -> <product_name>。

添加新产品只需在 AGENT_PRODUCT_MAP 中加入一行，无需改扫描逻辑。
未知目录显示为 product="unknown"，可在 GUI 手动添加 Profile。

安全边界：
- 只读 %HOME% 下一级子目录的 Name/FullName，不读正文
- 不做递归搜索（stale 检测最多 2 层 stat）
- 不做系统进程探测

stale 检测阈值：
- 30 天未修改 -> stale
- 60 天未修改 -> likely_uninstalled
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Mapping

# stale 判定阈值（秒）
STALE_THRESHOLD_SECONDS = 30 * 24 * 3600       # 30 天
LIKELY_UNINSTALLED_THRESHOLD_SECONDS = 60 * 24 * 3600  # 60 天

# ---------------------------------------------------------------------------
# AGENT_PRODUCT_MAP：点目录名 -> 产品名
# ---------------------------------------------------------------------------

AGENT_PRODUCT_MAP: dict[str, str] = {
    # ---------- 海外 Agent（已有完整 Profile）----------
    ".claude": "claude-code",
    ".codex": "codex",
    ".cursor": "cursor",
    ".codeium": "windsurf",
    ".grok": "grok",              # xAI Grok CLI / MCP client

    # ---------- 国产 Agent（独立 IDE / CLI）----------
    ".trae-cn": "trae",       # 字节跳动 TRAE（含 MarsCode 继承），独立 IDE
    ".trae": "trae",
    ".zcode": "zcode",        # zcode CLI Agent
    ".lingma": "lingma",      # 阿里通义灵码 IDE
    ".comate": "comate",      # 百度文心快码 IDE
    ".qoder": "qoder",        # Qoder AI IDE（项目级 .qoder/rules + .qoder/skills）

    # ---------- 国产 Agent（桌面 AI 助手）----------
    ".workbuddy": "workbuddy",  # 腾讯 WorkBuddy 桌面 AI 助手

    # ---------- 开源 / 套壳 Agent ----------
    ".openclaw": "openclaw",    # OpenClaw AI 智能体（含 MEMORY.md/SOUL.md/AGENTS.md）
    ".opencode": "opencode",    # OpenCode CLI coding agent（~/.config/opencode/）

    # ---------- 国产 Agent（插件型，数据在 VSCode/JetBrains 扩展目录）----------
    # CodeGeeX / CodeBuddy 作为插件时数据在宿主 IDE 扩展目录，无法用 ~/. 映射
    # 用户需手动添加 SourceRoot 指向扩展目录

    # ---------- 其他可探测目录 ----------
    ".echobird": "echobird",  # 本地模型管理器（非 coding agent）
    ".gumiho": "gumifox",
}

# APPDATA 下的 Agent 目录（Windows 专用，非 ~/. 目录）
APPDATA_AGENT_MAP: dict[str, str] = {
    "CodeBuddy": "codebuddy",  # 腾讯 CodeBuddy
}

# VSCode 扩展目录中的 Agent 插件名前缀
VSCODE_EXTENSION_AGENT_MAP: dict[str, str] = {
    "aminer.codegeex": "codegeex",      # 智谱 CodeGeeX
    "codebuddy": "codebuddy",            # 腾讯 CodeBuddy VSCode 插件
    "lingma": "lingma",                  # 通义灵码 VSCode 插件
    "comate": "comate",                  # 文心快码 VSCode 插件
}

# 从映射表中提取的已知产品名集合（不含 unknown）
KNOWN_PRODUCTS: set[str] = set(AGENT_PRODUCT_MAP.values())

# 需要跳过的非 Agent 目录名（精确匹配）
IGNORED_DIRS: set[str] = {
    ".config",
    ".vscode",
    ".vscode-server",
    ".vscode-shared",
    ".cache",
    ".android",
    ".chocolatey",
    ".dotnet",
    ".lldb",
    ".local",
    ".bun",
    ".ohos",
    ".ohpm",
    ".hvigor",
    ".djl.ai",
    ".BigNox",
    ".agents",
    ".agent-reach",
    ".codegraph",
    ".qmf",
    ".sbx-denybin",
    ".Icecream PDF Split and Merge",
    ".cc-switch",
    ".cc-weixin",
    ".harmony",
    ".ollama",
    ".headroom",
    ".tuval",
}


PRODUCT_DISPLAY_NAMES: dict[str, str] = {
    "claude-code": "Claude Code",
    "claude": "Claude",
    "codex": "Codex",
    "cursor": "Cursor",
    "grok": "Grok",
    "deepseek": "DeepSeek Harness",
    "trae": "Trae",
    "windsurf": "Windsurf",
    "zcode": "zcode",
    "lingma": "Lingma",
    "comate": "Comate",
    "qoder": "Qoder",
    "workbuddy": "WorkBuddy",
    "openclaw": "OpenClaw",
    "opencode": "OpenCode",
    "codebuddy": "CodeBuddy",
    "codegeex": "CodeGeeX",
    "echobird": "EchoBird",
    "gumifox": "GumiFox",
}

# Stable program identity aliases.  Only exact, trusted tokens are accepted;
# arbitrary account names and model names never become a program identity.
PROGRAM_ID_ALIASES: dict[str, str] = {
    **{name: name for name in PRODUCT_DISPLAY_NAMES},
    "grok-cli": "grok",
    "xai-grok": "grok",
    "claude-code": "claude-code",
    "claude": "claude-code",
    "dsh": "deepseek",
    "cursor-agent": "cursor",
    "codeium": "windsurf",
}


def _identity_token(value: Any) -> str:
    """Normalize one trusted identity field to an exact alias token."""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    # CLI fields may be absolute paths.  Never inspect arguments or contents.
    token = text.rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if token.endswith(suffix):
            token = token[: -len(suffix)]
            break
    return token.replace("_", "-").replace(" ", "-")


def normalize_program_identity(
    provider: str = "",
    *,
    cli_path: str = "",
    mcp_name: str = "",
    mcp_display_name: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Resolve a stable program identity from trusted discovery evidence.

    Resolution order is explicit provider, CLI basename, MCP server name, and
    MCP metadata provider/program fields.  Account/session identifiers are
    intentionally ignored.  Unknown values remain unresolved and carry a
    safe source hint for the UI instead of being guessed.
    """
    fields: list[tuple[str, str]] = [
        ("provider", provider),
        ("cli_basename", Path(cli_path).name if cli_path else ""),
        ("mcp_name", mcp_name),
        ("mcp_display_name", mcp_display_name),
    ]
    meta = metadata if isinstance(metadata, Mapping) else {}
    for key in ("program", "program_id", "provider", "server", "server_name"):
        fields.append((f"metadata.{key}", str(meta.get(key) or "")))

    for source, raw in fields:
        token = _identity_token(raw)
        product = PROGRAM_ID_ALIASES.get(token)
        if product:
            return {
                "program_id": product,
                "provider": product,
                "display_name": provider_display_name(product),
                "resolution": "verified",
                "source": source,
                "source_hint": token,
            }

    hints = [f"{source}={_identity_token(raw)}" for source, raw in fields if _identity_token(raw)]
    return {
        "program_id": "unknown",
        "provider": "",
        "display_name": provider_display_name("unknown"),
        "resolution": "unresolved",
        "source": "",
        "source_hint": "; ".join(hints[:3]),
    }


def product_for_dot_dir(dot_dir_name: str) -> str | None:
    """返回点目录对应的产品名。未匹配返回 None。"""
    return AGENT_PRODUCT_MAP.get(dot_dir_name)


def backup_dir_name(name: str) -> bool:
    folded = str(name or "").casefold()
    return any(token in folded for token in ("backup", ".bak", "-bak", ".old", "-old", ".copy", "-copy"))


def unmapped_agent_evidence(path: str | Path) -> bool:
    """True when an unmapped directory has an install file. Does not read contents."""
    try:
        names = {child.name.casefold() for child in Path(path).iterdir() if child.is_file()}
    except OSError:
        return False
    return bool(names & {"mcp.json", ".mcp.json", "hooks.json"})


def candidate_visible(dir_name: str, product: str, dir_path: str | Path) -> bool:
    """Mapped products stay visible. Ordinary hidden dirs and backups do not.

    An unmapped directory is unresolved, never a formal agent, and only when
    an install filename is present. Name alone is not evidence.
    """
    if backup_dir_name(dir_name):
        return False
    if str(product or "") not in {"", "unknown"}:
        return True
    return unmapped_agent_evidence(dir_path)


def is_known_product(product_name: str) -> bool:
    """检查产品名是否在映射表中。"""
    return product_name in KNOWN_PRODUCTS


def provider_display_name(product: str) -> str:
    """User-facing program label for a provider/product id."""
    key = str(product or "").strip().casefold()
    if key in PRODUCT_DISPLAY_NAMES:
        return PRODUCT_DISPLAY_NAMES[key]
    text = str(product or "").strip()
    if not text or text.casefold() == "unknown":
        return "unknown"
    return text.replace("-", " ").replace("_", " ").title()


def candidate_agent_products() -> list[dict[str, Any]]:
    """返回当前映射表支持的所有 Agent 产品信息（静态数据）。"""
    result = []
    seen_products: set[str] = set()
    for dot_dir, product in AGENT_PRODUCT_MAP.items():
        if product in seen_products:
            continue
        seen_products.add(product)
        result.append({
            "product": product,
            "dot_dir_name": dot_dir,
            "has_profile": is_known_product(product),
        })
    return result


def detect_stale_status(dir_path: str | Path) -> dict[str, Any]:
    """检测 Agent 目录的 stale 状态。

    只做 stat，不读正文，递归最多 2 层统计文件数和大小。

    返回:
        {
            "stale_status": "active" | "stale" | "likely_uninstalled",
            "mtime": float,
            "mtime_iso": str,
            "size_bytes": int,
            "file_count": int,
            "has_executable": bool,
            "days_since_modified": float,
        }
    """
    path = Path(dir_path)
    if not path.exists() or not path.is_dir():
        return {
            "stale_status": "likely_uninstalled",
            "mtime": 0.0,
            "mtime_iso": "",
            "size_bytes": 0,
            "file_count": 0,
            "has_executable": False,
            "days_since_modified": -1,
        }

    total_size = 0
    file_count = 0
    has_executable = False
    latest_mtime = path.stat().st_mtime

    def _scan(dir_path: Path, depth: int) -> None:
        nonlocal total_size, file_count, has_executable, latest_mtime
        if depth > 2:
            return
        try:
            for entry in dir_path.iterdir():
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if entry.is_file():
                    total_size += st.st_size
                    file_count += 1
                    if st.st_mtime > latest_mtime:
                        latest_mtime = st.st_mtime
                    if entry.suffix.lower() in (".exe", ".app") or (
                        entry.suffix == "" and os.access(entry, os.X_OK)
                    ):
                        has_executable = True
                elif entry.is_dir():
                    if st.st_mtime > latest_mtime:
                        latest_mtime = st.st_mtime
                    _scan(entry, depth + 1)
        except (OSError, PermissionError):
            pass

    _scan(path, 0)

    now = time.time()
    age_seconds = now - latest_mtime
    days_since = age_seconds / (24 * 3600)

    if age_seconds >= LIKELY_UNINSTALLED_THRESHOLD_SECONDS and not has_executable:
        stale_status = "likely_uninstalled"
    elif age_seconds >= STALE_THRESHOLD_SECONDS and not has_executable:
        stale_status = "stale"
    else:
        stale_status = "active"

    from datetime import datetime, timezone
    mtime_iso = datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat() if latest_mtime > 0 else ""

    return {
        "stale_status": stale_status,
        "mtime": latest_mtime,
        "mtime_iso": mtime_iso,
        "size_bytes": total_size,
        "file_count": file_count,
        "has_executable": has_executable,
        "days_since_modified": round(days_since, 1),
    }
