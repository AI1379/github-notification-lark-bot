"""群内指令：解析、权限、范围校验，以及「不覆盖 config」的语义。"""

from __future__ import annotations

import json

import pytest

from larkbot.commands import (
    HELP_TEXT,
    SLASH_COMMANDS,
    CommandHandler,
    parse_command,
    register_slash_commands,
)
from larkbot.config import Config
from larkbot.feishu.client import FeishuError
from larkbot.feishu.events import IncomingMessage
from larkbot.router import Router
from larkbot.store import StateStore
from tests.conftest import RecordingFeishu, make_config

OWNER = "ou_owner"
MEMBER = "ou_member"
ADMIN = "ou_admin"


def commands_config(**overrides):
    base = {
        "enabled": True,
        "admins": [ADMIN],
        "allow_group_owner": True,
        "repo_allowlist": [],
    }
    return {**base, **overrides}


def make_message(
    text: str,
    *,
    sender: str = OWNER,
    chat_id: str = "oc_test_chat",
    message_id: str = "om-1",
    chat_type: str = "group",
    sender_type: str = "user",
    mentions: list[dict] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        event_id="ev-1",
        message_id=message_id,
        chat_id=chat_id,
        chat_type=chat_type,
        message_type="text",
        sender_open_id=sender,
        sender_type=sender_type,
        text=text,
        # 默认带一个 @机器人 占位符：真实收到的群消息（group_at_msg 权限）总是带 @
        mentions=[{"key": "@_user_1", "name": "larkbot"}] if mentions is None else mentions,
        raw={"content": json.dumps({"text": text})},
    )


def build(
    config: Config, store: StateStore, feishu: RecordingFeishu | None = None
) -> tuple[CommandHandler, RecordingFeishu]:
    feishu = feishu or RecordingFeishu(owner=OWNER)
    router = Router(config)
    return CommandHandler(config, store, feishu, router), feishu


# --- 解析 ------------------------------------------------------------------


def test_parse_command_basic_and_aliases():
    assert parse_command("sub acme/api") == ("sub", ["acme/api"])
    assert parse_command("订阅 acme/api") == ("sub", ["acme/api"])
    assert parse_command("/help") == ("help", [])
    assert parse_command("退订 acme/api") == ("unsub", ["acme/api"])
    assert parse_command("") == (None, [])
    assert parse_command("随便说点什么") == ("随便说点什么", [])


def test_parse_command_accepts_slash_prefix():
    """飞书没有原生斜杠命令，但指令面板/用户习惯会带 /，所以要认。"""
    assert parse_command("/sub acme/api") == ("sub", ["acme/api"])
    assert parse_command("/订阅 acme/api") == ("sub", ["acme/api"])
    assert parse_command("/list") == ("list", [])
    assert parse_command("//sub acme/api") == ("sub", ["acme/api"])


def test_parse_command_handles_fullwidth_input():
    """中文输入法很容易打出全角 ／ 与 ？，不能因此认不出指令。"""
    assert parse_command("／sub acme/api") == ("sub", ["acme/api"])
    assert parse_command("／帮助") == ("help", [])
    assert parse_command("？") == ("help", [])
    assert parse_command("sub　acme/api") == ("sub", ["acme/api"])  # 全角空格


async def test_slash_prefixed_command_works_end_to_end(store):
    config = make_config(commands=commands_config())
    handler, _ = build(config, store)
    outcome = await handler.handle(make_message("/sub acme/api"))
    assert outcome.applied is True
    assert await store.dynamic_subscriptions() == {"rel": ["acme/api"]}


async def test_message_without_mention_is_ignored(store):
    """require_mention=true 时，群里没 @机器人 的话不应被当成指令。"""
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", mentions=[]))
    assert outcome.status == "ignored"
    assert await store.dynamic_subscriptions() == {}
    assert feishu.replies == []


async def test_require_mention_can_be_disabled(store):
    config = make_config(commands=commands_config(require_mention=False))
    handler, _ = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", mentions=[]))
    assert outcome.applied is True


# --- 门控与异常路径 --------------------------------------------------------


async def test_disabled_commands_are_ignored(config, store):
    handler, feishu = build(config, store)  # 默认 commands.enabled=false
    outcome = await handler.handle(make_message("sub acme/api"))
    assert outcome.status == "ignored"
    assert outcome.applied is False
    assert feishu.replies == []


async def test_bot_messages_are_ignored(store):
    config = make_config(commands=commands_config())
    handler, _ = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", sender_type="bot"))
    assert outcome.status == "ignored"


