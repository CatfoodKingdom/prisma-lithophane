"""Pure pack/validate/read for a Save-Run archive (no FastAPI, no disk, no server state).

Archive layout (zip):
  run.json            required  header + config + palette + image_domain dims + stats + result
  thickness_maps.npz  required  np.savez_compressed of tm__<key> / dbg__<key> float arrays
  image/<file>        required  exactly one source-image member
  solve_state.json    optional  profiles + solve_owned_fingerprint (review-tier)
  run_cache/<relpath> optional  the whole per-card run-cache subtree (png/bin/json/csv +
                                post_solve_export_bundle/…) for instant review; nesting allowed

Untrusted-input posture: read_run_archive NEVER calls extractall, validates every
member name against a closed allow-list (each nested run_cache/ relpath too), refuses
traversal / absolute / drive-letter / symlink / "."/".." members, rejects duplicate
member names, and enforces member-count + total-uncompressed-byte ceilings. pack_run_archive
enforces the SAME count/byte ceilings so Save can't produce an archive the loader rejects.
"""
from __future__ import annotations

import io
import json
import posixpath
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

SCHEMA_VERSION = 1
ACCEPTED_SCHEMA_VERSIONS = frozenset({1})

RUN_JSON = "run.json"
THICKNESS_NPZ = "thickness_maps.npz"
SOLVE_STATE_JSON = "solve_state.json"
IMAGE_PREFIX = "image/"
RUN_CACHE_PREFIX = "run_cache/"

MAX_MEMBERS = 4096
MAX_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024  # 512 MB ceiling (zip-bomb guard)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024              # compressed-upload cap (checked before full read)


class ArchiveError(ValueError):
    """Raised when an archive is malformed, unsafe, or an unsupported version."""


@dataclass
class ParsedArchive:
    run_json: dict
    thickness_arrays: dict
    image_name: str
    image_bytes: bytes
    solve_state: Optional[dict] = None
    run_cache_files: dict = field(default_factory=dict)  # relpath under run_cache/ -> bytes


def _is_safe_relpath(rel: str) -> bool:
    """True for a non-traversing, relative, forward-slash FILE path (nesting allowed).

    Rejects empty, absolute, drive-letter, backslash, traversal, and the directory
    sentinels "." / ".." (a "." member would resolve to the dir itself and crash a write).
    """
    if not rel or rel.startswith("/") or rel.startswith("\\") or ":" in rel or "\\" in rel:
        return False
    norm = posixpath.normpath(rel)
    if norm in (".", "..") or norm != rel or norm.startswith("../"):
        return False
    return True


def _member_is_safe(name: str) -> bool:
    """True only for allow-listed member names (run_cache/ may nest; nothing may traverse)."""
    if not name or name.startswith("/") or name.startswith("\\") or ":" in name:
        return False
    if name in (RUN_JSON, THICKNESS_NPZ, SOLVE_STATE_JSON):
        return True
    if name.startswith(IMAGE_PREFIX):
        leaf = name[len(IMAGE_PREFIX):]
        return leaf != "" and "/" not in leaf and _is_safe_relpath(leaf)
    if name.startswith(RUN_CACHE_PREFIX):
        rel = name[len(RUN_CACHE_PREFIX):]
        return rel != "" and _is_safe_relpath(rel)
    return False


def _zipinfo_is_symlink(zi: zipfile.ZipInfo) -> bool:
    """True if the entry's external attrs mark it a symlink/non-regular (unix mode high bits).

    Only the S_IF* TYPE field decides. Windows `writestr(str, ...)` sets permission bits
    with NO type field (ftype == 0) — those are regular members and must pass; only a
    SET, non-regular type (e.g. S_IFLNK 0o120000) is rejected.
    """
    ftype = (zi.external_attr >> 16) & 0o170000
    if ftype and ftype != 0o100000:   # type field present AND not a regular file
        return True
    return False


