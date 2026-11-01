"""Git operations never invoke a shell or repository hooks."""

import os
import re
import subprocess
from pathlib import Path

from apiheal.core.errors import InputError
from apiheal.telemetry.redaction import redact_text


def git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0"
    )
    command = [
        "git",
        "--no-optional-locks",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "core.attributesFile=/dev/null",
    ]
    try:
        # Worktree checkout and even status may invoke attribute clean/smudge
        # filters from local config. Enumerate keys (never values) without
        # executing repository code, then disable every command-bearing driver.
        configured = subprocess.run(
            [
                *command,
                "-C",
                str(root),
                "config",
                "--null",
                "--name-only",
                "--get-regexp",
                r"^filter\.",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=environment,
            check=False,
        )
        if configured.returncode not in {0, 1}:
            raise InputError("Cannot safely inspect repository Git configuration.")
        for key in configured.stdout.split("\0"):
            if not key:
                continue
            if not re.fullmatch(r"filter\.[^\s=\x00-\x1f]+\.[a-zA-Z]+", key):
                raise InputError("Repository has an unsafe Git filter configuration key.")
            suffix = key.rsplit(".", 1)[-1].lower()
            if suffix in {"clean", "smudge", "process", "required"}:
                command.extend(["-c", f"{key}={'false' if suffix == 'required' else ''}"])
        command.extend(["-C", str(root), *arguments])
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError("Git is unavailable or the operation timed out.") from exc
    if check and result.returncode:
        raise InputError(redact_text(result.stderr.strip() or "Git operation failed."))
    return result


def repository_root(path: Path) -> Path:
    if not path.is_dir():
        raise InputError("Repository path must be an existing directory.")
    result = git(path.resolve(), "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def head(root: Path) -> str | None:
    result = git(root, "rev-parse", "--verify", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def is_dirty(root: Path) -> bool:
    return bool(git(root, "status", "--porcelain=v1", "--untracked-files=normal").stdout)
