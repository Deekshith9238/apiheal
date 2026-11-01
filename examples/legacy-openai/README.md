# Legacy OpenAI fixture

Copy `app.py` and `requirements.txt` to a separate directory, initialize a Git repository,
and commit them. Then run `apiheal scan <that-directory>` and
`apiheal repair <that-directory> --dry-run` from an installed apiheal environment.

Do not scan this example directory in place expecting it to be its own Git repository:
apiheal intentionally resolves paths to their containing repository root.

The example is a source migration fixture, not an independently verified live incident.
No credentials are included. Do not execute provider calls to test the codemod; behavioral
verification belongs in the planned isolated runner.
