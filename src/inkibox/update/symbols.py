"""*Update Symbols from Library* on a schematic file (every symbol, update mode).

Follows KiCad 10's ``DIALOG_CHANGE_SYMBOLS::processSymbols`` for the options in
:class:`~inkibox.update.options.SymbolOptions`:

* shape and pins: the embedded copy in ``lib_symbols`` becomes the library symbol
  (flattened, as KiCad writes it: see :mod:`inkibox.update.libcache`); a locally renamed
  copy (``lib_name``) goes back to the library's; a placed symbol lists every pin number
  of every unit, existing pins keep their uuid and alternate function;
* keywords: the copy's ``ki_keywords`` / ``ki_fp_filters``;
* fields: text from the library per the field options; library fields a placed symbol
  lacks are always added (placed where the library puts them, transformed like the body);
* attributes, alternate pins, custom power values when asked.

Embedded copies no placed symbol uses any more are dropped, as KiCad does on save.
"""

from __future__ import annotations

import math
import re

from ..kicad.sexpr import stable_uuid
from ..kicad.sfile import Node, SFile, node, string, symbol
from .libcache import LibraryCache, LibraryError
from .options import SymbolOptions
from .report import Report

MANDATORY = ("Reference", "Value", "Footprint", "Datasheet", "Description")
ATTRIBUTES = ("exclude_from_sim", "in_bom", "on_board", "in_pos_files")
KEYWORD_FIELDS = ("ki_keywords", "ki_fp_filters")


# --------------------------------------------------------------------------- fields


def field_name(prop: Node) -> str | None:
    """A property's name; ``None`` for a private one (``(property private "n" "v")``),
    which stays in the library."""
    atoms = prop.atoms()
    if not atoms or not atoms[0].is_string:
        return None
    return atoms[0].text


def field_value(prop: Node) -> str:
    return prop.atom(1) or ""


def fields_of(nd: Node) -> dict[str, Node]:
    return {n: p for p in nd.children("property") if (n := field_name(p)) is not None}


def set_value(prop: Node, value: str) -> None:
    prop.set_atom(1, string(value))


def is_power(lib: Node) -> bool:
    return lib.child("power") is not None


def lib_pin_numbers(lib: Node) -> list[str]:
    """Every pin number of every unit and body style, in library order, once each."""
    seen: dict[str, None] = {}
    for unit in lib.children("symbol"):
        for pin in unit.children("pin"):
            num = pin.child("number")
            if num is not None and num.atom(0) is not None:
                seen.setdefault(num.atom(0) or "", None)
    return list(seen)


def _placed_at(inst: Node) -> tuple[float, float, float, str | None]:
    at = inst.child("at")
    vals = [float(a.raw) for a in at.atoms()] if at is not None else []
    vals += [0.0] * (3 - len(vals))
    mirror = inst.child("mirror")
    return vals[0], vals[1], vals[2], mirror.atom(0) if mirror is not None else None


def _to_sheet(inst: Node, lx: float, ly: float) -> tuple[float, float]:
    """Library coordinates (y up) of a point to sheet coordinates (y down) of the placed
    symbol: KiCad's symbol TRANSFORM (mirror, then rotation counter-clockwise)."""
    x, y, rot, mirror = _placed_at(inst)
    px, py = lx, -ly
    if mirror == "x":
        py = -py
    elif mirror == "y":
        px = -px
    a = math.radians(rot)
    rx = px * math.cos(a) + py * math.sin(a)
    ry = -px * math.sin(a) + py * math.cos(a)
    return round(x + rx, 4), round(y + ry, 4)


def _new_field(inst: Node, lib_prop: Node) -> Node:
    """A library field as a placed symbol's field: position transformed like the body."""
    prop = lib_prop.copy()
    at = prop.child("at")
    if at is not None:
        vals = [float(a.raw) for a in at.atoms()] + [0.0, 0.0, 0.0]
        sx, sy = _to_sheet(inst, vals[0], vals[1])
        rot = _placed_at(inst)[2]
        angle = (vals[2] + (90 if rot in (90, 270) else 0)) % 180
        prop.replace_child(
            at, node("at", sx, sy, int(angle) if angle == int(angle) else angle)
        )
    return prop


