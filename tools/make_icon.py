"""Draws assets/retro.ico - the Retro mark, used by the window, the tray and the exe.

Run it after changing the colours, then rebuild: python tools/make_icon.py

It is drawn at 4x and shrunk with LANCZOS, which is what gives the ring its smooth
edge; Pillow has no anti-aliased ellipse.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "retro.ico")

SIZE = 256
SCALE = 4
W = SIZE * SCALE
BG = (14, 17, 22, 255)
EDGE = (30, 37, 48, 255)
BLUE = (78, 168, 255)
WHITE = (230, 235, 244, 255)
FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw():
    image = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, W - 1, W - 1], radius=int(W * 0.23),
                           fill=BG, outline=EDGE, width=max(1, int(W * 0.012)))
    inset = W * 0.24
    # Three passes of the ring: two faint ones for the glow, one bright core.
    for width, alpha in ((W * 0.085, 22), (W * 0.055, 38), (W * 0.028, 255)):
        draw.ellipse([inset, inset, W - inset, W - inset],
                     outline=(BLUE[0], BLUE[1], BLUE[2], alpha), width=int(width))
    font = load_font(int(W * 0.40))
    draw.text((W / 2, W / 2 + W * 0.01), "R", font=font, fill=WHITE, anchor="mm")
    return image


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    image = draw().resize((SIZE, SIZE), Image.LANCZOS)
    image.save(OUT, format="ICO",
               sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
