"""GEM-side view of the controller.

The GEM layer never touches ``ChamberController`` directly; it talks to a
``ControllerLink``. ``OpcUaLink`` is the real path (OPC UA client against the
controller's server, with reconnect). ``DirectLink`` runs the same controller
model in-process without a socket and is used by unit tests that exercise GEM
logic only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

from asyncua import Client, ua

from .interface import (
    EVENT_RING_SIZE,
    NAMESPACE_URI,
    OBJECT,
    STATUS_TAGS,
    CmdCode,
    CmdResult,
    EventCode,
    Fault,
    RecipeStep,
)
from .model import ChamberController, Params
from .runtime import ScanLoop

log = logging.getLogger(__name__)

EventCallback = Callable[[EventCode, int], None]
AlarmWordCallback = Callable[[int, int], None]  # (old word, new word)
LinkStateCallback = Callable[[bool], None]


class ControllerLink(ABC):
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}
        self.event_listeners: list[EventCallback] = []
        self.alarm_listeners: list[AlarmWordCallback] = []
        self.link_listeners: list[LinkStateCallback] = []
        self.connected = False
        self.last_heartbeat_at = 0.0
        self._alarm_word = 0
        self._event_idx = 0

    # --- implemented by subclasses ------------------------------------------
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def command(self, code: CmdCode, str_arg: str = "", int_arg: int = 0) -> CmdResult: ...

    @abstractmethod
    async def load_recipe(self, name: str, steps: Sequence[RecipeStep]) -> CmdResult: ...

    @abstractmethod
    async def write_params(self, params: dict[str, float]) -> bool:
        """Write ``Param.*`` tags, e.g. ``{"Param.PumpDownTimeout": 60.0}``.

        Returns False if the controller could not be written.
        """

    # --- shared notification plumbing -----------------------------------------
    def _set_connected(self, value: bool) -> None:
        if value == self.connected:
            return
        self.connected = value
        for cb in list(self.link_listeners):
            cb(value)

    def _on_status(self, path: str, value: Any) -> None:
        self.status[path] = value
        if path == "Heartbeat":
            self.last_heartbeat_at = time.monotonic()
        elif path == "Alarms.Word":
            old, self._alarm_word = self._alarm_word, int(value)
            if old != self._alarm_word:
                for cb in list(self.alarm_listeners):
                    cb(old, self._alarm_word)

    def _on_ring(self, write_idx: int, codes: Sequence[int], args: Sequence[int]) -> None:
        """Deliver ring entries [self._event_idx, write_idx) in order."""
        missed = write_idx - self._event_idx - EVENT_RING_SIZE
        if missed > 0:
            log.error("event ring overrun: %d controller events lost", missed)
            self._event_idx = write_idx - EVENT_RING_SIZE
        while self._event_idx < write_idx:
            slot = self._event_idx % EVENT_RING_SIZE
            self._event_idx += 1
            try:
                code = EventCode(codes[slot])
            except ValueError:
                log.warning("unknown controller event code %s", codes[slot])
                continue
            for cb in list(self.event_listeners):
                cb(code, int(args[slot]))


class DirectLink(ControllerLink):
    """In-process link: same controller model, no OPC UA (fast unit tests)."""

    def __init__(self, controller: ChamberController | None = None, *, time_scale: float = 50.0) -> None:
        super().__init__()
        self.controller = controller or ChamberController()
        self.loop = ScanLoop(self.controller, time_scale=time_scale, scan_s=0.01, io=self)

    async def connect(self) -> None:
        for path, value in self.controller.status_image().items():
            self._on_status(path, value)
        self._event_idx = self.controller.event_write_idx
        self.last_heartbeat_at = time.monotonic()
        self.loop.start()
        self._set_connected(True)

    async def close(self) -> None:
        await self.loop.stop()
        self._set_connected(False)

    async def read_inputs(self, controller: ChamberController) -> None:
        return None

    async def write_outputs(self, controller: ChamberController) -> None:
        for path, value in controller.status_image().items():
            self._on_status(path, value)
        self._on_ring(controller.event_write_idx, controller.event_codes, controller.event_args)

    async def command(self, code: CmdCode, str_arg: str = "", int_arg: int = 0) -> CmdResult:
        result = self.controller.command(code, str_arg, int_arg)
        await self.write_outputs(self.controller)
        return result

    async def load_recipe(self, name: str, steps: Sequence[RecipeStep]) -> CmdResult:
        result = self.controller.load_recipe(name, list(steps))
        await self.write_outputs(self.controller)
        return result

    async def write_params(self, params: dict[str, float]) -> bool:
        mapping = {
            "Param.PumpDownTimeout": "pumpdown_timeout_s",
            "Param.BasePressure": "base_pressure_torr",
            "Param.TempHighLimit": "temp_high_limit_c",
            "Param.RobotTimeout": "robot_timeout_s",
        }
        p: Params = self.controller.params
        for path, value in params.items():
            setattr(p, mapping[path], float(value))
        return True

    async def inject(self, faults: Fault) -> None:
        self.controller.faults = faults


# Errors asyncua raises when the session or the TCP connection is gone.
LINK_ERRORS = (ua.UaError, OSError, TimeoutError)


class OpcUaLink(ControllerLink):
    """OPC UA client: subscription for status + event ring, handshake for commands.

    After the first successful ``connect()`` a supervisor task checks the session
    every ``health_s`` (a read of ``Heartbeat``). When that read fails, or the
    subscription reports a bad status, the link is marked down (the GEM layer
    raises ALID 10) and the supervisor reconnects with exponential backoff from
    ``retry_min_s`` up to ``retry_max_s``.

    On reconnect the alarm word goes through the normal edge detection, so alarms
    latched during the outage are reported. Controller events written during the
    outage are replayed from the ring if the controller kept running (at most
    ``EVENT_RING_SIZE``). If it restarted (lower write index) the link
    resynchronises and logs that events may be missing.
    """

    def __init__(
        self,
        url: str,
        *,
        publish_ms: float = 50.0,
        command_timeout: float = 3.0,
        health_s: float = 1.0,
        retry_min_s: float = 0.5,
        retry_max_s: float = 10.0,
    ) -> None:
        super().__init__()
        self.url = url
        self.publish_ms = publish_ms
        self.command_timeout = command_timeout
        self.health_s = health_s
        self.retry_min_s = retry_min_s
        self.retry_max_s = retry_max_s
        self._client: Any = None
        self._ns = 0
        self._path_by_node: dict[Any, str] = {}
        self._cmd_lock = asyncio.Lock()
        self._ack_event = asyncio.Event()
        self._req_seq = 0
        self._ring_lock = asyncio.Lock()
        self._synced_once = False
        self._supervisor: asyncio.Task[None] | None = None

    def _node(self, path: str) -> Any:
        return self._client.get_node(ua.NodeId(f"{OBJECT}.{path}", self._ns))

    async def connect(self) -> None:
        """First connection. Raises if the controller is not reachable."""
        await self._open()
        self._supervisor = asyncio.create_task(self._supervise(), name="opcua-link-supervisor")

    async def close(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._supervisor
            self._supervisor = None
        await self._teardown()
        self._set_connected(False)

    async def _open(self) -> None:
        client = Client(self.url, timeout=4)
        await client.connect()
        try:
            self._client = client
            self._ns = await client.get_namespace_index(NAMESPACE_URI)
            paths = [*STATUS_TAGS, "Cmd.AckSeq"]
            nodes = [self._node(p) for p in paths]
            values = await client.read_values(nodes)
            image = dict(zip(paths, values, strict=True))
            self._path_by_node = {node.nodeid: path for path, node in zip(paths, nodes, strict=True)}
            self._req_seq = int(image["Cmd.AckSeq"])
            self.status["Cmd.AckSeq"] = image["Cmd.AckSeq"]
            for path in STATUS_TAGS:
                if path != "Events.WriteIdx":
                    self._on_status(path, image[path])  # alarm edges from an outage show up here
            await self._sync_events(int(image["Events.WriteIdx"]))
            self.last_heartbeat_at = time.monotonic()
            sub = await client.create_subscription(self.publish_ms, self)
            await sub.subscribe_data_change(nodes, sampling_interval=self.publish_ms / 2)
        except BaseException:
            self._client = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 2.0)
            raise
        self._set_connected(True)
        log.info("OPC UA link connected to %s (ns=%d)", self.url, self._ns)

    async def _sync_events(self, write_idx: int) -> None:
        self.status["Events.WriteIdx"] = write_idx
        if not self._synced_once:
            self._event_idx = write_idx  # first connection: older history is not replayed
            self._synced_once = True
        elif write_idx >= self._event_idx:
            await self._read_ring(write_idx)  # same controller: replay what was missed
        else:
            log.warning(
                "controller event index went back (%d -> %d): controller restarted, "
                "events during the outage may be missing",
                self._event_idx,
                write_idx,
            )
            self._event_idx = write_idx

    async def _teardown(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), 2.0)

    async def _supervise(self) -> None:
        delay = self.retry_min_s
        while True:
            if self.connected:
                await asyncio.sleep(self.health_s)
                try:
                    await asyncio.wait_for(self._node("Heartbeat").read_value(), self.health_s * 2)
                except (*LINK_ERRORS, AttributeError) as exc:
                    log.error("controller link lost: %s", exc or type(exc).__name__)
                    self._set_connected(False)
                continue
            await self._teardown()
            try:
                await self._open()
            except Exception as exc:  # any failure to open means: try again later
                log.warning("reconnect to %s failed (%s); next attempt in %.1f s", self.url, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max_s)
            else:
                log.warning("controller link re-established")
                delay = self.retry_min_s

    # asyncua subscription handler interface ------------------------------------
    async def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        path = self._path_by_node.get(node.nodeid)
        if path is None:
            return
        if path == "Events.WriteIdx":
            self.status[path] = val
            await self._read_ring(int(val))
            return
        if path == "Cmd.AckSeq":
            self.status[path] = val
            self._ack_event.set()
            return
        self._on_status(path, val)

    def status_change_notification(self, status: Any) -> None:
        log.error("OPC UA subscription status change: %s", status)
        self._set_connected(False)

    async def _read_ring(self, write_idx: int) -> None:
        async with self._ring_lock:
            if write_idx <= self._event_idx or self._client is None:
                return
            try:
                codes, args = await self._client.read_values(
                    [self._node("Events.Code"), self._node("Events.Arg")]
                )
            except LINK_ERRORS as exc:
                # the read index stays put; the next notification or the reconnect re-reads
                log.warning("event ring read failed: %s", exc)
                return
            self._on_ring(write_idx, codes, args)

    # commands -------------------------------------------------------------------
    async def command(self, code: CmdCode, str_arg: str = "", int_arg: int = 0) -> CmdResult:
        async with self._cmd_lock:
            if not self.connected or self._client is None:
                return CmdResult.NO_RESPONSE
            try:
                return await self._handshake(code, str_arg, int_arg)
            except LINK_ERRORS as exc:
                log.error("command %s failed: %s", code.name, exc)
                return CmdResult.NO_RESPONSE

    async def _handshake(self, code: CmdCode, str_arg: str, int_arg: int) -> CmdResult:
        self._req_seq = (self._req_seq + 1) & 0xFFFFFFFF
        seq = self._req_seq
        await self._client.write_values(
            [self._node("Cmd.Code"), self._node("Cmd.StrArg"), self._node("Cmd.IntArg")],
            [
                ua.DataValue(ua.Variant(int(code), ua.VariantType.Int32)),
                ua.DataValue(ua.Variant(str_arg, ua.VariantType.String)),
                ua.DataValue(ua.Variant(int(int_arg), ua.VariantType.Int32)),
            ],
        )
        self._ack_event.clear()
        await self._node("Cmd.ReqSeq").write_value(ua.DataValue(ua.Variant(seq, ua.VariantType.UInt32)))
        deadline = time.monotonic() + self.command_timeout
        while int(self.status.get("Cmd.AckSeq", -1)) != seq:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.error("controller did not acknowledge %s (seq %d)", code.name, seq)
                return CmdResult.NO_RESPONSE
            self._ack_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ack_event.wait(), remaining)
        result = await self._node("Cmd.Result").read_value()
        return CmdResult(int(result))

    async def load_recipe(self, name: str, steps: Sequence[RecipeStep]) -> CmdResult:
        def arr(values: list[Any], vt: Any) -> Any:
            return ua.DataValue(ua.Variant(values, vt, is_array=True))

        if not self.connected or self._client is None:
            return CmdResult.NO_RESPONSE
        try:
            await self._client.write_values(
                [
                    self._node("RecipeIn.Name"),
                    self._node("RecipeIn.StepNames"),
                    self._node("RecipeIn.TempSP"),
                    self._node("RecipeIn.PressureSP"),
                    self._node("RecipeIn.Duration"),
                ],
                [
                    ua.DataValue(ua.Variant(name, ua.VariantType.String)),
                    arr([s.name for s in steps], ua.VariantType.String),
                    arr([s.temp_c for s in steps], ua.VariantType.Double),
                    arr([s.pressure_torr for s in steps], ua.VariantType.Double),
                    arr([s.duration_s for s in steps], ua.VariantType.Double),
                ],
            )
        except LINK_ERRORS as exc:
            log.error("recipe download failed: %s", exc)
            return CmdResult.NO_RESPONSE
        return await self.command(CmdCode.LOAD_RECIPE)

    async def write_params(self, params: dict[str, float]) -> bool:
        if not self.connected or self._client is None:
            return False
        try:
            await self._client.write_values(
                [self._node(p) for p in params],
                [ua.DataValue(ua.Variant(float(v), ua.VariantType.Double)) for v in params.values()],
            )
        except LINK_ERRORS as exc:
            log.error("parameter write failed: %s", exc)
            return False
        return True


class MaintenanceClient:
    """Separate OPC UA client standing for a technician's laptop on the
    controller: it only writes ``Sim.Faults``. The GEM layer never uses it."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._client = Client(url, timeout=4)
        self._ns = 0

    async def connect(self) -> None:
        await self._client.connect()
        self._ns = await self._client.get_namespace_index(NAMESPACE_URI)

    async def close(self) -> None:
        await self._client.disconnect()

    async def inject(self, faults: Fault) -> None:
        node = self._client.get_node(ua.NodeId(f"{OBJECT}.Sim.Faults", self._ns))
        await node.write_value(ua.DataValue(ua.Variant(int(faults), ua.VariantType.UInt32)))
        log.warning("maintenance: Sim.Faults <- %r", faults)
