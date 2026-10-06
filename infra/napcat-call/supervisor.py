"""Monitor the QQ process group and reap all media children on shutdown."""
import os
import signal
import subprocess
import time
import urllib.request

process = subprocess.Popen(['bash', '/opt/qq-call/entrypoint-upstream.sh'], start_new_session=True)
stopping = False

def terminate(*_):
    global stopping
    stopping = True
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)

signal.signal(signal.SIGTERM, terminate)
signal.signal(signal.SIGINT, terminate)
started = time.monotonic()
failures = 0
try:
    while process.poll() is None and not stopping:
        time.sleep(1)
        if os.getenv('QQ_CALL_ENABLED', '0') != '1' or time.monotonic() - started < 60:
            continue
        try:
            with urllib.request.urlopen('http://127.0.0.1:' + os.getenv('QQ_CALL_PORT', '6112') + '/healthz', timeout=3) as response:
                assert response.status == 200
            failures = 0
        except Exception:
            failures += 1
            if failures >= 15:
                terminate()
    try:
        code = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        code = process.wait()
finally:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
raise SystemExit(code if not stopping else 1)
