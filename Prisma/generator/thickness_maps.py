"""Typed container for solver thickness maps (Task 5.1).

Replaces the magic-string ``Dict[str, np.ndarray]`` convention with a typed
``ThicknessMaps`` mapping plus a ``MapKey`` enum for the webapp/facade reserved
sentinel keys. This is a behavior-preserving internal typing aid: every key the
container stores is a plain ``str`` identical to the legacy string, so any
serializer, export bundle, or frontend payload built from a ``ThicknessMaps``
emits exactly the same keys/filenames as before.

Two kinds of key live in a thickness map:

* Reserved webapp/facade sentinels with stable string values -> ``MapKey``.
* Dynamic filament IDs (data values from config/palette/ordering) -> plain str.

The legacy CLI-only ``__filler__`` sentinel (written by ``pipeline_cli.py`` on
the opt-in ``--filler`` path) is intentionally NOT a ``MapKey`` member: it is
not part of the webapp/facade payload contract. ``ThicknessMaps`` accepts it as
an ordinary ``__``-prefixed string.

``__translucent_underfill__`` was retired by Task 2.3 and is deliberately absent
from ``MapKey``; it must never be reintroduced as a live key.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from enum import Enum
from typing import NewType, Optional

import numpy as np


FilamentId = NewType("FilamentId", str)
"""Type-checking alias for a dynamic filament-id key.

Runtime code treats filament IDs as ordinary strings; the real IDs are data
values pulled from config/palette/material ordering and cannot be enumerated.
"""


class MapKey(str, Enum):
    """Reserved webapp/facade ``thickness_maps`` sentinel keys.

    Members subclass ``str`` so legacy ``maps["__white_cap__"]`` and
    ``maps[MapKey.WHITE_CAP]`` resolve to the same slot. ``__filler__`` is
    deliberately excluded (legacy CLI-only, not part of the webapp/facade
    contract); ``__translucent_underfill__`` was retired by Task 2.3 and must
    not be added here.
    """

    WHITE_CAP = "__white_cap__"
    WHITE_BOUNDARY_CAP = "__white_boundary_cap__"
    WHITE_DETAIL_CAP = "__white_detail_cap__"
    DE = "__de__"
    GAMUT_MASK = "__gamut_mask__"


ThicknessMapKey = MapKey | str


def _normalize_key(key: ThicknessMapKey) -> str:
    """Return the exact legacy string for ``key``.

    ``MapKey`` members normalize to their ``.value`` (a plain ``str``); plain
    strings (and any ``str`` subclass) coerce to ``str`` so the backing dict
    only ever holds plain-string keys. This guarantees serializers and frontend
    payloads never see an enum repr such as ``MapKey.WHITE_CAP``.
    """
    if isinstance(key, MapKey):
        return str(key.value)
    if isinstance(key, str):
        return str(key)
    raise TypeError(
        f"thickness-map key must be a MapKey or str, got {type(key).__name__}"
    )


class ThicknessMaps(MutableMapping[str, np.ndarray]):
    """Compatibility-first wrapper around ``dict[str, np.ndarray]``.

    Accepts ``MapKey`` enums, reserved legacy strings, the CLI-only
    ``__filler__`` sentinel, and dynamic filament-id strings. All keys are
    normalized to plain ``str`` on write, so iteration and serialization yield
    the exact legacy strings. This is a typing/ergonomics wrapper, not a
    semantic rewrite: it supports every access pattern the pre-5.1 magic-string
    dict supported.
    """

    __slots__ = ("_data",)

    def __init__(
        self, data: Mapping[ThicknessMapKey, np.ndarray] | None = None
    ) -> None:
        self._data: dict[str, np.ndarray] = {}
        if data is not None:
            self.update(data)

    # --- core MutableMapping protocol -------------------------------------
    def __getitem__(self, key: ThicknessMapKey) -> np.ndarray:
        return self._data[_normalize_key(key)]

    def __setitem__(self, key: ThicknessMapKey, value: np.ndarray) -> None:
        self._data[_normalize_key(key)] = value

    def __delitem__(self, key: ThicknessMapKey) -> None:
        del self._data[_normalize_key(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    # --- conveniences tolerant of enum / str / dynamic loop keys ----------
    def __contains__(self, key: object) -> bool:
        try:
            return _normalize_key(key) in self._data  # type: ignore[arg-type]
        except TypeError:
            return False

    def get(
        self, key: ThicknessMapKey, default: Optional[np.ndarray] = None
    ) -> Optional[np.ndarray]:
        try:
            normalized = _normalize_key(key)
        except TypeError:
            return default
        return self._data.get(normalized, default)

    def __repr__(self) -> str:
        return f"ThicknessMaps({list(self._data)!r})"

    # --- explicit plain-dict boundary helpers -----------------------------
    def copy(self) -> "ThicknessMaps":
        """Return a shallow ``ThicknessMaps`` copy (arrays are shared)."""
        return ThicknessMaps(self._data)

    def as_string_dict(self) -> dict[str, np.ndarray]:
        """Return a plain ``dict`` with exact legacy string keys (shallow)."""
        return dict(self._data)

    # Alias kept for call-site clarity at plain-dict boundaries.
    to_plain_dict = as_string_dict

    # --- filament / reserved partition helpers ----------------------------
    def filament_ids(self) -> list[str]:
        """Dynamic filament-id keys only (excludes ``__``-prefixed sentinels)."""
        return [k for k in self._data if not k.startswith("__")]

    def filament_items(self) -> list[tuple[str, np.ndarray]]:
        """``(filament_id, array)`` pairs, excluding reserved sentinels."""
        return [(k, v) for k, v in self._data.items() if not k.startswith("__")]

    def reserved_items(self) -> list[tuple[str, np.ndarray]]:
        """``(sentinel, array)`` pairs for ``__``-prefixed reserved keys."""
        return [(k, v) for k, v in self._data.items() if k.startswith("__")]
