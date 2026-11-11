"""``inkibox place``: pack a board's footprints tight with a constraint solver, under the
rules a carrier board has, and write the positions back.

Placing by rule ("this right of that, snapped to the grid") only ever nudges one
arrangement; turning a module 90 degrees or laying it down somewhere else is a different
arrangement such rules never reach. ``place`` instead states the problem and lets
OR-Tools' CP-SAT search all of them:

* every front footprint is a rectangle (its courtyard) at an integer position (0.01 mm) in
  one of its allowed rotations (0/90/180/270), and no two keep areas (courtyard, grown
  sides, half the ``gap``) overlap;
* the courtyard stays ``edge`` inside the board, or passes the edge by the part's
  ``overhang`` (a terminal or module that stands over the edge); its pads always stay
  ``pad_edge`` inside, as copper must;
* ``on_grid`` parts have their named pad on the ``grid`` (pitch through the ``grid_origin``
  pad, e.g. 2.54 mm from the main module's header): modules on pin sockets keep one
  perfboard grid; the others move on ``resolution``;
* ``edges`` pin a side of a part to a side of the board (a USB connector on the top edge,
  CAN ports on the bottom), which also limits its rotations; ``fixed`` parts stay put;
  ``apart`` keeps groups of parts a distance from each other (an isolation barrier);
* the objective is the board's area (``width`` × ``height``, when the outline is one
  rectangle and they are not given) plus ``wire_weight`` mm² for every mm of estimated
  wire length (the half perimeter of each net's pins, without the ``wire_ignore`` nets).

The current positions are the solver's starting point (a hand layout to beat). The
outline (one ``gr_rect`` on Edge.Cuts) keeps its top left corner and gets the solved size;
tracks, vias and zones are left as they are, so place before routing.

Options come from ``[tool.inkibox.place]`` in ``pyproject.toml``::

    [tool.inkibox.place]
    gap = 0.25                         # mm between courtyards
    edge = 0.5                         # mm from courtyard to board edge
    pad_edge = 0.5                     # mm from pad copper to board edge, always
    resolution = 0.05                  # mm, the position step off the grid
    grid = 2.54                        # mm; on_grid parts' pads on it
    grid_origin = "A1:J2_1"            # a pad whose current position is a grid point
    on_grid = { A1 = "J2_1", A2 = "J2_1" }   # part -> the pad that sits on the grid
    fixed = ["A1"]
    rotations = { A1 = [0] }           # default: all four
    edges = { A1 = { top = "top" } }   # the part's own side -> the board side it is on
    overhang = { J1 = 6.0 }            # mm the courtyard may pass the board edge
    grow = { A2 = { top = 0.88 } }     # mm kept clear past a side (terminals on a module)
    apart = [{ a = ["J2"], b = ["A4"], gap = 2.0 }]
    wire_weight = 0.5                  # mm² of board per mm of wire
    wire_ignore = ["GND"]
    width = 80.0                       # fix the board size instead of minimising it
    time_limit = 30                    # s
    workers = 8
    seed = 0
    unit = 0.01                        # mm per solver step (1/n mm)

The solver works in whole ``unit`` steps (0.01 mm); a part on the grid comes back exactly on
it, rebuilt from the grid line its pad landed on (:func:`exact_positions`), even when its
pad's offset is no whole number of steps.

The solver runs in-process when ``ortools`` is importable, else in an isolated ``uv run
--with ortools`` environment (OR-Tools needs protobuf 6, kicad-python protobuf 5). With
more than one worker and a time limit, two runs can return different placements of equal
or similar cost.
"""

from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field, fields
from fractions import Fraction
from pathlib import Path

from ..kicad.geometry import pad_points, rotate, shape_points
from ..kicad.sfile import Node, SFile, number
from ..update import ProjectError
from ..update.placement import _set_angle

UNIT = 100  # problem units per mm by default (``unit`` = 0.01 mm)
SOLVER = Path(__file__).with_name("_solver.py")
ORTOOLS = "ortools>=9.15"
SIDES = {"top": (0, -1), "bottom": (0, 1), "left": (-1, 0), "right": (1, 0)}

