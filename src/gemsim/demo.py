"""One-command demo: controller (OPC UA) + GEM equipment (HSMS) + scripted host.

All three run in one process but talk over real localhost sockets: OPC UA
between controller and GEM layer, HSMS between GEM layer and host.
"""

from __future__ import annotations

import logging
import socket
import sys
from collections import Counter
from pathlib import Path
from typing import TextIO

from .controller.interface import Fault
from .controller.link import MaintenanceClient, OpcUaLink
from .controller.model import ChamberController
from .controller.opcua_server import ControllerServer
from .gem.equipment import EquipmentConfig, GemEquipment
from .host.host import GemHost
from .host.scenario import Hooks, ScenarioResult, run_lot_scenario
from .hsms import HsmsTimeouts
from .trace import SmlTrace

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECIPE = ROOT / "recipes" / "ANNEAL_STD.rcp"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def quiet_third_party_logs() -> None:
    for name in ("asyncua", "asyncua.client", "asyncua.server", "asyncua.common"):
        logging.getLogger(name).setLevel(logging.ERROR)


async def run_demo(
    *,
    out_dir: Path,
    time_scale: float = 25.0,
    wafers: int = 3,
    fault_wafer: int | None = 2,
    recipe: Path = DEFAULT_RECIPE,
    echo: TextIO | None = sys.stdout,
) -> tuple[ScenarioResult, SmlTrace]:
    out_dir.mkdir(parents=True, exist_ok=True)
    quiet_third_party_logs()
    eq_log = logging.FileHandler(out_dir / "demo_equipment.log", mode="w", encoding="utf-8")
    eq_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    eq_log.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(eq_log)
    previous_level = root.level
    root.setLevel(logging.INFO)

    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/gemsim/"
    controller = ControllerServer(ChamberController(), endpoint, time_scale=time_scale)
    link = OpcUaLink(endpoint)
    equipment = GemEquipment(link, EquipmentConfig(port=0, initial_online=False, initial_remote=False))
    maintenance = MaintenanceClient(endpoint)
    trace_path = out_dir / "demo_trace.sml"
    with trace_path.open("w", encoding="utf-8", newline="\n") as trace_file:
        trace = SmlTrace("H", trace_file, echo=echo)
        try:
            await controller.start()
            await equipment.start()
            await maintenance.connect()

            async def operator_online() -> None:
                await equipment.operator_online()

            async def operator_remote() -> None:
                equipment.operator_set_remote(True)

            async def inject() -> None:
                await maintenance.inject(Fault.VACUUM_LEAK)

            async def clear() -> None:
                await maintenance.inject(Fault.NONE)

            hooks = Hooks(operator_online, operator_remote, inject, clear)
            host = GemHost(port=equipment.port, timeouts=HsmsTimeouts(t3=10.0), trace=trace)
            trace.note(
                f"gemsim demo: controller {endpoint} (time scale x{time_scale:g}), "
                f"HSMS 127.0.0.1:{equipment.port}, recipe {recipe.name}"
            )
            result = await run_lot_scenario(
                host, trace, recipe=recipe, wafers=wafers, fault_wafer=fault_wafer, hooks=hooks
            )
        finally:
            await maintenance.close()
            await equipment.stop()
            await controller.stop()
            root.removeHandler(eq_log)
            eq_log.close()
            root.setLevel(previous_level)
    (out_dir / "demo_summary.txt").write_text(format_summary(result, trace), encoding="utf-8")
    return result, trace


def format_summary(result: ScenarioResult, trace: SmlTrace) -> str:
    counts: Counter[str] = trace.counts
    lines = [
        f"result            : {'PASS' if result.ok else 'FAIL'}",
        f"lot               : {result.lot_id}, {result.wafers_requested} wafers requested, "
        f"{result.wafers_reported} reported complete",
        f"elapsed (wall)    : {result.elapsed_s:.1f} s",
        "remote commands   : " + ", ".join(f"{k} -> HCACK {v}" for k, v in result.hcacks.items()),
        "alarms            : " + ("; ".join(result.alarms) or "none"),
        f"messages traced   : {sum(counts.values())}",
        "by type           : " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=_sort_key)),
    ]
    return "\n".join(lines) + "\n"


def _sort_key(item: tuple[str, int]) -> tuple[int, int, str]:
    name = item[0]
    if name.startswith("S") and "F" in name:
        s, f = name[1:].split("F", 1)
        if s.isdigit() and f.isdigit():
            return (int(s), int(f), name)
    return (99, 0, name)
