# apiheal

apiheal statically detects legacy Python AI SDK calls, diagnoses Gemini errors offline,
and proposes a small OpenAI migration in an isolated Git worktree. It can verify the
supported migration in Linux Docker containers and, with explicit `--pr`, publish a draft
GitHub pull request backed by those checks.

Without `--verify`, patches remain unverified proposals. Missing Docker, missing images,
failed checks, and indeterminate checks block verification and publishing. Target code never
runs on the developer host as a fallback. The container runner is intended for reviewed demo
repositories; hardened infrastructure for arbitrary hostile external repositories is future work.

## GitHub App pilot

The app posts an advisory **apiheal / Deprecations** check on a pull request's head commit.
It scans changed files against their merge-base versions and annotates new findings.

| Detector | Source |
| --- | --- |
| npm dependencies | Live public npm registry metadata for newly added exact versions in v2/v3 npm lockfiles |
| GitHub Actions | Action metadata at the referenced tag or commit, compared with GitHub runtime retirement notices |
| Python | Existing static SDK and model detectors on changed Python source files |

### Register and run

1. [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app).
   Grant repository **Contents: Read**, **Pull requests: Read**, and **Checks: Read and write**.
   Subscribe to **Pull request** events. Set the webhook URL to
   `https://YOUR_HOST/webhooks/github`, set a webhook secret, and generate a private key.
2. Install the app on a pilot repository. Deploy the service behind HTTPS with a single
   process and store the key and webhook secret in your host's secret manager.
3. Set the environment variables below, start the service, and open or update a test PR.

```sh
uv sync --locked --extra app
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY_FILE=/path/to/github-app.private-key.pem
export APIHEAL_APP_HOST=0.0.0.0
# Inject GITHUB_WEBHOOK_SECRET through your environment or secret manager.
uv run --locked --extra app apiheal-app
```

The service listens on port 8000 by default and exposes `GET /healthz`.
The PR check can run with read access to repository contents. It does not install packages,
run repository code, or modify branches. It contacts the public npm registry only for
lockfile entries explicitly resolved there. External action metadata is fetched anonymously
from GitHub; inaccessible actions surface as a coverage limit.

This pilot covers changed PR files and uses an in-memory queue. Redeliver failed webhooks
from GitHub after restoring service. Scheduled default-branch rescans, a durable queue,
private registries, other package managers, and broader language analysis are future work.
Python findings use source patterns without grading installed dependency versions in the
GitHub check. The local CLI continues to support manifest-based grading.

Run the app tests with `uv run --locked --extra dev --extra app pytest tests/test_github_app.py`.

## Install

Requires Python 3.11+ and Git. From this project:

```powershell
uv sync --extra dev
uv run apiheal --help
uv run pytest
uv run ruff check .
```

Alternatively, install with `python -m pip install -e ".[dev]"` in a virtual environment.

## Use

```text
apiheal scan /path/to/repo --format json
apiheal diagnose /path/to/repo --log /path/to/error.log --format json
apiheal repair /path/to/repo --dry-run
apiheal setup-runner
apiheal repair /path/to/repo --verify
apiheal repair /path/to/github-checkout --verify --pr
apiheal report --run-id <32-character-run-id> --format json
```

Reports and redacted diffs are saved outside the checkout in `~/.apiheal/runs/<run-id>`.
Override storage with `--data-dir` or `APIHEAL_DATA_DIR`. Storage inside the target repository
is rejected. JSON goes to stdout; runtime errors requested as JSON also use stdout.

Repair requires a committed, clean baseline, including no untracked files. It never stashes,
commits, pushes, or modifies the caller's checkout. Transient detached worktrees are discarded
after saving the proposal. `--dry-run` does not apply the patch even to the temporary worktree.

The first codemod supports a single top-level OpenAI import, including an alias, and simple
top-level synchronous functions with this body:

```python
response = openai.ChatCompletion.create(model=model, messages=messages)
return response["choices"][0]["message"]["content"]
```

It introduces a scoped client context, preserves call arguments/model IDs, and changes the
supported response access. Requirements must be a single root `requirements.txt`; the
historical migration target is pinned to `openai==1.109.1`, not advertised as the latest SDK.
The original requirement must explicitly constrain a legacy version compatible with the
`0.28.1` fixture, or already pin that migration target; unbounded/modern constraints need review.
Configuration mutations, other SDK uses, streaming, async, decorators, dynamic arguments,
wrappers, complex response handling, test/build edits, and other dependency layouts require
manual review. The scanner also reports legacy embeddings/completions and Gemini imports.
Finding a legacy pattern is not evidence of an actual runtime outage.