__all__ = [
    "Part",
    "PlaceError",
    "PlaceOptions",
    "PlaceResult",
    "apply_placement",
    "build_problem",
    "exact_positions",
    "load_place_options",
    "place_project",
    "read_parts",
    "run_solver",
]


class PlaceError(RuntimeError):
    pass


@dataclass(slots=True)
class PlaceOptions:
    gap: float = 0.25
    edge: float = 0.5
    pad_edge: float = 0.5
    resolution: float = 0.05
    grid: float | None = None
    grid_origin: str | None = None
    on_grid: dict[str, str] = field(default_factory=dict)
    fixed: list[str] = field(default_factory=list)
    rotations: dict[str, list[int]] = field(default_factory=dict)
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    overhang: dict[str, float] = field(default_factory=dict)
    grow: dict[str, dict[str, float]] = field(default_factory=dict)
    apart: list[dict] = field(default_factory=list)
    wire_weight: float = 0.5
    wire_ignore: list[str] = field(default_factory=list)
    width: float | None = None
    height: float | None = None
    time_limit: float = 30.0
    workers: int = 8
    seed: int = 0
    unit: float = 0.01  # mm per solver unit: 1/n mm

    def scale(self) -> int:
        """Solver units per mm."""
        n = round(1 / self.unit) if self.unit > 0 else 0
        if not 1 <= n <= 10000 or abs(n * self.unit - 1) > 1e-9:
            raise ProjectError(
                f"[tool.inkibox.place].unit: {self.unit} is not 1/n mm (n = 1 .. 10000)"
            )
        return n

    def overhang_max(self) -> float:
        return max(self.overhang.values(), default=0.0)


def load_place_options(project_dir: Path, **override) -> PlaceOptions:
    opts = PlaceOptions()
    py = project_dir / "pyproject.toml"
    table: dict = {}
    if py.is_file():
        table = (
            tomllib.loads(py.read_text(encoding="utf-8"))
            .get("tool", {})
            .get("inkibox", {})
            .get("place", {})
        )
    unknown = set(table) - {f.name for f in fields(PlaceOptions)}
    if unknown:
        raise ProjectError(
            f"[tool.inkibox.place]: unknown option(s) {', '.join(sorted(unknown))}"
        )
    for key, value in {
        **table,
        **{k: v for k, v in override.items() if v is not None},
    }.items():
        setattr(opts, key, value)
    for ref, sides in {**opts.edges, **opts.grow}.items():
        bad = set(sides) - set(SIDES)
        if bad:
            raise ProjectError(
                f"[tool.inkibox.place]: {ref}: unknown side(s) {', '.join(sorted(bad))}"
            )
    return opts


# --------------------------------------------------------------------------- the board


@dataclass(slots=True)
class Pad:
    name: str
    x: float  # footprint frame, unrotated
    y: float
    box: tuple[float, float, float, float]  # its copper in that frame
    net: str | None


@dataclass(slots=True)
class Part:
    ref: str
    node: Node
    x: float  # board coordinates
    y: float
    rot: float
    back: bool
    box: tuple[float, float, float, float]  # courtyard, footprint frame
    pads: list[Pad]
    cut: tuple[float, float, float, float] | None = None  # its own Edge.Cuts, if any

    def pad(self, name: str) -> Pad:
        for p in self.pads:
            if p.name == name:
                return p
        raise PlaceError(f"{self.ref} has no pad {name!r}")


def _num(nd: Node | None, i: int, default: float = 0.0) -> float:
    if nd is None:
        return default
    a = nd.atoms()
    return float(a[i].raw) if len(a) > i else default


def _graphics_box(fp: Node, layer: str) -> tuple[float, float, float, float] | None:
    """The bounding box of a footprint's graphics on ``layer``, in its own frame."""
    pts: list[tuple[float, float]] = []
    for item in fp.children():
        if (
            item.head.startswith("fp_")
            and item.head != "fp_text"
            and item.value("layer") == layer
        ):
            pts += shape_points(_legacy(item))
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _courtyard(fp: Node, back: bool) -> tuple[float, float, float, float] | None:
    return _graphics_box(fp, "B.CrtYd" if back else "F.CrtYd")


