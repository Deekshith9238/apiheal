"""Bounded subprocess capture for trusted Docker/gh command vectors."""

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0
    interrupted: bool = False


def environment(*, github: bool = False) -> dict[str, str]:
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMDATA",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    }
    if github:
        allowed |= {"GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if github:
        result.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat")
    return result


def run_bounded(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60,
    max_bytes: int = 1_000_000,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    if not command or not command[0]:
        raise ValueError("Command must be nonempty.")
    started = time.monotonic()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            shell=False,
        )
        interrupted = False
        try:
            while process.poll() is None:
                if (
                    time.monotonic() - started > timeout
                    or os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size
                    > max_bytes
                ):
                    interrupted = True
                    process.kill()
                    break
                time.sleep(0.02)
            process.wait(timeout=5)
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
        if os.fstat(output.fileno()).st_size + os.fstat(errors.fileno()).st_size > max_bytes:
            interrupted = True
        output.seek(0)
        errors.seek(0)
        return ProcessResult(
            returncode=process.returncode,
            stdout=output.read(max_bytes).decode("utf-8", errors="replace"),
            stderr=errors.read(max_bytes).decode("utf-8", errors="replace"),
            duration=time.monotonic() - started,
            interrupted=interrupted,
        )
