"""交互面板结构测试。

验证意图：神经图用于查看内容，治理动作统一进入治理台；其余治理视图必须复用同一套视觉语义。
"""

from html import unescape
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.interactive import render_interactive_html  # noqa: E402


def test_neuron_graph_uses_status_rail_for_node_detail() -> None:
    html = render_interactive_html()

    assert '<link rel="icon" type="image/png" href="memoryguard-icon.png">' in html
    stage = html.index('id="neuron-stage"')
    rail = html.index('id="status-rail"')

    assert rail < stage
    assert "当前数据源映射" in html
    assert "原生记忆投影" in html
    assert "重构治理投影" in html
    assert "证据/萃取来源" in html
    assert "toggleProjectionSource" not in html
    assert "refreshNeuronGraph" in html
    assert "publishReconstructedMemory" not in html
    assert "publishToAgentNativeTarget" not in html
    assert "choose_publish_target_path" not in html
    assert "原生记忆文件保持只读" in html
    assert "选择写回目标编号" not in html
    assert "rollbackNativeMemoryRelease" not in html
    assert "showNativeReleaseArchive" not in html
    assert "发布存档" not in html
    assert "manifest_path" not in html
    assert "showRollbackModal" not in html
    assert "confirmRollbackModal" not in html
    assert "选择要恢复的版本编号" not in html
    assert "rollback_native_memory_release" not in html
    assert "await refreshNeuronGraph(shared ?" in html
    assert "await refreshNeuronGraph('当前投影已删除" in html
    assert "已写入 Agent 原生记忆入口" not in html
    assert "renderNeuronMetaBar" in html
    assert "${groupLabel} ·" in html
    assert "投影成员信息待加载" in html
    assert "meta.bound_agents" not in html
    assert "点击任意光点，在右侧查看可读内容" in html
    assert "规则与习惯可直接在图内治理" in html
    assert "selectedNeuronNode" in html
    assert "findNeuronByMemory" in html
    assert "focusNeuronNode" in html
    assert "自动纳入重构" in html
    assert ",'accept')" not in html
    assert "renderNeuronRailDetail" in html
    assert "selectNeuronByMemory" in html
    assert "cyInstance.animate({ center: { eles: cyNode }" in html
    assert "flashClass('sourcePulse'" in html
    assert "startNeuronSignalPulses" in html
    assert "pickNeuronSignalPath" in html
    assert "edge.signal" in html
    assert "mapData(strength, 0, 1, 2.4, 4.8)" in html
    assert "mapData(strength, 0, 1, 1.5, 3.0)" in html
    assert "相关连线（点击跳转）" in html
    assert "突触末梢（点击跳转）" in html
    assert "selectNeuronByMemory" in html
    assert "同源跨类型" in html
    assert "相似关联" in html
    assert "legend-edge shared" in html
    assert "删除/排除" in html
    assert "阅读语言" in html
    assert "setReaderLanguage('zh')" in html
    assert "displayTitle" in html
    assert "displayBody" in html
    assert "let ruleVisibilityFilter = 'all';" in html
    assert "hydrateNeuronNodeDetail" in html
    assert "callApi('get_memory', node.memory_id" in html
    assert "background-color': 'data(bg)'" in html
    assert "kindColor" in html
    assert "name: 'preset'" in html
    assert "neuronNodePositions" in html
    assert "接受候选" not in html
    assert "确认合并" not in html
    assert "同源突触" in html
    assert "记忆末梢" in html
    assert "来源/类型主题" in html
    assert "node.derivation" in html
    assert 'edge[etype = "duplicate"]' in html
    assert "构建重构投影" in html
    assert "构建重构投影（含 LLM 整理）" not in html
    assert "重构治理投影用于自动治理、萃取并发布回原生记忆" not in html
    assert "共享图直接读取 SharedMemoryStore" in html


def test_import_copy_treats_conversations_as_evidence_not_memory() -> None:
    html = render_interactive_html()

    assert "会话内容默认只作为证据/萃取来源" in html
    assert "不直接写入长期记忆" in html
    assert "会话解析为 MemoryRecord 写入 IR" not in html


def test_all_views_share_the_neural_visual_system() -> None:
    html = render_interactive_html()

    assert "--accent: #6ee7c4" in html
    assert 'class="brand-orb"' in html
    assert 'class="flow-canvas"' in html
    assert 'class="finding-item' in html
    assert 'class="plan-item' in html
    assert 'class="neuron-shell"' in html


def test_three_column_governance_shell_is_default_structure() -> None:
    html = render_interactive_html()

    assert 'class="app-shell"' in html
    assert 'class="sidebar"' in html
    assert 'class="main-wrapper"' in html
    assert 'class="status-rail"' in html
    assert "width: 224px" in html
    assert "width: 280px" in html
    assert "@media (max-width: 1024px)" in html
    assert "@media (max-width: 1100px)" not in html


def test_reference_shell_has_exact_primary_navigation_and_responsive_columns() -> None:
    html = render_interactive_html()

    # The final reference-layout contract is intentionally asserted as a
    # string: the browser owns the CSS, while this test prevents a later CSS
    # pass from silently restoring the old horizontal navigation.
    assert "grid-template-columns: 224px minmax(0, 1fr) 280px" in html
    assert ".main-wrapper { display: contents; }" in html
    assert 'grid-column: 1; grid-row: 1 / span 2' in html
    assert 'grid-column: 2 / span 2' in html
    assert 'grid-column: 3; grid-row: 2' in html
    nav_items = re.findall(r'<button class="nav-item[^>]+data-tab="([^"]+)"', html)
    assert nav_items == ['overview', 'sources', 'neurons', 'codegraph', 'rules', 'history', 'findings', 'token-usage']
    assert 'class="sidebar-settings"' in html
    assert 'aria-label="打开设置"' in html
    assert '@media (max-width: 720px)' in html


def test_narrow_navigation_keeps_readable_labels_inside_horizontal_scroller() -> None:
    html = render_interactive_html()

    assert '.sidebar-nav { display: flex; align-items: stretch; gap: 2px; padding: 6px 10px; overflow-x: auto; overflow-y: hidden; }' in html
    assert '.sidebar-nav .nav-item { flex: 0 0 auto; min-width: max-content; min-height: 34px; margin: 0; padding: 7px 10px; gap: 8px; justify-content: flex-start; overflow: visible; white-space: nowrap; font-size: 12px; }' in html
    assert '.sidebar-nav .nav-item::before { flex: none; width: 5px; height: 5px; }' in html


def test_navigation_has_no_numeric_page_badges_and_keeps_token_entry() -> None:
    html = render_interactive_html()

    assert 'data-tab="token-usage"' in html
    assert '.nav-item[data-tab="token-usage"]' not in html
    for index in range(1, 8):
        assert f'content: "{index}"' not in html


