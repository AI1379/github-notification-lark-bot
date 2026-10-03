"""GitHub webhook 的 HMAC 签名校验。"""

from __future__ import annotations

import hashlib
import hmac

SUPPORTED_ALGOS = ("sha256", "sha1")


def compute_signature(secret: str, body: bytes, *, algo: str = "sha256") -> str:
    """生成 ``sha256=<hex>`` 形式的签名头内容。"""
    if algo not in SUPPORTED_ALGOS:
        raise ValueError(f"不支持的签名算法: {algo}")
    digest = hmac.new(secret.encode("utf-8"), body, getattr(hashlib, algo)).hexdigest()
    return f"{algo}={digest}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str | None,
    *,
    allow_sha1: bool = True,
) -> bool:
    """校验 ``X-Hub-Signature-256``。使用 ``compare_digest`` 抵御时序攻击。"""
    if not secret or not header:
        return False
    algo, _, _ = header.partition("=")
    algo = algo.strip().lower()
    if algo not in SUPPORTED_ALGOS or (algo == "sha1" and not allow_sha1):
        return False
    expected = compute_signature(secret, body, algo=algo)
    return hmac.compare_digest(expected, header.strip())
