"""Build a KiCad 10 board: outline, footprints from libraries with nets on their pads,
tracks, vias and zones. Pairs with :class:`inkibox.kicad.schematic.Schematic`: a placed
schematic symbol carries the net of every pin, which becomes the net of the pad with the
same number."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path

from sexpdata import Symbol

from .libs import Libraries
from .schematic import PlacedSymbol
from .sexpr import (
    Node,
    S,
    atom_text,
    atoms_of,
    child,
    children,
    clone,
    head,
    num,
    replace_child,
    stable_uuid,
    write_pretty,
)

PCB_FORMAT = 20260206

LAYERS: list[tuple[int, str, str, str | None]] = [
    (0, "F.Cu", "signal", None),
    (2, "B.Cu", "signal", None),
    (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", None),
    (15, "B.Paste", "user", None),
    (5, "F.SilkS", "user", "F.Silkscreen"),
    (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user", None),
    (3, "B.Mask", "user", None),
    (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", None),
    (27, "Margin", "user", None),
    (31, "F.CrtYd", "user", "F.Courtyard"),
    (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user", None),
    (33, "B.Fab", "user", None),
]


@dataclass(slots=True)
class PlacedPad:
    number: str
    x: float  # board coordinates
    y: float
    kind: str  # smd | thru_hole | np_thru_hole | connect
    shape: str
    size: tuple[float, float]
    layers: frozenset[str]  # {"F.Cu"}, {"B.Cu"} or both
    net: str | None
    drill: float = 0.0  # hole diameter, 0 for SMD
    angle: float = (
        0.0  # orientation on the board (degrees, KiCad's sense), ``size`` before it
    )

    @property
    def radius(self) -> float:
        return max(self.size) / 2


@dataclass(slots=True)
class PlacedFootprint:
    ref: str
    lib_id: str
    x: float
    y: float
    rot: float
    layer: str
    node: Node
    pads: dict[str, PlacedPad] = field(default_factory=dict)  # first pad of each number
    all_pads: list[PlacedPad] = field(
        default_factory=list
    )  # every pad, duplicates included

    def pad(self, number: str) -> PlacedPad:
        return self.pads[str(number)]


def _xy(item: Node, key: str) -> tuple[float, float] | None:
    c = child(item, key)
    if c is None:
        return None
    a = atoms_of(c)
    return float(a[0]), float(a[1])


def _arc_extent(
    s: tuple[float, float], m: tuple[float, float], e: tuple[float, float]
) -> list[tuple[float, float]]:
    """Points that bound the arc from ``s`` through ``m`` to ``e``: its ends and every
    axis-extreme point of its circle that the arc passes."""
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # collinear: a straight segment
        return [s, m, e]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ox = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    oy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    r = math.hypot(ax - ox, ay - oy)
    tau = 2 * math.pi

    def ang(p: tuple[float, float]) -> float:
        return math.atan2(p[1] - oy, p[0] - ox) % tau

    t_s, t_m, t_e = ang(s), ang(m), ang(e)
    span = (t_e - t_s) % tau or tau
    if (t_m - t_s) % tau > span:  # the arc runs the other way: walk it from e to s
        t_s, span = t_e, tau - span
    pts = [s, e]
    for k in range(4):
        phi = k * math.pi / 2
        if (phi - t_s) % tau <= span + 1e-9:
            pts.append((ox + r * math.cos(phi), oy + r * math.sin(phi)))
    return pts


def _shape_points(item: Node) -> list[tuple[float, float]]:
    """Points whose bounding box is that of a graphic (``fp_*`` or a custom pad's ``gr_*``
    primitive), in its own coordinates; stroke width is not included."""
    kind = (head(item) or "")[3:]
    if kind == "circle":
        c, e = _xy(item, "center"), _xy(item, "end")
        if c is None or e is None:
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    if kind == "arc":
        s, m, e = _xy(item, "start"), _xy(item, "mid"), _xy(item, "end")
        if s is None or e is None:
            return []
        return _arc_extent(s, m, e) if m is not None else [s, e]
    pts = [p for p in (_xy(item, k) for k in ("start", "end")) if p is not None]
    p = child(item, "pts")
    if p is not None:
        for seg in p[1:]:
            h = head(seg)
            if h == "xy":
                a = atoms_of(seg)
                pts.append((float(a[0]), float(a[1])))
            elif h == "arc":  # a polygon edge that is an arc
                s, m, e = _xy(seg, "start"), _xy(seg, "mid"), _xy(seg, "end")
                if s is not None and m is not None and e is not None:
                    pts += _arc_extent(s, m, e)
    return pts  # line, rect, poly; a bezier lies within its control points


def _pad_points(pad: Node) -> list[tuple[float, float]]:
    """Points whose bounding box is that of a pad's copper, in footprint coordinates."""
    at = atoms_of(child(pad, "at") or [S("at"), 0, 0])
    x, y = float(at[0]), float(at[1])
    rot = float(at[2]) if len(at) > 2 else 0.0
    size = atoms_of(child(pad, "size") or [S("size"), 0, 0])
    w, h = float(size[0]) / 2, float(size[1]) / 2
    a = atoms_of(pad)
    shape = atom_text(a[2]) if len(a) > 2 else "rect"
    if shape == "circle":
        return [(x - w, y - w), (x + w, y + w)]
    if shape == "trapezoid":  # rect_delta widens one pair of sides
        delta = child(pad, "rect_delta")
        if delta is not None:
            dx, dy = (float(v) for v in atoms_of(delta)[:2])
            w, h = w + abs(dy) / 2, h + abs(dx) / 2
    local = [(-w, -h), (w, -h), (w, h), (-w, h)]
    if shape == "custom":
        prims = child(pad, "primitives")
        if prims is not None:
            for g in prims[1:]:
                if isinstance(g, list):
                    local += _shape_points(g)
    return [(x + px, y + py) for px, py in (rotate(qx, qy, rot) for qx, qy in local)]


