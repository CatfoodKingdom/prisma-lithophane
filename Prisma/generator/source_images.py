"""Source-image discovery, validation, normalization, and background imports.

User-owned originals always remain in the visible Images directory. Formats
that Prisma must normalize are decoded into an app-owned, disposable cache.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree

import numpy as np
from PIL import Image, ImageCms, ImageOps
from PIL.MpoImagePlugin import MpoImageFile

try:
    from pi_heif import register_heif_opener
except ImportError:  # pragma: no cover - exercised by release capability checks
    register_heif_opener = None
else:
    register_heif_opener(
        thumbnails=False,
        depth_images=False,
        aux_images=False,
        decode_threads=1,
    )


NATIVE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".jfif", ".jpe", ".tif", ".tiff", ".bmp", ".webp",
})
NORMALIZED_EXTENSIONS = frozenset({".heic", ".heif", ".hif", ".avif"})
SUPPORTED_EXTENSIONS = NATIVE_EXTENSIONS | NORMALIZED_EXTENSIONS
KNOWN_UNSUPPORTED_IMAGE_EXTENSIONS = frozenset({
    ".gif", ".jp2", ".j2k", ".jxl", ".mpo",
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2", ".orf",
})
NORMALIZATION_VERSION = 1
DEFAULT_CACHE_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
MAX_JOB_HISTORY = 64
SOURCE_VARIANT_SINGLE_FRAME = "single_frame"
SOURCE_VARIANT_HDR_GAIN_MAP = "hdr_gain_map"

_JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg", ".jfif", ".jpe"})
_MAX_XMP_BYTES = 1024 * 1024
_APPLE_AUXILIARY_TYPE_TAG = (
    "{http://ns.apple.com/pixeldatainfo/1.0/}AuxiliaryImageType"
)
_APPLE_HDR_GAIN_MAP_VERSION_TAG = (
    "{http://ns.apple.com/HDRGainMap/1.0/}HDRGainMapVersion"
)
_APPLE_HDR_GAIN_MAP_TYPE = "urn:com:apple:photo:2020:aux:hdrgainmap"
_ANDROID_HDR_GAIN_MAP_VERSION_TAG = (
    "{http://ns.adobe.com/hdr-gain-map/1.0/}Version"
)

_HDR_TRANSFERS = {16, 18}  # H.273 PQ / HLG
_SRGB_TRANSFER = 13
_LINEAR_TRANSFER = 8
_BT709_LIKE_TRANSFERS = {1, 6, 14, 15}

_RGB_TO_XYZ = {
    # H.273 primaries: BT.709/sRGB, BT.2020, Display-P3.
    1: np.array([
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ], dtype=np.float64),
    9: np.array([
        [0.6369580, 0.1446169, 0.1688810],
        [0.2627002, 0.6779981, 0.0593017],
        [0.0000000, 0.0280727, 1.0609851],
    ], dtype=np.float64),
    12: np.array([
        [0.4865709, 0.2656677, 0.1982173],
        [0.2289746, 0.6917385, 0.0792869],
        [0.0000000, 0.0451134, 1.0439444],
    ], dtype=np.float64),
}
_XYZ_TO_SRGB = np.linalg.inv(_RGB_TO_XYZ[1])
_EXPECTED_FORMATS = {
    ".png": {"PNG"},
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".jfif": {"JPEG"},
    ".jpe": {"JPEG"},
    ".tif": {"TIFF"},
    ".tiff": {"TIFF"},
    ".bmp": {"BMP"},
    ".webp": {"WEBP"},
    ".heic": {"HEIF", "HEIC"},
    ".heif": {"HEIF", "HEIC"},
    ".hif": {"HEIF", "HEIC"},
    ".avif": {"AVIF"},
}


class SourceImageError(ValueError):
    """A user-facing source image validation or conversion failure."""


@dataclass(frozen=True)
class SourceInspection:
    container_format: str
    source_format: str
    variant: str
    width: int
    height: int
    frame_count: int
    requires_normalization: bool


@dataclass(frozen=True)
class ResolvedSource:
    original_path: Path
    working_path: Path
    display_name: str
    source_format: str
    fingerprint: str
    normalized: bool
    width: int
    height: int
    source_variant: str = SOURCE_VARIANT_SINGLE_FRAME

    def provenance(self) -> dict:
        provenance = {
            "original_source_name": self.display_name,
            "original_source_format": self.source_format,
            "normalized_source_name": self.working_path.name if self.normalized else self.display_name,
            "source_digest": self.fingerprint,
            "normalization_version": NORMALIZATION_VERSION if self.normalized else None,
            "normalized": self.normalized,
        }
        if self.source_variant != SOURCE_VARIANT_SINGLE_FRAME:
            provenance["source_variant"] = self.source_variant
        return provenance


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_signature(source_format: str, suffix: str) -> None:
    expected = _EXPECTED_FORMATS.get(suffix.lower())
    if expected is None or source_format.upper() not in expected:
        raise SourceImageError(
            f"The file contents do not match the {suffix.upper() or 'image'} filename extension."
        )


def _parse_xmp(payload: bytes | str | None) -> ElementTree.Element | None:
    if not payload:
        return None
    encoded = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if len(encoded) > _MAX_XMP_BYTES:
        return None
    lowered = encoded.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    try:
        return ElementTree.fromstring(encoded)
    except (ElementTree.ParseError, ValueError, RecursionError):
        return None


def _xmp_values(root: ElementTree.Element | None, expanded_name: str) -> list[str]:
    if root is None:
        return []
    values: list[str] = []
    for element in root.iter():
        if element.tag == expanded_name and element.text is not None:
            values.append(element.text.strip())
        attribute = element.attrib.get(expanded_name)
        if attribute is not None:
            values.append(attribute.strip())
    return values


def _is_apple_hdr_gain_map_xmp(payload: bytes | str | None) -> bool:
    root = _parse_xmp(payload)
    auxiliary_types = _xmp_values(root, _APPLE_AUXILIARY_TYPE_TAG)
    versions = _xmp_values(root, _APPLE_HDR_GAIN_MAP_VERSION_TAG)
    return (
        auxiliary_types == [_APPLE_HDR_GAIN_MAP_TYPE]
        and len(versions) == 1
        and versions[0].isdigit()
        and int(versions[0]) > 0
    )


def _is_android_hdr_gain_map_xmp(payload: bytes | str | None) -> bool:
    versions = _xmp_values(_parse_xmp(payload), _ANDROID_HDR_GAIN_MAP_VERSION_TAG)
    return versions == ["1.0"]


def _multi_picture_error() -> SourceImageError:
    return SourceImageError(
        "This JPEG contains multiple pictures. Prisma can use an auxiliary HDR gain "
        "map, but not stereo, panorama, or other multi-picture JPEGs."
    )


def _ambiguous_auxiliary_error() -> SourceImageError:
    return SourceImageError(
        "Prisma can read the main JPEG but cannot safely identify its additional "
        "image as a supported HDR gain map."
    )


@dataclass(frozen=True)
class _MpfFrame:
    index: int
    offset: int
    length: int
    mode: str
    xmp: bytes | str | None


def _mpf_frames(path: Path) -> list[_MpfFrame]:
    """Read bounded MPF frame headers through Pillow's structured MPO view."""

    file_size = path.stat().st_size
    try:
        with MpoImageFile(path) as image:
            entries = image.mpinfo.get(0xB002)
            # Pillow exposes the parsed MP entries publicly but currently keeps
            # their resolved absolute offsets private. Keep this dependency in
            # one guarded function so a Pillow upgrade fails closed.
            offsets = getattr(image, "_MpoImageFile__mpoffsets", None)
            if not isinstance(entries, list) or not isinstance(offsets, list):
                raise _ambiguous_auxiliary_error()
            if image.n_frames != len(entries) or image.n_frames != len(offsets):
                raise _ambiguous_auxiliary_error()
            frames: list[_MpfFrame] = []
            for index, (entry, offset) in enumerate(zip(entries, offsets)):
                if not isinstance(entry, dict):
                    raise _ambiguous_auxiliary_error()
                length = int(entry.get("Size", 0))
                offset = int(offset)
                if length <= 0 or offset < 0 or offset + length > file_size:
                    raise _ambiguous_auxiliary_error()
                image.seek(index)
                frames.append(
                    _MpfFrame(
                        index=index,
                        offset=offset,
                        length=length,
                        mode=str(image.mode),
                        xmp=image.info.get("xmp"),
                    )
                )
            return frames
    except SourceImageError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, EOFError, SyntaxError) as exc:
        raise _ambiguous_auxiliary_error() from exc