def _legacy(nd: Node) -> list:
    """An :mod:`inkibox.kicad.sfile` node as the list form :mod:`.geometry` reads."""
    from ..kicad.sexpr import parse_one
    from ..kicad.sfile import format_node

    return parse_one(format_node(nd))


def read_parts(
    sf: SFile,
) -> tuple[list[Part], tuple[float, float, float, float] | None]:
    """The footprints of a board and its outline, when that is one ``gr_rect``."""
    parts: list[Part] = []
    for fp in sf.root.children("footprint"):
        ref = None
        for prop in fp.children("property"):
            if prop.atom(0) == "Reference":
                ref = prop.atom(1)
        if not ref:
            continue
        at = fp.child("at")
        x, y, rot = _num(at, 0), _num(at, 1), _num(at, 2)
        back = fp.value("layer") == "B.Cu"
        pads: list[Pad] = []
        for pad in fp.children("pad"):
            pat = pad.child("at")
            px, py = _num(pat, 0), _num(pat, 1)
            # the copper by its shape (custom primitives, trapezoids too), in the
            # footprint frame: a board stores pad angles absolute
            local = pad.copy()
            lat = local.child("at")
            if lat is not None:
                lat.items = [number(px), number(py), number(_num(pat, 2) - rot)]
            pts = pad_points(_legacy(local))
            net = pad.child("net")
            pads.append(
                Pad(
                    pad.atom(0) or "",
                    px,
                    py,
                    (
                        min(q[0] for q in pts),
                        min(q[1] for q in pts),
                        max(q[0] for q in pts),
                        max(q[1] for q in pts),
                    ),
                    (net.atoms()[-1].text if net is not None and net.atoms() else None)
                    or None,
                )
            )
        box = _courtyard(fp, back)
        if box is None:
            if not pads:
                continue
            box = _pads_box(pads)
        # the footprint's own cutouts / slots (Edge.Cuts) move with it: inside its keep
        # area and, like copper, inside the board
        cut = _graphics_box(fp, "Edge.Cuts")
        if cut is not None:
            box = _union(box, cut)
        parts.append(Part(ref, fp, x, y, rot, back, box, pads, cut))
    outline = None
    edge = [
        g
        for g in sf.root.children()
        if g.head.startswith("gr_") and g.value("layer") == "Edge.Cuts"
    ]
    rects = [g for g in edge if g.head == "gr_rect"]
    if len(rects) == 1 and len(edge) == 1:  # a cutout or slot would be ignored
        s, e = rects[0].child("start"), rects[0].child("end")
        x0, y0, x1, y1 = _num(s, 0), _num(s, 1), _num(e, 0), _num(e, 1)
        outline = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return parts, outline


# --------------------------------------------------------------------------- the problem


def _union(a, b) -> tuple[float, float, float, float]:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _pads_box(pads: list[Pad]) -> tuple[float, float, float, float]:
    return (
        min(p.box[0] for p in pads),
        min(p.box[1] for p in pads),
        max(p.box[2] for p in pads),
        max(p.box[3] for p in pads),
    )


def _u(scale: int, v: float) -> int:
    return round(v * scale)


def _err(scale: int, v: float) -> float:
    """``v`` mm less ``v`` rounded to whole units, in mm."""
    return v - round(v * scale) / scale


def _up(scale: int, v: float) -> int:
    """A minimum distance in units, rounded up: never weaker than asked."""
    return math.ceil(v * scale - 1e-6)


def _down(scale: int, v: float) -> int:
    """An allowance in units, rounded down: never more than granted."""
    return math.floor(v * scale + 1e-6)


def _rbox(scale: int, box, rot, shift: tuple[float, float] = (0.0, 0.0)) -> list[int]:
    """``box`` turned by ``rot`` and moved by ``shift`` (mm), in solver units, rounded
    outward: a clearance in it is never rounded down (a 0.25 mm gap stays 0.25 mm)."""
    pts = [
        (px + shift[0], py + shift[1])
        for px, py in (
            rotate(x, y, rot) for x in (box[0], box[2]) for y in (box[1], box[3])
        )
    ]
    eps = 1e-6  # float noise of an exact value, e.g. 0.1 * 100
    return [
        math.floor(min(p[0] for p in pts) * scale + eps),
        math.floor(min(p[1] for p in pts) * scale + eps),
        math.ceil(max(p[0] for p in pts) * scale - eps),
        math.ceil(max(p[1] for p in pts) * scale - eps),
    ]


