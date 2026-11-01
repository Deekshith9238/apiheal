import pytest

from apiheal.core.errors import InputError, PolicyError
from apiheal.core.git import git, head, is_dirty
from apiheal.core.worktree import IsolatedWorktree
from apiheal.detection.inventory import inventory, safe_path


def test_worktree_edits_and_exceptions_leave_caller_untouched(repository):
    original = (repository / "app.py").read_bytes()
    baseline = head(repository)
    with pytest.raises(RuntimeError), IsolatedWorktree(repository) as worktree:
        path = worktree.path
        parent = worktree.parent
        assert path != repository and head(path) == baseline
        (path / "app.py").write_text("changed", encoding="utf-8")
        raise RuntimeError("deliberate interruption")
    assert (repository / "app.py").read_bytes() == original
    assert head(repository) == baseline and not is_dirty(repository)
    assert not path.exists() and not parent.exists()
    assert git(repository, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1


def test_dirty_and_untracked_files_block_repair(repository):
    (repository / "user changes.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(PolicyError, match="clean"):
        with IsolatedWorktree(repository):
            pytest.fail("Must not create a repair workspace")
    assert (repository / "user changes.txt").read_text() == "keep me"


def test_unborn_repository_blocks_repair(tmp_path):
    git(tmp_path, "init")
    with pytest.raises(PolicyError, match="committed"):
        with IsolatedWorktree(tmp_path):
            pytest.fail("Must not repair an unborn repository")


def test_cleanup_rejects_wrong_owner(repository):
    worktree = IsolatedWorktree(repository)
    worktree.__enter__()
    marker = worktree.parent / ".apiheal-owner"
    try:
        marker.write_text("someone else", encoding="ascii")
        with pytest.raises(PolicyError, match="ownership"):
            worktree.cleanup()
        assert worktree.path.exists()
    finally:
        marker.write_text(worktree.token, encoding="ascii")
        worktree.cleanup()


def test_repository_hooks_are_disabled(repository, tmp_path):
    flag = tmp_path / "hook-ran"
    hook = repository / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{flag.as_posix()}'\n", encoding="utf-8")
    hook.chmod(0o755)
    with IsolatedWorktree(repository):
        pass
    assert not flag.exists()


def test_git_smudge_filters_are_disabled_before_checkout(repository, tmp_path):
    flag = tmp_path / "filter-ran"
    (repository / ".gitattributes").write_text("*.py filter=trap\n", encoding="utf-8")
    git(repository, "add", ".gitattributes")
    git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "Attribute fixture")
    command = f"echo triggered > '{flag.as_posix()}'; cat"
    git(repository, "config", "filter.trap.smudge", command)
    git(repository, "config", "filter.trap.clean", command)
    git(repository, "config", "filter.trap.required", "true")
    original = (repository / "app.py").read_bytes()
    with IsolatedWorktree(repository) as worktree:
        assert (worktree.path / "app.py").read_bytes() == original
    assert not flag.exists()


def test_inventory_never_imports_source(repository):
    (repository / "never_run.py").write_text("raise RuntimeError('DO NOT EXECUTE')\n")
    (repository / ".venv").mkdir()
    (repository / ".venv/ignored.py").write_text("irrelevant")
    (repository / "credentials.py").write_text("private material")
    result = inventory(repository)
    assert "never_run.py" in result.python_files
    assert ".venv/ignored.py" not in result.python_files
    assert "credentials.py" in result.skipped
    assert set(result.manifests) == {"requirements.txt"}
    assert result.dirty


def test_symlink_source_and_path_escape_are_blocked(repository, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("do not inspect", encoding="utf-8")
    try:
        (repository / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    assert "linked.py" in inventory(repository).skipped
    with pytest.raises(PolicyError):
        safe_path(repository, "../outside.py")
    with pytest.raises(PolicyError):
        safe_path(repository, "linked.py")


def test_invalid_directory_has_actionable_error(tmp_path):
    with pytest.raises(InputError):
        inventory(tmp_path / "missing")
