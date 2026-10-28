"""Passive (equipment) and active (host) HSMS-SS endpoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .connection import (
    HsmsConnection,
    HsmsTimeouts,
    PrimaryHandler,
    State,
    StateListener,
    TraceHook,
)

log = logging.getLogger(__name__)


class HsmsPassiveServer:
    """Listens for the host. HSMS-SS allows one selected session; a second TCP
    connection is answered with Select.rsp status 3 (connection exhaust)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        session_id: int,
        timeouts: HsmsTimeouts,
        handler: PrimaryHandler,
        trace: TraceHook | None = None,
        name: str = "equipment",
    ) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.timeouts = timeouts
        self.handler = handler
        self.trace = trace
        self.name = name
        self.connection: HsmsConnection | None = None
        self._server: asyncio.Server | None = None
        self._listeners: list[StateListener] = []
        self._accepted: set[HsmsConnection] = set()

    def add_state_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        log.info("%s: HSMS passive listening on %s:%d", self.name, self.host, self.port)

    async def stop(self) -> None:
        # Close every accepted connection, not only the session: since Python
        # 3.12 Server.wait_closed() also waits for all accepted connections.
        for conn in list(self._accepted):
            await conn.close("server stop")
        self._accepted.clear()
        if self._server:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5.0)
            except TimeoutError:
                log.warning("%s: listener did not close within 5 s", self.name)

    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        busy = self.connection is not None and not self.connection.closed
        conn = HsmsConnection(
            reader,
            writer,
            passive=True,
            session_id=self.session_id,
            timeouts=self.timeouts,
            handler=self.handler,
            trace=self.trace,
            name=f"{self.name}<-{peer[0]}:{peer[1]}" if peer else self.name,
            system_start=0x00010001,
            exhausted=busy,
        )
        if not busy:
            self.connection = conn
            for listener in self._listeners:
                conn.add_state_listener(listener)
        self._accepted = {c for c in self._accepted if not c.closed} | {conn}
        log.info("%s: TCP connection from %s%s", self.name, peer, " (exhausted)" if busy else "")
        conn.start()


async def connect_active(
    host: str,
    port: int,
    *,
    session_id: int,
    timeouts: HsmsTimeouts,
    handler: PrimaryHandler,
    trace: TraceHook | None = None,
    name: str = "host",
    attempts: int = 3,
    on_state: Callable[[HsmsConnection, State], None] | None = None,
) -> HsmsConnection:
    """Open TCP, retrying every T5, then send Select.req (T6)."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            break
        except OSError as exc:
            last_error = exc
            log.warning(
                "%s: connect attempt %d failed (%s); retry in T5=%.1fs", name, attempt, exc, timeouts.t5
            )
            if attempt < attempts:
                await asyncio.sleep(timeouts.t5)
    else:
        raise ConnectionError(f"could not connect to {host}:{port}: {last_error}")
    conn = HsmsConnection(
        reader,
        writer,
        passive=False,
        session_id=session_id,
        timeouts=timeouts,
        handler=handler,
        trace=trace,
        name=name,
    )
    if on_state:
        conn.add_state_listener(on_state)
    conn.start()
    await conn.select()
    return conn
