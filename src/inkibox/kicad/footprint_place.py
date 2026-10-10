"""A library footprint turned into a board footprint, step by step: header, fields,
attributes, the link to its schematic symbol, its pads (with nets) and stable uuids for
everything. :meth:`inkibox.kicad.board.Board.place` runs the steps in order."""

from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import rotate
from .placed import PlacedSymbol
from .sexpr import (
    Node,
    S,
    atom_text,
    atoms_of,
    child,
    children,
    head,
    num,
    replace_child,
    stable_uuid,
)

LIB_HEADER = ("version", "generator", "generator_version", "layer")
GRAPHICS = ("fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly")


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


# --------------------------------------------------------------------------- small edits


def rotate_text(item: Node, rot: float) -> None:
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


def place_zone(zone: Node, fx: float, fy: float, rot: float) -> None:
    """A footprint's own zone (a keepout, a pour) is stored in board coordinates in a
    .kicad_pcb, unlike the rest of the footprint: move its outline with the footprint."""
    for poly in children(zone, "polygon"):
        for pts in children(poly, "pts"):
            for xy in children(pts, "xy"):
                x, y = rotate(float(xy[1]), float(xy[2]), rot)
                xy[1], xy[2] = num(fx + x), num(fy + y)


def insert_after(pad: Node, new: Node, after: str = "layers") -> None:
    for i, item in enumerate(pad):
        if isinstance(item, list) and head(item) == after:
            pad.insert(i + 1, new)
            return
    pad.append(new)


def flipped(lib: Node, rot: float, at: tuple[float, float], copper: int) -> Node:
    """A library footprint as a board stores it on the back: mirrored top to bottom,
    layers swapped, texts mirrored, angles absolute (``inkibox update``'s own flip,
    which matches KiCad's)."""
    from ..update.placement import place as place_items
    from .sexpr import _to_sfile, parse_one
    from .sfile import Node as SNode
    from .sfile import format_node

    sf = _to_sfile(lib)
    assert isinstance(sf, SNode)
    items = place_items(sf, side="B.Cu", rotation=rot, copper=copper, origin=at)
    out = SNode("footprint", [sf.items[0], *items])
    return parse_one(format_node(out))


# --------------------------------------------------------------------------- the steps


def new_node(
    lib: Node, lib_id: str, layer: str, uuid: str, at: tuple[float, float], rot: float
) -> Node:
    """The board footprint: the library's items without its header, under the board's
    layer, uuid and position."""
    node: Node = [S("footprint"), lib_id]
    node += [
        item
        for item in lib[2:]
        if not (isinstance(item, list) and head(item) in LIB_HEADER)
    ]
    fx, fy = at
    node.insert(2, [S("layer"), layer])
    node.insert(3, [S("uuid"), uuid])
    node.insert(
        4, [S("at"), num(fx), num(fy), num(rot)] if rot else [S("at"), num(fx), num(fy)]
    )
    return node


def field_values(symbol: PlacedSymbol | None, ref: str, value: str | None) -> dict:
    """Reference / Value, and Datasheet / Description from the symbol too, so that
    `kicad-cli pcb drc --schematic-parity` sees no field mismatch."""
    values = {"Reference": ref, "Value": value or ""}
    if symbol is not None:
        lib_props = {
            atom_text(p[1]): atom_text(p[2]) for p in children(symbol.lib, "property")
        }
        for key in ("Datasheet", "Description"):
            values[key] = symbol.fields.get(key, lib_props.get(key, ""))
    return values


def set_fields(
    node: Node,
    values: dict,
    *,
    project: str,
    ref: str,
    rot: float,
    back: bool,
    hide_reference: bool,
) -> None:
    """Fill the footprint's properties, adding the ones the library lacks; turn the
    texts with the footprint (a flipped footprint's are turned already)."""
    seen: set[str] = set()
    for prop in children(node, "property"):
        key = atom_text(prop[1])
        seen.add(key)
        if key in values:
            prop[2] = values[key]
        if key == "Reference" and hide_reference and child(prop, "hide") is None:
            replace_child(prop, "hide", [S("hide"), S("yes")])
        if not back:
            rotate_text(prop, rot)
        replace_child(prop, "uuid", [S("uuid"), stable_uuid(project, ref, "prop", key)])
    for key in ("Datasheet", "Description"):
        if key in values and key not in seen:
            node.append(_hidden_property(key, values[key], project, ref, rot, back))
    if not back:
        for txt in children(node, "fp_text"):
            rotate_text(txt, rot)


def _hidden_property(
    key: str, value: str, project: str, ref: str, rot: float, back: bool
) -> Node:
    return [
        S("property"),
        key,
        value,
        [S("at"), 0, 0, num(rot)],
        [S("layer"), "B.Fab" if back else "F.Fab"],
        [S("hide"), S("yes")],
        [S("uuid"), stable_uuid(project, ref, "prop", key)],
        [S("effects"), [S("font"), [S("size"), 1.27, 1.27], [S("thickness"), 0.15]]],
    ]


