from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[2]
with Image.open(root / "web/public/icon-512.png") as image:
    image.save(root / "desktop/Momoi.Desktop/Assets/momoi.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
