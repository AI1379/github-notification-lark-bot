"""YAML 配置模型与加载。

所有敏感值都支持 ``env:NAME`` 间接引用，避免把密钥写进 YAML：

.. code-block:: yaml

    chats:
      - name: dev
        transport: webhook
        webhook_url: env:FEISHU_WEBHOOK_DEV
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ENV_REF_PREFIX = "env:"
DEFAULT_CONFIG_PATH = Path("config/config.yaml")
EXAMPLE_CONFIG_PATH = Path("config/config.example.yaml")


class ConfigError(RuntimeError):
    """配置无法加载或校验失败。"""


def _expand_env_refs(node: Any) -> Any:
    """递归展开 ``"env:NAME"``（缺失的变量变成 None，交给 pydantic 报错）。"""
    if isinstance(node, str):
        if node.startswith(ENV_REF_PREFIX):
            name = node[len(ENV_REF_PREFIX) :].strip()
            return os.environ.get(name) or None
        return node
    if isinstance(node, dict):
        return {key: _expand_env_refs(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand_env_refs(item) for item in node]
    return node


def _collect_env_refs(node: Any, acc: set[str]) -> None:
    if isinstance(node, str):
        if node.startswith(ENV_REF_PREFIX):
            acc.add(node[len(ENV_REF_PREFIX) :].strip())
    elif isinstance(node, dict):
        for value in node.values():
            _collect_env_refs(value, acc)
    elif isinstance(node, list):
        for value in node:
            _collect_env_refs(value, acc)


def _missing_env_refs(raw: Any) -> list[str]:
    referenced: set[str] = set()
    _collect_env_refs(raw, referenced)
    return sorted(name for name in referenced if not os.environ.get(name))


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8000
    admin_token: str | None = None
    timezone: str = "Asia/Shanghai"  # 卡片上的时间展示时区


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    token: str | None = None
    webhook_secret: str | None = None
    #: 未配置 webhook_secret 时是否仍然接受 webhook。
    #: 默认 False = 直接返回 503 拒绝（否则一旦端口可公网访问，任何人都能伪造事件
    #: 往群里发消息）。只在本地调试、端口不可能被外部访问时才改 true。
    allow_unsigned_webhooks: bool = False
    api_base_url: str = "https://api.github.com"
    request_timeout_seconds: float = 15.0
    #: always = 每轮都轮询（最强兜底）；auto = 最近收到过 webhook 就跳过；never = 只收 webhook
    poll_mode: Literal["always", "auto", "never"] = "auto"
    poll_interval_seconds: int = 180
    poll_overlap_seconds: int = 300
    webhook_freshness_seconds: int = 900
    max_pages: int = 2
    per_page: int = 50
    #: 首次轮询：baseline = 只记录游标不推送历史事件；backlog = 补推最近事件
    first_poll: Literal["baseline", "backlog"] = "baseline"
    first_poll_lookback_seconds: int = 3600
    #: 额外轮询的仓库（从未收到 webhook、也没有具体订阅时用）
    poll_repos: list[str] = Field(default_factory=list)


class FeishuConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://open.feishu.cn"
    app_id: str | None = None
    app_secret: str | None = None
    request_timeout_seconds: float = 10.0
    #: 事件订阅的 Verification Token（开发者后台 → 事件与回调 → 加密策略）
    verification_token: str | None = None
    #: 未配置 verification_token 时是否仍处理回调事件。
    #: 默认 False = 直接 503 拒绝：飞书的 token 是唯一的来源校验手段，没有它任何人都能
    #: 伪造一条“群内指令”消息（连 open_id 都能随便编）让机器人执行操作。
    #: 只在本地调试时改 true。（challenge 地址校验不受此项影响，始终应答）
    allow_unverified_callbacks: bool = False


class CommandsConfig(BaseModel):
    """群内指令（@机器人 sub/unsub/...）。默认关闭。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: 白名单 open_id，这些人可以在任何群改订阅
    admins: list[str] = Field(default_factory=list)
    #: 允许群主管理自己群的订阅（比较消息 sender 与群 owner_id）
    allow_group_owner: bool = True
    #: 允许指令订阅的仓库通配；**留空 = 只能订阅 config 里已经声明过的仓库**（安全默认值）
    repo_allowlist: list[str] = Field(default_factory=list)
    #: 群主 id 的缓存时间，避免每条指令都调一次群信息接口
    owner_cache_seconds: int = 300
    #: 是否回执到消息（关掉后指令静默执行，仅记日志）
    reply: bool = True
    #: 要求消息里必须 @ 了机器人才当成指令。
    #: 默认 true：按第 5.1 节建议只开 `im:message.group_at_msg:readonly` 时，收到的消息本来就带 @，
    #: 这项等于白送的保险；万一你为了别的需求申请了「接收群内所有消息」的敏感权限，
    #: 它能避免群里随便一句 “sub ...” 被当成指令。若发现指令面板发来的消息不带 @，把它改成 false。
    require_mention: bool = True
    #: 单个群通过指令最多订阅多少个仓库
    max_repos_per_chat: int = 50


