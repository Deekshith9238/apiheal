import json

import pytest
from typer.testing import CliRunner

from apiheal.cli import app
from apiheal.core.errors import VerificationError
from apiheal.core.git import git, head, is_dirty

runner = CliRunner()


def invoke(command, repository, tmp_path, *options):
    return runner.invoke(
        app,
        [
            command,
            str(repository),
            "--data-dir",
            str(tmp_path / "runs"),
            "--format",
            "json",
            *options,
        ],
    )


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert "0.1.0" in runner.invoke(app, ["--version"]).stdout


def test_scan_returns_typed_findings_without_execution(repository, tmp_path):
    response = invoke("scan", repository, tmp_path)
    assert response.exit_code == 1, response.output
    result = json.loads(response.stdout)
    assert result["schema_version"] == 1 and result["outcome"] == "findings"
    assert result["findings"][0]["span"]["line"] == 8
    assert result["message"].startswith("Found 1 deprecated or legacy AI API usage.")
    assert result["repository"]["base_commit"]
    assert not is_dirty(repository)


def test_text_scan_shows_severity_confidence_and_evidence(repository, tmp_path):
    response = runner.invoke(app, ["scan", str(repository), "--data-dir", str(tmp_path / "runs")])
    assert response.exit_code == 1, response.output
    lines = response.stdout.splitlines()
    assert lines[1].endswith("[openai-legacy-completions] warning, high confidence")
    assert lines[2] == "    Supported legacy chat call can be migrated."
    assert lines[3] == "    - Resolved imported call: openai.ChatCompletion.create"


def test_dry_run_generates_diff_and_report_without_caller_edits(repository, tmp_path):
    before = (repository / "app.py").read_bytes()
    response = invoke("repair", repository, tmp_path, "--dry-run")
    assert response.exit_code == 0, response.output
    result = json.loads(response.stdout)
    assert result["outcome"] == "planned"
    directory = tmp_path / "runs" / result["run_id"]
    diff = (directory / "patch.diff").read_text(encoding="utf-8")
    assert "response.choices[0].message.content" in diff
    assert "+openai==1.109.1" in diff
    assert (repository / "app.py").read_bytes() == before
    assert not is_dirty(repository)
    assert git(repository, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1
    report = runner.invoke(
        app,
        [
            "report",
            "--run-id",
            result["run_id"],
            "--data-dir",
            str(tmp_path / "runs"),
            "--format",
            "json",
        ],
    )
    assert report.exit_code == 0
    assert json.loads(report.stdout)["patch"] == result["patch"]


@pytest.mark.parametrize("options", [("--verify",), ("--verify", "--pr")])
def test_missing_verification_never_becomes_success_or_pr(
    repository, tmp_path, options, monkeypatch
):
    def unavailable(_self):
        raise VerificationError("Docker is unavailable in this controlled test.")

    monkeypatch.setattr("apiheal.verification.runner.DockerRunner.check_available", unavailable)
    response = invoke("repair", repository, tmp_path, *options)
    assert response.exit_code == 4, response.output
    result = json.loads(response.stdout)
    assert result["outcome"] == "verification_indeterminate"
    assert result["checks"][0]["outcome"] == "indeterminate"
    assert "No PR" in result["message"]
    assert not is_dirty(repository)


@pytest.mark.parametrize(
    "options",
    [
        ("--pr",),
        ("--dry-run", "--verify"),
        ("--rule", "other"),
        ("--base", "develop"),
    ],
)
def test_invalid_repair_options_are_json_errors(repository, tmp_path, options):
    response = invoke("repair", repository, tmp_path, *options)
    assert response.exit_code == 2
    assert json.loads(response.stdout)["exit_code"] == 2


def test_dirty_repair_is_blocked_and_recorded(repository, tmp_path):
    (repository / "important.txt").write_text("preserve user work")
    response = invoke("repair", repository, tmp_path, "--dry-run")
    assert response.exit_code == 3
    result = json.loads(response.stdout)
    assert result["outcome"] == "policy_blocked"
    assert (tmp_path / "runs" / result["run_id"] / "run.json").exists()
    assert (repository / "important.txt").read_text() == "preserve user work"


def test_non_git_input_is_a_json_error(tmp_path):
    response = invoke("scan", tmp_path, tmp_path)
    assert response.exit_code == 2
    assert "error" in json.loads(response.stdout)


def test_diagnose_keeps_raw_secret_out_of_all_artifacts(repository, tmp_path):
    log = tmp_path / "error.log"
    secret = "sk-proj-" + "synthetic" * 10
    log.write_text(f"403 PERMISSION_DENIED api_key={secret}", encoding="utf-8")
    response = invoke("diagnose", repository, tmp_path, "--log", str(log))
    assert response.exit_code == 1, response.output
    assert secret not in response.stdout
    result = json.loads(response.stdout)
    for path in (tmp_path / "runs" / result["run_id"]).iterdir():
        assert secret not in path.read_text(encoding="utf-8")
    assert secret in log.read_text()  # The caller's input file remains untouched.


def test_incomplete_source_analysis_blocks_repair(repository, tmp_path):
    (repository / "broken.py").write_text("def broken(\n", encoding="utf-8")
    git(repository, "add", "broken.py")
    git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "Unparseable source")
    response = invoke("repair", repository, tmp_path, "--dry-run")
    assert response.exit_code == 3, response.output
    assert json.loads(response.stdout)["outcome"] == "policy_blocked"


