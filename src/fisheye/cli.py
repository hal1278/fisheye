from __future__ import annotations

import argparse
from pathlib import Path
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fisheye",
        description="Fisheye-camera acquisition and GNSS time synchronization workflow.",
        epilog="Run 'fisheye <command> --help' for command-specific arguments and examples.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser(
        "record",
        help="Record camera video and GNSS timing data.",
        description="Record camera video and GNSS timing data into a new session.",
        epilog="Example:\n  fisheye record --config fisheye.toml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    record.add_argument(
        "--config",
        type=Path,
        default=Path("fisheye.toml"),
        metavar="TOML",
        help="TOML configuration file (default: fisheye.toml).",
    )

    subparsers.add_parser(
        "roi",
        help="Select the TIMEPULSE LED ROI interactively.",
        description=(
            "Open the ROI selector. Choose recordings/<session>/capture/ in the "
            "directory chooser; Copy places '--roi X,Y,W,H' on the clipboard."
        ),
        epilog="Example:\n  fisheye roi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sync = subparsers.add_parser(
        "sync",
        help="Estimate the video-PTS-to-UTC synchronization model.",
        description=(
            "Detect TIMEPULSE LED rises and estimate the mapping from source video "
            "PTS to UTC."
        ),
        epilog=(
            "Example:\n"
            "  fisheye sync recordings/20260925_090648/capture "
            "--sync-start 00:12:30 --roi 512,608,32,32\n\n"
            "Here, --sync-start is the source-video elapsed time after LED position "
            "adjustment, and --roi is the source-pixel rectangle copied by "
            "'fisheye roi'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync.add_argument(
        "capture",
        type=Path,
        metavar="CAPTURE_DIR",
        help=(
            "Capture directory produced by 'fisheye record', e.g. "
            "recordings/20260925_090648/capture."
        ),
    )
    sync.add_argument(
        "--sync-start",
        required=True,
        metavar="ELAPSED",
        help=(
            "First source-video elapsed time usable for LED detection. "
            "Accepted forms include HH:MM:SS[.sss], MM:SS[.sss], or seconds; "
            "e.g. 00:12:30."
        ),
    )
    sync.add_argument(
        "--roi",
        required=True,
        metavar="X,Y,W,H",
        help=(
            "LED ROI in source pixels: left,top,width,height; "
            "e.g. 512,608,32,32. 'fisheye roi' copies this option."
        ),
    )
    sync.add_argument(
        "--workers",
        type=int,
        metavar="N",
        help=(
            "Parallel segment decoders for LED extraction. Default: auto, up to 4 "
            "workers and no more than the available segments/CPUs."
        ),
    )

    export = subparsers.add_parser(
        "export",
        help="Create the synchronized shareable video.",
        description=(
            "Create a timestamped MP4 from a completed synchronization run without "
            "rerunning LED detection or model fitting."
        ),
        epilog=(
            "Example:\n"
            "  fisheye export "
            "recordings/20260925_090648/processing/20260928_220000 "
            "--start 00:53:00\n\n"
            "Here, SYNC_DIR is the directory printed by 'fisheye sync', and --start "
            "is source-video elapsed time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    export.add_argument(
        "processing",
        type=Path,
        metavar="SYNC_DIR",
        help=(
            "Synchronization-run directory containing sync.json, e.g. "
            "recordings/20260925_090648/processing/20260928_220000."
        ),
    )
    export.add_argument(
        "--start",
        required=True,
        metavar="ELAPSED",
        help=(
            "Requested source-video output start time. Accepted forms include "
            "HH:MM:SS[.sss], MM:SS[.sss], or seconds; e.g. 00:53:00."
        ),
    )

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

            result = run(args.capture, args.sync_start, args.roi, args.workers)
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
