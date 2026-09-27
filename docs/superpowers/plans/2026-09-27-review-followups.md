# Review Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the seven remaining review findings (F1–F7): real per-target concurrency, an isolated dry run, human commits on PR branches, no-op revision rounds, reply ordering, a separate agent-error counter, and a tracked-only, re-verified lint commit.

**Architecture:** The scheduler launches each in-flight item in its own daemon thread (own event loop) and returns without waiting, except when `tick(wait=True)` (tests, `run --once`). Dry run wraps the real forge in a write-free `DryRunForge` and runs against a SQLite snapshot and temp workspaces. Stage code learns to merge a human's PR commits and to ignore files whose content is still exactly what the human pushed.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (SQLite), stdlib `threading`/`asyncio`/`sqlite3`, git CLI, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff, mypy strict. Run everything with `uv run`.

**Spec:** `docs/superpowers/specs/2026-09-27-review-followups-design.md`

## Global Constraints

- No schema migrations: new state lives in `item.data` or the `flags` table.
- Existing target YAML must keep loading; new config keys are optional with defaults.
- `Limits.max_agent_errors` default is `3`.
- `busy` flag format: key `busy:<target>:<external_id>`, value `<stage>|<since ISO-8601>`.
- Dry-run traces directory: `<parent of --traces>/dry-run/<UTC %Y%m%dT%H%M%SZ>/traces` (default parent is `~/.agent-sdlc`).
- Park notes, verbatim: `PR branch has diverged and could not be merged: <files>` and `The implementer made no changes for the feedback.`
- Event kinds, verbatim: `pr_reply_failed`; `check` events from verify carry `"pass": 1 | 2`.
- Match surrounding style: 100-char lines, `from __future__ import annotations`, comments cite the spec section (e.g. `(spec §3.1)`).
- Every task ends green on: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy`.

## Deviations from the spec (fold into the spec in Task 10)

1. `Scheduler.tick(wait: bool = True)`: the default waits; only `run_forever` passes `wait=False`. About 30 existing tests call `await s.tick()` and assert right after; `Supervisor._once` needs no change.
2. Stale busy flags are cleared at the start of `run_forever`, not in `__init__`: `cli requeue` also builds a `Scheduler` and must not wipe a live loop's flags.
3. `human_blobs` records **every** path the human commits touched, and the diff-size limit also ignores files still at their human blob. Otherwise a large human commit parks POLICY every round.
4. `check_event(r, pass_=None)` adds `"pass"` only when given (verify passes 1/2), so install events and `tests/test_events.py` are unchanged.

## Existing tests this plan changes (authorized by the approved spec)

| Test | Why |
|---|---|
| `tests/test_scheduler.py::test_i1_error_on_one_item_does_not_stop_others` | Order of two concurrent items is no longer fixed; use a per-item executor |
| `tests/test_cli.py::test_status_busy_suppresses_loop_warning`, `::test_status_busy_past_stale_limit_is_stuck` | Per-item busy flags; busy no longer suppresses `LOOP NOT RUNNING?` |
| `tests/test_ado.py::test_dry_run_pr_methods_make_no_http_calls`, `::test_push_branch_dry_run_does_nothing` | Adapter dry-run removed (moved to `DryRunForge`) |
| `tests/test_github.py::forge()` helper, `::test_comment_item_posts_html`, `::test_dry_run_makes_pr_side_effects_no_ops` | Same |
| `tests/test_runtime.py` (`dry_run_push=` → `dry_run=`) | Parameter rename |
| `tests/test_stages.py::test_awaiting_handles_comments` | Replies are returned, not sent, by the executor |
| `tests/test_stages.py::test_i2_agent_error_retries_then_parks` | Agent errors use `agent_errors`, not `attempt` |
| `tests/test_stages.py::test_m9_lint_fix_commit_rechecks_diff_limit` | Lint commit is tracked-only; the fixture must modify a tracked file |

## Review Focus

1. A running item parks while the tick thread runs `_requeue_untagged`: park side effects must not run twice (test in Task 3).
2. Dry run against a WAL-mode DB whose latest writes are still in the `-wal` file: the snapshot must include them (test in Task 1).
3. The human deleted the PR branch on the remote: implement continues without merging (test in Task 2).
4. The remote is unreachable during the PR-branch fetch: raise `GitError` (infra retry), don't silently skip (test in Task 2).
5. A human commit deletes a protected file: recorded as `deleted`, passes the checks until the agent recreates it (test in Task 6).

---

### Task 1: Store — thread-safe in-memory DB, prefix flags, SQLite snapshot

**Files:**
- Modify: `src/agent_sdlc/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `Store.flags(prefix: str) -> dict[str, str]`; `snapshot_sqlite(url: str, dest: Path) -> str` (returns `sqlite:///<dest>`); `Store("sqlite://")` usable from several threads.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`; add `import threading` and `from pathlib import Path` if missing, and `from agent_sdlc.store import snapshot_sqlite`)

```python
def test_memory_store_is_shared_across_threads() -> None:
    store = Store("sqlite://")
    t = threading.Thread(target=lambda: store.set_flag("k", "v"))
    t.start()
    t.join()
    assert store.get_flag("k") == "v"


def test_flags_by_prefix_escapes_wildcards() -> None:
    store = Store("sqlite://")
    for k in ("busy:a:1", "busy:a:2", "busy:ab:3", "a_b:1", "axb:1"):
        store.set_flag(k, k)
    assert set(store.flags("busy:a:")) == {"busy:a:1", "busy:a:2"}
    assert set(store.flags("a_b:")) == {"a_b:1"}


def _files(db: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in db.parent.glob(db.name + "*")}


def test_snapshot_copies_uncheckpointed_wal_and_leaves_source_untouched(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = Store(f"sqlite:///{db}")
    store.add_item("t", WI, "agent/5-x")          # still in the -wal file: engine not disposed
    before = _files(db)
    url = snapshot_sqlite(f"sqlite:///{db}", tmp_path / "copy.db")
    assert url == f"sqlite:///{tmp_path / 'copy.db'}"
    assert Store(url).get_by_ref("t", WI.id).title == WI.title
    assert _files(db) == before


def test_snapshot_of_missing_db_is_empty(tmp_path: Path) -> None:
    url = snapshot_sqlite(f"sqlite:///{tmp_path / 'none.db'}", tmp_path / "copy.db")
    assert Store(url).items("t") == []
    assert not (tmp_path / "none.db").exists()


def test_snapshot_rejects_non_sqlite_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a SQLite file URL"):
        snapshot_sqlite("postgresql://h/db", tmp_path / "copy.db")
```

(`WI` is the module's existing `WorkItem` fixture constant in `tests/test_store.py`; if it is named differently, use that constant.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'snapshot_sqlite'`.

- [ ] **Step 3: Implement** in `src/agent_sdlc/store.py`

Add imports:

```python
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, closing, contextmanager, nullcontext
from pathlib import Path
from sqlalchemy.pool import StaticPool
```

Replace `Store.__init__` and `_session`:

```python
_MEMORY_URLS = ("sqlite://", "sqlite:///:memory:")


class Store:
    def __init__(self, url: str) -> None:
        self._lock: AbstractContextManager[Any]
        if url in _MEMORY_URLS:
            # One shared, serialized connection: the default pool gives every thread its own
            # empty in-memory database, and scheduler steps run in threads (spec §1).
            self._engine = create_engine(url, poolclass=StaticPool,
                                         connect_args={"check_same_thread": False})
            self._lock = threading.RLock()
        else:
            self._engine = create_engine(url)
            self._lock = nullcontext()
        if self._engine.dialect.name == "sqlite":
            event.listen(self._engine, "connect", _sqlite_pragmas)
        Base.metadata.create_all(self._engine)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self._lock, Session(self._engine, expire_on_commit=False) as s:
            yield s
```

Add next to `get_flag`:

```python
    def flags(self, prefix: str) -> dict[str, str]:
        with self._session() as s:
            q = select(FlagRow).where(FlagRow.key.startswith(prefix, autoescape=True))
            return {r.key: r.value for r in s.scalars(q)}
```

Add at module level (after `Store`):

```python
def snapshot_sqlite(url: str, dest: Path) -> str:
    """A consistent copy of a SQLite state DB for a dry run, read through a read-only
    connection so the source is never written (spec §2). A missing source gives an empty copy."""
    if not url.startswith("sqlite:///"):
        raise ValueError(f"not a SQLite file URL: {url}")
    src = Path(url.removeprefix("sqlite:///"))
    if src.exists():
        with closing(sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)) as s, \
                closing(sqlite3.connect(dest)) as d:
            s.backup(d)
    return f"sqlite:///{dest}"
```

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest tests/test_store.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/store.py tests/test_store.py
git commit -m "feat(store): thread-safe in-memory store, prefix flags, sqlite snapshot"
```

---

### Task 2: Workspaces — base lock, blob helpers, tracked-only commit, incorporate_remote

**Files:**
- Modify: `src/agent_sdlc/workspaces.py`, `src/agent_sdlc/ports.py`
- Test: `tests/test_workspaces.py`

**Interfaces:**
- Produces:
  - `class MergeConflict(Exception)` with `.files: list[str]` (in `workspaces.py`)
  - `Workspaces.head(wt: Path) -> str`
  - `Workspaces.blobs(wt: Path, paths: list[str]) -> dict[str, str]` — every requested path, value `"deleted"` when absent at HEAD
  - `Workspaces.has_tracked_changes(wt: Path) -> bool`
  - `Workspaces.commit(wt: Path, message: str, tracked_only: bool = False) -> bool`
  - `Workspaces.diff_lines(wt: Path, paths: list[str] | None = None) -> int`
  - `Workspaces.incorporate_remote(wt: Path, branch: str) -> list[str]` — paths changed by merged human commits; raises `MergeConflict` or `GitError`
  - The same signatures on `WorkspacePort` in `ports.py`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_workspaces.py`; add `import threading` and `import shutil`)

