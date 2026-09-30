"""The footprint-link step of ``inkibox check``."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkibox.kicad.sfile import write_text as write_lf
from inkibox.update.check import step_links

ROOT = "aaaaaaaa-0000-0000-0000-000000000000"
SYM = "bbbbbbbb-0000-0000-0000-000000000000"


def project(tmp_path: Path, instances: str, fp_path: str = f"/{SYM}") -> Path:
    (tmp_path / "demo.kicad_pro").write_text("{}")
    write_lf(
        tmp_path / "demo.kicad_sch",
        f'(kicad_sch\n\t(version 20260306)\n\t(uuid "{ROOT}")\n'
        f'\t(symbol\n\t\t(lib_id "Device:R")\n\t\t(uuid "{SYM}")\n'
        f"\t\t(instances\n{instances}\t\t)\n\t)\n)\n",
    )
    write_lf(
        tmp_path / "demo.kicad_pcb",
        '(kicad_pcb\n\t(version 20260206)\n\t(footprint "Resistor_SMD:R_0603_1608Metric"\n'
        f'\t\t(path "{fp_path}")\n\t\t(property "Reference" "R1")\n\t)\n)\n',
    )
    return tmp_path


def inst(name: str, ref: str) -> str:
    return (
        f'\t\t\t(project "{name}"\n\t\t\t\t(path "/{ROOT}"\n'
        f'\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n'
    )


@pytest.mark.parametrize("name", ["demo", "", "other_board"])
def test_a_footprint_links_to_its_symbol_whatever_project_the_instance_is_filed_under(
    tmp_path: Path, name: str
):
    """
    Given R1's symbol with its instance under project ``name`` (a pasted symbol has "" or
    its source project's name) and R1's footprint on that symbol's path
    When the links are checked
    Then there is no problem, as KiCad itself links them
    """
    assert step_links(project(tmp_path, inst(name, "R1"))) == []


def test_this_projects_instance_wins_over_another_on_the_same_path(tmp_path: Path):
    """
    Given the symbol filed as R9 under another project and as R1 under this one
    When the links are checked
    Then the footprint R1 matches (this project's reference counts)
    """
    both = inst("demo", "R1") + inst("other_board", "R9")
    assert step_links(project(tmp_path, both)) == []


def test_a_footprint_on_a_path_no_symbol_has_is_reported(tmp_path: Path):
    p = project(
        tmp_path, inst("demo", "R1"), fp_path="/cccccccc-0000-0000-0000-000000000000"
    )
    assert step_links(p) == [
        "R1: linked to /cccccccc-0000-0000-0000-000000000000, which no symbol has"
    ]
