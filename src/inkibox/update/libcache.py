"""Library symbols and footprints in the form KiCad itself would write them.

KiCad converts a library item to its current file format when it copies it into a design
(an AKL symbol saved by KiCad 8 carries ``(pin_numbers hide)``; embedded in a KiCad 10
schematic it reads ``(pin_numbers (hide yes))``). Rather than re-implementing those
conversions, every library file is passed through ``kicad-cli sym upgrade`` /
``kicad-cli fp upgrade`` once and the result cached by content hash, so an update copies
exactly what KiCad's own *Update … from Library* would.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from ..kicad.libs import Libraries
from ..kicad.sfile import Node, SFile, parse, read_text, string


class LibraryError(RuntimeError):
    pass


def cache_root() -> Path:
    if env := os.environ.get("INKIBOX_CACHE_DIR"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "inkibox" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "inkibox"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "inkibox"


def kicad_cli() -> str:
    exe = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if not exe:
        raise LibraryError("kicad-cli not found (set KICAD_CLI or put it on PATH)")
    return exe


@cache
def kicad_version() -> str:
    r = subprocess.run(
        [kicad_cli(), "version"], capture_output=True, text=True, check=False
    )
    return r.stdout.strip() or "unknown"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _upgraded(src: Path, kind: str) -> Path:
    """``src`` (a ``.kicad_sym`` or ``.kicad_mod``) as ``kicad-cli … upgrade --force`` writes
    it, cached by content and KiCad version."""
    suffix = ".kicad_sym" if kind == "sym" else ".kicad_mod"
    out = cache_root() / "upgraded" / kicad_version() / f"{_digest(src)}{suffix}"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inkibox-upgrade-") as tmp:
        if kind == "sym":
            target = Path(tmp) / f"lib{suffix}"
            cmd = [
                kicad_cli(),
                "sym",
                "upgrade",
                "--force",
                str(src),
                "-o",
                str(target),
            ]
        else:  # fp upgrade works on a .pretty directory
            lib = Path(tmp) / "in.pretty"
            lib.mkdir()
            shutil.copyfile(src, lib / src.name)
            outdir = Path(tmp) / "out.pretty"
            target = outdir / src.name
            cmd = [kicad_cli(), "fp", "upgrade", "--force", str(lib), "-o", str(outdir)]
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0 or not target.is_file():
            raise LibraryError(
                f"kicad-cli could not upgrade {src}: {(r.stderr or r.stdout).strip()}"
            )
        tmp_out = out.with_suffix(out.suffix + ".part")
        shutil.copyfile(target, tmp_out)
        os.replace(tmp_out, out)
    return out


def flatten(syms: dict[str, Node], name: str) -> Node:
    """A copy of library symbol ``name`` with the parent inlined when it ``extends`` one:
    the parent's body, the derived symbol's fields winning (KiCad's ``Flatten``)."""
    node = syms[name].copy()
    ext = node.child("extends")
    if ext is None:
        return node
    parent_name = ext.atom(0) or ""
    if parent_name not in syms:
        raise LibraryError(f"{name} extends unknown symbol {parent_name}")
    parent = flatten(syms, parent_name)
    out = Node(parent.head, [string(name)])
    own = {p.atom(0): p for p in node.children("property")}
    for item in parent.items[1:]:
        if not isinstance(item, Node):
            out.items.append(item)
        elif item.head == "property":
            out.items.append(own.pop(item.atom(0), item))
        elif item.head == "symbol":
            unit = item.copy()
            unit_name = unit.atom(0) or ""
            if unit_name.startswith(parent_name + "_"):
                unit.set_atom(0, string(name + unit_name[len(parent_name) :]))
            out.items.append(unit)
        else:
            out.items.append(item)
    out.items.extend(own.values())
    return out


@dataclass(slots=True)
class LibraryCache:
    """``nick:item`` through the project's library tables (and KiCad's global ones)."""

    project_dir: Path
    tables: Libraries = field(init=False)
    _symbol_files: dict[Path, dict[str, Node]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_dir = self.project_dir.resolve()
        self.tables = Libraries(self.project_dir)

    # ------------------------------------------------------------------ symbols
    def symbol(self, lib_id: str) -> Node:
        """Library symbol ``nick:name``, flattened and named ``nick:name`` the way KiCad
        embeds it in ``lib_symbols``."""
        nick, _, name = lib_id.partition(":")
        path = self.tables.symbol_libs.get(nick)
        if path is None:
            raise LibraryError(f"unknown symbol library {nick!r} (for {lib_id})")
        if not path.is_file():
            raise LibraryError(f"symbol library {nick!r} not found: {path}")
        if path not in self._symbol_files:
            root = parse(read_text(_upgraded(path, "sym")))
            self._symbol_files[path] = {
                s.atom(0) or "": s for s in root.children("symbol")
            }
        syms = self._symbol_files[path]
        if name not in syms:
            raise LibraryError(f"symbol {name!r} not in {nick} ({path})")
        node = flatten(syms, name)
        node.set_atom(0, string(lib_id))
        return node

    # ------------------------------------------------------------------ footprints
    def footprint_file(self, lib_id: str) -> Path:
        nick, _, name = lib_id.partition(":")
        d = self.tables.footprint_libs.get(nick)
        if d is None:
            raise LibraryError(f"unknown footprint library {nick!r} (for {lib_id})")
        p = d / f"{name}.kicad_mod"
        if not p.is_file():
            raise LibraryError(f"footprint {name!r} not in {nick} ({d})")
        return p

    def footprint(self, lib_id: str) -> Node:
        """Library footprint ``nick:name`` in the current format (a fresh copy)."""
        src = self.footprint_file(lib_id)
        return SFile.load(_upgraded(src, "fp")).root.copy()
