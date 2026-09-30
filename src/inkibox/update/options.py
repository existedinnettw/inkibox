"""What the three update steps may change.

The defaults are fixed here and are the safe set for a design whose schematic owns the
footprint assignments and whose board owns the text placement. A repo overrides them in
``pyproject.toml``; the command line overrides both::

    [tool.inkibox.update.symbols]
    field_text = true            # take Datasheet/Description/… text from the libraries

    inkibox update --set footprints.models_3d=false

Nothing is read from KiCad's per-machine dialog settings (``kicad_common.json``): the same
repo updates the same way on every machine and in CI.

Names follow KiCad 10's dialogs: *Update Symbols from Library* (``symbols``), *Update PCB
from Schematic* (``pcb``), *Update Footprints from Library* (``footprints``). Options that
only move or restyle text are not implemented headless; setting one is an error, so a
design never silently depends on them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


class OptionsError(ValueError):
    pass


@dataclass(slots=True)
class SymbolOptions:
    """*Update Symbols from Library*, update mode, every symbol."""

    shape_and_pins: bool = True  # Update symbol shape and pins (the embedded lib copy)
    keywords: bool = True  # Update keywords and footprint filters
    # Update/reset field text; KiCad's default is on, but it swaps footprints a design
    # chose over the library default (a stock D_SOD-323 back to PCM_Diode_SMD_AKL)
    field_text: bool = False
    update_references: bool = False  # …including Reference (keeps the number)
    update_values: bool = False  # …including Value
    reset_empty_fields: bool = False  # Reset fields if empty in library symbol
    remove_extra_fields: bool = False  # Remove fields if not in library symbol
    attributes: bool = False  # Update/reset symbol attributes (DNP, in BOM, on board)
    alternate_pins: bool = False  # Reset alternate pin functions
    custom_power: bool = False  # Reset custom power symbols (their Value)
    # not implemented headless (layout only): must stay off
    field_visibilities: bool = False
    field_effects: bool = False
    field_positions: bool = False
    pin_text_visibility: bool = False


@dataclass(slots=True)
class PcbOptions:
    """*Update PCB from Schematic*."""

    replace_footprints: bool = (
        True  # Replace footprints with those specified by symbols
    )
    delete_unused_footprints: bool = False  # Delete footprints with no symbols
    remove_extra_fields: bool = False  # Remove footprint fields no symbol has
    # a footprint with no link at all (no path; placed by hand, or linked in an old KiCad)
    # joins the symbol with its reference when no linked footprint has that symbol. KiCad
    # adds a second footprint instead; linked footprints are never re-linked.
    link_unlinked: bool = True
    # not implemented: footprints are linked to symbols by UUID path
    relink_by_reference: bool = False


@dataclass(slots=True)
class FootprintOptions:
    """*Update Footprints from Library*, update mode, every footprint."""

    remove_extra_texts: bool = (
        False  # Remove text items which are not in library footprint
    )
    text_content: bool = (
        False  # Update/reset text content (fields other than ref/value)
    )
    fabrication_attributes: bool = True  # Update/reset fabrication attributes (smd, …)
    clearance_overrides: bool = True  # Update/reset clearance overrides
    models_3d: bool = True  # Update/reset 3D models
    # not implemented headless (board text keeps its layer, style and place): must stay off
    text_layers: bool = False
    text_effects: bool = False
    text_positions: bool = False


UNSUPPORTED = {
    "symbols": (
        "field_visibilities",
        "field_effects",
        "field_positions",
        "pin_text_visibility",
    ),
    "pcb": ("relink_by_reference",),
    "footprints": ("text_layers", "text_effects", "text_positions"),
}


@dataclass(slots=True)
class UpdateOptions:
    symbols: SymbolOptions = field(default_factory=SymbolOptions)
    pcb: PcbOptions = field(default_factory=PcbOptions)
    footprints: FootprintOptions = field(default_factory=FootprintOptions)

    def set(self, dotted: str, value: object) -> None:
        section, _, name = dotted.partition(".")
        target = getattr(self, section, None)
        if target is None or section not in UNSUPPORTED or not name:
            raise OptionsError(
                f"unknown option {dotted!r} (symbols.*, pcb.*, footprints.*)"
            )
        names = {f.name for f in fields(target)}
        if name not in names:
            raise OptionsError(
                f"unknown option {dotted!r}; {section} has {', '.join(sorted(names))}"
            )
        if not isinstance(value, bool):
            raise OptionsError(f"{dotted} must be true or false, got {value!r}")
        setattr(target, name, value)

    def validate(self) -> None:
        on = [
            f"{section}.{name}"
            for section, names in UNSUPPORTED.items()
            for name in names
            if getattr(getattr(self, section), name)
        ]
        if on:
            raise OptionsError(
                f"not implemented headless, update these in KiCad instead: {', '.join(on)}"
            )

    def as_dict(self) -> dict[str, dict[str, bool]]:
        return {
            section: {
                f.name: getattr(getattr(self, section), f.name)
                for f in fields(getattr(self, section))
            }
            for section in UNSUPPORTED
        }


def parse_bool(text: str) -> bool:
    low = text.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise OptionsError(f"not a boolean: {text!r}")


def load_options(
    project_dir: Path, overrides: list[str] | None = None
) -> UpdateOptions:
    """Defaults, then ``[tool.inkibox.update]`` of the project's ``pyproject.toml``, then
    ``overrides`` (``section.name=value``)."""
    opts = UpdateOptions()
    pp = project_dir / "pyproject.toml"
    if pp.is_file():
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise OptionsError(f"{pp}: {exc}") from exc
        cfg = data.get("tool", {}).get("inkibox", {}).get("update", {})
        if not isinstance(cfg, dict):
            raise OptionsError(f"{pp}: [tool.inkibox.update] must be a table")
        for section, values in cfg.items():
            if not isinstance(values, dict):
                raise OptionsError(
                    f"{pp}: [tool.inkibox.update.{section}] must be a table"
                )
            for name, value in values.items():
                opts.set(f"{section}.{name}", value)
    for item in overrides or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise OptionsError(f"--set expects section.name=value, got {item!r}")
        opts.set(key.strip(), parse_bool(value))
    opts.validate()
    return opts