def test_token_usage_view_has_separated_measurement_contract() -> None:
    html = render_interactive_html()

    assert 'data-tab="token-usage"' in html
    assert "Token 用量与 MCP 节省" in html
    assert "function renderTokenUsage" in html
    assert "callApi('get_usage_telemetry'" in html
    assert "callApi('sync_usage_telemetry')" in html
    assert "同步用量数据" in html
    assert "function tokenUsageVerifiedHostTotals" in html
    assert "measured_total_coverage" in html
    assert "coverage.provider_reported === 'complete'" in html
    assert "coverage.input_output_derived === 'complete'" in html
    assert "tokenUsageSum(measuredRows, 'measuredTotal')" not in html
    assert "window_days" in html
    assert "宿主实测流量" in html
    assert "MG 估算节省" in html
    assert "不可用 Agent" in html
    assert "宿主实测" in html
    assert "MemoryGuard 估算" in html
    assert "原始候选" in html
    assert "实际注入" in html
    assert "实测输入" in html
    assert "实测输出" in html
    assert "转换次数" in html
    assert "最近同步" in html
    assert "不可合计" in html
    assert "宿主未提供" in html
    assert "宿主未提供实测用量" in html
    assert "未检测到来源" in html
    assert "未同步" in html
    assert "callApi('run_audit')" in html
    assert "用量数据：${tokenUsageSyncStatusText(sync)}；扫描：完成" in html


def test_token_usage_chart_keeps_edge_date_labels_inside_svg_viewbox() -> None:
    html = render_interactive_html()

    # Both edge labels use centered text anchors.  The symmetric SVG gutter is
    # therefore part of the rendering contract, otherwise the final date is
    # clipped when the chart fills a wide desktop container.
    assert "const left = 52;" in html
    assert "const right = 52;" in html
    assert "const chartWidth = width - left - right;" in html


def test_agent_cards_use_readable_identity_and_collapsed_technical_ids() -> None:
    html = render_interactive_html()

    assert 'function agentFamily(agentOrId)' in html
    assert 'data-agent-family="${family}"' in html
    assert 'class="agent-avatar"' in html
    assert 'class="agent-card ${active ? \'active\' : \'\'}" role="button" tabindex="0"' in html
    assert 'agent-technical-id' in html
    assert '未识别的 MCP 助手' in html
    assert '尚未返回可读来源摘要；可从本机 Agent 检测或匹配入口接入。' in html
    assert '远程favicon' not in html


def test_agent_cards_render_product_marks_without_remote_assets() -> None:
    html = render_interactive_html()

    # Product identity must be visible in the card itself.  A text glyph or a
    # missing icon regresses to the opaque/unknown presentation seen in the
    # GUI; inline SVG keeps the panel offline and deterministic.
    assert "function agentIconMarkup(agentOrId)" in html
    assert "agent-icon-svg" in html
    assert "data-agent-family=\"${family}\"" in html
    for family in ("codex", "claude", "cursor", "trae", "grok", "unknown"):
        assert f"data-agent-family=\"{family}\"" in html
    assert 'agent-mark-text">Codex' in html
    assert 'agent-mark-text">Grok' in html
    assert 'M21 10.5h3v3h-3v3h-1.5v3' in html
    assert 'M11.503.131 1.891 5.678' in html
    assert 'M24 20.5H3.5V17H0V3.5h24' in html
    assert '.agent-avatar .agent-icon-svg { display: block; width: 22px; height: 22px; fill: currentColor; stroke: none; }' in html
    assert "${agentIconMarkup(agent)}" in html
    assert "${agentIconMarkup(item)}" in html
    assert "<img" not in html[html.index("function agentIconMarkup"):html.index("function agentSourceSummary")]


def test_unknown_health_is_not_presented_as_zero_score() -> None:
    html = render_interactive_html()

    assert "function healthEvidenceUnavailable(report = {})" in html
    assert "function healthScopeLabel(report = {})" in html
    assert "reference_integrity: '引用完整性'" in html
    assert "health_available" in html
    assert "health_status" in html
    assert "function optionalFiniteNumber(value)" in html
    assert "const rawHealth = optionalFiniteNumber(report.health_score);" in html
    assert "const health = optionalFiniteNumber(report.health_score);" in html
    assert "pending: '待扫描'" in html
    assert "unavailable: '暂不可用'" in html
    assert "stale: '已过期'" in html
    assert "healthEvidenceUnavailable(r)" in html
    assert "healthEvidenceUnavailable(report)" in html
    assert "审计通过（未提供量化评分）" in html


def test_health_kpi_exposes_evidence_coverage_and_inconclusive_reason() -> None:
    html = render_interactive_html()

    assert "function healthCoverageText(report = {})" in html
    assert "health_coverage" in html
    assert "health_components" in html
    assert "4/4" not in html
    assert "coverage.status === 'complete'" in html
    assert "coverage.status === 'inconclusive'" in html
    assert "证据不完整" in html
    assert "out_of_scope" in html


def test_shell_uses_real_app_frame_with_no_dead_corner() -> None:
    html = render_interactive_html()

    assert ".app-shell { display: grid; grid-template-columns: 224px minmax(0, 1fr) 280px; grid-template-rows: 64px" in html
    assert ".status-rail { grid-column: 3; grid-row: 2;" in html
    assert ".topbar { grid-column: 2 / span 2; grid-row: 1;" in html
    assert ".sidebar { position: relative;" in html
    assert ".topbar-brand { display: inline-flex; }" in html
    assert "!important" not in html[html.index("/* Reference shell:"):html.index("</style>")]


def test_conflict_queue_keeps_missing_members_visible_and_exposes_backend_actions() -> None:
    html = render_interactive_html()
    start = html.index("async function renderConflictQueue()")
    end = html.index("async function renderQuarantine()", start)
    queue = html[start:end]

    assert "function conflictMemberLabel(member)" in html
    assert "function conflictActionDescriptors(conflict)" in html
    assert "item.display_name, item.title, item.label" in html
    assert "const preview = item.preview || item.body_preview || item.body" in queue
    assert "available_actions" in html
    assert "data-conflict-action" in queue
    assert "invokeConflictAction" in queue
    # A stale conflict may not have two keepable records, but it must remain
    # operable when the backend advertises close/cleanup/restore.
    assert "关闭冲突" in html
    assert "关闭失效冲突" in html
    assert "清理冲突" in html
    assert "恢复候选" in html
    assert "rawMethod === 'conflict_close_stale' ? 'close_stale_conflict'" in html


def test_agent_member_panel_separates_programs_from_historical_connections() -> None:
    html = render_interactive_html()

    assert "program_member_count" in html
    assert "endpoint_member_count" in html
    assert "unresolved_member_count" in html
    assert "member_details" in html
    assert "Math.max(0, endpointMemberCount - programCount)" in html
    assert "extra_connection_count" in html
    assert "function isUnknownHistoricalMember(member)" in html
    assert "historical_unknown" in html
    assert "item.canonical_program_id ?? item.program_id" in html
    assert "identity_resolution || item.identity_status" in html
    assert "agentFamily(item) !== 'unknown' || !genericLabel" in html
    assert "未识别的 mcp 助手" in html
    assert "optionalFiniteNumber(agentCardsData?.unknown_member_count)" in html
    assert "optionalFiniteNumber(agentCardsData?.unresolved_member_count)" in html
    assert "const programProjection = Array.isArray(agentCardsData?.program_member_details)" in html
    assert "const memberNames = railSummary.names.length ? railSummary.names" in html
    assert "function governanceGroupProgramSummary(group)" in html
    assert "program_member_details" in html
    assert "const unknown = summary.unknownCount ?" in html
    assert "条连接（其他 ${summary.otherCount}）" in html
    assert "待识别/历史连接" in html
    assert "agentMemberStatus(item, binding, true)" in html
    assert "unbindAgentBinding" in html
    assert "can_unbind" in html


