"""飞书客户端：两种通道的请求体、签名与错误处理（用 MockTransport 拦截）。"""

from __future__ import annotations

import json

import httpx
import pytest

from larkbot.config import ChatConfig, FeishuConfig
from larkbot.feishu.client import FeishuClient, FeishuError, webhook_sign

CARD = {"header": {"title": {"tag": "plain_text", "content": "hi"}}, "elements": []}


@pytest.fixture
def captured() -> list[httpx.Request]:
    return []


def _client(captured: list[httpx.Request], *, responses: list[httpx.Response] | None = None) -> FeishuClient:
    queue = list(responses or [])

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if queue:
            return queue.pop(0)
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-token", "expire": 7200})
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FeishuClient(FeishuConfig(app_id="cli_x", app_secret="s"), http=http)


def test_webhook_sign_algorithm():
    """官方算法：key = "timestamp\\nsecret"，内容为空，sha256 后 base64。"""
    import base64
    import hashlib
    import hmac

    expected = base64.b64encode(hmac.new(b"1700000000\nmy-secret", digestmod=hashlib.sha256).digest()).decode()
    assert webhook_sign("my-secret", "1700000000") == expected


async def test_webhook_transport_sends_card(captured):
    client = _client(captured)
    chat = ChatConfig(name="dev", webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    await client.send_card(chat, CARD)

    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert body["msg_type"] == "interactive"
    assert body["card"] == CARD
    assert "timestamp" not in body and "sign" not in body
    await client.aclose()


async def test_webhook_transport_adds_signature_when_secret_set(captured):
    client = _client(captured)
    chat = ChatConfig(
        name="dev",
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        webhook_secret="shhh",
    )
    await client.send_card(chat, CARD)
    body = json.loads(captured[0].content)
    assert body["timestamp"].isdigit()
    assert body["sign"] == webhook_sign("shhh", body["timestamp"])
    await client.aclose()


async def test_app_transport_fetches_token_then_sends(captured):
    client = _client(captured)
    chat = ChatConfig(name="rel", transport="app", chat_id="oc_123")
    await client.send_card(chat, CARD)

    assert len(captured) == 2
    assert captured[0].url.path.endswith("/open-apis/auth/v3/tenant_access_token/internal")
    assert captured[1].url.path.endswith("/open-apis/im/v1/messages")
    assert captured[1].url.params["receive_id_type"] == "chat_id"
    assert captured[1].headers["authorization"] == "Bearer t-token"
    body = json.loads(captured[1].content)
    assert body["receive_id"] == "oc_123"
    assert body["msg_type"] == "interactive"
    assert json.loads(body["content"]) == CARD


async def test_app_token_is_cached(captured):
    client = _client(captured)
    chat = ChatConfig(name="rel", transport="app", chat_id="oc_123")
    await client.send_card(chat, CARD)
    await client.send_card(chat, CARD)
    token_calls = [r for r in captured if "tenant_access_token" in r.url.path]
    assert len(token_calls) == 1
    await client.aclose()


async def test_api_error_raises_feishu_error(captured):
    client = _client(captured, responses=[httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})])
    chat = ChatConfig(name="dev", webhook_url="https://open.feishu.cn/hook/abc")
    with pytest.raises(FeishuError, match="sign match fail"):
        await client.send_card(chat, CARD)
    await client.aclose()


async def test_server_error_is_retried_once_then_raises(captured):
    client = _client(
        captured,
        responses=[httpx.Response(500, text="boom"), httpx.Response(500, text="boom")],
    )
    chat = ChatConfig(name="dev", webhook_url="https://open.feishu.cn/hook/abc")
    with pytest.raises(FeishuError) as excinfo:
        await client.send_card(chat, CARD)
    assert excinfo.value.retryable is True
    assert len(captured) == 2  # 重试了一次
    await client.aclose()


