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
import threading
from collections import OrderedDict
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

# Where the bottom of the SVG box lies (Affinity puts it on the text baseline when the equation
# is pasted into a text frame):
#   ink            lowest ink, e.g. the g of t_g (dvisvgm --exact-bbox) - the original behaviour
#   baseline       the TeX box + border (--bbox=papersize); TeXclip reports the baseline shift
#   baseline-crop  experimental: TeX box, cut off at the math baseline, overflow="visible"
ANCHORS = ("baseline", "ink", "baseline-crop")
TEX_PT_TO_BP = 72 / 72.27

# The math is set in a box first so its height/depth/width can be written to the log.
TEMPLATE = r"""\RequirePackage{{fix-cm}}
\documentclass[border={bp}pt]{{standalone}}
{preamble}
\newsavebox\texclipbox
\begin{{document}}
{style}
\sbox\texclipbox{{{body}}}%
\typeout{{TEXCLIP-DIMS: ht=\the\ht\texclipbox; dp=\the\dp\texclipbox; wd=\the\wd\texclipbox}}%
{output}
\end{{document}}
"""
# baseline modes ship the measured box; "ink" typesets the math directly as before, so its SVG
# stays byte-identical to earlier versions (the box is then only used for measuring)
_OUTPUT = {"ink": "{body}", "default": r"\usebox\texclipbox"}
_DIMS = re.compile(r"TEXCLIP-DIMS: ht=([-\d.]+)pt; dp=([-\d.]+)pt; wd=([-\d.]+)pt")

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
    anchor: str = "baseline"    # baseline | ink | baseline-crop  (see ANCHORS)
    # text blocks: a whole paragraph (text with $...$ math) set by LaTeX in a frame
    mode: str = "math"          # math | text
    width: str = "80mm"         # frame width (mm, cm, in, pt = Affinity pt)
    height: str = ""            # frame height; "" = as tall as the text
    align: str = "justify-left"  # key of ALIGNMENTS
    leading: float = 1.2        # line spacing as a multiple of the font size
    parskip: float = 0.0        # space between paragraphs, pt
    indent: float = 0.0         # first-line indent, pt
    hyphenate: bool = True

    def copy(self, **kw) -> "Options":
        return replace(self, **kw)


@dataclass
class Dims:
    """Size of the typeset math in TeX pt (from the TEXCLIP-DIMS log line)."""
    ht: float
    dp: float
    wd: float


@dataclass
class BuildResult:
    path: Path
    dims: Dims | None
    shift: float | None         # baseline shift for Affinity in pt (None for anchor "ink")
    overflow: float | None = None   # text blocks: pt the text is taller than the fixed frame


def baseline_shift(dims: Dims | None, a: Options) -> float | None:
    """Affinity puts the bottom of the pasted box on the text baseline; the box bottom lies
    dp + border below the math baseline, so the equation has to move down by that much
    (TeX pt -> Affinity pt = bp). None for anchor "ink" (no reference point)."""
    if dims is None or a.anchor == "ink" or a.mode == "text":
        return None
    if a.anchor == "baseline-crop":
        return 0.0
    v = round(-(dims.dp + float(a.border)) * TEX_PT_TO_BP, 2)
    return 0.0 if v == 0 else v


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


TIMEOUT = float(os.environ.get("TEXCLIP_TIMEOUT", 90))    # s; MiKTeX may install packages on first use
_RUNNING: dict[str, set] = {}                             # tag -> running processes (for cancel())
_CANCELLED: set = set()
_LOCK = threading.Lock()


class Cancelled(RuntimeError):
    """The job was superseded (e.g. a newer preview) and its processes were stopped."""


def cancel(tag: str | None = None) -> None:
    """Stop every running external process of `tag` (all tags if None)."""
    with _LOCK:
        for t, procs in _RUNNING.items():
            if tag is None or t == tag:
                for p in list(procs):
                    _CANCELLED.add(id(p))
                    try:
                        p.kill()
                    except OSError:
                        pass


