"""Copper layers and the board stackup: the layer table a ``.kicad_pcb`` lists, and the
``(stackup …)`` node KiCad's Board Setup writes."""

from __future__ import annotations

from .sexpr import Node, S, atom_text, child, children, num

LAYERS: list[tuple[int, str, str, str | None]] = [
    (0, "F.Cu", "signal", None),
    (2, "B.Cu", "signal", None),
    (9, "F.Adhes", "user", "F.Adhesive"),
    (11, "B.Adhes", "user", "B.Adhesive"),
    (13, "F.Paste", "user", None),
    (15, "B.Paste", "user", None),
    (5, "F.SilkS", "user", "F.Silkscreen"),
    (7, "B.SilkS", "user", "B.Silkscreen"),
    (1, "F.Mask", "user", None),
    (3, "B.Mask", "user", None),
    (17, "Dwgs.User", "user", "User.Drawings"),
    (19, "Cmts.User", "user", "User.Comments"),
    (21, "Eco1.User", "user", "User.Eco1"),
    (23, "Eco2.User", "user", "User.Eco2"),
    (25, "Edge.Cuts", "user", None),
    (27, "Margin", "user", None),
    (31, "F.CrtYd", "user", "F.Courtyard"),
    (29, "B.CrtYd", "user", "B.Courtyard"),
    (35, "F.Fab", "user", None),
    (33, "B.Fab", "user", None),
]


def inner_layers(copper_layers: int) -> list[str]:
    return [f"In{i}.Cu" for i in range(1, copper_layers - 1)]


def check_copper_layers(copper_layers: int) -> None:
    if copper_layers < 2 or copper_layers > 32 or copper_layers % 2:
        raise ValueError(
            f"copper_layers must be even, 2 to 32 (KiCad's limit), not {copper_layers}"
        )


def check_stackup(stackup: Node, copper_layers: int) -> None:
    """The stackup's copper rows are the board's copper layers, top to bottom."""
    want = ["F.Cu", *inner_layers(copper_layers), "B.Cu"]
    have = [
        atom_text(row[1])
        for row in children(stackup, "layer")
        if (kind := child(row, "type")) is not None and atom_text(kind[1]) == "copper"
    ]
    if have != want:
        raise ValueError(
            f"stackup copper layers {have} do not match a {copper_layers}-layer "
            f"board ({want})"
        )


def layers_node(copper_layers: int) -> Node:
    """The ``(layers …)`` table: F.Cu, the inner layers, B.Cu, then the user layers."""
    layers: Node = [S("layers")]
    inner = [
        (4 + 2 * i, name, "signal", None)
        for i, name in enumerate(inner_layers(copper_layers))
    ]
    for idx, name, kind, user in LAYERS[:1] + inner + LAYERS[1:]:
        layers.append([idx, name, S(kind)] + ([user] if user else []))
    return layers


def stackup(
    layers: list[tuple[str, str, float, dict[str, str | float]]],
    *,
    copper_finish: str = "ENIG",
    impedance_controlled: bool = True,
) -> Node:
    """A ``(stackup …)`` node from ``(name, type, thickness_mm, extra)`` rows, top to
    bottom, as KiCad's Board Setup writes it: copper (``"copper"``), dielectrics
    (``"core"``/``"prepreg"``, extra ``material``, ``epsilon_r``, ``loss_tangent``), mask
    (``"Top Solder Mask"`` …), silk and paste (thickness 0: none written)."""
    node: Node = [S("stackup")]
    for name, kind, thickness, extra in layers:
        row: Node = [S("layer"), name, [S("type"), kind]]
        if thickness:
            row.append([S("thickness"), num(thickness)])
        for key in ("material", "epsilon_r", "loss_tangent", "color"):
            if key in extra:
                val = extra[key]
                row.append([S(key), val if isinstance(val, str) else num(val)])
        node.append(row)
    node.append([S("copper_finish"), copper_finish])
    node.append(
        [S("dielectric_constraints"), S("yes" if impedance_controlled else "no")]
    )
    return node
