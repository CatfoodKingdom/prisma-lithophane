"""
models.py — Pydantic models for the unified calibration API.

These define the data shapes for API requests, responses, and internal domain objects.
"""
from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


# ── Domain objects ─────────────────────────────────────────────────────────────

class FilamentRef(BaseModel):
    """Filament references within a sample."""
    variable: str
    fixed: list[str] = Field(default_factory=list)


class StripGeometry(BaseModel):
    """Physical geometry of the swatch strip."""
    num_swatches: int = 8
    step_w_mm: float = 12.0
    step_h_mm: float = 20.0
    border_mm: float = 3.0


class StripDefinition(BaseModel):
    """What was printed — thicknesses, layers, geometry."""
    n_layers: int
    layer_height_mm: float
    mode: str = "linear"
    anchor_mm: Optional[float] = None
    variable_thicknesses_mm: list[float]
    fixed_thicknesses_mm: list[float] = Field(default_factory=list)
    strip_geometry: Optional[StripGeometry] = None


class SwatchMeasurement(BaseModel):
    """Extracted color data for one swatch."""
    swatch_index: int
    nominal_thickness_mm: float
    hex: str
    R: int
    G: int
    B: int
    R_linear: float
    G_linear: float
    B_linear: float
    fit_state: str = "included"      # "included" | "excluded"
    exclusion_reason: str = ""


class Measurements(BaseModel):
    """All measurements for a processed sample."""
    swatches: list[SwatchMeasurement]
    I0_linear: Optional[dict] = None  # {R, G, B} — traceability only
    blank_image: Optional[str] = None
    source_image: Optional[str] = None


class Sample(BaseModel):
    """Central domain object — a calibration sample (née experiment)."""
    sample_id: str                           # e.g. "exp-001"
    name: str = ""
    created: str = ""
    notes: Optional[str] = None
    filaments: FilamentRef
    step_id: str = ""
    step_file: str = ""
    roles: list[dict] = Field(default_factory=list)
    strip_definition: Optional[StripDefinition] = None
    photos: list[str] = Field(default_factory=list)
    blank_image: Optional[str] = None

    # New fields for the unified workflow:
    assigned_image: Optional[str] = None
    assigned_blank_id: Optional[str] = None
    processing_status: str = "unassigned"    # unassigned|assigned|processed|failed|flagged
    orientation_rots: Optional[int] = None
    measurements: Optional[Measurements] = None
    flag_reason: Optional[str] = None

    # Post-processing review accepted (persisted — survives reload)
    review_accepted: bool = False

    # Fitting exclusion state (persisted across sessions)
    fit_exclude: bool = False                # True → entire sample excluded from fitting
    excluded_swatches: list[int] = Field(default_factory=list)  # swatch indexes excluded from fitting


class Blank(BaseModel):
    """Registered flatfield reference image."""
    blank_id: str
    original_filename: str
    registered_at: str = ""
    exif_timestamp: Optional[str] = None
    storage_path: str = ""
    session_tag: Optional[str] = None


class Filament(BaseModel):
    """Filament from the registry."""
    filament_id: str
    display_name: str = ""
    manufacturer: str = ""
    color_name: str = ""
    material: str = ""
    hex: str = ""
    has_profile: bool = False
    white_cap_eligible: bool = False
    special_roles: list[str] = Field(default_factory=list)
    # Per-filament model exclusion (doc 33 B1f). When True, every production
    # fitter drops samples that reference this filament. Forward-compatible with
    # the revised-backend `exclude-from-model` field.
    exclude_from_model: bool = False
    notes: str = ""


class StepDefinitionMeta(BaseModel):
    """Structured STEP definition metadata."""
    step_id: str = ""
    file_name: str
    alias: str = ""
    bundle: str = ""
    layer_count: int = 0
    variable_thicknesses_mm: list[float] = Field(default_factory=list)
    fixed_layers: list[dict] = Field(default_factory=list)
    layer_height_mm: float = 0.0
    control_swatch: bool = False
    artifact_exists: bool = True
    artifact_path: str = ""
    source_filenames: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class StepRecord(BaseModel):
    """Canonical STEP record; artifact files are derived from this record."""
    step_id: str
    geometry_signature: str
    file_name: str
    alias: str = ""
    layer_count: int
    variable_thicknesses_mm: list[float]
    fixed_layers: list[dict] = Field(default_factory=list)
    roles: list[dict] = Field(default_factory=list)
    swatch_slots: list[dict] = Field(default_factory=list)
    layer_height_mm: float
    strip_geometry: StripGeometry = Field(default_factory=StripGeometry)
    artifact_exists: bool = True
    artifact_path: str = ""
    source_filenames: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ── Processing results ─────────────────────────────────────────────────────────

