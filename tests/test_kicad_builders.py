"""inkibox.kicad: stable uuids, footprint zones and body styles."""

from __future__ import annotations

import pytest

from inkibox.kicad import Board
from inkibox.kicad.libs import pin_defs
from inkibox.kicad.sexpr import atom_text, child, children, head
from inkibox.kicad.sexpr import parse_one as parse


def test_pin_defs_skip_the_de_morgan_body_style():
    sym = parse(
        '(symbol "X" (symbol "X_0_1" (pin passive line (at 0 0 0) (name "C") (number "3")))'
        ' (symbol "X_1_1" (pin passive line (at 0 2.54 0) (name "A") (number "1")))'
        ' (symbol "X_1_2" (pin passive line (at 5 5 0) (name "A") (number "1"))))'
    )
    pins = pin_defs(sym)
    assert sorted(atom_text(child(p, "number")[1]) for p in pins) == ["1", "3"]


FOOTPRINT = """(footprint "T"
\t(layer "F.Cu")
\t(fp_line (start -1 -1) (end 1 -1) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))
\t(fp_text user "${REFERENCE}" (at 0 0 0) (layer "F.Fab") (effects (font (size 1 1))))
\t(pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu"))
\t(zone (net 0) (net_name "") (layer "F.Cu") (hatch edge 0.5)
\t\t(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
\t\t(polygon (pts (xy 0 0) (xy 2 0) (xy 2 1) (xy 0 1))))
)"""


class _Libs:
    def footprint(self, lib_id):
        return parse(FOOTPRINT)


def _place(rot=0.0):
    pcb = Board("p", _Libs())  # type: ignore[arg-type]
    return pcb.place(None, (10.0, 20.0), rot, ref="U1", lib_id="L:T")


def test_footprint_texts_and_graphics_get_stable_uuids():
    a, b = _place().node, _place().node
    uuids = [
        atom_text(child(item, "uuid")[1])
        for item in a
        if head(item) in ("fp_line", "fp_text")
    ]
    assert len(uuids) == 2
    assert uuids == [
        atom_text(child(item, "uuid")[1])
        for item in b
        if head(item) in ("fp_line", "fp_text")
    ]


def test_footprint_zones_move_with_the_footprint():
    fp = _place(90.0)
    (zone,) = children(fp.node, "zone")
    pts = [
        (float(xy[1]), float(xy[2]))
        for xy in children(child(child(zone, "polygon"), "pts"), "xy")
    ]
    # rotate(x, y, 90) = (y, -x), then the footprint position
    assert pts == [(10.0, 20.0), (10.0, 18.0), (11.0, 18.0), (11.0, 20.0)]
    assert fp.pad("1").angle == 90.0


def _bbox(body: str):
    from inkibox.kicad.board import _fp_bbox

    return tuple(
        round(v, 6) for v in _fp_bbox(parse(f'(footprint "B" (layer "F.Cu") {body})'))
    )


def test_circle_courtyard_counts_its_radius():
    # MountingHole_3.2mm_M3: the courtyard is a circle of radius 3.45 around the origin
    assert _bbox(
        '(fp_circle (center 0 0) (end 3.45 0) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
        ' (pad "" np_thru_hole circle (at 0 0) (size 3.2 3.2) (drill 3.2) (layers "*.Cu"))'
    ) == (-3.45, -3.45, 3.45, 3.45)


def test_arc_courtyard_counts_the_extremes_it_passes():
    # a half circle of radius 2 around (1, 0), from the top through the right to the bottom
    arc = '(fp_arc (start 1 -2) (mid 3 0) (end 1 2) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
    assert _bbox(arc) == (1.0, -2.0, 3.0, 2.0)
    # the same ends through the left: the other half
    assert _bbox(arc.replace("(mid 3 0)", "(mid -1 0)")) == (-1.0, -2.0, 1.0, 2.0)
    # a quarter arc that passes no extreme: its ends
    q = '(fp_arc (start 2 0) (mid 1.414214 -1.414214) (end 0 -2) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))'
    assert _bbox(q) == (0.0, -2.0, 2.0, 0.0)


def test_polygon_arc_edges_count():
    poly = (
        "(fp_poly (pts (xy -1 0) (arc (start -1 0) (mid 0 -1) (end 1 0)) (xy 1 0))"
        ' (stroke (width 0.05) (type solid)) (fill no) (layer "F.CrtYd"))'
    )
    assert _bbox(poly) == (-1.0, -1.0, 1.0, 0.0)


