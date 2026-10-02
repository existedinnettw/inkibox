"""inkibox route: the DSN edit, Freerouting's verdict, options, tool discovery, and an end to
end run with KiCad's pcbnew, Java and Freerouting (skipped without them)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_e2e import make_project

from inkibox.kicad import Board
from inkibox.kicad.sexpr import to_pretty
from inkibox.route import (
    RouteEnvError,
    drop_planes,
    freerouting_score,
    load_route_options,
    route_project,
)
from inkibox.route.env import (
    _wrapper_pythonpath,
    find_freerouting,
    find_java,
    find_kicad_python,
)
from inkibox.update import ProjectError

DSN = """(pcb x
  (structure
    (layer F.Cu (type signal))
    (plane GND (polygon F.Cu 0  0 0  10 0  10 10  0 10))
    (plane "/VIN" (polygon F.Cu 0  0 0  5 0  5 5))
    (plane GND (polygon B.Cu 0  0 0  10 0  10 10  0 10))
    (plane GNDPWR (polygon B.Cu 0  0 0  1 0  1 1))
  )
)
"""


def test_drop_planes_removes_only_the_named_nets_bare_or_quoted():
    text, n = drop_planes(DSN, ["GND", "/VIN"])
    assert n == 3
    assert "(plane GND " not in text and "/VIN" not in text
    assert "(plane GNDPWR (polygon B.Cu" in text  # a prefix of another net stays
    assert text.count("(") == text.count(")")


def test_freerouting_score_is_the_last_one_reported():
    log = (
        "pass #1 ... score 870.96 (7 unrouted and 25 violations)\n"
        "Auto-routing stage completed ... final score: 935.47 (1 unrouted and 25 violations)\n"
        "Optimization stage completed ... (0 unrouted and 25 violations)\n"
    )
    assert freerouting_score(log) == 0
    assert freerouting_score("no score here") is None


def test_options_come_from_pyproject_and_unknown_ones_are_errors(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.inkibox.route]\npasses = 20\nroute_zone_nets = ["GND"]\n'
        'stitch = { net = "GND", pitch = 2.0 }\n'
    )
    opts = load_route_options(tmp_path, passes=None)
    assert (opts.passes, opts.route_zone_nets, opts.threads) == (20, ["GND"], 1)
    assert opts.clearance_margin == 0.0
    assert load_route_options(tmp_path, passes=5).passes == 5
    (tmp_path / "pyproject.toml").write_text("[tool.inkibox.route]\nmargins = 0.01\n")
    with pytest.raises(ProjectError, match="margins"):
        load_route_options(tmp_path)


def test_the_pythonpath_of_a_nix_kicad_cli_wrapper_is_found(tmp_path: Path):
    w = tmp_path / "kicad-cli"
    w.write_text(
        "#! /nix/store/x-bash/bin/bash -e\n"
        "export PYTHONPATH='/nix/store/a-kicad-base/lib/python3.14/site-packages:/nix/store/b/site-packages'\n"
        'exec "/nix/store/a-kicad-base/bin/kicad-cli"  "$@"\n'
    )
    assert _wrapper_pythonpath(str(w)) == (
        "/nix/store/a-kicad-base/lib/python3.14/site-packages:/nix/store/b/site-packages"
    )
    binary = tmp_path / "bin"
    binary.write_bytes(b"\x7fELF...")
    assert _wrapper_pythonpath(str(binary)) is None


def test_a_keepout_is_a_rule_area(tmp_path: Path):
    pcb = Board("k", None)  # type: ignore[arg-type]
    pcb.outline_rect(0, 0, 10, 10)
    pcb.keepout([(1, 1), (2, 1), (2, 9), (1, 9)], name="moat")
    pcb.write(tmp_path / "k.kicad_pcb")
    text = (tmp_path / "k.kicad_pcb").read_text()
    assert '(name "moat")' in text
    for rule in (
        "(tracks not_allowed)",
        "(vias not_allowed)",
        "(copperpour not_allowed)",
        "(pads allowed)",
    ):
        assert rule in text


def _tools_or_skip():
    if shutil.which("kicad-cli") is None:
        pytest.skip("kicad-cli not installed")
    try:
        find_kicad_python()
        find_java()
        find_freerouting()
    except RouteEnvError as exc:
        pytest.skip(str(exc))


def test_route_connects_the_board_and_a_second_run_changes_nothing(tmp_path: Path):
    _tools_or_skip()
    project = make_project(tmp_path / "demo")
    (project / "pyproject.toml").write_text("[tool.inkibox.route]\npasses = 10\n")
    opts = load_route_options(project)
    pro = (project / "demo.kicad_pro").read_bytes()
    first = route_project(project, opts, echo=lambda *_: None)
    # pcbnew's save rewrites the project file; route puts it back
    assert (project / "demo.kicad_pro").read_bytes() == pro
    assert first.ok, (first.unrouted, first.unconnected, first.errors)
    board = (project / "demo.kicad_pcb").read_text()
    assert '(net "/B")' in board.split("(segment", 1)[1]  # net B got a track
    # nothing left to route; the existing copper is kept as it is
    second = route_project(project, opts, echo=lambda *_: None)
    assert second.ok
    tracks = lambda t: sorted(
        ln.strip() for ln in t.splitlines() if ln.strip().startswith(("(start", "(end"))
    )
    assert tracks((project / "demo.kicad_pcb").read_text()) == tracks(board)


def test_route_fails_when_connections_are_left_unrouted(tmp_path: Path):
    _tools_or_skip()
    project = make_project(tmp_path / "demo")
    # a keepout over the whole board: nothing can be routed, yet Freerouting exits 0
    rule_area = Board("demo", None)  # type: ignore[arg-type]
    rule_area.keepout([(100, 100), (130, 100), (130, 120), (100, 120)], name="all")
    pcb = project / "demo.kicad_pcb"
    text = pcb.read_text().rstrip()
    assert text.endswith(")")
    pcb.write_text(text[:-1] + "\t" + to_pretty(rule_area.zones[0]) + "\n)\n")
    res = route_project(
        project, load_route_options(project, passes=3), echo=lambda *_: None
    )
    assert not res.ok
    assert res.unconnected, "KiCad's DRC reports the connection left open"
