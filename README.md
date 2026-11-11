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

Every item a footprint takes from its library gets a uuid of that footprint (KiCad tells
board items apart by uuid, and a library's own uuids would repeat in every copy); an item
the board already has keeps its own. Footprints whose items share uuids with other items of
the board (as inkibox 0.5.0 and older left them) get their own on the next update.

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

A board that is placed but not yet routed can still gate everything else in CI:

```toml
[tool.inkibox.check]
allow_unconnected = true   # DRC passes with unconnected items; every other finding fails
```

## autorouting (`inkibox route`)

```bash
uv run inkibox route            # route the project's board in place, then DRC it
uv run inkibox route --keep out # also keep the DSN, SES and Freerouting log in out/
uv run inkibox route --seed 3   # another try: Freerouting routes in another order
```

Routes with [Freerouting](https://github.com/freerouting/freerouting) (GPL-3.0, run as a
separate program; nothing of it is linked into inkibox) and judges the result with KiCad's
own DRC. Over running `freerouting` by hand it adds what was missing on real boards:

| | without `inkibox route` | with it |
|---|---|---|
| Specctra DSN / SES | only KiCad's GUI or its `pcbnew` Python module; `kicad-cli` has no export | found and driven headless: `/usr/bin/python3` (KiCad image, Debian), KiCad's bundled Python (Windows, macOS), the `kicad-cli` wrapper's `PYTHONPATH` (Nix); `INKIBOX_KICAD_PYTHON` / `INKIBOX_PCBNEW_PATH` override |
| existing copper | Freerouting rips up tracks and vias it finds redundant (a hand-placed GND via on dvsp_5v_pw_b) | locked for the export, kept as drawn |
| zone nets | a zone becomes a Specctra plane, taken for solid copper; KiCad's fill is cut by other nets' tracks, leaving islands (two GND breaks on ecat_io_b) | `route_zone_nets` connects those nets with tracks; the zone only adds copper |
| zone fills | stale after the import (`inkibox check` reads the saved fill) | refilled and saved; optional stitching vias, plus one via into every orphaned F.Cu island of the stitched net |
| clearances | Freerouting's diagonals can end a few nm inside the clearance (a DRC error on dvsp_esp_32-s3_core_b) | `clearance_margin` widens the exported clearances only; per board, as on ecat_io_b the same margin left connections unrouted |
| project files | pcbnew's save rewrites the `.kicad_pro` (reordered DRC exclusions) | restored: only the board changes |
| verdict | exits 0 with connections left unrouted | Freerouting's final score and KiCad's DRC (unconnected items, errors); exit 1 if either is not clean |
| reproducibility | KiCad's import gives the new copper random uuids in a varying order | a pinned release (downloaded once, SHA-256 checked), one optimisation thread, uuids from the copper's content in a fixed order: the same board routes to the same bytes |
| routing order | Freerouting routes in the order the DSN lists components and nets, has no seed, and on a crowded board that order decides what gets through: renaming one net turned a clean route into 1–4 unrouted connections (dvsp_io_diso_b) | `seed` (`--seed N`) shuffles that order reproducibly: another seed is another try, the same seed the same tracks; no seed keeps KiCad's order |

Design rules, net-class widths and clearances, pad shapes and rule areas (keepouts) reach
Freerouting through KiCad's export. Custom DRC rules (`.kicad_dru`) do not: draw the area
they protect as a keepout (`inkibox.kicad.Board.keepout`), and DRC still checks the rule.

```toml
[tool.inkibox.route]
passes = 100                 # Freerouting's maximum auto-routing passes (default 100)
route_zone_nets = ["GND"]    # connect these with tracks, not through their zones
clearance_margin = 0.01      # mm added to every clearance while Freerouting routes (default 0)
seed = 3                     # route in a shuffled order (default: KiCad's order)
stitch = { net = "GND", pitch = 2.54, size = 0.6, drill = 0.3 }  # optional
```

Needs Java 21+ besides KiCad 10. `FREEROUTING_JAR` or `--jar` use another Freerouting build.

## placement (`inkibox place`)

```bash
uv run inkibox place              # pack the footprints, write positions and outline size
uv run inkibox place --dry-run    # solve and report only
uv run inkibox place --time 120   # give the solver longer
```

Packs a board's front footprints as tight as the rules allow with OR-Tools' CP-SAT, a
constraint solver, instead of placing them by rule ("this right of that, then snap"): rules
only nudge one arrangement, while turning a module 90° or laying it down elsewhere is a
different arrangement only a search reaches. Each footprint is its courtyard at an integer
position (0.01 mm) in one of its allowed rotations; keep areas (courtyard, `grow`n sides,
half the `gap`) never overlap; courtyards stay `edge` inside the outline or pass it by the
part's `overhang`, while pads always stay `pad_edge` inside; `on_grid` parts keep a pad on
the `grid` (modules on 2.54 mm pin sockets share one perfboard grid); `edges` put a side of a
part on a side of the board; `fixed` parts stay; `apart` keeps groups a distance from each
other. It minimises the board's area plus `wire_weight` mm² per mm of estimated wire length
(each net's half perimeter). The current positions are the starting point: a hand layout to
beat. The outline must be one `gr_rect` on Edge.Cuts; it keeps its top left corner and gets
the solved size. Tracks, vias and zones stay as they are: place, then route.

```toml
[tool.inkibox.place]
grid = 2.54
grid_origin = "A1:J2_1"                 # a pad whose current position is a grid point
on_grid = { A1 = "J2_1", A2 = "J2_1" }  # part -> its pad on the grid
fixed = ["A1"]
edges = { A1 = { top = "top" }, A3 = { top = "bottom" } }  # own side -> board side
grow = { A2 = { top = 0.88 } }          # terminals standing past a module's courtyard
overhang = { J1 = 6.0, A3 = 8.0 }       # mm the courtyard may pass the edge
apart = [{ a = ["J2"], b = ["A4"], gap = 2.0 }]
wire_weight = 0.5                       # mm² of board per mm of wire (default 0.5)
wire_ignore = ["GND"]
time_limit = 30                         # s (default 30); workers = 8, seed = 0
# gap = 0.25, edge = 0.5, pad_edge = 0.5, resolution = 0.05; width / height fix the size
```

OR-Tools needs protobuf 6 and kicad-python protobuf 5, so it is not a dependency: the solver
(`inkibox/place/_solver.py`, OR-Tools and the standard library only) runs in-process when
`ortools` imports, else in an isolated `uv run --with ortools` environment. With several
workers and a time limit, two runs can return different placements of similar cost. On
asm-canfd-stepper-b it found a 70.0 × 73.8 mm placement (5 162 mm²) in 60 s, against
5 770 mm² by hand.

## file-based generation (`inkibox.kicad`)

Besides the `kipy` scripts that drive a running KiCad, `inkibox.kicad` writes KiCad 10
files directly, headless, for boards that are *assembled* from module packages managed
by [kippm](https://github.com/existedinnettw/KIPPM):

| Module | Does |
|---|---|
| `inkibox.kicad.Libraries` | resolves `nick:item` through the project tables and KiCad's global tables (KiCad path variables from the environment, `kicad_common.json` and the `kicad-cli` wrapper: `inkibox.kicad.tables`); loads symbols flattened (`extends` resolved) and footprints; a symbol's pins are those of body style 1 (not the De Morgan alternate) |
| `inkibox.kicad.Schematic` | places symbols (one unit at a time for multi-unit symbols: `place(…, unit=n)`), labels pins (stub + local or global label, optionally with a PWR_FLAG for a supply net named by a label), hangs power symbols, marks no-connects; sub-sheets (`sheet(name, file, at, size)`; nets cross sheets by global labels and power symbols, or by `hierarchical_label(pin, name)` on the sub-sheet and `sheet_pin(sub, name, side=…)` on its symbol, which the parent labels like any pin: the net takes the parent's name, `/X` for a root label `X`; `write()` refuses a label without its sheet pin and the reverse); tracks the KiCad net name of every pin; `components()` joins the units of each reference for the board |
| `inkibox.kicad.Board` | outline, any even number of copper layers with an optional stackup (`Board(…, copper_layers=8, stackup=stackup([...]))`), footprints from libraries with nets on their pads (from the schematic symbol), on either side (`layer="B.Cu"` flips them as KiCad does), linked to their sheet, tracks, through/blind vias, zones, keepouts (rule areas); a footprint's own zones (keepouts) move with it; every uuid is stable, so a regenerated board is byte-identical; `kicad-cli pcb drc --schematic-parity` sees no mismatch |
| `inkibox.kicad.GridRouter` | a small two-layer grid router, kept for existing scripts; new boards route with `inkibox route` |

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
  * [ ] hierarchical sheets in `Schematic`

## CI / release

`ci.yml` runs ruff, ty, an import check and `uv build` on Linux and Windows. `release.yml`
publishes on a `vX.Y.Z` tag matching `[project].version`: build, upload to the Gitea index,
GitHub release. Both set the `.env.example` variables from the `GITEA_PYPI_*` secrets pushed by `git-acc-rtn`.
