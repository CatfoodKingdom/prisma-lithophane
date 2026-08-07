"""Durable, crash-recoverable runtime record for destructive teaching guides."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


GUIDE_RUNTIME_SCHEMA_VERSION = 1
GUIDE_RUNTIME_PHASES = frozenset({"active", "restoring"})
GUIDE_RESOURCE_STATUSES = frozenset({
    "pending_create", "present", "pending_delete", "absent",
})
GUIDE_RESOURCE_TRANSITIONS = frozenset({
    ("absent", "pending_create"),
    ("pending_create", "present"),
    ("pending_create", "absent"),
    ("present", "pending_delete"),
    ("pending_delete", "absent"),
    ("pending_delete", "present"),
})


class GuideRuntimeError(ValueError):
    """Base guide-runtime persistence or lease error."""


class GuideRuntimeConflict(GuideRuntimeError):
    """Raised when another page owns the guide workspace."""


class GuideRuntimeCorrupt(GuideRuntimeError):
    """Raised when a pending recovery record cannot be trusted."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class GuideRuntimeStore:
    """Own one durable session plus an in-process provisional lease and epoch."""

    def __init__(self, path: Path, *, lease_timeout_s: float = 90.0) -> None:
        self.path = Path(path)
        self.state_path = self.path.with_name(f"{self.path.stem}_state.json")
        self.lease_timeout_s = float(lease_timeout_s)
        self._lock = threading.RLock()
        self._mutation_condition = threading.Condition(self._lock)
        self._active_mutations = 0
        self._lease: dict[str, Any] | None = None
        self._pages: dict[str, float] = {}
        self._state_error: str | None = None
        try:
            self._workspace_epoch = self._read_epoch()
        except GuideRuntimeCorrupt as exc:
            self._workspace_epoch = 0
            self._state_error = str(exc)

    def _read_epoch(self) -> int:
        if not self.state_path.exists():
            return 0
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            epoch = raw.get("workspace_epoch")
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise GuideRuntimeCorrupt(f"guide runtime state could not be read: {exc}") from exc
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise GuideRuntimeCorrupt("guide runtime state has an invalid workspace epoch")
        return epoch

    def _write_epoch_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                dir=self.state_path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump({"workspace_epoch": self._workspace_epoch}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _read_locked(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GuideRuntimeCorrupt(f"guide recovery record could not be read: {exc}") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != GUIDE_RUNTIME_SCHEMA_VERSION:
            raise GuideRuntimeCorrupt("guide recovery record has an unsupported schema")
        required = {"session_id", "guide_id", "route_id", "owner_page_id", "phase", "snapshot"}
        if not required.issubset(raw) or raw.get("phase") not in GUIDE_RUNTIME_PHASES:
            raise GuideRuntimeCorrupt("guide recovery record is incomplete")
        if not isinstance(raw.get("snapshot"), Mapping):
            raise GuideRuntimeCorrupt("guide recovery snapshot must be an object")
        snapshot = raw["snapshot"]
        if (
            set(snapshot) != {"server", "client"}
            or not isinstance(snapshot.get("server"), Mapping)
            or not isinstance(snapshot.get("client"), Mapping)
        ):
            raise GuideRuntimeCorrupt("guide recovery snapshot has invalid ownership sections")
        snapshot_sha256 = raw.get("snapshot_sha256")
        if (
            not isinstance(snapshot_sha256, str)
            or len(snapshot_sha256) != 64
            or snapshot_sha256 != _snapshot_digest(snapshot)
        ):
            raise GuideRuntimeCorrupt("guide recovery snapshot failed its integrity check")
        if not isinstance(raw.get("owned_jobs", {}), Mapping):
            raise GuideRuntimeCorrupt("guide recovery job ledger is invalid")
        resources = raw.get("owned_resources", {})
        if not isinstance(resources, Mapping):
            raise GuideRuntimeCorrupt("guide recovery resource ledger is invalid")
        for operation_id, entry in resources.items():
            if (
                not isinstance(operation_id, str)
                or not isinstance(entry, Mapping)
                or entry.get("operation_id") != operation_id
                or not isinstance(entry.get("kind"), str)
                or not isinstance(entry.get("name"), str)
                or entry.get("status") not in GUIDE_RESOURCE_STATUSES
                or (entry.get("id") is not None and not isinstance(entry.get("id"), str))
                or "fingerprint" not in entry
            ):
                raise GuideRuntimeCorrupt("guide recovery resource ledger has an invalid entry")
        return deepcopy(dict(raw))

    def read(self) -> dict[str, Any] | None:
        with self._lock:
            return self._read_locked()

    def _write_locked(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _lease_alive_locked(self) -> bool:
        return bool(
            self._lease
            and time.monotonic() - float(self._lease["heartbeat_monotonic"]) <= self.lease_timeout_s
        )

    def note_page(self, page_id: str | None) -> None:
        page = str(page_id or "").strip()
        if not page:
            return
        with self._lock:
            now = time.monotonic()
            self._pages[page] = now
            self._pages = {
                current: heartbeat
                for current, heartbeat in self._pages.items()
                if now - heartbeat <= self.lease_timeout_s
            }

    def depart_page(self, page_id: str | None) -> dict[str, Any]:
        """Relinquish ephemeral ownership while preserving durable recovery data."""
        page = str(page_id or "").strip()
        if not page:
            raise GuideRuntimeError("page_id is required")
        with self._lock:
            self._pages.pop(page, None)
            if self._lease and self._lease.get("page_id") == page:
                self._lease = None
            return self.status(page_id=page)

    @staticmethod
    def _assert_owner(record: Mapping[str, Any], session_id: str, page_id: str) -> None:
        if record.get("session_id") != session_id:
            raise GuideRuntimeConflict("guide session is no longer active")
        if record.get("owner_page_id") != page_id:
            raise GuideRuntimeConflict("another Prisma window owns the guide session")

    def acquire(self, page_id: str) -> dict[str, Any]:
        owner = str(page_id or "").strip()
        if not owner:
            raise GuideRuntimeError("page_id is required")
        with self._lock:
            if self._state_error:
                raise GuideRuntimeCorrupt(self._state_error)
            if self._read_locked() is not None:
                raise GuideRuntimeConflict("an interrupted guide must be recovered first")
            if self._lease_alive_locked() and self._lease:
                if self._lease["page_id"] != owner:
                    raise GuideRuntimeConflict("another Prisma window owns the guide workspace")
                # Repeated launch gestures from one page share the same provisional
                # lease. Replacing its ID would make the first start transaction fail
                # after it had already inspected the workspace and asked for consent.
                self._lease["heartbeat_monotonic"] = time.monotonic()
                return self.status(page_id=owner)
            self._lease = {
                "lease_id": uuid.uuid4().hex,
                "page_id": owner,
                "heartbeat_monotonic": time.monotonic(),
            }
            return self.status(page_id=owner)

    def release(self, lease_id: str, page_id: str) -> dict[str, Any]:
        with self._lock:
            if self._lease and (
                self._lease["lease_id"] != lease_id or self._lease["page_id"] != page_id
            ):
                raise GuideRuntimeConflict("guide lease ownership changed")
            if self._read_locked() is not None:
                raise GuideRuntimeConflict("a durable guide session cannot be released without recovery")
            self._lease = None
            return self.status(page_id=page_id)

    def begin(
        self,
        *,
        lease_id: str,
        page_id: str,
        guide_id: str,
        route_id: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            if not self._lease_alive_locked() or not self._lease:
                raise GuideRuntimeConflict("guide lease expired")
            if self._lease["lease_id"] != lease_id or self._lease["page_id"] != page_id:
                raise GuideRuntimeConflict("guide lease ownership changed")
            if self._read_locked() is not None:
                raise GuideRuntimeConflict("a guide recovery record already exists")
            durable_snapshot = deepcopy(dict(snapshot))
            record = {
                "schema_version": GUIDE_RUNTIME_SCHEMA_VERSION,
                "session_id": uuid.uuid4().hex,
                "guide_id": str(guide_id),
                "route_id": str(route_id),
                "owner_page_id": str(page_id),
                "phase": "active",
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "snapshot": durable_snapshot,
                "snapshot_sha256": _snapshot_digest(durable_snapshot),
                "owned_jobs": {},
                "owned_resources": {},
            }
            self._write_locked(record)
            return deepcopy(record)

    def heartbeat(self, session_id: str, page_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read_locked()
            if record is None or record["session_id"] != session_id:
                raise GuideRuntimeConflict("guide session is no longer active")
            if record["owner_page_id"] != page_id:
                raise GuideRuntimeConflict("another Prisma window owns the guide session")
            if self._lease:
                self._lease["heartbeat_monotonic"] = time.monotonic()
            return self.status(page_id=page_id)

    def claim_recovery(self, session_id: str, page_id: str) -> dict[str, Any]:
        """Transfer an interrupted session to a new page; guides are never resumed."""
        with self._lock:
            record = self._read_locked()
            if record is None or record["session_id"] != session_id:
                raise GuideRuntimeConflict("guide session is no longer active")
            if self._lease_alive_locked() and self._lease and self._lease["page_id"] != page_id:
                raise GuideRuntimeConflict("the guide owner is still active in another window")
            self._lease = {
                "lease_id": uuid.uuid4().hex,
                "page_id": page_id,
                "heartbeat_monotonic": time.monotonic(),
            }
            record["owner_page_id"] = page_id
            record["phase"] = "restoring"
            record["updated_at"] = _utc_now()
            self._write_locked(record)
            return deepcopy(record)

    def mark_restoring(self, session_id: str, page_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read_locked()
            if record is None:
                raise GuideRuntimeConflict("guide session is no longer active")
            self._assert_owner(record, session_id, page_id)
            record["phase"] = "restoring"
            record.pop("restored_at", None)
            record["updated_at"] = _utc_now()
            self._write_locked(record)
            return deepcopy(record)

    def mark_restored(self, session_id: str, page_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read_locked()
            if record is None:
                raise GuideRuntimeConflict("guide session is no longer active")
            self._assert_owner(record, session_id, page_id)
            if record.get("phase") != "restoring":
                raise GuideRuntimeConflict("guide restoration has not started")
            record["restored_at"] = _utc_now()
            record["updated_at"] = _utc_now()
            self._write_locked(record)
            return deepcopy(record)

    def register_job(self, session_id: str, page_id: str, kind: str, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read_locked()
            if record is None:
                raise GuideRuntimeConflict("guide session is no longer active")
            self._assert_owner(record, session_id, page_id)
            record.setdefault("owned_jobs", {})[str(kind)] = str(job_id)
            record["updated_at"] = _utc_now()
            self._write_locked(record)
            return deepcopy(record)

    def transition_resource(
        self,
        session_id: str,
        page_id: str,
        resource: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist one validated, idempotent owned-resource journal transition."""
        with self._lock:
            record = self._read_locked()
            if record is None:
                raise GuideRuntimeConflict("guide session is no longer active")
            self._assert_owner(record, session_id, page_id)
            operation_id = str(resource.get("operation_id") or "").strip()
            kind = str(resource.get("kind") or "").strip()
            name = str(resource.get("name") or "").strip()
            status = str(resource.get("status") or "").strip()
            resource_id = resource.get("id")
            if (
                not operation_id
                or not kind
                or not name
                or status not in GUIDE_RESOURCE_STATUSES
                or (resource_id is not None and not isinstance(resource_id, str))
                or "fingerprint" not in resource
            ):
                raise GuideRuntimeError("guide resource transition is invalid")
            resources = record.setdefault("owned_resources", {})
            existing = resources.get(operation_id)
            proposed = {
                "operation_id": operation_id,
                "kind": kind,
                "id": resource_id,
                "name": name,
                "fingerprint": deepcopy(resource["fingerprint"]),
                "status": status,
            }
            if existing is None:
                if status not in {"absent", "pending_create"}:
                    raise GuideRuntimeConflict("guide resource must begin absent or pending creation")
            else:
                for immutable in ("operation_id", "kind", "name", "fingerprint"):
                    if existing.get(immutable) != proposed.get(immutable):
                        raise GuideRuntimeConflict(f"guide resource {immutable} cannot change")
                if existing.get("id") is not None and existing.get("id") != resource_id:
                    raise GuideRuntimeConflict("guide resource id cannot change")
                previous_status = existing.get("status")
                if previous_status != status and (previous_status, status) not in GUIDE_RESOURCE_TRANSITIONS:
                    raise GuideRuntimeConflict(
                        f"invalid guide resource transition: {previous_status} to {status}"
                    )
                if previous_status == status and existing.get("id") != resource_id:
                    raise GuideRuntimeConflict("guide resource id can only be assigned on a state transition")
            resources[operation_id] = proposed
            record["updated_at"] = _utc_now()
            self._write_locked(record)
            return deepcopy(proposed)

    def replace_reconciled_resources(
        self,
        session_id: str,
        page_id: str,
        resources: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist server-authoritative reconciliation without relaxing public transitions."""
        with self._lock:
            record = self._read_locked()
            if record is None:
                raise GuideRuntimeConflict("guide session is no longer active")
            self._assert_owner(record, session_id, page_id)
            current = record.setdefault("owned_resources", {})
            if set(resources) != set(current):
                raise GuideRuntimeConflict("guide resource reconciliation changed ledger identity")
            for operation_id, proposed in resources.items():
                existing = current[operation_id]
                for immutable in ("operation_id", "kind", "name", "fingerprint"):
                    if existing.get(immutable) != proposed.get(immutable):
                        raise GuideRuntimeConflict("guide resource reconciliation changed immutable data")
                if proposed.get("status") not in {"present", "absent"}:
                    raise GuideRuntimeConflict("guide resource reconciliation is not authoritative")
            record["owned_resources"] = deepcopy(dict(resources))
            record["updated_at"] = _utc_now()
            self._write_locked(record)
            return deepcopy(record["owned_resources"])

    def finalize(self, session_id: str, page_id: str) -> int:
        with self._lock:
            record = self._read_locked()
            if record is None:
                raise GuideRuntimeConflict("guide session is no longer active")
            self._assert_owner(record, session_id, page_id)
            if not record.get("restored_at"):
                raise GuideRuntimeConflict("guide recovery has not completed")
            self.path.unlink(missing_ok=True)
            self._lease = None
            self._workspace_epoch += 1
            self._write_epoch_locked()
            return self._workspace_epoch

    def abandon(self, page_id: str | None = None) -> tuple[Path | None, int]:
        with self._lock:
            try:
                record = self._read_locked()
            except GuideRuntimeCorrupt:
                record = None
            if (
                record is not None
                and self._lease_alive_locked()
                and record.get("owner_page_id") != page_id
            ):
                raise GuideRuntimeConflict("the guide owner is still active in another window")
            destination: Path | None = None
            if self.path.exists():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                destination = self.path.with_name(
                    f"{self.path.stem}.abandoned-{stamp}{self.path.suffix}"
                )
                os.replace(self.path, destination)
            self._lease = None
            self._workspace_epoch += 1
            self._write_epoch_locked()
            self._state_error = None
            return destination, self._workspace_epoch

    def status(self, *, page_id: str | None = None, include_snapshot: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._state_error:
                raise GuideRuntimeCorrupt(self._state_error)
            record = self._read_locked()
            lease_alive = self._lease_alive_locked()
            result: dict[str, Any] = {
                "schema_version": GUIDE_RUNTIME_SCHEMA_VERSION,
                "workspace_epoch": self._workspace_epoch,
                "lease": None,
                "session": None,
                "other_active_windows": sum(
                    1
                    for current, heartbeat in self._pages.items()
                    if current != page_id and time.monotonic() - heartbeat <= self.lease_timeout_s
                ),
            }
            if lease_alive and self._lease:
                result["lease"] = {
                    "lease_id": self._lease["lease_id"] if self._lease["page_id"] == page_id else None,
                    "owned_by_page": self._lease["page_id"] == page_id,
                }
            if record is not None:
                session = {
                    key: deepcopy(value)
                    for key, value in record.items()
                    if key != "snapshot"
                }
                session["owned_by_page"] = record["owner_page_id"] == page_id
                if include_snapshot:
                    session["snapshot"] = deepcopy(record["snapshot"])
                result["session"] = session
            return result

    def authorize_mutation(
        self,
        *,
        page_id: str | None,
        session_id: str | None,
        workspace_epoch: int | None,
        allow_recovery: bool = False,
    ) -> tuple[bool, str | None]:
        with self._lock:
            return self._authorize_mutation_locked(
                page_id=page_id,
                session_id=session_id,
                workspace_epoch=workspace_epoch,
                allow_recovery=allow_recovery,
            )

    def _authorize_mutation_locked(
        self,
        *,
        page_id: str | None,
        session_id: str | None,
        workspace_epoch: int | None,
        allow_recovery: bool,
    ) -> tuple[bool, str | None]:
        """Authorize one ordinary mutation while ``self._lock`` is held."""
        if self._state_error:
            return False, self._state_error
        try:
            record = self._read_locked()
        except GuideRuntimeCorrupt as exc:
            return False, str(exc)
        if workspace_epoch is not None and workspace_epoch != self._workspace_epoch:
            return False, "This Prisma window is stale and must reload before changing the workspace"
        if record is not None:
            if session_id != record["session_id"] or page_id != record["owner_page_id"]:
                return False, "Another Prisma window is running or recovering a guide"
            if record.get("phase") != "active" and not allow_recovery:
                return False, "Prisma is restoring the workspace after a guide"
        elif self._lease_alive_locked() and self._lease and page_id != self._lease["page_id"]:
            return False, "Another Prisma window is preparing a guide"
        return True, None

    def begin_mutation(
        self,
        *,
        page_id: str | None,
        session_id: str | None,
        workspace_epoch: int | None,
        allow_recovery: bool = False,
    ) -> tuple[bool, str | None]:
        """Admit and count an ordinary request so recovery can drain it safely."""
        with self._lock:
            allowed, detail = self._authorize_mutation_locked(
                page_id=page_id,
                session_id=session_id,
                workspace_epoch=workspace_epoch,
                allow_recovery=allow_recovery,
            )
            if allowed:
                self._active_mutations += 1
            return allowed, detail

    def end_mutation(self) -> None:
        with self._mutation_condition:
            if self._active_mutations <= 0:
                raise GuideRuntimeError("guide mutation admission counter underflow")
            self._active_mutations -= 1
            if self._active_mutations == 0:
                self._mutation_condition.notify_all()

    def wait_for_mutations(self, *, timeout_s: float = 30.0) -> None:
        """Wait until requests admitted before recovery have left their endpoints."""
        deadline = time.monotonic() + float(timeout_s)
        with self._mutation_condition:
            while self._active_mutations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GuideRuntimeConflict(
                        "Prisma is still finishing a workspace change; retry recovery"
                    )
                self._mutation_condition.wait(timeout=remaining)
