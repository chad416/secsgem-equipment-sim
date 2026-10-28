"""Fault -> alarm paths, S5F3 enable/disable, S5F5/S5F7 listing, bridge watchdog."""

from __future__ import annotations

import asyncio

from gemsim.controller.interface import Fault, SeqState
from gemsim.gem.ids import ALID, CE, SV
from gemsim.gem.remote_commands import HcAck
from gemsim.secs import U4, B, L

from .conftest import Bench, wait_until
from .test_remote_commands import wait_seq


async def test_robot_stall_alarm_and_abort(ready: Bench) -> None:
    host, link = ready.host, ready.link
    await link.inject(Fault.ROBOT_STALL)
    since, mark = host.cursor(), len(host.alarms)
    await host.remote_command("START", {"LOTID": "LR"})
    alarm = await host.wait_alarm(ALID.ROBOT_TIMEOUT, set_=True, since=mark, timeout=20)
    assert alarm.alcd == 0x86 and alarm.text == "Wafer handling robot move timeout"
    ce = await host.wait_event(CE.ALARM_SET, since=since)
    assert ce.reports[5] == [ALID.ROBOT_TIMEOUT, "Wafer handling robot move timeout"]
    await link.inject(Fault.NONE)
    assert (await host.remote_command("ABORT"))[0] == HcAck.ACK_FINISH_LATER
    await host.wait_alarm(ALID.ROBOT_TIMEOUT, set_=False, since=mark)
    await host.wait_event(CE.LOT_ABORTED, since=since)


async def test_over_temperature_interlock(ready: Bench) -> None:
    host, link = ready.host, ready.link
    since, mark = host.cursor(), len(host.alarms)
    await host.remote_command("START", {"LOTID": "LT"})
    await wait_seq(ready, SeqState.PROCESSING)
    await link.inject(Fault.HEATER_RUNAWAY)
    alarm = await host.wait_alarm(ALID.OVER_TEMPERATURE, set_=True, since=mark, timeout=20)
    assert alarm.alcd == 0x82  # category 2: equipment safety
    # The chuck cools within milliseconds at x100, so the "still hot" interlock is
    # covered deterministically in test_controller_model; here: cool, then resume.
    await link.inject(Fault.NONE)
    await wait_until(lambda: link.status["Chamber.Temperature"] < 420.0, 10, "chuck cooled")
    assert (await host.remote_command("RESUME"))[0] == HcAck.ACK_FINISH_LATER
    await host.wait_alarm(ALID.OVER_TEMPERATURE, set_=False, since=mark)
    done = await host.wait_event(CE.LOT_COMPLETED, since=since, timeout=20)
    assert done.reports[4] == ["LT", 1]


async def test_interlock_result_maps_to_hcack_2(ready: Bench) -> None:
    ready.link.controller.wafer_present = True  # wafer left in the chamber
    assert await ready.host.remote_command("START", {"LOTID": "LW"}) == (HcAck.CANNOT_PERFORM_NOW, [])


async def test_disabled_alarm_sends_no_s5f1_but_is_still_tracked(ready: Bench) -> None:
    host, link = ready.host, ready.link
    assert await host.enable_alarms([ALID.VACUUM_NOT_REACHED], enable=False) == 0
    assert ALID.VACUUM_NOT_REACHED not in (await host.status_variables([SV.ALARMS_ENABLED]))[0]
    since, mark = host.cursor(), len(host.alarms)
    await link.inject(Fault.VACUUM_LEAK)
    await host.remote_command("START", {"LOTID": "LD"})
    await host.wait_event(CE.ALARM_SET, since=since, timeout=20)  # the CE is independent of S5F3
    assert host.alarms[mark:] == []
    rows = await host.list_alarms([ALID.VACUUM_NOT_REACHED])
    assert rows == [[b"\x84", ALID.VACUUM_NOT_REACHED, "Pump-down did not reach base pressure"]]
    assert await host.status_variables([SV.ALARMS_SET]) == [[ALID.VACUUM_NOT_REACHED]]
    enabled = (await host.request(5, 7)).body
    assert isinstance(enabled, L) and ALID.VACUUM_NOT_REACHED not in [row[1].to_python() for row in enabled]


async def test_s5f3_unknown_alid_and_bad_aled(bench: Bench) -> None:
    host = bench.host
    assert await host.enable_alarms([999]) == 1  # ACKC5 1: error
    reply = await host.request(5, 3, L(B(b"\x80"), U4()))  # empty vector = all alarms
    assert reply.body == B(b"\x00")
    assert sorted(await host.status_variables([SV.ALARMS_ENABLED]))[0] == [1, 2, 3, 10]


async def test_controller_heartbeat_loss_raises_link_alarm(ready: Bench) -> None:
    host, link, eq = ready.host, ready.link, ready.equipment
    eq.cfg.watchdog_s = 0.3
    mark = len(host.alarms)
    await link.loop.stop()  # controller stops scanning: heartbeat freezes
    alarm = await host.wait_alarm(ALID.CONTROLLER_LINK_LOST, set_=True, since=mark, timeout=5)
    assert alarm.alcd == 0x88
    assert await host.status_variables([SV.CONTROLLER_LINK_UP]) == [False]
    assert (await host.remote_command("START", {"LOTID": "LX"}))[0] == HcAck.CANNOT_PERFORM_NOW
    link.loop.start()
    await host.wait_alarm(ALID.CONTROLLER_LINK_LOST, set_=False, since=mark, timeout=5)
    await asyncio.sleep(0)
    assert await host.status_variables([SV.CONTROLLER_LINK_UP]) == [True]
