"""Remote command (S2F41 / S2F42) definitions and parameter validation."""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..controller.interface import MAX_WAFERS_PER_LOT, CmdResult
from ..secs import A, Fmt, Item, Num


class HcAck(enum.IntEnum):
    OK = 0
    INVALID_COMMAND = 1
    CANNOT_PERFORM_NOW = 2
    PARAMETER_INVALID = 3
    ACK_FINISH_LATER = 4
    ALREADY_IN_CONDITION = 5
    NO_SUCH_OBJECT = 6


class CpAck(enum.IntEnum):
    NAME_UNKNOWN = 1
    ILLEGAL_VALUE = 2
    ILLEGAL_FORMAT = 3


class ParamError(Exception):
    def __init__(self, cpack: CpAck) -> None:
        super().__init__(cpack.name)
        self.cpack = cpack


def text_param(max_len: int) -> Callable[[Item], Any]:
    def parse(item: Item) -> str:
        if not isinstance(item, A):
            raise ParamError(CpAck.ILLEGAL_FORMAT)
        if not 1 <= len(item.value) <= max_len:
            raise ParamError(CpAck.ILLEGAL_VALUE)
        return item.value

    return parse


def int_param(lo: int, hi: int) -> Callable[[Item], Any]:
    """Accepts an integer item or ASCII digits (hosts differ in what they send)."""

    def parse(item: Item) -> int:
        if isinstance(item, Num) and item.fmt not in (Fmt.F4, Fmt.F8) and len(item.values) == 1:
            value = int(item.values[0])
        elif isinstance(item, A) and item.value.isdigit():
            value = int(item.value)
        else:
            raise ParamError(CpAck.ILLEGAL_FORMAT)
        if not lo <= value <= hi:
            raise ParamError(CpAck.ILLEGAL_VALUE)
        return value

    return parse


@dataclass(frozen=True)
class CommandSpec:
    name: str
    params: dict[str, Callable[[Item], Any]]
    required: tuple[str, ...] = ()


COMMANDS: dict[str, CommandSpec] = {
    spec.name: spec
    for spec in (
        CommandSpec("PP-SELECT", {"PPID": text_param(32)}, required=("PPID",)),
        CommandSpec(
            "START",
            {"LOTID": text_param(40), "WAFERS": int_param(1, MAX_WAFERS_PER_LOT)},
            required=("LOTID",),
        ),
        CommandSpec("STOP", {}),
        CommandSpec("PAUSE", {}),
        CommandSpec("RESUME", {}),
        CommandSpec("ABORT", {}),
    )
}


def hcack_for(result: CmdResult) -> HcAck:
    """Translate the controller's handshake result into HCACK.

    Sequencer commands complete asynchronously (signalled by collection events),
    so an accepted command answers HCACK 4.
    """
    return {
        CmdResult.OK: HcAck.ACK_FINISH_LATER,
        CmdResult.ALREADY: HcAck.ALREADY_IN_CONDITION,
        CmdResult.BAD_STATE: HcAck.CANNOT_PERFORM_NOW,
        CmdResult.BAD_ARG: HcAck.PARAMETER_INVALID,
        CmdResult.INTERLOCK: HcAck.CANNOT_PERFORM_NOW,
        CmdResult.NO_RESPONSE: HcAck.CANNOT_PERFORM_NOW,
    }[result]
