import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import memoryguard.host_hooks as host_hooks
from memoryguard.content import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.cli import main as cli_main
from memoryguard.host_hooks import (
    HostHookManager,
    _binding_plane_for_workspace,
    _validate_binding,
    _state_path,
    run_hook,
    set_hook_mode,
)
from memoryguard.provider_adapters import CodexAdapter
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _bind(workspace: Path, agent_id: str, group_id: str) -> dict:
    _activate_v2_host_workspace(workspace)
    return GroupControlService(workspace, write=True).bind_agent(
        agent_id,
        group_id,
        idempotency_key=f"test-bind:{agent_id}:{group_id}",
    )


def _activate_v2_host_workspace(workspace: Path) -> None:
    manager = ManifestManager(workspace)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    RuleV2Store(workspace)
    ProjectionStore(workspace)
    ContentStore(workspace)
    RuntimeStore(workspace)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="host-hooks-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="host-hooks-source",
        target_digest="host-hooks-target",
        manifest_digest="host-hooks-manifest",
        digests={"validator_passed": True, "checkpoints": {"host_hooks": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _seed_v2_atom(
    workspace: Path,
    *,
    memory_id: str,
    body: str,
    kind: str = "preference",
    agent: str = "codex-agent",
    group: str = "group-a",
) -> None:
    if ManifestManager(workspace).current().state is not ManifestState.V2_ACTIVE:
        _activate_v2_host_workspace(workspace)
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    governance = GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    scope = {
        "workspace_id": str(workspace.resolve()),
        "share_group_id": group,
        "agent_instance_id": agent,
        "project_ref": canonical_project_ref(str(workspace.resolve())),
        "provider": "codex",
        "runtime_role": "root",
        "actor": "host-hooks-fixture",
        "authority": "manual",
    }
    atom = MemoryAtom(
        memory_id=memory_id,
        body=body,
        kind=kind,
        status="active",
        confidence=0.9,
        workspace_id=scope["workspace_id"],
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=scope["project_ref"],
        provider="codex",
        runtime_role="root",
    )
    persisted, _ = governance.put_atom(
        atom,
        context=scope,
        evidence=[{"source_ref": f"fixture:{memory_id}", "authority": "system"}],
        reason="host hooks V2 fixture",
        confidence=0.9,
        idempotency_key=f"host-hooks-memory:{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])


class _TestV2Facade:
    def __init__(self, packet=None, **envelope):
        self.packet = packet or {"items": [], "mandatory_items": [], "mandatory_rule_ids": []}
        self.envelope = envelope

    def state_snapshot(self):
        return {"state": "V2_ACTIVE", "generation": 1}

    def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
        return {**self.envelope, "packet": dict(self.packet)}


def _v2_context(
    workspace: Path,
    *,
    agent: str = "codex-agent",
    group: str = "group-a",
    provider: str = "codex",
    admin: bool = True,
):
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2.native_ports import bind_native_transport_context

    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"host-hooks-{agent}",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=group,
        project_ref=str(workspace.resolve()).casefold(),
        provider=provider,
        runtime_role="root",
        entrypoint="mcp",
    )


def _v2_port(workspace: Path):
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort

    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )


def _v2_bind(workspace: Path, agent: str = "codex-agent", group: str = "group-a"):
    return _bind(workspace, agent, group)


def _seed_v2_history(
    workspace: Path,
    events,
    *,
    source_id: str = "host-hooks-v2-history",
):
    _activate_v2_host_workspace(workspace)
    from memoryguard.content.conversation_sync import ConversationSync
    from memoryguard.content.store import ContentStore

    return ConversationSync(ContentStore(workspace)).sync(source_id, events)


def _v2_turn_ids(workspace: Path) -> list[str]:
    import sqlite3

    from memoryguard.content.store import ContentStore

    with sqlite3.connect(ContentStore(workspace).db_path) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT turn_id FROM conversation_turns ORDER BY ordinal,turn_id"
            ).fetchall()
        ]


def _first_v2_turn_id(workspace: Path) -> str:
    return _v2_turn_ids(workspace)[0]


def _seed_v2_rule_receipt(
    workspace: Path,
    *,
    agent: str = "codex-agent",
    group: str = "group-a",
    session_id: str = "host-hooks-rule-session",
):
    from memoryguard.rule_definition import build_definition
    from memoryguard.rules.v2_store import RuleV2Store

    _activate_v2_host_workspace(workspace)
    _v2_bind(workspace, agent, group)
    store = RuleV2Store(workspace)
    definition = store.upsert_definition(
        build_definition("Always preserve the explicit V2 rule receipt", kind="procedure")
    )
    receipt_id = f"receipt-{session_id}"
    store.record_receipt({
        "receipt_id": receipt_id,
        "definition_id": definition.definition_id,
        "source_rule_id": "source-v2-rule",
        "share_group_id": group,
        "agent_instance_id": agent,
        "project_ref": str(workspace.resolve()).casefold(),
        "session_id": session_id,
        "task_hash": "host-hooks-task",
        "selection_digest": "host-hooks-selection",
        "metadata_json": "{}",
        "created_at": "2026-08-12T00:00:00+00:00",
    })
    return store, receipt_id


@pytest.fixture(autouse=True)
def _pure_v2_host_seam(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
):
    fixture_home = tmp_path / "host-seam-home"
    fixture_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fixture_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    if request.node.name in {
        "test_binding_plane_retires_v1_workspace_without_legacy_fallback",
        "test_binding_validation_never_falls_back_to_legacy_in_v2",
    }:
        return
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda workspace: _TestV2Facade(),
    )
    monkeypatch.setattr(host_hooks, "_validate_binding", lambda *args, **kwargs: None)


def test_binding_plane_retires_v1_workspace_without_legacy_fallback(tmp_path: Path) -> None:
    GroupControlService(tmp_path, write=True).bind_agent(
        "legacy-agent", "group-a", idempotency_key="retired-v1-binding",
    )
    with pytest.raises(ValueError, match="v2_upgrade_required"):
        _binding_plane_for_workspace(tmp_path)


