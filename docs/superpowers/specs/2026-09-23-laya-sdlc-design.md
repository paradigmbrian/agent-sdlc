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

States: `intake → triage → plan → implement → verify → review → pr_open → awaiting_human → done | closed`, plus `parked:<reason>` from any state.

| Stage | Worker | Exit decision | Transitions |
|---|---|---|---|
| intake | ADO adapter polls items tagged `laya` in configured area path, not yet tracked | — | → triage |
| triage | Laya only | `kind` (choice: bug/feature/chore/question), `clarity` (score: unclear/partly clear/clear), `touches_protected` (noul), `size` (choice: small/medium/large) | kind=question, clarity<clear, touches_protected≠no, or size=large → `parked:needs_human` with an ADO comment explaining what's needed. Else → plan |
| plan | Planner agent, read-only tools | `plan_addresses_item` (noul), `plan_scope_ok` (noul) | Both yes → implement. Otherwise one replan with the decision as feedback, then `parked:plan_rejected` |
| implement | Implementer agent, write tools limited by path policy, TDD instructions | Hard checks: path policy, diff size ≤ `max_diff_lines` | Violation → `parked:policy`. Else → verify |
| verify | Workspace runs target `test`, `lint`, `typecheck`, `build` commands | Exit codes | All green → review. Red → implement with failure output (attempt+1). Attempts > `max_verify_retries` → `parked:red` |
| review | Reviewer agent, fresh context, read-only; sees item, plan, diff, verify output | `review_blocking` (noul), `risk` (score: low/medium/high, advisory) | Blocking=no → pr_open. Blocking≠no → implement with review notes (counts toward retry budget) |
| pr_open | ADO adapter: pre-push policy re-check, push `laya/*`, open PR to target base branch | — | → awaiting_human |
| awaiting_human | Poll PR status and new comment threads | Per new comment: `comment_intent` (choice: change_request/question/approval/noise) | Merged → done. Abandoned → closed. change_request → implement (round+1; rounds > `max_pr_rounds` → `parked:pr_rounds`). question → reply comment only; no code changes |

Rules:
- **Uncertainty routes to humans.** A `noul` answer of `unknown`, a confidence below the gate threshold, or a gate still in shadow mode all resolve to the human-routing branch.
- **Facts are deterministic.** Tests, lint, build, path violations, and diff size never go through Laya.
- **All loops are bounded.** Replans (1), verify/review retries (`max_verify_retries`, default 3), PR rounds (`max_pr_rounds`, default 3).
- **Parking always comments** on the ADO work item (and PR, if open) with what happened, what was tried, and what a human should do. It also adds tag `laya:parked`. Removing the tag or running `laya-sdlc requeue <id>` re-queues it at the stage it parked in.

### 4.1 PR body contents

Linked work item; plan; files changed summary; verify command results; reviewer summary; Laya decisions per gate (answer, confidence, shadow flag); advisory risk score; usage (agent turns, input/output tokens) per stage and total.

## 5. Laya decisions and calibration

- `decisions` uses `laya.Router(preload=True)` and `router.predict(state, questions)`. `state` is a dict assembled per gate (e.g. work item title/description/acceptance criteria; plan text; diff summary; comment text).
- Question types used: `choice`, `score`, `noul` as defined by Laya.
- Per gate and target we store: temperature(s) per (question type, option count), a confidence threshold, and mode `shadow | active`.
- **Shadow by default.** The Laya README states the shipped checkpoints are over-confident (mean ECE 0.466 for `laya`; `laya-multilingual` ships without fitted temperatures). A gate runs in shadow until it has fitted temperatures and its held-out ECE ≤ `max_ece` (default 0.10). In shadow, the answer is logged but the transition takes the human-routing branch.
- **Labels.** `laya-sdlc label <gate>` presents examples for Brian to label. Bootstrap sources: closed ADO work items (triage gates), historical PRs and comment threads (review/comment gates). Human outcomes during operation are recorded as labels automatically. Examples: clarification given on a parked item means clarity was not `clear`. Merge versus abandon labels the plan and review gates.
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
- `cwd` is the item's worktree. Agents cannot reach other worktrees or the base clone.
- Per-stage `max_turns` and token budget. The runner reports usage from SDK result messages.

### 6.1 Auth

- `auth.mode: subscription` (default): orchestrator env provides `CLAUDE_CODE_OAUTH_TOKEN` generated by `claude setup-token`. That token can only make model requests.
- `auth.mode: api_key`: `ANTHROPIC_API_KEY`.
- Credentials are read from the macOS keychain (or env) by the orchestrator and passed only to the SDK process env, never into prompts or agent-visible files.
- Subscription use is for Brian's personal tool on his own login; confirm plan terms cover work on client repositories.

## 7. Error handling, budgets, safety

### 7.1 Failure handling
- Each transition is one DB transaction. Stage work is idempotent, keyed by `(item_id, stage, attempt)`. On restart, in-flight stages re-run from a reset worktree.
- External errors (ADO API, model API, Laya load) get exponential backoff with a cap, then `parked:infra` with the error attached. Other items continue.
- **Usage-limit errors** (subscription window exhausted / 429) set a global `paused_until` from the reset time (or a 30-minute default probe) and suspend all agent work. Items are not parked or failed.

### 7.2 Budgets (per target, configurable)
- Per item per stage: `max_turns`, `max_tokens`. Per item total: `max_item_tokens`. Exceeding one → `parked:budget`.
- Global daily: `max_daily_agent_turns`, `max_daily_tokens`. Exceeding one pauses intake of new items. In-flight items finish their current stage and then wait.
- `max_concurrent_items`: default 1 (shares Brian's subscription quota).
- Optional `quiet_hours` window (e.g. only run 19:00–07:00 local).

### 7.3 Safety rails (enforced in code)
- **Protected paths** (RallySource defaults): `**/prisma/migrations/**`, `**/prisma/schema.prisma`, `infra/**`, `azure-pipelines*.yml`, `Dockerfile*`, `.env*`, `**/.env*`, `.husky/**`, `**/*.pem`, `**/*.key`, `sonar-project.properties`. Enforced at agent tool level and again by a pre-push diff check. Any hit parks the item.
- **Command policy:** allowlist only: target commands from config; `git status|diff|log|show`; `ls`, `cat`, `grep`, `rg`, `find` (read-only). Denied: `git push|commit|remote|config`, package installs outside the target install command, `prisma migrate|db`, `az`, `docker`, `kubectl`, `curl`, `wget`, `ssh`. The workspace manager (not agents) creates commits. The ADO adapter (not agents) pushes.
- **Push scope:** the adapter refuses any ref not matching `refs/heads/laya/*`. The ADO identity has no push rights to `dev`/`qa`/`main`/`prod` (branch policy set up by Brian, §9).
- **Secrets:** worktrees never contain the repo's `.env`. Tests use an optional target-provided `env_template` with non-secret values. PAT and model credentials are never exposed to agents.
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
  clone_url: git@ssh.dev.azure.com:v3/MilesThurman/CodvoMigration/RallySource
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
  quiet_hours: null
auth:
  mode: subscription
laya:
  model: auto
  max_ece: 0.10
  gates: {}   # temperatures/thresholds/mode written by `calibrate`
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
- **Thin test coverage in the pilot:** web and teams have no tests, so verify there is typecheck + lint + build only. The reviewer agent and the human merge gate carry more weight for frontend changes.
