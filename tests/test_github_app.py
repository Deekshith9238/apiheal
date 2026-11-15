import asyncio
import base64
import hashlib
import hmac
import json
from datetime import date

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("jwt")
pytest.importorskip("uvicorn")

from apiheal.github_app.checks import Credentials, PullRequestScan, publish_check  # noqa: E402
from apiheal.github_app.detectors import Detectors  # noqa: E402
from apiheal.github_app.webhook import Webhook  # noqa: E402

BASE, MERGE, HEAD = "a" * 40, "b" * 40, "c" * 40
REPO = "/repos/example/project"
WORKFLOW = ".github/workflows/build.yml"


def lock(*names):
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                f"node_modules/{name}": {
                    "version": "1.0.0",
                    "resolved": f"https://registry.npmjs.org/{name}/-/{name}-1.0.0.tgz",
                }
                for name in names
            },
        }
    )


def contents(text):
    data = text.encode()
    return {
        "type": "file",
        "size": len(data),
        "encoding": "base64",
        "content": base64.b64encode(data).decode(),
    }


def test_github_check_combines_three_detectors_and_excludes_existing_findings(monkeypatch):
    monkeypatch.setattr("apiheal.github_app.checks.jwt.encode", lambda *args, **kw: "app-jwt")
    job = PullRequestScan(
        installation=1, repository_id=2, repository="example/project", head=HEAD, base=BASE
    )
    existing = "from google import generativeai\n"
    paths = {
        "package-lock.json": (lock("old"), lock("old", "new-deprecated")),
        WORKFLOW: ("jobs: {}\n", "jobs:\n  job:\n    steps:\n      - uses: a/action@v1\n"),
        "app.py": (existing, "\n" + existing + "from langchain_google_vertexai import VertexAI\n"),
    }
    updates = []
    registry_calls = []

    def handler(request):
        path = request.url.path
        if request.url.host == "registry.npmjs.org":
            assert "authorization" not in request.headers
            registry_calls.append(path)
            return httpx.Response(200, json={"deprecated": "Use the replacement package."})
        if path == "/app/installations/1/access_tokens":
            assert request.headers["Authorization"] == "Bearer app-jwt"
            assert json.loads(request.content)["repository_ids"] == [2]
            return httpx.Response(201, json={"token": "installation-token"})
        if path == "/repos/a/action/contents/action.yml":
            assert "authorization" not in request.headers
            assert request.url.params["ref"] == "v1"
            return httpx.Response(200, json=contents("runs:\n  using: node16\n"))
        assert request.headers["Authorization"] == "Bearer installation-token"
        if path == f"{REPO}/commits/{HEAD}/check-runs":
            return httpx.Response(200, json={"check_runs": []})
        if path == f"{REPO}/check-runs":
            assert json.loads(request.content)["head_sha"] == HEAD
            return httpx.Response(201, json={"id": 3})
        if path == f"{REPO}/compare/{BASE}...{HEAD}":
            return httpx.Response(
                200,
                json={
                    "merge_base_commit": {"sha": MERGE},
                    "files": [{"filename": name, "status": "modified"} for name in paths],
                },
            )
        if path == f"{REPO}/check-runs/3":
            updates.append(json.loads(request.content))
            return httpx.Response(200, json={})
        name = path.removeprefix(f"{REPO}/contents/")
        ref = request.url.params["ref"]
        assert ref in {MERGE, HEAD}
        return httpx.Response(200, json=contents(paths[name][ref == HEAD]))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        publish_check(client, Credentials("1", "key"), job)
    assert registry_calls == ["/new-deprecated/1.0.0"]
    (update,) = updates
    assert update["status"] == "completed"
    assert update["conclusion"] == "neutral"
    findings = update["output"]["annotations"]
    assert [(f["path"], f["start_line"]) for f in findings] == [
        ("package-lock.json", 1),
        (WORKFLOW, 4),
        ("app.py", 3),
    ]
    assert all("Source: https://" in f["message"] for f in findings)


def test_registry_outage_is_visible_and_private_resolution_is_skipped():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(503)

    private = json.loads(lock("public", "private"))
    private["packages"]["node_modules/private"]["resolved"] = "https://private.example/pkg.tgz"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        detectors = Detectors(client, lambda ref: None, date(2026, 11, 13))
        assert detectors.compare("npm", "", None, "package-lock.json", json.dumps(private)) == []
    assert requests == ["/public/1.0.0"]
    assert detectors.warnings


def test_webhook_verifies_signature_before_enqueueing():
    accepted = []
    webhook = Webhook("secret", lambda job: accepted.append(job) or True)
    payload = json.dumps(
        {
            "action": "opened",
            "installation": {"id": 1},
            "repository": {"id": 2, "full_name": "example/project"},
            "pull_request": {"head": {"sha": HEAD}, "base": {"sha": BASE}},
        }
    ).encode()

    async def post(body, signature):
        transport = httpx.ASGITransport(app=webhook)
        async with httpx.AsyncClient(transport=transport, base_url="http://app") as client:
            return await client.post(
                "/webhooks/github",
                content=body,
                headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "pull_request"},
            )

    signature = "sha256=" + hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
    assert asyncio.run(post(payload + b" ", signature)).status_code == 401
    assert asyncio.run(post(payload, signature)).status_code == 202
    assert [job.head for job in accepted] == [HEAD]
