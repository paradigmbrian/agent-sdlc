from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agent_sdlc.fsutil import ensure_private_dir
from agent_sdlc.targets import TargetConfig
from agent_sdlc.types import CommandResult

log = logging.getLogger(__name__)

# HOME is deliberately absent: repo commands and git get a scratch HOME (C2).
_SAFE_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "SHELL",
    "USER",
    "NVM_DIR",
    "NVM_BIN",
    "TERM",
)
_OUTPUT_TAIL = 8000
_REAP_S = 10  # after killing a timed-out group, wait this long for its pipes to close
_AGENT_EMAIL = "agent-sdlc@localhost"
_GIT_ID = ["-c", "user.name=agent-sdlc", "-c", f"user.email={_AGENT_EMAIL}"]


class GitError(Exception):
    pass


class MergeConflict(Exception):
    """Human commits on the PR branch conflict with the agent's (spec §3.1)."""

    def __init__(self, files: list[str]) -> None:
        super().__init__("merge conflict: " + (", ".join(files) or "(no files reported)"))
        self.files = files


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-")


def safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlisted environment for anything that runs repo code. Never includes secrets."""
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env["CI"] = "1"
    env.update(extra or {})
    return env


def git_env(home: Path | None = None) -> dict[str, str]:
    """Environment for orchestrator git plumbing: never reads the user's or system git config,
    so credential helpers and url.insteadOf rewrites are not used (auth is the per-command
    http.extraheader only). LC_ALL=C wins over safe_env's LANG/LC_ALL passthrough, so matching
    git's stderr (e.g. "couldn't find remote ref") never depends on the operator's locale."""
    extra = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    if home is not None:
        extra["HOME"] = str(home)
    return safe_env(extra)


