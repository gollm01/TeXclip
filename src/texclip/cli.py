"""Command-line interface:  texclip 'E = mc^2'"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .core import (FONTS, FROM_PREAMBLE, PASTE_MODES, Options, build, from_preamble, missing_tools,
                   normalize_color, to_clipboard)


def handle(eq: str, a: Options, name: str | None = None) -> bool:
    try:
        out = build(eq, a, name)
    except (RuntimeError, ValueError) as e:
        print(e, file=sys.stderr)
        return False
    clip = (not a.no_clip) and to_clipboard(out, a.paste)
    print(f"{out}" + (f"   [copied as {a.paste} -> Ctrl+V in Affinity]" if clip else ""))
    return True


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="texclip", description="TeXclip: LaTeX math -> vector graphic for Affinity")
    p.add_argument("eq", nargs="?", help="LaTeX math (without $...$)")
    p.add_argument("-i", "--interactive", action="store_true", help="type equations in a loop")
    p.add_argument("-f", "--file", help="file with one equation per line (writes files only)")
    p.add_argument("-s", "--size", default="10",
                   help="font size in pt (default 10), or 'preamble' to keep the preamble's size")
    p.add_argument("-c", "--color", default="000000",
                   help="hex colour, e.g. 1f4e79 (default black), or 'preamble'")
    p.add_argument("--font", default="cm", choices=[*FONTS, FROM_PREAMBLE],
                   help="font style (see --fonts), or 'preamble' to keep the preamble's font")
    p.add_argument("--bold", action="store_true", help="bold math (\\boldmath)")
    p.add_argument("--inline", action="store_true", help="text style instead of display style")
    p.add_argument("--paste", default="object", choices=PASTE_MODES,
                   help="object = one scalable object (default), curves = editable glyphs")
    p.add_argument("--format", default="svg", choices=["svg", "pdf"], help="default svg")
    p.add_argument("--border", type=float, default=0.5, help="padding in pt")
    p.add_argument("--no-clip", action="store_true", help="only write the file")
    p.add_argument("--fonts", action="store_true", help="list font styles and exit")
    p.add_argument("--shortcut", action="store_true", help="create a desktop shortcut with the TeXclip icon and exit")
    p.add_argument("--version", action="version", version=f"texclip {__version__}")
    args = p.parse_args(argv)

    if args.shortcut:
        from .shortcut import create_shortcut
        try:
            print("created", create_shortcut())
        except RuntimeError as e:
            sys.exit(str(e))
        return
    if args.fonts:
        for k, (_, d) in FONTS.items():
            print(f"  {k:9s} {d}")
        return
    try:
        color = FROM_PREAMBLE if from_preamble(args.color) else normalize_color(args.color)
        size = FROM_PREAMBLE if from_preamble(args.size) else float(args.size)
    except ValueError as e:
        p.error(str(e))
    a = Options(size=size, color=color, font=args.font, bold=args.bold, inline=args.inline,
                border=args.border, paste=args.paste, format=args.format, no_clip=args.no_clip)

    if miss := missing_tools(("latex", "dvisvgm") if a.format == "svg" else ("pdflatex",)):
        sys.exit(f"missing on PATH: {', '.join(miss)} - install a TeX distribution (MiKTeX / TeX Live)")

    if args.file:
        eqs = [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()]
        a.no_clip = True
        for k, eq in enumerate([e for e in eqs if e and not e.startswith("%")], 1):
            handle(eq, a, name=f"{Path(args.file).stem}_{k:02d}")
    elif args.interactive or not args.eq:
        print(f"texclip - size {a.size}, colour {a.color}, font {a.font}, paste={a.paste}. "
              "Enter LaTeX, Enter to copy, Ctrl+C to quit")
        while True:
            try:
                eq = input("$ ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if eq:
                handle(eq, a)
    else:
        sys.exit(0 if handle(args.eq, a) else 1)


if __name__ == "__main__":
    main()
