"""Wrapper executável para o CLI unificado do Fast Clipper."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastclipper.cli import main

if __name__ == "__main__":
    sys.exit(main())
