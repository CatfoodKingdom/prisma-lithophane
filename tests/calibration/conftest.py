"""Path setup for Prisma calibration tests."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CAL_DIR = _PROJECT_ROOT / "Prisma" / "calibration"
_PRISMA_DIR = _PROJECT_ROOT / "Prisma"

# Make calibration modules importable
if str(_CAL_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_DIR))
if str(_PRISMA_DIR) not in sys.path:
    sys.path.insert(0, str(_PRISMA_DIR))
