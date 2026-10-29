"""End to end: controller (OPC UA server) + GEM equipment (OPC UA client, HSMS
passive) + scripted host (HSMS active), all in one process over real sockets."""

from __future__ import annotations

from pathlib import Path

from gemsim.controller.link import OpcUaLink
from gemsim.controller.model import ChamberController
from gemsim.controller.opcua_server import ControllerServer
from gemsim.demo import free_port, run_demo
from gemsim.gem.equipment import EquipmentConfig, GemEquipment
from gemsim.gem.ids import ALID, SV
from gemsim.host.host import GemHost

from .conftest import FAST, RECIPE


async def test_demo_lot_with_fault_and_recovery(tmp_path: Path) -> None:
    result, trace = await run_demo(out_dir=tmp_path, time_scale=60.0, wafers=2, fault_wafer=2, echo=None)
    assert result.ok, result
    assert result.wafers_reported == 2
    assert result.hcacks["RESUME"] == 4
    assert result.alarms == [
        "SET ALID 1 ALCD 0x84 'Pump-down did not reach base pressure'",
        "CLEARED ALID 1 ALCD 0x04",
    ]
    text = (tmp_path / "demo_trace.sml").read_text(encoding="utf-8")
    assert "Separate.req" in text and "Deselect.req" not in text  # HSMS-SS: end with Separate
    assert "S5F1 W" in text and "<B [1] 0x84>" in text and "<B [1] 0x04>" in text
    assert trace.counts["S6F11"] == trace.counts["S6F12"] > 20
    assert (tmp_path / "demo_summary.txt").read_text(encoding="utf-8").startswith("result            : PASS")
    log = (tmp_path / "demo_equipment.log").read_text(encoding="utf-8")
    assert "Machine controller link lost" not in log  # orderly shutdown is not a link failure


async def test_controller_loss_and_reconnect_over_opcua() -> None:
    """PLC goes away: ALID 10, HCACK 2. PLC comes back: link reconnects, ALID 10 clears."""
    endpoint = f"opc.tcp://127.0.0.1:{free_port()}/gemsim/"
    server = ControllerServer(ChamberController(), endpoint, time_scale=10)
    await server.start()
    equipment = GemEquipment(
        OpcUaLink(endpoint, health_s=0.25, retry_min_s=0.2, retry_max_s=0.5),
        EquipmentConfig(port=0, timeouts=FAST, initial_online=True, initial_remote=True, watchdog_s=1.0),
    )
    await equipment.start()
    host = GemHost(port=equipment.port, timeouts=FAST)
    restarted: ControllerServer | None = None
    try:
        await host.connect()
        await host.establish_communication()
        assert await host.enable_alarms() == 0
        assert await host.status_variables([SV.CONTROLLER_LINK_UP]) == [True]
        mark = len(host.alarms)
        await server.stop()  # the PLC goes away
        alarm = await host.wait_alarm(ALID.CONTROLLER_LINK_LOST, set_=True, since=mark, timeout=10)
        assert alarm.text == "Machine controller link lost"
        # with the controller gone every remote command is refused: HCACK 2
        assert (await host.remote_command("PP-SELECT", {"PPID": "X"}))[0] == 2
        assert (await host.remote_command("STOP"))[0] == 2
        assert await host.status_variables([SV.CONTROLLER_LINK_UP]) == [False]

        restarted = ControllerServer(ChamberController(), endpoint, time_scale=10)
        await restarted.start()  # PLC back on the same endpoint
        await host.wait_alarm(ALID.CONTROLLER_LINK_LOST, set_=False, since=mark, timeout=15)
        assert await host.status_variables([SV.CONTROLLER_LINK_UP]) == [True]
        # commands reach the new controller again: a recipe can be downloaded and selected
        assert await host.pp_download("R1", RECIPE) == (0, 0)
        assert await host.remote_command("PP-SELECT", {"PPID": "R1"}) == (0, [])
        assert restarted.controller.recipe_name == "R1"
    finally:
        await host.close()
        await equipment.stop()
        if restarted is not None:
            await restarted.stop()
