"""Nobetcim PWA ikonlarını üretir (kırmızı yuvarlak kare + beyaz 'E')."""
import os

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(OUT, exist_ok=True)
RED = (211, 47, 47, 255)   # #d32f2f
FONT_PATH = "C:/Windows/Fonts/arialbd.ttf"


def font_for(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size)


def draw_e(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    """Verilen kutunun içine ortalı beyaz 'E' çizer."""
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    fs = int(min(bw, bh) * 0.66)
    font = font_for(fs)
    bbox = d.textbbox((0, 0), "E", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + (bw - tw) / 2 - bbox[0]
    ty = y0 + (bh - th) / 2 - bbox[1]
    d.text((tx, ty), "E", font=font, fill=(255, 255, 255, 255))


def make_rounded(size: int, path: str) -> None:
    """Şeffaf köşeli, kırmızı yuvarlak kare + E (Android 'any')."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=RED)
    draw_e(img, (0, 0, size, size))
    img.save(path)


def make_maskable(size: int, path: str) -> None:
    """Kenara kadar dolu kırmızı (maskable); E güvenli bölgede (içte)."""
    img = Image.new("RGBA", (size, size), RED)
    pad = int(size * 0.18)
    draw_e(img, (pad, pad, size - pad, size - pad))
    img.save(path)


def make_apple(size: int, path: str) -> None:
    """iOS apple-touch: opak kare (iOS köşeleri kendi yuvarlar)."""
    img = Image.new("RGB", (size, size), RED[:3])
    draw_e(img, (0, 0, size, size))
    img.save(path)


def main() -> None:
    make_rounded(192, os.path.join(OUT, "icon-192.png"))
    make_rounded(512, os.path.join(OUT, "icon-512.png"))
    make_maskable(512, os.path.join(OUT, "icon-512-maskable.png"))
    make_apple(180, os.path.join(OUT, "apple-touch-icon.png"))
    make_rounded(32, os.path.join(OUT, "favicon-32.png"))
    print("İkonlar üretildi:", os.listdir(OUT))


if __name__ == "__main__":
    main()
