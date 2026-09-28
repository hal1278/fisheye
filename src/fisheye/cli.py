from __future__ import annotations

import argparse
from pathlib import Path
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fisheye")
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record")
    record.add_argument(
        "--config",
        type=Path,
        default=Path("fisheye.toml"),
        help="TOML configuration file (default: fisheye.toml)",
    )

    subparsers.add_parser("roi")

    sync = subparsers.add_parser("sync")
    sync.add_argument("capture", type=Path)
    sync.add_argument("--sync-start", required=True)
    sync.add_argument("--roi", required=True)

    export = subparsers.add_parser("export")
    export.add_argument("processing", type=Path)
    export.add_argument("--start", required=True)

    return parser


def main() -> None:
    args = _parser().parse_args()

    try:
        if args.command == "record":
            from .record.command import run

            result = run(args.config)
            print(result)
            return

        if args.command == "roi":
            from .roi.gui import run

            run()
            return

        if args.command == "sync":
            from .sync.command import run

            result = run(args.capture, args.sync_start, args.roi)
            print(result)
            return

        if args.command == "export":
            from .export.command import run

            result = run(args.processing, args.start)
            print(result)
            return

        raise AssertionError(f"unknown command: {args.command}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
