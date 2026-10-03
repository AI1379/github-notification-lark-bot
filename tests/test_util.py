"""通配匹配、时间解析等工具函数。"""

from __future__ import annotations

import os

import pytest

from larkbot.util import (
    glob_match,
    glob_match_any,
    load_env_file,
    mask_url,
    parse_ts,
    to_iso,
    truncate,
    utcnow,
)


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("acme/*", "acme/api", True),
        ("acme/*", "acme/api/web", True),
        ("acme/*", "other/api", False),
        ("*[bot]", "dependabot[bot]", True),  # [] 按字面量处理，而不是 fnmatch 的字符集
        ("*[bot]", "octocat", False),
        ("release/*", "release/1.0", True),
        ("release/*", "ReLeAsE/1.0", True),  # 大小写不敏感
        ("main", "main", True),
        ("main", "main2", False),
        ("v?", "v1", True),
        ("*", "anything", True),
    ],
)
def test_glob_match(pattern, value, expected):
    assert glob_match(pattern, value) is expected


def test_glob_match_any_handles_none():
    assert glob_match_any(["*[bot]"], None) is False
    assert glob_match_any(None, "x") is False
    assert glob_match_any(["*[bot]", "renovate*"], "renovate-bot") is True


def test_parse_ts_handles_iso_and_epoch():
    parsed = parse_ts("2024-05-01T10:00:00Z")
    assert parsed is not None and parsed.year == 2024 and parsed.tzinfo is not None
    assert parse_ts(0) is not None
    assert parse_ts("不是时间") is None
    assert parse_ts(None) is None


def test_to_iso_roundtrip_and_sortable():
    now = utcnow()
    text = to_iso(now)
    early = to_iso(utcnow().replace(year=2020))
    assert text is not None and early is not None
    assert parse_ts(text) == now
    assert early < text  # 字符串比较即时间比较


def test_truncate_collapses_whitespace():
    assert truncate("a\n\n b   c") == "a b c"
    assert truncate("x" * 20, 10) == "x" * 9 + "…"
    assert truncate(None) == ""


def test_load_env_file_reads_realistic_file(tmp_path, monkeypatch):
    """真实 .env 会长这样：注释、空行、值里有 + / =、带引号、export 前缀。"""
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "GITHUB_TOKEN", "EMPTY"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# larkbot 密钥\n"
        "\n"
        "FEISHU_APP_ID=cli_abc123\n"
        "FEISHU_APP_SECRET=AbC+/=def\n"
        "export GITHUB_TOKEN=ghp_xxx\n"
        'QUOTED="has spaces"\n'
        "EMPTY=\n",
        encoding="utf-8",
    )
    loaded = load_env_file(env)

    assert loaded["FEISHU_APP_ID"] == "cli_abc123"
    assert loaded["FEISHU_APP_SECRET"] == "AbC+/=def"  # = + / 必须原样保留
    assert loaded["GITHUB_TOKEN"] == "ghp_xxx"
    assert loaded["QUOTED"] == "has spaces"
    assert os.environ["FEISHU_APP_ID"] == "cli_abc123"


def test_load_env_file_does_not_interpolate_dollar(tmp_path, monkeypatch):
    """密钥里出现 ${...} 不能被当变量展开（静默损坏比报错更难查）。"""
    monkeypatch.delenv("WEIRD_SECRET", raising=False)
    env = tmp_path / ".env"
    env.write_text("WEIRD_SECRET=abc${HOME}def\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["WEIRD_SECRET"] == "abc${HOME}def"


def test_load_env_file_does_not_override_existing(tmp_path, monkeypatch):
    """systemd / 手动 export 的值优先于 .env。"""
    monkeypatch.setenv("FEISHU_APP_ID", "from-real-env")
    env = tmp_path / ".env"
    env.write_text("FEISHU_APP_ID=from-file\n", encoding="utf-8")
    load_env_file(env)
    assert os.environ["FEISHU_APP_ID"] == "from-real-env"


def test_load_env_file_handles_crlf(tmp_path, monkeypatch):
    """Windows 上编辑过再传上来的 .env 是 CRLF，不能把 \\r 带进值里。"""
    for key in ("GITHUB_WEBHOOK_SECRET", "FEISHU_APP_ID"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_bytes(b"GITHUB_WEBHOOK_SECRET=secret123\r\nFEISHU_APP_ID=cli_x\r\n")
    load_env_file(env)
    assert os.environ["GITHUB_WEBHOOK_SECRET"] == "secret123"
    assert os.environ["FEISHU_APP_ID"] == "cli_x"


def test_load_env_file_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("LARKBOT_ENV_FILE", raising=False)
    assert load_env_file(tmp_path / "nope.env") == {}


def test_load_env_file_respects_larkbot_env_file(tmp_path, monkeypatch):
    env = tmp_path / "custom.env"
    env.write_text("FEISHU_APP_ID=custom\n", encoding="utf-8")
    monkeypatch.setenv("LARKBOT_ENV_FILE", str(env))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    load_env_file()
    assert os.environ["FEISHU_APP_ID"] == "custom"


def test_mask_url_hides_last_segment():
    masked = mask_url("https://open.feishu.cn/open-apis/bot/v2/hook/supersecrettoken")
    assert masked == "https://open.feishu.cn/open-apis/bot/v2/hook/" + "*" * 12 + "oken"
    assert "supersecret" not in masked
    assert masked.endswith("oken")  # 保留尾部字符便于区分不同机器人
    assert mask_url("https://example.com") == "https://example.com"
