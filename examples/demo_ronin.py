"""Standalone runner for the braille animation demo.

This file lets you run the demo without installing the package:

    python3 examples/demo_ronin.py

Once Ronin is installed (`pip install -e .`) the same demo is available
as `ronin demo`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running from the repo root without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ronin.demos.braille import RoninDemo

ASSETS = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "nusamurai"
    / "pos-th30"
)

if __name__ == "__main__":
    RoninDemo(assets_dir=ASSETS).run()
