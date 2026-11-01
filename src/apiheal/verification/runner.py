"""Three-environment SDK contract verification and auditable evidence."""

import ast
import hashlib
import json
import tempfile
from pathlib import Path

from apiheal.core.errors import PolicyError, VerificationError
from apiheal.core.git import git
from apiheal.core.models import CheckResult, Finding, RunResult, VerificationEvidence
from apiheal.core.policy import Policy
from apiheal.core.run_store import RunStore
from apiheal.repair.dependency_edit import TARGET_OPENAI
from apiheal.telemetry.redaction import redact, redact_text
from apiheal.verification.container import LEGACY_OPENAI, DockerRunner, harness_digest
from apiheal.verification.snapshot import (
    contents,
    copy_for_execution,
    snapshot_digest,
    tracked_files,
    validate_delta,
)

REQUIRED_GATES = {
    "sandbox",
    "patch_apply",
    "dependencies",
    "syntax_imports",
    "baseline_contract",
    "expected_failure",
    "sdk_contract",
    "snapshot_integrity",
}


def contract_targets(baseline: Path, findings: list[Finding]) -> list[dict[str, str]]:
    targets = []
    for finding in findings:
        tree = ast.parse((baseline / finding.file).read_bytes())
        match = None
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            if not any(
                isinstance(node, ast.Call) and node.lineno == finding.span.line
                for node in ast.walk(function)
            ):
                continue
            parameters = [*function.args.posonlyargs, *function.args.args]
            required = [a.arg for a in parameters[: len(parameters) - len(function.args.defaults)]]
            if (
                function.args.posonlyargs
                or not {"model", "messages"}.issubset({a.arg for a in parameters})
                or set(required) - {"model", "messages"}
                or any(default is None for default in function.args.kw_defaults)
            ):
                raise VerificationError(
                    "The initial contract harness requires callable model/messages parameters "
                    "and no other required parameters."
                )
            match = {"file": finding.file, "function": function.name}
        if match is None:
            raise VerificationError(
                "Could not resolve a supported function for contract verification."
            )
        if match not in targets:
            targets.append(match)
    if not targets:
        raise VerificationError("No relevant SDK contract cases were selected.")
    return targets


def unit_files(root: Path) -> list[str]:
    return [
        name
        for name in contents(root, execution_only=True)
        if Path(name).suffix == ".py"
        and (Path(name).name.startswith("test_") or Path(name).name.endswith("_test.py"))
    ]


def _check(
    runner: DockerRunner,
    image: str,
    snapshot: Path,
    action: str,
    gate: str,
    request: dict,
    result: RunResult,
    store: RunStore,
) -> dict:
    completed = runner.execute(image, snapshot, {"action": action, **request})
    store.write_text(result.run_id, f"{gate}.log", completed.stdout + "\n" + completed.stderr)
    try:
        payload = json.loads(completed.stdout)
        if (
            completed.interrupted
            or completed.returncode not in {0, 1}
            or payload.get("schema_version") != 1
            or payload.get("action") != action
            or payload.get("outcome") not in {"passed", "failed"}
            or not isinstance(payload.get("dependencies"), dict)
            or not isinstance(payload.get("facts"), list)
            or payload["dependencies"].get("openai")
            != (LEGACY_OPENAI if gate == "baseline_contract" else TARGET_OPENAI)
        ):
            raise ValueError
        passed = payload["outcome"] == "passed" and completed.returncode == 0
        count = payload.get("test_count")
        if action in {"contract", "expected_failure", "unit"}:
            expected_count = len(request.get("targets", [])) * (3 if action == "contract" else 1)
            if type(count) is not int or count < max(expected_count, 1):
                passed = False
            if action != "unit" and (
                count != expected_count or len(payload["facts"]) != expected_count
            ):
                passed = False
            if action == "expected_failure" and any(
                fact.get("exception") != "APIRemovedInV1" for fact in payload["facts"]
            ):
                passed = False
        outcome = "passed" if passed else "failed"
        message = (
            "Offline check passed."
            if passed
            else redact_text(str(payload.get("error", "Container check failed.")))
        )
    except (ValueError, TypeError, AttributeError):
        payload = {}
        count, outcome = None, "indeterminate"
        message = "Container timed out, exceeded output limits, or returned invalid evidence."
    result.checks.append(
        CheckResult(
            gate=gate,
            outcome=outcome,
            command=["container", image, action],
            duration_seconds=completed.duration,
            exit_code=completed.returncode,
            test_count=count,
            message=message,
        )
    )
    if outcome != "passed":
        raise VerificationError(message)
    return redact(payload)


