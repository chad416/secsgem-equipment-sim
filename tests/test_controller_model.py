"""Controller (PLC program) unit tests: sequence, interlocks and injected faults.

These drive ``ChamberController.scan`` directly with a fixed process-time step,
so they are deterministic and independent of wall-clock timing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gemsim.controller.interface import (
    AlarmBit,
    CmdCode,
    CmdResult,
    EventCode,
    Fault,
    HeldReason,
    RecipeStep,
    SeqState,
)
from gemsim.controller.model import ATMOSPHERE_TORR, ChamberController

STEPS = [RecipeStep("HEAT", 200, 0.5, 20), RecipeStep("SOAK", 300, 0.2, 30)]
DT = 0.5


def events(c: ChamberController, since: int = 0) -> list[tuple[EventCode, int]]:
    out = []
    for i in range(since, c.event_write_idx):
        slot = i % len(c.event_codes)
        out.append((EventCode(c.event_codes[slot]), c.event_args[slot]))
    return out


def run_until(c: ChamberController, cond, limit_s: float = 2000) -> float:  # type: ignore[no-untyped-def]
    t = 0.0
    while not cond():
        c.scan(DT)
        t += DT
        if t > limit_s:
            raise AssertionError(f"condition not reached in {limit_s}s, state={c.state.name}")
    return t


@pytest.fixture
def ctl() -> ChamberController:
    c = ChamberController()
    assert c.load_recipe("R", STEPS) is CmdResult.OK
    return c


def test_full_lot_event_sequence(ctl: ChamberController) -> None:
    mark = ctl.event_write_idx
    assert ctl.command(CmdCode.START, "L1", 2) is CmdResult.OK
    run_until(ctl, lambda: ctl.state is SeqState.IDLE)
    per_wafer = lambda w: [  # noqa: E731
        (EventCode.WAFER_LOADED, w),
        (EventCode.PUMPDOWN_COMPLETE, w),
        (EventCode.STEP_STARTED, 1),
        (EventCode.STEP_COMPLETED, 1),
        (EventCode.STEP_STARTED, 2),
        (EventCode.STEP_COMPLETED, 2),
        (EventCode.WAFER_PROCESSED, w),
        (EventCode.VENT_COMPLETE, w),
        (EventCode.WAFER_UNLOADED, w),
    ]
    expected = [(EventCode.LOT_STARTED, 2), *per_wafer(1), *per_wafer(2), (EventCode.LOT_COMPLETED, 2)]
    assert events(ctl, mark) == expected
    assert ctl.wafers_done == 2 and not ctl.wafer_present and ctl.pressure == ATMOSPHERE_TORR


def test_pumpdown_time_matches_first_order_model(ctl: ChamberController) -> None:
    ctl.command(CmdCode.START, "L1", 1)
    run_until(ctl, lambda: ctl.state is SeqState.PUMPDOWN)
    t = run_until(ctl, lambda: ctl.state is not SeqState.PUMPDOWN)
    # p(t) = 0.01 + 759.99 * exp(-t/3) <= 0.05  ->  t = 3 * ln(759.99 / 0.04) = 29.6 s
    assert 29.0 <= t <= 30.5


@pytest.mark.parametrize(
    ("setup", "code", "args", "expected"),
    [
        ("no_recipe", CmdCode.START, ("L1", 1), CmdResult.BAD_STATE),
        ("idle", CmdCode.START, ("", 1), CmdResult.BAD_ARG),
        ("idle", CmdCode.START, ("L1", 26), CmdResult.BAD_ARG),
        ("idle", CmdCode.PAUSE, (), CmdResult.BAD_STATE),
        ("idle", CmdCode.STOP, (), CmdResult.BAD_STATE),
        ("idle", CmdCode.ABORT, (), CmdResult.BAD_STATE),
        ("idle", CmdCode.RESUME, (), CmdResult.BAD_STATE),
        ("running", CmdCode.START, ("L2", 1), CmdResult.ALREADY),
        ("running", CmdCode.RESUME, (), CmdResult.ALREADY),
        ("wafer_present", CmdCode.START, ("L1", 1), CmdResult.INTERLOCK),
    ],
)
def test_command_results(setup: str, code: CmdCode, args: tuple[object, ...], expected: CmdResult) -> None:
    c = ChamberController()
    if setup != "no_recipe":
        c.load_recipe("R", STEPS)
    if setup == "running":
        c.command(CmdCode.START, "L1", 1)
        run_until(c, lambda: c.state is SeqState.PROCESSING)
    if setup == "wafer_present":
        c.wafer_present = True
    assert c.command(code, *args) is expected  # type: ignore[arg-type]


def test_recipe_validation() -> None:
    c = ChamberController()
    assert c.load_recipe("R", []) is CmdResult.BAD_ARG
    assert c.load_recipe("R", [RecipeStep("X", 460, 0.5, 10)]) is CmdResult.BAD_ARG  # above high limit
    assert c.load_recipe("R", [RecipeStep("X", 200, 0, 10)]) is CmdResult.BAD_ARG
    assert c.load_recipe("R", STEPS) is CmdResult.OK
    c.command(CmdCode.START, "L1", 1)
    assert c.load_recipe("R2", STEPS) is CmdResult.BAD_STATE  # not while running


def test_vacuum_leak_times_out_and_resume_retries(ctl: ChamberController) -> None:
    ctl.faults = Fault.VACUUM_LEAK
    ctl.command(CmdCode.START, "L1", 1)
    run_until(ctl, lambda: ctl.state is SeqState.HELD)
    assert ctl.alarm_active(AlarmBit.VACUUM_NOT_REACHED)
    assert ctl.held_reason is HeldReason.FAULT and ctl.pressure > 1.0
    ctl.faults = Fault.NONE
    assert ctl.command(CmdCode.RESUME) is CmdResult.OK
    assert ctl.alarm_word == 0 and ctl.state is SeqState.PUMPDOWN
    run_until(ctl, lambda: ctl.state is SeqState.IDLE)
    assert ctl.wafers_done == 1


def test_leak_not_fixed_raises_alarm_again(ctl: ChamberController) -> None:
    ctl.faults = Fault.VACUUM_LEAK
    ctl.command(CmdCode.START, "L1", 1)
    run_until(ctl, lambda: ctl.state is SeqState.HELD)
    ctl.command(CmdCode.RESUME)
    run_until(ctl, lambda: ctl.state is SeqState.HELD)
    assert ctl.alarm_active(AlarmBit.VACUUM_NOT_REACHED)


def test_robot_stall_raises_robot_timeout(ctl: ChamberController) -> None:
    ctl.faults = Fault.ROBOT_STALL
    ctl.command(CmdCode.START, "L1", 1)
    t = run_until(ctl, lambda: ctl.state is SeqState.HELD)
    assert ctl.alarm_active(AlarmBit.ROBOT_TIMEOUT)
    assert t == pytest.approx(ctl.params.robot_timeout_s, abs=1.0)
    # abort while the wafer is still on the robot: nothing in the chamber -> lot aborted
    assert ctl.command(CmdCode.ABORT) is CmdResult.OK
    assert ctl.state is SeqState.IDLE and events(ctl)[-1] == (EventCode.LOT_ABORTED, 0)


def test_heater_runaway_trips_interlock_and_abort_recovers(ctl: ChamberController) -> None:
    ctl.command(CmdCode.START, "L1", 1)
    run_until(ctl, lambda: ctl.state is SeqState.PROCESSING)
    ctl.faults = Fault.HEATER_RUNAWAY
    run_until(ctl, lambda: ctl.state is SeqState.HELD)
    assert ctl.alarm_active(AlarmBit.OVER_TEMPERATURE)
    assert ctl.temperature > ctl.params.temp_high_limit_c
    # while hot, RESUME is refused by the interlock
    assert ctl.command(CmdCode.RESUME) is CmdResult.INTERLOCK
    ctl.faults = Fault.NONE
    assert ctl.command(CmdCode.ABORT) is CmdResult.OK
    run_until(ctl, lambda: ctl.state is SeqState.IDLE)
    assert events(ctl)[-1] == (EventCode.LOT_ABORTED, 0)
    assert not ctl.wafer_present
    # alarm stays latched after the abort and blocks the next lot until reset
    assert ctl.alarm_active(AlarmBit.OVER_TEMPERATURE)
    assert ctl.command(CmdCode.START, "L2", 1) is CmdResult.INTERLOCK
    assert ctl.command(CmdCode.RESUME) is CmdResult.OK  # alarm reset in IDLE (chuck has cooled)
    assert ctl.alarm_word == 0
    assert ctl.command(CmdCode.START, "L2", 1) is CmdResult.OK


def test_pause_during_robot_move_takes_effect_after_move(ctl: ChamberController) -> None:
    ctl.command(CmdCode.START, "L1", 1)
    assert ctl.state is SeqState.LOADING
    assert ctl.command(CmdCode.PAUSE) is CmdResult.OK
    assert ctl.state is SeqState.LOADING  # robot move is not interrupted
    assert ctl.command(CmdCode.PAUSE) is CmdResult.ALREADY
    run_until(ctl, lambda: ctl.state is SeqState.HELD)
    assert ctl.wafer_present and ctl.held_reason is HeldReason.PAUSED
    assert ctl.command(CmdCode.RESUME) is CmdResult.OK
    assert ctl.state is SeqState.PUMPDOWN


def test_pause_freezes_step_timer(ctl: ChamberController) -> None:
    ctl.command(CmdCode.START, "L1", 1)
    run_until(ctl, lambda: ctl.state is SeqState.PROCESSING and ctl.step_time_left < 15)
    ctl.command(CmdCode.PAUSE)
    left = ctl.step_time_left
    for _ in range(100):
        ctl.scan(DT)
    assert ctl.step_time_left == left
    ctl.command(CmdCode.RESUME)
    ctl.scan(DT)
    assert ctl.step_time_left == left - DT


def test_stop_finishes_current_wafer(ctl: ChamberController) -> None:
    ctl.command(CmdCode.START, "L1", 3)
    run_until(ctl, lambda: ctl.state is SeqState.PROCESSING)
    assert ctl.command(CmdCode.STOP) is CmdResult.OK
    assert ctl.command(CmdCode.STOP) is CmdResult.ALREADY
    run_until(ctl, lambda: ctl.state is SeqState.IDLE)
    assert events(ctl)[-1] == (EventCode.LOT_STOPPED, 1)


def test_abort_during_processing_vents_and_unloads(ctl: ChamberController) -> None:
    ctl.command(CmdCode.START, "L1", 2)
    run_until(ctl, lambda: ctl.state is SeqState.PROCESSING)
    assert ctl.command(CmdCode.ABORT) is CmdResult.OK
    assert ctl.state is SeqState.VENTING and ctl.aborting
    run_until(ctl, lambda: ctl.state is SeqState.IDLE)
    tail = [e for e, _ in events(ctl)[-3:]]
    assert tail == [EventCode.VENT_COMPLETE, EventCode.WAFER_UNLOADED, EventCode.LOT_ABORTED]


def test_event_ring_wraps(ctl: ChamberController) -> None:
    for _ in range(200):
        ctl._emit(EventCode.RESUMED)
    assert ctl.event_write_idx == 201  # +1 for RECIPE_LOADED
    assert all(code == EventCode.RESUMED for code in ctl.event_codes)


def test_gem_layer_does_not_import_the_controller_model() -> None:
    """The GEM side may use the interface contract and the link, never the PLC model."""
    gem_dir = Path(__file__).resolve().parents[1] / "src" / "gemsim" / "gem"
    for path in gem_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.endswith("controller.model"), f"{path.name} imports the model"
