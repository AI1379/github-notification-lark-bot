"""配置加载与校验。"""

from __future__ import annotations

import pytest
import yaml

from larkbot.config import Config, ConfigError, load_config
from tests.conftest import base_config_dict


def write_config(tmp_path, data) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def test_load_minimal_config(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_HOOK", "https://open.feishu.cn/hook/x")
    data = {
        "chats": [{"name": "dev", "webhook_url": "env:FEISHU_HOOK"}],
        "subscriptions": [{"repo": "acme/api", "chats": "dev"}],
    }
    config = load_config(write_config(tmp_path, data))
    assert config.chats[0].webhook_url == "https://open.feishu.cn/hook/x"
    assert config.subscriptions[0].repos == ["acme/api"]
    assert config.subscriptions[0].chats == ["dev"]


def test_missing_env_ref_reports_variable_name(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_MISSING", raising=False)
    data = {"chats": [{"name": "dev", "webhook_url": "env:FEISHU_MISSING"}]}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data))
    assert "FEISHU_MISSING" in str(excinfo.value)


def test_unknown_chat_reference_fails():
    raw = base_config_dict()
    raw["subscriptions"] = [{"repos": ["acme/api"], "chats": ["nope"]}]
    with pytest.raises(ValueError, match="未定义的 chat"):
        Config.model_validate(raw)


def test_duplicate_chat_names_fail():
    raw = base_config_dict()
    raw["chats"] = [
        {"name": "dev", "webhook_url": "https://x"},
        {"name": "dev", "webhook_url": "https://y"},
    ]
    with pytest.raises(ValueError, match="重复"):
        Config.model_validate(raw)


def test_extra_field_rejected():
    raw = base_config_dict()
    raw["github"]["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        Config.model_validate(raw)


def test_lenient_mode_disables_incomplete_chat(tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_HOOK", raising=False)
    data = {
        "chats": [{"name": "dev", "webhook_url": "env:FEISHU_HOOK"}, {"name": "ok", "webhook_url": "https://x"}],
        "subscriptions": [{"repos": ["acme/api"], "chats": ["dev", "ok"]}],
    }
    path = write_config(tmp_path, data)
    with pytest.raises(ConfigError):
        load_config(path)  # 严格模式：直接失败
    config = load_config(path, lenient=True)
    assert [chat.name for chat in config.active_chats] == ["ok"]
    assert any("dev" in warning for warning in config.warnings)


def test_missing_file_message(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.yaml")
