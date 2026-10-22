"""Build a KiCad 10 schematic from library symbols: a root sheet, optionally with
sub-sheets (:meth:`Schematic.sheet`), one file each.

The model is deliberately simple: symbols are placed, every pin is then either
labelled (a short wire stub and a local or global label), tied to a power symbol, or
marked no-connect. Connections are made by name. Net names follow KiCad's rules so the
board built from the same object matches what *Update PCB from Schematic* would
produce: a local label ``X`` is net ``/X`` on the root sheet and ``/<Sheet>/X`` on a
sub-sheet, a global label and a power symbol are their name, an unconnected pin is
``unconnected-(<ref>-<pin name>-Pad<number>)``. Labels and power symbols of one name on
a sheet connect whatever their kind, and a net with several names takes the strongest
(a global label or power symbol over a local label over a hierarchical label, the one
highest in the hierarchy among equals; :class:`inkibox.kicad.nets.NetGraph`). Each pin
takes one connection.

Nets cross sheets through global labels and power symbols, or through a hierarchical
label on the sub-sheet (:meth:`Schematic.hierarchical_label`) and the sheet pin of the
same name on its sheet symbol (:meth:`Schematic.sheet_pin`), which the parent connects
like any pin. Such a net takes the name the parent gives it (``/X`` for a local label
``X`` on the root sheet); a sheet pin the parent does not connect is refused when the
schematic is written, as are a hierarchical label without its sheet pin and a sheet pin
without its label.

A symbol with several units is placed once per unit (``place(…, unit=n)``), on any
sheet; :meth:`Schematic.components` joins the units for the board.
"""

from __future__ import annotations

from pathlib import Path

from . import sch_nodes as nodes
from .libs import Libraries, pin_defs, pin_geometry, unit_pin_defs
from .nets import GLOBAL, HIER_LABEL, LOCAL_LABEL, NetGraph, unconnected_net
from .nets import connected as _connected  # noqa: F401 - former home of these names
from .nets import escape_net as _escape_net
from .nets import unit_suffix as _unit_suffix  # noqa: F401
from .placed import (
    PlacedPin,
    PlacedSymbol,
    SheetPin,
    join_units,
    lib_to_sheet,
    pin_direction,
)
from .sch_nodes import FONT, SCH_FORMAT
from .sch_nodes import effects as _effects  # noqa: F401
from .sexpr import Node, S, atom_text, children, num, stable_uuid, write_pretty

__all__ = [
    "FONT",
    "SCH_FORMAT",
    "PlacedPin",
    "PlacedSymbol",
    "Schematic",
    "SheetPin",
    "pin_defs",
    "pin_geometry",
    "unconnected_net",
    "unit_pin_defs",
]


# characters KiCad's field validator refuses (common/validators.cpp, FIELD_VALIDATOR):
# no field takes a line break or a tab, a sheet name no "/" (it separates sheets in
# net names and hierarchy paths); neither may be empty
FIELD_EXCLUDES = "\r\n\t"
LABEL_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")
SHEET_PIN_STEP = 2.54

# a pin on the sheet, or a pin of a sub-sheet's symbol: what a label connects
Pin = PlacedPin | SheetPin
SHEET_NAME_EXCLUDES = FIELD_EXCLUDES + "/"


def check_sheet_name(name: str) -> None:
    bad = sorted({c for c in name if c in SHEET_NAME_EXCLUDES})
    if not name or bad:
        raise ValueError(
            f"sheet name {name!r}: KiCad refuses an empty sheet name and the characters "
            f"{', '.join(map(repr, bad)) or '(none here)'} in one"
        )


def check_sheet_file(file: str) -> None:
    bad = sorted({c for c in file if c in FIELD_EXCLUDES})
    if not file or bad:
        raise ValueError(
            f"sheet file {file!r}: KiCad refuses an empty file name and line breaks or "
            "tabs in one"
        )


