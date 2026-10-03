"""测试公共夹具：配置构造、临时状态库、假飞书客户端。"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from larkbot.config import Config
from larkbot.feishu.client import FeishuError
from larkbot.store import StateStore

WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/fake-dev"


def base_config_dict() -> dict[str, Any]:
    return {
        "server": {"timezone": "Asia/Shanghai", "admin_token": "admin-token"},
        "github": {
            "token": None,
            "webhook_secret": "test-secret",
            "poll_mode": "auto",
            "poll_interval_seconds": 60,
            "webhook_freshness_seconds": 900,
            "per_page": 50,
            "first_poll": "baseline",
        },
        "feishu": {"app_id": "cli_test", "app_secret": "secret"},
        "chats": [
            {"name": "dev", "transport": "webhook", "webhook_url": WEBHOOK_URL},
            {"name": "rel", "transport": "app", "chat_id": "oc_test_chat"},
        ],
        "defaults": {
            "events": ["push", "pull_request", "release"],
            "ignore_actors": ["*[bot]"],
            "ignore_drafts": True,
        },
        "subscriptions": [
            {"repos": ["acme/*"], "chats": ["dev"]},
            {"repos": ["acme/api"], "chats": ["rel"], "events": ["release"]},
        ],
    }


def make_config(**overrides: Any) -> Config:
    """构造测试配置，``overrides`` 为按 key 的浅覆盖（chats/subscriptions 整体替换）。"""
    raw = base_config_dict()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = copy.deepcopy(value)
    return Config.model_validate(raw)


class RecordingFeishu:
    """替代 FeishuClient 的记录器，用来断言「发了什么、发了几次」。"""

    def __init__(self, fail_chats: set[str] | None = None, owner: str | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.fail_chats = set(fail_chats or ())
        self.owner = owner

    async def send_card(self, chat: Any, card: dict[str, Any]) -> dict[str, Any]:
        if chat.name in self.fail_chats:
            raise FeishuError(f"模拟失败: {chat.name}", chat=chat.name, retryable=True)
        self.sent.append((chat.name, card))
        return {"code": 0}

    async def send_text(self, chat: Any, text: str) -> dict[str, Any]:
        self.sent.append((chat.name, {"text": text}))
        return {"code": 0}

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> dict[str, Any]:
        self.replies.append((message_id, card))
        return {"code": 0}

    async def get_chat_owner(self, chat_id: str) -> str | None:
        return self.owner

    def chats(self) -> list[str]:
        return [name for name, _ in self.sent]

    def reply_texts(self) -> str:
        return "\n".join(_card_text(card) for _, card in self.replies)

    async def aclose(self) -> None:  # 与服务层接口保持一致
        return None


def _card_text(card: dict[str, Any]) -> str:
    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("tag") in {"lark_md", "plain_text"} and isinstance(node.get("content"), str):
                chunks.append(node["content"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return "\n".join(chunks)


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
async def store(tmp_path):
    state = StateStore(tmp_path / "state.db")
    await state.open()
    try:
        yield state
    finally:
        await state.close()


@pytest.fixture
def feishu() -> RecordingFeishu:
    return RecordingFeishu()