The scanner also flags the LangChain classes deprecated with `langchain-google-genai` 4.0.0
(rule `langchain-vertexai-deprecated`): `ChatVertexAI`, `VertexAI`, and `VertexAIEmbeddings`
from `langchain-google-vertexai`, with their `langchain-google-genai` replacements. This rule
is detection-only; other `langchain-google-vertexai` features remain supported and are not
reported. See `rules/langchain-vertexai-deprecated.json` for its source and limitations.

Rule `model-retired` flags string literals that name a retired or scheduled-for-shutdown model
from the Google (Gemini API and Vertex AI), OpenAI, or Anthropic deprecation pages, with the
shutdown date and recommended replacement. Passed shutdowns are errors, those within 180 days
are warnings, and later ones are info. The bundled table is dated; provider and platform
schedules can differ. See `rules/model-retired.json` for sources and limitations.

Gemini diagnosis uses supplied sanitized error evidence and optional `--endpoint developer_api`
or `--endpoint vertex_ai`. Ambiguous errors retain uncertainty. It does not contact Google,
inspect credential files, grant roles, enable billing, rotate keys, or change endpoints.

## Add SDK detection rules

```sh
apiheal scan /path/to/repo --rule-pack /path/to/sdk-rules.json --format json
```

Pass `--rule-pack` once per file. Packs are explicitly selected, validated JSON files
that extend the [built-in SDK catalog](src/apiheal/detection/data/sdk_rules.json).
Each rule needs a unique ID. Built-in IDs cannot be overridden.

```json
{
  "schema_version": 1,
  "rules": [
    {
      "id": "example-legacy-client",
      "version": "1",
      "provider": "example",
      "category": "deprecation",
      "documentation": "https://example.com/sdk/migration",
      "dependency": {
        "package": "example-sdk",
        "deprecated_since": "2.0",
        "removed_in": "3.0"
      },
      "selectors": [
        {
          "kind": "symbol",
          "module": "example_sdk",
          "name": "LegacyClient",
          "message": "Replace LegacyClient with Client."
        }
      ]
    }
  ]
}
```

- `module` matches imports of a module and takes no `name`.
- `symbol` matches direct imports and qualified attribute references.
- `call` matches calls resolved to an import and accepts dotted names such as `Client.create`.
- Set `submodules` to `true` to include descendants of the named module. Matching uses
  static import resolution, including aliases.
- Optional `dependency` metadata grades findings against declared version ranges in
  `requirements*.txt` and PEP 621 `pyproject.toml`. These ranges describe what can be
  installed. Reports do not resolve the installed environment or lockfiles.

Custom packs extend scanning only. Repair continues to use the built-in OpenAI codemod
and its verification gates. Model shutdown dates use the separate bundled retirement table.

## Exit statuses

| Code | Meaning |
| --- | --- |
| 0 | Completed scan with no findings, or generated an explicitly unverified patch proposal |
| 1 | Findings, unsupported cases, or manual diagnostic action |
| 2 | Invalid input or configuration |
| 3 | Policy blocked repair |
| 4 | Required verification failed or could not run |
| 5 | Infrastructure or I/O error |

Run `report` returns the saved operation's exit status. CLI syntax/option errors use Typer's
standard error output. Static analysis and offline verification need no provider credentials.
Explicit GitHub publishing needs a separately scoped GitHub credential.

## Isolated verification

Install/start Docker with Linux containers and resource limits. Prepare apiheal's two SDK
images once, then run verification from this project on Windows:

```powershell
.\.venv\Scripts\apiheal.exe setup-runner
.\.venv\Scripts\apiheal.exe repair ..\apiheal-demo --verify
```

Image builds require network access for the Python base image and approved SDK/test packages.
The build context contains only apiheal's harness, Dockerfile, and trusted pytest configuration.
Target repository requirements and build scripts are never executed during the image build.
Re-run setup after changing harness assets. Images are checked against the harness digest and
executed by immutable image ID; missing/outdated images are not silently downloaded at run time.

Verification uses read-only source snapshots, a non-root user, dropped capabilities, seccomp,
memory/CPU/PID/time/output limits, and `--network none`. No home directory, Git metadata,
credential files, production provider keys, GitHub tokens, or Docker socket are mounted.
The only HTTP endpoint used by SDK contracts is a loopback mock server inside the container.
No live provider access or model availability is established.

