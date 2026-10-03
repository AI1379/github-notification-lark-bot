"""轮询回退：拿 GitHub REST Events API 补齐 webhook 漏掉的事件。

设计要点：
- **游标 + 回溯窗口**：游标存「已处理的最新事件时间」，每次往前多看
  ``poll_overlap_seconds``，重复部分靠 dedup 兜住，避免边界丢事件。
- **ETag 条件请求**：内容没变直接 304，几乎不吃 API 限额。
- **webhook 健康时跳过**：``poll_mode=auto`` 下，最近收到过 webhook 就不轮询该仓库。
- **首次见到仓库默认只打基线**：不把历史事件一股脑推给群。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .config import Config
from .github.client import GitHubClient, GitHubError
from .github.normalize import normalize_api_event
from .models import RepoEvent
from .router import Router
from .service import NotificationService
from .stats import Stats
from .store import StateStore
from .util import parse_ts, to_iso, utcnow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RepoPollReport:
    repo: str
    status: str  # ok / no_change / baseline / skipped_webhook_fresh / skipped_unwatched / error / disabled
    fetched: int = 0
    new_events: int = 0
    delivered: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None
    detail: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "status": self.status,
            "fetched": self.fetched,
            "new_events": self.new_events,
            "delivered": self.delivered,
            "skipped": self.skipped,
            "failed": self.failed,
            "error": self.error,
        }


class Poller:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        github: GitHubClient,
        service: NotificationService,
        router: Router | None = None,
        *,
        stats: Stats | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.github = github
        self.service = service
        self.router = router or service.router
        self.stats = stats or service.stats

    def set_config(self, config: Config) -> None:
        self.config = config
        self.router = Router(config)

    # --- 调度 -----------------------------------------------------------
    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info(
            "轮询调度器启动: poll_mode=%s interval=%ss（含失败投递重试）",
            self.config.github.poll_mode,
            self.config.github.poll_interval_seconds,
        )
        while not stop.is_set():
            try:
                reports = await self.poll_once()
                if reports:
                    summary = ", ".join(f"{r.repo}={r.status}" for r in reports)
                    logger.info("轮询完成: %s", summary)
            except Exception:
                logger.exception("轮询周期异常")
                self.stats.poll_errors += 1
            interval = self.config.github.poll_interval_seconds
            jitter = interval * 0.1
            delay = max(interval + random.uniform(-jitter, jitter), 5.0)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def poll_once(self) -> list[RepoPollReport]:
        self.stats.poll_cycles += 1
        self.stats.last_poll_at = utcnow()

        # 先把上一轮失败的投递补上：这一步不依赖 GitHub，因此在「webhook 健康所以
        # 跳过轮询」的情况下也会执行，避免 webhook 触发的失败投递永远没人管。
        retried = await self.service.retry_failed()
        retry_delivered = sum(len(outcome.delivered) for outcome in retried)
        retry_failed = sum(len(outcome.failed) for outcome in retried)
        if retried:
            logger.info("重试投递: 成功 %d，仍失败 %d", retry_delivered, retry_failed)

        reports: list[RepoPollReport] = []
        if not self.config.github.enabled or self.config.github.poll_mode == "never":
            self.stats.last_poll_summary = {
                "status": "poll_disabled",
                "retried": retry_delivered,
                "retry_failed": retry_failed,
            }
            return reports
        for repo in await self.target_repos():
            report = await self.poll_repo(repo)
            reports.append(report)
            if report.status == "error":
                self.stats.poll_errors += 1
        self.stats.last_poll_summary = {
            "repos": len(reports),
            "delivered": sum(r.delivered for r in reports),
            "failed": sum(r.failed for r in reports),
            "errors": [r.repo for r in reports if r.status == "error"],
            "retried": retry_delivered,
            "retry_failed": retry_failed,
        }
        return reports

    async def target_repos(self) -> list[str]:
        """需要轮询的仓库 = 已知仓库 ∪ 显式 poll_repos ∪ 订阅里写死的仓库（含指令创建的）。"""
        dynamic = await self.store.dynamic_subscriptions()
        candidates: list[str] = []
        for repo in [
            *self.config.github.poll_repos,
            *await self.store.known_repos(),
            *self.router.concrete_repos(dynamic),
        ]:
            if repo and repo not in candidates:
                candidates.append(repo)
        return [repo for repo in candidates if self.router.is_watched_repo(repo, dynamic)]

    # --- 单仓库轮询 -----------------------------------------------------
    async def poll_repo(self, repo: str) -> RepoPollReport:
        cfg = self.config.github
        report = RepoPollReport(repo=repo, status="ok")
        now = utcnow()

        last_webhook = await self._webhook_last_seen(repo)
        if cfg.poll_mode == "auto" and last_webhook is not None:
            age = (now - last_webhook).total_seconds()
            if age < cfg.webhook_freshness_seconds:
                logger.debug("%s 最近 %.0fs 收到过 webhook，跳过轮询", repo, age)
                return RepoPollReport(repo=repo, status="skipped_webhook_fresh")

        state = await self.store.poll_state(repo) or {}
        cursor = parse_ts(state.get("cursor"))
        etag = state.get("etag") if isinstance(state.get("etag"), str) else None

        # 首次轮询（没有游标）分两种情形，回溯起点完全不同：
        #  ① recovering：此仓库以前一直靠 webhook 投递（所以 poll_state 里没游标），
        #     现在 webhook 不新鲜了 → 从「最后一次确认它活着」开始补。
        #     不补的话，断链那段时间的事件会永久丢失，回退就失去了意义。
        #  ② 真正的全新仓库 → 按 first_poll 策略（默认 baseline，不把 90 天历史倒进群里）。
        recovering = cursor is None and last_webhook is not None
        if cursor is not None:
            floor = cursor - timedelta(seconds=cfg.poll_overlap_seconds)
        elif last_webhook is not None:  # 再判一次，顺便让类型检查器收窄
            floor = max(
                last_webhook - timedelta(seconds=cfg.poll_overlap_seconds),
                now - timedelta(seconds=cfg.fallback_lookback_seconds),
            )
        else:
            floor = now - timedelta(seconds=cfg.first_poll_lookback_seconds)

        try:
            events, new_etag, not_modified = await self._fetch(repo, etag=etag, floor=floor)
        except GitHubError as exc:
            await self.store.save_poll_state(repo, status="error", error=str(exc))
            logger.warning("轮询 %s 失败: %s", repo, exc)
            return RepoPollReport(repo=repo, status="error", error=str(exc))

        if not_modified:
            await self.store.save_poll_state(repo, etag=new_etag, status="no_change")
            return RepoPollReport(repo=repo, status="no_change")

        report.fetched = len(events)
        dated = [(parse_ts(item.get("created_at")), item) for item in events]
        dated = [(when, item) for when, item in dated if item]
        newest = max((when for when, _ in dated if when), default=None)

        if cursor is None and not recovering and cfg.first_poll == "baseline":
            await self.store.save_poll_state(repo, etag=new_etag, cursor=to_iso(newest), status="baseline")
            logger.info("首次轮询 %s，只记录基线游标 (%s)，不补推历史事件", repo, to_iso(newest))
            return RepoPollReport(repo=repo, status="baseline", fetched=report.fetched)
        if recovering:
            logger.info("%s 从 webhook 回退到轮询，补推 %s 之后的动静", repo, to_iso(floor))

        fresh = [(when, item) for when, item in dated if when is None or when >= floor]
        fresh.sort(key=lambda pair: pair[0] or utcnow())

        normalized: list[RepoEvent] = []
        for _, item in fresh:
            event = normalize_api_event(item, source="poll")
            if event is not None:
                normalized.append(event)

        report.new_events = len(normalized)
        if normalized:
            outcomes = await self.service.handle(normalized, source="poll")
            for outcome in outcomes:
                report.delivered += len(outcome.delivered)
                report.skipped += len(outcome.skipped)
                report.failed += len(outcome.failed)
                report.detail.append(outcome.as_dict())

        next_cursor = max(
            [when for when, _ in dated if when] + ([cursor] if cursor else []),
            default=None,
        )
        status = "error" if report.failed else "ok"
        error = None
        if report.failed:
            error = f"{report.failed} 个群投递失败，下轮会重试"
        await self.store.save_poll_state(
            repo,
            etag=new_etag,
            cursor=to_iso(next_cursor),
            status=status,
            error=error,
        )
        if report.failed:
            report.error = error
        return report

    async def _fetch(self, repo: str, *, etag: str | None, floor: Any) -> tuple[list[dict[str, Any]], str | None, bool]:
        cfg = self.config.github
        collected: list[dict[str, Any]] = []
        current_etag = etag
        for page_number in range(1, max(cfg.max_pages, 1) + 1):
            page = await self.github.repo_events(
                repo,
                etag=current_etag if page_number == 1 else None,
                per_page=cfg.per_page,
                page=page_number,
            )
            if page.not_modified:
                return [], page.etag or etag, True
            if page_number == 1:
                current_etag = page.etag
            collected.extend(page.events)
            if not page.page_full:
                break
            times = [when for when in (parse_ts(item.get("created_at")) for item in page.events) if when]
            if times and floor is not None and min(times) < floor:
                break
        return collected, current_etag, False

    async def _webhook_last_seen(self, repo: str):
        return parse_ts(await self.store.webhook_last_seen_at(repo))
