from __future__ import annotations

from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from ..media.decode import FrameDecoder
from ..media.timeline import MediaError, VirtualVideo, build_virtual_video
from .state import RoiState


class _DecodeWorker:
    def __init__(
        self,
        decoder: FrameDecoder,
        results: queue.Queue[tuple[str, int | None, object]],
    ):
        self.decoder = decoder
        self.results = results
        self._condition = threading.Condition()
        self._target: int | None = None
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="roi-decode", daemon=True)
        self._thread.start()

    def request(self, frame_index: int) -> None:
        with self._condition:
            self._target = frame_index
            self._condition.notify()

    def close(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify()
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self.decoder.close()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._target is None and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                target = self._target
                self._target = None
            assert target is not None
            try:
                image = self.decoder.decode(target)
            except BaseException as exc:
                self.results.put(("error", target, exc))
                continue
            self.results.put(("frame", target, image))


class RoiSelectionApp:
    def __init__(self, root: tk.Tk, video: VirtualVideo):
        self.root = root
        self.video = video
        self.roi = RoiState(video.width, video.height)
        self.current_index = 0
        self.source_image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self._seek_after: str | None = None
        self._resize_after: str | None = None
        self._geometry_after: str | None = None
        self._setting_scale = False
        self._closing = False
        self._last_root_size: tuple[int, int] | None = None
        self._controls_height = 0

        self.root.title("ROI Selection")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            root,
            width=video.width,
            height=video.height,
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self._image_item = self.canvas.create_image(0, 0, anchor="nw")
        self._roi_item = self.canvas.create_rectangle(
            0, 0, 0, 0, outline="white", width=1, state="hidden"
        )

        self._build_seek_row()
        self._build_step_row()
        self._build_roi_row()

        self.root.update_idletasks()
        self._controls_height = max(
            1, self.root.winfo_reqheight() - self.canvas.winfo_reqheight()
        )
        control_width = max(
            child.winfo_reqwidth()
            for child in self.root.grid_slaves()
            if int(child.grid_info()["row"]) != 0
        )
        minimum_width = max(control_width, 320)
        minimum_height = (
            round(minimum_width * video.height / video.width) + self._controls_height
        )
        self.root.minsize(minimum_width, minimum_height)
        self.root.geometry(f"{video.width}x{video.height + self._controls_height}")
        self._last_root_size = (video.width, video.height + self._controls_height)

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.root.bind("<Configure>", self._on_root_configure)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        decoder = FrameDecoder(video)
        self._decode_results: queue.Queue[tuple[str, int | None, object]] = queue.Queue()
        self.worker = _DecodeWorker(decoder, self._decode_results)
        self.root.after(20, self._poll_decode_results)
        self._set_index(0, immediate=True)
        self._update_roi_controls()

    def _build_seek_row(self) -> None:
        row = ttk.Frame(self.root)
        row.grid(row=1, column=0, sticky="ew")
        row.columnconfigure(0, weight=1)

        self.seek_var = tk.DoubleVar(value=0)
        self.seek = ttk.Scale(
            row,
            from_=0,
            to=max(0, self.video.frame_count - 1),
            orient="horizontal",
            variable=self.seek_var,
            command=self._on_scale,
        )
        self.seek.grid(row=0, column=0, sticky="ew")

        frame_width = max(1, len(str(max(0, self.video.frame_count - 1))))
        self.frame_var = tk.StringVar(value="0")
        self.frame_entry = ttk.Entry(row, textvariable=self.frame_var, width=frame_width)
        self.frame_entry.grid(row=0, column=1, sticky="ew")
        self.frame_entry.bind("<Return>", self._on_frame_enter)

        self.time_var = tk.StringVar()
        self.time_label = ttk.Label(row, textvariable=self.time_var, anchor="w")
        self.time_label.grid(row=0, column=2, sticky="w")

    def _build_step_row(self) -> None:
        row = ttk.Frame(self.root)
        row.grid(row=2, column=0, sticky="w")
        jumps = [
            ("<", -1),
            (">", 1),
            ("<<", -round(self.video.nominal_fps)),
            (">>", round(self.video.nominal_fps)),
            ("<<<", -round(10 * self.video.nominal_fps)),
            (">>>", round(10 * self.video.nominal_fps)),
            ("<<<<", -round(60 * self.video.nominal_fps)),
            (">>>>", round(60 * self.video.nominal_fps)),
            ("<<<<<", -round(600 * self.video.nominal_fps)),
            (">>>>>", round(600 * self.video.nominal_fps)),
        ]
        self.step_buttons: list[tuple[int, ttk.Button]] = []
        for column, (text, delta) in enumerate(jumps):
            button = ttk.Button(row, text=text, command=lambda d=delta: self._step(d))
            button.grid(row=0, column=column, sticky="w")
            self.step_buttons.append((delta, button))

    def _build_roi_row(self) -> None:
        row = ttk.Frame(self.root)
        row.grid(row=3, column=0, sticky="w")
        self.roi_vars = {
            "X": tk.StringVar(),
            "Y": tk.StringVar(),
            "W": tk.StringVar(),
            "H": tk.StringVar(),
        }
        widths = {
            "X": len(str(max(0, self.video.width - 1))),
            "Y": len(str(max(0, self.video.height - 1))),
            "W": len(str(self.video.width)),
            "H": len(str(self.video.height)),
        }
        actions = {
            "X": (lambda: self._move_xy("x", 1), lambda: self._move_xy("x", -1)),
            "Y": (lambda: self._move_xy("y", 1), lambda: self._move_xy("y", -1)),
            "W": (lambda: self._resize_wh("w", 1), lambda: self._resize_wh("w", -1)),
            "H": (lambda: self._resize_wh("h", 1), lambda: self._resize_wh("h", -1)),
        }
        self.adjust_buttons: dict[str, tuple[ttk.Button, ttk.Button]] = {}

        column = 0
        for name in ("X", "Y", "W", "H"):
            ttk.Label(row, text=name).grid(row=0, column=column, rowspan=2, sticky="w")
            column += 1
            entry = ttk.Entry(
                row,
                textvariable=self.roi_vars[name],
                width=max(1, widths[name]),
                state="readonly",
            )
            entry.grid(row=0, column=column, rowspan=2, sticky="w")
            column += 1
            up = ttk.Button(row, text="▲", width=2, command=actions[name][0])
            down = ttk.Button(row, text="▼", width=2, command=actions[name][1])
            up.grid(row=0, column=column, sticky="w")
            down.grid(row=1, column=column, sticky="w")
            self.adjust_buttons[name] = (up, down)
            column += 1

        self.unlock_button = ttk.Button(row, text="XY Unlock", command=self._unlock)
        self.unlock_button.grid(row=0, column=column, rowspan=2, sticky="w")
        column += 1
        self.copy_button = ttk.Button(row, text="Copy", command=self._copy)
        self.copy_button.grid(row=0, column=column, rowspan=2, sticky="w")

    @staticmethod
    def _format_time(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    def _update_time(self) -> None:
        current = float(self.video.elapsed(self.current_index))
        total = float(self.video.duration)
        self.time_var.set(f"{self._format_time(current)} / {self._format_time(total)}")

    def _set_index(self, frame_index: int, *, immediate: bool) -> None:
        frame_index = min(max(frame_index, 0), self.video.frame_count - 1)
        self.current_index = frame_index
        self.frame_var.set(str(frame_index))
        self._setting_scale = True
        self.seek_var.set(frame_index)
        self._setting_scale = False
        self._update_time()
        self._update_step_buttons()

        if self._seek_after is not None:
            self.root.after_cancel(self._seek_after)
            self._seek_after = None
        if immediate:
            self.worker.request(frame_index)
        else:
            self._seek_after = self.root.after(50, self._request_current)

    def _request_current(self) -> None:
        self._seek_after = None
        self.worker.request(self.current_index)

    def _on_scale(self, value: str) -> None:
        if self._setting_scale:
            return
        self._set_index(round(float(value)), immediate=False)

    def _on_frame_enter(self, _event=None) -> None:
        try:
            value = int(self.frame_var.get())
        except ValueError:
            self.frame_var.set(str(self.current_index))
            return
        if not 0 <= value < self.video.frame_count:
            self.frame_var.set(str(self.current_index))
            return
        self._set_index(value, immediate=True)

    def _step(self, delta: int) -> None:
        self._set_index(self.current_index + delta, immediate=True)

    def _update_step_buttons(self) -> None:
        at_first = self.current_index == 0
        at_last = self.current_index == self.video.frame_count - 1
        for delta, button in self.step_buttons:
            disabled = (delta < 0 and at_first) or (delta > 0 and at_last)
            button.state(["disabled"] if disabled else ["!disabled"])

    def _poll_decode_results(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, index, value = self._decode_results.get_nowait()
                if kind == "frame":
                    assert index is not None and isinstance(value, Image.Image)
                    self._receive_frame(index, value)
                else:
                    assert isinstance(value, BaseException)
                    self._decode_error(value)
        except queue.Empty:
            pass
        self.root.after(20, self._poll_decode_results)

    def _receive_frame(self, frame_index: int, image: Image.Image) -> None:
        if self._closing or frame_index != self.current_index:
            return
        self.source_image = image
        self._render_image()

    def _decode_error(self, exc: BaseException) -> None:
        if self._closing:
            return
        messagebox.showerror("ROI Selection", str(exc), parent=self.root)

    def _render_image(self) -> None:
        if self.source_image is None:
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        image = self.source_image.resize((width, height), Image.Resampling.BILINEAR)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.itemconfigure(self._image_item, image=self.photo)
        self.canvas.coords(self._image_item, 0, 0)
        self._draw_roi()

    def _on_canvas_configure(self, _event=None) -> None:
        if self._resize_after is not None:
            self.root.after_cancel(self._resize_after)
        self._resize_after = self.root.after(30, self._finish_resize)

    def _finish_resize(self) -> None:
        self._resize_after = None
        self._render_image()

    def _on_root_configure(self, event) -> None:
        if event.widget is not self.root or self._controls_height <= 0:
            return
        size = (event.width, event.height)
        if self._last_root_size is None:
            self._last_root_size = size
            return
        if self._geometry_after is not None:
            self.root.after_cancel(self._geometry_after)
        old_width, old_height = self._last_root_size
        self._last_root_size = size
        self._geometry_after = self.root.after(
            20,
            self._enforce_aspect,
            size,
            abs(event.width - old_width),
            abs(event.height - old_height),
        )

    def _enforce_aspect(self, size: tuple[int, int], dw: int, dh: int) -> None:
        self._geometry_after = None
        width, height = size
        if dw >= dh:
            frame_width = width
            frame_height = round(frame_width * self.video.height / self.video.width)
            target_width = frame_width
            target_height = frame_height + self._controls_height
        else:
            frame_height = max(1, height - self._controls_height)
            frame_width = round(frame_height * self.video.width / self.video.height)
            target_width = frame_width
            target_height = frame_height + self._controls_height
        if abs(target_width - width) > 1 or abs(target_height - height) > 1:
            self.root.geometry(f"{target_width}x{target_height}")
            self._last_root_size = (target_width, target_height)

    def _source_position(self, event) -> tuple[float, float]:
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        x = min(max(event.x, 0), canvas_width - 1) * self.video.width / canvas_width
        y = min(max(event.y, 0), canvas_height - 1) * self.video.height / canvas_height
        return x, y

    def _on_motion(self, event) -> None:
        if self.roi.locked:
            return
        x, y = self._source_position(event)
        self.roi.position_at(x, y)
        self._update_roi_controls()
        self._draw_roi(show=True)

    def _on_leave(self, _event=None) -> None:
        if not self.roi.locked:
            self.canvas.itemconfigure(self._roi_item, state="hidden")

    def _on_canvas_click(self, event) -> None:
        if self.roi.locked:
            return
        x, y = self._source_position(event)
        self.roi.position_at(x, y)
        self.roi.lock()
        self._update_roi_controls()
        self._draw_roi(show=True)

    def _unlock(self) -> None:
        self.roi.unlock()
        self._update_roi_controls()
        self.canvas.itemconfigure(self._roi_item, state="hidden")

    def _move_xy(self, axis: str, delta: int) -> None:
        if axis == "x":
            self.roi.move_x(delta)
        else:
            self.roi.move_y(delta)
        self._update_roi_controls()
        self._draw_roi(show=True)

    def _resize_wh(self, axis: str, delta: int) -> None:
        if axis == "w":
            self.roi.resize_width(delta)
        else:
            self.roi.resize_height(delta)
        self._update_roi_controls()
        self._draw_roi(show=self.roi.locked)

    def _update_roi_controls(self) -> None:
        values = {
            "X": self.roi.x,
            "Y": self.roi.y,
            "W": self.roi.width,
            "H": self.roi.height,
        }
        for name, value in values.items():
            self.roi_vars[name].set(str(value))

        x_up, x_down = self.adjust_buttons["X"]
        y_up, y_down = self.adjust_buttons["Y"]
        w_up, w_down = self.adjust_buttons["W"]
        h_up, h_down = self.adjust_buttons["H"]

        def set_enabled(button: ttk.Button, enabled: bool) -> None:
            button.state(["!disabled"] if enabled else ["disabled"])

        set_enabled(x_up, self.roi.locked and self.roi.x < self.video.width - self.roi.width)
        set_enabled(x_down, self.roi.locked and self.roi.x > 0)
        set_enabled(y_up, self.roi.locked and self.roi.y < self.video.height - self.roi.height)
        set_enabled(y_down, self.roi.locked and self.roi.y > 0)
        set_enabled(w_up, self.roi.width < self.video.width)
        set_enabled(w_down, self.roi.width > 1)
        set_enabled(h_up, self.roi.height < self.video.height)
        set_enabled(h_down, self.roi.height > 1)
        set_enabled(self.unlock_button, self.roi.locked)
        set_enabled(self.copy_button, self.roi.locked)

    def _draw_roi(self, *, show: bool | None = None) -> None:
        if show is False:
            self.canvas.itemconfigure(self._roi_item, state="hidden")
            return
        if not self.roi.locked and show is None:
            return
        canvas_width = max(1, self.canvas.winfo_width())
        canvas_height = max(1, self.canvas.winfo_height())
        sx = canvas_width / self.video.width
        sy = canvas_height / self.video.height
        x0 = self.roi.x * sx
        y0 = self.roi.y * sy
        x1 = (self.roi.x + self.roi.width) * sx
        y1 = (self.roi.y + self.roi.height) * sy
        self.canvas.coords(self._roi_item, x0, y0, x1, y1)
        self.canvas.itemconfigure(self._roi_item, state="normal")
        self.canvas.tag_raise(self._roi_item)

    def _copy(self) -> None:
        if not self.roi.locked:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.roi.copy_text())
        self.root.update_idletasks()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._seek_after is not None:
            self.root.after_cancel(self._seek_after)
        self.worker.close()
        self.root.destroy()


def run() -> None:
    root = tk.Tk()
    root.withdraw()
    capture = filedialog.askdirectory(title="Capture", parent=root)
    if not capture:
        root.destroy()
        return

    try:
        video = build_virtual_video(Path(capture))
    except (MediaError, OSError, ValueError) as exc:
        messagebox.showerror("ROI Selection", str(exc), parent=root)
        root.destroy()
        return

    root.deiconify()
    RoiSelectionApp(root, video)
    root.mainloop()
