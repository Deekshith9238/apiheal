"""Conservative edits to one simple requirements.txt layout."""

import re

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

from apiheal.core.errors import PolicyError

# A historical, reproducible migration target; deliberately not a claim of latest.
TARGET_OPENAI = "1.109.1"


def migrate_requirements(source: str) -> str:
    lines = source.splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        if content.startswith("-") or "\\" in content:
            raise PolicyError(
                "Requirements options, includes, editable installs, and hashes need review."
            )
        requirement_text = re.split(r"\s+#", content, maxsplit=1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            raise PolicyError(
                "Unsupported requirements.txt entry; no dependency edit was made."
            ) from exc
        if requirement.url:
            raise PolicyError("URL/VCS dependencies are outside the initial dependency layout.")
        if requirement.name.lower().replace("_", "-") != "openai":
            continue
        if requirement.extras or requirement.marker:
            raise PolicyError("OpenAI extras and conditional requirements need review.")
        if str(requirement.specifier) != f"=={TARGET_OPENAI}":
            specs = list(requirement.specifier)
            legacy = Version("0.28.1")
            bounded_to_legacy = any(
                (
                    spec.operator in {"<", "<="}
                    and (
                        Version(spec.version) < Version("1.0")
                        or (spec.operator == "<" and Version(spec.version) == Version("1.0"))
                    )
                )
                or (spec.operator in {"==", "~="} and spec.version.startswith("0."))
                for spec in specs
            )
            if legacy not in requirement.specifier or not bounded_to_legacy:
                raise PolicyError(
                    "OpenAI requirement must constrain the legacy SDK or already pin the target; "
                    "unbounded, modern, or conflicting versions require review."
                )
        matches.append(index)
    if len(matches) != 1:
        raise PolicyError("Exactly one unconditional OpenAI requirement is required.")
    index = matches[0]
    line = lines[index]
    comment = re.search(r"\s+#.*?(?=\r?$)", line)
    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    indent = line[: len(line) - len(line.lstrip())]
    lines[index] = f"{indent}openai=={TARGET_OPENAI}{comment.group(0) if comment else ''}{ending}"
    return "".join(lines)
