"""Inspect QQ native library in a disposable child process."""
import ctypes
import json
import os
from pathlib import Path
import sys

app, files, output = map(Path, sys.argv[1:])
result = {'dll_load': False, 'exports': {}}
handles = [os.add_dll_directory(str(p)) for p in (app, app / 'avsdk', app.parent.parent, files)]
try:
    library = ctypes.WinDLL(str(app / 'avsdk/AVSDKPlugin.dll'))
    result['dll_load'] = True
    for symbol in ('PPP_GetInterface', 'PPP_InitializeModule', 'PPP_ShutdownModule'):
        result['exports'][symbol] = bool(getattr(library, symbol, None))
except OSError as error:
    result['dll_error'] = str(error)
winmm = ctypes.WinDLL('winmm')
result['wave_input_count'] = winmm.waveInGetNumDevs()
result['wave_output_count'] = winmm.waveOutGetNumDevs()
output.write_text(json.dumps(result, indent=2), encoding='utf-8')
os._exit(0)
