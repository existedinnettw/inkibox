"""Headless KiCad design updates: *Update Symbols from Library*, then *Update PCB from
Schematic*, then *Update Footprints from Library*, with the options of
:mod:`inkibox.update.options` instead of whatever a machine's KiCad dialogs remember.

Everything runs on copies in a temporary directory: ``kicad-cli`` first brings every sheet
and the board to the current file format (so an edited file is never half KiCad 9, half
KiCad 10), the symbols are updated, the netlist is exported from the updated sheets and
applied to the board, and the board's footprints are refreshed. Only then are the results
compared with the originals; unless it is a dry run, files that differ are written. A
design that is up to date is not touched.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad.sfile import SFile, read_text, write_text
from .footprints import update_footprints
from .libcache import LibraryCache, LibraryError, kicad_cli, kicad_version
from .netlist import components, export_netlist
from .options import UpdateOptions
from .pcb import update_pcb
from .report import Report
from .symbols import update_symbols

STEPS = ("symbols", "pcb", "footprints")


class ProjectError(RuntimeError):
    pass


@dataclass(slots=True)
class UpdateResult:
    report: Report = field(default_factory=Report)
    changed: list[Path] = field(
        default_factory=list
    )  # files that differ (written unless dry run)


def project_files(project_dir: Path) -> tuple[Path, list[Path], Path | None]:
    """``(root schematic, every sheet file of the hierarchy, board or None)``."""
    pros = sorted(project_dir.glob("*.kicad_pro"))
    if len(pros) != 1:
        raise ProjectError(
            f"{project_dir}: expected one *.kicad_pro, found {len(pros)}"
        )
    stem = pros[0].stem
    root = project_dir / f"{stem}.kicad_sch"
    if not root.is_file():
        raise ProjectError(f"{root} not found")
    sheets: list[Path] = []
    todo = [root]
    while todo:
        f = todo.pop(0)
        if f in sheets:
            continue
        sheets.append(f)
        for sheet in SFile.load(f).root.children("sheet"):
            for p in sheet.children("property"):
                if p.atom(0) in ("Sheetfile", "Sheet file"):
                    child = (f.parent / (p.atom(1) or "")).resolve()
                    if child.is_file():
                        todo.append(child)
    pcb = project_dir / f"{stem}.kicad_pcb"
    return root, sheets, pcb if pcb.is_file() else None


def _upgrade(path: Path, kind: str, report: Report) -> None:
    before = path.read_bytes()
    r = subprocess.run(
        [kicad_cli(), kind, "upgrade", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise LibraryError(
            f"kicad-cli {kind} upgrade {path.name}: {(r.stderr or r.stdout).strip()}"
        )
    if path.read_bytes() != before:
        report.change(
            f"{path.name}: saved in the current file format (KiCad {kicad_version()})"
        )


def schematic_attrs(sheets: list[SFile]) -> dict[str, set[str]]:
    """Footprint attribute tokens the schematic owns, by symbol uuid."""
    out: dict[str, set[str]] = {}
    for sch in sheets:
        for sym in sch.root.children("symbol"):
            uid = sym.child("uuid")
            if uid is None:
                continue

            def flag(name: str, default: str, s=sym) -> str:
                c = s.child(name)
                return (c.atom(0) or default) if c is not None else default

            tokens = set()
            if flag("dnp", "no") == "yes":
                tokens.add("dnp")
            if flag("in_bom", "yes") == "no":
                tokens.add("exclude_from_bom")
            if flag("in_pos_files", "yes") == "no":
                tokens.add("exclude_from_pos_files")
            out[uid.atom(0) or ""] = tokens
    return out


def update_project(
    project_dir: Path,
    opts: UpdateOptions,
    *,
    dry_run: bool = False,
    steps: tuple[str, ...] = STEPS,
) -> UpdateResult:
    project_dir = project_dir.resolve()
    result = UpdateResult()
    report = result.report
    root, sheets, board = project_files(project_dir)
    originals = {p: read_text(p) for p in [*sheets, *([board] if board else [])]}
    libs = LibraryCache(project_dir)
    with tempfile.TemporaryDirectory(prefix="inkibox-update-") as tmp_name:
        tmp = Path(tmp_name)
        for f in project_dir.glob("*.kicad_pro"):
            shutil.copyfile(f, tmp / f.name)
        copies: dict[Path, Path] = {}
        for p in originals:
            rel = p.relative_to(project_dir)
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, tmp / rel)
            copies[p] = tmp / rel
        for p, c in copies.items():
            _upgrade(c, "pcb" if p.suffix == ".kicad_pcb" else "sch", report)

        sch_files = [SFile.load(copies[s]) for s in sheets]
        if "symbols" in steps:
            for sch in sch_files:
                sub = update_symbols(sch, libs, opts.symbols)
                report.extend(sub, prefix=f"[symbols] {sch.path.name}: ")
                sch.save()

        if board is not None and ("pcb" in steps or "footprints" in steps):
            pcb = SFile.load(copies[board])
            if "pcb" in steps:
                comps = components(export_netlist(copies[root]))
                uuid_attrs = schematic_attrs(sch_files)
                attrs = {
                    c.path: uuid_attrs.get(c.uuids[0], set())
                    for c in comps.values()
                    if c.uuids
                }
                sub = update_pcb(
                    pcb, comps, attrs, libs, opts.pcb, opts.footprints, project_dir
                )
                report.extend(sub, prefix="[pcb] ")
            if "footprints" in steps:
                sub = update_footprints(pcb, libs, opts.footprints)
                report.extend(sub, prefix="[footprints] ")
            pcb.save()

        for p, c in copies.items():
            new = read_text(c)
            if new != originals[p]:
                result.changed.append(p)
                if not dry_run:
                    write_text(p, new)
    return result


__all__ = ["STEPS", "ProjectError", "UpdateOptions", "UpdateResult", "update_project"]
