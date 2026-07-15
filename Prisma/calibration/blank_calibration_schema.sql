-- Prisma calibration revised-backend SQLite schema.
-- Status: final migration target schema, version 2026-06-18.
-- This is application schema, not the disposable rehearsal schema.

PRAGMA foreign_keys = ON;

CREATE TABLE schema_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_metadata(key, value) VALUES
  ('schema_name', 'prisma_calibration_revised_backend'),
  ('schema_version', '2026-06-18'),
  ('source_truth_policy', 'filaments/geometries/images/blanks/samples/evidence/fit_controls are source truth; extraction/model artifacts are regenerable derived records');

-- ---------------------------------------------------------------------------
-- Filaments
-- ---------------------------------------------------------------------------

CREATE TABLE filaments (
  filament_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  manufacturer TEXT NOT NULL,
  material TEXT NOT NULL DEFAULT '',
  hex_color TEXT NOT NULL,
  white_cap_eligible INTEGER NOT NULL DEFAULT 0 CHECK (white_cap_eligible IN (0, 1)),
  exclude_from_model INTEGER NOT NULL DEFAULT 0 CHECK (exclude_from_model IN (0, 1)),
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  updated_at TEXT,
  CHECK (hex_color GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]')
);

CREATE TABLE filament_special_roles (
  filament_id TEXT NOT NULL,
  special_role TEXT NOT NULL CHECK (special_role IN ('black', 'transparent')),
  PRIMARY KEY (filament_id, special_role),
  FOREIGN KEY (filament_id) REFERENCES filaments(filament_id) ON DELETE CASCADE
);

CREATE INDEX idx_filaments_manufacturer_name ON filaments(manufacturer, name);
CREATE INDEX idx_filaments_exclude_from_model ON filaments(exclude_from_model);

-- ---------------------------------------------------------------------------
-- Calibration strip geometries
-- ---------------------------------------------------------------------------

CREATE TABLE calibration_strip_geometries (
  geometry_id TEXT PRIMARY KEY,
  alias TEXT NOT NULL UNIQUE,
  structural_fingerprint TEXT NOT NULL UNIQUE,
  layout_rows INTEGER NOT NULL CHECK (layout_rows > 0),
  layout_columns INTEGER NOT NULL CHECK (layout_columns > 0),
  swatch_count INTEGER NOT NULL CHECK (swatch_count > 0),
  swatch_width_mm REAL NOT NULL CHECK (swatch_width_mm > 0),
  swatch_height_mm REAL NOT NULL CHECK (swatch_height_mm > 0),
  spine_width_mm REAL NOT NULL CHECK (spine_width_mm >= 0),
  spine_total_thickness_mm REAL CHECK (spine_total_thickness_mm IS NULL OR spine_total_thickness_mm > 0),
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  updated_at TEXT,
  CHECK (layout_rows * layout_columns = swatch_count)
);

