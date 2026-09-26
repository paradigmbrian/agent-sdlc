# Agent SDLC — Multiple Forges and Parallel Targets

- **Date:** 2026-09-26
- **Status:** Draft — awaiting review
- **Owner:** Brian (Paradigm Shift)
- **Builds on:** `2026-09-23-agent-sdlc-design.md` (the base spec) and
  `2026-09-24-trace-and-guardrails-design.md` (the guardrails spec)

## 1. Purpose

Let each target choose where its work comes from and where its PRs go: Azure DevOps (Work Items +
ADO Repos) as today, or GitHub (Issues + GitHub PRs). Make the repository explicit per target, and
run several targets from one loop at the same time. The first GitHub target is
`github.com/paradigmbrian/triathlon-agent`.

### 1.1 Decisions made during brainstorming

| Decision | Choice |
|---|---|
| Provider abstraction | One `ForgePort` (issue intake + code host) with `AdoForge` and `GitHubForge` implementations. Tracker and code host are not split; mixing (ADO items + GitHub repo) is out of scope |
| Parallel model | One process, one DB, many targets. Kill switch, Claude usage-limit pause and daily budget are shared (one subscription) |
| Existing state DB | Start fresh. No migration; the old DB file is left on disk untouched |
| GitHub identity | A private GitHub App installed only on the target repo; acts as `<slug>[bot]` |
| GitHub PR feedback | Conversation comments, inline review comments and review bodies go through `comment_intent`; `/agent` and a "changes requested" review are always change requests |
| Concurrency mechanism | One thread per target, each with its own event loop; a shared semaphore caps concurrent agent sessions |

### 1.2 Success criteria

1. A GitHub issue labeled `agent` on triathlon-agent progresses from intake to an open PR against
   `main` that says `Closes #<issue>`, with the same PR body contents as the ADO path (base spec
   §4.1).
2. Every park path works on GitHub: the item gets the `agent:parked` label and a comment worded for
   GitHub, and removing the label has the same effect as removing the ADO tag.
3. PR feedback on GitHub (comment, inline comment, review body, changes-requested review, `/agent`)
   starts bounded revision rounds on the same branch.
4. `agent-sdlc run` with `agent-sdlc.yaml` listing RallySource and triathlon serves both targets
   from one process. An error in one target never affects the other.
5. The same external id under two targets never collides in the store, workspaces, traces or CLI.
6. All ADO behaviour from the base and guardrails specs is unchanged. Existing tests pass after the
   renames.
7. Everything above is covered by unit and e2e tests using fakes and recorded HTTP.

### 1.3 Out of scope

- Mixing providers inside one target (ADO work items with a GitHub repo, or the reverse).
- GitHub webhooks. Polling stays at 60 seconds.
- GitHub Projects, milestones, assignees, or GitHub Actions status as a verify signal.
- Per-target Laya calibrations. Calibrations stay shared (§4.1).
- Migrating the existing state DB.
- Container isolation of verify (still the open risk from the base spec §11).

## 2. Configuration

### 2.1 Global config: `agent-sdlc.yaml`

New file at the repository root. It holds what belongs to the one Claude subscription and the one
process:

```yaml
targets:
  - targets/rallysource.yaml
  - targets/triathlon.yaml
auth:
  mode: subscription            # subscription | api_key
laya:
  model: auto
  max_ece: 0.10
  default_threshold: 0.8
limits:
  max_daily_agent_turns: 400
  max_daily_tokens: 20000000
  max_concurrent_sessions: 1    # agent sessions across all targets
  run_window: null              # e.g. {start: "19:00", end: "07:00"}
```

- Paths in `targets` are relative to the config file.
- Target `name`s must be unique across the list; loading fails otherwise.
- CLI: `--config` (default `<repo root>/agent-sdlc.yaml`, env `AGENT_SDLC_CONFIG`) loads the full
  set. `--target <file>` loads one target with global defaults (the values above) and ignores
  `--config`. Every command accepts either.

