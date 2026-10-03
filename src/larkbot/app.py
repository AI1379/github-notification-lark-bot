"""FastAPI 应用：GitHub webhook 入口 + 健康检查 + 管理接口。"""

from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .commands import CommandHandler
from .config import Config, ConfigError
from .feishu.events import (
    CARD_ACTION_EVENT,
    MESSAGE_EVENT,
    EventError,
    challenge_response,
    is_challenge,
    parse_event,
    parse_message,
    parse_payload,
)
from .github.normalize import normalize_webhook
from .github.webhook import verify_signature
from .runtime import BotRuntime
from .util import setup_logging, utcnow

logger = logging.getLogger(__name__)


def extract_repo(payload: dict[str, Any]) -> str | None:
    repository = payload.get("repository")
    if isinstance(repository, dict) and repository.get("full_name"):
        return str(repository["full_name"])
    repo = payload.get("repo")
    if isinstance(repo, dict) and repo.get("name"):
        return str(repo["name"])
    return None


def create_app(
    config: Config | None = None,
    *,
    config_path: str | Path | None = None,
    store_path: str | Path | None = None,
    dry_run: bool = False,
    start_poller: bool | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = await BotRuntime.build(
            config,
            config_path=config_path,
            store_path=store_path,
            dry_run=dry_run,
            start_poller=(
                bool(start_poller) if start_poller is not None else os.environ.get("LARKBOT_NO_POLLER", "") != "1"
            ),
            http_client=http_client,
        )
        app.state.bot = runtime
        if runtime.config.used_example_file:
            logger.warning("正在使用示例配置 %s；请复制为 config/config.yaml 后再改。", runtime.config.source_path)
        logger.info(
            "larkbot %s 启动: 群=%s 订阅=%d 轮询=%s",
            __version__,
            [chat.name for chat in runtime.config.active_chats],
            len(runtime.config.subscriptions),
            runtime.config.github.poll_mode,
        )
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title="larkbot",
        version=__version__,
        description="GitHub -> 飞书通知机器人（webhook + 轮询回退，按群订阅仓库）",
        lifespan=lifespan,
    )
    app.include_router(_router())
    return app


def _bot(request: Request) -> BotRuntime:
    runtime = getattr(request.app.state, "bot", None)
    if runtime is None:  # pragma: no cover - 只在生命周期异常时出现
        raise HTTPException(status_code=503, detail="runtime 尚未就绪")
    return runtime


async def require_admin(request: Request) -> None:
    """校验管理接口凭证：``X-Admin-Token`` 或 ``Authorization: Bearer <token>``。"""
    bot = _bot(request)
    expected = bot.config.server.admin_token
    if not expected:
        raise HTTPException(status_code=403, detail="未配置 server.admin_token，管理接口已关闭")
    authorization = request.headers.get("authorization") or ""
    provided = request.headers.get("x-admin-token") or (
        authorization[7:] if authorization.lower().startswith("bearer ") else ""
    )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="admin token 校验失败")


