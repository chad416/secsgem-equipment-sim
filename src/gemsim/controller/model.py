"""Single-wafer process chamber controller (the "PLC program").

The model is written like a PLC scan: ``scan(dt)`` is called cyclically with the
elapsed process time, reads its inputs (commands, parameters, injected faults),
advances the sequencer and the physics, and updates its outputs (status, latched
alarm word, event ring). It has no knowledge of OPC UA or SECS/GEM.

Per wafer the sequence is::

    LOADING -> PUMPDOWN -> PROCESSING (recipe steps) -> VENTING -> UNLOADING
       ^                                                              |
       +-------------------- next wafer of the lot -------------------+

Physics are deliberately simple first-order models with exact discrete updates
(stable for any scan time):

* pump-down: p -> p_floor with time constant ``PUMP_TAU``; a vacuum leak raises
  the floor above base pressure so the pump-down times out.
* process pressure: throttle valve, first order towards the step setpoint.
* chuck temperature: rate-limited ramp towards setpoint; a heater runaway fault
  heats at full rate regardless of setpoint until the over-temperature interlock
  trips the heater.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .interface import (
    EVENT_RING_SIZE,
    MAX_RECIPE_STEPS,
    MAX_WAFERS_PER_LOT,
    AlarmBit,
    CmdCode,
    CmdResult,
    EventCode,
    Fault,
    HeldReason,
    SeqState,
)

ATMOSPHERE_TORR = 760.0
AMBIENT_C = 25.0
PUMP_TAU = 3.0  # s
LEAK_FLOOR_TORR = 2.0
PUMP_FLOOR_TORR = 0.01
VENT_TAU = 2.0
VENT_DONE_TORR = 750.0
THROTTLE_TAU = 2.0
RAMP_RATE_C_S = 10.0
RUNAWAY_RATE_C_S = 15.0
OVERTEMP_RESET_HYST_C = 20.0


@dataclass(frozen=True)
class RecipeStep:
    name: str
    temp_c: float
    pressure_torr: float
    duration_s: float


@dataclass
class Params:
    """Machine parameters; the GEM layer maps these to equipment constants."""

    pumpdown_timeout_s: float = 90.0
    base_pressure_torr: float = 0.05
    temp_high_limit_c: float = 450.0
    robot_timeout_s: float = 20.0
    robot_move_s: float = 8.0


@dataclass
class _Event:
    code: EventCode
    arg: int


@dataclass
class ChamberController:
    params: Params = field(default_factory=Params)
    faults: Fault = Fault.NONE

    # --- outputs ---------------------------------------------------------
    state: SeqState = SeqState.IDLE
    held_reason: HeldReason = HeldReason.NONE
    aborting: bool = False
    pressure: float = ATMOSPHERE_TORR
    pressure_sp: float = ATMOSPHERE_TORR
    temperature: float = AMBIENT_C
    temperature_sp: float = AMBIENT_C
    wafer_present: bool = False
    robot_busy: bool = False
    lot_id: str = ""
    wafer_count: int = 0
    wafers_done: int = 0
    wafer_no: int = 0
    step_no: int = 0
    step_time_left: float = 0.0
    recipe_name: str = ""
    alarm_word: int = 0
    heartbeat: int = 0
    event_write_idx: int = 0
    event_codes: list[int] = field(default_factory=lambda: [0] * EVENT_RING_SIZE)
    event_args: list[int] = field(default_factory=lambda: [0] * EVENT_RING_SIZE)

    # --- internal --------------------------------------------------------
    recipe: tuple[RecipeStep, ...] = ()
    _phase_time: float = 0.0  # time spent in current phase (for timeouts)
    _held_from: SeqState = SeqState.IDLE
    _pause_pending: bool = False
    _stop_requested: bool = False

    # ================================================================ helpers
    @property
    def step_name(self) -> str:
        if 1 <= self.step_no <= len(self.recipe):
            return self.recipe[self.step_no - 1].name
        return ""

    def status_image(self) -> dict[str, Any]:
        """Controller outputs keyed by tag path (the PLC's output image)."""
        return {
            "Seq.State": int(self.state),
            "Seq.HeldReason": int(self.held_reason),
            "Seq.Aborting": self.aborting,
            "Seq.WaferNo": self.wafer_no,
            "Seq.StepNo": self.step_no,
            "Seq.StepName": self.step_name,
            "Seq.StepTimeLeft": round(self.step_time_left, 3),
            "Chamber.Pressure": self.pressure,
            "Chamber.PressureSP": self.pressure_sp,
            "Chamber.Temperature": self.temperature,
            "Chamber.TemperatureSP": self.temperature_sp,
            "Chamber.WaferPresent": self.wafer_present,
            "Robot.Busy": self.robot_busy,
            "Lot.Id": self.lot_id,
            "Lot.WaferCount": self.wafer_count,
            "Lot.WafersDone": self.wafers_done,
            "Recipe.Loaded": self.recipe_name,
            "Alarms.Word": self.alarm_word,
            "Events.WriteIdx": self.event_write_idx,
            "Heartbeat": self.heartbeat,
        }

    def alarm_active(self, bit: AlarmBit) -> bool:
        return bool(self.alarm_word & (1 << bit))

    def _emit(self, code: EventCode, arg: int = 0) -> None:
        slot = self.event_write_idx % EVENT_RING_SIZE
        self.event_codes[slot] = int(code)
        self.event_args[slot] = arg
        self.event_write_idx += 1

    def _enter(self, state: SeqState) -> None:
        self.state = state
        self._phase_time = 0.0

    def _raise_alarm(self, bit: AlarmBit) -> None:
        self.alarm_word |= 1 << bit
        self._hold(HeldReason.FAULT)

    def _hold(self, reason: HeldReason) -> None:
        if self.state is SeqState.HELD:
            if reason is HeldReason.FAULT and self.held_reason is not HeldReason.FAULT:
                self.held_reason = reason  # a fault while paused upgrades the hold
                self._emit(EventCode.HELD, int(reason))
            return
        self._held_from = self.state
        self.held_reason = reason
        self.state = SeqState.HELD
        self.robot_busy = False
        self._emit(EventCode.HELD, int(reason))

    # =============================================================== commands
    def load_recipe(self, name: str, steps: list[RecipeStep]) -> CmdResult:
        if self.state is not SeqState.IDLE:
            return CmdResult.BAD_STATE
        if not name or not steps or len(steps) > MAX_RECIPE_STEPS:
            return CmdResult.BAD_ARG
        for s in steps:
            if not (0 < s.pressure_torr < ATMOSPHERE_TORR and s.duration_s > 0):
                return CmdResult.BAD_ARG
            if not AMBIENT_C <= s.temp_c < self.params.temp_high_limit_c:
                return CmdResult.BAD_ARG
        self.recipe = tuple(steps)
        self.recipe_name = name
        self._emit(EventCode.RECIPE_LOADED, len(steps))
        return CmdResult.OK

    def command(self, code: CmdCode, str_arg: str = "", int_arg: int = 0) -> CmdResult:
        if code is CmdCode.START:
            return self._cmd_start(str_arg, int_arg)
        if code is CmdCode.STOP:
            if self.state is SeqState.IDLE:
                return CmdResult.BAD_STATE
            if self._stop_requested:
                return CmdResult.ALREADY
            self._stop_requested = True
            return CmdResult.OK
        if code is CmdCode.PAUSE:
            if self.state is SeqState.IDLE:
                return CmdResult.BAD_STATE
            if self.state is SeqState.HELD or self._pause_pending:
                return CmdResult.ALREADY
            if self.aborting:
                return CmdResult.BAD_STATE
            if self.state in (SeqState.LOADING, SeqState.UNLOADING):
                self._pause_pending = True  # a robot move is never interrupted
            else:
                self._hold(HeldReason.PAUSED)
            return CmdResult.OK
        if code is CmdCode.RESUME:
            return self._cmd_resume()
        if code is CmdCode.ABORT:
            return self._cmd_abort()
        return CmdResult.BAD_ARG

    def _cmd_start(self, lot_id: str, wafers: int) -> CmdResult:
        if self.state is not SeqState.IDLE:
            return CmdResult.ALREADY
        if not self.recipe:
            return CmdResult.BAD_STATE
        if not lot_id or not 1 <= wafers <= MAX_WAFERS_PER_LOT:
            return CmdResult.BAD_ARG
        if self.wafer_present or self.alarm_word:
            return CmdResult.INTERLOCK
        self.lot_id = lot_id
        self.wafer_count = wafers
        self.wafers_done = 0
        self.wafer_no = 1
        self.aborting = False
        self._stop_requested = False
        self._pause_pending = False
        self._emit(EventCode.LOT_STARTED, wafers)
        self._start_robot(SeqState.LOADING)
        return CmdResult.OK

    def _overtemp_latched(self) -> bool:
        return self.alarm_active(AlarmBit.OVER_TEMPERATURE) and (
            self.temperature > self.params.temp_high_limit_c - OVERTEMP_RESET_HYST_C
        )

    def _cmd_resume(self) -> CmdResult:
        if self.state is SeqState.IDLE:
            # In IDLE, RESUME acts as the alarm reset (an alarm raised while idle
            # would otherwise block START forever).
            if not self.alarm_word:
                return CmdResult.BAD_STATE
            if self._overtemp_latched():
                return CmdResult.INTERLOCK
            self.alarm_word = 0
            return CmdResult.OK
        if self._pause_pending:
            self._pause_pending = False
            return CmdResult.OK
        if self.state is not SeqState.HELD:
            return CmdResult.ALREADY
        if self._overtemp_latched():
            return CmdResult.INTERLOCK
        # Operator/host acknowledges: clear latched alarms and retry the held phase.
        self.alarm_word = 0
        self.held_reason = HeldReason.NONE
        target = self._held_from
        self.state = target
        self._phase_time = 0.0
        if target in (SeqState.LOADING, SeqState.UNLOADING):
            self.robot_busy = True
        if target is SeqState.PROCESSING and not self.aborting:
            self.temperature_sp = self.recipe[self.step_no - 1].temp_c  # heater back on
        self._emit(EventCode.RESUMED)
        return CmdResult.OK

    def _cmd_abort(self) -> CmdResult:
        if self.state is SeqState.IDLE:
            return CmdResult.BAD_STATE
        if self.aborting:
            return CmdResult.ALREADY
        self.aborting = True
        self._pause_pending = False
        phase = self._held_from if self.state is SeqState.HELD else self.state
        if self.state is SeqState.HELD:
            self.held_reason = HeldReason.NONE
            # An abort acknowledges the alarm; the abort path itself may re-raise it.
            self.alarm_word = 0
        if phase is SeqState.LOADING and not self.wafer_present:
            self._finish_lot(EventCode.LOT_ABORTED)
        elif phase in (SeqState.PUMPDOWN, SeqState.PROCESSING, SeqState.VENTING):
            self._enter(SeqState.VENTING)
        else:  # UNLOADING
            self._start_robot(SeqState.UNLOADING)
        self.temperature_sp = AMBIENT_C
        return CmdResult.OK

    # ================================================================== scan
    def scan(self, dt: float) -> None:
        """One controller cycle. ``dt`` is elapsed process time in seconds."""
        self.heartbeat = (self.heartbeat + 1) & 0xFFFFFFFF
        self._physics(dt)
        self._interlocks()
        if self.state is SeqState.HELD or self.state is SeqState.IDLE:
            return
        self._phase_time += dt
        handler = {
            SeqState.LOADING: self._run_loading,
            SeqState.PUMPDOWN: self._run_pumpdown,
            SeqState.PROCESSING: self._run_processing,
            SeqState.VENTING: self._run_venting,
            SeqState.UNLOADING: self._run_unloading,
        }[self.state]
        handler(dt)

    def _physics(self, dt: float) -> None:
        # pressure
        if self.state is SeqState.PUMPDOWN or (
            self.state is SeqState.HELD and self._held_from is SeqState.PUMPDOWN
        ):
            floor = LEAK_FLOOR_TORR if self.faults & Fault.VACUUM_LEAK else PUMP_FLOOR_TORR
            self.pressure = floor + (self.pressure - floor) * math.exp(-dt / PUMP_TAU)
        elif self.state is SeqState.VENTING:
            self.pressure = ATMOSPHERE_TORR + (self.pressure - ATMOSPHERE_TORR) * math.exp(-dt / VENT_TAU)
        elif self.state is SeqState.PROCESSING or (
            self.state is SeqState.HELD and self._held_from is SeqState.PROCESSING
        ):
            target = self.pressure_sp
            if self.faults & Fault.VACUUM_LEAK:
                target = max(target, LEAK_FLOOR_TORR)
            self.pressure = target + (self.pressure - target) * math.exp(-dt / THROTTLE_TAU)
        # temperature
        if self.faults & Fault.HEATER_RUNAWAY and not self.alarm_active(AlarmBit.OVER_TEMPERATURE):
            self.temperature += RUNAWAY_RATE_C_S * dt
        else:
            delta = self.temperature_sp - self.temperature
            step = RAMP_RATE_C_S * dt
            self.temperature += max(-step, min(step, delta))

    def _interlocks(self) -> None:
        if self.temperature > self.params.temp_high_limit_c and not self.alarm_active(
            AlarmBit.OVER_TEMPERATURE
        ):
            self.temperature_sp = AMBIENT_C  # heater off
            if self.state is SeqState.IDLE:
                self.alarm_word |= 1 << AlarmBit.OVER_TEMPERATURE
            else:
                self._raise_alarm(AlarmBit.OVER_TEMPERATURE)

    def _start_robot(self, state: SeqState) -> None:
        self._enter(state)
        self.robot_busy = True

    def _robot_done(self) -> bool:
        if self.faults & Fault.ROBOT_STALL:
            if self._phase_time > self.params.robot_timeout_s:
                self._raise_alarm(AlarmBit.ROBOT_TIMEOUT)
            return False
        if self._phase_time >= self.params.robot_move_s:
            self.robot_busy = False
            return True
        return False

    def _run_loading(self, dt: float) -> None:
        if not self._robot_done():
            return
        self.wafer_present = True
        self._emit(EventCode.WAFER_LOADED, self.wafer_no)
        if self._pause_pending:
            self._pause_pending = False
            self._enter(SeqState.PUMPDOWN)
            self._hold(HeldReason.PAUSED)
            return
        self._enter(SeqState.PUMPDOWN)

    def _run_pumpdown(self, dt: float) -> None:
        if self.pressure <= self.params.base_pressure_torr:
            self._emit(EventCode.PUMPDOWN_COMPLETE, self.wafer_no)
            self._start_step(1)
        elif self._phase_time > self.params.pumpdown_timeout_s:
            self._raise_alarm(AlarmBit.VACUUM_NOT_REACHED)

    def _start_step(self, number: int) -> None:
        step = self.recipe[number - 1]
        self._enter(SeqState.PROCESSING)
        self.step_no = number
        self.step_time_left = step.duration_s
        self.temperature_sp = step.temp_c
        self.pressure_sp = step.pressure_torr
        self._emit(EventCode.STEP_STARTED, number)

    def _run_processing(self, dt: float) -> None:
        self.step_time_left = max(0.0, self.step_time_left - dt)
        if self.step_time_left > 0:
            return
        self._emit(EventCode.STEP_COMPLETED, self.step_no)
        if self.step_no < len(self.recipe):
            self._start_step(self.step_no + 1)
            return
        self._emit(EventCode.WAFER_PROCESSED, self.wafer_no)
        self.step_no = 0
        self.temperature_sp = AMBIENT_C
        self.pressure_sp = ATMOSPHERE_TORR
        self._enter(SeqState.VENTING)

    def _run_venting(self, dt: float) -> None:
        if self.pressure >= VENT_DONE_TORR:
            self.pressure = ATMOSPHERE_TORR
            self._emit(EventCode.VENT_COMPLETE, self.wafer_no)
            if self.wafer_present:
                self._start_robot(SeqState.UNLOADING)
            else:
                self._finish_lot(EventCode.LOT_ABORTED)

    def _run_unloading(self, dt: float) -> None:
        if not self._robot_done():
            return
        self.wafer_present = False
        self._emit(EventCode.WAFER_UNLOADED, self.wafer_no)
        if self.aborting:
            self._finish_lot(EventCode.LOT_ABORTED)
            return
        self.wafers_done += 1
        if self.wafers_done >= self.wafer_count:
            self._finish_lot(EventCode.LOT_COMPLETED)
        elif self._stop_requested:
            self._finish_lot(EventCode.LOT_STOPPED)
        else:
            self.wafer_no += 1
            if self._pause_pending:
                self._pause_pending = False
                self._enter(SeqState.LOADING)
                self._hold(HeldReason.PAUSED)
                return
            self._start_robot(SeqState.LOADING)

    def _finish_lot(self, code: EventCode) -> None:
        self._emit(code, self.wafers_done)
        self._enter(SeqState.IDLE)
        self.aborting = False
        self._stop_requested = False
        self._pause_pending = False
        self.robot_busy = False
        self.step_no = 0
        self.step_time_left = 0.0
        self.temperature_sp = AMBIENT_C
        self.pressure_sp = ATMOSPHERE_TORR
