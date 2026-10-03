"""消费方真正用到的那部分飞书能力（结构化协议 / Protocol）。

这些 Protocol 让 service / commands / runtime 依赖「能力」而不是具体的 ``FeishuClient``：

- 单元测试可以直接塞假对象（``RecordingFeishu`` 之类），不必继承真实 HTTP 客户端；
- 每个消费方只声明自己需要的方法，接口面一目了然。

``FeishuClient`` 天然满足下面全部协议（多出来的可选参数不影响兼容）。
"""

from __future__ import annotations

from typing import Any, Protocol

from ..config import ChatConfig
from .client import ChatSummary


class CardSender(Protocol):
    """能给群发卡片（投递层用）。"""

    async def send_card(self, chat: ChatConfig, card: dict[str, Any]) -> dict[str, Any]: ...


class CommandReplier(Protocol):
    """能回复消息、能查群主（群内指令用）。"""

    async def reply_card(self, message_id: str, card: dict[str, Any]) -> dict[str, Any]: ...

    async def get_chat_owner(self, chat_id: str) -> str | None: ...


class ChatLister(Protocol):
    """能列出机器人所在的群（app 通道自检用）。"""

    async def list_all_chats(self, *, max_pages: int = 5) -> list[ChatSummary]: ...


class SlashCommandAdmin(Protocol):
    """能管理斜杠指令（注册 `/` 面板用）。"""

    async def list_slash_commands(self) -> list[dict[str, Any]]: ...

    async def create_slash_command(
        self, command: str, *, description: str, i18n: dict[str, str] | None = None
    ) -> dict[str, Any]: ...

    async def update_slash_command(
        self,
        command_id: str,
        *,
        command: str | None = None,
        description: str | None = None,
        i18n: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...
