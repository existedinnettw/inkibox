"""``inkibox``: headless KiCad design maintenance.

    inkibox update [DIR] [--dry-run] [--only symbols,pcb,footprints] [--set sec.opt=bool]…
    inkibox check  [DIR] [--set sec.opt=bool]…
    inkibox options [DIR]

``update`` runs *Update Symbols from Library*, *Update PCB from Schematic* and *Update
Footprints from Library* in that order; ``check`` verifies the design is up to date, ERC/DRC
clean and unchanged by the sequence; ``options`` prints the effective update options
(defaults, ``[tool.inkibox.update]`` in ``pyproject.toml``, ``--set``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .update import STEPS, ProjectError, update_project
from .update.check import StepResult, run_check
from .update.libcache import LibraryError
from .update.options import OptionsError, load_options


def _options(args: argparse.Namespace):
    return load_options(args.project.resolve(), args.set)


def cmd_update(args: argparse.Namespace) -> int:
    opts = _options(args)
    steps = tuple(s.strip() for s in args.only.split(",")) if args.only else STEPS
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise OptionsError(
            f"--only: unknown step(s) {', '.join(unknown)} ({', '.join(STEPS)})"
        )
    result = update_project(args.project, opts, dry_run=args.dry_run, steps=steps)
    rep = result.report
    for m in rep.changes:
        print(m)
    for m in rep.warnings:
        print(f"warning: {m}", file=sys.stderr)
    for m in rep.errors:
        print(f"error: {m}", file=sys.stderr)
    verb = "would write" if args.dry_run else "wrote"
    if result.changed:
        print(
            f"{verb}: {', '.join(str(p.relative_to(args.project.resolve())) for p in result.changed)}"
        )
    else:
        print("up to date: nothing to change")
    return 1 if rep.errors else 0


def cmd_check(args: argparse.Namespace) -> int:
    opts = _options(args)
    failed = False

    def echo(res: StepResult) -> None:
        nonlocal failed
        if res.skipped:
            print(f"skip  {res.title} ({res.skipped})")
            return
        failed |= bool(res.problems)
        print(
            f"{'FAIL' if res.problems else 'ok  '}  {res.title}"
            + (f" ({len(res.problems)})" if res.problems else "")
        )
        for p in res.problems:
            print(f"      {p}")
        sys.stdout.flush()

    run_check(args.project, opts, echo=echo)
    return 1 if failed else 0


def cmd_options(args: argparse.Namespace) -> int:
    print(json.dumps(_options(args).as_dict(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="inkibox",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (
        ("update", "run the three KiCad update actions headless"),
        ("check", "up to date, ERC/DRC clean, unchanged"),
        ("options", "print the effective update options"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument(
            "project",
            nargs="?",
            type=Path,
            default=Path.cwd(),
            help="project directory (default: .)",
        )
        p.add_argument(
            "--set",
            action="append",
            default=[],
            metavar="SECTION.OPTION=BOOL",
            help="override an update option",
        )
        if name == "update":
            p.add_argument(
                "--dry-run", action="store_true", help="report, write nothing"
            )
            p.add_argument(
                "--only",
                metavar="STEPS",
                help=f"comma-separated subset of {','.join(STEPS)}",
            )
    args = ap.parse_args(argv)
    try:
        return {"update": cmd_update, "check": cmd_check, "options": cmd_options}[
            args.cmd
        ](args)
    except (OptionsError, ProjectError, LibraryError) as exc:
        print(f"inkibox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
