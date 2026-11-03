import asyncio
import logging
import re
import threading
import time
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import av


logger = logging.getLogger(__name__)
SCHEMES = frozenset({"rtmp", "rtmps", "rtsp", "http", "https"})


class RecorderError(RuntimeError):
    """Recording failed; the message is safe to persist without URL credentials."""


class Recorder:
    def __init__(
        self,
        directory: Path,
        max_bytes: int = 50 * 1024 * 1024,
        concurrency: int = 2,
        allow_transcode: bool = False,
    ) -> None:
        if max_bytes <= 0 or concurrency <= 0:
            raise ValueError("recording limits must be positive")
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.allow_transcode = allow_transcode
        self._slots = asyncio.Semaphore(concurrency)
        self._sources: dict[str, tuple[asyncio.Lock, int]] = {}

    async def record(
        self,
        source: str,
        run_id: str,
        duration_seconds: int,
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, object]:
        parsed = urlsplit(source)
        if parsed.scheme.lower() not in SCHEMES or not parsed.hostname:
            raise RecorderError("invalid_video_source")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise RecorderError("invalid_recording_id")
        if isinstance(duration_seconds, bool) or not 1 <= duration_seconds <= 120:
            raise RecorderError("invalid_recording_duration")
        cancel = cancel if cancel is not None else threading.Event()
        source_lock, users = self._sources.get(source, (asyncio.Lock(), 0))
        self._sources[source] = (source_lock, users + 1)
        # Acquire the source before a global slot so repeated URLs cannot starve
        # unrelated cameras while waiting for their existing connection to close.
        try:
            async with source_lock, self._slots:
                if cancel.is_set():
                    raise RecorderError("recording_cancelled")
                logger.info("Recording camera host=%s", parsed.hostname)
                task = asyncio.create_task(asyncio.to_thread(
                    self._record, source, run_id, duration_seconds, cancel
                ))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancel.set()
                    # Closing an input from another thread is unsafe in FFmpeg.
                    # The bounded read timeout lets its owning thread close it.
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            # A repeated /stop must not release this camera's
                            # lock or concurrency slot while its thread lives.
                            continue
                        except RecorderError:
                            break
                    try:
                        task.result()
                    except RecorderError:
                        pass
                    raise
        finally:
            _, users = self._sources[source]
            if users == 1:
                self._sources.pop(source, None)
            else:
                self._sources[source] = (source_lock, users - 1)

    def _record(
        self, source: str, run_id: str, duration: int, cancel: threading.Event
    ) -> dict[str, object]:
        self.directory.mkdir(parents=True, exist_ok=True)
        part = self.directory / f"{run_id}.part.mp4"
        final = self.directory / f"{run_id}.mp4"
        deadline = time.monotonic() + duration + 30

        def check() -> None:
            if cancel.is_set():
                raise RecorderError("recording_cancelled")
            if time.monotonic() >= deadline:
                raise RecorderError("recording_timeout")
            if part.exists() and part.stat().st_size > self.max_bytes:
                raise RecorderError("recording_too_large")

        scheme = urlsplit(source).scheme.lower()
        options = {
            "rw_timeout": "5000000",
            "protocol_whitelist": "rtmp,rtmps,rtsp,http,https,tcp,tls,udp,rtp",
        }
        if scheme == "rtsp":
            options.update({"timeout": "5000000", "rtsp_transport": "tcp"})
        try:
            # FFmpeg errors can contain the entire URL; capture and discard them.
            with av.logging.Capture(), av.open(
                source, options=options, timeout=(5, 5)
            ) as input_file:
                check()
                if not input_file.streams.video:
                    raise RecorderError("recording_has_no_video")
                video = input_file.streams.video[0]
                streams = [video]
                if input_file.streams.audio:
                    streams.append(input_file.streams.audio[0])
                transcode = video.codec_context.name != "h264" or any(
                    stream.type == "audio" and stream.codec_context.name != "aac"
                    for stream in streams
                )
                if transcode and not self.allow_transcode:
                    raise RecorderError("recording_transcode_disabled")
                with av.open(
                    str(part), "w", format="mp4", options={"movflags": "+faststart"}
                ) as output:
                    if transcode:
                        self._transcode(input_file, output, streams, duration, check)
                    else:
                        self._remux(input_file, output, streams, duration, check)
            check()
            size = part.stat().st_size
            with av.logging.Capture(), av.open(str(part)) as recorded:
                if not recorded.streams.video:
                    raise RecorderError("recording_has_no_video")
                stream = recorded.streams.video[0]
                length = float(stream.duration * stream.time_base) if stream.duration else 0
                # Reading one frame also rejects an empty or corrupt video track.
                next(recorded.decode(video=0))
            if length < 1:
                raise RecorderError("recording_too_short")
            check()
            part.replace(final)
            return {"path": str(final), "duration_seconds": length, "size_bytes": size}
        except RecorderError:
            raise
        except Exception as error:
            if cancel.is_set():
                raise RecorderError("recording_cancelled") from None
            if isinstance(error, (av.error.TimeoutError, av.error.ExitError)):
                raise RecorderError("recording_timeout") from None
            raise RecorderError(f"recording_invalid_media:{type(error).__name__}") from None
        finally:
            part.unlink(missing_ok=True)

    @staticmethod
    def _remux(input_file, output, streams, duration, check) -> None:
        targets = {s.index: output.add_stream_from_template(s) for s in streams}
        start = None
        for packet in input_file.demux(streams):
            check()
            if packet.pts is None or packet.dts is None:
                continue
            if start is None:
                if packet.stream.type != "video" or not packet.is_keyframe:
                    continue
                start = packet.pts * packet.time_base
            elapsed = packet.pts * packet.time_base - start
            if packet.stream.type == "video" and elapsed >= duration:
                break
            if elapsed < 0 or elapsed >= duration:
                continue
            offset = int(start / packet.time_base)
            packet.pts -= offset
            packet.dts -= offset
            packet.stream = targets[packet.stream.index]
            output.mux(packet)

    @staticmethod
    def _transcode(input_file, output, streams, duration, check) -> None:
        video = streams[0]
        rate = video.guessed_rate or video.average_rate or Fraction(25)
        if rate <= 0 or rate > 120:
            rate = Fraction(25)
        target_video = output.add_stream("libx264", rate=rate)
        # Live MP4 average_rate is unreliable; retain the source timeline at
        # sufficient precision rather than quantizing it into nominal frames.
        video_time_base = Fraction(1, 90000)
        target_video.codec_context.time_base = video_time_base
        target_video.width = video.codec_context.width
        target_video.height = video.codec_context.height
        target_video.pix_fmt = "yuv420p"
        target_video.options = {"preset": "veryfast"}
        target_audio = None
        resampler = None
        if len(streams) > 1:
            audio = streams[1]
            target_audio = output.add_stream("aac", rate=audio.codec_context.sample_rate or 48000)
            channels = audio.codec_context.channels
            target_audio.layout = (
                "mono" if channels == 1 else "stereo" if channels == 2
                else audio.codec_context.layout
            )
            resampler = av.AudioResampler(format="fltp", layout=target_audio.layout, rate=target_audio.rate)
        start = None
        last_video_pts = None
        for packet in input_file.demux(streams):
            check()
            if start is None:
                if packet.stream.type != "video" or not packet.is_keyframe or packet.pts is None:
                    continue
                start = packet.pts * packet.time_base
            for frame in packet.decode():
                check()
                if frame.pts is None:
                    continue
                elapsed = frame.pts * frame.time_base - start
                if elapsed >= duration and isinstance(frame, av.VideoFrame):
                    for stream in [target_video, target_audio]:
                        if stream is not None:
                            output.mux(stream.encode(None))
                    return
                if elapsed < 0 or elapsed >= duration:
                    continue
                frame.pts -= int(start / frame.time_base)
                if isinstance(frame, av.VideoFrame):
                    pts = int(elapsed / video_time_base)
                    if last_video_pts is not None and pts <= last_video_pts:
                        continue
                    frame.pts, frame.time_base = pts, video_time_base
                    last_video_pts = pts
                    output.mux(target_video.encode(frame))
                elif resampler is not None:
                    for audio_frame in resampler.resample(frame):
                        output.mux(target_audio.encode(audio_frame))
        if resampler is not None:
            for frame in resampler.resample(None):
                output.mux(target_audio.encode(frame))
        for stream in [target_video, target_audio]:
            if stream is not None:
                output.mux(stream.encode(None))
