import asyncio
import functools
import http.server
import threading
from contextlib import contextmanager
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest

from momoi.media.recorder import Recorder, RecorderError


def make_video(path, *, codec="libx264", seconds=4, audio_codec=None):
    with av.open(str(path), "w") as output:
        stream = output.add_stream(codec, rate=10)
        stream.width = 64
        stream.height = 64
        stream.pix_fmt = "yuv420p"
        stream.options = {"g": "10", "bf": "0"}
        audio = None
        if audio_codec:
            audio = output.add_stream(audio_codec, rate=48000)
            audio.layout = "mono"
        for index in range(seconds * 10):
            frame = av.VideoFrame.from_ndarray(
                np.full((64, 64, 3), index % 256, dtype=np.uint8), format="rgb24"
            )
            frame.pts = index
            output.mux(stream.encode(frame))
            if audio is not None:
                audio_frame = av.AudioFrame.from_ndarray(
                    np.zeros((1, 4800), dtype=np.int16), layout="mono"
                )
                audio_frame.sample_rate = 48000
                audio_frame.pts = index * 4800
                audio_frame.time_base = Fraction(1, 48000)
                output.mux(audio.encode(audio_frame))
        output.mux(stream.encode(None))
        if audio is not None:
            output.mux(audio.encode(None))


@contextmanager
def serve(directory, types=None):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def guess_type(self, path):
            name = path.rsplit("/", 1)[-1]
            return (types or {}).get(name) or super().guess_type(path)

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(directory))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_remux_truncates_and_atomically_commits(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    make_video(source)
    directory = tmp_path / "recordings"
    from pathlib import Path
    replace = Path.replace
    renamed = []

    def checked_replace(part, final):
        assert part.name == "run.part.mp4"
        assert part.is_file()
        assert not final.exists()
        renamed.append(final)
        return replace(part, final)

    monkeypatch.setattr(Path, "replace", checked_replace)
    with serve(tmp_path) as base:
        result = asyncio.run(Recorder(directory).record(base + "/source.mp4", "run", 2))
    assert 1.9 <= result["duration_seconds"] <= 2.1
    assert result["size_bytes"] == (directory / "run.mp4").stat().st_size
    assert renamed == [directory / "run.mp4"]
    assert not list(directory.glob("*.part.mp4"))
    data = (directory / "run.mp4").read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")
    with av.open(result["path"]) as output:
        assert output.streams.video[0].codec_context.name == "h264"
        assert len(list(output.decode(video=0))) == 20


def test_incompatible_codec_requires_explicit_transcode(tmp_path):
    make_video(tmp_path / "source.mp4", codec="mpeg4")
    recorder = Recorder(tmp_path / "recordings")
    with serve(tmp_path) as base:
        with pytest.raises(RecorderError, match="transcode_disabled"):
            asyncio.run(recorder.record(base + "/source.mp4", "disabled", 2))
        result = asyncio.run(Recorder(recorder.directory, allow_transcode=True).record(
            base + "/source.mp4", "enabled", 2
        ))
    with av.open(result["path"]) as output:
        assert output.streams.video[0].codec_context.name == "h264"
        assert len(list(output.decode(video=0))) == 20
    assert not list(recorder.directory.glob("*.part.mp4"))


def test_encrypted_hls_event_clip_with_discontinuities(tmp_path):
    # Mirrors camera cloud event recordings: extensionless AES-128 segments,
    # each a standalone fragmented MP4 restarting at zero, last one truncated.
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key, iv = bytes(range(16)), bytes(range(16, 32))
    (tmp_path / "key").write_bytes(key)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:3",
             '#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x' + iv.hex()]
    for index in range(3):
        segment = tmp_path / f"segment{index}.mp4"
        with av.open(str(segment), "w", format="mp4", options={
            "movflags": "frag_keyframe+empty_moov+default_base_moof"
        }) as output:
            stream = output.add_stream("libx265", rate=10)
            stream.width = stream.height = 64
            stream.pix_fmt = "yuv420p"
            stream.options = {"x265-params": "log-level=none:keyint=10:bframes=0"}
            for pts in range(30):
                frame = av.VideoFrame.from_ndarray(
                    np.full((64, 64, 3), pts, dtype=np.uint8), format="rgb24"
                )
                frame.pts = pts
                output.mux(stream.encode(frame))
            output.mux(stream.encode(None))
        data = segment.read_bytes()
        if index == 2:
            data = data[: len(data) - 200]
        padder = padding.PKCS7(128).padder()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        (tmp_path / f"seg{index}").write_bytes(
            encryptor.update(padder.update(data) + padder.finalize()) + encryptor.finalize()
        )
        lines += ["#EXTINF:3.0,", f"seg{index}?token=x", "#EXT-X-DISCONTINUITY"]
    (tmp_path / "playlist").write_text("\n".join(lines + ["#EXT-X-ENDLIST", ""]))
    recorder = Recorder(tmp_path / "recordings", allow_transcode=True)
    with serve(tmp_path, {"playlist": "application/vnd.apple.mpegurl"}) as base:
        result = asyncio.run(recorder.record(base + "/playlist?data=x", "event", 30))
    with av.open(result["path"]) as output:
        decoded = list(output.decode(video=0))
        assert output.streams.video[0].codec_context.name == "h264"
    assert len(decoded) >= 80
    assert all(a.pts < b.pts for a, b in zip(decoded, decoded[1:]))
    assert 8 <= result["duration_seconds"] <= 9.1


