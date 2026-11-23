"""TeXclip core: LaTeX math -> tightly cropped vector SVG (glyphs as paths) -> clipboard.

No third-party dependencies. Needs ``latex`` and ``dvisvgm`` (MiKTeX / TeX Live / MacTeX);
``dvipng`` is only needed for the GUI preview, ``pdflatex`` only for ``--format pdf``.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

# --------------------------------------------------------------------------- configuration
BASE_PREAMBLE = r"""\usepackage{amsmath,amssymb,mathtools}
\usepackage{physics}
\usepackage{xcolor}"""

FONTS = {  # key: (preamble, description)
    "cm":       (r"\usepackage{amsfonts}", "Computer Modern (LaTeX default)"),
    "lm":       (r"\usepackage{lmodern}\usepackage{amsfonts}", "Latin Modern (CM, slightly finer)"),
    "times":    (r"\usepackage{newtxtext,newtxmath}", "Times-like (newtx)"),
    "palatino": (r"\usepackage{newpxtext,newpxmath}", "Palatino-like (newpx)"),
    "utopia":   (r"\usepackage{fourier}", "Utopia (fourier)"),
    "kp":       (r"\usepackage{kpfonts}", "Kp-Fonts"),
    "euler":    (r"\usepackage{amsfonts}\usepackage{eulervm}", "AMS Euler (upright, handwritten feel)"),
    "sans":     (r"\usepackage{cmbright}", "CM Bright (sans-serif)"),
}

# newtx / newpx define the amssymb symbols themselves; loading amssymb as well fails (\Bbbk clash)
NO_AMSSYMB = {"times", "palatino"}

# user-preamble packages that bring their own amssymb symbols (font = "preamble")
NO_AMSSYMB_PACKAGES = {"newtxmath", "newpxmath", "newtx", "newpx", "stix", "stix2", "libertinust1math"}
# dvisvgm on MiKTeX finds the CM Bright outlines only through this map
EXTRA_FONTMAPS = {"sans": "hfbright.map"}

PASTE_MODES = ("object", "curves")

# value for size / color / font meaning "do not override, take it from the preamble file"
FROM_PREAMBLE = "preamble"

TEMPLATE = r"""\RequirePackage{{fix-cm}}
\documentclass[border={bp}pt]{{standalone}}
{preamble}
\begin{{document}}
{style}
{body}
\end{{document}}
"""

OUTDIR = Path(os.environ.get("TEXCLIP_DIR", Path.home() / "texclip"))


def _default_preamble_file() -> Path:
    """~/.texclip_preamble.tex, or the file of the project's earlier name if only that one exists."""
    new, old = Path.home() / ".texclip_preamble.tex", Path.home() / (".tex2" + "aff_preamble.tex")
    return old if old.exists() and not new.exists() else new


PREAMBLE_FILE = Path(os.environ.get("TEXCLIP_PREAMBLE", _default_preamble_file()))

# packages whose job is to choose a font; the GUI/CLI "font" setting replaces them
FONT_PACKAGES = {
    "lmodern", "newtxtext", "newtxmath", "newpxtext", "newpxmath", "fourier", "kpfonts",
    "eulervm", "cmbright", "mathptmx", "mathpazo", "palatino", "times", "libertine",
    "libertinust1math", "stix", "stix2", "txfonts", "pxfonts", "arev", "helvet", "avant",
    "bookman", "charter", "mathdesign", "concmath", "cm-super",
}
# commands that fix size or colour; the "size"/"colour" settings replace them
_COLOR_CMD = re.compile(r"\\(?:color|pagecolor|normalcolor)\b")
_SIZE_CMD = re.compile(r"\\fontsize\b")
_SIZE_SWITCH = re.compile(r"^\s*\\(?:tiny|scriptsize|footnotesize|small|normalsize|large|Large|LARGE|huge|Huge)\s*$")
_USEPACKAGE = re.compile(r"\\usepackage(\[[^\]]*\])?\{([^}]*)\}")


