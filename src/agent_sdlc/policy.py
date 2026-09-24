from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from pathlib import Path

_OUTSIDE = "path is outside the worktree"


def categorize(reason: str) -> str:
    """Map a denial reason produced by this module or the runner to its category."""
    r = reason.lower()
    if r.startswith("tool ") and "is not permitted" in r:
        return "tool_not_permitted"
    if r.startswith("protected path"):
        return "protected_path"
    if r.startswith(_OUTSIDE):
        return "outside_worktree"
    if r.startswith("command not allowlisted") or r == "empty command":
        return "command_not_allowlisted"
    if r.startswith(("shell operators", "command could not be parsed")):
        return "shell_syntax"
    if r.startswith(("find with", "rg --pre", "tree -o", "git --output")):
        return "side_effect_flag"
    return "policy_error"


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Root-anchored glob: `**/` = zero or more dirs, `**` = anything, `*`/`?` stay in a segment."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    # Case-insensitive (M2): macOS/Windows filesystems treat DOCKERFILE and Dockerfile alike.
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def _relative(path: str, root: Path) -> str | None:
    """Resolve `path` (absolute or relative to root, following symlinks) to a posix path
    relative to root, or None if it escapes root."""
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    resolved = p.resolve(strict=False)
    root_r = root.resolve()
    if resolved != root_r and not resolved.is_relative_to(root_r):
        return None
    return resolved.relative_to(root_r).as_posix()


class PathPolicy:
    def __init__(self, protected: list[str]) -> None:
        self.protected = list(protected)
        self._patterns = [_glob_to_regex(p) for p in protected]

    def is_protected(self, rel: str) -> bool:
        low = rel.lower()
        return low == ".git" or low.startswith(".git/") or any(
            p.match(rel) for p in self._patterns)

    def violations(self, rel_paths: Iterable[str]) -> list[str]:
        return sorted({p for p in rel_paths if self.is_protected(p)})

    def check_write(self, path: str, root: Path) -> str | None:
        rel = _relative(path, root)
        if rel is None:
            return _OUTSIDE
        if self.is_protected(rel):
            return f"protected path: {rel}"
        return None

    def check_read(self, path: str, root: Path) -> str | None:
        return _OUTSIDE if _relative(path, root) is None else None


_SHELL_META = re.compile(r"[;&|<>`$\n\\]")
_READONLY = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "tree", "find"}
_FIND_SIDE_EFFECTS = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint",
                      "-fprintf", "-fls", "-fprint0"}
_GIT_READONLY = {"status", "diff", "log", "show"}
# rg --pre runs an arbitrary preprocessor command; tree -o/-R/--fromfile write files.
_RG_EXEC = ("--pre", "--pre-glob")
_TREE_WRITE_SHORT = set("oR")


def _rg_exec(argv: list[str]) -> bool:
    return any(a in _RG_EXEC or a.startswith(tuple(f"{f}=" for f in _RG_EXEC)) for a in argv)


def _tree_writes(argv: list[str]) -> bool:
    for a in argv[1:]:
        if a == "--fromfile" or a.startswith("--fromfile="):
            return True
        if a.startswith("-") and not a.startswith("--") and _TREE_WRITE_SHORT & set(a[1:]):
            return True
    return False


class CommandPolicy:
    """Allowlist for agent Bash. Target commands may take extra trailing args."""

    def __init__(self, target_commands: Iterable[str]) -> None:
        self._exact = {c.strip() for c in target_commands}
        self._prefixes = [shlex.split(c) for c in self._exact if not _SHELL_META.search(c)]

    def check(self, command: str) -> str | None:
        command = command.strip()
        if not command:
            return "empty command"
        if command in self._exact:
            return None
        if _SHELL_META.search(command):
            return "shell operators, substitutions and redirects are not allowed"
        try:
            argv = shlex.split(command)
        except ValueError:
            return "command could not be parsed"
        for prefix in self._prefixes:
            if argv[: len(prefix)] == prefix:
                return None
        head = argv[0]
        if head == "find" and _FIND_SIDE_EFFECTS & set(argv):
            return "find with side-effect actions is not allowed"
        if head == "rg" and _rg_exec(argv):
            return "rg --pre/--pre-glob is not allowed"
        if head == "tree" and _tree_writes(argv):
            return "tree -o/-R/--fromfile is not allowed"
        if head in _READONLY:
            return None
        if head == "git" and len(argv) > 1 and argv[1] in _GIT_READONLY:
            if any(a.startswith("--output") for a in argv):
                return "git --output is not allowed"
            return None
        return f"command not allowlisted: {head}"
