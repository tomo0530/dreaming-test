# Git, PR, and Secret Scanning

Detailed Git and PR rules for AI agents. AGENTS.md §6 indexes this
file. Apply these when committing, pushing, or creating PRs.

## Branch naming

* Use a feature branch prefixed `feature/`, `fix/`, `docs/`, or
  `chore/` (for example `feature/dreaming-harness`).
* **Never** commit directly to `main`.

## Commits

* Commit only when the user explicitly authorizes it.
* Keep commits focused on a single logical change.
* Destructive Git commands (`git reset --hard`, force push, etc.) are
  prohibited without explicit approval — see AGENTS.md §3.

## Pull requests

* Use the `gh` CLI. **Not installed by default** — install it on the
  host (`brew install gh` or the official release).
* State the purpose, the changes, and the verification (`make test`,
  `make all-lint`) in the PR body.
* Do **not** force-push to shared branches without approval.

## Secret scanning (`gitleaks`)

* `make secret-scan [path]` runs `gitleaks dir --redact --verbose` on
  the working tree. Default path is `.`.
* `gitleaks` is not installed by default — install it on the host
  (`brew install gitleaks`).

### Handling false positives

* Mark the line with an inline `# gitleaks:allow` comment.
* If an inline comment is impossible, ask the user how to suppress the
  finding before adding any config or ignore file.
