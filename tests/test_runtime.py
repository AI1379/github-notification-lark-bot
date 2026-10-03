"""app 通道的联网自检（verify_app_chats）。"""

from __future__ import annotations

from larkbot.feishu.client import ChatSummary, FeishuError
from larkbot.runtime import verify_app_chats
from tests.conftest import make_config


class FakeFeishu:
    def __init__(self, groups=None, error: Exception | None = None) -> None:
        self.groups = groups or []
        self.error = error
        self.calls = 0

    async def list_all_chats(self, *, max_pages: int = 5):
        self.calls += 1
        if self.error:
            raise self.error
        return self.groups


async def test_reports_matching_and_missing_chats():
    config = make_config()  # chats: dev(webhook) + rel(app, oc_test_chat)
    feishu = FakeFeishu(
        groups=[
            ChatSummary(chat_id="oc_test_chat", name="发布群", member_count=8),
            ChatSummary(chat_id="oc_other", name="别的群"),
        ]
    )
    report = await verify_app_chats(config, feishu)

    assert report["credentials_configured"] is True
    assert report["token_ok"] is True
    assert report["groups_total"] == 2
    assert report["chats"] == [{"name": "rel", "chat_id": "oc_test_chat", "found": True, "group_name": "发布群"}]
    assert report["problems"] == []


async def test_reports_chat_id_not_in_bot_groups():
    config = make_config()
    feishu = FakeFeishu(groups=[ChatSummary(chat_id="oc_other", name="别的群")])
    report = await verify_app_chats(config, feishu)

    assert report["chats"][0]["found"] is False
    assert len(report["problems"]) == 1
    assert "不在机器人所在的群列表里" in report["problems"][0]


async def test_reports_problem_when_credentials_missing():
    config = make_config(feishu={"app_id": None, "app_secret": None})
    feishu = FakeFeishu()
    report = await verify_app_chats(config, feishu)

    assert report["credentials_configured"] is False
    assert report["token_ok"] is None
    assert feishu.calls == 0  # 没凭证就不该打接口
    assert any("缺少 feishu.app_id" in item for item in report["problems"])


async def test_reports_api_error_without_crashing():
    config = make_config()
    feishu = FakeFeishu(error=FeishuError("飞书接口返回错误: code=99991 msg=no permission"))
    report = await verify_app_chats(config, feishu)

    assert report["token_ok"] is None
    assert len(report["problems"]) == 1
    assert "获取群列表失败" in report["problems"][0]


async def test_no_app_chats_is_clean_with_credentials():
    config = make_config(
        chats=[{"name": "dev", "webhook_url": "https://x"}],
        subscriptions=[{"repos": ["acme/*"], "chats": ["dev"]}],
    )
    feishu = FakeFeishu(groups=[ChatSummary(chat_id="oc_1", name="无关群")])
    report = await verify_app_chats(config, feishu)

    assert report["token_ok"] is True  # 凭证照样能验
    assert report["chats"] == []
    assert report["problems"] == []


async def test_bot_in_no_group_is_reported():
    config = make_config()
    feishu = FakeFeishu(groups=[])
    report = await verify_app_chats(config, feishu)

    assert report["groups_total"] == 0
    assert any("先把应用机器人拉进目标群" in item for item in report["problems"])
