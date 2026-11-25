import shutil

import pytest

from texclip import core
from texclip.core import (FONTS, Options, build_preamble, make_body, make_tex, normalize_color,
                          strip_overrides)

needs_tex = pytest.mark.skipif(not all(shutil.which(t) for t in ("latex", "dvisvgm", "dvipng")),
                               reason="TeX toolchain not installed")


# ---- colour / options ------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [("1f4e79", "1F4E79"), ("#1F4E79", "1F4E79"), ("f0a", "FF00AA"),
                                      (" #abc ", "AABBCC")])
def test_normalize_color(raw, want):
    assert normalize_color(raw) == want


@pytest.mark.parametrize("bad", ["", "red", "12345", "#12345g", "1234567"])
def test_normalize_color_rejects(bad):
    with pytest.raises(ValueError):
        normalize_color(bad)


# ---- body wrapping ---------------------------------------------------------------------
def test_body_display_and_inline():
    assert make_body("x^2") == r"$\displaystyle x^2$"
    assert make_body("x^2", inline=True) == "$x^2$"


def test_body_rewrites_environments():
    body = make_body(r"\begin{align}a&=b\\c&=d\end{align}")
    assert r"\begin{aligned}" in body and r"\end{aligned}" in body
    assert r"\begin{gathered}" in make_body(r"\begin{gather*}a\\b\end{gather*}")
    assert "equation" not in make_body(r"\begin{equation}x\end{equation}")


# ---- preamble: GUI settings must beat the personal preamble ----------------------------
def test_strip_overrides_removes_fonts_and_colours():
    src = "\n".join([
        r"\usepackage{lmodern,tikz} % keep tikz",
        r"\usepackage[T1]{fontenc}",
        r"\color{red}",
        r"\large",
        r"\newcommand{\a}{\small x}",
        r"50\% off",
    ])
    clean, removed = strip_overrides(src)
    assert "lmodern" not in clean
    assert r"\usepackage{tikz}" in clean
    assert r"\usepackage[T1]{fontenc}" in clean          # fontenc is not a font choice
    assert r"% overridden by texclip settings: \color{red}" in clean
    assert r"\newcommand{\a}{\small x}" in clean         # a macro definition is left alone
    assert r"50\% off" in clean                          # escaped percent is not a comment
    assert {"lmodern", r"\color{red}", r"\large"} <= set(removed)


def test_preamble_uses_selected_font_and_colour():
    pre = build_preamble(Options(font="times", color="#1f4e79", size=14),
                         user_preamble=r"\usepackage{lmodern}")
    assert "newtxtext" in pre and "lmodern" not in pre
    assert "1F4E79" in pre and "14pt" in pre
    assert "amssymb" not in pre                          # clashes with newtxmath
    assert "amssymb" in build_preamble(Options(font="cm"), user_preamble="")


def test_make_tex_layout():
    tex = make_tex("x", Options(size=12, color="9a031e", bold=True, border=2), user_preamble="")
    assert r"\documentclass[border=2pt]{standalone}" in tex
    assert r"\fontsize{12}{14.4}\selectfont\boldmath" in tex
    assert r"\color[HTML]{9A031E}" in tex


def test_make_tex_rejects_bad_input():
    with pytest.raises(ValueError):
        make_tex("x", Options(size=0), user_preamble="")
    with pytest.raises(ValueError):
        make_tex("x", Options(color="red"), user_preamble="")


# ---- "preamble" option: leave size / colour / font to the preamble file ---------------
def test_strip_overrides_keeps_what_comes_from_the_preamble():
    src = "\n".join([r"\usepackage{helvet,tikz}", r"\fontsize{24}{29}\selectfont", r"\Large",
                     r"\color{red}"])
    clean, removed = strip_overrides(src, font=False, size=False, color=False)
    assert r"\usepackage{helvet,tikz}" in clean and not removed
    # LaTeX resets the size at \begin{document}, so kept size lines are deferred
    assert r"\AtBeginDocument{\fontsize{24}{29}\selectfont}" in clean
    assert r"\AtBeginDocument{\Large}" in clean
    assert r"\color{red}" in clean and "overridden" not in clean
    clean, removed = strip_overrides(src, font=False, size=True, color=False)
    assert "helvet" in clean and r"\Large" in removed and r"\color{red}" in clean


