"""把 GitHub 的 webhook 负载与 REST Events API 负载归一化成 ``RepoEvent``。

关键点：两条链路生成**同一个** ``dedup_key``，因此 webhook 与轮询可以同时开启，
重复事件在投递前就会被识别出来。
"""

from __future__ import annotations

import re
from typing import Any

from ..models import KNOWN_KINDS, RepoEvent, Source, fallback_dedup_key, make_dedup_key
from ..util import as_dict, parse_ts, truncate

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def api_event_type_to_kind(event_type: str | None) -> str:
    """``PushEvent`` -> ``push``；``PullRequestReviewCommentEvent`` -> ``pull_request_review_comment``。"""
    if not event_type:
        return "unknown"
    stem = event_type[: -len("Event")] if event_type.endswith("Event") else event_type
    snake = _CAMEL_BOUNDARY.sub("_", stem).lower()
    return snake if snake in KNOWN_KINDS else "unknown"


def _first_line(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return text.strip().splitlines()[0] if text.strip() else ""


def _repo_url(repo: str, explicit: str | None = None) -> str:
    return explicit or f"https://github.com/{repo}"


def _commit_entry(raw: dict[str, Any]) -> dict[str, Any]:
    sha = str(raw.get("sha") or "")
    author = raw.get("author")
    if isinstance(author, dict):
        # 优先用 GitHub 登录名，没有才退回展示名
        name = author.get("username") or author.get("name") or author.get("email") or "unknown"
    elif isinstance(author, str):
        name = author
    else:
        name = "unknown"
    return {
        "sha": sha,
        "short_sha": sha[:7],
        "message": _first_line(raw.get("message")) or "(无提交信息)",
        "author": str(name),
        "url": raw.get("url") or (f"https://github.com/{raw.get('_repo', '')}/commit/{sha}" if sha else None),
        "distinct": bool(raw.get("distinct", True)),
    }


def _label_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[str] = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


def _user_login(value: Any) -> str | None:
    if isinstance(value, dict):
        login = value.get("login")
        return str(login) if login else None
    if isinstance(value, str) and value:
        return value
    return None


def normalize_webhook(event_name: str, payload: dict[str, Any], *, source: Source = "webhook") -> RepoEvent | None:
    """webhook 负载（顶层就是 payload）-> RepoEvent。"""
    if not isinstance(payload, dict):
        return None
    repository = as_dict(payload.get("repository"))
    repo = repository.get("full_name") or (payload.get("repo") or {}).get("name")
    if not repo:
        return None
    return _build(
        event_name,
        payload,
        repo=str(repo),
        actor=_user_login(payload.get("sender")) or _user_login(repository.get("owner")),
        repo_url=repository.get("html_url"),
        occurred_at=None,
        source=source,
    )


def normalize_api_event(item: dict[str, Any], *, source: Source = "poll") -> RepoEvent | None:
    """REST ``/repos/{owner}/{repo}/events`` 返回的单条记录 -> RepoEvent。"""
    if not isinstance(item, dict):
        return None
    repo = (item.get("repo") or {}).get("name")
    if not repo:
        return None
    payload = as_dict(item.get("payload"))
    return _build(
        api_event_type_to_kind(item.get("type")),
        payload,
        repo=str(repo),
        actor=_user_login(item.get("actor")),
        repo_url=None,
        occurred_at=parse_ts(item.get("created_at")),
        source=source,
    )


def _build(
    kind: str,
    payload: dict[str, Any],
    *,
    repo: str,
    actor: str | None,
    repo_url: str | None,
    occurred_at: Any,
    source: Source,
) -> RepoEvent:
    event = RepoEvent(
        kind=kind if kind in KNOWN_KINDS else "unknown",
        repo=repo,
        source=source,
        action=payload.get("action") if isinstance(payload.get("action"), str) else None,
        actor=actor,
        occurred_at=occurred_at,
        dedup_key="",
    )
    event.meta["repo_url"] = _repo_url(repo, repo_url)
    handler = _HANDLERS.get(event.kind, _handle_generic)
    handler(event, payload)
    if not event.dedup_key:
        event.dedup_key = fallback_dedup_key(event.kind, repo, payload)
    return event


# ---------------------------------------------------------------------------
# 各事件类型的归一化
# ---------------------------------------------------------------------------


def _handle_push(event: RepoEvent, payload: dict[str, Any]) -> None:
    raw_ref = str(payload.get("ref") or "")
    ref = raw_ref
    ref_type = "branch"
    for prefix, kind in (("refs/heads/", "branch"), ("refs/tags/", "tag")):
        if ref.startswith(prefix):
            ref, ref_type = ref[len(prefix) :], kind
            break
    head_sha = str(payload.get("after") or payload.get("head") or "")
    before_sha = str(payload.get("before") or "")
    deleted = bool(payload.get("deleted")) or bool(head_sha) and set(head_sha) == {"0"}

    commits = [_commit_entry(raw) for raw in (payload.get("commits") or []) if isinstance(raw, dict)]
    for entry in commits:
        entry["_repo"] = event.repo

    if payload.get("compare"):
        url = str(payload["compare"])
    elif deleted and before_sha and set(before_sha) != {"0"}:
        url = f"https://github.com/{event.repo}/tree/{before_sha}"
    elif before_sha and head_sha and set(before_sha) != {"0"}:
        url = f"https://github.com/{event.repo}/compare/{before_sha[:12]}...{head_sha[:12]}"
    elif head_sha:
        url = f"https://github.com/{event.repo}/commit/{head_sha}"
    else:
        url = event.repo_url

    head_commit = payload.get("head_commit") if isinstance(payload.get("head_commit"), dict) else None
    if not event.occurred_at and head_commit:
        event.occurred_at = parse_ts(head_commit.get("timestamp"))

    event.ref = ref
    event.url = url
    event.title = f"推送到 {ref or '未知分支'}" if not deleted else f"删除分支 {ref}"
    if commits:
        latest = commits[-1]["message"]
        event.summary = f"{len(commits)} 个提交，最新：{latest}"
    event.meta.update(
        {
            "ref_type": ref_type,
            "head_sha": head_sha[:10],
            "before_sha": before_sha[:10],
            "deleted": deleted,
            "forced": bool(payload.get("forced")),
            "commits": commits,
            "author": commits[-1]["author"] if commits else None,
        }
    )
    event.dedup_key = make_dedup_key("push", event.repo, raw_ref, head_sha)


def _handle_pull_request(event: RepoEvent, payload: dict[str, Any]) -> None:
    pr = as_dict(payload.get("pull_request"))
    base = as_dict(pr.get("base"))
    head = as_dict(pr.get("head"))
    event.number = payload.get("number") or pr.get("number")
    event.title = pr.get("title") or f"PR #{event.number}"
    event.url = pr.get("html_url")
    event.ref = base.get("ref")
    event.merged = bool(pr.get("merged"))
    event.draft = bool(pr.get("draft"))
    event.labels = _label_names(pr.get("labels"))
    event.summary = pr.get("body")
    if not event.occurred_at:
        event.occurred_at = parse_ts(pr.get("updated_at") or pr.get("created_at"))
    event.meta.update(
        {
            "author": _user_login(pr.get("user")),
            "head_ref": head.get("ref"),
            "base_ref": base.get("ref"),
            "additions": pr.get("additions"),
            "deletions": pr.get("deletions"),
            "changed_files": pr.get("changed_files"),
            "commits": pr.get("commits"),
            "merged_by": _user_login(pr.get("merged_by")),
            "requested_reviewers": [
                login for login in (_user_login(item) for item in (pr.get("requested_reviewers") or [])) if login
            ],
        }
    )
    event.dedup_key = make_dedup_key(
        "pull_request", event.repo, event.number, event.action, pr.get("updated_at") or pr.get("created_at")
    )


def _handle_issues(event: RepoEvent, payload: dict[str, Any]) -> None:
    issue = as_dict(payload.get("issue"))
    event.number = issue.get("number")
    event.title = issue.get("title") or f"Issue #{event.number}"
    event.url = issue.get("html_url")
    event.labels = _label_names(issue.get("labels"))
    event.summary = issue.get("body")
    event.meta["author"] = _user_login(issue.get("user"))
    event.meta["state_reason"] = issue.get("state_reason")
    if not event.occurred_at:
        event.occurred_at = parse_ts(issue.get("updated_at") or issue.get("created_at"))
    event.dedup_key = make_dedup_key(
        "issues", event.repo, event.number, event.action, issue.get("updated_at") or issue.get("created_at")
    )


def _handle_issue_comment(event: RepoEvent, payload: dict[str, Any]) -> None:
    comment = as_dict(payload.get("comment"))
    issue = as_dict(payload.get("issue"))
    event.number = issue.get("number")
    event.title = issue.get("title") or f"#{event.number} 的新评论"
    event.url = comment.get("html_url") or issue.get("html_url")
    event.summary = comment.get("body")
    event.meta["author"] = _user_login(comment.get("user"))
    event.meta["is_pull_request"] = "pull_request" in issue
    if not event.occurred_at:
        event.occurred_at = parse_ts(comment.get("updated_at") or comment.get("created_at"))
    event.dedup_key = make_dedup_key("issue_comment", event.repo, comment.get("id"), event.action)


def _handle_commit_comment(event: RepoEvent, payload: dict[str, Any]) -> None:
    comment = as_dict(payload.get("comment"))
    sha = str(comment.get("commit_id") or "")
    event.title = f"提交 {sha[:7]} 上的评论"
    event.url = comment.get("html_url")
    event.summary = comment.get("body")
    event.meta["author"] = _user_login(comment.get("user"))
    event.meta["sha"] = sha[:10]
    if not event.occurred_at:
        event.occurred_at = parse_ts(comment.get("updated_at") or comment.get("created_at"))
    event.dedup_key = make_dedup_key("commit_comment", event.repo, comment.get("id"), event.action)


def _handle_pull_request_review(event: RepoEvent, payload: dict[str, Any]) -> None:
    review = as_dict(payload.get("review"))
    pr = as_dict(payload.get("pull_request"))
    event.number = pr.get("number")
    event.title = pr.get("title") or f"PR #{event.number}"
    event.url = review.get("html_url") or pr.get("html_url")
    event.summary = review.get("body")
    event.ref = (pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else None
    event.meta["author"] = _user_login(review.get("user")) or _user_login(pr.get("user"))
    event.meta["review_state"] = review.get("state")
    if not event.occurred_at:
        event.occurred_at = parse_ts(review.get("submitted_at") or pr.get("updated_at"))
    event.dedup_key = make_dedup_key(
        "pull_request_review", event.repo, review.get("id"), review.get("state"), review.get("submitted_at")
    )


def _handle_pull_request_review_comment(event: RepoEvent, payload: dict[str, Any]) -> None:
    comment = as_dict(payload.get("comment"))
    pr = as_dict(payload.get("pull_request"))
    event.number = pr.get("number")
    event.title = pr.get("title") or f"PR #{event.number}"
    event.url = comment.get("html_url") or pr.get("html_url")
    event.summary = comment.get("body")
    event.ref = (pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else None
    event.meta["author"] = _user_login(comment.get("user"))
    if not event.occurred_at:
        event.occurred_at = parse_ts(comment.get("updated_at") or comment.get("created_at"))
    event.dedup_key = make_dedup_key("pull_request_review_comment", event.repo, comment.get("id"), event.action)


def _handle_release(event: RepoEvent, payload: dict[str, Any]) -> None:
    release = as_dict(payload.get("release"))
    tag = release.get("tag_name")
    event.title = release.get("name") or tag or "Release"
    event.ref = tag
    event.url = release.get("html_url")
    event.summary = release.get("body")
    event.meta["ref_type"] = "tag"
    event.meta["author"] = _user_login(release.get("author")) or _user_login(release.get("user"))
    event.meta["prerelease"] = bool(release.get("prerelease"))
    event.meta["draft"] = bool(release.get("draft"))
    event.meta["tag_name"] = tag
    if not event.occurred_at:
        event.occurred_at = parse_ts(release.get("published_at") or release.get("created_at"))
    event.dedup_key = make_dedup_key("release", event.repo, release.get("id"), event.action)


def _handle_ref(event: RepoEvent, payload: dict[str, Any]) -> None:
    """create / delete 事件（分支或标签）。"""
    ref = payload.get("ref")
    ref_type = payload.get("ref_type") or "branch"
    creating = event.kind == "create"
    event.ref = ref
    event.action = event.action or ("created" if creating else "deleted")
    event.title = f"{'创建' if creating else '删除'}{'分支' if ref_type == 'branch' else '标签'} {ref}"
    event.url = f"https://github.com/{event.repo}/tree/{ref}" if ref and ref_type == "branch" else event.repo_url
    event.meta["ref_type"] = ref_type
    event.meta["deleted"] = not creating
    event.dedup_key = make_dedup_key(
        event.kind, event.repo, ref_type, ref, event.occurred_at or payload.get("master_branch")
    )


def _handle_fork(event: RepoEvent, payload: dict[str, Any]) -> None:
    forkee = as_dict(payload.get("forkee"))
    event.title = f"Fork 到 {forkee.get('full_name') or '新仓库'}"
    event.url = forkee.get("html_url") or event.repo_url
    event.meta["author"] = _user_login(forkee.get("owner"))
    event.dedup_key = make_dedup_key("fork", event.repo, forkee.get("id"), event.occurred_at)


def _handle_watch(event: RepoEvent, payload: dict[str, Any]) -> None:
    event.action = event.action or "started"
    event.title = "给仓库点了个 Star"
    event.url = event.repo_url
    event.dedup_key = make_dedup_key("watch", event.repo, event.actor, event.occurred_at)


def _handle_public(event: RepoEvent, payload: dict[str, Any]) -> None:
    event.title = "仓库已公开"
    event.url = event.repo_url
    event.dedup_key = make_dedup_key("public", event.repo, event.occurred_at)


def _handle_workflow_run(event: RepoEvent, payload: dict[str, Any]) -> None:
    run = as_dict(payload.get("workflow_run"))
    event.title = run.get("name") or "Workflow"
    event.url = run.get("html_url")
    event.ref = run.get("head_branch")
    event.meta["ref_type"] = "branch"
    event.meta["conclusion"] = run.get("conclusion")
    event.meta["status"] = run.get("status")
    event.meta["run_number"] = run.get("run_number")
    event.meta["event"] = run.get("event")
    event.meta["author"] = _user_login(run.get("actor")) or _user_login(run.get("triggering_actor"))
    event.meta["head_sha"] = str(run.get("head_sha") or "")[:10]
    if not event.occurred_at:
        event.occurred_at = parse_ts(run.get("updated_at") or run.get("run_started_at"))
    event.dedup_key = make_dedup_key(
        "workflow_run", event.repo, run.get("id"), run.get("conclusion") or run.get("status")
    )


def _handle_check_run(event: RepoEvent, payload: dict[str, Any]) -> None:
    check = as_dict(payload.get("check_run"))
    event.title = check.get("name") or "Check"
    event.url = check.get("html_url")
    event.meta["conclusion"] = check.get("conclusion")
    event.meta["status"] = check.get("status")
    event.meta["head_sha"] = str(check.get("head_sha") or "")[:10]
    if not event.occurred_at:
        event.occurred_at = parse_ts(check.get("completed_at") or check.get("started_at"))
    event.dedup_key = make_dedup_key(
        "check_run", event.repo, check.get("id"), check.get("conclusion") or check.get("status")
    )


def _handle_status(event: RepoEvent, payload: dict[str, Any]) -> None:
    branches = payload.get("branches") or []
    if branches and isinstance(branches[0], dict):
        event.ref = branches[0].get("name")
    sha = str(payload.get("sha") or "")
    event.title = payload.get("description") or payload.get("context") or "提交状态更新"
    event.url = payload.get("target_url")
    event.meta["state"] = payload.get("state")
    event.meta["context"] = payload.get("context")
    event.meta["head_sha"] = sha[:10]
    event.dedup_key = make_dedup_key(
        "status", event.repo, sha, payload.get("context"), payload.get("state"), event.occurred_at
    )


def _handle_discussion(event: RepoEvent, payload: dict[str, Any]) -> None:
    discussion = as_dict(payload.get("discussion"))
    event.number = discussion.get("number")
    event.title = discussion.get("title") or f"Discussion #{event.number}"
    event.url = discussion.get("html_url")
    event.summary = discussion.get("body")
    category = discussion.get("category")
    if isinstance(category, dict):
        event.meta["category"] = category.get("name")
    event.meta["author"] = _user_login(discussion.get("user"))
    if not event.occurred_at:
        event.occurred_at = parse_ts(discussion.get("updated_at") or discussion.get("created_at"))
    event.dedup_key = make_dedup_key("discussion", event.repo, event.number, event.action, discussion.get("updated_at"))


def _handle_discussion_comment(event: RepoEvent, payload: dict[str, Any]) -> None:
    discussion = as_dict(payload.get("discussion"))
    comment = as_dict(payload.get("comment"))
    event.number = discussion.get("number")
    event.title = discussion.get("title") or f"Discussion #{event.number}"
    event.url = comment.get("html_url") or discussion.get("html_url")
    event.summary = comment.get("body")
    event.meta["author"] = _user_login(comment.get("user"))
    event.dedup_key = make_dedup_key(
        "discussion_comment", event.repo, comment.get("id") or discussion.get("id"), event.action
    )


def _handle_member(event: RepoEvent, payload: dict[str, Any]) -> None:
    member = as_dict(payload.get("member"))
    event.title = f"成员 {member.get('login') or ''}".strip()
    event.url = member.get("html_url") or event.repo_url
    event.meta["author"] = _user_login(member)
    event.dedup_key = make_dedup_key("member", event.repo, member.get("id"), event.action, event.occurred_at)


def _handle_gollum(event: RepoEvent, payload: dict[str, Any]) -> None:
    pages = payload.get("pages") or []
    names = [page.get("page_name") for page in pages if isinstance(page, dict) and page.get("page_name")]
    event.title = "Wiki 更新"
    event.url = event.repo_url + "/wiki"
    event.summary = "、".join(truncate(name, 40) for name in names[:5]) or None
    event.dedup_key = make_dedup_key("gollum", event.repo, ",".join(sorted(str(n) for n in names)), event.occurred_at)


def _handle_generic(event: RepoEvent, payload: dict[str, Any]) -> None:
    """未知类型：尽力提取标题和链接，去重交给 payload 摘要。"""
    event.title = payload.get("action") or payload.get("ref") or payload.get("description") or event.kind_label
    for key in ("html_url", "target_url", "url"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.startswith("http"):
            event.url = candidate
            break
    event.url = event.url or event.repo_url


_HANDLERS = {
    "push": _handle_push,
    "pull_request": _handle_pull_request,
    "issues": _handle_issues,
    "issue_comment": _handle_issue_comment,
    "commit_comment": _handle_commit_comment,
    "pull_request_review": _handle_pull_request_review,
    "pull_request_review_comment": _handle_pull_request_review_comment,
    "release": _handle_release,
    "create": _handle_ref,
    "delete": _handle_ref,
    "fork": _handle_fork,
    "watch": _handle_watch,
    "public": _handle_public,
    "workflow_run": _handle_workflow_run,
    "check_run": _handle_check_run,
    "status": _handle_status,
    "discussion": _handle_discussion,
    "discussion_comment": _handle_discussion_comment,
    "member": _handle_member,
    "gollum": _handle_gollum,
}
