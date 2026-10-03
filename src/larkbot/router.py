"""路由：把一条事件解析成「应该推送到哪些群」。

支持的能力（都是配置驱动的，不需要改代码）：
- 按仓库（支持 ``org/*`` 通配）
- 按事件类型 / action / 分支（支持通配）
- 忽略指定 actor（含作者）、带指定标签的、草稿 PR
- 多条规则叠加，同一事件可以进多个群，群不会重复
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import ChatConfig, Config, SubscriptionConfig
from .models import RepoEvent
from .util import glob_match, glob_match_any

logger = logging.getLogger(__name__)

#: 配置里可用的简写
EVENT_ALIASES = {
    "pr": "pull_request",
    "prs": "pull_request",
    "pull_requests": "pull_request",
    "issue": "issues",
    "comments": "issue_comment",
    "comment": "issue_comment",
    "tags": "create",
}


def normalize_event_names(names: list[str] | None) -> list[str]:
    if not names:
        return []
    return [EVENT_ALIASES.get(name.strip().lower(), name.strip().lower()) for name in names]


@dataclass(slots=True)
class RouteRule:
    """一次命中的记录，便于 /status 或日志排查「为什么没推」。"""

    subscription: SubscriptionConfig
    matched: bool
    reason: str
    chats: list[str] = field(default_factory=list)


class Router:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._chats = {chat.name: chat for chat in config.chats if chat.enabled}

    # --- 对外接口 -------------------------------------------------------
    def effective_subscriptions(self, dynamic: dict[str, list[str]] | None = None) -> list[SubscriptionConfig]:
        """config 里的规则 + 指令创建的规则（叠加）。

        指令创建的规则被当成"用默认策略订阅某个仓库"的一条普通规则：只带仓库通配和群，
        其余过滤条件全部继承 ``defaults``。**它永远不会修改/删除 config 里的规则**，
        所以不会出现"机器人在背后改了配置"的意外。
        """
        subscriptions = list(self.config.subscriptions)
        for chat_name, patterns in (dynamic or {}).items():
            if chat_name not in self._chats or not patterns:
                continue
            subscriptions.append(SubscriptionConfig(repos=list(patterns), chats=[chat_name], note="由群内指令创建"))
        return subscriptions

    def resolve(self, event: RepoEvent, dynamic: dict[str, list[str]] | None = None) -> list[ChatConfig]:
        """返回该事件要投递的群（去重、保持配置顺序）。"""
        targets: list[ChatConfig] = []
        seen: set[str] = set()
        for rule in self.explain(event, dynamic):
            if not rule.matched:
                continue
            for name in rule.chats:
                chat = self._chats.get(name)
                if chat is None or chat.name in seen:
                    continue
                seen.add(chat.name)
                targets.append(chat)
        return targets

    def explain(self, event: RepoEvent, dynamic: dict[str, list[str]] | None = None) -> list[RouteRule]:
        """给出每条订阅的命中情况（用于调试）。"""
        rules: list[RouteRule] = []
        for sub in self.effective_subscriptions(dynamic):
            if not sub.enabled:
                rules.append(RouteRule(sub, False, "disabled"))
                continue
            if not self._repo_matches(sub, event.repo):
                rules.append(RouteRule(sub, False, "repo"))
                continue
            passed, reason = self._filters_pass(sub, event)
            rules.append(RouteRule(sub, passed, reason, list(sub.chats)))
        return rules

    def is_watched_repo(self, repo: str, dynamic: dict[str, list[str]] | None = None) -> bool:
        """是否有任何启用的订阅（含指令创建的）关心这个仓库（决定要不要为它开轮询）。"""
        return any(sub.enabled and self._repo_matches(sub, repo) for sub in self.effective_subscriptions(dynamic))

    def concrete_repos(self, dynamic: dict[str, list[str]] | None = None) -> list[str]:
        """订阅里写死的仓库（不含通配），用于初始化轮询列表。"""
        repos: list[str] = []
        for sub in self.effective_subscriptions(dynamic):
            if not sub.enabled:
                continue
            for pattern in sub.repos:
                if "*" not in pattern and "?" not in pattern and "/" in pattern and pattern not in repos:
                    repos.append(pattern)
        return repos

    # --- 供指令层使用 ---------------------------------------------------
    @staticmethod
    def _actions_pass(actions: list[str] | dict[str, list[str]], event: RepoEvent) -> bool:
        """action 过滤。

        ``actions`` 支持两种写法：

        - **列表**：作用于所有事件类型。注意会连带影响 issue_comment / release 等——
          只写 ``[opened, closed]`` 会默默丢掉 ``published`` 的 Release。
        - **映射**：``{事件类型: [action...]}``，只对列到的事件类型过滤，其它类型不过滤。
          事件类型支持通配（``pull_request*`` 同时盖住 pull_request_review 等）。
        """
        if isinstance(actions, dict):
            matched = [patterns for kind, patterns in actions.items() if patterns and glob_match(kind, event.kind)]
            if not matched:
                return True  # 这个事件类型没被列到 -> 不做 action 过滤
            patterns_to_check = [pattern for patterns in matched for pattern in patterns]
        else:
            patterns_to_check = list(actions)

        candidates = [
            value
            for value in (
                event.action,
                "merged" if event.merged else None,
                event.meta.get("conclusion"),
                event.meta.get("state"),
                event.meta.get("review_state"),
            )
            if isinstance(value, str) and value
        ]
        return any(glob_match(pattern, candidate) for pattern in patterns_to_check for candidate in candidates)

    def static_patterns_for_chat(self, chat_name: str) -> list[str]:
        """config 里已声明、且会送到这个群的仓库通配（退订时提醒用）。"""
        patterns: list[str] = []
        for sub in self.config.subscriptions:
            if sub.enabled and chat_name in sub.chats:
                patterns.extend(pattern for pattern in sub.repos if pattern not in patterns)
        return patterns

    def known_repo_patterns(self) -> list[str]:
        """config 声明过的全部仓库通配（指令的默认许可范围）。"""
        patterns: list[str] = []
        for sub in self.config.subscriptions:
            if sub.enabled:
                patterns.extend(pattern for pattern in sub.repos if pattern not in patterns)
        return patterns

    # --- 匹配细节 -------------------------------------------------------
    @staticmethod
    def _repo_matches(sub: SubscriptionConfig, repo: str) -> bool:
        return any(glob_match(pattern, repo) for pattern in sub.repos)

    def _filters_pass(self, sub: SubscriptionConfig, event: RepoEvent) -> tuple[bool, str]:
        defaults = self.config.defaults

        events = normalize_event_names(sub.events if sub.events is not None else defaults.events)
        if events and not glob_match_any(events, event.kind):
            return False, "event"

        actions = sub.actions if sub.actions is not None else defaults.actions
        if actions and not self._actions_pass(actions, event):
            return False, "action"

        branches = sub.branches if sub.branches is not None else defaults.branches
        if branches and event.ref and not glob_match_any(branches, event.ref):
            return False, "branch"

        ignore_drafts = sub.ignore_drafts if sub.ignore_drafts is not None else defaults.ignore_drafts
        if ignore_drafts and event.draft:
            return False, "draft"

        ignore_actors = sub.ignore_actors if sub.ignore_actors is not None else defaults.ignore_actors
        if ignore_actors:
            for candidate in (event.actor, event.author):
                if glob_match_any(ignore_actors, candidate):
                    return False, "actor"

        ignore_labels = sub.ignore_labels if sub.ignore_labels is not None else defaults.ignore_labels
        if ignore_labels and any(glob_match_any(ignore_labels, label) for label in event.labels):
            return False, "label"

        return True, "ok"

    def summary(self) -> dict[str, Any]:
        return {
            "chats": sorted(self._chats),
            "subscriptions": len(self.config.subscriptions),
            "defaults_events": normalize_event_names(self.config.defaults.events),
        }