Required checks cover:

1. The saved patch applies cleanly to the recorded baseline.
2. Prepared dependencies satisfy the supported requirements and pass `pip check`.
3. Changed Python modules compile and import inside the sandbox.
4. Original code with OpenAI `0.28.1` passes the trusted contract cases.
5. Original code with OpenAI `1.109.1` fails specifically with `APIRemovedInV1`.
6. Repaired code with `1.109.1` preserves the legacy request/response facts, including Unicode
   output and a propagated HTTP 400 error: three cases per supported function.
7. Discovered repository unit tests pass with no zero-test or skipped-required-test success.
8. Baseline/candidate snapshots remain unchanged and are bound to the recorded evidence.

When no repository unit suite exists, the three-case SDK contract suite is the documented
alternative check. The report explicitly marks repository unit tests as skipped/optional and
records `contract_only: true`; it does not claim that a project test suite ran.

The first harness requires functions callable with `model` and `messages` keyword parameters,
with no other required parameters. Additional dependencies must already exist in the approved
image and satisfy their constraints. Unsupported dependencies and configured custom lint/type/
test gates stop verification instead of being ignored. There is no arbitrary test-command option.

Runs include sanitized per-gate logs, `verification.json`, and `verification-evidence.json`
with SDK inventories, image IDs, baseline/candidate digests, policy digest, and harness digest.
The caller checkout remains unchanged even when verification succeeds.

## GitHub pull requests

Use a clean checkout whose `origin` identifies your repository on `github.com`. The local
baseline must match the current remote base-branch tip. Authenticate `gh` with a credential
scoped to that repository with Contents write and Pull requests write permission.

```powershell
.\.venv\Scripts\apiheal.exe repair C:\path\to\github-checkout --verify --pr
# Select a non-default target branch explicitly when needed:
.\.venv\Scripts\apiheal.exe repair C:\path\to\github-checkout --verify --pr --base develop
```

The publisher prepares `pr-body.md`, rechecks the exact verified candidate, and uses GitHub's
Git Data API through `gh api` to upload the verified file bytes, create a commit/unique branch,
and open a draft PR. It does not run repository push hooks, credential helpers, or auto-merge.
PR contents include upstream provenance, before/after behavior, gate results, environment
identities, and human-review requirements. GitHub credentials remain outside the sandbox.

Retries reconcile a deterministic repair branch and existing PR. Unrelated branch conflicts,
closed PRs, stale bases, and changed candidates stop publishing; branches are never overwritten.
If the API response is lost after PR creation, a retry looks up that PR instead of blindly
creating another. Publication failure preserves local verification evidence with exit code 5.
An interrupted publication may leave a repair branch on GitHub for that retry to reconcile.

`apiheal-demo` is currently a local repository without a GitHub remote. It is ready for
`--verify` once Docker/images are available; publishing requires your actual GitHub checkout.

## Testing the runner

The ordinary `pytest` suite tests the orchestration and API adapters using simulated backends.
It does not pull/build Docker images or contact GitHub. To exercise the real SDKs in containers:

```powershell
.\.venv\Scripts\apiheal.exe setup-runner
$env:APIHEAL_DOCKER_TESTS = '1'
.\.venv\Scripts\pytest.exe -m docker -q
Remove-Item Env:APIHEAL_DOCKER_TESTS
```

The Linux CI job prepares the images and runs these integration tests. A skipped Docker test
is not runtime verification evidence. Live GitHub publication is not part of automated tests.

## Development status

The [implementation plan](IMPLEMENTATION_PLAN.md) describes the full target. The implementation
now includes container verification and an explicitly gated GitHub publisher alongside the
CLI, worktree isolation, migration proposals, and offline Gemini diagnosis. Broader provider
rules, dependency layouts, and hardened external-repository execution remain future work.

| Plan task | Current state |
| --- | --- |
| A01–A03 | Implemented: package/CLI, records/redaction/storage, inventory and worktree lifecycle |
| A05 | Deterministic migration proposal with supported response/dependency edits |
| A07 | Basic offline Gemini classification implemented |
| A04, A06 | Docker runner and three-environment offline verification implemented; real execution requires prepared Docker images |
| A08 | Draft PR publisher, exact-snapshot gate, and retry handling implemented; live publication has not been demonstrated here |
| A09 | Fixtures, docs, and real-Docker CI test present; recorded demo/customer evidence pending |