def _check_open(pin: Pin, what: str) -> None:
    """Refuse a second connection on ``pin``: KiCad would name the net after the
    stronger of the two (a power symbol or global label over a hierarchical or local
    label), on the parent sheet too, which one name per pin cannot follow."""
    if pin.net is not None:
        owner = (
            f"sheet pin {pin.name!r} of {pin.sheet.sheet_names}"
            if isinstance(pin, SheetPin)
            else f"{pin.symbol.ref} pin {pin.number}"
        )
        raise ValueError(
            f"{what}: {owner} is connected already (net {pin.net}); a pin takes one "
            "label, power symbol or no-connect"
        )


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
        self.hier_labels: list[tuple[str, float, float, int, tuple[str, ...], str]] = []
        self.sheet_pins: dict[str, SheetPin] = {}  # the pins of this sheet's symbol
        self.symbols: list[PlacedSymbol] = []
        self.lib_symbols: dict[str, Node] = {}
        self.wires: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self.labels: list[tuple[str, float, float, int, tuple[str, ...]]] = []
        self.no_connects: list[tuple[float, float]] = []
        self.junctions: list[tuple[float, float]] = []
        self.texts: list[tuple[str, float, float, float]] = []
        self._power_count = 0
        self._flag_count = 0
        self._nets = NetGraph()  # used on the root sheet only

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
        sheet symbol at ``at`` (its top left corner) of ``size``. Its pins are added with
        :meth:`sheet_pin`. Sheet names are unique among a sheet's
        sub-sheets, files across the project (a file placed twice would be a shared,
        multi-instance sheet, which this builder does not model)."""
        check_sheet_name(name)
        check_sheet_file(file)
        if any(s.name == name for s, _a, _z in self.sheets):
            raise ValueError(
                f"sheet {self.sheet_names}: a sub-sheet named {name!r} exists"
            )
        if file in {s.file for s in self.root.all_sheets()}:
            raise ValueError(f"sheet file {file!r} is used already")
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

    def sheet_pin(
        self,
        sub: Schematic,
        name: str,
        *,
        side: str = "left",
        offset: float | None = None,
        shape: str | None = None,
    ) -> SheetPin:
        """Pin ``name`` on the ``side`` edge (left, right, top, bottom) of ``sub``'s sheet
        symbol, ``offset`` from the edge's top or left end (by default one grid step
        past the side's last pin). It stands for the hierarchical labels ``name`` on
        ``sub``: connect it here with :meth:`label`, :meth:`global_label`,
        :meth:`power` or :meth:`hierarchical_label`, and their pins join that net.
        ``shape`` defaults to the shape of ``sub``'s label."""
        found = [(at, size) for s, at, size in self.sheets if s is sub]
        if not found:
            raise ValueError(
                f"{sub.sheet_names} is not a sub-sheet of {self.sheet_names}"
            )
        if name in sub.sheet_pins:
            raise ValueError(f"sheet {sub.sheet_names}: a sheet pin {name!r} exists")
        if shape is not None and shape not in LABEL_SHAPES:
            raise ValueError(
                f"sheet pin shape {shape!r}: one of {', '.join(LABEL_SHAPES)}"
            )
        (x, y), (w, h) = found[0]
        try:
            span, (dx, dy) = {
                "left": (h, (-1, 0)),
                "right": (h, (1, 0)),
                "top": (w, (0, -1)),
                "bottom": (w, (0, 1)),
            }[side]
        except KeyError:
            raise ValueError(
                f"sheet pin side {side!r}: left, right, top or bottom"
            ) from None
        if offset is None:
            taken = [
                p.y - y if dx else p.x - x
                for p in sub.sheet_pins.values()
                if p.side == side
            ]
            offset = max(taken, default=0.0) + SHEET_PIN_STEP
        if not 0 < offset < span:
            raise ValueError(
                f"sheet pin {name!r}: offset {offset} is off the {side} edge of "
                f"{sub.sheet_names} (0 to {span}, ends excluded)"
            )
        px = {"left": x, "right": x + w}.get(side, x + offset)
        py = {"top": y, "bottom": y + h}.get(side, y + offset)
        pin = SheetPin(sub, name, shape, side, num(px), num(py), dx, dy)
        if any(p.pos == pin.pos for p in sub.sheet_pins.values()):
            raise ValueError(f"sheet pin {name!r}: another pin sits at {pin.pos}")
        sub.sheet_pins[name] = pin
        return pin

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
            out.append(join_units([s for _page, s in placed]))
        return out

    # ------------------------------------------------------------------ geometry

    # library point -> sheet point, and a pin's outward direction (kept as static
    # methods: generator scripts call sch._direction)
    _transform = staticmethod(lib_to_sheet)
    _direction = staticmethod(pin_direction)

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

    def stub(self, pin: Pin, length: float = 2.54) -> tuple[float, float]:
        """A wire from the pin outward; returns its free end."""
        end = (pin.x + pin.dx * length, pin.y + pin.dy * length)
        self.wire(pin.pos, end)
        return end

    def label(
        self, pin: Pin, name: str, *, stub: float = 2.54, flag: bool = False
    ) -> None:
        """Local label ``name`` on a stub from ``pin``; the pin joins net ``/name``, or the
        stronger name of a net the label meets (KiCad connects the labels and power
        symbols of one name on a sheet, whatever their kind). With ``flag`` a PWR_FLAG shares the label's point (a supply net
        named by a label, such as the input of a regulator behind a protection diode)."""
        self._check_pin(pin, f"label {name!r}")
        end = self.stub(pin, stub) if stub else pin.pos
        angle, justify = {
            (1, 0): (0, ("left", "bottom")),
            (-1, 0): (180, ("right", "bottom")),
            (0, -1): (90, ("left", "bottom")),
            (0, 1): (270, ("left", "bottom")),
        }[(pin.dx, pin.dy)]
        self.labels.append((name, num(end[0]), num(end[1]), angle, justify))
        key = ("label", self.sheet_names, name)
        self._connect(
            pin, [key], (LOCAL_LABEL, f"{self.sheet_names}{_escape_net(name)}")
        )
        if flag:
            self._flag(end, key)

    def global_label(
        self,
        pin: Pin,
        name: str,
        *,
        stub: float = 2.54,
        shape: str = "bidirectional",
        flag: bool = False,
    ) -> None:
        """Global label ``name`` on a stub from ``pin``; the pin joins net ``name`` on
        every sheet. ``shape`` is KiCad's: input, output, bidirectional, tri_state,
        passive."""
        self._check_pin(pin, f"global label {name!r}")
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
        key = ("label", self.sheet_names, name)
        self._connect(pin, [key, ("global", name)], (GLOBAL, _escape_net(name)))
        if flag:
            self._flag(end, key)

    def hierarchical_label(
        self,
        pin: Pin,
        name: str,
        *,
        stub: float = 2.54,
        shape: str = "bidirectional",
    ) -> None:
        """Hierarchical label ``name`` on a stub from ``pin``, on a sub-sheet: the pin
        joins the net the parent connects to the sheet pin ``name`` (see
        :meth:`sheet_pin`), ``/X`` for a local label ``X`` on the root sheet. ``shape`` is
        KiCad's: input, output, bidirectional, tri_state, passive."""
        if self.parent is None:
            raise ValueError(
                f"hierarchical label {name!r}: the root sheet has no sheet symbol to "
                "carry it (use a local or global label)"
            )
        if shape not in LABEL_SHAPES:
            raise ValueError(f"label shape {shape!r}: one of {', '.join(LABEL_SHAPES)}")
        self._check_pin(pin, f"hierarchical label {name!r}")
        end = self.stub(pin, stub) if stub else pin.pos
        angle, justify = {
            (1, 0): (0, ("left",)),
            (-1, 0): (180, ("right",)),
            (0, -1): (90, ("left",)),
            (0, 1): (270, ("right",)),
        }[(pin.dx, pin.dy)]
        self.hier_labels.append((name, num(end[0]), num(end[1]), angle, justify, shape))
        self._connect(
            pin,
            [("label", self.sheet_names, name), ("sheet pin", self.sheet_names, name)],
            (HIER_LABEL, f"{self.sheet_names}{_escape_net(name)}"),
        )

    def _connect(
        self, pin: Pin | None, keys: list[tuple], driver: tuple[int, str] | None
    ) -> None:
        """Put ``pin`` on the net of ``keys`` (see :class:`NetGraph`), with ``driver``
        (priority, name) drawn on this sheet among the net's names; a sheet pin also
        carries the net to its sub-sheet's hierarchical labels."""
        if isinstance(pin, SheetPin):
            keys = [*keys, ("sheet pin", pin.sheet.sheet_names, pin.name)]
        depth = self.sheet_names.count("/") - 1
        self.root._nets.connect(
            keys,
            [] if pin is None else [pin],
            None if driver is None else (driver[0], depth, driver[1]),
        )

    def power(
        self,
        pin: Pin,
        lib_id: str,
        *,
        stub: float = 2.54,
        rot: int = 0,
        flag: bool = False,
    ) -> PlacedSymbol:
        """A power symbol (``power:+5V`` …) with its pin on the free end of a stub from
        ``pin``, upright by default. With ``flag`` a PWR_FLAG shares the point (for nets no
        power_out pin drives)."""
        self._check_pin(pin, f"power symbol {lib_id}")
        end = self.stub(pin, stub) if stub else pin.pos
        return self.power_at(end, lib_id, rot=rot, pin_net=pin, flag=flag)

    def power_at(
        self,
        point: tuple[float, float],
        lib_id: str,
        *,
        rot: int = 0,
        pin_net: Pin | None = None,
        flag: bool = False,
    ) -> PlacedSymbol:
        """Place a power symbol whose single pin sits on ``point``."""
        if pin_net is not None:
            self._check_pin(pin_net, f"power symbol {lib_id}")
        lib = self.libs.symbol(lib_id)
        pins = pin_defs(lib)
        if len(pins) != 1:
            raise ValueError(f"{lib_id} is not a one-pin power symbol")
        _n, _name, px, py, _angle, _l = pin_geometry(pins[0])
        ox, oy = self._transform(px, py, 0, 0, rot)
        self.root._power_count += 1
        ref = f"#PWR{self.root._power_count:03d}"
        sym = self.place(lib_id, ref, (point[0] - ox, point[1] - oy), rot, in_bom=False)
        key = ("label", self.sheet_names, sym.value)
        for p in sym.pins.values():
            self._connect(p, [key, ("global", sym.value)], (GLOBAL, sym.value))
        if pin_net is not None:
            self._connect(pin_net, [key], None)
        if flag:
            self._flag(point, key)
        return sym

    def _check_pin(self, pin: Pin, what: str) -> None:
        """Refuse a pin drawn on another sheet (a symbol's pin is on the sheet the
        symbol is placed on, a sheet pin on its sub-sheet's parent), then a pin that is
        connected already."""
        if isinstance(pin, SheetPin):
            owner, where = pin.sheet.parent, f"sheet pin {pin.name!r}"
        else:
            owner, where = pin.symbol.sheet, f"{pin.symbol.ref} pin {pin.number}"
        if owner is not self:
            raise ValueError(
                f"{what}: {where} is on sheet "
                f"{owner.sheet_names if owner is not None else '?'}, not on "
                f"{self.sheet_names}"
            )
        _check_open(pin, what)

    def _flag(self, point: tuple[float, float], key: tuple) -> PlacedSymbol:
        """A PWR_FLAG with its pin on ``point``, on the net of ``key``."""
        self.root._flag_count += 1
        flag_sym = self.place(
            "power:PWR_FLAG",
            f"#FLG{self.root._flag_count:03d}",
            (point[0], point[1]),
            0,
            in_bom=False,
        )
        for p in flag_sym.pins.values():
            self._connect(p, [key], None)
        return flag_sym

    def no_connect(self, pin: Pin) -> None:
        if isinstance(pin, SheetPin):
            raise TypeError(
                f"sheet pin {pin.name!r} of {pin.sheet.sheet_names}: a sheet pin takes "
                "no no-connect marker (leave the sub-sheet's label off instead)"
            )
        self._check_pin(pin, "no-connect")
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
        return nodes.symbol(sym, self.project, self.path)

    def to_node(self) -> Node:
        root = nodes.header(self.uuid, self.paper)
        tb = nodes.title_block(self.title, self.rev, self.company, self.comment)
        if tb is not None:
            root.append(tb)
        root.append(nodes.lib_symbols(self.lib_symbols))
        root += self._drawing_nodes()
        root += [self._symbol_node(sym) for sym in self.symbols]
        root += self._sheet_nodes()
        if self.parent is None:
            root.append([S("sheet_instances"), [S("path"), "/", [S("page"), "1"]]])
        root.append([S("embedded_fonts"), S("no")])
        return root

    def _drawing_nodes(self) -> list[Node]:
        """Junctions, no-connect markers, wires, texts and labels, in KiCad's order."""
        u = self.uuid
        out = [
            nodes.junction(u, i, at) for i, at in enumerate(sorted(set(self.junctions)))
        ]
        out += [nodes.no_connect(u, i, at) for i, at in enumerate(self.no_connects)]
        out += [nodes.wire(u, i, a, b) for i, (a, b) in enumerate(self.wires)]
        out += [nodes.text(u, i, *t) for i, t in enumerate(self.texts)]
        out += [nodes.label(u, i, *lb) for i, lb in enumerate(self.labels)]
        out += [
            nodes.global_label(u, i, *gl) for i, gl in enumerate(self.global_labels)
        ]
        out += [
            nodes.hierarchical_label(u, i, *hl) for i, hl in enumerate(self.hier_labels)
        ]
        return out

    def _sheet_nodes(self) -> list[Node]:
        pages = {
            sh.sheet_uuid: str(i + 1) for i, sh in enumerate(self.root.all_sheets())
        }
        return [
            nodes.sheet(
                sub,
                at,
                size,
                self.project,
                self.path,
                pages[sub.sheet_uuid],
                [
                    nodes.sheet_pin(pin, pin.shape or sub._label_shape(pin.name))
                    for pin in sub.sheet_pins.values()
                ],
            )
            for sub, at, size in self.sheets
        ]

    def _label_shape(self, name: str) -> str:
        """The shape of this sheet's first hierarchical label ``name``."""
        return next(
            (hl[5] for hl in self.hier_labels if hl[0] == name), "bidirectional"
        )

    def hierarchy_errors(self) -> list[str]:
        """What KiCad's ERC would flag in the hierarchy of this sheet and the sheets
        below it: a hierarchical label without the sheet pin of its name, a sheet pin
        without its label, a sheet pin the parent leaves unconnected."""
        out = []
        for sh in self.all_sheets():
            labels = {hl[0] for hl in sh.hier_labels}
            pins = set(sh.sheet_pins)
            out += [
                f"{sh.sheet_names}: hierarchical label {n!r} has no sheet pin"
                for n in sorted(labels - pins)
            ]
            out += [
                f"{sh.sheet_names}: sheet pin {n!r} has no hierarchical label"
                for n in sorted(pins - labels)
            ]
            out += [
                f"{sh.sheet_names}: sheet pin {n!r} is not connected on "
                f"{sh.parent.sheet_names if sh.parent else '/'}"
                for n, p in sh.sheet_pins.items()
                if p.net is None
            ]
        return out

    def write(self, path: Path) -> None:
        """Write this sheet to ``path`` and every sub-sheet next to it under its file name.
        Refused, before anything is written, when two sheets would land on one file (a
        sub-sheet named like the output path, say) or the hierarchy is inconsistent
        (:meth:`hierarchy_errors`)."""
        errors = self.hierarchy_errors()
        if errors:
            raise ValueError("; ".join(errors))
        targets = self.output_files(path)
        seen: dict[Path, str] = {}
        for sheet_names, target in targets:
            key = target.resolve()
            if key in seen:
                raise ValueError(
                    f"sheets {seen[key]} and {sheet_names} would both be written to {target}"
                )
            seen[key] = sheet_names
        self._write_all(path)

    def output_files(self, path: Path) -> list[tuple[str, Path]]:
        """``(sheet path, file)`` of this sheet written to ``path`` and of every sheet
        below it, each sub-sheet next to its parent's file."""
        out = [(self.sheet_names, path)]
        for sub, _at, _size in self.sheets:
            out += sub.output_files(path.parent / sub.file)
        return out

    def _write_all(self, path: Path) -> None:
        write_pretty(path, self.to_node())
        for sub, _at, _size in self.sheets:
            sub._write_all(path.parent / sub.file)

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