CREATE TABLE geometry_roles (
  geometry_role_id TEXT PRIMARY KEY,
  geometry_id TEXT NOT NULL,
  role_index INTEGER NOT NULL CHECK (role_index >= 1),
  role_label TEXT NOT NULL,
  role_kind TEXT NOT NULL CHECK (role_kind IN ('fixed', 'variable')),
  fixed_thickness_mm REAL,
  created_at TEXT,
  updated_at TEXT,
  UNIQUE (geometry_id, role_index),
  UNIQUE (geometry_id, role_label),
  CHECK (
    (role_kind = 'fixed' AND fixed_thickness_mm IS NOT NULL AND fixed_thickness_mm >= 0)
    OR
    (role_kind = 'variable' AND fixed_thickness_mm IS NULL)
  ),
  FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_geometry_one_variable_role
  ON geometry_roles(geometry_id)
  WHERE role_kind = 'variable';

CREATE TABLE geometry_swatch_slots (
  geometry_id TEXT NOT NULL,
  swatch_index INTEGER NOT NULL CHECK (swatch_index >= 0),
  row_index INTEGER NOT NULL CHECK (row_index >= 0),
  column_index INTEGER NOT NULL CHECK (column_index >= 0),
  variable_thickness_mm REAL NOT NULL CHECK (variable_thickness_mm >= 0),
  PRIMARY KEY (geometry_id, swatch_index),
  UNIQUE (geometry_id, row_index, column_index),
  FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
);

CREATE INDEX idx_geometry_roles_geometry ON geometry_roles(geometry_id, role_index);
CREATE INDEX idx_geometry_swatch_slots_geometry ON geometry_swatch_slots(geometry_id, swatch_index);

-- ---------------------------------------------------------------------------
-- Images and registered blanks
-- ---------------------------------------------------------------------------

CREATE TABLE image_import_sessions (
  import_session_id TEXT PRIMARY KEY,
  session_label TEXT NOT NULL UNIQUE,
  imported_at TEXT NOT NULL,
  source_inbox_path TEXT,
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE image_assets (
  image_asset_id TEXT PRIMARY KEY,
  content_sha256 TEXT NOT NULL,
  original_filename TEXT NOT NULL,
  original_extension TEXT NOT NULL,
  media_type TEXT NOT NULL CHECK (media_type IN ('raw_cr2', 'jpeg', 'other_supported')),
  managed_rel_path TEXT NOT NULL UNIQUE,
  import_session_id TEXT,
  capture_timestamp TEXT,
  file_size_bytes INTEGER CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
  original_mtime_ns INTEGER CHECK (original_mtime_ns IS NULL OR original_mtime_ns >= 0),
  rotation_override_rots INTEGER CHECK (rotation_override_rots IS NULL OR rotation_override_rots IN (0, 1, 2, 3)),
  created_at TEXT,
  UNIQUE (content_sha256, original_filename),
  FOREIGN KEY (import_session_id) REFERENCES image_import_sessions(import_session_id) ON DELETE SET NULL
);

CREATE INDEX idx_image_assets_hash ON image_assets(content_sha256);
CREATE INDEX idx_image_assets_filename ON image_assets(original_filename);
CREATE INDEX idx_image_assets_session ON image_assets(import_session_id);

CREATE TABLE image_asset_ui_state (
  image_asset_id TEXT PRIMARY KEY,
  hidden INTEGER NOT NULL DEFAULT 0 CHECK (hidden IN (0, 1)),
  updated_at TEXT,
  FOREIGN KEY (image_asset_id) REFERENCES image_assets(image_asset_id) ON DELETE CASCADE
);

CREATE TABLE registered_blanks (
  blank_id TEXT PRIMARY KEY,
  image_asset_id TEXT NOT NULL UNIQUE,
  registered_at TEXT,
  notes TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (image_asset_id) REFERENCES image_assets(image_asset_id) ON DELETE RESTRICT
);

CREATE INDEX idx_registered_blanks_image ON registered_blanks(image_asset_id);

-- ---------------------------------------------------------------------------
-- Samples, sample evidence, and fit controls
-- ---------------------------------------------------------------------------

CREATE TABLE samples (
  sample_id TEXT PRIMARY KEY,
  sample_number INTEGER NOT NULL UNIQUE CHECK (sample_number > 0),
  geometry_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  workflow_status TEXT NOT NULL DEFAULT 'unassigned'
    CHECK (workflow_status IN ('unassigned', 'assigned', 'processed', 'failed', 'flagged')),
  flag_reason TEXT,
  UNIQUE (sample_id, geometry_id),
  FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
);

CREATE TABLE sample_role_assignments (
  sample_id TEXT NOT NULL,
  geometry_id TEXT NOT NULL,
  role_index INTEGER NOT NULL,
  filament_id TEXT NOT NULL,
  PRIMARY KEY (sample_id, role_index),
  FOREIGN KEY (sample_id, geometry_id) REFERENCES samples(sample_id, geometry_id) ON DELETE CASCADE,
  FOREIGN KEY (geometry_id, role_index) REFERENCES geometry_roles(geometry_id, role_index) ON DELETE RESTRICT,
  FOREIGN KEY (filament_id) REFERENCES filaments(filament_id) ON DELETE RESTRICT
);

CREATE INDEX idx_sample_role_assignments_filament ON sample_role_assignments(filament_id);
CREATE INDEX idx_sample_role_assignments_geometry_role ON sample_role_assignments(geometry_id, role_index);

CREATE TABLE sample_evidence_assignments (
  sample_id TEXT PRIMARY KEY,
  sample_image_asset_id TEXT UNIQUE,
  blank_id TEXT,
  open_side_orientation_rots INTEGER CHECK (open_side_orientation_rots IS NULL OR open_side_orientation_rots IN (0, 1, 2, 3)),
  sample_image_rotation_override_rots INTEGER CHECK (
    sample_image_rotation_override_rots IS NULL OR sample_image_rotation_override_rots IN (0, 1, 2, 3)
  ),
  assigned_at TEXT,
  FOREIGN KEY (sample_id) REFERENCES samples(sample_id) ON DELETE CASCADE,
  FOREIGN KEY (sample_image_asset_id) REFERENCES image_assets(image_asset_id) ON DELETE RESTRICT,
  FOREIGN KEY (blank_id) REFERENCES registered_blanks(blank_id) ON DELETE RESTRICT
);

CREATE INDEX idx_sample_evidence_blank ON sample_evidence_assignments(blank_id);
CREATE INDEX idx_sample_evidence_sample_image ON sample_evidence_assignments(sample_image_asset_id);

CREATE TRIGGER trg_sample_image_not_registered_blank_insert
BEFORE INSERT ON sample_evidence_assignments
WHEN NEW.sample_image_asset_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM registered_blanks WHERE image_asset_id = NEW.sample_image_asset_id)
BEGIN
  SELECT RAISE(ABORT, 'sample image asset is registered as a blank');
END;

CREATE TRIGGER trg_sample_image_not_registered_blank_update
BEFORE UPDATE OF sample_image_asset_id ON sample_evidence_assignments
WHEN NEW.sample_image_asset_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM registered_blanks WHERE image_asset_id = NEW.sample_image_asset_id)
BEGIN
  SELECT RAISE(ABORT, 'sample image asset is registered as a blank');