def test_saved_dry_run_patch_applies_cleanly(repository, tmp_path):
    response = invoke("repair", repository, tmp_path, "--dry-run")
    result = json.loads(response.stdout)
    patch = tmp_path / "runs" / result["run_id"] / "patch.diff"
    git(repository, "apply", "--check", str(patch))
    assert not is_dirty(repository)


def test_verify_cli_connects_all_gates_and_saves_evidence(repository, tmp_path, monkeypatch):
    from helpers import FakeRunner

    monkeypatch.setattr("apiheal.verification.runner.DockerRunner", FakeRunner)
    response = invoke("repair", repository, tmp_path, "--verify")
    assert response.exit_code == 0, response.output
    result = json.loads(response.stdout)
    assert result["outcome"] == "verified" and result["verification"]
    assert result["publication"] is None
    assert (tmp_path / "runs" / result["run_id"] / "verification-evidence.json").is_file()
    assert not is_dirty(repository)


def test_verify_pr_cli_uses_verified_bytes_and_records_publication(
    repository, tmp_path, monkeypatch
):
    from helpers import FakeRunner
    from test_github import FakeGitHub

    api = FakeGitHub(head(repository))
    git(repository, "config", "remote.origin.url", "https://github.com/example/demo.git")
    monkeypatch.setattr("apiheal.verification.runner.DockerRunner", FakeRunner)
    monkeypatch.setattr("apiheal.integrations.github.GitHubAPI", lambda: api)
    response = invoke("repair", repository, tmp_path, "--verify", "--pr")
    assert response.exit_code == 0, response.output
    result = json.loads(response.stdout)
    assert result["outcome"] == "pr_opened"
    assert result["publication"]["url"] == "https://github.com/example/demo/pull/1"
    assert len(api.pulls) == 1 and not is_dirty(repository)


