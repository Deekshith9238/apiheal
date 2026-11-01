"""Bounded, symlink-safe repository inventory."""

import hashlib
import os
import stat
from pathlib import Path

from apiheal.core.errors import PolicyError
from apiheal.core.git import git, head, is_dirty, repository_root
from apiheal.core.models import RepositorySnapshot
from apiheal.telemetry.redaction import redact_text

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "__pycache__",
    ".tox",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    "graphify-out",
}
MANIFESTS = {"pyproject.toml", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock"}
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 10_000


def is_link(path: Path) -> bool:
    """Include Windows junctions/reparse points on Python 3.11."""
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return path.is_symlink() or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    except FileNotFoundError:
        return False


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if (
        Path(relative).is_absolute()
        or ":" in relative
        or any(part in {"..", ".git"} for part in Path(relative).parts)
        or not path.resolve().is_relative_to(root.resolve())
        or any(is_link(p) for p in [path, *path.parents] if p.is_relative_to(root))
    ):
        raise PolicyError("Repository path is outside the permitted source tree or is a link.")
    return path


def inventory(repository: Path) -> RepositorySnapshot:
    root = repository_root(repository)
    snapshot = RepositorySnapshot(root=str(root), base_commit=head(root), dirty=is_dirty(root))
    remote = git(root, "config", "--get", "remote.origin.url", check=False)
    if remote.returncode == 0:
        snapshot.remote = redact_text(remote.stdout.strip())
    seen = 0

    def walk_error(error: OSError) -> None:
        snapshot.warnings.append(f"Directory could not be scanned ({type(error).__name__}).")

    for current, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
        accepted = []
        for name in sorted(directories):
            if name in EXCLUDED or name.startswith("."):
                continue
            directory = Path(current) / name
            if is_link(directory):
                snapshot.skipped.append(directory.relative_to(root).as_posix())
                continue
            accepted.append(name)
        directories[:] = accepted
        for name in sorted(files):
            seen += 1
            if seen > MAX_FILES:
                raise PolicyError(f"Repository exceeds the {MAX_FILES}-file scan limit.")
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            relevant = (
                name.endswith(".py")
                or name in MANIFESTS
                or (name.startswith("requirements") and name.endswith(".txt"))
            )
            if not relevant:
                continue
            if any(word in name.lower() for word in ("credential", "secret", "token")):
                snapshot.skipped.append(relative)
                continue
            try:
                path = safe_path(root, relative)
                if path.stat().st_size > MAX_FILE_BYTES:
                    snapshot.skipped.append(relative)
                    snapshot.warnings.append(f"File exceeds size limit: {relative}")
                    continue
                if name.endswith(".py"):
                    snapshot.python_files.append(relative)
                else:
                    snapshot.manifests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, PolicyError):
                snapshot.skipped.append(relative)
    return snapshot
