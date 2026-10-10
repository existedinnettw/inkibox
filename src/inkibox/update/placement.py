"""Library footprints in the form a board stores them.

A board keeps a placed footprint in the footprint's own frame: geometry is local and
unrotated, only the angles of pads and texts are absolute (library angle plus the
footprint's rotation); a footprint on the back is mirrored top to bottom (``y`` negated,
``F.*`` layers become ``B.*``, pad angles negated, text angles ``180 - a``, texts
``(justify mirror)``). Zones are the exception: a board stores a footprint's zones in
board coordinates, on the board's copper layers only. :func:`place` turns a library
footprint into that form.
"""

from __future__ import annotations

import math
import re

from ..kicad.sfile import Atom, Node, node, number, string, symbol

LIB_HEADER = ("version", "generator", "generator_version")


def _num(a: Atom) -> float:
    return float(a.raw)


def absolute_angle(value: float) -> Atom:
    """An absolute item angle as KiCad writes it: in [0, 360), integral when it is."""
    v = round(value % 360, 6)
    if v == 360:
        v = 0
    return number(int(v) if v == int(v) else v)


def flip_layer(name: str, copper_layers: int) -> str:
    """KiCad's ``FlipLayer`` (top/bottom) for a layer name."""
    if name.startswith("F."):
        return "B." + name[2:]
    if name.startswith("B."):
        return "F." + name[2:]
    m = re.fullmatch(r"In(\d+)\.Cu", name)
    if m and copper_layers > 2:
        return f"In{copper_layers - 1 - int(m.group(1))}.Cu"
    return name


def copper_layer_count(pcb: Node) -> int:
    layers = pcb.child("layers")
    if layers is None:
        return 2
    return sum(
        1 for layer in layers.children() if (layer.atom(1) or "").endswith(".Cu")
    )


def _flip_xy(nd: Node) -> None:
    """Negate the y of an ``(at|start|end|mid|center|xy|offset … x y …)`` node."""
    atoms = nd.atoms()
    if len(atoms) >= 2:
        nd.set_atom(1, number(-_num(atoms[1]) + 0.0))


XY_HEADS = ("at", "start", "end", "mid", "center", "xy", "offset")


def _flip_item(item: Node, copper: int) -> None:
    """Mirror one footprint item top to bottom, recursively."""
    for sub in item.walk():  # (model …) is never passed here: its offsets stay
        if sub.head in XY_HEADS:
            _flip_xy(sub)
        elif sub.head in ("layer", "layers"):
            for i, a in enumerate(sub.atoms()):
                if a.is_string:
                    sub.set_atom(i, string(flip_layer(a.text, copper)))
        elif sub.head == "chamfer":
            swap = {
                "top_left": "bottom_left",
                "bottom_left": "top_left",
                "top_right": "bottom_right",
                "bottom_right": "top_right",
            }
            for i, a in enumerate(sub.atoms()):
                sub.set_atom(i, symbol(swap.get(a.raw, a.raw)))
        elif sub.head == "rect_delta":
            atoms = sub.atoms()
            if len(atoms) >= 2:
                sub.set_atom(1, number(-_num(atoms[1]) + 0.0))


def _texts(item: Node) -> bool:
    return item.head in ("property", "fp_text")


def _mirror_justify(item: Node) -> None:
    effects = item.child("effects")
    if effects is None:
        return
    justify = effects.child("justify")
    if justify is None:
        effects.items.append(node("justify", symbol("mirror")))
        return
    raws = [a.raw for a in justify.atoms()]
    if "mirror" in raws:
        justify.items = [
            a for a in justify.items if not (isinstance(a, Atom) and a.raw == "mirror")
        ]
        if not justify.items:
            effects.replace_child(justify, None)
    else:
        justify.items.append(symbol("mirror"))


def _set_angle(at: Node, angle: float, *, keep_zero: bool) -> None:
    atoms = at.atoms()
    a = absolute_angle(angle)
    extra = atoms[3:]  # e.g. "unlocked"
    new: list[Atom | Node] = [atoms[0], atoms[1]]
    if keep_zero or a.raw != "0":
        new.append(a)
    new.extend(extra)
    at.items = new + [i for i in at.items if isinstance(i, Node)]


def _board_layers(copper: int) -> set[str]:
    inner = {f"In{i}.Cu" for i in range(1, copper - 1)}
    return {"F.Cu", "B.Cu", *inner}


def _place_zone(
    zone: Node, at: tuple[float, float], rotation: float, copper: int
) -> None:
    """A (flipped) footprint zone to board coordinates: polygon points turned by the
    footprint's rotation (KiCad's ``RotatePoint``, y down) and moved to its position;
    copper layers the board does not have dropped, as KiCad does on load."""
    a = math.radians(rotation)
    c, s = math.cos(a), math.sin(a)
    for sub in zone.walk():
        if sub.head == "xy":
            atoms = sub.atoms()
            x, y = _num(atoms[0]), _num(atoms[1])
            sub.set_atom(0, number(round(at[0] + x * c + y * s, 6) + 0.0))
            sub.set_atom(1, number(round(at[1] - x * s + y * c, 6) + 0.0))
    layers = zone.child("layers")
    if layers is not None:
        keep = _board_layers(copper)
        layers.items = [
            i
            for i in layers.items
            if not (
                isinstance(i, Atom)
                and i.is_string
                and re.fullmatch(r"(F|B|In\d+)\.Cu", i.text)
                and i.text not in keep
            )
        ]


def place(
    lib_fp: Node,
    *,
    side: str,
    rotation: float,
    copper: int,
    origin: tuple[float, float] = (0.0, 0.0),
) -> list[Node]:
    """The items of a library footprint (a fresh copy) as a board stores them for a
    footprint on ``side`` (``F.Cu``/``B.Cu``) rotated by ``rotation`` degrees at
    ``origin`` (which only zones depend on)."""
    back = side == "B.Cu"
    out: list[Node] = []
    for item in lib_fp.children():
        if item.head in LIB_HEADER:
            continue
        it = item.copy()
        if it.head == "zone":
            if back:
                _flip_item(it, copper)
            _place_zone(it, origin, rotation, copper)
            out.append(it)
            continue
        is_pad = it.head == "pad"
        is_text = _texts(it)
        at = it.child("at") if (is_pad or is_text) else None
        base = 0.0
        if at is not None and len(at.atoms()) >= 3 and _is_number(at.atoms()[2]):
            base = _num(at.atoms()[2])
        if back and it.head != "model":
            _flip_item(it, copper)
            if is_text:
                _mirror_justify(it)
        if at is not None:
            if back:
                base = (180 - base) if is_text else -base
            _set_angle(at, base + rotation, keep_zero=is_text)
        out.append(it)
    return out


def _is_number(a: Atom) -> bool:
    try:
        float(a.raw)
    except ValueError:
        return False
    return True
