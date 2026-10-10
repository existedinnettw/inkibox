"""*Update Footprints from Library* (every footprint, update mode), and the footprint
exchange *Update PCB from Schematic* uses when a symbol names another footprint.

:func:`exchange` follows KiCad 10's ``PCB_EDIT_FRAME::ExchangeFootprint`` for the options
in :class:`~inkibox.update.options.FootprintOptions`: position, side, rotation, lock, links
to the schematic (path, sheet, filters), pad nets and pin data, Reference and Value, and
every other field's place and style stay with the board; text of other fields, fabrication
attributes, clearance overrides and 3D models come from the library as asked. Fields and
texts the library does not have are kept unless ``remove_extra_texts``. The library
footprint is placed as the board stores it first (:mod:`~inkibox.update.placement`).

Every item taken from the library gets a uuid of its own footprint
(:mod:`~inkibox.update.uuids`); an item the board already has, unchanged, keeps the board's.
A footprint whose update would only renumber uuids or reorder items is left alone, so a
board that is up to date is not rewritten; one whose uuids other items of the board share
(as inkibox 0.5.0 and older left them) gets its own.
"""

from __future__ import annotations

from ..kicad.sfile import Atom, Node, SFile, node, string, symbol
from .libcache import LibraryCache, LibraryError
from .options import FootprintOptions
from .placement import LIB_HEADER, copper_layer_count, place
from .report import Report
from .uuids import duplicates, make_unique, rename_members, set_uuid, stamp, uuid_of

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
# pad numbering and layer options, as the library has them
PAD_GROUPS = (
    "duplicate_pad_numbers_are_jumpers",
    "net_tie_pad_groups",
    "jumper_pad_groups",
    "private_layers",
)
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
        *PAD_GROUPS,
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
    return [(p.atom(0) or "", p) for p in fp.children("property") if _is_field(p)]


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
    pos, rotation = _position(existing)
    placed = place(lib_fp, side=side, rotation=rotation, copper=copper, origin=pos)
    stamp(placed, existing.value("uuid") or ref_of(existing))
    # library item's uuid -> that of the board item standing for it
    kept: dict[str, str] = {}

    def lib_children(*heads: str) -> list[Node]:
        return _in_order(placed, heads)

    def board_children(*heads: str) -> list[Node]:
        return _in_order(existing.children(), heads)

    out = Node("footprint", [string(lib_id), *existing.atoms()[1:]])  # locked, placed
    out.items.append(existing.child("layer") or node("layer", side))
    out.items += board_children("uuid", "at")
    out.items += lib_children("descr", "tags")
    out.items += _fields(existing, lib_children("property"), opts, kept)
    out.items += [p for p in existing.children("property") if not _is_field(p)]
    out.items += board_children(*BOARD_LINKS)
    out.items += [c for c in existing.children() if c.head not in KNOWN]  # (locked yes)
    clearance = placed if opts.clearance_overrides else existing.children()
    out.items += [c for c in clearance if c.head in CLEARANCE]  # as the source has them
    attr = _attr(existing, next(iter(lib_children("attr")), None), opts)
    if attr is not None:
        out.items.append(attr)
    out.items += lib_children(*PAD_GROUPS)
    graphics = [i for i in placed if i.head in GRAPHICS]  # in the library's order
    out.items += _graphics(existing, graphics, opts, kept)
    out.items += _pads(existing, lib_children("pad"), kept)
    out.items += _zones(existing, lib_children("zone"), kept)
    groups = lib_children("group")
    rename_members(groups, kept)
    out.items += groups
    out.items += lib_children("embedded_fonts")
    out.items += lib_children("model") if opts.models_3d else board_children("model")
    return out


def _in_order(items: list[Node], heads: tuple[str, ...]) -> list[Node]:
    """The ``items`` with one of ``heads``, in the order of ``heads`` (KiCad's write order
    for the singular ones)."""
    return [i for h in heads for i in items if i.head == h]


def _position(fp: Node) -> tuple[tuple[float, float], float]:
    """``((x, y), rotation)`` of a board footprint."""
    at = fp.child("at")
    nums = [float(a.raw) for a in at.atoms()[:3]] if at is not None else []
    pos = (nums[0], nums[1]) if len(nums) >= 2 else (0.0, 0.0)
    return pos, nums[2] if len(nums) >= 3 else 0.0


def _is_field(p: Node) -> bool:
    return bool(p.atoms()) and p.atoms()[0].is_string


def _fields(
    existing: Node, lib_props: list[Node], opts: FootprintOptions, kept: dict[str, str]
) -> list[Node]:
    """The library's fields in its order: one the board has keeps the board's place and
    style (and text for Reference and Value, or unless ``text_content``); then the board's
    other fields unless ``remove_extra_texts`` (Reference and Value always stay)."""
    old_fields = fields(existing)
    old_by_name: dict[str, Node] = {}
    for name, p in old_fields:
        old_by_name.setdefault(name, p)
    out: list[Node] = []
    for lp in filter(_is_field, lib_props):
        name = lp.atom(0) or ""
        old = old_by_name.pop(name, None)
        if old is None:
            out.append(lp)
            continue
        _keep_uuid(lp, old, kept)
        if (
            opts.text_content
            and name not in ("Reference", "Value")
            and (old.atom(1) or "") != (lp.atom(1) or "")
        ):
            old = old.copy()
            old.set_atom(1, string(lp.atom(1) or ""))
        out.append(old)
    if not opts.remove_extra_texts:
        return out + [p for name, p in old_fields if old_by_name.get(name) is p]
    return out + [old_by_name[n] for n in ("Reference", "Value") if n in old_by_name]


