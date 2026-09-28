from __future__ import annotations

from pathlib import Path

from .config import load_config
from .session import run_session


def run(config_path: Path) -> Path:
    config = load_config(config_path)
    return run_session(config, config_path)
