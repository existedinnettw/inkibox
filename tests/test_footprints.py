"""Placing library footprints on a board side and exchanging them (Update Footprints)."""

from __future__ import annotations

from conftest import R_FOOTPRINT, FakeLibs

from inkibox.kicad.sfile import Node, SFile, format_node, parse
from inkibox.kicad.sfile import write_text as write_lf
from inkibox.update.footprints import exchange, place, same_footprint, update_footprints
from inkibox.update.options import FootprintOptions

LIB_ID = "Resistor_SMD:R_0603_1608Metric"


def board_footprint(side: str = "F.Cu", rot: str = "-90") -> Node:
    """
    Given nothing
    When called
    Then a footprint as a board holds it: fields placed by the designer, nets on the pads
    """
    fp = parse(
        f"""(footprint "{LIB_ID}"
\t(layer "{side}")
\t(uuid "aaaaaaaa-0000-0000-0000-000000000000")
\t(at 10 20 {rot})
\t(property "Reference" "R1"
\t\t(at 1.5 -2 45)
\t\t(layer "{side[0]}.SilkS")
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000001")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 0.8 0.8)
\t\t\t\t(thickness 0.12)
\t\t\t)
\t\t)
\t)
\t(property "Value" "10k"
\t\t(at 0 1.43 0)
\t\t(layer "{side[0]}.Fab")
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000002")
\t)
\t(property "LCSC" "C25804"
\t\t(at 0 0 0)
\t\t(layer "{side[0]}.Fab")
\t\t(hide yes)
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000003")
\t)
\t(path "/bbbbbbbb-0000-0000-0000-000000000000")
\t(sheetname "/")
\t(sheetfile "x.kicad_sch")
\t(attr smd dnp)
\t(pad "1" smd roundrect
\t\t(at -0.825 0 270)
\t\t(size 0.8 0.95)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(net "VCC")
\t\t(pintype "passive")
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000004")
\t)
\t(pad "2" smd roundrect
\t\t(at 0.825 0 270)
\t\t(size 0.8 0.95)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(net "GND")
\t\t(pintype "passive")
\t\t(uuid "aaaaaaaa-0000-0000-0000-000000000005")
\t)
)"""
    )
    return fp


def test_front_rotation_only_turns_pad_and_text_angles():
    items = place(parse(R_FOOTPRINT), side="F.Cu", rotation=-90, copper=2)
    pad = next(i for i in items if i.head == "pad")
    text = next(i for i in items if i.head == "fp_text")
    line = next(i for i in items if i.head == "fp_line")
    assert [a.raw for a in pad.child("at").atoms()] == ["-0.825", "0", "270"]  # type: ignore[union-attr]
    assert [a.raw for a in text.child("at").atoms()] == ["0", "0", "270"]  # type: ignore[union-attr]
    assert line.value("start", index=1) == "-0.52"  # geometry stays local


def test_back_side_mirrors_top_to_bottom():
    """
    Given the library footprint
    When it is placed on the back rotated by 90
    Then y is negated, layers are B.*, pad angles 90 (-0 + 90), text angles 270 (180 - 0 + 90)
    And texts are mirrored
    """
    items = place(parse(R_FOOTPRINT), side="B.Cu", rotation=90, copper=2)
    pad = next(i for i in items if i.head == "pad")
    ref = next(i for i in items if i.head == "property" and i.atom(0) == "Reference")
    line = next(i for i in items if i.head == "fp_line")
    assert [a.text for a in pad.child("layers").atoms()] == [
        "B.Cu",
        "B.Mask",
        "B.Paste",
    ]  # type: ignore[union-attr]
    assert [a.raw for a in pad.child("at").atoms()] == ["-0.825", "0", "90"]  # type: ignore[union-attr]
    assert [a.raw for a in ref.child("at").atoms()] == ["0", "1.43", "270"]  # type: ignore[union-attr]
    assert ref.child("layer").atom(0) == "B.SilkS"  # type: ignore[union-attr]
    assert "(justify mirror)" in format_node(ref)
    assert line.value("start", index=1) == "0.52"


def test_exchange_keeps_what_the_board_owns():
    """
    Given a board footprint whose designer moved the reference and added a field
    When it is exchanged with the library footprint
    Then place, side, uuid, links, nets, DNP and field placement stay; nothing else changes
    """
    fp = board_footprint()
    new = exchange(fp, parse(R_FOOTPRINT), LIB_ID, FootprintOptions(), copper=2)
    text = format_node(new)
    assert (
        "(at 10 20 -90)" in text
        and '(uuid "aaaaaaaa-0000-0000-0000-000000000000")' in text
    )
    assert '(property "Reference" "R1"\n\t\t(at 1.5 -2 45)' in text
    assert '(property "LCSC" "C25804"' in text
    assert '(net "VCC")' in text and '(net "GND")' in text
    assert "(attr smd dnp)" in text
    assert '(path "/bbbbbbbb-0000-0000-0000-000000000000")' in text
    assert "fp_text" in text and "${KICAD10_3DMODEL_DIR}" in text


def test_exchange_of_an_up_to_date_footprint_is_a_no_op(tmp_path):
    """
    Given a board whose footprint was placed from the library as it is now
    When footprints are updated twice
    Then the first update may change it, the second changes nothing
    """
    pcb_path = tmp_path / "b.kicad_pcb"
    write_lf(
        pcb_path,
        "(kicad_pcb\n\t(version 20260206)\n\t"
        + format_node(board_footprint(), 1)
        + "\n)\n",
    )
    libs = FakeLibs()
    first = SFile.load(pcb_path)
    update_footprints(first, libs, FootprintOptions())  # type: ignore[arg-type]
    first.save()
    second = SFile.load(pcb_path)
    report = update_footprints(second, libs, FootprintOptions())  # type: ignore[arg-type]
    assert report.changes == [] and not second.changed()


def test_library_change_reaches_the_board(tmp_path):
    """
    Given a library whose pad got bigger
    When footprints are updated
    Then the board pads follow and keep their nets
    """
    fp = board_footprint()
    up_to_date = exchange(fp, parse(R_FOOTPRINT), LIB_ID, FootprintOptions(), copper=2)
    pcb_path = tmp_path / "b.kicad_pcb"
    write_lf(
        pcb_path,
        "(kicad_pcb\n\t(version 20260206)\n\t" + format_node(up_to_date, 1) + "\n)\n",
    )
    libs = FakeLibs(
        footprints={LIB_ID: R_FOOTPRINT.replace("(size 0.8 0.95)", "(size 0.9 0.95)")}
    )
    pcb = SFile.load(pcb_path)
    report = update_footprints(pcb, libs, FootprintOptions())  # type: ignore[arg-type]
    assert report.changes == [f"R1: {LIB_ID} updated from the library"]
    text = pcb.render()
    assert text.count("(size 0.9 0.95)") == 2 and '(net "VCC")' in text


def test_same_footprint_ignores_uuids_and_order():
    a = parse(R_FOOTPRINT)
    b = parse(
        R_FOOTPRINT.replace(
            "11111111-0000-0000-0000-000000000005",
            "22222222-0000-0000-0000-000000000005",
        )
    )
    pads = [
        i for i, it in enumerate(b.items) if isinstance(it, Node) and it.head == "pad"
    ]
    b.items[pads[0]], b.items[pads[1]] = b.items[pads[1]], b.items[pads[0]]
    assert same_footprint(a, b)  # KiCad re-sorts repeated items itself
    c = parse(R_FOOTPRINT.replace("(size 0.8 0.95)", "(size 0.9 0.95)", 1))
    assert not same_footprint(a, c)