@dataclass
class Options:
    """Everything that shapes one rendering. Mirrors the CLI flags.

    ``size``, ``color`` and ``font`` may be ``"preamble"`` (FROM_PREAMBLE): then TeXclip does not
    override that setting and whatever the preamble file says is used."""
    size: float | str = 10.0    # font size in pt
    color: str = "000000"       # hex RRGGBB
    font: str = "cm"            # key of FONTS
    bold: bool = False          # \boldmath
    inline: bool = False        # text style instead of display style
    border: float = 0.5         # padding in pt
    paste: str = "object"       # object (one compound path) | curves (one path per glyph)
    format: str = "svg"         # svg | pdf
    no_clip: bool = False       # only write the file

    def copy(self, **kw) -> "Options":
        return replace(self, **kw)


def from_preamble(value) -> bool:
    """True if a size / colour / font setting is left to the preamble file."""
    return isinstance(value, str) and value.strip().lower() == FROM_PREAMBLE


# --------------------------------------------------------------------------- helpers
_HEX = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def normalize_color(value: str) -> str:
    """'#1f4e79' / '1F4' -> '1F4E79'. Raises ValueError for anything else."""
    m = _HEX.match(value.strip())
    if not m:
        raise ValueError(f"not a hex colour: {value!r}")
    h = m.group(1).upper()
    return "".join(c * 2 for c in h) if len(h) == 3 else h


