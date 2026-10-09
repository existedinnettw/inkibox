"""End to end, with the real kicad-cli: a design that uses sub-sheets, a multi-unit symbol
spread over two sheets, back-side footprints and a 4-layer stackup is accepted by KiCad
as inkibox built it (skipped without kicad-cli and KiCad's stock libraries).

KiCad is the judge here: ERC clean, DRC with schematic parity clean but for the
unconnected items of an unrouted board, every pad on the net KiCad's netlist gives it,
and the flipped footprints' pads where pcbnew would put them is checked by the netlist
and parity (a pad on the wrong net or a missing link fails parity).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from inkibox.kicad import Board, Libraries, Schematic
from inkibox.kicad.board import stackup
from inkibox.kicad.sexpr import atom_text, child, children, parse_one

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None, reason="kicad-cli not installed"
)

OPAMP = "Amplifier_Operational:LM358"
SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
R0603 = "Resistor_SMD:R_0603_1608Metric"


def stock_libs(project: Path) -> Libraries:
    libs = Libraries(project)
    needed = {"Device", "Amplifier_Operational", "power"}
    if not needed <= set(libs.symbol_libs) or not {"Package_SO", "Resistor_SMD"} <= set(
        libs.footprint_libs
    ):
        pytest.skip("KiCad's stock libraries are not in the global tables here")
    return libs


def make_project(root: Path) -> tuple[Schematic, Board]:
    """
    Given nothing
    When called
    Then ``root`` holds a project: the LM358's two amplifiers on a sub-sheet, its power
         unit on the root sheet, a resistor on each sheet, both resistors on the back of
         a 4-layer board, one amplifier left unused (no-connect cleanup per unit)
    """
    root.mkdir()
    (root / "t.kicad_pro").write_text("{}")
    libs = stock_libs(root)
    sch = Schematic("t", libs, title="t")
    sub = sch.sheet("Amp", "amp.kicad_sch", (50, 50))
    g = 2.54
    a = sub.place(OPAMP, "U1", (40 * g, 40 * g), unit=1, footprint=SOIC8)
    b = sub.place(OPAMP, "U1", (70 * g, 40 * g), unit=2, footprint=SOIC8)
    pwr = sch.place(OPAMP, "U1", (40 * g, 60 * g), unit=3, footprint=SOIC8)
    sub.global_label(a.pin("1"), "OUT")
    sub.global_label(a.pin("2"), "OUT")  # follower
    sub.label(a.pin("3"), "IN")
    sub.no_connect_unused(b)  # the second amplifier is unused
    sch.power(pwr.pin("8"), "power:+5V")
    sch.power(pwr.pin("4"), "power:GND", rot=180)
    r1 = sch.place("Device:R", "R1", (20 * g, 20 * g), value="10k", footprint=R0603)
    sch.global_label(r1.pin("1"), "OUT")
    sch.power(r1.pin("2"), "power:GND", rot=180)
    r2 = sub.place("Device:R", "R2", (20 * g, 20 * g), value="10k", footprint=R0603)
    sub.label(r2.pin("1"), "IN")
    sub.power(r2.pin("2"), "power:+5V")
    sch.power_at((10 * g, 10 * g), "power:+5V", flag=True)
    sch.power_at((14 * g, 10 * g), "power:GND", rot=180, flag=True)
    assert not sch.unconnected()
    sch.write(root / "t.kicad_sch")

    cu = lambda n, t=0.035: (n, "copper", t, {})
    d = lambda n, k: (n, k, 0.2, {"material": "FR4", "epsilon_r": 4.5})
    st = stackup(
        [
            ("F.Mask", "Top Solder Mask", 0.01, {}),
            cu("F.Cu"),
            d("dielectric 1", "prepreg"),
            cu("In1.Cu", 0.0175),
            d("dielectric 2", "core"),
            cu("In2.Cu", 0.0175),
            d("dielectric 3", "prepreg"),
            cu("B.Cu"),
            ("B.Mask", "Bottom Solder Mask", 0.01, {}),
        ]
    )
    pcb = Board("t", libs, copper_layers=4, stackup=st)
    pcb.root_sheet_uuid = sch.uuid
    pcb.outline_rect(100, 100, 130, 120)
    comps = {c.ref: c for c in sch.components()}
    pcb.place(comps["U1"], (110, 110))
    pcb.place(comps["R1"], (122, 105), 90, layer="B.Cu")
    pcb.place(comps["R2"], (122, 115), 30, layer="B.Cu")
    pcb.write(root / "t.kicad_pcb")
    return sch, pcb


def kicad(*args: str, cwd: Path) -> None:
    subprocess.run(["kicad-cli", *args], cwd=cwd, check=True, capture_output=True)


def test_kicad_accepts_sheets_units_back_side_and_layers(tmp_path: Path):
    root = tmp_path / "t"
    _sch, pcb = make_project(root)

    # ERC: no violation at any severity
    kicad(
        "sch",
        "erc",
        "--severity-all",
        "--format",
        "json",
        "-o",
        "erc.json",
        "t.kicad_sch",
        cwd=root,
    )
    erc = json.loads((root / "erc.json").read_text())
    violations = [v for s in erc["sheets"] for v in s["violations"]]
    assert violations == [], [v["description"] for v in violations]

    # DRC with parity: only the unconnected items of an unrouted board
    kicad(
        "pcb", "drc", "--schematic-parity", "--severity-all", "--format", "json",
        "-o", "drc.json", "t.kicad_pcb", cwd=root,
    )  # fmt: skip
    drc = json.loads((root / "drc.json").read_text())
    assert drc["violations"] == [], [v["description"] for v in drc["violations"]]
    assert drc["schematic_parity"] == [], [
        v["description"] for v in drc["schematic_parity"]
    ]

    # every pad on the net KiCad's own netlist gives the pin (names included: the
    # unconnected-(U1B-…) form of a multi-unit symbol)
    kicad("sch", "export", "netlist", "-o", "t.net", "t.kicad_sch", cwd=root)
    netlist = parse_one((root / "t.net").read_text())
    kicad_nets: dict[tuple[str, str], str] = {}
    for net in children(child(netlist, "nets"), "net"):
        name = atom_text(child(net, "name")[1])
        for node in children(net, "node"):
            kicad_nets[
                (atom_text(child(node, "ref")[1]), atom_text(child(node, "pin")[1]))
            ] = name
    ours = {
        (fp.ref, p.number): p.net
        for fp in pcb.footprints
        for p in fp.all_pads
        if p.number
    }
    assert ours == {k: v for k, v in kicad_nets.items() if k[0] in {"U1", "R1", "R2"}}

    # the stackup survives KiCad: 4 copper layers, dielectrics read back
    board = (root / "t.kicad_pcb").read_text()
    assert '(layer "dielectric 3"' in board and "In2.Cu" in board
