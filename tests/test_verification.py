import json
import sys
from pathlib import Path

import pytest
from helpers import LEGACY_IMAGE, TARGET_IMAGE, FakeRunner, verified_run

from apiheal.core.errors import PolicyError, VerificationError
from apiheal.core.git import git
from apiheal.core.policy import required_checks_pass
from apiheal.repair.dependency_edit import TARGET_OPENAI
from apiheal.verification.container import DockerRunner, harness_digest, setup_runner
from apiheal.verification.process import ProcessResult, environment, run_bounded
from apiheal.verification.runner import REQUIRED_GATES, verify_candidate
from apiheal.verification.snapshot import copy_for_execution, snapshot_digest


def test_three_environments_and_explicit_contract_alternative(candidate_run):
    _, candidate, result, store, runner = verified_run(candidate_run)
    assert required_checks_pass(result.checks, set(result.plan.required_checks))
    assert REQUIRED_GATES <= set(result.plan.required_checks)
    assert result.verification.snapshot_sha256 == snapshot_digest(candidate)
    assert result.verification.contract_only and not result.verification.live_provider_tested
    assert [(image, request["action"]) for image, request in runner.calls] == [
        (TARGET_IMAGE, "dependencies"),
        (TARGET_IMAGE, "syntax_imports"),
        (LEGACY_IMAGE, "contract"),
        (TARGET_IMAGE, "expected_failure"),
        (TARGET_IMAGE, "contract"),
    ]
    assert next(c for c in result.checks if c.gate == "unit").required is False
    assert (store.directory(result.run_id) / "sdk_contract.log").is_file()


@pytest.mark.parametrize(
    "gate",
    [
        "dependencies",
        "syntax_imports",
        "baseline_contract",
        "expected_failure",
        "sdk_contract",
    ],
)
def test_required_container_failure_stops_pipeline(candidate_run, gate):
    _, baseline, candidate, result, store = candidate_run
    with pytest.raises(VerificationError):
        verify_candidate(baseline, candidate, result, store, FakeRunner(failure=gate))
    assert result.verification is None
    assert any(c.gate == gate and c.outcome == "failed" for c in result.checks)
    assert not required_checks_pass(result.checks, set(result.plan.required_checks))


@pytest.mark.parametrize(
    "runner", [FakeRunner(malformed=True), FakeRunner(count=0), FakeRunner(mismatch=True)]
)
def test_bad_evidence_zero_cases_and_behavior_regressions_block(candidate_run, runner):
    _, baseline, candidate, result, store = candidate_run
    with pytest.raises(VerificationError):
        verify_candidate(baseline, candidate, result, store, runner)
    assert result.verification is None


def test_patch_artifact_tampering_is_rejected(candidate_run):
    _, baseline, candidate, result, store = candidate_run
    store.write_text(result.run_id, "patch.diff", "tampered")
    with pytest.raises(PolicyError, match="patch digest"):
        verify_candidate(baseline, candidate, result, store, FakeRunner())


def test_new_file_changes_snapshot_and_is_rejected(candidate_run):
    _, _, candidate, _, _ = candidate_run
    (candidate / "extra.py").write_text("pass\n")
    with pytest.raises(PolicyError, match="untracked"):
        snapshot_digest(candidate)


def test_extra_tracked_file_cannot_be_hidden_outside_published_patch(candidate_run):
    _, baseline, candidate, result, store = candidate_run
    (candidate / "extra.py").write_text("pass\n")
    git(candidate, "add", "extra.py")
    with pytest.raises(PolicyError, match="tracked paths"):
        verify_candidate(baseline, candidate, result, store, FakeRunner())


def test_modified_approved_path_still_needs_the_exact_patch_bytes(candidate_run):
    _, baseline, candidate, result, store = candidate_run
    path = candidate / "app.py"
    path.write_text(path.read_text() + "\n# extra unapproved content\n")
    with pytest.raises(PolicyError, match="patch digest"):
        verify_candidate(baseline, candidate, result, store, FakeRunner())


def test_discovered_unit_suite_becomes_required(candidate_run, monkeypatch):
    _, baseline, candidate, result, store = candidate_run
    monkeypatch.setattr("apiheal.verification.runner.unit_files", lambda _: ["tests/test_app.py"])
    runner = FakeRunner(failure="unit")
    with pytest.raises(VerificationError):
        verify_candidate(baseline, candidate, result, store, runner)
    assert "unit" in result.plan.required_checks
    assert result.checks[-1].gate == "unit" and result.checks[-1].outcome == "failed"


def test_copy_omits_git_metadata_and_never_executes_target(candidate_run, tmp_path):
    _, baseline, _, _, _ = candidate_run
    destination = tmp_path / "snapshot"
    copy_for_execution(baseline, destination)
    assert (destination / "app.py").is_file()
    assert not (destination / ".git").exists()


