from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from agent_sdlc.adapters.errors import ForgeError, redact
from agent_sdlc.adapters.github_auth import HEADERS
from agent_sdlc.targets import GitHubForgeConfig, IntakeConfig
from agent_sdlc.types import PrComment, WorkItem
from agent_sdlc.workspaces import git_env

_PER_PAGE = 100
_MAX_PAGES = 50
_AC_HEADING = re.compile(r"^(#{1,6})[ \t]*acceptance criteria[ \t]*:?[ \t]*$",
                         re.IGNORECASE | re.MULTILINE)


class AppAuth(Protocol):
    def token(self) -> str: ...
    def invalidate(self) -> None: ...
    def bot_login(self) -> str: ...


def split_acceptance(body: str) -> tuple[str, str]:
    """(description without the section, acceptance criteria) from an issue body."""
    m = _AC_HEADING.search(body)
    if m is None:
        return body.strip(), ""
    level = len(m.group(1))
    rest = body[m.end():]
    nxt = re.search(rf"^#{{1,{level}}}[ \t]", rest, re.MULTILINE)
    criteria = rest[: nxt.start()] if nxt else rest
    after = rest[nxt.start():] if nxt else ""
    return (body[: m.start()] + after).strip(), criteria.strip()


class GitHubForge:
    kind = "github"
    label_word = "label"

    def __init__(self, cfg: GitHubForgeConfig, auth: AppAuth, *, intake: IntakeConfig,
                 base_branch: str, branch_prefix: str, http: httpx.Client,
                 push_url: str | None = None) -> None:
        self._cfg = cfg
        self._auth = auth
        self._intake = intake
        self._base_branch = base_branch
        self._branch_prefix = branch_prefix
        self._http = http
        self._push_url = push_url or cfg.clone_url
        self._repo = f"/repos/{cfg.owner}/{cfg.repo}"

    # plumbing --------------------------------------------------------------
    def _req(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             json: Any = None, ok: tuple[int, ...] = ()) -> Any:
        r = self._send(method, path, params, json)
        if r.status_code == 401:   # expired or revoked token: refresh once (spec §3.3)
            self._auth.invalidate()
            r = self._send(method, path, params, json)
        if r.status_code in ok:
            return None
        if r.status_code == 429 or (
                r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"):
            reset_time = r.headers.get("x-ratelimit-reset")
            if not reset_time and r.status_code == 429:
                # Secondary rate limit: Retry-After is seconds to wait
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    reset_time = f"retry after {retry_after} s"
            if not reset_time:
                reset_time = "unknown"
            raise ForgeError(f"GitHub rate limit on {method} {path}; resets at {reset_time}")
        if r.status_code >= 400:
            raise ForgeError(f"GitHub {method} {path} failed: HTTP {r.status_code}")
        return r.json() if r.content else None

    def _send(self, method: str, path: str, params: dict[str, Any] | None,
              json: Any) -> httpx.Response:
        headers = {**HEADERS, "Authorization": f"Bearer {self._auth.token()}"}
        return self._http.request(method, path, params=params, json=json, headers=headers)

    def _pages(self, path: str, params: dict[str, Any] | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._req("GET", path,
                              params={**(params or {}), "per_page": _PER_PAGE, "page": page})
            out += batch
            if len(batch) < _PER_PAGE or (limit is not None and len(out) >= limit):
                break
        return out

    def _check_branch(self, branch: str) -> None:
        if not branch.startswith(self._branch_prefix):
            raise ForgeError(f"refusing ref outside {self._branch_prefix}*: {branch}")

    def git_auth_header(self) -> str:
        return self._header_for(self._auth.token())

    @staticmethod
    def _header_for(token: str) -> str:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return f"Authorization: Basic {cred}"

    def pr_ref(self, pr_id: int) -> str:
        return f"#{pr_id}"

    def item_ref(self, item_id: int) -> str:
        return f"#{item_id}"

    # issues ----------------------------------------------------------------
    @staticmethod
    def _to_item(i: dict[str, Any]) -> WorkItem:
        description, criteria = split_acceptance(str(i.get("body") or ""))
        labels = tuple(str(x["name"] if isinstance(x, dict) else x) for x in i.get("labels", []))
        return WorkItem(
            id=int(i["number"]), title=str(i.get("title", "")), description=description,
            acceptance_criteria=criteria,
            work_item_type="Bug" if "bug" in {x.lower() for x in labels} else "Issue",
            tags=labels, url=str(i.get("html_url", "")))

    def list_intake(self) -> list[WorkItem]:
        raw = self._pages(f"{self._repo}/issues", {"state": "open", "labels": self._intake.label,
                                                  "sort": "created", "direction": "asc"})
        return [self._to_item(i) for i in raw if "pull_request" not in i]

    def list_closed(self, limit: int) -> list[WorkItem]:
        out: list[WorkItem] = []
        for page in range(1, _MAX_PAGES + 1):
            batch = self._req("GET", f"{self._repo}/issues",
                              params={"state": "closed", "labels": self._intake.label,
                                      "sort": "updated", "direction": "desc",
                                      "per_page": _PER_PAGE, "page": page})
            for i in batch:
                if "pull_request" not in i:
                    out.append(self._to_item(i))
                    if len(out) >= limit:
                        return out
            if len(batch) < _PER_PAGE:
                break
        return out

    def get_item(self, id: int) -> WorkItem:
        i = self._req("GET", f"{self._repo}/issues/{id}")
        if "pull_request" in i:
            raise ForgeError(f"#{id} is a pull request, not an issue")
        return self._to_item(i)

    def comment_item(self, id: int, html: str) -> None:
        self._req("POST", f"{self._repo}/issues/{id}/comments", json={"body": html})

    def has_label(self, id: int, label: str) -> bool:
        return label in self.get_item(id).tags

    def set_label(self, id: int, label: str, present: bool) -> None:
        if present:
            self._req("POST", f"{self._repo}/issues/{id}/labels", json={"labels": [label]})
        else:
            self._req("DELETE", f"{self._repo}/issues/{id}/labels/{quote(label, safe='')}",
                      ok=(404,))

    # git & pull requests ---------------------------------------------------
    def push_branch(self, worktree: Path, branch: str) -> None:
        self._check_branch(branch)
        # M-8: fetch the token exactly once so the header we push with and the string we
        # redact on failure can never diverge (a second token() call could return a new one).
        token = self._auth.token()
        r = subprocess.run(
            ["git", "-c", f"http.extraheader={self._header_for(token)}", "push", self._push_url,
             f"HEAD:refs/heads/{branch}"],
            cwd=worktree, capture_output=True, text=True, env=git_env())
        if r.returncode != 0:
            raise ForgeError(f"push failed: {redact(r.stderr.strip(), [token])}")

    @staticmethod
    def _closes(body: str, item_id: int) -> str:
        return f"{body}\n\nCloses #{item_id}"

    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int:
        self._check_branch(branch)
        res = self._req("POST", f"{self._repo}/pulls", json={
            "title": title, "head": branch, "base": self._base_branch,
            "body": self._closes(body, item_id)})
        return int(res["number"])

    def update_pr(self, pr_id: int, body: str, item_id: int) -> None:
        self._req("PATCH", f"{self._repo}/pulls/{pr_id}",
                  json={"body": self._closes(body, item_id)})

    def pr_status(self, pr_id: int) -> str:
        pr = self._req("GET", f"{self._repo}/pulls/{pr_id}")
        if pr.get("merged"):
            return "completed"
        return "abandoned" if pr.get("state") == "closed" else "active"

    def pr_comments(self, pr_id: int) -> list[PrComment]:
        me = self._auth.bot_login()
        out: list[PrComment] = []
        for c in self._pages(f"{self._repo}/issues/{pr_id}/comments"):
            author, body = str(c["user"]["login"]), str(c.get("body") or "")
            if author != me and body.strip():
                out.append(PrComment(0, int(c["id"]), author, body, kind="conversation"))
        for c in self._pages(f"{self._repo}/pulls/{pr_id}/comments"):
            author, body = str(c["user"]["login"]), str(c.get("body") or "")
            if author != me and body.strip():
                out.append(PrComment(int(c.get("in_reply_to_id") or c["id"]), int(c["id"]),
                                     author, body, kind="review_comment"))
        for r in self._pages(f"{self._repo}/pulls/{pr_id}/reviews"):
            author, body, state = str(r["user"]["login"]), str(r.get("body") or ""), r["state"]
            changes = state == "CHANGES_REQUESTED"
            if author == me or state == "PENDING" or not (body.strip() or changes):
                continue
            out.append(PrComment(0, int(r["id"]), author,
                                 body.strip() or "(changes requested with no summary)",
                                 kind="review", changes_requested=changes))
        return out

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        if comment.kind == "review_comment":
            self._req("POST",
                      f"{self._repo}/pulls/{pr_id}/comments/{comment.thread_id}/replies",
                      json={"body": text})
            return
        quoted = "\n".join(f"> {line}" for line in comment.content[:300].splitlines())
        self.comment_pr(pr_id, f"{quoted}\n\n@{comment.author} {text}")

    def comment_pr(self, pr_id: int, text: str) -> None:
        self._req("POST", f"{self._repo}/issues/{pr_id}/comments", json={"body": text})

    def delete_branch(self, branch: str) -> None:
        self._check_branch(branch)
        self._req("DELETE", f"{self._repo}/git/refs/heads/{branch}", ok=(404, 422))