def test_governance_flow_cards_navigate_to_real_queues() -> None:
    html = render_interactive_html()

    assert "flow-card cyan" in html
    assert "flow-card gray" in html
    assert "flow-card amber" in html
    assert "flow-card red" in html
    assert "switchGovernanceSub('recent_events')" in html
    assert "switchGovernanceSub('supersede')" in html
    assert "switchGovernanceSub('conflicts')" in html
    assert "switchGovernanceSub('quarantine')" in html


def test_agent_data_view_consumes_scopes_not_legacy_categories() -> None:
    html = render_interactive_html()

    assert "agentData.scopes" in html
    assert "const renderScope" in html
    assert "const renderCategory" in html
    assert "agentData && agentData.categories" not in html


def test_discovered_native_memory_rows_are_not_clickable_files() -> None:
    html = render_interactive_html()

    assert "const canOpen = !!f.root_id && f.authorized !== false && f.read_status !== 'discovered'" in html
    assert "仅发现，需先授权" in html
    assert "该条目只是发现结果，尚未授权为可读取来源" in html


def test_neuron_graph_selection_uses_rail_context_not_inner_panel() -> None:
    html = render_interactive_html()

    assert "neuron-detail-panel" not in html
    assert "进入治理台" in html
    assert "点击任意光点，在右侧查看可读内容" in html
    assert "displayBody(node) || node.body || '暂无正文内容'" in html
    assert "相关连线（点击跳转）" in html
    assert "接受候选" not in html


def test_lifecycle_ui_matches_backend_enums_and_residual_split() -> None:
    html = render_interactive_html()

    assert "installed_no_data: '已安装无数据'" in html
    assert "data_only: '原生数据待接入'" in html
    assert "uncertain: '待确认'" in html
    assert "agentCardsData.residuals" in html
    assert "残留与清理" in html
    assert "Agent 摘要" in html
    assert "已接入" in html
    assert "已发现 · 待接入" in html
    assert "if (binding) return binding.group_kind === 'personal' ? '已启用个人层' : '已绑定共享组'" in html
    assert "const lifecycleChip = binding ? 'confirmed'" in html
    assert "新建个人记忆层" in html
    assert "可接入 MemoryGuard 层" in html
    assert "const items = result.items || []" in html


def test_agent_and_governance_display_fallbacks_match_current_api_shapes() -> None:
    html = render_interactive_html()

    assert "function agentSummary(agent, sourceCount = null)" in html
    assert "item.found_surface_count" in html
    assert "finiteNumber(item.surface_count, surfaces.length)" in html
    assert "item.bound_source_count" in html
    assert "${a.found_surface_count}" not in html
    assert "${a.surface_count}" not in html
    assert "${a.bound_source_count}" not in html
    assert "function governanceSnapshot(raw)" in html
    assert "memory.total" in html
    assert "memory.status_counts" in html
    assert "finiteNumber(counts.active_memories, 0)" in html
    assert "clearSharedGovernance('stale_selection'" in html
    assert "当前没有活动绑定，不能作为共享治理范围" in html
    assert "audit_only" in html
    assert "function agentDisplayName(agentOrId, fallback = '未知助手')" in html
    assert "return label;" in html
    assert "activityActorLabel(item)" in html


def test_multi_agent_ui_keeps_existing_personal_and_shared_groups_selectable() -> None:
    html = render_interactive_html()

    assert "callApi('list_share_groups')" in html
    assert "showMultiAgentBinding(agentsResult, bindingsResult, hooksResult, groupsResult)" in html
    assert "data-existing-group-agent" in html
    assert "bindSelectedExistingGroup" in html
    assert "绑定已有记忆组" in html
    assert "已有记忆组" in html
    assert "当前没有绑定 Agent；仍可重新接入" in html


def test_discovery_ui_exposes_existing_groups_without_requiring_native_memory() -> None:
    html = render_interactive_html()

    assert "const [result, groupsResult, bindingsResult] = await Promise.all" in html
    assert "showDiscoveryResult(result, groupsResult, bindingsResult)" in html
    assert "data-existing-group-agent" in html
    assert "接入已有记忆组" in html
    assert "新建个人记忆层" in html
    assert "导入原生记忆（可选）" in html
    assert "无原生记忆不影响接入" in html
    assert "管理已有记忆组" in html


def test_discovery_ui_discloses_profile_bounded_market_coverage() -> None:
    html = render_interactive_html()

    assert "known_profile_count" in html
    assert "未登记的新产品不会被猜测扫描" in html
    assert "外部 Profile 或手工来源接入" in html


def test_gui_normalizes_v2_audit_and_redacted_path_values() -> None:
    """V2 receipts and redacted path descriptors must not break the browser shell."""
    html = render_interactive_html()

    assert "function normalizeAuditReport" in html
    assert "Number.isFinite" in html
    assert "function guiPathText" in html
    assert "function guiPathLabel" in html
    assert "source_root_id" in html
    assert "source_root_id: c.dataset.sourceRootId" in html
    assert "(f.path || '').split(/[/\\\\]/)" not in html
    assert "const p = escapeHtml(f.path || '').replaceAll" not in html


def test_governance_actions_use_choices_and_explicit_scope() -> None:
    """治理写操作不能让用户手填路径/ID/原因，也不能隐式落到 default 组。"""
    html = render_interactive_html()

    assert "prompt(" not in html
    assert "createBuildPlan" not in html
    assert "showBuildTargetModal" not in html
    assert "生成构建计划" not in html
    assert "发布事务" not in html
    assert "完整替换受管目标文件" not in html
    assert "pickDecisionReason('takeover')" in html
    assert "list_share_groups" in html
    assert "selectGovernanceGroup" in html
    assert "callApi('get_recent_events', activeShareGroupId)" in html
    assert "callApi('get_supersede_decisions', activeShareGroupId)" in html
    assert "callApi('get_conflicts', activeShareGroupId)" in html
    assert "callApi('get_quarantine', activeShareGroupId)" in html
    assert "callApi('list_memory_versions', activeShareGroupId)" in html


def test_conflict_queue_uses_self_contained_snapshots_and_fails_closed_for_stale_members() -> None:
    html = render_interactive_html()
    start = html.index("async function renderConflictQueue()")
    end = html.index("async function renderQuarantine()", start)
    queue = html[start:end]

    assert "callApi('get_conflicts', activeShareGroupId)" in queue
    assert "callApi('list_memory', '', '', activeShareGroupId)" not in queue
    assert "const [rawSnapshot, rawScope, rawConflicts]" in html
    assert "unresolved_total" in html
    assert "selectable_total" in html
    assert "closable_stale_total" in html
    assert "item.preview || item.body_preview || item.body || item.reason" in queue
    assert "item.selectable === true || item.live === true" in queue
    assert "历史冲突 · 候选失效，可关闭" in queue
    assert "该组不可二选一，但可关闭以保留审计记录。" in queue
    assert "个未闭合冲突组，可选择保留 ${actionableCount} 组、可关闭失效 ${closableStaleCount} 组" in queue
    assert "disabled title=\"${escapeHtml(invalidReason)}\"" in queue
    # Missing/legacy ID-only members have no explicit selectable flag and
    # therefore never get a radio input.
    assert "历史正文不可恢复（仅保留成员 ID）" in queue


