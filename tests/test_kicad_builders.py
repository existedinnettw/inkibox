"""inkibox.kicad: stable uuids, footprint zones and body styles."""

from __future__ import annotations

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
