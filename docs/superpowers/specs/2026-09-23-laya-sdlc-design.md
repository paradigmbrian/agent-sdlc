# Laya SDLC — Multi-Agent Development Loop

- **Date:** 2026-09-23
- **Status:** Draft — awaiting review
- **Owner:** Brian (Paradigm Shift)

## 1. Purpose

A personal, internal tool that continuously picks up opt-in Azure DevOps work items, triages them, plans, implements, verifies, reviews, and opens pull requests against a target repository. A human approves every merge; the target repo's existing CI/CD deploys merged changes.

Laya (`nandhakishorm/laya`, a non-autoregressive "System 1" decision engine returning calibrated probabilities for typed questions) makes the fast judgment calls at stage transitions. Claude agents (via the Claude Agent SDK) do the generative work inside stages. Deterministic checks decide facts.

### 1.1 Decisions made during brainstorming

| Decision | Choice |
|---|---|
| Role of Laya | Decision / gating / routing layer, not a code generator |
| Scope | Generic system with per-target config; RallySource is the pilot target |
| Autonomy | Human-gated merge. Laya risk score is logged and advisory only |
| Work source / code host | Azure DevOps (Boards work items, Repos PRs, Pipelines CI/CD) |
| Runtime | Long-running Python orchestrator + Claude Agent SDK, local-first |
| Orchestration model | Explicit state machine with Laya gates at transitions |
| Model auth | Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) by default; API key optional |
| Push policy | Standing exception: system pushes only `laya/*` branches and opens PRs to `dev` on the pilot repo, via a dedicated identity |

### 1.2 Success criteria (v1)

1. An ADO work item tagged `laya` on the pilot repo progresses unattended from intake to an open PR against `dev`, with plan, test evidence, Laya decisions, and usage in the PR body.
2. Every park path (unclear item, protected path, red tests past retry budget, review blocking past budget, rate limit, infra error) leaves the item in a clearly explained, resumable state.
3. PR review comments trigger bounded revision cycles on the same branch.
4. No agent can write protected paths, run disallowed commands, read secrets, or push. This is enforced in code and covered by tests.
5. Every Laya decision is logged with inputs and probabilities; human outcomes are captured as calibration labels.

### 1.3 Out of scope (v1)

Self-generated work (from logs/TODOs/roadmap), auto-merge, hosting the orchestrator in Azure, concurrent multi-target runs, web dashboard.

## 2. Pilot target: RallySource

- ADO: org `MilesThurman`, project `CodvoMigration`, repo `RallySource` (`git@ssh.dev.azure.com:v3/MilesThurman/CodvoMigration/RallySource`).
- Stack: Turborepo TypeScript monorepo — `apps/rallysource-api` (NestJS, Prisma, Postgres), `apps/rallysource-web` and `apps/rallysource-teams` (React/Vite), `apps/ado-agent`, `packages/*`. npm workspaces (`npm@11.1.0`).
- CI/CD: Azure Pipelines build Docker images and deploy **on push** to `dev`/`qa`/`main`/`prod`. No PR validation pipeline observed. A merge to `dev` therefore deploys to the dev environment.
- Checks available: only `rallysource-api` has tests (`jest`, plus `test:e2e`, which needs a database and is excluded). `rallysource-web` and `rallysource-teams` have `type-check` (`tsc --noEmit`) and no tests. The API's `lint` script runs `eslint --fix` (it mutates files), so verify runs lint via `turbo run lint` and treats any resulting working-tree change as part of the diff.
- **The system never touches the local checkout at `~/Development/rallysource/repos/RallySource/`.** It maintains its own clone under `~/Development/paradigm/laya/workspaces/`.

## 3. Architecture

```
ADO Boards ──poll──▶ Intake adapter
                          │
                   ┌──────▼───────┐        ┌──────────────┐
                   │ Orchestrator │◀──────▶│ State store  │  SQLite (local) / Postgres
                   │ (state mach.)│        └──────────────┘
                   └──┬───┬───┬───┘
     Laya decisions ◀─┘   │   └─▶ ADO adapter (work items, branches, PRs, comments)
                          ▼
              Agent runner (Claude Agent SDK)
              planner · implementer · reviewer
                          │
              Workspace manager (clone + git worktree per item)
```

Python ≥ 3.10 (required by Laya). Package: `laya_sdlc`.

### 3.1 Components

