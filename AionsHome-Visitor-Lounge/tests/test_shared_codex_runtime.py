import json
from pathlib import Path
import sys

import pytest

from visitor_lounge.shared_codex_runtime import (
    SharedCodexRuntime,
    SharedCodexRuntimeError,
)


def _write_fake_aionshome(root: Path) -> Path:
    script = root / "Connor-Codex/node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// fake local codex", "utf-8")
    auth_home = root / "existing-chat-home"
    auth_home.mkdir()
    (auth_home / "auth.json").write_text("{}", "utf-8")
    provider = root / "aion-chat/ai_providers.py"
    provider.parent.mkdir(parents=True)
    provider.write_text(
        "\n".join(
            [
                f"_CODEX_SCRIPT = {str(script)!r}",
                "def _build_codex_chat_command(node, script, workspace, model):",
                "    return [node, script, '-c', 'model_verbosity=\"high\"', '-c', 'model_instructions_file=\"owner.md\"', '-c', 'developer_instructions=\"owner only\"', '-c', 'features.shell_tool=false', '-c', 'features.multi_agent=false', 'app-server', '--stdio']",
                "def _build_codex_chat_environment():",
                f"    return {{'PATH': 'node-path', 'NO_COLOR': '1', 'CODEX_HOME': {str(auth_home)!r}, 'HOME': {str(auth_home.parent)!r}, 'USERPROFILE': {str(auth_home.parent)!r}, 'OWNER_SECRET': 'must-not-leak'}}",
            ]
        ),
        "utf-8",
    )
    return script


def test_shared_runtime_uses_local_codex_and_substitutes_lounge_prompts(
    tmp_path: Path,
) -> None:
    aionshome = tmp_path / "AionsHome"
    local_script = _write_fake_aionshome(aionshome)
    lounge = aionshome / ".worktrees/visitor-lounge/AionsHome-Visitor-Lounge"
    lounge.mkdir(parents=True)
    instructions = lounge / "config/codex_base.md"
    instructions.parent.mkdir()
    instructions.write_text("visitor lounge base", "utf-8")

    resolved = SharedCodexRuntime(
        aionshome_root=aionshome,
        python_executable=Path(sys.executable),
    ).resolve(
        lounge_root=lounge,
        model="gpt-5.6-sol",
        instructions_file=instructions,
        developer_instructions="visitor lounge only",
    )

    assert Path(resolved.command[0]).name.casefold() in {"node", "node.exe"}
    assert resolved.command[1] == str(local_script)
    assert 'model_verbosity="high"' in resolved.command
    assert "features.shell_tool=false" in resolved.command
    assert "features.multi_agent=false" in resolved.command
    overrides = {
        value.split("=", 1)[0]: json.loads(value.split("=", 1)[1])
        for value in resolved.command
        if value.startswith(("model_instructions_file=", "developer_instructions="))
    }
    assert overrides["model_instructions_file"] == str(instructions)
    assert overrides["developer_instructions"] == "visitor lounge only"
    assert all("owner.md" not in value for value in resolved.command)
    assert all("owner only" not in value for value in resolved.command)
    assert resolved.command[-2:] == ("app-server", "--stdio")
    assert resolved.environment["CODEX_HOME"].endswith("existing-chat-home")
    assert "OWNER_SECRET" not in resolved.environment


def test_shared_runtime_fails_closed_without_aionshome_local_codex(
    tmp_path: Path,
) -> None:
    aionshome = tmp_path / "AionsHome"
    _write_fake_aionshome(aionshome).unlink()
    lounge = aionshome / ".worktrees/visitor-lounge/AionsHome-Visitor-Lounge"
    instructions = lounge / "config/codex_base.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("visitor lounge base", "utf-8")

    with pytest.raises(SharedCodexRuntimeError, match="project-local Codex"):
        SharedCodexRuntime(
            aionshome_root=aionshome,
            python_executable=Path(sys.executable),
        ).resolve(
            lounge_root=lounge,
            model="gpt-5.6-sol",
            instructions_file=instructions,
            developer_instructions="visitor lounge only",
        )


def test_shared_runtime_skips_dependencyless_worktree_and_uses_parent_checkout(
    tmp_path: Path,
) -> None:
    aionshome = tmp_path / "AionsHome"
    local_script = _write_fake_aionshome(aionshome)
    worktree = aionshome / ".worktrees/visitor-lounge"
    (worktree / "aion-chat").mkdir(parents=True)
    (worktree / "aion-chat/ai_providers.py").write_text("", "utf-8")
    (worktree / "Connor-Codex").mkdir()
    lounge = worktree / "AionsHome-Visitor-Lounge"
    instructions = lounge / "config/codex_base.md"
    instructions.parent.mkdir(parents=True)
    instructions.write_text("visitor lounge base", "utf-8")

    resolved = SharedCodexRuntime(
        python_executable=Path(sys.executable),
    ).resolve(
        lounge_root=lounge,
        model="gpt-5.6-sol",
        instructions_file=instructions,
        developer_instructions="visitor lounge only",
    )

    assert resolved.command[1] == str(local_script)
