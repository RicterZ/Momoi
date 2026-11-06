import asyncio
import logging
import re
import threading
import time
from fractions import Fraction
from pathlib import Path
import urllib.request
from urllib.parse import urljoin, urlsplit

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
        elif scheme in {"http", "https"}:
            # Camera cloud HLS uses extensionless, AES-128 encrypted segments.
            # Local files stay excluded from the protocol whitelist.
            options.update({
                "protocol_whitelist": f"{options['protocol_whitelist']},crypto,data",
                "allowed_extensions": "ALL",
                "allowed_segment_extensions": "ALL",
                "extension_picky": "0",
            })
        try:
            # FFmpeg errors can contain the entire URL; capture and discard them.
            segments = None
            with av.logging.Capture(), av.open(
                source, options=options, timeout=(5, 5)
            ) as input_file:
                check()
                if input_file.format.name == "hls":
                    segments = _vod_segments(source)
                if segments is None:
                    self._write(input_file, part, duration, check)
            if segments is not None:
                # The HLS demuxer keeps the first segment's codec configuration
                # across discontinuities; decode each segment on its own.
                if not self.allow_transcode:
                    raise RecorderError("recording_transcode_disabled")
                with av.logging.Capture():
                    self._write_segments(segments, part, duration, check)
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

    def _write(self, input_file, part, duration, check) -> None:
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

    @staticmethod
    def _write_segments(segments, part, duration, check) -> None:
        encoder = None
        offset = Fraction(0)
        with av.open(
            str(part), "w", format="mp4", options={"movflags": "+faststart"}
        ) as output:
            for url, key, iv in segments:
                if offset >= duration:
                    break
                options = {"rw_timeout": "5000000", "protocol_whitelist": "crypto,http,https,tcp,tls"}
                if key is not None:
                    url = "crypto+" + url
                    options.update({"key": key.hex(), "iv": iv.hex()})
                try:
                    segment = av.open(url, options=options, timeout=(5, 5))
                except av.error.FFmpegError:
                    # An expired or truncated tail segment still leaves a clip.
                    if encoder is not None and offset > 0:
                        break
                    raise
                with segment:
                    if not segment.streams.video:
                        continue
                    streams = [segment.streams.video[0]]
                    if segment.streams.audio:
                        streams.append(segment.streams.audio[0])
                    if encoder is None:
                        encoder = _Encoder(output, *streams)
                    start = None
                    end = offset

                    def add(frame) -> None:
                        nonlocal end
                        if frame.pts is None:
                            return
                        elapsed = offset + frame.pts * frame.time_base - start
                        if elapsed < offset or elapsed >= duration:
                            return
                        encoder.add(frame, elapsed)
                        end = max(end, elapsed + (frame.duration or 0) * frame.time_base)

                    for packet, _ in Recorder._packets(segment, streams, check):
                        if start is None:
                            if (packet.stream.type != "video" or not packet.is_keyframe
                                    or packet.pts is None):
                                continue
                            start = packet.pts * packet.time_base
                        for frame in Recorder._decode(packet):
                            check()
                            add(frame)
                    if start is not None:
                        for stream in streams:
                            for frame in Recorder._drain(stream.codec_context):
                                add(frame)
                    offset = end
            if encoder is None:
                raise RecorderError("recording_has_no_video")
            encoder.close()

    @staticmethod
    def _packets(input_file, streams, check):
        """Yield ``(packet, restarted)`` on one continuous timeline per stream.

        HLS discontinuities restart each segment's timestamps at zero, and
        event recordings may end in a truncated fragment. ``restarted`` marks
        the first packet of a new segment for that stream.
        """
        packets = input_file.demux(streams)
        offsets: dict[int, int] = {}
        ends: dict[int, int] = {}
        yielded = False
        while True:
            check()
            try:
                packet = next(packets)
            except StopIteration:
                return
            except av.error.InvalidDataError:
                if yielded:
                    return
                raise
            if packet.pts is None:
                yield packet, False
                continue
            index = packet.stream.index
            offset = offsets.get(index, 0)
            end = ends.get(index)
            # Small reorders are live-camera jitter; a jump back over one
            # second is a new segment and continues after the previous one.
            restarted = end is not None and packet.pts + offset < end - int(1 / packet.time_base)
            if restarted:
                offset = end - packet.pts
                offsets[index] = offset
            packet.pts += offset
            if packet.dts is not None:
                packet.dts += offset
            ends[index] = max(end or 0, packet.pts + max(packet.duration or 0, 1))
            yielded = True
            yield packet, restarted

    @staticmethod
    def _decode(packet):
        try:
            return packet.decode()
        except av.error.FFmpegError:
            # A segment boundary can leave an undecodable video or audio
            # packet; skip it and keep the clip.
            return []

    @staticmethod
    def _drain(codec_context):
        # A new segment's IDR would discard frames still buffered for reordering.
        try:
            frames = list(codec_context.decode(None))
        except av.error.EOFError:
            frames = []
        codec_context.flush_buffers()
        return frames

    @staticmethod
    def _remux(input_file, output, streams, duration, check) -> None:
        targets = {s.index: output.add_stream_from_template(s) for s in streams}
        start = None
        for packet, _ in Recorder._packets(input_file, streams, check):
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
        encoder = _Encoder(output, *streams)
        start = None
        for packet, restarted in Recorder._packets(input_file, streams, check):
            if start is None:
                if packet.stream.type != "video" or not packet.is_keyframe or packet.pts is None:
                    continue
                start = packet.pts * packet.time_base
            frames = Recorder._drain(packet.stream.codec_context) if restarted else []
            frames += Recorder._decode(packet)
            for frame in frames:
                check()
                if frame.pts is None:
                    continue
                elapsed = frame.pts * frame.time_base - start
                if elapsed >= duration and isinstance(frame, av.VideoFrame):
                    encoder.close()
                    return
                if elapsed < 0 or elapsed >= duration:
                    continue
                encoder.add(frame, elapsed)
        encoder.close()