def _side_of(side: str, rot: float) -> str:
    vx, vy = rotate(*SIDES[side], rot)
    return min(SIDES, key=lambda s: (SIDES[s][0] - vx) ** 2 + (SIDES[s][1] - vy) ** 2)


def build_problem(
    parts: list[Part],
    origin: tuple[float, float],
    size: tuple[float | None, float | None],
    opts: PlaceOptions,
) -> dict:
    """The JSON problem :mod:`._solver` reads, positions relative to ``origin`` (the
    outline's top left corner); ``size`` fixes the board's width / height (``None``:
    solve for it)."""
    sc = opts.scale()
    if opts.wire_weight < 0:
        raise ProjectError("[tool.inkibox.place].wire_weight: must not be negative")
    wire = Fraction(opts.wire_weight * sc).limit_denominator(1000)
    if opts.wire_weight > 0 and wire == 0:
        wire = Fraction(1, 1000)
    refs = {p.ref: i for i, p in enumerate(parts)}
    dup = {p.ref for p in parts if sum(q.ref == p.ref for q in parts) > 1}
    named = [
        (key, ref)
        for key in ("fixed", "rotations", "edges", "overhang", "grow", "on_grid")
        for ref in getattr(opts, key)
    ]
    named += [
        ("apart", r)
        for rule in opts.apart
        for r in [*rule.get("a", []), *rule.get("b", [])]
    ]
    if opts.grid_origin and ":" in opts.grid_origin:
        named.append(("grid_origin", opts.grid_origin.split(":", 1)[0]))
    for key, ref in named:
        if ref not in refs:
            raise ProjectError(f"[tool.inkibox.place].{key}: no footprint {ref}")
        if ref in dup:
            raise ProjectError(
                f"[tool.inkibox.place].{key}: {ref} names more than one footprint"
            )
    grid = None
    grid_mm = None
    origin_err = (0.0, 0.0)
    if opts.grid:
        if not opts.grid_origin or ":" not in opts.grid_origin:
            raise ProjectError(
                "[tool.inkibox.place]: grid needs grid_origin = 'REF:PAD'"
            )
        gref, gpad = opts.grid_origin.split(":", 1)
        if gref not in refs:
            raise ProjectError(f"[tool.inkibox.place].grid_origin: no footprint {gref}")
        g = parts[refs[gref]]
        gx, gy = rotate(g.pad(gpad).x, g.pad(gpad).y, g.rot)
        pitch = _u(sc, opts.grid)
        if pitch < 1 or abs(opts.grid * sc - pitch) > 1e-6:
            raise ProjectError(
                f"[tool.inkibox.place]: grid {opts.grid} mm is no whole number of "
                f"unit {opts.unit} mm steps: use a finer unit"
            )
        grid = {
            "pitch": pitch,
            "ox": _u(sc, g.x + gx - origin[0]) % pitch,
            "oy": _u(sc, g.y + gy - origin[1]) % pitch,
        }
        # the same in exact mm, for :func:`exact_positions`
        grid_mm = [opts.grid, g.x + gx - origin[0], g.y + gy - origin[1]]
        origin_err = (_err(sc, grid_mm[1]), _err(sc, grid_mm[2]))
    # one pin per part and net: the middle of that part's pads on the net
    nets: dict[str, list[int]] = {}
    members: dict[str, dict[int, tuple[float, float]]] = {}
    for i, part in enumerate(parts):
        by: dict[str, list[Pad]] = {}
        for pad in part.pads:
            if (
                pad.net
                and pad.net not in opts.wire_ignore
                and not pad.net.startswith("unconnected-")
            ):
                by.setdefault(pad.net, []).append(pad)
        for net, ps in by.items():
            members.setdefault(net, {})[i] = (
                sum(p.x for p in ps) / len(ps),
                sum(p.y for p in ps) / len(ps),
            )
    for net, mem in members.items():
        if len(mem) > 1:
            nets[net] = sorted(mem)
    out_parts = []
    extent = 0  # in units, from the rounded geometry the solver sees
    far = 0
    for i, part in enumerate(parts):
        fixed = part.back or part.ref in opts.fixed
        rots = (
            [part.rot % 360]  # exactly as it is, fractional angles too
            if fixed
            else opts.rotations.get(part.ref, [0, 90, 180, 270])
        )
        want = opts.edges.get(part.ref, {})
        rots = [r for r in rots if all(_side_of(s, r) == e for s, e in want.items())]
        if not rots:
            raise ProjectError(
                f"{part.ref}: no allowed rotation puts its sides on {want}"
            )
        grow = opts.grow.get(part.ref, {})
        gbox = (
            part.box[0] - grow.get("left", 0.0),
            part.box[1] - grow.get("top", 0.0),
            part.box[2] + grow.get("right", 0.0),
            part.box[3] + grow.get("bottom", 0.0),
        )
        half = opts.gap / 2
        kbox = (gbox[0] - half, gbox[1] - half, gbox[2] + half, gbox[3] + half)
        pbox = _pads_box(part.pads) if part.pads else part.box
        if part.cut is not None:
            pbox = _union(pbox, part.cut)
        on_grid = part.ref in opts.on_grid and grid is not None
        options = []
        boxes: list[list[int]] = []  # every rounded box of every option
        for r in rots:
            # where the part will really stand, off the solver's position: a fixed part by
            # its position's rounding, a grid part (exact_positions) by the rounding of the
            # grid origin less that of its grid pad's offset (known per rotation and axis)
            shift = (0.0, 0.0)
            if fixed:  # it stays exactly where it is, not at its position in units
                shift = (_err(sc, part.x - origin[0]), _err(sc, part.y - origin[1]))
            elif on_grid:
                gp = part.pad(opts.on_grid[part.ref])
                gx, gy = rotate(gp.x, gp.y, r)
                shift = (origin_err[0] - _err(sc, gx), origin_err[1] - _err(sc, gy))
            ob, ok, op = (_rbox(sc, b, r, shift) for b in (part.box, kbox, pbox))
            boxes += [ob, ok, op]
            o = {
                "rot": r,
                "box": ob,
                "keep": ok,
                "pads": op,
                "shift_mm": list(shift),  # for exact_positions; the solver ignores it
            }
            if on_grid:
                gp = part.pad(opts.on_grid[part.ref])
                gx, gy = rotate(gp.x, gp.y, r)
                o["grid"] = [_u(sc, gx), _u(sc, gy)]
                o["grid_mm"] = [gx, gy]  # exact; the solver ignores it
            o["pins"] = {
                net: [_u(sc, c) for c in rotate(*members[net][i], r)]
                for net in nets
                if i in members[net]
            }
            options.append(o)
        extent += max(max(b[2] - b[0], b[3] - b[1]) for b in boxes)
        # a part's current place must lie inside the bound, a fixed one above all
        reach = max(abs(_u(sc, part.x - origin[0])), abs(_u(sc, part.y - origin[1])))
        far = max(far, reach + max(abs(v) for b in boxes for v in b))
        out_parts.append(
            {
                "ref": part.ref,
                "fixed": fixed,
                "hint": [
                    _u(sc, part.x - origin[0]),
                    _u(sc, part.y - origin[1]),
                    part.rot % 360 if fixed else round(part.rot) % 360,
                ],
                "overhang": _down(sc, opts.overhang.get(part.ref, 0.0)),
                "touch": [want[s] for s in want],
                "on_grid": on_grid,
                "options": options,
            }
        )
    apart = []
    for rule in opts.apart:
        try:
            apart.append(
                {
                    "a": [refs[r] for r in rule["a"]],
                    "b": [refs[r] for r in rule["b"]],
                    "gap": _up(sc, rule["gap"]),
                }
            )
        except KeyError as exc:
            raise ProjectError(f"[tool.inkibox.place].apart: {exc}") from exc
    w = opts.width if opts.width is not None else size[0]
    h = opts.height if opts.height is not None else size[1]
    for name, v in (("width", w), ("height", h)):
        if v is not None and abs(v * sc - round(v * sc)) > 1e-6:
            raise ProjectError(
                f"[tool.inkibox.place]: {name} {v} mm is no whole number of unit "
                f"{opts.unit} mm steps"
            )
    return {
        "parts": out_parts,
        # every length in units, as the solver will see it
        "bound": 2 * (extent + far)
        + (0 if w is None else _u(sc, w))
        + (0 if h is None else _u(sc, h))
        + _up(sc, opts.overhang_max())
        + 2 * (_up(sc, opts.edge) + _up(sc, opts.pad_edge))
        # every apart rule's gap between every pair it names, as if in one row
        + sum(_up(sc, r["gap"]) * len(r["a"]) * len(r["b"]) for r in opts.apart)
        # each part may need up to a lattice step to land on its grid / resolution
        + 2
        * len(parts)
        * ((grid["pitch"] if grid else 0) + max(1, _u(sc, opts.resolution))),
        "board": {
            "edge": _up(sc, opts.edge),
            "pad_edge": _up(sc, opts.pad_edge),
            "width": None if w is None else _u(sc, w),
            "height": None if h is None else _u(sc, h),
        },
        "grid": grid,
        "res": max(1, _u(sc, opts.resolution)),
        "apart": apart,
        "nets": nets,
        # wire_weight mm2 of board per mm of wire, as a ratio of integers: area_coef x
        # area (units2) + wire_coef x wire (units); a weight that rounds to 0 at a coarse
        # unit keeps its share
        "wire_coef": wire.numerator,
        "area_coef": wire.denominator,
        "scale": sc,
        "grid_mm": grid_mm,
        "time": opts.time_limit,
        "workers": opts.workers,
        "seed": opts.seed,
    }


