"""A small two-layer grid router (Dijkstra on a Manhattan grid) for carrier boards.

Good enough for boards made of modules and a handful of parts: every net is routed
on a uniform grid, one layer prefers horizontal runs and the other vertical, vias
cost extra, previously routed nets and pads are obstacles. Clearances are handled
by inflating obstacles by the design rules, so the result is DRC-clean for the
rules it was given; anything it cannot route is reported, never guessed.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

FREE = 0
BLOCK = -1  # hard obstacle (board edge, keepout)


@dataclass(slots=True)
class RouteResult:
    net: str
    segments: list[tuple[float, float, float, float, str]]  # x1 y1 x2 y2 layer
    vias: list[tuple[float, float]]
    ok: bool = True
    message: str = ""


@dataclass(slots=True)
class Terminal:
    x: float
    y: float
    layers: frozenset[str]  # copper layers the pad is on


@dataclass(slots=True)
class GridRouter:
    x0: float
    y0: float
    x1: float
    y1: float
    pitch: float = 0.3175  # 2.54 / 8
    layers: tuple[str, str] = ("F.Cu", "B.Cu")
    track_width: float = 0.25
    clearance: float = 0.2
    edge_clearance: float = 0.5
    via_size: float = 0.8
    via_drill: float = 0.4
    via_cost: int = 12
    bend_cost: int = 1
    nets: list[str] = field(default_factory=lambda: [""])
    hole_clearance: float = 0.25
    _grid: list[list[int]] = field(default_factory=list)
    _w: int = 0
    _h: int = 0
    _holes: list[tuple[float, float, float]] = field(
        default_factory=list
    )  # x, y, drill radius

    def __post_init__(self) -> None:
        self._w = math.ceil((self.x1 - self.x0) / self.pitch) + 1
        self._h = math.ceil((self.y1 - self.y0) / self.pitch) + 1
        self._grid = [[FREE] * (self._w * self._h) for _ in self.layers]
        edge = self.edge_clearance + self.track_width / 2
        for j in range(self._h):
            for i in range(self._w):
                x, y = self.xy(i, j)
                if (
                    x < self.x0 + edge
                    or x > self.x1 - edge
                    or y < self.y0 + edge
                    or y > self.y1 - edge
                ):
                    for g in self._grid:
                        g[j * self._w + i] = BLOCK

    # ------------------------------------------------------------------ geometry

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return (
            min(max(round((x - self.x0) / self.pitch), 0), self._w - 1),
            min(max(round((y - self.y0) / self.pitch), 0), self._h - 1),
        )

    def xy(self, i: int, j: int) -> tuple[float, float]:
        return self.x0 + i * self.pitch, self.y0 + j * self.pitch

    def net_id(self, net: str) -> int:
        if net not in self.nets:
            self.nets.append(net)
        return self.nets.index(net)

    def _layer_index(self, layer: str) -> int:
        return self.layers.index(layer)

    def _cells_within(self, x: float, y: float, r: float):
        i0, j0 = self.cell(x, y)
        n = math.ceil(r / self.pitch) + 1
        for j in range(max(j0 - n, 0), min(j0 + n, self._h - 1) + 1):
            for i in range(max(i0 - n, 0), min(i0 + n, self._w - 1) + 1):
                cx, cy = self.xy(i, j)
                if math.hypot(cx - x, cy - y) <= r + 1e-9:
                    yield i, j

    # ------------------------------------------------------------------ obstacles

    def add_pad(
        self,
        x: float,
        y: float,
        radius: float,
        layers: frozenset[str],
        net: str | None,
        drill: float = 0.0,
    ) -> None:
        """Block cells around a pad for every other net. ``radius`` is the pad's own
        half-size; clearance and track width are added here. A ``drill`` keeps vias away
        (hole-to-hole and hole clearance rules apply regardless of net)."""
        nid = self.net_id(net) if net else BLOCK
        if drill > 0:
            self._holes.append((x, y, drill / 2))
        r = radius + self.clearance + max(self.track_width, 0.4) / 2
        for li, layer in enumerate(self.layers):
            if layer not in layers:
                continue
            g = self._grid[li]
            for i, j in self._cells_within(x, y, r):
                cur = g[j * self._w + i]
                if cur == FREE or (cur != BLOCK and cur != nid and nid == BLOCK):
                    g[j * self._w + i] = nid
                elif cur != nid and cur != BLOCK:
                    g[j * self._w + i] = (
                        BLOCK  # two different nets claim it: nobody routes here
                    )

    def add_keepout(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        layers: frozenset[str] | None = None,
    ) -> None:
        for li, layer in enumerate(self.layers):
            if layers is not None and layer not in layers:
                continue
            g = self._grid[li]
            for j in range(self._h):
                for i in range(self._w):
                    x, y = self.xy(i, j)
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        g[j * self._w + i] = BLOCK

    def _claim(self, g: list[int], idx: int, nid: int) -> None:
        """Copper of ``nid`` is now within clearance of this cell: free cells become ours,
        cells another net reserved for its pad exits become nobody's (BLOCK), so that net
        cannot later run a track right next to ours."""
        cur = g[idx]
        if cur == FREE:
            g[idx] = nid
        elif cur != nid and cur != BLOCK:
            g[idx] = BLOCK

    def _mark_track(self, li: int, i: int, j: int, nid: int, width: float) -> None:
        r = width / 2 + self.clearance + max(self.track_width, 0.4) / 2 - 1e-6
        g = self._grid[li]
        x, y = self.xy(i, j)
        for ii, jj in self._cells_within(x, y, r):
            self._claim(g, jj * self._w + ii, nid)
        g[j * self._w + i] = nid

    def _mark_via(self, i: int, j: int, nid: int) -> None:
        r = self.via_size / 2 + self.clearance + self.track_width / 2
        x, y = self.xy(i, j)
        self._holes.append((x, y, self.via_drill / 2))
        for g in self._grid:
            for ii, jj in self._cells_within(x, y, r):
                self._claim(g, jj * self._w + ii, nid)
            g[j * self._w + i] = nid

    def _via_ok(self, i: int, j: int, nid: int) -> bool:
        r = self.via_size / 2 + self.clearance + self.track_width / 2
        x, y = self.xy(i, j)
        for g in self._grid:
            for ii, jj in self._cells_within(x, y, r):
                v = g[jj * self._w + ii]
                if v != FREE and v != nid:
                    return False
        # hole-to-hole with every drilled pad and via, whatever the net
        vr = self.via_drill / 2
        for hx, hy, hr in self._holes:
            if math.hypot(hx - x, hy - y) < hr + vr + self.hole_clearance + 0.1:
                return False
        return True

    def _passable(self, li: int, i: int, j: int, nid: int) -> bool:
        v = self._grid[li][j * self._w + i]
        return v == FREE or v == nid

    # ------------------------------------------------------------------ routing

    def route(
        self, net: str, terminals: list[Terminal], width: float | None = None
    ) -> RouteResult:
        """Connect all terminals of ``net`` (a spanning tree grown terminal by terminal)."""
        width = width or self.track_width
        nid = self.net_id(net)
        segs: list[tuple[float, float, float, float, str]] = []
        vias: list[tuple[float, float]] = []
        if len(terminals) < 2:
            return RouteResult(net, segs, vias, True, "single terminal")
        tree: set[tuple[int, int, int]] = set()
        for t in terminals:  # a pad centre is always reachable for its own net
            ci, cj = self.cell(t.x, t.y)
            for li, layer in enumerate(self.layers):
                if layer in t.layers:
                    self._grid[li][cj * self._w + ci] = nid
        first = terminals[0]
        for li, layer in enumerate(self.layers):
            if layer in first.layers:
                ci, cj = self.cell(first.x, first.y)
                tree.add((li, ci, cj))
        remaining = list(terminals[1:])
        while remaining:
            # nearest remaining terminal to the tree (cheap heuristic for the order)
            def dist(t: Terminal) -> float:
                return min(
                    abs(self.xy(i, j)[0] - t.x) + abs(self.xy(i, j)[1] - t.y)
                    for (_l, i, j) in tree
                )

            remaining.sort(key=dist)
            target = remaining.pop(0)
            targets = set()
            ti, tj = self.cell(target.x, target.y)
            for li, layer in enumerate(self.layers):
                if layer in target.layers:
                    targets.add((li, ti, tj))
            path = self._dijkstra(tree, targets, nid)
            if path is None:
                return RouteResult(
                    net, segs, vias, False, f"no path to ({target.x}, {target.y})"
                )
            # stub from the grid to the exact pad centre (pads are not always on grid)
            new_segs, new_vias = self._emit(path, width)
            segs += new_segs
            vias += new_vias
            for li, i, j in path:
                self._mark_track(li, i, j, nid, width)
                tree.add((li, i, j))
            for k in range(1, len(path)):
                if path[k][0] != path[k - 1][0]:
                    self._mark_via(path[k][1], path[k][2], nid)
            # terminal stubs
            for t in (target,):
                self._stub(t, path[-1], segs, width)
            if len(tree) and len(segs) == len(new_segs):
                self._stub(first, path[0], segs, width)
        return RouteResult(net, segs, vias, True)

    def route_to_via(
        self,
        net: str,
        terminal: Terminal,
        width: float | None = None,
        max_len: float = 6.0,
    ) -> RouteResult:
        """From an SMD pad, the shortest way to a spot where a via fits (to reach a zone
        of the same net on the other layer)."""
        width = width or self.track_width
        nid = self.net_id(net)
        li = self._layer_index(next(iter(terminal.layers)))
        si, sj = self.cell(terminal.x, terminal.y)
        self._grid[li][sj * self._w + si] = nid
        best = self._dijkstra_to_via({(li, si, sj)}, nid, max_len)
        if best is None:
            return RouteResult(
                net, [], [], False, f"no via spot near ({terminal.x}, {terminal.y})"
            )
        path = best
        segs, _ = self._emit(path, width)
        for l_, i, j in path:
            self._mark_track(l_, i, j, nid, width)
        vi, vj = path[-1][1], path[-1][2]
        self._mark_via(vi, vj, nid)
        self._stub(terminal, path[0], segs, width)
        return RouteResult(net, segs, [self.xy(vi, vj)], True)

    # ------------------------------------------------------------------ internals

    def _stub(
        self, t: Terminal, cell: tuple[int, int, int], segs, width: float
    ) -> None:
        li, i, j = cell
        x, y = self.xy(i, j)
        if abs(x - t.x) > 1e-6 or abs(y - t.y) > 1e-6:
            segs.append((x, y, t.x, t.y, self.layers[li]))

    def _neighbors(self, li: int, i: int, j: int):
        if i > 0:
            yield li, i - 1, j, 0
        if i < self._w - 1:
            yield li, i + 1, j, 0
        if j > 0:
            yield li, i, j - 1, 1
        if j < self._h - 1:
            yield li, i, j + 1, 1

    def _dijkstra(
        self,
        sources: set[tuple[int, int, int]],
        targets: set[tuple[int, int, int]],
        nid: int,
    ) -> list[tuple[int, int, int]] | None:
        dist: dict[tuple[int, int, int], int] = {}
        prev: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
        heap: list[tuple[int, int, int, int]] = []
        for s in sources:
            dist[s] = 0
            prev[s] = None
            heapq.heappush(heap, (0, *s))
        while heap:
            d, li, i, j = heapq.heappop(heap)
            node = (li, i, j)
            if dist.get(node, 1 << 30) < d:
                continue
            if node in targets:
                path = []
                cur: tuple[int, int, int] | None = node
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                path.reverse()
                return path
            for nl, ni, nj, axis in self._neighbors(li, i, j):
                if not self._passable(nl, ni, nj, nid):
                    continue
                # F.Cu (index 0) prefers horizontal (axis 0), B.Cu vertical
                cost = 2 if (axis == 0) == (nl == 0) else 3
                p = prev.get(node)
                if p is not None and p[0] == li and (p[1] != i) != (ni != i):
                    cost += self.bend_cost
                nd = d + cost
                nb = (nl, ni, nj)
                if nd < dist.get(nb, 1 << 30):
                    dist[nb] = nd
                    prev[nb] = node
                    heapq.heappush(heap, (nd, nl, ni, nj))
            # layer change
            ol = 1 - li
            if self._passable(ol, i, j, nid) and self._via_ok(i, j, nid):
                nb = (ol, i, j)
                nd = d + self.via_cost
                if nd < dist.get(nb, 1 << 30):
                    dist[nb] = nd
                    prev[nb] = node
                    heapq.heappush(heap, (nd, ol, i, j))
        return None

    def _dijkstra_to_via(
        self, sources: set[tuple[int, int, int]], nid: int, max_len: float
    ) -> list[tuple[int, int, int]] | None:
        dist: dict[tuple[int, int, int], int] = {}
        prev: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
        heap: list[tuple[int, int, int, int]] = []
        limit = int(max_len / self.pitch) * 3
        for s in sources:
            dist[s] = 0
            prev[s] = None
            heapq.heappush(heap, (0, *s))
        src = next(iter(sources))
        sx, sy = self.xy(src[1], src[2])
        while heap:
            d, li, i, j = heapq.heappop(heap)
            node = (li, i, j)
            if dist.get(node, 1 << 30) < d or d > limit:
                continue
            x, y = self.xy(i, j)
            if math.hypot(x - sx, y - sy) >= 0.9 and self._via_ok(i, j, nid):
                path = []
                cur: tuple[int, int, int] | None = node
                while cur is not None:
                    path.append(cur)
                    cur = prev[cur]
                path.reverse()
                return path
            for nl, ni, nj, _axis in self._neighbors(li, i, j):
                if not self._passable(nl, ni, nj, nid):
                    continue
                nd = d + 2
                nb = (nl, ni, nj)
                if nd < dist.get(nb, 1 << 30):
                    dist[nb] = nd
                    prev[nb] = node
                    heapq.heappush(heap, (nd, nl, ni, nj))
        return None

    def _emit(self, path: list[tuple[int, int, int]], width: float):
        """Collapse a cell path into straight segments and vias."""
        segs: list[tuple[float, float, float, float, str]] = []
        vias: list[tuple[float, float]] = []
        run_start = path[0]
        prev = path[0]
        direction: tuple[int, int] | None = None
        for cur in path[1:]:
            if cur[0] != prev[0]:  # via
                if run_start != prev:
                    x1, y1 = self.xy(run_start[1], run_start[2])
                    x2, y2 = self.xy(prev[1], prev[2])
                    segs.append((x1, y1, x2, y2, self.layers[prev[0]]))
                vias.append(self.xy(cur[1], cur[2]))
                run_start = cur
                direction = None
            else:
                d = (cur[1] - prev[1], cur[2] - prev[2])
                if direction is not None and d != direction:
                    x1, y1 = self.xy(run_start[1], run_start[2])
                    x2, y2 = self.xy(prev[1], prev[2])
                    segs.append((x1, y1, x2, y2, self.layers[prev[0]]))
                    run_start = prev
                direction = d
            prev = cur
        if run_start != prev:
            x1, y1 = self.xy(run_start[1], run_start[2])
            x2, y2 = self.xy(prev[1], prev[2])
            segs.append((x1, y1, x2, y2, self.layers[prev[0]]))
        return segs, vias

    def stats(self) -> dict[str, int]:
        free = sum(1 for g in self._grid for v in g if v == FREE)
        return {"cells": self._w * self._h * len(self.layers), "free": free}
