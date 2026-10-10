"""The uuids of the items inside a board footprint.

KiCad tells board items apart by uuid: DRC markers, selections, cross-probing and groups
refer to items by it, and two items with one uuid make KiCad name the wrong footprint. A
library footprint carries uuids for its items, the same in every copy, so whatever a board
takes from a library gets uuids of its own, derived from the footprint's uuid (an update
gives the same uuids every time).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet

from ..kicad.sexpr import stable_uuid
from ..kicad.sfile import Atom, Node, node, string

# footprint items KiCad gives a uuid (a library may ship some without, KiCad makes one up)
OWNED = frozenset(
    (
        "property",
        "fp_line",
        "fp_rect",
        "fp_circle",
        "fp_arc",
        "fp_poly",
        "fp_curve",
        "fp_text",
        "fp_text_box",
        "image",
        "barcode",
        "table",
        "pad",
        "zone",
        "group",
    )
)


def uuid_of(item: Node) -> str | None:
    u = item.child("uuid")
    return None if u is None else u.atom(0)


def set_uuid(item: Node, value: str) -> None:
    u = item.child("uuid")
    if u is None:
        item.items.append(node("uuid", value))
    else:
        item.replace_child(u, node("uuid", value))


def stamp(items: Iterable[Node], owner: str) -> dict[str, str]:
    """Give library ``items`` (fresh copies) the uuids of the footprint whose uuid is
    ``owner``: one per library uuid, one per position for an item that has none; group
    members follow. Returns the renames (library uuid -> new)."""
    items = list(items)
    renames: dict[str, str] = {}
    for index, item in enumerate(items):
        for sub in item.walk():
            old = uuid_of(sub)
            if old is not None:
                renames[old] = stable_uuid(owner, "lib", old)
                set_uuid(sub, renames[old])
            elif sub is item and item.head in OWNED:
                set_uuid(sub, stable_uuid(owner, "lib", item.head, str(index)))
    rename_members(items, renames)
    return renames


def rename_members(items: Iterable[Node], renames: Mapping[str, str]) -> None:
    """Point the members of every group among ``items`` at the renamed uuids."""
    for item in items:
        for group in item.walk():
            members = group.child("members") if group.head == "group" else None
            if members is None:
                continue
            members.items = [
                string(renames.get(i.text, i.text)) if isinstance(i, Atom) else i
                for i in members.items
            ]


def duplicates(root: Node) -> set[str]:
    """The uuids more than one item of ``root`` has."""
    seen = Counter(u.atom(0) for u in root.walk() if u.head == "uuid")
    return {u for u, n in seen.items() if u is not None and n > 1}


def make_unique(fp: Node, taken: AbstractSet[str]) -> list[str]:
    """Give every item of ``fp`` whose uuid is in ``taken`` (shared with other items of
    the board) or repeats inside ``fp`` a uuid of its own; group members follow. The
    footprint's own uuid stays. Returns the uuids replaced."""
    owner = uuid_of(fp) or ""
    seen: set[str] = set()
    renames: dict[str, str] = {}
    replaced: list[str] = []
    for index, sub in enumerate(fp.walk()):
        old = uuid_of(sub) if sub is not fp else None
        if old is None:
            continue
        if old in taken or old in seen:
            new = stable_uuid(owner, "unique", sub.head, str(index))
            replaced.append(old)
            if old in taken:  # a repeat inside fp leaves the members with the first
                renames.setdefault(old, new)
            set_uuid(sub, new)
            seen.add(new)
        else:
            seen.add(old)
    rename_members([fp], renames)
    return replaced