def test_pywebview_bridge_prefers_dispatch_and_keeps_safe_legacy_fallback() -> None:
    html = render_interactive_html()

    assert "typeof bridge.dispatch_api === 'function'" in html
    assert "raw = await bridge.dispatch_api(method, args)" in html
    assert "typeof bridge.call_readonly === 'function'" in html
    assert "typeof bridge.request_mutation === 'function'" in html
    assert "await bridge.request_mutation(method, args)" in html
    assert "await bridge.call_readonly(method, args)" in html


def test_rule_auto_scope_groups_actions_and_collapses_dominant_delete() -> None:
    html = render_interactive_html()

    assert "function groupRuleDecisions" in html
    assert "function renderRuleDecisionGroup" in html
    assert "rule-decision-group" in html
    assert "rule-decision-subgroup" in html
    assert "function ruleDecisionActionLabel" in html
    assert '<details class="rule-decision-group">' in html
    assert "scope_type" in html
    assert "范围置信度" in html
    assert "function undoRuleDecisionGroup" in html
    assert "批量撤销 ${undoableCount} 条" in html
    start = html.index("async function requestRuleDecisionUndo")
    end = html.index("async function undoRuleDecision(decisionId)", start)
    undo_request = html[start:end]
    assert "callApi('undo_rule_decision', id, activeShareGroupId || 'default', true)" in undo_request
    assert "agent_instance_id" not in undo_request
    assert "share_group_id:" not in undo_request
    assert "decisions.slice(-20).reverse()" not in html


def test_history_reads_use_one_selector_structured_requests_and_readable_cards() -> None:
    html = render_interactive_html()

    assert "function buildHistoryReadRequest" in html
    assert "buildHistoryReadRequest({sessionId, limit: 100, offset: 0})" in html
    assert "buildHistoryReadRequest({turnId, limit: 1, offset: 0})" in html
    assert "callApi('history_read', sessionId, '', historyScope(), 100, 0)" not in html
    assert "callApi('history_read', '', turnId, historyScope(), 1, 0)" not in html
    assert "callApi('history_extract_preview', {session_id:" in html
    assert "callApi('export_history', {session_ids:" in html
    assert "session?.display_title" in html
    assert "s.summary || s.preview_excerpt" in html
    assert "conversation_selector_invalid" in html


def test_neuron_graph_uses_root_outward_soft_signal_bands() -> None:
    html = render_interactive_html()

    assert 'id="neuron-particles"' in html
    assert "animateNeuronPathParticle" in html
    assert "raw * edges.length" in html
    assert "renderedPosition()" in html
    assert "control-point-distances" in html
    assert "Math.atan2" in html
    assert "isOutwardNeuronEdge" in html
    assert "collectNeuronSignalSources" in html
    assert "collectNeuronSignalPaths" in html
    assert "node[kind = \"root\"]" in html
    assert "const desired = Math.min(4, Math.max(2" in html
    assert "const perEdgeMs = 820" in html
    assert "sourcePulse" in html
    assert "terminalPulse" in html
    assert "nodeArrivalPulse" in html
    assert "neuron-edge-particle-core" not in html
    assert "neuron-edge-particle-trail" not in html
    assert "Always launch 5–8 concurrent full-path pulses" not in html
    assert "setInterval(fireWave, 1600)" not in html
    assert "const spark = () =>" not in html
    assert "'shape': 'ellipse'" in html
    assert "round-rectangle" not in html
    assert "Math.random()" not in html[html.index("function buildNeuronSignalPath"):html.index("function fitNeuronGraph")]
    assert "neuron-particle-travel" not in html


def test_neuron_layout_keeps_primary_ring_close_and_clusters_compact() -> None:
    html = render_interactive_html()

    assert "const radius = 118 + Math.min(18" in html
    assert "node.node_kind === 'history_session' ? 82" in html
    assert "node.node_kind === 'virtual_rule_ref' ? 78 : 96" in html
    assert "const clusterPadding = sameBranch ? 10 : 26" in html
    assert "const radius = 300 + Math.min(90" not in html


def test_risk_signals_offer_agent_handoff_without_blind_auto_fix() -> None:
    """不可自动修复的风险也必须有明确处理出口，并要求修复后复扫。"""
    html = render_interactive_html()

    assert "复制全部风险给 Agent" in html
    assert "复制给 Agent 处理" in html
    assert "copyFindingForAgent" in html
    assert "copyAllFindingsForAgent" in html
    assert "完成后重新扫描验证" in html
    assert "不要修改 MemoryGuard 的来源文件" in html
    assert 'aria-expanded="false"' in html
    assert 'style="display:none"' in html
    assert "element.closest('.finding-item')" in html


def test_reader_language_has_explicit_chinese_and_english_modes() -> None:
    """语言开关应表达显示语言，而不是含义不明的“原文”。"""
    html = render_interactive_html()

    assert "setReaderLanguage('auto')" in html
    assert "setReaderLanguage('zh')" in html
    assert "setReaderLanguage('en')" in html
    assert 'id="reader-original"' not in html
    assert ">原文</button>" not in html
    assert "无英文版本时显示来源原文" in html


def test_takeover_success_is_not_relabelled_as_refresh_failure() -> None:
    """正式接管提交成功后，投影刷新失败只能作为刷新警告。"""
    html = render_interactive_html()

    assert "正式接管已确认" in html
    assert "正式接管已完成，但神经图刷新失败" in html
    assert "await refreshNeuronGraph(`正式接管完成" not in html


def test_personal_and_shared_memory_layers_are_distinct_and_reachable() -> None:
    """个人层不能混进共享组解散区，且切换、安装和图谱入口都必须可达。"""
    html = render_interactive_html()

    assert "function memoryGroupKind(groupId)" in html
    assert "个人记忆层" in html
    assert "共享记忆层" in html
    assert "viewMemoryLayer" in html
    assert "installMemoryGroupMcp" in html
    assert "activeBindings = existingBindings.filter(b => b.status === 'active')" in html
    assert "b && b.group_kind === 'shared'" in html
    assert "确认启用该 Agent 的个人记忆层并安装全局 MCP" in html
    assert "MCP 未完整安装" in html
    assert "选择个人或共享记忆层" in html


def test_memory_layer_lifecycle_actions_are_explicit_and_safe() -> None:
    html = render_interactive_html()

    assert "showMemorySourceMap" in html
    assert "exportMemoryGroup" in html
    assert "clearMemoryGroup" in html
    assert "archiveMemoryGroup" in html
    assert "系统会先自动导出可恢复 ZIP" in html
    assert "保留 binding、MCP 配置和空数据库" in html
    assert "现有 MCP 将因无活动 binding 而拒绝读写" in html
    assert "原生文件不变" in html


def test_shared_group_dissolve_copy_matches_final_lifecycle_contract() -> None:
    html = render_interactive_html()
    start = html.index("async function dissolveSharedGroup(groupId)")
    end = html.index("async function exitMultiAgentMode", start)
    dissolve = html[start:end]

    assert "callApi('dissolve_shared_group', groupId, true, true)" in dissolve
    assert "解绑共享组内全部 Agent" in dissolve
    assert "移除匹配的 MemoryGuard Hook 条目" in dissolve
    assert "将每位原成员返回其个人记忆层" in dissolve
    assert "受管数据保留为仅审计 tombstone" in dissolve
    assert "if (!result || result.error || result.ok === false)" in dissolve
    assert "showToast(apiErrorMessage(result, '解散共享组失败'), 'error')" in dissolve
    assert "所有原成员已返回个人记忆层" in dissolve
    assert "；受管数据保留为仅审计 tombstone`" in dissolve
    assert "删除共享组投影" not in dissolve
    assert "归档 SharedMemoryStore 目录" not in dissolve
    assert "需手动恢复" not in dissolve
    assert "result.archived_to" not in dissolve


