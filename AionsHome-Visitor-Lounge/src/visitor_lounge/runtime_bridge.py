"""Standalone helper executed by AionsHome's existing Python environment."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys


OUTPUT_PREFIX = "VISITOR_LOUNGE_RUNTIME_JSON:"


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    aionshome_root = Path(sys.argv[1]).resolve()
    lounge_root = Path(sys.argv[2]).resolve()
    model = sys.argv[3]
    sys.path.insert(0, str(aionshome_root / "aion-chat"))
    import ai_providers  # type: ignore[import-not-found]

    script = str(getattr(ai_providers, "_CODEX_SCRIPT", ""))
    builder = getattr(ai_providers, "_build_codex_chat_command", None)
    environment_builder = getattr(
        ai_providers,
        "_build_codex_chat_environment",
        None,
    )
    if not script or not callable(builder) or not callable(environment_builder):
        return 3
    environment = environment_builder()
    command = builder(
        shutil.which("node") or "node",
        script,
        str(lounge_root),
        model,
    )
    print(
        OUTPUT_PREFIX
        + json.dumps(
            {
                "script": script,
                "command": command,
                "environment": environment,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
