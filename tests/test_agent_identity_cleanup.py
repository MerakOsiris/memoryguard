import json
from pathlib import Path

import pytest

from memoryguard.agent_locator import AgentLocator
from memoryguard import provider_adapters as providers
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativePortError, NativeV2RuntimePort


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setattr(providers, "_collect_codex_configured_agent_ids", lambda: [])
    return home


def _json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _config(provider, agent):
    return {"mcpServers": {"memoryguard": {"env": {
        "MEMORYGUARD_PROVIDER": provider, "MEMORYGUARD_AGENT_ID": agent,
    }}}}


def test_discovery_excludes_ordinary_dirs_and_backups(isolated_home, tmp_path):
    for name in (".ssh", ".docker", ".codex-backup", ".cursor", ".unknown", ".unknown-mcp"):
        (isolated_home / name).mkdir()
    _json(isolated_home / ".codex-backup" / "mcp.json", {})
    _json(isolated_home / ".unknown-mcp" / "mcp.json", {})
    rows = AgentLocator(tmp_path / "workspace").discover_candidates(include_unknown=True)
    names = {row.dir_name for row in rows}
    assert ".cursor" in names and ".unknown-mcp" in names
    assert not names & {".ssh", ".docker", ".codex-backup", ".unknown"}
    assert next(row.product for row in rows if row.dir_name == ".unknown-mcp") == "unknown"


def test_copied_provider_id_uses_own_hook_and_projects_existing_group(isolated_home, tmp_path):
    _json(isolated_home / ".claude.json", _config("claude", "cursor-id"))
    _json(isolated_home / ".cursor" / "mcp.json", _config("cursor", "cursor-id"))
    command = (
        "python -m memoryguard.host_hooks run --provider claude --event pre_tool "
        f'--workspace "{tmp_path}" --agent-id claude-id --share-group-id shared-team --managed-by memoryguard'
    )
    _json(isolated_home / ".claude" / "settings.json", {
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]},
    })
    evidence = providers.configured_provider_agent_ids()
    assert evidence["claude-code"] == {"claude-id"}
    assert evidence["cursor"] == {"cursor-id"}
    control = GroupControlService(tmp_path / "control", write=True)
    control.bind_agents(["claude-id", "cursor-id", "unattributed-id"], share_group_id="shared-team")
    rows = control.list_bindings(include_inactive=False)["bindings"]
    by_id = {row["agent_instance_id"]: row for row in rows}
    assert by_id["claude-id"]["program_id"] == "claude-code"
    assert by_id["cursor-id"]["program_id"] == "cursor"
    assert by_id["unattributed-id"]["program_id"] == "unknown"
    assert providers.resolve_provider_binding(control, "claude")["agent_instance_id"] == "claude-id"


def test_unproven_conflicting_claims_stay_unresolved(isolated_home):
    _json(isolated_home / ".claude.json", _config("claude", "copied-id"))
    _json(isolated_home / ".cursor" / "mcp.json", _config("cursor", "copied-id"))
    evidence = providers.configured_provider_agent_ids()
    assert not evidence["claude-code"] and not evidence["cursor"]


def test_provider_install_uses_target_binding_and_rejects_missing_target(isolated_home, tmp_path, monkeypatch):
    _json(isolated_home / ".claude.json", _config("claude", "claude-id"))
    control = GroupControlService(tmp_path / "control", write=True)
    control.bind_agents(["claude-id", "caller-id"], share_group_id="shared-team")
    port = NativeV2RuntimePort(tmp_path / "control")
    monkeypatch.setattr(port, "_trusted_admin", lambda _context: True)
    monkeypatch.setattr(port, "_group_service", lambda **_kwargs: control)
    calls = []
    monkeypatch.setattr(providers.ClaudeAdapter, "install", lambda self, *args, **kwargs: calls.append(kwargs) or {"configured": True})
    port._provider_install({"target_provider": "claude"}, {"agent_instance_id": "caller-id", "share_group_id": "caller-group"})
    assert calls[0]["agent_instance_id"] == "claude-id"
    assert calls[0]["share_group_id"] == "shared-team"
    assert control.provider_identity("claude-code")["canonical_id"] == "claude-id"
    with pytest.raises(NativePortError):
        port._provider_install({"target_provider": "cursor"}, {"agent_instance_id": "caller-id"})
    assert len(calls) == 1
