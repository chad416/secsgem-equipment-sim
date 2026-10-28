"""E30 control state model: unit transitions and the behaviour seen by a host."""

from __future__ import annotations

import pytest

from gemsim.gem.ids import CE, SV, ControlState
from gemsim.gem.state_models import ControlStateMachine, OnlAck
from gemsim.host.host import TransactionAborted

from .conftest import configure_reports, make_bench

EQ_OFF, ATTEMPT, HOST_OFF = (
    ControlState.EQUIPMENT_OFFLINE,
    ControlState.ATTEMPT_ONLINE,
    ControlState.HOST_OFFLINE,
)
LOCAL, REMOTE = ControlState.ONLINE_LOCAL, ControlState.ONLINE_REMOTE


def test_initial_states() -> None:
    assert ControlStateMachine().state is EQ_OFF
    assert ControlStateMachine(initial_online=True).state is LOCAL
    assert ControlStateMachine(initial_online=True, remote=True).state is REMOTE


def test_operator_online_attempt_succeeds_and_fails() -> None:
    m = ControlStateMachine()
    assert m.operator_online() is True and m.state is ATTEMPT
    m.attempt_result(host_answered=True)
    assert m.state is LOCAL
    m.operator_offline()
    assert m.state is EQ_OFF
    m.operator_online()
    m.attempt_result(host_answered=False)
    assert m.state is HOST_OFF  # default "OFF-LINE substate after failed attempt"
    m2 = ControlStateMachine(offline_after_failed_attempt=EQ_OFF)
    m2.operator_online()
    m2.attempt_result(False)
    assert m2.state is EQ_OFF


@pytest.mark.parametrize(
    ("start", "expected_ack", "end"),
    [
        (EQ_OFF, OnlAck.NOT_ALLOWED, EQ_OFF),
        (HOST_OFF, OnlAck.ACCEPTED, LOCAL),
        (LOCAL, OnlAck.ALREADY_ONLINE, LOCAL),
        (REMOTE, OnlAck.ALREADY_ONLINE, REMOTE),
    ],
)
def test_s1f17_request_online(start: ControlState, expected_ack: OnlAck, end: ControlState) -> None:
    m = ControlStateMachine(initial_online=start is not EQ_OFF, remote=start is REMOTE)
    if start is HOST_OFF:
        m.host_request_offline()
    assert m.state is start
    assert m.host_request_online() is expected_ack
    assert m.state is end


def test_local_remote_switch_and_memory_while_offline() -> None:
    m = ControlStateMachine(initial_online=True)
    seen: list[tuple[ControlState, ControlState]] = []
    m.listeners.append(lambda a, b: seen.append((a, b)))
    m.operator_set_remote(True)
    m.host_request_offline()
    m.host_request_online()  # switch position REMOTE is remembered
    assert seen == [(LOCAL, REMOTE), (REMOTE, HOST_OFF), (HOST_OFF, REMOTE)]
    m.operator_offline()
    assert m.state is EQ_OFF and m.previous is REMOTE


async def test_offline_equipment_aborts_everything_but_s1f13_s1f17() -> None:
    b = await make_bench(online=False, remote=False)
    try:
        with pytest.raises(TransactionAborted):
            await b.host.status_variables([SV.CONTROL_STATE])
        with pytest.raises(TransactionAborted):
            await b.host.are_you_there()
        assert await b.host.establish_communication() == 0
        assert await b.host.request_online() == OnlAck.NOT_ALLOWED  # EQUIPMENT OFF-LINE
        # operator turns ON-LINE: equipment sends S1F1, the host answers S1F2
        assert await b.equipment.operator_online() is LOCAL
        assert await b.host.status_variables([SV.CONTROL_STATE]) == [int(LOCAL)]
    finally:
        await b.host.close()
        await b.equipment.stop()


async def test_host_offline_online_cycle_reports_events() -> None:
    b = await make_bench(online=True, remote=False)
    try:
        await configure_reports(b.host)
        assert await b.host.link_events({CE.EQUIPMENT_OFFLINE: [1]}) == 0
        since = b.host.cursor()
        b.equipment.operator_set_remote(True)
        ev = await b.host.wait_event(CE.CONTROL_STATE_REMOTE, since=since, timeout=5)
        assert ev.reports[1][0] == REMOTE
        assert await b.host.request_offline() == 0
        off = await b.host.wait_event(CE.EQUIPMENT_OFFLINE, since=since, timeout=5)
        assert off.reports[1][0] == HOST_OFF
        with pytest.raises(TransactionAborted):
            await b.host.status_variables([SV.CONTROL_STATE])
        assert await b.host.request_online() == OnlAck.ACCEPTED
        back = await b.host.wait_event(CE.CONTROL_STATE_REMOTE, since=since + 2, timeout=5)
        assert back.reports[1][0] == REMOTE
    finally:
        await b.host.close()
        await b.equipment.stop()


async def test_attempt_online_without_communication_goes_host_offline() -> None:
    b = await make_bench(online=False)
    try:
        b.equipment.comm = b.equipment.comm.WAIT_DELAY  # as if S1F13 had not been answered yet
        assert await b.equipment.operator_online() is HOST_OFF
    finally:
        await b.host.close()
        await b.equipment.stop()