def run(cmd, cwd, tag: str = "job", timeout: float | None = None):
    """Run an external program; raise RuntimeError on failure, Cancelled if cancel(tag) stopped it."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace", **_nowindow())
    with _LOCK:
        _RUNNING.setdefault(tag, set()).add(p)
    try:
        out, err = p.communicate(timeout=timeout or TIMEOUT)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise RuntimeError(f"{Path(cmd[0]).name} did not finish within {timeout or TIMEOUT:.0f} s. "
                           "If MiKTeX is installing a package, wait a moment and try again.") from None
    finally:
        with _LOCK:
            _RUNNING.get(tag, set()).discard(p)
    if id(p) in _CANCELLED:
        _CANCELLED.discard(id(p))
        raise Cancelled("superseded")
    r = subprocess.CompletedProcess(cmd, p.returncode, out, err)
    if r.returncode != 0:
        raise RuntimeError(out[-3000:] + err[-2000:])
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


# --------------------------------------------------------------------------- text blocks
# Alignments as in Affinity's Paragraph panel. LaTeX breaks the whole paragraph at once
# (Knuth-Plass), not line by line. The "Justify Centre/Right" glue is the TeXbook trick:
# the +1fil/-1fil cancel on full lines, only the last line gets the fil from \parfillskip.
ALIGNMENTS = {
    "justify-left": ("Justify Left", r"\justifying"),
    "justify-center": ("Justify Centre",
                       r"\justifying\leftskip=0pt plus 1fil\rightskip=0pt plus -1fil\parfillskip=0pt plus 2fil"),
    "justify-right": ("Justify Right",
                      r"\justifying\leftskip=0pt plus 1fil\rightskip=0pt plus -1fil\parfillskip=0pt plus 1fil"),
    "justify-all": ("Justify All", r"\justifying\parfillskip=0pt"),
    "left": ("Align Left", r"\RaggedRight"),
    "center": ("Align Centre", r"\Centering"),
    "right": ("Align Right", r"\RaggedLeft"),
}
TEXT_PACKAGES = r"\usepackage{ragged2e}\usepackage{microtype}"
_DIM = re.compile(r"^\s*(\d+(?:\.\d*)?|\.\d+)\s*(mm|cm|in|pt|bp)?\s*$")


def tex_dim(value: str, default_unit: str = "pt") -> tuple[str, float]:
    """'80mm' / '300' / '300pt' -> (LaTeX dimension, size in Affinity pt). Affinity's pt is the
    PostScript point, i.e. TeX's bp, so 'pt' is written as bp to keep sizes identical."""
    m = _DIM.match(str(value))
    if not m:
        raise ValueError(f"not a length: {value!r} (use e.g. 80mm, 300pt)")
    num, unit = float(m.group(1)), (m.group(2) or default_unit)
    if num <= 0:
        raise ValueError("length must be positive")
    unit = "bp" if unit == "pt" else unit
    to_bp = {"bp": 1.0, "mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72.0}[unit]
    return f"{num:g}{unit}", num * to_bp


def make_text_body(text: str, a: Options) -> str:
    """A paragraph block (minipage of the chosen width) with the chosen alignment."""
    width, _ = tex_dim(a.width)
    if a.align not in ALIGNMENTS:
        raise ValueError(f"alignment must be one of {', '.join(ALIGNMENTS)}")
    # alignment first: ragged2e's commands reset \parindent to their own defaults
    setup = [ALIGNMENTS[a.align][1], rf"\setlength\parindent{{{float(a.indent):g}bp}}",
             rf"\setlength\parskip{{{float(a.parskip):g}bp}}", r"\frenchspacing"]
    if not a.hyphenate:
        setup.append(r"\hyphenpenalty=10000\exhyphenpenalty=10000")
    return rf"\begin{{minipage}}[t]{{{width}}}" + "".join(setup) + "\n" + text.strip() + "\n" + r"\end{minipage}"


def make_tex(eq: str, a: Options, user_preamble: str | None = None, preview: bool = False) -> str:
    text = a.mode == "text"
    style = ""
    if not from_preamble(a.size):
        size = float(a.size)
        if size <= 0:
            raise ValueError("size must be positive")
        lead = float(a.leading) if text else 1.2
        if lead <= 0:
            raise ValueError("line spacing must be positive")
        style += f"\\fontsize{{{size:g}}}{{{round(size * lead, 2):g}}}\\selectfont"
    if a.bold:
        style += r"\boldmath"
    if not from_preamble(a.color):
        style += f"\n\\color[HTML]{{{normalize_color(a.color)}}}"
    preamble = build_preamble(a, user_preamble)
    if text:
        body = make_text_body(eq, a)
        output = _OUTPUT["default"]
        if a.height:                         # fixed frame height, text from the top
            height, _ = tex_dim(a.height)
            width, _ = tex_dim(a.width)
            output = rf"\parbox[t][{height}][t]{{{width}}}{{\usebox\texclipbox}}"
        if preview:                          # thin grey frame so the preview shows the whole box
            output = (r"\colorlet{texclipfg}{.}{\color[HTML]{B4B4B4}\setlength\fboxsep{0pt}"
                      r"\setlength\fboxrule{0.5bp}\fbox{\color{texclipfg}" + output + "}}")
        preamble += "\n" + TEXT_PACKAGES
        border = 0                           # the box is exactly the frame
    else:
        body = make_body(eq, a.inline)
        output = _OUTPUT["ink"].format(body=body) if a.anchor == "ink" else _OUTPUT["default"]
        border = a.border
    return TEMPLATE.format(bp=border, preamble=preamble, style=style.strip("\n"), body=body, output=output)


def text_overflow(dims: Dims | None, a: Options) -> float | None:
    """How much taller (Affinity pt) the text is than the fixed frame height; None if it fits."""
    if a.mode != "text" or not a.height or dims is None:
        return None
    _, h_bp = tex_dim(a.height)
    over = (dims.ht + dims.dp) * TEX_PT_TO_BP - h_bp
    return round(over, 1) if over > 0.05 else None


# --------------------------------------------------------------------------- building
def _tmpdir():
    """Scratch folder; a compile still running when the app closes must not raise on cleanup."""
    if sys.version_info >= (3, 10):
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    return tempfile.TemporaryDirectory()


def read_dims(tmp: str) -> Dims | None:
    """The TEXCLIP-DIMS line of eq.log (None if missing, e.g. for a hand-made template)."""
    try:
        m = _DIMS.search((Path(tmp) / "eq.log").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return Dims(*(float(v) for v in m.groups())) if m else None


# --------------------------------------------------------------------------- speed
# 1. Precompiled preamble: everything before \begin{document} is dumped once into a format
#    (package mylatexformat) and loaded from it afterwards; key = hash of that preamble.
# 2. Compile cache: the DVI + log of the last documents are kept, so "Copy" after a preview
#    of the same document only runs dvisvgm.
CACHE_DIR = Path(os.environ.get("TEXCLIP_CACHE", Path.home() / ".texclip_cache"))
USE_FORMAT = os.environ.get("TEXCLIP_NO_FORMAT", "") == ""
_FMT_BUILDING: set = set()
_FMT_FAILED: set = set()
_DVI_KEEP = 40
_ENGINE: list = []


def _engine_id() -> str:
    if not _ENGINE:
        try:
            r = subprocess.run(["latex", "--version"], capture_output=True, text=True, **_nowindow())
            _ENGINE.append(r.stdout.splitlines()[0] if r.stdout else "?")
        except OSError:
            _ENGINE.append("?")
    return _ENGINE[0]


def _preamble_key(tex: str) -> str:
    head = tex.split(r"\begin{document}")[0]
    return hashlib.sha1((head + _engine_id()).encode()).hexdigest()[:16]


def _build_format(tex: str, key: str) -> None:
    """Dump the preamble of `tex` into CACHE_DIR/fmt/tc_<key>.fmt (runs in a background thread)."""
    try:
        with _tmpdir() as tmp:
            head = tex.split(r"\begin{document}")[0]
            (Path(tmp) / "pre.tex").write_text(head + "\\begin{document}\n\\end{document}\n", encoding="utf-8")
            run(["latex", "-ini", "-interaction=nonstopmode", f"-jobname=tc_{key}", "&latex",
                 "mylatexformat.ltx", "pre.tex"], tmp, tag="format", timeout=180)
            dest = CACHE_DIR / "fmt"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(Path(tmp) / f"tc_{key}.fmt"), str(dest / f"tc_{key}.fmt"))
    except (RuntimeError, OSError):
        _FMT_FAILED.add(key)            # e.g. mylatexformat missing: keep compiling normally
    finally:
        _FMT_BUILDING.discard(key)


def _format_for(tex: str) -> Path | None:
    """The cached format for this preamble, or None (then one is built in the background)."""
    if not USE_FORMAT:
        return None
    key = _preamble_key(tex)
    fmt = CACHE_DIR / "fmt" / f"tc_{key}.fmt"
    if fmt.exists():
        return fmt
    if key not in _FMT_FAILED and key not in _FMT_BUILDING:
        _FMT_BUILDING.add(key)
        threading.Thread(target=_build_format, args=(tex, key), daemon=True).start()
    return None


def _latex(tmp: str, tex: str, tag: str) -> None:
    fmt = _format_for(tex)
    if fmt is not None:
        try:
            shutil.copyfile(fmt, Path(tmp) / "tcfmt.fmt")
            run(["latex", "-interaction=nonstopmode", "-halt-on-error", "&tcfmt", "eq.tex"], tmp, tag=tag)
            return
        except RuntimeError as e:
            if isinstance(e, Cancelled) or "format" not in str(e).lower():
                raise                    # a real LaTeX error (or cancelled), not a format problem
            fmt.unlink(missing_ok=True)  # stale format (e.g. after a TeX update): rebuild later
    run(["latex", "-interaction=nonstopmode", "-halt-on-error", "eq.tex"], tmp, tag=tag)


def _compile_dvi(tex: str, tmp: str, tag: str = "job") -> Dims | None:
    (Path(tmp) / "eq.tex").write_text(tex, encoding="utf-8")
    key = hashlib.sha1(tex.encode()).hexdigest()[:20]
    cached = CACHE_DIR / "dvi" / key
    if (cached / "eq.dvi").exists() and (cached / "eq.log").exists():
        for f in ("eq.dvi", "eq.log"):
            shutil.copyfile(cached / f, Path(tmp) / f)
        os.utime(cached)
        return read_dims(tmp)
    try:
        _latex(tmp, tex, tag)
    except Cancelled:
        raise
    except RuntimeError as e:
        raise RuntimeError("LaTeX error:\n" + latex_error(str(e))) from None
    try:                                # keep the result for the next step (preview -> copy)
        cached.mkdir(parents=True, exist_ok=True)
        for f in ("eq.dvi", "eq.log"):
            shutil.copyfile(Path(tmp) / f, cached / f)
        old = sorted((CACHE_DIR / "dvi").iterdir(), key=lambda p: p.stat().st_mtime)
        for p in old[:-_DVI_KEEP]:
            shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass
    return read_dims(tmp)


def crop_at_baseline(svg: str, dims: Dims, border: float) -> str:
    """Anchor "baseline-crop": shorten the SVG box so it ends at the math baseline; what lies
    below (descenders, subscripts) stays visible through overflow="visible"."""
    cut = (dims.dp + border) * TEX_PT_TO_BP
    m = re.search(r"<svg\b[^>]*>", svg)
    head = m.group(0)
    vb = re.search(r"viewBox=(['\"])([^'\"]+)\1", head)
    x, y, w, h = (float(v) for v in vb.group(2).split())
    new = head.replace(vb.group(0), f"viewBox='{x:g} {y:g} {w:g} {max(h - cut, 0.001):g}'")
    hm = re.search(r"height=(['\"])([\d.]+)pt\1", new)
    if hm:
        new = new.replace(hm.group(0), f"height='{max(float(hm.group(2)) - cut, 0.001):g}pt'")
    if "overflow" not in new:
        new = new[:-1] + " overflow='visible'>"         # the root <svg ...> tag ends with '>'
    return svg.replace(head, new, 1)


def build(eq: str, a: Options, name: str | None = None) -> Path:
    """Compile ``eq`` and write ``<name>.svg|pdf`` (+ ``<name>.tex``) to OUTDIR."""
    return build_result(eq, a, name).path


def build_result(eq: str, a: Options, name: str | None = None) -> BuildResult:
    """Like build(), plus the TeX dimensions and the baseline shift for Affinity."""
    if a.anchor not in ANCHORS:
        raise ValueError(f"anchor must be one of {', '.join(ANCHORS)}")
    if a.mode not in ("math", "text"):
        raise ValueError("mode must be math or text")
    tex = make_tex(eq, a)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    name = name or "eq_" + hashlib.sha1(tex.encode()).hexdigest()[:8]
    with _tmpdir() as tmp:
        (OUTDIR / f"{name}.tex").write_text(eq + "\n", encoding="utf-8")  # source for later edits
        if a.format == "svg":
            dims = _compile_dvi(tex, tmp)  # DVI -> SVG: glyphs as paths, no Ghostscript needed
            if not shutil.which("dvisvgm"):
                raise RuntimeError("dvisvgm not found (install it via the MiKTeX Console)")
            out = OUTDIR / f"{name}.svg"
            # ink: box = lowest/highest ink; baseline: box = TeX box + border (standalone's papersize)
            bbox = "--exact-bbox" if a.anchor == "ink" and a.mode != "text" else "--bbox=papersize"
            cmd = ["dvisvgm", "--no-fonts", bbox, "-o", str(out), "eq.dvi"]
            if a.font in EXTRA_FONTMAPS:
                cmd.insert(1, f"--fontmap=+{EXTRA_FONTMAPS[a.font]}")
            r = run(cmd, tmp)
            # a font without outlines gives an SVG with missing glyphs - fail loudly instead
            bad = [l.strip() for l in (r.stdout + r.stderr).splitlines()
                   if "failed to create" in l or ("font" in l and "not found" in l and "map" not in l)]
            if bad:
                out.unlink(missing_ok=True)
                raise RuntimeError("dvisvgm could not find glyph outlines for this font:\n" + "\n".join(bad))
            if a.anchor == "baseline-crop" and dims and a.mode != "text":
                out.write_text(crop_at_baseline(out.read_text(encoding="utf-8"), dims, float(a.border)),
                               encoding="utf-8")
            return BuildResult(out, dims, baseline_shift(dims, a), text_overflow(dims, a))
        (Path(tmp) / "eq.tex").write_text(tex, encoding="utf-8")
        try:
            run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "eq.tex"], tmp)
        except RuntimeError as e:
            raise RuntimeError("LaTeX error:\n" + latex_error(str(e))) from None
        dims = read_dims(tmp)
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
        # a PDF page is always the TeX box + border, so the shift is the same as for "baseline"
        return BuildResult(out, dims, baseline_shift(dims, a.copy(anchor="baseline")
                                                     if a.anchor != "ink" else a), text_overflow(dims, a))


