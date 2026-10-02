# inkibox

scripts as KiCad toolbox

## setup

Every dependency comes from PyPI; `kicad-cli` (KiCad 10) must be on PATH for `inkibox update`
and `inkibox check`. Publishing to the org's Gitea index is by tag (`release.yml`).

```bash
uv sync
uv run pre-commit install

```

## run

```bash
kicad-cli pcb export step zectio_b.kicad_pcb
# u3d, vrml...
kicad-cli pcb render zectio_b.kicad_pcb -o zectio_b.jpg
```

Several scripts are useful to assist design:

Caution!!! enable new KiCad API in `preferences/preferences/plugins` if not enabled.

```bash
# plugins are written in `kipy`
# for pcbnew (deprecated), `export PYTHONPATH="/usr/lib/python3/dist-packages:$PYTHONPATH"`

uv run python -m inkibox.scripts.constraint_footprint

uv run python -m inkibox.scripts.calculate_footprint_area

uv run python -m inkibox.scripts.toggle_copper_zone off

# uv run python -m inkibox.scripts.clear_tracks_vias
```

## headless updates (`inkibox update`, `inkibox check`)

KiCad's three update actions, run from the command line in a fixed order and with options
the repo declares, instead of whatever each machine's KiCad dialogs remember:

```bash
uv run inkibox update             # Update Symbols from Library -> Update PCB from Schematic
                                  #   -> Update Footprints from Library; writes changed files
uv run inkibox update --dry-run   # report only
uv run inkibox update --only pcb  # a subset: symbols, pcb, footprints
uv run inkibox check              # kippm sync*, nothing left to update, footprint links,
                                  #   ERC, DRC with schematic parity, kippm doctor*, and no
                                  #   tracked file changed by any of it (CI, pre-commit)
uv run inkibox options            # the effective options
```

\* for [kippm](https://github.com/existedinnettw/KIPPM) projects (`build-backend = "kippm"`),
run as a program from the project's environment; inkibox does not depend on kippm, and
needs only `kicad-cli` besides its Python dependencies.

The files are edited in place: every sheet and the board are first brought to the current
format by `kicad-cli sch/pcb upgrade` (so a file is never half KiCad 9, half KiCad 10),
libraries are read through `kicad-cli sym/fp upgrade` (cached by hash) so embedded copies
are exactly what KiCad would embed, and only the items that change are rewritten, in
KiCad's own layout. A design that is up to date is not touched. On designs KiCad itself had
just updated, all three steps produce byte-identical files.

Options, with the defaults fixed in `inkibox.update.options` (the safe set: the schematic
owns footprint assignments, the board owns text placement):

```toml
[tool.inkibox.update.symbols]       # Update Symbols from Library
shape_and_pins = true               # embedded copy, pins
keywords = true                     # ki_keywords / ki_fp_filters
field_text = false                  # KiCad's default is on: it swaps footprints chosen in
                                    # the design back to the library default
update_references = false
update_values = false
reset_empty_fields = false
remove_extra_fields = false
attributes = false                  # DNP / in BOM / on board from the library
alternate_pins = false
custom_power = false

[tool.inkibox.update.pcb]           # Update PCB from Schematic (link by UUID path)
replace_footprints = true
delete_unused_footprints = false
remove_extra_fields = false
link_unlinked = true                # a footprint with no path joins the symbol with its
                                    # reference (KiCad would add a second footprint)

[tool.inkibox.update.footprints]    # Update Footprints from Library
remove_extra_texts = false
text_content = false
fabrication_attributes = true       # smd/through_hole…; DNP/BOM/pos stay with the schematic
clearance_overrides = true
models_3d = true
```

`--set footprints.models_3d=false` overrides one on the command line. The options that only
move or restyle text (field visibilities/effects/positions, pin text visibility, footprint
text layers/effects/positions) and re-linking by reference are not implemented headless:
setting one is an error, so a design never depends on them silently.

## file-based generation (`inkibox.kicad`)

Besides the `kipy` scripts that drive a running KiCad, `inkibox.kicad` writes KiCad 10
files directly, headless, for boards that are *assembled* from module packages managed
by [kippm](https://github.com/existedinnettw/KIPPM):

| Module | Does |
|---|---|
| `inkibox.kicad.Libraries` | resolves `nick:item` through the project tables and KiCad's global tables (KiCad path variables from the environment, `kicad_common.json` and the `kicad-cli` wrapper: `inkibox.kicad.tables`); loads symbols flattened (`extends` resolved) and footprints; a symbol's pins are those of body style 1 (not the De Morgan alternate) |
| `inkibox.kicad.Schematic` | places symbols, labels pins (stub + local label, optionally with a PWR_FLAG for a supply net named by a label), hangs power symbols, marks no-connects; tracks the KiCad net name of every pin |
| `inkibox.kicad.Board` | outline, footprints from libraries with nets on their pads (from the schematic symbol), tracks, vias, zones; a footprint's own zones (keepouts) move with it; every uuid is stable, so a regenerated board is byte-identical; `kicad-cli pcb drc --schematic-parity` sees no mismatch |
| `inkibox.kicad.GridRouter` | a two-layer Manhattan grid router (Dijkstra, layer direction preference, via cost) honouring clearance, hole-to-hole and edge rules; reports what it cannot route |

```python
from inkibox.kicad import Libraries, Schematic, Board

libs = Libraries(project_dir)
sch = Schematic("carrier", libs)
a1 = sch.place("core-board:Core_R1", "A1", (76.2, 152.4))
sch.label(a1.pin("J2_5"), "SPI_SCK")          # net /SPI_SCK
sch.power(a1.pin("J2_1"), "power:+5V", flag=True)
sch.no_connect_unused(a1)
sch.write(project_dir / "carrier.kicad_sch")

pcb = Board("carrier", libs)
pcb.outline_rect(100, 50, 215, 150)
pcb.place(a1, (150, 80))                      # pads get the pins' nets
pcb.write(project_dir / "carrier.kicad_pcb")
```

See `ecat_io_b/scripts/generate.py` for a complete carrier (three modules, terminals, routing and the kicad-cli ERC/DRC gate).

## todo

* schematic
  * [ ] ERC
  * [ ] black_f407ve like pinout defined
* layout
  * [ ] follow rail design rules
  * [ ] NA
* setup
  * [KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server)
* `inkibox.kicad`
  * [ ] diagonal (45°) routing and net classes in `GridRouter`
  * [ ] hierarchical sheets in `Schematic`

## CI / release

`ci.yml` runs ruff, ty, an import check and `uv build` on Linux and Windows. `release.yml`
publishes on a `vX.Y.Z` tag matching `[project].version`: build, upload to the Gitea index,
GitHub release. Both set the `.env.example` variables from the `GITEA_PYPI_*` secrets pushed by `git-acc-rtn`.
