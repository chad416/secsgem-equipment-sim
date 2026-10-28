"""S2F41 remote commands: every HCACK / CPACK path the equipment can answer."""

from __future__ import annotations

import pytest

from gemsim.controller.interface import SeqState
from gemsim.gem.ids import CE, ProcessState
from gemsim.gem.remote_commands import CpAck, HcAck
from gemsim.secs import F4, U4

from .conftest import RECIPE, Bench, make_bench, wait_until


async def wait_seq(bench: Bench, state: SeqState, timeout: float = 10) -> None:
    await wait_until(lambda: bench.link.status.get("Seq.State") == state, timeout, f"sequencer {state.name}")


async def test_unknown_command(bench: Bench) -> None:
    assert await bench.host.remote_command("FLUSH") == (HcAck.INVALID_COMMAND, [])


@pytest.mark.parametrize(
    ("params", "cpacks"),
    [
        ({"LOTID": "L1", "COLOR": "red"}, [("COLOR", CpAck.NAME_UNKNOWN)]),
        ({"LOTID": "L1", "WAFERS": "0"}, [("WAFERS", CpAck.ILLEGAL_VALUE)]),
        ({"LOTID": "L1", "WAFERS": "26"}, [("WAFERS", CpAck.ILLEGAL_VALUE)]),
        ({"LOTID": "L1", "WAFERS": "two"}, [("WAFERS", CpAck.ILLEGAL_FORMAT)]),
        ({"LOTID": "L1", "WAFERS": F4(2.0)}, [("WAFERS", CpAck.ILLEGAL_FORMAT)]),
        ({"LOTID": U4(7)}, [("LOTID", CpAck.ILLEGAL_FORMAT)]),
        ({"WAFERS": "2"}, [("LOTID", CpAck.ILLEGAL_VALUE)]),  # missing required parameter
    ],
)
async def test_start_parameter_errors(ready: Bench, params, cpacks) -> None:  # type: ignore[no-untyped-def]
    assert await ready.host.remote_command("START", params) == (HcAck.PARAMETER_INVALID, cpacks)


async def test_numeric_wafer_count_is_accepted(ready: Bench) -> None:
    assert (await ready.host.remote_command("START", {"LOTID": "L1", "WAFERS": U4(1)}))[
        0
    ] == HcAck.ACK_FINISH_LATER


async def test_parameters_on_parameterless_command(ready: Bench) -> None:
    assert await ready.host.remote_command("PAUSE", {"NOW": "1"}) == (
        HcAck.PARAMETER_INVALID,
        [("NOW", CpAck.NAME_UNKNOWN)],
    )


async def test_local_mode_refuses_commands() -> None:
    b = await make_bench(remote=False)
    try:
        await b.host.pp_download("P", RECIPE)
        assert await b.host.remote_command("PP-SELECT", {"PPID": "P"}) == (HcAck.CANNOT_PERFORM_NOW, [])
    finally:
        await b.host.close()
        await b.equipment.stop()


async def test_start_without_selected_recipe(bench: Bench) -> None:
    assert await bench.host.remote_command("START", {"LOTID": "L1"}) == (HcAck.CANNOT_PERFORM_NOW, [])


async def test_pp_select_unknown_and_invalid(bench: Bench) -> None:
    host = bench.host
    assert await host.remote_command("PP-SELECT", {"PPID": "NOPE"}) == (HcAck.NO_SUCH_OBJECT, [])
    await host.pp_download("BAD", "STEP X TEMP=hot PRESSURE=1 TIME=1\n")
    assert await host.remote_command("PP-SELECT", {"PPID": "BAD"}) == (
        HcAck.PARAMETER_INVALID,
        [("PPID", CpAck.ILLEGAL_VALUE)],
    )
    # parses, but the controller refuses a setpoint above the temperature limit
    await host.pp_download("HOT", "STEP X TEMP=480 PRESSURE=1 TIME=1\n")
    assert await host.remote_command("PP-SELECT", {"PPID": "HOT"}) == (
        HcAck.PARAMETER_INVALID,
        [("PPID", CpAck.ILLEGAL_VALUE)],
    )


async def test_sequencer_command_matrix(ready: Bench) -> None:
    host, eq = ready.host, ready.equipment
    assert eq.process_state is ProcessState.READY
    # idle: nothing to pause, resume, stop or abort
    for rcmd in ("PAUSE", "RESUME", "STOP", "ABORT"):
        assert (await host.remote_command(rcmd))[0] == HcAck.CANNOT_PERFORM_NOW, rcmd
    since = host.cursor()
    assert (await host.remote_command("START", {"LOTID": "L1", "WAFERS": "2"}))[0] == HcAck.ACK_FINISH_LATER
    await wait_seq(ready, SeqState.PROCESSING)
    assert (await host.remote_command("START", {"LOTID": "L2"}))[0] == HcAck.ALREADY_IN_CONDITION
    assert (await host.remote_command("RESUME"))[0] == HcAck.ALREADY_IN_CONDITION
    assert (await host.remote_command("PP-SELECT", {"PPID": "TEST_RCP"}))[0] == HcAck.CANNOT_PERFORM_NOW
    assert (await host.remote_command("PAUSE"))[0] == HcAck.ACK_FINISH_LATER
    held = await host.wait_event(CE.PROCESS_HELD, since=since)
    assert held.reports[6] == [1]  # HeldReason.PAUSED
    assert eq.process_state is ProcessState.PAUSE
    assert (await host.remote_command("PAUSE"))[0] == HcAck.ALREADY_IN_CONDITION
    assert (await host.remote_command("RESUME"))[0] == HcAck.ACK_FINISH_LATER
    assert (await host.remote_command("STOP"))[0] == HcAck.ACK_FINISH_LATER
    stopped = await host.wait_event(CE.LOT_STOPPED, since=since, timeout=30)
    assert stopped.reports[4] == ["L1", 1]
    assert eq.process_state is ProcessState.READY


async def test_abort_reports_lot_aborted(ready: Bench) -> None:
    host = ready.host
    since = host.cursor()
    await host.remote_command("START", {"LOTID": "LA", "WAFERS": "3"})
    await wait_seq(ready, SeqState.PROCESSING)
    assert (await host.remote_command("ABORT"))[0] == HcAck.ACK_FINISH_LATER
    assert (await host.remote_command("ABORT"))[0] == HcAck.ALREADY_IN_CONDITION
    ev = await host.wait_event(CE.LOT_ABORTED, since=since, timeout=30)
    assert ev.reports[4] == ["LA", 0]


async def test_recipe_locked_while_lot_runs(ready: Bench) -> None:
    host = ready.host
    await host.remote_command("START", {"LOTID": "L1"})
    assert await host.pp_download("TEST_RCP", RECIPE) == (4, -1)  # PPGNT 4: busy
    assert await host.pp_delete(["TEST_RCP"]) == 1  # ACKC7 1: permission not granted
