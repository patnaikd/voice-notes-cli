"""Memory, event-loop lag, disk, error counts, and the memory restart/exit policy."""
import asyncio
import logging
import shutil
import time
from collections import deque

import psutil

log = logging.getLogger("voice_notes.web")
MB = 1024 * 1024


class Health:
    def __init__(self, server):
        self.server, self.config = server, server.config
        self.process = psutil.Process()
        self.started = time.monotonic()
        self.lag_ms = 0.0
        self.errors: deque[float] = deque(maxlen=1000)
        self.last_error: str | None = None
        self.server_rss = self.process.memory_info().rss / MB
        self.helper_rss: float | None = None
        self.helper_over_since: float | None = None
        self.server_over_since: float | None = None

    def record_error(self, message: str):
        self.errors.append(time.monotonic())
        self.last_error = message

    async def measure_lag(self):
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.5)
            self.lag_ms = max(0.0, (time.monotonic() - before - 0.5) * 1000)

    async def monitor(self):
        while True:
            await asyncio.sleep(self.config.health_interval)
            self.sample()
            self.server.hub.broadcast("health", health=self.payload())

    def sample(self):
        now = time.monotonic()
        self.server_rss = self.process.memory_info().rss / MB
        recorder = self.server.recorder
        pid = recorder.supervisor.pid
        try:
            self.helper_rss = psutil.Process(pid).memory_info().rss / MB if pid else None
        except psutil.Error:
            self.helper_rss = None
        if self.helper_rss and self.helper_rss > self.config.helper_warn_mb:
            self.helper_over_since = self.helper_over_since or now
            if now - self.helper_over_since >= self.config.memory_window and not recorder.memory_restart:
                recorder.memory_restart = True
                self.server.activity("warning", f"Capture helper above {self.config.helper_warn_mb} MB for "
                                     f"{self.config.memory_window / 60:.0f} min; restarting it at the next quiet moment")
        else:
            self.helper_over_since = None
        if recorder.memory_restart and now - recorder.last_speech >= 10:
            recorder.memory_restart = False
            self.helper_over_since = None
            recorder.supervisor.restart("Restarting capture helper during a quiet moment to release memory")
        if self.server_rss > self.config.server_exit_mb:
            self.server_over_since = self.server_over_since or now
            if now - self.server_over_since >= self.config.memory_window:
                self.server.error(f"Server memory above {self.config.server_exit_mb} MB for "
                                  f"{self.config.memory_window / 60:.0f} min; exiting so launchd restarts it")
                self.server.request_exit(75)
        else:
            self.server_over_since = None

    def payload(self) -> dict:
        now, config, recorder = time.monotonic(), self.config, self.server.recorder
        warnings = []
        if self.server_rss > config.server_warn_mb:
            warnings.append(f"Server memory above {config.server_warn_mb} MB")
        if self.helper_rss and self.helper_rss > config.helper_warn_mb:
            warnings.append(f"Capture helper memory above {config.helper_warn_mb} MB")
        if recorder.state == "recording":
            silent = now - (recorder.supervisor.last_level or self.started)
            if silent >= 120:
                warnings.append(f"No audio level for {silent:.0f} s")
        if recorder.state == "error":
            warnings.append(recorder.message or "Capture helper is failing")
        try:
            free = shutil.disk_usage(self.server.store.root).free / 1e9
        except OSError:
            free = 0.0
        if free < 2:
            warnings.append(f"Low disk space: {free:.1f} GB free")
        queued, running = self.server.jobs.counts()
        hour_ago = now - 3600
        return {"uptime_s": round(now - self.started), "server_rss_mb": round(self.server_rss, 1),
                "helper_rss_mb": round(self.helper_rss, 1) if self.helper_rss else None,
                "helper_pid": recorder.supervisor.pid, "event_loop_lag_ms": round(self.lag_ms, 1),
                "ws_clients": len(self.server.hub.clients), "jobs_pending": queued, "jobs_running": running,
                "errors_last_hour": sum(t >= hour_ago for t in self.errors), "last_error": self.last_error,
                "disk_free_gb": round(free, 1), "warnings": warnings}