END;

CREATE TRIGGER trg_blank_image_not_sample_image_insert
BEFORE INSERT ON registered_blanks
WHEN EXISTS (SELECT 1 FROM sample_evidence_assignments WHERE sample_image_asset_id = NEW.image_asset_id)
BEGIN
  SELECT RAISE(ABORT, 'blank image asset is already assigned as a sample image');
END;

CREATE TRIGGER trg_blank_image_not_sample_image_update
BEFORE UPDATE OF image_asset_id ON registered_blanks
WHEN EXISTS (SELECT 1 FROM sample_evidence_assignments WHERE sample_image_asset_id = NEW.image_asset_id)
BEGIN
  SELECT RAISE(ABORT, 'blank image asset is already assigned as a sample image');
END;

CREATE TABLE sample_fit_controls (
  sample_id TEXT PRIMARY KEY,
  exclude_sample_from_fits INTEGER NOT NULL DEFAULT 0 CHECK (exclude_sample_from_fits IN (0, 1)),
  exclude_reason TEXT,
  updated_at TEXT,
  FOREIGN KEY (sample_id) REFERENCES samples(sample_id) ON DELETE CASCADE
);

CREATE TABLE sample_swatch_fit_exclusions (
  sample_id TEXT NOT NULL,
  swatch_index INTEGER NOT NULL CHECK (swatch_index >= 0),
  exclude_from_fits INTEGER NOT NULL DEFAULT 1 CHECK (exclude_from_fits IN (0, 1)),
  exclude_reason TEXT,
  updated_at TEXT,
  PRIMARY KEY (sample_id, swatch_index),
  FOREIGN KEY (sample_id) REFERENCES samples(sample_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Extraction results
-- ---------------------------------------------------------------------------

CREATE TABLE extraction_results (
  extraction_result_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
  sample_id TEXT NOT NULL UNIQUE,
  evidence_set_id TEXT,
  geometry_id TEXT NOT NULL,
  geometry_fingerprint TEXT,
  method TEXT NOT NULL CHECK (method IN ('automatic', 'manual', 'legacy_backfill')),
  review_state TEXT NOT NULL CHECK (review_state IN ('pending_review', 'accepted')),
  result_state TEXT NOT NULL DEFAULT 'active' CHECK (result_state = 'active'),
  created_at TEXT,
  reviewed_at TEXT,
  review_notes TEXT NOT NULL DEFAULT '',

  sample_image_asset_id TEXT NOT NULL,
  blank_id TEXT NOT NULL,
  orientation_rots INTEGER NOT NULL CHECK (orientation_rots IN (0, 1, 2, 3)),
  source_image TEXT,
  cr2_source TEXT CHECK (cr2_source IS NULL OR cr2_source IN ('images', 'inbox')),

  strip_location_source TEXT CHECK (
    strip_location_source IS NULL OR strip_location_source IN (
      'automatic_detected_contour_min_area_rect',
      'automatic_preview_contour_fallback_not_full_frame',
      'manual_corner_selection'
    )
  ),
  coordinate_space TEXT CHECK (
    coordinate_space IS NULL OR coordinate_space IN (
      'automatic_full_image_after_source_and_open_side_rotation',
      'manual_full_image_after_source_rotation_before_open_side_rotation'
    )
  ),
  corner_order TEXT CHECK (corner_order IS NULL OR corner_order = 'tl,tr,br,bl'),
  source_or_preview_asset_id TEXT,
  preview_width INTEGER CHECK (preview_width IS NULL OR preview_width > 0),
  preview_height INTEGER CHECK (preview_height IS NULL OR preview_height > 0),
  preview_scale REAL CHECK (preview_scale IS NULL OR preview_scale > 0),
  image_rotation_used INTEGER CHECK (image_rotation_used IS NULL OR image_rotation_used IN (0, 1, 2, 3)),

  i0_r_linear REAL,
  i0_g_linear REAL,
  i0_b_linear REAL,

  confidence REAL NOT NULL DEFAULT 0.0,
  detection_strategy TEXT NOT NULL DEFAULT '',
  appearance_order_correlation REAL,
  appearance_order_correlation_state TEXT NOT NULL DEFAULT 'not_computed' CHECK (
    appearance_order_correlation_state IN ('finite', 'nan', 'not_computed')
  ),
  appearance_orientation_flipped INTEGER CHECK (
    appearance_orientation_flipped IS NULL OR appearance_orientation_flipped IN (0, 1)
  ),
  appearance_error TEXT,
  decode_environment_json TEXT,
  skew_angle_deg REAL,
  contour_found INTEGER CHECK (contour_found IS NULL OR contour_found IN (0, 1)),

  CHECK (method = 'legacy_backfill' OR strip_location_source IS NOT NULL),
  CHECK (method != 'legacy_backfill' OR strip_location_source IS NULL),
  CHECK (
    (
      appearance_order_correlation_state = 'finite'
      AND appearance_order_correlation IS NOT NULL
    )
    OR
    (
      appearance_order_correlation_state IN ('nan', 'not_computed')
      AND appearance_order_correlation IS NULL
    )
  ),
  FOREIGN KEY (sample_id, geometry_id) REFERENCES samples(sample_id, geometry_id) ON DELETE CASCADE,
  FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT,
  FOREIGN KEY (sample_image_asset_id) REFERENCES image_assets(image_asset_id) ON DELETE RESTRICT,
  FOREIGN KEY (blank_id) REFERENCES registered_blanks(blank_id) ON DELETE RESTRICT
);

CREATE INDEX idx_extraction_results_sample ON extraction_results(sample_id);
CREATE INDEX idx_extraction_results_review_state ON extraction_results(review_state);
CREATE INDEX idx_extraction_results_method ON extraction_results(method);

CREATE TABLE extraction_result_quad_points (
  extraction_result_id TEXT NOT NULL,
  point_role TEXT NOT NULL CHECK (point_role IN ('tl', 'tr', 'br', 'bl')),
  x REAL NOT NULL,
  y REAL NOT NULL,
  PRIMARY KEY (extraction_result_id, point_role),
  FOREIGN KEY (extraction_result_id) REFERENCES extraction_results(extraction_result_id) ON DELETE CASCADE
);

CREATE TABLE extraction_result_swatches (
  extraction_result_id TEXT NOT NULL,
  swatch_index INTEGER NOT NULL CHECK (swatch_index >= 0),
  nominal_thickness_mm REAL NOT NULL CHECK (nominal_thickness_mm >= 0),
  geometry_variable_thickness_mm REAL CHECK (
    geometry_variable_thickness_mm IS NULL OR geometry_variable_thickness_mm >= 0
  ),

  transmission_r_linear REAL NOT NULL,
  transmission_g_linear REAL NOT NULL,
  transmission_b_linear REAL NOT NULL,

  display_hex TEXT NOT NULL,
  display_r INTEGER NOT NULL CHECK (display_r BETWEEN 0 AND 255),
  display_g INTEGER NOT NULL CHECK (display_g BETWEEN 0 AND 255),
  display_b INTEGER NOT NULL CHECK (display_b BETWEEN 0 AND 255),

  appearance_source TEXT,
  appearance_jpeg_r REAL,
  appearance_jpeg_g REAL,
  appearance_jpeg_b REAL,
  appearance_box_x0 INTEGER,
  appearance_box_y0 INTEGER,
  appearance_box_x1 INTEGER,
  appearance_box_y1 INTEGER,

  fit_excluded_snapshot INTEGER NOT NULL DEFAULT 0 CHECK (fit_excluded_snapshot IN (0, 1)),
  fit_exclusion_reason_snapshot TEXT NOT NULL DEFAULT '',

  PRIMARY KEY (extraction_result_id, swatch_index),
  CHECK (display_hex GLOB '#[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]'),
  CHECK (
    (
      appearance_source IS NULL
      AND appearance_jpeg_r IS NULL
      AND appearance_jpeg_g IS NULL
      AND appearance_jpeg_b IS NULL
    )
    OR
    (
      appearance_source IS NOT NULL
      AND appearance_jpeg_r IS NOT NULL
      AND appearance_jpeg_g IS NOT NULL
      AND appearance_jpeg_b IS NOT NULL
    )
  ),
  FOREIGN KEY (extraction_result_id) REFERENCES extraction_results(extraction_result_id) ON DELETE CASCADE
);

CREATE INDEX idx_extraction_result_swatches_index ON extraction_result_swatches(swatch_index);

-- ---------------------------------------------------------------------------
-- Geometry bundles
-- ---------------------------------------------------------------------------

CREATE TABLE geometry_bundles (
  geometry_bundle_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE geometry_bundle_members (
  geometry_bundle_member_id TEXT NOT NULL,
  geometry_bundle_id TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 0),
  geometry_id TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (geometry_bundle_member_id),
  UNIQUE (geometry_bundle_id, geometry_bundle_member_id),
  UNIQUE (geometry_bundle_id, geometry_id),
  UNIQUE (geometry_bundle_id, position),
  FOREIGN KEY (geometry_bundle_id) REFERENCES geometry_bundles(geometry_bundle_id) ON DELETE CASCADE,
  FOREIGN KEY (geometry_id) REFERENCES calibration_strip_geometries(geometry_id) ON DELETE RESTRICT
);

CREATE TABLE geometry_bundle_material_slots (
  geometry_bundle_id TEXT NOT NULL,
  material_slot_id TEXT NOT NULL,
  position INTEGER NOT NULL CHECK (position >= 0),
  key TEXT NOT NULL,
  label TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (geometry_bundle_id, material_slot_id),
  UNIQUE (geometry_bundle_id, position),
  UNIQUE (geometry_bundle_id, key),
  FOREIGN KEY (geometry_bundle_id) REFERENCES geometry_bundles(geometry_bundle_id) ON DELETE CASCADE
);

CREATE TABLE geometry_bundle_role_slot_mappings (
  geometry_bundle_id TEXT NOT NULL,
  geometry_bundle_member_id TEXT NOT NULL,
  geometry_role_id TEXT NOT NULL,
  material_slot_id TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (geometry_bundle_member_id, geometry_role_id),
  FOREIGN KEY (geometry_bundle_id, geometry_bundle_member_id)
    REFERENCES geometry_bundle_members(geometry_bundle_id, geometry_bundle_member_id) ON DELETE CASCADE,
  FOREIGN KEY (geometry_bundle_id, material_slot_id)
    REFERENCES geometry_bundle_material_slots(geometry_bundle_id, material_slot_id) ON DELETE CASCADE,
  FOREIGN KEY (geometry_role_id) REFERENCES geometry_roles(geometry_role_id) ON DELETE RESTRICT
);

-- ---------------------------------------------------------------------------
-- Model fits and generated artifacts
-- ---------------------------------------------------------------------------

CREATE TABLE model_fits (
  model_fit_id TEXT PRIMARY KEY,
  model_kind TEXT NOT NULL CHECK (model_kind IN ('camera_transform', 'legacy_spline', 'photo_stack_v2')),
  model_label TEXT NOT NULL DEFAULT '',
  currentness_state TEXT NOT NULL CHECK (currentness_state IN ('current', 'stale')),
  stale_reason TEXT,
  generated_at TEXT NOT NULL,
  artifact_root_rel_path TEXT,
  input_fingerprint TEXT,
  output_fingerprint TEXT,
  code_version TEXT,
  output_exists_at_last_check INTEGER NOT NULL DEFAULT 1 CHECK (output_exists_at_last_check IN (0, 1)),
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE model_fit_contributors (
  model_fit_id TEXT NOT NULL,
  sample_id TEXT NOT NULL,
  extraction_result_id TEXT,
  included_swatch_count INTEGER NOT NULL CHECK (included_swatch_count >= 0),
  PRIMARY KEY (model_fit_id, sample_id),
  FOREIGN KEY (model_fit_id) REFERENCES model_fits(model_fit_id) ON DELETE CASCADE,
  FOREIGN KEY (sample_id) REFERENCES samples(sample_id) ON DELETE CASCADE,
  FOREIGN KEY (extraction_result_id) REFERENCES extraction_results(extraction_result_id) ON DELETE SET NULL
);

CREATE TABLE model_artifacts (
  model_artifact_id TEXT PRIMARY KEY,
  model_fit_id TEXT NOT NULL,
  artifact_kind TEXT NOT NULL,
  artifact_rel_path TEXT NOT NULL,
  content_sha256 TEXT,
  exists_at_last_check INTEGER NOT NULL DEFAULT 1 CHECK (exists_at_last_check IN (0, 1)),
  FOREIGN KEY (model_fit_id) REFERENCES model_fits(model_fit_id) ON DELETE CASCADE
);

CREATE INDEX idx_model_fits_kind_state ON model_fits(model_kind, currentness_state);
CREATE INDEX idx_model_fit_contributors_sample ON model_fit_contributors(sample_id);

-- ---------------------------------------------------------------------------
-- Migration audit tables. These are not application source-truth tables.
-- ---------------------------------------------------------------------------

CREATE TABLE migration_trace_records (
  trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_table TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT,
  source_path TEXT,
  source_payload_json TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_migration_trace_entity ON migration_trace_records(entity_table, entity_id);

CREATE TABLE migration_regeneration_policy (
  artifact_group TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK (record_count >= 0),
  notes TEXT NOT NULL DEFAULT ''
);