def _fp_bbox(
    node: Node, layer_filter: str | None = "F.CrtYd"
) -> tuple[float, float, float, float]:
    """Bounding box (footprint coordinates) of the graphics on ``layer_filter`` (all graphics
    when it has none) and the pads. Circles and arcs count by their true extent, pads by
    their shape and rotation."""
    pts: list[tuple[float, float]] = []
    for item in node[1:]:
        if not isinstance(item, list):
            continue
        h = head(item) or ""
        if h.startswith("fp_") and h != "fp_text":
            lay = child(item, "layer")
            if layer_filter is None or (
                lay is not None and atom_text(lay[1]) == layer_filter
            ):
                pts += _shape_points(item)
    if not pts and layer_filter is not None:
        return _fp_bbox(node, None)
    for pad in children(node, "pad"):
        pts += _pad_points(pad)
    if not pts:
        return 0.0, 0.0, 0.0, 0.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


class Board:
    def __init__(
        self,
        project: str,
        libs: Libraries,
        *,
        thickness: float = 1.6,
        paper: str = "A4",
        title: str = "",
        sheet_file: str | None = None,
        copper_layers: int = 2,
        stackup: Node | None = None,
    ) -> None:
        """``copper_layers`` is an even number (2, 4, … 32); ``stackup`` an optional
        ``(stackup …)`` node for the board setup (see :func:`stackup`)."""
        if copper_layers < 2 or copper_layers > 32 or copper_layers % 2:
            raise ValueError(
                f"copper_layers must be even, 2 to 32 (KiCad's limit), not {copper_layers}"
            )
        if stackup is not None:
            want = ["F.Cu", *[f"In{i}.Cu" for i in range(1, copper_layers - 1)], "B.Cu"]
            have = [
                atom_text(row[1])
                for row in children(stackup, "layer")
                if (kind := child(row, "type")) is not None
                and atom_text(kind[1]) == "copper"
            ]
            if have != want:
                raise ValueError(
                    f"stackup copper layers {have} do not match a {copper_layers}-layer "
                    f"board ({want})"
                )
        self.project = project
        self.libs = libs
        self.copper_layers = copper_layers
        self.stackup = stackup
        self.thickness = thickness
        self.paper = paper
        self.title = title
        self.sheet_file = sheet_file or f"{project}.kicad_sch"
        self.root_sheet_uuid: str | None = None
        self.outline: list[Node] = []
        self.footprints: list[PlacedFootprint] = []
        self.segments: list[tuple[float, float, float, float, float, str, str]] = []
        self.vias: list[tuple[float, float, float, float, str, tuple[str, str]]] = []
        self.zones: list[Node] = []
        self.texts: list[Node] = []
        self._fp_cache: dict[str, Node] = {}

    # ------------------------------------------------------------------ outline

    def outline_rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.outline.append(
            [
                S("gr_rect"),
                [S("start"), num(x0), num(y0)],
                [S("end"), num(x1), num(y1)],
                [S("stroke"), [S("width"), 0.05], [S("type"), S("default")]],
                [S("fill"), S("no")],
                [S("layer"), "Edge.Cuts"],
                [S("uuid"), stable_uuid(self.project, "edge", f"{x0},{y0},{x1},{y1}")],
            ]
        )

    def bbox(self) -> tuple[float, float, float, float]:
        pts: list[tuple[float, float]] = []
        for item in self.outline:
            for key in ("start", "end"):
                c = child(item, key)
                if c is not None:
                    a = atoms_of(c)
                    pts.append((float(a[0]), float(a[1])))
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    # ------------------------------------------------------------------ footprints

    def library_footprint(self, lib_id: str) -> Node:
        if lib_id not in self._fp_cache:
            self._fp_cache[lib_id] = self.libs.footprint(lib_id)
        return self._fp_cache[lib_id]

    def footprint_size(self, lib_id: str) -> tuple[float, float, float, float]:
        """Courtyard bounding box of a library footprint in its own coordinates."""
        return _fp_bbox(self.library_footprint(lib_id))

    def place(
        self,
        symbol: PlacedSymbol | None,
        at: tuple[float, float],
        rot: float = 0,
        *,
        ref: str | None = None,
        lib_id: str | None = None,
        value: str | None = None,
        layer: str = "F.Cu",
        hide_reference: bool = False,
        in_bom: bool | None = None,
    ) -> PlacedFootprint:
        """Place the footprint of a schematic symbol (nets from its pins) or a bare footprint."""
        if symbol is not None:
            ref = ref or symbol.ref
            lib_id = lib_id or symbol.footprint
            value = value if value is not None else symbol.value
        if not ref or not lib_id:
            raise ValueError("place() needs a symbol or ref + lib_id")
        if layer not in ("F.Cu", "B.Cu"):
            raise ValueError(f"a footprint goes on F.Cu or B.Cu, not {layer}")
        back = layer == "B.Cu"
        lib = clone(self.library_footprint(lib_id))
        fx, fy = at
        if back:
            lib = self._flipped(lib, rot, (fx, fy))
        node: Node = [S("footprint"), lib_id]
        for item in lib[2:]:
            if isinstance(item, list) and head(item) in (
                "version",
                "generator",
                "generator_version",
                "layer",
            ):
                continue
            node.append(item)
        node.insert(2, [S("layer"), layer])
        node.insert(3, [S("uuid"), stable_uuid(self.project, "footprint", ref)])
        node.insert(
            4,
            [S("at"), num(fx), num(fy), num(rot)]
            if rot
            else [S("at"), num(fx), num(fy)],
        )
        # properties: Reference / Value from the symbol, Datasheet / Description too so that
        # `kicad-cli pcb drc --schematic-parity` sees no field mismatch
        values = {"Reference": ref, "Value": value or ""}
        if symbol is not None:
            lib_props = {
                atom_text(p[1]): atom_text(p[2])
                for p in children(symbol.lib, "property")
            }
            values["Datasheet"] = symbol.fields.get(
                "Datasheet", lib_props.get("Datasheet", "")
            )
            values["Description"] = symbol.fields.get(
                "Description", lib_props.get("Description", "")
            )
        seen_props: set[str] = set()
        for prop in children(node, "property"):
            key = atom_text(prop[1])
            seen_props.add(key)
            if key in values:
                prop[2] = values[key]
            if key == "Reference" and hide_reference and child(prop, "hide") is None:
                replace_child(prop, "hide", [S("hide"), S("yes")])
            if not back:  # a flipped footprint's texts are already turned
                _rotate_text(prop, rot)
            replace_child(
                prop, "uuid", [S("uuid"), stable_uuid(self.project, ref, "prop", key)]
            )
        for key in ("Datasheet", "Description"):
            if key in values and key not in seen_props:
                node.append(
                    [
                        S("property"),
                        key,
                        values[key],
                        [S("at"), 0, 0, num(rot)],
                        [S("layer"), "B.Fab" if back else "F.Fab"],
                        [S("hide"), S("yes")],
                        [S("uuid"), stable_uuid(self.project, ref, "prop", key)],
                        [
                            S("effects"),
                            [
                                S("font"),
                                [S("size"), 1.27, 1.27],
                                [S("thickness"), 0.15],
                            ],
                        ],
                    ]
                )
        if not back:
            for txt in children(node, "fp_text"):
                _rotate_text(txt, rot)
        if in_bom is not None:
            attr = child(node, "attr")
            flags = [
                a
                for a in (atoms_of(attr) if attr is not None else [])
                if atom_text(a) != "exclude_from_bom"
            ]
            if not in_bom:
                flags.append(S("exclude_from_bom"))
            replace_child(node, "attr", [S("attr"), *flags] if flags else None)
        if symbol is not None:
            sheet = symbol.sheet
            if sheet is not None and sheet.parent is not None:
                # a sub-sheet: the path runs through the sheet symbols, not the root
                path = sheet.path.split("/", 2)[2] if sheet.path.count("/") > 1 else ""
                node.append([S("path"), f"/{path}/{symbol.uuid}"])
                node.append([S("sheetname"), sheet.sheet_names])
                node.append([S("sheetfile"), sheet.file])
            else:
                node.append([S("path"), f"/{symbol.uuid}"])
                node.append([S("sheetname"), "/"])
                node.append([S("sheetfile"), self.sheet_file])
        fp = PlacedFootprint(ref, lib_id, fx, fy, rot, layer, node)
        for pad in children(node, "pad"):
            a = atoms_of(pad)
            number = atom_text(a[0]) if a else ""
            kind = atom_text(a[1]) if len(a) > 1 else "smd"
            shape = atom_text(a[2]) if len(a) > 2 else "circle"
            at_node = child(pad, "at")
            pa = atoms_of(at_node) if at_node is not None else [0, 0]
            px, py = float(pa[0]), float(pa[1])
            pang = float(pa[2]) if len(pa) > 2 else 0.0
            if back:  # angles already absolute in a flipped footprint
                pang -= rot
            elif rot:
                replace_child(
                    pad, "at", [S("at"), num(px), num(py), num((pang + rot) % 360)]
                )
            rx, ry = rotate(px, py, rot)
            size_node = child(pad, "size")
            sz = atoms_of(size_node) if size_node is not None else [0, 0]
            layers_node = child(pad, "layers")
            lay_atoms = (
                [atom_text(v) for v in atoms_of(layers_node)] if layers_node else []
            )
            cu = frozenset(
                lay
                for lay in ("F.Cu", "B.Cu", *self.inner_layers)
                if any(v in ("*.Cu", lay) for v in lay_atoms)
            )
            net: str | None = None
            if symbol is not None and number and kind != "np_thru_hole":
                pin = symbol.pins.get(number)
                if pin is not None and pin.net is not None:
                    net = pin.net
                    _insert_after_layers(pad, [S("net"), net])
                    _insert_after_layers(pad, [S("pintype"), pin.etype], after="net")
                    _insert_after_layers(pad, [S("pinfunction"), pin.name], after="net")
            replace_child(
                pad,
                "uuid",
                [
                    S("uuid"),
                    stable_uuid(self.project, ref, "pad", number, f"{px},{py}"),
                ],
            )
            drill_node = child(pad, "drill")
            drill = 0.0
            if drill_node is not None:
                nums = [
                    float(v) for v in atoms_of(drill_node) if isinstance(v, int | float)
                ]
                drill = max(nums) if nums else 0.0
            placed = PlacedPad(
                number,
                num(fx + rx),
                num(fy + ry),
                kind,
                shape,
                (float(sz[0]), float(sz[1])),
                cu,
                net,
                drill,
                (pang + rot) % 360,
            )
            fp.all_pads.append(placed)
            fp.pads.setdefault(number, placed)
        # stable uuids for the graphics and texts, by their place in the footprint: libraries
        # often ship `fp_text user "${REFERENCE}"` without one, and KiCad would make one up
        for index, item in enumerate(node):
            kind = head(item)
            if kind in ("fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly"):
                key = ("gfx", str(index))
            elif kind == "fp_text":
                key = ("fp_text", str(index))
            elif kind == "zone":
                key = ("zone", str(index))
                if not back:  # placed with the flip
                    _place_zone(item, fx, fy, rot)
            else:
                continue
            replace_child(
                item, "uuid", [S("uuid"), stable_uuid(self.project, ref, *key)]
            )
        self.footprints.append(fp)
        return fp

    @property
    def inner_layers(self) -> list[str]:
        return [f"In{i}.Cu" for i in range(1, self.copper_layers - 1)]

    def _flipped(self, lib: Node, rot: float, at: tuple[float, float]) -> Node:
        """A library footprint as a board stores it on the back: mirrored top to bottom,
        layers swapped, texts mirrored, angles absolute (``inkibox update``'s own flip,
        which matches KiCad's)."""
        from ..update.footprints import place as place_items
        from .sexpr import _to_sfile, parse_one
        from .sfile import Node as SNode
        from .sfile import format_node

        sf = _to_sfile(lib)
        assert isinstance(sf, SNode)
        items = place_items(
            sf, side="B.Cu", rotation=rot, copper=self.copper_layers, origin=at
        )
        out = SNode("footprint", [sf.items[0], *items])
        return parse_one(format_node(out))

    # ------------------------------------------------------------------ copper

    def track(
        self, pts: list[tuple[float, float]], width: float, layer: str, net: str
    ) -> None:
        for (x1, y1), (x2, y2) in itertools.pairwise(pts):
            if (x1, y1) != (x2, y2):
                self.segments.append(
                    (num(x1), num(y1), num(x2), num(y2), width, layer, net)
                )

    def via(
        self,
        at: tuple[float, float],
        net: str,
        size: float = 0.8,
        drill: float = 0.4,
        layers: tuple[str, str] = ("F.Cu", "B.Cu"),
    ) -> None:
        """A via; ``layers`` other than F.Cu–B.Cu make it blind or buried."""
        self.vias.append((num(at[0]), num(at[1]), size, drill, net, layers))

    def zone(
        self,
        net: str,
        layer: str,
        pts: list[tuple[float, float]],
        *,
        name: str = "",
        clearance: float = 0.3,
        min_thickness: float = 0.25,
        thermal_gap: float = 0.3,
        thermal_bridge: float = 0.4,
        priority: int = 0,
        solid_pads: bool = False,
        remove_islands: bool = True,
    ) -> None:
        node: Node = [
            S("zone"),
            [S("net"), net],
            [S("layer"), layer],
            [S("uuid"), stable_uuid(self.project, "zone", net, layer, name)],
            [S("name"), name],
        ]
        if priority:
            node.append([S("priority"), priority])
        node += [
            [S("hatch"), S("edge"), 0.5],
            [S("connect_pads"), S("yes"), [S("clearance"), num(clearance)]]
            if solid_pads
            else [S("connect_pads"), [S("clearance"), num(clearance)]],
            [S("min_thickness"), num(min_thickness)],
            [S("filled_areas_thickness"), S("no")],
            [
                S("fill"),
                S("yes"),
                [S("thermal_gap"), num(thermal_gap)],
                [S("thermal_bridge_width"), num(thermal_bridge)],
                [S("island_removal_mode"), 0 if remove_islands else 1],
                [S("island_area_min"), 10],
            ],
            [S("polygon"), [S("pts"), *[[S("xy"), num(x), num(y)] for x, y in pts]]],
        ]
        self.zones.append(node)

    def keepout(
        self,
        pts: list[tuple[float, float]],
        *,
        layers: tuple[str, ...] = ("F.Cu", "B.Cu"),
        name: str = "",
        tracks: bool = False,
        vias: bool = False,
        pours: bool = False,
        pads: bool = True,
        footprints: bool = True,
    ) -> None:
        """A rule area (keepout): what may not be placed inside ``pts`` on ``layers``
        (``False`` = not allowed). DRC enforces it, and KiCad's Specctra export hands it to
        Freerouting as a keepout, so ``inkibox route`` respects it too."""

        def rule(ok: bool) -> Symbol:
            return S("allowed") if ok else S("not_allowed")

        self.zones.append(
            [
                S("zone"),
                [S("layers"), *layers],
                [S("uuid"), stable_uuid(self.project, "keepout", name, str(pts))],
                *([[S("name"), name]] if name else []),
                [S("hatch"), S("edge"), 0.5],
                [S("connect_pads"), [S("clearance"), 0]],
                [S("min_thickness"), 0.25],
                [
                    S("keepout"),
                    [S("tracks"), rule(tracks)],
                    [S("vias"), rule(vias)],
                    [S("pads"), rule(pads)],
                    [S("copperpour"), rule(pours)],
                    [S("footprints"), rule(footprints)],
                ],
                [S("placement"), [S("enabled"), S("no")], [S("sheetname"), ""]],
                [
                    S("fill"),
                    [S("thermal_gap"), 0.5],
                    [S("thermal_bridge_width"), 0.5],
                    [S("island_removal_mode"), 0],
                ],
                [
                    S("polygon"),
                    [S("pts"), *[[S("xy"), num(x), num(y)] for x, y in pts]],
                ],
            ]
        )

    def text(
        self,
        text: str,
        at: tuple[float, float],
        *,
        layer: str = "F.SilkS",
        size: float = 1.5,
        thickness: float = 0.2,
    ) -> None:
        self.texts.append(
            [
                S("gr_text"),
                text,
                [S("at"), num(at[0]), num(at[1]), 0],
                [S("layer"), layer],
                [S("uuid"), stable_uuid(self.project, "text", text, f"{at}")],
                [
                    S("effects"),
                    [
                        S("font"),
                        [S("size"), num(size), num(size)],
                        [S("thickness"), num(thickness)],
                    ],
                ],
            ]
        )

    # ------------------------------------------------------------------ output

    def to_node(self) -> Node:
        root: Node = [
            S("kicad_pcb"),
            [S("version"), PCB_FORMAT],
            [S("generator"), "inkibox"],
            [S("generator_version"), "0.1"],
            [
                S("general"),
                [S("thickness"), num(self.thickness)],
                [S("legacy_teardrops"), S("no")],
            ],
            [S("paper"), self.paper],
        ]
        if self.title:
            root.append([S("title_block"), [S("title"), self.title]])
        layers: Node = [S("layers")]
        inner = [
            (4 + 2 * i, name, "signal", None)
            for i, name in enumerate(self.inner_layers)
        ]
        for idx, name, kind, user in LAYERS[:1] + inner + LAYERS[1:]:
            layers.append([idx, name, S(kind)] + ([user] if user else []))
        root.append(layers)
        setup: Node = [S("setup")]
        if self.stackup is not None:
            setup.append(self.stackup)
        setup += [
            [S("pad_to_mask_clearance"), 0],
            [S("allow_soldermask_bridges_in_footprints"), S("no")],
        ]
        root.append(setup)
        root.extend(self.outline)
        root.extend(self.texts)
        for fp in self.footprints:
            root.append(fp.node)
        for i, (x1, y1, x2, y2, w, layer, net) in enumerate(self.segments):
            root.append(
                [
                    S("segment"),
                    [S("start"), num(x1), num(y1)],
                    [S("end"), num(x2), num(y2)],
                    [S("width"), num(w)],
                    [S("layer"), layer],
                    [S("net"), net],
                    [
                        S("uuid"),
                        stable_uuid(
                            self.project, "segment", str(i), f"{x1},{y1},{x2},{y2}"
                        ),
                    ],
                ]
            )
        for i, (x, y, size, drill, net, vlayers) in enumerate(self.vias):
            through = tuple(vlayers) == ("F.Cu", "B.Cu")
            root.append(
                [
                    S("via"),
                    *([] if through else [S("blind")]),
                    [S("at"), num(x), num(y)],
                    [S("size"), num(size)],
                    [S("drill"), num(drill)],
                    [S("layers"), *vlayers],
                    [S("net"), net],
                    [S("uuid"), stable_uuid(self.project, "via", str(i), f"{x},{y}")],
                ]
            )
        root.extend(self.zones)
        root.append([S("embedded_fonts"), S("no")])
        return root

    def write(self, path: Path) -> None:
        write_pretty(path, self.to_node())


