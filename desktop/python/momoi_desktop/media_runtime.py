"""Pinned Windows FFmpeg component, installed once outside the code release."""
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from momoi.observability.events import log_event

logger = logging.getLogger(__name__)
WHEEL_NAME = "imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"
WHEEL_SHA256 = "02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a"
WHEEL_SIZE = 31246824
BINARY_NAME = "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
BINARY_SHA256 = "2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3"
BINARY_SIZE = 87638016
COMPONENT_URL = "https://momoi-1253047877.cos.ap-guangzhou.myqcloud.com/windows/components/" + WHEEL_NAME
_lock = threading.Lock()


def matches(path, size, digest):
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == digest


def install_ffmpeg(cache):
    """Never execute an unverified cached/downloaded executable."""
    with _lock:
        directory = Path(cache) / "ffmpeg" / BINARY_SHA256[:16]
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "ffmpeg.exe"
        if matches(binary, BINARY_SIZE, BINARY_SHA256):
            return str(binary)
        log_event(logger, logging.INFO, "media_component_installing", component="ffmpeg")
        with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as temporary:
            archive = Path(temporary) / WHEEL_NAME
            total = 0
            deadline = time.monotonic() + 180
            with urllib.request.urlopen(COMPONENT_URL, timeout=30) as source, archive.open("wb") as target:
                while chunk := source.read(65536):
                    total += len(chunk)
                    if total > WHEEL_SIZE or time.monotonic() > deadline:
                        raise OSError("FFmpeg component download exceeds its size/time limit")
                    target.write(chunk)
            if not matches(archive, WHEEL_SIZE, WHEEL_SHA256):
                raise OSError("FFmpeg component checksum mismatch")
            with zipfile.ZipFile(archive) as bundle:
                entry = bundle.getinfo(BINARY_NAME)
                if entry.file_size != BINARY_SIZE or bundle.namelist().count(BINARY_NAME) != 1:
                    raise OSError("FFmpeg component binary size/file list mismatch")
                staged = Path(temporary) / "ffmpeg.exe"
                with bundle.open(entry) as source, staged.open("wb") as target:
                    shutil.copyfileobj(source, target)
                if not matches(staged, BINARY_SIZE, BINARY_SHA256):
                    raise OSError("FFmpeg executable checksum mismatch")
                (directory / "imageio-ffmpeg-LICENSE.txt").write_bytes(bundle.read("imageio_ffmpeg-0.6.0.dist-info/LICENSE"))
                shutil.copyfile(Path(__file__).with_name("media_licenses") / "FFmpeg-GPLv3.txt", directory / "FFmpeg-GPLv3.txt")
                (directory / "SOURCE.txt").write_text(
                    "FFmpeg 7.1 Windows x64, distributed by imageio-ffmpeg 0.6.0.\n"
                    "Binary provenance: https://pypi.org/project/imageio-ffmpeg/0.6.0/\n"
                    "FFmpeg sources and license: https://ffmpeg.org/download.html\n"
                    "Run ffmpeg.exe -L for the binary's build/license details.\n", encoding="utf-8")
                staged.replace(binary)
        log_event(logger, logging.INFO, "media_component_ready", component="ffmpeg")
        return str(binary)


def ffmpeg_executable():
    if existing := shutil.which("ffmpeg"):
        return existing
    if sys.platform != "win32":
        return "ffmpeg"
    cache = os.environ.get("MOMOI_MEDIA_CACHE")
    if not cache:
        cache = str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Momoi" / "Tools")
    return install_ffmpeg(cache)


def prepare_configured_media(config_path):
    """Warm the decoder before accepting calls; missing tools must not hide repair UI."""
    if sys.platform != "win32":
        return
    try:
        from momoi.config.manager import ConfigurationManager
        manager = ConfigurationManager(config_path)
        app = manager.read_app()
        voice = app.get("channels", {}).get("enabled", {}).get("napcat", {}).get("voice_call", {})
        catalog = manager.read_providers()
        binding = catalog.get("bindings", {}).get("tts", {})
        service = catalog.get("services", {}).get(binding.get("service"), {})
        if voice.get("enabled") and binding.get("enabled", True) and service.get("adapter") == "vocu":
            ffmpeg_executable()
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("Automatic FFmpeg preparation failed; telephone playback will retry")
