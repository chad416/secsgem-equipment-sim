"""Shared fixtures: equipment over a DirectLink controller and a connected host."""

from __future__ import annotations

import asyncio
import dataclasses
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest

from gemsim.controller.link import DirectLink
from gemsim.gem.equipment import EquipmentConfig, GemEquipment
from gemsim.gem.ids import CE, DV, SV
from gemsim.host.host import GemHost
from gemsim.hsms import HsmsTimeouts

RECIPE = """# test recipe
STEP HEAT TEMP=200 PRESSURE=0.5 TIME=20
STEP SOAK TEMP=300 PRESSURE=0.2 TIME=30
"""

FAST = HsmsTimeouts(t3=5.0, t5=0.2, t6=2.0, t7=2.0, t8=1.0)


async def wait_until(cond: Callable[[], bool], timeout: float = 10.0, what: str = "condition") -> None:
    """Poll ``cond`` against a wall-clock deadline.

    Never count iterations of ``asyncio.sleep``: on Windows its real duration
    depends on the system timer resolution, which other processes change.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.01)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Bench:
    equipment: GemEquipment
    link: DirectLink
    host: GemHost


async def make_bench(*, online: bool = True, remote: bool = True, time_scale: float = 100.0) -> Bench:
    link = DirectLink(time_scale=time_scale)
    # fresh copy: tests may shorten timers and must not leak that into other tests
    timeouts = dataclasses.replace(FAST)
    cfg = EquipmentConfig(port=0, timeouts=timeouts, initial_online=online, initial_remote=remote)
    equipment = GemEquipment(link, cfg)
    await equipment.start()
    host = GemHost(port=equipment.port, timeouts=dataclasses.replace(FAST))
    await host.connect()
    await asyncio.wait_for(host.communicating.wait(), 5)
    # wait until the equipment side has processed the S1F14
    await wait_until(lambda: equipment.comm.name == "COMMUNICATING", 5, "COMMUNICATING")
    return Bench(equipment, link, host)


async def configure_reports(host: GemHost) -> None:
    assert (
        await host.define_reports(
            {
                1: [SV.CONTROL_STATE, SV.PROCESS_STATE, SV.PREVIOUS_PROCESS_STATE, SV.PP_EXEC_NAME],
                2: [SV.LOT_ID, DV.EVENT_WAFER_NO],
                3: [DV.EVENT_STEP_NO, DV.EVENT_STEP_NAME, SV.CHAMBER_PRESSURE],
                4: [SV.LOT_ID, DV.LOT_RESULT_WAFERS],
                5: [DV.ALARM_ID, DV.ALARM_TEXT],
                6: [DV.HELD_REASON],
            }
        )
        == 0
    )
    assert (
        await host.link_events(
            {
                CE.PROCESSING_STATE_CHANGE: [1],
                CE.PP_SELECTED: [1],
                CE.CONTROL_STATE_LOCAL: [1],
                CE.CONTROL_STATE_REMOTE: [1],
                CE.WAFER_LOADED: [2],
                CE.WAFER_UNLOADED: [2],
                CE.STEP_STARTED: [3],
                CE.STEP_COMPLETED: [3],
                CE.LOT_COMPLETED: [4],
                CE.LOT_STOPPED: [4],
                CE.LOT_ABORTED: [4],
                CE.ALARM_SET: [5],
                CE.ALARM_CLEARED: [5],
                CE.PROCESS_HELD: [6],
            }
        )
        == 0
    )
    assert await host.enable_events() == 0
    assert await host.enable_alarms() == 0


@pytest.fixture
async def bench() -> AsyncIterator[Bench]:
    b = await make_bench()
    try:
        yield b
    finally:
        await b.host.close()
        await b.equipment.stop()


@pytest.fixture
async def ready(bench: Bench) -> Bench:
    """Bench with reports configured and the test recipe downloaded + selected."""
    await configure_reports(bench.host)
    assert await bench.host.pp_download("TEST_RCP", RECIPE) == (0, 0)
    hcack, _ = await bench.host.remote_command("PP-SELECT", {"PPID": "TEST_RCP"})
    assert hcack == 0
    return bench
