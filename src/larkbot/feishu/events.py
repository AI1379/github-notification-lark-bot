"""飞书事件回调的解析与校验。

支持的是「将事件发送至开发者服务器」模式（HTTP 回调）。两种订阅方式的选择：

- **长连接**：靠飞书 SDK 建 WebSocket，不需要公网地址也不需解密，但要多一个 SDK 依赖；
- **回调地址**（本模块）：复用已有的公网入口，只需处理 challenge 校验与 Verification Token。

未实现：Encrypt Key 的 AES 解密。如果开发者后台配了加密策略，这里会明确报错而不是静默失败。
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..util import as_dict

logger = logging.getLogger(__name__)

CHALLENGE_TYPE = "url_verification"
MESSAGE_EVENT = "im.message.receive_v1"
CARD_ACTION_EVENT = "card.action.trigger"


class EventError(RuntimeError):
    """回调格式不合法 / 来源校验失败。"""

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_event") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(slots=True)
class IncomingMessage:
    """``im.message.receive_v1`` 里我们关心的一部分字段。"""

    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    sender_open_id: str | None
    sender_type: str
    text: str
    mentions: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def is_from_bot(self) -> bool:
        return self.sender_type == "bot"


def parse_payload(body: bytes | str) -> dict[str, Any]:
    """解析回调体；遇到加密体直接给出可操作的报错。"""
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EventError(f"回调体不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EventError("回调体必须是 JSON 对象")
    if "encrypt" in payload:
        raise EventError(
            "收到加密回调，但当前版本不支持 Encrypt Key 的 AES 解密；"
            "请在开发者后台「事件与回调 → 加密策略」里清空 Encrypt Key（或留空）后重试",
            code="encrypt_unsupported",
        )
    return payload


def is_challenge(payload: dict[str, Any]) -> bool:
    return payload.get("type") == CHALLENGE_TYPE or "challenge" in payload


def verify_token(payload: dict[str, Any], expected: str | None) -> None:
    """校验 Verification Token（v2.0 在 header.token，v1.0 在顶层 token）。"""
    if not expected:
        return
    header = as_dict(payload.get("header"))
    provided = payload.get("token") or header.get("token")
    if not isinstance(provided, str) or not hmac.compare_digest(provided, expected):
        raise EventError("Verification Token 校验失败（配置里的值与开发者后台不一致）", status=401, code="bad_token")


def challenge_response(payload: dict[str, Any], expected: str | None) -> dict[str, Any]:
    verify_token(payload, expected)
    challenge = payload.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise EventError("challenge 校验请求缺少 challenge 字段")
    return {"challenge": challenge}


def parse_event(payload: dict[str, Any], expected: str | None) -> tuple[str, str, dict[str, Any]]:
    """返回 ``(event_type, event_id, event)``，同时完成来源校验。"""
    verify_token(payload, expected)
    header = as_dict(payload.get("header"))
    event_type = header.get("event_type") or payload.get("type")
    event_id = header.get("event_id") or payload.get("uuid") or ""
    if not isinstance(event_type, str) or not event_type:
        raise EventError("回调缺少 header.event_type")
    if not event_id:
        # 没有唯一标识时用事件类型+时间兜底，避免幂等表把所有事件当成同一条
        event_id = f"{event_type}:{header.get('create_time') or payload.get('ts') or 'unknown'}"
    event = as_dict(payload.get("event"))
    return event_type, str(event_id), event


def parse_message(event: dict[str, Any], *, event_id: str) -> IncomingMessage:
    """把 ``event`` 结构体解析成 :class:`IncomingMessage`（会自动去掉 @机器人 的占位符）。"""
    message = as_dict(event.get("message"))
    sender = as_dict(event.get("sender"))
    sender_id = as_dict(sender.get("sender_id"))
    mentions = [item for item in (message.get("mentions") or []) if isinstance(item, dict)]

    text = _extract_text(message.get("content"), message.get("message_type"))
    text = strip_mentions(text, mentions)

    return IncomingMessage(
        event_id=event_id,
        message_id=str(message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        chat_type=str(message.get("chat_type") or "group"),
        message_type=str(message.get("message_type") or "text"),
        sender_open_id=sender_id.get("open_id"),
        sender_type=str(sender.get("sender_type") or "user"),
        text=text,
        mentions=mentions,
        raw=message,
    )


def _extract_text(content: Any, message_type: Any) -> str:
    """飞书消息体是 JSON 字符串，比如 ``{"text": "@_user_1 help"}``。"""
    if message_type not in (None, "text", "post"):
        return ""
    if isinstance(content, dict):
        data = content
    elif isinstance(content, str) and content:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return content.strip()
    else:
        return ""
    if not isinstance(data, dict):
        return ""
    text = data.get("text")
    if isinstance(text, str):
        return text.strip()
    # post（富文本）结构：{title, content: [[{tag: text, text: ...}]]}
    chunks: list[str] = []
    for line in data.get("content") or []:
        if not isinstance(line, list):
            continue
        for node in line:
            if isinstance(node, dict) and node.get("tag") == "text" and isinstance(node.get("text"), str):
                chunks.append(node["text"])
    title = data.get("title")
    if isinstance(title, str) and title:
        chunks.insert(0, title)
    return " ".join(chunks).strip()


def strip_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
    """把 ``@_user_1`` 这类占位符整体去掉，只留下真正的指令文本。

    飞书不会告诉我们哪个 mention 是机器人自己（除非额外调机器人信息接口查 open_id），
    而我们收到的群消息必然 @ 了机器人，所以干脆把所有占位符都删掉：指令参数里
    本来也不应该包含 @人。
    """
    result = text
    for mention in mentions:
        key = mention.get("key")
        if isinstance(key, str) and key:
            result = result.replace(key, " ")
    return " ".join(result.split())
