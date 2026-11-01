"""Bounded copies of tracked content, without Git metadata or credential files."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apiheal.core.errors import PolicyError
from apiheal.core.git import git, head
from apiheal.detection.inventory import EXCLUDED, MAX_FILES, safe_path
from apiheal.telemetry.redaction import redact_text

MAX_SNAPSHOT_BYTES = 20_000_000
MAX_SNAPSHOT_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class TrackedFile:
    path: str
    mode: str


def tracked_files(root: Path) -> list[TrackedFile]:
    raw = git(root, "ls-files", "--stage", "-z").stdout
    result = []
    for entry in raw.split("\0"):
        if not entry:
            continue
        try:
            metadata, name = entry.split("\t", 1)
            mode, _, stage = metadata.split()
        except ValueError as exc:
            raise PolicyError("Cannot interpret tracked repository entries.") from exc
        if stage != "0" or mode not in {"100644", "100755"}:
            raise PolicyError(
                "Verification does not support conflicted entries, links, or submodules."
            )
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or any(ord(character) < 32 for character in name)
        ):
            raise PolicyError("Tracked snapshot contains an unsafe filename.")
        result.append(TrackedFile(name, mode))
    if len(result) > MAX_FILES:
        raise PolicyError("Tracked snapshot exceeds the file-count limit.")
    return sorted(result, key=lambda item: item.path)


def excluded(name: str) -> bool:
    path = PurePosixPath(name)
    return any(part in EXCLUDED or part.startswith(".") for part in path.parts) or (
        path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
        or bool(re.search(r"credential|secret|token", path.name, re.I))
    )


def contents(root: Path, *, execution_only: bool = False) -> dict[str, bytes]:
    files = {}
    size = 0
    for entry in tracked_files(root):
        path = safe_path(root, entry.path)
        if execution_only and excluded(entry.path):
            continue
        if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_FILE_BYTES:
            raise PolicyError("Snapshot includes a missing or oversized tracked file.")
        data = path.read_bytes()
        size += len(data)
        if size > MAX_SNAPSHOT_BYTES:
            raise PolicyError("Tracked snapshot exceeds the total size limit.")
        if execution_only:
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                # P0 supports text-only reviewed fixtures, not arbitrary binary assets.
                raise PolicyError("Execution snapshot requires UTF-8 text files.") from None
            if redact_text(text) != text:
                raise PolicyError("Potential credentials found in the execution snapshot.")
        files[entry.path] = data
    return files


def snapshot_digest(root: Path) -> str:
    digest = hashlib.sha256()
    modes = {entry.path: entry.mode for entry in tracked_files(root)}
    for name, data in contents(root).items():
        digest.update(name.encode("utf-8") + b"\0" + modes[name].encode() + b"\0")
        digest.update(hashlib.sha256(data).digest())
    # New/untracked files are never allowed in the candidate.
    if git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout:
        raise PolicyError("Candidate contains unexpected untracked files.")
    return digest.hexdigest()


def validate_delta(
    baseline: Path, candidate: Path, expected_base: str, changed_paths: set[str]
) -> dict:
    if head(baseline) != expected_base or head(candidate) != expected_base:
        raise PolicyError("Verification snapshots do not use the pinned baseline commit.")
    before_entries = {entry.path: entry.mode for entry in tracked_files(baseline)}
    after_entries = {entry.path: entry.mode for entry in tracked_files(candidate)}
    if before_entries != after_entries:
        raise PolicyError("Candidate added, removed, or changed modes on tracked paths.")
    before, after = contents(baseline), contents(candidate)
    changed = {name for name in before if before[name] != after[name]}
    if changed != changed_paths:
        raise PolicyError("Candidate changes do not match the approved patch paths.")
    try:
        return {
            name: (before[name].decode("utf-8"), after[name].decode("utf-8")) for name in changed
        }
    except UnicodeError as error:
        raise PolicyError("Only UTF-8 patch content can be verified.") from error


def copy_for_execution(root: Path, destination: Path) -> None:
    destination.mkdir(exist_ok=False)
    for name, data in contents(root, execution_only=True).items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
    for directory in [destination, *(p for p in destination.rglob("*") if p.is_dir())]:
        directory.chmod(0o755)
