"""Build a KiCad 10 schematic from library symbols: a root sheet, optionally with
sub-sheets (:meth:`Schematic.sheet`), one file each.

The model is deliberately simple: symbols are placed, every pin is then either
labelled (a short wire stub and a local or global label), tied to a power symbol, or
marked no-connect. Connections are made by name. Net names follow KiCad's rules so the
board built from the same object matches what *Update PCB from Schematic* would
produce: a local label ``X`` is net ``/X`` on the root sheet and ``/<Sheet>/X`` on a
sub-sheet, a global label and a power symbol are their name, an unconnected pin is
``unconnected-(<ref>-<pin name>-Pad<number>)``.

A sub-sheet has no sheet pins: nets cross sheets through global labels and power
symbols. A symbol with several units is placed once per unit (``place(…, unit=n)``),
on any sheet; :meth:`Schematic.components` joins the units for the board.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .libs import Libraries, pin_defs, pin_geometry, unit_pin_defs
from .sexpr import (
    Node,
    S,
    atom_text,
    child,
    children,
    clone,
    head,
    num,
    stable_uuid,
    write_pretty,
)

SCH_FORMAT = 20260306
FONT = 1.27


def _effects(size: float = FONT, *justify: str, hide: bool = False) -> Node:
    node: Node = [S("effects"), [S("font"), [S("size"), num(size), num(size)]]]
    if justify:
        node.append([S("justify"), *[S(j) for j in justify]])
    if hide:
        node.append([S("hide"), S("yes")])
    return node


def _unit_suffix(sym: PlacedSymbol) -> str:
    """What KiCad appends to the reference of a multi-unit symbol in net names: the
    unit's name if the library gives one (``unit_name``), else its letter (A, B, …)."""
    units = {
        atom_text(u[1]).rsplit("_", 2)[-2]
        for u in children(sym.lib, "symbol")
        if atom_text(u[1]).rsplit("_", 2)[-2].isdigit()
    } - {"0"}
    if len(units) <= 1:
        return ""
    for u in children(sym.lib, "symbol"):
        parts = atom_text(u[1]).rsplit("_", 2)
        if parts[-2] == str(sym.unit):
            name = child(u, "unit_name")
            if name is not None:
                return atom_text(name[1])
    return chr(ord("A") + sym.unit - 1)


def _escape_net(text: str) -> str:
    """KiCad's net-name escaping (EscapeString, CTX_NETNAME)."""
    return text.replace("/", "{slash}")


def unconnected_net(pin: PlacedPin) -> str:
    """KiCad's name for the net of an unconnected pin."""
    sym = pin.symbol
    return f"unconnected-({sym.ref}{_unit_suffix(sym)}-{_escape_net(pin.name)}-Pad{pin.number})"


@dataclass(slots=True)
class PlacedPin:
    symbol: PlacedSymbol
    number: str
    name: str
    etype: str
    x: float  # schematic coordinates of the connection point
    y: float
    dx: int  # outward unit direction (away from the body)
    dy: int
    net: str | None = None  # KiCad net name once connected / flagged

    @property
    def pos(self) -> tuple[float, float]:
        return self.x, self.y


@dataclass(slots=True)
class PlacedSymbol:
    ref: str
    lib_id: str
    x: float
    y: float
    rot: int
    value: str
    footprint: str
    uuid: str
    lib: Node
    pins: dict[str, PlacedPin] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    in_bom: bool = True
    on_board: bool = True
    unit: int = 1
    sheet: Schematic | None = None  # the sheet it is placed on

    def pin(self, number: str) -> PlacedPin:
        try:
            return self.pins[str(number)]
        except KeyError as exc:
            raise KeyError(f"{self.ref} has no pin {number!r}") from exc

    @property
    def is_power(self) -> bool:
        return child(self.lib, "power") is not None


