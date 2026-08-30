"""Repo social preview card (1280x640) - the image GitHub renders when the
repo link is shared. Without one, a shared link is a generic grey card, which
is what most people will see first if the link travels."""
import pathlib
from PIL import Image, ImageDraw, ImageFont
import make_logo

OUT = pathlib.Path(__file__).resolve().parent
W, H = 1280, 640
BG, FG, MUTED, GREEN = "#0d1117", "#e6edf3", "#8b949e", "#7ee787"
REG = "C:/Windows/Fonts/consola.ttf"
BOLD = "C:/Windows/Fonts/consolab.ttf"

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

mark = make_logo.mark(200, transparent=True)
img.paste(mark, (86, 150), mark)

x = 330
d.text((x, 196), "Architecture Zero", font=ImageFont.truetype(BOLD, 62), fill=FG)
d.text((x, 286), "A self-hosted AI assistant platform where",
       font=ImageFont.truetype(REG, 31), fill=MUTED)
d.text((x, 328), "trust is measured, not claimed.",
       font=ImageFont.truetype(REG, 31), fill=GREEN)

f = ImageFont.truetype(REG, 24)
d.text((x, 404), "hybrid retrieval  ·  clearance tiers  ·  injection gate", font=f, fill=MUTED)
d.text((x, 440), "judged evals  ·  federation  ·  Apache-2.0", font=f, fill=MUTED)

d.line([x, 508, W - 86, 508], fill="#21262d", width=2)
d.text((x, 528), "github.com/architecture-zero", font=ImageFont.truetype(REG, 25), fill=FG)

img.save(OUT / "social-preview-1280x640.png")
print("social-preview-1280x640.png", img.size)
