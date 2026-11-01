# apiheal demo runbook

A 2–3 minute product demo: one verified SDK migration that ends in a draft GitHub PR, and one
Gemini incident that apiheal correctly refuses to "fix" with code. All content in the demo
repository is synthetic; say so on camera.

## One-time setup

1. Docker Desktop running with Linux containers.
2. Prepare the runner images (re-run after changing harness assets):

   ```powershell
   .\.venv\Scripts\apiheal.exe setup-runner
   ```

3. Publish `apiheal-demo` to a GitHub repository you own, so `--pr` has a target:

   ```powershell
   gh repo create apiheal-demo --public --source ..\apiheal-demo --push
   ```

   The local baseline must equal the remote base-branch tip, and the checkout must be clean.
   `gh` needs Contents write and Pull requests write on that repository.

## Recording script

Run each command from this project. Keep the terminal font large and clear the screen between steps.

### 1. Detect (about 20 s)

```powershell
.\.venv\Scripts\apiheal.exe scan ..\apiheal-demo
```

Show: one repairable `openai-legacy-completions` finding at `app.py:8`, exit code 1.
Say: LibCST resolves the import, so aliases are handled; matching a string alone does not count.

### 2. Diagnose instead of guessing (about 30 s)

```powershell
.\.venv\Scripts\apiheal.exe diagnose ..\apiheal-demo --log ..\apiheal-demo\logs\gemini-permission.log
```

Show: `manual_action_required`, the ranked causes, and the manual steps. Say that no PR is
generated because a permissions problem is not a code problem. This is the incident that
motivated apiheal.

### 3. Repair, verify, and open a PR (about 60 s)

```powershell
.\.venv\Scripts\apiheal.exe repair ..\apiheal-demo --verify --pr
```

Show the gate list as it passes, then point out:

- the original code passes on `openai==0.28.1`;
- the original code fails with `APIRemovedInV1` on `1.109.1`, which proves the break is real;
- the repaired code passes the same contract cases, including Unicode and HTTP 400 propagation;
- everything runs in Docker with `--network none` and no credentials.

### 4. The pull request (about 30 s)

Open the draft PR on GitHub. Scroll through the minimal diff (2 files), the verification
matrix, environment identities, and the "human review required" statement.

### 5. Close (about 15 s)

"Diagnosis first. Minimal repair second. Verification always. Human approval before merge."

## Optional: show the failure path

Make an unrelated uncommitted edit in `apiheal-demo` and run `repair --verify`. apiheal stops
with a policy block (exit 3) and leaves the checkout untouched. Revert the edit afterwards.

## Evidence for the application

- Link to the draft PR, plus the run ID from `~/.apiheal/runs/<run-id>` (`report.md`,
  `verification.json`, `verification-evidence.json`).
- The public apiheal repository with passing CI.
- Keep the product demo video separate from the founder video.

Do not describe the demo as fixing a live incident. It reproduces a known migration on a synthetic
repository; the Gemini log is a sanitized representation of the founder's incident, not the
original.
