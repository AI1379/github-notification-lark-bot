"""编排层：去重 -> 路由 -> 渲染 -> 投递。

去重语义是 ``(事件, 群)`` 粒度：
- 同一事件被 webhook 和轮询各拿到一次，只会推送一次；
- 某个群投递失败，下一次重试只会补发给失败的群，不会打扰已经收到的群。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import Config
from .feishu.cards import build_event_card, build_test_card
from .feishu.client import FeishuError
from .feishu.protocols import CardSender
from .models import RepoEvent
from .router import Router
from .stats import Stats
from .store import StateStore
from .util import truncate

logger = logging.getLogger(__name__)

ChatStatus = Literal["delivered", "skipped_duplicate", "failed", "dry_run"]


@dataclass(slots=True)
class ChatOutcome:
    chat: str
    status: ChatStatus
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"chat": self.chat, "status": self.status, "error": self.error}


@dataclass(slots=True)
class DispatchOutcome:
    repo: str
    kind: str
    action: str | None
    dedup_key: str
    source: str
    targets: list[str] = field(default_factory=list)
    results: list[ChatOutcome] = field(default_factory=list)

    @property
    def delivered(self) -> list[str]:
        return [r.chat for r in self.results if r.status in ("delivered", "dry_run")]

    @property
    def skipped(self) -> list[str]:
        return [r.chat for r in self.results if r.status == "skipped_duplicate"]

    @property
    def failed(self) -> list[ChatOutcome]:
        return [r for r in self.results if r.status == "failed"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "kind": self.kind,
            "action": self.action,
            "source": self.source,
            "dedup_key": self.dedup_key,
            "targets": self.targets,
            "results": [r.as_dict() for r in self.results],
        }


class NotificationService:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        feishu: CardSender,
        router: Router | None = None,
        *,
        stats: Stats | None = None,
        dry_run: bool = False,
        retry_limit: int = 20,
        retry_max_age_minutes: int = 60,
    ) -> None:
        self.config = config
        self.store = store
        self.feishu = feishu
        self.router = router or Router(config)
        self.stats = stats or Stats()
        self.dry_run = dry_run
        self.retry_limit = retry_limit
        self.retry_max_age_minutes = retry_max_age_minutes

    def set_config(self, config: Config) -> None:
        """/admin/reload 用：热替换配置与路由。"""
        self.config = config
        self.router = Router(config)

    async def handle(self, events: Sequence[RepoEvent], *, source: str | None = None) -> list[DispatchOutcome]:
        outcomes: list[DispatchOutcome] = []
        for event in events:
            if source:
                event.source = source  # type: ignore[assignment]
            try:
                outcomes.append(await self.dispatch(event))
            except Exception:  # 单条事件失败不能影响同批次其它事件
                logger.exception("处理事件失败: %s", event.to_log())
                self.stats.failed += 1
        return outcomes

    async def dispatch(self, event: RepoEvent) -> DispatchOutcome:
        self.stats.events_normalized += 1
        dynamic = await self.store.dynamic_subscriptions()
        targets = self.router.resolve(event, dynamic)
        outcome = DispatchOutcome(
            repo=event.repo,
            kind=event.kind,
            action=event.action,
            dedup_key=event.dedup_key,
            source=event.source,
            targets=[chat.name for chat in targets],
        )

        if not targets:
            logger.debug("没有匹配的群，跳过: %s", event.to_log())
            return outcome

        if self.dry_run:
            logger.info("[dry-run] %s -> %s", event.to_log(), outcome.targets)
            outcome.results = [ChatOutcome(chat.name, "dry_run") for chat in targets]
            return outcome

        await self.store.remember_repo(event.repo, event.source)
        # 快速路径：先把已成功的排掉，省掉一次写；但它只是优化，
        # 真正决定「该不该发」的是下面 claim_delivery 的原子抢占。
        already = await self.store.delivered_chats(event.dedup_key)
        card: dict[str, Any] | None = None

        for chat in targets:
            if chat.name in already or not await self.store.claim_delivery(event, chat.name):
                logger.debug("事件已投递或在投递中，跳过: %s -> %s", event.dedup_key, chat.name)
                outcome.results.append(ChatOutcome(chat.name, "skipped_duplicate"))
                self.stats.skipped_duplicate += 1
                continue
            if card is None:
                card = build_event_card(event, timezone=self.config.server.timezone)
            try:
                await self.feishu.send_card(chat, card)
            except FeishuError as exc:
                logger.warning("推送到 %s 失败: %s", chat.name, exc)
                await self.store.finish_delivery(event, chat.name, status="failed", error=str(exc))
                outcome.results.append(ChatOutcome(chat.name, "failed", truncate(str(exc), 200)))
                self.stats.failed += 1
            except Exception as exc:  # 兜底，避免单群异常拖垮整批
                logger.exception("推送到 %s 出现未预期错误", chat.name)
                await self.store.finish_delivery(event, chat.name, status="failed", error=str(exc))
                outcome.results.append(ChatOutcome(chat.name, "failed", truncate(str(exc), 200)))
                self.stats.failed += 1
            else:
                await self.store.finish_delivery(event, chat.name, status="ok")
                outcome.results.append(ChatOutcome(chat.name, "delivered"))
                self.stats.delivered += 1
                logger.info("已推送 %s -> %s", event.to_log(), chat.name)

        return outcome

    async def retry_failed(
        self, *, limit: int | None = None, max_age_minutes: int | None = None
    ) -> list[DispatchOutcome]:
        """重试之前投递失败的事件（快照存在状态库里，不依赖 GitHub 再拉一次）。

        因为去重是 ``(事件, 群)`` 粒度，重试只会补发给当时失败的群。
        """
        if self.dry_run:
            return []
        pending = await self.store.pending_retries(
            limit=self.retry_limit if limit is None else limit,
            max_age_minutes=self.retry_max_age_minutes if max_age_minutes is None else max_age_minutes,
        )
        outcomes: list[DispatchOutcome] = []
        for row in pending:
            try:
                event = RepoEvent.from_snapshot(json.loads(row["payload"]))
            except (TypeError, ValueError):
                logger.warning("重试记录无法反序列化，跳过: %s/%s", row.get("dedup_key"), row.get("chat"))
                continue
            logger.info("重试失败投递: %s -> %s (第 %s 次)", event.to_log(), row.get("chat"), row.get("attempts"))
            outcomes.append(await self.dispatch(event))
        return outcomes

    async def send_test(self, chat_name: str, *, details: dict[str, Any] | None = None) -> ChatOutcome:
        chat = self.config.chat(chat_name)
        if chat is None:
            raise ValueError(f"未找到 chat: {chat_name}")
        card = build_test_card(
            chat.name,
            timezone=self.config.server.timezone,
            details=details or {"transport": chat.transport},
        )
        if self.dry_run:
            return ChatOutcome(chat.name, "dry_run")
        try:
            await self.feishu.send_card(chat, card)
        except FeishuError as exc:
            return ChatOutcome(chat.name, "failed", str(exc))
        return ChatOutcome(chat.name, "delivered")
