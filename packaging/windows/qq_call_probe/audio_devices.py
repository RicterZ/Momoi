"""Enumerate WinMM devices without recording or playing audio."""
import ctypes
import json
from pathlib import Path
import sys

class Caps(ctypes.Structure):
    _fields_ = [('manufacturer', ctypes.c_ushort), ('product', ctypes.c_ushort),
               ('version', ctypes.c_uint), ('name', ctypes.c_wchar * 32),
               ('formats', ctypes.c_uint), ('channels', ctypes.c_ushort),
               ('reserved', ctypes.c_ushort), ('support', ctypes.c_uint)]

winmm = ctypes.WinDLL('winmm')
report = {}
for kind in ('In', 'Out'):
    count = getattr(winmm, f'wave{kind}GetNumDevs')()
    devices = []
    get_caps = getattr(winmm, f'wave{kind}GetDevCapsW')
    get_caps.argtypes = [ctypes.c_size_t, ctypes.POINTER(Caps), ctypes.c_uint]
    for index in range(count):
        caps = Caps()
        error = get_caps(index, ctypes.byref(caps), ctypes.sizeof(caps))
        devices.append({'index': index, 'error': error, 'name': caps.name, 'channels': caps.channels})
    report[kind.lower()] = devices
Path(sys.argv[1]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
