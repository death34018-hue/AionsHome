"""Operator-facing setup contracts exercised against the real artifacts."""

from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _readme_section(title: str, next_title: str) -> str:
    readme = (PROJECT_ROOT / "README.md").read_text("utf-8")
    return readme.split(title, 1)[1].split(next_title, 1)[0]


def _nonblank_recipe_lines(section: str) -> list[str]:
    recipe = section.split("```powershell", 1)[1].split("```", 1)[0]
    return [line.strip() for line in recipe.splitlines() if line.strip()]


def _run_base_python_resolution(project: Path) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the Windows operator scripts")
    probe = project / "resolve-base-python.ps1"
    probe.write_text(
        "param([Parameter(Mandatory = $true)][string]$ProjectRoot)\n"
        ". (Join-Path $ProjectRoot 'scripts\\runtime-common.ps1')\n"
        "Get-LoungeVenvBasePython -ProjectRoot $ProjectRoot\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(probe),
            "-ProjectRoot",
            str(project),
        ],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture")
    return path


def test_documented_dev_extra_installs_the_test_tooling() -> None:
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))

    extras = metadata["project"]["optional-dependencies"]

    assert extras["dev"] == extras["test"]


@pytest.mark.parametrize(
    ("title", "next_title", "operation"),
    [
        ("### 备份", "### 可恢复重置", "Copy-Item"),
        ("### 可恢复重置", "## 故障排查", "Move-Item"),
    ],
)
def test_database_recipes_stop_successfully_before_touching_the_database(
    title: str, next_title: str, operation: str
) -> None:
    section = _readme_section(title, next_title)
    lines = _nonblank_recipe_lines(section)
    stop_index = next(
        index for index, line in enumerate(lines) if "scripts\\stop.ps1" in line
    )
    guard_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("if ($LASTEXITCODE -ne 0) { throw")
    )
    operation_index = next(
        index for index, line in enumerate(lines) if line.startswith(operation)
    )

    assert guard_index == stop_index + 1
    assert stop_index < guard_index < operation_index
    assert "$ErrorActionPreference = 'Stop'" in section
    assert "Resolve-Path -LiteralPath" in section
    assert f"{operation} -LiteralPath" in section
    assert "不要在服务运行时" in section


def test_reset_recipe_checks_for_the_database_before_resolving_and_skips_missing() -> None:
    section = _readme_section("### 可恢复重置", "## 故障排查")
    lines = _nonblank_recipe_lines(section)
    check_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("if (Test-Path -LiteralPath $databasePath -PathType Leaf)")
    )
    resolve_index = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("$database = (Resolve-Path -LiteralPath $databasePath)")
    )
    move_index = next(
        index for index, line in enumerate(lines) if line.startswith("Move-Item")
    )
    else_index = next(index for index, line in enumerate(lines) if line == "} else {")
    skip_index = next(
        index
        for index, line in enumerate(lines)
        if "Database does not exist; reset move skipped." in line
    )

    assert check_index < resolve_index < move_index < else_index < skip_index


def test_acceptance_commands_use_the_venv_and_parse_every_powershell_script() -> None:
    section = _readme_section("## 本地验收清单", "## 第一阶段明确不包含")

    readme = (PROJECT_ROOT / "README.md").read_text("utf-8")
    assert "仅支持从项目根目录进行 editable 安装和运行；不支持 wheel 部署" in readme
    assert ".\\.venv\\Scripts\\python.exe -m pytest -q" in section
    assert ".\\.venv\\Scripts\\python.exe -m compileall -q src tests" in section
    assert "Get-ChildItem -LiteralPath 'scripts' -Filter '*.ps1'" in section
    assert "[Management.Automation.Language.Parser]::ParseFile" in section
    assert "if ($parseFailed) { throw" in section
    assert "录音/记录告知文本" not in readme


