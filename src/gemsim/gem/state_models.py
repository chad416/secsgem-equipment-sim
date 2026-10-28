"""GEM state models (SEMI E30): communication state and control state.

Control state model (E30 section 4.2), transition numbers as used in DESIGN.md::

                        +------------------------- OFF-LINE -------------------------+
     (1) start  ------> | EQUIPMENT OFF-LINE --(4) operator ON--> ATTEMPT ON-LINE    |
                        |        ^                                   |  (5) S1F0/T3  |
                        |        |(10) operator OFF                  v               |
                        |        +------------------------------ HOST OFF-LINE       |
                        +-------------------------------------------^------|---------+
                             (7) operator OFF   (8) S1F15            |      | (9) S1F17
                        +----------------------- ON-LINE -----------|------v---------+
     (6) S1F2 --------> |      LOCAL  <--(11) operator LOCAL/REMOTE-->  REMOTE        |
                        +------------------------------------------------------------+
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable

from .ids import ControlState

log = logging.getLogger(__name__)


class OnlAck(enum.IntEnum):
    ACCEPTED = 0
    NOT_ALLOWED = 1
    ALREADY_ONLINE = 2


class OflAck(enum.IntEnum):
    ACCEPTED = 0


class CommState(enum.Enum):
    """E30 communication state model (ENABLED substates; DISABLED is not modelled
    because the simulator always enables communication at start-up)."""

    WAIT_CRA = "NOT COMMUNICATING / WAIT CRA"
    WAIT_DELAY = "NOT COMMUNICATING / WAIT DELAY"
    COMMUNICATING = "COMMUNICATING"


TransitionListener = Callable[[ControlState, ControlState], None]


class ControlStateMachine:
    def __init__(
        self,
        *,
        initial_online: bool = False,
        remote: bool = False,
        offline_after_failed_attempt: ControlState = ControlState.HOST_OFFLINE,
    ) -> None:
        if offline_after_failed_attempt not in (ControlState.EQUIPMENT_OFFLINE, ControlState.HOST_OFFLINE):
            raise ValueError("failed attempt must end in an OFF-LINE substate")
        self.remote_switch = remote
        self.offline_after_failed_attempt = offline_after_failed_attempt
        self.state = self._online_substate() if initial_online else ControlState.EQUIPMENT_OFFLINE
        self.previous = self.state
        self.listeners: list[TransitionListener] = []

    def _online_substate(self) -> ControlState:
        return ControlState.ONLINE_REMOTE if self.remote_switch else ControlState.ONLINE_LOCAL

    def _go(self, new: ControlState) -> None:
        if new is self.state:
            return
        old = self.state
        self.previous, self.state = old, new
        log.info("control state %s -> %s", old.name, new.name)
        for cb in list(self.listeners):
            cb(old, new)

    @property
    def online(self) -> bool:
        return self.state.online

    @property
    def remote(self) -> bool:
        return self.state is ControlState.ONLINE_REMOTE

    # --- operator panel ------------------------------------------------------
    def operator_online(self) -> bool:
        """Transition 4. Returns True if the caller must now send S1F1."""
        if self.state is ControlState.EQUIPMENT_OFFLINE:
            self._go(ControlState.ATTEMPT_ONLINE)
            return True
        return False

    def attempt_result(self, host_answered: bool) -> None:
        """Transitions 5 (S1F0 or T3 timeout) and 6 (S1F2 received)."""
        if self.state is not ControlState.ATTEMPT_ONLINE:
            return
        self._go(self._online_substate() if host_answered else self.offline_after_failed_attempt)

    def operator_offline(self) -> None:
        """Transitions 7 and 10."""
        if self.state is not ControlState.EQUIPMENT_OFFLINE:
            self._go(ControlState.EQUIPMENT_OFFLINE)

    def operator_set_remote(self, remote: bool) -> None:
        """Transition 11. The switch position is remembered while off-line."""
        self.remote_switch = remote
        if self.online:
            self._go(self._online_substate())

    # --- host requests ---------------------------------------------------------
    def host_request_online(self) -> OnlAck:
        """S1F17, transition 9."""
        if self.online:
            return OnlAck.ALREADY_ONLINE
        if self.state is ControlState.HOST_OFFLINE:
            self._go(self._online_substate())
            return OnlAck.ACCEPTED
        return OnlAck.NOT_ALLOWED

    def host_request_offline(self) -> OflAck:
        """S1F15, transition 8 (E30 defines only OFLACK 0)."""
        if self.online:
            self._go(ControlState.HOST_OFFLINE)
        return OflAck.ACCEPTED
