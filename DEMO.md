# apiheal demo runbook

A two-minute product demo. It shows apiheal finding real breakages in real repositories and
opening a verified fix PR. Present it as an early prototype: detection covers Python and the
rules listed in the README. Say that on camera once.

## Before recording

1. Cache apiheal once, so recorded runs start instantly and need no `--refresh`:

   ```bash
   uvx --refresh --from git+https://github.com/Deekshith9238/apiheal apiheal --version
   ```

   The commands below use `apiheal` as shorthand for
   `uvx --from git+https://github.com/Deekshith9238/apiheal apiheal`.

2. Clone a public repository whose source you may show on camera:

   ```bash
   git clone https://github.com/shreeprabha1217/AI_Interviewer_ ~/demo/ai-interviewer
   ```

   Ask the owner before featuring it; it is a courtesy, not a requirement.

3. For private or employer repositories, show apiheal's text output only. Never show their
   source, the JSON report (it lists every file path and the Git remote), or paths that name
   the organization. Get the maintainer's permission to mention the result at all.

4. Use a terminal font of about 20pt, a dark theme, a short prompt, and a cleared screen.

5. Open these browser tabs:
   - the verified PR, [Deekshith9238/apiheal-demo#1](https://github.com/Deekshith9238/apiheal-demo/pull/1);
   - [Gemini API deprecations](https://ai.google.dev/gemini-api/docs/deprecations).

## Recording script (about 2 minutes)

Record one segment at a time and edit them together. Scan output is real; do not stage it.

### 1. The problem (0:00–0:15)

Show your face or a title card.

> "I lost two days to a Gemini SDK and Vertex AI migration. AI providers change SDKs, models,
> and authentication constantly, and nobody reads the changelogs. apiheal finds these breaks
> before they reach production."

### 2. A retired model in a public project (0:15–0:45)

```bash
apiheal scan ~/demo/ai-interviewer
```

Point at the `model-retired` finding, then open `backend/agent/src/agent/llm_factory.py` at
line 35.

> "This is a real open-source AI interviewer. apiheal finds it still calls `gemini-1.5-pro`,
> which Google shut down in September 2025, so every interview call fails when it runs on
> Gemini."

### 3. Your own backend (0:45–1:15)

```bash
apiheal scan /path/to/your/backend
```

Show the output only. Pause on the evidence lines under each finding.

> "This is my own production backend. apiheal found LangChain classes deprecated since 3.2.0,
> a package my Docker image never installs, and retired Gemini models. I didn't know about
> any of them."

Before recording, confirm whether each Gemini API date also applies to your Vertex AI project.
apiheal words those findings as "may fail" for that reason, so say it the same way.

### 4. A teammate's backend (1:15–1:25)

Show the output only, with permission.

> "A teammate's backend: eight calls to a Gemini SDK whose support ended a year ago. The
> developer who wrote them has left the company."

### 5. A verified fix (1:25–1:50)

Open the PR. Scroll through the two-file diff, then the verification table.

> "When a fix is safe, apiheal opens a PR, but only after proving it in a locked-down sandbox:
> the original code passes on the old SDK and fails on the new one, and the fix passes the
> same checks. Every check is attached as evidence."

### 6. Close (1:50–2:00)

> "Diagnosis first, minimal repair second, verification always, and a human approves the
> merge."

## Optional segments

- **Diagnosis instead of a guess:** run `apiheal diagnose` on the demo repository's
  `logs/gemini-permission.log`. It reports `manual_action_required` and does not open a PR,
  because a permissions failure is not a code change.
- **Failure path:** with an uncommitted edit in `apiheal-demo`, run
  `apiheal repair ../apiheal-demo --verify`. It stops with a policy block (exit 3) and
  leaves the checkout untouched. Revert the edit afterwards.
- **Recreate the verified PR:** see "One-time PR setup" below.

## One-time PR setup

The PR in segment 5 already exists. To recreate it, use Windows or Linux with Docker running:

```powershell
.\.venv\Scripts\apiheal.exe setup-runner
.\.venv\Scripts\apiheal.exe repair ..\apiheal-demo --verify --pr
```

The demo repository must be clean and match its GitHub base branch. `gh` needs Contents write
and Pull requests write on that repository.

## Claims to keep accurate

- Say "prototype". Automated repair covers one OpenAI migration; the other rules detect and
  explain.
- The demo repository is synthetic, and its Gemini log is a sanitized stand-in for the
  founder's incident, not the original.
- Model shutdown dates come from a dated table bundled with apiheal (see
  `rules/model-retired.json`). Gemini API dates are earliest dates, and Vertex AI keeps its own
  schedule.
- Count findings exactly: which repositories were scanned, which had findings, and which were
  clean.

## Evidence for the application

- The draft PR link and its run ID.
- The public apiheal repository with passing CI.
- Scan results from the real repositories, described without naming private code.
- Keep the product demo video separate from the founder video.
