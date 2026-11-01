"""Scan, diagnose, and propose patches without executing target code."""

from pathlib import Path

from apiheal.core.errors import (
    ApihealError,
    InputError,
    PolicyError,
    PublicationError,
    VerificationError,
)
from apiheal.core.git import head, is_dirty, repository_root
from apiheal.core.models import CheckResult, Outcome, RepairPlan, RunResult, utcnow
from apiheal.core.policy import Policy, required_checks_pass
from apiheal.core.run_store import RunStore
from apiheal.core.worktree import IsolatedWorktree
from apiheal.detection.inventory import inventory, safe_path
from apiheal.detection.python_cst import RULE, SourceAnalysis, analyze_source, read_source
from apiheal.detection.versions import apply_versions, declared_requirements
from apiheal.diagnosis.gemini_auth import diagnose_gemini
from apiheal.integrations.github import pr_body, publish
from apiheal.repair.dependency_edit import TARGET_OPENAI, migrate_requirements
from apiheal.verification.runner import verify_candidate


def _scan(result: RunResult, repository: Path, store: RunStore) -> dict[str, SourceAnalysis]:
    root = repository_root(repository)
    store.ensure_outside(root)
    result.repository = inventory(root)
    result.stage = "detection"
    analyses = {}
    for relative in result.repository.python_files:
        path = safe_path(root, relative)
        try:
            source, _ = read_source(path)
            analysis = analyze_source(source, relative)
        except (OSError, UnicodeError, SyntaxError) as exc:
            result.repository.warnings.append(
                f"Could not analyze {relative} ({type(exc).__name__})."
            )
            continue
        analyses[relative] = analysis
        result.findings.extend(analysis.findings)
    apply_versions(result.findings, declared_requirements(root, result.repository))
    return analyses


def scan(repository: Path, store: RunStore) -> RunResult:
    result = RunResult(command="scan")
    _scan(result, repository, store)
    if result.findings:
        result.outcome = Outcome.FINDINGS
    elif result.repository.warnings or result.repository.skipped:
        result.outcome = Outcome.UNSUPPORTED
    usages = sum(f.rule_id != "python-analysis" for f in result.findings)
    unreadable = len(result.findings) - usages
    summary = (
        f"Found {usages} deprecated or legacy AI API usage{'' if usages == 1 else 's'}."
        if usages
        else "No deprecated or legacy AI API usages found."
    )
    if unreadable:
        summary += f" {unreadable} file{'' if unreadable == 1 else 's'} could not be analyzed."
    result.message = f"{summary} Static scan only; this does not establish runtime failure."
    result.finished_at = utcnow()
    store.save(result)
    return result


def diagnose(repository: Path, log: Path, store: RunStore, endpoint: str = "unknown") -> RunResult:
    root = repository_root(repository)
    store.ensure_outside(root)
    if not log.is_file() or log.stat().st_size > 1_000_000:
        raise InputError("Diagnostic log must be an existing file of at most 1 MB.")
    result = RunResult(command="diagnose", stage="diagnosis")
    result.repository = inventory(root)
    try:
        text = log.read_text(encoding="utf-8")
    except (UnicodeError, OSError) as exc:
        raise InputError("Diagnostic log must be readable UTF-8 text.") from exc
    result.diagnosis = diagnose_gemini(text, endpoint=endpoint)
    result.outcome = Outcome.MANUAL_ACTION
    result.message = (
        "Offline Gemini diagnosis; no network checks or configuration changes were made."
    )
    result.finished_at = utcnow()
    store.save(result)
    return result