def _obsolete_env_template_codex_home_test(
    tmp_path: Path,
) -> None:
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is required for the Windows operator scripts")

    isolated_root = tmp_path / "AionsHome-Visitor-Lounge"
    shutil.copytree(PROJECT_ROOT / "scripts", isolated_root / "scripts")
    template = (PROJECT_ROOT / ".env.example").read_text("utf-8")
    secrets = {
        "VISITOR_LOUNGE_KEY_PEPPER": "operator-test-pepper",
        "VISITOR_LOUNGE_MASTER_KEY": "operator-test-master-key",
        "VISITOR_LOUNGE_SESSION_SECRET": "operator-test-session-secret",
    }
    for name, value in secrets.items():
        template = template.replace(f"{name}=", f"{name}={value}")
    (isolated_root / ".env").write_text(template, encoding="utf-8")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(isolated_root / "scripts" / "init-codex.ps1"),
            "-PrepareOnly",
        ],
        cwd=isolated_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (isolated_root / ".codex-home").is_dir()
    assert (isolated_root / ".runtime" / "codex-workdir").is_dir()
    combined_output = result.stdout + result.stderr
    assert all(value not in combined_output for value in secrets.values())


def test_nested_venv_chain_resolves_to_the_non_venv_base_python(tmp_path: Path) -> None:
    fixture_root = tmp_path / "operator's fixture"
    project = fixture_root / "AionsHome-Visitor-Lounge"
    shutil.copytree(PROJECT_ROOT / "scripts", project / "scripts")
    _fake_python(project / ".venv" / "Scripts" / "python.exe")
    outer_root = fixture_root / "outer-venv"
    outer_python = _fake_python(outer_root / "Scripts" / "python.exe")
    (project / ".venv" / "pyvenv.cfg").write_text(
        f"executable = {outer_python}\n", encoding="utf-8"
    )
    (outer_root / "pyvenv.cfg").write_text(
        f"executable = {Path(sys._base_executable).resolve()}\n", encoding="utf-8"
    )

    result = _run_base_python_resolution(project)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == Path(sys._base_executable).resolve()


def test_nested_venv_chain_rejects_a_cycle(tmp_path: Path) -> None:
    project = tmp_path / "AionsHome-Visitor-Lounge"
    shutil.copytree(PROJECT_ROOT / "scripts", project / "scripts")
    nested_python = _fake_python(project / ".venv" / "Scripts" / "python.exe")
    outer_root = tmp_path / "outer-venv"
    outer_python = _fake_python(outer_root / "Scripts" / "python.exe")
    (project / ".venv" / "pyvenv.cfg").write_text(
        f"executable = {outer_python}\n", encoding="utf-8"
    )
    (outer_root / "pyvenv.cfg").write_text(
        f"executable = {nested_python}\n", encoding="utf-8"
    )

    result = _run_base_python_resolution(project)

    assert result.returncode != 0
    assert "cycle" in result.stderr.casefold()


def test_venv_chain_rejects_a_missing_executable(tmp_path: Path) -> None:
    project = tmp_path / "AionsHome-Visitor-Lounge"
    shutil.copytree(PROJECT_ROOT / "scripts", project / "scripts")
    _fake_python(project / ".venv" / "Scripts" / "python.exe")
    missing = tmp_path / "missing-venv" / "Scripts" / "python.exe"
    (project / ".venv" / "pyvenv.cfg").write_text(
        f"executable = {missing}\n", encoding="utf-8"
    )

    result = _run_base_python_resolution(project)

    assert result.returncode != 0


def test_venv_chain_rejects_relative_executable_escape(tmp_path: Path) -> None:
    project = tmp_path / "AionsHome-Visitor-Lounge"
    shutil.copytree(PROJECT_ROOT / "scripts", project / "scripts")
    _fake_python(project / ".venv" / "Scripts" / "python.exe")
    _fake_python(tmp_path / "outside" / "python.exe")
    (project / ".venv" / "pyvenv.cfg").write_text(
        "executable = ..\\outside\\python.exe\n", encoding="utf-8"
    )

    result = _run_base_python_resolution(project)

    assert result.returncode != 0
    assert "absolute" in result.stderr.casefold()