def test_pads_count_by_rotation_and_shape():
    # no courtyard: graphics and pads; a 4 x 1 pad turned 90 degrees is 1 wide, 4 tall
    assert _bbox('(pad "1" smd rect (at 5 0 90) (size 4 1) (layers "F.Cu"))') == (
        4.5,
        -2.0,
        5.5,
        2.0,
    )
    # a round pad is the same at any angle
    assert _bbox(
        '(pad "1" thru_hole circle (at 0 0 45) (size 2 2) (drill 1) (layers "*.Cu"))'
    ) == (
        -1.0,
        -1.0,
        1.0,
        1.0,
    )
    # a custom pad reaches as far as its primitives
    assert _bbox(
        '(pad "1" smd custom (at 0 0 90) (size 1 1) (layers "F.Cu")'
        " (primitives (gr_circle (center 3 0) (end 4 0) (width 0) (fill yes))))"
    ) == (-1.0, -4.0, 1.0, 0.5)


# --------------------------------------------------------------------------- sheets, units, back side

TWO_UNITS = """(symbol "A"
\t(property "Reference" "U" (at 0 0 0))
\t(property "Value" "A" (at 0 0 0))
\t(symbol "A_0_1" (pin power_in line (at 0 -5.08 90) (length 2.54) (name "V+") (number "8")))
\t(symbol "A_1_1" (pin output line (at 5.08 0 180) (length 2.54) (name "OA") (number "1")))
\t(symbol "A_2_1" (pin output line (at 5.08 0 180) (length 2.54) (name "OB") (number "7"))))"""


class _SymLibs:
    def symbol(self, lib_id):
        return parse(TWO_UNITS)


def test_units_have_their_own_pins_and_join_for_the_board():
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    sub = sch.sheet("Sub", "sub.kicad_sch", (10, 10))
    a = sub.place("L:A", "U1", (0, 0), unit=1)
    b = sch.place("L:A", "U1", (20, 0), unit=2)
    assert sorted(a.pins) == ["1", "8"] and sorted(b.pins) == ["7", "8"]
    sub.label(a.pin("1"), "X")
    sub.global_label(a.pin("8"), "VCC")
    assert a.pin("1").net == "/Sub/X" and a.pin("8").net == "VCC"
    (u1,) = sch.components()
    assert sorted(u1.pins) == ["1", "7", "8"]
    # KiCad's netlist names the unit on the first sheet in page order: the root's
    assert u1.uuid == b.uuid and u1.sheet is sch
    # an instance lists every pin of the symbol, whichever unit it is
    node = sub._symbol_node(a)
    assert [atom_text(p[1]) for p in children(node, "pin")] == ["8", "1", "7"]


def test_sub_sheet_footprints_link_through_the_sheet_symbol():
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    sub = sch.sheet("Sub", "sub.kicad_sch", (10, 10))
    sub.place("L:A", "U1", (0, 0))
    (u1,) = sch.components()
    pcb = Board("p", _Libs())  # type: ignore[arg-type]
    fp = pcb.place(u1, (0, 0), lib_id="L:T")
    assert atom_text(child(fp.node, "path")[1]) == f"/{sub.sheet_uuid}/{u1.uuid}"
    assert atom_text(child(fp.node, "sheetname")[1]) == "/Sub/"
    assert atom_text(child(fp.node, "sheetfile")[1]) == "sub.kicad_sch"


def test_back_side_footprints_are_mirrored_top_to_bottom():
    pcb = Board("p", _Libs(), copper_layers=4)  # type: ignore[arg-type]
    fp = pcb.place(None, (10.0, 20.0), 90.0, ref="R1", lib_id="L:T", layer="B.Cu")
    assert atom_text(child(fp.node, "layer")[1]) == "B.Cu"
    pad = fp.pad("1")
    assert pad.layers == frozenset({"B.Cu"})
    # library (-1, 0), mirrored (-1, 0), turned 90 degrees: (0, 1) from the origin
    assert (pad.x, pad.y) == (10.0, 21.0)
    line = next(i for i in fp.node if head(i) == "fp_line")
    assert atom_text(child(line, "layer")[1]) == "B.SilkS"
    assert [float(v) for v in child(line, "start")[1:3]] == [-1.0, 1.0]


