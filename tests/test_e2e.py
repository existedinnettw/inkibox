"""End to end with the real kicad-cli and KiCad's stock libraries (skipped without them).

A two-resistor design is generated with inkibox.kicad, brought up to date by
``update_project``; a second run changes nothing, DRC with schematic parity is clean, and
a value changed in the schematic reaches the board.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from inkibox.kicad import Board, Libraries, Schematic
from inkibox.update import update_project
from inkibox.update.options import UpdateOptions

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None, reason="kicad-cli not installed"
)


def stock_libs(project: Path) -> Libraries:
    libs = Libraries(project)
    if "Device" not in libs.symbol_libs or "Resistor_SMD" not in libs.footprint_libs:
        pytest.skip("KiCad's stock libraries are not in the global tables here")
    return libs


def make_project(root: Path) -> Path:
    """
    Given nothing
    When called
    Then ``root`` holds a KiCad project: R1 and R2 in series between labels A and C
    """
    root.mkdir()
    (root / "demo.kicad_pro").write_text("{}")
    libs = stock_libs(root)
    sch = Schematic("demo", libs, paper="A4")
    r1 = sch.place(
        "Device:R",
        "R1",
        (50.8, 50.8),
        value="10k",
        footprint="Resistor_SMD:R_0603_1608Metric",
    )
    r2 = sch.place(
        "Device:R",
        "R2",
        (76.2, 50.8),
        value="22k",
        footprint="Resistor_SMD:R_0603_1608Metric",
    )
    sch.label(r1.pin("1"), "A")
    sch.label(r1.pin("2"), "B")
    sch.label(r2.pin("1"), "B")
    sch.label(r2.pin("2"), "C")
    sch.write(root / "demo.kicad_sch")
    pcb = Board("demo", libs)
    pcb.outline_rect(100, 100, 130, 120)
    pcb.place(r1, (110, 110))
    pcb.place(r2, (120, 110), 90)
    pcb.write(root / "demo.kicad_pcb")
    return root


def drc_parity(project: Path) -> list[dict]:
    out = project / "drc.json"
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--schematic-parity",
            "--format",
            "json",
            "-o",
            str(out),
            str(project / "demo.kicad_pcb"),
        ],
        capture_output=True,
        check=False,
    )
    return json.loads(out.read_text())["schematic_parity"]


def test_update_is_idempotent_and_follows_the_schematic(tmp_path: Path):
    project = make_project(tmp_path / "demo")
    opts = UpdateOptions()

    first = update_project(project, opts)
    assert first.report.errors == [], first.report.errors
    second = update_project(project, opts, dry_run=True)
    assert second.changed == [] and second.report.changes == [], second.report.changes
    assert drc_parity(project) == []

    sch = project / "demo.kicad_sch"
    sch.write_text(
        sch.read_text().replace('(property "Value" "22k"', '(property "Value" "47k"')
    )
    third = update_project(project, opts)
    assert "[pcb] R2: Value '22k' -> '47k'" in third.report.changes
    assert '(property "Value" "47k"' in (project / "demo.kicad_pcb").read_text()
    assert drc_parity(project) == []
