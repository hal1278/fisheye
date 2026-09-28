# ROI Selection stage

## Purpose

ROI Selection is a small interactive tool for selecting the fixed image region containing the TIMEPULSE LED/fiber spot.

User-facing command:

```text
fisheye roi
```

It does not modify capture data and does not create a processing run directory. Its only persisted output is the ROI text copied to the clipboard.

## Input selection

On startup, show a directory chooser.

The selected directory is directly:

```text
recordings/YYYYMMDD_HHMMSS/capture/
```

The directory must contain `session.json` and one or more `video_*.mkv` files. Do not silently fall back to a parent directory or legacy layout.

Canceling the chooser exits without another dialog.

The segment files are presented as one virtual movie.

## Window

Window title:

```text
ROI Selection
```

The initial frame viewport size equals the source frame size. Controls are placed below it.

The window is resizable, but the frame viewport always preserves the source aspect ratio. The image must not be stretched.

Do not add letterboxing/pillarboxing inside the frame viewport. Window geometry is constrained so that the viewport itself retains the source aspect ratio while the control area keeps its required height.

The frame viewport has no padding, border, caption, or other unused margin.

Apart from the controls specified below, do not add explanatory labels, headings, captions, status bars, or decorative text.

Closing the title-bar X terminates any decode worker/subprocess and exits.

## Virtual frame sequence

All `video_*.mkv` segments are ordered by their numeric suffix and exposed as a single zero-based frame sequence:

```text
0 ... N-1
```

Segment boundaries are not exposed as a normal UI concept.

The source PTS of every frame is authoritative for elapsed-time display. Do not compute time as `frame / nominal_fps`.

A shared media layer should provide:

- segment discovery;
- virtual frame indexing;
- PTS/time-base access;
- seek/decode by virtual frame.

For the current MJPEG capture, packet/frame indexing may use the intra-frame nature of the stream, but decoded-frame PTS remains the final identity check after a seek.

## Media implementation

Use PyAV for in-process demuxing, seeking, decoding, frame PTS, and stream time bases.

Do not start a new FFmpeg process for every seek operation.

Use a background worker for seek/decode. Tk event handling and widget updates remain on the GUI thread.

A small decoded-frame LRU cache may be used for repeated local stepping.

Pillow is used to resize frames for display while preserving aspect ratio and to convert images to Tk-compatible objects.

## Seek controls

The first control row contains, from left to right:

1. seek bar;
2. frame-number input field;
3. current source elapsed time and total source duration.

Time format:

```text
HH:MM:SS.mmm / HH:MM:SS.mmm
```

The seek bar range is `0 ... N-1` with one-frame resolution.

The frame field displays the current zero-based virtual frame number. Pressing Enter jumps to the entered frame if it is in range. Invalid input restores the current value without an explanatory dialog.

Seeking by any mechanism updates the seek bar, frame field, elapsed time, and displayed frame.

Seek-bar dragging should debounce decode requests so stale intermediate requests do not accumulate.

## Step buttons

The second control row is left-aligned:

```text
< > << >> <<< >>> <<<< >>>> <<<<< >>>>>
```

The final right button is `>>>>>`.

Frame increments are:

- `<` / `>`: -/+ 1 frame;
- `<<` / `>>`: -/+ `round(fps)` frames;
- `<<<` / `>>>`: -/+ `round(10 * fps)` frames;
- `<<<<` / `>>>>`: -/+ `round(60 * fps)` frames;
- `<<<<<` / `>>>>>`: -/+ `round(600 * fps)` frames.

Targets are clamped to `0 ... N-1`. Directional buttons are disabled only when already at the corresponding end.

The configured nominal frame rate is used only to define these convenience jump distances, not as a timing source.

## ROI coordinates

The third control row is left-aligned and contains fixed-position value fields for:

```text
X Y W H
```

Each field is display-only. Field widths are derived from source dimensions so changing numeric values does not move subsequent controls.

Each value has vertically stacked up/down buttons immediately to its right.

Then show:

```text
XY Unlock
Copy
```

in that order.

ROI semantics match a normal image crop:

- `X`, `Y`: upper-left source-pixel coordinate;
- `W`, `H`: width and height in source pixels.

Maintain:

```text
0 <= X <= frame_width - W
0 <= Y <= frame_height - H
1 <= W <= frame_width
1 <= H <= frame_height
```

Initial W/H are 32 x 32 pixels.

## Unlocked XY state

Initially XY is unlocked.

In this state:

- X/Y up/down buttons are disabled;
- `XY Unlock` is disabled;
- `Copy` is disabled;
- W/H up/down buttons remain enabled.

When the pointer is over the frame viewport, draw an unfilled rectangle centered as closely as possible on the pointer with the current W/H.

Clamp X/Y so the rectangle always remains entirely inside the source frame. Near an image edge, the pointer therefore need not remain exactly at the rectangle center.

Pointer motion updates the rectangle and displayed X/Y.

Left-clicking the frame locks X/Y at the current position.

## Locked XY state

After a frame click:

- pointer motion no longer changes the ROI;
- X/Y up/down buttons are enabled subject to bounds;
- `XY Unlock` is enabled;
- `Copy` is enabled.

Seeking to another frame retains the same source-pixel ROI so its stability over time can be inspected.

A further frame click does not relocate the ROI while XY is locked.

`XY Unlock` returns to pointer-following selection without changing W/H.

## Manual changes

X/Y up/down changes the corresponding coordinate by one source pixel and is enabled only when the resulting ROI remains in bounds.

W/H up/down changes size by one source pixel and is available in both locked and unlocked states wherever the resulting rectangle can be kept in the frame.

When W/H changes, preserve the current ROI center as closely as possible and clamp X/Y as necessary.

## Copy

When XY is locked, `Copy` places exactly the following form on the clipboard:

```text
--roi X,Y,W,H
```

Example:

```text
--roi 512,608,32,32
```

Do not add quotes, explanatory text, or newline-dependent formatting. The button caption remains `Copy` after activation.

## Error handling

Use a modal error dialog only for conditions that prevent operation, including:

- missing `session.json`;
- no `video_*.mkv`;
- incompatible segment dimensions/codecs;
- unusable timestamps;
- demux/decode failure;
- unavailable required GUI/media dependency.

Do not add a permanent status bar solely for error reporting.