| Module | Responsibility | Depends on |
|---|---|---|
| `laya_sdlc.decisions` | Wraps `laya.Router`. Registry of typed questions per gate; applies per-gate temperatures and thresholds; returns a `Decision` (answer, calibrated probs, confidence, `shadow` flag); logs every call. The only module importing `laya`. | laya, store |
| `laya_sdlc.orchestrator` | State machine. Transitions are pure functions `(ItemState, StageResult \| Decision) -> Transition`. Scheduler loop picks runnable items within concurrency/budget limits. Sole writer of item state. | all below |
| `laya_sdlc.agents` | Role definitions (system prompt, allowed tools, allowed paths, turn/token budget) and a runner that executes a role via the Claude Agent SDK in an isolated config dir. | claude-agent-sdk, policy |
| `laya_sdlc.policy` | Path policy (protected globs, allowlists) and command policy (allowlisted commands). Used by agent tool hooks and by the pre-push diff check. | — |
| `laya_sdlc.workspaces` | Maintains one bare/base clone per target; creates/removes `laya/<item-id>-<slug>` worktrees; runs target commands with timeouts; captures output. | git |
| `laya_sdlc.adapters.ado` | ADO REST: query/tag/comment work items, push branch (only adapter may push), create/update PR, read PR threads and status. | httpx, PAT |
| `laya_sdlc.store` | Persistence: items, stage attempts, decisions, labels, usage, control flags. SQLAlchemy; SQLite locally, Postgres optional. | SQLAlchemy |
| `laya_sdlc.targets` | Loads and validates `targets/<name>.yaml` (Pydantic v2). | pydantic |
| `laya_sdlc.cli` | `run`, `pause`, `resume`, `status`, `requeue`, `label`, `calibrate`. | all |

Interfaces between orchestrator and `decisions`, `agents`, `workspaces`, `adapters.ado` are Python protocols so each can be replaced with a fake in tests.

## 4. Work item lifecycle

States: `triage → plan → implement → verify → review → pr_open → awaiting_human → done | closed`, plus `parked:<reason>` from any state. Intake is not a state — it is the adapter poll that admits a work item into the machine; items enter at `triage`.

