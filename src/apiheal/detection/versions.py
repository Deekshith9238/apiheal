"""Relate findings to the package versions a repository declares.

Only requirements*.txt files and PEP 621 pyproject.toml dependencies are read.
Lock files and other layouts leave the version unknown rather than guessed.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from apiheal.core.models import Finding, RepositorySnapshot
from apiheal.detection.inventory import safe_path

# Bounds standing in for "every earlier/future version" of a package.
_EARLIEST = Version("0")
_FUTURE = Version("99999")


@dataclass(frozen=True)
class VersionedChange:
    package: str
    deprecated_since: Version | None = None
    removed_in: Version | None = None


CHANGES = {
    # @deprecated(since="3.2.0", removal="4.0.0") on all three classes in
    # langchain_google_vertexai/{chat_models,embeddings,llms}.py.
    "langchain-vertexai-deprecated": VersionedChange(
        "langchain-google-vertexai", Version("3.2.0"), Version("4.0.0")
    ),
    # The whole package is past end of support; versions only add evidence.
    "gemini-old-sdk": VersionedChange("google-generativeai"),
}


@dataclass(frozen=True)
class Declaration:
    file: str
    specifier: SpecifierSet

    def describe(self) -> str:
        return f"{self.specifier or 'unpinned'} in {self.file}"


def declared_requirements(root: Path, snapshot: RepositorySnapshot) -> dict[str, list[Declaration]]:
    declared: dict[str, list[Declaration]] = {}
    for relative in sorted(snapshot.manifests):
        name = Path(relative).name
        try:
            text = safe_path(root, relative).read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        if name == "pyproject.toml":
            entries = _pyproject_entries(text)
        elif name.startswith("requirements") and name.endswith(".txt"):
            entries = _requirements_entries(text)
        else:
            continue
        for entry in entries:
            try:
                requirement = Requirement(entry)
            except InvalidRequirement:
                continue
            if requirement.url:
                continue
            declared.setdefault(canonicalize_name(requirement.name), []).append(
                Declaration(relative, requirement.specifier)
            )
    return declared


def _requirements_entries(text: str) -> list[str]:
    entries = []
    for line in text.splitlines():
        content = re.split(r"(^|\s)#", line, maxsplit=1)[0].strip()
        # Options, includes, and editable installs do not name a plain requirement.
        if content and not content.startswith("-"):
            entries.append(content)
    return entries


def _pyproject_entries(text: str) -> list[str]:
    try:
        project = tomllib.loads(text).get("project", {})
    except tomllib.TOMLDecodeError:
        return []
    entries = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        entries.extend(group)
    return [entry for entry in entries if isinstance(entry, str)]


def _reachable(specifier: SpecifierSet, change: VersionedChange) -> list[Version]:
    candidates = {_EARLIEST, _FUTURE}
    candidates.update(v for v in (change.deprecated_since, change.removed_in) if v)
    for spec in specifier:
        try:
            candidates.add(Version(spec.version.removesuffix(".*")))
        except InvalidVersion:
            continue
    return sorted(v for v in candidates if specifier.contains(v, prereleases=True))


def apply_versions(findings: list[Finding], declared: dict[str, list[Declaration]]) -> None:
    for finding in findings:
        change = CHANGES.get(finding.rule_id)
        if not change:
            continue
        declarations = declared.get(canonicalize_name(change.package), [])
        if not declarations:
            finding.confidence = "medium"
            finding.evidence.append(
                f"No requirements*.txt or pyproject.toml declares {change.package}; "
                "the installed version is unknown and the import fails if it is absent."
            )
            continue
        finding.evidence.extend(f"Declared {change.package}: {d.describe()}" for d in declarations)
        if not change.deprecated_since:
            continue
        reachable = sorted({v for d in declarations for v in _reachable(d.specifier, change)})
        if not reachable:
            continue
        lowest, highest = reachable[0], reachable[-1]
        removed = change.removed_in
        if removed and lowest >= removed:
            finding.severity = "error"
            finding.message += (
                f" Every declared version is {removed} or later, where it is removed."
            )
        elif removed and highest >= removed:
            finding.message += (
                f" The declared constraint allows {removed} or later, where the import fails; "
                f"pin below {removed} or migrate."
            )
        elif highest < change.deprecated_since:
            finding.severity = "info"
            finding.message += (
                f" The declared version predates the {change.deprecated_since} deprecation; "
                "upgrading will deprecate it."
            )
