"""The s-expression nodes of a KiCad 10 schematic sheet, as KiCad writes them: header,
title block, wires, labels, placed symbols and sheet symbols. Each helper builds one
item from plain values; :class:`inkibox.kicad.schematic.Schematic` assembles them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .libs import pin_defs, pin_geometry
from .placed import PlacedSymbol, lib_to_sheet
from .sexpr import Node, S, atom_text, child, children, clone, head, num, stable_uuid

if TYPE_CHECKING:
    from .schematic import Schematic

SCH_FORMAT = 20260306
FONT = 1.27
MANDATORY_FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description")

Point = tuple[float, float]


def effects(size: float = FONT, *justify: str, hide: bool = False) -> Node:
    node: Node = [S("effects"), [S("font"), [S("size"), num(size), num(size)]]]
    if justify:
        node.append([S("justify"), *[S(j) for j in justify]])
    if hide:
        node.append([S("hide"), S("yes")])
    return node


# --------------------------------------------------------------------------- sheet frame


def header(uuid: str, paper: str) -> Node:
    return [
        S("kicad_sch"),
        [S("version"), SCH_FORMAT],
        [S("generator"), "inkibox"],
        [S("generator_version"), "0.1"],
        [S("uuid"), uuid],
        [S("paper"), paper],
    ]


def title_block(title: str, rev: str, company: str, comment: str) -> Node | None:
    if not (title or rev or company or comment):
        return None
    tb: Node = [S("title_block")]
    if title:
        tb.append([S("title"), title])
    if rev:
        tb.append([S("rev"), rev])
    if company:
        tb.append([S("company"), company])
    if comment:
        tb.append([S("comment"), 1, comment])
    return tb


def lib_symbols(symbols: dict[str, Node]) -> Node:
    return [S("lib_symbols"), *[symbols[lib_id] for lib_id in sorted(symbols)]]


# --------------------------------------------------------------------------- drawing items


def junction(sheet_uuid: str, i: int, at: Point) -> Node:
    return [
        S("junction"),
        [S("at"), num(at[0]), num(at[1])],
        [S("diameter"), 0],
        [S("color"), 0, 0, 0, 0],
        [S("uuid"), stable_uuid(sheet_uuid, "junction", str(i))],
    ]


def no_connect(sheet_uuid: str, i: int, at: Point) -> Node:
    x, y = at
    return [
        S("no_connect"),
        [S("at"), num(x), num(y)],
        [S("uuid"), stable_uuid(sheet_uuid, "nc", str(i), f"{x},{y}")],
    ]


def wire(sheet_uuid: str, i: int, a: Point, b: Point) -> Node:
    return [
        S("wire"),
        [S("pts"), [S("xy"), num(a[0]), num(a[1])], [S("xy"), num(b[0]), num(b[1])]],
        [S("stroke"), [S("width"), 0], [S("type"), S("default")]],
        [S("uuid"), stable_uuid(sheet_uuid, "wire", str(i), f"{a}-{b}")],
    ]


def text(sheet_uuid: str, i: int, body: str, x: float, y: float, size: float) -> Node:
    return [
        S("text"),
        body,
        [S("exclude_from_sim"), S("no")],
        [S("at"), num(x), num(y), 0],
        [
            S("effects"),
            [S("font"), [S("size"), num(size), num(size)]],
            [S("justify"), S("left"), S("bottom")],
        ],
        [S("uuid"), stable_uuid(sheet_uuid, "text", str(i))],
    ]


def label(
    sheet_uuid: str,
    i: int,
    name: str,
    x: float,
    y: float,
    angle: int,
    justify: tuple[str, ...],
) -> Node:
    return [
        S("label"),
        name,
        [S("at"), num(x), num(y), angle],
        effects(FONT, *justify),
        [S("uuid"), stable_uuid(sheet_uuid, "label", str(i), name, f"{x},{y}")],
    ]


def global_label(
    sheet_uuid: str,
    i: int,
    name: str,
    x: float,
    y: float,
    angle: int,
    justify: tuple[str, ...],
    shape: str,
) -> Node:
    return [
        S("global_label"),
        name,
        [S("shape"), S(shape)],
        [S("at"), num(x), num(y), angle],
        [S("fields_autoplaced"), S("yes")],
        effects(FONT, *justify),
        [S("uuid"), stable_uuid(sheet_uuid, "global", str(i), name, f"{x},{y}")],
        [
            S("property"),
            "Intersheetrefs",
            "${INTERSHEET_REFS}",
            [S("at"), num(x), num(y), 0],
            [S("hide"), S("yes")],
            [S("show_name"), S("no")],
            [S("do_not_autoplace"), S("no")],
            effects(FONT, *justify),
        ],
    ]


# --------------------------------------------------------------------------- symbols


def _field_values(
    sym: PlacedSymbol, lib_props: dict[str, Node]
) -> tuple[dict, list[str]]:
    """The text of every field and the order KiCad writes them in: the mandatory five,
    then the user fields."""

    def lib_text(key: str) -> str:
        return atom_text(lib_props[key][2]) if key in lib_props else ""

    values = {
        "Reference": sym.ref,
        "Value": sym.value,
        "Footprint": sym.footprint,
        "Datasheet": lib_text("Datasheet"),
        "Description": lib_text("Description"),
    }
    values.update(sym.fields)
    order = list(MANDATORY_FIELDS) + [
        k for k in sym.fields if k not in values or k not in MANDATORY_FIELDS
    ]
    return values, list(dict.fromkeys(order))


def _field(sym: PlacedSymbol, key: str, value: str, lp: Node | None) -> Node:
    """A placed symbol's field, at the library's spot turned with the symbol."""
    lp_at = child(lp, "at") if lp is not None else None
    if lp is not None and lp_at is not None:
        x, y = lib_to_sheet(float(lp_at[1]), float(lp_at[2]), sym.x, sym.y, sym.rot)
        lp_eff = child(lp, "effects")
        eff = clone(lp_eff) if lp_eff is not None else effects()
        hide = child(lp, "hide") is not None or child(eff, "hide") is not None
    else:
        x, y = sym.x, sym.y
        eff = effects()
        hide = key not in ("Reference", "Value")
    eff = [i for i in eff if not (isinstance(i, list) and head(i) == "hide")]
    prop: Node = [S("property"), key, value, [S("at"), num(x), num(y), 0]]
    if hide or (sym.is_power and key == "Reference"):
        prop.append([S("hide"), S("yes")])
    prop.append([S("show_name"), S("no")])
    prop.append([S("do_not_autoplace"), S("no")])
    prop.append(eff)
    return prop


def symbol(sym: PlacedSymbol, project: str, path: str) -> Node:
    """A placed symbol (one unit) with its fields, pin uuids and instance path."""
    node: Node = [
        S("symbol"),
        [S("lib_id"), sym.lib_id],
        [S("at"), num(sym.x), num(sym.y), sym.rot],
        [S("unit"), sym.unit],
        [S("body_style"), 1],
        [S("exclude_from_sim"), S("no")],
        [S("in_bom"), S("yes" if sym.in_bom else "no")],
        [S("on_board"), S("yes" if sym.on_board else "no")],
        [S("in_pos_files"), S("yes")],
        [S("dnp"), S("no")],
        [S("fields_autoplaced"), S("yes")],
        [S("uuid"), sym.uuid],
    ]
    lib_props = {atom_text(p[1]): p for p in children(sym.lib, "property")}
    values, order = _field_values(sym, lib_props)
    for key in order:
        node.append(_field(sym, key, values[key], lib_props.get(key)))
    # every pin of the symbol, whichever unit this is, as KiCad writes an instance
    numbers = dict.fromkeys(pin_geometry(p)[0] for p in pin_defs(sym.lib))
    for number in numbers:
        node.append(
            [S("pin"), number, [S("uuid"), stable_uuid(sym.uuid, "pin", number)]]
        )
    node.append(
        [
            S("instances"),
            [
                S("project"),
                project,
                [S("path"), path, [S("reference"), sym.ref], [S("unit"), sym.unit]],
            ],
        ]
    )
    return node


def sheet(
    sub: Schematic, at: Point, size: Point, project: str, path: str, page: str
) -> Node:
    """The sheet symbol of sub-sheet ``sub`` on its parent (no sheet pins)."""
    (x, y), (w, h) = at, size
    return [
        S("sheet"),
        [S("at"), num(x), num(y)],
        [S("size"), num(w), num(h)],
        [S("exclude_from_sim"), S("no")],
        [S("in_bom"), S("yes")],
        [S("on_board"), S("yes")],
        [S("dnp"), S("no")],
        [S("fields_autoplaced"), S("yes")],
        [S("stroke"), [S("width"), 0.1524], [S("type"), S("solid")]],
        [S("fill"), [S("color"), 0, 0, 0, 0]],
        [S("uuid"), sub.sheet_uuid],
        [
            S("property"),
            "Sheetname",
            sub.name,
            [S("at"), num(x), num(y - 0.7116), 0],
            [S("show_name"), S("no")],
            [S("do_not_autoplace"), S("no")],
            effects(FONT, "left", "bottom"),
        ],
        [
            S("property"),
            "Sheetfile",
            sub.file,
            [S("at"), num(x), num(y + h + 0.5846), 0],
            [S("show_name"), S("no")],
            [S("do_not_autoplace"), S("no")],
            effects(FONT, "left", "top"),
        ],
        [
            S("instances"),
            [S("project"), project, [S("path"), path, [S("page"), page]]],
        ],
    ]
