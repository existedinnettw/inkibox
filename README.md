# inkibox

scripts as KiCad toolbox

## run

```bash
kikit panelize -p kikit_panel.json zectio_b.kicad_pcb panelized/zectio_b.kicad_pcb

# or
# kikit panelize --layout "rows: 2; cols: 2" --cuts "type: vcuts" --framing "type: railstb" --tooling "type: 4hole; hoffset: 3mm; voffset: 3mm" --fiducials "type: 4fid; hoffset: 6mm; voffset: 3mm" "./zectio_b.kicad_pcb" "./panelized/panelized_zectio_b.kicad_pcb"
```

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

uv run python scripts/constraint_footprint.py

uv run python scripts/calculate_footprint_area.py

uv run python scripts/toggle_copper_zone.py off

# uv run python scripts/clear_tracks_vias.py
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

