from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
import time

from . import __version__
from .camera import CameraRecorder
from .config import AppConfig
from .gnss import GnssLogger


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _ffmpeg_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout.splitlines()[0] if result.stdout else ""


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def run_session(config: AppConfig, config_path: Path) -> Path:
    started = datetime.now()
    session_dir = config.session.output_dir / started.strftime("%Y%m%d_%H%M%S")
    session_dir.mkdir(parents=True, exist_ok=False)
    metadata_path = session_dir / "session.json"

    metadata: dict = {
        "schema_version": 1,
        "status": "starting",
        "started_utc": _utc_now(),
        "config_path": str(config_path.resolve()),
        "software": {
            "fisheye_logging_version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "configured": asdict(config),
    }
    metadata["configured"]["session"]["output_dir"] = str(
        metadata["configured"]["session"]["output_dir"]
    )
    _write_json(metadata_path, metadata)

    gnss = GnssLogger(config.gnss, session_dir)
    camera = CameraRecorder(config.camera, session_dir)
    camera_exit: int | None = None
    failure: BaseException | None = None

    try:
        metadata["software"]["ffmpeg"] = _ffmpeg_version(config.camera.ffmpeg)
        metadata["gnss"] = gnss.open_and_configure()
        metadata["status"] = "recording"
        _write_json(metadata_path, metadata)

        gnss.start()
        camera.start()
        metadata["camera"] = {"command": camera.command}
        _write_json(metadata_path, metadata)

        while True:
            gnss.raise_if_failed()
            camera.raise_if_failed()
            camera_exit = camera.poll()
            if camera_exit is not None:
                if camera_exit != 0:
                    raise RuntimeError(f"ffmpeg exited with status {camera_exit}")
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        metadata["status"] = "stopping"
    except BaseException as exc:
        failure = exc
        metadata["status"] = "failed"
        metadata["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        camera_exit = camera.stop()
        gnss.stop()
        metadata["ended_utc"] = _utc_now()
        metadata["camera_exit_code"] = camera_exit
        if failure is None:
            metadata["status"] = "completed"
        _write_json(metadata_path, metadata)

    if failure is not None:
        raise failure
    return session_dir
