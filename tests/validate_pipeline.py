"""
Quick validation that the Prisma pipeline is working correctly.
Run after any structural changes to confirm nothing broke.

Usage: python tests/validate_pipeline.py
"""

import sys, os, json, time
import numpy as np

# Add project root to path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lithophane_generator"))
sys.path.insert(0, os.path.join(ROOT, "calibration", "perceptual_color_calibrator"))

PASS = 0
FAIL = 0

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ── Test 1: All profiles load ────────────────────────────────────────────────

print("\n[Test 1] Profile loading")
from lith_model import load_profile, PROFILES_DIR
from pathlib import Path

profile_files = sorted(PROFILES_DIR.glob("*.json"))
test("profiles directory exists", PROFILES_DIR.exists())
test("at least 18 profiles", len(profile_files) >= 18, f"found {len(profile_files)}")

load_errors = []
for pf in profile_files:
    try:
        load_profile(pf.stem)
    except Exception as e:
        load_errors.append(f"{pf.stem}: {e}")

test("all profiles load without error", len(load_errors) == 0,
     f"errors: {load_errors[:3]}")

# Verify filament IDs use hyphens (no underscores)
underscore_files = [pf.name for pf in profile_files if "_" in pf.stem]
test("no underscore filenames", len(underscore_files) == 0,
     f"found: {underscore_files[:3]}")


# ── Test 2: LUT builds successfully ─────────────────────────────────────────

print("\n[Test 2] LUT build + query")
from lith_model import load_profiles
from lith_lut import build_luts, query_luts_batch
import model_spline as mspl

wb = load_profile("panchroma-matte-cotton-white")
profiles = load_profiles(["bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"])

t0 = time.time()
luts = build_luts(
    profiles, wb, wb,
    layer_height=0.08, max_layers=10, k_max=2,
    d_wb=0.20, d_wc_min=0.08, d_wc_max=2.0,
)
dt = time.time() - t0

test("LUT builds without error", luts is not None and len(luts) > 0)
test("LUT build time < 30s", dt < 30, f"took {dt:.1f}s")

total_entries = sum(len(lut.oklab) for lut in luts)
test("LUT has entries", total_entries > 0, f"total: {total_entries}")

# Query for white target
white_oklab = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
thickness_dicts, de_array = query_luts_batch(luts, white_oklab)
test("white target query returns result", thickness_dicts is not None and len(thickness_dicts) > 0)
if len(de_array) > 0:
    best_de = float(de_array[0])
    test("white target dE < 10", best_de < 10, f"dE={best_de:.2f}")


# ── Test 3: Small image solve ────────────────────────────────────────────────

print("\n[Test 3] Small image solve")
from lith_pipeline import run

# Create 4x4 synthetic test image
test_img_data = np.array([
    [[255,0,0],[0,255,0],[0,0,255],[255,255,255]],
    [[255,255,0],[0,255,255],[255,0,255],[128,128,128]],
    [[64,0,0],[0,64,0],[0,0,64],[192,192,192]],
    [[200,100,50],[50,200,100],[100,50,200],[20,20,20]],
], dtype=np.uint8)

from PIL import Image
import tempfile
test_dir = tempfile.mkdtemp(prefix="prisma_test_")
test_img_path = os.path.join(test_dir, "test_4x4.png")
Image.fromarray(test_img_data).save(test_img_path)

try:
    result = run(
        image_path=test_img_path,
        palette=["bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"],
        out_dir=os.path.join(test_dir, "output"),
        layer_height=0.08, max_layers=15, pixel_size_mm=0.20,
        d_wb=0.20, k_max=2, max_dim_mm=1.0,
        verbose=False,
    )
    test("solve completes without error", True)

    # Check predicted image exists (output dir has timestamp appended)
    pred_path = None
    for root, dirs, files in os.walk(test_dir):
        if "predicted.png" in files:
            pred_path = os.path.join(root, "predicted.png")
            break
    test("predicted.png created", pred_path is not None)

except Exception as e:
    test("solve completes without error", False, str(e))

# Cleanup
import shutil
shutil.rmtree(test_dir, ignore_errors=True)


# ── Test 5: Baseline regression ─────────────────────────────────────────────

print("\n[Test 5] Baseline regression check")
baseline_dir = os.path.join(ROOT, "lithophane_generator", "data", "output", "_baseline_2026-03-19")
baseline_metrics_path = os.path.join(baseline_dir, "baseline_metrics.json")

if os.path.exists(baseline_metrics_path):
    with open(baseline_metrics_path) as f:
        baseline = json.load(f)

    # Re-run baseline
    cafe_path = os.path.join(ROOT, "lithophane_generator", "data", "input", "src_Cafe_Terrace_at_Night.jpg")
    if os.path.exists(cafe_path):
        test_out = tempfile.mkdtemp(prefix="prisma_regression_")
        try:
            result = run(
                image_path=cafe_path,
                palette=baseline["palette"],
                out_dir=os.path.join(test_out, "regression"),
                verbose=False,
                **baseline["params"],
            )
            # Find predicted.png and compare (output dir has timestamp appended)
            pred = None
            for root, dirs, files in os.walk(test_out):
                if "predicted.png" in files:
                    pred = np.array(Image.open(os.path.join(root, "predicted.png")))
                    break

            baseline_pred = np.array(Image.open(os.path.join(baseline_dir, "predicted.png")))

            if pred is None:
                test("regression predicted.png found", False)
            elif pred.shape == baseline_pred.shape:
                max_diff = np.max(np.abs(pred.astype(int) - baseline_pred.astype(int)))
                test("predicted image matches baseline (tolerance +/-2)",
                     max_diff <= 2, f"max pixel diff = {max_diff}")
            else:
                test("predicted image shape matches baseline", False,
                     f"{pred.shape} vs {baseline_pred.shape}")

        except Exception as e:
            test("baseline regression run", False, str(e))
        finally:
            shutil.rmtree(test_out, ignore_errors=True)
    else:
        print("  SKIP  cafe image not found")
else:
    print("  SKIP  no baseline metrics found (run baseline first)")


# ── Summary ──────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"  {PASS} passed, {FAIL} failed")
if FAIL > 0:
    print("  SOME TESTS FAILED")
    sys.exit(1)
else:
    print("  ALL TESTS PASSED")
    sys.exit(0)