def render_preview(eq: str, a: Options, dpi: int = 200,
                   user_preamble: str | None = None) -> tuple[bytes, Dims | None]:
    """Fast raster preview (latex -> dvipng, transparent background) plus the TeX dimensions."""
    if not shutil.which("dvipng"):
        raise RuntimeError("dvipng not found (install it via the MiKTeX Console); preview disabled")
    tex = make_tex(eq, a, user_preamble, preview=True)
    key = (hashlib.sha1(tex.encode()).hexdigest(), int(dpi))
    if key in _PREVIEWS:                 # undo / switching back: instant
        _PREVIEWS.move_to_end(key)
        return _PREVIEWS[key]
    with _tmpdir() as tmp:
        dims = _compile_dvi(tex, tmp, tag="preview")
        run(["dvipng", "-T", "tight", "-D", str(dpi), "-bg", "Transparent", "-q*",
             "-o", "eq.png", "eq.dvi"], tmp, tag="preview")
        result = (Path(tmp) / "eq.png").read_bytes(), dims
    _PREVIEWS[key] = result
    while len(_PREVIEWS) > 40:
        _PREVIEWS.popitem(last=False)
    return result


_PREVIEWS: OrderedDict = OrderedDict()   # (hash of the .tex, dpi) -> (png, dims)