def _nowindow() -> dict:
    """Keep console windows from flashing when launched from the GUI on Windows."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", **_nowindow())
    if r.returncode != 0:
        raise RuntimeError(r.stdout[-3000:] + r.stderr[-2000:])
    return r


def latex_error(log: str) -> str:
    lines = log.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("!"):
            return "\n".join(lines[i:i + 4])
    return log[-1500:]


def missing_tools(need=("latex", "dvisvgm")) -> list[str]:
    return [t for t in need if not shutil.which(t)]


# --------------------------------------------------------------------------- preamble
def strip_overrides(text: str, font: bool = True, size: bool = True,
                    color: bool = True) -> tuple[str, list[str]]:
    """Neutralise font/size/colour settings in a user preamble.

    Settings chosen in TeXclip must win over whatever the personal preamble says. For every
    category that is overridden (``font``/``size``/``color`` True), font packages are dropped
    from ``\\usepackage`` lists and lines that set the size or colour are commented out.
    Size lines that are *kept* are wrapped in ``\\AtBeginDocument`` because LaTeX resets the
    size at ``\\begin{document}``. Returns ``(clean_text, removed)``.
    """
    removed: list[str] = []

    def fix_use(m: re.Match) -> str:
        opts, pkgs = m.group(1) or "", [p.strip() for p in m.group(2).split(",") if p.strip()]
        drop = [p for p in pkgs if p in FONT_PACKAGES] if font else []
        keep = [p for p in pkgs if p not in drop]
        removed.extend(drop)
        return f"\\usepackage{opts}{{{','.join(keep)}}}" if keep else ""

    out = []
    for line in text.splitlines():
        m = re.search(r"(?<!\\)%", line)                    # split off an unescaped % comment
        code, comment = (line[:m.start()], line[m.start():]) if m else (line, "")
        new = _USEPACKAGE.sub(fix_use, code)
        is_size = bool(_SIZE_CMD.search(new) or _SIZE_SWITCH.match(new))
        is_color = bool(_COLOR_CMD.search(new))
        if (is_size and size) or (is_color and color):
            removed.append(new.strip())
            out.append("% overridden by texclip settings: " + line.strip())
            continue
        if is_size and "AtBeginDocument" not in new:
            new = f"\\AtBeginDocument{{{new.strip()}}}"
        out.append((new + comment).rstrip())
    return "\n".join(out).strip(), removed


def _loaded_packages(text: str) -> set[str]:
    code = "\n".join(re.split(r"(?<!\\)%", l)[0] for l in text.splitlines())
    return {p.strip() for m in _USEPACKAGE.finditer(code) for p in m.group(2).split(",")}


def read_user_preamble(path: Path | None = None) -> str:
    p = path or PREAMBLE_FILE
    return p.read_text(encoding="utf-8") if p.exists() else ""


def install_preamble(src: str | Path, dest: Path | None = None) -> Path | None:
    """Make ``src`` the personal preamble (copied over PREAMBLE_FILE).

    A ``\\documentclass`` line and everything from ``\\begin{document}`` on are dropped, so a
    complete .tex file works too. The previous preamble is kept as ``<name>.bak``; its path is
    returned (None if there was none)."""
    dest = dest or PREAMBLE_FILE
    src = Path(src)
    text = src.read_text(encoding="utf-8", errors="replace")
    text = re.split(r"\\begin\{document\}", text)[0]
    text = "\n".join(l for l in text.splitlines() if not re.match(r"\s*\\documentclass", l))
    backup = None
    if dest.exists():
        if dest.resolve() == src.resolve():
            return None
        backup = dest.with_name(dest.name + ".bak")
        shutil.copy2(dest, backup)
    dest.write_text(f"% TeXclip preamble, loaded from {src.name}\n" + text.strip() + "\n", encoding="utf-8")
    return backup


def build_preamble(a: Options, user_preamble: str | None = None) -> str:
    """The personalised preamble: chosen font, base packages, user preamble (with overrides
    removed) and the helpers the size option needs. No \\documentclass, so it can be pasted
    into any document."""
    user = read_user_preamble() if user_preamble is None else user_preamble
    pf, ps, pc = from_preamble(a.font), from_preamble(a.size), from_preamble(a.color)
    if not pf and a.font not in FONTS:
        raise ValueError(f"unknown font {a.font!r}")
    user, _ = strip_overrides(user, font=not pf, size=not ps, color=not pc)
    no_amssymb = (a.font in NO_AMSSYMB) or (pf and _loaded_packages(user) & NO_AMSSYMB_PACKAGES)
    size_txt = FROM_PREAMBLE if ps else f"{float(a.size):g}pt"
    color_txt = FROM_PREAMBLE if pc else "#" + normalize_color(a.color)
    parts = [
        f"% --- texclip preamble: font={a.font}, size={size_txt}, colour={color_txt}",
        "" if pf else FONTS[a.font][0],
        BASE_PREAMBLE.replace("amssymb,", "") if no_amssymb else BASE_PREAMBLE,
        user,
        r"\usepackage{bm,anyfontsize}",
        "" if pc else
        f"\\definecolor{{eqcolor}}{{HTML}}{{{normalize_color(a.color)}}}   % use as \\color{{eqcolor}}",
    ]
    return "\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- LaTeX source
_ENVS = (("align", "aligned"), ("gather", "gathered"), ("multline", "gathered"),
         ("equation", None), ("eqnarray", "aligned"))


def make_body(eq: str, inline: bool = False) -> str:
    """Wrap math in $...$. Multi-line environments become their inner-math counterparts so the
    standalone box still shrinks to fit."""
    for env, inner in _ENVS:
        eq_ = re.sub(rf"\\(begin|end)\{{{env}\*?\}}",
                     (lambda m, i=inner: f"\\{m.group(1)}{{{i}}}") if inner else "", eq)
        if eq_ != eq:
            return f"$\\displaystyle {eq_}$"
    return f"${eq}$" if inline else f"$\\displaystyle {eq}$"


def make_tex(eq: str, a: Options, user_preamble: str | None = None) -> str:
    style = ""
    if not from_preamble(a.size):
        size = float(a.size)
        if size <= 0:
            raise ValueError("size must be positive")
        style += f"\\fontsize{{{size:g}}}{{{round(size * 1.2, 2):g}}}\\selectfont"
    if a.bold:
        style += r"\boldmath"
    if not from_preamble(a.color):
        style += f"\n\\color[HTML]{{{normalize_color(a.color)}}}"
    return TEMPLATE.format(bp=a.border, preamble=build_preamble(a, user_preamble),
                           style=style.strip("\n"), body=make_body(eq, a.inline))


# --------------------------------------------------------------------------- building
def _tmpdir():
    """Scratch folder; a compile still running when the app closes must not raise on cleanup."""
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


def _compile_dvi(tex: str, tmp: str) -> None:
    (Path(tmp) / "eq.tex").write_text(tex, encoding="utf-8")
    try:
        run(["latex", "-interaction=nonstopmode", "-halt-on-error", "eq.tex"], tmp)
    except RuntimeError as e:
        raise RuntimeError("LaTeX error:\n" + latex_error(str(e))) from None


def build(eq: str, a: Options, name: str | None = None) -> Path:
    """Compile ``eq`` and write ``<name>.svg|pdf`` (+ ``<name>.tex``) to OUTDIR."""
    tex = make_tex(eq, a)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    name = name or "eq_" + hashlib.sha1(tex.encode()).hexdigest()[:8]
    with _tmpdir() as tmp:
        (OUTDIR / f"{name}.tex").write_text(eq + "\n", encoding="utf-8")  # source for later edits
        if a.format == "svg":
            _compile_dvi(tex, tmp)       # DVI -> SVG: glyphs as paths, no Ghostscript needed
            if not shutil.which("dvisvgm"):
                raise RuntimeError("dvisvgm not found (install it via the MiKTeX Console)")
            out = OUTDIR / f"{name}.svg"
            cmd = ["dvisvgm", "--no-fonts", "--exact-bbox", "-o", str(out), "eq.dvi"]
            if a.font in EXTRA_FONTMAPS:
                cmd.insert(1, f"--fontmap=+{EXTRA_FONTMAPS[a.font]}")
            r = run(cmd, tmp)
            # a font without outlines gives an SVG with missing glyphs - fail loudly instead
            bad = [l.strip() for l in (r.stdout + r.stderr).splitlines()
                   if "failed to create" in l or ("font" in l and "not found" in l and "map" not in l)]
            if bad:
                out.unlink(missing_ok=True)
                raise RuntimeError("dvisvgm could not find glyph outlines for this font:\n" + "\n".join(bad))
            return out
        (Path(tmp) / "eq.tex").write_text(tex, encoding="utf-8")
        try:
            run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "eq.tex"], tmp)
        except RuntimeError as e:
            raise RuntimeError("LaTeX error:\n" + latex_error(str(e))) from None
        pdf = Path(tmp) / "eq.pdf"
        gs = next((shutil.which(g) for g in ("gs", "gswin64c", "gswin32c", "mgs") if shutil.which(g)), None)
        if gs:   # outline glyphs so Affinity does not substitute fonts
            o = Path(tmp) / "eq_outl.pdf"
            run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dNoOutputFonts",
                 "-sDEVICE=pdfwrite", f"-sOutputFile={o}", str(pdf)], tmp)
            pdf = o
        else:
            print("warning: Ghostscript not found - PDF keeps fonts; use SVG instead", file=sys.stderr)
        out = OUTDIR / f"{name}.pdf"
        shutil.copy(pdf, out)
        return out


def render_png(eq: str, a: Options, dpi: int = 200, user_preamble: str | None = None) -> bytes:
    """Fast raster preview (latex -> dvipng, transparent background). Returns PNG bytes."""
    if not shutil.which("dvipng"):
        raise RuntimeError("dvipng not found (install it via the MiKTeX Console); preview disabled")
    tex = make_tex(eq, a, user_preamble)
    with _tmpdir() as tmp:
        _compile_dvi(tex, tmp)
        run(["dvipng", "-T", "tight", "-D", str(dpi), "-bg", "Transparent", "-q*",
             "-o", "eq.png", "eq.dvi"], tmp)
        return (Path(tmp) / "eq.png").read_bytes()


# --------------------------------------------------------------------------- clipboard
# --------------------------------------------------------------------------- SVG flattening
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_XLINK = "{http://www.w3.org/1999/xlink}href"


def _parse_transform(t: str | None):
    """SVG transform list -> affine (a, b, c, d, e, f)."""
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", t or ""):
        v = [float(x) for x in re.findall(_NUM, args)]
        if name == "translate":
            n = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            n = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "matrix":
            n = tuple(v)
        elif name == "rotate":
            import math
            a = math.radians(v[0])
            n = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                m = _mul(m, (1, 0, 0, 1, v[1], v[2]))
                m = _mul(m, n)
                n = (1, 0, 0, 1, -v[1], -v[2])
        else:
            raise ValueError(f"transform {name} not supported")
        m = _mul(m, n)
    return m


def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + c * B, b * A + d * B, a * C + c * D, b * C + d * D, a * E + c * F + e, b * E + d * F + f)


def _path_abs(d: str, m) -> str:
    """Rewrite path data with all coordinates transformed by m, as absolute M/L/C/Q/Z."""
    a, b, c, dd, e, f = m
    tp = lambda x, y: (a * x + c * y + e, b * x + dd * y + f)
    fmt = lambda p: f"{p[0]:.3f} {p[1]:.3f}"
    toks = re.findall(r"[A-Za-z]|" + _NUM, d)
    out, i, cmd = [], 0, ""
    cx = cy = sx = sy = 0.0
    last_ctrl = None                    # (x, y, kind) for S/T reflection
    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
        rel = cmd.islower()
        C = cmd.upper()

        def nums(k):
            nonlocal i
            vals = [float(x) for x in toks[i:i + k]]
            i += k
            return vals
        if C == "Z":
            out.append("Z")
            cx, cy, last_ctrl = sx, sy, None
            continue
        if C in "ML":
            x, y = nums(2)
            if rel:
                x, y = cx + x, cy + y
            out.append(("M " if C == "M" else "L ") + fmt(tp(x, y)))
            cx, cy, last_ctrl = x, y, None
            if C == "M":
                sx, sy = x, y
                cmd = "l" if rel else "L"       # further pairs are line-tos
        elif C in "HV":
            (v,) = nums(1)
            if C == "H":
                cx = cx + v if rel else v
            else:
                cy = cy + v if rel else v
            out.append("L " + fmt(tp(cx, cy)))
            last_ctrl = None
        elif C in "CS":
            if C == "C":
                x1, y1, x2, y2, x, y = nums(6)
                if rel:
                    x1, y1, x2, y2, x, y = cx + x1, cy + y1, cx + x2, cy + y2, cx + x, cy + y
            else:
                x2, y2, x, y = nums(4)
                if rel:
                    x2, y2, x, y = cx + x2, cy + y2, cx + x, cy + y
                x1, y1 = (2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1]) if last_ctrl and last_ctrl[2] == "C" else (cx, cy)
            out.append("C " + " ".join(fmt(tp(*p)) for p in ((x1, y1), (x2, y2), (x, y))))
            cx, cy, last_ctrl = x, y, (x2, y2, "C")
        elif C in "QT":
            if C == "Q":
                x1, y1, x, y = nums(4)
                if rel:
                    x1, y1, x, y = cx + x1, cy + y1, cx + x, cy + y
            else:
                x, y = nums(2)
                if rel:
                    x, y = cx + x, cy + y
                x1, y1 = (2 * cx - last_ctrl[0], 2 * cy - last_ctrl[1]) if last_ctrl and last_ctrl[2] == "Q" else (cx, cy)
            out.append("Q " + " ".join(fmt(tp(*p)) for p in ((x1, y1), (x, y))))
            cx, cy, last_ctrl = x, y, (x1, y1, "Q")
        else:
            raise ValueError(f"path command {cmd} not supported")
    return " ".join(out)


def _outer_sign(d: str) -> int:
    """Orientation (+1/-1) of the largest subpath of absolute path data (end points only)."""
    best, sign = 0.0, 0
    for sub in re.split(r"(?=M )", d):
        nums = [float(x) for x in re.findall(_NUM, sub)]
        pts = list(zip(nums[0::2], nums[1::2]))
        if len(pts) < 3:
            continue
        area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))
        if abs(area) > best:
            best, sign = abs(area), (1 if area > 0 else -1)
    return sign


def _rect_path(x, y, w, h, m, positive: bool) -> str:
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    d = "M{} {}L{} {}L{} {}L{} {}Z".format(*[v for p in corners for v in p])
    out = _path_abs(d, m)
    return out if (_outer_sign(out) > 0) == positive else _path_abs(
        "M{} {}L{} {}L{} {}L{} {}Z".format(*[v for p in corners[::-1] for v in p]), m)


def flatten_svg(svg_text: str, merge: bool = True) -> str:
    """Resolve the glyph references of a dvisvgm SVG into plain paths.

    ``merge=True``: one compound path per colour (one object in Affinity);
    ``merge=False``: one path per glyph / rule. Raises ValueError for anything unexpected."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(svg_text)
    defs = {el.get("id"): el for el in root.iter() if el.get("id")}
    pieces: list[tuple[str, str]] = []           # (colour, path data)

    def glyph(el, m, fill, depth=0):
        tag = el.tag.rsplit("}", 1)[-1]
        if depth > 20:
            raise ValueError("reference loop")
        fill = el.get("fill", fill)
        m = _mul(m, _parse_transform(el.get("transform")))
        if tag == "use":
            ref = defs.get((el.get(_XLINK) or el.get("href") or "#")[1:])
            if ref is None:
                raise ValueError("dangling <use>")
            m = _mul(m, (1, 0, 0, 1, float(el.get("x", 0)), float(el.get("y", 0))))
            glyph(ref, m, fill, depth + 1)
        elif tag == "path":
            pieces.append((fill, _path_abs(el.get("d", ""), m)))
        elif tag == "rect":       # rules (fraction bars, roots): drawn later in glyph direction
            x, y, w, h = (float(el.get(k, 0)) for k in ("x", "y", "width", "height"))
            pieces.append((fill, ("rect", x, y, w, h, m)))
        elif tag == "g":
            for ch in el:
                glyph(ch, m, fill, depth)
        elif tag in ("defs", "title", "desc", "style"):
            return
        else:
            raise ValueError(f"<{tag}> not supported")

    page = next((e for e in root if e.tag.rsplit("}", 1)[-1] == "g"), None)
    if page is None:
        raise ValueError("no drawing found")
    glyph(page, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), root.get("fill", "#000"))
    # In one compound path (nonzero fill) a rule overlapping a glyph must run in the same
    # direction as the glyph outlines, otherwise the overlap becomes a hole.
    signs = [_outer_sign(d) for _, d in pieces if isinstance(d, str)]
    cw = sum(signs) >= 0
    pieces = [(col, d if isinstance(d, str) else _rect_path(*d[1:], cw)) for col, d in pieces]
    if merge:
        by_color: dict[str, list[str]] = {}
        for col, d in pieces:
            by_color.setdefault(col, []).append(d)
        paths = [f'<path d="{" ".join(ds)}" fill="{col}"/>' for col, ds in by_color.items()]
    else:
        paths = [f'<path d="{d}" fill="{col}"/>' for col, d in pieces]
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{root.get("width")}" '
            f'height="{root.get("height")}" viewBox="{root.get("viewBox")}">')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + head + "\n" + "\n".join(paths) + "\n</svg>\n"


