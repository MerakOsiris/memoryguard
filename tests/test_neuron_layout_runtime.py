from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from memoryguard.interactive import render_interactive_html


def _run_dense_layout() -> dict:
    html = render_interactive_html()
    runner = r"""
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const matches = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1]).filter(Boolean);
const source = matches[matches.length - 1];
function element() {
  return {
    value: '', innerHTML: '', textContent: '', style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    appendChild() {}, removeChild() {}, addEventListener() {}, removeEventListener() {},
    focus() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  };
}
const sandbox = {
  console,
  document: {
    getElementById() { return element(); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {}, removeEventListener() {},
    createElement() { return element(); },
    documentElement: { dataset: {} },
    body: { appendChild() {}, removeChild() {} },
  },
  window: { addEventListener() {}, removeEventListener() {}, pywebview: null },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  navigator: { language: 'zh-CN' },
  confirm() { return true; },
  alert() {},
  fetch() { throw new Error('fetch should not be used in layout smoke'); },
  setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {},
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'memoryguard-interactive.js' });

const nodes = [
  {id: 'main', node_kind: 'root', kind: 'root'},
  {id: 'virtual-rules-habits', parent_id: 'main', node_kind: 'virtual_category', kind: 'rules_habits'},
  {id: 'virtual-conversation-history', parent_id: 'main', node_kind: 'virtual_category', kind: 'conversation_history'},
  {id: 'rules-procedures', parent_id: 'virtual-rules-habits', node_kind: 'virtual_bucket', kind: 'procedures'},
  {id: 'history-project:p', parent_id: 'virtual-conversation-history', node_kind: 'history_project', kind: 'project'},
  {id: 'history-agent:a', parent_id: 'history-project:p', node_kind: 'history_agent', kind: 'agent'},
];
for (let i = 0; i < 16; i++) {
  nodes.push({
    id: `rule:${i}`, parent_id: 'rules-procedures', node_kind: 'virtual_rule_ref',
    kind: i % 2 ? 'procedure' : 'preference',
  });
}
for (let i = 0; i < 50; i++) {
  nodes.push({
    id: `history-session:${i}`, parent_id: 'history-agent:a', node_kind: 'history_session', kind: 'session',
  });
}
const positions = sandbox.neuronNodePositions(nodes);
const ids = nodes.map(node => node.id);
const finite = ids.every(id => Number.isFinite(positions[id]?.x) && Number.isFinite(positions[id]?.y));
const seen = new Set();
let exactOverlaps = 0;
for (const id of ids) {
  const p = positions[id];
  const key = `${p.x.toFixed(6)}:${p.y.toFixed(6)}`;
  if (seen.has(key)) exactOverlaps += 1;
  seen.add(key);
}
const historyIds = ids.filter(id => id.startsWith('history-session:'));
let minHistoryDistance = Infinity;
for (let i = 0; i < historyIds.length; i++) {
  for (let j = i + 1; j < historyIds.length; j++) {
    const a = positions[historyIds[i]], b = positions[historyIds[j]];
    minHistoryDistance = Math.min(minHistoryDistance, Math.hypot(a.x - b.x, a.y - b.y));
  }
}
const rules = positions['virtual-rules-habits'];
const history = positions['virtual-conversation-history'];
const mainBranchDistance = Math.hypot(rules.x - history.x, rules.y - history.y);
const rulesRadius = Math.hypot(rules.x, rules.y);
const historyRadius = Math.hypot(history.x, history.y);

const codeNodes = [];
for (let fileIndex = 0; fileIndex < 12; fileIndex++) {
  const fileId = `file:${fileIndex}`;
  codeNodes.push({id: fileId, node_kind: 'file', path: `src/module_${fileIndex}.py`});
}
for (let symbolIndex = 0; symbolIndex < 88; symbolIndex++) {
  const fileIndex = symbolIndex % 12;
  codeNodes.push({
    id: `symbol:${symbolIndex}`, node_kind: 'symbol', file_id: `file:${fileIndex}`,
    label: `symbol_${symbolIndex}`, kind: 'function',
  });
}
const semanticGraph = {
  nodes: [
    {id:'main-color', node_kind:'root', kind:'root'},
    {id:'rule-pref', parent_id:'main-color', node_kind:'virtual_rule_ref', virtual_category:'rules_habits', kind:'preference'},
    {id:'rule-proc', parent_id:'main-color', node_kind:'virtual_rule_ref', virtual_category:'rules_habits', kind:'procedure'},
    {id:'rule-correction', parent_id:'main-color', node_kind:'virtual_rule_ref', virtual_category:'rules_habits', kind:'correction'},
    {id:'history-project-color', parent_id:'main-color', node_kind:'history_project', virtual_category:'conversation_history', kind:'project'},
    {id:'history-agent-color', parent_id:'history-project-color', node_kind:'history_agent', virtual_category:'conversation_history', kind:'agent'},
    {id:'history-session-color', parent_id:'history-agent-color', node_kind:'history_session', virtual_category:'conversation_history', kind:'session'},
  ],
  edges: [],
};
const semanticElements = sandbox.graphElements(semanticGraph)
  .filter(item => item.data && !item.data.source);
const semanticColors = Object.fromEntries(semanticElements.map(item => [item.data.id, item.data.bg]));
const ruleColorsDistinct = new Set([
  semanticColors['rule-pref'], semanticColors['rule-proc'], semanticColors['rule-correction'],
]).size === 3;
const historyColorsDistinct = new Set([
  semanticColors['history-project-color'], semanticColors['history-agent-color'], semanticColors['history-session-color'],
]).size === 3;

const codePositions = sandbox.codeGraphNodePositions({nodes: codeNodes});
const overview = sandbox.codeGraphDisplayGraph({nodes: codeNodes, edges: [
  {source: 'symbol:0', target: 'symbol:1'},
  {source: 'symbol:12', target: 'symbol:13'},
  {source: 'symbol:1', target: 'symbol:0'},
  {source: 'file:0', target: 'symbol:0'},
  {source: 'missing', target: 'symbol:0'},
]});
const overviewValid = overview.nodes.length === 12
  && overview.edges.length === 2
  && overview.edges.some(edge => edge.source === 'file:0' && edge.target === 'file:1' && edge.weight === 2)
  && overview.nodes.reduce((sum, node) => sum + node.visible_symbols, 0) === 88;
vm.runInContext("codeGraphView = 'symbols'; codeGraphFocusFile = 'file:0';", sandbox);
const focused = sandbox.codeGraphDisplayGraph({nodes: codeNodes, edges: [
  {source: 'file:0', target: 'symbol:0'}, {source: 'symbol:0', target: 'symbol:1'},
]});
const focusValid = focused.nodes.length === 9 && focused.edges.length === 1
  && focused.nodes.every(node => node.id === 'file:0' || node.file_id === 'file:0');
const codeFinite = codeNodes.every(node => Number.isFinite(codePositions[node.id]?.x) && Number.isFinite(codePositions[node.id]?.y));
const codeSeen = new Set();
let codeExactOverlaps = 0;
for (const node of codeNodes) {
  const p = codePositions[node.id];
  const key = `${p.x.toFixed(6)}:${p.y.toFixed(6)}`;
  if (codeSeen.has(key)) codeExactOverlaps += 1;
  codeSeen.add(key);
}
const fileIds = codeNodes.filter(node => node.node_kind === 'file').map(node => node.id);
let minFileDistance = Infinity;
for (let i = 0; i < fileIds.length; i++) {
  for (let j = i + 1; j < fileIds.length; j++) {
    const a = codePositions[fileIds[i]], b = codePositions[fileIds[j]];
    minFileDistance = Math.min(minFileDistance, Math.hypot(a.x - b.x, a.y - b.y));
  }
}
process.stdout.write(JSON.stringify({
  finite, exactOverlaps, minHistoryDistance, mainBranchDistance, rulesRadius, historyRadius,
  codeFinite, codeExactOverlaps, minFileDistance, ruleColorsDistinct, historyColorsDistinct, overviewValid, focusValid,
}));
"""
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "neuron-layout.html"
        html_path.write_text(html, encoding="utf-8")
        result = subprocess.run(
            ["node", "-e", runner, str(html_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        pytest.fail(result.stderr or result.stdout)
    return json.loads(result.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js unavailable")
def test_dense_neuron_layout_separates_rule_and_history_leaves() -> None:
    result = _run_dense_layout()
    assert result["finite"] is True
    assert result["exactOverlaps"] == 0
    assert result["minHistoryDistance"] >= 30
    # 一级分支必须围绕主光点形成紧凑环，而不是被碰撞算法推出大空洞。
    assert 115 <= result["rulesRadius"] <= 140
    assert 115 <= result["historyRadius"] <= 140
    assert 220 <= result["mainBranchDistance"] <= 290
    assert result["codeFinite"] is True
    assert result["codeExactOverlaps"] == 0
    assert result["minFileDistance"] >= 280
    assert result["overviewValid"] is True
    assert result["focusValid"] is True
    assert result["ruleColorsDistinct"] is True
    assert result["historyColorsDistinct"] is True
