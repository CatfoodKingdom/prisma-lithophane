from Prisma.lib.stack_geometry import canonicalize_filament_stack, collapse_adjacent_layers, unzip_layers


def test_collapse_adjacent_same_filament_layers() -> None:
    layers = collapse_adjacent_layers(
        [
            ("bambu-beige", 0.4),
            ("bambu-beige", 0.2),
            ("bambu-tough-white", 0.2),
        ]
    )

    assert layers == (("bambu-beige", 0.6), ("bambu-tough-white", 0.2))


def test_non_adjacent_repeats_remain_separate() -> None:
    layers = collapse_adjacent_layers(
        [
            ("filament-a", 0.2),
            ("filament-b", 0.2),
            ("filament-a", 0.2),
        ]
    )

    assert layers == (("filament-a", 0.2), ("filament-b", 0.2), ("filament-a", 0.2))


def test_canonicalize_parallel_lists_with_missing_and_zero_values() -> None:
    layers = canonicalize_filament_stack(
        ["filament-a", "filament-a", "filament-b", "filament-c"],
        [0.2, 0.0, 0.3],
    )

    assert layers == (("filament-a", 0.2), ("filament-b", 0.3))


def test_unzip_layers_returns_parallel_tuples() -> None:
    filaments, thicknesses = unzip_layers((("filament-a", 0.6), ("filament-b", 0.2)))

    assert filaments == ("filament-a", "filament-b")
    assert thicknesses == (0.6, 0.2)
