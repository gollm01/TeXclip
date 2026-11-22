"""Generate the TeXclip icon (SVG source, PNG, multi-size ICO) from one set of shapes.

    python tools/make_icon.py          # needs Pillow (dev only, not a runtime dependency)

Edit the shapes / colours below and re-run; the files land in src/texclip/assets/.
Design: a clipboard (the "clip") with a sigma (the math) on a deep-blue tile.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src" / "texclip" / "assets"
S = 1024                                   # design grid

BLUE, PAPER, ORANGE = "#003049", "#f6f2ec", "#fb8b24"
TILE = (0, 0, S, S, 224)                   # x0, y0, x1, y1, corner radius
BOARD = (232, 190, 792, 860, 70)
CLIP = (382, 120, 642, 300, 56)
SLOT = (452, 168, 572, 214, 23)
SIGMA = [(340, 380), (684, 380), (684, 444), (462, 444), (558, 590), (462, 736),
         (684, 736), (684, 800), (340, 800), (478, 590)]


def draw_png(size: int = S) -> Image.Image:
    k = 4                                  # supersampling for smooth edges
    im = Image.new("RGBA", (S * k, S * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def rr(box, fill):
        x0, y0, x1, y1, r = box
        d.rounded_rectangle((x0 * k, y0 * k, x1 * k, y1 * k), r * k, fill=fill)

    rr(TILE, BLUE)
    rr(BOARD, PAPER)
    rr(CLIP, ORANGE)
    rr(SLOT, BLUE)
    d.polygon([(x * k, y * k) for x, y in SIGMA], fill=BLUE)
    return im.resize((size, size), Image.LANCZOS)


def write_svg(path: Path) -> None:
    def rr(box, fill):
        x0, y0, x1, y1, r = box
        return f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="{fill}"/>'

    pts = " ".join(f"{x},{y}" for x, y in SIGMA)
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" width="256" height="256">\n'
        f'  <title>TeXclip</title>\n  {rr(TILE, BLUE)}\n  {rr(BOARD, PAPER)}\n  {rr(CLIP, ORANGE)}\n'
        f'  {rr(SLOT, BLUE)}\n  <polygon points="{pts}" fill="{BLUE}"/>\n</svg>\n', encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw_png(256).save(OUT / "texclip.png")
    draw_png(256).save(OUT / "texclip.ico",
                       sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    write_svg(OUT / "texclip.svg")
    print("wrote", *(p.name for p in sorted(OUT.iterdir())))
