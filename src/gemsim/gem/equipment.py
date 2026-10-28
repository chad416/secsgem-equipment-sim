"""GEM equipment: the SECS/GEM interface of the simulated process chamber.

This is the "equipment PC" software that sits between the factory host (HSMS)
and the machine controller (OPC UA). It owns everything GEM: communication and
control state, variables, reports, alarms, remote commands and process program
storage. Machine behaviour lives in the controller; the GEM layer only reads its
process image and writes commands through ``ControllerLink``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..controller.interface import CmdCode, CmdResult, EventCode, SeqState
from ..controller.link import ControllerLink
from ..hsms import (
    HsmsConnection,
    HsmsError,
    HsmsPassiveServer,
    HsmsTimeouts,
    State,
    T3Timeout,
)
from ..hsms.connection import TraceHook
from ..secs import (
    U4,
    A,
    B,
    Item,
    L,
    SecsDecodeError,
    SecsMessage,
    as_bool,
    as_int,
    as_ints,
    as_text,
    ensure_list,
)
from .alarms import AlarmCategory, AlarmDef, AlarmManager
from .ids import ALID, CE, CE_DVS, DV, EC, MDLN, SOFTREV, SV, ControlState, ProcessState
from .process_programs import AckC7, ProcessProgramStore, Recipe, RecipeError, parse_recipe
from .remote_commands import COMMANDS, CpAck, HcAck, ParamError, hcack_for
from .reports import ReportManager
from .state_models import CommState, ControlStateMachine
from .variables import (
    EquipmentConstant,
    Variable,
    VariableRegistry,
    enc_bool,
    enc_f4,
    enc_text,
    enc_u1,
    enc_u2,
    enc_u4,
    enc_u4_list,
)

log = logging.getLogger(__name__)

Handler = Callable[[SecsMessage], Awaitable[Item | None]]

ALARMS = (
    AlarmDef(
        ALID.VACUUM_NOT_REACHED,
        "Pump-down did not reach base pressure",
        AlarmCategory.PARAMETER_CONTROL_ERROR,
        0,
    ),
    AlarmDef(
        ALID.ROBOT_TIMEOUT, "Wafer handling robot move timeout", AlarmCategory.EQUIPMENT_STATUS_WARNING, 1
    ),
    AlarmDef(
        ALID.OVER_TEMPERATURE, "Chuck over-temperature, heater tripped", AlarmCategory.EQUIPMENT_SAFETY, 2
    ),
    AlarmDef(ALID.CONTROLLER_LINK_LOST, "Machine controller link lost", AlarmCategory.DATA_INTEGRITY),
)

CONTROLLER_EVENT_CE: dict[EventCode, CE] = {
    EventCode.LOT_STARTED: CE.LOT_STARTED,
    EventCode.WAFER_LOADED: CE.WAFER_LOADED,
    EventCode.PUMPDOWN_COMPLETE: CE.PUMPDOWN_COMPLETE,
    EventCode.STEP_STARTED: CE.STEP_STARTED,
    EventCode.STEP_COMPLETED: CE.STEP_COMPLETED,
    EventCode.WAFER_PROCESSED: CE.WAFER_PROCESSED,
    EventCode.VENT_COMPLETE: CE.VENT_COMPLETE,
    EventCode.WAFER_UNLOADED: CE.WAFER_UNLOADED,
    EventCode.LOT_COMPLETED: CE.LOT_COMPLETED,
    EventCode.LOT_STOPPED: CE.LOT_STOPPED,
    EventCode.LOT_ABORTED: CE.LOT_ABORTED,
    EventCode.HELD: CE.PROCESS_HELD,
    EventCode.RESUMED: CE.PROCESS_RESUMED,
}

SEQUENCER_COMMANDS = {
    "START": CmdCode.START,
    "STOP": CmdCode.STOP,
    "PAUSE": CmdCode.PAUSE,
    "RESUME": CmdCode.RESUME,
    "ABORT": CmdCode.ABORT,
}


@dataclass
class EquipmentConfig:
    host: str = "127.0.0.1"
    port: int = 5000
    session_id: int = 1
    timeouts: HsmsTimeouts = field(default_factory=HsmsTimeouts)
    initial_online: bool = False
    initial_remote: bool = False
    offline_after_failed_attempt: ControlState = ControlState.HOST_OFFLINE
    send_s1f13: bool = True  # equipment-initiated establish communications
    watchdog_s: float = 3.0  # controller heartbeat supervision


class GemEquipment:
    def __init__(
        self,
        link: ControllerLink,
        config: EquipmentConfig | None = None,
        *,
        trace: TraceHook | None = None,
    ) -> None:
        self.link = link
        self.cfg = config or EquipmentConfig()
        self.control = ControlStateMachine(
            initial_online=self.cfg.initial_online,
            remote=self.cfg.initial_remote,
            offline_after_failed_attempt=self.cfg.offline_after_failed_attempt,
        )
        self.comm = CommState.WAIT_CRA
        self.vars = VariableRegistry()
        self.alarms = AlarmManager(ALARMS)
        self.reports = ReportManager([int(c) for c in CE], self.vars.exists)
        self.pps = ProcessProgramStore()
        self.process_state = ProcessState.IDLE
        self.previous_process_state = ProcessState.IDLE
        self.selected: Recipe | None = None
        self.clock_offset = 0.0
        self.dataid = 0
        self.ce_history: list[tuple[int, dict[int, Any]]] = []
        self.server = HsmsPassiveServer(
            self.cfg.host,
            self.cfg.port,
            session_id=self.cfg.session_id,
            timeouts=self.cfg.timeouts,
            handler=self._on_primary,
            trace=trace,
        )
        self.server.add_state_listener(self._on_hsms_state)
        self._outbox: asyncio.Queue[SecsMessage] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._comm_task: asyncio.Task[None] | None = None
        self._comm_established = asyncio.Event()
        self._handlers: dict[tuple[int, int], Handler] = {}
        self._stopping = False
        self._build_variables()
        self._register_handlers()
        self.control.listeners.append(self._on_control_transition)
        link.event_listeners.append(self._on_controller_event)
        link.alarm_listeners.append(self._on_alarm_word)
        link.link_listeners.append(self._on_link_state)

    # ================================================================ lifecycle
    async def start(self) -> None:
        await self.link.connect()
        self._derive_initial_process_state()
        await self.server.start()
        self._tasks.append(asyncio.create_task(self._sender(), name="gem-sender"))
        self._tasks.append(asyncio.create_task(self._watchdog(), name="gem-watchdog"))

    async def stop(self) -> None:
        self._stopping = True  # an orderly link close is not a link-lost alarm
        for task in [*self._tasks, *([self._comm_task] if self._comm_task else [])]:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        await self.server.stop()
        await self.link.close()

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def connection(self) -> HsmsConnection | None:
        conn = self.server.connection
        return conn if conn is not None and not conn.closed else None

    # ============================================================ variables
    def _build_variables(self) -> None:
        st = self.link.status

        def tag(path: str, default: Any = 0) -> Callable[[], Any]:
            return lambda: st.get(path, default)

        add = self.vars.add_sv
        add(Variable(SV.CLOCK, "Clock", "", enc_text, self.clock))
        add(Variable(SV.CONTROL_STATE, "ControlState", "", enc_u1, lambda: self.control.state))
        add(
            Variable(
                SV.PREVIOUS_CONTROL_STATE, "PreviousControlState", "", enc_u1, lambda: self.control.previous
            )
        )
        add(
            Variable(
                SV.EVENTS_ENABLED, "EventsEnabled", "", enc_u4_list, lambda: sorted(self.reports.enabled)
            )
        )
        add(
            Variable(SV.ALARMS_ENABLED, "AlarmsEnabled", "", enc_u4_list, lambda: sorted(self.alarms.enabled))
        )
        add(Variable(SV.ALARMS_SET, "AlarmsSet", "", enc_u4_list, lambda: sorted(self.alarms.active)))
        add(
            Variable(
                SV.PP_EXEC_NAME,
                "PPExecName",
                "",
                enc_text,
                lambda: self.selected.ppid if self.selected else "",
            )
        )
        add(Variable(SV.PROCESS_STATE, "ProcessState", "", enc_u1, lambda: self.process_state))
        add(
            Variable(
                SV.PREVIOUS_PROCESS_STATE,
                "PreviousProcessState",
                "",
                enc_u1,
                lambda: self.previous_process_state,
            )
        )
        add(Variable(SV.CHAMBER_PRESSURE, "ChamberPressure", "Torr", enc_f4, tag("Chamber.Pressure")))
        add(Variable(SV.CHAMBER_PRESSURE_SP, "ChamberPressureSP", "Torr", enc_f4, tag("Chamber.PressureSP")))
        add(Variable(SV.CHUCK_TEMPERATURE, "ChuckTemperature", "degC", enc_f4, tag("Chamber.Temperature")))
        add(
            Variable(
                SV.CHUCK_TEMPERATURE_SP, "ChuckTemperatureSP", "degC", enc_f4, tag("Chamber.TemperatureSP")
            )
        )
        add(Variable(SV.WAFER_PRESENT, "WaferPresent", "", enc_bool, tag("Chamber.WaferPresent", False)))
        add(Variable(SV.ROBOT_BUSY, "RobotBusy", "", enc_bool, tag("Robot.Busy", False)))
        add(Variable(SV.SEQ_STATE, "SequencerState", "", enc_u1, tag("Seq.State")))
        add(Variable(SV.STEP_NO, "StepNumber", "", enc_u4, tag("Seq.StepNo")))
        add(Variable(SV.STEP_NAME, "StepName", "", enc_text, tag("Seq.StepName", "")))
        add(Variable(SV.STEP_TIME_LEFT, "StepTimeLeft", "s", enc_f4, tag("Seq.StepTimeLeft")))
        add(Variable(SV.LOT_ID, "LotID", "", enc_text, tag("Lot.Id", "")))
        add(Variable(SV.LOT_WAFER_COUNT, "LotWaferCount", "", enc_u4, tag("Lot.WaferCount")))
        add(Variable(SV.WAFERS_PROCESSED, "WafersProcessed", "", enc_u4, tag("Lot.WafersDone")))
        add(Variable(SV.WAFER_NO, "WaferNumber", "", enc_u4, tag("Seq.WaferNo")))
        add(Variable(SV.CONTROLLER_LINK_UP, "ControllerLinkUp", "", enc_bool, self.controller_ok))
        add(Variable(SV.MDLN, "MDLN", "", enc_text, lambda: MDLN))
        add(Variable(SV.SOFTREV, "SOFTREV", "", enc_text, lambda: SOFTREV))

        dv = self.vars.add_dv
        dv(Variable(DV.EVENT_WAFER_NO, "EventWaferNumber", "", enc_u4))
        dv(Variable(DV.EVENT_STEP_NO, "EventStepNumber", "", enc_u4))
        dv(Variable(DV.EVENT_STEP_NAME, "EventStepName", "", enc_text))
        dv(Variable(DV.ALARM_ID, "AlarmID", "", enc_u4))
        dv(Variable(DV.ALARM_TEXT, "AlarmText", "", enc_text))
        dv(Variable(DV.LOT_RESULT_WAFERS, "LotResultWafers", "", enc_u4))
        dv(Variable(DV.HELD_REASON, "HeldReason", "", enc_u1))
        dv(Variable(DV.PP_SELECTED, "PPSelected", "", enc_text))

        ec = self.vars.add_ec
        ec(
            EquipmentConstant(
                EC.ESTABLISH_COMM_TIMEOUT, "EstablishCommunicationsTimeout", "s", enc_u2, 1, 120, 10
            )
        )
        ec(EquipmentConstant(EC.TIME_FORMAT, "TimeFormat", "", enc_u1, 0, 1, 1))
        ec(
            EquipmentConstant(
                EC.PUMPDOWN_TIMEOUT, "PumpDownTimeout", "s", enc_f4, 10, 600, 90, "Param.PumpDownTimeout"
            )
        )
        ec(
            EquipmentConstant(
                EC.BASE_PRESSURE, "BasePressure", "Torr", enc_f4, 0.01, 1.0, 0.05, "Param.BasePressure"
            )
        )
        ec(
            EquipmentConstant(
                EC.TEMP_HIGH_LIMIT, "TempHighLimit", "degC", enc_f4, 100, 500, 450, "Param.TempHighLimit"
            )
        )
        ec(EquipmentConstant(EC.ROBOT_TIMEOUT, "RobotTimeout", "s", enc_f4, 5, 120, 20, "Param.RobotTimeout"))

    def clock(self) -> str:
        now = dt.datetime.now() + dt.timedelta(seconds=self.clock_offset)
        if self.vars.ecs[EC.TIME_FORMAT].value == 0:
            return now.strftime("%y%m%d%H%M%S")
        return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 10000:02d}"

    def controller_ok(self) -> bool:
        return self.link.connected and ALID.CONTROLLER_LINK_LOST not in self.alarms.active

    # ======================================================= message dispatch
    def _register_handlers(self) -> None:
        h = self._handlers
        h[(1, 1)] = self._s1f1
        h[(1, 3)] = self._s1f3
        h[(1, 11)] = self._s1f11
        h[(1, 13)] = self._s1f13
        h[(1, 15)] = self._s1f15
        h[(1, 17)] = self._s1f17
        h[(1, 21)] = self._s1f21
        h[(1, 23)] = self._s1f23
        h[(2, 13)] = self._s2f13
        h[(2, 15)] = self._s2f15
        h[(2, 17)] = self._s2f17
        h[(2, 29)] = self._s2f29
        h[(2, 31)] = self._s2f31
        h[(2, 33)] = self._s2f33
        h[(2, 35)] = self._s2f35
        h[(2, 37)] = self._s2f37
        h[(2, 41)] = self._s2f41
        h[(5, 3)] = self._s5f3
        h[(5, 5)] = self._s5f5
        h[(5, 7)] = self._s5f7
        h[(6, 15)] = self._s6f15
        h[(6, 19)] = self._s6f19
        h[(7, 1)] = self._s7f1
        h[(7, 3)] = self._s7f3
        h[(7, 5)] = self._s7f5
        h[(7, 17)] = self._s7f17
        h[(7, 19)] = self._s7f19

    @property
    def supported(self) -> list[tuple[int, int]]:
        return sorted(self._handlers)

    async def _send_s9(self, function: int, msg: SecsMessage) -> None:
        conn = self.connection
        if conn is None:
            return
        with contextlib.suppress(HsmsError):
            await conn.send(SecsMessage(9, function, False, B(msg.raw_header)))

    async def _on_primary(self, msg: SecsMessage) -> SecsMessage | None:
        if msg.session_id != self.cfg.session_id:
            await self._send_s9(1, msg)  # unrecognized device ID
            return None
        streams = {s for s, _ in self._handlers}
        if msg.stream not in streams:
            await self._send_s9(3, msg)
            return None
        handler = self._handlers.get((msg.stream, msg.function))
        if handler is None:
            await self._send_s9(5, msg)
            return None
        if msg.decode_error:
            await self._send_s9(7, msg)
            return None
        sf = (msg.stream, msg.function)
        if self.comm is not CommState.COMMUNICATING and sf != (1, 13):
            log.info("%s received while NOT COMMUNICATING -> abort", msg.sf)
            return msg.abort()
        if not self.control.online and sf not in ((1, 13), (1, 17)):
            return msg.abort()  # E30: off-line equipment answers SxF0
        try:
            body = await handler(msg)
        except SecsDecodeError as exc:
            log.warning("%s: illegal data: %s", msg.sf, exc)
            await self._send_s9(7, msg)
            return None
        return msg.reply(body)

    # ----------------------------------------------------------------- stream 1
    async def _s1f1(self, msg: SecsMessage) -> Item:
        return L(A(MDLN), A(SOFTREV))

    async def _s1f3(self, msg: SecsMessage) -> Item:
        svids = as_ints(msg.body) if msg.body is not None else []
        svids = svids or sorted(self.vars.svs)
        return L(*(self.vars.sv_item(s) for s in svids))

    async def _s1f11(self, msg: SecsMessage) -> Item:
        svids = (as_ints(msg.body) if msg.body is not None else []) or sorted(self.vars.svs)
        rows = []
        for s in svids:
            var = self.vars.svs.get(s)
            rows.append(L(U4(s), A(var.name if var else ""), A(var.units if var else "")))
        return L(*rows)

    async def _s1f13(self, msg: SecsMessage) -> Item:
        self._set_comm(CommState.COMMUNICATING)
        return L(B(b"\x00"), L(A(MDLN), A(SOFTREV)))

    async def _s1f15(self, msg: SecsMessage) -> Item:
        return B(bytes([self.control.host_request_offline()]))

    async def _s1f17(self, msg: SecsMessage) -> Item:
        return B(bytes([self.control.host_request_online()]))

    async def _s1f21(self, msg: SecsMessage) -> Item:
        vids = (as_ints(msg.body) if msg.body is not None else []) or sorted(self.vars.dvs)
        rows = []
        for v in vids:
            var = self.vars.dvs.get(v)
            rows.append(L(U4(v), A(var.name if var else ""), A(var.units if var else "")))
        return L(*rows)

    async def _s1f23(self, msg: SecsMessage) -> Item:
        ceids = (as_ints(msg.body) if msg.body is not None else []) or [int(c) for c in CE]
        rows = []
        for c in ceids:
            try:
                ce = CE(c)
            except ValueError:
                rows.append(L(U4(c), A(""), L()))
                continue
            rows.append(L(U4(c), A(ce.name), L(*(U4(v) for v in CE_DVS.get(ce, ())))))
        return L(*rows)

    # ----------------------------------------------------------------- stream 2
    async def _s2f13(self, msg: SecsMessage) -> Item:
        ecids = (as_ints(msg.body) if msg.body is not None else []) or sorted(self.vars.ecs)
        return L(*(self.vars.ecs[e].item() if e in self.vars.ecs else L() for e in ecids))

    async def _s2f15(self, msg: SecsMessage) -> Item:
        pairs = ensure_list(msg.body)
        updates: list[tuple[EquipmentConstant, float]] = []
        for pair in pairs:
            p = ensure_list(pair, min_len=2)
            ec = self.vars.ecs.get(as_int(p[0]))
            if ec is None:
                return B(b"\x01")  # EAC 1: at least one ECID does not exist
            value = ec.coerce(p[1])
            if value is None or not ec.in_range(value):
                return B(b"\x03")  # EAC 3: out of range (or wrong format)
            updates.append((ec, value))
        machine = {ec.controller_tag: v for ec, v in updates if ec.controller_tag is not None}
        if machine and self.link.status.get("Seq.State") != SeqState.IDLE:
            return B(b"\x02")  # EAC 2: busy - machine parameters only change while idle
        if machine:
            await self.link.write_params(machine)
        for ec, value in updates:
            ec.value = value
        return B(b"\x00")

    async def _s2f17(self, msg: SecsMessage) -> Item:
        return A(self.clock())

    async def _s2f29(self, msg: SecsMessage) -> Item:
        ecids = (as_ints(msg.body) if msg.body is not None else []) or sorted(self.vars.ecs)
        rows = []
        for e in ecids:
            ec = self.vars.ecs.get(e)
            if ec is None:
                rows.append(L(U4(e), A(""), A(""), A(""), A(""), A("")))
                continue
            rows.append(
                L(
                    U4(e),
                    A(ec.name),
                    ec.encoder(ec.minimum),
                    ec.encoder(ec.maximum),
                    ec.encoder(ec.default),
                    A(ec.units),
                )
            )
        return L(*rows)

    async def _s2f31(self, msg: SecsMessage) -> Item:
        text = as_text(msg.body) if msg.body is not None else ""
        try:
            if len(text) == 12:
                target = dt.datetime.strptime(text, "%y%m%d%H%M%S")
            elif len(text) == 16:
                target = dt.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
            else:
                return B(b"\x01")
        except ValueError:
            return B(b"\x01")
        # The simulator keeps an offset instead of changing the PC clock.
        self.clock_offset = (target - dt.datetime.now()).total_seconds()
        return B(b"\x00")

    async def _s2f33(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        defs = []
        for entry in ensure_list(body[1]):
            e = ensure_list(entry, min_len=2)
            defs.append((as_int(e[0]), as_ints(e[1])))
        return B(bytes([self.reports.define(defs)]))

    async def _s2f35(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        links = []
        for entry in ensure_list(body[1]):
            e = ensure_list(entry, min_len=2)
            links.append((as_int(e[0]), as_ints(e[1])))
        return B(bytes([self.reports.link(links)]))

    async def _s2f37(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        return B(bytes([self.reports.enable(as_bool(body[0]), as_ints(body[1]))]))

    async def _s2f41(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        rcmd = as_text(body[0])
        raw_params = []
        for p in ensure_list(body[1]):
            pair = ensure_list(p, min_len=2)
            raw_params.append((as_text(pair[0]), pair[1]))
        hcack, cpacks = await self.remote_command(rcmd, raw_params)
        return L(B(bytes([hcack])), L(*(L(A(n), B(bytes([c]))) for n, c in cpacks)))

    # ----------------------------------------------------------------- stream 5
    async def _s5f3(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        aled = body[0]
        if not isinstance(aled, B) or len(aled.value) != 1:
            raise SecsDecodeError("ALED must be <B [1]>")
        return B(bytes([self.alarms.enable(bool(aled.first & 0x80), as_ints(body[1]))]))

    def _alarm_rows(self, alids: list[int]) -> Item:
        rows = []
        for alid in alids:
            d = self.alarms.defs.get(alid)
            if d is None:
                rows.append(L(B(b""), U4(alid), A("")))
                continue
            rows.append(L(B(bytes([d.alcd(alid in self.alarms.active)])), U4(alid), A(d.text)))
        return L(*rows)

    async def _s5f5(self, msg: SecsMessage) -> Item:
        alids = (as_ints(msg.body) if msg.body is not None else []) or sorted(self.alarms.defs)
        return self._alarm_rows(alids)

    async def _s5f7(self, msg: SecsMessage) -> Item:
        return self._alarm_rows(sorted(self.alarms.enabled))

    # ----------------------------------------------------------------- stream 6
    def _report_body(self, ceid: int, context: dict[int, Any]) -> Item:
        self.dataid = (self.dataid + 1) & 0xFFFFFFFF
        rpts = [
            L(U4(rptid), L(*(self.vars.value_item(v, context) for v in vids)))
            for rptid, vids in self.reports.reports_for(ceid)
        ]
        return L(U4(self.dataid), U4(ceid), L(*rpts))

    async def _s6f15(self, msg: SecsMessage) -> Item:
        if msg.body is None:
            raise SecsDecodeError("S6F15 needs a CEID")
        return self._report_body(as_int(msg.body), {})

    async def _s6f19(self, msg: SecsMessage) -> Item:
        if msg.body is None:
            raise SecsDecodeError("S6F19 needs a RPTID")
        vids = self.reports.reports.get(as_int(msg.body), [])
        return L(*(self.vars.value_item(v, {}) for v in vids))

    # ----------------------------------------------------------------- stream 7
    async def _s7f1(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        return B(bytes([self.pps.inquire(as_text(body[0]), as_int(body[1]))]))

    async def _s7f3(self, msg: SecsMessage) -> Item:
        body = ensure_list(msg.body, min_len=2)
        ppbody = body[1]
        if isinstance(ppbody, A):
            data = ppbody.value.encode("ascii")
        elif isinstance(ppbody, B):
            data = ppbody.value
        else:
            raise SecsDecodeError("PPBODY must be <A> or <B>")
        return B(bytes([self.pps.store(as_text(body[0]), data)]))

    async def _s7f5(self, msg: SecsMessage) -> Item:
        if msg.body is None:
            raise SecsDecodeError("S7F5 needs a PPID")
        ppid = as_text(msg.body)
        data = self.pps.get(ppid)
        if data is None:
            return L()  # E5: zero-length list when PPID is unknown
        return L(A(ppid), A(data.decode("ascii", errors="replace")))

    async def _s7f17(self, msg: SecsMessage) -> Item:
        ppids = [as_text(p) for p in ensure_list(msg.body)]
        result = self.pps.delete(ppids)
        if result is AckC7.ACCEPTED and self.selected and self.selected.ppid not in self.pps.programs:
            self.selected = None
            self._set_process_state(ProcessState.IDLE)
        return B(bytes([result]))

    async def _s7f19(self, msg: SecsMessage) -> Item:
        return L(*(A(p) for p in self.pps.directory()))

    # ======================================================= remote commands
    async def remote_command(
        self, rcmd: str, params: list[tuple[str, Item]]
    ) -> tuple[HcAck, list[tuple[str, CpAck]]]:
        spec = COMMANDS.get(rcmd)
        if spec is None:
            return HcAck.INVALID_COMMAND, []
        values: dict[str, Any] = {}
        bad: list[tuple[str, CpAck]] = []
        for name, item in params:
            parser = spec.params.get(name)
            if parser is None:
                bad.append((name, CpAck.NAME_UNKNOWN))
                continue
            try:
                values[name] = parser(item)
            except ParamError as exc:
                bad.append((name, exc.cpack))
        # Missing required parameter: reported with CPACK 2 (see DESIGN.md).
        bad.extend(
            (name, CpAck.ILLEGAL_VALUE)
            for name in spec.required
            if name not in values and name not in dict(bad)
        )
        if bad:
            return HcAck.PARAMETER_INVALID, bad
        if not self.control.remote:
            return HcAck.CANNOT_PERFORM_NOW, []
        if not self.controller_ok():
            return HcAck.CANNOT_PERFORM_NOW, []
        if rcmd == "PP-SELECT":
            return await self._pp_select(values["PPID"])
        code = SEQUENCER_COMMANDS[rcmd]
        if code is CmdCode.START and self.selected is None:
            return HcAck.CANNOT_PERFORM_NOW, []
        result = await self.link.command(code, values.get("LOTID", ""), values.get("WAFERS", 1))
        log.info("remote command %s -> controller %s", rcmd, result.name)
        if code is CmdCode.START and result is CmdResult.OK and self.selected:
            self.pps.locked = self.selected.ppid
        return hcack_for(result), []

    async def _pp_select(self, ppid: str) -> tuple[HcAck, list[tuple[str, CpAck]]]:
        body = self.pps.get(ppid)
        if body is None:
            return HcAck.NO_SUCH_OBJECT, []
        if self.link.status.get("Seq.State") != SeqState.IDLE:
            return HcAck.CANNOT_PERFORM_NOW, []
        try:
            recipe = parse_recipe(ppid, body)
        except RecipeError as exc:
            log.warning("PP-SELECT %s: invalid recipe: %s", ppid, exc)
            return HcAck.PARAMETER_INVALID, [("PPID", CpAck.ILLEGAL_VALUE)]
        result = await self.link.load_recipe(ppid, recipe.steps)
        if result is CmdResult.BAD_ARG:
            return HcAck.PARAMETER_INVALID, [("PPID", CpAck.ILLEGAL_VALUE)]
        if result is not CmdResult.OK:
            return hcack_for(result), []
        self.selected = recipe
        self._set_process_state(ProcessState.READY)
        self.collection_event(CE.PP_SELECTED, {DV.PP_SELECTED: ppid})
        return HcAck.OK, []

    # ======================================================= events & alarms
    def collection_event(
        self, ceid: CE, context: dict[int, Any] | None = None, *, force: bool = False
    ) -> None:
        ctx = dict(context or {})
        self.ce_history.append((int(ceid), ctx))
        if not (self.control.online or force) or ceid not in self.reports.enabled:
            return
        self._outbox.put_nowait(SecsMessage(6, 11, True, self._report_body(ceid, ctx)))

    def _set_process_state(self, new: ProcessState) -> None:
        if new is self.process_state:
            return
        self.previous_process_state, self.process_state = self.process_state, new
        log.info("process state %s -> %s", self.previous_process_state.name, new.name)
        self.collection_event(CE.PROCESSING_STATE_CHANGE)

    def _derive_initial_process_state(self) -> None:
        state = self.link.status.get("Seq.State", SeqState.IDLE)
        if state == SeqState.HELD:
            self.process_state = ProcessState.PAUSE
        elif state != SeqState.IDLE:
            self.process_state = ProcessState.EXECUTING

    def _on_controller_event(self, code: EventCode, arg: int) -> None:
        ctx: dict[int, Any] = {}
        if code in (EventCode.LOT_STARTED, EventCode.RESUMED):
            self._set_process_state(ProcessState.EXECUTING)
        elif code is EventCode.HELD:
            self._set_process_state(ProcessState.PAUSE)
            ctx[DV.HELD_REASON] = arg
        elif code in (EventCode.LOT_COMPLETED, EventCode.LOT_STOPPED, EventCode.LOT_ABORTED):
            self.pps.locked = None
            self._set_process_state(ProcessState.READY if self.selected else ProcessState.IDLE)
            ctx[DV.LOT_RESULT_WAFERS] = arg
        elif code in (EventCode.STEP_STARTED, EventCode.STEP_COMPLETED):
            ctx[DV.EVENT_STEP_NO] = arg
            if self.selected and 1 <= arg <= len(self.selected.steps):
                ctx[DV.EVENT_STEP_NAME] = self.selected.steps[arg - 1].name
        else:
            ctx[DV.EVENT_WAFER_NO] = arg
        ce = CONTROLLER_EVENT_CE.get(code)
        if ce is not None:
            self.collection_event(ce, ctx)

    def _on_alarm_word(self, old: int, new: int) -> None:
        for bit, d in self.alarms.by_bit.items():
            mask = 1 << bit
            if (old ^ new) & mask:
                self._alarm_changed(d, bool(new & mask))

    def _alarm_changed(self, d: AlarmDef, active: bool) -> None:
        changed = self.alarms.set(d.alid) if active else self.alarms.clear(d.alid)
        if not changed:
            return
        log.warning("alarm %s %d %s", "SET" if active else "CLEARED", d.alid, d.text)
        if self.control.online and self.alarms.is_enabled(d.alid):
            body = L(B(bytes([d.alcd(active)])), U4(d.alid), A(d.text))
            self._outbox.put_nowait(SecsMessage(5, 1, True, body))
        self.collection_event(
            CE.ALARM_SET if active else CE.ALARM_CLEARED,
            {DV.ALARM_ID: d.alid, DV.ALARM_TEXT: d.text},
        )

    def _on_link_state(self, connected: bool) -> None:
        if self._stopping:
            return
        self._alarm_changed(self.alarms.defs[ALID.CONTROLLER_LINK_LOST], not connected)

    async def _watchdog(self) -> None:
        d = self.alarms.defs[ALID.CONTROLLER_LINK_LOST]
        while True:
            await asyncio.sleep(0.25)
            stale = time.monotonic() - self.link.last_heartbeat_at > self.cfg.watchdog_s
            self._alarm_changed(d, stale or not self.link.connected)

    # ===================================================== outbound messages
    async def _sender(self) -> None:
        """Sends S6F11/S5F1 one at a time, in order, each waiting for its ack."""
        while True:
            msg = await self._outbox.get()
            conn = self.connection
            if conn is None or conn.state is not State.SELECTED or self.comm is not CommState.COMMUNICATING:
                log.warning("%s discarded: no communicating host (spooling not implemented)", msg.sf)
                continue
            try:
                await conn.request(msg)
            except T3Timeout as exc:
                log.error("%s", exc)
                with contextlib.suppress(HsmsError):
                    await conn.send(SecsMessage(9, 9, False, B(exc.header)))
            except HsmsError as exc:
                log.warning("%s not delivered: %s", msg.sf, exc)

    # =================================================== communication state
    def _communicating(self) -> bool:
        return self.comm is CommState.COMMUNICATING

    def _set_comm(self, state: CommState) -> None:
        if state is self.comm:
            return
        log.info("communication state %s -> %s", self.comm.value, state.value)
        self.comm = state
        if state is CommState.COMMUNICATING:
            self._comm_established.set()
        else:
            self._comm_established.clear()

    def _on_hsms_state(self, conn: HsmsConnection, state: State) -> None:
        if state is State.SELECTED:
            self._set_comm(CommState.WAIT_CRA)
            if self.cfg.send_s1f13:
                self._comm_task = asyncio.create_task(self._establish_comm(conn), name="gem-s1f13")
        else:
            self._set_comm(CommState.WAIT_CRA)
            if self._comm_task:
                self._comm_task.cancel()

    async def _establish_comm(self, conn: HsmsConnection) -> None:
        """E30 4.2.1: equipment sends S1F13 until it gets COMMACK 0."""
        while not self._communicating() and not conn.closed:
            try:
                reply = await conn.request(SecsMessage(1, 13, True, L(A(MDLN), A(SOFTREV))))
                body = reply.body
                if (
                    reply.function == 14
                    and isinstance(body, L)
                    and isinstance(body[0], B)
                    and body[0].first == 0
                ):
                    self._set_comm(CommState.COMMUNICATING)
                    return
            except HsmsError as exc:
                log.info("S1F13 attempt failed: %s", exc)
            if self._communicating():  # host's own S1F13 may have arrived meanwhile
                return
            self._set_comm(CommState.WAIT_DELAY)
            delay = self.vars.ecs[EC.ESTABLISH_COMM_TIMEOUT].value
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._comm_established.wait(), delay)

    # ========================================================= operator panel
    async def operator_online(self) -> ControlState:
        """Operator turns the ON-LINE switch: E30 transition 4 -> S1F1 -> 5 or 6."""
        if self.control.operator_online():
            answered = False
            conn = self.connection
            if conn is not None and self.comm is CommState.COMMUNICATING:
                try:
                    reply = await conn.request(SecsMessage(1, 1, True))
                    answered = reply.function == 2
                except HsmsError as exc:
                    log.info("ATTEMPT ON-LINE: %s", exc)
            self.control.attempt_result(answered)
        return self.control.state

    def operator_offline(self) -> None:
        self.control.operator_offline()

    def operator_set_remote(self, remote: bool) -> None:
        self.control.operator_set_remote(remote)

    def _on_control_transition(self, old: ControlState, new: ControlState) -> None:
        if new is ControlState.ONLINE_LOCAL:
            self.collection_event(CE.CONTROL_STATE_LOCAL)
        elif new is ControlState.ONLINE_REMOTE:
            self.collection_event(CE.CONTROL_STATE_REMOTE)
        elif old.online:
            # Reported as the last message before going off-line.
            self.collection_event(CE.EQUIPMENT_OFFLINE, force=True)
