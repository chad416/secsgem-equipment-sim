"""Dynamic event reports (SEMI E30 4.3): S2F33 define, S2F35 link, S2F37 enable.

All three requests are validated completely before anything is changed: a
request that returns a non-zero ACK leaves the configuration untouched.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Sequence


class DrAck(enum.IntEnum):
    OK = 0
    INSUFFICIENT_SPACE = 1
    INVALID_FORMAT = 2
    RPTID_ALREADY_DEFINED = 3
    VID_UNKNOWN = 4


class LrAck(enum.IntEnum):
    OK = 0
    INSUFFICIENT_SPACE = 1
    INVALID_FORMAT = 2
    CEID_LINK_ALREADY_DEFINED = 3
    CEID_UNKNOWN = 4
    RPTID_UNKNOWN = 5


class ErAck(enum.IntEnum):
    OK = 0
    CEID_UNKNOWN = 1


class ReportManager:
    def __init__(
        self,
        ceids: Iterable[int],
        vid_exists: Callable[[int], bool],
        *,
        max_reports: int = 100,
    ) -> None:
        self.ceids = set(ceids)
        self._vid_exists = vid_exists
        self.max_reports = max_reports
        self.reports: dict[int, list[int]] = {}
        self.links: dict[int, list[int]] = {}
        self.enabled: set[int] = set()

    def define(self, definitions: Sequence[tuple[int, Sequence[int]]]) -> DrAck:
        if not definitions:
            # S2F33 with an empty list deletes every report and every link.
            self.reports.clear()
            self.links.clear()
            return DrAck.OK
        seen: set[int] = set()
        new_count = 0
        for rptid, vids in definitions:
            if rptid in seen:
                return DrAck.INVALID_FORMAT
            seen.add(rptid)
            if not vids:
                continue  # deletion of rptid (unknown rptid is accepted silently)
            if rptid in self.reports:
                return DrAck.RPTID_ALREADY_DEFINED
            if not all(self._vid_exists(v) for v in vids):
                return DrAck.VID_UNKNOWN
            new_count += 1
        if len(self.reports) + new_count > self.max_reports:
            return DrAck.INSUFFICIENT_SPACE
        for rptid, vids in definitions:
            if vids:
                self.reports[rptid] = list(vids)
            else:
                self.reports.pop(rptid, None)
                for ceid in list(self.links):
                    self.links[ceid] = [r for r in self.links[ceid] if r != rptid]
                    if not self.links[ceid]:
                        del self.links[ceid]
        return DrAck.OK

    def link(self, links: Sequence[tuple[int, Sequence[int]]]) -> LrAck:
        seen: set[int] = set()
        for ceid, rptids in links:
            if ceid in seen or len(set(rptids)) != len(rptids):
                return LrAck.INVALID_FORMAT
            seen.add(ceid)
            if ceid not in self.ceids:
                return LrAck.CEID_UNKNOWN
            if not all(r in self.reports for r in rptids):
                return LrAck.RPTID_UNKNOWN
            if rptids and self.links.get(ceid):
                return LrAck.CEID_LINK_ALREADY_DEFINED
        for ceid, rptids in links:
            if rptids:
                self.links[ceid] = list(rptids)
            else:
                self.links.pop(ceid, None)  # empty RPTID list unlinks the CEID
        return LrAck.OK

    def enable(self, enable: bool, ceids: Sequence[int]) -> ErAck:
        targets = set(ceids) if ceids else set(self.ceids)
        if not targets <= self.ceids:
            return ErAck.CEID_UNKNOWN
        if enable:
            self.enabled |= targets
        else:
            self.enabled -= targets
        return ErAck.OK

    def reports_for(self, ceid: int) -> list[tuple[int, list[int]]]:
        return [(r, self.reports[r]) for r in self.links.get(ceid, []) if r in self.reports]
