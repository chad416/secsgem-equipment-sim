"""SECS-II data items (SEMI E5 section 9).

Every item is encoded as a format byte, 1-3 length bytes and the payload.
The upper six bits of the format byte select the type, the lower two bits give
the number of length bytes that follow. Numbers are big-endian. A list carries
the number of child items in its length field, every other type the number of
payload bytes.

The classes here are small immutable value objects so they can be compared in
tests and rendered as SML (the text notation used in equipment manuals and
host logs).
"""

from __future__ import annotations

import enum
import math
import struct
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Union


class SecsDecodeError(ValueError):
    """Raised when a byte string is not a valid SECS-II item."""


class Fmt(enum.IntEnum):
    """Format codes (octal, as written in SEMI E5 table 1)."""

    L = 0o00
    B = 0o10
    BOOLEAN = 0o11
    A = 0o20
    J = 0o21
    I8 = 0o30
    I1 = 0o31
    I2 = 0o32
    I4 = 0o34
    F8 = 0o40
    F4 = 0o44
    U8 = 0o50
    U1 = 0o51
    U2 = 0o52
    U4 = 0o54


# struct code, byte width, min, max for every numeric format
_NUMERIC: dict[Fmt, tuple[str, int, float, float]] = {
    Fmt.I1: ("b", 1, -(2**7), 2**7 - 1),
    Fmt.I2: ("h", 2, -(2**15), 2**15 - 1),
    Fmt.I4: ("i", 4, -(2**31), 2**31 - 1),
    Fmt.I8: ("q", 8, -(2**63), 2**63 - 1),
    Fmt.U1: ("B", 1, 0, 2**8 - 1),
    Fmt.U2: ("H", 2, 0, 2**16 - 1),
    Fmt.U4: ("I", 4, 0, 2**32 - 1),
    Fmt.U8: ("Q", 8, 0, 2**64 - 1),
    Fmt.F4: ("f", 4, -math.inf, math.inf),
    Fmt.F8: ("d", 8, -math.inf, math.inf),
}

MAX_ITEM_LENGTH = 2**24 - 1

Item = Union["L", "A", "B", "Boolean", "Num"]


def _encode_header(fmt: Fmt, length: int) -> bytes:
    if length < 0 or length > MAX_ITEM_LENGTH:
        raise ValueError(f"item length {length} out of range")
    if length <= 0xFF:
        n = 1
    elif length <= 0xFFFF:
        n = 2
    else:
        n = 3
    return bytes([(fmt << 2) | n]) + length.to_bytes(n, "big")


@dataclass(frozen=True)
class L:
    """List of items. ``len(L)`` is the number of children."""

    items: tuple[Item, ...] = ()
    fmt: ClassVar[Fmt] = Fmt.L

    def __init__(self, *items: Item) -> None:
        object.__setattr__(self, "items", tuple(items))

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> Item:
        return self.items[index]

    def __iter__(self) -> Iterator[Item]:
        return iter(self.items)

    def encode(self) -> bytes:
        return _encode_header(Fmt.L, len(self.items)) + b"".join(i.encode() for i in self.items)

    def to_python(self) -> list[object]:
        return [i.to_python() for i in self.items]

    def sml(self, indent: int = 0) -> str:
        pad = "  " * indent
        if not self.items:
            return f"{pad}<L [0]>"
        lines = [f"{pad}<L [{len(self.items)}]"]
        lines.extend(i.sml(indent + 1) for i in self.items)
        lines.append(f"{pad}>")
        return "\n".join(lines)


@dataclass(frozen=True)
class A:
    """ASCII text."""

    value: str = ""
    fmt: ClassVar[Fmt] = Fmt.A

    def encode(self) -> bytes:
        data = self.value.encode("ascii")
        return _encode_header(Fmt.A, len(data)) + data

    def to_python(self) -> str:
        return self.value

    def sml(self, indent: int = 0) -> str:
        escaped = self.value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'{"  " * indent}<A "{escaped}">'


