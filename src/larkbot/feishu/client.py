"""飞书发送客户端：自定义机器人 webhook 与自建应用两种通道。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import __version__
from ..config import ChatConfig, FeishuConfig
from ..util import as_dict, as_list, scrub_url

logger = logging.getLogger(__name__)

#: tenant_access_token 过期时飞书返回的 code，收到后强制刷新并重试一次
TOKEN_EXPIRED_CODE = 99991663


@dataclass(slots=True)
class ChatSummary:
    """机器人所在的一个群（来自 ``GET /open-apis/im/v1/chats``）。"""

    chat_id: str
    name: str
    description: str | None = None
    member_count: int | None = None
    owner_id: str | None = None
    chat_mode: str | None = None
    external: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "name": self.name,
            "description": self.description,
            "member_count": self.member_count,
            "owner_id": self.owner_id,
            "chat_mode": self.chat_mode,
            "external": self.external,
        }


@dataclass(slots=True)
class ChatsPage:
    chats: list[ChatSummary] = field(default_factory=list)
    has_more: bool = False
    page_token: str | None = None


def _first_int(item: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, int):
            return value
    return None


def _to_chat_summary(item: dict[str, Any]) -> ChatSummary:
    """飞书返回的群名可能为空，用 chat_id 兑底以便辨识。"""
    chat_id = str(item.get("chat_id") or "")
    name = item.get("name") or f"(未命名群 {chat_id[-8:]})"
    description = item.get("description")
    return ChatSummary(
        chat_id=chat_id,
        name=str(name),
        description=str(description) if description else None,
        # 不同接口版本的字段名不一致，两个都试
        member_count=_first_int(item, "user_count", "member_count"),
        owner_id=item.get("owner_id"),
        chat_mode=item.get("chat_mode"),
        external=item.get("external") if isinstance(item.get("external"), bool) else None,
    )


def _slash_command_body(
    command: str | None,
    description: str | None,
    i18n: dict[str, str] | None,
    icon_key: str | None,
    *,
    partial: bool = False,
) -> dict[str, Any]:
    """拼 Slash Command 请求体；``partial=True`` 时只带确实提供的字段（PATCH 用）。"""
    body: dict[str, Any] = {}
    if command is not None:
        body["command"] = command.lstrip("/")  # 接口不带前导斜杠
    if description is not None:
        text: dict[str, Any] = {"default_value": description}
        if i18n:
            text["i18n"] = {key: value for key, value in i18n.items() if value}
        body["description"] = text
    if icon_key:
        body["icon"] = {"icon_key": icon_key}
    return body


#: 自定义机器人 webhook 的签名算法（官方文档：key 是 "timestamp\\nsecret"，内容为空）
def webhook_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        chat: str | None = None,
        code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.chat = chat
        self.code = code
        self.retryable = retryable


class FeishuClient:
    def __init__(
        self,
        config: FeishuConfig | None = None,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or FeishuConfig()
        self._owns_client = http is None
        self._http = http or httpx.AsyncClient(
            timeout=self.config.request_timeout_seconds,
            headers={"User-Agent": f"larkbot/{__version__}"},
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> FeishuClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # --- 发送入口 -------------------------------------------------------
    async def send_card(self, chat: ChatConfig, card: dict[str, Any]) -> dict[str, Any]:
        if chat.transport == "webhook":
            return await self._post_webhook(chat, {"msg_type": "interactive", "card": card})
        return await self._send_app(chat, "interactive", json.dumps(card, ensure_ascii=False))

    async def send_text(self, chat: ChatConfig, text: str) -> dict[str, Any]:
        if chat.transport == "webhook":
            return await self._post_webhook(chat, {"msg_type": "text", "content": {"text": text}})
        return await self._send_app(chat, "text", json.dumps({"text": text}, ensure_ascii=False))

    # --- 通道 1：自定义机器人 webhook -----------------------------------
    async def _post_webhook(self, chat: ChatConfig, body: dict[str, Any]) -> dict[str, Any]:
        if not chat.webhook_url:
            raise FeishuError(f"chat {chat.name!r} 未配置 webhook_url", chat=chat.name)
        payload = dict(body)
        if chat.webhook_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = webhook_sign(chat.webhook_secret, timestamp)
        response = await self._request("POST", chat.webhook_url, json=payload, chat=chat.name)
        data = self._parse_json(response, chat=chat.name)
        self._raise_on_api_error(data, chat=chat.name, transport="webhook")
        return data

    # --- 通道 2：自建应用 im/v1 -----------------------------------------
    async def tenant_access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            if not (self.config.app_id and self.config.app_secret):
                raise FeishuError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，无法使用 transport=app")
            url = f"{self.config.base_url.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal"
            response = await self._request(
                "POST",
                url,
                json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                chat=None,
            )
            data = self._parse_json(response, chat=None)
            if data.get("code") != 0:
                raise FeishuError(
                    f"获取 tenant_access_token 失败: code={data.get('code')} msg={data.get('msg')}",
                    code=data.get("code"),
                )
            token = data.get("tenant_access_token")
            if not token:
                # 外面拿到的任何异常都不该是 KeyError，这里把非预期响应当成错误报出去
                raise FeishuError(f"获取 tenant_access_token 的响应缺少该字段: {data}")
            self._token = str(token)
            # 官方默认 7200s，提前 60s 过期
            self._token_expires_at = time.monotonic() + max(int(data.get("expire", 7200)) - 60, 60)
            return self._token

    async def _send_app(self, chat: ChatConfig, msg_type: str, content: str) -> dict[str, Any]:
        if not chat.chat_id:
            raise FeishuError(f"chat {chat.name!r} 未配置 chat_id", chat=chat.name)
        url = f"{self.config.base_url.rstrip('/')}/open-apis/im/v1/messages?receive_id_type=chat_id"
        return await self._call_app_api(
            "POST",
            url,
            chat=chat.name,
            json={"receive_id": chat.chat_id, "msg_type": msg_type, "content": content},
        )

    # --- 通道 2 的辅助能力：列出机器人在的群 --------------------------------
    async def list_chats(self, *, page_size: int = 100, page_token: str | None = None) -> ChatsPage:
        """列出机器人所在的群（不含单聊），用于获取 `chat_id`。

        需要应用开启「机器人」能力 + 群组信息读取权限（`im:chat:readonly` / `im:chat`），
        且权限变更后必须重新发布应用版本才生效。
        """
        url = f"{self.config.base_url.rstrip('/')}/open-apis/im/v1/chats"
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        data = await self._call_app_api("GET", url, chat=None, params=params)
        payload = as_dict(data.get("data"))
        items = as_list(payload.get("items"))
        return ChatsPage(
            chats=[_to_chat_summary(item) for item in items if isinstance(item, dict)],
            has_more=bool(payload.get("has_more")),
            page_token=payload.get("page_token"),
        )

    async def list_all_chats(self, *, max_pages: int = 5, page_size: int = 100) -> list[ChatSummary]:
        """翻页取回机器人在的全部群。"""
        collected: list[ChatSummary] = []
        page_token: str | None = None
        for _ in range(max(max_pages, 1)):
            page = await self.list_chats(page_size=page_size, page_token=page_token)
            collected.extend(page.chats)
            if not page.has_more or not page.page_token:
                break
            page_token = page.page_token
        return collected

    async def _call_app_api(
        self,
        method: str,
        url: str,
        *,
        chat: str | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """带 tenant_access_token 的调用；token 失效时自动刷新重试一次。"""
        token = await self.tenant_access_token()
        response = await self._request(method, url, headers={"Authorization": f"Bearer {token}"}, chat=chat, **kwargs)
        data = self._parse_json(response, chat=chat)
        if data.get("code") == TOKEN_EXPIRED_CODE:
            logger.info("tenant_access_token 已失效，刷新后重试")
            token = await self.tenant_access_token(force=True)
            response = await self._request(
                method, url, headers={"Authorization": f"Bearer {token}"}, chat=chat, **kwargs
            )
            data = self._parse_json(response, chat=chat)
        self._raise_on_api_error(data, chat=chat, transport="app")
        return data

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> dict[str, Any]:
        """以应用身份回复某条消息（挂在指令下面，不刷屏）。"""
        url = f"{self.config.base_url.rstrip('/')}/open-apis/im/v1/messages/{message_id}/reply"
        return await self._call_app_api(
            "POST",
            url,
            chat=None,
            json={"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        )

    async def get_chat_owner(self, chat_id: str) -> str | None:
        """获取群主的 open_id（用于"群主可以管理自己群的订阅"校验）。"""
        url = f"{self.config.base_url.rstrip('/')}/open-apis/im/v1/chats/{chat_id}"
        data = await self._call_app_api("GET", url, chat=None, params={"user_id_type": "open_id"})
        payload = as_dict(data.get("data"))
        owner = payload.get("owner_id")
        return str(owner) if owner else None

    # --- Slash Command（把指令注册到客户端的 `/` 面板）-----------------------
    # 接口：/open-apis/application/v7/app_slash_commands
    # 权限：application:app_slash_command:write（创建/更新/删除）、:read（列表）
    # 两个从官方源码确认的细节：command 不带前导 `/`；icon 在请求体里与 description **平级**
    # （官方 create 示例把 icon 嵌在 description 里是文档笔误，larksuite/cli 实测已钉死）。
    async def list_slash_commands(self) -> list[dict[str, Any]]:
        url = f"{self.config.base_url.rstrip('/')}/open-apis/application/v7/app_slash_commands"
        data = await self._call_app_api("GET", url, chat=None)
        payload = as_dict(data.get("data"))
        items = as_list(payload.get("items"))
        return [dict(item) for item in items if isinstance(item, dict)]

    async def create_slash_command(
        self,
        command: str,
        *,
        description: str,
        i18n: dict[str, str] | None = None,
        icon_key: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}/open-apis/application/v7/app_slash_commands"
        return await self._call_app_api(
            "POST", url, chat=None, json=_slash_command_body(command, description, i18n, icon_key)
        )

    async def update_slash_command(
        self,
        command_id: str,
        *,
        command: str | None = None,
        description: str | None = None,
        i18n: dict[str, str] | None = None,
        icon_key: str | None = None,
    ) -> dict[str, Any]:
        """PATCH 是字段级部分更新：顶层未传的字段服务端保留，但 i18n 传了就整张覆盖。"""
        url = f"{self.config.base_url.rstrip('/')}/open-apis/application/v7/app_slash_commands/{command_id}"
        body = _slash_command_body(command, description, i18n, icon_key, partial=True)
        return await self._call_app_api("PATCH", url, chat=None, json=body)

    async def delete_slash_command(self, command_id: str) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}/open-apis/application/v7/app_slash_commands/{command_id}"
        return await self._call_app_api("DELETE", url, chat=None)

    # --- 公共请求/解析 ---------------------------------------------------
    async def _request(self, method: str, url: str, *, chat: str | None, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(2):  # 网络抖动/5xx 重试一次
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                # webhook URL 里的 token 就是凭据，异常文本可能带完整 URL，先脱敏
                last_error = FeishuError(
                    f"请求飞书失败: {scrub_url(str(exc) or exc.__class__.__name__, url)}",
                    chat=chat,
                    retryable=True,
                )
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise last_error from exc
            if response.status_code == 429 or response.status_code >= 500:
                last_error = FeishuError(
                    f"飞书返回 HTTP {response.status_code}: {scrub_url(response.text[:200], url)}",
                    chat=chat,
                    code=response.status_code,
                    retryable=True,
                )
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                raise last_error
            return response
        raise FeishuError(f"请求飞书失败: {last_error}", chat=chat, retryable=True)

    @staticmethod
    def _parse_json(response: httpx.Response, *, chat: str | None) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise FeishuError(
                f"飞书返回非 JSON 响应 (HTTP {response.status_code}): {response.text[:200]}",
                chat=chat,
                retryable=response.status_code >= 500,
            ) from exc
        if not isinstance(data, dict):
            raise FeishuError(f"飞书返回了非预期结构: {type(data).__name__}", chat=chat)
        return data

    @staticmethod
    def _raise_on_api_error(data: dict[str, Any], *, chat: str | None, transport: str) -> None:
        code = data.get("code")
        if code is None:
            code = data.get("StatusCode")  # 自定义机器人老版本字段
        if code == 0:
            return
        raise FeishuError(
            f"飞书接口返回错误 (transport={transport}): code={code} msg={data.get('msg') or data.get('StatusMessage')}",
            chat=chat,
            code=code if isinstance(code, int) else None,
        )