async def test_unregistered_chat_gets_actionable_reply(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", chat_id="oc_unknown"))
    assert outcome.status == "unregistered"
    assert "oc_unknown" in feishu.reply_texts()
    assert "config.yaml" in feishu.reply_texts()


async def test_owner_can_subscribe(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api"))

    assert outcome.applied is True
    assert await store.dynamic_subscriptions() == {"rel": ["acme/api"]}
    assert "已订阅" in feishu.reply_texts()
    assert feishu.replies[0][0] == "om-1"  # 回复挂在原消息下


async def test_non_owner_is_denied(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", sender=MEMBER))

    assert outcome.status == "denied"
    assert await store.dynamic_subscriptions() == {}
    assert "只有群主" in feishu.reply_texts()


async def test_admin_bypasses_owner_check(store):
    config = make_config(commands=commands_config())
    handler, _ = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", sender=ADMIN))
    assert outcome.applied is True
    assert await store.dynamic_subscriptions() == {"rel": ["acme/api"]}


async def test_owner_check_can_be_disabled(store):
    config = make_config(commands=commands_config(allow_group_owner=False))
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api"))
    assert outcome.status == "denied"
    assert "allow_group_owner=false" in feishu.reply_texts()


async def test_unknown_owner_falls_back_with_hint(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store, RecordingFeishu(owner=None))
    outcome = await handler.handle(make_message("sub acme/api"))
    assert outcome.status == "denied"
    assert "im:chat:readonly" in feishu.reply_texts()  # 告诉用户缺哪个权限


# --- 仓库范围校验 ----------------------------------------------------------


async def test_repo_outside_declared_universe_is_rejected(store):
    """默认安全值：只能订阅 config 已声明过的仓库范围。"""
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub someone/secret-repo"))

    assert outcome.applied is False
    assert await store.dynamic_subscriptions() == {}
    assert "不在允许范围内" in feishu.reply_texts()


async def test_repo_allowlist_extends_universe(store):
    config = make_config(commands=commands_config(repo_allowlist=["other/*"]))
    handler, _ = build(config, store)
    outcome = await handler.handle(make_message("sub other/thing"))
    assert outcome.applied is True
    assert await store.dynamic_subscriptions() == {"rel": ["other/thing"]}


async def test_repo_allowlist_still_blocks_others(store):
    config = make_config(commands=commands_config(repo_allowlist=["other/*"]))
    handler, _ = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api"))
    assert outcome.applied is False


async def test_bad_repo_format_is_reported(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub not-a-repo"))
    assert outcome.applied is False
    assert "格式不对" in feishu.reply_texts()


async def test_duplicate_subscribe_is_reported_not_reapplied(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    await handler.handle(make_message("sub acme/api"))
    outcome = await handler.handle(make_message("sub acme/api"))

    assert outcome.applied is False
    assert "本来就在" in feishu.reply_texts()
    assert await store.dynamic_subscriptions() == {"rel": ["acme/api"]}


async def test_max_repos_per_chat(store):
    config = make_config(commands=commands_config(repo_allowlist=["acme/*"], max_repos_per_chat=2))
    handler, feishu = build(config, store)
    await handler.handle(make_message("sub acme/a acme/b acme/c"))
    assert len((await store.dynamic_subscriptions()).get("rel", [])) == 2
    assert "超过单群上限" in feishu.reply_texts()


# --- 退订与「config 不受影响」 ---------------------------------------------


async def test_unsubscribe_removes_dynamic_rule(store):
    config = make_config(
        commands=commands_config(),
        subscriptions=[
            {"repos": ["acme/*"], "chats": ["dev"]},
            {"repos": ["acme/api"], "chats": ["rel"], "events": ["release"]},
        ],
    )
    handler, feishu = build(config, store)
    await store.add_dynamic_subscription("rel", "acme/other", OWNER)
    outcome = await handler.handle(make_message("unsub acme/other"))

    assert outcome.applied is True
    assert await store.dynamic_subscriptions() == {}
    assert "已退订" in feishu.reply_texts()


async def test_unsubscribe_warns_about_remaining_config_rules(store):
    """退订后 config 仍在推同一个仓库时必须明确告知，否则用户以为退订失效。"""
    config = make_config(
        commands=commands_config(),
        subscriptions=[{"repos": ["acme/*"], "chats": ["dev", "rel"]}],
    )
    handler, feishu = build(config, store)
    await store.add_dynamic_subscription("rel", "acme/api", OWNER)
    outcome = await handler.handle(make_message("unsub acme/api"))

    text = feishu.reply_texts()
    assert outcome.applied is True
    assert "config.yaml 里仍有规则" in text
    assert "acme/*" in text


async def test_unsubscribe_missing_rule_is_reported(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("unsub acme/api"))
    assert outcome.applied is False
    assert "没有这条指令订阅" in feishu.reply_texts()


# --- list / whoami / help --------------------------------------------------


async def test_list_shows_both_sources(store):
    config = make_config(
        commands=commands_config(),
        subscriptions=[{"repos": ["acme/static-repo"], "chats": ["rel"]}],
    )
    handler, feishu = build(config, store)
    await store.add_dynamic_subscription("rel", "acme/dynamic-repo", OWNER)
    outcome = await handler.handle(make_message("list"))

    text = feishu.reply_texts()
    assert outcome.status == "ok"
    assert "acme/static-repo" in text  # config 规则
    assert "acme/dynamic-repo" in text  # 指令订阅
    assert "config 里的规则" in text
    assert "指令创建的订阅" in text


async def test_whoami_prints_ids(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    await handler.handle(make_message("whoami", sender=MEMBER))
    text = feishu.reply_texts()
    assert MEMBER in text
    assert "oc_test_chat" in text


async def test_help_lists_commands(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    await handler.handle(make_message("help"))
    assert "sub <owner/repo>" in feishu.reply_texts()


async def test_unknown_command_shows_help(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("deploy production"))
    assert outcome.status == "ok"
    assert "没认出这条指令" in feishu.reply_texts()


async def test_usage_when_args_missing(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub"))
    assert outcome.applied is False
    assert "用法" in feishu.reply_texts()


async def test_reply_can_be_disabled(store):
    config = make_config(commands=commands_config(reply=False))
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api"))
    assert outcome.applied is True
    assert feishu.replies == []  # 静默执行，但状态库里已经生效
    assert await store.dynamic_subscriptions() == {"rel": ["acme/api"]}


async def test_p2p_chat_is_unregistered(store):
    config = make_config(commands=commands_config())
    handler, feishu = build(config, store)
    outcome = await handler.handle(make_message("sub acme/api", chat_id="oc_p2p", chat_type="p2p"))
    assert outcome.status == "unregistered"
    assert feishu.replies != []


# --- Slash Command 注册表与帮助文本的一致性 ---------------------------------


def test_help_text_is_generated_from_catalogue():
    """帮助文案必须由注册表生成，否则两边会漂移。"""
    for spec in SLASH_COMMANDS:
        assert f"`@我 {spec.usage}`" in HELP_TEXT
        assert spec.description in HELP_TEXT


def test_every_registered_command_is_actually_handled():
    from larkbot.commands import KNOWN_COMMANDS

    for spec in SLASH_COMMANDS:
        assert spec.command in KNOWN_COMMANDS
        parsed, _ = parse_command(spec.slash)
        assert parsed == spec.command  # 面板发过来的 `/sub` 能解析到 `sub`


def test_slash_command_body_has_no_leading_slash():
    for spec in SLASH_COMMANDS:
        body = spec.to_body()
        assert not body["command"].startswith("/")
        assert body["description"]["default_value"]
        assert body["description"]["i18n"]["zh_cn"] == spec.description


class FakeSlashFeishu:
    def __init__(self, existing=None, fail_on=None) -> None:
        self.existing = existing or []
        self.fail_on = fail_on
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    async def list_slash_commands(self):
        return self.existing

    async def create_slash_command(self, command, *, description, i18n=None, icon_key=None):
        if self.fail_on == command:
            raise FeishuError(f"模拟失败: {command}")
        self.created.append({"command": command, "description": description})
        return {"code": 0}

    async def update_slash_command(self, command_id, *, command=None, description=None, i18n=None, icon_key=None):
        self.updated.append((command_id, {"command": command, "description": description}))
        return {"code": 0}


async def test_register_creates_missing_commands():
    feishu = FakeSlashFeishu()
    report = await register_slash_commands(feishu)
    assert report.created == [spec.slash for spec in SLASH_COMMANDS]
    assert report.skipped == [] and report.updated == [] and report.failed == []
    assert len(feishu.created) == len(SLASH_COMMANDS)


async def test_register_skips_existing_by_default():
    feishu = FakeSlashFeishu(existing=[{"command_id": "c1", "command": "/sub"}])
    report = await register_slash_commands(feishu)
    assert "/sub" in report.skipped
    assert "/sub" not in report.created
    assert all(c["command"] != "sub" for c in feishu.created)


async def test_register_force_updates_existing():
    feishu = FakeSlashFeishu(existing=[{"command_id": "c1", "command": "sub"}])
    report = await register_slash_commands(feishu, force=True)
    assert report.updated == ["/sub"]
    assert feishu.updated[0][0] == "c1"
    assert feishu.updated[0][1]["description"]


async def test_register_reports_failures_without_crashing():
    feishu = FakeSlashFeishu(fail_on="list")
    report = await register_slash_commands(feishu)
    assert [item["command"] for item in report.failed] == ["/list"]
    assert "/sub" in report.created  # 其它指令不受影响


async def test_register_dry_run_touches_nothing():
    report = await register_slash_commands(None, dry_run=True)
    assert len(report.bodies) == len(SLASH_COMMANDS)
    assert not report.created and not report.skipped
    assert report.bodies[0]["command"] == "help"


async def test_register_requires_client_when_not_dry_run():
    with pytest.raises(ValueError, match="必须传入 feishu"):
        await register_slash_commands(None)


async def test_slash_registered_commands_work_through_handler(store):
    """注册到面板的指令，必须真的能被处理（子集即可，参数靠用户输入）。"""
    config = make_config(commands=commands_config())
    handler, _ = build(config, store)
    assert (await handler.handle(make_message("/help"))).command == "help"
    assert (await handler.handle(make_message("/list"))).command == "list"
    assert (await handler.handle(make_message("/whoami"))).command == "whoami"
    assert (await handler.handle(make_message("/sub acme/api"))).applied is True
    assert (await handler.handle(make_message("/unsub acme/api"))).applied is True
