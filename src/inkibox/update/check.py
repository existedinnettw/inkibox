"""``inkibox check``: the design is up to date, clean, and stays unchanged.

The kippm steps run only for kippm projects (``build-backend = "kippm"``) and call kippm as
a program; inkibox itself does not depend on it.

1. ``kippm sync``                  3rd/ links and library-table rows (kippm projects)
2. ``inkibox update --dry-run``     nothing left to update (symbols, board, footprints)
3. footprint links                  every footprint linked to its symbol by UUID path
4. ERC                              ``kicad-cli sch erc`` (errors and warnings)
5. DRC with schematic parity        ``kicad-cli pcb drc --schematic-parity``
6. ``kippm doctor``                 errors only (kippm projects)
7. unchanged                        no tracked file changed on the way

Run it before every commit and in CI; a clean run means KiCad's own update actions would
change nothing either.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..kicad.sfile import SFile
from . import ProjectError, project_files, update_project
from .libcache import kicad_cli
from .options import UpdateOptions


@dataclass(slots=True)
class StepResult:
    title: str
    problems: list[str]
    skipped: str | None = None


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def uses_kippm(project_dir: Path) -> bool:
    pp = project_dir / "pyproject.toml"
    if not pp.is_file():
        return False
    data = tomllib.loads(pp.read_text(encoding="utf-8"))
    backend = data.get("build-system", {}).get("build-backend")
    return backend in ("kippm", "kippm.backend")


def kippm_command() -> list[str] | None:
    """How to run kippm here, without importing it (inkibox does not depend on kippm):
    the project environment's ``python -m kippm``, else ``kippm`` on PATH."""
    if importlib.util.find_spec("kippm") is not None:
        return [sys.executable, "-m", "kippm"]
    exe = shutil.which("kippm")
    return [exe] if exe else None


