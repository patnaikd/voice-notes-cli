"""Cooperative UI control without installing signal handlers in worker threads."""
import threading


class SessionControl:
    def __init__(self):
        self.stop_requested = threading.Event()
        self.skip_formatting = threading.Event()
        self._lock = threading.Lock()
        self._stop = None
        self._cancel = None

    def set_stop(self, callback):
        with self._lock:
            self._stop = callback
            requested = self.stop_requested.is_set()
        if callback and requested:
            callback()

    def request_stop(self):
        with self._lock:
            self.stop_requested.set()
            callback = self._stop
        if callback:
            callback()

    def set_cancel(self, callback):
        with self._lock:
            self._cancel = callback
            requested = self.skip_formatting.is_set()
        if callback and requested:
            callback()

    def cancel_formatting(self):
        with self._lock:
            was_requested = self.skip_formatting.is_set()
            self.skip_formatting.set()
            callback = None if was_requested else self._cancel
        if callback:
            callback()

    def request_quit(self):
        self.cancel_formatting()
        self.request_stop()
