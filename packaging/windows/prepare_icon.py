from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[2]
assets = root / "desktop/Momoi.Desktop/Assets"
with Image.open(root / "web/public/icon-512.png") as source:
    image = source.convert("RGBA")
    image.save(assets / "momoi.png")
    # WIC's ICO decoder does not reliably support PNG-compressed icon frames.
    image.save(assets / "momoi.ico", bitmap_format="bmp", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