def test_container_command_has_limits_and_no_host_secrets(candidate_run, monkeypatch):
    _, baseline, _, _, _ = candidate_run
    runner = DockerRunner()
    calls = []

    def command(arguments, *, timeout=30):
        calls.append(arguments)
        return ProcessResult(0, "{}")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-container")
    monkeypatch.setattr(runner, "command", command)
    runner.execute(TARGET_IMAGE, baseline, {"action": "dependencies"})
    invocation = calls[0]
    assert invocation[invocation.index("--network") + 1] == "none"
    assert "--read-only" in invocation and "--cap-drop" in invocation
    assert "--pids-limit" in invocation and "--memory" in invocation and "--cpus" in invocation
    assert "65532:65532" in invocation and "no-new-privileges:true" in invocation
    assert all("must-not-enter-container" not in part for part in invocation)
    assert "OPENAI_API_KEY" not in environment()
    assert calls[-1][:2] == ["rm", "--force"]


def test_container_is_removed_when_execution_raises(candidate_run, monkeypatch):
    _, baseline, _, _, _ = candidate_run
    runner = DockerRunner()
    calls = []

    def command(arguments, *, timeout=30):
        calls.append(arguments)
        if arguments[0] == "run":
            raise KeyboardInterrupt
        return ProcessResult(0)

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(KeyboardInterrupt):
        runner.execute(TARGET_IMAGE, baseline, {})
    assert calls[-1][:2] == ["rm", "--force"]


def test_missing_docker_has_no_host_execution_fallback(monkeypatch):
    monkeypatch.setattr("apiheal.verification.container.shutil.which", lambda _: None)
    with pytest.raises(VerificationError, match="Docker CLI is unavailable"):
        DockerRunner().check_available()


def test_docker_requires_reported_resource_isolation(monkeypatch):
    runner = DockerRunner()
    info = {
        "OSType": "linux",
        "MemoryLimit": True,
        "PidsLimit": True,
        "CpuCfsQuota": True,
        "CgroupDriver": "systemd",
        "SecurityOptions": ["name=seccomp,profile=builtin"],
    }
    monkeypatch.setattr(runner, "command", lambda *a, **k: ProcessResult(0, json.dumps(info)))
    runner.check_available()
    info["PidsLimit"] = False
    with pytest.raises(VerificationError, match="limits"):
        runner.check_available()


def test_image_digest_and_harness_labels_are_required(monkeypatch):
    runner = DockerRunner()
    inspected = {
        "Id": TARGET_IMAGE,
        "Os": "linux",
        "Config": {
            "Labels": {
                "org.apiheal.sdk": TARGET_OPENAI,
                "org.apiheal.harness-sha256": harness_digest(),
            }
        },
    }
    monkeypatch.setattr(
        runner, "command", lambda *a, **k: ProcessResult(0, json.dumps([inspected]))
    )
    assert runner.resolve_image(TARGET_OPENAI) == TARGET_IMAGE
    inspected["Config"]["Labels"]["org.apiheal.harness-sha256"] = "outdated"
    with pytest.raises(VerificationError, match="outdated"):
        runner.resolve_image(TARGET_OPENAI)


def test_setup_builds_only_trusted_assets(monkeypatch):
    class Builder(FakeRunner):
        def command(self, arguments, **kwargs):
            assert arguments[0] == "build"
            context = Path(arguments[-1])
            assert sorted(p.name for p in context.iterdir()) == [
                "Dockerfile",
                "harness.py",
                "pytest.ini",
            ]
            assert "--build-arg" in arguments
            self.calls.append(arguments)
            return ProcessResult(0)

    builder = Builder()
    assert len(setup_runner(builder)) == 2 and len(builder.calls) == 2


def test_bounded_process_timeout_and_output_limit():
    result = run_bounded([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.05)
    assert result.interrupted
    result = run_bounded([sys.executable, "-c", "print('x'*10000)"], max_bytes=100)
    assert result.interrupted and len(result.stdout) <= 100


def test_github_credentials_are_excluded_from_docker_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake-gh-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-model-token")
    assert "GH_TOKEN" not in environment()
    assert environment(github=True)["GH_TOKEN"] == "fake-gh-token"
    assert "ANTHROPIC_API_KEY" not in environment(github=True)


@pytest.mark.docker
def test_real_docker_three_environment_demo(candidate_run):
    # Opt-in: the ordinary test suite never pulls/builds/runs Docker images.
    import os

    if os.environ.get("APIHEAL_DOCKER_TESTS") != "1":
        pytest.skip("Set APIHEAL_DOCKER_TESTS=1 after setup-runner for real SDK integration.")
    _, baseline, candidate, result, store = candidate_run
    verify_candidate(baseline, candidate, result, store)
    assert required_checks_pass(result.checks, set(result.plan.required_checks))
    assert result.verification.contract_only


@pytest.mark.docker
def test_real_docker_cli_verify(repository, tmp_path):
    import os

    from typer.testing import CliRunner

    from apiheal.cli import app

    if os.environ.get("APIHEAL_DOCKER_TESTS") != "1":
        pytest.skip("Real CLI container verification requires APIHEAL_DOCKER_TESTS=1.")
    response = CliRunner().invoke(
        app,
        [
            "repair",
            str(repository),
            "--verify",
            "--format",
            "json",
            "--data-dir",
            str(tmp_path / "runs"),
        ],
    )
    assert response.exit_code == 0, response.output
    payload = json.loads(response.stdout)
    assert (
        payload["outcome"] == "verified"
        and payload["verification"]["live_provider_tested"] is False
    )
    assert next(c for c in payload["checks"] if c["gate"] == "sdk_contract")["test_count"] == 3