### 2.2 Target config

Target files lose `auth`, `laya`, `max_daily_agent_turns`, `max_daily_tokens` and `run_window`. A
target file that still contains any of them fails validation with a message naming the key and
`agent-sdlc.yaml`, so an old file cannot silently diverge from the global settings.

```yaml
name: triathlon
forge:
  kind: github
  owner: paradigmbrian
  repo: triathlon-agent
  app_id: 123456
  installation_id: null         # null: looked up via GET /repos/{owner}/{repo}/installation
intake:
  label: agent
  parked_label: "agent:parked"
repo:
  base_branch: main
  branch_prefix: agent/
  clone_url: null               # null: derived from forge
  install:
    - uv sync --all-packages
    - npm ci --prefix web
  commands:
    test: uv run pytest
    lint: uv run ruff check .
    typecheck: uv run mypy
    web_lint: npm run lint --prefix web
    web_test: npm run test --prefix web
    web_build: npm run build --prefix web
  command_timeout_s: 1200
  env_template: null
policy:
  protected_paths:
    - "migrations/**"
    - ".env*"
    - "**/.env*"
    - "docker-compose.yml"
    - "setup.sh"
    - "conftest.py"             # root pytest config; package conftests are test code
    - ".claude/**"
    - ".github/**"
    - "web/*.config.*"          # vite, eslint, playwright
    - "web/tsconfig*.json"
    - "**/*.pem"
    - "**/*.key"
  manifest_paths:
    - "**/pyproject.toml"
    - "uv.lock"
    - "web/package.json"
    - "web/package-lock.json"
  max_diff_lines: 600
limits:
  max_concurrent_items: 1
  max_verify_retries: 3
  max_pr_rounds: 3
  max_turns: {plan: 30, implement: 80, review: 30}
  max_item_tokens: 2000000
  max_denials_per_session: 5
  stale_after_minutes: 120
```

Rules:

- `forge` is a Pydantic discriminated union on `kind`:
  - `ado`: `org`, `project`, `repo`, optional `pat_secret` (keychain service name, default
    `agent-sdlc-ado-pat`).
  - `github`: `owner`, `repo`, `app_id`, optional `installation_id`, optional `api_url` (default
    `https://api.github.com`, for testing only).
- The clone URL is `repo.clone_url` if set, else derived: ADO
  `https://dev.azure.com/<org>/<project>/_git/<repo>`, GitHub
  `https://github.com/<owner>/<repo>.git`.
- `intake.label` and `intake.parked_label` replace `ado.intake_tag` and `ado.parked_tag`.
  `repo.base_branch` and `repo.branch_prefix` replace their `ado.*` equivalents.
- `repo.install` accepts a string or a list of strings. A list runs in order and stops at the first
  failure; the `CommandResult` for install concatenates their output and reports the failing exit
  code. No install entry needs shell operators.
- The agent `CommandPolicy` allowlist is built from each install entry and each command, as today.
- `targets/rallysource.yaml` is rewritten to the new shape with identical values.

### 2.3 Secrets

| Secret | Keychain service | Env var (read first) |
|---|---|---|
| ADO PAT | `forge.pat_secret` (default `agent-sdlc-ado-pat`) | `AGENT_SDLC_ADO_PAT` |
| GitHub App private key (PEM) | `agent-sdlc-github-app-<app_id>` | `AGENT_SDLC_GITHUB_APP_KEY` |
| Claude token / API key | unchanged | unchanged |

All secrets are read in-process via `keyring` (base spec §6.1) and are blanked from agent and verify
environments by the existing environment allowlist. The private key and installation tokens are
never written to disk, logs, events or comments.

## 3. The forge port

### 3.1 Interface

`ports.ForgePort` replaces `AdoPort`. "Item" means an ADO work item or a GitHub issue; ids are the
provider's own numbers.

