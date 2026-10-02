"""inkibox.kicad: router clearances against real pad shapes and wide tracks, stable uuids,
footprint zones and body styles."""

from __future__ import annotations

import math

import pytest

from inkibox.kicad import Board, GridRouter
from inkibox.kicad.libs import pin_defs
from inkibox.kicad.router import Terminal
from inkibox.kicad.sexpr import atom_text, child, children, head
from inkibox.kicad.sexpr import parse_one as parse

CLEARANCE = 0.2
FRONT = frozenset({"F.Cu"})


def rect_distance(px, py, cx, cy, w, h, angle=0.0):
    a = math.radians(angle)
    c, s = math.cos(a), math.sin(a)
    u = (px - cx) * c - (py - cy) * s
    v = (px - cx) * s + (py - cy) * c
    return math.hypot(max(abs(u) - w / 2, 0.0), max(abs(v) - h / 2, 0.0))


def segment_points(seg, step=0.01):
    x1, y1, x2, y2, _layer = seg
    n = max(1, int(math.hypot(x2 - x1, y2 - y1) / step))
    return [(x1 + (x2 - x1) * k / n, y1 + (y2 - y1) * k / n) for k in range(n + 1)]


def min_gap(res, width, pads):
    """Smallest copper-to-copper distance between a route's tracks and ``pads``
    (x, y, w, h, angle) on F.Cu."""
    gap = math.inf
    for seg in res.segments:
        if seg[4] != "F.Cu":
            continue
        for px, py in segment_points(seg):
            for cx, cy, w, h, ang in pads:
                gap = min(gap, rect_distance(px, py, cx, cy, w, h, ang) - width / 2)
    return gap


@pytest.mark.parametrize("angle", [0.0, 30.0, 90.0])
def test_tracks_clear_the_corners_of_a_large_rectangular_pad(angle):
    # a 2.5 x 3.3 pad (an SMC diode) right between the two ends of another net; the
    # circle of half its longer side (1.65) misses its corners (2.07 from the centre)
    r = GridRouter(0, 0, 12, 12, clearance=CLEARANCE)
    r.add_keepout(0, 0, 12, 12, frozenset({"B.Cu"}))  # one layer: it has to go round
    pad = (6.0, 6.0, 2.5, 3.3, angle)
    r.add_pad(6.0, 6.0, 1.65, FRONT, "GND", size=(2.5, 3.3), angle=angle, shape="rect")
    res = r.route("SIG", [Terminal(1.0, 6.0, FRONT), Terminal(11.0, 6.0, FRONT)], 0.25)
    assert res.ok
    assert min_gap(res, 0.25, [pad]) >= CLEARANCE - 1e-6


def test_a_wide_track_keeps_its_clearance():
    # obstacles are inflated for a 0.4 mm reference track; a 1.0 mm one must still keep
    # 0.2 mm to the pads it runs between
    r = GridRouter(0, 0, 14, 10, clearance=CLEARANCE)
    r.add_keepout(0, 0, 14, 10, frozenset({"B.Cu"}))
    pads = [(7.0, 3.0, 2.0, 2.0, 0.0), (7.0, 7.0, 2.0, 2.0, 0.0)]
    for x, y, w, h, _a in pads:
        r.add_pad(x, y, 1.0, FRONT, "GND", size=(w, h), shape="rect")
    res = r.route("+5V", [Terminal(1.0, 5.0, FRONT), Terminal(13.0, 5.0, FRONT)], 1.0)
    assert res.ok
    assert min_gap(res, 1.0, pads) >= CLEARANCE - 1e-6


def test_a_fine_pitch_pin_off_the_grid_can_leave():
    # SO-8 pins (0.6 x 1.55, 1.27 pitch) whose row sits half a grid cell off the router
    # grid: as circles of 0.775 the neighbours closed the middle pin in
    pitch = 0.3175
    r = GridRouter(0, 0, 10, 10, pitch=pitch, clearance=CLEARANCE)
    y0 = 5.0 + pitch / 2
    for k, net in enumerate(["A", "B", "C"]):
        r.add_pad(
            3.0, y0 + (k - 1) * 1.27, 0.775, FRONT, net, size=(1.55, 0.6), shape="rect"
        )
    res = r.route("B", [Terminal(3.0, y0, FRONT), Terminal(9.0, y0, FRONT)], 0.25)
    assert res.ok, res.message


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
