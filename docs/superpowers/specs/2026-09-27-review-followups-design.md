# Review follow-ups: concurrency, isolated dry run, PR rounds, retries, lint commit

Date: 2026-09-27. Follows `2026-09-27-review-fixes-design.md` (R1–R3, merged in 562d7cb).
Scope: the remaining findings of the 2026-09-27 review. No schema migrations: new state lives in
`item.data` or flags. Existing target YAML keeps loading.

Findings covered:

| # | Finding | Section |
|---|---|---|
| F1 | `max_concurrent_items` runs a target's items one after another | §1 |
| F2 | `--dry-run-push` writes the real DB, worktrees and tracker | §2 |
| F3 | Human commits on the PR branch make the push fail and park INFRA | §3.1 |
| F4 | A revision round with no new change is reported as handled | §3.2 |
| F5 | PR replies are sent before `seen_comments` is saved (duplicates) | §3.3 |
| F6 | Agent errors share `attempt` with verify/review retries | §4.1 |
| F7 | The lint-fix commit takes untracked output and is never re-verified | §4.2 |

## 1. Concurrency within a target (F1)

**Behavior.** Up to `limits.max_concurrent_items` items of one target run at the same time. A
slot that frees up is refilled on the next poll, not when the slowest item finishes.
`max_concurrent_sessions` still caps agent sessions across all targets.

**Scheduler.**

- `Scheduler` keeps `_running: dict[int, threading.Thread]` (item id → thread) under a lock.
- `tick(wait: bool = True)`:
  1. As today: last-tick flag, pause check, intake, requeue, stale warning.
  2. AWAITING_HUMAN items are polled serially in the tick thread (cheap). The set of running item
     ids is snapshotted before the AWAITING_HUMAN rows are read, and again before the rows are
     read in `_to_launch` and in `_requeue_untagged`: an item whose thread finishes mid-scan has
     already committed its write in its `finally`, so a row read after the snapshot is fresh for
     it; reading rows first could instead catch a stale row and step or park-side-effect it a
     second time.
  3. If agent work is allowed: `free = max_concurrent_items − len(_running)`. From the active
     items in `in_flight` order (created-at, triage last), skip running ones and launch the first
     `free` of the rest. Order is not rotated: the oldest active item keeps its slot for as long
     as it stays in an active stage, whether or not it happens to be running this tick. Each
     launch is a daemon thread running `asyncio.run(self._step(item, now))`; the thread removes
     itself from `_running` in a `finally`.
  4. With `wait=True`, join the threads launched by this tick before returning.
- Running items are excluded before the limit is applied, so a requeued item that sorts ahead
  cannot push the running count over the limit.
- `run --once` calls `tick(wait=True)` (via `Supervisor._once`), so its exit code still covers
  every item — this is also `tick`'s default. `run_forever` is the only caller that passes
  `tick(wait=False)`.
- Pause: a paused tick launches nothing; running sessions stop through the existing
  `should_stop` check. Shutdown: threads are daemons, as target threads are today.
- `_run_step` already catches every exception per item; the thread wrapper only logs what escapes.

**Shared state.**

- Store (session per call, SQLite WAL + busy timeout), `LockedDecider`, `SessionSlots` and
  `GitHubAppAuth` are already thread-safe.
- `Workspaces` gets a `threading.Lock` held around `_ensure_base` + `worktree add` in `create`,
  around `remove` (`worktree remove`/`prune`, `branch -D`), and around the PR-branch fetch in
  `incorporate_remote` (§3.1) — all four write or read the shared base `.git`, and the fetch
  opportunistically updates the shared remote-tracking ref `refs/remotes/origin/<branch>`, which
  would race `_ensure_base`'s own `fetch --prune`. Per-worktree git commands stay unlocked.
- The scratch HOME's npm and uv caches tolerate concurrent use.

**Busy flags.**

- `busy:<target>` becomes one flag per item: `busy:<target>:<external_id>` = `<stage>|<since>`.
- New `Store.flags(prefix: str) -> dict[str, str]`.
- Stale busy flags (left by a crashed process) are cleared at the start of `run_forever`, not at
  construction: `cli requeue` also builds a `Scheduler` and must not wipe a live loop's flags.
- `status` prints one `busy:` line per flag, each with the existing `STUCK?` check. Ticks no longer
  block on steps, so `LOOP NOT RUNNING?` needs no busy exception.

