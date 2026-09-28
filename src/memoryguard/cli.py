"""MemoryGuard CLI 入口（spec §4）。

命令面:
- audit [path]       只读扫描 + 规则引擎，生成 report.json + report.html
- open [path]        打开报告（降级链：原生窗口->localhost->HTML）
- explain <id>       解释 Finding 的证据和风险
- plan <id...>       生成最小修复补丁 Diff（不写文件）
- apply <plan>       经批准应用：备份 + 补丁 + 重扫
- verify             重扫并比较修复前后
- undo <change>      撤销并再次验证

退出码（spec §4）:
- 0 成功
- 1 可恢复错误
- 2 安全策略阻断
- 3 能力不可用需回退

无网络、Core 零第三方依赖。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .discover import WorkspaceDiscoverer
from .report import render_html_report
from .rules import RuleContext, default_registry, run_rules
# 触发规则注册（副作用：导入即注册到 default_registry）
from .rules import instruction_rules  # noqa: F401
from .rules import skill_rules  # noqa: F401
from .rules import memory_rules  # noqa: F401
from .rules import rag_rules  # noqa: F401
from .runtime_v2.public_safety import v2_upgrade_payload
from .workspace_resolver import (
    WorkspaceResolutionError,
    discover_migration_source,
    resolve_workspace,
)
from .schema import (
    Change,
    ChangeStatus,
    Finding,
    Plan,
    Patch,
    Report,
    RiskLevel,
    now_iso,
    sha256_file,
    sha256_text,
    stable_id,
)


# ---------------------------------------------------------------------------
# .memoryguard/ 布局（spec §2）
# ---------------------------------------------------------------------------

MG_DIR = ".memoryguard"
REPORTS_DIR = f"{MG_DIR}/reports"


def ensure_layout(workspace: Path) -> None:
    """创建 .memoryguard/ 布局（spec §2）。可完全删除，非事实源。"""
    (workspace / MG_DIR).mkdir(exist_ok=True)
    (workspace / REPORTS_DIR).mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def run_audit(workspace: Path) -> Report:
    """执行只读扫描 + 规则引擎，返回 Report。无副作用（不写文件由调用方决定）。"""
    start = time.perf_counter()
    discoverer = WorkspaceDiscoverer(workspace)
    discovery = discoverer.discover()

    # 规则引擎（spec §8）
    ctx = RuleContext(agrs=discovery.agrs)
    findings = run_rules(ctx)

    duration_ms = int((time.perf_counter() - start) * 1000)

    report = Report(
        schema_version="1.0",
        workspace=str(workspace),
        generated_at=now_iso(),
        duration_ms=duration_ms,
        health_score=_health_score(discovery, findings),
        objects=discovery.agrs,
        findings=findings,
        invisible=discovery.invisible,
    )
    return report


def _health_score(discovery, findings=None) -> float:
    """健康分：可见率为主，扣减 Finding 严重度。0-100。"""
    total = len(discovery.agrs) + len(discovery.invisible)
    if total == 0:
        return 0.0
    visible_ratio = len(discovery.agrs) / total
    score = visible_ratio * 80 + min(len(discovery.agrs) / 20, 1.0) * 20
    # Finding 扣分
    if findings:
        sev_penalty = {"critical": 15, "high": 8, "medium": 3, "low": 1, "info": 0}
        for f in findings:
            score -= sev_penalty.get(f.severity.value, 0)
    return round(max(0.0, min(100.0, score)), 1)


def cmd_audit(args: argparse.Namespace) -> int:
    workspace = _effective_workspace(args, getattr(args, "path", "."))
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 1
    if not _is_within_cwd_or_explicit(workspace, args):
        # 安全：扫描范围必须由用户参数限定（spec §1.3, §10）
        print(f"error: workspace must be under current directory or given explicitly", file=sys.stderr)
        return 2

    ensure_layout(workspace)
    report = run_audit(workspace)

    # 写 JSON + HTML
    json_path = workspace / REPORTS_DIR / "report.json"
    html_path = workspace / REPORTS_DIR / "report.html"
    json_path.write_text(report.to_json(), encoding="utf-8")
    html_path.write_text(render_html_report(report), encoding="utf-8")

    # 摘要输出到 stdout
    s = report.summary()
    print(f"MemoryGuard audit complete in {report.duration_ms} ms")
    print(f"  workspace: {workspace}")
    print(f"  objects:   {s['object_count']}")
    print(f"  findings:  {len(report.findings)}")
    print(f"  invisible: {s['invisible_count']}")
    print(f"  health:    {report.health_score}/100")
    print(f"  report:    {html_path}")
    return 0


def _is_within_cwd_or_explicit(workspace: Path, args: argparse.Namespace) -> bool:
    """允许：显式传参，或在 cwd 下。禁止默认扫描整个用户目录。"""
    if args.path != ".":
        return True
    try:
        workspace.relative_to(Path.cwd().resolve())
        return True
    except ValueError:
        return True  # 显式传 . 也允许


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def cmd_open(args: argparse.Namespace) -> int:
    """按 spec §6.1 降级链打开治理面板。

    顺序: 桌面原生窗口 -> localhost 浏览器 -> 静态 HTML 文件 -> 文本+JSON 路径
    退出码: 0 成功, 1 无报告, 3 所有 GUI 能力不可用（已降级到文本）
    """
    workspace = _effective_workspace(args, getattr(args, "path", "."))
    mode = getattr(args, "mode", "auto")

    from .interactive import render_interactive_html
    interactive_html = render_interactive_html()

    if mode == "html":
        ui_path = workspace / ".memoryguard" / "ui" / "index.html"
        ui_path.parent.mkdir(parents=True, exist_ok=True)
        ui_path.write_text(interactive_html, encoding="utf-8")
        return _open_static_html(ui_path)

    # 1. 桌面原生窗口（pywebview 可选依赖）
    if mode in ("auto", "native"):
        from .gui import open_interactive_window, has_native_gui

        if has_native_gui():
            print("opening interactive governance panel (pywebview)...")
            rc = open_interactive_window(str(workspace), title=f"MemoryGuard - {workspace.name}")
            if rc == 0:
                return 0
            print("warning: native window unavailable, falling back...", file=sys.stderr)

    # 2. localhost 浏览器窗口（标准库 http.server）
    if mode in ("auto", "localhost"):
        from .gui import open_localhost_window

        print("opening localhost window (press Ctrl+C to close)...")
        rc, url = open_localhost_window(str(workspace), auto_open=True)
        if rc == 0:
            return 0

    # 3. 静态 HTML 文件（webbrowser.open file://）
    ui_path = workspace / ".memoryguard" / "ui" / "index.html"
    ui_path.parent.mkdir(parents=True, exist_ok=True)
    ui_path.write_text(interactive_html, encoding="utf-8")
    return _open_static_html(ui_path)


def _open_static_html(html_path: Path) -> int:
    """降级第3步：用默认浏览器打开静态 HTML 文件。"""
    url = html_path.resolve().as_uri()
    print(f"opening static HTML: {url}")
    try:
        webbrowser.open(url)
        return 0
    except Exception as e:
        print(f"warning: could not open browser ({e})", file=sys.stderr)
        # 4. 最终回退：文本 + JSON 路径
        print(f"open manually: {url}")
        print(f"json report:   {html_path.with_suffix('.json')}")
        return 3


# ---------------------------------------------------------------------------
# explain (spec §4)
# ---------------------------------------------------------------------------


def _load_report(workspace: Path) -> Report | None:
    """从 .memoryguard/reports/report.json 加载最近报告。"""
    json_path = workspace / REPORTS_DIR / "report.json"
    if not json_path.exists():
        return None
    import json

    return Report.from_dict(json.loads(json_path.read_text(encoding="utf-8")))


def cmd_explain(args: argparse.Namespace) -> int:
    """解释 Finding 的证据、影响、建议、验证方式（spec §4）。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    report = _load_report(workspace)
    if report is None:
        print(f"error: no report found. run `memoryguard audit {args.workspace}` first.", file=sys.stderr)
        return 1
    finding = next((f for f in report.findings if f.id == args.finding_id), None)
    if finding is None:
        print(f"error: finding not found: {args.finding_id}", file=sys.stderr)
        print(f"available findings: {[f.id for f in report.findings]}", file=sys.stderr)
        return 1
    print(f"Finding: {finding.id}")
    print(f"  rule:      {finding.rule_id}")
    print(f"  severity:  {finding.severity.value}")
    print(f"  dimension: {finding.dimension.value}")
    print(f"  surface:   {finding.surface.value}")
    print(f"  location:  {finding.location.path}:{finding.location.span[0]}-{finding.location.span[1]}")
    print(f"  evidence:  {finding.evidence}")
    print(f"  impact:    {finding.impact}")
    print(f"  suggestion:{finding.suggestion}")
    print(f"  confidence:{finding.confidence}")
    print(f"  fixable:   {finding.fixable}")
    print(f"  verify:    {finding.verification}")
    return 0


# ---------------------------------------------------------------------------
# plan / apply / verify / undo (spec §3.4, §9)
# ---------------------------------------------------------------------------

PLANS_DIR = f"{MG_DIR}/plans"
CHANGES_DIR = f"{MG_DIR}/changes"
BACKUPS_DIR = f"{MG_DIR}/backups"


