"""File-based KiCad design generation: symbol/footprint library access, a schematic
builder, a board builder and a small grid router.

These work on the s-expression files directly (KiCad 9/10 formats), so they run
headless in CI and need no running KiCad; the `kipy` scripts in
:mod:`inkibox.scripts` are the counterpart that drives a live KiCad session.
"""

from .board import Board, PlacedFootprint
from .libs import Libraries
from .router import GridRouter, RouteResult
from .schematic import PlacedPin, PlacedSymbol, Schematic

__all__ = [
    "Board",
    "GridRouter",
    "Libraries",
    "PlacedFootprint",
    "PlacedPin",
    "PlacedSymbol",
    "RouteResult",
    "Schematic",
]
