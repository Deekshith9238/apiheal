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

from apiheal.core.models import Finding, RepositorySnapshot
from apiheal.detection.catalog import SDKRule, builtin_rules
from apiheal.detection.inventory import safe_path


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
    if not isinstance(project, dict):
        return []
    dependencies = project.get("dependencies", [])
    entries = list(dependencies) if isinstance(dependencies, list) else []
    extras = project.get("optional-dependencies", {})
    if isinstance(extras, dict):
        for group in extras.values():
            if isinstance(group, list):
                entries.extend(group)
    return [entry for entry in entries if isinstance(entry, str)]


def apply_versions(
    findings: list[Finding],
    declared: dict[str, list[Declaration]],
    rules: tuple[SDKRule, ...] | None = None,
) -> None:
    changes = {
        rule.id: rule.dependency
        for rule in (rules if rules is not None else builtin_rules())
        if rule.dependency is not None
    }
    for finding in findings:
        change = changes.get(finding.rule_id)
        if not change:
            continue
        declarations = declared.get(canonicalize_name(change.package), [])
        if not declarations:
            if finding.confidence == "high":
                finding.confidence = "medium"
            finding.evidence.append(
                f"No requirements*.txt or pyproject.toml declares {change.package}; "
                "the installed version is unknown and the import fails if it is absent."
            )
            continue
        finding.evidence.extend(f"Declared {change.package}: {d.describe()}" for d in declarations)
        if not change.deprecated_since:
            continue
        deprecated = SpecifierSet(f">={change.deprecated_since}")
        removed = SpecifierSet(f">={change.removed_in}") if change.removed_in else None
        constraints = [declaration.specifier for declaration in declarations]
        try:
            satisfiable = [spec for spec in constraints if not spec.is_unsatisfiable()]
            if not satisfiable:
                continue
            before_deprecation = all(spec.is_disjoint(deprecated) for spec in satisfiable)
            always_removed = removed and all(spec.is_subset(removed) for spec in satisfiable)
            allows_removal = removed and any(not spec.is_disjoint(removed) for spec in satisfiable)
        except ValueError:
            # Arbitrary equality (===) cannot be classified by PEP 440 range relations.
            continue
        if always_removed:
            finding.severity = "error"
            finding.message += (
                f" Every declared version is {change.removed_in} or later, where it is removed."
            )
        elif allows_removal:
            finding.message += (
                f" The declared constraint allows {change.removed_in} or later, "
                f"where the import fails; pin below {change.removed_in} or migrate."
            )
        elif before_deprecation:
            finding.severity = "info"
            finding.message += (
                f" The declared version predates the {change.deprecated_since} deprecation; "
                "upgrading will deprecate it."
            )
