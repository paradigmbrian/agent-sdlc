# agent-sdlc

Local multi-agent development loop: Azure DevOps work items or GitHub issues tagged `agent` are
triaged by [Laya](https://github.com/nandhakishorm/laya), planned/implemented/reviewed by Claude
agents, verified with the target repo's own commands, and opened as PRs. A human approves every
merge.

Design: `docs/superpowers/specs/2026-09-23-agent-sdlc-design.md` plus
`docs/superpowers/specs/2026-09-26-multi-forge-design.md` (GitHub and parallel targets).

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

## Configuration

- `agent-sdlc.yaml` lists the targets and holds settings shared by the one Claude subscription:
  `auth`, `laya`, daily turn/token caps, `max_concurrent_sessions`, `run_window`.
- `targets/<name>.yaml` holds one repository: `forge` (`kind: ado` or `kind: github`),
  `intake` labels, `repo` (base branch, branch prefix, install list, verify commands), `policy`
  and per-item `limits`.
- `--config <file>` picks another global config; `--target <file>` runs a single target with
  default global settings.
- State lives in `~/.agent-sdlc/agent-sdlc-v2.db`. Items are referenced as `<target>#<id>`
  (`agent-sdlc trace triathlon#12`); a bare id works when only one target has it.

## GitHub setup (per repository, done by a human)

1. Create a private GitHub App on the `paradigmbrian` account: webhook disabled; repository
   permissions Contents: read & write, Issues: read & write, Pull requests: read & write,
   Metadata: read. Install it on the target repo only. Note the App id.
2. Generate a private key and store it:
   `security add-generic-password -s agent-sdlc-github-app-<app_id> -a $USER -T <resolved interpreter path> -w "$(cat <key>.pem)"`,
   then delete the `.pem` (or export `AGENT_SDLC_GITHUB_APP_KEY` in the launching shell).
3. Add a branch ruleset on `main`: require a pull request with 1 approval, block force pushes and
   deletion, no bypass for the App.
4. Create the labels `agent` and `agent:parked` on the repo.
5. Pilot check: in a clean clone of `main` with no `.env` and no running Postgres, confirm the
   install list and every command in `targets/triathlon.yaml` pass (db-marked tests skip). Narrow
   any command that fails for environmental reasons.
6. Fill `app_id` in `targets/triathlon.yaml`, then uncomment `targets/triathlon.yaml` in
   `agent-sdlc.yaml` (it ships commented out so the loop does not retry an unconfigured target).

## Everyday use

```bash
uv run agent-sdlc status
uv run agent-sdlc status --target-name triathlon
uv run agent-sdlc run --once --dry-run-push   # full pipeline, no push, prints PR body
uv run agent-sdlc run                         # loop (polls every 60s)
uv run agent-sdlc pause | resume              # kill switch
uv run agent-sdlc pause --target-name triathlon
uv run agent-sdlc requeue <target>#<id>       # same as removing the agent:parked tag
uv run agent-sdlc trace <target>#<id> [--full] # timeline: transitions, sessions, denials, checks
uv run agent-sdlc metrics [--days 30]          # outcomes, parks, effort, latency, denials, gates
```

Opt an item in by adding the `agent` tag (ADO) or label (GitHub). Parked items get a comment and
the `agent:parked` tag; removing the tag approves proceeding past a gate park or retries a failed
stage. On a PR, start a comment with `/agent` to request a revision; on GitHub a "Request changes"
review also counts.

`status` also shows when the loop last ticked (`LOOP NOT RUNNING?` after 3 missed polls) and
marks items with no event for `limits.stale_after_minutes` as `STALE`.

### Traces and logs

- `~/.agent-sdlc/traces/<target>/<id>/` holds one JSONL transcript per agent session (every tool
  call, result and blocked call) and the full output of every install/verify command. Files are
  `0600`. Override with `--traces` or `AGENT_SDLC_TRACES`.
- `~/.agent-sdlc/logs/agent-sdlc.log` is the rotating log (14 days), each line tagged
  `[<target>#<id> <stage>]`. Override with `--logs` or `AGENT_SDLC_LOGS`.

### Parks you will see from the guardrails

- `policy` with "stopped after blocked tool calls": an agent tried to read outside its worktree
  or write a protected path, or hit `limits.max_denials_per_session` blocked calls. The comment
  quotes the attempts.
- `manifest`: the change edits `package.json` or a lockfile. Review the diff in the comment;
  removing the tag approves exactly that change, and install and verify run with it. A later
  different change parks again.
- `budget` mid-session: the agent was stopped when the item's token budget ran out.

Tooling config that verify executes (eslint/vite/postcss/tailwind/prettier config, `turbo.json`,
`.npmrc`, rc-style files such as `.prettierrc`, `.babelrc` and `.eslintrc.json`, `.config/**`,
`nest-cli.json`, `packages/eslint-config/**`, tsconfig files) is protected. Implementer writes to
gitignored paths (`node_modules/`, `dist/`) are denied and escalate.
Agent-written source and test code still runs on the host during verify. Container isolation is
a separate follow-up and must be in place before unattended runs.

To label decisions from abandoned PRs first: `uv run agent-sdlc label review --abandoned`.

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
