"""Read-only bridge to AionsHome's existing optimized Codex runtime."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess


_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NO_COLOR",
        "CODEX_HOME",
        "HOME",
        "USERPROFILE",
    }
)

_BRIDGE_ENV_ALLOWLIST = frozenset(
    set(_ENV_ALLOWLIST)
    | {
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROCESSOR_ARCHITECTURE",
    }
)
_BRIDGE_OUTPUT_PREFIX = "VISITOR_LOUNGE_RUNTIME_JSON:"


class SharedCodexRuntimeError(RuntimeError):
    """Raised before spawn when the existing AionsHome runtime is unavailable."""


@dataclass(frozen=True)
class ResolvedCodexRuntime:
    command: tuple[str, ...]
    environment: dict[str, str]


class SharedCodexRuntime:
    """Resolve AionsHome's local Codex package and invocation-only options."""

    def __init__(
        self,
        *,
        aionshome_root: Path | None = None,
        python_executable: Path | None = None,
    ) -> None:
        self._configured_root = aionshome_root
        self._configured_python = python_executable

    def resolve(
        self,
        *,
        lounge_root: Path,
        model: str,
        instructions_file: Path,
        developer_instructions: str,
    ) -> ResolvedCodexRuntime:
        lounge_root = lounge_root.resolve()
        aionshome_root = self._resolve_aionshome_root(lounge_root)
        local_script = (
            aionshome_root
            / "Connor-Codex/node_modules/@openai/codex/bin/codex.js"
        ).resolve()
        if not local_script.is_file():
            raise SharedCodexRuntimeError(
                "AionsHome project-local Codex package is unavailable"
            )
        instructions_file = instructions_file.resolve()
        if not instructions_file.is_file() or not instructions_file.is_relative_to(
            lounge_root
        ):
            raise SharedCodexRuntimeError(
                "Visitor Lounge Codex instructions file is unavailable"
            )

        bridge_data = self._load_runtime_data(
            aionshome_root=aionshome_root,
            lounge_root=lounge_root,
            model=model,
        )
        configured_script = Path(str(bridge_data.get("script", ""))).resolve()
        if configured_script != local_script:
            raise SharedCodexRuntimeError(
                "AionsHome Codex resolver did not select its project-local package"
            )
        raw_environment = bridge_data.get("environment")
        if not isinstance(raw_environment, dict):
            raise SharedCodexRuntimeError("AionsHome Codex environment is invalid")
        environment = {
            str(key): str(value)
            for key, value in raw_environment.items()
            if key in _ENV_ALLOWLIST and value is not None
        }
        codex_home = Path(environment.get("CODEX_HOME", ""))
        if not codex_home.is_dir() or not (codex_home / "auth.json").is_file():
            raise SharedCodexRuntimeError(
                "AionsHome Codex authentication profile is unavailable"
            )

        raw_command = bridge_data.get("command")
        command = self._replace_owner_prompt_overrides(
            raw_command,
            instructions_file=instructions_file,
            developer_instructions=developer_instructions,
        )
        if len(command) < 4 or Path(command[1]).resolve() != local_script:
            raise SharedCodexRuntimeError(
                "AionsHome optimized command did not use project-local Codex"
            )
        if command[-2:] != ("app-server", "--stdio"):
            raise SharedCodexRuntimeError("AionsHome Codex command shape is incompatible")
        return ResolvedCodexRuntime(command=command, environment=environment)

    def _resolve_aionshome_root(self, lounge_root: Path) -> Path:
        if self._configured_root is not None:
            candidate = self._configured_root.resolve()
            if not lounge_root.is_relative_to(candidate):
                raise SharedCodexRuntimeError(
                    "Visitor Lounge is outside the configured AionsHome checkout"
                )
            return candidate
        for candidate in lounge_root.parents:
            if (
                (candidate / "aion-chat/ai_providers.py").is_file()
                and (
                    candidate
                    / "Connor-Codex/node_modules/@openai/codex/bin/codex.js"
                ).is_file()
            ):
                return candidate
        raise SharedCodexRuntimeError("parent AionsHome checkout is unavailable")

    @staticmethod
    def _replace_owner_prompt_overrides(
        raw_command: object,
        *,
        instructions_file: Path,
        developer_instructions: str,
    ) -> tuple[str, ...]:
        if not isinstance(raw_command, (list, tuple)) or not all(
            isinstance(value, str) for value in raw_command
        ):
            raise SharedCodexRuntimeError("AionsHome Codex command is invalid")
        command = list(raw_command)
        replacements = {
            "model_instructions_file=": (
                "model_instructions_file="
                + json.dumps(str(instructions_file), ensure_ascii=False)
            ),
            "developer_instructions=": (
                "developer_instructions="
                + json.dumps(developer_instructions, ensure_ascii=False)
            ),
        }
        replaced: set[str] = set()
        for index in range(len(command) - 1):
            if command[index] != "-c":
                continue
            value = command[index + 1]
            for prefix, replacement in replacements.items():
                if value.startswith(prefix):
                    command[index + 1] = replacement
                    replaced.add(prefix)
                    break
        if replaced != set(replacements):
            raise SharedCodexRuntimeError(
                "AionsHome Codex prompt override contract is incompatible"
            )
        return tuple(command)

    def _load_runtime_data(
        self,
        *,
        aionshome_root: Path,
        lounge_root: Path,
        model: str,
    ) -> dict[str, object]:
        python = (
            self._configured_python.resolve()
            if self._configured_python is not None
            else (aionshome_root / ".venv/Scripts/python.exe").resolve()
        )
        if not python.is_file():
            raise SharedCodexRuntimeError(
                "AionsHome Python runtime is unavailable"
            )
        bridge = Path(__file__).with_name("runtime_bridge.py").resolve()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _BRIDGE_ENV_ALLOWLIST and value
        }
        environment["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                [
                    str(python),
                    str(bridge),
                    str(aionshome_root),
                    str(lounge_root),
                    model,
                ],
                cwd=str(lounge_root),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SharedCodexRuntimeError(
                "AionsHome Codex runtime bridge failed"
            ) from error
        if completed.returncode != 0:
            raise SharedCodexRuntimeError(
                "AionsHome Codex runtime bridge failed"
            )
        payload_line = next(
            (
                line[len(_BRIDGE_OUTPUT_PREFIX) :]
                for line in reversed(completed.stdout.splitlines())
                if line.startswith(_BRIDGE_OUTPUT_PREFIX)
            ),
            None,
        )
        if payload_line is None:
            raise SharedCodexRuntimeError(
                "AionsHome Codex runtime bridge returned no contract"
            )
        try:
            payload = json.loads(payload_line)
        except json.JSONDecodeError as error:
            raise SharedCodexRuntimeError(
                "AionsHome Codex runtime bridge returned invalid data"
            ) from error
        if not isinstance(payload, dict):
            raise SharedCodexRuntimeError(
                "AionsHome Codex runtime bridge returned invalid data"
            )
        return payload