def _read_env_template(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _kill_group(pgid: int) -> None:
    """SIGKILL every process in the group a command's session started (R1)."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _write_log(path: Path, command: str, output: str, code: int) -> str | None:
    try:
        ensure_private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"$ {command}\n{output}\n[exit {code}]\n")
        return str(path)
    except OSError as e:
        log.warning("could not write command log %s: %s", path, e)
        return None


class Workspaces:
    def __init__(
        self, root: Path, target: TargetConfig, git_auth: Callable[[], str] | None = None
    ):
        self._root = root / target.name
        self._t = target
        self._auth = git_auth
        self._base = self._root / "base"
        self.home = self._root / "home"  # scratch HOME for repo commands and agent sessions
        self._cmd_env = safe_env({**_read_env_template(target.repo.env_template),
                                  "HOME": str(self.home)})
        self._base_lock = threading.Lock()  # guards the shared base .git (spec §1)

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

    def _ensure_base(self) -> None:
        if (self._base / ".git").exists() or (self._base / "HEAD").exists():
            self._git("fetch", "--prune", "origin", cwd=self._base, auth=True)
            return
        self._root.mkdir(parents=True, exist_ok=True)
        self._git(
            "clone",
            "--no-checkout",
            self._t.clone_url,
            str(self._base),
            cwd=self._root,
            auth=True,
        )

    def worktree_path(self, item_id: int) -> Path:
        return self._root / "wt" / str(item_id)

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

    def reset(self, wt: Path) -> None:
        self._git("reset", "--hard", "HEAD", cwd=wt)
        self._git("clean", "-fd", cwd=wt)

    def run(self, name: str, command: str, wt: Path, log: Path | None = None) -> CommandResult:
        self.home.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        # shell=True is deliberate: commands come only from the trusted target YAML,
        # never from agents or work item text. Its own session lets us kill everything it
        # started, not just the shell (R1).
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._cmd_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self._t.repo.command_timeout_s)
            code, out = proc.returncode, stdout + stderr
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid)
            try:
                proc.communicate(timeout=_REAP_S)
            except subprocess.TimeoutExpired:  # a child escaped the group and holds the pipes
                proc.kill()
                proc.wait()
            code, out = 124, f"timed out after {self._t.repo.command_timeout_s}s"
        _kill_group(proc.pid)  # background children the command left behind
        written = _write_log(log, command, out, code) if log is not None else None
        return CommandResult(
            name, command, code, out[-_OUTPUT_TAIL:], round(time.monotonic() - start, 2),
            written,
        )

    def install(self, wt: Path, log: Path | None = None) -> CommandResult:
        """Run each install command in order, stopping at the first failure (spec §2.2)."""
        start = time.monotonic()
        parts: list[str] = []
        code = 0
        for cmd in self._t.repo.install:
            r = self.run("install", cmd, wt)
            parts.append(f"$ {cmd}\n{r.output}")
            code = r.exit_code
            if not r.ok:
                break
        command = " ; ".join(self._t.repo.install)
        out = "\n".join(parts)
        written = _write_log(log, command, out, code) if log is not None else None
        return CommandResult("install", command, code, out[-_OUTPUT_TAIL:],
                             round(time.monotonic() - start, 2), written)

    def run_checks(self, wt: Path,
                   log_for: Callable[[str], Path | None] | None = None) -> list[CommandResult]:
        return [self.run(name, cmd, wt, log_for(name) if log_for else None)
                for name, cmd in self._t.repo.commands.items()]

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

    def _range(self) -> str:
        return f"origin/{self._t.repo.base_branch}...HEAD"

    def changed_files(self, wt: Path) -> list[str]:
        out = self._git("diff", "--name-only", self._range(), cwd=wt)
        return sorted(line for line in out.splitlines() if line)

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

    def diff(self, wt: Path, max_chars: int = 60000, paths: list[str] | None = None,
             stat: bool = False) -> str:
        extra = ["--", *paths] if paths else []
        flags = ["--stat"] if stat else []
        return self._git("diff", *flags, self._range(), *extra, cwd=wt)[:max_chars]

    def blobs(self, wt: Path, paths: list[str], rev: str = "HEAD") -> dict[str, str]:
        """Blob id at `rev` for each path; "deleted" when the path is absent."""
        found: dict[str, str] = {}
        if paths:
            out = self._git("ls-tree", "-z", rev, "--", *paths, cwd=wt)
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

    def tracked_files(self, wt: Path) -> list[str]:
        out = self._git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=wt)
        return sorted(p for p in out.split("\0") if p)

    def _is_ancestor(self, a: str, b: str, wt: Path) -> bool:
        return self._run_git("merge-base", "--is-ancestor", a, b, cwd=wt).returncode == 0

    def _names(self, *args: str, wt: Path) -> list[str]:
        return sorted(filter(None, self._git(*args, cwd=wt).splitlines()))

    def incorporate_remote(self, wt: Path, branch: str) -> list[str]:
        """Bring commits a human pushed to the PR branch into the worktree (spec §3.1).
        Returns the paths they changed. Raises MergeConflict (after aborting the merge) when
        the histories conflict, and GitError when the remote cannot be read."""
        # Held for the fetch too: it opportunistically updates the shared remote-tracking ref
        # refs/remotes/origin/<branch>, which races _ensure_base's `fetch --prune` (spec §1).
        with self._base_lock:
            # Refresh origin/<base> first: during PR rounds it is otherwise only refreshed when
            # a worktree is first created, so after a human merges base into the PR branch
            # ("Update branch"), upstream commits by other (agent-authored) PRs would be counted
            # as this agent's own changes against a stale origin/<base> (spec §3.1 fix).
            self._git("fetch", "--prune", "origin", cwd=self._base, auth=True)
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

    def human_blobs(self, wt: Path) -> dict[str, str]:
        """Paths committed by someone other than the agent identity on the PR branch, since it
        diverged from base, with their blob at FETCH_HEAD (spec §3.1); "deleted" when a path is
        absent there. Derived from the worktree's own history rather than stored state, so it
        is recomputed the same way every round. {} when nothing has been fetched yet (no
        incorporate_remote call, or the remote branch doesn't exist)."""
        if self._run_git("rev-parse", "--verify", "FETCH_HEAD", cwd=wt).returncode != 0:
            return {}
        out = self._git("log", "--no-merges", "--format=%x00%ae", "--name-only",
                        f"origin/{self._t.repo.base_branch}..FETCH_HEAD", cwd=wt)
        paths: set[str] = set()
        for entry in filter(None, out.split("\x00")):
            email, _, files = entry.partition("\n\n")
            if email != _AGENT_EMAIL:
                paths.update(filter(None, files.splitlines()))
        return self.blobs(wt, sorted(paths), rev="FETCH_HEAD")

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
