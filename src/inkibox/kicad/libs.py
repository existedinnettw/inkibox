"""Resolve ``nickname:item`` through the project's and KiCad's library tables and load
symbols (flattened, ``extends`` resolved) and footprints from disk."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .sexpr import (
    Node,
    atom_text,
    atoms_of,
    child,
    children,
    clone,
    head,
    parse_file,
    set_atom,
)
from .tables import library_paths


class LibraryError(RuntimeError):
    pass


@dataclass(slots=True)
class Libraries:
    """Library tables of a KiCad project plus the global ones.

    ``${KIPRJMOD}`` expands to ``project_dir``; KiCad's own variables come from
    :mod:`inkibox.kicad.tables` (environment, ``kicad_common.json``, ``kicad-cli`` wrapper).
    """

    project_dir: Path
    symbol_libs: dict[str, Path] = field(default_factory=dict)
    footprint_libs: dict[str, Path] = field(default_factory=dict)
    _symbol_files: dict[Path, dict[str, Node]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_dir = self.project_dir.resolve()
        self.symbol_libs.update(library_paths(self.project_dir, "symbol"))
        self.footprint_libs.update(library_paths(self.project_dir, "footprint"))

    # ------------------------------------------------------------------ symbols

    def _symbols_of(self, path: Path) -> dict[str, Node]:
        if path not in self._symbol_files:
            if not path.is_file():
                raise LibraryError(f"symbol library not found: {path}")
            root = parse_file(path)
            self._symbol_files[path] = {
                atom_text(s[1]): s for s in children(root, "symbol") if len(s) > 1
            }
        return self._symbol_files[path]

    def symbol(self, lib_id: str) -> Node:
        """The library symbol ``nick:name``, flattened (``extends`` resolved) and renamed
        ``nick:name`` the way KiCad embeds it in a schematic's ``lib_symbols``."""
        nick, _, name = lib_id.partition(":")
        path = self.symbol_libs.get(nick)
        if path is None:
            raise LibraryError(
                f"unknown symbol library nickname {nick!r} (for {lib_id})"
            )
        syms = self._symbols_of(path)
        if name not in syms:
            raise LibraryError(f"symbol {name!r} not in {path}")
        node = _flatten(syms, name)
        set_atom(node, 0, lib_id)
        return node

    # ------------------------------------------------------------------ footprints

    def footprint_path(self, lib_id: str) -> Path:
        nick, _, name = lib_id.partition(":")
        d = self.footprint_libs.get(nick)
        if d is None:
            raise LibraryError(
                f"unknown footprint library nickname {nick!r} (for {lib_id})"
            )
        p = d / f"{name}.kicad_mod"
        if not p.is_file():
            raise LibraryError(f"footprint not found: {p}")
        return p

    def footprint(self, lib_id: str) -> Node:
        node = parse_file(self.footprint_path(lib_id))
        if head(node) != "footprint":
            raise LibraryError(f"{lib_id}: not a footprint file")
        return node


def _flatten(syms: dict[str, Node], name: str) -> Node:
    """A copy of ``name`` with a parent's body inlined when it ``extends`` one."""
    node = clone(syms[name])
    ext = child(node, "extends")
    if ext is None:
        return node
    parent_name = atom_text(ext[1])
    if parent_name not in syms:
        raise LibraryError(f"{name} extends unknown symbol {parent_name}")
    parent = _flatten(syms, parent_name)
    out: Node = [parent[0], name]
    own_props = {atom_text(p[1]): p for p in children(node, "property")}
    for item in parent[2:]:
        if not isinstance(item, list):
            out.append(item)
            continue
        h = head(item)
        if h == "property":
            key = atom_text(item[1])
            out.append(own_props.pop(key, item))
        elif h == "symbol":
            unit = clone(item)
            unit_name = atom_text(unit[1])
            if unit_name.startswith(parent_name + "_"):
                set_atom(unit, 0, name + unit_name[len(parent_name) :])
            out.append(unit)
        else:
            out.append(item)
    for prop in own_props.values():
        out.append(prop)
    return out


def pin_defs(symbol: Node) -> list[Node]:
    """The ``(pin …)`` nodes of a flattened library symbol: every unit, body style 1 only.

    Sub-symbols are named ``<name>_<unit>_<style>``; style 0 is common to all styles, 2 is
    the De Morgan alternate, whose pins may sit elsewhere (the AKL LED and TVS symbols) and
    which a placed symbol does not show unless asked to."""
    pins: list[Node] = []
    for unit in children(symbol, "symbol"):
        style = atom_text(unit[1]).rsplit("_", 1)[-1]
        if style in ("0", "1") or not style.isdigit():
            pins.extend(children(unit, "pin"))
    return pins


def unit_pin_defs(symbol: Node, unit: int) -> list[Node]:
    """The pins of unit ``unit`` (``<name>_<unit>_<style>``) and of unit 0, which is common
    to every unit; body style 1 only. A symbol with one unit gets all its pins."""
    units = {
        int(atom_text(u[1]).rsplit("_", 2)[-2])
        for u in children(symbol, "symbol")
        if atom_text(u[1]).rsplit("_", 2)[-2].isdigit()
    }
    if len(units - {0}) <= 1:
        return pin_defs(symbol)
    pins: list[Node] = []
    for sub in children(symbol, "symbol"):
        parts = atom_text(sub[1]).rsplit("_", 2)
        if len(parts) < 3 or not parts[-2].isdigit():
            continue
        u, style = int(parts[-2]), parts[-1]
        if u in (0, unit) and style in ("0", "1"):
            pins.extend(children(sub, "pin"))
    return pins


def pin_geometry(pin: Node) -> tuple[str, str, float, float, float, float]:
    """``(number, name, x, y, angle, length)`` of a library pin (library coordinates, y up).
    ``(x, y)`` is the connection point; ``angle`` is the direction toward the body."""
    at = child(pin, "at")
    vals = (list(atoms_of(at)) if at is not None else []) + [0, 0, 0]
    x, y, angle = (float(v) for v in vals[:3])
    length_node = child(pin, "length")
    length = float(atoms_of(length_node)[0]) if length_node is not None else 2.54
    number_node = child(pin, "number")
    name_node = child(pin, "name")
    number = atom_text(number_node[1]) if number_node is not None else ""
    pname = atom_text(name_node[1]) if name_node is not None else ""
    return number, pname, x, y, angle, length
