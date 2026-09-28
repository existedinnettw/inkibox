"""Small helpers over ``kicad_libtable.sexpr`` (sexpdata lists) for building KiCad files."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

from kicad_libtable.sexpr import (
    Node,
    atom_text,
    child,
    children,
    head,
    parse_one,
    to_pretty,
)
from sexpdata import Symbol

__all__ = [
    "Node",
    "S",
    "atom_text",
    "atoms_of",
    "child",
    "children",
    "clone",
    "head",
    "num",
    "parse_file",
    "replace_child",
    "req",
    "set_atom",
    "stable_uuid",
    "write_pretty",
]

_NS = uuid.UUID("4d2f2a0e-9b7c-4b7e-8a6e-1f3c5d7e9a1b")


def S(name: str) -> Symbol:
    return Symbol(name)


def num(v: float) -> float | int:
    """Round to 4 decimals and drop a trailing ``.0`` the way KiCad writes numbers."""
    r = round(float(v), 4)
    if r == 0:
        return 0
    return int(r) if r == int(r) else r


def stable_uuid(*parts: str) -> str:
    """Deterministic uuid so that regenerating a design keeps every item's identity."""
    return str(uuid.uuid5(_NS, ":".join(parts)))


def parse_file(path: Path) -> Node:
    return parse_one(path.read_text(encoding="utf-8", errors="replace"))


def write_pretty(path: Path, node: Node) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_pretty(node) + "\n", encoding="utf-8")


def clone(node: Node) -> Node:
    return copy.deepcopy(node)


def req(node: Node, name: str) -> Node:
    """The first child called ``name``; raises when the file lacks it."""
    c = child(node, name)
    if c is None:
        raise ValueError(f"({head(node)} …) has no ({name} …)")
    return c


def atoms_of(node: Node) -> list[Any]:
    """Atoms of a node in order (not the head)."""
    return [i for i in node[1:] if not isinstance(i, list)]


def set_atom(node: Node, index: int, value: Any) -> None:
    """Set the ``index``-th atom (0-based, head excluded) of ``node``."""
    seen = -1
    for i, item in enumerate(node):
        if i == 0 or isinstance(item, list):
            continue
        seen += 1
        if seen == index:
            node[i] = value
            return
    node.append(value)


def replace_child(node: Node, name: str, new: Node | None) -> None:
    """Replace the first child called ``name`` (append if absent); ``None`` removes it."""
    for i, item in enumerate(node):
        if isinstance(item, list) and head(item) == name:
            if new is None:
                del node[i]
            else:
                node[i] = new
            return
    if new is not None:
        node.append(new)
