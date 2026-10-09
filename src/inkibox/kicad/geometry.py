"""Plane geometry of footprints as KiCad computes it: point rotation, and the bounding
boxes of graphics (arcs and circles by their true extent) and pads (by shape and
rotation)."""

from __future__ import annotations

import math

from .sexpr import Node, S, atom_text, atoms_of, child, children, head


def rotate(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """A point turned like KiCad's ``RotatePoint`` (y down, positive angles counter-clockwise
    on screen)."""
    if angle_deg % 360 == 0:
        return x, y
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _xy(item: Node, key: str) -> tuple[float, float] | None:
    c = child(item, key)
    if c is None:
        return None
    a = atoms_of(c)
    return float(a[0]), float(a[1])


def arc_extent(
    s: tuple[float, float], m: tuple[float, float], e: tuple[float, float]
) -> list[tuple[float, float]]:
    """Points that bound the arc from ``s`` through ``m`` to ``e``: its ends and every
    axis-extreme point of its circle that the arc passes."""
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # collinear: a straight segment
        return [s, m, e]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ox = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    oy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    r = math.hypot(ax - ox, ay - oy)
    tau = 2 * math.pi

    def ang(p: tuple[float, float]) -> float:
        return math.atan2(p[1] - oy, p[0] - ox) % tau

    t_s, t_m, t_e = ang(s), ang(m), ang(e)
    span = (t_e - t_s) % tau or tau
    if (t_m - t_s) % tau > span:  # the arc runs the other way: walk it from e to s
        t_s, span = t_e, tau - span
    pts = [s, e]
    for k in range(4):
        phi = k * math.pi / 2
        if (phi - t_s) % tau <= span + 1e-9:
            pts.append((ox + r * math.cos(phi), oy + r * math.sin(phi)))
    return pts


def shape_points(item: Node) -> list[tuple[float, float]]:
    """Points whose bounding box is that of a graphic (``fp_*`` or a custom pad's ``gr_*``
    primitive), in its own coordinates; stroke width is not included."""
    kind = (head(item) or "")[3:]
    if kind == "circle":
        c, e = _xy(item, "center"), _xy(item, "end")
        if c is None or e is None:
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    if kind == "arc":
        s, m, e = _xy(item, "start"), _xy(item, "mid"), _xy(item, "end")
        if s is None or e is None:
            return []
        return arc_extent(s, m, e) if m is not None else [s, e]
    pts = [p for p in (_xy(item, k) for k in ("start", "end")) if p is not None]
    p = child(item, "pts")
    if p is not None:
        for seg in p[1:]:
            h = head(seg)
            if h == "xy":
                a = atoms_of(seg)
                pts.append((float(a[0]), float(a[1])))
            elif h == "arc":  # a polygon edge that is an arc
                s, m, e = _xy(seg, "start"), _xy(seg, "mid"), _xy(seg, "end")
                if s is not None and m is not None and e is not None:
                    pts += arc_extent(s, m, e)
    return pts  # line, rect, poly; a bezier lies within its control points


def pad_points(pad: Node) -> list[tuple[float, float]]:
    """Points whose bounding box is that of a pad's copper, in footprint coordinates."""
    at = atoms_of(child(pad, "at") or [S("at"), 0, 0])
    x, y = float(at[0]), float(at[1])
    rot = float(at[2]) if len(at) > 2 else 0.0
    size = atoms_of(child(pad, "size") or [S("size"), 0, 0])
    w, h = float(size[0]) / 2, float(size[1]) / 2
    a = atoms_of(pad)
    shape = atom_text(a[2]) if len(a) > 2 else "rect"
    if shape == "circle":
        return [(x - w, y - w), (x + w, y + w)]
    if shape == "trapezoid":  # rect_delta widens one pair of sides
        delta = child(pad, "rect_delta")
        if delta is not None:
            dx, dy = (float(v) for v in atoms_of(delta)[:2])
            w, h = w + abs(dy) / 2, h + abs(dx) / 2
    local = [(-w, -h), (w, -h), (w, h), (-w, h)]
    if shape == "custom":
        prims = child(pad, "primitives")
        if prims is not None:
            for g in prims[1:]:
                if isinstance(g, list):
                    local += shape_points(g)
    return [(x + px, y + py) for px, py in (rotate(qx, qy, rot) for qx, qy in local)]


def fp_bbox(
    node: Node, layer_filter: str | None = "F.CrtYd"
) -> tuple[float, float, float, float]:
    """Bounding box (footprint coordinates) of the graphics on ``layer_filter`` (all graphics
    when it has none) and the pads. Circles and arcs count by their true extent, pads by
    their shape and rotation."""
    pts: list[tuple[float, float]] = []
    for item in node[1:]:
        if not isinstance(item, list):
            continue
        h = head(item) or ""
        if h.startswith("fp_") and h != "fp_text":
            lay = child(item, "layer")
            if layer_filter is None or (
                lay is not None and atom_text(lay[1]) == layer_filter
            ):
                pts += shape_points(item)
    if not pts and layer_filter is not None:
        return fp_bbox(node, None)
    for pad in children(node, "pad"):
        pts += pad_points(pad)
    if not pts:
        return 0.0, 0.0, 0.0, 0.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)