def test_history_ui_routes_result_types_and_exposes_export() -> None:
    html = render_interactive_html()

    assert "r.matched_summary || r.summary" in html
    assert "r.can_timeline && anchor" in html
    assert 'data-mg-action="history-read-session"' in html
    assert 'data-session-id="${escapeHtml(r.session_id)}"' in html
    assert "exportHistorySession" in html
    assert "callApi('export_history', {session_ids:" in html
    assert "callApi('export_history', [sessionId], historyScope())" not in html


def test_history_ui_requires_real_governance_scope_and_refreshes_on_agent_switch() -> None:
    html = render_interactive_html()

    assert "function historyScope()" in html
    assert "if (isShareGroupScope())" in html
    assert "if (activeAgentInstanceId)" in html
    assert "const scopeReady = await ensureGovernanceScope();" in html
    assert "需要有效治理范围" in html
    assert "if (state.activeTab === 'history') renderHistory()" in html


def test_history_ui_hides_cross_owner_delete_action() -> None:
    html = render_interactive_html()

    assert "const canDelete = !!activeAgentInstanceId && owner === activeAgentInstanceId;" in html
    assert "仅会话 owner 可删除" in html
    assert "${canDelete ? `<button class=\"btn\" data-mg-action=\"history-delete\"" in html
    assert "当前共享组成员可查，仅 owner 可删" in html
    assert "meta.project_status === 'unknown' ? ' · 未识别项目'" not in html
    assert "meta.project_status === 'removed' ? ' · 路径已移除' : ''" in html


def test_data_sources_render_as_folder_tree() -> None:
    html = render_interactive_html()

    assert "const buildFileTree" in html
    assert 'class="folder-group"' in html
    assert 'class="folder-row"' in html
    assert "${count} 个文件" in html
    assert '<div class="folder-children">${renderFileTree(child, depth + 1)}</div>' in html


def test_history_projects_render_as_collapsible_folders() -> None:
    html = render_interactive_html()

    assert 'class="card history-project-group folder-group"' in html
    assert "${sessionCount} 个会话" in html
    assert '<div class="folder-children">${agents}</div>' in html


def test_rule_habits_render_as_collapsible_folders() -> None:
    html = render_interactive_html()

    assert '<details class="folder-group" open style="--folder-depth:0">' in html
    assert '<summary class="folder-row"><span class="folder-caret" aria-hidden="true"></span><span class="folder-name">${label}</span><span class="folder-count">${cards.length} 条</span></summary>' in html
    assert '<div class="folder-children">${cards.join(\'\')}</div>' in html
    assert "const mergedHtml = mergedCount ? `<span class=\"chip chip-info\">已合并 ${mergedCount} 条旧记忆</span>` : '';" in html


def test_neuron_drag_carries_descendant_branches() -> None:
    html = render_interactive_html()

    assert "const collectNeuronSubtree = (rootId) => {" in html
    assert "if (!dragRoot.selected()) dragRoot.select();" in html
    assert "const dragNodeIds = new Set([dragRoot.id()]);" in html
    assert "collectNeuronSubtree(dragRoot.id()).forEach(id => dragNodeIds.add(id));" in html
    assert "dragNodeIds.forEach(nodeId => {" in html


def test_neuron_graph_bridges_missing_parent_edges() -> None:
    html = render_interactive_html()

    assert "const edgeKeys = new Set();" in html
    assert "parent-bridge:" in html
    assert "主光点到分类不会悬空" in html


def test_projection_build_ui_closes_start_cancel_and_error_states() -> None:
    html = render_interactive_html()

    assert "let activeBuildRunId = '';" in html
    assert '<button class="btn" type="button" disabled>正在创建任务…</button>' in html
    assert "pointer-events:none\">正在创建任务…" in html
    assert "phase: 'cancelling', message: '正在提交取消请求…'" in html
    assert "await restoreNeuronAfterBuild(buildResultMessage(result || {}, '取消失败'), true)" in html
    assert "await restoreNeuronAfterBuild('构建已取消', false)" in html
    assert "{ id: 'engine', label: '引擎' }" in html
    assert "{ id: 'enrich', label: '整理' }" not in html


def test_codegraph_has_an_independent_navigation_tab() -> None:
    html = render_interactive_html()

    assert 'data-tab="codegraph"' in html
    assert "case 'codegraph': renderCodeGraph(); break;" in html
    assert "let codeGraph = null;" in html
    assert "let codeCyInstance = null;" in html
    assert 'id="codegraph-canvas"' in html
    assert "codeGraphElements" in html
    assert "codeGraphElements(graph)" in html
    assert "selectedCodeGraphNode" in html
    assert "Memory Projection" in html


def test_memory_core_requests_memory_projection_graph_endpoint() -> None:
    html = render_interactive_html()

    assert "async function renderNeurons()" in html
    assert "neuronGraph = await callApi('get_memory_neuron_graph'" in html
    assert "async function refreshNeuronGraph(message = '')" in html
    assert html.count("callApi('get_memory_neuron_graph'") == 2


def test_codegraph_requests_independent_endpoint_without_legacy_neuron_endpoint() -> None:
    html = render_interactive_html()

    assert "async function renderCodeGraph()" in html
    assert "callApi('get_codegraph_graph', request)" in html
    assert "codeGraph = {...graph, codegraph_status: codegraphStatus};" in html
    assert html.count("callApi('get_codegraph_graph'") == 1
    assert "get_neuron_graph" not in html
    assert "callApi('codegraph_graph'" not in html


def test_scope_selectors_render_agent_names_and_build_has_no_dialogue_path() -> None:
    html = render_interactive_html()

    assert "function agentDisplayName" in html
    assert "agentNamesForIds" in html
    assert "scopeSelectionLabel" in html
    assert "governanceScopeState.status !== 'active'" in html
    assert "return '未选择治理范围'" in html
    assert "agentDisplayName(agentId)" in html
    assert "memberNames.join('、')" in html
    assert "list_host_llm_agents" in html
    assert "function refreshProjectionEngines" in html
    assert "未发现可执行整理引擎，确定性构建可用；LLM 整理不可选。" in html
    assert "<option value=\"deterministic\">确定性构建（不使用 LLM）</option>" in html
    assert "String(item.mode || 'cli').toLowerCase() === 'cli'" in html
    assert "showLlmPickModal" not in html
    assert "宿主 Skill" not in html
    assert "须在 Cursor 对话" not in html
    assert "无需在别处对话继续" not in html
    assert "deterministic" in html
    assert "后端未返回可追踪任务 ID" in html
    assert "onclick=\"renderNeurons()\">重新读取 Memory Projection" in html


def test_projection_build_ui_uses_honest_engine_and_task_lifecycle_contract() -> None:
    html = render_interactive_html()

    assert "let buildStartInFlight = false;" in html
    assert "let buildCancelInFlight = false;" in html
    assert "不能重复确认" in html
    assert "构建任务已创建，正在运行" in html
    assert "buildHasNoSources(result)" in html
    assert "buildIsBlocked(result)" in html
    assert "没有可构建的数据源：当前数据源映射为空" in html
    assert "构建被后端阻止" in html
    assert "当前没有可取消的构建" in html
    assert "已提交取消请求，等待后端确认；当前构建尚未确认停止" in html
    assert "scope-fallback" not in html


