"""Disposable detached worktrees with ownership-checked cleanup."""

import tempfile
from pathlib import Path
from uuid import uuid4

from apiheal.core.errors import PolicyError
from apiheal.core.git import git, head, is_dirty, repository_root


class IsolatedWorktree:
    def __init__(self, repository: Path):
        self.repository = repository_root(repository)
        self.base_commit: str | None = None
        self.parent: Path | None = None
        self.path: Path | None = None
        self.token = uuid4().hex

    def __enter__(self) -> "IsolatedWorktree":
        self.base_commit = head(self.repository)
        if self.base_commit is None:
            raise PolicyError("Repair requires a committed baseline. Commit the repository first.")
        if is_dirty(self.repository):
            raise PolicyError("Repair requires a clean checkout; commit changes before retrying.")
        self.parent = Path(tempfile.mkdtemp(prefix="apiheal-worktree-")).resolve()
        (self.parent / ".apiheal-owner").write_text(self.token, encoding="ascii")
        self.path = self.parent / "checkout"
        try:
            git(self.repository, "worktree", "add", "--detach", str(self.path), self.base_commit)
        except BaseException:
            self.cleanup()
            raise
        return self

    def cleanup(self) -> None:
        if self.parent is None or self.path is None:
            return
        marker = self.parent / ".apiheal-owner"
        if (
            self.parent.is_symlink()
            or self.path.is_symlink()
            or marker.is_symlink()
            or self.path.resolve().parent != self.parent
            or not marker.is_file()
            or marker.read_text(encoding="ascii") != self.token
        ):
            raise PolicyError("Worktree ownership check failed; refusing cleanup.")
        if self.path.exists():
            git(self.repository, "worktree", "remove", "--force", str(self.path))
        marker.unlink()
        self.parent.rmdir()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cleanup()