def test_transcode_live_camera_timestamps_and_misleading_average_rate(tmp_path):
    # go2rtc's live H.265 MP4 reports ~989 fps and occasionally repeats or
    # moves a timestamp backwards, although the camera runs at about 20 fps.
    frames = []
    for index in range(40):
        frame = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24")
        frame.time_base = Fraction(1, 90000)
        frame.pts = (11 if index == 12 else 10 if index == 13 else index) * 4500
        frames.append(frame)
    packet = SimpleNamespace(
        stream=SimpleNamespace(type="video", index=0), is_keyframe=True,
        pts=0, dts=0, duration=None, time_base=Fraction(1, 90000),
        decode=lambda: iter(frames),
    )
    source = SimpleNamespace(demux=lambda streams: iter([packet]))
    video = SimpleNamespace(
        average_rate=Fraction(90000, 91), guessed_rate=Fraction(20),
        codec_context=SimpleNamespace(width=64, height=64),
    )
    path = tmp_path / "camera.mp4"
    with av.open(str(path), "w", format="mp4") as output:
        Recorder._transcode(source, output, [video], 2, lambda: None)
    with av.open(str(path)) as output:
        stream = output.streams.video[0]
        decoded = list(output.decode(video=0))
        assert stream.codec_context.name == "h264"
        assert len(decoded) == 38
        assert all(a.pts < b.pts for a, b in zip(decoded, decoded[1:]))
        assert 1.9 <= float(stream.duration * stream.time_base) <= 2.1


@pytest.mark.parametrize("video_codec,audio_codec,transcode", [
    ("libx264", "aac", False), ("mpeg4", "pcm_s16le", True),
])
def test_recording_preserves_audio(tmp_path, video_codec, audio_codec, transcode):
    make_video(tmp_path / "source.mkv", codec=video_codec, audio_codec=audio_codec)
    with serve(tmp_path) as base:
        result = asyncio.run(Recorder(tmp_path / "recordings", allow_transcode=transcode).record(
            base + "/source.mkv", "run", 2
        ))
    with av.open(result["path"]) as output:
        assert len(output.streams.video) == len(output.streams.audio) == 1
        assert output.streams.audio[0].codec_context.name == "aac"
        assert len(list(output.decode(audio=0))) > 0


def test_size_limit_cleans_partial_file(tmp_path):
    make_video(tmp_path / "source.mp4")
    directory = tmp_path / "recordings"
    with serve(tmp_path) as base, pytest.raises(RecorderError, match="too_large"):
        asyncio.run(Recorder(directory, max_bytes=64).record(base + "/source.mp4", "run", 2))
    assert list(directory.iterdir()) == []


def test_no_video_is_rejected(tmp_path):
    with av.open(str(tmp_path / "audio.wav"), "w") as output:
        stream = output.add_stream("pcm_s16le", rate=8000)
        frame = av.AudioFrame.from_ndarray(np.zeros((1, 16000), dtype=np.int16), layout="mono")
        frame.sample_rate = 8000
        output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    directory = tmp_path / "recordings"
    with serve(tmp_path) as base, pytest.raises(RecorderError, match="has_no_video"):
        asyncio.run(Recorder(directory).record(base + "/audio.wav", "run", 2))
    assert not list(directory.glob("*.mp4"))


@pytest.mark.parametrize("source", ["file:///etc/passwd", "concat:http://camera/a|file:/etc/passwd", "pipe:0", "http:///missing"])
def test_source_scheme_whitelist(tmp_path, source):
    with pytest.raises(RecorderError, match="invalid_video_source"):
        asyncio.run(Recorder(tmp_path).record(source, "run", 2))


def test_cancel_before_open(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RecorderError, match="cancelled"):
        asyncio.run(Recorder(tmp_path).record("http://camera/video", "run", 2, cancel=cancel))
    assert not list(tmp_path.iterdir())


