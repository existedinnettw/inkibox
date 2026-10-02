"""Where ``inkibox route`` finds its tools: KiCad's Python bindings, Java, Freerouting.

* **pcbnew.** ``kicad-cli`` cannot export or import Specctra files; only the ``pcbnew``
  Python module can. It ships with KiCad, outside any virtualenv: on Debian/Ubuntu and in
  the ``kicad/kicad`` image as ``/usr/bin/python3``'s ``pcbnew``, on Windows and macOS
  with KiCad's bundled Python, on Nix on the ``PYTHONPATH`` the ``kicad-cli`` wrapper
  sets. ``INKIBOX_KICAD_PYTHON`` (an interpreter) and ``INKIBOX_PCBNEW_PATH`` (the
  directory holding ``pcbnew.py``) override the search.
* **Java** from ``JAVA_HOME`` or ``PATH``; Freerouting 2 needs 21 or newer.
* **Freerouting**: a pinned release, downloaded once from GitHub into the user cache and
  checked against its SHA-256 (``FREEROUTING_JAR`` or ``--jar`` use a jar as is).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class RouteEnvError(RuntimeError):
    pass


# Freerouting releases inkibox knows the hash of (the jar is the platform-independent one)
FREEROUTING = {
    "2.4.1": "251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9",
}
DEFAULT_FREEROUTING = "2.4.1"
JAVA_MIN = 21


@dataclass(frozen=True, slots=True)
class KicadPython:
    executable: str
    pythonpath: str | None  # prepended to PYTHONPATH, or None to leave it as is

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.pythonpath:
            env["PYTHONPATH"] = os.pathsep.join(
                p for p in (self.pythonpath, env.get("PYTHONPATH")) if p
            )
        return env


def _imports_pcbnew(exe: str, pythonpath: str | None) -> bool:
    try:
        r = subprocess.run(
            [exe, "-c", "import pcbnew"],
            env=KicadPython(exe, pythonpath).env(),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _wrapper_pythonpath(kicad_cli: str | None) -> str | None:
    """The ``PYTHONPATH`` a wrapper script around kicad-cli exports (Nix, some AppImages)."""
    if not kicad_cli:
        return None
    try:
        head = Path(kicad_cli).read_bytes()[:65536]
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    m = re.search(rb"export PYTHONPATH=['\"]?([^'\"\n]+)", head)
    if not m:
        return None
    return m.group(1).decode().split(":$PYTHONPATH")[0].rstrip(":")


def _bundled_pythons() -> list[str]:
    out = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
            for d in sorted(Path(base, "KiCad").glob("*/bin"), reverse=True):
                out.append(str(d / "python.exe"))
    elif sys.platform == "darwin":
        out.append(
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3"
        )
    out.append("/usr/bin/python3")
    return out


def find_kicad_python(kicad_cli: str | None = None) -> KicadPython:
    exe = os.environ.get("INKIBOX_KICAD_PYTHON")
    path = os.environ.get("INKIBOX_PCBNEW_PATH")
    if exe or path:
        cand = KicadPython(exe or sys.executable, path)
        if _imports_pcbnew(cand.executable, cand.pythonpath):
            return cand
        raise RouteEnvError(
            f"INKIBOX_KICAD_PYTHON / INKIBOX_PCBNEW_PATH set, but {cand.executable} cannot import pcbnew"
        )
    kicad_cli = kicad_cli or os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    wrapped = _wrapper_pythonpath(kicad_cli)
    candidates = [KicadPython(sys.executable, None)]
    if wrapped:
        candidates.append(KicadPython(sys.executable, wrapped))
        # the interpreter the bindings were built for, if it is a different minor version
        candidates += [KicadPython(e, wrapped) for e in ("python3",) if shutil.which(e)]
    candidates += [
        KicadPython(e, None) for e in _bundled_pythons() if Path(e).is_file()
    ]
    for cand in candidates:
        if _imports_pcbnew(cand.executable, cand.pythonpath):
            return cand
    raise RouteEnvError(
        "no Python with KiCad's pcbnew module found (install KiCad 10, or set "
        "INKIBOX_KICAD_PYTHON / INKIBOX_PCBNEW_PATH)"
    )


def find_java() -> str:
    home = os.environ.get("JAVA_HOME")
    exe = str(Path(home, "bin", "java")) if home else shutil.which("java")
    if not exe or not (Path(exe).is_file() or shutil.which(exe)):
        raise RouteEnvError(f"java not found (Freerouting needs Java {JAVA_MIN}+)")
    r = subprocess.run([exe, "-version"], capture_output=True, text=True, check=False)
    m = re.search(r'version "(\d+)', r.stderr + r.stdout)
    if not m or int(m.group(1)) < JAVA_MIN:
        raise RouteEnvError(
            f"{exe}: Java {m.group(1) if m else '?'}; Freerouting needs {JAVA_MIN}+"
        )
    return exe


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    )
    return Path(base) if base else Path.home() / ".cache"


def find_freerouting(
    jar: str | None = None, version: str = DEFAULT_FREEROUTING
) -> Path:
    given = jar or os.environ.get("FREEROUTING_JAR")
    if given:
        p = Path(given)
        if not p.is_file():
            raise RouteEnvError(f"Freerouting jar {p} not found")
        return p
    if version not in FREEROUTING:
        raise RouteEnvError(
            f"Freerouting {version}: unknown release (known: {', '.join(FREEROUTING)}); "
            "pass --jar to use another one"
        )
    target = cache_dir() / "inkibox" / f"freerouting-{version}.jar"
    if target.is_file() and _sha256(target) == FREEROUTING[version]:
        return target
    url = f"https://github.com/freerouting/freerouting/releases/download/v{version}/freerouting-{version}.jar"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as exc:
        raise RouteEnvError(f"downloading {url}: {exc}") from exc
    digest = _sha256(tmp)
    if digest != FREEROUTING[version]:
        tmp.unlink(missing_ok=True)
        raise RouteEnvError(f"{url}: SHA-256 {digest}, expected {FREEROUTING[version]}")
    tmp.replace(target)
    return target


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