async def test_network_error_does_not_leak_webhook_token(captured):
    """httpx 异常文本里的 webhook URL 必须脱敏（URL 末段就是凭据）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection failed for {request.url}", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = FeishuClient(FeishuConfig(), http=http)
    chat = ChatConfig(name="dev", webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/supersecrettoken")
    with pytest.raises(FeishuError) as excinfo:
        await client.send_card(chat, CARD)
    message = str(excinfo.value)
    assert "supersecrettoken" not in message
    assert "****" in message
    await client.aclose()


async def test_legacy_statuscode_success_accepted(captured):
    client = _client(captured, responses=[httpx.Response(200, json={"StatusCode": 0, "StatusMessage": "success"})])
    chat = ChatConfig(name="dev", webhook_url="https://open.feishu.cn/hook/abc")
    await client.send_card(chat, CARD)  # 不应抛异常
    await client.aclose()


# --- 列群（app 通道的 chat_id 发现）--------------------------------------


def _app_client(handler) -> FeishuClient:
    return FeishuClient(
        FeishuConfig(app_id="cli_x", app_secret="s"), http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_list_chats_maps_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        assert request.url.path.endswith("/open-apis/im/v1/chats")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "has_more": False,
                    "items": [
                        {
                            "chat_id": "oc_1",
                            "name": "产品研发群",
                            "description": "desc",
                            "user_count": 12,
                            "owner_id": "ou_1",
                            "chat_mode": "group",
                            "external": False,
                        },
                        {"chat_id": "oc_2"},  # 群名为空 -> 兑底
                    ],
                },
            },
        )

    client = _app_client(handler)
    page = await client.list_chats()
    assert page.has_more is False
    assert page.chats[0].chat_id == "oc_1"
    assert page.chats[0].name == "产品研发群"
    assert page.chats[0].member_count == 12
    assert page.chats[0].external is False
    assert page.chats[1].name.startswith("(未命名群")
    assert page.chats[0].as_dict()["owner_id"] == "ou_1"
    await client.aclose()


async def test_list_all_chats_paginates_until_done():
    pages = [
        {"has_more": True, "page_token": "t2", "items": [{"chat_id": "oc_1", "name": "A"}]},
        {"has_more": False, "items": [{"chat_id": "oc_2", "name": "B"}]},
    ]
    seen_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        seen_tokens.append(request.url.params.get("page_token"))
        return httpx.Response(200, json={"code": 0, "data": pages[len(seen_tokens) - 1]})

    client = _app_client(handler)
    chats = await client.list_all_chats()
    assert [chat.chat_id for chat in chats] == ["oc_1", "oc_2"]
    assert seen_tokens == [None, "t2"]
    await client.aclose()


async def test_list_chats_respects_max_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        # 永远 has_more，考验 max_pages 是否兜住
        return httpx.Response(200, json={"code": 0, "data": {"has_more": True, "page_token": "x", "items": []}})

    client = _app_client(handler)
    chats = await client.list_all_chats(max_pages=2)
    assert chats == []
    await client.aclose()


async def test_list_chats_permission_error_is_actionable():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        return httpx.Response(200, json={"code": 99991672, "msg": "permission denied: im:chat:readonly"})

    client = _app_client(handler)
    with pytest.raises(FeishuError) as excinfo:
        await client.list_chats()
    assert "99991672" in str(excinfo.value)
    assert "im:chat:readonly" in str(excinfo.value)
    await client.aclose()


async def test_slash_command_endpoints(captured):
    """创建/列表/删除斜杠指令：路径、方法、请求体形状。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"command_id": "c1", "command": "sub", "description": {"default_value": "订阅"}}]
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _app_client(handler)
    await client.create_slash_command(
        "sub", description="订阅一个仓库", i18n={"zh_cn": "订阅一个仓库", "en_us": "Subscribe"}
    )
    items = await client.list_slash_commands()
    await client.delete_slash_command("c1")
    await client.aclose()

    assert [r.method for r in requests] == ["POST", "GET", "DELETE"]
    assert all(r.url.path.endswith("/open-apis/application/v7/app_slash_commands") for r in requests[:2])
    assert requests[2].url.path.endswith("/open-apis/application/v7/app_slash_commands/c1")

    body = json.loads(requests[0].content)
    assert body == {
        "command": "sub",
        "description": {
            "default_value": "订阅一个仓库",
            "i18n": {"zh_cn": "订阅一个仓库", "en_us": "Subscribe"},
        },
    }
    # icon 必须与 description 平级（官方 create 示例把它嵌进 description 是文档笔误）
    assert "icon" not in body["description"]
    assert items[0]["command_id"] == "c1"


async def test_create_slash_command_puts_icon_at_top_level(captured):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _app_client(handler)
    await client.create_slash_command("/help", description="帮助", icon_key="list_outlined")
    await client.aclose()
    body = json.loads(requests[0].content)
    assert body["command"] == "help"  # 接口不带前导斜杠
    assert body["icon"] == {"icon_key": "list_outlined"}


async def test_update_slash_command_is_partial(captured):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _app_client(handler)
    await client.update_slash_command("c1", description="新说明")
    await client.aclose()
    assert requests[0].method == "PATCH"
    body = json.loads(requests[0].content)
    assert body == {"description": {"default_value": "新说明"}}  # 未传的字段不发，避免误改


async def test_expired_token_is_refreshed_and_retried():
    token_requests = 0
    chat_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests, chat_requests
        if "tenant_access_token" in request.url.path:
            token_requests += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": f"t{token_requests}", "expire": 7200})
        chat_requests += 1
        if chat_requests == 1:
            return httpx.Response(200, json={"code": 99991663, "msg": "token expired"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [{"chat_id": "oc_1", "name": "A"}]}})

    client = _app_client(handler)
    page = await client.list_chats()
    assert [chat.chat_id for chat in page.chats] == ["oc_1"]
    assert token_requests == 2  # 第一次拿 token + 失效后强制刷新
    assert chat_requests == 2
    await client.aclose()