def test_inner_layers_and_stackup_are_written():
    from inkibox.kicad.board import stackup

    cu = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
    st = stackup([(n, "copper", 0.035, {}) for n in cu])
    pcb = Board("p", _Libs(), copper_layers=6, stackup=st)  # type: ignore[arg-type]
    root = pcb.to_node()
    names = [row[1] for row in child(root, "layers")[1:]][:6]
    assert names == ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
    assert child(child(root, "setup"), "stackup") is not None


def test_common_pins_keep_the_connected_copy_when_units_join():
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    a = sch.place("L:A", "U1", (0, 0), unit=1)
    sch.place("L:A", "U1", (20, 0), unit=2)  # its copy of the common pin 8 stays open
    sch.global_label(a.pin("8"), "VCC")
    (u1,) = sch.components()
    assert u1.pins["8"].net == "VCC"


def test_common_pins_on_two_nets_are_an_error():
    import pytest

    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    a = sch.place("L:A", "U1", (0, 0), unit=1)
    b = sch.place("L:A", "U1", (20, 0), unit=2)
    sch.global_label(a.pin("8"), "VCC")
    sch.global_label(b.pin("8"), "VDD")
    with pytest.raises(ValueError, match="pin 8"):
        sch.components()


def test_more_than_32_copper_layers_is_refused():
    import pytest

    with pytest.raises(ValueError, match="32"):
        Board("p", _Libs(), copper_layers=34)  # type: ignore[arg-type]


def test_common_pins_survive_no_connect_cleanup_of_another_unit():
    from inkibox.kicad import Schematic

    # Given unit 1 driving the common pin 8, unit 2's leftovers marked no-connect
    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    a = sch.place("L:A", "U1", (0, 0), unit=1)
    b = sch.place("L:A", "U1", (20, 0), unit=2)
    sch.global_label(a.pin("8"), "VCC")
    sch.global_label(a.pin("1"), "OA")
    sch.no_connect_unused(b)
    # When the units join, Then pin 8 keeps its net and pin 7 is no-connect
    (u1,) = sch.components()
    assert u1.pins["8"].net == "VCC"
    assert u1.pins["7"].net.startswith("unconnected-(")


def test_unconnected_net_names_follow_kicad():
    from inkibox.kicad import Schematic

    # Given a two-unit symbol (no unit names: letters) with a slash in a pin name
    lib = TWO_UNITS.replace('(name "OB")', '(name "SDO/PDM")')

    class Libs:
        def symbol(self, lib_id):
            return parse(lib)

    sch = Schematic("p", Libs())  # type: ignore[arg-type]
    b = sch.place("L:A", "U1", (0, 0), unit=2)
    sch.no_connect(b.pin("7"))
    # Then the net is KiCad's: reference + unit letter, '/' escaped
    assert b.pin("7").net == "unconnected-(U1B-SDO{slash}PDM-Pad7)"


def test_unnamed_pin_unconnected_net_has_no_unit_suffix():
    from inkibox.kicad import Schematic

    # Given a two-unit symbol whose pin 7 has no name
    lib = TWO_UNITS.replace('(name "OB")', '(name "")')

    class Libs:
        def symbol(self, lib_id):
            return parse(lib)

    sch = Schematic("p", Libs())  # type: ignore[arg-type]
    b = sch.place("L:A", "U1", (0, 0), unit=2)
    sch.no_connect(b.pin("7"))
    # Then KiCad's form for an unnamed pin: the bare reference and the pad
    assert b.pin("7").net == "unconnected-(U1-Pad7)"


def test_label_names_are_escaped_in_nets_as_kicad_does():
    from inkibox.kicad import Schematic

    # Given labels with a slash and characters KiCad leaves alone
    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    sub = sch.sheet("Sub", "sub.kicad_sch", (10, 10))
    a = sub.place("L:A", "U1", (0, 0), unit=1)
    sub.label(a.pin("1"), "A/B")
    sub.global_label(a.pin("8"), 'USB/D+ {x} N\\M "q"')
    # Then "/" is {slash} (it separates sheets); the sheet path keeps its slashes and
    # nothing else is escaped (kicad-cli's netlist, tests/test_e2e_hierarchy.py)
    assert a.pin("1").net == "/Sub/A{slash}B"
    assert a.pin("8").net == 'USB{slash}D+ {x} N\\M "q"'


