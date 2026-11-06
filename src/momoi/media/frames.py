from pathlib import Path

import av

MAX_FRAME_WIDTH = 1280


def extract_frames(path: str | Path, seconds: list[float]) -> list[tuple[float, bytes]]:
    """Return ``(timestamp, jpeg)`` for the frame shown at each requested second."""
    frames: list[tuple[float, bytes]] = []
    with av.logging.Capture(), av.open(str(path)) as container:
        stream = container.streams.video[0]
        length = float(stream.duration * stream.time_base) if stream.duration else None
        for second in seconds:
            target = min(float(second), length) if length is not None else float(second)
            container.seek(int(target / stream.time_base), stream=stream, backward=True)
            chosen = None
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                if chosen is not None and float(frame.pts * frame.time_base) > target:
                    break
                chosen = frame
            if chosen is None:
                continue
            frames.append((round(float(chosen.pts * chosen.time_base), 2), _jpeg(chosen)))
    return frames


def _jpeg(frame: av.VideoFrame) -> bytes:
    width, height = frame.width, frame.height
    if width > MAX_FRAME_WIDTH:
        width, height = MAX_FRAME_WIDTH, max(2, round(height * MAX_FRAME_WIDTH / width / 2) * 2)
    encoder = av.CodecContext.create("mjpeg", "w")
    encoder.width, encoder.height = width, height
    encoder.pix_fmt = "yuvj420p"
    encoder.time_base = frame.time_base or 1
    image = frame.reformat(width=width, height=height, format="yuvj420p")
    image.pts = None
    return b"".join(bytes(packet) for packet in [*encoder.encode(image), *encoder.encode(None)])
