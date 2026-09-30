"""KiCad library tables (``sym-lib-table``, ``fp-lib-table``) and path variables, as KiCad
resolves them: the project's table first, then the global one, whose rows may include
further tables (KiCad 9+ lists its stock libraries through a single
``(type "Table") (uri "${KICAD10_TEMPLATE_DIR}/sym-lib-table")`` row).

Path variables (``${KICAD10_SYMBOL_DIR}`` …) come, later sources winning, from the stock
install directories that exist, the ``kicad-cli`` wrapper script (nix and Homebrew set them
there), ``environment.vars`` in ``kicad_common.json``, and the process environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .sfile import SExprError, parse, read_text

TABLE_FILES = {"symbol": "sym-lib-table", "footprint": "fp-lib-table"}
VERSIONS = ("10.0", "9.0", "8.0")
_VAR = re.compile(r"\$[{(]([A-Za-z0-9_]+)[})]")
# `export KICAD10_SYMBOL_DIR=/x`, `KICAD10_SYMBOL_DIR=${KICAD10_SYMBOL_DIR-'/x'}` (nix)
_ASSIGN = re.compile(
    r"""(?:^|[\s;])(?:export\s+)?(KICAD\d*_[A-Z0-9_]+)=(?:\$\{\1-)?(['"]?)([^'"\s}]+)\2\}?""",
    re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    type: str
    uri: str


def read_table(path: Path) -> list[Row]:
    """The rows of one table file; ``[]`` when it is missing or unreadable."""
    if not path.is_file():
        return []
    try:
        root = parse(read_text(path))
    except (SExprError, OSError, UnicodeDecodeError):
        return []
    rows = []
    for lib in root.children("lib"):
        rows.append(
            Row(lib.value("name"), lib.value("type", "KiCad"), lib.value("uri"))
        )
    return rows


# --------------------------------------------------------------------------- directories


def config_dirs() -> list[Path]:
    """KiCad's per-user configuration directories, newest version first."""
    if env := os.environ.get("KICAD_CONFIG_HOME"):
        bases = [Path(env)]
    elif sys.platform == "win32":
        bases = [Path(os.environ.get("APPDATA", Path.home())) / "kicad"]
    elif sys.platform == "darwin":
        bases = [Path.home() / "Library" / "Preferences" / "kicad"]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        bases = [(Path(xdg) if xdg else Path.home() / ".config") / "kicad"]
    out = []
    for base in bases:
        out += [base / v for v in VERSIONS if (base / v).is_dir()]
        out.append(base)
    return out


def _stock_roots() -> list[Path]:
    if sys.platform == "win32":
        pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        return [pf / "KiCad" / v / "share" / "kicad" for v in VERSIONS]
    if sys.platform == "darwin":
        return [Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")]
    return [Path("/usr/share/kicad"), Path("/usr/local/share/kicad")]


def _wrapper_vars() -> dict[str, str]:
    exe = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if not exe:
        return {}
    try:
        with open(exe, "rb") as fh:
            head = fh.read(1 << 16)
    except OSError:
        return {}
    if b"\0" in head:  # a binary, not a wrapper script
        return {}
    return {
        m.group(1): m.group(3)
        for m in _ASSIGN.finditer(head.decode("utf-8", "replace"))
    }


def _common_json_vars() -> dict[str, str]:
    for d in config_dirs():
        p = d / "kicad_common.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        env = (data.get("environment") or {}).get("vars") or {}
        return {k: str(v) for k, v in env.items() if isinstance(k, str)}
    return {}


@cache
def _kicad_vars(_key: tuple) -> dict[str, str]:
    env: dict[str, str] = {}
    for root in _stock_roots():
        for v in VERSIONS:
            n = v.split(".")[0]
            for name, sub in (
                ("SYMBOL_DIR", "symbols"),
                ("FOOTPRINT_DIR", "footprints"),
                ("3DMODEL_DIR", "3dmodels"),
                ("TEMPLATE_DIR", "template"),
            ):
                if (root / sub).is_dir():
                    env.setdefault(f"KICAD{n}_{name}", str(root / sub))
    env.update(_wrapper_vars())
    env.update(_common_json_vars())
    env.update({k: v for k, v in os.environ.items() if k.startswith("KICAD")})
    return env


def kicad_vars() -> dict[str, str]:
    """Every KiCad path variable known here."""
    key = tuple(
        sorted(
            (k, v)
            for k, v in os.environ.items()
            if k.startswith(("KICAD", "XDG", "PATH"))
        )
    )
    return dict(_kicad_vars(key))


def expand(text: str, project_dir: Path | None = None) -> str | None:
    """``${VAR}`` expanded (``${KIPRJMOD}`` to ``project_dir``); ``None`` if one is unknown."""
    env = kicad_vars()
    if project_dir is not None:
        env["KIPRJMOD"] = str(project_dir)
    unknown = False

    def repl(m: re.Match[str]) -> str:
        nonlocal unknown
        val = os.environ.get(m.group(1)) if m.group(1) != "KIPRJMOD" else None
        val = val or env.get(m.group(1))
        if val is None:
            unknown = True
            return m.group(0)
        return val

    out = _VAR.sub(repl, text)
    return None if unknown else out


# --------------------------------------------------------------------------- resolution


def _rows_of(path: Path, seen: set[Path], project_dir: Path | None) -> list[Row]:
    if path in seen:
        return []
    seen.add(path)
    rows: list[Row] = []
    for row in read_table(path):
        if row.type.lower() == "table":
            target = expand(row.uri, project_dir)
            if target is not None:
                rows += _rows_of(Path(target), seen, project_dir)
        else:
            rows.append(row)
    return rows


def global_rows(kind: str) -> list[Row]:
    """The global table of ``kind`` with its includes followed (newest KiCad version)."""
    for d in config_dirs():
        p = d / TABLE_FILES[kind]
        if p.is_file():
            return _rows_of(p, set(), None)
    return []


def library_paths(project_dir: Path, kind: str) -> dict[str, Path]:
    """``nickname -> path`` of every ``KiCad``-type library a project sees: its own rows
    shadow global ones; rows whose variables are unknown here are left out."""
    out: dict[str, Path] = {}
    for row in [
        *_rows_of(project_dir / TABLE_FILES[kind], set(), project_dir),
        *global_rows(kind),
    ]:
        if row.type != "KiCad" or row.name in out:
            continue
        p = expand(row.uri, project_dir)
        if p is not None:
            out[row.name] = Path(p)
    return out
