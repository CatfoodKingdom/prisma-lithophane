export const CALIBRATION_CONSTANTS = {
  modeConfig: {
    logbook: { subtabs: [] },
    imageProcessing: {
      subtabs: [
        { id: "associate", label: "Assign Images" },
        { id: "queue", label: "Processing Queue" },
      ],
    },
    filaments: { subtabs: [] },
    geometries: { subtabs: [] },
    profiles: {
      subtabs: [
        { id: "overview", label: "Overview" },
        { id: "samples", label: "Samples" },
        { id: "filaments", label: "Filaments" },
      ],
    },
  },
  MODELING_REVIEW_PAGE_SIZE: 1000,
  MODELING_DETAIL_SETTINGS_KEY: "prisma.modeling.detailSettings.v1",
  tableKicker: { set textContent(_) {} },
  tableTitle: { set textContent(_) {}, set innerHTML(_) {} },
  SAMPLE_INSPECT_EXPANDED_SESSION_KEY:
    "prisma.calibration.sampleInspectExpanded",
  LINKED_SAMPLE_DRAWER_GAP: 14,
  LINKED_SAMPLE_DRAWER_MAX_WIDTH: 430,
  LINKED_SAMPLE_DRAWER_MIN_WIDTH: 360,
  LINKED_SAMPLE_DRAWER_MIN_LEFT_MARGIN: 12,
  BUNDLE_MAPPING_SLOT_COLORS: [
    "#8aa0a6",
    "#c9956a",
    "#8f9f73",
    "#b47a84",
    "#7f91bd",
    "#a783aa",
    "#6f9d91",
    "#b2a15f",
    "#9d8874",
    "#7897a0",
    "#a8796b",
    "#7f8f7b",
  ],
  MODEL_OVERVIEW_ORDER: [
    { key: "legacy_spline", label: "Color Model v1" },
    { key: "photo_stack_v2", label: "Color Model v2" },
    { key: "camera_transform", label: "Camera Transform" },
  ],
  MODEL_REVIEW_OKLAB_ERROR_SCALE_MAX: 0.15,
  MODEL_REVIEW_OKLAB_ERROR_LANDMARKS: [
    { value: 0.02, label: "JND" },
    { value: 0.05, label: "Noticeable" },
    { value: 0.1, label: "Large" },
  ],
  CORNER_LABELS: ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"],
  CORNER_COLORS: ["#e53935", "#fb8c00", "#43a047", "#1e88e5"],
};
