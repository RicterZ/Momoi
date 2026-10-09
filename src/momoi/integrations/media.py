"""Decoder discovery; the application host may supply its managed tool resolver."""
import shutil


def ffmpeg_executable():
    return shutil.which("ffmpeg") or "ffmpeg"