class Schematic:
    def __init__(
        self,
        project: str,
        libs: Libraries,
        *,
        paper: str = "A3",
        title: str = "",
        rev: str = "",
        company: str = "",
        comment: str = "",
        name: str | None = None,
        file: str | None = None,
        parent: Schematic | None = None,
    ) -> None:
        self.project = project
        self.libs = libs
        self.paper = paper
        self.title = title
        self.rev = rev
        self.company = company
        self.comment = comment
        self.parent = parent
        self.name = name  # sheet name; None for the root sheet
        self.file = file or f"{project}.kicad_sch"
        if parent is None:
            self.uuid = stable_uuid(project, "root-sheet")
            self.sheet_uuid = self.uuid
            self.path = f"/{self.uuid}"  # instance path of the symbols placed here
            self.sheet_names = "/"
        else:
            assert name is not None
            self.uuid = stable_uuid(project, "sheet-file", self.file)
            self.sheet_uuid = stable_uuid(project, "sheet", parent.sheet_names, name)
            self.path = f"{parent.path}/{self.sheet_uuid}"
            self.sheet_names = f"{parent.sheet_names}{name}/"
        self.sheets: list[
            tuple[Schematic, tuple[float, float], tuple[float, float]]
        ] = []
        self.global_labels: list[
            tuple[str, float, float, int, tuple[str, ...], str]
        ] = []
        self.symbols: list[PlacedSymbol] = []
        self.lib_symbols: dict[str, Node] = {}
        self.wires: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self.labels: list[tuple[str, float, float, int, tuple[str, ...]]] = []
        self.no_connects: list[tuple[float, float]] = []
        self.junctions: list[tuple[float, float]] = []
        self.texts: list[tuple[str, float, float, float]] = []
        self._power_count = 0
        self._flag_count = 0

    @property
    def root(self) -> Schematic:
        return self if self.parent is None else self.parent.root

    def sheet(
        self,
        name: str,
        file: str,
        at: tuple[float, float],
        size: tuple[float, float] = (25.4, 12.7),
        *,
        paper: str | None = None,
        title: str | None = None,
    ) -> Schematic:
        """A sub-sheet ``name`` stored in ``file`` (next to this sheet's file), drawn as a
        sheet symbol at ``at``. It has no sheet pins: connect across sheets with
        :meth:`global_label` and power symbols."""
        sub = Schematic(
            self.project,
            self.libs,
            paper=paper or self.paper,
            title=title if title is not None else name,
            rev=self.rev,
            company=self.company,
            name=name,
            file=file,
            parent=self,
        )
        self.sheets.append((sub, at, size))
        return sub

    def all_sheets(self) -> list[Schematic]:
        """This sheet and every sheet below it, depth first (page order)."""
        out = [self]
        for sub, _at, _size in self.sheets:
            out += sub.all_sheets()
        return out

    def all_symbols(self) -> list[PlacedSymbol]:
        return [s for sh in self.all_sheets() for s in sh.symbols]

    def components(self) -> list[PlacedSymbol]:
        """One entry per reference, the units of a multi-unit symbol joined: the pins of
        every unit, the identity (uuid, sheet) of the unit KiCad's netlist names, which
        the footprint links to: the lowest unit on the first sheet (page order) that has
        one. Power and flag symbols are left out."""
        by_ref: dict[str, list[tuple[int, PlacedSymbol]]] = {}
        for page, sh in enumerate(self.all_sheets()):
            for s in sh.symbols:
                if not s.ref.startswith("#"):
                    by_ref.setdefault(s.ref, []).append((page, s))
        out: list[PlacedSymbol] = []
        for placed in by_ref.values():
            placed.sort(key=lambda ps: (ps[0], ps[1].unit))
            units = [s for _page, s in placed]
            if len(units) == 1:
                out.append(units[0])
                continue
            first = units[0]
            # pins of unit 0 (common to all units) appear in every placed unit: keep the
            # copy that is connected, and refuse two copies on different nets
            pins: dict[str, PlacedPin] = {}
            for u in units:
                for number, pin in u.pins.items():
                    have = pins.get(number)
                    if have is None or have.net is None:
                        pins[number] = pin
                    elif pin.net is not None and pin.net != have.net:
                        raise ValueError(
                            f"{first.ref} pin {number}: {have.net} on unit {have.symbol.unit}, "
                            f"{pin.net} on unit {u.unit}"
                        )
            out.append(
                PlacedSymbol(
                    ref=first.ref,
                    lib_id=first.lib_id,
                    x=first.x,
                    y=first.y,
                    rot=first.rot,
                    value=first.value,
                    footprint=first.footprint,
                    uuid=first.uuid,
                    lib=first.lib,
                    pins=pins,
                    fields=first.fields,
                    in_bom=first.in_bom,
                    on_board=first.on_board,
                    unit=first.unit,
                    sheet=first.sheet,
                )
            )
        return out

    # ------------------------------------------------------------------ geometry

    @staticmethod
    def _transform(
        px: float, py: float, sx: float, sy: float, rot: int
    ) -> tuple[float, float]:
        """Library point (y up) → schematic point (y down) for a symbol at (sx, sy, rot)."""
        a = math.radians(rot)
        c, s = round(math.cos(a)), round(math.sin(a))
        rx = px * c - py * s
        ry = px * s + py * c
        return sx + rx, sy - ry

    @staticmethod
    def _direction(angle: float, rot: int) -> tuple[int, int]:
        """Outward unit vector (schematic coords) of a pin whose library angle points toward
        the body."""
        out = (angle + 180 + rot) % 360
        return {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[
            int(round(out / 90) * 90) % 360
        ]

    # ------------------------------------------------------------------ placement

    def place(
        self,
        lib_id: str,
        ref: str,
        at: tuple[float, float],
        rot: int = 0,
        *,
        value: str | None = None,
        footprint: str | None = None,
        fields: dict[str, str] | None = None,
        in_bom: bool = True,
        on_board: bool = True,
        unit: int = 1,
    ) -> PlacedSymbol:
        """Place unit ``unit`` of ``lib_id`` (its own pins and those common to all
        units); the other units of the same reference are placed with their own call."""
        lib = self.lib_symbols.get(lib_id)
        if lib is None:
            lib = self.libs.symbol(lib_id)
            self.lib_symbols[lib_id] = lib
        props = {atom_text(p[1]): atom_text(p[2]) for p in children(lib, "property")}
        sym = PlacedSymbol(
            ref=ref,
            lib_id=lib_id,
            x=at[0],
            y=at[1],
            rot=rot % 360,
            value=value
            if value is not None
            else props.get("Value", lib_id.split(":")[-1]),
            footprint=footprint
            if footprint is not None
            else props.get("Footprint", ""),
            uuid=stable_uuid(self.project, "symbol", ref)
            if unit == 1
            else stable_uuid(self.project, "symbol", ref, "unit", str(unit)),
            lib=lib,
            fields=dict(fields or {}),
            in_bom=in_bom,
            on_board=on_board,
            unit=unit,
            sheet=self,
        )
        for pin in unit_pin_defs(lib, unit):
            number, name, px, py, angle, _length = pin_geometry(pin)
            x, y = self._transform(px, py, sym.x, sym.y, sym.rot)
            dx, dy = self._direction(angle, sym.rot)
            etype = atom_text(pin[1]) if len(pin) > 1 else "passive"
            sym.pins[number] = PlacedPin(
                sym, number, name, etype, num(x), num(y), dx, dy
            )
        self.symbols.append(sym)
        return sym

    # ------------------------------------------------------------------ connections

    def wire(self, a: tuple[float, float], b: tuple[float, float]) -> None:
        if a != b:
            self.wires.append(((num(a[0]), num(a[1])), (num(b[0]), num(b[1]))))

    def stub(self, pin: PlacedPin, length: float = 2.54) -> tuple[float, float]:
        """A wire from the pin outward; returns its free end."""
        end = (pin.x + pin.dx * length, pin.y + pin.dy * length)
        self.wire(pin.pos, end)
        return end

    def label(
        self, pin: PlacedPin, name: str, *, stub: float = 2.54, flag: bool = False
    ) -> None:
        """Local label ``name`` on a stub from ``pin``; the pin joins net ``/name``. With
        ``flag`` a PWR_FLAG shares the label's point (a supply net named by a label, such as
        the input of a regulator behind a protection diode)."""
        end = self.stub(pin, stub) if stub else pin.pos
        angle, justify = {
            (1, 0): (0, ("left", "bottom")),
            (-1, 0): (180, ("right", "bottom")),
            (0, -1): (90, ("left", "bottom")),
            (0, 1): (270, ("left", "bottom")),
        }[(pin.dx, pin.dy)]
        self.labels.append((name, num(end[0]), num(end[1]), angle, justify))
        pin.net = f"{self.sheet_names}{name}"
        if flag:
            self._flag(end, pin.net)

    def global_label(
        self,
        pin: PlacedPin,
        name: str,
        *,
        stub: float = 2.54,
        shape: str = "bidirectional",
        flag: bool = False,
    ) -> None:
        """Global label ``name`` on a stub from ``pin``; the pin joins net ``name`` on
        every sheet. ``shape`` is KiCad's: input, output, bidirectional, tri_state,
        passive."""
        end = self.stub(pin, stub) if stub else pin.pos
        angle, justify = {
            (1, 0): (0, ("left",)),
            (-1, 0): (180, ("right",)),
            (0, -1): (90, ("left",)),
            (0, 1): (270, ("right",)),
        }[(pin.dx, pin.dy)]
        self.global_labels.append(
            (name, num(end[0]), num(end[1]), angle, justify, shape)
        )
        pin.net = name
        if flag:
            self._flag(end, pin.net)

    def power(
        self,
        pin: PlacedPin,
        lib_id: str,
        *,
        stub: float = 2.54,
        rot: int = 0,
        flag: bool = False,
    ) -> PlacedSymbol:
        """A power symbol (``power:+5V`` …) with its pin on the free end of a stub from
        ``pin``, upright by default. With ``flag`` a PWR_FLAG shares the point (for nets no
        power_out pin drives)."""
        end = self.stub(pin, stub) if stub else pin.pos
        return self.power_at(end, lib_id, rot=rot, pin_net=pin, flag=flag)

    def power_at(
        self,
        point: tuple[float, float],
        lib_id: str,
        *,
        rot: int = 0,
        pin_net: PlacedPin | None = None,
        flag: bool = False,
    ) -> PlacedSymbol:
        """Place a power symbol whose single pin sits on ``point``."""
        lib = self.libs.symbol(lib_id)
        pins = pin_defs(lib)
        if len(pins) != 1:
            raise ValueError(f"{lib_id} is not a one-pin power symbol")
        _n, _name, px, py, _angle, _l = pin_geometry(pins[0])
        ox, oy = self._transform(px, py, 0, 0, rot)
        self.root._power_count += 1
        ref = f"#PWR{self.root._power_count:03d}"
        sym = self.place(lib_id, ref, (point[0] - ox, point[1] - oy), rot, in_bom=False)
        net = sym.value
        for p in sym.pins.values():
            p.net = net
        if pin_net is not None:
            pin_net.net = net
        if flag:
            self._flag(point, net)
        return sym

    def _flag(self, point: tuple[float, float], net: str) -> None:
        """A PWR_FLAG with its pin on ``point``, on ``net``."""
        self.root._flag_count += 1
        flag_sym = self.place(
            "power:PWR_FLAG",
            f"#FLG{self.root._flag_count:03d}",
            (point[0], point[1]),
            0,
            in_bom=False,
        )
        for p in flag_sym.pins.values():
            p.net = net

    def no_connect(self, pin: PlacedPin) -> None:
        self.no_connects.append(pin.pos)
        pin.net = unconnected_net(pin)

    def no_connect_unused(self, sym: PlacedSymbol) -> int:
        n = 0
        for pin in sym.pins.values():
            if pin.net is None:
                self.no_connect(pin)
                n += 1
        return n

    def junction(self, point: tuple[float, float]) -> None:
        self.junctions.append((num(point[0]), num(point[1])))

    def text(self, text: str, at: tuple[float, float], size: float = 2.0) -> None:
        self.texts.append((text, at[0], at[1], size))

    def connect_label(self, pins: list[PlacedPin], name: str) -> None:
        for p in pins:
            self.label(p, name)

    def net_of(self, pin: PlacedPin) -> str:
        if pin.net is None:
            raise ValueError(
                f"{pin.symbol.ref} pin {pin.number} ({pin.name}) is not connected"
            )
        return pin.net

    # ------------------------------------------------------------------ output

    def _symbol_node(self, sym: PlacedSymbol) -> Node:
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
        values = {
            "Reference": sym.ref,
            "Value": sym.value,
            "Footprint": sym.footprint,
            "Datasheet": atom_text(lib_props["Datasheet"][2])
            if "Datasheet" in lib_props
            else "",
            "Description": atom_text(lib_props["Description"][2])
            if "Description" in lib_props
            else "",
        }
        values.update(sym.fields)
        order = ["Reference", "Value", "Footprint", "Datasheet", "Description"] + [
            k
            for k in sym.fields
            if k not in values
            or k not in ("Reference", "Value", "Footprint", "Datasheet", "Description")
        ]
        seen: set[str] = set()
        for key in order:
            if key in seen:
                continue
            seen.add(key)
            lp = lib_props.get(key)
            lp_at = child(lp, "at") if lp is not None else None
            if lp is not None and lp_at is not None:
                px, py = float(lp_at[1]), float(lp_at[2])
                x, y = self._transform(px, py, sym.x, sym.y, sym.rot)
                lp_eff = child(lp, "effects")
                eff = clone(lp_eff) if lp_eff is not None else _effects()
                hide = child(lp, "hide") is not None or child(eff, "hide") is not None
            else:
                x, y = sym.x, sym.y
                eff = _effects()
                hide = key not in ("Reference", "Value")
            eff = [i for i in eff if not (isinstance(i, list) and head(i) == "hide")]
            prop: Node = [
                S("property"),
                key,
                values[key],
                [S("at"), num(x), num(y), 0],
            ]
            if hide or (sym.is_power and key == "Reference"):
                prop.append([S("hide"), S("yes")])
            prop.append([S("show_name"), S("no")])
            prop.append([S("do_not_autoplace"), S("no")])
            prop.append(eff)
            node.append(prop)
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
                    self.project,
                    [
                        S("path"),
                        self.path,
                        [S("reference"), sym.ref],
                        [S("unit"), sym.unit],
                    ],
                ],
            ]
        )
        return node

    def to_node(self) -> Node:
        root: Node = [
            S("kicad_sch"),
            [S("version"), SCH_FORMAT],
            [S("generator"), "inkibox"],
            [S("generator_version"), "0.1"],
            [S("uuid"), self.uuid],
            [S("paper"), self.paper],
        ]
        if self.title or self.rev or self.company or self.comment:
            tb: Node = [S("title_block")]
            if self.title:
                tb.append([S("title"), self.title])
            if self.rev:
                tb.append([S("rev"), self.rev])
            if self.company:
                tb.append([S("company"), self.company])
            if self.comment:
                tb.append([S("comment"), 1, self.comment])
            root.append(tb)
        lib_symbols: Node = [S("lib_symbols")]
        for lib_id in sorted(self.lib_symbols):
            lib_symbols.append(self.lib_symbols[lib_id])
        root.append(lib_symbols)
        for i, (x, y) in enumerate(sorted(set(self.junctions))):
            root.append(
                [
                    S("junction"),
                    [S("at"), num(x), num(y)],
                    [S("diameter"), 0],
                    [S("color"), 0, 0, 0, 0],
                    [S("uuid"), stable_uuid(self.uuid, "junction", str(i))],
                ]
            )
        for i, (x, y) in enumerate(self.no_connects):
            root.append(
                [
                    S("no_connect"),
                    [S("at"), num(x), num(y)],
                    [S("uuid"), stable_uuid(self.uuid, "nc", str(i), f"{x},{y}")],
                ]
            )
        for i, (a, b) in enumerate(self.wires):
            root.append(
                [
                    S("wire"),
                    [
                        S("pts"),
                        [S("xy"), num(a[0]), num(a[1])],
                        [S("xy"), num(b[0]), num(b[1])],
                    ],
                    [S("stroke"), [S("width"), 0], [S("type"), S("default")]],
                    [S("uuid"), stable_uuid(self.uuid, "wire", str(i), f"{a}-{b}")],
                ]
            )
        for i, (text, x, y, size) in enumerate(self.texts):
            root.append(
                [
                    S("text"),
                    text,
                    [S("exclude_from_sim"), S("no")],
                    [S("at"), num(x), num(y), 0],
                    [
                        S("effects"),
                        [S("font"), [S("size"), num(size), num(size)]],
                        [S("justify"), S("left"), S("bottom")],
                    ],
                    [S("uuid"), stable_uuid(self.uuid, "text", str(i))],
                ]
            )
        for i, (name, x, y, angle, justify) in enumerate(self.labels):
            root.append(
                [
                    S("label"),
                    name,
                    [S("at"), num(x), num(y), angle],
                    _effects(FONT, *justify),
                    [
                        S("uuid"),
                        stable_uuid(self.uuid, "label", str(i), name, f"{x},{y}"),
                    ],
                ]
            )
        for i, (name, x, y, angle, justify, shape) in enumerate(self.global_labels):
            root.append(
                [
                    S("global_label"),
                    name,
                    [S("shape"), S(shape)],
                    [S("at"), num(x), num(y), angle],
                    [S("fields_autoplaced"), S("yes")],
                    _effects(FONT, *justify),
                    [
                        S("uuid"),
                        stable_uuid(self.uuid, "global", str(i), name, f"{x},{y}"),
                    ],
                    [
                        S("property"),
                        "Intersheetrefs",
                        "${INTERSHEET_REFS}",
                        [S("at"), num(x), num(y), 0],
                        [S("hide"), S("yes")],
                        [S("show_name"), S("no")],
                        [S("do_not_autoplace"), S("no")],
                        _effects(FONT, *justify),
                    ],
                ]
            )
        for sym in self.symbols:
            root.append(self._symbol_node(sym))
        pages = {
            sh.sheet_uuid: str(i + 1) for i, sh in enumerate(self.root.all_sheets())
        }
        for sub, (x, y), (w, h) in self.sheets:
            root.append(
                [
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
                        _effects(FONT, "left", "bottom"),
                    ],
                    [
                        S("property"),
                        "Sheetfile",
                        sub.file,
                        [S("at"), num(x), num(y + h + 0.5846), 0],
                        [S("show_name"), S("no")],
                        [S("do_not_autoplace"), S("no")],
                        _effects(FONT, "left", "top"),
                    ],
                    [
                        S("instances"),
                        [
                            S("project"),
                            self.project,
                            [S("path"), self.path, [S("page"), pages[sub.sheet_uuid]]],
                        ],
                    ],
                ]
            )
        if self.parent is None:
            root.append([S("sheet_instances"), [S("path"), "/", [S("page"), "1"]]])
        root.append([S("embedded_fonts"), S("no")])
        return root

    def write(self, path: Path) -> None:
        """Write this sheet to ``path`` and every sub-sheet next to it under its file name."""
        write_pretty(path, self.to_node())
        for sub, _at, _size in self.sheets:
            sub.write(path.parent / sub.file)

    # ------------------------------------------------------------------ netlist view

    def nets(self) -> dict[str, list[PlacedPin]]:
        """Every net of this sheet and the sheets below it."""
        out: dict[str, list[PlacedPin]] = {}
        for sym in self.all_symbols():
            for pin in sym.pins.values():
                if pin.net is not None:
                    out.setdefault(pin.net, []).append(pin)
        return out

    def unconnected(self) -> list[PlacedPin]:
        return [p for s in self.all_symbols() for p in s.pins.values() if p.net is None]