```python
BR = "agent/1-a"


def _pushed(ws: Workspaces, origin: Path) -> Path:
    """A worktree with one agent commit pushed to origin's BR."""
    wt = ws.create(1, BR)
    (wt / "a.txt").write_text("agent\n")
    ws.commit(wt, "feat: a")
    git("push", "-q", str(origin), f"HEAD:refs/heads/{BR}", cwd=wt)
    return wt


def _human_push(tmp_path: Path, origin: Path, files: dict[str, str | None]) -> None:
    """Clone origin, apply `files` on BR (None deletes), commit as a human, push."""
    h = tmp_path / "human"
    if not h.exists():
        git("clone", "-q", str(origin), str(h), cwd=tmp_path)
    git("fetch", "-q", "origin", cwd=h)
    git("checkout", "-q", "-B", BR, f"origin/{BR}", cwd=h)
    for name, content in files.items():
        p = h / name
        if content is None:
            git("rm", "-q", name, cwd=h)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    git("add", "-A", cwd=h)
    git("-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "human", cwd=h)
    git("push", "-q", "origin", f"HEAD:refs/heads/{BR}", cwd=h)


def test_concurrent_create_both_succeed(ws: Workspaces) -> None:
    barrier, errors = threading.Barrier(2), []

    def make(n: int) -> None:
        barrier.wait(5)
        try:
            ws.create(n, f"agent/{n}-x")
        except Exception as e:  # noqa: BLE001 - collected for the assertion
            errors.append(e)

    threads = [threading.Thread(target=make, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert (ws.worktree_path(1) / "check.sh").exists()
    assert (ws.worktree_path(2) / "check.sh").exists()


def test_commit_tracked_only_skips_untracked(ws: Workspaces) -> None:
    wt = ws.create(1, BR)
    (wt / "build.txt").write_text("out")
    assert ws.commit(wt, "style", tracked_only=True) is False
    (wt / "README.md").write_text("changed\n")
    assert ws.has_tracked_changes(wt) is True
    assert ws.commit(wt, "style", tracked_only=True) is True
    assert "build.txt" not in git("ls-files", cwd=wt)
    assert ws.has_tracked_changes(wt) is False


def test_blobs_marks_absent_paths_deleted(ws: Workspaces) -> None:
    wt = ws.create(1, BR)
    b = ws.blobs(wt, ["README.md", "nope.txt"])
    assert b["nope.txt"] == "deleted" and len(b["README.md"]) == 40
    assert ws.head(wt) == git("rev-parse", "HEAD", cwd=wt).strip()


def test_diff_lines_can_be_limited_to_paths(ws: Workspaces) -> None:
    wt = ws.create(1, BR)
    (wt / "a.txt").write_text("1\n2\n")
    (wt / "b.txt").write_text("1\n")
    ws.commit(wt, "feat")
    assert ws.diff_lines(wt) == 3 and ws.diff_lines(wt, ["b.txt"]) == 1


def test_incorporate_fast_forwards_human_commits(
    ws: Workspaces, tmp_path: Path, origin_repo: Path
) -> None:
    wt = _pushed(ws, origin_repo)
    _human_push(tmp_path, origin_repo, {"h.txt": "human\n"})
    assert ws.incorporate_remote(wt, BR) == ["h.txt"]
    assert (wt / "h.txt").read_text() == "human\n"


def test_incorporate_merges_diverged_history(
    ws: Workspaces, tmp_path: Path, origin_repo: Path
) -> None:
    wt = _pushed(ws, origin_repo)
    _human_push(tmp_path, origin_repo, {"h.txt": "human\n"})
    (wt / "local.txt").write_text("unpushed\n")
    ws.commit(wt, "feat: local")
    assert ws.incorporate_remote(wt, BR) == ["h.txt"]
    assert (wt / "h.txt").exists() and (wt / "local.txt").exists()


def test_incorporate_conflict_aborts_and_raises(
    ws: Workspaces, tmp_path: Path, origin_repo: Path
) -> None:
    wt = _pushed(ws, origin_repo)
    _human_push(tmp_path, origin_repo, {"a.txt": "human\n"})
    (wt / "a.txt").write_text("agent again\n")
    ws.commit(wt, "feat: again")
    with pytest.raises(MergeConflict) as e:
        ws.incorporate_remote(wt, BR)
    assert e.value.files == ["a.txt"]
    assert git("status", "--porcelain", cwd=wt) == ""


def test_incorporate_nothing_new_returns_empty(ws: Workspaces, origin_repo: Path) -> None:
    wt = _pushed(ws, origin_repo)
    assert ws.incorporate_remote(wt, BR) == []


def test_incorporate_missing_remote_branch_returns_empty(ws: Workspaces) -> None:
    wt = ws.create(1, BR)                      # never pushed, or deleted by a human
    assert ws.incorporate_remote(wt, BR) == []


def test_incorporate_unreachable_remote_raises_git_error(
    ws: Workspaces, origin_repo: Path
) -> None:
    wt = ws.create(1, BR)
    shutil.move(str(origin_repo), str(origin_repo) + ".gone")
    with pytest.raises(GitError):
        ws.incorporate_remote(wt, BR)
```

Update the import line: `from agent_sdlc.workspaces import GitError, MergeConflict, Workspaces, git_env, slugify`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_workspaces.py -q`
Expected: FAIL — `ImportError: cannot import name 'MergeConflict'`.

- [ ] **Step 3: Implement** in `src/agent_sdlc/workspaces.py`

Add `import threading`. After `class GitError`:

```python
class MergeConflict(Exception):
    """Human commits on the PR branch conflict with the agent's (spec §3.1)."""

    def __init__(self, files: list[str]) -> None:
        super().__init__("merge conflict: " + (", ".join(files) or "(no files reported)"))
        self.files = files
```

In `Workspaces.__init__` add: `self._base_lock = threading.Lock()  # guards the shared base .git (spec §1)`.

Replace `_git` with a pair:

```python
    def _run_git(self, *args: str, cwd: Path,
                 auth: bool = False) -> subprocess.CompletedProcess[str]:
        cmd = ["git"]
        if auth and self._auth is not None:
            # Fetched per call: GitHub installation tokens expire after an hour.
            cmd += ["-c", f"http.extraheader={self._auth()}"]
        return subprocess.run([*cmd, *args], cwd=cwd, capture_output=True, text=True,
                              env=git_env(self.home))

    def _git(self, *args: str, cwd: Path, auth: bool = False, check: bool = True) -> str:
        r = self._run_git(*args, cwd=cwd, auth=auth)
        if check and r.returncode != 0:
            raise GitError(f"git {args[0]} failed: {r.stderr.strip()}")
        return r.stdout
```

Wrap `create` and `remove` bodies:

```python
    def create(self, item_id: int, branch: str) -> Path:
        path = self.worktree_path(item_id)
        with self._base_lock:
            if path.exists():
                return path
            self._ensure_base()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "-B", branch, str(path),
                      f"origin/{self._t.repo.base_branch}", cwd=self._base)
        return path
```

```python
    def remove(self, item_id: int, branch: str) -> None:
        path = self.worktree_path(item_id)
        with self._base_lock:
            if not self._base.exists():
                return
            if path.exists():
                self._git("worktree", "remove", "--force", str(path), cwd=self._base,
                          check=False)
                shutil.rmtree(path, ignore_errors=True)
            self._git("worktree", "prune", cwd=self._base, check=False)
            self._git("branch", "-D", branch, cwd=self._base, check=False)
```

Change `commit` and `diff_lines`, add helpers, rebuild `blob_digest` on `blobs`:

```python
    def commit(self, wt: Path, message: str, tracked_only: bool = False) -> bool:
        # tracked_only: never commit untracked output such as builds or coverage (spec §4.2).
        self._git("add", "-u" if tracked_only else "-A", cwd=wt)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt,
                                env=git_env(self.home)).returncode
        if staged == 0:
            return False
        self._git(*_GIT_ID, "commit", "--no-verify", "-m", message, cwd=wt)
        return True

    def head(self, wt: Path) -> str:
        return self._git("rev-parse", "HEAD", cwd=wt).strip()

    def has_tracked_changes(self, wt: Path) -> bool:
        return bool(self._git("diff", "--name-only", "HEAD", cwd=wt).strip())

    def diff_lines(self, wt: Path, paths: list[str] | None = None) -> int:
        keep = None if paths is None else set(paths)
        total = 0
        for line in self._git("diff", "--numstat", self._range(), cwd=wt).splitlines():
            added, deleted, path = line.split("\t", 2)
            if keep is not None and path not in keep:
                continue
            total += (int(added) if added != "-" else 0) + (
                int(deleted) if deleted != "-" else 0)
        return total

    def blobs(self, wt: Path, paths: list[str]) -> dict[str, str]:
        """Blob id at HEAD for each path; "deleted" when the path is absent."""
        found: dict[str, str] = {}
        if paths:
            out = self._git("ls-tree", "-z", "HEAD", "--", *paths, cwd=wt)
            for entry in filter(None, out.split("\0")):
                meta, path = entry.split("\t", 1)
                found[path] = meta.split()[2]
        return {p: found.get(p, "deleted") for p in paths}

    def blob_digest(self, wt: Path, paths: list[str]) -> str:
        """sha256 over (path, blob at HEAD) for `paths`; a path absent at HEAD counts as
        deleted, so removing a manifest changes the digest too."""
        b = self.blobs(wt, paths)
        lines = [f"{p}:{b[p]}" for p in sorted(paths)]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()
```

Add `incorporate_remote`:

```python
    def _is_ancestor(self, a: str, b: str, wt: Path) -> bool:
        return self._run_git("merge-base", "--is-ancestor", a, b, cwd=wt).returncode == 0

    def _names(self, *args: str, wt: Path) -> list[str]:
        return sorted(filter(None, self._git(*args, cwd=wt).splitlines()))

    def incorporate_remote(self, wt: Path, branch: str) -> list[str]:
        """Bring commits a human pushed to the PR branch into the worktree (spec §3.1).
        Returns the paths they changed. Raises MergeConflict (after aborting the merge) when
        the histories conflict, and GitError when the remote cannot be read."""
        with self._base_lock:
            r = self._run_git("fetch", "origin", f"refs/heads/{branch}", cwd=wt, auth=True)
        if r.returncode != 0:
            if "couldn't find remote ref" in r.stderr.lower():
                return []                      # never pushed, or deleted by a human
            raise GitError(f"git fetch failed: {r.stderr.strip()}")
        old = self.head(wt)
        tip = self._git("rev-parse", "FETCH_HEAD", cwd=wt).strip()
        if self._is_ancestor(tip, old, wt):
            return []
        if self._is_ancestor(old, tip, wt):
            self._git("merge", "--ff-only", tip, cwd=wt)
        elif self._run_git(*_GIT_ID, "merge", "--no-edit", tip, cwd=wt).returncode != 0:
            files = self._names("diff", "--name-only", "--diff-filter=U", wt=wt)
            self._git("merge", "--abort", cwd=wt, check=False)
            raise MergeConflict(files)
        return self._names("diff", "--name-only", old, "HEAD", wt=wt)
```

In `src/agent_sdlc/ports.py`, `WorkspacePort`: change `commit` and `diff_lines`, add the new methods:

```python
    def commit(self, wt: Path, message: str, tracked_only: bool = False) -> bool: ...
    def head(self, wt: Path) -> str: ...
    def has_tracked_changes(self, wt: Path) -> bool: ...
    def diff_lines(self, wt: Path, paths: list[str] | None = None) -> int: ...
    def blobs(self, wt: Path, paths: list[str]) -> dict[str, str]: ...
    def incorporate_remote(self, wt: Path, branch: str) -> list[str]: ...
```

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest tests/test_workspaces.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/workspaces.py src/agent_sdlc/ports.py tests/test_workspaces.py
git commit -m "feat(workspaces): base-repo lock, blob helpers, tracked-only commit, incorporate_remote"
```

---

### Task 3: Scheduler — concurrent items, per-item busy flags, status

**Files:**
- Modify: `src/agent_sdlc/orchestrator/scheduler.py`, `src/agent_sdlc/cli.py` (`_target_status`)
- Test: `tests/test_scheduler.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `Store.flags(prefix)` (Task 1).
- Produces: `Scheduler.tick(wait: bool = True) -> None`; `Scheduler._running: dict[int, threading.Thread]` (tests join it); busy flags `busy:<target>:<external_id>` = `<stage>|<since>`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_scheduler.py`; add `import threading`)

```python
class GatedExecutor:
    """Per-item results; an item with a gate blocks until the gate is set (spec §1 tests)."""

    def __init__(self, results: dict[int, StepResult | Exception],
                 gates: dict[int, threading.Event] | None = None,
                 barrier: threading.Barrier | None = None) -> None:
        self.results, self.gates, self.barrier = results, gates or {}, barrier
        self.seen: set[int] = set()

    async def run(self, item: Item) -> StepResult:
        self.seen.add(item.external_id)
        if self.barrier is not None:
            self.barrier.wait(5)             # every item must be running at the same time
        gate = self.gates.get(item.external_id)
        if gate is not None:
            assert gate.wait(5)
        r = self.results[item.external_id]
        if isinstance(r, Exception):
            raise r
        return r


def _limit(target: TargetConfig, n: int) -> TargetConfig:
    return target.model_copy(update={"limits": target.limits.model_copy(
        update={"max_concurrent_items": n})})


def _two_implementing(store: Store) -> None:
    for i in (5, 6):
        store.add_item("fixture", replace(WI, id=i), f"agent/{i}-x")
        store.save(replace(_it(store, i), stage=Stage.IMPLEMENT))


def _drain(s: Scheduler) -> None:
    for th in list(s._running.values()):  # noqa: SLF001 - joining the item threads
        th.join(5)


async def test_items_run_concurrently(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    _two_implementing(store)
    ex = GatedExecutor({5: StepResult(Transition(Stage.VERIFY)),
                        6: StepResult(Transition(Stage.VERIFY))}, barrier=threading.Barrier(2))
    await Scheduler(target=_limit(target, 2), store=store, executor=ex, forge=ado,
                    workspaces=ws, clock=lambda: NOW).tick()
    assert _it(store, 5).stage is Stage.VERIFY and _it(store, 6).stage is Stage.VERIFY


async def test_limit_holds_and_freed_slot_is_refilled(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    _two_implementing(store)
    gate = threading.Event()
    ex = GatedExecutor({5: StepResult(Transition(Stage.VERIFY)),
                        6: StepResult(Transition(Stage.VERIFY))}, gates={5: gate})
    s = Scheduler(target=_limit(target, 1), store=store, executor=ex, forge=ado,
                  workspaces=ws, clock=lambda: NOW)
    await s.tick(wait=False)
    await s.tick(wait=False)                  # 5 still running: 6 must not start
    assert ex.seen == {5}
    assert set(store.flags("busy:fixture:")) == {"busy:fixture:5"}
    assert (store.get_flag("busy:fixture:5") or "").startswith("implement|")
    gate.set()
    _drain(s)
    assert store.flags("busy:fixture:") == {}
    await s.tick()
    assert ex.seen == {5, 6} and _it(store, 6).stage is Stage.VERIFY


async def test_run_forever_clears_stale_busy_flags(env) -> None:  # type: ignore[no-untyped-def]
    store = env[0]
    store.set_flag("busy:fixture:9", "implement|2026-09-01T00:00:00+00:00")
    store.set_flag("busy:other:9", "implement|2026-09-01T00:00:00+00:00")
    stop = threading.Event()
    stop.set()
    await sched(env, GatedExecutor({})).run_forever(1, stop)
    assert store.get_flag("busy:fixture:9") is None
    assert store.get_flag("busy:other:9") is not None


async def test_requeue_scan_skips_a_running_item(env) -> None:  # type: ignore[no-untyped-def]
    """Review focus 1: a running item that parked must not get park side effects twice."""
    store, ado, ws, target = env
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.IMPLEMENT))
    gate = threading.Event()
    ex = GatedExecutor({5: StepResult(Transition(Stage.VERIFY))}, gates={5: gate})
    s = sched(env, ex)
    await s.tick(wait=False)
    store.save(replace(_it(store), stage=Stage.PARKED, park_reason=ParkReason.RED,
                       parked_from=Stage.VERIFY))   # as if the item thread just parked
    await s.tick(wait=False)
    assert ado.wi_comments == [] and "agent:parked" not in ado.tags[5]
    gate.set()
    _drain(s)
```

Replace `test_i1_error_on_one_item_does_not_stop_others` with:

```python
async def test_i1_error_on_one_item_does_not_stop_others(env) -> None:  # type: ignore[no-untyped-def]
    store, ado, ws, target = env
    _two_implementing(store)
    ex = GatedExecutor({5: RuntimeError("boom"), 6: StepResult(Transition(Stage.VERIFY))})
    await Scheduler(target=_limit(target, 2), store=store, executor=ex, forge=ado,
                    workspaces=ws, clock=lambda: NOW).tick()
    assert ex.seen == {5, 6}
    assert _it(store).infra_failures == 1 and _it(store, 6).stage is Stage.VERIFY