def set_in_bom(node: Node, in_bom: bool) -> None:
    attr = child(node, "attr")
    flags = [
        a
        for a in (atoms_of(attr) if attr is not None else [])
        if atom_text(a) != "exclude_from_bom"
    ]
    if not in_bom:
        flags.append(S("exclude_from_bom"))
    replace_child(node, "attr", [S("attr"), *flags] if flags else None)


def link_symbol(node: Node, symbol: PlacedSymbol, root_sheet_file: str) -> None:
    """The footprint's link to its symbol: path, sheet name and file. On a sub-sheet the
    path runs through the sheet symbols, not the root."""
    sheet = symbol.sheet
    if sheet is not None and sheet.parent is not None:
        path = sheet.path.split("/", 2)[2] if sheet.path.count("/") > 1 else ""
        node.append([S("path"), f"/{path}/{symbol.uuid}"])
        node.append([S("sheetname"), sheet.sheet_names])
        node.append([S("sheetfile"), sheet.file])
    else:
        node.append([S("path"), f"/{symbol.uuid}"])
        node.append([S("sheetname"), "/"])
        node.append([S("sheetfile"), root_sheet_file])


def place_pad(
    pad: Node,
    fp: PlacedFootprint,
    symbol: PlacedSymbol | None,
    *,
    project: str,
    copper: list[str],
    back: bool,
) -> PlacedPad:
    """One pad: turned with the footprint, on the net of the symbol pin of its number,
    with a stable uuid; returned in board coordinates."""
    a = atoms_of(pad)
    number = atom_text(a[0]) if a else ""
    kind = atom_text(a[1]) if len(a) > 1 else "smd"
    shape = atom_text(a[2]) if len(a) > 2 else "circle"
    at_node = child(pad, "at")
    pa = atoms_of(at_node) if at_node is not None else [0, 0]
    px, py = float(pa[0]), float(pa[1])
    pang = float(pa[2]) if len(pa) > 2 else 0.0
    rot = fp.rot
    if back:  # angles already absolute in a flipped footprint
        pang -= rot
    elif rot:
        replace_child(pad, "at", [S("at"), num(px), num(py), num((pang + rot) % 360)])
    rx, ry = rotate(px, py, rot)
    net = _pad_net(pad, number, kind, symbol)
    replace_child(
        pad,
        "uuid",
        [S("uuid"), stable_uuid(project, fp.ref, "pad", number, f"{px},{py}")],
    )
    return PlacedPad(
        number,
        num(fp.x + rx),
        num(fp.y + ry),
        kind,
        shape,
        _pad_size(pad),
        _pad_copper(pad, copper),
        net,
        _pad_drill(pad),
        (pang + rot) % 360,
    )


def _pad_net(
    pad: Node, number: str, kind: str, symbol: PlacedSymbol | None
) -> str | None:
    if symbol is None or not number or kind == "np_thru_hole":
        return None
    pin = symbol.pins.get(number)
    if pin is None or pin.net is None:
        return None
    insert_after(pad, [S("net"), pin.net])
    insert_after(pad, [S("pintype"), pin.etype], after="net")
    insert_after(pad, [S("pinfunction"), pin.name], after="net")
    return pin.net


def _pad_size(pad: Node) -> tuple[float, float]:
    size_node = child(pad, "size")
    sz = atoms_of(size_node) if size_node is not None else [0, 0]
    return float(sz[0]), float(sz[1])


def _pad_copper(pad: Node, copper: list[str]) -> frozenset[str]:
    layers_node = child(pad, "layers")
    names = [atom_text(v) for v in atoms_of(layers_node)] if layers_node else []
    return frozenset(lay for lay in copper if any(v in ("*.Cu", lay) for v in names))


def _pad_drill(pad: Node) -> float:
    drill_node = child(pad, "drill")
    if drill_node is None:
        return 0.0
    nums = [float(v) for v in atoms_of(drill_node) if isinstance(v, int | float)]
    return max(nums) if nums else 0.0


def stamp_items(
    node: Node, *, project: str, ref: str, back: bool, at, rot: float
) -> None:
    """Stable uuids for the graphics, texts and zones, by their place in the footprint
    (libraries often ship `fp_text user "${REFERENCE}"` without one, and KiCad would make
    one up); a footprint's own zones move with it (placed with the flip on the back)."""
    for index, item in enumerate(node):
        kind = head(item)
        if kind in GRAPHICS:
            key = ("gfx", str(index))
        elif kind == "fp_text":
            key = ("fp_text", str(index))
        elif kind == "zone":
            key = ("zone", str(index))
            if not back:
                place_zone(item, at[0], at[1], rot)
        else:
            continue
        replace_child(item, "uuid", [S("uuid"), stable_uuid(project, ref, *key)])
