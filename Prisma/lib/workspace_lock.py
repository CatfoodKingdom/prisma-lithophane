"""Crash-recoverable single-writer locks for portable Prisma Workspaces."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO


class WorkspaceLockError(RuntimeError):
    """Raised when another process already owns a Workspace lock."""

    def __init__(self, message: str, *, owner_url: str | None = None) -> None:
        super().__init__(message)
        self.owner_url = owner_url


class WorkspaceLock:
    def __init__(self, workspace: str | Path, *, owner: str) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.owner = str(owner)
        self.path = self.workspace / f".prisma-{self.owner}.lock"
        self._stream: BinaryIO | None = None
        self._metadata: dict = {}

    def _try_lock(self, stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - Windows is the release target
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, stream: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - Windows is the release target
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _existing_owner(stream: BinaryIO) -> dict:
        try:
            stream.seek(1)
            payload = json.loads(stream.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
        return {}

    def _write_metadata(self) -> None:
        stream = self._stream
        if stream is None:
            raise WorkspaceLockError("Workspace lock is not held")
        stream.seek(1)
        stream.truncate()
        stream.write(json.dumps(self._metadata, sort_keys=True).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())

    def acquire(self) -> "WorkspaceLock":
        if self._stream is not None:
            return self
        self.workspace.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                stream.write(b" ")
                stream.flush()
            try:
                self._try_lock(stream)
            except OSError as exc:
                metadata = self._existing_owner(stream)
                pid = metadata.get("pid")
                started = metadata.get("started_at")
                owner = f" (process {pid}, started {started})" if pid else ""
                owner_url = str(metadata.get("url") or "").strip() or None
                raise WorkspaceLockError(
                    f"The Prisma {self.owner.title()} Workspace is already in use{owner}. "
                    "Use the already running window or close it before starting another copy.",
                    owner_url=owner_url,
                ) from exc
            self._stream = stream
            self._metadata = {
                "owner": self.owner,
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_metadata()
            return self
        except Exception:
            self._stream = None
            stream.close()
            raise

    def update_metadata(self, **values: object) -> None:
        """Atomically refresh discoverable owner details while holding the lock."""

        self._metadata.update(values)
        self._write_metadata()

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        self._metadata = {}
        try:
            self._unlock(stream)
        finally:
            stream.close()

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, *_args) -> None:
        self.release()
