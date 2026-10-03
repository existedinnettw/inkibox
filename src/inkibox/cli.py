"""``inkibox``: headless KiCad design maintenance.

    inkibox update [DIR] [--dry-run] [--only symbols,pcb,footprints] [--set sec.opt=bool]…
    inkibox check  [DIR] [--set sec.opt=bool]…
    inkibox options [DIR]
    inkibox route  [DIR] [--passes N] [--jar FREEROUTING.jar] [--keep DIR]

``update`` runs *Update Symbols from Library*, *Update PCB from Schematic* and *Update
Footprints from Library* in that order; ``check`` verifies the design is up to date, ERC/DRC
clean and unchanged by the sequence; ``options`` prints the effective update options
(defaults, ``[tool.inkibox.update]`` in ``pyproject.toml``, ``--set``); ``route``
autoroutes the board with Freerouting (``[tool.inkibox.route]``) and checks it with DRC.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .route import RouteEnvError, load_route_options, route_project
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


def cmd_route(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    opts = load_route_options(project, passes=args.passes)
    res = route_project(project, opts, jar=args.jar, keep=args.keep)
    print(
        f"freerouting: {res.unrouted if res.unrouted is not None else '?'} unrouted"
        + (
            f"; {res.stitching_vias} stitching via(s)"
            + (
                f", {res.orphaned} pour island(s) left without one"
                if res.orphaned
                else ""
            )
            if opts.stitch
            else ""
        )
    )
    print(
        f"DRC: {len(res.unconnected)} unconnected, {len(res.errors)} error(s) -> {res.board.name}"
    )
    for m in res.unconnected + res.errors:
        print(f"  {m}")
    return 0 if res.ok else 1


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
        ("route", "autoroute with Freerouting, then check with DRC"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument(
            "project",
            nargs="?",
            type=Path,
            default=Path.cwd(),
            help="project directory (default: .)",
        )
        if name == "route":
            p.add_argument(
                "--passes", type=int, help="Freerouting passes (default 100)"
            )
            p.add_argument(
                "--jar", help="a Freerouting jar to use instead of the pinned release"
            )
            p.add_argument(
                "--keep", type=Path, help="keep the DSN, SES and Freerouting log here"
            )
            continue
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
        return {
            "update": cmd_update,
            "check": cmd_check,
            "options": cmd_options,
            "route": cmd_route,
        }[args.cmd](args)
    except (OptionsError, ProjectError, LibraryError, RouteEnvError) as exc:
        print(f"inkibox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
