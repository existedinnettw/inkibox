"""``inkibox route``: autoroute a board with Freerouting, from KiCad's own data, to a
board that passes DRC — or say what is left.

What it does that ``freerouting`` on its own cannot (each was found on real boards):

1. **Specctra round trip.** Freerouting reads DSN and writes SES; ``kicad-cli`` does
   neither, only KiCad's ``pcbnew`` bindings do. inkibox finds them (see :mod:`.env`) and
   runs the export / import there, so the design rules, pad shapes, net classes and rule
   areas (keepouts) Freerouting sees are KiCad's.
2. **Keeps existing copper.** Tracks and vias already on the board are locked for the
   export (KiCad writes them as ``protect``); left unlocked, Freerouting rips up hand-drawn
   copper it finds redundant (a GND via and its stub on dvsp_5v_pw_b).
3. **Zone nets as tracks** (``route_zone_nets``). KiCad exports a zone as a Specctra
   plane, which Freerouting takes for solid copper; KiCad's fill is cut by the other nets'
   tracks, so pins Freerouting counted as connected through it end up on islands (two GND
   breaks on ecat_io_b). Dropping a net's planes from the DSN makes Freerouting connect it
   with tracks; the zone then only adds copper.
4. **Zones refilled and saved.** A .kicad_pcb stores its fills, and ``inkibox check`` runs
   DRC on the saved fill; after the import every zone is refilled, optionally followed by
   stitching vias for a net poured on both layers.
5. **A real verdict.** Freerouting exits 0 with connections left unrouted; ``route`` reads
   its final score and then KiCad's own DRC (unconnected items, errors) on the result, and
   fails on either.
6. **Clearance margin** (``clearance_margin``, off by default). Freerouting's diagonals
   can end a few nm inside the clearance it was given (a clearance error on
   dvsp_esp_32-s3_core_b); a margin widens the clearances in the exported rules only. It
   is per board: on ecat_io_b the same margin left connections unrouted.
7. **Reproducible.** One optimisation thread (``-mt 1``) and a pinned Freerouting release
   give the same tracks on every run; KiCad's SES import, though, gives them fresh random
   uuids in a varying order, so the new copper is rewritten with uuids derived from its
   content and in a fixed order: the same board routes to the same bytes. No GUI, API
   server or analytics.

Options come from ``[tool.inkibox.route]`` in ``pyproject.toml``::

    [tool.inkibox.route]
    passes = 100                  # Freerouting's maximum auto-routing passes
    route_zone_nets = ["GND"]     # connect these with tracks, not through their zones
    clearance_margin = 0.01       # mm added to every clearance while Freerouting routes
    stitch = { net = "GND", pitch = 2.54, size = 0.6, drill = 0.3 }   # optional
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad import sfile
from ..update import ProjectError
from ..update.libcache import kicad_cli
from .env import (
    DEFAULT_FREEROUTING,
    RouteEnvError,
    find_freerouting,
    find_java,
    find_kicad_python,
)

WORKER = Path(__file__).with_name("_worker.py")

__all__ = [
    "RouteEnvError",
    "RouteOptions",
    "RouteResult",
    "drop_planes",
    "freerouting_score",
    "load_route_options",
    "route_project",
]


@dataclass(slots=True)
class RouteOptions:
    passes: int = 100
    threads: int = 1
    route_zone_nets: list[str] = field(default_factory=list)
    clearance_margin: float = 0.0
    stitch: dict | None = None
    freerouting: str = DEFAULT_FREEROUTING

    _STITCH_KEYS = ("net", "pitch", "size", "drill", "hole_to_hole")


def load_route_options(project_dir: Path, **override) -> RouteOptions:
    opts = RouteOptions()
    py = project_dir / "pyproject.toml"
    table: dict = {}
    if py.is_file():
        table = (
            tomllib.loads(py.read_text(encoding="utf-8"))
            .get("tool", {})
            .get("inkibox", {})
            .get("route", {})
        )
    known = {
        "passes",
        "threads",
        "route_zone_nets",
        "clearance_margin",
        "stitch",
        "freerouting",
    }
    unknown = set(table) - known
    if unknown:
        raise ProjectError(
            f"[tool.inkibox.route]: unknown option(s) {', '.join(sorted(unknown))}"
        )
    for key, value in {
        **table,
        **{k: v for k, v in override.items() if v is not None},
    }.items():
        setattr(opts, key, value)
    if opts.stitch is not None:
        bad = set(opts.stitch) - set(RouteOptions._STITCH_KEYS)
        if bad:
            raise ProjectError(
                f"[tool.inkibox.route].stitch: unknown key(s) {', '.join(sorted(bad))}"
            )
    return opts


@dataclass(slots=True)
class RouteResult:
    board: Path
    unrouted: int | None  # Freerouting's final count, None if it printed none
    stitching_vias: int
    orphaned: int  # F.Cu pour fragments of the stitched net no via could reach
    unconnected: list[str]  # KiCad DRC on the result
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.unrouted and not self.unconnected and not self.errors


def _sexpr_end(text: str, start: int) -> int:
    """Index just past the s-expression opening at ``start`` (quotes respected)."""
    depth, i, quoted = 0, start, False
    while i < len(text):
        c = text[i]
        if quoted:
            if c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced s-expression")


def drop_planes(dsn: str, nets: list[str]) -> tuple[str, int]:
    """Remove the ``(plane <net> …)`` entries of ``nets`` from a Specctra DSN; returns the
    text and how many were removed. KiCad quotes names with characters beyond letters,
    digits and ``+-._`` (``"/VIN"``), so both spellings are matched."""
    removed = 0
    for net in nets:
        for spelled in {net, f'"{net}"'}:
            pat = re.compile(r"\(plane " + re.escape(spelled) + r"[\s(]")
            while (m := pat.search(dsn)) is not None:
                end = _sexpr_end(dsn, m.start())
                dsn = dsn[: m.start()] + dsn[end:]
                removed += 1
    return dsn, removed


_SCORE = re.compile(r"\((\d+) unrouted and (\d+) violations\)")


def freerouting_score(log: str) -> int | None:
    """Unrouted connections in Freerouting's last reported score."""
    hits = _SCORE.findall(log)
    return int(hits[-1][0]) if hits else None


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _drc(board: Path) -> tuple[list[str], list[str]]:
    with tempfile.TemporaryDirectory(prefix="inkibox-route-") as tmp:
        out = Path(tmp) / "drc.json"
        r = _run(
            [
                kicad_cli(),
                "pcb",
                "drc",
                "--severity-error",
                "--format",
                "json",
                "-o",
                str(out),
                str(board),
            ]
        )
        if not out.is_file():
            raise ProjectError(
                f"kicad-cli pcb drc failed: {(r.stderr or r.stdout).strip()}"
            )
        report = json.loads(out.read_text(encoding="utf-8"))

    def line(v: dict) -> str:
        return f"[{v['type']}] " + "; ".join(
            i["description"] for i in v.get("items", [])[:2]
        )

    return [line(v) for v in report.get("unconnected_items", [])], [
        line(v) for v in report.get("violations", []) if v.get("severity") == "error"
    ]