def test_cancel_during_packets_removes_partial(tmp_path, monkeypatch):
    make_video(tmp_path / "source.mp4")
    cancel = threading.Event()
    original_remux = Recorder._remux

    def remux(input_file, output, streams, duration, check):
        packets = 0

        def cancel_after_packets():
            nonlocal packets
            packets += 1
            if packets == 4:
                cancel.set()
            check()

        original_remux(input_file, output, streams, duration, cancel_after_packets)

    monkeypatch.setattr(Recorder, "_remux", staticmethod(remux))
    directory = tmp_path / "recordings"
    with serve(tmp_path) as base, pytest.raises(RecorderError, match="recording_cancelled"):
        asyncio.run(Recorder(directory).record(base + "/source.mp4", "run", 2, cancel=cancel))
    assert list(directory.iterdir()) == []


def test_thread_cancel_is_joined_and_connection_limited(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, concurrency=2)
    started = threading.Event()
    finished = threading.Event()
    active = 0
    peak = 0

    def record(source, run_id, duration, cancel):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.set()
        cancel.wait(2)
        active -= 1
        finished.set()
        raise RecorderError("recording_cancelled")

    monkeypatch.setattr(recorder, "_record", record)

    async def exercise():
        first = asyncio.create_task(recorder.record("http://camera/a", "one", 2))
        await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(recorder.record("http://camera/a", "two", 2))
        await asyncio.sleep(0)
        second.cancel()
        first.cancel()
        for task in [first, second]:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()

    asyncio.run(exercise())
    assert peak == 1
    assert recorder._sources == {}


def test_repeated_cancel_keeps_locks_until_thread_exits(tmp_path, monkeypatch):
    recorder = Recorder(tmp_path, concurrency=1)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cancel = threading.Event()
    calls = []

    def record(source, run_id, duration, signal):
        calls.append(run_id)
        started.set()
        try:
            assert release.wait(5), "test did not release recording thread"
            raise RecorderError("recording_cancelled")
        finally:
            finished.set()

    monkeypatch.setattr(recorder, "_record", record)

    async def exercise():
        first = asyncio.create_task(recorder.record(
            "http://camera/a", "first", 2, cancel=cancel
        ))
        waiting = []
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel("first cancellation")
            await asyncio.sleep(0)
            assert cancel.is_set()
            first.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not first.done()
            assert not finished.is_set()
            assert recorder._sources["http://camera/a"][0].locked()
            assert recorder._slots.locked()

            waiting = [
                asyncio.create_task(recorder.record("http://camera/a", "same", 2)),
                asyncio.create_task(recorder.record("http://camera/b", "other", 2)),
            ]
            await asyncio.sleep(0)
            assert calls == ["first"]
            for task in waiting:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert recorder._sources["http://camera/a"][1] == 1
            assert recorder._slots.locked()
        finally:
            release.set()
            for task in waiting:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
            with pytest.raises(asyncio.CancelledError, match="first cancellation"):
                await first
        assert finished.is_set()
        assert recorder._sources == {}
        assert not recorder._slots.locked()

    asyncio.run(exercise())


def test_total_wall_clock_deadline(tmp_path, monkeypatch):
    make_video(tmp_path / "source.mp4")
    import momoi.media.recorder as module
    times = iter([0, 34])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times, 34))
    # Call the blocking core directly: patching time.monotonic also changes the
    # asyncio event loop clock, which is unrelated to recording's deadline.
    with serve(tmp_path) as base, pytest.raises(RecorderError, match="recording_timeout"):
        Recorder(tmp_path / "recordings")._record(base + "/source.mp4", "run", 2, threading.Event())
    assert not list((tmp_path / "recordings").glob("*.mp4"))


def test_open_timeout_is_sanitized(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise av.error.TimeoutError(110, "secret=http://user:password@camera/?token=secret")

    monkeypatch.setattr(av, "open", fail)
    with pytest.raises(RecorderError) as caught:
        asyncio.run(Recorder(tmp_path).record("http://user:password@camera/?token=secret", "run", 2))
    assert str(caught.value) == "recording_timeout"


def test_http_read_timeout(tmp_path, monkeypatch):
    release = threading.Event()

    class Stall(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            release.wait(2)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stall)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    open_media = av.open

    def short_timeout(*args, **kwargs):
        kwargs["timeout"] = (0.1, 0.1)
        return open_media(*args, **kwargs)

    monkeypatch.setattr(av, "open", short_timeout)
    try:
        with pytest.raises(RecorderError, match="recording_timeout"):
            asyncio.run(Recorder(tmp_path).record(
                f"http://127.0.0.1:{server.server_port}/stall.mp4", "run", 2
            ))
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()
    assert not list(tmp_path.iterdir())


def test_http_playlist_cannot_open_local_file(tmp_path):
    private_media = tmp_path / "private.ts"
    make_video(private_media)
    public = tmp_path / "public"
    public.mkdir()
    (public / "source.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:4,\n{private_media.as_uri()}\n#EXT-X-ENDLIST\n"
    )
    directory = tmp_path / "recordings"
    with serve(public) as base, pytest.raises(RecorderError):
        asyncio.run(Recorder(directory).record(base + "/source.m3u8", "run", 2))
    assert not list(directory.glob("*.mp4"))