def _classify_mpf(path: Path, primary_xmp: bytes | str | None) -> tuple[str, int]:
    frames = _mpf_frames(path)
    if len(frames) <= 1:
        return SOURCE_VARIANT_SINGLE_FRAME, len(frames)
    if len(frames) != 2:
        raise _multi_picture_error()
    apple_gain_map = _is_apple_hdr_gain_map_xmp(frames[1].xmp)
    android_gain_map = _is_android_hdr_gain_map_xmp(primary_xmp)
    if apple_gain_map or android_gain_map:
        return SOURCE_VARIANT_HDR_GAIN_MAP, len(frames)
    if frames[1].xmp:
        raise _ambiguous_auxiliary_error()
    raise _multi_picture_error()


def _multi_frame_error(source_format: str) -> SourceImageError:
    if source_format == "TIFF":
        return SourceImageError(
            "Multi-page TIFF images are not supported. Export the page you want as a "
            "single JPEG, PNG, or TIFF."
        )
    if source_format == "WEBP":
        return SourceImageError(
            "Animated WebP images are not supported. Export one still frame as JPEG or PNG."
        )
    if source_format == "PNG":
        return SourceImageError(
            "Animated PNG images are not supported. Export one still frame as JPEG or PNG."
        )
    return SourceImageError(
        f"Multi-frame {source_format or 'image'} files are not supported. "
        "Export one still frame as JPEG or PNG."
    )


