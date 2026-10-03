"""飞书交互卡片渲染。

用的是卡片 1.0 结构（不写 ``schema``），自定义机器人 webhook 与 ``im/v1/messages``
都能直接吃这一份 JSON。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..labels import card_template, conclusion_label
from ..models import RepoEvent
from ..util import truncate

logger = logging.getLogger(__name__)

MAX_COMMITS_SHOWN = 3


def _fmt_time(value: datetime | None, timezone: str) -> str | None:
    if value is None:
        return None
    try:
        local = value.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        logger.debug("时区 %s 不可用，回退到 UTC 展示", timezone)
        local = value
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _md(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _fields(pairs: Sequence[tuple[str, str | None]]) -> dict[str, Any] | None:
    rendered = [
        {"is_short": True, "text": {"tag": "lark_md", "content": f"**{name}**\n{value}"}}
        for name, value in pairs
        if value
    ]
    if not rendered:
        return None
    return {"tag": "div", "fields": rendered}


def _note(content: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def _commits_block(event: RepoEvent) -> dict[str, Any] | None:
    commits = [c for c in event.commits if c.get("message") or c.get("sha")]
    if not commits:
        return None
    lines: list[str] = []
    for commit in commits[:MAX_COMMITS_SHOWN]:
        sha = commit.get("short_sha") or ""
        message = truncate(commit.get("message"), 90)
        author = commit.get("author") or ""
        label = f"[`{sha}`]({commit['url']})" if commit.get("url") and sha else f"`{sha}`"
        suffix = f" — {author}" if author else ""
        lines.append(f"• {label} {message}{suffix}")
    hidden = len(commits) - MAX_COMMITS_SHOWN
    if hidden > 0:
        lines.append(f"• …还有 {hidden} 个提交")
    return _md("\n".join(lines))


def build_event_card(event: RepoEvent, *, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    """把一条事件渲染成飞书交互卡片。"""
    combined = event.failure_reason()
    success = None
    if event.kind == "workflow_run" and combined:
        success = combined == "success"

    header = {
        "template": card_template(event.kind, event.action, merged=event.merged, success=success),
        "title": {
            "tag": "plain_text",
            "content": f"{event.kind_emoji} {event.kind_label} · {event.repo}",
        },
    }

    elements: list[dict[str, Any]] = []

    title = event.display_title
    title_line = f"**[{title}]({event.url})**" if event.url else f"**{title}**"
    if event.number and event.kind in {"pull_request", "issues", "discussion"}:
        title_line = f"#{event.number} {title_line}"
    subtitle = f"**{event.repo}** · {event.action_label}"
    if event.meta.get("ref_type") == "tag":
        subtitle += " · 标签"
    elements.append(_md(f"{title_line}\n{subtitle}"))

    commits_block = _commits_block(event) if event.kind == "push" else None
    if commits_block:
        elements.append(commits_block)
    elif event.summary:
        elements.append(_md(truncate(event.summary, 300)))

    fields = _fields(
        [
            ("触发人", f"@{event.actor}" if event.actor else None),
            ("作者", f"@{event.author}" if event.author and event.author != event.actor else None),
            ("分支" if event.meta.get("ref_type") != "tag" else "标签", f"`{event.ref}`" if event.ref else None),
            ("提交", event.meta.get("head_sha")),
            ("标签", "、".join(event.labels) if event.labels else None),
            ("结论", conclusion_label(event.meta.get("conclusion"))),
            ("时间", _fmt_time(event.occurred_at, timezone)),
        ]
    )
    if fields:
        elements.append(fields)

    if event.url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "在 GitHub 查看"},
                        "type": "default",
                        "url": event.url,
                    }
                ],
            }
        )

    elements.append(_note(f"larkbot · 事件: {event.kind} · 来源: {event.source}"))

    return {
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    }


def build_test_card(
    chat_name: str, *, timezone: str = "Asia/Shanghai", details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """自检用卡片：验证 webhook 地址 / 应用凭证是否可用。"""
    pairs = [("目标群", chat_name)]
    for key, value in (details or {}).items():
        if value is not None:
            pairs.append((key, str(value)))
    elements = [_md("**配置连通性自检**\n如果你看到这条消息，说明 larkbot 已经能推送到这个群了。")]
    fields = _fields(pairs)
    if fields:
        elements.append(fields)
    elements.append(_note("larkbot · test message"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": "✅ larkbot 连通性测试"},
        },
        "elements": elements,
    }
