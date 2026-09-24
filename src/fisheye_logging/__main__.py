from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .config import ConfigError, load_config
from .session import run_session


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fisheye-logging",
        description="Record fisheye video and u-blox M8T timing data.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("fisheye.toml"),
        help="TOML configuration file (default: fisheye.toml)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        config = load_config(args.config)
        session_dir = run_session(config, args.config)
    except (ConfigError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(session_dir)


if __name__ == "__main__":
    main()
