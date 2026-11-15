"""Receive signed GitHub deliveries and run scans in a bounded background worker."""

import hashlib
import hmac
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import BoundedSemaphore

import httpx
import uvicorn

from apiheal.github_app.checks import Credentials, PullRequestScan, publish_check

LOG = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1_000_000
PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


async def _reply(send, status: int) -> None:
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": b""})


class Webhook:
    def __init__(self, secret: str, enqueue):
        self.secret = secret.encode()
        self.enqueue = enqueue

    async def __call__(self, scope, receive, send):
        if scope["path"] == "/healthz" and scope["method"] == "GET":
            return await _reply(send, 200)
        if scope["path"] != "/webhooks/github" or scope["method"] != "POST":
            return await _reply(send, 404)
        headers = dict(scope["headers"])
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] != "http.request":
                return
            body.extend(event.get("body", b""))
            if len(body) > MAX_WEBHOOK_BYTES:
                return await _reply(send, 413)
            if not event.get("more_body"):
                break

        expected = b"sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest().encode()
        if not hmac.compare_digest(headers.get(b"x-hub-signature-256", b""), expected):
            return await _reply(send, 401)
        if headers.get(b"x-github-event") != b"pull_request":
            return await _reply(send, 204)

        try:
            payload = json.loads(body)
            if payload["action"] not in PR_ACTIONS:
                return await _reply(send, 204)
            job = PullRequestScan.from_webhook(payload)
        except (ValueError, KeyError, TypeError, RecursionError):
            return await _reply(send, 400)
        return await _reply(send, 202 if self.enqueue(job) else 503)


class Worker:
    def __init__(self, credentials: Credentials):
        self.credentials = credentials
        self.capacity = BoundedSemaphore(16)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apiheal-check")

    def enqueue(self, job: PullRequestScan) -> bool:
        if not self.capacity.acquire(blocking=False):
            return False
        self.executor.submit(self.run, job)
        return True

    def run(self, job: PullRequestScan) -> None:
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                publish_check(client, self.credentials, job)
        except Exception as exc:
            LOG.error("Scan failed for %s at %s (%s)", job.repository, job.head, type(exc).__name__)
        finally:
            self.capacity.release()


def main() -> None:
    required = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_FILE", "GITHUB_WEBHOOK_SECRET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
    credentials = Credentials(
        os.environ["GITHUB_APP_ID"],
        Path(os.environ["GITHUB_APP_PRIVATE_KEY_FILE"]).read_text(encoding="utf-8"),
    )
    worker = Worker(credentials)
    try:
        uvicorn.run(
            Webhook(os.environ["GITHUB_WEBHOOK_SECRET"], worker.enqueue),
            host=os.environ.get("APIHEAL_APP_HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            lifespan="off",
        )
    finally:
        worker.executor.shutdown()
