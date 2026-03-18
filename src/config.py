"""
config.py – Loads and validates bot configuration from YAML and environment variables.
"""

from __future__ import annotations

import os
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "config", "config.yaml"
)


def load_config(path: str = _DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load configuration from a YAML file and apply environment variable overrides.

    Environment variable overrides (all optional):
        MT5_LOGIN     – account number
        MT5_PASSWORD  – account password
        MT5_SERVER    – broker server name

    Args:
        path: Path to the YAML configuration file.

    Returns:
        Nested dict of configuration values.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If required top-level sections are missing.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh) or {}

    # Validate required top-level sections
    required_sections = {"mt5", "trading", "risk", "strategy", "indicators", "logging", "model"}
    missing = required_sections - set(cfg.keys())
    if missing:
        raise ValueError(f"Configuration file is missing required sections: {missing}")

    # Apply environment variable overrides for MT5 credentials
    if os.getenv("MT5_LOGIN"):
        cfg["mt5"]["login"] = int(os.environ["MT5_LOGIN"])
    if os.getenv("MT5_PASSWORD"):
        cfg["mt5"]["password"] = os.environ["MT5_PASSWORD"]
    if os.getenv("MT5_SERVER"):
        cfg["mt5"]["server"] = os.environ["MT5_SERVER"]

    return cfg