def test_binding_validation_never_falls_back_to_legacy_in_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    GroupControlService(tmp_path, write=True).bind_agent(
        "legacy-agent", "group-a", idempotency_key="v2-binding-validation",
    )
    monkeypatch.setattr(
        "memoryguard.host_hooks._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )
    with pytest.raises(ValueError, match="active binding not found"):
        _validate_binding(tmp_path, "agent-a", "group-a")

    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    _validate_binding(tmp_path, "agent-a", "group-a")


@pytest.mark.parametrize(
    ("provider", "relative_config", "event_name"),
    [
        ("claude", ".claude/settings.json", "UserPromptSubmit"),
        ("codex", ".codex/hooks.json", "UserPromptSubmit"),
        ("cursor", ".cursor/hooks.json", "beforeSubmitPrompt"),
    ],
)
def test_hook_install_is_idempotent_and_preserves_other_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    relative_config: str,
    event_name: str,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, f"{provider}-agent", "group-a")

    config_path = home / relative_config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "cursor":
        original = {
            "version": 1,
            "hooks": {
                "stop": [{"command": "python user-stop.py"}],
            },
        }
    else:
        original = {
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "type": "command",
                        "command": "python user-stop.py",
                    }],
                }],
            },
        }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    manager = HostHookManager(workspace)
    first = manager.install(
        provider,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
    )
    second = manager.install(
        provider,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    serialized = json.dumps(data)
    assert first["configured"] and second["configured"]
    expected_handlers = 6 if provider == "cursor" else 7
    if provider == "cursor":
        owned_handlers = [
            entry
            for entries in data["hooks"].values()
            for entry in entries
            if "memoryguard.host_hooks" in entry.get("command", "")
        ]
    else:
        owned_handlers = [
            handler
            for groups in data["hooks"].values()
            for group in groups
            for handler in group.get("hooks", [])
            if "memoryguard.host_hooks" in handler.get("command", "")
        ]
    assert len(owned_handlers) == expected_handlers
    assert serialized.count("python user-stop.py") == 1
    assert event_name in data["hooks"]

    removed = manager.uninstall(provider)
    remaining = json.loads(config_path.read_text(encoding="utf-8"))
    remaining_text = json.dumps(remaining)
    assert removed["configured"] is False
    assert "memoryguard.host_hooks" not in remaining_text
    assert remaining_text.count("python user-stop.py") == 1


def test_codex_hook_config_uses_utf8_commands_for_each_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")

    HostHookManager(workspace).install(
        "codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
    )

    data = json.loads(
        (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    limited_events = set()
    for event_name, groups in data["hooks"].items():
        handler = groups[0]["hooks"][0]
        assert "-X utf8" in handler["command"]
        assert "-X utf8" in handler["commandWindows"]
        if "additionalContextLimit" in handler:
            limited_events.add(event_name)
    assert limited_events == {
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
    }


def test_managed_hook_events_use_the_explicit_runtime_interpreter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A snapshot MCP and every Codex lifecycle handler share one interpreter."""
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    runtime_python = tmp_path / "snapshot" / "venv" / "Scripts" / "python.exe"
    home.mkdir()
    workspace.mkdir()
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("snapshot", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")

    HostHookManager(workspace).install(
        "codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        runtime_python=runtime_python,
    )

    data = json.loads(
        (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    commands = [
        handler[key]
        for groups in data["hooks"].values()
        for group in groups
        for handler in group.get("hooks", [])
        for key in ("command", "commandWindows")
    ]
    assert len(commands) == 14
    assert all(str(runtime_python) in command for command in commands)
    assert all("python -X utf8" not in command for command in commands)


def test_hook_cli_forces_utf8_stdio_when_windows_defaults_to_gbk(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "codex-agent", "group-a")
    preference = "\u7528\u6237\u957f\u671f\u504f\u597d\uff1a\u56de\u7b54\u4fdd\u6301\u7b80\u6d01"
    prompt = "\u8bf7\u8bfb\u53d6\u4e2d\u6587\u957f\u671f\u504f\u597d"
    _seed_v2_atom(workspace, memory_id="pref", body=preference)
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    env["PYTHONIOENCODING"] = "gbk"
    env["MEMORYGUARD_AGENT_ID"] = "codex-agent"
    env["MEMORYGUARD_STRICT_BINDING"] = "1"

    def run_cli(
        provider: str,
        agent: str,
        group: str,
        payload: dict[str, object],
        *,
        fixture_v2: bool = False,
    ):
        child_env = env.copy()
        child_env["MEMORYGUARD_AGENT_ID"] = agent
        if fixture_v2:
            child_env["MEMORYGUARD_TEST_PREFERENCE"] = preference
            launcher = (
                "import os,sys; import memoryguard.host_hooks as hooks; "
                "facade=type('V2FixtureFacade',(),{"
                "'state_snapshot':lambda self:{'state':'V2_ACTIVE','generation':1},"
                "'bootstrap_hook':lambda self,event,payload,**kwargs:{'packet':{"
                "'relevant':[{'item_id':'pref','body':os.environ['MEMORYGUARD_TEST_PREFERENCE'],"
                "'kind':'preference'}]}}})(); "
                "hooks._v2_runtime_facade_factory=lambda _workspace:facade; "
                "raise SystemExit(hooks.main(sys.argv[1:]))"
            )
            command = [sys.executable, "-c", launcher]
        else:
            command = [sys.executable, "-m", "memoryguard.host_hooks"]
        command.extend([
            "run",
            "--provider",
            provider,
            "--event",
            "user_prompt",
            "--workspace",
            str(workspace),
            "--agent-id",
            agent,
            "--share-group-id",
            group,
        ])
        return subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            check=False,
        )

    completed = run_cli(
        "codex",
        "codex-agent",
        "group-a",
        {
            "session_id": "session-utf8",
            "turn_id": "turn-utf8",
            "prompt": prompt,
            "cwd": str(workspace),
        },
        fixture_v2=True,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )
    stdout = completed.stdout.decode("utf-8", errors="strict")
    assert "\ufffd" not in stdout
    result = json.loads(stdout)
    hook_output = result["hookSpecificOutput"]
    context = hook_output["additionalContext"]
    assert hook_output["hookEventName"] == "UserPromptSubmit"
    assert isinstance(context, str)
    assert "回答保持简洁" in context
    assert prompt not in context

    _bind(workspace, "cursor-agent", "group-cursor")
    cursor = run_cli(
        "cursor",
        "cursor-agent",
        "group-cursor",
        {
            "conversation_id": "conversation-utf8",
            "generation_id": "generation-utf8",
            "prompt": "中文继续",
            "cwd": str(workspace),
        },
    )
    assert cursor.returncode == 0, cursor.stderr.decode("utf-8", errors="replace")
    cursor_stdout = cursor.stdout.decode("utf-8", errors="strict")
    assert "\ufffd" not in cursor_stdout
    assert json.loads(cursor_stdout)["continue"] is True


def test_runtime_receipt_is_bound_to_exact_hook_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")
    manager = HostHookManager(workspace)
    manager.install(
        "codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
    )

    run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    verified = manager.status("codex", agent_instance_id="codex-agent")
    assert verified["status"] == "operational"
    assert verified["runtime_verified"] is True
    inferred = manager.status("codex")
    assert inferred["status"] == "operational"
    assert inferred["agent_instance_id"] == "codex-agent"

    config_path = home / ".codex" / "hooks.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    handler = data["hooks"]["PreToolUse"][0]["hooks"][0]
    handler["commandWindows"] += " --changed"
    config_path.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )

    stale = manager.status("codex", agent_instance_id="codex-agent")
    assert stale["configured"] is True
    assert stale["status"] == "configured_pending_runtime"
    assert stale["runtime_verified"] is False


def test_trae_reports_verified_fallback_instead_of_writing_fake_hook(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    result = HostHookManager(workspace).install(
        "trae",
        agent_instance_id="trae-agent",
        share_group_id="group-a",
    )
    assert result["status"] == "unsupported"
    assert result["configured"] is False
    assert result["capability"]["context_mode"] == "mcp_and_rules_only"


def test_user_prompt_injects_bounded_context_and_receipt_has_no_raw_prompt(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "codex-agent", "group-a")
    _seed_v2_atom(
        workspace,
        memory_id="pref",
        body="用户长期偏好：回答保持简洁",
        kind="preference",
    )
    _seed_v2_atom(
        workspace,
        memory_id="project",
        body="MemoryGuard 项目默认使用 RTK 运行测试",
        kind="project",
    )
    prompt = "检查 MemoryGuard 项目的 RTK 测试规则"

    result = run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "turn_id": "turn-1",
            "prompt": prompt,
            "cwd": str(workspace),
        },
    )

    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert prompt not in json.dumps(result, ensure_ascii=False)
    state_files = list(
        (workspace / ".memoryguard" / "hook-runtime" / "state").rglob("*.json")
    )
    assert state_files
    assert prompt not in state_files[0].read_text(encoding="utf-8")


def test_codex_defers_precompact_reminder_to_compact_session_start(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "codex-agent", "group-a")
    _seed_v2_atom(
        workspace,
        memory_id="pref",
        body="用户长期偏好：回答保持简洁",
        kind="preference",
    )
    session_id = "session-compact"

    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "turn_id": "turn-1",
            "prompt": "以后默认使用结构化输出",
            "cwd": str(workspace),
        },
    )
    before = run_hook(
        provider="codex",
        event="pre_compact",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "trigger": "auto"},
    )
    resumed = run_hook(
        provider="codex",
        event="session_start",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "source": "compact"},
    )

    assert before == {}
    assert resumed["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_pre_tool_blocks_native_memory_write_but_allows_project_file(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    base = {
        "session_id": "session-1",
        "tool_name": "Write",
    }

    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            **base,
            "tool_input": {
                "file_path": str(
                    tmp_path / "home" / ".codex" / "memories" / "MEMORY.md"
                ),
            },
        },
    )
    allowed = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            **base,
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )

    assert (
        denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert allowed == {}


@pytest.mark.parametrize("tool_name", ["Bash", "Write"])
def test_bootstrap_text_in_non_mcp_input_does_not_bypass_native_memory_guard(
    tmp_path: Path,
    tool_name: str,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    native_memory = tmp_path / "home" / ".codex" / "memories" / "MEMORY.md"
    tool_input = (
        {
            "command": (
                "printf memoryguard_context_bootstrap "
                f">> {native_memory}"
            ),
        }
        if tool_name == "Bash"
        else {
            "file_path": str(native_memory),
            "content": "memoryguard_context_bootstrap",
        }
    )

    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": f"bootstrap-text-{tool_name}",
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_cursor_bootstrap_requires_verified_mcp_envelope(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    session_id = "cursor-verified-bootstrap"

    assert host_hooks._is_memoryguard_bootstrap(
        "mcp__memoryguard__memoryguard_context_bootstrap"
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "MCP:memoryguard_context_bootstrap"
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "CallMcpTool",
        {
            "serverName": "memoryguard",
            "toolName": "memoryguard_context_bootstrap",
            "arguments": {"task": "current request"},
        },
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "CallDynamicTool",
        {
            "server_name": "memoryguard",
            "tool_name": "memoryguard_context_bootstrap",
        },
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "CallDynamicTool",
        {
            "namespace": "user-memoryguard",
            "toolName": "memoryguard_context_bootstrap",
            "arguments": {"task": "current request"},
        },
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "CallDynamicTool",
        {
            "namespace": "user-memoryguard",
            "serverName": "memoryguard",
            "toolName": "memoryguard_context_bootstrap",
            "name": "CallDynamicTool",
            "arguments": {"task": "current request"},
        },
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "CallDynamicTool",
        json.dumps({
            "namespace": "user-memoryguard",
            "toolName": "memoryguard_context_bootstrap",
            "arguments": {"task": "current request"},
        }),
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "MCP:user-memoryguard:memoryguard_context_bootstrap"
    )
    assert host_hooks._is_memoryguard_bootstrap(
        "MCP:user-memoryguard/memoryguard_context_bootstrap"
    )
    assert not host_hooks._is_memoryguard_bootstrap(
        "CallDynamicTool",
        {
            "namespace": "user-OpenSea",
            "toolName": "get_collection_stats",
            "arguments": {"slug": "pudgypenguins"},
        },
    )
    assert not host_hooks._is_memoryguard_bootstrap(
        "CallDynamicTool",
        {
            "namespace": "user-OpenSea",
            "toolName": "memoryguard_context_bootstrap",
        },
    )
    for tool_name, tool_input in (
        (
            "Bash",
            {
                "serverName": "memoryguard",
                "toolName": "memoryguard_context_bootstrap",
            },
        ),
        (
            "CallMcpTool",
            {"toolName": "memoryguard_context_bootstrap"},
        ),
        (
            "CallMcpTool",
            {
                "serverName": "other-server",
                "toolName": "memoryguard_context_bootstrap",
            },
        ),
        (
            "CallMcpTool",
            {
                "serverName": "memoryguard",
                "toolName": "memoryguard_context_bootstrap_extra",
            },
        ),
        (
            "mcp__memoryguard__memoryguard_context_bootstrap_extra",
            {},
        ),
    ):
        assert not host_hooks._is_memoryguard_bootstrap(tool_name, tool_input)

    result = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool": {
                "name": "CallMcpTool",
                "arguments": {
                    "serverName": "memoryguard",
                    "toolName": "memoryguard_context_bootstrap",
                    "arguments": {"task": "current request"},
                },
            },
        },
    )

    assert result == {}
    state = json.loads(
        _state_path(workspace, "cursor", session_id).read_text(encoding="utf-8")
    )
    assert state["bootstrap_pending"] is True
    assert state.get("bootstrap_ok") is not True
    assert run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool": {
                "name": "CallMcpTool",
                "arguments": {
                    "serverName": "memoryguard",
                    "toolName": "memoryguard_context_bootstrap",
                    "arguments": {"task": "current request"},
                },
            },
            "tool_result": {"ok": True},
        },
    ) == {}
    state = json.loads(
        _state_path(workspace, "cursor", session_id).read_text(encoding="utf-8")
    )
    assert state["bootstrap_ok"] is True


def test_cursor_calldynamictool_namespace_bootstrap_passes_first_tool_gate(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    session_id = "cursor-dynamictool-bootstrap"
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": session_id, "prompt": "查 floor"},
    ) == {"continue": True}

    denied = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "CallDynamicTool",
            "tool_input": {
                "namespace": "user-OpenSea",
                "toolName": "get_collection_stats",
                "arguments": {"slug": "pudgypenguins"},
            },
        },
    )
    assert denied["permission"] == "deny"

    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "tool_name": "CallDynamicTool",
            "tool_input": {
                "namespace": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
                "arguments": {"task": "查 floor"},
            },
        },
    )
    assert allowed == {}


def test_cursor_calldynamictool_string_and_alias_bootstrap_passes_first_tool_gate(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    session_id = "cursor-dynamictool-alias-bootstrap"
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": session_id, "prompt": "修钩子"},
    ) == {"continue": True}

    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": session_id,
            "name": "CallDynamicTool",
            "input": json.dumps({
                "namespace": "user-memoryguard",
                "toolName": "memoryguard_context_bootstrap",
                "name": "CallDynamicTool",
                "arguments": {"task": "修钩子"},
            }),
        },
    )
    assert allowed == {}


def test_cursor_requires_bootstrap_before_first_tool_and_stop_continues_once(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    prompt_payload = {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "prompt": "以后默认使用 RTK",
    }
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload=prompt_payload,
    ) == {"continue": True}

    first_tool = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert first_tool["permission"] == "deny"
    assert "bootstrap" in json.dumps(first_tool, ensure_ascii=False).casefold()

    run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "MCP:memoryguard_context_bootstrap",
            "tool_result": {"ok": True},
        },
    )
    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert allowed == {}

    first_stop = run_hook(
        provider="cursor",
        event="stop",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": "conversation-1", "loop_count": 0},
    )
    second_stop = run_hook(
        provider="cursor",
        event="stop",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": "conversation-1", "loop_count": 1},
    )
    assert first_stop == {}
    assert second_stop == {}

    subagent_block = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "subagent_id": "subagent-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert subagent_block == {}


def test_global_provider_install_includes_hook_without_duplicate_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(workspace))
    _bind(workspace, "codex-agent", "group-a")

    adapter = CodexAdapter(workspace)
    first = adapter.install(
        workspace,
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        global_scope=True,
    )
    second = adapter.install(
        workspace,
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        global_scope=True,
    )

    hooks = json.loads(
        (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    assert first["hook_configured"] is True
    assert second["hook_configured"] is True
    owned_handlers = [
        handler
        for groups in hooks.values()
        for group in groups
        for handler in group.get("hooks", [])
        if "memoryguard.host_hooks" in handler.get("command", "")
    ]
    assert len(owned_handlers) == 7


def test_paused_mode_is_emergency_bypass_for_tool_guard(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    set_hook_mode(workspace, "codex", "codex-agent", "paused")

    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(
                    tmp_path / ".codex" / "memories" / "MEMORY.md"
                ),
            },
        },
    )
    assert result == {}


def test_paused_mode_bypasses_v2_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    set_hook_mode(workspace, "codex", "codex-agent", "paused")

    def fail_if_v2_cutover_runs(_workspace):
        raise AssertionError("paused mode must bypass V2 cutover")

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        fail_if_v2_cutover_runs,
    )
    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "paused-session",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert result == {}


def test_successful_bootstrap_clears_prior_mandatory_budget_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    session_id = "recover-mandatory-budget"

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            ok=False,
            error="mandatory_budget_exceeded",
        ),
    )
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "first attempt"},
    )
    state_path = _state_path(workspace, "codex", session_id)
    failed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert failed_state["mandatory_overflow"] is True
    assert failed_state["bootstrap_error"] == "mandatory_budget_exceeded"

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(),
    )
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "retry after repair"},
    )
    recovered_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered_state["mandatory_overflow"] is False
    assert not recovered_state.get("bootstrap_error")

    allowed = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert allowed == {}


@pytest.mark.parametrize(
    ("failure_kind", "failed_event"),
    [("dispatch", "user_prompt"), ("capability", "pre_tool")],
)
def test_bootstrap_failure_clears_prior_success_state_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    failed_event: str,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    session_id = f"bootstrap-failure-{failure_kind}"

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(),
    )
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "initial bootstrap"},
    )
    state_path = _state_path(workspace, "codex", session_id)
    successful_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert successful_state["bootstrap_ok"] is True
    assert successful_state["context_hash"]

    if failure_kind == "dispatch":
        class _DispatchFailureFacade(_TestV2Facade):
            def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
                raise RuntimeError("fixture dispatch failure")

        failure_facade = _DispatchFailureFacade()
    else:
        class _CapabilityMissingFacade:
            def state_snapshot(self):
                return {"state": "V2_ACTIVE", "generation": 1}

        failure_facade = _CapabilityMissingFacade()
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: failure_facade,
    )
    failed = run_hook(
        provider="codex",
        event=failed_event,
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "prompt": "failed bootstrap",
            "tool_name": "Read",
            "tool_input": {},
        },
    )
    failed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert failed_state["bootstrap_ok"] is False
    assert failed_state["bootstrap_error"]
    assert len(failed_state["bootstrap_error"]) <= 500
    assert failed_state["context_hash"] == ""
    if failed_event == "pre_tool":
        assert failed["hookSpecificOutput"]["permissionDecision"] == "deny"

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(),
    )
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "recovered bootstrap"},
    )
    recovered_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert recovered_state["bootstrap_ok"] is True
    assert recovered_state["bootstrap_error"] == ""
    assert recovered_state["context_hash"]


def test_pre_tool_transient_bootstrap_failure_recovers_in_same_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    calls = 0

    class _TransientFacade(_TestV2Facade):
        def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "ok": False,
                    "packet": {
                        "status": "blocked",
                        "error": "context_build_failed",
                        "mandatory": [],
                        "relevant": [],
                        "receipts": [],
                    },
                }
            return {
                "ok": True,
                "packet": {
                    "status": "ok",
                    "mandatory": [],
                    "relevant": [],
                    "receipts": [],
                },
            }

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TransientFacade(),
    )
    session_id = "same-turn-recovery"
    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )

    assert result == {}
    assert calls == 2
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["bootstrap_ok"] is True
    assert state["bootstrap_error"] == ""
    assert state["context_hash"]


def test_pre_tool_retry_failure_remains_blocked_and_is_not_reentered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    calls = 0

    class _AlwaysTransientFailure(_TestV2Facade):
        def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
            nonlocal calls
            calls += 1
            return {
                "ok": False,
                "packet": {
                    "status": "blocked",
                    "error": "context_build_failed",
                    "mandatory": [],
                    "relevant": [],
                    "receipts": [],
                },
            }

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _AlwaysTransientFailure(),
    )
    payload = {
        "session_id": "same-turn-retry-failed",
        "tool_name": "Read",
        "tool_input": {"file_path": str(workspace / "README.md")},
    }
    first = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id="codex-agent", share_group_id="group-a", payload=payload,
    )
    second = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id="codex-agent", share_group_id="group-a", payload=payload,
    )

    assert first["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert second["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "memoryguard_context_bootstrap" in second["hookSpecificOutput"][
        "permissionDecisionReason"
    ]
    assert calls == 2
    state = json.loads(
        _state_path(workspace, "codex", payload["session_id"]).read_text(encoding="utf-8")
    )
    assert state["bootstrap_ok"] is False
    assert state["bootstrap_error"] == "context_build_failed"
    assert state["context_hash"] == ""
    assert state["bootstrap_retry_claimed"] is True


def test_pre_tool_mandatory_failure_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    calls = 0

    class _MandatoryFailure(_TestV2Facade):
        def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
            nonlocal calls
            calls += 1
            return {
                "ok": False,
                "packet": {
                    "status": "blocked",
                    "error": "mandatory_budget_exceeded",
                    "mandatory": [],
                    "relevant": [],
                    "receipts": [],
                },
            }

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _MandatoryFailure(),
    )
    result = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id="codex-agent", share_group_id="group-a",
        payload={
            "session_id": "mandatory-no-retry",
            "tool_name": "Read",
            "tool_input": {},
        },
    )

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert calls == 1
    state = json.loads(
        _state_path(workspace, "codex", "mandatory-no-retry").read_text(encoding="utf-8")
    )
    assert state["mandatory_overflow"] is True
    assert state.get("bootstrap_retry_claimed") is not True


def test_pre_tool_recovery_claim_serializes_concurrent_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    session_id = "concurrent-recovery"
    host_hooks._save_state(
        workspace,
        "codex",
        session_id,
        {
            "bootstrap_ok": False,
            "bootstrap_error": "context_build_failed",
            "context_hash": "",
            "mandatory_overflow": False,
        },
    )
    calls = 0
    calls_lock = threading.Lock()

    class _SuccessfulFacade(_TestV2Facade):
        def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.05)
            return {"ok": True, "packet": {"status": "ok", "items": []}}

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _SuccessfulFacade(),
    )
    payload = {
        "session_id": session_id,
        "tool_name": "Read",
        "tool_input": {},
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: run_hook(
                provider="codex", event="pre_tool", workspace=workspace,
                agent_instance_id="codex-agent", share_group_id="group-a",
                payload=payload,
            ),
            range(8),
        ))

    assert calls == 1
    assert any(result == {} for result in results)
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["bootstrap_ok"] is True
    assert state["context_hash"]


def test_pre_tool_reclaims_legacy_retry_claim_without_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Pre-patch state must not turn a transient error into permanent debt."""
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    session_id = "legacy-retry-claim"
    host_hooks._save_state(
        workspace,
        "codex",
        session_id,
        {
            "bootstrap_ok": False,
            "bootstrap_error": "context_build_failed",
            "context_hash": "",
            "mandatory_overflow": False,
            "bootstrap_retry_claimed": True,
            # Deliberately omit bootstrap_retry_claimed_at: old state shape.
        },
    )
    calls = 0

    class _RecoveryFacade(_TestV2Facade):
        def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
            nonlocal calls
            calls += 1
            return {"ok": True, "packet": {"status": "ok", "items": []}}

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _RecoveryFacade(),
    )
    started = time.monotonic()
    result = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id="codex-agent", share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "Read",
            "tool_input": {},
        },
    )
    elapsed = time.monotonic() - started

    assert result == {}
    assert calls == 1
    assert elapsed < 5.0
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["bootstrap_ok"] is True
    assert state["context_hash"]