```

In `tests/test_cli.py` replace the two busy tests:

```python
def test_status_lists_each_busy_item(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    store = Store(db)
    since = datetime.now(UTC) - timedelta(minutes=10)
    store.set_flag("last_tick:rallysource", datetime.now(UTC).isoformat())
    store.set_flag("busy:rallysource:9", f"implement|{since.isoformat()}")
    store.set_flag("busy:rallysource:12", f"verify|{since.isoformat()}")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "busy: rallysource#9 implement for 10m" in out
    assert "busy: rallysource#12 verify for 10m" in out
    assert "LOOP NOT RUNNING?" not in out and "STUCK?" not in out


def test_status_busy_past_stale_limit_is_stuck(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(db)
    since = datetime.now(UTC) - timedelta(hours=3)
    store.set_flag("last_tick:rallysource", since.isoformat())
    store.set_flag("busy:rallysource:9", f"implement|{since.isoformat()}")
    assert run(db, "status") == 0
    out = capsys.readouterr().out
    assert "busy: rallysource#9 implement for 3h  STUCK?" in out and "LOOP NOT RUNNING?" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scheduler.py tests/test_cli.py -q`
Expected: FAIL — `TypeError: tick() got an unexpected keyword argument 'wait'` and the busy-line assertions.

- [ ] **Step 3: Implement** in `src/agent_sdlc/orchestrator/scheduler.py`

In `__init__` add:

```python
        self._running: dict[int, threading.Thread] = {}   # item id -> step thread (spec §1)
        self._run_lock = threading.Lock()
```

Replace `run_forever` and `tick`, add helpers:

```python
    async def run_forever(self, poll_s: int = 60,
                          stop: threading.Event | None = None) -> None:
        self._clear_busy()
        while stop is None or not stop.is_set():
            try:
                await self.tick(wait=False)
            except Exception:
                log.exception("tick failed")
            if stop is None:
                await asyncio.sleep(poll_s)
            else:
                await asyncio.to_thread(stop.wait, poll_s)

    def _busy_key(self, item: Item) -> str:
        return f"busy:{self._t.name}:{item.external_id}"

    def _clear_busy(self) -> None:
        """Busy flags left by a process that died mid-step (spec §1). Only the long-running
        loop does this: `requeue` builds a Scheduler too and must not wipe live flags."""
        for key in self._store.flags(f"busy:{self._t.name}:"):
            self._store.set_flag(key, None)

    def _is_running(self, item_id: int) -> bool:
        with self._run_lock:
            return item_id in self._running

    async def tick(self, wait: bool = True) -> None:
        now = self._clock()
        self._store.set_flag(f"last_tick:{self._t.name}", now.isoformat())
        if self._paused(now):
            return
        self._intake()
        self._requeue_untagged()
        self._warn_stale(now)
        for item in self._store.items(self._t.name, [Stage.AWAITING_HUMAN]):
            if not self._is_running(item.id):
                await self._step(item, now)
        if not self._agent_work_allowed(now):
            return
        launched: list[threading.Thread] = []
        for item in self._to_launch():
            if self._paused(self._clock()):
                break
            launched.append(self._launch(item, now))
        if wait:
            for th in launched:
                await asyncio.to_thread(th.join)

    def _to_launch(self) -> list[Item]:
        """In-flight items not already running, up to the free slots. Running items are left
        out before the limit applies, so a requeued item that sorts ahead can't exceed it."""
        active = self._store.items(self._t.name, ACTIVE_STAGES)
        with self._run_lock:
            running = set(self._running)
        free = self._t.limits.max_concurrent_items - len(running)
        waiting = [i for i in in_flight(active, len(active)) if i.id not in running]
        return waiting[:max(free, 0)]

    def _launch(self, item: Item, now: datetime) -> threading.Thread:
        """One daemon thread with its own event loop per item step (spec §1)."""
        def run() -> None:
            try:
                asyncio.run(self._step(item, now))
            except Exception:
                log.exception("step thread failed for %s", _ref(item))
            finally:
                with self._run_lock:
                    self._running.pop(item.id, None)

        th = threading.Thread(target=run, name=f"item-{_ref(item)}", daemon=True)
        with self._run_lock:
            self._running[item.id] = th
        th.start()
        return th
```

In `_requeue_untagged`, first line of the loop body:

```python
            if self._is_running(item.id):
                continue   # its own thread is still committing and running side effects
```

Replace `_step`:

```python
    async def _step(self, item: Item, now: datetime) -> None:
        # Lets `status` show every running item and tell a long step from a dead loop (I5).
        key = self._busy_key(item)
        self._store.set_flag(key, f"{item.stage.value}|{now.isoformat()}")
        try:
            with log_context(_ref(item), item.stage.value):
                await self._run_step(item, now)
        finally:
            self._store.set_flag(key, None)
```

In `src/agent_sdlc/cli.py`, replace the body of `_target_status` from `tick = store.get_flag(...)` to just before `stale_after = ...` with:

```python
    tick = store.get_flag(f"last_tick:{target.name}")
    if tick is None:
        print("last tick: never  LOOP NOT RUNNING?")
    else:
        age = now - datetime.fromisoformat(tick)
        poll = int(store.get_flag("poll_s") or 60)
        warn = "  LOOP NOT RUNNING?" if age > timedelta(seconds=3 * poll) else ""
        print(f"last tick: {_ago(age)} ago{warn}")
    stuck_after = timedelta(minutes=target.limits.stale_after_minutes)
    for key, value in sorted(store.flags(f"busy:{target.name}:").items()):
        stage, since = value.split("|", 1)
        busy_age = now - datetime.fromisoformat(since)
        stuck = "  STUCK?" if busy_age > stuck_after else ""
        print(f"busy: {target.name}#{key.rsplit(':', 1)[1]} {stage} for "
              f"{_ago(busy_age)}{stuck}")
```

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest tests/test_scheduler.py tests/test_cli.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass. If an e2e test flakes, it is racing a `tick(wait=False)`; only `run_forever` may pass `wait=False`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/scheduler.py src/agent_sdlc/cli.py tests/test_scheduler.py tests/test_cli.py
git commit -m "feat(scheduler): run a target's in-flight items concurrently"
```

---

### Task 4: Isolated dry run — DryRunForge, CLI `--dry-run`

**Files:**
- Create: `src/agent_sdlc/adapters/dry_run.py`, `tests/test_dry_run.py`
- Modify: `src/agent_sdlc/adapters/ado.py`, `src/agent_sdlc/adapters/github.py`, `src/agent_sdlc/orchestrator/runtime.py`, `src/agent_sdlc/cli.py`
- Test: `tests/test_dry_run.py`, `tests/test_ado.py`, `tests/test_github.py`, `tests/test_runtime.py`

**Interfaces:**
- Consumes: `snapshot_sqlite(url, dest)` (Task 1).
- Produces: `DryRunForge(inner: ForgePort)`; `make_forge(target, *, dry_run: bool, secret=get_secret) -> ForgePort`; `build_scheduler(..., dry_run: bool, ...)`; CLI `run --once --dry-run` (alias `--dry-run-push`).

- [ ] **Step 1: Write the failing tests** — create `tests/test_dry_run.py`:

```python
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from agent_sdlc.adapters.dry_run import DryRunForge
from agent_sdlc.cli import main
from agent_sdlc.store import Store
from agent_sdlc.types import PrComment, Stage, WorkItem
from tests.fakes import FakeDecider, FakeForge, FakeRunner

WI = WorkItem(5, "Add feature", "d", "ac", "Bug", ("agent",), "u")


def test_dry_run_forge_reads_through_and_skips_writes(tmp_path: Path) -> None:
    inner = FakeForge()
    inner.add(WI)
    f = DryRunForge(inner)
    assert f.kind == "ado" and f.label_word == "tag"
    assert f.list_intake() == [WI] and f.get_item(5) == WI and f.has_label(5, "agent")
    f.comment_item(5, "x")
    f.set_label(5, "agent:parked", True)
    f.push_branch(tmp_path, "agent/5-x")
    assert f.create_pr("agent/5-x", "t", "b", 5) == 0
    f.update_pr(0, "b", 5)
    f.reply_pr(0, PrComment(1, 1, "a", "c"), "x")
    f.comment_pr(0, "x")
    f.delete_branch("agent/5-x")
    assert f.pr_status(0) == "active" and f.pr_comments(0) == []
    assert inner.wi_comments == [] and inner.prs == {} and inner.replies == []
    assert inner.tags[5] == {"agent"} and inner.deleted_branches == []


def test_dry_run_forge_reads_real_prs(tmp_path: Path) -> None:
    inner = FakeForge()
    pr = inner.create_pr("agent/5-x", "t", "b", 5)
    inner.prs[pr]["status"] = "completed"
    assert DryRunForge(inner).pr_status(pr) == "completed"


TARGET = """name: fixture
forge: {{kind: ado, org: o, project: p, repo: r}}
repo:
  base_branch: dev
  clone_url: {origin}
  install: "true"
  commands: {{test: sh check.sh}}
policy: {{protected_paths: ["infra/**"]}}
"""


def _db_files(db: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in db.parent.glob(db.name + "*")}


def _leftovers() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("agent-sdlc-dry-run-*"))


def test_cli_dry_run_is_isolated(
    tmp_path: Path, origin_repo: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import agent_sdlc.cli as cli_mod
    import agent_sdlc.orchestrator.runtime as rt

    spy = FakeForge()                      # no origin: a real push would fail loudly
    spy.add(WI)
    monkeypatch.setenv("AGENT_SDLC_ADO_PAT", "pat")
    monkeypatch.setattr(rt, "AdoForge", lambda *a, **k: spy)
    monkeypatch.setattr(rt, "ClaudeAgentRunner", lambda *a, **k: FakeRunner())
    monkeypatch.setattr(cli_mod, "claude_auth_env", lambda cfg: {})
    monkeypatch.setattr(cli_mod, "_decider", lambda cfg, store: FakeDecider())
    target = tmp_path / "t.yaml"
    target.write_text(TARGET.format(origin=origin_repo))
    db = tmp_path / "state.db"
    store = Store(f"sqlite:///{db}")
    it = store.add_item("fixture", WI, "agent/5-add-feature")
    assert it is not None
    store.save(replace(it, stage=Stage.PR_OPEN, data={"plan": "p", "checks": []}))
    before, temps = _db_files(db), _leftovers()

    rc = main(["--target", str(target), "--db", f"sqlite:///{db}", "run", "--once",
               "--dry-run"])

    assert rc == 0
    assert _db_files(db) == before
    assert Store(f"sqlite:///{db}").get_by_ref("fixture", 5).stage is Stage.PR_OPEN
    assert spy.prs == {} and spy.wi_comments == [] and spy.tags[5] == {"agent"}
    assert _leftovers() == temps
    out = capsys.readouterr().out
    kept = Path(out.split("dry run: traces kept in ", 1)[1].strip())
    assert kept.is_dir() and kept.parts[-3] == "dry-run"


def test_cli_dry_run_needs_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    rc = main(["--target", str(root / "targets" / "rallysource.yaml"),
               "--db", f"sqlite:///{tmp_path / 's.db'}", "run", "--dry-run"])
    assert rc == 1 and "--dry-run needs --once" in capsys.readouterr().out


def test_cli_dry_run_needs_sqlite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    rc = main(["--target", str(root / "targets" / "rallysource.yaml"),
               "--db", "postgresql://h/db", "run", "--once", "--dry-run"])
    assert rc == 1 and "--dry-run needs a SQLite --db" in capsys.readouterr().out
```

Adapter test updates:
- `tests/test_ado.py`: delete `test_dry_run_pr_methods_make_no_http_calls` and `test_push_branch_dry_run_does_nothing` (now covered by `test_dry_run_forge_reads_through_and_skips_writes`).
- `tests/test_github.py`: in the `forge(...)` helper drop the `dry_run` parameter and the `dry_run_push=dry_run` argument; in `test_comment_item_posts_html` call `forge()` and delete the trailing comment; delete `test_dry_run_makes_pr_side_effects_no_ops`.
- `tests/test_runtime.py`: in the three `make_forge` tests pass `dry_run=False`; in `test_i3_build_scheduler_wires_...` pass `dry_run=False` to `build_scheduler`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_dry_run.py tests/test_runtime.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_sdlc.adapters.dry_run'`.

- [ ] **Step 3: Implement**

Create `src/agent_sdlc/adapters/dry_run.py`:

```python
from __future__ import annotations

import logging
from pathlib import Path

from agent_sdlc.ports import ForgePort
from agent_sdlc.types import PrComment, WorkItem

log = logging.getLogger(__name__)


class DryRunForge:
    """A forge for `run --dry-run`: reads go to the real tracker, every write is logged and
    skipped (spec §2). PR 0 stands for the PR a dry run would have opened."""

    def __init__(self, inner: ForgePort) -> None:
        self._inner = inner
        self.kind = inner.kind
        self.label_word = inner.label_word

    # reads -------------------------------------------------------------------
    def list_intake(self) -> list[WorkItem]:
        return self._inner.list_intake()

    def list_closed(self, limit: int) -> list[WorkItem]:
        return self._inner.list_closed(limit)

    def get_item(self, id: int) -> WorkItem:
        return self._inner.get_item(id)

    def has_label(self, id: int, label: str) -> bool:
        return self._inner.has_label(id, label)

    def git_auth_header(self) -> str:
        return self._inner.git_auth_header()

    def pr_ref(self, pr_id: int) -> str:
        return self._inner.pr_ref(pr_id)

    def item_ref(self, item_id: int) -> str:
        return self._inner.item_ref(item_id)

    def pr_status(self, pr_id: int) -> str:
        return "active" if pr_id == 0 else self._inner.pr_status(pr_id)

    def pr_comments(self, pr_id: int) -> list[PrComment]:
        return [] if pr_id == 0 else self._inner.pr_comments(pr_id)

    # writes ------------------------------------------------------------------
    def comment_item(self, id: int, html: str) -> None:
        log.info("dry-run: would comment on item %s", id)

    def set_label(self, id: int, label: str, present: bool) -> None:
        log.info("dry-run: would %s %s on item %s", "add" if present else "remove", label, id)

    def push_branch(self, worktree: Path, branch: str) -> None:
        log.info("dry-run: would push %s", branch)

    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int:
        log.info("dry-run: would open PR %s\n%s", title, body)
        return 0

    def update_pr(self, pr_id: int, body: str, item_id: int) -> None:
        log.info("dry-run: would update PR %s description", pr_id)

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        log.info("dry-run: would reply on PR %s to %s: %s", pr_id, comment.key, text)

    def comment_pr(self, pr_id: int, text: str) -> None:
        log.info("dry-run: would comment on PR %s: %s", pr_id, text)

    def delete_branch(self, branch: str) -> None:
        log.info("dry-run: would delete branch %s", branch)
```

`src/agent_sdlc/adapters/ado.py`: remove the `dry_run_push` parameter, `self._dry_run`, and every `if self._dry_run:` block (in `push_branch`, `create_pr`, `update_pr`, `pr_status`, `pr_comments`, `reply_pr`, `comment_pr`, `delete_branch`).

`src/agent_sdlc/adapters/github.py`: same removals (parameter, attribute, and the `if self._dry_run:` blocks in `push_branch`, `create_pr`, `update_pr`, `pr_status`, `pr_comments`, `reply_pr`, `comment_pr`, `delete_branch`).

`src/agent_sdlc/orchestrator/runtime.py`:

```python
from agent_sdlc.adapters.dry_run import DryRunForge


def make_forge(target: TargetConfig, *, dry_run: bool,
               secret: Secret = get_secret) -> ForgePort:
    forge = _real_forge(target, secret)
    return DryRunForge(forge) if dry_run else forge   # writes skipped in a dry run (spec §2)


def _real_forge(target: TargetConfig, secret: Secret) -> ForgePort:
    f, repo = target.forge, target.repo
    if isinstance(f, AdoForgeConfig):
        return AdoForge(f, secret(f.pat_secret, "AGENT_SDLC_ADO_PAT"), intake=target.intake,
                        base_branch=repo.base_branch, branch_prefix=repo.branch_prefix)
    if f.app_id is None:
        raise ValueError(f"set forge.app_id for target {target.name} (spec §7)")
    http = httpx.Client(base_url=f.api_url, timeout=30)
    auth = GitHubAppAuth(app_id=f.app_id, private_key=secret(*github_app_key(f.app_id)),
                         owner=f.owner, repo=f.repo, http=http,
                         installation_id=f.installation_id)
    return GitHubForge(f, auth, intake=target.intake, base_branch=repo.base_branch,
                       branch_prefix=repo.branch_prefix, http=http)
```

In `build_scheduler` rename the parameter `dry_run_push: bool` to `dry_run: bool` and call `make_forge(target, dry_run=dry_run)`.

`src/agent_sdlc/cli.py`:
- Imports: `import tempfile`; `from agent_sdlc.fsutil import ensure_private_dir`; `from agent_sdlc.store import Store, snapshot_sqlite`.
- Parser: replace `run.add_argument("--dry-run-push", action="store_true")` with
  `run.add_argument("--dry-run", "--dry-run-push", dest="dry_run", action="store_true", help="isolated preview: snapshot DB, temp workspaces, no tracker writes")`.
- `_scheduler(..., dry_run: bool)` passes `dry_run=dry_run` to `build_scheduler`.
- `label triage`: `make_forge(target, dry_run=True)`.
- `requeue` (non-local): `_scheduler(..., dry_run=False)`.
- `run`: `_scheduler(loaded, t, store, args, decider, slots, args.dry_run)`.
- Split `main`:

```python
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "run" and args.dry_run:
        return _dry_run(args)
    return _main(args)


def _dry_run(args: argparse.Namespace) -> int:
    """Run one tick against a snapshot DB and temp workspaces with a write-free forge; keep
    only the traces (spec §2)."""
    if not args.once:
        print("--dry-run needs --once")
        return 1
    if not args.db.startswith("sqlite:///"):
        print("--dry-run needs a SQLite --db (sqlite:///path)")
        return 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    kept = Path(args.traces).expanduser().parent / "dry-run" / stamp / "traces"
    ensure_private_dir(kept)
    with tempfile.TemporaryDirectory(prefix="agent-sdlc-dry-run-") as tmp:
        args.db = snapshot_sqlite(args.db, Path(tmp) / "state.db")
        args.workspaces = str(Path(tmp) / "workspaces")
        args.traces = str(kept)
        rc = _main(args)
    print(f"dry run: traces kept in {kept}")
    return rc


def _main(args: argparse.Namespace) -> int:
    configure_logging(Path(args.logs))
    # ... the rest of the former main() body, unchanged ...
```

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest tests/test_dry_run.py tests/test_ado.py tests/test_github.py tests/test_runtime.py -q && uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/adapters src/agent_sdlc/orchestrator/runtime.py src/agent_sdlc/cli.py tests/test_dry_run.py tests/test_ado.py tests/test_github.py tests/test_runtime.py
git commit -m "feat: isolated dry run (snapshot DB, temp workspaces, write-free forge)"
```

---

### Task 5: PR replies after the commit

**Files:**
- Modify: `src/agent_sdlc/orchestrator/stages.py` (`StepResult`, `_awaiting`), `src/agent_sdlc/orchestrator/scheduler.py` (`_run_step`)
- Test: `tests/test_stages.py`, `tests/test_scheduler.py`

**Interfaces:**
- Produces: `StepResult.replies: list[tuple[PrComment, str]]`; scheduler event `pr_reply_failed` with payload `{"comment": <PrComment.key>, "error": <str ≤500>}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_stages.py::test_awaiting_handles_comments` replace
`assert [t for _, t, _ in ado.replies] == [2]` with:

```python
    assert ado.replies == []                                   # the scheduler sends them
    assert [(c.thread_id, text) for c, text in res.replies] == [(2, QUESTION_REPLY)]
```

and add `from agent_sdlc.orchestrator.reporting import QUESTION_REPLY`.

Append to `tests/test_scheduler.py` (add `from agent_sdlc.types import PrComment` to the import block):

```python
@dataclass
class ReplyForge(FakeForge):
    store: Store | None = None
    fail_replies: bool = False
    seen_at_reply: list[list[str]] = field(default_factory=list)

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        assert self.store is not None
        self.seen_at_reply.append(list(self.store.get_by_ref("fixture", 5)
                                       .data.get("seen_comments", [])))
        if self.fail_replies:
            raise httpx.ConnectError("down")
        super().reply_pr(pr_id, comment, text)


async def test_replies_are_sent_after_the_commit_and_at_most_once(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig
) -> None:
    store = Store("sqlite://")
    forge = ReplyForge(store=store, fail_replies=True)
    forge.add(WI)
    pr = forge.create_pr("agent/5-add-feature", "t", "b", 5)
    forge.pr_threads[pr] = [PrComment(2, 1, "Brian", "why this approach?")]
    decider = FakeDecider()
    decider.answers["comment"] = {"comment_intent": "question"}
    ws = Workspaces(tmp_path / "ws", target)
    ex = StageExecutor(target=target, forge=forge, decider=decider, runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(target.policy.protected_paths),
                       decisions_for=store.decisions_for)
    s = Scheduler(target=target, store=store, executor=ex, forge=forge, workspaces=ws,
                  clock=lambda: NOW)
    store.add_item("fixture", WI, "agent/5-add-feature")
    store.save(replace(_it(store), stage=Stage.AWAITING_HUMAN, pr_id=pr))
    await s.tick()
    assert forge.seen_at_reply == [["thread:2:1"]]            # saved before the reply
    assert [e.kind for e in store.events_for(_it(store).id)].count("pr_reply_failed") == 1
    forge.fail_replies = False
    await s.tick()
    assert forge.seen_at_reply == [["thread:2:1"]] and forge.replies == []   # not re-sent
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stages.py::test_awaiting_handles_comments tests/test_scheduler.py -k "replies or awaiting_handles" -q`
Expected: FAIL — `AttributeError: 'StepResult' object has no attribute 'replies'`.

- [ ] **Step 3: Implement**

`stages.py`: add `from agent_sdlc.types import PrComment` to the types import; add to `StepResult`:

```python
    # PR replies the scheduler sends after committing the step (spec §3.3)
    replies: list[tuple[PrComment, str]] = field(default_factory=list)
```

In `_awaiting`, create `replies: list[tuple[PrComment, str]] = []` next to `events`, replace `self._forge.reply_pr(item.pr_id, c, reply)` with `replies.append((c, reply))`, and return `StepResult(t, decisions=logged, data={"seen_comments": seen}, labels=labels, events=events, replies=replies)`.

`scheduler.py`: in `_run_step`, right after `self._store.commit_step(new, res.decisions, ...)`, add `self._send_replies(new, res.replies)`; add:

```python
    def _send_replies(self, item: Item, replies: list[tuple[PrComment, str]]) -> None:
        """After the commit that saved seen_comments, so a reply is sent at most once (§3.3)."""
        for comment, text in replies:
            try:
                self._forge.reply_pr(item.pr_id or 0, comment, text)
            except _INFRA_ERRORS as e:
                log.warning("reply to %s failed on %s: %s", comment.key, _ref(item), e)
                self._store.add_event("pr_reply_failed",
                                      {"comment": comment.key, "error": str(e)[:500]}, item=item)
```

(add `PrComment` to the scheduler's `agent_sdlc.types` import.)

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass (`tests/e2e/test_pipeline.py` still sees one reply, now sent by the scheduler).

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/stages.py src/agent_sdlc/orchestrator/scheduler.py tests/test_stages.py tests/test_scheduler.py
git commit -m "fix: send PR replies after the step commit, at most once"
```

---

### Task 6: Incorporate human commits; judge only the agent's files

**Files:**
- Modify: `src/agent_sdlc/orchestrator/stages.py`
- Test: `tests/test_stages.py`

**Interfaces:**
- Consumes: `Workspaces.incorporate_remote`, `.blobs`, `.diff_lines(wt, paths)`, `MergeConflict` (Task 2).
- Produces: `item.data["human_blobs"]: dict[str, str]` (path → blob or `"deleted"`); `StageExecutor._agent_owned(item, wt, paths) -> list[str]`; `_policy_park(item, wt, when)` (new first parameter).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_stages.py`; add `from tests.conftest import git`)

```python
BRANCH = "agent/5-add-feature"


def _pr_branch(ws: Workspaces, ado: FakeForge) -> Path:
    """Worktree with an agent commit pushed to origin, as after a PR was opened."""
    wt = ws.create(5, BRANCH)
    (wt / "feature.txt").write_text("v1\n")
    ws.commit(wt, "feat: v1")
    ado.push_branch(wt, BRANCH)
    return wt


def _human(tmp_path: Path, origin: Path, files: dict[str, str | None]) -> None:
    h = tmp_path / "human"
    if not h.exists():
        git("clone", "-q", str(origin), str(h), cwd=tmp_path)
    git("fetch", "-q", "origin", cwd=h)
    git("checkout", "-q", "-B", BRANCH, f"origin/{BRANCH}", cwd=h)
    for name, content in files.items():
        if content is None:
            git("rm", "-q", name, cwd=h)
        else:
            (h / name).parent.mkdir(parents=True, exist_ok=True)
            (h / name).write_text(content)
    git("add", "-A", cwd=h)
    git("-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "human", cwd=h)
    git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=h)


PR_ROUND = {"plan": "p", "feedback": "Reviewer requested changes"}


async def test_pr_round_incorporates_human_commits(  # type: ignore[no-untyped-def]
    parts, tmp_path: Path, origin_repo: Path
) -> None:
    ex, ado, ws, *_ = parts
    wt = _pr_branch(ws, ado)
    _human(tmp_path, origin_repo, {"human.txt": "fix\n"})
    res = await ex.run(item(Stage.IMPLEMENT, pr_id=100, data=PR_ROUND))
    assert res.transition.to is Stage.VERIFY
    assert (wt / "human.txt").read_text() == "fix\n"
    assert res.data["human_blobs"].keys() == {"human.txt"}


async def test_pr_round_conflict_parks_with_files(  # type: ignore[no-untyped-def]
    parts, tmp_path: Path, origin_repo: Path
) -> None:
    ex, ado, ws, *_ = parts
    wt = _pr_branch(ws, ado)
    _human(tmp_path, origin_repo, {"feature.txt": "human\n"})
    (wt / "feature.txt").write_text("agent local\n")
    ws.commit(wt, "feat: unpushed")
    res = await ex.run(item(Stage.IMPLEMENT, pr_id=100, data=PR_ROUND))
    assert res.transition.park_reason is ParkReason.NEEDS_HUMAN
    assert res.transition.note == ("PR branch has diverged and could not be merged: "
                                   "feature.txt")
    assert git("status", "--porcelain", cwd=wt) == ""


async def test_human_protected_edit_passes_but_agent_edit_is_caught(  # type: ignore[no-untyped-def]
    parts, tmp_path: Path, origin_repo: Path
) -> None:
    ex, ado, ws, _, runner = parts
    _pr_branch(ws, ado)
    _human(tmp_path, origin_repo, {"infra/main.tf": "human\n"})
    res = await ex.run(item(Stage.IMPLEMENT, pr_id=100, data=PR_ROUND))
    assert res.transition.to is Stage.VERIFY
    human = res.data["human_blobs"]

    def edit_infra(role, prompt, cwd):  # type: ignore[no-untyped-def]
        (cwd / "infra" / "main.tf").write_text("agent\n")
        return AgentResult("done", Usage(1, 1, 1))

    runner.behaviors = {"implementer": edit_infra}
    again = await ex.run(item(Stage.IMPLEMENT, pr_id=100,
                              data={**PR_ROUND, "human_blobs": human}))
    assert again.transition.park_reason is ParkReason.POLICY
    assert "infra/main.tf" in again.transition.note


async def test_human_manifest_edit_needs_no_approval(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    t = _with_manifests(target)
    ado = FakeForge(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", t)
    ex = StageExecutor(target=t, forge=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(t.policy.protected_paths),
                       decisions_for=lambda _id: [])
    _pr_branch(ws, ado)
    _human(tmp_path, origin_repo, {"package.json": '{"name": "x"}\n'})
    res = await ex.run(item(Stage.IMPLEMENT, pr_id=100, data=PR_ROUND))
    assert res.transition.to is Stage.VERIFY


async def test_human_deleting_a_protected_file_passes(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    """Review focus 5: deleting a protected base file is recorded as 'deleted' and is not
    the agent's change. README.md exists on the base branch, so the deletion is in the diff."""
    t = target.model_copy(update={"policy": target.policy.model_copy(
        update={"protected_paths": [*target.policy.protected_paths, "README.md"]})})
    ado = FakeForge(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", t)
    ex = StageExecutor(target=t, forge=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(t.policy.protected_paths),
                       decisions_for=lambda _id: [])
    _pr_branch(ws, ado)
    _human(tmp_path, origin_repo, {"README.md": None})
    res = await ex.run(item(Stage.IMPLEMENT, pr_id=100, data=PR_ROUND))
    assert res.data["human_blobs"] == {"README.md": "deleted"}
    assert res.transition.to is Stage.VERIFY


async def test_large_human_commit_does_not_trip_the_diff_limit(  # type: ignore[no-untyped-def]
    parts, tmp_path: Path, origin_repo: Path
) -> None:
    ex, ado, ws, *_ = parts
    _pr_branch(ws, ado)
    _human(tmp_path, origin_repo, {"big.txt": "".join(f"{i}\n" for i in range(500))})
    res = await ex.run(item(Stage.IMPLEMENT, pr_id=100, data=PR_ROUND))
    assert res.transition.to is Stage.VERIFY          # fixture max_diff_lines is 200
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stages.py -k "pr_round or human" -q`
Expected: FAIL — `human.txt` missing / `KeyError: 'human_blobs'`.

- [ ] **Step 3: Implement** in `src/agent_sdlc/orchestrator/stages.py`

Import `from agent_sdlc.workspaces import MergeConflict`.

Add helpers in the "policy, manifests & install" section:

```python
    def _agent_owned(self, item: Item, wt: Path, paths: list[str]) -> list[str]:
        """`paths` minus those still exactly as a human pushed them (spec §3.1): policy, the
        manifest gate and the diff limit judge only the agent's changes."""
        human: dict[str, str] = item.data.get("human_blobs") or {}
        theirs = [p for p in paths if p in human]
        if not theirs:
            return paths
        now = self._ws.blobs(wt, theirs)
        return [p for p in paths if p not in human or now[p] != human[p]]

    def _agent_diff_lines(self, item: Item, wt: Path) -> int:
        if not item.data.get("human_blobs"):
            return self._ws.diff_lines(wt)
        return self._ws.diff_lines(wt, self._agent_owned(item, wt, self._ws.changed_files(wt)))

    def _incorporate(self, item: Item, wt: Path) -> Item | StepResult:
        """Merge commits a human pushed to the PR branch and record the blobs of every path
        they touched (spec §3.1). A conflict parks for a human."""
        try:
            touched = self._ws.incorporate_remote(wt, item.branch)
        except MergeConflict as e:
            return StepResult(park(
                ParkReason.NEEDS_HUMAN,
                "PR branch has diverged and could not be merged: " + ", ".join(e.files)))
        if not touched:
            return item
        human = {**(item.data.get("human_blobs") or {}), **self._ws.blobs(wt, touched)}
        return replace(item, data={**item.data, "human_blobs": human})
```

Change `_policy_park`:

```python
    def _policy_park(self, item: Item, wt: Path, when: str) -> StepResult | None:
        """Protected-path and diff-limit checks, re-run wherever a park (e.g. an unapproved
        manifest) may have let a human requeue past them without a fresh recheck."""
        changed = self._agent_owned(item, wt, self._ws.changed_files(wt))
        violations = self._pp.violations(changed)
        if violations:
            return StepResult(park(
                ParkReason.POLICY, f"{when} found protected paths: " + ", ".join(violations)))
        lines, limit = self._agent_diff_lines(item, wt), self._t.policy.max_diff_lines
        if lines > limit:
            return StepResult(park(
                ParkReason.POLICY,
                f"{when}: the diff is {lines} lines, over the {limit}-line limit."))
        return None
```

and update its callers: `self._policy_park(item, wt, "Verify")`, `self._policy_park(item, wt, "Pre-push check")`.

In `_manifest_gate` change the first line to:

```python
        files = self._mp.violations(self._agent_owned(item, wt, self._ws.changed_files(wt)))
```

Split `_implement`:

```python
    async def _implement(self, item: Item) -> StepResult:
        wi = self._forge.get_item(item.external_id)
        wt = self._ws.create(item.external_id, item.branch)
        self._ws.reset(wt)
        human: dict[str, Any] = {}
        if item.pr_id:
            merged = self._incorporate(item, wt)
            if isinstance(merged, StepResult):
                return merged
            if merged is not item:
                item, human = merged, {"human_blobs": merged.data["human_blobs"]}
        res = await self._implement_changes(item, wi, wt)
        return replace(res, data={**human, **res.data}) if human else res

    async def _implement_changes(self, item: Item, wi: WorkItem, wt: Path) -> StepResult:
        # Resume at implement: pending feedback (e.g. a PR change request) must still apply (I2).
        if gate := self._manifest_gate(item, wt, Stage.IMPLEMENT):
            return gate
        # ... the rest of the former _implement body, unchanged except these two lines:
        files = self._ws.changed_files(wt)
        t = after_implement(self._pp.violations(self._agent_owned(item, wt, files)),
                            bool(files), self._agent_diff_lines(item, wt),
                            self._t.policy.max_diff_lines)
```

(add `WorkItem` to the `agent_sdlc.types` import.)

In `_verify`'s lint branch, change `violations = self._pp.violations(self._ws.changed_files(wt))` to `violations = self._pp.violations(self._agent_owned(item, wt, self._ws.changed_files(wt)))` and `lines, limit = self._ws.diff_lines(wt), ...` to `lines, limit = self._agent_diff_lines(item, wt), ...`.

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/stages.py tests/test_stages.py
git commit -m "feat: merge human commits on the PR branch; judge only the agent's files"
```

---

### Task 7: Park rounds that change nothing

**Files:**
- Modify: `src/agent_sdlc/orchestrator/stages.py` (`_implement_changes`)
- Test: `tests/test_stages.py`

**Interfaces:**
- Consumes: `Workspaces.head` (Task 2), `_implement_changes` (Task 6).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_stages.py`)

```python
def _idle(role, prompt, cwd):  # type: ignore[no-untyped-def]
    return AgentResult("nothing to change", Usage(1, 1, 1))


async def test_round_with_feedback_and_no_new_commit_parks(  # type: ignore[no-untyped-def]
    parts,
) -> None:
    ex, _, ws, _, runner = parts
    wt = ws.create(5, BRANCH)
    (wt / "feature.txt").write_text("v1\n")
    ws.commit(wt, "feat: v1")                         # an earlier round's change
    runner.behaviors = {"implementer": _idle}
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p", "feedback": "rename it"}))
    assert res.transition.park_reason is ParkReason.NEEDS_HUMAN
    assert res.transition.note == "The implementer made no changes for the feedback."
    assert "feedback" not in res.data                 # kept on the item for the requeue


async def test_first_implement_without_changes_keeps_existing_park(  # type: ignore[no-untyped-def]
    parts,
) -> None:
    ex, _, _, _, runner = parts
    runner.behaviors = {"implementer": _idle}
    res = await ex.run(item(Stage.IMPLEMENT, data={"plan": "p"}))
    assert res.transition.note == "The implementer made no changes."
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stages.py -k "no_new_commit or first_implement_without" -q`
Expected: first test FAILS (transition is VERIFY); second passes (guard).

- [ ] **Step 3: Implement** — in `_implement_changes`, right before `res, agent_evs, data = await self._run_agent(...)` add `before = self._ws.head(wt)`, and right after `self._ws.commit(wt, commit_message(...))` add:

```python
        if item.data.get("feedback") and self._ws.head(wt) == before:
            # Nothing new for the feedback: don't report the request as handled (spec §3.2).
            return StepResult(park(ParkReason.NEEDS_HUMAN,
                                   "The implementer made no changes for the feedback."),
                              res.usage, data=data, events=events)
```

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/stages.py tests/test_stages.py
git commit -m "fix: park a revision round that commits nothing for its feedback"
```

---

### Task 8: Separate agent-error counter

**Files:**
- Modify: `src/agent_sdlc/targets.py` (`Limits`), `src/agent_sdlc/orchestrator/transitions.py` (`after_agent_error`, `requeue`), `src/agent_sdlc/orchestrator/stages.py` (`_agent_failed`, success paths)
- Test: `tests/test_stages.py`, `tests/test_transitions.py`, `tests/test_targets.py`

**Interfaces:**
- Produces: `Limits.max_agent_errors: int = 3`; `after_agent_error(stage: Stage, error: str, errors: int, max_errors: int) -> Transition` (never sets `count_attempt`); `item.data["agent_errors"]: dict[str, int]` keyed by stage value.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_stages.py::test_i2_agent_error_retries_then_parks` body after `runner.behaviors = _failing(role)`:

```python
    data = {"plan": "p", "checks": [], "installed": True}
    res = await ex.run(item(stage, data=data))
    assert res.transition.to is stage and not res.transition.count_attempt
    assert res.data["agent_errors"] == {stage.value: 1}
    assert res.usage == Usage(2, 10, 1)
    assert decider.calls == []  # no gate decision on an unfinished agent run
    res = await ex.run(item(stage, data={**data, "agent_errors": {stage.value: 3}}))
    assert res.transition.park_reason is ParkReason.AGENT_ERROR
    assert "agent did not finish: error_max_turns" in res.transition.note
    assert res.usage == Usage(2, 10, 1)
```

Append to `tests/test_stages.py`:

```python
async def test_clean_session_clears_that_stages_agent_errors(parts) -> None:  # type: ignore[no-untyped-def]
    ex, *_ = parts
    res = await ex.run(item(Stage.PLAN, data={"agent_errors": {"plan": 2, "review": 1}}))
    assert res.transition.to is Stage.IMPLEMENT
    assert res.data["agent_errors"] == {"review": 1}
    res = await ex.run(item(Stage.PLAN, data={"agent_errors": {"plan": 2}}))
    assert res.data["agent_errors"] is None           # None removes the key on merge
```

Append to `tests/test_transitions.py`:

```python
def test_agent_error_does_not_count_toward_verify_retries() -> None:
    from agent_sdlc.orchestrator.transitions import after_agent_error

    t = after_agent_error(Stage.PLAN, "error_max_turns", 0, 3)
    assert t == Transition(Stage.PLAN)
    assert apply_transition(Item(1, "t", "x", "b", Stage.PLAN), t).attempt == 0


def test_requeue_drops_agent_errors() -> None:
    it = Item(1, "t", "x", "b", Stage.PARKED, park_reason=ParkReason.AGENT_ERROR,
              parked_from=Stage.PLAN, data={"agent_errors": {"plan": 4}, "plan": "p"})
    assert requeue(it).data == {"plan": "p"}
```

Append to `tests/test_targets.py`:

```python
def test_max_agent_errors_defaults_to_three() -> None:
    root = Path(__file__).resolve().parents[1]
    assert load_target(root / "targets" / "rallysource.yaml").limits.max_agent_errors == 3
```

(import `load_target` and `Path` if the module doesn't already.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stages.py tests/test_transitions.py tests/test_targets.py -q`
Expected: FAIL — `KeyError: 'agent_errors'`, `AttributeError: ... max_agent_errors`.

- [ ] **Step 3: Implement**

`targets.py` `Limits`: add `max_agent_errors: int = 3   # failed agent sessions per stage (spec §4.1)`.

`transitions.py`:

```python
def after_agent_error(stage: Stage, error: str, errors: int, max_errors: int) -> Transition:
    """The agent session ended with an error result (max turns, execution error). `errors`
    counts earlier failed sessions of this stage; it is separate from verify/review retries
    (spec §4.1). Park for a human once the budget is spent."""
    if errors >= max_errors:
        return park(ParkReason.AGENT_ERROR,
                    f"The {stage.value} agent did not finish after {errors} retries "
                    f"(agent did not finish: {error or 'error'}).")
    return Transition(stage)
```

In `requeue`, extend the dropped keys: `if k not in ("park_note", "parked_tag_set", "last_denials", "agent_errors")}`.

`stages.py`:

```python
    def _agent_failed(self, item: Item, res: AgentResult, events: list[EventInput],
                      data: dict[str, Any]) -> StepResult | None:
        """An agent error result (max turns, execution error) is a failed attempt of this
        stage, counted per stage (spec §4.1)."""
        if not res.is_error:
            return None
        errors = dict(item.data.get("agent_errors") or {})
        n = int(errors.get(item.stage.value, 0))
        errors[item.stage.value] = n + 1
        t = after_agent_error(item.stage, res.error, n, self._t.limits.max_agent_errors)
        return StepResult(t, res.usage, events=events, data={**data, "agent_errors": errors})

    @staticmethod
    def _agent_ok(item: Item) -> dict[str, Any]:
        """Clears this stage's agent-error count once a session finishes cleanly (§4.1)."""
        errors = dict(item.data.get("agent_errors") or {})
        if errors.pop(item.stage.value, None) is None:
            return {}
        return {"agent_errors": errors or None}
```

In `_plan`, `_implement_changes` and `_review`, directly after `if failed := self._agent_failed(item, res, events, data): return failed`, add `data.update(self._agent_ok(item))`.

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass (`test_agent_error_park_is_not_a_gate_and_requeue_retries_stage` still passes: `after_agent_error(stage, ..., 3, 3)` parks).

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/targets.py src/agent_sdlc/orchestrator/transitions.py src/agent_sdlc/orchestrator/stages.py tests/test_stages.py tests/test_transitions.py tests/test_targets.py
git commit -m "fix: count agent errors per stage, separate from verify retries"
```

---

### Task 9: Tracked-only lint commit and one re-verify

**Files:**
- Modify: `src/agent_sdlc/orchestrator/events.py` (`check_event`), `src/agent_sdlc/orchestrator/stages.py` (`_verify`)
- Test: `tests/test_stages.py`

**Interfaces:**
- Consumes: `Workspaces.commit(..., tracked_only=True)`, `.has_tracked_changes` (Task 2); `_agent_owned`, `_agent_diff_lines` (Task 6).
- Produces: `check_event(r: CommandResult, pass_: int | None = None) -> EventInput` (adds `"pass"` only when given).

- [ ] **Step 1: Write the failing tests**

In `tests/test_stages.py::test_m9_lint_fix_commit_rechecks_diff_limit` change the command to modify a tracked file:
`update={"commands": {"lint": "seq 1 300 >> README.md"}}`.

Append:

```python
def _verifier(tmp_path: Path, target: TargetConfig, origin_repo: Path, lint: str):  # type: ignore[no-untyped-def]
    t = target.model_copy(update={"repo": target.repo.model_copy(
        update={"commands": {"test": "sh check.sh", "lint": lint}})})
    ado = FakeForge(origin=origin_repo)
    ado.add(WI)
    ws = Workspaces(tmp_path / "ws", t)
    ex = StageExecutor(target=t, forge=ado, decider=FakeDecider(), runner=FakeRunner(),
                       workspaces=ws, path_policy=PathPolicy(t.policy.protected_paths),
                       decisions_for=lambda _id: [])
    return ex, ws


def _passes(res) -> list[int]:  # type: ignore[no-untyped-def]
    return [e.payload["pass"] for e in res.events if e.kind == "check" and "pass" in e.payload]


async def test_untracked_check_output_is_not_committed(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, ws = _verifier(tmp_path, target, origin_repo, "echo out > build.txt")
    res = await ex.run(item(Stage.VERIFY))
    wt = ws.worktree_path(5)
    assert res.transition.to is Stage.REVIEW and _passes(res) == [1, 1]
    assert "build.txt" not in git("ls-files", cwd=wt)


async def test_lint_fix_is_committed_then_verified_once_more(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, ws = _verifier(tmp_path, target, origin_repo,
                       "grep -q fixed README.md || echo fixed >> README.md")
    res = await ex.run(item(Stage.VERIFY))
    wt = ws.worktree_path(5)
    assert res.transition.to is Stage.REVIEW and _passes(res) == [1, 1, 2, 2]
    assert "style: apply lint fixes" in git("log", "--format=%s", cwd=wt)


async def test_non_idempotent_fix_is_discarded_after_the_second_pass(  # type: ignore[no-untyped-def]
    tmp_path: Path, target: TargetConfig, origin_repo: Path
) -> None:
    ex, ws = _verifier(tmp_path, target, origin_repo, "echo x >> README.md")
    res = await ex.run(item(Stage.VERIFY))
    wt = ws.worktree_path(5)
    assert _passes(res) == [1, 1, 2, 2]
    assert git("status", "--porcelain", cwd=wt) == ""
    assert git("show", "HEAD:README.md", cwd=wt) == "fixture\nx\n"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stages.py -k "lint or untracked or idempotent" -q`
Expected: FAIL — `_passes` returns `[]` (no `pass` in payloads).

- [ ] **Step 3: Implement**

`events.py`:

```python
def check_event(r: CommandResult, pass_: int | None = None) -> EventInput:
    payload = {"name": r.name, "command": r.command, "exit_code": r.exit_code,
               "duration_s": r.duration_s, "log": r.log}
    if pass_ is not None:
        payload["pass"] = pass_          # verify runs checks twice after lint fixes (§4.2)
    return EventInput("check", payload)
```

`stages.py` — replace `_verify` from `results = self._ws.run_checks(` to the end:

```python
        results = self._checks(item, wt, 1, events)
        if self._ws.commit(wt, "style: apply lint fixes", tracked_only=True):
            if parked := self._after_lint_commit(item, wt, events, inst_data):
                return parked
            results = self._checks(item, wt, 2, events)
            if self._ws.has_tracked_changes(wt):
                log.warning("checks changed tracked files again; discarding them "
                            "(the fixer is not idempotent)")
                self._ws.reset(wt)
        t = after_verify(results, item.attempt, self._t.limits.max_verify_retries)
        return StepResult(t, data={"checks": [_check_dict(r) for r in results], **inst_data},
                          events=events)

    def _checks(self, item: Item, wt: Path, pass_: int,
                events: list[EventInput]) -> list[CommandResult]:
        suffix = "" if pass_ == 1 else f"-p{pass_}"
        results = self._ws.run_checks(
            wt, log_for=lambda name: self._trace_path(item, f"{name}{suffix}", "log"))
        events += [check_event(r, pass_) for r in results]
        for r in results:
            log.info("check %s (pass %s): exit %s (%ss)", r.name, pass_, r.exit_code,
                     r.duration_s)
        return results

    def _after_lint_commit(self, item: Item, wt: Path, events: list[EventInput],
                           inst_data: dict[str, Any]) -> StepResult | None:
        """The checks a lint-fix commit must pass before the re-verify (M9, spec §4.2)."""
        violations = self._pp.violations(self._agent_owned(item, wt, self._ws.changed_files(wt)))
        if violations:
            return StepResult(park(
                ParkReason.POLICY, "Lint fixes touched protected paths: " + ", ".join(violations)),
                events=events, data=inst_data)
        if gate := self._manifest_gate(item, wt, Stage.VERIFY):
            return replace(gate, events=events, data={**inst_data, **gate.data})
        lines, limit = self._agent_diff_lines(item, wt), self._t.policy.max_diff_lines
        if lines > limit:
            return StepResult(park(
                ParkReason.POLICY,
                f"After lint fixes the diff is {lines} lines, over the {limit}-line limit."),
                events=events, data=inst_data)
        return None
```

- [ ] **Step 4: Run to verify pass, then the whole suite**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_sdlc/orchestrator/events.py src/agent_sdlc/orchestrator/stages.py tests/test_stages.py
git commit -m "fix: commit only tracked lint fixes and re-verify once"
```

---

### Task 10: README, spec sync, final checks

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-09-27-review-followups-design.md`
- Copy: spec and this plan to `/Users/brian/Documents/dev-vault/projects/paradigm/agent-sdlc/docs/superpowers/{specs,plans}/`

- [ ] **Step 1: README** — make these edits:
  - "Everyday use" block: replace `uv run agent-sdlc run --once --dry-run-push   # full pipeline, no push, prints PR body` with `uv run agent-sdlc run --once --dry-run        # preview one tick; no DB, worktree or tracker writes`.
  - After the `status` paragraph add: "`limits.max_concurrent_items` items of a target run at the same time (one thread each); `max_concurrent_sessions` still caps agent sessions across all targets. `status` shows one `busy:` line per running item."
  - New subsection "### Dry run": the DB is snapshotted, worktrees are temporary, the tracker is read but never written, traces are kept under `~/.agent-sdlc/dry-run/<timestamp>/traces`; Laya and Claude run for real and those tokens are not counted in the daily caps.
  - "Parks you will see from the guardrails": add `needs_human` "PR branch has diverged and could not be merged" (resolve the conflict on the branch, then remove the tag) and `needs_human` "The implementer made no changes for the feedback" (clarify the request, then remove the tag).
  - After "On a PR, start a comment with `/agent`…": "Commits you push to the PR branch are merged in before the next revision; files you changed are exempt from protected-path, manifest and diff-size checks until the agent edits them."
  - Configuration: mention `limits.max_agent_errors` (default 3), failed agent sessions per stage, separate from `max_verify_retries`.
- [ ] **Step 2: Spec sync** — apply "Deviations from the spec" 1–4 above to the spec text (§1 tick default and busy cleanup; §3.1 `human_blobs` covers every touched path and the diff limit; §4.2 `pass` only on verify checks).
- [ ] **Step 3: Full checks** (Definition of Done order)

Run: `uv run pytest -q && uv run ruff check . && uv run mypy && uv build --out-dir /tmp/agent-sdlc-dist`
Expected: all pass; `Successfully built`.

- [ ] **Step 4: Export and commit**

```bash
cp docs/superpowers/specs/2026-09-27-review-followups-design.md /Users/brian/Documents/dev-vault/projects/paradigm/agent-sdlc/docs/superpowers/specs/
mkdir -p /Users/brian/Documents/dev-vault/projects/paradigm/agent-sdlc/docs/superpowers/plans
cp docs/superpowers/plans/2026-09-27-review-followups.md /Users/brian/Documents/dev-vault/projects/paradigm/agent-sdlc/docs/superpowers/plans/
git add README.md docs/superpowers
git commit -m "docs: README and spec for concurrency, dry run, PR rounds, retries, lint commit"
```
