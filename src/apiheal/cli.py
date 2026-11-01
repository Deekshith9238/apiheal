"""Small CLI with structured results and actionable exit statuses."""

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from apiheal import __version__
from apiheal.core import orchestrator
from apiheal.core.errors import ApihealError
from apiheal.core.models import Outcome, RunResult
from apiheal.core.run_store import RunStore
from apiheal.telemetry.redaction import redact, redact_text
from apiheal.verification.container import setup_runner

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class Endpoint(StrEnum):
    UNKNOWN = "unknown"
    DEVELOPER = "developer_api"
    VERTEX = "vertex_ai"


def exit_code(result: RunResult) -> int:
    if result.outcome == Outcome.PUBLICATION_FAILED:
        return 5
    if result.outcome == Outcome.POLICY_BLOCKED:
        return 3
    if result.outcome in {Outcome.VERIFICATION_FAILED, Outcome.VERIFICATION_INDETERMINATE}:
        return 4
    if result.outcome in {Outcome.FINDINGS, Outcome.UNSUPPORTED, Outcome.MANUAL_ACTION}:
        return 1
    return 0


def _execute(action: Callable[[], RunResult], fmt: OutputFormat, store: RunStore) -> None:
    try:
        result = action()
    except (ApihealError, OSError) as exc:
        code = exc.exit_code if isinstance(exc, ApihealError) else 5
        message = redact_text(str(exc))
        if fmt == OutputFormat.JSON:
            typer.echo(json.dumps({"schema_version": 1, "error": message, "exit_code": code}))
        else:
            typer.echo(f"apiheal: {message}", err=True)
        raise typer.Exit(code) from exc
    if fmt == OutputFormat.JSON:
        typer.echo(json.dumps(redact(result.model_dump(mode="json")), indent=2))
    else:
        typer.echo(redact_text(f"{result.outcome.value}: {result.message}"))
        for finding in result.findings:
            position = f":{finding.span.line}" if finding.span else ""
            typer.echo(
                redact_text(f"  {finding.file}{position} [{finding.rule_id}] {finding.message}")
            )
        if result.diagnosis:
            diagnosis = result.diagnosis
            typer.echo(f"  {diagnosis.category} ({diagnosis.confidence} confidence)")
            for step in diagnosis.manual_steps:
                typer.echo(redact_text(f"  - {step}"))
        if result.repository:
            for warning in result.repository.warnings:
                typer.echo(redact_text(f"  Warning: {warning}"))
            for path in result.repository.skipped:
                typer.echo(redact_text(f"  Skipped: {path}"))
        typer.echo(f"Run: {result.run_id}")
        typer.echo(f"Report: {store.directory(result.run_id) / 'report.md'}")
        if result.patch:
            typer.echo(f"Patch: {store.directory(result.run_id) / 'patch.diff'}")
        for check in result.checks:
            typer.echo(redact_text(f"  {check.gate}: {check.outcome} — {check.message}"))
        if result.publication:
            typer.echo(f"Pull request: {result.publication.url}")
    raise typer.Exit(exit_code(result))


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def callback(
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True)] = False,
):
    """Diagnose AI integrations and propose narrow, reviewable migrations."""


@app.command()
def scan(
    repository: Annotated[Path, typer.Argument(help="Git repository or a directory inside it.")],
    format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TEXT,
    data_dir: Annotated[
        Path | None, typer.Option(help="Run storage outside the target checkout.")
    ] = None,
):
    """Statically find legacy SDK patterns without importing target code."""
    store = RunStore(data_dir)
    _execute(lambda: orchestrator.scan(repository, store), format, store)


@app.command()
def diagnose(
    repository: Path,
    log: Annotated[Path, typer.Option(help="UTF-8 provider error log; redacted before storage.")],
    endpoint: Annotated[Endpoint, typer.Option()] = Endpoint.UNKNOWN,
    format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TEXT,
    data_dir: Path | None = None,
):
    """Classify a Gemini incident offline; never change credentials or cloud settings."""
    store = RunStore(data_dir)
    _execute(lambda: orchestrator.diagnose(repository, log, store, endpoint.value), format, store)


@app.command()
def repair(
    repository: Path,
    rule: str = "openai-legacy-completions",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Plan a diff without applying it.")
    ] = False,
    verify: Annotated[bool, typer.Option("--verify", help="Require sandbox verification.")] = False,
    pr: Annotated[bool, typer.Option("--pr", help="Requires passing verification.")] = False,
    base: Annotated[str | None, typer.Option("--base", help="GitHub base branch for --pr.")] = None,
    format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TEXT,
    data_dir: Path | None = None,
):
    """Propose a migration, optionally verify in Docker and open a draft GitHub PR."""
    store = RunStore(data_dir)
    _execute(
        lambda: orchestrator.repair(repository, store, rule, dry_run, verify, pr, base),
        format,
        store,
    )


@app.command("setup-runner")
def prepare_runner(
    format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TEXT,
):
    """Build trusted legacy/target SDK images. Requires Docker and network for the build."""
    typer.echo("Preparing trusted SDK runner images; the build may take several minutes.", err=True)
    try:
        images = setup_runner()
    except ApihealError as error:
        if format == OutputFormat.JSON:
            typer.echo(
                json.dumps(
                    {
                        "schema_version": 1,
                        "error": redact_text(str(error)),
                        "exit_code": error.exit_code,
                    }
                )
            )
        else:
            typer.echo(redact_text(str(error)), err=True)
        raise typer.Exit(error.exit_code) from error
    if format == OutputFormat.JSON:
        typer.echo(json.dumps({"schema_version": 1, "images": images}, indent=2))
    else:
        for version, image in images.items():
            typer.echo(f"OpenAI {version}: {image}")


@app.command()
def report(
    run_id: Annotated[str, typer.Option(help="ID from a previous run.")],
    format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TEXT,
    data_dir: Path | None = None,
):
    """Read a saved, redacted run result."""
    store = RunStore(data_dir)
    _execute(lambda: store.load(run_id), format, store)


def main() -> None:
    app()
