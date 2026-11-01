"""Docker-only execution with immutable images and no host execution fallback."""

import hashlib
import json
import re
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from apiheal.core.errors import ApihealError, VerificationError
from apiheal.repair.dependency_edit import TARGET_OPENAI
from apiheal.telemetry.redaction import redact_text
from apiheal.verification.process import ProcessResult, environment, run_bounded

LEGACY_OPENAI = "0.28.1"
VERSIONS = (LEGACY_OPENAI, TARGET_OPENAI)
ASSET_NAMES = ("Dockerfile", "harness.py", "pytest.ini")


def assets() -> dict[str, bytes]:
    root = files("apiheal.verification").joinpath("assets")
    return {name: root.joinpath(name).read_bytes() for name in ASSET_NAMES}


def harness_digest() -> str:
    digest = hashlib.sha256()
    for name, data in assets().items():
        digest.update(name.encode() + b"\0" + data)
    return digest.hexdigest()


def image_tag(version: str) -> str:
    if version not in VERSIONS:
        raise VerificationError("Unsupported SDK runner version.")
    return f"apiheal-runner:openai-{version}-v1"


class DockerRunner:
    def __init__(self):
        self.executable = shutil.which("docker")

    def command(self, arguments: list[str], *, timeout: float = 30) -> ProcessResult:
        if not self.executable:
            raise VerificationError(
                "Docker CLI is unavailable. Install/start Docker with Linux containers, "
                "then run 'apiheal setup-runner'. Target code was not run on the host."
            )
        try:
            return run_bounded(
                [self.executable, *arguments],
                timeout=timeout,
                env=environment(),
            )
        except OSError as exc:
            raise VerificationError("Docker could not be started.") from exc

    def check_available(self) -> None:
        result = self.command(["info", "--format", "{{json .}}"])
        try:
            info = json.loads(result.stdout)
            capabilities = {key.lower(): value for key, value in info.items()}
            if (
                result.interrupted
                or result.returncode
                or info.get("OSType") != "linux"
                or any(
                    capabilities.get(key) is not True
                    for key in (
                        "memorylimit",
                        "pidslimit",
                        "cpucfsquota",
                    )
                )
                or info.get("CgroupDriver", "none") == "none"
                or not any("seccomp" in option for option in info.get("SecurityOptions", []))
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise VerificationError(
                "Linux Docker with memory, PID, CPU, and seccomp limits is required."
            ) from None

    def resolve_image(self, version: str) -> str:
        result = self.command(["image", "inspect", image_tag(version)])
        try:
            if result.returncode or result.interrupted:
                raise ValueError
            inspected = json.loads(result.stdout)[0]
            labels = inspected["Config"]["Labels"]
            identity = inspected["Id"]
            if (
                not re.fullmatch(r"sha256:[a-f0-9]{64}", identity)
                or inspected.get("Os") != "linux"
                or labels.get("org.apiheal.sdk") != version
                or labels.get("org.apiheal.harness-sha256") != harness_digest()
            ):
                raise ValueError
            return identity
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise VerificationError(
                "Runner image is missing or outdated. Run 'apiheal setup-runner'."
            ) from exc

    def execute(self, image: str, snapshot: Path, request: dict) -> ProcessResult:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise VerificationError("Container execution requires an immutable image ID.")
        name = "apiheal-" + uuid4().hex
        with tempfile.TemporaryDirectory(prefix="apiheal-request-") as directory:
            inputs = Path(directory).resolve()
            request_path = inputs / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            request_path.chmod(0o644)
            inputs.chmod(0o755)
            snapshot = snapshot.resolve()
            if any(character in str(path) for path in (snapshot, inputs) for character in ",\r\n"):
                raise VerificationError(
                    "Docker mount paths containing commas or newlines are unsupported."
                )
            arguments = [
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                name,
                "--no-healthcheck",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--user",
                "65532:65532",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=67108864,mode=1777",
                "--mount",
                f"type=bind,source={snapshot},target=/workspace,readonly",
                "--mount",
                f"type=bind,source={inputs},target=/input,readonly",
                "--entrypoint",
                "python",
                "--env",
                "HTTP_PROXY=",
                "--env",
                "HTTPS_PROXY=",
                "--env",
                "ALL_PROXY=",
                "--env",
                "NO_PROXY=127.0.0.1,localhost",
                "--env",
                "http_proxy=",
                "--env",
                "https_proxy=",
                "--env",
                "all_proxy=",
                "--env",
                "no_proxy=127.0.0.1,localhost",
                image,
                "-I",
                "/opt/apiheal/harness.py",
            ]
            try:
                return self.command(arguments, timeout=60)
            finally:
                # A killed Docker CLI must never leave its target container running.
                self.command(["rm", "--force", name], timeout=10)


def setup_runner(runner: DockerRunner | None = None) -> dict[str, str]:
    runner = runner or DockerRunner()
    runner.check_available()
    with tempfile.TemporaryDirectory(prefix="apiheal-build-") as directory:
        context = Path(directory)
        for name, data in assets().items():
            (context / name).write_bytes(data)
        for version in VERSIONS:
            result = runner.command(
                [
                    "build",
                    "--pull",
                    "--build-arg",
                    f"OPENAI_SDK_VERSION={version}",
                    "--build-arg",
                    f"APIHEAL_HARNESS_SHA256={harness_digest()}",
                    "--tag",
                    image_tag(version),
                    str(context),
                ],
                timeout=300,
            )
            if result.returncode or result.interrupted:
                raise ApihealError(
                    "Runner image build failed or exceeded its limits: "
                    + redact_text(result.stderr[-1500:])
                )
    return {version: runner.resolve_image(version) for version in VERSIONS}
