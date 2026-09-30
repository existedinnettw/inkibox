"""The schematic's netlist as ``kicad-cli sch export netlist`` writes it (KiCad's own
connectivity: net names, unconnected pins, pin functions and types)."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad.sfile import Node, parse
from .libcache import LibraryError, kicad_cli


@dataclass(slots=True)
class Pin:
    net: str
    function: str | None
    type: str | None


@dataclass(slots=True)
class Component:
    ref: str
    value: str
    footprint: str
    fields: dict[str, str]  # Datasheet, Description, user fields (not Footprint)
    properties: dict[str, str]  # Sheetname, Sheetfile, ki_fp_filters, ki_keywords, …
    sheet_names: str  # "/" or "/AIx4/"
    sheet_tstamps: str  # "/" or "/f3194e3b-…/"
    uuids: list[str]  # the symbol's (one per unit)
    pins: dict[str, Pin] = field(default_factory=dict)

    def paths(self) -> list[str]:
        base = self.sheet_tstamps.rstrip("/")
        return [f"{base}/{u}" for u in self.uuids]

    @property
    def path(self) -> str:
        return self.paths()[0]


def export_netlist(root_sch: Path) -> Node:
    with tempfile.TemporaryDirectory(prefix="inkibox-net-") as tmp:
        out = Path(tmp) / "design.net"
        r = subprocess.run(
            [
                kicad_cli(),
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadsexpr",
                "-o",
                str(out),
                str(root_sch),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if not out.is_file():
            raise LibraryError(
                f"kicad-cli could not export the netlist: {(r.stderr or r.stdout).strip()}"
            )
        return parse(out.read_text(encoding="utf-8"))


def _text(nd: Node | None, index: int = 0) -> str:
    return (nd.atom(index) or "") if nd is not None else ""


def components(net: Node) -> dict[str, Component]:
    """Components by reference, with every pin's net."""
    comps: dict[str, Component] = {}
    for c in (net.child("components") or Node("components")).children("comp"):
        fields: dict[str, str] = {}
        for f in (c.child("fields") or Node("fields")).children("field"):
            name = _text(f.child("name"))
            if name and name != "Footprint":
                fields[name] = f.atom(0) or ""
        props = {
            _text(p.child("name")): _text(p.child("value"))
            for p in c.children("property")
        }
        sheet = c.child("sheetpath")
        tstamps = c.child("tstamps")
        comp = Component(
            ref=_text(c.child("ref")),
            value=_text(c.child("value")),
            footprint=_text(c.child("footprint")),
            fields=fields,
            properties=props,
            sheet_names=_text(sheet.child("names")) if sheet is not None else "/",
            sheet_tstamps=_text(sheet.child("tstamps")) if sheet is not None else "/",
            uuids=[a.text for a in tstamps.atoms()] if tstamps is not None else [],
        )
        comps[comp.ref] = comp
    for n in (net.child("nets") or Node("nets")).children("net"):
        name = _text(n.child("name"))
        for node_ in n.children("node"):
            ref = _text(node_.child("ref"))
            pin = _text(node_.child("pin"))
            if ref in comps:
                fn = node_.child("pinfunction")
                ty = node_.child("pintype")
                comps[ref].pins[pin] = Pin(
                    name, _text(fn) if fn else None, _text(ty) if ty else None
                )
    return comps