def test_preamble_mode_in_build_preamble_and_make_tex():
    user = r"\usepackage{newtxtext,newtxmath}" + "\n" + r"\color{blue}"
    a = Options(font=core.FROM_PREAMBLE, size=core.FROM_PREAMBLE, color=core.FROM_PREAMBLE)
    pre = build_preamble(a, user_preamble=user)
    assert "newtxmath" in pre and r"\color{blue}" in pre
    assert "amssymb" not in pre                          # newtxmath brings its own
    assert "eqcolor" not in pre and "amsfonts" not in pre
    tex = make_tex("x", a, user_preamble=user)
    assert r"\fontsize" not in tex.split(r"\begin{document}")[1]
    assert r"\color[HTML]" not in tex
    # the window settings still win for whatever is not set to "preamble"
    tex = make_tex("x", a.copy(size=14), user_preamble=user)
    assert r"\fontsize{14}{16.8}\selectfont" in tex


def test_install_preamble_strips_document_and_keeps_backup(tmp_path):
    dest = tmp_path / "pre.tex"
    dest.write_text("old", encoding="utf-8")
    src = tmp_path / "mine.tex"
    src.write_text("\\documentclass{article}\n\\usepackage{siunitx}\n\\begin{document}\nbody\n\\end{document}\n",
                   encoding="utf-8")
    backup = core.install_preamble(src, dest)
    text = dest.read_text(encoding="utf-8")
    assert r"\usepackage{siunitx}" in text
    assert "documentclass" not in text and "body" not in text
    assert backup and backup.read_text(encoding="utf-8") == "old"


def test_all_fonts_known():
    for key in FONTS:
        assert build_preamble(Options(font=key), user_preamble="").strip()


# ---- real compilation (skipped without a TeX install) ----------------------------------
@needs_tex
@pytest.mark.tex
@pytest.mark.parametrize("font", list(FONTS))
def test_every_font_compiles(font):
    png = core.render_png(r"\ket{\psi}=\sum_n \alpha_n^2", Options(font=font), user_preamble="")
    assert png[:4] == b"\x89PNG"


