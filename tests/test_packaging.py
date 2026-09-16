"""Verify that a built wheel contains the runtime package only."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_wheel_contains_runtime_package_without_source_extras(tmp_path: Path) -> None:
    output_dir = tmp_path / "wheelhouse"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output_dir),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    wheels = list(output_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    assert any(name.startswith("jev_mcp/") for name in names)
    assert not any(name.startswith(prefix) for name in names for prefix in ("tests/", "examples/", "scripts/"))
