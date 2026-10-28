"""OPC UA server that exposes the controller's process image.

The address space mirrors how a PLC with an embedded OPC UA server (for example
an S7-1500) would publish a data block: one object ``Chamber1`` with status
tags, a command handshake, a recipe download area, machine parameters and the
event ring. Node ids are readable strings: ``ns=<idx>;s=Chamber1.Seq.State``.

Command handshake (one command in flight)::

    GEM layer                         controller (next scan)
    ---------                         ----------------------
    write Cmd.Code / StrArg / IntArg
    write Cmd.ReqSeq = n          -->  sees ReqSeq != AckSeq, executes command
                                  <--  writes Cmd.Result, then Cmd.AckSeq = n
"""

from __future__ import annotations

import logging
from typing import Any

from asyncua import Node, Server, ua

from .interface import (
    COMMAND_TAGS,
    EVENT_RING_SIZE,
    EVENT_TAGS,
    NAMESPACE_URI,
    OBJECT,
    PARAM_TAGS,
    RECIPE_TAGS,
    SIM_TAGS,
    STATUS_TAGS,
    CmdCode,
    CmdResult,
    Fault,
)
from .model import ChamberController, RecipeStep
from .runtime import ScanLoop

log = logging.getLogger(__name__)

WRITABLE = {
    "Cmd.Code",
    "Cmd.StrArg",
    "Cmd.IntArg",
    "Cmd.ReqSeq",
    *RECIPE_TAGS,
    *PARAM_TAGS,
    "Sim.Faults",
}
ARRAYS = {*EVENT_TAGS, *RECIPE_TAGS}


def variant_type(name: str) -> ua.VariantType:
    return getattr(ua.VariantType, name)  # type: ignore[no-any-return]


class ControllerServer:
    def __init__(
        self,
        controller: ChamberController,
        endpoint: str = "opc.tcp://127.0.0.1:4840/gemsim/",
        *,
        time_scale: float = 10.0,
        scan_s: float = 0.05,
    ) -> None:
        self.controller = controller
        self.endpoint = endpoint
        self.time_scale = time_scale
        self.server = Server()
        self.nodes: dict[str, Node] = {}
        self.ns = 0
        self.loop = ScanLoop(controller, time_scale=time_scale, scan_s=scan_s, io=self)
        self._written: dict[str, Any] = {}
        self._last_req = 0
        self._last_event_idx = 0

    def node_id(self, path: str) -> ua.NodeId:
        return ua.NodeId(f"{OBJECT}.{path}", self.ns)

    async def start(self) -> None:
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("gemsim chamber controller (simulated PLC)")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await self.server.register_namespace(NAMESPACE_URI)
        obj = await self.server.nodes.objects.add_object(ua.NodeId(OBJECT, self.ns), OBJECT)
        c = self.controller
        initial: dict[str, Any] = {
            **c.status_image(),
            "Events.WriteIdx": 0,
            "Events.Code": [0] * EVENT_RING_SIZE,
            "Events.Arg": [0] * EVENT_RING_SIZE,
            "Cmd.Code": 0,
            "Cmd.StrArg": "",
            "Cmd.IntArg": 0,
            "Cmd.ReqSeq": 0,
            "Cmd.AckSeq": 0,
            "Cmd.Result": 0,
            "RecipeIn.Name": "",
            "RecipeIn.StepNames": [],
            "RecipeIn.TempSP": [],
            "RecipeIn.PressureSP": [],
            "RecipeIn.Duration": [],
            "Param.PumpDownTimeout": c.params.pumpdown_timeout_s,
            "Param.BasePressure": c.params.base_pressure_torr,
            "Param.TempHighLimit": c.params.temp_high_limit_c,
            "Param.RobotTimeout": c.params.robot_timeout_s,
            "Sim.Faults": int(c.faults),
            "Sim.TimeScale": self.time_scale,
        }
        tags = {**STATUS_TAGS, **EVENT_TAGS, **COMMAND_TAGS, **RECIPE_TAGS, **PARAM_TAGS, **SIM_TAGS}
        for path, type_name in tags.items():
            vt = variant_type(type_name)
            value = initial[path]
            if path in ARRAYS:
                value = ua.Variant(value, vt, is_array=True)
            node = await obj.add_variable(self.node_id(path), path, value, vt)
            if path in WRITABLE:
                await node.set_writable()
            self.nodes[path] = node
        await self.server.start()
        self._written = {k: v for k, v in initial.items() if k not in ARRAYS}
        self.loop.start()
        log.info("controller OPC UA server on %s (ns=%d, time scale x%g)", self.endpoint, self.ns, self.time_scale)

    async def stop(self) -> None:
        await self.loop.stop()
        await self.server.stop()

    # ------------------------------------------------------------- ScanIO
    def _read(self, path: str) -> Any:
        return self.server.read_attribute_value(self.node_id(path)).Value.Value

    async def _write(self, path: str, value: Any, *, force: bool = False) -> None:
        if not force and self._written.get(path) == value:
            return
        vt = variant_type({**STATUS_TAGS, **EVENT_TAGS, **COMMAND_TAGS}[path])
        variant = ua.Variant(value, vt, is_array=path in ARRAYS)
        await self.server.write_attribute_value(self.node_id(path), ua.DataValue(variant))
        self._written[path] = value

    async def read_inputs(self, c: ChamberController) -> None:
        c.params.pumpdown_timeout_s = float(self._read("Param.PumpDownTimeout"))
        c.params.base_pressure_torr = float(self._read("Param.BasePressure"))
        c.params.temp_high_limit_c = float(self._read("Param.TempHighLimit"))
        c.params.robot_timeout_s = float(self._read("Param.RobotTimeout"))
        c.faults = Fault(int(self._read("Sim.Faults")) & 0x7)
        req = int(self._read("Cmd.ReqSeq"))
        if req != self._last_req:
            self._last_req = req
            result = self._execute()
            log.info("command seq=%d -> %s", req, result.name)
            await self._write("Cmd.Result", int(result), force=True)
            await self._write("Cmd.AckSeq", req, force=True)

    def _execute(self) -> CmdResult:
        c = self.controller
        try:
            code = CmdCode(int(self._read("Cmd.Code")))
        except ValueError:
            return CmdResult.BAD_ARG
        if code is CmdCode.LOAD_RECIPE:
            names = list(self._read("RecipeIn.StepNames") or [])
            temps = list(self._read("RecipeIn.TempSP") or [])
            press = list(self._read("RecipeIn.PressureSP") or [])
            durs = list(self._read("RecipeIn.Duration") or [])
            if not len(names) == len(temps) == len(press) == len(durs):
                return CmdResult.BAD_ARG
            steps = [RecipeStep(n, float(t), float(p), float(d)) for n, t, p, d in zip(names, temps, press, durs)]
            return c.load_recipe(str(self._read("RecipeIn.Name")), steps)
        return c.command(code, str(self._read("Cmd.StrArg")), int(self._read("Cmd.IntArg")))

    async def write_outputs(self, c: ChamberController) -> None:
        for path, value in c.status_image().items():
            if path != "Events.WriteIdx":
                await self._write(path, value)
        if c.event_write_idx != self._last_event_idx:
            # ring first, index last: a reader that sees the new index finds the data
            await self._write("Events.Code", list(c.event_codes), force=True)
            await self._write("Events.Arg", list(c.event_args), force=True)
            await self._write("Events.WriteIdx", c.event_write_idx)
            self._last_event_idx = c.event_write_idx
