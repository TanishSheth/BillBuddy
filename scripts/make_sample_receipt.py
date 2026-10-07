"""Generate fixtures/sample-receipt.png used by OCR regression tests."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> Path:
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "fixtures"
    fixtures.mkdir(exist_ok=True)
    img = Image.new("RGB", (480, 720), "white")
    draw = ImageDraw.Draw(img)
    try:
        font_lg = ImageFont.truetype("arial.ttf", 36)
        font = ImageFont.truetype("arial.ttf", 28)
        font_sm = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font_lg = font = font_sm = ImageFont.load_default()

    draw.text((40, 40), "SHARE SQUAD CAFE", fill="black", font=font_lg)
    draw.text((40, 100), "123 Demo Street", fill="black", font=font_sm)
    draw.text((40, 140), "Date: 10/07/2026", fill="black", font=font)
    draw.text((40, 220), "Latte            5.50", fill="black", font=font)
    draw.text((40, 270), "Muffin           3.75", fill="black", font=font)
    draw.text((40, 320), "Tax              0.75", fill="black", font=font)
    draw.text((40, 390), "SUBTOTAL        10.00", fill="black", font=font)
    draw.text((40, 450), "TOTAL           13.25", fill="black", font=font_lg)
    draw.text((40, 520), "CARD            13.25", fill="black", font=font)
    path = fixtures / "sample-receipt.png"
    img.save(path)
    return path


if __name__ == "__main__":
    print(main())