def test_pre_tool_real_bootstrap_request_bypasses_legacy_failure_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A real MemoryGuard bootstrap request remains reachable for repair."""
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    session_id = "legacy-bootstrap-request"
    host_hooks._save_state(
        workspace,
        "codex",
        session_id,
        {
            "bootstrap_ok": False,
            "bootstrap_error": "context_build_failed",
            "context_hash": "",
            "mandatory_overflow": False,
            "bootstrap_retry_claimed": True,
        },
    )

    def must_not_dispatch(_workspace):
        raise AssertionError("real bootstrap request must remain reachable")

    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", must_not_dispatch)
    result = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id="codex-agent", share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "mcp__memoryguard__memoryguard_context_bootstrap",
            "tool_input": {"task": "当前用户请求"},
        },
    )

    assert result == {}
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["bootstrap_ok"] is False
    assert state["bootstrap_error"] == "context_build_failed"


@pytest.mark.parametrize(
    ("provider", "tool_name", "tool_input"),
    [
        (
            "codex",
            "mcp__memoryguard__memoryguard_memory_update",
            {
                "memory_id": "duplicate-rule",
                "preview": True,
                "expected_revision": 1,
                "idempotency_key": "preview-budget",
                "reason": "check budget",
            },
        ),
        (
            "cursor",
            "CallMcpTool",
            {
                "serverName": "memoryguard",
                "toolName": "memoryguard_memory_read",
                "arguments": {"memory_id": "duplicate-rule"},
            },
        ),
    ],
)
def test_recovery_tools_bypass_broken_context_pretool_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    tool_name: str,
    tool_input: dict,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    host_hooks._save_state(workspace, provider, "repair-session", {
        "bootstrap_ok": False,
        "bootstrap_error": "mandatory_budget_exceeded",
        "mandatory_overflow": True,
        "mandatory_invalid_reason": "mandatory_budget_exceeded",
        "context_hash": "",
    })

    def fail_if_bootstrap_runs(_workspace):
        raise AssertionError("recovery PreToolUse must not enter broken V2 bootstrap")

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        fail_if_bootstrap_runs,
    )
    result = run_hook(
        provider=provider,
        event="pre_tool",
        workspace=workspace,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
        payload={
            "session_id": "repair-session",
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
    )
    assert result == {}
    state = host_hooks._load_state(workspace, provider, "repair-session")
    assert state["mandatory_overflow"] is True
    assert state["bootstrap_ok"] is False


def test_overflow_blocks_committing_update_and_names_recovery_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    host_hooks._save_state(workspace, "codex", "repair-session", {
        "bootstrap_ok": False,
        "bootstrap_error": "mandatory_budget_exceeded",
        "mandatory_overflow": True,
        "mandatory_invalid_reason": "mandatory_budget_exceeded",
        "context_hash": "",
    })
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: (_ for _ in ()).throw(AssertionError("ordinary tools must not enter V2")),
    )
    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "repair-session",
            "tool_name": "mcp__memoryguard__memoryguard_memory_update",
            "tool_input": {"memory_id": "duplicate-rule", "body": "commit"},
        },
    )
    reason = denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "普通任务工具已阻断" in reason
    assert "memoryguard_context_bootstrap" in reason
    assert "preview=true" in reason
    assert "停止继续执行" not in reason
    state = host_hooks._load_state(workspace, "codex", "repair-session")
    assert state["mandatory_overflow"] is True
    assert state["bootstrap_ok"] is False


def test_stop_fails_open_when_v2_reports_mandatory_budget_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            ok=False,
            error="mandatory_budget_exceeded",
        ),
    )
    monkeypatch.setattr(
        host_hooks,
        "_best_effort_codex_reconcile",
        lambda **_kwargs: {"ok": True},
    )

    result = run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "mandatory-budget-stop"},
    )

    assert result == {}
    state = json.loads(
        _state_path(workspace, "codex", "mandatory-budget-stop").read_text(
            encoding="utf-8"
        )
    )
    assert state["mandatory_overflow"] is True
    assert state["bootstrap_error"] == "mandatory_budget_exceeded"


def test_stop_runtime_failure_does_not_create_bootstrap_failure_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)

    class _DispatchFailureFacade(_TestV2Facade):
        def bootstrap_hook(self, event, payload, *, context=None, snapshot=None):
            raise RuntimeError("fixture dispatch failure")

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _DispatchFailureFacade(),
    )
    monkeypatch.setattr(
        host_hooks,
        "_best_effort_codex_reconcile",
        lambda **_kwargs: {"ok": True},
    )

    result = run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "ordinary-stop-failure"},
    )

    assert result == {}
    assert not _state_path(workspace, "codex", "ordinary-stop-failure").exists()


def test_v2_provider_install_routes_bound_identity_without_duplicate_hook_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from memoryguard import provider_adapters

    _v2_bind(tmp_path)
    GroupControlService(tmp_path, write=True).record_provider_identity("codex", "codex-agent", "group-a")
    calls: list[dict[str, object]] = []

    def fake_install(
        self,
        workspace="",
        share_group_id="default",
        agent_instance_id="",
        global_scope=False,
    ):
        del self
        calls.append({
            "workspace": str(workspace),
            "share_group_id": share_group_id,
            "agent_instance_id": agent_instance_id,
            "global_scope": global_scope,
        })
        return {
            "status": "configured",
            "restart_required": True,
            "runtime_verified": False,
            "binding_id": "v2-binding",
            "hook_configured": True,
            "hook_runtime_verified": False,
            "warnings": [],
            "mcp_config_file": "C:/private/config.toml",
        }

    monkeypatch.setattr(provider_adapters.CodexAdapter, "install", fake_install)
    port = _v2_port(tmp_path)
    context = _v2_context(tmp_path, admin=True)
    first = port.dispatch_mcp(
        "memoryguard_provider_install",
        {"provider": "codex"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    second = port.dispatch_mcp(
        "memoryguard_provider_install",
        {"provider": "codex"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )

    assert first["ok"] is True, first
    assert second["ok"] is True, second
    assert first["data"] == second["data"]
    assert first["data"]["hook_configured"] is True
    assert "config.toml" not in json.dumps(first)
    assert calls == [
        {
            "workspace": str(tmp_path.resolve()),
            "share_group_id": "group-a",
            "agent_instance_id": "codex-agent",
            "global_scope": True,
        },
        {
            "workspace": str(tmp_path.resolve()),
            "share_group_id": "group-a",
            "agent_instance_id": "codex-agent",
            "global_scope": True,
        },
    ]


def test_v2_history_preserves_repeated_content_without_text_deduplication(
    tmp_path: Path,
):
    from memoryguard.content.conversation_sync import ConversationEvent

    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    text = "same legitimate prompt"
    _seed_v2_history(workspace, [
        ConversationEvent(
            external_object_key="repeat-session",
            content=text,
            ordinal=0,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
        ConversationEvent(
            external_object_key="repeat-session",
            content=text,
            ordinal=1,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
    ])

    port = _v2_port(workspace)
    context = _v2_context(workspace)
    listed = port.dispatch_mcp(
        "memoryguard_history_list_sessions", {},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert listed["ok"] is True, listed
    sessions = listed["data"]["sessions"]
    assert len(sessions) == 1
    contents = []
    for turn_id in _v2_turn_ids(workspace):
        read = port.dispatch_mcp(
            "memoryguard_history_read",
            {"turn_id": turn_id},
            context=context,
            generation=1,
            state="V2_ACTIVE",
        )
        assert read["ok"] is True, read
        contents.append(read["data"]["turn"]["content"])
    assert contents == [text, text]


@pytest.mark.parametrize("disable_mode", ["env", "config"])
def test_v2_content_history_remains_available_under_retired_capture_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disable_mode: str,
):
    from memoryguard.content.conversation_sync import ConversationEvent

    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    if disable_mode == "env":
        monkeypatch.setenv("MEMORYGUARD_HISTORY_ENABLED", "0")
    else:
        config_path = workspace / ".memoryguard" / "history" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text('{"enabled": false}', encoding="utf-8")
    _seed_v2_history(workspace, [
        ConversationEvent(
            external_object_key="explicit-history-session",
            content="explicit V2 history remains readable",
            ordinal=0,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
    ])

    result = _v2_port(workspace).dispatch_mcp(
        "memoryguard_history_search",
        {"query": "explicit V2 history"},
        context=_v2_context(workspace),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"]["results"]


def test_v2_history_search_hides_secret_until_explicit_read(tmp_path: Path):
    from memoryguard.content.conversation_sync import ConversationEvent

    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    secret = "api_key=super-secret-value"
    _seed_v2_history(workspace, [
        ConversationEvent(
            external_object_key="secret-session",
            content=secret,
            ordinal=0,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
    ])
    port = _v2_port(workspace)
    context = _v2_context(workspace)
    search = port.dispatch_mcp(
        "memoryguard_history_search",
        {"query": "super-secret-value"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert search["ok"] is True, search
    assert secret not in json.dumps(search, ensure_ascii=False)

    sessions = port.dispatch_mcp(
        "memoryguard_history_list_sessions", {},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )["data"]["sessions"]
    read = port.dispatch_mcp(
        "memoryguard_history_read",
        {"turn_id": _first_v2_turn_id(workspace)},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert read["ok"] is True, read
    assert read["data"]["turn"]["content"] == secret


def test_v2_history_scope_uses_bound_provider_not_payload_provider(tmp_path: Path):
    from memoryguard.content.conversation_sync import ConversationEvent

    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    _seed_v2_history(workspace, [
        ConversationEvent(
            external_object_key="payload-provider-session",
            content="trusted provider scope",
            ordinal=0,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
    ])
    port = _v2_port(workspace)
    context = _v2_context(workspace, provider="codex")
    trusted = port.dispatch_mcp(
        "memoryguard_history_list_sessions",
        {},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert trusted["ok"] is True, trusted
    assert trusted["data"]["sessions"][0]["provider"] == "codex"

    result = port.dispatch_mcp(
        "memoryguard_history_list_sessions",
        {
            "provider": "claude",
            "agent_instance_id": "attacker",
            "share_group_id": "attacker-group",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is False, result
    assert result["code"] == "context_identity_spoof"


def test_history_without_stable_event_id_preserves_legitimate_repeats_and_marks_degraded(
    tmp_path: Path,
):
    from memoryguard.content.conversation_sync import ConversationEvent

    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace, "codex-agent", "group-a")
    text = "same legitimate prompt"
    _seed_v2_history(workspace, [
        ConversationEvent(
            external_object_key="repeat-session",
            content=text,
            ordinal=0,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
        ConversationEvent(
            external_object_key="repeat-session",
            content=text,
            ordinal=1,
            provider="codex",
            workspace_id=str(workspace.resolve()),
            agent_instance_id="codex-agent",
            project_ref=str(workspace.resolve()).casefold(),
            share_group_id="group-a",
        ),
    ])
    port = _v2_port(workspace)
    context = _v2_context(workspace)
    turn_ids = _v2_turn_ids(workspace)
    assert len(turn_ids) == 2 and turn_ids[0] != turn_ids[1]
    contents = [
        port.dispatch_mcp(
            "memoryguard_history_read",
            {"turn_id": turn_id},
            context=context,
            generation=1,
            state="V2_ACTIVE",
        )["data"]["turn"]["content"]
        for turn_id in turn_ids
    ]
    assert contents == [text, text]


def test_post_tool_memory_write_success_marks_write_seen(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    session_id = "post-tool-success"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "turn_id": "turn-1", "prompt": "测试", "cwd": str(workspace)},
    )

    run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {},
            "tool_result": {"isError": False, "ok": True, "memory_id": "m-1"},
        },
    )

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["write_seen"] is True
    assert state.get("write_failed") is None


def test_post_tool_memory_write_failure_keeps_write_seen_false(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    session_id = "post-tool-fail"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "turn_id": "turn-1", "prompt": "测试", "cwd": str(workspace)},
    )

    run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {},
            "tool_result": {"isError": True, "error": "write failed"},
        },
    )

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["write_seen"] is False
    assert state["write_failed"] is True
    assert state["write_error"]


def test_post_tool_memory_write_without_tool_result_does_not_auto_mark_success(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    session_id = "post-tool-unknown"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "turn_id": "turn-1", "prompt": "测试", "cwd": str(workspace)},
    )

    run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {},
        },
    )

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["write_seen"] is False
    assert state.get("write_failed") is None


def test_v2_explicit_feedback_replaces_hook_stop_inference(tmp_path: Path):
    import sqlite3

    workspace = tmp_path / "control"
    workspace.mkdir()
    store, receipt_id = _seed_v2_rule_receipt(workspace)
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "feedback-session", "prompt": "task"},
    )
    run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "feedback-session"},
    )
    with sqlite3.connect(store.db_path) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()[0]
    assert before == 0

    result = _v2_port(workspace).dispatch_mcp(
        "memoryguard_rule_feedback",
        {
            "receipt_id": receipt_id,
            "outcome": "followed",
            "evidence": "explicit V2 observation",
            "idempotency_key": "explicit-feedback",
        },
        context=_v2_context(workspace, admin=False),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"]["outcome"] == "followed"
    with sqlite3.connect(store.db_path) as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()[0]
    assert after == 1


def test_v2_explicit_feedback_replay_does_not_duplicate_existing_feedback(
    tmp_path: Path,
):
    import sqlite3

    workspace = tmp_path / "control"
    workspace.mkdir()
    store, receipt_id = _seed_v2_rule_receipt(
        workspace,
        session_id="replay-session",
    )
    port = _v2_port(workspace)
    context = _v2_context(workspace, admin=False)
    payload = {
        "receipt_id": receipt_id,
        "outcome": "followed",
        "evidence": "one explicit observation",
        "idempotency_key": "feedback-replay",
    }
    first = port.dispatch_mcp(
        "memoryguard_rule_feedback", payload,
        context=context, generation=1, state="V2_ACTIVE",
    )
    second = port.dispatch_mcp(
        "memoryguard_rule_feedback", payload,
        context=context, generation=1, state="V2_ACTIVE",
    )
    assert first["ok"] is True, first
    assert second["ok"] is True, second
    assert first["data"]["feedback_id"]
    assert second["data"]["idempotent_replay"] is True
    with sqlite3.connect(store.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()[0]
    assert count == 1


def test_v2_feedback_authority_ignores_actor_display_text(tmp_path: Path):
    import sqlite3

    workspace = tmp_path / "control"
    workspace.mkdir()
    store, receipt_id = _seed_v2_rule_receipt(
        workspace,
        session_id="authority-session",
    )
    result = _v2_port(workspace).dispatch_mcp(
        "memoryguard_rule_feedback",
        {
            "receipt_id": receipt_id,
            "outcome": "violated",
            "actor": "user",
            "producer": "user",
            "evidence": "display text cannot elevate MCP authority",
            "idempotency_key": "authority-feedback",
        },
        context=_v2_context(workspace, admin=False),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT authority,metadata_json FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == 3
    assert json.loads(row[1])["producer"] == "agent"


def test_stop_flushes_pending_mandatory_rule_feedback(tmp_path: Path):
    import sqlite3

    workspace = tmp_path / "control"
    workspace.mkdir()
    store, receipt_id = _seed_v2_rule_receipt(workspace, session_id="feedback-session")
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "feedback-session", "prompt": "请检查项目代码。"},
    )
    stop_result = run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "feedback-session", "loop_count": 0},
    )
    assert "memoryguard_memory_write" not in stop_result
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()[0] == 0


def test_stop_skips_feedback_when_explicit_feedback_is_already_present(tmp_path: Path):
    import sqlite3

    workspace = tmp_path / "control"
    workspace.mkdir()
    store, receipt_id = _seed_v2_rule_receipt(
        workspace,
        session_id="feedback-explicit-session",
    )
    port = _v2_port(workspace)
    result = port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {
            "receipt_id": receipt_id,
            "outcome": "followed",
            "evidence": "explicit V2 observation",
            "idempotency_key": "explicit-stop-feedback",
        },
        context=_v2_context(workspace, admin=False),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "feedback-explicit-session", "loop_count": 0},
    )
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT feedback_id,outcome FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    assert row is not None and row[1] == "followed"


def test_internal_hook_feedback_cannot_infer_user_authority_from_actor_text(
    tmp_path: Path,
):
    import sqlite3

    workspace = tmp_path / "control"
    workspace.mkdir()
    store, receipt_id = _seed_v2_rule_receipt(
        workspace,
        session_id="hook-authority-session",
    )
    result = _v2_port(workspace).dispatch_mcp(
        "memoryguard_rule_feedback",
        {
            "receipt_id": receipt_id,
            "outcome": "violated",
            "actor": "user",
            "producer": "user",
            "evidence": "display text cannot elevate native authority",
            "idempotency_key": "hook-authority-feedback",
        },
        context=_v2_context(workspace, admin=False),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    with sqlite3.connect(store.db_path) as conn:
        authority, metadata_json = conn.execute(
            "SELECT authority,metadata_json FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    assert authority == 3
    assert json.loads(metadata_json)["producer"] == "agent"


def test_hook_mandatory_receipt_and_overflow_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            packet={
                "mandatory": [{
                    "item_id": "rule-v2-mandatory",
                    "body": "private mandatory body",
                }],
                "relevant": [],
                "receipts": [{
                    "hit": True,
                    "layer": "mandatory",
                    "item_id": "rule-v2-mandatory",
                    "digest": "rule-digest",
                }],
            },
        ),
    )
    session_id = "mandatory-receipt-session"
    result = run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "raw prompt must not be a receipt"},
    )
    state_path = _state_path(workspace, "codex", session_id)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert state["mandatory_rule_ids"] == ["rule-v2-mandatory"]
    assert len(state["mandatory_match_receipts"]) == 1
    assert "private mandatory body" not in json.dumps(state, ensure_ascii=False)
    assert "raw prompt must not be a receipt" not in json.dumps(state, ensure_ascii=False)

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            ok=False,
            error="mandatory_overflow",
        ),
    )
    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "Write",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    overflow_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert overflow_state["mandatory_overflow"] is True
    heartbeat = json.loads(
        host_hooks._heartbeat_path(workspace, "codex", "codex-agent").read_text(
            encoding="utf-8",
        )
    )
    assert heartbeat["mandatory_overflow"] is True


def test_context_build_failed_is_bootstrap_error_not_mandatory_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            ok=False,
            error="context_build_failed",
            packet={
                "status": "blocked",
                "error": "context_build_failed",
                "mandatory": [],
                "relevant": [],
                "receipts": [],
            },
        ),
    )
    session_id = "context-build-failed"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "generic runtime failure"},
    )
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["mandatory_overflow"] is False
    assert state["bootstrap_ok"] is False
    assert state["bootstrap_error"] == "context_build_failed"
    heartbeat = json.loads(
        host_hooks._heartbeat_path(workspace, "codex", "codex-agent").read_text(
            encoding="utf-8",
        )
    )
    assert heartbeat["mandatory_overflow"] is False
    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "Write",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    recovered = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "mcp__memoryguard__memoryguard_runtime_processes",
            "tool_input": {},
        },
    )
    assert recovered == {}


def test_bootstrap_success_clears_previous_error_and_stamps_context_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    session_id = "atomic-bootstrap-state"
    host_hooks._save_state(
        workspace,
        "codex",
        session_id,
        {
            "bootstrap_ok": False,
            "bootstrap_error": "context_build_failed",
            "context_hash": "stale-context",
        },
    )
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            packet={
                "status": "ok",
                "mandatory": [],
                "relevant": [{"item_id": "relevant-1"}],
                "receipts": [],
            },
        ),
    )
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "recover"},
    )
    recovered = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert recovered["bootstrap_ok"] is True
    assert recovered["bootstrap_error"] == ""
    assert recovered["context_hash"]

    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            ok=False,
            error="context_build_failed",
            packet={
                "status": "blocked",
                "error": "context_build_failed",
                "mandatory": [],
                "relevant": [],
                "receipts": [],
            },
        ),
    )
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "fail again"},
    )
    failed = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert failed["bootstrap_ok"] is False
    assert failed["bootstrap_error"] == "context_build_failed"
    assert failed["context_hash"] == ""


def test_state_updates_are_serialized_without_holding_bootstrap_lock(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    session_id = "state-update-concurrency"
    host_hooks._save_state(
        workspace,
        "codex",
        session_id,
        {"bootstrap_ok": False, "bootstrap_error": "initial"},
    )

    def write_receipt(index: int) -> None:
        if index % 2:
            host_hooks._update_state(
                workspace,
                "codex",
                session_id,
                updates={
                    "bootstrap_ok": True,
                    "bootstrap_error": "",
                    "context_hash": f"context-{index}",
                },
            )
        else:
            host_hooks._update_state(
                workspace,
                "codex",
                session_id,
                updates={
                    "bootstrap_ok": False,
                    "bootstrap_error": f"context_build_failed-{index}",
                    "context_hash": "",
                },
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_receipt, range(16)))

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["bootstrap_ok"] is (state["bootstrap_error"] == "")
    if state["bootstrap_ok"]:
        assert state["context_hash"]
    else:
        assert state["context_hash"] == ""


def test_no_source_absent_packet_is_not_mandatory_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            packet={
                "status": "NO_SOURCE",
                "canonical_state": "absent",
                "error": "",
                "mandatory": [],
                "relevant": [],
                "receipts": [],
                "ready": True,
            },
        ),
    )
    session_id = "no-source-session"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "neutral fallback"},
    )
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["mandatory_overflow"] is False
    assert state["bootstrap_ok"] is True
    assert not state.get("bootstrap_error")


def test_mandatory_sensitive_still_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _v2_bind(workspace)
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(
            ok=False,
            error="mandatory_sensitive_blocked",
        ),
    )
    session_id = "mandatory-sensitive"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "prompt": "sensitive rule"},
    )
    state = json.loads(_state_path(workspace, "codex", session_id).read_text(encoding="utf-8"))
    assert state["mandatory_overflow"] is True
    assert state["bootstrap_ok"] is False
    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "Write",
            "tool_input": {"file_path": str(workspace / "secret.md")},
        },
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    recovered = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "mcp__memoryguard__memoryguard_diagnostics_snapshot",
            "tool_input": {},
        },
    )
    assert recovered == {}


def _retired_v1_reference_history_capture_global_disable_is_visible_in_receipt(tmp_path: Path, monkeypatch, disable_mode: str):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    if disable_mode == "env":
        monkeypatch.setenv("MEMORYGUARD_HISTORY_ENABLED", "0")
        expected = "disabled_by_env"
    else:
        path = workspace / ".memoryguard" / "history" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"enabled": false}', encoding="utf-8")
        expected = "disabled_by_config"
    run_hook(provider="codex", event="user_prompt", workspace=workspace,
             agent_instance_id="codex-agent", share_group_id="group-a", payload={
                 "session_id": "disabled-session", "turn_id": "turn-1", "prompt": "not archived",
             })
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    history = json.loads(receipt.read_text(encoding="utf-8"))["history_archive"]
    assert history == {"attempted": False, "reason": expected, "capture_enabled": False}


def _retired_v1_reference_history_capture_blocks_obvious_secret_without_persisting_raw(tmp_path: Path):
    from memoryguard.runtime_v2.history_store import ContentHistoryStore, V2HistoryScope as HistoryScope

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    secret = "api_key=super-secret-value"
    run_hook(provider="codex", event="user_prompt", workspace=workspace,
             agent_instance_id="codex-agent", share_group_id="group-a", payload={
                 "session_id": "secret-session", "turn_id": "secret-turn", "prompt": secret,
             })
    assert ContentHistoryStore(workspace, readonly=True).list_sessions(
        HistoryScope(agent_instance_id="codex-agent")
    )["sessions"] == []
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    receipt_text = receipt.read_text(encoding="utf-8")
    assert secret not in receipt_text
    history = json.loads(receipt_text)["history_archive"]
    assert history["reason"] == "secret_detected_blocked"
    assert history["secret_blocked"] is True


def test_concurrent_runtime_receipt_writes_remain_valid_json(tmp_path: Path):
    from memoryguard.host_hooks import _write_json_config

    path = tmp_path / ".memoryguard" / "hook-runtime" / "heartbeat" / "agent.json"
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda index: _write_json_config(path, {"index": index}), range(80)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["index"] in range(80)
    assert not list(path.parent.glob("*.tmp"))


def test_thread_lock_registry_releases_unique_paths(tmp_path: Path):
    baseline = set(host_hooks._PATH_THREAD_LOCKS)
    paths = [tmp_path / f"heartbeat-{index}.json" for index in range(8)]
    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        list(pool.map(lambda path: host_hooks._write_json_config(path, {"ok": True}), paths))
    assert set(host_hooks._PATH_THREAD_LOCKS) == baseline
    assert all(entry.users == 0 for entry in host_hooks._PATH_THREAD_LOCKS.values())


def test_cross_process_runtime_lock_still_times_out(tmp_path: Path):
    path = tmp_path / "heartbeat.json"
    lock_path = path.with_name(f".{path.name}.memoryguard.lock")
    child_code = "\n".join(
        [
            "import sys, time",
            "from pathlib import Path",
            "from memoryguard.host_hooks import _cross_process_path_lock",
            "path = Path(sys.argv[1])",
            "with _cross_process_path_lock(path):",
            "    print('locked', flush=True)",
            "    time.sleep(5)",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(TimeoutError, match="hook runtime lock"):
            with host_hooks._cross_process_path_lock(path):
                pass
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)
        lock_path.unlink(missing_ok=True)


def test_atomic_runtime_replace_retries_windows_access_denied(tmp_path: Path, monkeypatch):
    import memoryguard.host_hooks as hooks

    path = tmp_path / "heartbeat.json"
    real_replace = hooks.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("temporarily locked")
            error.winerror = 5
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(hooks.os, "replace", flaky_replace)
    hooks._atomic_write_text(path, '{"ok": true}')
    assert attempts == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_heartbeat_write_failure_never_blocks_pretool_hook(tmp_path: Path, monkeypatch):
    import memoryguard.host_hooks as hooks

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")

    def fail_write(*_args, **_kwargs):
        raise PermissionError("simulated heartbeat lock")

    monkeypatch.setattr(hooks, "_write_json_config", fail_write)
    result = run_hook(provider="codex", event="pre_tool", workspace=workspace,
                      agent_instance_id="codex-agent", share_group_id="group-a", payload={
                          "session_id": "session-1", "tool_name": "Read",
                          "tool_input": {"file_path": str(workspace / "README.md")},
                      })
    assert result == {}


def test_mandatory_state_write_failure_is_fail_closed_and_diagnosed(tmp_path: Path, monkeypatch, capsys):
    import memoryguard.host_hooks as hooks

    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "codex-agent", "group-a")
    real_write = hooks._write_json_config

    def fail_state_only(path, data):
        if "state" in path.parts:
            raise PermissionError("simulated mandatory state lock")
        return real_write(path, data)

    monkeypatch.setattr(hooks, "_write_json_config", fail_state_only)
    with pytest.raises(PermissionError, match="mandatory state lock"):
        run_hook(provider="codex", event="user_prompt", workspace=workspace,
                 agent_instance_id="codex-agent", share_group_id="group-a", payload={
                     "session_id": "session-1", "turn_id": "turn-1",
                     "prompt": "regular prompt", "cwd": str(workspace),
                 })
    diagnostic = capsys.readouterr().err
    assert "mandatory_state_write_failed" in diagnostic
    assert "regular prompt" not in diagnostic


def test_invalid_existing_hook_config_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON hook config"):
        HostHookManager(workspace).install(
            "codex",
            agent_instance_id="codex-agent",
            share_group_id="group-a",
        )
    assert path.read_text(encoding="utf-8") == "{broken"


def test_successful_bootstrap_post_tool_clears_recovery_claim(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    session_id = "post-tool-recovery-claim"
    host_hooks._save_state(
        workspace,
        "codex",
        session_id,
        {
            "bootstrap_ok": False,
            "bootstrap_error": "context_build_failed",
            "context_hash": "",
            "bootstrap_retry_claimed": True,
            "bootstrap_retry_claimed_at": time.time(),
            "mandatory_overflow": False,
        },
    )

    result = run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "mcp__memoryguard__memoryguard_context_bootstrap",
            "tool_input": {"task": "恢复当前任务上下文"},
            "tool_result": {"ok": True},
        },
    )

    assert result == {}
    state = host_hooks._load_state(workspace, "codex", session_id)
    assert state["bootstrap_ok"] is True
    assert state["bootstrap_error"] == ""
    assert state["context_hash"]
    assert state["bootstrap_retry_claimed"] is False
    assert state["bootstrap_retry_claimed_at"] == 0


@pytest.mark.parametrize("result_kind", ["valid", "blocked", "foreign_agent", "bare_success"])
def test_verified_bootstrap_post_tool_recovers_mandatory_overflow(tmp_path, result_kind):
    workspace = tmp_path / "control"
    workspace.mkdir()
    session_id = "mandatory-post-tool-recovery"
    host_hooks._save_state(workspace, "codex", session_id, {
        "bootstrap_ok": False, "bootstrap_error": "mandatory_budget_exceeded",
        "mandatory_overflow": True, "mandatory_invalid_reason": "mandatory_budget_exceeded",
        "context_hash": "", "mandatory_rule_ids": ["stale-rule"],
    })
    packet = {"status": "ok", "error": "", "state": "V2_ACTIVE", "ready": True,
              "effective_agent": "codex-agent", "mandatory": [], "relevant": [],
              "receipts": [], "budget": {"mandatory": {"items": 0, "chars": 0, "tokens": 0}}}
    if result_kind == "blocked":
        packet.update(status="blocked", error="mandatory_budget_exceeded")
    elif result_kind == "foreign_agent":
        packet["effective_agent"] = "other-agent"
    envelope = {"ok": True, "name": "memoryguard_context_bootstrap", "data": packet}
    if result_kind == "bare_success":
        envelope = {"ok": True}
    run_hook(provider="codex", event="post_tool", workspace=workspace,
             agent_instance_id="codex-agent", share_group_id="group-a", payload={
                 "session_id": session_id,
                 "tool_name": "mcp__memoryguard__memoryguard_context_bootstrap", "tool_input": {"task": "repair"},
                 "tool_result": {"isError": False, "content": [{"type": "text", "text": json.dumps(envelope)}]},
             })
    state = host_hooks._load_state(workspace, "codex", session_id)
    if result_kind == "valid":
        assert state["bootstrap_ok"] is True
        assert state["mandatory_overflow"] is False
        assert state["mandatory_invalid_reason"] == ""
        assert state["mandatory_rule_ids"] == []
        assert state["context_hash"]
    else:
        assert state["bootstrap_ok"] is False
        assert state["mandatory_overflow"] is True


def test_subagent_start_receives_bounded_governance_context(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "claude-agent", "group-a")
    _seed_v2_atom(
        workspace,
        memory_id="project",
        body="MemoryGuard Hook 适配必须保持配置幂等",
        kind="project",
        agent="claude-agent",
    )

    result = run_hook(
        provider="claude",
        event="subagent_start",
        workspace=workspace,
        agent_instance_id="claude-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "task": "检查 MemoryGuard Hook 配置幂等",
        },
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "MemoryGuard Hook" in context
    assert "配置幂等" not in context


def test_successful_hook_records_final_conversion_without_changing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "claude-agent", "group-a")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        host_hooks,
        "_record_conversion_usage",
        lambda **kwargs: calls.append(kwargs),
    )
    packet = {
        "status": "ok",
        "relevant": [{"item_id": "pref", "body": "保持简洁", "kind": "preference"}],
        "usage": {"baseline_units": 100, "delivered_units": 20},
    }
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(packet=packet),
    )

    result = run_hook(
        provider="claude",
        event="session_start",
        workspace=workspace,
        agent_instance_id="claude-agent",
        share_group_id="group-a",
        payload={"session_id": "session-usage", "task": "读取长期偏好"},
    )

    assert result["hookSpecificOutput"]["additionalContext"]
    assert len(calls) == 1
    assert calls[0]["provider"] == "claude"
    assert calls[0]["event"] == "session_start"
    assert calls[0]["text"] == result["hookSpecificOutput"]["additionalContext"]
    assert calls[0]["packet"] is not packet
    assert calls[0]["packet"]["usage"]["baseline_units"] == 100


def test_conversion_telemetry_failure_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "claude-agent", "group-a")
    packet = {
        "status": "ok",
        "relevant": [{"item_id": "pref", "body": "保持简洁", "kind": "preference"}],
        "usage": {
            "candidate_body_units": 100,
            "delivered_body_units": 20,
        },
    }
    monkeypatch.setattr(
        host_hooks,
        "_v2_runtime_facade_factory",
        lambda _workspace: _TestV2Facade(packet=packet),
    )
    import memoryguard.usage_telemetry as usage_telemetry

    def fail_record(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    diagnostics: list[tuple[object, ...]] = []
    monkeypatch.setattr(usage_telemetry, "record_conversion_event", fail_record)
    monkeypatch.setattr(
        host_hooks,
        "_emit_runtime_write_diagnostic",
        lambda *args, **kwargs: diagnostics.append(args),
    )

    result = run_hook(
        provider="claude",
        event="session_start",
        workspace=workspace,
        agent_instance_id="claude-agent",
        share_group_id="group-a",
        payload={"session_id": "session-usage-failure"},
    )

    assert result["hookSpecificOutput"]["additionalContext"]
    assert diagnostics and diagnostics[0][0] == "usage_telemetry_record_failed"


def test_codex_child_agent_id_is_runtime_identity_not_binding_spoof(
    tmp_path: Path,
):
    """Codex uses ``agent_id`` instead of ``subagent_id`` on child tools."""
    workspace = tmp_path / "control"
    workspace.mkdir()
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "codex-parent", "group-a")
    payload = {
        "session_id": "parent-session",
        "agent_id": "codex-child",
        "cwd": str(workspace),
        "tool_name": "Read",
        "tool_input": {"file_path": str(workspace / "README.md")},
    }

    started = run_hook(
        provider="codex",
        event="subagent_start",
        workspace=workspace,
        agent_instance_id="codex-parent",
        share_group_id="group-a",
        payload=payload,
    )
    assert "hookSpecificOutput" in started
    state = host_hooks._load_state(
        workspace,
        "codex",
        "parent-session:subagent:codex-child",
    )
    assert state["context_identity"]["runtime_role"] == "subagent"

    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-parent",
        share_group_id="group-a",
        payload=payload,
    )
    assert result == {}


def test_cli_ensure_installs_only_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _activate_v2_host_workspace(workspace)
    _bind(workspace, "claude-agent", "group-a")
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "claude-agent")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(workspace))
    monkeypatch.setenv("MEMORYGUARD_CONTROL_SCOPE", "project")
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "claude")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "cli-ensure-session")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "host")

    rc = cli_main([
        "hooks",
        "ensure",
        "--provider",
        "claude",
        "--workspace",
        str(workspace),
        "--agent-id",
        "claude-agent",
        "--share-group-id",
        "group-a",
    ])
    output = json.loads(capsys.readouterr().out)

    assert output["configured"] is True
    assert output["provider"] == "claude"
    assert output["status"] in {"configured_pending_runtime", "operational"}
    assert output["last_error"] in {None, ""}
    if output["status"] == "configured_pending_runtime":
        assert output["restart_required"] is True
        assert rc in {0, 1}
    else:
        assert rc == 0
    assert (home / ".claude" / "settings.json").exists()
    assert not (home / ".codex" / "hooks.json").exists()
    assert not (home / ".cursor" / "hooks.json").exists()


# --- Part B1: payload-derived host provider + conflict diagnostic ----------

def test_derive_host_provider_recognizes_cursor_envelope_and_claude_event():
    from memoryguard.host_hooks import derive_host_provider

    assert derive_host_provider({"event": {"name": "beforeSubmitPrompt"}}) == "cursor"
    assert derive_host_provider({"session": {"session_id": "x"}}) == "cursor"
    assert derive_host_provider({"hook_event_name": "UserPromptSubmit"}) == "claude"
    assert derive_host_provider({}) == ""
    assert derive_host_provider({"event": "user_prompt"}) == ""  # top-level string, not dict
    assert derive_host_provider(None) == ""


def _retired_v1_reference_history_archived_under_payload_provider_and_conflict_recorded(tmp_path: Path):
    from memoryguard.host_hooks import derive_host_provider, run_hook

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    # argv says cursor, but payload has a Claude-shaped top-level marker:
    # the session must be archived under the *proven* provider (claude),
    # and a host_provider_conflict diagnostic must be recorded.
    payload = {
        "session_id": "payload-proven-session",
        "prompt": "根据宿主形状归档",
        "cwd": str(workspace),
        "hook_event_name": "UserPromptSubmit",
    }
    result = run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload=payload,
    )
    assert derive_host_provider(payload) == "claude"

    from memoryguard.runtime_v2.history_store import ContentHistoryStore, V2HistoryScope as HistoryScope
    scope = HistoryScope(agent_instance_id="cursor-agent", project_ref=str(workspace),
                         provider="claude", share_group_id="group-a")
    sessions = ContentHistoryStore(workspace, readonly=True).list_sessions(scope)["sessions"]
    proven = [s for s in sessions if s["external_id"] == "payload-proven-session"]
    assert len(proven) == 1
    assert proven[0]["provider"] == "claude"
    receipts = (workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json")
    history = next(
        json.loads(r.read_text(encoding="utf-8"))["history_archive"]
        for r in receipts
        if "history_archive" in json.loads(r.read_text(encoding="utf-8"))
    )
    assert history.get("host_provider_conflict") is True
    assert history.get("payload_provider") == "claude"
