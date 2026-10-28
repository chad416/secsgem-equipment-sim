"""Cyclic execution of the controller model (the PLC "scan cycle")."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Protocol

from .model import ChamberController

log = logging.getLogger(__name__)


class ScanIO(Protocol):
    """Process image exchange around each scan (read inputs, write outputs)."""

    async def read_inputs(self, controller: ChamberController) -> None: ...

    async def write_outputs(self, controller: ChamberController) -> None: ...


class ScanLoop:
    """Runs ``controller.scan`` every ``scan_s`` seconds of wall time.

    ``time_scale`` speeds the process up: with ``time_scale=20`` one real second
    advances the recipe by twenty process seconds. The physics use exact
    discrete updates, so a larger process step per scan stays stable.
    """

    def __init__(
        self,
        controller: ChamberController,
        *,
        time_scale: float = 1.0,
        scan_s: float = 0.05,
        io: ScanIO | None = None,
    ) -> None:
        self.controller = controller
        self.time_scale = time_scale
        self.scan_s = scan_s
        self.io = io
        self.process_time = 0.0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="controller-scan")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        last = time.monotonic()
        while True:
            start = time.monotonic()
            # Cap dt so a stalled event loop cannot make the process jump ahead.
            dt = min(start - last, 5 * self.scan_s) * self.time_scale
            last = start
            try:
                if self.io:
                    await self.io.read_inputs(self.controller)
                self.controller.scan(dt)
                self.process_time += dt
                if self.io:
                    await self.io.write_outputs(self.controller)
            except Exception:
                log.exception("controller scan failed")
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, self.scan_s - elapsed))