def _enum_int(value, default: int = 2) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _srgb_to_linear(values: np.ndarray) -> np.ndarray:
    return np.where(
        values <= 0.04045,
        values / 12.92,
        ((values + 0.055) / 1.055) ** 2.4,
    )


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0.0, 1.0)
    return np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * (values ** (1.0 / 2.4)) - 0.055,
    )


def _bt709_to_linear(values: np.ndarray) -> np.ndarray:
    return np.where(
        values < 0.081,
        values / 4.5,
        ((values + 0.099) / 1.099) ** (1.0 / 0.45),
    )


def _normalize_nclx(image: Image.Image, profile: dict) -> Image.Image:
    primaries = _enum_int(profile.get("color_primaries"))
    transfer = _enum_int(profile.get("transfer_characteristics"))
    if transfer in _HDR_TRANSFERS:
        raise SourceImageError(
            "This image is HDR-only (PQ/HLG). Export an SDR sRGB JPEG or PNG and try again."
        )
    if primaries == 2 and transfer == 2:
        return image.convert("RGB")
    if primaries not in _RGB_TO_XYZ:
        raise SourceImageError(
            f"Unsupported image color primaries ({primaries}). Export an sRGB JPEG or PNG."
        )
    encoded = np.asarray(image.convert("RGB"), dtype=np.float64) / 255.0
    if transfer == _SRGB_TRANSFER:
        linear = _srgb_to_linear(encoded)
    elif transfer == _LINEAR_TRANSFER:
        linear = encoded
    elif transfer in _BT709_LIKE_TRANSFERS:
        linear = _bt709_to_linear(encoded)
    elif transfer == 2:
        # An unspecified transfer with BT.709 or Display-P3 primaries is the
        # common phone-camera fallback; libheif has already produced RGB bytes.
        linear = _srgb_to_linear(encoded)
    else:
        raise SourceImageError(
            f"Unsupported image transfer characteristic ({transfer}). "
            "Export an SDR sRGB JPEG or PNG."
        )
    xyz = linear @ _RGB_TO_XYZ[primaries].T
    srgb_linear = xyz @ _XYZ_TO_SRGB.T
    encoded_srgb = _linear_to_srgb(srgb_linear)
    return Image.fromarray(np.rint(encoded_srgb * 255.0).astype(np.uint8), mode="RGB")


