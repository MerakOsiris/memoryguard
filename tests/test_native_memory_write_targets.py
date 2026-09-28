import json
from pathlib import Path

import pytest

from memoryguard.host_hooks import _targets_native_memory


@pytest.mark.parametrize("tool", ["Write", "Edit", "edit_file", "Delete"])
def test_file_target_not_source_text_controls_guard(tmp_path, tool):
    native = tmp_path / ".codex" / "memories" / "MEMORY.md"
    payload = {"path": str(tmp_path / "locator.py"), "content": str(native)}
    assert not _targets_native_memory(tool, payload)
    assert not _targets_native_memory(tool, json.dumps(payload))
    payload["path"] = str(native)
    assert _targets_native_memory(tool, payload)
    payload["path"] = str(Path(".codex") / "memories" / "MEMORY.md")
    assert _targets_native_memory(tool, payload)


def test_patch_checks_all_headers_and_moves(tmp_path):
    native = tmp_path / ".cursor" / "memories" / "entry.md"
    patch = f"*** Begin Patch\n*** Update File: src/locator.py\n@@\n+example = {native!s}\n*** End Patch"
    assert not _targets_native_memory("apply_patch", patch)
    assert _targets_native_memory("apply_patch", patch.replace(
        "*** End Patch", f"*** Add File: {native}\n+data\n*** End Patch"))
    assert _targets_native_memory("apply_patch", patch.replace(
        "@@", f"*** Move to: {native}\n@@"))


def test_shell_and_unstructured_writes_remain_protected(tmp_path):
    native = tmp_path / ".claude" / "projects" / "demo" / "memory" / "MEMORY.md"
    assert _targets_native_memory("Bash", {"command": f"echo data >> {native}"})
    assert _targets_native_memory("Edit", str(native))
