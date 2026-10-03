"""webhook HMAC 签名校验。"""

from __future__ import annotations

import pytest

from larkbot.github.webhook import compute_signature, verify_signature

BODY = b'{"zen":"Keep it logically awesome.","hook_id":1}'
SECRET = "it-is-a-secret"


def test_accepts_valid_signature():
    header = compute_signature(SECRET, BODY)
    assert header.startswith("sha256=")
    assert verify_signature(SECRET, BODY, header) is True


def test_rejects_tampered_body():
    header = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY + b" ", header) is False


def test_rejects_wrong_secret():
    header = compute_signature("other-secret", BODY)
    assert verify_signature(SECRET, BODY, header) is False


@pytest.mark.parametrize("header", [None, "", "sha256=", "md5=abc", "garbage", "sha256=deadbeef"])
def test_rejects_malformed_header(header):
    assert verify_signature(SECRET, BODY, header) is False


def test_sha1_supported_but_can_be_disabled():
    header = compute_signature(SECRET, BODY, algo="sha1")
    assert verify_signature(SECRET, BODY, header) is True
    assert verify_signature(SECRET, BODY, header, allow_sha1=False) is False


def test_unknown_algo_raises():
    with pytest.raises(ValueError, match="不支持的签名算法"):
        compute_signature(SECRET, BODY, algo="md5")
