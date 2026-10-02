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