# --------------------------------------------------------------------------- update


def update_symbols(sch: SFile, libs: LibraryCache, opts: SymbolOptions) -> Report:
    report = Report()
    root = sch.root
    lib_symbols = root.child("lib_symbols")
    if lib_symbols is None:
        lib_symbols = node("lib_symbols")
        root.items.insert(_after(root, ("paper", "title_block", "uuid")), lib_symbols)
    embedded: dict[str, Node] = {
        s.atom(0) or "": s for s in lib_symbols.children("symbol")
    }
    fresh: dict[str, Node] = {}  # library symbols read this run, by lib_id
    failed: set[str] = set()

    def library(lib_id: str) -> Node | None:
        if lib_id in failed:
            return None
        if lib_id not in fresh:
            try:
                fresh[lib_id] = libs.symbol(lib_id)
            except LibraryError as exc:
                failed.add(lib_id)
                report.error(str(exc))
                return None
        return fresh[lib_id]

    used: set[str] = set()
    for inst in root.children("symbol"):
        lib_id = inst.value("lib_id")
        lib_name_node = inst.child("lib_name")
        ref = _reference(inst)
        lib = library(lib_id)
        if lib is None:
            used.add(inst.value("lib_name") or lib_id)
            continue
        if opts.shape_and_pins and lib_name_node is not None:
            report.change(
                f"{ref}: embedded copy {lib_name_node.atom(0)!r} replaced by {lib_id}"
            )
            inst.replace_child(lib_name_node, None)
            lib_name_node = None
        emb_name = inst.value("lib_name") or lib_id
        used.add(emb_name)
        _update_embedded(lib_symbols, embedded, emb_name, lib, opts, report)
        if opts.shape_and_pins:
            _update_pins(inst, lib, ref, report)
        _update_fields(inst, lib, ref, opts, report)
        if opts.attributes:
            _update_attributes(inst, lib, ref, report)
        if opts.alternate_pins:
            for pin in inst.children("pin"):
                alt = pin.child("alternate")
                if alt is not None:
                    pin.replace_child(alt, None)
                    report.change(
                        f"{ref}: pin {pin.atom(0)} alternate {alt.atom(0)!r} reset"
                    )
    for name, emb in list(embedded.items()):
        if name not in used:
            lib_symbols.replace_child(emb, None)
            report.change(f"embedded {name}: removed (no symbol uses it)")
    return report


def _after(root: Node, heads: tuple[str, ...]) -> int:
    idx = 0
    for i, item in enumerate(root.items):
        if isinstance(item, Node) and item.head in heads:
            idx = i + 1
    return idx


def _reference(inst: Node) -> str:
    prop = fields_of(inst).get("Reference")
    return field_value(prop) if prop is not None else "?"


def _update_embedded(
    lib_symbols: Node,
    embedded: dict[str, Node],
    name: str,
    lib: Node,
    opts: SymbolOptions,
    report: Report,
) -> None:
    old = embedded.get(name)
    if old is None:
        new = lib.copy()
        new.set_atom(0, string(name))
        names = sorted([*embedded, name])
        pos = names.index(name)
        after = embedded[names[pos - 1]] if pos > 0 else None
        index = (
            lib_symbols.items.index(after) + 1
            if after is not None
            else len(lib_symbols.atoms())
        )
        lib_symbols.items.insert(index, new)
        embedded[name] = new
        report.change(f"embedded {name}: added from the library")
        return
    if opts.shape_and_pins:
        new = lib.copy()
        new.set_atom(0, string(name))
        if not opts.keywords:  # keep the design's keywords and filters
            _copy_fields(old, new, KEYWORD_FIELDS)
    elif opts.keywords:
        new = old.copy()
        _copy_fields(lib, new, KEYWORD_FIELDS)
    else:
        return
    if new.key() != old.key():
        lib_symbols.replace_child(old, new)
        embedded[name] = new
        report.change(f"embedded {name}: updated from the library")


