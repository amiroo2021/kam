from __future__ import annotations
import sys
from pathlib import Path

GF_ROOT = Path(__file__).resolve().parents[1] / "GoldenFibo"
if GF_ROOT.is_dir() and str(GF_ROOT) not in sys.path:
    sys.path.insert(0, str(GF_ROOT))
