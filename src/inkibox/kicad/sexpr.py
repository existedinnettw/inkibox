"""Small helpers over sexpdata lists for building KiCad files; written out in KiCad's own
layout by :func:`to_pretty` (through :mod:`inkibox.kicad.sfile`)."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

import sexpdata
from sexpdata import Symbol

from . import sfile

Node = list  # a KiCad list: [Symbol(head), item, …]


def parse_one(text: str) -> Node:
    """The single top-level list of ``text`` (bare ``t``/``nil`` stay symbols)."""
    forms = [
        f for f in sexpdata.parse(text, nil=None, true=None) if isinstance(f, list)
    ]
    if len(forms) != 1:
        raise ValueError(f"expected one top-level list, found {len(forms)}")
    return forms[0]


def head(node: Any) -> str | None:
    if isinstance(node, list) and node and isinstance(node[0], Symbol):
        return node[0].value()
    return None


def atom_text(item: Any) -> str:
    """An atom's value as text, without quotes."""
    if isinstance(item, Symbol):
        return item.value()
    if isinstance(item, str):
        return item
    return sfile.number(item).raw if isinstance(item, (int, float)) else str(item)


def children(node: Node, name: str) -> list[Node]:
    return [i for i in node[1:] if isinstance(i, list) and head(i) == name]


def child(node: Node, name: str) -> Node | None:
    found = children(node, name)
    return found[0] if found else None


def _to_sfile(node: Any) -> sfile.Node | sfile.Atom:
    if isinstance(node, list):
        items = [_to_sfile(i) for i in node[1:]]
        return sfile.Node(atom_text(node[0]), items)
    if isinstance(node, Symbol):
        return sfile.symbol(node.value())
    if isinstance(node, str):
        return sfile.string(node)
    if isinstance(node, bool):
        return sfile.symbol("yes" if node else "no")
    if isinstance(node, (int, float)):
        return sfile.number(node)
    return sfile.symbol(str(node))


def to_pretty(node: Node, indent: int = 0) -> str:
    """``node`` in the layout KiCad 10 writes (see :func:`inkibox.kicad.sfile.format_node`)."""
    nd = _to_sfile(node)
    assert isinstance(nd, sfile.Node)
    return "\t" * indent + sfile.format_node(nd, indent)


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
    sfile.write_text(path, to_pretty(node) + "\n")


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