def _copy_fields(src: Node, dst: Node, names: tuple[str, ...]) -> None:
    have = fields_of(dst)
    for name, prop in fields_of(src).items():
        if name not in names:
            continue
        if name in have:
            dst.replace_child(have[name], prop.copy())
        else:
            dst.items.append(prop.copy())
    for name in names:
        if name in have and name not in fields_of(src):
            dst.replace_child(have[name], None)


def _update_pins(inst: Node, lib: Node, ref: str, report: Report) -> None:
    want = lib_pin_numbers(lib)
    pins = inst.children("pin")
    have = {p.atom(0) or "": p for p in pins}
    if set(have) == set(want):
        return  # KiCad's own order is not reproducible and does not matter
    uid = inst.child("uuid")
    base = uid.atom(0) if uid is not None else ref
    for num, pin in have.items():
        if num not in want:
            inst.replace_child(pin, None)
    added = [n for n in want if n not in have]
    anchor = inst.children("pin")
    index = (
        inst.items.index(anchor[-1]) + 1
        if anchor
        else next(
            (
                i
                for i, it in enumerate(inst.items)
                if isinstance(it, Node) and it.head == "instances"
            ),
            len(inst.items),
        )
    )
    for offset, num in enumerate(added):
        inst.items.insert(
            index + offset,
            node("pin", num, node("uuid", stable_uuid(str(base), "pin", num))),
        )
    gone = sorted(set(have) - set(want))
    report.change(
        f"{ref}: pins"
        + (f" added {', '.join(added)}" if added else "")
        + (f" removed {', '.join(str(g) for g in gone)}" if gone else "")
    )


def _update_fields(
    inst: Node, lib: Node, ref: str, opts: SymbolOptions, report: Report
) -> None:
    have = fields_of(inst)
    lib_fields = fields_of(lib)
    skip = set()
    if not opts.update_references:
        skip.add("Reference")
    if not opts.update_values:
        skip.add("Value")
    power = is_power(lib)
    for name, prop in have.items():
        if name in skip or name.startswith("ki_"):
            continue
        if name not in lib_fields:
            if opts.remove_extra_fields and name not in MANDATORY:
                inst.replace_child(prop, None)
                report.change(
                    f"{ref}: field {name!r} removed (not in the library symbol)"
                )
            continue
        want = field_value(lib_fields[name])
        if not (opts.reset_empty_fields if want == "" else opts.field_text):
            continue
        cur = field_value(prop)
        if name == "Reference":
            prefix = re.match(r"[^0-9?]*", want)
            want = re.sub(r"^[^0-9?]*", prefix.group(0) if prefix else "", cur)
        if name == "Value" and power and not opts.custom_power:
            continue
        if cur != want:
            set_value(prop, want)
            report.change(f"{ref}: {name} {cur!r} -> {want!r}")
    last = have.get(list(have)[-1]) if have else None
    for name, lib_prop in lib_fields.items():
        if name in MANDATORY or name.startswith("ki_") or name in have:
            continue
        new = _new_field(inst, lib_prop)
        index = inst.items.index(last) + 1 if last is not None else len(inst.items)
        inst.items.insert(index, new)
        last = new
        report.change(f"{ref}: field {name!r} added ({field_value(lib_prop)!r})")


def _update_attributes(inst: Node, lib: Node, ref: str, report: Report) -> None:
    for attr in ATTRIBUTES:
        default = "no" if attr == "exclude_from_sim" else "yes"
        lib_node = lib.child(attr)
        want = lib_node.atom(0) if lib_node is not None else default
        cur_node = inst.child(attr)
        cur = cur_node.atom(0) if cur_node is not None else default
        if cur != want:
            new = node(attr, symbol(want or default))
            if cur_node is not None:
                inst.replace_child(cur_node, new)
            else:
                uid = inst.child("uuid")
                inst.items.insert(
                    inst.items.index(uid) if uid is not None else len(inst.items), new
                )
            report.change(f"{ref}: {attr} {cur} -> {want}")
