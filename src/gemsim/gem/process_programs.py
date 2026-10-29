"""Process program (recipe) store and recipe text format (E5 stream 7, E30 4.6).

Recipes are unformatted process programs: the PPBODY is ASCII text, one step per
line::

    # comment
    STEP <name> TEMP=<degC> PRESSURE=<Torr> TIME=<s>

The equipment stores bodies verbatim. Validation happens when a recipe is
selected (PP-SELECT), because that is the point where it must be translated into
controller setpoints. All values in the example recipes are illustrative.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

from ..controller.interface import MAX_RECIPE_STEPS, RecipeStep

PPID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
STEP_RE = re.compile(
    r"^STEP\s+(?P<name>[A-Za-z0-9_]{1,24})\s+TEMP=(?P<t>[0-9.]+)\s+PRESSURE=(?P<p>[0-9.]+)\s+TIME=(?P<d>[0-9.]+)$"
)


class PpGnt(enum.IntEnum):
    """S7F2 grant codes."""

    OK = 0
    ALREADY_HAVE = 1
    NO_SPACE = 2
    INVALID_PPID = 3
    BUSY = 4
    WILL_NOT_ACCEPT = 5
    OTHER_ERROR = 6


class AckC7(enum.IntEnum):
    ACCEPTED = 0
    PERMISSION_NOT_GRANTED = 1
    LENGTH_ERROR = 2
    MATRIX_OVERFLOW = 3
    PPID_NOT_FOUND = 4
    MODE_UNSUPPORTED = 5
    OTHER_ERROR = 6


class RecipeError(ValueError):
    pass


@dataclass(frozen=True)
class Recipe:
    ppid: str
    steps: tuple[RecipeStep, ...]


def parse_recipe(ppid: str, body: bytes) -> Recipe:
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RecipeError("recipe body is not ASCII") from exc
    steps: list[RecipeStep] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = STEP_RE.match(line)
        if not m:
            raise RecipeError(f"line {lineno}: cannot parse {line!r}")
        try:
            step = RecipeStep(m["name"], float(m["t"]), float(m["p"]), float(m["d"]))
        except ValueError as exc:
            raise RecipeError(f"line {lineno}: {exc}") from exc
        steps.append(step)
    if not steps:
        raise RecipeError("recipe has no STEP lines")
    if len(steps) > MAX_RECIPE_STEPS:
        raise RecipeError(f"recipe has {len(steps)} steps, maximum is {MAX_RECIPE_STEPS}")
    return Recipe(ppid, tuple(steps))


class ProcessProgramStore:
    def __init__(self, *, max_programs: int = 32, max_body: int = 64 * 1024) -> None:
        self.max_programs = max_programs
        self.max_body = max_body
        self.programs: dict[str, bytes] = {}
        self._binary: set[str] = set()  # PPIDs downloaded as <B> rather than <A>
        self.locked: str | None = None  # selected PP while a lot is running

    def inquire(self, ppid: str, length: int) -> PpGnt:
        """S7F1 load inquire."""
        if not PPID_RE.match(ppid):
            return PpGnt.INVALID_PPID
        if ppid == self.locked:
            return PpGnt.BUSY
        if length > self.max_body or (ppid not in self.programs and len(self.programs) >= self.max_programs):
            return PpGnt.NO_SPACE
        return PpGnt.OK

    def store(self, ppid: str, body: bytes, *, binary: bool = False) -> AckC7:
        """S7F3. Overwrites an existing PP with the same PPID."""
        if not PPID_RE.match(ppid):
            return AckC7.OTHER_ERROR
        if ppid == self.locked:
            return AckC7.PERMISSION_NOT_GRANTED
        if len(body) > self.max_body:
            return AckC7.LENGTH_ERROR
        if ppid not in self.programs and len(self.programs) >= self.max_programs:
            return AckC7.MATRIX_OVERFLOW
        self.programs[ppid] = body
        if binary:
            self._binary.add(ppid)
        else:
            self._binary.discard(ppid)
        return AckC7.ACCEPTED

    def get(self, ppid: str) -> bytes | None:
        return self.programs.get(ppid)

    def is_binary(self, ppid: str) -> bool:
        return ppid in self._binary

    def delete(self, ppids: list[str]) -> AckC7:
        """S7F17. An empty list deletes all programs. All-or-nothing."""
        targets = ppids or list(self.programs)
        if any(p not in self.programs for p in targets):
            return AckC7.PPID_NOT_FOUND
        if self.locked is not None and self.locked in targets:
            return AckC7.PERMISSION_NOT_GRANTED
        for p in targets:
            del self.programs[p]
            self._binary.discard(p)
        return AckC7.ACCEPTED

    def directory(self) -> list[str]:
        return sorted(self.programs)
