# agent-sdlc

Local multi-agent development loop: Azure DevOps work items tagged `agent` are triaged by
[Laya](https://github.com/nandhakishorm/laya), planned/implemented/reviewed by Claude agents,
verified with the target repo's own commands, and opened as PRs. A human approves every merge.

Design: `docs/superpowers/specs/2026-09-23-agent-sdlc-design.md`

## One-time setup (done by a human)

1. **ADO identity/PAT** with Work Items (read/write), Code (read/write), Pull Requests
   (read/write). Store it with a restricted access list:
   `security add-generic-password -s agent-sdlc-ado-pat -a $USER -T <resolved interpreter path, see below> -w`
2. **Branch policies** on `dev`, `qa`, `main`, `prod`: deny direct push for that identity;
   require a PR with you as required reviewer.
3. **Claude token:** `claude setup-token`, then
   `security add-generic-password -s agent-sdlc-claude-token -a $USER -T <resolved interpreter path, see below> -w`

   Why `-T`: without it the item's access list trusts whichever app created it (`security`),
   so any process running as you could read it with `security find-generic-password` and no
   prompt. agent-sdlc reads secrets in-process through `keyring`, so the keychain checks the
   Python interpreter itself; `-T <interpreter>` makes that interpreter the only app that reads
   the item without a prompt, and `security` (or anything else) gets a prompt instead. Use the
   resolved interpreter path, not the `.venv` symlink:
   `uv run python -c "import os, sys; print(os.path.realpath(sys.executable))"`.
   If you recreate the venv with a different Python, re-add the items with the new path.

   **What this does not protect against:** any code running as you can still launch that same
   interpreter and read the item, and a prompt that says "python" won't tell you who is asking.
   So never click "Always Allow" on a prompt you didn't expect. Alternative: export the secrets
   as environment variables (`AGENT_SDLC_ADO_PAT`, `CLAUDE_CODE_OAUTH_TOKEN` or
   `ANTHROPIC_API_KEY`) only in the shell that launches agent-sdlc; they are read before the
   keychain and never passed to agents or verify commands.

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
uv run agent-sdlc status
uv run agent-sdlc run --once --dry-run-push   # full pipeline, no push, prints PR body
uv run agent-sdlc run                         # loop (polls every 60s)
uv run agent-sdlc pause | resume              # kill switch
uv run agent-sdlc requeue <id>                # same as removing the agent:parked tag
```

Opt a work item in by adding the `agent` tag. Parked items get a comment and the `agent:parked`
tag; removing the tag approves proceeding past a gate park or retries a failed stage. On a PR,
start a comment with `/agent` to request a revision.

## Calibrating Laya gates

All gates start in shadow mode (logged, never trusted). To activate a gate:

```bash
uv run agent-sdlc label triage --limit 40      # labels closed ADO items
uv run agent-sdlc label review --limit 40      # labels logged decisions
uv run agent-sdlc calibrate triage --promote   # fits temperature; activates if ECE <= max_ece
```

Human approvals (tag removals, merges, `/agent` comments) add labels automatically.

## Development

```bash
uv run pytest            # unit + e2e (fast)
uv run pytest -m slow    # real Laya model + real Claude SDK policy test
uv run ruff check . && uv run mypy
```
