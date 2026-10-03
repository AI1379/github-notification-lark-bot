"""轮询回退：基线、游标、ETag、webhook 健康跳过、跨源去重。"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from larkbot.fixtures import push_payload
from larkbot.github.client import GitHubClient
from larkbot.github.normalize import normalize_webhook
from larkbot.poller import Poller
from larkbot.router import Router
from larkbot.service import NotificationService
from larkbot.util import to_iso, utcnow
from tests.conftest import RecordingFeishu, make_config

HEAD_SHA = "9f2c1a4b7d3e5f60718293a4b5c6d7e8f9012345"
EVENTS_URL = "https://api.github.com/repos/acme/api/events"


def api_push_event(head: str = HEAD_SHA, created_at: str | None = None) -> dict:
    return {
        "id": "1",
        "type": "PushEvent",
        "actor": {"login": "octocat"},
        "repo": {"name": "acme/api"},
        "payload": {
            "ref": "refs/heads/main",
            "head": head,
            "before": "0123456789abcdef0123456789abcdef01234567",
            "commits": [{"sha": head, "message": "fix: 边界", "author": {"username": "octocat"}}],
        },
        "created_at": created_at or to_iso(utcnow()),
    }


async def _build(config, store, feishu):
    github = GitHubClient(token=None)
    service = NotificationService(config, store, feishu, Router(config))
    return Poller(config, store, github, service), github


@respx.mock
async def test_first_poll_only_sets_baseline(store):
    config = make_config()
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event()], headers={"etag": '"e1"'}))
        reports = await poller.poll_once()
    finally:
        await github.aclose()

    assert [report.status for report in reports] == ["baseline"]
    assert feishu.sent == []
    state = await store.poll_state("acme/api")
    assert state is not None and state["cursor"] is not None and state["etag"] == '"e1"'


@respx.mock
async def test_backlog_first_poll_dispatches_recent_events(store):
    config = make_config(github={"first_poll": "backlog"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event()]))
        reports = await poller.poll_once()
    finally:
        await github.aclose()

    assert reports[0].status == "ok"
    assert reports[0].new_events == 1
    assert reports[0].delivered == 1
    assert feishu.chats() == ["dev"]


@respx.mock
async def test_not_modified_uses_etag(store):
    config = make_config()
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.save_poll_state("acme/api", etag='"e1"', cursor=to_iso(utcnow()), status="ok")
    try:
        route = respx.get(EVENTS_URL).mock(return_value=httpx.Response(304))
        reports = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert reports.status == "no_change"
    assert route.calls[0].request.headers.get("if-none-match") == '"e1"'
    assert feishu.sent == []


@respx.mock
async def test_skip_poll_when_webhook_is_fresh(store):
    config = make_config(github={"poll_mode": "auto", "webhook_freshness_seconds": 900})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.remember_repo("acme/api", "webhook")
    try:
        route = respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[]))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.status == "skipped_webhook_fresh"
    assert route.call_count == 0  # 压根没打 GitHub API


@respx.mock
async def test_always_mode_polls_even_with_fresh_webhook(store):
    config = make_config(github={"poll_mode": "always", "first_poll": "backlog"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.remember_repo("acme/api", "webhook")
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event()]))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.status == "ok"
    assert report.delivered == 1


@respx.mock
async def test_webhook_then_poll_does_not_resend(store):
    """webhook 已经发过的事件，轮询再拿到必须被去重。"""
    config = make_config(github={"first_poll": "backlog", "poll_mode": "always"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    service = NotificationService(config, store, feishu, Router(config))

    webhook_event = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    assert webhook_event is not None
    assert (await service.dispatch(webhook_event)).delivered == ["dev"]

    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event()]))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.new_events == 1
    assert report.delivered == 0
    assert report.skipped == 1
    assert len(feishu.sent) == 1


@respx.mock
async def test_poll_skips_events_older_than_cursor_window(store):
    config = make_config(github={"first_poll": "backlog", "poll_overlap_seconds": 60})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.save_poll_state("acme/api", cursor=to_iso(utcnow()), status="ok")
    old = to_iso(utcnow().replace(year=utcnow().year - 1))
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event(created_at=old)]))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.fetched == 1
    assert report.new_events == 0
    assert feishu.sent == []


@respx.mock
async def test_github_error_is_recorded(store):
    config = make_config()
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(500, text="boom"))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.status == "error"
    state = await store.poll_state("acme/api")
    assert state is not None and state["last_status"] == "error" and "500" in state["last_error"]


@respx.mock
async def test_404_marks_repo_error(store):
    config = make_config()
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()
    assert report.status == "error"
    assert "无权访问" in (report.error or "")


async def _backdate_webhook_seen(store, repo: str, minutes: int) -> None:
    """把「最后一次收到 webhook」的时间往前拨，模拟隧道此时断了。"""
    await store.connection.execute(
        "UPDATE repos SET webhook_last_seen_at = ? WHERE repo = ?",
        (to_iso(utcnow() - timedelta(minutes=minutes)), repo),
    )
    await store.connection.commit()


@respx.mock
async def test_fallback_recovers_events_missed_during_webhook_outage(store):
    """核心行为：一直靠 webhook 的仓库，隧道断后轮询接手时，必须把断链期间漏掉的补上。

    回归保护：以前这里会走 first_poll=baseline 分支只记游标不投递，
    于是回退机制恰好丢掉了它最该救的那批事件。
    """
    config = make_config(github={"poll_mode": "auto", "first_poll": "baseline"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.remember_repo("acme/api", "webhook")
    await _backdate_webhook_seen(store, "acme/api", 18)
    try:
        # 断链期间真实发生的 3 件事（webhook 没送到）；head 各不相同，否则会被 dedup 当成同一事件
        respx.get(EVENTS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    api_push_event(head="aaa1", created_at=to_iso(utcnow() - timedelta(minutes=15))),
                    api_push_event(head="bbb2", created_at=to_iso(utcnow() - timedelta(minutes=10))),
                    api_push_event(head="ccc3", created_at=to_iso(utcnow() - timedelta(minutes=3))),
                ],
            )
        )
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.status == "ok"
    assert report.fetched == 3
    assert report.delivered == 3
    assert feishu.chats() == ["dev"] * 3
    state = await store.poll_state("acme/api")
    assert state is not None and state["cursor"] is not None


@respx.mock
async def test_fallback_does_not_duplicate_already_delivered_events(store):
    """断链期间其实没漏（webhook 已投递）时，补课抓到的会判为重复，不会重推。"""
    config = make_config(github={"poll_mode": "always", "first_poll": "baseline"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.remember_repo("acme/api", "webhook")
    await _backdate_webhook_seen(store, "acme/api", 18)

    webhook_event = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    assert webhook_event is not None
    service = NotificationService(config, store, feishu, Router(config))
    assert (await service.dispatch(webhook_event)).delivered == ["dev"]

    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event()]))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.new_events == 1
    assert report.delivered == 0
    assert report.skipped == 1
    assert feishu.chats() == ["dev"]  # 只有 webhook 那一次


@respx.mock
async def test_fallback_lookback_is_bounded(store):
    """断链很久时不能一次刷屏：只补 fallback_lookback_seconds 内的事件。"""
    config = make_config(github={"poll_mode": "always", "first_poll": "baseline", "fallback_lookback_seconds": 3600})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    await store.remember_repo("acme/api", "webhook")
    await _backdate_webhook_seen(store, "acme/api", 3 * 24 * 60)  # 3 天前断的
    try:
        respx.get(EVENTS_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    api_push_event(head="old", created_at=to_iso(utcnow() - timedelta(days=3))),
                    api_push_event(head="new", created_at=to_iso(utcnow() - timedelta(minutes=10))),
                ],
            )
        )
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.fetched == 2
    assert report.new_events == 1  # 3 天前那个被封顶挡掉
    assert report.delivered == 1


@respx.mock
async def test_genuinely_new_repo_still_only_baselines(store):
    """真的新仓库（从没见过 webhook）仍然不补历史，避免把 90 天事件倒进群里。"""
    config = make_config(github={"poll_mode": "always", "first_poll": "baseline"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json=[api_push_event()]))
        report = await poller.poll_repo("acme/api")
    finally:
        await github.aclose()

    assert report.status == "baseline"
    assert report.new_events == 0
    assert feishu.sent == []


async def test_target_repos_filters_unwatched(store):
    config = make_config()
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        await store.remember_repo("nobody/other", "webhook")
        await store.remember_repo("acme/web", "webhook")
        repos = await poller.target_repos()
    finally:
        await github.aclose()
    # nobody/other 没有订阅，不被轮询；acme/api 因为订阅里写死了所以在列表里
    assert "nobody/other" not in repos
    assert set(repos) == {"acme/web", "acme/api"}


async def test_poll_disabled_returns_no_reports(store):
    config = make_config(github={"poll_mode": "never"})
    feishu = RecordingFeishu()
    poller, github = await _build(config, store, feishu)
    try:
        assert await poller.poll_once() == []
        summary = poller.stats.last_poll_summary
        assert summary is not None and summary["status"] == "poll_disabled"
    finally:
        await github.aclose()


async def test_failed_delivery_is_retried_even_when_polling_is_skipped(store):
    """webhook 健康导致跳过轮询时，失败的投递仍必须被重试。"""
    config = make_config(github={"poll_mode": "auto"})
    feishu = RecordingFeishu(fail_chats={"dev"})
    poller, github = await _build(config, store, feishu)
    service = NotificationService(config, store, feishu, Router(config))

    event = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    assert event is not None
    assert [item.chat for item in (await service.dispatch(event)).failed] == ["dev"]

    feishu.fail_chats.clear()
    try:
        reports = await poller.poll_once()  # auto 模式下仓库会被跳过，但重试要跑
    finally:
        await github.aclose()

    assert [report.status for report in reports] == ["skipped_webhook_fresh"]
    assert feishu.chats() == ["dev"]
    assert await store.pending_retries() == []
    summary = poller.stats.last_poll_summary
    assert summary is not None and summary["retried"] == 1


@pytest.mark.parametrize("repo", ["acme/api"])
def test_repo_param_is_used(repo):
    assert repo == "acme/api"
