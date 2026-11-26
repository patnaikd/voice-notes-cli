"""Capture helper lifecycle: spawn, startup deadline, stall watchdog, restart with backoff."""
import asyncio
import logging
import os
import signal
import time
from collections import deque
from contextlib import suppress

from ..capture import parse_event

log = logging.getLogger("voice_notes.web")
BACKOFF = (1, 2, 5, 10, 30, 60)


class Supervisor:
    """Keeps one helper running while desired. Callbacks run on the event loop:
    on_event(event, run, origin) for every parsed event after spawn, and
    on_state(state, message) for "starting", "recording", "restarting", "error"."""

    def __init__(self, on_event, on_state, *, startup_timeout=120.0, stall_seconds=60.0, kill_timeout=10.0,
                 backoff=BACKOFF, healthy_reset=600.0, failure_window=900.0, max_failures=10, error_retry=300.0):
        self.on_event, self.on_state = on_event, on_state
        self.startup_timeout, self.stall_seconds, self.kill_timeout = startup_timeout, stall_seconds, kill_timeout
        self.backoff, self.healthy_reset = backoff, healthy_reset
        self.failure_window, self.max_failures, self.error_retry = failure_window, max_failures, error_retry
        self.command: list[str] = []
        self.process: asyncio.subprocess.Process | None = None
        self.run = 0
        self.restarts = 0
        self.ready = False
        self.origin: float | None = None
        self.last_level = 0.0
        self.failures: deque[float] = deque(maxlen=max_failures)
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self._desired = False
        self._task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None  # referenced so it is not garbage collected
        self._wake = asyncio.Event()
        self._reason: str | None = None  # why the current helper is being ended
        self._planned = False  # planned restarts (memory) are not failures

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.returncode is None else None

    def start(self, command: list[str]):
        self.command, self._desired = command, True
        self.failures.clear()
        if not self.active:
            self._task = asyncio.create_task(self._loop(), name="helper-supervisor")

    async def stop(self, grace: float = 20.0):
        """Ask the helper to finalize (SIGTERM), kill it after `grace`, and stop restarting."""
        self._desired = False
        self._wake.set()
        await self._terminate(grace)
        if self._task:
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def restart(self, reason: str):
        """Gracefully end the current helper; the loop starts a new one."""
        if self.process and self.process.returncode is None and not self._reason:
            self._reason, self._planned = reason, True
            self._restart_task = asyncio.create_task(self._terminate(self.kill_timeout))

    async def _terminate(self, grace: float):
        process = self.process
        if not process or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), grace)
        except TimeoutError:
            log.warning("Capture helper ignored SIGTERM for %.0f s; killing it", grace)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()

    async def _sleep(self, seconds: float):
        self._wake.clear()
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    async def _loop(self):
        attempt = 0
        while self._desired:
            started = time.monotonic()
            message = await self._run_once()
            if not self._desired:
                break
            now = time.monotonic()
            if self.ready and now - started >= self.healthy_reset:
                attempt = 0
            planned = self._planned
            if not planned:
                self.failures.append(now)
            self.restarts += 1
            recent = [t for t in self.failures if now - t <= self.failure_window]
            if len(recent) >= self.max_failures:
                text = (f"Capture helper failed {len(recent)} times in {self.failure_window / 60:.0f} minutes: "
                        f"{message}. Retrying every {self.error_retry / 60:.0f} minutes.")
                self.on_state("error", text)
                await self._sleep(self.error_retry)
                continue
            delay = 0 if planned else self.backoff[min(attempt, len(self.backoff) - 1)]
            attempt += 0 if planned else 1
            self.on_state("restarting", f"{message}; restarting in {delay:g} s")
            if delay:
                await self._sleep(delay)

    async def _run_once(self) -> str:
        """Run one helper process to completion; returns why it ended."""
        self.run += 1
        self.ready, self.origin, self._reason, self._planned = False, None, None, False
        self.on_state("starting", "Starting capture helper…")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16 * 1024 * 1024)
        except OSError as error:
            return f"Cannot start capture helper: {error}"
        process, run = self.process, self.run
        stderr = asyncio.create_task(self._drain_stderr(process))
        watchdog = asyncio.create_task(self._watchdog(process))
        problem = None
        try:
            while line := await process.stdout.readline():
                try:
                    event = parse_event(line.decode(errors="replace"))
                except ValueError as error:
                    self.on_event({"type": "warning", "message": f"Ignored invalid capture event: {error}"}, run, None)
                    continue
                kind = event["type"]
                if kind == "ready":
                    self.ready, self.origin, self.last_level = True, time.time(), time.monotonic()
                    # The helper's ready text is written for the CLI; drop its Ctrl-C hint.
                    message = (event.get("message") or "Recording").replace(" Press Ctrl-C to stop.", "")
                    self.on_state("recording", message)
                elif kind == "level":
                    self.last_level = time.monotonic()
                elif kind == "error":
                    problem = event.get("message")
                self.on_event(event, run, self.origin)
        except ValueError as error:
            problem = f"Capture output line too long: {error}"
            await self._terminate(self.kill_timeout)
        finally:
            code = await process.wait()
            watchdog.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(stderr, 2)
        if self._reason:
            return self._reason
        detail = problem or (self.stderr_tail[-1] if self.stderr_tail else None)
        return f"Capture helper exited with status {code}" + (f" ({detail})" if detail else "")

    async def _drain_stderr(self, process):
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            if text:
                self.stderr_tail.append(text[:500])
                log.info("helper: %s", text[:500])

    async def _watchdog(self, process):
        deadline = time.monotonic() + self.startup_timeout
        while process.returncode is None:
            await asyncio.sleep(min(1.0, self.stall_seconds / 4))
            now = time.monotonic()
            if not self.ready and now > deadline:
                self._reason = (f"Capture did not become ready within {self.startup_timeout:g} s; "
                                "check macOS permissions and model downloads")
            elif self.ready and now - self.last_level > self.stall_seconds:
                self._reason = f"No audio level from capture helper for {self.stall_seconds:g} s (hung)"
            else:
                continue
            log.warning(self._reason)
            await self._terminate(self.kill_timeout)
            return
