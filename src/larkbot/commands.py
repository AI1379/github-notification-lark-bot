"""群内指令：``@机器人 sub acme/api`` 这类操作。

三条设计原则（都是为了不让人被机器人搞糊涂）：

1. **指令只能增删「指令自己创建的订阅」**，永远不能修改或删除 config.yaml 里的规则。
   退订时我们会明确告诉他"config 里还有规则在推"，而不是让他以为退订失灵了。
2. **权限收敛**：默认只允许群主（或 config 里 ``commands.admins`` 白名单）改订阅，
   且只能订阅 ``commands.repo_allowlist``（留空时 = config 已声明过的仓库）范围内的仓库。
3. **每次变更都在群里回执**，谁改的都留在群里可见，相当于自带的审计记录。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ChatConfig, Config
from .feishu.client import FeishuError
from .feishu.events import IncomingMessage
from .feishu.protocols import CommandReplier, SlashCommandAdmin
from .router import Router
from .stats import Stats
from .store import StateStore
from .util import glob_match, truncate

logger = logging.getLogger(__name__)

HELP_TEXT_HEADER = "**larkbot 指令**"


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    """一条可注册到客户端 `/` 面板的指令。"""

    command: str  # 不带前导斜杠（接口要求）
    usage: str  # 帮助里显示的用法，可能带参数
    description: str  # 中文说明，作为 description.default_value
    en_us: str | None = None

    @property
    def slash(self) -> str:
        return f"/{self.command}"

    def to_i18n(self) -> dict[str, str]:
        mapping = {"zh_cn": self.description}
        if self.en_us:
            mapping["en_us"] = self.en_us
        return mapping

    def to_body(self) -> dict[str, Any]:
        return {"command": self.command, "description": {"default_value": self.description, "i18n": self.to_i18n()}}


#: 帮助文本与斜杠指令注册表共用同一份定义，避免两处漂移
SLASH_COMMANDS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec("help", "help", "显示 larkbot 指令帮助", "Show larkbot command help"),
    SlashCommandSpec("list", "list", "查看本群订阅的仓库", "Show subscriptions of this chat"),
    SlashCommandSpec("sub", "sub <owner/repo>", "订阅一个仓库（支持 org/* 通配）", "Subscribe a repository"),
    SlashCommandSpec("unsub", "unsub <owner/repo>", "退订一个仓库", "Unsubscribe a repository"),
    SlashCommandSpec("whoami", "whoami", "查看你的 open_id 与群 chat_id", "Show your open_id and chat id"),
)

#: 所有能被处理的指令名（含中文别名）
KNOWN_COMMANDS: frozenset[str] = frozenset(
    {"help", "list", "sub", "unsub", "whoami", "?", "帮助", "列表", "查看", "ls", "订阅", "退订", "我是谁"}
)


def build_help_text() -> str:
    """由 SLASH_COMMANDS 生成帮助，保证注册的指令和帮助永远一致。"""
    lines = [HELP_TEXT_HEADER, ""]
    lines += [f"`@我 {spec.usage}` — {spec.description}" for spec in SLASH_COMMANDS]
    lines += [
        "",
        "带 `/` 前缀同样识别（`/help`、`/sub acme/api`）：飞书的指令面板发过来的也是普通文本消息。",
        "",
        "指令创建的订阅按 `defaults` 里的事件范围推送；需要更细的过滤（分支、action、忽略某人）请改 config.yaml。",
    ]
    return "\n".join(lines)


#: 实际使用的帮助文案
HELP_TEXT = build_help_text()


@dataclass(slots=True)
class SlashRegisterReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)  # --dry-run 时打印的内容

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "bodies": self.bodies,
        }


async def register_slash_commands(
    feishu: SlashCommandAdmin | None,
    specs: tuple[SlashCommandSpec, ...] | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> SlashRegisterReport:
    """把指令注册到客户端的 `/` 面板（幂等）。

    - 默认：已存在同名指令就跳过（不碰用户可能在控制台手动加过的说明）
    - ``force=True``：已存在则 PATCH 更新（注意：i18n 传了会整张覆盖）
    - ``dry_run=True``：不打任何接口，只返回将要发送的请求体

    需要应用权限 ``application:app_slash_command:write``；列已有指令还需 ``...:read``。
    """
    spec_list = specs or SLASH_COMMANDS
    report = SlashRegisterReport()
    if dry_run:
        report.bodies = [spec.to_body() for spec in spec_list]
        return report
    if feishu is None:
        raise ValueError("非 dry-run 调用必须传入 feishu 客户端")

    existing: dict[str, dict[str, Any]] = {}
    for item in await feishu.list_slash_commands():
        name = str(item.get("command") or "").lstrip("/").lower()
        if name:
            existing[name] = item

    for spec in spec_list:
        current = existing.get(spec.command.lower())
        if current is not None and not force:
            report.skipped.append(spec.slash)
            continue
        try:
            if current is None:
                await feishu.create_slash_command(spec.command, description=spec.description, i18n=spec.to_i18n())
                report.created.append(spec.slash)
            else:
                await feishu.update_slash_command(
                    str(current.get("command_id") or ""),
                    command=spec.command,
                    description=spec.description,
                    i18n=spec.to_i18n(),
                )
                report.updated.append(spec.slash)
        except FeishuError as exc:
            logger.warning("注册斜杠指令 %s 失败: %s", spec.slash, exc)
            report.failed.append({"command": spec.slash, "error": str(exc)})
    return report


@dataclass(slots=True)
class CommandOutcome:
    command: str | None
    status: str  # ok / unregistered / denied / ignored / error
    detail: str
    applied: bool = False
    reply: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"command": self.command, "status": self.status, "detail": self.detail, "applied": self.applied}


def _card(title: str, template: str, lines: list[str], note: str | None = None) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": line}} for line in lines if line
    ]
    if note:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def parse_command(text: str) -> tuple[str | None, list[str]]:
    """把消息文本拆成 ``(命令, 参数)``；带中文别名、兼容全角斜杠。"""
    parts = text.split()
    if not parts:
        return None, []
    # 中文输入法很容易打出全角斜杠（／）与全角问号（？），先归一化
    head = parts[0].replace("／", "/").replace("？", "?")
    raw = head.lstrip("/")
    aliases = {
        "?": "help",
        "帮助": "help",
        "说明": "help",
        "列表": "list",
        "查看": "list",
        "ls": "list",
        "订阅": "sub",
        "subscribe": "sub",
        "add": "sub",
        "退订": "unsub",
        "取消订阅": "unsub",
        "unsubscribe": "unsub",
        "remove": "unsub",
        "del": "unsub",
        "我是谁": "whoami",
        "me": "whoami",
        "id": "whoami",
    }
    command = aliases.get(raw.lower(), raw.lower())
    return command, parts[1:]


class CommandHandler:
    def __init__(
        self,
        config: Config,
        store: StateStore,
        feishu: CommandReplier,
        router: Router,
        *,
        stats: Stats | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.feishu = feishu
        self.router = router
        self.stats = stats or Stats()
        self._owner_cache: dict[str, tuple[str | None, float]] = {}

    def set_config(self, config: Config) -> None:
        self.config = config
        self.router = Router(config)
        self._owner_cache.clear()

    # --- 入口 -----------------------------------------------------------
    async def handle(self, message: IncomingMessage) -> CommandOutcome:
        commands = self.config.commands
        if not commands.enabled:
            return CommandOutcome(None, "ignored", "指令功能未启用（commands.enabled=false）")
        if message.is_from_bot or not message.text:
            return CommandOutcome(None, "ignored", "机器人消息或空消息")
        if commands.require_mention and not message.mentions:
            return CommandOutcome(None, "ignored", "消息里没有 @ 机器人（commands.require_mention=true）")

        command, args = parse_command(message.text)
        if command is None:
            return CommandOutcome(None, "ignored", "空指令")

        chat = self.config.chat_by_chat_id(message.chat_id)
        if chat is None:
            outcome = CommandOutcome(
                command,
                "unregistered",
                f"这个会话（chat_id={message.chat_id}）不在 config 的 chats 里，无法管理订阅",
            )
            outcome.reply = _card(
                "这个群还没有登记",
                "orange",
                [
                    "我需要先知道这个群叫什么名字，才能管理它的订阅。",
                    f"`chat_id`: `{message.chat_id}`",
                    "请把它加进 `config.yaml` 的 `chats:`（`transport: app`），或用 `larkbot chats --yaml` 生成。",
                ],
                note="larkbot · 未登记的会话",
            )
            return await self._finish(message, outcome)

        try:
            if command in ("help", "?"):
                outcome = await self._cmd_help(chat)
            elif command == "list":
                outcome = await self._cmd_list(chat)
            elif command == "whoami":
                outcome = await self._cmd_whoami(message, chat)
            elif command == "sub":
                outcome = await self._cmd_subscribe(message, chat, args)
            elif command == "unsub":
                outcome = await self._cmd_unsubscribe(message, chat, args)
            else:
                outcome = CommandOutcome(command, "ok", f"未知指令 {command!r}")
                outcome.reply = _card(
                    "没认出这条指令",
                    "grey",
                    [HELP_TEXT],
                    note=f"收到的内容: {truncate(message.text, 80)}",
                )
        except FeishuError as exc:
            logger.warning("执行指令 %s 失败: %s", command, exc)
            outcome = CommandOutcome(command, "error", str(exc))
            outcome.reply = _card("执行失败", "red", [f"调用飞书接口出错：{truncate(str(exc), 200)}"])

        if outcome.applied:
            self.stats.commands_applied += 1
        self.stats.commands_handled += 1
        return await self._finish(message, outcome)

    async def _finish(self, message: IncomingMessage, outcome: CommandOutcome) -> CommandOutcome:
        logger.info(
            "指令: cmd=%s status=%s chat=%s sender=%s detail=%s",
            outcome.command,
            outcome.status,
            message.chat_id,
            message.sender_open_id,
            outcome.detail,
        )
        if self.config.commands.reply and outcome.reply is not None and message.message_id:
            try:
                await self.feishu.reply_card(message.message_id, outcome.reply)
            except FeishuError as exc:
                logger.warning("回复指令失败: %s", exc)
        return outcome

    # --- 各指令 ---------------------------------------------------------
    async def _cmd_help(self, chat: ChatConfig) -> CommandOutcome:
        outcome = CommandOutcome("help", "ok", "帮助")
        outcome.reply = _card(
            f"larkbot 指令 · {chat.name}",
            "turquoise",
            [HELP_TEXT, f"把 **{chat.name}** 这个别名写进 `subscriptions[].chats` 即可在 config 里管理。"],
        )
        return outcome

    async def _cmd_list(self, chat: ChatConfig) -> CommandOutcome:
        static_patterns = self.router.static_patterns_for_chat(chat.name)
        dynamic = (await self.store.dynamic_subscriptions()).get(chat.name, [])
        lines = [
            f"**群别名** `{chat.name}`",
            "**config 里的规则**（需要改 YAML 才能动）\n"
            + ("\n".join(f"• `{pattern}`" for pattern in static_patterns) if static_patterns else "• 无"),
            "**指令创建的订阅**\n" + ("\n".join(f"• `{pattern}`" for pattern in dynamic) if dynamic else "• 无"),
        ]
        outcome = CommandOutcome("list", "ok", f"static={len(static_patterns)} dynamic={len(dynamic)}")
        outcome.reply = _card(
            "本群订阅",
            "blue",
            lines,
            note="`@我 sub owner/repo` 添加；指令不会改动 config 里的规则",
        )
        return outcome

    async def _cmd_whoami(self, message: IncomingMessage, chat: ChatConfig) -> CommandOutcome:
        outcome = CommandOutcome("whoami", "ok", "whoami")
        outcome.reply = _card(
            "身份信息",
            "grey",
            [
                f"你的 `open_id`：`{message.sender_open_id or '未知'}`",
                f"本群 `chat_id`：`{message.chat_id}`（别名 `{chat.name}`）",
            ],
            note="把 open_id 填进 config 的 commands.admins 可以授予管理权限",
        )
        return outcome

    async def _cmd_subscribe(self, message: IncomingMessage, chat: ChatConfig, args: list[str]) -> CommandOutcome:
        if not args:
            return self._usage("sub", "用法：`@我 sub owner/repo`（可一次写多个，支持 `org/*`）")
        allowed, reason = await self._authorize(message)
        if not allowed:
            return self._denied("sub", reason)

        current = (await self.store.dynamic_subscriptions()).get(chat.name, [])
        added: list[str] = []
        skipped: list[str] = []
        rejected: list[str] = []

        for raw_repo in args:
            repo = raw_repo.strip().lower()
            if not _looks_like_repo(repo):
                rejected.append(f"`{raw_repo}` 格式不对（应为 `owner/repo`）")
                continue
            ok, why = self._repo_allowed(repo)
            if not ok:
                rejected.append(f"`{repo}` 被拒绝：{why}")
                continue
            if len(current) + len(added) >= self.config.commands.max_repos_per_chat:
                rejected.append(f"超过单群上限 {self.config.commands.max_repos_per_chat} 条")
                break
            if await self.store.add_dynamic_subscription(chat.name, repo, message.sender_open_id):
                added.append(repo)
            else:
                skipped.append(repo)

        lines: list[str] = []
        if added:
            lines.append("**已订阅**\n" + "\n".join(f"• `{repo}`" for repo in added))
        if skipped:
            lines.append("**本来就在**\n" + "\n".join(f"• `{repo}`" for repo in skipped))
        if rejected:
            lines.append("**未处理**\n" + "\n".join(f"• {item}" for item in rejected))
        defaults = self.config.defaults.events
        if added:
            lines.append(
                "推送范围："
                + (f"`{'、'.join(defaults)}`（继承 config 的 defaults）" if defaults else "config 的全部事件")
            )
        if not lines:
            lines.append("没有发生任何变更。")

        outcome = CommandOutcome(
            "sub", "ok", f"added={added} skipped={skipped} rejected={len(rejected)}", applied=bool(added)
        )
        outcome.reply = _card(
            f"订阅更新 · {chat.name}",
            "green" if added else "yellow",
            lines,
            note="@我 list 查看本群全部订阅",
        )
        return outcome

    async def _cmd_unsubscribe(self, message: IncomingMessage, chat: ChatConfig, args: list[str]) -> CommandOutcome:
        if not args:
            return self._usage("unsub", "用法：`@我 unsub owner/repo`")
        allowed, reason = await self._authorize(message)
        if not allowed:
            return self._denied("unsub", reason)

        removed: list[str] = []
        missing: list[str] = []
        for raw_repo in args:
            repo = raw_repo.strip().lower()
            if await self.store.remove_dynamic_subscription(chat.name, repo):
                removed.append(repo)
            else:
                missing.append(repo)

        lines: list[str] = []
        if removed:
            lines.append("**已退订**\n" + "\n".join(f"• `{repo}`" for repo in removed))
        if missing:
            lines.append("**没有这条指令订阅**\n" + "\n".join(f"• `{repo}`" for repo in missing))

        # 关键提醒：config 里的规则不受指令影响，否则用户会以为退订失效了
        still_static = [
            pattern
            for pattern in self.router.static_patterns_for_chat(chat.name)
            if any(glob_match(pattern, repo) for repo in removed)
        ]
        if still_static:
            lines.append(
                "**注意：config.yaml 里仍有规则会推这些仓库**，指令删不掉它：\n"
                + "\n".join(f"• `{pattern}`" for pattern in still_static)
                + "\n要彻底停掉请修改 `config.yaml` 的 `subscriptions` 后 `POST /admin/reload`。"
            )
        if not lines:
            lines.append("没有发生任何变更。")

        outcome = CommandOutcome("unsub", "ok", f"removed={removed} missing={missing}", applied=bool(removed))
        outcome.reply = _card(
            f"退订结果 · {chat.name}",
            "green" if removed else "yellow",
            lines,
            note="@我 list 查看本群全部订阅",
        )
        return outcome

    # --- 权限与范围校验 -------------------------------------------------
    async def _authorize(self, message: IncomingMessage) -> tuple[bool, str]:
        commands = self.config.commands
        sender = message.sender_open_id
        if not sender:
            return False, "拿不到你的 open_id，无法校验权限"
        if sender in commands.admins:
            return True, "admin"
        if not commands.allow_group_owner:
            return False, "只有 commands.admins 里的成员可以改订阅（allow_group_owner=false）"
        owner = await self._group_owner(message.chat_id)
        if owner is None:
            return False, (
                "无法确认群主身份（读取群信息需要 `im:chat:readonly` 权限且要重新发布应用版本）；"
                "也可以把你的 open_id 填进 config 的 `commands.admins`"
            )
        if owner == sender:
            return True, "owner"
        return False, f"只有群主可以改本群订阅（你的 open_id: `{sender}`，可用 `@我 whoami` 查看）"

    async def _group_owner(self, chat_id: str) -> str | None:
        cached = self._owner_cache.get(chat_id)
        now = time.monotonic()
        if cached and cached[1] > now:
            return cached[0]
        try:
            owner = await self.feishu.get_chat_owner(chat_id)
        except FeishuError as exc:
            logger.warning("获取群主失败: %s", exc)
            owner = None
        self._owner_cache[chat_id] = (owner, now + max(self.config.commands.owner_cache_seconds, 0))
        return owner

    def _repo_allowed(self, repo: str) -> tuple[bool, str]:
        allowlist = self.config.commands.repo_allowlist
        pool = allowlist or self.router.known_repo_patterns()
        if not pool:
            return False, "config 里没有任何订阅，无法判断允许范围，请先设置 `commands.repo_allowlist`"
        if any(glob_match(pattern, repo) for pattern in pool):
            return True, ""
        shown = "、".join(f"`{p}`" for p in pool[:8])
        more = f" 等 {len(pool)} 项" if len(pool) > 8 else ""
        return False, f"不在允许范围内（当前允许 {shown}{more}）"

    def _usage(self, command: str, text: str) -> CommandOutcome:
        outcome = CommandOutcome(command, "ok", "参数缺失")
        outcome.reply = _card("用法", "grey", [text, HELP_TEXT])
        return outcome

    def _denied(self, command: str, reason: str) -> CommandOutcome:
        outcome = CommandOutcome(command, "denied", reason)
        outcome.reply = _card("没有权限", "red", [reason, HELP_TEXT])
        return outcome


def _looks_like_repo(value: str) -> bool:
    if value.count("/") != 1:
        return False
    owner, name = value.split("/", 1)
    if not owner or not name:
        return False
    # 允许 * 与 ? 通配，其余必须是 GitHub 仓库名允许的字符（含作为分隔符的 /）
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_.*?/")
    return set(value) <= allowed
