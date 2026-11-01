"""Operator policy: repository content cannot relax these limits."""

import difflib
import hashlib
from pathlib import PurePosixPath

from pydantic import Field

from apiheal.core.errors import PolicyError
from apiheal.core.models import CheckResult, PatchArtifact, Record
from apiheal.telemetry.redaction import redact_text


class Policy(Record):
    max_changed_files: int = Field(default=5, ge=1, le=20)
    max_changed_lines: int = Field(default=200, ge=1, le=2000)

    def validate_changes(
        self,
        base_commit: str,
        changes: dict[str, tuple[str, str]],
        allowed_paths: set[str],
    ) -> tuple[PatchArtifact, str]:
        if not changes:
            raise PolicyError("Repair produced no changes.")
        if set(changes) - allowed_paths:
            raise PolicyError("Patch contains paths outside the repair plan.")
        if len(changes) > self.max_changed_files:
            raise PolicyError("Patch exceeds the changed-file limit; manual review is required.")
        diff = ""
        for name, (before, after) in sorted(changes.items()):
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or any(ord(character) < 32 for character in name)
            ):
                raise PolicyError("Patch contains an unsafe path.")
            if name != "requirements.txt" and (
                path.suffix != ".py"
                or any(
                    part.startswith(".") or part.lower() in {"tests", "test", "build", "dist"}
                    for part in path.parts
                )
                or path.name.startswith(("test_", "conftest", "setup"))
                or path.name.endswith("_test.py")
            ):
                raise PolicyError("Patch attempts to edit a protected or unsupported file.")
            generated = difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
            for line in generated:
                diff += line
                if not line.endswith("\n"):
                    diff += "\n\\ No newline at end of file\n"
        if not diff:
            raise PolicyError("Repair produced no changes.")
        if redact_text(diff) != diff:
            raise PolicyError(
                "Patch context contains potential credentials; no patch artifact was saved. "
                "Remove credentials from source before retrying."
            )
        changed = sum(
            1
            for line in diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        if changed > self.max_changed_lines:
            raise PolicyError("Patch exceeds the changed-line limit; manual review is required.")
        artifact = PatchArtifact(
            base_commit=base_commit,
            sha256=hashlib.sha256(diff.encode()).hexdigest(),
            changed_paths=sorted(changes),
            changed_lines=changed,
        )
        return artifact, diff


def required_checks_pass(checks: list[CheckResult], required_gates: set[str]) -> bool:
    by_gate = {check.gate: check for check in checks}
    if len(by_gate) != len(checks) or not required_gates:
        return False
    return (
        required_gates <= by_gate.keys()
        and all(
            by_gate[gate].required and by_gate[gate].outcome == "passed" for gate in required_gates
        )
        and all(check.outcome == "passed" for check in checks if check.required)
        and all(
            bool(check.command) and check.test_count is not None and check.test_count > 0
            for check in checks
            if check.gate in {"unit", "sdk_contract", "contract"} and check.required
        )
    )
