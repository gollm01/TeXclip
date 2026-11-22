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
def test_latex_error_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "PREAMBLE_FILE", tmp_path / "none.tex")
    with pytest.raises(RuntimeError, match="Undefined control sequence"):
        core.render_png(r"\nosuchmacro", Options())
