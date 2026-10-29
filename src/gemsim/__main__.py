"""Command line entry point.

python -m gemsim demo                      # everything in one process
python -m gemsim controller                # OPC UA controller only
python -m gemsim equipment --online --remote
python -m gemsim host --scenario lot       # scripted host against a running tool
python -m gemsim host                      # interactive host shell
python -m gemsim inject VACUUM_LEAK        # maintenance: inject a controller fault
python -m gemsim coverage                  # SECS-II message coverage table (markdown)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shlex
import sys
from pathlib import Path

from .controller.interface import Fault
from .controller.link import MaintenanceClient, OpcUaLink
from .controller.model import ChamberController
from .controller.opcua_server import ControllerServer
from .coverage import coverage_markdown
from .demo import DEFAULT_RECIPE, ROOT, format_summary, quiet_third_party_logs, run_demo
from .gem.equipment import EquipmentConfig, GemEquipment
from .host.host import GemHost
from .host.scenario import run_lot_scenario
from .secs import u4_list
from .trace import SmlTrace

DEFAULT_OPCUA = "opc.tcp://127.0.0.1:4840/gemsim/"


async def _forever() -> None:
    await asyncio.Event().wait()


async def cmd_demo(args: argparse.Namespace) -> int:
    result, trace = await run_demo(
        out_dir=Path(args.out),
        time_scale=args.time_scale,
        wafers=args.wafers,
        fault_wafer=None if args.no_fault else args.fault_wafer,
        echo=None if args.quiet else sys.stdout,
    )
    out = Path(args.out)
    print()
    print(format_summary(result, trace), end="")
    print(f"full SML trace    : {display_path(out / 'demo_trace.sml')}")
    print(f"equipment log     : {display_path(out / 'demo_equipment.log')}")
    return 0 if result.ok else 1


def display_path(path: Path) -> str:
    """Path relative to the repository (or the working directory), never absolute."""
    resolved = path.resolve()
    for base in (ROOT, Path.cwd()):
        if resolved.is_relative_to(base):
            return resolved.relative_to(base).as_posix()
    return path.name


async def cmd_controller(args: argparse.Namespace) -> int:
    server = ControllerServer(ChamberController(), args.endpoint, time_scale=args.time_scale)
    await server.start()
    print(f"controller running on {args.endpoint} (x{args.time_scale:g}); Ctrl+C to stop")
    try:
        await _forever()
    finally:
        await server.stop()
    return 0


async def cmd_equipment(args: argparse.Namespace) -> int:
    cfg = EquipmentConfig(
        host=args.bind, port=args.port, initial_online=args.online, initial_remote=args.remote
    )
    equipment = GemEquipment(OpcUaLink(args.opcua), cfg)
    await equipment.start()
    print(f"GEM equipment on {args.bind}:{equipment.port}, controller {args.opcua}; Ctrl+C to stop")
    try:
        await _forever()
    finally:
        await equipment.stop()
    return 0


async def cmd_inject(args: argparse.Namespace) -> int:
    faults = Fault.NONE
    for name in args.faults:
        faults |= Fault[name]
    client = MaintenanceClient(args.opcua)
    await client.connect()
    await client.inject(faults)
    await client.close()
    print(f"Sim.Faults = {int(faults)} ({faults!r})")
    return 0


async def cmd_host(args: argparse.Namespace) -> int:
    trace_file = open(args.trace, "w", encoding="utf-8", newline="\n") if args.trace else None  # noqa: SIM115
    trace = SmlTrace("H", trace_file, echo=sys.stdout, echo_bodies=args.scenario is None)
    host = GemHost(args.address, args.port, trace=trace)
    try:
        if args.scenario == "lot":
            result = await run_lot_scenario(
                host, trace, recipe=Path(args.recipe), wafers=args.wafers, fault_wafer=None
            )
            print(format_summary(result, trace), end="")
            return 0 if result.ok else 1
        await host.connect()
        await asyncio.wait_for(host.communicating.wait(), 10)
        await interactive(host)
        return 0
    finally:
        await host.close()
        if trace_file:
            trace_file.close()


async def cmd_coverage(args: argparse.Namespace) -> int:
    text = coverage_markdown()
    if args.write:
        Path(args.write).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {args.write}")
    else:
        print(text, end="")
    return 0


HELP = """commands:
  sv [svid ...]                 S1F3 status variables (none = all)
  ec [ecid ...]                 S2F13 equipment constants
  online | offline              S1F17 / S1F15
  rcmd NAME [K=V ...]           S2F41 remote command, e.g. rcmd START LOTID=L1 WAFERS=2
  pp-download FILE              S7F1 + S7F3 (PPID = file stem)
  pp-list                       S7F19
  alarms                        S5F5 list alarms
  send S F [svid ...]           raw primary with optional <L <U4>...> body
  events                        show collection events received so far
  quit"""


async def interactive(host: GemHost) -> None:
    loop = asyncio.get_running_loop()
    print(HELP)
    while True:
        try:
            line = await loop.run_in_executor(None, input, "gem> ")
        except EOFError:
            return
        parts = shlex.split(line)
        if not parts:
            continue
        cmd, rest = parts[0].lower(), parts[1:]
        try:
            if cmd in ("quit", "exit"):
                return
            if cmd == "help":
                print(HELP)
            elif cmd == "sv":
                print(await host.status_variables([int(x) for x in rest]))
            elif cmd == "ec":
                print(await host.equipment_constants([int(x) for x in rest]))
            elif cmd == "online":
                print("ONLACK", await host.request_online())
            elif cmd == "offline":
                print("OFLACK", await host.request_offline())
            elif cmd == "rcmd":
                params = dict(p.split("=", 1) for p in rest[1:])
                print(await host.remote_command(rest[0], params))
            elif cmd == "pp-download":
                path = Path(rest[0])
                print(await host.pp_download(path.stem, path.read_text(encoding="ascii")))
            elif cmd == "pp-list":
                print(await host.pp_list())
            elif cmd == "alarms":
                print(await host.list_alarms())
            elif cmd == "send":
                s, f = int(rest[0]), int(rest[1])
                body = u4_list(int(x) for x in rest[2:]) if len(rest) > 2 else None
                await host.request(s, f, body)  # the reply is echoed by the trace
            elif cmd == "events":
                for e in host.events:
                    print(f"CEID {e.ceid} DATAID {e.dataid} {e.reports}")
            else:
                print("unknown command; type help")
        except Exception as exc:  # keep the shell alive
            print(f"error: {exc}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gemsim", description="SECS/GEM equipment simulator with OPC UA controller"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="run controller + equipment + host lot scenario")
    d.add_argument("--out", default=str(ROOT / "out"), help="output directory (default: out/)")
    d.add_argument("--time-scale", type=float, default=25.0)
    d.add_argument("--wafers", type=int, default=3)
    d.add_argument("--fault-wafer", type=int, default=2)
    d.add_argument("--no-fault", action="store_true")
    d.add_argument("--quiet", action="store_true", help="do not echo the trace to the console")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("controller", help="run the simulated chamber controller (OPC UA server)")
    c.add_argument("--endpoint", default=DEFAULT_OPCUA)
    c.add_argument("--time-scale", type=float, default=10.0)
    c.set_defaults(func=cmd_controller)

    e = sub.add_parser("equipment", help="run the GEM equipment (HSMS passive) against a controller")
    e.add_argument("--opcua", default=DEFAULT_OPCUA)
    e.add_argument("--bind", default="127.0.0.1")
    e.add_argument("--port", type=int, default=5000)
    e.add_argument("--online", action="store_true", help="start ON-LINE instead of EQUIPMENT OFF-LINE")
    e.add_argument("--remote", action="store_true", help="ON-LINE substate REMOTE instead of LOCAL")
    e.set_defaults(func=cmd_equipment)

    h = sub.add_parser("host", help="connect as host (interactive shell or scripted scenario)")
    h.add_argument("--address", default="127.0.0.1")
    h.add_argument("--port", type=int, default=5000)
    h.add_argument("--scenario", choices=["lot"])
    h.add_argument("--recipe", default=str(DEFAULT_RECIPE))
    h.add_argument("--wafers", type=int, default=2)
    h.add_argument("--trace", help="write the SML trace to this file")
    h.set_defaults(func=cmd_host)

    i = sub.add_parser("inject", help="maintenance: set controller fault bits")
    i.add_argument("faults", nargs="+", choices=list(Fault.__members__))
    i.add_argument("--opcua", default=DEFAULT_OPCUA)
    i.set_defaults(func=cmd_inject)

    m = sub.add_parser("coverage", help="print the SECS-II message coverage table (markdown)")
    m.add_argument("--write", metavar="PATH", help="write it to PATH instead of stdout")
    m.set_defaults(func=cmd_coverage)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # The demo raises the root level to INFO for its log file; keep the console as asked.
    for handler in logging.getLogger().handlers:
        handler.setLevel(level)
    quiet_third_party_logs()
    with contextlib.suppress(KeyboardInterrupt):
        return int(asyncio.run(args.func(args)))
    return 130


if __name__ == "__main__":
    sys.exit(main())