class DeliveryConfig(BaseModel):
    """投递与重试策略。"""

    model_config = ConfigDict(extra="forbid")

    #: 每轮最多重试多少条失败投递
    retry_limit: int = 20
    #: 首次失败超过这个时间的记录不再重试（避免无限重试）
    retry_max_age_minutes: int = 60
    #: 投递记录保留天数（去重依赖它，建议 >= 轮询回溯窗口）
    ttl_days: int = 14


class ChatConfig(BaseModel):
    """一个推送目标（飞书群）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["webhook", "app"] = "webhook"
    #: transport=webhook：群自定义机器人的 webhook 地址
    webhook_url: str | None = None
    #: 机器人开启「签名校验」时的密钥
    webhook_secret: str | None = None
    #: transport=app：飞书群 chat_id（oc_ 开头）
    chat_id: str | None = None
    enabled: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def _check_transport_fields(self) -> ChatConfig:
        if not self.enabled:
            return self
        if self.transport == "webhook" and not self.webhook_url:
            raise ValueError(
                f"chat {self.name!r}: transport=webhook 需要 webhook_url"
                "（如果配置里写的是 `env:VAR`，请确认该环境变量已导出，参考 .env.example）"
            )
        if self.transport == "app" and not self.chat_id:
            raise ValueError(
                f"chat {self.name!r}: transport=app 需要 chat_id（如果配置里写的是 `env:VAR`，请确认该环境变量已导出）"
            )
        return self


class Defaults(BaseModel):
    """订阅的公共默认值，可被单条 subscription 覆盖。"""

    model_config = ConfigDict(extra="forbid")

    events: list[str] | None = None
    #: 两种写法：
    #: 1) 列表：作用于**所有**事件类型（写窄了会连带丢掉 issue_comment / release 等）
    #: 2) 映射：按事件类型分别过滤，没列到的事件类型不过滤（推荐）
    #:    actions: {pull_request: [opened, closed, merged]}
    actions: list[str] | dict[str, list[str]] | None = None
    branches: list[str] | None = None
    ignore_actors: list[str] = Field(default_factory=list)
    ignore_labels: list[str] = Field(default_factory=list)
    ignore_drafts: bool = False


class SubscriptionConfig(BaseModel):
    """「哪些仓库的哪些事件 -> 推到哪些群」。"""

    model_config = ConfigDict(extra="forbid")

    repos: list[str]
    chats: list[str]
    events: list[str] | None = None
    #: 同 ``Defaults.actions``：列表（全局）或映射（按事件类型）
    actions: list[str] | dict[str, list[str]] | None = None
    branches: list[str] | None = None
    #: None = 继承 defaults；[] = 显式清空（即不做任何 actor 过滤）
    ignore_actors: list[str] | None = None
    ignore_labels: list[str] | None = None
    ignore_drafts: bool | None = None
    enabled: bool = True
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_repo_shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "repo" in data:
            repo = data.pop("repo")
            data.setdefault("repos", repo)
        if isinstance(data.get("repos"), str):
            data["repos"] = [data["repos"]]
        if isinstance(data.get("chats"), str):
            data["chats"] = [data["chats"]]
        if not data.get("repos"):
            raise ValueError("subscription 需要 repo 或 repos")
        if not data.get("chats"):
            raise ValueError("subscription 需要 chats")
        return data


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    defaults: Defaults = Field(default_factory=Defaults)
    chats: list[ChatConfig] = Field(default_factory=list)
    subscriptions: list[SubscriptionConfig] = Field(default_factory=list)

    # 运行时补充信息，不来自 YAML
    source_path: str | None = None
    used_example_file: bool = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistency(self) -> Config:
        names = [chat.name for chat in self.chats]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"chat 名称重复: {sorted(duplicates)}")
        known = set(names)
        for sub in self.subscriptions:
            missing = [name for name in sub.chats if name not in known]
            if missing:
                raise ValueError(f"subscription {sub.repos!r} 引用了未定义的 chat: {missing}")
        if self.subscriptions and not self.chats:
            raise ValueError("存在 subscriptions 但没有定义任何 chats")
        return self

    # --- 便捷访问 -------------------------------------------------------
    def chat(self, name: str) -> ChatConfig | None:
        for chat in self.chats:
            if chat.name == name:
                return chat
        return None

    def chat_by_chat_id(self, chat_id: str | None) -> ChatConfig | None:
        if not chat_id:
            return None
        for chat in self.chats:
            if chat.chat_id and chat.chat_id == chat_id:
                return chat
        return None

    @property
    def active_chats(self) -> list[ChatConfig]:
        return [chat for chat in self.chats if chat.enabled]

    def summary(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "used_example_file": self.used_example_file,
            "warnings": self.warnings,
            "poll_mode": self.github.poll_mode,
            "poll_interval_seconds": self.github.poll_interval_seconds,
            "webhook_signature_required": bool(self.github.webhook_secret),
            "allow_unsigned_webhooks": self.github.allow_unsigned_webhooks,
            "github_token_configured": bool(self.github.token),
            "delivery": {
                "retry_limit": self.delivery.retry_limit,
                "retry_max_age_minutes": self.delivery.retry_max_age_minutes,
                "ttl_days": self.delivery.ttl_days,
            },
            "commands": {
                "enabled": self.commands.enabled,
                "admins": len(self.commands.admins),
                "allow_group_owner": self.commands.allow_group_owner,
                "repo_allowlist": self.commands.repo_allowlist,
                "callback_ready": bool(self.feishu.verification_token and self.commands.enabled),
            },
            "chats": [
                {
                    "name": chat.name,
                    "transport": chat.transport,
                    "enabled": chat.enabled,
                    "target": ("webhook" if chat.transport == "webhook" else chat.chat_id),
                }
                for chat in self.chats
            ],
            "subscriptions": [
                {
                    "repos": sub.repos,
                    "chats": sub.chats,
                    "events": sub.events,
                    "actions": sub.actions,
                    "branches": sub.branches,
                    "enabled": sub.enabled,
                }
                for sub in self.subscriptions
            ],
        }


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env_path = os.environ.get("LARKBOT_CONFIG")
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def load_config(path: str | Path | None = None, *, lenient: bool = False) -> Config:
    """读取并校验配置。

    - 默认路径缺失时回退到 ``config.example.yaml``，方便首次试跑
    - ``lenient=True``：缺少凭证的 chat 只标记为 ``enabled: false`` 并记入 ``warnings``，
      而不是直接报错。供 ``check`` / ``simulate`` 这类诊断与演练命令使用；
      ``serve`` 仍然使用严格模式，避免「以为配好了其实没推」的静默失败。
    """
    resolved = resolve_config_path(path)
    used_example = False
    if not resolved.exists():
        if resolved == DEFAULT_CONFIG_PATH and EXAMPLE_CONFIG_PATH.exists():
            resolved = EXAMPLE_CONFIG_PATH
            used_example = True
        else:
            raise ConfigError(f"配置文件不存在: {resolved}（可从 config/config.example.yaml 复制）")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败: {resolved}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是映射: {resolved}")

    missing = _missing_env_refs(raw)
    expanded = _expand_env_refs(raw)
    warnings: list[str] = []
    if lenient:
        warnings = _disable_incomplete_chats(expanded)
    try:
        config = Config.model_validate(expanded)
    except ValidationError as exc:
        hint = ""
        if missing:
            hint = (
                "\n提示: 配置引用了这些尚未设置的环境变量: "
                + ", ".join(missing)
                + "（可参考 .env.example；先 `cp .env.example .env` 并导出）"
            )
        raise ConfigError(f"配置文件校验失败: {resolved}{hint}\n{exc}") from exc
    config.source_path = str(resolved)
    config.used_example_file = used_example
    config.warnings = warnings
    return config


def _disable_incomplete_chats(raw: dict[str, Any]) -> list[str]:
    """宽松模式：把缺凭证的 chat 置为 disabled，返回说明列表。"""
    warnings: list[str] = []
    for chat in raw.get("chats") or []:
        if not isinstance(chat, dict) or not chat.get("enabled", True):
            continue
        transport = chat.get("transport", "webhook")
        if transport == "webhook" and not chat.get("webhook_url"):
            chat["enabled"] = False
            warnings.append(f"chat {chat.get('name')!r}: 缺少 webhook_url，已临时禁用")
        elif transport == "app" and not chat.get("chat_id"):
            chat["enabled"] = False
            warnings.append(f"chat {chat.get('name')!r}: 缺少 chat_id，已临时禁用")
    return warnings
