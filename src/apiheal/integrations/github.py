"""GitHub Git Data API publisher: no repository hooks, pushes, or automatic merge."""

import base64
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

from apiheal.core.errors import PolicyError, PublicationError
from apiheal.core.models import Publication, RunResult
from apiheal.core.policy import Policy, required_checks_pass
from apiheal.core.run_store import RunStore
from apiheal.telemetry.redaction import redact_text
from apiheal.verification.container import harness_digest
from apiheal.verification.process import environment, run_bounded
from apiheal.verification.runner import REQUIRED_GATES
from apiheal.verification.snapshot import snapshot_digest, tracked_files


def github_repository(remote: str | None) -> str:
    remote = remote or ""
    if remote.startswith("git@github.com:"):
        path = remote[len("git@github.com:") :]
    else:
        try:
            parsed = urlsplit(remote)
            port = parsed.port
        except ValueError as error:
            raise PublicationError("Origin has an invalid URL.") from error
        if (
            parsed.hostname != "github.com"
            or parsed.query
            or parsed.fragment
            or port
            or parsed.password
            or (parsed.scheme == "https" and parsed.username)
            or parsed.scheme not in {"https", "ssh"}
            or (parsed.scheme == "ssh" and parsed.username != "git")
        ):
            raise PublicationError(
                "Publishing requires an origin on github.com without embedded tokens."
            )
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", path) or path.split("/")[
        -1
    ] in {".", ".."}:
        raise PublicationError("Origin must identify one GitHub OWNER/REPO repository.")
    return path


class GitHubAPI:
    def __init__(self):
        self.executable = shutil.which("gh")

    def request(self, endpoint: str, *, data: dict | None = None, missing_ok: bool = False):
        if not self.executable:
            raise PublicationError(
                "GitHub CLI is unavailable. Install gh and authenticate it first."
            )
        with tempfile.TemporaryDirectory(prefix="apiheal-github-") as directory:
            cwd = Path(directory)
            command = [
                self.executable,
                "api",
                "--hostname",
                "github.com",
                "--method",
                "GET" if data is None else "POST",
                "--header",
                "Accept: application/vnd.github+json",
                "--header",
                "X-GitHub-Api-Version: 2026-03-10",
                endpoint,
            ]
            if data is not None:
                payload = cwd / "request.json"
                payload.write_text(json.dumps(data), encoding="utf-8")
                command.extend(["--input", str(payload)])
            try:
                completed = run_bounded(command, cwd=cwd, env=environment(github=True), timeout=30)
            except OSError as error:
                raise PublicationError("GitHub CLI could not be started.") from error
            if completed.interrupted:
                raise PublicationError(
                    "GitHub request timed out or exceeded output limits; retry safely."
                )
            if completed.returncode:
                if missing_ok and re.search(r"\bHTTP 404\b", completed.stderr):
                    return None
                raise PublicationError(
                    redact_text(completed.stderr[-1500:] or "GitHub API request failed.")
                )
            try:
                payload = json.loads(completed.stdout)
                expected_list = data is None and endpoint.split("?", 1)[0].endswith("/pulls")
                if not isinstance(payload, list if expected_list else dict):
                    raise ValueError
                return payload
            except ValueError as error:
                raise PublicationError("GitHub returned an unreadable response.") from error


def assert_verified(candidate: Path, result: RunResult) -> None:
    evidence = result.verification
    if (
        evidence is None
        or result.plan is None
        or result.patch is None
        or result.outcome not in {"verified", "pr_opened"}
        or not REQUIRED_GATES <= set(result.plan.required_checks)
        or not required_checks_pass(result.checks, set(result.plan.required_checks))
        or evidence.base_commit != result.patch.base_commit
        or evidence.patch_sha256 != result.patch.sha256
        or evidence.harness_sha256 != harness_digest()
        or evidence.policy_sha256 != hashlib.sha256(Policy().model_dump_json().encode()).hexdigest()
        or evidence.snapshot_sha256 != snapshot_digest(candidate)
    ):
        raise PolicyError(
            "Publishing requires current passing checks bound to this exact candidate."
        )


