"""通用小工具：通配匹配、时间解析、日志与脱敏。"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把只含 ``*`` / ``?`` 的 glob 编译成大小写不敏感的正则。

    与 :mod:`fnmatch` 不同，``[`` 和 ``]`` 被当作字面量，这样
    ``*[bot]`` 才能匹配 ``dependabot[bot]``（fnmatch 会把它当字符集）。
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    parts = ["^"]
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    parts.append("$")
    compiled = re.compile("".join(parts), re.IGNORECASE | re.DOTALL)
    _GLOB_CACHE[pattern] = compiled
    return compiled


def glob_match(pattern: str, value: str | None) -> bool:
    """``value`` 是否匹配 ``pattern``（glob，大小写不敏感）。"""
    if not value:
        return False
    if pattern == "*":
        return True
    return glob_to_regex(pattern).match(value) is not None


def glob_match_any(patterns: list[str] | None, value: str | None) -> bool:
    if not patterns or not value:
        return False
    return any(glob_match(p, value) for p in patterns)


def parse_ts(value: Any) -> datetime | None:
    """解析 GitHub 的时间戳（ISO8601 字符串或 epoch 秒），统一为 UTC。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    """统一以 UTC ISO8601 落库，保证字符串比较即时间比较。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def as_dict(value: Any) -> dict[str, Any]:
    """把负载字段规整成 dict。

    GitHub / 飞书的同名字段在不同事件里可能是 `null`、也可能是别种类型，统一收敛到这里，
    避免满屏的 ``x if isinstance(x, dict) else {}``（那种内联写法在类型检查器眼里会推成
    ``dict | None``，后面每一处 ``.get`` 都会报警）。
    """
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """把负载字段规整成 list（非列表一律当空列表）。"""
    return value if isinstance(value, list) else []


def truncate(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def mask(value: str | None, *, keep: int = 4) -> str | None:
    """脱敏输出：保留尾部 ``keep`` 个字符，用于 /status 之类的展示。"""
    if not value:
        return None
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def mask_url(url: str) -> str:
    """脱敏 URL 路径的最后一段。

    飞书自定义机器人 webhook 的 token 就在路径末段，而它本身就是凭据；
    httpx 的异常文本有时会带上完整 URL，所以写日志/落库前先过一遍这里。
    """
    parts = urlsplit(url)
    segments = parts.path.rstrip("/").split("/")
    if not segments or not segments[-1]:
        return url
    segments[-1] = mask(segments[-1], keep=4) or "****"
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), "", ""))


def scrub_url(text: str, url: str | None) -> str:
    """把文本里的敏感 URL 换成脱敏版。"""
    if not url:
        return text
    return text.replace(url, mask_url(url))


def slugify(text: str, *, fallback: str = "group") -> str:
    """把群名转成适合作配置别名的 slug（中文名会得到 fallback）。"""
    ascii_only = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug[:40] or fallback


def setup_logging(level: str | None = None) -> None:
    resolved = (level or os.environ.get("LARKBOT_LOG_LEVEL") or "INFO").upper()
    # Windows 控制台默认是 cp936 之类的编码，打印 emoji 会直接抛 UnicodeEncodeError，
    # 这里只放宽错误处理（保持原编码，避免中文变荠码）。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # 某些宿主（如被重定向的管道）不允许 reconfigure，忽略即可
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")
    logging.basicConfig(
        level=getattr(logging, resolved, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )
    # httpx 每个请求都打一行 INFO，太吵
    logging.getLogger("httpx").setLevel(logging.WARNING)
