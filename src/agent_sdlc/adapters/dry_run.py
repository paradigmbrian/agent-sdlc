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
