# laya-sdlc

Local multi-agent development loop: Azure DevOps work items tagged `laya` are triaged by
[Laya](https://github.com/nandhakishorm/laya), planned/implemented/reviewed by Claude agents,
verified with the target repo's own commands, and opened as PRs. A human approves every merge.

Design: `docs/superpowers/specs/2026-09-23-laya-sdlc-design.md`

## One-time setup (done by a human)

1. **ADO identity/PAT** with Work Items (read/write), Code (read/write), Pull Requests
   (read/write). Store it with a restricted access list:
   `security add-generic-password -s laya-sdlc-ado-pat -a $USER -T <path to the project's .venv python> -w`
2. **Branch policies** on `dev`, `qa`, `main`, `prod`: deny direct push for that identity;
   require a PR with you as required reviewer.
3. **Claude token:** `claude setup-token`, then
   `security add-generic-password -s laya-sdlc-claude-token -a $USER -T <path to the project's .venv python> -w`

   Why `-T`: without it the item's access list trusts the app that created it, so any process
   running as you (including repo code executed by the verify commands) could read it silently.
   `-T` names the only application trusted to read it without a prompt. Use the resolved
   interpreter path (`uv run python -c "import os, sys; print(os.path.realpath(sys.executable))"`).
   laya-sdlc reads secrets by running `/usr/bin/security`, so macOS may prompt the first time;
   check the prompt names the app you expect before allowing. Alternative: export the secrets as
   environment variables (`LAYA_SDLC_ADO_PAT`, `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`)
   only in the shell that launches laya-sdlc; they are read before the keychain.

   **Isolation limits.** Agent Bash runs in the Claude Agent SDK sandbox (no network, no Unix
   sockets, no escape hatch) with a scratch `HOME` and an environment allowlist. The target's
   verify commands (install/test/lint/typecheck/build) still execute repo code outside that
   sandbox — only the scratch `HOME`, the environment allowlist and the absence of credential
   variables protect you there. Full isolation (a container or VM) is a decision to make before
   running unattended.
4. **Pilot check:** confirm `npm run test --workspace=apps/rallysource-api` passes on `dev`
   without a database or `.env`; otherwise set `repo.env_template` or narrow the command in
   `targets/rallysource.yaml`.
5. `uv sync`

## Everyday use

```bash
uv run laya-sdlc status
uv run laya-sdlc run --once --dry-run-push   # full pipeline, no push, prints PR body
uv run laya-sdlc run                         # loop (polls every 60s)
uv run laya-sdlc pause | resume              # kill switch
uv run laya-sdlc requeue <id>                # same as removing the laya:parked tag
```

Opt a work item in by adding the `laya` tag. Parked items get a comment and the `laya:parked`
tag; removing the tag approves proceeding past a gate park or retries a failed stage. On a PR,
start a comment with `/laya` to request a revision.

## Calibrating Laya gates

All gates start in shadow mode (logged, never trusted). To activate a gate:

```bash
uv run laya-sdlc label triage --limit 40      # labels closed ADO items
uv run laya-sdlc label review --limit 40      # labels logged decisions
uv run laya-sdlc calibrate triage --promote   # fits temperature; activates if ECE <= max_ece
```

Human approvals (tag removals, merges, `/laya` comments) add labels automatically.

## Development

```bash
uv run pytest            # unit + e2e (fast)
uv run pytest -m slow    # real Laya model + real Claude SDK policy test
uv run ruff check . && uv run mypy
```
