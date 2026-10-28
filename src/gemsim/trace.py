"""SML-style message trace (what a host log or a SECS analyser shows)."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import TextIO

from .hsms.frame import HsmsHeader
from .secs.message import SecsMessage


class SmlTrace:
    """HSMS trace hook that renders every message as SML.

    ``local`` names this side ("H" for host, "E" for equipment); transmitted
    messages are shown as ``H -> E``, received ones as ``E -> H``.
    """

    def __init__(
        self,
        local: str = "H",
        sink: TextIO | None = None,
        *,
        echo: TextIO | None = None,
        echo_bodies: bool = False,
    ) -> None:
        self.local = local
        self.remote = "E" if local == "H" else "H"
        self.sink = sink
        self.echo = echo
        self.echo_bodies = echo_bodies  # False: console shows one line per message
        self.counts: Counter[str] = Counter()
        self.lines: list[str] = []

    def _emit(self, text: str, *, headline: bool = True) -> None:
        self.lines.append(text)
        if self.sink is not None:
            self.sink.write(text + "\n")
            self.sink.flush()
        if self.echo is not None and (headline or self.echo_bodies):
            self.echo.write(text + "\n")
            self.echo.flush()

    def note(self, text: str) -> None:
        """Scenario annotation, rendered as an SML comment."""
        self._emit(f"\n// ---- {text}")

    def __call__(self, direction: str, header: HsmsHeader, msg: SecsMessage | None) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        arrow = f"{self.local} -> {self.remote}" if direction == "tx" else f"{self.remote} -> {self.local}"
        sb = f"SB=0x{header.system:08X}"
        if msg is None:
            name = header.describe()
            self.counts[name.split()[0]] += 1
            self._emit(f"{ts}  {arrow}  {name:<28} {sb}")
            return
        self.counts[msg.sf] += 1
        lines = msg.sml().splitlines()
        self._emit(f"{ts}  {arrow}  {lines[0]:<28} {sb}")
        for line in lines[1:]:
            self._emit(line, headline=False)
