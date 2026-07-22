import asyncio
import base64
import binascii
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"


def aes_key(value: str, *, hex_value: bool = False) -> bytes:
    try:
        decoded = (
            bytes.fromhex(value)
            if hex_value
            else base64.b64decode(value, validate=True)
        )
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid Weixin media AES key") from error
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        decoded = bytes.fromhex(decoded.decode("ascii"))
    if len(decoded) != 16:
        raise ValueError("Weixin media AES key must be 16 bytes")
    return decoded


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return cipher.update(padded) + cipher.finalize()


def decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = cipher.update(ciphertext) + cipher.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def padded_size(size: int) -> int:
    return ((size // 16) + 1) * 16


def safe_filename(value: str | None, fallback: str) -> str:
    name = (value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:120] or fallback


def media_type(path: str | Path, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(str(path))[0] or fallback


async def read_source(
    session: aiohttp.ClientSession, source: str, max_bytes: int
) -> tuple[bytes, str, str]:
    if source.startswith("base64://"):
        try:
            content = base64.b64decode(source.removeprefix("base64://"), validate=True)
        except binascii.Error as error:
            raise ValueError("invalid base64 media source") from error
        if len(content) > max_bytes:
            raise ValueError("media exceeds channel.settings.media_max_bytes")
        return content, "attachment.bin", "application/octet-stream"

    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        async with session.get(
            source, timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            if response.status >= 400:
                raise ValueError(f"remote media returned HTTP {response.status}")
            declared = response.content_length
            if declared is not None and declared > max_bytes:
                raise ValueError("media exceeds channel.settings.media_max_bytes")
            content = await _bounded_response(response, max_bytes)
            name = safe_filename(Path(parsed.path).name, "attachment.bin")
            mime = response.headers.get("Content-Type", "").split(";", 1)[0]
            return content, name, mime or media_type(name)
    if parsed.scheme:
        raise ValueError("media source must be a local path, HTTP(S), or base64://")

    path = Path(source).expanduser()
    try:
        size = path.stat().st_size
        if not path.is_file():
            raise ValueError("media source is not a file")
        if size > max_bytes:
            raise ValueError("media exceeds channel.settings.media_max_bytes")
        content = await asyncio.to_thread(path.read_bytes)
    except OSError as error:
        raise ValueError(
            f"media source cannot be read: {type(error).__name__}"
        ) from error
    return content, safe_filename(path.name, "attachment.bin"), media_type(path)


async def download_item(
    session: aiohttp.ClientSession,
    item: dict[str, Any],
    directory: Path,
    stem: str,
    max_bytes: int,
) -> tuple[Path, str, str]:
    kind = int(item.get("type") or 0)
    field, fallback, mime = {
        2: ("image_item", "image.jpg", "image/jpeg"),
        3: ("voice_item", "voice.silk", "audio/silk"),
        4: ("file_item", "file.bin", "application/octet-stream"),
        5: ("video_item", "video.mp4", "video/mp4"),
    }.get(kind, ("", "file.bin", "application/octet-stream"))
    value = item.get(field)
    if not field or not isinstance(value, dict):
        raise ValueError("unsupported Weixin media item")
    media = value.get("media")
    if not isinstance(media, dict):
        raise ValueError("Weixin media reference is missing")
    full_url = str(media.get("full_url") or "").strip()
    parameter = str(media.get("encrypt_query_param") or "").strip()
    url = full_url or (
        f"{CDN_BASE_URL}/download?encrypted_query_param={quote(parameter, safe='')}"
        if parameter
        else ""
    )
    if not url or urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("Weixin media download URL is missing")

    original_name = value.get("file_name") if kind == 4 else None
    name = safe_filename(str(original_name) if original_name else None, fallback)
    suffix = Path(name).suffix or Path(fallback).suffix
    destination = directory / f"{safe_filename(stem, 'media')}{suffix.lower()}"
    if destination.is_file() and destination.stat().st_size <= max_bytes:
        return destination, media_type(destination, mime), name

    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as response:
        if response.status >= 400:
            raise ValueError(f"Weixin CDN returned HTTP {response.status}")
        ciphertext = await _bounded_response(response, max_bytes + 16)

    raw_key = value.get("aeskey") if kind == 2 else None
    encoded_key = media.get("aes_key")
    if raw_key:
        content = decrypt(ciphertext, aes_key(str(raw_key), hex_value=True))
    elif encoded_key:
        content = decrypt(ciphertext, aes_key(str(encoded_key)))
    elif kind == 2:
        content = ciphertext
    else:
        raise ValueError("Weixin media AES key is missing")
    if len(content) > max_bytes:
        raise ValueError("media exceeds channel.settings.media_max_bytes")

    if kind == 2 and not original_name:
        suffix = _image_suffix(content)
        destination = destination.with_suffix(suffix)
        mime = media_type(destination, mime)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    await asyncio.to_thread(temporary.write_bytes, content)
    temporary.replace(destination)
    return destination, media_type(destination, mime), name


async def _bounded_response(response: aiohttp.ClientResponse, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.content.iter_chunked(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise ValueError("media exceeds channel.settings.media_max_bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _image_suffix(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"
