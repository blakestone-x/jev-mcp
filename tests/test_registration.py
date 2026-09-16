"""Registration helper tests with a fake client CLI."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def test_registration_helpers_print_all_client_formats() -> None:
    shell_text = (SCRIPTS / "register.sh").read_text(encoding="utf-8")
    powershell_text = (SCRIPTS / "register.ps1").read_text(encoding="utf-8")
    assert "bash 4+ required; do not run with sh" in shell_text
    for content in (shell_text, powershell_text):
        assert "claude mcp add --scope user jev" in content
        assert "[mcp_servers.jev]" in content
        assert "mcpServers" in content
        assert "uvx" in content
        assert "@v0.2.1" in content
    assert "umask 077" in shell_text
    assert "chmod 600" in shell_text
    assert "SetAccessRuleProtection" in powershell_text
    assert "WindowsIdentity" in powershell_text


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("pwsh") is None, reason="PowerShell is required")
def test_powershell_apply_ignores_nonzero_get(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude.cmd"
    fake_claude.write_text(
        "@echo off\n"
        "if \"%1 %2\"==\"mcp list\" exit /b 0\n"
        "if \"%1 %2\"==\"mcp get\" exit /b 1\n"
        "if \"%1 %2\"==\"mcp add\" exit /b 0\n"
        "exit /b 0\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["HOME"] = str(tmp_path / "home")
    environment["TYPESAFE_API_KEY"] = "unit-test-key"

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPTS / "register.ps1"), "-Apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = tmp_path / "home" / ".codex" / "config.toml"
    assert config.exists()
    assert config.read_text(encoding="utf-8").count("[mcp_servers.jev]") == 1


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell is required")
def test_shell_apply_ignores_nonzero_get(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"mcp list\" ]; then exit 0; fi\n"
        "if [ \"$1 $2\" = \"mcp get\" ]; then exit 1; fi\n"
        "if [ \"$1 $2\" = \"mcp add\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["HOME"] = str(tmp_path / "home")
    environment["TYPESAFE_API_KEY"] = "unit-test-key"

    result = subprocess.run(
        ["bash", str(SCRIPTS / "register.sh"), "--apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config = tmp_path / "home" / ".codex" / "config.toml"
    assert config.exists()
    assert config.read_text(encoding="utf-8").count("[mcp_servers.jev]") == 1
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("pwsh") is None, reason="PowerShell is required")
def test_powershell_apply_requires_key(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("TYPESAFE_API_KEY", None)
    environment["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPTS / "register.ps1"), "-Apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "TYPESAFE_API_KEY is not set" in result.stderr
    assert not (tmp_path / "home" / ".codex" / "config.toml").exists()


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("pwsh") is None, reason="PowerShell is required")
def test_powershell_apply_rejects_backslash_key(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path / "home")
    environment["TYPESAFE_API_KEY"] = r"unit-test\key"

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPTS / "register.ps1"), "-Apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unsupported characters" in result.stderr
    assert not (tmp_path / "home" / ".codex" / "config.toml").exists()


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("pwsh") is None, reason="PowerShell is required")
def test_powershell_apply_replaces_codex_placeholder(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude.cmd"
    fake_claude.write_text(
        "@echo off\n"
        "if \"%1 %2\"==\"mcp list\" (echo jev & exit /b 0)\n"
        "if \"%1 %2\"==\"mcp get\" (echo TYPESAFE_API_KEY = \"...\" & exit /b 0)\n"
        "if \"%1 %2\"==\"mcp remove\" exit /b 0\n"
        "if \"%1 %2\"==\"mcp add\" exit /b 0\n"
        "exit /b 0\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["HOME"] = str(tmp_path / "home")
    environment["TYPESAFE_API_KEY"] = "unit-test-key"
    config = tmp_path / "home" / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[other]\nenv = { TYPESAFE_API_KEY = "..." }\n'
        '[mcp_servers.jev]\ncommand = "uvx"\nenv = { TYPESAFE_API_KEY = "..." }\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPTS / "register.ps1"), "-Apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config_text = config.read_text(encoding="utf-8")
    assert config_text.count('TYPESAFE_API_KEY = "unit-test-key"') == 1
    assert config_text.count('TYPESAFE_API_KEY = "..."') == 1
    backup = Path(str(config) + ".bak")
    assert backup.read_text(encoding="utf-8").count('TYPESAFE_API_KEY = "..."') == 2


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell is required")
def test_shell_apply_requires_key(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("TYPESAFE_API_KEY", None)
    environment["HOME"] = str(tmp_path / "home")

    result = subprocess.run(
        ["bash", str(SCRIPTS / "register.sh"), "--apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "TYPESAFE_API_KEY is not set" in result.stderr
    assert not (tmp_path / "home" / ".codex" / "config.toml").exists()


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell is required")
def test_shell_apply_rejects_backslash_key(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path / "home")
    environment["TYPESAFE_API_KEY"] = r"unit-test\key"

    result = subprocess.run(
        ["bash", str(SCRIPTS / "register.sh"), "--apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unsupported characters" in result.stderr
    assert not (tmp_path / "home" / ".codex" / "config.toml").exists()


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="POSIX shell is required")
def test_shell_rejects_sh_invocation() -> None:
    result = subprocess.run(
        ["sh", str(SCRIPTS / "register.sh")],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "bash 4+ required; do not run with sh" in result.stderr


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="POSIX shell is required")
def test_shell_apply_replaces_codex_placeholder(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"mcp list\" ]; then printf '%s\\n' jev; exit 0; fi\n"
        "if [ \"$1 $2\" = \"mcp get\" ]; then printf '%s\\n' 'TYPESAFE_API_KEY = \"...\"'; exit 0; fi\n"
        "if [ \"$1 $2\" = \"mcp remove\" ]; then exit 0; fi\n"
        "if [ \"$1 $2\" = \"mcp add\" ]; then exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    environment = dict(os.environ)
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["HOME"] = str(tmp_path / "home")
    environment["TYPESAFE_API_KEY"] = "unit-test-key"
    config = tmp_path / "home" / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[other]\nenv = { TYPESAFE_API_KEY = "..." }\n'
        '[mcp_servers.jev]\ncommand = "uvx"\nenv = { TYPESAFE_API_KEY = "..." }\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPTS / "register.sh"), "--apply"],
        cwd=str(REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    config_text = config.read_text(encoding="utf-8")
    assert config_text.count('TYPESAFE_API_KEY = "unit-test-key"') == 1
    assert config_text.count('TYPESAFE_API_KEY = "..."') == 1
    backup = Path(str(config) + ".bak")
    assert backup.read_text(encoding="utf-8").count('TYPESAFE_API_KEY = "..."') == 2
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
