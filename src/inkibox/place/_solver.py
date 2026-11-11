"""The placement model for OR-Tools' CP-SAT, run as a script: a problem in JSON on stdin, the
placement in JSON on stdout.

It imports nothing from inkibox, only the standard library and ``ortools``, so it runs in
any interpreter that has OR-Tools: inkibox's own when it can import it, else an isolated
``uv run --with ortools`` environment (OR-Tools needs protobuf 6, kicad-python protobuf 5,
so the two cannot share one).

Every length is an integer in the problem's unit (0.01 mm). A part is a set of options,
one per allowed rotation, each with its courtyard (``box``), the area it keeps clear of
other parts (``keep``: courtyard, grown sides and half the gap), the bounding box of its
pads, the offset of its grid pad and one pin per net; one option is chosen. The board is
``(0, 0)``-``(width, height)``.
"""

from __future__ import annotations

import json
import sys


def solve(p: dict) -> dict:
    from ortools.sat.python import cp_model  # ty: ignore[unresolved-import]  # optional

    m = cp_model.CpModel()
    parts = p["parts"]
    bound = p["bound"]
    board = p["board"]
    edge, pad_edge = board["edge"], board["pad_edge"]

    def size_var(name: str, fixed: int | None):
        return (
            m.new_constant(fixed)
            if fixed is not None
            else m.new_int_var(0, bound, name)
        )

    W = size_var("width", board.get("width"))
    H = size_var("height", board.get("height"))

    xs_iv, ys_iv = [], []
    rows = []  # per part: (x, y, bools, options)
    for i, part in enumerate(parts):
        opts = part["options"]
        x = m.new_int_var(-bound, bound, f"x{i}")
        y = m.new_int_var(-bound, bound, f"y{i}")
        bs = [m.new_bool_var(f"r{i}_{k}") for k in range(len(opts))]
        m.add_exactly_one(bs)
        rows.append((x, y, bs, opts))

        def lin(base, key, idx, bs=bs, opts=opts):
            return base + sum(b * o[key][idx] for b, o in zip(bs, opts))

        cx0, cy0 = lin(x, "box", 0), lin(y, "box", 1)
        cx1, cy1 = lin(x, "box", 2), lin(y, "box", 3)
        part["_c"] = (cx0, cy0, cx1, cy1)
        for b, o in zip(bs, opts):
            k = o["keep"]
            xs_iv.append(
                m.new_optional_fixed_size_interval_var(
                    x + k[0], k[2] - k[0], b, f"kx{i}"
                )
            )
            ys_iv.append(
                m.new_optional_fixed_size_interval_var(
                    y + k[1], k[3] - k[1], b, f"ky{i}"
                )
            )
        # on the board: the courtyard within ``edge`` of it (or past it by the part's
        # overhang), the pads always ``pad_edge`` inside
        ov = part["overhang"]
        m.add(cx0 >= edge - ov)
        m.add(cy0 >= edge - ov)
        m.add(cx1 <= W - edge + ov)
        m.add(cy1 <= H - edge + ov)
        m.add(lin(x, "pads", 0) >= pad_edge)
        m.add(lin(y, "pads", 1) >= pad_edge)
        m.add(lin(x, "pads", 2) <= W - pad_edge)
        m.add(lin(y, "pads", 3) <= H - pad_edge)
        for side in part["touch"]:
            m.add(
                {
                    "top": cy0 <= edge,
                    "bottom": cy1 >= H - edge,
                    "left": cx0 <= edge,
                    "right": cx1 >= W - edge,
                }[side]
            )
        # the grid: its pad on a grid point; off the grid: the origin on the resolution.
        # Neither binds a fixed part, which stays exactly where it is.
        grid = p.get("grid")
        if part["fixed"]:
            pass
        elif grid and part["on_grid"]:
            for v, idx, o in ((x, 0, grid["ox"]), (y, 1, grid["oy"])):
                k = m.new_int_var(
                    -bound // grid["pitch"] - 1, bound // grid["pitch"] + 1, ""
                )
                m.add(lin(v, "grid", idx) - grid["pitch"] * k == o)
        elif p["res"] > 1:
            for v in (x, y):
                k = m.new_int_var(-bound // p["res"] - 1, bound // p["res"] + 1, "")
                m.add(v == p["res"] * k)
        if part["fixed"]:
            hx, hy, hr = part["hint"]
            m.add(x == hx)
            m.add(y == hy)
            for b, o in zip(bs, opts):
                m.add(b == (1 if o["rot"] == hr else 0))
        if part["hint"] is not None:
            hx, hy, hr = part["hint"]
            m.add_hint(x, hx)
            m.add_hint(y, hy)
            for b, o in zip(bs, opts):
                m.add_hint(b, 1 if o["rot"] == hr else 0)

    m.add_no_overlap_2d(xs_iv, ys_iv)

    for rule in p.get("apart", []):
        gap = rule["gap"]
        for i in rule["a"]:
            for j in rule["b"]:
                a, b = parts[i]["_c"], parts[j]["_c"]
                ways = [m.new_bool_var("") for _ in range(4)]
                m.add(a[2] + gap <= b[0]).only_enforce_if(ways[0])
                m.add(b[2] + gap <= a[0]).only_enforce_if(ways[1])
                m.add(a[3] + gap <= b[1]).only_enforce_if(ways[2])
                m.add(b[3] + gap <= a[1]).only_enforce_if(ways[3])
                m.add_bool_or(ways)

    # estimated wire length: the half perimeter of each net's pins (in the objective
    # only when it has a weight; reported from the solved pins either way)
    spans = []  # per net and axis, the pins' coordinates
    for net, members in p.get("nets", {}).items():
        for idx in (0, 1):
            pts = []
            for i in members:
                x, y, bs, opts = rows[i]
                base = x if idx == 0 else y
                pts.append(
                    base + sum(b * o["pins"][net][idx] for b, o in zip(bs, opts))
                )
            spans.append(pts)
    wire = []
    if p["wire_coef"]:
        for pts in spans:
            lo = m.new_int_var(-bound, bound, "")
            hi = m.new_int_var(-bound, bound, "")
            for e in pts:
                m.add(lo <= e)
                m.add(hi >= e)
            wire.append(hi - lo)
    hpwl = sum(wire) if wire else 0

    if board.get("width") is not None and board.get("height") is not None:
        area = board["width"] * board["height"]
    elif board.get("width") is not None:
        area = board["width"] * H
    elif board.get("height") is not None:
        area = board["height"] * W
    else:
        area = m.new_int_var(0, bound * bound, "area")
        m.add_multiplication_equality(area, [W, H])
    m.minimize(area + p["wire_coef"] * hpwl)

    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = float(p["time"])
    s.parameters.num_workers = int(p["workers"])
    s.parameters.random_seed = int(p["seed"])
    status = s.solve(m)
    name = s.status_name(status)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": name}
    out = []  # in the problem's order: references need not be unique
    for part, (x, y, bs, opts) in zip(parts, rows):
        k = next(k for k, b in enumerate(bs) if s.value(b))
        out.append([s.value(x), s.value(y), opts[k]["rot"]])
    return {
        "status": name,
        "width": s.value(W),
        "height": s.value(H),
        "wire": sum(
            max(s.value(e) for e in pts) - min(s.value(e) for e in pts) for pts in spans
        ),
        "bound": s.best_objective_bound,
        "objective": s.objective_value,
        "parts": out,
    }


if __name__ == "__main__":
    json.dump(solve(json.load(sys.stdin)), sys.stdout)
