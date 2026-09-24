from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
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
_GIT_ID = ["-c", "user.name=agent-sdlc", "-c", "user.email=agent-sdlc@localhost"]


class GitError(Exception):
    pass


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
    http.extraheader only)."""
    extra = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
             "GIT_CONFIG_NOSYSTEM": "1"}
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
        self, root: Path, target: TargetConfig, git_auth_header: str | None = None
    ):
        self._root = root / target.name
        self._t = target
        self._auth = git_auth_header
        self._base = self._root / "base"
        self.home = self._root / "home"  # scratch HOME for repo commands and agent sessions
        self._cmd_env = safe_env({**_read_env_template(target.repo.env_template),
                                  "HOME": str(self.home)})

    def _git(
        self,
        *args: str,
        cwd: Path,
        auth: bool = False,
        check: bool = True,
    ) -> str:
        cmd = ["git"]
        if auth and self._auth:
            cmd += ["-c", f"http.extraheader={self._auth}"]
        cmd += list(args)
        r = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=git_env(self.home),
        )
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
        if path.exists():
            return path
        self._ensure_base()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            "worktree",
            "add",
            "-B",
            branch,
            str(path),
            f"origin/{self._t.ado.base_branch}",
            cwd=self._base,
        )
        return path

    def reset(self, wt: Path) -> None:
        self._git("reset", "--hard", "HEAD", cwd=wt)
        self._git("clean", "-fd", cwd=wt)

    def run(self, name: str, command: str, wt: Path, log: Path | None = None) -> CommandResult:
        self.home.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        try:
            # shell=True is deliberate: commands come only from the trusted target YAML,
            # never from agents or work item text.
            r = subprocess.run(
                command,
                shell=True,
                cwd=wt,
                capture_output=True,
                text=True,
                env=self._cmd_env,
                timeout=self._t.repo.command_timeout_s,
            )
            code, out = r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, f"timed out after {self._t.repo.command_timeout_s}s"
        written = _write_log(log, command, out, code) if log is not None else None
        return CommandResult(
            name, command, code, out[-_OUTPUT_TAIL:], round(time.monotonic() - start, 2),
            written,
        )

    def install(self, wt: Path, log: Path | None = None) -> CommandResult:
        return self.run("install", self._t.repo.install, wt, log)

    def run_checks(self, wt: Path,
                   log_for: Callable[[str], Path | None] | None = None) -> list[CommandResult]:
        return [self.run(name, cmd, wt, log_for(name) if log_for else None)
                for name, cmd in self._t.repo.commands.items()]

    def commit(self, wt: Path, message: str) -> bool:
        self._git("add", "-A", cwd=wt)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=wt,
            env=git_env(self.home),
        ).returncode
        if staged == 0:
            return False
        self._git(*_GIT_ID, "commit", "--no-verify", "-m", message, cwd=wt)
        return True

    def _range(self) -> str:
        return f"origin/{self._t.ado.base_branch}...HEAD"

    def changed_files(self, wt: Path) -> list[str]:
        out = self._git("diff", "--name-only", self._range(), cwd=wt)
        return sorted(line for line in out.splitlines() if line)

    def diff_lines(self, wt: Path) -> int:
        total = 0
        for line in self._git("diff", "--numstat", self._range(), cwd=wt).splitlines():
            added, deleted, _ = line.split("\t", 2)
            total += (int(added) if added != "-" else 0) + (
                int(deleted) if deleted != "-" else 0
            )
        return total

    def diff(self, wt: Path, max_chars: int = 60000, paths: list[str] | None = None,
             stat: bool = False) -> str:
        extra = ["--", *paths] if paths else []
        flags = ["--stat"] if stat else []
        return self._git("diff", *flags, self._range(), *extra, cwd=wt)[:max_chars]

    def blob_digest(self, wt: Path, paths: list[str]) -> str:
        """sha256 over (path, blob at HEAD) for `paths`; a path absent at HEAD counts as
        deleted, so removing a manifest changes the digest too."""
        blobs: dict[str, str] = {}
        if paths:
            out = self._git("ls-tree", "-z", "HEAD", "--", *paths, cwd=wt)
            for entry in filter(None, out.split("\0")):
                meta, path = entry.split("\t", 1)
                blobs[path] = meta.split()[2]
        lines = [f"{p}:{blobs.get(p, 'deleted')}" for p in sorted(paths)]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def tracked_files(self, wt: Path) -> list[str]:
        out = self._git("ls-tree", "-r", "-z", "--name-only", "HEAD", cwd=wt)
        return sorted(p for p in out.split("\0") if p)

    def remove(self, item_id: int, branch: str) -> None:
        path = self.worktree_path(item_id)
        if not self._base.exists():
            return
        if path.exists():
            self._git("worktree", "remove", "--force", str(path), cwd=self._base, check=False)
            shutil.rmtree(path, ignore_errors=True)
        self._git("worktree", "prune", cwd=self._base, check=False)
        self._git("branch", "-D", branch, cwd=self._base, check=False)
