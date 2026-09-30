"""Update options: fixed defaults, [tool.inkibox.update] in pyproject.toml, --set."""

from __future__ import annotations

from pathlib import Path

import pytest

from inkibox.update.options import OptionsError, load_options


def test_defaults_are_the_safe_set(tmp_path: Path):
    opts = load_options(tmp_path)
    assert opts.symbols.shape_and_pins and not opts.symbols.field_text
    assert opts.pcb.replace_footprints and not opts.pcb.delete_unused_footprints
    assert opts.footprints.models_3d and not opts.footprints.text_positions


def test_pyproject_then_command_line(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.inkibox.update.symbols]\nfield_text = true\n"
        "[tool.inkibox.update.footprints]\nmodels_3d = false\n"
    )
    opts = load_options(tmp_path, ["footprints.models_3d=true"])
    assert opts.symbols.field_text
    assert opts.footprints.models_3d


@pytest.mark.parametrize(
    "override, message",
    [
        ("symbols.nope=true", "unknown option"),
        ("symbols.field_text=maybe", "not a boolean"),
        ("footprints.text_positions=true", "not implemented headless"),
        ("symbols", "section.name=value"),
    ],
)
def test_bad_options_are_errors(tmp_path: Path, override: str, message: str):
    with pytest.raises(OptionsError, match=message):
        load_options(tmp_path, [override])