def board_file(project_dir: Path) -> Path:
    """The board of the project in ``project_dir`` (the one ``*.kicad_pro``'s name)."""
    pros = sorted(project_dir.glob("*.kicad_pro"))
    if len(pros) != 1:
        raise ProjectError(
            f"{project_dir}: expected one *.kicad_pro, found {len(pros)}"
        )
    board = pros[0].with_suffix(".kicad_pcb")
    if not board.is_file():
        raise ProjectError(f"{board} not found")
    return board


_COPPER = ("segment", "arc", "via")
_NS = uuid.UUID("0b1e7d9e-6f4a-4f43-9d1e-6b0f5d1c2a77")


def _copper_uuids(board: Path) -> set[str]:
    root = sfile.SFile.load(board).root
    return {
        u.atom() or ""
        for c in root.children()
        if c.head in _COPPER and (u := c.child("uuid")) is not None
    }


def canonical_copper(board: Path, before: set[str]) -> int:
    """Give the copper not in ``before`` (by uuid) uuids derived from its content and write
    it, sorted, right after the copper that was there; returns how many items that is."""
    f = sfile.SFile.load(board)
    items = f.root.items
    new = [
        i
        for i in items
        if isinstance(i, sfile.Node)
        and i.head in _COPPER
        and ((u := i.child("uuid")) is None or u.atom() not in before)
    ]
    if not new:
        return 0
    keys = {id(n): repr(n.key(ignore=frozenset({"uuid"}))) for n in new}
    new.sort(key=lambda n: keys[id(n)])
    seen: dict[str, int] = {}
    for n in new:
        k = keys[id(n)]
        seen[k] = seen.get(k, 0) + 1
        value = sfile.string(str(uuid.uuid5(_NS, f"{k}#{seen[k]}")))
        u = n.child("uuid")
        if u is None:
            n.items.append(sfile.node("uuid", value))
        else:
            u.set_atom(0, value)
    ids = {id(n) for n in new}
    kept = [i for i in items if id(i) not in ids]
    last_old = max(
        (
            k
            for k, i in enumerate(kept)
            if isinstance(i, sfile.Node) and i.head in _COPPER
        ),
        default=None,
    )
    if last_old is None:  # no copper before: after the footprints, where KiCad puts it
        last_old = max(
            (
                k
                for k, i in enumerate(kept)
                if isinstance(i, sfile.Node) and i.head == "footprint"
            ),
            default=len(kept) - 1,
        )
    f.root.items = kept[: last_old + 1] + new + kept[last_old + 1 :]
    f.save()
    return len(new)