```python
class ForgePort(Protocol):
    kind: str                     # "ado" | "github"
    label_word: str               # "tag" | "label", for human-facing text
    def list_intake(self) -> list[WorkItem]: ...
    def list_closed(self, limit: int) -> list[WorkItem]: ...
    def get_item(self, id: int) -> WorkItem: ...
    def comment_item(self, id: int, html: str) -> None: ...
    def set_label(self, id: int, label: str, present: bool) -> None: ...
    def has_label(self, id: int, label: str) -> bool: ...
    def git_auth_header(self) -> str: ...
    def push_branch(self, worktree: Path, branch: str) -> None: ...
    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int: ...
    def update_pr(self, pr_id: int, body: str) -> None: ...
    def pr_status(self, pr_id: int) -> str: ...          # active | completed | abandoned
    def pr_comments(self, pr_id: int) -> list[PrComment]: ...
    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None: ...
    def comment_pr(self, pr_id: int, text: str) -> None: ...
    def delete_branch(self, branch: str) -> None: ...
    def pr_ref(self, pr_id: int) -> str: ...             # "!12" | "#12"
```

`PrComment` gains two fields:

```python
kind: str = "thread"             # ado: thread | github: conversation | review_comment | review
changes_requested: bool = False  # github: a review in state CHANGES_REQUESTED
```

`PrComment.key` becomes `f"{kind}:{thread_id}:{comment_id}"` so ids from different GitHub
endpoints cannot collide. `reply_pr` takes the whole comment so each forge can choose where the
reply goes.

`ForgeError` (in `agent_sdlc.adapters.errors`) replaces `AdoError` in the scheduler's
`_INFRA_ERRORS`. Both forges raise it for HTTP and protocol failures. Its message never contains a
token or key.

### 3.2 `AdoForge`

Today's `AdoClient`, renamed and adapted to the port with no behaviour change: `get_work_item` →
`get_item`, `comment_work_item` → `comment_item`, `set_tag`/`has_tag` → `set_label`/`has_label`,
`reply_pr` reads `thread_id` and `comment_id` from the comment. `git_auth_header()` returns the
existing Basic PAT header. `label_word = "tag"`, `pr_ref(n) = f"!{n}"`.

### 3.3 `GitHubForge`

httpx client against the GitHub REST API (`Accept: application/vnd.github+json`, API version header
pinned to the version current at implementation time).

**Authentication (GitHub App).**

- A JWT signed RS256 with the App private key (`iss` = `app_id`, `iat` = now − 60 s, `exp` = now +
  9 min) is exchanged at `POST /app/installations/{installation_id}/access_tokens` for an
  installation token. If `installation_id` is null, it is looked up once via
  `GET /repos/{owner}/{repo}/installation` and cached for the process.
- The token is cached and refreshed when fewer than 5 minutes remain. Every API call and every
  `git_auth_header()` call gets the current token.
- A 401 response forces one refresh and one retry of that request; a second 401 raises
  `ForgeError`.
- `git_auth_header()` returns `Authorization: Basic base64("x-access-token:<token>")`.
- The App's bot login (`<slug>[bot]`, from `GET /app` with the JWT) is cached and used to skip the
  App's own comments.
- Signing uses PyJWT with the `crypto` extra (new dependency).

**Items.**

- `list_intake`: `GET /repos/{o}/{r}/issues?state=open&labels=<intake.label>` (paginated). Entries
  with a `pull_request` key are dropped.
- `list_closed`: closed issues with the intake label, newest first, up to `limit`, PRs dropped.
- `WorkItem` mapping: `title`; `description` = body with any acceptance-criteria section removed;
  `acceptance_criteria` = the text under a heading matching `#{1,6} acceptance criteria`
  (case-insensitive) up to the next heading of the same or higher level, else empty;
  `work_item_type` = `Bug` if the issue has a `bug` label, else `Issue`; `tags` = label names;
  `url` = `html_url`.
