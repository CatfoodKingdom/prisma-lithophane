"""
blank_registry.py — Blank image registration logic.

Handles registering CR2 images as flatfield references: EXIF extraction,
file copying to blanks/ storage, and ID assignment.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import sys as _sys
from pathlib import Path as _Path
_CAL_DIR = _Path(__file__).resolve().parent.parent
if str(_CAL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_CAL_DIR))

from data_access import DataStore
from models import Blank
from path_safety import require_unlinked_path, safe_unlink


def _extract_exif_timestamp(image_path: Path) -> Optional[str]:
    """
    Try to extract the EXIF DateTimeOriginal from a CR2 file.

    Uses exifread to parse image metadata without decoding RAW pixel data.
    Returns None if exifread is unavailable or the timestamp cannot be read.
    """
    # Try exifread (lightweight, pure-Python EXIF reader)
    try:
        import exifread
        with open(image_path, "rb") as f:
            tags = exifread.process_file(f, stop_tag="EXIF DateTimeOriginal", details=False)
        dt_tag = tags.get("EXIF DateTimeOriginal")
        if dt_tag:
            # EXIF format: "2026:03:25 14:30:00" → ISO: "2026-03-25T14:30:00"
            raw = str(dt_tag)
            return raw.replace(":", "-", 2).replace(" ", "T", 1)
    except ImportError:
        pass
    except Exception:
        pass

    return None


def register_blank_image(
    store: DataStore,
    filename: str,
    session_tag: Optional[str] = None,
) -> Blank:
    """
    Register a CR2 image as a blank (flatfield reference).

    Steps:
    1. Verify the image file exists in the images directory
    2. Try to extract EXIF timestamp from the CR2
    3. Assign the next blank ID via store.next_blank_id()
    4. Copy the file to blanks/ storage with the blank ID as filename
    5. Register via store.register_blank()
    6. Return the Blank object

    Raises:
        FileNotFoundError: if the image file does not exist in images/
        RuntimeError: if the registration process fails
    """
    # 1. Verify the source image exists
    image_path = store.get_image_path(filename)
    if image_path is None:
        raise FileNotFoundError(f"Image '{filename}' not found in images directory")
    require_unlinked_path(Path(image_path), Path(store.root))

    # 1b. Check if this image is already registered as a blank
    existing_blanks = store.list_blanks()
    for b in existing_blanks:
        if b.original_filename == filename:
            raise RuntimeError(
                f"Image '{filename}' is already registered as {b.blank_id}"
            )

    # 2. Extract EXIF timestamp (best-effort)
    exif_ts = _extract_exif_timestamp(image_path)

    # 3. Assign next blank ID
    blank_id = store.next_blank_id()

    # 4. Copy file to blanks/ storage
    suffix = image_path.suffix  # e.g. ".CR2"
    dest_path = store.root / "blanks" / f"{blank_id}{suffix}"
    require_unlinked_path(dest_path, Path(store.root))
    try:
        shutil.copy2(str(image_path), str(dest_path))
    except OSError as exc:
        raise RuntimeError(f"Failed to copy blank image to storage: {exc}") from exc

    # 5. Build and register the Blank object
    blank = Blank(
        blank_id=blank_id,
        original_filename=filename,
        registered_at=datetime.now().isoformat(),
        exif_timestamp=exif_ts,
        storage_path=f"blanks/{blank_id}{suffix}",
        session_tag=session_tag,
    )

    try:
        store.register_blank(blank)
    except Exception as exc:
        # Clean up the copied file if registration fails
        safe_unlink(dest_path, Path(store.root))
        raise RuntimeError(f"Failed to register blank: {exc}") from exc

    return blank