**Tests.** Two items whose fake runner blocks on an event run overlapped; the limit holds across
ticks; a slot freed while another item runs is refilled on the next tick; `tick(wait=True)` waits
and `run --once` reports a failed item; busy flags are per item and cleared at startup; concurrent
`create` calls for two items both succeed.

## 2. Dry run as an isolated preview (F2)

**CLI.** `run --once --dry-run`; `--dry-run-push` stays as an alias. `--dry-run` without
`--once` exits 1 with a message. A non-`sqlite:///` `--db` with `--dry-run` exits 1.

**State.** Snapshot the real DB into a temp directory with `sqlite3.Connection.backup` (consistent
under WAL), opening the source read-only (`file:<path>?mode=ro`, `uri=True`). No real DB yet: the
copy starts empty. The CLI builds its `Store` on the copy, never on the real URL, so the real DB is
never opened for writing. The temp directory is deleted at the end.

**Workspaces.** The run uses a temp workspaces root, deleted at the end, so dry-run commits never
reach real worktrees. Cost: one fresh clone per dry run.

**Traces.** Written to `~/.agent-sdlc/dry-run/<UTC timestamp>/traces/` and kept; the CLI prints the
path when the run ends.

**Forge.** New `DryRunForge(inner: ForgePort)` in `adapters/dry_run.py`:

- Delegates reads: `list_intake`, `list_closed`, `get_item`, `has_label`, `git_auth_header`,
  `pr_ref`, `item_ref`, `kind`, `label_word`.
- Logs `dry-run: would …` and skips writes: `comment_item`, `set_label`, `push_branch`,
  `update_pr`, `reply_pr`, `comment_pr`, `delete_branch`.
- `create_pr` returns 0; `pr_status(0)` returns `"active"`; `pr_comments(0)` returns `[]`.
  For a non-zero PR id these delegate.
- The `dry_run_push` parameters and branches in `AdoForge` and `GitHubForge` are removed;
  `make_forge` wraps the adapter when dry-run is on. `label triage` (read-only) keeps working.

**Not isolated.** Laya and Claude run for real. Their usage is recorded only in the copy, so the
real daily caps do not count dry-run tokens. The README says so.

**Tests.** After a dry run that reaches `awaiting_human`, the real DB is byte-identical: its data
files, `<db>` and `<db>-wal`, are unchanged; `<db>-shm` is excluded from the comparison because it
is SQLite's shared-memory reader index, which any reader touches, including the dry run's own
backup read. A write-spy inner forge records no writes; the temp workspaces root is gone and the
traces directory exists; `--dry-run` without `--once` and with a non-SQLite URL each exit 1.

## 3. PR revision rounds

### 3.1 Incorporating human commits (F3)

At the start of `_implement`, when `item.pr_id` is set, before the manifest gate and install:

1. `git fetch origin refs/heads/<branch>` (authenticated). Remote branch missing: skip.
2. Remote tip is an ancestor of HEAD: nothing to do.
3. HEAD is an ancestor of the remote tip: `git merge --ff-only FETCH_HEAD`.
4. Otherwise: `git merge --no-edit FETCH_HEAD` with the agent identity.
5. Merge conflict: `git merge --abort`; park `needs_human` with note
   `PR branch has diverged and could not be merged: <conflicted files>`. After the human
   resolves it, a requeue returns to implement (`needs_human` from implement has no gate to
   approve, so `requeue` falls back to the parked stage and adds no approval labels).

