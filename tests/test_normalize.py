"""事件归一化：webhook 与 Events API 必须产出同一个 dedup_key。"""

from __future__ import annotations

from larkbot.fixtures import (
    create_payload,
    issues_payload,
    push_payload,
    release_payload,
    workflow_run_payload,
)
from larkbot.github.normalize import api_event_type_to_kind, normalize_api_event, normalize_webhook

HEAD_SHA = "9f2c1a4b7d3e5f60718293a4b5c6d7e8f9012345"
BEFORE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _api_push_event(head: str = HEAD_SHA, repo: str = "acme/api") -> dict:
    return {
        "id": "12345678901",
        "type": "PushEvent",
        "actor": {"login": "octocat", "display_login": "octocat"},
        "repo": {"id": 1, "name": repo, "url": f"https://api.github.com/repos/{repo}"},
        "payload": {
            "push_id": 987654,
            "size": 1,
            "distinct_size": 1,
            "ref": "refs/heads/main",
            "head": head,
            "before": BEFORE_SHA,
            "commits": [
                {
                    "sha": head,
                    "message": "fix: 边界条件",
                    "author": {"email": "a@example.com", "name": "octocat", "username": "octocat"},
                    "distinct": True,
                    "url": f"https://api.github.com/repos/{repo}/commits/{head}",
                }
            ],
        },
        "public": True,
        "created_at": "2024-05-01T10:00:00Z",
    }


def test_webhook_push_normalized():
    event = normalize_webhook("push", push_payload(repo="acme/api", branch="main", commit_count=2))
    assert event is not None
    assert event.kind == "push"
    assert event.repo == "acme/api"
    assert event.ref == "main"
    assert event.meta["head_sha"] == HEAD_SHA[:10]
    assert event.action_label == "推送 2 个提交"
    assert len(event.commits) == 2
    assert event.url is not None
    assert event.url.startswith("https://github.com/acme/api/compare/")
    assert event.dedup_key == f"push|acme/api|refs/heads/main|{HEAD_SHA}"


def test_push_dedup_key_matches_between_webhook_and_events_api():
    """核心保证：两条链路算出的 key 必须一致，否则 webhook + 轮询会重复推送。"""
    webhook_event = normalize_webhook("push", push_payload(repo="acme/api"))
    api_event = normalize_api_event(_api_push_event())
    assert webhook_event is not None and api_event is not None
    assert webhook_event.dedup_key == api_event.dedup_key


def test_deleted_branch_push():
    event = normalize_webhook("push", push_payload(repo="acme/api", branch="feature/gone", deleted=True))
    assert event is not None
    assert event.deleted is True
    assert event.action_label == "删除分支"
    assert event.title == "删除分支 feature/gone"


def test_pull_request_merged():
    from larkbot.fixtures import pull_request_payload

    payload = pull_request_payload(repo="acme/api", number=42, action="closed", merged=True, labels=["release"])
    event = normalize_webhook("pull_request", payload)
    assert event is not None
    assert event.number == 42
    assert event.merged is True
    assert event.action_label == "已合并"
    assert event.ref == "main"
    assert event.author == "contributor"
    assert event.labels == ("release",)
    assert event.dedup_key.startswith("pull_request|acme/api|42|closed|")


def test_pull_request_same_key_from_events_api():
    from larkbot.fixtures import pull_request_payload

    payload = pull_request_payload(repo="acme/api", number=42, action="opened")
    webhook_event = normalize_webhook("pull_request", payload)
    api_item = {
        "id": "1",
        "type": "PullRequestEvent",
        "actor": {"login": "octocat"},
        "repo": {"name": "acme/api"},
        "payload": {
            "action": payload["action"],
            "number": payload["number"],
            "pull_request": payload["pull_request"],
        },
        "created_at": "2024-05-01T10:00:00Z",
    }
    api_event = normalize_api_event(api_item)
    assert webhook_event is not None and api_event is not None
    assert webhook_event.dedup_key == api_event.dedup_key


def test_release_uses_tag_and_dedup_by_id():
    event = normalize_webhook("release", release_payload(repo="acme/api", tag="v1.2.3"))
    assert event is not None
    assert event.kind == "release"
    assert event.ref == "v1.2.3"
    assert event.meta["ref_type"] == "tag"
    assert event.dedup_key == "release|acme/api|300001|published"


def test_issues_and_comment_and_workflow():
    issues = normalize_webhook("issues", issues_payload(repo="acme/api", number=7))
    assert issues is not None and issues.number == 7 and issues.labels == ("bug",)
    assert issues.action_label == "已开启"

    workflow = normalize_webhook("workflow_run", workflow_run_payload(repo="acme/api", conclusion="failure"))
    assert workflow is not None
    assert workflow.meta["conclusion"] == "failure"
    assert workflow.ref == "main"
    assert workflow.dedup_key == "workflow_run|acme/api|800001|failure"


def test_create_event_marks_branch_created():
    event = normalize_webhook("create", create_payload(repo="acme/api", ref="feature/x"))
    assert event is not None
    assert event.ref == "feature/x"
    assert event.action_label == "已创建"
    assert event.meta["ref_type"] == "branch"


def test_unknown_event_type_gets_stable_fallback_key():
    payload = {"repository": {"full_name": "acme/api"}, "sender": {"login": "octocat"}, "weird": {"id": 5}}
    first = normalize_webhook("repository_vulnerability_alert", payload)
    second = normalize_webhook("repository_vulnerability_alert", payload)
    assert first is not None and second is not None
    assert first.kind == "unknown"
    assert first.dedup_key == second.dedup_key
    assert first.dedup_key.startswith("unknown|acme/api|")


def test_missing_repo_returns_none():
    assert normalize_webhook("push", {"ref": "refs/heads/main"}) is None
    assert normalize_api_event({"type": "PushEvent"}) is None


def test_api_event_type_mapping():
    assert api_event_type_to_kind("PushEvent") == "push"
    assert api_event_type_to_kind("PullRequestReviewCommentEvent") == "pull_request_review_comment"
    assert api_event_type_to_kind("IssueCommentEvent") == "issue_comment"
    assert api_event_type_to_kind("WatchEvent") == "watch"
    assert api_event_type_to_kind("SomethingBrandNewEvent") == "unknown"
    assert api_event_type_to_kind(None) == "unknown"
