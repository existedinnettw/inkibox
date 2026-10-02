"""The KiCad half of ``inkibox route``, run by KiCad's own Python (``pcbnew``).

    python _worker.py export BOARD DSN [MARGIN_MM]
    python _worker.py import BOARD SES OUT [--stitch JSON]

Standard library and ``pcbnew`` only: this file runs in whatever interpreter carries
KiCad's bindings (the KiCad image's ``/usr/bin/python3``, the bundled Python on Windows and
macOS), which need not have inkibox's dependencies.

``export`` locks every existing track and via before writing the Specctra DSN, so that
Freerouting keeps them (KiCad exports locked copper as ``(type protect)``); without that it
rips up hand-drawn copper it finds redundant. A margin widens every net-class clearance in
the exported rules only: Freerouting's diagonal segments can end a few nm inside the
clearance it was given (a DRC error on dvsp_esp_32-s3_core_b). ``import`` reads the session into a fresh
copy of the board (lock flags as they were), refills every zone (a .kicad_pcb stores its
fills, and Freerouting's tracks cut them) and optionally adds GND stitching vias.
"""

from __future__ import annotations

import json
import math
import sys

import pcbnew  # ty: ignore[unresolved-import]  # KiCad's bindings, not in the venv


def _export(board_path: str, dsn: str, margin_mm: float = 0.0) -> None:
    board = pcbnew.LoadBoard(board_path)
    for t in board.GetTracks():
        t.SetLocked(True)
    if margin_mm:
        ns = board.GetDesignSettings().m_NetSettings
        extra = int(pcbnew.FromMM(margin_mm))
        for nc in [ns.GetDefaultNetclass(), *ns.GetNetclasses().values()]:
            nc.SetClearance(nc.GetClearance() + extra)
        ns.RecomputeEffectiveNetclasses()
        board.SynchronizeNetsAndNetClasses(False)
    if not pcbnew.ExportSpecctraDSN(board, dsn):
        sys.exit("Specctra DSN export failed")


def _import(board_path: str, ses: str, out: str, stitch: dict | None) -> None:
    board = pcbnew.LoadBoard(board_path)
    locked = {t.m_Uuid.AsString() for t in board.GetTracks() if t.IsLocked()}
    for t in board.GetTracks():
        t.SetLocked(True)
    if not pcbnew.ImportSpecctraSES(board, ses):
        sys.exit("Specctra session import failed")
    for t in board.GetTracks():
        t.SetLocked(t.m_Uuid.AsString() in locked)
    added = rescued = orphaned = 0
    _fill(board)
    if stitch:
        stitcher = _Stitcher(board, **stitch)
        added = stitcher.grid(stitch.get("pitch", 2.54))
        _fill(board)
        stitcher.refresh()
        rescued, orphaned = stitcher.rescue()
        _fill(board)
    board.Save(out)
    print(json.dumps({"stitching_vias": added + rescued, "orphaned": orphaned}))


def _fill(board) -> None:
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())


