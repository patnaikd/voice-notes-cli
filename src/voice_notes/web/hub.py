"""WebSocket fan-out with bounded per-client queues; levels and partials are dropped first."""
import asyncio
import json
import logging
import time
from collections import deque
from contextlib import suppress

log = logging.getLogger("voice_notes.web")
DROPPABLE = ("levels", "partial")


class Client:
    def __init__(self, websocket, limit: int, stuck_seconds: float):
        self.websocket, self.limit, self.stuck_seconds = websocket, limit, stuck_seconds
        self.queue: deque[tuple[str, str]] = deque()
        self.ready = asyncio.Event()
        self.full_since: float | None = None
        self.closed = False
        self.dropped = 0
        self.sender_task: asyncio.Task | None = None

    def push(self, kind: str, text: str) -> bool:
        """Queue a message; returns False when the client should be disconnected."""
        if len(self.queue) >= self.limit:
            if kind in DROPPABLE:
                self.dropped += 1
                return self._check_stuck()
            victim = next((i for i, (k, _) in enumerate(self.queue) if k in DROPPABLE), None)
            if victim is None:
                self.dropped += 1
                return self._check_stuck()
            del self.queue[victim]
        else:
            self.full_since = None
        self.queue.append((kind, text))
        self.ready.set()
        return True

    def _check_stuck(self) -> bool:
        now = time.monotonic()
        self.full_since = self.full_since or now
        return now - self.full_since < self.stuck_seconds

    async def sender(self):
        while not self.closed:
            await self.ready.wait()
            self.ready.clear()
            while self.queue:
                _, text = self.queue.popleft()
                await self.websocket.send_text(text)


class Hub:
    def __init__(self, limit: int = 500, stuck_seconds: float = 10.0):
        self.limit, self.stuck_seconds = limit, stuck_seconds
        self.clients: set[Client] = set()
        self._closing: set[asyncio.Task] = set()  # keep references so close tasks are not garbage collected

    def broadcast(self, kind: str, **payload):
        if not self.clients:
            return
        text = json.dumps({"type": kind, **payload})
        for client in list(self.clients):
            if not client.push(kind, text):
                log.warning("Disconnecting a WebSocket client whose queue stayed full for %.0f s", self.stuck_seconds)
                self._drop(client)

    def _drop(self, client: Client):
        client.closed = True
        client.ready.set()
        self.clients.discard(client)
        # The sender is usually blocked writing to the stuck socket; cancelling it ends serve() for this client.
        if client.sender_task:
            client.sender_task.cancel()
        with suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(self._close(client.websocket))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close(websocket):
        with suppress(Exception):
            await asyncio.wait_for(websocket.close(code=1013), 5)

    async def serve(self, websocket, snapshot):
        """Run one connection: send the snapshot first, then queued messages; ignore client text."""
        await websocket.accept()
        client = Client(websocket, self.limit, self.stuck_seconds)
        client.push("snapshot", json.dumps({"type": "snapshot", **snapshot()}))
        self.clients.add(client)
        sender = client.sender_task = asyncio.create_task(client.sender())
        receiver = asyncio.create_task(self._receive(websocket))
        try:
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            client.closed = True
            client.ready.set()
            self.clients.discard(client)
            for task in (sender, receiver):
                task.cancel()
            for task in (sender, receiver):
                with suppress(asyncio.CancelledError, Exception):
                    await task

    @staticmethod
    async def _receive(websocket):
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass
