"""
test_config.py – Unit tests for the config loader.
"""

import os
import pytest

from src.config import load_config


class TestLoadConfig:
    def test_loads_valid_config(self):
        cfg = load_config()
        assert "mt5" in cfg
        assert "trading" in cfg
        assert "risk" in cfg
        assert "strategy" in cfg

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nonexistent.yaml"))

    def test_env_override_login(self, monkeypatch):
        monkeypatch.setenv("MT5_LOGIN", "123456")
        cfg = load_config()
        assert cfg["mt5"]["login"] == 123456

    def test_env_override_server(self, monkeypatch):
        monkeypatch.setenv("MT5_SERVER", "XMGlobal-Demo")
        cfg = load_config()
        assert cfg["mt5"]["server"] == "XMGlobal-Demo"

    def test_raises_on_missing_section(self, tmp_path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("mt5:\n  login: 0\n")
        with pytest.raises(ValueError, match="missing required sections"):
            load_config(str(bad_yaml))