@needs_tex
@pytest.mark.tex
def test_build_svg_has_paths_not_text(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    svg = core.build(r"E=mc^2", Options()).read_text(encoding="utf-8")
    assert "<path" in svg and "<text" not in svg


@needs_tex
@pytest.mark.tex
@pytest.mark.parametrize("font", list(FONTS))
def test_every_font_gives_glyph_paths(font, tmp_path, monkeypatch):
    """dvisvgm must find outlines for every font (CM Bright needed an extra font map)."""
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    svg = core.build(r"\alpha+x^2", Options(font=font)).read_text(encoding="utf-8")
    assert svg.count("<path") >= 3


@needs_tex
@pytest.mark.tex
def test_flatten_svg_for_the_clipboard(tmp_path, monkeypatch):
    """object = one compound path per colour, curves = one path per glyph; no <use> left."""
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    svg = core.build(r"\sqrt{x}+\frac{a}{b}", Options()).read_text(encoding="utf-8")
    one = core.flatten_svg(svg, merge=True)
    many = core.flatten_svg(svg, merge=False)
    assert one.count("<path") == 1 and "<use" not in one
    assert many.count("<path") >= 5 and "<use" not in many
    two = core.flatten_svg(core.build(r"\textcolor{red}{a}+b", Options()).read_text(encoding="utf-8"))
    assert two.count("<path") == 2                        # one per colour


def test_affinity_clipboard_formats():
    # Affinity reads SVG from the clipboard only under these names (see Serif.Win32.dll)
    assert {"image/x-inkscape-svg", "Scalable Vector Graphics"} <= set(core.SVG_CLIP_FORMATS)
    assert core.PASTE_MODES == ("object", "curves")


# ---- anchor / baseline shift --------------------------------------------------------------
def test_make_tex_measures_the_box():
    tex = make_tex("x", Options(), user_preamble="")
    assert r"\newsavebox\texclipbox" in tex and "TEXCLIP-DIMS" in tex
    assert r"\usebox\texclipbox" in tex                    # baseline: the measured box is shipped
    ink = make_tex("x", Options(anchor="ink"), user_preamble="")
    assert r"\usebox" not in ink and r"$\displaystyle x$" in ink.split("TEXCLIP-DIMS")[1]


def test_baseline_shift_arithmetic():
    d = core.Dims(ht=8.64, dp=0.0, wd=9.0)
    assert core.baseline_shift(d, Options()) == -0.5            # -(0 + 0.5) * 72/72.27
    assert core.baseline_shift(core.Dims(18.0, 8.5833, 20.0), Options()) == -9.05
    assert core.baseline_shift(d, Options(border=0)) == 0.0
    assert core.baseline_shift(d, Options(anchor="ink")) is None
    assert core.baseline_shift(d, Options(anchor="baseline-crop")) == 0.0


def test_crop_at_baseline():
    svg = "<svg width='10pt' height='12pt' viewBox='-72 -72 10 12'><g/></svg>"
    out = core.crop_at_baseline(svg, core.Dims(ht=9, dp=2.5, wd=8), border=0.5)
    cut = 3.0 * 72 / 72.27
    assert f"viewBox='-72 -72 10 {12 - cut:g}'" in out
    assert f"height='{12 - cut:g}pt'" in out and "overflow='visible'" in out


LEGACY_TEMPLATE = r"""\RequirePackage{{fix-cm}}
\documentclass[border={bp}pt]{{standalone}}
{preamble}
\begin{{document}}
{style}
{body}
\end{{document}}
"""


@needs_tex
@pytest.mark.tex
@pytest.mark.parametrize("eq", ["x^2", "t_g", r"\frac{a}{b}", r"i\hbar\,\partial_t\ket{\psi}=\hat H\ket{\psi}"])
def test_anchor_ink_matches_the_old_output(eq, tmp_path, monkeypatch):
    """--anchor ink: same box and same glyph positions as before the anchor feature
    (dvisvgm's glyph ids are not stable between runs, so the files are compared by geometry)."""
    import re
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    a = Options(size=30, anchor="ink")
    new = core.build(eq, a).read_text(encoding="utf-8")
    # the old pipeline, by hand
    style = r"\fontsize{30}{36}\selectfont" + "\n" + r"\color[HTML]{000000}"
    old_tex = LEGACY_TEMPLATE.format(bp=a.border, preamble=build_preamble(a, ""), style=style,
                                     body=make_body(eq))
    (tmp_path / "eq.tex").write_text(old_tex, encoding="utf-8")
    core.run(["latex", "-interaction=nonstopmode", "eq.tex"], str(tmp_path))
    core.run(["dvisvgm", "--no-fonts", "--exact-bbox", "-o", "old.svg", "eq.dvi"], str(tmp_path))
    old = (tmp_path / "old.svg").read_text(encoding="utf-8")
    root = lambda s: re.search(r"<svg[^>]*>", s).group(0)
    uses = lambda s: sorted(re.findall(r"<use x='([^']+)' y='([^']+)'", s))
    rects = lambda s: sorted(re.findall(r"<rect [^>]*>", s))
    assert root(new) == root(old)
    assert uses(new) == uses(old) and rects(new) == rects(old)


@needs_tex
@pytest.mark.tex
def test_baseline_anchor_dims_and_shift(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    x2 = core.build_result("x^2", Options())
    assert x2.dims.dp == 0 and x2.shift == -0.5                 # spec test 1
    tg = core.build_result("t_g", Options(size=30))
    assert tg.dims.dp > 0 and tg.shift < -0.5                   # spec test 2
    fr = core.build_result(r"\frac{a}{b}", Options(size=30))
    assert fr.shift < tg.shift                                  # spec test 3: larger shift
    al = core.build_result(r"\begin{align*} a&=b\\ c&=d \end{align*}", Options())
    assert al.dims.dp > 0 and al.shift is not None              # spec test 4
    # the SVG box is exactly the TeX box + border (in bp)
    vb = [float(v) for v in re.search(r"viewBox='([^']+)'", tg.path.read_text(encoding="utf-8")).group(1).split()]
    assert abs(vb[3] - (tg.dims.ht + tg.dims.dp + 2 * 0.5) * 72 / 72.27) < 0.01
    crop = core.build_result("t_g", Options(size=30, anchor="baseline-crop"))
    svg = crop.path.read_text(encoding="utf-8")
    vb = [float(v) for v in re.search(r"viewBox='([^']+)'", svg).group(1).split()]
    assert "overflow='visible'" in svg and abs(vb[3] - (tg.dims.ht + 0.5) * 72 / 72.27) < 0.01


@needs_tex
@pytest.mark.tex
def test_latex_error_writes_no_svg(tmp_path, monkeypatch):           # spec test 5
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    with pytest.raises(RuntimeError, match="Undefined control sequence"):
        core.build_result(r"\nosuchmacro", Options())
    assert not list(tmp_path.glob("*.svg"))


# ---- text blocks --------------------------------------------------------------------------
def test_tex_dim_uses_affinity_points():
    assert core.tex_dim("80mm") == ("80mm", pytest.approx(80 * 72 / 25.4))
    assert core.tex_dim("300") == ("300bp", 300.0)          # pt = Affinity pt = TeX bp
    assert core.tex_dim("300 pt")[0] == "300bp"
    for bad in ("", "abc", "-5mm", "10px"):
        with pytest.raises(ValueError):
            core.tex_dim(bad)


def test_text_block_tex():
    a = Options(mode="text", width="80mm", align="center", parskip=6, hyphenate=False)
    tex = make_tex("Some $x$ text.", a, user_preamble="")
    assert r"\begin{minipage}[t]{80mm}" in tex and r"\Centering" in tex
    assert r"\setlength\parskip{6bp}" in tex and r"\hyphenpenalty=10000" in tex
    assert "ragged2e" in tex and r"\documentclass[border=0pt]{standalone}" in tex
    assert r"\parbox" not in tex and r"\fbox" not in tex
    fixed = make_tex("x", a.copy(height="40mm"), user_preamble="", preview=True)
    assert r"\parbox[t][40mm][t]{80mm}" in fixed and r"\fbox" in fixed     # frame only in the preview
    for key in core.ALIGNMENTS:
        assert core.ALIGNMENTS[key][1] in make_tex("x", a.copy(align=key), user_preamble="")
    with pytest.raises(ValueError):
        make_tex("x", a.copy(align="sideways"), user_preamble="")


@needs_tex
@pytest.mark.tex
def test_text_block_has_the_frame_size(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    text = ("A paragraph of text with $x^2$ inline math that is long enough to be broken "
            "into several lines by the paragraph builder.\n\nA second paragraph.")
    viewbox = lambda r: [float(v) for v in re.search(r"viewBox='([^']+)'",
                                                     r.path.read_text(encoding="utf-8")).group(1).split()]
    for align in core.ALIGNMENTS:
        r = core.build_result(text, Options(mode="text", width="60mm", align=align))
        assert abs(viewbox(r)[2] - 60 * 72 / 25.4) < 0.01 and r.shift is None and r.overflow is None
    fixed = core.build_result(text, Options(mode="text", width="60mm", height="50mm"))
    assert abs(viewbox(fixed)[3] - 50 * 72 / 25.4) < 0.01 and fixed.overflow is None
    tight = core.build_result(text, Options(mode="text", width="60mm", height="5mm"))
    assert tight.overflow and tight.overflow > 10                  # reported, box keeps 5 mm
    assert abs(viewbox(tight)[3] - 5 * 72 / 25.4) < 0.01


# ---- point 6: "curves" keeps separate glyphs, grouped by line and word --------------------
@needs_tex
@pytest.mark.tex
def test_curves_mode_groups_lines_and_words(tmp_path, monkeypatch):
    import re
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    text = ("The quick brown fox jumps over the lazy dog with $x^2+y_k$ inline. " * 4).strip()
    svg = core.build(text, Options(mode="text", size=20, width="80mm")).read_text(encoding="utf-8")
    curves = core.flatten_svg(svg, merge=False)
    lines = re.findall(r'<g id="line\d+">', curves)
    words = re.findall(r'<g id="line\d+_word\d+">', curves)
    assert len(lines) >= 3 and len(words) > 2 * len(lines)          # several lines, words inside
    assert "<use" not in curves and curves.count("<path") > 100     # every glyph its own path
    assert "glyphs in" in core.clipboard_stats(curves)
    assert core.clipboard_stats(core.flatten_svg(svg, merge=True)) == "one object"
    first = re.search(r'<g id="line01">(.*?)</g>\s*</g>', curves, re.S).group(1)
    assert len(re.findall(r'_word\d+"', first)) >= 5                # "The quick brown fox jumps ..."


# ---- point 5: speed and stability ---------------------------------------------------------
def test_run_timeout_and_cancel():
    import sys
    import threading
    with pytest.raises(RuntimeError, match="did not finish"):
        core.run([sys.executable, "-c", "import time; time.sleep(5)"], ".", tag="t1", timeout=0.5)
    box = {}

    def slow():
        try:
            core.run([sys.executable, "-c", "import time; time.sleep(10)"], ".", tag="t2")
        except core.Cancelled:
            box["cancelled"] = True

    th = threading.Thread(target=slow)
    th.start()
    import time
    for _ in range(50):                                  # wait until the process runs
        if core._RUNNING.get("t2"):
            break
        time.sleep(0.05)
    core.cancel("t2")
    th.join(5)
    assert box.get("cancelled")


@needs_tex
@pytest.mark.tex
def test_copy_reuses_the_preview_compile(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "OUTDIR", tmp_path)
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    monkeypatch.setattr(core, "CACHE_DIR", tmp_path / "cache")
    calls = []
    real = core._latex
    monkeypatch.setattr(core, "_latex", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    a = Options(size=17)
    core.render_preview(r"\sqrt{z}", a, dpi=60)
    core.build_result(r"\sqrt{z}", a)                     # same document: no second latex run
    assert len(calls) == 1
    png1 = core.render_preview(r"\sqrt{z}", a, dpi=60)    # memory cache
    assert len(calls) == 1 and png1[0][:4] == b"\x89PNG"


@needs_tex
@pytest.mark.tex
def test_size_from_preamble_takes_effect(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    a = Options(size=core.FROM_PREAMBLE)
    small = core.render_png("x", a, dpi=150, user_preamble="")
    big = core.render_png("x", a, dpi=150, user_preamble=r"\fontsize{40}{48}\selectfont")
    assert len(big) > len(small)


@needs_tex
@pytest.mark.tex
def test_latex_error_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    with pytest.raises(RuntimeError, match="Undefined control sequence"):
        core.render_png(r"\nosuchmacro", Options())


# ---- icon / shortcut --------------------------------------------------------------------
def test_icon_assets_present_and_multisize():
    from texclip import shortcut
    assert shortcut.ICON_PNG.read_bytes()[:4] == b"\x89PNG"
    ico = shortcut.ICON_ICO.read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00"                  # ICO header
    assert int.from_bytes(ico[4:6], "little") >= 4          # several sizes: 16, 32, 48, 256 ...
    assert "<svg" in (shortcut.ASSETS / "texclip.svg").read_text(encoding="utf-8")


def test_launch_command_points_to_something_runnable():
    import os
    from texclip import shortcut
    exe, args = shortcut.launch_command()
    assert os.path.exists(exe)
    assert args == ["-m", "texclip"] or args[0].endswith("run_gui.pyw")
