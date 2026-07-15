"""Unit tests for the typed ``thickness_maps`` container (Task 5.1).

These pin the behavior-preserving contract: ``MapKey`` values equal the exact
legacy strings, ``ThicknessMaps`` accepts enum / legacy-string / dynamic
filament-id / CLI ``__filler__`` keys interchangeably, and every boundary that
serializes the container emits plain string keys (never an enum repr).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from thickness_maps import (
    FilamentId,
    MapKey,
    ThicknessMapKey,
    ThicknessMaps,
    _normalize_key,
)


def _arr(seed: int) -> np.ndarray:
    return np.full((2, 2), float(seed), dtype=np.float32)


def test_mapkey_values_equal_exact_legacy_strings():
    assert MapKey.WHITE_CAP.value == "__white_cap__"
    assert MapKey.WHITE_BOUNDARY_CAP.value == "__white_boundary_cap__"
    assert MapKey.WHITE_DETAIL_CAP.value == "__white_detail_cap__"
    assert MapKey.DE.value == "__de__"
    assert MapKey.GAMUT_MASK.value == "__gamut_mask__"


def test_mapkey_str_equality_with_legacy_string():
    # str subclass: enum member compares equal to its legacy string.
    assert MapKey.WHITE_CAP == "__white_cap__"
    assert "__de__" == MapKey.DE


def test_translucent_underfill_is_not_a_mapkey_member():
    values = {m.value for m in MapKey}
    assert "__translucent_underfill__" not in values
    with pytest.raises((KeyError, ValueError)):
        MapKey("__translucent_underfill__")


def test_filler_is_not_a_mapkey_member():
    # __filler__ is legacy CLI-only and must not be a webapp/facade reserved key.
    values = {m.value for m in MapKey}
    assert "__filler__" not in values


def test_dynamic_filament_id_set_get():
    maps = ThicknessMaps()
    maps["bambu-basic-cyan"] = _arr(1)
    assert np.array_equal(maps["bambu-basic-cyan"], _arr(1))
    assert "bambu-basic-cyan" in maps


def test_reserved_enum_set_get():
    maps = ThicknessMaps()
    maps[MapKey.WHITE_CAP] = _arr(2)
    assert np.array_equal(maps[MapKey.WHITE_CAP], _arr(2))


def test_enum_and_legacy_string_resolve_to_same_slot():
    maps = ThicknessMaps()
    maps[MapKey.DE] = _arr(3)
    # Legacy string read sees the enum-written value...
    assert np.array_equal(maps["__de__"], _arr(3))
    # ...and a legacy-string write is visible via the enum.
    maps["__de__"] = _arr(4)
    assert np.array_equal(maps[MapKey.DE], _arr(4))
    assert len(maps) == 1


def test_legacy_reserved_string_get_still_works():
    maps = ThicknessMaps({"__white_boundary_cap__": _arr(5)})
    assert np.array_equal(maps["__white_boundary_cap__"], _arr(5))
    assert MapKey.WHITE_BOUNDARY_CAP in maps


def test_filler_accepted_as_ordinary_string():
    maps = ThicknessMaps()
    maps["__filler__"] = _arr(6)
    assert np.array_equal(maps["__filler__"], _arr(6))
    assert "__filler__" in maps
    # __filler__ is a reserved (``__``-prefixed) sentinel, not a filament id.
    assert "__filler__" not in maps.filament_ids()


def test_get_accepts_enum_string_and_missing():
    maps = ThicknessMaps({MapKey.GAMUT_MASK: _arr(7)})
    assert np.array_equal(maps.get(MapKey.GAMUT_MASK), _arr(7))
    assert np.array_equal(maps.get("__gamut_mask__"), _arr(7))
    assert maps.get("missing-filament") is None
    sentinel = _arr(99)
    assert np.array_equal(maps.get("missing", sentinel), sentinel)


def test_contains_tolerates_non_key_types():
    maps = ThicknessMaps({"f1": _arr(1)})
    assert "f1" in maps
    assert 123 not in maps  # non-str/non-enum -> False, never TypeError


def test_iteration_keys_items_values_yield_plain_strings():
    maps = ThicknessMaps()
    maps[MapKey.WHITE_CAP] = _arr(1)
    maps["bambu-basic-yellow"] = _arr(2)

    keys = list(maps)
    assert set(keys) == {"__white_cap__", "bambu-basic-yellow"}
    for k in keys:
        assert type(k) is str  # plain str, not MapKey

    item_keys = {k for k, _ in maps.items()}
    assert item_keys == {"__white_cap__", "bambu-basic-yellow"}
    for k in maps.keys():
        assert type(k) is str
    assert len(list(maps.values())) == 2


def test_dict_roundtrip_emits_exact_string_keys():
    maps = ThicknessMaps()
    maps[MapKey.WHITE_CAP] = _arr(1)
    maps[MapKey.DE] = _arr(2)
    maps["bambu-basic-magenta"] = _arr(3)

    plain = dict(maps)
    assert set(plain.keys()) == {
        "__white_cap__",
        "__de__",
        "bambu-basic-magenta",
    }
    for k in plain:
        assert type(k) is str

    # The key set must be JSON-serializable as exact strings (no enum repr).
    dumped = json.dumps({k: None for k in plain})
    assert "MapKey" not in dumped
    assert "__white_cap__" in dumped


def test_as_string_dict_and_alias_emit_exact_string_keys():
    maps = ThicknessMaps({MapKey.WHITE_DETAIL_CAP: _arr(1), "f": _arr(2)})
    sd = maps.as_string_dict()
    assert set(sd) == {"__white_detail_cap__", "f"}
    for k in sd:
        assert type(k) is str
    # to_plain_dict is an alias for as_string_dict.
    assert set(maps.to_plain_dict()) == set(sd)


def test_copy_is_isolated_for_membership():
    maps = ThicknessMaps({"f1": _arr(1)})
    clone = maps.copy()
    assert isinstance(clone, ThicknessMaps)
    clone["f2"] = _arr(2)
    assert "f2" in clone
    assert "f2" not in maps  # structural isolation
    # Shallow: arrays are shared references.
    assert clone["f1"] is maps["f1"]


def test_filament_helpers_exclude_reserved_sentinels():
    maps = ThicknessMaps()
    maps["bambu-basic-cyan"] = _arr(1)
    maps["bambu-basic-magenta"] = _arr(2)
    maps[MapKey.WHITE_CAP] = _arr(3)
    maps[MapKey.DE] = _arr(4)
    maps["__filler__"] = _arr(5)

    assert set(maps.filament_ids()) == {"bambu-basic-cyan", "bambu-basic-magenta"}
    assert {k for k, _ in maps.filament_items()} == {
        "bambu-basic-cyan",
        "bambu-basic-magenta",
    }
    assert {k for k, _ in maps.reserved_items()} == {
        "__white_cap__",
        "__de__",
        "__filler__",
    }


def test_construct_from_mapping_with_enum_keys():
    src = {MapKey.WHITE_CAP: _arr(1), "f": _arr(2)}
    maps = ThicknessMaps(src)
    assert set(maps) == {"__white_cap__", "f"}


def test_delete_and_setdefault_and_pop_normalize_keys():
    maps = ThicknessMaps({MapKey.WHITE_CAP: _arr(1)})
    # setdefault via enum hits the existing legacy-string slot.
    existing = maps.setdefault(MapKey.WHITE_CAP, _arr(2))
    assert np.array_equal(existing, _arr(1))
    # pop via legacy string.
    popped = maps.pop("__white_cap__")
    assert np.array_equal(popped, _arr(1))
    assert MapKey.WHITE_CAP not in maps


def test_normalize_key_rejects_non_key_types():
    assert _normalize_key(MapKey.DE) == "__de__"
    assert _normalize_key("plain") == "plain"
    with pytest.raises(TypeError):
        _normalize_key(123)  # type: ignore[arg-type]


def test_filament_id_newtype_is_str_at_runtime():
    fid = FilamentId("bambu-basic-cyan")
    assert isinstance(fid, str)
    assert fid == "bambu-basic-cyan"


def test_thickness_map_key_alias_admits_enum_and_str():
    # ThicknessMapKey is a typing alias (MapKey | str); a runtime smoke check.
    assert ThicknessMapKey is not None