def repair(
    repository: Path,
    store: RunStore,
    rule: str = RULE,
    dry_run: bool = False,
    verify: bool = False,
    pr: bool = False,
    base_branch: str | None = None,
) -> RunResult:
    if rule != RULE:
        raise InputError(f"Automatic repair currently supports only {RULE}.")
    if pr and not verify:
        raise InputError("--pr requires --verify; unverified publishing is never permitted.")
    if dry_run and (verify or pr):
        raise InputError("--dry-run cannot be combined with --verify or --pr.")
    if base_branch is not None and not pr:
        raise InputError("--base is only meaningful with --pr.")
    root = repository_root(repository)
    store.ensure_outside(root)
    result = RunResult(command="repair")
    diff = None
    try:
        with IsolatedWorktree(root) as worktree:
            analyses = _scan(result, worktree.path, store)
            # Reports identify the caller repository, never a soon-to-be-deleted temp directory.
            result.repository.root = str(root)
            relevant = [f for f in result.findings if f.rule_id == rule]
            if not relevant:
                result.outcome = Outcome.NO_FINDINGS
                result.message = "No matching legacy calls found; no patch generated."
                if (
                    result.repository.warnings
                    or result.repository.skipped
                    or any(f.rule_id == "python-analysis" for f in result.findings)
                ):
                    result.outcome = Outcome.UNSUPPORTED
                    result.message = "Scan was incomplete; inspect warnings and skipped paths."
            elif any(not f.repairable for f in relevant):
                result.outcome = Outcome.UNSUPPORTED
                result.message = (
                    "At least one matching call requires manual review; no patch generated."
                )
            else:
                if (
                    result.repository.warnings
                    or result.repository.skipped
                    or any(f.rule_id == "python-analysis" for f in result.findings)
                ):
                    raise PolicyError(
                        "An incomplete source scan cannot authorize automatic repair."
                    )
                if set(result.repository.manifests) != {"requirements.txt"}:
                    raise PolicyError(
                        "Initial repair requires only one root requirements.txt manifest."
                    )
                changes = {}
                encodings = {}
                for relative, analysis in analyses.items():
                    if analysis.migrated_source is not None:
                        before, encoding = read_source(safe_path(worktree.path, relative))
                        if encoding.lower().replace("_", "-") not in {"utf-8", "utf8"}:
                            raise PolicyError(
                                "Initial patch generation supports plain UTF-8 source only."
                            )
                        changes[relative] = (before, analysis.migrated_source)
                        encodings[relative] = encoding
                requirement = safe_path(worktree.path, "requirements.txt")
                before = requirement.read_bytes().decode("utf-8")
                after = migrate_requirements(before)
                if after != before:
                    changes["requirements.txt"] = (before, after)
                    encodings["requirements.txt"] = "utf-8"
                result.stage = "planning"
                result.plan = RepairPlan(
                    rule_id=rule,
                    finding_ids=[f.id for f in relevant],
                    allowed_paths=sorted(changes),
                    target_sdk=f"openai=={TARGET_OPENAI}",
                    required_checks=["dependencies", "syntax_imports", "unit", "sdk_contract"],
                    notes=["Local source proposal only; offline/live behavior is not verified."],
                )
                result.patch, diff = Policy().validate_changes(
                    worktree.base_commit,
                    changes,
                    set(result.plan.allowed_paths),
                )
                if head(root) != worktree.base_commit or is_dirty(root):
                    raise PolicyError("Caller checkout changed while planning; discard and retry.")
                if not dry_run:
                    for relative, (_, updated) in changes.items():
                        safe_path(worktree.path, relative).write_bytes(
                            updated.encode(encodings[relative])
                        )
                result.stage = "planning" if dry_run else "patching"
                result.outcome = Outcome.PLANNED if dry_run else Outcome.PATCH_GENERATED
                result.message = (
                    "Minimal patch generated in isolation and saved for review. "
                    "The caller checkout is unchanged. The patch is unverified."
                )
                if verify:
                    result.stage = "verification"
                    store.save(result, diff)
                    try:
                        with IsolatedWorktree(root) as baseline:
                            if baseline.base_commit != worktree.base_commit:
                                raise PolicyError("Baseline changed before verification; retry.")
                            verify_candidate(baseline.path, worktree.path, result, store)
                        if not required_checks_pass(
                            result.checks, set(result.plan.required_checks)
                        ):
                            raise VerificationError("Required checks are missing or did not pass.")
                    except VerificationError as error:
                        result.outcome = (
                            Outcome.VERIFICATION_FAILED
                            if any(c.required and c.outcome == "failed" for c in result.checks)
                            else Outcome.VERIFICATION_INDETERMINATE
                        )
                        if not result.checks:
                            result.checks.append(
                                CheckResult(
                                    gate="verification_runner",
                                    outcome="indeterminate",
                                    message=str(error),
                                )
                            )
                        result.message = f"Verification could not pass: {error} No PR opened."
                    else:
                        result.outcome = Outcome.VERIFIED
                        result.message = (
                            "Required offline SDK checks passed in isolated containers. "
                            "No live provider call was made. The caller checkout is unchanged."
                        )
                        result.plan.notes = [
                            "Verified against offline SDK contracts; live provider behavior "
                            "and behavior beyond the selected cases are not established.",
                        ]
                        if pr:
                            result.stage = "publication"
                            store.save(result, diff)
                            try:
                                if head(root) != worktree.base_commit or is_dirty(root):
                                    raise PolicyError("Caller checkout changed before publishing.")
                                result.publication = publish(
                                    worktree.path,
                                    result,
                                    store,
                                    base_branch=base_branch,
                                )
                                result.outcome = Outcome.PR_OPENED
                                result.message = (
                                    "Verified offline repair proposed as a draft GitHub PR."
                                )
                            except PublicationError as error:
                                result.outcome = Outcome.PUBLICATION_FAILED
                                result.message = (
                                    f"Local verification passed; publishing failed: {error}"
                                )
                    store.write_text(result.run_id, "pr-body.md", pr_body(result))
                    result.artifacts["pr_body"] = "pr-body.md"
    except PolicyError as exc:
        result.outcome = Outcome.POLICY_BLOCKED
        result.message = str(exc)
    except ApihealError:
        raise
    result.finished_at = utcnow()
    store.save(result, diff)
    return result
