"""Alarm management (SEMI E30 4.5 / E5 stream 5)."""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass


class AlarmCategory(enum.IntEnum):
    """ALCD bits 1-7 (E5): category of the alarm. Bit 8 = alarm set."""

    PERSONAL_SAFETY = 1
    EQUIPMENT_SAFETY = 2
    PARAMETER_CONTROL_WARNING = 3
    PARAMETER_CONTROL_ERROR = 4
    IRRECOVERABLE_ERROR = 5
    EQUIPMENT_STATUS_WARNING = 6
    ATTENTION_FLAGS = 7
    DATA_INTEGRITY = 8


class AckC5(enum.IntEnum):
    OK = 0
    ERROR = 1


ALCD_SET = 0x80


@dataclass(frozen=True)
class AlarmDef:
    alid: int
    text: str  # ALTX, max 40 characters
    category: AlarmCategory
    controller_bit: int | None = None  # bit in the PLC alarm word, None = bridge alarm

    def __post_init__(self) -> None:
        if len(self.text) > 40:
            raise ValueError(f"ALTX longer than 40 characters: {self.text!r}")

    def alcd(self, active: bool) -> int:
        return (ALCD_SET if active else 0) | int(self.category)


class AlarmManager:
    def __init__(self, defs: Sequence[AlarmDef]) -> None:
        self.defs = {d.alid: d for d in defs}
        self.by_bit = {d.controller_bit: d for d in defs if d.controller_bit is not None}
        self.enabled: set[int] = set()
        self.active: set[int] = set()

    def set(self, alid: int) -> bool:
        """Returns True if the alarm changed state (edge)."""
        if alid in self.active:
            return False
        self.active.add(alid)
        return True

    def clear(self, alid: int) -> bool:
        if alid not in self.active:
            return False
        self.active.discard(alid)
        return True

    def enable(self, enable: bool, alids: Sequence[int]) -> AckC5:
        """S5F3. An empty ALID list means all alarms."""
        targets = set(alids) if alids else set(self.defs)
        if not targets <= set(self.defs):
            return AckC5.ERROR
        if enable:
            self.enabled |= targets
        else:
            self.enabled -= targets
        return AckC5.OK

    def is_enabled(self, alid: int) -> bool:
        return alid in self.enabled
