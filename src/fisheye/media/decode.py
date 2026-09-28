from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import av
from PIL import Image

from .timeline import FrameRef, MediaError, VirtualVideo


class FrameDecoder:
    def __init__(self, video: VirtualVideo, cache_size: int = 16):
        self.video = video
        self.cache_size = max(0, cache_size)
        self._cache: OrderedDict[int, Image.Image] = OrderedDict()
        self._container = None
        self._segment_path: Path | None = None

    def close(self) -> None:
        if self._container is not None:
            self._container.close()
            self._container = None
            self._segment_path = None
        self._cache.clear()

    def _open(self, path: Path):
        if self._container is not None and self._segment_path == path:
            return self._container
        if self._container is not None:
            self._container.close()
        self._container = av.open(str(path), mode="r")
        self._segment_path = path
        return self._container

    def _cache_get(self, frame_index: int) -> Image.Image | None:
        image = self._cache.get(frame_index)
        if image is not None:
            self._cache.move_to_end(frame_index)
            return image.copy()
        return None

    def _cache_put(self, frame_index: int, image: Image.Image) -> None:
        if self.cache_size <= 0:
            return
        self._cache[frame_index] = image.copy()
        self._cache.move_to_end(frame_index)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def decode(self, frame_index: int) -> Image.Image:
        if not 0 <= frame_index < self.video.frame_count:
            raise IndexError(frame_index)
        cached = self._cache_get(frame_index)
        if cached is not None:
            return cached

        ref = self.video.frames[frame_index]
        image = self._decode_seek(ref)
        self._cache_put(frame_index, image)
        return image

    def _decode_seek(self, ref: FrameRef) -> Image.Image:
        container = self._open(ref.path)
        stream = container.streams.video[0]
        try:
            container.seek(ref.pts, stream=stream, backward=True, any_frame=False)
            for frame in container.decode(video=0):
                if frame.pts is None:
                    continue
                pts = int(frame.pts)
                if pts == ref.pts:
                    return frame.to_image()
                if pts > ref.pts:
                    break
        except av.FFmpegError:
            pass

        if self._container is not None:
            self._container.close()
        self._container = av.open(str(ref.path), mode="r")
        self._segment_path = ref.path
        for frame in self._container.decode(video=0):
            if frame.pts is None:
                continue
            pts = int(frame.pts)
            if pts == ref.pts:
                return frame.to_image()
            if pts > ref.pts:
                break
        raise MediaError(
            f"failed to decode frame {ref.index} at PTS {ref.pts} from {ref.path.name}"
        )