- `comment_item`: `POST /issues/{n}/comments` with the HTML body (GitHub renders
  `<p> <b> <code> <pre> <ul> <li>`, the only tags `reporting.py` emits).
- `set_label` adds via `POST /issues/{n}/labels` and removes via `DELETE /issues/{n}/labels/{name}`
  (a 404 on removal is success). `has_label` reads the issue's labels.

**Pull requests.**

- `push_branch`: `git push` over HTTPS with the auth header, refusing any ref outside
  `refs/heads/<branch_prefix>*` (same check as ADO).
- `create_pr`: `POST /pulls` with `head=<branch>`, `base=<repo.base_branch>`, and the body with
  `Closes #<item_id>` appended on its own line. Returns the PR number.
- `update_pr`: `PATCH /pulls/{n}` with the new body (with `Closes #<item_id>` kept).
- `pr_status`: `merged` → `completed`; `state == closed` and not merged → `abandoned`; otherwise
  `active` (a reopened PR is `active` again).
- `pr_comments` merges three sources, skipping the bot login and empty bodies:
  - conversation comments, `GET /issues/{n}/comments` → `kind=conversation`, `thread_id=0`;
  - inline review comments, `GET /pulls/{n}/comments` → `kind=review_comment`,
    `thread_id=in_reply_to_id or id`;
  - reviews, `GET /pulls/{n}/reviews` with a non-empty body or state `CHANGES_REQUESTED` →
    `kind=review`, `thread_id=0`, `changes_requested` set from the state. A changes-requested
    review with an empty body gets the content `"(changes requested with no summary)"`.
- `reply_pr`: `review_comment` → `POST /pulls/{n}/comments/{id}/replies`; otherwise a new
  conversation comment that quotes the first 300 characters of the original and mentions its
  author.
- `comment_pr`: `POST /issues/{n}/comments`.
- `delete_branch`: `DELETE /git/refs/heads/<branch>` after the prefix check; 404 and 422 (already
  gone) are success.
- `label_word = "label"`, `pr_ref(n) = f"#{n}"`.

**Dry run.** Same rule as ADO (base spec §7.3): `push_branch`, `create_pr`, `update_pr`,
`comment_pr`, `reply_pr` and `delete_branch` are no-ops; a dry-run PR has id `0`, status `active` and
no comments. Issue labels and issue comments stay real, except the plan comment for PR `0`.

**Rate limits.** A 429, or a 403 with `x-ratelimit-remaining: 0`, raises `ForgeError` with the
reset time in the message. The scheduler's per-item backoff applies. It never sets the global
`paused_until`, which is reserved for Claude usage limits.

### 3.4 Orchestrator changes

- `transitions.classify_comment` treats `c.changes_requested` like a `/agent` prefix: always
  `change_request`. The `/agent` slash-command label is recorded for both; the label `source` is
  `slash_command` or `changes_requested`.
- `reporting.py` functions take the forge's `label_word`, `pr_ref` and the configured
  `intake.parked_label` instead of hard-coding "tag", `agent:parked` and `!N`.
- `StageExecutor` and `Scheduler` take `forge: ForgePort` instead of `ado: AdoPort` and read
  `intake.*` and `repo.*` instead of `ado.*`.
- `Workspaces` takes a callable `git_auth: Callable[[], str]` (bound to `forge.git_auth_header`)
  instead of a fixed header string, and reads `repo.base_branch`.

## 4. State and paths

### 4.1 Store (fresh DB)

- `items.id` becomes an autoincrement surrogate key. New column `external_id` (int), with a unique
  constraint on `(target, external_id)`. `Item` gains `external_id`; every forge call, branch name,
  worktree path and trace path uses `external_id`. Events, decisions and the store's lookups keep
  using the surrogate `id`.
