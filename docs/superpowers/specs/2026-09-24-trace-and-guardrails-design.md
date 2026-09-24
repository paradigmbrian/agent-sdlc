# Agent SDLC — Tracing, Guardrails and Evals Wave

- **Date:** 2026-09-24
- **Status:** Draft — awaiting review
- **Owner:** Brian (Paradigm Shift)
- **Builds on:** `2026-09-23-agent-sdlc-design.md` (referred to below as "the base spec")

## 1. Purpose

Before the first live RallySource run, make every item's history reconstructable, stop the
known ways an agent can get code executed outside its sandbox, and measure how the pipeline
performs. The goal is to catch and block problems early rather than discover them in a PR.

### 1.1 Findings this wave closes

| # | Finding | Section |
|---|---|---|
| 1 | The implementer can edit tooling config (`package.json` scripts, eslint/vite/postcss/tailwind config, `turbo.json`, `.npmrc`) that verify then executes on the host, unsandboxed, with network | §5.1, §5.2 |
| 2 | Denied tool calls are dropped (planner/reviewer), overwritten (implementer), and never shown | §5.3 |
| 3 | The per-item token budget is only checked after a stage ends | §5.4 |
| 5 | The `items` row is overwritten each step; there is no history per item or per attempt | §3 |
| 6 | Agent sessions keep only final text and usage: no tool calls, session id, duration or cost | §4.1 |
| 7 | Verify output is truncated to its last 8000 chars in memory and 2000 chars in the DB | §4.2 |
| 8 | Logs go to stderr only, without item or stage context; transitions are not logged | §4.3 |
| 9 | No per-item inspection command | §6.1 |
| 10 | Abandoned PRs produce no labels, so gate calibration only sees approvals | §7.1 |
| 11 (part) | No pipeline metrics | §7.2 |

### 1.2 Success criteria

1. `agent-sdlc trace <id>` prints, for any item, every transition, agent session, denied tool
   call, check run and park/requeue in order, with paths to the full transcript and check logs.
2. An agent attempt to read outside its worktree or write a protected path interrupts the
   session and parks the item `policy` with the attempt quoted in the park comment.
3. No change to a dependency manifest or lockfile reaches `install`/verify without a human
   approving that exact change.
4. No tooling config that verify executes as code can be written by an agent.
5. `agent-sdlc metrics` reports outcome, park, retry, token, latency, denial and calibration
   figures for a time window.
6. `agent-sdlc status` shows whether the loop is alive (last tick) and flags stale items.
7. All of the above is covered by unit and e2e tests using the existing fakes.

### 1.3 Out of scope

- Running install/verify in a container or VM. This wave reduces what an agent can steer into
  verify; agent-authored source and test code still runs on the host during verify. Container
  isolation is its own follow-up spec and remains a precondition for unattended runs.
- Golden-set replay of historical work items (needs real run data first).
- Trace retention/cleanup, secret scanning of diffs, a web dashboard, OpenTelemetry export.

## 2. Design overview

```
StageExecutor ──StepResult(transition, usage, decisions, events)──▶ Scheduler
     │  runner.run(..., trace=path)                                   │ commit_step(... events)
     ▼                                                                ▼
ClaudeAgentRunner ──JSONL──▶ ~/.agent-sdlc/traces/<id>/…     Store: items, decisions,
Workspaces.run    ──.log───▶ ~/.agent-sdlc/traces/<id>/…            labels, events (new)
                                                                      │
logging (stderr + ~/.agent-sdlc/logs/agent-sdlc.log, item/stage ctx)  ▼
                                                        cli: trace · metrics · status
```

The Scheduler stays the sole writer of item state and becomes the sole writer of events.
Large artifacts (transcripts, full command output) go to files; the DB stores their paths.

## 3. Event log

New table `events` (created by `create_all`; no existing table is altered):