@dataclass(frozen=True)
class B:
    """Binary bytes (used for ACK codes such as HCACK, ALCD, ACKC5)."""

    value: bytes = b""
    fmt: ClassVar[Fmt] = Fmt.B

    def encode(self) -> bytes:
        return _encode_header(Fmt.B, len(self.value)) + self.value

    def to_python(self) -> bytes:
        return self.value

    @property
    def first(self) -> int:
        """First byte, the usual way an ACK code is read."""
        if not self.value:
            raise ValueError("empty binary item")
        return self.value[0]

    def sml(self, indent: int = 0) -> str:
        body = " ".join(f"0x{b:02X}" for b in self.value)
        return f"{'  ' * indent}<B [{len(self.value)}] {body}>".replace(" >", ">")


@dataclass(frozen=True)
class Boolean:
    """Boolean array (one byte per value, non-zero = true)."""

    values: tuple[bool, ...] = ()
    fmt: ClassVar[Fmt] = Fmt.BOOLEAN

    def __init__(self, *values: bool) -> None:
        object.__setattr__(self, "values", tuple(bool(v) for v in values))

    def encode(self) -> bytes:
        return _encode_header(Fmt.BOOLEAN, len(self.values)) + bytes(int(v) for v in self.values)

    def to_python(self) -> bool | list[bool]:
        return self.values[0] if len(self.values) == 1 else list(self.values)

    def sml(self, indent: int = 0) -> str:
        body = " ".join("T" if v else "F" for v in self.values)
        return f"{'  ' * indent}<BOOLEAN [{len(self.values)}] {body}>".replace(" >", ">")


@dataclass(frozen=True)
class Num:
    """Numeric array of one fixed format (I1..I8, U1..U8, F4, F8).

    SECS-II has no scalar type: a scalar is an array of length one.
    """

    fmt: Fmt
    values: tuple[int | float, ...] = ()

    def __post_init__(self) -> None:
        if self.fmt not in _NUMERIC:
            raise ValueError(f"{self.fmt!r} is not numeric")
        _, _, lo, hi = _NUMERIC[self.fmt]
        is_float = self.fmt in (Fmt.F4, Fmt.F8)
        clean: list[int | float] = []
        for v in self.values:
            if is_float:
                clean.append(float(v))
                continue
            if isinstance(v, float) and not v.is_integer():
                raise ValueError(f"{self.fmt.name} needs integers, got {v}")
            iv = int(v)
            if not lo <= iv <= hi:
                raise ValueError(f"{iv} out of range for {self.fmt.name}")
            clean.append(iv)
        object.__setattr__(self, "values", tuple(clean))

    def encode(self) -> bytes:
        code, width, _, _ = _NUMERIC[self.fmt]
        payload = struct.pack(f">{len(self.values)}{code}", *self.values)
        return _encode_header(self.fmt, len(self.values) * width) + payload

    def to_python(self) -> int | float | list[int | float]:
        return self.values[0] if len(self.values) == 1 else list(self.values)

    @property
    def scalar(self) -> int | float:
        if len(self.values) != 1:
            raise ValueError(f"expected 1 value, got {len(self.values)}")
        return self.values[0]

    def sml(self, indent: int = 0) -> str:
        if self.fmt in (Fmt.F4, Fmt.F8):
            body = " ".join(f"{v:g}" for v in self.values)
        else:
            body = " ".join(str(v) for v in self.values)
        return f"{'  ' * indent}<{self.fmt.name} [{len(self.values)}] {body}>".replace(" >", ">")


# Convenience constructors that read like SML: U4(5), F4(1.5, 2.5) ...
def _num(fmt: Fmt) -> Callable[..., Num]:
    def make(*values: int | float) -> Num:
        return Num(fmt, tuple(values))

    make.__name__ = fmt.name
    make.__doc__ = f"{fmt.name} numeric item"
    return make


I1 = _num(Fmt.I1)
I2 = _num(Fmt.I2)
I4 = _num(Fmt.I4)
I8 = _num(Fmt.I8)
U1 = _num(Fmt.U1)
U2 = _num(Fmt.U2)
U4 = _num(Fmt.U4)
U8 = _num(Fmt.U8)
F4 = _num(Fmt.F4)
F8 = _num(Fmt.F8)