def pr_body(result: RunResult) -> str:
    rows = "\n".join(
        f"| {check.gate} | {check.outcome} | "
        f"{check.test_count if check.test_count is not None else '—'} |"
        for check in result.checks
    )
    evidence = result.verification
    details = "No completed verification evidence. Publishing is blocked."
    if evidence:
        details = (
            f"Candidate snapshot: `{evidence.snapshot_sha256}`\n\n"
            f"Harness: `{evidence.harness_sha256}`\n\n"
            + "\n".join(
                f"- OpenAI {version}: `{image}`" for version, image in evidence.images.items()
            )
            + (
                "\n\nNo repository unit suite was present. The trusted SDK contract suite is the "
                "documented alternative check."
                if evidence.contract_only
                else ""
            )
        )
    return (
        "## Repair\n\n"
        "Migrate a supported legacy Python OpenAI chat call to a scoped client and typed "
        "response access, preserving its request parameters and model identifier.\n\n"
        "Before: `openai.ChatCompletion.create(...)` and dictionary response access.\n\n"
        "After: `client.chat.completions.create(...)` and `.choices[0].message.content`.\n\n"
        f"Target SDK: `{result.plan.target_sdk}`. "
        "[Upstream client SDK reference](https://developers.openai.com/api/reference/python).\n\n"
        "## Verification\n\n"
        "Offline loopback HTTP contract checks use the actual legacy/target SDKs. "
        "The original code must reproduce APIRemovedInV1 with the target SDK; "
        "the repaired request/response facts must match the legacy baseline. "
        "No live provider behavior is claimed.\n\n"
        "| Gate | Outcome | Cases |\n| --- | --- | ---: |\n"
        + rows
        + "\n\n"
        + details
        + f"\n\nRun: `{result.run_id}`\n\nBase: `{result.patch.base_commit}`\n\n"
        f"Patch SHA-256: `{result.patch.sha256}`\n\n"
        "Created automatically by apiheal. Human review is required before merge. "
        "Review application-specific behavior beyond the supported contract cases.\n"
    )


def _sha(value: dict, key: str = "sha") -> str:
    sha = value.get(key, "") if isinstance(value, dict) else ""
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise PublicationError("GitHub returned an invalid Git object identifier.")
    return sha


