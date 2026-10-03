"""终端输出辅助：中英文对齐的表格、可直接粘贴的 YAML 片段。"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from .feishu.client import ChatSummary
from .util import slugify


def display_width(text: str) -> int:
    """终端显示宽度：CJK 全角字符算 2 列，否则对齐会歪。"""
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    return text + " " * max(width - display_width(text), 0)


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], display_width(cell))
    lines = ["  ".join(pad(header, widths[i]) for i, header in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _yaml_scalar(value: str) -> str:
    """必要时给 YAML 标量加引号，避免中文/冒号/井号把配置写坏。"""
    if value and all(char.isalnum() or char in "-_./" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_chats_yaml(chats: Sequence[ChatSummary]) -> str:
    """生成可直接粘进 config.yaml 的 ``chats:`` 片段（transport: app）。"""
    lines = [
        "# 由 `larkbot chats --yaml` 生成：请把 name 改成你想要的别名，",
        "# 然后在 subscriptions 里用这个别名指定哪些仓库推到这个群。",
        "chats:",
    ]
    for chat in chats:
        alias = slugify(chat.name, fallback=f"group-{chat.chat_id[-6:]}")
        lines.extend(
            [
                f"  - name: {_yaml_scalar(alias)}",
                f"    # 飞书群: {chat.name}",
                "    transport: app",
                f"    chat_id: {_yaml_scalar(chat.chat_id)}",
            ]
        )
    if not chats:
        lines.append("  []  # 机器人当前不在任何群里")
    return "\n".join(lines)