def decode(data: bytes) -> Item:
    """Decode exactly one item; trailing bytes are an error."""
    item, end = decode_at(data, 0)
    if end != len(data):
        raise SecsDecodeError(f"{len(data) - end} trailing bytes after item")
    return item


def decode_at(data: bytes, pos: int, depth: int = 0) -> tuple[Item, int]:
    """Decode one item starting at ``pos``. Returns the item and the end offset."""
    if depth > 64:
        raise SecsDecodeError("list nesting deeper than 64")
    if pos >= len(data):
        raise SecsDecodeError("unexpected end of data (format byte)")
    fb = data[pos]
    n_len = fb & 0x03
    if n_len == 0:
        raise SecsDecodeError(f"format byte 0x{fb:02X} has zero length bytes")
    try:
        fmt = Fmt(fb >> 2)
    except ValueError as exc:
        raise SecsDecodeError(f"unknown format code 0o{fb >> 2:o}") from exc
    pos += 1
    if pos + n_len > len(data):
        raise SecsDecodeError("unexpected end of data (length bytes)")
    length = int.from_bytes(data[pos : pos + n_len], "big")
    pos += n_len

    if fmt is Fmt.L:
        children: list[Item] = []
        for _ in range(length):
            child, pos = decode_at(data, pos, depth + 1)
            children.append(child)
        return L(*children), pos

    end = pos + length
    if end > len(data):
        raise SecsDecodeError(f"{fmt.name} needs {length} bytes, {len(data) - pos} available")
    payload = data[pos:end]
    if fmt is Fmt.A or fmt is Fmt.J:
        try:
            return A(payload.decode("ascii")), end
        except UnicodeDecodeError as exc:
            raise SecsDecodeError("non-ASCII byte in <A> item") from exc
    if fmt is Fmt.B:
        return B(payload), end
    if fmt is Fmt.BOOLEAN:
        return Boolean(*(b != 0 for b in payload)), end
    code, width, _, _ = _NUMERIC[fmt]
    if length % width:
        raise SecsDecodeError(f"{fmt.name} length {length} not a multiple of {width}")
    values = struct.unpack(f">{length // width}{code}", payload)
    return Num(fmt, tuple(values)), end


def ensure_list(item: Item | None, *, min_len: int = 0) -> L:
    """Return ``item`` as a list or raise SecsDecodeError (used by message handlers)."""
    if not isinstance(item, L):
        raise SecsDecodeError(f"expected <L>, got {type(item).__name__}")
    if len(item) < min_len:
        raise SecsDecodeError(f"expected at least {min_len} list items, got {len(item)}")
    return item


def as_text(item: Item) -> str:
    if not isinstance(item, A):
        raise SecsDecodeError(f"expected <A>, got {type(item).__name__}")
    return item.value


def as_int(item: Item) -> int:
    if isinstance(item, Num) and item.fmt not in (Fmt.F4, Fmt.F8) and len(item.values) == 1:
        return int(item.values[0])
    if isinstance(item, B) and len(item.value) == 1:
        return item.value[0]
    raise SecsDecodeError(f"expected an integer scalar, got {item!r}")


def as_ints(item: Item) -> list[int]:
    """Read a vector of IDs. Accepts a numeric array or a list of scalars."""
    if isinstance(item, Num) and item.fmt not in (Fmt.F4, Fmt.F8):
        return [int(v) for v in item.values]
    if isinstance(item, L):
        return [as_int(child) for child in item]
    raise SecsDecodeError(f"expected an ID vector, got {item!r}")


def as_bool(item: Item) -> bool:
    if isinstance(item, Boolean) and len(item.values) == 1:
        return item.values[0]
    if isinstance(item, B) and len(item.value) == 1:
        return item.value[0] != 0
    raise SecsDecodeError(f"expected a BOOLEAN scalar, got {item!r}")


def u4_list(values: Iterable[int]) -> L:
    """``<L <U4 a> <U4 b> ...>`` - the common way to send an ID vector."""
    return L(*(U4(v) for v in values))


def text_list(values: Sequence[str]) -> L:
    return L(*(A(v) for v in values))