class ProcessingConfidence(BaseModel):
    """Confidence metadata from headless processing."""
    spine_score: float = 0.0
    detection_strategy: str = ""
    skew_angle_deg: float = 0.0
    contour_found: bool = False


class ProcessingResult(BaseModel):
    """Result of processing one sample."""
    sample_id: str
    status: str                              # success|failed_detection|failed_flatfield|low_confidence
    confidence: Optional[ProcessingConfidence] = None
    measurements: Optional[Measurements] = None
    error_detail: Optional[str] = None
    extraction_result_payload: Optional[dict] = None


class BatchProcessingResult(BaseModel):
    """Result of batch processing."""
    total: int
    succeeded: int
    failed: int
    flagged: int
    results: list[ProcessingResult]


# ── API request/response shapes ────────────────────────────────────────────────

class AssignImageRequest(BaseModel):
    filename: Optional[str] = None
    orientation_rots: Optional[int] = None


class AssignBlankRequest(BaseModel):
    blank_id: Optional[str] = None


class SwapImagesRequest(BaseModel):
    sample_id_a: str
    sample_id_b: str


class FlagRequest(BaseModel):
    reason: str = ""


class ExcludeSwatchRequest(BaseModel):
    swatch_index: int
    reason: str = ""


class IncludeSwatchRequest(BaseModel):
    swatch_index: int


class FitExclusionRequest(BaseModel):
    """Toggle sample-level or swatch-level fitting exclusion."""
    fit_exclude: Optional[bool] = None               # set sample-level exclusion
    excluded_swatches: Optional[list[int]] = None     # replace full swatch exclusion list


class RegisterBlankRequest(BaseModel):
    filename: str
    session_tag: Optional[str] = None


class RotateImageRequest(BaseModel):
    rotation_cw: int = Field(ge=0, le=3)


class CsvAssignmentCommitRequest(BaseModel):
    preview_token: str
    register_unregistered_blanks: bool = False


class BackupCreateRequest(BaseModel):
    package_type: Literal["core_library", "working_state"] = "working_state"
    include_raw_images: bool = True


