from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SessionConfig:
    output_dir: Path


@dataclass(frozen=True)
class CameraConfig:
    backend: str
    device: str
    input_codec: str
    video_size: str
    framerate: float
    output_codec: str
    segment_seconds: int
    anchor_interval_seconds: float
    ffmpeg: str


@dataclass(frozen=True)
class TimepulseConfig:
    index: int
    frequency_hz: int
    duty_cycle: float
    active: bool
    align_to_tow: bool
    polarity: str
    time_grid: str
    lock_gnss_frequency: bool


@dataclass(frozen=True)
class GnssConfig:
    port: str
    baudrate: int
    timeout_seconds: float
    measurement_rate_ms: int
    nav_rate: int
    time_ref: int
    timepulse: TimepulseConfig
    messages: dict[str, int]


@dataclass(frozen=True)
class AppConfig:
    session: SessionConfig
    camera: CameraConfig
    gnss: GnssConfig


def _require(table: dict, key: str, section: str):
    if key not in table:
        raise ConfigError(f"missing required setting [{section}].{key}")
    return table[key]


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as stream:
        data = tomllib.load(stream)

    session = data.get("session", {})
    camera = data.get("camera", {})
    gnss = data.get("gnss", {})
    tp = gnss.get("timepulse", {})
    messages = gnss.get("messages", {})

    cfg = AppConfig(
        session=SessionConfig(output_dir=Path(_require(session, "output_dir", "session"))),
        camera=CameraConfig(
            backend=str(_require(camera, "backend", "camera")),
            device=str(_require(camera, "device", "camera")),
            input_codec=str(_require(camera, "input_codec", "camera")),
            video_size=str(_require(camera, "video_size", "camera")),
            framerate=float(_require(camera, "framerate", "camera")),
            output_codec=str(_require(camera, "output_codec", "camera")),
            segment_seconds=int(_require(camera, "segment_seconds", "camera")),
            anchor_interval_seconds=float(
                _require(camera, "anchor_interval_seconds", "camera")
            ),
            ffmpeg=str(camera.get("ffmpeg", "ffmpeg")),
        ),
        gnss=GnssConfig(
            port=str(_require(gnss, "port", "gnss")),
            baudrate=int(gnss.get("baudrate", 9600)),
            timeout_seconds=float(gnss.get("timeout_seconds", 0.2)),
            measurement_rate_ms=int(_require(gnss, "measurement_rate_ms", "gnss")),
            nav_rate=int(gnss.get("nav_rate", 1)),
            time_ref=int(gnss.get("time_ref", 1)),
            timepulse=TimepulseConfig(
                index=int(tp.get("index", 0)),
                frequency_hz=int(_require(tp, "frequency_hz", "gnss.timepulse")),
                duty_cycle=float(_require(tp, "duty_cycle", "gnss.timepulse")),
                active=bool(tp.get("active", True)),
                align_to_tow=bool(tp.get("align_to_tow", True)),
                polarity=str(tp.get("polarity", "rising")),
                time_grid=str(tp.get("time_grid", "utc")),
                lock_gnss_frequency=bool(tp.get("lock_gnss_frequency", True)),
            ),
            messages={str(k): int(v) for k, v in messages.items()},
        ),
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: AppConfig) -> None:
    if cfg.camera.backend != "dshow":
        raise ConfigError("initial implementation supports camera.backend = 'dshow' only")
    if not cfg.camera.device:
        raise ConfigError("camera.device must not be empty")
    if cfg.camera.framerate <= 0:
        raise ConfigError("camera.framerate must be positive")
    if cfg.camera.segment_seconds <= 0:
        raise ConfigError("camera.segment_seconds must be positive")
    if cfg.camera.anchor_interval_seconds <= 0:
        raise ConfigError("camera.anchor_interval_seconds must be positive")
    if cfg.gnss.measurement_rate_ms <= 0:
        raise ConfigError("gnss.measurement_rate_ms must be positive")
    if cfg.gnss.nav_rate <= 0:
        raise ConfigError("gnss.nav_rate must be positive")
    if not (0.0 < cfg.gnss.timepulse.duty_cycle < 1.0):
        raise ConfigError("gnss.timepulse.duty_cycle must be between 0 and 1")
    if cfg.gnss.timepulse.frequency_hz <= 0:
        raise ConfigError("gnss.timepulse.frequency_hz must be positive")
    if cfg.gnss.timepulse.polarity not in {"rising", "falling"}:
        raise ConfigError("gnss.timepulse.polarity must be 'rising' or 'falling'")
    if cfg.gnss.timepulse.time_grid not in {"utc", "gnss"}:
        raise ConfigError("gnss.timepulse.time_grid must be 'utc' or 'gnss'")
    for name, rate in cfg.gnss.messages.items():
        if not 0 <= rate <= 255:
            raise ConfigError(f"GNSS message rate for {name} must be in 0..255")
