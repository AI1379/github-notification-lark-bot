"""飞书回调的解析与校验。"""

from __future__ import annotations

import json

import pytest

from larkbot.feishu.events import (
    EventError,
    challenge_response,
    is_challenge,
    parse_event,
    parse_message,
    parse_payload,
    strip_mentions,
    verify_token,
)

TOKEN = "vt-secret"


def message_event(text: str = "@_user_1 help", chat_id: str = "oc_test_chat", sender: str = "ou_owner") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": "ev-1", "event_type": "im.message.receive_v1", "token": TOKEN, "create_time": "1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
            "message": {
                "message_id": "om-1",
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": [{"key": "@_user_1", "name": "larkbot", "id": {"open_id": "ou_bot"}}],
            },
        },
    }


def test_parse_payload_rejects_non_json():
    with pytest.raises(EventError, match="不是合法 JSON"):
        parse_payload(b"{oops")


def test_parse_payload_rejects_encrypted_body():
    with pytest.raises(EventError) as excinfo:
        parse_payload(json.dumps({"encrypt": "FIAfJPGR..."}))
    assert excinfo.value.code == "encrypt_unsupported"
    assert "Encrypt Key" in str(excinfo.value)  # 报错要能指导用户去关掉加密


def test_challenge_handshake():
    payload = {"type": "url_verification", "challenge": "abc-123", "token": TOKEN}
    assert is_challenge(payload) is True
    assert challenge_response(payload, TOKEN) == {"challenge": "abc-123"}


def test_challenge_with_wrong_token_is_rejected():
    payload = {"type": "url_verification", "challenge": "abc", "token": "wrong"}
    with pytest.raises(EventError) as excinfo:
        challenge_response(payload, TOKEN)
    assert excinfo.value.status == 401


def test_challenge_without_token_configured_is_allowed():
    payload = {"type": "url_verification", "challenge": "abc", "token": "whatever"}
    assert challenge_response(payload, None) == {"challenge": "abc"}


def test_challenge_missing_field():
    with pytest.raises(EventError, match="challenge 字段"):
        challenge_response({"type": "url_verification"}, None)


def test_verify_token_reads_v1_top_level_too():
    verify_token({"token": TOKEN}, TOKEN)  # v1.0 事件把 token 放在顶层
    with pytest.raises(EventError):
        verify_token({"token": "nope"}, TOKEN)


def test_parse_event_returns_type_and_id():
    payload = message_event()
    event_type, event_id, event = parse_event(payload, TOKEN)
    assert event_type == "im.message.receive_v1"
    assert event_id == "ev-1"
    assert event["message"]["message_id"] == "om-1"


def test_parse_event_falls_back_when_event_id_missing():
    payload = message_event()
    payload["header"].pop("event_id")
    _, event_id, _ = parse_event(payload, TOKEN)
    assert event_id.startswith("im.message.receive_v1:")  # 不能退化成空字符串，否则幂等表会互相覆盖


def test_parse_event_requires_type():
    with pytest.raises(EventError, match="event_type"):
        parse_event({"header": {"event_id": "x", "token": TOKEN}}, TOKEN)


def test_parse_message_strips_bot_mention():
    message = parse_message(message_event("@_user_1 sub acme/api")["event"], event_id="ev-1")
    assert message.text == "sub acme/api"
    assert message.is_group is True
    assert message.sender_open_id == "ou_owner"
    assert message.message_id == "om-1"
    assert message.chat_id == "oc_test_chat"


def test_parse_message_strips_every_mention():
    """无法区分哪个 @ 是机器人，所以所有 @占位符都清掉（指令参数里不该有 @）。"""
    event = message_event("@_user_1 sub acme/api")["event"]
    event["message"]["mentions"].append({"key": "@_user_9", "name": "张三"})
    event["message"]["content"] = json.dumps({"text": "@_user_1 sub acme/api cc @_user_9"})
    message = parse_message(event, event_id="ev-1")
    assert message.text == "sub acme/api cc"
    assert "@" not in message.text


def test_parse_message_handles_post_content():
    event = message_event()["event"]
    event["message"]["message_type"] = "post"
    event["message"]["content"] = json.dumps(
        {"title": "标题", "content": [[{"tag": "text", "text": "@_user_1"}, {"tag": "text", "text": " list"}]]}
    )
    message = parse_message(event, event_id="ev-1")
    assert message.text == "标题 list"


def test_parse_message_ignores_non_text():
    event = message_event()["event"]
    event["message"]["message_type"] = "image"
    event["message"]["content"] = json.dumps({"image_key": "img_1"})
    assert parse_message(event, event_id="ev-1").text == ""


def test_parse_message_detects_bot_sender():
    event = message_event("@_user_1 list")["event"]
    event["sender"]["sender_type"] = "bot"
    assert parse_message(event, event_id="ev-1").is_from_bot is True


def test_strip_mentions_removes_all_keys():
    assert strip_mentions("@_user_1   list", [{"key": "@_user_1"}]) == "list"
    assert strip_mentions("@_user_1 sub @_user_2", [{"key": "@_user_1"}, {"key": "@_user_2"}]) == "sub"
    assert strip_mentions("no mention", []) == "no mention"
