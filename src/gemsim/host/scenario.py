"""Scripted host scenario: bring the tool on-line, configure it, run a lot.

The scenario is what a cell controller does at tool start-up plus one lot. It
also deliberately sends a few requests that must be refused, so the trace shows
the equipment's acknowledge codes, and it handles one injected fault.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..gem.ids import ALID, CE, DV, EC, SV, ControlState
from ..secs import F4
from ..trace import SmlTrace
from .host import GemHost, HostEvent, TransactionAborted

log = logging.getLogger(__name__)

REPORTS: dict[int, list[int]] = {
    1: [SV.CONTROL_STATE, SV.PROCESS_STATE, SV.PREVIOUS_PROCESS_STATE, SV.PP_EXEC_NAME],
    2: [SV.LOT_ID, DV.EVENT_WAFER_NO, SV.WAFERS_PROCESSED],
    3: [DV.EVENT_STEP_NO, DV.EVENT_STEP_NAME, SV.CHAMBER_PRESSURE, SV.CHUCK_TEMPERATURE],
    4: [SV.LOT_ID, DV.LOT_RESULT_WAFERS],
    5: [DV.ALARM_ID, DV.ALARM_TEXT, SV.CHAMBER_PRESSURE, SV.CHUCK_TEMPERATURE],
    6: [DV.HELD_REASON, SV.SEQ_STATE, SV.WAFER_NO],
}

LINKS: dict[int, list[int]] = {
    CE.CONTROL_STATE_LOCAL: [1],
    CE.CONTROL_STATE_REMOTE: [1],
    CE.EQUIPMENT_OFFLINE: [1],
    CE.PROCESSING_STATE_CHANGE: [1],
    CE.PP_SELECTED: [1],
    CE.LOT_STARTED: [2],
    CE.WAFER_LOADED: [2],
    CE.PUMPDOWN_COMPLETE: [3],
    CE.STEP_STARTED: [3],
    CE.STEP_COMPLETED: [3],
    CE.WAFER_PROCESSED: [2],
    CE.VENT_COMPLETE: [2],
    CE.WAFER_UNLOADED: [2],
    CE.LOT_COMPLETED: [4],
    CE.LOT_STOPPED: [4],
    CE.LOT_ABORTED: [4],
    CE.ALARM_SET: [5],
    CE.ALARM_CLEARED: [5],
    CE.PROCESS_HELD: [6],
    CE.PROCESS_RESUMED: [6],
}


@dataclass
class Hooks:
    """Actions outside the host's reach, supplied by the demo harness.

    operator_online / operator_remote stand for an operator at the tool's panel;
    inject_fault / clear_fault stand for a maintenance laptop on the controller.
    """

    operator_online: Callable[[], Awaitable[None]] | None = None
    operator_remote: Callable[[], Awaitable[None]] | None = None
    inject_fault: Callable[[], Awaitable[None]] | None = None
    clear_fault: Callable[[], Awaitable[None]] | None = None


class ScenarioError(RuntimeError):
    """The equipment answered a configuration step with an unexpected code."""


def expect(actual: object, expected: object, what: str) -> None:
    """Check a reply code. Not an ``assert``: the check must survive ``python -O``."""
    if actual != expected:
        raise ScenarioError(f"{what}: expected {expected!r}, got {actual!r}")


@dataclass
class ScenarioResult:
    lot_id: str
    wafers_requested: int
    wafers_reported: int
    alarms: list[str] = field(default_factory=list)
    hcacks: dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    ok: bool = False


async def run_lot_scenario(
    host: GemHost,
    trace: SmlTrace,
    *,
    recipe: Path,
    lot_id: str = "LOT-0001",
    wafers: int = 3,
    fault_wafer: int | None = 2,
    hooks: Hooks | None = None,
    event_timeout: float = 60.0,
) -> ScenarioResult:
    hooks = hooks or Hooks()
    result = ScenarioResult(lot_id, wafers, 0)
    t0 = time.monotonic()
    note = trace.note
    ppid = recipe.stem

    note("1. HSMS connect + Select, establish communications (S1F13)")
    await host.connect()
    await asyncio.wait_for(host.communicating.wait(), 10)
    await host.establish_communication()

    note("2. S1F1 while the tool is still OFF-LINE -> equipment must abort (S1F0)")
    try:
        await host.are_you_there()
        online = True
    except TransactionAborted:
        online = False

    if not online:
        if hooks.operator_online is None:
            raise RuntimeError("equipment is off-line and no operator hook was given")
        note("3. operator turns the ON-LINE switch -> equipment sends S1F1, host answers S1F2")
        await hooks.operator_online()

    note("4. clean slate: delete all reports (S2F33 with empty list), then define, link, enable")
    expect(await host.define_reports({}), 0, "S2F33 delete all (DRACK)")
    expect(await host.define_reports(REPORTS), 0, "S2F33 define (DRACK)")
    expect(await host.link_events(LINKS), 0, "S2F35 link (LRACK)")
    expect(await host.enable_events(), 0, "S2F37 enable all (ERACK)")
    expect(await host.enable_alarms(), 0, "S5F3 enable all (ACKC5)")

    note("5. read status variables and equipment constants, shorten pump-down timeout")
    await host.status_variables(
        [SV.CONTROL_STATE, SV.PROCESS_STATE, SV.CHAMBER_PRESSURE, SV.CHUCK_TEMPERATURE]
    )
    await host.equipment_constants([EC.PUMPDOWN_TIMEOUT, EC.BASE_PRESSURE, EC.TEMP_HIGH_LIMIT])
    expect(await host.set_equipment_constants({EC.PUMPDOWN_TIMEOUT: F4(60.0)}), 0, "S2F15 (EAC)")

    note(f"6. process program download {ppid} (S7F1/S7F3), directory (S7F19), upload check (S7F5)")
    body = recipe.read_text(encoding="ascii")
    expect(await host.pp_download(ppid, body), (0, 0), "S7F1/S7F3 (PPGNT, ACKC7)")
    expect(ppid in await host.pp_list(), True, "S7F19 directory contains the PPID")
    expect(await host.pp_upload(ppid), body, "S7F5 upload equals the download")

    status = await host.status_variables([SV.CONTROL_STATE])
    if status[0] != ControlState.ONLINE_REMOTE:
        note("7. START while ON-LINE/LOCAL -> HCACK 2 (cannot perform now)")
        result.hcacks["START (local)"] = (await host.remote_command("START", {"LOTID": lot_id}))[0]
        if hooks.operator_remote is None:
            raise RuntimeError("equipment is LOCAL and no operator hook was given")
        note("8. operator switches LOCAL -> REMOTE (CE ControlStateRemote)")
        since = host.cursor()
        await hooks.operator_remote()
        await host.wait_event(CE.CONTROL_STATE_REMOTE, since=since, timeout=event_timeout)

    note("9. negative checks: unknown command (HCACK 1), unknown PPID (HCACK 6), bad parameter (HCACK 3)")
    result.hcacks["FLUSH"] = (await host.remote_command("FLUSH"))[0]
    result.hcacks["PP-SELECT NOPE"] = (await host.remote_command("PP-SELECT", {"PPID": "NOPE"}))[0]
    result.hcacks["START WAFERS=99"] = (
        await host.remote_command("START", {"LOTID": lot_id, "WAFERS": "99"})
    )[0]

    note(f"10. PP-SELECT {ppid} -> recipe validated and written to the controller")
    result.hcacks["PP-SELECT"] = (await host.remote_command("PP-SELECT", {"PPID": ppid}))[0]

    fault_armed = asyncio.Event()
    if fault_wafer is not None and hooks.inject_fault is not None:
        inject = hooks.inject_fault

        tasks: list[asyncio.Task[None]] = []

        def arm(event: HostEvent) -> None:
            wafer = event.reports.get(2, [None, None])[1]
            if event.ceid == CE.WAFER_LOADED and wafer == fault_wafer and not fault_armed.is_set():
                fault_armed.set()
                tasks.append(asyncio.ensure_future(inject()))

        host.event_hooks.append(arm)

    note(f"11. START lot {lot_id} with {wafers} wafers")
    since, alarm_mark = host.cursor(), len(host.alarms)
    result.hcacks["START"] = (await host.remote_command("START", {"LOTID": lot_id, "WAFERS": str(wafers)}))[0]

    if fault_wafer is not None and hooks.inject_fault is not None:
        await host.wait_event(
            CE.WAFER_LOADED,
            since=since,
            timeout=event_timeout,
            where=lambda e: e.reports[2][1] == fault_wafer,
        )
        trace.note(f"12. [maintenance] vacuum leak injected on wafer {fault_wafer}; waiting for the alarm")
        alarm = await host.wait_alarm(
            ALID.VACUUM_NOT_REACHED, set_=True, since=alarm_mark, timeout=event_timeout
        )
        result.alarms.append(f"SET ALID {alarm.alid} ALCD 0x{alarm.alcd:02X} '{alarm.text}'")
        await host.wait_event(CE.PROCESS_HELD, since=since, timeout=event_timeout)
        note("13. host reads chamber pressure while HELD, technician fixes the leak")
        await host.status_variables([SV.CHAMBER_PRESSURE, SV.ALARMS_SET, SV.PROCESS_STATE])
        if hooks.clear_fault:
            await hooks.clear_fault()
        note("14. RESUME -> controller clears the latched alarm and retries the pump-down")
        result.hcacks["RESUME"] = (await host.remote_command("RESUME"))[0]
        cleared = await host.wait_alarm(
            ALID.VACUUM_NOT_REACHED, set_=False, since=alarm_mark, timeout=event_timeout
        )
        result.alarms.append(f"CLEARED ALID {cleared.alid} ALCD 0x{cleared.alcd:02X}")

    done = await host.wait_event(CE.LOT_COMPLETED, since=since, timeout=event_timeout * wafers)
    result.wafers_reported = int(done.reports[4][1])
    note(f"15. lot complete: equipment reports {result.wafers_reported} wafers; final status")
    await host.status_variables([SV.PROCESS_STATE, SV.WAFERS_PROCESSED, SV.ALARMS_SET, SV.PP_EXEC_NAME])

    note("16. host takes the tool off-line (S1F15), waits for CE EquipmentOffline, then Separate")
    since = host.cursor()
    await host.request_offline()
    await host.wait_event(CE.EQUIPMENT_OFFLINE, since=since, timeout=event_timeout)
    await host.close()

    result.elapsed_s = time.monotonic() - t0
    expected = {"FLUSH": 1, "PP-SELECT NOPE": 6, "START WAFERS=99": 3, "PP-SELECT": 0, "START": 4}
    if fault_wafer is not None and hooks.inject_fault is not None:
        expected["RESUME"] = 4
    result.ok = result.wafers_reported == wafers and all(
        result.hcacks.get(k) == v for k, v in expected.items()
    )
    return result
