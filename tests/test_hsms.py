"""HSMS-SS transport: framing, control transactions, timers and reject paths."""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest
import secsgem.hsms as sg_hsms

from gemsim.hsms import (
    ControlTimeout,
    HsmsConnection,
    HsmsHeader,
    HsmsPassiveServer,
    HsmsTimeouts,
    RejectReason,
    SelectRefused,
    SelectStatus,
    State,
    SType,
    T3Timeout,
    connect_active,
)
from gemsim.hsms.frame import encode_frame
from gemsim.secs import U4, A, L, SecsMessage

from .conftest import wait_until

T = HsmsTimeouts(t3=0.5, t5=0.1, t6=0.5, t7=0.5, t8=0.3)


def test_data_header_matches_secsgem() -> None:
    ours = HsmsHeader.data(session_id=1, stream=6, function=11, wbit=True, system=0x10002).encode()
    theirs = sg_hsms.HsmsHeader(0x10002, 1, 6, 11, True, 0, sg_hsms.HsmsSType.DATA_MESSAGE).encode()
    assert ours == theirs
    assert HsmsHeader.decode(ours).wbit and HsmsHeader.decode(ours).stream == 6


def test_select_header_matches_secsgem() -> None:
    ours = HsmsHeader.control(SType.SELECT_REQ, 7).encode()
    theirs = sg_hsms.HsmsSelectReqHeader(7).encode()
    assert ours == theirs


async def echo(msg: SecsMessage) -> SecsMessage | None:
    return msg.reply(msg.body)


async def never(msg: SecsMessage) -> SecsMessage | None:
    await asyncio.sleep(10)
    return None


@pytest.fixture
async def server() -> AsyncIterator[HsmsPassiveServer]:
    srv = HsmsPassiveServer("127.0.0.1", 0, session_id=1, timeouts=T, handler=echo)
    await srv.start()
    yield srv
    await srv.stop()


async def active(port: int, handler=echo) -> HsmsConnection:  # type: ignore[no-untyped-def]
    return await connect_active("127.0.0.1", port, session_id=1, timeouts=T, handler=handler)


async def read_frame(reader: asyncio.StreamReader) -> tuple[HsmsHeader, bytes]:
    length = struct.unpack(">I", await reader.readexactly(4))[0]
    payload = await reader.readexactly(length)
    return HsmsHeader.decode(payload[:10]), payload[10:]


async def test_select_request_linktest_deselect(server: HsmsPassiveServer) -> None:
    conn = await active(server.port)
    assert conn.state is State.SELECTED
    reply = await conn.request(SecsMessage(1, 1, True, L(A("x"), U4(5))))
    assert reply.function == 2 and reply.body == L(A("x"), U4(5))
    await conn.linktest()
    assert await conn.deselect() == 0
    assert conn.state is State.NOT_SELECTED
    await conn.separate()
    assert conn.closed


async def test_t3_reply_timeout() -> None:
    srv = HsmsPassiveServer("127.0.0.1", 0, session_id=1, timeouts=T, handler=never)
    await srv.start()
    try:
        conn = await active(srv.port)
        with pytest.raises(T3Timeout) as info:
            await conn.request(SecsMessage(1, 1, True))
        assert len(info.value.header) == 10
        assert conn.state is State.SELECTED  # T3 is not a link failure
        await conn.separate()
    finally:
        await srv.stop()


async def test_t7_not_selected_timeout(server: HsmsPassiveServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    # send nothing: passive side must drop the connection after T7
    data = await asyncio.wait_for(reader.read(), T.t7 + 1.0)
    assert data == b""
    assert server.connection is not None and server.connection.close_reason == "T7 not-selected timeout"
    writer.close()


async def test_t6_control_timeout_closes_connection() -> None:
    async def silent(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)

    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ControlTimeout):
            await active(port)
    finally:
        srv.close()


async def test_t8_intercharacter_timeout(server: HsmsPassiveServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(struct.pack(">I", 10) + b"\xff\xff")  # 2 of 10 header bytes, then stall
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), T.t8 + 1.0) == b""
    assert server.connection is not None
    assert server.connection.close_reason == "T8 inter-character timeout"
    writer.close()


async def test_data_before_select_is_rejected(server: HsmsPassiveServer) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(encode_frame(HsmsHeader.data(1, 1, 1, True, 42)))
    header, _ = await asyncio.wait_for(read_frame(reader), 2)
    assert header.stype == SType.REJECT_REQ
    assert header.byte3 == RejectReason.ENTITY_NOT_SELECTED
    assert header.system == 42
    writer.close()


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        (HsmsHeader(0xFFFF, 0, 0, 0, 8, 5), RejectReason.STYPE_NOT_SUPPORTED),  # SType 8 undefined
        (HsmsHeader(0xFFFF, 0, 0, 3, 5, 6), RejectReason.PTYPE_NOT_SUPPORTED),
        (HsmsHeader.control(SType.SELECT_RSP, 7), RejectReason.TRANSACTION_NOT_OPEN),
    ],
)
async def test_reject_reasons(server: HsmsPassiveServer, frame: HsmsHeader, reason: RejectReason) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(encode_frame(frame))
    header, _ = await asyncio.wait_for(read_frame(reader), 2)
    assert header.stype == SType.REJECT_REQ and header.byte3 == reason
    writer.close()


async def test_second_connection_gets_connection_exhaust(server: HsmsPassiveServer) -> None:
    first = await active(server.port)
    with pytest.raises(SelectRefused) as info:
        await active(server.port)
    assert info.value.status == SelectStatus.CONNECTION_EXHAUST
    assert first.state is State.SELECTED  # the established session is unaffected
    await first.separate()


async def test_select_twice_reports_already_active(server: HsmsPassiveServer) -> None:
    conn = await active(server.port)
    with pytest.raises(SelectRefused) as info:
        await conn.select()
    assert info.value.status == SelectStatus.ALREADY_ACTIVE
    await conn.separate()


async def test_separate_closes_passive_side(server: HsmsPassiveServer) -> None:
    conn = await active(server.port)
    await conn.separate()
    await wait_until(lambda: server.connection is not None and server.connection.closed, 2, "close")
    assert server.connection is not None and server.connection.close_reason == "peer sent Separate.req"


async def test_t5_retry_then_give_up() -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once the socket closes
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(ConnectionError):
        await connect_active("127.0.0.1", port, session_id=1, timeouts=T, handler=echo, attempts=3)
    assert loop.time() - t0 >= 2 * T.t5  # two T5 waits between three attempts
