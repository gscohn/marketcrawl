"""Draws the bot's Bluesky profile picture and header.

ELI5: rather than drawing these by hand, this script paints them with code,
so they can be tweaked and redrawn the same way every time.

    pip install pillow          (only needed for this script, not the bot)
    python brand/make.py        writes brand/avatar.png and brand/banner.png

Layout notes for the header (3000x1000, Bluesky's size):
  - on iPhone the top ~40% sits under the status bar and notch, so the
    chart line goes up there and the text stays in the lower half
  - the round profile picture covers the bottom-left, so the text starts
    about a third of the way across
"""

import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = Path(__file__).parent
# macOS's system font. On another system, point this at any bold-capable .ttf.
FONT = "/System/Library/Fonts/SFNS.ttf"

NAVY_TOP = (12, 22, 38); NAVY_BOT = (20, 36, 60)
GREEN = (34, 197, 94); RED = (239, 68, 68); WHITE = (240, 244, 248); MUTED = (148, 163, 184)
GRID = (255, 255, 255, 18)

def font(size, weight="Bold"):
    f = ImageFont.truetype(FONT, size)
    try: f.set_variation_by_name(weight)
    except Exception: pass
    return f

def gradient(w, h):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(NAVY_TOP, NAVY_BOT)))
    return img.convert("RGBA")

def grid(img, step):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    w, h = img.size
    for x in range(0, w, step): d.line([(x, 0), (x, h)], fill=GRID, width=max(2, step // 60))
    for y in range(0, h, step): d.line([(0, y), (w, y)], fill=GRID, width=max(2, step // 60))
    return Image.alpha_composite(img, layer)

def glow_line(img, pts, color, width):
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(pts, fill=color + (150,), width=width * 3, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(width * 1.5))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    d.line(pts, fill=color, width=width, joint="curve")
    for p in pts[1:-1]:
        d.ellipse([p[0] - width / 2, p[1] - width / 2, p[0] + width / 2, p[1] + width / 2], fill=color)
    return img

def dot(img, p, r, color):
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([p[0] - r * 2.2, p[1] - r * 2.2, p[0] + r * 2.2, p[1] + r * 2.2], fill=color + (70,))
    img = Image.alpha_composite(img, halo.filter(ImageFilter.GaussianBlur(r * 0.6)))
    ImageDraw.Draw(img).ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color, outline=WHITE, width=max(2, r // 4))
    return img

def candle(d, x, top, bottom, wick_top, wick_bot, color, w):
    d.line([(x, wick_top), (x, wick_bot)], fill=color, width=max(2, w // 6))
    d.rounded_rectangle([x - w / 2, top, x + w / 2, bottom], radius=w // 6, fill=color)

# ---------- avatar ----------
S = 2000
av = grid(gradient(S, S), 250)
d = ImageDraw.Draw(av)
# candles in the background, rising
for i, (x, lo, hi, c) in enumerate([(560, 1300, 1050, RED), (820, 1220, 900, GREEN), (1080, 1120, 760, GREEN), (1340, 900, 560, GREEN)]):
    candle(d, x, min(lo, hi), max(lo, hi), min(lo, hi) - 90, max(lo, hi) + 90, c + (90,) if False else tuple(int(v * 0.45) + 10 for v in c), 120)
pts = [(420, 1420), (700, 1180), (900, 1290), (1180, 880), (1360, 980), (1600, 560)]
av = glow_line(av, pts, GREEN, 90)
av = dot(av, pts[-1], 75, GREEN)
av = av.resize((1000, 1000), Image.LANCZOS).convert("RGB")
av.save(HERE / "avatar.png")

# ---------- banner ----------
W, H = 6000, 2000
bn = grid(gradient(W, H), 250)
random.seed(7)
# a long wandering market line across the whole banner, ending high on the right
pts = []
for x in range(-100, W + 200, 200):
    # The chart lives in the top band (decoration: fine under the iPhone's
    # status bar), climbing gently, with a rally at the right edge.
    trend = 900 - (x / W) * 300
    if x > 4900:
        trend -= (x - 4900) / (W - 4900) * 450
    pts.append((x, trend + random.uniform(-70, 70)))
area = Image.new("RGBA", bn.size, (0, 0, 0, 0))
ImageDraw.Draw(area).polygon(pts + [(W + 200, H), (-100, H)], fill=GREEN + (22,))
bn = Image.alpha_composite(bn, area)
bn = glow_line(bn, pts, GREEN, 24)
end = [p for p in pts if p[0] <= W - 260][-1]
bn = dot(bn, end, 44, GREEN)
# Text in the lower half (below the iPhone's status bar and notch) and to
# the right of where Bluesky puts the round profile picture.
d = ImageDraw.Draw(bn)
x0, y0 = 1800, 820
d.text((x0, y0), "MarketCrawl", font=font(470), fill=WHITE)
d.text((x0 + 14, y0 + 590), "Closing prices, by DM, after the bell.", font=font(172, "Medium"), fill=WHITE)
sub = font(150, "Regular"); bold = font(150, "Bold")
x = x0 + 14; yy = y0 + 840
for text, f, c in (("DM ", sub, MUTED), ("help", bold, GREEN), (" to start  ·  a SportsCrawl spin-off", sub, MUTED)):
    d.text((x, yy), text, font=f, fill=c); x += d.textlength(text, font=f)
# print("text right edge:", int(max(x, x0 + 14 + d.textlength("Closing prices, by DM, after the bell.", font=font(172, "Medium")), x0 + d.textlength("MarketCrawl", font=font(470)))), "of", W)
bn = bn.resize((3000, 1000), Image.LANCZOS).convert("RGB")
bn.save(HERE / "banner.png")
print("ok")
