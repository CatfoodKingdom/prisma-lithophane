"""Path setup for Prisma calibration tests."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CAL_DIR = _PROJECT_ROOT / "Prisma" / "calibration"
_PRISMA_DIR = _PROJECT_ROOT / "Prisma"

# Make calibration modules importable and remove the other app's bare-import
# path.  This is needed both for combined runs and when pytest is given only a
# calibration selection (where the root collection hook is not guaranteed to
# run first).
_GEN_DIR = _PROJECT_ROOT / "Prisma" / "generator"
for _path in (str(_CAL_DIR), str(_GEN_DIR), str(_PRISMA_DIR)):
    while _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, str(_PRISMA_DIR))
sys.path.insert(0, str(_CAL_DIR))
_server = sys.modules.get("server")
if _server is not None and str(_GEN_DIR) in str(getattr(_server, "__file__", "")):
    sys.modules.pop("server", None)
