"""Bounded PCM utterances with pre-roll and sustained-speech detection."""
from collections import deque
import io
import math
import struct
import wave


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return output.getvalue()


class Segmenter:
    def __init__(self):
        self.reset()

    def reset(self):
        self.pre = deque(maxlen=10)
        self.frames = []
        self.voiced = self.silence = self.onsets = 0
        self.noise = -60.0

    def feed(self, frame):
        if len(frame) != 640:
            return False, None
        samples = struct.unpack('<320h', frame)
        rms = math.sqrt(sum(s * s for s in samples) / 320)
        db = 20 * math.log10(max(rms, 1) / 32768)
        voice = db > max(-48, min(-34, self.noise + 10))
        if not voice:
            self.noise = self.noise * .98 + db * .02
        started = False
        if not self.frames:
            self.pre.append(frame)
            self.onsets = self.onsets + 1 if voice else 0
            if self.onsets < 3:
                return False, None
            self.frames = list(self.pre)
            self.pre.clear()
            self.voiced = 3
            started = True
        else:
            self.frames.append(frame)
            self.voiced += int(voice)
        self.silence = 0 if voice else self.silence + 1
        if self.silence >= 50 or len(self.frames) >= 750:
            result = b''.join(self.frames) if self.voiced >= 6 else None
            self.reset()
            return started, result
        return started, None
