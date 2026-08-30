"""Architecture Zero mark - a zero built from stacked strata.

The concept, written down so a future change is a decision rather than a
redraw: the mark is the digit zero, cut into three bands. The bands are the
clearance tiers, which are the product's actual organizing idea - a corpus
partitioned into layers, each one readable by a different rung. So the mark
says the name and the thesis with the same shape.

Two rejected alternatives, recorded so they are not re-tried:

- **A slashed zero** (the programmer's zero). Conceptually apt - the slash is a
  typographic disambiguation mark, invented for the exact problem this project
  is about. It renders as a PROHIBITION SIGN. A circle with a diagonal bar
  means "forbidden" to every viewer alive, which is a poor first impression for
  a platform whose pitch is that it grants access carefully rather than denying
  it. Killed on the render, not on the idea.
- **Four cuts instead of two.** Read cleanly at 128px and turned to mush at 32.
  Avatars are seen small far more often than large, so the small size is the
  binding constraint and the band count is set by it.

Rendered at 4x and downsampled - PIL's ellipse has no anti-aliasing, and a
jagged mark at avatar size would undo the point.

Run:  <az0>/backend/.venv/Scripts/python.exe make_logo.py
"""
import pathlib
from PIL import Image, ImageDraw

OUT = pathlib.Path(__file__).resolve().parent
SS = 4  # supersample factor

BG     = "#0d1117"   # the terminal ground the project's own screenshots use
STROKE = "#7ee787"   # the green those screenshots use for a passing state

CUTS   = (-0.30, 0.30)   # two cuts, three bands - set by the 32px render
GAP_F  = 0.052
RING_W = 0.088
RX_F   = 0.240           # narrower than tall, so it reads as a digit


def mark(size: int, bg: str = BG, stroke: str = STROKE, transparent: bool = False):
    S = size * SS
    img = Image.new("RGBA" if transparent else "RGB", (S, S),
                    (0, 0, 0, 0) if transparent else bg)
    d = ImageDraw.Draw(img)

    cx = cy = S / 2
    rx, ry = S * RX_F, S * 0.325
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry],
              outline=stroke, width=int(S * RING_W))

    # Cut the strata. On the transparent variant the cuts must ERASE rather
    # than paint the background colour, or the mark carries dark bars on a
    # light page.
    gap = int(S * GAP_F)
    for f in CUTS:
        y = cy + ry * f
        box = [0, y - gap / 2, S, y + gap / 2]
        if transparent:
            d.rectangle(box, fill=(0, 0, 0, 0))
        else:
            d.rectangle(box, fill=bg)

    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for name, kw in (
        ("logo-512.png",             dict(size=512)),
        ("logo-1024.png",            dict(size=1024)),
        ("logo-512-transparent.png", dict(size=512, transparent=True)),
        ("logo-light-512.png",       dict(size=512, bg="#ffffff", stroke="#0d1117")),
    ):
        p = OUT / name
        mark(**kw).save(p)
        print(f"{name:28} {p.stat().st_size // 1024:>4} KB  {Image.open(p).size}")
