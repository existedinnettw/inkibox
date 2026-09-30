"""Update PCB from Schematic on a board file."""

from __future__ import annotations

from pathlib import Path

from conftest import FakeLibs
from test_footprints import LIB_ID, board_footprint

from inkibox.kicad.sfile import SFile, format_node
from inkibox.update.netlist import Component, Pin
from inkibox.update.options import FootprintOptions, PcbOptions
from inkibox.update.pcb import update_pcb

PATH = "/bbbbbbbb-0000-0000-0000-000000000000"


def component(**over) -> Component:
    base = {
        "ref": "R1",
        "value": "4k7",
        "footprint": LIB_ID,
        "fields": {"Datasheet": "r.pdf", "Description": "Resistor", "LCSC": "C23162"},
        "properties": {"Sheetfile": "x.kicad_sch", "ki_fp_filters": "R_*"},
        "sheet_names": "/",
        "sheet_tstamps": "/",
        "uuids": [PATH.lstrip("/")],
        "pins": {"1": Pin("+3V3", None, "passive"), "2": Pin("GND", None, "passive")},
    }
    base.update(over)
    return Component(**base)  # type: ignore[arg-type]


def board(tmp_path: Path, *footprints: str, extra: str = "") -> SFile:
    p = tmp_path / "x.kicad_pcb"
    body = "".join("\n\t" + f for f in footprints)
    p.write_text(
        "(kicad_pcb\n\t(version 20260206)"
        + body
        + extra
        + '\n\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 50 40)\n\t\t(layer "Edge.Cuts")\n\t)\n)\n'
    )
    return SFile.load(p)


def run(pcb: SFile, comps: list[Component], attrs=None, **opts):
    return update_pcb(
        pcb,
        {c.ref: c for c in comps},
        attrs or {},
        FakeLibs(),  # type: ignore[arg-type]
        PcbOptions(**opts),
        FootprintOptions(),
        pcb.path.parent,
    )


def test_fields_nets_filters_and_attributes_follow_the_symbol(tmp_path: Path):
    """
    Given R1 on the board (10k, VCC/GND, DNP) and its symbol now 4k7 on +3V3/GND, populated
    When the board is updated
    Then value, fields, pad nets, filters and DNP follow; the designer's field keeps its place
    """
    pcb = board(tmp_path, format_node(board_footprint(), 1))
    report = run(pcb, [component()], attrs={PATH: set()})
    text = pcb.render()
    assert '(property "Value" "4k7"' in text
    assert '(property "LCSC" "C23162"\n\t\t\t(at 0 0 0)' in text  # updated in place
    assert '(property "Datasheet" "r.pdf"' in text  # added
    assert '(property ki_fp_filters "R_*")' in text
    assert '(net "+3V3")' in text and '(net "VCC")' not in text
    assert "(attr smd)" in text
    assert "R1: pads 1 VCC -> +3V3" in report.changes


def test_an_up_to_date_board_is_not_touched(tmp_path: Path):
    pcb = board(tmp_path, format_node(board_footprint(), 1))
    run(pcb, [component()], attrs={PATH: set()})
    pcb.save()
    again = SFile.load(pcb.path)
    report = run(again, [component()], attrs={PATH: set()})
    assert report.changes == [] and not again.changed()


def test_tracks_follow_a_renamed_net(tmp_path: Path):
    """
    Given a track on VCC and the schematic renaming VCC to +3V3
    When the board is updated
    Then the track moves to +3V3
    """
    track = '\n\t(segment\n\t\t(start 1 1)\n\t\t(end 2 2)\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n\t\t(net "VCC")\n\t)'
    pcb = board(tmp_path, format_node(board_footprint(), 1), extra=track)
    report = run(pcb, [component()])
    assert (
        "net 'VCC' renamed '+3V3': 1 track(s), via(s) or zone(s) follow"
        in report.changes
    )
    assert '(net "VCC")' not in pcb.render()


def test_unlinked_footprint_is_linked_by_reference_and_strays_are_reported(
    tmp_path: Path,
):
    unlinked = format_node(board_footprint(), 1).replace(f'(path "{PATH}")', "")
    stray = (
        format_node(board_footprint(), 1)
        .replace('"R1"', '"R9"')
        .replace(PATH, "/99999999-0000-0000-0000-000000000000")
    )
    pcb = board(tmp_path, unlinked, stray)
    report = run(pcb, [component()])
    assert "R1: unlinked footprint linked to its symbol by reference" in report.changes
    assert f'(path "{PATH}")' in pcb.render()
    assert report.warnings == ["R9: no symbol in the schematic (kept)"]
    assert (
        run(board(tmp_path, unlinked), [component()], link_unlinked=False)
        .changes[0]
        .startswith("R1: added")
    )


def test_new_symbol_gets_a_footprint_below_the_board(tmp_path: Path):
    pcb = board(tmp_path)
    report = run(pcb, [component()])
    assert report.changes[0] == f"R1: added {LIB_ID} at (0.0, 50.0); place it"
    text = pcb.render()
    assert (
        f'(footprint "{LIB_ID}"' in text
        and '(net "+3V3")' in text
        and '(property "Reference" "R1"' in text
    )
