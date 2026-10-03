"""终端输出：中文对齐表格与可粘贴的配置片段。"""

from __future__ import annotations

import yaml

from larkbot.console import display_width, pad, render_chats_yaml, render_table
from larkbot.feishu.client import ChatSummary
from larkbot.util import slugify


def test_display_width_counts_cjk_as_two():
    assert display_width("abc") == 3
    assert display_width("产品群") == 6
    assert display_width("a产b") == 4


def test_pad_aligns_mixed_width_text():
    assert pad("产品", 8) == "产品" + " " * 4
    assert pad("abcd", 8) == "abcd" + " " * 4
    assert pad("很长的中文内容", 4) == "很长的中文内容"  # 不截断


def test_render_table_columns_line_up():
    table = render_table(["群名称", "chat_id"], [["产品群", "oc_a"], ["ab", "oc_bbbb"]])
    lines = table.splitlines()
    # 表头 / 分隔行 / 两行数据
    assert len(lines) == 4
    header, separator, *rows = lines
    assert separator == "------  -------"
    # 第二列在所有行里的起始**显示宽度**一致（中文群名占 2 列，靠 pad 对齐）
    starts = {display_width(row[: row.rindex("oc_")]) for row in rows}
    starts.add(display_width(header[: header.index("chat_id")]))
    assert starts == {8}


def test_render_table_empty_rows():
    assert render_table(["a"], []).splitlines() == ["a", "-"]


def test_render_chats_yaml_is_valid_and_pasteable():
    chats = [
        ChatSummary(chat_id="oc_111", name="产品研发群", member_count=12),
        ChatSummary(chat_id="oc_222", name="Release Room", external=True),
    ]
    text = render_chats_yaml(chats)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    entries = parsed["chats"]
    assert len(entries) == 2
    # 中文群名 slug 化后为空 -> 回退成 group-<chat_id 尾号>
    assert entries[0] == {"name": "group-oc_111", "transport": "app", "chat_id": "oc_111"}
    assert entries[1]["name"] == "release-room"
    assert entries[1]["transport"] == "app"
    assert entries[1]["chat_id"] == "oc_222"
    assert "# 飞书群: 产品研发群" in text  # 原群名保留在注释里


def test_render_chats_yaml_handles_no_groups():
    parsed = yaml.safe_load(render_chats_yaml([]))
    assert parsed == {"chats": []}


def test_slugify():
    assert slugify("Release Room") == "release-room"
    assert slugify("Infra / Ops") == "infra-ops"
    assert slugify("产品研发群", fallback="group-x") == "group-x"
    assert slugify("") == "group"
