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

## todo

* schematic
  * [ ] ERC
  * [ ] black_f407ve like pinout defined
* layout
  * [ ] follow rail design rules
  * [ ] NA
* setup
  * [KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server)
