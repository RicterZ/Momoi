"""Half-duplex capture gate for a capture/injection pair sharing one virtual cable."""
import logging
import re
import time

from ....observability.events import log_event

logger = logging.getLogger('momoi.qq_call')
_DIRECTION = re.compile(r'^(?P<base>.*?)[\s_-]*\b(?P<direction>input|output)\b(?P<rest>.*)$', re.IGNORECASE)


def _endpoint(device):
    if isinstance(device, dict):
        return str(device.get('id') or ''), str(device.get('name') or device.get('label') or '')
    return '', str(device or '')


def _cable_key(name):
    match = _DIRECTION.match(name.strip())
    if not match:
        return None
    base = (match['base'] + '|' + match['rest']).casefold().strip()
    return (match['direction'].casefold(), re.sub(r'\s+', ' ', base))


def detect_half_duplex(devices, *, mode='auto', bridge_flag=False):
    """Return (enabled, reason). Device identity first, names only as a fallback."""
    if mode == 'on':
        return True, 'forced_on'
    if mode == 'off':
        return False, 'forced_off'
    # Momoi captures QQ's speaker (output_device, a render endpoint) and injects TTS into
    # injection_device, which feeds QQ's microphone (input_device). With one cable QQ's
    # speaker and the injection target are the same render endpoint, so TTS loops back.
    devices = devices if isinstance(devices, dict) else {}
    speaker_id, speaker_name = _endpoint(devices.get('output_device'))
    inject_id, inject_name = _endpoint(devices.get('injection_device'))
    microphone_name = _endpoint(devices.get('input_device'))[1]
    if speaker_id and inject_id:
        if speaker_id.casefold() == inject_id.casefold():
            return True, 'shared_virtual_cable'
        if not bridge_flag:
            return False, 'separate_devices'
    if bridge_flag:
        return True, 'shared_virtual_cable'
    if speaker_name and inject_name and speaker_name.casefold() == inject_name.casefold():
        return True, 'shared_virtual_cable_name'
    speaker, microphone = _cable_key(speaker_name), _cable_key(microphone_name)
    if not inject_name and speaker and microphone and speaker[0] == 'input' and microphone[0] == 'output' \
            and speaker[1] == microphone[1]:
        return True, 'shared_virtual_cable_name'
    return False, 'separate_devices'


class CaptureGate:
    """Closed from turn/send start until the last sentence's audible end plus a tail."""

    def __init__(self, tail_ms=400, clock=time.monotonic):
        self.tail_ms = tail_ms
        self.clock = clock
        self.reset()

    def reset(self):
        self.closed = False
        self.turn_id = None
        self.turn_active = False
        self.sends = 0
        self.play_end = 0.0
        self.open_at = None
        self.dropped = 0
        self.dropped_peak = 0

    def _close(self, reason, turn_id):
        if not self.closed:
            self.closed = True
            self.play_end = 0.0
            self.dropped = self.dropped_peak = 0
            log_event(logger, logging.INFO, 'qq_call_capture_gate_closed', reason=reason, turn_id=turn_id)
        self.open_at = None

    def _settle(self):
        if self.closed and not self.turn_active and self.sends == 0:
            self.open_at = self.play_end + self.tail_ms / 1000

    def turn_started(self, turn_id):
        self.turn_id = turn_id
        self.turn_active = True
        self._close('turn_started', turn_id)

    def turn_finished(self, turn_id, *, cancelled=False):
        if not self.turn_active or turn_id != self.turn_id:
            return
        self.turn_active = False
        if cancelled or self.play_end == 0.0:
            self.play_end = max(self.play_end, self.clock())
        self._settle()

    def send_started(self, utterance_id=None):
        self.sends += 1
        self._close('send_started', self.turn_id or utterance_id)

    def send_finished(self, *, audible_end=None):
        """``audible_end`` is first-frame time plus PCM duration; None means aborted or failed."""
        if self.sends == 0:
            return
        self.sends -= 1
        self.play_end = max(self.play_end, self.clock() if audible_end is None else audible_end)
        self._settle()

    def drop(self, peak=0):
        self.dropped += 1
        self.dropped_peak = max(self.dropped_peak, peak)

    def poll(self):
        """True exactly once, when the gate reopens; callers then reset VAD/ASR state."""
        if not self.closed:
            return False
        if self.open_at is None or self.clock() < self.open_at:
            return False
        log_event(logger, logging.INFO, 'qq_call_capture_gate_opened', tail_ms=self.tail_ms,
                  dropped_frames=self.dropped, dropped_peak=self.dropped_peak, turn_id=self.turn_id)
        self.reset()
        return True