def test_projection_source_mapping_empty_state_routes_to_sources() -> None:
    html = render_interactive_html()

    assert "当前治理范围已设定，但尚未选择数据源" in html
    assert "范围不等于数据源" in html
    assert "去数据源页选择数据源" in html
    assert "no_projection_sources" in html
    assert "请先到数据源页启用来源" in html


def test_projection_source_map_defaults_to_accessible_collapsed_cards() -> None:
    html = render_interactive_html()
    start = html.index("function renderProjectionSourceMap")
    end = html.index("function projectionModeControls", start)
    source_map = html[start:end]

    assert "aria-expanded=\"false\"" in source_map
    assert 'aria-controls="${sourceDetailsId}"' in source_map
    assert "展开 ${sourceCount} 条来源" in source_map
    assert "function toggleSourceMapDetails(button)" in source_map
    assert "details.hidden = expanded" in source_map
    assert "source-map-list" in source_map
    assert "<dl class=\"source-map-fields\">" in source_map
    assert "<table" not in source_map


def test_shared_source_summary_uses_governed_memory_connector_and_projection_counts() -> None:
    html = render_interactive_html()
    start = html.index("function renderProjectionSourceMap")
    end = html.index("function projectionModeControls", start)
    source_map = html[start:end]

    assert "summary.governed_memory" in source_map
    assert "summary.selected_source_connectors" in source_map
    assert "summary.selected_source_connector_total" in source_map
    assert "summary.enabled" in source_map
    assert "受管记忆 ${governedMemory}" in source_map
    assert "连接器 ${connectorCount}/${connectorTotal}" in source_map
    assert "参与投影 ${participatingCount}" in source_map


def test_projection_source_cards_keep_long_ids_single_line_and_responsive() -> None:
    html = render_interactive_html()

    assert ".source-map-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));" in html
    assert ".source-map-fields dd code { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }" in html
    assert 'class="source-map-id" title="${escapeHtml(sourceId)}"' in html
    assert ".source-map-id { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }" in html
    assert "@media (max-width: 680px)" in html
    assert ".source-map-fields { grid-template-columns: minmax(0, 1fr); }" in html


def test_dashboard_chrome_exposes_seven_readable_pages() -> None:
    html = render_interactive_html()

    assert 'class="topbar-brand"' in html
    assert 'id="topbar-context"' in html
    assert "const PAGE_CHROME =" in html
    for tab in ("overview", "sources", "neurons", "codegraph", "rules", "history", "findings"):
        assert f"{tab}: {{index:" in html
        assert f'data-tab="{tab}"' in html
    assert ".page-heading::before" in html
    assert ".page-heading::after" in html
    assert "program_name || match.program" in html
    assert '<h2>规则与习惯</h2>' in html
    assert '<h2>对话历史</h2>' in html
    assert '<h2>风险信号与治理控制台</h2>' in html
    assert '<div class="page-head">' not in html
    assert ".nav-item { justify-content: center; font-size: 0;" in html
    assert ".page-actions { display: flex; flex-wrap: wrap;" in html
    assert "const knownGuiTabs =" in html
    assert "history.replaceState(null, '', '#' + tab)" in html


def test_agent_labels_do_not_expose_opaque_suffix_as_primary_name() -> None:
    html = render_interactive_html()

    assert "if (!label || /^(?:未知助手|未知\\s*Agent|unknown)$/i.test(label)) label = '未识别的 MCP 助手';" in html
    assert "id.slice(-4)" not in html
    assert "function looksLikeOpaqueAgentId" in html
    assert "readableAgentPart(program, id) || readableAgentPart(provider, id)" in html


def test_codegraph_ui_reports_automation_only_from_backend_state() -> None:
    html = render_interactive_html()

    assert "function codeGraphAutomationState(graph = {})" in html
    assert "const incremental = source.codegraph_status" in html
    assert "incremental.enabled === true && supported && builtScope && activeBinding" in html
    assert "自动增量已启用" in html
    assert "自动增量待建图" in html
    assert "自动增量待绑定" in html
    assert "callApi('codegraph_status', request)" in html
    assert "source.automatic === true" in html
    assert "自动更新状态待后端确认" in html
    assert "当前页面不伪造运行状态" in html
    assert "自动更新不可用" in html
    assert "写入成功后按变更文件刷新" not in html
    assert "自动增量更新'," not in html


def test_overview_uses_api_payloads_not_fake_screenshot_stats() -> None:
    html = render_interactive_html()

    assert "12.4K" not in html
    assert "暂不可用" in html
    assert "暂无扫描时间" in html
    assert "不补造统计" in html
    assert "function eventActionLabel" in html
    assert "auto_supersede: '自动覆盖'" in html


def test_overview_uses_explicit_scan_state_and_does_not_fabricate_health() -> None:
    html = render_interactive_html()

    assert "function normalizeAuditState(value)" in html
    assert "const explicitState = normalizeAuditState(report.audit_state || report.auditStatus || report.status);" in html
    assert "const completed = auditIsCompleted(report);" in html
    assert "health === null ? '健康度 ' + (findings.length ? `需处理 ${findings.length} 项` : '审计通过（未提供量化评分）')" in html
    assert "const healthText = health === null" in html
    assert "扫描完成/需处理" not in html


def test_overview_exposes_readable_detail_routes_for_conflicts_and_risks() -> None:
    html = render_interactive_html()

    assert "function openGovernanceSubtab(subTab)" in html
    assert "function openFinding(findingId)" in html
    assert "onclick=\"openGovernanceSubtab('conflicts')\"" in html
    assert "aria-label=\"打开风险信号详情\"" in html
    assert "原因：${escapeHtml(riskReasonText(finding))}" in html
    assert "建议：${escapeHtml(riskActionText(finding))}" in html


def test_overview_stage_statuses_are_explicit_or_unknown() -> None:
    html = render_interactive_html()

    assert "function governanceStageStates(report = {})" in html
    assert "只展示后端明确状态；缺少证据的阶段标为不可判定。" in html
    assert "stageStateLabel(stage)" in html
    assert "unknown: '不可判定'" in html


def test_reader_layer_is_above_horizontal_navigation() -> None:
    html = render_interactive_html()

    assert "position: relative; z-index: 30; min-height: 56px" in html
    assert ".topbar-reader { position: relative; z-index: 40; }" in html
    assert "z-index: 50; top: calc(100% + 7px)" in html


def test_risk_entries_explain_reason_impact_and_action_in_chinese() -> None:
    html = render_interactive_html()

    assert "function riskReasonText" in html
    assert "function riskImpactText" in html
    assert "function riskActionText" in html
    assert "<strong>原因</strong>" in html
    assert "<strong>影响</strong>" in html
    assert "<strong>建议动作</strong>" in html
    assert "打开治理台" in html
    assert "escapeHtml(finding.impact)" not in html
    assert "escapeHtml(finding.suggestion)" not in html


def test_risk_localization_hides_internal_target_from_summary_but_keeps_detail() -> None:
    html = render_interactive_html()

    assert "unknown_authoritative_table: '未知权威表'" in html
    assert "function riskTechnicalSource(finding = {})" in html
    assert "function riskAuditTargetLabel(finding = {})" in html
    assert "'system / group_outbox': '系统域的共享组出站记录'" in html
    assert '引用审计未能确认“${target}”的结构或引用完整性。' in html
    assert '<span class="key">技术来源</span>' in html


