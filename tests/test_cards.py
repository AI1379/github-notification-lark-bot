"""飞书卡片渲染。"""

from __future__ import annotations

from larkbot.feishu.cards import build_event_card, build_test_card
from larkbot.fixtures import pull_request_payload, push_payload, release_payload, workflow_run_payload
from larkbot.github.normalize import normalize_webhook


def _card(**kwargs):
    event = normalize_webhook(**kwargs)
    assert event is not None
    return build_event_card(event, timezone="Asia/Shanghai")


def _texts(card: dict) -> str:
    chunks: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("tag") in {"lark_md", "plain_text"} and isinstance(node.get("content"), str):
                chunks.append(node["content"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return "\n".join(chunks)


def test_push_card_has_blue_header_and_commits():
    card = _card(event_name="push", payload=push_payload(repo="acme/api", commit_count=2), source="webhook")
    assert card["header"]["template"] == "blue"
    assert "acme/api" in card["header"]["title"]["content"]
    body = _texts(card)
    assert "推送" in body
    assert "fix: 修复第 1 个问题" in body
    assert "`main`" in body
    assert "在 GitHub 查看" in body


def test_push_card_limits_commit_lines():
    card = _card(event_name="push", payload=push_payload(repo="acme/api", commit_count=7), source="webhook")
    body = _texts(card)
    assert "…还有 4 个提交" in body


def test_merged_pull_request_is_green():
    card = _card(
        event_name="pull_request",
        payload=pull_request_payload(repo="acme/api", number=42, action="closed", merged=True),
        source="webhook",
    )
    assert card["header"]["template"] == "green"
    body = _texts(card)
    assert "#42" in body
    assert "已合并" in body


def test_closed_pull_request_is_grey():
    card = _card(
        event_name="pull_request",
        payload=pull_request_payload(repo="acme/api", action="closed", merged=False),
        source="webhook",
    )
    assert card["header"]["template"] == "grey"


def test_release_card_uses_tag_field():
    card = _card(event_name="release", payload=release_payload(repo="acme/api", tag="v1.0.0"), source="webhook")
    assert card["header"]["template"] == "violet"
    body = _texts(card)
    assert "标签" in body
    assert "`v1.0.0`" in body


def test_workflow_run_color_follows_conclusion():
    failed = _card(
        event_name="workflow_run",
        payload=workflow_run_payload(repo="acme/api", conclusion="failure"),
        source="webhook",
    )
    passed = _card(
        event_name="workflow_run",
        payload=workflow_run_payload(repo="acme/api", conclusion="success"),
        source="webhook",
    )
    assert failed["header"]["template"] == "red"
    assert passed["header"]["template"] == "green"
    assert "失败" in _texts(failed)
    assert "成功" in _texts(passed)


def test_time_is_rendered_in_configured_timezone():
    card = _card(event_name="push", payload=push_payload(repo="acme/api"), source="webhook")
    assert "时间" in _texts(card)


def test_test_card_structure():
    card = build_test_card("dev", details={"transport": "webhook"})
    assert card["header"]["template"] == "turquoise"
    assert "dev" in _texts(card)
    assert "连通性" in _texts(card)
