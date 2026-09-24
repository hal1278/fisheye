from __future__ import annotations

import csv
from pathlib import Path
import subprocess
import threading
import time

from .config import CameraConfig


class CameraError(RuntimeError):
    pass


class CameraRecorder:
    def __init__(self, config: CameraConfig, output_dir: Path):
        self.config = config
        self.output_dir = output_dir
        self.process: subprocess.Popen[str] | None = None
        self.command: list[str] = []
        self._progress_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def build_command(self) -> list[str]:
        cfg = self.config
        output = self.output_dir / "video_%03d.mkv"
        cmd = [
            cfg.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-progress",
            "pipe:1",
            "-stats_period",
            str(cfg.anchor_interval_seconds),
            "-f",
            "dshow",
            "-video_size",
            cfg.video_size,
            "-framerate",
            str(cfg.framerate),
        ]
        if cfg.input_codec:
            cmd += ["-vcodec", cfg.input_codec]
        cmd += [
            "-i",
            f"video={cfg.device}",
            "-map",
            "0:v:0",
            "-copyts",
            "-start_at_zero",
            "-fps_mode",
            "passthrough",
            "-c:v",
            cfg.output_codec,
            "-f",
            "segment",
            "-segment_time",
            str(cfg.segment_seconds),
            "-reset_timestamps",
            "0",
            "-segment_format",
            "matroska",
            str(output),
        ]
        return cmd

    def start(self) -> None:
        self.command = self.build_command()
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._progress_thread = threading.Thread(
            target=self._read_progress, name="ffmpeg-progress", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="ffmpeg-stderr", daemon=True
        )
        self._progress_thread.start()
        self._stderr_thread.start()

    def _read_progress(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        path = self.output_dir / "camera.csv"
        record: dict[str, str] = {}
        try:
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["video_time_ns", "host_monotonic_ns"])
                stream.flush()
                for line in self.process.stdout:
                    line = line.rstrip("\r\n")
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    record[key] = value
                    if key != "progress":
                        continue
                    out_time_us = record.get("out_time_us")
                    if out_time_us not in (None, "N/A"):
                        writer.writerow([int(out_time_us) * 1000, time.monotonic_ns()])
                        stream.flush()
                    record.clear()
        except BaseException as exc:
            self._error = exc

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        try:
            with (self.output_dir / "ffmpeg.log").open(
                "w", encoding="utf-8", errors="replace"
            ) as stream:
                for line in self.process.stderr:
                    stream.write(line)
                    stream.flush()
        except BaseException as exc:
            self._error = exc

    def poll(self) -> int | None:
        assert self.process is not None
        return self.process.poll()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise CameraError("camera logging failed") from self._error

    def stop(self) -> int:
        if self.process is None:
            return 0
        if self.process.poll() is None:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.write("q\n")
                    self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait()
        if self._progress_thread is not None:
            self._progress_thread.join(timeout=2)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        return int(self.process.returncode or 0)
