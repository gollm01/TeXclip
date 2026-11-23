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
