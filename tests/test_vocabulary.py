"""Keep private project vocabulary out of the public release surface."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = Path(__file__).resolve()
EXCLUDED_DIRECTORIES = {".git", ".venv", ".pytest_cache", ".ruff_cache", ".pyright", "build", "dist"}
BLOCKED_WORDS = (
    "ESC",
    "Atlas",
    "cdc",
    "VPS",
    "tailnet",
    "Codex",
    "Claude",
    "beads",
    "CRUCIBLE",
    "edevbox",
    "Tug",
    "ICS",
    "Blake",
    "icsops",
    "C:\\dev",
    "Luna",
    "Terra",
    "Sol",
    "Haiku",
    "Sonnet",
    "Opus",
    "FleetView",
    "Prebrief",
)
ALLOWED_BY_PATH = {
    Path("LICENSE"): {"Blake"},
    Path("pyproject.toml"): {"Blake"},
    Path("README.md"): {"Codex", "Claude"},
    Path("tests/test_registration.py"): {"Codex", "Claude"},
}


def _pattern(word: str) -> re.Pattern[str]:
    if word == "C:\\dev":
        return re.compile(re.escape(word), re.IGNORECASE)
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)


def _files_to_scan() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT)
        if path.resolve() == SCANNER_PATH:
            continue
        if any(part in EXCLUDED_DIRECTORIES or part.endswith(".egg-info") for part in relative.parts):
            continue
        try:
            content = path.read_bytes()
            if b"\x00" in content:
                continue
            content.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        files.append(path)
    return sorted(files)


def _history_blobs_to_scan() -> list[tuple[Path, str]]:
    if os.environ.get("JEV_MCP_SCAN_HISTORY") != "1":
        return []
    git = shutil.which("git")
    if git is None:
        return []
    try:
        inside = subprocess.run(
            [git, "rev-parse", "--is-inside-work-tree"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        return []

    try:
        listing = subprocess.run(
            [git, "rev-list", "--objects", "--all"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    objects: list[tuple[str, Path]] = []
    for line in listing.stdout.splitlines():
        object_id, separator, raw_path = line.partition(" ")
        if separator and raw_path:
            objects.append((object_id, Path(raw_path)))
    if not objects:
        return []

    try:
        cat_file = subprocess.run(
            [git, "cat-file", "--batch"],
            cwd=str(REPO_ROOT),
            input=("\n".join(object_id for object_id, _ in objects) + "\n").encode("ascii"),
            capture_output=True,
            text=False,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    output = cat_file.stdout
    offset = 0
    blobs: list[tuple[Path, str]] = []
    for _, relative in objects:
        header_end = output.find(b"\n", offset)
        if header_end == -1:
            break
        header = output[offset:header_end].decode("ascii", errors="replace").split()
        offset = header_end + 1
        if len(header) != 3:
            break
        object_type = header[1]
        try:
            size = int(header[2])
        except ValueError:
            break
        content_end = offset + size
        if content_end > len(output):
            break
        content = output[offset:content_end]
        offset = content_end
        if offset >= len(output) or output[offset] != 10:
            break
        offset += 1
        if object_type != "blob" or b"\x00" in content:
            continue
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        blobs.append((relative, decoded))
    return blobs


def _allowed_words(relative: Path) -> set[str]:
    allowed = ALLOWED_BY_PATH.get(relative, set())
    if relative.parts and relative.parts[0] == "scripts":
        return {"Codex", "Claude"}
    return allowed


def _violations_for_content(
    relative: Path,
    content: str,
    patterns: dict[str, re.Pattern[str]],
) -> list[str]:
    violations: list[str] = []
    allowed = _allowed_words(relative)
    for word, pattern in patterns.items():
        if word not in allowed and pattern.search(content):
            violations.append(f"{relative}: {word}")
    return violations


def test_public_vocabulary_is_scrubbed() -> None:
    patterns = {word: _pattern(word) for word in BLOCKED_WORDS}
    violations: list[str] = []
    for path in _files_to_scan():
        relative = path.relative_to(REPO_ROOT)
        content = path.read_text(encoding="utf-8")
        violations.extend(_violations_for_content(relative, content, patterns))
    scanner_relative = SCANNER_PATH.relative_to(REPO_ROOT)
    for relative, content in _history_blobs_to_scan():
        if relative == scanner_relative:
            continue
        violations.extend(_violations_for_content(relative, content, patterns))
    assert not violations, "public vocabulary violations: " + ", ".join(violations)
