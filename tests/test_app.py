"""HTTP 层：webhook 入口、签名校验、去重、管理接口。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from larkbot.app import create_app, extract_repo
from larkbot.github.webhook import compute_signature
from tests.conftest import make_config

SECRET = "test-secret"


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    @property
    def feishu(self) -> list[httpx.Request]:
        return [request for request in self.requests if "api.github.com" not in str(request.url)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "api.github.com" in str(request.url):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"code": 0, "msg": "success"})


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def client(tmp_path, recorder):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    app = create_app(
        make_config(),
        store_path=tmp_path / "app.db",
        start_poller=False,
        http_client=http,
    )
    with TestClient(app) as test_client:
        yield test_client


def post_event(
    client, payload, *, event="push", delivery="d-1", signature=None, secret: str | None = SECRET, headers=None
):
    body = json.dumps(payload).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        **(headers or {}),
    }
    if signature is None and secret:
        signature = compute_signature(secret, body)
    if signature:
        request_headers["X-Hub-Signature-256"] = signature
    return client.post("/webhooks/github", content=body, headers=request_headers)


def push_payload(**kwargs):
    from larkbot.fixtures import push_payload as build

    return build(repo="acme/api", **kwargs)


def test_healthz_and_index(client):
    assert client.get("/healthz").json()["status"] == "ok"
    index = client.get("/").json()
    assert index["name"] == "larkbot"
    assert "dev" in index["chats"]


def test_ping_returns_pong(client):
    response = post_event(client, {"zen": "hi", "hook_id": 1}, event="ping")
    assert response.status_code == 200
    assert response.json()["msg"] == "pong"


def test_push_event_creates_feishu_request(client, recorder):
    response = post_event(client, push_payload())
    assert response.status_code == 200
    payload = response.json()
    assert payload["received"] == 1
    assert payload["delivered"] == 1
    assert payload["repo"] == "acme/api"
    assert len(recorder.feishu) == 1
    body = json.loads(recorder.feishu[0].content)
    assert body["msg_type"] == "interactive"
    assert "代码推送" in body["card"]["header"]["title"]["content"]


def test_duplicate_delivery_is_deduped(client, recorder):
    assert post_event(client, push_payload(), delivery="d-1").json()["delivered"] == 1
    second = post_event(client, push_payload(), delivery="d-2").json()
    assert second["delivered"] == 0
    assert second["skipped_duplicate"] == 1
    assert len(recorder.feishu) == 1  # 第二次没有真的发


def test_bad_signature_is_rejected(client, recorder):
    response = post_event(client, push_payload(), signature="sha256=deadbeef")
    assert response.status_code == 401
    assert recorder.feishu == []


def test_missing_signature_header_is_rejected(client):
    response = post_event(client, push_payload(), signature="")
    assert response.status_code == 401


def test_invalid_json_body_is_rejected(client):
    body = b"{not json"
    response = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": compute_signature(SECRET, body),
        },
    )
    assert response.status_code == 400


def test_unsigned_webhook_rejected_by_default(tmp_path, recorder):
    """没配 secret 时必须拒掉，否则端口一公开任何人都能伪造事件。"""
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    app = create_app(
        make_config(github={"webhook_secret": None}),
        store_path=tmp_path / "nodb.db",
        start_poller=False,
        http_client=http,
    )
    with TestClient(app) as client:
        response = post_event(client, push_payload(), secret=None)
        assert response.status_code == 503
        assert "webhook_secret" in response.json()["detail"]
        assert recorder.feishu == []  # 没有转发到飞书


def test_unsigned_webhook_allowed_only_when_explicitly_enabled(tmp_path, recorder):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    app = create_app(
        make_config(github={"webhook_secret": None, "allow_unsigned_webhooks": True}),
        store_path=tmp_path / "nodb.db",
        start_poller=False,
        http_client=http,
    )
    with TestClient(app) as client:
        response = post_event(client, push_payload(), secret=None)
        assert response.status_code == 200
        assert response.json()["delivered"] == 1


def test_unmatched_repo_delivers_nothing(client, recorder):
    from larkbot.fixtures import push_payload as build

    response = post_event(client, build(repo="nobody/repo"))
    assert response.json()["delivered"] == 0
    assert recorder.feishu == []


def test_status_endpoint_reports_state(client):
    post_event(client, push_payload())
    status = client.get("/status").json()
    assert status["config"]["poll_mode"] == "auto"
    assert status["stats"]["delivery"]["delivered"] == 1
    assert status["store"]["known_repos"] == 1
    assert [row["repo"] for row in status["repos"]] == ["acme/api"]
    # 不应泄漏任何密钥
    assert SECRET not in json.dumps(status)


def test_admin_endpoints_require_token(client):
    assert client.post("/admin/reload").status_code == 401
    assert client.post("/admin/poll", headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_admin_test_sends_card(client, recorder):
    response = client.post("/admin/test/dev", headers={"X-Admin-Token": "admin-token"})
    assert response.status_code == 200
    assert response.json()["result"]["status"] == "delivered"
    assert len(recorder.feishu) == 1


def test_admin_test_unknown_chat_returns_404(client):
    response = client.post("/admin/test/nope", headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 404


def test_admin_poll_runs_baseline(client):
    response = client.post("/admin/poll", headers={"X-Admin-Token": "admin-token"})
    assert response.status_code == 200
    reports = response.json()["reports"]
    assert [report["repo"] for report in reports] == ["acme/api"]
    assert reports[0]["status"] == "baseline"


def test_admin_reload_reloads_config(client, tmp_path, monkeypatch):
    monkeypatch.setenv("LARKBOT_CONFIG", str(tmp_path / "missing.yaml"))
    response = client.post("/admin/reload", headers={"X-Admin-Token": "admin-token"})
    # 配置路径指到不存在的文件 -> 400，而不是 500
    assert response.status_code == 400


def test_admin_disabled_without_token(tmp_path, recorder):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    app = create_app(
        make_config(server={"admin_token": None}),
        store_path=tmp_path / "nodb2.db",
        start_poller=False,
        http_client=http,
    )
    with TestClient(app) as client:
        assert client.post("/admin/reload").status_code == 403


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"repository": {"full_name": "a/b"}}, "a/b"),
        ({"repo": {"name": "c/d"}}, "c/d"),
        ({}, None),
        ({"repository": {}}, None),
    ],
)
def test_extract_repo(payload, expected):
    assert extract_repo(payload) == expected


# --- 飞书回调（群内指令）--------------------------------------------------

VT = "vt-secret"
OWNER_OPEN_ID = "ou_owner"


def feishu_event(
    text: str = "@_user_1 list",
    *,
    event_id: str = "ev-1",
    chat_id: str = "oc_test_chat",
    sender: str = OWNER_OPEN_ID,
) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1", "token": VT, "create_time": "1"},
        "event": {
            "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
            "message": {
                "message_id": f"om-{event_id}",
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "mentions": [{"key": "@_user_1", "name": "larkbot"}],
            },
        },
    }


@pytest.fixture
def command_client(tmp_path, recorder):
    """开启指令的 app，飞书群 rel 的 chat_id = oc_test_chat，群主 = OWNER_OPEN_ID。"""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(200, json=[])
        if "tenant_access_token" in url:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        if "/open-apis/im/v1/chats/oc_test_chat" in url:
            return httpx.Response(200, json={"code": 0, "data": {"chat_id": "oc_test_chat", "owner_id": OWNER_OPEN_ID}})
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    config = make_config(
        feishu={"verification_token": VT},
        commands={"enabled": True, "allow_group_owner": True, "repo_allowlist": [], "admins": []},
        # 只有 dev 有 config 规则；rel 完全靠群内指令
        subscriptions=[{"repos": ["acme/*"], "chats": ["dev"]}],
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = create_app(config, store_path=tmp_path / "cmd.db", start_poller=False, http_client=http)
    with TestClient(app) as client:
        yield client


def test_challenge_handshake(command_client):
    response = command_client.post(
        "/webhooks/feishu", json={"type": "url_verification", "challenge": "abc-123", "token": VT}
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc-123"}


def test_challenge_with_bad_token_is_rejected(command_client):
    response = command_client.post(
        "/webhooks/feishu", json={"type": "url_verification", "challenge": "abc", "token": "wrong"}
    )
    assert response.status_code == 401


def test_event_with_bad_token_is_rejected(command_client):
    payload = feishu_event()
    payload["header"]["token"] = "wrong"
    assert command_client.post("/webhooks/feishu", json=payload).status_code == 401


def test_encrypted_callback_gets_actionable_400(command_client):
    response = command_client.post("/webhooks/feishu", json={"encrypt": "FIAfJPGRmFZWkaxPQ1XrJZVb"})
    assert response.status_code == 400
    assert "Encrypt Key" in response.json()["detail"]


def test_duplicate_callback_is_ignored(command_client, recorder):
    payload = feishu_event("@_user_1 help")
    assert command_client.post("/webhooks/feishu", json=payload).status_code == 200
    first_replies = len(recorder.feishu)
    second = command_client.post("/webhooks/feishu", json=payload)
    assert second.json()["msg"] == "duplicate ignored"
    assert len(recorder.feishu) == first_replies  # 没有重复回复


def test_command_creates_subscription_and_routes_events(command_client, recorder):
    """完整闭环：群里 @我 sub → 该群开始收到这个仓库的推送。"""
    # 1) 指令前：acme/api 只进 dev
    before = post_event(command_client, push_payload(), delivery="d-1").json()
    assert before["delivered"] == 1

    # 2) 群主在 rel 群发指令
    response = command_client.post("/webhooks/feishu", json=feishu_event("@_user_1 sub acme/api"))
    assert response.status_code == 200
    reply_calls = [r for r in recorder.feishu if "/reply" in str(r.url)]
    assert len(reply_calls) == 1
    assert "已订阅" in reply_calls[0].content.decode("utf-8")

    # 3) 指令后：同一条事件多进一个群（按新事件的 dedup_key）
    after = post_event(
        command_client,
        push_payload(commit_count=3, branch="feature/x"),
        delivery="d-2",
    ).json()
    assert after["delivered"] == 2
    assert {item["chat"] for item in after["detail"][0]["results"]} == {"dev", "rel"}


def test_non_owner_command_is_denied(command_client, recorder):
    command_client.post("/webhooks/feishu", json=feishu_event("@_user_1 sub acme/api", sender="ou_someone"))
    reply_calls = [r for r in recorder.feishu if "/reply" in str(r.url)]
    assert "只有群主" in reply_calls[0].content.decode("utf-8")

    # 订阅没有生效：事件仍然只进 dev
    result = post_event(command_client, push_payload(), delivery="d-9").json()
    assert result["delivered"] == 1


def test_status_exposes_dynamic_subscriptions(command_client):
    command_client.post("/webhooks/feishu", json=feishu_event("@_user_1 sub acme/api"))
    status = command_client.get("/status").json()
    assert status["dynamic_subscriptions"] == [
        {"chat": "rel", "repo": "acme/api", "created_by": OWNER_OPEN_ID, "created_at": ANY}
    ]
    assert status["stats"]["feishu_events"]["callbacks"] >= 1
    assert status["stats"]["commands"]["applied"] == 1


class _AnyValue:
    def __eq__(self, other: object) -> bool:
        return True


ANY = _AnyValue()
