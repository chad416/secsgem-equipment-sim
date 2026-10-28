"""Dynamic event reports: S2F33 / S2F35 / S2F37 acknowledge codes and S6F11 content."""

from __future__ import annotations

import pytest

from gemsim.gem.ids import CE, DV, SV
from gemsim.gem.reports import DrAck, ErAck, LrAck, ReportManager
from gemsim.secs import U4, A, Boolean, L

from .conftest import Bench

KNOWN = {1, 2, 3}


@pytest.fixture
def rm() -> ReportManager:
    return ReportManager([100, 200], lambda v: v in KNOWN)


def test_define_ack_codes(rm: ReportManager) -> None:
    assert rm.define([(10, [1, 2])]) is DrAck.OK
    assert rm.define([(10, [3])]) is DrAck.RPTID_ALREADY_DEFINED
    assert rm.define([(11, [1, 99])]) is DrAck.VID_UNKNOWN
    assert rm.define([(12, [1]), (12, [2])]) is DrAck.INVALID_FORMAT
    assert set(rm.reports) == {10}  # failed requests changed nothing


def test_define_is_atomic(rm: ReportManager) -> None:
    assert rm.define([(20, [1]), (21, [99])]) is DrAck.VID_UNKNOWN
    assert 20 not in rm.reports


def test_insufficient_space() -> None:
    rm = ReportManager([100], lambda v: True, max_reports=2)
    assert rm.define([(1, [1]), (2, [1]), (3, [1])]) is DrAck.INSUFFICIENT_SPACE


def test_delete_single_report_removes_its_links(rm: ReportManager) -> None:
    rm.define([(10, [1]), (11, [2])])
    assert rm.link([(100, [10, 11])]) is LrAck.OK
    assert rm.define([(10, [])]) is DrAck.OK
    assert rm.links == {100: [11]}


def test_delete_all_reports(rm: ReportManager) -> None:
    rm.define([(10, [1])])
    rm.link([(100, [10])])
    assert rm.define([]) is DrAck.OK
    assert rm.reports == {} and rm.links == {}


def test_link_ack_codes(rm: ReportManager) -> None:
    rm.define([(10, [1]), (11, [2])])
    assert rm.link([(999, [10])]) is LrAck.CEID_UNKNOWN
    assert rm.link([(100, [55])]) is LrAck.RPTID_UNKNOWN
    assert rm.link([(100, [10])]) is LrAck.OK
    assert rm.link([(100, [11])]) is LrAck.CEID_LINK_ALREADY_DEFINED
    assert rm.link([(100, [])]) is LrAck.OK  # unlink
    assert rm.link([(100, [11])]) is LrAck.OK
    assert rm.link([(200, [10, 10])]) is LrAck.INVALID_FORMAT


def test_enable_ack_codes(rm: ReportManager) -> None:
    assert rm.enable(True, [100, 999]) is ErAck.CEID_UNKNOWN
    assert rm.enabled == set()
    assert rm.enable(True, []) is ErAck.OK and rm.enabled == {100, 200}
    assert rm.enable(False, [200]) is ErAck.OK and rm.enabled == {100}


async def test_acks_over_the_wire(bench: Bench) -> None:
    host = bench.host
    assert await host.define_reports({1: [SV.CHAMBER_PRESSURE]}) == DrAck.OK
    assert await host.define_reports({1: [SV.CHAMBER_PRESSURE]}) == DrAck.RPTID_ALREADY_DEFINED
    assert await host.define_reports({2: [424242]}) == DrAck.VID_UNKNOWN
    assert await host.link_events({4242: [1]}) == LrAck.CEID_UNKNOWN
    assert await host.link_events({CE.PP_SELECTED: [7]}) == LrAck.RPTID_UNKNOWN
    assert await host.enable_events([4242]) == ErAck.CEID_UNKNOWN


async def test_disabled_event_is_not_sent_and_dv_outside_context_is_empty(bench: Bench) -> None:
    host = bench.host
    # report mixes an SV, an EC and a DV that is not valid for PP_SELECTED
    assert await host.define_reports({1: [SV.PP_EXEC_NAME, 3003, DV.ALARM_ID, DV.PP_SELECTED]}) == 0
    assert await host.link_events({CE.PP_SELECTED: [1]}) == 0
    assert await host.pp_download("P1", "STEP A TEMP=100 PRESSURE=1 TIME=5\n") == (0, 0)
    since = host.cursor()
    assert (await host.remote_command("PP-SELECT", {"PPID": "P1"}))[0] == 0
    assert host.events[since:] == []  # linked but not enabled
    assert await host.enable_events([CE.PP_SELECTED]) == 0
    assert (await host.remote_command("PP-SELECT", {"PPID": "P1"}))[0] == 0
    ev = await host.wait_event(CE.PP_SELECTED, since=since, timeout=5)
    assert ev.reports == {1: ["P1", 90.0, [], "P1"]}


async def test_s6f15_event_report_request_and_s6f19(bench: Bench) -> None:
    host = bench.host
    await host.define_reports({5: [SV.MDLN, SV.SOFTREV]})
    await host.link_events({CE.LOT_STARTED: [5]})
    reply = await host.request(6, 15, U4(CE.LOT_STARTED))
    body = reply.body
    assert isinstance(body, L)
    assert body[1] == U4(CE.LOT_STARTED)
    assert body[2] == L(L(U4(5), L(A("GEMSIM-PC1"), A("0.1.0"))))
    rpt = await host.request(6, 19, U4(5))
    assert rpt.body == L(A("GEMSIM-PC1"), A("0.1.0"))


async def test_events_enabled_sv_reflects_s2f37(bench: Bench) -> None:
    host = bench.host
    await host.request(2, 37, L(Boolean(True), L(U4(CE.LOT_STARTED), U4(CE.LOT_COMPLETED))))
    assert await host.status_variables([SV.EVENTS_ENABLED]) == [[CE.LOT_STARTED, CE.LOT_COMPLETED]]