def route_project(
    project_dir: Path,
    opts: RouteOptions,
    *,
    jar: str | None = None,
    keep: Path | None = None,
    echo=print,
) -> RouteResult:
    """Route the project's board in place and check the result with KiCad's DRC."""
    board = board_file(project_dir)
    before = _copper_uuids(board)
    # pcbnew's save writes the project files too (reordered DRC exclusions, KiCad's ERC
    # defaults): routing changes the board and nothing else
    side = [board.with_suffix(s) for s in (".kicad_pro", ".kicad_prl")]
    saved = {p: p.read_bytes() for p in side if p.is_file()}
    try:
        return _route(board, before, opts, jar=jar, keep=keep, echo=echo)
    finally:
        for p in side:
            if p in saved:
                if p.read_bytes() != saved[p]:
                    p.write_bytes(saved[p])
            else:
                p.unlink(missing_ok=True)


def _route(
    board: Path,
    before: set[str],
    opts: RouteOptions,
    *,
    jar: str | None,
    keep: Path | None,
    echo,
) -> RouteResult:
    kpy = find_kicad_python()
    java = find_java()
    jar_path = find_freerouting(jar, opts.freerouting)
    with tempfile.TemporaryDirectory(prefix="inkibox-route-") as tmp_s:
        tmp = keep or Path(tmp_s)
        tmp.mkdir(parents=True, exist_ok=True)
        dsn, ses = tmp / f"{board.stem}.dsn", tmp / f"{board.stem}.ses"
        r = _run(
            [
                kpy.executable,
                str(WORKER),
                "export",
                str(board),
                str(dsn),
                str(opts.clearance_margin),
            ],
            env=kpy.env(),
        )
        if r.returncode != 0 or not dsn.is_file():
            raise ProjectError(f"DSN export failed: {_tail(r)}")
        if opts.route_zone_nets:
            text, removed = drop_planes(
                dsn.read_text(encoding="utf-8"), opts.route_zone_nets
            )
            dsn.write_text(text, encoding="utf-8")
            echo(
                f"zone nets routed as tracks: {', '.join(opts.route_zone_nets)} ({removed} plane(s) left out)"
            )
        cmd = [
            java, "-Djava.awt.headless=true", "-jar", str(jar_path),
            "-de", str(dsn), "-do", str(ses),
            "-mp", str(opts.passes), "-mt", str(opts.threads),
            "--gui.enabled=false",
            "--api_server.enabled=false",
            "--usage_and_diagnostic_data.disable_analytics=true",
        ]  # fmt: skip
        echo(
            f"freerouting {jar_path.name}: {opts.passes} passes, {opts.threads} thread(s)"
        )
        fr = _run(cmd, cwd=tmp)
        log = fr.stdout + fr.stderr
        if keep:
            (tmp / "freerouting.log").write_text(log, encoding="utf-8")
        if fr.returncode != 0 or not ses.is_file():
            raise ProjectError(
                f"Freerouting failed (exit {fr.returncode}): {_tail(fr)}"
            )
        unrouted = freerouting_score(log)
        args = [kpy.executable, str(WORKER), "import", str(board), str(ses), str(board)]
        if opts.stitch:
            args += ["--stitch", json.dumps(opts.stitch)]
        r = _run(args, env=kpy.env())
        if r.returncode != 0:
            raise ProjectError(f"SES import failed: {_tail(r)}")
        info = json.loads(r.stdout.strip().splitlines()[-1])
    canonical_copper(board, before)
    unconnected, errors = _drc(board)
    return RouteResult(
        board,
        unrouted,
        info.get("stitching_vias", 0),
        info.get("orphaned", 0),
        unconnected,
        errors,
    )


def _tail(r: subprocess.CompletedProcess[str], n: int = 12) -> str:
    lines = [ln for ln in (r.stderr or r.stdout or "").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])
