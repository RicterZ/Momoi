"""Release-local entry; isolated Python does not implicitly import this directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from momoi_desktop.backend import main

if __name__ == "__main__":
    main()
