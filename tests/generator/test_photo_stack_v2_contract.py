"""Task 4B Gate 2 synthetic round-trip contract tests.

Prove the renamed lib contract writes only new values, rejects old schema/runtime
identity, and that predict.py no longer honors the removed old fallback manifest
keys. (Provider-alias rejection lives in test_appearance_model_provider.py.)
"""

from __future__ import annotations

import copy
import json

import pytest

from Prisma.lib.photo_stack_model.bundle import (
    BUNDLE_SCHEMA,
    DEPLOYMENT_BUNDLE_SCHEMA,
    PhotoStackBundleError,
    build_photo_stack_deployment_bundle,
    load_photo_stack_bundle,
    validate_photo_stack_bundle_payload,
    write_photo_stack_bundle,
)
from Prisma.lib.photo_stack_model.default_bundle import load_default_photo_stack_bundle
from Prisma.lib.photo_stack_model.model import PhotoStackModelArtifact
from Prisma.lib.photo_stack_model.predict import predict_stack


def _valid_payload() -> dict:
    return copy.deepcopy(load_default_photo_stack_bundle().payload)


def test_writer_emits_only_new_schema_values(tmp_path) -> None:
    path = write_photo_stack_bundle(tmp_path / "runtime_bundle.json", _valid_payload())
    reloaded = load_photo_stack_bundle(path)
    assert reloaded.payload["schema"] == BUNDLE_SCHEMA == "prisma_photo_stack_v2_runtime_bundle"
    assert reloaded.payload["runtime_constants_version"] == "photo_stack_v2_2026_06_09"
    assert reloaded.payload["model_family"] == "photo_stack"
    assert reloaded.payload["model_version"] == "v2"
    blob = json.dumps(reloaded.payload)
    for tok in ("prisma_non_ml_photo", "td_full_license_probe_v63", "v63_direct_context"):  # photo-stack-v2-allow
        assert tok not in blob


def test_deployment_bundle_is_runtime_equivalent_public_subset(tmp_path) -> None:
    source = _valid_payload()
    original = copy.deepcopy(source)
    deployment = build_photo_stack_deployment_bundle(source)

    assert source == original
    assert deployment["schema"] == DEPLOYMENT_BUNDLE_SCHEMA
    assert deployment["model"] == source["model"]
    if "filament_classification" in source:
        assert deployment["filament_classification"] == source["filament_classification"]
    else:
        assert deployment["filament_classification"]["mode"] == "legacy_token_white"
    assert deployment["fingerprint"] == source["fingerprint"]
    assert "verification" not in deployment
    assert "source" not in deployment
    assert "exported_at_unix" not in deployment
    assert "live_fit_source_of_truth" not in deployment
    reloaded = load_photo_stack_bundle(
        write_photo_stack_bundle(tmp_path / "deployment.json", deployment)
    )
    assert reloaded.payload == deployment


def test_deployment_bundle_rejects_private_or_diagnostic_fields() -> None:
    deployment = build_photo_stack_deployment_bundle(_valid_payload())
    deployment["source"] = {"data_root": "C:/private"}
    with pytest.raises(PhotoStackBundleError, match="exactly the public runtime fields"):
        validate_photo_stack_bundle_payload(deployment)


def test_loader_rejects_old_runtime_schema_id() -> None:
    payload = _valid_payload()
    payload["schema"] = "prisma_non_ml_photo_v63_runtime_bundle"  # photo-stack-v2-allow
    with pytest.raises(PhotoStackBundleError, match="unsupported bundle schema"):
        validate_photo_stack_bundle_payload(payload)


def test_loader_rejects_old_runtime_constants_version() -> None:
    payload = _valid_payload()
    payload["runtime_constants_version"] = "v63_direct_context_2026_06_09"  # photo-stack-v2-allow
    with pytest.raises(PhotoStackBundleError, match="runtime_constants_version"):
        validate_photo_stack_bundle_payload(payload)


def test_solve_resolver_rejects_any_bundle_outside_active_published_library(tmp_path) -> None:
    from fastapi import HTTPException

    from Prisma.generator import server

    stale = tmp_path / "runtime_bundle_v63.json"  # photo-stack-v2-allow
    stale.write_text("{}", encoding="utf-8")
    cfg = {
        "appearance_model_provider": "photo_stack_bundle",
        "photo_stack_bundle_path": str(stale),
    }
    with pytest.raises(HTTPException) as excinfo:
        server._resolve_photo_stack_candidate_path_for_solve(cfg)
    assert excinfo.value.status_code == 400
    assert "outside the active published library" in str(excinfo.value.detail)


def test_predict_rejects_removed_old_fallback_manifest_keys(tmp_path) -> None:
    # predict.py no longer reads the old v63_runtime_bundle / v63_runtime_bundle_path
    # fallbacks, so an artifact that only carries them cannot resolve a bundle.
    artifact = PhotoStackModelArtifact(
        run_dir=tmp_path,
        manifest={"v63_runtime_bundle_path": "runtime_bundle_v63.json"},  # photo-stack-v2-allow
        model={"v63_runtime_bundle_path": "runtime_bundle_v63.json"},  # photo-stack-v2-allow
        corrections={},
    )
    with pytest.raises(ValueError, match="does not identify a runtime bundle"):
        predict_stack(artifact, [("unit-filament", 0.1)], use_corrections=False)
