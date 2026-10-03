"""本地演练用的合成 webhook 负载。

``larkbot simulate`` 用它构造一条「看起来像真 GitHub 发来的」事件，走完整链路
（签名之外的部分）：归一化 -> 路由 -> 卡片 -> 投递，便于在没有 GitHub 的环境下调试。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .util import utcnow

logger = logging.getLogger(__name__)


def _repo_block(repo: str) -> dict[str, Any]:
    owner = repo.split("/", 1)[0]
    return {
        "id": 1296269,
        "name": repo.split("/", 1)[-1],
        "full_name": repo,
        "html_url": f"https://github.com/{repo}",
        "owner": {"login": owner, "html_url": f"https://github.com/{owner}"},
        "default_branch": "main",
    }


def _user(login: str) -> dict[str, Any]:
    return {"login": login, "html_url": f"https://github.com/{login}"}


def _now_iso(offset_minutes: int = 0) -> str:
    return (utcnow() - timedelta(minutes=offset_minutes)).isoformat().replace("+00:00", "Z")


def push_payload(
    repo: str = "octocat/Hello-World",
    *,
    branch: str = "main",
    commit_count: int = 2,
    actor: str = "octocat",
    deleted: bool = False,
    forced: bool = False,
) -> dict[str, Any]:
    head = "9f2c1a4b7d3e5f60718293a4b5c6d7e8f9012345"
    before = "0123456789abcdef0123456789abcdef01234567"
    commits = [
        {
            "id": f"c{index}",
            "sha": f"{index:040x}",
            "message": f"fix: 修复第 {index} 个问题\n\n详细说明",
            "author": {"name": f"开发者 {index}", "email": f"dev{index}@example.com", "username": actor},
            "url": f"https://github.com/{repo}/commit/c{index}",
            "distinct": True,
        }
        for index in range(1, commit_count + 1)
    ]
    return {
        "ref": f"refs/heads/{branch}",
        "before": before,
        "after": "0000000000000000000000000000000000000000" if deleted else head,
        "created": False,
        "deleted": deleted,
        "forced": forced,
        "compare": f"https://github.com/{repo}/compare/{before[:12]}...{head[:12]}",
        "commits": commits,
        "head_commit": dict(commits[-1], timestamp=int(utcnow().timestamp())),
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def pull_request_payload(
    repo: str = "octocat/Hello-World",
    *,
    number: int = 42,
    action: str = "opened",
    merged: bool = False,
    draft: bool = False,
    title: str = "feat: 支持 webhook 与轮询双通道",
    actor: str = "octocat",
    author: str = "contributor",
    branch: str = "main",
    head_branch: str = "feature/dual-channel",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "id": 900001,
            "number": number,
            "title": title,
            "body": "这个 PR 让 bot 在 webhook 挂掉时还能靠轮询兜底。",
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "state": "closed" if action == "closed" else "open",
            "draft": draft,
            "merged": merged,
            "merged_by": _user(actor) if merged else None,
            "created_at": _now_iso(30),
            "updated_at": _now_iso(0),
            "user": _user(author),
            "base": {"ref": branch, "sha": "base0000000000000000000000000000000000"},
            "head": {"ref": head_branch, "sha": "head0000000000000000000000000000000000"},
            "labels": [{"name": name} for name in (labels or ["enhancement"])],
            "requested_reviewers": [_user("reviewer")],
        },
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def issues_payload(
    repo: str = "octocat/Hello-World",
    *,
    number: int = 7,
    action: str = "opened",
    title: str = "轮询模式下会漏掉同一秒的事件",
    actor: str = "octocat",
    author: str = "reporter",
) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "id": 700001,
            "number": number,
            "title": title,
            "body": "复现步骤：1. 关掉 webhook 2. 连推两次 …",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "state": "closed" if action == "closed" else "open",
            "state_reason": "completed" if action == "closed" else None,
            "created_at": _now_iso(120),
            "updated_at": _now_iso(0),
            "user": _user(author),
            "labels": [{"name": "bug"}],
        },
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def issue_comment_payload(
    repo: str = "octocat/Hello-World",
    *,
    number: int = 7,
    action: str = "created",
    comment_id: int = 555001,
    actor: str = "octocat",
    body: str = "我这边也复现了，怀疑是游标边界问题。",
) -> dict[str, Any]:
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": "轮询模式下会漏掉同一秒的事件",
            "html_url": f"https://github.com/{repo}/issues/{number}",
            "pull_request": None,
        },
        "comment": {
            "id": comment_id,
            "body": body,
            "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}",
            "created_at": _now_iso(1),
            "updated_at": _now_iso(1),
            "user": _user(actor),
        },
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def release_payload(
    repo: str = "octocat/Hello-World",
    *,
    action: str = "published",
    tag: str = "v0.1.0",
    name: str = "larkbot v0.1.0 原型",
    actor: str = "octocat",
    prerelease: bool = False,
) -> dict[str, Any]:
    return {
        "action": action,
        "release": {
            "id": 300001,
            "tag_name": tag,
            "name": name,
            "body": "## 新增\n- webhook + 轮询双通道\n- 按群订阅仓库",
            "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
            "draft": False,
            "prerelease": prerelease,
            "created_at": _now_iso(2),
            "published_at": _now_iso(2),
            "author": _user(actor),
        },
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def workflow_run_payload(
    repo: str = "octocat/Hello-World",
    *,
    action: str = "completed",
    conclusion: str = "failure",
    name: str = "CI",
    branch: str = "main",
    run_id: int = 800001,
    actor: str = "octocat",
) -> dict[str, Any]:
    return {
        "action": action,
        "workflow_run": {
            "id": run_id,
            "name": name,
            "run_number": 128,
            "event": "push",
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": branch,
            "head_sha": "abcdef0123456789abcdef0123456789abcdef01",
            "html_url": f"https://github.com/{repo}/actions/runs/{run_id}",
            "created_at": _now_iso(6),
            "updated_at": _now_iso(3),
            "actor": _user(actor),
        },
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def create_payload(
    repo: str = "octocat/Hello-World",
    *,
    ref: str = "feature/new-branch",
    ref_type: str = "branch",
    actor: str = "octocat",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "ref_type": ref_type,
        "master_branch": "main",
        "description": None,
        "pusher_type": "user",
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def delete_payload(
    repo: str = "octocat/Hello-World",
    *,
    ref: str = "feature/old-branch",
    ref_type: str = "branch",
    actor: str = "octocat",
) -> dict[str, Any]:
    return create_payload(repo, ref=ref, ref_type=ref_type, actor=actor)


def fork_payload(repo: str = "octocat/Hello-World", *, actor: str = "fan") -> dict[str, Any]:
    forkee_name = f"{actor}/Hello-World"
    return {
        "forkee": {
            "id": 400001,
            "full_name": forkee_name,
            "html_url": f"https://github.com/{forkee_name}",
            "owner": _user(actor),
        },
        "repository": _repo_block(repo),
        "sender": _user(actor),
    }


def star_payload(repo: str = "octocat/Hello-World", *, actor: str = "fan") -> dict[str, Any]:
    return {"action": "started", "repository": _repo_block(repo), "sender": _user(actor)}


#: simulate --event 的取值 -> (GitHub 事件名, 构造器)
SCENARIOS: dict[str, tuple[str, Callable[..., dict[str, Any]]]] = {
    "push": ("push", push_payload),
    "pr": ("pull_request", pull_request_payload),
    "issue": ("issues", issues_payload),
    "comment": ("issue_comment", issue_comment_payload),
    "release": ("release", release_payload),
    "workflow": ("workflow_run", workflow_run_payload),
    "create": ("create", create_payload),
    "delete": ("delete", delete_payload),
    "fork": ("fork", fork_payload),
    "star": ("watch", star_payload),
}


def build(scenario: str, **overrides: Any) -> tuple[str, dict[str, Any]]:
    """构造一次模拟事件，返回 ``(github_event_name, payload)``。

    不同场景支持的参数不同（比如 ``star`` 没有 action），这里按构造器签名过滤，
    多传的参数会被忽略，方便 CLI 用一套参数跑所有场景。
    """
    if scenario not in SCENARIOS:
        raise KeyError(f"未知场景 {scenario!r}，可选: {sorted(SCENARIOS)}")
    event_name, factory = SCENARIOS[scenario]
    accepted = set(inspect.signature(factory).parameters)
    kwargs = {key: value for key, value in overrides.items() if value is not None and key in accepted}
    ignored = sorted(key for key, value in overrides.items() if value is not None and key not in accepted)
    if ignored:
        logger.debug("场景 %s 不支持这些覆盖参数，已忽略: %s", scenario, ignored)
    return event_name, factory(**kwargs)
