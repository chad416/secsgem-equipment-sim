"""SECS-II codec: round trips, edge cases and a byte-level cross-check against
the independent ``secsgem`` implementation (used only as a test oracle)."""

from __future__ import annotations

import pytest
import secsgem.secs.functions as sg_functions
import secsgem.secs.variables as sg

from gemsim.secs import (
    F4,
    F8,
    I1,
    I2,
    I4,
    I8,
    U1,
    U2,
    U4,
    U8,
    A,
    B,
    Boolean,
    L,
    SecsDecodeError,
    SecsMessage,
    decode,
)


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        (U1(200), sg.U1(200)),
        (U2(60000), sg.U2(60000)),
        (U4(4_000_000_000), sg.U4(4_000_000_000)),
        (U8(2**63), sg.U8(2**63)),
        (I1(-5), sg.I1(-5)),
        (I2(-30000), sg.I2(-30000)),
        (I4(-2_000_000_000), sg.I4(-2_000_000_000)),
        (I8(-(2**62)), sg.I8(-(2**62))),
        (F4(1.5), sg.F4(1.5)),
        (F8(-273.15), sg.F8(-273.15)),
        (A("ETCH_STD"), sg.String("ETCH_STD")),
        (A(""), sg.String("")),
        (B(b"\x80\x01"), sg.Binary(b"\x80\x01")),
        (Boolean(True), sg.Boolean(True)),
        (U4(1, 2, 3), sg.U4([1, 2, 3])),
    ],
)
def test_scalar_bytes_match_secsgem(ours, theirs) -> None:  # type: ignore[no-untyped-def]
    assert ours.encode() == theirs.encode()
    assert decode(ours.encode()) == ours


def test_s2f41_bytes_match_secsgem() -> None:
    ours = L(A("START"), L(L(A("LOTID"), A("LOT-0001")), L(A("WAFERS"), A("3"))))
    theirs = sg_functions.SecsS02F41(
        {
            "RCMD": "START",
            "PARAMS": [{"CPNAME": "LOTID", "CPVAL": "LOT-0001"}, {"CPNAME": "WAFERS", "CPVAL": "3"}],
        }
    )
    assert ours.encode() == theirs.encode()


def test_s6f11_bytes_match_secsgem() -> None:
    ours = L(U4(1), U4(141), L(L(U4(10), L(F4(0.05), A("LOT-0001")))))
    theirs = sg_functions.SecsS06F11(
        {
            # secsgem picks the smallest integer type unless told otherwise
            "DATAID": sg.U4(1),
            "CEID": sg.U4(141),
            "RPT": [{"RPTID": sg.U4(10), "V": [sg.F4(0.05), sg.String("LOT-0001")]}],
        }
    )
    assert ours.encode() == theirs.encode()


def test_length_byte_count_grows_with_payload() -> None:
    assert A("x" * 255).encode()[0] & 0x03 == 1
    assert A("x" * 256).encode()[0] & 0x03 == 2
    assert A("x" * 70000).encode()[0] & 0x03 == 3
    assert decode(A("x" * 70000).encode()) == A("x" * 70000)


def test_nested_list_round_trip() -> None:
    item = L(U4(1), L(), L(A("a"), L(Boolean(False, True), F4(1.0, 2.0))), B(b""))
    assert decode(item.encode()) == item


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\x41",  # A with missing length byte
        b"\x41\x05AB",  # declares 5 bytes, has 2
        b"\xb1\x03\x00\x00\x00",  # U4 with 3 bytes
        b"\xfd\x01\x00",  # unknown format code
        b"\x40\x00",  # zero length-byte count
        b"\x01\x02\x41\x00",  # list of 2 with only one child
        b"\x41\x01A\x00",  # trailing byte
    ],
)
def test_malformed_input_raises(raw: bytes) -> None:
    with pytest.raises(SecsDecodeError):
        decode(raw)


def test_range_checks() -> None:
    with pytest.raises(ValueError):
        U1(256)
    with pytest.raises(ValueError):
        I1(-129)
    with pytest.raises(ValueError):
        U4(1.5)


def test_sml_rendering() -> None:
    msg = SecsMessage(1, 13, True, L(A("MDLN"), A("1.0")))
    assert msg.sml() == 'S1F13 W\n  <L [2]\n    <A "MDLN">\n    <A "1.0">\n  >\n.'
    assert B(b"\x04").sml() == "<B [1] 0x04>"
    assert U4().sml() == "<U4 [0]>"
    assert L().sml() == "<L [0]>"
    assert F4(0.5).sml() == "<F4 [1] 0.5>"
