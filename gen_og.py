"""Sosyal medya paylaşım görseli (OG image, 1200x630) üretir."""
import os

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "static", "og-image.png")
BOLD = "C:/Windows/Fonts/arialbd.ttf"
REG = "C:/Windows/Fonts/arial.ttf"
RED = (211, 47, 47, 255)
TEAL = (36, 107, 143, 255)
DARK = (31, 45, 58, 255)
MUTED = (103, 120, 137, 255)
BG = (244, 247, 250, 255)

W, H = 1200, 630


def main() -> None:
    img = Image.new("RGB", (W, H), BG[:3])
    d = ImageDraw.Draw(img)

    # Üstte ince teal şerit
    d.rectangle([0, 0, W, 12], fill=TEAL[:3])

    # Sol: kırmızı yuvarlak E logosu
    lx, ly, ls = 90, 200, 230
    d.rounded_rectangle([lx, ly, lx + ls, ly + ls], radius=int(ls * 0.22), fill=RED[:3])
    ef = ImageFont.truetype(BOLD, int(ls * 0.66))
    bb = d.textbbox((0, 0), "E", font=ef)
    d.text((lx + (ls - (bb[2] - bb[0])) / 2 - bb[0],
            ly + (ls - (bb[3] - bb[1])) / 2 - bb[1]), "E", font=ef, fill="white")

    # Sağ: metin bloğu
    tx = lx + ls + 70
    d.text((tx, 195), "Nobetcim", font=ImageFont.truetype(BOLD, 96), fill=TEAL[:3])
    d.text((tx, 310), "En Yakın Nöbetçi Eczane", font=ImageFont.truetype(BOLD, 50), fill=DARK[:3])
    d.text((tx, 378), "Konumuna göre, anında — tüm Türkiye, ücretsiz.",
           font=ImageFont.truetype(REG, 33), fill=MUTED[:3])

    # Alt: site adresi
    d.text((tx, 470), "nobetcim.com.tr", font=ImageFont.truetype(BOLD, 40), fill=TEAL[:3])

    img.save(OUT)
    print("OG görseli üretildi:", OUT)


if __name__ == "__main__":
    main()
