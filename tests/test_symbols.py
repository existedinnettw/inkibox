"""Update Symbols from Library on a schematic."""

from __future__ import annotations

from pathlib import Path

from conftest import R_SYMBOL, FakeLibs

from inkibox.kicad.sfile import SFile, format_node, parse
from inkibox.kicad.sfile import write_text as write_lf
from inkibox.update.options import SymbolOptions
from inkibox.update.symbols import fields_of, update_symbols


def schematic(tmp_path: Path, embedded: str = R_SYMBOL, pins: str = '"1" "2"') -> SFile:
    """
    Given an embedded copy and the pin numbers the placed R1 lists
    When called
    Then a one-symbol schematic with R1 (value 10k, a footprint the designer chose)
    """
    pin_nodes = "".join(
        f'\n\t\t(pin {n}\n\t\t\t(uuid "cccccccc-0000-0000-0000-00000000000{i}")\n\t\t)'
        for i, n in enumerate(pins.split())
    )
    text = f"""(kicad_sch
\t(version 20260306)
\t(generator "eeschema")
\t(uuid "dddddddd-0000-0000-0000-000000000000")
\t(paper "A4")
\t(lib_symbols
\t\t{format_node(parse(embedded), 2)}
\t\t(symbol "Device:Unused")
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 90)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "eeeeeeee-0000-0000-0000-000000000000")
\t\t(property "Reference" "R1"
\t\t\t(at 100 47 90)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 50 90)
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric"
\t\t\t(at 100 50 0)
\t\t\t(hide yes)
\t\t)
\t\t(property "Datasheet" "old.pdf"
\t\t\t(at 100 50 0)
\t\t\t(hide yes)
\t\t){pin_nodes}
\t\t(instances
\t\t\t(project "x"
\t\t\t\t(path "/dddddddd-0000-0000-0000-000000000000"
\t\t\t\t\t(reference "R1")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)
)
"""
    p = tmp_path / "x.kicad_sch"
    write_lf(p, text)
    return SFile.load(p)


def test_up_to_date_symbol_changes_nothing_but_unused_copies(tmp_path: Path):
    sch = schematic(tmp_path)
    report = update_symbols(sch, FakeLibs(), SymbolOptions())  # type: ignore[arg-type]
    assert report.changes == ["embedded Device:Unused: removed (no symbol uses it)"]
    assert "Device:Unused" not in sch.render()


def test_shape_and_pins_refresh_the_copy_but_not_the_designers_fields(tmp_path: Path):
    """
    Given an embedded copy that lost a pin, and R1 listing only pin 1
    When symbols are updated with the defaults
    Then the copy is the library's, R1 lists pin 2 (pin 1 keeps its uuid)
    And Value, Footprint and Datasheet stay as the designer set them (field text is off)
    """
    old = R_SYMBOL.replace('(number "2")', '(number "3")')
    sch = schematic(tmp_path, embedded=old, pins='"1"')
    report = update_symbols(sch, FakeLibs(), SymbolOptions())  # type: ignore[arg-type]
    assert "embedded Device:R: updated from the library" in report.changes
    assert "R1: pins added 2" in report.changes
    inst = sch.root.child("symbol")
    assert inst is not None
    assert [p.atom(0) for p in inst.children("pin")] == ["1", "2"]
    assert (
        inst.children("pin")[0].value("uuid") == "cccccccc-0000-0000-0000-000000000000"
    )
    f = {k: v.atom(1) for k, v in fields_of(inst).items()}
    assert f == {
        "Reference": "R1",
        "Value": "10k",
        "Footprint": "Resistor_SMD:R_0603_1608Metric",
        "Datasheet": "old.pdf",
    }


def test_field_text_takes_the_library_text(tmp_path: Path):
    sch = schematic(tmp_path)
    report = update_symbols(sch, FakeLibs(), SymbolOptions(field_text=True))  # type: ignore[arg-type]
    assert "R1: Datasheet 'old.pdf' -> '~'" in report.changes
    assert (
        "R1: Footprint 'Resistor_SMD:R_0603_1608Metric' -> ''" not in report.changes
    )  # empty in lib


def test_missing_library_is_an_error_and_keeps_the_copy(tmp_path: Path):
    sch = schematic(tmp_path)
    report = update_symbols(sch, FakeLibs(symbols={}), SymbolOptions())  # type: ignore[arg-type]
    assert report.errors == ["unknown symbol Device:R"]
    assert '(symbol "Device:R"' in sch.render()