def render_png(eq: str, a: Options, dpi: int = 200, user_preamble: str | None = None) -> bytes:
    """Fast raster preview (latex -> dvipng, transparent background). Returns PNG bytes."""
    return render_preview(eq, a, dpi, user_preamble)[0]


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
    fmt = lambda p: f"{p[0]:.2f} {p[1]:.2f}"          # 0.01 pt is far below anything visible
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


def _bbox(d: str):
    v = [float(x) for x in re.findall(_NUM, d)]
    xs, ys = v[0::2], v[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _grouped_paths(pieces) -> list[str]:
    """"curves" mode: <g> per line > <g> per word > <path> per glyph. The ids become layer
    names in Affinity, so lines and words can be picked (and recoloured) in the Layers panel."""
    items = [(col, d, _bbox(d)) for col, d in pieces]
    lines: list[dict] = []
    # a glyph belongs to the current line if it overlaps it vertically by at least 30 % of its
    # own height (letters, commas, descenders and sub/superscripts of one line all do; the next
    # line starts below the previous line's descenders)
    for it in sorted(items, key=lambda t: t[2][1]):
        top, bottom = it[2][1], it[2][3]
        h = max(bottom - top, 1e-6)
        ln = lines[-1] if lines else None
        if ln and min(ln["bottom"], bottom) - top >= 0.3 * h:
            ln["bottom"] = max(ln["bottom"], bottom)
            ln["items"].append(it)
        else:
            lines.append({"top": top, "bottom": bottom, "items": [it]})
    out = []
    for li, ln in enumerate(lines, 1):
        row = sorted(ln["items"], key=lambda t: t[2][0])
        heights = sorted(t[2][3] - t[2][1] for t in row)
        gap = 0.3 * heights[len(heights) // 2]                 # wider than a kern, narrower than a space
        words, right = [], None
        for it in row:
            if right is None or it[2][0] - right > gap:
                words.append([])
            words[-1].append(it)
            right = it[2][2] if right is None else max(right, it[2][2])
        out.append(f'<g id="line{li:02d}">')
        for wi, word in enumerate(words, 1):
            out.append(f'<g id="line{li:02d}_word{wi:02d}">'
                       + "".join(f'<path d="{d}" fill="{col}"/>' for col, d, _ in word) + "</g>")
        out.append("</g>")
    return out


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
        paths = _grouped_paths(pieces)
    overflow = f' overflow="{root.get("overflow")}"' if root.get("overflow") else ""
    head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{root.get("width")}" '
            f'height="{root.get("height")}" viewBox="{root.get("viewBox")}"{overflow}>')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + head + "\n" + "\n".join(paths) + "\n</svg>\n"


def clipboard_svg(path: Path, mode: str) -> str:
    """SVG text to put on the clipboard for a paste mode (falls back to the file as is)."""
    global LAST_CLIPBOARD_SVG
    text = path.read_text(encoding="utf-8")
    try:
        LAST_CLIPBOARD_SVG = flatten_svg(text, merge=(mode != "curves"))
    except (ValueError, IndexError, KeyError) as e:   # unexpected SVG structure: keep the original
        print(f"texclip: could not flatten SVG ({e}); copying it unchanged", file=sys.stderr)
        LAST_CLIPBOARD_SVG = text
    return LAST_CLIPBOARD_SVG


LAST_CLIPBOARD_SVG = ""          # exactly what went onto the clipboard last ("Save clipboard SVG...")


def clipboard_stats(svg: str) -> str:
    """'1443 glyphs in 26 lines' (curves) / 'one object' (merged) - for the status line."""
    lines = len(re.findall(r'<g id="line\d+"', svg))
    paths = svg.count("<path")
    if lines:
        return f"{paths} glyphs in {lines} line{'s' if lines != 1 else ''}"
    return "one object" if paths <= 1 else f"{paths} objects (one per colour)"


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
