"""v3.1 §3.2 AgentLocator 有限候选发现。

四层职责：
- AgentLocator：发现本机已知 Agent 候选，只做有限元数据探测
- AgentProfile：声明该 Agent 的已知本地表面、分类、加载关系和版本证据
- SourceRegistry：保存用户已经授权读取的 SourceRoot
- SourceAdapter：对授权根执行 inventory/read，不负责猜 Agent

安全边界（v3.1 §3.3）：
允许：
1. 当前工作区及其 Profile 明确声明的祖先规则文件；
2. Skill/CLI 宿主显式传入的配置根；
3. Profile 声明的少量固定路径模板；
4. 用户点击"检测本机 Agent"后，对固定路径执行 exists/lstat 和有限数量统计；
5. 用户手工选择的其他目录。

禁止：
- 从用户主目录递归搜索所有名为 memory/context/data 的文件夹；
- 候选阶段读取正文；
- 探测 Cookie、凭据和浏览器数据；
- 通过未公开路径猜测厂商私有数据库；
- 把云端或 GUI 内存声称为已追溯。
"""
from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_profiles import AgentProfileRegistry, detect_surface
from .agent_mapping import (
    AGENT_PRODUCT_MAP, IGNORED_DIRS,
    product_for_dot_dir, is_known_product, detect_stale_status,
    provider_display_name, candidate_visible,
)
from .schema_v3 import (
    AgentInstance, AgentProfile, DiscoveryEntry, DiscoveryLedger,
    SurfaceStatus, TakeoverState, TargetCapability, stable_hash,
)


@dataclass
class AgentCandidate:
    """discover_candidates() 返回的候选条目。"""
    dir_path: str
    dir_name: str
    product: str            # "unknown" 表示未映射
    has_profile: bool       # 是否有完整 AgentProfile
    stale_status: str       # "active" / "stale" / "likely_uninstalled"
    marked_uninstalled: bool = False
    mtime_iso: str = ""
    size_bytes: int = 0
    file_count: int = 0
    days_since_modified: float = -1

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_path": self.dir_path,
            "dir_name": self.dir_name,
            "product": self.product,
            "has_profile": self.has_profile,
            "stale_status": self.stale_status,
            "marked_uninstalled": self.marked_uninstalled,
            "mtime_iso": self.mtime_iso,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "days_since_modified": self.days_since_modified,
        }



@dataclass
class DetectionContext:
    """宿主显式传入的检测上下文（v3.1 §3.2）。"""

    workspace: str = ""
    host_id: str = ""
    config_root: str = ""
    platform: str = field(default_factory=lambda: platform.system().lower())

    @classmethod
    def from_workspace(cls, workspace: str | Path) -> "DetectionContext":
        return cls(
            workspace=str(workspace),
            host_id=os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "localhost",
            platform=platform.system().lower(),
        )


# 勾选授权：仅 Agent 长期/原生记忆；其余按用途分流展示
MEMORY_SELECTABLE_CATEGORIES: frozenset[str] = frozenset({
    "native_memory",
    "project_memory",
})
# 下方可点开萃取（非常规记忆层）
EXTRACT_DISPLAY_CATEGORIES: frozenset[str] = frozenset({
    "conversation_history",
    "runtime_evidence",
    "knowledge_source",
})
# 控制面 / Skill 为规则与指令层，不进记忆勾选也不展示
HIDDEN_SURFACE_CATEGORIES: frozenset[str] = frozenset({
    "control_surface",
    "skill_surface",
    "ignored_runtime_data",
})
SESSION_DISPLAY_CATEGORIES: frozenset[str] = EXTRACT_DISPLAY_CATEGORIES


_CODEX_HOME_NAMES = frozenset({".codex", "codex-home"})
_ROUTER_DATA_ENV = (
    "CODEXROUTER_DATA",
    "CODEX_ROUTER_DATA",
    "CODEXROUTER_HOME",
    "CODEX_ROUTER_HOME",
)


