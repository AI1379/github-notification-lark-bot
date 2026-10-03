"""通配匹配、时间解析等工具函数。"""

from __future__ import annotations

import pytest

from larkbot.util import glob_match, glob_match_any, mask_url, parse_ts, to_iso, truncate, utcnow


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


def test_mask_url_hides_last_segment():
    masked = mask_url("https://open.feishu.cn/open-apis/bot/v2/hook/supersecrettoken")
    assert masked == "https://open.feishu.cn/open-apis/bot/v2/hook/" + "*" * 12 + "oken"
    assert "supersecret" not in masked
    assert masked.endswith("oken")  # 保留尾部字符便于区分不同机器人
    assert mask_url("https://example.com") == "https://example.com"
