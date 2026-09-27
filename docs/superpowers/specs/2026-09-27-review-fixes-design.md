# Review fixes: orphaned commands, lost PR ids, grep false parks

Date: 2026-09-27. Scope: three defects found in the 2026-09-27 codebase review. No config or
schema changes.

## R1. Timed-out commands leave orphaned processes

**Problem.** `Workspaces.run` uses `subprocess.run(shell=True, timeout=...)`. On timeout Python
kills the shell only; the command it started (node, jest, pytest) is reparented and keeps
running. Every verify retry of a hanging test adds another one.

**Change.** Start each command in its own session (`start_new_session=True`) and, on timeout,
`SIGKILL` the whole process group, then collect output. After a command finishes normally, also
kill its process group, so background children it left behind do not outlive it. The result stays
`exit_code=124` with "timed out after Ns" in the output.

**Test.** A command that starts a background grandchild and outlives the timeout: after `run`
returns, the grandchild is gone.

## R2. PR id lost when the plan comment fails

**Problem.** `StageExecutor._pr_open` creates the PR, then comments the plan on the work item. If
the comment raises, the step fails, `pr_id` is never stored, and the retry calls `create_pr`
again, which the forge rejects (a PR for the branch exists). The item parks as INFRA and the PR
is orphaned.

**Change.** Once `create_pr` returns, the step must return the PR id. A failure of the plan
comment (`httpx.HTTPError`, `ForgeError`, `OSError`) is logged and recorded as a
`plan_comment_failed` event; the item still moves to `awaiting_human` with its `pr_id`. The
comment is not retried (the PR body already carries the plan).

**Test.** A forge whose `comment_item` raises `ForgeError`: `_pr_open` returns
`awaiting_human` with the new PR id and a `plan_comment_failed` event.

## R3. Grep patterns that look like absolute paths stop the session

**Problem.** `_bash_path_violation` checks every Bash argument as a path. `grep -rn "/api/users"
src` resolves `/api/users` outside the worktree, which is the escalating `outside_worktree`
category: the session stops and the item parks as POLICY.

**Change.** For read-only commands only (the `ls/cat/head/tail/wc/grep/rg/pwd/tree/find` set and
`git status/diff/log/show`), an argument outside the worktree is allowed when **all** hold:

- it is absolute (starts with `/`); relative escapes such as `../x` stay denied;
- nothing exists at that path (`os.path.lexists` is false), so the command cannot read it;
- it contains only plain characters (`A-Z a-z 0-9 _ . / : @ % + , -`), so the shell cannot
  expand it into a path that does exist (globs, braces, `~` and zsh qualifiers are excluded).

Target commands (install/verify, which may write) keep the strict check. Existing denials
(`cat /etc/hosts`, `cat ~/.ssh/id_rsa`, `../..`, symlink escapes, `--config=/etc/x`) are
unchanged.

**Tests.** Allowed: `grep -rn "/api/users" src`, `rg "/v1/" .`, `git log --grep=/api/x`. Still
denied: `cat /etc/hosts`, `grep x /etc/*`, `cat /etc/{hosts,x}`, `grep x /etc/hosts(N)`, and an
outside path for a target command (`npm run lint /nonexistent-x`).
