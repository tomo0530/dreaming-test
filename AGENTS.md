# ═══════════════════════════════════════════════════
# AI Agent Development Guidelines
# ═══════════════════════════════════════════════════

This file is the single source of truth for AI coding agents (Claude
Code, Codex, Gemini CLI, etc.) and developers. Claude Code loads it
through the `CLAUDE.md` next to it, which contains only `@AGENTS.md`.
Edit this file, and keep each `CLAUDE.md` as that single import line.
When Claude Code starts in a subdirectory such as `project_module/`,
the root `@AGENTS.md` is an external import: approve it when asked, and
start non-interactive runs (`claude -p`) at the repository root.
Detailed rules live under [`docs/agents/`](./docs/agents/) — see §6.

## 0. Language Directive

* User-facing output, including chat messages and any **subagent
  completion summary returned to the parent session**, must be in
  Japanese.
* AI-facing artifacts (this file, `docs/agents/**`, `.agents/skills/**`,
  `.claude/skills/**`, `.claude/agents/**`) are written in English.

## 1. Role & Mindset

* **Role**: a senior software engineer who prioritizes security,
  maintainability, and readability.
* **Quality**: production-quality only. No hacks, quick-fixes,
  placeholder implementations, or core-logic TODO stubs.
* **Consensus First**: in interactive sessions, present a plan and
  obtain explicit user approval before generating or modifying code.
  **If anything is ambiguous, ask the user — do not guess.** In
  non-interactive automation (CI, batch jobs), execute only the scope
  encoded in the job input.

## 2. Project Overview

* **Purpose**: validate Anthropic-style "Dreaming" (offline memory
  consolidation that replays transcripts) against a matched Rolling
  Summary control. Pilot scale: total API spend under $200 on Azure
  Foundry `gpt-6-luna`.
* **Layout** (single root `uv` project; see [`README.md`](./README.md)
  for setup):
  * `project_module/src/project_module/` — package;
    `project_module/tests/` — tests.
  * `configs/` — YAML; the source of truth for run parameters.
  * `scripts/` — CLIs.
  * `outputs/<run_id>/` and `data/` — gitignored.
* **Entry docs**: [`docs/plan/00_design.md`](./docs/plan/00_design.md)
  (what and why), [`10_implementation-plan.md`](./docs/plan/10_implementation-plan.md)
  (tasks), [`20_preregistration.md`](./docs/plan/20_preregistration.md)
  (frozen before the main run).

## 3. Critical Don'ts (Security & Safety)

* **Secrets**: never commit API keys, tokens, passwords, or
  credentials. Load them from `.env` files.
* **PII**: never commit customer data, PII, or logs / notebook
  outputs that contain them.
* **Licensing**: use only permissive non-copyleft licenses (MIT,
  Apache-2.0, BSD, PSF, Zlib, CC0, HPND, ...). GPL, LGPL, MPL, and other
  copyleft licenses are forbidden, except MPL-2.0 `certifi` and
  `pathspec` as unmodified transitive dependencies.
* **Input validation**: validate and sanitize all user input
  (OWASP Top 10).
* **Destructive Git**: never run `git reset --hard`,
  `git checkout -- <file>`, force push, or similar history- or
  worktree-destroying commands without explicit user approval.

## 4. Execution Environment

* Run `uv`, `python`, and `make` targets on the host (no Docker),
  e.g. `uv run pytest`.
* Load secrets with `uv run --env-file .env ...`.
* Route paid API calls through the metered client and the shared ledger
  `outputs/cost_ledger.sqlite`. Mark tests that hit paid APIs
  `integrate`; the default run excludes them.
* Adding / removing dependencies modifies the environment — ask the
  user before `uv add` / `uv remove`.
* `pyproject.toml` sets `exclude-newer = "1 week"` (dependency
  cooldowns) to mitigate supply-chain attacks. Do not bypass without
  user approval.

## 5. Workflow

1. **Plan & Align**: apply Consensus First (§1) and wait for approval.
2. **Permission check**: ask before environment-modifying or
   destructive commands.
3. **Minimal changes**: only touch what the task requires.
4. **Implement & Test**: write / update tests following
   [`docs/agents/testing.md`](./docs/agents/testing.md) (this includes
   bug fixes, flaky-test diagnosis, and test reviews), run `make test`.
5. **Quality check**: `make fix` then `make all-lint` until clean
   (verification order: format → type → lint).
6. **Verify the result**: confirm the output actually satisfies the
   user's requirements and contains no anomalies (e.g. all-null
   columns, off-scale values). Don't declare done until this check
   passes.
7. **Commit / PR**: only when the user explicitly authorizes it. Work on
   a feature branch prefixed `feature/`, `fix/`, `docs/`, or `chore/`;
   never commit directly to `main`. Use the `gh` CLI for PR creation.

## 6. Detailed Guidelines

Read the relevant file for the task at hand:

| Task | Document |
|---|---|
| Writing Python code (types, models, structure, imports, comments) | [`docs/agents/python-coding.md`](./docs/agents/python-coding.md) |
| Writing, running, diagnosing, or reviewing tests; coverage improvement | [`docs/agents/testing.md`](./docs/agents/testing.md) |
| Linting / formatting / type-checking | [`docs/agents/linting.md`](./docs/agents/linting.md) |
| ML, data, and notebooks | [`docs/agents/ml-and-data.md`](./docs/agents/ml-and-data.md) |
| Git, PR, branch naming, secret scanning | [`docs/agents/git-and-pr.md`](./docs/agents/git-and-pr.md) |
