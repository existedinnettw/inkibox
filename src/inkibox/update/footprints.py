"""*Update Footprints from Library* (every footprint, update mode), and the footprint
exchange *Update PCB from Schematic* uses when a symbol names another footprint.

A board keeps a placed footprint in the footprint's own frame: geometry is local and
unrotated, only the angles of pads and texts are absolute (library angle plus the
footprint's rotation); a footprint on the back is mirrored top to bottom (``y`` negated,
``F.*`` layers become ``B.*``, pad angles negated, text angles ``180 - a``, texts
``(justify mirror)``). Zones are the exception: a board stores a footprint's zones in
board coordinates, on the board's copper layers only. :func:`place` turns a library
footprint into that form.

:func:`exchange` follows KiCad 10's ``PCB_EDIT_FRAME::ExchangeFootprint`` for the options
in :class:`~inkibox.update.options.FootprintOptions`: position, side, rotation, lock, links
to the schematic (path, sheet, filters), pad nets and pin data, Reference and Value, and
every other field's place and style stay with the board; text of other fields, fabrication
attributes, clearance overrides and 3D models come from the library as asked. Fields and
texts the library does not have are kept unless ``remove_extra_texts``.

A footprint whose update would only renumber uuids or reorder items is left alone, so a
board that is up to date is not rewritten.
"""

from __future__ import annotations

import math
import re

from ..kicad.sexpr import stable_uuid
from ..kicad.sfile import Atom, Node, SFile, node, number, string, symbol
from .libcache import LibraryCache, LibraryError
from .options import FootprintOptions
from .report import Report

# owned by the schematic (Update PCB), whatever the library footprint says
SCHEMATIC_ATTRS = ("dnp", "exclude_from_bom", "exclude_from_pos_files")
CLEARANCE = (
    "solder_mask_margin",
    "solder_paste_margin",
    "solder_paste_margin_ratio",
    "solder_paste_ratio",
    "clearance",
    "zone_connect",
    "thermal_width",
    "thermal_gap",
)
BOARD_LINKS = ("path", "sheetname", "sheetfile", "component_classes")
LIB_HEADER = ("version", "generator", "generator_version")
GRAPHICS = (
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
)
UNSUPPORTED_ITEMS = ("dimension", "fp_dimension")
KNOWN = frozenset(
    (
        *GRAPHICS,
        *CLEARANCE,
        *BOARD_LINKS,
        *LIB_HEADER,
        *UNSUPPORTED_ITEMS,
        "zone",
        "layer",
        "uuid",
        "at",
        "descr",
        "tags",
        "property",
        "attr",
        "pad",
        "group",
        "model",
        "embedded_fonts",
        "duplicate_pad_numbers_are_jumpers",
        "net_tie_pad_groups",
        "jumper_pad_groups",
        "private_layers",
    )
)


class ExchangeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- helpers


def ref_of(fp: Node) -> str:
    for p in fp.children("property"):
        if p.atom(0) == "Reference":
            return p.atom(1) or "?"
    return "?"


def fields(fp: Node) -> list[tuple[str, Node]]:
    """``(name, property)`` of the footprint's fields, in order (``ki_fp_filters`` and other
    unquoted-name properties excluded)."""
    return [
        (p.atom(0) or "", p)
        for p in fp.children("property")
        if p.atoms() and p.atoms()[0].is_string
    ]


def _num(a: Atom) -> float:
    return float(a.raw)


def _angle(value: float) -> Atom:
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


# --------------------------------------------------------------------------- placement


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
    a = _angle(angle)
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


# --------------------------------------------------------------------------- exchange


