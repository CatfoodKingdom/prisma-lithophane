"""Packaged guide-image catalog and once-per-workspace example-image seeding."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from PIL import Image


ASSET_MANIFEST_SCHEMA_VERSION = 1
SEED_STATE_SCHEMA_VERSION = 1


class GuideAssetError(ValueError):
    """Raised when packaged guide assets cannot be trusted or resolved."""


class ExampleImageSeedStateError(ValueError):
    """Raised when once-only public-image seed history is unreadable."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class GuideAssetCatalog:
    """Validated allowlist of immutable, product-owned image assets."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.manifest_path = self.root / "manifest.json"
        self._assets = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GuideAssetError(f"guide asset manifest could not be read: {exc}") from exc
        if raw.get("schema_version") != ASSET_MANIFEST_SCHEMA_VERSION:
            raise GuideAssetError("unsupported guide asset manifest schema")
        assets: dict[str, dict[str, Any]] = {}
        for entry in raw.get("assets", []):
            if not isinstance(entry, Mapping):
                raise GuideAssetError("guide asset entries must be objects")
            asset_id = str(entry.get("id") or "").strip()
            filename = str(entry.get("filename") or "").strip()
            expected_digest = str(entry.get("sha256") or "").strip().lower()
            display_name = str(entry.get("guide_display_name") or "").strip()
            public_name = str(entry.get("public_seed_filename") or "").strip()
            license_reference = str(entry.get("license") or "").strip()
            if not asset_id or not filename or asset_id in assets:
                raise GuideAssetError("guide asset IDs and filenames must be unique and non-empty")
            if Path(filename).name != filename:
                raise GuideAssetError(f"guide asset filename is unsafe: {filename}")
            if len(expected_digest) != 64 or any(character not in "0123456789abcdef" for character in expected_digest):
                raise GuideAssetError(f"guide asset digest is invalid: {asset_id}")
            if not display_name or Path(display_name).name != display_name:
                raise GuideAssetError(f"guide asset display name is invalid: {asset_id}")
            if not license_reference:
                raise GuideAssetError(f"guide asset license reference is missing: {asset_id}")
            if entry.get("seed_public") and (
                not public_name or Path(public_name).name != public_name
            ):
                raise GuideAssetError(f"public guide asset filename is invalid: {asset_id}")
            path = (self.root / filename).resolve()
            if self.root not in path.parents or not path.is_file():
                raise GuideAssetError(f"guide asset is missing: {asset_id}")
            actual_digest = _sha256(path)
            if actual_digest != expected_digest:
                raise GuideAssetError(f"guide asset digest mismatch: {asset_id}")
            with Image.open(path) as image:
                width, height = image.size
                source_format = str(image.format or path.suffix.lstrip(".")).lower()
            assets[asset_id] = {
                **deepcopy(dict(entry)),
                "id": asset_id,
                "path": path,
                "width": width,
                "height": height,
                "size_kb": round(path.stat().st_size / 1024, 1),
                "source_format": source_format,
            }
        return assets

    def get(self, asset_id: str) -> dict[str, Any]:
        canonical = str(asset_id or "").strip()
        asset = self._assets.get(canonical)
        if asset is None:
            raise GuideAssetError(f"unknown guide asset: {canonical}")
        return deepcopy(asset)

    def resolve(self, asset_id: str) -> Path:
        return Path(self.get(asset_id)["path"])

    def metadata(self, asset_id: str) -> dict[str, Any]:
        asset = self.get(asset_id)
        return {
            "asset_id": asset["id"],
            "filename": asset["guide_display_name"],
            "width": asset["width"],
            "height": asset["height"],
            "size_kb": asset["size_kb"],
            "source_format": asset["source_format"],
            "normalized": False,
            "source_ref": f"guide-image:{asset['id']}",
            "virtual": True,
            "deletable": False,
            "renameable": False,
        }

    def public_assets(self) -> list[dict[str, Any]]:
        return [deepcopy(asset) for asset in self._assets.values() if asset.get("seed_public")]


class ExampleImageSeeder:
    """Seed normal user images once without reviving later deletions."""

    def __init__(self, catalog: GuideAssetCatalog, image_dir: Path, state_path: Path) -> None:
        self.catalog = catalog
        self.image_dir = Path(image_dir)
        self.state_path = Path(state_path)
        self._lock = threading.RLock()

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": SEED_STATE_SCHEMA_VERSION, "seeded": {}}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExampleImageSeedStateError(
                f"example image seed history is unreadable; no images were seeded: {exc}"
            ) from exc
        if raw.get("schema_version") != SEED_STATE_SCHEMA_VERSION or not isinstance(raw.get("seeded"), dict):
            raise ExampleImageSeedStateError(
                "example image seed history has an unsupported schema; no images were seeded"
            )
        return raw

    @staticmethod
    def _legacy_candidates(image_dir: Path, asset: Mapping[str, Any]) -> list[Path]:
        public_name = str(asset.get("public_seed_filename") or "")
        names = [public_name]
        if public_name:
            public_path = Path(public_name)
            names.extend(
                path.name
                for path in image_dir.glob(f"{public_path.stem}*{public_path.suffix}")
            )
        if asset.get("id") == "bubba-blanket":
            names.extend(path.name for path in image_dir.glob("Prisma Tutorial - Bubba Blanket*.jpg"))
        if image_dir.exists():
            names.extend(path.name for path in image_dir.iterdir() if path.is_file())
        return [image_dir / name for name in dict.fromkeys(names) if name]

    def _existing_matching_copy(self, asset: Mapping[str, Any]) -> Path | None:
        for candidate in self._legacy_candidates(self.image_dir, asset):
            try:
                if candidate.is_file() and _sha256(candidate) == asset["sha256"]:
                    return candidate
            except OSError:
                continue
        return None

    def _destination(self, preferred_name: str) -> Path:
        preferred = self.image_dir / preferred_name
        if not preferred.exists():
            return preferred
        stem, suffix = preferred.stem, preferred.suffix
        index = 2
        while True:
            candidate = self.image_dir / f"{stem} ({index}){suffix}"
            if not candidate.exists():
                return candidate
            index += 1

    def _publish_without_overwrite(self, temporary: Path, preferred_name: str) -> Path:
        """Atomically publish a same-volume temporary file without replacing user data."""
        while True:
            destination = self._destination(preferred_name)
            try:
                # A hard-link publication is atomic and fails if another process
                # creates the destination after _destination() inspects it. Both
                # paths are in the Images directory, so they share a volume.
                os.link(temporary, destination)
            except FileExistsError:
                continue
            except OSError as exc:
                raise ExampleImageSeedStateError(
                    f"example image could not be published safely: {exc}"
                ) from exc
            temporary.unlink(missing_ok=True)
            return destination

    def seed(self) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            seeded = state["seeded"]
            results: list[dict[str, Any]] = []
            self.image_dir.mkdir(parents=True, exist_ok=True)
            for asset in self.catalog.public_assets():
                asset_id = asset["id"]
                if asset_id in seeded:
                    results.append({"asset_id": asset_id, "status": "already-recorded"})
                    continue
                existing = self._existing_matching_copy(asset)
                if existing is not None:
                    destination = existing
                    status = "adopted-existing"
                else:
                    preferred_name = str(asset["public_seed_filename"])
                    temporary = self.image_dir / (
                        f".{Path(preferred_name).name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
                    )
                    try:
                        with Path(asset["path"]).open("rb") as source, temporary.open("xb") as output:
                            for block in iter(lambda: source.read(1024 * 1024), b""):
                                output.write(block)
                            output.flush()
                            os.fsync(output.fileno())
                        destination = self._publish_without_overwrite(temporary, preferred_name)
                    finally:
                        temporary.unlink(missing_ok=True)
                    status = "seeded"
                seeded[asset_id] = {
                    "filename": destination.name,
                    "sha256": asset["sha256"],
                }
                _atomic_json_write(self.state_path, state)
                results.append({"asset_id": asset_id, "status": status, "filename": destination.name})
            return {"results": results, "state": deepcopy(state)}
