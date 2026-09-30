"""Fixtures: tiny KiCad 10 library items and a stand-in for the library cache."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from inkibox.kicad.sfile import Node, parse
from inkibox.update.libcache import LibraryError

R_SYMBOL = """(symbol "Device:R"
\t(pin_numbers
\t\t(hide yes)
\t)
\t(exclude_from_sim no)
\t(in_bom yes)
\t(on_board yes)
\t(property "Reference" "R"
\t\t(at 2.032 0 90)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t)
\t)
\t(property "Value" "R"
\t\t(at 0 0 90)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t)
\t)
\t(property "Footprint" ""
\t\t(at -1.778 0 90)
\t\t(hide yes)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t)
\t)
\t(property "Datasheet" "~"
\t\t(at 0 0 0)
\t\t(hide yes)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t)
\t)
\t(property "Description" "Resistor"
\t\t(at 0 0 0)
\t\t(hide yes)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t)
\t)
\t(property "ki_keywords" "R res resistor"
\t\t(at 0 0 0)
\t\t(hide yes)
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1.27 1.27)
\t\t\t)
\t\t)
\t)
\t(symbol "R_0_1"
\t\t(rectangle
\t\t\t(start -1.016 -2.54)
\t\t\t(end 1.016 2.54)
\t\t)
\t)
\t(symbol "R_1_1"
\t\t(pin passive line
\t\t\t(at 0 3.81 270)
\t\t\t(length 1.27)
\t\t\t(name "~")
\t\t\t(number "1")
\t\t)
\t\t(pin passive line
\t\t\t(at 0 -3.81 90)
\t\t\t(length 1.27)
\t\t\t(name "~")
\t\t\t(number "2")
\t\t)
\t)
)"""

R_FOOTPRINT = """(footprint "R_0603_1608Metric"
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(layer "F.Cu")
\t(descr "Resistor SMD 0603")
\t(tags "resistor")
\t(property "Reference" "REF**"
\t\t(at 0 -1.43 0)
\t\t(layer "F.SilkS")
\t\t(uuid "11111111-0000-0000-0000-000000000001")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.15)
\t\t\t)
\t\t)
\t)
\t(property "Value" "R_0603_1608Metric"
\t\t(at 0 1.43 0)
\t\t(layer "F.Fab")
\t\t(uuid "11111111-0000-0000-0000-000000000002")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 1 1)
\t\t\t\t(thickness 0.15)
\t\t\t)
\t\t)
\t)
\t(attr smd)
\t(fp_line
\t\t(start -0.23 -0.52)
\t\t(end 0.23 -0.52)
\t\t(stroke
\t\t\t(width 0.12)
\t\t\t(type solid)
\t\t)
\t\t(layer "F.SilkS")
\t\t(uuid "11111111-0000-0000-0000-000000000003")
\t)
\t(fp_text user "${REFERENCE}"
\t\t(at 0 0 0)
\t\t(layer "F.Fab")
\t\t(uuid "11111111-0000-0000-0000-000000000004")
\t\t(effects
\t\t\t(font
\t\t\t\t(size 0.4 0.4)
\t\t\t\t(thickness 0.06)
\t\t\t)
\t\t)
\t)
\t(pad "1" smd roundrect
\t\t(at -0.825 0)
\t\t(size 0.8 0.95)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t\t(uuid "11111111-0000-0000-0000-000000000005")
\t)
\t(pad "2" smd roundrect
\t\t(at 0.825 0)
\t\t(size 0.8 0.95)
\t\t(layers "F.Cu" "F.Mask" "F.Paste")
\t\t(roundrect_rratio 0.25)
\t\t(uuid "11111111-0000-0000-0000-000000000006")
\t)
\t(embedded_fonts no)
\t(model "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step"
\t\t(offset
\t\t\t(xyz 0 0 0)
\t\t)
\t\t(scale
\t\t\t(xyz 1 1 1)
\t\t)
\t\t(rotate
\t\t\t(xyz 0 0 0)
\t\t)
\t)
)"""


@dataclass
class FakeLibs:
    """Stand-in for :class:`~inkibox.update.libcache.LibraryCache`: items by lib_id."""

    symbols: dict[str, str] = field(default_factory=lambda: {"Device:R": R_SYMBOL})
    footprints: dict[str, str] = field(
        default_factory=lambda: {"Resistor_SMD:R_0603_1608Metric": R_FOOTPRINT}
    )

    def symbol(self, lib_id: str) -> Node:
        if lib_id not in self.symbols:
            raise LibraryError(f"unknown symbol {lib_id}")
        return parse(self.symbols[lib_id]).copy()

    def footprint(self, lib_id: str) -> Node:
        if lib_id not in self.footprints:
            raise LibraryError(f"unknown footprint {lib_id}")
        return parse(self.footprints[lib_id]).copy()


@pytest.fixture
def libs() -> FakeLibs:
    return FakeLibs()