def exchange(
    existing: Node,
    lib_fp: Node,
    lib_id: str,
    opts: FootprintOptions,
    *,
    copper: int,
) -> Node:
    """The board footprint ``existing`` rebuilt from ``lib_fp`` (see the module doc)."""
    for bad in UNSUPPORTED_ITEMS:
        if lib_fp.child(bad) is not None:
            raise ExchangeError(
                f"library footprint {lib_id} has a {bad}; update it in KiCad"
            )
    side = existing.value("layer", "F.Cu") or "F.Cu"
    at = existing.child("at")
    rotation = _num(at.atoms()[2]) if at is not None and len(at.atoms()) >= 3 else 0.0
    pos = (_num(at.atoms()[0]), _num(at.atoms()[1])) if at is not None else (0.0, 0.0)
    placed = place(lib_fp, side=side, rotation=rotation, copper=copper, origin=pos)
    fp_uuid = existing.value("uuid") or ref_of(existing)

    lib_fields = [
        (p.atom(0) or "", p)
        for p in placed
        if p.head == "property" and p.atoms() and p.atoms()[0].is_string
    ]
    old_fields = fields(existing)
    old_by_name: dict[str, Node] = {}
    for name, p in old_fields:
        old_by_name.setdefault(name, p)
    new_fields: list[Node] = []
    for name, lp in lib_fields:
        old = old_by_name.pop(name, None)
        if old is None:
            new_fields.append(_with_uuid(lp, fp_uuid, "field", name))
            continue
        f = old
        if (
            opts.text_content
            and name not in ("Reference", "Value")
            and (old.atom(1) or "") != (lp.atom(1) or "")
        ):
            f = old.copy()
            f.set_atom(1, string(lp.atom(1) or ""))
        new_fields.append(f)
    if not opts.remove_extra_texts:
        new_fields += [
            p
            for name, p in old_fields
            if name in old_by_name and old_by_name[name] is p
        ]
    else:
        for name in ("Reference", "Value"):
            if name in old_by_name:
                new_fields.append(old_by_name[name])

    # graphic texts: an existing one with the same text keeps its place and style
    old_texts = [t for t in existing.children("fp_text")]
    graphics: list[Node] = []
    for item in placed:
        if item.head not in GRAPHICS:
            continue
        if item.head == "fp_text":
            match = next(
                (t for t in old_texts if _text_key(t) == _text_key(item)), None
            )
            if match is not None:
                old_texts.remove(match)
                graphics.append(match)
                continue
        graphics.append(item)
    if not opts.remove_extra_texts:
        graphics += old_texts

    pads = _pads(existing, [i for i in placed if i.head == "pad"], fp_uuid)

    def lib_child(head: str) -> Node | None:
        return next((i for i in placed if i.head == head), None)

    out = Node("footprint", [string(lib_id)])
    out.items += [a for a in existing.atoms()[1:]]  # locked, placed
    out.items.append(existing.child("layer") or node("layer", side))
    for head in ("uuid", "at"):
        c = existing.child(head)
        if c is not None:
            out.items.append(c)
    for head in ("descr", "tags"):
        c = lib_child(head)
        if c is not None:
            out.items.append(c)
    out.items += new_fields
    out.items += [
        p
        for p in existing.children("property")
        if not (p.atoms() and p.atoms()[0].is_string)
    ]
    for head in BOARD_LINKS:
        c = existing.child(head)
        if c is not None:
            out.items.append(c)
    out.items += [
        c for c in existing.children() if c.head not in KNOWN
    ]  # e.g. (locked yes)
    source = placed if opts.clearance_overrides else existing.children()
    out.items += [c for c in source if c.head in CLEARANCE]
    attr = _attr(existing, lib_child("attr"), opts)
    if attr is not None:
        out.items.append(attr)
    for head in (
        "duplicate_pad_numbers_are_jumpers",
        "net_tie_pad_groups",
        "jumper_pad_groups",
        "private_layers",
    ):
        c = lib_child(head)
        if c is not None:
            out.items.append(c)
    out.items += graphics
    out.items += pads
    out.items += _zones(existing, [i for i in placed if i.head == "zone"], fp_uuid)
    out.items += [i for i in placed if i.head == "group"]
    c = lib_child("embedded_fonts")
    if c is not None:
        out.items.append(c)
    out.items += (
        [i for i in placed if i.head == "model"]
        if opts.models_3d
        else existing.children("model")
    )
    return out


def _text_key(t: Node) -> tuple:
    return (t.atom(0), t.atom(1))


def _with_uuid(item: Node, *parts: str) -> Node:
    uid = item.child("uuid")
    if uid is not None:
        item.replace_child(uid, node("uuid", stable_uuid(*parts)))
    return item


# what KiCad writes after a pad's (net) (pinfunction) (pintype): the pad's own overrides,
# its custom shape and the uuid
_PAD_AFTER_NET = frozenset(
    (
        "die_length",
        "die_delay",
        *CLEARANCE,
        "thermal_bridge_width",
        "thermal_bridge_angle",
        "zone_layer_connections",
        "teardrops",
        "options",
        "primitives",
        "tenting",
        "uuid",
    )
)


