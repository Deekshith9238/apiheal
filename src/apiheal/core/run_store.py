"""Atomic, redacted run artifacts outside target checkouts."""

import json
import os
import re
import tempfile
from pathlib import Path

from apiheal.core.errors import InputError
from apiheal.core.models import RunResult
from apiheal.telemetry.redaction import redact, redact_text


def default_data_dir() -> Path:
    if path := os.environ.get("APIHEAL_DATA_DIR"):
        return Path(path).expanduser()
    return Path.home() / ".apiheal" / "runs"


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or default_data_dir()).expanduser().resolve()

    def ensure_outside(self, repository: Path) -> None:
        if self.root.is_relative_to(repository.resolve()):
            raise InputError("The run data directory must be outside the target repository.")

    def directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise InputError("Invalid run ID: expected 32 lowercase hexadecimal characters.")
        directory = self.root / run_id
        if directory.is_symlink() or not directory.resolve().is_relative_to(self.root):
            raise InputError("Unsafe run directory.")
        return directory

    def write_text(self, run_id: str, name: str, text: str) -> Path:
        return self._write(run_id, name, redact_text(text))

    def _write(self, run_id: str, name: str, text: str) -> Path:
        if Path(name).name != name or name in {".", ".."} or ":" in name or "\\" in name:
            raise InputError("Artifact names must be plain filenames.")
        directory = self.directory(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        if target.is_symlink():
            raise InputError("Refusing a symlink artifact.")
        fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    def save(self, result: RunResult, diff: str | None = None) -> None:
        if diff is not None:
            self.write_text(result.run_id, "patch.diff", diff)
            result.artifacts["patch"] = "patch.diff"
        for field, filename in (
            ("findings", "findings.json"),
            ("diagnosis", "diagnosis.json"),
            ("plan", "repair-plan.json"),
            ("checks", "verification.json"),
            ("verification", "verification-evidence.json"),
            ("publication", "publication.json"),
        ):
            value = result.model_dump(mode="json")[field]
            if value:
                self._write(result.run_id, filename, json.dumps(redact(value), indent=2))
                result.artifacts[field] = filename
        data = redact(result.model_dump(mode="json"))
        self._write(result.run_id, "run.json", json.dumps(data, indent=2))
        summary = (
            f"# apiheal run {result.run_id}\n\n"
            f"Command: {result.command}\n\nOutcome: {result.outcome.value}\n\n"
            f"{result.message}\n\nFindings: {len(result.findings)}\n"
        )
        if result.diagnosis:
            summary += "\n" + "\n".join(f"- {s}" for s in result.diagnosis.manual_steps) + "\n"
        if result.checks:
            summary += (
                "\n"
                + "\n".join(f"- {c.gate}: {c.outcome} — {c.message}" for c in result.checks)
                + "\n"
            )
        if result.publication:
            summary += f"\nPull request: {result.publication.url}\n"
        self.write_text(result.run_id, "report.md", summary)

    def load(self, run_id: str) -> RunResult:
        path = self.directory(run_id) / "run.json"
        if not path.is_file() or path.is_symlink():
            raise InputError(f"Run {run_id} was not found.")
        try:
            return RunResult.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise InputError("Run report is unreadable or has an unsupported schema.") from exc