def publish(
    candidate: Path,
    result: RunResult,
    store: RunStore,
    *,
    base_branch: str | None = None,
    api: GitHubAPI | None = None,
) -> Publication:
    assert_verified(candidate, result)
    body = pr_body(result)
    store.write_text(result.run_id, "pr-body.md", body)
    result.artifacts["pr_body"] = "pr-body.md"
    repo = github_repository(result.repository.remote)
    api = api or GitHubAPI()
    metadata = api.request(f"repos/{repo}")
    base = base_branch or metadata.get("default_branch")
    if (
        not isinstance(base, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", base)
        or ".." in base
        or base.endswith(("/", ".", ".lock"))
    ):
        raise PublicationError("An explicit valid GitHub base branch is required.")
    base_endpoint = f"repos/{repo}/git/ref/heads/{quote(base, safe='')}"

    def check_base():
        if _sha(api.request(base_endpoint).get("object", {})) != result.patch.base_commit:
            raise PublicationError(
                "GitHub base has moved or differs from the baseline. Reverify its latest checkout."
            )

    check_base()
    commit = api.request(f"repos/{repo}/git/commits/{result.patch.base_commit}")
    tree_base = _sha(commit.get("tree", {}))
    modes = {item.path: item.mode for item in tracked_files(candidate)}
    entries = []
    for name in result.patch.changed_paths:
        data = (candidate / name).read_bytes()
        if redact_text(data.decode("utf-8")) != data.decode("utf-8"):
            raise PolicyError("Refusing to publish potential credentials.")
        expected_blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        blob = api.request(
            f"repos/{repo}/git/blobs",
            data={
                "content": base64.b64encode(data).decode(),
                "encoding": "base64",
            },
        )
        if _sha(blob) != expected_blob:
            raise PublicationError("GitHub blob differs from the verified file bytes.")
        entries.append({"path": name, "mode": modes[name], "type": "blob", "sha": expected_blob})
    tree = _sha(
        api.request(f"repos/{repo}/git/trees", data={"base_tree": tree_base, "tree": entries})
    )
    key = hashlib.sha256(
        f"{repo.lower()}:{result.patch.base_commit}:{result.plan.rule_id}:"
        f"{result.plan.rule_version}:{result.patch.sha256}".encode()
    ).hexdigest()
    branch = f"apiheal/{result.plan.rule_id}/{key[:20]}"
    branch_endpoint = f"repos/{repo}/git/ref/heads/{quote(branch, safe='')}"
    existing = api.request(branch_endpoint, missing_ok=True)
    reused = existing is not None
    if existing is None:
        made = api.request(
            f"repos/{repo}/git/commits",
            data={
                "message": f"apiheal: migrate supported OpenAI chat calls\n\nRepair key: {key}",
                "tree": tree,
                "parents": [result.patch.base_commit],
            },
        )
        commit_sha = _sha(made)
        check_base()
        assert_verified(candidate, result)
        try:
            api.request(
                f"repos/{repo}/git/refs",
                data={
                    "ref": f"refs/heads/{branch}",
                    "sha": commit_sha,
                },
            )
        except PublicationError:
            existing = api.request(branch_endpoint, missing_ok=True)
            if existing is None:
                raise
            reused = True
    if existing is not None:
        commit_sha = _sha(existing.get("object", {}))
        existing_commit = api.request(f"repos/{repo}/git/commits/{commit_sha}")
        parents = existing_commit.get("parents")
        if not isinstance(parents, list) or not all(isinstance(p, dict) for p in parents):
            raise PublicationError("Existing repair commit has invalid parent evidence.")
        if _sha(existing_commit.get("tree", {})) != tree or [
            parent.get("sha") for parent in existing_commit.get("parents", [])
        ] != [result.patch.base_commit]:
            raise PublicationError(
                "Repair branch conflicts with unrelated changes; it was not overwritten."
            )
    pulls_endpoint = (
        f"repos/{repo}/pulls?state=all&head={quote(repo.split('/')[0] + ':' + branch, safe='')}"
        f"&base={quote(base, safe='')}"
    )

    def existing_pr():
        matches = api.request(pulls_endpoint)
        if not isinstance(matches, list) or len(matches) > 1:
            raise PublicationError("Ambiguous existing pull requests; manual review is required.")
        if not matches:
            return None
        pull = matches[0]
        if (
            not isinstance(pull, dict)
            or not isinstance(pull.get("head"), dict)
            or not isinstance(pull.get("base"), dict)
            or pull.get("state") != "open"
            or pull.get("head", {}).get("sha") != commit_sha
            or pull.get("base", {}).get("ref") != base
        ):
            raise PublicationError(
                "An existing repair PR was closed or changed; no duplicate was opened."
            )
        return pull

    pull = existing_pr()
    if pull is None:
        assert_verified(candidate, result)
        check_base()
        if _sha(api.request(branch_endpoint).get("object", {})) != commit_sha:
            raise PublicationError("Repair branch changed before PR creation; publishing stopped.")
        try:
            pull = api.request(
                f"repos/{repo}/pulls",
                data={
                    "title": "apiheal: migrate legacy OpenAI chat calls",
                    "head": branch,
                    "base": base,
                    "body": body,
                    "draft": True,
                    "maintainer_can_modify": False,
                },
            )
        except PublicationError:
            pull = existing_pr()
            if pull is None:
                raise
            reused = True
    else:
        reused = True
    if (
        pull.get("head", {}).get("sha") != commit_sha
        or pull.get("base", {}).get("ref") != base
        or pull.get("base", {}).get("sha") != result.patch.base_commit
    ):
        raise PublicationError(
            "PR head/base changed during publication. Inspect the draft before any further action."
        )
    url = pull.get("html_url", "")
    if not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[0-9]+", url, flags=re.I):
        raise PublicationError("GitHub did not return the expected pull-request URL.")
    return Publication(
        repository=repo,
        base_branch=base,
        branch=branch,
        commit=commit_sha,
        url=url,
        idempotency_key=key,
        reused=reused,
    )
