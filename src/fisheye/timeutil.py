from __future__ import annotations


def parse_elapsed(value: str) -> float:
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"invalid elapsed time: {value!r}")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"invalid elapsed time: {value!r}") from exc
    if any(number < 0 for number in numbers):
        raise ValueError(f"elapsed time must not be negative: {value!r}")
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours = 0.0
        minutes, seconds = numbers
    else:
        hours = 0.0
        minutes = 0.0
        seconds = numbers[0]
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"invalid elapsed time: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def parse_roi(value: str) -> tuple[int, int, int, int]:
    try:
        parts = [int(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise ValueError(f"invalid ROI: {value!r}") from exc
    if len(parts) != 4:
        raise ValueError(f"invalid ROI: {value!r}")
    x, y, width, height = parts
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise ValueError(f"invalid ROI: {value!r}")
    return x, y, width, height
