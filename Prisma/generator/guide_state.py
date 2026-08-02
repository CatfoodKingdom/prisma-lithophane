"""Durable, workspace-owned state for Generator first-launch onboarding."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


GUIDE_STATE_SCHEMA_VERSION = 2
WELCOME_STATUSES = frozenset({"not_offered", "deferred", "declined", "accepted"})


class GuideStateError(ValueError):
    """Raised when guide state cannot be normalized safely."""


class GuideStateRevisionConflict(GuideStateError):
    """Raised when a client attempts to replace a stale state revision."""

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__(
            f"Guide state changed in another browser tab "
            f"(expected revision {expected}, current revision {actual})."
        )
        self.expected = expected
        self.actual = actual


def default_guide_state() -> dict[str, Any]:
    """Return a new canonical first-launch state."""
    return {
        "schema_version": GUIDE_STATE_SCHEMA_VERSION,
        "revision": 0,
        "welcome_status": "not_offered",
    }


def normalize_guide_state(
    value: Any,
    *,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Validate and normalize current or legacy guide state."""
    if not isinstance(value, Mapping):
        raise GuideStateError("guide state must be an object")

    raw_schema = value.get("schema_version", 0)
    if not isinstance(raw_schema, int) or isinstance(raw_schema, bool):
        raise GuideStateError("schema_version must be an integer")
    schema_version = raw_schema
    if schema_version not in (0, 1, GUIDE_STATE_SCHEMA_VERSION):
        raise GuideStateError(f"unsupported guide state schema version: {schema_version}")
    if require_complete:
        required_fields = {
            "schema_version",
            "revision",
            "welcome_status",
        }
        if schema_version != GUIDE_STATE_SCHEMA_VERSION or set(value) != required_fields:
            raise GuideStateError(
                "replacement guide state must contain exactly the canonical schema fields"
            )

    raw_revision = value.get("revision", 0)
    if not isinstance(raw_revision, int) or isinstance(raw_revision, bool):
        raise GuideStateError("revision must be a non-negative integer")
    revision = raw_revision
    if revision < 0:
        raise GuideStateError("revision must be a non-negative integer")

    welcome_status = str(value.get("welcome_status") or "").strip()
    if schema_version == 0 and not welcome_status:
        if value.get("welcome_offered") is True:
            welcome_status = "declined"
        else:
            welcome_status = "not_offered"
    if welcome_status not in WELCOME_STATUSES:
        raise GuideStateError(
            f"welcome_status must be one of: {', '.join(sorted(WELCOME_STATUSES))}"
        )

    return {
        "schema_version": GUIDE_STATE_SCHEMA_VERSION,
        "revision": revision,
        "welcome_status": welcome_status,
    }


class GuideStateStore:
    """Read and atomically replace one workspace first-launch state record."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _corrupt_destination(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")

    def _read_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_guide_state()
        try:
            serialized = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GuideStateError(f"guide state could not be read: {exc}") from exc
        try:
            raw = json.loads(serialized)
            return normalize_guide_state(raw)
        except (json.JSONDecodeError, GuideStateError, TypeError, ValueError):
            destination = self._corrupt_destination()
            try:
                os.replace(self.path, destination)
            except OSError as exc:
                raise GuideStateError(
                    f"guide state is invalid and could not be preserved: {exc}"
                ) from exc
            return default_guide_state()

    def read(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._read_locked())

    def replace(
        self,
        value: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._lock:
            if (
                not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool)
                or expected_revision < 0
            ):
                raise GuideStateError("expected_revision must be a non-negative integer")
            current = self._read_locked()
            actual_revision = int(current["revision"])
            if expected_revision != actual_revision:
                raise GuideStateRevisionConflict(
                    expected=expected_revision,
                    actual=actual_revision,
                )

            normalized = normalize_guide_state(value, require_complete=True)
            if int(normalized["revision"]) != expected_revision:
                raise GuideStateError(
                    "guide state revision must match expected_revision"
                )
            normalized["revision"] = actual_revision + 1
            normalized["schema_version"] = GUIDE_STATE_SCHEMA_VERSION

            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                    delete=False,
                ) as stream:
                    temporary_path = Path(stream.name)
                    json.dump(normalized, stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path is not None and temporary_path.exists():
                    try:
                        temporary_path.unlink()
                    except OSError:
                        pass
            return deepcopy(normalized)