class PublishModelLibraryRequest(BaseModel):
    library_name: str = Field(min_length=1, max_length=120)
    library_version: str = Field(min_length=1, max_length=64)
    publisher: str = Field(min_length=1, max_length=120)
    minimum_prisma_version: str = Field(default="0.1.0", min_length=1, max_length=64)
    maximum_prisma_version: str | None = Field(default=None, min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    release_notes: str = Field(default="", max_length=8000)


class BackupRestoreRequest(BaseModel):
    restore_token: str
    confirmation: str


class BackupRestorePathValidateRequest(BaseModel):
    path: str


class RawArchiveImportRequest(BaseModel):
    archive_token: str
    image_asset_ids: Optional[list[str]] = None


class RawArchiveReleaseRequest(BaseModel):
    archive_token: str
    confirmation: str
    image_asset_ids: Optional[list[str]] = None


class RawArchivePathValidateRequest(BaseModel):
    path: str


class AdjustedMarginsRequest(BaseModel):
    left: float
    right: float
    top: float
    bottom: float
    dividers: list[float]


class CreateFilamentRequest(BaseModel):
    manufacturer: str
    color_name: str
    hex: str
    material: str = "unknown"
    white_cap_eligible: bool = False
    special_roles: list[str] = Field(default_factory=list)
    exclude_from_model: bool = False
    notes: str = ""


class UpdateFilamentRequest(BaseModel):
    manufacturer: Optional[str] = None
    color_name: Optional[str] = None
    hex: Optional[str] = None
    material: Optional[str] = None
    white_cap_eligible: Optional[bool] = None
    special_roles: Optional[list[str]] = None
    exclude_from_model: Optional[bool] = None
    notes: Optional[str] = None


class SampleRoleAssignmentRequest(BaseModel):
    role_index: int = Field(ge=1)
    filament_id: str


class CreateSampleRequest(BaseModel):
    step_id: Optional[str] = None
    step_file: Optional[str] = None
    variable_filament_id: str
    fixed_filament_ids: list[str] = Field(default_factory=list)
    fixed_thicknesses_mm: Optional[list[float]] = None
    role_assignments: Optional[list[SampleRoleAssignmentRequest]] = None
    notes: Optional[str] = None


class UpdateSampleRequest(BaseModel):
    step_id: Optional[str] = None
    step_file: Optional[str] = None
    variable_filament_id: Optional[str] = None
    fixed_filament_ids: Optional[list[str]] = None
    fixed_thicknesses_mm: Optional[list[float]] = None
    role_assignments: Optional[list[SampleRoleAssignmentRequest]] = None
    notes: Optional[str] = None
    review_accepted: Optional[bool] = None


class BundleCreateRequest(BaseModel):
    variable_filament_id: str
    step_ids: list[str] = Field(default_factory=list)
    step_files: list[str] = Field(default_factory=list)
    fixed_filament_ids: list[str] = Field(default_factory=list)
    role_assignments_by_step: Optional[dict[str, list[SampleRoleAssignmentRequest]]] = None
    notes: Optional[str] = None


class BundleMappingDraftSlotRequest(BaseModel):
    draft_slot_id: str
    label: Optional[str] = None


class BundleMappingRoleSlotRequest(BaseModel):
    geometry_role_id: str
    draft_slot_id: Optional[str] = None


class BundleMappingMemberRequest(BaseModel):
    geometry_bundle_member_id: str
    role_slot_map: list[BundleMappingRoleSlotRequest] = Field(default_factory=list)


class BundleMappingSaveRequest(BaseModel):
    expected_updated_at: Optional[str] = None
    allow_incomplete: bool = False
    draft_material_slots: list[BundleMappingDraftSlotRequest] = Field(default_factory=list)
    members: list[BundleMappingMemberRequest] = Field(default_factory=list)


class BundleMaterialSlotAssignmentRequest(BaseModel):
    material_slot_id: str
    filament_id: str


class GeometryBundleSampleCreateRequest(BaseModel):
    bundle_id: str
    material_slot_assignments: list[BundleMaterialSlotAssignmentRequest] = Field(default_factory=list)
    batch_material_slot_id: Optional[str] = None
    batch_filament_ids: list[str] = Field(default_factory=list)
    notes: Optional[str] = None


class BatchSampleCreateRequest(BaseModel):
    step_id: Optional[str] = None
    step_file: Optional[str] = None
    batch_role: str
    batch_filament_ids: list[str] = Field(default_factory=list)
    variable_filament_id: Optional[str] = None
    fixed_filament_ids: list[str] = Field(default_factory=list)
    fixed_thicknesses_mm: Optional[list[float]] = None
    role_assignments: Optional[list[SampleRoleAssignmentRequest]] = None
    notes: Optional[str] = None


class CornerPoint(BaseModel):
    """A single 2D point in image pixel coordinates."""
    x: float
    y: float


class ManualExtractRequest(BaseModel):
    """Request to perform manual strip extraction using 4 corners."""
    sample_id: str
    corners: list[CornerPoint] = Field(..., min_length=4, max_length=4)
    orientation: int = Field(ge=0, le=3, description="D-pad value: 0=up, 1=right, 2=down, 3=left")
    preview_width: int = Field(gt=0, description="Natural width of the preview image used for corner placement")
    preview_height: int = Field(gt=0, description="Natural height of the preview image used for corner placement")
    commit: bool = Field(default=False, description="If true, finalize sample as processed")


# ── Shared utility functions ───────────────────────────────────────────────────

def classify_mode(variable_thicknesses: list[float]) -> str:
    """Classify thickness spacing as 'linear' or 'manual'.

    Returns 'linear' if all consecutive differences are equal (within
    rounding tolerance), otherwise 'manual'.
    """
    if len(variable_thicknesses) < 2:
        return "linear"
    diffs = [round(variable_thicknesses[i + 1] - variable_thicknesses[i], 6)
             for i in range(len(variable_thicknesses) - 1)]
    if len(set(round(d, 4) for d in diffs)) > 1:
        return "manual"
    return "linear"


# ── Extraction results ──────────────────────────────────────────────────────
# Unified per-sample extraction_result sidecar (Step 1: schema + storage only).
# Additive — nothing in product code reads or writes this yet. Governed by docs 21/24.
# Placed at end-of-file so the existing `CornerPoint` (above) is already defined.

class SwatchTransmission(BaseModel):
    """DOMAIN 1 — optical input (linear, scene-referred). Used by the color models."""
    R_linear: float
    G_linear: float
    B_linear: float


class SwatchDisplay(BaseModel):
    """DOMAIN 2 — display-referred sRGB, UI/diagnostic only (0-255)."""
    hex: str
    R: int
    G: int
    B: int


class SwatchBox(BaseModel):
    """Pixel bounding box of a swatch in its source asset."""
    x0: int
    y0: int
    x1: int
    y1: int


class SwatchAppearance(BaseModel):
    """DOMAIN 3 — Camera Transform target sampled from the embedded JPEG."""
    source: str = ""
    jpeg_r: float
    jpeg_g: float
    jpeg_b: float
    swatch_box: Optional[SwatchBox] = None


class SwatchExtraction(BaseModel):
    """One swatch's extracted color across domains. Exclusion-agnostic (stores all swatches)."""
    swatch_index: int                                  # canonical identity
    nominal_thickness_mm: float                        # unrounded float-mm
    geometry_variable_thickness_mm: Optional[float] = None
    transmission: SwatchTransmission
    display: SwatchDisplay
    appearance: Optional[SwatchAppearance] = None
    fit_excluded: bool = False                         # NOT the legacy fit_state enum
    fit_exclusion_reason: str = ""


class MethodProvenance(BaseModel):
    """How the strip was located/extracted — audit trail."""
    strip_location_quad: Optional[list[CornerPoint]] = None
    strip_location_source: Optional[str] = None        # "automatic_detected_fit_rectangle" | "manual_corner_selection"
    coordinate_space: Optional[str] = None             # "raw_image_pixels" | "normalized_source_image" | "preview_image_pixels"
    corner_order: Optional[str] = None                 # "tl,tr,br,bl"
    source_or_preview_asset_id: Optional[str] = None
    preview_width: Optional[int] = None
    preview_height: Optional[int] = None
    preview_scale: Optional[float] = None
    image_rotation_used: Optional[int] = None


class EvidenceBinding(BaseModel):
    """Binds the result to its source evidence assets."""
    sample_image_asset_id: Optional[str] = None
    blank_id: Optional[str] = None
    orientation_rots: Optional[int] = None
    source_image: Optional[str] = None
    cr2_source: Optional[str] = None         # "images" | "inbox" | None (resolved CR2 location, doc-29 §3.1)


class ExtractionMeasurements(BaseModel):
    """The measured payload — all swatches, plus traceability I0."""
    I0_linear: Optional[dict] = None                   # {R, G, B} traceability only
    swatches: list[SwatchExtraction] = Field(default_factory=list)


class ExtractionDiagnostics(BaseModel):
    """Detection/quality diagnostics."""
    confidence: float = 0.0                            # spine_score; 0.0 sentinel for manual
    detection_strategy: str = ""                       # "cascade" | "all_failed" | "manual"
    appearance_order_correlation: Optional[float] = None
    appearance_order_correlation_state: Optional[str] = None  # "finite" | "nan" | "not_computed"
    appearance_orientation_flipped: Optional[bool] = None
    appearance_error: Optional[str] = None             # appearance-extraction failure reason (doc-29 §3.2)
    decode_environment: Optional[dict[str, str]] = None  # rawpy/libraw/pillow/libjpeg versions (doc-29 §3.3)
    skew_angle_deg: Optional[float] = None
    contour_found: Optional[bool] = None


class ExtractionResult(BaseModel):
    """Per-sample extraction-result sidecar. Additive; nothing reads it in Step 1."""
    extraction_result_id: str
    schema_version: int = 1
    sample_id: str                                     # carried from Sample.sample_id
    evidence_set_id: Optional[str] = None
    geometry_id: Optional[str] = None                  # carried from Sample.step_id
    geometry_fingerprint: Optional[str] = None         # stays null in Step 1
    method: str = "automatic"                          # "automatic" | "manual"
    review_state: str = "pending_review"               # "pending_review" | "accepted"
    reviewed_at: Optional[str] = None
    review_notes: str = ""
    method_provenance: Optional[MethodProvenance] = None
    evidence_binding: Optional[EvidenceBinding] = None
    measurements: ExtractionMeasurements = Field(default_factory=ExtractionMeasurements)
    diagnostics: Optional[ExtractionDiagnostics] = None
    state: str = "active"                               # doc-06 lifecycle shape
    created_at: Optional[str] = None

    @model_validator(mode="after")
    def _check_swatch_indices(self) -> "ExtractionResult":
        """Swatch identity is `swatch_index` (doc-24 Q1). The spline adapter now
        pairs by swatch_index (doc 32 Stage 4.2/4.3), so positional equality is no
        longer required — only that swatch_index values are unique integers, so
        reordered/sparse swatches are valid but a duplicate fails loudly."""
        seen: set[int] = set()
        for swatch in self.measurements.swatches:
            si = swatch.swatch_index
            if si in seen:
                raise ValueError(
                    f"duplicate swatch_index {si}; swatch_index values must be unique"
                )
            seen.add(si)
        return self
