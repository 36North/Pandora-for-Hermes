"""Generate the store artwork (icon + hero) for the Pandora plugin.

Run from the plugin root:  python assets/generate_artwork.py
Outputs assets/icon.png (512x512) and assets/hero.png (1200x630).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
BLUE = (54, 104, 255)
BLUE_LIGHT = (108, 146, 255)
WHITE = (255, 255, 255)
INK = (11, 13, 18)


def rounded_icon(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.215), fill=(15, 17, 22, 255))

    # Soft blue glow behind the bars.
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [int(size * 0.16), int(size * 0.2), int(size * 0.84), int(size * 0.84)],
        fill=(54, 104, 255, 90),
    )
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size * 0.09)))

    # Five equalizer bars: blue, blue, white, blue, blue.
    width = int(size * 0.052)
    gap = int(size * 0.039)
    heights = [0.25, 0.39, 0.55, 0.39, 0.25]
    total = width * 5 + gap * 4
    x = (size - total) / 2
    bottom = size * 0.74
    for index, height in enumerate(heights):
        top = bottom - size * height
        colour = WHITE if index == 2 else (BLUE_LIGHT if index in (1, 3) else BLUE)
        draw.rounded_rectangle(
            [x, top, x + width, bottom], radius=width / 2, fill=colour
        )
        x += width + gap
    return img


def hero(icon: Image.Image, width: int = 1200, height: int = 630) -> Image.Image:
    img = Image.new("RGB", (width, height), INK)
    draw = ImageDraw.Draw(img)
    # Backdrop glow.
    glow = Image.new("RGB", (width, height), INK)
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse([-220, -260, 760, 720], fill=(28, 44, 96))
    gdraw.ellipse([620, -140, 1500, 740], fill=(20, 28, 58))
    img = glow.filter(ImageFilter.GaussianBlur(90))
    draw = ImageDraw.Draw(img)

    big = icon.resize((300, 300), Image.LANCZOS)
    img.paste(big, (96, 165), big)

    bold = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", 92)
    body = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 34)
    small = ImageFont.truetype(r"C:\Windows\Fonts\segoeui.ttf", 26)
    draw.text((452, 226), "Pandora", font=bold, fill=WHITE)
    draw.text((456, 336), "Your stations, in the Hermes status bar", font=body, fill=(178, 190, 208))
    draw.text((456, 392), "Play, search, thumbs and Cast — without leaving the app",
              font=small, fill=(122, 134, 154))
    return img


if __name__ == "__main__":
    if (HERE / "icon.png").exists() or (HERE / "hero.png").exists():
        raise SystemExit(
            "assets/icon.png or assets/hero.png already exists (final artwork) — refusing to overwrite")
    icon = rounded_icon()
    icon.save(HERE / "icon.png")
    hero(icon).save(HERE / "hero.png")
    print("wrote", HERE / "icon.png", icon.size)
    print("wrote", HERE / "hero.png", (1200, 630))
