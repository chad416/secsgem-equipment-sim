"""HSMS-SS connection state machine and timers (SEMI E37 / E37.1).

States (E37 figure 2)::

    NOT CONNECTED --TCP connect--> CONNECTED.NOT_SELECTED --Select--> CONNECTED.SELECTED
          ^                               |   ^                            |
          |                     T7 expiry |   +--------Deselect------------+
          +------ Separate / T6 / T8 / TCP loss ------------------------------+

Timers implemented:

* T3 reply timeout: a primary with W-bit gets no reply in time -> ``T3Timeout``.
* T5 connect separation: delay between active connect attempts (``connect_active``).
* T6 control transaction: Select/Deselect/Linktest response timeout -> connection closed.
* T7 not-selected: passive side closes if no Select arrives in time after connect.
* T8 inter-character: once a message has started, every following TCP chunk must
  arrive within T8 (checked at chunk granularity, the closest a stream socket allows).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..secs.items import SecsDecodeError, decode
from ..secs.message import SecsMessage
from .frame import (
    HEADER_LEN,
    DeselectStatus,
    HsmsHeader,
    RejectReason,
    SelectStatus,
    SType,
    encode_frame,
)

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 16 * 1024 * 1024


class HsmsError(Exception):
    """Base class for HSMS failures."""


class ConnectionClosed(HsmsError):
    pass


class ControlTimeout(HsmsError):
    """T6 expired on a control transaction."""


class T3Timeout(HsmsError):
    def __init__(self, message: SecsMessage, header: bytes) -> None:
        super().__init__(f"T3 timeout waiting for reply to {message.sf} (system 0x{message.system:08X})")
        self.message = message
        self.header = header


class TransactionRejected(HsmsError):
    def __init__(self, reason: int) -> None:
        super().__init__(f"peer sent Reject.req reason {reason}")
        self.reason = reason


class SelectRefused(HsmsError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Select.rsp status {status}")
        self.status = status


@dataclass
class HsmsTimeouts:
    t3: float = 45.0
    t5: float = 10.0
    t6: float = 5.0
    t7: float = 10.0
    t8: float = 5.0
    linktest: float | None = None  # periodic linktest interval, None = off


class State(enum.Enum):
    NOT_CONNECTED = "NOT CONNECTED"
    NOT_SELECTED = "NOT SELECTED"
    SELECTED = "SELECTED"


TraceHook = Callable[[str, HsmsHeader, SecsMessage | None], None]
PrimaryHandler = Callable[[SecsMessage], Awaitable[SecsMessage | None]]
StateListener = Callable[["HsmsConnection", State], None]


class HsmsConnection:
    """One TCP connection carrying one HSMS-SS session."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        passive: bool,
        session_id: int,
        timeouts: HsmsTimeouts,
        handler: PrimaryHandler | None = None,
        trace: TraceHook | None = None,
        name: str = "hsms",
        system_start: int = 1,
        exhausted: bool = False,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.passive = passive
        self.session_id = session_id
        self.timeouts = timeouts
        self.handler = handler
        self._trace = trace
        self.name = name
        self._exhausted = exhausted
        self._system = itertools.count(system_start)
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[SecsMessage]] = {}
        self._pending_ctrl: dict[int, tuple[SType, asyncio.Future[HsmsHeader]]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._listeners: list[StateListener] = []
        self._closed = asyncio.Event()
        self._t7_task: asyncio.Task[None] | None = None
        self.close_reason: str | None = None
        self.state = State.NOT_SELECTED

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._spawn(self._read_loop(), "reader")
        if self.passive:
            self._start_t7()
        if self.timeouts.linktest:
            self._spawn(self._linktest_loop(self.timeouts.linktest), "linktest")

    def add_state_listener(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self, reason: str = "closed") -> None:
        if self._closed.is_set():
            return
        self.close_reason = reason
        log.info("%s: closing connection (%s)", self.name, reason)
        self._set_state(State.NOT_CONNECTED)
        self._closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionClosed(reason))
        for _, fut2 in self._pending_ctrl.values():
            if not fut2.done():
                fut2.set_exception(ConnectionClosed(reason))
        self._pending.clear()
        self._pending_ctrl.clear()
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        if self._t7_task and self._t7_task is not current:
            self._t7_task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), 1.0)

    # ------------------------------------------------------------- public API
    def next_system(self) -> int:
        return next(self._system) & 0xFFFFFFFF

    async def select(self) -> None:
        rsp = await self._control_transaction(SType.SELECT_REQ, SType.SELECT_RSP)
        if rsp.byte3 != SelectStatus.OK:
            raise SelectRefused(rsp.byte3)
        self._set_state(State.SELECTED)

    async def deselect(self) -> int:
        rsp = await self._control_transaction(SType.DESELECT_REQ, SType.DESELECT_RSP)
        if rsp.byte3 == DeselectStatus.OK:
            self._set_state(State.NOT_SELECTED)
            if self.passive:
                self._start_t7()
        return rsp.byte3

    async def linktest(self) -> None:
        await self._control_transaction(SType.LINKTEST_REQ, SType.LINKTEST_RSP)

    async def separate(self) -> None:
        if self.closed:
            return
        header = HsmsHeader.control(SType.SEPARATE_REQ, self.next_system())
        with contextlib.suppress(Exception):
            await self._write(header)
        await self.close("separate")

    async def send(self, msg: SecsMessage) -> SecsMessage:
        """Send a message that expects no reply (no W-bit, or a secondary)."""
        system = msg.system if not msg.is_primary or msg.system else self.next_system()
        out = self._stamp(msg, system)
        await self._write(self._data_header(out), out)
        return out

    async def request(self, msg: SecsMessage, t3: float | None = None) -> SecsMessage:
        """Send a primary with W-bit and wait for the reply (T3)."""
        if self.state is not State.SELECTED:
            raise HsmsError(f"cannot send {msg.sf}: connection {self.state.value}")
        out = self._stamp(msg, self.next_system(), wbit=True)
        fut: asyncio.Future[SecsMessage] = asyncio.get_running_loop().create_future()
        self._pending[out.system] = fut
        header = self._data_header(out)
        try:
            await self._write(header, out)
            return await asyncio.wait_for(fut, self.timeouts.t3 if t3 is None else t3)
        except TimeoutError:
            raise T3Timeout(out, header.encode()) from None
        finally:
            self._pending.pop(out.system, None)

    # --------------------------------------------------------------- internals
    def _spawn(self, coro: Awaitable[None], label: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(f"{self.name}:{label}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _set_state(self, state: State) -> None:
        if state is self.state:
            return
        log.info("%s: %s -> %s", self.name, self.state.value, state.value)
        self.state = state
        if state is not State.NOT_SELECTED and self._t7_task:
            self._t7_task.cancel()
            self._t7_task = None
        for listener in list(self._listeners):
            try:
                listener(self, state)
            except Exception:  # listener bugs must not kill the link
                log.exception("%s: state listener failed", self.name)

    def _start_t7(self) -> None:
        if self._t7_task:
            self._t7_task.cancel()

        async def t7() -> None:
            await asyncio.sleep(self.timeouts.t7)
            if self.state is State.NOT_SELECTED:
                await self.close("T7 not-selected timeout")

        self._t7_task = asyncio.ensure_future(t7())

    def _stamp(self, msg: SecsMessage, system: int, wbit: bool | None = None) -> SecsMessage:
        return SecsMessage(
            msg.stream,
            msg.function,
            msg.wbit if wbit is None else wbit,
            msg.body,
            system,
            self.session_id,
        )

    def _data_header(self, msg: SecsMessage) -> HsmsHeader:
        return HsmsHeader.data(self.session_id, msg.stream, msg.function, msg.wbit, msg.system)

    async def _write(self, header: HsmsHeader, msg: SecsMessage | None = None) -> None:
        if self.closed:
            raise ConnectionClosed(self.close_reason or "closed")
        text = msg.body.encode() if msg is not None and msg.body is not None else b""
        frame = encode_frame(header, text)
        async with self._write_lock:
            if self._trace:
                self._trace("tx", header, msg)
            self._writer.write(frame)
            try:
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                self._spawn(self.close(f"write failed: {exc}"), "close")
                raise ConnectionClosed(str(exc)) from exc

    async def _control_transaction(self, req: SType, rsp: SType) -> HsmsHeader:
        system = self.next_system()
        fut: asyncio.Future[HsmsHeader] = asyncio.get_running_loop().create_future()
        self._pending_ctrl[system] = (rsp, fut)
        try:
            await self._write(HsmsHeader.control(req, system))
            return await asyncio.wait_for(fut, self.timeouts.t6)
        except TimeoutError:
            # E37: T6 expiry is a communication failure -> drop the TCP connection.
            await self.close(f"T6 timeout on {req.name}")
            raise ControlTimeout(f"T6 timeout waiting for {rsp.name}") from None
        finally:
            self._pending_ctrl.pop(system, None)

    async def _linktest_loop(self, interval: float) -> None:
        while not self.closed:
            await asyncio.sleep(interval)
            if self.state is State.SELECTED:
                with contextlib.suppress(HsmsError):
                    await self.linktest()

    async def _read_exact(self, n: int, wait_first: bool) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            if wait_first and not buf:
                chunk = await self._reader.read(n - len(buf))
            else:
                chunk = await asyncio.wait_for(self._reader.read(n - len(buf)), self.timeouts.t8)
            if not chunk:
                raise ConnectionClosed("peer closed TCP connection")
            buf.extend(chunk)
        return bytes(buf)

    async def _read_loop(self) -> None:
        try:
            while True:
                length = int.from_bytes(await self._read_exact(4, wait_first=True), "big")
                if length < HEADER_LEN or length > MAX_MESSAGE_LEN:
                    await self.close(f"invalid message length {length}")
                    return
                payload = await self._read_exact(length, wait_first=False)
                await self._on_frame(payload)
        except TimeoutError:
            await self.close("T8 inter-character timeout")
        except (ConnectionClosed, ConnectionError, OSError) as exc:
            await self.close(str(exc) or type(exc).__name__)
        except asyncio.CancelledError:
            raise

    async def _reject(self, header: HsmsHeader, reason: RejectReason) -> None:
        byte2 = header.ptype if reason is RejectReason.PTYPE_NOT_SUPPORTED else header.stype
        await self._write(HsmsHeader.control(SType.REJECT_REQ, header.system, byte2, reason))

    async def _on_frame(self, payload: bytes) -> None:
        raw = payload[:HEADER_LEN]
        header = HsmsHeader.decode(raw)
        text = payload[HEADER_LEN:]
        if header.ptype != 0:
            if self._trace:
                self._trace("rx", header, None)
            await self._reject(header, RejectReason.PTYPE_NOT_SUPPORTED)
            return
        if header.stype == SType.DATA:
            await self._on_data(header, raw, text)
        else:
            if self._trace:
                self._trace("rx", header, None)
            await self._on_control(header)

    async def _on_data(self, header: HsmsHeader, raw: bytes, text: bytes) -> None:
        body = None
        error = None
        if text:
            try:
                body = decode(text)
            except SecsDecodeError as exc:
                error = str(exc)
        msg = SecsMessage(
            header.stream,
            header.function,
            header.wbit,
            body,
            header.system,
            header.session_id,
            raw,
            error,
        )
        if self._trace:
            self._trace("rx", header, msg)
        if self.state is not State.SELECTED:
            await self._reject(header, RejectReason.ENTITY_NOT_SELECTED)
            return
        if msg.is_primary:
            self._spawn(self._dispatch(msg), msg.sf)
            return
        fut = self._pending.get(header.system)
        if fut is None or fut.done():
            log.warning("%s: unexpected reply %s system 0x%08X dropped", self.name, msg.sf, msg.system)
            return
        fut.set_result(msg)

    async def _dispatch(self, msg: SecsMessage) -> None:
        reply: SecsMessage | None
        try:
            reply = await self.handler(msg) if self.handler else msg.abort()
        except Exception:
            log.exception("%s: handler failed for %s", self.name, msg.sf)
            reply = msg.abort()
        if msg.wbit and reply is not None and not self.closed:
            with contextlib.suppress(ConnectionClosed):
                await self.send(reply)

    async def _on_control(self, header: HsmsHeader) -> None:
        st = header.stype
        if st == SType.SELECT_REQ:
            if self._exhausted:
                status = SelectStatus.CONNECTION_EXHAUST
            elif self.state is State.SELECTED:
                status = SelectStatus.ALREADY_ACTIVE
            else:
                status = SelectStatus.OK
            await self._write(HsmsHeader.control(SType.SELECT_RSP, header.system, 0, status))
            if status is SelectStatus.OK:
                self._set_state(State.SELECTED)
            elif status is SelectStatus.CONNECTION_EXHAUST:
                await self.close("connection exhaust: another session is active")
        elif st == SType.DESELECT_REQ:
            ok = self.state is State.SELECTED
            dstatus = DeselectStatus.OK if ok else DeselectStatus.NOT_ESTABLISHED
            await self._write(HsmsHeader.control(SType.DESELECT_RSP, header.system, 0, dstatus))
            if ok:
                self._set_state(State.NOT_SELECTED)
                if self.passive:
                    self._start_t7()
        elif st == SType.LINKTEST_REQ:
            await self._write(HsmsHeader.control(SType.LINKTEST_RSP, header.system))
        elif st in (SType.SELECT_RSP, SType.DESELECT_RSP, SType.LINKTEST_RSP):
            entry = self._pending_ctrl.get(header.system)
            if entry is None or entry[0] != st:
                await self._reject(header, RejectReason.TRANSACTION_NOT_OPEN)
                return
            # Change state here, in the reader, not in the waiting coroutine: the
            # peer may send a data message right behind Select.rsp.
            if st == SType.SELECT_RSP and header.byte3 == SelectStatus.OK:
                self._set_state(State.SELECTED)
            elif st == SType.DESELECT_RSP and header.byte3 == DeselectStatus.OK:
                self._set_state(State.NOT_SELECTED)
            if not entry[1].done():
                entry[1].set_result(header)
        elif st == SType.REJECT_REQ:
            log.warning("%s: received %s", self.name, header.describe())
            data_fut = self._pending.get(header.system)
            if data_fut and not data_fut.done():
                data_fut.set_exception(TransactionRejected(header.byte3))
            ctrl = self._pending_ctrl.get(header.system)
            if ctrl and not ctrl[1].done():
                ctrl[1].set_exception(TransactionRejected(header.byte3))
        elif st == SType.SEPARATE_REQ:
            await self.close("peer sent Separate.req")
        else:
            await self._reject(header, RejectReason.STYPE_NOT_SUPPORTED)
