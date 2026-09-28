"""Build a KiCad 10 board: outline, footprints from libraries with nets on their pads,
tracks, vias and zones. Pairs with :class:`inkibox.kicad.schematic.Schematic`: a placed
schematic symbol carries the net of every pin, which becomes the net of the pad with the
same number."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path

from kippm.board import rotate

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


def _fp_bbox(
    node: Node, layer_filter: str | None = "F.CrtYd"
) -> tuple[float, float, float, float]:
    """Bounding box (footprint coordinates) of the graphics on ``layer_filter``, falling back
    to all graphics and pads."""
    pts: list[tuple[float, float]] = []

    def collect(item: Node) -> None:
        for key in ("start", "end", "mid", "center"):
            c = child(item, key)
            if c is not None:
                a = atoms_of(c)
                pts.append((float(a[0]), float(a[1])))
        p = child(item, "pts")
        if p is not None:
            for xy in children(p, "xy"):
                a = atoms_of(xy)
                pts.append((float(a[0]), float(a[1])))

    for item in node[1:]:
        if not isinstance(item, list):
            continue
        h = head(item) or ""
        if h.startswith("fp_") and h != "fp_text":
            lay = child(item, "layer")
            if layer_filter is None or (
                lay is not None and atom_text(lay[1]) == layer_filter
            ):
                collect(item)
    if not pts and layer_filter is not None:
        return _fp_bbox(node, None)
    for pad in children(node, "pad"):
        at_node = child(pad, "at")
        size_node = child(pad, "size")
        at = atoms_of(at_node) if at_node is not None else [0, 0]
        size = atoms_of(size_node) if size_node is not None else [0, 0]
        x, y = float(at[0]), float(at[1])
        w, h = float(size[0]) / 2, float(size[1]) / 2
        pts += [(x - w, y - h), (x + w, y + h)]
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
    ) -> None:
        self.project = project
        self.libs = libs
        self.thickness = thickness
        self.paper = paper
        self.title = title
        self.sheet_file = sheet_file or f"{project}.kicad_sch"
        self.root_sheet_uuid: str | None = None
        self.outline: list[Node] = []
        self.footprints: list[PlacedFootprint] = []
        self.segments: list[tuple[float, float, float, float, float, str, str]] = []
        self.vias: list[tuple[float, float, float, float, str]] = []
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
        if layer != "F.Cu":
            raise NotImplementedError("only front-side placement is supported")
        lib = clone(self.library_footprint(lib_id))
        fx, fy = at
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
                        [S("layer"), "F.Fab"],
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
            if rot:
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
                for lay in ("F.Cu", "B.Cu")
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
            )
            fp.all_pads.append(placed)
            fp.pads.setdefault(number, placed)
        for item in node[1:]:
            if isinstance(item, list) and head(item) in (
                "fp_line",
                "fp_rect",
                "fp_arc",
                "fp_circle",
                "fp_poly",
            ):
                replace_child(
                    item,
                    "uuid",
                    [S("uuid"), stable_uuid(self.project, ref, "gfx", str(id(item)))],
                )
        self.footprints.append(fp)
        return fp

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
        self, at: tuple[float, float], net: str, size: float = 0.8, drill: float = 0.4
    ) -> None:
        self.vias.append((num(at[0]), num(at[1]), size, drill, net))

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
        for idx, name, kind, user in LAYERS:
            row: Node = [S("layers"), idx, name, S(kind)]
            row = [idx, name, S(kind)] + ([user] if user else [])
            layers.append(row)
        root.append(layers)
        root.append(
            [
                S("setup"),
                [S("pad_to_mask_clearance"), 0],
                [S("allow_soldermask_bridges_in_footprints"), S("no")],
            ]
        )
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
        for i, (x, y, size, drill, net) in enumerate(self.vias):
            root.append(
                [
                    S("via"),
                    [S("at"), num(x), num(y)],
                    [S("size"), num(size)],
                    [S("drill"), num(drill)],
                    [S("layers"), "F.Cu", "B.Cu"],
                    [S("net"), net],
                    [S("uuid"), stable_uuid(self.project, "via", str(i), f"{x},{y}")],
                ]
            )
        root.extend(self.zones)
        root.append([S("embedded_fonts"), S("no")])
        return root

    def write(self, path: Path) -> None:
        write_pretty(path, self.to_node())


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


def _insert_after_layers(pad: Node, new: Node, after: str = "layers") -> None:
    for i, item in enumerate(pad):
        if isinstance(item, list) and head(item) == after:
            pad.insert(i + 1, new)
            return
    pad.append(new)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])
