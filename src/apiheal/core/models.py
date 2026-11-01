"""Versioned records shared by CLI, reports, and repair policy."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Outcome(StrEnum):
    NO_FINDINGS = "no_findings"
    FINDINGS = "findings"
    UNSUPPORTED = "unsupported"
    MANUAL_ACTION = "manual_action_required"
    POLICY_BLOCKED = "policy_blocked"
    PLANNED = "planned"
    PATCH_GENERATED = "patch_generated"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_INDETERMINATE = "verification_indeterminate"
    VERIFIED = "verified"
    PR_OPENED = "pr_opened"
    PUBLICATION_FAILED = "publication_failed"


class Span(Record):
    line: int = Field(ge=1)
    column: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=0)


class RepositorySnapshot(Record):
    root: str
    base_commit: str | None = None
    dirty: bool = False
    remote: str | None = None
    manifests: dict[str, str] = Field(default_factory=dict)
    python_files: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Finding(Record):
    id: str
    rule_id: str
    rule_version: str = "1"
    provider: str
    category: str
    file: str
    span: Span | None = None
    severity: Literal["info", "warning", "error"] = "warning"
    confidence: Literal["low", "medium", "high"] = "high"
    repairable: bool = False
    message: str
    evidence: list[str] = Field(default_factory=list)
    documentation: str | None = None


class Diagnosis(Record):
    provider: str = "gemini"
    endpoint: Literal["developer_api", "vertex_ai", "unknown"] = "unknown"
    auth_context: str = "unknown"
    category: str
    outcome: Outcome = Outcome.MANUAL_ACTION
    confidence: Literal["low", "medium", "high"]
    evidence: list[str]
    likely_causes: list[str]
    missing_evidence: list[str] = Field(default_factory=list)
    manual_steps: list[str]
    documentation: list[str] = Field(default_factory=list)


class RepairPlan(Record):
    rule_id: str
    rule_version: str = "1"
    finding_ids: list[str]
    allowed_paths: list[str]
    target_sdk: str
    required_checks: list[str]
    notes: list[str] = Field(default_factory=list)


class PatchArtifact(Record):
    base_commit: str
    sha256: str
    changed_paths: list[str]
    changed_lines: int = Field(ge=0)


class CheckResult(Record):
    gate: str
    required: bool = True
    outcome: Literal["passed", "failed", "skipped", "indeterminate"]
    command: list[str] = Field(default_factory=list)
    duration_seconds: float = 0
    exit_code: int | None = None
    test_count: int | None = None
    message: str = ""


class VerificationEvidence(Record):
    schema_version: Literal[1] = 1
    base_commit: str
    patch_sha256: str
    snapshot_sha256: str
    baseline_snapshot_sha256: str
    policy_sha256: str
    harness_sha256: str
    images: dict[str, str]
    resolved_dependencies: dict[str, dict[str, str]] = Field(default_factory=dict)
    contract_only: bool = False
    live_provider_tested: bool = False


class Publication(Record):
    repository: str
    base_branch: str
    branch: str
    commit: str
    url: str
    idempotency_key: str
    reused: bool = False


class RunResult(Record):
    schema_version: Literal[1] = 1
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    command: str
    stage: str = "inventory"
    outcome: Outcome = Outcome.NO_FINDINGS
    repository: RepositorySnapshot | None = None
    findings: list[Finding] = Field(default_factory=list)
    diagnosis: Diagnosis | None = None
    plan: RepairPlan | None = None
    patch: PatchArtifact | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    verification: VerificationEvidence | None = None
    publication: Publication | None = None
    message: str = ""
    artifacts: dict[str, str] = Field(default_factory=dict)
