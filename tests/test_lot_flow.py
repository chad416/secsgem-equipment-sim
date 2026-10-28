"""GEM-level lot flow over a DirectLink controller (no OPC UA)."""

from __future__ import annotations

from gemsim.controller.interface import Fault
from gemsim.gem.ids import ALID, CE, ProcessState

from .conftest import Bench


async def test_full_lot_reports_every_wafer(ready: Bench) -> None:
    host = ready.host
    since = host.cursor()
    hcack, cpacks = await host.remote_command("START", {"LOTID": "LOT-T1", "WAFERS": "2"})
    assert (hcack, cpacks) == (4, [])
    done = await host.wait_event(CE.LOT_COMPLETED, since=since, timeout=30)
    assert done.reports[4] == ["LOT-T1", 2]
    loaded = [e.reports[2][1] for e in host.events[since:] if e.ceid == CE.WAFER_LOADED]
    assert loaded == [1, 2]
    steps = [(e.reports[3][0], e.reports[3][1]) for e in host.events[since:] if e.ceid == CE.STEP_STARTED]
    assert steps == [(1, "HEAT"), (2, "SOAK"), (1, "HEAT"), (2, "SOAK")]
    states = [e.reports[1][1] for e in host.events[since:] if e.ceid == CE.PROCESSING_STATE_CHANGE]
    assert states == [ProcessState.EXECUTING, ProcessState.READY]


async def test_vacuum_fault_raises_alarm_and_resume_recovers(ready: Bench) -> None:
    host, link = ready.host, ready.link
    await link.inject(Fault.VACUUM_LEAK)
    since, a0 = host.cursor(), len(host.alarms)
    assert (await host.remote_command("START", {"LOTID": "LOT-F1"}))[0] == 4
    alarm = await host.wait_alarm(ALID.VACUUM_NOT_REACHED, set_=True, since=a0)
    assert alarm.alcd == 0x84
    held = await host.wait_event(CE.PROCESS_HELD, since=since)
    assert held.reports[6] == [2]  # HeldReason.FAULT
    # technician fixes the leak, host acknowledges with RESUME -> pump-down retried
    await link.inject(Fault.NONE)
    assert (await host.remote_command("RESUME"))[0] == 4
    await host.wait_alarm(ALID.VACUUM_NOT_REACHED, set_=False, since=a0)
    done = await host.wait_event(CE.LOT_COMPLETED, since=since, timeout=30)
    assert done.reports[4] == ["LOT-F1", 1]
