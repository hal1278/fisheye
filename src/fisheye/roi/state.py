from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)


@dataclass(slots=True)
class RoiState:
    frame_width: int
    frame_height: int
    x: int = 0
    y: int = 0
    width: int = 32
    height: int = 32
    locked: bool = False

    def __post_init__(self) -> None:
        if self.frame_width <= 0 or self.frame_height <= 0:
            raise ValueError("frame dimensions must be positive")
        self.width = _clamp(self.width, 1, self.frame_width)
        self.height = _clamp(self.height, 1, self.frame_height)
        self._clamp_xy()

    def _clamp_xy(self) -> None:
        self.x = _clamp(self.x, 0, self.frame_width - self.width)
        self.y = _clamp(self.y, 0, self.frame_height - self.height)

    def position_at(self, center_x: float, center_y: float) -> None:
        self.x = round(center_x - self.width / 2)
        self.y = round(center_y - self.height / 2)
        self._clamp_xy()

    def lock(self) -> None:
        self.locked = True

    def unlock(self) -> None:
        self.locked = False

    def move_x(self, delta: int) -> None:
        if not self.locked:
            return
        self.x = _clamp(self.x + delta, 0, self.frame_width - self.width)

    def move_y(self, delta: int) -> None:
        if not self.locked:
            return
        self.y = _clamp(self.y + delta, 0, self.frame_height - self.height)

    def resize_width(self, delta: int) -> None:
        center = self.x + self.width / 2
        self.width = _clamp(self.width + delta, 1, self.frame_width)
        self.x = round(center - self.width / 2)
        self._clamp_xy()

    def resize_height(self, delta: int) -> None:
        center = self.y + self.height / 2
        self.height = _clamp(self.height + delta, 1, self.frame_height)
        self.y = round(center - self.height / 2)
        self._clamp_xy()

    def copy_text(self) -> str:
        return f"--roi {self.x},{self.y},{self.width},{self.height}"
