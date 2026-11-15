"""Compare immutable Git revisions and publish a GitHub Check."""

import base64
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx
import jwt
from pydantic import BaseModel, Field

from apiheal.github_app.detectors import Detectors, scan_kind
from apiheal.github_app.transport import UpstreamError, request_json

API = "https://api.github.com"
CHECK_NAME = "apiheal / Deprecations"
MAX_FILES = 80
MAX_FILE_BYTES = 1_000_000
ANNOTATIONS_PER_REQUEST = 50
MAX_ANNOTATIONS = 200


class PullRequestScan(BaseModel):
    installation: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    repository: str = Field(pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")
    head: str = Field(pattern=r"^[0-9a-f]{40}$")
    base: str = Field(pattern=r"^[0-9a-f]{40}$")

    @classmethod
    def from_webhook(cls, payload):
        return cls(
            installation=payload["installation"]["id"],
            repository_id=payload["repository"]["id"],
            repository=payload["repository"]["full_name"],
            head=payload["pull_request"]["head"]["sha"],
            base=payload["pull_request"]["base"]["sha"],
        )


@dataclass(frozen=True)
class Credentials:
    app_id: str
    private_key: str

    def token(self, client: httpx.Client, job: PullRequestScan) -> str:
        now = int(time.time())
        app_jwt = jwt.encode(
            {"iss": self.app_id, "iat": now - 60, "exp": now + 540},
            self.private_key,
            algorithm="RS256",
        )
        result = request_json(
            client,
            "POST",
            f"{API}/app/installations/{job.installation}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
            json={
                "repository_ids": [job.repository_id],
                "permissions": {"contents": "read", "checks": "write"},
            },
        )
        return result["token"]


class GitHub:
    def __init__(self, client: httpx.Client, repository: str, token: str):
        self.client = client
        self.repository = repository
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    def request(self, method: str, route: str, **kwargs):
        return request_json(
            self.client,
            method,
            f"{API}/repos/{self.repository}/{route}",
            headers=self.headers,
            **kwargs,
        )

    def read_file(
        self, path: str, ref: str, *, repository: str | None = None, missing_ok: bool = False
    ) -> str | None:
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise UpstreamError("Unsupported repository path.")
        external = repository is not None
        record = request_json(
            self.client,
            "GET",
            f"{API}/repos/{repository or self.repository}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
            headers=None if external else self.headers,
            missing_ok=missing_ok,
        )
        if record is None:
            return None
        if (
            not isinstance(record, dict)
            or record.get("type") != "file"
            or record.get("encoding") != "base64"
            or not isinstance(record.get("size"), int)
            or record["size"] > MAX_FILE_BYTES
            or not isinstance(record.get("content"), str)
        ):
            raise UpstreamError("Unsupported file type, encoding or size.")
        try:
            return base64.b64decode(record["content"]).decode("utf-8-sig")
        except ValueError as exc:
            raise UpstreamError("File is not UTF-8 text.") from exc

    def action_metadata(self, reference: str) -> str | None:
        action, ref = reference.rsplit("@", 1)
        owner, repo, *parts = action.split("/")
        if any(part in {".", "..", ""} for part in (owner, repo, *parts)):
            return None
        for name in ("action.yml", "action.yaml"):
            text = self.read_file(
                "/".join([*parts, name]),
                ref,
                repository=f"{owner}/{repo}",
                missing_ok=True,
            )
            if text is not None:
                return text
        return None


def _ignored(path: str) -> bool:
    return any(
        part in {"node_modules", "vendor", ".venv", "dist", "build"}
        for part in PurePosixPath(path).parts[:-1]
    )


def scan_changes(github: GitHub, job: PullRequestScan) -> tuple[list[dict], set[str], int]:
    comparison = github.request("GET", f"compare/{job.base}...{job.head}", params={"per_page": 1})
    if not isinstance(comparison, dict) or not isinstance(comparison.get("files"), list):
        raise UpstreamError("GitHub comparison did not contain a file list.")
    entries = comparison["files"]
    merge_base = comparison["merge_base_commit"]["sha"]
    detectors = Detectors(github.client, github.action_metadata, datetime.now(UTC).date())
    annotations = []
    scanned = 0
    for item in entries:
        path = item["filename"]
        kind = scan_kind(path)
        if item["status"] == "removed" or kind is None or _ignored(path):
            continue
        if scanned == MAX_FILES:
            detectors.warnings.add(f"Changed-file limit reached ({MAX_FILES} supported files).")
            break
        scanned += 1
        try:
            head = github.read_file(path, job.head)
            previous = item.get("previous_filename", path)
            before = None
            if item["status"] != "added" and scan_kind(previous) == kind:
                before = github.read_file(previous, merge_base)
            for notice in detectors.compare(kind, previous, before, path, head):
                annotations.append(notice.annotation(path))
        except UpstreamError:
            detectors.warnings.add("Some supported files or their baselines could not be scanned.")
    if len(entries) >= 300:
        detectors.warnings.add("GitHub returned its maximum of 300 changed files.")
    return annotations, detectors.warnings, scanned


def publish_check(client: httpx.Client, credentials: Credentials, job: PullRequestScan) -> None:
    github = GitHub(client, job.repository, credentials.token(client, job))
    reference = f"{job.base}:{job.head}"
    previous = github.request(
        "GET",
        f"commits/{job.head}/check-runs",
        params={"check_name": CHECK_NAME, "filter": "all", "per_page": 100},
    )
    for check in previous["check_runs"]:
        if (
            check["external_id"] == reference
            and str((check.get("app") or {}).get("id")) == credentials.app_id
        ):
            if check["status"] == "completed":
                return
            check_id = check["id"]
            break
    else:
        created = github.request(
            "POST",
            "check-runs",
            json={
                "name": CHECK_NAME,
                "head_sha": job.head,
                "status": "in_progress",
                "external_id": reference,
            },
        )
        check_id = created["id"]

    route = f"check-runs/{check_id}"
    try:
        annotations, warnings, scanned = scan_changes(github, job)
    except UpstreamError:
        annotations, warnings, scanned = [], {"GitHub comparison could not be read."}, 0
    summary = f"Scanned {scanned} changed files. Found {len(annotations)} new notices."
    if len(annotations) > MAX_ANNOTATIONS:
        warnings.add(f"Only the first {MAX_ANNOTATIONS} findings are shown.")
    if warnings:
        summary += "\n\nCoverage limits\n" + "\n".join(
            f"- {warning}" for warning in sorted(warnings)
        )

    visible = annotations[:MAX_ANNOTATIONS]
    conclusion = "neutral" if annotations or warnings else "success"
    for start in range(0, max(1, len(visible)), ANNOTATIONS_PER_REQUEST):
        batch = visible[start : start + ANNOTATIONS_PER_REQUEST]
        payload = {
            "output": {"title": "Deprecation scan", "summary": summary, "annotations": batch}
        }
        if start + ANNOTATIONS_PER_REQUEST >= len(visible):
            payload.update(status="completed", conclusion=conclusion)
        github.request("PATCH", route, json=payload)