| column | type | notes |
|---|---|---|
| `id` | int PK autoincrement | ordering key |
| `item_id` | int, nullable, FK items.id | null for global events |
| `ts` | datetime (UTC) | |
| `kind` | str | see below |
| `stage` | str, nullable | stage the event happened in |
| `attempt` | int, nullable | `item.attempt` at the time |
| `payload` | JSON | kind-specific |

Event kinds and payloads:

| kind | written by | payload |
|---|---|---|
| `intake` | Scheduler `_intake` | `title`, `branch` |
| `transition` | Scheduler `_step`, `_infra_failure` | `from`, `to`, `park_reason`, `note` (≤2000 chars), `pr_round` |
| `agent_session` | StageExecutor (via StepResult) | `role`, `session_id`, `subtype`, `is_error`, `turns`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_usd`, `duration_ms`, `denials`, `escalated` (category or null), `transcript` (path) |
| `tool_denied` | StageExecutor (via StepResult) | `role`, `tool`, `category`, `reason`, `input` (JSON, ≤500 chars) |
| `check` | StageExecutor (via StepResult) | `name`, `command`, `exit_code`, `duration_s`, `log` (path) |
| `infra_failure` | Scheduler | `n`, `error` (type + message, ≤2000 chars) |
| `usage_limit` | Scheduler | `until` |
| `park_tagged` / `park_side_effect_failed` | Scheduler `_park_side_effects` | `step` (`tag`/`comment`), `error` |
| `requeue` | Scheduler `requeue_item`, CLI `requeue --local` | `from_reason`, `to`, `approved` (bool: a gate approval was recorded) |
| `pr_comment` | StageExecutor (via StepResult) | `thread_id`, `comment_id`, `author`, `intent` |
| `outcome` | Scheduler, on `done`/`closed` | `result` (`merged`/`abandoned`), `pr_id` |
| `pause` / `resume` | CLI | — |

Rules:

- `StepResult` gains `events: list[EventInput]` (`kind`, `payload`). The Scheduler writes them
  plus the `transition` event inside the existing `commit_step` transaction, so an event exists
  if and only if the step it describes was committed.
- Events recorded by a step that is then discarded (e.g. `UsageLimitError`, an exception) are
  not written; the `usage_limit`/`infra_failure` event is written instead, together with any
  partial `agent_session` data the exception carries (§4.1).
- `Store` gains `add_event(...)`, `events_for(item_id)`, `events_since(ts, kinds=None)` and
  `last_event_ts(item_id)`.

## 4. Trace artifacts and logging

### 4.1 Agent transcripts

- `AgentRunner.run(role, prompt, cwd, max_turns, trace: Path | None = None)`. When `trace` is
  set, the runner writes one JSON object per line, flushed per line so a crash keeps the
  partial transcript:
  - `{"type":"prompt","role","text"}` first;
  - `assistant_text` (`text`), `tool_use` (`id`, `name`, `input`) from `AssistantMessage`;
  - `tool_result` (`tool_use_id`, `is_error`, `content` truncated to 20,000 chars) from
    `UserMessage` content blocks;
  - `denied` (`tool`, `category`, `reason`) from the PreToolUse hook;
  - `rate_limit` from `RateLimitEvent`; `result` (`subtype`, `session_id`, `num_turns`,
    `duration_ms`, `total_cost_usd`, `usage`) from `ResultMessage`.
  Each line carries `ts`. Serialization lives in a small pure `TranscriptWriter` so it can be
  unit-tested with constructed SDK dataclasses (claude-agent-sdk 0.2.158 types:
  `ToolUseBlock`, `ToolResultBlock`, `UserMessage`, `AssistantMessage`, `ResultMessage`).
- `AgentResult` gains `session_id`, `duration_ms`, `cost_usd`, `denials: tuple[Denial, ...]`
  (replacing `denied: tuple[str, ...]`), `escalated: str | None` and `trace: str | None`.
- `UsageLimitError` and `AgentInfraError` carry the partial `AgentResult` fields known at the
  time (usage, trace path, denials) so the Scheduler can record them.
- File path: `<traces>/<item_id>/<UTC yyyymmddTHHMMSS>-<stage>-a<attempt>-<role>.jsonl`.
  `<traces>` defaults to `~/.agent-sdlc/traces` (CLI `--traces`). Directories are created
  `0700`, files `0600`.
- Transcripts hold only what the agent saw or produced. Agents never receive credentials, so
  transcripts contain none.

### 4.2 Full command output

- `Workspaces.run(name, command, wt, log: Path | None = None)` streams full output to `log`
  when given, and still returns the last 8000 chars in `CommandResult.output`.
  `CommandResult` gains `log: str | None`.
- Verify and install logs go to `<traces>/<item_id>/<ts>-<stage>-a<attempt>-<name>.log`.
- Each check emits a `check` event. `data["checks"]` keeps its 2000-char tail for the PR body.

### 4.3 Logging

- `cli.main` configures two handlers: stderr (INFO) and
  `~/.agent-sdlc/logs/agent-sdlc.log` (`TimedRotatingFileHandler`, daily, 14 backups, DEBUG).
- A `contextvars`-based filter adds `item` and `stage` to every record; the format becomes
  `%(asctime)s %(levelname)s [#%(item)s %(stage)s] %(name)s: %(message)s` (`-` when unset).
  The Scheduler sets the context around each `_step` and park/requeue.
- New INFO lines: every transition (`plan -> implement`), every agent session summary (role,
  turns, tokens, denials, duration), every check result. Every denial logs at WARNING.

## 5. Guardrails

### 5.1 Tooling config becomes protected (finding 1, part)

Add to `targets/rallysource.yaml` `policy.protected_paths` (verified against the RallySource
tree on 2026-09-24; the globs are root-anchored, so app source such as
`apps/rallysource-api/src/config/app.config.ts` stays writable):

```yaml
    - "*.config.*"                 # eslint, commitlint at repo root
    - "apps/*/*.config.*"          # eslint, vite, postcss, tailwind per app
    - "packages/*/*.config.*"
    - "packages/eslint-config/**"  # shared eslint config executed by lint
    - "turbo.json"
    - ".npmrc"
    - "**/.npmrc"
    - "**/nest-cli.json"
```

Writes to these are denied by the agent tool hook and caught again by the pre-push check, as
with every protected path. Combined with §5.3, an attempt parks the item.

### 5.2 Manifest gate (finding 1, part)

`package.json` (which also holds RallySource's jest config and all npm scripts) and lockfiles
change legitimately, so they are gated rather than protected.

- New `policy.manifest_paths` in the target config; RallySource value:
  `["**/package.json", "package-lock.json", "**/package-lock.json", "**/npm-shrinkwrap.json"]`.
  Must not overlap `protected_paths` (validated at load).
- `Workspaces.manifest_digest(wt) -> str | None`: over files changed on the branch (vs the
  merge-base with `base_branch`) that match `manifest_paths`, sha256 of the sorted
  `(path, blob sha at HEAD)` pairs; `None` if no manifest file changed.
- After the implement commit, and again after a verify lint-fix commit and in the pre-push
  check: if the digest is not `None` and differs from `data["manifest_approved"]`, park with
  new reason **`manifest`**, store the digest in `data["manifest_pending"]`, and include the
  diff of those files (escaped, truncated to ~6000 chars) in the park comment.
- `manifest` is not a gate park. Requeue sends the item to `verify` with fresh counters and sets
  `data["manifest_approved"] = data["manifest_pending"]`. The park comment says exactly that:
  "Removing the tag approves these dependency changes; install and verify will run with them."
- A later change to the manifests (new digest) parks again; an unchanged digest does not.
- **Install follows the manifests.** The boolean `data["installed"]` is replaced by
  `data["installed_digest"]` (sha256 of all manifest file contents at HEAD). Implement and
  verify both call `_ensure_installed(wt)`, which runs `install` when the current digest
  differs. So approved dependency changes are actually installed before verify, and an item
  with no manifest changes installs once, as today.
- **Never install an unapproved manifest.** `_ensure_installed` first checks the manifest
  digest; if it is unapproved it does not install and the stage parks `manifest` instead. This
  covers a branch whose manifest change was committed alongside a different park (e.g. a
  `policy` park that a human then requeues to implement).

### 5.3 Denials are recorded, surfaced and escalated (finding 2)

- `policy.py`/`runner.py` gain `Denial(category, reason)` with categories: `tool_not_permitted`,
  `protected_path`, `outside_worktree`, `command_not_allowlisted`, `shell_syntax`,
  `side_effect_flag`, `policy_error`. A new `evaluate_tool(...) -> Denial | None` does the work;
  the existing `check_tool(...) -> str | None` becomes a thin wrapper returning
  `denial.reason`, so existing policy tests are unchanged.
- **Escalation.** The runner interrupts the session and returns `escalated=<category>` when:
  - any denial is `outside_worktree` or `protected_path`; or
  - denials in the session reach `limits.max_denials_per_session` (new, default 5).
  After `client.interrupt()` the runner keeps reading until a `ResultMessage` arrives or 30 s
  pass; if none arrives, usage is taken from the summed `AssistantMessage.usage` values and the
  `agent_session` event records `usage_estimated: true`.
- StageExecutor checks `res.escalated` before `_agent_failed` and parks `policy` with a note
  naming the category and quoting up to five denials (tool, reason, input excerpt).
- Every denial becomes a `tool_denied` event. The PR body gets a "Blocked tool calls" section
  (count per role and the first 10), and park comments list the denials from the parked stage.

### 5.4 Mid-session token budget (finding 3)

The runner receives `token_budget: int | None` (remaining item budget, computed by the
StageExecutor from `max_item_tokens`, `budget_offset` and `item.usage`). It sums
`AssistantMessage.usage` as the session runs and interrupts when the budget is exceeded,
returning `escalated="budget"`; the StageExecutor parks `budget`. The existing post-step check
in the Scheduler stays as the backstop.

## 6. Operator commands

### 6.1 `agent-sdlc trace <id> [--full]`

Chronological merge of `events` and `decisions` for the item:

```
#4821 "Fix session timeout on refresh"   stage: parked (policy from implement)
2026-10-02 19:04:11  intake            branch agent/4821-fix-session-timeout-on-refresh
2026-10-02 19:04:12  triage   decision clarity=clear 0.91 (shadow)  kind=bug 0.88 ...
2026-10-02 19:04:12  triage → plan
2026-10-02 19:09:40  plan     agent    planner 14 turns 212k tok 5m28s  0 denied
                                       traces/4821/20261002T190412-plan-a0-planner.jsonl
2026-10-02 19:21:03  implement DENIED  Read outside_worktree: /Users/brian/.ssh/config
2026-10-02 19:21:04  implement agent   implementer ESCALATED outside_worktree  31 turns ...
2026-10-02 19:21:04  implement → parked (policy)
```

`--full` also prints each transcript's `tool_use` lines (tool, compact input) under its session.

### 6.2 `agent-sdlc status` additions

- Header: `last tick: 2m ago` from a `last_tick` flag the Scheduler sets at the start of every
  tick (including paused ticks). Missing or older than 3 × `--poll` prints `LOOP NOT RUNNING?`.
- Per item: age of the last event and total denials. An item in an active stage whose last
  event is older than `limits.stale_after_minutes` (new, default 120) is marked `STALE`, and
  the Scheduler logs a WARNING for it once per tick.

### 6.3 `agent-sdlc metrics [--days 30]`

Plain-text report over events in the window:

- **Outcomes:** items taken in; done / closed / parked / in flight; merge rate
  = done / (done + closed).
- **Parks:** count by reason and by stage; share of parks later requeued.
- **Effort:** verify retries per item, PR rounds per item, replans; tokens per item and per
  stage (median, p90) and cache-read share; cost_usd total where reported.
- **Latency:** intake → PR open and PR open → done (median, p90).
- **Denials:** count by role × category; escalations.
- **Gates:** per gate/question: calibration mode, label count, last ECE, and the agreement rate
  of logged decisions with their labels.

## 7. Evals

### 7.1 Abandoned PRs feed labeling instead of auto-labels (finding 10)

Abandonment can mean a bad plan, a bad change, or a work item that became obsolete, so it is not
a reliable automatic label. Instead:

- On `awaiting_human → closed` the Scheduler writes an `outcome` event
  (`result: abandoned`) as well as the transition.
- `agent-sdlc label plan|review --abandoned` presents only unlabeled decisions from abandoned
  items, first, showing the PR's final comment threads alongside the decision state.
- Merges keep recording approval labels as today.

### 7.2 Metrics

§6.3. The golden-set replay stays out of scope (§1.3) and should use these metrics as its
baseline when it is specified.

## 8. Interface and config changes

| Change | Where |
|---|---|
| `AgentRunner.run(..., trace=None, token_budget=None)` | `ports.py`, `runner.py`, `tests/fakes.py` |
| `AgentResult`: + `session_id`, `duration_ms`, `cost_usd`, `denials`, `escalated`, `trace`; − `denied` | `types.py` |
| `CommandResult`: + `log` | `types.py`, `workspaces.py` |
| `WorkspacePort`: `run_checks(wt, log_dir=None)`, `install(wt, log=None)`, + `manifest_digest(wt)`, + `install_digest(wt)` | `ports.py`, `workspaces.py`, fakes |
| `ParkReason.MANIFEST`; `requeue` sends it to `verify` | `types.py`, `transitions.py` |
| `StepResult.events` | `stages.py` |
| `events` table and accessors | `store.py` |
| `policy.manifest_paths`; `limits.max_denials_per_session`, `limits.stale_after_minutes` | `targets.py`, `rallysource.yaml` |
| CLI: `trace`, `metrics`, `label --abandoned`, `--traces`, status additions | `cli.py`, `labeling.py` |

Existing tests change only where a signature changes (the fakes and `denied` → `denials`).
No existing assertion is weakened.

## 9. Error handling

- A failure to write a transcript or log file never fails the stage: the writer logs a WARNING
  once per session, stops writing, and the `agent_session`/`check` event records
  `trace: null` with `trace_error`.
- Event writes share the step's transaction; if the transaction fails, the existing per-item
  infra handling applies (the step is retried, the item parks `infra` after 3 failures).
- `trace` and `metrics` are read-only and work while `run` is active (SQLite readers).
- Escalation interrupt errors are treated like any SDK error (§7.1 of the base spec): the
  denials already recorded are still returned with the `AgentInfraError`.

## 10. Testing

- **Unit:** `evaluate_tool` categories for each existing denial path; `TranscriptWriter` with
  constructed SDK messages; runner escalation (outside_worktree, protected_path, threshold,
  token budget) with a fake SDK client; `manifest_digest` and `_ensure_installed` against a
  temp git repo; the new protected globs (config files denied, `src/config/app.config.ts`
  allowed); requeue of a `manifest` park; event writes in `commit_step`; `metrics` figures from
  a seeded store; `trace` rendering; `status` stale and last-tick logic; log context filter.
- **E2E (fakes):** an escalated implementer session parks `policy` with events, park comment
  and trace file; a manifest change parks, requeue proceeds to verify and reinstalls, an
  unchanged digest on a later PR round does not re-park; an abandoned PR writes the outcome
  event and appears in `label --abandoned`.
- **Slow (real SDK):** extend `test_real_agent_policy.py` to assert that a real session asked to
  read `~/.ssh` is interrupted with `escalated="outside_worktree"` and that its transcript
  contains the `tool_use`, `denied` and `result` lines.
- Definition of done: `uv run pytest`, `uv run ruff check .`, `uv run mypy`, and
  `uv run pytest -m slow` once the Claude token exists.

## 11. Rollout

1. Land this wave (it changes no live behavior besides new parks for manifests, escalations and
   the new protected globs).
2. Base-spec open items: README setup, real-agent slow test, container isolation spec.
3. `--dry-run-push` smoke test with Brian present, using `trace` and `metrics` to review it.
