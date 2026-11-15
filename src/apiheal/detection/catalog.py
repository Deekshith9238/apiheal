"""Validated, declarative rules for matching imported Python APIs."""

from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Literal

from packaging.version import InvalidVersion, Version
from pydantic import Field, ValidationError, model_validator

from apiheal.core.errors import InputError
from apiheal.core.models import Record


class DependencyChange(Record):
    package: str = Field(min_length=1)
    deprecated_since: str | None = None
    removed_in: str | None = None

    @model_validator(mode="after")
    def validate_versions(self):
        try:
            deprecated = Version(self.deprecated_since) if self.deprecated_since else None
            removed = Version(self.removed_in) if self.removed_in else None
        except InvalidVersion as exc:
            raise ValueError("invalid dependency version") from exc
        if removed and (deprecated is None or removed <= deprecated):
            raise ValueError("removed_in requires an earlier deprecated_since")
        return self


class Selector(Record):
    kind: Literal["call", "module", "symbol"]
    module: str = Field(pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
    name: str | None = Field(default=None, pattern=r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
    submodules: bool = False
    message: str = Field(min_length=1)
    evidence: str | None = None

    @model_validator(mode="after")
    def validate_selector(self):
        if (self.kind == "module") != (self.name is None):
            raise ValueError("call and symbol selectors require a name; module selectors do not")
        return self

    def matches(self, qualified_name: str) -> bool:
        if self.name is None:
            module = qualified_name
        else:
            suffix = f".{self.name}"
            if not qualified_name.endswith(suffix):
                return False
            module = qualified_name.removesuffix(suffix)
        return module == self.module or (self.submodules and module.startswith(f"{self.module}."))


class SDKRule(Record):
    id: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    category: str = Field(min_length=1)
    documentation: str = Field(pattern=r"^https://\S+$")
    severity: Literal["info", "warning", "error"] = "warning"
    dependency: DependencyChange | None = None
    selectors: list[Selector] = Field(min_length=1)


class RulePack(Record):
    schema_version: Literal[1]
    rules: list[SDKRule] = Field(min_length=1)


@cache
def builtin_rules() -> tuple[SDKRule, ...]:
    data = files("apiheal.detection").joinpath("data/sdk_rules.json").read_bytes()
    return tuple(RulePack.model_validate_json(data).rules)


def load_rules(paths: tuple[Path, ...]) -> tuple[SDKRule, ...]:
    rules = list(builtin_rules())
    ids = {"model-retired", "python-analysis", *(rule.id for rule in rules)}
    for path in paths:
        try:
            with path.open("rb") as handle:
                data = handle.read(1_000_001)
            if len(data) > 1_000_000:
                raise InputError(f"Rule pack exceeds 1 MB: {path}")
            pack = RulePack.model_validate_json(data)
        except OSError as exc:
            raise InputError(f"Could not read rule pack: {path}") from exc
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                for error in exc.errors(include_input=False)[:3]
            )
            raise InputError(f"Invalid rule pack {path}: {details}") from exc
        for rule in pack.rules:
            if rule.id in ids:
                raise InputError(f"Duplicate or reserved rule ID {rule.id!r} in {path}")
            ids.add(rule.id)
            rules.append(rule)
    return tuple(rules)