def stackup(
    layers: list[tuple[str, str, float, dict[str, str | float]]],
    *,
    copper_finish: str = "ENIG",
    impedance_controlled: bool = True,
) -> Node:
    """A ``(stackup …)`` node from ``(name, type, thickness_mm, extra)`` rows, top to
    bottom, as KiCad's Board Setup writes it: copper (``"copper"``), dielectrics
    (``"core"``/``"prepreg"``, extra ``material``, ``epsilon_r``, ``loss_tangent``), mask
    (``"Top Solder Mask"`` …), silk and paste (thickness 0: none written)."""
    node: Node = [S("stackup")]
    for name, kind, thickness, extra in layers:
        row: Node = [S("layer"), name, [S("type"), kind]]
        if thickness:
            row.append([S("thickness"), num(thickness)])
        for key in ("material", "epsilon_r", "loss_tangent", "color"):
            if key in extra:
                val = extra[key]
                row.append([S(key), val if isinstance(val, str) else num(val)])
        node.append(row)
    node.append([S("copper_finish"), copper_finish])
    node.append(
        [S("dielectric_constraints"), S("yes" if impedance_controlled else "no")]
    )
    return node


def rotate(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """A point turned like KiCad's ``RotatePoint`` (y down, positive angles counter-clockwise
    on screen)."""
    if angle_deg % 360 == 0:
        return x, y
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def _rotate_text(item: Node, rot: float) -> None:
    """KiCad stores text orientation inside a footprint as an absolute angle."""
    if not rot:
        return
    at = child(item, "at")
    if at is None:
        return
    a = atoms_of(at)
    x, y = float(a[0]), float(a[1])
    ang = float(a[2]) if len(a) > 2 else 0.0
    replace_child(item, "at", [S("at"), num(x), num(y), num((ang + rot) % 360)])


def _place_zone(zone: Node, fx: float, fy: float, rot: float) -> None:
    """A footprint's own zone (a keepout, a pour) is stored in board coordinates in a
    .kicad_pcb, unlike the rest of the footprint: move its outline with the footprint."""
    for poly in children(zone, "polygon"):
        for pts in children(poly, "pts"):
            for xy in children(pts, "xy"):
                x, y = rotate(float(xy[1]), float(xy[2]), rot)
                xy[1], xy[2] = num(fx + x), num(fy + y)


def _insert_after_layers(pad: Node, new: Node, after: str = "layers") -> None:
    for i, item in enumerate(pad):
        if isinstance(item, list) and head(item) == after:
            pad.insert(i + 1, new)
            return
    pad.append(new)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
