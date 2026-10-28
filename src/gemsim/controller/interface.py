"""PLC <-> GEM interface contract.

This module is the equivalent of the interface specification a controls team
hands to the equipment-software team: tag names, command codes, result codes,
event codes and alarm bits. Both the controller simulation and the GEM bridge
import it, so the two sides cannot drift apart silently.
"""

from __future__ import annotations

import enum

NAMESPACE_URI = "urn:gemsim:chamber-controller"
OBJECT = "Chamber1"
EVENT_RING_SIZE = 64
MAX_RECIPE_STEPS = 16
MAX_WAFERS_PER_LOT = 25


class SeqState(enum.IntEnum):
    IDLE = 0
    LOADING = 1  # robot: load port -> chamber
    PUMPDOWN = 2
    PROCESSING = 3
    VENTING = 4
    UNLOADING = 5  # robot: chamber -> load port
    HELD = 6  # paused by command or stopped by a fault/interlock


class HeldReason(enum.IntEnum):
    NONE = 0
    PAUSED = 1
    FAULT = 2


class CmdCode(enum.IntEnum):
    NONE = 0
    LOAD_RECIPE = 1
    START = 2
    STOP = 3
    PAUSE = 4
    RESUME = 5
    ABORT = 6


class CmdResult(enum.IntEnum):
    OK = 0
    BAD_STATE = 1  # command not allowed in current sequencer state
    ALREADY = 2  # already in the requested condition
    BAD_ARG = 3
    INTERLOCK = 4  # refused by an interlock (wafer present, alarm active, over-temp)
    NO_RESPONSE = 99  # bridge-side: controller did not acknowledge in time


class EventCode(enum.IntEnum):
    LOT_STARTED = 1
    WAFER_LOADED = 2  # arg = wafer number (1-based)
    PUMPDOWN_COMPLETE = 3  # arg = wafer number
    STEP_STARTED = 4  # arg = step number (1-based)
    STEP_COMPLETED = 5  # arg = step number
    WAFER_PROCESSED = 6  # arg = wafer number
    VENT_COMPLETE = 7  # arg = wafer number
    WAFER_UNLOADED = 8  # arg = wafer number
    LOT_COMPLETED = 9  # arg = wafers processed
    LOT_STOPPED = 10  # arg = wafers processed
    LOT_ABORTED = 11  # arg = wafers processed
    HELD = 12  # arg = HeldReason
    RESUMED = 13
    RECIPE_LOADED = 14  # arg = number of steps


class AlarmBit(enum.IntEnum):
    """Bit numbers in ``Alarms.Word`` (latched by the controller)."""

    VACUUM_NOT_REACHED = 0
    ROBOT_TIMEOUT = 1
    OVER_TEMPERATURE = 2


class Fault(enum.IntFlag):
    """Bits in ``Sim.Faults`` - written by a maintenance client to inject faults."""

    NONE = 0
    VACUUM_LEAK = 1  # pump-down stalls above base pressure
    ROBOT_STALL = 2  # robot never reports move complete
    HEATER_RUNAWAY = 4  # chuck heats at full power, ignores setpoint


# Tag paths below the controller object. Node ids are "ns=<idx>;s=Chamber1.<path>".
STATUS_TAGS: dict[str, str] = {
    # path: OPC UA variant type name
    "Seq.State": "Int32",
    "Seq.HeldReason": "Int32",
    "Seq.Aborting": "Boolean",
    "Seq.WaferNo": "Int32",
    "Seq.StepNo": "Int32",
    "Seq.StepName": "String",
    "Seq.StepTimeLeft": "Double",
    "Chamber.Pressure": "Double",
    "Chamber.PressureSP": "Double",
    "Chamber.Temperature": "Double",
    "Chamber.TemperatureSP": "Double",
    "Chamber.WaferPresent": "Boolean",
    "Robot.Busy": "Boolean",
    "Lot.Id": "String",
    "Lot.WaferCount": "Int32",
    "Lot.WafersDone": "Int32",
    "Recipe.Loaded": "String",
    "Alarms.Word": "UInt32",
    "Events.WriteIdx": "UInt32",
    "Heartbeat": "UInt32",
}

EVENT_TAGS = {"Events.Code": "Int32", "Events.Arg": "Int32"}

COMMAND_TAGS: dict[str, str] = {
    "Cmd.Code": "Int32",
    "Cmd.StrArg": "String",
    "Cmd.IntArg": "Int32",
    "Cmd.ReqSeq": "UInt32",
    "Cmd.AckSeq": "UInt32",
    "Cmd.Result": "Int32",
}

RECIPE_TAGS: dict[str, str] = {
    "RecipeIn.Name": "String",
    "RecipeIn.StepNames": "String",
    "RecipeIn.TempSP": "Double",
    "RecipeIn.PressureSP": "Double",
    "RecipeIn.Duration": "Double",
}

PARAM_TAGS: dict[str, str] = {
    "Param.PumpDownTimeout": "Double",
    "Param.BasePressure": "Double",
    "Param.TempHighLimit": "Double",
    "Param.RobotTimeout": "Double",
}

SIM_TAGS: dict[str, str] = {"Sim.Faults": "UInt32", "Sim.TimeScale": "Double"}
