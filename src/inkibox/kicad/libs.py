"""Resolve ``nickname:item`` through the project's and KiCad's library tables and load
symbols (flattened, ``extends`` resolved) and footprints from disk."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kicad_libtable import load_all_tables
from kippm.kicad import global_table_expanded
from kippm.kicadenv import expand

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


class LibraryError(RuntimeError):
    pass


@dataclass(slots=True)
class Libraries:
    """Library tables of a KiCad project plus the global ones.

    ``${KIPRJMOD}`` expands to ``project_dir``; KiCad's own variables come from
    :mod:`kippm.kicadenv` (environment, ``kicad_common.json``, ``kicad-cli`` wrapper).
    """

    project_dir: Path
    symbol_libs: dict[str, Path] = field(default_factory=dict)
    footprint_libs: dict[str, Path] = field(default_factory=dict)
    _symbol_files: dict[Path, dict[str, Node]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_dir = self.project_dir.resolve()
        tables = load_all_tables(self.project_dir)
        for kind, target in (
            ("symbol", self.symbol_libs),
            ("footprint", self.footprint_libs),
        ):
            for row in tables[kind].rows:
                if row.type == "KiCad":
                    p = self._expand(row.uri)
                    if p is not None:
                        target[row.name] = p
            glob = global_table_expanded(kind)
            if glob is not None:
                for row in glob.rows:
                    if row.type == "KiCad" and row.name not in target:
                        p = self._expand(row.uri)
                        if p is not None:
                            target[row.name] = p

    def _expand(self, uri: str) -> Path | None:
        text = uri.replace("${KIPRJMOD}", str(self.project_dir)).replace(
            "$(KIPRJMOD)", str(self.project_dir)
        )
        out = expand(text)
        return Path(out) if out is not None else None

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
    """All ``(pin …)`` nodes of a flattened library symbol (every unit and body style)."""
    pins: list[Node] = []
    for unit in children(symbol, "symbol"):
        pins.extend(children(unit, "pin"))
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