def test_cli_failed_check_never_calls_publisher(repository, tmp_path, monkeypatch):
    from helpers import FakeRunner

    monkeypatch.setattr(
        "apiheal.verification.runner.DockerRunner", lambda: FakeRunner(failure="dependencies")
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Failed required checks must prevent publication")

    monkeypatch.setattr("apiheal.core.orchestrator.publish", forbidden)
    response = invoke("repair", repository, tmp_path, "--verify", "--pr")
    assert response.exit_code == 4
    assert json.loads(response.stdout)["outcome"] == "verification_failed"


def test_cli_publication_failure_keeps_verification_evidence(repository, tmp_path, monkeypatch):
    from helpers import FakeRunner

    monkeypatch.setattr("apiheal.verification.runner.DockerRunner", FakeRunner)
    response = invoke("repair", repository, tmp_path, "--verify", "--pr")
    assert response.exit_code == 5
    result = json.loads(response.stdout)
    assert result["outcome"] == "publication_failed" and result["verification"]
    assert result["publication"] is None


def test_setup_runner_missing_docker_is_an_actionable_json_error(monkeypatch):
    monkeypatch.setattr("apiheal.verification.container.shutil.which", lambda _: None)
    response = runner.invoke(app, ["setup-runner", "--format", "json"])
    assert response.exit_code == 4
    assert "Docker CLI is unavailable" in json.loads(response.stdout)["error"]


def test_scan_loads_declarative_rule_pack(repository, tmp_path):
    source = (
        "import example_sdk as sdk\n"
        "sdk.old.run()\n"
        "from example_sdk import LegacyClient\n"
        "from .example_sdk import LegacyClient\n"
        "import example_sdk.deprecated\n"
    )
    (repository / "custom.py").write_text(source, encoding="utf-8")
    requirements = repository / "requirements.txt"
    requirements.write_text("openai==0.28.1\nexample-sdk==2.1\n", encoding="utf-8")
    rule = {
        "id": "example-sdk-deprecated",
        "version": "3",
        "provider": "example",
        "category": "deprecation",
        "documentation": "https://example.invalid/migration",
        "dependency": {"package": "example-sdk", "deprecated_since": "1", "removed_in": "2"},
        "selectors": [
            {"kind": "call", "module": "example_sdk", "name": "old.run", "message": "Use new.run."},
            {
                "kind": "symbol",
                "module": "example_sdk",
                "name": "LegacyClient",
                "message": "Use NewClient.",
            },
            {
                "kind": "module",
                "module": "example_sdk.deprecated",
                "message": "Use the new module.",
            },
        ],
    }
    pack = tmp_path / "sdk-rules.json"
    pack.write_text(json.dumps({"schema_version": 1, "rules": [rule]}), encoding="utf-8")

    default = invoke("scan", repository, tmp_path)
    assert {f["rule_id"] for f in json.loads(default.stdout)["findings"]} == {
        "openai-legacy-completions"
    }

    response = invoke("scan", repository, tmp_path, "--rule-pack", str(pack))
    assert response.exit_code == 1, response.output
    findings = json.loads(response.stdout)["findings"]
    assert any(f["rule_id"] == "openai-legacy-completions" for f in findings)
    custom = [f for f in findings if f["rule_id"] == rule["id"]]
    assert [f["span"]["line"] for f in custom] == [2, 3, 5]
    assert all(
        f["rule_version"] == "3" and f["severity"] == "error" and not f["repairable"]
        for f in custom
    )

    for rule_id in ("openai-legacy-completions", "model-retired"):
        rule["id"] = rule_id
        pack.write_text(json.dumps({"schema_version": 1, "rules": [rule]}), encoding="utf-8")
        rejected = invoke("scan", repository, tmp_path, "--rule-pack", str(pack))
        assert rejected.exit_code == 2
        assert "Duplicate or reserved rule ID" in json.loads(rejected.stdout)["error"]


def test_repair_blocks_dependency_upgrade_with_legacy_embeddings(repository, tmp_path):
    (repository / "embeddings.py").write_text(
        "import openai\nopenai.Embedding.create(input=['text'])\n", encoding="utf-8"
    )
    git(repository, "add", "embeddings.py")
    git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "Add legacy embeddings")
    response = invoke("repair", repository, tmp_path, "--dry-run")
    assert response.exit_code == 1, response.output
    result = json.loads(response.stdout)
    assert result["outcome"] == "unsupported"
    assert result["patch"] is None
    assert not is_dirty(repository)
