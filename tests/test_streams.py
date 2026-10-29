"""Stream 1/2 housekeeping messages and stream 9 error reporting."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from gemsim.gem.ids import EC, SV
from gemsim.host.host import GemHost
from gemsim.hsms import T3Timeout
from gemsim.secs import F4, U1, U2, U4, A, B, L, SecsMessage

from .conftest import Bench, wait_until


async def wait_s9(host: GemHost, function: int) -> SecsMessage:
    await wait_until(lambda: any(m.function == function for m in host.stream9), 5, f"S9F{function}")
    return next(m for m in host.stream9 if m.function == function)


async def test_s9f3_unknown_stream(bench: Bench) -> None:
    with pytest.raises(T3Timeout):
        await bench.host.conn.request(SecsMessage(99, 1, True), t3=0.3)  # type: ignore[union-attr]
    s9 = await wait_s9(bench.host, 3)
    assert isinstance(s9.body, B) and len(s9.body.value) == 10  # MHEAD = offending header


async def test_s9f5_unknown_function(bench: Bench) -> None:
    with pytest.raises(T3Timeout):
        await bench.host.conn.request(SecsMessage(1, 99, True), t3=0.3)  # type: ignore[union-attr]
    await wait_s9(bench.host, 5)


async def test_s9f7_illegal_data(bench: Bench) -> None:
    with pytest.raises(T3Timeout):
        await bench.host.conn.request(SecsMessage(2, 41, True, A("not a list")), t3=0.3)  # type: ignore[union-attr]
    await wait_s9(bench.host, 7)


async def test_s9f1_unrecognized_device_id(bench: Bench) -> None:
    # HSMS-SS allows one session, so send on the bench connection with a wrong device ID
    conn = bench.host.conn
    assert conn is not None
    conn.session_id = 7
    with pytest.raises(T3Timeout):
        await conn.request(SecsMessage(1, 1, True), t3=0.3)
    conn.session_id = 1
    await wait_s9(bench.host, 1)


async def test_s9f9_when_host_never_acknowledges_an_event(bench: Bench) -> None:
    host, eq = bench.host, bench.equipment
    eq.cfg.timeouts.t3 = 0.3
    assert await host.define_reports({1: [SV.CONTROL_STATE]}) == 0
    assert await host.link_events({102: [1]}) == 0
    assert await host.enable_events([102]) == 0

    original = host._on_primary

    async def slow(msg: SecsMessage) -> SecsMessage | None:
        if (msg.stream, msg.function) == (6, 11):
            await asyncio.sleep(1.0)
        return await original(msg)

    assert host.conn is not None
    host.conn.handler = slow
    eq.operator_set_remote(False)
    eq.operator_set_remote(True)  # CE 102 -> S6F11 that the host answers too late
    s9 = await wait_s9(host, 9)
    assert isinstance(s9.body, B) and s9.body.value[2:4] == bytes([0x86, 11])  # SHEAD: W-bit|S6, F11


async def test_s1_namelists(bench: Bench) -> None:
    host = bench.host
    rows = (await host.request(1, 11, L(U4(SV.CHAMBER_PRESSURE), U4(4242)))).body
    assert rows == L(L(U4(1020), A("ChamberPressure"), A("Torr")), L(U4(4242), A(""), A("")))
    dvs = (await host.request(1, 21, L())).body
    assert isinstance(dvs, L) and len(dvs) == 8
    ces = (await host.request(1, 23, L(U4(142)))).body
    assert ces == L(L(U4(142), A("STEP_STARTED"), L(U4(2002), U4(2003))))
    assert (await host.request(1, 1)).body == L(A("GEMSIM-PC1"), A("0.1.0"))


async def test_all_svs_and_unknown_sv(bench: Bench) -> None:
    host = bench.host
    allsv = await host.status_variables([])
    assert len(allsv) == len(bench.equipment.vars.svs)
    assert await host.status_variables([4242]) == [[]]


async def test_equipment_constants(bench: Bench) -> None:
    host, link = bench.host, bench.link
    assert await host.equipment_constants([EC.PUMPDOWN_TIMEOUT, 4242]) == [90.0, []]
    assert await host.set_equipment_constants({EC.PUMPDOWN_TIMEOUT: F4(45.0)}) == 0
    assert link.controller.params.pumpdown_timeout_s == 45.0  # written through to the controller
    assert await host.set_equipment_constants({EC.PUMPDOWN_TIMEOUT: F4(5.0)}) == 3  # below minimum
    assert await host.set_equipment_constants({4242: U4(1)}) == 1
    assert await host.set_equipment_constants({EC.TIME_FORMAT: F4(1.0)}) == 3  # float for an integer EC
    assert await host.set_equipment_constants({EC.ESTABLISH_COMM_TIMEOUT: U2(30)}) == 0
    # atomic: one bad value rejects the whole request
    assert await host.set_equipment_constants({EC.ROBOT_TIMEOUT: F4(30.0), EC.BASE_PRESSURE: F4(9.0)}) == 3
    assert link.controller.params.robot_timeout_s == 20.0
    namelist = (await host.request(2, 29, L(U4(EC.BASE_PRESSURE)))).body
    expected = L(L(U4(3004), A("BasePressure"), F4(0.01), F4(1.0), F4(0.05), A("Torr")))
    assert namelist is not None and namelist.encode() == expected.encode()  # F4: compare on the wire


async def test_clock_and_time_set(bench: Bench) -> None:
    host = bench.host
    clock = (await host.request(2, 17)).body
    assert isinstance(clock, A) and len(clock.value) == 16
    assert (await host.request(2, 31, A("2030010112000000"))).body == B(b"\x00")
    now = (await host.request(2, 17)).body
    assert isinstance(now, A) and now.value.startswith("20300101120")
    assert (await host.request(2, 31, A("garbage"))).body == B(b"\x01")
    assert await host.set_equipment_constants({EC.TIME_FORMAT: U1(0)}) == 0
    short = (await host.request(2, 17)).body
    assert isinstance(short, A) and len(short.value) == 12
    assert dt.datetime.strptime(short.value, "%y%m%d%H%M%S").year == 2030


async def test_machine_constants_refused_while_controller_link_is_down(bench: Bench) -> None:
    host, link = bench.host, bench.link
    link._set_connected(False)  # as an OPC UA session loss would
    assert await host.set_equipment_constants({EC.PUMPDOWN_TIMEOUT: F4(45.0)}) == 2  # EAC 2
    assert link.controller.params.pumpdown_timeout_s == 90.0
    assert await host.set_equipment_constants({EC.TIME_FORMAT: U1(0)}) == 0  # GEM-only EC still works
    link._set_connected(True)
    await wait_until(lambda: bench.equipment.controller_ok(), 2, "link alarm cleared")
    assert await host.set_equipment_constants({EC.PUMPDOWN_TIMEOUT: F4(45.0)}) == 0
