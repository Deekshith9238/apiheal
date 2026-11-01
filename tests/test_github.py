import base64
import hashlib
import json
from urllib.parse import unquote

import pytest
from helpers import verified_run

from apiheal.core.errors import PolicyError, PublicationError
from apiheal.integrations.github import GitHubAPI, github_repository, publish
from apiheal.verification.process import ProcessResult


class FakeGitHub:
    """In-memory GitHub API; never contacts a real account."""

    def __init__(self, base):
        self.base = base
        self.calls = []
        self.refs = {}
        self.commits = {base: {"sha": base, "tree": {"sha": "a" * 40}, "parents": []}}
        self.pulls = []
        self.fail_after_create = False

    def request(self, endpoint, *, data=None, missing_ok=False):
        self.calls.append((endpoint, data))
        endpoint = unquote(endpoint)
        if endpoint == "repos/example/demo":
            return {"default_branch": "main"}
        relative = endpoint.removeprefix("repos/example/demo/")
        if relative == "git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if relative.startswith("git/ref/heads/"):
            branch = relative.removeprefix("git/ref/heads/")
            return {"object": {"sha": self.refs[branch]}} if branch in self.refs else None
        if relative.startswith("git/commits/"):
            return self.commits[relative.removeprefix("git/commits/")]
        if relative == "git/blobs":
            content = base64.b64decode(data["content"])
            return {
                "sha": hashlib.sha1(
                    b"blob " + str(len(content)).encode() + b"\0" + content
                ).hexdigest()
            }
        if relative == "git/trees":
            return {"sha": hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()}
        if relative == "git/commits":
            sha = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
            self.commits[sha] = {
                "sha": sha,
                "tree": {"sha": data["tree"]},
                "parents": [{"sha": parent} for parent in data["parents"]],
            }
            return {"sha": sha}
        if relative == "git/refs":
            branch = data["ref"].removeprefix("refs/heads/")
            assert branch not in self.refs, "Must never overwrite existing branches"
            self.refs[branch] = data["sha"]
            return {"object": {"sha": data["sha"]}}
        if relative.startswith("pulls?"):
            return self.pulls
        if relative == "pulls":
            assert data["draft"] is True
            pull = {
                "html_url": "https://github.com/example/demo/pull/1",
                "state": "open",
                "head": {"sha": self.refs[data["head"]]},
                "base": {"ref": data["base"], "sha": self.base},
            }
            self.pulls.append(pull)
            if self.fail_after_create:
                raise PublicationError("Simulated lost response after PR creation")
            return pull
        raise AssertionError(f"Unexpected API request: {endpoint}")


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/example/demo.git",
        "git@github.com:example/demo.git",
        "ssh://git@github.com/example/demo",
        "https://github.com/example/demo",
    ],
)
def test_github_origin_mapping(remote):
    assert github_repository(remote) == "example/demo"


@pytest.mark.parametrize(
    "remote",
    [
        None,
        "file:///tmp/repo",
        "https://other.invalid/example/demo",
        "https://github.com/example/..",
        "https://user:token@github.com/example/demo",
        "https://github.com/example/demo?key=token",
        "https://github.com:invalid/example/demo",
        "https://github.com/example/demo;echo-hi",
    ],
)
def test_unsafe_or_unsupported_origin_is_rejected(remote):
    with pytest.raises(PublicationError):
        github_repository(remote)


def test_publish_verified_bytes_and_retry_reuses_one_draft(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    api = FakeGitHub(result.patch.base_commit)
    first = publish(candidate, result, store, api=api)
    second = publish(candidate, result, store, api=api)
    assert first.url == second.url and second.reused
    assert len(api.pulls) == 1 and len(api.refs) == 1
    assert first.branch.startswith("apiheal/openai-legacy-completions/")
    assert (store.directory(result.run_id) / "pr-body.md").read_text().count("\n") > 10
    assert "Human review is required" in (store.directory(result.run_id) / "pr-body.md").read_text()


def test_lost_pr_response_is_reconciled_without_duplicate(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    api = FakeGitHub(result.patch.base_commit)
    api.fail_after_create = True
    published = publish(candidate, result, store, api=api)
    assert published.reused and len(api.pulls) == 1


def test_stale_remote_base_blocks_every_write(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    api = FakeGitHub("b" * 40)
    with pytest.raises(PublicationError, match="base has moved"):
        publish(candidate, result, store, api=api)
    assert all(data is None for _, data in api.calls)


def test_changed_candidate_cannot_publish(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    (candidate / "app.py").write_text("changed after verification\n")
    api = FakeGitHub(result.patch.base_commit)
    with pytest.raises(PolicyError, match="exact candidate"):
        publish(candidate, result, store, api=api)
    assert api.calls == []


@pytest.mark.parametrize("state", ["failed", "skipped", "indeterminate"])
def test_nonpassing_required_check_never_reaches_github(candidate_run, state):
    _, candidate, result, store, _ = verified_run(candidate_run)
    next(check for check in result.checks if check.gate == "sdk_contract").outcome = state
    api = FakeGitHub(result.patch.base_commit)
    with pytest.raises(PolicyError):
        publish(candidate, result, store, api=api)
    assert api.calls == []


def test_conflicting_branch_is_not_overwritten(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    api = FakeGitHub(result.patch.base_commit)
    published = publish(candidate, result, store, api=api)
    api.commits[published.commit]["tree"]["sha"] = "f" * 40
    original_ref = dict(api.refs)
    with pytest.raises(PublicationError, match="conflicts"):
        publish(candidate, result, store, api=api)
    assert api.refs == original_ref
    assert len(api.pulls) == 1


def test_closed_pr_is_not_reopened_or_duplicated(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    api = FakeGitHub(result.patch.base_commit)
    publish(candidate, result, store, api=api)
    api.pulls[0]["state"] = "closed"
    with pytest.raises(PublicationError, match="closed"):
        publish(candidate, result, store, api=api)
    assert len(api.pulls) == 1


def test_explicit_nondefault_base_is_used(candidate_run):
    _, candidate, result, store, _ = verified_run(candidate_run)
    api = FakeGitHub(result.patch.base_commit)
    api.refs["develop"] = api.base
    published = publish(candidate, result, store, api=api, base_branch="develop")
    assert published.base_branch == "develop"
    assert api.pulls[0]["base"]["ref"] == "develop"


def test_gh_payload_uses_json_file_and_is_outside_target(monkeypatch):
    from pathlib import Path

    api = GitHubAPI()
    api.executable = "gh"
    payload = {"body": "First paragraph.\n\nSecond paragraph with literal `code`."}

    def capture(command, **options):
        filename = Path(command[command.index("--input") + 1])
        assert json.loads(filename.read_text()) == payload
        assert options["cwd"] == filename.parent
        assert command[:4] == ["gh", "api", "--hostname", "github.com"]
        return ProcessResult(0, '{"ok": true}')

    monkeypatch.setattr("apiheal.integrations.github.run_bounded", capture)
    assert api.request("repos/example/demo/pulls", data=payload) == {"ok": True}