def clipboard_svg(path: Path, mode: str) -> str:
    """SVG text to put on the clipboard for a paste mode (falls back to the file as is)."""
    text = path.read_text(encoding="utf-8")
    try:
        return flatten_svg(text, merge=(mode != "curves"))
    except (ValueError, IndexError, KeyError) as e:   # unexpected SVG structure: keep the original
        print(f"texclip: could not flatten SVG ({e}); copying it unchanged", file=sys.stderr)
        return text


# Affinity reads SVG from the clipboard under Inkscape's and Illustrator's format names (not
# image/svg+xml), and PDF under "Portable Document Format".
SVG_CLIP_FORMATS = ("image/x-inkscape-svg", "Scalable Vector Graphics", "image/svg+xml")


def to_clipboard(path: Path, mode: str) -> bool:
    if sys.platform == "win32":
        ps = ["Add-Type -AssemblyName System.Windows.Forms",
              "$d = New-Object System.Windows.Forms.DataObject"]
        tmp = None
        if path.suffix == ".svg":
            fd, name = tempfile.mkstemp(suffix=".svg")
            os.close(fd)
            tmp = Path(name)
            tmp.write_text(clipboard_svg(path, mode), encoding="utf-8")
            q = str(tmp).replace("'", "''")
            ps.append(f"$b = [IO.File]::ReadAllBytes('{q}')")
            ps += [f"$d.SetData('{fmt}', (New-Object IO.MemoryStream(,$b)))" for fmt in SVG_CLIP_FORMATS]
        else:
            q = str(path).replace("'", "''")
            ps += [f"$b = [IO.File]::ReadAllBytes('{q}')",
                   "$d.SetData('Portable Document Format', (New-Object IO.MemoryStream(,$b)))"]
        ps.append("[System.Windows.Forms.Clipboard]::SetDataObject($d, $true)")
        try:
            subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", "; ".join(ps)],
                           check=True, **_nowindow())
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
        return True
    if sys.platform == "darwin":
        cls = "«class PDF »" if path.suffix == ".pdf" else "«class utf8»"
        subprocess.run(["osascript", "-e",
                        f'set the clipboard to (read (POSIX file "{path}") as {cls})'], check=True)
        return True
    if shutil.which("xclip"):
        mime = "application/pdf" if path.suffix == ".pdf" else "image/svg+xml"
        subprocess.run(["xclip", "-selection", "clipboard", "-t", mime, "-i", str(path)], check=True)
        return True
    return False
