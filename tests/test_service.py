"""编排层：去重粒度、失败重试、dry-run。"""

from __future__ import annotations

from larkbot.fixtures import push_payload, release_payload
from larkbot.github.normalize import normalize_webhook
from larkbot.router import Router
from larkbot.service import NotificationService
from tests.conftest import RecordingFeishu


def _service(config, store, feishu) -> NotificationService:
    return NotificationService(config, store, feishu, Router(config))


async def test_delivers_to_matching_chat(config, store, feishu):
    service = _service(config, store, feishu)
    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None

    outcome = await service.dispatch(event)
    assert outcome.targets == ["dev"]
    assert outcome.delivered == ["dev"]
    assert feishu.chats() == ["dev"]
    assert outcome.failed == []


async def test_duplicate_from_poll_is_skipped(config, store, feishu):
    """同一条事件先由 webhook 投递，再由轮询拿到时必须被去重。"""
    service = _service(config, store, feishu)
    event = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    assert event is not None
    assert (await service.dispatch(event)).delivered == ["dev"]

    same_event = normalize_webhook("push", push_payload(repo="acme/api"), source="poll")
    assert same_event is not None
    second = await service.dispatch(same_event)
    assert second.delivered == []
    assert second.skipped == ["dev"]
    assert len(feishu.sent) == 1  # 没有重复发第二次


async def test_failed_chat_is_retried_without_resending_to_others(config, store):
    feishu = RecordingFeishu(fail_chats={"rel"})
    service = _service(config, store, feishu)
    event = normalize_webhook("release", release_payload(repo="acme/api"))
    assert event is not None

    first = await service.dispatch(event)
    assert first.targets == ["dev", "rel"]
    assert first.delivered == ["dev"]
    assert [item.chat for item in first.failed] == ["rel"]
    assert feishu.chats() == ["dev"]

    feishu.fail_chats.clear()
    second = await service.dispatch(event)
    assert second.delivered == ["rel"]  # 只补发失败的群
    assert second.skipped == ["dev"]  # 已成功的群不重复打扰
    assert feishu.chats() == ["dev", "rel"]


async def test_event_without_matching_chat_writes_nothing(config, store, feishu):
    service = _service(config, store, feishu)
    event = normalize_webhook("push", push_payload(repo="nobody/repo"))
    assert event is not None
    outcome = await service.dispatch(event)
    assert outcome.targets == []
    assert feishu.sent == []
    stats = await store.stats()
    assert stats["deliveries"] == {}
    assert stats["known_repos"] == 0
    assert stats["recent_failures"] == []
    assert stats["dynamic_subscriptions"] == []


async def test_dry_run_does_not_touch_store(config, store, feishu):
    service = NotificationService(config, store, feishu, Router(config), dry_run=True)
    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None
    outcome = await service.dispatch(event)
    assert outcome.delivered == ["dev"]
    assert feishu.sent == []
    assert await store.known_repos() == []


async def test_remember_repo_records_webhook_liveness(config, store, feishu):
    service = _service(config, store, feishu)
    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None
    await service.dispatch(event)
    assert await store.known_repos() == ["acme/api"]
    assert await store.webhook_last_seen_at("acme/api") is not None


async def test_failed_delivery_is_retried_from_snapshot(config, store):
    """失败投递会把事件快照存起来，重试不依赖 GitHub 重新拉取。"""
    feishu = RecordingFeishu(fail_chats={"dev"})
    service = _service(config, store, feishu)
    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None
    assert [item.chat for item in (await service.dispatch(event)).failed] == ["dev"]

    pending = await store.pending_retries()
    assert len(pending) == 1
    assert pending[0]["chat"] == "dev" and pending[0]["attempts"] == 1

    feishu.fail_chats.clear()
    outcomes = await service.retry_failed()
    assert [outcome.delivered for outcome in outcomes] == [["dev"]]
    assert await store.pending_retries() == []
    assert (await store.stats())["pending_retries"] == 0


async def test_retry_window_is_bounded(config, store):
    """首次失败超过重试窗口的记录不再重试，避免无限打。"""
    feishu = RecordingFeishu(fail_chats={"dev"})
    service = _service(config, store, feishu)
    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None
    await service.dispatch(event)

    assert await store.pending_retries(max_age_minutes=60) != []
    assert await store.pending_retries(max_age_minutes=-1) == []


async def test_retry_does_not_resend_to_successful_chats(config, store):
    feishu = RecordingFeishu(fail_chats={"rel"})
    service = _service(config, store, feishu)
    event = normalize_webhook("release", release_payload(repo="acme/api"))
    assert event is not None
    await service.dispatch(event)

    feishu.fail_chats.clear()
    outcomes = await service.retry_failed()
    assert outcomes[0].skipped == ["dev"]
    assert outcomes[0].delivered == ["rel"]
    assert feishu.chats() == ["dev", "rel"]


async def test_retry_reports_nothing_in_dry_run(config, store, feishu):
    service = NotificationService(config, store, feishu, Router(config), dry_run=True)
    assert await service.retry_failed() == []


async def test_send_test_unknown_chat_raises(config, store, feishu):
    service = _service(config, store, feishu)
    outcome = await service.send_test("dev")
    assert outcome.status == "delivered"
    assert feishu.chats() == ["dev"]

    import pytest

    with pytest.raises(ValueError, match="未找到 chat"):
        await service.send_test("nope")