def _kippm(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = kippm_command()
    if cmd is None:
        raise RuntimeError(
            "kippm is not installed here (add it to the project's dev dependencies)"
        )
    return _run([*cmd, *args], project_dir)


def step_sync(project_dir: Path) -> list[str]:
    r = _kippm(project_dir, "sync")
    return [] if r.returncode == 0 else [(r.stderr or r.stdout).strip()]


def step_update(project_dir: Path, opts: UpdateOptions) -> list[str]:
    result = update_project(project_dir, opts, dry_run=True)
    rep = result.report
    return (
        [f"error: {m}" for m in rep.errors]
        + [f"would change: {m}" for m in rep.changes]
        + (
            [f"would write: {', '.join(p.name for p in result.changed)}"]
            if result.changed
            else []
        )
    )


def step_links(project_dir: Path) -> list[str]:
    """Every footprint (but board-only ones) is linked by UUID path to a symbol with the
    same reference."""
    _, sheets, board = project_files(project_dir)
    if board is None:
        return []
    root_uuid = SFile.load(sheets[0]).root.value("uuid")
    project = next(iter(project_dir.glob("*.kicad_pro"))).stem
    symbols: dict[str, str] = {}  # footprint path -> reference
    for sheet in sheets:
        for sym in SFile.load(sheet).root.children("symbol"):
            uid = sym.value("uuid")
            instances = sym.child("instances")
            if instances is None:
                continue
            # KiCad finds a symbol's instance by sheet path whatever project it is filed
            # under (a pasted symbol carries `(project "")` or the source project's name);
            # this project's entries go last so they win on the same path
            projs = sorted(
                instances.children("project"), key=lambda p: p.atom(0) == project
            )
            for proj in projs:
                for path in proj.children("path"):
                    parts = [p for p in (path.atom(0) or "").split("/") if p]
                    if parts and parts[0] == root_uuid:
                        parts = parts[1:]
                    symbols["/" + "/".join([*parts, uid])] = path.value("reference")
    problems = []
    for fp in SFile.load(board).root.children("footprint"):
        attr = fp.child("attr")
        if attr is not None and "board_only" in [a.raw for a in attr.atoms()]:
            continue
        ref = next(
            (
                p.atom(1) or "?"
                for p in fp.children("property")
                if p.atom(0) == "Reference"
            ),
            "?",
        )
        path = fp.value("path")
        if not path:
            problems.append(f"{ref}: not linked to a symbol (no path)")
        elif path not in symbols:
            problems.append(f"{ref}: linked to {path}, which no symbol has")
        elif symbols[path] != ref:
            problems.append(f"{ref}: linked to the symbol of {symbols[path]}")
    return problems


def _report(kind: str, project_dir: Path) -> list[str]:
    root, _, board = project_files(project_dir)
    if kind == "drc" and board is None:
        return []
    with tempfile.TemporaryDirectory(prefix="inkibox-check-") as tmp:
        out = Path(tmp) / "report.json"
        if kind == "erc":
            cmd = [kicad_cli(), "sch", "erc", str(root)]
        else:
            cmd = [kicad_cli(), "pcb", "drc", "--schematic-parity", str(board)]
        cmd += [
            "--severity-error",
            "--severity-warning",
            "--format",
            "json",
            "-o",
            str(out),
        ]
        r = _run(cmd, project_dir)
        if not out.is_file():
            return [f"kicad-cli failed: {(r.stderr or r.stdout).strip()}"]
        report = json.loads(out.read_text(encoding="utf-8"))
    items = []
    for sheet in report.get("sheets", []):
        items += sheet.get("violations", [])
    for key in ("violations", "unconnected_items", "schematic_parity"):
        items += report.get(key, [])
    return [
        f"[{v['type']}] {v['description']}"
        + "".join(f"\n      {i['description']}" for i in v.get("items", [])[:2])
        for v in items
    ]


def step_doctor(project_dir: Path) -> list[str]:
    r = _kippm(project_dir, "doctor", "--json")
    try:
        report = json.loads(r.stdout)
    except json.JSONDecodeError:
        return [(r.stderr or "kippm doctor printed no JSON").strip()]
    if report.get("ok"):
        return []
    return [
        f"[{f.get('check')}] {f.get('message')}"
        for f in report.get("findings", [])
        if f.get("level") == "error"
    ] or ["kippm doctor: not ok"]


def tracked_state(project_dir: Path) -> dict[str, str]:
    r = _run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"], project_dir
    )
    if r.returncode != 0:
        return {}
    return {
        f: hashlib.sha256((project_dir / f).read_bytes()).hexdigest()
        for f in r.stdout.splitlines()
        if (project_dir / f).is_file()
    }


def run_check(
    project_dir: Path,
    opts: UpdateOptions,
    *,
    echo: Callable[[StepResult], None] | None = None,
) -> list[StepResult]:
    project_dir = project_dir.resolve()
    kippm = uses_kippm(project_dir)
    before = tracked_state(project_dir)
    steps: list[tuple[str, Callable[[], list[str]] | None]] = [
        ("kippm sync", (lambda: step_sync(project_dir)) if kippm else None),
        (
            "up to date (inkibox update --dry-run)",
            lambda: step_update(project_dir, opts),
        ),
        ("footprint links", lambda: step_links(project_dir)),
        ("ERC", lambda: _report("erc", project_dir)),
        ("DRC + schematic parity", lambda: _report("drc", project_dir)),
        ("kippm doctor", (lambda: step_doctor(project_dir)) if kippm else None),
    ]
    results = []
    for title, fn in steps:
        if fn is None:
            res = StepResult(title, [], skipped="not a kippm project")
        else:
            try:
                res = StepResult(title, fn())
            except (ProjectError, RuntimeError, OSError) as exc:
                res = StepResult(title, [str(exc)])
        results.append(res)
        if echo:
            echo(res)
    after = tracked_state(project_dir)
    changed = sorted(
        f for f in before.keys() | after.keys() if before.get(f) != after.get(f)
    )
    res = StepResult("unchanged by the sequence", changed)
    results.append(res)
    if echo:
        echo(res)
    return results
