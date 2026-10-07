"""Retry unavailable virtual devices without replacing them with physical audio."""
import json
from pathlib import Path
import sys


class DeferredWindowsAudio:
    def __init__(self, runtime, bridge):
        sys.path.insert(0, str(Path(bridge).resolve() / 'windows'))
        from audio_backend import WindowsAudioBackend
        self.factory = WindowsAudioBackend
        self.runtime = runtime
        self.backend = None
        self.config_path = Path(runtime).parent / "config.json"
        self.device_ids = self.read_device_ids()

    def read_device_ids(self):
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            options = value.get("channels", {}).get("enabled", {}).get("napcat", {}).get("voice_call", {})
            return {key: options.get(key, "") for key in ("input_device", "output_device")}
        except FileNotFoundError:
            return {}

    def configuration_changed(self):
        return self.read_device_ids() != self.device_ids

    def reload_configuration(self):
        self.close()
        self.device_ids = self.read_device_ids()

    @property
    def device_selection(self):
        return getattr(self.backend, "device_selection", {})

    @property
    def ready(self):
        return bool(self.backend and self.backend.ready)

    @property
    def capture_command(self):
        return self.backend.capture_command if self.backend else ()

    @property
    def playback_command(self):
        return self.backend.playback_command if self.backend else ()

    async def prepare(self, http, host_url, token):
        if self.ready:
            return
        backend = self.factory(self.runtime, device_ids=self.device_ids)
        entered = False
        try:
            backend.__enter__()
            entered = True
            await backend.prepare(http, host_url, token)
        except BaseException:
            if entered:
                backend.__exit__(*sys.exc_info())
            raise
        self.backend = backend

    def close(self):
        if self.backend:
            backend, self.backend = self.backend, None
            backend.__exit__(None, None, None)


def device_catalog(bridge):
    if sys.platform != "win32":
        return {"inputs": [], "outputs": [], "errors": ["设备选择仅适用于 Windows 桌面客户端"]}
    sys.path.insert(0, str(Path(bridge).resolve() / "windows"))
    from virtual_audio import audio_device_catalog
    return audio_device_catalog()