def test_unconsumed_outbox_payload_has_specific_chinese_title_and_reason() -> None:
    html = render_interactive_html()

    assert "unconsumed_outbox: '共享组事件待消费'" in html
    assert "function isUnconsumedOutboxFinding" in html
    assert "source === 'system / group_outbox' && title === 'unconsumed outbox'" in html
    assert "共享组事件队列存在未消费记录。" in html


def test_codegraph_label_strategy_is_explicit_and_zoom_safe() -> None:
    html = render_interactive_html()

    assert "function codeGraphKeyLabelIds(graph, limit = 8)" in html
    assert "label_priority: keyLabelIds.has(String(node.id || '')) ? 'true' : 'false'" in html
    assert "'label': ''" in html
    assert 'node[label_priority = "true"], node:selected, node.codegraph-label-hover, node.codegraph-label-zoomed' in html
    assert "codeCyInstance.on('mouseover', 'node'" in html
    assert "codeCyInstance.on('zoom', updateCodeGraphLabelPolicy);" in html
    assert "同文件符号合并显示 · 仅展示当前范围的跨文件关系" in html
    assert "文件总览" in html and "符号展开" in html


def test_automatic_scope_decisions_stay_collapsed_with_chinese_event_labels() -> None:
    html = render_interactive_html()

    assert '<details class="rule-decision-group">' in html
    assert '<details class="rule-decision-group" open' not in html
    assert "eventActionLabel(a.action || a.type || 'auto')" in html
    assert "有效记忆" in html
    assert "Active memories" not in html


def test_system_and_organizer_activity_has_a_stable_governance_label() -> None:
    html = render_interactive_html()

    assert "function activityActorLabel(item = {})" in html
    assert "authority === 'system'" in html
    assert "actor.startsWith('organizer:')" in html
    assert "MemoryGuard 自动治理" in html
    assert "activityActorLabel(item)" in html
    start = html.index("async function renderRecentEvents()")
    end = html.index("async function renderSupersedeChain()", start)
    recent_events = html[start:end]
    assert "activityActorLabel(e)" in recent_events
    assert recent_events.count("activityActorLabel(e)") >= 2


def test_projection_source_map_has_readable_shared_empty_state() -> None:
    html = render_interactive_html()

    assert "暂无共享记忆入库来源" in html
    assert "当前共享组没有可展示的受管记忆或连接器" in html


def test_reference_information_architecture_keeps_seven_primary_views_and_contextual_rails() -> None:
    """Reference layout is a working IA change, not a cosmetic card skin."""
    html = render_interactive_html()

    for css_class in ("dashboard-view", "compact-toolbar", "page-tabs", "kpi-grid", "data-table", "detail-rail"):
        assert css_class in html
    for tab in ("overview", "sources", "neurons", "codegraph", "rules", "history", "findings"):
        assert f'data-tab="{tab}"' in html
    assert 'data-tab="releases"' not in html
    assert 'data-tab="governance"' not in html
    assert "function renderOverviewRail" in html
    assert "function renderSourcesRail" in html
    assert "function renderRulesRail" in html
    assert "function renderHistoryRail" in html
    assert "function renderRiskRail" in html
    assert "let selectedSourceId = ''" in html
    assert "let selectedRuleId = ''" in html
    assert "let selectedHistorySessionId = ''" in html
    assert "let selectedFindingId = ''" in html
    assert "function filterNeuronGraph" in html
    assert "function sortNeuronGraph" in html
    assert "@media (max-width: 768px)" in html
    assert "@media (max-width: 1024px)" in html


def test_async_tab_renders_reject_stale_full_page_results() -> None:
    """A late tab request must not replace the page owned by the active tab."""
    html = render_interactive_html()

    assert "let contentRenderGeneration = 0;" in html
    assert "function contentRenderIsCurrent(token)" in html
    assert "if (!contentRenderIsCurrent(renderToken)) return false;" in html
    assert "const renderToken = beginContentRender(state.activeTab);" in html
    assert "const renderToken = takeContentRenderToken('sources');" in html
    assert "const renderToken = takeContentRenderToken('governance');" in html
    sources = html[html.index("async function renderSources()"):html.index("async function selectAgentCard", html.index("async function renderSources()"))]
    assert "renderSourcesView(sourcesResult, rawResult, agentData, bindingsResult, renderToken);" in sources
    assert "setContent(`<div class=\"view-heading\"><span class=\"eyebrow\">Sources</span>" in sources
    governance = html[html.index("async function renderGovernance()"):html.index("async function selectGovernanceGroup", html.index("async function renderGovernance()"))]
    assert "setContent(`<div class=\"view-heading\"><span class=\"eyebrow\">Governance</span>" in governance
    assert "renderGovernanceSub();" in governance
    assert "if (!contentRenderIsCurrent(renderToken)) return;" in governance