def test_blind_vias_name_their_layers():
    pcb = Board("p", _Libs(), copper_layers=4)  # type: ignore[arg-type]
    pcb.via((1.0, 2.0), "GND", size=0.25, drill=0.1, layers=("F.Cu", "In1.Cu"))
    pcb.via((3.0, 4.0), "GND")
    vias = children(pcb.to_node(), "via")
    assert [atom_text(v[1]) for v in vias][:1] == ["blind"]
    assert [atom_text(x) for x in child(vias[0], "layers")[1:]] == ["F.Cu", "In1.Cu"]
    assert head(vias[1][1]) == "at"  # a through via carries no type
    assert [atom_text(x) for x in child(vias[1], "layers")[1:]] == ["F.Cu", "B.Cu"]


def test_back_side_footprint_zone_is_flipped_and_placed():
    # Given the test footprint, whose keepout zone spans (0,0)-(2,1) on F.Cu
    pcb = Board("p", _Libs(), copper_layers=4)  # type: ignore[arg-type]
    fp = pcb.place(None, (10.0, 20.0), 0, ref="U1", lib_id="L:T", layer="B.Cu")
    (zone,) = children(fp.node, "zone")
    pts = sorted(
        (float(xy[1]), float(xy[2]))
        for xy in children(child(child(zone, "polygon"), "pts"), "xy")
    )
    # Then it is mirrored top to bottom (y negated), moved to the footprint and on B.Cu
    assert pts == [(10.0, 19.0), (10.0, 20.0), (12.0, 19.0), (12.0, 20.0)]
    assert atom_text(child(zone, "layer")[1]) == "B.Cu"


def test_stackup_rows_as_kicad_writes_them():
    from inkibox.kicad.board import stackup

    st = stackup(
        [
            ("F.SilkS", "Top Silk Screen", 0, {}),
            ("F.Cu", "copper", 0.035, {}),
            ("dielectric 1", "prepreg", 0.09, {"material": "FR4", "epsilon_r": 4.4}),
            ("B.Cu", "copper", 0.035, {}),
        ],
        copper_finish="ENIG",
    )
    rows = {atom_text(r[1]): r for r in children(st, "layer")}
    assert child(rows["F.SilkS"], "thickness") is None  # 0: not written
    assert child(rows["dielectric 1"], "material")[1] == "FR4"
    assert child(rows["dielectric 1"], "epsilon_r")[1] == 4.4
    assert atom_text(child(st, "copper_finish")[1]) == "ENIG"
    assert atom_text(child(st, "dielectric_constraints")[1]) == "yes"


def test_copper_layer_count_must_be_even_and_at_least_two():
    import pytest

    for bad in (0, 1, 3, 33):
        with pytest.raises(ValueError):
            Board("p", _Libs(), copper_layers=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- input validation


def test_a_unit_the_symbol_does_not_define_is_refused():
    import pytest

    from inkibox.kicad import Schematic
    from inkibox.kicad.libs import unit_pin_defs

    single = parse(
        '(symbol "R" (symbol "R_0_1" (pin passive line (at 0 2.54 270) (name "~") (number "1")))'
        ' (symbol "R_1_1" (pin passive line (at 0 -2.54 90) (name "~") (number "2"))))'
    )
    # a single-unit symbol: unit 1 is all its pins, unit 2 does not exist
    assert len(unit_pin_defs(single, 1)) == 2
    with pytest.raises(ValueError, match=r"symbol R has no unit 2 \(units: \[1\]\)"):
        unit_pin_defs(single, 2)
    # a multi-unit symbol: each defined unit, nothing past them
    two = parse(TWO_UNITS)
    assert len(unit_pin_defs(two, 2)) == 2
    with pytest.raises(ValueError, match=r"no unit 9 \(units: \[1, 2\]\)"):
        unit_pin_defs(two, 9)
    with pytest.raises(ValueError, match="no unit 0"):
        unit_pin_defs(two, 0)
    # and place() refuses it before writing an instance of nothing
    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no unit 3"):
        sch.place("L:A", "U1", (0, 0), unit=3)
    assert sch.symbols == []


def test_sheet_names_and_files_must_be_unique():
    import pytest

    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    a = sch.sheet("Power", "power.kicad_sch", (10, 10))
    with pytest.raises(ValueError, match="named 'Power'"):
        sch.sheet("Power", "power2.kicad_sch", (40, 10))
    with pytest.raises(ValueError, match="'power.kicad_sch' is used"):
        a.sheet("Sub", "power.kicad_sch", (10, 10))  # anywhere in the project
    with pytest.raises(ValueError, match="'p.kicad_sch' is used"):
        sch.sheet("Root again", "p.kicad_sch", (40, 10))
    a.sheet(
        "Power", "inner.kicad_sch", (10, 10)
    )  # the same name one level down is fine


def test_back_side_is_the_only_other_side():
    import pytest

    pcb = Board("p", _Libs())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="F.Cu or B.Cu"):
        pcb.place(None, (0, 0), ref="R1", lib_id="L:T", layer="In1.Cu")


