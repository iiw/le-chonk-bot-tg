#!/usr/bin/env python3
"""Generates LeChonk avatar variants (512×512 PNG) into ../variants/."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = 1024
OUT = Path(__file__).resolve().parent.parent / "variants"
OUT.mkdir(exist_ok=True)

BG = (255, 237, 214, 255)
BG2 = (255, 247, 236, 255)
DARK = (68, 63, 77, 255)
DARK2 = (97, 90, 112, 255)
INK = (38, 33, 46, 255)
PINK = (244, 167, 186, 255)
NOSE = (240, 138, 158, 255)
BLUSH = (255, 168, 183, 120)
WHISK = (168, 137, 108, 255)


def _face(img: Image.Image, d: ImageDraw.ImageDraw, open_eyes: bool = True) -> None:
    # body (the chonk)
    d.ellipse((228, 588, 796, 1002), fill=DARK)
    d.ellipse((342, 726, 682, 996), fill=DARK2)
    # paws
    d.ellipse((344, 902, 474, 1004), fill=DARK)
    d.ellipse((550, 902, 680, 1004), fill=DARK)
    # ears
    d.polygon([(266, 546), (322, 168), (514, 376)], fill=DARK)
    d.polygon([(758, 546), (702, 168), (510, 376)], fill=DARK)
    d.polygon([(320, 480), (352, 262), (470, 380)], fill=PINK)
    d.polygon([(704, 480), (672, 262), (554, 380)], fill=PINK)
    # head
    d.ellipse((196, 296, 828, 868), fill=DARK)

    if open_eyes:
        d.ellipse((352, 470, 470, 648), fill=INK)
        d.ellipse((554, 470, 672, 648), fill=INK)
        # eye highlights
        d.ellipse((374, 504, 416, 546), fill=(255, 255, 255, 255))
        d.ellipse((434, 566, 460, 592), fill=(255, 255, 255, 255))
        d.ellipse((576, 504, 618, 546), fill=(255, 255, 255, 255))
        d.ellipse((636, 566, 662, 592), fill=(255, 255, 255, 255))
    else:
        # happy closed eyes (roof-shaped arcs)
        d.arc((356, 520, 466, 620), start=180, end=360, fill=INK, width=16)
        d.arc((558, 520, 668, 620), start=180, end=360, fill=INK, width=16)

    # nose and mouth
    d.polygon([(488, 654), (536, 654), (512, 690)], fill=NOSE)
    d.line([(512, 690), (512, 704)], fill=INK, width=10)
    d.arc((456, 690, 514, 742), start=0, end=120, fill=INK, width=10)
    d.arc((510, 690, 568, 742), start=60, end=180, fill=INK, width=10)

    # whiskers
    for dy in (-26, 0, 26):
        d.line([(272, 656 + dy), (110, 630 + dy)], fill=WHISK, width=9)
        d.line([(752, 656 + dy), (914, 630 + dy)], fill=WHISK, width=9)

    # blush
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.ellipse((292, 626, 420, 700), fill=BLUSH)
    od.ellipse((604, 626, 732, 700), fill=BLUSH)
    img.alpha_composite(ov)


def make(open_eyes: bool = True, name: str = "a") -> Path:
    img = Image.new("RGBA", (BASE, BASE), BG)
    d = ImageDraw.Draw(img)
    d.ellipse((40, 40, 984, 984), fill=BG2)
    _face(img, d, open_eyes)
    img = img.resize((512, 512), Image.LANCZOS)
    p = OUT / f"avatar_{name}.png"
    img.save(p)
    return p


def make_emoji(name: str = "c") -> Path | None:
    font_path = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
    if not Path(font_path).exists():
        return None
    try:
        font = ImageFont.truetype(font_path, 109)  # native CBDT size
        tmp = Image.new("RGBA", (137, 137), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((6, 6), "🐱", font=font, embedded_color=True)
    except Exception:
        return None
    tmp = tmp.resize((760, 760), Image.LANCZOS)
    img = Image.new("RGBA", (BASE, BASE), BG)
    d = ImageDraw.Draw(img)
    d.ellipse((40, 40, 984, 984), fill=BG2)
    img.alpha_composite(tmp, (132, 132))
    img = img.resize((512, 512), Image.LANCZOS)
    p = OUT / f"avatar_{name}.png"
    img.save(p)
    return p


if __name__ == "__main__":
    for eyes, name in ((True, "a"), (False, "b")):
        print("saved", make(open_eyes=eyes, name=name))
    p = make_emoji("c")
    print("saved", p if p else "(emoji font not found, skipped)")
