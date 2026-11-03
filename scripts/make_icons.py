"""Generate the GilzaBots app icons from one geometric drawing.

The icon is drawn with Pillow (no SVG toolchain is needed) at 4x the target
size and downsampled, which gives clean anti-aliased edges at every size.

Run from the repository root:

    python scripts/make_icons.py

It rewrites the desktop icons (icon.png, gilzabots/icon.png, AppIcons/*,
gui_src/public/splash-icon.png), the Windows .ico files, and the Android
launcher icons and splash screens under mobile/.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

# Palette (see gui_src/src/index.css): indigo-violet accent on the dark surface.
GRADIENT_TOP = (139, 124, 246)     # #8b7cf6
GRADIENT_BOTTOM = (70, 54, 196)    # #4636c4
FACE = (255, 255, 255)
EYES = (46, 35, 145)               # #2e2391
ANTENNA_TIP = (196, 181, 253)      # #c4b5fd
SPLASH_BG = (15, 17, 23)           # #0f1117
ADAPTIVE_BG = "#5847d6"

SUPERSAMPLE = 4


def _gradient(size: int) -> Image.Image:
    """Diagonal gradient from the top-left to the bottom-right corner."""
    small = Image.new("RGB", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            t = (x + y) / 510
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(GRADIENT_TOP, GRADIENT_BOTTOM))
    return small.resize((size, size), Image.BICUBIC)


def _robot(draw: ImageDraw.ImageDraw, size: int, box: tuple[float, float, float, float]) -> None:
    """Draw the robot head inside ``box`` (fractions of ``size``: x0, y0, x1, y1)."""
    x0, y0, x1, y1 = (v * size for v in box)
    w, h = x1 - x0, y1 - y0
    # Face: a wide rounded rectangle in the lower part of the box.
    fx0, fx1 = x0 + 0.12 * w, x1 - 0.12 * w
    fy0, fy1 = y0 + 0.34 * h, y1 - 0.06 * h
    fh = fy1 - fy0
    draw.rounded_rectangle((fx0, fy0, fx1, fy1), radius=0.32 * fh, fill=FACE)
    # Ears: small rounded tabs on both sides of the face.
    ew, eh = 0.07 * w, 0.30 * fh
    ey0 = fy0 + (fh - eh) / 2
    draw.rounded_rectangle((fx0 - ew, ey0, fx0 + ew * 0.3, ey0 + eh), radius=ew * 0.5, fill=FACE)
    draw.rounded_rectangle((fx1 - ew * 0.3, ey0, fx1 + ew, ey0 + eh), radius=ew * 0.5, fill=FACE)
    # Antenna: a stem and a round tip above the face.
    cx = (x0 + x1) / 2
    sw = 0.045 * w
    tip_r = 0.075 * w
    tip_cy = y0 + 0.11 * h
    draw.rounded_rectangle((cx - sw / 2, tip_cy, cx + sw / 2, fy0 + 1), radius=sw / 2, fill=FACE)
    draw.ellipse((cx - tip_r, tip_cy - tip_r, cx + tip_r, tip_cy + tip_r), fill=ANTENNA_TIP)
    # Eyes: two vertical pills.
    pw, ph = 0.10 * w, 0.40 * fh
    py0 = fy0 + 0.27 * fh
    for ex in (fx0 + 0.30 * (fx1 - fx0), fx0 + 0.70 * (fx1 - fx0)):
        draw.rounded_rectangle((ex - pw / 2, py0, ex + pw / 2, py0 + ph), radius=pw / 2, fill=EYES)


def app_icon(size: int, shape: str = "squircle") -> Image.Image:
    """The full icon: gradient tile (rounded square or circle) with the robot."""
    big = size * SUPERSAMPLE
    tile = _gradient(big).convert("RGBA")
    mask = Image.new("L", (big, big), 0)
    mdraw = ImageDraw.Draw(mask)
    if shape == "circle":
        mdraw.ellipse((0, 0, big - 1, big - 1), fill=255)
    else:
        mdraw.rounded_rectangle((0, 0, big - 1, big - 1), radius=0.225 * big, fill=255)
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)
    inset = 0.17 if shape == "circle" else 0.14
    _robot(ImageDraw.Draw(out), big, (inset, inset, 1 - inset, 1 - inset))
    return out.resize((size, size), Image.LANCZOS)


def adaptive_foreground(size: int) -> Image.Image:
    """Android adaptive-icon foreground: the robot alone inside the 66% safe zone."""
    big = size * SUPERSAMPLE
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    _robot(ImageDraw.Draw(out), big, (0.25, 0.25, 0.75, 0.75))
    return out.resize((size, size), Image.LANCZOS)


def splash(width: int, height: int) -> Image.Image:
    out = Image.new("RGB", (width, height), SPLASH_BG)
    side = round(min(width, height) * 0.32)
    icon = app_icon(side)
    out.paste(icon, ((width - side) // 2, (height - side) // 2), icon)
    return out


def _save(img: Image.Image, rel: str) -> None:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    print(f"wrote {rel} {img.size[0]}x{img.size[1]}")


def main() -> None:
    master = app_icon(1024)
    for rel in ("icon.png", "gilzabots/icon.png", "AppIcons/GilzaBotsArt1024.png", "mobile/public/icon.png"):
        _save(master, rel)
    _save(app_icon(2048), "AppIcons/GilzaBotsArt.png")
    _save(app_icon(192), "gui_src/public/splash-icon.png")
    for rel, size in (("AppIcons/Square150x150Logo.png", 150), ("AppIcons/Square44x44Logo.png", 44),
                      ("AppIcons/StoreLogo.png", 50)):
        _save(app_icon(size), rel)

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    app_icon(256).save(ROOT / "AppIcons/GilzaBots.ico", sizes=[(s, s) for s in ico_sizes],
                       append_images=[app_icon(s) for s in ico_sizes])
    print("wrote AppIcons/GilzaBots.ico (multi-size)")
    for s in ico_sizes:
        app_icon(s).save(ROOT / f"AppIcons/GilzaBots{s}.ico", sizes=[(s, s)])
        print(f"wrote AppIcons/GilzaBots{s}.ico")

    res = "mobile/android/app/src/main/res"
    for density, launcher in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        _save(app_icon(launcher), f"{res}/mipmap-{density}/ic_launcher.png")
        _save(app_icon(launcher, "circle"), f"{res}/mipmap-{density}/ic_launcher_round.png")
        _save(adaptive_foreground(round(launcher * 2.25)), f"{res}/mipmap-{density}/ic_launcher_foreground.png")
    for folder in sorted(os.listdir(ROOT / res)):
        if folder.startswith("drawable") and (ROOT / res / folder / "splash.png").exists():
            w, h = Image.open(ROOT / res / folder / "splash.png").size
            _save(splash(w, h), f"{res}/{folder}/splash.png")

    colors = ROOT / res / "values/ic_launcher_background.xml"
    colors.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
        f'    <color name="ic_launcher_background">{ADAPTIVE_BG}</color>\n</resources>\n',
        encoding="utf-8",
    )
    print(f"wrote {colors.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
