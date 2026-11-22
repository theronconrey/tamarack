# SPDX-License-Identifier: GPL-3.0-or-later
"""Command-line interface.

tamarack plan 4TB 4TB 8TB 8TB
tamarack plan /dev/sda=4TB /dev/sdb=4TB /dev/sdc=8TB --commands
tamarack plan 4TB 8TB --json
tamarack grow /dev/sda=4TB --add /dev/sdb=4TB --commands
tamarack plan /dev/sda=4TB /dev/sdb=4TB --layout > layout.json
tamarack grow --from layout.json --add /dev/sdc=4TB --layout-out next.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .commands import render
from .grow import GrowPlan, plan_add_drive, render_grow
from .layout import Layout, layout_from_plan
from .model import Level, Plan, Protection
from .planner import make_plan
from .units import format_size, parse_size

_PROTECTION_TEXT = {
    Protection.SAME_DRIVE_MIRROR: "two copies on one drive (bad sectors only)",
    Protection.ONE_DRIVE: "survives one drive failure",
    Protection.TWO_DRIVE: "survives two drive failures",
}

_LEVEL_TEXT = {
    Level.RAID1: "mirror",
    Level.RAID5: "single parity",
    Level.RAID6: "dual parity",
    Level.UNUSED: "unused",
}


def parse_drive(arg: str, position: int) -> tuple[str, int]:
    """``/dev/sda=4TB`` -> (device, bytes); a bare ``4TB`` gets a placeholder."""
    if "=" in arg:
        device, size = arg.rsplit("=", 1)
        if not device:
            raise ValueError(f"missing device in {arg!r}")
        return device, parse_size(size)
    return f"disk{position}", parse_size(arg)


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "protection": plan.protection.value,
        "drive_failures_tolerated": plan.drive_failures_tolerated,
        "raw_bytes": plan.raw_total,
        "usable_bytes_nominal": plan.usable_nominal,
        "usable_bytes_estimate": plan.usable_estimate,
        "integrity_overhead_estimate": plan.integrity_overhead,
        "drives": [
            {"device": d.device, "size": d.size, "usable": d.usable, "bucket": d.bucket}
            for d in plan.drives
        ],
        "layers": [
            {
                "index": layer.index,
                "level": layer.level.value,
                "slice_bytes": layer.slice_size,
                "members": [plan.drives[i].device for i in layer.members],
                "copies_on_same_drive": layer.copies_on_same_drive,
                "usable_bytes_nominal": layer.usable_nominal,
            }
            for layer in plan.layers
        ],
        "warnings": plan.warnings,
    }


def format_plan(plan: Plan) -> str:
    out = [f"Protection: {_PROTECTION_TEXT[plan.protection]} (chosen automatically)", "", "Drives:"]
    for d in plan.drives:
        out.append(f"  {d.device:<28} {format_size(d.size):>10}")
    out += ["", f"  {'Layer':<6}{'Protection':<15}{'Drives':>7}{'Slice':>12}{'Usable':>12}"]
    for layer in plan.layers:
        drives = "1 (x2)" if layer.copies_on_same_drive else str(len(layer.members))
        out.append(
            f"  {layer.index:<6}{_LEVEL_TEXT[layer.level]:<15}{drives:>7}"
            f"{format_size(layer.slice_size):>12}"
            f"{format_size(layer.usable_nominal) if layer.usable_nominal else '-':>12}"
        )
    out += [
        "",
        f"Raw capacity:    {format_size(plan.raw_total)}",
        f"Usable:          {format_size(plan.usable_estimate)} "
        f"(estimate, after {plan.integrity_overhead:.1%} for dm-integrity)",
    ]
    if plan.warnings:
        out += ["", "Warnings:"] + [f"  - {w}" for w in plan.warnings]
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tamarack", description="Drobo-style mixed-drive storage planner."
    )
    parser.add_argument("--version", action="version", version=f"tamarack {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="show the layout for a set of drives (never writes)")
    plan.add_argument(
        "drives",
        nargs="+",
        metavar="DRIVE",
        help="a size like 4TB, or DEVICE=SIZE like /dev/sda=4TB",
    )
    out = plan.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print the plan as JSON")
    out.add_argument(
        "--commands", action="store_true", help="print the commands that would build it (dry run)"
    )
    out.add_argument("--layout", action="store_true", help="print the layout file it would create")
    grow = sub.add_parser("grow", help="plan adding a drive to an existing layout (never writes)")
    grow.add_argument(
        "drives",
        nargs="*",
        metavar="DRIVE",
        help="drives of a layout Tamarack created fresh, as DEVICE=SIZE (or use --from)",
    )
    grow.add_argument("--from", dest="layout_file", metavar="FILE", help="current layout file")
    grow.add_argument("--add", required=True, metavar="DRIVE", help="the new drive, DEVICE=SIZE")
    grow.add_argument(
        "--commands", action="store_true", help="print the commands that would do it (dry run)"
    )
    grow.add_argument("--layout-out", metavar="FILE", help="write the resulting layout file here")
    return parser


def _failures_text(n: int) -> str:
    if n == 0:
        return "no drive-failure protection"
    return f"survives {n} drive failure{'s' if n != 1 else ''}"


def format_grow(plan: GrowPlan) -> str:
    before = plan.before
    out = [
        f"Adding {plan.new_device} to {', '.join(d.device for d in before.drives)}",
        "",
        f"Before: {_failures_text(before.drive_failures_tolerated)}, "
        f"{format_size(plan.usable_before)} usable",
        f"After:  {_failures_text(plan.drive_failures_tolerated)}, "
        f"{format_size(plan.usable_after)} usable (before dm-integrity overhead)",
        "",
        "Steps:",
    ]
    out += [f"  {i}. {step.title}" for i, step in enumerate(plan.steps, start=1)]
    if plan.warnings:
        out += ["", "Warnings:"] + [f"  - {w}" for w in plan.warnings]
    out += ["", "Run with --commands to see every command (nothing is executed)."]
    return "\n".join(out)


def _main_plan(args: argparse.Namespace) -> str:
    parsed = [parse_drive(a, i) for i, a in enumerate(args.drives, start=1)]
    plan = make_plan([s for _, s in parsed], [d for d, _ in parsed])
    if args.json:
        return json.dumps(plan_to_dict(plan), indent=2)
    if args.layout:
        return layout_from_plan(plan).to_json()
    if args.commands:
        return "\n".join(render(plan))
    return format_plan(plan)


def _current_layout(args: argparse.Namespace) -> Layout:
    if args.layout_file and args.drives:
        raise ValueError("give either --from FILE or the current drives, not both")
    if args.layout_file:
        try:
            with open(args.layout_file, encoding="utf-8") as f:
                return Layout.from_json(f.read())
        except OSError as exc:
            raise ValueError(f"cannot read {args.layout_file}: {exc.strerror}") from exc
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{args.layout_file} is not a valid layout file") from exc
    if not args.drives:
        raise ValueError("give the current drives or --from FILE")
    parsed = [parse_drive(a, i) for i, a in enumerate(args.drives, start=1)]
    return layout_from_plan(make_plan([s for _, s in parsed], [d for d, _ in parsed]))


def _main_grow(args: argparse.Namespace) -> str:
    current = _current_layout(args)
    new_device, new_size = parse_drive(args.add, len(current.drives) + 1)
    plan = plan_add_drive(current, new_size, new_device)
    if args.layout_out:
        try:
            with open(args.layout_out, "w", encoding="utf-8") as f:
                f.write(plan.after.to_json() + "\n")
        except OSError as exc:
            raise ValueError(f"cannot write {args.layout_out}: {exc.strerror}") from exc
    return "\n".join(render_grow(plan)) if args.commands else format_grow(plan)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = _main_grow(args) if args.command == "grow" else _main_plan(args)
    except ValueError as exc:
        print(f"tamarack: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