def run_solver(problem: dict) -> dict:
    """Solve in-process when ``ortools`` imports, else in ``uv run --with ortools``."""
    if importlib.util.find_spec("ortools") is not None:
        from ._solver import solve

        return {**solve(problem), "scale": problem.get("scale", UNIT)}
    uv = shutil.which("uv")
    if uv is None:
        raise PlaceError(
            f"inkibox place needs OR-Tools: install {ORTOOLS!r} or put uv on PATH"
        )
    r = subprocess.run(
        [
            uv,
            "run",
            "--quiet",
            "--no-project",
            "--isolated",
            "--with",
            ORTOOLS,
            "python",
            str(SOLVER),
        ],
        input=json.dumps(problem),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise PlaceError(f"solver failed: {r.stderr.strip()[-2000:]}")
    return {**json.loads(r.stdout), "scale": problem.get("scale", UNIT)}


# --------------------------------------------------------------------------- write back


def _move(part: Part, x: float, y: float, rot: float) -> None:
    fp = part.node
    at = fp.child("at")
    assert at is not None
    _set_angle(at, rot, keep_zero=False)
    at.set_atom(0, number(round(x, 6) + 0.0))
    at.set_atom(1, number(round(y, 6) + 0.0))
    for item in fp.children():
        if item.head in ("pad", "property", "fp_text"):
            a = item.child("at")
            if a is not None:
                _set_angle(a, _num(a, 2) - part.rot + rot, keep_zero=item.head != "pad")
        elif item.head == "zone":  # stored in board coordinates
            for sub in item.walk():
                if sub.head == "xy":
                    lx, ly = rotate(
                        _num(sub, 0) - part.x, _num(sub, 1) - part.y, -part.rot
                    )
                    bx, by = rotate(lx, ly, rot)
                    sub.set_atom(0, number(round(x + bx, 6) + 0.0))
                    sub.set_atom(1, number(round(y + by, 6) + 0.0))
    part.x, part.y, part.rot = x, y, rot


def exact_positions(problem: dict, result: dict) -> list[tuple[float, float, float]]:
    """The solved positions in mm from the outline's corner, a grid part's exact. The
    solver keeps its pad on the grid with the grid origin and the pad's offset rounded to
    its unit (10.565 mm is no whole number of 0.01 mm), so the exact position is the
    solved one plus the difference of those roundings: the very shift :func:`build_problem`
    gave the part's boxes, so the solver judged it where it stands."""
    scale = problem["scale"]
    out = []
    for part, (ux, uy, rot) in zip(problem["parts"], result["parts"], strict=True):
        opt = next(o for o in part["options"] if o["rot"] == rot)
        dx, dy = opt.get("shift_mm", (0.0, 0.0))
        out.append((ux / scale + dx, uy / scale + dy, rot))
    return out


def apply_placement(
    sf: SFile,
    parts: list[Part],
    origin: tuple[float, float],
    result: dict,
    *,
    resize: bool,
    fixed: frozenset[int] = frozenset(),
    positions: list[tuple[float, float, float]] | None = None,
) -> list[str]:
    """Write the solved positions (``result["parts"]``: one per part, in order) and, with
    ``resize``, the outline's size; the refs that moved. ``fixed`` (indices into
    ``parts``) are never written: the solver sees them in its units, the board keeps them
    exactly. ``positions`` (mm from ``origin``, :func:`exact_positions`) replace the
    solver's rounded ones."""
    moved = []
    scale = result.get("scale", UNIT)
    if positions is None:
        positions = [(ux / scale, uy / scale, rot) for ux, uy, rot in result["parts"]]
    for i, (part, (px, py, rot)) in enumerate(zip(parts, positions, strict=True)):
        if i in fixed:
            continue
        x, y = origin[0] + px, origin[1] + py
        if abs(x - part.x) > 1e-6 or abs(y - part.y) > 1e-6 or (rot - part.rot) % 360:
            _move(part, x, y, rot)
            moved.append(part.ref)
    if resize:
        rect = next(
            r for r in sf.root.children("gr_rect") if r.value("layer") == "Edge.Cuts"
        )
        s, e = rect.child("start"), rect.child("end")
        assert s is not None and e is not None
        s.set_atom(0, number(origin[0]))
        s.set_atom(1, number(origin[1]))
        e.set_atom(0, number(round(origin[0] + result["width"] / scale, 6)))
        e.set_atom(1, number(round(origin[1] + result["height"] / scale, 6)))
    return moved


# --------------------------------------------------------------------------- the command


@dataclass(slots=True)
class PlaceResult:
    board: Path
    status: str
    width: float | None = None
    height: float | None = None
    wire: float | None = None
    moved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


def place_project(
    project_dir: Path, opts: PlaceOptions, *, dry_run: bool = False
) -> PlaceResult:
    project_dir = project_dir.resolve()
    boards = sorted(project_dir.glob("*.kicad_pcb"))
    if len(boards) != 1:
        raise ProjectError(
            f"{project_dir}: expected one .kicad_pcb, found {len(boards)}"
        )
    sf = SFile.load(boards[0])
    parts, outline = read_parts(sf)
    warnings = []
    if outline is None:
        raise ProjectError(
            f"{boards[0].name}: place needs the outline as one gr_rect and nothing "
            "else on Edge.Cuts (no cutouts, slots or drawn outlines)"
        )
    resize = opts.width is None or opts.height is None
    origin = (outline[0], outline[1])
    size = (
        None if resize and opts.width is None else outline[2] - outline[0],
        None if resize and opts.height is None else outline[3] - outline[1],
    )
    problem = build_problem(parts, origin, size, opts)
    result = run_solver(problem)
    res = PlaceResult(boards[0], result["status"], warnings=warnings)
    if not res.ok:
        return res
    scale = problem["scale"]
    res.width, res.height = result["width"] / scale, result["height"] / scale
    res.wire = result["wire"] / scale
    res.moved = apply_placement(
        sf,
        parts,
        origin,
        result,
        resize=True,
        fixed=frozenset(i for i, p in enumerate(problem["parts"]) if p["fixed"]),
        positions=exact_positions(problem, result),
    )
    routed = len(sf.root.children("segment")) + len(sf.root.children("via"))
    if routed and res.moved:
        warnings.append(f"{routed} track(s) / via(s) left where they were: route again")
    if not dry_run:
        sf.save()
    return res
