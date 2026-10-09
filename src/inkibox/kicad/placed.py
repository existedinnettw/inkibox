"""Placed schematic symbols and their pins: where a library pin lands on the sheet, and
how the units of one reference join into the component a footprint stands for."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .nets import connected
from .sexpr import Node, child

if TYPE_CHECKING:
    from .schematic import Schematic


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


# --------------------------------------------------------------------------- geometry


def lib_to_sheet(
    px: float, py: float, sx: float, sy: float, rot: int
) -> tuple[float, float]:
    """Library point (y up) → schematic point (y down) for a symbol at (sx, sy, rot)."""
    a = math.radians(rot)
    c, s = round(math.cos(a)), round(math.sin(a))
    rx = px * c - py * s
    ry = px * s + py * c
    return sx + rx, sy - ry


def pin_direction(angle: float, rot: int) -> tuple[int, int]:
    """Outward unit vector (schematic coords) of a pin whose library angle points toward
    the body."""
    out = (angle + 180 + rot) % 360
    return {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[
        int(round(out / 90) * 90) % 360
    ]


# --------------------------------------------------------------------------- units


def join_pins(units: list[PlacedSymbol]) -> dict[str, PlacedPin]:
    """The pins of every unit of one reference. Pins of unit 0 (common to all units)
    appear in every placed unit: keep the copy that is connected (an open or no-connect
    copy yields to it), and refuse two copies on different nets."""
    first = units[0]
    pins: dict[str, PlacedPin] = {}
    for u in units:
        for number, pin in u.pins.items():
            have = pins.get(number)
            if have is None or not connected(have):
                pins[number] = pin
            elif connected(pin) and pin.net != have.net:
                raise ValueError(
                    f"{first.ref} pin {number}: {have.net} on unit {have.symbol.unit}, "
                    f"{pin.net} on unit {u.unit}"
                )
    return pins


def join_units(units: list[PlacedSymbol]) -> PlacedSymbol:
    """One component from the units of a reference, sorted as KiCad's netlist orders
    them: the pins of all, the identity (uuid, sheet) of the first."""
    if len(units) == 1:
        return units[0]
    first = units[0]
    return PlacedSymbol(
        ref=first.ref,
        lib_id=first.lib_id,
        x=first.x,
        y=first.y,
        rot=first.rot,
        value=first.value,
        footprint=first.footprint,
        uuid=first.uuid,
        lib=first.lib,
        pins=join_pins(units),
        fields=first.fields,
        in_bom=first.in_bom,
        on_board=first.on_board,
        unit=first.unit,
        sheet=first.sheet,
    )
