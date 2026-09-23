from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from laya_sdlc.targets import TargetConfig
from laya_sdlc.types import CommandResult

_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
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
_GIT_ID = ["-c", "user.name=laya-sdlc", "-c", "user.email=laya-sdlc@localhost"]


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


class Workspaces:
    def __init__(
        self, root: Path, target: TargetConfig, git_auth_header: str | None = None
    ):
        self._root = root / target.name
        self._t = target
        self._auth = git_auth_header
        self._base = self._root / "base"
        self._cmd_env = safe_env(_read_env_template(target.repo.env_template))

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
            env=safe_env({"GIT_TERMINAL_PROMPT": "0"}),
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

    def run(self, name: str, command: str, wt: Path) -> CommandResult:
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
        return CommandResult(
            name, command, code, out[-_OUTPUT_TAIL:], round(time.monotonic() - start, 2)
        )

    def install(self, wt: Path) -> CommandResult:
        return self.run("install", self._t.repo.install, wt)

    def run_checks(self, wt: Path) -> list[CommandResult]:
        return [self.run(name, cmd, wt) for name, cmd in self._t.repo.commands.items()]

    def commit(self, wt: Path, message: str) -> bool:
        self._git("add", "-A", cwd=wt)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=wt,
            env=safe_env(),
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

    def diff(self, wt: Path, max_chars: int = 60000) -> str:
        return self._git("diff", self._range(), cwd=wt)[:max_chars]

    def remove(self, item_id: int, branch: str) -> None:
        path = self.worktree_path(item_id)
        if not self._base.exists():
            return
        if path.exists():
            self._git("worktree", "remove", "--force", str(path), cwd=self._base, check=False)
            shutil.rmtree(path, ignore_errors=True)
        self._git("worktree", "prune", cwd=self._base, check=False)
        self._git("branch", "-D", branch, cwd=self._base, check=False)