def _router() -> APIRouter:
    api = APIRouter()

    @api.get("/")
    async def index(request: Request) -> dict[str, Any]:
        bot = _bot(request)
        return {
            "name": "larkbot",
            "version": __version__,
            "chats": [chat.name for chat in bot.config.active_chats],
            "subscriptions": len(bot.config.subscriptions),
            "poll_mode": bot.config.github.poll_mode,
            "endpoints": [
                "/healthz",
                "/status",
                "/webhooks/github",
                "/admin/reload",
                "/admin/poll",
                "/admin/test/{chat}",
            ],
        }

    @api.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        bot = _bot(request)
        return {
            "status": "ok",
            "time": utcnow().isoformat(),
            "poller_running": bool(bot.poll_task and not bot.poll_task.done()),
        }

    @api.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        return await _bot(request).status()

    @api.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
        x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        x_hub_signature: str | None = Header(default=None, alias="X-Hub-Signature"),
    ) -> dict[str, Any]:
        bot = _bot(request)
        stats = bot.stats
        stats.webhook_requests += 1

        body = await request.body()
        secret = bot.config.github.webhook_secret
        if secret:
            signature = x_hub_signature_256 or x_hub_signature
            if not verify_signature(secret, body, signature):
                stats.webhook_rejected += 1
                logger.warning("webhook 签名校验失败 (delivery=%s)", x_github_delivery)
                raise HTTPException(status_code=401, detail="签名校验失败")
        elif not bot.config.github.allow_unsigned_webhooks:
            # 没有 secret 就不校验签名；一旦这个端口能被外部访问，等于谁都能伪造事件
            # 往群里发消息。所以默认直接拒绝，要本地调试就显式打开 allow_unsigned_webhooks。
            stats.webhook_rejected += 1
            logger.error(
                "收到未签名的 webhook，但未配置 github.webhook_secret；已拒绝。"
                " 填好 GITHUB_WEBHOOK_SECRET，或（仅本地调试）设 github.allow_unsigned_webhooks: true"
            )
            raise HTTPException(status_code=503, detail="未配置 github.webhook_secret，拒绝处理未签名请求")
        else:
            logger.warning("allow_unsigned_webhooks=true：webhook 不校验签名（仅限本地调试）")

        event_name = x_github_event or "unknown"
        if event_name == "ping":
            stats.webhook_pings += 1
            return {"ok": True, "event": "ping", "delivery": x_github_delivery, "msg": "pong"}

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

        stats.last_webhook_at = utcnow()
        stats.last_webhook_event = event_name
        stats.last_webhook_delivery = x_github_delivery

        repo = extract_repo(payload)
        if repo:
            # 即使事件类型不处理，也要记录存活，供 poll_mode=auto 判断 webhook 是否健康
            await bot.store.remember_repo(repo, "webhook")

        event = normalize_webhook(event_name, payload, source="webhook")
        events = [event] if event is not None else []
        outcomes = await bot.service.handle(events, source="webhook")

        return {
            "ok": True,
            "event": event_name,
            "delivery": x_github_delivery,
            "repo": repo,
            "received": len(events),
            "delivered": sum(len(outcome.delivered) for outcome in outcomes),
            "skipped_duplicate": sum(len(outcome.skipped) for outcome in outcomes),
            "failed": sum(len(outcome.failed) for outcome in outcomes),
            "detail": [outcome.as_dict() for outcome in outcomes],
        }

    @api.post("/admin/reload")
    async def admin_reload(request: Request) -> dict[str, Any]:
        await require_admin(request)
        bot = _bot(request)
        try:
            config = bot.reload_config()
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "config": config.summary()}

    @api.post("/admin/poll")
    async def admin_poll(request: Request) -> dict[str, Any]:
        await require_admin(request)
        bot = _bot(request)
        reports = await bot.poller.poll_once()
        return {"ok": True, "reports": [report.as_dict() for report in reports]}

    @api.post("/admin/test/{chat_name}")
    async def admin_test(chat_name: str, request: Request) -> dict[str, Any]:
        await require_admin(request)
        bot = _bot(request)
        try:
            outcome = await bot.service.send_test(chat_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": outcome.status != "failed", "result": outcome.as_dict()}

    @api.post("/webhooks/feishu")
    async def feishu_callback(request: Request, background: BackgroundTasks) -> JSONResponse:
        """飞书事件订阅回调：challenge 校验 + 群内指令。

        飞书要求 **1 秒内**应答 challenge、**3 秒内**响应事件回调（超时会重推），
        所以这里只做校验与幂等登记，真正的处理丢给 background task 异步跑。
        """
        bot = _bot(request)
        stats = bot.stats
        stats.feishu_callbacks += 1
        body = await request.body()
        token = bot.config.feishu.verification_token

        try:
            payload = parse_payload(body)
            if is_challenge(payload):
                stats.feishu_challenges += 1
                return JSONResponse(challenge_response(payload, token))
            event_type, event_id, event = parse_event(payload, token)
        except EventError as exc:
            stats.feishu_rejected += 1
            logger.warning("飞书回调被拒绝(%s): %s", exc.code, exc)
            return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=exc.status)

        stats.last_callback_at = utcnow()
        stats.last_callback_type = event_type

        if not await bot.store.record_inbound_event(event_id, event_type):
            logger.debug("忽略重复推送的回调: %s", event_id)
            return JSONResponse({"code": 0, "msg": "duplicate ignored"})

        if event_type == MESSAGE_EVENT:
            message = parse_message(event, event_id=event_id)
            background.add_task(_run_command, bot.commands, message)
        elif event_type == CARD_ACTION_EVENT:
            logger.info("收到卡片交互回调（当前版本尚未处理按钮动作）: %s", event_id)
        else:
            logger.debug("未处理的事件类型: %s", event_type)
        return JSONResponse({"code": 0})

    return api


async def _run_command(handler: CommandHandler, message: object) -> None:
    """后台执行指令；异常只记日志，不能影响已返回的 200。"""
    try:
        await handler.handle(message)  # type: ignore[arg-type]
    except Exception:
        logger.exception("处理群内指令时出现未预期错误")
