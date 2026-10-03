"""编排层：去重粒度、失败重试、dry-run。"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from larkbot.fixtures import push_payload, release_payload
from larkbot.github.normalize import normalize_webhook
from larkbot.router import Router
from larkbot.service import NotificationService
from larkbot.util import to_iso, utcnow
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


class SlowFeishu(RecordingFeishu):
    """在发送中间让出控制权，制造并发窗口（真实场景里两个 webhook 相差 11ms 到达）。"""

    async def send_card(self, chat, card):
        await asyncio.sleep(0.01)
        return await super().send_card(chat, card)


async def test_concurrent_dispatch_sends_only_once(config, store):
    """回归保护：同一次 GitHub 事件被两个 webhook 同时投递（或 webhook 与轮询撞车）时，

    只能发一条消息。修之前去重是「先查 → 再发 → 再写记录」，两个请求会在对方写记录前
    双双通过检查，用户看到重复消息（实测日志里相差 11ms）。
    """
    feishu = SlowFeishu()
    service = NotificationService(config, store, feishu, Router(config))
    first = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    second = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    assert first is not None and second is not None
    assert first.dedup_key == second.dedup_key

    results = await asyncio.gather(service.dispatch(first), service.dispatch(second))

    assert feishu.chats() == ["dev"]  # 只发了一次
    assert sum(len(r.delivered) for r in results) == 1
    assert sum(len(r.skipped) for r in results) == 1


async def test_dedup_relies_on_atomic_claim_not_the_fast_path_read(config, store, monkeypatch):
    """把快速路径的读直接置空（模拟并发窗口），仍然只能发一次 —— 证明防线在 claim 上。"""
    feishu = SlowFeishu()
    service = NotificationService(config, store, feishu, Router(config))

    async def always_empty(dedup_key: str) -> set[str]:
        return set()

    monkeypatch.setattr(store, "delivered_chats", always_empty)

    event = normalize_webhook("push", push_payload(repo="acme/api"), source="webhook")
    assert event is not None
    await asyncio.gather(service.dispatch(event), service.dispatch(event))
    assert feishu.chats() == ["dev"]


async def test_pending_claim_blocks_concurrent_sender(config, store, feishu):
    """正在发送中的 pending 不能被另一个请求抢占。"""
    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None
    assert await store.claim_delivery(event, "dev") is True
    assert await store.claim_delivery(event, "dev") is False


async def test_stale_pending_can_be_reclaimed(config, store, feishu):
    """进程在投递中途被杀会留下 pending；超过 TTL 后应允许被重新抢占（否则这条永久卡死）。"""
    from larkbot.store import PENDING_CLAIM_TTL_SECONDS

    event = normalize_webhook("push", push_payload(repo="acme/api"))
    assert event is not None
    assert await store.claim_delivery(event, "dev") is True
    await store.connection.execute(
        "UPDATE deliveries SET updated_at=? WHERE dedup_key=? AND chat='dev'",
        (to_iso(utcnow() - timedelta(seconds=PENDING_CLAIM_TTL_SECONDS + 60)), event.dedup_key),
    )
    await store.connection.commit()
    assert await store.claim_delivery(event, "dev") is True


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
