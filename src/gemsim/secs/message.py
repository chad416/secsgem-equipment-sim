"""SECS-II message (stream, function, W-bit, body) and its SML rendering."""

from __future__ import annotations

from dataclasses import dataclass, field

from .items import Item


@dataclass(frozen=True)
class SecsMessage:
    stream: int
    function: int
    wbit: bool = False
    body: Item | None = None
    # Filled in by the HSMS layer; part of the identity of a transaction.
    system: int = field(default=0, compare=False)
    session_id: int = field(default=0, compare=False)
    # Raw 10-byte HSMS header of a received message (needed for S9Fx MHEAD).
    raw_header: bytes = field(default=b"", compare=False, repr=False)
    # Set when the text could not be decoded as one SECS-II item (-> S9F7).
    decode_error: str | None = field(default=None, compare=False)

    @property
    def sf(self) -> str:
        return f"S{self.stream}F{self.function}"

    @property
    def is_primary(self) -> bool:
        """Odd function = primary message; even (or F0 abort) = reply."""
        return self.function % 2 == 1

    def reply(self, body: Item | None = None, function: int | None = None) -> SecsMessage:
        """Build the secondary message for this primary (same system bytes)."""
        fn = self.function + 1 if function is None else function
        return SecsMessage(self.stream, fn, False, body, self.system, self.session_id)

    def abort(self) -> SecsMessage:
        """SxF0: transaction abort (SEMI E5 9.5)."""
        return SecsMessage(self.stream, 0, False, None, self.system, self.session_id)

    def sml(self) -> str:
        head = self.sf + (" W" if self.wbit else "")
        if self.body is None:
            return head + "\n."
        return f"{head}\n{self.body.sml(1)}\n."