- `Store.get_by_ref(target, external_id)` and `Store.find_external(external_id)` (all targets)
  support the CLI.
- `labels` gains `target` (nullable for labels not tied to an item's target; every label written by
  this system sets it).
- Shared across targets: `calibrations`, `daily_usage`, and the flags `paused` and `paused_until`.
- Per-target flags: `paused:<target>`, `last_tick:<target>`, `busy:<target>`.
- SQLite runs in WAL mode with a 30-second busy timeout, set on each new connection.
- The default DB path changes to `~/.agent-sdlc/agent-sdlc-v2.db`. The previous file is not opened
  or modified.

### 4.2 Paths and logs

- Worktrees: `<workspaces>/<target>/wt/<external_id>` (the root was already per target).
- Traces: `~/.agent-sdlc/traces/<target>/<external_id>/`.
- Log context: `[<target>#<external_id> <stage>]`.

### 4.3 CLI

- Item references are `<target>#<id>` (e.g. `triathlon#12`). A bare `12` is accepted when exactly one
  target has an item with that external id; otherwise the command exits 1 and lists the matches.
  Applies to `trace` and `requeue`.
- `status` and `metrics` cover all configured targets, grouped by target, and accept
  `--target-name <name>` to filter. `status` shows each target's last tick and busy step.
- `pause` and `resume` act globally; `pause --target-name <name>` and
  `resume --target-name <name>` act on one target. `resume` without a name clears `paused` and
  `paused_until` only, not per-target pauses.
- `label triage` and `calibrate` run across all targets (calibrations are shared); `label triage`
  accepts `--target-name` to choose which forge's closed items to label.

## 5. Runtime

### 5.1 Supervisor

`orchestrator/supervisor.py` loads the global config, builds one runtime per target (forge,
Workspaces, PathPolicy, CommandPolicy, ClaudeAgentRunner, StageExecutor, Scheduler) and runs each
target's `Scheduler.run_forever()` in its own thread with its own event loop
(`asyncio.run` inside the thread). `run --once` runs one tick per target in parallel threads and
joins them.

- A target thread that exits with an exception is logged with a traceback and restarted after 60
  seconds. The main thread joins the threads and exits on Ctrl-C; an interrupted step re-runs from
  a reset worktree on restart (base spec §7.1).
- Startup failures for one target (bad config, missing secret, token minting fails) are logged and
  that target is retried every 60 seconds; the other targets start normally.

### 5.2 Shared resources

- **Decider.** One `Decider` and one `LayaPredictor` for the process. `predict` runs under a
  `threading.Lock`.
- **Session slots.** `SessionSlots`, a `threading.BoundedSemaphore(max_concurrent_sessions)`, is
  acquired by `StageExecutor._run_agent` before `runner.run` and released after it, including on
  exceptions. Acquisition polls with a 5-second timeout and re-checks the kill switch between
  polls; if paused, the step raises `AgentInterrupted` without a partial result. Verify commands,
  intake and PR polling do not take a slot.
- **Daily budget.** `_agent_work_allowed` reads the shared `daily_usage` and the global limits.
  Targets check it independently before each step, so the combined overshoot is bounded by one step
  per target.
- **Kill switch.** `tick()` returns early if `paused`, `paused_until` or `paused:<target>` is set.
  The runner's `should_stop` checks `paused` and `paused:<target>`.
- **Usage limit.** A `UsageLimitError` in any target sets the global `paused_until`, so all targets
  stop dispatching agent work until it passes (unchanged semantics, now shared).

## 6. Error handling summary

| Failure | Handling |
|---|---|
| GitHub token minting fails (bad key, App uninstalled, wrong `app_id`) | `ForgeError`; the item's 3-strike backoff then `parked:infra`. During intake, that target skips intake for the tick |
| Token expires during a long step | Refreshed 5 minutes before expiry and fetched per call; a 401 forces one refresh and retry |
| GitHub rate limit | `ForgeError` with the reset time; per-item backoff for that target only |
| Issue deleted or transferred (404) | `ForgeError`; eventually `parked:infra` with the error in the note |
| Exception in one target's thread | Logged; thread restarted after 60 s; other targets unaffected |
| SQLite busy | Waits up to 30 s (busy timeout); after that, the step's exception path applies |

