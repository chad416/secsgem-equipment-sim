"""Scriptable GEM host (the factory side / MES-cell controller role)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..hsms import HsmsConnection, HsmsError, HsmsTimeouts, connect_active
from ..hsms.connection import TraceHook
from ..secs import (
    U4,
    A,
    B,
    Boolean,
    Item,
    L,
    SecsMessage,
    as_int,
    as_text,
    ensure_list,
    u4_list,
)

log = logging.getLogger(__name__)


class TransactionAborted(HsmsError):
    """The equipment answered SxF0."""


@dataclass
class HostEvent:
    dataid: int
    ceid: int
    reports: dict[int, list[Any]]
    received: float = field(default_factory=time.monotonic)

    def values(self) -> list[Any]:
        """All report values flattened in report order (handy in tests)."""
        return [v for rpt in self.reports.values() for v in rpt]


@dataclass
class HostAlarm:
    alcd: int
    alid: int
    text: str
    received: float = field(default_factory=time.monotonic)

    @property
    def is_set(self) -> bool:
        return bool(self.alcd & 0x80)


class GemHost:
    def __init__(
        self,
        address: str = "127.0.0.1",
        port: int = 5000,
        *,
        session_id: int = 1,
        timeouts: HsmsTimeouts | None = None,
        trace: TraceHook | None = None,
    ) -> None:
        self.address = address
        self.port = port
        self.session_id = session_id
        self.timeouts = timeouts or HsmsTimeouts()
        self.trace = trace
        self.conn: HsmsConnection | None = None
        self.events: list[HostEvent] = []
        self.alarms: list[HostAlarm] = []
        self.stream9: list[SecsMessage] = []
        self.communicating = asyncio.Event()
        self._changed = asyncio.Condition()
        self.event_hooks: list[Callable[[HostEvent], None]] = []

    # ------------------------------------------------------------ connection
    async def connect(self, attempts: int = 3) -> None:
        self.conn = await connect_active(
            self.address,
            self.port,
            session_id=self.session_id,
            timeouts=self.timeouts,
            handler=self._on_primary,
            trace=self.trace,
            name="host",
            attempts=attempts,
        )

    async def close(self) -> None:
        if self.conn and not self.conn.closed:
            with contextlib.suppress(HsmsError):
                await self.conn.deselect()
            await self.conn.separate()

    async def request(self, stream: int, function: int, body: Item | None = None) -> SecsMessage:
        if self.conn is None:
            raise HsmsError("not connected")
        reply = await self.conn.request(SecsMessage(stream, function, True, body))
        if reply.function == 0:
            raise TransactionAborted(f"S{stream}F{function} aborted by equipment (S{stream}F0)")
        return reply

    # -------------------------------------------------------- unsolicited
    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def _on_primary(self, msg: SecsMessage) -> SecsMessage | None:
        sf = (msg.stream, msg.function)
        if sf == (6, 11):
            body = ensure_list(msg.body, min_len=3)
            reports: dict[int, list[Any]] = {}
            for rpt in ensure_list(body[2]):
                r = ensure_list(rpt, min_len=2)
                reports[as_int(r[0])] = [v.to_python() for v in ensure_list(r[1])]
            event = HostEvent(as_int(body[0]), as_int(body[1]), reports)
            self.events.append(event)
            for hook in list(self.event_hooks):
                hook(event)
            await self._notify()
            return msg.reply(B(b"\x00"))  # ACKC6
        if sf == (5, 1):
            body = ensure_list(msg.body, min_len=3)
            alcd = body[0]
            code = alcd.first if isinstance(alcd, B) else as_int(alcd)
            text = body[2].to_python()
            self.alarms.append(HostAlarm(code, as_int(body[1]), str(text)))
            await self._notify()
            return msg.reply(B(b"\x00"))  # ACKC5
        if sf == (1, 13):
            self.communicating.set()
            return msg.reply(L(B(b"\x00"), L()))
        if sf == (1, 1):
            return msg.reply(L())  # host answers S1F2 with an empty list
        if msg.stream == 9:
            log.warning("equipment reported S9F%d", msg.function)
            self.stream9.append(msg)
            await self._notify()
            return None
        return msg.abort()

    def cursor(self) -> int:
        """Position in the event list; pass to ``wait_event(since=...)``."""
        return len(self.events)

    async def wait_event(
        self,
        ceid: int,
        *,
        since: int = 0,
        timeout: float = 30.0,
        where: Callable[[HostEvent], bool] | None = None,
    ) -> HostEvent:
        def find() -> HostEvent | None:
            for e in self.events[since:]:
                if e.ceid == ceid and (where is None or where(e)):
                    return e
            return None

        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(find), timeout)
        found = find()
        assert found is not None
        return found

    async def wait_alarm(self, alid: int, *, set_: bool, since: int = 0, timeout: float = 30.0) -> HostAlarm:
        def find() -> HostAlarm | None:
            for a in self.alarms[since:]:
                if a.alid == alid and a.is_set == set_:
                    return a
            return None

        async with self._changed:
            await asyncio.wait_for(self._changed.wait_for(find), timeout)
        found = find()
        assert found is not None
        return found

    # ------------------------------------------------------- GEM operations
    async def establish_communication(self) -> int:
        reply = await self.request(1, 13, L())
        self.communicating.set()
        commack = ensure_list(reply.body, min_len=1)[0]
        return commack.first if isinstance(commack, B) else as_int(commack)

    async def are_you_there(self) -> list[Any]:
        reply = await self.request(1, 1)
        return ensure_list(reply.body).to_python()

    async def request_online(self) -> int:
        return _ack(await self.request(1, 17))

    async def request_offline(self) -> int:
        return _ack(await self.request(1, 15))

    async def status_variables(self, svids: Sequence[int]) -> list[Any]:
        reply = await self.request(1, 3, u4_list(svids))
        return ensure_list(reply.body).to_python()

    async def equipment_constants(self, ecids: Sequence[int]) -> list[Any]:
        reply = await self.request(2, 13, u4_list(ecids))
        return ensure_list(reply.body).to_python()

    async def set_equipment_constants(self, values: Mapping[int, Item]) -> int:
        body = L(*(L(U4(ecid), v) for ecid, v in values.items()))
        return _ack(await self.request(2, 15, body))

    async def define_reports(self, reports: Mapping[int, Sequence[int]], dataid: int = 1) -> int:
        body = L(U4(dataid), L(*(L(U4(r), u4_list(v)) for r, v in reports.items())))
        return _ack(await self.request(2, 33, body))

    async def link_events(self, links: Mapping[int, Sequence[int]], dataid: int = 1) -> int:
        body = L(U4(dataid), L(*(L(U4(c), u4_list(r)) for c, r in links.items())))
        return _ack(await self.request(2, 35, body))

    async def enable_events(self, ceids: Sequence[int] = (), enable: bool = True) -> int:
        return _ack(await self.request(2, 37, L(Boolean(enable), u4_list(ceids))))

    async def enable_alarms(self, alids: Sequence[int] = (), enable: bool = True) -> int:
        alid_item: Item = U4(*alids) if alids else U4()
        return _ack(await self.request(5, 3, L(B(b"\x80" if enable else b"\x00"), alid_item)))

    async def list_alarms(self, alids: Sequence[int] = ()) -> list[Any]:
        alid_item: Item = U4(*alids) if alids else U4()
        return ensure_list((await self.request(5, 5, alid_item)).body).to_python()

    async def remote_command(
        self, rcmd: str, params: Mapping[str, Item | str] | None = None
    ) -> tuple[int, list[tuple[str, int]]]:
        items = [L(A(k), A(v) if isinstance(v, str) else v) for k, v in (params or {}).items()]
        reply = await self.request(2, 41, L(A(rcmd), L(*items)))
        body = ensure_list(reply.body, min_len=2)
        cpacks = []
        for row in ensure_list(body[1]):
            r = ensure_list(row, min_len=2)
            cpacks.append((str(r[0].to_python()), as_int(r[1])))
        return as_int(body[0]), cpacks

    async def pp_download(self, ppid: str, body: str) -> tuple[int, int]:
        """S7F1 inquire, then S7F3 send if granted. Returns (PPGNT, ACKC7 or -1)."""
        data = body.encode("ascii")
        grant = _ack(await self.request(7, 1, L(A(ppid), U4(len(data)))))
        if grant != 0:
            return grant, -1
        return grant, _ack(await self.request(7, 3, L(A(ppid), A(body))))

    async def pp_upload(self, ppid: str) -> str | None:
        """S7F5. Returns the PPBODY as text (an <A> or ASCII <B> body), None if unknown."""
        reply = await self.request(7, 5, A(ppid))
        body = ensure_list(reply.body)
        if len(body) != 2:
            return None
        ppbody = body[1]
        return ppbody.value.decode("ascii") if isinstance(ppbody, B) else as_text(ppbody)

    async def pp_list(self) -> list[str]:
        return [str(p) for p in ensure_list((await self.request(7, 19)).body).to_python()]

    async def pp_delete(self, ppids: Sequence[str]) -> int:
        return _ack(await self.request(7, 17, L(*(A(p) for p in ppids))))


def _ack(reply: SecsMessage) -> int:
    """Read a single-byte ACK (<B [1]>) or integer reply body."""
    if reply.body is None:
        raise HsmsError(f"{reply.sf} has no body")
    return as_int(reply.body)
