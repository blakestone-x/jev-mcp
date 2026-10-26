"""Keep private project vocabulary out of the public release surface."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = Path(__file__).resolve()
EXCLUDED_DIRECTORIES = {".git", ".venv", ".pytest_cache", ".ruff_cache", ".pyright", "build", "dist"}
# Public product and author names, matched as whole words.
BLOCKED_WORDS = (
    "Codex",
    "Claude",
    "Blake",
    "Luna",
    "Terra",
    "Sol",
    "Haiku",
    "Sonnet",
    "Opus",
)
# Private names, kept out of this file too: salted sha256 of the lowercased word. A whole word, or a Windows
# drive path's first three characters ("x:\abc"), is hashed and checked against this set.
HASH_SALT = "jev-mcp-public-vocabulary-v1:"
BLOCKED_HASHES = frozenset(
    {
        "382a61ce9afe1216d2e58e12d3cfdec96e7fa1d465539281d24683a85ed4b24d",
        "ff07ae037fd24317e4291939384b3dc6ae9ce6f6f9171923ac6d9d7cf792bc57",
        "1e482efe892bfba8e96e68b250b13c4eab24496d72ee4c7414202c8d6934da60",
        "00faf5f53cd60d9e027717545764ae6ab4f7ca1c00537f224cd08c2ae0b155cc",
        "bd5e40b7085ea40f47431c1ce9feef70744eed50054a8194a63d1d4af1cf96f6",
        "a514f2e562fe3364dba89249e62ff82ab6285691e4500f9140333f53c538d74e",
        "c32ed45641781a36c97934403fa6ae2566c07cc97822302596640ef6981543d6",
        "94f458c849eb7d062215244f1eb6995866fe3a9256884d1f59bf91fe40fb00c8",
        "7a4edaa8356f912aba88d44642adf05720482e5fc69dcd57a40da904a997f8ba",
        "f79a0a77949851d325b9e8c03f6447c81b4b1fbe3b4c724d6881bf88598c3399",
        "1797a1dcf7eab34b0c5ffbf285c6e992120157b1f4a51727d8498b4dfecf70ac",
        "cbdb2caf3ae27e7efbbfddc27534fbf340200eb27d6024886762eff17d4f0fff",
        "df13e03fad5f8c86fa094968378d68c684b503a65d64d646c780ab2cc0faba21",
        "d8ac33fc24f5782ab0f1e859ca77963d8c4f637a3543180ef26d9d4d8de2ff25",
    }
)
WORD_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]+(?![A-Za-z0-9])")
DRIVE_PATH_TOKEN = re.compile(r"[A-Za-z]:\\[A-Za-z0-9]{3}")
ALLOWED_BY_PATH = {
    Path("LICENSE"): {"Blake"},
    Path("pyproject.toml"): {"Blake"},
    Path("README.md"): {"Codex", "Claude"},
    Path("tests/test_registration.py"): {"Codex", "Claude"},
}


def _pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", re.IGNORECASE)


def _digest(token: str) -> str:
    return hashlib.sha256((HASH_SALT + token.lower()).encode("utf-8")).hexdigest()


def _hashed_hits(content: str) -> set[str]:
    tokens = {match.group(0) for match in WORD_TOKEN.finditer(content)}
    tokens |= {match.group(0) for match in DRIVE_PATH_TOKEN.finditer(content)}
    return {digest[:12] for digest in map(_digest, tokens) if digest in BLOCKED_HASHES}


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
    for digest_prefix in sorted(_hashed_hits(content)):
        violations.append(f"{relative}: private word {digest_prefix}")
    return violations


def test_hashed_scan_catches_a_private_word() -> None:
    sample_digest = next(iter(sorted(BLOCKED_HASHES)))
    assert all(len(digest) == 64 for digest in BLOCKED_HASHES)
    assert _hashed_hits("nothing private here") == set()
    assert _digest("Example") == _digest("EXAMPLE")
    assert sample_digest[:12] not in _hashed_hits("Example text")


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
