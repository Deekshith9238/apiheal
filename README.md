# apiheal

apiheal statically detects legacy Python AI SDK calls, diagnoses Gemini errors offline,
and proposes a small OpenAI migration in an isolated Git worktree. It can verify the
supported migration in Linux Docker containers and, with explicit `--pr`, publish a draft
GitHub pull request backed by those checks.

Without `--verify`, patches remain unverified proposals. Missing Docker, missing images,
failed checks, and indeterminate checks block verification and publishing. Target code never
runs on the developer host as a fallback. The container runner is intended for reviewed demo
repositories; hardened infrastructure for arbitrary hostile external repositories is future work.

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

Gemini diagnosis uses supplied sanitized error evidence and optional `--endpoint developer_api`
or `--endpoint vertex_ai`. Ambiguous errors retain uncertainty. It does not contact Google,
inspect credential files, grant roles, enable billing, rotate keys, or change endpoints.

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
