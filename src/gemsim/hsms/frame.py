"""HSMS message framing (SEMI E37 section 8).

On the wire every HSMS message is::

    +----------------+-----------------------------------------------+---------+
    | length (4 B)   | header (10 B)                                 | text    |
    +----------------+-----------------------------------------------+---------+
                       session id | byte 2 | byte 3 | PType | SType | system bytes
                       (2 B)        (1 B)    (1 B)    (1 B)   (1 B)   (4 B)

For data messages (SType 0) byte 2 holds the W-bit and the stream, byte 3 the
function. For control messages bytes 2/3 carry status or reason codes.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass

HEADER_LEN = 10
CONTROL_SESSION_ID = 0xFFFF


class SType(enum.IntEnum):
    DATA = 0
    SELECT_REQ = 1
    SELECT_RSP = 2
    DESELECT_REQ = 3
    DESELECT_RSP = 4
    LINKTEST_REQ = 5
    LINKTEST_RSP = 6
    REJECT_REQ = 7
    SEPARATE_REQ = 9


class SelectStatus(enum.IntEnum):
    OK = 0
    ALREADY_ACTIVE = 1
    NOT_READY = 2
    CONNECTION_EXHAUST = 3


class DeselectStatus(enum.IntEnum):
    OK = 0
    NOT_ESTABLISHED = 1
    BUSY = 2


class RejectReason(enum.IntEnum):
    STYPE_NOT_SUPPORTED = 1
    PTYPE_NOT_SUPPORTED = 2
    TRANSACTION_NOT_OPEN = 3
    ENTITY_NOT_SELECTED = 4


_FMT = ">HBBBBI"


@dataclass(frozen=True)
class HsmsHeader:
    session_id: int
    byte2: int
    byte3: int
    ptype: int
    stype: int
    system: int

    def encode(self) -> bytes:
        return struct.pack(_FMT, self.session_id, self.byte2, self.byte3, self.ptype, self.stype, self.system)

    @classmethod
    def decode(cls, data: bytes) -> HsmsHeader:
        if len(data) != HEADER_LEN:
            raise ValueError(f"HSMS header must be {HEADER_LEN} bytes, got {len(data)}")
        return cls(*struct.unpack(_FMT, data))

    # --- data message helpers -------------------------------------------------
    @property
    def wbit(self) -> bool:
        return bool(self.byte2 & 0x80)

    @property
    def stream(self) -> int:
        return self.byte2 & 0x7F

    @property
    def function(self) -> int:
        return self.byte3

    @classmethod
    def data(cls, session_id: int, stream: int, function: int, wbit: bool, system: int) -> HsmsHeader:
        if not 0 <= stream <= 127 or not 0 <= function <= 255:
            raise ValueError(f"invalid S{stream}F{function}")
        return cls(session_id, (0x80 if wbit else 0) | stream, function, 0, SType.DATA, system)

    @classmethod
    def control(cls, stype: SType, system: int, byte2: int = 0, byte3: int = 0) -> HsmsHeader:
        return cls(CONTROL_SESSION_ID, byte2, byte3, 0, stype, system)

    def describe(self) -> str:
        """Short text used in traces, e.g. ``Select.rsp status=0``."""
        names = {
            SType.SELECT_REQ: "Select.req",
            SType.SELECT_RSP: "Select.rsp",
            SType.DESELECT_REQ: "Deselect.req",
            SType.DESELECT_RSP: "Deselect.rsp",
            SType.LINKTEST_REQ: "Linktest.req",
            SType.LINKTEST_RSP: "Linktest.rsp",
            SType.REJECT_REQ: "Reject.req",
            SType.SEPARATE_REQ: "Separate.req",
        }
        try:
            name = names[SType(self.stype)]
        except (ValueError, KeyError):
            return f"SType={self.stype}"
        if self.stype in (SType.SELECT_RSP, SType.DESELECT_RSP):
            return f"{name} status={self.byte3}"
        if self.stype == SType.REJECT_REQ:
            return f"{name} stype={self.byte2} reason={self.byte3}"
        return name


def encode_frame(header: HsmsHeader, text: bytes = b"") -> bytes:
    body = header.encode() + text
    return len(body).to_bytes(4, "big") + body
