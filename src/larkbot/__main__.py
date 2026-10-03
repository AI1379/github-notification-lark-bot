"""``larkbot`` 命令行入口。

larkbot serve              启动 HTTP 服务（webhook 入口 + 后台轮询）
larkbot poll --loop        只跑轮询（也负责重试失败的投递）
larkbot check --verify     校验配置，可选联网验证 app 通道
larkbot chats --yaml       列出应用机器人所在的群，生成可粘贴的配置片段
larkbot subs               查看群内指令创建的订阅（存在状态库里）
larkbot slash --register   把指令注册到客户端的 `/` 面板
larkbot send-test dev      往某个群发一条测试卡片
larkbot simulate --event pr --print-card
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from typing import Any

from . import __version__
from .commands import register_slash_commands
from .config import ConfigError, load_config
from .console import render_chats_yaml, render_table
from .feishu.cards import build_event_card
from .feishu.client import FeishuClient, FeishuError
from .fixtures import SCENARIOS
from .fixtures import build as build_fixture
from .github.normalize import normalize_webhook
from .runtime import BotRuntime, verify_app_chats
from .util import setup_logging


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    common.add_argument("--store", default=None, help="SQLite 状态库路径（默认 data/larkbot.db）")
    common.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING")

    parser = argparse.ArgumentParser(
        prog="larkbot",
        description="GitHub -> 飞书通知机器人（webhook + 轮询回退，按群订阅仓库）",
    )
    parser.add_argument("--version", action="version", version=f"larkbot {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", parents=[common], help="启动 HTTP 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true", help="代码变更自动重载（开发用）")
    serve.add_argument("--no-poller", action="store_true", help="不启动后台轮询")

    poll = subparsers.add_parser("poll", parents=[common], help="立即跑一轮轮询并打印结果")
    poll.add_argument("--loop", action="store_true", help="持续轮询（Ctrl-C 退出）")
    poll.add_argument("--repo", action="append", default=None, help="只轮询指定仓库，可重复")

    subparsers.add_parser("check", parents=[common], help="校验配置并打印摘要").add_argument(
        "--verify",
        action="store_true",
        help="联网验证 app 通道：凭证换取 token + 配置的 chat_id 是否在机器人在的群里",
    )

    chats = subparsers.add_parser("chats", parents=[common], help="列出应用机器人所在的飞书群（用于获取 chat_id）")
    chats.add_argument("--json", action="store_true", help="输出 JSON")
    chats.add_argument("--yaml", action="store_true", help="输出可直接粘进 config.yaml 的 chats 片段")
    chats.add_argument("--max-pages", type=int, default=5, help="最多翻几页（默认 5，每页 100）")

    subs = subparsers.add_parser(
        "subs", parents=[common], help="查看/维护群内指令创建的订阅（存在状态库里，不是 config）"
    )
    subs.add_argument("--json", action="store_true", help="输出 JSON")
    subs.add_argument("--add", action="append", default=None, metavar="CHAT:REPO", help="新增，可重复")
    subs.add_argument("--remove", action="append", default=None, metavar="CHAT:REPO", help="删除，可重复")

    slash = subparsers.add_parser(
        "slash", parents=[common], help="管理客户端的 `/` 指令面板（Slash Command，可发现性优化）"
    )
    slash.add_argument("--json", action="store_true", help="列表用 JSON 输出")
    slash.add_argument("--register", action="store_true", help="注册/刷新 larkbot 的斜杠指令（幂等）")
    slash.add_argument("--force", action="store_true", help="已有同名指令时更新它，而不是跳过")
    slash.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求体，不调任何接口")
    slash.add_argument("--delete", metavar="COMMAND_ID", default=None, help="按 command_id 删除一个指令")

    send_test = subparsers.add_parser("send-test", parents=[common], help="向指定群发送测试卡片")
    send_test.add_argument("chat", help="chat 名称（config 里的 chats[].name）")
    send_test.add_argument("--dry-run", action="store_true")

    simulate = subparsers.add_parser("simulate", parents=[common], help="本地构造 GitHub 事件走完整链路")
    simulate.add_argument("--event", default="push", choices=sorted(SCENARIOS))
    simulate.add_argument("--repo", default="octocat/Hello-World")
    simulate.add_argument("--actor", default="octocat")
    simulate.add_argument("--branch", default="main")
    simulate.add_argument("--action", default=None, help="覆盖 action，如 closed / synchronize")
    simulate.add_argument("--conclusion", default=None, help="workflow_run 的结论，如 success / failure / timed_out")
    simulate.add_argument("--merged", action="store_true", help="PR 已合并")
    simulate.add_argument("--draft", action="store_true", help="草稿 PR")
    simulate.add_argument("--send", action="store_true", help="真的推到飞书（默认只演练）")
    simulate.add_argument(
        "--print-card", action="store_true", help="打印渲染后的卡片 JSON（stdout 只输出 JSON，可直接接 jq）"
    )
    return parser


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    config = load_config(args.config)
    if args.config:
        os.environ["LARKBOT_CONFIG"] = args.config
    if args.store:
        os.environ["LARKBOT_DB"] = args.store
    host = args.host or config.server.host
    port = args.port or config.server.port

    print(json.dumps(config.summary(), ensure_ascii=False, indent=2))
    print(f"\nwebhook 地址: http://{host}:{port}/webhooks/github")
    print(f"健康检查:     http://{host}:{port}/healthz")
    print(f"状态总览:     http://{host}:{port}/status")
    if not config.github.webhook_secret:
        print("提示: 未设置 github.webhook_secret，webhook 将不做签名校验。")
    if args.no_poller or config.github.poll_mode == "never":
        print("提示: 后台轮询未启用。")

    os.environ["LARKBOT_NO_POLLER"] = "1" if args.no_poller else ""
    uvicorn.run(
        "larkbot.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level=(args.log_level or "info").lower(),
    )
    return 0


_APP_PERMISSION_HINT = """常见原因（按排查顺序）:
  1. 应用没开启「机器人」能力：开发者后台 → 应用能力 → 添加「机器人」
  2. 缺权限：权限管理 → 开通 im:chat:readonly（或 im:chat），发送消息需 im:message:send_as_bot
  3. 改完能力/权限没有「创建版本并发布」—— 不发布不生效
  4. App ID / App Secret 不对，或应用未在企业内启用
  5. 机器人还没被拉进任何群：接口不报错但列表为空，需在群里 设置 → 群机器人 → 添加机器人"""


async def _chats(args: argparse.Namespace) -> int:
    config = load_config(args.config, lenient=True)
    if not (config.feishu.app_id and config.feishu.app_secret):
        print(
            "需要自建应用凭证才能列出群：请在 .env 里填 FEISHU_APP_ID / FEISHU_APP_SECRET，"
            "并确认 config 里用 env: 引用了它们。",
            file=sys.stderr,
        )
        return 2

    async with FeishuClient(config.feishu) as feishu:
        try:
            chats = await feishu.list_all_chats(max_pages=args.max_pages)
        except FeishuError as exc:
            print(f"调用飞书群列表接口失败: {exc}", file=sys.stderr)
            print(f"\n{_APP_PERMISSION_HINT}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps([chat.as_dict() for chat in chats], ensure_ascii=False, indent=2))
        return 0
    if args.yaml:
        print(render_chats_yaml(chats))
        return 0

    if not chats:
        print("机器人当前不在任何群里。")
        print("把机器人拉进群：群 设置 → 群机器人 → 添加机器人 → 选择你的应用")
        return 0
    rows = [
        [
            chat.name,
            chat.chat_id,
            str(chat.member_count if chat.member_count is not None else "-"),
            chat.chat_mode or "-",
            "是" if chat.external else "否",
        ]
        for chat in chats
    ]
    print(render_table(["群名称", "chat_id", "成员", "群模式", "外部群"], rows))
    print(f"\n共 {len(chats)} 个群。用 `larkbot chats --yaml` 生成可粘贴的 chats 配置片段。")
    return 0


async def _subs(args: argparse.Namespace) -> int:
    runtime = await BotRuntime.build(config_path=args.config, store_path=args.store, lenient=True)
    try:
        changed: list[str] = []
        for item in args.add or []:
            chat_name, _, repo = item.partition(":")
            if not chat_name or not repo:
                print(f"--add 需要 CHAT:REPO 格式，收到: {item}", file=sys.stderr)
                return 2
            if runtime.config.chat(chat_name) is None:
                print(f"config 里没有这个 chat 别名: {chat_name}", file=sys.stderr)
                return 2
            created = await runtime.store.add_dynamic_subscription(chat_name, repo.strip().lower(), "cli")
            changed.append(f"{'add' if created else 'already'} {chat_name}:{repo}")
        for item in args.remove or []:
            chat_name, _, repo = item.partition(":")
            if not chat_name or not repo:
                print(f"--remove 需要 CHAT:REPO 格式，收到: {item}", file=sys.stderr)
                return 2
            removed = await runtime.store.remove_dynamic_subscription(chat_name, repo.strip().lower())
            changed.append(f"{'removed' if removed else 'not-found'} {chat_name}:{repo}")

        rows = await runtime.store.list_dynamic_subscriptions()
        for line in changed:
            print(line, file=sys.stderr)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("没有任何指令创建的订阅（群内 `@机器人 sub owner/repo` 新增）。")
        else:
            table_rows = [
                [row["chat"], row["repo"], row.get("created_by") or "-", (row.get("created_at") or "")[:19]]
                for row in rows
            ]
            print(render_table(["群别名", "仓库", "创建者", "时间(UTC)"], table_rows))
            print(f"\n共 {len(rows)} 条。config.yaml 里的规则不在这个表里，也不会被这里的操作影响。")
    finally:
        await runtime.aclose()
    return 0


async def _slash(args: argparse.Namespace) -> int:
    if args.dry_run and args.register:
        # 不需要凭证、不联网：只想看请求体长什么样
        report = await register_slash_commands(None, dry_run=True)
        print(json.dumps(report.bodies, ensure_ascii=False, indent=2))
        print("\n[dry-run] 未调用任何接口。", file=sys.stderr)
        return 0

    config = load_config(args.config, lenient=True)
    if not (config.feishu.app_id and config.feishu.app_secret):
        print("需要自建应用凭证：请在 .env 里填 FEISHU_APP_ID / FEISHU_APP_SECRET。", file=sys.stderr)
        return 2

    async with FeishuClient(config.feishu) as feishu:
        try:
            if args.delete:
                await feishu.delete_slash_command(args.delete)
                print(f"已删除指令: {args.delete}")
                return 0
            if args.register:
                report = await register_slash_commands(feishu, force=args.force)
                print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
                if report.failed:
                    print(
                        f"\n{_APP_PERMISSION_HINT}\n另：注册斜杠指令需要 application:app_slash_command:write 权限。",
                        file=sys.stderr,
                    )
                    return 1
                if report.created or report.updated:
                    print(
                        "\n提示：面板生效可能需要几分钟；客户端要求 PC 7.70+ / 移动 7.71+。",
                        file=sys.stderr,
                    )
                return 0

            commands = await feishu.list_slash_commands()
        except FeishuError as exc:
            print(f"调用飞书接口失败: {exc}", file=sys.stderr)
            print(f"\n{_APP_PERMISSION_HINT}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(commands, ensure_ascii=False, indent=2))
        return 0
    if not commands:
        print("还没有注册任何斜杠指令。用 `larkbot slash --register` 注册 larkbot 的指令。")
        return 0
    rows = [
        [
            str(item.get("command") or ""),
            _slash_description(item.get("description")),
            str(item.get("command_id") or ""),
        ]
        for item in commands
    ]
    print(render_table(["指令", "说明", "command_id"], rows))
    print(f"\n共 {len(commands)} 条。`larkbot slash --register` 可补齐缺失的 larkbot 指令。")
    return 0


def _slash_description(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("default_value")
        return str(text) if text else "-"
    return str(value) if value else "-"


async def _check(args: argparse.Namespace) -> int:
    config = load_config(args.config, lenient=True)
    problems: list[str] = []

    if config.used_example_file:
        problems.append(
            f"当前用的是示例配置 {config.source_path}；建议 cp config/config.example.yaml config/config.yaml 后修改"
        )
    problems.extend(config.warnings)
    for chat in config.active_chats:
        if chat.transport == "webhook" and not str(chat.webhook_url).startswith("http"):
            problems.append(f"chat {chat.name}: webhook_url 看起来不是合法 URL")
        if chat.transport == "app" and not (config.feishu.app_id and config.feishu.app_secret):
            problems.append(f"chat {chat.name}: 使用 transport=app，但缺少 feishu.app_id/app_secret")
    if not config.github.webhook_secret:
        if config.github.allow_unsigned_webhooks:
            problems.append(
                "未配置 github.webhook_secret 但 allow_unsigned_webhooks=true：webhook 不验签，"
                "任何人都能伪造事件。仅限本地调试！"
            )
        else:
            problems.append(
                "未配置 github.webhook_secret：webhook 入口会返回 503 拒绝所有请求（这是安全默认值）；"
                "填好 GITHUB_WEBHOOK_SECRET 后即可"
            )
    if not config.github.enabled or config.github.poll_mode == "never":
        problems.append("轮询被关闭：webhook 一旦漏投就没有兜底")
    if not config.subscriptions:
        problems.append("没有任何 subscriptions，不会有消息被推送")
    if not config.active_chats:
        problems.append("没有任何可用（enabled 且有凭证）的 chat，消息无处可发")
    if config.commands.enabled:
        if not config.feishu.verification_token:
            problems.append("commands.enabled=true 但缺少 feishu.verification_token，飞书回调会被拒（401）")
        if not [chat for chat in config.active_chats if chat.transport == "app"]:
            problems.append(
                "commands.enabled=true 但没有任何 transport=app 的群；自定义机器人（webhook）是单向的，收不到指令"
            )
        if not (config.feishu.app_id and config.feishu.app_secret):
            problems.append("commands.enabled=true 但缺少 app 凭证，无法回复指令")
    if isinstance(config.defaults.actions, list) and config.defaults.actions:
        problems.append(
            "defaults.actions 用的是列表写法：它会作用于**所有**事件类型，"
            "收窄 PR 的同时容易连带丢掉 issue_comment(created) / release(published)；"
            "推荐按事件类型写：actions: {pull_request: [opened, closed, merged]}"
        )

    verify_report: dict[str, Any] | None = None
    if args.verify:
        if not (config.feishu.app_id and config.feishu.app_secret):
            problems.append("--verify 需要 FEISHU_APP_ID / FEISHU_APP_SECRET，当前未配置")
        else:
            async with FeishuClient(config.feishu) as feishu:
                verify_report = await verify_app_chats(config, feishu)
            for item in verify_report["problems"]:
                if item not in problems:
                    problems.append(item)

    print(json.dumps(config.summary(), ensure_ascii=False, indent=2))
    print("\n检查结果:")
    if problems:
        for item in problems:
            print(f"  [!] {item}")
    else:
        print("  [OK] 配置看起来没问题")

    if verify_report is not None:
        print("\n联网验证（自建应用通道）:")
        if verify_report["token_ok"] is None:
            print("  [!] 未能完成验证（凭证缺失或接口报错，详见上面的 [!]）")
        else:
            print(f"  [OK] 凭证可用，机器人当前在 {verify_report['groups_total']} 个群里")
            for item in verify_report["chats"]:
                if item["found"]:
                    print(f"  [OK] chat {item['name']} ({item['chat_id']}) → 群「{item['group_name']}」")
                else:
                    print(f"  [!] chat {item['name']} ({item['chat_id']}) → 不在机器人所在的群列表里")
            if not verify_report["chats"]:
                print("  - 没有使用 transport=app 的群（webhook 通道无法远程验证，用 send-test 测）")
            if verify_report["groups_total"]:
                names = "、".join(group["name"] for group in verify_report["groups"][:10])
                print(f"  机器人所在的群: {names}")

    concrete = sorted({repo for sub in config.subscriptions if sub.enabled for repo in sub.repos if "*" not in repo})
    print(f"\n订阅里写死的仓库（会直接进入轮询列表）: {concrete or '无'}")
    print("通配订阅（org/*）只对「已知仓库」生效：需要先收到一次 webhook，或写进 github.poll_repos")
    print(f"\n结论: {'存在问题，请先处理上面的 [!] 项' if problems else '可以启动服务'}")
    return 1 if problems else 0


async def _poll(args: argparse.Namespace) -> int:
    runtime = await BotRuntime.build(config_path=args.config, store_path=args.store)
    try:
        if args.repo:
            reports = [await runtime.poller.poll_repo(repo) for repo in args.repo]
        elif args.loop:
            stop = asyncio.Event()
            with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
                await runtime.poller.run_forever(stop)
            return 0
        else:
            reports = await runtime.poller.poll_once()
        print(json.dumps([report.as_dict() for report in reports], ensure_ascii=False, indent=2))
    finally:
        await runtime.aclose()
    return 0


async def _send_test(args: argparse.Namespace) -> int:
    runtime = await BotRuntime.build(
        config_path=args.config, store_path=args.store, dry_run=args.dry_run, lenient=args.dry_run
    )
    try:
        outcome = await runtime.service.send_test(args.chat)
    finally:
        await runtime.aclose()
    print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
    return 0 if outcome.status != "failed" else 1


async def _simulate(args: argparse.Namespace) -> int:
    runtime = await BotRuntime.build(
        config_path=args.config, store_path=args.store, dry_run=not args.send, lenient=True
    )
    try:
        if runtime.config.warnings:
            for item in runtime.config.warnings:
                print(f"[!] {item}", file=sys.stderr)
        overrides: dict[str, Any] = {
            "repo": args.repo,
            "actor": args.actor,
            "branch": args.branch,
            "action": args.action,
            "conclusion": args.conclusion,
            "merged": args.merged or None,
            "draft": args.draft or None,
        }
        event_name, payload = build_fixture(args.event, **overrides)
        event = normalize_webhook(event_name, payload, source="manual")
        if event is None:
            print(f"该事件未被归一化: {event_name}", file=sys.stderr)
            return 1

        if args.print_card:
            print(
                json.dumps(
                    build_event_card(event, timezone=runtime.config.server.timezone), ensure_ascii=False, indent=2
                )
            )

        outcome = await runtime.service.dispatch(event)
        # stdout 只输出 JSON，方便 `larkbot simulate ... | jq`；提示信息一律走 stderr
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
        if not outcome.targets:
            print(
                "\n没有任何群匹配这条事件。请检查 config 里的 subscriptions"
                "（仓库通配、events、branches、ignore_actors）。",
                file=sys.stderr,
            )
        if not args.send:
            print("[dry-run] 没有真正发消息；加 --send 才会推送到飞书。", file=sys.stderr)
        return 0
    finally:
        await runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        if args.command == "serve":
            return _serve(args)
        coroutine = {
            "check": _check,
            "chats": _chats,
            "subs": _subs,
            "slash": _slash,
            "poll": _poll,
            "send-test": _send_test,
            "simulate": _simulate,
        }[args.command]
        return asyncio.run(coroutine(args))
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
