import hashlib
import io
import zipfile
from unittest.mock import patch

import pytest

from momoi.desktop import media_runtime


def component(monkeypatch):
    binary = b'MZverified ffmpeg test fixture'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(media_runtime.BINARY_NAME, binary)
        bundle.writestr('imageio_ffmpeg-0.6.0.dist-info/LICENSE', b'license')
    content = archive.getvalue()
    for name, value in [('BINARY_SIZE', len(binary)), ('BINARY_SHA256', hashlib.sha256(binary).hexdigest()),
                        ('WHEEL_SIZE', len(content)), ('WHEEL_SHA256', hashlib.sha256(content).hexdigest())]:
        monkeypatch.setattr(media_runtime, name, value)
    return binary, content


def test_windows_component_installs_verifies_and_reuses_cache(tmp_path, monkeypatch):
    binary, archive = component(monkeypatch)
    with patch.object(media_runtime.urllib.request, 'urlopen', side_effect=lambda *args, **kwargs: io.BytesIO(archive)) as download:
        path = media_runtime.install_ffmpeg(tmp_path)
        assert media_runtime.Path(path).read_bytes() == binary
        assert media_runtime.install_ffmpeg(tmp_path) == path
        assert download.call_count == 1
        media_runtime.Path(path).write_bytes(b'X' * len(binary))
        assert media_runtime.install_ffmpeg(tmp_path) == path
        assert download.call_count == 2
        assert media_runtime.Path(path).read_bytes() == binary


def test_corrupted_component_is_never_installed(tmp_path, monkeypatch):
    _, archive = component(monkeypatch)
    with patch.object(media_runtime.urllib.request, 'urlopen', return_value=io.BytesIO(b'X' * len(archive))):
        with pytest.raises(OSError, match='checksum mismatch'):
            media_runtime.install_ffmpeg(tmp_path)
    assert not list(tmp_path.rglob('ffmpeg.exe'))


def test_windows_missing_ffmpeg_uses_managed_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(media_runtime.sys, 'platform', 'win32')
    monkeypatch.setenv('MOMOI_MEDIA_CACHE', str(tmp_path))
    with patch.object(media_runtime.shutil, 'which', return_value=None), patch.object(media_runtime, 'install_ffmpeg', return_value='private/ffmpeg.exe') as install:
        assert media_runtime.ffmpeg_executable() == 'private/ffmpeg.exe'
        install.assert_called_once_with(str(tmp_path))
