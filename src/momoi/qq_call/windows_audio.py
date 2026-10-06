"""Retry unavailable virtual devices without replacing them with physical audio."""
from pathlib import Path
import sys


class DeferredWindowsAudio:
    def __init__(self, runtime, bridge):
        sys.path.insert(0, str(Path(bridge).resolve() / 'windows'))
        from audio_backend import WindowsAudioBackend
        self.factory = WindowsAudioBackend
        self.runtime = runtime
        self.backend = None

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
        backend = self.factory(self.runtime)
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
