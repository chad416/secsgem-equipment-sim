"""Status variables, data variables and equipment constants."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..secs import F4, U1, U2, U4, A, Boolean, Fmt, Item, L, Num

Encoder = Callable[[Any], Item]


def enc_text(v: Any) -> Item:
    return A(str(v))


def enc_bool(v: Any) -> Item:
    return Boolean(bool(v))


def enc_u1(v: Any) -> Item:
    return U1(int(v))


def enc_u2(v: Any) -> Item:
    return U2(int(v))


def enc_u4(v: Any) -> Item:
    return U4(int(v))


def enc_f4(v: Any) -> Item:
    return F4(float(v))


def enc_u4_list(v: Any) -> Item:
    return L(*(U4(int(x)) for x in v))


_EMPTY: dict[Encoder, Item] = {
    enc_text: A(""),
    enc_bool: Boolean(),
    enc_u1: U1(),
    enc_u2: U2(),
    enc_u4: U4(),
    enc_f4: F4(),
    enc_u4_list: L(),
}


def empty_like(encoder: Encoder) -> Item:
    """Zero-length item of the variable's format (DV not valid for this event)."""
    return _EMPTY.get(encoder, L())


@dataclass
class Variable:
    vid: int
    name: str
    units: str
    encoder: Encoder
    getter: Callable[[], Any] | None = None  # SVs only

    def item(self, value: Any) -> Item:
        return self.encoder(value)


@dataclass
class EquipmentConstant:
    ecid: int
    name: str
    units: str
    encoder: Encoder
    minimum: float
    maximum: float
    default: float
    value: float = field(init=False)
    controller_tag: str | None = None  # PLC parameter this EC is written to

    def __post_init__(self) -> None:
        self.value = self.default

    def coerce(self, item: Item) -> float | None:
        """Return the new value or None if the item has the wrong format."""
        if not isinstance(item, Num) or len(item.values) != 1:
            return None
        expected = self.encoder(0)
        assert isinstance(expected, Num)
        is_float_ec = expected.fmt in (Fmt.F4, Fmt.F8)
        is_float_item = item.fmt in (Fmt.F4, Fmt.F8)
        if is_float_item and not is_float_ec:
            return None
        return float(item.values[0])

    def in_range(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum

    def item(self) -> Item:
        return self.encoder(self.value)


class VariableRegistry:
    def __init__(self) -> None:
        self.svs: dict[int, Variable] = {}
        self.dvs: dict[int, Variable] = {}
        self.ecs: dict[int, EquipmentConstant] = {}

    def add_sv(self, var: Variable) -> None:
        self.svs[var.vid] = var

    def add_dv(self, var: Variable) -> None:
        self.dvs[var.vid] = var

    def add_ec(self, ec: EquipmentConstant) -> None:
        self.ecs[ec.ecid] = ec

    def exists(self, vid: int) -> bool:
        return vid in self.svs or vid in self.dvs or vid in self.ecs

    def sv_item(self, svid: int) -> Item:
        var = self.svs.get(svid)
        if var is None or var.getter is None:
            return L()  # E5: unknown SVID -> zero-length item
        return var.item(var.getter())

    def value_item(self, vid: int, context: Mapping[int, Any]) -> Item:
        """Value of any VID for a report: SV now, EC now, DV from event context."""
        if vid in self.svs:
            return self.sv_item(vid)
        if vid in self.ecs:
            return self.ecs[vid].item()
        dv = self.dvs.get(vid)
        if dv is None:
            return L()
        if vid in context:
            return dv.item(context[vid])
        return empty_like(dv.encoder)
