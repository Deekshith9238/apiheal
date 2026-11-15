"""Scan source files and compare evidence across revisions without running target code."""

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib.resources import files
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

import httpx
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from apiheal.detection.python_cst import analyze_source
from apiheal.github_app.transport import UpstreamError, request_json
from apiheal.telemetry.redaction import redact_text

NPM = "https://registry.npmjs.org"
PACKAGE = re.compile(r"(?:@[a-zA-Z0-9_.-]+/)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*")
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.+-]+)?")
ACTION = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[\w./-]+", re.ASCII)
NPM_LOCKS = {"package-lock.json", "npm-shrinkwrap.json"}


@dataclass(frozen=True)
class Notice:
    key: str
    line: int
    title: str
    message: str
    source: str

    def annotation(self, path: str) -> dict:
        return {
            "path": path,
            "start_line": self.line,
            "end_line": self.line,
            "annotation_level": "warning",
            "title": self.title[:255],
            "message": redact_text(f"{self.message}\nSource: {self.source}")[:2000],
        }


def new_notices(before: list[Notice], after: list[Notice]) -> list[Notice]:
    existing = Counter(notice.key for notice in before)
    added = []
    for notice in after:
        if existing[notice.key]:
            existing[notice.key] -= 1
        else:
            added.append(notice)
    return added