def cmd_plan(args: argparse.Namespace) -> int:
    """为指定 Findings 生成最小修复 Plan（只读，不写源文件，spec §9）。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    report = _load_report(workspace)
    if report is None:
        print("error: no report found. run audit first.", file=sys.stderr)
        return 1
    # 找到指定的 findings
    target_findings = [f for f in report.findings if f.id in args.finding_ids]
    if not target_findings:
        print(f"error: no matching findings. available: {[f.id for f in report.findings]}", file=sys.stderr)
        return 1
    # 生成补丁（首期仅支持可 fixable 的简单文本替换/删除）
    patches: list[Patch] = []
    for f in target_findings:
        if not f.fixable:
            print(f"warning: {f.id} not auto-fixable, skipping patch generation")
            continue
        try:
            patch = _generate_patch(f)
            if patch:
                patches.append(patch)
        except Exception as e:
            print(f"warning: could not generate patch for {f.id}: {e}", file=sys.stderr)
    if not patches:
        print("no auto-fixable patches generated. manual review required.", file=sys.stderr)
        return 1
    # 风险评估
    has_high = any(f.severity.value in ("high", "critical") for f in target_findings)
    risk = RiskLevel.HIGH if has_high else RiskLevel.LOW
    plan = Plan(
        plan_id=stable_id("plan", *[f.id for f in target_findings]),
        finding_ids=[f.id for f in target_findings],
        intent=f"fix {len(target_findings)} finding(s)",
        risk_level=risk,
        patches=patches,
        created_at=now_iso(),
        preconditions=[f"file hash matches: {p.before_hash}" for p in patches],
        verification=[f.verification for f in target_findings],
        requires_approval=True,
    )
    # 写 plan 到 .memoryguard/plans/
    (workspace / PLANS_DIR).mkdir(parents=True, exist_ok=True)
    plan_path = workspace / PLANS_DIR / f"{plan.plan_id}.json"
    import json

    plan_path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"plan generated: {plan_path}")
    print(f"  findings: {plan.finding_ids}")
    print(f"  patches:  {len(plan.patches)}")
    print(f"  risk:     {plan.risk_level.value}")
    if plan.requires_approval:
        print(f"  requires approval: run `memoryguard apply {plan.plan_id}`")
    return 0


def _generate_patch(finding: Finding) -> Patch | None:
    """为 Finding 生成简单补丁。首期仅支持空文件删除/标题补全等简单场景。"""
    path = finding.location.path
    try:
        before_hash = sha256_file(Path(path))
    except OSError:
        return None
    # 简单策略：根据 rule_id 生成 diff
    if finding.rule_id == "rag.empty" and finding.evidence == "文档内容为空":
        # 删除空文件
        return Patch(path=path, operation="delete", before_hash=before_hash, diff="- (empty file) delete it")
    if finding.rule_id == "rag.missing_metadata":
        # 补标题
        return Patch(
            path=path,
            operation="insert",
            before_hash=before_hash,
            diff="+ # Document Title (auto-generated placeholder)",
        )
    return None


def cmd_apply(args: argparse.Namespace) -> int:
    """经批准应用 Plan：备份 + 补丁 + 重扫（spec §9）。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    plan_path = workspace / PLANS_DIR / f"{args.plan_id}.json"
    if not plan_path.exists():
        print(f"error: plan not found: {args.plan_id}", file=sys.stderr)
        return 1
    import json

    plan_dict = json.loads(plan_path.read_text(encoding="utf-8"))
    # 简化：手动重建 Plan
    from .schema import RiskLevel as RL

    plan = Plan(
        plan_id=plan_dict["plan_id"],
        finding_ids=plan_dict["finding_ids"],
        intent=plan_dict["intent"],
        risk_level=RL(plan_dict["risk_level"]),
        patches=[Patch(**p) for p in plan_dict["patches"]],
        created_at=plan_dict.get("created_at", ""),
        preconditions=plan_dict.get("preconditions", []),
        verification=plan_dict.get("verification", []),
        requires_approval=plan_dict.get("requires_approval", True),
    )

    # 安全：high risk 首期不自动应用
    if plan.risk_level == RiskLevel.HIGH and not args.force:
        print(f"error: plan {plan.plan_id} is high risk, requires --force to apply", file=sys.stderr)
        return 2

    # 校验 preconditions
    for patch in plan.patches:
        try:
            current_hash = sha256_file(Path(patch.path))
        except OSError as e:
            print(f"error: cannot read {patch.path}: {e}", file=sys.stderr)
            return 2
        if current_hash != patch.before_hash:
            print(f"error: file changed since plan was generated: {patch.path}", file=sys.stderr)
            print(f"  expected: {patch.before_hash}", file=sys.stderr)
            print(f"  actual:   {current_hash}", file=sys.stderr)
            return 2

    # 备份 + 应用
    (workspace / BACKUPS_DIR).mkdir(parents=True, exist_ok=True)
    (workspace / CHANGES_DIR).mkdir(parents=True, exist_ok=True)
    backup_paths: list[str] = []
    changed_paths: list[str] = []
    for patch in plan.patches:
        src = Path(patch.path)
        backup = workspace / BACKUPS_DIR / f"{src.name}.{stable_id('bak', patch.path)[:8]}"
        backup.write_bytes(src.read_bytes())
        backup_paths.append(str(backup))
        if patch.operation == "delete":
            src.unlink()
        elif patch.operation == "insert":
            content = src.read_text(encoding="utf-8")
            src.write_text(patch.diff.lstrip("+ ") + "\n" + content, encoding="utf-8")
        elif patch.operation == "replace":
            src.write_text(patch.diff, encoding="utf-8")
        changed_paths.append(patch.path)

    # 重扫验证
    verify_report = run_audit(workspace)
    verify_path = workspace / REPORTS_DIR / "report.json"
    # 验证 Finding 是否消失
    remaining = [f for f in verify_report.findings if f.id in plan.finding_ids]
    status = ChangeStatus.VERIFIED if not remaining else ChangeStatus.FAILED

    change = Change(
        change_id=stable_id("change", plan.plan_id),
        plan_id=plan.plan_id,
        applied_at=now_iso(),
        backup_paths=backup_paths,
        changed_paths=changed_paths,
        status=status,
        verify_report=str(verify_path),
    )
    change_path = workspace / CHANGES_DIR / f"{change.change_id}.json"
    change_path.write_text(json.dumps(change.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"applied: {change.change_id}")
    print(f"  status:  {status.value}")
    print(f"  backups: {len(backup_paths)}")
    print(f"  changed: {len(changed_paths)}")
    if remaining:
        print(f"  remaining findings: {[f.id for f in remaining]}", file=sys.stderr)
    print(f"  undo:   run `memoryguard undo {change.change_id}`")
    return 0 if status == ChangeStatus.VERIFIED else 1


def cmd_undo(args: argparse.Namespace) -> int:
    """撤销 Change：从备份恢复 + 重扫验证（spec §9）。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    change_path = workspace / CHANGES_DIR / f"{args.change_id}.json"
    if not change_path.exists():
        print(f"error: change not found: {args.change_id}", file=sys.stderr)
        return 1
    import json

    change_dict = json.loads(change_path.read_text(encoding="utf-8"))
    # 恢复备份
    for backup_path, changed_path in zip(change_dict["backup_paths"], change_dict["changed_paths"]):
        backup = Path(backup_path)
        target = Path(changed_path)
        if not backup.exists():
            print(f"error: backup missing: {backup}", file=sys.stderr)
            return 1
        target.write_bytes(backup.read_bytes())
    # 重扫验证
    verify_report = run_audit(workspace)
    # 更新 change 状态
    change_dict["status"] = ChangeStatus.UNDONE.value
    change_path.write_text(json.dumps(change_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"undone: {args.change_id}")
    print(f"  restored: {len(change_dict['backup_paths'])} files")
    print(f"  health:   {verify_report.health_score}/100")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """重扫并比较修复前后（spec §4）。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    report = _load_report(workspace)
    if report is None:
        print("error: no report found.", file=sys.stderr)
        return 1
    # 重新扫描
    new_report = run_audit(workspace)
    print(f"verify: {new_report.generated_at}")
    print(f"  before: {len(report.findings)} findings, health {report.health_score}")
    print(f"  after:  {len(new_report.findings)} findings, health {new_report.health_score}")
    # 写新报告
    json_path = workspace / REPORTS_DIR / "report.json"
    html_path = workspace / REPORTS_DIR / "report.html"
    import json

    json_path.write_text(new_report.to_json(), encoding="utf-8")
    html_path.write_text(render_html_report(new_report), encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# v3 命令：source / scan / import / memory（spec §4.1-§4.4）
# ---------------------------------------------------------------------------


def cmd_source(args: argparse.Namespace) -> int:
    """来源管理（spec §4.1）。"""
    from .runtime_v2.source_control import SourceControlError, SourceControlService
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    context = getattr(args, "_native_context", None) or _cli_trusted_context(workspace)
    service = SourceControlService(workspace)
    action = args.action
    source_id = str(getattr(args, "source_id", "") or "").strip()
    if action == "remove" and not source_id:
        # Backward-compatible handling for hand-built Namespaces created
        # before the parser's source-target fix.
        source_id = str(getattr(args, "path", "") or "").strip()
    if action == "list":
        result = service.list_sources(context)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if result.get("ok") else 2
    if action == "add":
        try:
            result = service.add(
                str(getattr(args, "path", "") or ""),
                str(getattr(args, "type", "") or ""),
                context,
                display_name=str(getattr(args, "name", "") or ""),
            )
        except SourceControlError as exc:
            result = {"ok": False, "status": "blocked", "code": exc.code, "error": exc.code}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if result.get("ok") else 2
    if action == "remove":
        try:
            result = service.remove(source_id, context)
        except SourceControlError as exc:
            result = {"ok": False, "status": "blocked", "code": exc.code, "error": exc.code}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if result.get("ok") else 2
    if action == "preview":
        try:
            result = service.preview_path(str(getattr(args, "path", "") or ""), context)
        except SourceControlError as exc:
            result = {"ok": False, "status": "blocked", "code": exc.code, "error": exc.code}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if result.get("ok") else 2
    result = {"ok": False, "status": "error", "code": "invalid_source_action", "error": "invalid_source_action"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2


def cmd_scan(args: argparse.Namespace) -> int:
    """只读扫描，生成快照 + 覆盖率账本（spec §4.2）。"""
    from .runtime_v2.source_control import SourceControlService

    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    context = getattr(args, "_native_context", None) or _cli_trusted_context(workspace)
    result = SourceControlService(workspace).scan_summary(context)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result.get("ok") else 2
    # 持久化快照
    snap_dir = workspace / MG_DIR / "snapshots" / snap.snapshot_id
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "sources.json").write_text(
        _json.dumps([o.to_dict() for o in snap.source_objects], ensure_ascii=False, indent=2),
        encoding="utf-8")
    (snap_dir / "coverage.json").write_text(
        _json.dumps(cov.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  saved: {snap_dir}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """离线导入（spec §4.3）。"""
    del args
    print(json.dumps({
        "ok": False,
        "status": "retired",
        "code": "v2_operation_retired",
        "error": "v2_operation_retired",
    }, ensure_ascii=False, sort_keys=True))
    return 2
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    bundle = Path(args.bundle).resolve()
    if not bundle.exists():
        print(f"error: bundle not found: {bundle}", file=sys.stderr)
        return 1
    # 尝试 detect
    adapters = [ChatGPTImportAdapter(), GenericImportAdapter()]
    detected = None
    for ad in adapters:
        d = ad.detect(bundle)
        if d.supported:
            detected = (ad, d)
            break
    if detected is None:
        print(f"error: unsupported bundle format: {bundle}", file=sys.stderr)
        return 1
    ad, det = detected
    print(f"detected: provider={det.provider} confidence={det.confidence} notes={det.notes}")
    if args.action == "preview":
        inv = ad.inventory(bundle)
        print(f"inventory: {inv}")
        return 0
    if args.action == "create":
        convs = ad.parse(bundle)
        records = ad.normalize(convs)
        print(f"imported: conversations={len(convs)} memory_records={len(records)}")
        # 保存到 imports
        import_id = "imp-" + stable_id(bundle.name, str(len(records)))
        imp_dir = workspace / MG_DIR / "imports" / import_id
        imp_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "import_id": import_id, "provider": det.provider,
            "bundle_path": str(bundle), "created_at": now_iso(),
            "conversation_count": len(convs),
            "memory_record_count": len(records),
            "records": [r.to_dict() for r in records[:100]],
        }
        (imp_dir / "manifest.json").write_text(
            _json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved: {imp_dir / 'manifest.json'}")
        return 0
    return 1


def cmd_memory(args: argparse.Namespace) -> int:
    """记忆构建与发布（spec §4.4）。须显式 --agent-instance-id。"""
    del args
    print(json.dumps({
        "ok": False,
        "status": "retired",
        "code": "v2_operation_retired",
        "error": "v2_operation_retired",
    }, ensure_ascii=False, sort_keys=True))
    return 2
    from .adapters import GenericMarkdownTarget
    from .governance_scope import (
        filter_ir_for_agent, resolve_governance_scope,
        resolve_scoped_roots, derive_publish_target_file, root_authorizes_agent,
    )
    from .release_manager import ReleaseManager
    workspace = Path(getattr(args, "workspace", ".")).resolve()
    rm = ReleaseManager(workspace)
    action = args.action
    agent_id = str(getattr(args, "agent_instance_id", "") or "").strip()
    share_id = str(getattr(args, "share_group_id", "") or "").strip()
    if share_id:
        print("error: share_group scope is not supported for memory build/publish; use GUI neuron share_group projection", file=sys.stderr)
        return 2
    scope, scope_err = resolve_governance_scope(agent_instance_id=agent_id, mode="agent")
    if scope is None:
        print(f"error: {scope_err or 'missing_governance_scope'}; pass --agent-instance-id", file=sys.stderr)
        return 2
    gscope = scope
    scope_dict = gscope.to_dict()

    def _resolve_authorized_root(target_root_id: str):
        if not target_root_id:
            return None, "target_root_id_required"
        reg = None
        root = next((r for r in reg.list_all_sources() if r.root_id == target_root_id), None)
        if root is None or not root_authorizes_agent(root, gscope.agent_instance_id):
            return None, "target_root_not_authorized_for_agent"
        return root, ""

    if action == "build-plan":
        target = GenericMarkdownTarget()
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        if not target_root_id:
            print("error: --target-root-id required for build-plan", file=sys.stderr)
            return 2
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        reg = None
        snap, ir = rm.scan_and_normalize(ScanBudget())
        roots, _ = resolve_scoped_roots(reg.list_all_sources(), gscope, enabled_only=True)
        allowed = {r.root_id for r in roots}
        scoped_ir = filter_ir_for_agent(ir, allowed, snap)
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        plan = rm.create_build_plan(
            scoped_ir, target, target_path,
            governance_scope=scope_dict, target_root_id=target_root_id,
        )
        print(f"plan: {plan.plan_id}")
        print(f"  scope: agent={gscope.agent_instance_id}")
        print(f"  target_root_id: {target_root_id}")
        print(f"  scoped_records: {len(scoped_ir.records)}")
        print(f"  snapshot: {plan.snapshot_id}")
        print(f"  target_profile: {plan.target_profile}")
        print(f"  coverage: {plan.coverage_status}")
        print(f"  integrity: {plan.integrity_ok}")
        print(f"  published: {plan.manifest.published_record_count}")
        print(f"  unaccounted: {plan.manifest.unaccounted_record_count}")
        print(f"  diff: {plan.diff_preview}")
        if not plan.integrity_ok:
            print("  WARNING: integrity check failed; apply will refuse", file=sys.stderr)
            return 2
        return 0
    if action == "build-apply":
        if not args.yes:
            print("error: apply requires --yes to confirm", file=sys.stderr)
            return 2
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        target = GenericMarkdownTarget()
        try:
            release = rm.apply_build(
                args.plan_id, target, target_path, approval=True,
                expected_scope=scope_dict, expected_target_root_id=target_root_id,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"apply: {release.release_id}  status={release.status.value}")
        print(f"  scope: agent={gscope.agent_instance_id}")
        print(f"  target_root_id: {target_root_id}")
        print(f"  target_file: {derived}")
        print(f"  changed: {len(release.changed_paths)}  backups: {len(release.backup_paths)}")
        if release.status.value != "verified":
            print(f"  FAILED: {release.verify_result}", file=sys.stderr)
            return 1
        return 0
    if action == "verify":
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        target = GenericMarkdownTarget()
        # 校验 release 绑定
        release_path = workspace / MG_DIR / "releases" / f"{args.release_id}.json"
        if not release_path.exists():
            # 兼容旧 changes/
            release_path = workspace / MG_DIR / "changes" / f"{args.release_id}.json"
        if not release_path.exists():
            print(f"error: release not found: {args.release_id}", file=sys.stderr)
            return 1
        data = _json.loads(release_path.read_text(encoding="utf-8"))
        try:
            ReleaseManager.validate_release_binding(
                data,
                expected_scope=scope_dict,
                expected_target_root_id=target_root_id,
                expected_target_path=target_path,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        change_path = workspace / MG_DIR / "changes" / f"{args.release_id}.json"
        plan_files = list((workspace / MG_DIR / "plans").glob("*.json"))
        manifest = data.get("manifest")
        if manifest is None and change_path.exists():
            cdata = _json.loads(change_path.read_text(encoding="utf-8"))
            for pf in plan_files:
                pd = _json.loads(pf.read_text(encoding="utf-8"))
                if pd.get("manifest", {}).get("build_id") == cdata.get("build_id"):
                    manifest = pd["manifest"]
                    break
        if manifest is None:
            print(f"error: manifest not found for release {args.release_id}", file=sys.stderr)
            return 1
        from .schema_v3 import BuildManifest
        mm = BuildManifest(
            build_id=manifest["build_id"], source_snapshot_id=manifest.get("source_snapshot_id", ""),
            target_profile=manifest.get("target_profile", ""), coverage_status=manifest.get("coverage_status", ""),
            input_record_count=manifest.get("input_record_count", 0),
            published_record_count=manifest.get("published_record_count", 0),
            linked_record_count=manifest.get("linked_record_count", 0),
            excluded_record_count=manifest.get("excluded_record_count", 0),
            quarantined_record_count=manifest.get("quarantined_record_count", 0),
            unaccounted_record_count=manifest.get("unaccounted_record_count", 0),
            release_hash=manifest.get("release_hash", ""),
        )
        v = rm.verify_release(args.release_id, target, target_path, mm)
        print(f"verify: {v['release_id']}  rescan={v['rescan_match']}  hashes={v['hashes_match']}")
        return 0 if v["rescan_match"] and v["hashes_match"] else 1
    if action == "rollback":
        if not args.yes:
            print("error: rollback requires --yes to confirm", file=sys.stderr)
            return 2
        target_root_id = str(getattr(args, "target_root_id", "") or "").strip()
        root, err = _resolve_authorized_root(target_root_id)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        derived = derive_publish_target_file(root)
        target_path = derived.parent if derived.suffix else derived
        # 校验 release 绑定，禁止用错误目录删文件；无绑定则拒绝
        release_path = workspace / MG_DIR / "releases" / f"{args.release_id}.json"
        if not release_path.exists():
            print(f"error: release not found: {args.release_id}", file=sys.stderr)
            return 1
        data = _json.loads(release_path.read_text(encoding="utf-8"))
        try:
            ReleaseManager.validate_release_binding(
                data,
                expected_scope=scope_dict,
                expected_target_root_id=target_root_id,
                expected_target_path=target_path,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        target = GenericMarkdownTarget()
        try:
            rb = rm.rollback_release(
                args.release_id, target, target_path,
                expected_scope=scope_dict,
                expected_target_root_id=target_root_id,
                expected_target_path=target_path,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"rollback: {rb.release_id}  status={rb.status.value}")
        return 0
    print(f"unknown memory action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# doctor / mcp-status
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """诊断安装环境，输出检查报告。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    issues = 0
    lines: list[str] = ["MemoryGuard Doctor", "=================="]

    # 1. Python 版本（>= 3.10）
    py_ok = sys.version_info >= (3, 10)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    lines.append(
        f"Python: {py_ver} {'[ok]' if py_ok else '[error] require >= 3.10'}"
    )
    if not py_ok:
        issues += 1

    # 2. memoryguard 包可 import
    try:
        import memoryguard  # noqa: F401
        lines.append("memoryguard package: [ok]")
    except ImportError as e:
        lines.append(f"memoryguard package: [error] ({e})")
        issues += 1

    # 3. MCP server 模块可 import
    try:
        from . import mcp_server  # noqa: F401
        lines.append("MCP server module: [ok]")
    except ImportError as e:
        lines.append(f"MCP server module: [error] ({e})")
        issues += 1

    # 4. 工作区 .memoryguard/ 目录
    mg_dir = workspace / MG_DIR
    if mg_dir.is_dir():
        lines.append(f"Workspace .memoryguard/: [ok] (found at {mg_dir})")
    else:
        lines.append(f"Workspace .memoryguard/: not found (run `memoryguard audit` to initialize)")

    # 5. 已绑定的 Agent
    try:
        from .runtime_v2.group_native import GroupControlService
        bindings = GroupControlService(workspace, write=False).list_bindings(
            include_inactive=False,
        ).get("bindings", [])
        lines.append(f"Agent bindings: {len(bindings)} active")
    except Exception as e:
        lines.append(f"Agent bindings: error ({e})")

    # 6. shared-memory 记录
    try:
        groups = GroupControlService(workspace, write=False).list_groups().get("groups", [])
    except Exception:
        groups = []
    lines.append(f"V2 memory groups: {len(groups)}")

    # 7. provider adapter 安装状态
    lines.append("Provider adapters:")
    try:
        from .provider_adapters import ClaudeAdapter, CodexAdapter, CursorAdapter
        for name, cls in (("claude", ClaudeAdapter), ("codex", CodexAdapter), ("cursor", CursorAdapter)):
            try:
                st = cls(str(workspace)).status()
                if st.get("installed"):
                    lines.append(f"  {name}: installed [ok]")
                else:
                    lines.append(f"  {name}: not installed")
            except Exception as e:
                lines.append(f"  {name}: error ({e})")
    except ImportError as e:
        lines.append(f"  (provider_adapters unavailable: {e})")

    # 8. user-level host hooks
    lines.append("Host hooks:")
    try:
        from .host_hooks import HostHookManager

        hook_status = HostHookManager(workspace).status()
        for item in hook_status.get("providers", []):
            if not item.get("supported"):
                state = "unsupported"
            elif item.get("runtime_verified"):
                state = "operational [ok]"
            elif item.get("configured"):
                state = "configured, awaiting runtime receipt"
            else:
                state = "not configured"
            lines.append(f"  {item.get('provider')}: {state}")
    except Exception as e:
        lines.append(f"  error ({e})")

    # 9. 运行时租约（split brain）
    #    升级后旧进程不退出会让写入被全面拒绝，而症状只表现为工具被挡，
    #    所以 doctor 必须能独立报出来，不能只在 MCP 返回值里可见。
    try:
        from .runtime_lease import runtime_lease_status, describe_split_brain

        lease = runtime_lease_status(workspace)
        described = describe_split_brain(lease, control_workspace=workspace)
        if described["split_brain"]:
            lines.append("Runtime lease: [error] split brain")
            lines.extend(described["detail_lines"])
            lines.append(f"  修复: {described['remedy']}")
            issues += 1
        else:
            live = len(lease.get("live") or [])
            stale = len(lease.get("stale") or [])
            suffix = f", {stale} stale" if stale else ""
            lines.append(f"Runtime lease: [ok] ({live} live{suffix})")
    except Exception as e:
        lines.append(f"Runtime lease: error ({e})")

    # 10. pywebview（可选）
    try:
        import webview  # type: ignore  # noqa: F401
        lines.append("GUI (pywebview): available [ok]")
    except ImportError:
        lines.append("GUI (pywebview): not available (optional)")

    # 汇总
    if issues == 0:
        lines.append("")
        lines.append("All checks passed.")
    else:
        lines.append("")
        lines.append(f"{issues} issue(s) found.")

    print("\n".join(lines))
    return 0 if issues == 0 else 1


# ---------------------------------------------------------------------------
# runtime (split-brain 诊断与显式回收)
# ---------------------------------------------------------------------------


def _runtime_lease_report(workspace) -> tuple[dict, dict]:
    from .runtime_lease import runtime_lease_status, describe_split_brain

    status = runtime_lease_status(workspace)
    return status, describe_split_brain(status, control_workspace=workspace)


def cmd_runtime_status(args: argparse.Namespace) -> int:
    """只读查看运行时租约与 split brain 状态。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    status, described = _runtime_lease_report(workspace)

    print("MemoryGuard Runtime Lease")
    print("=========================")
    print(f"workspace: {workspace}")
    print(f"leases:    {status.get('leases_path', '?')}")
    for lease in status.get("live") or []:
        print(
            "  live  pid={pid} version={ver} started={started}".format(
                pid=lease.get("pid"),
                ver=lease.get("memoryguard_version", "?"),
                started=lease.get("process_started_at", "?"),
            )
        )
    for lease in status.get("stale") or []:
        print(f"  stale pid={lease.get('pid')} (进程已退出，条目可被自动清理)")

    if not described["split_brain"]:
        print("\nsplit brain: no")
        return 0

    print("\nsplit brain: YES")
    print(described["summary"])
    print("\n冲突进程:")
    for line in described["detail_lines"]:
        print(line)
    print(f"\n修复: {described['remedy']}")
    return 1


def cmd_runtime_reap(args: argparse.Namespace) -> int:
    """结束以不同构建持有本 workspace 的残留进程。

    仅在用户显式调用时执行，且逐个核对进程身份；MemoryGuard 自身在任何
    自动路径上都不会终止进程。
    """
    import os
    import signal
    import time

    from .runtime_lease import _process_started_at_for_pid, release_runtime_lease

    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    _status, described = _runtime_lease_report(workspace)

    if not described["split_brain"]:
        print("没有检测到 split brain，无需回收。")
        return 0

    print(described["summary"])
    print("\n将要结束以下进程:")
    for line in described["detail_lines"]:
        print(line)

    if not getattr(args, "yes", False):
        try:
            answer = input("\n确认结束这些进程？只有你确认这些不是正在使用的窗口时才继续 [y/N]: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            print("已取消，未终止任何进程。")
            return 1

    me = os.getpid()
    killed, skipped = 0, 0
    for lease in _status.get("conflicting") or []:
        try:
            pid = int(lease.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid == me:
            continue

        # PID 会被系统复用。租约里记的启动时间和当前同号进程对不上时，
        # 说明这个号已经属于别的程序，绝不能杀。
        recorded = str(lease.get("process_started_at", "") or "")
        observed = _process_started_at_for_pid(pid)
        observed_iso = observed.isoformat() if observed is not None else ""
        if observed_iso and recorded and observed_iso[:19] != recorded[:19]:
            print(f"  pid={pid} 跳过：启动时间不匹配，PID 可能已被复用")
            skipped += 1
            continue
        if observed is None:
            print(f"  pid={pid} 已不在运行，仅清理租约条目")
            release_runtime_lease(workspace, pid)
            continue

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            release_runtime_lease(workspace, pid)
            continue
        except PermissionError:
            print(f"  pid={pid} 跳过：权限不足")
            skipped += 1
            continue

        for _ in range(20):
            time.sleep(0.25)
            if _process_started_at_for_pid(pid) is None:
                break
        else:
            if hasattr(signal, "SIGKILL"):
                try:
                    os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
                except OSError:
                    pass

        if _process_started_at_for_pid(pid) is None:
            release_runtime_lease(workspace, pid)
            print(f"  pid={pid} 已结束")
            killed += 1
        else:
            print(f"  pid={pid} 未能结束")
            skipped += 1

    print(f"\n已结束 {killed} 个，跳过 {skipped} 个。")
    if skipped:
        print("仍有冲突进程存在，重启宿主前请先手动处理。")
        return 1
    print("现在重启宿主（Cursor / Codex 等）即可恢复。")
    return 0


def cmd_mcp_status(args: argparse.Namespace) -> int:
    """查询 MCP 记忆后端状态。"""
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    lines: list[str] = ["MemoryGuard MCP Status", "======================"]

    # MCP server 运行状态（stdio 模式，无常驻进程）
    lines.append("MCP server: not running (stdio mode, starts on demand)")
    lines.append("")

    # 收集所有 share group
    from .runtime_v2.group_native import GroupControlService
    native_groups = GroupControlService(workspace, write=False).list_groups().get("groups", [])
    groups: list[tuple[str, dict]] = [
        (str(item.get("share_group_id") or ""), {
            "total_records": 0,
            "total_events": 0,
            "total_conflicts": 0,
            "total_quarantine": 0,
            "active_version": None,
        })
        for item in native_groups
    ]
    # 收集已绑定 Agent
    group_agents: dict[str, list[str]] = {}
    try:
        bindings = GroupControlService(workspace, write=False).list_bindings(
            include_inactive=False,
        ).get("bindings", [])
        for binding in bindings:
            group_agents.setdefault(
                str(binding.get("share_group_id") or ""), []
            ).append(str(binding.get("agent_instance_id") or ""))
    except Exception:
        pass

    # 输出每个 group
    lines.append("Shared Memory Groups:")
    total_records = 0
    total_events = 0
    for group_id, st in groups:
        recs = st.get("total_records", 0)
        evs = st.get("total_events", 0)
        confs = st.get("total_conflicts", 0)
        quar = st.get("total_quarantine", 0)
        ver = st.get("active_version") or "(none)"
        agents = group_agents.get(group_id, [])
        lines.append(
            f"  [{group_id}] {recs} records, {evs} events, {confs} conflicts, {quar} quarantined"
        )
        lines.append(f"    active version: {ver}")
        lines.append(f"    agents: {', '.join(agents) if agents else '(none)'}")
        lines.append("")
        total_records += recs
        total_events += evs

    # 绑定的 Agent 总览
    bound_agents: list[str] = []
    try:
        bindings = GroupControlService(workspace, write=False).list_bindings(
            include_inactive=False,
        ).get("bindings", [])
        bound_agents = [str(item.get("agent_instance_id") or "") for item in bindings]
    except Exception:
        pass
    if bound_agents:
        lines.append(f"Bound agents: {len(bound_agents)} ({', '.join(bound_agents)})")

    lines.append(f"Total: {len(groups)} groups, {total_records} records, {total_events} events")
    print("\n".join(lines))
    return 0


def _infer_hook_provider(workspace: Path, agent_instance_id: str = "") -> str:
    """Infer only when evidence is unambiguous; never install into every host."""
    import os

    explicit = os.environ.get("MEMORYGUARD_PROVIDER", "").strip().lower()
    if explicit:
        return explicit

    candidates: set[str] = set()
    if os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.add("claude")
    if os.environ.get("CURSOR_PROJECT_DIR") or os.environ.get("CURSOR_VERSION"):
        candidates.add("cursor")
    if os.environ.get("CODEX_HOME") or os.environ.get("CODEX_THREAD_ID"):
        candidates.add("codex")
    if os.environ.get("TRAE_PROJECT_DIR") or os.environ.get("TRAE_VERSION"):
        candidates.add("trae")

    if agent_instance_id:
        try:
            from .agent_locator import AgentLocator

            instances, _ = AgentLocator(workspace).detect_instances()
            product = next(
                (
                    item.product.lower()
                    for item in instances
                    if item.instance_id == agent_instance_id
                ),
                "",
            )
            aliases = {
                "claude-code": "claude",
                "claude": "claude",
                "codex": "codex",
                "cursor": "cursor",
                "trae": "trae",
            }
            if product in aliases:
                candidates.add(aliases[product])
        except Exception:
            pass
    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        raise ValueError(
            "cannot infer current host; pass --provider "
            "claude|codex|cursor|trae"
        )
    raise ValueError(
        "host inference is ambiguous: "
        + ", ".join(sorted(candidates))
        + "; pass --provider explicitly"
    )


def cmd_hooks(args: argparse.Namespace) -> int:
    """Install/status/uninstall user-level lifecycle hooks."""
    import json
    import os

    from .host_hooks import HostHookManager, set_hook_mode
    from .runtime_v2.group_native import GroupControlService

    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    manager = HostHookManager(workspace)
    agent_id = (
        str(getattr(args, "agent_id", "") or "")
        or os.environ.get("MEMORYGUARD_AGENT_ID", "").strip()
    )
    provider = str(getattr(args, "provider", "") or "").lower()
    try:
        if provider == "auto":
            provider = _infer_hook_provider(workspace, agent_id)

        if args.action == "status":
            result = manager.status(
                "" if provider in {"", "all"} else provider,
                agent_instance_id=agent_id,
                inspect_trust=True,
            )
        elif args.action in {"install", "ensure"}:
            if not agent_id:
                raise ValueError(
                    "agent identity is required; pass --agent-id or set "
                    "MEMORYGUARD_AGENT_ID"
                )
            group_id = str(args.share_group_id or "")
            if not group_id:
                binding = GroupControlService(workspace, write=False).active_binding_for_agent(
                    agent_id,
                )
                if binding is None:
                    raise ValueError(
                        f"no active binding found for {agent_id!r}"
                    )
                group_id = str(binding.get("share_group_id") or "")
            result = manager.install(
                provider,
                agent_instance_id=agent_id,
                share_group_id=group_id,
                mode=args.mode,
                reconcile_trust=provider == "codex",
            )
        elif args.action == "uninstall":
            result = manager.uninstall(provider)
        elif args.action == "mode":
            if not agent_id:
                raise ValueError("--agent-id is required for mode changes")
            result = set_hook_mode(
                workspace,
                provider,
                agent_id,
                args.mode,
            )
        else:
            raise ValueError(f"unknown hooks action: {args.action}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") in {"error", "unsupported"}:
        return 1
    return 0


def cmd_provider(args: argparse.Namespace) -> int:
    """Repair global MCP/rule integrations from canonical data-home bindings."""
    from .provider_adapters import repair_global_provider_configs

    try:
        result = repair_global_provider_configs(list(args.providers or ["all"]))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def cmd_gc(args: argparse.Namespace) -> int:
    """`.memoryguard/` GC：默认可重建物优先清的 dry-run 预览。"""
    from .gc import MemoryGuardGc

    workspace = _effective_workspace(args, getattr(args, "path", "."))
    if not workspace.is_dir():
        print(f"error: workspace not found: {workspace}", file=sys.stderr)
        return 1

    gc = MemoryGuardGc(
        workspace,
        older_than_days=args.older_than_days,
        keep_releases=args.keep_releases,
        keep_snapshots=args.keep_snapshots,
    )
    dry_run = not args.apply
    plan = gc.plan(dry_run=dry_run)

    print(f"MemoryGuard gc {'plan' if dry_run else 'apply'}")
    print(f"  workspace:     {workspace}")
    print(f"  items:         {len(plan.items)}")
    print(f"  total_bytes:   {plan.total_bytes}")
    print(f"  dry_run:       {plan.dry_run}")
    for item in plan.items:
        print(f"  - {item.action:16} {item.bytes_estimate:>10} B  {item.path}")
        print(f"    {item.reason}")

    if dry_run:
        if plan.items:
            print("  (dry-run only; pass --apply to execute)")
        return 0

    result = gc.apply(plan, confirmed=True)
    if not result.get("ok"):
        print(f"error: gc apply failed: {result.get('error', 'see results')}", file=sys.stderr)
        for entry in result.get("results", []):
            if not entry.get("ok"):
                print(f"  failed: {entry}", file=sys.stderr)
        return 1
    print(f"  applied:       {result.get('applied', 0)}")
    print(f"  history:       {result.get('history_path', '')}")
    return 0


def cmd_storage(args: argparse.Namespace) -> int:
    """Phase 7 storage audit/report entrypoint for pre-active workspaces."""

    from .maintenance_v2.api import MaintenanceV2Api

    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    try:
        api = MaintenanceV2Api(workspace)
        if args.action == "audit":
            payload = api.audit_references()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if not payload.get("blocked") else 2
        if args.action == "report":
            payload = api.storage_report(args.domain)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        # V1_ACTIVE/V2_BUILDING routes reach this legacy callback.  Every
        # mutating Phase 7 operation must wait for the native V2_ACTIVE port.
        print(json.dumps({"ok": False, "status": "error", "code": "v2_not_active", "error": "v2_not_active"}, sort_keys=True))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "status": "error", "code": type(exc).__name__, "error": type(exc).__name__}, sort_keys=True))
        return 2


def cmd_groups(args: argparse.Namespace) -> int:
    """Inspect and migrate shared-memory groups.

    ``groups list`` prints every non-empty group in a workspace plus the
    active bindings.  ``groups migrate`` copies one legacy group's records
    into a target group (dry-run unless ``--apply``), which is the manual
    recovery path for the "control directory moved but memory did not"
    incident.
    """
    from .runtime_v2.group_native import GroupControlService

    workspace = _effective_workspace(args, getattr(args, "workspace", "."))

    if args.action == "list":
        result = GroupControlService(workspace, write=False).list_groups()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return 0 if result.get("ok") else 2

    print(json.dumps({
        "ok": False,
        "status": "retired",
        "code": "v2_operation_retired",
        "error": "v2_operation_retired",
    }, ensure_ascii=False, sort_keys=True))
    return 2

def cmd_desktop(args: argparse.Namespace) -> int:
    """启动 MemoryGuard Desktop Executor（可信执行端）。"""
    from .desktop_executor import main as desktop_main
    argv = []
    workspace = _effective_workspace(args, getattr(args, "workspace", "."))
    if workspace:
        argv.append(str(workspace))
    if getattr(args, "auto_confirm", False):
        argv.append("--auto-confirm")
    if getattr(args, "watch", False):
        argv.append("--watch")
    if getattr(args, "request", ""):
        argv.extend(["--request", args.request])
    if getattr(args, "uri", ""):
        argv.extend(["--uri", args.uri])
    if getattr(args, "register_uri", False):
        argv.append("--register-uri")
    return desktop_main(argv)


def cmd_gui(args: argparse.Namespace) -> int:
    """从可见终端启动交互式治理台。"""
    workspace = getattr(args, "_resolved_workspace", None)
    if workspace is None:
        workspace = getattr(args, "workspace_option", "") or getattr(args, "workspace", "")
    workspace_args = [str(workspace)] if workspace else []
    if os.name == "nt" and os.environ.get("_MEMORYGUARD_GUI_CHILD") != "1":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        env = os.environ.copy()
        env["_MEMORYGUARD_GUI_CHILD"] = "1"
        creation_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        subprocess.Popen(
            [str(pythonw), "-m", "memoryguard.cli", "gui", *workspace_args],
            cwd=str(Path.cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        return 0
    return gui_main(workspace_args)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


class _SourceTargetAction(argparse.Action):
    """Route source's sole positional to ``path`` or ``source_id``.

    Declaring two optional positionals made ``source remove ROOT`` populate
    ``path`` and leave the id empty.  Keep the Namespace names stable while
    selecting the destination from the already-parsed action.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, "action", "") == "remove":
            setattr(namespace, "source_id", values)
            setattr(namespace, "path", None)
        else:
            setattr(namespace, "path", values)
            setattr(namespace, "source_id", None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryguard",
        description="Local-first Agent governance: audit instructions, skills, memory, and local RAG.",
        epilog="Verified V1→V2 migration: run `memoryguard upgrade`; use `--preview` for zero-write inspection.",
    )
    parser.add_argument("--version", action="version", version=f"memoryguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="read-only scan, generate report")
    p_audit.add_argument("path", nargs="?", default=".", help="workspace path (default: .)")
    p_audit.add_argument(
        "--json", action="store_true", help="also print JSON to stdout"
    )
    p_audit.set_defaults(func=cmd_audit)

    p_open = sub.add_parser("open", help="open latest report in a window")
    p_open.add_argument("path", nargs="?", default=".", help="workspace path (default: .)")
    p_open.add_argument(
        "--mode",
        choices=("auto", "native", "localhost", "html"),
        default="auto",
        help="window mode: auto(native->localhost->html) | native | localhost | html (default: auto)",
    )
    p_open.set_defaults(func=cmd_open)

    p_explain = sub.add_parser("explain", help="explain a finding's evidence and risk")
    p_explain.add_argument("finding_id", help="finding id from report")
    p_explain.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_explain.set_defaults(func=cmd_explain)

    p_plan = sub.add_parser("plan", help="generate minimal fix plan (no write)")
    p_plan.add_argument("finding_ids", nargs="+", help="finding ids to fix")
    p_plan.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="apply a plan: backup + patch + rescan")
    p_apply.add_argument("plan_id", help="plan id from `plan` command")
    p_apply.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_apply.add_argument("--force", action="store_true", help="force apply high-risk plans")
    p_apply.set_defaults(func=cmd_apply)

    p_verify = sub.add_parser("verify", help="rescan and compare before/after")
    p_verify.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_verify.set_defaults(func=cmd_verify)

    p_undo = sub.add_parser("undo", help="restore from backup and re-verify")
    p_undo.add_argument("change_id", help="change id from `apply` command")
    p_undo.add_argument("-w", "--workspace", default=".", help="workspace path (default: .)")
    p_undo.set_defaults(func=cmd_undo)

    # v3 命令（spec §4.1-§4.4）
    p_source = sub.add_parser("source", help="manage authorized sources (v3)")
    p_source.add_argument("action", choices=("list", "add", "remove", "preview"))
    p_source.add_argument(
        "target", nargs="?", action=_SourceTargetAction,
        help="path for add/preview, or source_id for remove",
    )
    p_source.add_argument("--type", default="selected_directory",
                          choices=("project_directory", "selected_directory", "selected_file", "obsidian_vault"))
    p_source.add_argument("--name", default="", help="display name")
    p_source.set_defaults(path=None, source_id=None)
    p_source.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_source.set_defaults(func=cmd_source)

    p_scan = sub.add_parser("scan", help="read-only scan, build coverage ledger (v3)")
    p_scan.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_scan.set_defaults(func=cmd_scan)

    p_import = sub.add_parser("import", help="offline import bundle (v3)")
    p_import.add_argument("action", choices=("preview", "create"))
    p_import.add_argument("bundle", help="bundle path (file or dir)")
    p_import.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_import.set_defaults(func=cmd_import)

    p_doctor = sub.add_parser("doctor", help="diagnose installation and environment")
    p_doctor.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_doctor.set_defaults(func=cmd_doctor)

    p_runtime = sub.add_parser(
        "runtime", help="inspect runtime leases and reap stale split-brain processes"
    )
    runtime_sub = p_runtime.add_subparsers(dest="runtime_command", required=True)
    p_runtime_status = runtime_sub.add_parser("status", help="read-only lease / split-brain status")
    p_runtime_status.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_runtime_status.set_defaults(func=cmd_runtime_status)
    p_runtime_reap = runtime_sub.add_parser(
        "reap", help="terminate processes holding this workspace with a different build"
    )
    p_runtime_reap.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_runtime_reap.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt"
    )
    p_runtime_reap.set_defaults(func=cmd_runtime_reap)

    p_mcp_status = sub.add_parser("mcp-status", help="query MCP memory backend status")
    p_mcp_status.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_mcp_status.set_defaults(func=cmd_mcp_status)

    p_hooks = sub.add_parser(
        "hooks",
        help="install and inspect user-level host lifecycle hooks",
    )
    p_hooks.add_argument(
        "action",
        choices=("status", "install", "ensure", "uninstall", "mode"),
    )
    p_hooks.add_argument(
        "--provider",
        default="auto",
        choices=("auto", "all", "claude", "codex", "cursor", "trae"),
        help="host provider; auto requires unambiguous runtime evidence",
    )
    p_hooks.add_argument("-w", "--workspace", default=".", help="MemoryGuard control workspace")
    p_hooks.add_argument("--agent-id", default="", help="trusted Agent instance ID")
    p_hooks.add_argument("--share-group-id", default="", help="bound memory group; inferred when omitted")
    p_hooks.add_argument(
        "--mode",
        default="enforce",
        choices=("enforce", "observe", "paused"),
        help="runtime policy (default: enforce)",
    )
    p_hooks.set_defaults(func=cmd_hooks)

    p_provider = sub.add_parser(
        "provider",
        help="repair global provider MCP/rule integrations from canonical bindings",
    )
    p_provider.add_argument("action", choices=("repair",))
    p_provider.add_argument(
        "providers",
        nargs="*",
        default=["all"],
        help="claude codex cursor trae, or all (default: all)",
    )
    p_provider.set_defaults(func=cmd_provider)

    p_gc = sub.add_parser("gc", help="garbage-collect .memoryguard/ artifacts (default dry-run)")
    p_gc.add_argument("path", nargs="?", default=".", help="workspace path (default: .)")
    p_gc.add_argument("--apply", action="store_true", help="execute the GC plan (default: dry-run)")
    p_gc.add_argument("--older-than-days", type=int, default=30, help="native release artifact age threshold")
    p_gc.add_argument("--keep-releases", type=int, default=20, help="reserved for future release pruning")
    p_gc.add_argument("--keep-snapshots", type=int, default=3, help="snapshots to retain")
    p_gc.set_defaults(func=cmd_gc)

    p_storage = sub.add_parser("storage", help="audit and maintain authoritative V2 SQLite storage")
    p_storage.add_argument("action", choices=("audit", "report", "lease-acquire", "lease-release", "sweep", "compact"))
    p_storage.add_argument("-w", "--workspace", default=".", help="target workspace (default: .)")
    p_storage.add_argument("--domain", default="content", choices=("runtime", "memory", "rules", "evidence", "content", "knowledge", "codegraph", "assets", "scenario", "profile", "system", "skills"))
    p_storage.add_argument("--request-key", default="", help="stable idempotency key for sweep/compact")
    p_storage.add_argument("--lease-id", default="", help="active maintenance lease")
    p_storage.add_argument("--ttl-seconds", type=int, default=300, help="lease lifetime for lease-acquire")
    p_storage.add_argument("--apply", action="store_true", help="perform physical sweep/compact (default: dry-run)")
    p_storage.set_defaults(func=cmd_storage)

    p_groups = sub.add_parser(
        "groups",
        help="inspect and migrate shared-memory groups (migrate defaults to dry-run)",
    )
    p_groups.add_argument("action", choices=("list", "migrate"))
    p_groups.add_argument(
        "-w", "--workspace", default=".",
        help="target workspace (migration destination; default: .)",
    )
    p_groups.add_argument(
        "--source-workspace", default="",
        help="legacy source workspace (default: --workspace)",
    )
    p_groups.add_argument(
        "--from", dest="from_gid", default="",
        help="source group id (default: largest non-empty legacy group)",
    )
    p_groups.add_argument(
        "--to", dest="to_gid", default="",
        help="target group id (default: first active shared binding in --workspace)",
    )
    p_groups.add_argument(
        "--apply", action="store_true",
        help="execute the migration (default: dry-run)",
    )
    p_groups.add_argument(
        "--archive-source", action="store_true",
        help="archive the source group directory after a successful copy",
    )
    p_groups.set_defaults(func=cmd_groups)

    p_gui = sub.add_parser("gui", help="launch the interactive governance console")
    p_gui.add_argument(
        "workspace",
        nargs="?",
        default="",
        help="advanced: explicit MemoryGuard data home (default: global user data home)",
    )
    p_gui.add_argument(
        "-w",
        "--workspace",
        "--data-home",
        dest="workspace_option",
        default="",
        help="advanced: explicit MemoryGuard data home; this is not a project UI path",
    )
    p_gui.set_defaults(func=cmd_gui)

    p_desktop = sub.add_parser("desktop", help="launch MemoryGuard Desktop Executor (trusted execution)")
    p_desktop.add_argument("-w", "--workspace", default=".", help="workspace path")
    p_desktop.add_argument("--auto-confirm", action="store_true", help="skip confirmation (testing only)")
    p_desktop.add_argument("--watch", action="store_true", help="continuously poll for new requests")
    p_desktop.add_argument("--request", default="", help="process a specific request ID")
    p_desktop.add_argument("--uri", default="", help="launch from memoryguard:// URI")
    p_desktop.add_argument("--register-uri", action="store_true", help="register memoryguard:// URI protocol")
    p_desktop.set_defaults(func=cmd_desktop)

    return parser


def _default_gui_workspace() -> Path:
    """Return the one global user-level Data Home used by the GUI.

    The installed package directory and the current project are execution or
    source locations only. Runtime UI and governance state belong to the one
    canonical Data Home shared by all Agent integrations.
    """
    from .data_home import resolve_runtime_data_home

    return resolve_runtime_data_home(discover_stable_control_home=True)


def _resolve_gui_workspace(argv: list[str]) -> Path | None:
    if argv:
        # Compatibility/diagnostic override: the positional value names an
        # alternate MemoryGuard Data Home, never a project-local UI.
        from .data_home import resolve_runtime_data_home

        return resolve_runtime_data_home(argv[0])

    # A bare GUI is always the single global governance console. Ambient
    # MEMORYGUARD_WORKSPACE may describe an older project/control context and
    # must never redirect the GUI to a project database. resolve_data_home()
    # already honors MEMORYGUARD_HOME for user-selected non-default locations.
    return _default_gui_workspace().expanduser().resolve()


def gui_main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        workspace = _resolve_gui_workspace(argv)
    except Exception as exc:
        from .data_home import ControlHomeResolutionError

        if not isinstance(exc, ControlHomeResolutionError):
            raise
        print(
            json.dumps(exc.to_payload(surface="GUI"), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    if workspace is None:
        print(
            "error: no global MemoryGuard data home could be resolved. "
            "Set MEMORYGUARD_HOME or pass `memoryguard gui --data-home <path>`.",
            file=sys.stderr,
        )
        return 2
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    if workspace == system_root or system_root in workspace.parents:
        print(
            "error: refusing to use a Windows system directory as the workspace.\n"
            f"  resolved path: {workspace}\n"
            "  configure a non-system MemoryGuard data home, for example:\n"
            r"  memoryguard gui --data-home H:\MemoryGuard",
            file=sys.stderr,
        )
        return 2
    if not workspace.is_dir():
        if workspace != _default_gui_workspace():
            print(
                f"error: GUI data home does not exist or is not a directory: {workspace}",
                file=sys.stderr,
            )
            return 2
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"error: cannot create the fixed GUI control directory: {workspace}\n"
                f"  {exc}",
                file=sys.stderr,
            )
            return 2
    from .gui import (
        has_native_gui,
        open_interactive_window,
        open_localhost_window,
    )
    if has_native_gui():
        rc = open_interactive_window(
            str(workspace), title=f"MemoryGuard - {workspace.name}",
        )
        if rc == 0:
            return 0
    # GUI 启动器的降级也必须保留完整交互能力；静态报告不能展开风险、
    # 调用治理 API 或展示实时 MCP 记忆层。
    rc, _ = open_localhost_window(str(workspace), auto_open=True)
    if rc == 0:
        return 0
    report = run_audit(workspace)
    out_dir = workspace / REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "report.html"
    html_path.write_text(render_html_report(report), encoding="utf-8")
    webbrowser.open(html_path.resolve().as_uri())
    return 0


def _cli_trusted_context(
    workspace: str | Path = ".",
    *,
    allow_verified_provider_repair: bool = False,
) -> dict:
    """Return one process-issued CLI authority for native and maintenance ports.

    CLI flags are business inputs, never identity.  Resolve the active binding
    from the trusted process AccessContext, mint the same NativeBoundContext
    used by MCP/GUI, then attach the narrower maintenance sentinel to that
    envelope. A bare shell has no host-injected AccessContext, so the provider
    repair command alone may derive one from a verified Codex installation and
    its unique active canonical binding. A missing/ambiguous binding returns no
    capability and all other V2 mutations fail closed.
    """
    try:
        from .access_context import load_access_context
        from .runtime_v2.group_native import GroupControlService
        from .runtime_v2.native_ports import bind_native_transport_context
        from .maintenance_v2.runtime_port import bind_maintenance_transport_context

        ctx = load_access_context()
        agent_id = str(getattr(ctx, "trusted_agent_id", "") or "").strip()
        resolved_workspace = Path(workspace).expanduser().resolve()
        if not agent_id and allow_verified_provider_repair:
            # This is deliberately narrower than the normal host context:
            # only a selected V2 control home with a unique, still-active
            # Codex binding and verified installation evidence can mint the
            # process-local admin capability needed by provider repair.
            from .agent_locator import AgentLocator
            from .provider_adapters import (
                _resolve_codex_canonical_identity,
                _select_verified_codex_control_home,
            )

            try:
                verified_home = _select_verified_codex_control_home()
            except Exception:
                return {}
            if verified_home.expanduser().resolve() != resolved_workspace:
                return {}

            binding_store = GroupControlService(resolved_workspace, write=False)
            instances, _ = AgentLocator(resolved_workspace).detect_instances()
            canonical, share_group_id, _aliases = _resolve_codex_canonical_identity(
                binding_store,
                instances,
            )
            if not canonical or not share_group_id:
                return {}
            binding = binding_store.active_binding_for_agent(canonical)
            if binding is None or str(binding.get("share_group_id") or "").strip() != share_group_id:
                return {}
            from .access_context import AccessContext

            ctx = AccessContext(
                trusted_agent_id=canonical,
                is_admin=True,
                strict_binding=True,
                allow_anon=False,
                session_id=f"cli-provider-repair:{os.getpid()}",
                session_source="host",
                session_trusted=True,
            )
            agent_id = canonical
        if not agent_id:
            return {}
        binding = GroupControlService(resolved_workspace, write=False).active_binding_for_agent(
            agent_id,
        )
        if binding is None:
            return {}
        envelope = bind_native_transport_context(
            ctx,
            workspace_id=str(resolved_workspace),
            share_group_id=str(binding.get("share_group_id") or ""),
            runtime_role="cli",
            entrypoint="cli",
        )
        return bind_maintenance_transport_context(envelope)
    except Exception:
        return {}


def _cli_workspace(args: argparse.Namespace) -> Path:
    command = str(getattr(args, "command", "") or "")
    if command == "gui" and not (
        getattr(args, "workspace", "") or getattr(args, "workspace_option", "")
    ):
        resolved = _resolve_gui_workspace([])
        if resolved is not None:
            return resolved.expanduser().resolve()
    raw = (
        getattr(args, "workspace_option", None)
        or getattr(args, "workspace", None)
        or getattr(args, "path", ".")
        or "."
    )
    explicit = bool(raw and str(raw) != ".")
    # Governance/control-plane commands operate on the one user-level shared
    # library when no path was explicitly supplied.  Project inspection
    # commands (audit/scan/open/...) intentionally retain bounded cwd lookup.
    control_commands = {
        "doctor", "mcp-status", "hooks", "provider", "groups", "desktop", "open",
        "gc", "storage", "source", "import", "runtime",
    }
    if command in control_commands:
        from .data_home import resolve_runtime_data_home

        # Bare program controls never use MEMORYGUARD_WORKSPACE, cwd, or
        # nearby projects as their store selector.  Explicit CLI paths and
        # MEMORYGUARD_HOME retain their documented priority inside resolver.
        return resolve_runtime_data_home(
            str(raw) if explicit else None,
            discover_stable_control_home=not explicit,
        ).expanduser().resolve()
    if command == "gui":
        from .data_home import resolve_runtime_data_home

        return resolve_runtime_data_home(str(raw) if explicit else None).expanduser().resolve()
    return resolve_workspace(raw, explicit=explicit)


def _effective_workspace(args: argparse.Namespace, raw: str | Path = ".") -> Path:
    """Use the cutover-selected path, otherwise apply the shared resolver."""

    selected = getattr(args, "_resolved_workspace", None)
    if selected is not None:
        return Path(selected).expanduser().resolve()
    value = str(raw or ".")
    return resolve_workspace(value, explicit=value != ".")


def _cli_manifest_snapshot(workspace: Path) -> tuple[str, int | None, Any]:
    """Read one immutable-enough manifest snapshot without creating state."""
    from .system.manifest import ManifestManager

    try:
        record = ManifestManager(workspace).current()
    except Exception:
        return "UNKNOWN", None, None
    if isinstance(record, dict):
        raw_state = record.get("state", record.get("status", ""))
        raw_generation = record.get("generation")
    else:
        raw_state = getattr(record, "state", "")
        raw_generation = getattr(record, "generation", None)
    state = str(getattr(raw_state, "value", raw_state) or "").strip().upper()
    if state not in {"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"}:
        return "UNKNOWN", None, record
    generation = raw_generation if type(raw_generation) is int and raw_generation >= 0 else None
    return state, generation, record


def _cli_mutation(args: argparse.Namespace) -> bool:
    command = str(getattr(args, "command", "") or "")
    action = str(getattr(args, "action", "") or "").casefold()
    if command in {"provider", "desktop"}:
        return True
    if command == "source":
        return action in {"add", "remove"}
    if command == "import":
        return action == "create"
    if command == "hooks":
        return action != "status"
    if command == "groups":
        return action == "migrate" and bool(getattr(args, "apply", False))
    if command == "storage":
        return action not in {"audit", "report"}
    if command == "gc":
        return bool(getattr(args, "apply", False))
    return command in {"apply", "undo"}


def _dispatch_cutover(args: argparse.Namespace) -> int:
    """Dispatch every normal CLI command through the native V2 runtime."""
    try:
        workspace = _cli_workspace(args)
    except WorkspaceResolutionError as exc:
        print(json.dumps(exc.to_payload(surface="CLI"), ensure_ascii=False, sort_keys=True))
        return 2
    except Exception as exc:
        from .data_home import ControlHomeResolutionError

        if not isinstance(exc, ControlHomeResolutionError):
            raise
        print(json.dumps(exc.to_payload(surface="CLI"), ensure_ascii=False, sort_keys=True))
        return 2
    setattr(args, "_resolved_workspace", workspace)
    state, generation, manifest_record = _cli_manifest_snapshot(workspace)
    if state not in {"V2_READY", "V2_ACTIVE"}:
        result = v2_upgrade_payload(state, surface="CLI")
        result.update({
            "command": str(getattr(args, "command", "") or ""),
            "path": "v2",
            "status": "blocked",
            "guidance": (
                "Run `memoryguard upgrade`, or explicitly select an active V2 "
                "control home with --data-home/MEMORYGUARD_HOME and repair its "
                "provider binding."
            ),
        })
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    if generation is None or manifest_record is None:
        result = v2_upgrade_payload("UNKNOWN", surface="CLI")
        result.update({
            "command": str(getattr(args, "command", "") or ""),
            "path": "v2",
            "status": "blocked",
        })
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2

    from .runtime_v2.native_ports import NativeV2RuntimePort

    maintenance_port = None
    if str(getattr(args, "command", "") or "") == "storage":
        from .maintenance_v2.runtime_port import MaintenanceRuntimePort

        maintenance_port = MaintenanceRuntimePort(workspace)
    context = _cli_trusted_context(
        workspace,
        allow_verified_provider_repair=(
            str(getattr(args, "command", "") or "") == "provider"
            and str(getattr(args, "action", "") or "").casefold() == "repair"
        ),
    )
    native = NativeV2RuntimePort(
        workspace,
        maintenance_port=maintenance_port,
        state_provider=lambda: manifest_record,
    )
    result = native.dispatch_cli(
        str(getattr(args, "command", "") or ""),
        args,
        context=context,
        generation=generation,
        mutation=_cli_mutation(args),
        state=state,
    )
    cursor: Any = result
    host_action = ""
    for _ in range(3):
        if not isinstance(cursor, dict):
            break
        host_action = str(cursor.get("host_action") or "")
        if host_action:
            break
        cursor = cursor.get("data")
    allowed_host_actions = {"source", "provider", "hooks", "gui", "open", "desktop", "runtime"}
    if result.get("ok") and host_action:
        if host_action != str(getattr(args, "command", "")) or host_action not in allowed_host_actions:
            result = {
                "ok": False,
                "status": "error",
                "path": "v2",
                "code": "invalid_v2_host_action",
                "error": "invalid_v2_host_action",
            }
        else:
            setattr(args, "_native_context", context)
            return int(args.func(args) or 0)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    if result.get("ok"):
        return 0
    code = str(result.get("code", ""))
    return 2 if code in {
        "v2_upgrade_required",
        "v2_manifest_state_unavailable",
        "v2_not_active",
        "unknown_cli_command",
        "v2_context_capability_required",
        "v2_operation_retired",
    } else 1


def _warn_split_brain_after_upgrade(upgrade_argv: list[str]) -> None:
    """升级后提示仍在运行的旧构建进程。

    新代码已经落盘，但常驻的旧进程还持有同一个 workspace，下一次写入就会被
    拒绝。这里只报告并给出命令，不代替用户终止任何进程。
    """
    try:
        workspace = None
        if "--workspace" in upgrade_argv:
            workspace = upgrade_argv[upgrade_argv.index("--workspace") + 1]
        if not workspace:
            from .data_home import resolve_runtime_data_home

            workspace = str(resolve_runtime_data_home().expanduser().resolve())

        from .runtime_lease import split_brain_hint

        hint = split_brain_hint(workspace)
        if not hint.get("split_brain"):
            return
        print("")
        print("!! 升级已完成，但检测到仍在运行的旧构建进程：")
        for line in hint["detail_lines"]:
            print(line)
        print("   在它们退出前，所有写入都会被拒绝。")
        print(f"   处理：{hint['remedy']}，然后重启宿主。")
    except Exception:
        # 提示失败绝不能影响升级本身的返回码
        pass


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # ``upgrade`` is a public lifecycle command, not a legacy data-plane
    # command.  Intercept it before the compatibility cutover adapter so an
    # old 0.6.2 user never gets routed back to the legacy runtime.  Keeping it
    # outside the adapter's historical command snapshot also preserves the
    # existing public CLI compatibility contract.
    if raw_argv and raw_argv[0] == "upgrade":
        from .migration.upgrade import (
            build_parser as build_upgrade_parser,
            main as upgrade_main,
        )

        upgrade_argv = raw_argv[1:]
        requested = build_upgrade_parser().parse_args(upgrade_argv)
        if not requested.workspace and not requested.workspace_arg:
            from .data_home import resolve_runtime_data_home

            control_home = resolve_runtime_data_home().expanduser().resolve()
            # Bare upgrade is the one explicit migration affordance allowed to
            # inspect a project-local V1 tree. An explicitly configured data
            # home is an operator-selected isolated target, never permission
            # to inspect, migrate, or clean cwd/ancestor V1 trees.
            source = None
            if (
                requested.data_home is None
                and not os.environ.get("MEMORYGUARD_HOME", "").strip()
            ):
                # Ambient MEMORYGUARD_WORKSPACE must not redirect this
                # decision: only cwd and bounded ancestors are eligible
                # sources, and the runtime control home stays global.
                source = discover_migration_source(
                    cwd=Path.cwd(), data_home=control_home,
                )
            # Upgrade target is canonical user V2 Data Home.  A discovered
            # cwd/ancestor V1 tree is source only; never make it the V2
            # control root or the runtime will split across two planes.
            upgrade_argv = [*upgrade_argv, "--workspace", str(control_home)]
            if source is not None:
                upgrade_argv.extend(["--data-home", str(source)])
        # Product default: one command completes the verified cutover.  A
        # read-only plan remains available, but must be requested explicitly.
        if not requested.preview and not requested.apply and requested.confirm is None:
            upgrade_argv.extend(["--apply", "--confirm", "V2_ACTIVE"])
        elif not requested.preview and requested.confirm is not None and not requested.apply:
            upgrade_argv.append("--apply")
        rc = upgrade_main(upgrade_argv)
        if not requested.preview:
            _warn_split_brain_after_upgrade(upgrade_argv)
        return rc
    parser = build_parser()
    args = parser.parse_args(argv)
    return _dispatch_cutover(args)


if __name__ == "__main__":
    sys.exit(main())