def verify_candidate(
    baseline: Path,
    candidate: Path,
    result: RunResult,
    store: RunStore,
    runner: DockerRunner | None = None,
) -> None:
    runner = runner or DockerRunner()
    result.plan.required_checks = sorted(REQUIRED_GATES)
    try:
        runner.check_available()
        images = {
            version: runner.resolve_image(version) for version in (LEGACY_OPENAI, TARGET_OPENAI)
        }
        result.checks.append(
            CheckResult(
                gate="sandbox",
                outcome="passed",
                message="Linux Docker runner and matching immutable images.",
            )
        )
        original_digest, candidate_digest = snapshot_digest(baseline), snapshot_digest(candidate)
        actual_changes = validate_delta(
            baseline,
            candidate,
            result.patch.base_commit,
            set(result.patch.changed_paths),
        )
        actual_patch, _ = Policy().validate_changes(
            result.patch.base_commit,
            actual_changes,
            set(result.plan.allowed_paths),
        )
        if actual_patch.sha256 != result.patch.sha256:
            raise PolicyError("Candidate bytes do not match the approved patch digest.")
        lint_configs = {
            "ruff.toml",
            ".ruff.toml",
            "mypy.ini",
            ".mypy.ini",
            "setup.cfg",
            ".flake8",
            "tox.ini",
            "pytest.ini",
            ".pytest.ini",
        }
        if any(Path(item.path).name in lint_configs for item in tracked_files(candidate)):
            raise VerificationError(
                "Configured lint/type/test gates are not supported by this prepared runner yet; "
                "they cannot be silently skipped."
            )
        patch_path = store.directory(result.run_id) / "patch.diff"
        if (
            not patch_path.is_file()
            or hashlib.sha256(patch_path.read_bytes()).hexdigest() != result.patch.sha256
        ):
            raise PolicyError("The saved patch does not match the planned patch digest.")
        applied = git(baseline, "apply", "--check", str(patch_path), check=False)
        result.checks.append(
            CheckResult(
                gate="patch_apply",
                outcome="passed" if applied.returncode == 0 else "failed",
                command=["git", "apply", "--check"],
                exit_code=applied.returncode,
                message="Patch applies to the pinned baseline."
                if applied.returncode == 0
                else "Patch does not apply to the baseline.",
            )
        )
        if applied.returncode:
            raise VerificationError("Patch did not apply cleanly.")
        targets = contract_targets(baseline, [f for f in result.findings if f.repairable])
        tests = unit_files(candidate)
        if tests:
            result.plan.required_checks.append("unit")
        with tempfile.TemporaryDirectory(prefix="apiheal-verify-") as directory:
            base_copy, candidate_copy = Path(directory) / "baseline", Path(directory) / "candidate"
            copy_for_execution(baseline, base_copy)
            copy_for_execution(candidate, candidate_copy)
            target_image = images[TARGET_OPENAI]
            dependencies = _check(
                runner,
                target_image,
                candidate_copy,
                "dependencies",
                "dependencies",
                {},
                result,
                store,
            )
            _check(
                runner,
                target_image,
                candidate_copy,
                "syntax_imports",
                "syntax_imports",
                {"files": [name for name in result.patch.changed_paths if name.endswith(".py")]},
                result,
                store,
            )
            original = _check(
                runner,
                images[LEGACY_OPENAI],
                base_copy,
                "contract",
                "baseline_contract",
                {"targets": targets},
                result,
                store,
            )
            _check(
                runner,
                target_image,
                base_copy,
                "expected_failure",
                "expected_failure",
                {"targets": targets},
                result,
                store,
            )
            repaired = _check(
                runner,
                target_image,
                candidate_copy,
                "contract",
                "sdk_contract",
                {"targets": targets},
                result,
                store,
            )
            if original["facts"] != repaired["facts"]:
                result.checks[-1].outcome = "failed"
                result.checks[
                    -1
                ].message = "Original/candidate request or response contracts differ."
                raise VerificationError("Behavior differs from the legacy baseline.")
            if tests:
                _check(
                    runner,
                    target_image,
                    candidate_copy,
                    "unit",
                    "unit",
                    {"files": tests},
                    result,
                    store,
                )
            else:
                result.checks.append(
                    CheckResult(
                        gate="unit",
                        required=False,
                        outcome="skipped",
                        message="No repository unit suite. The trusted contract suite "
                        "SDK contract suite is the documented alternative check.",
                    )
                )
        if (
            snapshot_digest(baseline) != original_digest
            or snapshot_digest(candidate) != candidate_digest
        ):
            raise PolicyError("Snapshot changed during verification.")
        result.checks.append(
            CheckResult(
                gate="snapshot_integrity",
                outcome="passed",
                message="Baseline and candidate match recorded snapshots.",
            )
        )
        result.verification = VerificationEvidence(
            base_commit=result.patch.base_commit,
            patch_sha256=result.patch.sha256,
            snapshot_sha256=candidate_digest,
            baseline_snapshot_sha256=original_digest,
            policy_sha256=hashlib.sha256(Policy().model_dump_json().encode()).hexdigest(),
            harness_sha256=harness_digest(),
            images=images,
            resolved_dependencies={
                LEGACY_OPENAI: original["dependencies"],
                TARGET_OPENAI: dependencies["dependencies"],
            },
            contract_only=not bool(tests),
        )
    except VerificationError as error:
        if not result.checks or result.checks[-1].outcome == "passed":
            result.checks.append(
                CheckResult(gate="verification_runner", outcome="indeterminate", message=str(error))
            )
        raise
