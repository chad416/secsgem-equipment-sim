"""Stream 7 process program management and the recipe parser."""

from __future__ import annotations

import pytest

from gemsim.gem.process_programs import AckC7, PpGnt, ProcessProgramStore, RecipeError, parse_recipe
from gemsim.secs import U4, A, B, L

from .conftest import RECIPE, Bench


def test_parse_recipe() -> None:
    r = parse_recipe("R", RECIPE.encode())
    assert [s.name for s in r.steps] == ["HEAT", "SOAK"]
    assert r.steps[1].temp_c == 300 and r.steps[1].pressure_torr == 0.2 and r.steps[1].duration_s == 30


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"# only a comment\n",
        b"STEP X TEMP=1 PRESSURE=1\n",
        b"STEP X TEMP=1 PRESSURE=1 TIME=1 EXTRA=2\n",
        b"\xff",
    ],
)
def test_parse_recipe_rejects(body: bytes) -> None:
    with pytest.raises(RecipeError):
        parse_recipe("R", body)


def test_store_limits() -> None:
    store = ProcessProgramStore(max_programs=1, max_body=10)
    assert store.inquire("bad id!", 5) is PpGnt.INVALID_PPID
    assert store.inquire("A", 11) is PpGnt.NO_SPACE
    assert store.store("A", b"0123456789x") is AckC7.LENGTH_ERROR
    assert store.store("A", b"ok") is AckC7.ACCEPTED
    assert store.store("B", b"ok") is AckC7.MATRIX_OVERFLOW
    assert store.inquire("B", 2) is PpGnt.NO_SPACE
    assert store.inquire("A", 2) is PpGnt.OK  # overwrite of an existing PPID is allowed


async def test_download_upload_list_delete(bench: Bench) -> None:
    host = bench.host
    assert await host.pp_download("R1", RECIPE) == (PpGnt.OK, AckC7.ACCEPTED)
    assert await host.pp_download("R2", RECIPE) == (PpGnt.OK, AckC7.ACCEPTED)
    assert await host.pp_list() == ["R1", "R2"]
    assert await host.pp_upload("R1") == RECIPE
    assert await host.pp_upload("NOPE") is None  # S7F6 <L [0]>
    assert await host.pp_delete(["NOPE"]) == AckC7.PPID_NOT_FOUND
    assert await host.pp_delete(["R1"]) == AckC7.ACCEPTED
    assert await host.pp_list() == ["R2"]
    assert await host.pp_delete([]) == AckC7.ACCEPTED  # empty list = delete all
    assert await host.pp_list() == []


async def test_binary_ppbody_accepted(bench: Bench) -> None:
    host = bench.host
    reply = await host.request(7, 3, L(A("RB"), B(RECIPE.encode())))
    assert reply.body == B(b"\x00")
    assert await host.pp_upload("RB") == RECIPE


async def test_s7f6_returns_ppbody_in_the_downloaded_format(bench: Bench) -> None:
    host = bench.host
    raw = bytes(range(256))  # not ASCII: must come back byte for byte as <B>
    assert (await host.request(7, 3, L(A("BIN"), B(raw)))).body == B(b"\x00")
    assert (await host.request(7, 5, A("BIN"))).body == L(A("BIN"), B(raw))
    assert (await host.request(7, 3, L(A("TXT"), A(RECIPE)))).body == B(b"\x00")
    assert (await host.request(7, 5, A("TXT"))).body == L(A("TXT"), A(RECIPE))


async def test_s7f1_grant_codes_over_the_wire(bench: Bench) -> None:
    host = bench.host
    assert (await host.request(7, 1, L(A("has space"), U4(10)))).body == B(bytes([PpGnt.INVALID_PPID]))
    assert (await host.request(7, 1, L(A("BIG"), U4(10**6)))).body == B(bytes([PpGnt.NO_SPACE]))


async def test_deleting_selected_recipe_returns_to_idle(ready: Bench) -> None:
    eq = ready.equipment
    assert eq.selected is not None
    assert await ready.host.pp_delete(["TEST_RCP"]) == AckC7.ACCEPTED
    assert eq.selected is None and eq.process_state.name == "IDLE"