def current_codex_home() -> Path:
    """Return the Codex user root for this process: CODEX_HOME, else ~/.codex."""
    configured = str(os.environ.get("CODEX_HOME", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _looks_like_codex_home(path: Path) -> bool:
    if not path.is_dir():
        return False
    name = path.name.casefold()
    if name in _CODEX_HOME_NAMES:
        return True
    return (path / "config.toml").is_file() or (path / "hooks.json").is_file()


def _add_codex_home(found: list[Path], seen: set[str], raw: Path) -> None:
    try:
        resolved = raw.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return
    if not _looks_like_codex_home(resolved):
        return
    key = str(resolved).casefold()
    if key in seen:
        return
    seen.add(key)
    found.append(resolved)


def _scan_router_profiles(found: list[Path], seen: set[str], profiles_root: Path) -> None:
    if not profiles_root.is_dir() or profiles_root.name.casefold() != "profiles":
        return
    try:
        children = list(profiles_root.iterdir())
    except (OSError, PermissionError):
        return
    for child in children:
        if not child.is_dir():
            continue
        _add_codex_home(found, seen, child / "codex-home")
        _add_codex_home(found, seen, child / ".codex")
        if _looks_like_codex_home(child):
            _add_codex_home(found, seen, child)


def discover_codex_homes(*, include_default_router: bool = False) -> tuple[Path, ...]:
    """Discover current and sibling Codex roots without scanning $HOME recursively.

    Router account directories are transport aliases of one Codex program.
    The current CODEX_HOME, ~/.codex, and bounded Router profile roots are
    included when they exist.  The documented per-user CodexRouter root is
    opt-in because only bare control-home recovery may inspect it; ordinary
    provider writes must not fan out to profiles selected by a clean shell.
    """
    found: list[Path] = []
    seen: set[str] = set()
    current = current_codex_home()
    _add_codex_home(found, seen, current)
    _add_codex_home(found, seen, Path.home() / ".codex")
    if current.name.casefold() in _CODEX_HOME_NAMES:
        _scan_router_profiles(found, seen, current.parent.parent)
    if include_default_router:
        # Bare system launchers do not inherit CODEX_HOME.  CodexRouter's own
        # fixed LocalAppData root remains a bounded provider-owned discovery
        # root; this is not a cwd or generic user-directory scan.
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        router_root = (
            Path(local_appdata) / "CodexRouter"
            if local_appdata
            else Path.home() / "AppData" / "Local" / "CodexRouter"
        )
        _scan_router_profiles(found, seen, router_root / "profiles")
    for env_name in _ROUTER_DATA_ENV:
        raw = str(os.environ.get(env_name, "") or "").strip()
        if not raw:
            continue
        root = Path(raw).expanduser()
        _scan_router_profiles(found, seen, root / "profiles")
        _scan_router_profiles(found, seen, root)
    return tuple(found)


def _read_codex_memories_flags(config_path: Path) -> dict[str, bool | None]:
    """读取 ~/.codex/config.toml 的 [memories] 开关（无 tomllib 依赖，轻量解析）。"""
    out: dict[str, bool | None] = {
        "generate_memories": None,
        "use_memories": None,
    }
    if not config_path.is_file():
        return out
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_section = line.lower() in {"[memories]", "[memories.memories]"}
            continue
        if not in_section or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().lower()
        val = val.split("#", 1)[0].strip().lower()
        if key in out and val in {"true", "false"}:
            out[key] = val == "true"
    return out


def _count_codex_stage1_rows(db_path: Path) -> int | None:
    """统计 memories_1.sqlite stage1_outputs 行数；失败返回 None。"""
    if not db_path.is_file():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM stage1_outputs"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception:
        return None


def session_overview_title(path: str, project_ref: str = "") -> str:
    """从会话文件路径生成可读概览标题（不参与勾选）。"""
    p = Path(path.replace("\\", "/"))
    stem = p.stem or p.name
    parent = p.parent.name
    if project_ref and project_ref not in ("", "(未归属)"):
        base = project_ref
    elif parent and parent.lower() not in ("projects", "sessions", "transcripts", "agent-transcripts"):
        base = parent
    else:
        base = stem
    if len(base) > 40:
        base = base[:36] + "…"
    return f"会话 · {base}"


class AgentLocator:
    """v3.1 §3.2 有限候选发现器。

    只做：
    1. 加载所有 Profile
    2. 对每个 Profile 的每个 surface 执行 exists/lstat
    3. 生成 DiscoveryLedger
    4. 返回 AgentInstance 列表

    不做：
    - 不读正文
    - 不递归扫描
    - 不猜测未公开路径
    """

    def __init__(self, workspace: str | Path, context: DetectionContext | None = None):
        self.workspace = Path(workspace).resolve()
        self.context = context or DetectionContext.from_workspace(self.workspace)
        self.registry = AgentProfileRegistry(self.workspace)

    def discover_candidates(self, *, include_uninstalled: bool = False,
                            include_stale: bool = True,
                            include_unknown: bool = True) -> list[AgentCandidate]:
        """扫描当前系统 HOME 下所有 .<dir>，返回 Agent 候选列表。

        只扫当前系统 HOME（Path.home()），不做跨系统探测。
        每个候选取 stale_status，并查 uninstalled.json 判断是否已标记卸载。

        参数:
            include_uninstalled: 是否包含已标记为 uninstalled 的候选
            include_stale: 是否包含 stale 候选
            include_unknown: 是否包含未映射的 unknown 目录
        """
        home = Path.home()
        uninstalled = self._load_uninstalled_set()
        candidates: list[AgentCandidate] = []

        try:
            entries = list(home.iterdir())
        except (OSError, PermissionError):
            return []

        for entry in entries:
            if not entry.is_dir():
                continue
            if not entry.name.startswith("."):
                continue
            if entry.name in IGNORED_DIRS:
                continue

            product = product_for_dot_dir(entry.name) or "unknown"
            if not candidate_visible(entry.name, product, entry):
                continue
            if product == "unknown" and not include_unknown:
                continue

            marked = product in uninstalled
            if marked and not include_uninstalled:
                continue

            stale_info = detect_stale_status(entry)
            if stale_info["stale_status"] == "stale" and not include_stale:
                continue

            has_profile = product != "unknown" and is_known_product(product)

            candidate = AgentCandidate(
                dir_path=str(entry),
                dir_name=entry.name,
                product=product,
                has_profile=has_profile,
                stale_status=stale_info["stale_status"],
                marked_uninstalled=marked,
                mtime_iso=stale_info["mtime_iso"],
                size_bytes=stale_info["size_bytes"],
                file_count=stale_info["file_count"],
                days_since_modified=stale_info["days_since_modified"],
            )
            candidates.append(candidate)

        return candidates

    def _load_uninstalled_set(self) -> set[str]:
        """读取 .memoryguard/cleanup/uninstalled.json，返回已标记卸载的产品名集合。"""
        path = self.workspace / ".memoryguard" / "cleanup" / "uninstalled.json"
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(data.get("products", []))
        except (ValueError, OSError):
            return set()

    def detect_instances(self) -> tuple[list[AgentInstance], dict[str, DiscoveryLedger]]:
        """检测所有 Profile 的实例。

        返回 (instances, ledgers_by_instance_id)。
        一个 Profile 只要有一个 surface FOUND 就算作 discovered。
        """
        instances: list[AgentInstance] = []
        ledgers: dict[str, DiscoveryLedger] = {}
        for profile in self.registry.list_profiles():
            instance, ledger = self._detect_one(profile)
            # v3.1 §3.4：只有至少一个 FOUND 才算 discovered
            found_count = sum(1 for e in ledger.entries if e.status == SurfaceStatus.FOUND)
            if found_count > 0:
                instances.append(instance)
                ledgers[instance.instance_id] = ledger
        return instances, ledgers

    def _detect_one(self, profile: AgentProfile) -> tuple[AgentInstance, DiscoveryLedger]:
        """探测单个 Profile 的所有 surface。"""
        instance_id = stable_hash(
            profile.profile_id,
            self.context.host_id,
            str(self.workspace),
        )
        surfaces_results: list[dict[str, Any]] = []
        entries: list[DiscoveryEntry] = []
        overlay_roots = discover_codex_homes() if profile.product == "codex" else ()
        for surface in profile.surfaces:
            status, resolved = detect_surface(
                surface,
                home=Path.home(),
                workspace=self.workspace,
                appdata=os.environ.get("APPDATA", str(Path.home())),
            )
            if (
                profile.product == "codex"
                and status != SurfaceStatus.FOUND
                and str(surface.path_template or "").startswith("%HOME%/.codex")
            ):
                rest = str(surface.path_template)[len("%HOME%/.codex"):].lstrip("/\\")
                for overlay in overlay_roots:
                    candidate = overlay.joinpath(*rest.split("/")) if rest else overlay
                    try:
                        if candidate.exists():
                            status, resolved = SurfaceStatus.FOUND, str(candidate)
                            break
                    except OSError:
                        continue
            entries.append(DiscoveryEntry(
                profile_id=profile.profile_id,
                surface_id=surface.surface_id,
                status=status,
                resolved_path=resolved,
                reason="" if status == SurfaceStatus.FOUND else f"surface {surface.surface_id} {status.value}",
            ))
            surfaces_results.append({
                "surface_id": surface.surface_id,
                "path_template": surface.path_template,
                "resolved_path": resolved,
                "status": status.value,
                "surface_role": surface.surface_role,
                "scope": surface.scope,
                "category": surface.category.value,
                "ingestion_policy": surface.ingestion_policy.value,
                "ownership": surface.ownership.value,
                "target_role": surface.target_role.value,
                "classification_confidence": surface.classification_confidence,
                "evidence_role": surface.evidence_role,
                "file_globs": list(surface.file_globs or []),
            })
        instance = AgentInstance(
            instance_id=instance_id,
            profile_id=profile.profile_id,
            product=profile.product,
            profile_version=profile.profile_version,
            platform=self.context.platform,
            host_id=self.context.host_id,
            workspace=str(self.workspace),
            config_root=self.context.config_root,
            surfaces=surfaces_results,
            target_capability=profile.target_capability,
        )
        ledger = DiscoveryLedger(instance_id=instance_id, entries=entries)
        return instance, ledger

    def get_selection_tree(self, instance_id: str) -> dict[str, Any]:
        """v3.2 改动包1：返回作用域优先的分类勾选树。

        结构从按 category 分组改为按 scope -> project_ref -> category 三层分组：
        {
          "instance_id": ...,
          "profile_id": ...,
          "product": ...,
          "scopes": [
            {
              "scope": "user",
              "scope_source": "profile_declared",
              "categories": [
                {
                  "category": "native_memory",
                  "files": [
                    {
                      "path": "/abs/path",
                      "surface_id": "...",
                      "scope": "user",
                      "scope_source": "profile_declared",
                      "project_ref": "",
                      "discovery_object_id": "...",
                      "ingestion_policy": "import_verbatim",
                      "ownership": "agent_managed",
                      "target_role": "takeover_input",
                      "default_selected": True,
                      "default_reason": "原生记忆默认选中",
                      "status": "found",
                      "confidence": 0.95,
                      "last_modified": "..."
                    }
                  ]
                }
              ]
            },
            {
              "scope": "project",
              "projects": [
                {
                  "project_ref": "MemoryGuard",
                  "scope_source": "project_resolver",
                  "categories": [...]
                }
              ]
            },
            {
              "scope": "unknown",
              "categories": [...]
            }
          ]
        }
        """
        instances, ledgers = self.detect_instances()
        instance = next((i for i in instances if i.instance_id == instance_id), None)
        if instance is None:
            return {"error": f"instance not found: {instance_id}"}

        # 按 scope -> project_ref -> category 三层分组
        scope_map: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
        # scope_map[scope][project_ref][category] = [files]

        for s in instance.surfaces:
            if s.get("status") != "found":
                continue
            scope = s.get("scope", "user")
            cat = s.get("category", "unknown")
            resolved = s.get("resolved_path", "")

            # 项目根目录展开：如果该 surface 是项目根目录，展开子目录
            # 再按 file_globs 落到真实文件节点
            effective_surfaces = [s]
            has_globs = bool(s.get("file_globs"))
            if self._is_date_tree_root(resolved):
                effective_surfaces = self._expand_date_tree(resolved, s)
            elif self._is_project_root_surface(resolved):
                expanded = self._expand_project_root(resolved, s)
                # 声明了 file_globs 时，即使无匹配也不得回退到整目录节点
                # （避免 ~/.claude/projects 整树被误授权）
                if has_globs:
                    effective_surfaces = expanded
                elif expanded:
                    effective_surfaces = expanded
            elif has_globs and Path(resolved).is_dir():
                expanded = self._expand_files_in_dir(Path(resolved), s, project_ref="")
                if expanded:
                    effective_surfaces = expanded
                elif cat in MEMORY_SELECTABLE_CATEGORIES:
                    # 专用记忆目录已找到但尚无 md：保留目录节点供勾选/提示
                    # （例如 Codex memories 未开启时 ~/.codex/memories 为空）
                    node = dict(s)
                    node["_empty_glob_match"] = True
                    node["_is_file_node"] = False
                    node["default_reason_override"] = (
                        "已找到记忆目录，但尚未生成 MEMORY.md 等文件"
                    )
                    effective_surfaces = [node]
                else:
                    effective_surfaces = []

            for es in effective_surfaces:
                es_resolved = es.get("resolved_path", "")
                file_cat = es.get("category") or cat
                if file_cat in HIDDEN_SURFACE_CATEGORIES:
                    continue
                # 项目归属：scope=project 时尝试从路径解析项目名
                project_ref = ""
                scope_source = "profile_declared"
                if es.get("_expanded_project_ref"):
                    project_ref = es["_expanded_project_ref"]
                    scope_source = "project_resolver"
                elif scope == "project":
                    project_ref = self._resolve_project_ref(es_resolved)
                    scope_source = "project_resolver" if project_ref else "fallback"
                elif scope == "user":
                    project_ref = ""
                    scope_source = "profile_declared"
                else:
                    scope_source = "fallback"

                es_scope = es.get("scope", scope)

                # 生成稳定的 discovery_object_id
                canonical_path = es_resolved.replace("\\", "/")
                surface_id = es.get("surface_id", "")
                dobj_id = stable_hash(instance_id, surface_id, canonical_path, "v1")

                # 默认选中策略：仅记忆层面可勾选；会话/证据只读展示
                ing = es.get("ingestion_policy", "extract_candidates")
                selectable = file_cat in MEMORY_SELECTABLE_CATEGORIES
                default_selected = (
                    selectable
                    and ing == "import_verbatim"
                    and not es.get("_is_truncation_marker")
                )
                default_reason = es.get("default_reason_override") or self._default_selection_reason(ing, file_cat)
                if file_cat in SESSION_DISPLAY_CATEGORIES:
                    default_reason = "可点开萃取，不纳入记忆勾选"

                file_info = {
                    "path": es_resolved,
                    "surface_id": surface_id,
                    "scope": es_scope,
                    "scope_source": scope_source,
                    "project_ref": project_ref,
                    "discovery_object_id": dobj_id,
                    "ingestion_policy": ing,
                    "ownership": es.get("ownership", "external_read_only"),
                    "target_role": es.get("target_role", "none"),
                    "default_selected": default_selected,
                    "default_reason": default_reason,
                    "status": es.get("status", "found"),
                    "confidence": es.get("classification_confidence", 0.5),
                    "last_modified": "",
                    "is_file_node": bool(es.get("_is_file_node")),
                    "selectable": selectable,
                    "display_only": not selectable,
                    "empty_glob_match": bool(es.get("_empty_glob_match")),
                }
                if file_cat in SESSION_DISPLAY_CATEGORIES:
                    file_info["session_title"] = session_overview_title(es_resolved, project_ref)

                scope_map.setdefault(es_scope, {})
                pr_key = project_ref or "_no_project"
                scope_map[es_scope].setdefault(pr_key, {})
                scope_map[es_scope][pr_key].setdefault(file_cat, [])
                scope_map[es_scope][pr_key][file_cat].append(file_info)

        # 构建返回结构
        scopes_output = []
        for scope in ["user", "project", "unknown"]:
            if scope not in scope_map:
                continue
            projects = scope_map[scope]
            if scope == "project":
                # 按项目分组
                project_list = []
                for pr_key, cat_map in projects.items():
                    project_ref = pr_key if pr_key != "_no_project" else ""
                    categories = [
                        {"category": cat, "files": files}
                        for cat, files in cat_map.items()
                    ]
                    project_list.append({
                        "project_ref": project_ref or "(未归属)",
                        "scope_source": "project_resolver" if project_ref else "fallback",
                        "categories": categories,
                    })
                scopes_output.append({
                    "scope": scope,
                    "scope_source": "project_resolver",
                    "projects": project_list,
                })
            else:
                # user / unknown：无 project_ref 时扁平 categories；
                # 展开后的项目子目录（如 ~/.claude/projects/<proj>）走 projects 列表
                project_list = []
                for pr_key, cat_map in projects.items():
                    if pr_key == "_no_project":
                        continue
                    project_ref = pr_key
                    categories = [
                        {"category": cat, "files": files}
                        for cat, files in cat_map.items()
                    ]
                    project_list.append({
                        "project_ref": project_ref,
                        "scope_source": "project_resolver",
                        "categories": categories,
                    })
                cat_map = projects.get("_no_project", {})
                categories = [
                    {"category": cat, "files": files}
                    for cat, files in cat_map.items()
                ]
                scope_entry: dict[str, Any] = {
                    "scope": scope,
                    "scope_source": "profile_declared" if scope == "user" else "fallback",
                    "categories": categories,
                }
                if project_list:
                    scope_entry["projects"] = project_list
                scopes_output.append(scope_entry)

        notes = self._selection_discovery_notes(instance)
        return {
            "instance_id": instance_id,
            "profile_id": instance.profile_id,
            "product": instance.product,
            "display_name": provider_display_name(instance.product),
            "label": provider_display_name(instance.product),
            "scopes": scopes_output,
            "discovery_notes": notes,
        }

    def _selection_discovery_notes(self, instance: AgentInstance) -> list[dict[str, Any]]:
        """选择树提示：例如 Codex memories 功能关闭、目录为空。"""
        notes: list[dict[str, Any]] = []
        product = getattr(instance, "product", "") or ""
        if product == "codex":
            notes.extend(self._codex_memories_notes(instance))
        elif product == "cursor":
            notes.extend(self._cursor_memories_notes(instance))
        return notes

    def _cursor_memories_notes(self, instance: AgentInstance) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        gui = next(
            (s for s in instance.surfaces if s.get("surface_id") == "cursor_memories_gui_only"),
            None,
        )
        if gui and gui.get("status") in {"unsupported", "missing", "found"}:
            notes.append({
                "level": "warn",
                "code": "cursor_memories_gui_only",
                "message": (
                    "Cursor Settings Memories 当前无本地明文记忆文件可勾选"
                    "（gui-only 表面）。state.vscdb 主要是会话气泡/composer 证据，不是长期记忆库。"
                ),
                "hint": "可勾选/萃取：agent-transcripts；长期记忆请走 MemoryGuard 共享 MCP 接管。",
            })
        return notes

    def _codex_memories_notes(self, instance: AgentInstance) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        mem_surface = next(
            (s for s in instance.surfaces if s.get("surface_id") == "codex_native_memories"),
            None,
        )
        mem_path = Path((mem_surface or {}).get("resolved_path") or "")
        cfg_path = current_codex_home() / "config.toml"
        flags = _read_codex_memories_flags(cfg_path)
        if flags.get("generate_memories") is False or flags.get("use_memories") is False:
            notes.append({
                "level": "warn",
                "code": "codex_memories_disabled",
                "message": (
                    "Codex config.toml 中 [memories] 已关闭"
                    f"（generate_memories={flags.get('generate_memories')}, "
                    f"use_memories={flags.get('use_memories')}）。"
                    "未开启时不会生成 ~/.codex/memories/*.md，勾选树只能看到空目录。"
                ),
                "hint": "在 Codex 设置/Personalization 开启 Enable memories，或编辑 config.toml。",
            })
        if mem_path.is_dir():
            md_count = sum(1 for _ in mem_path.rglob("*.md"))
            if md_count == 0:
                notes.append({
                    "level": "info",
                    "code": "codex_memories_empty",
                    "message": f"已找到 {mem_path}，但其中尚无 .md 记忆文件。",
                    "hint": "可先勾选该目录授权；会话历史请在下方「可萃取来源」中处理。",
                })
        sqlite_surface = next(
            (s for s in instance.surfaces if s.get("surface_id") == "codex_memories_sqlite"),
            None,
        )
        if sqlite_surface and sqlite_surface.get("status") == "found":
            stage1 = _count_codex_stage1_rows(Path(sqlite_surface.get("resolved_path") or ""))
            if stage1 == 0:
                notes.append({
                    "level": "info",
                    "code": "codex_memories_sqlite_empty",
                    "message": "memories_1.sqlite 中 stage1_outputs 为空（尚无抽取中间态）。",
                    "hint": "这是运行证据，不进入记忆勾选；开启 memories 并产生会话后才会写入。",
                })
        return notes

    def _resolve_project_ref(self, path: str) -> str:
        """从路径尝试解析项目名。

        规则：
        - workspace 下的固定 Surface -> 项目名为 workspace 本身的名称
        - ~/.claude/projects/<project_dir> -> 取 <project_dir>
        - ~/.codex/sessions/<project_dir> -> 取 <project_dir>
        - ~/.cursor/projects/<project_dir> -> 取 <project_dir>
        - ~/.codeium/windsurf/memories/<project_dir> -> 取 <project_dir>
        - ~/.trae-cn/memory/projects/<project_dir> -> 取 <project_dir>
        - ~/.zcode/cli/agents/<project_dir> -> 取 <project_dir>
        - 无法确认时返回空（进入 unknown）
        """
        if not path:
            return ""
        p = Path(path)
        try:
            ws = self.workspace.resolve()
            pr = p.resolve()
            # 真实父子路径判断，避免 C:\project 和 C:\project-old 误判
            if pr == ws or ws in pr.parents:
                return ws.name
        except (ValueError, OSError):
            pass
        normalized = path.replace("\\", "/")
        # 各 Agent 的项目目录模式
        project_patterns = [
            "/.claude/projects/",
            "/.codex/sessions/",
            "/.cursor/projects/",
            "/.codeium/windsurf/memories/",
            "/.trae-cn/memory/projects/",
            "/.zcode/cli/agents/",
            "/.trae-cn/work/",
        ]
        for pattern in project_patterns:
            if pattern in normalized:
                parts = normalized.split(pattern)
                if len(parts) > 1 and parts[1]:
                    # 取第一级目录名（可能后面还有子路径）
                    segments = parts[1].split("/")
                    if segments and segments[0]:
                        return segments[0]
        return ""

    # 需要展开子目录的项目根目录模式（不含 Codex sessions 日期树）
    _PROJECT_ROOT_PATTERNS = [
        "/.claude/projects",
        "/.cursor/projects",
        "/.codeium/windsurf/memories",
        "/.trae-cn/memory/projects",
        "/.zcode/cli/agents",
    ]
    # 日期分区树：展开文件但不把 YYYY 当年/月当 project_ref
    _DATE_TREE_ROOT_PATTERNS = [
        "/.codex/sessions",
    ]

    def _is_project_root_surface(self, resolved_path: str) -> bool:
        """判断该 surface 路径是否是项目根目录（含多个项目子目录）。"""
        if not resolved_path:
            return False
        normalized = resolved_path.replace("\\", "/").rstrip("/")
        for pattern in self._PROJECT_ROOT_PATTERNS:
            if normalized.endswith(pattern):
                return True
        return False

    def _is_date_tree_root(self, resolved_path: str) -> bool:
        if not resolved_path:
            return False
        normalized = resolved_path.replace("\\", "/").rstrip("/")
        return any(normalized.endswith(p) for p in self._DATE_TREE_ROOT_PATTERNS)

    def _expand_project_root(self, resolved_path: str, surface: dict) -> list[dict]:
        """展开项目根目录：一级子目录 + 可选 file_globs 到文件节点。

        预算：每项目最多 MAX_FILES_PER_PROJECT 个文件；超出写入 truncation 标记。
        """
        root = Path(resolved_path)
        if not root.exists() or not root.is_dir():
            return []
        globs = list(surface.get("file_globs") or [])
        expanded: list[dict] = []
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name.startswith(".") or sub.name.startswith("_"):
                continue
            if globs:
                files = self._expand_files_in_dir(sub, surface, project_ref=sub.name)
                expanded.extend(files)
            else:
                expanded_surface = dict(surface)
                expanded_surface["resolved_path"] = str(sub)
                expanded_surface["surface_id"] = surface.get("surface_id", "") + ":" + sub.name
                expanded_surface["_expanded_project_ref"] = sub.name
                expanded.append(expanded_surface)
        return expanded

    def _expand_date_tree(self, resolved_path: str, surface: dict) -> list[dict]:
        """Codex sessions 等日期树：直接 glob 文件，project_ref 置空。"""
        root = Path(resolved_path)
        if not root.exists() or not root.is_dir():
            return []
        globs = list(surface.get("file_globs") or ["**/*.jsonl"])
        # 在根上直接 glob，不把 YYYY 当项目名
        surface_copy = dict(surface)
        surface_copy["file_globs"] = globs
        return self._expand_files_in_dir(root, surface_copy, project_ref="")

    MAX_FILES_PER_PROJECT = 200

    def _expand_files_in_dir(self, base: Path, surface: dict, *, project_ref: str) -> list[dict]:
        """按 file_globs 在 base 下展开文件节点。"""
        globs = list(surface.get("file_globs") or [])
        if not globs:
            return []
        found: list[Path] = []
        seen: set[str] = set()
        truncated = False
        for pattern in globs:
            try:
                matches = sorted(base.glob(pattern))
            except (OSError, ValueError):
                continue
            for p in matches:
                if not p.is_file():
                    continue
                # 逻辑路径必须在 base 下
                try:
                    p.relative_to(base)
                except ValueError:
                    continue
                # 符号链接目标不得逃逸；目录 junction 导致 resolve 跨卷时保留逻辑路径
                try:
                    resolved_file = p.resolve()
                    resolved_base = base.resolve()
                    resolved_file.relative_to(resolved_base)
                except (ValueError, OSError):
                    if p.is_symlink():
                        continue
                    # 非 symlink 文件但祖先是 junction：用逻辑路径，读时再做 containment
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                if len(found) >= self.MAX_FILES_PER_PROJECT:
                    truncated = True
                    break
                found.append(p)
            if truncated:
                break

        out: list[dict] = []
        for p in found:
            rel = str(p.relative_to(base)).replace("\\", "/")
            node = dict(surface)
            node["resolved_path"] = str(p)
            node["surface_id"] = f"{surface.get('surface_id', '')}:{project_ref or base.name}:{rel}"
            node["_expanded_project_ref"] = project_ref or base.name
            node["_is_file_node"] = True
            node["_relative_path"] = rel
            out.append(node)
        if truncated:
            # 显式截断节点，禁止静默丢弃
            marker = dict(surface)
            marker["resolved_path"] = str(base)
            marker["surface_id"] = f"{surface.get('surface_id', '')}:{project_ref}:__truncated__"
            marker["_expanded_project_ref"] = project_ref or base.name
            marker["status"] = "found"
            marker["ingestion_policy"] = "ignore"
            marker["default_reason_override"] = (
                f"file_globs truncated at {self.MAX_FILES_PER_PROJECT} files"
            )
            marker["_is_truncation_marker"] = True
            out.append(marker)
        return out

    @staticmethod
    def _default_selection_reason(ingestion_policy: str, category: str) -> str:
        """默认选中/不选中的原因。"""
        reasons = {
            "import_verbatim": "原生记忆默认选中，读取后仍需确认治理",
            "extract_candidates": "普通文档需手动勾选",
            "govern_only": "规则/指令文件默认治理，不自动萃取为事实",
            "evidence_only": "运行证据仅用于评估，默认不选",
            "ignore": "密钥/认证/缓存始终排除",
        }
        return reasons.get(ingestion_policy, "按策略处理")

    def save_discovery(self, instances: list[AgentInstance],
                       ledgers: dict[str, DiscoveryLedger]) -> Path:
        """持久化 DiscoveryLedger 到 .memoryguard/discovery/。"""
        out_dir = self.workspace / ".memoryguard" / "discovery"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "latest.json"
        data = {
            "detected_at": ledgers and list(ledgers.values())[0].entries[0].reason or "",
            "context": {
                "workspace": str(self.workspace),
                "host_id": self.context.host_id,
                "platform": self.context.platform,
            },
            "instances": [i.to_dict() for i in instances],
            "ledgers": {k: v.to_dict() for k, v in ledgers.items()},
        }
        out_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out_path

    def validate_discovery_objects(self, instance_id: str,
                                   discovery_object_ids: list[str]) -> dict[str, dict]:
        """验证 discovery_object_id 是否属于当前 Agent 实例。

        返回 {discovery_object_id: {valid: bool, file_info: dict|None, reason: str}}
        """
        tree = self.get_selection_tree(instance_id)
        if "error" in tree:
            return {dobj_id: {"valid": False, "file_info": None, "reason": "instance not found"} for dobj_id in discovery_object_ids}

        server_index: dict[str, dict] = {}
        for scope_obj in tree.get("scopes", []):
            for proj in scope_obj.get("projects", []):
                for cat in proj.get("categories", []):
                    for f in cat.get("files", []):
                        dobj_id = f.get("discovery_object_id", "")
                        if dobj_id:
                            info = dict(f)
                            info["category"] = cat.get("category", "unknown")
                            info["scope"] = scope_obj.get("scope", info.get("scope", "unknown"))
                            info["scope_source"] = proj.get("scope_source", scope_obj.get("scope_source", info.get("scope_source", "fallback")))
                            info["project_ref"] = proj.get("project_ref", info.get("project_ref", ""))
                            server_index[dobj_id] = info
            for cat in scope_obj.get("categories", []):
                for f in cat.get("files", []):
                    dobj_id = f.get("discovery_object_id", "")
                    if dobj_id:
                        info = dict(f)
                        info["category"] = cat.get("category", "unknown")
                        info["scope"] = scope_obj.get("scope", info.get("scope", "unknown"))
                        info["scope_source"] = scope_obj.get("scope_source", info.get("scope_source", "fallback"))
                        server_index[dobj_id] = info

        result = {}
        for dobj_id in discovery_object_ids:
            if dobj_id not in server_index:
                result[dobj_id] = {"valid": False, "file_info": None, "reason": "discovery_object_id not found in server snapshot"}
            else:
                f = server_index[dobj_id]
                if f.get("status") != "found":
                    result[dobj_id] = {"valid": False, "file_info": None, "reason": f"surface status is {f.get('status')}, not found"}
                else:
                    result[dobj_id] = {"valid": True, "file_info": f, "reason": ""}
        return result


def compute_takeover_state(instance: AgentInstance,
                           ledger: DiscoveryLedger | None,
                           selection_committed: bool,
                           canonicalized: bool,
                           release_planned: bool,
                           published: bool,
                           runtime_verified: bool,
                           drifted: bool = False) -> TakeoverState:
    """v3.1 §2.3 接管状态机。

    优先级（从高到低）：
    DRIFTED > OPERATIONAL > PUBLISHED > RELEASE_PLANNED > CANONICALIZED >
    SELECTED > PARTIAL（覆盖缺口） > DISCOVERED > NOT_DETECTED
    """
    if drifted:
        return TakeoverState.DRIFTED
    if runtime_verified:
        return TakeoverState.OPERATIONAL
    if published:
        return TakeoverState.PUBLISHED
    if release_planned:
        return TakeoverState.RELEASE_PLANNED
    if canonicalized:
        return TakeoverState.CANONICALIZED
    if selection_committed:
        return TakeoverState.SELECTED
    # 覆盖缺口优先于 DISCOVERED（v3.1 §2.3：PARTIAL 表示部分覆盖）
    if ledger is not None:
        cnt = ledger.counts()
        if cnt.get("unaccounted_count", 0) > 0 or cnt.get("missing", 0) > 0:
            return TakeoverState.PARTIAL
    if instance is not None:
        return TakeoverState.DISCOVERED
    return TakeoverState.NOT_DETECTED
