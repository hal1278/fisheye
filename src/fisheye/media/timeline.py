from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
from pathlib import Path
import re
from typing import Iterable

import av


class MediaError(RuntimeError):
    pass


_VIDEO_RE = re.compile(r"video_(\d+)\.mkv$")


@dataclass(frozen=True, slots=True)
class FrameRef:
    index: int
    segment_index: int
    local_index: int
    path: Path
    pts: int
    time_base_num: int
    time_base_den: int

    @property
    def time(self) -> Fraction:
        return Fraction(self.pts * self.time_base_num, self.time_base_den)


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    index: int
    path: Path
    first_frame: int
    frame_count: int
    codec: str
    width: int
    height: int
    time_base_num: int
    time_base_den: int


@dataclass(frozen=True, slots=True)
class VirtualVideo:
    capture_dir: Path
    frames: tuple[FrameRef, ...]
    segments: tuple[SegmentInfo, ...]
    width: int
    height: int
    codec: str
    nominal_fps: float

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def first_time(self) -> Fraction:
        return self.frames[0].time

    @property
    def last_time(self) -> Fraction:
        return self.frames[-1].time

    @property
    def duration(self) -> Fraction:
        return self.last_time - self.first_time

    def elapsed(self, frame_index: int) -> Fraction:
        return self.frames[frame_index].time - self.first_time

    def segment_for_frame(self, frame_index: int) -> SegmentInfo:
        return self.segments[self.frames[frame_index].segment_index]


def discover_segments(capture_dir: Path) -> list[Path]:
    capture_dir = capture_dir.resolve()
    if not capture_dir.is_dir():
        raise MediaError(f"capture directory not found: {capture_dir}")
    if not (capture_dir / "session.json").is_file():
        raise MediaError(f"session.json not found in capture directory: {capture_dir}")

    found: list[tuple[int, Path]] = []
    for path in capture_dir.iterdir():
        match = _VIDEO_RE.fullmatch(path.name)
        if match and path.is_file():
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    if not found:
        raise MediaError(f"no video_*.mkv files found in: {capture_dir}")

    numbers = [number for number, _ in found]
    expected = list(range(len(numbers)))
    if numbers != expected:
        raise MediaError(f"video segment sequence has gaps or duplicates: {numbers}")
    return [path for _, path in found]


def _nominal_fps(capture_dir: Path) -> float:
    try:
        data = json.loads((capture_dir / "session.json").read_text(encoding="utf-8"))
        value = float(data["configured"]["camera"]["framerate"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("session.json does not contain a valid configured camera framerate") from exc
    if value <= 0:
        raise MediaError("configured camera framerate must be positive")
    return value


def _time_base_parts(stream) -> tuple[int, int]:
    time_base = stream.time_base
    if time_base is None:
        raise MediaError("video stream has no time base")
    return int(time_base.numerator), int(time_base.denominator)


def _packet_pts(container, stream) -> Iterable[int]:
    for packet in container.demux(stream):
        if packet.pts is not None:
            yield int(packet.pts)


def _decoded_pts(container) -> Iterable[int]:
    for frame in container.decode(video=0):
        if frame.pts is None:
            raise MediaError("decoded video frame has no PTS")
        yield int(frame.pts)


def build_virtual_video(capture_dir: Path) -> VirtualVideo:
    capture_dir = capture_dir.resolve()
    paths = discover_segments(capture_dir)
    nominal_fps = _nominal_fps(capture_dir)

    frames: list[FrameRef] = []
    segments: list[SegmentInfo] = []
    expected_signature: tuple[str, int, int] | None = None
    previous_time: Fraction | None = None

    for segment_index, path in enumerate(paths):
        with av.open(str(path), mode="r") as container:
            if not container.streams.video:
                raise MediaError(f"no video stream in: {path}")
            stream = container.streams.video[0]
            codec = str(stream.codec_context.name)
            width = int(stream.codec_context.width)
            height = int(stream.codec_context.height)
            signature = (codec, width, height)
            if expected_signature is None:
                expected_signature = signature
            elif signature != expected_signature:
                raise MediaError(
                    f"video stream mismatch in {path.name}: {signature} != {expected_signature}"
                )

            tb_num, tb_den = _time_base_parts(stream)
            first_frame = len(frames)
            pts_iter = _packet_pts(container, stream) if codec == "mjpeg" else _decoded_pts(container)

            local_count = 0
            previous_local_pts: int | None = None
            for pts in pts_iter:
                if previous_local_pts is not None and pts <= previous_local_pts:
                    raise MediaError(f"non-increasing PTS in {path.name}: {pts}")
                current_time = Fraction(pts * tb_num, tb_den)
                if previous_time is not None and current_time <= previous_time:
                    raise MediaError(
                        f"non-increasing PTS across virtual video at {path.name}: {float(current_time):.9f}"
                    )
                frames.append(
                    FrameRef(
                        index=len(frames),
                        segment_index=segment_index,
                        local_index=local_count,
                        path=path,
                        pts=pts,
                        time_base_num=tb_num,
                        time_base_den=tb_den,
                    )
                )
                previous_local_pts = pts
                previous_time = current_time
                local_count += 1

            if local_count == 0:
                raise MediaError(f"no timestamped video frames found in: {path}")

            segments.append(
                SegmentInfo(
                    index=segment_index,
                    path=path,
                    first_frame=first_frame,
                    frame_count=local_count,
                    codec=codec,
                    width=width,
                    height=height,
                    time_base_num=tb_num,
                    time_base_den=tb_den,
                )
            )

    assert expected_signature is not None
    codec, width, height = expected_signature
    return VirtualVideo(
        capture_dir=capture_dir,
        frames=tuple(frames),
        segments=tuple(segments),
        width=width,
        height=height,
        codec=codec,
        nominal_fps=nominal_fps,
    )
