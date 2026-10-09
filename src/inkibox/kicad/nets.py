"""KiCad's net names, as *Update PCB from Schematic* and kicad-cli's netlist form them:
the escaping of label and pin names, the unit suffix of a multi-unit symbol's reference,
and the name of an unconnected pin's net."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .sexpr import atom_text, child, children

if TYPE_CHECKING:
    from .placed import PlacedPin, PlacedSymbol


def escape_net(text: str) -> str:
    """KiCad's net-name escaping, ``EscapeString(…, CTX_NETNAME)`` in
    common/string_utils.cpp: "/" separates sheets in a net name, so one in a label or
    pin name becomes ``{slash}``; line feeds and carriage returns are dropped; every
    other character (braces, backslashes, quotes, spaces, tabs …) stays as it is."""
    return text.replace("\n", "").replace("\r", "").replace("/", "{slash}")


def unit_suffix(sym: PlacedSymbol) -> str:
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


def connected(pin: PlacedPin) -> bool:
    """On a real net: not open, not marked no-connect."""
    return pin.net is not None and not pin.net.startswith("unconnected-(")


def unconnected_net(pin: PlacedPin) -> str:
    """KiCad's name for the net of an unconnected pin: ``unconnected-(<ref>-<pin>-Pad<n>)``,
    the reference with the unit suffix of a multi-unit symbol; a pin without a name
    gives ``unconnected-(<ref>-Pad<n>)``, the reference without it (as kicad-cli's
    netlist names them, checked in tests/test_e2e_hierarchy.py)."""
    sym = pin.symbol
    if not pin.name or pin.name == "~":
        return f"unconnected-({sym.ref}-Pad{pin.number})"
    return f"unconnected-({sym.ref}{unit_suffix(sym)}-{escape_net(pin.name)}-Pad{pin.number})"
