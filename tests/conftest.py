from pathlib import Path

import pytest

from apiheal.core.git import git
from apiheal.core.models import RepairPlan, RepositorySnapshot, RunResult
from apiheal.core.policy import Policy
from apiheal.core.run_store import RunStore
from apiheal.core.worktree import IsolatedWorktree
from apiheal.detection.python_cst import analyze_source
from apiheal.repair.dependency_edit import TARGET_OPENAI, migrate_requirements


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo with spaces ; literal"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "apiheal tests")
    git(root, "config", "user.email", "tests@example.invalid")
    fixture = Path(__file__).parent.parent / "examples" / "legacy-openai"
    for name in ("app.py", "requirements.txt"):
        (root / name).write_bytes((fixture / name).read_bytes())
    git(root, "add", "--", "app.py", "requirements.txt")
    git(root, "-c", "commit.gpgsign=false", "commit", "-m", "Pinned legacy fixture")
    return root


@pytest.fixture
def candidate_run(repository, tmp_path):
    store = RunStore(tmp_path / "runs")
    with IsolatedWorktree(repository) as baseline, IsolatedWorktree(repository) as candidate:
        source = (baseline.path / "app.py").read_text()
        analysis = analyze_source(source, "app.py")
        requirements = (baseline.path / "requirements.txt").read_text()
        changes = {
            "app.py": (source, analysis.migrated_source),
            "requirements.txt": (requirements, migrate_requirements(requirements)),
        }
        patch, diff = Policy().validate_changes(baseline.base_commit, changes, set(changes))
        for name, (_, updated) in changes.items():
            (candidate.path / name).write_text(updated, encoding="utf-8", newline="\n")
        result = RunResult(
            command="repair",
            repository=RepositorySnapshot(
                root=str(repository),
                base_commit=baseline.base_commit,
                remote="https://github.com/example/demo.git",
            ),
            findings=analysis.findings,
            patch=patch,
            plan=RepairPlan(
                rule_id="openai-legacy-completions",
                finding_ids=[f.id for f in analysis.findings],
                allowed_paths=list(changes),
                target_sdk=f"openai=={TARGET_OPENAI}",
                required_checks=[],
            ),
        )
        store.save(result, diff)
        yield repository, baseline.path, candidate.path, result, store