def _graphics(
    existing: Node, lib_items: list[Node], opts: FootprintOptions, kept: dict[str, str]
) -> list[Node]:
    """The library's graphics; the board's copy of one stays as the board has it (its
    uuid included): a graphic text with the same text keeps its place and style, any
    other graphic the same shape. The board's other graphic texts stay unless
    ``remove_extra_texts``; its other graphics go."""
    old_texts = existing.children("fp_text")
    old_shapes = [
        g for g in existing.children() if g.head in GRAPHICS and g.head != "fp_text"
    ]
    out: list[Node] = []
    for item in lib_items:
        if item.head == "fp_text":
            match = next(
                (t for t in old_texts if _text_key(t) == _text_key(item)), None
            )
            pool = old_texts
        else:
            key = _canon(item)
            match = next((g for g in old_shapes if _canon(g) == key), None)
            pool = old_shapes
        if match is None:
            out.append(item)
            continue
        pool.remove(match)
        _keep_uuid(item, match, kept)
        out.append(match)
    if not opts.remove_extra_texts:
        out += old_texts
    return out


def _keep_uuid(lib_item: Node, board_item: Node, kept: dict[str, str]) -> None:
    """Note that ``board_item``, with its uuid, stands for ``lib_item``."""
    new, old = uuid_of(lib_item), uuid_of(board_item)
    if new is not None and old is not None:
        kept[new] = old


def _adopt_uuid(lib_item: Node, board_item: Node) -> None:
    """``lib_item`` takes the uuid of ``board_item``, if that has one."""
    uid = uuid_of(board_item)
    if uid is not None:
        set_uuid(lib_item, uid)


def _text_key(t: Node) -> tuple:
    return (t.atom(0), t.atom(1))


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
PIN_DATA = ("net", "pinfunction", "pintype")


def _pads(existing: Node, lib_pads: list[Node], kept: dict[str, str]) -> list[Node]:
    """Library pads with the board's nets, pin data and uuids, matched by pad number in
    order."""
    old: dict[str, list[Node]] = {}
    for p in existing.children("pad"):
        old.setdefault(p.atom(0) or "", []).append(p)
    for pad in lib_pads:
        match = old.get(pad.atom(0) or "", [])
        prev = match.pop(0) if match else None
        for c in pad.children():
            if c.head in PIN_DATA:
                pad.replace_child(c, None)
        if prev is None:
            continue
        # net and pin data from the board, before the overrides (where KiCad writes them)
        carry = [c for c in (prev.child(h) for h in PIN_DATA) if c is not None]
        index = next(
            (
                k
                for k, item in enumerate(pad.items)
                if isinstance(item, Node) and item.head in _PAD_AFTER_NET
            ),
            len(pad.items),
        )
        pad.items[index:index] = carry
        _keep_uuid(pad, prev, kept)
        _adopt_uuid(pad, prev)
    return lib_pads


_ZONE_FILL = ("filled_polygon", "filled_areas_thickness")


def _zone_key(zone: Node) -> tuple:
    bare = zone.copy()
    bare.items = [
        i
        for i in bare.items
        if not (isinstance(i, Node) and i.head in ("uuid", *_ZONE_FILL))
    ]
    return _canon(bare)


def _zones(existing: Node, lib_zones: list[Node], kept: dict[str, str]) -> list[Node]:
    """Library zones, matched in order with the board's: an unchanged one stays as the
    board has it (fill included), a changed one keeps the board's uuid."""
    old = existing.children("zone")
    out: list[Node] = []
    for i, z in enumerate(lib_zones):
        prev = old[i] if i < len(old) else None
        if prev is not None:
            _keep_uuid(z, prev, kept)
            if _zone_key(prev) == _zone_key(z):
                out.append(prev)
                continue
            _adopt_uuid(z, prev)
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
    shared = duplicates(pcb.root)
    for fp in pcb.root.children("footprint"):
        lib_id = fp.atom(0) or ""
        ref = ref_of(fp)
        try:
            lib_fp = libs.footprint(lib_id)
            new = exchange(fp, lib_fp, lib_id, opts, copper=copper)
        except (LibraryError, ExchangeError) as exc:
            report.error(f"{ref}: {exc}")
            continue
        if not same_footprint(new, fp):
            make_unique(new, shared)
            pcb.root.replace_child(fp, new)
            report.change(f"{ref}: {lib_id} updated from the library")
        elif make_unique(fp, shared):  # in place: up to date but for its uuids
            report.change(f"{ref}: uuids shared with other items made unique")
    return report