def normalize_to_srgb(image: Image.Image) -> Image.Image:
    """Apply orientation and available color metadata, returning owned RGB pixels."""

    image.load()
    oriented = ImageOps.exif_transpose(image)
    icc = oriented.info.get("icc_profile")
    if icc:
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            output_profile = ImageCms.createProfile("sRGB")
            converted = ImageCms.profileToProfile(
                oriented.convert("RGB"),
                source_profile,
                output_profile,
                outputMode="RGB",
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SourceImageError(
                "The embedded color profile is invalid. Export an sRGB JPEG or PNG."
            ) from exc
        return converted.copy()
    nclx = oriented.info.get("nclx_profile")
    if isinstance(nclx, dict):
        return _normalize_nclx(oriented, nclx)
    return oriented.convert("RGB").copy()


class SourceImageService:
    """Resolve visible source assets to stable native or normalized rasters."""

    def __init__(
        self,
        images_dir: Path,
        cache_dir: Path,
        *,
        cache_limit_bytes: int = DEFAULT_CACHE_LIMIT_BYTES,
    ):
        self.images_dir = Path(images_dir)
        self.cache_dir = Path(cache_dir)
        self.cache_limit_bytes = max(0, int(cache_limit_bytes))
        self.manifest_path = self.cache_dir / "manifest.json"
        self._lock = threading.RLock()
        self._manifest = self._load_manifest()

    @property
    def heif_available(self) -> bool:
        return register_heif_opener is not None

    def _load_manifest(self) -> dict:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"schema_version": 1, "entries": {}, "native_entries": {}}
        if raw.get("schema_version") != 1 or not isinstance(raw.get("entries"), dict):
            return {"schema_version": 1, "entries": {}, "native_entries": {}}
        if not isinstance(raw.get("native_entries"), dict):
            raw["native_entries"] = {}
        return raw

    def _write_manifest(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(f".manifest-{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self._manifest, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def _safe_source(self, filename: str) -> Path:
        name = Path(str(filename)).name
        if not name or name != str(filename):
            raise SourceImageError("Invalid image filename")
        path = (self.images_dir / name).resolve()
        if not path.is_relative_to(self.images_dir.resolve()):
            raise SourceImageError("Invalid image filename")
        return path

    def _entry_for(self, path: Path) -> dict | None:
        entry = self._manifest["entries"].get(path.name)
        if not isinstance(entry, dict):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        cached = self.cache_dir / str(entry.get("cache_name") or "")
        if (
            entry.get("normalization_version") != NORMALIZATION_VERSION
            or entry.get("size") != stat.st_size
            or entry.get("mtime_ns") != stat.st_mtime_ns
            or (
                entry.get("ctime_ns") is not None
                and entry.get("ctime_ns") != stat.st_ctime_ns
            )
            or not cached.is_file()
        ):
            return None
        try:
            with Image.open(cached) as image:
                image.verify()
        except (OSError, ValueError):
            return None
        return entry

    @staticmethod
    def _stat_identity(path: Path) -> tuple[int, int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _native_entry_for(self, path: Path) -> dict | None:
        entry = self._manifest["native_entries"].get(path.name)
        if not isinstance(entry, dict):
            return None
        try:
            size, mtime_ns, ctime_ns = self._stat_identity(path)
        except OSError:
            return None
        if (
            entry.get("size") != size
            or entry.get("mtime_ns") != mtime_ns
            or entry.get("ctime_ns") != ctime_ns
            or not isinstance(entry.get("digest"), str)
        ):
            return None
        return entry

    def _record_native_entry(
        self,
        path: Path,
        *,
        digest: str,
        width: int,
        height: int,
        source_format: str,
        source_variant: str = SOURCE_VARIANT_SINGLE_FRAME,
    ) -> None:
        size, mtime_ns, ctime_ns = self._stat_identity(path)
        self._manifest["entries"].pop(path.name, None)
        self._manifest["native_entries"][path.name] = {
            "digest": digest,
            "source_format": source_format,
            "size": size,
            "mtime_ns": mtime_ns,
            "ctime_ns": ctime_ns,
            "width": int(width),
            "height": int(height),
            "source_variant": source_variant,
            "last_used_ns": time.time_ns(),
        }

    def _record_normalized_entry(
        self,
        path: Path,
        *,
        cache_name: str,
        digest: str,
        width: int,
        height: int,
        source_format: str,
        source_variant: str,
    ) -> None:
        size, mtime_ns, ctime_ns = self._stat_identity(path)
        self._manifest["native_entries"].pop(path.name, None)
        self._manifest["entries"][path.name] = {
            "cache_name": cache_name,
            "digest": digest,
            "source_format": source_format,
            "source_variant": source_variant,
            "size": size,
            "mtime_ns": mtime_ns,
            "ctime_ns": ctime_ns,
            "width": int(width),
            "height": int(height),
            "normalization_version": NORMALIZATION_VERSION,
            "last_used_ns": time.time_ns(),
        }

    def _store_normalized_image(
        self,
        image: Image.Image,
        *,
        digest: str,
    ) -> tuple[Path, int, int]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_name = f"{digest}-v{NORMALIZATION_VERSION}.png"
        cached = self.cache_dir / cache_name
        temporary = self.cache_dir / f".{cache_name}-{uuid.uuid4().hex}.tmp"
        try:
            image.save(temporary, format="PNG", compress_level=6)
            os.replace(temporary, cached)
        finally:
            image.close()
            temporary.unlink(missing_ok=True)
        with Image.open(cached) as prepared:
            width, height = prepared.size
        return cached, width, height

    @staticmethod
    def _resolved_native(path: Path, entry: dict) -> ResolvedSource:
        return ResolvedSource(
            path,
            path,
            path.name,
            str(entry["source_format"]),
            str(entry["digest"]),
            False,
            int(entry["width"]),
            int(entry["height"]),
            str(entry.get("source_variant") or SOURCE_VARIANT_SINGLE_FRAME),
        )

    def _resolved_normalized(self, path: Path, entry: dict) -> ResolvedSource:
        return ResolvedSource(
            path,
            self.cache_dir / str(entry["cache_name"]),
            path.name,
            str(entry["source_format"]),
            str(entry["digest"]),
            True,
            int(entry["width"]),
            int(entry["height"]),
            str(entry.get("source_variant") or SOURCE_VARIANT_SINGLE_FRAME),
        )

    def paths_with_digest(self, digest: str) -> list[Path]:
        """Return ready source paths whose cached source-byte digest matches."""

        canonical = str(digest or "").lower()
        if len(canonical) != 64:
            return []
        ready, _pending = self.discover()
        ready_by_name = {path.name: path for path in ready}
        with self._lock:
            matches = {
                name
                for name, entry in self._manifest["native_entries"].items()
                if isinstance(entry, dict)
                and str(entry.get("digest") or "").lower() == canonical
            }
            matches.update(
                name
                for name, entry in self._manifest["entries"].items()
                if isinstance(entry, dict)
                and str(entry.get("digest") or "").lower() == canonical
            )
        return [ready_by_name[name] for name in matches if name in ready_by_name]

    def is_ready(self, path: Path) -> bool:
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False
        with self._lock:
            if self._entry_for(path) is not None:
                return True
            if self._native_entry_for(path) is not None:
                return True
        if path.suffix.lower() in NATIVE_EXTENSIONS:
            try:
                inspection = self._inspect_native(path)
            except SourceImageError:
                return False
            if inspection.requires_normalization:
                return False
            try:
                self.prepare(path)
            except SourceImageError:
                return False
            return True
        return False

    def describe(self, path: Path) -> dict:
        resolved = self.resolve(path.name, prepare=False)
        return {
            "filename": path.name,
            "width": resolved.width,
            "height": resolved.height,
            "size_kb": round(path.stat().st_size / 1024, 1),
            "thumbnail_url": f"/api/images/preview/{path.name}",
            "source_format": resolved.source_format,
            "normalized": resolved.normalized,
        }

    def _inspect_native(
        self,
        path: Path,
        *,
        expected_suffix: str | None = None,
    ) -> SourceInspection:
        suffix = (expected_suffix or path.suffix).lower()
        try:
            with Image.open(path) as raw:
                container_format = str(raw.format or "").upper()
                if suffix in _JPEG_EXTENSIONS:
                    if container_format not in {"JPEG", "MPO"}:
                        _validate_signature(container_format, suffix)
                    variant = SOURCE_VARIANT_SINGLE_FRAME
                    frame_count = int(getattr(raw, "n_frames", 1))
                    if container_format == "MPO" or raw.info.get("mp"):
                        variant, frame_count = _classify_mpf(path, raw.info.get("xmp"))
                    requires_normalization = variant == SOURCE_VARIANT_HDR_GAIN_MAP
                    source_format = "JPEG"
                else:
                    _validate_signature(container_format, suffix)
                    source_format = container_format
                    variant = SOURCE_VARIANT_SINGLE_FRAME
                    frame_count = int(getattr(raw, "n_frames", 1))
                    requires_normalization = False
                    if frame_count > 1:
                        raise _multi_frame_error(source_format)
                raw.verify()
            with Image.open(path) as raw:
                if hasattr(raw, "seek"):
                    raw.seek(0)
                oriented = ImageOps.exif_transpose(raw)
                width, height = oriented.size
        except SourceImageError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise SourceImageError(
                "Prisma could not read this image. Export a fresh JPEG or PNG and try again."
            ) from exc
        return SourceInspection(
            container_format=container_format,
            source_format=source_format,
            variant=variant,
            width=width,
            height=height,
            frame_count=frame_count,
            requires_normalization=requires_normalization,
        )

    def _decode_normalized(
        self,
        source: Path,
        *,
        expected_suffix: str | None = None,
    ) -> tuple[Image.Image, str]:
        suffix = (expected_suffix or source.suffix).lower()
        if suffix in {".heic", ".heif", ".hif"} and not self.heif_available:
            raise SourceImageError("HEIC/HEIF support is unavailable in this Prisma build.")
        try:
            with Image.open(source) as raw:
                source_format = str(raw.format or "").upper()
                _validate_signature(source_format, suffix)
                if int(getattr(raw, "n_frames", 1)) > 1:
                    raise _multi_frame_error(source_format)
                normalized = normalize_to_srgb(raw)
        except SourceImageError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            format_name = suffix.lstrip(".").upper() or "source"
            raise SourceImageError(
                f"Cannot decode {format_name} image. Export it as an SDR sRGB JPEG or PNG and try again."
            ) from exc
        return normalized, source_format

    @staticmethod
    def _decode_gain_map_primary(source: Path) -> Image.Image:
        try:
            with Image.open(source) as raw:
                if hasattr(raw, "seek"):
                    raw.seek(0)
                return normalize_to_srgb(raw)
        except SourceImageError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise SourceImageError(
                "Prisma could not read the primary JPEG image. Export a fresh JPEG or PNG "
                "and try again."
            ) from exc

    def publish_staged(self, staged: Path, destination: Path) -> ResolvedSource:
        """Validate private staging, then atomically publish the untouched original."""

        staged = Path(staged)
        destination = Path(destination).resolve()
        if not destination.is_relative_to(self.images_dir.resolve()):
            raise SourceImageError("Image destination is outside the Images folder")
        suffix = destination.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise SourceImageError(f"Unsupported image format: {suffix or '(none)'}")
        before = self._stat_identity(staged)
        digest = _sha256_file(staged)
        if suffix in NATIVE_EXTENSIONS:
            inspection = self._inspect_native(
                staged,
                expected_suffix=suffix,
            )
            if inspection.requires_normalization:
                image = self._decode_gain_map_primary(staged)
                cached = None
                width = height = 0
            else:
                image = None
                cached = None
                width, height = inspection.width, inspection.height
            if before != self._stat_identity(staged):
                if image is not None:
                    image.close()
                raise SourceImageError("The image changed while Prisma was preparing it; retry.")
            if image is not None:
                cached, width, height = self._store_normalized_image(image, digest=digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
            with self._lock:
                if cached is not None:
                    self._record_normalized_entry(
                        destination,
                        cache_name=cached.name,
                        digest=digest,
                        width=width,
                        height=height,
                        source_format=inspection.source_format,
                        source_variant=inspection.variant,
                    )
                    self._prune_locked(protect_cache_name=cached.name)
                    entry = self._manifest["entries"][destination.name]
                    resolved = self._resolved_normalized(destination, entry)
                else:
                    self._record_native_entry(
                        destination,
                        digest=digest,
                        width=width,
                        height=height,
                        source_format=inspection.source_format,
                        source_variant=inspection.variant,
                    )
                    self._prune_locked()
                    entry = self._manifest["native_entries"][destination.name]
                    resolved = self._resolved_native(destination, entry)
                self._write_manifest()
            return resolved

        image, source_format = self._decode_normalized(
            staged,
            expected_suffix=suffix,
        )
        if before != self._stat_identity(staged):
            image.close()
            raise SourceImageError("The image changed while Prisma was preparing it; retry.")
        cached, width, height = self._store_normalized_image(image, digest=digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)
        with self._lock:
            self._record_normalized_entry(
                destination,
                cache_name=cached.name,
                digest=digest,
                width=width,
                height=height,
                source_format=source_format,
                source_variant=SOURCE_VARIANT_SINGLE_FRAME,
            )
            self._prune_locked(protect_cache_name=cached.name)
            self._write_manifest()
            return self._resolved_normalized(
                destination,
                self._manifest["entries"][destination.name],
            )

    def prepare(self, path_or_name: str | Path) -> ResolvedSource:
        path = (
            self._safe_source(str(path_or_name))
            if not isinstance(path_or_name, Path) or not path_or_name.is_absolute()
            else path_or_name.resolve()
        )
        if not path.is_relative_to(self.images_dir.resolve()):
            raise SourceImageError("Image path is outside the Images folder")
        if not path.is_file():
            raise SourceImageError(f"Image not found: {path.name}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise SourceImageError(f"Unsupported image format: {suffix or '(none)'}")
        with self._lock:
            entry = self._entry_for(path)
            if entry is not None:
                entry["last_used_ns"] = time.time_ns()
                self._write_manifest()
                return self._resolved_normalized(path, entry)
            if suffix in NATIVE_EXTENSIONS:
                entry = self._native_entry_for(path)
                if entry is not None:
                    entry["last_used_ns"] = time.time_ns()
                    return self._resolved_native(path, entry)

            before = self._stat_identity(path)
            if suffix in NATIVE_EXTENSIONS:
                inspection = self._inspect_native(path)
                image = (
                    self._decode_gain_map_primary(path)
                    if inspection.requires_normalization
                    else None
                )
                source_format = inspection.source_format
                source_variant = inspection.variant
            else:
                image, source_format = self._decode_normalized(path)
                source_variant = SOURCE_VARIANT_SINGLE_FRAME
            digest = _sha256_file(path)
            if before != self._stat_identity(path):
                if image is not None:
                    image.close()
                raise SourceImageError("The image changed while Prisma was preparing it; retry.")
            if image is None:
                self._record_native_entry(
                    path,
                    digest=digest,
                    width=inspection.width,
                    height=inspection.height,
                    source_format=source_format,
                    source_variant=source_variant,
                )
                self._prune_locked()
                self._write_manifest()
                return self._resolved_native(
                    path,
                    self._manifest["native_entries"][path.name],
                )
            cached, width, height = self._store_normalized_image(image, digest=digest)
            self._record_normalized_entry(
                path,
                cache_name=cached.name,
                digest=digest,
                width=width,
                height=height,
                source_format=source_format,
                source_variant=source_variant,
            )
            self._prune_locked(protect_cache_name=cached.name)
            self._write_manifest()
            return self._resolved_normalized(path, self._manifest["entries"][path.name])

    def resolve(self, filename: str, *, prepare: bool = True) -> ResolvedSource:
        path = self._safe_source(filename)
        if prepare:
            return self.prepare(path)
        if not path.is_file():
            raise SourceImageError(f"Image not found: {filename}")
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise SourceImageError(f"Unsupported image format: {suffix or '(none)'}")
        with self._lock:
            entry = self._entry_for(path)
            if entry is not None:
                return self._resolved_normalized(path, entry)
            if suffix in NATIVE_EXTENSIONS:
                entry = self._native_entry_for(path)
                if entry is not None:
                    return self._resolved_native(path, entry)
                inspection = self._inspect_native(path)
                if inspection.requires_normalization:
                    raise SourceImageError("Image is still being prepared")
                stat = path.stat()
                fingerprint = f"native:{stat.st_size}:{stat.st_mtime_ns}"
                return ResolvedSource(
                    path,
                    path,
                    path.name,
                    inspection.source_format,
                    fingerprint,
                    False,
                    inspection.width,
                    inspection.height,
                    inspection.variant,
                )
            raise SourceImageError("Image is still being prepared")

    def discover(self) -> tuple[list[Path], list[Path]]:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        ready: list[Path] = []
        pending: list[Path] = []
        for path in sorted(self.images_dir.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            (ready if self.is_ready(path) else pending).append(path)
        return ready, pending

    def discover_unsupported(self) -> list[Path]:
        if not self.images_dir.exists():
            return []
        return [
            path
            for path in sorted(self.images_dir.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and path.suffix.lower() in KNOWN_UNSUPPORTED_IMAGE_EXTENSIONS
        ]

    def prune_orphans(self) -> None:
        with self._lock:
            existing_supported = {
                path.name
                for path in self.images_dir.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
            } if self.images_dir.exists() else set()
            changed = False
            for name in list(self._manifest["entries"]):
                if name not in existing_supported:
                    self._manifest["entries"].pop(name, None)
                    changed = True
            for name in list(self._manifest["native_entries"]):
                if name not in existing_supported:
                    self._manifest["native_entries"].pop(name, None)
                    changed = True
            for name in set(self._manifest["entries"]) & set(self._manifest["native_entries"]):
                normalized = self._manifest["entries"].get(name)
                if (
                    isinstance(normalized, dict)
                    and normalized.get("source_variant") == SOURCE_VARIANT_HDR_GAIN_MAP
                ):
                    self._manifest["native_entries"].pop(name, None)
                else:
                    self._manifest["entries"].pop(name, None)
                changed = True
            changed = self._prune_locked() or changed
            if changed:
                self._write_manifest()

    def _prune_locked(self, *, protect_cache_name: str | None = None) -> bool:
        changed = False
        referenced = {
            str(entry.get("cache_name"))
            for entry in self._manifest["entries"].values()
            if isinstance(entry, dict)
        }
        for path in self.cache_dir.glob("*.png"):
            if path.name not in referenced:
                path.unlink(missing_ok=True)
        if self.cache_limit_bytes <= 0:
            return changed
        entries = list(self._manifest["entries"].items())
        total = sum(
            (self.cache_dir / str(entry.get("cache_name"))).stat().st_size
            for _, entry in entries
            if (self.cache_dir / str(entry.get("cache_name"))).is_file()
        )
        for name, entry in sorted(entries, key=lambda pair: int(pair[1].get("last_used_ns", 0))):
            if total <= self.cache_limit_bytes:
                break
            cached = self.cache_dir / str(entry.get("cache_name"))
            if cached.name == protect_cache_name:
                continue
            try:
                size = cached.stat().st_size
            except OSError:
                size = 0
            cached.unlink(missing_ok=True)
            self._manifest["entries"].pop(name, None)
            total -= size
            changed = True
        return changed

    def clear(self) -> int:
        with self._lock:
            removed = 0
            if self.cache_dir.exists():
                for path in self.cache_dir.iterdir():
                    if path.is_file():
                        path.unlink(missing_ok=True)
                        removed += 1
                    elif path.is_dir():
                        shutil.rmtree(path)
                        removed += 1
            self._manifest = {"schema_version": 1, "entries": {}, "native_entries": {}}
            return removed


class ImageImportManager:
    """One-worker import queue with bounded, pollable batch history."""

    def __init__(
        self,
        service: SourceImageService,
        staging_dir: Path,
        *,
        priority_busy: Callable[[], bool] | None = None,
    ):
        self.service = service
        self.staging_dir = Path(staging_dir)
        self.priority_busy = priority_busy or (lambda: False)
        self._queue: queue.Queue[tuple[str, dict] | None] = queue.Queue()
        self._batches: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.RLock()
        self._mutation_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="prisma-image-import", daemon=True)
        self._thread.start()

    def _new_batch(self, origin: str, items: list[dict]) -> dict:
        batch_id = uuid.uuid4().hex
        batch = {
            "batch_id": batch_id,
            "origin": origin,
            "status": "queued" if items else "complete",
            "total": len(items),
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "current_filename": None,
            "items": items,
        }
        with self._lock:
            self._batches[batch_id] = batch
            while len(self._batches) > MAX_JOB_HISTORY:
                self._batches.popitem(last=False)
        for item in items:
            self._queue.put((batch_id, item))
        return self.status(batch_id)

    def refresh(self) -> dict:
        self.service.prune_orphans()
        _, pending = self.service.discover()
        pending.extend(self.service.discover_unsupported())
        with self._lock:
            for batch in reversed(self._batches.values()):
                if batch["origin"] == "scan" and batch["status"] in {"queued", "running"}:
                    return json.loads(json.dumps(batch))
        items = [
            {"requested_name": path.name, "stored_name": path.name, "status": "queued", "error": None}
            for path in pending
        ]
        return self._new_batch("scan", items)

    def import_staged(self, staged: Iterable[tuple[str, Path]]) -> dict:
        items = [
            {
                "requested_name": Path(name).name,
                "stored_name": None,
                "staging_path": str(path),
                "status": "queued",
                "error": None,
            }
            for name, path in staged
        ]
        return self._new_batch("upload", items)

    def import_staged_sync(self, requested_name: str, staged: Path) -> ResolvedSource:
        item = {
            "requested_name": Path(requested_name).name,
            "stored_name": None,
            "staging_path": str(staged),
            "status": "queued",
            "error": None,
        }
        self._process_item(item)
        if item["status"] != "complete":
            raise SourceImageError(str(item.get("error") or "Image import failed"))
        return self.service.resolve(str(item["stored_name"]))

    def status(self, batch_id: str) -> dict:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            public = json.loads(json.dumps(batch))
        public["job_id"] = public["batch_id"]
        for item in public["items"]:
            item.pop("staging_path", None)
        return public

    def active(self) -> bool:
        with self._lock:
            return any(batch["status"] in {"queued", "running"} for batch in self._batches.values())

    def _reserve_destination(self, requested_name: str) -> Path:
        safe_name = Path(requested_name).name
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise SourceImageError(f"Unsupported image format: {suffix or '(none)'}")
        stem = Path(safe_name).stem
        destination = self.service.images_dir / safe_name
        counter = 1
        while destination.exists():
            destination = self.service.images_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        return destination

    def _process_item(self, item: dict) -> None:
        with self._mutation_lock:
            requested_name = str(item["requested_name"])
            staging_text = item.get("staging_path")
            destination: Path | None = None
            try:
                if staging_text:
                    staged = Path(staging_text)
                    destination = self._reserve_destination(requested_name)
                    resolved = self.service.publish_staged(staged, destination)
                else:
                    destination = self.service._safe_source(requested_name)
                    resolved = self.service.prepare(destination)
                item["stored_name"] = destination.name
                item["width"] = resolved.width
                item["height"] = resolved.height
                item["status"] = "complete"
            except (SourceImageError, OSError) as exc:
                if staging_text and destination is not None:
                    destination.unlink(missing_ok=True)
                item["status"] = "failed"
                item["error"] = str(exc)
            finally:
                if staging_text:
                    Path(staging_text).unlink(missing_ok=True)

    def _finish_batch_if_terminal(self, batch: dict) -> None:
        if batch["completed"] < batch["total"]:
            return
        if batch["failed"] == 0:
            batch["status"] = "complete"
        elif batch["succeeded"] == 0:
            batch["status"] = "failed"
        else:
            batch["status"] = "partial"
        batch["current_filename"] = None

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                queued = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if queued is None:
                break
            batch_id, item = queued
            while self.priority_busy() and not self._stop.wait(0.2):
                pass
            if self._stop.is_set():
                break
            with self._lock:
                batch = self._batches.get(batch_id)
                if batch is None:
                    continue
                batch["status"] = "running"
                batch["current_filename"] = item["requested_name"]
                item["status"] = "running"
            self._process_item(item)
            with self._lock:
                batch = self._batches.get(batch_id)
                if batch is None:
                    continue
                batch["completed"] += 1
                if item["status"] == "complete":
                    batch["succeeded"] += 1
                else:
                    batch["failed"] += 1
                self._finish_batch_if_terminal(batch)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        self._thread.join(timeout=2)
        if self.staging_dir.exists():
            for path in self.staging_dir.glob(".upload-*"):
                path.unlink(missing_ok=True)
