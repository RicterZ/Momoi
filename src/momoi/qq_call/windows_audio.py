"""Retry unavailable virtual devices without replacing them with physical audio."""
import asyncio
import json
import logging
import subprocess
from pathlib import Path
import sys


class DeferredWindowsAudio:
    def __init__(self, runtime, bridge):
        sys.path.insert(0, str(Path(bridge).resolve() / 'windows'))
        from audio_backend import WindowsAudioBackend
        self.factory = WindowsAudioBackend
        self.runtime = runtime
        self.bridge = Path(bridge)
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
    def warnings(self):
        return getattr(self.backend, "warnings", [])

    @property
    def half_duplex(self):
        return getattr(self.backend, "half_duplex", False)

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

    async def check_endpoints(self):
        if not self.ready:
            return True
        try:
            active = await _read_audio_probe(self.bridge,
                "{str(flow): virtual_audio.active_endpoint_ids(flow) for flow in (0,1)}")
            active = {int(flow): {value.casefold() for value in values}
                      for flow, values in active.items()}
            return all(device["id"].casefold() in active[flow]
                       for role, flow in (("input_device", 1), ("output_device", 0),
                                          ("injection_device", 0))
                       if (device := self.device_selection.get(role)))
        except (OSError, RuntimeError, ValueError):
            return False

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


async def read_device_catalog(bridge):
    """Isolate Windows audio enumeration so a stuck driver cannot wedge the API."""
    if sys.platform != "win32":
        return await asyncio.to_thread(device_catalog, bridge)
    return await _read_audio_probe(bridge, "virtual_audio.audio_device_catalog()")


async def _read_audio_probe(bridge, expression):
    script = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); import virtual_audio; "
        "print(json.dumps(" + expression + "))"
    )
    def run():
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-X", "utf8", "-c", script,
                 str(Path(bridge).resolve() / "windows")],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=3, check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("音频设备响应超时，请稍后刷新") from None
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-8192:]
            logging.getLogger(__name__).warning("event=qq_call_device_probe_failed exit_code=%s detail=%s",
                                              result.returncode, detail)
            reason = detail.splitlines()[-1][:300] if detail else f"探测进程退出码 {result.returncode}"
            raise RuntimeError("暂时无法读取音频设备：" + reason)
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("音频设备探测返回了无效数据，请稍后刷新") from None
    return await asyncio.to_thread(run)