class _Stitcher:
    """Vias of ``net`` on a grid, where the net's pours are solid on both outer layers all
    around the via (the pours keep their clearance to other nets, so the via does too), the
    hole-to-hole distance to every drill holds, no courtyard is hit and no pad of the net is
    touched (no solder wicking)."""

    def __init__(
        self,
        board,
        net: str = "GND",
        size: float = 0.6,
        drill: float = 0.3,
        hole_to_hole: float = 0.5,
        pitch: float = 2.54,
    ) -> None:
        mm = pcbnew.FromMM
        self.board = board
        self.netinfo = board.FindNet(net)
        if self.netinfo is None:
            sys.exit(f"stitch: no net {net!r} on the board")
        self.net = net
        self.size, self.drill = int(mm(size)), int(mm(drill))
        self.hole_gap = int(mm(drill / 2 + hole_to_hole))
        self.pad_gap = int(mm(size / 2 + 0.05))
        r = mm(size / 2 + 0.05)
        self.ring = [(0, 0)] + [
            (round(r * math.cos(k * math.pi / 4)), round(r * math.sin(k * math.pi / 4)))
            for k in range(8)
        ]
        pads = [p for fp in board.GetFootprints() for p in fp.Pads()]
        self.own_pads = [p for p in pads if p.GetNetname() == net]
        self.holes = [
            (p.GetPosition().x, p.GetPosition().y, max(p.GetDrillSize().x, p.GetDrillSize().y) // 2)
            for p in pads
            if p.GetDrillSize().x > 0
        ]  # fmt: skip
        self.holes += [
            (t.GetPosition().x, t.GetPosition().y, t.GetDrillValue() // 2)
            for t in board.GetTracks()
            if t.Type() == pcbnew.PCB_VIA_T
        ]
        self.refresh()

    def refresh(self) -> None:
        zones = [
            z
            for z in self.board.Zones()
            if not z.GetIsRuleArea() and z.GetNetname() == self.net
        ]
        self.front = [
            z.GetFilledPolysList(pcbnew.F_Cu) for z in zones if z.IsOnLayer(pcbnew.F_Cu)
        ]
        self.back = [
            z.GetFilledPolysList(pcbnew.B_Cu) for z in zones if z.IsOnLayer(pcbnew.B_Cu)
        ]

    def _inside(self, fills, x: int, y: int) -> bool:
        return all(
            any(f.Contains(pcbnew.VECTOR2I(x + dx, y + dy)) for f in fills)
            for dx, dy in self.ring
        )

    def fits(self, x: int, y: int, front=None) -> bool:
        if not (self.front and self.back):
            return False
        if not (
            self._inside([front] if front is not None else self.front, x, y)
            and self._inside(self.back, x, y)
        ):
            return False
        if any(
            math.hypot(hx - x, hy - y) < hr + self.hole_gap for hx, hy, hr in self.holes
        ):
            return False
        p = pcbnew.VECTOR2I(x, y)
        return not any(pad.HitTest(p, self.pad_gap) for pad in self.own_pads)

    def grid(self, pitch: float) -> int:
        step = int(pcbnew.FromMM(pitch))
        courtyards = [
            fp.GetCourtyard(pcbnew.F_CrtYd) for fp in self.board.GetFootprints()
        ]
        bb = self.board.GetBoardEdgesBoundingBox()
        added = 0
        for y in range(bb.GetTop() + step // 2, bb.GetBottom(), step):
            for x in range(bb.GetLeft() + step // 2, bb.GetRight(), step):
                if any(c.Contains(pcbnew.VECTOR2I(x, y)) for c in courtyards):
                    continue
                if self.fits(x, y):
                    self._add(x, y)
                    added += 1
        return added

    def _add(self, x: int, y: int) -> None:
        via = pcbnew.PCB_VIA(self.board)
        via.SetPosition(pcbnew.VECTOR2I(x, y))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetWidth(self.size)
        via.SetDrill(self.drill)
        via.SetNet(self.netinfo)
        self.board.Add(via)
        self.holes.append((x, y, self.drill // 2))

    def _orphans(self) -> list:
        """F.Cu pour fragments of the net with no via, drilled pad or track of the net in
        them: KiCad keeps such an island only while something of the net touches it."""
        anchors = [pcbnew.VECTOR2I(x, y) for x, y, _r in self.holes]
        anchors += [
            t.GetStart()
            for t in self.board.GetTracks()
            if t.Type() == pcbnew.PCB_TRACE_T and t.GetNetname() == self.net
        ]
        out = []
        for fill in self.front:
            for i in range(fill.OutlineCount()):
                frag = pcbnew.SHAPE_POLY_SET()
                frag.AddOutline(fill.Outline(i))
                for h in range(fill.HoleCount(i)):
                    frag.AddHole(fill.Hole(i, h))
                if not any(frag.Contains(a) for a in anchors):
                    out.append(frag)
        return out

    def rescue(self, pitch: float = 0.1) -> tuple[int, int]:
        """One via into every orphaned F.Cu fragment, searched on a fine grid inside it;
        returns (vias added, fragments left without one)."""
        step = int(pcbnew.FromMM(pitch))
        added = failed = 0
        for frag in self._orphans():
            bb = frag.BBox()
            spot = next(
                (
                    (x, y)
                    for y in range(bb.GetTop(), bb.GetBottom(), step)
                    for x in range(bb.GetLeft(), bb.GetRight(), step)
                    if self.fits(x, y, frag)
                ),
                None,
            )
            if spot is None:
                failed += 1
            else:
                self._add(*spot)
                added += 1
        return added, failed


def main(argv: list[str]) -> None:
    if argv[:1] == ["export"] and len(argv) in (3, 4):
        _export(argv[1], argv[2], float(argv[3]) if len(argv) == 4 else 0.0)
    elif argv[:1] == ["import"] and len(argv) in (4, 6):
        stitch = (
            json.loads(argv[5]) if len(argv) == 6 and argv[4] == "--stitch" else None
        )
        _import(argv[1], argv[2], argv[3], stitch)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
