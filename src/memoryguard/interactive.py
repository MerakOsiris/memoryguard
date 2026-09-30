"""MemoryGuard 交互式本地治理面板。

界面通过 pywebview JS API 调用本地 Python 后端，完成只读审计、
记忆神经图浏览、人工治理、修复计划、应用与撤销。
"""

from __future__ import annotations


def render_interactive_html() -> str:
    """返回自包含 CSS/JS 的交互式治理面板 HTML。"""
    return _HTML_TEMPLATE


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="memoryguard-icon.png">
<title>MemoryGuard · 本地记忆治理</title>
<script src="cytoscape.min.js"></script>
<style>
:root {
  color-scheme: dark;
  --bg: #040b09;
  --bg-raised: #08120f;
  --panel: rgba(10, 25, 21, 0.86);
  --panel-solid: #0b1a16;
  --panel-bright: #10251f;
  --fg: #e4f5ef;
  --muted: #78988d;
  --faint: #48685e;
  --line: rgba(110, 231, 196, 0.16);
  --line-strong: rgba(110, 231, 196, 0.34);
  --accent: #6ee7c4;
  --accent-bright: #bcffeb;
  --accent-soft: rgba(110, 231, 196, 0.10);
  --red: #ff7d88;
  --orange: #e9bb64;
  --green: #6ee7c4;
  --blue: #6ee7c4;
  --purple: #9be8d4;
  --shadow: 0 24px 70px rgba(0, 0, 0, 0.32);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  position: relative; overflow: hidden;
  background:
    radial-gradient(circle at 14% 12%, rgba(48, 170, 133, 0.10), transparent 30rem),
    radial-gradient(circle at 84% 82%, rgba(78, 150, 125, 0.07), transparent 34rem),
    var(--bg);
  color: var(--fg);
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  line-height: 1.55;
}
body::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .42;
  background-image:
    linear-gradient(var(--line) 1px, transparent 1px),
    linear-gradient(90deg, var(--line) 1px, transparent 1px);
  background-size: 56px 56px;
  mask-image: radial-gradient(circle at center, black 0, transparent 78%);
}
button, input, select { font: inherit; }
button:focus-visible, [role="tab"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
code {
  padding: .15em .4em; border: 1px solid var(--line); border-radius: 5px;
  background: rgba(110, 231, 196, .06); color: #b9d8ce; font-size: .88em;
  overflow-wrap: anywhere;
}
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(110, 231, 196, .22); border-radius: 8px; }
::-webkit-scrollbar-track { background: transparent; }

/* 三栏布局：侧栏 + 主工作区 + 状态栏 */
.app-shell { display: flex; height: 100%; overflow: hidden; }

/* 左侧导航 224px */
.sidebar {
  position: relative; z-index: 10; width: 224px; flex: none;
  display: flex; flex-direction: column; padding: 18px 0;
  border-right: 1px solid var(--line); background: rgba(4, 11, 9, .88);
  backdrop-filter: blur(20px);
}
.sidebar-brand { display: flex; align-items: center; gap: 12px; padding: 0 20px 18px; }
.brand-orb {
  position: relative; width: 28px; height: 28px; flex: none; border-radius: 50%;
  border: 1px solid rgba(188, 255, 235, .74);
  background: radial-gradient(circle at 38% 34%, #e4fff7 0 6%, var(--accent) 8% 19%, #163d33 44%, #07120f 72%);
  box-shadow: 0 0 20px rgba(110, 231, 196, .35), inset 0 0 12px rgba(110, 231, 196, .38);
}
.brand-orb::before, .brand-orb::after {
  content: ""; position: absolute; top: 50%; left: 50%; height: 1px;
  transform-origin: left; background: linear-gradient(90deg, var(--accent), transparent);
}
.brand-orb::before { width: 18px; transform: rotate(35deg); }
.brand-orb::after { width: 15px; transform: rotate(150deg); }
.brand-copy strong { display: block; font-size: 15px; letter-spacing: .06em; }
.brand-copy span { display: block; color: var(--muted); font-size: 10px; letter-spacing: .18em; text-transform: uppercase; }
.sidebar-nav { flex: 1; padding: 0 10px; overflow-y: auto; }
.nav-section-label { padding: 14px 10px 6px; color: var(--faint); font-size: 9px; letter-spacing: .14em; text-transform: uppercase; }
.nav-item {
  position: relative; display: flex; align-items: center; gap: 9px; padding: 9px 10px; margin-bottom: 2px;
  color: var(--muted); cursor: pointer; font-size: 12px; border-radius: 8px; transition: all .16s ease;
}
.nav-item::before { content: ""; width: 5px; height: 5px; border: 1px solid var(--faint); border-radius: 50%; transition: all .16s ease; flex: none; }
.nav-item:hover { color: var(--fg); background: rgba(110, 231, 196, .04); }
.nav-item.active { color: var(--accent-bright); background: rgba(110, 231, 196, .10); }
.nav-item.active::before { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 10px var(--accent); }
.nav-item .count { min-width: 18px; text-align: right; color: var(--faint); font-size: 10px; flex: 1; }
.sidebar-footer { padding: 12px 20px 0; border-top: 1px solid var(--line); }
.reader-toggle { display: grid; gap: 8px; margin-bottom: 12px; }
.reader-toggle-label { color: var(--faint); font-size: 9px; letter-spacing: .14em; text-transform: uppercase; }
.reader-toggle-buttons { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.reader-toggle button { padding: 7px 8px; border: 1px solid var(--line); border-radius: 8px; background: rgba(110,231,196,.04); color: var(--muted); font-size: 11px; cursor: pointer; }
.reader-toggle button.active { color: var(--accent-bright); border-color: rgba(110,231,196,.42); background: rgba(110,231,196,.12); }
.local-badge { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 10px; }
.local-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }

/* CodeGraph is a separate projection surface.  It intentionally does not
   reuse neuron-shell/neuron-canvas styles or state. */
.codegraph-shell { display: grid; gap: 14px; }
.codegraph-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.codegraph-toolbar h2 { font-size: 16px; }
.codegraph-toolbar p { margin-top: 4px; color: var(--muted); font-size: 11px; }
.codegraph-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.codegraph-controls label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; }
.codegraph-controls select { min-height: 32px; padding: 6px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel-solid); color: var(--fg); }
.codegraph-stage { position: relative; min-height: 560px; overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: radial-gradient(circle at 50% 45%, rgba(99,179,237,.12), transparent 46%), #06101a; }
.codegraph-stage::before { content: ""; position: absolute; inset: 0; pointer-events: none; background-image: linear-gradient(rgba(99,179,237,.12) 1px, transparent 1px), linear-gradient(90deg, rgba(99,179,237,.12) 1px, transparent 1px); background-size: 42px 42px; mask-image: radial-gradient(circle at center, black, transparent 84%); }
.codegraph-canvas { position: absolute; inset: 0; z-index: 1; }
.codegraph-legend { position: absolute; z-index: 2; left: 14px; bottom: 14px; display: flex; gap: 10px; flex-wrap: wrap; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: rgba(4,11,9,.72); color: var(--muted); font-size: 10px; }
.codegraph-dot { display: inline-block; width: 8px; height: 8px; margin-right: 4px; border-radius: 50%; background: #63b3ed; box-shadow: 0 0 8px rgba(99,179,237,.65); }
.codegraph-dot.symbol { background: #f6ad55; box-shadow: 0 0 8px rgba(246,173,85,.65); }
.codegraph-stats { display: flex; gap: 8px; flex-wrap: wrap; }
.codegraph-stat { min-width: 92px; padding: 9px 11px; border: 1px solid var(--line); border-radius: 8px; background: rgba(99,179,237,.06); }
.codegraph-stat strong { display: block; color: var(--accent-bright); font-size: 18px; }
.codegraph-stat span { color: var(--muted); font-size: 10px; }

/* 主工作区 */
.main-wrapper { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
.topbar {
  position: relative; z-index: 30; min-height: 56px; padding: 0 24px;
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  border-bottom: 1px solid var(--line); background: rgba(4, 11, 9, .64); backdrop-filter: blur(14px);
}
.topbar-left { display: flex; align-items: center; gap: 16px; min-width: 0; }
.topbar-right { display: flex; align-items: center; gap: 10px; flex: none; }
.ws-path { max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.health-badge {
  display: inline-flex; align-items: center; gap: 7px; padding: 5px 10px;
  color: var(--fg); border: 1px solid var(--line); border-radius: 999px;
  background: rgba(110, 231, 196, .06); font-size: 12px; font-weight: 600;
}
.health-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; box-shadow: 0 0 10px currentColor; }
.btn {
  min-height: 34px; padding: 7px 13px; border: 1px solid var(--line-strong); border-radius: 8px;
  background: rgba(110, 231, 196, .04); color: var(--fg); cursor: pointer; font-size: 12px;
  transition: transform .16s ease, border-color .16s ease, background .16s ease, box-shadow .16s ease;
}
.btn:hover { transform: translateY(-1px); border-color: rgba(110, 231, 196, .62); background: rgba(110, 231, 196, .09); }
.btn:active { transform: translateY(0); }
.btn-primary { border-color: var(--accent); background: var(--accent); color: #062019; font-weight: 700; box-shadow: 0 0 18px rgba(110, 231, 196, .12); }
.btn-primary:hover { background: var(--accent-bright); box-shadow: 0 0 24px rgba(110, 231, 196, .22); }
.btn-danger { border-color: rgba(255, 125, 136, .48); color: var(--red); }
.btn-danger:hover { border-color: var(--red); background: rgba(255, 125, 136, .08); }
.btn-icon { min-width: 32px; padding: 5px 8px; }
.btn:disabled { opacity: .38; cursor: not-allowed; transform: none; }

.content {
  position: relative; z-index: 1; flex: 1; width: 100%;
  overflow: auto; padding: 24px 28px 38px;
}

/* 右侧状态栏 280px */
.status-rail {
  width: 280px; flex: none; padding: 20px 18px; overflow-y: auto;
  border-left: 1px solid var(--line); background: rgba(4, 11, 9, .72); backdrop-filter: blur(14px);
}
.status-rail h3 { margin-bottom: 14px; font-size: 10px; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
.status-item {
  display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 12px; margin-bottom: 7px;
  border: 1px solid var(--line); border-radius: 10px; background: rgba(110, 231, 196, .03);
  cursor: pointer; transition: border-color .16s ease, background .16s ease, transform .16s ease;
}
.status-item:hover { border-color: var(--line-strong); background: rgba(110, 231, 196, .07); transform: translateX(-2px); }
.status-item .status-label { color: var(--muted); font-size: 12px; }
.status-item .status-num { font-size: 18px; font-weight: 560; color: var(--accent-bright); }
.status-item.alert .status-num { color: var(--orange); }
.status-item.danger .status-num { color: var(--red); }
.status-item.zero .status-num { color: var(--faint); }
.status-rail .rail-link { display: block; margin-top: 12px; padding: 8px 12px; color: var(--muted); font-size: 11px; border: 1px solid var(--line); border-radius: 8px; text-align: center; cursor: pointer; transition: all .16s ease; }
.status-rail .rail-link:hover { color: var(--accent); border-color: var(--line-strong); }

/* 概念图：Governance Flow 事件卡 */
.flow-canvas { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
.flow-card {
  position: relative; overflow: hidden; padding: 18px; min-height: 130px;
  border: 1px solid var(--line); border-radius: 14px; cursor: pointer;
  background: linear-gradient(145deg, rgba(15, 35, 29, .82), rgba(7, 18, 15, .78));
  transition: border-color .16s ease, transform .16s ease;
}
.flow-card:hover { transform: translateY(-2px); }
.flow-card::before { content: ""; position: absolute; top: 0; left: 0; width: 3px; height: 100%; }
.flow-card.cyan::before { background: var(--accent); box-shadow: 0 0 14px var(--accent); }
.flow-card.gray::before { background: var(--faint); }
.flow-card.amber::before { background: var(--orange); box-shadow: 0 0 14px var(--orange); }
.flow-card.red::before { background: var(--red); box-shadow: 0 0 14px var(--red); }
.flow-card .flow-kicker { font-size: 9px; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
.flow-card.cyan .flow-kicker { color: var(--accent); }
.flow-card.amber .flow-kicker { color: var(--orange); }
.flow-card.red .flow-kicker { color: var(--red); }
.flow-card .flow-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.flow-card .flow-body { color: var(--muted); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.flow-card .flow-time { margin-top: 8px; color: var(--faint); font-size: 10px; }
.flow-card.empty { cursor: default; }
.flow-card.empty:hover { transform: none; }
.flow-arrow { display: flex; align-items: center; justify-content: center; color: var(--faint); font-size: 20px; }
.view-heading { margin-bottom: 18px; }
.eyebrow { color: var(--accent); font-size: 10px; letter-spacing: .18em; text-transform: uppercase; }
.view-heading h2 { margin-top: 4px; font-size: 24px; font-weight: 560; letter-spacing: -.03em; }
.view-heading p { max-width: 720px; margin-top: 5px; color: var(--muted); font-size: 13px; }
.card {
  position: relative; overflow: hidden; padding: 20px; margin-bottom: 14px;
  border: 1px solid var(--line); border-radius: 14px;
  background: linear-gradient(145deg, rgba(15, 35, 29, .82), rgba(7, 18, 15, .78));
  box-shadow: 0 18px 50px rgba(0, 0, 0, .10);
}
.card::before { content: ""; position: absolute; top: -2px; left: 22px; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 12px var(--accent); }
.card h2 { margin-bottom: 14px; font-size: 13px; font-weight: 650; letter-spacing: .04em; }
.card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.card-head h2 { margin: 0; }
.card-head p { color: var(--muted); font-size: 12px; }

/* 总览 */
.overview-hero { display: grid; grid-template-columns: minmax(220px, .75fr) minmax(0, 2fr); gap: 14px; margin-bottom: 14px; }
.health-orbit { min-height: 238px; display: grid; place-items: center; }
.health-ring {
  --health-angle: 0deg; position: relative; width: 164px; height: 164px; display: grid; place-items: center;
  border-radius: 50%; background: conic-gradient(var(--accent) var(--health-angle), rgba(110, 231, 196, .08) 0);
  box-shadow: 0 0 40px rgba(110, 231, 196, .08);
}
.health-ring::before { content: ""; position: absolute; inset: 10px; border-radius: 50%; background: radial-gradient(circle, #10251f, #07120f 72%); border: 1px solid var(--line); }
.health-ring::after { content: ""; position: absolute; inset: -8px; border: 1px dashed rgba(110, 231, 196, .20); border-radius: 50%; }
.health-ring-content { position: relative; z-index: 1; text-align: center; }
.health-ring-content strong { display: block; color: var(--accent-bright); font-size: 44px; font-weight: 530; line-height: 1; letter-spacing: -.05em; }
.health-ring-content span { color: var(--muted); font-size: 11px; letter-spacing: .14em; }
.metrics { display: grid; grid-template-columns: repeat(2, minmax(130px, 1fr)); gap: 10px; }
.metric {
  position: relative; min-height: 112px; padding: 18px; overflow: hidden;
  border: 1px solid var(--line); border-radius: 11px; background: rgba(110, 231, 196, .035);
}
.metric::after { content: ""; position: absolute; right: -12px; bottom: -12px; width: 58px; height: 58px; border: 1px solid var(--line); border-radius: 50%; box-shadow: 0 0 24px rgba(110, 231, 196, .05); }
.metric .num { color: var(--accent-bright); font-size: 30px; font-weight: 530; line-height: 1.2; letter-spacing: -.04em; }
.metric .label, .metric > div:last-child { margin-top: 8px; color: var(--muted); font-size: 11px; }
.overview-grid { display: grid; grid-template-columns: 1.3fr .7fr; gap: 14px; }
.scan-list { display: grid; gap: 10px; }
.scan-row { display: flex; justify-content: space-between; gap: 16px; padding-bottom: 9px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; }
.scan-row:last-child { border-bottom: 0; padding-bottom: 0; }
.scan-row strong { color: var(--fg); font-weight: 500; text-align: right; }

/* 状态与列表 */
.chips { display: flex; flex-wrap: wrap; gap: 7px; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 8px;
  border: 1px solid var(--line); border-radius: 999px; color: var(--muted); background: rgba(255,255,255,.018);
  font-size: 10px; line-height: 1.5;
}
.chip::before { content: ""; width: 4px; height: 4px; border-radius: 50%; background: currentColor; box-shadow: 0 0 7px currentColor; }
.chip-critical, .chip-high { color: var(--red); border-color: rgba(255,125,136,.28); }
.chip-medium { color: var(--orange); border-color: rgba(233,187,100,.28); }
.chip-low, .chip-confirmed { color: var(--accent); }
.chip-info, .chip-tentative { color: #9fc4b8; }
.modal-backdrop { position: fixed; inset: 0; z-index: 2000; display: grid; place-items: center; padding: 22px; background: rgba(0,0,0,.58); backdrop-filter: blur(7px); }
.modal-card { width: min(720px, 96vw); max-height: min(720px, 88vh); overflow: auto; border: 1px solid var(--line-strong); border-radius: 18px; background: rgba(5, 18, 14, .98); box-shadow: 0 24px 80px rgba(0,0,0,.48); }
.modal-head { padding: 18px 20px 12px; border-bottom: 1px solid var(--line); }
.modal-head h3 { margin: 0 0 6px; font-size: 18px; }
.modal-head p { margin: 0; color: var(--muted); font-size: 12px; }
.modal-body { padding: 14px 20px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 14px 20px 18px; border-top: 1px solid var(--line); }
.release-option { display: flex; gap: 12px; align-items: flex-start; padding: 12px 13px; margin-bottom: 9px; border: 1px solid var(--line); border-radius: 12px; cursor: pointer; background: rgba(255,255,255,.018); }
.release-option:hover { border-color: var(--line-strong); background: rgba(110,231,196,.045); }
.release-option input { margin-top: 3px; accent-color: var(--accent); }
.release-title { font-weight: 700; font-size: 13px; }
.release-meta { margin-top: 4px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.finding-item, .plan-item {
  position: relative; padding: 15px 16px 15px 22px; margin-bottom: 9px; cursor: pointer;
  border: 1px solid var(--line); border-radius: 11px; background: rgba(10, 26, 21, .70);
  transition: border-color .16s ease, background .16s ease, transform .16s ease;
}
.finding-item::before, .plan-item::before { content: ""; position: absolute; top: 21px; left: 10px; width: 4px; height: 4px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.finding-item:hover { transform: translateX(2px); border-color: var(--line-strong); background: rgba(16, 39, 32, .72); }
.finding-item.sev-high::before, .finding-item.sev-critical::before, .plan-item.failed::before { background: var(--red); box-shadow: 0 0 8px var(--red); }
.finding-item.sev-medium::before { background: var(--orange); box-shadow: 0 0 8px var(--orange); }
.finding-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.finding-rule { font-size: 12px; font-weight: 650; }
.finding-evidence { margin-top: 6px; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.finding-toggle { color: var(--accent); font-size: 11px; white-space: nowrap; }
.finding-detail { margin-top: 12px; padding: 12px 0 0; border-top: 1px solid var(--line); font-size: 12px; }
.row { display: flex; align-items: flex-start; gap: 10px; margin: 5px 0; }
.key { min-width: 72px; color: var(--muted); }
.finding-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.empty-state { min-height: 210px; display: grid; place-items: center; text-align: center; color: var(--muted); }
.empty-state .empty-orb { width: 44px; height: 44px; margin: 0 auto 14px; border: 1px solid var(--line-strong); border-radius: 50%; box-shadow: 0 0 24px rgba(110,231,196,.10), inset 0 0 18px rgba(110,231,196,.06); }

/* 对象表 */
.table-shell { overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 11px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .10em; text-transform: uppercase; }
tbody tr { transition: background .15s ease; }
tbody tr:hover { background: rgba(110, 231, 196, .035); }
tbody tr:last-child td { border-bottom: 0; }
.filter-bar { display: flex; gap: 8px; margin-bottom: 14px; }
.scope-tabs {
  display: flex; gap: 4px; padding: 4px; margin-bottom: 16px;
  border: 1px solid var(--line); border-radius: 10px;
  background: rgba(10, 26, 21, .50);
}
.scope-tab {
  flex: 1; padding: 8px 14px; border-radius: 7px; text-align: center;
  color: var(--muted); cursor: pointer; font-size: 12px; letter-spacing: .04em;
  transition: all .16s ease;
}
.scope-tab:hover { color: var(--fg); background: rgba(110, 231, 196, .05); }
.scope-tab.active {
  color: var(--accent-bright); background: rgba(110, 231, 196, .12);
  box-shadow: inset 0 0 0 1px rgba(110, 231, 196, .35);
}
.scope-tab .count { display: inline-block; min-width: 18px; margin-left: 6px;
  padding: 1px 6px; border-radius: 999px; background: rgba(110, 231, 196, .12);
  color: var(--accent); font-size: 10px; }
.scope-select {
  width: 100%; padding: 10px 12px; border: 1px solid var(--line-strong); border-radius: 10px;
  color: var(--fg); background: rgba(7, 24, 18, .96); font: inherit; outline: none;
}
.scope-select:focus { box-shadow: 0 0 0 2px rgba(110,231,196,.16); }
.surface-row {
  display: grid; grid-template-columns: auto 1fr auto auto auto; gap: 10px; align-items: center;
  padding: 10px 12px; margin-bottom: 6px;
  border: 1px solid var(--line); border-radius: 9px;
  background: rgba(10, 26, 21, .50); transition: border-color .15s ease;
}
.surface-row:hover { border-color: var(--line-strong); }
.surface-row .surface-icon { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.surface-row .surface-icon.found { background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.surface-row .surface-icon.missing { background: var(--faint); }
.surface-row .surface-icon.unsupported { background: var(--orange); }
.surface-row .surface-icon.permission_denied { background: var(--red); }
.surface-row .surface-path { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.surface-row .surface-path code { font-size: 11px; }
.surface-row .surface-meta { font-size: 10px; color: var(--muted); letter-spacing: .04em; }
 /* v3.2 Agent 卡片 */
 .agent-cards {
   display: flex; gap: 8px; padding: 8px 0; margin-bottom: 16px;
   overflow-x: auto; flex-wrap: wrap;
 }
 .agent-card {
   flex: none; padding: 12px 16px; border: 1px solid var(--line); border-radius: 12px;
   background: rgba(10, 26, 21, .50); cursor: pointer;
   transition: all .16s ease; min-width: 120px;
 }
 .agent-card:hover { border-color: var(--line-strong); background: rgba(16, 39, 32, .60); }
 .agent-card.active {
   border-color: var(--accent); background: rgba(110, 231, 196, .12);
   box-shadow: 0 0 18px rgba(110, 231, 196, .15);
 }
 .agent-card .agent-name { font-size: 13px; font-weight: 600; color: var(--fg); }
 .agent-card .agent-meta { font-size: 10px; color: var(--muted); margin-top: 4px; }
 .agent-card .agent-badge {
   display: inline-block; padding: 1px 6px; border-radius: 999px;
   background: rgba(110, 231, 196, .12); color: var(--accent);
   font-size: 9px; margin-top: 4px;
 }
 .agent-card.add-card { border-style: dashed; color: var(--muted); }
.knowledge-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 10px;
}
.memory-card { margin: 10px 0; padding: 13px 14px; border: 1px solid rgba(110,231,196,.16); border-radius: 10px; background: rgba(3,20,15,.46); }
.memory-card-top { display:flex; justify-content:space-between; gap:10px; align-items:center; }
.memory-card p { margin:8px 0; color:var(--muted); white-space:pre-wrap; }
.muted { color:var(--faint); font-size:11px; }
.raw-content { max-height:420px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--fg); font:12px/1.6 var(--mono, monospace); }
.knowledge-card {
  position: relative; min-width: 0; padding: 15px; overflow: hidden;
  border: 1px solid var(--line); border-radius: 12px;
  background: radial-gradient(circle at 16px 16px, rgba(110,231,196,.12), transparent 46px), rgba(10,26,21,.52);
}
.knowledge-card::before {
  content: ""; position: absolute; top: 15px; left: 14px; width: 7px; height: 7px;
  border-radius: 50%; background: var(--accent); box-shadow: 0 0 11px var(--accent);
}
.knowledge-card.missing::before { background: var(--red); box-shadow: 0 0 11px var(--red); }
.knowledge-title { padding-left: 17px; font-size: 13px; font-weight: 650; color: var(--fg); }
.knowledge-path {
  margin: 10px 0; padding: 8px 10px; overflow: hidden; text-overflow: ellipsis;
  border: 1px solid rgba(110,231,196,.10); border-radius: 8px;
  background: rgba(3,10,8,.46); color: var(--muted); white-space: nowrap;
}
.knowledge-path code { font-size: 10px; }
.knowledge-files { margin-top: 10px; }
.knowledge-files summary { cursor: pointer; color: var(--muted); font-size: 11px; }
.knowledge-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
.plan-item { cursor: default; }
.plan-item.verified::before, .plan-item.applied::before { background: var(--accent); }

/* 神经元画布 */
.neuron-shell {
  position: relative; min-height: calc(100vh - 120px); overflow: hidden;
  border: 1px solid var(--line); border-radius: 18px;
  background: radial-gradient(circle at 50% 48%, rgba(37, 104, 83, .14), transparent 38%), rgba(3, 10, 8, .74);
  box-shadow: var(--shadow), inset 0 0 80px rgba(0, 0, 0, .30);
  animation: neuron-shell-breath 8s ease-in-out infinite;
  isolation: isolate;
  overflow: hidden;
}
.neuron-shell::before {
  content: ""; position: absolute; inset: 0; pointer-events: none; opacity: .28;
  background-image: radial-gradient(circle, rgba(110,231,196,.28) 1px, transparent 1.4px);
  background-size: 24px 24px; mask-image: radial-gradient(circle at center, black, transparent 82%);
  animation: neuron-grid-drift 18s linear infinite;
}
.neuron-shell::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(90deg, transparent 0%, rgba(110,231,196,.14) 48%, transparent 100%);
  mix-blend-mode: screen; opacity: .24;
  transform: skewX(-28deg) translateX(-62%);
  animation: neuron-scanline 7.8s linear infinite;
}
.neuron-stage {
  position: relative; width: 100%; height: calc(100vh - 120px); min-height: 610px;
  overflow: hidden;
}
.neuron-stage::before {
  content: ""; position: absolute; inset: -20%; pointer-events: none; z-index: 1;
  border-radius: 50%;
  border: 1px solid rgba(188, 255, 235, .12);
  background:
    radial-gradient(circle at 28% 22%, rgba(110,231,196,.19), transparent 33%),
    radial-gradient(circle at 72% 74%, rgba(125,211,252,.11), transparent 38%);
  opacity: .52;
  mix-blend-mode: screen;
  animation: neuron-orbit-spin 20s linear infinite;
}
.neuron-stage::after {
  content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 2;
  background-image:
    radial-gradient(circle at 60% 32%, rgba(110,231,196,.06) 0 0.7px, transparent 1.2px),
    radial-gradient(circle at 28% 78%, rgba(129,237,215,.05) 0 0.7px, transparent 1.2px);
  background-size: 24px 24px, 18px 18px;
  opacity: .25;
  animation: neuron-noise-shimmer 4.7s ease-in-out infinite;
  mix-blend-mode: soft-light;
}
.neuron-toolbar {
  position: absolute; z-index: 12; top: 16px; left: 18px; right: 18px;
  display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; pointer-events: none;
}
.neuron-canvas-haze {
  position: absolute; inset: -10%; pointer-events: none; z-index: 2;
  background:
    radial-gradient(circle at 30% 20%, rgba(110,231,196,.08), transparent 22%),
    radial-gradient(circle at 82% 72%, rgba(124,211,255,.06), transparent 26%),
    radial-gradient(circle at 58% 58%, rgba(255,255,255,.02), transparent 32%);
  filter: blur(4px);
  animation: neuron-shell-ripple 16s ease-in-out infinite;
}
.neuron-noise-layer {
  position: absolute; inset: 0; pointer-events: none; z-index: 8;
  background-image: repeating-radial-gradient(circle at 20% 80%, rgba(255,255,255,.05), rgba(255,255,255,.05) 1.2px, transparent 1.2px, transparent 2.4px);
  mix-blend-mode: multiply;
  opacity: .09;
  animation: neuron-noise-flicker 3.2s linear infinite;
}
.neuron-title, .canvas-actions, .neuron-legend, .neuron-stats, .merge-dock { pointer-events: auto; }
.neuron-title { max-width: 390px; }
.neuron-title .eyebrow { display: block; margin-bottom: 5px; }
.neuron-title h2 { font-size: 18px; font-weight: 560; }
.neuron-title p { margin-top: 4px; color: var(--muted); font-size: 11px; }
.canvas-actions { display: flex; gap: 8px; }
.neuron-canvas { position: absolute; inset: 0; z-index: 5; }
.neuron-particles {
  position: absolute; inset: 0; z-index: 6; pointer-events: none;
  overflow: hidden; mix-blend-mode: screen;
}
.neuron-edge-particle {
  display: block; position: absolute; left: 0; top: 0;
  width: 30px; height: 5px; margin: 0; padding: 0; border: 0;
  border-radius: 999px;
  background: linear-gradient(90deg,
    transparent 0%,
    rgba(110,231,196,.14) 16%,
    rgba(110,231,196,.54) 34%,
    rgba(226,255,248,.9) 47%,
    rgba(255,255,255,.96) 50%,
    rgba(226,255,248,.9) 53%,
    rgba(110,231,196,.54) 66%,
    rgba(110,231,196,.14) 84%,
    transparent 100%);
  box-shadow: 0 0 7px rgba(255,255,255,.22), 0 0 13px rgba(110,231,196,.24);
  filter: blur(.35px);
  transform-origin: 50% 50%;
  will-change: transform, opacity;
  transform: translate3d(-9999px, -9999px, 0);
  pointer-events: none;
}
.neuron-stats {
  position: absolute; z-index: 11; left: 18px; bottom: 18px; max-width: calc(100% - 390px);
  display: flex; flex-wrap: wrap; gap: 8px; padding: 8px;
  border: 1px solid var(--line); border-radius: 11px; background: rgba(4, 13, 10, .78); backdrop-filter: blur(14px);
}
.neuron-stat { min-width: 82px; padding: 6px 10px; border-right: 1px solid var(--line); }
.neuron-stat:last-child { border-right: 0; }
.neuron-stat strong { display: block; color: var(--accent-bright); font-size: 16px; font-weight: 550; }
.neuron-stat span { color: var(--muted); font-size: 9px; letter-spacing: .08em; }
.neuron-legend {
  position: absolute; z-index: 11; right: 18px; top: 82px; padding: 10px 12px;
  border: 1px solid var(--line); border-radius: 10px; background: rgba(4, 13, 10, .76); backdrop-filter: blur(12px);
  color: var(--muted); font-size: 10px;
}
.legend-item { display: flex; align-items: center; gap: 7px; margin: 5px 0; }
.legend-node { width: 7px; height: 7px; border: 1px solid var(--accent); border-radius: 50%; box-shadow: 0 0 7px rgba(110,231,196,.48); }
.legend-node.soma { width: 10px; height: 10px; background: rgba(110,231,196,.18); box-shadow: 0 0 12px rgba(110,231,196,.35); }
.legend-node.hub { width: 9px; height: 9px; border-radius: 50%; border-style: dashed; border-color: #7dd3fc; box-shadow: 0 0 10px rgba(125,211,252,.28); }
.legend-node.tentative { border-style: dashed; border-color: var(--orange); box-shadow: none; }
.legend-node.anchor { width: 5px; height: 5px; border: 0; border-radius: 50%; background: rgba(110,231,196,.72); box-shadow: 0 0 8px rgba(110,231,196,.55); }
.legend-edge { width: 16px; height: 0; border-top: 1.5px solid rgba(110,231,196,.55); }
.legend-edge.related { border-top-style: dashed; border-top-color: rgba(110,231,196,.4); }
.legend-edge.shared { border-top-style: dashed; border-top-color: rgba(99,179,237,.7); }
.detail-section { margin: 10px 0 8px; }
.detail-section h4 { margin: 0 0 6px; color: var(--accent); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.detail-path { color: #c5ddd4; font-size: 11px; line-height: 1.55; overflow-wrap: anywhere; }
.claim-list { margin-top: 10px; }
.claim-list h4 { margin: 0 0 6px; color: var(--accent); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
.raw-file-row { cursor: pointer; }
.raw-file-row:hover { border-color: rgba(110,231,196,.45); }
.merge-dock {
  position: absolute; z-index: 15; right: 18px; bottom: 18px; width: 310px; max-height: 220px; overflow: auto;
  padding: 12px; border: 1px solid var(--line); border-radius: 12px;
  background: rgba(4, 13, 10, .84); backdrop-filter: blur(16px); box-shadow: 0 18px 48px rgba(0,0,0,.28);
}
.merge-dock h3 { margin-bottom: 8px; font-size: 11px; font-weight: 650; }
.merge-item { padding: 9px 0; border-top: 1px solid var(--line); }
.merge-item:first-of-type { border-top: 0; }
.merge-copy { color: var(--muted); font-size: 10px; overflow-wrap: anywhere; }
.merge-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 7px; }
.merge-score { color: var(--accent); font-size: 10px; }
.neuron-popover {
  position: absolute; z-index: 30; width: min(360px, calc(100% - 28px)); max-height: min(470px, calc(100% - 32px));
  overflow: auto; padding: 16px; border: 1px solid rgba(110,231,196,.42); border-radius: 14px;
  background: radial-gradient(circle at 20% 16%, rgba(110,231,196,.08), transparent 45%), rgba(7, 22, 17, .94);
  backdrop-filter: blur(22px); box-shadow: 0 24px 74px rgba(0,0,0,.52), 0 0 38px rgba(110,231,196,.20);
  opacity: 0; visibility: hidden; transform: translate(-50%, calc(-100% - 42px)) scale(.96);
  transform-origin: bottom center; transition: opacity .22s ease, transform .22s ease, visibility .22s;
  animation: neuron-popover-glow 2.6s ease-in-out infinite;
}
.neuron-popover.show { opacity: 1; visibility: visible; transform: translate(-50%, calc(-100% - 42px)) scale(1); }
.neuron-popover.below { transform-origin: top center; transform: translate(-50%, 42px) scale(.96); }
.neuron-popover.below.show { transform: translate(-50%, 42px) scale(1); }
.neuron-popover::after {
  content: ""; position: absolute; left: 50%; bottom: -6px; width: 11px; height: 11px;
  transform: translateX(-50%) rotate(45deg); border-right: 1px solid rgba(110,231,196,.42); border-bottom: 1px solid rgba(110,231,196,.42); background: #071611;
}
.neuron-popover.below::after { top: -6px; bottom: auto; border: 0; border-left: 1px solid rgba(110,231,196,.42); border-top: 1px solid rgba(110,231,196,.42); }
.neuron-detail-body { color: #d5ebe3; font-size: 12px; line-height: 1.75; white-space: pre-wrap; overflow-wrap: anywhere; }
.popover-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.popover-head h3 { font-size: 15px; font-weight: 600; }
.popover-kicker { color: var(--accent); font-size: 9px; letter-spacing: .14em; text-transform: uppercase; }
.claim-list { margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--line); }
.claim-list h4 { margin-bottom: 7px; color: var(--muted); font-size: 10px; font-weight: 600; letter-spacing: .08em; }
.claim-item { position: relative; padding: 7px 0 7px 11px; color: #b8d0c8; font-size: 11px; border-bottom: 1px solid rgba(110,231,196,.08); }
.claim-item::before { content: ""; position: absolute; top: 13px; left: 0; width: 4px; height: 4px; border-radius: 50%; background: var(--accent); }
.claim-item:last-child { border-bottom: 0; }

.loading { min-height: 320px; display: grid; place-items: center; color: var(--muted); font-size: 12px; }
.loading::before { content: ""; width: 34px; height: 34px; margin-right: 12px; border: 1px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: pulse-spin 1.2s linear infinite; }
@keyframes neuron-shell-breath {
  0%, 100% { box-shadow: var(--shadow), inset 0 0 80px rgba(0,0,0,.30); }
  50% { box-shadow: 0 24px 86px rgba(0,0,0,.34), inset 0 0 98px rgba(110,231,196,.14); }
}
@keyframes neuron-shell-ripple {
  0%, 100% { transform: scale(0.98); opacity: .26; }
  50% { transform: scale(1.03); opacity: .48; }
}
@keyframes neuron-orbit-spin {
  to { transform: rotate(360deg); }
}
@keyframes neuron-grid-drift {
  0% { background-position: 0 0; }
  50% { background-position: 12px 14px; }
  100% { background-position: 24px 0; }
}
@keyframes neuron-noise-shimmer {
  0%, 100% { opacity: .16; transform: translateY(0); }
  50% { opacity: .32; transform: translateY(4px); }
}
@keyframes neuron-noise-flicker {
  0%, 100% { opacity: .06; }
  20% { opacity: .09; }
  40% { opacity: .03; }
  60% { opacity: .08; }
}
@keyframes neuron-scanline {
  0% { transform: skewX(-28deg) translateX(-140%); opacity: 0; }
  12% { opacity: .28; }
  30%, 100% { transform: skewX(-28deg) translateX(140%); opacity: 0; }
}
@keyframes neuron-popover-glow {
  0%, 100% { border-color: rgba(110,231,196,.42); box-shadow: 0 24px 64px rgba(0,0,0,.46), 0 0 30px rgba(110,231,196,.09); }
  50% { border-color: rgba(188,255,235,.65); box-shadow: 0 30px 82px rgba(0,0,0,.52), 0 0 44px rgba(110,231,196,.24); }
}
@keyframes pulse-spin { to { transform: rotate(360deg); } }

.build-progress {
  min-height: 360px; display: flex; flex-direction: column; justify-content: center;
  gap: 14px; max-width: 520px; margin: 40px auto; padding: 28px 24px;
  border: 1px solid var(--line); border-radius: 14px; background: rgba(7,22,17,.55);
}
.build-progress .bp-kicker { color: var(--accent); font-size: 10px; letter-spacing: .14em; text-transform: uppercase; }
.build-progress h2 { font-size: 18px; font-weight: 600; margin: 0; }
.build-progress .bp-msg { color: var(--muted); font-size: 13px; line-height: 1.6; min-height: 40px; }
.build-progress .bp-bar {
  height: 8px; border-radius: 999px; background: rgba(110,231,196,.12); overflow: hidden;
  border: 1px solid rgba(110,231,196,.18);
}
.build-progress .bp-bar > i {
  display: block; height: 100%; width: 0%; background: linear-gradient(90deg, #2a8f6f, #6ee7c4);
  transition: width .35s ease;
}
.build-progress .bp-meta { display: flex; justify-content: space-between; color: var(--faint); font-size: 11px; }
.build-progress .bp-phases { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.build-progress .bp-phase {
  padding: 3px 8px; border-radius: 999px; border: 1px solid var(--line); font-size: 10px; color: var(--faint);
}
.build-progress .bp-phase.active { border-color: rgba(110,231,196,.5); color: var(--accent); }
.build-progress .bp-phase.done { border-color: rgba(110,231,196,.28); color: #9fd9c4; }
.toast {
  position: fixed; z-index: 100; right: 22px; bottom: 22px; min-width: 220px; padding: 11px 14px;
  border: 1px solid var(--line-strong); border-radius: 10px; background: rgba(7,22,17,.94); box-shadow: var(--shadow);
  color: var(--fg); font-size: 12px; opacity: 0; transform: translateY(8px); pointer-events: none;
  transition: opacity .22s ease, transform .22s ease;
}
.toast.show { opacity: 1; transform: translateY(0); }
.toast.success { border-color: rgba(110,231,196,.52); }
.toast.error { border-color: rgba(255,125,136,.55); }

.topbar, .topbar-left, .topbar-right, .page-actions, .canvas-actions, .codegraph-toolbar, .codegraph-controls, .finding-header, .scope-tabs, .neuron-toolbar { min-width: 0; }
.page-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 0 0 14px; }
.page-actions input, .page-actions select { min-width: 0; flex: 1 1 180px; max-width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-solid); color: var(--fg); }
.canvas-actions, .codegraph-controls { flex-wrap: wrap; }
.finding-header { flex-wrap: wrap; }
.scope-tabs { flex-wrap: wrap; }
.risk-explain { display: grid; gap: 4px; margin-top: 8px; color: var(--muted); font-size: 11px; line-height: 1.55; }
.risk-explain strong { color: var(--fg); font-weight: 650; }
@media (max-width: 1024px) {
  .status-rail { display: none; }
}
@media (max-width: 900px) {
  .sidebar { width: 64px; overflow: hidden; }
  .sidebar-brand .brand-copy, .nav-item .count, .nav-section-label, .sidebar-footer { display: none; }
  .nav-item { justify-content: center; font-size: 0; gap: 0; padding: 9px 6px; overflow: hidden; white-space: nowrap; }
  .nav-item::before { font-size: 9px; }
  .ws-path { display: none; }
  .content { padding: 18px 16px 28px; }
  .overview-grid, .flow-canvas { grid-template-columns: 1fr; }
  .neuron-shell, .neuron-stage { min-height: 680px; height: calc(100vh - 170px); }
  .neuron-stats { max-width: calc(100% - 36px); bottom: 18px; right: 18px; }
  .merge-dock { right: 18px; bottom: 112px; }
  .neuron-toolbar { flex-wrap: wrap; }
  .surface-row { grid-template-columns: minmax(0, 1fr); }
  .knowledge-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 620px) {
  .brand-copy span, .health-badge { display: none; }
  .topbar { flex-wrap: wrap; padding: 8px 12px; gap: 8px; }
  .topbar-right { width: 100%; justify-content: flex-end; flex-wrap: wrap; }
  .topbar-right .btn { padding-inline: 10px; }
  .neuron-title p, .neuron-legend { display: none; }
  .canvas-actions { flex-direction: column; }
  .merge-dock { width: calc(100% - 36px); max-height: 150px; bottom: 130px; }
  .neuron-stat { min-width: 62px; }
  .page-heading .finding-actions, .page-heading .page-actions { width: 100%; }
}

/* v3 投影门控 + 数据源 + 变更记录 */
.projection-gate {
  display: grid; grid-template-columns: 80px 1fr; gap: 22px; align-items: start;
  padding: 28px;
}
.projection-gate .gate-orb {
  width: 56px; height: 56px; margin-top: 6px; border-radius: 50%;
  border: 1px dashed var(--line-strong);
  background: radial-gradient(circle, rgba(110,231,196,.08), transparent 70%);
  box-shadow: 0 0 24px rgba(110,231,196,.10), inset 0 0 18px rgba(110,231,196,.06);
  animation: gate-pulse 2.4s ease-in-out infinite;
}
@keyframes gate-pulse {
  0%, 100% { box-shadow: 0 0 24px rgba(110,231,196,.10), inset 0 0 18px rgba(110,231,196,.06); }
  50% { box-shadow: 0 0 36px rgba(110,231,196,.22), inset 0 0 22px rgba(110,231,196,.10); }
}
.gate-body h3 { font-size: 16px; font-weight: 580; margin-bottom: 8px; }
.gate-reason { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
.gate-warning {
  margin: 14px 0; padding: 12px 14px; border: 1px solid rgba(233,187,100,.32);
  border-radius: 9px; background: rgba(233,187,100,.06); color: var(--fg);
  font-size: 12px; line-height: 1.7;
}
.gate-warning strong { color: var(--orange); }

.raw-file-list { margin-top: 10px; display: grid; gap: 4px; }
.raw-file-row {
  display: flex; align-items: center; gap: 10px; padding: 8px 11px;
  border: 1px solid var(--line); border-radius: 8px; background: rgba(10,26,21,.50);
  cursor: pointer; transition: border-color .15s ease, background .15s ease, transform .15s ease;
}
.raw-file-row:hover { transform: translateX(2px); border-color: var(--line-strong); background: rgba(16,39,32,.72); }
.raw-file-path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.raw-file-path code { font-size: 11px; }
.folder-group { margin: 2px 0; border: 1px solid var(--line); border-radius: 8px; background: rgba(10,26,21,.34); }
.folder-group > .folder-children { padding: 4px 6px 6px 14px; display: grid; gap: 4px; border-top: 1px solid var(--line); }
.folder-row { display: flex; align-items: center; gap: 8px; min-height: 34px; padding: 6px 10px; cursor: pointer; list-style: none; user-select: none; }
.folder-row::-webkit-details-marker { display: none; }
.folder-row::marker { content: ""; }
.folder-caret { width: 0; height: 0; border-left: 5px solid var(--accent); border-top: 4px solid transparent; border-bottom: 4px solid transparent; transition: transform .15s ease; }
.folder-group[open] > .folder-row .folder-caret { transform: rotate(90deg); }
.folder-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg); font-size: 11px; font-weight: 600; }
.folder-count { color: var(--faint); font-size: 10px; }

.raw-file-content {
  margin: 0; padding: 16px; border: 1px solid var(--line); border-radius: 10px;
  background: rgba(4,13,10,.78); color: #cce5dc; font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
  font-size: 12px; line-height: 1.65; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 70vh; overflow: auto;
}
.source-map-table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }
.source-map-table { width: 100%; border-collapse: collapse; min-width: 980px; }
.source-map-table th, .source-map-table td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 12px; }
.source-map-table th { color: var(--muted); font-weight: 700; background: rgba(12,34,27,.72); }
.source-map-table tr:last-child td { border-bottom: 0; }
.projection-source-map { min-width: 0; }
.source-map-toggle-row { display: flex; justify-content: flex-start; margin-top: 12px; }
.source-map-toggle { min-width: 152px; }
.source-map-details { margin-top: 12px; }
.source-map-list { display: grid; gap: 10px; }
.source-map-entry {
  min-width: 0; padding: 13px 14px; border: 1px solid var(--line); border-radius: 10px;
  background: rgba(4,13,10,.42);
}
.source-map-entry.muted-row { opacity: .58; }
.source-map-entry-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; min-width: 0; }
.source-map-entry-name { min-width: 0; }
.source-map-entry-name strong { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.source-map-entry-status { display: flex; flex: none; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.source-map-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 14px; margin-top: 12px; }
.source-map-fields > div { min-width: 0; }
.source-map-fields dt { color: var(--faint); font-size: 10px; letter-spacing: .04em; }
.source-map-fields dd { min-width: 0; margin-top: 3px; color: var(--fg); font-size: 11px; overflow-wrap: anywhere; }
.source-map-fields dd code { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-map-id { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-map-empty { margin-top: 12px; padding: 12px 14px; border: 1px dashed var(--line-strong); border-radius: 10px; color: var(--muted); background: rgba(110,231,196,.035); }
.source-map-empty strong { display: block; margin-bottom: 3px; color: var(--fg); font-size: 12px; }
.source-map-empty p { font-size: 11px; }
@media (max-width: 680px) {
  .source-map-entry-header { display: grid; grid-template-columns: minmax(0, 1fr); }
  .source-map-entry-status { justify-content: flex-start; }
  .source-map-fields { grid-template-columns: minmax(0, 1fr); }
}
.path-cell { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.muted-row { opacity: .58; }
.rule-cockpit-panel { border-color: rgba(110,231,196,.28); }
.rule-create-panel textarea { width: 100%; min-height: 82px; resize: vertical; padding: 11px 12px; border: 1px solid var(--line); border-radius: 9px; background: rgba(4,13,10,.78); color: var(--fg); }
.rule-context-grid { display: grid; grid-template-columns: repeat(3, minmax(160px, 1fr)); gap: 10px; margin-top: 12px; }
.rule-create-result { margin-top: 12px; padding: 10px 12px; border: 1px solid rgba(110,231,196,.34); border-radius: 9px; background: rgba(110,231,196,.06); }
.rule-create-result.error { border-color: rgba(255,125,136,.48); background: rgba(255,125,136,.06); }
.rule-decision-row, .rule-exception-row, .rule-receipt { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; padding: 9px 0; border-top: 1px solid var(--line); }
.rule-decision-row:first-of-type { border-top: 0; }
.rule-decision-row > .muted { flex: 1 1 100%; }
.rule-decision-groups { display: grid; gap: 9px; margin-top: 12px; }
.rule-decision-group { border: 1px solid var(--line); border-radius: 10px; background: rgba(4,13,10,.34); overflow: hidden; }
.rule-decision-group > summary, .rule-decision-subgroup > summary { cursor: pointer; list-style: none; }
.rule-decision-group > summary::-webkit-details-marker, .rule-decision-subgroup > summary::-webkit-details-marker { display: none; }
.rule-decision-group-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 11px 12px; }
.rule-decision-group-head::before { content: '›'; color: var(--accent); transition: transform .16s ease; }
.rule-decision-group[open] > .rule-decision-group-head::before { transform: rotate(90deg); }
.rule-decision-group-title { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; min-width: 0; }
.rule-decision-group-meta { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 6px; color: var(--muted); font-size: 10px; }
.rule-decision-group-body { padding: 0 12px 10px; border-top: 1px solid var(--line); }
.rule-decision-subgroup { margin-top: 8px; border: 1px solid rgba(110,231,196,.13); border-radius: 8px; background: rgba(110,231,196,.025); }
.rule-decision-subgroup > summary { padding: 7px 9px; color: var(--muted); font-size: 10px; }
.rule-decision-subgroup-body { padding: 0 9px 7px; }
.rule-decision-link, .rule-receipts, .rule-exceptions { margin-top: 9px; padding-top: 8px; border-top: 1px solid var(--line); }
.rule-feedback-actions { display: flex; flex-wrap: wrap; gap: 4px; }
.rule-feedback-actions .btn { min-height: 26px; padding: 3px 7px; font-size: 10px; }
.rule-advanced { margin-top: 10px; border: 1px solid rgba(233,187,100,.24); border-radius: 9px; background: rgba(233,187,100,.035); }
.rule-advanced > summary { padding: 8px 10px; color: var(--orange); cursor: pointer; font-size: 11px; list-style-position: inside; }
.rule-advanced-body { padding: 0 10px 10px; }
.rule-advanced-body .muted { font-size: 11px; }
.feedback-error { margin-top: 8px; color: var(--red); font-size: 12px; min-height: 1.4em; }
@media (max-width: 720px) { .rule-context-grid { grid-template-columns: 1fr; } }
/* Seven-page governance dashboard chrome. Existing renderers keep ownership
   of data and actions; this layer gives every view one readable shell. */
.topbar-brand { display: inline-flex; align-items: center; gap: 9px; min-width: 190px; }
.topbar-brand-mark { width: 22px; height: 22px; border: 1px solid var(--accent); border-radius: 7px; background: radial-gradient(circle at 35% 30%, var(--accent-bright), var(--accent) 22%, #0b2b20 60%, #06110d 100%); box-shadow: 0 0 13px rgba(110,231,196,.28); }
.topbar-brand-copy { display: grid; gap: 0; line-height: 1.15; }
.topbar-brand-copy strong { font-size: 13px; letter-spacing: .04em; }
.topbar-brand-copy span { color: var(--muted); font-size: 9px; }
.topbar-context { display: inline-flex; align-items: center; gap: 7px; min-width: 0; color: var(--muted); font-size: 11px; }
.topbar-context::before { content: ""; width: 4px; height: 4px; flex: none; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); }
.page-heading { display: grid; grid-template-columns: auto minmax(0,1fr) auto; align-items: start; gap: 14px; padding: 15px 17px; margin-bottom: 14px; border: 1px solid var(--line); border-radius: 13px; background: linear-gradient(120deg, rgba(16,39,32,.88), rgba(7,18,15,.74)); }
.page-heading::before { content: attr(data-page-index); display: grid; place-items: center; width: 26px; height: 26px; border-radius: 8px; background: var(--accent); color: #062019; font-size: 12px; font-weight: 800; box-shadow: 0 0 15px rgba(110,231,196,.2); }
.page-heading .eyebrow { grid-column: 2; grid-row: 1; margin: 0; color: var(--accent); font-size: 10px; letter-spacing: .1em; }
.page-heading h2 { grid-column: 2; grid-row: 2; margin: -7px 0 0; font-size: 20px; letter-spacing: -.02em; }
.page-heading p { grid-column: 2 / -1; grid-row: 3; margin: 0; max-width: 860px; color: var(--muted); font-size: 11px; }
.page-heading .finding-actions, .page-heading .page-actions { grid-column: 2 / -1; grid-row: 4; margin-top: 8px; }
.page-heading::after { content: attr(data-page-subtitle); grid-column: 3; grid-row: 1 / span 3; align-self: center; max-width: 170px; color: var(--faint); font-size: 10px; text-align: right; }
.nav-item { padding-left: 13px; }
.nav-item::before { width: 5px; height: 5px; border: 1px solid var(--faint); border-radius: 50%; background: transparent; box-shadow: none; content: ""; display: block; flex: none; }
.nav-item.active::before { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 10px var(--accent); }
.status-rail { padding-top: 18px; }
.status-rail h3 { display: flex; align-items: center; gap: 7px; padding-bottom: 11px; border-bottom: 1px solid var(--line); }
.status-rail h3::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 9px var(--accent); }
.rail-warning { margin: 10px 0; padding: 10px 11px; border: 1px solid rgba(233,187,100,.28); border-radius: 9px; background: rgba(233,187,100,.05); color: var(--muted); font-size: 11px; line-height: 1.55; }
.surface-meta { overflow-wrap: anywhere; }
.agent-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; overflow: visible; }
.agent-card { min-width: 0; padding: 13px 14px; }
.agent-card .agent-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.agent-card .finding-actions { opacity: .78; }
.agent-card:hover .finding-actions, .agent-card.active .finding-actions { opacity: 1; }
.codegraph-stage { background: radial-gradient(circle at 50% 45%, rgba(110,231,196,.10), transparent 46%), #06110d; }
.codegraph-stage::before { background-image: linear-gradient(rgba(110,231,196,.09) 1px, transparent 1px), linear-gradient(90deg, rgba(110,231,196,.09) 1px, transparent 1px); }
.codegraph-stat { background: rgba(110,231,196,.05); }
.codegraph-view .dashboard-main { gap: 16px; }
.codegraph-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.codegraph-heading h2 { margin: 0; font-size: 22px; letter-spacing: -.5px; }
.codegraph-heading p { margin-top: 5px; color: var(--muted); font-size: 11px; }
.codegraph-settings { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 12px 0; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.codegraph-settings label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; }
.codegraph-settings select { max-width: 260px; padding: 7px 9px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel-solid); color: var(--fg); }
.codegraph-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 24px; font-size: 11px; color: var(--muted); }
.codegraph-summary strong { color: var(--fg); margin-right: 5px; font-size: 16px; font-variant-numeric: tabular-nums; }
.codegraph-view .codegraph-stage { min-height: 420px; height: clamp(420px, calc(100dvh - 330px), 760px); background: #091714; border-color: rgba(153,195,178,.16); border-radius: 12px; }
.codegraph-view .codegraph-stage::before { background-image: radial-gradient(rgba(153,195,178,.18) .7px, transparent .7px); background-size: 22px 22px; mask-image: none; opacity: .45; }
.codegraph-stage-head { position: absolute; top: 16px; left: 18px; z-index: 2; pointer-events: none; color: #9eb7ad; font-size: 10px; letter-spacing: .03em; }
.codegraph-stage-head strong { display: block; margin-bottom: 5px; color: #d5e7df; font-size: 12px; font-weight: 500; }
.codegraph-view-switch { display: inline-flex; gap: 3px; padding: 3px; border: 1px solid var(--line); border-radius: 8px; background: rgba(0,0,0,.16); }
.codegraph-view-switch button { padding: 7px 13px; border: 0; border-radius: 5px; background: transparent; color: var(--muted); font: inherit; font-size: 11px; cursor: pointer; }
.codegraph-view-switch button[aria-pressed="true"] { color: #cbf7e6; background: #1b3b30; }
.codegraph-view-switch button:hover { color: var(--fg); }
.codegraph-view-switch button:focus-visible, .codegraph-zoom button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.codegraph-zoom { position: absolute; right: 16px; bottom: 16px; z-index: 2; display: flex; align-items: center; padding: 4px; gap: 3px; border: 1px solid var(--line); border-radius: 8px; background: #0d201b; }
.codegraph-zoom button { width: 30px; height: 30px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: #d6e9e0; font-size: 17px; cursor: pointer; }
.codegraph-zoom button:hover { background: #254437; }
.codegraph-zoom output { min-width: 42px; text-align: center; font-size: 10px; color: #a7c3b5; font-variant-numeric: tabular-nums; }
.codegraph-view .codegraph-legend { background: #0d201b; border-color: var(--line); padding: 9px 11px; }
.codegraph-view .codegraph-dot { width: 10px; height: 7px; border: 1px solid #6da58d; border-radius: 2px; background: #24513e; box-shadow: none; }
.codegraph-view .codegraph-dot.symbol { border-color: #6d8da4; background: #244357; box-shadow: none; }
.codegraph-automation { align-items: center; padding: 10px 12px; margin: 0; border: 1px solid var(--line); border-radius: 10px; background: rgba(110,231,196,.035); }
.codegraph-automation strong { font-size: 11px; }
.codegraph-automation p { margin-top: 2px; color: var(--muted); font-size: 10px; }
.finding-meta { margin-top: 7px; color: var(--faint); font-size: 10px; }
@media (max-width: 1280px) {
  .ws-path { max-width: 220px; }
}
@media (max-width: 1120px) {
  .topbar-brand { min-width: auto; }
  .topbar-context { display: none; }
  .page-heading::after { display: none; }
}
@media (max-width: 720px) {
  .page-heading { grid-template-columns: auto minmax(0,1fr); }
  .page-heading .eyebrow, .page-heading h2, .page-heading p, .page-heading .finding-actions, .page-heading .page-actions { grid-column: 2; }
  .page-heading::after { display: none; }
  .topbar-brand-copy span { display: none; }
  .agent-cards { grid-template-columns: 1fr; }
}

/* Reference-layout consolidation: one global header, seven primary views,
   and page-specific context in the right rail.  Existing renderers retain
   their API/action ownership; these primitives prevent card-stack layouts. */
.app-shell { display: grid; grid-template-columns: minmax(0, 1fr) 280px; }
.sidebar {
  position: fixed; z-index: 12; top: 56px; left: 0; right: 280px; width: auto; height: 48px;
  display: block; padding: 0; overflow: visible; border-right: 0; border-bottom: 1px solid var(--line);
  background: rgba(4, 11, 9, .94);
}
.sidebar-brand, .sidebar-footer { display: none; }
.sidebar-nav { display: flex; align-items: stretch; gap: 2px; height: 100%; padding: 6px 20px; overflow-x: auto; overflow-y: hidden; }
.sidebar-nav .nav-section-label { display: none; }
.sidebar-nav .nav-item { flex: 0 0 auto; min-height: 34px; margin: 0; padding: 7px 10px; white-space: nowrap; }
.sidebar-nav .nav-item .count { min-width: auto; margin-left: 2px; }
.main-wrapper { grid-column: 1; min-width: 0; }
.status-rail { grid-column: 2; width: 280px; }
.content { padding: 66px 22px 30px; }
.topbar { min-height: 56px; padding: 0 20px; }
.topbar-brand { min-width: 0; }
.topbar-context { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.topbar-reader { position: relative; z-index: 40; }.topbar-reader > summary { list-style: none; padding: 5px 8px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); cursor: pointer; font-size: 11px; }.topbar-reader > summary::-webkit-details-marker { display: none; }.topbar-reader .reader-toggle { position: absolute; z-index: 50; top: calc(100% + 7px); right: 0; width: 190px; padding: 10px; border: 1px solid var(--line); border-radius: 9px; background: var(--panel-solid); box-shadow: var(--shadow); }

.dashboard-view { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; min-width: 0; }
.dashboard-main { min-width: 0; display: grid; gap: 12px; }
.compact-toolbar { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; padding: 9px 0; border-bottom: 1px solid var(--line); }
.compact-toolbar h2 { font-size: 15px; }
.compact-toolbar .toolbar-grow { flex: 1 1 180px; min-width: 0; }
.compact-toolbar .field, .compact-toolbar label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 11px; }
.compact-toolbar select, .compact-toolbar input, .compact-toolbar textarea { min-height: 31px; max-width: 100%; padding: 5px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel-solid); color: var(--fg); }
.page-tabs { display: inline-flex; gap: 3px; max-width: 100%; overflow-x: auto; padding: 3px; border: 1px solid var(--line); border-radius: 8px; background: rgba(110,231,196,.025); }
.page-tabs button { min-height: 27px; padding: 4px 9px; border: 0; border-radius: 5px; background: transparent; color: var(--muted); cursor: pointer; font-size: 11px; white-space: nowrap; }
.page-tabs button.active { background: rgba(110,231,196,.14); color: var(--accent-bright); }
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.kpi { min-width: 0; padding: 10px 11px; border: 1px solid var(--line); border-radius: 9px; background: rgba(110,231,196,.035); }
.kpi[role="button"] { cursor: pointer; transition: border-color .15s ease, background .15s ease, transform .15s ease; }
.kpi[role="button"]:hover, .kpi[role="button"]:focus-visible { border-color: var(--line-strong); background: rgba(110,231,196,.09); transform: translateY(-1px); }
.kpi span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 10px; }
.kpi strong { display: block; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--accent-bright); font-size: 19px; }
.kpi-hint { display: block; margin-top: 3px; color: var(--faint); font-size: 9px; }
.kpi.alert strong { color: var(--orange); }.kpi.danger strong { color: var(--red); }.kpi.muted strong { color: var(--faint); }
.data-table-wrap { min-width: 0; overflow: auto; border: 1px solid var(--line); border-radius: 10px; }
.data-table { width: 100%; min-width: 680px; border-collapse: collapse; }
.data-table th, .data-table td { padding: 9px 10px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 11px; text-align: left; vertical-align: top; }
.data-table th { position: sticky; top: 0; z-index: 1; color: var(--faint); font-size: 10px; letter-spacing: .06em; text-transform: uppercase; background: var(--panel-solid); }
.data-table td strong { color: var(--fg); font-weight: 620; }.data-table tr:last-child td { border-bottom: 0; }
.data-table tbody tr { cursor: pointer; transition: background .15s ease; }.data-table tbody tr:hover, .data-table tbody tr.is-selected { background: rgba(110,231,196,.075); }
.data-table .table-actions { display: flex; flex-wrap: wrap; gap: 5px; }.data-table .btn { min-height: 27px; padding: 4px 8px; font-size: 10px; }
.detail-rail { min-width: 0; }.detail-rail .rail-title { margin-bottom: 10px; color: var(--fg); font-size: 14px; }.detail-rail .rail-section { margin: 12px 0; padding-top: 11px; border-top: 1px solid var(--line); }.detail-rail .rail-section:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
.detail-rail .rail-section h4 { margin-bottom: 6px; color: var(--faint); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }.detail-rail .rail-copy { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--muted); font-size: 11px; line-height: 1.6; }
.compact-secondary { border: 1px solid var(--line); border-radius: 9px; background: rgba(110,231,196,.02); }.compact-secondary > summary { padding: 8px 10px; color: var(--muted); cursor: pointer; font-size: 11px; }.compact-secondary > *:not(summary) { margin: 0 10px 10px; }
.governance-timeline { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 0; overflow: auto; padding: 10px 0; }.governance-stage { position: relative; min-width: 100px; min-height: 42px; padding: 6px 8px 6px 18px; border-top: 1px solid var(--line-strong); border-bottom: 1px solid var(--line-strong); color: var(--fg); background: rgba(110,231,196,.025); font-size: 10px; line-height: 1.35; }.governance-stage::before { content: ''; position: absolute; left: 0; top: 17px; width: 8px; height: 8px; border: 1px solid var(--line-strong); border-radius: 50%; background: var(--bg); transform: translate(-50%, -50%); }.governance-stage .stage-name { display: block; color: var(--fg); font-weight: 650; }.governance-stage .stage-state { display: block; margin-top: 2px; color: var(--muted); font-size: 9px; }.governance-stage.completed { border-color: rgba(110,231,196,.48); background: rgba(110,231,196,.10); }.governance-stage.completed .stage-state { color: var(--accent-bright); }.governance-stage.completed::before { background: var(--accent); box-shadow: 0 0 10px var(--accent); }.governance-stage.current { border-color: rgba(233,187,100,.64); background: rgba(233,187,100,.10); }.governance-stage.current .stage-state { color: var(--orange); }.governance-stage.current::before { border-color: var(--orange); background: var(--orange); box-shadow: 0 0 10px rgba(233,187,100,.65); }.governance-stage.pending { color: var(--muted); background: rgba(72,104,94,.08); }.governance-stage.unknown { border-style: dashed; color: var(--muted); background: transparent; }.governance-stage.unknown .stage-state { color: var(--faint); }.overview-columns { display: grid; grid-template-columns: minmax(0, 1fr) minmax(260px, .78fr); gap: 12px; }.overview-list { display: grid; gap: 0; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }.overview-list button { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; width: 100%; padding: 9px 10px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer; text-align: left; font-size: 11px; }.overview-list button:last-child { border-bottom: 0; }.overview-list button:hover, .overview-list button:focus-visible { background: rgba(110,231,196,.06); color: var(--fg); }.overview-risk-copy { display: grid; min-width: 0; gap: 2px; }.overview-risk-copy strong { color: var(--fg); font-weight: 650; }.overview-risk-copy span, .overview-risk-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.overview-risk-copy small { color: var(--faint); font-size: 10px; }
.agent-discovery-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(165px, 1fr)); gap: 8px; }.agent-discovery-grid .agent-card { min-height: 124px; padding: 10px; }.agent-discovery-grid .agent-meta { margin-top: 5px; font-size: 10px; }.agent-discovery-grid .surface-meta { display: none; }
.source-detail-row { cursor: pointer; }.source-detail-row.is-selected { background: rgba(110,231,196,.075); }
.neuron-shell { min-height: min(680px, calc(100vh - 190px)); }.neuron-stage { min-height: min(610px, calc(100vh - 230px)); }.codegraph-stage { min-height: min(660px, calc(100vh - 210px)); }.codegraph-shell { gap: 10px; }
.rule-table .rule-body-preview { display: block; max-width: 390px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.history-table .history-summary { display: block; max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.risk-table .risk-reason { display: block; max-width: 390px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-inline { padding: 16px; border: 1px dashed var(--line-strong); border-radius: 10px; color: var(--muted); font-size: 11px; }
@media (max-width: 1024px) {
  .app-shell { grid-template-columns: minmax(0, 1fr); }.status-rail { grid-column: 1; grid-row: 2; width: auto; max-height: none; border-top: 1px solid var(--line); border-left: 0; }.sidebar { right: 0; }.content { padding: 66px 16px 22px; }.kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.overview-columns { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  body { overflow: auto; }.app-shell { min-height: 100%; height: auto; }.main-wrapper { overflow: visible; }.content { overflow: visible; }.sidebar { position: sticky; top: 0; height: auto; }.sidebar-nav { padding: 5px 10px; }.topbar { padding: 0 12px; }.ws-path, .topbar-context { display: none; }.topbar-right .btn { padding: 6px 8px; }.content { padding-top: 58px; }.governance-timeline { grid-template-columns: repeat(6, 100px); }.data-table { min-width: 620px; }.neuron-shell { min-height: 520px; }.neuron-stage, .codegraph-stage { min-height: 500px; }.status-rail { overflow: visible; }
}
/* Final narrow guard. Keep this after every shared layout rule: the embedded
   Edge host can report a scaled CSS viewport wider than its screenshot width. */
@media (max-width: 640px) {
  html, body, .app-shell, .main-wrapper, .content, .dashboard-view, .dashboard-main { width: 100%; max-width: 100%; min-width: 0; }
  .app-shell, .main-wrapper, .content, .dashboard-view, .dashboard-main { min-inline-size: 0; }
  .dashboard-main > *, #content .overview-view section { min-width: 0; max-width: 100%; }
  .content { padding: 58px 10px 18px; }
  .topbar, .topbar-left, .topbar-right { min-width: 0; max-width: 100%; }
  .topbar-right { flex-wrap: wrap; justify-content: flex-end; }
  #content .overview-view .kpi-grid { grid-template-columns: minmax(0, 1fr); width: 100%; }
  #content .overview-view .governance-timeline { grid-template-columns: minmax(0, 1fr); width: 100%; overflow: visible; gap: 6px; padding: 6px 0; }
  #content .overview-view .governance-stage { min-width: 0; width: 100%; border: 1px solid var(--line); border-radius: 7px; }
  #content .overview-view .governance-stage::before { left: 7px; transform: translateY(-50%); }
}

/* Reference shell: persistent 224px navigation, full-width workspace header,
   readable central work area, and a 280px context rail.  The old template
   accumulated several layout passes; this final contract intentionally wins
   without changing any renderer's data or action ownership. */
.app-shell { display: grid; grid-template-columns: 224px minmax(0, 1fr) 280px; grid-template-rows: 64px minmax(0, 1fr); height: 100%; min-width: 0; }
.main-wrapper { display: contents; }
.sidebar { position: relative; z-index: 12; grid-column: 1; grid-row: 1 / span 2; width: auto; height: auto; display: flex; flex-direction: column; padding: 18px 0 14px; overflow: hidden; border-right: 1px solid var(--line); border-bottom: 0; background: rgba(4, 11, 9, .94); }
.sidebar-brand { display: flex; }
.sidebar-footer { display: block; margin-top: auto; }
.sidebar-nav { display: block; height: auto; padding: 0 10px; overflow-y: auto; overflow-x: clip; }
.sidebar-nav .nav-section-label { display: block; }
.sidebar-nav .nav-item { display: flex; min-height: 38px; margin-bottom: 3px; padding: 9px 10px 9px 13px; white-space: normal; }
.sidebar-nav .nav-item { border: 0; background: transparent; text-align: left; }
.sidebar-nav .nav-item .count { min-width: 18px; margin-left: auto; }
.sidebar-settings { width: 100%; display: flex; align-items: center; gap: 9px; padding: 9px 0; border: 0; background: transparent; color: var(--muted); cursor: pointer; font-size: 12px; text-align: left; }
.sidebar-settings::before { content: '⚙'; width: 18px; color: var(--faint); text-align: center; }
.sidebar-settings:hover, .sidebar-settings:focus-visible { color: var(--fg); }
.local-badge { display: none; }
.topbar { grid-column: 2 / span 2; grid-row: 1; min-width: 0; }
.content { grid-column: 2; grid-row: 2; min-width: 0; overflow: auto; padding: 22px 24px 30px; }
.status-rail { grid-column: 3; grid-row: 2; width: auto; min-width: 0; max-height: none; overflow-y: auto; border-left: 1px solid var(--line); border-top: 0; }
.topbar-brand { display: inline-flex; }
.topbar-left { gap: 12px; }
.topbar-context { font-size: 12px; color: var(--fg); }
.topbar-context::before { display: none; }
.dashboard-view { max-width: 1240px; margin: 0 auto; }
.page-heading { border: 0; border-bottom: 1px solid var(--line); border-radius: 0; padding: 0 0 16px; margin-bottom: 18px; background: transparent; }
.page-heading::before { width: 24px; height: 24px; border-radius: 6px; }
.agent-discovery-grid { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.agent-card { display: grid; grid-template-columns: auto minmax(0, 1fr); column-gap: 10px; align-items: start; min-height: 132px; padding: 14px; border-radius: 9px; }
.agent-card[role="button"] { cursor: pointer; }
.agent-card[role="button"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.agent-card .agent-avatar { grid-row: 1 / span 4; }
.agent-card .agent-name, .agent-card .agent-meta, .agent-card .agent-badge, .agent-card > .chip, .agent-card .finding-actions, .agent-card .agent-technical-id { grid-column: 2; }
.agent-card .agent-meta { margin-top: 6px; }
.agent-card .agent-technical-id { margin-top: 4px; }
.agent-card .agent-technical-id summary { color: var(--faint); cursor: pointer; font-size: 9px; }
.agent-card .agent-technical-id code { display: block; margin-top: 3px; font-size: 9px; }
.agent-avatar { display: grid; place-items: center; width: 34px; height: 34px; flex: none; border: 1px solid var(--line-strong); border-radius: 8px; background: rgba(110,231,196,.08); color: var(--accent-bright); font-size: 12px; font-weight: 750; letter-spacing: -.03em; }
.agent-avatar[data-agent-family="grok"] { background: #050505; border-color: rgba(255,255,255,.38); color: #fff; font-size: 17px; }
.agent-avatar[data-agent-family="codex"] { background: rgba(154, 237, 214, .14); border-color: rgba(154,237,214,.44); color: #d8fff3; }
.agent-avatar[data-agent-family="trae"] { background: rgba(125,211,252,.13); border-color: rgba(125,211,252,.38); color: #c7efff; }
.agent-avatar[data-agent-family="chatgpt"] { background: rgba(110,231,196,.13); }
.agent-avatar[data-agent-family="copilot"] { background: rgba(99,179,237,.13); color: #b9e7ff; }
.agent-avatar[data-agent-family="claude"] { background: rgba(233,187,100,.13); color: #f3d697; }
.agent-avatar[data-agent-family="cursor"] { background: rgba(205,205,205,.1); color: #f0f0f0; }
.agent-avatar[data-agent-family="unknown"] { color: var(--muted); background: rgba(72,104,94,.14); }
.agent-avatar .agent-icon-svg { display: block; width: 22px; height: 22px; fill: currentColor; stroke: none; }
.agent-mark-text { display: block; color: currentColor; font-size: 10px; font-weight: 800; letter-spacing: -.04em; }
.agent-avatar[data-agent-family="grok"] .agent-mark-text { font-size: 9px; letter-spacing: -.03em; }
.agent-avatar[data-agent-family="codex"] .agent-mark-text { font-size: 9px; }
.agent-avatar[data-agent-family="unknown"] .agent-icon-svg { stroke-width: 1.45; opacity: .82; }
.agent-card > .chip { justify-self: start; margin-top: 9px; }
.agent-card .finding-actions { margin-top: 8px; }
.source-detail-row td:first-child strong { display: inline-flex; align-items: center; gap: 8px; }
.source-detail-row td:first-child strong::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px rgba(110,231,196,.5); }
.sources-view .dashboard-main > section { min-width: 0; }
@media (max-width: 1024px) {
  .app-shell { grid-template-columns: 224px minmax(0, 1fr); grid-template-rows: 64px auto auto; height: auto; min-height: 100%; }
  .sidebar { grid-column: 1; grid-row: 1 / span 2; }
  .topbar { grid-column: 2; grid-row: 1; }
  .content { grid-column: 2; grid-row: 2; }
  .status-rail { grid-column: 1 / span 2; grid-row: 3; width: auto; border-top: 1px solid var(--line); border-left: 0; }
}
@media (max-width: 720px) {
  .app-shell { display: block; height: auto; min-height: 100%; }
  .sidebar { position: sticky; top: 0; z-index: 40; width: 100%; height: auto; padding: 0; border-right: 0; border-bottom: 1px solid var(--line); }
  .sidebar-footer, .sidebar-nav .nav-section-label { display: none; }
  .sidebar-brand { display: flex; padding: 10px 14px 8px; }
  .sidebar-nav { display: flex; align-items: stretch; gap: 2px; padding: 6px 10px; overflow-x: auto; overflow-y: hidden; }
  .sidebar-nav .nav-item { flex: 0 0 auto; min-width: max-content; min-height: 34px; margin: 0; padding: 7px 10px; gap: 8px; justify-content: flex-start; overflow: visible; white-space: nowrap; font-size: 12px; }
  .sidebar-nav .nav-item::before { flex: none; width: 5px; height: 5px; }
  .topbar, .content, .status-rail { display: block; }
  .topbar-brand { display: none; }
  .topbar { min-height: 56px; }
  .content { padding: 18px 12px 24px; }
  .status-rail { border-top: 1px solid var(--line); border-left: 0; }
  .agent-discovery-grid { grid-template-columns: 1fr; }
}

/* Token usage is a compact evidence view, not a second dashboard.  The page
   keeps measured host traffic separate from MemoryGuard's deterministic
   estimate and remains readable when no telemetry has been synced. */
.token-usage-view { display: grid; gap: 14px; }
.token-usage-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.token-usage-toolbar h2 { font-size: 17px; }
.token-usage-toolbar p { margin-top: 4px; max-width: 760px; color: var(--muted); font-size: 11px; }
.token-window-tabs { display: inline-flex; gap: 4px; padding: 4px; border: 1px solid var(--line); border-radius: 9px; background: rgba(10,26,21,.50); }
.token-window-tabs button { min-width: 56px; padding: 7px 10px; border: 0; border-radius: 6px; color: var(--muted); background: transparent; cursor: pointer; font-size: 11px; }
.token-window-tabs button.active { color: var(--accent-bright); background: rgba(110,231,196,.13); box-shadow: inset 0 0 0 1px rgba(110,231,196,.30); }
.token-agent-filter { min-width: 190px; max-width: 300px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; color: var(--fg); background: var(--panel-solid); }
.token-kpi-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.token-kpi { min-height: 96px; padding: 15px; border: 1px solid var(--line); border-radius: 11px; background: rgba(110,231,196,.035); }
.token-kpi.measured { border-color: rgba(110,231,196,.34); }
.token-kpi.estimated { border-color: rgba(233,187,100,.38); }
.token-kpi.unavailable { border-color: rgba(72,104,94,.34); }
.token-kpi span { display: block; color: var(--muted); font-size: 11px; }
.token-kpi strong { display: block; margin-top: 7px; color: var(--accent-bright); font-size: 24px; line-height: 1.2; }
.token-kpi.estimated strong { color: var(--orange); }
.token-kpi.unavailable strong { color: var(--faint); }
.token-kpi small { display: block; margin-top: 5px; color: var(--faint); font-size: 10px; }
.token-chart-card { min-width: 0; padding: 16px; border: 1px solid var(--line); border-radius: 12px; background: rgba(10,26,21,.50); }
.token-chart-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.token-chart-head h3 { font-size: 13px; }
.token-chart-head p { margin-top: 3px; color: var(--muted); font-size: 10px; }
.token-chart { width: 100%; min-height: 218px; overflow-x: auto; }
.token-chart svg { display: block; width: 100%; min-width: 560px; height: 218px; }
.token-chart .chart-grid { stroke: rgba(110,231,196,.12); stroke-width: 1; }
.token-chart .chart-axis { stroke: rgba(110,231,196,.28); stroke-width: 1; }
.token-chart .chart-label { fill: var(--muted); font-size: 10px; }
.token-chart .chart-line-raw { fill: none; stroke: var(--orange); stroke-width: 2.2; }
.token-chart .chart-line-injected { fill: none; stroke: var(--accent); stroke-width: 2.2; }
.token-chart .chart-dot-raw { fill: var(--orange); }
.token-chart .chart-dot-injected { fill: var(--accent); }
.token-legend { display: flex; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 10px; }
.token-legend-item { display: inline-flex; align-items: center; gap: 5px; }
.token-legend-swatch { width: 18px; height: 2px; background: currentColor; }
.token-legend-swatch.raw { color: var(--orange); }
.token-legend-swatch.injected { color: var(--accent); }
.token-table-wrap { overflow-x: auto; }
.token-table { width: 100%; min-width: 840px; table-layout: fixed; }
.token-table th, .token-table td { white-space: normal; }
.token-table th:nth-child(1) { width: 11%; }
.token-table th:nth-child(2) { width: 16%; }
.token-table th:nth-child(3) { width: 12%; }
.token-table th:nth-child(4) { width: 17%; }
.token-table th:nth-child(5) { width: 13%; }
.token-table th:nth-child(6) { width: 14%; }
.token-table th:nth-child(7) { width: 10%; }
.token-table th:nth-child(8) { width: 7%; }
.token-flow-cell { display: grid; gap: 3px; min-width: 0; line-height: 1.35; }
.token-flow-line { display: flex; justify-content: space-between; gap: 6px; }
.token-flow-line strong { color: var(--fg); font-weight: 620; }
.token-agent-name { color: var(--fg); font-weight: 600; }
.token-agent-id { margin-top: 3px; }
.token-agent-id summary { color: var(--faint); cursor: pointer; font-size: 9px; }
.token-agent-id code { display: block; max-width: 190px; margin-top: 3px; font-size: 9px; overflow-wrap: anywhere; }
.token-source-measured { color: var(--accent); }
.token-source-estimated { color: var(--orange); }
.token-source-mixed { color: #b8d98a; }
.token-source-unavailable { color: var(--faint); }
.token-unavailable-row td { color: var(--faint); }
.token-scope-note { padding: 10px 12px; border: 1px dashed var(--line-strong); border-radius: 9px; color: var(--muted); font-size: 11px; line-height: 1.55; }
.token-error { border-color: rgba(255,125,136,.45); color: var(--red); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 720px) {
  .token-kpi-grid { grid-template-columns: 1fr; }
  .token-usage-toolbar { display: grid; }
  .token-window-tabs, .token-agent-filter { width: 100%; max-width: none; }
  .token-window-tabs button { flex: 1; }
}
</style>
</head>
<body>
<div class="app-shell">
  <!-- 左侧导航 224px -->
  <aside class="sidebar">
    <div class="sidebar-brand">
      <span class="brand-orb" aria-hidden="true"></span>
      <span class="brand-copy"><strong>MemoryGuard</strong><span>Local governance</span></span>
    </div>
    <nav class="sidebar-nav" role="tablist" aria-label="治理模块">
      <div class="nav-section-label">治理视图</div>
      <button class="nav-item active" type="button" role="tab" aria-selected="true" aria-controls="content" tabindex="0" data-tab="overview" onclick="switchTab('overview')">总览</button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="sources" onclick="switchTab('sources')">数据源与 Agent<span class="count" id="sources-count"></span></button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="neurons" onclick="switchTab('neurons')">记忆核心<span class="count" id="neuron-count"></span></button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="codegraph" onclick="switchTab('codegraph')">CodeGraph<span class="count" id="codegraph-count"></span></button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="rules" onclick="switchTab('rules')">规则与习惯</button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="history" onclick="switchTab('history')">对话历史</button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="findings" onclick="switchTab('findings')">风险信号与治理控制台<span class="count" id="findings-count"></span></button>
      <button class="nav-item" type="button" role="tab" aria-selected="false" aria-controls="content" tabindex="0" data-tab="token-usage" onclick="switchTab('token-usage')">Token 用量与节省</button>
    </nav>
    <div class="sidebar-footer"><button class="sidebar-settings" type="button" aria-label="打开设置" onclick="openSettings()">设置</button></div>
  </aside>

  <!-- 主工作区 -->
  <div class="main-wrapper">
    <header class="topbar">
      <div class="topbar-left">
        <div class="topbar-brand" aria-label="MemoryGuard">
          <span class="topbar-brand-mark" aria-hidden="true"></span>
          <span class="topbar-brand-copy"><strong>MemoryGuard</strong><span>AI Memory Governance</span></span>
        </div>
        <div class="topbar-context" id="topbar-context">Current workspace</div>
        <span class="ws-path" id="ws-path">正在连接本地工作区…</span>
      </div>
      <div class="topbar-right">
        <span id="releases-count" hidden></span>
        <details class="topbar-reader"><summary>阅读</summary><div class="reader-toggle"><div class="reader-toggle-label">阅读语言</div><div class="reader-toggle-buttons" title="英文模式优先显示英文内容；无英文版本时显示来源原文"><button type="button" id="reader-auto" onclick="setReaderLanguage('auto')">自动</button><button type="button" id="reader-zh" class="active" onclick="setReaderLanguage('zh')">中文</button><button type="button" id="reader-en" onclick="setReaderLanguage('en')">English</button></div></div></details>
        <button class="btn" type="button" onclick="openKnowledge()">知识书库<span class="count" id="knowledge-count"></span></button>
        <span class="health-badge" id="health-badge">健康度 --</span>
        <button class="btn btn-primary audit-refresh-button" type="button" onclick="runAudit()">重新扫描</button>
      </div>
    </header>
    <main class="content" id="content"><div class="loading">正在建立本地治理视图</div></main>
  </div>

  <!-- 右侧状态栏 280px -->
  <aside class="status-rail" id="status-rail">
    <div class="detail-rail">
      <h3>治理状态</h3>
      <div id="status-rail-content"><div class="loading" style="min-height:120px">连接中…</div></div>
    </div>
  </aside>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const knownGuiTabs = ['overview','sources','neurons','codegraph','rules','history','findings','token-usage','releases','governance'];
const guiTabHashAliases = {memory: 'neurons'};
function currentGuiHash() {
  try {
    return typeof window !== 'undefined' && window.location && typeof window.location.hash === 'string'
      ? window.location.hash : '';
  } catch (_) { return ''; }
}
function guiTabFromHash(value) {
  const tab = String(value === undefined ? currentGuiHash() : value || '').replace(/^#/, '');
  return guiTabHashAliases[tab] || tab;
}
const hashTab = guiTabFromHash();
const rememberedActiveTab = (knownGuiTabs.includes(hashTab) ? hashTab : '') || localStorage.getItem('memoryguard.activeTab') || 'overview';
let state = { report: null, activeTab: rememberedActiveTab, plans: [], changes: [], releases: [], lastPlan: null, governanceSnapshot: null };
let neuronGraph = null;
let codeGraph = null;
let projectionMode = localStorage.getItem('memoryguard.projectionMode') || 'native';
let cyInstance = null;
let codeCyInstance = null;
let codeGraphView = 'files';
let codeGraphFocusFile = '';
let selectedNeuronId = null;
let selectedNeuronNode = null;
let neuronDetailHydrationSeq = 0;
let selectedCodeGraphNode = null;
let selectedNeuronIds = new Set();
let neuronDragState = null;
let neuronTapSelectionAdditive = false;
let historyFocusSessionId = '';
let historyBackfillContinuation = null;
let selectedSourceId = '';
let selectedSourceRecord = null;
let selectedRuleId = '';
let selectedHistorySessionId = '';
let selectedFindingId = '';
let sourceViewTab = 'all';
let ruleViewTab = 'rules';
let historyPageData = null;
let sourcePageRecords = new Map();
let readerLanguage = localStorage.getItem('memoryguard.readerLanguage') || 'zh';
if (readerLanguage === 'original') readerLanguage = 'en';
let sourcesScope = 'all';      // 数据源 sub-tab: 'all' | 'user' | 'project'
let discoveryResult = null;    // 缓存 discover_agents 结果
let activeAgentInstanceId = '';  // v3.2：当前选中的 Agent 卡片
let agentCardsData = null;     // v3.2：缓存 list_agents 结果
let dataPageMode = 'single_agent';  // v3.2：single_agent | multi_agent_shared_mcp
let activeShareGroupId = '';
let governanceSubTab = 'recent_events';  // 治理台子视图
let codeGraphLimit = 100;
let codeGraphProvenance = '';
let codeGraphProjects = [];
let codeGraphAutomation = {};
let codeGraphAutomationSaving = false;
let codeGraphAutomationTimer = null;
let codeGraphBuildReady = false;
let selectedCodeGraphProject = localStorage.getItem('memoryguard.codeGraphProject') || '';
let codeGraphBuildInFlight = false;
let activeScopeMemberIds = [];
let governanceScopeState = { status: 'unselected', share_group_id: '', reason: '' };
let projectionEngineState = { loaded: false, engines: [], error: '' };
let selectedProjectionEngine = localStorage.getItem('memoryguard.projectionEngine') || 'deterministic';
let activeBuildRunId = '';
let buildStartInFlight = false;
let buildCancelInFlight = false;
// Each full-page render gets a monotonically increasing token.  Async page
// reads may finish out of order; only the render that still owns the active
// tab is allowed to replace #content.  Sub-views use the current token
// without advancing it, so legitimate updates inside one tab remain valid.
let contentRenderGeneration = 0;
let pendingContentRenderToken = null;
let tokenUsageState = { windowDays: 7, agentKey: '', payload: null, syncResult: null, syncInFlight: null, auditInFlight: null };

function beginContentRender(tab = state.activeTab) {
  contentRenderGeneration += 1;
  return {tab, generation: contentRenderGeneration};
}

function currentContentRenderToken(tab = state.activeTab) {
  return {tab, generation: contentRenderGeneration};
}

function takeContentRenderToken(tab) {
  if (pendingContentRenderToken && pendingContentRenderToken.tab === tab) {
    return pendingContentRenderToken;
  }
  return beginContentRender(tab);
}

function contentRenderIsCurrent(token) {
  return !token || (token.tab === state.activeTab && token.generation === contentRenderGeneration);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

function finiteNumber(value, fallback = 0) {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function finiteOrNull(value) {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

function optionalFiniteNumber(value) {
  if (value === null || value === undefined || (typeof value === 'string' && !value.trim())) return null;
  return finiteOrNull(value);
}

function safeDisplayText(value, fallback = '') {
  if (value === null || value === undefined || value === '') return fallback;
  const text = String(value);
  return text === 'undefined' || text === 'null' || text === 'NaN' ? fallback : text;
}

function sameNonEmptyAgentId(left, right) {
  const leftId = safeDisplayText(left, '').trim();
  const rightId = safeDisplayText(right, '').trim();
  return Boolean(leftId && rightId && leftId === rightId);
}

function apiRows(result, key) {
  if (Array.isArray(result)) return result;
  return Array.isArray(result?.[key]) ? result[key] : [];
}

function agentSummary(agent, sourceCount = null) {
  const item = agent && typeof agent === 'object' ? agent : {};
  const surfaces = Array.isArray(item.surfaces) ? item.surfaces : [];
  const foundSurfaceCount = finiteNumber(
    item.found_surface_count,
    surfaces.filter(surface => surface && surface.status === 'found').length,
  );
  const surfaceCount = finiteNumber(item.surface_count, surfaces.length);
  const resolvedSourceCount = finiteNumber(
    item.bound_source_count,
    finiteNumber(item.source_count, finiteNumber(sourceCount, 0)),
  );
  return {
    foundSurfaceCount: Math.max(0, foundSurfaceCount),
    surfaceCount: Math.max(0, surfaceCount),
    privateSurfaceCount: Math.max(0, finiteNumber(item.private_data_surface_count, 0)),
    sharedSurfaceCount: Math.max(0, finiteNumber(item.shared_surface_count, 0)),
    sourceCount: Math.max(0, resolvedSourceCount),
  };
}

function agentSourceLabel(agent, sourceCount = null) {
  const summary = agentSummary(agent, sourceCount);
  return `${summary.sourceCount} 来源`;
}

function objectValue(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function finiteFirst(...values) {
  for (const value of values) {
    const number = finiteOrNull(value);
    if (number !== null) return number;
  }
  return 0;
}

function governanceSnapshot(raw) {
  const root = objectValue(raw);
  const source = objectValue(root.governance_snapshot || root.governanceSnapshot || root.snapshot || root.governance || root);
  const memory = objectValue(source.memory);
  const counts = objectValue(source.counts || source.metrics);
  const status = objectValue(source.status);
  const statusCounts = objectValue(source.status_counts || memory.status_counts || memory.statusCounts);
  const conflicts = objectValue(source.conflicts);
  const quarantine = objectValue(source.quarantine);
  const rollback = objectValue(source.rollback);
  const statusText = safeDisplayText(source.scope_status || source.governance_status || source.status, '').toLowerCase();
  const auditOnly = source.audit_only === true || statusText === 'audit_only';
  const activeMemories = finiteFirst(
    counts.active_memories,
    counts.active_count,
    source.active_memories,
    source.active_count,
    status.active_count,
    memory.active_records,
    statusCounts.active_memories,
    statusCounts.active_count,
    statusCounts.active,
    memory.total_records,
    memory.total,
  );
  const conflictCount = finiteFirst(
    counts.unresolved_conflicts,
    counts.conflicts,
    counts.conflict_count,
    source.conflict_count,
    conflicts.unresolved_total,
    conflicts.total,
    conflicts.count,
    memory.conflict_count,
    statusCounts.conflicts,
    statusCounts.conflict,
  );
  const conflictSelectableCount = [
    counts.selectable_conflicts,
    counts.selectable_conflict_count,
    source.selectable_conflicts,
    conflicts.selectable_total,
    conflicts.actionable_total,
  ].map(finiteOrNull).find(value => value !== null) ?? null;
  const conflictClosableStaleCount = [
    counts.closable_stale_conflicts,
    counts.closable_stale_conflict_count,
    source.closable_stale_conflicts,
    conflicts.closable_stale_total,
    conflicts.stale_total,
  ].map(finiteOrNull).find(value => value !== null) ?? null;
  const quarantineCount = finiteFirst(
    counts.quarantined,
    counts.quarantine_count,
    source.quarantined,
    quarantine.count,
    memory.quarantined_count,
    statusCounts.quarantined,
    statusCounts.quarantine,
  );
  const rollbackCount = finiteFirst(
    counts.rollback_ready,
    source.rollback_ready,
    rollback.count,
    rollback.ready,
  );
  const members = Array.isArray(source.members)
    ? source.members
    : (Array.isArray(source.group_members) ? source.group_members : []);
  return {
    ...source,
    audit_only: auditOnly,
    counts: {
      active_memories: activeMemories,
      conflicts: conflictCount,
      unresolved_conflicts: conflictCount,
      selectable_conflicts: conflictSelectableCount,
      closable_stale_conflicts: conflictClosableStaleCount,
      quarantined: quarantineCount,
      rollback_ready: rollbackCount,
    },
    conflicts: {
      ...conflicts,
      count: conflictCount,
      unresolved_total: conflictCount,
      selectable_total: conflictSelectableCount,
      closable_stale_total: conflictClosableStaleCount,
    },
    quarantine: {...quarantine, count: quarantineCount},
    rollback_ready: rollbackCount,
    members,
    active_binding: source.active_binding || source.binding || null,
  };
}

function normalizeGovernanceScope(raw, expectedGroupId = '') {
  const root = objectValue(raw);
  const scope = objectValue(root.scope);
  const groupId = safeDisplayText(expectedGroupId || scope.share_group_id || root.share_group_id, '');
  const agentId = safeDisplayText(scope.agent_instance_id || root.agent_instance_id, '');
  const bindingCandidate = root.active_binding || root.binding;
  const binding = objectValue(bindingCandidate);
  const bindingGroupId = safeDisplayText(binding.share_group_id, '');
  const bindingStatus = safeDisplayText(binding.status, 'active').toLowerCase();
  const trustedBinding = groupId && bindingGroupId === groupId && bindingStatus === 'active' ? binding : null;
  const bindings = [
    ...(Array.isArray(root.bindings) ? root.bindings : []),
    ...(Array.isArray(scope.bindings) ? scope.bindings : []),
  ];
  const matchingBinding = bindings.find(item => {
    const candidate = objectValue(item);
    return safeDisplayText(candidate.share_group_id, '') === groupId
      && safeDisplayText(candidate.status, '').toLowerCase() === 'active';
  }) || null;
  const rawMembers = [
    ...(Array.isArray(root.members) ? root.members : []),
    ...(Array.isArray(root.group_members) ? root.group_members : []),
    ...(Array.isArray(scope.members) ? scope.members : []),
    ...(Array.isArray(scope.agent_instance_ids) ? scope.agent_instance_ids : []),
  ];
  const members = [...new Set(rawMembers.map(item => {
    const value = objectValue(item);
    return safeDisplayText(value.instance_id || value.agent_instance_id || item, '');
  }).filter(Boolean))];
  const selectedBinding = trustedBinding || matchingBinding;
  if (selectedBinding) {
    const selectedId = safeDisplayText(selectedBinding.agent_instance_id || selectedBinding.instance_id, '');
    if (selectedId && !members.includes(selectedId)) members.push(selectedId);
  }
  const statusText = safeDisplayText(root.scope_status || root.governance_status || root.status, '').toLowerCase();
  const auditOnly = root.audit_only === true || statusText === 'audit_only';
  return {
    status: auditOnly ? 'audit_only' : (groupId ? (selectedBinding ? 'active' : 'stale_selection') : (agentId ? 'active' : 'unselected')),
    share_group_id: groupId,
    agent_instance_id: agentId,
    binding: selectedBinding,
    members,
    reason: auditOnly ? 'audit_only' : (groupId && !selectedBinding ? 'active_binding_required' : ''),
  };
}

function clearSharedGovernance(status = 'stale_selection', reason = 'active_binding_required') {
  activeShareGroupId = '';
  activeScopeMemberIds = [];
  dataPageMode = 'single_agent';
  governanceScopeState = {status, share_group_id: '', reason, binding: null, members: []};
}

// SafeBridge redacts filesystem paths into descriptors before they reach the
// browser.  Every display path must pass this adapter; never call String or
// split directly on a path-bearing API field.
function guiPathText(value, fallback = '') {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    for (const key of ['summary', 'display_name', 'relative_path', 'name', 'ref']) {
      if (typeof value[key] === 'string' && value[key]) return value[key];
    }
    return fallback;
  }
  return value == null ? fallback : String(value);
}

function guiPathLabel(value, fallback = '受保护来源') {
  const text = guiPathText(value, fallback).replaceAll('\\\\', '/');
  const parts = text.split('/').filter(Boolean);
  return parts.slice(-2).join('/') || fallback;
}

function agentCapabilityLabel(value) {
  return ({
    export_only: '可接入 MemoryGuard 层',
    mcp: '支持 MCP',
    native_takeover: '支持原生接管',
  })[String(value || '')] || String(value || '能力待确认');
}

function setReaderLanguage(language) {
  readerLanguage = language;
  localStorage.setItem('memoryguard.readerLanguage', language);
  document.getElementById('reader-auto')?.classList.toggle('active', language === 'auto');
  document.getElementById('reader-zh')?.classList.toggle('active', language === 'zh');
  document.getElementById('reader-en')?.classList.toggle('active', language === 'en');
  renderStatusRail();
  renderContent();
}

function displayTitle(item) {
  if (readerLanguage === 'zh') return item.title_zh || item.zh_title || item.title || item.memory_id || '';
  if (readerLanguage === 'en') return item.title_en || item.en_title || item.original_title || item.title || item.title_zh || item.memory_id || '';
  return item.display_language === 'zh'
    ? (item.title_zh || item.zh_title || item.title || item.original_title || item.memory_id || '')
    : (item.original_title || item.title || item.title_zh || item.memory_id || '');
}

function displayBody(item) {
  if (readerLanguage === 'zh') return item.body_zh || item.zh_summary || item.body || item.body_preview || '';
  if (readerLanguage === 'en') return item.body_en || item.en_body || item.original_body || item.body || item.body_zh || item.body_preview || '';
  return item.display_language === 'zh'
    ? (item.body_zh || item.zh_summary || item.body || item.original_body || item.body_preview || '')
    : (item.original_body || item.body || item.body_zh || item.body_preview || '');
}

let MUTATION_METHODS = null;  // 从后端动态加载

async function getMutationMethods() {
  if (MUTATION_METHODS !== null) return MUTATION_METHODS;
  try {
    const registry = await callApiRaw('get_api_method_registry');
    MUTATION_METHODS = new Set(registry.mutation || []);
  } catch(e) {
    MUTATION_METHODS = new Set();
  }
  return MUTATION_METHODS;
}

let _sandboxMode = null;

async function isSandboxMode() {
  if (_sandboxMode !== null) return _sandboxMode;
  if (window.__MG_SANDBOX__ !== undefined) { _sandboxMode = window.__MG_SANDBOX__; return _sandboxMode; }
  try {
    const r = await callApiRaw('get_sandbox_status');
    _sandboxMode = r.sandbox;
  } catch(e) { _sandboxMode = false; }
  return _sandboxMode;
}

async function callApiRaw(method, ...args) {
  if (window.pywebview && window.pywebview.api) {
    return await window.pywebview.api[method](...args);
  }
  const headers = {'Content-Type': 'application/json'};
  if (window.__MG_SESSION__) headers['X-Session-Token'] = window.__MG_SESSION__;
  const resp = await fetch('/api/' + method, { method: 'POST', headers, body: JSON.stringify(args) });
  if (!resp.ok) {
    const errBody = await resp.json().catch(() => ({}));
    throw new Error(errBody.error || ('API ' + method + ' 返回 ' + resp.status));
  }
  return await resp.json();
}

function normalizeApiResult(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return raw || {};
  const nested = raw.data && typeof raw.data === 'object' && !Array.isArray(raw.data)
    ? raw.data : {};
  // Business data overlays transport metadata so callers see the semantic
  // status (succeeded/running/failed) instead of an outer compatibility "ok".
  const merged = {...raw, ...nested};
  if (raw.ok === false) merged.ok = false;
  if (raw.task && typeof raw.task === 'object') merged.task = raw.task;
  if (nested.task && typeof nested.task === 'object') merged.task = nested.task;
  if (raw.receipt && typeof raw.receipt === 'object') merged.receipt = raw.receipt;
  if (nested.receipt && typeof nested.receipt === 'object') merged.receipt = nested.receipt;
  if (raw.result_ref && typeof raw.result_ref === 'object') merged.result_ref = raw.result_ref;
  if (nested.result_ref && typeof nested.result_ref === 'object') merged.result_ref = nested.result_ref;
  return merged;
}

function normalizeAuditState(value) {
  const key = String(value ?? '').trim().toLowerCase().replace(/[ -]+/g, '_');
  const aliases = {
    complete: 'completed', done: 'completed', pass: 'completed', passed: 'completed',
    ready: 'completed', success: 'completed', succeeded: 'completed',
    in_progress: 'running', queued: 'running', scanning: 'running',
    failed: 'failed', error: 'failed', blocked: 'failed',
    not_run: 'pending', not_started: 'pending', waiting: 'pending',
  };
  return aliases[key] || (['completed', 'running', 'failed', 'pending'].includes(key) ? key : 'unknown');
}

function auditIsCompleted(report = {}) {
  return normalizeAuditState(report.audit_state || report.auditStatus || report.status) === 'completed';
}

function auditStateLabel(report = {}) {
  const state = normalizeAuditState(report.audit_state || report.auditStatus || report.status);
  return ({completed: '扫描完成', running: '扫描中', failed: '扫描失败', pending: '待扫描'})[state] || '状态待确认';
}

function healthEvidenceUnavailable(report = {}) {
  const explicit = String(
    report.health_status || report.health_state || report.health_availability || ''
  ).trim().toLowerCase().replace(/[ -]+/g, '_');
  if (report.health_available === false || report.health_ready === false) return true;
  if (['pending', 'unavailable', 'stale', 'expired', 'not_scanned', 'unknown'].includes(explicit)) return true;
  const auditState = normalizeAuditState(report.audit_state || report.auditStatus || report.status);
  return !auditIsCompleted(report) || ['pending', 'running', 'failed', 'unknown'].includes(auditState);
}

function healthEvidenceLabel(report = {}) {
  const explicit = String(
    report.health_status || report.health_state || report.health_availability || ''
  ).trim().toLowerCase().replace(/[ -]+/g, '_');
  const labels = {pending: '待扫描', unavailable: '暂不可用', stale: '已过期', expired: '已过期', not_scanned: '待扫描', unknown: '暂不可用'};
  return labels[explicit] || auditStateLabel(report);
}

function healthScopeLabel(report = {}) {
  const health = report.health && typeof report.health === 'object' ? report.health : {};
  const scope = String(report.health_scope || health.scope || '').trim().toLowerCase();
  return ({reference_integrity: '引用完整性', governance: '治理健康度'})[scope] || '治理健康度';
}

function healthCoverageText(report = {}) {
  const coverage = report.health_coverage && typeof report.health_coverage === 'object' ? report.health_coverage : {};
  const components = report.health_components && typeof report.health_components === 'object' ? report.health_components : {};
  const covered = Array.isArray(coverage.covered) ? coverage.covered.filter(Boolean) : [];
  const outOfScope = Array.isArray(coverage.out_of_scope) ? coverage.out_of_scope.filter(Boolean) : [];
  const labels = {
    schema: '架构', storage_integrity: '存储完整性', references: '引用', delivery: '交付',
    bindings: '绑定', projection_freshness: '投影新鲜度', runtime_leases: '运行租约',
    host_integration: '宿主集成', token_usage: 'Token 用量',
  };
  const label = value => labels[String(value).trim()] || humanizeUiKey(value);
  if (coverage.status === 'complete') {
    const total = Object.keys(components).length || covered.length;
    if (!total) return '覆盖范围未提供';
    const checked = covered.length || total;
    const coveredText = covered.map(label).join('、') || '检查项';
    const outsideText = outOfScope.length ? '；未覆盖：' + outOfScope.map(label).join('、') : '';
    return `${checked}/${total} 项有证据 · 范围：${coveredText}${outsideText}`;
  }
  if (coverage.status === 'inconclusive') {
    const evidence = report.health_evidence && typeof report.health_evidence === 'object' ? report.health_evidence : {};
    const reasons = Array.isArray(evidence.inconclusive_codes) ? evidence.inconclusive_codes.filter(Boolean).map(label) : [];
    return '不可判定：' + (reasons.length ? '证据不完整（' + reasons.join('、') + '）' : '审计证据不完整');
  }
  return '覆盖范围未提供';
}

function normalizeAuditReport(raw) {
  const report = raw && typeof raw === 'object' ? {...raw} : {};
  const blockers = Array.isArray(report.blockers) ? report.blockers : [];
  const findings = Array.isArray(report.findings) ? report.findings : blockers.map((blocker, index) => {
    const blockerData = blocker && typeof blocker === 'object' ? {...blocker} : {};
    const code = blockerData.code || blockerData.rule_id || 'reference_audit_blocker';
    const domain = blockerData.domain || 'runtime';
    const table = blockerData.table || '';
    const location = blockerData.location && typeof blockerData.location === 'object'
      ? {...blockerData.location, span: Array.isArray(blockerData.location.span) ? blockerData.location.span : [1, 1]}
      : {path: table || domain, span: [1, 1]};
    const evidence = blockerData.evidence || blockerData.evidence_summary || blockerData.summary
      || `${domain}${table ? ' / ' + table : ''}`;
    return {
      ...blockerData,
      id: blockerData.finding_id || blockerData.id || `v2-audit-${index}-${code}`,
      severity: blockerData.severity || 'high',
      rule_id: blockerData.rule_id || code,
      evidence,
      dimension: blockerData.dimension || 'reference audit',
      surface: blockerData.surface || domain,
      location,
      impact: blockerData.impact || 'V2 参考审计未通过，当前结果不能作为完整健康证明。',
      suggestion: blockerData.suggestion || '先处理审计阻塞项，再重新扫描验证。',
      confidence: finiteNumber(blockerData.confidence, 1),
      fixable: blockerData.fixable === true,
    };
  });
  const summary = report.summary && typeof report.summary === 'object' ? {...report.summary} : {};
  const generatedAt = String(report.generated_at || report.completed_at || '').trim();
  const explicitState = normalizeAuditState(report.audit_state || report.auditStatus || report.status);
  const hasCompletedAudit = explicitState === 'completed'
    || Boolean(report.completed === true || report.audit_completed === true)
    || (explicitState === 'unknown' && Boolean(generatedAt));
  const rawHealth = optionalFiniteNumber(report.health_score);
  // Completion is an explicit backend fact. Health is optional evidence and
  // must never be fabricated from completion or a transport default.
  const health = hasCompletedAudit && rawHealth !== null
    ? Math.max(0, Math.min(100, rawHealth))
    : null;
  summary.object_count = finiteNumber(summary.object_count, finiteNumber(report.reference_count, 0));
  summary.invisible_count = finiteNumber(summary.invisible_count, 0);
  summary.finding_count_by_severity = summary.finding_count_by_severity || (findings.length ? {high: findings.length} : {});
  report.workspace = guiPathText(report.workspace, '');
  report.generated_at = generatedAt;
  report.audit_state = hasCompletedAudit ? 'completed' : (explicitState === 'unknown' ? 'pending' : explicitState);
  report.findings = findings;
  report.invisible = Array.isArray(report.invisible) ? report.invisible : [];
  report.summary = summary;
  report.health_score = health;
  return report;
}

function apiErrorMessage(result, fallback = '操作失败') {
  if (!result) return fallback;
  const error = result.error;
  if (typeof error === 'string' && error) return error;
  if (error && typeof error === 'object') return error.message || error.code || fallback;
  if (typeof result.code === 'string' && result.code && result.code !== 'ok') return result.code;
  return fallback;
}

function projectionEngineId(engine) {
  return String(engine && (engine.agent || engine.id || engine.engine_id) || '').trim();
}

async function refreshProjectionEngines() {
  try {
    const result = await callApi('list_host_llm_agents');
    const rows = Array.isArray(result?.agents) ? result.agents : [];
    projectionEngineState = {
      loaded: true,
      engines: rows.filter(item => projectionEngineId(item) && String(item.mode || 'cli').toLowerCase() === 'cli'),
      error: '',
    };
  } catch (error) {
    projectionEngineState = {loaded: true, engines: [], error: String(error && (error.message || error) || '')};
  }
  const valid = projectionEngineState.engines.some(item => projectionEngineId(item) === selectedProjectionEngine);
  if (selectedProjectionEngine !== 'deterministic' && !valid) {
    selectedProjectionEngine = 'deterministic';
    localStorage.setItem('memoryguard.projectionEngine', selectedProjectionEngine);
  }
  return projectionEngineState;
}

function setProjectionEngine(engineId) {
  const requested = String(engineId || '').trim();
  const valid = requested === 'deterministic'
    || projectionEngineState.engines.some(item => projectionEngineId(item) === requested);
  selectedProjectionEngine = valid ? requested : 'deterministic';
  localStorage.setItem('memoryguard.projectionEngine', selectedProjectionEngine);
  renderContent();
}

function projectionEngineControls() {
  if (!projectionEngineState.loaded) {
    return `<section class="card projection-engine-status"><div class="card-head"><div><h2>整理引擎</h2><p>正在检测可执行整理引擎；确定性构建可用。</p></div></div></section>`;
  }
  if (!projectionEngineState.engines.length) {
    const detail = projectionEngineState.error
      ? '暂时无法读取可执行引擎；为避免误导，LLM 整理不可选。'
      : '未发现可执行整理引擎，确定性构建可用；LLM 整理不可选。';
    return `<section class="card projection-engine-status"><div class="card-head"><div><h2>整理引擎</h2><p>${detail}</p></div><span class="chip chip-medium">仅确定性构建</span></div></section>`;
  }
  const options = [
    '<option value="deterministic">确定性构建（不使用 LLM）</option>',
    ...projectionEngineState.engines.map(engine => {
      const id = projectionEngineId(engine);
      const label = engine.label || id;
      return `<option value="${escapeHtml(id)}" ${selectedProjectionEngine === id ? 'selected' : ''}>${escapeHtml(label)} · CLI</option>`;
    }),
  ].join('');
  return `<section class="card projection-engine-status"><div class="card-head"><div><h2>整理引擎</h2><p>仅列出当前可执行的真实 CLI；不选择 LLM 时使用确定性构建。</p></div><span class="chip chip-confirmed">${projectionEngineState.engines.length} 个 CLI 可用</span></div>
    <label class="scope-select-label" for="projection-engine-select">构建整理方式</label>
    <select id="projection-engine-select" class="scope-select" onchange="setProjectionEngine(this.value)">${options}</select>
  </section>`;
}

function buildResultCode(result) {
  const error = result && result.error;
  const values = [
    result && result.code, result && result.status, result && result.reason,
    error && (typeof error === 'object' ? error.code : error),
    result && result.result_ref && result.result_ref.status,
    result && result.result && result.result.status,
  ];
  return values.filter(Boolean).map(value => String(value).toLowerCase()).join(' ');
}

function buildHasNoSources(result) {
  const code = buildResultCode(result);
  return code.includes('no_projection_sources') || code.includes('no_source') || code.includes('no_ir');
}

function buildIsBlocked(result) {
  const code = buildResultCode(result);
  return (result && result.blocked === true) || code.includes('blocked') || code.includes('block');
}

function buildResultMessage(result, fallback = '构建失败') {
  if (buildHasNoSources(result)) {
    return '没有可构建的数据源：当前数据源映射为空。请先到数据源页选择并启用数据源，再重新构建。';
  }
  if (buildIsBlocked(result)) {
    const reason = apiErrorMessage(result, '后端拒绝了当前构建');
    return `构建被后端阻止：${reason}。请先处理阻止原因后重试。`;
  }
  const code = buildResultCode(result);
  if (code.includes('timeout') || code.includes('timed_out') || result?.pending) {
    return '构建等待超时：任务未确认完成，已恢复记忆核心页面；请重新读取状态或重试。';
  }
  if (code.includes('no_active_projection_build')) return '当前没有可取消的构建';
  return apiErrorMessage(result, fallback);
}

function normalizeTaskState(result) {
  const task = result && result.task && typeof result.task === 'object' ? result.task : {};
  let state = String(task.state || result.status || result.state || '').toLowerCase();
  const legacy = {done:'succeeded', error:'failed', rejected:'failed', expired:'failed', cancelling:'cancelling'};
  state = legacy[state] || state || 'unknown';
  const progress = Number(task.progress != null ? task.progress : (result.processed != null ? result.processed : result.percent));
  return {
    state,
    run_id: task.run_id || result.job_id || result.request_id || '',
    progress: Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0,
    stage: task.stage || result.phase || result.stage || state,
    result_ref: result.result_ref && typeof result.result_ref === 'object'
      ? result.result_ref : (result.result && typeof result.result === 'object' ? result.result : {}),
    error: result.error || {},
  };
}

async function callApi(method, ...args) {
  let raw;
  // pywebview 和 localhost 都只经过 SafeBridge；HTTP 不再维护第二套请求队列。
  const bridge = window.pywebview && window.pywebview.api;
  if (bridge && typeof bridge.dispatch_api === 'function') {
    raw = await bridge.dispatch_api(method, args);
  } else if (bridge && typeof bridge.call_readonly === 'function'
      && typeof bridge.request_mutation === 'function') {
    // Backward-compatible shell fallback. Current desktop builds use dispatch_api
    // so mutation/read classification lives only in the server registry.
    const mutMethods = await getMutationMethods();
    raw = mutMethods.has(method)
      ? await bridge.request_mutation(method, args)
      : await bridge.call_readonly(method, args);
  } else {
    const headers = {'Content-Type': 'application/json'};
    if (window.__MG_SESSION__) headers['X-Session-Token'] = window.__MG_SESSION__;
    const resp = await fetch('/api/' + method, { method: 'POST', headers, body: JSON.stringify(args) });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(apiErrorMessage(body, 'API ' + method + ' 返回 ' + resp.status));
    raw = body;
  }
  const normalized = normalizeApiResult(raw);
  return method === 'get_audit' || method === 'run_audit'
    ? normalizeAuditReport(normalized)
    : normalized;
}

function sleepMs(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForTask(runId, label = '任务', timeoutMs = 120000, onProgress = null) {
  if (!runId) return {ok:false, error:`${label}未返回可追踪的 TaskRun`};
  const deadline = Date.now() + timeoutMs;
  let latest = {};
  while (Date.now() < deadline) {
    try {
      latest = await callApi('get_request_status', runId);
    } catch (error) {
      return {ok:false, error:`${label}状态查询失败：${error}`, execution_status:'failed'};
    }
    const task = normalizeTaskState(latest);
    if (typeof onProgress === 'function') onProgress(latest, task);
    if (task.state === 'succeeded') {
      return {...task.result_ref, ...latest, ok:true, request_id:runId, job_id:runId, execution_status:'succeeded'};
    }
    if (task.state === 'cancelled') {
      return {...latest, ok:false, cancelled:true, error:apiErrorMessage(latest, `${label}已取消`), execution_status:'cancelled'};
    }
    if (task.state === 'failed') {
      return {...latest, ok:false, error:apiErrorMessage(latest, `${label}失败`), execution_status:'failed'};
    }
    await sleepMs(300);
  }
  return {...latest, ok:false, pending:true, error:`${label}等待超时`, execution_status:normalizeTaskState(latest).state};
}

async function waitForMutation(result, label = '操作', timeoutMs = 120000) {
  result = normalizeApiResult(result || {});
  const task = normalizeTaskState(result);
  const runId = task.run_id || (result.request && result.request.request_id) || '';
  if (!result.deferred && !runId) return result;
  return await waitForTask(runId, label, timeoutMs);
}

// 知识书库入口：跳转到书架页（localhost / pywebview 均可用）。
// pywebview 下用 window.location 切到 /knowledge；localhost 下同样导航。
async function openKnowledge() {
  try {
    window.location.href = '/knowledge';
  } catch (_) {
    // 极端情况：原地刷新
    window.location.reload();
  }
}

async function refreshKnowledgeCount() {
  try {
    const data = await callApi('knowledge_candidates_list', '', 'pending');
    const n = (data && data.total) || 0;
    const el = document.getElementById('knowledge-count');
    if (el) el.textContent = n > 0 ? String(n) : '';
  } catch (_) { /* 知识库未初始化时静默 */ }
}

// Optional lifecycle endpoints are intentionally feature-detected so an older
// GUI can still browse the existing rules page while the rule cockpit service
// is being rolled out.  Mutations still go through callApi (and therefore the
// normal pywebview/request-queue bridge) when the endpoint is present.
async function callApiOptional(method, fallback, ...args) {
  try { return await callApi(method, ...args); }
  catch (_) { return fallback; }
}

function waitForPywebview(timeoutMs) {
  return new Promise((resolve) => {
    // localhost pages do not expose pywebview; the server-issued session
    // token is the trusted signal that the HTTP bridge is ready.
    if ((window.pywebview && window.pywebview.api) || window.__MG_SESSION__) return resolve(true);
    let elapsed = 0;
    const interval = setInterval(() => {
      elapsed += 100;
      if ((window.pywebview && window.pywebview.api) || window.__MG_SESSION__) {
        clearInterval(interval); resolve(true);
      }
      else if (elapsed >= timeoutMs) { clearInterval(interval); resolve(false); }
    }, 100);
  });
}

async function loadGovernanceScopePreference() {
  try {
    const runtime = await callApi('get_governance_scope_state');
    const normalized = normalizeGovernanceScope(runtime);
    governanceScopeState = normalized;
    if (normalized.status === 'audit_only' || normalized.status === 'stale_selection') {
      clearSharedGovernance(normalized.status, normalized.reason);
      activeAgentInstanceId = '';
    } else if (runtime && runtime.ok && runtime.scope && normalized.share_group_id) {
      activeShareGroupId = normalized.share_group_id;
      activeAgentInstanceId = runtime.principal_agent_instance_id || '';
      activeScopeMemberIds = normalized.members;
      dataPageMode = 'multi_agent_shared_mcp';
    } else if (runtime && runtime.ok && runtime.scope && normalized.agent_instance_id) {
      activeAgentInstanceId = normalized.agent_instance_id;
      activeScopeMemberIds = [activeAgentInstanceId];
      activeShareGroupId = '';
      dataPageMode = 'single_agent';
    } else {
      activeAgentInstanceId = '';
      activeScopeMemberIds = [];
      activeShareGroupId = '';
    }
  } catch (_) {}
}

function isShareGroupScope() {
  return dataPageMode === 'multi_agent_shared_mcp' && !!activeShareGroupId;
}

function memoryGroupKind(groupId) {
  return String(groupId || '').startsWith('personal-') ? 'personal' : 'shared';
}

function memoryGroupLabel(groupId) {
  return memoryGroupKind(groupId) === 'personal' ? '个人记忆层' : '共享记忆层';
}

function looksLikeOpaqueAgentId(value, id = '') {
  const text = String(value || '').trim();
  if (!text) return true;
  if (id && text === id) return true;
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{19,}$/i.test(text)) return true;
  if (/^[0-9a-f]{16,}$/i.test(text)) return true;
  if (/(?:agent|instance|inst)[-_:]/i.test(text) && /[0-9a-f]{8,}/i.test(text)) return true;
  return false;
}

function readableAgentPart(value, id = '') {
  const text = String(value || '').trim();
  return text && !looksLikeOpaqueAgentId(text, id) ? text : '';
}

function agentDisplayName(agentOrId, fallback = '未知助手') {
  const id = typeof agentOrId === 'object'
    ? String(agentOrId.instance_id || agentOrId.agent_instance_id || agentOrId.id || agentOrId.agent || agentOrId.binding_id || '')
    : String(agentOrId || '');
  const direct = typeof agentOrId === 'object' ? agentOrId : null;
  const pools = [
    agentCardsData?.agents || [], agentCardsData?.instances || [], agentCardsData?.residuals || [],
    discoveryResult?.instances || [], discoveryResult?.agents || [],
  ];
  let match = direct;
  if (!match) {
    for (const pool of pools) {
      match = pool.find(item => String(item.instance_id || item.agent_instance_id || item.id || item.agent || '') === id);
      if (match) break;
    }
  }
  const current = id && id === activeAgentInstanceId;
  const alias = match && (match.user_alias || match.alias || match.user_label);
  const discoveredName = match && (match.display_name || match.product || match.label || match.name || match.agent_name || match.member_name);
  const provider = match && (match.provider || match.provider_name || match.product);
  const program = match && (match.program_name || match.program || match.client_name || match.host_name);
  // Keep unknown identities readable without exposing a stable ID.
  const fallbackLabel = readableAgentPart(fallback, id);
  const rawProject = match && (match.project_ref || match.project);
  const project = String(rawProject || '').replaceAll('\\', '/').replace(/\/+$/, '').split('/').filter(Boolean).pop() || '';
  let label = readableAgentPart(alias, id) || readableAgentPart(program, id) || readableAgentPart(provider, id)
    || readableAgentPart(discoveredName, id)
    || fallbackLabel
    || (readableAgentPart(program, id) && readableAgentPart(provider, id) ? `${program} · ${provider}` : '')
    || (readableAgentPart(provider, id) && readableAgentPart(project, id) ? `${provider} · ${project}` : '');
  const productLabels = {codex: 'Codex', claude: 'Claude Code', cursor: 'Cursor', trae: 'Trae', grok: 'Grok', workbuddy: 'WorkBuddy', chatgpt: 'ChatGPT', copilot: 'GitHub Copilot'};
  const family = agentFamily(match || agentOrId);
  // Provider/program/display_name are identity hints, not user-facing names.
  // Collapse known product slugs to family labels; keep opaque IDs in details.
  if (!alias && productLabels[family]) label = productLabels[family];
  if (!label || /^(?:未知助手|未知\s*Agent|unknown)$/i.test(label)) label = '未识别的 MCP 助手';
  if (/^codex$/i.test(label)) label = 'Codex';
  if (current && !label.includes('（当前）')) label += '（当前）';
  return label;
}

function agentIdentityDetail(agentOrId) {
  const id = typeof agentOrId === 'object'
    ? String(agentOrId.instance_id || agentOrId.agent_instance_id || agentOrId.id || agentOrId.agent || agentOrId.binding_id || '')
    : String(agentOrId || '');
  return id;
}

// Presentation-only identity helpers. They consume trusted discovery metadata
// and never fetch remote favicons or expose opaque IDs as the visible label.
function agentFamily(agentOrId) {
  const item = agentOrId && typeof agentOrId === 'object' ? agentOrId : {};
  const text = [item.provider, item.provider_name, item.product, item.display_name, item.agent_name, item.member_name,
    item.program_name, item.program, item.client_name, item.host_name,
    typeof agentOrId === 'string' ? agentOrId : ''].join(' ').toLowerCase();
  if (/workbuddy/.test(text)) return 'workbuddy';
  if (/grok|xai|x\.ai/.test(text)) return 'grok';
  if (/trae(?:\b|[-_.])|trae\.ai/.test(text)) return 'trae';
  if (/codex|openai-codex/.test(text)) return 'codex';
  if (/chatgpt|openai/.test(text)) return 'chatgpt';
  if (/copilot|github/.test(text)) return 'copilot';
  if (/claude|anthropic/.test(text)) return 'claude';
  if (/cursor/.test(text)) return 'cursor';
  return 'unknown';
}

function agentIconMarkup(agentOrId) {
  const family = agentFamily(agentOrId);
  // Keep marks local and deterministic.  These are small product silhouettes,
  // not remote favicons: the family is still resolved from trusted discovery
  // metadata and the technical ID remains available below the card.
  const marks = {
    codex: '<span class="agent-mark-text">Codex</span>',
    claude: '<svg class="agent-icon-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M21 10.5h3v3h-3v3h-1.5v3H18v-3h-1.5v3H15v-3H9v3H7.5v-3H6v3H4.5v-3H3v-3H0v-3h3v-6h18Zm-15 0h1.5v-3H6Zm10.5 0H18v-3h-1.5z"/></svg>',
    cursor: '<svg class="agent-icon-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23"/></svg>',
    trae: '<svg class="agent-icon-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M24 20.5H3.5V17H0V3.5h24ZM3.5 17h17V7h-17Zm8.5-5-2.5 2.5L7 12l2.5-2.5Zm7 0-2.5 2.5L14 12l2.5-2.5z"/></svg>',
    grok: '<span class="agent-mark-text">Grok</span>',
    workbuddy: '<span class="agent-mark-text">WorkBuddy</span>',
    chatgpt: '<svg class="agent-icon-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 4.1a4.2 4.2 0 0 1 7.2 3 4.2 4.2 0 0 1 1 7.8 4.2 4.2 0 0 1-4.2 5.8 4.2 4.2 0 0 1-7.2 0 4.2 4.2 0 0 1-5.1-5.8 4.2 4.2 0 0 1 1-7.8 4.2 4.2 0 0 1 7.3-3Z"/><path d="m8.1 15.6 7.8-4.5M8.5 9.4l7.8 4.5"/></svg>',
    copilot: '<svg class="agent-icon-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 8.1c0-2 1.6-3.6 3.6-3.6h1.5c1 0 1.9.4 2.6 1.1l.3.3.3-.3c.7-.7 1.6-1.1 2.6-1.1h1.5c2 0 3.6 1.6 3.6 3.6v7.8c0 2-1.6 3.6-3.6 3.6h-1.5c-1 0-1.9-.4-2.6-1.1l-.3-.3-.3.3c-.7.7-1.6 1.1-2.6 1.1H8.6c-2 0-3.6-1.6-3.6-3.6V8.1Z"/><path d="M8 10.5h3M13 10.5h3M8 14h3M13 14h3"/></svg>',
    unknown: '?',
  };
  return `<span class="agent-avatar" data-agent-family="${family}" aria-label="${family} product mark">${marks[family] || marks.unknown}</span>`;
}

function agentSourceSummary(agentOrId) {
  const item = agentOrId && typeof agentOrId === 'object' ? agentOrId : {};
  return readableAgentPart(item.source_summary || item.summary || item.description || item.capability_summary, '')
    || '尚未返回可读来源摘要；可从本机 Agent 检测或匹配入口接入。';
}

function openSettings() {
  switchTab('governance');
  showToast('设置入口已打开治理控制台', 'info');
}

function agentNamesForIds(ids) {
  return [...new Set((ids || []).map(id => String(id || '')).filter(Boolean))]
    .map(id => agentDisplayName(id)).filter(Boolean);
}

function scopeSelectionLabel() {
  // ``activeAgentInstanceId`` also drives the data-source card selection.  It
  // must not be rendered as an authorized governance scope until the trusted
  // scope endpoint has confirmed an active binding.
  if (governanceScopeState.status !== 'active') return '未选择治理范围';
  if (isShareGroupScope()) {
    const names = agentNamesForIds(activeScopeMemberIds);
    return names.length ? `共享组 · ${names.join('、')}` : '共享记忆组';
  }
  return activeAgentInstanceId ? agentDisplayName(activeAgentInstanceId) : '未选择治理范围';
}

async function ensureAgentLabels() {
  if (agentCardsData) return;
  try { agentCardsData = await callApi('list_agents'); } catch (_) { /* labels are best effort */ }
}

function scopeApiArgs() {
  if (isShareGroupScope()) {
    return [{ mode: 'share_group', share_group_id: activeShareGroupId }, '', activeShareGroupId];
  }
  if (activeAgentInstanceId) {
    return [{ mode: 'agent', agent_instance_id: activeAgentInstanceId }, activeAgentInstanceId, ''];
  }
  return [null, '', ''];
}

async function setActiveShareGroup(groupId) {
  const normalized = String(groupId || '').trim();
  if (!normalized) return {ok: false, error: 'share_group_id_required'};
  const result = await waitForMutation(
    await callApi('set_governance_scope', {
      mode: 'share_group',
      share_group_id: normalized,
    }),
    '切换治理范围',
  );
  if (!result || result.error || result.ok === false) {
    throw new Error((result && result.error) || '治理范围保存失败');
  }
  activeShareGroupId = normalized;
  activeScopeMemberIds = result.members || result.agent_instance_ids || activeScopeMemberIds;
  dataPageMode = 'multi_agent_shared_mcp';
  return result;
}

async function init() {
  const ready = await waitForPywebview(5000);
  if (!ready) { showToast('GUI 桥接未就绪，请稍后重试', 'error'); return; }
  try {
    // scope 偏好失败不阻塞首屏
    await loadGovernanceScopePreference();
  } catch (_) {}
  try {
    state.report = await callApi('get_audit');
    renderAll();
  } catch (e) {
    showToast('扫描失败：' + e, 'error');
    // 即使审计失败也渲染空壳，避免永久转圈、无法切 tab
    if (!state.report) {
      state.report = { findings: [], summary: {}, workspace: '', health_score: 0 };
      renderAll();
    }
  }
  refreshKnowledgeCount();
}

function setAuditBusy(busy) {
  document.querySelectorAll('.audit-refresh-button').forEach(button => {
    button.disabled = busy;
    button.setAttribute('aria-busy', busy ? 'true' : 'false');
  });
}

async function runAudit() {
  if (tokenUsageState.auditInFlight) return tokenUsageState.auditInFlight;
  const request = (async () => {
    setAuditBusy(true);
    setContent('<div class="loading">正在同步用量数据并重新扫描工作区</div>');
    const sync = await syncUsageTelemetry({refresh: false});
    const syncFailed = !sync || sync.ok === false || ['error', 'failed'].includes(String(sync.status || '').toLowerCase());
    try {
      state.report = await callApi('run_audit');
      showToast(`用量数据：${tokenUsageSyncStatusText(sync)}；扫描：完成`, syncFailed ? 'error' : 'success');
      renderAll();
    } catch (e) {
      showToast(`用量数据：${tokenUsageSyncStatusText(sync)}；扫描失败：${e}`, 'error');
    }
  })();
  tokenUsageState.auditInFlight = request;
  try {
    return await request;
  } finally {
    if (tokenUsageState.auditInFlight === request) tokenUsageState.auditInFlight = null;
    setAuditBusy(false);
  }
}

function syncNavigationState(tab = state.activeTab) {
  document.querySelectorAll('.nav-item[data-tab]').forEach(el => {
    const active = el.dataset.tab === tab;
    el.classList.toggle('active', active);
    el.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

// HTML ships with an overview fallback class. Apply the actual hash/local state
// before the bridge is ready so a fresh deep link never flashes the wrong tab.
syncNavigationState(state.activeTab);

function switchTab(tab) {
  state.activeTab = tab;
  localStorage.setItem('memoryguard.activeTab', tab);
  if (knownGuiTabs.includes(tab) && currentGuiHash().replace(/^#/, '') !== tab) {
    try {
      if (typeof history !== 'undefined' && typeof history.replaceState === 'function') history.replaceState(null, '', '#' + tab);
      else if (typeof window !== 'undefined' && window.location) window.location.hash = tab;
    } catch (_) {
      try { if (typeof window !== 'undefined' && window.location) window.location.hash = tab; } catch (_) {}
    }
  }
  decorateViewHeading();
  if (tab !== 'neurons') {
    selectedNeuronId = null;
    selectedNeuronNode = null;
    selectedNeuronIds = new Set();
    neuronDragState = null;
  }
  if (tab !== 'codegraph') {
    selectedCodeGraphNode = null;
    if (codeCyInstance) {
      try { codeCyInstance.destroy(); } catch (_) { /* graph torn down */ }
      codeCyInstance = null;
    }
  }
  syncNavigationState(tab);
  renderStatusRail();
  renderContent();
}

document.querySelectorAll('.nav-item').forEach(el => el.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); switchTab(el.dataset.tab); }
}));
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('hashchange', () => {
    const tab = guiTabFromHash();
    if (knownGuiTabs.includes(tab) && tab !== state.activeTab) switchTab(tab);
  });
}

function setContent(html, renderToken = null) {
  if (!contentRenderIsCurrent(renderToken)) return false;
  document.getElementById('content').innerHTML = html;
  decorateViewHeading();
  bindRuleHistoryActionDelegation();
  return true;
}

const PAGE_CHROME = {
  overview: {index: '1', label: 'Governance Overview', subtitle: '治理总览'},
  sources: {index: '2', label: 'Data Sources', subtitle: '数据源与代理'},
  neurons: {index: '3', label: 'Memory Core', subtitle: '记忆核心 · 神经图谱'},
  codegraph: {index: '4', label: 'CodeGraph', subtitle: '代码图谱'},
  rules: {index: '5', label: 'Rules & Habits', subtitle: '规则与习惯'},
  history: {index: '6', label: 'Conversation History', subtitle: '对话历史'},
  findings: {index: '7', label: 'Risk Signals', subtitle: '风险信号与治理控制台'},
  'token-usage': {index: '8', label: 'Token Usage', subtitle: 'Token 用量与 MCP 节省'},
  releases: {index: '·', label: 'Change Log', subtitle: '变更记录'},
  governance: {index: '·', label: 'Governance Console', subtitle: '治理控制台'},
};

function decorateViewHeading() {
  const heading = document.querySelector('.view-heading');
  const meta = PAGE_CHROME[state.activeTab] || PAGE_CHROME.overview;
  const context = document.getElementById('topbar-context');
  if (context) context.textContent = meta.subtitle;
  if (document.body?.dataset) document.body.dataset.activeTab = state.activeTab || 'overview';
  if (!heading) return;
  heading.classList.add('page-heading');
  heading.dataset.pageIndex = meta.index;
  heading.dataset.pageSubtitle = meta.subtitle;
  const eyebrow = heading.querySelector('.eyebrow');
  if (eyebrow) eyebrow.textContent = meta.label;
}

function bindRuleHistoryActionDelegation() {
  if (document.documentElement.dataset.ruleHistoryDelegated) return;
  document.documentElement.dataset.ruleHistoryDelegated = '1';
  document.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-mg-action]');
    if (!target) return;
    const action = target.dataset.mgAction;
    const memoryId = target.dataset.memoryId || '';
    const sessionId = target.dataset.sessionId || '';
    const turnId = target.dataset.turnId || '';
    const nodeId = target.dataset.nodeId || '';
    if (!action) return;
    event.preventDefault();
    if (action === 'rule-edit') return ensureRuleAudienceEditor(memoryId);
    if (action === 'neuron-select-node') return selectNeuron(nodeId, true);
    if (action === 'neuron-open-virtual') return routeVirtualNeuron(selectedNeuronNode);
    if (action === 'neuron-rule-edit-body') return openNeuronRuleBodyEditor(memoryId);
    if (action === 'neuron-rule-delete') return governNeuronRule(memoryId, 'delete_memory');
    if (action === 'neuron-rule-restore') return governNeuronRule(memoryId, 'restore_memory');
    if (action === 'neuron-rule-body-close') return removeNeuronRuleBodyModal();
    if (action === 'neuron-rule-body-save') return saveNeuronRuleBody(memoryId);
    if (action === 'neuron-history-read') return openNeuronHistorySession(sessionId);
    if (action === 'rule-modal-close') return removeRuleAudienceModal();
    if (action === 'rule-save') return saveRuleAudience(memoryId);
    if (action === 'history-read-session') return readHistorySession(sessionId);
    if (action === 'history-extract') return previewHistoryExtract(sessionId);
    if (action === 'history-export') return exportHistorySession(sessionId);
    if (action === 'history-delete') return deleteHistorySession(sessionId);
    if (action === 'history-timeline') return showHistoryTimeline(sessionId, turnId);
if (action === 'history-read-turn') return readHistoryTurn(turnId);
if (action === 'history-back') return renderHistory();
if (action === 'history-search') return searchHistory();
if (action === 'history-backfill') return runHistoryBackfill();
  });
}

function renderAll() {
  if (!state.report) return;
  const r = state.report;
  const findings = Array.isArray(r.findings) ? r.findings : [];
  const health = optionalFiniteNumber(r.health_score);
  const healthUnavailable = healthEvidenceUnavailable(r);
  const hasCompletedAudit = auditIsCompleted(r);
  document.getElementById('ws-path').textContent = guiPathText(r.workspace, '');
  const badge = document.getElementById('health-badge');
  document.getElementById('reader-auto')?.classList.toggle('active', readerLanguage === 'auto');
  document.getElementById('reader-zh')?.classList.toggle('active', readerLanguage === 'zh');
  document.getElementById('reader-en')?.classList.toggle('active', readerLanguage === 'en');
  badge.textContent = healthUnavailable
    ? '健康度 ' + healthEvidenceLabel(r)
    : hasCompletedAudit
    ? (health === null ? '健康度 ' + (findings.length ? `需处理 ${findings.length} 项` : '审计通过（未提供量化评分）') : healthScopeLabel(r) + ' ' + Math.round(health) + '/100')
    : '健康度 ' + auditStateLabel(r);
  badge.style.color = healthUnavailable ? 'var(--orange)' : !hasCompletedAudit ? 'var(--faint)'
    : health === null ? 'var(--orange)'
    : health >= 70 ? 'var(--accent)' : health >= 40 ? 'var(--orange)' : 'var(--red)';
  document.getElementById('findings-count').textContent = findings.length || '';
  document.getElementById('sources-count').textContent = '';
  document.getElementById('releases-count').textContent = state.releases ? state.releases.length : '';
  syncNavigationState(state.activeTab);
  renderContent();
  loadGovernanceSnapshot();
}

async function loadGovernanceSnapshot() {
  if (!activeShareGroupId) {
    state.governanceSnapshot = null;
    renderStatusRail();
    return;
  }
  try {
    const [rawSnapshot, rawScope, rawConflicts] = await Promise.all([
      callApi('get_governance_snapshot', activeShareGroupId),
      callApi('get_governance_scope_state'),
      callApi('get_conflicts', activeShareGroupId),
    ]);
    // The snapshot endpoint keeps a compatibility count, while conflict
    // queue owns the authoritative unclosed/actionable/stale totals. Merge
    // that bounded summary before rendering the overview and status rail.
    const snapshotInput = {...objectValue(rawSnapshot)};
    if (rawConflicts && rawConflicts.ok !== false) {
      snapshotInput.conflicts = {...objectValue(snapshotInput.conflicts), ...objectValue(rawConflicts)};
    }
    const snapshot = governanceSnapshot(snapshotInput);
    const runtimeScope = normalizeGovernanceScope(rawScope, activeShareGroupId);
    const snapshotScope = normalizeGovernanceScope(snapshot, activeShareGroupId);
    const trustedScope = runtimeScope.status === 'active' ? runtimeScope : snapshotScope;
    if (snapshot.audit_only || runtimeScope.status === 'audit_only' || snapshotScope.status === 'audit_only') {
      state.governanceSnapshot = {...snapshot, audit_only: true};
      clearSharedGovernance('audit_only', 'audit_only');
      renderStatusRail();
      if (state.activeTab === 'overview') renderOverview();
      return;
    }
    if (trustedScope.status !== 'active') {
      state.governanceSnapshot = {...snapshot, non_governable: true};
      clearSharedGovernance('stale_selection', 'active_binding_required');
      renderStatusRail();
      if (state.activeTab === 'overview') renderOverview();
      return;
    }
    governanceScopeState = trustedScope;
    activeScopeMemberIds = trustedScope.members;
    state.governanceSnapshot = {...snapshot, active_binding: trustedScope.binding, members: trustedScope.members};
    renderStatusRail();
    if (state.activeTab === 'overview') renderOverview();
  } catch (e) { /* 静默失败，状态栏显示占位 */ }
}

function renderStatusRail() {
  const container = document.getElementById('status-rail-content');
  const title = document.querySelector('#status-rail h3');
  if (!container) return;
  if (governanceScopeState.status === 'audit_only') {
    if (title) title.textContent = '治理状态';
    container.innerHTML = `<div class="status-item danger"><span class="status-label">共享治理</span><span class="status-num">仅审计</span></div>
      <div class="rail-warning">当前共享组没有可用治理绑定，已停止共享治理投影。</div>
      <div class="rail-link" onclick="switchTab('sources')">去数据源恢复绑定 →</div>`;
    return;
  }
  if (governanceScopeState.status === 'stale_selection') {
    if (title) title.textContent = '治理状态';
    container.innerHTML = `<div class="status-item danger"><span class="status-label">共享治理</span><span class="status-num">未激活</span></div>
      <div class="rail-warning">已清除失效共享组选择：当前没有活动绑定，不能作为共享治理范围。</div>
      <div class="rail-link" onclick="switchTab('sources')">去数据源选择有效绑定 →</div>`;
    return;
  }
  if (state.activeTab === 'codegraph') {
    if (title) title.textContent = 'CodeGraph';
    container.innerHTML = renderCodeGraphRail();
    return;
  }
  if (state.activeTab === 'findings') {
    if (title) title.textContent = '风险与治理';
    const findings = Array.isArray(state.report?.findings) ? state.report.findings : [];
    const counts = {critical: 0, high: 0, medium: 0, low: 0};
    findings.forEach(item => {
      const key = String(item?.severity || '').toLowerCase();
      if (key in counts) counts[key] += 1;
    });
    const highCount = counts.critical + counts.high;
    container.innerHTML = `
      <div class="status-item ${highCount ? 'danger' : 'zero'}"><span class="status-label">高风险</span><span class="status-num">${highCount}</span></div>
      <div class="status-item ${counts.medium ? 'alert' : 'zero'}"><span class="status-label">中风险</span><span class="status-num">${counts.medium}</span></div>
      <div class="status-item ${counts.low ? '' : 'zero'}"><span class="status-label">低风险</span><span class="status-num">${counts.low}</span></div>
      <div class="status-item ${findings.length ? '' : 'zero'}"><span class="status-label">风险条目</span><span class="status-num">${findings.length}</span></div>
      <div class="rail-link" onclick="switchTab('governance')">打开治理台 →</div>`;
    return;
  }
  if (state.activeTab === 'neurons' && selectedNeuronNode) {
    if (title) title.textContent = '节点详情';
    container.innerHTML = renderNeuronRailDetail(selectedNeuronNode);
    return;
  }
  if (title) title.textContent = state.activeTab === 'neurons' ? '神经图详情' : '治理状态';
  if (state.activeTab === 'neurons') {
    container.innerHTML = `<div class="status-item zero"><span class="status-label">当前选择</span><span class="status-num">—</span></div>
      <div class="rail-link" onclick="switchTab('governance')">进入治理台 →</div>`;
    return;
  }
  if (!activeShareGroupId) {
    container.innerHTML = `<div class="status-item zero"><span class="status-label">记忆治理组</span><span class="status-num">—</span></div>
      <div class="rail-link" onclick="switchTab('governance')">选择治理组 →</div>`;
    return;
  }
  const snap = state.governanceSnapshot;
  if (!snap) {
    container.innerHTML = '<div class="loading" style="min-height:60px">连接中…</div>';
    return;
  }
  const counts = objectValue(snap.counts);
  const activeCount = finiteNumber(counts.active_memories, 0);
  const conflictCount = finiteNumber(counts.conflicts, 0);
  const conflictDetail = conflictDispositionText(snap);
  const quarantineCount = finiteNumber(counts.quarantined, 0);
  const rollbackCount = finiteNumber(counts.rollback_ready, 0);
  const conflictClass = conflictCount > 0 ? 'alert' : 'zero';
  const quarantineClass = quarantineCount > 0 ? 'danger' : 'zero';
  const rollbackClass = rollbackCount > 0 ? '' : 'zero';
  container.innerHTML = `
    <div class="status-item" onclick="switchTab('governance')">
      <span class="status-label">有效记忆</span>
      <span class="status-num">${activeCount}</span>
    </div>
    <div class="status-item ${conflictClass}" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('conflicts'),50)">
      <span class="status-label">冲突</span>
      <span><span class="status-num">${conflictCount}</span>${conflictDetail ? `<small class="surface-meta" style="display:block;text-align:right">${escapeHtml(conflictDetail)}</small>` : ''}</span>
    </div>
    <div class="status-item ${quarantineClass}" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('quarantine'),50)">
      <span class="status-label">隔离</span>
      <span class="status-num">${quarantineCount}</span>
    </div>
    <div class="status-item ${rollbackClass}" onclick="switchTab('releases')">
      <span class="status-label">可回滚</span>
      <span class="status-num">${rollbackCount}</span>
    </div>
    <div class="rail-link" onclick="switchTab('governance')">打开治理台 →</div>`;
}

function renderNeuronRailDetail(node) {
  const nodes = (neuronGraph && neuronGraph.nodes) ? neuronGraph.nodes : [];
  const childCount = nodes.filter(n => n.parent_id === node.id).length;
  const isAnchor = node.node_kind === 'claim_anchor' || node.node_kind === 'duplicate_cluster';
  const isHub = node.node_kind === 'source_hub';
  const kindText = node.node_kind === 'topic' ? topicNodeLabel(node) : memoryKindLabel(node.kind || node.label || '');
  const title = isAnchor
    ? (displayTitle(node) || node.label || '未命名记忆')
    : (isHub ? (node.title || node.label || '同源突触')
      : (node.node_kind === 'topic' ? kindText : (node.label || '记忆胞体')));

  const virtualChildren = (heading = '分支（点击聚焦）') => {
    const children = nodes.filter(item => item.parent_id === node.id);
    if (!children.length) return '';
    return `<div class="claim-list"><h4>${escapeHtml(heading)}</h4>${children.map(item =>
      `<button class="raw-file-row" type="button" data-mg-action="neuron-select-node" data-node-id="${escapeHtml(item.id || '')}">
        <span>${escapeHtml(item.label || item.title || '未命名节点')}</span><span class="chip chip-medium">${Number(item.count || 0) || ''}</span>
      </button>`).join('')}</div>`;
  };

  // Virtual nodes are graph-native indexes.  Selecting them must never change
  // the current tab: the rail is the governance surface, and cross-page reads
  // are deliberate secondary actions only.
  if (node.node_kind === 'virtual_rule_ref') {
    const policy = node.injection_policy === 'always' ? '强制注入' : '按需召回';
    const audience = node.audience || ruleAudience({assignments: node.assignments || []});
    const restorable = ['deleted', 'shadowed', 'superseded'].includes(node.status || '');
    const branchLabels = {source: '来源', classification: '自动分类', scope: '适用范围', history: '历史版本', conflict: '冲突', exception: '例外'};
    const branchRows = (node.detail_branches || []).map(branch => {
      const items = Array.isArray(branch.items) ? branch.items : [];
      const rows = items.map(item => {
        if (branch.branch_type === 'source') return `<div class="raw-file-row"><span>来源记忆</span><code>${escapeHtml(item.memory_id || '')}</code></div>`;
        if (branch.branch_type === 'scope') return `<div class="raw-file-row"><span>${escapeHtml(ruleAudience({assignments: [item]}))}</span><code>${escapeHtml([item.target_id, item.project_ref].filter(Boolean).join(' / '))}</code></div>`;
        if (branch.branch_type === 'classification') return `<div class="raw-file-row"><span>${escapeHtml(item.rule_kind || '规则')}</span><span class="muted">${escapeHtml([item.polarity, item.rule_strength, item.maturity_state].filter(Boolean).join(' · '))}</span></div>`;
        if (branch.branch_type === 'history') return `<div class="raw-file-row"><span>版本 ${escapeHtml(String(item.revision || item.version || ''))}</span><span class="muted">仅显示已有版本元数据</span></div>`;
        return `<div class="raw-file-row"><span>${escapeHtml(item.label || item.reason || item.value || '已记录详情')}</span><span class="muted">${escapeHtml(item.id || '')}</span></div>`;
      }).join('');
      const label = branchLabels[branch.branch_type] || branch.label || '详情';
      return `<details class="neuron-detail-branch"><summary>${escapeHtml(label)} · ${items.length} 项（展开）</summary><div class="claim-list">${rows || '<p class="muted">暂无可展示详情。</p>'}</div></details>`;
    }).join('');
    return `<div class="popover-kicker">受治理规则 · 图内管理</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
      <div class="detail-section"><h4>规则正文</h4><div class="neuron-detail-body">${escapeHtml(node.body || '暂无正文内容')}</div></div>
      <div class="row"><span class="key">类型</span><span>${escapeHtml(memoryKindLabel(node.kind || ''))}</span></div>
      <div class="row"><span class="key">状态</span><span>${escapeHtml(node.status || 'active')}</span></div>
      <div class="row"><span class="key">注入</span><span>${escapeHtml(policy)}${node.injection_policy === 'always' ? ` · 优先级 ${Number(node.priority || 0)}` : ''}</span></div>
      <div class="row"><span class="key">适用范围</span><span>${escapeHtml(audience || '按需规则，无固定范围')}</span></div>
      ${branchRows ? `<div class="detail-section"><h4>规则详情分支</h4>${branchRows}</div>` : ''}
      ${node.locked ? '<div class="muted">此规则已锁定；正文修改将由治理层校验。</div>' : ''}
      <div class="finding-actions" style="margin:12px 0 6px;display:flex;flex-wrap:wrap;gap:6px">
        <button class="btn" type="button" data-mg-action="neuron-rule-edit-body" data-memory-id="${escapeHtml(node.memory_id || '')}">编辑正文</button>
        <button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(node.memory_id || '')}">管理适用范围</button>
        ${restorable
          ? `<button class="btn btn-primary" type="button" data-mg-action="neuron-rule-restore" data-memory-id="${escapeHtml(node.memory_id || '')}">恢复</button>`
          : `<button class="btn btn-danger" type="button" data-mg-action="neuron-rule-delete" data-memory-id="${escapeHtml(node.memory_id || '')}">软删除</button>`}
      </div>`;
  }

  if (node.node_kind === 'history_session') {
    return `<div class="popover-kicker">会话历史索引 · 按需读取</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(node.title || node.label || '会话')}</h3>
      <div class="neuron-detail-body">${escapeHtml(node.summary || '尚无摘要；原文仍隔离在本地历史库。')}</div>
      <div class="row"><span class="key">来源</span><span>${escapeHtml(node.provider || 'local')}</span></div>
      ${node.project_ref ? `<div class="row"><span class="key">项目</span><span>${escapeHtml(node.project_ref)}</span></div>` : ''}
      <div class="row"><span class="key">时间</span><span>${escapeHtml(node.created_at || node.imported_at || '未知')}</span></div>
      <div class="row"><span class="key">记录</span><span>${Number(node.turn_count || 0)} 条对话 · ${Number(node.evidence_count || 0)} 条已萃取证据</span></div>
      <div class="muted">点击此节点只在神经图聚焦；原文不会进入长期记忆或 bootstrap。</div>
      <div class="finding-actions" style="margin-top:12px"><button class="btn" type="button" data-mg-action="neuron-history-read" data-session-id="${escapeHtml(node.session_id || '')}">在历史页读取原文</button></div>`;
  }

  if (node.node_kind === 'virtual_bucket') {
    return `<div class="popover-kicker">规则分支 · 图内管理</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
      <div class="status-item zero"><span class="status-label">规则条目</span><span class="status-num">${node.count || childCount}</span></div>
      <div class="neuron-detail-body">这是图内规则索引：选择下方规则节点即可在左侧直接治理；原生文件不改动。</div>
      ${node.has_more ? '<div class="muted">当前仅显示前 50 条；更多规则请继续扫描补齐索引。</div>' : ''}
      ${virtualChildren()}
      <div class="finding-actions"><button class="btn" type="button" data-mg-action="neuron-open-virtual">聚焦该分支（图内）</button></div>`;
  }

  if (node.virtual_category) {
    const history = node.virtual_category === 'conversation_history';
  const detail = history
        ? (node.requires_agent_selection
        ? '共享组不聚合成员的原始对话。请先显式选择一个 Agent，再查看其独立历史索引。'
        : `仅显示会话元数据索引；已显示 ${childCount} 条${node.has_more ? '，还有更多' : ''}。原文不会进入神经图或长期记忆。`)
      : '规则与习惯只引用既有受治理记忆，不创建第二份持久记录。选择分支或规则节点可直接在图内治理。';
    const label = history && node.requires_agent_selection ? '待选 Agent 后聚焦' : (history ? '查看会话索引（图内）' : '聚焦该分支（图内）');
    return `<div class="popover-kicker">虚拟索引 · 图内查看</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
      <div class="status-item zero"><span class="status-label">索引条目</span><span class="status-num">${node.count || childCount}</span></div>
      <div class="neuron-detail-body">${escapeHtml(detail)}</div>
      ${node.load_error ? `<div class="detail-section"><h4>暂时不可用</h4><div class="neuron-detail-body">${escapeHtml(node.load_error)}</div></div>` : ''}
      ${virtualChildren(history ? '会话索引（点击聚焦）' : '规则分支（点击聚焦）')}
      <div class="finding-actions"><button class="btn" type="button" data-mg-action="neuron-open-virtual">${escapeHtml(label)}</button></div>`;
  }

  const linkRows = (items, chip) => (items || []).map(item => {
    const mid = item.memory_id || '';
    const rel = item.relation_label || chip || '相关';
    return `<div class="raw-file-row" onclick="selectNeuronByMemory('${escapeHtml(mid)}')" title="跳转到关联节点">
      <div><code>${escapeHtml(displayTitle(item) || item.title || mid || '')}</code>
        <div class="surface-meta">${escapeHtml(displayBody(item) || item.body_preview || '')}</div>
        ${item.relation_reason ? `<div class="surface-meta">${escapeHtml(item.relation_reason)}</div>` : ''}
      </div>
      <span class="chip chip-medium">${escapeHtml(rel)}</span>
    </div>`;
  }).join('');

  if (!isAnchor && !isHub) {
    const role = node.node_kind === 'topic' ? '主题树突' : '记忆胞体';
    return `<div class="status-item zero"><span class="status-label">${escapeHtml(role)}</span><span class="status-num">${childCount}</span></div>
      ${node.derivation ? `<div class="detail-section"><h4>衍生路径</h4><div class="detail-path">${escapeHtml(node.derivation)}</div></div>` : ''}
      ${node.edge_hint ? `<div class="row"><span class="key">连接说明</span><span>${escapeHtml(node.edge_hint)}</span></div>` : ''}
      <div class="neuron-detail-body">${escapeHtml(node.body || (node.node_kind === 'topic'
        ? `该主题下有 ${childCount} 个下级节点。点击光点查看具体内容。`
        : `当前投影共有 ${nodes.length} 个节点。`))}</div>
      <div class="rail-link" onclick="switchTab('governance')">进入治理台 →</div>`;
  }

  let members = node.members || [];
  if ((!members || !members.length) && isHub) {
    members = nodes.filter(n => n.parent_id === node.id && n.node_kind === 'claim_anchor').map(n => ({
      memory_id: n.memory_id || '',
      title: displayTitle(n) || n.label || '',
      kind: n.kind || '',
      body_preview: (displayBody(n) || n.body || '').slice(0, 180),
    }));
  }
  const related = node.related || [];
  const actionTarget = node.memory_id || node.id || '';
  const roleKick = isHub ? '同源突触' : '记忆末梢';
  return `<div class="popover-kicker">${escapeHtml(kindText)} · ${roleKick}</div>
    <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(title)}</h3>
    ${node.derivation ? `<div class="detail-section"><h4>衍生路径</h4><div class="detail-path">${escapeHtml(node.derivation)}</div></div>` : ''}
    ${node.edge_hint ? `<div class="row"><span class="key">连接说明</span><span>${escapeHtml(node.edge_hint)}</span></div>` : ''}
    <div class="detail-section"><h4>内容</h4><div class="neuron-detail-body">${escapeHtml(displayBody(node) || node.body || '暂无正文内容')}</div></div>
    <div class="row"><span class="key">作用域</span><span>${escapeHtml(node.scope || 'project')}</span></div>
    <div class="row"><span class="key">置信度</span><span>${escapeHtml(String(node.confidence ?? '—'))}</span></div>
    <div class="row"><span class="key">完整性</span><span>${escapeHtml(node.completeness || '—')}</span></div>
    <div class="row"><span class="key">来源</span><span>${node.provenance_count || 0} 个来源证据</span></div>
    ${node.source_key ? `<div class="row"><span class="key">同源键</span><code style="overflow-wrap:anywhere">${escapeHtml(node.source_key)}</code></div>` : ''}
    ${node.cluster_count ? `<div class="row"><span class="key">合并片段</span><span>${node.cluster_count} 条</span></div>` : ''}
    <div class="row"><span class="key">记录 ID</span><code style="overflow-wrap:anywhere">${escapeHtml(actionTarget)}</code></div>
    ${isAnchor ? `<div class="finding-actions" style="margin:12px 0 10px;display:flex;flex-wrap:wrap;gap:6px">
      <span class="chip chip-confirmed">自动纳入重构</span>
      <button class="btn btn-danger" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','exclude')">删除/排除</button>
      <button class="btn" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','quarantine')">隔离</button>
      <button class="btn" type="button" onclick="neuronAction('${escapeHtml(actionTarget)}','merge')">合并</button>
    </div>` : ''}
    ${members && members.length ? `<div class="claim-list"><h4>${isHub ? '突触末梢（点击跳转）' : '合并片段'}</h4>${linkRows(members, '末梢')}</div>` : ''}
    ${related && related.length ? `<div class="claim-list"><h4>相关连线（点击跳转）</h4>${linkRows(related)}</div>` : ''}`;
}

function renderContent() {
  const renderToken = beginContentRender(state.activeTab);
  pendingContentRenderToken = renderToken;
  try {
    switch (state.activeTab) {
      case 'overview': renderOverview(); break;
      case 'sources': renderSources(); break;
      case 'neurons': renderNeurons(); break;
      case 'codegraph': renderCodeGraph(); break;
      case 'rules': renderRulesHabits(); break;
      case 'history': renderHistory(); break;
      case 'findings': renderFindings(); break;
      case 'token-usage': renderTokenUsage(); break;
      case 'releases': renderReleases(); break;
      case 'governance': renderGovernance(); break;
    }
  } finally {
    pendingContentRenderToken = null;
  }
}

async function ensureGovernanceScope() {
  try {
    const runtime = await callApi('get_governance_scope_state');
    const normalized = normalizeGovernanceScope(runtime);
    governanceScopeState = normalized;
    if (normalized.status === 'audit_only' || normalized.status === 'stale_selection') {
      clearSharedGovernance(normalized.status, normalized.reason);
      activeAgentInstanceId = '';
      return false;
    }
    if (runtime && runtime.ok && runtime.scope && normalized.share_group_id) {
      activeShareGroupId = normalized.share_group_id;
      activeAgentInstanceId = runtime.principal_agent_instance_id || '';
      activeScopeMemberIds = normalized.members;
      dataPageMode = 'multi_agent_shared_mcp';
      return true;
    }
    if (runtime && runtime.ok && runtime.scope && normalized.agent_instance_id) {
      activeAgentInstanceId = normalized.agent_instance_id;
      activeScopeMemberIds = [activeAgentInstanceId];
      activeShareGroupId = '';
      dataPageMode = 'single_agent';
      return true;
    }
    // A single active binding is safe to select automatically; multiple
    // bindings require an explicit user choice.
    const options = (runtime && runtime.options) || {};
    const agents = options.agents || [];
    if (agents.length === 1 && !(options.share_groups || []).length) {
      activeAgentInstanceId = agents[0].agent_instance_id || '';
      activeScopeMemberIds = activeAgentInstanceId ? [activeAgentInstanceId] : [];
      activeShareGroupId = '';
      dataPageMode = 'single_agent';
      return !!activeAgentInstanceId;
    }
  } catch (_) {}
  return false;
}

async function renderNeurons() {
  setContent('<div class="loading">正在读取神经图投影</div>');
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) {
      setContent(`<div class="card empty-state"><div><div class="empty-orb"></div>
        <p>请先在数据源页选择 Agent，或进入多 Agent 共享组并设为治理范围。</p>
        <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="switchTab('sources')">去数据源</button></div>
      </div></div>`);
      return;
    }
    await ensureAgentLabels();
    await refreshProjectionEngines();
    const [scope, agentId, groupId] = scopeApiArgs();
    neuronGraph = await callApi('get_memory_neuron_graph', projectionMode, scope, agentId, groupId);
    renderNeuronGraph();
  }
  catch (e) {
    showToast('神经图构建失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>神经图构建失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function codeGraphNodeLabel(node) {
  if (!node) return '未命名节点';
  return guiPathText(node.label) || guiPathText(node.path) || guiPathText(node.name) || guiPathText(node.signature) || String(node.id || '未命名节点');
}

function codeGraphNodeColor(node) {
  return node && node.node_kind === 'symbol' ? '#142737' : '#12372e';
}

function codeGraphDisplayGraph(graph) {
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const files = new Map(nodes.filter(node => node.node_kind === 'file').map(node => [String(node.id), {...node, visible_symbols: 0}]));
  const owner = new Map();
  nodes.forEach(node => {
    const file = files.get(String(node.file_id || ''));
    owner.set(String(node.id), file ? String(file.id) : String(node.id));
    if (file && node.node_kind === 'symbol') file.visible_symbols++;
  });
  if (codeGraphView === 'symbols') {
    const visible = nodes.filter(node => !codeGraphFocusFile || owner.get(String(node.id)) === codeGraphFocusFile).map(node => files.get(String(node.id)) || node);
    const ids = new Set(visible.map(node => String(node.id)));
    return {...graph, nodes: visible, edges: (graph.edges || []).filter(edge => ids.has(String(edge.from_id || edge.source || edge.from || '')) && ids.has(String(edge.to_id || edge.target || edge.to || '')))};
  }
  const links = new Map();
  (graph.edges || []).forEach(edge => {
    const source = owner.get(String(edge.from_id || edge.source || edge.from || ''));
    const target = owner.get(String(edge.to_id || edge.target || edge.to || ''));
    if (!source || !target || source === target) return;
    const key = JSON.stringify([source, target]);
    const link = links.get(key) || {id: `file-link:${key}`, source, target, relation: '跨文件关系', weight: 0};
    link.weight++;
    links.set(key, link);
  });
  return {...graph, nodes: nodes.filter(node => owner.get(String(node.id)) === String(node.id)).map(node => files.get(String(node.id)) || node), edges: [...links.values()]};
}

function codeGraphCardLabel(node) {
  if (node.node_kind !== 'file') return codeGraphNodeLabel(node).slice(0, 26);
  const path = guiPathText(node.path, codeGraphNodeLabel(node)).replace(/\\/g, '/');
  const parts = path.split('/');
  const name = parts.pop();
  const folder = parts.slice(-2).join('/') || String(node.language || '代码文件').toUpperCase();
  return `${name.length > 27 ? name.slice(0, 24) + '…' : name}\n${folder.length > 30 ? '…' + folder.slice(-28) : folder}\n${Number(node.visible_symbols || 0)} 个符号`;
}

function codeGraphNodePositions(graph) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const files = nodes.filter(node => node.node_kind === 'file').sort((a, b) => String(a.path || a.label || '').localeCompare(String(b.path || b.label || '')));
  const symbolsByFile = new Map();
  nodes.filter(node => node.node_kind === 'symbol').forEach(node => {
    const fileId = String(node.file_id || '');
    if (!symbolsByFile.has(fileId)) symbolsByFile.set(fileId, []);
    symbolsByFile.get(fileId).push(node);
  });
  const positions = {};
  const columns = Math.max(1, Math.min(3, Math.ceil(Math.sqrt(files.length || 1))));
  const gapX = symbolsByFile.size ? 360 : 304;
  let rowY = 0;
  let rowHeight = 0;
  files.forEach((file, index) => {
    const col = index % columns;
    if (index && col === 0) { rowY += rowHeight + 64; rowHeight = 0; }
    const x = (col - (columns - 1) / 2) * gapX;
    const y = rowY;
    positions[file.id] = {x, y};
    const symbols = (symbolsByFile.get(String(file.id)) || []).sort((a, b) => String(a.id || '').localeCompare(String(b.id || '')));
    symbols.forEach((symbol, symbolIndex) => {
      positions[symbol.id] = {x: x + (symbolIndex % 2 ? 82 : -82), y: y + 84 + Math.floor(symbolIndex / 2) * 42};
    });
    rowHeight = Math.max(rowHeight, symbols.length ? 100 + Math.ceil(symbols.length / 2) * 42 : 80);
  });
  let orphanIndex = 0;
  nodes.forEach(node => {
    if (positions[node.id]) return;
    positions[node.id] = {x: (orphanIndex % columns - (columns - 1) / 2) * gapX, y: rowY + rowHeight + 90 + Math.floor(orphanIndex / columns) * 56};
    orphanIndex++;
  });
  return positions;
}

function codeGraphKeyLabelIds(graph, limit = 8) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const degree = new Map();
  (Array.isArray(graph?.edges) ? graph.edges : []).forEach(edge => {
    [edge.from_id || edge.source || edge.from, edge.to_id || edge.target || edge.to].forEach(rawId => {
      const id = String(rawId || '');
      if (id) degree.set(id, (degree.get(id) || 0) + 1);
    });
  });
  return new Set(nodes.filter(node => node.node_kind === 'file').sort((left, right) =>
    (degree.get(String(right.id || '')) || 0) - (degree.get(String(left.id || '')) || 0)
    || codeGraphNodeLabel(left).localeCompare(codeGraphNodeLabel(right))
  ).slice(0, limit).map(node => String(node.id || '')));
}

function codeGraphElements(graph) {
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const positions = codeGraphNodePositions(graph);
  const keyLabelIds = codeGraphKeyLabelIds(graph);
  const nodeIds = new Set(nodes.map(node => String(node.id || '')).filter(Boolean));
  const elements = nodes.filter(node => nodeIds.has(String(node.id || ''))).map(node => ({
    data: {
      id: String(node.id),
      label: codeGraphNodeLabel(node),
      card_label: codeGraphCardLabel(node),
      label_priority: keyLabelIds.has(String(node.id || '')) ? 'true' : 'false',
      node_kind: node.node_kind || 'file',
      color: codeGraphNodeColor(node),
      path: node.path || '',
      language: node.language || '',
      kind: node.kind || '',
      signature: node.signature || '',
      file_id: node.file_id || '',
      line_start: node.line_start || '',
      line_end: node.line_end || '',
      provenance: node.provenance || '',
    },
    position: positions[String(node.id)] || {x: 0, y: 0},
  }));
  const edgeKeys = new Set();
  (Array.isArray(graph?.edges) ? graph.edges : []).forEach(edge => {
    const source = String(edge.from_id || edge.source || edge.from || '');
    const target = String(edge.to_id || edge.target || edge.to || '');
    if (!nodeIds.has(source) || !nodeIds.has(target)) return;
    const relation = String(edge.relation || edge.etype || 'related');
    const key = `${source}|${target}|${relation}`;
    if (edgeKeys.has(key)) return;
    edgeKeys.add(key);
    elements.push({data: {
      id: String(edge.id || `codegraph:${key}`),
      source,
      target,
      relation,
      label: relation,
    }});
  });
  return elements;
}

function updateCodeGraphLabelPolicy() {
  if (!codeCyInstance) return;
  const nodes = codeCyInstance.nodes('[node_kind = "symbol"]');
  if (codeCyInstance.zoom() >= .65) nodes.addClass('codegraph-label-zoomed');
  else nodes.removeClass('codegraph-label-zoomed');
  const readout = document.getElementById('codegraph-zoom');
  if (readout) readout.textContent = `${Math.round(codeCyInstance.zoom() * 100)}%`;
}

function setCodeGraphView(value) {
  codeGraphView = value === 'symbols' ? 'symbols' : 'files';
  if (codeGraphView === 'symbols') codeGraphFocusFile = String(selectedCodeGraphNode?.node_kind === 'file' ? selectedCodeGraphNode.id : (codeGraph?.nodes || []).find(node => node.node_kind === 'file')?.id || '');
  renderCodeGraphView();
}

function setCodeGraphFocus(value) {
  codeGraphFocusFile = String(value || '');
  renderCodeGraphView();
}

function zoomCodeGraph(factor) {
  if (!codeCyInstance) return;
  codeCyInstance.zoom({level: Math.max(codeCyInstance.minZoom(), Math.min(codeCyInstance.maxZoom(), codeCyInstance.zoom() * factor)), renderedPosition: {x: codeCyInstance.width() / 2, y: codeCyInstance.height() / 2}});
}

function codeGraphNodeForId(nodeId) {
  const nodes = Array.isArray(codeGraph?.nodes) ? codeGraph.nodes : [];
  return nodes.find(node => String(node.id || '') === String(nodeId || '')) || null;
}

function codeGraphAutomationState(graph = {}) {
  const automation = selectedCodeGraphProjectRow()?.automation || graph.codegraph_status?.automation;
  if (automation) {
    if (automation.policy_status !== 'ok') return {label: '自动建图设置不可用', tone: 'medium', detail: automation.error || '请检查设置文件权限与格式'};
    if (!automation.enabled) return {label: '自动建图已关闭', tone: 'info', detail: '保留已有图谱；停止后续自动建图与增量刷新'};
    const labels = {not_built: '自动建图待启动', queued: '自动建图已排队', building: '自动建图中', ready: '图谱已就绪 · 自动增量开启', no_source: '当前项目没有可索引代码', failed: '自动建图失败', blocked: '自动建图受阻'};
    const errors = {codegraph_source_file_limit: '代码文件超过 10,000 个，请选择更具体的项目目录', codegraph_worker_interrupted: '后台建图已中断，可点击重试'};
    return {label: labels[automation.build_status] || '自动建图待确认', tone: automation.build_status === 'ready' ? 'confirmed' : 'info', detail: errors[automation.error] || automation.error || '项目进入 Agent 可信工作目录后首次建图，写入代码后增量更新'};
  }
  const source = graph && typeof graph === 'object' ? graph : {};
  const incremental = source.codegraph_status && typeof source.codegraph_status === 'object'
    ? source.codegraph_status.incremental : null;
  if (incremental && typeof incremental === 'object') {
    const supported = incremental.supported === true;
    const builtScope = incremental.built_scope === true;
    const activeBinding = incremental.active_binding === true;
    const queueDepth = Number(incremental.queue_depth || 0);
    if (incremental.enabled === true && supported && builtScope && activeBinding) {
      return {label: '自动增量已启用', tone: 'confirmed', detail: `已绑定当前范围；${queueDepth ? `待刷新 ${queueDepth} 项` : '当前无待刷新项'}`};
    }
    if (!supported) return {label: '自动增量不可用', tone: 'medium', detail: '后端未声明增量刷新能力'};
    if (!builtScope) return {label: '自动增量待建图', tone: 'info', detail: '当前范围尚无已构建的 CodeGraph'};
    if (!activeBinding) return {label: '自动增量待绑定', tone: 'info', detail: '当前范围没有活动 Agent 绑定'};
    return {label: '自动增量状态未确认', tone: 'info', detail: '后端未返回启用状态'};
  }
  const status = String(source.watch_status || source.automation_status || source.incremental_status || '').trim();
  const statusKey = status.toLowerCase();
  const capability = source.automatic === true || source.auto_refresh === true
    || source.incremental === true || source.watch_supported === true || source.automation_available === true;
  const running = statusKey === 'active' || statusKey === 'running' || statusKey === 'watching';
  const unavailable = statusKey === 'unavailable' || statusKey === 'error' || statusKey === 'failed'
    || source.available === false || source.ok === false;
  if (unavailable) return {label: '自动更新不可用', tone: 'medium', detail: status || '后端未提供自动更新能力'};
  if (running) return {label: '自动增量更新运行中', tone: 'confirmed', detail: status};
  if (capability) return {label: '自动更新待后端确认', tone: 'info', detail: status || '后端声明支持自动更新，但尚未返回运行状态。当前页面不伪造运行状态'};
  if (status) return {label: '自动更新未启用', tone: 'medium', detail: status};
  return {label: '自动更新状态待后端确认', tone: 'info', detail: '当前页面不伪造运行状态；可手动刷新或构建'};
}

function selectedCodeGraphProjectRow() {
  if (!selectedCodeGraphProject) return null;
  return codeGraphProjects.find(item => item.source_id === selectedCodeGraphProject
    || item.project_key === selectedCodeGraphProject || item.project_ref === selectedCodeGraphProject) || null;
}

function codeGraphProjectControls() {
  const options = codeGraphProjects.map(item => {
    const value = item.source_id || item.project_key || item.project_ref || '';
    const selected = value === selectedCodeGraphProject ? 'selected' : '';
    const counts = `${Number(item.file_count || 0)} 文件 · ${Number(item.symbol_count || 0)} 符号`;
    return `<option value="${escapeHtml(value)}" ${selected}>${escapeHtml(item.label || item.project_ref || value)} · ${counts}</option>`;
  }).join('');
  const select = codeGraphProjects.length
    ? `<label>项目<select aria-label="CodeGraph 项目" onchange="setCodeGraphProject(this.value)"><option value="" ${selectedCodeGraphProject ? '' : 'selected'}>${codeGraphProjects.length > 1 ? '选择项目' : '自动选择'}</option>${options}</select></label>`
    : '<span class="muted">尚未构建任何项目 CodeGraph</span>';
  const buildLabel = codeGraphBuildInFlight ? '正在构建…' : (codeGraphProjects.length ? '构建 / 更新项目' : '选择项目并构建');
  const project = selectedCodeGraphProjectRow();
  const disabled = codeGraphAutomationSaving || codeGraphAutomation.policy_status !== 'ok';
  const controls = `<label title="默认开启；新项目进入 Agent 工作目录后自动建图。项目单独设置优先。"><input type="checkbox" role="switch" aria-label="新项目默认自动建图" ${codeGraphAutomation.default_enabled ? 'checked' : ''} ${disabled ? 'disabled' : ''} onchange="setCodeGraphAutomation(this.checked, true)">新项目默认自动建图</label>
    ${project ? `<label title="开启后首次建图，随后自动增量更新；关闭保留已有图谱。"><input type="checkbox" role="switch" aria-label="当前项目自动建图" ${project.automation?.enabled ? 'checked' : ''} ${disabled ? 'disabled' : ''} onchange="setCodeGraphAutomation(this.checked, false)">当前项目自动建图</label>` : ''}
    ${project && ['failed', 'blocked', 'not_built', 'no_source'].includes(project.automation?.build_status) && project.automation?.enabled ? '<button class="btn" type="button" onclick="setCodeGraphAutomation(true, false)">启动 / 重试自动建图</button>' : ''}`;
  return `${select}${controls}<button class="btn ${codeGraphProjects.length ? '' : 'btn-primary'}" type="button" onclick="buildCodeGraphFromFolder()" ${codeGraphBuildInFlight || !codeGraphBuildReady ? 'disabled' : ''}>${buildLabel}</button>`;
}

async function setCodeGraphAutomation(enabled, defaultPolicy) {
  if (codeGraphAutomationSaving) return;
  const project = selectedCodeGraphProjectRow();
  if (!defaultPolicy && !project) return;
  codeGraphAutomationSaving = true;
  renderCodeGraphView();
  try {
    const result = await callApi('set_codegraph_automation', {enabled, default_policy: defaultPolicy, codegraph_project_ref: project?.project_ref || ''});
    if (result.error || result.ok === false) throw new Error(apiErrorMessage(result, '自动建图设置失败'));
    await refreshCodeGraph(enabled ? '自动建图已开启' : '自动建图已关闭，已有图谱保留');
  } catch (error) { showToast(error.message || String(error), 'error'); }
  finally { codeGraphAutomationSaving = false; renderCodeGraphView(); }
}

async function loadCodeGraphProjects() {
  const result = await callApi('list_codegraph_projects');
  if (result.error || result.ok === false) throw new Error(apiErrorMessage(result, 'CodeGraph 项目列表读取失败'));
  codeGraphProjects = Array.isArray(result.projects) ? result.projects : [];
  codeGraphBuildReady = result.build_ready === true;
  codeGraphAutomation = result.automation || {};
  if (selectedCodeGraphProject && !selectedCodeGraphProjectRow()) selectedCodeGraphProject = '';
  if (!selectedCodeGraphProject && codeGraphProjects.length === 1) {
    selectedCodeGraphProject = codeGraphProjects[0].source_id || codeGraphProjects[0].project_key || codeGraphProjects[0].project_ref || '';
    if (selectedCodeGraphProject) localStorage.setItem('memoryguard.codeGraphProject', selectedCodeGraphProject);
  }
  return result;
}

function renderCodeGraphRail() {
  const graph = codeGraph || {};
  if (selectedCodeGraphNode) {
    const node = selectedCodeGraphNode;
    return `<div class="popover-kicker">CodeGraph 节点</div>
      <h3 style="margin:4px 0 10px;font-size:15px">${escapeHtml(codeGraphNodeLabel(node))}</h3>
      <div class="row"><span class="key">类型</span><span>${escapeHtml(node.node_kind || 'file')}</span></div>
      ${node.path ? `<div class="row"><span class="key">路径</span><code>${escapeHtml(guiPathText(node.path))}</code></div>` : ''}
      ${node.kind ? `<div class="row"><span class="key">符号</span><span>${escapeHtml(node.kind)}</span></div>` : ''}
      ${node.signature ? `<div class="detail-section"><h4>签名</h4><div class="neuron-detail-body">${escapeHtml(node.signature)}</div></div>` : ''}
      ${node.line_start ? `<div class="row"><span class="key">行号</span><span>${escapeHtml(node.line_start)}${node.line_end ? `–${escapeHtml(node.line_end)}` : ''}</span></div>` : ''}
      ${node.provenance ? `<div class="row"><span class="key">来源</span><span>${escapeHtml(node.provenance)}</span></div>` : ''}`;
  }
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  return `<div class="status-item"><span class="status-label">节点</span><span class="status-num">${graph.node_count ?? nodes.length}</span></div>
    <div class="status-item"><span class="status-label">关系</span><span class="status-num">${graph.edge_count ?? edges.length}</span></div>
    <div class="status-item"><span class="status-label">项目</span><span class="status-num" style="font-size:11px">${escapeHtml(selectedCodeGraphProjectRow()?.label || graph.project_ref || '未选择')}</span></div>
    <div class="rail-link" onclick="refreshCodeGraph()">刷新 CodeGraph</div>`;
}

function renderCodeGraphEmpty(message = '当前范围没有 CodeGraph 数据。') {
  setContent(`<div class="view-heading"><span class="eyebrow">CodeGraph</span><h2>CodeGraph</h2>
    <p>代码结构图独立于记忆核心。Graphify 只写入文件/符号/关系元数据，不会把代码正文塞进长期记忆。</p></div>
    <section class="card empty-state"><div><div class="empty-orb"></div><p>${escapeHtml(message)}</p>
      <div class="codegraph-controls" style="justify-content:center;margin-top:14px">${codeGraphProjectControls()}<button class="btn" type="button" onclick="refreshCodeGraph()">刷新 CodeGraph</button></div>
      <p class="muted" style="margin-top:10px">${escapeHtml(codeGraphAutomationState(codeGraph || {}).label)} · ${escapeHtml(codeGraphAutomationState(codeGraph || {}).detail)}</p>
      ${!codeGraphBuildReady ? '<p class="muted" style="margin-top:10px">内置 Graphify Core 当前不可用，请运行 MemoryGuard 诊断/修复；无需安装外部 Graphify。</p>' : ''}</div></section>`);
  renderStatusRail();
}

function renderCodeGraphView() {
  const graph = codeGraph || {};
  const nodes = Array.isArray(graph.nodes) ? graph.nodes : [];
  const edges = Array.isArray(graph.edges) ? graph.edges : [];
  const count = Number(graph.node_count ?? nodes.length);
  const edgeCount = Number(graph.edge_count ?? edges.length);
  const totalCounts = graph.total_counts || {};
  const countEl = document.getElementById('codegraph-count');
  if (countEl) countEl.textContent = count || '';
  if (graph.error || graph.ok === false) {
    renderCodeGraphEmpty(`CodeGraph 读取失败：${apiErrorMessage(graph, 'CodeGraph 读取失败')}`);
    return;
  }
  if (!nodes.length) {
    const emptyMessage = graph.status === 'PROJECT_REQUIRED'
      ? '当前共享组有多个 CodeGraph 项目，请选择一个项目。'
      : (graph.status === 'NO_SOURCE' ? (selectedCodeGraphProjectRow() ? '当前项目尚未建立 CodeGraph 图谱。' : '当前共享组尚未构建任何 CodeGraph 项目。') : '当前项目没有 CodeGraph 节点。');
    renderCodeGraphEmpty(emptyMessage);
    return;
  }
  selectedCodeGraphNode = null;
  if (codeGraphFocusFile && !nodes.some(node => String(node.id) === codeGraphFocusFile)) codeGraphFocusFile = String(nodes.find(node => node.node_kind === 'file')?.id || '');
  const displayGraph = codeGraphDisplayGraph(graph);
  const formatCount = value => Number(value || 0).toLocaleString('zh-CN');
  setContent(`<div class="dashboard-view codegraph-view"><div class="dashboard-main">
    <div class="codegraph-heading"><div><h2>代码结构</h2><p>从文件看全局，沿连接查看代码关系。</p></div><div class="codegraph-view-switch" role="group" aria-label="代码图视图"><button type="button" aria-pressed="${codeGraphView === 'files'}" onclick="setCodeGraphView('files')">文件总览</button><button type="button" aria-pressed="${codeGraphView === 'symbols'}" onclick="setCodeGraphView('symbols')">符号展开</button></div></div>
    <div class="codegraph-settings">
      ${codeGraphProjectControls()}
      ${codeGraphView === 'symbols' ? `<label>当前文件<select aria-label="符号所属文件" onchange="setCodeGraphFocus(this.value)"><option value="" ${!codeGraphFocusFile ? 'selected' : ''}>全部文件</option>${nodes.filter(node => node.node_kind === 'file').map(node => `<option value="${escapeHtml(node.id)}" ${String(node.id) === codeGraphFocusFile ? 'selected' : ''}>${escapeHtml(codeGraphNodeLabel(node))}</option>`).join('')}</select></label>` : ''}
      ${selectedCodeGraphProjectRow()?.branch || graph.branch ? `<span class="muted">分支 ${escapeHtml(selectedCodeGraphProjectRow()?.branch || graph.branch)}</span>` : ''}
      <label>节点上限<select aria-label="CodeGraph 节点上限" onchange="setCodeGraphLimit(this.value)">${[50, 100, 200, 500].map(limit => `<option value="${limit}" ${limit === codeGraphLimit ? 'selected' : ''}>${limit}</option>`).join('')}</select></label>
      <span class="chip chip-${codeGraphAutomationState(graph).tone}" title="${escapeHtml(codeGraphAutomationState(graph).detail)}">${escapeHtml(codeGraphAutomationState(graph).label)}</span>
      <button class="btn" type="button" onclick="refreshCodeGraph()">刷新</button>
    </div>
    <div class="codegraph-summary"><span><strong>${formatCount(count)}</strong>当前节点</span><span><strong>${formatCount(edgeCount)}</strong>当前关系</span><span><strong>${formatCount(totalCounts.symbols || graph.displayed_symbol_count)}</strong>项目符号</span><span><strong>${formatCount(totalCounts.edges || edgeCount)}</strong>项目关系</span></div>
    <section class="codegraph-stage" aria-label="CodeGraph 代码结构图"><div class="codegraph-stage-head"><strong>${codeGraphView === 'files' ? '文件关系总览' : '文件与符号'}</strong>${codeGraphView === 'files' ? '同文件符号合并显示 · 仅展示当前范围的跨文件关系' : '符号按所属文件排列 · 放大查看名称'}</div><div class="codegraph-canvas" id="codegraph-canvas"></div><div class="codegraph-legend"><span><i class="codegraph-dot"></i>文件</span>${codeGraphView === 'symbols' ? '<span><i class="codegraph-dot symbol"></i>符号</span>' : ''}<span>点击聚焦 · 空白处取消</span></div><div class="codegraph-zoom" role="group" aria-label="图谱缩放"><button type="button" aria-label="缩小代码图" onclick="zoomCodeGraph(.8)">−</button><output id="codegraph-zoom" aria-label="缩放比例">100%</output><button type="button" aria-label="放大代码图" onclick="zoomCodeGraph(1.25)">+</button><button type="button" aria-label="适应画布" title="适应画布" onclick="fitCodeGraph()">⌗</button></div></section>
  </div></div>`);
  renderStatusRail();
  if (typeof cytoscape === 'undefined') {
    document.getElementById('codegraph-canvas').innerHTML = '<div class="empty-state" style="color:var(--red)">本地 Cytoscape 资源加载失败</div>';
    return;
  }
  if (codeCyInstance) {
    try { codeCyInstance.destroy(); } catch (_) { /* graph torn down */ }
  }
  codeCyInstance = cytoscape({
    container: document.getElementById('codegraph-canvas'),
    elements: codeGraphElements(displayGraph),
    style: [
      { selector: 'node', style: {
        'shape': 'roundrectangle', 'width': 146, 'height': 28, 'background-color': 'data(color)',
        'border-width': 1, 'border-color': '#436078', 'border-opacity': .8,
        'label': '', 'color': '#b7cad8', 'font-size': 11,
        'font-family': 'Segoe UI, Microsoft YaHei, sans-serif', 'text-valign': 'center',
        'text-halign': 'center', 'text-wrap': 'ellipsis', 'text-max-width': 128,
        'overlay-opacity': 0,
      }},
      { selector: 'node[label_priority = "true"], node:selected, node.codegraph-label-hover, node.codegraph-label-zoomed', style: {'label': 'data(card_label)'} },
      { selector: 'node[node_kind = "file"]', style: {'width': 256, 'height': 86, 'border-color': '#487a64', 'border-width': 1.3, 'label': 'data(card_label)', 'text-wrap': 'wrap', 'text-max-width': 244, 'font-size': 16, 'line-height': 1.4, 'font-weight': 500, 'color': '#d0eadc'} },
      { selector: 'edge', style: {
        'width': 1.2, 'line-color': '#588876', 'opacity': .32,
        'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'target-arrow-color': '#588876', 'arrow-scale': .7,
      }},
      { selector: '.codegraph-muted', style: {'opacity': .12} },
      { selector: 'edge:selected, edge.codegraph-focused', style: {'line-color': '#b5dcc6', 'target-arrow-color': '#b5dcc6', 'width': 2, 'opacity': .9} },
      { selector: 'node:selected', style: {'border-color': '#c9e4ae', 'border-width': 2.5, 'background-color': '#254936'} },
    ],
    layout: {name: 'preset', fit: true, padding: 64},
    minZoom: .05, maxZoom: 1.6, wheelSensitivity: .2,
  });
  codeCyInstance.on('tap', 'node', event => {
    selectedCodeGraphNode = codeGraphNodeForId(event.target.id());
    const neighborhood = event.target.closedNeighborhood();
    codeCyInstance.elements().removeClass('codegraph-muted codegraph-focused');
    codeCyInstance.elements().not(neighborhood).addClass('codegraph-muted');
    neighborhood.edges().addClass('codegraph-focused');
    renderStatusRail();
  });
  codeCyInstance.on('mouseover', 'node', event => event.target.addClass('codegraph-label-hover'));
  codeCyInstance.on('mouseout', 'node', event => event.target.removeClass('codegraph-label-hover'));
  codeCyInstance.on('zoom', updateCodeGraphLabelPolicy);
  updateCodeGraphLabelPolicy();
  codeCyInstance.on('tap', event => {
    if (event.target === codeCyInstance) {
      selectedCodeGraphNode = null;
      codeCyInstance.elements().removeClass('codegraph-muted codegraph-focused');
      renderStatusRail();
    }
  });
}

async function renderCodeGraph() {
  setContent('<div class="loading">正在读取 CodeGraph</div>');
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) {
      renderCodeGraphEmpty('请先选择一个受治理的 Agent / 共享组。');
      return;
    }
    await ensureAgentLabels();
    await loadCodeGraphProjects();
    await refreshCodeGraph();
  } catch (error) {
    codeGraph = {error: error.message || String(error)};
    showToast('CodeGraph 读取失败：' + (error.message || error), 'error');
    renderCodeGraphView();
  }
}

async function refreshCodeGraph(message = '') {
  clearTimeout(codeGraphAutomationTimer);
  await loadCodeGraphProjects();
  const selected = selectedCodeGraphProjectRow();
  const request = {
    codegraph_source_id: selected?.source_id || '',
    codegraph_project_ref: selected?.source_id ? '' : (selected?.project_ref || selectedCodeGraphProject || ''),
    limit: codeGraphLimit,
    provenance: codeGraphProvenance,
  };
  const [graph, codegraphStatus] = await Promise.all([
    callApi('get_codegraph_graph', request),
    callApi('codegraph_status', request),
  ]);
  codeGraph = {...graph, codegraph_status: codegraphStatus};
  if (Array.isArray(codeGraph.projects) && !codeGraphProjects.length) codeGraphProjects = codeGraph.projects;
  renderCodeGraphView();
  if (codeGraphProjects.some(item => ['queued', 'building'].includes(item.automation?.build_status))) {
    codeGraphAutomationTimer = setTimeout(() => {
      if (state.activeTab === 'codegraph') refreshCodeGraph().catch(error => showToast(error.message, 'error'));
    }, 2000);
  }
  if (message) showToast(message, 'success');
}

async function setCodeGraphProject(value) {
  selectedCodeGraphProject = String(value || '');
  if (selectedCodeGraphProject) localStorage.setItem('memoryguard.codeGraphProject', selectedCodeGraphProject);
  else localStorage.removeItem('memoryguard.codeGraphProject');
  try { await refreshCodeGraph(); }
  catch (error) { showToast('CodeGraph 项目切换失败：' + (error.message || error), 'error'); }
}

async function buildCodeGraphFromFolder() {
  if (codeGraphBuildInFlight) return;
  const picked = await callApi('pick_path');
  const projectPath = String(picked?.path || '').trim();
  if (!projectPath || picked?.is_directory === false) return;
  if (!confirm(`确认使用 Graphify 构建该项目的 CodeGraph？\n\n${projectPath}\n\n只写入结构元数据，不复制代码正文到长期记忆。`)) return;
  codeGraphBuildInFlight = true;
  renderCodeGraphView();
  try {
    const added = await callApi('add_source', projectPath, 'directory', '', true);
    if (added.error || added.ok === false) throw new Error(apiErrorMessage(added, '代码来源授权失败'));
    const sourceId = String(added.source_id || added.root_id || '');
    if (!sourceId) throw new Error('代码来源未返回 source_id');
    selectedCodeGraphProject = sourceId;
    localStorage.setItem('memoryguard.codeGraphProject', sourceId);
    const accepted = await callApi('build_codegraph', sourceId, true);
    if (accepted.error || accepted.ok === false) throw new Error(apiErrorMessage(accepted, 'CodeGraph 构建启动失败'));
    const task = normalizeTaskState(accepted);
    const runId = task.run_id || accepted.job_id || '';
    const result = (accepted.deferred || runId)
      ? await waitForTask(runId, 'CodeGraph 构建', 300000)
      : accepted;
    if (!result || result.ok === false || result.execution_status === 'failed') {
      throw new Error(apiErrorMessage(result || {}, 'CodeGraph 构建失败'));
    }
    const projectRef = String(result.project_ref || accepted.project_ref || '');
    const builtSourceId = String(result.source_id || accepted.source_id || selectedCodeGraphProject || '');
    await loadCodeGraphProjects();
    const match = codeGraphProjects.find(item => item.source_id === builtSourceId
      || item.project_key === projectRef || item.project_ref === projectRef);
    selectedCodeGraphProject = match?.source_id || match?.project_key || match?.project_ref || builtSourceId || projectRef;
    if (selectedCodeGraphProject) localStorage.setItem('memoryguard.codeGraphProject', selectedCodeGraphProject);
    const counts = result.counts || {};
    await refreshCodeGraph(`CodeGraph 已更新：${Number(counts.source_files || 0)} 文件 · ${Number(counts.symbols || 0)} 符号 · ${Number(counts.edges || 0)} 关系`);
  } catch (error) {
    showToast('CodeGraph 构建失败：' + (error.message || error), 'error');
    try { await loadCodeGraphProjects(); await refreshCodeGraph(); } catch (_) { /* keep original error */ }
  } finally {
    codeGraphBuildInFlight = false;
    if (state.activeTab === 'codegraph') renderCodeGraphView();
  }
}

async function setCodeGraphLimit(value) {
  const next = Number(value);
  if (![50, 100, 200, 500].includes(next)) return;
  codeGraphLimit = next;
  try { await refreshCodeGraph(); }
  catch (error) { showToast('CodeGraph 刷新失败：' + (error.message || error), 'error'); }
}

function fitCodeGraph() {
  if (!codeCyInstance) return;
  codeCyInstance.elements().removeClass('codegraph-muted codegraph-focused');
  codeCyInstance.fit(undefined, 64);
  updateCodeGraphLabelPolicy();
}

function kindColor(kind) {
  const colors = {
    fact: '#6ee7c4', preference: '#f6ad55', project: '#63b3ed', episode: '#fc8181', procedure: '#b794f4', correction: '#f687b3', workflow: '#b794f4', constraint: '#fbd38d',
    user: '#c084fc', agent: '#38bdf8', session: '#94a3b8', share_group: '#2dd4bf', unknown: '#64748b',
    rules_habits: '#f6ad55', mandatory: '#fb7185', preferences: '#f6ad55', procedures: '#b794f4', corrections: '#f687b3', projects: '#63b3ed',
    conversation_history: '#7dd3fc'
  };
  return colors[kind] || '#6ee7c4';
}

function memoryKindLabel(kind) {
  const labels = {
    fact: '事实', preference: '偏好', project: '项目', episode: '事件', procedure: '流程', correction: '纠错',
    constraint: '约束', workflow: '流程', decision: '决策', context: '上下文', instruction: '指令', unknown: '未知',
    user: '用户来源', agent: 'Agent 来源', session: '会话来源', share_group: '共享项目'
  };
  return labels[kind] || kind || '未知';
}

function topicNodeLabel(node) {
  if (!node) return '主题';
  if (node.label && (String(node.label).includes('来源') || /[\u4e00-\u9fff]/.test(String(node.label)))) {
    return String(node.label);
  }
  return memoryKindLabel(node.kind || node.label || '');
}

function neuronHashUnit(value) {
  let hash = 2166136261;
  const text = String(value || '');
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 10000) / 10000;
}

function neuronNodePositions(nodes) {
  const positions = { main: { x: 0, y: 0 } };
  const children = {};
  const byId = new Map();
  nodes.forEach(node => {
    byId.set(String(node.id || ''), node);
    const parent = String(node.parent_id || '');
    if (!children[parent]) children[parent] = [];
    children[parent].push(node);
  });
  Object.values(children).forEach(items => items.sort((a, b) => String(a.id || '').localeCompare(String(b.id || ''))));

  const descendantCount = (nodeId, memo = new Map()) => {
    if (memo.has(nodeId)) return memo.get(nodeId);
    let total = 0;
    for (const child of children[nodeId] || []) total += 1 + descendantCount(String(child.id || ''), memo);
    memo.set(nodeId, total);
    return total;
  };
  const branchRank = (node) => {
    if (node.id === 'virtual-rules-habits') return 0;
    if (node.id === 'virtual-conversation-history') return 1;
    if (node.kind === 'user') return 2;
    if (node.kind === 'project') return 3;
    if (node.kind === 'shared') return 4;
    return 10;
  };
  const directMain = [...(children.main || [])].sort((a, b) => branchRank(a) - branchRank(b) || String(a.id || '').localeCompare(String(b.id || '')));
  const weights = directMain.map(node => Math.max(1.2, Math.sqrt(1 + descendantCount(String(node.id || ''))) * .72));
  const totalWeight = Math.max(1, weights.reduce((sum, item) => sum + item, 0));
  const branchMeta = new Map();
  let cursor = -Math.PI / 2;
  directMain.forEach((node, index) => {
    const sector = Math.PI * 2 * (weights[index] / totalWeight);
    const angle = cursor + sector / 2;
    const radius = 118 + Math.min(18, descendantCount(String(node.id || '')) * .45);
    positions[node.id] = { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
    branchMeta.set(String(node.id || ''), { angle, sector: Math.max(.42, sector) });
    cursor += sector;
  });

  const topBranch = (nodeId) => {
    let current = byId.get(String(nodeId || ''));
    let guard = 0;
    while (current && current.parent_id && current.parent_id !== 'main' && guard++ < 12) {
      current = byId.get(String(current.parent_id));
    }
    return current && current.parent_id === 'main' ? String(current.id || '') : String(nodeId || '');
  };
  const depthOf = (nodeId) => {
    let depth = 0;
    let current = byId.get(String(nodeId || ''));
    let guard = 0;
    while (current && current.parent_id && guard++ < 12) {
      depth += 1;
      if (current.parent_id === 'main') break;
      current = byId.get(String(current.parent_id));
    }
    return depth;
  };
  const pending = nodes.filter(node => node.id !== 'main' && !positions[node.id]);
  for (let pass = 0; pass < 12 && pending.length; pass++) {
    for (let i = pending.length - 1; i >= 0; i--) {
      const node = pending[i];
      const parentId = String(node.parent_id || 'main');
      const parent = positions[parentId];
      if (!parent) continue;
      const siblings = children[parentId] || [node];
      const index = Math.max(0, siblings.findIndex(item => item.id === node.id));
      const siblingCount = Math.max(1, siblings.length);
      const branchId = topBranch(node.id);
      const meta = branchMeta.get(branchId) || {
        angle: Math.atan2(parent.y, parent.x) || -Math.PI / 2,
        sector: Math.PI / Math.max(2, directMain.length || 2),
      };
      const parentAngle = Math.atan2(parent.y, parent.x);
      const depth = Math.max(2, depthOf(node.id));
      const perRing = Math.max(6, Math.min(14, Math.ceil(Math.sqrt(siblingCount) * 2.2)));
      const ring = Math.floor(index / perRing);
      const ringIndex = index % perRing;
      const ringCount = Math.max(1, Math.min(perRing, siblingCount - ring * perRing));
      const fan = Math.min(1.32, meta.sector * .76);
      const offset = ringCount === 1 ? 0 : ((ringIndex + .5) / ringCount - .5) * fan;
      const angle = parentAngle + offset + (neuronHashUnit(node.id) - .5) * .035;
      const parentRadius = Math.sqrt(parent.x * parent.x + parent.y * parent.y);
      const radialStep = node.node_kind === 'history_session' ? 82
        : node.node_kind === 'virtual_rule_ref' ? 78 : 96;
      const radius = parentRadius + radialStep + ring * 58 + Math.min(30, depth * 5);
      positions[node.id] = { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
      pending.splice(i, 1);
    }
  }
  pending.forEach((node, index) => {
    const angle = neuronHashUnit(node.id) * Math.PI * 2;
    const radius = 420 + index * 14;
    positions[node.id] = { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
  });

  // Stronger collision pass.  It only adjusts visualization coordinates and
  // keeps the semantic parent/edge graph untouched.
  const estimateRadius = (item) => {
    const kind = item.node_kind || '';
    if (kind === 'root') return 36;
    if (kind === 'source_hub') return 30;
    if (kind === 'virtual_category') return 44;
    if (kind === 'virtual_bucket') return 38;
    if (kind === 'history_project') return 38;
    if (kind === 'history_agent') return 30;
    if (kind === 'topic') return 32;
    if (kind === 'history_session') return 11;
    if (kind === 'virtual_rule_ref') return 12;
    if (kind === 'claim_anchor' || kind === 'duplicate_cluster') return 12;
    return 20 + Math.min(18, (item.provenance_count || 0) * 2.2);
  };
  const radii = Object.fromEntries(nodes.map(node => [node.id, estimateRadius(node)]));
  const anchored = new Set(directMain.map(node => String(node.id || '')));
  const ids = nodes.map(item => item.id).filter(id => positions[id] && id !== 'main');
  const branchById = Object.fromEntries(ids.map(id => [id, topBranch(id)]));
  for (let iter = 0; iter < 78; iter++) {
    let moved = false;
    for (let i = 0; i < ids.length; i++) {
      const posA = positions[ids[i]];
      for (let j = i + 1; j < ids.length; j++) {
        const posB = positions[ids[j]];
        let dx = posB.x - posA.x;
        let dy = posB.y - posA.y;
        let dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < .001) {
          const splitAngle = neuronHashUnit(`${ids[i]}:${ids[j]}`) * Math.PI * 2;
          dx = Math.cos(splitAngle);
          dy = Math.sin(splitAngle);
          dist = 1;
        }
        const sameBranch = branchById[ids[i]] === branchById[ids[j]];
        const clusterPadding = sameBranch ? 10 : 26;
        const required = (radii[ids[i]] || 18) + (radii[ids[j]] || 18) + clusterPadding;
        if (dist >= required) continue;
        const force = (required - dist) * .46 * (1 - iter / 92);
        const nx = dx / dist;
        const ny = dy / dist;
        const anchorA = anchored.has(ids[i]);
        const anchorB = anchored.has(ids[j]);
        if (anchorA && anchorB) continue;
        if (anchorA) {
          posB.x += nx * force * 1.7;
          posB.y += ny * force * 1.7;
        } else if (anchorB) {
          posA.x -= nx * force * 1.7;
          posA.y -= ny * force * 1.7;
        } else {
          posA.x -= nx * force;
          posA.y -= ny * force;
          posB.x += nx * force;
          posB.y += ny * force;
        }
        moved = true;
      }
    }
    if (!moved) break;
  }
  return positions;
}

function graphElements(graph) {
  // v3.1 §6.3：统一 v3 图契约
  // node: id / parent_id / label / node_kind / memory_id / kind / provenance_count
  // edge: id / source / target / edge_type (+ strength 粗细)
  const elements = [];
  const graphNodes = graph.nodes || [];
  const nodeById = new Map(graphNodes.map(node => [String(node.id || ''), node]));
  const positions = neuronNodePositions(graphNodes);
  const nodeDepth = (node) => {
    let depth = 0;
    let current = node;
    let guard = 0;
    while (current && current.parent_id && guard++ < 16) {
      depth += 1;
      current = nodeById.get(String(current.parent_id));
    }
    return depth;
  };
  const EDGE_STRENGTH = {
    derived_from: 0.58, related: 0.28, shared_source: 0.4,
    duplicate: 0.34, virtual_index: 0.46,
  };
  for (const node of graphNodes) {
    const root = node.node_kind === 'root';
    const hub = node.node_kind === 'source_hub';
    const anchor = node.node_kind === 'claim_anchor' || node.node_kind === 'duplicate_cluster';
    const cluster = node.node_kind === 'duplicate_cluster';
    const virtualCategory = node.node_kind === 'virtual_category';
    const historySession = node.node_kind === 'history_session';
    // v3：用 provenance_count 替代旧 claim_count 决定大小
    const provCount = node.provenance_count || 0;
    const size = root ? 66
      : hub ? Math.max(22, Math.min(40, 18 + (node.cluster_count || provCount || 2) * 3.5))
      : virtualCategory ? Math.max(34, Math.min(50, 31 + (node.count || 0) * .22))
      : historySession ? 12
      : cluster ? Math.max(15, Math.min(30, 12 + (node.cluster_count || 2) * 4))
      : anchor ? 7
      : Math.max(27, Math.min(54, 25 + provCount * 3.2));
    const compactLeaf = anchor || historySession || node.node_kind === 'virtual_rule_ref';
    elements.push({ data: {
      id: node.id,
      label: compactLeaf ? '' : String(node.node_kind === 'topic' ? topicNodeLabel(node) : (node.label || '')).slice(0, 18),
      kind: node.node_kind,
      parent_id: node.parent_id || '',
      depth: nodeDepth(node),
      memory_id: node.memory_id || '',
      record_kind: node.record_kind || node.virtual_category || node.kind || '',
      cluster_count: node.cluster_count || 0,
      provenance_count: provCount,
      virtual_category: node.virtual_category || '',
      session_id: node.session_id || '',
      status: node.status || '',
      effective: node.effective === true ? 'true' : 'false',
      excluded: node.excluded === true ? 'true' : 'false',
      size,
      bg: node.bg || kindColor(node.kind || node.label || ''),
      opacity: 0.85,
    }, position: positions[node.id] || { x: 0, y: 0 }});
  }
  const edgeKeys = new Set();
  for (const edge of graph.edges || []) {
    const source = String(edge.source || '');
    const target = String(edge.target || '');
    const sourceNode = nodeById.get(source) || {};
    const targetNode = nodeById.get(target) || {};
    const branchKind = targetNode.record_kind || targetNode.virtual_category
      || sourceNode.record_kind || sourceNode.virtual_category || '';
    edgeKeys.add(`${source}:${target}`);
    elements.push({ data: {
      id: edge.id, source, target,
      etype: edge.edge_type || 'derived_from',
      branch_kind: branchKind,
      strength: EDGE_STRENGTH[edge.edge_type] || 0.4,
    }});
  }
  for (const node of graphNodes) {
    const parentId = String(node.parent_id || '');
    const nodeId = String(node.id || '');
    if (!parentId || !nodeId || parentId === nodeId || edgeKeys.has(`${parentId}:${nodeId}`)) continue;
    // 有些旧投影/虚拟叠加只写了 parent_id，忘了落边；这里补出树形连线，主光点到分类不会悬空。
    edgeKeys.add(`${parentId}:${nodeId}`);
    const parentNode = nodeById.get(parentId) || {};
    const branchKind = node.record_kind || node.virtual_category
      || parentNode.record_kind || parentNode.virtual_category || '';
    elements.push({ data: {
      id: `parent-bridge:${parentId}:${nodeId}`, source: parentId, target: nodeId,
      etype: 'derived_from', branch_kind: branchKind, strength: 0.52,
    }});
  }
  return elements;
}

function renderMergeDock(suggestions) {
  // v3.1 §6.3：projection 不再返回 merge_suggestions（自动晋升凋亡已移除）
  // 重复候选在 Memory IR 的 duplicate_groups 中查看
  if (!suggestions || !suggestions.length) return '';
  return `<aside class="merge-dock" aria-label="重复候选">
    <h3>重复候选 · ${suggestions.length} 条</h3>
    ${suggestions.map(item => `<div class="merge-item">
      <div class="merge-copy">${escapeHtml(item.group_id || '')}</div>
      <div class="merge-actions"><span class="merge-score">${item.member_ids ? item.member_ids.length : 0} 条记录</span></div>
    </div>`).join('')}
  </aside>`;
}

function projectionModeLabel(mode) {
  return {
    native_memory_projection: '原生记忆投影',
    logical_reconstruction_projection: '逻辑重构投影',
    shared_memory_projection: '共享记忆',
    evidence_only: '证据/萃取来源'
  }[mode] || mode || '未知';
}

function sourceCategoryLabel(category) {
  return {
    native_memory: '原生记忆', project_memory: '项目记忆', knowledge_source: '知识文档',
    shared_memory: 'MCP 实时记忆', content_source: '内容连接器',
    conversation_history: '会话历史', runtime_evidence: '运行证据', ignored_runtime_data: '忽略运行数据',
    control_surface: '控制面', skill_surface: '技能面'
  }[category] || category || '未知';
}

function renderProjectionSourceMap(sourceMap) {
  const entries = sourceMap?.entries || [];
  const summary = sourceMap?.summary || {};
  const shared = sourceMap?.projection_kind === 'shared_memory_projection';
  const sourceCount = entries.length;
  const sourceDetailsId = 'projection-source-map-details';
  const cards = entries.map(renderProjectionSourceEntry).join('');
  const governedMemory = finiteFirst(summary.governed_memory, summary.shared_memory);
  const connectorCount = finiteFirst(summary.selected_source_connectors, summary.enabled_connectors);
  const connectorTotal = finiteFirst(summary.selected_source_connector_total, summary.source_connector_total);
  const participatingCount = finiteFirst(summary.enabled, summary.governed_memory_eligible);
  const emptyMappingNotice = !shared && !entries.length
    ? `<div class="gate-warning"><strong>当前治理范围已设定，但尚未选择数据源。</strong>范围不等于数据源；请先到数据源页选择并启用 Agent、项目或来源，之后再构建投影。</div><div class="finding-actions"><button class="btn btn-primary" type="button" onclick="switchTab('sources')">去数据源页选择数据源</button></div>`
    : '';
  const emptySourceNotice = !entries.length && shared
    ? '<div class="source-map-empty" role="status"><strong>暂无共享记忆入库来源</strong><p>当前共享组没有可展示的受管记忆或连接器；来源出现后会在此处按需展开。</p></div>'
    : '';
  const toggle = sourceCount
    ? `<div class="source-map-toggle-row"><button class="btn source-map-toggle" type="button" aria-expanded="false" aria-controls="${sourceDetailsId}" data-source-count="${sourceCount}" onclick="toggleSourceMapDetails(this)">展开 ${sourceCount} 条来源</button></div>`
    : '';
  return `<section class="card projection-source-map">
    <div class="card-head"><div><h2>${shared ? '共享记忆入库来源' : '当前数据源映射'}</h2><p>${shared ? '共享图直接读取 SharedMemoryStore。这里展示 active 记忆最初由 MCP 写入或从哪个已授权来源导入；取消来源不会自动删除已经入库的记忆。' : '这里只读展示数据源页已勾选的 Agent / 项目 / 来源。勾选和取消请回到数据源页处理。'}</p></div>
      <div class="chips">${shared ? `<span class="chip chip-info">受管记忆 ${governedMemory}</span><span class="chip chip-info">连接器 ${connectorCount}/${connectorTotal}</span><span class="chip chip-info">参与投影 ${participatingCount}</span>` : `<span class="chip chip-info">启用 ${finiteFirst(summary.enabled)}/${finiteFirst(summary.total)}</span><span class="chip chip-info">原生 ${finiteFirst(summary.native_memory)}</span><span class="chip chip-info">逻辑 ${finiteFirst(summary.logical_reconstruction)}</span><span class="chip chip-medium">证据 ${finiteFirst(summary.evidence_only)}</span>`}</div></div>
    ${emptyMappingNotice}${emptySourceNotice}${toggle}<div id="${sourceDetailsId}" class="source-map-details" hidden><div class="source-map-list">${cards}</div></div>
  </section>`;
}

function renderProjectionSourceEntry(entry) {
  const eligible = entry.logical_eligible || entry.native_eligible;
  const sharedOrigin = entry.is_shared_memory_origin === true;
  const mode = projectionModeLabel(entry.projection_mode);
  const sourceId = safeDisplayText(entry.source_id || entry.root_id || entry.surface_id, '未标识来源');
  const sourceName = safeDisplayText(entry.display_name || entry.surface_id || entry.root_id, '未命名来源');
  const path = guiPathText(entry.path, '无本地路径');
  const project = entry.project_ref || (entry.scope === 'project' ? '当前项目' : entry.scope || '未知');
  const status = sharedOrigin
    ? (entry.participates ? `已入库 · ${entry.record_count || 0} 条` : '历史来源')
    : (entry.enabled ? '已勾选' : '未勾选');
  const statusTone = (sharedOrigin && entry.participates) || entry.enabled ? 'chip-confirmed' : 'chip-medium';
  return `<article class="source-map-entry ${entry.enabled ? '' : 'muted-row'}">
    <div class="source-map-entry-header"><div class="source-map-entry-name"><strong title="${escapeHtml(sourceName)}">${escapeHtml(sourceName)}</strong><div class="surface-meta">${escapeHtml(sourceCategoryLabel(entry.source_category))}</div></div><div class="source-map-entry-status"><span class="chip ${statusTone}">${escapeHtml(status)}</span><span class="chip ${eligible ? 'chip-confirmed' : 'chip-medium'}">${escapeHtml(mode)}</span></div></div>
    <dl class="source-map-fields">
      <div><dt>来源 ID</dt><dd><code class="source-map-id" title="${escapeHtml(sourceId)}">${escapeHtml(sourceId)}</code></dd></div>
      <div><dt>Agent</dt><dd>${escapeHtml(agentDisplayName(entry.agent_instance_id, '未绑定'))}</dd></div>
      <div><dt>项目/范围</dt><dd>${escapeHtml(project)}</dd></div>
      <div><dt>入库策略</dt><dd>${escapeHtml(entry.ingestion_policy || '—')}${sharedOrigin && entry.first_imported_at ? `<div class="surface-meta">${escapeHtml(entry.first_imported_at)}</div>` : ''}</dd></div>
      <div><dt>路径</dt><dd>${escapeHtml(path)}</dd></div>
    </dl>
  </article>`;
}

function toggleSourceMapDetails(button) {
  const detailsId = button && button.getAttribute('aria-controls');
  const details = detailsId ? document.getElementById(detailsId) : null;
  if (!button || !details) return;
  const expanded = button.getAttribute('aria-expanded') === 'true';
  button.setAttribute('aria-expanded', String(!expanded));
  details.hidden = expanded;
  button.textContent = expanded ? `展开 ${button.dataset.sourceCount || 0} 条来源` : '收起';
}

function projectionModeControls() {
  const nativeActive = projectionMode === 'native' ? 'btn-primary' : '';
  const reconstructedActive = projectionMode === 'reconstructed' ? 'btn-primary' : '';
  return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>投影模式</h2><p>原生投影只读查看当前真实记忆；重构治理投影写入 MemoryGuard 管理层，不覆盖原生记忆文件。</p></div></div>
    <div class="finding-actions">
      <button class="btn ${nativeActive}" type="button" onclick="switchProjectionMode('native')">原生记忆投影</button>
      <button class="btn ${reconstructedActive}" type="button" onclick="switchProjectionMode('reconstructed')">重构治理投影</button>
      <button class="btn" type="button" onclick="switchTab('sources')">去数据源页调整</button>
    </div></section>`;
}

async function switchProjectionMode(mode) {
  projectionMode = mode === 'native' ? 'native' : 'reconstructed';
  localStorage.setItem('memoryguard.projectionMode', projectionMode);
  await renderNeurons();
}

function renderNeuronMetaBar(graph) {
  const meta = (graph && graph.meta) || {};
  if (isShareGroupScope()) {
    const gid = (graph && graph.scope && graph.scope.share_group_id)
      || meta.share_group_id || activeShareGroupId || '—';
    const trustedMemberIds = Array.isArray(governanceScopeState.members)
      ? governanceScopeState.members
      : [];
    const trustedBinding = governanceScopeState.binding || objectValue(state.governanceSnapshot?.active_binding);
    const bindingAgentId = safeDisplayText(trustedBinding.agent_instance_id || trustedBinding.instance_id, '');
    const memberIds = [...new Set([
      ...trustedMemberIds,
      ...(bindingAgentId ? [bindingAgentId] : []),
    ])];
    const memberChips = memberIds.length
      ? memberIds.map(memberId => {
          const label = agentDisplayName(memberId);
          return `<span class="chip chip-info">成员 · ${escapeHtml(label)}</span>`;
        }).join('')
      : '<span class="chip chip-medium">投影成员信息待加载</span>';
    const snapshot = governanceSnapshot(state.governanceSnapshot);
    const memberCount = memberIds.length ? memberIds.length : '待加载';
    const activeRecords = finiteNumber(meta.active_records, snapshot.counts.active_memories);
    const conflictCount = finiteNumber(meta.conflict_count, snapshot.counts.conflicts);
    const coverage = safeDisplayText(meta.coverage_status, safeDisplayText(snapshot.coverage_status, 'unknown'));
    const groupLabel = memoryGroupLabel(gid);
    return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>记忆核心状态</h2>
      <p>${groupLabel}状态（组名 · 成员 · 记忆 · 冲突）</p></div></div>
      <div class="chips">
        <span class="chip chip-confirmed" title="${escapeHtml(gid)}">${groupLabel} · ${escapeHtml(gid)}</span>
        ${memberChips}
        <span class="chip chip-info">成员数 · ${memberCount}</span>
        <span class="chip chip-info">记忆 · ${activeRecords}</span>
        <span class="chip chip-${conflictCount ? 'high' : 'confirmed'}">冲突 · ${conflictCount}</span>
        <span class="chip chip-${coverage === 'complete' ? 'confirmed' : 'medium'}">覆盖 · ${escapeHtml(coverage)}</span>
      </div></section>`;
  }
  const instances = meta.agent_instances || [];
  const instanceChips = instances.length ? instances.map(inst => {
     return `<span class="chip chip-info">${escapeHtml(agentDisplayName(inst))} · ${escapeHtml(inst.takeover_state || 'not_detected')}</span>
      <span class="chip chip-info">版本 · ${escapeHtml(inst.managed_version ? inst.managed_version.slice(0,8) : '—')}</span>
      <span class="chip chip-info">记录 · ${inst.record_count || 0}</span>`;
  }).join('') : '<span class="chip chip-info">Agent · 未发现</span>';
  return `<section class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>记忆核心状态</h2>
    <p>顶部 7 项状态信息（v3.1 §6.1）</p></div></div>
    <div class="chips">
      ${instanceChips}
      <span class="chip chip-info">实例数 · ${meta.instance_count || 0}</span>
      <span class="chip chip-info">Release · ${meta.release_count || 0}</span>
      <span class="chip chip-${meta.coverage_status === 'complete' ? 'confirmed' : 'medium'}">覆盖 · ${escapeHtml(meta.coverage_status || 'unknown')}</span>
      <span class="chip chip-${meta.drifted ? 'high' : 'confirmed'}">漂移 · ${meta.drifted ? '是' : '否'}</span>
    </div></section>`;
}

function renderNeuronGraph() {
  const graph = neuronGraph;
  // 顶部状态：单 Agent 用实例条；共享组用组名 + 成员
  const meta = (graph && graph.meta) || {};
  const sourceMapPanel = renderProjectionSourceMap(graph?.source_map || {});
  const engineControls = projectionEngineControls();
  const modeControls = projectionModeControls();
  const metaBar = renderNeuronMetaBar(graph);
  // 未构建时显示门控
  if (!graph || !graph.nodes || !graph.nodes.length || (graph.empty && !graph.virtual_overlay_available)) {
    stopNeuronSignalPulses();
    document.getElementById('neuron-count').textContent = '';
    const reason = graph && graph.reason ? graph.reason : 'not_built';
    const reasonText = {
      'not_built': '尚未构建投影。神经图是 Memory IR 的可视化投影，不是事实源。',
      'no_ir': 'Memory IR 为空，请先在数据源 tab 扫描来源。',
      'no_projection_sources': '当前数据源映射为空；已设治理范围不等于已选择数据源，请先到数据源页启用来源。',
      'blocked': '后端阻止了当前构建，请先处理阻止原因。',
      'error': '投影读取失败。',
    }[reason] || '尚未构建投影。';
    setContent(`<div class="view-heading"><span class="eyebrow">Live cognition map</span><h2>记忆核心</h2>
      <p>神经图是 Memory IR 的可视化投影，不是事实源。删除后可从 IR + DecisionLog 完整重建。图上治理操作会写入 DecisionLog 并生成新规范版本。</p></div>
      ${metaBar}
      ${modeControls}
      ${sourceMapPanel}
      ${engineControls}
      <section class="card projection-gate">
        <div class="gate-orb" aria-hidden="true"></div>
        <div class="gate-body">
          <h3>当前状态：未构建</h3>
          <p class="gate-reason">${escapeHtml(reasonText)}</p>
          <div class="gate-warning">
            <strong>${isShareGroupScope() ? '共享 MCP 投影读取 SharedMemoryStore。' : projectionMode === 'native' ? '原生投影读取当前真实记忆。' : '重构治理会自动萃取、合并和清理记忆。'}</strong><br>
            ${isShareGroupScope() ? '确认正式接管时打版本快照；Agent 通过 MCP 读写共享记忆，不再各自写原生文件。' : projectionMode === 'native' ? '此操作只生成当前原生记忆的可视化图。' : '重构结果保存在 MemoryGuard 管理层；原生记忆文件保持只读。'}
          </div>
          <div class="finding-actions">
            ${buildHasNoSources(graph) || !((graph && graph.source_map && graph.source_map.entries) || []).length
              ? '<button class="btn btn-primary" type="button" onclick="switchTab(\'sources\')">去数据源页选择数据源</button>'
              : `<button class="btn btn-primary" type="button" onclick="buildProjection()">${projectionMode === 'native' ? '构建原生投影' : '构建重构投影'}</button>`}
          </div>
        </div>
      </section>`);
    return;
  }
  const stats = graph.stats || {};
  const publishActions = isShareGroupScope()
    ? `<button class="btn btn-primary" type="button" onclick="commitSharedMemoryGovernance()">确认正式接管</button>`
    : '';
  const suggestions = [];
  selectedNeuronId = null;
  selectedNeuronNode = null;
  selectedNeuronIds = new Set();
  neuronDragState = null;
  renderStatusRail();
  document.getElementById('neuron-count').textContent = stats.node_count || '';
  setContent(`<div class="dashboard-view neurons-view"><div class="dashboard-main">
    <div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Live cognition map</span><h2>记忆核心</h2></div>
      <input id="neuron-search" type="search" placeholder="搜索节点" oninput="filterNeuronGraph()">
      <select id="neuron-kind-filter" onchange="filterNeuronGraph()"><option value="">全部类型</option><option value="rules_habits">规则与习惯</option><option value="conversation_history">对话历史</option></select>
      <select id="neuron-sort" onchange="sortNeuronGraph()"><option value="topology">拓扑布局</option><option value="label">标题排序</option></select><span class="muted">点击任意光点，在右侧查看可读内容。规则与习惯可直接在图内治理。</span>
      <button class="btn" type="button" onclick="fitNeuronGraph()">重置视野</button><button class="btn" type="button" onclick="deleteProjection()">删除投影</button><button class="btn btn-primary" type="button" onclick="buildProjection()">${projectionMode === 'native' ? '重建原生投影' : '重建投影'}</button>${publishActions}
    </div>
    <section class="neuron-shell">
    <div class="neuron-stage" id="neuron-stage">
      <div class="neuron-canvas-haze" aria-hidden="true"></div>
      <div class="neuron-noise-layer" aria-hidden="true"></div>
      <div class="neuron-canvas" id="cy" aria-label="本地记忆神经图画布"></div>
      <div class="neuron-particles" id="neuron-particles" aria-hidden="true"></div>
      <div class="neuron-legend">
        <div class="legend-item"><span class="legend-node soma"></span>记忆胞体</div>
        <div class="legend-item"><span class="legend-node"></span>来源/类型主题</div>
        <div class="legend-item"><span class="legend-node hub"></span>同源突触</div>
        <div class="legend-item"><span class="legend-node anchor"></span>记忆末梢</div>
        <div class="legend-item"><span class="legend-edge"></span>衍生轴突</div>
        <div class="legend-item"><span class="legend-edge related"></span>相似关联</div>
        <div class="legend-item"><span class="legend-edge shared"></span>同源跨类型</div>
      </div>
      <div class="neuron-stats">
        <div class="neuron-stat"><strong>${stats.claim_anchor_count || 0}</strong><span>记忆末梢</span></div>
        <div class="neuron-stat"><strong>${stats.source_hub_count || 0}</strong><span>同源突触</span></div>
        <div class="neuron-stat"><strong>${stats.related_edge_count || 0}</strong><span>相似关联</span></div>
        <div class="neuron-stat"><strong>${stats.shared_source_edge_count || 0}</strong><span>同源连线</span></div>
        <div class="neuron-stat"><strong>${stats.edge_count || 0}</strong><span>关系边</span></div>
      </div>
      ${renderMergeDock(suggestions)}
      <aside class="neuron-popover" id="neuron-popover" role="dialog" aria-live="polite" aria-label="光点治理"></aside>
    </div>
  </section>
  <details class="compact-secondary"><summary>投影范围、数据源与引擎控制</summary>${metaBar}${modeControls}${sourceMapPanel}${engineControls}</details>
  </div></div>`);

  if (typeof cytoscape === 'undefined') {
    document.getElementById('cy').innerHTML = '<div class="empty-state" style="color:var(--red)">本地 Cytoscape 资源加载失败</div>';
    return;
  }

  stopNeuronSignalPulses();
  cyInstance = cytoscape({
    container: document.getElementById('cy'),
    elements: graphElements(graph),
    style: [
      { selector: 'node', style: {
        'shape': 'ellipse',
        'width': 'data(size)', 'height': 'data(size)', 'background-color': 'data(bg)',
        'background-opacity': 'data(opacity)', 'border-width': 1.2, 'border-color': '#6ee7c4',
        'border-opacity': .9,
        'underlay-opacity': 0, 'underlay-padding': 0, 'overlay-opacity': 0,
        'label': 'data(label)', 'color': '#cce5dc', 'font-size': 9.5,
        'font-family': 'Segoe UI, PingFang SC, sans-serif', 'font-weight': 500,
        'text-valign': 'bottom', 'text-halign': 'center', 'text-margin-y': 8,
        'text-outline-width': 2, 'text-outline-color': '#040b09', 'text-wrap': 'wrap', 'text-max-width': 92,
        'transition-property': 'border-width, border-color, background-color, opacity, background-opacity',
        'transition-duration': '220ms',
      }},
      { selector: 'node[kind = "root"]', style: {
        'shape': 'ellipse',
        'background-color': '#6ee7c4', 'background-opacity': .32, 'border-width': 2.8,
        'border-color': '#bcffeb', 'font-size': 11,
      }},
      { selector: 'node[kind = "claim_anchor"]', style: {
        'shape': 'ellipse',
        'background-opacity': .95, 'border-width': 1.0, 'border-color': 'data(bg)',
        'border-opacity': .55, 'label': '',
        'underlay-opacity': 0, 'underlay-padding': 0,
      }},
      { selector: 'node[kind = "source_hub"]', style: {
        'background-opacity': .42, 'border-width': 2.0,
        'border-color': '#7dd3fc', 'border-style': 'dashed',
        'font-size': 9, 'shape': 'ellipse',
      }},
      { selector: 'node[kind = "duplicate_cluster"]', style: {
        'background-opacity': .78, 'border-width': 1.8,
        'border-color': '#d8ffe9', 'label': '',
      }},
      { selector: 'node[record_kind = "rules_habits"]', style: {
        'background-opacity': .48,
        'border-width': 2.2, 'border-color': '#ffe3a1', 'shape': 'ellipse',
        'font-size': 10.5,
      }},
      { selector: 'node[record_kind = "conversation_history"]', style: {
        'background-opacity': .42,
        'border-width': 2.2, 'border-color': '#c5efff', 'shape': 'ellipse',
        'font-size': 10.5,
      }},
      { selector: 'node[kind = "history_session"]', style: {
        'background-color': '#7dd3fc', 'background-opacity': .7,
        'border-width': 1.2, 'border-color': '#bae6fd', 'font-size': 8,
      }},
      { selector: 'edge[etype = "virtual_index"]', style: {
        'line-style': 'dotted', 'line-color': '#8de8cf', 'line-opacity': .42,
        'width': 'mapData(strength, 0, 1, 1.2, 3.8)',
      }},
      { selector: 'node[status = "tentative"]', style: {
        'background-color': '#2b2a20', 'border-color': '#e9bb64', 'border-style': 'dashed',
      }},
      { selector: 'edge', style: {
        'width': 'mapData(strength, 0, 1, .55, 3.2)', 'line-color': '#6ee7c4', 'line-opacity': .22,
        'curve-style': 'unbundled-bezier', 'control-point-distances': 20, 'control-point-weights': .5,
        'target-arrow-shape': 'none', 'transition-property': 'line-opacity, width, line-color', 'transition-duration': '140ms',
      }},
      { selector: 'edge[branch_kind = "rules_habits"]', style: {
        'line-color': '#f6ad55', 'line-opacity': .38, 'line-style': 'dotted',
      }},
      { selector: 'edge[branch_kind = "conversation_history"]', style: {
        'line-color': '#7dd3fc', 'line-opacity': .34, 'line-style': 'dotted',
      }},
      { selector: 'node[record_kind = "rules_habits"][effective = "true"]', style: {
        'background-opacity': .86, 'border-color': '#fff1bd', 'border-width': 2.6,
      }},
      { selector: 'node[record_kind = "rules_habits"][excluded = "true"]', style: {
        'background-color': '#5f2c2c', 'background-opacity': .72, 'border-color': '#fc8181', 'border-style': 'dashed',
      }},
      { selector: 'node[record_kind = "rules_habits"][status = "observing"]', style: {
        'border-color': '#f6ad55', 'border-style': 'dashed', 'background-opacity': .24,
      }},
      { selector: 'edge[etype = "related"]', style: { 'line-style': 'dashed', 'line-opacity': .2, 'line-color': '#9ae6b4' }},
      { selector: 'edge[etype = "shared_source"]', style: { 'line-style': 'dashed', 'line-color': '#63b3ed', 'line-opacity': .34 }},
      { selector: 'edge[etype = "duplicate"]', style: { 'line-style': 'dashed', 'line-color': '#f6ad55', 'line-opacity': .28 }},
      { selector: 'edge.signal', style: {
        'width': 'mapData(strength, 0, 1, 2.4, 4.8)',
        'line-opacity': .82,
      }},
      { selector: 'edge.signal-trail', style: {
        'width': 'mapData(strength, 0, 1, 1.5, 3.0)',
        'line-opacity': .44,
      }},
      { selector: 'node.signal', style: {
        'border-width': 3.2, 'border-color': '#ffffff', 'border-opacity': 1,
        'underlay-opacity': 0, 'underlay-padding': 0,
      }},
      { selector: 'node.hover', style: {
        'border-width': 3.0, 'border-color': '#fff3a3', 'border-opacity': 1,
        'underlay-opacity': 0, 'underlay-padding': 0,
      }},
      { selector: 'node.focusPulse', style: {
        'border-width': 3.6, 'border-color': '#ffffff', 'border-opacity': 1,
        'underlay-color': '#6ee7c4', 'underlay-opacity': .12, 'underlay-padding': 9,
      }},
      { selector: '.neighborhood', style: { 'line-opacity': .62, 'width': 2.1 }},
      { selector: 'node.neighborhood', style: { 'border-color': '#bcffeb', 'border-width': 2.4 }},
      { selector: 'node:selected', style: {
        'border-width': 3.0, 'border-color': '#fff6c7', 'border-opacity': 1,
        'underlay-opacity': 0, 'underlay-padding': 0,
      }},
      { selector: 'node.pulse', style: {
        'border-width': 3.4, 'border-color': '#ffffff', 'border-opacity': 1,
        'underlay-opacity': 0, 'underlay-padding': 0,
      }},
      { selector: 'node.sourcePulse', style: {
        'border-width': 3.7, 'border-color': '#eafff9', 'border-opacity': 1,
        'underlay-color': '#6ee7c4', 'underlay-opacity': .18, 'underlay-padding': 13,
      }},
      { selector: 'node.nodeArrivalPulse', style: {
        'border-width': 2.7, 'border-color': '#d8fff4', 'border-opacity': .96,
        'underlay-color': '#6ee7c4', 'underlay-opacity': .08, 'underlay-padding': 7,
      }},
      { selector: 'node.terminalPulse', style: {
        'border-width': 3.0, 'border-color': '#ffffff', 'border-opacity': 1,
        'underlay-color': '#9ff6dc', 'underlay-opacity': .13, 'underlay-padding': 9,
      }},
    ],
    layout: {
      name: 'preset', animate: true, animationDuration: 720, fit: true, padding: 86,
    },
    minZoom: .22, maxZoom: 3.6,
  });

  cyInstance.on('tap', 'node', event => {
    neuronTapSelectionAdditive = !!(event.originalEvent && (event.originalEvent.shiftKey || event.originalEvent.ctrlKey || event.originalEvent.metaKey));
    selectNeuron(event.target.id());
  });
  cyInstance.on('mouseover', 'node', event => {
    const node = event.target;
    node.addClass('hover');
    node.addClass('neighborhood');
    node.connectedEdges().addClass('neighborhood');
    node.neighborhood('node').addClass('neighborhood');
  });
  cyInstance.on('mouseout', 'node', () => cyInstance.elements().removeClass('neighborhood hover'));
  cyInstance.on('tap', event => { if (event.target === cyInstance) hideNeuronPopover(); });
  cyInstance.on('pan zoom resize', () => { if (selectedNeuronId) positionNeuronPopover(selectedNeuronId); });
  cyInstance.on('drag position', 'node', event => {
    const selId = event.target.id();
    if (selectedNeuronId === selId) positionNeuronPopover(selectedNeuronId);
  });
  const collectNeuronSubtree = (rootId) => {
    const result = new Set();
    const nodes = (neuronGraph && Array.isArray(neuronGraph.nodes)) ? neuronGraph.nodes : [];
    const childrenByParent = new Map();
    nodes.forEach(node => {
      const parentId = String(node.parent_id || '');
      if (!parentId) return;
      if (!childrenByParent.has(parentId)) childrenByParent.set(parentId, []);
      childrenByParent.get(parentId).push(node.id);
    });
    const visit = (nodeId) => {
      for (const childId of childrenByParent.get(nodeId) || []) {
        if (result.has(childId)) continue;
        result.add(childId);
        visit(childId);
      }
    };
    visit(String(rootId || ''));
    return [...result];
  };
  cyInstance.on('grab', 'node', event => {
    if (!cyInstance) return;
    const dragRoot = event.target;
    if (!dragRoot) return;
    if (!dragRoot.selected()) dragRoot.select();
    const dragNodeIds = new Set([dragRoot.id()]);
    collectNeuronSubtree(dragRoot.id()).forEach(id => dragNodeIds.add(id));
    const basePositions = {};
    dragNodeIds.forEach(nodeId => {
      if (nodeId === dragRoot.id()) return;
      const n = cyInstance.getElementById(nodeId);
      if (!n || !n.length) return;
      basePositions[nodeId] = { ...n.position() };
    });
    neuronDragState = {
      dragRootId: dragRoot.id(),
      basePositions,
      rootStart: { ...dragRoot.position() },
    };
  });
  cyInstance.on('drag', 'node', event => {
    if (!neuronDragState || !cyInstance) return;
    if (event.target.id() !== neuronDragState.dragRootId) return;
    const root = cyInstance.getElementById(neuronDragState.dragRootId);
    if (!root || !root.length) return;
    const current = root.position();
    const deltaX = current.x - neuronDragState.rootStart.x;
    const deltaY = current.y - neuronDragState.rootStart.y;
    Object.entries(neuronDragState.basePositions).forEach(([nodeId, pos]) => {
      const n = cyInstance.getElementById(nodeId);
      if (!n || !n.length) return;
      n.position({ x: pos.x + deltaX, y: pos.y + deltaY });
      if (selectedNeuronId === nodeId) positionNeuronPopover(nodeId);
    });
    if (selectedNeuronId === neuronDragState.dragRootId) positionNeuronPopover(neuronDragState.dragRootId);
  });
  cyInstance.on('free', 'node', () => {
    neuronDragState = null;
  });
  startNeuronSignalPulses(cyInstance);
}

function stopNeuronSignalPulses() {
  if (window.__neuronSignalTimer) {
    clearTimeout(window.__neuronSignalTimer);
    window.__neuronSignalTimer = null;
  }
  if (window.__neuronSomaPulse) {
    clearInterval(window.__neuronSomaPulse);
    window.__neuronSomaPulse = null;
  }
  const pending = window.__neuronSignalChains || [];
  pending.forEach(id => clearTimeout(id));
  window.__neuronSignalChains = [];
  const frames = window.__neuronSignalFrames || new Set();
  frames.forEach(id => cancelAnimationFrame(id));
  window.__neuronSignalFrames = new Set();
  window.__neuronSignalWaveIndex = 0;
  const particles = document.querySelectorAll('.neuron-edge-particle');
  particles.forEach(particle => particle.remove());
  window.__neuronSignalRefs = {};
  if (cyInstance) {
    try {
      cyInstance.edges().removeClass('signal signal-trail');
      cyInstance.nodes().removeClass('signal sourcePulse nodeArrivalPulse terminalPulse');
    } catch (e) { /* graph torn down */ }
  }
}

function _signalRefKey(eleId, cls) {
  return eleId + '::' + cls;
}

function _acquireSignal(cy, eleId, cls) {
  if (!window.__neuronSignalRefs) window.__neuronSignalRefs = {};
  const key = _signalRefKey(eleId, cls);
  window.__neuronSignalRefs[key] = (window.__neuronSignalRefs[key] || 0) + 1;
  const ele = cy.getElementById(eleId);
  if (ele && ele.length) ele.addClass(cls);
}

function _releaseSignal(cy, eleId, cls) {
  if (!window.__neuronSignalRefs) window.__neuronSignalRefs = {};
  const key = _signalRefKey(eleId, cls);
  const next = Math.max(0, (window.__neuronSignalRefs[key] || 1) - 1);
  window.__neuronSignalRefs[key] = next;
  if (next > 0) return;
  delete window.__neuronSignalRefs[key];
  const ele = cy.getElementById(eleId);
  if (ele && ele.length) ele.removeClass(cls);
}

function isSignalNeuronEdge(edge) {
  return !!edge && edge.length > 0;
}

function isOutwardNeuronEdge(edge, source = null) {
  if (!isSignalNeuronEdge(edge)) return false;
  const src = source && source.length ? source : edge.source();
  const target = edge.target();
  if (!src || !src.length || !target || !target.length || src.id() === target.id()) return false;
  const parentId = String(target.data('parent_id') || '');
  if (parentId && parentId === src.id()) return true;
  const sourceDepth = Number(src.data('depth'));
  const targetDepth = Number(target.data('depth'));
  const type = String(edge.data('etype') || '');
  return ['derived_from', 'virtual_index'].includes(type)
    && Number.isFinite(sourceDepth) && Number.isFinite(targetDepth)
    && targetDepth > sourceDepth;
}

function collectNeuronSignalSources(cy) {
  if (!cy) return [];
  const roots = [];
  cy.nodes('node[kind = "root"]').forEach(node => {
    if (node.outgoers('edge').filter(edge => isOutwardNeuronEdge(edge, node)).length) roots.push(node);
  });
  if (roots.length) return roots;
  const fallback = [];
  cy.nodes().forEach(node => {
    if (!node || !node.length) return;
    const outward = node.outgoers('edge').filter(edge => isOutwardNeuronEdge(edge, node));
    if (!outward.length) return;
    const parentId = String(node.data('parent_id') || '');
    if (!parentId) fallback.push(node);
  });
  return fallback;
}

function buildNeuronSignalPath(cy, start, seed = 0, firstEdge = null) {
  if (!cy || !start || !start.length) return null;
  const pathNodes = [start];
  const pathEdges = [];
  let cur = start;
  const seen = new Set();
  while (cur && cur.length && !seen.has(cur.id())) {
    seen.add(cur.id());
    const outgoers = cur.outgoers('edge').filter(edge => isOutwardNeuronEdge(edge, cur)).sort((a, b) =>
      String(a.id()).localeCompare(String(b.id()))
    );
    if (!outgoers.length) break;
    let edge = null;
    if (pathEdges.length === 0 && firstEdge && firstEdge.length) {
      edge = firstEdge;
    } else {
      const unit = neuronHashUnit(`${cur.id()}:${seed}:${pathEdges.length}`);
      edge = outgoers[Math.min(outgoers.length - 1, Math.floor(unit * outgoers.length))];
    }
    if (!edge || !edge.length) break;
    pathEdges.push(edge);
    cur = edge.target();
    if (cur && cur.length) pathNodes.push(cur);
  }
  if (!pathEdges.length) return null;
  return { nodes: pathNodes, edges: pathEdges };
}

function collectNeuronSignalPaths(cy, limit = 4, waveIndex = 0) {
  if (!cy || limit < 1) return [];
  const sources = collectNeuronSignalSources(cy);
  if (!sources.length) return [];
  const paths = [];
  const signatures = new Set();
  const sourceStart = Math.abs(waveIndex) % sources.length;
  for (let sourceOffset = 0; sourceOffset < sources.length && paths.length < limit; sourceOffset++) {
    const source = sources[(sourceStart + sourceOffset) % sources.length];
    const rootEdges = source.outgoers('edge').filter(edge => isOutwardNeuronEdge(edge, source)).sort((a, b) =>
      String(a.id()).localeCompare(String(b.id()))
    );
    if (!rootEdges.length) continue;
    const desiredForSource = Math.min(limit - paths.length, Math.max(1, Math.ceil(limit / sources.length)));
    const first = Math.abs(waveIndex + sourceOffset) % rootEdges.length;
    for (let offset = 0; offset < desiredForSource * 5 && paths.length < limit; offset++) {
      const branch = rootEdges[(first + offset) % rootEdges.length];
      const seed = waveIndex * 17 + sourceOffset * 43 + offset * 31;
      const path = buildNeuronSignalPath(cy, source, seed, branch);
      if (!path) continue;
      const signature = path.edges.map(edge => edge.id()).join('>');
      if (!signature || signatures.has(signature)) continue;
      signatures.add(signature);
      paths.push(path);
    }
  }
  return paths;
}

function pickNeuronSignalPath(cy) {
  return collectNeuronSignalPaths(cy, 1, Number(window.__neuronSignalWaveIndex || 0))[0] || null;
}

function runNeuronSignalPulse(cy, path) {
  if (!cy || !path || !path.edges.length) return;
  // One soft wavefront travels root -> branch -> leaf and terminates there.
  const perEdgeMs = 820;
  const totalMs = Math.max(1250, path.edges.length * perEdgeMs);
  try {
    animateNeuronPathParticle(cy, path, totalMs);
  } catch (_) {
    // 粒子层异常不能影响 Cytoscape 边/节点脉冲。
  }
  path.edges.forEach((edge, index) => {
    const startAt = Math.floor(index * perEdgeMs);
    const tid = setTimeout(() => {
      if (!cyInstance || cyInstance !== cy) return;
      _acquireSignal(cy, edge.id(), 'signal');
      const src = edge.source();
      const tgt = edge.target();
      if (src && src.length) _acquireSignal(cy, src.id(), 'signal');
      if (tgt && tgt.length) _acquireSignal(cy, tgt.id(), 'signal');
      const releaseId = setTimeout(() => {
        if (!cyInstance || cyInstance !== cy) return;
        _releaseSignal(cy, edge.id(), 'signal');
        _acquireSignal(cy, edge.id(), 'signal-trail');
        if (src && src.length) _releaseSignal(cy, src.id(), 'signal');
        if (tgt && tgt.length && tgt.flashClass) {
          tgt.flashClass(index === path.edges.length - 1 ? 'terminalPulse' : 'nodeArrivalPulse', index === path.edges.length - 1 ? 180 : 150);
        }
        // Keep leaf lit until the path particle arrives.
        if (tgt && tgt.length && index < path.edges.length - 1) {
          _releaseSignal(cy, tgt.id(), 'signal');
        }
        const trailId = setTimeout(() => {
          if (!cyInstance || cyInstance !== cy) return;
          _releaseSignal(cy, edge.id(), 'signal-trail');
          if (tgt && tgt.length && index === path.edges.length - 1) {
            _releaseSignal(cy, tgt.id(), 'signal');
          }
        }, 180);
        (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(trailId);
      }, perEdgeMs);
      (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(releaseId);
    }, startAt);
    (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(tid);
  });
}

function _neuronEdgeBezier(edge, offsetX, offsetY) {
  const sourcePosition = edge.source().renderedPosition();
  const targetPosition = edge.target().renderedPosition();
  const source = { x: sourcePosition.x + offsetX, y: sourcePosition.y + offsetY };
  const target = { x: targetPosition.x + offsetX, y: targetPosition.y + offsetY };
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const length = Math.hypot(dx, dy) || 1;
  const styleValue = name => (typeof edge.style === 'function' ? edge.style(name) : null);
  const weight = Number.parseFloat(styleValue('control-point-weights')) || .5;
  const distance = Number.parseFloat(styleValue('control-point-distances')) || 0;
  const control = {
    x: source.x + dx * weight - (dy / length) * distance,
    y: source.y + dy * weight + (dx / length) * distance,
  };
  return { source, target, control };
}

function _neuronBezierPoint(source, control, target, t) {
  const eased = t * t * (3 - 2 * t);
  const inverse = 1 - eased;
  const x = inverse * inverse * source.x + 2 * inverse * eased * control.x + eased * eased * target.x;
  const y = inverse * inverse * source.y + 2 * inverse * eased * control.y + eased * eased * target.y;
  const tangentX = 2 * inverse * (control.x - source.x) + 2 * eased * (target.x - control.x);
  const tangentY = 2 * inverse * (control.y - source.y) + 2 * eased * (target.y - control.y);
  return { x, y, angle: Math.atan2(tangentY, tangentX) };
}

function _spawnNeuronParticle() {
  const particle = document.createElement('span');
  particle.className = 'neuron-edge-particle';
  return particle;
}

function animateNeuronEdgeParticle(cy, edge, duration = 820) {
  if (!cy || !edge || !edge.length) return;
  animateNeuronPathParticle(cy, { edges: [edge], nodes: [edge.source(), edge.target()] }, duration);
}

function animateNeuronPathParticle(cy, path, duration = 1250) {
  const layer = document.getElementById('neuron-particles');
  if (!layer || !cy || !path || !path.edges || !path.edges.length) return;
  const edges = path.edges.filter(edge => edge && edge.length);
  if (!edges.length) return;
  const particle = _spawnNeuronParticle();
  layer.appendChild(particle);
  const particleHeight = 5;
  const started = performance.now();
  let frameId = null;
  const cleanup = () => {
    if (frameId !== null) {
      const frames = window.__neuronSignalFrames;
      if (frames && frames.delete) frames.delete(frameId);
      cancelAnimationFrame(frameId);
      frameId = null;
    }
    particle.remove();
  };
  const frame = () => {
    if (!cyInstance || cyInstance !== cy) {
      cleanup();
      return;
    }
    try {
      const cyRect = cy.container().getBoundingClientRect();
      const layerRect = layer.getBoundingClientRect();
      const offsetX = cyRect.left - layerRect.left;
      const offsetY = cyRect.top - layerRect.top;
      const raw = Math.min(1, (performance.now() - started) / duration);
      // Map progress across the whole path so the light reaches the leaf end.
      const scaled = raw * edges.length;
      const idx = Math.min(edges.length - 1, Math.floor(scaled));
      const local = (idx === edges.length - 1)
        ? Math.min(1, scaled - idx)
        : (scaled - idx);
      const edge = edges[idx];
      if (!edge || !edge.length) {
        cleanup();
        return;
      }
      const curve = _neuronEdgeBezier(edge, offsetX, offsetY);
      if (!Number.isFinite(curve.source.x) || !Number.isFinite(curve.target.x)) {
        cleanup();
        return;
      }
      const point = _neuronBezierPoint(curve.source, curve.control, curve.target, local);
      const renderedEdgeLength = Math.hypot(curve.target.x - curve.source.x, curve.target.y - curve.source.y);
      const particleWidth = Math.max(18, Math.min(36, renderedEdgeLength * .14));
      const x = point.x - particleWidth / 2;
      const y = point.y - particleHeight / 2;
      particle.style.width = `${particleWidth}px`;
      particle.style.opacity = String(raw < .04 ? raw / .04 : (raw > .96 ? (1 - raw) / .04 : 1));
      particle.style.transform = `translate3d(${x}px,${y}px,0) rotate(${point.angle}rad)`;
      if (raw < 1) {
        frameId = requestAnimationFrame(frame);
        const frames = window.__neuronSignalFrames || (window.__neuronSignalFrames = new Set());
        frames.add(frameId);
      } else {
        cleanup();
      }
    } catch (_) {
      cleanup();
    }
  };
  frame();
}

function startNeuronSignalPulses(cy) {
  stopNeuronSignalPulses();
  if (!cy) return;
  const fireWave = () => {
    if (!cyInstance || cyInstance !== cy) return;
    const waveIndex = Number(window.__neuronSignalWaveIndex || 0);
    window.__neuronSignalWaveIndex = waveIndex + 1;
    // Every burst begins at a real root/main node.  Two to four short
    // wavefronts fan outward and die at leaves; lateral relation edges never
    // participate, so there is no mid-edge spawning or reverse travel.
    const desired = Math.min(4, Math.max(2, 2 + Math.floor(neuronHashUnit(`wave:${waveIndex}`) * 3)));
    const paths = collectNeuronSignalPaths(cy, desired, waveIndex);
    if (!paths.length) return;
    const pulsedSources = new Set();
    paths.forEach(path => {
      const source = path.nodes && path.nodes.length ? path.nodes[0] : null;
      if (!source || !source.length || pulsedSources.has(source.id())) return;
      pulsedSources.add(source.id());
      if (source.flashClass) source.flashClass('sourcePulse', 420);
    });
    paths.forEach((path, index) => {
      const jitter = Math.floor(neuronHashUnit(`stagger:${waveIndex}:${index}`) * 120);
      const delay = index * 120 + jitter;
      const tid = setTimeout(() => {
        if (!cyInstance || cyInstance !== cy) return;
        runNeuronSignalPulse(cy, path);
      }, delay);
      (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(tid);
    });
  };
  const scheduleNext = () => {
    if (!cyInstance || cyInstance !== cy) return;
    const index = Number(window.__neuronSignalWaveIndex || 0);
    const delay = 2200 + Math.floor(neuronHashUnit(`interval:${index}`) * 1300);
    window.__neuronSignalTimer = setTimeout(() => {
      if (!cyInstance || cyInstance !== cy) return;
      fireWave();
      scheduleNext();
    }, delay);
  };
  const initialWave = setTimeout(() => {
    if (!cyInstance || cyInstance !== cy) return;
    fireWave();
    scheduleNext();
  }, 420);
  (window.__neuronSignalChains || (window.__neuronSignalChains = [])).push(initialWave);
}

function fitNeuronGraph() {
  hideNeuronPopover();
  if (cyInstance) cyInstance.animate({ fit: { eles: cyInstance.elements(), padding: 72 }, duration: 340 });
}

function findNeuronByMemory(memoryId) {
  return (neuronGraph.nodes || []).find(item => item.memory_id === memoryId
    || (item.member_ids || []).includes(memoryId)
    || (item.members || []).some(member => member.memory_id === memoryId)
    || (item.related || []).some(related => related.memory_id === memoryId));
}

function focusNeuronNode(nodeId) {
  if (!cyInstance) return;
  const cyNode = cyInstance.getElementById(nodeId);
  if (!cyNode || !cyNode.length) return;
  cyInstance.animate({ center: { eles: cyNode }, zoom: Math.max(cyInstance.zoom(), 1.18) }, { duration: 420 });
  cyNode.flashClass('focusPulse', 980);
}

function routeVirtualNeuron(node) {
  if (!node || !node.virtual_category) return;
  if (node.virtual_category === 'rules_habits') {
    showToast('规则与习惯为图内索引，选择下方规则即可直接治理。', 'success');
    selectNeuron(node.id);
    return;
  }
  if (node.virtual_category === 'conversation_history') {
    if (node.requires_agent_selection || !activeAgentInstanceId) {
      showToast('对话历史按 Agent 隔离。请先在数据源页选择一个 Agent。', 'info');
      return;
    }
    showToast('会话历史索引已在图内展示。点击会话节点后再按“读取原文”打开。', 'success');
    selectNeuron(node.id);
    return;
  }
}

function openNeuronHistorySession(sessionId) {
  if (!sessionId) return showToast('未找到会话索引', 'error');
  if (!activeAgentInstanceId) {
    showToast('对话历史按 Agent 隔离。请先在数据源页选择一个 Agent。', 'info');
    switchTab('sources');
    return;
  }
  historyFocusSessionId = sessionId;
  switchTab('history');
}

function removeNeuronRuleBodyModal() {
  document.getElementById('neuron-rule-body-modal')?.remove();
}

function openNeuronRuleBodyEditor(memoryId) {
  const node = ruleRecordsById.get(memoryId) || (neuronGraph?.nodes || []).find(item => item.memory_id === memoryId && item.node_kind === 'virtual_rule_ref');
  if (!memoryId || !node) return showToast('未找到可编辑的规则节点', 'error');
  removeNeuronRuleBodyModal();
  const modal = document.createElement('div');
  modal.id = 'neuron-rule-body-modal';
  modal.className = 'modal-backdrop';
  modal.dataset.definitionId = node.definition_id || '';
  modal.dataset.revision = String(node.revision || '');
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="编辑规则正文">
    <div class="modal-head"><h3>编辑规则正文</h3><p>保存前检查版本和强制规则预算，保留修改记录。</p></div>
    <div class="modal-body"><label class="field"><span>正文</span><textarea id="neuron-rule-body-input" rows="8" maxlength="12000"></textarea></label></div>
    <div class="modal-actions"><button class="btn" type="button" data-mg-action="neuron-rule-body-close">取消</button><button class="btn btn-primary" type="button" data-mg-action="neuron-rule-body-save" data-memory-id="${escapeHtml(memoryId)}">保存</button></div>
  </div>`;
  document.body.appendChild(modal);
  const input = document.getElementById('neuron-rule-body-input');
  if (input) { input.value = String(node.canonical_text || node.body || ''); input.focus(); }
}

async function refreshNeuronRuleGovernance(memoryId, message = '') {
  await refreshNeuronGraph(message);
  const next = (neuronGraph?.nodes || []).find(item => item.memory_id === memoryId && item.node_kind === 'virtual_rule_ref');
  if (next) selectNeuron(next.id, false);
}

async function saveNeuronRuleBody(memoryId) {
  const body = String(document.getElementById('neuron-rule-body-input')?.value || '').trim();
  if (!body) return showToast('规则正文不能为空', 'error');
  try {
    const modal = document.getElementById('neuron-rule-body-modal');
    const definitionId = modal?.dataset.definitionId;
    const result = definitionId
      ? await callApi('update_rule_body', definitionId, body, Number(modal.dataset.revision), activeShareGroupId || 'default')
      : await callApi('edit_memory', memoryId, body, activeShareGroupId || 'default');
    if (result.error || result.ok === false) throw new Error(result.error || '更新失败');
    removeNeuronRuleBodyModal();
    if (state.activeTab === 'rules') { await renderRulesHabits(); showToast('规则正文已更新', 'success'); }
    else await refreshNeuronRuleGovernance(memoryId, result.message || '规则正文已更新');
  } catch (error) { showToast(`规则正文更新失败：${error.message || error}`, 'error'); }
}

async function governNeuronRule(memoryId, method) {
  if (!memoryId || !['delete_memory', 'restore_memory'].includes(method)) return;
  const deleting = method === 'delete_memory';
  if (deleting && !confirm('确认软删除这条规则？可在图内恢复，原始历史不会受影响。')) return;
  try {
    const result = await callApi(method, memoryId, activeShareGroupId || 'default');
    if (result.error || result.ok === false) throw new Error(result.error || '操作失败');
    await refreshNeuronRuleGovernance(memoryId, result.message || (deleting ? '规则已软删除' : '规则已恢复'));
  } catch (error) { showToast(`规则${deleting ? '删除' : '恢复'}失败：${error.message || error}`, 'error'); }
}

async function hydrateNeuronNodeDetail(nodeId) {
  const node = (neuronGraph?.nodes || []).find(item => item.id === nodeId);
  if (!node || !node.memory_id || displayBody(node) || node.body) return;
  const requestSeq = ++neuronDetailHydrationSeq;
  node._detail_loading = true;
  node._detail_error = '';
  try {
    // V2 Projection stays reference-only.  Restore the V1 UX by hydrating the
    // selected node from the canonical MemoryAtomStore on demand instead of
    // copying memory bodies back into the durable projection payload.
    const atom = await callApi('get_memory', node.memory_id, activeShareGroupId || '');
    if (requestSeq !== neuronDetailHydrationSeq || selectedNeuronId !== nodeId) return;
    if (!atom || atom.ok === false || atom.error || !atom.memory_id) {
      node._detail_error = apiErrorMessage(atom || {}, '正文读取失败');
      return;
    }
    node.body = String(atom.body || '');
    node.kind = atom.kind || node.kind || '';
    if (atom.confidence != null) node.confidence = atom.confidence;
    node.status = atom.status || node.status || 'active';
    const metadata = atom.metadata && typeof atom.metadata === 'object' ? atom.metadata : {};
    node.title = metadata.title || node.title || node.label || atom.memory_id;
    node.scope = metadata.scope || node.scope || (atom.project_ref ? 'project' : 'shared');
    node.completeness = metadata.completeness || node.completeness || '';
    if (Array.isArray(atom.provenance)) node.provenance_count = atom.provenance.length;
  } catch (error) {
    if (requestSeq === neuronDetailHydrationSeq) {
      node._detail_error = String(error && (error.message || error) || '正文读取失败');
    }
  } finally {
    if (requestSeq === neuronDetailHydrationSeq) {
      node._detail_loading = false;
      if (selectedNeuronId === nodeId) {
        selectedNeuronNode = node;
        renderStatusRail();
      }
    }
  }
}

function selectNeuron(nodeId, focus = true) {
  const node = (neuronGraph.nodes || []).find(item => item.id === nodeId);
  const popover = document.getElementById('neuron-popover');
  if (!node) return;
  selectedNeuronId = nodeId;
  selectedNeuronNode = node;
  const additive = !!neuronTapSelectionAdditive;
  neuronTapSelectionAdditive = false;

  if (cyInstance) {
    const target = cyInstance.getElementById(nodeId);
    const wasSelected = target && target.length && target.selected();
    if (!additive) {
      cyInstance.elements().unselect();
      selectedNeuronIds.clear();
    }
    if (target && target.length) {
      if (additive && wasSelected) {
        target.unselect();
        selectedNeuronIds.delete(nodeId);
      } else {
        target.select();
        selectedNeuronIds.add(nodeId);
      }
    }
    if (additive && selectedNeuronIds.size && !selectedNeuronIds.has(nodeId)) {
      selectedNeuronIds.add(nodeId);
    }
    if (!additive) {
      selectedNeuronId = target && target.length && target.selected() ? nodeId : null;
      selectedNeuronNode = selectedNeuronId ? node : null;
    } else if (!selectedNeuronIds.size) {
      selectedNeuronId = null;
      selectedNeuronNode = null;
    } else if (selectedNeuronIds.has(nodeId) && wasSelected === false && additive) {
      selectedNeuronId = nodeId;
    } else if (!selectedNeuronIds.has(selectedNeuronId) && selectedNeuronIds.size) {
      const nextId = selectedNeuronIds.values().next().value;
      selectedNeuronId = nextId;
      selectedNeuronNode = (neuronGraph.nodes || []).find(item => item.id === nextId) || node;
    }
  }
  if (popover) popover.classList.remove('show');
  renderStatusRail();
  if (focus) focusNeuronNode(nodeId);
  if (selectedNeuronId === nodeId && node.memory_id && !displayBody(node) && !node.body) {
    void hydrateNeuronNodeDetail(nodeId);
  }
}

function selectNeuronByMemory(memoryId) {
  const node = findNeuronByMemory(memoryId);
  if (!node) return showToast('未找到关联节点', 'error');
  selectNeuron(node.id, true);
}

const DECISION_REASON_OPTIONS = {
  exclude: [
    { id: 'wrong', label: '内容错误或过时' },
    { id: 'irrelevant', label: '与当前项目无关' },
    { id: 'duplicate', label: '重复记忆' },
    { id: 'privacy', label: '隐私 / 不应保留' },
    { id: 'low_quality', label: '质量过低，无治理价值' },
  ],
  quarantine: [
    { id: 'secret', label: '含敏感信息（密钥/令牌等）' },
    { id: 'untrusted', label: '内容可疑 / 不可信' },
    { id: 'review', label: '待人工复核' },
    { id: 'policy', label: '与安全策略冲突' },
  ],
  supersede: [
    { id: 'replaced', label: '已被更新内容替代' },
    { id: 'stale', label: '过时版本' },
    { id: 'merged', label: '已合并到其他记忆' },
  ],
  takeover: [
    { id: 'mcp_takeover', label: 'MCP 正式接管（推荐）' },
    { id: 'migration_complete', label: '原生记忆迁移完成' },
    { id: 'governance_verified', label: '治理结果已人工确认' },
  ],
};

async function neuronAction(nodeId, action) {
  // v3.1 §6.2：图上操作 → DecisionEvent → 轻量刷新投影
  let reason = '';
  if (action === 'exclude' || action === 'quarantine' || action === 'supersede') {
    reason = await pickDecisionReason(action);
    if (!reason) return;
  }
  // 先从本地图拿掉，避免「写入中」干等
  const mid = (selectedNeuronNode && (selectedNeuronNode.memory_id || selectedNeuronNode.id)) || nodeId;
  optimisticRemoveNeuron(nodeId, mid);
  showToast('正在写入决策并刷新投影…');
  try {
    const [scope, agentId, groupId] = scopeApiArgs();
    const result = await callApi('neuron_decide', nodeId, action, reason, true, scope, agentId, groupId);
    if (result.error) {
      showToast(result.error, 'error');
      await refreshNeuronGraph();
      return;
    }
    const ver = result.memory_version || result.version_id || '';
    showToast(`已${action === 'exclude' ? '排除' : action === 'quarantine' ? '隔离' : '更新'}${ver ? ' · ' + ver.slice(0, 8) : ''}`, 'success');
    await refreshNeuronGraph();
  } catch (e) {
    showToast('操作失败：' + e, 'error');
    await refreshNeuronGraph();
  }
}

function optimisticRemoveNeuron(nodeId, memoryId) {
  try {
    if (cyInstance && !cyInstance.destroyed()) {
      const el = cyInstance.getElementById(nodeId);
      if (el && el.length) el.remove();
      if (memoryId) {
        cyInstance.nodes().filter(n => n.data('memory_id') === memoryId).remove();
      }
    }
    if (neuronGraph && Array.isArray(neuronGraph.nodes)) {
      neuronGraph.nodes = neuronGraph.nodes.filter(n =>
        n.id !== nodeId && n.memory_id !== memoryId && !(n.member_ids || []).includes(memoryId)
      );
    }
    selectedNeuronIds.delete(nodeId);
    if (memoryId) {
      [...selectedNeuronIds].forEach(id => {
        const targetNode = (neuronGraph?.nodes || []).find(item => item.id === id);
        if (targetNode && targetNode.memory_id === memoryId) selectedNeuronIds.delete(id);
      });
    }
    if (!selectedNeuronIds.size) {
      selectedNeuronId = null;
      selectedNeuronNode = null;
    } else if (!selectedNeuronNode || !selectedNeuronIds.has(selectedNeuronId)) {
      const nextId = selectedNeuronIds.values().next().value;
      selectedNeuronId = nextId;
      selectedNeuronNode = (neuronGraph.nodes || []).find(item => item.id === nextId) || null;
    }
    renderStatusRail();
  } catch (_) {}
}

function pickDecisionReason(action) {
  const options = DECISION_REASON_OPTIONS[action] || [];
  if (!options.length) return Promise.resolve(action);
  return new Promise((resolve) => {
    closeDecisionReasonModal();
    const titles = {
      exclude: '选择排除原因',
      quarantine: '选择隔离原因',
      supersede: '选择覆盖原因',
      takeover: '选择正式接管说明',
    };
    const rows = options.map((o, i) => `<label class="release-option">
      <input type="radio" name="decision-reason" value="${i}" ${i === 0 ? 'checked' : ''}>
      <span><div class="release-title">${escapeHtml(o.label)}</div></span>
    </label>`).join('');
    const modal = document.createElement('div');
    modal.id = 'decision-reason-modal';
    modal.className = 'modal-backdrop';
    modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="${escapeHtml(titles[action] || '选择原因')}">
      <div class="modal-head"><h3>${escapeHtml(titles[action] || '选择原因')}</h3>
        <p>请点选预设原因，无需手输文字。</p></div>
      <div class="modal-body">${rows}</div>
      <div class="modal-actions">
        <button class="btn" type="button" data-act="cancel">取消</button>
        <button class="btn btn-primary" type="button" data-act="ok">确认</button>
      </div>
    </div>`;
    const finish = (value) => {
      closeDecisionReasonModal();
      resolve(value);
    };
    modal.addEventListener('click', (event) => {
      if (event.target === modal) finish('');
    });
    modal.querySelector('[data-act="cancel"]').onclick = () => finish('');
    modal.querySelector('[data-act="ok"]').onclick = () => {
      const selected = modal.querySelector('input[name="decision-reason"]:checked');
      if (!selected) return showToast('请选择一个原因', 'error');
      const opt = options[Number(selected.value)];
      finish(opt ? opt.label : '');
    };
    document.body.appendChild(modal);
  });
}

function closeDecisionReasonModal() {
  const modal = document.getElementById('decision-reason-modal');
  if (modal) modal.remove();
}

function positionNeuronPopover(nodeId) {
  const popover = document.getElementById('neuron-popover');
  const stage = document.getElementById('neuron-stage');
  if (!popover || !stage || !cyInstance) return;
  const element = cyInstance.getElementById(nodeId);
  if (!element || !element.length) return;
  const pos = element.renderedPosition();
  const half = Math.min(180, (stage.clientWidth - 28) / 2);
  const x = Math.max(half + 14, Math.min(stage.clientWidth - half - 14, pos.x));
  const needsBelow = pos.y < Math.min(popover.offsetHeight + 30, stage.clientHeight * .48);
  popover.classList.toggle('below', needsBelow);
  popover.style.left = x + 'px';
  popover.style.top = Math.max(14, Math.min(stage.clientHeight - 14, pos.y)) + 'px';
}

function hideNeuronPopover() {
  selectedNeuronId = null;
  selectedNeuronNode = null;
  selectedNeuronIds = new Set();
  neuronDragState = null;
  const popover = document.getElementById('neuron-popover');
  if (popover) popover.classList.remove('show');
  if (cyInstance) cyInstance.elements().unselect();
  renderStatusRail();
}

async function doPromote(lightId) {
  showToast('v3 已移除自动晋升操作（神经图是纯投影）', 'error');
}

async function doDissolve(lightId) {
  showToast('v3 已移除自动凋亡操作（神经图是纯投影）', 'error');
}

async function doMerge(fromId, toId) {
  showToast('v3 已移除合并操作（神经图是纯投影）', 'error');
}

async function refreshNeuronGraph(message = '') {
  const [scope, agentId, groupId] = scopeApiArgs();
  neuronGraph = await callApi('get_memory_neuron_graph', projectionMode, scope, agentId, groupId);
  renderNeuronGraph();
  if (message) showToast(message, 'success');
}

async function pollBuildProgress(jobId) {
  const phases = [
    { id: 'engine', label: '引擎' },
    { id: 'scan', label: '扫描' },
    { id: 'scope', label: '范围' },
    { id: 'evidence', label: '证据' },
    { id: 'graph', label: '出图' },
    { id: 'save', label: '保存' },
    { id: 'complete', label: '完成' },
  ];
  const result = await waitForTask(jobId, '投影构建', 10 * 60 * 1000, (raw, task) => {
    activeBuildRunId = task.run_id || jobId;
    const view = {
      ...raw,
      job_id: task.run_id || jobId,
      status: task.state,
      phase: task.stage,
      percent: task.progress,
      message: raw.message || (task.state === 'cancelling' ? '正在取消…' : '构建中…'),
      result: task.result_ref,
    };
    renderBuildProgressPage(view, phases);
    renderBuildStatusRail(view);
  });
  activeBuildRunId = '';
  buildCancelInFlight = false;
  if (result.execution_status === 'cancelled') {
    await restoreNeuronAfterBuild('构建已取消', false);
    return;
  }
  if (!result.ok || buildHasNoSources(result) || buildIsBlocked(result) || result.pending) {
    await restoreNeuronAfterBuild(buildResultMessage(result, '构建失败'), true);
    return;
  }
  await refreshNeuronGraph('投影构建完成');
}

async function restoreNeuronAfterBuild(message = '', isError = false) {
  activeBuildRunId = '';
  buildStartInFlight = false;
  buildCancelInFlight = false;
  state.activeTab = 'neurons';
  try {
    await refreshNeuronGraph();
  } catch (e) {
    renderBuildRetryPage(message || '构建未完成');
  }
  renderStatusRail();
  if (message) showToast(message, isError ? 'error' : 'info');
}

function renderBuildRetryPage(message) {
  setContent(`<div class="card empty-state"><div><div class="empty-orb"></div>
    <p>${escapeHtml(message || '构建未完成')}</p>
      <p style="margin-top:6px;font-size:11px">可重新读取 Memory Projection，或返回记忆核心重试。</p>
      <div class="finding-actions" style="margin-top:12px">
       <button class="btn btn-primary" type="button" onclick="renderNeurons()">重新读取 Memory Projection</button>
    </div>
  </div></div>`);
  renderStatusRail();
}

function renderBuildStartingPage() {
  setContent(`<div class="build-progress" role="status" aria-live="polite">
    <div class="bp-kicker">Build progress</div>
    <h2>正在创建构建任务</h2>
    <div class="bp-msg">等待后端返回可追踪任务…</div>
    <div class="bp-bar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i style="width:0%"></i></div>
    <div class="bp-meta"><span>starting</span><span>0%</span></div>
    <div class="bp-phases"></div>
    <div class="finding-actions" style="margin-top:8px">
      <button class="btn" type="button" disabled>正在创建任务…</button>
    </div>
  </div>`);
  renderBuildStatusRail({ phase: 'starting', message: '正在创建任务…', percent: 0, job_id: '' });
}

function renderBuildProgressPage(prog, phases) {
  const pct = Math.max(0, Math.min(100, prog.percent != null ? prog.percent : 0));
  const msg = prog.message || '构建中…';
  const phase = prog.phase || '';
  const phaseOrder = phases.map(p => p.id);
  const idx = Math.max(0, phaseOrder.indexOf(phase));
  const chips = phases.map((p, i) => {
    const cls = i < idx ? 'done' : (i === idx || phase === p.id ? 'active' : '');
    return `<span class="bp-phase ${cls}">${escapeHtml(p.label)}</span>`;
  }).join('');
  setContent(`<div class="build-progress" role="status" aria-live="polite">
    <div class="bp-kicker">Build progress</div>
    <h2>正在构建投影</h2>
    <div class="bp-msg">${escapeHtml(msg)}</div>
    <div class="bp-bar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
    <div class="bp-meta"><span>${escapeHtml(phase || 'starting')}</span><span>${pct}%</span></div>
    <div class="bp-phases">${chips}</div>
     <div class="finding-actions" style="margin-top:8px">
       <button class="btn" type="button" ${prog.status === 'cancelling' || phase === 'cancelling' ? 'disabled' : 'onclick="cancelActiveBuild()"'}>${prog.status === 'cancelling' || phase === 'cancelling' ? '正在取消…' : '取消构建'}</button>
    </div>
  </div>`);
}

function renderBuildStatusRail(prog) {
  const container = document.getElementById('status-rail-content');
  const title = document.querySelector('#status-rail h3');
  if (!container) return;
  if (title) title.textContent = '构建状态';
  const pct = prog.percent != null ? prog.percent : 0;
  container.innerHTML = `
    <div class="status-item"><span class="status-label">阶段</span><span class="status-num" style="font-size:12px">${escapeHtml(prog.phase || '—')}</span></div>
    <div class="status-item"><span class="status-label">进度</span><span class="status-num">${pct}%</span></div>
    <div class="neuron-detail-body" style="margin-top:10px">${escapeHtml(prog.message || '构建中…')}</div>
     ${prog.job_id && prog.status !== 'cancelling'
       ? '<div class="rail-link" onclick="cancelActiveBuild()">取消构建</div>'
       : prog.status === 'cancelling'
       ? '<div class="rail-link" style="opacity:.7;pointer-events:none">正在取消…</div>'
       : '<div class="rail-link" style="opacity:.55;pointer-events:none">正在创建任务…</div>'}`;
}

async function cancelActiveBuild(jobId) {
  // 取消只能使用页面内已确认的精确 run id；没有 id 时不得伪造回退目标。
  const id = String(jobId || activeBuildRunId || '').trim();
  if (!id) {
    await restoreNeuronAfterBuild('当前没有可取消的构建', true);
    return;
  }
  if (buildCancelInFlight) {
    showToast('取消请求已提交，等待后端确认', 'info');
    return;
  }
  buildCancelInFlight = true;
  try {
    renderBuildStatusRail({ status: 'cancelling', phase: 'cancelling', message: '正在提交取消请求…', percent: 0, job_id: id });
    const result = await callApi('cancel_build_projection', id, true);
    if (!result || result.error || result.ok === false) {
      await restoreNeuronAfterBuild(buildResultMessage(result || {}, '取消失败'), true);
      return;
    }
    if (result.status === 'cancelled' || (result.task && result.task.state === 'cancelled')) {
      await restoreNeuronAfterBuild('构建已取消', false);
      return;
    }
    if (String(result.status || '').toLowerCase() === 'failed') {
      await restoreNeuronAfterBuild(buildResultMessage(result, '取消失败'), true);
      return;
    }
    buildCancelInFlight = false;
    showToast('已提交取消请求，等待后端确认；当前构建尚未确认停止', 'info');
  } catch (e) {
    await restoreNeuronAfterBuild('取消失败：' + (e.message || e), true);
  }
}

async function buildProjection(skipConfirm = false) {
  if (buildStartInFlight || activeBuildRunId) {
    showToast('已有构建任务正在创建或进行中，不能重复确认', 'info');
    return;
  }
  const native = projectionMode === 'native';
  const shared = isShareGroupScope();
  const message = shared
    ? '构建共享 MCP 记忆投影？\n\n· 读取当前共享治理范围\n· 生成 Memory Projection\n· 不修改 Agent 原生记忆文件\n\n继续？'
    : native
    ? '构建原生记忆投影？\n\n· 读取当前治理范围内的原生/项目记忆\n· 生成当前真实记忆图\n· 不修改原生记忆文件\n\n继续？'
    : '构建重构治理投影？\n\n· 读取当前治理范围内的来源\n· 生成 Memory Projection\n· 原生记忆文件保持只读\n\n继续？';
  if (!skipConfirm && !confirm(message)) return;

  buildStartInFlight = true;
  renderBuildStartingPage();
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) { await restoreNeuronAfterBuild('缺少治理范围，请先选择 Agent 或共享组', true); return; }
    const [scope, agentId, groupId] = scopeApiArgs();
    const engineId = selectedProjectionEngine === 'deterministic' ? '' : selectedProjectionEngine;
    const enrichMode = engineId ? 'cli' : 'deterministic';
    const result = await callApi(
      'start_build_projection', true, projectionMode, scope, agentId, groupId, engineId, '', enrichMode,
    );
    const task = normalizeTaskState(result || {});
    const runId = task.run_id || (result && result.request && result.request.request_id) || '';
    if (result.error && !runId) { await restoreNeuronAfterBuild(buildResultMessage(result, '构建启动失败'), true); return; }
    if (runId) {
      activeBuildRunId = runId;
      buildStartInFlight = false;
      const initialStatus = task.state && task.state !== 'unknown' ? task.state : (result.status || 'queued');
      renderBuildProgressPage({
        ...result,
        job_id: runId,
        status: initialStatus,
        phase: task.stage || 'queued',
        percent: task.progress || 0,
        message: '构建任务已创建，正在运行…',
      }, [
        { id: 'engine', label: '引擎' }, { id: 'scan', label: '扫描' },
        { id: 'scope', label: '范围' }, { id: 'evidence', label: '证据' },
        { id: 'graph', label: '出图' }, { id: 'save', label: '保存' },
        { id: 'complete', label: '完成' },
      ]);
      renderBuildStatusRail({job_id: runId, status: initialStatus, phase: task.stage || 'queued', percent: task.progress || 0, message: '构建任务已创建，正在运行…'});
      await pollBuildProgress(runId);
      return;
    }
    if (result.ok === false || result.error || result.status === 'queued' || result.status === 'running') {
      await restoreNeuronAfterBuild(buildResultMessage(result, '构建启动失败：后端未返回可追踪任务 ID'), true);
      return;
    }
    if (buildHasNoSources(result) || buildIsBlocked(result)) {
      await restoreNeuronAfterBuild(buildResultMessage(result, '构建未完成'), true);
      return;
    }
    await refreshNeuronGraph(shared ? '共享组投影构建完成' : native ? '原生投影构建完成' : '重构投影构建完成');
  } catch (e) { await restoreNeuronAfterBuild(buildResultMessage({error: e.message || e}, '构建失败'), true); }
  finally { buildStartInFlight = false; }
}

async function deleteProjection() {
  if (!confirm(`删除当前${projectionMode === 'native' ? '原生' : '重构'}投影？\n\n只删除投影文件，不删除原生记忆。`)) return;
  try {
    const ok = await ensureGovernanceScope();
    if (!ok) return showToast('缺少治理范围，无法删除投影', 'error');
    const [scope, agentId, groupId] = scopeApiArgs();
    const result = await callApi('delete_projection', true, projectionMode, scope, agentId, groupId);
    if (result.error) return showToast(result.error, 'error');
    await refreshNeuronGraph('当前投影已删除，可随时重建');
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

async function commitSharedMemoryGovernance() {
  const gid = activeShareGroupId;
  if (!gid) return showToast('请先选择或创建共享组', 'error');
  const reason = await pickDecisionReason('takeover');
  if (!reason) return;
  if (!confirm(`确认对共享组正式接管？\n\n· 共享组：${gid}\n· 对 SharedMemoryStore 打版本快照\n· Agent 已通过 MCP 重定向读写共享记忆\n\n继续？`)) return;
  showToast('正在提交共享记忆治理…');
  try {
    const result = await callApi('commit_shared_memory_governance', gid, reason, true);
    if (result.error) return showToast(result.error, 'error');
    showToast(`正式接管已确认：${result.version_id || ''}`, 'success');
    try {
      await refreshNeuronGraph();
      if (result.projection_warning) {
        showToast('正式接管已完成；后台投影重建提示：' + result.projection_warning, 'info');
      }
    } catch (refreshError) {
      showToast('正式接管已完成，但神经图刷新失败：' + refreshError, 'info');
    }
  } catch (e) { showToast('提交失败：' + e, 'error'); }
}

async function importNativeMemoriesToGroup(groupId) {
  const gid = groupId || activeShareGroupId;
  if (!gid) return showToast('缺少共享组 ID', 'error');
  if (!confirm(`从各 Agent 已勾选的原生/项目记忆导入共享组？\n\n· 共享组：${gid}\n· 一次性迁移，写入 SharedMemoryStore\n\n继续？`)) return;
  showToast('正在导入原生记忆…');
  try {
    const result = await callApi('import_native_memories_to_group', gid, null, true);
    if (result.error) return showToast(result.error, 'error');
    await setActiveShareGroup(gid);
    await refreshNeuronGraph(`导入完成：${result.records_written || 0} 条记录`);
  } catch (e) { showToast('导入失败：' + e, 'error'); }
}

async function installSharedGroupMcpRedirects(groupId) {
  const gid = groupId || activeShareGroupId;
  if (!gid) return showToast('缺少共享组 ID', 'error');
  if (!confirm(`为共享组内 Agent 安装全局 MCP + Hook 接管？\n\n· 写入各 Agent 的用户级配置\n· 自动安装宿主支持的生命周期 Hook\n· 固定连接当前 MemoryGuard 控制目录\n· 以后从任意项目启动都读写同一共享记忆\n\n继续？`)) return;
  showToast('正在安装 MCP 与宿主 Hook…');
  try {
    const result = await callApi('install_shared_group_mcp_redirects', gid, true);
    if (result.error) return showToast(result.error, 'error');
    const configured = result.configured_count || 0;
    const skipped = result.skipped_count || 0;
    const failed = result.error_count || 0;
    const warnings = result.warning_count || 0;
    const hookConfigured = result.hook_configured_count || 0;
    const hookUnsupported = result.hook_unsupported_count || 0;
    const hookFailed = result.hook_error_count || 0;
    const warningText = (result.installed || []).flatMap(x => x.warnings || []).join('；');
    if (result.status === 'failure') {
      return showToast(`MCP 配置失败：${failed} 个错误，${skipped} 个暂缺自动安装适配器`, 'error');
    }
    const suffix = `${skipped ? `，${skipped} 个暂缺自动安装适配器` : ''}${failed ? `，${failed} 个 MCP 失败` : ''}${hookUnsupported ? `，${hookUnsupported} 个宿主无已验证 Hook` : ''}${hookFailed ? `，${hookFailed} 个 Hook 失败` : ''}${warnings ? `，${warnings} 条配置提示` : ''}`;
    showToast(`全局 MCP 已配置 ${configured} 个 Agent，Hook 已配置 ${hookConfigured} 个${suffix}。请按提示重启/信任 Hook；之后任意项目都会连接当前共享记忆。${warningText ? ` ${warningText}` : ''}`,
      result.status === 'partial' ? 'info' : 'success');
  } catch (e) { showToast('安装失败：' + e, 'error'); }
}

async function reExtract() {
  // 兼容旧调用：转发到 buildProjection
  await buildProjection();
}

function unavailableMetric(value, fallback = '暂不可用') {
  if (value === null || value === undefined || value === '') return fallback;
  const text = String(value);
  return text === 'undefined' || text === 'null' || text === 'NaN' ? fallback : text;
}

function eventActionLabel(value) {
  const key = String(value || '').trim().toLowerCase();
  const labels = {
    auto: '自动处理', auto_write: '自动写入', write: '写入',
    auto_supersede: '自动覆盖', supersede: '覆盖',
    auto_quarantine: '自动隔离', quarantine: '隔离',
    conflict: '冲突', merge: '合并', restore: '恢复',
    delete: '删除', classify: '分类',
  };
  return labels[key] || (key ? humanizeUiKey(key, '自动处理') : '自动处理');
}

function activityActorLabel(item = {}) {
  const authority = String(item.authority || item.actor_type || item.actor_kind || '').trim().toLowerCase();
  const actor = String(item.actor || item.actor_id || item.agent_instance_id || '').trim().toLowerCase();
  if (authority === 'system' || actor.startsWith('organizer:')) return 'MemoryGuard 自动治理';
  return agentDisplayName(item.agent_instance_id || item.actor || '', eventActionLabel(item.action || item.type || 'auto'));
}

function renderOverviewLegacy() {
  const report = state.report || {};
  const snap = state.governanceSnapshot && typeof state.governanceSnapshot === 'object'
    ? state.governanceSnapshot : null;

  const evt = snap && snap.latest_event;
  const sup = snap && snap.latest_supersede;
  const conf = snap && snap.conflicts;
  const quar = snap && snap.quarantine;
  const hasEvents = !!(snap && snap.has_events);

  const evtCard = evt ? `<div class="flow-card cyan" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('recent_events'),50)">
    <div class="flow-kicker">最新记忆写入</div>
     <div class="flow-title">${escapeHtml(activityActorLabel(evt))}</div>
    <div class="flow-body">${escapeHtml(evt.raw_content_preview || '(无内容)')}${evt.auto_actions && evt.auto_actions.length ? '<br>自动处理：' + evt.auto_actions.map(a => escapeHtml(eventActionLabel(a.action || a.type))).join('、') : ''}</div>
    <div class="flow-time">${escapeHtml(evt.created_at || '')}</div>
  </div>` : `<div class="flow-card empty cyan"><div class="flow-kicker">新写入</div><div class="flow-title">${hasEvents ? '暂无事件' : '等待事件'}</div><div class="flow-body">${hasEvents ? '' : 'Agent 写入记忆后，事件将出现在这里。'}</div></div>`;

  const supCard = sup ? `<div class="flow-card gray" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('supersede'),50)">
    <div class="flow-kicker">被覆盖的旧记忆</div>
    <div class="flow-title">${escapeHtml(eventActionLabel('auto_supersede'))}</div>
    <div class="flow-body">新：${escapeHtml((sup.new_content_preview || '').slice(0, 80))}<br>旧：${escapeHtml((sup.old_content_preview || '').slice(0, 80))}<br>原因：${escapeHtml(sup.reason || '未提供原因')}</div>
    <div class="flow-time">${escapeHtml(sup.created_at || '')}</div>
  </div>` : `<div class="flow-card empty gray"><div class="flow-kicker">覆盖</div><div class="flow-title">${hasEvents ? '暂无覆盖' : '等待事件'}</div><div class="flow-body">${hasEvents ? '' : '自动覆盖决策将出现在这里。'}</div></div>`;

  const confCard = conf && conf.count > 0 ? `<div class="flow-card amber" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('conflicts'),50)">
    <div class="flow-kicker">未解决冲突</div>
    <div class="flow-title">${conf.count} 组冲突</div>
    <div class="flow-body">${escapeHtml(conf.first_reason || '点击查看冲突队列')}</div>
  </div>` : `<div class="flow-card empty amber"><div class="flow-kicker">冲突</div><div class="flow-title">${hasEvents ? '无未解决冲突' : '等待事件'}</div><div class="flow-body">${hasEvents ? '' : '运行期冲突将出现在这里。'}</div></div>`;

  const quarCard = quar && quar.count > 0 ? `<div class="flow-card red" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('quarantine'),50)">
    <div class="flow-kicker">隔离项</div>
    <div class="flow-title">${quar.count} 条隔离</div>
    <div class="flow-body">${quar.items && quar.items.length ? '模式：' + escapeHtml(quar.items[0].detected_pattern || '') + ' · ' + escapeHtml(quar.items[0].masked_preview || '') : '点击查看隔离队列'}</div>
  </div>` : `<div class="flow-card empty red"><div class="flow-kicker">隔离</div><div class="flow-title">${hasEvents ? '无隔离项' : '等待事件'}</div><div class="flow-body">${hasEvents ? '' : '隔离项将出现在这里。'}</div></div>`;

  const summary = report.summary && typeof report.summary === 'object' ? report.summary : {};
  const findings = Array.isArray(report.findings) ? report.findings : [];
  const health = optionalFiniteNumber(report.health_score);
  const hasCompletedAudit = report.audit_state === 'completed' && health !== null;
  const healthText = hasCompletedAudit ? `${Math.round(Math.max(0, Math.min(100, health)))}/100` : '待扫描';
  const healthColor = !hasCompletedAudit ? 'var(--faint)' : (health >= 70 ? 'var(--accent)' : health >= 40 ? 'var(--orange)' : 'var(--red)');
  const objectCount = finiteOrNull(summary.object_count);
  const generatedAt = unavailableMetric(report.generated_at, '暂无扫描时间');
  const severity = Object.entries(summary.finding_count_by_severity || {})
    .map(([name, count]) => `<span class="chip chip-${escapeHtml(name)}">${escapeHtml(riskSeverityLabel(name))} · ${count}</span>`).join('');
  const invisible = finiteNumber(summary.invisible_count, 0) > 0 ? `<section class="card"><div class="card-head"><div><h2>不可见范围</h2><p>治理边界之外的对象会明确显示，不会静默忽略。</p></div></div>
    ${(Array.isArray(report.invisible) ? report.invisible : []).map(item => `<div class="finding-evidence">${escapeHtml(guiPathText(item.path))} · ${escapeHtml(item.reason || '')}</div>`).join('')}</section>` : '';
  const emptyNote = hasEvents ? '' : `<section class="card empty-state"><div><div class="empty-orb"></div>
        <p>尚无记忆写入事件</p>
        <p style="margin-top:6px;font-size:11px">连接本地 Agent 或导入示例工作区以查看治理流。下方数字只来自已返回的扫描结果，缺失项显示为暂不可用。</p>
      </div></section>`;

  setContent(`<div class="view-heading"><span class="eyebrow">Governance Flow</span><h2>总览</h2>
    <p>概念图式的治理流控制台。新写入 -> 覆盖 / 冲突 / 隔离，实时展示真实事件。</p></div>
    ${emptyNote}
    <div class="flow-canvas">${evtCard}${supCard}${confCard}${quarCard}</div>
    <div class="overview-grid">
      <section class="card"><div class="card-head"><div><h2>风险频谱</h2><p>仅保留扫描返回的严重度信号，不补造统计</p></div></div><div class="chips">${severity || '<span class="chip chip-low">当前未发现风险</span>'}</div></section>
      <section class="card"><div class="card-head"><div><h2>健康度</h2></div></div><div class="scan-list">
        <div class="scan-row"><span>健康分</span><strong style="color:${healthColor}">${escapeHtml(healthText)}</strong></div>
        <div class="scan-row"><span>已识别对象</span><strong>${objectCount === null ? '暂不可用' : objectCount}</strong></div>
        <div class="scan-row"><span>风险信号</span><strong>${findings.length}</strong></div>
        <div class="scan-row"><span>生成时间</span><strong>${escapeHtml(generatedAt)}</strong></div>
      </div></section>
    </div>${invisible}`);
}

function riskSeverityLabel(value, finding = {}) {
  const readable = String(finding?.severity_label || '').trim();
  if (readable) return readable;
  const key = String(value || '').trim().toLowerCase();
  const labels = {critical: '极高风险', high: '高风险', medium: '中风险', low: '低风险', info: '提示'};
  return labels[key] || (key ? '未知严重度（' + humanizeUiKey(key) + '）' : '未知严重度');
}

function riskRuleLabel(value, finding = {}) {
  const key = String(value || '').trim().toLowerCase();
  const labels = {
    forbidden_path: '禁止访问路径', sensitive_path: '敏感路径', unsafe_path: '不安全路径',
    forbidden_write: '禁止写入', unsafe_write: '不安全写入', scope_violation: '范围越界',
    cross_scope_write: '跨范围写入', missing_evidence: '缺少证据', stale_reference: '引用已过期',
    reference_audit_blocker: '引用审计阻断', duplicate_memory: '重复记忆', conflict: '规则冲突',
    quarantine: '隔离风险', unverified_change: '变更未验证', missing_test: '缺少测试',
    schema_unreadable: '结构不可读', partial_schema: '结构不完整',
    unknown_authoritative_table: '未知权威表', unknown_authoritative_column: '未知权威列',
    integrity_check: '完整性检查失败', foreign_key_check: '外键检查失败',
    missing_database: '缺少数据库', storage_unreadable: '存储不可读',
    manifest_not_ready: '清单尚未就绪', manifest_unreadable: '清单不可读',
    unconsumed_outbox: '共享组事件待消费',
  };
  const title = String(finding?.title_zh || finding?.zh_title || finding?.title || finding?.type_label || '').trim();
  const genericTitle = /^(unknown risk|unknown|未知风险|未知)$/i.test(title);
  return (!genericTitle && title) || labels[key] || (key ? '未知风险（' + humanizeUiKey(key) + '）' : '未知风险');
}

function riskDimensionLabel(value, finding = {}) {
  const readable = String(finding?.dimension_label || '').trim();
  if (readable) return readable;
  const key = String(value || '').trim().toLowerCase();
  const labels = {
    integrity: '数据完整性', security: '安全性', scope: '适用范围', provenance: '来源可信度',
    freshness: '内容新鲜度', correctness: '正确性', reliability: '可靠性',
    'reference audit': '引用审计', audit: '审计',
  };
  return labels[key] || (key ? '未知维度（' + humanizeUiKey(key) + '）' : '未知维度');
}

function riskSurfaceLabel(value, finding = {}) {
  const readable = String(finding?.surface_label || '').trim();
  if (readable) return readable;
  const key = String(value || '').trim().toLowerCase();
  const labels = {
    runtime: '运行时', storage: '存储', filesystem: '文件系统', source: '数据源',
    api: '接口', gui: '治理界面', bridge: '本地桥接', governance: '治理层',
  };
  return labels[key] || (key ? '未知来源（' + humanizeUiKey(key) + '）' : '未知来源');
}

function riskEvidenceSummary(finding = {}) {
  const raw = finding?.evidence_summary || finding?.summary || finding?.evidence || '';
  const value = raw && typeof raw === 'object'
    ? (raw.summary || raw.text || raw.message || raw.detail || '') : raw;
  const text = String(value || '').trim();
  if (!text) return '暂未提供证据摘要，请展开详情查看内部记录。';
  return text.length > 240 ? text.slice(0, 237) + '...' : text;
}

function riskReadableField(value, fallback) {
  const text = String(value ?? '').trim();
  if (!text || text === 'undefined' || text === 'null') return fallback;
  return text;
}

function riskTechnicalSource(finding = {}) {
  return [finding?.domain, finding?.table].map(value => String(value || '').trim()).filter(Boolean).join(' / ');
}

function isUnconsumedOutboxFinding(finding = {}) {
  const ruleId = String(finding?.rule_id || finding?.code || '').trim().toLowerCase();
  const title = String(finding?.title || finding?.type_label || '').trim().toLowerCase();
  const source = riskTechnicalSource(finding).toLowerCase();
  return ruleId === 'unconsumed_outbox'
    || (source === 'system / group_outbox' && title === 'unconsumed outbox');
}

function riskAuditTargetLabel(finding = {}) {
  const source = riskTechnicalSource(finding).toLowerCase();
  const labels = {
    'system / group_outbox': '系统域的共享组出站记录',
    system: '系统域记录',
    group_outbox: '共享组出站记录',
  };
  return labels[source] || '';
}

function riskReasonText(finding = {}) {
  const raw = riskReadableField(finding.reason || finding.why || finding.cause || riskEvidenceSummary(finding), '');
  const source = riskTechnicalSource(finding);
  const target = riskAuditTargetLabel(finding);
  if (isUnconsumedOutboxFinding(finding)) return '共享组事件队列存在未消费记录。';
  const sourceOnly = source && raw && raw.replace(/\s+/g, '').toLowerCase() === source.replace(/\s+/g, '').toLowerCase();
  if (target && (!raw || sourceOnly)) return `引用审计未能确认“${target}”的结构或引用完整性。`;
  return raw || '暂未提供原因，请展开详情查看内部记录。';
}

function riskImpactText(finding = {}) {
  return riskReadableField(
    finding.impact || finding.impact_zh || finding.effect,
    '影响未返回：可能削弱治理完整性或让错误记忆继续生效。',
  );
}

function riskActionText(finding = {}) {
  return riskReadableField(
    finding.suggestion || finding.suggested_action || finding.action,
    '建议先核验证据，再交给 Agent 处理，完成后重新扫描验证。',
  );
}

function renderFindings() {
  const report = state.report || {};
  const findings = Array.isArray(report.findings) ? report.findings : [];
  if (!findings.length) {
    setContent('<div class="view-heading"><span class="eyebrow">Risk signals</span><h2>风险信号与治理控制台</h2><p>风险条目会说明原因、影响和建议动作。治理处置仍在治理台完成。</p></div><div class="card empty-state"><div><div class="empty-orb"></div><p>没有发现需要处理的风险信号。</p></div></div>');
    return;
  }
  const items = findings.map((finding, index) => `<article class="finding-item sev-${escapeHtml(finding.severity)}" role="button" tabindex="0"
    aria-expanded="false"
    onclick="toggleFinding('${escapeHtml(finding.id)}')"
    onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleFinding('${escapeHtml(finding.id)}')}">
    <div class="finding-header"><span class="finding-rule"><span class="chip chip-${escapeHtml(finding.severity)}">${escapeHtml(riskSeverityLabel(finding.severity, finding))}</span> ${escapeHtml(riskRuleLabel(finding.rule_id, finding))}</span>
      <span class="finding-toggle" id="toggle-${escapeHtml(finding.id)}">展开详情</span></div>
    <div class="surface-meta finding-meta">风险维度：${escapeHtml(riskDimensionLabel(finding.dimension, finding))} · 来源：${escapeHtml(riskSurfaceLabel(finding.surface, finding))}</div>
    <div class="risk-explain">
      <div><strong>证据摘要</strong>：${escapeHtml(riskEvidenceSummary(finding))}</div>
      <div><strong>原因</strong>：${escapeHtml(riskReasonText(finding))}</div>
      <div><strong>影响</strong>：${escapeHtml(riskImpactText(finding))}</div>
      <div><strong>建议动作</strong>：${escapeHtml(riskActionText(finding))}</div>
    </div>
    <div class="finding-detail" id="detail-${escapeHtml(finding.id)}" style="display:none">
      <div class="row"><span class="key">内部规则 code</span><code>${escapeHtml(finding.rule_id || '')}</code></div>
      <div class="row"><span class="key">技术来源</span><code>${escapeHtml(riskTechnicalSource(finding) || '未提供')}</code></div>
      <div class="row"><span class="key">内部路径</span><code>${escapeHtml(finding.location?.path || '')}:${escapeHtml(finding.location?.span?.[0] || '')}</code></div>
      <div class="row"><span class="key">风险维度</span><span>${escapeHtml(riskDimensionLabel(finding.dimension, finding))}</span></div>
      <div class="row"><span class="key">风险来源</span><span>${escapeHtml(riskSurfaceLabel(finding.surface, finding))}</span></div>
      <div class="row"><span class="key">影响</span><span>${escapeHtml(riskImpactText(finding))}</span></div>
      <div class="row"><span class="key">建议</span><span>${escapeHtml(riskActionText(finding))}</span></div>
      <div class="row"><span class="key">置信度</span><span>${Number.isFinite(Number(finding.confidence)) ? (finding.confidence * 100).toFixed(0) : '暂不可用'}%</span></div>
      <div class="finding-actions">
        <button class="btn" type="button" onclick="event.stopPropagation();copyFindingForAgent('${escapeHtml(finding.id)}')">复制给 Agent 处理</button>
        ${finding.fixable ? `<button class="btn btn-primary" type="button" onclick="event.stopPropagation();generatePlan('${escapeHtml(finding.id)}')">生成修复计划</button>` : ''}
      </div>
    </div></article>`).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Risk signals</span><h2>风险信号与治理控制台</h2>
    <p>带“可生成变更”的风险可由 MemoryGuard 自动修复；其余是诊断证据，请交给 Agent 分析处理，完成后重新扫描验证。</p>
    <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="copyAllFindingsForAgent()">复制全部风险给 Agent</button>
      <button class="btn" type="button" onclick="switchTab('governance')">打开治理台</button></div>
  </div>${items}`);
}

function toggleFinding(id) {
  const element = document.getElementById('detail-' + id);
  if (!element) return;
  const open = element.style.display === 'none';
  element.style.display = open ? 'block' : 'none';
  const card = element.closest('.finding-item');
  if (card) card.setAttribute('aria-expanded', open ? 'true' : 'false');
  const label = document.getElementById('toggle-' + id);
  if (label) label.textContent = open ? '收起详情' : '展开详情';
}

function findingAgentPrompt(finding) {
  const line = finding.location && finding.location.span ? finding.location.span[0] : 1;
  const path = finding.location && finding.location.path ? finding.location.path : '未知位置';
  return [
    '请处理下面的 MemoryGuard 风险信号：',
    `规则：${riskRuleLabel(finding.rule_id, finding)}`,
    `严重度：${riskSeverityLabel(finding.severity, finding)}`,
    `维度/表面：${riskDimensionLabel(finding.dimension, finding)} / ${riskSurfaceLabel(finding.surface, finding)}`,
    `位置：${path}:${line}`,
    `原因：${riskReasonText(finding)}`,
    `影响：${riskImpactText(finding)}`,
    `建议动作：${riskActionText(finding)}`,
    '',
    '要求：先核验证据和根因，再修改被引用的项目内容；不要修改 MemoryGuard 的来源文件，也不要盲目删除。完成后重新运行扫描，确认该风险消失且没有引入回归。',
  ].join('\n');
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    return true;
  } catch (_) {
    return false;
  }
}

async function copyFindingForAgent(findingId) {
  const finding = (Array.isArray(state.report?.findings) ? state.report.findings : []).find(item => item.id === findingId);
  if (!finding) return showToast('未找到风险信号', 'error');
  const ok = await copyText(findingAgentPrompt(finding));
  showToast(ok ? '已复制，可直接粘贴给 Agent 处理' : '复制失败，请展开后手动复制证据', ok ? 'success' : 'error');
}

async function copyAllFindingsForAgent() {
  const findings = Array.isArray(state.report?.findings) ? state.report.findings : [];
  if (!findings.length) return showToast('当前没有风险信号', 'info');
  const text = findings.map((finding, index) =>
    `# 风险 ${index + 1}\n${findingAgentPrompt(finding)}`
  ).join('\n\n---\n\n');
  const ok = await copyText(text);
  showToast(ok ? `已复制 ${findings.length} 条风险，可直接粘贴给 Agent 处理` : '复制失败，请逐条复制', ok ? 'success' : 'error');
}

async function generatePlan(findingId) {
  showToast('正在生成修复计划…');
  try {
    const result = await callApi('generate_plan', findingId);
    if (result.error) return showToast(result.error, 'error');
    state.plans.push(result.plan);
    document.getElementById('releases-count').textContent = (state.releases ? state.releases.length : 0) + state.plans.length;
    showToast('修复计划已生成', 'success'); switchTab('releases');
  } catch (e) { showToast('生成失败：' + e, 'error'); }
}

async function applyPlan(planId) {
  showToast('正在应用并验证…');
  try {
    const result = await callApi('apply_plan', planId);
    if (result.error) return showToast(result.error, 'error');
    const plan = state.plans.find(item => item.plan_id === planId);
    if (plan) plan.change = result.change;
    showToast(result.change.status === 'verified' ? '变更已通过验证' : '变更未通过验证', result.change.status === 'verified' ? 'success' : 'error');
    state.report = await callApi('get_audit'); renderAll();
  } catch (e) { showToast('应用失败：' + e, 'error'); }
}

async function undoChange(changeId, planId) {
  showToast('正在撤销变更…');
  try {
    const result = await callApi('undo_change', changeId);
    if (result.error) return showToast(result.error, 'error');
    const plan = state.plans.find(item => item.plan_id === planId);
    if (plan) plan.change = null;
    state.report = await callApi('get_audit'); showToast('变更已撤销', 'success'); renderAll();
  } catch (e) { showToast('撤销失败：' + e, 'error'); }
}

// ===========================================================================
// v3 数据源 tab：SourceRoot 列表 + 原始记忆按 agent 分组 + 文件内容查看
// spec §7.1 / §7.2 SourceApi + get_raw_memory + get_source_file_content
// ===========================================================================

async function renderSources() {
  const renderToken = takeContentRenderToken('sources');
  setContent('<div class="loading">正在加载数据页</div>', renderToken);
  try {
    // v3.2：先加载 Agent 卡片
    agentCardsData = await callApi('list_agents');
    const agents = agentCardsData.agents || [];
    const availableAgents = [...agents, ...(agentCardsData.residuals || [])];
    // 默认选中第一个 Agent
    if (!activeAgentInstanceId && availableAgents.length > 0) {
      activeAgentInstanceId = availableAgents[0].instance_id;
    }
    // 加载选中 Agent 的数据 + 来源列表 + 覆盖率
    const [agentData, sourcesResult, rawResult, bindingsResult] = await Promise.all([
      activeAgentInstanceId ? callApi('get_agent_data', activeAgentInstanceId) : Promise.resolve(null),
      callApi('list_sources'),
      callApi('get_raw_memory'),
      callApi('list_bindings'),
    ]);
    renderSourcesView(sourcesResult, rawResult, agentData, bindingsResult, renderToken);
  } catch (e) {
    showToast('数据源加载失败：' + e, 'error');
    setContent(`<div class="view-heading"><span class="eyebrow">Sources</span><h2>数据源与代理</h2><p>数据源加载失败。</p></div><div class="card empty-state"><div><div class="empty-orb"></div><p>数据源加载失败：${escapeHtml(e)}</p></div></div>`, renderToken);
  }
}

async function selectAgentCard(instanceId) {
  try {
    const result = await waitForMutation(
      await callApi('set_governance_scope', {
        mode: 'agent',
        agent_instance_id: instanceId,
      }),
      '切换 Agent 治理范围',
    );
    if (!result || result.error || result.ok === false) {
      return showToast((result && result.error) || '治理范围保存失败', 'error');
    }
    activeAgentInstanceId = instanceId;
    activeScopeMemberIds = [instanceId];
    dataPageMode = 'single_agent';
    activeShareGroupId = '';
    if (state.activeTab === 'history') renderHistory();
    else renderSources();
  } catch (error) {
    showToast('切换 Agent 失败：' + error, 'error');
  }
}

function renderSourcesView(sourcesResult, rawResult, agentData, bindingsResult) {
  const sources = sourcesResult.sources || [];
  const cov = rawResult.coverage || {};
  document.getElementById('sources-count').textContent = sources.length || '';

  // v3.2 Agent 卡片
  const agents = (agentCardsData && agentCardsData.agents) || [];
  const activeBindings = ((bindingsResult && bindingsResult.bindings) || []).filter(b => b.status === 'active');
  const residuals = (agentCardsData && agentCardsData.residuals) || [];
  const discoveredCount = Array.isArray(discoveryResult?.instances) ? discoveryResult.instances.length : 0;
  const knownProfileCount = Number(agentCardsData?.known_profile_count || discoveryResult?.known_profile_count || 0);
  const lifecycleLabels = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '原生数据待接入', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
  const lifecycleChips = { installed: 'confirmed', installed_no_data: 'info', data_only: 'medium', uncertain: 'info', ignored: 'low', not_detected: 'low' };
  const lifecycleLabel = (item, binding = null) => {
    if (binding) return binding.group_kind === 'personal' ? '已启用个人层' : '已绑定共享组';
    const state = item?.lifecycle_state || 'uncertain';
    const discoveryOnly = Object.prototype.hasOwnProperty.call(item || {}, 'install_confidence')
      && finiteNumber(item.install_confidence, 0) <= 0
      && item.target_capability === 'export_only';
    return discoveryOnly ? '已发现 · 待接入' : (lifecycleLabels[state] || state);
  };
  const agentCardsHtml = agents.length ? agents.map(a => {
    const isActive = a.instance_id === activeAgentInstanceId;
    const lifecycle = a.lifecycle_state || 'uncertain';
    const summary = agentSummary(a);
    const binding = activeBindings.find(b => sameNonEmptyAgentId(b.agent_instance_id, a.instance_id));
    const kindLabel = binding ? (binding.group_kind === 'personal' ? '个人记忆层' : '共享记忆层') : '未绑定';
    const bindingAction = binding
      ? `<button class="btn" type="button" onclick="event.stopPropagation(); viewMemoryLayer('${escapeHtml(binding.share_group_id)}')">进入记忆层</button>
         <button class="btn" type="button" onclick="event.stopPropagation(); installMemoryGroupMcp('${escapeHtml(binding.share_group_id)}')">重新安装 MCP</button>
         ${binding.group_kind === 'shared' ? `<button class="btn btn-danger" type="button" onclick="event.stopPropagation(); leaveSharedToPersonal('${escapeHtml(a.instance_id)}')">退出共享组并回个人层</button>` : ''}`
      : `<button class="btn btn-primary" type="button" onclick="event.stopPropagation(); ensurePersonalLayer('${escapeHtml(a.instance_id)}')">启用个人记忆层</button>`;
    return `<div class="agent-card ${isActive ? 'active' : ''}" onclick="selectAgentCard('${escapeHtml(a.instance_id)}')">
       <div class="agent-name" title="${escapeHtml(agentIdentityDetail(a))}">${escapeHtml(agentDisplayName(a))}</div>
      <div class="agent-meta">${summary.foundSurfaceCount}/${summary.surfaceCount} 表面 · 私有 ${summary.privateSurfaceCount} · 共享 ${summary.sharedSurfaceCount} · ${summary.sourceCount} 来源</div>
      <div class="agent-badge">${escapeHtml(a.target_capability || 'export_only')}</div>
       <span class="chip chip-${binding ? 'confirmed' : (lifecycleChips[lifecycle] || 'info')}">${escapeHtml(lifecycleLabel(a, binding))}</span>
      <div class="surface-meta">${kindLabel}${binding ? ` · ${escapeHtml(binding.share_group_id)} · ${escapeHtml(binding.canonical_store_path || '')}` : ''}</div>
      ${binding && binding.migration_required ? '<div class="chip chip-medium">待迁移（仅提示）</div>' : ''}
      <div class="finding-actions">${bindingAction}</div>
    </div>`;
  }).join('') : `<div class="agent-card" style="cursor:default"><div class="agent-meta">${discoveredCount ? `当前没有可接入 Agent；已检测到 ${discoveredCount} 个候选，请从“本机 Agent 检测”中授权。` : '未发现可接入 Agent，点击“检测本机 Agent”'}</div></div>`;
  const residualCardsHtml = residuals.length ? residuals.map(r => {
    const lifecycle = r.lifecycle_state || 'uncertain';
    return `<div class="agent-card" onclick="showResidualCleanup('${escapeHtml(r.instance_id)}')">
       <div class="agent-name" title="${escapeHtml(agentIdentityDetail(r))}">${escapeHtml(agentDisplayName(r))}</div>
      <div class="agent-meta">原生数据 ${r.private_data_surface_count || 0} · 共享表面 ${r.shared_surface_count || 0}${r.control_repair_required ? ' · V2 绑定待修复/接入' : ''}</div>
      <span class="chip chip-${lifecycleChips[lifecycle] || 'medium'}">${escapeHtml(lifecycleLabel(r))}</span>
      <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="event.stopPropagation(); ensurePersonalLayer('${escapeHtml(r.instance_id)}')">启用个人记忆层</button></div>
    </div>`;
  }).join('') : '<div class="agent-card" style="cursor:default"><div class="agent-meta">无私有残留数据。</div></div>';
  const addCards = `<div class="agent-card add-card" onclick="addSourceDialog()"><div class="agent-name">+ 手动来源</div></div>
    <div class="agent-card add-card" onclick="importBundleDialog()"><div class="agent-name">+ 外部 MCP</div></div>`;

  const catLabels = {
    native_memory: '原生记忆', control_surface: '控制面', skill_surface: 'Skill 表面',
    conversation_history: '会话历史', runtime_evidence: '运行证据', knowledge_source: '知识来源',
    unknown: '其他', project_memory: '项目记忆',
  };
  const scopeLabels = {user: '全局/用户', project: '项目', unknown: '未归属'};
  const renderFileRow = (f) => {
    const canOpen = !!f.root_id && f.authorized !== false && f.read_status !== 'discovered';
    // JSON string keeps arbitrary local filenames out of executable HTML.
    const viewArgs = escapeHtml(JSON.stringify([String(f.root_id || ''), String(f.relative_path || '')]));
    const clickAttr = canOpen ? ` onclick="viewSourceFile(...${viewArgs})"` : '';
    const statusText = canOpen ? (f.read_status || '') : '仅发现，需先授权';
    const displayPath = guiPathText(f.relative_path || f.path || f.display_name, '未命名文件').replaceAll('\\', '/');
    return `<div class="raw-file-row"${clickAttr} style="${canOpen ? '' : 'cursor:default;opacity:.72'}">
      <span class="raw-file-path"><code>${escapeHtml(displayPath)}</code></span>
      <span class="chip chip-${canOpen && f.read_status === 'read' ? 'confirmed' : 'medium'}">${escapeHtml(statusText)}</span>
      <span style="color:var(--faint);font-size:10px">${escapeHtml(f.media_type || '')}</span>
    </div>`;
  };
  const buildFileTree = (files) => {
    const root = { dirs: new Map(), files: [] };
    for (const f of files || []) {
      const parts = guiPathText(f.relative_path || f.path || f.display_name, '未命名文件')
        .replaceAll('\\', '/').split('/').filter(Boolean);
      if (!parts.length) { root.files.push(f); continue; }
      let node = root;
      parts.slice(0, -1).forEach(part => {
        if (!node.dirs.has(part)) node.dirs.set(part, { name: part, dirs: new Map(), files: [] });
        node = node.dirs.get(part);
      });
      node.files.push(f);
    }
    return root;
  };
  const countTreeFiles = (node) => node.files.length + [...node.dirs.values()].reduce((sum, child) => sum + countTreeFiles(child), 0);
  const renderFileTree = (node, depth = 0) => {
    const dirHtml = [...node.dirs.values()].map(child => {
      const count = countTreeFiles(child);
      return `<details class="folder-group" ${depth === 0 ? 'open' : ''} style="--folder-depth:${Math.min(depth, 8)}">
        <summary class="folder-row"><span class="folder-caret" aria-hidden="true"></span><span class="folder-name">${escapeHtml(child.name)}</span><span class="folder-count">${count} 个文件</span></summary>
        <div class="folder-children">${renderFileTree(child, depth + 1)}</div>
      </details>`;
    }).join('');
    const rows = (node.files || []).map(renderFileRow).join('');
    return dirHtml + rows;
  };
  const renderFiles = (files) => `<div class="raw-file-list">${renderFileTree(buildFileTree(files))}</div>`;
  const knowledgeTypes = new Set(['selected_directory', 'selected_file', 'obsidian_vault']);
  const nonKnowledgeCategories = new Set([
    'native_memory', 'project_memory', 'control_surface', 'skill_surface',
    'conversation_history', 'runtime_evidence', 'ignored_runtime_data',
  ]);
  const knowledgeSources = sources.filter(s =>
    knowledgeTypes.has(s.type) && !nonKnowledgeCategories.has(s.source_category || 'unknown')
  );
  const rawGroupsByRoot = new Map(((rawResult && rawResult.groups) || []).map(g => [g.root_id, g]));
  const sourceTypeLabels = {
    selected_directory: '文件夹知识库',
    selected_file: '单文件知识库',
    obsidian_vault: 'Obsidian 知识库',
  };
  const knowledgeCardsHtml = knowledgeSources.length ? knowledgeSources.map(source => {
    const group = rawGroupsByRoot.get(source.root_id) || {};
    const files = (group.files || []).map(file => ({
      ...file,
      root_id: source.root_id,
      authorized: true,
    }));
    const visibleFiles = files.slice(0, 24);
    const remaining = Math.max(0, files.length - visibleFiles.length);
    const connected = source.path_exists !== false;
    return `<article class="knowledge-card ${connected ? '' : 'missing'}">
      <div class="finding-header">
        <div>
          <div class="knowledge-title">${escapeHtml(source.display_name || '未命名知识库')}</div>
          <div class="surface-meta">${escapeHtml(sourceTypeLabels[source.type] || '本地知识库')} · ${files.length} 个文件 · ${escapeHtml(scopeLabels[source.scope] || source.scope || '未归属')}</div>
        </div>
        <span class="chip chip-${connected ? 'confirmed' : 'high'}">${connected ? '已连接' : '路径失效'}</span>
      </div>
      <div class="knowledge-path" title="${escapeHtml(guiPathText(source.path, '受保护来源'))}"><code>${escapeHtml(guiPathText(source.path, '受保护来源'))}</code></div>
      <details class="knowledge-files">
        <summary>${files.length ? `查看已扫描文件（${files.length}）` : '暂无可读取文件'}</summary>
        ${files.length ? renderFiles(visibleFiles) : '<div class="surface-meta" style="margin-top:8px">目录为空，或没有符合扫描策略的文件。</div>'}
        ${remaining ? `<div class="surface-meta" style="margin-top:8px">另有 ${remaining} 个文件未在卡片中展开。</div>` : ''}
      </details>
      <div class="knowledge-actions">
        <button class="btn btn-danger" type="button"
          data-source-id="${escapeHtml(source.root_id)}"
          data-source-name="${escapeHtml(source.display_name || source.root_id)}"
          onclick="removeSourceCard(this)">删除映射</button>
      </div>
    </article>`;
  }).join('') : `<div class="empty-state"><div><div class="empty-orb"></div>
    <p>尚未添加本地知识库。添加文件夹后会在这里显示；原文件保持只读。</p></div></div>`;
  const knowledgeSection = `<section class="card">
    <div class="card-head"><div><h2>本地知识库</h2>
      <p>${knowledgeSources.length} 个来源 · 只读扫描；文档需萃取后才进入长期记忆</p></div>
      <button class="btn btn-primary" type="button" onclick="addSourceDialog()">+ 添加文件夹或文件</button></div>
    <div class="knowledge-grid">${knowledgeCardsHtml}</div>
  </section>`;
  const renderCategory = (cat) => {
    const label = catLabels[cat.category] || cat.category || 'unknown';
    const files = cat.files || [];
    return `<div style="margin-bottom:14px">
      <div class="finding-header"><span class="finding-rule">${escapeHtml(label)}</span>
        <span class="chip chip-info">${files.length} 个文件</span></div>
      ${renderFiles(files)}
    </div>`;
  };
  const renderScope = (scopeObj) => {
    const scope = scopeObj.scope || 'unknown';
    const label = scopeLabels[scope] || scope;
    const directCategories = (scopeObj.categories || []).map(renderCategory).join('');
    const projectHtml = (scopeObj.projects || []).map(proj => `<details class="scope-block" style="margin:12px 0 0 12px">
      <summary class="finding-header" style="cursor:pointer"><span class="finding-rule">${escapeHtml(label)} · ${escapeHtml(proj.project_ref || '未命名项目')}</span>
        <span class="chip chip-info">${escapeHtml(proj.scope_source || scopeObj.scope_source || '')}</span></summary>
      ${(proj.categories || []).map(renderCategory).join('') || '<div class="empty-state"><p>该项目下暂无可显示文件。</p></div>'}
    </details>`).join('');
    return `<details class="scope-block" style="margin-bottom:18px" ${scope === 'user' ? 'open' : ''}>
      <summary class="finding-header" style="cursor:pointer"><span class="finding-rule">${escapeHtml(label)}</span>
        <span class="chip chip-info">${escapeHtml(scopeObj.scope_source || '')}</span></summary>
      ${directCategories}${projectHtml || (!directCategories ? '<div class="empty-state"><p>该作用域暂无可显示文件。</p></div>' : '')}
    </details>`;
  };
  const agentDataHtml = agentData ? ((agentData.scopes || []).map(renderScope).join('') || '<div class="empty-state"><div class="empty-orb"></div><p>该 Agent 暂无已发现数据。</p></div>') : '<div class="empty-state"><div class="empty-orb"></div><p>选中一个 Agent 卡片查看其数据。</p></div>';

  const covCard = `<section class="card">
    <div class="card-head"><div><h2>覆盖率账本</h2><p>证明扫描完整性，unaccounted_count 必须为 0</p></div></div>
    <div class="chips">
      <span class="chip chip-info">candidates · ${cov.candidate_count || 0}</span>
      <span class="chip chip-confirmed">read · ${cov.read || 0}</span>
      <span class="chip chip-medium">unsupported · ${cov.unsupported || 0}</span>
      <span class="chip chip-high">unreadable · ${cov.unreadable || 0}</span>
      <span class="chip chip-medium">skipped · ${cov.skipped_by_policy || 0}</span>
      <span class="chip chip-${(cov.unaccounted_count || 0) === 0 ? 'confirmed' : 'high'}">unaccounted · ${cov.unaccounted_count || 0}</span>
      <span class="chip chip-${cov.coverage_status === 'complete' ? 'confirmed' : 'medium'}">${escapeHtml(cov.coverage_status || 'unknown')}</span>
    </div>
  </section>`;

  const agentInfo = agentData && agentData.agent ? agentData.agent : null;
  setContent(`<div class="view-heading"><span class="eyebrow">Sources</span><h2>数据源与代理</h2>
    <p>顶部选择 Agent，下方查看其数据。全局/项目可折叠展开。</p></div>
    <section class="card"><div class="card-head"><div><h2>Agent 摘要</h2>
       <p>${agents.length} 个已接入/可接入 · ${residuals.length} 个绑定待修复或接入${discoveredCount ? ` · 检测到 ${discoveredCount} 个候选` : ''}${knownProfileCount ? ` · 已注册 ${knownProfileCount} 个 Agent Profile` : ''} · 点击卡片切换数据视图</p></div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="discoverAgents()">检测本机 Agent</button>
        <button class="btn" type="button" onclick="enterMultiAgentMode()">管理已有记忆组</button>
        <button class="btn" type="button" onclick="addSourceDialog()">手工添加</button>
        <button class="btn" type="button" onclick="importBundleDialog()">导入导出包</button>
      </div></div>
      <div class="agent-cards">${agentCardsHtml}${addCards}</div></section>
    ${knowledgeSection}
    ${residuals.length ? `<details class="card" style="margin-bottom:16px">
      <summary class="card-head" style="cursor:pointer"><div><h2>未接入原生数据</h2>
        <p>${residuals.length} 个 Agent · 可恢复旧绑定、接入已有组或管理原生数据</p></div></summary>
      <div class="agent-cards" style="padding:16px">${residualCardsHtml}</div>
    </details>` : ''}
    <section class="card"><div class="card-head"><div><h2>${agentInfo ? escapeHtml(agentDisplayName(agentInfo)) + ' 数据视图' : 'Agent 数据视图'}</h2>
      <p>${agentData ? agentData.total_files + ' 个文件，' + agentData.category_count + ' 个类别' : '选中 Agent 后显示数据'}</p></div>
      ${agentInfo ? `<div class="finding-actions"><button class="btn" type="button" onclick="selectAgentInstance('${escapeHtml(agentInfo.instance_id)}')">勾选授权</button>
        <button class="btn btn-primary" type="button" onclick="enterMultiAgentMode()">进入多 Agent 共享 MCP 模式</button></div>` : ''}</div>
      ${agentDataHtml}</section>
    ${covCard}`);
}

async function enterMultiAgentMode() {
  dataPageMode = 'multi_agent_shared_mcp';
  showToast('进入多 Agent 共享 MCP 模式，请选择 Agent');
  await renderMultiAgentBinding();
}

async function renderMultiAgentBinding() {
  setContent('<div class="loading">正在加载 Agent 列表与已有绑定…</div>');
  try {
    const [agentsResult, bindingsResult, hooksResult, groupsResult] = await Promise.all([
      callApi('list_agents'),
      callApi('list_bindings'),
      callApi('get_host_hook_status'),
      callApi('list_share_groups'),
    ]);
    showMultiAgentBinding(agentsResult, bindingsResult, hooksResult, groupsResult);
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(e)}</p></div></div>`);
  }
}

function showMultiAgentBinding(agentsResult, bindingsResult, hooksResult, groupsResult) {
  agentCardsData = agentsResult || agentCardsData;
  const discoveredAgents = (agentsResult && agentsResult.agents) || [];
  const residualAgents = (agentsResult && agentsResult.residuals) || [];
  const agents = Array.from(new Map([...discoveredAgents, ...residualAgents].map(item => [item.instance_id, item])).values());
  const existingBindings = (bindingsResult && bindingsResult.bindings) || [];
  const hookAgents = (hooksResult && hooksResult.agents) || [];
  const existingGroups = (groupsResult && groupsResult.groups) || [];
  if (!agents.length) {
    setContent(`<div class="view-heading"><span class="eyebrow">Multi-agent</span><h2>多 Agent 共享 MCP 模式</h2></div>
      <div class="card empty-state"><div><div class="empty-orb"></div><p>未发现 Agent。请先在数据源 tab 检测本机 Agent 或手工添加来源。</p></div></div>
      <div class="finding-actions"><button class="btn" type="button" onclick="renderSources()">← 返回数据源</button></div>`);
    return;
  }
  // 已有 active binding 的 agent 默认勾选
  const boundAgentSet = new Set(
    existingBindings
      .filter(b => b.status === 'active')
      .map(b => safeDisplayText(b.agent_instance_id, '').trim())
      .filter(Boolean)
  );
  const agentRowsHtml = agents.map(a => {
    const agentId = safeDisplayText(a.instance_id, '').trim();
    const bound = Boolean(agentId && boundAgentSet.has(agentId));
    const summary = agentSummary(a);
    return `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto;align-items:center">
      <input type="checkbox" data-agent-id="${escapeHtml(a.instance_id)}" ${bound ? 'checked' : ''}>
      <div>
         <div class="finding-rule">${escapeHtml(agentDisplayName(a))}</div>
        <div class="surface-meta">${summary.foundSurfaceCount}/${summary.surfaceCount} 表面 · ${summary.sourceCount} 来源</div>
      </div>
      ${bound ? '<span class="chip chip-confirmed">已绑定</span>' : '<span class="chip chip-info">未绑定</span>'}
    </label>`;
  }).join('');

  const activeBindings = existingBindings.filter(b => b.status === 'active');
  const groupById = new Map(existingGroups
    .filter(g => String(g.share_group_id || g.group_id || '').trim())
    .map(g => [String(g.share_group_id || g.group_id), g]));
  // Keep groups that are present only in the memory database as selectable
  // targets too.  A group with zero current members is still a valid,
  // recoverable memory layer from an earlier binding/migration.
  activeBindings.forEach(b => {
    const gid = String(b.share_group_id || '').trim();
    if (gid && !groupById.has(gid)) groupById.set(gid, {
      share_group_id: gid,
      group_id: gid,
      group_kind: b.group_kind || 'shared',
      members: [b.agent_instance_id],
      member_count: 1,
      active_records: 0,
    });
  });
  const selectableGroups = [...groupById.values()]
    .filter(g => String(g.share_group_id || g.group_id || '').trim())
    .sort((left, right) => String(left.share_group_id || left.group_id).localeCompare(String(right.share_group_id || right.group_id)));
  const agentNameById = new Map(agents.map(a => [a.instance_id, agentDisplayName(a)]));
  const groupOptionLabel = (group) => {
    const kind = group.group_kind === 'personal' ? '个人' : '共享';
    const summary = governanceGroupProgramSummary(group);
    const memberNames = summary.names.length
      ? summary.names
      : [...new Set((group.members || []).map(id => agentNameById.get(id) || agentDisplayName(id)))].filter(name => name !== '未识别的 MCP 助手');
    const memberCount = summary.programCount;
    const connectionCount = summary.connectionCount;
    const recordCount = Number(group.active_records || group.active_count || 0);
    const unknown = summary.unknownCount ? `（${summary.unknownCount} 待识别连接）` : '';
    return `${kind} · ${memberNames.join('、') || '程序名称待加载'} · ${memberCount} 个程序 · ${connectionCount} 条连接（其他 ${summary.otherCount}）${unknown} · ${recordCount} 条记忆`;
  };
  const existingGroupSelector = (agentId, selectedGroupId = '') => {
    if (!selectableGroups.length) return '';
    const options = selectableGroups.map(group => {
      const gid = String(group.share_group_id || group.group_id || '');
      return `<option value="${escapeHtml(gid)}" ${gid === selectedGroupId ? 'selected' : ''}>${escapeHtml(groupOptionLabel(group))}</option>`;
    }).join('');
    return `<label class="surface-meta" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span>绑定已有记忆组</span>
      <select class="scope-select" data-existing-group-agent="${escapeHtml(agentId)}" aria-label="为 ${escapeHtml(agentDisplayName(agentId))} 选择已有记忆组">${options}</select>
      <button class="btn" type="button" onclick="bindSelectedExistingGroup('${escapeHtml(agentId)}')">应用</button>
    </label>`;
  };
  const personalLayerHtml = agents.map(a => {
    const b = activeBindings.find(x => sameNonEmptyAgentId(x.agent_instance_id, a.instance_id));
    const hook = hookAgents.find(x => sameNonEmptyAgentId(x.agent_instance_id, a.instance_id));
    const label = b ? (b.group_kind === 'personal' ? '个人记忆层' : '共享记忆层') : '未启用个人记忆层';
    const action = b
      ? (b.group_kind === 'shared' ? '' : `<button class="btn" type="button" onclick="ensurePersonalLayer('${escapeHtml(a.instance_id)}')">保持个人层</button>`)
      : `<button class="btn btn-primary" type="button" onclick="ensurePersonalLayer('${escapeHtml(a.instance_id)}')">启用个人记忆层</button>`;
    const groupAction = existingGroupSelector(a.instance_id, b ? b.share_group_id : '');
    const viewAction = b ? `<button class="btn" type="button" onclick="viewMemoryLayer('${escapeHtml(b.share_group_id)}')">进入记忆层</button>` : '';
    const hookStatus = hook ? (hook.runtime_verified ? '运行已验证' : (hook.configured ? '已配置待运行' : (hook.supported === false ? '宿主无 Hook' : '未配置'))) : '未配置';
    const hookChip = hook && hook.runtime_verified ? 'confirmed' : (hook && hook.configured ? 'medium' : 'info');
    const hookActions = hook && hook.supported !== false
      ? `<button class="btn" type="button" onclick="setHostHookMode('${escapeHtml(hook.provider)}','${escapeHtml(a.instance_id)}','enforce')">强制</button>
         <button class="btn" type="button" onclick="setHostHookMode('${escapeHtml(hook.provider)}','${escapeHtml(a.instance_id)}','paused')">暂停 Hook</button>
         <button class="btn btn-danger" type="button" onclick="uninstallHostHook('${escapeHtml(hook.provider)}')">卸载 Hook</button>`
      : '';
    return `<article class="plan-item"><div class="finding-header"><span class="finding-rule">${escapeHtml(agentDisplayName(a))}</span><span class="chip chip-info">${label}</span><span class="chip chip-${hookChip}">${hookStatus}</span></div>
      <div class="row"><span class="key">group</span><code>${escapeHtml(b ? b.share_group_id : '未绑定')}</code></div>
      <div class="row"><span class="key">canonical DB</span><span>${escapeHtml(b ? (b.canonical_store_path || '') : '—')}</span></div>
      <div class="row"><span class="key">Hook</span><span>${escapeHtml(hookStatus)}${hook && hook.mode ? ` · ${escapeHtml(hook.mode)}` : ''}</span></div>
      <div class="row"><span class="key">last receipt</span><span>${escapeHtml((hook && hook.last_seen_at) || '—')}</span></div>
      <div class="finding-actions">${viewAction}${action}${hookActions}${b && b.group_kind === 'shared' ? `<button class="btn btn-danger" type="button" onclick="leaveSharedToPersonal('${escapeHtml(a.instance_id)}')">退出共享组并回个人层</button>` : ''}</div>
      ${groupAction ? `<div style="margin-top:10px">${groupAction}</div>` : ''}</article>`;
  }).join('');

  // 已有记忆组分组展示：同时保留个人组、空共享组和只有数据的旧组。
  const groupMap = new Map();
  existingBindings.forEach(b => {
    if (b.status !== 'active') return;
    const gid = String(b.share_group_id || '').trim();
    if (!gid) return;
    if (!groupMap.has(gid)) groupMap.set(gid, []);
    groupMap.get(gid).push(b);
  });
  selectableGroups.forEach(group => {
    const gid = String(group.share_group_id || group.group_id || '').trim();
    if (!groupMap.has(gid)) groupMap.set(gid, []);
  });
  const groupsHtml = groupMap.size ? Array.from(groupMap.entries()).map(([gid, binds]) => {
    const group = groupById.get(gid) || {};
    const memberIds = new Set([...(Array.isArray(group.members) ? group.members : []), ...binds.map(b => b.agent_instance_id)]);
    const memberNames = [...memberIds].map(id => escapeHtml(agentNameById.get(id) || id)).join(' · ');
    const kind = group.group_kind || binds[0]?.group_kind || (gid.startsWith('personal-') ? 'personal' : 'shared');
    const recordCount = Number(group.active_records || group.active_count || 0);
    return `<article class="plan-item verified">
    <div class="finding-header">
      <span class="finding-rule">${kind === 'personal' ? '个人记忆组' : '共享记忆组'} ${escapeHtml(gid.slice(0, 20))}</span>
      <span class="chip chip-confirmed">${memberIds.size} 个 Agent</span>
      <span class="chip chip-info">${recordCount} active</span>
    </div>
    <div class="finding-evidence">${memberNames || '当前没有绑定 Agent；仍可重新接入'}</div>
    <div class="finding-actions">
      <button class="btn" type="button" onclick="activateShareGroup('${escapeHtml(gid)}')">设为治理范围</button>
      <button class="btn" type="button" onclick="viewMemoryLayer('${escapeHtml(gid)}')">进入记忆层</button>
      ${kind === 'shared' ? `<button class="btn" type="button" onclick="previewSharedGroup('${escapeHtml(gid)}')">查看共享组预览</button>
      <button class="btn btn-danger" type="button" onclick="dissolveSharedGroup('${escapeHtml(gid)}')">解散共享组</button>` : ''}
    </div>
  </article>`;
  }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>暂无个人或共享记忆组。可先启用个人层，或勾选多个 Agent 创建共享组。</p></div>';

  setContent(`<div class="view-heading"><span class="eyebrow">Multi-agent shared MCP</span><h2>多 Agent 共享 MCP 模式</h2>
    <p>勾选多个 Agent，创建共享组绑定，所有 Agent 通过 MemoryGuard MCP 共享同一组记忆。</p></div>
    <section class="card"><div class="card-head"><div><h2>Agent 列表</h2>
      <p>勾选要加入共享组的 Agent。已有绑定的 Agent 默认勾选。</p></div></div>
      <div class="raw-file-list">${agentRowsHtml}</div>
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="createSharedBinding()">创建共享组绑定</button>
        <button class="btn" type="button" onclick="exitMultiAgentMode()">退出多 Agent 模式</button>
      </div>
    </section>
    <section class="card"><div class="card-head"><div><h2>个人记忆层</h2><p>个人层与共享层都使用 MemoryGuard SharedMemoryStore；原生文件仅只读扫描源。</p></div></div>${personalLayerHtml}</section>
    <section class="card"><div class="card-head"><div><h2>已有记忆组</h2><p>未绑定 Agent 也可以在上方选择已有个人组或共享组；不会强制新建个人层。</p></div></div>
      ${groupsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
      </div>
    </section>`);
}

function historyScope() {
  if (isShareGroupScope()) {
    return {mode: 'share_group', share_group_id: activeShareGroupId};
  }
  if (activeAgentInstanceId) {
    return {mode: 'agent', agent_instance_id: activeAgentInstanceId};
  }
  return {};
}

function renderHistoryScopeGate() {
  return `<section class="card empty-state">
    <div>
      <div class="empty-orb"></div>
      <h2>需要有效治理范围</h2>
      <p>对话历史只允许读取当前 Agent 的 active binding。请先在数据源页选择已绑定的 Agent 或共享组。</p>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="switchTab('sources')">去数据源选择</button>
      </div>
    </div>
  </section>`;
}

let ruleScopeOptions = null;
let rulePreviewAgentId = '';
let rulePreviewProjectRef = '';
let rulePreviewProvider = '';
let rulePreviewRuntimeRole = '';
let ruleRangeFilter = 'all';
let ruleVisibilityFilter = 'all';
let ruleRecordsById = new Map();
let rulePreviewById = new Map();
let ruleDecisionRows = [];
let ruleScopeMetrics = {};
let ruleReceiptRows = [];
let ruleExceptionRows = [];
let ruleCreateResult = null;

function rulePageBucket(record) {
  const kind = String(record?.rule_kind || record?.kind || '').toLowerCase();
  const strength = String(record?.rule_strength || '').toLowerCase();
  const polarity = String(record?.polarity || '').toLowerCase();
  if (['must', 'mandatory', 'required'].includes(strength)) return 'mandatory';
  if (kind === 'preference') return 'preferences';
  if (['procedure', 'workflow', 'instruction'].includes(kind)) return 'procedures';
  if (['correction', 'constraint'].includes(kind) || polarity === 'negative') return 'corrections';
  if (['project', 'decision'].includes(kind)) return 'projects';
  return 'preferences';
}

function normalizeRulePageRecord(record) {
  const source = record && typeof record === 'object' ? record : {};
  const bindings = Array.isArray(source.bindings) ? source.bindings : [];
  const assignments = bindings.map(binding => ({
    assignment_id: binding.binding_id || '',
    target_type: binding.target_type || '',
    target_id: binding.target_id || '',
    project_ref: binding.project_ref || '',
    provider: binding.provider || '',
    runtime_role: binding.runtime_role || '',
    effect: binding.effect || 'include',
    priority_override: Number.isFinite(Number(binding.priority)) ? Number(binding.priority) : null,
  }));
  const body = String(source.body || source.canonical_text || '').trim();
  const memoryId = String(source.memory_id || source.definition_id || '').trim();
  const priorities = bindings.map(item => Number(item.priority)).filter(Number.isFinite);
  return {
    ...source,
    memory_id: memoryId,
    rule_id: source.definition_id || memoryId,
    body,
    title: source.title || (body ? body.split(/\r?\n/, 1)[0].slice(0, 80) : memoryId),
    kind: source.rule_kind || source.kind || 'procedure',
    status: source.excluded ? 'excluded' : 'active',
    injection_policy: bindings.length ? 'always' : 'relevant',
    priority: priorities.length ? Math.max(...priorities) : 0,
    assignments,
  };
}

function rulePageBuckets(data) {
  if (data?.buckets && typeof data.buckets === 'object') return data.buckets;
  const buckets = {mandatory: [], preferences: [], procedures: [], corrections: [], projects: []};
  for (const raw of (Array.isArray(data?.rules) ? data.rules : [])) {
    const record = normalizeRulePageRecord(raw);
    buckets[rulePageBucket(record)].push(record);
  }
  return buckets;
}

function ruleAudience(record) {
  const items = record.assignments || [];
  if (!items.length) return record.audience_label || '当前上下文（按需召回）';
  const includes = items.filter(item => item.effect !== 'exclude');
  const excludes = items.filter(item => item.effect === 'exclude');
  const projectName = value => String(value || '').replace(/\\/g, '/').replace(/\/+$/, '').split('/').filter(Boolean).pop() || '';
  const humanTarget = assignment => {
    const type = String(assignment.target_type || '').toLowerCase();
    const targetId = String(assignment.target_id || '');
    const projectRef = String(assignment.project_ref || '');
    if (type === 'agent') return targetId === activeAgentInstanceId ? `当前 ${agentDisplayName(targetId)}` : agentDisplayName(targetId, '已连接助手');
    if (type === 'group') {
      const count = activeScopeMemberIds.length || Number(assignment.member_count || 0) || Number(ruleScopeOptions?.connected_agent_count || 0);
      return targetId === activeShareGroupId && count ? `${count} 个已连接助手` : '已连接助手组';
    }
    if (type === 'project') return projectRef === ruleScopeOptions?.current_project_ref || projectRef === state.report?.workspace ? '当前项目' : `项目 · ${projectName(projectRef) || '已确认项目'}`;
    if (type === 'agent_project') {
      const agent = targetId === activeAgentInstanceId ? `当前 ${agentDisplayName(targetId)}` : agentDisplayName(targetId, '已连接助手');
      return projectRef === ruleScopeOptions?.current_project_ref ? `${agent} · 当前项目` : `${agent} · ${projectName(projectRef) || '已确认项目'}`;
    }
    if (type === 'provider') return `宿主 · ${targetId || '已确认宿主'}`;
    if (type === 'runtime_role') return `运行角色 · ${targetId || '已确认角色'}`;
    if (type === 'system') return '所有可信助手';
    return assignment.target_id || assignment.project_ref ? '已确认范围' : '当前上下文';
  };
  const summary = includes.length === 1
    ? humanTarget(includes[0])
    : includes.length > 1 ? `${includes.length} 个适用范围` : '当前上下文';
  return excludes.length ? `${summary} · ${excludes.length} 个例外` : summary;
}

function ruleSelectOptions(items, selected = '') {
  return (items || []).map(item => `<option value="${escapeHtml(item.id)}" ${item.id === selected ? 'selected' : ''}>${escapeHtml(item.label || item.id)}</option>`).join('');
}

function rulePreviewState(record) {
  return rulePreviewById.get(record.memory_id) || 'unavailable';
}

function ruleDecisionFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return ruleDecisionRows.find(item =>
    String(item.rule_id || item.memory_id || item.target_rule_id || '') === String(id)
  ) || null;
}

function ruleStatsFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return (ruleScopeMetrics.stats || []).find(item =>
    String(item.rule_id || item.memory_id || '') === String(id)
  ) || null;
}

function ruleReceiptsFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return ruleReceiptRows.filter(item => String(item.memory_id || '') === String(id));
}

function ruleExceptionsFor(record) {
  const id = record?.memory_id || record?.rule_id || '';
  return ruleExceptionRows.filter(item =>
    String(item.parent_rule || item.parent_rule_id || '') === String(id)
  );
}

function ruleConfidenceLabel(value) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  const pct = Math.round(Math.max(0, Math.min(1, n)) * 100);
  const cls = pct >= 80 ? 'chip-confirmed' : pct >= 50 ? 'chip-medium' : 'chip-high';
  return `<span class="chip ${cls}" title="自动范围置信度">范围置信度 ${pct}%</span>`;
}

function renderRuleReceiptActions(receipt) {
  const receiptId = receipt?.receipt_id || '';
  if (!receiptId) return '';
  const feedback = receipt.feedback || {};
  const outcome = feedback.outcome || '';
  const outcomes = [
    ['followed', '已遵循'], ['violated', '已违反'], ['not_applicable', '不适用'],
    ['corrected', '纠正'], ['exception', '例外'],
  ];
  return `<div class="rule-receipt"><span class="chip chip-info">命中回执 ${escapeHtml(receiptId.slice(0, 12))}</span>
    ${outcome ? `<span class="chip chip-confirmed">反馈：${escapeHtml(outcome)}</span>` : '<span class="muted">尚无反馈</span>'}
    <div class="rule-feedback-actions">${outcomes.map(([value, label]) =>
      `<button class="btn btn-icon" type="button" title="${label}" onclick="submitRuleFeedback('${escapeHtml(receiptId)}','${value}')">${label}</button>`
    ).join('')}</div></div>`;
}

function ruleDecisionScopeLabel(item) {
  const value = String(item?.scope_type || item?.target_type || item?.scope || 'agent').trim().toLowerCase();
  return ({
    agent: '当前 Agent', current_agent: '当前 Agent',
    project: '当前 Agent + 项目', current_project: '当前 Agent + 项目',
    share_group: '共享组', shared: '共享组', system: '系统',
  })[value] || value || '当前 Agent';
}

function humanizeUiKey(value, fallback = '未知') {
  const raw = String(value || '').trim();
  if (!raw) return fallback;
  const words = raw.replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/[_.:/-]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!words) return fallback;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function ruleDecisionActionLabel(value) {
  const key = String(value || '').trim().toLowerCase() || 'auto_scope';
  const labels = {
    delete: '删除/移除',
    rule_create_auto: '自动创建规则',
    restore: '恢复',
    auto_write: '自动写入',
    rule_superseded: '规则已被更新替代',
    rule_undo: '撤销规则操作',
    classify_override: '调整分类',
    auto_scope: '自动判定范围',
  };
  return labels[key] || `未知操作（${humanizeUiKey(key)}）`;
}

function groupRuleDecisions(decisions) {
  const groups = new Map();
  (decisions || []).forEach(item => {
    const action = String(item?.action || 'auto_scope').trim().toLowerCase() || 'auto_scope';
    if (!groups.has(action)) groups.set(action, {action: ruleDecisionActionLabel(action), items: [], scopes: new Map(), confidences: [], latest: ''});
    const group = groups.get(action);
    group.items.push(item);
    const scopeLabel = ruleDecisionScopeLabel(item);
    if (!group.scopes.has(scopeLabel)) group.scopes.set(scopeLabel, []);
    group.scopes.get(scopeLabel).push(item);
    const confidence = Number(item?.scope_confidence ?? item?.confidence);
    if (Number.isFinite(confidence)) group.confidences.push(Math.max(0, Math.min(1, confidence)));
    group.latest = [group.latest, String(item?.created_at || '')].sort().at(-1) || '';
  });
  return [...groups.values()].map(group => ({
    ...group,
    count: group.items.length,
    confidence: group.confidences.length
      ? group.confidences.reduce((sum, value) => sum + value, 0) / group.confidences.length
      : null,
  })).sort((a, b) => b.count - a.count || String(b.latest).localeCompare(String(a.latest)) || a.action.localeCompare(b.action));
}

function ruleDecisionCanUndo(item) {
  const id = item?.decision_id || item?.event_id || '';
  return !!(item?.undo_id || id) && item?.status !== 'undone' && !item?.undone;
}

function renderRuleDecisionRow(item) {
  const id = item.decision_id || item.event_id || '';
  const confidence = item.scope_confidence ?? item.confidence;
  const canUndo = ruleDecisionCanUndo(item);
  const objectType = String(item.object_type || 'rule');
  const actionCode = String(item.action || 'auto_scope').trim().toLowerCase() || 'auto_scope';
  return `<div class="rule-decision-row"><div><span class="chip chip-info">${escapeHtml(objectType)}</span>
    <span class="chip chip-info">${escapeHtml((item.rule_id || item.memory_id || '').slice(0, 14))}</span>
    ${ruleConfidenceLabel(confidence)}</div>
    <div class="muted">${escapeHtml(item.scope_reason || item.reason || '未提供原因')} · ${escapeHtml(item.created_at || '')}</div>
    <div class="finding-actions"><code>${escapeHtml(id)}</code>${canUndo ? `<button class="btn btn-danger btn-icon" type="button" onclick="undoRuleDecision('${escapeHtml(id)}')">撤销自动决定</button>` : '<span class="chip chip-medium">已撤销</span>'}</div>
    <details class="rule-advanced"><summary>查看内部详情</summary><div class="rule-advanced-body"><div class="row"><span class="key">action code</span><code>${escapeHtml(actionCode)}</code></div><div class="row"><span class="key">rule_id</span><code>${escapeHtml(item.rule_id || item.memory_id || '')}</code></div></div></details></div>`;
}

function renderRuleDecisionGroup(group, total) {
  const undoableCount = group.items.filter(ruleDecisionCanUndo).length;
  const scopes = [...group.scopes.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  const scopeSummary = scopes.length === 1 ? scopes[0][0] : `${scopes.length} 个范围`;
  const scopeGroups = scopes.map(([scopeLabel, items]) => `<details class="rule-decision-subgroup">
    <summary>${escapeHtml(scopeLabel)} · ${items.length} 条</summary>
    <div class="rule-decision-subgroup-body">${items.map(renderRuleDecisionRow).join('')}</div>
  </details>`).join('');
  return `<details class="rule-decision-group">
    <summary class="rule-decision-group-head"><span class="rule-decision-group-title"><strong>${escapeHtml(group.action)}</strong><span class="chip chip-info">${group.count} 条</span>${ruleConfidenceLabel(group.confidence)}</span>
      <span class="rule-decision-group-meta"><span>${escapeHtml(scopeSummary)}</span><span>${escapeHtml(group.latest || '暂无时间')}</span></span></summary>
    <div class="rule-decision-group-body">${undoableCount ? `<div class="finding-actions rule-decision-bulk"><button class="btn btn-danger btn-icon" type="button" onclick="undoRuleDecisionGroup('${encodeURIComponent(group.action)}')">批量撤销 ${undoableCount} 条</button></div>` : ''}${scopeGroups}</div>
  </details>`;
}

function renderRuleAutoScopePanel() {
  const metrics = ruleScopeMetrics.auto_scope || ruleScopeMetrics.metrics || {};
  const decisions = ruleDecisionRows || [];
  const total = Number(metrics.total ?? metrics.decisions ?? metrics.assignment_count ?? decisions.length ?? 0);
  const low = Number(metrics.low_confidence ?? metrics.low_confidence_count ?? decisions.filter(d => Number(d.scope_confidence ?? d.confidence) < .5).length ?? 0);
  const narrowed = Number(metrics.narrowed ?? metrics.narrowed_count ?? 0);
  const undone = Number(metrics.undone ?? metrics.undone_count ?? decisions.filter(d => d.status === 'undone' || d.undone).length ?? 0);
  const groups = groupRuleDecisions(decisions);
  const rows = groups.map(group => renderRuleDecisionGroup(group, decisions.length)).join('');
  return `<section class="card rule-cockpit-panel"><div class="card-head"><div><h2>自动范围决策</h2><p>按动作聚合并按范围二级折叠；所有组默认收起，展开后再查看详情。</p></div>
    <div class="chips"><span class="chip chip-info">自动决定 ${total}</span><span class="chip ${low ? 'chip-high' : 'chip-confirmed'}">低置信度 ${low}</span><span class="chip chip-info">已收窄 ${narrowed}</span><span class="chip chip-info">已撤销 ${undone}</span></div></div>
    <div class="rule-decision-groups">${rows || '<p class="muted">暂无自动范围决定。</p>'}</div></section>`;
}

function renderRuleCreatePanel(options) {
  const agents = options.agents || [];
  const groups = options.groups || [];
  // Preview controls are diagnostic-only.  Never copy preview Agent/project
  // into the creation context or its read-only display.
  const selectedAgent = activeAgentInstanceId || '';
  const selectedAgentLabel = agentDisplayName(agents.find(item => item.id === selectedAgent) || selectedAgent, '当前 Agent');
  const selectedGroupId = activeShareGroupId || 'default';
  const selectedGroupLabel = groups.find(item => item.id === selectedGroupId)?.label || groups.find(item => item.id === selectedGroupId)?.name || selectedGroupId;
  const result = ruleCreateResult;
  const resultHtml = result ? `<div class="rule-create-result ${result.error ? 'error' : ''}">
    ${result.error ? `<strong>未创建：${escapeHtml(result.error)}</strong>` : `<strong>已创建规则 ${escapeHtml(result.rule_id || result.memory_id || '')}</strong>
      <div class="chips">${result.kind ? `<span class="chip chip-info">分类 ${escapeHtml(result.kind)}</span>` : ''}${ruleConfidenceLabel(result.scope_confidence)}${result.decision_id ? `<span class="chip chip-info">决定 ${escapeHtml(result.decision_id.slice(0, 14))}</span>` : ''}</div>
      <p>${escapeHtml(result.scope_reason || result.blocked_reason || '范围由当前上下文确定')}</p>`}
  </div>` : '';
  // v2: 日常新增规则零额外选择。  Agent、共享组与项目是只读上下文标签，
  // 不作为可选项暴露给用户；服务端只读取当前可信上下文自动定范围。
  return `<section class="card rule-create-panel"><div class="card-head"><div><h2>一句话新增规则</h2><p>规则范围由当前可信上下文自动判断，无需手动选择。系统范围与跨 Agent 范围不会由自动流程创建。</p></div></div>
    <textarea id="rule-create-text" rows="3" maxlength="12000" placeholder="例如：所有 Unity UI 修复先补 EditMode 回归测试"></textarea>
    <div class="rule-context-readonly"><span class="chip chip-info">当前上下文：${escapeHtml(selectedAgentLabel)} · ${escapeHtml(selectedGroupLabel)}</span></div>
    <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="createRuleFromText()">分析并创建</button><span class="muted">系统只自动写入当前 Agent / 当前 Agent + 项目范围，绝不扩大。</span></div>${resultHtml}</section>`;
}

async function createRuleFromText() {
  const text = String(document.getElementById('rule-create-text')?.value || '').trim();
  if (!text) return showToast('请输入规则正文。', 'error');
  ruleCreateResult = null;
  try {
    // Creation intentionally sends text only.  The backend derives Agent and
    // project from its trusted runtime context; diagnostic preview state never
    // crosses this boundary.
    const result = await callApi('create_rule_from_text', text);
    ruleCreateResult = result || {error: 'empty_service_response'};
    if (result?.error || result?.ok === false) showToast(result.error || '规则未创建', 'error');
    else showToast('规则已创建，正在刷新范围与回执。', 'success');
    await renderRulesHabits();
  } catch (error) {
    ruleCreateResult = {error: error.message || String(error)};
    showToast(`规则创建失败：${error.message || error}`, 'error');
    await renderRulesHabits();
  }
}

async function requestRuleDecisionUndo(decisionId) {
  const id = String(decisionId || '').trim();
  if (!id) throw new Error('decision_id_required');
  // Identity/scope comes only from the trusted native bridge.  Do not put
  // Agent/group authority-shaped fields into browser metadata: SafeBridge
  // correctly rejects those before dispatch.
  const result = await callApi('undo_rule_decision', id, activeShareGroupId || 'default', true);
  if (result?.error || result?.ok === false) throw new Error(result.error || result.code || '撤销失败');
  return result;
}

async function undoRuleDecision(decisionId) {
  if (!decisionId || !confirm('确认撤销这条自动范围决定？')) return;
  try {
    await requestRuleDecisionUndo(decisionId);
    showToast('自动范围决定已撤销。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`撤销失败：${error.message || error}`, 'error'); }
}

async function undoRuleDecisionGroup(encodedAction) {
  const action = decodeURIComponent(String(encodedAction || ''));
  const ids = [...new Set((ruleDecisionRows || [])
    .filter(item => ruleDecisionActionLabel(item?.action) === action && ruleDecisionCanUndo(item))
    .map(item => item.decision_id || item.event_id || '')
    .filter(Boolean))];
  if (!ids.length) return showToast('这一组没有可撤销的自动决定。', 'info');
  if (!confirm(`确认批量撤销 ${action} 组中的 ${ids.length} 条自动范围决定？`)) return;
  let failed = 0;
  for (const decisionId of ids) {
    try { await requestRuleDecisionUndo(decisionId); }
    catch (_) { failed += 1; }
  }
  if (failed) showToast(`批量撤销完成：成功 ${ids.length - failed} 条，失败 ${failed} 条。`, 'error');
  else showToast(`已批量撤销 ${ids.length} 条自动范围决定。`, 'success');
  await renderRulesHabits();
}

async function submitRuleFeedback(receiptId, outcome) {
  if (!receiptId || !outcome) return;
  if (outcome === 'exception') return openRuleExceptionFeedbackModal(receiptId);
  // Evidence is optional; keep the one-click feedback action free of native
  // prompt dialogs so the desktop and localhost surfaces behave identically.
  const evidence = '';
  try {
    // Backend fixes producer/source=user and actor=user; no Agent identity
    // comes from diagnostic UI state.
    const result = await callApi('submit_rule_feedback', receiptId, outcome, '', evidence, activeShareGroupId || 'default', 1.0);
    if (result?.error || result?.ok === false) throw new Error(result.error || '反馈提交失败');
    showToast('反馈已记录。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`反馈提交失败：${error.message || error}`, 'error'); }
}

function openRuleExceptionFeedbackModal(receiptId) {
  if (!receiptId) return;
  document.getElementById('rule-exception-feedback-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'rule-exception-feedback-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="提交规则例外">
    <div class="modal-head"><h3>记录规则例外</h3><p>当前项目应遵循什么替代规则？只填写替代规则正文，提交后由治理层创建可撤销例外。</p></div>
    <div class="modal-body"><label class="field"><span>替代规则正文</span><textarea id="rule-exception-override" rows="5" maxlength="6000" placeholder="例如：仅在本项目的测试目录中允许……"></textarea></label><div id="rule-exception-feedback-error" class="feedback-error" role="alert" aria-live="polite"></div></div>
    <div class="modal-actions"><button class="btn" type="button" onclick="document.getElementById('rule-exception-feedback-modal')?.remove()">取消</button><button class="btn btn-primary" type="button" onclick="submitRuleExceptionFeedback('${escapeHtml(receiptId)}')">提交例外</button></div>
  </div>`;
  modal.addEventListener('click', event => { if (event.target === modal) modal.remove(); });
  document.body.appendChild(modal);
  document.getElementById('rule-exception-override')?.focus?.();
}

async function submitRuleExceptionFeedback(receiptId) {
  const input = document.getElementById('rule-exception-override');
  const errorNode = document.getElementById('rule-exception-feedback-error');
  const override = String(input?.value || '').trim();
  if (!override) {
    if (errorNode) errorNode.textContent = '请填写替代规则正文。';
    showToast('替代规则正文不能为空。', 'error');
    return;
  }
  try {
    // The GUI bridge fixes producer=user and validates receipt ownership.  The
    // override is passed as evidence because the service treats it as the
    // atomic child-rule body for an exception outcome.
    const result = await callApi('submit_rule_feedback', receiptId, 'exception', '', override, activeShareGroupId || 'default', 1.0);
    if (result?.error || result?.ok === false) throw new Error(result.error || '例外提交被治理层阻断');
    document.getElementById('rule-exception-feedback-modal')?.remove();
    showToast('例外已记录并创建替代规则。', 'success');
    await renderRulesHabits();
  } catch (error) {
    const message = error.message || String(error);
    if (errorNode) errorNode.textContent = `治理层阻断：${message}`;
    showToast(`例外提交失败：${message}`, 'error');
  }
}

async function createChildException(parentRule) {
  if (!parentRule) return;
  document.getElementById('rule-exception-modal')?.remove();
  const modal = document.createElement('div');
  modal.id = 'rule-exception-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="新增子例外">
    <div class="modal-head"><h3>新增子例外</h3><p>父规则：<code>${escapeHtml(parentRule)}</code>。子规则必须不同，且原因必填。</p></div>
    <div class="modal-body"><label class="field"><span>子例外规则 ID</span><input id="rule-exception-child" maxlength="160" /></label>
      <label class="field"><span>优先级（-100 到 100）</span><input id="rule-exception-priority" type="number" min="-100" max="100" value="0" /></label>
      <label class="field"><span>例外原因</span><textarea id="rule-exception-reason" rows="3" maxlength="2000"></textarea></label></div>
    <div class="modal-actions"><button class="btn" type="button" onclick="document.getElementById('rule-exception-modal')?.remove()">取消</button><button class="btn btn-primary" type="button" onclick="submitChildException('${escapeHtml(parentRule)}')">创建</button></div>
  </div>`;
  modal.addEventListener('click', event => { if (event.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

async function submitChildException(parentRule) {
  const child = String(document.getElementById('rule-exception-child')?.value || '').trim();
  const priorityRaw = String(document.getElementById('rule-exception-priority')?.value || '0');
  const priority = Number.isFinite(Number(priorityRaw)) ? Number(priorityRaw) : 0;
  const reason = String(document.getElementById('rule-exception-reason')?.value || '').trim();
  if (!child) return showToast('请填写子例外规则 ID。', 'error');
  if (!reason) return showToast('请填写例外原因。', 'error');
  document.getElementById('rule-exception-modal')?.remove();
  try {
    const result = await callApi('create_child_exception', parentRule, child.trim(), priority, reason.trim(), activeShareGroupId || 'default', true);
    if (result?.error || result?.ok === false) throw new Error(result.error || '创建例外失败');
    showToast('子例外已创建。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`创建子例外失败：${error.message || error}`, 'error'); }
}

async function revokeRuleException(exceptionId) {
  if (!exceptionId || !confirm('确认撤销这条子例外？')) return;
  try {
    const result = await callApi('revoke_rule_exception', exceptionId, activeShareGroupId || 'default', true);
    if (result?.error || result?.ok === false) throw new Error(result.error || '撤销例外失败');
    showToast('子例外已撤销。', 'success');
    await renderRulesHabits();
  } catch (error) { showToast(`撤销例外失败：${error.message || error}`, 'error'); }
}

function ruleCard(record) {
  const state = rulePreviewState(record);
  const range = (record.assignments || []).map(a => a.target_type);
  if (ruleRangeFilter !== 'all' && !range.includes(ruleRangeFilter)) return '';
  if (rulePreviewAgentId && ruleVisibilityFilter !== 'all' && record.injection_policy === 'always' && state !== ruleVisibilityFilter) return '';
  const preview = record._preview || {};
  const stateLabel = state === 'effective' ? '当前 Agent 生效'
    : state === 'excluded' ? '当前 Agent 被排除' : state === 'unavailable' ? '当前 Agent 不适用' : '';
  const stateChip = state === 'effective' ? 'chip-confirmed' : (state === 'excluded' ? 'chip-high' : 'chip-info');
  const sources = state === 'effective' ? `来源：${(preview.matched_sources || []).join('；')}`
    : state === 'excluded' ? `排除原因：${(preview.excluded_sources || []).join('；')}` : '';
  const editable = record.injection_policy === 'always' || record.status === 'active';
  const decision = ruleDecisionFor(record);
  const stats = ruleStatsFor(record);
  const receipts = ruleReceiptsFor(record);
  const exceptions = ruleExceptionsFor(record);
  const confidence = decision?.scope_confidence ?? record.scope_confidence ?? record.auto_scope_confidence ?? record.confidence;
  const decisionId = decision?.decision_id || record.decision_id || '';
  const mergedCount = Array.isArray(record.supersedes) ? record.supersedes.length : 0;
  const mergedHtml = mergedCount ? `<span class="chip chip-info">已合并 ${mergedCount} 条旧记忆</span>` : '';
  const exceptionHtml = exceptions.length ? `<div class="rule-exceptions"><div class="muted">子例外（父规则：${escapeHtml(record.memory_id)}）</div>${exceptions.map(item => `<div class="rule-exception-row"><code>${escapeHtml(item.child_exception || item.child_rule_id || '')}</code><span class="chip chip-info">priority ${Number(item.priority || 0)}</span><span class="muted">${escapeHtml(item.reason || '')}</span>${item.active === false ? '<span class="chip chip-medium">已撤销</span>' : `<button class="btn btn-danger btn-icon" type="button" onclick="revokeRuleException('${escapeHtml(item.exception_id || '')}')">撤销</button>`}</div>`).join('')}</div>` : '';
  const priorityHtml = record.injection_policy === 'always' ? `<div class="muted">基础优先级：${Number(record.priority || 0)}${sources ? ` · ${escapeHtml(sources)}` : ''}</div>` : '';
  // Pure parent/child relation editing is an administrator diagnostic path.
  // Daily governance uses the receipt-level “例外” action above, which carries
  // the actual project override and enters the atomic service path.
  const advancedHtml = `<details class="rule-advanced"><summary>诊断与高级治理（管理员）</summary><div class="rule-advanced-body"><p class="muted">仅用于排查已有关系；日常例外请从命中回执提交替代规则。</p>${priorityHtml}<div class="finding-actions"><button class="btn" type="button" onclick="createChildException('${escapeHtml(record.memory_id)}')">新增子例外关系</button></div>${exceptionHtml}</div></details>`;
  const receiptHtml = receipts.slice(-3).map(renderRuleReceiptActions).join('');
  return `<article class="memory-card"><div class="memory-card-top"><strong>${escapeHtml(displayTitle(record))}</strong>
    <span class="chip ${record.injection_policy === 'always' ? 'chip-confirmed' : ''}">${record.injection_policy === 'always' ? '强制' : '按需'}</span>
    ${mergedHtml}
    ${stateLabel ? `<span class="chip ${stateChip}">${escapeHtml(stateLabel)}</span>` : ''}</div>
    <p>${escapeHtml(displayBody(record)).slice(0, 300)}</p>
    <div class="muted">适用范围：${escapeHtml(ruleAudience(record))}</div>
    ${confidence !== undefined ? `<div class="chips">${ruleConfidenceLabel(confidence)}${decision?.scope_reason || record.scope_reason ? `<span class="muted">${escapeHtml(decision?.scope_reason || record.scope_reason)}</span>` : ''}</div>` : ''}
    ${stats ? `<div class="muted">范围命中：${Number(stats.total || 0)} · 遵循 ${Number(stats.accepted || 0)} · 纠正 ${Number(stats.corrected || 0)} · 作用域错误 ${Number(stats.wrong_scope || 0)}</div>` : ''}
    ${decisionId ? `<div class="rule-decision-link"><code>decision ${escapeHtml(decisionId)}</code>${decision?.undo_id || decision?.status !== 'undone' ? `<button class="btn btn-danger btn-icon" type="button" onclick="undoRuleDecision('${escapeHtml(decisionId)}')">撤销自动决定</button>` : '<span class="chip chip-medium">已撤销</span>'}</div>` : ''}
    ${receiptHtml ? `<div class="rule-receipts"><div class="muted">命中回执与反馈</div>${receiptHtml}</div>` : ''}
    <div class="finding-actions">${editable ? `<button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(record.memory_id)}">管理适用范围</button>` : ''}</div>
    ${advancedHtml}
  </article>`;
}

async function setRulePreviewAgent(agentId) {
  rulePreviewAgentId = agentId || '';
  await renderRulesHabits();
}

async function setRulePreviewProject(projectRef) {
  rulePreviewProjectRef = projectRef || '';
  await renderRulesHabits();
}

async function setRulePreviewProvider(provider) {
  rulePreviewProvider = provider || '';
  await renderRulesHabits();
}

async function setRulePreviewRuntimeRole(runtimeRole) {
  rulePreviewRuntimeRole = runtimeRole || '';
  await renderRulesHabits();
}

async function setRuleVisibilityFilter(value) {
  ruleVisibilityFilter = value || 'all';
  await renderRulesHabits();
}

async function setRuleRangeFilter(value) {
  ruleRangeFilter = value || 'all';
  await renderRulesHabits();
}

function removeRuleAudienceModal() {
  document.getElementById('rule-audience-modal')?.remove();
}

function ruleTargetControls(type, assignment = {}) {
  const options = ruleScopeOptions || {};
  const select = (name, list, selected, label) => `<label class="field"><span>${label}</span><select id="rule-audience-${name}"><option value="">请选择</option>${ruleSelectOptions(list, selected || '')}</select></label>`;
  if (type === 'agent') return select('target', options.agents, assignment.target_id, 'Agent');
  if (type === 'group') return select('target', options.groups, assignment.target_id, '共享组');
  if (type === 'project') return select('project', options.projects, assignment.project_ref || assignment.target_id, '项目');
  if (type === 'agent_project') return `${select('target', options.agents, assignment.target_id, 'Agent')}${select('project', options.projects, assignment.project_ref, '项目')}`;
  if (type === 'provider') return select('target', options.providers, assignment.target_id, '宿主');
  if (type === 'runtime_role') return select('target', options.runtime_roles, assignment.target_id, '运行角色');
  return '<p class="muted">系统范围不需要额外目标；它会对所有可信宿主生效。</p>';
}

function ruleAssignmentDetail(assignment) {
  const visible = ruleAudience({assignments: [assignment]});
  const targetId = assignment.target_id || assignment.project_ref || '';
  const projectRef = assignment.target_type === 'agent_project' ? (assignment.project_ref || '') : '';
  const ids = [targetId, projectRef].filter(Boolean).join(' / ');
  return `${escapeHtml(visible)}${ids ? ` · <code>${escapeHtml(ids)}</code>` : ''}`;
}

function ruleSimpleDefaults(record) {
  const first = (record.assignments || []).find(item => item.effect !== 'exclude') || {};
  const currentAgent = ruleScopeOptions?.current_agent_id || activeAgentInstanceId || '';
  const currentProject = ruleScopeOptions?.current_project_ref || '';
  const type = String(first.target_type || '').toLowerCase();
  const who = type === 'group' || type === 'project' || type === 'system' ? 'all_agents' : 'current_agent';
  const where = type === 'agent_project' || (type === 'project' && first.project_ref) ? 'current_project' : 'all_projects';
  return {
    who: currentAgent ? who : 'all_agents',
    where: currentProject ? where : 'all_projects',
  };
}

function simpleRuleAudienceAssignments() {
  const who = document.getElementById('rule-audience-simple-who')?.value || 'current_agent';
  const where = document.getElementById('rule-audience-simple-where')?.value || 'current_project';
  const options = ruleScopeOptions || {};
  const currentAgent = String(options.current_agent_id || activeAgentInstanceId || '');
  const currentProject = String(options.current_project_ref || '').trim();
  if (!['all_projects', 'current_project'].includes(where)) return null;
  if (where === 'current_project' && !currentProject) return null;
  const agents = (options.agents || []).map(item => String(item.id || '')).filter(Boolean);
  const allAgentIds = agents.length ? agents : (currentAgent ? [currentAgent] : []);
  const useCurrentProject = where === 'current_project';
  const makeAgent = id => ({target_type: useCurrentProject ? 'agent_project' : 'agent', target_id: id, project_ref: useCurrentProject ? currentProject : '', effect: 'include', priority_override: null});
  if (who === 'all_agents') {
    if (activeShareGroupId && where !== 'current_project') return [{target_type: 'group', target_id: activeShareGroupId, project_ref: '', effect: 'include', priority_override: null}];
    return allAgentIds.map(makeAgent);
  }
  return currentAgent ? [makeAgent(currentAgent)] : [];
}

function refreshSimpleRuleAudience() {
  const modal = document.getElementById('rule-audience-modal');
  if (modal) modal.dataset.simpleDirty = 'true';
}

function refreshRuleAudienceTarget() {
  const type = document.getElementById('rule-audience-type')?.value || 'agent';
  const box = document.getElementById('rule-audience-targets');
  if (box) box.innerHTML = ruleTargetControls(type);
}

function openRuleAudienceEditor(memoryId) {
  const record = ruleRecordsById.get(memoryId);
  if (!record || !ruleScopeOptions) return;
  removeRuleAudienceModal();
  const legacyIds = new Set(record.legacy_unknown_assignment_ids || []);
  const assignmentRows = (record.assignments || []).map((item, index) => {
    const legacy = legacyIds.has(item.assignment_id);
    return `<label class="raw-file-row" style="grid-template-columns:auto 1fr;align-items:start"><input type="checkbox" data-rule-assignment="${index}" checked><span>${ruleAssignmentDetail(item)}${legacy ? ' · legacy_unknown（仅可保留或删除，不能新增此目标）' : ''}</span></label>`;
  }).join('') || '<p class="muted">尚未配置适用范围。若设为强制，至少新增一个“包含”范围。</p>';
  const priority = Number.isInteger(record.priority) ? record.priority : 0;
  const defaults = ruleSimpleDefaults(record);
  const currentAgent = ruleScopeOptions.current_agent_id || activeAgentInstanceId || '';
  const currentAgentLabel = agentDisplayName(currentAgent, '当前助手');
  const connectedLabel = `所有已连接助手（${Number(ruleScopeOptions.connected_agent_count || (ruleScopeOptions.agents || []).length || 0)} 个）`;
  const currentProjectKnown = Boolean(String(ruleScopeOptions.current_project_ref || '').trim()) && ruleScopeOptions.current_project_available !== false;
  const simplePolicy = record.injection_policy === 'always' ? 'always' : 'relevant';
  const advancedTypes = (ruleScopeOptions.target_types || ['agent', 'group', 'project', 'agent_project', 'provider', 'runtime_role', 'system']).filter(Boolean);
  const modal = document.createElement('div');
  modal.id = 'rule-audience-modal';
  modal.className = 'modal-backdrop';
  modal.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-label="管理规则适用范围">
    <div class="modal-head"><h3>管理规则适用范围</h3><p>删除范围不会删除记忆。只有“强制”规则才会按范围注入。</p><p>用人话选择规则适用范围；保存仍使用受信任的稳定 ID。</p></div>
    <div class="modal-body">
      <section class="rule-simple-settings" aria-label="简单规则设置">
        <label class="field"><span>记忆方式</span><select id="rule-audience-policy" onchange="refreshSimpleRuleAudience()"><option value="relevant" ${simplePolicy === 'relevant' ? 'selected' : ''}>普通习惯</option><option value="always" ${simplePolicy === 'always' ? 'selected' : ''}>必须遵守</option></select></label>
        <label class="field"><span>谁适用</span><select id="rule-audience-simple-who" onchange="refreshSimpleRuleAudience()"><option value="current_agent" ${defaults.who === 'current_agent' ? 'selected' : ''}>${escapeHtml(currentAgentLabel)}</option><option value="all_agents" ${defaults.who === 'all_agents' ? 'selected' : ''}>${escapeHtml(connectedLabel)}</option></select></label>
        <label class="field"><span>哪里适用</span><select id="rule-audience-simple-where" onchange="refreshSimpleRuleAudience()"><option value="all_projects" ${defaults.where === 'all_projects' ? 'selected' : ''}>所有项目</option>${currentProjectKnown ? `<option value="current_project" ${defaults.where === 'current_project' ? 'selected' : ''}>当前项目</option>` : '<option value="current_project" disabled>当前项目（未确认）</option>'}</select></label>
      </section>
      <details class="rule-advanced"><summary>高级设置</summary><div class="rule-advanced-body">
        <p class="muted">高级设置保留 include/exclude、基础/覆盖优先级、target_type、provider、runtime_role 与原始 ID。日常修改无需展开。</p>
        <label class="field"><span>基础优先级</span><select id="rule-audience-priority">${[-100, -50, 0, 50, 100].map(value => `<option value="${value}" ${value === priority ? 'selected' : ''}>${value}</option>`).join('')}</select></label>
        <h4>已有适用范围</h4><div id="rule-existing-assignments">${assignmentRows}</div>
        <h4>新增适用范围</h4>
        <label class="field"><span>作用（include / exclude）</span><select id="rule-audience-effect"><option value="include">包含</option><option value="exclude">排除</option></select></label>
        <label class="field"><span>范围类型（target_type）</span><select id="rule-audience-type" onchange="refreshRuleAudienceTarget()">${advancedTypes.map(type => `<option value="${type}">${escapeHtml({agent:'当前助手',group:'已连接助手组',project:'项目',agent_project:'当前助手 + 项目',provider:'宿主',runtime_role:'运行角色',system:'所有可信助手'}[type] || type)}</option>`).join('')}</select></label>
        <div id="rule-audience-targets">${ruleTargetControls('agent')}</div>
        <label class="field"><span>覆盖优先级</span><select id="rule-audience-override"><option value="">不覆盖</option>${[-100, -50, 0, 50, 100].map(value => `<option value="${value}">${value}</option>`).join('')}</select></label>
        <label class="raw-file-row" style="grid-template-columns:auto 1fr"><input type="checkbox" id="rule-audience-add"><span>保存时加入这条新范围</span></label>
      </div></details>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-mg-action="rule-modal-close">取消</button><button class="btn btn-primary" type="button" data-mg-action="rule-save" data-memory-id="${escapeHtml(memoryId)}">确认更新</button></div>
  </div>`;
  document.body.appendChild(modal);
}

async function ensureRuleAudienceEditor(memoryId) {
  const groupId = activeShareGroupId || 'default';
  try {
    if (!ruleScopeOptions) ruleScopeOptions = await callApi('get_rule_scope_options', groupId);
    if (!ruleScopeOptions || ruleScopeOptions.error) throw new Error(ruleScopeOptions?.error || '无法读取规则范围');
    if (!ruleRecordsById.has(memoryId)) {
      const data = await callApi('list_rules_habits', groupId);
      if (data.error) throw new Error(data.error);
      ruleRecordsById = new Map(Object.values(rulePageBuckets(data)).flat().map(record => [record.memory_id, record]));
    }
    openRuleAudienceEditor(memoryId);
  } catch (error) { showToast(`无法打开规则范围：${error.message || error}`, 'error'); }
}

async function saveRuleAudience(memoryId) {
  const record = ruleRecordsById.get(memoryId);
  if (!record) return;
  const policy = document.getElementById('rule-audience-policy')?.value || 'relevant';
  let assignments = (record.assignments || []).filter((_, index) => document.querySelector(`[data-rule-assignment="${index}"]`)?.checked).map(item => ({...item}));
  const selectedSimpleWhere = document.getElementById('rule-audience-simple-where')?.value || '';
  const currentProjectKnown = Boolean(String(ruleScopeOptions?.current_project_ref || '').trim()) && ruleScopeOptions?.current_project_available !== false;
  if (selectedSimpleWhere === 'current_project' && !currentProjectKnown) {
    showToast('未确认当前项目，不能保存当前项目范围。', 'error');
    return;
  }
  // relevant has no audience relation by definition.  The server performs the
  // policy flip and assignment cleanup in one transaction after confirmation.
  if (policy === 'relevant') assignments = [];
  const modal = document.getElementById('rule-audience-modal');
  if (policy !== 'relevant' && modal?.dataset.simpleDirty === 'true') {
    const simpleAssignments = simpleRuleAudienceAssignments();
    if (!simpleAssignments) {
      showToast('未确认当前项目，不能保存当前项目范围。', 'error');
      return;
    }
    assignments = simpleAssignments;
  }
  if (policy !== 'relevant' && document.getElementById('rule-audience-add')?.checked) {
    const type = document.getElementById('rule-audience-type')?.value || '';
    const targetId = document.getElementById('rule-audience-target')?.value || '';
    const projectRef = document.getElementById('rule-audience-project')?.value || '';
    const effect = document.getElementById('rule-audience-effect')?.value || 'include';
    const priorityValue = document.getElementById('rule-audience-override')?.value || '';
    const targetRequired = ['agent', 'group', 'provider', 'runtime_role'].includes(type);
    if ((targetRequired && !targetId) || (type === 'project' && !projectRef) || (type === 'agent_project' && (!targetId || !projectRef))) {
      showToast('请选择已发现的适用对象；没有可选对象时不能保存。', 'error');
      return;
    }
    assignments.push({target_type: type, target_id: targetId, project_ref: projectRef, effect, priority_override: priorityValue === '' ? null : Number(priorityValue)});
  }
  if (policy === 'always' && !assignments.some(item => item.effect === 'include')) {
    showToast('强制规则至少需要一个“包含”适用范围。', 'error');
    return;
  }
  if (!confirm(policy === 'relevant' ? '切回按需召回并清理适用范围？记忆正文不会删除。' : '确认更新这条规则的适用范围？')) return;
  try {
    const priority = Number(document.getElementById('rule-audience-priority')?.value || 0);
    const result = await callApi('update_rule_audience', memoryId, assignments, activeShareGroupId || 'default', policy, priority, true);
    if (result.error || result.ok === false) throw new Error(result.error || '更新失败');
    removeRuleAudienceModal();
    showToast(result.message || '适用范围已更新。', 'success');
    if (state.activeTab === 'neurons') await refreshNeuronRuleGovernance(memoryId);
    else await renderRulesHabits();
  } catch (error) { showToast(`规则范围更新失败：${error.message || error}`, 'error'); }
}

async function renderRulesHabits() {
  setContent('<div class="loading">正在读取规则与习惯…</div>');
  try {
    const groupId = activeShareGroupId || 'default';
    const [data, options, decisions, metrics, receipts, exceptions] = await Promise.all([
      callApi('list_rules_habits', groupId), callApi('get_rule_scope_options', groupId),
      callApiOptional('list_rule_decisions', {decisions: [], total: 0}, groupId, 50),
      callApiOptional('get_rule_auto_scope_metrics', {stats: [], auto_scope: {}}, groupId),
      callApiOptional('list_rule_match_receipts', {receipts: [], total: 0}, groupId, '', activeAgentInstanceId, 50),
      callApiOptional('list_rule_exceptions', {exceptions: [], total: 0}, groupId, ''),
    ]);
    if (data.error) throw new Error(data.error);
    if (options.error) throw new Error(options.error);
    ruleDecisionRows = Array.isArray(decisions) ? decisions : (decisions.decisions || decisions.items || []);
    ruleScopeMetrics = metrics || {};
    ruleReceiptRows = Array.isArray(receipts) ? receipts : (receipts.receipts || receipts.items || []);
    ruleExceptionRows = Array.isArray(exceptions) ? exceptions : (exceptions.exceptions || exceptions.items || []);
    ruleScopeOptions = options;
    const agents = options.agents || [];
    const projects = options.projects || [];
    const providers = options.providers || [];
    const runtimeRoles = options.runtime_roles || [];
    if (!agents.some(item => item.id === rulePreviewAgentId)) {
      rulePreviewAgentId = agents.some(item => item.id === activeAgentInstanceId) ? activeAgentInstanceId : '';
    }
    if (!projects.some(item => item.id === rulePreviewProjectRef)) rulePreviewProjectRef = '';
    if (!providers.some(item => item.id === rulePreviewProvider)) rulePreviewProvider = '';
    if (!runtimeRoles.some(item => item.id === rulePreviewRuntimeRole)) rulePreviewRuntimeRole = '';
    rulePreviewById = new Map();
    const labels = {mandatory: '强制规则', preferences: '长期习惯与偏好', procedures: '工作流程', corrections: '纠错与禁忌', projects: '项目决策'};
    const pageBuckets = rulePageBuckets(data);
    const allRecords = Object.values(pageBuckets).flat();
    ruleRecordsById = new Map(allRecords.map(record => [record.memory_id, record]));
    if (rulePreviewAgentId) {
      const preview = await callApi(
        'preview_effective_rules', rulePreviewAgentId, groupId,
        rulePreviewProjectRef, rulePreviewProvider, rulePreviewRuntimeRole,
      );
      if (preview.error) throw new Error(preview.error);
      (preview.effective || []).forEach(item => { rulePreviewById.set(item.memory_id, 'effective'); const r = ruleRecordsById.get(item.memory_id); if (r) r._preview = item; });
      (preview.excluded || []).forEach(item => { rulePreviewById.set(item.memory_id, 'excluded'); const r = ruleRecordsById.get(item.memory_id); if (r) r._preview = item; });
      (preview.unavailable || []).forEach(item => { rulePreviewById.set(item.memory_id, 'unavailable'); const r = ruleRecordsById.get(item.memory_id); if (r) r._preview = item; });
    }
    const blocks = Object.entries(labels).map(([key, label]) => {
      const cards = (pageBuckets[key] || []).map(ruleCard).filter(Boolean);
      if (!cards.length) return '';
      return `<details class="folder-group" open style="--folder-depth:0">
        <summary class="folder-row"><span class="folder-caret" aria-hidden="true"></span><span class="folder-name">${label}</span><span class="folder-count">${cards.length} 条</span></summary>
        <div class="folder-children">${cards.join('')}</div>
      </details>`;
    }).join('');
    const agentOptions = `<option value="">全部范围（不做有效性预览）</option>${ruleSelectOptions(agents, rulePreviewAgentId)}`;
    const projectOptions = `<option value="">未确认项目（不猜测）</option>${ruleSelectOptions(projects, rulePreviewProjectRef)}`;
    const providerOptions = `<option value="">未确认宿主</option>${ruleSelectOptions(providers, rulePreviewProvider)}`;
    const roleOptions = `<option value="">未确认运行角色</option>${ruleSelectOptions(runtimeRoles, rulePreviewRuntimeRole)}`;
    const scopeOptions = `<option value="all">全部范围类型</option>${['agent','group','project','agent_project','provider','runtime_role','system'].map(type => `<option value="${type}" ${ruleRangeFilter === type ? 'selected' : ''}>${escapeHtml({agent:'Agent',group:'共享组',project:'项目',agent_project:'Agent + 项目',provider:'宿主',runtime_role:'运行角色',system:'系统'}[type])}</option>`).join('')}`;
    const visibilityOptions = `<option value="effective" ${ruleVisibilityFilter === 'effective' ? 'selected' : ''}>仅当前 Agent 生效</option><option value="excluded" ${ruleVisibilityFilter === 'excluded' ? 'selected' : ''}>仅当前 Agent 被排除</option><option value="all" ${ruleVisibilityFilter === 'all' ? 'selected' : ''}>不按当前 Agent 过滤</option>`;
    // v2: the six diagnostic filters are hidden behind a closed <details> so
    // they never enter the first-viewport tab order and never participate in
    // a routine "add rule" request.
    const diagnosticFilters = `<section class="card"><details id="rule-diagnostics"><summary class="card-head" style="cursor:pointer"><div><h2>诊断与高级筛选</h2><p>管理员或排查时才需要调整；日常使用保持默认值。</p></div></summary>
      <div class="page-actions"><label class="field"><span>预览 Agent</span><select onchange="setRulePreviewAgent(this.value)">${agentOptions}</select></label><label class="field"><span>项目</span><select onchange="setRulePreviewProject(this.value)">${projectOptions}</select></label><label class="field"><span>宿主</span><select onchange="setRulePreviewProvider(this.value)">${providerOptions}</select></label><label class="field"><span>运行角色</span><select onchange="setRulePreviewRuntimeRole(this.value)">${roleOptions}</select></label><label class="field"><span>显示</span><select onchange="setRuleVisibilityFilter(this.value)">${visibilityOptions}</select></label><label class="field"><span>范围</span><select onchange="setRuleRangeFilter(this.value)">${scopeOptions}</select></label></div><p class="muted">预览只使用已发现/可信上下文；未知项目、宿主或角色保持空值，不会猜测命中规则。</p></details></section>`;
    setContent(`<div class="view-heading"><span class="eyebrow">Rules & Habits</span><h2>规则与习惯</h2>
      <p>规则受众独立于记忆来源。范围删除不会删除记忆；只有强制规则会按范围注入。</p></div>
      ${renderRuleCreatePanel(options)}${renderRuleAutoScopePanel()}
      ${diagnosticFilters}${blocks || '<div class="card empty-state"><p>当前筛选范围没有规则或习惯。</p></div>'}`);
  } catch (e) { setContent(`<div class="view-heading"><span class="eyebrow">Rules & Habits</span><h2>规则与习惯</h2><p>规则加载失败。</p></div><div class="card empty-state"><p>规则加载失败：${escapeHtml(e)}</p></div>`); }
}

function historyBytes(value) {
  const n = Number(value || 0);
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / Math.pow(1024, index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function renderHistoryBackfillPanel(inventory) {
  const sources = (inventory && inventory.sources) || [];
  const groups = new Map();
  sources.forEach(source => {
    const status = source.status || (source.supported ? 'importable' : 'unsupported');
    const key = [source.provider || 'unknown', status, source.matched_agent_id || '', source.support_reason || ''].join('|');
    const current = groups.get(key) || { ...source, status, file_count: 0, byte_count: 0, path_count: 0 };
    current.file_count += Number(source.file_count || 0);
    current.byte_count += Number(source.byte_count || 0);
    current.path_count += 1;
    groups.set(key, current);
  });
  const grouped = Array.from(groups.values());
  const rows = grouped.map(source => {
    const status = source.status || (source.supported ? 'importable' : 'unsupported');
    const label = { importable: '可导入', complete: '已完成', partial: '部分导入（会话已索引）', pending_binding: '待绑定 Agent', unsupported: '暂不支持', error: '扫描失败' }[status] || status;
    const tone = status === 'importable' ? 'chip-confirmed' : (status === 'unsupported' || status === 'pending_binding' || status === 'partial' ? 'chip-medium' : 'chip-high');
     const bound = source.matched_agent_id ? ` → ${agentDisplayName(source.matched_agent_id)}` : '';
    const location = source.path_count > 1 ? `${source.path_count} 个本地路径` : guiPathText(source.path, '受保护来源');
    return `<tr><td><strong>${escapeHtml(source.provider || 'unknown')}</strong></td><td class="path-cell">${escapeHtml(location)}</td><td>${Number(source.file_count || 0)}</td><td>${historyBytes(source.byte_count)}</td><td><span class="chip ${tone}">${escapeHtml(label)}</span><div class="surface-meta">${escapeHtml(source.support_reason || '')}${escapeHtml(bound)}</div></td></tr>`;
  }).join('') || '<tr><td colspan="5" class="empty-note">未发现可识别的本地会话来源。</td></tr>';
  const canImport = sources.some(source => source.status === 'importable' && source.matched_agent_id);
  return `<section class="card" id="history-backfill"><div class="card-head"><div><h2>扫描旧会话</h2><p>只扫描本机可读日志。导入会写入独立历史库，不会写入长期记忆或注入当前对话。</p></div><div class="finding-actions"><button class="btn" type="button" onclick="renderHistory()">重新扫描</button>${canImport ? `<button class="btn btn-primary" data-mg-action="history-backfill">导入已绑定来源</button>` : ''}</div></div><div class="source-map-table-wrap"><table class="source-map-table"><thead><tr><th>宿主</th><th>来源</th><th>文件</th><th>大小</th><th>状态 / 绑定</th></tr></thead><tbody>${rows}</tbody></table></div><p class="muted">超大日志只会导入有界可见前缀，并标为“部分导入”；专有数据库会明确标为暂不支持，未绑定宿主不会被错误导入到当前 Agent。</p></section>`;
}

async function runHistoryBackfill() {
  if (!confirm('确认导入本机旧会话？原文只进入独立对话历史库，不会写入长期记忆。')) return;
  let continuation = historyBackfillContinuation;
  let imported = 0;
  let skipped = 0;
  let errors = 0;
  let turns = 0;
  let batches = 0;
  let retryableFailed = 0;
  try {
    // The backend intentionally bounds each task.  Follow its durable
    // continuation until all never-before-processed sources have been visited;
    // failed sources are reported but do not create an endless retry loop.
    while (batches < 20) {
      const accepted = await callApi('backfill_local_history', continuation);
      if (accepted.error || accepted.ok === false) {
        throw new Error(apiErrorMessage(accepted, '旧会话导入启动失败'));
      }
      const task = normalizeTaskState(accepted);
      const runId = task.run_id || accepted.job_id || '';
      const data = (accepted.deferred || runId)
        ? await waitForTask(runId, '旧会话导入', 300000)
        : accepted;
      if (!data || data.ok === false || data.execution_status === 'failed') {
        throw new Error(apiErrorMessage(data || {}, '旧会话导入失败'));
      }
      batches += 1;
      imported += Number(data.imported ?? data.session_count ?? 0);
      // ``skipped`` is a durable already-processed count at this batch's
      // boundary, not a per-batch delta. Keep the latest value instead of
      // summing it repeatedly across continuations.
      skipped = Number(data.skipped || 0);
      errors += Array.isArray(data.errors)
        ? data.errors.length
        : Number(data.errors ?? data.error_count ?? 0);
      turns += Number(data.turn_count || 0);
      retryableFailed = Number(data.retryable_failed_files || 0);
      continuation = data.continuation || null;
      historyBackfillContinuation = continuation;
      const remaining = Number(data.remaining_files ?? data.remaining_fresh_files ?? 0);
      if (!continuation || remaining <= 0) break;
      showToast(`旧会话导入中：已完成 ${batches} 批，新增 ${imported} 个会话，剩余 ${remaining} 个来源`, 'info');
    }
    const suffix = `${turns ? `，${turns} 条消息` : ''}${errors ? `，错误 ${errors}` : ''}${retryableFailed ? `，待重试失败源 ${retryableFailed}` : ''}`;
    showToast(`旧会话导入完成：新增 ${imported} 个会话，跳过已处理 ${skipped}${suffix}`, errors || retryableFailed ? 'info' : 'success');
    await renderHistory();
    if (state.activeTab === 'neurons') await refreshNeuronGraph();
  } catch (e) { showToast(`旧会话导入失败：${e.message || e}`, 'error'); }
}

function historySessionTitle(session) {
  const displayTitle = String(session?.display_title || '').trim();
  if (displayTitle) return displayTitle;
  const title = String(session?.title || '').trim();
  if (title && title !== '未命名会话') return title;
  const provider = String(session?.provider || '本地').trim() || '本地';
  const stamp = String(session?.created_at || session?.imported_at || '').trim();
  return stamp ? `${provider} 对话 · ${stamp}` : `${provider} 对话`;
}

function buildHistoryReadRequest({sessionId = '', turnId = '', limit = 100, offset = 0} = {}) {
  const session = String(sessionId || '').trim();
  const turn = String(turnId || '').trim();
  if (Boolean(session) === Boolean(turn)) throw new Error('conversation_selector_invalid');
  const request = {limit: Number(limit) || 100, offset: Math.max(0, Number(offset) || 0)};
  if (session) request.session_id = session;
  else request.turn_id = turn;
  return request;
}

function historyErrorMessage(result, fallback = '对话历史操作失败') {
  const code = String(result?.code || result?.error?.code || result?.error || '').trim();
  if (code === 'conversation_selector_invalid') {
    return '读取会话需要且仅需要 session_id；读取单条消息需要且仅需要 turn_id。';
  }
  return apiErrorMessage(result, fallback);
}

function renderHistoryGrouped(data) {
  const groups = new Map();
  for (const session of (data.sessions || [])) {
    const key = session.project_key || 'history-project-unknown';
    if (!groups.has(key)) groups.set(key, {meta: session, agents: new Map()});
    const group = groups.get(key);
    const owner = session.owner_agent_instance_id || session.agent_instance_id || 'unknown-agent';
    if (!group.agents.has(owner)) group.agents.set(owner, []);
    group.agents.get(owner).push(session);
  }
  return [...groups.values()].map(group => {
    const meta = group.meta;
    const status = meta.project_status === 'removed' ? ' · 路径已移除' : '';
    const parent = meta.project_parent ? ` · ${meta.project_parent}` : '';
    const sessionCount = [...group.agents.values()].reduce((n, sessions) => n + sessions.length, 0);
    const agents = [...group.agents.entries()].map(([owner, sessions]) => {
      const canDelete = !!activeAgentInstanceId && owner === activeAgentInstanceId;
      return `<section class="history-agent-group"><h3>${escapeHtml(agentDisplayName(owner, sessions[0]?.provider || '未知 Agent'))}</h3>${sessions.map(s => `<article class="memory-card"><div class="memory-card-top"><strong>${escapeHtml(historySessionTitle(s))}</strong><span class="chip">${escapeHtml(s.provider || 'local')}</span></div><p>${escapeHtml(s.summary || s.preview_excerpt || '尚无可用概要')}</p><div class="chips"><span class="chip ${s.summarized ? 'chip-confirmed' : 'chip-info'}">${s.summarized ? '已摘要' : '首条用户消息'}</span></div><div class="muted">${escapeHtml(s.created_at || s.imported_at || '')} · ${s.turn_count || 0} 条消息 · ${s.evidence_count || 0} 条已萃取证据</div><div class="finding-actions"><button class="btn" data-mg-action="history-read-session" data-session-id="${escapeHtml(s.session_id)}">阅读原文</button><button class="btn" data-mg-action="history-extract" data-session-id="${escapeHtml(s.session_id)}">萃取预览</button><button class="btn" data-mg-action="history-export" data-session-id="${escapeHtml(s.session_id)}">导出</button>${canDelete ? `<button class="btn" data-mg-action="history-delete" data-session-id="${escapeHtml(s.session_id)}">删除历史</button>` : '<span class="muted">仅会话 owner 可删除</span>'}</div></article>`).join('')}</section>`;
    }).join('');
    return `<details class="card history-project-group folder-group" open>
      <summary class="folder-row"><span class="folder-caret" aria-hidden="true"></span><span class="folder-name">${escapeHtml(meta.project_label || '未识别项目')}${escapeHtml(status + parent)}</span><span class="folder-count">${sessionCount} 个会话</span></summary>
      <div class="folder-children">${agents}</div>
    </details>`;
  }).join('');
}

async function refreshHistoryBackfillPanel() {
  const host = document.getElementById('history-backfill-host');
  if (!host) return;
  try {
    const inventory = await callApi('discover_local_history_sources');
    if (inventory.error || inventory.ok === false) throw new Error(apiErrorMessage(inventory, '旧会话扫描失败'));
    const current = document.getElementById('history-backfill-host');
    if (current) current.innerHTML = renderHistoryBackfillPanel(inventory);
  } catch (e) {
    const current = document.getElementById('history-backfill-host');
    if (current) current.innerHTML = `<section class="card"><div class="card-head"><div><h2>扫描旧会话</h2><p>已索引会话仍可正常读取。</p></div></div><p class="muted">本地来源扫描失败：${escapeHtml(e.message || e)}</p></section>`;
  }
}

function historyBackfillLoadingPanel() {
  return '<div id="history-backfill-host"><section class="card"><div class="card-head"><div><h2>扫描旧会话</h2><p>后台发现 Claude / Codex / Cursor 本地日志，不阻塞已索引会话。</p></div></div><div class="loading" style="min-height:72px">正在扫描本地来源…</div></section></div>';
}

async function renderHistory() {
  // 已索引会话是主页面；本地日志发现可能扫描数 GB 文件，不能阻塞首屏。
  setContent('<div class="loading">正在读取已索引对话历史…</div>');
  const scopeReady = await ensureGovernanceScope();
  if (!scopeReady) {
    setContent(`<div class="view-heading"><span class="eyebrow">Conversation History</span><h2>对话历史</h2>
      <p>会话索引可显示在神经图；原文不会进入长期记忆或 bootstrap。</p></div>${historyBackfillLoadingPanel()}${renderHistoryScopeGate()}`);
    void refreshHistoryBackfillPanel();
    return;
  }
  try {
    const data = await callApi('list_history_sessions', historyScope(), 50, 0, null, '', '');
    if (data.error || data.ok === false) throw new Error(apiErrorMessage(data, '对话历史加载失败'));
    const cards = renderHistoryGrouped(data);
    setContent(`<div class="view-heading"><span class="eyebrow">Conversation History</span><h2>对话历史</h2>
      <p>会话索引会显示在神经图；原文不会进入长期记忆或 bootstrap，需在此页按需读取。</p>
      <div class="page-actions"><input id="history-search" placeholder="搜索历史" /><button class="btn" data-mg-action="history-search">搜索</button></div></div>${historyBackfillLoadingPanel()}<section class="card"><p class="muted">检索顺序：搜索摘要 → 附近时间线 → 单条原文。原始对话归属 owner；当前共享组成员可查，仅 owner 可删。</p></section><div id="history-results">${cards || '<div class="card empty-state"><p>当前治理范围还没有已索引会话，可在上方导入本机旧会话。</p></div>'}</div>`);
    void refreshHistoryBackfillPanel();
    if (historyFocusSessionId) {
      const sessionId = historyFocusSessionId;
      historyFocusSessionId = '';
      await readHistorySession(sessionId);
    }
  } catch (e) { setContent(`<div class="view-heading"><span class="eyebrow">Conversation History</span><h2>对话历史</h2><p>对话历史加载失败。</p></div><div class="card empty-state"><p>对话历史加载失败：${escapeHtml(e.message || e)}</p></div>`); }
}

async function searchHistory() {
  const query = document.getElementById('history-search')?.value?.trim();
  if (!query) return;
  const box = document.getElementById('history-results');
  try {
    const data = await callApi('search_history', query, historyScope(), 20, 0);
    if (data.error) throw new Error(data.error);
    box.innerHTML = (data.results || []).map(r => {
      const anchor = r.anchor_turn_id || r.turn_id || '';
      const actions = r.can_timeline && anchor
        ? `<button class="btn" data-mg-action="history-timeline" data-session-id="${escapeHtml(r.session_id)}" data-turn-id="${escapeHtml(anchor)}">查看附近记录</button><button class="btn" data-mg-action="history-read-turn" data-turn-id="${escapeHtml(anchor)}">读取此条原文</button>`
        : `<button class="btn" data-mg-action="history-read-session" data-session-id="${escapeHtml(r.session_id)}">读取所属会话</button>`;
      return `<article class="memory-card"><div class="memory-card-top"><strong>${escapeHtml(r.title || '未命名会话')}</strong><span class="chip">${escapeHtml(r.result_type || 'turn')}</span></div><p>${escapeHtml(r.matched_summary || r.summary || '匹配到历史记录')}</p><div class="muted">${escapeHtml(r.provider || '')} · ${escapeHtml(r.turn_created_at || r.created_at || '')}</div><div class="finding-actions">${actions}</div></article>`;
    }).join('') || '<div class="card empty-state"><p>没有匹配历史。</p></div>';
  } catch (e) { showToast('历史搜索失败：' + e, 'error'); }
}

async function showHistoryTimeline(sessionId, turnId) {
  try {
    const data = await callApi('history_timeline', sessionId, turnId, historyScope(), 4);
    if (data.error) throw new Error(data.error);
    const rows = (data.turns || []).map(t => `<div class="memory-card"><strong>${escapeHtml(t.role)}</strong><p>${escapeHtml(t.content_preview)}</p></div>`).join('');
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button><h3>附近时间线</h3>${rows}`;
  } catch (e) { showToast('时间线读取失败：' + e, 'error'); }
}

async function readHistoryTurn(turnId) {
  try {
    const data = await callApi('history_read', buildHistoryReadRequest({turnId, limit: 1, offset: 0}));
    if (data.error || data.ok === false) throw new Error(historyErrorMessage(data, '原文读取失败'));
    const t = data.turn || {};
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button><article class="memory-card"><strong>${escapeHtml(t.role || '')}</strong><pre class="raw-content">${escapeHtml(t.content || '')}</pre></article>`;
  } catch (e) { showToast('原文读取失败：' + e, 'error'); }
}

async function readHistorySession(sessionId) {
  try {
    const data = await callApi('history_read', buildHistoryReadRequest({sessionId, limit: 100, offset: 0}));
    if (data.error || data.ok === false) throw new Error(historyErrorMessage(data, '会话读取失败'));
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button>${(data.turns || []).map(t => `<article class="memory-card"><strong>${escapeHtml(t.role || '')}</strong><pre class="raw-content">${escapeHtml(t.content || '')}</pre></article>`).join('')}`;
  } catch (e) { showToast('会话读取失败：' + e, 'error'); }
}

async function previewHistoryExtract(sessionId) {
  try {
    const data = await callApi('history_extract_preview', {session_id: String(sessionId || '').trim(), limit: 8});
    if (data.error || data.ok === false) throw new Error(historyErrorMessage(data, '萃取预览失败'));
    const rows = (data.candidates || []).map(c => `<article class="memory-card"><strong>${escapeHtml(c.title)}</strong><p>${escapeHtml(c.body)}</p><div class="muted">证据：${escapeHtml(c.evidence?.turn_id || '')}</div></article>`).join('');
    document.getElementById('history-results').innerHTML = `<button class="btn" data-mg-action="history-back">返回会话</button><p class="muted">以下仅为候选预览，尚未写入长期记忆。</p>${rows || '<div class="card empty-state"><p>没有可萃取内容。</p></div>'}`;
  } catch (e) { showToast('萃取预览失败：' + e, 'error'); }
}

async function exportHistorySession(sessionId) {
  try {
    const data = await callApi('export_history', {session_ids: [String(sessionId || '').trim()]});
    if (data.error || data.ok === false) throw new Error(historyErrorMessage(data, '历史导出失败'));
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `memoryguard-history-${sessionId}.json`;
    link.click();
    URL.revokeObjectURL(url);
  } catch (e) { showToast('历史导出失败：' + e, 'error'); }
}

async function deleteHistorySession(sessionId) {
  if (!confirm('只删除这份历史？关联长期记忆不会自动删除。')) return;
  try {
    // Delete is one atomic provenance action: the backend always tombstones
    // valid evidence before removing raw history, so no cancel branch can
    // leave a valid link whose source no longer exists.
    const data = await callApi('delete_history', [sessionId], historyScope(), true, true);
    if (data.error) throw new Error(data.error);
    showToast('历史已删除；长期记忆未删除。', 'success'); renderHistory();
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

async function setHostHookMode(provider, agentId, mode) {
  const verb = mode === 'paused' ? '暂停' : (mode === 'observe' ? '观察' : '强制');
  if (!confirm(`确认将 ${provider} 的 MemoryGuard Hook 切换为“${verb}”模式？`)) return;
  const result = await callApi('set_host_hook_mode', provider, agentId, mode, true);
  if (!result || result.error || result.ok === false) {
    return showToast((result && result.error) || 'Hook 模式更新失败', 'error');
  }
  showToast(`Hook 已切换为 ${mode}`, 'success');
  await renderMultiAgentBinding();
}

async function uninstallHostHook(provider) {
  if (!confirm(`确认卸载 ${provider} 的 MemoryGuard 用户级 Hook？\n\n只删除 MemoryGuard 自己的 handler；MCP、规则和其他 Hook 保留。`)) return;
  const result = await callApi('uninstall_host_hook', provider, true);
  if (!result || result.error || result.ok === false) {
    return showToast((result && result.error) || 'Hook 卸载失败', 'error');
  }
  showToast('MemoryGuard Hook 已卸载', 'success');
  await renderMultiAgentBinding();
}

async function createSharedBinding() {
  const checks = document.querySelectorAll('input[type=checkbox][data-agent-id]:checked');
  const agentIds = Array.from(checks).map(c => c.dataset.agentId);
  if (agentIds.length < 2) return showToast('多 Agent 共享组至少需要选择 2 个 Agent', 'error');
  if (!confirm(`确认创建共享组绑定？\n\n· ${agentIds.length} 个 Agent 通过 MemoryGuard MCP 共享同一组记忆\n· 原生记忆模式默认为 redirected（MCP 接管）\n· 创建后建议：导入原生记忆 → 安装 MCP 重定向 → 构建投影 → 确认正式接管\n\n继续？`)) return;
  showToast('正在创建绑定…');
  try {
    const result = await waitForMutation(
      await callApi('bind_agents_to_shared_group', agentIds),
      '创建共享组绑定',
    );
    if (result.error) return showToast(result.error, 'error');
    const groupId = result.share_group_id
      || (result.scope && result.scope.share_group_id)
      || '';
    if (!groupId) {
      return showToast('绑定已返回，但没有共享组 ID；请刷新绑定列表确认结果', 'error');
    }
    if (result.scope_persisted) {
      activeShareGroupId = groupId;
      dataPageMode = 'multi_agent_shared_mcp';
    } else {
      await setActiveShareGroup(groupId);
    }
    showToast(`已创建共享组，绑定 ${agentIds.length} 个 Agent`, 'success');
    showSharedGroupPreview(groupId, result.preview);
  } catch (e) { showToast('创建失败：' + e, 'error'); }
}

async function activateShareGroup(groupId) {
  await setActiveShareGroup(groupId);
  showToast(`已切换治理范围到共享组 ${groupId.slice(0, 12)}…`, 'success');
  switchTab('neurons');
}

async function previewSharedGroup(groupId) {
  showToast('正在加载共享组预览…');
  try {
    const result = await callApi('get_shared_group_preview', groupId);
    if (result.error) return showToast(result.error, 'error');
    showSharedGroupPreview(groupId, result);
  } catch (e) { showToast('加载失败：' + e, 'error'); }
}

function showSharedGroupPreview(groupId, preview) {
  const bindings = (preview && preview.bindings) || [];
  const memStatus = (preview && preview.memory_status) || {};
  const bindingsHtml = bindings.length ? bindings.map(b => `<article class="plan-item verified">
    <div class="finding-header">
       <span class="finding-rule">${escapeHtml(agentDisplayName(b.agent_instance_id))}</span>
      <span class="chip chip-${b.status === 'active' ? 'confirmed' : 'medium'}">${escapeHtml(b.status || '')}</span>
    </div>
    <div class="row"><span class="key">binding_id</span><code>${escapeHtml(b.binding_id || '')}</code></div>
    <div class="row"><span class="key">native_mode</span><span>${escapeHtml(b.native_memory_mode || '')}</span></div>
    <div class="row"><span class="key">mcp_server</span><span>${escapeHtml(b.mcp_server_name || '')}</span></div>
    <div class="row"><span class="key">redirect_paths</span><span>${(b.redirect_paths || []).length} 个</span></div>
  </article>`).join('') : '<div class="empty-state"><p>无绑定</p></div>';
  setContent(`<div class="view-heading"><span class="eyebrow">Shared group preview</span><h2>共享组预览</h2>
    <p>共享组 <code>${escapeHtml(groupId)}</code> · ${preview.agent_count || 0} 个 Agent</p></div>
    <section class="card"><div class="card-head"><div><h2>状态摘要</h2></div></div>
      <div class="chips">
        <span class="chip chip-info">agents · ${preview.agent_count || 0}</span>
        <span class="chip chip-confirmed">auto_writes · ${preview.auto_write_count || 0}</span>
        <span class="chip chip-medium">auto_decisions · ${preview.auto_decision_count || 0}</span>
        <span class="chip chip-high">conflicts · ${preview.conflict_count || 0}</span>
        <span class="chip chip-high">quarantine · ${preview.quarantine_count || 0}</span>
      </div>
    </section>
    <section class="card"><div class="card-head"><div><h2>绑定详情</h2></div></div>
      ${bindingsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="importNativeMemoriesToGroup('${escapeHtml(groupId)}')">导入原生记忆</button>
        <button class="btn" type="button" onclick="installSharedGroupMcpRedirects('${escapeHtml(groupId)}')">安装 MCP 重定向</button>
        <button class="btn" type="button" onclick="activateShareGroup('${escapeHtml(groupId)}')">设为治理范围并打开神经图</button>
        <button class="btn btn-danger" type="button" onclick="dissolveSharedGroup('${escapeHtml(groupId)}')">解散共享组</button>
        <button class="btn" type="button" onclick="renderMultiAgentBinding()">← 返回多 Agent 模式</button>
      </div>
    </section>`);
}

async function dissolveSharedGroup(groupId) {
  if (!groupId) return showToast('缺少共享组 ID', 'error');
  if (!confirm(`确认解散共享组？\n\n· 解绑共享组内全部 Agent\n· 移除匹配的 MemoryGuard Hook 条目\n· 将每位原成员返回其个人记忆层\n· 受管数据保留为仅审计 tombstone\n\n继续？`)) return;
  showToast('正在解散共享组…');
  try {
    const result = await callApi('dissolve_shared_group', groupId, true, true);
    if (!result || result.error || result.ok === false) {
      return showToast(apiErrorMessage(result, '解散共享组失败'), 'error');
    }
    if (activeShareGroupId === groupId) {
      activeShareGroupId = '';
      dataPageMode = 'multi_agent_shared_mcp';
    }
    const n = result.unbound_count || 0;
    showToast(`共享组已解散：已解绑 ${n} 个 Agent，已移除匹配的 MemoryGuard Hook 条目，所有原成员已返回个人记忆层；受管数据保留为仅审计 tombstone`, 'success');
    renderMultiAgentBinding();
  } catch (e) { showToast('解散共享组失败：' + (e.message || e), 'error'); }
}

async function exitMultiAgentMode() {
  try {
    if (activeAgentInstanceId) {
      const result = await waitForMutation(
        await callApi('set_governance_scope', {
          mode: 'agent',
          agent_instance_id: activeAgentInstanceId,
        }),
        '退出多 Agent 范围',
      );
      if (!result || result.error || result.ok === false) {
        return showToast((result && result.error) || '治理范围保存失败', 'error');
      }
    }
    dataPageMode = 'single_agent';
    activeShareGroupId = '';
  } catch (_) {}
  showToast('已退回单 Agent 模式');
  renderSources();
}


async function discoverAgents() {
  showToast('正在检测本机 Agent…');
  try {
    const [result, groupsResult, bindingsResult] = await Promise.all([
      callApi('discover_agents'),
      callApi('list_share_groups'),
      callApi('list_bindings'),
    ]);
    if (result.error) return showToast(result.error, 'error');
    discoveryResult = result;
    showDiscoveryResult(result, groupsResult, bindingsResult);
  } catch (e) {
    showToast('检测失败：' + e, 'error');
  }
}

function showDiscoveryResult(result, groupsResult = {}, bindingsResult = {}) {
  const instances = result.instances || [];
  const ledger = result.discovery_ledger || {};
  const knownProfileCount = Number(result.known_profile_count || 0);
  const knownProducts = Array.isArray(result.known_products) ? result.known_products : [];
  const activeBindings = ((bindingsResult && bindingsResult.bindings) || [])
    .filter(binding => binding.status === 'active');
  const existingGroups = ((groupsResult && groupsResult.groups) || [])
    .filter(group => String(group.share_group_id || group.group_id || '').trim());
  const instanceNameById = new Map(instances.map(item => [item.instance_id, agentDisplayName(item)]));
  const groupLabel = (group) => {
    const kind = group.group_kind === 'personal' ? '个人' : '共享';
    const memberNames = [...new Set((group.members || []).map(id => instanceNameById.get(id) || agentDisplayName(id)))].filter(Boolean);
    const members = Number(group.member_count || (group.members || []).length || 0);
    const records = Number(group.active_records || group.active_count || group.record_count || 0);
    return `${kind} · ${memberNames.join('、') || '未显示成员'} · ${members} Agent · ${records} 条记忆`;
  };
  const groupSelector = (agentId) => {
    if (!existingGroups.length) return '<span class="surface-meta">暂无已有记忆组，可新建个人记忆层。</span>';
    const options = existingGroups.map(group => {
      const groupId = String(group.share_group_id || group.group_id || '');
      return `<option value="${escapeHtml(groupId)}">${escapeHtml(groupLabel(group))}</option>`;
    }).join('');
    return `<label class="surface-meta" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span>已有记忆组</span>
      <select class="scope-select" data-existing-group-agent="${escapeHtml(agentId)}" aria-label="为 ${escapeHtml(agentDisplayName(agentId))} 选择已有记忆组">${options}</select>
      <button class="btn btn-primary" type="button" onclick="bindSelectedExistingGroup('${escapeHtml(agentId)}')">接入已有记忆组</button>
    </label>`;
  };

  // 聚合所有 surface 按 scope 分组：user / project
  const SCOPE_LABEL = { user: '全局/用户', project: '项目' };
  const scopeOrder = ['user', 'project'];
  const surfacesByScope = { user: [], project: [] };
  instances.forEach(inst => {
    (inst.surfaces || []).forEach(s => {
      const scope = s.scope || 'project';
      if (!surfacesByScope[scope]) surfacesByScope[scope] = [];
      surfacesByScope[scope].push({ ...s, product: inst.product, instance_id: inst.instance_id });
    });
  });

  const surfaceRowHtml = (s) => {
    const state = s.status || 'unknown';
    const role = s.surface_role || '';
    const cat = (s.category || '').replace(/_/g, ' ');
    return `<div class="surface-row" title="${escapeHtml(s.surface_id || '')}">
      <span class="surface-icon ${escapeHtml(state)}"></span>
      <div class="surface-path">
        <code>${escapeHtml(guiPathText(s.resolved_path || s.path_template, '受保护来源'))}</code>
        <div class="surface-meta">${escapeHtml(s.product || '')} · ${escapeHtml(s.surface_id || '')} · ${escapeHtml(role)}</div>
      </div>
      <span class="chip chip-${state==='found'?'confirmed':state==='missing'?'medium':state==='unsupported'?'medium':'info'}">${escapeHtml(state)}</span>
      <span class="chip chip-info">${escapeHtml(cat)}</span>
    </div>`;
  };

  const scopeSectionsHtml = scopeOrder.map(scope => {
    const list = surfacesByScope[scope] || [];
    const foundCount = list.filter(s => s.status === 'found').length;
    return `<details class="card" style="margin-bottom:12px">
      <summary class="card-head" style="cursor:pointer"><div><h2>${SCOPE_LABEL[scope] || scope}</h2>
        <p>${foundCount} / ${list.length} 个表面已发现 · 点击展开</p></div></summary>
      ${list.length ? list.map(surfaceRowHtml).join('') : '<div class="empty-state" style="min-height:80px"><p>此层级无候选表面</p></div>'}
    </details>`;
  }).join('');

  // Agent 实例摘要
  const LIFECYCLE_LABEL = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '仅数据残留', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
  const LIFECYCLE_CHIP = { installed: 'confirmed', installed_no_data: 'info', data_only: 'medium', uncertain: 'info', ignored: 'low', not_detected: 'low' };
  const SUPPORT_CHIP = { A: 'confirmed', B: 'info', C: 'medium', D: 'high' };
  const instancesHtml = instances.length ? instances.map(inst => {
    const foundCount = (inst.surfaces || []).filter(s => s.status === 'found').length;
    const totalCount = (inst.surfaces || []).length;
    const lifecycle = inst.lifecycle_state || inst.install_state || 'pending';
    const binding = activeBindings.find(item => sameNonEmptyAgentId(item.agent_instance_id, inst.instance_id));
    const discoveryOnly = Object.prototype.hasOwnProperty.call(inst, 'install_confidence')
      && finiteNumber(inst.install_confidence, 0) <= 0
      && inst.target_capability === 'export_only';
    const lifecycleLabel = binding
      ? (binding.group_kind === 'personal' ? '已启用个人层' : '已绑定共享组')
      : (discoveryOnly ? '已发现 · 待接入' : (LIFECYCLE_LABEL[lifecycle] || lifecycle));
    const lifecycleChip = binding ? 'confirmed' : (discoveryOnly ? 'info' : (LIFECYCLE_CHIP[lifecycle] || 'info'));
    const supportLevel = inst.support_level || '';
    const supportChip = SUPPORT_CHIP[supportLevel] || 'info';
    const nativeMemoryCount = (inst.surfaces || []).filter(surface =>
      surface.status === 'found' && (
        surface.category === 'native_memory' || surface.evidence_role === 'private_data_evidence'
      )
    ).length;
    const bindingControls = binding
      ? `<span class="chip chip-confirmed">已接入${binding.group_kind === 'personal' ? '个人' : '共享'}记忆组</span>
         <button class="btn btn-primary" type="button" onclick="viewMemoryLayer('${escapeHtml(binding.share_group_id)}')">进入记忆层</button>`
      : `${groupSelector(inst.instance_id)}
         <button class="btn" type="button" onclick="ensurePersonalLayer('${escapeHtml(inst.instance_id)}')">新建个人记忆层</button>`;
    return `<article class="plan-item verified">
      <div class="finding-header">
        <span class="finding-rule">${escapeHtml(agentDisplayName(inst))}</span>
        <span class="chip chip-confirmed">${foundCount}/${totalCount} 表面</span>
        <span class="chip chip-${lifecycleChip}">${escapeHtml(lifecycleLabel)}</span>
        ${supportLevel ? `<span class="chip chip-${supportChip}">支持 ${escapeHtml(supportLevel)}</span>` : ''}
        <span class="chip chip-info">${escapeHtml(agentCapabilityLabel(inst.target_capability))}</span>
      </div>
      <div class="row"><span class="key">profile</span><code>${escapeHtml(inst.profile_id || '')}</code></div>
      <div class="row"><span class="key">platform</span><span>${escapeHtml(inst.platform || '')} · ${escapeHtml(inst.host_id || '')}</span></div>
      <div class="surface-meta">MemoryGuard 接入与原生记忆相互独立；无原生记忆不影响接入。${nativeMemoryCount ? `检测到 ${nativeMemoryCount} 个可选原生记忆来源。` : '当前未发现原生记忆。'}</div>
      <div class="finding-actions">
        ${bindingControls}
        <button class="btn" type="button" onclick="selectAgentInstance('${escapeHtml(inst.instance_id)}')">导入原生记忆（可选）</button>
        ${nativeMemoryCount ? `<button class="btn" type="button" onclick="showResidualCleanup('${escapeHtml(inst.instance_id)}')">原生数据管理</button>` : ''}
      </div>
    </article>`;
  }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>未检测到可接入 Agent。可手工添加文件/文件夹。</p></div>';

  setContent(`<div class="view-heading"><span class="eyebrow">Discovery</span><h2>本机 Agent 检测</h2>
    <p>有限候选发现：只检测已注册 Profile 声明的固定路径，不递归扫描用户主目录，候选阶段不读取正文。未登记的新产品不会被猜测扫描，可通过外部 Profile 或手工来源接入。</p></div>
    <section class="card"><div class="card-head"><div><h2>Agent 摘要</h2><p>${instances.length} 个 Agent · ${activeBindings.length} 个已接入 · ${existingGroups.length} 个已有记忆组 · 已注册 ${knownProfileCount} 个 Profile${knownProducts.length ? ` · ${escapeHtml(knownProducts.join('、'))}` : ''}</p></div>
      <div class="finding-actions"><button class="btn" type="button" onclick="enterMultiAgentMode()">管理已有记忆组</button></div></div>
      ${instancesHtml}</section>
    ${scopeSectionsHtml}
    <section class="card"><div class="card-head"><div><h2>发现账本</h2>
      <p>所有已知表面 100% 进入账本，unaccounted 必须为 0</p></div></div>
      <div class="chips">
        <span class="chip chip-confirmed">found · ${ledger.found || 0}</span>
        <span class="chip chip-medium">missing · ${ledger.missing || 0}</span>
        <span class="chip chip-high">unsupported · ${ledger.unsupported || 0}</span>
        <span class="chip chip-${(ledger.unaccounted_count || 0) === 0 ? 'confirmed' : 'high'}">unaccounted · ${ledger.unaccounted_count || 0}</span>
        <span class="chip chip-info">total · ${ledger.surface_count || 0}</span>
      </div></section>
    <div class="finding-actions" style="margin-top:14px">
      <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
    </div>`);
}

async function selectAgentInstance(instanceId) {
  showToast('正在加载分类勾选树…');
  try {
    const result = await callApi('get_selection_tree', instanceId);
    if (result.error) return showToast(result.error, 'error');
    activeAgentInstanceId = instanceId;
    showSelectionTree(instanceId, result);
  } catch (e) { showToast('加载失败：' + e, 'error'); }
}

async function showResidualCleanup(instanceId) {
  setContent('<div class="loading">正在加载残留数据…</div>');
  showToast('正在加载残留数据…');
  try {
    const result = await callApi('get_residual_cleanup', instanceId);
    if (result.error) {
      setContent(`<div class="view-heading"><span class="eyebrow">Residual</span><h2>残留与清理</h2></div>
        <div class="card empty-state"><div><div class="empty-orb"></div>
        <p>加载失败：${escapeHtml(result.error)}</p></div></div>
        <div class="finding-actions"><button class="btn" type="button" onclick="discoverAgents()">返回检测</button></div>`);
      showToast(result.error, 'error');
      return;
    }
    const items = result.items || [];
    const candidateId = result.candidate_id || '';
    const productName = result.product || instanceId;
    const lifecycleLabel = { installed: '已安装', installed_no_data: '已安装无数据', data_only: '仅数据残留', uncertain: '待确认', ignored: '已忽略', not_detected: '未检测到' };
    const lifecycle = lifecycleLabel[result.lifecycle_state] || result.lifecycle_state || '';
    const installEvHtml = (result.install_evidence || []).map(e => `<div class="row"><span class="key">${escapeHtml(e.probe_type)}</span><span style="color:${e.found ? 'var(--accent)' : 'var(--danger)'}">${e.found ? '命中' : '未命中'}: ${escapeHtml(e.detail || '')}</span></div>`).join('');
    const dataEvHtml = (result.data_evidence || []).map(e => `<div class="row"><span class="key">${escapeHtml(guiPathText(e.dir_path, '受保护来源'))}</span><span>${e.exists ? `${e.file_count} 文件` : '不存在'}</span></div>`).join('');
    const itemsHtml = items.length ? items.map((it, idx) => {
      const preview = it.archive_preview || {};
      const previewOk = preview.ok !== false;
      const safeIdx = idx;
      const itemRef = typeof it.path_ref === 'string' ? it.path_ref : '';
      const itemLabel = guiPathLabel(it.path, '受保护来源');
      return `<article class="plan-item" data-candidate-id="${escapeHtml(candidateId)}" data-item-path="${escapeHtml(itemRef)}" data-instance-id="${escapeHtml(instanceId)}">
        <div class="finding-header">
          <span class="finding-rule">${escapeHtml(itemLabel)}</span>
          <span class="chip chip-info">${escapeHtml(it.residual_type || '')}</span>
        </div>
        <div class="finding-evidence">${escapeHtml(it.description || '')}</div>
        ${preview.error ? `<div style="color:var(--danger);font-size:11px;margin-top:4px">归档预检失败：${escapeHtml(preview.error)}</div>` : previewOk ? `<div style="color:var(--accent);font-size:11px;margin-top:4px">可归档（dry-run 通过）</div>` : ''}
        <div class="finding-actions" style="margin-top:8px">
          <button class="btn btn-primary" type="button" onclick="archiveResidualByIdx(${safeIdx})">归档此项</button>
          <button class="btn" type="button" onclick="openResidualFolderByIdx(${safeIdx})">打开文件夹</button>
        </div>
      </article>`;
    }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>无残留数据。</p></div>';
    const archivesHtml = (result.archives || []).slice(0, 10).map((a, idx) => `<div class="plan-item" data-archive-id="${escapeHtml(a.archive_id || '')}" data-instance-id="${escapeHtml(instanceId)}">
      <div class="finding-header">
        <span class="finding-rule">${escapeHtml(agentDisplayName(a))} · ${escapeHtml(a.original_path || '')}</span>
        <span class="chip chip-info">${escapeHtml(a.archive_id || '')}</span>
      </div>
      <div class="finding-evidence">归档原因: ${escapeHtml(a.reason || '')} · 归档时间: ${escapeHtml(a.archived_at || '')}</div>
      <div class="finding-actions" style="margin-top:8px">
        <button class="btn" type="button" onclick="restoreArchivedByIdx(${idx})">恢复</button>
        <button class="btn" type="button" style="border-color:var(--danger);color:var(--danger)" onclick="deleteArchivedByIdx(${idx})">永久删除</button>
      </div>
    </div>`).join('');
    setContent(`<div class="view-heading"><span class="eyebrow">Residual</span><h2>残留与清理</h2>
      <p><strong>${escapeHtml(productName)}</strong> · ${escapeHtml(lifecycle)} · candidate: <code>${escapeHtml(candidateId)}</code></p></div>
      <section class="card"><div class="card-head"><div><h2>安装证据</h2><p>${(result.install_evidence || []).length} 条探针</p></div></div>
        ${installEvHtml || '<div class="empty-state"><p>无安装探针配置。</p></div>'}</section>
      <section class="card"><div class="card-head"><div><h2>数据残留</h2><p>${items.length} 个残留项 · 可归档或打开文件夹手动处理</p></div></div>
        ${itemsHtml}</section>
      ${dataEvHtml ? `<section class="card"><div class="card-head"><div><h2>数据证据明细</h2></div></div>${dataEvHtml}</section>` : ''}
      ${archivesHtml ? `<section class="card"><div class="card-head"><div><h2>归档历史</h2><p>可恢复或永久删除</p></div></div>${archivesHtml}</section>` : ''}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="ensurePersonalLayer('${escapeHtml(instanceId)}')">启用个人记忆层</button>
        <button class="btn" type="button" onclick="discoverAgents()">返回检测</button>
      </div>`);
  } catch (e) {
    setContent(`<div class="view-heading"><span class="eyebrow">Residual</span><h2>残留与清理</h2></div>
      <div class="card empty-state"><div><div class="empty-orb"></div>
      <p>加载异常：${escapeHtml(String(e))}</p></div></div>
      <div class="finding-actions"><button class="btn" type="button" onclick="discoverAgents()">返回检测</button></div>`);
    showToast('加载失败：' + e, 'error');
  }
}

async function archiveResidualByIdx(idx) {
  const articles = document.querySelectorAll('.plan-item[data-candidate-id]');
  const el = articles[idx];
  if (!el) return showToast('未找到残留项', 'error');
  const candidateId = el.getAttribute('data-candidate-id');
  const dirPath = el.getAttribute('data-item-path');
  const instanceId = el.getAttribute('data-instance-id');
  if (!candidateId || !dirPath) return showToast('缺少归档参数', 'error');
  if (!confirm('确认归档此目录到 MemoryGuard 可恢复归档区？\n' + dirPath)) return;
  showToast('正在归档…');
  try {
    const result = await callApi('archive_agent_dir', '', dirPath, '', candidateId, false);
    if (result.error) return showToast('归档失败：' + result.error, 'error');
    showToast('归档成功，可从归档历史恢复');
    showResidualCleanup(instanceId);
  } catch (e) {
    showToast('归档失败：' + e, 'error');
  }
}

async function openResidualFolderByIdx(idx) {
  const articles = document.querySelectorAll('.plan-item[data-candidate-id]');
  const el = articles[idx];
  if (!el) return showToast('未找到残留项', 'error');
  const candidateId = el.getAttribute('data-candidate-id');
  const dirPath = el.getAttribute('data-item-path');
  if (!candidateId || !dirPath) return showToast('缺少打开参数', 'error');
  try {
    const result = await callApi('open_agent_folder', dirPath, candidateId);
    if (result.error) return showToast('打开失败：' + (result.reason || result.error), 'error');
    showToast('已打开文件夹，请在系统文件管理器中手动处理', 'info');
  } catch (e) {
    showToast('打开失败：' + e, 'error');
  }
}

async function restoreArchivedByIdx(idx) {
  const items = document.querySelectorAll('.plan-item[data-archive-id]');
  const el = items[idx];
  if (!el) return showToast('未找到归档项', 'error');
  const archiveId = el.getAttribute('data-archive-id');
  const instanceId = el.getAttribute('data-instance-id');
  if (!archiveId) return showToast('缺少归档 ID', 'error');
  if (!confirm('确认恢复此归档到原路径？\n' + archiveId)) return;
  showToast('正在恢复…');
  try {
    const result = await callApi('restore_archived_agent', archiveId);
    if (result.error) return showToast('恢复失败：' + result.error, 'error');
    showToast('恢复成功');
    showResidualCleanup(instanceId);
  } catch (e) {
    showToast('恢复失败：' + e, 'error');
  }
}

async function deleteArchivedByIdx(idx) {
  const items = document.querySelectorAll('.plan-item[data-archive-id]');
  const el = items[idx];
  if (!el) return showToast('未找到归档项', 'error');
  const archiveId = el.getAttribute('data-archive-id');
  const instanceId = el.getAttribute('data-instance-id');
  if (!archiveId) return showToast('缺少归档 ID', 'error');
  if (!confirm('永久删除此归档？此操作不可恢复！\n' + archiveId)) return;
  showToast('正在删除…');
  try {
    const result = await callApi('delete_archived_agent', archiveId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast('删除失败：' + (result.reason || result.error), 'error');
    showToast('已永久删除');
    showResidualCleanup(instanceId);
  } catch (e) {
    showToast('删除失败：' + e, 'error');
  }
}

function showSelectionTree(instanceId, tree) {
  const MEMORY_SELECT_CATS = new Set(['native_memory', 'project_memory']);
  const EXTRACT_DISPLAY_CATS = new Set(['conversation_history', 'runtime_evidence', 'knowledge_source']);
  const extractFiles = [];
  const scopeTabs = `
    <div class="scope-tabs">
      <div class="scope-tab active" data-scope="all" onclick="filterSelectionScope('all')">全部</div>
      <div class="scope-tab" data-scope="user" onclick="filterSelectionScope('user')">全局/用户</div>
      <div class="scope-tab" data-scope="project" onclick="filterSelectionScope('project')">项目</div>
      <div class="scope-tab" data-scope="unknown" onclick="filterSelectionScope('unknown')">未归属</div>
    </div>`;
  const scopeLabels = { user: '全局/用户', project: '项目', unknown: '未归属' };
  const scopeSourceLabels = { profile_declared: 'Profile声明', project_resolver: '项目解析器', fallback: '默认' };

  function collectExtractFiles(categories) {
    for (const cat of categories || []) {
      if (!EXTRACT_DISPLAY_CATS.has(cat.category)) continue;
      for (const f of cat.files || []) extractFiles.push({ ...f, category: cat.category });
    }
  }
  function renderScopeCategories(categories, scope, projectRef) {
    let html = '';
    for (const cat of categories || []) {
      if (EXTRACT_DISPLAY_CATS.has(cat.category)) continue;
      if (!MEMORY_SELECT_CATS.has(cat.category)) continue;
      html += renderSelectionCategory(cat, scope, projectRef);
    }
    return html;
  }

  let treeHtml = '';
  for (const scopeObj of (tree.scopes || [])) {
    const scope = scopeObj.scope;
    const scopeLabel = scopeLabels[scope] || scope;
    const scopeSourceLabel = scopeSourceLabels[scopeObj.scope_source] || scopeObj.scope_source || '';
    const projects = scope === 'project' ? (scopeObj.projects || []) : [
      ...((scopeObj.projects || [])),
    ];
    if (scope !== 'project' && (scopeObj.categories || []).length) {
      collectExtractFiles(scopeObj.categories);
      const catHtml = renderScopeCategories(scopeObj.categories, scope);
      if (catHtml) {
        treeHtml += `<div class="selection-group" data-scope="${escapeHtml(scope)}">
          <div class="finding-header" style="margin:14px 0 8px">
            <span class="finding-rule">${scopeLabel}</span>
            <span class="chip chip-info">${escapeHtml(scopeSourceLabel)}</span>
          </div>${catHtml}</div>`;
      }
    }
    for (const proj of projects) {
      collectExtractFiles(proj.categories);
      const catHtml = renderScopeCategories(proj.categories, scope, proj.project_ref);
      if (!catHtml) continue;
      treeHtml += `<div class="selection-group" data-scope="${escapeHtml(scope)}" data-project="${escapeHtml(proj.project_ref || '')}">
        <div class="finding-header" style="margin:14px 0 8px">
          <span class="finding-rule">${scopeLabel}${proj.project_ref ? ' · ' + escapeHtml(proj.project_ref) : ''}</span>
          <span class="chip chip-info">${escapeHtml(proj.scope_source || scopeObj.scope_source || '')}</span>
        </div>${catHtml}</div>`;
    }
  }

  const extractSection = renderExtractFileSection(extractFiles);
  const notes = (tree.discovery_notes || []).map(n => {
    const chip = n.level === 'warn' ? 'high' : 'info';
    return `<article class="plan-item"><div class="finding-header">
      <span class="finding-rule">${escapeHtml(n.code || 'note')}</span>
      <span class="chip chip-${chip}">${escapeHtml(n.level || 'info')}</span></div>
      <p>${escapeHtml(n.message || '')}</p>
      ${n.hint ? `<div class="surface-meta">${escapeHtml(n.hint)}</div>` : ''}
    </article>`;
  }).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Selection</span><h2>分类勾选授权</h2>
    <p>上方只勾选 Agent 长期记忆。下方会话/知识文档可点开萃取。Skill 与控制面不展示。</p></div>
    <section class="card">
      <div class="card-head"><div><h2>授权摘要</h2><p>Agent：${escapeHtml(tree.product || instanceId)}</p></div></div>
      <div class="row"><span class="key">记忆勾选</span><span>原生记忆 · 项目记忆</span></div>
      <div class="row"><span class="key">可萃取</span><span>会话 · 知识文档（不勾选）</span></div>
      <div class="row"><span class="key">不展示</span><span>Skill · 控制面（规则层，非记忆）</span></div>
    </section>
    ${notes ? `<section class="card"><div class="card-head"><div><h2>发现提示</h2></div></div>${notes}</section>` : ''}
    <section class="card"><div class="card-head"><div><h2>记忆来源勾选</h2></div></div>
      ${scopeTabs}
       ${treeHtml || `<div class="empty-state"><p>该 Agent 没有可勾选的原生长期记忆文件；这不影响接入 MemoryGuard 个人记忆层。</p><div class="finding-actions" style="margin-top:12px"><button class="btn btn-primary" type="button" onclick="ensurePersonalLayer('${escapeHtml(instanceId)}')">启用个人记忆层</button></div></div>`}
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="confirmSelection('${escapeHtml(instanceId)}')">确认授权</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>
    ${extractSection}`);
}

function renderExtractFileSection(files) {
  if (!files.length) return '';
  const catTitles = { conversation_history: '会话历史', knowledge_source: '知识文档', runtime_evidence: '运行证据' };
  const byCat = {};
  files.forEach(f => { const c = f.category || 'unknown'; (byCat[c] ||= []).push(f); });
  const html = Object.keys(byCat).map(cat => {
    const list = byCat[cat];
    const rows = list.slice(0, 12).map(f => {
      const rawPath = typeof f.path === 'string' ? f.path : '';
      const sourceRef = typeof f.source_root_id === 'string' ? f.source_root_id : '';
      const extractRef = rawPath || sourceRef;
      const p = escapeHtml(extractRef).replaceAll("'", "\\'");
      const canExtract = !!extractRef;
      const clickAttr = canExtract ? ` onclick="extractSourceFileByPath('${p}')"` : '';
      const label = guiPathLabel(f.path || f.relative_path || f.display_name, '受保护来源');
      return `<div class="raw-file-row" style="${canExtract ? 'cursor:pointer' : 'cursor:default;opacity:.72'};grid-template-columns:1fr auto"${clickAttr}>
        <span><code>${escapeHtml(label)}</code>
          <div class="surface-meta">${escapeHtml(f.session_title || catTitles[cat] || cat)}</div></span>
        <span class="chip chip-${canExtract ? 'info' : 'medium'}">${canExtract ? '萃取' : '路径已保护'}</span></div>`;
    }).join('');
    return `<div style="margin-bottom:12px"><div class="finding-header"><span class="finding-rule">${escapeHtml(catTitles[cat] || cat)}</span>
      <span class="chip chip-info">${list.length} 个</span></div><div class="raw-file-list">${rows}</div></div>`;
  }).join('');
  return `<section class="card"><div class="card-head"><div><h2>可萃取来源</h2>
    <p>点开进入萃取预览，确认后才写入长期记忆。</p></div></div>${html}</section>`;
}

async function extractSourceFileByPath(absPath) {
  if (!absPath) return;
  try {
    // 优先走已授权 SourceRoot；否则对已发现的会话/证据路径直接预览萃取
    const data = await callApi('list_sources');
    const sources = data.sources || [];
    const full = String(absPath).replace(/\\/g, '/');
    const hit = sources.find(s => {
      const p = String(s.path || '').replace(/\\/g, '/');
      return full.startsWith(p + '/') || full === p;
    });
    if (hit && hit.root_id) {
      const root = String(hit.path).replace(/\\/g, '/');
      const rel = full.startsWith(root + '/') ? full.slice(root.length + 1) : '';
      await extractSourceFile(hit.root_id, rel);
      return;
    }
    const result = await callApi(
      'extract_preview_by_path',
      absPath,
      activeAgentInstanceId || '',
      20,
    );
    if (result.error) return showToast(result.error, 'error');
    showExtractPreviewByPath(absPath, result);
  } catch (e) { showToast('萃取失败：' + e, 'error'); }
}

function showExtractPreviewByPath(absPath, result) {
  const candidates = result.candidates || [];
  if (!candidates.length) {
    showToast('未萃取到可用候选', 'info');
    return;
  }
  const rows = candidates.map(c => `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto">
    <input type="checkbox" data-candidate-id="${escapeHtml(c.candidate_id)}" checked>
    <div><code>${escapeHtml(c.kind || '')}</code><div class="surface-meta">${escapeHtml(c.preview || c.body || '')}</div></div>
    <span class="chip chip-${c.risk_level === 'high' ? 'high' : c.risk_level === 'medium' ? 'medium' : 'info'}">${escapeHtml(c.risk_level || 'low')}</span>
  </label>`).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Extract preview</span><h2>会话/证据萃取预览</h2>
    <p>来自发现路径（无需先勾选授权）。确认后写入共享记忆库。</p></div>
    <section class="card"><div class="card-head"><div><h2>文件</h2></div></div>
      <code style="overflow-wrap:anywhere">${escapeHtml(absPath)}</code></section>
    <section class="card"><div class="card-head"><div><h2>候选 ${candidates.length}</h2></div></div>
      <div class="raw-file-list">${rows}</div>
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="acceptExtractByPath('${escapeHtml(result.extract_id)}')">接受所选候选</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>`);
}

async function acceptExtractByPath(extractId) {
  const checks = document.querySelectorAll('input[type=checkbox][data-candidate-id]:checked');
  const ids = Array.from(checks).map(c => c.dataset.candidateId);
  if (!ids.length) return showToast('请至少选择一个候选', 'error');
  const gid = activeShareGroupId || 'default';
  showToast('正在写入候选…');
  try {
    const result = await callApi('accept_candidates', extractId, ids, gid, activeAgentInstanceId || 'document-extractor');
    if (result.error) return showToast(result.error, 'error');
    showToast(`已写入 ${result.accepted || ids.length} 条候选`, 'success');
    renderSources();
  } catch (e) { showToast('写入失败：' + e, 'error'); }
}

function renderSelectionCategory(cat, scope, projectRef) {
  projectRef = projectRef || '';
  const catLabels = {
    native_memory: '原生记忆', control_surface: '控制面', skill_surface: 'Skill 表面',
    conversation_history: '会话历史', runtime_evidence: '运行证据', knowledge_source: '知识来源',
    unknown: '其他', project_memory: '项目记忆',
  };
  const scopeTags = { user: '全局', project: '项目', unknown: '未归属' };
  const catLabel = catLabels[cat.category] || cat.category;
  const files = (cat.files || []).map(f => {
    const checked = f.default_selected ? 'checked' : '';
    const persisted = f.saved_selected === true ? '已保存勾选' : f.saved_selected === false ? '已保存取消' : '默认建议';
    const persistedChip = f.saved_selected === true ? 'confirmed' : f.saved_selected === false ? 'medium' : 'info';
    const fScope = f.scope || scope;
    const scopeSource = f.scope_source || '';
    const fProjectRef = f.project_ref || projectRef;
    const discoveryId = f.discovery_object_id || '';
    const sourceRootId = typeof f.source_root_id === 'string' ? f.source_root_id : '';
    const displayPath = guiPathText(f.path, '受保护来源');
    const confidence = f.confidence != null ? (f.confidence * 100).toFixed(0) + '%' : '';
    const reason = f.default_reason || '';
    const scopeTag = scopeTags[fScope] || fScope;
    const metaParts = [scopeTag, catLabel];
    if (reason) metaParts.push(escapeHtml(reason));
    if (confidence) metaParts.push('置信度 ' + confidence);
    return `<label class="raw-file-row" style="cursor:pointer">
      <input type="checkbox" data-selectable="true" data-cat="${escapeHtml(cat.category)}" data-source-root-id="${escapeHtml(sourceRootId)}" data-scope="${escapeHtml(fScope)}" data-scope-source="${escapeHtml(scopeSource)}" data-project-ref="${escapeHtml(fProjectRef)}" data-discovery-object-id="${escapeHtml(discoveryId)}" ${checked}>
      <span class="raw-file-path">
        <code>${escapeHtml(displayPath)}</code>
        <div class="surface-meta">${metaParts.join(' · ')}</div>
      </span>
      <span class="chip chip-${persistedChip}">${escapeHtml(persisted)}</span>
      <span class="chip chip-${f.ingestion_policy === 'import_verbatim' ? 'confirmed' : 'info'}">${escapeHtml(f.ingestion_policy || '')}</span>
    </label>`;
  }).join('');
  return `<div style="margin-bottom:14px">
    <div class="finding-header"><span class="finding-rule">${escapeHtml(catLabel)}</span>
      <span class="chip chip-info">${(cat.files || []).length} 个文件</span></div>
    <div class="raw-file-list">${files}</div>
  </div>`;
}

function filterSelectionScope(scope) {
  document.querySelectorAll('.scope-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.scope === scope);
  });
  document.querySelectorAll('.selection-group').forEach(g => {
    if (scope === 'all') {
      g.style.display = '';
    } else {
      g.style.display = g.dataset.scope === scope ? '' : 'none';
    }
  });
}

async function confirmSelection(instanceId) {
  const checks = document.querySelectorAll('input[type=checkbox][data-selectable="true"]:checked');
  const selected = Array.from(checks).map(c => ({
    category: c.dataset.cat,
    source_root_id: c.dataset.sourceRootId || '',
    scope: c.dataset.scope || 'project',
    scope_source: c.dataset.scopeSource || 'fallback',
    project_ref: c.dataset.projectRef || '',
    discovery_object_id: c.dataset.discoveryObjectId || ''
  }));
  if (!selected.length) return showToast('请至少勾选一个记忆来源（原生/项目记忆）', 'error');
  showToast('正在写入 SelectionManifest…');
  try {
    const result = await callApi('commit_selection', instanceId, selected, true);
    if (result.error) return showToast(result.error, 'error');
    showToast(`已授权 ${selected.length} 个文件，可去神经图构建投影`, 'success');
    renderSources();
  } catch (e) { showToast('授权失败：' + e, 'error'); }
}

async function addSourceDialog() {
  // 使用系统目录选择器（pywebview）或文件选择器
  const result = await callApi('pick_path');
  if (!result || result.error || !result.path) {
    if (result && result.error && result.error !== 'cancelled') showToast(result.error, 'error');
    return;
  }
  const path = result.path;
  const isDir = result.is_directory;
  // 显示名称由后端从已选择路径自动推导，避免重复手工录入。
  const name = '';
  showToast('正在添加来源…');
  try {
    const addResult = await callApi('add_source', path, isDir ? 'directory' : 'file', name, true);
    if (addResult.error) return showToast(addResult.error, 'error');
    showToast('来源已添加', 'success');
    renderSources();
  } catch (e) { showToast('添加失败：' + e, 'error'); }
}

async function importBundleDialog() {
  const result = await callApi('pick_path', true);
  if (!result || result.error || !result.path) {
    if (result && result.error && result.error !== 'cancelled') showToast(result.error, 'error');
    return;
  }
  const path = result.path;
  showToast('正在检测导出包…');
  try {
    const preview = await callApi('preview_import', path);
    if (preview.error) {
      showToast(preview.error, 'error');
      return;
    }
    showImportPreview(path, preview);
  } catch (e) {
    showToast('检测失败：' + e, 'error');
  }
}

async function importBundlePrompt() {
  // 兼容旧调用：转发到 importBundleDialog
  await importBundleDialog();
}

function showImportPreview(path, preview) {
  const inv = preview.inventory || {};
  const invText = Object.keys(inv).map(k => `· ${k}: ${JSON.stringify(inv[k]).slice(0, 200)}`).join('\n');
  setContent(`<div class="view-heading"><span class="eyebrow">Import preview</span><h2>离线导出包预览</h2>
    <p>检测到的 provider 和 inventory。会话内容默认只作为证据/萃取来源，不直接写入长期记忆。</p></div>
    <section class="card">
      <div class="card-head"><div><h2>检测结果</h2></div>
        <span class="chip chip-confirmed">${escapeHtml(preview.provider || 'unknown')}</span></div>
      <div class="row"><span class="key">path</span><code style="overflow-wrap:anywhere">${escapeHtml(path)}</code></div>
      <div class="row"><span class="key">confidence</span><span>${(preview.confidence || 0) * 100}%</span></div>
      <div class="row"><span class="key">notes</span><span>${escapeHtml(preview.notes || '')}</span></div>
    </section>
    <section class="card">
      <div class="card-head"><div><h2>Inventory</h2><p>检测到的会话/文件清单</p></div></div>
      <pre class="raw-file-content">${escapeHtml(invText || '(empty)')}</pre>
    </section>
    <section class="card">
      <div class="gate-warning">
        <strong>会话不会默认写入 Memory IR。</strong><br>
        原始文件不会被修改；需要像文档一样先萃取候选，用户接受后才进入长期记忆。
      </div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="confirmImport('${escapeHtml(path).replaceAll("'", "\\'")}')">确认导入</button>
        <button class="btn" type="button" onclick="renderSources()">取消</button>
      </div>
    </section>`);
}

async function confirmImport(path) {
  if (!confirm('确认解析此导出包？\n· 会话只作为证据/萃取来源\n· 不直接写入长期记忆\n· 原始文件不被修改')) return;
  showToast('正在解析…');
  try {
    const result = await callApi('create_import', path, true, activeAgentInstanceId || '', '', activeShareGroupId || '');
    if (result.error) {
      showToast(result.error, 'error');
      return;
    }
    showToast(`解析完成：${result.conversation_count} 个会话，未直接写入长期记忆`, 'success');
    renderSources();
  } catch (e) {
    showToast('导入失败：' + e, 'error');
  }
}

async function addSourcePrompt() {
  // 兼容旧调用：转发到 addSourceDialog
  await addSourceDialog();
}

async function removeSourceCard(button) {
  await removeSource(button.dataset.sourceId || '', button.dataset.sourceName || '');
}

async function removeSource(rootId, displayName = '') {
  if (!rootId) return showToast('来源标识缺失，无法删除', 'error');
  const label = displayName || rootId;
  if (!confirm('删除知识库映射“' + label + '”？\n仅移除 MemoryGuard 映射，磁盘中的原文件不会被删除。')) return;
  try {
    const result = await callApi('remove_source', rootId, true);
    if (result.error) return showToast(result.error, 'error');
    if (!result.ok) return showToast('来源不存在或不可删除', 'error');
    showToast('知识库映射已删除，原文件未改动', 'success');
    renderSources();
  } catch (e) { showToast('移除失败：' + e, 'error'); }
}

async function viewSourceFile(rootId, relativePath) {
  if (!rootId) return showToast('该条目只是发现结果，尚未授权为可读取来源', 'info');
  try {
    const result = await callApi('get_source_file_content', rootId, relativePath);
    if (result.error) {
      showToast(result.error === 'file not found' ? '文件已不存在，请刷新数据源' : result.error, 'error');
      if (result.error === 'file not found' || result.error === 'source root not found') renderSources();
      return;
    }
    const escaped = escapeHtml(result.content);
    const lines = result.content.split('\n').length;
    const extractArgs = escapeHtml(JSON.stringify([String(rootId || ''), String(relativePath || '')]));
    setContent(`<div class="view-heading"><span class="eyebrow">Raw memory</span><h2>${escapeHtml(result.display_name)} · ${escapeHtml(relativePath).replaceAll('\\', '/')}</h2>
      <p>原始记忆文件，只读查看。size=${result.size} bytes · lines=${lines}</p></div>
      <section class="card"><div class="card-head"><div>
        <button class="btn" type="button" onclick="renderSources()">← 返回数据源</button>
        <button class="btn btn-primary" type="button" onclick="extractSourceFile(...${extractArgs})">萃取为实用记忆</button>
      </div></div>
      <pre class="raw-file-content">${escaped}</pre></section>`);
  } catch (e) { showToast('读取失败：' + e, 'error'); }
}

async function extractSourceFile(rootId, relativePath) {
  showToast('正在萃取预览…');
  try {
    const result = await callApi('extract_preview', rootId, relativePath);
    if (result.error) return showToast(result.error, 'error');
    showExtractPreview(rootId, relativePath, result);
  } catch (e) { showToast('萃取失败：' + e, 'error'); }
}

function showExtractPreview(rootId, relativePath, result) {
  const candidates = result.candidates || [];
  const riskLabel = { high: '高风险', medium: '中风险', low: '低风险' };
  const riskChip = { high: 'high', medium: 'medium', low: 'confirmed' };
  const itemsHtml = candidates.length ? candidates.map((c, i) => `<label class="raw-file-row" style="cursor:pointer;grid-template-columns:auto 1fr auto;align-items:flex-start">
      <input type="checkbox" data-candidate-id="${escapeHtml(c.candidate_id)}" checked>
      <div>
        <div class="finding-rule">#${i + 1} · ${escapeHtml(c.kind || '')}</div>
        <div class="finding-evidence" style="white-space:pre-wrap;margin-top:4px">${escapeHtml(c.preview || c.body || '').slice(0, 300)}</div>
      </div>
      <span class="chip chip-${riskChip[c.risk_level] || 'info'}">${escapeHtml(riskLabel[c.risk_level] || c.risk_level)}</span>
    </label>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>未识别到可萃取的记忆片段。</p></div>';
  const safePath = escapeHtml(relativePath).replaceAll('\\', '/').replaceAll("'", "\\'");
  const extractId = escapeHtml(result.extract_id || '');
  setContent(`<div class="view-heading"><span class="eyebrow">Extract preview</span><h2>萃取候选预览</h2>
    <p>从 <code>${escapeHtml(relativePath).replaceAll('\\', '/')}</code> 提取出 ${result.total || 0} 条候选记忆。请审阅后选择接受哪些。</p></div>
    <section class="card"><div class="card-head"><div><h2>候选列表</h2>
      <p>勾选要接受的候选，取消勾选将丢弃。风险等级标注敏感内容或低置信度。</p></div></div>
      <div class="raw-file-list">${itemsHtml}</div>
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn btn-primary" type="button" onclick="acceptExtractCandidates('${extractId}','${escapeHtml(rootId)}','${safePath}')">接受选中</button>
        <button class="btn btn-danger" type="button" onclick="discardExtractCandidates('${escapeHtml(rootId)}','${safePath}')">全部丢弃</button>
      </div>
    </section>`);
}

async function acceptExtractCandidates(extractId, rootId, relativePath) {
  const checks = document.querySelectorAll('input[type=checkbox][data-candidate-id]:checked');
  const candidateIds = Array.from(checks).map(c => c.dataset.candidateId);
  if (!candidateIds.length) return showToast('请至少勾选一个候选', 'error');
  if (!confirm('确认接受 ' + candidateIds.length + ' 条候选记忆并写入共享记忆？')) return;
  showToast('正在写入记忆…');
  try {
    const result = await callApi('accept_candidates', extractId, candidateIds);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已接受 ' + result.total + ' 条记忆', 'success');
    showAcceptResult(rootId, relativePath, result);
  } catch (e) { showToast('写入失败：' + e, 'error'); }
}

function discardExtractCandidates(rootId, relativePath) {
  showToast('已丢弃全部候选，staging 文件将在 24 小时后自动清理', 'success');
  viewSourceFile(rootId, relativePath);
}

function showAcceptResult(rootId, relativePath, result) {
  const accepted = result.accepted || [];
  const itemsHtml = accepted.length ? accepted.map((m, i) => `<article class="plan-item verified">
    <div class="finding-header">
      <span class="finding-rule">#${i + 1} · ${escapeHtml(m.kind || '')}</span>
      <span class="chip chip-${m.status === 'active' ? 'confirmed' : 'medium'}">${escapeHtml(m.status || '')}</span>
    </div>
    <div class="row"><span class="key">memory_id</span><code>${escapeHtml(m.memory_id || '')}</code></div>
  </article>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>无写入记录。</p></div>';
  const viewArgs = escapeHtml(JSON.stringify([String(rootId || ''), String(relativePath || '')]));
  setContent(`<div class="view-heading"><span class="eyebrow">Accept</span><h2>写入结果</h2>
    <p>已接受 ${result.total || 0} 条记忆，写入共享组 <code>${escapeHtml(result.share_group_id || 'default')}</code>。</p></div>
    <section class="card"><div class="card-head"><div><h2>写入的记忆</h2></div></div>
      ${itemsHtml}
      <div class="finding-actions" style="margin-top:14px">
        <button class="btn" type="button" onclick="viewSourceFile(...${viewArgs})">← 返回源文件</button>
        <button class="btn btn-primary" type="button" onclick="renderSources()">返回数据源</button>
      </div>
    </section>`);
}

// ===========================================================================
// v3 变更记录 tab：BuildPlan + ReleaseChange + apply/verify/rollback
// spec §7.1 / §7.2 MemoryApi
// ===========================================================================

async function renderReleases() {
  setContent('<div class="loading">正在读取变更记录</div>');
  try {
    // v3.1 §8.4：用 list_history 替代 list_releases，包含 rule_change + memory_release + warnings
    const historyResult = await callApi('list_history');
    state.releases = (historyResult.items || []).filter(it => it.record_type === 'memory_release');
    state.ruleChanges = (historyResult.items || []).filter(it => it.record_type === 'rule_change');
    state.historyWarnings = historyResult.warnings || [];
    document.getElementById('releases-count').textContent = historyResult.items ? historyResult.items.length : 0;
    renderReleasesView();
  } catch (e) {
    showToast('变更记录加载失败：' + e, 'error');
    state.releases = [];
    state.ruleChanges = [];
    state.historyWarnings = [];
    renderReleasesView();
  }
}

function renderReleasesView() {
  // 规则级修复（spec §11.1 保留 v2.1）
  const plans = state.plans || [];
  // v3.1 §8.4：state.ruleChanges 来自旧 changes/ 的兼容读取
  const ruleChanges = state.ruleChanges || [];
  const warnings = state.historyWarnings || [];

  const warningsHtml = warnings.length ? `<section class="card">
    <div class="card-head"><div><h2>历史解析警告</h2>
      <p>${warnings.length} 条历史记录无法解析，单条坏记录不阻断页面</p></div></div>
    ${warnings.map(w => `<div class="raw-file-row" style="cursor:default">
      <span class="raw-file-path"><code>${escapeHtml(w.file_name)}</code></span>
      <span class="chip chip-high">${escapeHtml(w.reason)}</span>
      ${w.missing_keys && w.missing_keys.length ? `<span class="chip chip-medium">missing: ${escapeHtml(w.missing_keys.join(', '))}</span>` : ''}
    </div>`).join('')}
  </section>` : '';

  const plansHtml = plans.length ? plans.map(plan => {
    const status = plan.change ? plan.change.status : 'pending';
    const statusClass = status === 'verified' ? 'confirmed' : status === 'failed' ? 'high' : 'info';
    return `<article class="plan-item ${escapeHtml(status)}"><div class="finding-header">
      <span class="finding-rule">修复计划 ${escapeHtml(plan.plan_id.slice(-8))}</span><span class="chip chip-${statusClass}">${escapeHtml(status)}</span></div>
      <div class="finding-evidence">处理 ${plan.finding_ids.length} 个风险信号 · ${plan.patches.length} 个补丁 · 风险 ${escapeHtml(plan.risk_level)}</div>
      <div class="finding-actions">
        ${status === 'pending' ? `<button class="btn btn-primary" type="button" onclick="applyPlan('${escapeHtml(plan.plan_id)}')">应用并验证</button>` : ''}
        ${status === 'verified' || status === 'applied' ? `<button class="btn btn-danger" type="button" onclick="undoChange('${escapeHtml(plan.change.change_id)}','${escapeHtml(plan.plan_id)}')">撤销变更</button>` : ''}
      </div></article>`;
  }).join('') : (ruleChanges.length ? ruleChanges.map(rc => `<article class="plan-item">
    <div class="finding-header">
      <span class="finding-rule">规则修复 ${escapeHtml((rc.event_id || '').slice(-8))}</span>
      <span class="chip chip-info">rule_change · schema ${escapeHtml(rc.schema_version || '2.1')}</span>
    </div>
    <div class="row"><span class="key">plan_id</span><code>${escapeHtml(rc.plan_id || '')}</code></div>
    <div class="row"><span class="key">applied_at</span><span>${escapeHtml(rc.applied_at || '')}</span></div>
    <div class="row"><span class="key">status</span><span>${escapeHtml(rc.status || '')}</span></div>
  </article>`).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>没有规则级修复记录。</p></div>');

  setContent(`<div class="view-heading"><span class="eyebrow">Changes</span><h2>变更记录</h2>
    <p>这里只记录 MemoryGuard 治理层的规则修复。原生记忆仅用于扫描与投影，文件保持只读。</p></div>
    ${warningsHtml}
    <section class="card"><div class="card-head"><div><h2>规则级修复</h2><p>对单个 Finding 生成最小补丁，备份后应用，重扫验证，可撤销</p></div></div>
      ${plansHtml}</section>
    <section class="card"><div class="gate-warning" style="margin-top:0">
      <strong>只读边界：</strong>治理结果写入 MemoryGuard 管理层与共享记忆库，不覆盖任何 Agent 原生记忆文件。
    </div></section>`);
}

async function ensurePersonalLayer(agentId) {
  if (!confirm('确认启用该 Agent 的个人记忆层并安装全局 MCP + Hook？\n\n· 创建 MemoryGuard 管理的个人数据库\n· 写入该 Agent 的用户级 MCP、Hook 与记忆规则\n· 原生记忆文件保持只读\n· 已有共享绑定不会被自动切换\n· 安装后按提示重启/信任 Hook')) return;
  const result = await callApi('ensure_personal_memory_group', agentId, true);
  if (result && result.error) return showToast(result.error, 'error');
  const gid = result.group_id || result.share_group_id || (result.binding && result.binding.share_group_id);
  if (gid) {
    const install = await callApi('install_shared_group_mcp_redirects', gid, true);
    if (!install || install.error || install.ok === false) {
      const detail = install && (install.error || `${install.error_count || 0} 失败，${install.skipped_count || 0} 跳过`);
      showToast('个人记忆层已创建，但 MCP 未完整安装：' + (detail || '未知错误'), 'error');
    } else {
      showToast(`个人记忆层与 MCP 已配置；Hook ${install.hook_configured_count || 0} 个。请按提示重启/信任`, 'success');
    }
  } else {
    showToast('个人记忆层返回结果缺少组 ID', 'error');
  }
  if (dataPageMode === 'multi_agent_shared_mcp') await renderMultiAgentBinding(); else await renderSources();
}

async function bindSelectedExistingGroup(agentId) {
  const select = Array.from(document.querySelectorAll('select[data-existing-group-agent]'))
    .find(item => item.dataset.existingGroupAgent === agentId);
  const groupId = String(select?.value || '').trim();
  if (!groupId) return showToast('请选择已有记忆组', 'error');
  const label = memoryGroupLabel(groupId);
  if (!confirm(`确认将 ${agentDisplayName(agentId)} 绑定到已有${label}？\n\n· 该 Agent 当前个人/共享绑定会切换到此组\n· 不删除原记忆组，也不改写原生记忆文件\n· 绑定后更新该组 MCP + Hook 配置\n\n继续？`)) return;
  showToast('正在绑定到已有记忆组…');
  try {
    const result = await waitForMutation(
      await callApi('bind_agent', agentId, groupId, 'memoryguard', 'redirected', []),
      '绑定已有记忆组',
    );
    if (!result || result.error || result.ok === false) {
      return showToast((result && result.error) || '已有记忆组绑定失败', 'error');
    }
    const install = await callApi('install_shared_group_mcp_redirects', groupId, true);
    if (!install || install.error || install.ok === false) {
      const detail = install && (install.error || `${install.error_count || 0} 失败，${install.skipped_count || 0} 跳过`);
      showToast(`已绑定已有${label}，但 MCP + Hook 未完整更新：${detail || '未知错误'}`, 'error');
    } else {
      showToast(`已绑定到已有${label}，MCP + Hook 已更新；请按提示重启/信任`, 'success');
    }
    await renderMultiAgentBinding();
  } catch (error) {
    showToast('绑定已有记忆组失败：' + error, 'error');
  }
}

async function installMemoryGroupMcp(groupId) {
  const label = memoryGroupLabel(groupId);
  if (!confirm(`确认重新安装${label}的全局 MCP + Hook？\n\n· 会更新用户级 MCP、Hook 与记忆规则\n· 不改写原生记忆文件\n· 完成后按提示重启/信任 Hook`)) return;
  const result = await callApi('install_shared_group_mcp_redirects', groupId, true);
  if (!result || result.error || result.ok === false) {
    const detail = result && (result.error || `${result.error_count || 0} 失败，${result.skipped_count || 0} 跳过`);
    return showToast('MCP 未完整安装：' + (detail || '未知错误'), 'error');
  }
  showToast(`MCP 已配置，Hook ${result.hook_configured_count || 0} 个；请按提示重启/信任`, 'success');
}

async function viewMemoryLayer(groupId) {
  await setActiveShareGroup(groupId);
  switchTab('neurons');
}

async function leaveSharedToPersonal(agentId) {
  if (!confirm('确认退出共享组并回到个人记忆层？\n\n· 两边记忆不会合并或删除\n· 会更新该 Agent 的全局 MCP 规则指向个人层\n· 完成后需要重启对应 Agent')) return;
  const result = await callApi('leave_shared_group_to_personal', agentId, true);
  if (result && result.error) return showToast(result.error, 'error');
  const gid = result.group_id || result.share_group_id || (result.binding && result.binding.share_group_id);
  const install = gid ? await callApi('install_shared_group_mcp_redirects', gid, true) : null;
  if (!install || install.error || install.ok === false) {
    const detail = install && (install.error || `${install.error_count || 0} 失败，${install.skipped_count || 0} 跳过`);
    showToast('已切换到个人记忆层，但 MCP 未完整更新：' + (detail || '缺少个人组 ID'), 'error');
  } else {
    showToast(`已回到个人记忆层并更新 MCP + Hook（${install.hook_configured_count || 0} 个）；请按提示重启/信任`, 'success');
  }
  if (dataPageMode === 'multi_agent_shared_mcp') await renderMultiAgentBinding(); else await renderSources();
}

async function exportMemoryGroup(groupId) {
  if (!confirm(`确认导出${memoryGroupLabel(groupId)}？\n\n导出包包含记忆、事件、治理历史、完整版本快照、binding 清单和来源文件映射；不包含原生文件正文。`)) return;
  const result = await callApi('export_memory_group', groupId, true);
  if (!result || result.error) return showToast((result && result.error) || '导出失败', 'error');
  setContent(`<div class="view-heading"><span class="eyebrow">Memory export</span><h2>记忆层已导出</h2>
    <p>${memoryGroupLabel(groupId)} <code>${escapeHtml(groupId)}</code></p></div>
    <section class="card"><div class="card-head"><div><h2>导出包</h2><p>原生文件未包含，也未被修改。</p></div></div>
      <div class="row"><span class="key">路径</span><code>${escapeHtml(result.export_path || '')}</code></div>
      <div class="row"><span class="key">记录</span><span>${escapeHtml((result.counts && result.counts.records) || 0)}</span></div>
      <div class="finding-actions">
        <button class="btn btn-primary" type="button" onclick="copyText(${JSON.stringify(result.export_path || '')})">复制路径</button>
        <button class="btn" type="button" onclick="renderGovernance()">返回治理台</button>
      </div>
    </section>`);
}

async function showMemorySourceMap(groupId) {
  setContent('<div class="loading">正在解析记忆到文件的来源链</div>');
  const result = await callApi('get_memory_source_map', groupId);
  if (!result || result.error) return showToast((result && result.error) || '来源映射失败', 'error');
  const mappings = result.mappings || [];
  const rows = mappings.length ? mappings.map(item => {
    const sources = (item.sources || []).map(source => {
      const location = source.absolute_path || source.relative_path || '无本地文件';
      const state = source.origin_kind === 'local_file'
        ? (source.exists ? '文件存在' : source.path_valid ? '文件已移动/删除' : '路径无效或未授权')
        : 'MCP/衍生来源';
      return `<div class="source-item">
        <div><strong>${escapeHtml(source.display_name || source.origin_kind)}</strong></div>
        <div class="surface-meta">${escapeHtml(state)} · ${escapeHtml(source.scope || '')} · ${escapeHtml(source.locator || '')}</div>
        <code>${escapeHtml(location)}</code>
      </div>`;
    }).join('');
    return `<article class="plan-item">
      <div class="finding-header"><span class="finding-rule">${escapeHtml(item.kind)} · ${escapeHtml(item.memory_id)}</span><span class="chip chip-info">${escapeHtml(item.status)}</span></div>
      <div class="finding-evidence">${escapeHtml(item.body_preview || '')}</div>
      <div class="source-list">${sources}</div>
    </article>`;
  }).join('') : '<div class="empty-state"><div class="empty-orb"></div><p>当前记忆层为空。</p></div>';
  setContent(`<div class="view-heading"><span class="eyebrow">Source map</span><h2>记忆文件映射</h2>
    <p>${memoryGroupLabel(groupId)} · ${result.total_records || 0} 条记忆 · ${result.file_source_count || 0} 条本地文件来源</p></div>
    <section class="card"><div class="gate-warning" style="margin-top:0"><strong>映射规则：</strong>本地文件只是只读来源；MCP 对话写入没有对应文件；衍生记忆会继续追溯其上游记忆。</div></section>
    <section class="card">${rows}<div class="finding-actions"><button class="btn" type="button" onclick="renderGovernance()">返回治理台</button></div></section>`);
}

async function clearMemoryGroup(groupId) {
  if (!confirm(`确认清空${memoryGroupLabel(groupId)}？\n\n· 系统会先自动导出可恢复 ZIP\n· 清空全部记忆、事件、治理历史与版本\n· 保留 binding、MCP 配置和空数据库\n· 不修改任何原生文件\n\n此操作完成后只能从导出包恢复。`)) return;
  const result = await callApi('clear_memory_group', groupId, true);
  if (!result || result.error) return showToast((result && result.error) || '清空失败', 'error');
  showToast(`记忆层已清空；备份：${result.export_path || ''}`, 'success');
  await loadGovernanceSnapshot();
  renderGovernance();
}

async function archiveMemoryGroup(groupId) {
  if (!confirm(`确认归档删除${memoryGroupLabel(groupId)}？\n\n· 系统会先自动导出 ZIP\n· 解绑组内 Agent 并归档整个数据库目录\n· 原生文件不变\n· 现有 MCP 将因无活动 binding 而拒绝读写，直到重新启用记忆层\n\n继续？`)) return;
  const result = await callApi('archive_memory_group', groupId, true);
  if (!result || result.error) return showToast((result && result.error) || '归档失败', 'error');
  activeShareGroupId = '';
  state.governanceSnapshot = null;
  showToast(`记忆层已归档；导出：${result.export_path || ''}`, 'success');
  renderGovernance();
  renderStatusRail();
}

// ===========================================================================
// 治理台 tab：最近写入 / 覆盖记录 / 冲突队列 / 隔离队列 / 版本回滚
// ===========================================================================

function governanceGroupProgramSummary(group) {
  const item = group && typeof group === 'object' ? group : {};
  const discovered = [
    ...(Array.isArray(agentCardsData?.agents) ? agentCardsData.agents : []),
    ...(Array.isArray(agentCardsData?.program_members) ? agentCardsData.program_members : []),
  ].filter(candidate => candidate && typeof candidate === 'object');
  const byId = new Map(discovered.flatMap(candidate => {
    const keys = [candidate.instance_id, candidate.agent_instance_id, candidate.agent_id, candidate.id, candidate.binding_id]
      .map(value => String(value || '').trim()).filter(Boolean);
    return keys.map(key => [key, candidate]);
  }));
  const preferredDetails = Array.isArray(item.program_member_details) ? item.program_member_details
    : (Array.isArray(item.member_details) ? item.member_details : []);
  const rawMembers = preferredDetails.length ? preferredDetails : (item.members || item.agent_instance_ids || []);
  const members = rawMembers.map(member => {
    if (member && typeof member === 'object') return member;
    return byId.get(String(member || '').trim()) || null;
  }).filter(Boolean);
  const historicalStatuses = new Set(['historical', 'historical_unknown', 'history', 'stale', 'missing', 'unresolved', 'unknown', 'inactive', 'expired']);
  const programs = Array.from(new Map(members
    .filter(member => member.is_program_member !== false && member.program_member !== false)
    .filter(member => !historicalStatuses.has(String(member.member_status || member.lifecycle_state || member.status || '').trim().toLowerCase()))
    .filter(member => !isUnknownHistoricalMember(member))
    .map((member, index) => [agentProgramKey(member) || `program-${index}`, member])).values());
  const names = [...new Set(programs.map(member => agentDisplayName(member, '')).filter(name => name && name !== '未识别的 MCP 助手'))];
  const programCount = optionalFiniteNumber(item.program_member_count) ?? programs.length;
  const endpointCount = optionalFiniteNumber(item.member_count ?? item.endpoint_member_count);
  const declaredOtherCount = optionalFiniteNumber(item.extra_connection_count);
  const otherCount = declaredOtherCount === null
    ? (endpointCount === null ? Math.max(0, members.length - programCount) : Math.max(0, endpointCount - programCount))
    : Math.max(0, declaredOtherCount);
  const connectionCount = endpointCount === null ? programCount + otherCount : Math.max(0, endpointCount);
  const unknownCount = optionalFiniteNumber(item.unknown_member_count)
    ?? optionalFiniteNumber(item.unresolved_member_count)
    ?? members.filter(isUnknownHistoricalMember).length;
  return {names, programCount, connectionCount, otherCount, unknownCount};
}

async function renderGovernance() {
  const renderToken = takeContentRenderToken('governance');
  setContent('<div class="loading">正在读取记忆治理组</div>', renderToken);
  let groups = [];
  try {
    const [groupsResult, agentsResult] = await Promise.all([
      callApi('list_share_groups'), callApi('list_agents'),
    ]);
    groups = groupsResult.groups || [];
    agentCardsData = agentsResult || agentCardsData;
  } catch (e) {
    setContent(`<div class="card empty-state"><div><div class="empty-orb"></div><p>记忆治理组加载失败：${escapeHtml(String(e))}</p></div></div>`, renderToken);
    return;
  }
  if (activeShareGroupId && !groups.some(g => g.share_group_id === activeShareGroupId)) {
    activeShareGroupId = '';
  }
  const groupOptions = groups.map(g => {
    const summary = governanceGroupProgramSummary(g);
    const names = summary.names.length ? summary.names.join('、') : '程序名称待加载';
    const unknown = summary.unknownCount ? `（${summary.unknownCount} 待识别连接）` : '';
    const label = `${g.group_kind === 'personal' ? '个人' : '共享'} · ${names} · ${summary.programCount} 个程序 · ${summary.connectionCount} 条连接（其他 ${summary.otherCount}）${unknown} · ${g.active_records || 0} 条记忆`;
    return `<option value="${escapeHtml(g.share_group_id)}" ${g.share_group_id === activeShareGroupId ? 'selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('');
  const groupSelector = groups.length
    ? `<section class="card"><div class="card-head"><div><h2>治理范围</h2><p>选择个人或共享记忆层；所有读取和处置都严格绑定此组。</p></div></div>
        <select class="scope-select" aria-label="选择记忆治理组" onchange="selectGovernanceGroup(this.value)">
          <option value="">请选择记忆层</option>${groupOptions}
        </select></section>`
    : `<section class="card empty-state"><div><div class="empty-orb"></div><p>尚无个人或共享记忆层。</p>
        <div class="finding-actions"><button class="btn btn-primary" type="button" onclick="switchTab('sources')">去数据源启用记忆层</button></div></div></section>`;
  if (!activeShareGroupId) {
    setContent(`<div class="view-heading"><span class="eyebrow">Governance</span><h2>治理台</h2>
      <p>请先选择个人或共享记忆层，系统不会再隐式使用 default。</p></div>${groupSelector}`, renderToken);
    return;
  }
  const tabs = [
    { id: 'records', label: '记忆记录' },
    { id: 'recent_events', label: '最近写入' },
    { id: 'supersede', label: '覆盖记录' },
    { id: 'conflicts', label: '冲突队列' },
    { id: 'quarantine', label: '隔离队列' },
    { id: 'rollback', label: '版本回滚' },
  ];
  const tabsHtml = tabs.map(t =>
    `<div class="scope-tab ${t.id === governanceSubTab ? 'active' : ''}" onclick="switchGovernanceSub('${t.id}')">${t.label}</div>`
  ).join('');
  setContent(`<div class="view-heading"><span class="eyebrow">Governance</span><h2>治理台</h2>
    <p>记忆治理操作台：最近写入、覆盖记录、冲突队列、隔离队列、版本回滚。</p></div>
    ${groupSelector}
    <section class="card"><div class="card-head"><div><h2>记忆层生命周期</h2>
      <p>导出与映射不会改原生文件；清空保留绑定；归档删除会解绑并使 MCP 安全拒绝。</p></div></div>
      <div class="finding-actions">
        <button class="btn" type="button" onclick="showMemorySourceMap('${escapeHtml(activeShareGroupId)}')">文件映射</button>
        <button class="btn" type="button" onclick="exportMemoryGroup('${escapeHtml(activeShareGroupId)}')">导出记忆层</button>
        <button class="btn btn-danger" type="button" onclick="clearMemoryGroup('${escapeHtml(activeShareGroupId)}')">清空记忆</button>
        <button class="btn btn-danger" type="button" onclick="archiveMemoryGroup('${escapeHtml(activeShareGroupId)}')">归档删除记忆层</button>
      </div>
    </section>
    <div class="scope-tabs">${tabsHtml}</div>
    <div id="governance-content"><div class="loading">正在加载</div></div>`, renderToken);
  if (!contentRenderIsCurrent(renderToken)) return;
  renderGovernanceSub();
}

async function selectGovernanceGroup(groupId) {
  if (!groupId) {
    activeShareGroupId = '';
    activeScopeMemberIds = [];
    governanceScopeState = {status: 'unselected', share_group_id: '', reason: '', binding: null, members: []};
    state.governanceSnapshot = null;
    renderGovernance();
    renderStatusRail();
    return;
  }
  const groupsResult = await callApi('list_share_groups');
  const selected = (groupsResult.groups || []).find(group => group.share_group_id === groupId);
  activeScopeMemberIds = selected?.members || selected?.agent_instance_ids || [];
  await setActiveShareGroup(groupId);
  await loadGovernanceSnapshot();
  renderGovernance();
}

function switchGovernanceSub(subTab) {
  governanceSubTab = subTab;
  renderGovernance();
}

function renderGovernanceSub() {
  switch (governanceSubTab) {
    case 'records': renderMemoryRecords(); break;
    case 'recent_events': renderRecentEvents(); break;
    case 'supersede': renderSupersedeChain(); break;
    case 'conflicts': renderConflictQueue(); break;
    case 'quarantine': renderQuarantine(); break;
    case 'rollback': renderRollback(); break;
  }
}

async function renderMemoryRecords() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取记忆记录</div>';
  try {
    const result = await callApi('list_memory', '', '', activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    const records = result.records || [];
    if (!records.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>当前记忆层没有记录。</p></div></div>';
      return;
    }
    const items = records.map(rec => {
      const isAlways = rec.injection_policy === 'always';
      const active = rec.status === 'active';
      const targetPolicy = isAlways ? 'relevant' : 'always';
      const badge = isAlways ? '强制' : '按需';
      const explanation = isAlways
        ? '强制规则每任务注入'
        : '按需记忆按相关性召回';
      const action = active ? `<button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(rec.memory_id)}">管理注入范围</button>` : '';
      return `<article class="plan-item">
        <div class="finding-header"><span class="finding-rule">${escapeHtml(rec.kind || 'fact')}</span>
          <span class="chip chip-${isAlways ? 'confirmed' : 'medium'}">${badge}</span>
          <span class="chip chip-info">优先级 ${escapeHtml(String(rec.priority ?? 0))}</span>
          <span class="chip chip-medium">${escapeHtml(rec.status || '')}</span></div>
        <div class="finding-evidence">${escapeHtml((rec.body || '').slice(0, 260))}</div>
        <div class="surface-meta">${explanation}${rec.locked ? ' · 已锁定（仍可切换注入策略）' : ''}</div>
        <div class="finding-actions" style="margin-top:10px">${action}</div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>记忆记录</h2>
      <p>强制规则每任务注入；按需记忆按相关性召回。仅 active 记录可切换。</p></div></div>${items}</section>`;
  } catch (e) {
    showToast('加载记忆记录失败：' + e, 'error');
  }
}

async function toggleMemoryInjectionPolicy(memoryId, targetPolicy, priority) {
  const label = targetPolicy === 'always' ? '强制' : '按需';
  if (!confirm(`确认切换为${label}？\n\n强制规则每任务注入；按需记忆按相关性召回。`)) return;
  try {
    const result = await callApi('set_memory_injection_policy', memoryId, targetPolicy, priority, activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    if (result.ok === false) return showToast(result.blocked_reason || '切换被拒绝', 'error');
    showToast(`已切换为${label}`, 'success');
    renderMemoryRecords();
    renderStatusRail();
  } catch (e) {
    showToast('切换失败：' + e, 'error');
  }
}

async function renderRecentEvents() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取最近写入</div>';
  try {
    const result = await callApi('get_recent_events', activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    const events = result.events || [];
    if (!events.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无自动写入事件。</p></div></div>';
      return;
    }
    const items = events.map(e => {
      const preview = escapeHtml((e.raw_content || '').slice(0, 100));
      const actions = (e.auto_actions || []).map(a => `<span class="chip chip-info">${escapeHtml(eventActionLabel(a.action || a.type || 'auto'))}</span>`).join('');
      return `<article class="plan-item" onclick="toggleEventDetail('${escapeHtml(e.event_id)}')">
        <div class="finding-header">
           <span class="finding-rule">${escapeHtml(activityActorLabel(e))}</span>
          <span class="chip chip-info">${escapeHtml(e.created_at || '')}</span>
        </div>
        <div class="finding-evidence">${preview}${(e.raw_content || '').length > 100 ? '…' : ''}</div>
        ${actions ? `<div class="chips" style="margin-top:6px">${actions}</div>` : ''}
        <div class="finding-detail" id="event-detail-${escapeHtml(e.event_id)}" style="display:none">
          <div class="row"><span class="key">event_id</span><code>${escapeHtml(e.event_id || '')}</code></div>
           <div class="row"><span class="key">agent</span><span>${escapeHtml(activityActorLabel(e))}</span></div>
          <div class="row"><span class="key">group</span><span>${escapeHtml(e.share_group_id || '')}</span></div>
          <div class="row"><span class="key">时间</span><span>${escapeHtml(e.created_at || '')}</span></div>
          <div class="row"><span class="key">完整内容</span></div>
          <pre class="raw-file-content" style="max-height:300px">${escapeHtml(e.raw_content || '')}</pre>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>最近自动写入</h2>
      <p>最近 ${events.length} 条 MemoryEvent（最多 50 条）</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

function toggleEventDetail(eventId) {
  const el = document.getElementById('event-detail-' + eventId);
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

async function renderSupersedeChain() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取覆盖记录</div>';
  try {
    const result = await callApi('get_supersede_decisions', activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    const decisions = result.decisions || [];
    if (!decisions.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无自动覆盖记录。</p></div></div>';
      return;
    }
    const items = decisions.map(d => `<article class="plan-item">
      <div class="finding-header">
        <span class="finding-rule">覆盖 · ${escapeHtml(d.created_at || '')}</span>
        <span class="chip chip-medium">auto_supersede</span>
      </div>
      <div class="row" style="margin-top:8px"><span class="key">新记忆</span><code>${escapeHtml(d.new_memory_id || '')}</code></div>
      <div class="finding-evidence" style="margin-left:82px">${escapeHtml(d.new_content_preview || '(无内容)')}</div>
      <div class="row" style="margin-top:8px"><span class="key">旧记忆</span><code>${escapeHtml(d.old_memory_id || '')}</code></div>
      <div class="finding-evidence" style="margin-left:82px;color:var(--faint)">${escapeHtml(d.old_content_preview || '(无内容)')}</div>
      <div class="row" style="margin-top:8px"><span class="key">原因</span><span>${escapeHtml(d.reason || '')}</span></div>
    </article>`).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>自动覆盖记录</h2>
      <p>共 ${decisions.length} 条 auto_supersede 决策。新记忆 -> 旧记忆覆盖链。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

function conflictMemberLabel(member) {
  const item = member && typeof member === 'object' ? member : {memory_id: member};
  const id = String(item.memory_id || item.id || item.record_id || '').trim();
  const direct = [item.display_name, item.title, item.label, item.name]
    .map(value => String(value || '').trim()).find(Boolean);
  return direct || (id ? `记忆记录 ${id.slice(0, 16)}` : '未命名冲突记录');
}

function conflictActionDescriptors(conflict) {
  const raw = conflict && (conflict.available_actions || conflict.actions || conflict.recovery_actions);
  const rows = Array.isArray(raw) ? raw : [];
  const labels = {
    close_conflict: '关闭冲突', close: '关闭冲突',
    close_stale_conflict: '关闭失效冲突', conflict_close_stale: '关闭失效冲突',
    cleanup_conflict: '清理冲突', cleanup: '清理冲突',
    restore_conflict: '恢复候选', restore: '恢复候选',
    resolve_conflict: '保留所选并解决', resolve: '保留所选并解决',
  };
  const normalized = rows.map(row => {
    const item = row && typeof row === 'object' ? row : {action: row};
    const rawMethod = String(item.method || item.operation || item.action || item.id || '').trim().toLowerCase();
    const method = rawMethod === 'conflict_close_stale' ? 'close_stale_conflict' : rawMethod;
    if (!method) return null;
    return {
      method,
      label: String(item.label_zh || item.label || labels[method] || method),
      enabled: item.enabled !== false && item.available !== false,
      reason: String(item.reason || item.disabled_reason || '').trim(),
    };
  }).filter(Boolean);
  // Older V2 responses did not advertise recovery actions even though the
  // close-stale operation was already available.  Keep an explicitly stale
  // queue item actionable as a compatibility fallback; the call still goes
  // through the normal bridge, so an actually old backend returns a visible
  // API error instead of silently leaving an unusable disabled row.
  const status = String(conflict?.status || conflict?.source_status || '').trim().toLowerCase();
  const liveCount = optionalFiniteNumber(conflict?.live_member_count ?? conflict?.program_member_count);
  const stale = status !== 'ambiguous' && (['stale', 'invalid', 'expired', 'unrecoverable'].includes(status) || conflict?.can_resolve === false || (Number.isFinite(liveCount) && liveCount < 2));
  if (stale && !normalized.some(item => item.method === 'close_stale_conflict')) {
    normalized.push({method: 'close_stale_conflict', label: '关闭失效冲突', enabled: true, reason: '候选不足，保留历史审计记录并关闭该冲突'});
  }
  return normalized;
}

async function invokeConflictAction(groupId, method) {
  const supported = new Set(['close_conflict', 'close', 'close_stale_conflict', 'conflict_close_stale', 'cleanup_conflict', 'cleanup', 'restore_conflict', 'restore']);
  const requested = String(method || '').trim().toLowerCase();
  const action = requested === 'conflict_close_stale' ? 'close_stale_conflict' : requested;
  if (!supported.has(action)) return showToast('该治理动作尚未由后端提供', 'error');
  if (!confirm('确认执行“' + action + '”？\n\n该操作会更新冲突队列状态，并保留审计记录。')) return;
  showToast('正在执行冲突治理…');
  try {
    const result = action === 'close_stale_conflict'
      ? await callApi('close_stale_conflict', groupId)
      : await callApi(action, groupId, activeShareGroupId);
    if (result && (result.error || result.ok === false)) return showToast(apiErrorMessage(result, '冲突治理失败'), 'error');
    showToast('冲突治理已提交', 'success');
    renderConflictQueue();
  } catch (error) {
    showToast('冲突治理失败：' + error, 'error');
  }
}

async function renderConflictQueue() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取冲突队列</div>';
  try {
    // get_conflicts is self-contained: member snapshots come from the V2
    // atom/tombstone history.  A second list_memory request used to turn
    // deleted or compatibility members into opaque, still-selectable IDs.
    const conflictsResult = await callApi('get_conflicts', activeShareGroupId);
    if (conflictsResult.error) return showToast(conflictsResult.error, 'error');
    const conflicts = (conflictsResult.conflicts || []).filter(c => !['resolved', 'closed'].includes(String(c.status || '').toLowerCase()));
    if (!conflicts.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无未解决冲突。</p></div></div>';
      return;
    }
    const actionableCount = Number.isFinite(Number(conflictsResult.actionable_total))
      ? Number(conflictsResult.actionable_total)
      : conflicts.filter(c => c.can_resolve === true).length;
    const closableStaleCount = Number.isFinite(Number(conflictsResult.closable_stale_total))
      ? Number(conflictsResult.closable_stale_total)
      : conflicts.filter(c => ['stale', 'invalid'].includes(String(c.status || '').toLowerCase()) || c.can_resolve === false).length;
    const items = conflicts.map(c => {
      const memberRows = Array.isArray(c.members) ? c.members : (Array.isArray(c.member_details) ? c.member_details : (c.member_ids || []).map(mid => ({memory_id: mid, status: 'missing', selectable: false, live: false, reason: '旧版响应未提供成员快照，历史正文不可恢复'})));
      const liveMembers = memberRows.filter(member => member && (member.selectable === true || member.live === true));
      // Even an optimistic compatibility flag cannot override the visible
      // member evidence: never enable a resolve action without two explicit
      // live/selectable snapshots.
      const canResolve = liveMembers.length >= 2 && (c.can_resolve === true || !Object.prototype.hasOwnProperty.call(c, 'can_resolve'));
      const actions = conflictActionDescriptors(c);
      const actionButtons = actions.map(action => {
        const danger = ['close', 'close_conflict', 'close_stale_conflict', 'cleanup', 'cleanup_conflict'].includes(action.method);
        const handler = action.enabled ? `onclick="invokeConflictAction('${escapeHtml(c.group_id)}','${escapeHtml(action.method)}')"` : `disabled title="${escapeHtml(action.reason || '后端暂不可用')}"`;
        return `<button class="btn ${danger ? 'btn-danger' : ''}" type="button" data-conflict-action="${escapeHtml(action.method)}" ${handler}>${escapeHtml(action.label)}</button>`;
      }).join('');
      const members = memberRows.map(member => {
        const item = member && typeof member === 'object' ? member : {memory_id: member};
        const mid = String(item.memory_id || item.id || '');
        if (!mid) return '';
        const selectable = item.selectable === true || item.live === true;
        const preview = item.preview || item.body_preview || item.body || item.reason || (item.missing ? '历史正文不可恢复（仅保留成员 ID）' : '成员详情不可用');
        const title = conflictMemberLabel(item);
        const radio = selectable ? `<input type="radio" name="conflict-${escapeHtml(c.group_id)}" value="${escapeHtml(mid)}" aria-label="选择保留 ${escapeHtml(title)}">` : '';
        const chipClass = selectable ? 'confirmed' : 'high';
        const status = item.status || (item.missing ? 'missing' : 'unavailable');
        const details = [item.kind, item.created_at, item.reason].filter(Boolean).join(' · ');
        return `<label class="raw-file-row conflict-member-row" style="${selectable ? 'cursor:pointer;' : 'cursor:default;'}grid-template-columns:auto 1fr auto;align-items:start">
          ${radio}
          <div>
            <strong>${escapeHtml(title)}</strong>
            <div class="surface-meta">${escapeHtml(String(preview))}</div>
            <details class="conflict-technical-id"><summary>技术详情</summary><code>${escapeHtml(mid || '未提供 ID')}</code>${details ? `<div class="surface-meta">${escapeHtml(details)}</div>` : ''}</details>
          </div>
          <span class="chip chip-${chipClass}">${escapeHtml(status)}${!selectable ? ' · 不可选' : ''}</span>
        </label>`;
      }).join('');
      const isStale = !canResolve || ['stale', 'invalid'].includes(String(c.status || '').toLowerCase());
      const reasonLabels = {canonical_composition_conflict: '相关记忆的内容主张互相矛盾，需选择保留版本', explicit_composition_conflict: '相关记忆被明确标记为内容主张冲突，需选择保留版本'};
      const reasonText = reasonLabels[String(c.reason || c.reason_code || '').toLowerCase()] || c.reason || c.reason_code || '相关记忆的内容主张存在差异，需选择保留版本';
      const statusLabel = isStale ? '历史冲突 · 候选失效，可关闭' : (c.status || 'unresolved');
      const invalidReason = c.invalid_reason
        ? `${c.invalid_reason} 该组不可二选一，但可关闭以保留审计记录。`
        : '历史冲突中没有至少 2 条仍有效的记忆，不能二选一；可关闭以保留审计记录。';
      return `<article class="plan-item">
        <div class="finding-header">
          <span class="finding-rule">冲突组 ${escapeHtml((c.group_id || '').slice(0, 16))}</span>
          <span class="chip chip-high">${escapeHtml(statusLabel)}</span>
        </div>
        <div class="finding-evidence" style="margin-top:6px">原因：${escapeHtml(reasonText)}</div>
        ${c.reason_code || c.raw_reason ? `<div class="surface-meta" style="margin-top:4px">诊断原因：${escapeHtml(c.reason_code || c.raw_reason)}</div>` : ''}
        <div class="row" style="margin-top:6px"><span class="key">创建时间</span><span>${escapeHtml(c.created_at || '')}</span></div>
        <div class="raw-file-list" style="margin-top:10px">${members}</div>
        <div class="finding-actions" style="margin-top:10px">
          ${canResolve ? `<button class="btn btn-primary" type="button" onclick="resolveConflict('${escapeHtml(c.group_id)}')">保留选中并解决</button>` : `<button class="btn btn-primary" type="button" disabled title="${escapeHtml(invalidReason)}">保留选中并解决</button><span class="surface-meta">${escapeHtml(invalidReason)}</span>`}
          ${actionButtons}
        </div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>冲突队列</h2>
      <p>共 ${conflicts.length} 个未闭合冲突组，可选择保留 ${actionableCount} 组、可关闭失效 ${closableStaleCount} 组。可处理冲突选择保留哪条，其余将被软删除；失效冲突不可二选一，但可关闭并保留审计记录。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function resolveConflict(groupId) {
  const selected = document.querySelector(`input[name="conflict-${groupId}"]:checked`);
  if (!selected) return showToast('请先选择要保留的记忆', 'error');
  const keepId = selected.value;
  if (!confirm('确认解决冲突？\n\n· 保留记忆：' + keepId + '\n· 其他成员将被软删除')) return;
  showToast('正在解决冲突…');
  try {
    const result = await callApi('resolve_conflict', groupId, keepId, activeShareGroupId, true);
    if (result.error) return showToast(result.error, 'error');
    showToast('冲突已解决', 'success');
    renderConflictQueue();
  } catch (e) { showToast('解决失败：' + e, 'error'); }
}

async function renderQuarantine() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取隔离队列</div>';
  try {
    const result = await callApi('get_quarantine', activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    const entries = (result.quarantine || []).filter(e => !e.released);
    if (!entries.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>隔离队列为空。</p></div></div>';
      return;
    }
    const items = entries.map(e => {
      return `<article class="plan-item">
        <div class="finding-header">
          <span class="finding-rule">隔离 ${escapeHtml((e.quarantine_id || '').slice(0, 16))}</span>
          <span class="chip chip-high">quarantined</span>
        </div>
        <div class="row"><span class="key">memory_id</span><code>${escapeHtml(e.memory_id || '')}</code></div>
        <div class="row"><span class="key">原内容</span><span style="font-family:monospace">${escapeHtml(e.masked_preview || '••••')}</span></div>
        <div class="row"><span class="key">原因</span><span>${escapeHtml(e.reason || '')}</span></div>
        <div class="row"><span class="key">匹配模式</span><code>${escapeHtml(e.detected_pattern || '')}</code></div>
        <div class="row"><span class="key">隔离时间</span><span>${escapeHtml(e.quarantined_at || '')}</span></div>
        <div class="finding-actions" style="margin-top:10px">
          <button class="btn btn-primary" type="button" onclick="releaseQuarantine('${escapeHtml(e.quarantine_id)}')">释放</button>
          <button class="btn btn-danger" type="button" onclick="deleteQuarantine('${escapeHtml(e.quarantine_id)}')">永久删除</button>
        </div>
      </article>`;
    }).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>隔离队列</h2>
      <p>共 ${entries.length} 条未释放隔离记忆。敏感内容已脱敏显示。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function releaseQuarantine(quarantineId) {
  if (!confirm('释放此隔离记忆？\n\n· 记忆将恢复为 active 状态\n· 请确认内容安全')) return;
  showToast('正在释放…');
  try {
    const result = await callApi('release_quarantine', quarantineId, activeShareGroupId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已释放', 'success');
    renderQuarantine();
  } catch (e) { showToast('释放失败：' + e, 'error'); }
}

async function deleteQuarantine(quarantineId) {
  if (!confirm('永久删除此隔离记忆？\n\n· 记忆将被标记为 deleted\n· 此操作不可撤销')) return;
  showToast('正在删除…');
  try {
    const result = await callApi('delete_quarantine', quarantineId, activeShareGroupId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已删除', 'success');
    renderQuarantine();
  } catch (e) { showToast('删除失败：' + e, 'error'); }
}

async function renderRollback() {
  const container = document.getElementById('governance-content');
  if (!container) return;
  container.innerHTML = '<div class="loading">正在读取版本历史</div>';
  try {
    const result = await callApi('list_memory_versions', activeShareGroupId);
    if (result.error) return showToast(result.error, 'error');
    const versions = (result.versions || []).slice().sort((a, b) =>
      (b.created_at || '').localeCompare(a.created_at || ''));
    if (!versions.length) {
      container.innerHTML = '<div class="card empty-state"><div><div class="empty-orb"></div><p>暂无版本记录。</p></div></div>';
      return;
    }
    const items = versions.map(v => `<article class="plan-item">
      <div class="finding-header">
        <span class="finding-rule">版本 ${escapeHtml((v.version_id || '').slice(0, 16))}</span>
        <span class="chip chip-info">${escapeHtml(v.created_at || '')}</span>
      </div>
      <div class="row"><span class="key">version_id</span><code>${escapeHtml(v.version_id || '')}</code></div>
      <div class="row"><span class="key">原因</span><span>${escapeHtml(v.reason || '(无)')}</span></div>
      <div class="row"><span class="key">记录数</span><span>${v.record_count || 0} 条（active: ${v.active_count || 0}）</span></div>
      <div class="finding-actions" style="margin-top:10px">
        <button class="btn btn-danger" type="button" onclick="rollbackToVersion('${escapeHtml(v.version_id)}')">回滚到此版本</button>
      </div>
    </article>`).join('');
    container.innerHTML = `<section class="card"><div class="card-head"><div><h2>版本回滚</h2>
      <p>共 ${versions.length} 个版本。回滚前会自动创建当前状态快照。</p></div></div>
      ${items}</section>`;
  } catch (e) {
    showToast('加载失败：' + e, 'error');
    container.innerHTML = `<div class="card empty-state"><div><div class="empty-orb"></div><p>加载失败：${escapeHtml(String(e))}</p></div></div>`;
  }
}

async function rollbackToVersion(versionId) {
  if (!confirm('确认回滚到版本 ' + versionId.slice(0, 16) + '？\n\n· 会先备份当前状态为新版本\n· 恢复为该版本的 records 和 decisions\n· 可通过新版本再次回滚')) return;
  showToast('正在回滚…');
  try {
    const result = await callApi('rollback_memory', versionId, activeShareGroupId);
    if (result.deferred) { showToast('请求已提交到桌面执行器', 'info'); return; }
    if (result.error) return showToast(result.error, 'error');
    showToast('已回滚到目标版本', 'success');
    renderRollback();
  } catch (e) { showToast('回滚失败：' + e, 'error'); }
}

function showToast(message, type) {
  const toast = document.getElementById('toast');
  toast.textContent = message; toast.className = 'toast show ' + (type || '');
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toast.className = 'toast', 2600);
}

// ---------------------------------------------------------------------------
// Compact seven-view information architecture.  These renderers intentionally
// reuse existing reads and mutations; no browser-side aggregate is treated as
// a new source of truth.
// ---------------------------------------------------------------------------
function railTitle(title, html) {
  const heading = document.querySelector('#status-rail h3');
  const container = document.getElementById('status-rail-content');
  if (heading) heading.textContent = title;
  if (container) container.innerHTML = html;
}

function railKey(label, value, className = '') {
  return `<div class="status-item ${className}"><span class="status-label">${escapeHtml(label)}</span><span class="status-num">${escapeHtml(value)}</span></div>`;
}

function governanceCounts() {
  const counts = objectValue(state.governanceSnapshot?.counts);
  return {
    active: finiteOrNull(counts.active_memories),
    conflicts: finiteOrNull(counts.conflicts),
    selectableConflicts: finiteOrNull(counts.selectable_conflicts),
    closableStaleConflicts: finiteOrNull(counts.closable_stale_conflicts),
    quarantined: finiteOrNull(counts.quarantined),
    rollback: finiteOrNull(counts.rollback_ready),
  };
}

function conflictDispositionText(snapshot = state.governanceSnapshot) {
  const conflict = objectValue(snapshot?.conflicts);
  const selectable = finiteOrNull(conflict.selectable_total ?? conflict.actionable_total);
  const closable = finiteOrNull(conflict.closable_stale_total ?? conflict.stale_total);
  if (selectable === null && closable === null) return '';
  return `可选择保留 ${selectable === null ? '暂不可用' : selectable} · 可关闭失效 ${closable === null ? '暂不可用' : closable}`;
}

function renderOverviewRail() {
  const snap = state.governanceSnapshot || {};
  const event = snap.latest_event || null;
  const counts = governanceCounts();
  const conflictDetail = conflictDispositionText(snap);
  railTitle('最近动态', `
    ${event ? `<div class="rail-section"><h4>${escapeHtml(agentDisplayName(event.agent_instance_id, '最近写入'))}</h4><div class="rail-copy">${escapeHtml(event.raw_content_preview || '事件未返回内容摘要')}</div><div class="surface-meta">${escapeHtml(event.created_at || '')}</div></div>` : '<div class="empty-inline">暂无运行期事件。连接 Agent 后会在这里显示真实写入。</div>'}
    <div class="rail-section"><h4>快捷操作</h4><div class="finding-actions"><button class="btn btn-primary audit-refresh-button" type="button" onclick="runAudit()">重新扫描</button><button class="btn" type="button" onclick="switchTab('sources')">管理数据源</button><button class="btn" type="button" onclick="switchTab('findings')">查看风险</button></div></div>
    <div class="rail-section"><h4>治理摘要</h4>${railKey('有效记忆', counts.active === null ? '暂不可用' : String(counts.active))}<div class="status-item ${counts.conflicts ? 'alert' : 'zero'}" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('conflicts'),50)"><span class="status-label">未解决冲突</span><span><span class="status-num">${counts.conflicts === null ? '暂不可用' : counts.conflicts}</span>${conflictDetail ? `<small class="surface-meta" style="display:block;text-align:right">${escapeHtml(conflictDetail)}</small>` : ''}</span></div>${railKey('待回滚版本', counts.rollback === null ? '暂不可用' : String(counts.rollback), counts.rollback ? '' : 'zero')}</div>`);
}

function openGovernanceSubtab(subTab) {
  governanceSubTab = subTab;
  switchTab('governance');
}

function openFinding(findingId) {
  selectedFindingId = String(findingId || '');
  switchTab('findings');
}

function normalizeStageState(value) {
  const key = String(value ?? '').trim().toLowerCase().replace(/[ -]+/g, '_');
  if (['completed', 'complete', 'done', 'passed', 'success'].includes(key)) return 'completed';
  if (['current', 'active', 'running', 'in_progress'].includes(key)) return 'current';
  if (['pending', 'not_started', 'waiting', 'queued'].includes(key)) return 'pending';
  return 'unknown';
}

function governanceStageStates(report = {}) {
  const summary = objectValue(report.summary);
  const objectCount = finiteOrNull(summary.object_count);
  const stageSource = objectValue(report.stage_states || report.stages || report.phases || report.phase_states);
  const states = ['unknown', 'unknown', 'unknown', 'unknown', 'unknown', 'unknown'];
  const stageKeys = [
    ['discovery', 'discover', '发现'], ['integration', 'ingest', 'connect', '接入'],
    ['extraction', 'extract', '提取'], ['normalization', 'normalize', '规范化'],
    ['monitoring', 'monitor', '监控'], ['audit', '审计'],
  ];
  stageKeys.forEach((keys, index) => {
    for (const key of keys) {
      if (stageSource[key] !== undefined) {
        const candidate = objectValue(stageSource[key]);
        states[index] = normalizeStageState(candidate.state ?? candidate.status ?? stageSource[key]);
        break;
      }
    }
  });
  // These two facts are safe, narrow evidence: an object count means discovery
  // returned objects, and an active binding means the source is connected.
  if (states[0] === 'unknown' && objectCount !== null) states[0] = 'completed';
  if (states[1] === 'unknown' && governanceScopeState.status === 'active' && activeShareGroupId) states[1] = 'completed';
  const state = normalizeAuditState(report.audit_state || report.auditStatus || report.status);
  if (states[5] === 'unknown' && state === 'completed') states[5] = 'completed';
  if (states[5] === 'unknown' && state === 'running') states[5] = 'current';
  return states;
}

function stageStateLabel(state) {
  return ({completed: '已完成', current: '当前', pending: '待开始', unknown: '不可判定'})[state] || '不可判定';
}

// The compact renderer is the sole active overview implementation.
function renderOverview() {
  const report = state.report || {};
  const summary = objectValue(report.summary);
  const findings = Array.isArray(report.findings) ? report.findings : [];
  const snap = state.governanceSnapshot || {};
  const counts = governanceCounts();
  const health = optionalFiniteNumber(report.health_score);
  const healthUnavailable = healthEvidenceUnavailable(report);
  const completed = auditIsCompleted(report);
  const objectCount = finiteOrNull(summary.object_count);
  const trend = Array.isArray(report.risk_trend) ? report.risk_trend : (Array.isArray(report.trend) ? report.trend : []);
  const topRules = Array.isArray(report.top_rules) ? report.top_rules : [];
  const latest = [snap.latest_event, snap.latest_supersede].filter(Boolean);
  const conflictDetail = conflictDispositionText(snap);
  const stages = ['发现', '接入', '提取', '规范化', '监控', '审计'];
  const stageStates = governanceStageStates(report);
  const healthText = health === null
    ? (healthUnavailable ? healthEvidenceLabel(report) : (completed ? (findings.length ? `需处理 ${findings.length} 项` : '审计通过（未提供量化评分）') : auditStateLabel(report)))
    : (healthUnavailable ? healthEvidenceLabel(report) : `${Math.round(Math.max(0, Math.min(100, health)))}/100`);
  const riskRows = findings.slice(0, 5).map(finding => `<button type="button" aria-label="查看风险详情：${escapeHtml(riskRuleLabel(finding.rule_id, finding))}" onclick="openFinding('${escapeHtml(finding.id || '')}')"><span class="overview-risk-copy"><strong>${escapeHtml(riskRuleLabel(finding.rule_id, finding))}</strong><span>原因：${escapeHtml(riskReasonText(finding))}</span><small>建议：${escapeHtml(riskActionText(finding))}</small></span><span class="chip chip-${escapeHtml(finding.severity || 'info')}">${escapeHtml(riskSeverityLabel(finding.severity, finding))}</span></button>`).join('') || '<div class="empty-inline">当前审计未返回风险信号。</div>';
  const ruleRows = topRules.slice(0, 5).map(rule => `<button type="button" onclick="selectedRuleId='${escapeHtml(rule.memory_id || rule.rule_id || '')}';switchTab('rules')"><span>${escapeHtml(displayTitle(rule) || '未命名规则')}</span><span class="muted">${escapeHtml(rule.hit_count ?? rule.hits ?? '暂不可用')}</span></button>`).join('') || '<div class="empty-inline">当前审计未返回规则命中统计。</div>';
  const activityRows = latest.map(item => `<button type="button" onclick="switchTab('governance')"><span>${escapeHtml(activityActorLabel(item))}</span><span class="muted">${escapeHtml(item.created_at || '')}</span></button>`).join('') || '<div class="empty-inline">暂无近期活动。</div>';
  setContent(`<div class="dashboard-view overview-view"><div class="dashboard-main">
    <div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Governance overview</span><h2>总览</h2></div><span class="muted">${escapeHtml(unavailableMetric(report.generated_at, '暂无扫描时间'))}</span><button class="btn btn-primary audit-refresh-button" type="button" onclick="runAudit()">重新扫描</button></div>
    <div class="kpi-grid">
      <div class="kpi ${completed && !healthUnavailable ? '' : 'muted'}"><span>${escapeHtml(healthScopeLabel(report))}</span><strong>${escapeHtml(healthText)}</strong><small class="kpi-hint">${escapeHtml(healthCoverageText(report))}</small></div>
      <div class="kpi"><span>已识别对象</span><strong>${objectCount === null ? '暂不可用' : objectCount}</strong></div>
      <div class="kpi ${counts.conflicts ? 'alert' : ''}" role="button" tabindex="0" aria-label="打开未解决冲突队列" onclick="openGovernanceSubtab('conflicts')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openGovernanceSubtab('conflicts')}"><span>未解决冲突</span><strong>${counts.conflicts === null ? '暂不可用' : counts.conflicts}</strong><small class="kpi-hint">${escapeHtml(conflictDetail || '点击查看详情')}</small></div>
      <div class="kpi ${findings.length ? 'danger' : ''}" role="button" tabindex="0" aria-label="打开风险信号详情" onclick="switchTab('findings')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();switchTab('findings')}"><span>风险信号</span><strong>${findings.length}</strong><small class="kpi-hint">点击查看详情</small></div>
    </div>
    <section><div class="compact-toolbar"><div class="toolbar-grow"><h2>治理流程</h2><span class="muted">只展示后端明确状态；缺少证据的阶段标为不可判定。</span></div></div><div class="governance-timeline">${stages.map((label, index) => { const stage = stageStates[index]; return `<div class="governance-stage ${stage}" aria-label="${index + 1}. ${label}：${stageStateLabel(stage)}"><span class="stage-name">${index + 1}. ${label}</span><span class="stage-state">${stageStateLabel(stage)}</span></div>`; }).join('')}</div></section>
    <div class="overview-columns">
      <section><div class="compact-toolbar"><div class="toolbar-grow"><h2>风险趋势</h2></div></div>${trend.length ? `<div class="overview-list">${trend.slice(-8).map(item => `<button type="button" onclick="switchTab('findings')"><span>${escapeHtml(String(item.label || item.date || item.created_at || '审计批次'))}</span><span>${escapeHtml(String(item.count ?? item.value ?? '暂不可用'))}</span></button>`).join('')}</div>` : '<div class="empty-inline">当前 API 未返回可比较的风险趋势。</div>'}</section>
      <section><div class="compact-toolbar"><div class="toolbar-grow"><h2>最近活动</h2></div></div><div class="overview-list">${activityRows}</div></section>
    </div>
    <div class="overview-columns">
      <section><div class="compact-toolbar"><div class="toolbar-grow"><h2>规则 Top</h2><span class="muted">只展示审计已返回的命中统计。</span></div></div><div class="overview-list">${ruleRows}</div></section>
      <section><div class="compact-toolbar"><div class="toolbar-grow"><h2>风险 Top</h2></div></div><div class="overview-list">${riskRows}</div></section>
    </div>
  </div></div>`);
  renderOverviewRail();
}

function sourceStatus(source, bindings = []) {
  const rootId = String(source?.root_id || source?.source_id || source?.id || '');
  const binding = bindings.find(item => String(item?.source_id || item?.root_id || '') === rootId);
  if (binding?.status === 'active') return {label: '已接入', tone: 'confirmed', bucket: 'connected'};
  if (source?.enabled === true || source?.status === 'active' || source?.status === 'connected') return {label: '已接入', tone: 'confirmed', bucket: 'connected'};
  if (source?.path_exists === true || source?.available === true || source?.read_status === 'read') return {label: '可用', tone: 'info', bucket: 'available'};
  if (source?.path_exists === false) return {label: '路径失效', tone: 'high', bucket: 'unavailable'};
  return {label: '状态待确认', tone: 'medium', bucket: 'available'};
}

function setSourceViewTab(tab) {
  sourceViewTab = ['all', 'connected', 'available'].includes(tab) ? tab : 'all';
  if (sourcePageRecords.size) renderSourcesView(sourcePageRecords.get('__sourcesResult') || {sources: []}, sourcePageRecords.get('__rawResult') || {}, sourcePageRecords.get('__agentData') || null, sourcePageRecords.get('__bindingsResult') || {});
}

function selectSourceRecord(sourceId) {
  selectedSourceId = String(sourceId || '');
  selectedSourceRecord = sourcePageRecords.get(selectedSourceId) || null;
  document.querySelectorAll('.source-detail-row').forEach(row => row.classList.toggle('is-selected', row.dataset.sourceId === selectedSourceId));
  renderStatusRail();
}

function renderSourcesRail() {
  const source = selectedSourceRecord;
  if (source) {
    const status = sourceStatus(source, sourcePageRecords.get('__bindings') || []);
    railTitle('来源详情', `<div class="rail-section"><div class="rail-title">${escapeHtml(source.display_name || source.name || '未命名来源')}</div><span class="chip chip-${status.tone}">${escapeHtml(status.label)}</span></div>
      <div class="rail-section"><h4>类型与范围</h4><div class="rail-copy">${escapeHtml(source.type || source.source_category || '未返回类型')} · ${escapeHtml(source.scope || '未返回范围')}</div></div>
      <div class="rail-section"><h4>路径</h4><code>${escapeHtml(guiPathText(source.path, '受保护来源'))}</code></div>
      <details class="compact-secondary"><summary>技术详情</summary><div class="rail-copy">source_id: ${escapeHtml(source.root_id || source.source_id || source.id || '')}</div></details>
      <div class="rail-section"><div class="finding-actions"><button class="btn" type="button" onclick="addSourceDialog()">添加来源</button>${source.root_id ? `<button class="btn btn-danger" type="button" onclick="removeSource('${escapeHtml(source.root_id)}','${escapeHtml(source.display_name || source.root_id)}')">删除映射</button>` : ''}</div></div>`);
    return;
  }
  if (isShareGroupScope()) {
    const snapshot = state.governanceSnapshot || {};
    const memberIds = [...new Set([
      ...activeScopeMemberIds,
      ...(Array.isArray(snapshot.members) ? snapshot.members.map(item => String(item?.instance_id || item?.agent_instance_id || item || '')) : []),
    ].filter(Boolean))];
    const programProjection = Array.isArray(agentCardsData?.program_member_details)
      ? agentCardsData.program_member_details
      : (Array.isArray(agentCardsData?.member_details)
        ? agentCardsData.member_details
        : (Array.isArray(agentCardsData?.program_members) ? agentCardsData.program_members : (agentCardsData?.agents || [])));
    const railSummary = governanceGroupProgramSummary({...agentCardsData, members: programProjection});
    const memberNames = railSummary.names.length ? railSummary.names : agentNamesForIds(memberIds).filter(name => name !== '未识别的 MCP 助手');
    const declaredProgramCount = optionalFiniteNumber(agentCardsData?.program_member_count);
    const endpointMemberCount = optionalFiniteNumber(agentCardsData?.member_count ?? agentCardsData?.endpoint_member_count);
    const declaredOtherCount = optionalFiniteNumber(agentCardsData?.extra_connection_count);
    const programCount = declaredProgramCount === null ? railSummary.programCount : Math.max(0, declaredProgramCount);
    const otherCount = declaredOtherCount === null
      ? (endpointMemberCount === null ? Math.max(0, memberIds.length - programCount) : Math.max(0, endpointMemberCount - programCount))
      : Math.max(0, declaredOtherCount);
    const connectionCount = endpointMemberCount === null ? programCount + otherCount : Math.max(0, endpointMemberCount);
    const unknownCount = optionalFiniteNumber(agentCardsData?.unknown_member_count)
      ?? optionalFiniteNumber(agentCardsData?.unresolved_member_count)
      ?? railSummary.unknownCount;
    const counts = governanceCounts();
    const summary = memberNames.length
      ? `${memoryGroupLabel(activeShareGroupId)} · ${memberNames.join('、')}`
      : `${memoryGroupLabel(activeShareGroupId)} · 成员名称待加载`;
    railTitle('共享治理', `<div class="rail-section"><div class="rail-title">共享治理 · 已激活</div><div class="rail-copy">${escapeHtml(summary)}</div></div>
      ${railKey('程序', `${programCount} 个`)}
      ${railKey('连接', `${connectionCount} 条（其他 ${otherCount}${unknownCount ? `，待识别 ${unknownCount}` : ''}）`)}
      ${railKey('有效记忆', counts.active === null ? '暂不可用' : String(counts.active))}
      <div class="rail-section"><div class="rail-copy">当前共享范围包含 ${programCount} 个程序、${connectionCount} 条连接，其中 ${otherCount} 条为其他连接${unknownCount ? `（${unknownCount} 条待识别）` : ''}。选择 Agent 卡片后，才切换到该 Agent 的详情。</div></div>
      <details class="compact-secondary"><summary>技术详情</summary><div class="rail-copy">share_group_id: ${escapeHtml(activeShareGroupId)}</div></details>
      <div class="rail-section"><div class="finding-actions"><button class="btn btn-primary" type="button" onclick="enterMultiAgentMode()">管理记忆组</button><button class="btn" type="button" onclick="discoverAgents()">检测本机 Agent</button></div></div>`);
    return;
  }
  const agent = ((agentCardsData?.agents || []).find(item => item.instance_id === activeAgentInstanceId) || activeAgentInstanceId);
  railTitle('Agent 详情', `<div class="rail-section"><div class="rail-title">${escapeHtml(agentDisplayName(agent, '未选择 Agent'))}</div><div class="rail-copy">选择数据源行后，在此查看来源细节。</div></div><div class="rail-section"><div class="finding-actions"><button class="btn btn-primary" type="button" onclick="discoverAgents()">检测本机 Agent</button>${activeAgentInstanceId ? `<button class="btn" type="button" onclick="selectAgentInstance('${escapeHtml(activeAgentInstanceId)}')">勾选授权</button>` : ''}<button class="btn" type="button" onclick="enterMultiAgentMode()">管理记忆组</button></div></div>`);
}

function agentMemberKey(member) {
  const item = member && typeof member === 'object' ? member : {};
  return String(item.instance_id || item.agent_instance_id || item.agent_id || item.id || item.binding_id || '').trim();
}

function agentProgramKey(member) {
  const item = member && typeof member === 'object' ? member : {};
  return String(item.canonical_program_id || item.program_id || agentMemberKey(item)).trim();
}

function agentMemberLabel(member, index = 0) {
  const label = agentDisplayName(member, '');
  return label && label !== '未识别的 MCP 助手' ? label : `待识别/历史连接 ${index + 1}`;
}

function agentMemberStatus(member, binding, historical = false) {
  if (binding?.status === 'active') return historical ? '历史连接 · 已绑定' : (binding.group_kind === 'shared' ? '已绑定共享组' : '已启用个人层');
  const raw = String(member?.member_status || member?.lifecycle_state || member?.status || '').trim().toLowerCase();
  const labels = {
    active: '已连接', connected: '已连接', bound: '已绑定', ready: '可用',
    stale: '历史连接 · 已失效', missing: '历史连接 · 待识别', expired: '历史连接 · 已过期',
    unresolved: '待识别', unknown: '待识别', unavailable: '暂不可用',
  };
  return labels[raw] || (historical ? '历史连接 · 待识别' : '待确认');
}

function isUnknownHistoricalMember(member) {
  const item = member && typeof member === 'object' ? member : {};
  const status = String(item.member_status || item.lifecycle_state || item.status || '').trim().toLowerCase();
  const label = [item.display_name, item.agent_name, item.member_name, item.provider_name,
    item.provider, item.product, item.program_name, item.program, item.client_name]
    .map(value => String(value || '').trim()).find(Boolean);
  const readableLabel = readableAgentPart(label, agentMemberKey(item));
  const genericLabel = !readableLabel || ['unknown', 'unresolved', 'missing', 'n/a', 'null', '未知助手', '未知 agent', '未识别', '未识别的 mcp 助手'].includes(readableLabel.toLowerCase());
  const knownProduct = agentFamily(item) !== 'unknown' || !genericLabel;
  const programId = String(item.canonical_program_id ?? item.program_id ?? '').trim().toLowerCase();
  const unknownProgramId = !programId || ['unknown', 'unresolved', 'missing', 'n/a', 'null'].includes(programId);
  const identityResolution = String(item.identity_resolution || item.identity_status || '').trim().toLowerCase();
  if (status === 'historical_unknown') return !knownProduct;
  if (knownProduct) return false;
  if (['unknown', 'unresolved', 'missing'].includes(status)) return true;
  return unknownProgramId && ['unknown', 'unresolved', 'missing'].includes(identityResolution);
}

async function unbindAgentBinding(bindingId) {
  const id = String(bindingId || '').trim();
  if (!id) return showToast('缺少 binding_id，无法解除连接', 'error');
  if (!confirm('确认解除该 Agent 连接？\n\nMemoryGuard 记忆库不会被删除，只移除这条绑定。')) return;
  try {
    const result = await callApi('unbind_agent', id);
    if (result && (result.error || result.ok === false)) return showToast(apiErrorMessage(result, '解除连接失败'), 'error');
    showToast('连接已解除', 'success');
    await renderSources();
  } catch (error) {
    showToast('解除连接失败：' + error, 'error');
  }
}

function agentUnbindButton(member, binding) {
  const id = String(member?.binding_id || binding?.binding_id || '').trim();
  const allowed = member?.can_unbind === true || binding?.can_unbind === true;
  return id && allowed
    ? `<button class="btn btn-danger" type="button" onclick="event.stopPropagation();unbindAgentBinding('${escapeHtml(id)}')">解除连接</button>`
    : '';
}

function renderSourcesView(sourcesResult, rawResult, agentData, bindingsResult, renderToken = currentContentRenderToken('sources')) {
  const sources = Array.isArray(sourcesResult?.sources) ? sourcesResult.sources : [];
  const listedAgents = Array.isArray(agentCardsData?.agents) ? agentCardsData.agents.filter(item => item && typeof item === 'object') : [];
  const explicitProgramMembers = Array.isArray(agentCardsData?.program_members)
    ? agentCardsData.program_members.filter(item => item && typeof item === 'object')
    : [];
  const residuals = Array.isArray(agentCardsData?.residuals) ? agentCardsData.residuals : [];
  const bindings = Array.isArray(bindingsResult?.bindings) ? bindingsResult.bindings : [];
  const memberDetails = Array.isArray(agentCardsData?.member_details)
    ? agentCardsData.member_details
    : (Array.isArray(agentCardsData?.members)
      ? agentCardsData.members
      : bindings.map(item => ({...item, member_status: item.member_status || item.status, historical: item.historical === true || item.status !== 'active'})));
  const unresolvedMemberKeys = new Set(memberDetails
    .filter(item => ['unresolved', 'missing', 'stale', 'history', 'historical', 'unknown', 'inactive'].includes(String(item?.member_status || item?.status || '').trim().toLowerCase()))
    .map(agentMemberKey).filter(Boolean));
  const discoveredAgents = listedAgents.length ? listedAgents : explicitProgramMembers;
  const agents = Array.from(new Map(discoveredAgents
    .filter(item => !unresolvedMemberKeys.has(agentMemberKey(item)))
    .map((item, index) => [agentProgramKey(item) || `program-${index}`, item])).values());
  const programKeys = new Set(agents.map(agentMemberKey).filter(Boolean));
  const historicalMembers = Array.from(new Map(
    [...residuals, ...memberDetails].filter(item => item && typeof item === 'object')
      .filter(item => {
        const key = agentMemberKey(item);
        return !key || !programKeys.has(key) || item.is_program_member === false || item.program_member === false;
      })
      .map((item, index) => [agentMemberKey(item) || `history-${index}`, item]),
  ).values());
  const activeBindings = bindings.filter(item => item.status === 'active');
  sourcePageRecords = new Map(sources.map(item => [String(item.root_id || item.source_id || item.id || ''), item]));
  sourcePageRecords.set('__sourcesResult', sourcesResult); sourcePageRecords.set('__rawResult', rawResult); sourcePageRecords.set('__agentData', agentData); sourcePageRecords.set('__bindingsResult', bindingsResult); sourcePageRecords.set('__bindings', activeBindings);
  if (selectedSourceId && sourcePageRecords.has(selectedSourceId)) selectedSourceRecord = sourcePageRecords.get(selectedSourceId);
  else { selectedSourceId = ''; selectedSourceRecord = null; }
  const filteredSources = sources.filter(source => sourceViewTab === 'all' || sourceStatus(source, activeBindings).bucket === sourceViewTab);
  const agentCards = agents.map(agent => {
    const active = !isShareGroupScope() && agent.instance_id === activeAgentInstanceId;
    const summary = agentSummary(agent);
    const agentKey = agentMemberKey(agent);
    const binding = activeBindings.find(item => sameNonEmptyAgentId(item.agent_instance_id, agent.instance_id)
      || (agent.binding_id && String(item.binding_id || '') === String(agent.binding_id)));
    const action = binding
      ? `<button class="btn" type="button" onclick="event.stopPropagation();viewMemoryLayer('${escapeHtml(binding.share_group_id)}')">进入记忆层</button><button class="btn" type="button" onclick="event.stopPropagation();installMemoryGroupMcp('${escapeHtml(binding.share_group_id)}')">安装 MCP</button>${binding.group_kind === 'shared' ? `<button class="btn btn-danger" type="button" onclick="event.stopPropagation();leaveSharedToPersonal('${escapeHtml(agent.instance_id)}')">回个人层</button>` : ''}${agentUnbindButton(agent, binding)}`
      : `<button class="btn btn-primary" type="button" onclick="event.stopPropagation();ensurePersonalLayer('${escapeHtml(agent.instance_id || agentKey)}')">启用个人层</button>${agentUnbindButton(agent, binding)}`;
    const displayName = agentDisplayName(agent);
    const sourceSummary = agentSourceSummary(agent);
    const identity = agentIdentityDetail(agent) || agent.binding_id || agent.agent_instance_id;
    return `<article class="agent-card ${active ? 'active' : ''}" role="button" tabindex="0" data-agent-id="${escapeHtml(agent.instance_id || agentKey)}" onclick="selectAgentCard('${escapeHtml(agent.instance_id || agentKey)}')" onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectAgentCard('${escapeHtml(agent.instance_id || agentKey)}'); }">${agentIconMarkup(agent)}<div class="agent-name">${escapeHtml(displayName)}</div><div class="agent-meta">${summary.sourceCount} 来源 · ${summary.foundSurfaceCount}/${summary.surfaceCount} 表面</div><div class="agent-meta agent-source-summary">${escapeHtml(sourceSummary)}</div><span class="chip chip-${binding ? 'confirmed' : 'info'}">${escapeHtml(agentMemberStatus(agent, binding))}</span><details class="agent-technical-id" onclick="event.stopPropagation()"><summary>技术标识</summary><code>${escapeHtml(identity || '未返回')}</code></details><div class="finding-actions">${action}</div></article>`;
  }).join('') || '<div class="empty-inline">未发现可接入 Agent。可执行本机检测或手工添加来源。</div>';
  const sourceRows = filteredSources.map(source => {
    const id = String(source.root_id || source.source_id || source.id || '');
    const status = sourceStatus(source, activeBindings);
    return `<tr class="source-detail-row ${id === selectedSourceId ? 'is-selected' : ''}" data-source-id="${escapeHtml(id)}" onclick="selectSourceRecord('${escapeHtml(id)}')"><td><strong>${escapeHtml(source.display_name || source.name || '未命名来源')}</strong></td><td>${escapeHtml(source.type || source.source_category || '未返回')}</td><td>${escapeHtml(source.scope || '未返回')}</td><td><span class="chip chip-${status.tone}">${escapeHtml(status.label)}</span></td><td><div class="table-actions"><button class="btn" type="button" onclick="event.stopPropagation();selectSourceRecord('${escapeHtml(id)}')">详情</button>${source.root_id ? `<button class="btn btn-danger" type="button" onclick="event.stopPropagation();removeSource('${escapeHtml(source.root_id)}','${escapeHtml(source.display_name || source.root_id)}')">删除映射</button>` : ''}</div></td></tr>`;
  }).join('') || '<tr><td colspan="5" class="empty-note">此筛选条件下没有真实数据源。</td></tr>';
  const scopeRows = (agentData?.scopes || []).flatMap(scope => {
    const direct = (scope.categories || []).flatMap(category => (category.files || []).map(file => ({scope: scope.scope || 'unknown', category: category.category || 'unknown', file})));
    const projects = (scope.projects || []).flatMap(project => (project.categories || []).flatMap(category => (category.files || []).map(file => ({scope: `${scope.scope || 'unknown'} · ${project.project_ref || '未命名项目'}`, category: category.category || 'unknown', file}))));
    return direct.concat(projects);
  }).slice(0, 80).map(item => {
    const file = item.file || {}; const canOpen = !!file.root_id && file.authorized !== false && file.read_status !== 'discovered';
    const args = escapeHtml(JSON.stringify([String(file.root_id || ''), String(file.relative_path || '')]));
    return `<tr><td>${escapeHtml(item.scope)}</td><td>${escapeHtml(item.category)}</td><td><code>${escapeHtml(guiPathText(file.relative_path || file.path || file.display_name, '未命名文件'))}</code></td><td>${canOpen ? `<button class="btn" type="button" onclick="viewSourceFile(...${args})">查看</button>` : '<span class="muted">仅发现，需先授权</span>'}</td></tr>`;
  }).join('') || '<tr><td colspan="4" class="empty-note">选中 Agent 暂无已发现数据。</td></tr>';
  const coverage = rawResult?.coverage || {};
  const declaredProgramCount = optionalFiniteNumber(agentCardsData?.program_member_count);
  const endpointMemberCount = optionalFiniteNumber(agentCardsData?.member_count ?? agentCardsData?.endpoint_member_count);
  const declaredHistoricalCount = optionalFiniteNumber(agentCardsData?.unresolved_member_count);
  const declaredUnknownCount = optionalFiniteNumber(agentCardsData?.unknown_member_count)
    ?? optionalFiniteNumber(agentCardsData?.unresolved_member_count);
  const programCount = declaredProgramCount === null ? agents.length : Math.max(0, declaredProgramCount);
  // ``member_count`` is the endpoint total, while program_member_count only
  // counts recognized programs.  Keep redundant and unknown bindings visible
  // as other connections instead of silently folding them into the headline.
  const historicalCount = endpointMemberCount === null
    ? (declaredHistoricalCount === null ? historicalMembers.length : Math.max(declaredHistoricalCount, historicalMembers.length))
    : Math.max(0, endpointMemberCount - programCount);
  const unknownCount = declaredUnknownCount === null
    ? historicalMembers.filter(isUnknownHistoricalMember).length
    : Math.max(0, declaredUnknownCount);
  const historicalCards = historicalMembers.map((item, index) => {
    const key = agentMemberKey(item);
    const displayName = agentMemberLabel(item, index);
    const identity = agentIdentityDetail(item) || item.binding_id || item.agent_instance_id;
    const binding = activeBindings.find(candidate => (item.binding_id && String(candidate.binding_id || '') === String(item.binding_id))
      || (item.agent_instance_id && sameNonEmptyAgentId(candidate.agent_instance_id, item.agent_instance_id)));
    const details = [item.member_status || item.status, item.reason, item.last_seen_at || item.updated_at].filter(Boolean).join(' · ');
    const actions = agentUnbindButton(item, binding) || (item.instance_id || item.agent_instance_id
      ? `<button class="btn" type="button" onclick="event.stopPropagation();showResidualCleanup('${escapeHtml(item.instance_id || item.agent_instance_id)}')">查看原生数据</button>`
      : `<button class="btn" type="button" onclick="event.stopPropagation();renderMultiAgentBinding()">管理连接</button>`);
    return `<article class="agent-card historical-agent-card" data-agent-id="${escapeHtml(key)}">${agentIconMarkup(item)}<div class="agent-name">${escapeHtml(displayName)}</div><div class="agent-meta">${escapeHtml(agentSourceSummary(item))}</div><div class="agent-meta">${escapeHtml(details || '后端尚未提供可读状态')}</div><span class="chip chip-high">${escapeHtml(agentMemberStatus(item, binding, true))}</span><details class="agent-technical-id"><summary>技术详情</summary><code>${escapeHtml(identity || '未返回')}</code>${item.binding_id ? `<div class="surface-meta">binding_id · ${escapeHtml(item.binding_id)}</div>` : ''}</details><div class="finding-actions">${actions}</div></article>`;
  }).join('');
  const historicalSection = historicalCount || historicalCards
    ? `<section class="agent-member-section historical-members"><div class="compact-toolbar"><div class="toolbar-grow"><h2>待识别/历史连接</h2><span class="muted">${historicalCount} 条其他连接${unknownCount ? `（${unknownCount} 待识别）` : ''}；保留记录并提供解除或查看入口。</span></div></div><div class="agent-discovery-grid">${historicalCards || `<div class="empty-inline">后端报告 ${historicalCount} 个其他连接，但尚未返回 member_details。</div>`}</div></section>`
    : '';
  setContent(`<div class="dashboard-view sources-view"><div class="dashboard-main">
    <div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Data sources</span><h2>数据源与代理</h2></div><div class="page-tabs" role="tablist"><button class="${sourceViewTab === 'all' ? 'active' : ''}" onclick="setSourceViewTab('all')">全部 (${sources.length})</button><button class="${sourceViewTab === 'connected' ? 'active' : ''}" onclick="setSourceViewTab('connected')">已接入</button><button class="${sourceViewTab === 'available' ? 'active' : ''}" onclick="setSourceViewTab('available')">可用</button></div><button class="btn btn-primary" type="button" onclick="discoverAgents()">检测本机 Agent</button><button class="btn" type="button" onclick="addSourceDialog()">添加来源</button><button class="btn" type="button" onclick="importBundleDialog()">导入导出包</button></div>
    <section class="agent-member-section"><div class="compact-toolbar"><div class="toolbar-grow"><h2>程序成员</h2><span class="muted">${programCount} 个程序 · ${historicalCount} 条其他连接${unknownCount ? `（${unknownCount} 待识别）` : ''}；名称来自程序/Provider/别名，技术 ID 仅在详情显示。</span></div><button class="btn" type="button" onclick="enterMultiAgentMode()">管理记忆组</button></div><div class="agent-discovery-grid">${agentCards}</div></section>
    ${historicalSection}
    <section><div class="compact-toolbar"><div class="toolbar-grow"><h2>已接入来源</h2><span class="muted">点击行固定右侧来源详情。</span></div></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>名称</th><th>类型</th><th>范围</th><th>状态</th><th>操作</th></tr></thead><tbody>${sourceRows}</tbody></table></div></section>
    <details class="compact-secondary"><summary>文件与扫描数据 · ${agentData?.total_files ?? 0} 个文件</summary><div class="data-table-wrap"><table class="data-table"><thead><tr><th>范围</th><th>分类</th><th>文件</th><th>操作</th></tr></thead><tbody>${scopeRows}</tbody></table></div><div class="chips"><span class="chip chip-info">候选 ${coverage.candidate_count ?? 0}</span><span class="chip chip-confirmed">可读 ${coverage.read ?? 0}</span><span class="chip chip-${Number(coverage.unaccounted_count || 0) ? 'high' : 'confirmed'}">未计入 ${coverage.unaccounted_count ?? 0}</span></div></details>
    ${residuals.length ? `<details class="compact-secondary"><summary>未接入原生数据 · ${residuals.length}</summary><div class="agent-discovery-grid">${residuals.map(item => `<article class="agent-card" role="button" tabindex="0" data-agent-id="${escapeHtml(item.instance_id || '')}" onclick="showResidualCleanup('${escapeHtml(item.instance_id)}')" onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); showResidualCleanup('${escapeHtml(item.instance_id)}'); }">${agentIconMarkup(item)}<div class="agent-name">${escapeHtml(agentDisplayName(item))}</div><div class="agent-meta">${escapeHtml(agentSourceSummary(item))}</div><div class="agent-meta">原生数据 ${item.private_data_surface_count || 0}</div><details class="agent-technical-id" onclick="event.stopPropagation()"><summary>技术标识</summary><code>${escapeHtml(agentIdentityDetail(item) || '未返回')}</code></details><div class="finding-actions"><button class="btn btn-primary" type="button" onclick="event.stopPropagation();ensurePersonalLayer('${escapeHtml(item.instance_id)}')">启用个人层</button></div></article>`).join('')}</div></details>` : ''}
  </div></div>`, renderToken);
  renderSourcesRail();
}

function selectFinding(findingId) {
  selectedFindingId = String(findingId || '');
  document.querySelectorAll('.risk-row').forEach(row => row.classList.toggle('is-selected', row.dataset.findingId === selectedFindingId));
  renderStatusRail();
}

function renderRiskRail() {
  const findings = Array.isArray(state.report?.findings) ? state.report.findings : [];
  const finding = findings.find(item => String(item.id || '') === selectedFindingId) || null;
  const counts = findings.reduce((all, item) => { const key = String(item.severity || '').toLowerCase(); all[key] = (all[key] || 0) + 1; return all; }, {});
  if (finding) {
    railTitle('风险详情', `<div class="rail-section"><div class="rail-title">${escapeHtml(riskRuleLabel(finding.rule_id, finding))}</div><span class="chip chip-${escapeHtml(finding.severity || 'info')}">${escapeHtml(riskSeverityLabel(finding.severity, finding))}</span></div><div class="rail-section"><h4>原因</h4><div class="rail-copy">${escapeHtml(riskReasonText(finding))}</div></div><div class="rail-section"><h4>影响</h4><div class="rail-copy">${escapeHtml(riskImpactText(finding))}</div></div><div class="rail-section"><h4>建议</h4><div class="rail-copy">${escapeHtml(riskActionText(finding))}</div></div><div class="rail-section"><div class="finding-actions"><button class="btn" type="button" onclick="copyFindingForAgent('${escapeHtml(finding.id)}')">复制给 Agent</button>${finding.fixable ? `<button class="btn btn-primary" type="button" onclick="generatePlan('${escapeHtml(finding.id)}')">生成计划</button>` : ''}</div></div><details class="compact-secondary"><summary>技术详情</summary><div class="rail-copy">规则: ${escapeHtml(finding.rule_id || '')}<br>来源: ${escapeHtml(riskTechnicalSource(finding) || '未提供')}<br>位置: ${escapeHtml(finding.location?.path || '')}</div></details>${renderRiskGovernanceLinks()}`);
    return;
  }
  railTitle('风险与治理', `${railKey('极高/高风险', String((counts.critical || 0) + (counts.high || 0)), (counts.critical || counts.high) ? 'danger' : 'zero')}${railKey('中风险', String(counts.medium || 0), counts.medium ? 'alert' : 'zero')}${railKey('低风险', String(counts.low || 0), counts.low ? '' : 'zero')}${renderRiskGovernanceLinks()}`);
}

function renderRiskGovernanceLinks() {
  return `<div class="rail-section"><h4>治理入口</h4><div class="finding-actions"><button class="btn" type="button" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('conflicts'),50)">冲突</button><button class="btn" type="button" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('quarantine'),50)">隔离</button><button class="btn" type="button" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('rollback'),50)">回滚</button><button class="btn" type="button" onclick="switchTab('governance');setTimeout(()=>switchGovernanceSub('recent_events'),50)">最近事件</button></div></div>`;
}

function renderFindings() {
  const report = state.report || {};
  const findings = Array.isArray(report.findings) ? report.findings : [];
  const counts = findings.reduce((all, item) => { const key = String(item.severity || '').toLowerCase(); all[key] = (all[key] || 0) + 1; return all; }, {});
  const rows = findings.map(finding => `<tr class="risk-row ${String(finding.id || '') === selectedFindingId ? 'is-selected' : ''}" data-finding-id="${escapeHtml(finding.id || '')}" onclick="selectFinding('${escapeHtml(finding.id || '')}')"><td><span class="chip chip-${escapeHtml(finding.severity || 'info')}">${escapeHtml(riskSeverityLabel(finding.severity, finding))}</span></td><td><strong>${escapeHtml(riskRuleLabel(finding.rule_id, finding))}</strong></td><td>${escapeHtml(riskDimensionLabel(finding.dimension, finding))}</td><td><span class="risk-reason">${escapeHtml(riskReasonText(finding))}</span></td><td><div class="table-actions"><button class="btn" type="button" onclick="event.stopPropagation();selectFinding('${escapeHtml(finding.id || '')}')">详情</button><button class="btn" type="button" onclick="event.stopPropagation();copyFindingForAgent('${escapeHtml(finding.id || '')}')">复制</button></div></td></tr>`).join('') || '<tr><td colspan="5" class="empty-note">没有发现需要处理的风险信号。</td></tr>';
  setContent(`<div class="dashboard-view findings-view"><div class="dashboard-main"><div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Risk signals</span><h2>风险信号</h2></div><button class="btn" type="button" onclick="copyAllFindingsForAgent()">复制全部风险给 Agent</button><button class="btn btn-primary audit-refresh-button" type="button" onclick="runAudit()">重新扫描</button></div><div class="kpi-grid"><div class="kpi danger"><span>极高/高风险</span><strong>${(counts.critical || 0) + (counts.high || 0)}</strong></div><div class="kpi alert"><span>中风险</span><strong>${counts.medium || 0}</strong></div><div class="kpi"><span>低风险</span><strong>${counts.low || 0}</strong></div><div class="kpi"><span>风险总数</span><strong>${findings.length}</strong></div></div><section><div class="compact-toolbar"><div class="toolbar-grow"><h2>最新风险</h2><span class="muted">主列表不展示内部技术来源；选择后在右栏查看证据与处置建议。</span></div></div><div class="data-table-wrap"><table class="data-table risk-table"><thead><tr><th>严重度</th><th>风险</th><th>维度</th><th>原因摘要</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table></div></section></div></div>`);
  renderRiskRail();
}

function tokenUsageNumber(...values) {
  for (const value of values) {
    const number = optionalFiniteNumber(value);
    if (number !== null) return number;
  }
  return null;
}

function tokenUsageMode(value) {
  const key = String(value || '').trim().toLowerCase().replace(/[ -]+/g, '_');
  if (['measured', 'host_measured', 'host', 'actual', 'observed'].includes(key)) return 'measured';
  if (['estimated', 'memoryguard', 'memoryguard_estimated', 'deterministic'].includes(key)) return 'estimated';
  if (['mixed', 'combined', 'both'].includes(key)) return 'mixed';
  if (['unavailable', 'unknown', 'not_available', 'not_ready', 'pending'].includes(key)) return 'unavailable';
  return '';
}

function tokenUsageAgentKey(item = {}) {
  if (typeof item === 'string') return item.trim();
  return String(item.agent_key || item.agentKey || item.canonical_program_id || item.program_id
    || item.program_key || item.agent_instance_id || item.instance_id || item.id || '').trim();
}

function tokenUsageAgentName(item = {}) {
  if (typeof item === 'string') return agentDisplayName(item, '未识别 Agent');
  const direct = item.display_name || item.program_name || item.program || item.provider_name
    || item.provider || item.product || item.agent_name || item.member_name || '';
  const resolved = agentDisplayName(item, '');
  return resolved && resolved !== '未识别的 MCP 助手' ? resolved : (direct || '未识别 Agent');
}

function tokenUsageTimestamp(row = {}) {
  return row.last_synced_at || row.synced_at || row.recorded_at || row.updated_at
    || row.timestamp || row.created_at || '';
}

function tokenUsageHostReason(item = {}) {
  const status = String(item.host_measurement_status || item.hostMeasurementStatus || '').trim().toLowerCase();
  const reason = String(item.host_measurement_reason || item.hostMeasurementReason || item.last_error || '').trim().toLowerCase();
  if (status === 'host_not_supported' || reason === 'host_does_not_report_tokens') return '宿主未提供实测用量';
  if (status === 'source_not_found' || reason === 'source_not_detected') return '未检测到来源';
  if (status === 'not_synced' || status === 'unavailable' || !status) return '未同步';
  return '';
}

function tokenUsageMeasuredLabel(row = {}) {
  if (['measured', 'mixed'].includes(row.mode)) {
    return `${tokenUsageFormat(row.measuredInput)} / ${tokenUsageFormat(row.measuredOutput)}`;
  }
  if (row.mode === 'estimated') return '宿主未提供';
  return tokenUsageHostReason(row) || '宿主未提供';
}

function tokenUsageEmptyCopy(payload = {}) {
  const sync = payload.sync_state && typeof payload.sync_state === 'object' ? payload.sync_state : {};
  const status = String(sync.status || payload.empty_reason || '').trim().toLowerCase();
  const providers = sync.providers && typeof sync.providers === 'object' ? sync.providers : {};
  if (!Object.keys(providers).length || status === 'unavailable' || status === 'not_synced') {
    return {title: '当前窗口没有 MCP 转换记录。', detail: '未同步。查询本身不会采集宿主用量，请使用顶栏重新扫描。'};
  }
  if (status === 'no_measured_source' || status === 'source_not_found') {
    return {title: '当前窗口没有 MCP 转换记录。', detail: '未检测到来源。已绑定宿主未提供可同步的实测用量文件。'};
  }
  if (status === 'host_not_supported') {
    return {title: '当前窗口没有 MCP 转换记录。', detail: '宿主未提供实测用量。MemoryGuard 估算在有转换事件时单独显示。'};
  }
  return {title: '当前窗口没有 MCP 转换记录。', detail: '没有数据时不补零，也不推断节省量。'};
}

function tokenUsageNormalizeRow(row = {}) {
  const item = row && typeof row === 'object' ? row : {};
  const state = item.measurement_state || item.measurement_mode || item.measurement
    || item.metering || item.source_kind || item.source || item.status;
  const explicitMode = tokenUsageMode(state);
  const baseline = tokenUsageNumber(item.estimated_baseline_units, item.baseline_units,
    item.raw_candidates, item.raw_candidate_tokens, item.candidate_tokens, item.baseline_tokens,
    item.raw_context_tokens, item.raw_tokens, item.original_tokens);
  const delivered = tokenUsageNumber(item.estimated_delivered_units, item.delivered_units,
    item.actual_injected, item.actual_injected_tokens, item.injected_tokens, item.mcp_tokens,
    item.context_tokens, item.memoryguard_tokens, item.actual_tokens);
  const measuredInput = tokenUsageNumber(item.measured_input, item.measured_input_tokens,
    item.host_input_tokens, item.input_tokens);
  const measuredOutput = tokenUsageNumber(item.measured_output, item.measured_output_tokens,
    item.host_output_tokens, item.output_tokens);
  const measuredTotal = tokenUsageNumber(item.measured_total, item.measured_total_tokens,
    item.host_total_tokens, item.total_tokens);
  const explicitSavings = tokenUsageNumber(item.estimated_saved_units, item.estimated_savings,
    item.estimated_saved_tokens, item.saved_tokens, item.savings_tokens);
  const savings = explicitSavings !== null
    ? explicitSavings
    : (baseline !== null && delivered !== null ? Math.max(0, baseline - delivered) : null);
  const ratioValue = tokenUsageNumber(item.estimated_ratio, item.savings_ratio, item.saved_ratio,
    item.estimated_savings_ratio);
  const savingsRatio = ratioValue !== null
    ? (ratioValue > 1 ? ratioValue / 100 : ratioValue)
    : (savings !== null && baseline > 0 ? savings / baseline : null);
  const resolvedMode = explicitMode || (baseline !== null && delivered !== null
    ? (measuredInput !== null || measuredOutput !== null || measuredTotal !== null ? 'mixed' : 'estimated')
    : (measuredInput !== null || measuredOutput !== null || measuredTotal !== null ? 'measured' : 'unavailable'));
  return {
    ...item,
    date: String(item.date || item.day || item.period || item.period_start || '').trim(),
    agentKey: tokenUsageAgentKey(item),
    agentName: tokenUsageAgentName(item),
    mode: resolvedMode,
    rawCandidates: baseline,
    actualInjected: delivered,
    savings,
    savingsRatio,
    measuredInput,
    measuredOutput,
    measuredTotal: measuredTotal !== null
      ? measuredTotal
      : (measuredInput !== null || measuredOutput !== null
        ? (measuredInput || 0) + (measuredOutput || 0) : null),
    conversions: tokenUsageNumber(item.conversion_count, item.conversions,
      item.mcp_conversions, item.packet_count, item.request_count),
    syncedAt: tokenUsageTimestamp(item),
  };
}

function normalizeTokenUsagePayload(raw = {}) {
  const source = objectValue(raw?.data && typeof raw.data === 'object' ? raw.data : raw);
  const rawRows = Array.isArray(source.rows) ? source.rows
    : (Array.isArray(source.records) ? source.records
      : (Array.isArray(source.daily) ? source.daily : []));
  const rawAgents = Array.isArray(source.agents) ? source.agents
    : (Array.isArray(source.agent_summaries) ? source.agent_summaries : []);
  const rows = rawRows.map(tokenUsageNormalizeRow);
  const status = tokenUsageMode(source.measurement_state || source.status || source.availability || source.measurement_status)
    || (raw?.ok === false ? 'unavailable' : (rows.length ? 'available' : 'unavailable'));
  const summary = source.summary && typeof source.summary === 'object' ? {...source.summary} : {};
  const series = Array.isArray(source.series) ? source.series.map(tokenUsageNormalizeRow) : [];
  return {
    ...source,
    rows,
    agents: rawAgents,
    series,
    summary,
    status,
    windowDays: tokenUsageNumber(source.window_days, source.windowDays) || tokenUsageState.windowDays,
    unit: source.unit || source.counter_unit || source.token_unit || '未提供单位',
    generatedAt: source.generated_at || source.generatedAt || '',
    error: raw?.error || source.error || '',
  };
}

function tokenUsageErrorKind(payload = {}) {
  const text = [payload.status, payload.error, payload.code, payload.reason].filter(Boolean)
    .join(' ').toLowerCase();
  return text.includes('unavailable') || text.includes('not_ready') || text.includes('not found')
    || text.includes('operation_not_found') || text.includes('telemetry') ? 'unavailable' : 'error';
}

function tokenUsageCatalog(payload = {}, bindingsResult = {}) {
  // Backend roster is authoritative.  Discovery cards contain installed and
  // historical products, not necessarily members of the trusted group, so
  // never use them to create token filter options.
  const roster = Array.isArray(payload.agents) ? payload.agents : [];
  const fallback = roster.length ? [] : (Array.isArray(bindingsResult?.bindings) ? bindingsResult.bindings : []);
  const candidates = [...roster, ...fallback];
  const byKey = new Map();
  candidates.forEach((candidate, index) => {
    const item = candidate && typeof candidate === 'object' ? candidate : {agent_key: candidate};
    const key = tokenUsageAgentKey(item) || `unresolved-${index}`;
    if (!byKey.has(key)) byKey.set(key, {...item, agentKey: key, agentName: tokenUsageAgentName(item)});
  });
  payload.rows.forEach(row => {
    const key = row.agentKey || `unresolved-row-${byKey.size}`;
    if (!byKey.has(key)) byKey.set(key, {agentKey: key, agentName: row.agentName || '未识别 Agent'});
  });
  return [...byKey.values()];
}

function tokenUsageRowsForView(payload = {}) {
  const selected = String(tokenUsageState.agentKey || '').trim();
  return (payload.rows || []).filter(row => !selected || row.agentKey === selected);
}

function tokenUsageFormat(value, suffix = '') {
  const number = optionalFiniteNumber(value);
  if (number === null) return '未提供';
  return `${Math.round(number).toLocaleString('zh-CN')}${suffix}`;
}

function tokenUsagePercent(value) {
  const number = optionalFiniteNumber(value);
  return number === null ? '未提供' : `${(number * 100).toFixed(1)}%`;
}

function tokenUsageSum(rows, selector) {
  const values = rows.map(row => optionalFiniteNumber(row?.[selector])).filter(value => value !== null);
  return values.length ? values.reduce((total, value) => total + value, 0) : null;
}

function tokenUsageVerifiedHostTotals(summary = {}) {
  const coverage = summary.measured_total_coverage && typeof summary.measured_total_coverage === 'object'
    ? summary.measured_total_coverage : {};
  const providerTotal = optionalFiniteNumber(summary.measured_total);
  if (coverage.provider_reported === 'complete' && providerTotal !== null) {
    return {available: true, basis: 'provider', total: providerTotal, input: null, output: null};
  }
  const input = optionalFiniteNumber(summary.measured_input);
  const output = optionalFiniteNumber(summary.measured_output);
  const derivedTotal = optionalFiniteNumber(summary.measured_derived_total);
  if (coverage.input_output_derived === 'complete' && input !== null && output !== null
      && derivedTotal !== null && derivedTotal === input + output) {
    return {available: true, basis: 'derived', total: derivedTotal, input, output};
  }
  return {available: false, basis: '', total: null, input: null, output: null};
}

function tokenUsageSyncStatusText(result = {}, fallback = {}) {
  const item = result && typeof result === 'object' ? result : {};
  const fallbackState = fallback && typeof fallback === 'object' ? fallback : {};
  const status = String(item.status || fallbackState.status || '').trim().toLowerCase();
  if (item.ok === false || ['error', 'failed'].includes(status)) {
    return `失败${item.error ? `（${item.error}）` : ''}`;
  }
  if (status === 'success') {
    return `完成，新增 ${tokenUsageFormat(item.inserted || 0)} 条`;
  }
  if (status === 'source_not_found' || status === 'no_measured_source') return '未检测到可同步来源';
  if (status === 'host_not_supported') return '宿主未提供实测用量';
  return '未同步';
}

function renderTokenUsageSyncControl(payload = {}, renderToken = currentContentRenderToken('token-usage')) {
  if (!contentRenderIsCurrent(renderToken)) return;
  const toolbar = document.querySelector('.token-usage-toolbar');
  if (!toolbar) return;
  const syncState = payload.sync_state && typeof payload.sync_state === 'object' ? payload.sync_state : {};
  const busy = Boolean(tokenUsageState.syncInFlight);
  toolbar.insertAdjacentHTML('beforeend', `<div class="token-usage-sync"><button id="token-usage-sync-button" class="btn btn-primary" type="button" onclick="syncUsageTelemetry()" ${busy ? 'disabled aria-busy="true"' : 'aria-busy="false"'}>${busy ? '同步中…' : '同步用量数据'}</button><span class="muted">${escapeHtml(tokenUsageSyncStatusText(tokenUsageState.syncResult, syncState))}</span></div>`);
}

function renderTokenUsageHostTotal(total = {}, renderToken = currentContentRenderToken('token-usage')) {
  if (!contentRenderIsCurrent(renderToken)) return;
  const card = document.querySelector('.token-kpi.measured');
  if (!card) return;
  const label = total.basis === 'provider'
    ? '宿主报告全量'
    : total.basis === 'derived' ? '宿主输入/输出派生全量' : '宿主实测全量不可用';
  const detail = total.basis === 'derived'
    ? `输入 ${tokenUsageFormat(total.input)} · 输出 ${tokenUsageFormat(total.output)}`
    : total.basis === 'provider' ? '仅在提供方报告覆盖完整时显示' : '提供方覆盖不完整或缺少可验证摘要字段';
  card.querySelector('span').textContent = label;
  card.querySelector('small').textContent = detail;
}

function tokenUsageChart(series) {
  const points = (Array.isArray(series) ? series : []).map(tokenUsageNormalizeRow).filter(row =>
    row.rawCandidates !== null && row.actualInjected !== null);
  if (!points.length) return '<div class="empty-state" style="min-height:170px"><div><div class="empty-orb"></div><p>暂无可绘制的 MG 估算数据。</p><p class="muted">宿主实测流量单独显示，不与估算线合计。</p></div></div>';
  const width = 720;
  const height = 218;
  // Keep the first and last date labels inside the SVG viewBox.  The chart
  // scales to its container, so a small symmetric gutter prevents the final
  // centered label from being clipped at either edge on wide and narrow views.
  const left = 52;
  const right = 52;
  const top = 14;
  const bottom = 34;
  const chartWidth = width - left - right;
  const chartHeight = height - top - bottom;
  const max = Math.max(1, ...points.flatMap(row => [row.rawCandidates, row.actualInjected]));
  const x = index => left + (points.length === 1 ? chartWidth / 2 : index * chartWidth / (points.length - 1));
  const y = value => top + chartHeight - (value / max) * chartHeight;
  const rawPoints = points.map((row, index) => `${x(index).toFixed(1)},${y(row.rawCandidates).toFixed(1)}`).join(' ');
  const injectedPoints = points.map((row, index) => `${x(index).toFixed(1)},${y(row.actualInjected).toFixed(1)}`).join(' ');
  const labels = points.map((row, index) => {
    const date = row.date || `第 ${index + 1} 日`;
    return `<text class="chart-label" x="${x(index).toFixed(1)}" y="${height - 10}" text-anchor="middle">${escapeHtml(date.slice(0, 10))}</text>`;
  }).join('');
  const dots = points.map((row, index) => `<circle class="chart-dot-raw" cx="${x(index).toFixed(1)}" cy="${y(row.rawCandidates).toFixed(1)}" r="3"><title>${escapeHtml(row.date || '')} 原始候选 ${tokenUsageFormat(row.rawCandidates)}</title></circle><circle class="chart-dot-injected" cx="${x(index).toFixed(1)}" cy="${y(row.actualInjected).toFixed(1)}" r="3"><title>${escapeHtml(row.date || '')} 实际注入 ${tokenUsageFormat(row.actualInjected)}</title></circle>`).join('');
  const grid = [0, .5, 1].map(ratio => {
    const lineY = top + chartHeight * ratio;
    return `<line class="chart-grid" x1="${left}" y1="${lineY.toFixed(1)}" x2="${width - right}" y2="${lineY.toFixed(1)}" /><text class="chart-label" x="${left - 7}" y="${(lineY + 3).toFixed(1)}" text-anchor="end">${tokenUsageFormat(max * (1 - ratio))}</text>`;
  }).join('');
  return `<div class="token-chart" role="img" aria-label="MemoryGuard 元开销与实际注入趋势"><svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><g>${grid}<line class="chart-axis" x1="${left}" y1="${top + chartHeight}" x2="${width - right}" y2="${top + chartHeight}" /><polyline class="chart-line-raw" points="${rawPoints}" /><polyline class="chart-line-injected" points="${injectedPoints}" />${dots}${labels}</g></svg></div>`;
}

function tokenUsageTable(rows, catalog, payload = {}) {
  const visibleRows = [...rows].sort((left, right) => String(right.date || '').localeCompare(String(left.date || '')));
  const existing = new Set(visibleRows.map(row => row.agentKey).filter(Boolean));
  const unavailable = catalog.filter(item => !existing.has(item.agentKey)).map(item => ({
    agentKey: item.agentKey,
    agentName: item.agentName || '未识别 Agent',
    mode: 'unavailable',
    date: '',
    rawCandidates: null,
    actualInjected: null,
    savings: null,
    savingsRatio: null,
    measuredInput: null,
    measuredOutput: null,
    conversions: null,
    syncedAt: '',
    host_measurement_status: item.host_measurement_status || item.hostMeasurementStatus || '',
    host_measurement_reason: item.host_measurement_reason || item.hostMeasurementReason || '',
  }));
  const tableRows = [...visibleRows, ...unavailable];
  if (!tableRows.length) {
    const empty = tokenUsageEmptyCopy(payload);
    return `<div class="empty-state" style="min-height:180px"><div><div class="empty-orb"></div><p>${escapeHtml(empty.title)}</p><p class="muted">${escapeHtml(empty.detail)}</p></div></div>`;
  }
  return `<div class="token-table-wrap"><table class="data-table token-table"><caption class="sr-only">按日期和 Agent 展示 Token 用量及 MCP 转换数据</caption><thead><tr><th>日期</th><th>Agent</th><th>口径</th><th>原始候选 / 实际注入</th><th>估算节省</th><th>实测输入 / 实测输出</th><th>实测总量</th><th>转换次数</th></tr></thead><tbody>${tableRows.map(row => {
    const modeClass = row.mode === 'measured' ? 'token-source-measured' : row.mode === 'estimated' ? 'token-source-estimated' : row.mode === 'mixed' ? 'token-source-mixed' : 'token-source-unavailable';
    const modeLabel = row.mode === 'measured' ? '宿主实测' : row.mode === 'estimated' ? 'MemoryGuard 估算' : row.mode === 'mixed' ? '实测 + 估算' : (tokenUsageHostReason(row) || '无数据');
    const key = row.agentKey || '';
    const estimated = ['estimated', 'mixed'].includes(row.mode) && row.savings !== null
      ? `${tokenUsageFormat(row.savings)} <span class="muted">(${tokenUsagePercent(row.savingsRatio)})</span>` : (row.mode === 'unavailable' ? (tokenUsageHostReason(row) || '无数据') : '宿主未提供');
    const measured = tokenUsageMeasuredLabel(row);
    const measuredTotal = ['measured', 'mixed'].includes(row.mode)
      ? tokenUsageFormat(row.measuredTotal)
      : (row.mode === 'estimated' ? '宿主未提供' : (tokenUsageHostReason(row) || '宿主未提供'));
    const flow = `<div class="token-flow-cell"><span class="token-flow-line"><span>原始候选</span><strong>${tokenUsageFormat(row.rawCandidates)}</strong></span><span class="token-flow-line"><span>实际注入</span><strong>${tokenUsageFormat(row.actualInjected)}</strong></span></div>`;
    return `<tr class="${row.mode === 'unavailable' ? 'token-unavailable-row' : ''}"><td>${escapeHtml(row.date || (row.mode === 'unavailable' ? (tokenUsageHostReason(row) || '窗口内无数据') : '窗口内无同步数据'))}</td><td><div class="token-agent-name">${escapeHtml(row.agentName || '未识别 Agent')}</div>${key ? `<details class="token-agent-id"><summary>技术 ID</summary><code>${escapeHtml(key)}</code></details>` : ''}</td><td><span class="${modeClass}">${modeLabel}</span></td><td>${flow}</td><td>${estimated}</td><td>${measured}</td><td>${measuredTotal}</td><td>${tokenUsageFormat(row.conversions)}</td></tr>`;
  }).join('')}</tbody></table></div>`;
}

function setTokenUsageWindow(days) {
  tokenUsageState.windowDays = Number(days) === 30 ? 30 : 7;
  renderTokenUsage();
}

function setTokenUsageAgent(agentKey) {
  tokenUsageState.agentKey = String(agentKey || '');
  if (tokenUsageState.payload) renderTokenUsageView(tokenUsageState.payload, tokenUsageState.bindings || {}, currentContentRenderToken('token-usage'));
}

function renderTokenUsageView(payload = {}, bindingsResult = {}, renderToken = currentContentRenderToken('token-usage')) {
  const rows = tokenUsageRowsForView(payload);
  const catalog = tokenUsageCatalog(payload, bindingsResult);
  const measuredRows = rows.filter(row => ['measured', 'mixed'].includes(row.mode));
  const estimatedRows = rows.filter(row => ['estimated', 'mixed'].includes(row.mode));
  const summary = {
    ...(payload.summary && typeof payload.summary === 'object' ? payload.summary : {}),
    ...Object.fromEntries(['estimated_baseline_units', 'estimated_delivered_units', 'estimated_saved_units',
      'estimated_ratio', 'savings_ratio', 'measured_input', 'measured_output', 'measured_total',
      'measured_derived_total', 'measured_total_coverage']
      .filter(key => payload[key] !== null && payload[key] !== undefined)
      .map(key => [key, payload[key]])),
  };
  const verifiedHostTotal = tokenUsageVerifiedHostTotals(summary);
  const hostTotal = verifiedHostTotal.total;
  const hostMeasuredLabel = hostTotal === null ? '不可用' : tokenUsageFormat(hostTotal);
  const mgSavings = tokenUsageNumber(summary.estimated_saved_units, tokenUsageSum(estimatedRows, 'savings'));
  const mgRaw = tokenUsageNumber(summary.estimated_baseline_units, tokenUsageSum(estimatedRows, 'rawCandidates'));
  const mgRatio = tokenUsageNumber(summary.estimated_ratio, summary.savings_ratio,
    mgSavings !== null && mgRaw ? mgSavings / mgRaw : null);
  const rowsWithData = new Set(rows.filter(row => ['measured', 'estimated', 'mixed'].includes(row.mode)).map(row => row.agentKey));
  const unavailableCount = catalog.filter(item => !rowsWithData.has(item.agentKey)).length;
  const lastSync = rows.map(row => row.syncedAt).filter(Boolean).sort().pop() || payload.generatedAt || '未同步';
  const options = catalog.map(item => `<option value="${escapeHtml(item.agentKey)}" ${item.agentKey === tokenUsageState.agentKey ? 'selected' : ''}>${escapeHtml(item.agentName || '未识别 Agent')}</option>`).join('');
  const hostInput = verifiedHostTotal.basis === 'derived' ? verifiedHostTotal.input : null;
  const hostOutput = verifiedHostTotal.basis === 'derived' ? verifiedHostTotal.output : null;
  const syncState = payload.sync_state && typeof payload.sync_state === 'object' ? payload.sync_state : {};
  const syncStatus = String(syncState.status || payload.sync_status || '').trim().toLowerCase();
  const syncError = payload.sync_error || syncState.error || syncState.message || '';
  const syncNotice = syncError || ['error', 'failed', 'partial'].includes(syncStatus)
    ? `<div class="token-sync-notice">同步状态：${escapeHtml(syncError || syncStatus || '失败')}。已显示可用历史数据，未将缺失数据补为 0。</div>` : '';
  setContent(`<div class="dashboard-view token-usage-view"><div class="token-usage-toolbar"><div class="toolbar-grow"><span class="eyebrow">Token usage</span><h2>Token 用量与 MCP 节省</h2><p>按绑定 Agent 查看上下文转换量。宿主实测与 MemoryGuard 估算分开呈现，不把不同口径合计为一个实际 Token。</p></div><div class="token-window-tabs" role="tablist" aria-label="统计窗口"><button type="button" class="${tokenUsageState.windowDays === 7 ? 'active' : ''}" role="tab" aria-selected="${tokenUsageState.windowDays === 7}" onclick="setTokenUsageWindow(7)">7 日</button><button type="button" class="${tokenUsageState.windowDays === 30 ? 'active' : ''}" role="tab" aria-selected="${tokenUsageState.windowDays === 30}" onclick="setTokenUsageWindow(30)">30 日</button></div><label class="sr-only" for="token-agent-filter">选择 Agent</label><select id="token-agent-filter" class="token-agent-filter" onchange="setTokenUsageAgent(this.value)"><option value="">全部绑定 Agent</option>${options}</select></div><div class="token-scope-note">计量来源：绿色为宿主实测，琥珀为 MemoryGuard 确定性估算，灰色为宿主未提供或未同步。当前单位：${escapeHtml(payload.unit)}。缺少同步数据时显示未同步/未检测到来源/宿主未提供实测用量，不按 0 处理。宿主实测与估算结果不可合计。</div>${syncNotice}<div class="token-kpi-grid"><div class="token-kpi measured"><span>宿主实测流量</span><strong>${hostMeasuredLabel}</strong><small>输入 ${tokenUsageFormat(hostInput)} · 输出 ${tokenUsageFormat(hostOutput)}</small></div><div class="token-kpi estimated"><span>MG 估算节省</span><strong>${mgSavings === null ? (rows.length ? '宿主未提供' : '无数据') : tokenUsageFormat(mgSavings)}</strong><small>估算比例 ${tokenUsagePercent(mgRatio)} · 仅估算行</small></div><div class="token-kpi unavailable"><span>不可用 Agent</span><strong>${unavailableCount}</strong><small>窗口内没有可用的实测或估算记录</small></div></div><section class="token-chart-card"><div class="token-chart-head"><div><h3>MemoryGuard 估算趋势</h3><p>元开销（原始候选）与实际注入的两条线。宿主实测流量在上方独立显示。</p></div><div class="token-legend" aria-label="图例"><span class="token-legend-item"><i class="token-legend-swatch raw"></i>元开销 / 原始候选</span><span class="token-legend-item"><i class="token-legend-swatch injected"></i>实际注入</span></div></div>${tokenUsageChart(payload.series)}</section><section><div class="compact-toolbar"><div class="toolbar-grow"><h2>实际经 MCP 转换的数据</h2><span class="muted">最近同步：${escapeHtml(lastSync)} · 窗口：${tokenUsageState.windowDays} 日</span></div></div>${tokenUsageTable(rows, catalog, payload)}</section></div>`, renderToken);
  renderTokenUsageHostTotal(verifiedHostTotal, renderToken);
  renderTokenUsageSyncControl(payload, renderToken);
  renderTokenUsageRail(payload, rows, lastSync);
}

function renderTokenUsageRail(payload = {}, rows = [], lastSync = '') {
  const measured = rows.filter(row => ['measured', 'mixed'].includes(row.mode)).length;
  const estimated = rows.filter(row => ['estimated', 'mixed'].includes(row.mode)).length;
  railTitle('Token 计量口径', `<div class="rail-section"><div class="rail-title">${payload.status === 'unavailable' ? '等待计量同步' : '数据已分口径'}</div><div class="rail-copy">宿主实测 ${measured} 行 · MemoryGuard 估算 ${estimated} 行</div></div><div class="rail-section"><h4>最近同步</h4><div class="rail-copy">${escapeHtml(lastSync || '未同步')}</div></div><div class="rail-section"><h4>口径说明</h4><div class="rail-copy">宿主输入与输出来自绑定 Agent 的实测同步。MemoryGuard 估算来自确定性上下文计数。两种来源不合计。</div></div>`);
}

async function syncUsageTelemetry({refresh = true} = {}) {
  if (tokenUsageState.syncInFlight) return tokenUsageState.syncInFlight;
  const request = (async () => {
    let result;
    try {
      result = await callApi('sync_usage_telemetry');
    } catch (error) {
      result = {ok: false, status: 'error', error: apiErrorMessage(error, '用量同步失败')};
    }
    tokenUsageState.syncResult = result;
    if (refresh && state.activeTab === 'token-usage') await renderTokenUsage();
    return result;
  })();
  tokenUsageState.syncInFlight = request;
  const button = document.getElementById('token-usage-sync-button');
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = '同步中…';
  }
  try {
    return await request;
  } finally {
    if (tokenUsageState.syncInFlight === request) tokenUsageState.syncInFlight = null;
    const currentButton = document.getElementById('token-usage-sync-button');
    if (currentButton) {
      currentButton.disabled = false;
      currentButton.setAttribute('aria-busy', 'false');
      currentButton.textContent = '同步用量数据';
    }
  }
}

async function renderTokenUsage() {
  const renderToken = takeContentRenderToken('token-usage');
  setContent('<div class="loading">正在加载 Token 计量</div>', renderToken);
  try {
    if (!agentCardsData) {
      try { agentCardsData = await callApi('list_agents'); } catch (_) { agentCardsData = {agents: []}; }
    }
    const [rawTelemetry, bindingsResult] = await Promise.all([
      callApi('get_usage_telemetry', tokenUsageState.windowDays, tokenUsageState.agentKey),
      callApi('list_bindings').catch(() => ({bindings: []})),
    ]);
    const payload = normalizeTokenUsagePayload(rawTelemetry);
    tokenUsageState.payload = payload;
    tokenUsageState.bindings = bindingsResult;
    if (rawTelemetry?.ok === false && tokenUsageErrorKind(payload) === 'error') {
      throw new Error(apiErrorMessage(rawTelemetry, 'Token 计量读取失败'));
    }
    renderTokenUsageView(payload, bindingsResult, renderToken);
  } catch (error) {
    if (!contentRenderIsCurrent(renderToken)) return;
    setContent(`<div class="dashboard-view token-usage-view"><div class="token-chart-card token-error"><h2>Token 计量读取失败</h2><p>${escapeHtml(apiErrorMessage(error, '当前无法读取 Token 计量。'))}</p><div class="finding-actions"><button class="btn btn-primary" type="button" onclick="renderTokenUsage()">重新读取</button></div></div></div>`, renderToken);
    railTitle('Token 计量口径', '<div class="empty-inline">计量读取失败，未生成估算或合计。</div>');
  }
}

function renderStatusRail() {
  if (governanceScopeState.status === 'audit_only') {
    railTitle('治理状态', `${railKey('共享治理', '仅审计', 'danger')}<div class="rail-warning">当前共享组没有可用治理绑定，已停止共享治理投影。</div><div class="rail-link" onclick="switchTab('sources')">去数据源恢复绑定</div>`);
    return;
  }
  if (governanceScopeState.status === 'stale_selection') {
    railTitle('治理状态', `${railKey('共享治理', '未激活', 'danger')}<div class="rail-warning">已清除失效共享组选择：当前没有活动绑定。</div><div class="rail-link" onclick="switchTab('sources')">去数据源选择有效绑定</div>`);
    return;
  }
  if (state.activeTab === 'overview') return renderOverviewRail();
  if (state.activeTab === 'sources') return renderSourcesRail();
  if (state.activeTab === 'findings') return renderRiskRail();
  if (state.activeTab === 'token-usage') {
    const payload = tokenUsageState.payload || {};
    return renderTokenUsageRail(payload, tokenUsageRowsForView(payload), '');
  }
  if (state.activeTab === 'codegraph') { railTitle('CodeGraph', renderCodeGraphRail()); return; }
  if (state.activeTab === 'neurons' && selectedNeuronNode) { railTitle('节点详情', renderNeuronRailDetail(selectedNeuronNode)); return; }
  if (state.activeTab === 'neurons') { railTitle('记忆核心', '<div class="empty-inline">选择图节点后，在这里查看正文、来源与治理操作。</div><div class="rail-section"><button class="btn" type="button" onclick="switchTab(\'governance\')">进入治理台</button></div>'); return; }
  if (state.activeTab === 'rules') return renderRulesRail();
  if (state.activeTab === 'history') return renderHistoryRail();
  const counts = governanceCounts();
  railTitle('治理状态', `${railKey('有效记忆', counts.active === null ? '暂不可用' : String(counts.active))}${railKey('冲突', counts.conflicts === null ? '暂不可用' : String(counts.conflicts), counts.conflicts ? 'alert' : 'zero')}${railKey('隔离', counts.quarantined === null ? '暂不可用' : String(counts.quarantined), counts.quarantined ? 'danger' : 'zero')}`);
}

function setRuleViewTab(tab) {
  ruleViewTab = tab === 'habits' ? 'habits' : 'rules';
  if (ruleRecordsById.size) renderRulesHabits();
}

function selectRuleRecord(memoryId) {
  selectedRuleId = String(memoryId || '');
  document.querySelectorAll('.rule-row').forEach(row => row.classList.toggle('is-selected', row.dataset.memoryId === selectedRuleId));
  renderStatusRail();
}

function renderRulesRail() {
  const record = ruleRecordsById.get(selectedRuleId) || null;
  if (!record) {
    railTitle('规则详情', '<div class="empty-inline">选择规则或习惯后，在这里查看正文、范围、合并来源和命中回执。</div><div class="rail-section"><button class="btn btn-primary" type="button" onclick="document.getElementById(\'rule-create-details\')?.setAttribute(\'open\',\'\')">新增规则</button></div>');
    return;
  }
  const receipts = ruleReceiptsFor(record);
  const merged = Array.isArray(record.supersedes) ? record.supersedes : [];
  const sourceText = Array.isArray(record.sources) ? record.sources.join('、') : (record.source || record.origin || '未返回合并来源');
  railTitle('规则详情', `<div class="rail-section"><div class="rail-title">${escapeHtml(displayTitle(record))}</div><span class="chip ${record.injection_policy === 'always' ? 'chip-confirmed' : 'chip-info'}">${record.injection_policy === 'always' ? '强制' : '按需'}</span></div><div class="rail-section"><h4>正文</h4><div class="rail-copy">${escapeHtml(displayBody(record) || '暂无正文内容')}</div></div><div class="rail-section"><h4>适用范围</h4><div class="rail-copy">${escapeHtml(ruleAudience(record))}</div></div><div class="rail-section"><h4>合并来源</h4><div class="rail-copy">${escapeHtml(merged.length ? `${merged.length} 条旧记忆` : sourceText)}</div></div><div class="rail-section"><h4>命中回执</h4>${receipts.length ? receipts.slice(-3).map(renderRuleReceiptActions).join('') : '<div class="rail-copy">暂无命中回执。</div>'}</div><div class="rail-section"><button class="btn btn-primary" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(record.memory_id)}">管理适用范围</button><button class="btn" type="button" data-mg-action="neuron-rule-edit-body" data-memory-id="${escapeHtml(record.memory_id)}">编辑正文</button></div><details class="compact-secondary"><summary>技术详情</summary><div class="rail-copy">memory_id: ${escapeHtml(record.memory_id || '')}<br>分类: ${escapeHtml(record.kind || '')}</div></details>`);
}

function ruleTableRows(records) {
  return records.map(record => {
    const state = rulePreviewState(record);
    const stats = ruleStatsFor(record);
    const kind = rulePageBucket(record);
    const hit = stats ? Number(stats.total || 0) : '暂不可用';
    const selected = String(record.memory_id || '') === selectedRuleId;
    return `<tr class="rule-row ${selected ? 'is-selected' : ''}" data-memory-id="${escapeHtml(record.memory_id || '')}" onclick="selectRuleRecord('${escapeHtml(record.memory_id || '')}')"><td><strong>${escapeHtml(displayTitle(record))}</strong><span class="rule-body-preview">${escapeHtml(displayBody(record))}</span></td><td>${escapeHtml({mandatory:'强制', preferences:'习惯', procedures:'流程', corrections:'纠错', projects:'项目'}[kind] || record.kind || '规则')}</td><td>${escapeHtml(ruleAudience(record))}</td><td>${escapeHtml(String(hit))}</td><td><span class="chip ${record.status === 'active' ? (state === 'excluded' ? 'chip-high' : 'chip-confirmed') : 'chip-medium'}">${escapeHtml(record.status === 'active' ? (state === 'effective' ? '生效' : '已启用') : record.status || '待确认')}</span></td><td><div class="table-actions"><button class="btn" type="button" onclick="event.stopPropagation();selectRuleRecord('${escapeHtml(record.memory_id || '')}')">详情</button><button class="btn" type="button" data-mg-action="rule-edit" data-memory-id="${escapeHtml(record.memory_id || '')}" onclick="event.stopPropagation()">管理</button></div></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="empty-note">当前筛选条件下没有规则或习惯。</td></tr>';
}

async function renderRulesHabits() {
  setContent('<div class="loading">正在读取规则与习惯…</div>');
  try {
    const groupId = activeShareGroupId || 'default';
    const [data, options, decisions, metrics, receipts, exceptions] = await Promise.all([
      callApi('list_rules_habits', groupId), callApi('get_rule_scope_options', groupId),
      callApiOptional('list_rule_decisions', {decisions: [], total: 0}, groupId, 50),
      callApiOptional('get_rule_auto_scope_metrics', {stats: [], auto_scope: {}}, groupId),
      callApiOptional('list_rule_match_receipts', {receipts: [], total: 0}, groupId, '', activeAgentInstanceId, 50),
      callApiOptional('list_rule_exceptions', {exceptions: [], total: 0}, groupId, ''),
    ]);
    if (data.error) throw new Error(data.error);
    if (options.error) throw new Error(options.error);
    ruleDecisionRows = Array.isArray(decisions) ? decisions : (decisions.decisions || decisions.items || []);
    ruleScopeMetrics = metrics || {};
    ruleReceiptRows = Array.isArray(receipts) ? receipts : (receipts.receipts || receipts.items || []);
    ruleExceptionRows = Array.isArray(exceptions) ? exceptions : (exceptions.exceptions || exceptions.items || []);
    ruleScopeOptions = options;
    const pageBuckets = rulePageBuckets(data);
    const allRecords = Object.values(pageBuckets).flat();
    // Presentation-only dedupe: canonical memory_id remains source authority.
    const uniqueRecords = [...new Map(allRecords.filter(record => record.memory_id).map(record => [record.memory_id, record])).values()];
    ruleRecordsById = new Map(uniqueRecords.map(record => [record.memory_id, record]));
    if (!ruleRecordsById.has(selectedRuleId)) selectedRuleId = '';
    const isHabit = record => String(record.kind || record.rule_kind || '').toLowerCase() === 'preference' || rulePageBucket(record) === 'preferences';
    const records = uniqueRecords.filter(record => (ruleViewTab === 'habits' ? isHabit(record) : !isHabit(record))).filter(record => {
      const ranges = (record.assignments || []).map(item => item.target_type);
      if (ruleRangeFilter !== 'all' && !ranges.includes(ruleRangeFilter)) return false;
      const state = rulePreviewState(record);
      return !(rulePreviewAgentId && ruleVisibilityFilter !== 'all' && record.injection_policy === 'always' && state !== ruleVisibilityFilter);
    });
    const agents = options.agents || []; const projects = options.projects || []; const providers = options.providers || []; const roles = options.runtime_roles || [];
    const diagnostics = `<div class="page-actions"><label class="field"><span>预览 Agent</span><select onchange="setRulePreviewAgent(this.value)"><option value="">不做有效性预览</option>${ruleSelectOptions(agents, rulePreviewAgentId)}</select></label><label class="field"><span>项目</span><select onchange="setRulePreviewProject(this.value)"><option value="">未确认项目</option>${ruleSelectOptions(projects, rulePreviewProjectRef)}</select></label><label class="field"><span>宿主</span><select onchange="setRulePreviewProvider(this.value)"><option value="">未确认宿主</option>${ruleSelectOptions(providers, rulePreviewProvider)}</select></label><label class="field"><span>运行角色</span><select onchange="setRulePreviewRuntimeRole(this.value)"><option value="">未确认运行角色</option>${ruleSelectOptions(roles, rulePreviewRuntimeRole)}</select></label></div>`;
    setContent(`<div class="dashboard-view rules-view"><div class="dashboard-main"><div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Rules & Habits</span><h2>规则与习惯</h2></div><div class="page-tabs" role="tablist"><button class="${ruleViewTab === 'rules' ? 'active' : ''}" onclick="setRuleViewTab('rules')">规则</button><button class="${ruleViewTab === 'habits' ? 'active' : ''}" onclick="setRuleViewTab('habits')">习惯</button></div><button class="btn btn-primary" type="button" onclick="document.getElementById('rule-create-details')?.setAttribute('open','')">新增</button></div><div class="data-table-wrap"><table class="data-table rule-table"><thead><tr><th>名称</th><th>类型</th><th>范围</th><th>命中</th><th>状态</th><th>操作</th></tr></thead><tbody>${ruleTableRows(records)}</tbody></table></div><details class="compact-secondary" id="rule-create-details"><summary>新增规则</summary>${renderRuleCreatePanel(options)}</details><details class="compact-secondary"><summary>自动决策 · ${ruleDecisionRows.length} 条（默认折叠）</summary>${renderRuleAutoScopePanel()}</details><details class="compact-secondary"><summary>诊断与高级筛选</summary>${diagnostics}<div class="page-actions"><label class="field"><span>显示</span><select onchange="setRuleVisibilityFilter(this.value)"><option value="effective" ${ruleVisibilityFilter === 'effective' ? 'selected' : ''}>仅生效</option><option value="excluded" ${ruleVisibilityFilter === 'excluded' ? 'selected' : ''}>仅排除</option><option value="all" ${ruleVisibilityFilter === 'all' ? 'selected' : ''}>全部</option></select></label><label class="field"><span>范围</span><select onchange="setRuleRangeFilter(this.value)"><option value="all">全部范围</option>${['agent','group','project','agent_project','provider','runtime_role','system'].map(type => `<option value="${type}" ${ruleRangeFilter === type ? 'selected' : ''}>${escapeHtml(type)}</option>`).join('')}</select></label></div></details></div></div>`);
    renderRulesRail();
  } catch (error) {
    setContent(`<div class="dashboard-view"><div class="dashboard-main"><div class="compact-toolbar"><span class="eyebrow">Rules & Habits</span><h2>规则与习惯</h2></div><div class="empty-inline">规则加载失败：${escapeHtml(error.message || error)}</div></div></div>`);
    railTitle('规则详情', '<div class="empty-inline">规则数据不可用。</div>');
  }
}

function selectedHistorySession() {
  return (historyPageData?.sessions || []).find(item => String(item.session_id || '') === selectedHistorySessionId) || null;
}

function historySessionsForToolbar() {
  const sessions = Array.isArray(historyPageData?.sessions) ? historyPageData.sessions : [];
  const agent = document.getElementById('history-agent-filter')?.value || '';
  const status = document.getElementById('history-status-filter')?.value || '';
  const range = document.getElementById('history-time-filter')?.value || '';
  const query = String(document.getElementById('history-search')?.value || '').trim().toLowerCase();
  const cutoff = range ? Date.now() - Number(range) * 24 * 60 * 60 * 1000 : 0;
  return sessions.filter(session => {
    const owner = String(session.owner_agent_instance_id || session.agent_instance_id || '');
    if (agent && owner !== agent) return false;
    if (status === 'summarized' && !session.summarized) return false;
    if (status === 'unsummarized' && session.summarized) return false;
    if (cutoff) { const stamp = Date.parse(session.created_at || session.imported_at || ''); if (Number.isFinite(stamp) && stamp < cutoff) return false; }
    if (query) return [historySessionTitle(session), session.summary, session.preview_excerpt, session.provider].join(' ').toLowerCase().includes(query);
    return true;
  });
}

function historySessionRows(sessions) {
  return sessions.map(session => {
    const id = String(session.session_id || '');
    const owner = session.owner_agent_instance_id || session.agent_instance_id || '';
    const selected = id === selectedHistorySessionId;
    return `<tr class="history-row ${selected ? 'is-selected' : ''}" data-session-id="${escapeHtml(id)}" onclick="selectHistorySession('${escapeHtml(id)}')"><td><strong>${escapeHtml(historySessionTitle(session))}</strong><span class="history-summary">${escapeHtml(session.summary || session.preview_excerpt || '尚无可用概要')}</span></td><td>${escapeHtml(agentDisplayName(owner, session.provider || '未知 Agent'))}</td><td>${escapeHtml(session.provider || 'local')}</td><td>${escapeHtml(session.created_at || session.imported_at || '暂不可用')}</td><td><span class="chip ${session.summarized ? 'chip-confirmed' : 'chip-info'}">${session.summarized ? '已摘要' : '待摘要'}</span></td><td><div class="table-actions"><button class="btn" type="button" onclick="event.stopPropagation();selectHistorySession('${escapeHtml(id)}')">详情</button><button class="btn" type="button" data-mg-action="history-read-session" data-session-id="${escapeHtml(id)}" onclick="event.stopPropagation()">阅读</button></div></td></tr>`;
  }).join('') || '<tr><td colspan="6" class="empty-note">当前筛选条件下没有已索引会话。</td></tr>';
}

function filterHistorySessions() {
  const host = document.getElementById('history-results');
  if (!host || !historyPageData) return;
  host.innerHTML = `<div class="data-table-wrap"><table class="data-table history-table"><thead><tr><th>会话</th><th>Agent</th><th>宿主</th><th>时间</th><th>状态</th><th>操作</th></tr></thead><tbody>${historySessionRows(historySessionsForToolbar())}</tbody></table></div>`;
}

function selectHistorySession(sessionId) {
  selectedHistorySessionId = String(sessionId || '');
  document.querySelectorAll('.history-row').forEach(row => row.classList.toggle('is-selected', row.dataset.sessionId === selectedHistorySessionId));
  renderStatusRail();
}

function renderHistoryRail() {
  const session = selectedHistorySession();
  if (!session) {
    railTitle('会话详情', '<div class="empty-inline">选择会话后，在此查看归属、时间、消息与证据统计。</div>');
    return;
  }
  const owner = session.owner_agent_instance_id || session.agent_instance_id || '';
  const canDelete = !!activeAgentInstanceId && owner === activeAgentInstanceId;
  railTitle('会话详情', `<div class="rail-section"><div class="rail-title">${escapeHtml(historySessionTitle(session))}</div><span class="chip chip-info">${escapeHtml(session.provider || 'local')}</span></div><div class="rail-section"><h4>归属</h4><div class="rail-copy">${escapeHtml(agentDisplayName(owner, session.provider || '未知 Agent'))}<br>${escapeHtml(session.created_at || session.imported_at || '暂无时间')}</div></div><div class="rail-section"><h4>会话统计</h4><div class="rail-copy">${Number(session.turn_count || 0)} 条消息 · ${Number(session.evidence_count || 0)} 条已萃取证据</div></div><div class="rail-section"><h4>概要</h4><div class="rail-copy">${escapeHtml(session.summary || session.preview_excerpt || '尚无可用概要')}</div></div><div class="rail-section"><div class="finding-actions"><button class="btn btn-primary" type="button" data-mg-action="history-read-session" data-session-id="${escapeHtml(session.session_id)}">阅读原文</button><button class="btn" type="button" data-mg-action="history-extract" data-session-id="${escapeHtml(session.session_id)}">萃取预览</button><button class="btn" type="button" data-mg-action="history-export" data-session-id="${escapeHtml(session.session_id)}">导出</button>${canDelete ? `<button class="btn btn-danger" type="button" data-mg-action="history-delete" data-session-id="${escapeHtml(session.session_id)}">删除</button>` : ''}</div></div>`);
}

async function renderHistory() {
  setContent('<div class="loading">正在读取已索引对话历史…</div>');
  const scopeReady = await ensureGovernanceScope();
  if (!scopeReady) {
    setContent(`<div class="dashboard-view history-view"><div class="dashboard-main"><div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Conversation History</span><h2>对话历史</h2></div></div><div class="empty-inline">需要有效治理范围后才能读取会话。旧会话扫描仍可在下方展开。</div><details class="compact-secondary"><summary>扫描或导入旧会话</summary>${historyBackfillLoadingPanel()}</details></div></div>`);
    railTitle('会话详情', '<div class="empty-inline">选择有效治理范围后显示会话。</div>');
    void refreshHistoryBackfillPanel();
    return;
  }
  try {
    const data = await callApi('list_history_sessions', historyScope(), 50, 0, null, '', '');
    if (data.error || data.ok === false) throw new Error(apiErrorMessage(data, '对话历史加载失败'));
    historyPageData = data;
    if (!(data.sessions || []).some(item => String(item.session_id || '') === selectedHistorySessionId)) selectedHistorySessionId = '';
    const agents = [...new Map((data.sessions || []).map(item => { const id = item.owner_agent_instance_id || item.agent_instance_id || ''; return [id, agentDisplayName(id, item.provider || '未知 Agent')]; })).entries()];
    setContent(`<div class="dashboard-view history-view"><div class="dashboard-main"><div class="compact-toolbar"><div class="toolbar-grow"><span class="eyebrow">Conversation History</span><h2>对话历史</h2></div><label>Agent <select id="history-agent-filter" onchange="filterHistorySessions()"><option value="">全部</option>${agents.map(([id, label]) => `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`).join('')}</select></label><label>时间 <select id="history-time-filter" onchange="filterHistorySessions()"><option value="">全部</option><option value="7">近 7 天</option><option value="30">近 30 天</option></select></label><label>状态 <select id="history-status-filter" onchange="filterHistorySessions()"><option value="">全部</option><option value="summarized">已摘要</option><option value="unsummarized">待摘要</option></select></label><input id="history-search" type="search" placeholder="搜索会话" oninput="filterHistorySessions()"><button class="btn" type="button" data-mg-action="history-search">深度搜索</button></div><div id="history-results"><div class="data-table-wrap"><table class="data-table history-table"><thead><tr><th>会话</th><th>Agent</th><th>宿主</th><th>时间</th><th>状态</th><th>操作</th></tr></thead><tbody>${historySessionRows(data.sessions || [])}</tbody></table></div></div><details class="compact-secondary"><summary>扫描或导入旧会话</summary>${historyBackfillLoadingPanel()}</details></div></div>`);
    renderHistoryRail();
    void refreshHistoryBackfillPanel();
    if (historyFocusSessionId) { const sessionId = historyFocusSessionId; historyFocusSessionId = ''; await readHistorySession(sessionId); }
  } catch (error) {
    setContent(`<div class="dashboard-view history-view"><div class="dashboard-main"><div class="compact-toolbar"><span class="eyebrow">Conversation History</span><h2>对话历史</h2></div><div class="empty-inline">对话历史加载失败：${escapeHtml(error.message || error)}</div></div></div>`);
    railTitle('会话详情', '<div class="empty-inline">对话历史数据不可用。</div>');
  }
}

async function readHistorySession(sessionId) {
  selectHistorySession(sessionId);
  try {
    const data = await callApi('history_read', buildHistoryReadRequest({sessionId, limit: 100, offset: 0}));
    if (data.error || data.ok === false) throw new Error(historyErrorMessage(data, '会话读取失败'));
    const host = document.getElementById('history-results');
    if (host) host.innerHTML = `<div class="compact-toolbar"><button class="btn" data-mg-action="history-back">返回会话列表</button></div>${(data.turns || []).map(turn => `<article class="memory-card"><strong>${escapeHtml(turn.role || '')}</strong><pre class="raw-content">${escapeHtml(turn.content || '')}</pre></article>`).join('') || '<div class="empty-inline">该会话没有可读取的原文。</div>'}`;
  } catch (error) { showToast('会话读取失败：' + error, 'error'); }
}

function renderCodeGraphRail() {
  const graph = codeGraph || {};
  const project = selectedCodeGraphProjectRow() || {};
  if (selectedCodeGraphNode) {
    const node = selectedCodeGraphNode;
    return `<div class="rail-section"><div class="rail-title">${escapeHtml(codeGraphNodeLabel(node))}</div><span class="chip chip-info">${escapeHtml(node.node_kind || 'node')}</span></div><div class="rail-section"><h4>${node.node_kind === 'symbol' ? '符号' : '文件'}</h4><div class="rail-copy">${escapeHtml(node.signature || node.path || node.kind || '未返回详情')}</div></div>${node.path ? `<div class="rail-section"><h4>路径</h4><code>${escapeHtml(guiPathText(node.path))}</code></div>` : ''}<details class="compact-secondary"><summary>技术详情</summary><div class="rail-copy">id: ${escapeHtml(node.id || '')}<br>行号: ${escapeHtml(node.line_start || '')}${node.line_end ? `–${escapeHtml(node.line_end)}` : ''}</div></details>`;
  }
  const automation = codeGraphAutomationState(graph);
  return `<div class="rail-section"><div class="rail-title">${escapeHtml(project.label || graph.project_ref || '未选择项目')}</div><div class="rail-copy">分支：${escapeHtml(project.branch || graph.branch || '后端未返回')}</div></div>${railKey('文件/节点', String(graph.node_count ?? (graph.nodes || []).length))}${railKey('关系', String(graph.edge_count ?? (graph.edges || []).length))}<div class="rail-section"><h4>自动状态</h4><span class="chip chip-${automation.tone}" title="${escapeHtml(automation.detail)}">${escapeHtml(automation.label)}</span><div class="rail-copy">${escapeHtml(automation.detail)}</div></div><div class="rail-section"><button class="btn btn-primary" type="button" onclick="refreshCodeGraph()">刷新 CodeGraph</button></div>`;
}

function filterNeuronGraph() {
  if (!cyInstance) return;
  const query = String(document.getElementById('neuron-search')?.value || '').trim().toLowerCase();
  const kind = String(document.getElementById('neuron-kind-filter')?.value || '');
  cyInstance.nodes().forEach(node => {
    const data = node.data();
    const text = [data.label, data.memory_id, data.kind, data.record_kind].join(' ').toLowerCase();
    const visible = (!query || text.includes(query)) && (!kind || data.record_kind === kind);
    node.style('display', visible ? 'element' : 'none');
  });
  cyInstance.edges().forEach(edge => edge.style('display', edge.source().style('display') === 'none' || edge.target().style('display') === 'none' ? 'none' : 'element'));
}

function sortNeuronGraph() {
  const value = document.getElementById('neuron-sort')?.value || 'topology';
  if (value === 'topology') return fitNeuronGraph();
  const first = cyInstance?.nodes().filter(node => node.style('display') !== 'none').toArray().sort((left, right) => String(left.data('label') || '').localeCompare(String(right.data('label') || '')))[0];
  if (first) selectNeuron(first.id(), true);
  showToast('标题排序用于当前检索焦点；图谱保持真实拓扑布局。', 'info');
}

window.addEventListener('pywebviewready', init);
setTimeout(() => {
  if (!state.report && (window.__MG_SESSION__ || (window.pywebview && window.pywebview.api))) init();
}, 2000);
</script>
</body>
</html>"""
