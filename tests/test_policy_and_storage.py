import hashlib
import json

import pytest

from apiheal.core.errors import InputError, PolicyError
from apiheal.core.models import CheckResult, RunResult
from apiheal.core.policy import Policy, required_checks_pass
from apiheal.core.run_store import RunStore
from apiheal.repair.dependency_edit import migrate_requirements
from apiheal.telemetry.redaction import REDACTED, redact, redact_text


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-" + "synthetic" * 8,
        "AIza" + "synthetic" * 6,
        "ghp_" + "synthetic" * 6,
        "github_pat_" + "synthetic" * 6,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.syntheticSignature",
    ],
)
def test_known_credential_shapes_are_redacted(secret):
    assert secret not in redact_text(f"Error: {secret}")


@pytest.mark.parametrize(
    "text,secret",
    [
        ("Authorization: Bearer arbitrary-opaque-value", "arbitrary-opaque-value"),
        ('api_key="shortsecret"', "shortsecret"),
        ("GOOGLE_API_KEY=shortsecret", "shortsecret"),
        ("https://user:shortsecret@github.com/example/repo", "shortsecret"),
        ("https://example.invalid/?key=shortsecret&model=example", "shortsecret"),
        ("Authorization: Basic synthetic-base64-token", "synthetic-base64-token"),
        ("-----BEGIN PRIVATE KEY-----\nshortsecret\n-----END PRIVATE KEY-----", "shortsecret"),
    ],
)
def test_contextual_secrets_are_redacted(text, secret):
    assert secret not in redact_text(text)
    assert REDACTED in redact_text(text)


def test_structured_redaction_preserves_safe_fields():
    assert redact({"nested": {"api_key": "tiny", "status": 403}}) == {
        "nested": {"api_key": REDACTED, "status": 403},
    }


def test_store_redacts_all_artifacts_and_loads_valid_json(tmp_path):
    secret = "sk-proj-" + "synthetic" * 8
    store = RunStore(tmp_path / "runs")
    result = RunResult(command="scan", message=f'api_key="{secret}"')
    store.save(result, diff=f'+api_key="{secret}"\n')
    directory = store.directory(result.run_id)
    for path in directory.iterdir():
        assert secret not in path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            json.loads(path.read_text())
    assert REDACTED in store.load(result.run_id).message
    assert not list(directory.glob(".writing-*"))


def test_store_rejects_traversal_and_in_repo_storage(tmp_path):
    store = RunStore(tmp_path / "runs")
    with pytest.raises(InputError):
        store.directory("../elsewhere")
    with pytest.raises(InputError):
        store.write_text("a" * 32, "../escape", "x")
    with pytest.raises(InputError):
        store.ensure_outside(tmp_path)
    with pytest.raises(InputError, match="not found"):
        store.load("a" * 32)


def test_dependency_edit_is_scoped_and_idempotent():
    before = "# comment\r\nopenai>=0.27,<1  # SDK\r\nrequests==2.32.5\r\n"
    after = migrate_requirements(before)
    assert after == "# comment\r\nopenai==1.109.1  # SDK\r\nrequests==2.32.5\r\n"
    assert migrate_requirements(after) == after


@pytest.mark.parametrize(
    "source",
    [
        "requests==2\n",
        "openai==0.28\nopenai>=0.27\n",
        "-r base.txt\nopenai==0.28\n",
        "openai[extra]==0.28\n",
        "openai==0.28; python_version<'3.12'\n",
        "openai @ https://example.invalid/sdk.whl\n",
        "-e .\nopenai==0.28\n",
        "--index-url https://example.invalid\nopenai==0.28\n",
        "openai>=2\n",
        "openai\n",
        "openai>=0.28\n",
    ],
)
def test_unsupported_dependencies_fail_closed(source):
    with pytest.raises(PolicyError):
        migrate_requirements(source)


@pytest.mark.parametrize(
    "name",
    [
        "tests/test_app.py",
        "conftest.py",
        "setup.py",
        ".github/workflows/build.yml",
        "../outside.py",
        "C:/outside.py",
        "source\\app.py",
    ],
)
def test_protected_patch_paths(name):
    with pytest.raises(PolicyError):
        Policy().validate_changes("base", {name: ("old\n", "new\n")}, {name})


def test_patch_limits_and_path_scope():
    changes = {"app.py": ("old\n", "new\n")}
    artifact, diff = Policy().validate_changes("base", changes, {"app.py"})
    assert artifact.changed_lines == 2 and len(artifact.sha256) == 64
    assert "--- a/app.py" in diff
    with pytest.raises(PolicyError, match="outside"):
        Policy().validate_changes("base", changes, {"other.py"})
    with pytest.raises(PolicyError, match="line limit"):
        Policy(max_changed_lines=1).validate_changes("base", changes, {"app.py"})


def test_noop_patch_cannot_be_success():
    with pytest.raises(PolicyError, match="no changes"):
        Policy().validate_changes("base", {"app.py": ("same", "same")}, {"app.py"})


def test_secret_bearing_diff_is_blocked_instead_of_saved_with_a_wrong_digest():
    secret = "sk-proj-" + "synthetic" * 8
    changes = {"app.py": (f"KEY = '{secret}'\nold\n", f"KEY = '{secret}'\nnew\n")}
    with pytest.raises(PolicyError, match="credentials"):
        Policy().validate_changes("base", changes, set(changes))


def test_missing_final_newline_is_a_valid_diff(repository, tmp_path):
    from apiheal.core.git import git

    path = repository / "requirements.txt"
    path.write_text("openai==0.28.1", encoding="utf-8")
    changes = {"requirements.txt": ("openai==0.28.1", "openai==1.109.1")}
    artifact, diff = Policy().validate_changes("base", changes, set(changes))
    patch = tmp_path / "no-newline.diff"
    patch.write_text(diff, encoding="utf-8", newline="\n")
    assert "\\ No newline at end of file" in diff
    assert hashlib.sha256(diff.encode()).hexdigest() == artifact.sha256
    git(repository, "apply", "--check", str(patch))


def test_required_verification_cannot_be_empty_skipped_or_duplicated():
    passed = CheckResult(gate="unit", outcome="passed", test_count=1, command=["pytest"])
    assert required_checks_pass([passed], {"unit"})
    assert not required_checks_pass([], set())
    assert not required_checks_pass([passed], {"unit", "contract"})
    assert not required_checks_pass([passed, passed], {"unit"})
    assert not required_checks_pass([passed.model_copy(update={"test_count": 0})], {"unit"})
    assert not required_checks_pass([passed.model_copy(update={"command": []})], {"unit"})
    for state in ("failed", "skipped", "indeterminate"):
        assert not required_checks_pass([CheckResult(gate="unit", outcome=state)], {"unit"})
