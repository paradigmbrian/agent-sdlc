from __future__ import annotations

import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from agent_sdlc.adapters.errors import ForgeError
from agent_sdlc.secrets import basic_auth_header
from agent_sdlc.targets import AdoForgeConfig, IntakeConfig
from agent_sdlc.types import PrComment, WorkItem
from agent_sdlc.workspaces import git_env

API = "7.1"
_FIELDS = ("System.Id,System.Title,System.Description,Microsoft.VSTS.Common.AcceptanceCriteria,"
           "Microsoft.VSTS.TCM.ReproSteps,System.Tags,System.WorkItemType")
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "pre"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _Text()
    parser.feed(value or "")
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


class AdoForge:
    kind = "ado"
    label_word = "tag"

    def __init__(self, cfg: AdoForgeConfig, pat: str, *, intake: IntakeConfig | None = None,
                 base_branch: str = "dev", branch_prefix: str = "agent/",
                 http: httpx.Client | None = None, push_url: str | None = None) -> None:
        self._cfg = cfg
        self._intake = intake or IntakeConfig()
        self._base_branch = base_branch
        self._branch_prefix = branch_prefix
        self._http = http or httpx.Client(base_url=f"https://dev.azure.com/{cfg.org}",
                                          auth=("", pat), timeout=30)
        self._auth_header = basic_auth_header(pat)
        self._push_url = push_url or cfg.clone_url
        self._self_id: str | None = None

    # plumbing --------------------------------------------------------------
    def _req(self, method: str, path: str, *, params: dict[str, Any] | None = None,
             json: Any = None, headers: dict[str, str] | None = None) -> Any:
        params = {"api-version": API, **(params or {})}
        r = self._http.request(method, path, params=params, json=json, headers=headers)
        r.raise_for_status()
        return r.json() if r.content else None

    @property
    def _p(self) -> str:
        return f"/{self._cfg.project}/_apis"

    @property
    def _repo(self) -> str:
        return f"{self._p}/git/repositories/{self._cfg.repo}"

    def _check_branch(self, branch: str) -> None:
        if not branch.startswith(self._branch_prefix):
            raise ForgeError(f"refusing ref outside {self._branch_prefix}*: {branch}")

    # work items ------------------------------------------------------------
    def _wiql_ids(self, where: str, order: str) -> list[int]:
        query = (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project "
                 f"AND {where} ORDER BY {order}")
        res = self._req("POST", f"{self._p}/wit/wiql", json={"query": query})
        return [int(w["id"]) for w in res["workItems"]]

    def list_intake(self) -> list[WorkItem]:
        ids = self._wiql_ids(
            f"[System.Tags] CONTAINS '{self._intake.label}' "
            "AND [System.State] NOT IN ('Closed', 'Removed', 'Done')",
            "[System.CreatedDate] ASC")
        return self.get_work_items(ids)

    def get_item(self, id: int) -> WorkItem:
        [wi] = self.get_work_items([id])
        return wi

    def comment_item(self, id: int, html: str) -> None:
        self._req("POST", f"{self._p}/wit/workItems/{id}/comments",
                  params={"api-version": "7.1-preview.4"}, json={"text": html})

    def has_label(self, id: int, label: str) -> bool:
        return label in self.get_item(id).tags

    def set_label(self, id: int, label: str, present: bool) -> None:
        tags = [t for t in self.get_item(id).tags if t != label]
        if present:
            tags.append(label)
        self._req("PATCH", f"{self._p}/wit/workitems/{id}",
                  json=[{"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)}],
                  headers={"Content-Type": "application/json-patch+json"})

    def list_closed(self, limit: int) -> list[WorkItem]:
        ids = self._wiql_ids("[System.State] IN ('Closed', 'Done')", "[System.ChangedDate] DESC")
        return self.get_work_items(ids[:limit])

    def get_work_items(self, ids: list[int]) -> list[WorkItem]:
        out: list[WorkItem] = []
        for i in range(0, len(ids), 200):
            chunk = ",".join(str(x) for x in ids[i:i + 200])
            res = self._req("GET", f"{self._p}/wit/workitems",
                            params={"ids": chunk, "fields": _FIELDS})
            for v in res["value"]:
                f = v["fields"]
                out.append(WorkItem(
                    id=int(v["id"]),
                    title=f.get("System.Title", ""),
                    description=html_to_text(f.get("System.Description")
                                             or f.get("Microsoft.VSTS.TCM.ReproSteps") or ""),
                    acceptance_criteria=html_to_text(
                        f.get("Microsoft.VSTS.Common.AcceptanceCriteria") or ""),
                    work_item_type=f.get("System.WorkItemType", ""),
                    tags=tuple(t.strip() for t in (f.get("System.Tags") or "").split(";")
                               if t.strip()),
                    url=(f"https://dev.azure.com/{self._cfg.org}/{self._cfg.project}"
                         f"/_workitems/edit/{v['id']}"),
                ))
        return out

    # git & pull requests ---------------------------------------------------
    def push_branch(self, worktree: Path, branch: str) -> None:
        self._check_branch(branch)
        r = subprocess.run(
            ["git", "-c", f"http.extraheader={self._auth_header}", "push", self._push_url,
             f"HEAD:refs/heads/{branch}"],
            cwd=worktree, capture_output=True, text=True,
            env=git_env())
        if r.returncode != 0:
            raise ForgeError(f"push failed: {r.stderr.strip()}")

    def create_pr(self, branch: str, title: str, body: str, item_id: int) -> int:
        self._check_branch(branch)
        res = self._req("POST", f"{self._repo}/pullrequests", json={
            "sourceRefName": f"refs/heads/{branch}",
            "targetRefName": f"refs/heads/{self._base_branch}",
            "title": title, "description": body,
            "workItemRefs": [{"id": str(item_id)}],
        })
        return int(res["pullRequestId"])

    def update_pr(self, pr_id: int, body: str, item_id: int) -> None:
        # item_id is unused: ADO links the work item through workItemRefs on create.
        self._req("PATCH", f"{self._repo}/pullrequests/{pr_id}", json={"description": body})

    def pr_status(self, pr_id: int) -> str:
        return str(self._req("GET", f"{self._repo}/pullrequests/{pr_id}")["status"])

    def _self_identity(self) -> str:
        if self._self_id is None:
            res = self._req("GET", "/_apis/connectionData", params={"api-version": "7.1-preview"})
            self._self_id = str(res["authenticatedUser"]["id"])
        return self._self_id

    def pr_comments(self, pr_id: int) -> list[PrComment]:
        me = self._self_identity()
        out: list[PrComment] = []
        for thread in self._req("GET", f"{self._repo}/pullRequests/{pr_id}/threads")["value"]:
            if thread.get("isDeleted"):
                continue
            for c in thread.get("comments", []):
                if c.get("isDeleted") or c.get("commentType") != "text":
                    continue
                if c.get("author", {}).get("id") == me:
                    continue
                out.append(PrComment(int(thread["id"]), int(c["id"]),
                                     c["author"].get("displayName", ""), c.get("content", "")))
        return out

    def reply_pr(self, pr_id: int, comment: PrComment, text: str) -> None:
        self._req("POST",
                  f"{self._repo}/pullRequests/{pr_id}/threads/{comment.thread_id}/comments",
                  json={"content": text, "parentCommentId": comment.comment_id,
                        "commentType": 1})

    def comment_pr(self, pr_id: int, text: str) -> None:
        self._req("POST", f"{self._repo}/pullRequests/{pr_id}/threads",
                  json={"comments": [{"content": text, "commentType": 1}], "status": 4})

    def delete_branch(self, branch: str) -> None:
        self._check_branch(branch)
        refs = self._req("GET", f"{self._repo}/refs", params={"filter": f"heads/{branch}"})
        match = [r for r in refs.get("value", []) if r["name"] == f"refs/heads/{branch}"]
        if not match:
            return
        self._req("POST", f"{self._repo}/refs", json=[{
            "name": f"refs/heads/{branch}", "oldObjectId": match[0]["objectId"],
            "newObjectId": "0" * 40}])

    def git_auth_header(self) -> str:
        return self._auth_header

    def pr_ref(self, pr_id: int) -> str:
        return f"!{pr_id}"

    def item_ref(self, item_id: int) -> str:
        return f"AB#{item_id}"