def test_edge_async_tab_switch_keeps_latest_root_content(tmp_path: Path) -> None:
    """Reproduce the slow sources response after switching to governance."""
    edge = _edge_executable()
    if edge is None:
        pytest.skip("Microsoft Edge is not installed for the async tab probe")

    probe = r'''
<pre id="async-tab-probe"></pre>
<script>
(async () => {
  let releaseSourceAgents;
  let listAgentsCalls = 0;
  window.callApi = (method) => {
    if (method === 'list_agents' && ++listAgentsCalls === 1) {
      return new Promise(resolve => { releaseSourceAgents = resolve; });
    }
    if (method === 'list_share_groups') return Promise.resolve({groups: []});
    return Promise.resolve({agents: [], residuals: [], sources: [], coverage: {}, bindings: []});
  };
  state.report = {summary: {}, findings: [], audit_state: 'completed', health_score: null};
  switchTab('sources');
  switchTab('governance');
  await new Promise(resolve => setTimeout(resolve, 20));
  const before = document.querySelector('#content h2')?.textContent || '';
  releaseSourceAgents({agents: [], residuals: []});
  await new Promise(resolve => setTimeout(resolve, 40));
  const after = document.querySelector('#content h2')?.textContent || '';
  document.getElementById('async-tab-probe').textContent = JSON.stringify({
    activeTab: state.activeTab, before, after,
    governanceVisible: document.getElementById('content').textContent.includes('治理台'),
    sourcesVisible: document.getElementById('content').textContent.includes('数据源与代理'),
  });
})();
</script>
'''
    page = tmp_path / "interactive-async-tab.html"
    page.write_text(render_interactive_html().replace("</body>", probe + "</body>"), encoding="utf-8")
    completed = subprocess.run(
        [
            str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
            "--disable-background-networking", "--user-data-dir=" + str(tmp_path / "edge-profile"),
            "--virtual-time-budget=1000", "--dump-dom", "--window-size=1200,800", page.as_uri(),
        ],
        capture_output=True, check=False, encoding="utf-8", errors="replace", text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    match = re.search(r'<pre id="async-tab-probe">(.*?)</pre>', completed.stdout, re.DOTALL)
    assert match, completed.stderr[-1000:]
    payload = json.loads(unescape(match.group(1)))
    assert payload["activeTab"] == "governance"
    assert payload["before"] == "治理台"
    assert payload["after"] == "治理台"
    assert payload["governanceVisible"] is True
    assert payload["sourcesVisible"] is False


def test_overview_narrow_layout_stacks_kpis_and_governance_stages() -> None:
    html = render_interactive_html()
    mobile_start = html.rindex("@media (max-width: 640px)")
    mobile = html[mobile_start:html.index("</style>", mobile_start)]

    assert "html, body, .app-shell, .main-wrapper, .content, .dashboard-view, .dashboard-main" in mobile
    assert "min-inline-size: 0;" in mobile
    assert ".dashboard-main > *, #content .overview-view section { min-width: 0; max-width: 100%; }" in mobile
    assert "overflow-x: hidden" not in mobile
    assert "#content .overview-view .kpi-grid { grid-template-columns: minmax(0, 1fr);" in mobile
    assert "#content .overview-view .governance-timeline { grid-template-columns: minmax(0, 1fr);" in mobile
    assert "#content .overview-view .governance-stage { min-width: 0; width: 100%;" in mobile


def test_agent_display_name_uses_provider_fallback_for_history_rows_and_rail() -> None:
    html = render_interactive_html()
    start = html.index("function agentDisplayName")
    end = html.index("function agentIdentityDetail", start)
    display_name = html[start:end]

    assert "const fallbackLabel = readableAgentPart(fallback, id);" in display_name
    assert "readableAgentPart(alias, id) || readableAgentPart(program, id) || readableAgentPart(provider, id)" in display_name
    assert "|| fallbackLabel" in display_name
    assert "if (/^codex$/i.test(label)) label = 'Codex';" in display_name
    assert html.count("agentDisplayName(owner, session.provider || '未知 Agent')") >= 2
    assert "agentDisplayName(id, item.provider || '未知 Agent')" in html


def test_initial_hash_navigation_and_shared_sources_rail_have_explicit_state_paths() -> None:
    html = render_interactive_html()

    assert "const guiTabHashAliases = {memory: 'neurons'};" in html
    assert "const hashTab = guiTabFromHash();" in html
    assert html.count("syncNavigationState(state.activeTab);") >= 2
    assert "window.addEventListener('hashchange'" in html

    start = html.rindex("function renderSourcesRail()")
    end = html.index("function renderSourcesView", start)
    rail = html[start:end]
    assert "if (isShareGroupScope())" in rail
    assert "共享治理 · 已激活" in rail
    assert "const declaredProgramCount = optionalFiniteNumber(agentCardsData?.program_member_count);" in rail
    assert "const endpointMemberCount = optionalFiniteNumber(agentCardsData?.member_count ?? agentCardsData?.endpoint_member_count);" in rail
    assert "const connectionCount = endpointMemberCount === null ? programCount + otherCount : Math.max(0, endpointMemberCount);" in rail
    assert "选择 Agent 卡片后，才切换到该 Agent 的详情。" in rail
    assert "share_group_id:" in rail


def _edge_executable() -> Path | None:
    for candidate in (
        Path(r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"),
        Path(r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"),
    ):
        if candidate.exists():
            return candidate
    return None


def test_edge_narrow_layout_hash_and_shared_sources_rail(tmp_path: Path) -> None:
    """Probe final CSS/JS in Chromium when Edge is installed, without a GUI host."""
    if os.environ.get("MEMORYGUARD_EDGE_PROBE") != "1":
        pytest.skip("run one-shot Edge probe only with MEMORYGUARD_EDGE_PROBE=1")
    edge = _edge_executable()
    if edge is None:
        pytest.skip("Microsoft Edge is not installed for the optional DOM probe")

    probe = r'''
<pre id="layout-probe"></pre>
<script>
(() => {
  const content = document.getElementById('content');
  content.innerHTML = `<div class="dashboard-view overview-view"><div class="dashboard-main">
    <div class="kpi-grid"><div class="kpi">1</div><div class="kpi">2</div><div class="kpi">3</div><div class="kpi">4</div></div>
    <section><div class="governance-timeline">${Array.from({length: 6}, (_, index) => `<div class="governance-stage">${index + 1}. stage</div>`).join('')}</div></section>
  </div></div>`;
  const kpi = document.querySelector('.kpi-grid');
  const timeline = document.querySelector('.governance-timeline');
  const initialTab = state.activeTab;
  const initialActive = document.querySelector('.nav-item.active')?.dataset.tab || '';
  const memoryHashTab = guiTabFromHash('#memory');
  syncNavigationState(memoryHashTab);
  const memoryActive = document.querySelector('.nav-item.active')?.dataset.tab || '';
  syncNavigationState('history');
  const historyActive = document.querySelector('.nav-item.active')?.dataset.tab || '';
  syncNavigationState(initialTab);
  const memberIds = Array.from({length: 6}, (_, index) => `member-${index + 1}`);
  state.activeTab = 'sources';
  dataPageMode = 'multi_agent_shared_mcp';
  activeShareGroupId = 'shared-6767d0c38b9cc5f1';
  activeScopeMemberIds = memberIds;
  activeAgentInstanceId = 'runtime-principal';
  selectedSourceRecord = null;
  agentCardsData = {agents: memberIds.map((instance_id, index) => ({instance_id, provider: `Provider ${index + 1}`}))};
  governanceScopeState = {status: 'active', share_group_id: activeShareGroupId, members: memberIds};
  state.governanceSnapshot = {counts: {active_memories: 9}, members: memberIds};
  renderSourcesRail();
  const timelineRect = timeline.getBoundingClientRect();
  const stagesFit = Array.from(document.querySelectorAll('.governance-stage')).every(stage => {
    const rect = stage.getBoundingClientRect();
    return rect.left >= timelineRect.left && rect.right <= timelineRect.right;
  });
  const columns = element => getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).filter(Boolean).length;
  document.getElementById('layout-probe').textContent = JSON.stringify({
    initialTab,
    initialActive,
    memoryHashTab,
    memoryActive,
    historyActive,
    kpiColumns: columns(kpi),
    timelineColumns: columns(timeline),
    stagesFit,
    viewport: document.documentElement.clientWidth,
    documentWidth: document.documentElement.scrollWidth,
    rail: document.getElementById('status-rail-content').textContent,
  });
})();
</script>
'''
    page = tmp_path / "interactive-probe.html"
    page.write_text(render_interactive_html().replace("</body>", probe + "</body>"), encoding="utf-8")
    completed = subprocess.run(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--disable-background-networking",
            "--user-data-dir=" + str(tmp_path / "edge-profile"),
            "--virtual-time-budget=100",
            "--dump-dom",
            "--window-size=420,1000",
            page.as_uri() + "#sources",
        ],
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    match = re.search(r'<pre id="layout-probe">(.*?)</pre>', completed.stdout, re.DOTALL)
    assert match, completed.stderr[-1000:]
    payload = json.loads(unescape(match.group(1)))

    assert payload["initialTab"] == "sources"
    assert payload["initialActive"] == "sources"
    assert payload["memoryHashTab"] == "neurons"
    assert payload["memoryActive"] == "neurons"
    assert payload["historyActive"] == "history"
    assert payload["kpiColumns"] == 1
    assert payload["timelineColumns"] == 1
    assert payload["stagesFit"] is True
    assert payload["documentWidth"] <= payload["viewport"]
    assert "共享治理 · 已激活" in payload["rail"]
    assert "6 个" in payload["rail"]
    assert "未选择 Agent" not in payload["rail"]
