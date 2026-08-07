import json
from pathlib import Path

import pytest

from guide_assets import (
    ExampleImageSeeder,
    ExampleImageSeedStateError,
    GuideAssetCatalog,
)


def _catalog() -> GuideAssetCatalog:
    return GuideAssetCatalog(
        Path(__file__).parents[2] / "Prisma" / "data" / "generator" / "tutorial_images"
    )


def test_catalog_exposes_protected_virtual_metadata() -> None:
    metadata = _catalog().metadata("bubba-blanket")
    assert metadata["source_ref"] == "guide-image:bubba-blanket"
    assert metadata["virtual"] is True
    assert metadata["deletable"] is False
    assert metadata["renameable"] is False


def test_public_seed_is_once_only_and_deleted_copy_stays_deleted(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    state = tmp_path / "config" / "seed.json"
    seeder = ExampleImageSeeder(_catalog(), images, state)

    first = seeder.seed()["results"][0]
    public = images / first["filename"]
    assert first["status"] == "seeded"
    assert public.is_file()
    public.unlink()

    second = seeder.seed()["results"][0]
    assert second["status"] == "already-recorded"
    assert not public.exists()


def test_seed_collision_never_overwrites_user_file(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    collision = images / "Bubba Blanket.jpg"
    collision.write_bytes(b"user image")
    seeder = ExampleImageSeeder(_catalog(), images, tmp_path / "seed.json")

    result = seeder.seed()["results"][0]

    assert collision.read_bytes() == b"user image"
    assert result["filename"] == "Bubba Blanket (2).jpg"


def test_seed_publication_does_not_overwrite_a_racing_user_file(
    tmp_path: Path, monkeypatch
) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    seeder = ExampleImageSeeder(_catalog(), images, tmp_path / "seed.json")
    original_destination = seeder._destination
    raced = False

    def racing_destination(preferred_name: str) -> Path:
        nonlocal raced
        destination = original_destination(preferred_name)
        if not raced:
            raced = True
            destination.write_bytes(b"user image created during seeding")
        return destination

    monkeypatch.setattr(seeder, "_destination", racing_destination)
    result = seeder.seed()["results"][0]

    assert (images / "Bubba Blanket.jpg").read_bytes() == b"user image created during seeding"
    assert result["filename"] == "Bubba Blanket (2).jpg"


def test_legacy_tutorial_copy_is_adopted_without_duplication(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = _catalog().resolve("bubba-blanket")
    legacy = images / "Prisma Tutorial - Bubba Blanket.jpg"
    legacy.write_bytes(source.read_bytes())

    result = ExampleImageSeeder(_catalog(), images, tmp_path / "seed.json").seed()["results"][0]

    assert result["status"] == "adopted-existing"
    assert result["filename"] == legacy.name
    assert len(list(images.iterdir())) == 1


def test_renamed_matching_public_copy_is_adopted_without_duplication(tmp_path: Path) -> None:
    images = tmp_path / "Images"
    images.mkdir()
    source = _catalog().resolve("bubba-blanket")
    renamed = images / "my favorite example.jpg"
    renamed.write_bytes(source.read_bytes())

    result = ExampleImageSeeder(_catalog(), images, tmp_path / "seed.json").seed()["results"][0]

    assert result["status"] == "adopted-existing"
    assert result["filename"] == renamed.name
    assert len(list(images.iterdir())) == 1


def test_corrupt_seed_history_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "seed.json"
    state.write_text(json.dumps({"schema_version": 999, "seeded": {}}), encoding="utf-8")
    images = tmp_path / "Images"

    with pytest.raises(ExampleImageSeedStateError):
        ExampleImageSeeder(_catalog(), images, state).seed()
    assert not images.exists()


def test_asset_manifest_is_licensed_and_included_by_generator_specs() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads(
        (root / "Prisma" / "data" / "generator" / "tutorial_images" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    license_inventory = (root / "ASSET_LICENSES.md").read_text(encoding="utf-8")
    for asset in manifest["assets"]:
        assert asset["license"]
        assert asset["filename"] in license_inventory
    for spec_name in ("Prisma.spec", "PrismaSuite.spec"):
        spec = (root / "packaging" / spec_name).read_text(encoding="utf-8")
        assert '"tutorial_images"' in spec
        assert '"Prisma/data/generator/tutorial_images"' in spec
