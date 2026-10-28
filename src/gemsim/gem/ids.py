"""Equipment identifiers: SVIDs, DVIDs, ECIDs, CEIDs and ALIDs.

All variable IDs (SV, DV, EC) share one VID space, as required for report
definitions (S2F33 accepts any of them).
"""

from __future__ import annotations

import enum

MDLN = "GEMSIM-PC1"  # model name: simulated single-wafer process chamber
SOFTREV = "0.1.0"


class SV(enum.IntEnum):
    CLOCK = 1001
    CONTROL_STATE = 1002
    PREVIOUS_CONTROL_STATE = 1003
    EVENTS_ENABLED = 1004
    ALARMS_ENABLED = 1005
    ALARMS_SET = 1006
    PP_EXEC_NAME = 1007
    PROCESS_STATE = 1008
    PREVIOUS_PROCESS_STATE = 1009
    CHAMBER_PRESSURE = 1020
    CHAMBER_PRESSURE_SP = 1021
    CHUCK_TEMPERATURE = 1022
    CHUCK_TEMPERATURE_SP = 1023
    WAFER_PRESENT = 1024
    ROBOT_BUSY = 1025
    SEQ_STATE = 1026
    STEP_NO = 1027
    STEP_NAME = 1028
    STEP_TIME_LEFT = 1029
    LOT_ID = 1030
    LOT_WAFER_COUNT = 1031
    WAFERS_PROCESSED = 1032
    WAFER_NO = 1033
    CONTROLLER_LINK_UP = 1040
    MDLN = 1041
    SOFTREV = 1042


class DV(enum.IntEnum):
    """Data variables: only valid in the context of specific collection events."""

    EVENT_WAFER_NO = 2001
    EVENT_STEP_NO = 2002
    EVENT_STEP_NAME = 2003
    ALARM_ID = 2004
    ALARM_TEXT = 2005
    LOT_RESULT_WAFERS = 2006
    HELD_REASON = 2007
    PP_SELECTED = 2008


class EC(enum.IntEnum):
    ESTABLISH_COMM_TIMEOUT = 3001
    TIME_FORMAT = 3002
    PUMPDOWN_TIMEOUT = 3003
    BASE_PRESSURE = 3004
    TEMP_HIGH_LIMIT = 3005
    ROBOT_TIMEOUT = 3006


class CE(enum.IntEnum):
    EQUIPMENT_OFFLINE = 100
    CONTROL_STATE_LOCAL = 101
    CONTROL_STATE_REMOTE = 102
    PROCESSING_STATE_CHANGE = 110
    PP_SELECTED = 120
    LOT_STARTED = 130
    LOT_COMPLETED = 131
    LOT_STOPPED = 132
    LOT_ABORTED = 133
    WAFER_LOADED = 140
    PUMPDOWN_COMPLETE = 141
    STEP_STARTED = 142
    STEP_COMPLETED = 143
    WAFER_PROCESSED = 144
    VENT_COMPLETE = 145
    WAFER_UNLOADED = 146
    ALARM_SET = 150
    ALARM_CLEARED = 151
    PROCESS_HELD = 160
    PROCESS_RESUMED = 161


class ALID(enum.IntEnum):
    VACUUM_NOT_REACHED = 1
    ROBOT_TIMEOUT = 2
    OVER_TEMPERATURE = 3
    CONTROLLER_LINK_LOST = 10


class ControlState(enum.IntEnum):
    """SV ControlState values (common E30 numbering)."""

    EQUIPMENT_OFFLINE = 1
    ATTEMPT_ONLINE = 2
    HOST_OFFLINE = 3
    ONLINE_LOCAL = 4
    ONLINE_REMOTE = 5

    @property
    def online(self) -> bool:
        return self in (ControlState.ONLINE_LOCAL, ControlState.ONLINE_REMOTE)


class ProcessState(enum.IntEnum):
    """Equipment-specific processing state model (see DESIGN.md)."""

    IDLE = 1  # no process program selected
    READY = 2  # PP selected and loaded into the controller, sequencer idle
    EXECUTING = 3  # lot running
    PAUSE = 4  # sequencer held (paused by command or stopped by a fault)


# DVs that carry meaning for each CE (others are reported as zero-length items).
CE_DVS: dict[CE, tuple[DV, ...]] = {
    CE.PP_SELECTED: (DV.PP_SELECTED,),
    CE.LOT_COMPLETED: (DV.LOT_RESULT_WAFERS,),
    CE.LOT_STOPPED: (DV.LOT_RESULT_WAFERS,),
    CE.LOT_ABORTED: (DV.LOT_RESULT_WAFERS,),
    CE.WAFER_LOADED: (DV.EVENT_WAFER_NO,),
    CE.PUMPDOWN_COMPLETE: (DV.EVENT_WAFER_NO,),
    CE.STEP_STARTED: (DV.EVENT_STEP_NO, DV.EVENT_STEP_NAME),
    CE.STEP_COMPLETED: (DV.EVENT_STEP_NO, DV.EVENT_STEP_NAME),
    CE.WAFER_PROCESSED: (DV.EVENT_WAFER_NO,),
    CE.VENT_COMPLETE: (DV.EVENT_WAFER_NO,),
    CE.WAFER_UNLOADED: (DV.EVENT_WAFER_NO,),
    CE.ALARM_SET: (DV.ALARM_ID, DV.ALARM_TEXT),
    CE.ALARM_CLEARED: (DV.ALARM_ID, DV.ALARM_TEXT),
    CE.PROCESS_HELD: (DV.HELD_REASON,),
}
