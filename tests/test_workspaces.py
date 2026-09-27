import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from agent_sdlc.targets import TargetConfig
from agent_sdlc.workspaces import GitError, MergeConflict, Workspaces, git_env, slugify
from tests.conftest import git


@pytest.fixture
def ws(tmp_path: Path, target: TargetConfig) -> Workspaces:
    return Workspaces(tmp_path / "workspaces", target)


def test_slugify() -> None:
    result = slugify("Fix: Login button (Teams) doesn't work!")
    assert result == "fix-login-button-teams-doesn-t-work"
    assert len(slugify("x" * 100)) == 40


def test_create_is_idempotent_and_on_branch(ws: Workspaces) -> None:
    wt = ws.create(7, "agent/7-fix")
    assert (wt / "check.sh").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).strip() == "agent/7-fix"
    assert ws.create(7, "agent/7-fix") == wt


def test_commit_and_diff(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    assert ws.commit(wt, "chore: nothing") is False
    (wt / "a.txt").write_text("one\ntwo\n")
    assert ws.commit(wt, "feat: add a") is True
    assert ws.changed_files(wt) == ["a.txt"]
    assert ws.diff_lines(wt) == 2
    assert "+one" in ws.diff(wt)


def test_run_checks_pass_and_fail(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    [ok] = ws.run_checks(wt)
    assert ok.ok and "ok" in ok.output
    (wt / "broken.txt").write_text("x")
    [bad] = ws.run_checks(wt)
    assert not bad.ok and "broken.txt present" in bad.output


def test_command_timeout(tmp_path: Path, target: TargetConfig) -> None:
    slow = target.model_copy(
        update={
            "repo": target.repo.model_copy(
                update={"commands": {"test": "sleep 5"}, "command_timeout_s": 1}
            )
        }
    )
    ws = Workspaces(tmp_path / "w", slow)
    [r] = ws.run_checks(ws.create(1, "agent/1-a"))
    assert r.exit_code == 124 and "timed out" in r.output


def _gone(pid: int, wait_s: float = 3.0) -> bool:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _with_command(target: TargetConfig, command: str, timeout_s: int) -> TargetConfig:
    return target.model_copy(update={"repo": target.repo.model_copy(
        update={"commands": {"test": command}, "command_timeout_s": timeout_s})})


def test_r1_timeout_kills_the_whole_process_group(tmp_path: Path,
                                                   target: TargetConfig) -> None:
    ws = Workspaces(tmp_path / "w", _with_command(
        target, "sleep 30 & echo $! > child.pid; sleep 30", timeout_s=1))
    wt = ws.create(1, "agent/1-a")
    [r] = ws.run_checks(wt)
    assert r.exit_code == 124
    assert _gone(int((wt / "child.pid").read_text()))


def test_r1_finished_command_leaves_no_background_children(tmp_path: Path,
                                                           target: TargetConfig) -> None:
    ws = Workspaces(tmp_path / "w", _with_command(
        target, "sleep 30 >/dev/null 2>&1 & echo $! > child.pid", timeout_s=10))
    wt = ws.create(1, "agent/1-a")
    [r] = ws.run_checks(wt)
    assert r.ok
    assert _gone(int((wt / "child.pid").read_text()))


def test_command_env_excludes_secrets(ws: Workspaces, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_SDLC_ADO_PAT", "super-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok-7f3a9c")
    wt = ws.create(1, "agent/1-a")
    r = ws.run("env", "env", wt)
    assert "super-secret" not in r.output and "oauth-tok-7f3a9c" not in r.output
    assert "PATH=" in r.output


def test_reset_discards_uncommitted(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    (wt / "keep.txt").write_text("k")
    ws.commit(wt, "feat: keep")
    (wt / "junk.txt").write_text("j")
    (wt / "README.md").write_text("changed")
    ws.reset(wt)
    assert not (wt / "junk.txt").exists()
    assert (wt / "README.md").read_text() == "fixture\n"
    assert (wt / "keep.txt").exists()


def test_remove(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    ws.remove(1, "agent/1-a")
    assert not wt.exists()
    ws.remove(1, "agent/1-a")  # idempotent


# --- final review fix wave ---------------------------------------------------------------


def test_c2_commands_get_scratch_home_and_no_agent_socket(
    ws: Workspaces, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    wt = ws.create(1, "agent/1-a")
    r = ws.run("env", "env", wt)
    home = tmp_path / "workspaces" / "fixture" / "home"
    assert f"HOME={home}\n" in r.output and home.is_dir()
    assert "SSH_AUTH_SOCK" not in r.output


def test_c2_git_env_ignores_user_git_config(tmp_path: Path) -> None:
    env = git_env(tmp_path / "home")
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["HOME"] == str(tmp_path / "home")


def test_git_env_forces_c_locale_regardless_of_the_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 6: stderr matching (e.g. "couldn't find remote ref") must not depend on the
    operator's locale."""
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    assert git_env()["LC_ALL"] == "C"


def test_c2_user_insteadof_rewrite_is_not_used(
    tmp_path: Path, target: TargetConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(
        f'[url "{tmp_path}/nowhere.git"]\n\tinsteadOf = {target.clone_url}\n')
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(fake_home / ".gitconfig"))
    ws = Workspaces(tmp_path / "w2", target)
    assert (ws.create(1, "agent/1-a") / "README.md").exists()


def test_run_writes_full_log(ws: Workspaces, tmp_path: Path) -> None:
    wt = ws.create(1, "agent/1-a")
    log = tmp_path / "logs" / "big.log"
    r = ws.run("big", "python3 -c \"print('x' * 9000)\"", wt, log=log)
    assert len(r.output) == 8000 and r.log == str(log)
    assert log.read_text().count("x") >= 9000
    assert oct(log.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_run_log_unwritable_dir(ws: Workspaces, tmp_path: Path) -> None:
    wt = ws.create(1, "agent/1-a")
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    r = ws.run("test", "sh check.sh", wt, log=locked / "sub" / "t.log")
    assert r.ok and r.log is None


def test_run_checks_log_for(ws: Workspaces, tmp_path: Path) -> None:
    wt = ws.create(1, "agent/1-a")
    [r] = ws.run_checks(wt, log_for=lambda name: tmp_path / f"{name}.log")
    assert r.log == str(tmp_path / "test.log") and "ok" in (tmp_path / "test.log").read_text()


def test_blob_digest_tracks_content_and_deletion(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    base = ws.blob_digest(wt, ["README.md"])
    assert base == ws.blob_digest(wt, ["README.md"])
    (wt / "README.md").write_text("changed\n")
    ws.commit(wt, "c")
    changed = ws.blob_digest(wt, ["README.md"])
    (wt / "README.md").unlink()
    ws.commit(wt, "d")
    deleted = ws.blob_digest(wt, ["README.md"])
    assert len({base, changed, deleted}) == 3
    assert "README.md" in ws.changed_files(wt)  # a deletion still shows up for the gate
    assert ws.blob_digest(wt, []) == ws.blob_digest(wt, [])


def test_tracked_files_and_diff_paths(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    assert ws.tracked_files(wt) == ["README.md", "check.sh"]
    (wt / "a.txt").write_text("a\n")
    (wt / "b.txt").write_text("b\n")
    ws.commit(wt, "ab")
    d = ws.diff(wt, paths=["a.txt"])
    assert "a.txt" in d and "b.txt" not in d


def test_diff_stat_for_paths(ws: Workspaces) -> None:
    wt = ws.create(1, "agent/1-a")
    (wt / "a.txt").write_text("a\n")
    (wt / "b.txt").write_text("b\n")
    ws.commit(wt, "ab")
    d = ws.diff(wt, paths=["a.txt"], stat=True)
    assert "a.txt" in d and "1 file changed" in d and "b.txt" not in d


def test_install_runs_each_command_and_stops_at_first_failure(
    tmp_path: Path, target: TargetConfig
) -> None:
    multi = target.model_copy(update={"repo": target.repo.model_copy(
        update={"install": ["echo one", "sh -c 'exit 3'", "echo never"]})})
    ws = Workspaces(tmp_path / "w", multi)
    wt = ws.create(1, "agent/1-a")
    r = ws.install(wt, log=tmp_path / "install.log")
    assert r.exit_code == 3 and "one" in r.output and "never" not in r.output
    assert r.command == "echo one ; sh -c 'exit 3' ; echo never"
    assert "$ echo one" in (tmp_path / "install.log").read_text()


def test_git_auth_callable_is_called_for_every_authenticated_git_call(
    tmp_path: Path, target: TargetConfig
) -> None:
    calls: list[int] = []

    def header() -> str:
        calls.append(1)
        return "X-Test: 1"

    ws = Workspaces(tmp_path / "w", target, git_auth=header)
    ws.create(1, "agent/1-a")   # clone (auth)
    ws.create(2, "agent/2-b")   # fetch (auth)
    assert len(calls) == 2


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


def test_human_blobs_returns_only_the_humans_paths_at_fetch_head(
    ws: Workspaces, tmp_path: Path, origin_repo: Path
) -> None:
    wt = _pushed(ws, origin_repo)
    _human_push(tmp_path, origin_repo, {"h.txt": "human\n"})
    ws.incorporate_remote(wt, BR)
    human = ws.human_blobs(wt)
    assert human.keys() == {"h.txt"}
    assert human["h.txt"] == ws.blobs(wt, ["h.txt"])["h.txt"]


def test_human_blobs_empty_before_anything_is_fetched(ws: Workspaces) -> None:
    wt = ws.create(1, BR)                      # never fetched the PR branch
    assert ws.human_blobs(wt) == {}


def test_incorporate_refreshes_base_so_upstream_commits_are_not_the_agents(
    ws: Workspaces, tmp_path: Path, origin_repo: Path
) -> None:
    """Finding 2: origin/dev is only refreshed when a worktree is first created. If a human
    merges dev into the PR branch ("Update branch") after another (agent-authored) PR landed on
    dev, incorporate_remote must refresh origin/dev first, or that upstream commit is counted as
    this agent's own change."""
    wt = _pushed(ws, origin_repo)                       # worktree created; origin/dev now fixed

    other = tmp_path / "other"
    git("clone", "-q", str(origin_repo), str(other), cwd=tmp_path)
    git("checkout", "-q", "dev", cwd=other)
    (other / "upstream.txt").write_text("upstream\n")
    git("add", "-A", cwd=other)
    git("-c", "user.name=agent-sdlc", "-c", "user.email=agent-sdlc@localhost",
        "commit", "-qm", "feat: upstream", cwd=other)
    git("push", "-q", "origin", "dev", cwd=other)        # another agent's PR merged to dev

    human = tmp_path / "human2"
    git("clone", "-q", str(origin_repo), str(human), cwd=tmp_path)
    git("fetch", "-q", "origin", cwd=human)
    git("checkout", "-q", "-B", BR, f"origin/{BR}", cwd=human)
    git("-c", "user.name=h", "-c", "user.email=h@h", "merge", "-q", "--no-edit", "origin/dev",
        cwd=human)
    git("push", "-q", "origin", f"HEAD:refs/heads/{BR}", cwd=human)  # human's "Update branch"

    ws.incorporate_remote(wt, BR)
    assert (wt / "upstream.txt").exists()                # merged in
    assert "upstream.txt" not in ws.changed_files(wt)     # but not counted as the agent's change