def _pads(existing: Node, lib_pads: list[Node], fp_uuid: str) -> list[Node]:
    """Library pads with the board's nets and pin data, matched by pad number in order."""
    old: dict[str, list[Node]] = {}
    for p in existing.children("pad"):
        old.setdefault(p.atom(0) or "", []).append(p)
    out: list[Node] = []
    for i, p in enumerate(lib_pads):
        num = p.atom(0) or ""
        match = old.get(num, [])
        prev = match.pop(0) if match else None
        pad = p
        # net and pin data from the board, just before the uuid (where KiCad writes them)
        for head in ("net", "pinfunction", "pintype"):
            c = pad.child(head)
            if c is not None:
                pad.replace_child(c, None)
        carry = (
            [prev.child(h) for h in ("net", "pinfunction", "pintype")] if prev else []
        )
        carry = [c for c in carry if c is not None]
        uid = pad.child("uuid")
        index = next(
            (
                k
                for k, item in enumerate(pad.items)
                if isinstance(item, Node) and item.head in _PAD_AFTER_NET
            ),
            len(pad.items),
        )
        pad.items[index:index] = carry
        prev_uid = prev.child("uuid") if prev is not None else None
        uid_new = prev_uid or node("uuid", stable_uuid(fp_uuid, "pad", num, str(i)))
        if uid is not None:
            pad.replace_child(uid, uid_new)
        else:
            pad.items.append(uid_new)
        out.append(pad)
    return out


_ZONE_FILL = ("filled_polygon", "filled_areas_thickness")


def _zone_key(zone: Node) -> tuple:
    bare = zone.copy()
    bare.items = [
        i
        for i in bare.items
        if not (isinstance(i, Node) and i.head in ("uuid", *_ZONE_FILL))
    ]
    return _canon(bare)


def _zones(existing: Node, lib_zones: list[Node], fp_uuid: str) -> list[Node]:
    """Library zones, matched in order with the board's: an unchanged one stays as the
    board has it (fill included), a changed one keeps the board's uuid."""
    old = existing.children("zone")
    out: list[Node] = []
    for i, z in enumerate(lib_zones):
        prev = old[i] if i < len(old) else None
        if prev is not None and _zone_key(prev) == _zone_key(z):
            out.append(prev)
            continue
        uid = z.child("uuid")
        new_uid = (prev.child("uuid") if prev is not None else None) or node(
            "uuid", stable_uuid(fp_uuid, "zone", str(i))
        )
        if uid is not None:
            z.replace_child(uid, new_uid)
        else:
            z.items.append(new_uid)
        out.append(z)
    return out


def _attr(existing: Node, lib_attr: Node | None, opts: FootprintOptions) -> Node | None:
    old = existing.child("attr")
    if not opts.fabrication_attributes:
        return old
    kinds = [a.raw for a in lib_attr.atoms()] if lib_attr is not None else []
    kinds = [k for k in kinds if k not in SCHEMATIC_ATTRS]
    keep = (
        [a.raw for a in old.atoms() if a.raw in SCHEMATIC_ATTRS]
        if old is not None
        else []
    )
    if old is None and existing.child("path") is None and lib_attr is not None:
        keep = [a.raw for a in lib_attr.atoms() if a.raw in SCHEMATIC_ATTRS]
    tokens = kinds + keep
    if not tokens:
        return None
    new = node("attr", *[symbol(t) for t in tokens])
    if old is not None and [a.raw for a in old.atoms()] == tokens:
        return old
    return new


# --------------------------------------------------------------------------- compare


_ORDERLESS = frozenset((*GRAPHICS, "pad", "model", "property"))


def same_footprint(a: Node, b: Node) -> bool:
    """Equal but for uuids and the order of repeated items (KiCad re-sorts both)."""
    return _canon(a) == _canon(b)


def _canon(nd: Node) -> tuple:
    items: list = []
    repeated: list = []
    for i in nd.items:
        if isinstance(i, Atom):
            items.append(i.raw)
        elif i.head == "uuid" or i.head == "tstamp":
            continue
        elif i.head in _ORDERLESS:
            repeated.append(_canon(i))
        else:
            items.append(_canon(i))
    return (nd.head, tuple(items), tuple(sorted(repeated, key=repr)))


# --------------------------------------------------------------------------- update


def update_footprints(pcb: SFile, libs: LibraryCache, opts: FootprintOptions) -> Report:
    report = Report()
    copper = copper_layer_count(pcb.root)
    for fp in pcb.root.children("footprint"):
        lib_id = fp.atom(0) or ""
        ref = ref_of(fp)
        try:
            lib_fp = libs.footprint(lib_id)
            new = exchange(fp, lib_fp, lib_id, opts, copper=copper)
        except (LibraryError, ExchangeError) as exc:
            report.error(f"{ref}: {exc}")
            continue
        if same_footprint(new, fp):
            continue
        pcb.root.replace_child(fp, new)
        report.change(f"{ref}: {lib_id} updated from the library")
    return report
