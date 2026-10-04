"""Restorable shutdown handlers, including Windows and embedded event loops."""

import asyncio
import signal
import threading
from contextlib import contextmanager


@contextmanager
def shutdown_signals(stop: asyncio.Event):
    loop = asyncio.get_running_loop()
    handlers = []
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            previous = signal.getsignal(sig)
            try:
                loop.add_signal_handler(sig, stop.set)
                handlers.append((sig, previous, True))
            except NotImplementedError:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
                handlers.append((sig, previous, False))
    try:
        yield
    finally:
        for sig, previous, asynchronous in handlers:
            if asynchronous:
                loop.remove_signal_handler(sig)
            signal.signal(sig, previous)
