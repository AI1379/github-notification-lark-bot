"""内部统一事件模型。

GitHub 的 webhook 负载和 REST Events API 负载字段并不完全一致（例如 push 事件
webhook 用 ``after``，Events API 用 ``head``）。这里把两者归一化成同一个
``RepoEvent``，并生成**与来源无关**的 ``dedup_key`` —— 这是 webhook 与轮询
可以同时开启而不会重复推送的关键。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .labels import action_label as _action_label
from .labels import kind_emoji, kind_label
from .util import parse_ts, to_iso

Source = Literal["webhook", "poll", "manual"]

#: 归一路径能识别的事件类型（其余会落到 ``unknown`` 并仍然被记录/去重）
KNOWN_KINDS: frozenset[str] = frozenset(
    {
        "push",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issues",
        "issue_comment",
        "commit_comment",
        "release",
        "create",
        "delete",
        "fork",
        "watch",
        "public",
        "workflow_run",
        "check_run",
        "status",
        "discussion",
        "discussion_comment",
        "member",
        "gollum",
        "deployment",
    }
)


def make_dedup_key(*parts: Any) -> str:
    """用事件自身的内容拼出稳定 key。"""
    return "|".join("" if part is None else str(part) for part in parts)


def fallback_dedup_key(kind: str, repo: str, payload: dict[str, Any]) -> str:
    """未知事件类型的兜底 key：对负载做摘要。

    仅用于没有专门归一化逻辑的事件；由于 webhook 与 Events API 的负载字段不同，
    这类事件的跨源去重是「尽力而为」。
    """
    blob = json.dumps(payload, sort_keys=True, default=str)[:8000]
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]
    return f"{kind}|{repo}|{digest}"


@dataclass(slots=True)
class RepoEvent:
    """一条待推送的仓库事件。"""

    kind: str
    repo: str  # owner/name
    source: Source = "webhook"
    action: str | None = None
    actor: str | None = None
    title: str | None = None
    url: str | None = None
    summary: str | None = None
    ref: str | None = None  # 分支/标签名（已去掉 refs/heads/ 前缀）
    number: int | None = None  # PR / Issue 编号
    merged: bool = False
    draft: bool = False
    labels: tuple[str, ...] = ()
    occurred_at: datetime | None = None
    dedup_key: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    # --- 展示辅助 -------------------------------------------------------
    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0] if "/" in self.repo else self.repo

    @property
    def repo_url(self) -> str:
        return self.meta.get("repo_url") or f"https://github.com/{self.repo}"

    @property
    def author(self) -> str | None:
        return self.meta.get("author")

    @property
    def deleted(self) -> bool:
        return bool(self.meta.get("deleted"))

    @property
    def kind_label(self) -> str:
        return kind_label(self.kind)

    @property
    def kind_emoji(self) -> str:
        return kind_emoji(self.kind)

    @property
    def action_label(self) -> str:
        if self.kind == "push":
            if self.deleted:
                return "删除分支"
            if self.meta.get("forced"):
                return "强制推送"
            return f"推送 {len(self.commits)} 个提交" if self.commits else "推送"
        return _action_label(self.action, merged=self.merged, deleted=self.deleted)

    @property
    def commits(self) -> list[dict[str, Any]]:
        value = self.meta.get("commits")
        return value if isinstance(value, list) else []

    @property
    def display_title(self) -> str:
        """卡片标题行的文本。"""
        if self.title:
            return self.title
        if self.kind == "push":
            return f"推送到 {self.ref or '未知分支'}"
        return self.kind_label

    def failure_reason(self) -> str | None:
        """workflow_run 结论，用于卡片配色。"""
        conclusion = self.meta.get("conclusion")
        return str(conclusion) if conclusion else None

    def to_log(self) -> str:
        bits = [self.kind]
        if self.action:
            bits.append(self.action)
        if self.number:
            bits.append(f"#{self.number}")
        if self.ref:
            bits.append(self.ref)
        return f"{self.repo} [{' '.join(bits)}] src={self.source}"

    # --- 快照（用于投递失败后的重试）-----------------------------------
    def to_snapshot(self) -> dict[str, Any]:
        """可 JSON 序列化的快照，存进状态库供后续重试时重建事件。"""
        return {
            "kind": self.kind,
            "repo": self.repo,
            "source": self.source,
            "action": self.action,
            "actor": self.actor,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "ref": self.ref,
            "number": self.number,
            "merged": self.merged,
            "draft": self.draft,
            "labels": list(self.labels),
            "occurred_at": to_iso(self.occurred_at),
            "dedup_key": self.dedup_key,
            "meta": self.meta,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> RepoEvent:
        return cls(
            kind=str(data.get("kind") or "unknown"),
            repo=str(data.get("repo") or ""),
            source=data.get("source") or "manual",
            action=data.get("action"),
            actor=data.get("actor"),
            title=data.get("title"),
            url=data.get("url"),
            summary=data.get("summary"),
            ref=data.get("ref"),
            number=data.get("number"),
            merged=bool(data.get("merged")),
            draft=bool(data.get("draft")),
            labels=tuple(data.get("labels") or ()),
            occurred_at=parse_ts(data.get("occurred_at")),
            dedup_key=str(data.get("dedup_key") or ""),
            meta=data.get("meta") or {},
        )
