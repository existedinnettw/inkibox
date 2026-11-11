"""inkibox place: options, reading a board's footprints, writing positions back, and the
solver on small problems (skipped without OR-Tools or uv to fetch it)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from inkibox.kicad.sfile import Node, SFile
from inkibox.place import (
    Pad,
    Part,
    PlaceOptions,
    _rbox,
    apply_placement,
    build_problem,
    exact_positions,
    load_place_options,
    place_project,
    read_parts,
    run_solver,
)
from inkibox.update import ProjectError

BOARD = """(kicad_pcb
\t(version 20250114)
\t(generator "test")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t)
\t(footprint "lib:two"
\t\t(layer "F.Cu")
\t\t(uuid "00000000-0000-0000-0000-000000000001")
\t\t(at 10 10)
\t\t(property "Reference" "U1"
\t\t\t(at 0 -2 0)
\t\t\t(layer "F.SilkS")
\t\t)
\t\t(fp_rect
\t\t\t(start -3 -1)
\t\t\t(end 3 1)
\t\t\t(layer "F.CrtYd")
\t\t)
\t\t(pad "1" thru_hole rect
\t\t\t(at -1.27 0)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1)
\t\t\t(layers "*.Cu")
\t\t\t(net "A")
\t\t)
\t\t(pad "2" thru_hole circle
\t\t\t(at 1.27 0)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1)
\t\t\t(layers "*.Cu")
\t\t\t(net "B")
\t\t)
\t)
\t(footprint "lib:two"
\t\t(layer "F.Cu")
\t\t(uuid "00000000-0000-0000-0000-000000000002")
\t\t(at 20 20 90)
\t\t(property "Reference" "U2"
\t\t\t(at 0 -2 90)
\t\t\t(layer "F.SilkS")
\t\t)
\t\t(fp_rect
\t\t\t(start -3 -1)
\t\t\t(end 3 1)
\t\t\t(layer "F.CrtYd")
\t\t)
\t\t(pad "1" thru_hole rect
\t\t\t(at -1.27 0 90)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1)
\t\t\t(layers "*.Cu")
\t\t\t(net "A")
\t\t)
\t\t(pad "2" thru_hole circle
\t\t\t(at 1.27 0 90)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1)
\t\t\t(layers "*.Cu")
\t\t\t(net "B")
\t\t)
\t)
\t(gr_rect
\t\t(start 0 0)
\t\t(end 40 40)
\t\t(stroke
\t\t\t(width 0.05)
\t\t\t(type default)
\t\t)
\t\t(fill no)
\t\t(layer "Edge.Cuts")
\t)
)
"""

solver = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None and shutil.which("uv") is None,
    reason="needs OR-Tools, or uv to run it",
)


def _board(tmp_path: Path) -> Path:
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD)
    return path


def test_options_come_from_pyproject_and_unknown_ones_are_errors(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.inkibox.place]\ngap = 0.5\nfixed = ['U1']\n"
    )
    opts = load_place_options(tmp_path, time_limit=5)
    assert (opts.gap, opts.fixed, opts.time_limit) == (0.5, ["U1"], 5)
    (tmp_path / "pyproject.toml").write_text("[tool.inkibox.place]\nspeed = 1\n")
    with pytest.raises(ProjectError, match="speed"):
        load_place_options(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.inkibox.place]\nedges = { U1 = { up = 'top' } }\n"
    )
    with pytest.raises(ProjectError, match="up"):
        load_place_options(tmp_path)


def test_read_parts_takes_courtyards_pads_nets_and_the_outline(tmp_path: Path):
    parts, outline = read_parts(SFile.load(_board(tmp_path)))
    assert outline == (0, 0, 40, 40)
    u1, u2 = parts
    assert (u1.ref, u1.x, u1.y, u1.rot, u1.box) == ("U1", 10, 10, 0, (-3, -1, 3, 1))
    assert u2.rot == 90 and u2.box == (-3, -1, 3, 1)  # the footprint's own frame
    assert [(p.name, p.x, p.net) for p in u2.pads] == [
        ("1", -1.27, "A"),
        ("2", 1.27, "B"),
    ]
    # pad copper in the footprint's frame, though the board stores the angle absolute
    assert u2.pads[0].box == pytest.approx((-2.12, -0.85, -0.42, 0.85))


def test_apply_moves_turns_pads_and_texts_and_resizes_the_outline(tmp_path: Path):
    sf = SFile.load(_board(tmp_path))
    parts, _ = read_parts(sf)
    result = {
        "parts": [[500, 600, 270], [2000, 2000, 90]],
        "width": 3000,
        "height": 2500,
    }
    moved = apply_placement(sf, parts, (0, 0), result, resize=True)
    assert moved == ["U1"]  # U2 is where it was
    sf.save()
    text = (tmp_path / "b.kicad_pcb").read_text()
    assert "(at 5 6 270)" in text
    assert "(at -1.27 0 270)" in text  # pad angles are absolute
    assert "(at 0 -2 270)" in text  # so are text angles
    assert "(end 30 25)" in text
    assert "(at 20 20 90)" in text  # untouched


def test_edges_limit_the_rotations(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    opts = PlaceOptions(edges={"U1": {"top": "bottom"}})
    problem = build_problem(parts, (0, 0), (None, None), opts)
    assert [o["rot"] for o in problem["parts"][0]["options"]] == [180]
    assert problem["parts"][0]["touch"] == ["bottom"]
    with pytest.raises(ProjectError, match="U9"):
        build_problem(parts, (0, 0), (None, None), PlaceOptions(fixed=["U9"]))


def _square(ref: str, w: float, h: float) -> Part:
    return Part(
        ref,
        Node("footprint"),
        0.0,
        0.0,
        0.0,
        False,
        (-w / 2, -h / 2, w / 2, h / 2),
        [Pad("1", 0.0, 0.0, (-0.5, -0.5, 0.5, 0.5), None)],
    )


@solver
def test_solver_packs_rectangles_into_the_smallest_board():
    parts = [_square("A", 10, 10), _square("B", 10, 10), _square("C", 20, 10)]
    opts = PlaceOptions(
        gap=0,
        edge=0,
        pad_edge=0,
        resolution=0.01,
        wire_weight=0,
        time_limit=20,
        workers=1,
    )
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] == "OPTIMAL"
    assert res["width"] * res["height"] == 2000 * 2000  # 20 x 20 mm, in 0.01 mm units


@solver
def test_solver_keeps_grid_parts_on_the_grid_and_fixed_parts_put(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    opts = PlaceOptions(
        grid=2.54,
        grid_origin="U1:1",
        on_grid={"U2": "1"},
        fixed=["U1"],
        time_limit=20,
        workers=1,
    )
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] in ("OPTIMAL", "FEASIBLE")
    assert res["parts"][0] == [1000, 1000, 0]
    x, y, rot = res["parts"][1]
    from inkibox.kicad.geometry import rotate

    px, py = rotate(-1.27, 0, rot)
    ox, oy = 10 - 1.27, 10.0  # U1's pad 1
    for v, o in ((x / 100 + px, ox), (y / 100 + py, oy)):
        k = (v - o) / 2.54
        assert abs(k - round(k)) < 1e-6


@solver
def test_place_project_writes_a_smaller_board(tmp_path: Path):
    _board(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.inkibox.place]\ntime_limit = 20\nworkers = 1\n"
    )
    res = place_project(tmp_path, load_place_options(tmp_path))
    assert res.ok and res.width is not None and res.height is not None
    assert res.width * res.height < 40 * 40
    _, outline = read_parts(SFile.load(tmp_path / "b.kicad_pcb"))
    assert outline is not None and outline[2] - outline[0] == pytest.approx(res.width)


CUSTOM = """\t\t(pad "3" smd custom
\t\t\t(at 0 0 90)
\t\t\t(size 0.5 0.5)
\t\t\t(layers "F.Cu")
\t\t\t(primitives
\t\t\t\t(gr_rect
\t\t\t\t\t(start -0.25 -0.25)
\t\t\t\t\t(end 4 0.25)
\t\t\t\t)
\t\t\t)
\t\t)
"""


def test_custom_pads_count_by_their_primitives(tmp_path: Path):
    text = BOARD.replace(
        '\t\t(pad "2" thru_hole circle\n\t\t\t(at 1.27 0 90)',
        CUSTOM + '\t\t(pad "2" thru_hole circle\n\t\t\t(at 1.27 0 90)',
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    parts, _ = read_parts(SFile.load(path))
    pad = parts[1].pad("3")  # U2 is turned 90: the pad's own angle is 0
    assert pad.box == pytest.approx((-0.25, -0.25, 4, 0.25))


@solver
def test_fixed_parts_need_not_sit_on_the_resolution_or_the_grid(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    parts[0].x = 10.16  # not a multiple of 0.05 mm
    opts = PlaceOptions(
        fixed=["U1"],
        grid=2.54,
        grid_origin="U2:1",
        on_grid={"U1": "1"},
        time_limit=20,
        workers=1,
    )
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] in ("OPTIMAL", "FEASIBLE")
    assert res["parts"][0] == [1016, 1000, 0]


@solver
def test_a_fixed_part_far_from_the_origin_is_inside_the_search():
    parts = [_square("A", 2, 2)]
    parts[0].x = parts[0].y = 50.0
    opts = PlaceOptions(fixed=["A"], time_limit=20, workers=1)
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] == "OPTIMAL"
    assert res["parts"][0] == [5000, 5000, 0]


@solver
def test_apart_gaps_widen_the_search_bound():
    parts = [_square("A", 2, 2), _square("B", 2, 2)]
    opts = PlaceOptions(
        apart=[{"a": ["A"], "b": ["B"], "gap": 20.0}], time_limit=20, workers=1
    )
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] == "OPTIMAL"
    assert max(res["width"], res["height"]) >= 2400  # 2 + 20 + 2 mm, and the edges


def test_fixed_parts_keep_fractional_angles_and_are_never_written(tmp_path: Path):
    sf = SFile.load(_board(tmp_path))
    parts, _ = read_parts(sf)
    parts[0].rot = 22.5
    parts[0].x = 10.004
    problem = build_problem(parts, (0, 0), (None, None), PlaceOptions(fixed=["U1"]))
    assert [o["rot"] for o in problem["parts"][0]["options"]] == [22.5]
    assert problem["parts"][0]["hint"][2] == 22.5
    result = {
        "parts": [[1000, 1000, 22.5], [2000, 2000, 90]],
        "width": 4000,
        "height": 4000,
    }
    moved = apply_placement(
        sf, parts, (0, 0), result, resize=False, fixed=frozenset({0})
    )
    assert moved == []
    assert (parts[0].x, parts[0].rot) == (10.004, 22.5)


def test_more_than_one_rectangle_on_edge_cuts_is_no_outline(tmp_path: Path):
    cutout = (
        "\t(gr_circle\n\t\t(center 20 20)\n\t\t(end 21 20)\n"
        '\t\t(layer "Edge.Cuts")\n\t)\n)\n'
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD.rstrip()[:-1] + cutout)
    _, outline = read_parts(SFile.load(path))
    assert outline is None
    with pytest.raises(ProjectError, match="nothing else on Edge.Cuts"):
        place_project(tmp_path, PlaceOptions())


@solver
def test_a_coarse_grid_is_inside_the_search():
    parts = [_square("A", 2, 2)]
    opts = PlaceOptions(
        grid=20.0, grid_origin="A:1", on_grid={"A": "1"}, time_limit=20, workers=1
    )
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] == "OPTIMAL"
    assert res["parts"][0][:2] == [2000, 2000]  # the first grid point with room


def test_a_footprints_own_edge_cuts_count_as_its_area_and_its_copper(tmp_path: Path):
    slot = (
        "\t\t(fp_circle\n\t\t\t(center 0 0)\n\t\t\t(end 5 0)\n"
        '\t\t\t(layer "Edge.Cuts")\n\t\t)\n'
    )
    text = BOARD.replace(
        '\t\t(pad "1" thru_hole rect', slot + '\t\t(pad "1" thru_hole rect', 1
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    parts, outline = read_parts(SFile.load(path))
    assert outline == (0, 0, 40, 40)  # a footprint's slot is not the board outline
    assert parts[0].cut == pytest.approx((-5, -5, 5, 5))
    assert parts[0].box == pytest.approx((-5, -5, 5, 5))
    problem = build_problem(parts, (0, 0), (None, None), PlaceOptions())
    assert problem["parts"][0]["options"][0]["pads"] == [-500, -500, 500, 500]


def test_boxes_round_outward_so_no_clearance_shrinks():
    assert _rbox(100, (-5.125, -1.005, 5.125, 1.005), 0) == [-513, -101, 513, 101]
    assert _rbox(100, (-0.1, 0, 0.3, 0.7), 0) == [-10, 0, 30, 70]  # exact values stay


@solver
def test_wire_length_is_measured_even_without_a_weight(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    opts = PlaceOptions(fixed=["U1", "U2"], wire_weight=0, time_limit=20, workers=1)
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    # nets A and B join U1's pads (10 -+ 1.27, 10) and U2's turned ones (20, 20 +- 1.27)
    a = abs(20 - (10 - 1.27)) + abs((20 + 1.27) - 10)
    b = abs(20 - (10 + 1.27)) + abs((20 - 1.27) - 10)
    assert res["wire"] == round((a + b) * 100)


@solver
def test_footprints_sharing_a_reference_keep_their_own_places(tmp_path: Path):
    path = tmp_path / "b.kicad_pcb"
    path.write_text(BOARD.replace('"U2"', '"U1"'))  # REF** holes, say
    (tmp_path / "pyproject.toml").write_text(
        "[tool.inkibox.place]\ntime_limit = 20\nworkers = 1\n"
    )
    res = place_project(tmp_path, load_place_options(tmp_path))
    assert res.ok
    a, b = ((p.x, p.y, p.rot) for p in read_parts(SFile.load(path))[0])
    assert a != b
    with pytest.raises(ProjectError, match="more than one"):
        build_problem(
            read_parts(SFile.load(path))[0],
            (0, 0),
            (None, None),
            PlaceOptions(fixed=["U1"]),
        )


def test_a_fixed_back_part_does_not_pin_a_front_part_of_the_same_reference(
    tmp_path: Path,
):
    # two U1s, the first on the back: build_problem fixes it, not the front one
    text = BOARD.replace('"U2"', '"U1"').replace(
        '(footprint "lib:two"\n\t\t(layer "F.Cu")',
        '(footprint "lib:two"\n\t\t(layer "B.Cu")',
        1,
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(text)
    sf = SFile.load(path)
    parts, _ = read_parts(sf)
    problem = build_problem(parts, (0, 0), (None, None), PlaceOptions())
    assert [p["fixed"] for p in problem["parts"]] == [True, False]
    result = {
        "parts": [[1000, 1000, 0], [3000, 3000, 0]],
        "width": 4000,
        "height": 4000,
    }
    fixed = frozenset(i for i, p in enumerate(problem["parts"]) if p["fixed"])
    moved = apply_placement(sf, parts, (0, 0), result, resize=False, fixed=fixed)
    assert moved == ["U1"] and (parts[1].x, parts[1].y) == (30, 30)
    assert (parts[0].x, parts[0].y) == (10, 10)


def _module(ref: str) -> Part:
    """A module whose grid pad sits at a 0.005 mm offset (dvsp_is2062a_b's J2_1 is 10.565
    mm off its origin), which no whole number of 0.01 mm units reaches."""
    return Part(
        ref,
        Node("footprint"),
        0.0,
        0.0,
        0.0,
        False,
        (-20.0, -12.785, 20.0, 12.785),
        [Pad("1", -16.38, 10.565, (-17.23, 9.715, -15.53, 11.415), None)],
    )


@solver
def test_grid_parts_come_back_exactly_on_the_grid():
    parts = [_square("G", 2, 2), _module("M")]
    parts[0].x = parts[0].y = 2.02  # its pad 1 (at its origin) sets the grid
    opts = PlaceOptions(
        grid=2.54,
        grid_origin="G:1",
        on_grid={"M": "1"},
        fixed=["G"],
        time_limit=20,
        workers=1,
    )
    problem = build_problem(parts, (0, 0), (None, None), opts)
    result = run_solver(problem)
    assert result["status"] in ("OPTIMAL", "FEASIBLE")
    from inkibox.kicad.geometry import rotate

    x, y, rot = exact_positions(problem, result)[1]
    px, py = rotate(-16.38, 10.565, rot)
    for v in (x + px, y + py):
        k = (v - 2.02) / 2.54
        assert abs(k - round(k)) < 1e-9  # exactly; the solver's own value is 0.005 off


def test_the_unit_is_one_nth_of_a_mm(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    problem = build_problem(parts, (0, 0), (None, None), PlaceOptions(unit=0.001))
    assert problem["scale"] == 1000
    assert problem["parts"][0]["options"][0]["box"] == [-3000, -1000, 3000, 1000]
    assert (problem["wire_coef"], problem["area_coef"]) == (500, 1)  # 0.5 mm2 per mm
    with pytest.raises(ProjectError, match="unit"):
        build_problem(parts, (0, 0), (None, None), PlaceOptions(unit=0.003))


@solver
def test_a_finer_unit_solves_to_the_same_board(tmp_path: Path):
    parts = [_square("A", 10, 10), _square("B", 10, 10), _square("C", 20, 10)]
    opts = PlaceOptions(
        gap=0,
        edge=0,
        pad_edge=0,
        resolution=0.01,
        wire_weight=0,
        unit=0.001,
        time_limit=20,
        workers=1,
    )
    res = run_solver(build_problem(parts, (0, 0), (None, None), opts))
    assert res["status"] == "OPTIMAL" and res["scale"] == 1000
    assert res["width"] * res["height"] == 20000 * 20000


@solver
def test_the_exact_shift_never_takes_a_part_past_the_edge_or_into_another():
    # a pad 0.005 mm off its origin on a 1 mm grid, courtyards on whole units, no margins
    g = _square("G", 2, 2)
    g.x = g.y = 1.0
    m = Part(
        "M",
        Node("footprint"),
        5.0,
        1.0,
        0.0,
        False,
        (-1.0, -1.0, 1.0, 1.0),
        [Pad("1", 0.005, 0.0, (-0.495, -0.5, 0.505, 0.5), None)],
    )
    opts = PlaceOptions(
        gap=0,
        edge=0,
        pad_edge=0,
        resolution=0.01,
        grid=1.0,
        grid_origin="G:1",
        on_grid={"M": "1"},
        fixed=["G"],
        wire_weight=0,
        time_limit=20,
        workers=1,
    )
    problem = build_problem([g, m], (0, 0), (None, None), opts)
    result = run_solver(problem)
    assert result["status"] == "OPTIMAL"
    w, h = result["width"] / 100, result["height"] / 100
    (gx, gy, _), (x, y, rot) = exact_positions(problem, result)
    assert rot % 180 == 0
    box = (x - 1, y - 1, x + 1, y + 1)
    assert (
        box[0] >= -1e-9
        and box[1] >= -1e-9
        and box[2] <= w + 1e-9
        and box[3] <= h + 1e-9
    )
    gbox = (gx - 1, gy - 1, gx + 1, gy + 1)
    apart = (
        box[0] >= gbox[2] - 1e-9
        or gbox[0] >= box[2] - 1e-9
        or (box[1] >= gbox[3] - 1e-9 or gbox[1] >= box[3] - 1e-9)
    )
    assert apart


def test_the_grid_must_be_a_whole_number_of_units(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    for unit, grid in ((1.0, 0.5), (0.1, 2.54)):
        opts = PlaceOptions(unit=unit, grid=grid, grid_origin="U1:1")
        with pytest.raises(ProjectError, match="finer unit"):
            build_problem(parts, (0, 0), (None, None), opts)


@solver
def test_the_correction_keeps_an_edge_part_on_its_edge():
    # the pad offset rounds in y only: the part must still reach the left edge
    g = _square("G", 2, 2)
    g.x = g.y = 3.0  # its pad 1 at (3, 3): a 1 mm grid through (0, 0)
    m = Part(
        "M",
        Node("footprint"),
        10.0,
        10.0,
        0.0,
        False,
        (-1.0, -1.0, 1.0, 1.0),
        [Pad("1", 0.0, 0.005, (-0.5, -0.495, 0.5, 0.505), None)],
    )
    opts = PlaceOptions(
        gap=0,
        edge=0,
        pad_edge=0,
        resolution=0.01,
        grid=1.0,
        grid_origin="G:1",
        on_grid={"M": "1"},
        fixed=["G"],
        rotations={"M": [0]},
        edges={"M": {"left": "left"}},
        wire_weight=0,
        time_limit=20,
        workers=1,
    )
    problem = build_problem([g, m], (0, 0), (None, None), opts)
    result = run_solver(problem)
    assert result["status"] == "OPTIMAL"
    x, y, _ = exact_positions(problem, result)[1]
    assert x == pytest.approx(1.0)  # its courtyard on the left edge
    k = y + 0.005 - 3.0  # its pad on the grid, exactly
    assert abs(k - round(k)) < 1e-9


def test_clearances_round_up_and_allowances_down_at_a_coarse_unit(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    opts = PlaceOptions(
        unit=1.0,
        edge=0.5,
        pad_edge=0.5,
        overhang={"U1": 0.7},
        apart=[{"a": ["U1"], "b": ["U2"], "gap": 0.5}],
    )
    problem = build_problem(parts, (0, 0), (None, None), opts)
    assert problem["board"]["edge"] == problem["board"]["pad_edge"] == 1
    assert problem["apart"][0]["gap"] == 1
    assert problem["parts"][0]["overhang"] == 0
    with pytest.raises(ProjectError, match="width"):
        build_problem(parts, (0, 0), (None, None), PlaceOptions(unit=1.0, width=72.5))


@solver
def test_a_one_unit_grid_keeps_the_grid_line_the_solver_chose():
    # unit = grid = 0.01 mm, the grid origin and the pad offset both off the unit
    g = _square("G", 2, 2)
    g.x, g.y = 3.006, 3.0
    m = Part(
        "M",
        Node("footprint"),
        10.0,
        10.0,
        0.0,
        False,
        (-1.0, -1.0, 1.0, 1.0),
        [Pad("1", 0.004, 0.0, (-0.496, -0.5, 0.504, 0.5), None)],
    )
    opts = PlaceOptions(
        gap=0,
        edge=0,
        pad_edge=0,
        resolution=0.01,
        unit=0.01,
        grid=0.01,
        grid_origin="G:1",
        on_grid={"M": "1"},
        fixed=["G"],
        rotations={"M": [0]},
        wire_weight=0,
        time_limit=20,
        workers=1,
    )
    problem = build_problem([g, m], (0, 0), (None, None), opts)
    result = run_solver(problem)
    assert result["status"] == "OPTIMAL"
    w, h = result["width"] / 100, result["height"] / 100
    x, y, _ = exact_positions(problem, result)[1]
    assert x - 1 >= -1e-9 and y - 1 >= -1e-9 and x + 1 <= w + 1e-9 and y + 1 <= h + 1e-9
    k = (x + 0.004 - 3.006) / 0.01  # its pad on the exact grid
    assert abs(k - round(k)) < 1e-6
    gx0, gy0 = 3.006 - 1, 3.0 - 1  # and clear of G (rounding the grid line anew put it
    assert x + 1 <= gx0 + 1e-9 or y + 1 <= gy0 + 1e-9 or x - 1 >= 4.006 - 1e-9  # in G)


@solver
def test_the_bound_counts_the_geometry_the_coarse_unit_rounds_up():
    parts = []
    for k in range(10):
        p = _square(f"P{k}", 0.4, 0.4)
        p.pads = [Pad("1", 0.0, 0.0, (-0.1, -0.1, 0.1, 0.1), None)]
        parts.append(p)
    opts = PlaceOptions(
        unit=1.0, width=2.0, edge=0, pad_edge=0, wire_weight=0, time_limit=20, workers=1
    )
    problem = build_problem(parts, (0, 0), (None, None), opts)
    assert problem["parts"][0]["options"][0]["keep"] == [-1, -1, 1, 1]
    res = run_solver(problem)
    assert (
        res["status"] == "OPTIMAL" and res["height"] == 20
    )  # one column of 2 mm cells


def test_a_coarse_unit_keeps_the_wire_weight(tmp_path: Path):
    parts, _ = read_parts(SFile.load(_board(tmp_path)))
    problem = build_problem(parts, (0, 0), (None, None), PlaceOptions(unit=1.0))
    # 0.5 mm2 per mm at 1 mm units: 2 x area + 1 x wire, not round(0.5) = 0
    assert (problem["wire_coef"], problem["area_coef"]) == (1, 2)
    with pytest.raises(ProjectError, match="wire_weight"):
        build_problem(parts, (0, 0), (None, None), PlaceOptions(wire_weight=-1))


@solver
def test_a_fixed_part_is_judged_where_it_really_stands_at_a_coarse_unit():
    a, b = _square("A", 2, 2), _square("B", 2, 2)
    a.x, a.y = 1.49, 1.0  # fixed; 1 mm units round it to 1
    opts = PlaceOptions(
        unit=1.0,
        gap=0,
        edge=0,
        pad_edge=0,
        fixed=["A"],
        rotations={"B": [0]},
        wire_weight=0,
        time_limit=20,
        workers=1,
    )
    problem = build_problem([a, b], (0, 0), (None, None), opts)
    result = run_solver(problem)
    assert result["status"] == "OPTIMAL"
    (ax, ay, _), (bx, by, _) = exact_positions(problem, result)
    assert (ax, ay) == pytest.approx((1.49, 1.0))
    apart = abs(ax - bx) >= 2 - 1e-9 or abs(ay - by) >= 2 - 1e-9
    assert apart  # no overlap with the part beside it