def pack_run_archive(*, run_json: dict, thickness_arrays: dict, image_bytes: bytes,
                     image_name: str, solve_state: Optional[dict] = None,
                     run_cache_files: Optional[dict] = None) -> bytes:
    """Build the archive zip bytes from already-extracted pieces (pure).

    image_bytes is REQUIRED and must be non-empty (the source image is a required
    archive member — caller fails loud before reaching here if it's missing).
    run_cache_files maps a (possibly nested) relpath -> bytes, copied verbatim
    under run_cache/.
    """
    if not image_name or "/" in image_name or "\\" in image_name:
        raise ArchiveError(f"bad image_name: {image_name!r}")
    if not image_bytes:
        raise ArchiveError("image_bytes is empty; source image is a required member")
    npz_buf = io.BytesIO()
    np.savez_compressed(npz_buf, **{k: np.asarray(v) for k, v in thickness_arrays.items()})
    npz_bytes = npz_buf.getvalue()
    # Serialize the JSON members to the EXACT bytes that will be written, so the byte
    # total counted here matches the sum of ZipInfo.file_size the reader later checks
    # (writestr's uncompressed size == len of these byte payloads).
    run_json_bytes = json.dumps(run_json, default=str).encode("utf-8")
    solve_state_bytes = (json.dumps(solve_state, default=str).encode("utf-8")
                         if solve_state is not None else None)
    # Enforce the SAME ceilings the loader checks, so Save never produces an archive
    # the loader would reject (the run_cache subtree can be large).
    rc = run_cache_files or {}
    member_count = 3 + (1 if solve_state_bytes is not None else 0) + len(rc)  # run.json+npz+image+...
    if member_count > MAX_MEMBERS:
        raise ArchiveError(f"too many members: {member_count} > {MAX_MEMBERS}")
    total_uncompressed = (
        len(run_json_bytes) + len(npz_bytes) + len(image_bytes)
        + (len(solve_state_bytes) if solve_state_bytes is not None else 0)
        + sum(len(v) for v in rc.values())
    )
    if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ArchiveError(f"archive uncompressed size {total_uncompressed} exceeds ceiling")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(RUN_JSON, run_json_bytes)
        zf.writestr(THICKNESS_NPZ, npz_bytes)
        zf.writestr(f"{IMAGE_PREFIX}{image_name}", image_bytes)
        if solve_state_bytes is not None:
            zf.writestr(SOLVE_STATE_JSON, solve_state_bytes)
        for rel, payload in (run_cache_files or {}).items():
            rel = rel.replace("\\", "/")
            if not _is_safe_relpath(rel):
                raise ArchiveError(f"unsafe run_cache relpath: {rel!r}")
            zf.writestr(f"{RUN_CACHE_PREFIX}{rel}", payload)
    return buf.getvalue()


def read_run_archive(data) -> ParsedArchive:
    """Validate + parse archive bytes (or a path) into a ParsedArchive. Safe on untrusted zips."""
    raw = data.read_bytes() if hasattr(data, "read_bytes") else data
    bio = io.BytesIO(raw) if isinstance(raw, (bytes, bytearray)) else raw
    if not zipfile.is_zipfile(bio):
        raise ArchiveError("not a zip archive")
    bio.seek(0)
    with zipfile.ZipFile(bio) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError(f"too many members: {len(infos)} > {MAX_MEMBERS}")
        names = [zi.filename for zi in infos]
        if len(set(names)) != len(names):
            raise ArchiveError("duplicate member names in archive")
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ArchiveError(f"uncompressed size {total} exceeds ceiling")
        for zi in infos:
            if not _member_is_safe(zi.filename):
                raise ArchiveError(f"unsafe/unknown member: {zi.filename!r}")
            if _zipinfo_is_symlink(zi):
                raise ArchiveError(f"non-regular member: {zi.filename!r}")
        nameset = set(names)
        for required in (RUN_JSON, THICKNESS_NPZ):
            if required not in nameset:
                raise ArchiveError(f"missing required member: {required}")
        run_json = json.loads(zf.read(RUN_JSON))
        if run_json.get("schema_version") not in ACCEPTED_SCHEMA_VERSIONS:
            raise ArchiveError(f"unsupported schema_version: {run_json.get('schema_version')!r}")
        with np.load(io.BytesIO(zf.read(THICKNESS_NPZ))) as npz:
            thickness_arrays = {k: npz[k] for k in npz.files}
        image_members = [n for n in nameset if n.startswith(IMAGE_PREFIX)]
        if len(image_members) != 1:
            raise ArchiveError(f"expected exactly one image member, found {len(image_members)}")
        image_name = image_members[0][len(IMAGE_PREFIX):]
        if not image_name:
            raise ArchiveError("image member has an empty filename")
        image_bytes = zf.read(image_members[0])
        if not image_bytes:
            raise ArchiveError("archive image member is empty")
        solve_state = json.loads(zf.read(SOLVE_STATE_JSON)) if SOLVE_STATE_JSON in nameset else None
        run_cache_files = {n[len(RUN_CACHE_PREFIX):]: zf.read(n)
                           for n in nameset if n.startswith(RUN_CACHE_PREFIX)}
    return ParsedArchive(run_json=run_json, thickness_arrays=thickness_arrays,
                         image_name=image_name, image_bytes=image_bytes,
                         solve_state=solve_state, run_cache_files=run_cache_files)