class _Encoder:
    """H.264/AAC encoder fed with frames placed on the clip's own timeline."""

    # Live MP4 average_rate is unreliable; retain the source timeline at
    # sufficient precision rather than quantizing it into nominal frames.
    VIDEO_TIME_BASE = Fraction(1, 90000)

    def __init__(self, output, video, audio=None) -> None:
        rate = video.guessed_rate or video.average_rate or Fraction(25)
        if rate <= 0 or rate > 120:
            rate = Fraction(25)
        self.output = output
        self.video = output.add_stream("libx264", rate=rate)
        self.video.codec_context.time_base = self.VIDEO_TIME_BASE
        self.video.width = video.codec_context.width
        self.video.height = video.codec_context.height
        self.video.pix_fmt = "yuv420p"
        self.video.options = {"preset": "veryfast"}
        self.audio = None
        self.resampler = None
        if audio is not None:
            self.audio = output.add_stream("aac", rate=audio.codec_context.sample_rate or 48000)
            channels = audio.codec_context.channels
            self.audio.layout = (
                "mono" if channels == 1 else "stereo" if channels == 2
                else audio.codec_context.layout
            )
            self.resampler = av.AudioResampler(
                format="fltp", layout=self.audio.layout, rate=self.audio.rate
            )
        self.last_video_pts = None

    def add(self, frame, elapsed: Fraction) -> None:
        if isinstance(frame, av.VideoFrame):
            pts = int(elapsed / self.VIDEO_TIME_BASE)
            if self.last_video_pts is not None and pts <= self.last_video_pts:
                return
            if (frame.width, frame.height) != (self.video.width, self.video.height):
                frame = frame.reformat(width=self.video.width, height=self.video.height)
            frame.pts, frame.time_base = pts, self.VIDEO_TIME_BASE
            self.last_video_pts = pts
            self.output.mux(self.video.encode(frame))
        elif self.resampler is not None:
            frame.pts = int(elapsed / frame.time_base)
            for audio_frame in self.resampler.resample(frame):
                self.output.mux(self.audio.encode(audio_frame))

    def close(self) -> None:
        if self.resampler is not None:
            for frame in self.resampler.resample(None):
                self.output.mux(self.audio.encode(frame))
        for stream in [self.video, self.audio]:
            if stream is not None:
                self.output.mux(stream.encode(None))


def _vod_segments(source: str) -> list[tuple[str, bytes | None, bytes | None]] | None:
    """Return ``(url, key, iv)`` per segment of a finished HLS playlist.

    Live playlists and unsupported encryption return ``None`` so the HLS
    demuxer keeps handling them.
    """
    try:
        with urllib.request.urlopen(source, timeout=5) as response:
            if urlsplit(response.geturl()).scheme.lower() not in {"http", "https"}:
                return None
            body = response.read(1024 * 1024).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        return None
    if any(line.startswith("#EXT-X-STREAM-INF") or line.startswith("#EXT-X-MAP")
           for line in lines):
        return None
    sequence = 0
    key = iv = None
    keys: dict[str, bytes] = {}
    segments = []
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            attributes = dict(_ATTRIBUTE.findall(line.split(":", 1)[1]))
            method = attributes.get("METHOD", "NONE")
            if method == "NONE":
                key = iv = None
                continue
            uri = urljoin(source, attributes.get("URI", "").strip('"'))
            if method != "AES-128" or urlsplit(uri).scheme.lower() not in {"http", "https"}:
                return None
            if uri not in keys:
                try:
                    with urllib.request.urlopen(uri, timeout=5) as response:
                        keys[uri] = response.read(17)
                except (OSError, ValueError):
                    return None
                if len(keys[uri]) != 16:
                    return None
            key = keys[uri]
            iv = bytes.fromhex(attributes["IV"][2:]) if "IV" in attributes else None
        elif not line.startswith("#"):
            url = urljoin(source, line)
            if urlsplit(url).scheme.lower() not in {"http", "https"}:
                return None
            segment_iv = iv or (sequence + len(segments)).to_bytes(16, "big")
            segments.append((url, key, segment_iv if key is not None else None))
    return segments or None


_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
