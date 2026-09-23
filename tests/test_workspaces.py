from pathlib import Path

import pytest

from laya_sdlc.targets import TargetConfig
from laya_sdlc.workspaces import Workspaces, git_env, slugify
from tests.conftest import git


@pytest.fixture
def ws(tmp_path: Path, target: TargetConfig) -> Workspaces:
    return Workspaces(tmp_path / "workspaces", target)


def test_slugify() -> None:
    result = slugify("Fix: Login button (Teams) doesn't work!")
    assert result == "fix-login-button-teams-doesn-t-work"
    assert len(slugify("x" * 100)) == 40


def test_create_is_idempotent_and_on_branch(ws: Workspaces) -> None:
    wt = ws.create(7, "laya/7-fix")
    assert (wt / "check.sh").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).strip() == "laya/7-fix"
    assert ws.create(7, "laya/7-fix") == wt


def test_commit_and_diff(ws: Workspaces) -> None:
    wt = ws.create(1, "laya/1-a")
    assert ws.commit(wt, "chore: nothing") is False
    (wt / "a.txt").write_text("one\ntwo\n")
    assert ws.commit(wt, "feat: add a") is True
    assert ws.changed_files(wt) == ["a.txt"]
    assert ws.diff_lines(wt) == 2
    assert "+one" in ws.diff(wt)


def test_run_checks_pass_and_fail(ws: Workspaces) -> None:
    wt = ws.create(1, "laya/1-a")
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
    [r] = ws.run_checks(ws.create(1, "laya/1-a"))
    assert r.exit_code == 124 and "timed out" in r.output


def test_command_env_excludes_secrets(ws: Workspaces, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYA_SDLC_ADO_PAT", "super-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok-7f3a9c")
    wt = ws.create(1, "laya/1-a")
    r = ws.run("env", "env", wt)
    assert "super-secret" not in r.output and "oauth-tok-7f3a9c" not in r.output
    assert "PATH=" in r.output


def test_reset_discards_uncommitted(ws: Workspaces) -> None:
    wt = ws.create(1, "laya/1-a")
    (wt / "keep.txt").write_text("k")
    ws.commit(wt, "feat: keep")
    (wt / "junk.txt").write_text("j")
    (wt / "README.md").write_text("changed")
    ws.reset(wt)
    assert not (wt / "junk.txt").exists()
    assert (wt / "README.md").read_text() == "fixture\n"
    assert (wt / "keep.txt").exists()


def test_remove(ws: Workspaces) -> None:
    wt = ws.create(1, "laya/1-a")
    ws.remove(1, "laya/1-a")
    assert not wt.exists()
    ws.remove(1, "laya/1-a")  # idempotent


# --- final review fix wave ---------------------------------------------------------------


def test_c2_commands_get_scratch_home_and_no_agent_socket(
    ws: Workspaces, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    wt = ws.create(1, "laya/1-a")
    r = ws.run("env", "env", wt)
    home = tmp_path / "workspaces" / "fixture" / "home"
    assert f"HOME={home}\n" in r.output and home.is_dir()
    assert "SSH_AUTH_SOCK" not in r.output


def test_c2_git_env_ignores_user_git_config(tmp_path: Path) -> None:
    env = git_env(tmp_path / "home")
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["HOME"] == str(tmp_path / "home")


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
    assert (ws.create(1, "laya/1-a") / "README.md").exists()
