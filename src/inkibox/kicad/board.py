"""Build a KiCad 10 board: outline, footprints from libraries with nets on their pads,
tracks, vias and zones. Pairs with :class:`inkibox.kicad.schematic.Schematic`: a placed
schematic symbol carries the net of every pin, which becomes the net of the pad with the
same number."""

from __future__ import annotations

import itertools
from pathlib import Path

from sexpdata import Symbol

from .footprint_place import (
    PlacedFootprint,
    PlacedPad,
    field_values,
    flipped,
    link_symbol,
    new_node,
    place_pad,
    set_fields,
    set_in_bom,
    stamp_items,
)
from .footprint_place import insert_after as _insert_after  # noqa: F401 - former home
from .footprint_place import place_zone as _place_zone  # noqa: F401
from .footprint_place import rotate_text as _rotate_text  # noqa: F401
from .geometry import arc_extent as _arc_extent  # noqa: F401
from .geometry import distance, fp_bbox, rotate
from .geometry import pad_points as _pad_points  # noqa: F401
from .geometry import shape_points as _shape_points  # noqa: F401
from .libs import Libraries
from .placed import PlacedSymbol
from .sexpr import (
    Node,
    S,
    atoms_of,
    child,
    children,
    clone,
    num,
    stable_uuid,
    write_pretty,
)
from .stackup import (
    LAYERS,
    check_copper_layers,
    check_stackup,
    inner_layers,
    layers_node,
    stackup,
)

PCB_FORMAT = 20260206
_fp_bbox = fp_bbox  # former name, kept for callers

__all__ = [
    "LAYERS",
    "PCB_FORMAT",
    "Board",
    "PlacedFootprint",
    "PlacedPad",
    "distance",
    "rotate",
    "stackup",
]


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
        check_copper_layers(copper_layers)
        if stackup is not None:
            check_stackup(stackup, copper_layers)
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
        return fp_bbox(self.library_footprint(lib_id))

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
        if back:
            lib = flipped(lib, rot, at, self.copper_layers)
        uuid = stable_uuid(self.project, "footprint", ref)
        node = new_node(lib, lib_id, layer, uuid, at, rot)
        set_fields(
            node,
            field_values(symbol, ref, value),
            project=self.project,
            ref=ref,
            rot=rot,
            back=back,
            hide_reference=hide_reference,
        )
        if in_bom is not None:
            set_in_bom(node, in_bom)
        if symbol is not None:
            link_symbol(node, symbol, self.sheet_file)
        fp = PlacedFootprint(ref, lib_id, at[0], at[1], rot, layer, node)
        copper = ["F.Cu", "B.Cu", *self.inner_layers]
        for pad in children(node, "pad"):
            placed = place_pad(
                pad, fp, symbol, project=self.project, copper=copper, back=back
            )
            fp.all_pads.append(placed)
            fp.pads.setdefault(placed.number, placed)
        stamp_items(node, project=self.project, ref=ref, back=back, at=at, rot=rot)
        self.footprints.append(fp)
        return fp

    @property
    def inner_layers(self) -> list[str]:
        return inner_layers(self.copper_layers)

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
        root.append(layers_node(self.copper_layers))
        root.append(self._setup_node())
        root.extend(self.outline)
        root.extend(self.texts)
        root += [fp.node for fp in self.footprints]
        root += [self._segment_node(i, seg) for i, seg in enumerate(self.segments)]
        root += [self._via_node(i, via) for i, via in enumerate(self.vias)]
        root.extend(self.zones)
        root.append([S("embedded_fonts"), S("no")])
        return root

    def _setup_node(self) -> Node:
        setup: Node = [S("setup")]
        if self.stackup is not None:
            setup.append(self.stackup)
        setup += [
            [S("pad_to_mask_clearance"), 0],
            [S("allow_soldermask_bridges_in_footprints"), S("no")],
        ]
        return setup

    def _segment_node(self, i: int, seg: tuple) -> Node:
        x1, y1, x2, y2, w, layer, net = seg
        return [
            S("segment"),
            [S("start"), num(x1), num(y1)],
            [S("end"), num(x2), num(y2)],
            [S("width"), num(w)],
            [S("layer"), layer],
            [S("net"), net],
            [
                S("uuid"),
                stable_uuid(self.project, "segment", str(i), f"{x1},{y1},{x2},{y2}"),
            ],
        ]

    def _via_node(self, i: int, via: tuple) -> Node:
        x, y, size, drill, net, vlayers = via
        through = tuple(vlayers) == ("F.Cu", "B.Cu")
        return [
            S("via"),
            *([] if through else [S("blind")]),
            [S("at"), num(x), num(y)],
            [S("size"), num(size)],
            [S("drill"), num(drill)],
            [S("layers"), *vlayers],
            [S("net"), net],
            [S("uuid"), stable_uuid(self.project, "via", str(i), f"{x},{y}")],
        ]

    def write(self, path: Path) -> None:
        write_pretty(path, self.to_node())
