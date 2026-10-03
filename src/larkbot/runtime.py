"""运行时装配：把配置、状态库、GitHub 客户端、飞书客户端、服务层和轮询器接起来。

CLI 与 HTTP 服务共用这里，避免两套装配逻辑。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .commands import CommandHandler
from .config import Config, load_config
from .feishu.client import FeishuClient, FeishuError
from .feishu.protocols import ChatLister
from .github.client import GitHubClient
from .poller import Poller
from .router import Router
from .service import NotificationService
from .stats import Stats
from .store import DEFAULT_DB_PATH, StateStore

logger = logging.getLogger(__name__)


def resolve_db_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    return Path(os.environ.get("LARKBOT_DB") or DEFAULT_DB_PATH)


async def verify_app_chats(config: Config, feishu: ChatLister, *, max_pages: int = 5) -> dict[str, Any]:
    """联网验证自建应用通道：凭证能不能换到 token、配置的 chat_id 是不是机器人在的群。

    这一步能提前接住三类常见坑：应用没发版 -> 换不到 token；机器人没被拉进群；
    chat_id 抄错（把别的群的 ID 粘过来了）。
    """
    app_chats = [chat for chat in config.active_chats if chat.transport == "app"]
    report: dict[str, Any] = {
        "credentials_configured": bool(config.feishu.app_id and config.feishu.app_secret),
        "token_ok": None,
        "groups_total": 0,
        "groups": [],
        "chats": [],
        "problems": [],
    }
    if not report["credentials_configured"]:
        if app_chats:
            report["problems"].append(
                "有 transport=app 的群，但缺少 feishu.app_id / feishu.app_secret"
                "（env:FEISHU_APP_ID / env:FEISHU_APP_SECRET）"
            )
        return report

    try:
        groups = await feishu.list_all_chats(max_pages=max_pages)
    except FeishuError as exc:
        report["problems"].append(f"获取群列表失败（凭证/权限/发版问题）: {exc}")
        return report

    report["token_ok"] = True
    report["groups_total"] = len(groups)
    report["groups"] = [group.as_dict() for group in groups]
    by_id = {group.chat_id: group for group in groups}

    for chat in app_chats:
        found = by_id.get(chat.chat_id or "")
        report["chats"].append(
            {
                "name": chat.name,
                "chat_id": chat.chat_id,
                "found": found is not None,
                "group_name": found.name if found else None,
            }
        )
        if found is None:
            report["problems"].append(
                f"chat {chat.name!r}: chat_id={chat.chat_id} 不在机器人所在的群列表里"
                "（机器人可能没被拉进这个群，或 chat_id 写错了；跑 `larkbot chats` 看正确的 ID）"
            )
    if app_chats and not groups:
        report["problems"].append("机器人当前不在任何群里，先把应用机器人拉进目标群")
    return report


@dataclass
class BotRuntime:
    config: Config
    store: StateStore
    feishu: FeishuClient
    github: GitHubClient
    router: Router
    service: NotificationService
    poller: Poller
    commands: CommandHandler
    stats: Stats
    config_path: Path | None = None
    poll_task: asyncio.Task[None] | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    # --- 装配 / 销毁 ----------------------------------------------------
    @classmethod
    async def build(
        cls,
        config: Config | None = None,
        *,
        config_path: str | Path | None = None,
        store_path: str | Path | None = None,
        dry_run: bool = False,
        start_poller: bool = False,
        lenient: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> BotRuntime:
        resolved = config or load_config(config_path, lenient=lenient)
        store = StateStore(resolve_db_path(store_path))
        await store.open()
        await store.prune(delivery_ttl_days=resolved.delivery.ttl_days)

        feishu = FeishuClient(resolved.feishu, http=http_client)
        github = GitHubClient(
            resolved.github.token,
            base_url=resolved.github.api_base_url,
            timeout=resolved.github.request_timeout_seconds,
            http=http_client,
        )
        stats = Stats()
        router = Router(resolved)
        service = NotificationService(
            resolved,
            store,
            feishu,
            router,
            stats=stats,
            dry_run=dry_run,
            retry_limit=resolved.delivery.retry_limit,
            retry_max_age_minutes=resolved.delivery.retry_max_age_minutes,
        )
        poller = Poller(resolved, store, github, service, router, stats=stats)
        command_handler = CommandHandler(resolved, store, feishu, router, stats=stats)
        runtime = cls(
            config=resolved,
            store=store,
            feishu=feishu,
            github=github,
            router=router,
            service=service,
            poller=poller,
            commands=command_handler,
            stats=stats,
            config_path=Path(config_path)
            if config_path
            else (Path(resolved.source_path) if resolved.source_path else None),
        )
        # 只要 github.enabled 就起调度器：即使 poll_mode=never（不轮询 GitHub），
        # 它仍然负责重试失败的飞书投递。
        if start_poller and resolved.github.enabled:
            runtime.start_poller()
        return runtime

    def start_poller(self) -> None:
        if self.poll_task is None or self.poll_task.done():
            self._stop = asyncio.Event()
            self.poll_task = asyncio.create_task(self.poller.run_forever(self._stop), name="larkbot-poller")

    async def aclose(self) -> None:
        self._stop.set()
        if self.poll_task is not None:
            self.poll_task.cancel()
            # 关闭阶段：取消/异常都不影响后续清理
            with contextlib.suppress(BaseException):
                await self.poll_task
            self.poll_task = None
        await self.github.aclose()
        await self.feishu.aclose()
        await self.store.close()

    # --- 配置热加载 -----------------------------------------------------
    async def verify_app_chats(self, *, max_pages: int = 5) -> dict[str, Any]:
        return await verify_app_chats(self.config, self.feishu, max_pages=max_pages)

    def reload_config(self) -> Config:
        new_config = load_config(self.config_path)
        self.config = new_config
        self.service.set_config(new_config)
        self.poller.set_config(new_config)
        self.commands.set_config(new_config)
        self.router = self.service.router
        self.stats.config_reloads += 1
        logger.info("配置已重载: %s", new_config.source_path)
        return new_config

    # --- 状态汇总 -------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        return {
            "config": self.config.summary(),
            "stats": self.stats.as_dict(),
            "store": await self.store.stats(),
            "dynamic_subscriptions": await self.store.list_dynamic_subscriptions(),
            "repos": await self.store.repo_states(),
            "router": self.router.summary(),
            "poller_running": bool(self.poll_task and not self.poll_task.done()),
        }
