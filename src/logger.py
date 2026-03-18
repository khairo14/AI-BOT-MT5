"""
logger.py – Centralised logging configuration for AI-BOT-MT5.
"""

import logging
import logging.handlers
import os
from typing import Optional


def setup_logger(
    name: str,
    level: str = "INFO",
    log_dir: str = "logs",
    log_file: str = "bot.log",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure and return a named logger with console and rotating-file handlers."""
    os.makedirs(log_dir, exist_ok=True)

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger(name)
    logger.setLevel(numeric_level)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Rotating file handler
    file_path = os.path.join(log_dir, log_file)
    file_handler = logging.handlers.RotatingFileHandler(
        file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger by name (returns the root 'ai-bot-mt5' logger if no name given)."""
    return logging.getLogger(name or "ai-bot-mt5")
