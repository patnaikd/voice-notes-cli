"""Live terminal audio meters; microphone and remote tracks stay independent."""
import math
import os
import shutil
import sys
import threading
import time


def level_db(rms: float) -> float:
    if not math.isfinite(rms) or rms <= 0:
        return -60.0
    return min(0.0, max(-60.0, 20 * math.log10(rms)))


class AudioMeter:
    def __init__(self, sources, *, remote_label="Speaker", enabled=True, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled and self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.sources = sources
        self.remote_label = remote_label
        self.levels = {}
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None

    def update(self, source, rms, peak):
        if self.enabled and source in self.sources:
            with self.lock:
                self.levels[source] = (rms, peak, time.monotonic())

    def render(self, now=None):
        now = time.monotonic() if now is None else now
        parts = []
        for source in self.sources:
            name = "Mic" if source == "mic" else self.remote_label
            rms, peak, received = self.levels.get(source, (0, 0, None))
            if received is None or now - received > 1.5:
                db, status = -60, "no audio"
            else:
                db = level_db(rms)
                status = "CLIP" if peak >= 0.99 else ("silent" if db < -55 else f"{db:4.0f} dB")
            filled = round((db + 60) / 60 * 12)
            parts.append(f"{name} [{'#' * filled}{'.' * (12 - filled)}] {status}")
        return " | ".join(parts)

    def _draw(self):
        width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        self.stream.write("\r\x1b[2K" + self.render()[:width])
        self.stream.flush()

    def start(self):
        if not self.enabled or self.thread is not None:
            return

        def refresh():
            while not self.stopped.wait(0.1):
                with self.lock:
                    self._draw()

        self.thread = threading.Thread(target=refresh, daemon=True)
        self.thread.start()

    def print(self, message, *, stream=None):
        with self.lock:
            active = self.thread is not None and not self.stopped.is_set()
            if active:
                self.stream.write("\r\x1b[2K")
                self.stream.flush()
            print(message, file=stream if stream is not None else sys.stderr, flush=True)
            if active:
                self._draw()

    def close(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
            with self.lock:
                self.stream.write("\r\x1b[2K")
                self.stream.flush()
