"""Isolated interpreter entry for the shell-owned media service."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from momoi.qq_call.broker import main

if __name__ == '__main__':
    main()
