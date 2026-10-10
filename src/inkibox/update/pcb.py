"""*Update PCB from Schematic* on a board file.

Follows KiCad 10's ``BOARD_NETLIST_UPDATER`` with footprints linked to symbols by UUID
path (never re-linked by reference): for every component of the netlist

* a footprint the board lacks is added from the library (below the board outline, for
  the designer to place), one whose symbol names another footprint is exchanged
  (:func:`~inkibox.update.footprints.exchange`, keeping place, side and board text);
* Reference, Value, Datasheet, Description and user fields take the symbol's text; a field
  the footprint lacks is added hidden on the Fab layer at the footprint origin; the
  footprint filters, sheet name and file follow the symbol;
* DNP, exclude-from-BOM and exclude-from-position-files follow the symbol;
* every pad takes the net, pin function and pin type of its symbol pin (none when the
  symbol has no such pin).

Tracks, vias and zones on a net the schematic renamed move to the new name when every pad
of the old net went to that one net; otherwise they are reported. Footprints no symbol
has are reported, or deleted with ``delete_unused_footprints``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..kicad.sexpr import stable_uuid
from ..kicad.sfile import Node, SFile, node, string, symbol
from .footprints import SCHEMATIC_ATTRS, ExchangeError, exchange, ref_of
from .libcache import LibraryCache, LibraryError
from .netlist import Component
from .options import FootprintOptions, PcbOptions
from .placement import absolute_angle, copper_layer_count, place
from .report import Report

ATTR_ORDER = (
    "smd",
    "through_hole",
    "board_only",
    "exclude_from_pos_files",
    "exclude_from_bom",
    "allow_missing_courtyard",
    "dnp",
)
BOARD_FIELDS_FIRST = ("Reference", "Value", "Datasheet", "Description")
NET_ITEMS = ("segment", "arc", "via", "zone")


# --------------------------------------------------------------------------- small helpers


def _prop(fp: Node, name: str) -> Node | None:
    for p in fp.children("property"):
        atoms = p.atoms()
        if atoms and atoms[0].is_string and atoms[0].text == name:
            return p
    return None


def _set_link(fp: Node, head: str, value: str) -> bool:
    cur = fp.child(head)
    if cur is not None and cur.atom(0) == value:
        return False
    new = node(head, value)
    if cur is not None:
        fp.replace_child(cur, new)
    else:
        anchor = _last(fp, ("path", "sheetname", "property"))
        fp.items.insert(
            fp.items.index(anchor) + 1 if anchor is not None else len(fp.items), new
        )
    return True


def _last(fp: Node, heads: tuple[str, ...]) -> Node | None:
    found = None
    for c in fp.children():
        if c.head in heads:
            found = c
    return found


def fab_text_style(project_dir: Path) -> tuple[float, float, float]:
    """``(size_h, size_v, thickness)`` of new Fab-layer text from the board setup."""
    pro = next(iter(sorted(project_dir.glob("*.kicad_pro"))), None)
    try:
        d = (
            json.loads(pro.read_text(encoding="utf-8"))["board"]["design_settings"][
                "defaults"
            ]
            if pro
            else {}
        )
    except (OSError, ValueError, KeyError):
        d = {}
    return (
        float(d.get("fab_text_size_h", 1.0)),
        float(d.get("fab_text_size_v", 1.0)),
        float(d.get("fab_text_thickness", 0.15)),
    )


def _new_field(
    fp: Node, name: str, value: str, style: tuple[float, float, float]
) -> Node:
    back = fp.value("layer", "F.Cu") == "B.Cu"
    at = fp.child("at")
    rot = at.atoms()[2].raw if at is not None and len(at.atoms()) >= 3 else "0"
    uid = fp.value("uuid") or ref_of(fp)
    effects = node(
        "effects",
        node("font", node("size", style[0], style[1]), node("thickness", style[2])),
    )
    if back:
        effects.items.append(node("justify", symbol("mirror")))
    return node(
        "property",
        name,
        value,
        node("at", 0, 0, absolute_angle(float(rot))),
        node("unlocked", symbol("yes")),
        node("layer", "B.Fab" if back else "F.Fab"),
        node("hide", symbol("yes")),
        node("uuid", stable_uuid(str(uid), "field", name)),
        effects,
    )


# --------------------------------------------------------------------------- update


def update_pcb(
    pcb: SFile,
    comps: dict[str, Component],
    attrs: dict[str, set[str]],
    libs: LibraryCache,
    opts: PcbOptions,
    fp_opts: FootprintOptions,
    project_dir: Path,
) -> Report:
    """``attrs``: schematic-owned attribute tokens per footprint path (``dnp`` …)."""
    report = Report()
    root = pcb.root
    copper = copper_layer_count(root)
    style = fab_text_style(project_dir)
    by_path: dict[str, Node] = {}
    for fp in root.children("footprint"):
        p = fp.child("path")
        if p is not None and p.atom(0):
            by_path[p.atom(0) or ""] = fp
    old_nets = _pad_nets(root)
    matched: set[int] = set()
    new_spot = _new_footprint_spot(root)

    unlinked = {
        ref_of(fp): fp for fp in root.children("footprint") if not fp.value("path")
    }
    for comp in comps.values():
        fp = next((by_path[p] for p in comp.paths() if p in by_path), None)
        if fp is None and opts.link_unlinked and comp.ref in unlinked:
            fp = unlinked.pop(comp.ref)
            report.change(
                f"{comp.ref}: unlinked footprint linked to its symbol by reference"
            )
        if fp is None:
            fp = _add_footprint(root, comp, libs, copper, next(new_spot), report)
            if fp is None:
                continue
        elif fp.atom(0) != comp.footprint and comp.footprint:
            if not opts.replace_footprints:
                report.warn(
                    f"{comp.ref}: footprint {fp.atom(0)} kept (symbol has {comp.footprint})"
                )
            else:
                try:
                    new = exchange(
                        fp,
                        libs.footprint(comp.footprint),
                        comp.footprint,
                        fp_opts,
                        copper=copper,
                    )
                except (LibraryError, ExchangeError) as exc:
                    report.error(f"{comp.ref}: {exc}")
                else:
                    root.replace_child(fp, new)
                    report.change(
                        f"{comp.ref}: footprint {fp.atom(0)} -> {comp.footprint}"
                    )
                    fp = new
        matched.add(id(fp))
        _update_fields(fp, comp, opts, style, libs, report)
        _update_links(fp, comp, report)
        _update_attrs(fp, attrs.get(comp.path, set()), comp.ref, report)
        _update_pads(fp, comp, report)

    for fp in list(root.children("footprint")):
        if id(fp) in matched:
            continue
        attr = fp.child("attr")
        if attr is not None and "board_only" in [a.raw for a in attr.atoms()]:
            continue
        if opts.delete_unused_footprints:
            root.replace_child(fp, None)
            report.change(f"{ref_of(fp)}: deleted (no symbol)")
        else:
            report.warn(f"{ref_of(fp)}: no symbol in the schematic (kept)")
    _follow_renamed_nets(root, old_nets, report)
    return report


def _pad_nets(root: Node) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for fp in root.children("footprint"):
        uid = fp.value("uuid") or ref_of(fp)
        for i, pad in enumerate(fp.children("pad")):
            net = pad.child("net")
            if net is not None:
                out[(str(uid), f"{pad.atom(0)}#{i}")] = net.atoms()[-1].text
    return out


def _new_footprint_spot(root: Node):
    """Places for new footprints: a row below the board outline, 5 mm apart."""
    xs, ys = [], []
    for item in root.children():
        if item.head.startswith("gr_") and (item.value("layer") == "Edge.Cuts"):
            for sub in item.walk():
                if (
                    sub.head in ("start", "end", "xy", "center", "mid")
                    and len(sub.atoms()) >= 2
                ):
                    xs.append(float(sub.atoms()[0].raw))
                    ys.append(float(sub.atoms()[1].raw))
    x0, y0 = (min(xs), max(ys) + 10) if xs else (0.0, 0.0)
    i = 0
    while True:
        yield (round(x0 + 5 * i, 4), round(y0, 4))
        i += 1


def _add_footprint(
    root: Node,
    comp: Component,
    libs: LibraryCache,
    copper: int,
    spot: tuple[float, float],
    report: Report,
) -> Node | None:
    if not comp.footprint:
        report.error(f"{comp.ref}: symbol has no footprint; not added to the board")
        return None
    try:
        lib_fp = libs.footprint(comp.footprint)
    except LibraryError as exc:
        report.error(f"{comp.ref}: {exc}")
        return None
    items = place(lib_fp, side="F.Cu", rotation=0, copper=copper)
    uid = stable_uuid("footprint", comp.path)
    fp = Node("footprint", [string(comp.footprint)])
    fp.items.append(
        next((i for i in items if i.head == "layer"), node("layer", "F.Cu"))
    )
    fp.items.append(node("uuid", uid))
    fp.items.append(node("at", spot[0], spot[1]))
    fp.items += [i for i in items if i.head != "layer"]
    for n, item in enumerate(fp.walk()):
        u = item.child("uuid") if item is not fp else None
        if u is not None:
            item.replace_child(u, node("uuid", stable_uuid(uid, "item", str(n))))
    for n, prop in enumerate(fp.children("property")):
        if prop.atom(0) == "Reference":
            prop.set_atom(1, string(comp.ref))
    root.items.insert(_footprint_index(root), fp)
    report.change(
        f"{comp.ref}: added {comp.footprint} at ({spot[0]}, {spot[1]}); place it"
    )
    return fp


def _footprint_index(root: Node) -> int:
    last = None
    for i, item in enumerate(root.items):
        if isinstance(item, Node) and item.head == "footprint":
            last = i
    if last is not None:
        return last + 1
    for i, item in enumerate(root.items):
        if isinstance(item, Node) and item.head not in (
            "version",
            "generator",
            "generator_version",
            "general",
            "paper",
            "title_block",
            "layers",
            "setup",
            "property",
            "net",
        ):
            return i
    return len(root.items)


def _update_fields(
    fp: Node,
    comp: Component,
    opts: PcbOptions,
    style: tuple[float, float, float],
    libs: LibraryCache,
    report: Report,
) -> None:
    want = {"Reference": comp.ref, "Value": comp.value, **comp.fields}
    for name, value in want.items():
        prop = _prop(fp, name)
        if prop is None:
            new = _new_field(fp, name, value, style)
            anchor = _last(fp, ("property",))
            fp.items.insert(
                fp.items.index(anchor) + 1 if anchor is not None else 0, new
            )
            report.change(f"{comp.ref}: field {name!r} added ({value!r})")
        elif (prop.atom(1) or "") != value:
            report.change(f"{comp.ref}: {name} {prop.atom(1)!r} -> {value!r}")
            prop.set_atom(1, string(value))
    if opts.remove_extra_fields:
        lib_names: set[str] = set()
        try:
            lib_names = {
                p.atom(0) or ""
                for p in libs.footprint(fp.atom(0) or "").children("property")
            }
        except LibraryError:
            pass
        for p in list(fp.children("property")):
            atoms = p.atoms()
            if not atoms or not atoms[0].is_string:
                continue
            name = atoms[0].text
            if name in want or name in BOARD_FIELDS_FIRST or name in lib_names:
                continue
            fp.replace_child(p, None)
            report.change(f"{comp.ref}: field {name!r} removed (no symbol has it)")
    # footprint filters: an unquoted-name property
    filters = comp.properties.get("ki_fp_filters")
    cur = next(
        (
            p
            for p in fp.children("property")
            if p.atoms() and p.atoms()[0].raw == "ki_fp_filters"
        ),
        None,
    )
    if filters:
        new = Node("property", [symbol("ki_fp_filters"), string(filters)])
        if cur is None:
            anchor = _last(fp, ("property",))
            fp.items.insert(
                fp.items.index(anchor) + 1 if anchor is not None else 0, new
            )
            report.change(f"{comp.ref}: footprint filters {filters!r}")
        elif cur.atom(1) != filters:
            fp.replace_child(cur, new)
            report.change(f"{comp.ref}: footprint filters -> {filters!r}")
    elif cur is not None:
        fp.replace_child(cur, None)
        report.change(f"{comp.ref}: footprint filters removed")


def _update_links(fp: Node, comp: Component, report: Report) -> None:
    changed = []
    if _set_link(fp, "path", comp.path):
        changed.append("path")
    if _set_link(fp, "sheetname", comp.sheet_names):
        changed.append("sheet name")
    sheetfile = comp.properties.get("Sheetfile", "")
    if sheetfile and _set_link(fp, "sheetfile", sheetfile):
        changed.append("sheet file")
    if changed:
        report.change(f"{comp.ref}: {', '.join(changed)} updated")


def _update_attrs(fp: Node, owned: set[str], ref: str, report: Report) -> None:
    attr = fp.child("attr")
    tokens = [a.raw for a in attr.atoms()] if attr is not None else []
    new = [t for t in tokens if t not in SCHEMATIC_ATTRS] + [
        t for t in SCHEMATIC_ATTRS if t in owned
    ]
    new = sorted(
        new, key=lambda t: ATTR_ORDER.index(t) if t in ATTR_ORDER else len(ATTR_ORDER)
    )
    if new == tokens:
        return
    report.change(
        f"{ref}: attributes {' '.join(tokens) or '-'} -> {' '.join(new) or '-'}"
    )
    replacement = node("attr", *[symbol(t) for t in new]) if new else None
    if attr is not None:
        fp.replace_child(attr, replacement)
    elif replacement is not None:
        anchor = _last(fp, ("sheetfile", "sheetname", "path", "property"))
        fp.items.insert(
            fp.items.index(anchor) + 1 if anchor is not None else len(fp.items),
            replacement,
        )


def _update_pads(fp: Node, comp: Component, report: Report) -> None:
    changed: list[str] = []
    for pad in fp.children("pad"):
        num = pad.atom(0) or ""
        pin = comp.pins.get(num) if num else None
        want: list[Node] = []
        if pin is not None:
            want.append(node("net", pin.net))
            if pin.function:
                want.append(node("pinfunction", pin.function))
            if pin.type:
                want.append(node("pintype", pin.type))
        have = [
            c for c in pad.children() if c.head in ("net", "pinfunction", "pintype")
        ]
        if [c.key() for c in have] == [c.key() for c in want]:
            continue
        old_net = next((c.atoms()[-1].text for c in have if c.head == "net"), None)
        for c in have:
            pad.replace_child(c, None)
        uid = pad.child("uuid")
        index = pad.items.index(uid) if uid is not None else len(pad.items)
        pad.items[index:index] = want
        new_net = pin.net if pin is not None else None
        if old_net != new_net:
            changed.append(f"{num} {old_net or '-'} -> {new_net or '-'}")
        else:
            changed.append(f"{num} pin data")
    if changed:
        report.change(f"{comp.ref}: pads {'; '.join(changed)}")


def _follow_renamed_nets(
    root: Node, old: dict[tuple[str, str], str], report: Report
) -> None:
    new = _pad_nets(root)
    pad_nets = set(new.values())
    mapping: dict[str, set[str | None]] = {}
    for key, net in old.items():
        mapping.setdefault(net, set()).add(new.get(key))
    moved: dict[tuple[str, str], int] = {}
    stranded: dict[str, int] = {}
    for item in root.children():
        if item.head not in NET_ITEMS:
            continue
        net = item.child("net")
        if net is None or not net.atoms():
            continue
        name = net.atoms()[-1].text
        if not name or name in pad_nets:
            continue
        targets = mapping.get(name, set()) - {None}
        if len(targets) == 1:
            target = str(next(iter(targets)))
            item.replace_child(net, node("net", target))
            moved[(name, str(target))] = moved.get((name, str(target)), 0) + 1
        else:
            stranded[name] = stranded.get(name, 0) + 1
    for (name, target), n in sorted(moved.items()):
        report.change(
            f"net {name!r} renamed {target!r}: {n} track(s), via(s) or zone(s) follow"
        )
    for name, n in sorted(stranded.items()):
        targets = sorted(t for t in mapping.get(name, set()) if t)
        report.warn(
            f"{n} track(s), via(s) or zone(s) on net {name!r}, which no pad has any more"
            + (f" (its pads went to {', '.join(targets)})" if targets else "")
        )