@cache
def retired_runtimes() -> dict:
    resource = files("apiheal.github_app").joinpath("actions_runtimes.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _mapping(node) -> dict:
    if not isinstance(node, MappingNode):
        return {}
    return {key.value: value for key, value in node.value if isinstance(key, ScalarNode)}


def _sequence(node) -> list:
    return node.value if isinstance(node, SequenceNode) else []


def _yaml(source: str):
    try:
        return yaml.compose(source, Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise UpstreamError("Action YAML could not be parsed.") from exc


def _runtime(source: str) -> ScalarNode | None:
    using = _mapping(_mapping(_yaml(source)).get("runs")).get("using")
    return using if isinstance(using, ScalarNode) else None


def _workflow_refs(source: str) -> list[ScalarNode]:
    jobs = _mapping(_mapping(_yaml(source)).get("jobs"))
    refs = []
    for job in jobs.values():
        for step in _sequence(_mapping(job).get("steps")):
            using = _mapping(step).get("uses")
            if isinstance(using, ScalarNode) and ACTION.fullmatch(using.value):
                refs.append(using)
    return refs


def _npm_packages(source: str) -> set[tuple[str, str]]:
    try:
        lock = json.loads(source)
    except (ValueError, RecursionError) as exc:
        raise UpstreamError("npm lockfile could not be parsed.") from exc
    if not isinstance(lock, dict) or lock.get("lockfileVersion") not in (2, 3):
        raise UpstreamError("npm lockfile format is unsupported; expected v2 or v3.")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise UpstreamError("npm lockfile has no package index.")
    resolved_packages = set()
    for location, package in packages.items():
        if not isinstance(package, dict) or "node_modules/" not in location:
            continue
        if package.get("link") or package.get("inBundle"):
            continue
        name = package.get("name", location.rsplit("node_modules/", 1)[1])
        version = package.get("version")
        resolved = package.get("resolved")
        if not all(isinstance(value, str) for value in (name, version, resolved)):
            continue
        try:
            origin = urlsplit(resolved)
        except ValueError:
            continue
        if (
            origin.netloc != "registry.npmjs.org"
            or origin.scheme != "https"
            or not PACKAGE.fullmatch(name)
            or not VERSION.fullmatch(version)
        ):
            continue
        resolved_packages.add((name, version))
    return resolved_packages


def scan_kind(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in NPM_LOCKS:
        return "npm"
    if name in {"action.yml", "action.yaml"}:
        return "action"
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        return "workflow"
    if name.endswith(".py"):
        return "python"
    return None


class Detectors:
    def __init__(
        self,
        client: httpx.Client,
        action_metadata: Callable[[str], str | None],
        today: date,
    ):
        self.client = client
        self.action_metadata = action_metadata
        self.today = today
        self.npm_cache: dict[tuple[str, str], str | None] = {}
        self.action_cache: dict[str, str | None] = {}
        self.warnings: set[str] = set()
        self.methods = {
            "action": self.action,
            "workflow": self.workflow,
            "python": self.python,
        }

    def compare(
        self, kind: str, old_path: str, before: str | None, path: str, after: str
    ) -> list[Notice]:
        if kind == "npm":
            previous = _npm_packages(before) if before is not None else set()
            return self.npm(_npm_packages(after) - previous)
        scan = self.methods[kind]
        previous = scan(old_path, before) if before is not None else []
        return new_notices(previous, scan(path, after))

    def npm(self, packages: set[tuple[str, str]]) -> list[Notice]:
        findings = []
        for name, version in sorted(packages):
            key = (name, version)
            if key not in self.npm_cache:
                if len(self.npm_cache) >= 100:
                    self.warnings.add("npm lookup limit reached (100 exact versions per check).")
                    break
                try:
                    record = request_json(
                        self.client,
                        "GET",
                        f"{NPM}/{quote(name, safe='@')}/{quote(version)}",
                        missing_ok=True,
                    )
                except UpstreamError:
                    self.warnings.add("Some npm registry lookups failed.")
                    record = None
                if record is None:
                    self.warnings.add("Some public npm versions could not be verified.")
                message = record.get("deprecated") if isinstance(record, dict) else None
                self.npm_cache[key] = message if isinstance(message, str) else None
            if message := self.npm_cache[key]:
                findings.append(
                    Notice(
                        f"npm:{name}@{version}",
                        1,
                        "Deprecated npm package",
                        f"`{name}@{version}` — {message[:1200]}",
                        f"https://www.npmjs.com/package/{quote(name, safe='@/')}"
                        f"/v/{quote(version)}",
                    )
                )
        return findings

    def _action_notice(self, runtime: str, line: int, ref: str) -> Notice | None:
        record = retired_runtimes().get(runtime)
        if record and date.fromisoformat(record["date"]) <= self.today:
            return Notice(
                f"action:{runtime}" if "@" not in ref else f"action:{ref}:{runtime}",
                line,
                "Retired GitHub Actions runtime",
                f"`{ref}` declares `{runtime}`. GitHub retired it on {record['date']}. "
                f"{record['guidance']}",
                record["source"],
            )
        return None

    def action(self, path: str, source: str) -> list[Notice]:
        runtime = _runtime(source)
        if runtime and (
            notice := self._action_notice(runtime.value, runtime.start_mark.line + 1, path)
        ):
            return [notice]
        return []

    def workflow(self, path: str, source: str) -> list[Notice]:
        notices = []
        for ref in _workflow_refs(source):
            value = ref.value
            if value not in self.action_cache:
                if len(self.action_cache) >= 25:
                    self.warnings.add("External action lookup limit reached (25 refs per check).")
                    break
                try:
                    metadata = self.action_metadata(value)
                    runtime = _runtime(metadata) if metadata is not None else None
                    self.action_cache[value] = runtime.value if runtime else None
                    if metadata is None:
                        self.warnings.add("Some external action metadata was unavailable.")
                except UpstreamError:
                    self.warnings.add("Some external action metadata could not be read.")
                    self.action_cache[value] = None
            runtime_name = self.action_cache[value]
            if runtime_name and (
                notice := self._action_notice(runtime_name, ref.start_mark.line + 1, value)
            ):
                notices.append(notice)
        return notices

    def python(self, path: str, source: str) -> list[Notice]:
        notices = []
        for finding in analyze_source(source, path, today=self.today).findings:
            if finding.rule_id == "python-analysis":
                self.warnings.add("Some Python files could not be analyzed.")
                continue
            if finding.span and finding.documentation:
                key = f"python:{finding.rule_id}:{finding.evidence or finding.message}"
                notices.append(
                    Notice(
                        key,
                        finding.span.line,
                        finding.rule_id,
                        finding.message,
                        finding.documentation,
                    )
                )
        return notices