def test_stackup_copper_must_match_the_layer_count():
    import pytest

    from inkibox.kicad.board import stackup

    four = stackup(
        [(n, "copper", 0.035, {}) for n in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")]
    )
    Board("p", _Libs(), copper_layers=4, stackup=four)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="do not match a 6-layer board"):
        Board("p", _Libs(), copper_layers=6, stackup=four)  # type: ignore[arg-type]
    wrong = stackup(
        [(n, "copper", 0.035, {}) for n in ("F.Cu", "In3.Cu", "In4.Cu", "B.Cu")]
    )
    with pytest.raises(ValueError, match="do not match a 4-layer board"):
        Board("p", _Libs(), copper_layers=4, stackup=wrong)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- KiCad's rules


def test_net_names_escape_as_kicads_ctx_netname():
    from inkibox.kicad.nets import escape_net

    # EscapeString(…, CTX_NETNAME), common/string_utils.cpp: "/" -> {slash}
    assert escape_net("USB/D+") == "USB{slash}D+"
    # line feeds and carriage returns are dropped
    assert escape_net("A\nB") == "AB"
    assert escape_net("A\r\nB") == "AB"
    # everything else stays
    for text in (
        "a{b}c",
        "back\\slash",
        'q"uote',
        "sp ace",
        "tab\there",
        "~",
        "$x:y.z",
    ):
        assert escape_net(text) == text


def test_labels_with_line_breaks_name_kicads_net():
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    sub = sch.sheet("Sub", "sub.kicad_sch", (10, 10))
    a = sub.place("L:A", "U1", (0, 0), unit=1)
    sub.label(a.pin("1"), "TWO\nLINES")
    sub.global_label(a.pin("8"), "V/CC\r\n")
    assert a.pin("1").net == "/Sub/TWOLINES"
    assert a.pin("8").net == "V{slash}CC"


@pytest.mark.parametrize("name", ["A/B", "", "A\tB", "A\nB", "A\rB"])
def test_sheet_names_kicad_refuses_are_refused(name):
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sheet name"):
        sch.sheet(name, "sub.kicad_sch", (10, 10))


@pytest.mark.parametrize("file", ["", "a\nb.kicad_sch", "a\tb.kicad_sch"])
def test_sheet_files_kicad_refuses_are_refused(file):
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sheet file"):
        sch.sheet("Sub", file, (10, 10))


def test_sheet_files_in_a_subdirectory_are_fine():
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    sub = sch.sheet("Sub", "pages/sub.kicad_sch", (10, 10))
    assert sub.file == "pages/sub.kicad_sch"


def test_write_refuses_a_sub_sheet_on_the_output_file(tmp_path):
    from inkibox.kicad import Schematic

    # Given a sub-sheet whose file is the name the root is then written under
    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    sch.sheet("Sub", "main.kicad_sch", (10, 10))
    # When / Then the write is refused, and nothing is written
    with pytest.raises(ValueError, match="both be written"):
        sch.write(tmp_path / "main.kicad_sch")
    assert list(tmp_path.iterdir()) == []


def test_write_refuses_nested_sheets_landing_on_one_file(tmp_path):
    from inkibox.kicad import Schematic

    # Given a nested sub-sheet in a subdirectory whose path meets another sheet's file
    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    a = sch.sheet("A", "pages/a.kicad_sch", (10, 10))
    a.sheet("B", "b.kicad_sch", (10, 10))
    sch.sheet("C", "pages/b.kicad_sch", (40, 10))
    with pytest.raises(ValueError, match="both be written"):
        sch.write(tmp_path / "p.kicad_sch")
    assert list(tmp_path.iterdir()) == []


def test_write_puts_every_sheet_where_output_files_says(tmp_path):
    from inkibox.kicad import Schematic

    sch = Schematic("p", _SymLibs())  # type: ignore[arg-type]
    a = sch.sheet("A", "a.kicad_sch", (10, 10))
    a.sheet("B", "b.kicad_sch", (10, 10))
    sch.write(tmp_path / "p.kicad_sch")
    assert sorted(f.name for f in tmp_path.iterdir()) == [
        "a.kicad_sch",
        "b.kicad_sch",
        "p.kicad_sch",
    ]