| Stage | Worker | Exit decision | Transitions |
|---|---|---|---|
| intake | ADO adapter polls items tagged `laya` in configured area path, not yet tracked | — | → triage |
| triage | Laya only | `kind` (choice: bug/feature/chore/question), `clarity` (score: unclear/partly clear/clear), `touches_protected` (noul), `size` (choice: small/medium/large) | kind=question, clarity<clear, touches_protected≠no, or size=large → `parked:needs_human` with an ADO comment explaining what's needed. Else → plan |
| plan | Planner agent, read-only tools | `plan_addresses_item` (noul), `plan_scope_ok` (noul) | Both yes → implement. Otherwise one replan with the decision as feedback, then `parked:plan_rejected` |
| implement | Implementer agent, write tools limited by path policy, TDD instructions | Hard checks: path policy, diff size ≤ `max_diff_lines` | Violation → `parked:policy`. Else → verify |
| verify | Workspace runs target `test`, `lint`, `typecheck`, `build` commands | Exit codes | All green → review. Red → implement with failure output (attempt+1). Attempts > `max_verify_retries` → `parked:red` |
| review | Reviewer agent, fresh context, read-only; sees item, plan, diff, verify output | `review_blocking` (noul), `risk` (score: low/medium/high, advisory) | Blocking=no → pr_open. Blocking=yes → implement with review notes (counts toward retry budget). Uncertain/shadow → pr_open, with the concern flagged in the PR body |
| pr_open | ADO adapter: pre-push policy re-check, push `laya/*`, open PR to target base branch | — | → awaiting_human |
| awaiting_human | Poll PR status and new comment threads | Per new comment: `comment_intent` (choice: change_request/question/approval/noise) | Merged → done. Abandoned → closed. change_request → implement (round+1; rounds > `max_pr_rounds` → `parked:pr_rounds`, keeping the triggering change request as the item's feedback). question → reply comment only; no code changes. A comment starting with `/laya` is always a change request, regardless of `comment_intent`. An uncertain comment (low confidence or shadow) gets a reply asking the human to prefix with `/laya` to request a change |

Rules:
- **Uncertainty routes to humans.** A `noul` question whose P(true) falls inside the `(1 − threshold, threshold)` band is reported as `unknown`; that, a confidence below the gate threshold, or a gate still in shadow mode all resolve to the human-routing branch. `touches_protected` (triage) passes triage only when its answer is `no`.
- **Facts are deterministic.** Tests, lint, build, path violations, and diff size never go through Laya.
- **All loops are bounded.** Replans (1), verify/review retries (`max_verify_retries`, default 3), PR rounds (`max_pr_rounds`, default 3).
- **Agent error results are failed attempts** (final review I2). If a planner, implementer or reviewer session ends with an error result (e.g. `error_max_turns`, `error_during_execution`) that is not a usage limit, no gate decision is taken: the stage is retried with attempt+1, and once attempt ≥ `max_verify_retries` the item goes to `parked:agent_error` ("agent did not finish: <subtype>"). `agent_error` is not a gate park: removing the tag retries the same stage with fresh counters and records no approval labels. The session's usage is still recorded.
- **A confident review clears a stale note** (M1): moving to `pr_open` without a reviewer-concern note removes any note left by an earlier uncertain review.
- **Parking always comments** on the ADO work item (and PR, if open) with what happened, what was tried, and what a human should do; the comment states the actual effect of removing the tag (ruling R13). It also adds tag `laya:parked`. A park of the `plan` gate includes the plan (escaped, truncated to ~6000 chars) and a park of the `review` gate includes the review notes, so the human can judge what they are approving (I3). **Park side-effect order (final review C1):** the tag is set first and `parked_tag_set` is recorded only after that succeeds; comments follow. A parked item without `parked_tag_set` is never treated as untagged-by-a-human — its park side effects are retried on later ticks instead. Only after the tag was confirmed set does a missing tag mean approval. Removing the tag or running `laya-sdlc requeue <id>` re-queues it: for a gate park (`needs_human`/`plan_rejected`) taken at the `triage`, `plan`, or `review` gate, this **approves proceeding past that gate** and records approval labels for it. For any other park — including a gate-reason park taken outside those three stages — it **resumes the parked stage with fresh counters** (attempt, replans, infra failures reset; PR rounds reset only for a `pr_rounds` park). Exception (I5): a `pr_rounds` park re-queues to `implement` with the kept change request as feedback and PR rounds reset to 0.

### 4.1 PR body contents

Linked work item; plan; files changed summary; verify command results; reviewer summary; Laya decisions per gate (answer, confidence, shadow flag); advisory risk score; usage (agent turns, budgeted tokens, and cache-read tokens shown separately) per stage and total.

## 5. Laya decisions and calibration

- `decisions` uses `laya.Router(preload=True)` and `router.predict(state, questions)`. `state` is a dict assembled per gate (e.g. work item title/description/acceptance criteria; plan text; diff summary; comment text).
- Question types used: `choice`, `score`, `noul` as defined by Laya.
- Per gate and target we store: temperature(s) per (question type, option count), a confidence threshold, and mode `shadow | active`. These calibrations live in the state DB (`calibrations` table), not in the target YAML.
- **Shadow by default.** The Laya README states the shipped checkpoints are over-confident (mean ECE 0.466 for `laya`; `laya-multilingual` ships without fitted temperatures). A gate runs in shadow until it has fitted temperatures and its held-out ECE ≤ `max_ece` (default 0.10). In shadow, the answer is logged but the transition takes the human-routing branch.
- **Labels.** `laya-sdlc label <gate>` presents examples for Brian to label. Bootstrap sources: closed ADO work items (triage gates), historical PRs and comment threads (review/comment gates). Human outcomes during operation are recorded as labels automatically. Examples: clarification given on a parked item means clarity was not `clear`. Merge versus abandon labels the plan and review gates — a merged PR (stage → `done`) adds `plan_addresses_item=true`, `plan_scope_ok=true`, and `review_blocking=false` approval labels, sourced from that item's latest logged decisions for those gates.
- **Fit.** `laya-sdlc calibrate [<gate>]` fits temperature scaling on a held-out split, reports ECE/accuracy, and promotes the gate to `active` only on explicit confirmation.

## 6. Agents

Roles are defined in `laya_sdlc/agents/roles/` (prompt + config):

| Role | Tools | Paths | Output |
|---|---|---|---|
| planner | Read, Glob, Grep, allowlisted read-only Bash | read: repo | Plan markdown + acceptance tests list |
| implementer | Read, Glob, Grep, Edit, Write, allowlisted Bash (target commands, read-only git) | write: repo minus protected globs | Working-tree changes + summary |
| reviewer | Read, Glob, Grep, read-only Bash | read: repo | Review notes (blocking issues vs nits) |

Runner requirements:
- **Isolated Claude config.** Each session runs with `CLAUDE_CONFIG_DIR` pointing to `~/.laya-sdlc/claude-config/`, `settingSources` empty, `strictMcpConfig`, auto memory disabled, claude.ai connectors disabled. Agents never load Brian's personal `~/.claude` settings, memory, or connectors.
- **Tool enforcement via SDK hooks / permission callback.** Every Write/Edit path is checked against the path policy. Every Bash command is checked against the command policy. Violations are denied and recorded.
- `cwd` is the item's worktree. Agents cannot reach other worktrees or the base clone. Glob patterns that are absolute, start with `~`, or contain `..` are path-checked like `path` (M3).
- **SDK Bash sandbox (final review C2).** Sessions pass `ClaudeAgentOptions.sandbox` = `{enabled: true, autoAllowBashIfSandboxed: false, allowUnsandboxedCommands: false, excludedCommands: [], network: {allowedDomains: [], allowUnixSockets: [], allowAllUnixSockets: false, allowLocalBinding: false}}`: no `dangerouslyDisableSandbox` escape hatch, no network for Bash, no Unix sockets (e.g. an ssh-agent).
- **Environment allowlist and scratch HOME (C2).** The SDK merges `os.environ` into the CLI's environment, so the runner sets every inherited variable outside the allowlist (`PATH`, `LANG`, `LC_ALL`, `TMPDIR`, `SHELL`, `USER`, `TERM`, `NVM_DIR`, `NVM_BIN`, plus `CLAUDE_CODE_ENTRYPOINT` which the SDK sets) to `""`, then sets its own variables and the active auth var. `HOME` is `<workspaces root>/<target>/home`, shared with the verify commands.
- Per-stage `max_turns`; token usage is tracked against the per-item `max_item_tokens` budget (§7.2), not a per-stage token cap. The runner reports usage from SDK result messages.

### 6.1 Auth

- `auth.mode: subscription` (default): orchestrator env provides `CLAUDE_CODE_OAUTH_TOKEN` generated by `claude setup-token`. That token can only make model requests.
- `auth.mode: api_key`: `ANTHROPIC_API_KEY`.
- Credentials are read from env vars first, then the macOS keychain in-process via `keyring` (item `<service>`, account `$USER`), so a keychain access list restricted with `-T <interpreter>` applies to the orchestrator's Python rather than `/usr/bin/security`. They are passed only to the SDK process env, never into prompts or agent-visible files.
- **Env blanking (ruling R11):** the agent subprocess env blanks whichever Claude auth var isn't the active mode's — in `subscription` mode an inherited `ANTHROPIC_API_KEY` is blanked, and in `api_key` mode an inherited `CLAUDE_CODE_OAUTH_TOKEN` is blanked — so an inherited value can never silently override the mode the orchestrator chose. The ADO PAT (and other orchestrator secrets) are always blanked in the agent env, regardless of auth mode. Since the final review (C2) this is subsumed by the environment allowlist in §6: every non-allowlisted inherited variable is blanked.
- Subscription use is for Brian's personal tool on his own login; confirm plan terms cover work on client repositories.

## 7. Error handling, budgets, safety

### 7.1 Failure handling
- Each transition is one DB transaction. Stage work is idempotent, keyed by `(item_id, stage, attempt)`. On restart, in-flight stages re-run from a reset worktree.
- External errors (ADO API, model API, Laya load) get exponential backoff with a cap, then `parked:infra` with the error attached. Other items continue. Since the final review (I1) **any** exception while stepping an item (including polling an `awaiting_human` item) is handled this way — logged with a traceback, backed off, parked `infra` on the 3rd consecutive failure — and never aborts the tick. Agent SDK failures (`ClaudeSDKError` and subclasses, or a session that ends without a result) surface as `AgentInfraError`.
- ADO errors during intake (`list_intake`) or requeue's tag check (`has_tag`) are logged and that item (or the whole intake pass) is skipped for the tick — they never abort the tick or crash the loop (ruling R15).
- **Usage-limit errors** (subscription window exhausted / 429) set a global `paused_until` from the reset time (or a 30-minute default probe) and suspend all agent work. Items are not parked or failed. Detection is narrow (I2): `ResultMessage.api_error_status == 429` / `ResultError.api_error_status == 429`, or the provider's own wording (`usage limit reached`, `hit your limit`, `rate_limit_error`, `API Error: 429`); free text such as "rate limiting" is not a usage limit. The reset time comes from a rejected `RateLimitEvent.resets_at` or a `usage limit reached|<epoch>` suffix (M11). Tokens the session spent before the limit are still added to item and daily usage.

### 7.2 Budgets (per target, configurable)
- Per item per stage: `max_turns`. Per item total: `max_item_tokens` (there is no per-stage token cap). Exceeding it → `parked:budget`.
- Budgeted tokens (item and daily) = input tokens including cache creation + output tokens. **Cache reads are tracked separately and do not count** (final review I4).
- Global daily: `max_daily_agent_turns`, `max_daily_tokens`. Exceeding one pauses intake of new items. In-flight items finish their current stage and then wait.
- `max_concurrent_items`: default 1 (shares Brian's subscription quota).
- Optional `run_window` (e.g. only run 19:00–07:00 local); outside the window, agent work is skipped for the tick.

### 7.3 Safety rails (enforced in code)
- **Protected paths** (RallySource defaults): `**/prisma/migrations/**`, `**/prisma/schema.prisma`, `infra/**`, `azure-pipelines*.yml`, `Dockerfile*`, `.env*`, `**/.env*`, `.husky/**`, `**/*.pem`, `**/*.key`, `sonar-project.properties`. Enforced at agent tool level and again by a pre-push diff check. Any hit parks the item. Matching is case-insensitive (M2), including `.git/`. A lint-fix commit made during verify re-checks both protected paths and `max_diff_lines` (M9).
- **Command policy:** allowlist only: target commands from config; `git status|diff|log|show`; `ls`, `cat`, `grep`, `rg`, `find`, `head`, `tail`, `wc`, `pwd`, `tree` (read-only, ruling R7). Denied forms of those: `find -exec/-execdir/-ok/-okdir/-delete/-fprint/-fprint0/-fprintf/-fls`, `rg --pre`/`--pre-glob` (any form incl. `--pre=`), `tree -o`/`-R`/`--fromfile` (also inside clustered short flags such as `-aR`), `git --output`. Denied: `git push|commit|remote|config`, package installs outside the target install command, `prisma migrate|db`, `az`, `docker`, `kubectl`, `curl`, `wget`, `ssh`. The workspace manager (not agents) creates commits. The ADO adapter (not agents) pushes.
- **Bash is confined to the worktree** (rulings R10/R12): every non-flag argument in an allowlisted Bash command (except the executable itself, argv[0]) and every value of a `--flag=value` argument must resolve — following symlinks — inside the item's worktree, or the call is denied as "path is outside the worktree". A `~`-prefixed path is always denied, without resolving it.
- **Fail-closed policy hook:** any exception raised while evaluating tool policy (a malformed path, a symlink loop, an unparseable command, etc.) is treated as a denial, not a pass-through.
- **Push scope:** the adapter refuses any ref not matching `refs/heads/laya/*`. The ADO identity has no push rights to `dev`/`qa`/`main`/`prod` (branch policy set up by Brian, §9).
- **`--dry-run-push` (ruling R9):** makes `push_branch`, `create_pr`/`update_pr`, `comment_pr`/`reply_pr`, and `delete_branch` no-ops; a dry-run PR polls with id `0` and status `active` with no comments. Work-item comments and tags (parking, `laya:parked`) stay real — except the "plan for PR !N" comment, which is skipped for the dry-run PR id `0` (M6) — so the park/approve loop still works end to end without ever touching the real PR or pushing a branch.
- **Secrets:** worktrees never contain the repo's `.env`. Tests use an optional target-provided `env_template` with non-secret values. PAT and model credentials are never exposed to agents. Orchestrator git calls (fetch, clone, worktree, commit, push) run with `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1` so the user's credential helpers and `url.*.insteadOf` rewrites are never used; auth is only the per-command `http.extraheader`. Verify/install commands and git get the scratch `HOME` and the safe-env allowlist (no `SSH_AUTH_SOCK`, no cloud or token variables).
- **Kill switch:** `laya-sdlc pause` sets a DB flag. The scheduler stops dispatching and in-flight agent sessions are interrupted at the next turn. `resume` clears it.
- **Branch hygiene:** worktrees are removed when an item reaches done/closed. The remote `laya/*` branch is deleted by the adapter after the PR closes.

## 8. Target configuration

`targets/rallysource.yaml`:

```yaml
name: rallysource
ado:
  org: MilesThurman
  project: CodvoMigration
  repo: RallySource
  intake_tag: laya
  base_branch: dev
  branch_prefix: laya/
repo:
  # clone_url is derived, not configured: https://dev.azure.com/<org>/<project>/_git/<repo>,
  # pushed with a per-command `http.extraheader` carrying the PAT (never written to git config).
  install: npm ci
  commands:
    test: npm run test --workspace=apps/rallysource-api
    lint: npm run lint
    typecheck: npm run type-check --workspace=apps/rallysource-web --workspace=apps/rallysource-teams
    build: npm run build
  command_timeout_s: 1200
  env_template: null
policy:
  protected_paths: [ ...see §7.3... ]
  max_diff_lines: 600
limits:
  max_concurrent_items: 1
  max_verify_retries: 3
  max_pr_rounds: 3
  max_turns: {plan: 30, implement: 80, review: 30}
  max_item_tokens: 2000000
  max_daily_agent_turns: 400
  run_window: null   # e.g. {start: "19:00", end: "07:00"} to run only in that window
auth:
  mode: subscription
laya:
  model: auto
  max_ece: 0.10
  # gate temperatures/thresholds/mode are not in this file — they live in the state DB's
  # `calibrations` table, written by `laya-sdlc calibrate` (see §5).
```

## 9. Setup Brian performs (not automated)

1. Create a dedicated ADO identity or PAT scoped to: Work Items (read/write), Code (read/write), Pull Requests (read/write). Store in keychain.
2. ADO branch policies on `dev`, `qa`, `main`, `prod`: deny direct push for the laya identity; require PR with Brian as required reviewer.
3. Generate `CLAUDE_CODE_OAUTH_TOKEN` via `claude setup-token`; store in keychain.
4. Confirm that `npm run test --workspace=apps/rallysource-api` passes on `dev` without a database or `.env`. If not, provide a non-secret `env_template` or a narrower test command.

## 10. Testing strategy

- **Unit (pytest):** state-machine transitions (every stage × outcome, including unknown/low-confidence/shadow → human); path and command policy; budget accounting; target config validation.
- **Decisions:** fake decider for unit tests; slow-marked integration tests running real `laya.Router` on fixture items to verify wiring, not accuracy.
- **Adapters:** ADO adapter against recorded HTTP fixtures. No live ADO in automated tests.
- **Agents:** fake runner that applies canned diffs/outputs. One slow-marked real-SDK test against a toy repo verifying tool-policy hooks deny protected writes and disallowed commands.
- **End-to-end (local):** fixture git repo with its own test command + fake ADO + fake agent runner + real Laya. Covers happy path to PR open and each park path.
- **Live smoke (manual):** one real opt-in RallySource item with `--dry-run-push` (everything except push; prints PR body), then one real run.
- Project checks: `pytest`, `ruff`, `mypy`.

## 11. Risks and open items

- **Laya accuracy on SDLC text is unknown.** Mitigation: shadow mode, labels, calibration. Worst case, gates stay in shadow and the system behaves as a propose-with-human-checkpoints assistant.
- **Subscription usage limits** will throttle throughput and compete with interactive use. Mitigation: concurrency 1, quiet hours, pause-until-reset.
- **Pilot is a live client system with deploy-on-merge to dev.** Mitigation: human-gated merge, protected paths, branch policies.
- **Monorepo verify cost:** full turbo build/test per attempt may be slow. v1 runs the full command set; scoping to affected workspaces (`turbo --filter`) is a later optimization.
- **Verify commands execute repo code outside the SDK sandbox.** The target's install/test/lint/typecheck/build commands run as ordinary subprocesses with only a scratch `HOME`, the environment allowlist and no credential variables (§7.3). They are not network- or filesystem-isolated, so code from a PR branch can still read files the user can read (for example files under the real home directory by absolute path, or keychain items — by launching the trusted interpreter or triggering a prompt the user might approve). Full isolation (a container or VM per run) is a design decision for Brian before any unattended run.
- **Thin test coverage in the pilot:** web and teams have no tests, so verify there is typecheck + lint + build only. The reviewer agent and the human merge gate carry more weight for frontend changes.
