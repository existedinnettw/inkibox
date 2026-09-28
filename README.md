# inkibox

scripts as KiCad toolbox

## setup

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

## file-based generation (`inkibox.kicad`)

Besides the `kipy` scripts that drive a running KiCad, `inkibox.kicad` writes KiCad 10
files directly, headless, for boards that are *assembled* from module packages managed
by [kippm](https://github.com/existedinnettw/KIPPM):

| Module | Does |
|---|---|
| `inkibox.kicad.Libraries` | resolves `nick:item` through the project tables and KiCad's global tables (KiCad path variables via `kippm.kicadenv`); loads symbols flattened (`extends` resolved) and footprints |
| `inkibox.kicad.Schematic` | places symbols, labels pins (stub + local label), hangs power symbols, marks no-connects; tracks the KiCad net name of every pin |
| `inkibox.kicad.Board` | outline, footprints from libraries with nets on their pads (from the schematic symbol), tracks, vias, zones; `kicad-cli pcb drc --schematic-parity` sees no mismatch |
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

See `ecat_io_b/scripts/generate.py` for a complete carrier (three modules, a buck converter, terminals, routing and the kicad-cli ERC/DRC gate).

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