New `Workspaces.incorporate_remote(wt, branch) -> list[str]` returns the paths the human commits
changed (`git diff --name-only <old HEAD> <new HEAD>`, empty when nothing was merged) and raises
`MergeConflict(files)` on step 5. New `Workspaces.human_blobs(wt) -> dict[str, str]` computes
`data["human_blobs"]` as described below; it returns `{}` when nothing has been fetched yet (no
`incorporate_remote` call, or the remote branch doesn't exist).

**Human-changed paths.** Policy, the manifest gate and the diff-size limit all check the whole
diff against base, so a human's own edit to a protected path, a manifest, or just a large edit
would park every round. `data["human_blobs"]` is not taken from the merge; it is recomputed on
every PR-round implement, after incorporating, by `Workspaces.human_blobs(wt)`:

- The paths changed by non-merge commits in `origin/<base>..FETCH_HEAD` whose author email is not
  the agent identity (`agent-sdlc@localhost`) — a human committing with the agent identity is
  treated as the agent — each mapped to its blob at `FETCH_HEAD` (`"deleted"` when the path is
  absent there).
- The stored map is replaced with this result each round, rather than merged into what was there
  before. Deriving it fresh from the worktree's own history, rather than keeping it as stored
  state, means an interrupted step never leaves it stale, and it survives a crash between steps.
- A helper `_agent_owned(item, wt, paths) -> list[str]` drops each path whose current blob equals
  `human_blobs[path]`. It filters the input of `PathPolicy.violations` in `_policy_park`, the
  diff-size limit, the implement violation check and the lint-commit check, and of
  `_manifest_gate`.
- When the agent later edits such a file its blob differs, so the check applies again. Because the
  comparison is by blob rather than by "who touched it last," an agent edit that git auto-merged
  into a file a human also edited is still judged on its own blob, separately from the human's.

### 3.2 Rounds that change nothing (F4)

`_implement` records HEAD after incorporating (§3.1) and before the implementer runs. If
`item.data["feedback"]` is set (PR change request, red checks or review notes) and HEAD is
unchanged after the implementer's commit, park `needs_human`:
`The implementer made no changes for the feedback.` The feedback is kept, so a requeue applies it
again. The existing "no changes at all" park for a first implement is unchanged.

### 3.3 PR replies after the commit (F5)

- `StepResult` gains `replies: list[tuple[PrComment, str]]`. `_awaiting` appends to it instead of
  calling `reply_pr`.
- `Scheduler._run_step` sends replies after `commit_step` (so `seen_comments` is saved first).
  Replies are at-most-once: a failure (`_INFRA_ERRORS`) is logged and recorded as a
  `pr_reply_failed` event with the comment key; it is not retried.
- Replies are still sent only while the PR is active (unchanged condition).

**Tests.** Fast-forward and diverged-merge cases pull human commits in; a conflict parks with the
files listed and `merge --abort` leaves a clean tree; a human-edited protected file and manifest
pass policy and the gate, and an agent edit to the same file is caught; a no-op round parks and
keeps the feedback; replies are sent only after the commit, and a failing reply does not produce a
second reply on the next poll.

## 4. Retries and verify

### 4.1 Separate agent-error counter (F6)

- `attempt` counts only the implement ↔ verify/review loop (red checks, blocking reviews), capped
  by `max_verify_retries`. `after_agent_error` no longer touches it.
- `data["agent_errors"]: dict[stage, int]`. `after_agent_error(stage, error, errors, max_errors)`
  retries the stage while `errors < max_errors`, else parks `agent_error`. The executor increments
  the stage's count on an error result and removes the stage's entry when that stage's agent
  session finishes without an error.
- New `Limits.max_agent_errors: int = 3` (today's effective limit). Optional, so existing target
  files load.
- `requeue` drops `agent_errors` (fresh counters), like it resets `attempt`.

### 4.2 Lint-fix commit (F7)

- `Workspaces.commit(wt, message, tracked_only: bool = False)`: `tracked_only` stages with
  `git add -u`. Verify's lint commit uses it, so untracked output (build, snapshots, coverage) is
  never committed. Untracked leftovers are cleaned by the next implement `reset` and are never
  pushed (push sends HEAD).
- When the lint commit changed anything, after the existing protected-path, manifest and diff-limit
  checks pass, verify runs the checks once more; the second results decide the transition and
  are the ones stored in `data["checks"]`.
- If the second run modifies tracked files again (a non-idempotent fixer), `reset --hard HEAD`
  discards them and the step logs a warning; there is no third run.
- `check_event(r, pass_=None)` adds `"pass": 1 | 2` to the event only when `pass_` is given, for
  verify's own check commands on their first and (when a lint commit triggers one) second run.
  Install events and other `check` events that don't go through verify's re-check pass keep no
  `"pass"` key, so `tests/test_events.py` and the install-event shape are unchanged.

**Tests.** A planner max-turns error leaves `attempt` at 0 and the next verify still gets every
retry; the per-stage counter resets on success and parks at `max_agent_errors`; a target file
without `max_agent_errors` loads; an untracked build file is not committed; a lint change causes
exactly one re-run whose results are stored; a non-idempotent change is discarded.

## 5. Documentation

README updates: what `max_concurrent_items` means now; `--dry-run` isolation, the traces path and
the token caveat; human commits on a PR are merged in and a conflict parks; the no-op-round and
diverged-branch parks; `limits.max_agent_errors`.

## Out of scope

- Reusing an existing PR when `create_pr` finds one already open (an orphan from before R2).
- Counting dry-run tokens toward the real daily caps.
- Container isolation for verify commands.