## 7. Setup performed by Brian (not automated)

1. Create a private GitHub App on the `paradigmbrian` account: webhook disabled; repository
   permissions Contents: read & write, Issues: read & write, Pull requests: read & write,
   Metadata: read. Install it on `triathlon-agent` only. Note the App id.
2. Generate a private key and store it:
   `security add-generic-password -s agent-sdlc-github-app-<app_id> -a $USER -T <resolved interpreter path> -w "$(cat <key>.pem)"`,
   then delete the `.pem`.
3. Add a branch ruleset on `main`: require a pull request with 1 approval, block force pushes and
   deletion, no bypass for the App.
4. Create the labels `agent` and `agent:parked` on the repo.
5. Pilot check: in a clean clone of `main` with no `.env` and no running Postgres, confirm the
   install list and every command in `targets/triathlon.yaml` pass (db-marked tests skip). Narrow
   any command that fails for environmental reasons.
6. Fill `app_id` in `targets/triathlon.yaml`.

## 8. Testing

- **Config:** `forge` union for both kinds; rejection of target files carrying global keys;
  duplicate target names; `install` as string and list; `--target` alone; relative target paths.
- **GitHubForge (respx):** JWT claims and signature against a throwaway RSA key generated in the
  test; installation lookup; token caching, early refresh and the 401 retry; intake drops PRs;
  acceptance-criteria extraction; `Closes #N` on create and update; status mapping (merged, closed,
  reopened); all three comment sources with bot filtering and `changes_requested`; reply routing
  per kind; branch-prefix refusal on push and delete; dry-run no-ops; rate-limit errors; no token in
  any `ForgeError` message.
- **AdoForge:** existing ADO tests, renamed to the port methods.
- **Store:** same external id under two targets; `get_by_ref`/`find_external`; per-target flags;
  label target; WAL and busy timeout set.
- **Transitions/reporting:** changes-requested review is a change request; comment wording per
  forge and configured label.
- **Supervisor:** two fake targets tick in parallel threads; one session slot shared between them;
  global and per-target pause (including while waiting for a slot); a crashing target thread is
  restarted and the other keeps ticking; one target's startup failure does not block the other.
- **CLI:** `<target>#<id>` parsing, ambiguous bare ids, grouped `status`.
- **E2E:** the existing pipeline test on a fake GitHub forge; a two-target run (fake ADO + fake
  GitHub) taking both items to `awaiting_human` in one Supervisor run.
- **Manual smoke:** `run --once --dry-run-push --target targets/triathlon.yaml` on one real issue;
  then one real run; then both targets under `agent-sdlc.yaml`.
- Project checks: `pytest`, `ruff check .`, `mypy` (strict).

## 9. Risks and open items

- **Verify on the host.** triathlon-agent's install and verify (`uv sync`, pytest, npm) run
  unsandboxed with network, as RallySource's do. Container isolation remains a precondition for
  unattended runs.
- **Parallel sessions and the subscription.** With `max_concurrent_sessions > 1`, two targets draw
  on one subscription window and hit usage limits sooner. Default is 1.
- **Shared calibrations.** Labels from two repos with different issue-writing styles feed one
  temperature per question. Labels record their target, so per-target calibration can be added
  later if accuracy differs.
- **Acceptance criteria on GitHub.** Issues without a `## Acceptance criteria` section will usually
  triage as not `clear` and park `needs_human`. That is intended; write issues with the section or
  approve past the gate.
- **Personal-account repo.** Rulesets on a personal repo are enforced for the App, but you remain an
  admin who can bypass them.
