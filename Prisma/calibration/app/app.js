function assertPolledJobIdentity(status, expectedJobId) {
  const expected = String(expectedJobId || "");
  const actual = String(status?.job_id || "");
  if (!expected || actual !== expected) {
    const error = new Error(
      actual
        ? `Polled job identity mismatch: expected ${expected || "(missing)"}, received ${actual}.`
        : `Polled job response omitted the expected job id ${expected || "(missing)"}.`,
    );
    error.name = "JobPollingIdentityError";
    error.expectedJobId = expected;
    error.actualJobId = actual;
    throw error;
  }
}

async function pollJobUntilTerminal({
  jobId,
  fetchStatus,
  isTerminal,
  onStatus = () => {},
  shouldContinue = () => true,
  onTransientError = () => {},
  onRecovered = () => {},
  intervalMs = 500,
  maxRetryDelayMs = 4000,
  wait = (delayMs) => new Promise((resolve) => setTimeout(resolve, delayMs)),
}) {
  const expectedJobId = String(jobId || "");
  if (!expectedJobId) throw new Error("Cannot poll a job without a job id.");
  let consecutiveFailures = 0;
  let sequence = 0;

  while (shouldContinue()) {
    let status;
    try {
      status = await fetchStatus(expectedJobId);
    } catch (error) {
      if (!shouldContinue()) return null;
      consecutiveFailures += 1;
      const retryDelayMs = Math.min(
        maxRetryDelayMs,
        intervalMs * (2 ** Math.min(consecutiveFailures - 1, 3)),
      );
      onTransientError(error, { consecutiveFailures, retryDelayMs });
      await wait(retryDelayMs);
      continue;
    }

    if (!shouldContinue()) return null;
    assertPolledJobIdentity(status, expectedJobId);
    sequence += 1;
    if (consecutiveFailures > 0) {
      onRecovered({ consecutiveFailures, sequence });
      consecutiveFailures = 0;
    }
    await onStatus(status, { sequence });
    if (isTerminal(status)) return status;
    await wait(intervalMs);
  }
  return null;
}


const data = {
  summary: {},
  filaments: [],
  samples: [],
  steps: [],
  processed_samples: [],
  images: [],
  image_overrides: {},
  model_status: {},
};

// Track the current data source for the UI
let _dataSource = 'api';
let _refreshPromise = null;

// Server config — loaded asynchronously by loadServerConfig() below

const modeConfig = {
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
};

let currentMode = "logbook";
let currentSubtab = "";
let sortState = { key: "sample_id", direction: "asc" };
let profilesState = {
  selectedFilamentId: null,
  profileCache: {},       // filament_id -> profile data from API
  curveCache: {},         // filament_id -> dense curve data from /curve endpoint
  swatchCache: {},        // filament_id -> swatch error data from API
  errorCache: {},         // filament_id -> per-swatch dE bar data from /errors endpoint
  loadingProfile: false,
  crosscalSortKey: "dE",
  crosscalSortDir: "desc",
  detailSection: "chart", // "chart" | "swatches" | "data"
};
let fitModelsWorkflowLaunchBusy = false;
let modelingState = {
  overview: null,
  samples: null,
  filaments: null,
  detailSamplePayload: null,
  detailFilamentPayload: null,
  detailRequestSeq: 0,
  loadingTab: null,
  error: "",
  samplesFilter: "all",
  samplesSort: "sample_id",
  samplesSortDir: "asc",
  samplesFilamentIds: [],
  filamentsSort: "name",
  filamentsSortDir: "asc",
  sampleDetailReturnFilamentId: null,
  sampleDetailReturnFilamentPayload: null,
  sampleDetailReturnSampleContext: null,
};
const MODELING_REVIEW_PAGE_SIZE = 1000;
const MODELING_DETAIL_SETTINGS_KEY = "prisma.modeling.detailSettings.v1";
let geometryDetailReturnSampleContext = null;

function loadModelingDetailSettings() {
  const defaults = { includeCorrections: true, domain: "appearance" };
  try {
    const raw = window.sessionStorage?.getItem(MODELING_DETAIL_SETTINGS_KEY);
    if (!raw) return defaults;
    const parsed = JSON.parse(raw);
    return {
      includeCorrections: parsed.includeCorrections !== false,
      domain: parsed.domain === "transmission" ? "transmission" : "appearance",
    };
  } catch {
    return defaults;
  }
}

let modelingDetailSettings = loadModelingDetailSettings();

function persistModelingDetailSettings() {
  try {
    window.sessionStorage?.setItem(MODELING_DETAIL_SETTINGS_KEY, JSON.stringify(modelingDetailSettings));
  } catch {
    // Session persistence is a convenience; rendering should continue if unavailable.
  }
}
let profileFitJobState = {
  running: false,
  jobId: null,
  status: null,
  lastResult: null,
  error: null,
};
let modelFittingState = {
  selectedFilamentId: null,
  selectedSampleId: null,
  predictionCache: {},
  isFittingAll: false,
  renderSeq: 0,
};
let photoStackModelState = {
  isFitting: false,
  jobId: null,
  status: null,
  latest: null,
  candidate: null,
  predictions: null,
  loadingCandidate: false,
  requestedInitialLoad: false,
  error: null,
  search: "",
  evidenceClass: "all",
};
let cameraTransformState = {
  isBuilding: false,
  jobId: null,
  status: null,
  current: null,
  requestedInitialLoad: false,
  error: null,
};
let logbookFilter = "all";
let selectedRecord = { kind: null, id: null };

// ── Confirm-to-act helper ─────────────────────────────────────────────────────
// First click arms the button (changes text/style); second click executes.
// Auto-resets after `timeout` ms if not confirmed.
function bindConfirmAction(el, { onConfirm, armedText = "confirm?", timeout = 3000 } = {}) {
  let armed = false;
  let timer = null;
  const originalHtml = el.innerHTML;
  const originalClass = el.className;

  function reset() {
    armed = false;
    el.innerHTML = originalHtml;
    el.className = originalClass;
    clearTimeout(timer);
  }

  el.addEventListener("click", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    if (el.disabled) return;
    if (!armed) {
      armed = true;
      el.textContent = armedText;
      el.classList.add("is-armed");
      timer = setTimeout(reset, timeout);
    } else {
      reset();
      if (onConfirm) await onConfirm();
    }
  });
}

// ── Server config cache ──────────────────────────────────────────────────────
let _serverConfig = null;
async function loadServerConfig() {
  try {
    _serverConfig = await fetchConfig();
    data._stepLibraryFullPath = _serverConfig.step_export_path || _serverConfig.step_library_path || "";
  } catch (_) { /* static mode */ }
}

// ── Import view state ─────────────────────────────────────────────────────────
let importState = {
  images: [],              // from /api/images
  blanks: [],              // from /api/blanks
  selectedImage: null,     // filename string
  selectedBlank: null,     // blank filename string
  selectedSample: null,    // sample_id string
  assignedCount: 0,        // session counter
  loading: false,
  loaded: false,
  loadingMessage: "",
  imageAssignments: {},    // filename -> sample_id (derived from samples)
  hideReady: false,        // toggle to hide fully assigned samples
};
let stepBuilderState = {
  values: ["0.20", "0.28", "0.36", "0.44", "0.52", "0.60", "0.68", "0.76"],
  fixedLayers: [],
  layerRoles: [],
  nextLayerRoleId: 1,
  alias: "",
  bundle: "",
};
let stepEditorState = {
  stepId: null,
  isEditing: false,
  draftAlias: "",
  draftBundle: "",
  confirmDelete: false,
  deleteMessage: "",
  deleteMessageKind: "",
};

let processingState = {
  batchRunning: false,
  batchProgress: null,
  singleRunningSampleIds: new Set(),
};
let maintenanceState = {
  operations: null,
  loading: false,
  error: "",
  loadPromise: null,
};

let maintenanceCacheBust = {
  version: 0,
  previews: new Map(),
  blankPreviews: new Map(),
  sampleThumbnails: new Map(),
  allPreviews: 0,
  allSampleThumbnails: 0,
};

const summaryRail = document.getElementById("summaryRail") || { set innerHTML(_) {} };
const modeSwitch = document.getElementById("modeSwitch");
const subtabContainer = document.getElementById("subtabContainer");
const subtabRow = document.getElementById("subtabRow");
const workspaceRoot = document.querySelector(".workspace");
const statusSummary = document.getElementById("statusSummary") || { set innerHTML(_) {} };
const tableKicker = { set textContent(_) {} };  // removed from DOM
const tableTitle = { set textContent(_) {}, set innerHTML(_) {} };  // removed from DOM
const tableSummary = document.getElementById("tableSummary");
const tableToolbar = document.getElementById("tableToolbar");
const tableContainer = document.getElementById("tableContainer");
const detailHeading = document.getElementById("detailHeading");
const detailSidebar = document.getElementById("detailSidebar");
const detailActionArea = document.getElementById("detailActionArea");
const detailWindowArea = document.getElementById("detailWindowArea");
const recordDrawer = document.getElementById("recordDrawer");
const closeRecordDrawer = document.getElementById("closeRecordDrawer");
const drawerStatusPill = document.getElementById("drawerStatusPill");
const linkedSampleDrawer = document.getElementById("linkedSampleDrawer");
const linkedSampleHeading = document.getElementById("linkedSampleHeading");
const linkedSampleStatusPill = document.getElementById("linkedSampleStatusPill");
const linkedSampleActionArea = document.getElementById("linkedSampleActionArea");
const linkedSampleWindowArea = document.getElementById("linkedSampleWindowArea");
const linkedSampleSidebar = document.getElementById("linkedSampleSidebar");
const closeLinkedSampleDrawerBtn = document.getElementById("closeLinkedSampleDrawer");
const stepBuilderDrawer = document.getElementById("stepBuilderDrawer");
const stepBuilderBody = document.getElementById("stepBuilderBody");
const stepBundleOptions = document.getElementById("stepBundleOptions");
const bundleMgmtDrawer = document.getElementById("bundleMgmtDrawer");
const bundleMgmtBody = document.getElementById("bundleMgmtBody");
const imageLightboxOverlay = document.getElementById("imageLightboxOverlay");
const imageLightboxTitle = document.getElementById("imageLightboxTitle");
const imageLightboxImg = document.getElementById("imageLightboxImg");
const closeImageLightbox = document.getElementById("closeImageLightbox");

function enableKeyboardNavigationMode() {
  document.body?.classList.add("using-keyboard-nav");
}

function disableKeyboardNavigationMode() {
  document.body?.classList.remove("using-keyboard-nav");
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const keyboardNavKeys = new Set([
    "Tab",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "Home",
    "End",
  ]);
  if (keyboardNavKeys.has(e.key)) {
    enableKeyboardNavigationMode();
  }
});

document.addEventListener("pointerdown", disableKeyboardNavigationMode);

// Step builder DOM helpers — elements are dynamically created when drawer opens
function _sbEl(id) { return document.getElementById(id); }

// filament drawer state
let _filamentDrawerMode = null; // null | "view" | "edit" | "create"
let _filamentDrawerData = null; // filament object for view/edit, null for create
const SAMPLE_INSPECT_EXPANDED_SESSION_KEY = "prisma.calibration.sampleInspectExpanded";

function readSampleInspectExpandedPreference() {
  try {
    return window.sessionStorage?.getItem(SAMPLE_INSPECT_EXPANDED_SESSION_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function setSampleInspectExpandedPreference(expanded) {
  _sampleInspectExpanded = !!expanded;
  try {
    window.sessionStorage?.setItem(SAMPLE_INSPECT_EXPANDED_SESSION_KEY, _sampleInspectExpanded ? "1" : "0");
  } catch (_) {
    // Session storage can be unavailable in hardened browser contexts.
  }
}

let _sampleInspectExpanded = readSampleInspectExpandedPreference();
let _linkedSampleDrawerState = {
  sampleId: null,
  returnFocusEl: null,
};

const LINKED_SAMPLE_DRAWER_GAP = 14;
const LINKED_SAMPLE_DRAWER_MAX_WIDTH = 430;
const LINKED_SAMPLE_DRAWER_MIN_WIDTH = 360;
const LINKED_SAMPLE_DRAWER_MIN_LEFT_MARGIN = 12;

const stepMetadata = Object.fromEntries(
  (data.steps || []).map((step) => [
    step.step_id || step.file_name,
    {
      alias: step.alias || "",
      bundle: step.bundle || "",
      deleted: false,
    },
  ])
);

function stepRecordByRef(stepRef) {
  if (!stepRef) return null;
  return (data.steps || []).find((step) =>
    step.step_id === stepRef ||
    step.file_name === stepRef ||
    step.artifact_filename === stepRef ||
    step.full_path === stepRef
  ) || null;
}

function stepIdFromRef(stepRef) {
  return stepRecordByRef(stepRef)?.step_id || stepRef || "";
}

function stepFileNameFromRef(stepRef) {
  const step = stepRecordByRef(stepRef);
  if (step?.file_name) return step.file_name;
  const normalized = String(stepRef || "").replace(/\\/g, "/");
  return normalized.split("/").pop() || "";
}

function sampleStepId(exp) {
  return exp?.step_id || stepIdFromRef(exp?.step_file || "");
}

function sampleStepFileName(exp) {
  return stepFileNameFromRef(sampleStepId(exp) || exp?.step_file || "");
}

function sampleStatusMeta(exp) {
  const processingStatus = exp._processing_status || exp.processing_status || (exp.processed ? "processed" : "unassigned");
  const assignedImage = exp._assigned_image || exp.assigned_image || null;
  const assignedBlankId = exp._assigned_blank_id || exp.assigned_blank_id || null;
  const orientation = exp._orientation_rots ?? exp.orientation_rots;
  const reviewAccepted = Boolean(exp._review_accepted ?? exp.review_accepted);
  const hasAnySetup = Boolean(assignedImage || assignedBlankId || orientation != null);
  const isReady = Boolean(assignedImage && assignedBlankId && orientation != null);

  if (processingStatus === "failed") return { label: "failed", cls: "failed" };
  if (processingStatus === "flagged" || exp._flag_reason || exp.flag_reason) return { label: "flagged", cls: "flagged" };
  if (processingStatus === "processed" || exp.processed) {
    return reviewAccepted
      ? { label: "accepted", cls: "accepted" }
      : { label: "processed", cls: "processed" };
  }
  if (isReady) return { label: "ready", cls: "ready" };
  if (processingStatus === "assigned" || hasAnySetup) return { label: "incomplete", cls: "incomplete" };
  return { label: "unassigned", cls: "unassigned" };
}

function sampleHasMeasurementOutput(exp = {}) {
  const workflowStatus = String(exp._processing_status || exp.processing_status || "").toLowerCase();
  return Boolean(exp.processed) || workflowStatus === "processed" || workflowStatus === "flagged";
}

function statusForSample(exp) {
  return sampleStatusMeta(exp).label;
}

function filamentStatusMeta(fil) {
  if (fil.has_profile) return { label: "profiled", cls: "profiled" };
  if (fil.has_strips || (fil.processed_count || 0) > 0) return { label: "strips only", cls: "strips_only" };
  return { label: "pending", cls: "pending" };
}

function profilePillMeta(filamentId) {
  const fil = data.filaments.find((f) => f.filament_id === filamentId);
  if (!fil) return null;
  const cached = profilesState.profileCache[filamentId];
  const hasProfile = fil.has_profile;
  const hasStrips = fil.has_strips || fil.processed_count > 0;
  const isStale = cached?.stale === true;
  const isActive = cached?.active !== false && hasProfile;

  if (isStale) return { label: "stale", cls: "stale" };
  if (hasProfile && isActive) return { label: "active", cls: "active" };
  if (hasProfile) return { label: "fitted", cls: "fitted" };
  if (hasStrips) return { label: "strips only", cls: "strips_only" };
  return null;
}

function sampleLabel(exp) {
  return `${exp.variable_color_name} ${exp.range_label} @${Number(exp.layer_height_mm).toFixed(2)}`;
}

function filamentUsageLabels(exp, filamentId) {
  return sampleFilamentRoleLines(exp, { filterFilamentId: filamentId }).map((line) => line.layerLabel);
}

function filamentUsedBySamples(filamentId) {
  return (data.samples || [])
    .map((exp) => ({ exp, usageLabels: filamentUsageLabels(exp, filamentId) }))
    .filter((entry) => entry.usageLabels.length > 0);
}

function normalizeUsageLabel(label) {
  const text = String(label || "").trim();
  const lrMatch = text.match(/^LR_(\d+)\s+(fixed|variable)$/i);
  if (lrMatch) return `LR_${lrMatch[1].padStart(2, "0")} ${lrMatch[2].toLowerCase()}`;
  const raw = text.toLowerCase();
  if (!raw) return "variable";
  if (raw.startsWith("variable")) return "variable";
  const fixedMatch = raw.match(/fixed layer\s+(\d+)/);
  if (fixedMatch) return `fixed ${fixedMatch[1]}`;
  return raw;
}

function compactLayerRoleToken(label, roleIndex = 0, fallback = "LR_?") {
  const text = String(label || "").trim();
  const lrMatch = text.match(/^LR[_\s-]*(\d+)/i);
  if (lrMatch) return `LR_${lrMatch[1].padStart(2, "0")}`;
  const layerRoleMatch = text.match(/(?:layer\s*)?role\s*0*(\d+)/i);
  if (layerRoleMatch) return `LR_${layerRoleMatch[1].padStart(2, "0")}`;
  const fixedMatch = text.match(/^fixed\s+(\d+)$/i);
  if (fixedMatch) return `LR_${fixedMatch[1].padStart(2, "0")}`;
  if (roleIndex > 0) return `LR_${String(roleIndex).padStart(2, "0")}`;
  return text || fallback;
}

function sampleFilamentDisplayName(fid, fallbackName = "") {
  const fil = filamentMeta(fid);
  return fil?.display_name
    || [fil?.manufacturer || "", fil?.color_name || ""].filter(Boolean).join(" ")
    || fallbackName
    || fid
    || "—";
}

function sampleFilamentColorName(fid, fallbackName = "") {
  const fil = filamentMeta(fid);
  return fil?.color_name
    || fallbackName
    || fid
    || "—";
}

function sampleFilamentBrandName(fid, fallbackBrand = "") {
  const fil = filamentMeta(fid);
  return fil?.manufacturer
    || fallbackBrand
    || "—";
}

function formatLayerRoleLabel(roleOrIndex, kind = "fixed") {
  const role = typeof roleOrIndex === "object" ? roleOrIndex : { role_index: roleOrIndex, role_kind: kind };
  const roleIndex = Number(role?.role_index || 0);
  const roleKind = (role?.role_kind || kind || "").toLowerCase() === "variable" ? "variable" : "fixed";
  const fallback = roleIndex > 0 ? `LR_${String(roleIndex).padStart(2, "0")}` : (roleKind === "variable" ? "variable" : "LR_?");
  const roleLabel = compactLayerRoleToken(role?.role_label, roleIndex, fallback);
  if (roleLabel.toLowerCase() === roleKind) return roleKind;
  return `${roleLabel} ${roleKind}`;
}

function sampleRoleRows(exp, { order = "top-to-bottom" } = {}) {
  const roles = (exp?.roles || [])
    .filter((role) => role && role.role_kind)
    .map((role) => ({
      ...role,
      role_index: Number(role.role_index || 0),
      role_kind: String(role.role_kind || "").toLowerCase(),
      filament_id: role.filament_id || "",
    }));

  if (roles.length > 0) {
    return roles.sort((a, b) => {
      const delta = Number(a.role_index || 0) - Number(b.role_index || 0);
      return order === "bottom-to-top" ? delta : -delta;
    });
  }
  if (isStructuredGeometryBackend()) return [];

  // JSON rollback compatibility only. SQLite samples are expected to carry roles.
  let variableRow = null;
  if (exp?.variable_filament_id) {
    variableRow = {
      role_index: 0,
      role_label: "Variable",
      role_kind: "variable",
      filament_id: exp.variable_filament_id,
      legacy_label: "variable",
    };
  }
  const fixedRows = (exp?.fixed_filament_ids || []).map((fid, index) => ({
    role_index: index + 1,
    role_label: `Fixed ${index + 1}`,
    role_kind: "fixed",
    filament_id: fid,
    fixed_thickness_mm: (exp.fixed_thicknesses_mm || [])[index],
    legacy_label: `fixed ${index + 1}`,
  }));
  if (order === "bottom-to-top") {
    return [...fixedRows, ...(variableRow ? [variableRow] : [])];
  }
  return [...(variableRow ? [variableRow] : []), ...fixedRows.reverse()];
}

function sampleFilamentRoleLines(exp, options = {}) {
  const filterFilamentId = options.filterFilamentId || null;
  return sampleRoleRows(exp, { order: options.order || "top-to-bottom" })
    .filter((role) => !filterFilamentId || role.filament_id === filterFilamentId)
    .map((role) => {
      const fil = filamentMeta(role.filament_id);
      const fallbackColorName = role.role_kind === "variable" ? exp?.variable_color_name || "" : "";
      const fallbackBrandName = role.role_kind === "variable" ? exp?.manufacturer || "" : "";
      const fallbackDisplayName = [fallbackBrandName, fallbackColorName].filter(Boolean).join(" ");
      const hex = fil?.hex || (role.role_kind === "variable" ? exp?.variable_hex : null) || "#cccccc";
      return {
        roleIndex: Number(role.role_index || 0),
        roleKind: role.role_kind,
        layerLabel: formatLayerRoleLabel(role),
        thicknessMm: role.role_kind === "fixed" && role.fixed_thickness_mm != null
          ? Number(role.fixed_thickness_mm)
          : null,
        filamentId: role.filament_id,
        hex,
        name: sampleFilamentDisplayName(role.filament_id, fallbackDisplayName),
        colorName: sampleFilamentColorName(role.filament_id, fallbackColorName),
        brand: sampleFilamentBrandName(role.filament_id, fallbackBrandName),
        excludeFromModel: !!fil?.exclude_from_model,
      };
    });
}

function sampleMaterialLines(exp, options = {}) {
  return sampleFilamentRoleLines(exp, options);
}

function sampleFilamentStackSortText(exp) {
  const lines = sampleFilamentRoleLines(exp);
  if (!lines.length) return exp?.variable_color_name || exp?.variable_filament_id || "";
  return lines.map((line) => line.colorName || line.filamentId || "").join(" ");
}

function sampleBrandStackSortText(exp) {
  const lines = sampleFilamentRoleLines(exp);
  if (!lines.length) return exp?.manufacturer || "";
  return lines.map((line) => line.brand || "").join(" ");
}

function renderWindowCloseButton({
  id = "",
  className = "",
  label = "Close dialog",
  title = "Close dialog",
  disabled = false,
  attributes = "",
} = {}) {
  const idAttr = id ? ` id="${escapeHtml(id)}"` : "";
  const extraClass = className ? ` ${escapeHtml(className)}` : "";
  const disabledAttr = disabled ? " disabled" : "";
  const extraAttrs = attributes ? ` ${attributes}` : "";
  return `
    <button class="close-button small window-close-button${extraClass}" type="button"${idAttr} aria-label="${escapeHtml(label)}" title="${escapeHtml(title)}"${disabledAttr}${extraAttrs}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <path d="M2 2 L10 10 M10 2 L2 10"></path>
      </svg>
    </button>
  `;
}

function renderWindowControls({
  closeButtonHtml = "",
  extraControlsHtml = "",
  className = "",
} = {}) {
  if (!closeButtonHtml && !extraControlsHtml) return "";
  const extraClass = className ? ` ${escapeHtml(className)}` : "";
  return `
    <div class="drawer-window-controls dialog-window-controls${extraClass}">
      ${extraControlsHtml || ""}
      ${closeButtonHtml || ""}
    </div>
  `;
}

function renderDialogHeader({
  title,
  titleId = "",
  subtitle = "",
  actionsHtml = "",
  closeButtonHtml = "",
  extraControlsHtml = "",
  headerClass = "",
  titleClass = "",
} = {}) {
  const titleIdAttr = titleId ? ` id="${escapeHtml(titleId)}"` : "";
  const headerClassAttr = headerClass ? ` ${escapeHtml(headerClass)}` : "";
  const titleClassAttr = titleClass ? ` class="${escapeHtml(titleClass)}"` : "";
  const actions = actionsHtml ? `<div class="dialog-header-actions">${actionsHtml}</div>` : "";
  const controls = renderWindowControls({ closeButtonHtml, extraControlsHtml });
  const toolbar = actions || controls ? `<div class="dialog-header-toolbar">${actions}${controls}</div>` : "";
  return `
    <div class="info-dialog-header${headerClassAttr}">
      <div class="info-dialog-title-block">
        <h3${titleIdAttr}${titleClassAttr}>${escapeHtml(title || "")}</h3>
        ${subtitle ? `<div class="info-dialog-subtitle">${escapeHtml(subtitle)}</div>` : ""}
      </div>
      ${toolbar}
    </div>
  `;
}

function renderLogbookFilamentCell(exp) {
  const lines = sampleFilamentRoleLines(exp);
  if (!lines.length) {
    const fallbackFilament = filamentMeta(exp.variable_filament_id || "");
    return `
      <div class="filament-cell logbook-filament-stack">
        <div class="logbook-filament-layer">
          <span class="color-chip" style="background:${_escAttr(exp.variable_hex || "#dddddd")}"></span>
          <span class="logbook-filament-name">${_escHtml(exp.variable_color_name || exp.variable_filament_id || "—")}</span>
          ${fallbackFilament?.exclude_from_model ? `<span class="status-pill logbook-fit-pill planned">Excluded</span>` : ""}
        </div>
      </div>
    `;
  }
  return `
    <div class="filament-cell logbook-filament-stack" title="${_escAttr(lines.map((line) => `${line.layerLabel}: ${line.name || line.filamentId || "—"}`).join(" | "))}">
      ${lines.map((line) => `
        <div class="logbook-filament-layer">
          <span class="color-chip" style="background:${_escAttr(line.hex || "#cccccc")}"></span>
          <span class="logbook-filament-name">${_escHtml(line.colorName || line.filamentId || "—")}</span>
          ${line.excludeFromModel ? `<span class="status-pill logbook-fit-pill planned">Excluded</span>` : ""}
        </div>
      `).join("")}
    </div>
  `;
}

function renderLogbookBrandCell(exp) {
  const lines = sampleFilamentRoleLines(exp);
  if (!lines.length) return _escHtml(exp.manufacturer || "—");
  return `
    <div class="logbook-brand-stack" title="${_escAttr(lines.map((line) => `${line.layerLabel}: ${line.brand || "—"}`).join(" | "))}">
      ${lines.map((line) => `
        <div class="logbook-brand-line">${_escHtml(line.brand || "—")}</div>
      `).join("")}
    </div>
  `;
}

function buildCompactUsedByList(entries, emptyMessage) {
  if (!entries.length) {
    return `<div class="used-by-empty muted-line">${emptyMessage}</div>`;
  }

  return `
    <div class="used-by-list">
      ${entries.map((entry) => `
        <div class="used-by-row used-by-row-button is-disabled" data-linked-sample="${_escHtml(entry.sampleId)}" role="button" tabindex="-1" aria-disabled="true">
          <span class="used-by-sample mono">${_escHtml(entry.sampleId)}</span>
          <span class="used-by-details">
            ${entry.lines.map((line) => `
              <span class="used-by-detail-line">
                <span class="color-chip used-by-chip" style="background:${line.hex}"></span>
                <span class="used-by-filament-name">${_escHtml(line.name)}</span>
                <span class="used-by-layer">${_escHtml(normalizeUsageLabel(line.layerLabel))}</span>
              </span>
            `).join("")}
          </span>
          <span class="used-by-status"><span class="status-pill ${entry.status.cls}">${entry.status.label}</span></span>
        </div>
      `).join("")}
    </div>
  `;
}

function buildFilamentUsedBySection(filamentId) {
  const usedBy = filamentUsedBySamples(filamentId);
  const rows = usedBy.map(({ exp }) => ({
    sampleId: exp.sample_id,
    lines: sampleMaterialLines(exp),
    status: sampleStatusMeta(exp),
  }));

  return {
    count: usedBy.length,
    html: buildCompactUsedByList(rows, "No samples use this filament"),
  };
}

function sourceDisplayName(exp) {
  return exp.source_image || exp.photo_name || "—";
}

function blankDisplayName(exp) {
  const blankObj = exp._assigned_blank_id
    ? (data.blanks || []).find((b) => b.blank_id === exp._assigned_blank_id)
    : null;
  return blankObj?.original_filename || exp.blank_image || exp._assigned_blank_id || "—";
}

function getImageRotationCw(filename) {
  if (!filename) return 0;
  const overrideRot = Number(data.image_overrides?.[filename]?.rotation_cw);
  if (Number.isFinite(overrideRot)) {
    return ((overrideRot % 4) + 4) % 4;
  }
  const match = (importState.images || []).find((img) => img.filename === filename);
  const rot = Number(match?.rotation_cw ?? 0);
  return Number.isFinite(rot) ? ((rot % 4) + 4) % 4 : 0;
}

function previewUrl(filename, options = {}) {
  if (!filename) return "";
  const params = new URLSearchParams();
  const size = options.size || "small";
  if (size && size !== "small") params.set("size", size);
  params.set("r", String(getImageRotationCw(filename)));
  const bump = maintenanceCacheBust.allPreviews || maintenanceCacheBust.previews.get(filename);
  if (bump) params.set("maintenance_v", String(bump));
  return `/api/previews/${encodeURIComponent(filename)}?${params.toString()}`;
}

function blankPreviewUrl(blankId, options = {}) {
  if (!blankId) return "";
  const params = new URLSearchParams();
  const size = options.size || "small";
  if (size && size !== "small") params.set("size", size);
  const bump = maintenanceCacheBust.allPreviews || maintenanceCacheBust.blankPreviews.get(blankId);
  if (bump) params.set("maintenance_v", String(bump));
  return `/api/blanks/${encodeURIComponent(blankId)}/preview?${params.toString()}`;
}

function imageRotationPillHtml(filename) {
  const rotationCw = getImageRotationCw(filename);
  if (!rotationCw) return "";
  return `<span class="image-rotation-pill" title="Image rotation override: ${rotationCw * 90}\u00b0 clockwise">&#8635; ${rotationCw * 90}\u00b0</span>`;
}

function hexToRgbString(hex) {
  const clean = (hex || "").replace("#", "");
  if (clean.length !== 6) return "—";
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  return `${r}, ${g}, ${b}`;
}

function normalizeHexInput(raw) {
  const clean = (raw || "").trim().replace("#", "");
  if (!/^[0-9A-Fa-f]{6}$/.test(clean)) return null;
  return "#" + clean.toUpperCase();
}

function rgbStringToHex(rgbString) {
  const parts = (rgbString || "").split(",").map((part) => part.trim()).filter(Boolean);
  if (parts.length !== 3) return null;
  const nums = parts.map((part) => Number(part));
  if (nums.some((n) => Number.isNaN(n) || n < 0 || n > 255)) return null;
  return "#" + nums.map((n) => Math.round(n).toString(16).padStart(2, "0")).join("").toUpperCase();
}

function placeholderThumb(label) {
  return `<div class="thumb-placeholder"><span>${label}</span></div>`;
}

function sigfig(val, n = 4) {
  if (val === 0) return "0";
  return Number(val.toPrecision(n)).toString();
}

function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d > 0) {
    if (max === r) h = ((g - b) / d + 6) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
  }
  const s = max === 0 ? 0 : d / max * 100;
  const v = max * 100;
  return { h: Math.round(h), s: Math.round(s), v: Math.round(v) };
}

function textColor(hex) {
  if (!hex) return "#111";
  const clean = hex.replace("#", "");
  if (clean.length !== 6) return "#111";
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return luminance > 0.72 ? "#111" : "#fff";
}

function filamentMeta(fid) {
  return data.filaments.find((fil) => fil.filament_id === fid) || null;
}

function formatStepNumber(value) {
  const num = Number(value);
  if (Number.isNaN(num)) return "0.00";
  return num.toFixed(2);
}

function numericValue(value, fallback = 0) {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed || trimmed === ".") return fallback;
  }
  const num = Number(value);
  return Number.isNaN(num) ? fallback : num;
}

function sanitizeStepDecimalInput(raw) {
  let sanitized = String(raw ?? "").replace(/[^\d.]/g, "");
  const firstDot = sanitized.indexOf(".");
  if (firstDot >= 0) {
    sanitized =
      sanitized.slice(0, firstDot + 1) +
      sanitized.slice(firstDot + 1).replace(/\./g, "");
  }
  if (sanitized.startsWith(".")) sanitized = `0${sanitized}`;
  return sanitized;
}

function normalizeStepDecimalInput(raw, fallback = "0.00") {
  const sanitized = sanitizeStepDecimalInput(raw);
  const numeric = numericValue(sanitized, NaN);
  if (!Number.isFinite(numeric)) return fallback;
  return formatStepNumber(numeric);
}

function bindStepDecimalInput(input, { onInput, onBlur } = {}) {
  if (!input) return;
  input.setAttribute("inputmode", "decimal");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("spellcheck", "false");

  input.addEventListener("input", () => {
    const sanitized = sanitizeStepDecimalInput(input.value);
    if (input.value !== sanitized) input.value = sanitized;
    if (onInput) onInput(sanitized, input);
  });

  input.addEventListener("blur", () => {
    const normalized = normalizeStepDecimalInput(input.value);
    input.value = normalized;
    if (onBlur) onBlur(normalized, input);
    else if (onInput) onInput(normalized, input);
  });
}

function isEvenLayerIncrement(increment, layerHeight) {
  const inc = numericValue(increment, NaN);
  const lh = numericValue(layerHeight, NaN);
  if (!Number.isFinite(inc) || !Number.isFinite(lh) || lh <= 0) return false;
  const steps = inc / lh;
  return Math.abs(steps - Math.round(steps)) < 1e-6;
}

function getSuspectSwatchIndexes() {
  const lhEl = _sbEl("stepLayerHeight");
  const layerHeight = numericValue(lhEl ? lhEl.value : NaN, NaN);
  if (!Number.isFinite(layerHeight) || layerHeight <= 0) return [];

  const baseIndex = 0;
  const baseValue = numericValue(stepBuilderState.values[baseIndex], NaN);
  if (!Number.isFinite(baseValue)) return [];

  return stepBuilderState.values.reduce((suspectIndexes, value, index) => {
    if (index < baseIndex) return suspectIndexes;
    const numeric = numericValue(value, NaN);
    if (!Number.isFinite(numeric)) {
      suspectIndexes.push(index);
      return suspectIndexes;
    }
    const delta = numeric - baseValue;
    const steps = delta / layerHeight;
    if (Math.abs(steps - Math.round(steps)) >= 1e-6) {
      suspectIndexes.push(index);
    }
    return suspectIndexes;
  }, []);
}

function buildStripMiniTable(exp) {
  const roleRows = [...(exp.roles || [])].sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
  if (roleRows.length === 0) {
    return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
  }
  const variableRole = roleRows.find((role) => role.role_kind === "variable");
  if (!variableRole) {
    return `<div class="strip-diagram-contract-error">Missing variable layer role</div>`;
  }
  const variableFilament = filamentMeta(variableRole.filament_id || "");
  const variableHex = variableFilament?.hex || "#dddddd";
  const variableText = textColor(variableHex);
  if (!(exp.variable_thicknesses_mm || []).length) {
    return `<div class="strip-diagram-contract-error">Missing swatch thickness data</div>`;
  }
  const variableCells = (exp.variable_thicknesses_mm || []).map((thickness) => (
    `<td style="background:${variableHex};color:${variableText}">${Number(thickness).toFixed(2)}</td>`
  )).join("");
  const swatchCount = (exp.variable_thicknesses_mm || []).length;

  const rows = roleRows.map((role) => {
    if (role.role_kind === "variable") return `<tr>${variableCells}</tr>`;
    if (role.fixed_thickness_mm == null) {
      return `<tr><td colspan="${swatchCount}"><div class="strip-diagram-contract-error">Missing fixed role thickness</div></td></tr>`;
    }
    const thickness = role.fixed_thickness_mm;
    const fixedId = role.filament_id || "";
    const fixed = filamentMeta(fixedId);
    const fixedHex = fixed?.hex || "#eeeeee";
    const fixedText = textColor(fixedHex);
    const label = `${Number(thickness).toFixed(2)}mm`;
    return `<tr><td colspan="${swatchCount}" style="background:${fixedHex};color:${fixedText}">${label}</td></tr>`;
  });

  return `<table class="mini-strip-table">${rows.join("")}</table>`;
}

function buildAssignedGeometryRoles(step, variableFilamentId = "", fixedFilamentIds = []) {
  const roles = [...(step?.roles || [])].sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0));
  const fixedRoles = roles.filter((role) => role.role_kind === "fixed");
  const fixedIdByRole = new Map(fixedRoles.map((role, index) => [Number(role.role_index), fixedFilamentIds[index] || ""]));
  return roles.map((role) => ({
    ...role,
    filament_id: role.role_kind === "variable"
      ? variableFilamentId
      : fixedIdByRole.get(Number(role.role_index)) || "",
  }));
}

function buildAssignedGeometryRolesFromAssignments(step, roleAssignments = []) {
  const filamentByRole = new Map((roleAssignments || []).map((assignment) => [
    Number(assignment.role_index),
    assignment.filament_id || "",
  ]));
  return [...(step?.roles || [])]
    .sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0))
    .map((role) => ({
      ...role,
      filament_id: filamentByRole.get(Number(role.role_index)) || "",
    }));
}

function buildGeometryStripMiniTable(step) {
  const variableSlots = [...(step?.swatch_slots || [])]
    .sort((a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0));
  if (!variableSlots.length) {
    return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
  }
  const variableThicknesses = variableSlots.map((slot) => Number(slot.variable_thickness_mm || 0));

  const variableCells = variableThicknesses.map((thickness) => (
    `<td style="background:#d7d7d3;color:#222">${Number(thickness).toFixed(2)}</td>`
  )).join("");
  const swatchCount = variableThicknesses.length || Number(step?.swatch_count || step?.layout_columns || 8);

  const roles = [...(step?.roles || [])].sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
  if (roles.length === 0) {
    return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
  }
  const rows = roles.map((role) => {
    if (role.role_kind === "variable") {
      return `<tr>${variableCells}</tr>`;
    }
    const value = Number(role.fixed_thickness_mm || 0).toFixed(2);
    return `<tr><td colspan="${swatchCount}" style="background:#ececea;color:#222">${value}mm</td></tr>`;
  });
  return `<table class="mini-strip-table">${rows.join("")}</table>`;
}

function fixedLayerDisplayEntries(fixedLayers = []) {
  return (fixedLayers || [])
    .map((layer, index) => ({ layer, index }))
    .reverse();
}

function fixedLayerCanonicalEntries(fixedLayers = []) {
  return (fixedLayers || [])
    .map((layer, index) => ({ layer, index }))
    .sort((a, b) => Number(a.layer.role_index || a.index + 1) - Number(b.layer.role_index || b.index + 1));
}

function collectFixedSelectValuesInDomOrder(selector) {
  return Array.from(detailSidebar.querySelectorAll(selector)).map((el) => (
    el.dataset?.filamentId || el.value || ""
  ));
}

function collectFixedSelectValuesByRole(selector) {
  const byRole = new Map();
  detailSidebar.querySelectorAll(selector).forEach((el) => {
    const roleIndex = Number(el.dataset?.roleIndex || 0);
    if (Number.isFinite(roleIndex) && roleIndex > 0) {
      byRole.set(roleIndex, el.dataset?.filamentId || el.value || "");
    }
  });
  return byRole;
}

function canonicalFixedFilamentIdsFromMap(step, fixedIdByRole) {
  return fixedLayerCanonicalEntries(step?.fixed_layers || []).map(({ layer }) => (
    fixedIdByRole.get(Number(layer.role_index || 0)) || ""
  ));
}

function fixedLayerCanonicalThicknesses(fixedLayers = []) {
  return fixedLayerCanonicalEntries(fixedLayers).map((entry) => entry.layer.thickness_mm || 0);
}

function buildRoleAssignmentsForStep(step, variableFilamentId, fixedIdByRole) {
  return [...(step?.roles || [])]
    .sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0))
    .map((role) => ({
      role_index: Number(role.role_index || 0),
      filament_id: role.role_kind === "variable"
        ? variableFilamentId
        : fixedIdByRole.get(Number(role.role_index || 0)) || "",
    }));
}

function filamentIdsFromRoleAssignments(step, roleAssignments = [], { order = "top-to-bottom" } = {}) {
  return buildAssignedGeometryRolesFromAssignments(step, roleAssignments)
    .sort((a, b) => {
      const delta = Number(a.role_index || 0) - Number(b.role_index || 0);
      return order === "bottom-to-top" ? delta : -delta;
    })
    .map((role) => role.filament_id || "");
}

function sampleRoleAssignmentTuple(exp) {
  return [...(exp?.roles || [])]
    .sort((a, b) => Number(a.role_index || 0) - Number(b.role_index || 0))
    .map((role) => ({
      role_index: Number(role.role_index || 0),
      filament_id: role.filament_id || "",
    }));
}

function fixedFilamentIdsByRoleFromSample(exp) {
  const byRole = new Map();
  (exp?.roles || []).forEach((role) => {
    if (role.role_kind === "fixed") {
      byRole.set(Number(role.role_index || 0), role.filament_id || "");
    }
  });
  return byRole;
}

function fixedLayerDisplayThicknesses(fixedLayers = []) {
  return fixedLayerDisplayEntries(fixedLayers).map((entry) => entry.layer.thickness_mm || 0);
}

function variableRoleForStep(step) {
  return (step?.roles || []).find((role) => role.role_kind === "variable") || null;
}

function buildFixedLayerPreviewValues(fixedLayers = [], fixedIdsByIndex = []) {
  return fixedLayerDisplayEntries(fixedLayers).reduce((acc, entry) => {
    acc.thicknesses.push(entry.layer.thickness_mm || 0);
    acc.ids.push(fixedIdsByIndex[entry.index] || "");
    return acc;
  }, { thicknesses: [], ids: [] });
}

function stepMeta(stepId) {
  const canonicalId = stepIdFromRef(stepId);
  if (!stepMetadata[canonicalId]) {
    const step = stepRecordByRef(stepId);
    stepMetadata[canonicalId] = { alias: step?.alias || "", bundle: step?.bundle || "", deleted: false };
  }
  return stepMetadata[canonicalId];
}

function existingBundleNames() {
  return [...new Set(
    Object.values(stepMetadata)
      .map((meta) => (meta.bundle || "").trim())
      .filter(Boolean)
  )].sort((a, b) => a.localeCompare(b));
}

function renderBundleOptions() {
  if (!stepBundleOptions) return;
  // Try to load from bundles registry, fall back to stepMetadata-based names
  refreshBundleOptionsFromRegistry().catch(() => {
    stepBundleOptions.innerHTML = existingBundleNames()
      .map((bundle) => `<option value="${bundle}"></option>`)
      .join("");
  });
}

function resetStepEditorState(stepId = null) {
  const meta = stepId ? stepMeta(stepId) : { alias: "", bundle: "" };
  stepEditorState = {
    stepId,
    isEditing: false,
    draftAlias: meta.alias || "",
    draftBundle: meta.bundle || "",
    confirmDelete: false,
    deleteMessage: "",
    deleteMessageKind: "",
  };
}

function stepUsageCount(stepId) {
  const canonicalId = stepIdFromRef(stepId);
  return data.samples.filter((exp) => sampleStepId(exp) === canonicalId).length;
}

function renderStepExpandedRow(step) {
  const stepId = step.step_id || step.file_name;
  const meta = stepMeta(stepId);
  const usageCount = stepUsageCount(stepId);
  const isSelected = selectedRecord.kind === "step" && selectedRecord.id === stepId;
  if (!isSelected) return "";
  if (stepEditorState.stepId !== stepId) {
    resetStepEditorState(stepId);
  }

  const messageClass = stepEditorState.deleteMessageKind ? ` delete-notice is-${stepEditorState.deleteMessageKind}` : " delete-notice";

  return `
    <tr class="inline-detail-row">
      <td colspan="5">
        <div class="inline-detail-card">
          <div class="inline-detail-header">
            <div class="inline-detail-title">
              <span class="sidebar-label">STEP Detail</span>
              <strong class="mono">${step.file_name}</strong>
            </div>
          </div>

          ${stepEditorState.isEditing ? `
            <div class="inline-editor-stack">
              <label class="compact-inline-row">
                <span>Alias</span>
                <input type="text" id="inlineStepAliasInput" value="${stepEditorState.draftAlias}" placeholder="e.g. thin over white" />
              </label>
              <label class="compact-inline-row">
                <span>Bundle</span>
                <input type="text" id="inlineStepBundleInput" list="stepBundleOptions" value="${stepEditorState.draftBundle}" placeholder="e.g. default calibration set" />
              </label>
            </div>
            <div class="inline-action-row">
              <button class="save-button small" type="button" data-step-action="save-edit" data-step-id="${stepId}">Save</button>
              <button class="ghost-button small" type="button" data-step-action="discard-edit" data-step-id="${stepId}">Discard</button>
              <button class="delete-button small" type="button" data-step-action="start-delete" data-step-id="${stepId}">Delete</button>
            </div>
          ` : `
            <div class="inline-detail-grid">
              <div class="inline-mini-box">
                <span class="sidebar-label">Alias</span>
                <strong>${meta.alias || "—"}</strong>
              </div>
              <div class="inline-mini-box">
                <span class="sidebar-label">Bundle</span>
                <strong>${meta.bundle || "—"}</strong>
              </div>
              <div class="inline-mini-box">
                <span class="sidebar-label">Usage</span>
                <strong>${usageCount} sample(s)</strong>
              </div>
            </div>
            <div class="inline-action-row">
              <button class="edit-button small${stepEditorState.isEditing ? " is-active" : ""}" type="button" data-step-action="toggle-edit" data-step-id="${stepId}">
                Edit
              </button>
              <button class="delete-button small" type="button" data-step-action="start-delete" data-step-id="${stepId}">Delete</button>
            </div>
          `}

          ${stepEditorState.confirmDelete ? `
            <div class="delete-confirm-row">
              <span>Delete this STEP definition?</span>
              <button class="delete-button small" type="button" data-step-action="confirm-delete" data-step-id="${stepId}">Confirm Delete</button>
              <button class="ghost-button small" type="button" data-step-action="cancel-delete" data-step-id="${stepId}">Cancel</button>
            </div>
          ` : ""}

          <div class="${messageClass.trim()}" id="inlineStepDeleteNotice">${stepEditorState.deleteMessage}</div>
        </div>
      </td>
    </tr>
  `;
}

function renderSummaryRail() {
  const exps = data.samples || [];
  const processedCount = exps.filter((e) => e._processing_status === "processed" || e.processed).length;
  summaryRail.innerHTML = `
    <div class="rail-stat"><strong>${(data.filaments || []).length}</strong><span>filaments</span></div>
    <div class="rail-stat"><strong>${exps.length}</strong><span>samples</span></div>
    <div class="rail-stat"><strong>${(data.steps || []).length}</strong><span>STEP files</span></div>
    <div class="rail-stat"><strong>${processedCount}</strong><span>processed strips</span></div>
  `;
}

function renderModeButtons() {
  if (modeSwitch) {
    modeSwitch.setAttribute("role", "tablist");
    modeSwitch.setAttribute("aria-label", "Primary navigation");
  }
  modeSwitch.querySelectorAll(".mode-button").forEach((button) => {
    const isActive = button.dataset.mode === currentMode;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", isActive ? "true" : "false");
    button.tabIndex = isActive ? 0 : -1;
  });
}

function getTabButtons(container, selector) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(selector)).filter((button) => !button.disabled);
}

function focusModeButton(modeId = currentMode) {
  const button = modeSwitch?.querySelector(`.mode-button[data-mode="${modeId}"]`);
  button?.focus();
}

function focusSubtabButton(subtabId = currentSubtab) {
  const button = subtabRow?.querySelector(`.subtab-button[data-subtab="${subtabId}"]`);
  button?.focus();
}

async function activateMode(modeId) {
  if (!modeId) return;
  currentMode = modeId;
  const subtabs = modeConfig[currentMode]?.subtabs || [];
  currentSubtab = subtabs.length > 0 ? subtabs[0].id : "";
  clearSelectionAndDrawer();
  closeStepBuilderDrawer();
  closeBundleMgmtDrawer();
  closeFilamentBuilderPanel();
  if (currentMode === "imageProcessing" && !importState.loaded) {
    importState.loading = true;
    importState.loadingMessage = "Loading image inbox";
    renderWorkspace();
    await loadImportData();
  }
  renderWorkspace();
}

async function activateSubtab(subtabId) {
  if (!subtabId) return;
  currentSubtab = subtabId;
  clearSelectionAndDrawer();
  closeStepBuilderDrawer();
  closeBundleMgmtDrawer();
  closeFilamentBuilderPanel();
  if (currentSubtab === "associate" && !importState.loaded) {
    importState.loading = true;
    importState.loadingMessage = "Loading image inbox";
    renderWorkspace();
    await loadImportData();
  }
  renderWorkspace();
}

function bindArrowTabNavigation(container, selector, options = {}) {
  if (!container || container.dataset.arrowTabsBound === "true") return;
  container.dataset.arrowTabsBound = "true";
  container.addEventListener("keydown", async (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const activeButton = e.target.closest(selector);
    if (!activeButton || !container.contains(activeButton)) return;

    const buttons = getTabButtons(container, selector);
    if (!buttons.length) return;

    const currentIndex = Math.max(0, buttons.indexOf(activeButton));
    let targetButton = null;

    if (e.key === "ArrowLeft") {
      targetButton = buttons[(currentIndex - 1 + buttons.length) % buttons.length];
    } else if (e.key === "ArrowRight") {
      targetButton = buttons[(currentIndex + 1) % buttons.length];
    } else if (e.key === "Home") {
      targetButton = buttons[0];
    } else if (e.key === "End") {
      targetButton = buttons[buttons.length - 1];
    } else if (e.key === "ArrowDown" && typeof options.onArrowDown === "function") {
      e.preventDefault();
      options.onArrowDown(activeButton);
      return;
    } else if (e.key === "ArrowUp" && typeof options.onArrowUp === "function") {
      e.preventDefault();
      options.onArrowUp(activeButton);
      return;
    } else {
      return;
    }

    if (!targetButton || targetButton === activeButton) return;

    e.preventDefault();
    if (typeof options.activate === "function") {
      await options.activate(targetButton);
    } else {
      targetButton.click();
    }
    if (typeof options.focusActive === "function") {
      options.focusActive(targetButton);
    } else {
      targetButton.focus();
    }
  });
}

function mountSubtabsInOwnedSurface() {
  if (!subtabContainer) return;
  const subtabs = modeConfig[currentMode]?.subtabs || [];
  const defaultContent = document.getElementById("defaultContent");
  const importView = document.getElementById("importView");
  const importModeShell = document.getElementById("importModeShell");
  const mainLogbook = defaultContent?.querySelector(".main-logbook");

  if (subtabs.length <= 1) {
    modeSwitch.insertAdjacentElement("afterend", subtabContainer);
    return;
  }

  if (currentMode === "imageProcessing" && currentSubtab === "associate" && importModeShell) {
    importModeShell.prepend(subtabContainer);
    return;
  }

  if (mainLogbook) {
    mainLogbook.prepend(subtabContainer);
  }
}

function renderSubtabs() {
  const subtabs = modeConfig[currentMode]?.subtabs || [];
  if (subtabContainer) {
    subtabContainer.style.display = subtabs.length <= 1 ? "none" : "";
  }
  workspaceRoot?.classList.toggle("subtabs-hidden", subtabs.length <= 1);
  if (subtabs.length === 0) {
    subtabRow.innerHTML = "";
    currentSubtab = "";
    return;
  }
  if (!subtabs.some((tab) => tab.id === currentSubtab)) {
    currentSubtab = subtabs[0].id;
  }

  subtabRow.setAttribute("role", "tablist");
  subtabRow.setAttribute("aria-label", `${currentMode} section navigation`);

  subtabRow.innerHTML = subtabs.map((tab) => `
    <button class="subtab-button${tab.id === currentSubtab ? " is-active" : ""}" data-subtab="${tab.id}" role="tab" aria-selected="${tab.id === currentSubtab ? "true" : "false"}" tabindex="${tab.id === currentSubtab ? "0" : "-1"}">
      ${tab.label}
    </button>
  `).join("");

  subtabRow.querySelectorAll(".subtab-button").forEach((button) => {
    button.addEventListener("click", async () => {
      await activateSubtab(button.dataset.subtab);
    });
  });
}

function renderStatusSummary() {
  const metas = data.samples.map((exp) => sampleStatusMeta(exp));
  const processed = metas.filter((meta) => meta.cls === "processed" || meta.cls === "accepted").length;
  const ready = metas.filter((meta) => meta.cls === "ready").length;
  const incomplete = metas.filter((meta) => meta.cls === "incomplete").length;
  const unassigned = metas.filter((meta) => meta.cls === "unassigned").length;
  const profiled = data.filaments.filter((fil) => fil.has_profile).length;

  statusSummary.innerHTML = `
    <div class="status-box"><span>Processed</span><strong>${processed}</strong></div>
    <div class="status-box"><span>Ready</span><strong>${ready}</strong></div>
    <div class="status-box"><span>Incomplete</span><strong>${incomplete}</strong></div>
    <div class="status-box"><span>Unassigned</span><strong>${unassigned}</strong></div>
    <div class="status-box"><span>Profiled filaments</span><strong>${profiled}</strong></div>
  `;
}

function syncRecordDrawerPosition() {
  if (!recordDrawer) return;
  const importModeShell = document.getElementById("importModeShell");
  const importView = document.getElementById("importView");
  const defaultPrimaryPanel = document.querySelector("#defaultContent .panel:first-child");
  const importIsVisible = importView && !importView.classList.contains("is-hidden");
  const ownedSurface = (currentMode === "imageProcessing" && currentSubtab === "associate" && importIsVisible && importModeShell)
    ? importModeShell
    : defaultPrimaryPanel;
  if (!ownedSurface) {
    recordDrawer.style.removeProperty("--record-drawer-top");
    return;
  }
  const top = Math.max(12, Math.round(ownedSurface.getBoundingClientRect().top));
  recordDrawer.style.setProperty("--record-drawer-top", `${top}px`);
}

function getLinkedSampleDrawerMetrics() {
  if (!recordDrawer?.classList.contains("is-open")) return null;
  const mainRect = recordDrawer.getBoundingClientRect();
  const availableWidth = Math.floor(mainRect.left - LINKED_SAMPLE_DRAWER_GAP - LINKED_SAMPLE_DRAWER_MIN_LEFT_MARGIN);
  const canOpen = availableWidth >= LINKED_SAMPLE_DRAWER_MIN_WIDTH;
  const width = Math.min(LINKED_SAMPLE_DRAWER_MAX_WIDTH, Math.max(0, availableWidth));
  return {
    canOpen,
    width,
    top: Math.max(12, Math.round(mainRect.top)),
    right: Math.max(12, Math.round(window.innerWidth - mainRect.left + LINKED_SAMPLE_DRAWER_GAP)),
  };
}

function canOpenLinkedSampleDrawer() {
  return Boolean(getLinkedSampleDrawerMetrics()?.canOpen);
}

function updateLinkedSampleTriggers(root = detailSidebar) {
  if (!root) return;
  const enabled = canOpenLinkedSampleDrawer();
  root.querySelectorAll("[data-linked-sample]").forEach((node) => {
    node.classList.toggle("is-disabled", !enabled);
    node.classList.toggle("is-enabled", enabled);
    node.setAttribute("aria-disabled", enabled ? "false" : "true");
    node.tabIndex = enabled ? 0 : -1;
  });
}

function syncLinkedSampleDrawerPosition() {
  if (!linkedSampleDrawer) return;
  const metrics = getLinkedSampleDrawerMetrics();
  if (!metrics?.canOpen) {
    if (linkedSampleDrawer.classList.contains("is-open")) {
      closeLinkedSampleDrawer({ restoreFocus: false });
    }
    return;
  }

  linkedSampleDrawer.style.setProperty("--record-drawer-top", `${metrics.top}px`);
  linkedSampleDrawer.style.setProperty("--linked-drawer-shift", `${metrics.right}px`);
  linkedSampleDrawer.style.width = `${metrics.width}px`;
  linkedSampleDrawer.style.right = `${metrics.right}px`;
}

function syncModeTabRowWidth() {
  if (!modeSwitch) return;
  const importModeShell = document.getElementById("importModeShell");
  const importView = document.getElementById("importView");
  const defaultPrimaryPanel = document.querySelector("#defaultContent .panel:first-child");
  const importIsVisible = importView && !importView.classList.contains("is-hidden");
  let ownedSurface = defaultPrimaryPanel;
  if (currentMode === "imageProcessing" && currentSubtab === "associate" && importIsVisible && importModeShell) {
    ownedSurface = importModeShell;
  }
  if (!ownedSurface) {
    modeSwitch.style.removeProperty("--mode-tab-row-width");
    return;
  }
  const rect = ownedSurface.getBoundingClientRect();
  const availableWidth = Math.max(240, Math.floor(window.innerWidth - rect.left - 12));
  const width = Math.max(320, Math.min(Math.round(rect.width), availableWidth));
  modeSwitch.style.setProperty("--mode-tab-row-width", `${width}px`);
}

function setDrawerHeading(value, { html = false, technical = false } = {}) {
  if (!detailHeading) return;
  detailHeading.classList.toggle("is-technical", technical);
  if (html) {
    detailHeading.innerHTML = value;
  } else {
    detailHeading.textContent = value;
  }
}

function setDetailSidebarStackMode(mode = "default") {
  if (!detailSidebar) return;
  detailSidebar.classList.remove("drawer-form-stack");
  if (mode === "form") {
    detailSidebar.classList.add("drawer-form-stack");
  }
}


function renderManagementLogbook() {
  tableToolbar.className = "toolbar-inline";
  const processedCount = data.samples.filter((exp) => exp.processed).length;
  const unprocessedCount = data.samples.filter((exp) => !exp.processed).length;
  tableSummary.textContent = `${data.samples.length} samples, ${processedCount} processed`;
  tableToolbar.innerHTML = `
    <button class="primary-button small" id="newSampleBtn">+ New Samples</button>
  `;

  const enriched = [...data.samples].map((exp) => ({
    ...exp,
    _status: statusForSample(exp),
    _filament_stack_sort: sampleFilamentStackSortText(exp),
    _brand_stack_sort: sampleBrandStackSortText(exp),
  }));
  if (sortState.key) {
    enriched.sort((a, b) => compareRows(a, b, sortState.key, sortState.direction));
  }

  const rows = enriched.map((exp) => {
    const status = sampleStatusMeta(exp);
    const imageName = sourceDisplayName(exp);
    const blankName = blankDisplayName(exp);
    const imageRotationPill = exp._assigned_image ? imageRotationPillHtml(exp._assigned_image) : "";
    const imageCustodyBadge = exp._assigned_image ? imageCustodyBadgeHtml(exp._assigned_image, "Image") : "";
    const blankObj = exp._assigned_blank_id
      ? (data.blanks || []).find((b) => b.blank_id === exp._assigned_blank_id)
      : null;
    const blankCustodyBadge = blankObj?.original_filename ? imageCustodyBadgeHtml(blankObj.original_filename, "Blank image") : "";
    return `
      <tr class="data-row" data-kind="sample" data-id="${exp.sample_id}">
        <td class="id-cell">${exp.sample_id}</td>
        <td>
          ${buildStripMiniTable(exp)}
        </td>
        <td>
          ${renderLogbookFilamentCell(exp)}
        </td>
        <td>${renderLogbookBrandCell(exp)}</td>
        <td class="mono sample-source-state-cell">${imageName}${imageRotationPill}${imageCustodyBadge}</td>
        <td class="mono sample-source-state-cell">${blankName}${blankCustodyBadge}</td>
        <td><span class="status-pill ${status.cls}">${status.label}</span></td>
      </tr>
    `;
  }).join("");

  tableContainer.innerHTML = `
    <table class="data-table management-library-table">
      <thead>
        <tr>
          <th class="sortable" data-sort="sample_id">ID${sortArrow("sample_id")}</th>
          <th>Strip</th>
          <th class="sortable" data-sort="_filament_stack_sort">Filament${sortArrow("_filament_stack_sort")}</th>
          <th class="sortable" data-sort="_brand_stack_sort">Brand${sortArrow("_brand_stack_sort")}</th>
          <th>Image</th>
          <th>Blank</th>
          <th class="sortable" data-sort="_status">Status${sortArrow("_status")}</th>
        </tr>
      </thead>
      <tbody>${rows || `<tr><td colspan="7" class="empty-cell">No samples yet. Use + New Samples to create the first calibration sample set.</td></tr>`}</tbody>
    </table>
  `;
  bindSortHeaders();
  // Bind sample creation entry point.
  const newSampleBtn = document.getElementById("newSampleBtn");
  if (newSampleBtn) {
    newSampleBtn.addEventListener("click", () => openBulkSampleCreateDrawer());
  }
}

function renderFilamentLibrary() {
  tableToolbar.className = "toolbar-inline";
  tableSummary.textContent = `${data.filaments.length} filaments, ${data.filaments.filter((fil) => fil.has_profile).length} profiled`;
  tableToolbar.innerHTML = `
    <button class="primary-button small" id="addFilamentBtn">+ New Filament</button>
  `;

  const sorted = [...data.filaments];
  if (sortState.key) {
    const key = sortState.key === "profile" ? "_profileSort" : sortState.key;
    sorted.forEach((f) => { f._profileSort = f.has_profile ? "profiled" : "pending"; });
    sorted.sort((a, b) => compareRows(a, b, key, sortState.direction));
  }

  const rows = sorted.map((fil) => {
    const status = filamentStatusMeta(fil);
    return `
    <tr class="data-row" data-kind="filament" data-id="${fil.filament_id}">
      <td>
        <div class="filament-cell">
          <span class="color-chip" style="background:${fil.hex}"></span>
          ${fil.color_name}
        </div>
      </td>
      <td>${fil.manufacturer}</td>
      <td class="mono">${fil.filament_id}</td>
      <td>${fil.sample_count}</td>
      <td>${fil.processed_count}</td>
      <td><span class="status-pill ${status.cls}">${status.label}</span></td>
    </tr>
  `;
  }).join("");

  tableContainer.innerHTML = `
    <table class="data-table management-library-table">
      <thead>
        <tr>
          <th class="sortable" data-sort="color_name">Filament${sortArrow("color_name")}</th>
          <th class="sortable" data-sort="manufacturer">Manufacturer${sortArrow("manufacturer")}</th>
          <th>ID</th>
          <th>Samples</th>
          <th>Processed</th>
          <th class="sortable" data-sort="profile">Profile${sortArrow("profile")}</th>
        </tr>
      </thead>
      <tbody>${rows || `<tr><td colspan="6" class="empty-cell">No filaments yet. Use + New Filament to create the first filament.</td></tr>`}</tbody>
    </table>
  `;

  bindSortHeaders();
  document.getElementById("addFilamentBtn")?.addEventListener("click", () => {
    openFilamentCreateDrawer();
  });
}

function renderStepLibrary() {
  tableToolbar.className = "toolbar-inline";
  const visibleSteps = data.steps.filter((step) => !stepMeta(step.step_id).deleted);
  const stepStoragePath = (_serverConfig && (_serverConfig.step_export_relative || _serverConfig.step_library_relative)) || "output/steps/";
  tableSummary.textContent = `${visibleSteps.length} sample geometries`;
  tableToolbar.innerHTML = `
    <button class="primary-button small" id="openStepBuilderBtn">+ New Sample Geometry</button>
    <button class="ghost-button small" id="createBundleBtn">Manage Bundles</button>
    <button class="ghost-button small" id="exportGeometryFilesBtn">Export Geometry Files</button>
  `;

  const usage = {};
  data.samples.forEach((exp) => {
    const sid = sampleStepId(exp);
    if (!sid) return;
    usage[sid] = (usage[sid] || 0) + 1;
  });

  const enriched = visibleSteps.map((step) => {
    const meta = stepMeta(step.step_id);
    return {
      ...step,
      _layers: step.layer_count || (1 + (step.fixed_layers || []).length),
      _usage: usage[step.step_id] || 0,
      _alias: meta.alias || "",
      _bundle: (step.bundle_names || []).join(", ") || meta.bundle || "",
    };
  });

  if (sortState.key) {
    enriched.sort((a, b) => compareRows(a, b, sortState.key, sortState.direction));
  }

  const rows = enriched.map((step) => {
    const meta = stepMeta(step.step_id);
    const geometryName = meta.alias || step.alias || step.step_id;
    return `
      <tr class="data-row${selectedRecord.kind === "step" && selectedRecord.id === step.step_id ? " is-selected" : ""}" data-kind="step" data-id="${step.step_id}">
        <td>${buildGeometryStripMiniTable(step)}</td>
        <td>${geometryName || "—"}</td>
        <td>${(step.bundle_names || []).join(", ") || meta.bundle || "—"}</td>
        <td class="sortable" data-sort="_layers">${step._layers}</td>
        <td>${step._usage}</td>
        <td>${step.last_write_time}</td>
      </tr>
    `;
  }).join("");

  tableContainer.innerHTML = `
    <table class="data-table management-library-table">
      <thead>
        <tr>
          <th>Strip</th>
          <th class="sortable" data-sort="_alias">Alias${sortArrow("_alias")}</th>
          <th class="sortable" data-sort="_bundle">Bundle${sortArrow("_bundle")}</th>
          <th class="sortable" data-sort="_layers"># Layers${sortArrow("_layers")}</th>
          <th class="sortable" data-sort="_usage">Used by${sortArrow("_usage")}</th>
          <th class="sortable" data-sort="last_write_time">Last modified${sortArrow("last_write_time")}</th>
        </tr>
      </thead>
      <tbody>${rows || `<tr><td colspan="6" class="empty-cell">No sample geometries yet. Use + New Sample Geometry to create the first geometry.</td></tr>`}</tbody>
    </table>
  `;

  bindSortHeaders();
  bindStepBuilderButton();
  bindGeometryLibraryExportButton();
  bindStepStoragePathButton(stepStoragePath);
  bindStepInlineActions();

  // Manage Bundles button — opens the bundle management drawer
  const createBundleBtn = document.getElementById("createBundleBtn");
  if (createBundleBtn) {
    createBundleBtn.addEventListener("click", () => {
      if (isBundleMgmtOpen()) {
        closeBundleMgmtDrawer();
      } else {
        openBundleManagementDrawer();
      }
    });
  }
}

function renderProcessedData() {
  tableToolbar.className = "toolbar-inline";
  tableSummary.textContent = `${data.processed_samples.length} processed strip rows`;
  tableToolbar.innerHTML = `
    <button class="ghost-button small" id="goToReviewBtn">Review processing data</button>
  `;

  const enriched = [...data.processed_samples];
  if (sortState.key) {
    enriched.sort((a, b) => compareRows(a, b, sortState.key, sortState.direction));
  }

  const rows = enriched.slice(0, 80).map((row) => {
    const sample = data.samples.find((exp) => exp.sample_id === row.sample_id);
    return `
      <tr class="data-row" data-kind="processed" data-id="${row.strip_id}">
        <td class="id-cell">${row.sample_id}</td>
        <td>${sample ? buildStripMiniTable(sample) : `<div class="strip-diagram-contract-error">Missing sample role data</div>`}</td>
        <td>
          <div class="filament-cell">
            <span class="color-chip" style="background:${row.hex}"></span>
            ${row.color_name}
          </div>
        </td>
        <td>${row.manufacturer || "—"}</td>
        <td>${row.swatch_count}</td>
        <td><span class="status-pill processed">processed</span></td>
      </tr>
    `;
  }).join("");

  tableContainer.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th class="sortable" data-sort="sample_id">ID${sortArrow("sample_id")}</th>
          <th>Strip</th>
          <th class="sortable" data-sort="color_name">Filament${sortArrow("color_name")}</th>
          <th class="sortable" data-sort="manufacturer">Brand${sortArrow("manufacturer")}</th>
          <th>Swatches</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  bindSortHeaders();
  document.getElementById("goToReviewBtn")?.addEventListener("click", () => {
    currentMode = "imageProcessing";
    currentSubtab = "queue";
    renderWorkspace();
  });
}

function renderProcessingPlaceholder(title, kicker, bullets) {
  tableSummary.textContent = title;
  tableToolbar.innerHTML = "";
  tableContainer.innerHTML = `
    <div class="placeholder-panel">
      <p class="panel-kicker">${kicker}</p>
      <ul class="check-list">
        ${bullets.map((bullet) => `<li>${bullet}</li>`).join("")}
      </ul>
    </div>
  `;
}

function renderImageProcessingLogbook() {
  tableToolbar.className = "toolbar-inline";

  // Show all samples that have entered the pipeline (assigned, processed, failed, flagged)
  const pipelineSamples = data.samples.filter((exp) => {
    const ps = exp._processing_status;
    return ps === "assigned" || ps === "processed" || ps === "failed" || ps === "flagged";
  });

  const counts = {
    assigned: pipelineSamples.filter((e) => e._processing_status === "assigned").length,
    processed: pipelineSamples.filter((e) => e._processing_status === "processed").length,
    failed: pipelineSamples.filter((e) => e._processing_status === "failed").length,
    flagged: pipelineSamples.filter((e) => e._processing_status === "flagged").length,
  };

  const parts = [`${pipelineSamples.length} in pipeline`, `${counts.processed} processed`];
  if (counts.assigned) parts.push(`${counts.assigned} ready`);
  if (counts.failed) parts.push(`${counts.failed} failed`);
  if (counts.flagged) parts.push(`${counts.flagged} flagged`);
  tableSummary.textContent = parts.join(", ");
  tableToolbar.innerHTML = "";

  let sorted = [...pipelineSamples];
  if (sortState.key) {
    sorted.sort((a, b) => compareRows(a, b, sortState.key, sortState.direction));
  }

  const rows = sorted.slice(0, 100).map((exp) => {
    const status = sampleStatusMeta(exp);
    const nSwatches = exp._n_swatches || (exp.variable_thicknesses_mm || []).length;
    const imgName = exp._assigned_image ? exp._assigned_image.replace(/\.[^.]+$/, "") : "—";
    return `
      <tr class="data-row" data-kind="sample" data-id="${exp.sample_id}">
        <td class="id-cell">${exp.sample_id}</td>
        <td>${buildStripMiniTable(exp)}</td>
        <td>
          <div class="filament-cell">
            <span class="color-chip" style="background:${exp.variable_hex}"></span>
            ${exp.variable_color_name}
          </div>
        </td>
        <td>${exp.manufacturer || "—"}</td>
        <td>${nSwatches || "—"}</td>
        <td class="mono">${imgName}</td>
        <td><span class="status-pill ${status.cls}">${status.label}</span></td>
      </tr>
    `;
  }).join("");

  tableContainer.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th class="sortable" data-sort="sample_id">ID${sortArrow("sample_id")}</th>
          <th>Strip</th>
          <th class="sortable" data-sort="variable_color_name">Filament${sortArrow("variable_color_name")}</th>
          <th class="sortable" data-sort="manufacturer">Brand${sortArrow("manufacturer")}</th>
          <th>Swatches</th>
          <th>Image</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>${rows || `<tr><td colspan="7" class="empty-cell">No samples have entered the processing pipeline yet.</td></tr>`}</tbody>
    </table>
  `;

  bindSortHeaders();
}


function resolveSampleMedia(exp) {
  const sourceImageFile = exp._assigned_image || exp.source_image || null;
  const blankObj = exp._assigned_blank_id
    ? (data.blanks || []).find((b) => b.blank_id === exp._assigned_blank_id)
    : null;
  const blankImageFile = blankObj?.original_filename || exp.blank_image || null;
  return {
    sourceName: sourceDisplayName(exp),
    sourceImageFile,
    blankObj,
    blankImageFile,
    blankLabel: blankObj?.original_filename || exp.blank_image || exp._assigned_blank_id || "—",
  };
}

  function buildLightboxThumbButton({ src, alt, title, emptyLabel, imgClass = "drawer-thumb", buttonAttrs = "", imgAttrs = "" }) {
    if (!src) return placeholderThumb(emptyLabel);
    return `
      <button class="drawer-thumb-button" type="button" data-lightbox-src="${_escAttr(src)}" data-lightbox-title="${_escAttr(title || alt)}" ${buttonAttrs}>
        <img class="${imgClass}" src="${src}" alt="${_escAttr(alt)}" ${imgAttrs} onload="this.style.display='block'" onerror="this.style.display='none'; this.parentElement.classList.add('is-empty'); this.nextElementSibling.style.display='flex'">
        <span class="thumb-placeholder" style="display:none"><span>${emptyLabel}</span></span>
      </button>
    `;
  }

function buildCompactSampleMediaPair(exp, media) {
  return `
    <div class="drawer-image-pair">
      <div class="drawer-image-card">
        <span class="sidebar-label" style="font-size:10px">Source</span>
        ${buildLightboxThumbButton({ src: media.sourceImageFile ? previewUrl(media.sourceImageFile) : null, alt: `${exp.sample_id} source`, title: `${exp.sample_id} source`, emptyLabel: "No preview" })}
        <span class="mono small-copy">${media.sourceName}</span>
      </div>
      <div class="drawer-image-card">
        <span class="sidebar-label" style="font-size:10px">Blank</span>
        ${buildLightboxThumbButton({ src: media.blankObj?.blank_id ? blankPreviewUrl(media.blankObj.blank_id) : null, alt: `${exp.sample_id} blank`, title: `${exp.sample_id} blank`, emptyLabel: "No blank" })}
        <span class="mono small-copy">${media.blankLabel}</span>
      </div>
    </div>
  `;
}

  function sampleStripMetrics(exp, fallbackN = null) {
    const geom = exp.strip_definition?.strip_geometry || exp.geometry || {};
    const positiveNumber = (value, fallback) => {
      const num = Number(value);
      return Number.isFinite(num) && num > 0 ? num : fallback;
    };
    const fallbackCount = fallbackN || exp._measurements?.swatches?.length || exp._n_swatches || exp.variable_thicknesses_mm?.length || 8;
    const borderMm = positiveNumber(geom.border_mm ?? geom.spine_width_mm, 3.0);
    const stepWMm = positiveNumber(geom.step_w_mm ?? geom.swatch_width_mm, 12.0);
    const stepHMm = positiveNumber(geom.step_h_mm ?? geom.swatch_height_mm, 20.0);
    const n = Math.max(1, Math.round(positiveNumber(geom.num_swatches ?? geom.swatch_count, Number(fallbackCount) || 8)));
    const totalW = (2 * borderMm) + (n * stepWMm);
    const totalH = stepHMm + borderMm;
    const ratio = totalW > 0 && totalH > 0 ? (totalW / totalH) : 4;
    const gridCols = `${borderMm}fr repeat(${n}, ${stepWMm}fr) ${borderMm}fr`;
    const gridRows = `${borderMm}fr ${stepHMm}fr`;
    return { borderMm, stepWMm, stepHMm, n, totalW, totalH, ratio, gridCols, gridRows };
  }

function swatchDisplayDomain(sw) {
  const display = sw?.display || {};
  return {
    hex: display.hex || "",
    R: display.R,
    G: display.G,
    B: display.B,
  };
}

function swatchTransmissionDomain(sw) {
  const transmission = sw?.transmission || {};
  return {
    R_linear: transmission.R_linear,
    G_linear: transmission.G_linear,
    B_linear: transmission.B_linear,
  };
}

function swatchAppearanceDomain(sw) {
  return sw?.appearance || null;
}

function rgbValuesToHex(r, g, b) {
  const values = [r, g, b].map((value) => Number(value));
  if (values.some((value) => !Number.isFinite(value))) return "";
  return "#" + values
    .map((value) => Math.max(0, Math.min(255, Math.round(value))).toString(16).padStart(2, "0"))
    .join("")
    .toUpperCase();
}

function swatchAppearanceHex(sw) {
  const appearance = swatchAppearanceDomain(sw);
  return appearance ? rgbValuesToHex(appearance.jpeg_r, appearance.jpeg_g, appearance.jpeg_b) : "";
}

function formatSwatchNumber(value, n = 4) {
  const num = Number(value);
  return Number.isFinite(num) ? sigfig(num, n) : "—";
}

function formatDisplayRgb(display) {
  if (display.R == null || display.G == null || display.B == null) return "—";
  return `${Math.round(Number(display.R))}, ${Math.round(Number(display.G))}, ${Math.round(Number(display.B))}`;
}

function buildMeasuredSwatchStripHtml(exp, options = {}) {
    const domain = options.domain || "display";
    const swatches = exp._measurements?.swatches || [];
    const { ratio, borderMm, stepWMm, stepHMm, n } = sampleStripMetrics(exp);
    if (swatches.length === 0) {
      return `
        <div class="sample-strip-frame" data-strip-render-frame="${exp.sample_id}" style="--strip-ratio:${ratio}">
          <div class="sample-render-stage is-empty sample-render-stage-sync"
               data-strip-render="${exp.sample_id}"
               data-border-mm="${borderMm}"
               data-step-w-mm="${stepWMm}"
               data-step-h-mm="${stepHMm}"
               data-swatches="${n}">
            <span class="small-copy">No swatches</span>
          </div>
        </div>
      `;
    }
    const cells = swatches.map((sw, index) => {
      const hex = domain === "appearance" ? swatchAppearanceHex(sw) : swatchDisplayDomain(sw).hex;
      return `<div class="sample-render-swatch${index > 0 ? " has-divider" : ""}${hex ? "" : " is-missing"}" style="background:${hex || "#d8d5cc"}"></div>`;
    }).join("");
    return `
      <div class="sample-strip-frame" data-strip-render-frame="${exp.sample_id}" style="--strip-ratio:${ratio}">
        <div class="sample-render-stage sample-render-stage-sync"
             data-strip-render="${exp.sample_id}"
             data-border-mm="${borderMm}"
             data-step-w-mm="${stepWMm}"
             data-step-h-mm="${stepHMm}"
             data-swatches="${n}">
          <div class="sample-render-shell" style="grid-template-columns:repeat(${Math.max(n, 1)}, minmax(0, 1fr))">
            ${cells}
          </div>
        </div>
      </div>
    `;
  }

  function applySampleStripGeometry(img) {
    const sid = img?.dataset?.stripSource;
    if (!sid || !img.naturalWidth || !img.naturalHeight) return;
    const renderFrames = Array.from(detailSidebar.querySelectorAll(`[data-strip-render-frame="${sid}"]`));
    const sourceFrame = detailSidebar.querySelector(`[data-strip-source-frame="${sid}"]`);
    const renderStages = Array.from(detailSidebar.querySelectorAll(`[data-strip-render="${sid}"]`));
    const metricStage = renderStages[0];
    if (!renderFrames.length || !sourceFrame || !metricStage) return;

    const sw = Number(img.naturalWidth);
    const sh = Number(img.naturalHeight);
    const borderMm = Number(metricStage.dataset.borderMm || 3);
    const stepWMm = Number(metricStage.dataset.stepWMm || 12);
    const stepHMm = Number(metricStage.dataset.stepHMm || 20);
    const n = Number(metricStage.dataset.swatches || 8);
    const deskewPad = 6;
    const totalWmm = (2 * borderMm) + (n * stepWMm);
    const plasticWPx = Math.max(1, sw - 2 * deskewPad);
    const pxPerMm = plasticWPx / totalWmm;
    const innerX = Math.round(deskewPad + borderMm * pxPerMm);
    const innerY = Math.round(deskewPad + borderMm * pxPerMm);
    const innerW = Math.round(n * stepWMm * pxPerMm);
    const innerH = Math.round(stepHMm * pxPerMm * 0.95);

    const ratio = sw / sh;
    const topPct = innerY / sh;
    const heightPct = innerH / sh;
    const bottomPct = Math.max(0, 1 - topPct - heightPct);
    sourceFrame.style.setProperty("--strip-ratio", ratio);
    renderFrames.forEach((renderFrame) => {
      renderFrame.style.setProperty("--strip-ratio", ratio);
      const frameHeight = renderFrame.getBoundingClientRect().height || 84;
      renderFrame.style.setProperty("--render-frame-top-gap", `${topPct * frameHeight}px`);
      renderFrame.style.setProperty("--render-frame-bottom-gap", `${bottomPct * frameHeight}px`);
    });
    renderStages.forEach((renderStage) => {
      renderStage.style.setProperty("--render-left", `${(innerX / sw) * 100}%`);
      renderStage.style.setProperty("--render-top", `${(innerY / sh) * 100}%`);
      renderStage.style.setProperty("--render-width", `${(innerW / sw) * 100}%`);
      renderStage.style.setProperty("--render-height", `${(innerH / sh) * 100}%`);
    });
  }

  function bindSampleStripGeometry() {
    detailSidebar.querySelectorAll("img[data-strip-source]").forEach((img) => {
      if (img.complete && img.naturalWidth) {
        applySampleStripGeometry(img);
      } else {
        img.addEventListener("load", () => applySampleStripGeometry(img), { once: true });
      }
    });
  }

function buildSampleMeasurementsTable(exp, options = {}) {
  const editableFit = !!options.editableFit;
  const swatches = [...(exp._measurements?.swatches || [])].sort((a, b) => {
    const ai = Number(a.swatch_index ?? Number.MAX_SAFE_INTEGER);
    const bi = Number(b.swatch_index ?? Number.MAX_SAFE_INTEGER);
    if (ai !== bi) return ai - bi;
    return Number(a.nominal_thickness_mm ?? 0) - Number(b.nominal_thickness_mm ?? 0);
  });
  if (swatches.length === 0) {
    return `<p class="small-copy">No per-swatch measurements are available for this sample yet.</p>`;
  }

  const headers = swatches.map((sw) => `
    <th class="sample-detail-colhead">${Number(sw.nominal_thickness_mm ?? 0).toFixed(2)}</th>
  `).join("");

  const buildDataRow = (label, cellClass, renderCell) => `
    <tr>
      <th scope="row" class="sample-detail-rowhead">${label}</th>
      ${swatches.map((sw) => `<td class="${cellClass}">${renderCell(sw)}</td>`).join("")}
    </tr>
  `;

  const buildSectionRow = (label) => `
    <tr class="sample-detail-section-row">
      <th colspan="${swatches.length + 1}">${label}</th>
    </tr>
  `;

  const rows = [
    buildSectionRow("Appearance Domain"),
    buildDataRow("HEX", "sample-detail-value", (sw) => {
      const hex = swatchAppearanceHex(sw);
      return hex ? _escHtml(hex) : `<span class="sample-detail-muted">—</span>`;
    }),
    buildDataRow("R", "sample-detail-value", (sw) => {
      const appearance = swatchAppearanceDomain(sw);
      return appearance ? formatSwatchNumber(appearance.jpeg_r) : `<span class="sample-detail-muted">—</span>`;
    }),
    buildDataRow("G", "sample-detail-value", (sw) => {
      const appearance = swatchAppearanceDomain(sw);
      return appearance ? formatSwatchNumber(appearance.jpeg_g) : `<span class="sample-detail-muted">—</span>`;
    }),
    buildDataRow("B", "sample-detail-value", (sw) => {
      const appearance = swatchAppearanceDomain(sw);
      return appearance ? formatSwatchNumber(appearance.jpeg_b) : `<span class="sample-detail-muted">—</span>`;
    }),
    buildSectionRow("Transmission domain"),
    buildDataRow("T<sub>R</sub>", "sample-detail-value", (sw) => formatSwatchNumber(swatchTransmissionDomain(sw).R_linear)),
    buildDataRow("T<sub>G</sub>", "sample-detail-value", (sw) => formatSwatchNumber(swatchTransmissionDomain(sw).G_linear)),
    buildDataRow("T<sub>B</sub>", "sample-detail-value", (sw) => formatSwatchNumber(swatchTransmissionDomain(sw).B_linear)),
    buildSectionRow("Model fit controls"),
    buildDataRow("Fit", "sample-detail-fit", (sw) => {
      const isExcluded = sw.fit_state === "excluded";
      const swatchIndex = Number(sw.swatch_index);
      if (editableFit && Number.isInteger(swatchIndex)) {
        return `
          <button type="button"
                  class="sample-swatch-fit-toggle ${isExcluded ? "is-excluded" : "is-included"}"
                  data-swatch-index="${swatchIndex}"
                  aria-pressed="${isExcluded ? "false" : "true"}"
                  title="${isExcluded ? "Excluded from model fits" : "Included in model fits"}">
            ${isExcluded ? "Excl" : "Incl"}
          </button>
        `;
      }
      return `<span class="sample-fit-cell ${isExcluded ? "is-excluded" : "is-included"}">${isExcluded ? "Ext" : "Inc"}</span>`;
    }),
  ].join("");

  return `
    <table class="data-table compact-table sample-detail-table sample-detail-table-transposed">
      <thead>
        <tr>
          <th class="sample-detail-corner">mm</th>
          ${headers}
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function sampleThumbnailUrl(sampleId, kind, bustCache = false) {
  const base = `/api/thumbnails/${sampleId}/${kind}`;
  if (bustCache) return `${base}?t=${Date.now()}`;
  const key = `${sampleId}:${kind}`;
  const bump = maintenanceCacheBust.allSampleThumbnails || maintenanceCacheBust.sampleThumbnails.get(key);
  return bump ? `${base}?maintenance_v=${encodeURIComponent(String(bump))}` : base;
}

function buildSampleSidebarBlock(title, bodyHtml, options = {}) {
  const classes = options.classes ? ` ${options.classes}` : "";
  const bodyClass = options.bodyClass ? ` ${options.bodyClass}` : "";
  const attributes = options.attributes ? ` ${options.attributes}` : "";
  const actionsHtml = options.actionsHtml
    ? `<div class="drawer-module-cap-actions">${options.actionsHtml}</div>`
    : "";
  return `
    <div class="sidebar-block drawer-module${classes}"${attributes}>
      <div class="drawer-module-cap">
        <span class="sidebar-label">${title}</span>
        ${actionsHtml}
      </div>
      <div class="drawer-module-body${bodyClass}">
        ${bodyHtml}
      </div>
    </div>
  `;
}

function buildDrawerFormModule(title, bodyHtml, options = {}) {
  const classes = ["drawer-form-module"];
  if (options.classes) classes.push(options.classes);
  const density = options.density || "compact";
  const bodyClasses = ["drawer-form-module-body"];
  if (density === "compact") {
    bodyClasses.push("drawer-module-body-compact");
  } else {
    bodyClasses.push(`drawer-module-body-${density}`);
  }
  if (options.bodyClass) bodyClasses.push(options.bodyClass);
  return buildSampleSidebarBlock(title, bodyHtml, {
    classes: classes.join(" "),
    bodyClass: bodyClasses.join(" "),
    actionsHtml: options.actionsHtml,
    attributes: options.attributes,
  });
}

function sampleSwatchFitExclusionSummary(exp) {
  const indexes = new Set();
  (exp._excluded_swatches || []).forEach((idx) => {
    const n = Number(idx);
    if (Number.isInteger(n)) indexes.add(n);
  });
  const swatches = exp._measurements?.swatches || [];
  swatches.forEach((sw) => {
    if (sw && sw.fit_state === "excluded") {
      const n = Number(sw.swatch_index);
      if (Number.isInteger(n)) indexes.add(n);
    }
  });
  const excludedIndexes = Array.from(indexes).sort((a, b) => a - b);
  const totalSwatches = Number(exp._n_swatches || swatches.length || (exp.variable_thicknesses_mm || []).length || 0);
  const excludedCount = Math.max(Number(exp._n_excluded || 0), excludedIndexes.length);
  return { totalSwatches, excludedCount, excludedIndexes };
}

function buildSampleSwatchFitHook(exp) {
  const summary = sampleSwatchFitExclusionSummary(exp);
  const indexes = summary.excludedIndexes.join(",");
  return `<div class="sample-swatch-fit-hook" hidden data-sample-id="${_escHtml(exp.sample_id || "")}" data-total-swatches="${summary.totalSwatches}" data-excluded-count="${summary.excludedCount}" data-excluded-swatches="${_escHtml(indexes)}"></div>`;
}

function setSwatchFitToggleVisual(button, isExcluded) {
  if (!button) return;
  button.classList.toggle("is-excluded", isExcluded);
  button.classList.toggle("is-included", !isExcluded);
  button.setAttribute("aria-pressed", isExcluded ? "false" : "true");
  button.title = isExcluded ? "Excluded from model fits" : "Included in model fits";
  button.textContent = isExcluded ? "Excl" : "Incl";
}

function syncSampleSwatchFitHooks(exp) {
  const summary = sampleSwatchFitExclusionSummary(exp);
  const indexes = summary.excludedIndexes.join(",");
  detailSidebar.querySelectorAll(".sample-swatch-fit-hook").forEach((hook) => {
    if (hook.dataset.sampleId !== (exp.sample_id || "")) return;
    hook.dataset.totalSwatches = String(summary.totalSwatches);
    hook.dataset.excludedCount = String(summary.excludedCount);
    hook.dataset.excludedSwatches = indexes;
  });
}

function bindSampleSwatchFitToggles(exp) {
  detailSidebar.querySelectorAll(".sample-swatch-fit-toggle").forEach((button) => {
    button.addEventListener("click", async () => {
      const swatchIndex = Number(button.dataset.swatchIndex);
      if (!Number.isInteger(swatchIndex)) return;

      const wasExcluded = button.classList.contains("is-excluded");
      const nextExcluded = !wasExcluded;
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "Saving";

      try {
        let result;
        if (nextExcluded) {
          result = await excludeSwatch(exp.sample_id, swatchIndex, "");
        } else {
          result = await includeSwatch(exp.sample_id, swatchIndex);
        }
        applyFitControlMutationResponse(result);

        const swatches = exp._measurements?.swatches || [];
        const swatch = swatches.find((sw) => Number(sw.swatch_index) === swatchIndex);
        if (swatch) {
          swatch.fit_state = nextExcluded ? "excluded" : "included";
          if (!nextExcluded) swatch.exclusion_reason = "";
        }
        const excluded = new Set((exp._excluded_swatches || []).map((idx) => Number(idx)));
        if (nextExcluded) excluded.add(swatchIndex);
        else excluded.delete(swatchIndex);
        exp._excluded_swatches = Array.from(excluded).filter(Number.isInteger).sort((a, b) => a - b);
        exp._n_excluded = exp._excluded_swatches.length;

        setSwatchFitToggleVisual(button, nextExcluded);
        syncSampleSwatchFitHooks(exp);
        showProfileToast(`Swatch ${swatchIndex + 1} ${nextExcluded ? "excluded" : "included"}`);
      } catch (err) {
        button.textContent = originalText;
        showProfileToast(err.message || "Failed to update swatch fit state");
      } finally {
        button.disabled = false;
      }
    });
  });
}

function buildSampleCompactSidebarHtml(exp, media, options = {}) {
  const filamentRows = sampleFilamentRoleLines(exp);
  const missingStructuredRoles = isStructuredGeometryBackend() && !(exp.roles || []).length;
  const filamentsHtml = filamentRows.map((line) => {
    return `
      <div class="drawer-subtitle${line.roleKind === "fixed" ? " drawer-subtitle-fixed" : ""}">
        <span class="color-chip" style="background:${line.hex}"></span>
        <span class="filament-name-text"><strong>${_escHtml(line.name)}</strong></span>
        <span class="muted-line">${_escHtml(line.layerLabel)}</span>
      </div>
    `;
  }).join("");
  const notesText = exp.notes || "";
  const notesDisplay = notesText.trim() ? notesText : "None";
  const sampleFitExcluded = !!exp._fit_exclude;
  const stepId = sampleStepId(exp);
  const step = stepRecordByRef(stepId);
  const geometryLabel = _geometryAliasFromRef(stepId) || sampleStepFileName(exp);
  const artifactSummary = sampleGeometryArtifactSummary(step);
  const showInspectLinks = options.inspectLinks !== false;
  const geometryInspectButton = showInspectLinks && step
    ? drawerInspectButtonHtml({
      title: "Inspect sample geometry",
      attributes: `data-inspect-sample-geometry="${_escAttr(stepId)}"`,
    })
    : "";
  const modelFitInspectButton = showInspectLinks && sampleHasModelingReviewDetail(exp)
    ? drawerInspectButtonHtml({
      title: "Inspect modeling sample",
      attributes: `data-inspect-sample-model="${_escAttr(exp.sample_id || "")}"`,
    })
    : "";
  return `
    ${buildSampleSidebarBlock(
      "Filaments",
      filamentsHtml || (missingStructuredRoles
        ? `<div class="strip-diagram-contract-error">Missing geometry role data</div>`
        : `<span class="muted-line">No filament assignments</span>`),
      { bodyClass: "drawer-module-body-compact" }
    )}
    ${buildSampleSidebarBlock("Images", buildCompactSampleMediaPair(exp, media), { bodyClass: "drawer-module-body-compact" })}
    ${buildSampleSidebarBlock("Strip", `<div class="sample-strip-tight">${buildStripMiniTable(exp)}</div>`, { bodyClass: "drawer-module-body-compact" })}
    ${buildSampleSidebarBlock("Sample Geometry", `
      <span class="muted-line step-name-display sample-geometry-alias-display">${_escHtml(geometryLabel)}</span>
      <span class="drawer-form-value sample-geometry-artifact-status">${_escHtml(artifactSummary)}</span>
    `, {
      bodyClass: "drawer-module-body-compact",
      actionsHtml: `${geometryInspectButton}<button class="ghost-button xs step-copy-button" data-copy="folder" data-step="${stepId}">Copy Path</button>`,
      classes: "sample-step-module",
    })}
    ${buildSampleSidebarBlock("Notes", `<span class="drawer-form-value sample-notes-display">${_escHtml(notesDisplay)}</span>`, {
      bodyClass: "drawer-module-body-compact",
      classes: "sample-notes-module",
    })}
    ${buildSampleSidebarBlock("Model Fit", `
      <span class="drawer-form-value sample-fit-status ${sampleFitExcluded ? "is-excluded" : "is-included"}">
        ${sampleFitExcluded ? "Excluded from model fits" : "Included in model fits"}
      </span>
      ${buildSampleSwatchFitHook(exp)}
    `, {
      bodyClass: "drawer-module-body-compact",
      actionsHtml: modelFitInspectButton,
      classes: "sample-fit-controls-module",
    })}
  `;
}

function sampleGeometryArtifactSummary(step) {
  if (!step) return "Geometry record missing";
  const summary = step?.artifact_summary || {};
  const stepCount = Array.isArray(summary.step_paths)
    ? summary.step_paths.length
    : (step?.artifact_path ? 1 : 0);
  const stlCount = Array.isArray(summary.stl_paths) ? summary.stl_paths.length : 0;
  if (summary.manifest_error) return "Artifact manifest unreadable";
  const parts = [];
  if (stepCount > 0) parts.push(`${stepCount} STEP`);
  if (stlCount > 0) parts.push(`${stlCount} STL`);
  if (!parts.length) return "No managed artifacts yet";
  const manifestStatus = summary.manifest_exists ? "manifest present" : "manifest missing";
  return `Managed assets: ${parts.join(", ")}; ${manifestStatus}`;
}

function buildSampleExpandedView(exp, media) {
  return `
    <div class="sample-expanded-shell">
      <div class="sample-expanded-left compact-sidebar-stack">
        ${buildSampleCompactSidebarHtml(exp, media)}
      </div>
      <div class="sample-expanded-right">
        ${buildSampleExpandedAnalysisPane(exp)}
      </div>
    </div>
  `;
}

function buildSampleExpandedAnalysisPane(exp) {
  const sid = exp.sample_id;
  const processedLike = ["processed", "flagged", "failed"].includes(exp._processing_status);
  const { ratio } = sampleStripMetrics(exp);
  const stripSrc = processedLike ? sampleThumbnailUrl(sid, "strip") : null;
  const hasAppearanceSwatches = (exp._measurements?.swatches || []).some((sw) => !!swatchAppearanceDomain(sw));

  return `
    ${buildSampleSidebarBlock("Swatch Strip Comparison", `
      <div class="sample-photo-panel">
        <div class="sample-photo-panel-top">
          <div class="sample-strip-row">
            <div class="sample-strip-label-bubble">Extracted<br>Strip</div>
            <div class="sample-strip-row-content">
              <div class="sample-strip-frame" data-strip-source-frame="${sid}" style="--strip-ratio:${ratio}">
                ${buildLightboxThumbButton({
                  src: stripSrc,
                  alt: `${sid} extracted strip`,
                  title: `${sid} extracted strip`,
                  emptyLabel: "No strip",
                  imgClass: "drawer-thumb drawer-thumb-strip",
                  imgAttrs: `data-strip-source="${sid}"`,
                })}
              </div>
            </div>
          </div>
          ${hasAppearanceSwatches ? `
          <div class="sample-strip-row">
            <div class="sample-strip-label-bubble">Extracted<br>Appearance</div>
            <div class="sample-strip-row-content">
              ${buildMeasuredSwatchStripHtml(exp, { domain: "appearance" })}
            </div>
          </div>
          ` : ""}
          <div class="sample-strip-row">
            <div class="sample-strip-label-bubble">Extracted<br>Transmission</div>
            <div class="sample-strip-row-content">
              ${buildMeasuredSwatchStripHtml(exp, { domain: "display" })}
            </div>
          </div>
        </div>
      </div>
    `, { classes: "sample-evidence-panel" })}
    ${buildSampleSidebarBlock("Per-Swatch Data", buildSampleMeasurementsTable(exp, {
      editableFit: _sampleDrawerMode === "edit",
    }), { classes: "sample-swatches-panel" })}
  `;
}

function buildSampleInspectFrameHtml(contentHtml, expanded) {
  return `
    <div class="sample-inspect-frame">
      <div class="sample-inspect-main">
        ${contentHtml}
      </div>
    </div>
  `;
}

function stepFilenameFromRef(stepRef = "") {
  const normalized = String(stepRef || "").replace(/\\/g, "/");
  const parts = normalized.split("/");
  return parts[parts.length - 1] || "";
}

function resolveStepLibraryFolderPath() {
  if (_serverConfig?.step_export_path) return _serverConfig.step_export_path;
  if (_serverConfig?.step_library_path) return _serverConfig.step_library_path;
  if (_serverConfig?.data_root) {
    const prismaRoot = _serverConfig.data_root.replace(/[\\\/]data$/, "");
    if (prismaRoot && prismaRoot !== _serverConfig.data_root) {
      const sep = prismaRoot.includes("\\") ? "\\" : "/";
      return `${prismaRoot}${sep}output${sep}steps`;
    }
  }
  return "";
}

function resolveStepClipboardPath(stepRef, mode = "folder") {
  const folderPath = resolveStepLibraryFolderPath();
  if (mode !== "file") return folderPath;
  const filename = stepFileNameFromRef(stepRef);
  if (!folderPath || !filename) return "";
  const sep = folderPath.includes("\\") ? "\\" : "/";
  return `${folderPath}${sep}${filename}`;
}

function sampleWindowToggleButtonHtml(expanded) {
  const sampleWindowLabel = expanded ? "Compact sample drawer" : "Expand sample drawer";
  const sampleWindowIcon = expanded
    ? `
      <svg viewBox="0 0 14 14" aria-hidden="true" focusable="false">
        <path d="M3 5.25H8.75V11H3Z"></path>
        <path d="M5.25 3H11V8.75H9.25"></path>
      </svg>
    `
    : `
      <svg viewBox="0 0 14 14" aria-hidden="true" focusable="false">
        <path d="M3 3H11V11H3Z"></path>
      </svg>
    `;

  return `
    <button class="ghost-button xs drawer-header-action drawer-window-button" id="toggleSampleInspectBtn" type="button" aria-pressed="${expanded ? "true" : "false"}" aria-label="${sampleWindowLabel}" title="${sampleWindowLabel}">
      ${sampleWindowIcon}
    </button>
  `;
}

function drawerInspectButtonHtml({ id = "", label = "Inspect", title = "Inspect related record", attributes = "" } = {}) {
  const idAttr = id ? ` id="${_escAttr(id)}"` : "";
  const titleAttr = title ? ` title="${_escAttr(title)}" aria-label="${_escAttr(title)}"` : "";
  return `
    <button class="drawer-inspect-button" type="button"${idAttr}${titleAttr} ${attributes}>
      <svg viewBox="0 0 14 14" aria-hidden="true" focusable="false">
        <circle cx="6" cy="6" r="3.5"></circle>
        <path d="M8.7 8.7L12 12"></path>
      </svg>
      <span>${_escHtml(label)}</span>
    </button>
  `;
}

function sampleInspectReturnContext(exp, expanded = false) {
  return {
    sampleId: exp?.sample_id || "",
    expanded: !!expanded,
    mode: currentMode || "logbook",
    subtab: currentSubtab || "",
  };
}

function sampleHasModelingReviewDetail(exp = {}) {
  const swatchCount = Number(exp._n_swatches || exp._measurements?.swatches?.length || 0);
  const processingStatus = exp._processing_status || exp.processing_status || (exp.processed ? "processed" : "");
  const acceptedKnown = exp._review_accepted != null || exp.review_accepted != null;
  const accepted = acceptedKnown ? Boolean(exp._review_accepted ?? exp.review_accepted) : Boolean(exp.processed);
  return Boolean(exp.sample_id) && accepted && processingStatus === "processed" && swatchCount > 0;
}

function returnToSampleInspectDrawer(context = {}) {
  const sampleId = context.sampleId || "";
  const exp = data.samples.find((item) => item.sample_id === sampleId);
  if (!exp) {
    showProfileToast("Sample is no longer available");
    return;
  }
  currentMode = context.mode || "logbook";
  currentSubtab = context.subtab || "";
  renderWorkspace();
  renderSidebarForSample(exp, { expanded: !!context.expanded });
  const row = tableContainer.querySelector(`.data-row[data-kind="sample"][data-id="${CSS.escape(sampleId)}"]`);
  row?.classList.add("is-selected");
}

function bindStepCopyButtons(root = detailSidebar) {
  root?.querySelectorAll(".step-copy-button, .copy-pill[data-step], .copy-pill[data-copy-text]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const stepFile = btn.dataset.step;
      const text = btn.dataset.copyText || resolveStepClipboardPath(stepFile, btn.dataset.copy || "folder");
      if (!text) { showProfileToast("Path not available"); return; }
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        const orig = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => { btn.textContent = orig; }, 1200);
      } catch (e) {
        console.error("Clipboard copy failed:", e, "Text was:", text);
        showProfileToast("Clipboard copy failed — check console");
      }
    });
  });
}

function bindSampleInspectLinks(root = detailSidebar, exp = {}, options = {}) {
  const expanded = !!options.expanded;
  root?.querySelector("[data-inspect-sample-geometry]")?.addEventListener("click", () => {
    const stepId = sampleStepId(exp);
    if (!stepId || !stepRecordByRef(stepId)) {
      showProfileToast("Geometry record not found");
      return;
    }
    currentMode = "geometries";
    currentSubtab = "";
    renderWorkspace();
    renderStepDetailDrawer(stepId, { returnSampleContext: sampleInspectReturnContext(exp, expanded) });
  });

  root?.querySelector("[data-inspect-sample-model]")?.addEventListener("click", async (event) => {
    if (!sampleHasModelingReviewDetail(exp)) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      currentMode = "profiles";
      currentSubtab = "samples";
      if (!modelingState.samples) {
        await loadModelingTab("samples", { force: true });
      }
      renderWorkspace();
      await openModelingSampleDetailDrawer(exp.sample_id, button, {
        returnSampleContext: sampleInspectReturnContext(exp, expanded),
      });
    } catch (err) {
      showProfileToast(err.message || "Could not open modeling sample detail");
    } finally {
      if (document.body.contains(button)) button.disabled = false;
    }
  });
}

function renderLinkedSampleDrawer(exp) {
  if (!linkedSampleDrawer || !linkedSampleSidebar) return;
  const media = resolveSampleMedia(exp);
  const status = sampleStatusMeta(exp);
  linkedSampleHeading.textContent = exp.sample_id;
  linkedSampleStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
  linkedSampleActionArea.innerHTML = "";
  linkedSampleWindowArea.innerHTML = "";
  linkedSampleSidebar.innerHTML = buildSampleInspectFrameHtml(buildSampleCompactSidebarHtml(exp, media, { inspectLinks: false }), false);
  bindStepCopyButtons(linkedSampleSidebar);
  bindDrawerLightboxButtons(linkedSampleSidebar);
}

function openLinkedSampleDrawer(sampleId, options = {}) {
  if (!linkedSampleDrawer || !linkedSampleSidebar || !canOpenLinkedSampleDrawer()) return;
  const exp = data.samples.find((item) => item.sample_id === sampleId);
  if (!exp) return;

  _linkedSampleDrawerState.sampleId = sampleId;
  _linkedSampleDrawerState.returnFocusEl = options.returnFocusEl || document.activeElement;

  renderLinkedSampleDrawer(exp);
  syncLinkedSampleDrawerPosition();
  linkedSampleDrawer.classList.add("is-open");
  linkedSampleDrawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("record-drawer-open");
  if (document.body?.classList.contains("using-keyboard-nav")) {
    closeLinkedSampleDrawerBtn?.focus();
  }
}

function closeLinkedSampleDrawer(options = {}) {
  const restoreFocus = options.restoreFocus !== false;
  if (!linkedSampleDrawer) return;

  linkedSampleDrawer.classList.remove("is-open");
  linkedSampleDrawer.setAttribute("aria-hidden", "true");
  linkedSampleDrawer.style.removeProperty("right");
  linkedSampleDrawer.style.removeProperty("width");
  linkedSampleDrawer.style.removeProperty("--linked-drawer-shift");
  linkedSampleStatusPill.innerHTML = "";
  linkedSampleActionArea.innerHTML = "";
  linkedSampleWindowArea.innerHTML = "";
  linkedSampleSidebar.innerHTML = `
    <p class="small-copy">
      Select a sample from a Used By list to inspect it here.
    </p>
  `;

  const focusTarget = _linkedSampleDrawerState.returnFocusEl;
  _linkedSampleDrawerState.sampleId = null;
  _linkedSampleDrawerState.returnFocusEl = null;

  if (restoreFocus && focusTarget instanceof HTMLElement && focusTarget.isConnected && focusTarget.getAttribute("aria-disabled") !== "true") {
    focusTarget.focus();
  }
}

function bindLinkedSampleTriggers(root = detailSidebar) {
  root?.querySelectorAll("[data-linked-sample]").forEach((node) => {
    node.addEventListener("click", () => {
      if (!canOpenLinkedSampleDrawer()) return;
      openLinkedSampleDrawer(node.dataset.linkedSample, { returnFocusEl: node });
    });
    node.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (!canOpenLinkedSampleDrawer()) return;
      e.preventDefault();
      openLinkedSampleDrawer(node.dataset.linkedSample, { returnFocusEl: node });
    });
  });
  updateLinkedSampleTriggers(root);
}

function renderSidebarForSample(exp, options = {}) {
  setDetailSidebarStackMode("default");
  selectedRecord = { kind: "sample", id: exp.sample_id };
  geometryDetailReturnSampleContext = null;
  modelingState.sampleDetailReturnSampleContext = null;
  _sampleDrawerMode = null;
  const expanded = options.expanded != null ? !!options.expanded : _sampleInspectExpanded;
  setSampleInspectExpandedPreference(expanded);
  if (expanded && sampleHasMeasurementOutput(exp) && !exp._measurements) {
    ensureMeasurementsThenRerender([exp.sample_id], () => {
      if (
        _sampleDrawerMode === null &&
        selectedRecord.kind === "sample" &&
        selectedRecord.id === exp.sample_id &&
        recordDrawer?.classList.contains("is-open")
      ) {
        const hydrated = data.samples.find((item) => item.sample_id === exp.sample_id) || exp;
        renderSidebarForSample(hydrated, { expanded: _sampleInspectExpanded });
      }
    });
  }
  recordDrawer.classList.remove("narrow-drawer");
  recordDrawer.classList.remove("sample-set-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  recordDrawer.classList.toggle("sample-expanded", expanded);
  _filamentDrawerMode = null;
  _filamentDrawerData = null;
  setDrawerHeading(exp.sample_id);
  drawerStatusPill.innerHTML = "";
  const status = sampleStatusMeta(exp);
  drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
  detailActionArea.innerHTML = `
    <button class="ghost-button xs drawer-header-action" id="editSampleBtn">Edit</button>
  `;
  detailWindowArea.innerHTML = sampleWindowToggleButtonHtml(expanded);
  const media = resolveSampleMedia(exp);
  const sampleBodyHtml = expanded
    ? buildSampleExpandedView(exp, media)
    : buildSampleCompactSidebarHtml(exp, media);
  detailSidebar.innerHTML = buildSampleInspectFrameHtml(sampleBodyHtml, expanded);
  openRecordDrawer();
  bindStepCopyButtons(detailSidebar);
  bindDrawerLightboxButtons(detailSidebar);
  bindSampleInspectLinks(detailSidebar, exp, { expanded });
  bindSampleStripGeometry();

  document.getElementById("toggleSampleInspectBtn")?.addEventListener("click", () => {
    renderSidebarForSample(exp, { expanded: !expanded });
  });

  document.getElementById("editSampleBtn")?.addEventListener("click", () => openSampleEditDrawer(exp, { expanded }));

}

function renderStepDetailDrawer(stepId, options = {}) {
  recordDrawer.classList.remove("narrow-drawer");
  recordDrawer.classList.remove("sample-expanded");
  recordDrawer.classList.remove("sample-set-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  _filamentDrawerMode = null;
  _filamentDrawerData = null;

  const canonicalStepId = stepIdFromRef(stepId);
  selectedRecord = { kind: "step", id: canonicalStepId };
  if (options.returnSampleContext) {
    geometryDetailReturnSampleContext = options.returnSampleContext;
  } else if (!options.preserveReturn) {
    geometryDetailReturnSampleContext = null;
  }
  const step = stepRecordByRef(stepId);
  const meta = stepMeta(canonicalStepId);
  const usedBy = data.samples.filter((exp) => {
    return sampleStepId(exp) === canonicalStepId;
  });
  const displayName = meta.alias || (step?.file_name || canonicalStepId).replace(/_/g, "_\u200B");
  setDetailSidebarStackMode("form");
  setDrawerHeading(displayName, { html: true });
  drawerStatusPill.innerHTML = "";
  detailWindowArea.innerHTML = "";
  const returnButton = geometryDetailReturnSampleContext ? `
    <button class="secondary-button small drawer-header-action" id="stepReturnSampleBtn" type="button">Return to Sample</button>
  ` : "";
  detailActionArea.innerHTML = `
    ${returnButton}
    ${isStructuredGeometryBackend() ? `<button class="ghost-button small drawer-header-action" id="exportStepArtifactBtn">Export</button>` : ""}
    <button class="ghost-button small drawer-header-action" id="editStepBtn">Edit</button>
  `;

  const sampleRows = usedBy.map((exp) => ({
    sampleId: exp.sample_id,
    lines: sampleMaterialLines(exp),
    status: sampleStatusMeta(exp),
  }));
  const artifactHtml = buildGeometryArtifactDetailHtml(step, canonicalStepId);
  const exportHtml = buildGeometryExportDetailHtml(step);

  detailSidebar.innerHTML = `
    ${buildDrawerFormModule("Alias", `
      <span class="step-view-field step-field-slot drawer-form-value" id="stepAliasView">${meta.alias || "—"}</span>
      <input type="text" class="step-edit-field step-field-slot step-detail-input" id="stepAliasInput" value="${meta.alias}" placeholder="e.g. thin over white" style="display:none" />
    `, { density: "form" })}
    ${buildDrawerFormModule("ID", `<span class="mono muted-line drawer-form-value drawer-break-all">${canonicalStepId}</span>`, { density: "compact" })}
    ${buildDrawerFormModule("Bundle", `
      <div id="stepBundleChips"></div>
      <div class="step-bundle-add-row">
        <select id="stepBundleAddSelect"><option value="">---none---</option></select>
        <button class="bundle-chip-remove step-bundle-placeholder-x" type="button" style="visibility:hidden" aria-hidden="true">
          <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M2.5 2.5L9.5 9.5"></path>
            <path d="M9.5 2.5L2.5 9.5"></path>
          </svg>
        </button>
      </div>
    `, { density: "form" })}
          ${buildDrawerFormModule("Last Modified", `<span class="drawer-form-value">${step?.last_write_time || "—"}</span>`, { density: "compact" })}
          ${buildDrawerFormModule(`Used By ${usedBy.length} Sample${usedBy.length === 1 ? "" : "s"}`, buildCompactUsedByList(sampleRows, "No samples use this geometry"), { density: "table" })}
          ${buildDrawerFormModule("Exported Files", exportHtml, { density: "compact" })}
          ${buildDrawerFormModule("Managed Artifacts", artifactHtml, { density: "compact" })}
          <div class="delete-notice" id="stepDeleteNotice"></div>
        `;
  bindStepMetaForm(canonicalStepId);
  bindStepArtifactActions(canonicalStepId);
  openRecordDrawer();
  bindStepCopyButtons(detailSidebar);
  bindLinkedSampleTriggers(detailSidebar);
}

function buildGeometryArtifactDetailHtml(step) {
  const summary = step?.artifact_summary || {};
  const stepCount = Array.isArray(summary.step_paths) && summary.step_paths.length
    ? summary.step_paths.length
    : (step?.artifact_path ? 1 : 0);
  const stlCount = Array.isArray(summary.stl_paths) ? summary.stl_paths.length : 0;
  const bodyCount = Array.isArray(summary.body_names) ? summary.body_names.length : 0;
  const rows = [];
  if (stepCount > 0) rows.push({ label: "STEP", value: `${stepCount} managed file${stepCount === 1 ? "" : "s"}` });
  if (stlCount > 0) rows.push({ label: "STL", value: `${stlCount} managed file${stlCount === 1 ? "" : "s"}` });
  if (bodyCount > 0) rows.push({ label: "Bodies", value: `${bodyCount} labeled solid${bodyCount === 1 ? "" : "s"}` });
  if (summary.manifest_error) rows.push({ label: "Manifest", value: "Unreadable" });
  else if (stepCount || stlCount) rows.push({ label: "Manifest", value: summary.manifest_exists ? "Present" : "Missing" });
  if (!rows.length) {
    return `<span class="drawer-form-value">No managed artifacts yet</span>`;
  }
  return `
    <div class="artifact-path-list">
      ${rows.map((row) => `
        <div class="artifact-summary-row">
          <span class="artifact-kind">${row.label}</span>
          <span class="muted-line drawer-form-value">${escapeHtml(row.value)}</span>
        </div>
      `).join("")}
    </div>
  `;
}

function buildGeometryExportDetailHtml(step) {
  const summary = step?.artifact_summary || {};
  const rows = [];
  if (summary.latest_step_export_path) {
    rows.push({ label: "STEP", path: summary.latest_step_export_path });
  }
  if (summary.latest_stl_export_path) {
    const stlCount = Number(summary.latest_stl_export_file_count || 0);
    rows.push({
      label: stlCount > 1 || summary.latest_stl_export_kind === "folder" ? "STLs" : "STL",
      path: summary.latest_stl_export_path,
    });
  }
  if (!rows.length) {
    const exportPaths = Array.isArray(summary.export_paths)
      ? summary.export_paths.filter((path) => !!path)
      : [];
    if (!exportPaths.length && step?.export_path) exportPaths.push(step.export_path);
    exportPaths.forEach((path) => {
      rows.push({ label: String(path).toLowerCase().endsWith(".stl") ? "STL" : "STEP", path });
    });
  }
  if (!rows.length) {
    return `<span class="drawer-form-value">No user-facing exports yet</span>`;
  }
  return `
    <div class="artifact-path-list">
      ${rows.map((row) => {
        const displayPath = displayPathFromPrismaRoot(row.path);
        return `
        <div class="artifact-path-row">
          <span class="artifact-kind">${row.label}</span>
          <span class="mono muted-line drawer-break-all" title="${escapeHtml(row.path)}">${escapeHtml(displayPath)}</span>
          <button class="copy-pill drawer-utility-button" type="button" data-copy-text="${escapeHtml(row.path)}">Copy Path</button>
        </div>
      `;
      }).join("")}
    </div>
  `;
}

function displayPathFromPrismaRoot(path) {
  const raw = String(path || "");
  const normalized = raw.replace(/\//g, "\\");
  const marker = "\\Prisma\\";
  const markerIndex = normalized.toLowerCase().lastIndexOf(marker.toLowerCase());
  if (markerIndex >= 0) {
    return normalized.slice(markerIndex + 1);
  }
  if (normalized.toLowerCase().startsWith("prisma\\")) {
    return normalized;
  }
  return raw;
}

function fixedSwatchIncrement(values = []) {
  if (!Array.isArray(values) || values.length < 2) return null;
  const nums = values.map((value) => numericValue(value, NaN));
  if (nums.some((value) => !Number.isFinite(value))) return null;
  const increment = nums[1] - nums[0];
  for (let index = 2; index < nums.length; index += 1) {
    if (Math.abs((nums[index] - nums[index - 1]) - increment) > 1e-6) return null;
  }
  return increment;
}

function bindStepArtifactActions(stepId) {
  const exportButton = document.getElementById("exportStepArtifactBtn");
  if (!exportButton) return;
  exportButton.addEventListener("click", () => {
    const step = stepRecordByRef(stepId);
    openGeometryExportDialog(stepId, step?.alias || stepId);
  });
}

function sortArrow(key) {
  if (sortState.key !== key) return "";
  return sortState.direction === "asc" ? " ↓" : " ↑";
}

function compareRows(a, b, key, direction) {
  const av = ((a[key] ?? "") + "").toLowerCase();
  const bv = ((b[key] ?? "") + "").toLowerCase();
  const cmp = av.localeCompare(bv, undefined, { numeric: true });
  return direction === "asc" ? cmp : -cmp;
}

function bindSortHeaders() {
  tableContainer.querySelectorAll(".sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (sortState.key === key) {
        sortState.direction = sortState.direction === "asc" ? "desc" : "asc";
      } else {
        sortState.key = key;
        sortState.direction = "asc";
      }
      renderWorkspace();
    });
  });
}

function bindFilterChips() {
  tableToolbar.querySelectorAll(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      logbookFilter = chip.dataset.filter;
      renderWorkspace();
    });
  });
}

function bindRowSelection() {
  tableContainer.querySelectorAll(".data-row").forEach((row) => {
    if (!row.dataset.kind || !row.dataset.id) return;
    row.addEventListener("click", () => {
      const isSameRecord = selectedRecord.kind === row.dataset.kind && selectedRecord.id === row.dataset.id;
      const drawerOpen = recordDrawer.classList.contains("is-open");
      const isSameSelection = isSameRecord && drawerOpen;

      if (isSameSelection) {
        selectedRecord = { kind: null, id: null };
        tableContainer.querySelectorAll(".data-row").forEach((node) => node.classList.remove("is-selected"));
        closeDrawer();
        return;
      }

      // Close step builder drawer if open — mutually exclusive with record drawer
      if (isStepBuilderOpen()) closeStepBuilderDrawer();
      // Close bundle management drawer if open
      if (isBundleMgmtOpen()) closeBundleMgmtDrawer();

      selectedRecord = { kind: row.dataset.kind, id: row.dataset.id };
      tableContainer.querySelectorAll(".data-row").forEach((node) => node.classList.remove("is-selected"));
      row.classList.add("is-selected");

      if (row.dataset.kind === "sample") {
        const exp = data.samples.find((item) => item.sample_id === row.dataset.id);
        if (exp) {
          renderSidebarForSample(exp);
        }
      } else if (row.dataset.kind === "filament") {
        recordDrawer.classList.remove("sample-expanded");
        recordDrawer.classList.remove("model-filament-drawer");
        const fil = data.filaments.find((item) => item.filament_id === row.dataset.id);
        if (fil) renderSidebarForFilament(fil);
      } else if (row.dataset.kind === "step") {
        renderStepDetailDrawer(row.dataset.id);
      } else {
        setDetailSidebarStackMode("default");
        recordDrawer.classList.remove("narrow-drawer");
        recordDrawer.classList.remove("sample-expanded");
        recordDrawer.classList.remove("sample-set-drawer");
        recordDrawer.classList.remove("model-filament-drawer");
        _filamentDrawerMode = null;
        _filamentDrawerData = null;
        setDrawerHeading(row.dataset.id);
        detailActionArea.innerHTML = "";
        detailWindowArea.innerHTML = "";
        detailSidebar.innerHTML = `<p class="small-copy">This row is connected to the real project data snapshot. The detail view can become richer as we decide what belongs on each record type.</p>`;
        openRecordDrawer();
      }
    });
  });
}

function openRecordDrawer() {
  syncRecordDrawerPosition();
  syncLinkedSampleDrawerPosition();
  recordDrawer.classList.add("is-open");
  recordDrawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("record-drawer-open");
  updateLinkedSampleTriggers(detailSidebar);
}

function closeDrawer() {
  closeLinkedSampleDrawer({ restoreFocus: false });
  recordDrawer.classList.remove("is-open");
  recordDrawer.classList.remove("narrow-drawer");
  recordDrawer.classList.remove("sample-expanded");
  recordDrawer.classList.remove("sample-set-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  setDetailSidebarStackMode("default");
  recordDrawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("record-drawer-open");
  drawerStatusPill.innerHTML = "";
  detailWindowArea.innerHTML = "";
  _filamentDrawerMode = null;
  _filamentDrawerData = null;
  _sampleDrawerMode = null;
  geometryDetailReturnSampleContext = null;
  modelingState.sampleDetailReturnSampleContext = null;
}

function clearSelectionAndDrawer() {
  selectedRecord = { kind: null, id: null };
  resetStepEditorState(null);
  tableContainer.querySelectorAll(".data-row").forEach((node) => node.classList.remove("is-selected"));
  closeDrawer();
}


function buildStepFilename() {
  const layerCount = 1 + stepBuilderState.fixedLayers.length;
  const variablePart = `v-${stepBuilderState.values.map((value) => formatStepNumber(value)).join("-")}`;
  const fixedThicknesses = getStepBuilderFixedThicknesses();
  const fixedPart = fixedThicknesses.length
    ? `_f-${fixedThicknesses.map((thickness) => formatStepNumber(thickness)).join("-")}`
    : "";
  const lhEl = _sbEl("stepLayerHeight");
  return `${layerCount}L_${variablePart}${fixedPart}_lh${formatStepNumber(lhEl ? lhEl.value : "0.08")}.step`;
}

function defaultStepBuilderValues(count = 8) {
  return Array.from({ length: count }, (_, index) => formatStepNumber(0.2 + 0.08 * index));
}

function makeStepBuilderRole(kind, values = null) {
  const role = {
    id: `role-${stepBuilderState.nextLayerRoleId++}`,
    kind,
  };
  if (kind === "variable") {
    role.values = Array.isArray(values) ? values.slice() : defaultStepBuilderValues(stepBuilderSwatchCount());
  } else {
    role.thickness = values == null ? "0.20" : String(values);
  }
  return role;
}

function initializeStepBuilderLayerRoles(values = null) {
  stepBuilderState.nextLayerRoleId = 1;
  stepBuilderState.layerRoles = [
    makeStepBuilderRole("variable", Array.isArray(values) ? values : defaultStepBuilderValues(8)),
  ];
  syncStepBuilderLegacyLayerState();
}

function stepBuilderVariableRole() {
  let variable = stepBuilderState.layerRoles.find((role) => role.kind === "variable");
  if (!variable) {
    variable = makeStepBuilderRole("variable", stepBuilderState.values);
    stepBuilderState.layerRoles.unshift(variable);
  }
  return variable;
}

function syncStepBuilderLegacyLayerState() {
  const variable = stepBuilderVariableRole();
  stepBuilderState.values = variable.values || [];
  stepBuilderState.fixedLayers = stepBuilderState.layerRoles
    .filter((role) => role.kind === "fixed")
    .slice()
    .reverse()
    .map((role) => ({ thickness: role.thickness, roleId: role.id }));
}

function getStepBuilderFixedThicknesses() {
  if (isStructuredGeometryBackend() && stepBuilderState.layerRoles.length) {
    syncStepBuilderLegacyLayerState();
  }
  return stepBuilderState.fixedLayers.map((layer) => layer.thickness);
}

function isStructuredGeometryBackend() {
  return (_serverConfig?.backend || "").toLowerCase() === "sqlite";
}

function stepBuilderSwatchCount() {
  const countEl = _sbEl("stepColumnCount");
  const raw = countEl ? Number.parseInt(countEl.value, 10) : stepBuilderState.values.length;
  return Math.max(1, Math.min(48, Number.isFinite(raw) ? raw : stepBuilderState.values.length || 8));
}

function updateStepBuilderDrawerWidth() {
  if (!stepBuilderDrawer) return;
  if (!isStructuredGeometryBackend()) {
    stepBuilderDrawer.style.removeProperty("--step-builder-width");
    stepBuilderDrawer.style.removeProperty("--step-builder-form-width");
    return;
  }
  const count = stepBuilderSwatchCount();
  const swatchGridWidth = count * 35 + Math.max(0, count - 1);
  const roleChromeWidth = 72 + 4 + 4 + 22 + 32;
  const formWidth = Math.max(520, Math.min(900, swatchGridWidth + roleChromeWidth));
  const visualWidth = 408;
  const columnGap = 8;
  const desired = Math.min(1320, formWidth + visualWidth + columnGap);
  stepBuilderDrawer.style.setProperty("--step-builder-form-width", `${formWidth}px`);
  stepBuilderDrawer.style.setProperty("--step-builder-width", `${desired}px`);
}

function resizeStepBuilderValues(count) {
  const normalized = Math.max(1, Math.min(48, Number.parseInt(count, 10) || 8));
  const variable = isStructuredGeometryBackend() && stepBuilderState.layerRoles.length
    ? stepBuilderVariableRole()
    : null;
  const source = variable ? (variable.values || []) : stepBuilderState.values;
  const current = source.slice(0, normalized);
  while (current.length < normalized) {
    const previous = current.length ? numericValue(current[current.length - 1], 0) : 0.2;
    current.push(formatStepNumber(previous + 0.08));
  }
  if (variable) {
    variable.values = current;
    syncStepBuilderLegacyLayerState();
  } else {
    stepBuilderState.values = current;
  }
}

function stepBuilderStackHeightMax() {
  const fixedSum = getStepBuilderFixedThicknesses()
    .map((value) => numericValue(value, 0))
    .reduce((sum, value) => sum + value, 0);
  const variableMax = Math.max(...stepBuilderState.values.map((value) => numericValue(value, 0)));
  return fixedSum + variableMax;
}

function defaultSpineTotalThickness() {
  return formatStepNumber(stepBuilderStackHeightMax() + 0.08);
}

function structuredGeometryPayloadFromBuilder() {
  const rows = 1;
  const columns = stepBuilderSwatchCount();
  const aliasEl = _sbEl("stepBuilderAlias");
  const widthEl = _sbEl("stepSwatchWidth");
  const heightEl = _sbEl("stepSwatchHeight");
  const spineWidthEl = _sbEl("stepSpineWidth");
  const spineTotalEl = _sbEl("stepSpineTotalThickness");
  syncStepBuilderLegacyLayerState();
  const orderedBottomToTop = stepBuilderState.layerRoles.slice().reverse();
  const roles = orderedBottomToTop.map((role, index) => ({
    role_index: index + 1,
    role_label: `LR_${String(index + 1).padStart(2, "0")}`,
    role_kind: role.kind,
    fixed_thickness_mm: role.kind === "fixed" ? numericValue(role.thickness, NaN) : null,
  }));
  const variableThicknesses = stepBuilderVariableRole().values.map((thickness) => numericValue(thickness, NaN));
  return {
    alias: aliasEl ? aliasEl.value.trim() : "",
    layout_rows: rows,
    layout_columns: columns,
    swatch_width_mm: numericValue(widthEl ? widthEl.value : "12.00", NaN),
    swatch_height_mm: numericValue(heightEl ? heightEl.value : "20.00", NaN),
    spine_width_mm: numericValue(spineWidthEl ? spineWidthEl.value : "3.00", NaN),
    spine_total_thickness_mm: numericValue(spineTotalEl ? spineTotalEl.value : defaultSpineTotalThickness(), NaN),
    roles,
    swatch_slots: variableThicknesses.map((thickness, index) => ({
      swatch_index: index,
      row_index: Math.floor(index / columns),
      column_index: index % columns,
      variable_thickness_mm: thickness,
    })),
  };
}

function projectGeometryVisualDraft(payload, viewportSpec = {}) {
  const unavailable = (reason) => ({ available: false, reason });
  const viewport = viewportSpec && typeof viewportSpec === "object" ? viewportSpec : {};
  const isFiniteNumber = (value) => typeof value === "number" && Number.isFinite(value);
  const isPositiveNumber = (value) => isFiniteNumber(value) && value > 0;
  const isPositiveInteger = (value) => Number.isInteger(value) && value > 0;
  const readViewportValue = (key, fallback) => {
    const value = viewport[key];
    return value === undefined ? fallback : value;
  };

  if (!payload || typeof payload !== "object") return unavailable("missing-payload");

  const rows = payload.layout_rows;
  const columns = payload.layout_columns;
  const swatchWidth = payload.swatch_width_mm;
  const swatchHeight = payload.swatch_height_mm;
  const spineWidth = payload.spine_width_mm;
  const spineTotal = payload.spine_total_thickness_mm;
  if (rows !== 1) return unavailable("unsupported-row-count");
  if (!isPositiveInteger(columns)) return unavailable("invalid-column-count");
  if (![swatchWidth, swatchHeight, spineWidth, spineTotal].every(isPositiveNumber)) {
    return unavailable("invalid-dimensions");
  }

  const roles = Array.isArray(payload.roles) ? payload.roles.slice() : [];
  if (roles.length === 0) return unavailable("missing-roles");
  if (roles.some((role) => !role || typeof role !== "object")) {
    return unavailable("invalid-role");
  }
  roles.sort((a, b) => a.role_index - b.role_index);
  const expectedRoleIndexes = roles.map((_, index) => index + 1);
  if (roles.some((role, index) => !role || role.role_index !== expectedRoleIndexes[index])) {
    return unavailable("invalid-role-order");
  }
  if (roles.filter((role) => role.role_kind === "variable").length !== 1) {
    return unavailable("invalid-variable-role-count");
  }
  for (const role of roles) {
    if (role.role_kind === "fixed") {
      if (!isPositiveNumber(role.fixed_thickness_mm)) return unavailable("invalid-fixed-thickness");
    } else if (role.role_kind === "variable") {
      if (role.fixed_thickness_mm !== null && role.fixed_thickness_mm !== undefined) {
        return unavailable("variable-role-has-fixed-thickness");
      }
    } else {
      return unavailable("invalid-role-kind");
    }
  }

  const slots = Array.isArray(payload.swatch_slots) ? payload.swatch_slots.slice() : [];
  if (slots.length !== rows * columns) return unavailable("invalid-slot-count");
  if (slots.some((slot) => !slot || typeof slot !== "object")) {
    return unavailable("invalid-swatch");
  }
  slots.sort((a, b) => a.swatch_index - b.swatch_index);
  const occupiedPositions = new Set();
  for (let index = 0; index < slots.length; index += 1) {
    const slot = slots[index];
    if (!slot || slot.swatch_index !== index) return unavailable("invalid-swatch-order");
    if (!Number.isInteger(slot.row_index) || slot.row_index !== 0) {
      return unavailable("invalid-swatch-row");
    }
    if (!Number.isInteger(slot.column_index) || slot.column_index < 0 || slot.column_index >= columns) {
      return unavailable("invalid-swatch-column");
    }
    const positionKey = `${slot.row_index}:${slot.column_index}`;
    if (occupiedPositions.has(positionKey)) return unavailable("duplicate-swatch-position");
    occupiedPositions.add(positionKey);
    if (!isFiniteNumber(slot.variable_thickness_mm) || slot.variable_thickness_mm < 0) {
      return unavailable("invalid-variable-thickness");
    }
  }

  const topWidth = readViewportValue("topWidth", 520);
  const topHeight = readViewportValue("topHeight", 220);
  const topPaddingX = readViewportValue("topPaddingX", 42);
  const topPaddingTop = readViewportValue("topPaddingTop", 28);
  const topPaddingBottom = readViewportValue("topPaddingBottom", 34);
  const sideWidth = readViewportValue("sideWidth", topWidth);
  const sideHeight = readViewportValue("sideHeight", 160);
  const sidePaddingTop = readViewportValue("sidePaddingTop", 18);
  const sidePaddingBottom = readViewportValue("sidePaddingBottom", 22);
  const viewportValues = [
    topWidth,
    topHeight,
    topPaddingX,
    topPaddingTop,
    topPaddingBottom,
    sideWidth,
    sideHeight,
    sidePaddingTop,
    sidePaddingBottom,
  ];
  if (!viewportValues.every((value) => isFiniteNumber(value) && value >= 0)) {
    return unavailable("invalid-viewport");
  }
  if (
    topWidth <= 2 * topPaddingX
    || topHeight <= topPaddingTop + topPaddingBottom
    || sideWidth !== topWidth
    || sideHeight <= sidePaddingTop + sidePaddingBottom
  ) {
    return unavailable("invalid-viewport-bounds");
  }

  const footprintWidth = columns * swatchWidth + 2 * spineWidth;
  const footprintHeight = rows * swatchHeight + spineWidth;
  const topAvailableWidth = topWidth - 2 * topPaddingX;
  const topAvailableHeight = topHeight - topPaddingTop - topPaddingBottom;
  const sharedXScale = Math.min(
    topAvailableWidth / footprintWidth,
    topAvailableHeight / footprintHeight,
  );
  if (!isPositiveNumber(sharedXScale)) return unavailable("invalid-top-scale");

  const topDrawWidth = footprintWidth * sharedXScale;
  const topDrawHeight = footprintHeight * sharedXScale;
  const sharedXOrigin = topPaddingX + (topAvailableWidth - topDrawWidth) / 2;
  const topYOrigin = topPaddingTop + (topAvailableHeight - topDrawHeight) / 2;
  const topRect = (xMm, yMm, widthMm, heightMm) => ({
    x: sharedXOrigin + xMm * sharedXScale,
    y: topYOrigin + (footprintHeight - yMm - heightMm) * sharedXScale,
    width: widthMm * sharedXScale,
    height: heightMm * sharedXScale,
    xMm,
    yMm,
    widthMm,
    heightMm,
  });

  const topSpines = [
    { part: "left", ...topRect(0, 0, spineWidth, footprintHeight) },
    {
      part: "right",
      ...topRect(spineWidth + columns * swatchWidth, 0, spineWidth, footprintHeight),
    },
    {
      part: "top",
      ...topRect(spineWidth, rows * swatchHeight, columns * swatchWidth, spineWidth),
    },
  ];
  const topSwatches = slots.map((slot) => ({
    swatchIndex: slot.swatch_index,
    rowIndex: slot.row_index,
    columnIndex: slot.column_index,
    ...topRect(
      spineWidth + slot.column_index * swatchWidth,
      slot.row_index * swatchHeight,
      swatchWidth,
      swatchHeight,
    ),
  }));

  const sideStacks = slots.map((slot) => {
    let z = 0;
    const layers = roles.map((role) => {
      const thickness = role.role_kind === "fixed"
        ? role.fixed_thickness_mm
        : slot.variable_thickness_mm;
      const zMin = z;
      const zMax = zMin + thickness;
      z = zMax;
      return {
        roleIndex: role.role_index,
        roleKind: role.role_kind,
        thicknessMm: thickness,
        zMinMm: zMin,
        zMaxMm: zMax,
      };
    });
    return {
      swatchIndex: slot.swatch_index,
      columnIndex: slot.column_index,
      xMm: spineWidth + slot.column_index * swatchWidth,
      widthMm: swatchWidth,
      stackHeightMm: z,
      layers,
    };
  });
  const maxStackHeight = Math.max(...sideStacks.map((stack) => stack.stackHeightMm));
  if (spineTotal + 1e-9 < maxStackHeight) return unavailable("spine-shorter-than-stack");

  const sideAvailableHeight = sideHeight - sidePaddingTop - sidePaddingBottom;
  const sideZScale = sideAvailableHeight / Math.max(spineTotal, maxStackHeight);
  if (!isPositiveNumber(sideZScale)) return unavailable("invalid-side-scale");
  const sideBaseline = sideHeight - sidePaddingBottom;
  const sideRect = (xMm, zMinMm, widthMm, heightMm) => ({
    x: sharedXOrigin + xMm * sharedXScale,
    y: sideBaseline - (zMinMm + heightMm) * sideZScale,
    width: widthMm * sharedXScale,
    height: heightMm * sideZScale,
    xMm,
    zMinMm,
    widthMm,
    heightMm,
  });

  sideStacks.forEach((stack) => {
    stack.x = sharedXOrigin + stack.xMm * sharedXScale;
    stack.width = stack.widthMm * sharedXScale;
    stack.layers.forEach((layer) => {
      Object.assign(
        layer,
        sideRect(stack.xMm, layer.zMinMm, stack.widthMm, layer.thicknessMm),
      );
    });
  });
  const sideSpines = [
    { part: "left", ...sideRect(0, 0, spineWidth, spineTotal) },
    {
      part: "right",
      ...sideRect(spineWidth + columns * swatchWidth, 0, spineWidth, spineTotal),
    },
  ];

  return {
    available: true,
    footprint: {
      widthMm: footprintWidth,
      heightMm: footprintHeight,
      swatchWidthMm: swatchWidth,
      swatchHeightMm: swatchHeight,
      spineWidthMm: spineWidth,
      spineTotalMm: spineTotal,
    },
    scales: {
      sharedX: sharedXScale,
      sideZ: sideZScale,
    },
    top: {
      width: topWidth,
      height: topHeight,
      xOrigin: sharedXOrigin,
      yOrigin: topYOrigin,
      drawWidth: topDrawWidth,
      drawHeight: topDrawHeight,
      spines: topSpines,
      swatches: topSwatches,
    },
    side: {
      width: sideWidth,
      height: sideHeight,
      xOrigin: sharedXOrigin,
      baseline: sideBaseline,
      maxStackHeightMm: maxStackHeight,
      spines: sideSpines,
      stacks: sideStacks,
    },
  };
}

function geometryVisualSvgNumber(value) {
  return Number(value).toFixed(3);
}

function buildGeometryVisualTopSvg(projected) {
  const rect = (item, className) => `
    <rect class="${className}"
      x="${geometryVisualSvgNumber(item.x)}" y="${geometryVisualSvgNumber(item.y)}"
      width="${geometryVisualSvgNumber(item.width)}" height="${geometryVisualSvgNumber(item.height)}"
      vector-effect="non-scaling-stroke"></rect>`;
  return `
    <svg class="geometry-visual-svg geometry-visual-top-svg"
      viewBox="0 0 ${projected.top.width} ${projected.top.height}"
      role="img" aria-label="Top view of the calibration strip footprint"
      preserveAspectRatio="xMidYMid meet">
      <g class="geometry-visual-top-swatches">
        ${projected.top.swatches.map((swatch) => rect(swatch, "geometry-visual-swatch")).join("")}
      </g>
      <g class="geometry-visual-spines">
        ${projected.top.spines.map((spine) => rect(spine, "geometry-visual-spine")).join("")}
      </g>
    </svg>`;
}

function buildGeometryVisualSideSvg(projected) {
  const rect = (item, className, attributes = "") => `
    <rect class="${className}" ${attributes}
      x="${geometryVisualSvgNumber(item.x)}" y="${geometryVisualSvgNumber(item.y)}"
      width="${geometryVisualSvgNumber(item.width)}" height="${geometryVisualSvgNumber(item.height)}"
      vector-effect="non-scaling-stroke"></rect>`;
  const layerRects = projected.side.stacks.flatMap((stack) => stack.layers)
    .filter((layer) => layer.height > 0)
    .map((layer) => {
      const classes = [
        "geometry-visual-layer",
        layer.roleKind === "variable" ? "is-variable" : "is-fixed",
        `role-tone-${(layer.roleIndex - 1) % 3}`,
      ].join(" ");
      return rect(layer, classes, `data-role-index="${layer.roleIndex}"`);
    }).join("");
  return `
    <svg class="geometry-visual-svg geometry-visual-side-svg"
      viewBox="0 0 ${projected.side.width} ${projected.side.height}"
      role="img" aria-label="Side view of the calibration strip layer profile"
      preserveAspectRatio="xMidYMid meet">
      <g class="geometry-visual-side-layers">${layerRects}</g>
      <g class="geometry-visual-spines">
        ${projected.side.spines.map((spine) => rect(spine, "geometry-visual-spine")).join("")}
      </g>
    </svg>`;
}

function renderStepGeometryVisualPreview(payload) {
  const root = _sbEl("stepGeometryVisualPreview");
  if (!root) return;
  const topSurface = _sbEl("stepGeometryTopView");
  const sideSurface = _sbEl("stepGeometrySideView");
  const footprintLabel = _sbEl("stepGeometryFootprintLabel");
  const projected = projectGeometryVisualDraft(payload);
  root.dataset.previewState = projected.available ? "available" : "unavailable";

  if (!projected.available) {
    const unavailableHtml = `<div class="geometry-visual-unavailable">Complete valid strip dimensions to see this view.</div>`;
    if (topSurface) topSurface.innerHTML = unavailableHtml;
    if (sideSurface) sideSurface.innerHTML = unavailableHtml;
    if (footprintLabel) footprintLabel.textContent = "";
    return;
  }

  if (topSurface) topSurface.innerHTML = buildGeometryVisualTopSvg(projected);
  if (sideSurface) sideSurface.innerHTML = buildGeometryVisualSideSvg(projected);
  if (footprintLabel) {
    footprintLabel.textContent = `Overall footprint: ${formatStepNumber(projected.footprint.widthMm)} × ${formatStepNumber(projected.footprint.heightMm)} mm`;
  }
}

function markStepBuilderInvalid(input) {
  if (!input) return;
  input.classList.add("is-invalid");
}

function clearStepBuilderValidationHighlights() {
  if (!stepBuilderBody) return;
  stepBuilderBody.querySelectorAll(".is-invalid").forEach((node) => node.classList.remove("is-invalid"));
}

function parentPathFromExportPath(path) {
  const raw = String(path || "");
  const slashIndex = raw.lastIndexOf("/");
  const backslashIndex = raw.lastIndexOf("\\");
  const index = Math.max(slashIndex, backslashIndex);
  return index > 0 ? raw.slice(0, index) : raw;
}

function geometryExportDestinationLabel(paths = []) {
  const exportPaths = Array.isArray(paths) ? paths.filter((path) => !!path) : [];
  if (exportPaths.length === 0) return "";
  if (exportPaths.length === 1) return exportPaths[0];
  const folders = [...new Set(exportPaths.map((path) => parentPathFromExportPath(path)))].filter((path) => !!path);
  if (folders.length === 1) return folders[0];
  return `${folders[0]} (+${folders.length - 1} more)`;
}

function geometryExportToastMessage(exportName, manifest) {
  const destinations = manifest?.export_destinations || manifest?.export_paths || [];
  const exportedCount = (manifest?.export_paths || destinations).length;
  const fileWord = exportedCount === 1 ? "file" : "files";
  const destination = geometryExportDestinationLabel(destinations);
  const countText = exportedCount ? ` (${exportedCount} ${fileWord})` : "";
  return destination
    ? `Exported ${exportName}${countText} to ${destination}`
    : `Exported ${exportName}${countText}`;
}

function geometryExportConflictDetail(err) {
  const detail = err?.detail;
  if (err?.status === 409 && detail?.requires_overwrite) return detail;
  return null;
}

function showGeometryOverwriteConfirmDialog(detail, exportName) {
  return new Promise((resolve) => {
    const conflicts = Array.isArray(detail?.conflicts) ? detail.conflicts.filter((path) => !!path) : [];
    const rows = conflicts.slice(0, 6);
    const extraCount = Math.max(0, conflicts.length - rows.length);
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay";
    overlay.innerHTML = `
      <div class="info-dialog info-dialog-wide" role="dialog" aria-modal="true" aria-labeledby="geometryOverwriteTitle">
        ${renderDialogHeader({
          title: "Replace Existing Export?",
          titleId: "geometryOverwriteTitle",
          closeButtonHtml: renderWindowCloseButton({ id: "geometryOverwriteClose", className: "info-dialog-close" }),
        })}
        <div class="info-dialog-body">
          <p class="info-dialog-lede">An export named <strong>${escapeHtml(exportName)}</strong> already exists in the public output folder.</p>
          <p class="small-copy">Continuing will overwrite the existing file or STL folder contents.</p>
          ${rows.length ? `
            <div class="artifact-path-list geometry-conflict-list">
              ${rows.map((path) => `
                <div class="artifact-path-row geometry-conflict-row">
                  <span class="artifact-kind">Path</span>
                  <span class="mono muted-line drawer-break-all" title="${escapeHtml(path)}">${escapeHtml(displayPathFromPrismaRoot(path))}</span>
                </div>
              `).join("")}
              ${extraCount ? `<span class="small-copy">+${extraCount} more</span>` : ""}
            </div>
          ` : ""}
        </div>
        <div class="info-dialog-footer">
          <button class="delete-button small" id="geometryOverwriteConfirm">Overwrite</button>
          <button class="ghost-button small" id="geometryOverwriteCancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = (value) => {
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
      resolve(value);
    };
    const handleKeydown = (event) => {
      if (event.key === "Escape") cleanup(false);
    };
    document.addEventListener("keydown", handleKeydown);
    overlay.querySelector("#geometryOverwriteConfirm")?.addEventListener("click", () => cleanup(true));
    overlay.querySelector("#geometryOverwriteCancel")?.addEventListener("click", () => cleanup(false));
    overlay.querySelector("#geometryOverwriteClose")?.addEventListener("click", () => cleanup(false));
    overlay.addEventListener("click", (event) => { if (event.target === overlay) cleanup(false); });
  });
}

let activeGeometryExportDialogCleanup = null;

function openGeometryExportDialog(geometryId, alias = "") {
  if (activeGeometryExportDialogCleanup?.() === false) return;
  document.querySelectorAll(".geometry-export-dialog").forEach((existing) => existing.remove());
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay geometry-export-dialog";
  overlay.innerHTML = `
    <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="geometryExportTitle">
      ${renderDialogHeader({
        title: "Export Sample Geometry",
        titleId: "geometryExportTitle",
        closeButtonHtml: renderWindowCloseButton({
          id: "geometryExportClose",
          className: "info-dialog-close",
          label: "Close export dialog",
          title: "Close export dialog",
        }),
      })}
      <div class="info-dialog-body geometry-export-body">
        <label class="geometry-export-name">
          <span class="field-label">File name</span>
          <input type="text" id="geometryExportName" value="${escapeHtml(alias || geometryId)}" />
        </label>
        <div class="sb-validation-error" id="geometryExportError"></div>
      </div>
      <div class="info-dialog-footer geometry-export-footer">
        <button class="ghost-button small" id="geometryExportCancelBtn">Cancel</button>
        <div class="geometry-export-action-group" role="group" aria-label="Export format">
          <button class="primary-button small" id="geometryExportStepBtn">Export STEP</button>
          <button class="primary-button small" id="geometryExportStlBtn">Export STLs</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const nameInput = overlay.querySelector("#geometryExportName");
  const errorEl = overlay.querySelector("#geometryExportError");
  let busy = false;
  const setBusy = (nextBusy) => {
    busy = Boolean(nextBusy);
    if (!overlay.isConnected) return;
    overlay.querySelectorAll("button, input").forEach((control) => { control.disabled = busy; });
  };
  const cleanup = ({ force = false } = {}) => {
    if (busy && !force) return false;
    document.removeEventListener("keydown", onKeyDown);
    overlay.remove();
    if (activeGeometryExportDialogCleanup === cleanup) {
      activeGeometryExportDialogCleanup = null;
    }
    return true;
  };
  activeGeometryExportDialogCleanup = cleanup;
  const onKeyDown = (event) => {
    if (event.key === "Escape") cleanup();
  };
  const doExport = async ({ includeStep, includeStls }) => {
    const exportName = (nameInput?.value || "").trim();
    nameInput?.classList.remove("is-invalid");
    if (!exportName) {
      if (errorEl) { errorEl.style.display = "block"; errorEl.textContent = "File name is required"; }
      markStepBuilderInvalid(nameInput);
      showImportToast("File name is required", "error");
      return;
    }
    const exportOptions = {
      export_name: exportName,
      include_step: includeStep,
      include_stls: includeStls,
    };
    const submitExport = async (overwrite = false) => {
      try {
        return await generateGeometryArtifacts(geometryId, {
          ...exportOptions,
          overwrite,
        });
      } catch (err) {
        const conflict = !overwrite ? geometryExportConflictDetail(err) : null;
        if (!conflict) throw err;
        const confirmed = await showGeometryOverwriteConfirmDialog(conflict, exportName);
        if (!confirmed) return null;
        return generateGeometryArtifacts(geometryId, {
          ...exportOptions,
          overwrite: true,
        });
      }
    };
    setBusy(true);
    try {
      const manifest = await submitExport(false);
      if (!manifest) return;
      showImportToast(geometryExportToastMessage(exportName, manifest), "success", { durationMs: 6500 });
      await handleRefresh();
      cleanup({ force: true });
    } catch (err) {
      const msg = err.message || "Export failed";
      if (errorEl) { errorEl.style.display = "block"; errorEl.textContent = msg; }
      showImportToast(msg, "error");
    } finally {
      setBusy(false);
    }
  };
  overlay.querySelector("#geometryExportClose")?.addEventListener("click", cleanup);
  overlay.querySelector("#geometryExportCancelBtn")?.addEventListener("click", cleanup);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) cleanup(); });
  overlay.querySelector("#geometryExportStepBtn")?.addEventListener("click", () => doExport({ includeStep: true, includeStls: false }));
  overlay.querySelector("#geometryExportStlBtn")?.addEventListener("click", () => doExport({ includeStep: false, includeStls: true }));
  document.addEventListener("keydown", onKeyDown);
  nameInput?.focus();
  nameInput?.select();
}

function populateStepValues() {
  const startEl = _sbEl("stepStartValue");
  const incEl = _sbEl("stepIncrementValue");
  const count = stepBuilderSwatchCount();
  const start = numericValue(startEl ? startEl.value : "0.20", 0.2);
  const increment = numericValue(incEl ? incEl.value : "0.08", 0.08);
  const values = Array.from({ length: count }, (_, index) => {
    return formatStepNumber(start + increment * index);
  });
  if (isStructuredGeometryBackend() && stepBuilderState.layerRoles.length) {
    stepBuilderVariableRole().values = values;
    syncStepBuilderLegacyLayerState();
  } else {
    stepBuilderState.values = values;
  }
}

function updateStepPreview() {
  const previewArea = _sbEl("stepPreviewArea");
  const filenameEl = _sbEl("stepFilenamePreview");
  if (!previewArea) return;
  if (isStructuredGeometryBackend() && stepBuilderState.layerRoles.length) {
    syncStepBuilderLegacyLayerState();
  }
  const structuredPayload = isStructuredGeometryBackend()
    ? structuredGeometryPayloadFromBuilder()
    : null;
  const incEl = _sbEl("stepIncrementValue");
  const lhEl = _sbEl("stepLayerHeight");
  const widthEl = _sbEl("stepSwatchWidth");
  const heightEl = _sbEl("stepSwatchHeight");
  const spineWidthEl = _sbEl("stepSpineWidth");
  const spineTotalEl = _sbEl("stepSpineTotalThickness");
  const previewFixed = buildFixedLayerPreviewValues(
    stepBuilderState.fixedLayers.map((layer) => ({ thickness_mm: numericValue(layer.thickness, 0) })),
  );
  const expLike = {
    variable_hex: "#cccccc",
    variable_thicknesses_mm: stepBuilderState.values.map((v) => numericValue(v, 0)),
    fixed_thicknesses_mm: previewFixed.thicknesses,
    fixed_filament_ids: [],
    roles: structuredPayload ? structuredPayload.roles : [],
  };
  const uniformIncrement = fixedSwatchIncrement(stepBuilderState.values);
  previewArea.innerHTML = `
    ${buildStripMiniTable(expLike)}
    <div class="sb-summary">
      <span class="sb-summary-line">${isStructuredGeometryBackend() ? `${stepBuilderSwatchCount()} swatches · ${formatStepNumber(widthEl ? widthEl.value : "12.00")} × ${formatStepNumber(heightEl ? heightEl.value : "20.00")} mm` : `${formatStepNumber(lhEl ? lhEl.value : "0.08")} mm layer height`}</span>
      ${isStructuredGeometryBackend() ? `<span class="sb-summary-line">${formatStepNumber(spineWidthEl ? spineWidthEl.value : "3.00")} mm spine · ${formatStepNumber(spineTotalEl ? spineTotalEl.value : defaultSpineTotalThickness())} mm total spine height</span>` : ""}
      ${uniformIncrement === null ? "" : `<span class="sb-summary-line">${formatStepNumber(uniformIncrement)} mm swatch increment</span>`}
    </div>
  `;
  if (filenameEl) filenameEl.textContent = buildStepFilename();
  if (filenameEl && isStructuredGeometryBackend()) {
    const aliasEl = _sbEl("stepBuilderAlias");
    filenameEl.textContent = (aliasEl?.value || "").trim() || "enter an alias";
  }
  if (structuredPayload) renderStepGeometryVisualPreview(structuredPayload);
}

function renderStepBuilder() {
  const valueGrid = _sbEl("stepValueGrid");
  if (!valueGrid) return;
  if (isStructuredGeometryBackend()) {
    renderStructuredStepBuilder();
    return;
  }
  const suspectIndexes = getSuspectSwatchIndexes();
  const columns = stepBuilderState.values.length;
  const gridStyle = `grid-template-columns: repeat(${columns}, 42px); width: calc(42px * ${columns} + 1px * ${Math.max(0, columns - 1)});`;

  valueGrid.innerHTML = `
    <div class="sb-variable-row">
      <span class="row-label">Variable Layer</span>
      <div class="sb-content-col">
        <div class="sb-swatch-inputs" style="${gridStyle}">
          ${stepBuilderState.values.map((value, index) => `
            <div class="sb-swatch${suspectIndexes.includes(index) ? " is-suspect" : ""}">
              <span class="sb-swatch-number">#${index + 1}</span>
              <input type="text" class="step-manual-input" inputmode="decimal" data-step-index="${index}" value="${formatStepNumber(value)}" />
            </div>
          `).join("")}
        </div>
        ${fixedLayerDisplayEntries(stepBuilderState.fixedLayers).map(({ layer, index }) => `
          <div class="sb-fixed-inline">
            <input type="text" class="fixed-layer-input" inputmode="decimal" data-fixed-index="${index}" value="${formatStepNumber(layer.thickness)}" />
            <span class="sb-fixed-label">mm — Fixed Layer ${index + 1}</span>
            <button class="ghost-button small remove-fixed-layer" type="button" data-fixed-index="${index}" style="color:#c62828">Remove</button>
          </div>
        `).join("")}
        <button class="ghost-button small sb-add-fixed-btn" type="button" id="inlineAddFixedLayerBtn">+ Add Fixed Layer</button>
      </div>
    </div>
  `;

  valueGrid.querySelectorAll(".step-manual-input").forEach((input) => {
    bindStepDecimalInput(input, {
      onInput: (value) => {
        stepBuilderState.values[Number(input.dataset.stepIndex)] = value;
        const spineTotalEl = _sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
        updateStepPreview();
      },
    });
  });

  valueGrid.querySelectorAll(".fixed-layer-input").forEach((input) => {
    bindStepDecimalInput(input, {
      onInput: (value) => {
        stepBuilderState.fixedLayers[Number(input.dataset.fixedIndex)].thickness = value;
        const spineTotalEl = _sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
        updateStepPreview();
      },
    });
  });

  valueGrid.querySelectorAll(".remove-fixed-layer").forEach((button) => {
    button.addEventListener("click", () => {
      stepBuilderState.fixedLayers.splice(Number(button.dataset.fixedIndex), 1);
      renderStepBuilder();
      const spineTotalEl = _sbEl("stepSpineTotalThickness");
      if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
      updateStepPreview();
    });
  });

  const inlineAddFixedLayerBtn = document.getElementById("inlineAddFixedLayerBtn");
  if (inlineAddFixedLayerBtn) {
    inlineAddFixedLayerBtn.addEventListener("click", () => {
      stepBuilderState.fixedLayers.push({ thickness: "0.20" });
      renderStepBuilder();
      const spineTotalEl = _sbEl("stepSpineTotalThickness");
      if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
      updateStepPreview();
    });
  }

  updateStepPreview();
}

function structuredRoleLabel(displayIndex) {
  const roleIndex = stepBuilderState.layerRoles.length - displayIndex;
  return `LR_${String(roleIndex).padStart(2, "0")}`;
}

function renderStructuredStepBuilder() {
  const valueGrid = _sbEl("stepValueGrid");
  if (!valueGrid) return;
  syncStepBuilderLegacyLayerState();
  updateStepBuilderDrawerWidth();
  const variable = stepBuilderVariableRole();
  const suspectIndexes = getSuspectSwatchIndexes();
  const columns = variable.values.length;
  const gridStyle = `grid-template-columns: repeat(${columns}, 35px); width: calc(35px * ${columns} + 1px * ${Math.max(0, columns - 1)});`;

  valueGrid.innerHTML = `
    <div class="sb-layer-role-stack">
      ${stepBuilderState.layerRoles.map((role, displayIndex) => {
        const roleLabel = structuredRoleLabel(displayIndex);
        if (role.kind === "variable") {
          return `
            <div class="sb-layer-role-card is-variable" data-role-id="${role.id}">
              <div class="sb-layer-role-side">
                <button class="sb-layer-drag-handle" type="button" draggable="true" data-role-id="${role.id}" title="Drag to reorder" aria-label="Drag ${roleLabel} to reorder">⋮⋮</button>
                <div class="sb-layer-role-meta">
                  <span class="sb-layer-role-id">${roleLabel}</span>
                  <span class="sb-layer-role-kind">Variable</span>
                </div>
              </div>
              <div class="sb-layer-role-content">
                <div class="sb-swatch-inputs" style="${gridStyle}">
                  ${role.values.map((value, index) => `
                    <div class="sb-swatch${suspectIndexes.includes(index) ? " is-suspect" : ""}">
                      <input type="text" class="step-manual-input" inputmode="decimal" data-step-index="${index}" value="${formatStepNumber(value)}" />
                    </div>
                  `).join("")}
                </div>
              </div>
            </div>
          `;
        }
        return `
          <div class="sb-layer-role-card is-fixed" data-role-id="${role.id}">
            <div class="sb-layer-role-side">
              <button class="sb-layer-drag-handle" type="button" draggable="true" data-role-id="${role.id}" title="Drag to reorder" aria-label="Drag ${roleLabel} to reorder">⋮⋮</button>
              <div class="sb-layer-role-meta">
                <span class="sb-layer-role-id">${roleLabel}</span>
                <span class="sb-layer-role-kind">Fixed</span>
              </div>
            </div>
            <div class="sb-layer-role-content">
              <label class="sb-fixed-role-field" style="${gridStyle}">
                <input type="text" class="fixed-layer-input" inputmode="decimal" data-role-id="${role.id}" value="${formatStepNumber(role.thickness)}" />
              </label>
            </div>
            <button class="sb-layer-remove-button" type="button" data-role-id="${role.id}" title="Remove layer role" aria-label="Remove ${roleLabel}">
              <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                <path d="M3 3 L9 9 M9 3 L3 9"></path>
              </svg>
            </button>
          </div>
        `;
      }).join("")}
      <button class="ghost-button small sb-add-fixed-btn" type="button" id="inlineAddFixedLayerBtn">+ Add Layer Role</button>
    </div>
  `;

  valueGrid.querySelectorAll(".step-manual-input").forEach((input) => {
    bindStepDecimalInput(input, {
      onInput: (value) => {
        variable.values[Number(input.dataset.stepIndex)] = value;
        syncStepBuilderLegacyLayerState();
        const spineTotalEl = _sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
        updateStepPreview();
      },
    });
  });

  valueGrid.querySelectorAll(".fixed-layer-input").forEach((input) => {
    bindStepDecimalInput(input, {
      onInput: (value) => {
        const role = stepBuilderState.layerRoles.find((item) => item.id === input.dataset.roleId);
        if (role) role.thickness = value;
        syncStepBuilderLegacyLayerState();
        const spineTotalEl = _sbEl("stepSpineTotalThickness");
        if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
        updateStepPreview();
      },
    });
  });

  valueGrid.querySelectorAll(".sb-layer-remove-button").forEach((button) => {
    button.addEventListener("click", () => {
      const roleId = button.dataset.roleId;
      stepBuilderState.layerRoles = stepBuilderState.layerRoles.filter((role) => role.id !== roleId || role.kind === "variable");
      syncStepBuilderLegacyLayerState();
      renderStepBuilder();
      const spineTotalEl = _sbEl("stepSpineTotalThickness");
      if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
      updateStepPreview();
    });
  });

  valueGrid.querySelectorAll(".sb-layer-drag-handle").forEach((handle) => {
    handle.addEventListener("dragstart", (event) => {
      event.dataTransfer?.setData("text/plain", handle.dataset.roleId || "");
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
      handle.closest(".sb-layer-role-card")?.classList.add("is-dragging");
    });
    handle.addEventListener("dragend", () => {
      valueGrid.querySelectorAll(".sb-layer-role-card").forEach((card) => card.classList.remove("is-dragging", "is-drop-target"));
    });
  });

  valueGrid.querySelectorAll(".sb-layer-role-card").forEach((card) => {
    card.addEventListener("dragover", (event) => {
      event.preventDefault();
      card.classList.add("is-drop-target");
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    });
    card.addEventListener("dragleave", () => card.classList.remove("is-drop-target"));
    card.addEventListener("drop", (event) => {
      event.preventDefault();
      card.classList.remove("is-drop-target");
      const fromId = event.dataTransfer?.getData("text/plain");
      const toId = card.dataset.roleId;
      if (!fromId || !toId || fromId === toId) return;
      const fromIndex = stepBuilderState.layerRoles.findIndex((role) => role.id === fromId);
      const toIndex = stepBuilderState.layerRoles.findIndex((role) => role.id === toId);
      if (fromIndex < 0 || toIndex < 0) return;
      const [moved] = stepBuilderState.layerRoles.splice(fromIndex, 1);
      stepBuilderState.layerRoles.splice(toIndex, 0, moved);
      syncStepBuilderLegacyLayerState();
      renderStepBuilder();
      const spineTotalEl = _sbEl("stepSpineTotalThickness");
      if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
      updateStepPreview();
    });
  });

  const inlineAddFixedLayerBtn = document.getElementById("inlineAddFixedLayerBtn");
  if (inlineAddFixedLayerBtn) {
    inlineAddFixedLayerBtn.addEventListener("click", () => {
      stepBuilderState.layerRoles.push(makeStepBuilderRole("fixed", "0.20"));
      syncStepBuilderLegacyLayerState();
      renderStepBuilder();
      const spineTotalEl = _sbEl("stepSpineTotalThickness");
      if (spineTotalEl && !spineTotalEl.dataset.userEdited) spineTotalEl.value = defaultSpineTotalThickness();
      updateStepPreview();
    });
  }

  updateStepPreview();
}

function openStepBuilderDrawer() {
  if (!stepBuilderDrawer || !stepBuilderBody) return;

  // Close other drawers — mutually exclusive
  closeDrawer();
  closeBundleMgmtDrawer();

  // Reset builder state to defaults
  stepBuilderState.values = ["0.20", "0.28", "0.36", "0.44", "0.52", "0.60", "0.68", "0.76"];
  stepBuilderState.fixedLayers = [];
  stepBuilderState.alias = "";
  stepBuilderState.bundle = "";
  const structuredMode = isStructuredGeometryBackend();
  if (structuredMode) initializeStepBuilderLayerRoles(stepBuilderState.values);
  stepBuilderDrawer.classList.toggle("is-structured", structuredMode);
  const saveBtn = document.getElementById("stepBuilderSave");
  const createExportBtn = document.getElementById("stepBuilderCreateExport");
  if (saveBtn) saveBtn.textContent = structuredMode ? "Create" : "Generate";
  if (createExportBtn) createExportBtn.style.display = structuredMode ? "" : "none";

  stepBuilderBody.innerHTML = `
    ${structuredMode ? `
      <div class="step-builder-layout">
        <div class="step-builder-visual-column">
          ${buildDrawerFormModule("Visual Preview", `
            <div class="geometry-visual-preview" id="stepGeometryVisualPreview" data-preview-state="unavailable">
              <section class="geometry-visual-view" aria-labelledby="stepGeometryTopHeading">
                <div class="geometry-visual-view-heading" id="stepGeometryTopHeading">Top View</div>
                <div class="geometry-visual-surface geometry-visual-top-surface" id="stepGeometryTopView">
                  <div class="geometry-visual-unavailable">Complete valid strip dimensions to see this view.</div>
                </div>
                <div class="geometry-visual-derived" id="stepGeometryFootprintLabel"></div>
              </section>
              <section class="geometry-visual-view" aria-labelledby="stepGeometrySideHeading">
                <div class="geometry-visual-view-heading" id="stepGeometrySideHeading">Side View</div>
                <div class="geometry-visual-surface geometry-visual-side-surface" id="stepGeometrySideView">
                  <div class="geometry-visual-unavailable">Complete valid strip dimensions to see this view.</div>
                </div>
                <div class="geometry-visual-note">Vertical thickness is exaggerated for visibility.</div>
              </section>
            </div>
          `, {
            classes: "step-builder-module step-builder-visual-module",
            bodyClass: "drawer-module-body-tight",
            density: "compact",
          })}
        </div>
        <div class="step-builder-form-column">
    ` : ""}
    ${structuredMode ? buildDrawerFormModule("Strip Layout", `
      <div class="sb-param-line sb-strip-layout-row">
        <label class="sb-field-inline">
          <span class="field-label">Swatch Width (mm)</span>
          <input type="text" id="stepSwatchWidth" inputmode="decimal" value="12.00" />
        </label>
        <label class="sb-field-inline">
          <span class="field-label">Swatch Height (mm)</span>
          <input type="text" id="stepSwatchHeight" inputmode="decimal" value="20.00" />
        </label>
        <label class="sb-field-inline">
          <span class="field-label">Number of Swatches</span>
          <input type="number" id="stepColumnCount" min="1" max="48" step="1" value="8" />
        </label>
      </div>
      <div class="sb-param-line sb-strip-layout-row">
        <label class="sb-field-inline">
          <span class="field-label">Spine Width (mm)</span>
          <input type="text" id="stepSpineWidth" inputmode="decimal" value="3.00" />
        </label>
        <label class="sb-field-inline">
          <span class="field-label">Spine Thickness (mm)</span>
          <input type="text" id="stepSpineTotalThickness" inputmode="decimal" value="${defaultSpineTotalThickness()}" />
        </label>
      </div>
    `, { classes: "step-builder-module", bodyClass: "drawer-module-body-tight", density: "compact" }) : ""}

    ${buildDrawerFormModule(structuredMode ? "Variable Layer Increment" : "Parameters", `
      ${structuredMode ? `<p class="sb-module-caption">Auto-fill swatch thicknesses for strips with constant swatch-to-swatch thickness increments (optional)</p>` : ""}
      ${!structuredMode ? `
        <div class="sb-param-line">
          <label class="sb-field-inline">
            <span class="field-label">Layer<br>Height (mm)</span>
            <input type="text" id="stepLayerHeight" inputmode="decimal" value="0.08" />
          </label>
        </div>
      ` : ""}
      <div class="sb-param-line sb-param-line-actions">
        <label class="sb-field-inline">
          <span class="field-label">First Swatch Thickness (mm)</span>
          <input type="text" id="stepStartValue" inputmode="decimal" value="0.20" />
        </label>
        <label class="sb-field-inline">
          <span class="field-label">Swatch-to-Swatch Increment (mm)</span>
          <input type="text" id="stepIncrementValue" inputmode="decimal" value="0.08" />
        </label>
        <div class="sb-params-actions">
          <button class="primary-button small" type="button" id="populateStepBtn">Fill Values</button>
        </div>
      </div>
    `, { classes: "step-builder-module", bodyClass: "drawer-module-body-tight", density: "compact" })}

    ${buildDrawerFormModule("Strip Definition", `<div id="stepValueGrid"></div>`, {
      classes: "step-builder-module",
      bodyClass: "drawer-module-body-tight",
      density: "compact",
    })}

    ${buildDrawerFormModule(structuredMode ? "Strip Diagram Preview" : "Preview", `<div id="stepPreviewArea" class="sb-preview"></div>`, {
      classes: "step-builder-module",
      bodyClass: "drawer-module-body-tight",
      density: "compact",
    })}

    ${buildDrawerFormModule("Metadata", `
      <div class="sb-meta-stack">
        <label class="sb-field-full">
          <span class="field-label">Alias</span>
          <input type="text" id="stepBuilderAlias" placeholder="e.g. thin over white" value="" />
        </label>
        <label class="sb-field-full">
          <span class="field-label">Bundle</span>
          <select id="stepBuilderBundle"><option value="">— no bundles exist —</option></select>
        </label>
      </div>
    `, { classes: "step-builder-module", bodyClass: "drawer-module-body-tight", density: "form" })}

    ${buildDrawerFormModule("Output", `
      <div class="sb-output-stack">
        <div class="sb-output-field">
          <span class="field-label">${structuredMode ? "Default export name:" : "Filename:"}</span>
          <code class="mono" id="stepFilenamePreview"></code>
        </div>
        <div class="sb-output-field">
          <span class="field-label">STEP Export Path:</span>
          <span class="mono muted-line" id="stepLibraryPathDisplay" style="font-size:11px">${_serverConfig ? (_serverConfig.step_export_relative || _serverConfig.step_library_relative) : "output/steps/"}</span>
          <button class="copy-pill drawer-utility-button" id="copyStepBuilderPath" type="button">Copy Full Export Path</button>
        </div>
      </div>
      <div class="sb-validation-error" id="stepValidationError"></div>
    `, { classes: "step-builder-module", bodyClass: "drawer-module-body-tight", density: "compact" })}
    ${structuredMode ? `
        </div>
      </div>
    ` : ""}
  `;

  stepBuilderDrawer.classList.add("is-open");
  stepBuilderDrawer.setAttribute("aria-hidden", "false");

  bindStepBuilderControls();
  renderStepBuilder();
  populateStepBuilderBundleDropdown();
}

async function populateStepBuilderBundleDropdown() {
  const select = document.getElementById("stepBuilderBundle");
  if (!select) return;
  try {
    const bundles = await fetchBundles();
    if (bundles.length === 0) {
      select.innerHTML = `<option value="">— no bundles exist —</option>`;
    } else {
      select.innerHTML = `<option value="">— none —</option>` +
        bundles.map((b) => `<option value="${b.name}">${b.name}</option>`).join("");
    }
  } catch (_) {
    select.innerHTML = `<option value="">— no bundles available —</option>`;
  }
}

function closeStepBuilderDrawer() {
  if (!stepBuilderDrawer) return;
  stepBuilderDrawer.classList.remove("is-open");
  stepBuilderDrawer.classList.remove("is-structured");
  stepBuilderDrawer.setAttribute("aria-hidden", "true");
  stepBuilderDrawer.style.removeProperty("--step-builder-width");
  stepBuilderDrawer.style.removeProperty("--step-builder-form-width");
  if (stepBuilderBody) stepBuilderBody.innerHTML = "";
  const saveBtn = document.getElementById("stepBuilderSave");
  const createExportBtn = document.getElementById("stepBuilderCreateExport");
  if (saveBtn) saveBtn.textContent = "Generate";
  if (createExportBtn) createExportBtn.style.display = "";
}

function isStepBuilderOpen() {
  return stepBuilderDrawer && stepBuilderDrawer.classList.contains("is-open");
}

function bindStepBuilderButton() {
  const openButton = document.getElementById("openStepBuilderBtn");
  if (!openButton) return;
  openButton.addEventListener("click", () => {
    if (isStepBuilderOpen()) {
      closeStepBuilderDrawer();
    } else {
      openStepBuilderDrawer();
    }
  });
}

function bindGeometryLibraryExportButton() {
  const button = document.getElementById("exportGeometryFilesBtn");
  if (!button) return;
  button.addEventListener("click", async () => {
    if (document.querySelector(".maintenance-workflow-overlay")) {
      showImportToast("Close the active maintenance workflow before opening another.", "warning");
      return;
    }
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "Loading...";
    try {
      const operation = await maintenanceOperationById("export_geometry_files");
      if (!operation) {
        showImportToast("Export Geometry Files is not available", "error");
        return;
      }
      if (operation.enabled === false) {
        showImportToast(operation.unavailable_reason || operation.disabled_reason || "Export Geometry Files is unavailable", "warning");
        return;
      }
      showMaintenanceWorkflow(operation, null, { exportGeometryScope: "all_geometries" });
    } catch (err) {
      showImportToast(err.message || "Could not open geometry export workflow", "error");
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  });
}

// ── Bundle management drawer ─────────────────────────────────────────────────

let _bundleDrawerState = {
  bundles: [],
  selectedBundleName: null,
  showNewInput: false,
  renamingBundleName: null,
};

const BUNDLE_MAPPING_SLOT_COLORS = [
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
];

function isBundleMgmtOpen() {
  return bundleMgmtDrawer && bundleMgmtDrawer.classList.contains("is-open");
}

function closeBundleMgmtDrawer() {
  if (!bundleMgmtDrawer) return;
  bundleMgmtDrawer.classList.remove("is-open");
  bundleMgmtDrawer.setAttribute("aria-hidden", "true");
  if (bundleMgmtBody) bundleMgmtBody.innerHTML = "";
  _bundleDrawerState.selectedBundleName = null;
  _bundleDrawerState.showNewInput = false;
  _bundleDrawerState.renamingBundleName = null;
}

function handleOutsideDrawerDismiss(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;

  const anyDrawerOpen =
    recordDrawer?.classList.contains("is-open") ||
    linkedSampleDrawer?.classList.contains("is-open") ||
    isStepBuilderOpen() ||
    isBundleMgmtOpen();
  if (!anyDrawerOpen) return;

  if (target.closest(
    ".record-drawer, .linked-record-drawer, .step-builder-drawer, .bundle-mgmt-drawer, .manual-proc-overlay, .image-lightbox-overlay"
  )) {
    return;
  }

  if (target.closest(
    "button, a, input, select, textarea, label, canvas, [role='button'], .data-row, .import-section-title"
  )) {
    return;
  }

  if (recordDrawer?.classList.contains("is-open")) {
    clearSelectionAndDrawer();
  }
  if (isStepBuilderOpen()) {
    closeStepBuilderDrawer();
  }
  if (isBundleMgmtOpen()) {
    closeBundleMgmtDrawer();
  }
}

async function openBundleManagementDrawer() {
  // Close other drawers — mutually exclusive
  closeDrawer();
  closeStepBuilderDrawer();

  try {
    _bundleDrawerState.bundles = await fetchBundles();
    const bundles = _sortedBundles(_bundleDrawerState.bundles || []);
    _bundleDrawerState.selectedBundleName = bundles[0]?.name || null;
    _bundleDrawerState.renamingBundleName = null;
  } catch (err) {
    showImportToast("Failed to load bundles: " + err.message, "error");
    return;
  }

  bundleMgmtDrawer.classList.add("is-open");
  bundleMgmtDrawer.setAttribute("aria-hidden", "false");
  renderBundleMgmtBody();
  bindBundleMgmtEvents();
}

function renderBundleMgmtBody() {
  if (!bundleMgmtBody) return;
  const bundles = _sortedBundles(_bundleDrawerState.bundles || []);
  const selected = _selectedBundle();
  const selectedStepIds = selected?.step_ids || [];
  const availableSteps = _availableBundleGeometries(selectedStepIds);

  const newBundleBlock = `
    <div class="bundle-new-input-row" id="bundleNewInputRow">
      <input type="text" id="bundleNewNameInput" placeholder="New bundle name..." />
      <button class="primary-button small" id="bundleNewSaveBtn">Create</button>
    </div>
  `;

  const bundleList = bundles.length
    ? bundles.map((bundle) => {
        const count = (bundle.step_ids || []).length;
        const isSelected = selected?.name === bundle.name;
        return `
          <button type="button" class="bundle-list-item${isSelected ? " is-selected" : ""}" data-bundle="${_escAttr(bundle.name)}">
            <span class="bundle-list-main">
              <span class="bundle-list-name">${_escHtml(bundle.name)}</span>
              <span class="bundle-list-count" aria-label="${count} geometr${count === 1 ? "y" : "ies"}">(${count})</span>
            </span>
            ${_renderBundleMappingStatusPill(bundle)}
          </button>
        `;
      }).join("")
    : `<div class="bundle-empty-msg">No bundles defined yet.</div>`;

  const detailHtml = selected
    ? _renderBundleDetail(selected, selectedStepIds, availableSteps)
    : `
      <div class="bundle-detail-empty">
        <strong>No bundle selected</strong>
        <span>Create a bundle or select one from the list.</span>
      </div>
    `;

  bundleMgmtBody.innerHTML = `
    <div class="bundle-manager-layout">
      <section class="bundle-list-pane" aria-label="Geometry bundles">
        <div class="bundle-pane-cap">
          <span class="sidebar-label">Geometry Bundles</span>
        </div>
        <div class="bundle-pane-body">
          ${newBundleBlock}
          <div class="bundle-list">${bundleList}</div>
        </div>
      </section>
      <section class="bundle-detail-pane" aria-label="Selected bundle">
        ${detailHtml}
      </section>
    </div>
  `;
  bindBundleMgmtInteractions();
}

function _sortedBundles(bundles) {
  return [...(bundles || [])].sort((a, b) => (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base" }));
}

function _selectedBundle() {
  const bundles = _sortedBundles(_bundleDrawerState.bundles || []);
  if (bundles.length === 0) return null;
  const selected = bundles.find((bundle) => bundle.name === _bundleDrawerState.selectedBundleName);
  if (selected) return selected;
  _bundleDrawerState.selectedBundleName = bundles[0].name;
  return bundles[0];
}

function _bundleSlotKey(position) {
  let n = Math.max(0, Number(position) || 0);
  let label = "";
  while (true) {
    const rem = n % 26;
    label = String.fromCharCode(65 + rem) + label;
    n = Math.floor(n / 26) - 1;
    if (n < 0) return label;
  }
}

function _bundleSlotColor(positionOrKey) {
  let index = Number(positionOrKey);
  if (typeof positionOrKey === "string") {
    index = 0;
    for (const char of positionOrKey.toUpperCase()) {
      const code = char.charCodeAt(0) - 64;
      if (code < 1 || code > 26) break;
      index = (index * 26) + code;
    }
    index = Math.max(0, index - 1);
  }
  if (!Number.isFinite(index)) index = 0;
  return BUNDLE_MAPPING_SLOT_COLORS[index % BUNDLE_MAPPING_SLOT_COLORS.length];
}

function _bundleStatusMeta(bundle) {
  const status = String(bundle?.mapping_status || "unmapped").toLowerCase();
  if (status === "mapped") return { label: "Mapped", className: "mapped" };
  if (status === "incomplete") return { label: "Incomplete", className: "incomplete" };
  if (status === "invalid") return { label: "Invalid", className: "failed" };
  return { label: "Unmapped", className: "unmapped" };
}

function _renderBundleMappingStatusPill(bundle) {
  const meta = _bundleStatusMeta(bundle);
  return `<span class="status-pill bundle-status-pill ${_escAttr(meta.className)}">${_escHtml(meta.label)}</span>`;
}

function _bundleSlotById(bundle, materialSlotId) {
  return (bundle?.material_slots || []).find((slot) => slot.material_slot_id === materialSlotId) || null;
}

function _renderBundleMappingChips(bundle, member) {
  const roles = member?.roles || [];
  if (!roles.length) return `<span class="bundle-role-chip is-unmapped">No roles</span>`;
  return roles.map((role) => {
    const slot = _bundleSlotById(bundle, role.material_slot_id);
    const roleLabel = formatLayerRoleLabel(role);
    if (!slot) {
      return `<span class="bundle-role-chip is-unmapped" title="${_escAttr(roleLabel)}">${_escHtml(roleLabel)} · unmapped</span>`;
    }
    const color = _bundleSlotColor(slot.key || slot.position || 0);
    return `
      <span class="bundle-role-chip" title="${_escAttr(`${roleLabel}: Shared Filament ${slot.key}`)}">
        <span class="bundle-role-chip-color" style="background:${_escAttr(color)}"></span>
        <span>${_escHtml(roleLabel)}</span>
        <strong>${_escHtml(slot.key)}</strong>
      </span>
    `;
  }).join("");
}

function _buildBundleMemberPreviewDiagram(bundle, member, step) {
  const variableSlots = [...(step?.swatch_slots || [])]
    .sort((a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0));
  if (!variableSlots.length) {
    return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
  }
  const stepRoles = [...(step?.roles || [])];
  const memberRoles = [...(member?.roles || [])];
  const roleSource = stepRoles.length ? stepRoles : memberRoles;
  const roles = roleSource
    .map((role) => ({
      ...role,
      role_index: Number(role.role_index || 0),
      _memberRole: memberRoles.find((candidate) => Number(candidate.role_index || 0) === Number(role.role_index || 0)) || null,
    }))
    .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
  if (!roles.length) {
    return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
  }

  const swatchCount = variableSlots.length || Number(step?.swatch_count || step?.layout_columns || 8);
  const rowHtml = [];
  const labelHtml = [];
  roles.forEach((role) => {
    const memberRole = role._memberRole || {};
    const roleIndex = Number(role.role_index || 0);
    const roleKind = String(memberRole.role_kind || role.role_kind || "").toLowerCase();
    const slot = _bundleSlotById(bundle, memberRole.material_slot_id || role.material_slot_id || "");
    const slotKey = slot ? (slot.key || _bundleSlotKey(Number(slot.position || 0))) : "";
    const slotColor = slot ? _bundleSlotColor(slotKey || slot.position || 0) : (roleKind === "variable" ? "#d7d7d3" : "#ececea");
    const rowStyle = `--bundle-preview-row-color:${_escAttr(slotColor)}`;
    const roleToken = compactLayerRoleToken(memberRole.role_label || role.role_label, roleIndex, `LR_${String(roleIndex).padStart(2, "0")}`);
    labelHtml.push(`
      <div class="bundle-selector-role-map-label${slot ? "" : " is-unmapped"}">
        <span>${_escHtml(roleToken)} -&gt;</span>
        <strong>${_escHtml(slotKey || "?")}</strong>
      </div>
    `);

    if (roleKind === "variable") {
      const cells = variableSlots.map((slotInfo) => (
        `<td style="${rowStyle}">${Number(slotInfo.variable_thickness_mm || 0).toFixed(2)}</td>`
      )).join("");
      rowHtml.push(`<tr>${cells}</tr>`);
      return;
    }
    const fixedThickness = memberRole.fixed_thickness_mm ?? role.fixed_thickness_mm;
    const thickness = Number(fixedThickness);
    const label = Number.isFinite(thickness) ? `${thickness.toFixed(2)}mm` : "";
    rowHtml.push(`<tr><td colspan="${swatchCount}" style="${rowStyle}">${_escHtml(label)}</td></tr>`);
  });

  return `
    <div class="bundle-selector-member-diagram">
      <div class="bundle-selector-role-map-labels">${labelHtml.join("")}</div>
      <table class="mini-strip-table bulk-bundle-strip-table">${rowHtml.join("")}</table>
    </div>
  `;
}

function _geometryAliasFromRef(stepRef) {
  const step = stepRecordByRef(stepRef);
  return step?.alias || step?.name || stepFileNameFromRef(stepRef) || stepRef || "";
}

function _geometryLabelForStep(step) {
  return step?.alias || step?.name || step?.display_name || "Unnamed geometry";
}

function _geometryUserLabelFromRef(stepRef) {
  return _geometryLabelForStep(stepRecordByRef(stepRef));
}

function _geometryMetaLineForStep(step) {
  if (!step) return "";
  const pieces = [];
  if (step.swatch_count != null) pieces.push(`${step.swatch_count} swatches`);
  else if (Array.isArray(step.variable_thicknesses_mm)) pieces.push(`${step.variable_thicknesses_mm.length} swatches`);
  if (step.layer_count != null) pieces.push(`${step.layer_count} role${Number(step.layer_count) === 1 ? "" : "s"}`);
  return pieces.filter(Boolean).join(" · ");
}

function _geometryMetaLine(stepRef) {
  return _geometryMetaLineForStep(stepRecordByRef(stepRef));
}

function _geometryBundleNames(stepRef) {
  const stepId = stepIdFromRef(stepRef);
  const names = new Set();
  const step = stepRecordByRef(stepId);
  (step?.bundle_names || []).forEach((name) => {
    if (name) names.add(name);
  });
  const metaBundle = stepMeta(stepId).bundle || step?.bundle || "";
  if (metaBundle) names.add(metaBundle);
  (data.bundles || []).forEach((bundle) => {
    if ((bundle.step_ids || []).includes(stepId) && bundle.name) names.add(bundle.name);
  });
  return Array.from(names).sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
}

function _geometrySelectorSearchText(step) {
  return [
    _geometryLabelForStep(step),
    _geometryMetaLineForStep(step),
    ..._geometryBundleNames(step?.step_id),
  ].filter(Boolean).join(" ").toLowerCase();
}

function _availableBundleGeometries(selectedStepIds) {
  const selected = new Set(selectedStepIds || []);
  return [...(data.steps || [])]
    .filter((step) => step?.step_id && !selected.has(step.step_id))
    .sort((a, b) => _geometryLabelForStep(a).localeCompare(_geometryLabelForStep(b), undefined, { sensitivity: "base" }));
}

function availableGeometryRecords() {
  return [...(data.steps || [])]
    .filter((step) => step?.step_id && !stepMeta(step.step_id).deleted)
    .sort((a, b) => _geometryLabelForStep(a).localeCompare(_geometryLabelForStep(b), undefined, { sensitivity: "base" }));
}

function renderGeometrySelectorField(button, stepId, placeholder = "Select Geometry", stepRecord = null) {
  if (!button) return;
  const step = stepRecord || stepRecordByRef(stepId);
  if (!step) {
    button.dataset.stepId = "";
    button.innerHTML = `<span class="filament-selector-placeholder">${_escHtml(placeholder)}</span>`;
    return;
  }
  const bundles = _geometryBundleNames(step.step_id);
  const bundleText = bundles.length ? bundles.join(", ") : "No bundle";
  button.dataset.stepId = step.step_id;
  button.innerHTML = `
    <span class="filament-selector-field-name">${_escHtml(_geometryLabelForStep(step))}</span>
    <span class="filament-selector-field-meta">${_escHtml(_geometryMetaLineForStep(step))}</span>
    <span class="geometry-selector-field-bundles" title="${_escAttr(bundleText)}">${_escHtml(bundleText)}</span>
  `;
}

function openGeometrySelector(options = {}) {
  const mode = options.mode === "multi" ? "multi" : "single";
  const isMulti = mode === "multi";
  const selectedId = options.selectedStepId || "";
  const selectedIds = new Set(options.selectedStepIds || (selectedId ? [selectedId] : []));
  const selectPanelTitle = options.selectPanelTitle || (isMulti ? "Select Geometries" : "Select Geometry");
  const previewPanelTitle = options.previewPanelTitle || (isMulti ? "Selected Geometry Preview" : "Geometry Preview");
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay geometry-selector-overlay";
  overlay.innerHTML = `
    <div class="info-dialog geometry-selector-dialog" role="dialog" aria-modal="true" aria-labeledby="geometrySelectorTitle">
      ${renderDialogHeader({
        title: options.title || "Select Geometry",
        titleId: "geometrySelectorTitle",
        headerClass: "geometry-selector-header",
        closeButtonHtml: renderWindowCloseButton({
          className: "info-dialog-close",
          label: "Close selector",
          title: "Close selector",
          attributes: "data-geometry-selector-close",
        }),
      })}
      <div class="geometry-selector-body">
        <section class="geometry-selector-panel geometry-selector-select-panel" aria-label="Select sample geometries">
          <div class="geometry-selector-panel-head">
            <h4>${_escHtml(selectPanelTitle)}</h4>
          </div>
          <div class="geometry-selector-panel-body">
            <div class="geometry-selector-filter-group">
              <div class="geometry-selector-filter-title">Filter By:</div>
              <div class="geometry-selector-controls">
                <label class="geometry-selector-filter">
                  <span>Bundle</span>
                  <select id="geometrySelectorBundleFilter"></select>
                </label>
                <label class="geometry-selector-filter">
                  <span>Name</span>
                  <input class="geometry-selector-search" id="geometrySelectorSearch" type="search" placeholder="Search geometries">
                </label>
              </div>
            </div>
            <div class="geometry-selector-results-frame">
              <div class="geometry-selector-results" id="geometrySelectorResults"></div>
            </div>
          </div>
        </section>
        <aside class="geometry-selector-panel geometry-selector-preview-panel" aria-label="Selected geometry preview">
          <div class="geometry-selector-panel-head">
            <h4>${_escHtml(previewPanelTitle)}</h4>
          </div>
          <div class="geometry-selector-preview" id="geometrySelectorPreview"></div>
        </aside>
      </div>
      <div class="info-dialog-footer geometry-selector-footer">
        <button class="ghost-button small" type="button" id="geometrySelectorCancel">Cancel</button>
        <button class="primary-button small" type="button" id="geometrySelectorApply">${_escHtml(options.applyLabel || (isMulti ? "Add Selected" : "Select Geometry"))}</button>
      </div>
    </div>
  `;

  const search = overlay.querySelector("#geometrySelectorSearch");
  const bundleFilter = overlay.querySelector("#geometrySelectorBundleFilter");
  const results = overlay.querySelector("#geometrySelectorResults");
  const preview = overlay.querySelector("#geometrySelectorPreview");
  const applyButton = overlay.querySelector("#geometrySelectorApply");
  let activeIndex = 0;
  let tentativeId = selectedId;

  function close() {
    overlay.remove();
  }

  function optionButtons() {
    return Array.from(results.querySelectorAll(".geometry-selector-option"));
  }

  function sourceGeometryRecords() {
    return Array.isArray(options.steps) ? options.steps : availableGeometryRecords();
  }

  function sourceGeometryById(stepId) {
    return sourceGeometryRecords().find((step) => step?.step_id === stepId) || stepRecordByRef(stepId);
  }

  function bundleFilterOptions() {
    const bundleNames = new Set();
    let hasUnbundled = false;
    sourceGeometryRecords().forEach((step) => {
      const names = _geometryBundleNames(step?.step_id);
      if (names.length === 0) {
        hasUnbundled = true;
        return;
      }
      names.forEach((name) => bundleNames.add(name));
    });
    const options = [
      `<option value="">All geometries</option>`,
      ...Array.from(bundleNames)
        .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }))
        .map((name) => `<option value="${_escAttr(name)}">${_escHtml(name)}</option>`),
    ];
    if (hasUnbundled) {
      options.push(`<option value="__none__">No bundle</option>`);
    }
    return options.join("");
  }

  function activateOption(index, { focus = false } = {}) {
    const options = optionButtons();
    if (options.length === 0) {
      activeIndex = 0;
      return;
    }
    activeIndex = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((button, i) => {
      const isActive = i === activeIndex;
      button.classList.toggle("is-active", isActive);
      button.tabIndex = isActive ? 0 : -1;
      button.setAttribute("aria-current", isActive ? "true" : "false");
    });
    if (focus) {
      options[activeIndex].focus();
      options[activeIndex].scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function updateSelectionState() {
    optionButtons().forEach((button) => {
      const stepId = button.dataset.stepId || "";
      button.classList.toggle("is-selected", isMulti ? selectedIds.has(stepId) : stepId === tentativeId);
    });
    renderPreview();
  }

  function tentativeIsVisible() {
    return optionButtons().some((button) => button.dataset.stepId === tentativeId);
  }

  function chooseOption() {
    if (isMulti) {
      const chosenIds = Array.from(selectedIds).filter((stepId) => sourceGeometryById(stepId));
      const chosenSteps = chosenIds.map((stepId) => sourceGeometryById(stepId)).filter(Boolean);
      if (chosenSteps.length === 0) return;
      try {
        if (typeof options.onApply === "function") {
          options.onApply(chosenIds, chosenSteps);
        }
      } finally {
        close();
      }
      return;
    }
    if (!tentativeIsVisible()) return;
    const step = sourceGeometryById(tentativeId);
    if (!step) return;
    try {
      if (typeof options.onApply === "function") {
        options.onApply(step.step_id, step);
      }
    } finally {
      close();
    }
  }

  function renderPreview() {
    if (isMulti) {
      const selectedSteps = Array.from(selectedIds).map((stepId) => sourceGeometryById(stepId)).filter(Boolean);
      if (selectedSteps.length === 0) {
        preview.innerHTML = `
          <div class="geometry-selector-preview-empty small-copy">
            Select one or more geometries to add to the bundle.
          </div>
        `;
        applyButton.disabled = true;
        return;
      }
      preview.innerHTML = `
        <div class="geometry-selector-selection-count">${selectedSteps.length} selected</div>
        <div class="geometry-selector-preview-list">
          ${selectedSteps.map((step) => {
            const bundles = _geometryBundleNames(step.step_id);
            const bundleHtml = bundles.length
              ? bundles.map((name) => `<span class="geometry-selector-bundle-chip">${_escHtml(name)}</span>`).join("")
              : `<span class="geometry-selector-bundle-empty">No bundle</span>`;
            return `
              <div class="geometry-selector-preview-card">
                <div class="geometry-selector-preview-head">
                  <strong>${_escHtml(_geometryLabelForStep(step))}</strong>
                </div>
                <div class="geometry-selector-option-bundles">${bundleHtml}</div>
                <div class="geometry-selector-preview-diagram">${buildGeometryStripMiniTable(step)}</div>
              </div>
            `;
          }).join("")}
        </div>
      `;
      applyButton.disabled = false;
      return;
    }
    const step = tentativeIsVisible() ? sourceGeometryById(tentativeId) : null;
    if (!step) {
      preview.innerHTML = `
        <div class="geometry-selector-preview-empty small-copy">
          Select a geometry to inspect its strip diagram.
        </div>
      `;
      applyButton.disabled = true;
      return;
    }
    const bundles = _geometryBundleNames(step.step_id);
    const bundleHtml = bundles.length
      ? bundles.map((name) => `<span class="geometry-selector-bundle-chip">${_escHtml(name)}</span>`).join("")
      : `<span class="geometry-selector-bundle-empty">No bundle</span>`;
    preview.innerHTML = `
      <div class="geometry-selector-preview-head">
        <strong>${_escHtml(_geometryLabelForStep(step))}</strong>
        <span>${_escHtml(_geometryMetaLineForStep(step))}</span>
      </div>
      <div class="geometry-selector-option-bundles">${bundleHtml}</div>
      <div class="geometry-selector-preview-diagram">${buildGeometryStripMiniTable(step)}</div>
    `;
    applyButton.disabled = false;
  }

  function render() {
    const q = (search.value || "").trim().toLowerCase();
    const selectedBundle = bundleFilter.value || "";
    const geometries = sourceGeometryRecords().filter((step) => {
      const bundles = _geometryBundleNames(step?.step_id);
      const bundleMatches =
        !selectedBundle ||
        (selectedBundle === "__none__" ? bundles.length === 0 : bundles.includes(selectedBundle));
      const searchMatches = !q || _geometrySelectorSearchText(step).includes(q);
      return bundleMatches && searchMatches;
    });
    if (geometries.length === 0) {
      activeIndex = 0;
      results.innerHTML = `<div class="geometry-selector-empty small-copy">No matching geometries</div>`;
      renderPreview();
      return;
    }
    results.innerHTML = geometries.map((step, index) => {
      const alias = _geometryLabelForStep(step);
      const meta = _geometryMetaLineForStep(step);
      const bundles = _geometryBundleNames(step.step_id);
      const isSelected = isMulti ? selectedIds.has(step.step_id) : step.step_id === tentativeId;
      const bundleHtml = bundles.length
        ? bundles.map((name) => `<span class="geometry-selector-bundle-chip">${_escHtml(name)}</span>`).join("")
        : `<span class="geometry-selector-bundle-empty">No bundle</span>`;
      return `
        <button type="button" class="geometry-selector-option${isSelected ? " is-selected" : ""}" data-step-id="${_escAttr(step.step_id)}" data-option-index="${index}" tabindex="-1">
          <span class="geometry-selector-check" aria-hidden="true"></span>
          <div class="geometry-selector-option-main">
            <div class="geometry-selector-option-head">
              <span class="geometry-selector-option-name">${_escHtml(alias)}</span>
              <span class="geometry-selector-option-meta">${_escHtml(meta)}</span>
            </div>
            <div class="geometry-selector-option-bundles">${bundleHtml}</div>
          </div>
        </button>
      `;
    }).join("");
    activateOption(activeIndex);
    updateSelectionState();
  }

  overlay.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
  });
  overlay.addEventListener("click", (event) => {
    event.stopPropagation();
    if (event.target === overlay) {
      close();
      return;
    }
    const closeBtn = event.target.closest("[data-geometry-selector-close], #geometrySelectorCancel");
    if (closeBtn) {
      close();
      return;
    }
    const option = event.target.closest(".geometry-selector-option");
    if (!option) return;
    activeIndex = Number(option.dataset.optionIndex || activeIndex);
    tentativeId = option.dataset.stepId || "";
    if (isMulti) {
      if (selectedIds.has(tentativeId)) {
        selectedIds.delete(tentativeId);
      } else if (tentativeId) {
        selectedIds.add(tentativeId);
      }
    }
    activateOption(activeIndex, { focus: true });
    updateSelectionState();
  });

  applyButton.addEventListener("click", chooseOption);
  search.addEventListener("input", () => {
    activeIndex = 0;
    render();
  });
  bundleFilter.addEventListener("change", () => {
    activeIndex = 0;
    render();
    search.focus();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.shiftKey) return;
    const options = optionButtons();
    if (options.length === 0) return;
    event.preventDefault();
    activateOption(activeIndex, { focus: true });
  });
  overlay.addEventListener("keydown", (event) => {
    const options = optionButtons();
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      if (options.length === 0) return;
      event.preventDefault();
      activateOption((activeIndex + 1) % options.length, { focus: true });
      return;
    }
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      if (options.length === 0) return;
      event.preventDefault();
      activateOption((activeIndex - 1 + options.length) % options.length, { focus: true });
      return;
    }
    if (event.key === "Home") {
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(0, { focus: true });
      return;
    }
    if (event.key === "End") {
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(options.length - 1, { focus: true });
      return;
    }
    if (event.key === "Enter" && event.target?.classList?.contains("geometry-selector-option")) {
      event.preventDefault();
      tentativeId = event.target.dataset.stepId || "";
      if (isMulti) {
        if (selectedIds.has(tentativeId)) {
          selectedIds.delete(tentativeId);
        } else if (tentativeId) {
          selectedIds.add(tentativeId);
        }
      }
      updateSelectionState();
    }
  });

  document.body.appendChild(overlay);
  bundleFilter.innerHTML = bundleFilterOptions();
  render();
  search.focus();
}

function _renderBundleDetail(bundle, stepIds, availableSteps) {
  const count = stepIds.length;
  const isRenaming = _bundleDrawerState.renamingBundleName === bundle.name;
  const membersByGeometry = new Map((bundle.members || []).map((member) => [member.geometry_id, member]));
  const mappingButtonDisabled = count === 0 ? "disabled" : "";
  const slotSummary = (bundle.material_slots || []).length
    ? (bundle.material_slots || []).map((slot) => `
        <span class="bundle-slot-summary-chip" title="${_escAttr(slot.label || `Shared Filament ${slot.key}`)}">
          <span class="bundle-slot-summary-color" style="background:${_escAttr(_bundleSlotColor(slot.key || slot.position || 0))}"></span>
          <strong>${_escHtml(slot.key || "")}</strong>
        </span>
      `).join("")
    : `<span class="bundle-slot-summary-empty">No shared filament slots mapped</span>`;
  const titleBlock = isRenaming ? `
    <div class="bundle-rename-row">
      <input type="text" class="bundle-rename-input" id="bundleRenameInput" value="${_escAttr(bundle.name)}" />
      <button class="primary-button small" id="bundleRenameSaveBtn" data-bundle="${_escAttr(bundle.name)}">Save</button>
      <button class="ghost-button small" id="bundleRenameCancelBtn">Cancel</button>
    </div>
  ` : `
    <div>
      <h4>${_escHtml(bundle.name)}</h4>
      <span>${count} geometr${count === 1 ? "y" : "ies"} in this bundle</span>
      ${_renderBundleMappingStatusPill(bundle)}
    </div>
    <div class="bundle-detail-actions">
      <button type="button" class="ghost-button small bundle-rename-btn" data-bundle="${_escAttr(bundle.name)}">Rename</button>
      <button type="button" class="delete-button small bundle-delete-btn" data-bundle="${_escAttr(bundle.name)}">Delete</button>
    </div>
  `;

  const members = stepIds.length ? stepIds.map((stepId) => {
    const step = stepRecordByRef(stepId);
    const member = membersByGeometry.get(stepId);
    const label = _geometryLabelForStep(step);
    const meta = _geometryMetaLine(stepId);
    const diagramHtml = step ? buildGeometryStripMiniTable(step) : "";
    const chipHtml = member ? _renderBundleMappingChips(bundle, member) : `<span class="bundle-role-chip is-unmapped">No mapping data</span>`;
    return `
      <div class="bundle-member-row" tabindex="0">
        <div class="bundle-member-main">
          <span class="bundle-member-name" title="${_escAttr(label)}">${_escHtml(label)}</span>
          <span class="bundle-member-meta" title="${_escAttr(meta)}">${_escHtml(meta)}</span>
        </div>
        <button type="button" class="sb-layer-remove-button bundle-remove-btn" data-bundle="${_escAttr(bundle.name)}" data-step="${_escAttr(stepId)}" title="Remove geometry" aria-label="Remove geometry">
          <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M3 3 L9 9 M9 3 L3 9"></path>
          </svg>
        </button>
        ${diagramHtml ? `
          <div class="bundle-member-preview" aria-hidden="true">
            <div class="bundle-member-preview-title">${_escHtml(label)}</div>
            ${diagramHtml}
            <div class="bundle-member-preview-mapping">${chipHtml}</div>
          </div>
        ` : ""}
      </div>
    `;
  }).join("") : `<div class="bundle-member-empty">This bundle has no geometries yet.</div>`;

  const addControl = availableSteps.length ? `
    <button type="button" class="primary-button small bundle-add-step-field" data-bundle="${_escAttr(bundle.name)}">
      Add Geometries...
    </button>
  ` : "";
  const availabilityNotice = !availableSteps.length
    ? `<div class="bundle-member-empty">All sample geometries are already in this bundle.</div>`
    : "";

  return `
    <div class="bundle-detail-header">${titleBlock}</div>
    <div class="bundle-detail-section">
      <div class="bundle-section-cap">
        <span class="bundle-section-label">Bundle Geometries</span>
        ${addControl}
      </div>
      <div class="bundle-section-body bundle-member-section-body">
        <div class="bundle-member-list">${availabilityNotice}${members}</div>
      </div>
    </div>
    <div class="bundle-detail-section">
      <div class="bundle-section-cap">
        <span class="bundle-section-label">Bundle Filament Mapping</span>
      </div>
      <div class="bundle-section-body">
        <div class="bundle-mapping-summary">
          <div class="bundle-slot-summary">${slotSummary}</div>
          <button type="button" class="ghost-button small bundle-open-mapping-btn" data-bundle-id="${_escAttr(bundle.geometry_bundle_id || "")}" data-bundle="${_escAttr(bundle.name)}" ${mappingButtonDisabled}>
            Edit Mapping
          </button>
        </div>
      </div>
    </div>
  `;
}

function _escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function _escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function filamentSelectorGroups(filaments = data.filaments, query = "") {
  const q = query.trim().toLowerCase();
  const filtered = [...(filaments || [])].filter((fil) => {
    if (!q) return true;
    const haystack = [
      fil.manufacturer,
      fil.color_name,
      fil.display_name,
      fil.filament_id,
    ].filter(Boolean).join(" ").toLowerCase();
    const reverseName = [fil.color_name, fil.manufacturer].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(q) || reverseName.includes(q);
  });
  filtered.sort((a, b) =>
    (a.manufacturer || "").localeCompare(b.manufacturer || "") ||
    (a.color_name || a.display_name || "").localeCompare(b.color_name || b.display_name || "")
  );
  return filtered.reduce((groups, fil) => {
    const manufacturer = (fil.manufacturer || "Other").trim() || "Other";
    if (!groups[manufacturer]) groups[manufacturer] = [];
    groups[manufacturer].push(fil);
    return groups;
  }, {});
}

function openFilamentSelector(options = {}) {
  const mode = options.mode === "multi" ? "multi" : "single";
  const selected = new Set(options.selectedIds || []);
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay filament-selector-overlay";
  overlay.innerHTML = `
    <div class="info-dialog filament-selector-dialog" role="dialog" aria-modal="true" aria-labeledby="filamentSelectorTitle">
      ${renderDialogHeader({
        title: options.title || "Select Filament",
        titleId: "filamentSelectorTitle",
        headerClass: "filament-selector-header",
        closeButtonHtml: renderWindowCloseButton({
          className: "info-dialog-close",
          label: "Close selector",
          title: "Close selector",
          attributes: "data-filament-selector-close",
        }),
      })}
      <div class="filament-selector-body">
        <input class="filament-selector-search" id="filamentSelectorSearch" type="search" placeholder="Search filaments">
        <div class="filament-selector-results" id="filamentSelectorResults"></div>
      </div>
      ${mode === "multi" ? `
        <div class="info-dialog-footer filament-selector-footer">
          <span class="filament-selector-count" id="filamentSelectorCount"></span>
          <button class="ghost-button small" type="button" id="filamentSelectorCancel">Cancel</button>
          <button class="primary-button small" type="button" id="filamentSelectorApply">Apply</button>
        </div>
      ` : ""}
    </div>
  `;

  const search = overlay.querySelector("#filamentSelectorSearch");
  const results = overlay.querySelector("#filamentSelectorResults");
  const count = overlay.querySelector("#filamentSelectorCount");
  let activeIndex = 0;

  function close() {
    overlay.remove();
  }

  function applySelection() {
    try {
      if (typeof options.onApply === "function") {
        options.onApply(Array.from(selected));
      }
    } finally {
      close();
    }
  }

  function optionButtons() {
    return Array.from(results.querySelectorAll(".filament-selector-option"));
  }

  function activateOption(index, { focus = false } = {}) {
    const options = optionButtons();
    if (options.length === 0) {
      activeIndex = 0;
      return;
    }
    activeIndex = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((button, i) => {
      const isActive = i === activeIndex;
      button.classList.toggle("is-active", isActive);
      button.tabIndex = isActive ? 0 : -1;
      button.setAttribute("aria-current", isActive ? "true" : "false");
    });
    if (focus) {
      options[activeIndex].focus();
      options[activeIndex].scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function gridNavigationIndex(key) {
    const options = optionButtons();
    if (options.length === 0) return activeIndex;
    if (key === "ArrowLeft") return (activeIndex - 1 + options.length) % options.length;
    if (key === "ArrowRight") return (activeIndex + 1) % options.length;

    const active = options[activeIndex];
    if (!active) return activeIndex;
    const activeRect = active.getBoundingClientRect();
    const activeX = activeRect.left + activeRect.width / 2;
    const activeY = activeRect.top + activeRect.height / 2;
    const movingDown = key === "ArrowDown";
    const rowEpsilon = Math.max(4, activeRect.height * 0.35);

    const candidates = options
      .map((button, index) => {
        const rect = button.getBoundingClientRect();
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        return {
          index,
          dx: Math.abs(centerX - activeX),
          dy: Math.abs(centerY - activeY),
          centerY,
        };
      })
      .filter((candidate) => movingDown
        ? candidate.centerY > activeY + rowEpsilon
        : candidate.centerY < activeY - rowEpsilon
      )
      .sort((a, b) => a.dy - b.dy || a.dx - b.dx || a.index - b.index);

    if (candidates.length === 0) {
      return activeIndex;
    }

    const targetRowY = candidates[0].centerY;
    const sameRow = candidates
      .filter((candidate) => Math.abs(candidate.centerY - targetRowY) <= rowEpsilon)
      .sort((a, b) => a.dx - b.dx || a.index - b.index);
    return sameRow[0]?.index ?? activeIndex;
  }

  function chooseOption(id, { focusAfter = false } = {}) {
    if (!id) return;
    if (mode === "single") {
      selected.clear();
      selected.add(id);
      applySelection();
      return;
    }
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    render();
    if (focusAfter) {
      activateOption(activeIndex, { focus: true });
    }
  }

  function render() {
    const groups = filamentSelectorGroups(data.filaments, search.value || "");
    const groupNames = Object.keys(groups).sort((a, b) => a.localeCompare(b));
    if (count) {
      count.textContent = `${selected.size} selected`;
    }
    if (groupNames.length === 0) {
      activeIndex = 0;
      results.innerHTML = `<div class="filament-selector-empty small-copy">No matching filaments</div>`;
      return;
    }
    let optionIndex = 0;
    results.innerHTML = groupNames.map((manufacturer) => `
      <section class="filament-selector-group">
        <h4>${_escHtml(manufacturer)}</h4>
        <div class="filament-selector-group-grid">
          ${groups[manufacturer].map((fil) => {
            const isSelected = selected.has(fil.filament_id);
            const thisIndex = optionIndex++;
            return `
              <button type="button" class="filament-selector-option${isSelected ? " is-selected" : ""}" data-filament-id="${_escAttr(fil.filament_id)}" data-option-index="${thisIndex}" tabindex="-1">
                <span class="filament-selector-check" aria-hidden="true"></span>
                <span class="color-chip" style="background:${_escAttr(fil.hex || '#cccccc')}"></span>
                <span class="filament-selector-option-name">${_escHtml(fil.color_name || fil.display_name || fil.filament_id)}</span>
              </button>
            `;
          }).join("")}
        </div>
      </section>
    `).join("");
    activateOption(activeIndex);
  }

  overlay.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
  });
  overlay.addEventListener("click", (event) => {
    event.stopPropagation();
    if (event.target === overlay) {
      close();
      return;
    }
    const closeBtn = event.target.closest("[data-filament-selector-close], #filamentSelectorCancel");
    if (closeBtn) {
      close();
      return;
    }
    const option = event.target.closest(".filament-selector-option");
    if (!option) return;
    activeIndex = Number(option.dataset.optionIndex || activeIndex);
    chooseOption(option.dataset.filamentId);
  });

  overlay.querySelector("#filamentSelectorApply")?.addEventListener("click", applySelection);
  search.addEventListener("input", () => {
    activeIndex = 0;
    render();
  });
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || event.shiftKey) return;
    const options = optionButtons();
    if (options.length === 0) return;
    event.preventDefault();
    activateOption(activeIndex, { focus: true });
  });
  overlay.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }

    if (["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"].includes(event.key)) {
      const options = optionButtons();
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(gridNavigationIndex(event.key), { focus: true });
      return;
    }

    if (event.key === "Home") {
      const options = optionButtons();
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(0, { focus: true });
      return;
    }

    if (event.key === "End") {
      const options = optionButtons();
      if (options.length === 0) return;
      event.preventDefault();
      activateOption(options.length - 1, { focus: true });
      return;
    }

    if (event.key === "Enter" && event.target?.classList?.contains("filament-selector-option")) {
      event.preventDefault();
      chooseOption(event.target.dataset.filamentId, { focusAfter: true });
    }
  });

  document.body.appendChild(overlay);
  render();
  search.focus();
}

function showBundleIncompleteMappingConfirmDialog(bundleName = "") {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay bundle-mapping-confirm-overlay";
    overlay.innerHTML = `
      <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="bundleMappingIncompleteTitle">
        ${renderDialogHeader({
          title: "Save Incomplete Mapping",
          titleId: "bundleMappingIncompleteTitle",
          closeButtonHtml: renderWindowCloseButton({ id: "bundleMappingIncompleteClose", className: "info-dialog-close" }),
        })}
        <div class="info-dialog-body">
          <p class="info-dialog-lede">${_escHtml(bundleName || "This bundle")} has not been fully mapped yet.</p>
          <p>Bundles must be fully mapped before they can be used to create samples. Save it anyway?</p>
        </div>
        <div class="info-dialog-footer">
          <button class="primary-button small" type="button" id="bundleMappingIncompleteProceed">Save Anyway</button>
          <button class="ghost-button small" type="button" id="bundleMappingIncompleteCancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = (value) => {
      overlay.remove();
      document.removeEventListener("keydown", onKeydown);
      resolve(value);
    };
    const onKeydown = (event) => {
      if (event.key === "Escape") cleanup(false);
    };
    document.addEventListener("keydown", onKeydown);
    overlay.querySelector("#bundleMappingIncompleteProceed")?.addEventListener("click", () => cleanup(true));
    overlay.querySelector("#bundleMappingIncompleteCancel")?.addEventListener("click", () => cleanup(false));
    overlay.querySelector("#bundleMappingIncompleteClose")?.addEventListener("click", () => cleanup(false));
    overlay.addEventListener("click", (event) => { if (event.target === overlay) cleanup(false); });
  });
}

function _bundleMappingDraftFromDetail(bundle) {
  const savedSlots = Array.isArray(bundle?.material_slots) ? bundle.material_slots : [];
  const draftSlots = savedSlots.length
    ? savedSlots.map((slot, index) => ({
        draft_slot_id: slot.material_slot_id || `slot-${index}`,
        saved_material_slot_id: slot.material_slot_id || "",
      }))
    : [{ draft_slot_id: "draft-a" }];
  const assignments = {};
  (bundle?.members || []).forEach((member) => {
    (member.roles || []).forEach((role) => {
      if (role.material_slot_id) assignments[role.geometry_role_id] = role.material_slot_id;
    });
  });
  return {
    bundle,
    draftSlots,
    assignments,
    selectedDraftSlotId: null,
    dragDraftSlotId: null,
    validation: "",
    stale: false,
  };
}

function _bundleMappingAssignedSlotIds(state) {
  return new Set(Object.values(state.assignments || {}).filter(Boolean));
}

function _bundleMappingRoleCount(state) {
  return (state.bundle?.members || []).reduce((total, member) => total + (member.roles || []).length, 0);
}

function _bundleMappingMappedRoleCount(state) {
  return (state.bundle?.members || []).reduce((total, member) => {
    return total + (member.roles || []).filter((role) => !!state.assignments[role.geometry_role_id]).length;
  }, 0);
}

function _bundleMappingIsComplete(state) {
  const total = _bundleMappingRoleCount(state);
  return total > 0 && _bundleMappingMappedRoleCount(state) === total;
}

function _bundleMappingSlotIndex(state, draftSlotId) {
  const index = (state.draftSlots || []).findIndex((slot) => slot.draft_slot_id === draftSlotId);
  return index >= 0 ? index : 0;
}

function _bundleMappingSlotLabel(state, draftSlotId) {
  return `Shared Filament ${_bundleSlotKey(_bundleMappingSlotIndex(state, draftSlotId))}`;
}

function _bundleMappingAddSlot(state) {
  state.draftSlots.push({
    draft_slot_id: `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
  });
}

function _bundleMappingRemoveSlot(state, draftSlotId) {
  const assigned = _bundleMappingAssignedSlotIds(state);
  if (assigned.has(draftSlotId)) {
    showImportToast("Shared filament slot is in use. Clear its assignments before removing it.", "warning");
    return false;
  }
  if ((state.draftSlots || []).length <= 1) {
    showImportToast("At least one shared filament slot must remain while editing.", "warning");
    return false;
  }
  state.draftSlots = state.draftSlots.filter((slot) => slot.draft_slot_id !== draftSlotId);
  if (state.selectedDraftSlotId === draftSlotId) state.selectedDraftSlotId = null;
  return true;
}

async function _refreshBundleDrawerAfterMapping(bundleName) {
  _bundleDrawerState.bundles = await fetchBundles();
  const match = (_bundleDrawerState.bundles || []).find((bundle) => bundle.name === bundleName);
  _bundleDrawerState.selectedBundleName = match?.name || bundleName;
  renderBundleMgmtBody();
  bindBundleMgmtEvents();
}

async function openBundleMappingDialog(bundleSummary) {
  const bundleId = bundleSummary?.geometry_bundle_id;
  if (!bundleId) {
    showImportToast("Bundle is missing a stable id", "error");
    return;
  }

  let state;
  try {
    state = _bundleMappingDraftFromDetail(await fetchGeometryBundle(bundleId));
  } catch (err) {
    showImportToast("Failed to load bundle mapping: " + err.message, "error");
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay bundle-mapping-overlay";
  document.body.appendChild(overlay);

  const close = () => {
    document.removeEventListener("keydown", onKeydown);
    overlay.remove();
  };

  const reload = async () => {
    try {
      state = _bundleMappingDraftFromDetail(await fetchGeometryBundle(bundleId));
      showImportToast("Reloaded bundle mapping", "success");
      render();
    } catch (err) {
      showImportToast("Failed to reload bundle mapping: " + err.message, "error");
    }
  };

  const assignRole = (roleId, draftSlotId) => {
    if (!roleId || !draftSlotId) return;
    state.assignments[roleId] = draftSlotId;
    state.validation = "";
    state.stale = false;
    render();
  };

  const clearRole = (roleId) => {
    delete state.assignments[roleId];
    state.validation = "";
    render();
  };

  const save = async () => {
    const complete = _bundleMappingIsComplete(state);
    if (!complete) {
      const proceed = await showBundleIncompleteMappingConfirmDialog(state.bundle?.name || bundleSummary?.name || "");
      if (!proceed) return;
    }
    const payload = {
      expected_updated_at: state.bundle?.updated_at || "",
      allow_incomplete: !complete,
      draft_material_slots: state.draftSlots.map((slot) => ({
        draft_slot_id: slot.draft_slot_id,
        label: _bundleMappingSlotLabel(state, slot.draft_slot_id),
      })),
      members: (state.bundle?.members || []).map((member) => ({
        geometry_bundle_member_id: member.geometry_bundle_member_id,
        role_slot_map: (member.roles || []).map((role) => ({
          geometry_role_id: role.geometry_role_id,
          draft_slot_id: state.assignments[role.geometry_role_id] || null,
        })),
      })),
    };
    const saveBtn = overlay.querySelector("#bundleMappingSave");
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving...";
    }
    try {
      const saved = await saveGeometryBundleMapping(bundleId, payload);
      state = _bundleMappingDraftFromDetail(saved);
      await _refreshBundleDrawerAfterMapping(saved.name || bundleSummary?.name || "");
      showImportToast(`Saved mapping for "${saved.name || bundleSummary?.name || "bundle"}"`, "success");
      close();
    } catch (err) {
      state.validation = err.message || "Failed to save bundle mapping";
      state.stale = Number(err.status) === 409;
      render();
      showImportToast(state.validation, state.stale ? "warning" : "error");
    }
  };

  function renderSlotToken(slot, index) {
    const key = _bundleSlotKey(index);
    const label = `Shared Filament ${key}`;
    const color = _bundleSlotColor(key);
    const used = _bundleMappingAssignedSlotIds(state).has(slot.draft_slot_id);
    const selected = state.selectedDraftSlotId === slot.draft_slot_id;
    return `
      <div class="bundle-map-slot-row">
        <button type="button"
          class="bundle-map-token${selected ? " is-selected" : ""}"
          draggable="true"
          data-draft-slot-id="${_escAttr(slot.draft_slot_id)}"
          aria-pressed="${selected ? "true" : "false"}"
          title="${_escAttr(label)}">
          <span class="bundle-map-token-color" style="background:${_escAttr(color)}"></span>
          <span class="bundle-map-token-label">${_escHtml(label)}</span>
        </button>
        <button type="button" class="sb-layer-remove-button bundle-map-remove-slot" data-draft-slot-id="${_escAttr(slot.draft_slot_id)}" aria-label="Remove ${_escAttr(label)}" title="${used ? "Clear assignments before removing" : "Remove shared filament slot"}">
          <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M3 3 L9 9 M9 3 L3 9"></path>
          </svg>
        </button>
      </div>
    `;
  }

  function renderDropZone(role) {
    const draftSlotId = state.assignments[role.geometry_role_id] || "";
    const assignedIndex = draftSlotId ? _bundleMappingSlotIndex(state, draftSlotId) : -1;
    const key = assignedIndex >= 0 ? _bundleSlotKey(assignedIndex) : "";
    const color = assignedIndex >= 0 ? _bundleSlotColor(key) : "";
    const text = color ? textColor(color) : "";
    const roleToken = compactLayerRoleToken(role.role_label, Number(role.role_index || 0));
    const style = color
      ? ` style="--bundle-map-slot-color:${_escAttr(color)};--bundle-map-slot-text:${_escAttr(text)}"`
      : "";
    return `
      <div class="bundle-map-role-zone${draftSlotId ? " is-assigned" : ""}" data-role-id="${_escAttr(role.geometry_role_id)}" tabindex="0" role="button" aria-label="${_escAttr(formatLayerRoleLabel(role))}"${style}>
        <span class="bundle-map-role-label">${_escHtml(roleToken)}</span>
        ${draftSlotId ? `
          <span class="bundle-map-assigned-token">
            <span class="bundle-map-token-color" style="background:${_escAttr(color)}"></span>
            <span>${_escHtml(key)}</span>
          </span>
          <button type="button" class="sb-layer-remove-button bundle-map-clear-role" data-role-id="${_escAttr(role.geometry_role_id)}" aria-label="Clear assignment" title="Clear assignment">
            <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
              <path d="M3 3 L9 9 M9 3 L3 9"></path>
            </svg>
          </button>
        ` : `<span class="bundle-map-unmapped">Unmapped</span>`}
      </div>
    `;
  }

  function renderMappedStripDiagram(step, member) {
    const variableSlots = [...(step?.swatch_slots || [])]
      .sort((a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0));
    if (!variableSlots.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
    }
    const swatchCount = variableSlots.length || Number(step?.swatch_count || step?.layout_columns || 8);
    const stepRoleByIndex = new Map((step?.roles || []).map((role) => [Number(role.role_index || 0), role]));
    const roles = [...(member?.roles || [])].sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
    if (!roles.length) {
      return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
    }
    const rows = roles.map((role) => {
      const stepRole = stepRoleByIndex.get(Number(role.role_index || 0)) || {};
      const roleKind = role.role_kind || stepRole.role_kind || "";
      const draftSlotId = state.assignments[role.geometry_role_id] || "";
      const assignedIndex = draftSlotId ? _bundleMappingSlotIndex(state, draftSlotId) : -1;
      const key = assignedIndex >= 0 ? _bundleSlotKey(assignedIndex) : "";
      const color = assignedIndex >= 0 ? _bundleSlotColor(key) : "#ececea";
      const text = textColor(color);
      if (roleKind === "variable") {
        const cells = variableSlots.map((slot) => (
          `<td style="background:${_escAttr(color)};color:${_escAttr(text)}">${Number(slot.variable_thickness_mm || 0).toFixed(2)}</td>`
        )).join("");
        return `<tr>${cells}</tr>`;
      }
      const fixedThickness = role.fixed_thickness_mm ?? stepRole.fixed_thickness_mm;
      const value = Number(fixedThickness || 0).toFixed(2);
      return `<tr><td colspan="${swatchCount}" style="background:${_escAttr(color)};color:${_escAttr(text)}">${value}mm</td></tr>`;
    });
    return `<table class="mini-strip-table bundle-map-strip-table">${rows.join("")}</table>`;
  }

  function renderMemberCard(member) {
    const step = stepRecordByRef(member.geometry_id);
    const label = member.geometry_alias || _geometryLabelForStep(step) || member.geometry_id;
    const roleZones = [...(member.roles || [])]
      .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
      .map(renderDropZone)
      .join("");
    return `
      <section class="bundle-map-member-card">
        <div class="bundle-map-member-head">
          <h4>${_escHtml(label)}</h4>
        </div>
        <div class="bundle-map-member-body">
          <div class="bundle-map-member-strip">${step ? renderMappedStripDiagram(step, member) : ""}</div>
          <div class="bundle-map-role-grid">
            ${roleZones}
          </div>
        </div>
      </section>
    `;
  }

  function bindDialogEvents() {
    overlay.querySelectorAll(".bundle-map-token").forEach((token) => {
      token.addEventListener("click", () => {
        const id = token.dataset.draftSlotId || "";
        state.selectedDraftSlotId = state.selectedDraftSlotId === id ? null : id;
        render();
      });
      token.addEventListener("dragstart", (event) => {
        state.dragDraftSlotId = token.dataset.draftSlotId || "";
        event.dataTransfer?.setData("text/plain", state.dragDraftSlotId);
        event.dataTransfer.effectAllowed = "copy";
      });
      token.addEventListener("dragend", () => {
        state.dragDraftSlotId = null;
        overlay.querySelectorAll(".bundle-map-role-zone.is-drag-over").forEach((zone) => zone.classList.remove("is-drag-over"));
      });
    });

    overlay.querySelectorAll(".bundle-map-remove-slot").forEach((button) => {
      button.addEventListener("click", () => {
        if (_bundleMappingRemoveSlot(state, button.dataset.draftSlotId || "")) render();
      });
    });

    overlay.querySelectorAll(".bundle-map-role-zone").forEach((zone) => {
      zone.addEventListener("dragover", (event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
        zone.classList.add("is-drag-over");
      });
      zone.addEventListener("dragleave", () => zone.classList.remove("is-drag-over"));
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("is-drag-over");
        const draftSlotId = event.dataTransfer?.getData("text/plain") || state.dragDraftSlotId || "";
        assignRole(zone.dataset.roleId || "", draftSlotId);
      });
      zone.addEventListener("click", (event) => {
        if (event.target.closest(".bundle-map-clear-role")) return;
        if (state.selectedDraftSlotId) assignRole(zone.dataset.roleId || "", state.selectedDraftSlotId);
      });
      zone.addEventListener("keydown", (event) => {
        if ((event.key === "Enter" || event.key === " ") && state.selectedDraftSlotId) {
          event.preventDefault();
          assignRole(zone.dataset.roleId || "", state.selectedDraftSlotId);
        }
      });
    });

    overlay.querySelectorAll(".bundle-map-clear-role").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        clearRole(button.dataset.roleId || "");
      });
    });

    overlay.querySelector("#bundleMappingAddSlot")?.addEventListener("click", () => {
      _bundleMappingAddSlot(state);
      render();
    });
    overlay.querySelector("#bundleMappingSave")?.addEventListener("click", save);
    overlay.querySelector("#bundleMappingReload")?.addEventListener("click", reload);
    overlay.querySelector("#bundleMappingCancel")?.addEventListener("click", close);
    overlay.querySelector("[data-bundle-mapping-close]")?.addEventListener("click", close);
  }

  function render() {
    const mapped = _bundleMappingMappedRoleCount(state);
    const total = _bundleMappingRoleCount(state);
    const complete = _bundleMappingIsComplete(state);
    overlay.innerHTML = `
      <div class="info-dialog bundle-mapping-dialog" role="dialog" aria-modal="true" aria-labeledby="bundleMappingTitle">
        ${renderDialogHeader({
          title: "Bundle Filament Mapping",
          titleId: "bundleMappingTitle",
          subtitle: `${state.bundle?.name || bundleSummary?.name || "Bundle"} · ${mapped}/${total} roles mapped`,
          headerClass: "bundle-mapping-header",
          closeButtonHtml: renderWindowCloseButton({
            className: "info-dialog-close",
            label: "Close mapping dialog",
            title: "Close mapping dialog",
            attributes: "data-bundle-mapping-close",
          }),
        })}
        <div class="bundle-mapping-body">
          <section class="bundle-map-members-pane" aria-label="Bundle geometries">
            ${(state.bundle?.members || []).length ? (state.bundle.members || []).map(renderMemberCard).join("") : `<div class="bundle-member-empty">This bundle has no geometries yet.</div>`}
          </section>
          <aside class="bundle-map-slots-pane" aria-label="Shared filament slots">
            <div class="bundle-map-slots-head">
              <h4>Shared Filament Slots</h4>
            </div>
            <div class="bundle-map-token-list">
              ${state.draftSlots.map(renderSlotToken).join("")}
              <button type="button" class="bundle-map-add-slot-button" id="bundleMappingAddSlot">+ Add Shared Filament Slot</button>
            </div>
            <div class="bundle-map-slot-note small-copy">${state.selectedDraftSlotId ? "Click a role slot to assign the selected shared filament." : "Drag a token onto a role slot, or click a token then click a role slot."}</div>
          </aside>
        </div>
        <div class="bundle-mapping-validation${state.validation ? " is-visible" : ""}">
          ${state.validation ? _escHtml(state.validation) : ""}
        </div>
        <div class="info-dialog-footer bundle-mapping-footer">
          <span class="bundle-mapping-footer-status ${complete ? "is-complete" : "is-incomplete"}">${complete ? "Fully mapped" : "Incomplete mapping"}</span>
          ${state.stale ? `<button class="ghost-button small" type="button" id="bundleMappingReload">Reload</button>` : ""}
          <button class="ghost-button small" type="button" id="bundleMappingCancel">Cancel</button>
          <button class="primary-button small" type="button" id="bundleMappingSave">Save Mapping</button>
        </div>
      </div>
    `;
    bindDialogEvents();
  }

  function onKeydown(event) {
    if (event.key === "Escape") close();
  }

  overlay.addEventListener("click", (event) => {
    event.stopPropagation();
    if (event.target === overlay) close();
  });
  overlay.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
  });
  document.addEventListener("keydown", onKeydown);
  render();
}

function bindBundleMgmtEvents() {
}

function _positionBundleMemberPreview(row) {
  const preview = row?.querySelector?.(".bundle-member-preview");
  if (!preview) return;
  row.classList.remove("is-preview-above");
  requestAnimationFrame(() => {
    const list = row.closest(".bundle-member-list");
    const bounds = (list || bundleMgmtBody)?.getBoundingClientRect?.();
    if (!bounds) return;
    const rowRect = row.getBoundingClientRect();
    const previewRect = preview.getBoundingClientRect();
    const belowSpace = bounds.bottom - rowRect.bottom;
    const aboveSpace = rowRect.top - bounds.top;
    row.classList.toggle("is-preview-above", previewRect.height + 8 > belowSpace && aboveSpace > belowSpace);
  });
}

function bindBundleMgmtInteractions() {
  // New bundle create
  const newSaveBtn = document.getElementById("bundleNewSaveBtn");
  const newNameInput = document.getElementById("bundleNewNameInput");

  if (newSaveBtn && newNameInput) {
    const doCreate = async () => {
      const name = newNameInput.value.trim();
      if (!name) {
        showImportToast("Bundle name cannot be empty", "error");
        return;
      }
      try {
        await createBundle(name);
        _bundleDrawerState.bundles = await fetchBundles();
        _bundleDrawerState.selectedBundleName = name;
        _bundleDrawerState.renamingBundleName = null;
        showImportToast(`Created bundle "${name}"`, "success");
        renderBundleMgmtBody();
      } catch (err) {
        showImportToast("Failed to create bundle: " + err.message, "error");
      }
    };
    newSaveBtn.addEventListener("click", doCreate);
    newNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doCreate();
      if (e.key === "Escape") {
        newNameInput.value = "";
      }
    });
  }

  bundleMgmtBody.querySelectorAll(".bundle-list-item").forEach((button) => {
    button.addEventListener("click", () => {
      _bundleDrawerState.selectedBundleName = button.dataset.bundle;
      _bundleDrawerState.renamingBundleName = null;
      renderBundleMgmtBody();
      bindBundleMgmtEvents();
    });
  });

  bundleMgmtBody.querySelectorAll(".bundle-rename-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      _bundleDrawerState.renamingBundleName = btn.dataset.bundle;
      renderBundleMgmtBody();
      bindBundleMgmtEvents();
      const input = document.getElementById("bundleRenameInput");
      if (input) {
        input.focus();
        input.select();
      }
    });
  });

  const renameInput = document.getElementById("bundleRenameInput");
  const renameSaveBtn = document.getElementById("bundleRenameSaveBtn");
  const renameCancelBtn = document.getElementById("bundleRenameCancelBtn");
  if (renameInput && renameSaveBtn) {
    const oldName = renameSaveBtn.dataset.bundle;
    const doRename = async () => {
      const newName = renameInput.value.trim();
      if (!newName) {
        showImportToast("Bundle name cannot be empty", "error");
        return;
      }
      if (newName === oldName) {
        _bundleDrawerState.renamingBundleName = null;
        renderBundleMgmtBody();
        bindBundleMgmtEvents();
        return;
      }
      try {
        await updateBundle(oldName, { new_name: newName });
        _bundleDrawerState.bundles = await fetchBundles();
        _bundleDrawerState.selectedBundleName = newName;
        _bundleDrawerState.renamingBundleName = null;
        showImportToast(`Renamed bundle to "${newName}"`, "success");
        renderBundleMgmtBody();
        bindBundleMgmtEvents();
      } catch (err) {
        showImportToast("Failed to rename bundle: " + err.message, "error");
      }
    };
    renameSaveBtn.addEventListener("click", doRename);
    renameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doRename();
      if (e.key === "Escape") {
        _bundleDrawerState.renamingBundleName = null;
        renderBundleMgmtBody();
        bindBundleMgmtEvents();
      }
    });
  }
  if (renameCancelBtn) {
    renameCancelBtn.addEventListener("click", () => {
      _bundleDrawerState.renamingBundleName = null;
      renderBundleMgmtBody();
      bindBundleMgmtEvents();
    });
  }

  bundleMgmtBody.querySelectorAll(".bundle-remove-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const bundleName = btn.dataset.bundle;
      const stepId = btn.dataset.step;
      try {
        await removeStepFromBundle(bundleName, stepId);
        _bundleDrawerState.bundles = await fetchBundles();
        _bundleDrawerState.selectedBundleName = bundleName;
        renderBundleMgmtBody();
        bindBundleMgmtEvents();
      } catch (err) {
        showImportToast("Failed to remove geometry: " + err.message, "error");
      }
    });
  });

  bundleMgmtBody.querySelectorAll(".bundle-member-row").forEach((row) => {
    row.addEventListener("mouseenter", () => _positionBundleMemberPreview(row));
    row.addEventListener("focusin", () => _positionBundleMemberPreview(row));
  });

  bundleMgmtBody.querySelectorAll(".bundle-add-step-field").forEach((field) => {
    field.addEventListener("click", () => {
      const bundleName = field.dataset.bundle;
      const selected = (_bundleDrawerState.bundles || []).find((bundle) => bundle.name === bundleName);
      const availableSteps = _availableBundleGeometries(selected?.step_ids || []);
      openGeometrySelector({
        title: "Add Geometries to Bundle",
        mode: "multi",
        applyLabel: "Add Selected",
        selectPanelTitle: "Select Geometries to Add",
        previewPanelTitle: "Selected Geometry Preview",
        steps: availableSteps,
        onApply: async (stepIds) => {
          const ids = Array.isArray(stepIds) ? stepIds : [];
          if (ids.length === 0) return;
          const failed = [];
          for (const stepId of ids) {
            try {
              await addStepToBundle(bundleName, stepId);
            } catch (err) {
              failed.push({ stepId, message: err.message || "unknown error" });
            }
          }
          try {
            _bundleDrawerState.bundles = await fetchBundles();
            _bundleDrawerState.selectedBundleName = bundleName;
            renderBundleMgmtBody();
            bindBundleMgmtEvents();
          } catch (err) {
            showImportToast("Added geometries, but failed to refresh bundles: " + err.message, "warning");
            return;
          }
          const addedCount = ids.length - failed.length;
          if (failed.length) {
            showImportToast(
              `Added ${addedCount}; ${failed.length} failed while updating "${bundleName}"`,
              addedCount ? "warning" : "error"
            );
          } else {
            showImportToast(`Added ${ids.length} geometr${ids.length === 1 ? "y" : "ies"} to "${bundleName}"`, "success");
          }
        },
      });
    });
  });

  bundleMgmtBody.querySelectorAll(".bundle-open-mapping-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const bundleName = btn.dataset.bundle || "";
      const bundleId = btn.dataset.bundleId || "";
      const bundle = (_bundleDrawerState.bundles || []).find((candidate) =>
        candidate.geometry_bundle_id === bundleId || candidate.name === bundleName
      );
      await openBundleMappingDialog(bundle || { geometry_bundle_id: bundleId, name: bundleName });
    });
  });

  bundleMgmtBody.querySelectorAll(".bundle-delete-btn").forEach((btn) => {
    const bundleName = btn.dataset.bundle;
    bindConfirmAction(btn, {
      armedText: "Confirm delete?",
      onConfirm: async () => {
        try {
          await deleteBundle(bundleName);
          _bundleDrawerState.bundles = await fetchBundles();
          const bundles = _sortedBundles(_bundleDrawerState.bundles || []);
          _bundleDrawerState.selectedBundleName = bundles[0]?.name || null;
          _bundleDrawerState.renamingBundleName = null;
          showImportToast(`Deleted bundle "${bundleName}"`, "success");
          renderBundleMgmtBody();
          bindBundleMgmtEvents();
        } catch (err) {
          showImportToast("Failed to delete bundle: " + err.message, "error");
        }
      },
    });
  });
}

// Update renderBundleOptions to pull from bundles registry when available
async function refreshBundleOptionsFromRegistry() {
  try {
    const bundles = await fetchBundles();
    if (stepBundleOptions) {
      stepBundleOptions.innerHTML = bundles
        .map((b) => `<option value="${b.name}"></option>`)
        .join("");
    }
  } catch (_) {
    // Fall back to existing stepMetadata-based bundle names
    renderBundleOptions();
  }
}

function bindStepStoragePathButton(stepStoragePath) {
  const button = document.getElementById("copyStepStoragePath");
  if (!button) return;

  button.addEventListener("click", async () => {
    let copied = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(stepStoragePath);
        copied = true;
      }
    } catch (error) {
      copied = false;
    }

    if (!copied) {
      const tempInput = document.createElement("input");
      tempInput.value = stepStoragePath;
      document.body.appendChild(tempInput);
      tempInput.select();
      document.execCommand("copy");
      document.body.removeChild(tempInput);
      copied = true;
    }

    const originalText = button.textContent;
    button.textContent = copied ? "Copied export path" : originalText;
    setTimeout(() => {
      button.textContent = originalText;
    }, 1200);
  });
}

function bindStepBuilderControls() {
  const populateBtn = _sbEl("populateStepBtn");
  const saveBtn = document.getElementById("stepBuilderSave");
  const createExportBtn = document.getElementById("stepBuilderCreateExport");
  const startInput = _sbEl("stepStartValue");
  const incInput = _sbEl("stepIncrementValue");
  const lhInput = _sbEl("stepLayerHeight");
  const countInput = _sbEl("stepColumnCount");
  const swatchWidthInput = _sbEl("stepSwatchWidth");
  const swatchHeightInput = _sbEl("stepSwatchHeight");
  const spineWidthInput = _sbEl("stepSpineWidth");
  const spineTotalInput = _sbEl("stepSpineTotalThickness");
  const aliasInput = _sbEl("stepBuilderAlias");
  const bundleInput = _sbEl("stepBuilderBundle");
  const copyPathBtn = _sbEl("copyStepBuilderPath");
  const structuredMode = isStructuredGeometryBackend();

  if (populateBtn) populateBtn.addEventListener("click", () => {
    populateStepValues();
    renderStepBuilder();
  });

  const doCreate = async ({ openExport = false } = {}) => {
    // Validate all inputs
    const validationEl = _sbEl("stepValidationError");
    const errors = [];
    clearStepBuilderValidationHighlights();
    const lhVal = numericValue(lhInput ? lhInput.value : "", NaN);
    if (!structuredMode && (isNaN(lhVal) || lhVal <= 0)) errors.push("Layer height must be a positive number");
    if (structuredMode) {
      const payload = structuredGeometryPayloadFromBuilder();
      const rawCount = Number(countInput?.value || "");
      if (!Number.isInteger(rawCount) || rawCount < 1 || rawCount > 48) {
        errors.push("Swatch count must be a whole number from 1 to 48");
        markStepBuilderInvalid(countInput);
      }
      if (!payload.alias) {
        errors.push("Alias is required");
        markStepBuilderInvalid(aliasInput);
      }
      if (!Number.isFinite(payload.swatch_width_mm) || payload.swatch_width_mm <= 0) {
        errors.push("Swatch width must be a positive number");
        markStepBuilderInvalid(swatchWidthInput);
      }
      if (!Number.isFinite(payload.swatch_height_mm) || payload.swatch_height_mm <= 0) {
        errors.push("Swatch height must be a positive number");
        markStepBuilderInvalid(swatchHeightInput);
      }
      if (!Number.isFinite(payload.spine_width_mm) || payload.spine_width_mm <= 0) {
        errors.push("Spine width must be a positive number");
        markStepBuilderInvalid(spineWidthInput);
      }
      if (!Number.isFinite(payload.spine_total_thickness_mm) || payload.spine_total_thickness_mm <= 0) {
        errors.push("Spine total height must be a positive number");
        markStepBuilderInvalid(spineTotalInput);
      }
      if (payload.spine_total_thickness_mm + 1e-9 < stepBuilderStackHeightMax()) {
        errors.push("Spine total height must be at least as tall as the thickest swatch stack");
        markStepBuilderInvalid(spineTotalInput);
      }
    }
    stepBuilderState.values.forEach((v, i) => {
      if (isNaN(numericValue(v, NaN))) {
        errors.push(`Swatch #${i + 1} is not a valid number`);
        markStepBuilderInvalid(document.querySelector(`[data-step-index="${i}"]`));
      }
    });
    stepBuilderState.fixedLayers.forEach((fl, i) => {
      if (isNaN(numericValue(fl.thickness, NaN))) {
        errors.push(`Fixed layer ${i + 1} is not a valid number`);
        markStepBuilderInvalid(
          fl.roleId
            ? document.querySelector(`.fixed-layer-input[data-role-id="${fl.roleId}"]`)
            : document.querySelector(`.fixed-layer-input[data-fixed-index="${i}"]`),
        );
      }
    });
    const filename = buildStepFilename();

    if (errors.length > 0) {
      if (validationEl) { validationEl.style.display = "block"; validationEl.textContent = errors[0]; }
      showImportToast(errors[0], "error");
      return;
    }
    if (validationEl) validationEl.style.display = "none";

    // Generate STEP + STL files
    const varThick = stepBuilderState.values.map((v) => numericValue(v, NaN));
    const fixThick = getStepBuilderFixedThicknesses().map((thickness) => numericValue(thickness, NaN));

    [saveBtn, createExportBtn].forEach((button) => { if (button) button.disabled = true; });
    if (saveBtn) saveBtn.textContent = structuredMode ? "Creating..." : "Generating...";
    try {
      const alias = (aliasInput ? aliasInput.value.trim() : "");
      const bundle = (bundleInput ? bundleInput.value.trim() : "");
      if (structuredMode) {
        const geometry = await createGeometry(structuredGeometryPayloadFromBuilder());
        stepMetadata[geometry.geometry_id] = { alias: geometry.alias || alias, bundle, deleted: false };
        let bundleWarning = "";
        if (bundle && typeof addStepToBundle === "function") {
          try {
            await addStepToBundle(bundle, geometry.geometry_id);
          } catch (bundleErr) {
            bundleWarning = ` Bundle link failed: ${bundleErr.message || "unknown error"}`;
          }
        }
        showImportToast(`Created ${geometry.alias || geometry.geometry_id}.${bundleWarning}`, bundleWarning ? "warning" : "success");
        await handleRefresh();
        closeStepBuilderDrawer();
        if (openExport) openGeometryExportDialog(geometry.geometry_id, geometry.alias || alias);
      } else {
        const result = await generateStepFile(varThick, fixThick, lhVal, filename);

        // Save alias/bundle metadata locally and persist to server
        const generatedStepId = result.step_id || filename;
        stepMetadata[generatedStepId] = { alias, bundle, deleted: false };
        if (typeof updateStepMetadata === "function") {
          await updateStepMetadata(generatedStepId, alias, bundle);
        }
        showImportToast(`${result.reused ? "Reused" : "Saved"} ${result.artifact_filename || filename} + ${result.stl_files.length} STL(s)`, "success");
        handleRefresh();
        closeStepBuilderDrawer();
      }
    } catch (err) {
      const msg = err.message || "Generation failed";
      showImportToast(msg, "error");
      if (validationEl) { validationEl.style.display = "block"; validationEl.textContent = msg; }
    } finally {
      [saveBtn, createExportBtn].forEach((button) => { if (button) button.disabled = false; });
      if (saveBtn) saveBtn.textContent = structuredMode ? "Create" : "Generate";
    }
  };

  if (saveBtn) saveBtn.onclick = () => doCreate({ openExport: false });
  if (createExportBtn) createExportBtn.onclick = () => doCreate({ openExport: true });

  [startInput, incInput, lhInput, swatchWidthInput, swatchHeightInput, spineWidthInput].forEach((input) => {
    bindStepDecimalInput(input, { onInput: () => updateStepPreview() });
  });

  if (spineTotalInput) {
    bindStepDecimalInput(spineTotalInput, {
      onInput: () => {
        spineTotalInput.dataset.userEdited = "true";
        updateStepPreview();
      },
    });
  }

  if (countInput) {
    const normalizeCount = () => {
      countInput.value = String(countInput.value || "").replace(/[^\d]/g, "");
    };
    countInput.addEventListener("change", () => {
      normalizeCount();
      resizeStepBuilderValues(countInput.value);
      renderStepBuilder();
    });
    countInput.addEventListener("input", () => {
      normalizeCount();
      resizeStepBuilderValues(countInput.value);
      renderStepBuilder();
    });
  }

  if (aliasInput) aliasInput.addEventListener("input", () => {
    stepBuilderState.alias = aliasInput.value.trim();
    updateStepPreview();
  });

  if (bundleInput) bundleInput.addEventListener("change", () => {
    stepBuilderState.bundle = bundleInput.value.trim();
    updateStepPreview();
  });

  if (copyPathBtn) {
    copyPathBtn.addEventListener("click", async () => {
      // Copy the full system path — the server provides data_root info
      const fullPath = (data._stepLibraryFullPath || (_serverConfig ? _serverConfig.step_library_path : "step/"));
      try { await navigator.clipboard.writeText(fullPath); } catch (e) {
        const t = document.createElement("input"); t.value = fullPath;
        document.body.appendChild(t); t.select(); document.execCommand("copy"); document.body.removeChild(t);
      }
      const orig = copyPathBtn.textContent;
      copyPathBtn.textContent = "Copied!";
      setTimeout(() => { copyPathBtn.textContent = orig; }, 1200);
    });
  }
}

// ── Filament detail drawer (view / edit / create) ────────────────────────────

function _filamentSpecialRoles(fil) {
  return Array.isArray(fil?.special_roles) ? fil.special_roles : [];
}

function _filamentMaterial(fil) {
  return (fil?.material || "unknown").trim() || "unknown";
}

function _filamentNotes(fil) {
  return (fil?.notes || "").trim();
}

function _filamentRolesLabel(fil) {
  const roles = _filamentSpecialRoles(fil);
  return roles.length ? roles.join(", ") : "None";
}

function _filamentPolicyHtml(fil) {
  const excluded = !!fil.exclude_from_model;
  return `
    <div class="filament-policy-grid">
      <span class="sidebar-label">White Cap</span>
      <span class="drawer-form-value">${fil.white_cap_eligible ? "Eligible" : "Not eligible"}</span>
      <span class="sidebar-label">Special Roles</span>
      <span class="drawer-form-value">${escapeHtml(_filamentRolesLabel(fil))}</span>
      <span class="sidebar-label">Model Fit</span>
      <span class="drawer-form-value">${excluded ? "Excluded" : "Included"}</span>
      <span class="sidebar-label">Generation</span>
      <span class="drawer-form-value">${excluded ? "Unavailable for new generation" : "Available"}</span>
    </div>
  `;
}

function _filamentOptionsHtml(fil = {}) {
  const roles = new Set(_filamentSpecialRoles(fil));
  const excluded = !!fil.exclude_from_model;
  return `
    <div class="filament-option-stack">
      <label class="filament-option-row">
        <input type="checkbox" id="filWhiteCapEligible" ${fil.white_cap_eligible ? "checked" : ""}>
        <span>White-cap eligible</span>
      </label>
      <label class="filament-option-row">
        <input type="checkbox" id="filSpecialRoleTransparent" ${roles.has("transparent") ? "checked" : ""}>
        <span>Special role: Transparent</span>
      </label>
      <label class="filament-option-row">
        <input type="checkbox" id="filSpecialRoleBlack" ${roles.has("black") ? "checked" : ""}>
        <span>Special role: Black</span>
      </label>
      <label class="filament-option-row filament-option-row-warning">
        <input type="checkbox" id="filExcludeFromModel" ${excluded ? "checked" : ""}>
        <span>Exclude from model fits</span>
      </label>
    </div>
  `;
}

function _readFilamentOptions() {
  const roles = [];
  if (document.getElementById("filSpecialRoleTransparent")?.checked) roles.push("transparent");
  if (document.getElementById("filSpecialRoleBlack")?.checked) roles.push("black");
  return {
    material: (document.getElementById("filEditMaterial")?.value || "").trim() || "unknown",
    whiteCapEligible: !!document.getElementById("filWhiteCapEligible")?.checked,
    specialRoles: roles,
    excludeFromModel: !!document.getElementById("filExcludeFromModel")?.checked,
    notes: document.getElementById("filEditNotes")?.value || "",
  };
}

function _setFilamentValidation(validation, message, fields = []) {
  document.querySelectorAll(".filament-field-invalid").forEach((el) => {
    el.classList.remove("filament-field-invalid");
  });
  if (!validation) return;
  validation.textContent = message || "";
  validation.className = message ? "filament-validation is-error" : "filament-validation";
  fields.forEach((field) => field?.classList?.add("filament-field-invalid"));
  if (message) showImportToast(message, "error");
}

function renderSidebarForFilament(fil) {
  _filamentDrawerMode = "view";
  _filamentDrawerData = fil;
  recordDrawer.classList.remove("sample-expanded");
  recordDrawer.classList.remove("sample-set-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  recordDrawer.classList.add("narrow-drawer");
  _renderFilamentDrawerView(fil);
  openRecordDrawer();
}

function openFilamentCreateDrawer() {
  _filamentDrawerMode = "create";
  _filamentDrawerData = null;
  selectedRecord = { kind: "filament", id: "__new__" };
  recordDrawer.classList.remove("sample-expanded");
  recordDrawer.classList.remove("sample-set-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  recordDrawer.classList.add("narrow-drawer");
  _renderFilamentDrawerCreate();
  openRecordDrawer();
}

function _renderFilamentDrawerView(fil) {
  setDetailSidebarStackMode("form");
  setDrawerHeading(fil.display_name || fil.filament_id);
  const status = filamentStatusMeta(fil);
  const usedBySection = buildFilamentUsedBySection(fil.filament_id);
  detailActionArea.innerHTML = `
    <button class="ghost-button small drawer-header-action" id="filDrawerEditBtn">Edit</button>
  `;
  detailWindowArea.innerHTML = "";
  drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;

  detailSidebar.innerHTML = `
    ${buildDrawerFormModule("Filament", `
      <div class="filament-header-row">
        <span class="filament-drawer-chip" style="background:${escapeHtml(fil.hex)}"></span>
        <strong>${escapeHtml(fil.color_name || fil.display_name || fil.filament_id)}</strong>
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Manufacturer", `<span class="drawer-form-value">${escapeHtml(fil.manufacturer)}</span>`, { density: "compact" })}
    ${buildDrawerFormModule("Material", `<span class="drawer-form-value">${escapeHtml(_filamentMaterial(fil))}</span>`, { density: "compact" })}
    ${buildDrawerFormModule("Color", `
      <div class="filament-color-labeled">
        <span class="sidebar-label">Hex</span>
        <span class="mono">${escapeHtml(fil.hex)}</span>
        <span class="sidebar-label">RGB</span>
        <span class="mono">${escapeHtml(hexToRgbString(fil.hex))}</span>
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Policy", _filamentPolicyHtml(fil), { density: "compact" })}
    ${buildDrawerFormModule("Notes", `<span class="drawer-form-value">${escapeHtml(_filamentNotes(fil) || "None")}</span>`, { density: "compact" })}
    ${buildDrawerFormModule(`Used By ${usedBySection.count} Sample${usedBySection.count === 1 ? "" : "s"}`, usedBySection.html, { density: "table" })}
    ${buildDrawerFormModule("ID", `<span class="mono drawer-form-value filament-slug-preview">${escapeHtml(fil.filament_id)}</span>`, { density: "compact" })}
  `;
  bindLinkedSampleTriggers(detailSidebar);

  document.getElementById("filDrawerEditBtn")?.addEventListener("click", () => {
    _filamentDrawerMode = "edit";
    _renderFilamentDrawerEdit(fil);
  });
}

function _renderFilamentDrawerEdit(fil) {
  setDetailSidebarStackMode("form");
  setDrawerHeading(fil.display_name || fil.filament_id);
  const status = filamentStatusMeta(fil);
  const usedBySection = buildFilamentUsedBySection(fil.filament_id);
  drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
  detailWindowArea.innerHTML = "";
  detailActionArea.innerHTML = `
    <button class="primary-button small drawer-header-action" id="filDrawerSaveBtn">Save</button>
    <button class="ghost-button small drawer-header-action" id="filDrawerDiscardBtn">Discard</button>
    <button class="delete-button small drawer-header-action" id="filDrawerDeleteBtn">Delete</button>
  `;

  detailSidebar.innerHTML = `
    ${buildDrawerFormModule("Filament", `
      <div class="filament-header-row filament-header-row-edit">
        <span class="filament-drawer-chip" id="filEditChip" style="background:${escapeHtml(fil.hex)}"></span>
        <input type="text" id="filEditColorName" class="filament-inline-name-input filament-drawer-input" value="${escapeHtml(fil.color_name)}" placeholder="e.g. Basic Blue">
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Manufacturer", `
      <div class="filament-edit-field filament-inline-field">
        <input type="text" id="filEditMfr" class="filament-drawer-input" value="${escapeHtml(fil.manufacturer)}">
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Material", `
      <div class="filament-edit-field filament-inline-field">
        <input type="text" id="filEditMaterial" class="filament-drawer-input" value="${escapeHtml(_filamentMaterial(fil))}" placeholder="e.g. PLA">
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Hex Color", `
      <div class="filament-edit-field filament-inline-field">
        <div class="filament-hex-row">
          <input type="color" id="filEditPicker" value="${escapeHtml(fil.hex)}">
          <input type="text" id="filEditHex" class="filament-drawer-input" value="${escapeHtml(fil.hex)}" maxlength="7">
        </div>
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Policy", _filamentOptionsHtml(fil), { density: "large", classes: "filament-policy-module" })}
    ${buildDrawerFormModule("Notes", `
      <textarea id="filEditNotes" class="sample-create-textarea filament-notes-input" rows="3" placeholder="Optional notes...">${escapeHtml(_filamentNotes(fil))}</textarea>
    `, { density: "large" })}
    ${buildDrawerFormModule(`Used By ${usedBySection.count} Sample${usedBySection.count === 1 ? "" : "s"}`, usedBySection.html, { density: "table" })}
    ${buildDrawerFormModule("ID", `<span class="mono drawer-form-value filament-slug-preview">${escapeHtml(fil.filament_id)}</span>`, { density: "compact" })}
    <div class="filament-validation" id="filEditValidation"></div>
  `;
  bindLinkedSampleTriggers(detailSidebar);

  _bindFilamentEditControls(fil);
}

function _renderFilamentDrawerCreate() {
  setDetailSidebarStackMode("form");
  setDrawerHeading("New Filament");
  drawerStatusPill.innerHTML = "";
  detailWindowArea.innerHTML = "";
  detailActionArea.innerHTML = `
    <button class="primary-button small drawer-header-action" id="filDrawerSaveBtn">Create</button>
  `;

  // Populate manufacturer suggestions
  const manufacturers = [...new Set(data.filaments.map((f) => f.manufacturer).filter(Boolean))].sort();
  const mfrOptions = manufacturers.map((m) => `<option value="${escapeHtml(m)}">`).join("");

  detailSidebar.innerHTML = `
    ${buildDrawerFormModule("Filament", `
      <div class="filament-header-row filament-header-row-edit">
        <span class="filament-drawer-chip" id="filEditChip" style="background:#888888"></span>
        <input type="text" id="filEditColorName" class="filament-inline-name-input filament-drawer-input" value="" placeholder="e.g. Basic Cyan" autocomplete="off">
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Manufacturer", `
      <div class="filament-edit-field filament-inline-field">
        <input type="text" id="filEditMfr" class="filament-drawer-input" value="" placeholder="e.g. Bambu" list="filMfrSuggestions" autocomplete="off">
        <datalist id="filMfrSuggestions">${mfrOptions}</datalist>
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Material", `
      <div class="filament-edit-field filament-inline-field">
        <input type="text" id="filEditMaterial" class="filament-drawer-input" value="unknown" placeholder="e.g. PLA" autocomplete="off">
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Hex Color", `
      <div class="filament-edit-field filament-inline-field">
        <div class="filament-hex-row">
          <input type="color" id="filEditPicker" value="#888888">
          <input type="text" id="filEditHex" class="filament-drawer-input" value="#888888" maxlength="7">
        </div>
      </div>
    `, { density: "compact" })}
    ${buildDrawerFormModule("Policy", _filamentOptionsHtml({ material: "unknown" }), { density: "large", classes: "filament-policy-module" })}
    ${buildDrawerFormModule("Notes", `
      <textarea id="filEditNotes" class="sample-create-textarea filament-notes-input" rows="3" placeholder="Optional notes..."></textarea>
    `, { density: "large" })}
    ${buildDrawerFormModule("Generated ID", `<span class="mono drawer-form-value filament-slug-preview" id="filCreateSlug">\u2014</span>`, { density: "compact" })}
    <div class="filament-validation" id="filEditValidation"></div>
  `;

  _bindFilamentCreateControls();
}

function _bindFilamentEditControls(fil) {
  const mfrInput = document.getElementById("filEditMfr");
  const colorInput = document.getElementById("filEditColorName");
  const picker = document.getElementById("filEditPicker");
  const hexInput = document.getElementById("filEditHex");
  const chip = document.getElementById("filEditChip");
  const saveBtn = document.getElementById("filDrawerSaveBtn");
  const discardBtn = document.getElementById("filDrawerDiscardBtn");
  const deleteBtn = document.getElementById("filDrawerDeleteBtn");
  const validation = document.getElementById("filEditValidation");
  const hasRefs = (fil.sample_count > 0) || fil.has_profile;

  function syncChip() {
    const hex = normalizeHexInput(hexInput.value);
    if (hex) {
      chip.style.background = hex;
      picker.value = hex;
    }
  }

  picker.addEventListener("input", () => {
    hexInput.value = picker.value.toUpperCase();
    syncChip();
  });
  hexInput.addEventListener("input", syncChip);

  discardBtn.addEventListener("click", () => {
    _filamentDrawerMode = "view";
    _renderFilamentDrawerView(fil);
  });

  if (deleteBtn) {
    if (hasRefs) {
      deleteBtn.addEventListener("click", () => {
        showInfoDialog("Filaments cannot be deleted when they have samples associated with them.");
      });
    } else {
      bindConfirmAction(deleteBtn, {
        onConfirm: async () => {
          try {
            await deleteFilament(fil.filament_id);
            showImportToast(`Deleted filament ${fil.filament_id}`, "success");
            closeDrawer();
            await handleRefresh();
          } catch (err) {
            showImportToast(err.message || "Failed to delete filament", "error");
          }
        },
      });
    }
  }

  saveBtn.addEventListener("click", async () => {
    const mfr = mfrInput.value.trim();
    const cn = colorInput.value.trim();
    const hex = normalizeHexInput(hexInput.value);
    const options = _readFilamentOptions();

    if (!mfr) { _setFilamentValidation(validation, "Manufacturer is required.", [mfrInput]); return; }
    if (!cn) { _setFilamentValidation(validation, "Filament name is required.", [colorInput]); return; }
    if (!hex) { _setFilamentValidation(validation, "Hex must be in #RRGGBB format.", [hexInput]); return; }
    _setFilamentValidation(validation, "");

    try {
      const resp = await fetch(`/api/filaments/${encodeURIComponent(fil.filament_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          manufacturer: mfr,
          color_name: cn,
          hex: hex,
          material: options.material,
          white_cap_eligible: options.whiteCapEligible,
          special_roles: options.specialRoles,
          exclude_from_model: options.excludeFromModel,
          notes: options.notes,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        _setFilamentValidation(validation, err.detail || "Server error");
        return;
      }
      showProfileToast(`Updated ${mfr} ${cn}`);
      await handleRefresh();
      // Re-fetch the updated filament from the refreshed data
      const updated = data.filaments.find((f) => f.filament_id === fil.filament_id);
      if (updated) {
        _filamentDrawerMode = "view";
        _filamentDrawerData = updated;
        _renderFilamentDrawerView(updated);
      }
    } catch (err) {
      _setFilamentValidation(validation, `Network error: ${err.message}`);
    }
  });
}

function _bindFilamentCreateControls() {
  const mfrInput = document.getElementById("filEditMfr");
  const colorInput = document.getElementById("filEditColorName");
  const picker = document.getElementById("filEditPicker");
  const hexInput = document.getElementById("filEditHex");
  const chip = document.getElementById("filEditChip");
  const slugEl = document.getElementById("filCreateSlug");
  const saveBtn = document.getElementById("filDrawerSaveBtn");
  const validation = document.getElementById("filEditValidation");

  function syncChip() {
    const hex = normalizeHexInput(hexInput.value);
    if (hex) {
      chip.style.background = hex;
      picker.value = hex;
    }
  }

  function syncPreview() {
    const mfr = mfrInput.value.trim();
    const cn = colorInput.value.trim();
    const slug = _generateFilamentSlug(mfr, cn);
    slugEl.textContent = slug || "\u2014";

    if (slug && data.filaments.some((f) => f.filament_id === slug)) {
      _setFilamentValidation(validation, `ID "${slug}" already exists in the library.`);
    } else {
      _setFilamentValidation(validation, "");
    }
  }

  picker.addEventListener("input", () => {
    hexInput.value = picker.value.toUpperCase();
    syncChip();
  });
  hexInput.addEventListener("input", syncChip);
  mfrInput.addEventListener("input", syncPreview);
  colorInput.addEventListener("input", syncPreview);

  saveBtn.addEventListener("click", async () => {
    const mfr = mfrInput.value.trim();
    const cn = colorInput.value.trim();
    const hex = normalizeHexInput(hexInput.value);
    const options = _readFilamentOptions();

    if (!mfr) { _setFilamentValidation(validation, "Manufacturer is required.", [mfrInput]); return; }
    if (!cn) { _setFilamentValidation(validation, "Filament name is required.", [colorInput]); return; }
    if (!hex) { _setFilamentValidation(validation, "Hex must be in #RRGGBB format.", [hexInput]); return; }

    const slug = _generateFilamentSlug(mfr, cn);
    if (data.filaments.some((f) => f.filament_id === slug)) {
      _setFilamentValidation(validation, `ID "${slug}" already exists.`, [colorInput]);
      return;
    }

    _setFilamentValidation(validation, "");
    try {
      const created = await createFilament(mfr, cn, hex, options);
      showProfileToast(`Added ${created.display_name}`);
      clearSelectionAndDrawer();
      recordDrawer.classList.remove("narrow-drawer");
      _filamentDrawerMode = null;
      _filamentDrawerData = null;
      await handleRefresh();
    } catch (err) {
      _setFilamentValidation(validation, err.message || "Server error");
    }
  });
}


// ── Sample creation state/shared helpers ─────────────────────────────────────

let _sampleDrawerMode = null; // null | "create" | "edit"; create opens the unified New Samples drawer.
let _sampleCreateSteps = []; // cached STEP list from API

function syncSampleStepCacheFromData() {
  _sampleCreateSteps = Array.isArray(data.steps) ? [...data.steps] : [];
}

function renderFilamentSelectorField(button, filamentId) {
  if (!button) return;
  button.dataset.filamentId = filamentId || "";
  const fil = filamentMeta(filamentId);
  if (!fil) {
    button.innerHTML = `<span class="filament-selector-placeholder">Select filament</span>`;
    return;
  }
  button.innerHTML = `
    <span class="color-chip" style="background:${fil.hex || '#cccccc'}"></span>
    <span class="filament-selector-field-name">${_escHtml(fil.color_name || fil.display_name || fil.filament_id)}</span>
    <span class="filament-selector-field-meta">${_escHtml(fil.manufacturer || "")}</span>
  `;
}

function _sampleBatchPreviewName(startId, offset) {
  const match = String(startId || "").match(/^(.*?)(\d+)$/);
  if (!match) return `Sample ${offset + 1}`;
  const prefix = match[1];
  const width = match[2].length;
  const next = String(Number(match[2]) + offset).padStart(width, "0");
  return `${prefix}${next}`;
}

function _bundleStepReferenceLabel(index) {
  let n = index + 1;
  let label = "";
  while (n > 0) {
    n -= 1;
    label = String.fromCharCode(65 + (n % 26)) + label;
    n = Math.floor(n / 26);
  }
  return label;
}

function _buildBundleStepTable(steps) {
  return `
    <div class="bundle-step-table">
      ${steps.map((step, index) => {
        const label = _geometryLabelForStep(step);
        return `
        <div class="bundle-step-row">
          <span class="bundle-step-ref">${_escHtml(_bundleStepReferenceLabel(index))}</span>
          <span class="bundle-step-name" title="${_escAttr(label)}">${_escHtml(label)}</span>
        </div>
      `;
      }).join("")}
    </div>
  `;
}

// ── Unified Bulk Sample drawer ───────────────────────────────────────────────

let _bulkCreateNextId = "...";
let _bulkCreateBundles = [];

async function openBulkSampleCreateDrawer() {
  _sampleDrawerMode = "create";
  _filamentDrawerMode = null;
  _filamentDrawerData = null;
  selectedRecord = { kind: "sample", id: "__bulk__" };
  recordDrawer.classList.remove("narrow-drawer");
  recordDrawer.classList.remove("sample-expanded");
  recordDrawer.classList.remove("model-filament-drawer");
  recordDrawer.classList.add("sample-set-drawer");
  try {
    const [stepsResp, bundlesResp, idResp] = await Promise.all([
      fetchSteps(),
      fetchBundles(),
      fetchNextSampleId(),
    ]);
    _sampleCreateSteps = stepsResp || [];
    _bulkCreateBundles = bundlesResp || [];
    _bulkCreateNextId = idResp?.next_id || "...";
  } catch (err) {
    console.warn("[bulk-create] Failed to fetch steps/bundles/next-id:", err);
    _sampleCreateSteps = [];
    _bulkCreateBundles = [];
    _bulkCreateNextId = "...";
  }

  _renderBulkSampleCreateDrawer();
  openRecordDrawer();
}

function _bulkFilamentButtonHtml(filamentId, placeholder = "Select filament") {
  const fil = filamentMeta(filamentId);
  if (!fil) return `<span class="filament-selector-placeholder">${_escHtml(placeholder)}</span>`;
  return `
    <span class="color-chip" style="background:${_escAttr(fil.hex || "#cccccc")}"></span>
    <span class="filament-selector-field-name">${_escHtml(fil.color_name || fil.display_name || filamentId)}</span>
    <span class="filament-selector-field-meta">${_escHtml(fil.manufacturer || "")}</span>
  `;
}

function _bulkSelectedFilamentChips(filamentIds = []) {
  const ids = [...new Set((filamentIds || []).filter(Boolean))];
  if (!ids.length) return "";
  return `
    <div class="sample-batch-selected-list bulk-selected-filaments">
      ${ids.map((fid) => {
        const fil = filamentMeta(fid);
        if (!fil) return "";
        return `
          <span class="sample-batch-selected-chip">
            <span class="color-chip" style="background:${_escAttr(fil.hex || "#cccccc")}"></span>
            <span>${_escHtml(fil.color_name || fil.display_name || fid)}</span>
          </span>
        `;
      }).join("")}
    </div>
  `;
}

function _bulkGeometrySlots(step) {
  return [...(step?.roles || [])]
    .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
    .map((role) => ({
      slot_id: `role:${Number(role.role_index || 0)}`,
      role_index: Number(role.role_index || 0),
      label: formatLayerRoleLabel(role),
      role_kind: role.role_kind || "",
      fixed_thickness_mm: role.fixed_thickness_mm,
    }));
}

function _bulkBundleSlots(bundle) {
  return [...(bundle?.material_slots || [])]
    .sort((a, b) => Number(a.position || 0) - Number(b.position || 0))
    .map((slot) => ({
      slot_id: slot.material_slot_id,
      label: `Bundle Role ${slot.key || _bundleSlotKey(Number(slot.position || 0))}`.trim(),
      key: slot.key || "",
      color: _bundleSlotColor(slot.key || slot.position || 0),
    }));
}

function _bulkGeometrySlotAssignments(state, batchFilamentId = "") {
  const assignments = {};
  (state.slots || []).forEach((slot) => {
    assignments[slot.slot_id] = state.batchSlotId === slot.slot_id
      ? batchFilamentId
      : (state.slotAssignments[slot.slot_id] || "");
  });
  return assignments;
}

function _bulkGeometryRolePayload(state, batchFilamentId = "") {
  const slotAssignments = _bulkGeometrySlotAssignments(state, batchFilamentId);
  const variableSlot = (state.slots || []).find((slot) => slot.role_kind === "variable");
  const variableFilamentId = variableSlot ? (slotAssignments[variableSlot.slot_id] || "") : "";
  const fixedByRole = new Map();
  (state.slots || []).forEach((slot) => {
    if (slot.role_kind === "fixed") {
      fixedByRole.set(Number(slot.role_index || 0), slotAssignments[slot.slot_id] || "");
    }
  });
  return {
    variableFilamentId,
    fixedByRole,
    fixedIds: canonicalFixedFilamentIdsFromMap(state.source || {}, fixedByRole),
    fixedThicknesses: fixedLayerCanonicalThicknesses(state.source?.fixed_layers || []),
    roleAssignments: (state.slots || []).map((slot) => ({
      role_index: Number(slot.role_index || 0),
      filament_id: slotAssignments[slot.slot_id] || "",
    })),
  };
}

function _bulkGeometryPreviewChipIds(state, batchFilamentId = "") {
  const payload = _bulkGeometryRolePayload(state, batchFilamentId);
  return filamentIdsFromRoleAssignments(state.source || {}, payload.roleAssignments);
}

function _bulkBundleSlotAssignments(state, batchFilamentId = "") {
  const assignments = {};
  (state.slots || []).forEach((slot) => {
    assignments[slot.slot_id] = state.batchSlotId === slot.slot_id
      ? batchFilamentId
      : (state.slotAssignments[slot.slot_id] || "");
  });
  return assignments;
}

function _bulkBundlePreviewChipIds(state, member, batchFilamentId = "") {
  const assignmentBySlot = _bulkBundleSlotAssignments(state, batchFilamentId);
  return [...(member?.roles || [])]
    .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
    .map((role) => assignmentBySlot[role.material_slot_id] || "");
}

function _bulkPreviewChips(filamentIds = []) {
  return (filamentIds || []).map((fid) => {
    const fil = filamentMeta(fid);
    const hex = fil?.hex || "#cccccc";
    const title = fil ? `${fil.manufacturer || ""} ${fil.color_name || fil.display_name || fid}`.trim() : "Unselected filament";
    return `<span class="sample-batch-preview-chip${fil ? "" : " is-missing"}" style="background:${_escAttr(hex)}" title="${_escAttr(title)}"></span>`;
  }).join("");
}

function _bulkColoredGeometryStripMiniTable(step, roleAssignments = [], { slotIdByRoleIndex = new Map() } = {}) {
  const variableSlots = [...(step?.swatch_slots || [])]
    .sort((a, b) => Number(a.swatch_index || 0) - Number(b.swatch_index || 0));
  if (!variableSlots.length) {
    return `<div class="strip-diagram-contract-error">Missing geometry swatch slots</div>`;
  }
  const swatchCount = variableSlots.length || Number(step?.swatch_count || step?.layout_columns || 8);
  const filamentByRole = new Map((roleAssignments || []).map((assignment) => [
    Number(assignment.role_index || 0),
    assignment.filament_id || "",
  ]));
  const roles = [...(step?.roles || [])].sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
  if (!roles.length) {
    return `<div class="strip-diagram-contract-error">Missing geometry role data</div>`;
  }
  const rows = roles.map((role) => {
    const roleIndex = Number(role.role_index || 0);
    const slotId = slotIdByRoleIndex instanceof Map
      ? (slotIdByRoleIndex.get(roleIndex) || "")
      : (slotIdByRoleIndex?.[roleIndex] || "");
    const slotAttr = slotId ? ` data-bulk-preview-slot="${_escAttr(slotId)}"` : "";
    const filamentId = filamentByRole.get(Number(role.role_index || 0)) || "";
    const fil = filamentMeta(filamentId);
    const hex = fil?.hex || (role.role_kind === "variable" ? "#d7d7d3" : "#ececea");
    const color = textColor(hex);
    if (role.role_kind === "variable") {
      const cells = variableSlots.map((slot) => (
        `<td${slotAttr} style="background:${_escAttr(hex)};color:${_escAttr(color)}">${Number(slot.variable_thickness_mm || 0).toFixed(2)}</td>`
      )).join("");
      return `<tr class="bulk-preview-role-row"${slotAttr}>${cells}</tr>`;
    }
    const value = Number(role.fixed_thickness_mm || 0).toFixed(2);
    return `<tr class="bulk-preview-role-row"${slotAttr}><td${slotAttr} colspan="${swatchCount}" style="background:${_escAttr(hex)};color:${_escAttr(color)}">${value}mm</td></tr>`;
  });
  return `<table class="mini-strip-table bulk-sample-diagram-table">${rows.join("")}</table>`;
}

function _bulkBundleMemberRoleAssignments(member, slotAssignments = {}) {
  return [...(member?.roles || [])].map((role) => ({
    role_index: Number(role.role_index || 0),
    filament_id: slotAssignments[role.material_slot_id] || "",
  }));
}

function _bulkGeometrySlotIdByRoleIndex(state) {
  return new Map((state.slots || []).map((slot) => [Number(slot.role_index || 0), slot.slot_id]));
}

function _bulkBundleSlotIdByRoleIndex(member) {
  return new Map((member?.roles || []).map((role) => [Number(role.role_index || 0), role.material_slot_id || ""]));
}

function _bulkRoleAssignmentListHtml(step, roleAssignments = []) {
  const filamentByRole = new Map((roleAssignments || []).map((assignment) => [
    Number(assignment.role_index || 0),
    assignment.filament_id || "",
  ]));
  const rows = [...(step?.roles || [])]
    .sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0))
    .map((role) => {
      const roleIndex = Number(role.role_index || 0);
      const fid = filamentByRole.get(roleIndex) || "";
      const fil = filamentMeta(fid);
      const label = compactLayerRoleToken(role.role_label, roleIndex, `LR_${String(roleIndex).padStart(2, "0")}`);
      const name = fil ? sampleFilamentDisplayName(fid) : "Unassigned";
      const chip = fil ? `<span class="color-chip" style="background:${_escAttr(fil.hex || "#cccccc")}"></span>` : `<span class="color-chip is-missing"></span>`;
      return `
        <div class="sample-set-role-assignment${fil ? "" : " is-missing"}">
          <span class="mono">${_escHtml(label)}</span>
          <span>${chip}${_escHtml(name)}</span>
        </div>
      `;
    }).join("");
  return `<div class="sample-set-role-list">${rows}</div>`;
}

function _bulkGeometryCount(state) {
  if (state.sourceKind === "bundle") return (state.source?.members || []).length;
  if (state.sourceKind === "geometry" && state.source) return 1;
  return 0;
}

function _bulkCreateCount(state) {
  const batchCount = state.batchSlotId ? state.batchFilamentIds.length : 1;
  return _bulkGeometryCount(state) * Math.max(1, batchCount);
}

function _bulkValidation(state) {
  if (!state.sourceKind || !state.source) {
    return { valid: false, message: "Select a single geometry or mapped geometry bundle." };
  }
  if (!state.slots.length) {
    return { valid: false, message: "Selected source has no assignable role slots." };
  }
  if (state.sourceKind === "bundle" && !state.source.creation_eligible) {
    return { valid: false, message: "Selected bundle must be fully mapped before creating samples." };
  }
  if (state.sourceKind === "geometry" && !state.slots.some((slot) => slot.role_kind === "variable")) {
    return { valid: false, message: "Selected geometry must have a variable role to create calibration samples." };
  }
  if (state.batchSlotId) {
    if (state.batchFilamentIds.length < 2) {
      return { valid: false, message: "Select at least two filaments, or switch the slot back to Single." };
    }
  }
  for (const slot of state.slots) {
    if (slot.slot_id === state.batchSlotId) continue;
    if (!state.slotAssignments[slot.slot_id]) {
      return { valid: false, message: `Select a filament for ${slot.label}.` };
    }
  }
  return { valid: true, message: "" };
}

function _bulkSourcePickerHtml(state) {
  if (!state.source) {
    return `<span class="filament-selector-placeholder">Select ${state.sourceKind === "bundle" ? "geometry bundle" : "single geometry"}</span>`;
  }
  if (state.sourceKind === "geometry") {
    return `
      <span class="filament-selector-field-name">${_escHtml(_geometryLabelForStep(state.source))}</span>
      <span class="filament-selector-field-meta">${_escHtml(_geometryMetaLineForStep(state.source))}</span>
    `;
  }
  const bundle = state.source;
  const roleCount = (bundle.material_slots || []).length;
  return `
    <span class="filament-selector-field-name">${_escHtml(bundle.name || bundle.alias || "Bundle")}</span>
    <span class="filament-selector-field-meta">${(bundle.step_ids || []).length} geometr${(bundle.step_ids || []).length === 1 ? "y" : "ies"} · ${roleCount} bundle role${roleCount === 1 ? "" : "s"}</span>
  `;
}

function _bulkSourcePreview(state) {
  if (state.sourceKind === "geometry" && state.source) {
    return buildGeometryStripMiniTable(state.source);
  }
  if (state.sourceKind === "bundle" && state.source) {
    const steps = (state.source.step_ids || [])
      .map((stepId) => stepRecordByRef(stepId))
      .filter(Boolean);
    return steps.length
      ? _buildBundleStepTable(steps)
      : `<div class="sample-batch-preview-empty small-copy">No current geometry records found for this bundle.</div>`;
  }
  return `<div class="sample-batch-preview-empty small-copy">Choose a source to preview its geometries.</div>`;
}

function openBulkBundleSelector(options = {}) {
  const selectedId = options.selectedBundleId || "";
  const bundles = _sortedBundles(options.bundles || []);
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay geometry-selector-overlay";
  overlay.innerHTML = `
    <div class="info-dialog geometry-selector-dialog bulk-bundle-selector-dialog" role="dialog" aria-modal="true" aria-labeledby="bulkBundleSelectorTitle">
      ${renderDialogHeader({
        title: "Select Geometry Bundle",
        titleId: "bulkBundleSelectorTitle",
        headerClass: "geometry-selector-header",
        closeButtonHtml: renderWindowCloseButton({
          className: "info-dialog-close",
          label: "Close selector",
          title: "Close selector",
          attributes: "data-bulk-bundle-close",
        }),
      })}
      <div class="geometry-selector-body">
        <section class="geometry-selector-panel geometry-selector-select-panel" aria-label="Select geometry bundles">
          <div class="geometry-selector-panel-head">
            <h4>Select Bundle</h4>
          </div>
          <div class="geometry-selector-panel-body bulk-bundle-selector-panel-body">
            <div class="geometry-selector-results-frame">
              <div class="geometry-selector-results" id="bulkBundleSelectorResults"></div>
            </div>
          </div>
        </section>
        <aside class="geometry-selector-panel geometry-selector-preview-panel" aria-label="Selected bundle preview">
          <div class="geometry-selector-panel-head">
            <h4>Bundle Preview</h4>
          </div>
          <div class="geometry-selector-preview" id="bulkBundleSelectorPreview"></div>
        </aside>
      </div>
      <div class="info-dialog-footer geometry-selector-footer">
        <button class="ghost-button small" type="button" data-bulk-bundle-cancel>Cancel</button>
        <button class="primary-button small" type="button" id="bulkBundleSelectorApply">Select Bundle</button>
      </div>
    </div>
  `;

  const results = overlay.querySelector("#bulkBundleSelectorResults");
  const preview = overlay.querySelector("#bulkBundleSelectorPreview");
  const applyButton = overlay.querySelector("#bulkBundleSelectorApply");
  let activeIndex = Math.max(0, bundles.findIndex((bundle) => (bundle?.geometry_bundle_id || bundle?.name || "") === selectedId));
  let tentativeKey = selectedId || "";

  function bundleKey(bundle) {
    return bundle?.geometry_bundle_id || bundle?.name || "";
  }

  function bundleByKey(key) {
    return bundles.find((bundle) => bundleKey(bundle) === key) || null;
  }

  function bundleMemberRefs(bundle) {
    const members = bundle?.members || [];
    if (members.length) {
      return members.map((member, index) => ({
        member,
        index,
        geometryId: member.geometry_id,
        step: stepRecordByRef(member.geometry_id),
      }));
    }
    return (bundle?.step_ids || []).map((stepId, index) => ({
      member: null,
      index,
      geometryId: stepId,
      step: stepRecordByRef(stepId),
    }));
  }

  function bundleName(bundle) {
    return bundle?.name || bundle?.alias || "Bundle";
  }

  function bundleGeometryCount(bundle) {
    return Math.max((bundle?.members || []).length, (bundle?.step_ids || []).length);
  }

  function bundleMeta(bundle) {
    const geometryCount = bundleGeometryCount(bundle);
    const slotCount = (bundle?.material_slots || []).length;
    return `${geometryCount} geometr${geometryCount === 1 ? "y" : "ies"} · ${slotCount} shared role${slotCount === 1 ? "" : "s"}`;
  }

  function bundleSlotSummaryHtml(bundle) {
    const slots = bundle?.material_slots || [];
    return slots.length
      ? slots.map((slot) => `
        <span class="bundle-slot-summary-chip" title="${_escAttr(slot.label || `Shared Filament ${slot.key}`)}">
          <span class="bundle-slot-summary-color" style="background:${_escAttr(_bundleSlotColor(slot.key || slot.position || 0))}"></span>
          <strong>${_escHtml(slot.key || "")}</strong>
        </span>
      `).join("")
      : `<span class="bundle-slot-summary-empty">No shared roles mapped</span>`;
  }

  function close() {
    overlay.remove();
  }

  function optionButtons() {
    return Array.from(results.querySelectorAll(".bulk-bundle-selector-option"));
  }

  function activateOption(index, { focus = false } = {}) {
    const buttons = optionButtons();
    if (buttons.length === 0) {
      activeIndex = 0;
      return;
    }
    activeIndex = Math.max(0, Math.min(index, buttons.length - 1));
    buttons.forEach((button, i) => {
      const active = i === activeIndex;
      button.classList.toggle("is-active", active);
      button.tabIndex = active ? 0 : -1;
      button.setAttribute("aria-current", active ? "true" : "false");
    });
    if (focus) {
      buttons[activeIndex].focus();
      buttons[activeIndex].scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function tentativeIsVisible() {
    return optionButtons().some((button) => button.dataset.bundleKey === tentativeKey);
  }

  function selectedBundle() {
    return tentativeIsVisible() ? bundleByKey(tentativeKey) : null;
  }

  function renderPreview() {
    const bundle = selectedBundle();
    if (!bundle) {
      preview.innerHTML = `
        <div class="geometry-selector-preview-empty small-copy">
          Select a bundle to inspect its member geometries.
        </div>
      `;
      applyButton.disabled = true;
      return;
    }
    const selectable = !!bundle.creation_eligible;
    const members = bundleMemberRefs(bundle);
    applyButton.disabled = !selectable;
    preview.innerHTML = `
      <div class="bulk-bundle-selector-preview-summary">
        <div class="bulk-bundle-selector-preview-topline">
          <div class="geometry-selector-preview-head">
            <strong>${_escHtml(bundleName(bundle))}</strong>
            <span>${_escHtml(bundleMeta(bundle))}</span>
          </div>
          ${_renderBundleMappingStatusPill(bundle)}
        </div>
        <div class="bundle-slot-summary">${bundleSlotSummaryHtml(bundle)}</div>
        ${selectable ? "" : `<div class="bulk-bundle-selector-preview-note small-copy">Bundle is not fully mapped.</div>`}
      </div>
      ${members.length ? `
        <div class="geometry-selector-preview-list bulk-bundle-selector-preview-list">
          ${members.map(({ member, step, geometryId, index }) => {
            const label = member?.geometry_alias || _geometryLabelForStep(step) || geometryId || "Geometry";
            return `
              <div class="geometry-selector-preview-card bulk-bundle-selector-preview-card">
                <div class="geometry-selector-preview-head">
                  <strong>${_escHtml(label)}</strong>
                </div>
                <div class="geometry-selector-preview-diagram bulk-bundle-selector-preview-diagram">
                  ${step ? _buildBundleMemberPreviewDiagram(bundle, member, step) : `<div class="strip-diagram-contract-error">Missing geometry record</div>`}
                </div>
              </div>
            `;
          }).join("")}
        </div>
      ` : `<div class="geometry-selector-preview-empty small-copy">This bundle has no geometries.</div>`}
    `;
  }

  function updateSelectionState() {
    optionButtons().forEach((button) => {
      const bundle = bundleByKey(button.dataset.bundleKey || "");
      button.classList.toggle("is-selected", !!bundle?.creation_eligible && button.dataset.bundleKey === tentativeKey);
    });
    renderPreview();
  }

  function render() {
    const filtered = bundles;
    if (filtered.length === 0) {
      activeIndex = 0;
      results.innerHTML = `<div class="geometry-selector-empty small-copy">No geometry bundles exist yet.</div>`;
      renderPreview();
      return;
    }
    results.innerHTML = filtered.map((bundle, index) => {
      const key = bundleKey(bundle);
      const selectable = !!bundle.creation_eligible;
      const selected = selectable && key === tentativeKey;
      return `
        <button type="button" class="geometry-selector-option bulk-bundle-selector-option${selected ? " is-selected" : ""}${selectable ? "" : " is-unavailable"}" data-bundle-key="${_escAttr(key)}" data-selectable="${selectable ? "true" : "false"}" data-option-index="${index}" tabindex="-1">
          <span class="geometry-selector-check" aria-hidden="true"></span>
          <div class="geometry-selector-option-main">
            <div class="geometry-selector-option-head">
              <span class="geometry-selector-option-name">${_escHtml(bundleName(bundle))}</span>
              <span class="geometry-selector-option-meta">${_escHtml(bundleMeta(bundle))}</span>
            </div>
            <div class="geometry-selector-option-bundles">
              ${_renderBundleMappingStatusPill(bundle)}
              ${bundleSlotSummaryHtml(bundle)}
            </div>
          </div>
        </button>
      `;
    }).join("");
    activateOption(activeIndex);
    updateSelectionState();
  }

  function chooseOption() {
    const bundle = selectedBundle();
    if (!bundle?.creation_eligible) return;
    try {
      if (typeof options.onApply === "function") options.onApply(bundle);
    } finally {
      close();
    }
  }

  overlay.addEventListener("pointerdown", (event) => {
    event.stopPropagation();
  });
  overlay.addEventListener("click", (event) => {
    event.stopPropagation();
    if (event.target === overlay) {
      close();
      return;
    }
    if (event.target.closest("[data-bulk-bundle-close], [data-bulk-bundle-cancel]")) {
      close();
      return;
    }
    const option = event.target.closest(".bulk-bundle-selector-option");
    if (!option) return;
    activeIndex = Number(option.dataset.optionIndex || activeIndex);
    tentativeKey = option.dataset.bundleKey || "";
    activateOption(activeIndex, { focus: true });
    updateSelectionState();
  });
  applyButton.addEventListener("click", chooseOption);
  overlay.addEventListener("keydown", (event) => {
    const buttons = optionButtons();
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      if (buttons.length === 0) return;
      event.preventDefault();
      activateOption((activeIndex + 1) % buttons.length, { focus: true });
      return;
    }
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      if (buttons.length === 0) return;
      event.preventDefault();
      activateOption((activeIndex - 1 + buttons.length) % buttons.length, { focus: true });
      return;
    }
    if (event.key === "Home") {
      if (buttons.length === 0) return;
      event.preventDefault();
      activateOption(0, { focus: true });
      return;
    }
    if (event.key === "End") {
      if (buttons.length === 0) return;
      event.preventDefault();
      activateOption(buttons.length - 1, { focus: true });
      return;
    }
    if (event.key === "Enter" && event.target?.classList?.contains("bulk-bundle-selector-option")) {
      event.preventDefault();
      tentativeKey = event.target.dataset.bundleKey || "";
      updateSelectionState();
    }
  });

  document.body.appendChild(overlay);
  render();
  if (optionButtons().length) activateOption(activeIndex, { focus: true });
}

function _renderBulkSampleCreateDrawer() {
  setDetailSidebarStackMode("form");
  setDrawerHeading("New Samples");
  drawerStatusPill.innerHTML = "";
  detailWindowArea.innerHTML = "";
  detailActionArea.innerHTML = `
    <button class="primary-button small drawer-header-action" id="bulkSampleCreateBtn">Create</button>
  `;

  detailSidebar.innerHTML = `
    <div class="sample-set-layout">
      <div class="sample-set-controls">
        ${buildDrawerFormModule("Select Geometry Source", `
          <div class="bulk-source-row">
            <div class="bulk-source-mode" role="group" aria-label="Geometry source type">
              <button type="button" class="bulk-source-mode-btn" id="bulkSourceGeometryMode" data-source-kind="geometry">Single Geometry</button>
              <button type="button" class="bulk-source-mode-btn" id="bulkSourceBundleMode" data-source-kind="bundle">Geometry Bundle</button>
            </div>
            <button type="button" class="filament-selector-field bulk-source-picker" id="bulkSourcePickerBtn"></button>
          </div>
        `, { density: "form" })}
        ${buildDrawerFormModule("Selected Geometry Preview", `
          <div id="bulkSourcePreview"></div>
        `, { density: "compact", bodyClass: "sample-preview-module-body" })}
        <div id="bulkSlotFields"></div>
        ${buildDrawerFormModule("Notes", `
          <div class="sample-create-field">
            <textarea id="bulkSampleNotes" class="sample-create-textarea" rows="2" placeholder="Optional notes for all samples..."></textarea>
          </div>
        `, { density: "large" })}
        ${buildDrawerFormModule("Sample Summary", `
          <div class="bulk-create-summary" id="bulkCreateSummary"></div>
          <div class="sample-batch-record-preview sample-bundle-record-preview" id="bulkRecordPreview"></div>
        `, { density: "table" })}
        <div class="filament-validation" id="bulkSampleValidation"></div>
      </div>
      <section class="sample-set-preview-panel" aria-label="Sample preview">
        <div class="sample-set-preview-cap">
          <span class="sidebar-label">Sample Preview</span>
        </div>
        <div class="sample-set-preview-body" id="bulkDiagramPreview"></div>
      </section>
    </div>
  `;

  _bindBulkSampleCreateControls();
}

function _bindBulkSampleCreateControls() {
  const sourceGeometryModeBtn = document.getElementById("bulkSourceGeometryMode");
  const sourceBundleModeBtn = document.getElementById("bulkSourceBundleMode");
  const sourcePickerBtn = document.getElementById("bulkSourcePickerBtn");
  const sourcePreviewEl = document.getElementById("bulkSourcePreview");
  const slotFieldsEl = document.getElementById("bulkSlotFields");
  const notesEl = document.getElementById("bulkSampleNotes");
  const createBtn = document.getElementById("bulkSampleCreateBtn");
  const validationEl = document.getElementById("bulkSampleValidation");
  const summaryEl = document.getElementById("bulkCreateSummary");
  const recordPreviewEl = document.getElementById("bulkRecordPreview");
  const diagramPreviewEl = document.getElementById("bulkDiagramPreview");

  const state = {
    sourceKind: "geometry",
    source: null,
    slots: [],
    slotAssignments: {},
    batchSlotId: null,
    batchFilamentIds: [],
  };

  function resetAssignments() {
    state.slotAssignments = {};
    state.batchSlotId = null;
    state.batchFilamentIds = [];
  }

  function setSourceKind(kind) {
    const nextKind = kind === "bundle" ? "bundle" : "geometry";
    if (state.sourceKind === nextKind) {
      render();
      return;
    }
    state.sourceKind = nextKind;
    state.source = null;
    state.slots = [];
    resetAssignments();
    render();
  }

  function selectGeometry(stepId) {
    const step = _sampleCreateSteps.find((candidate) => candidate.step_id === stepId) || stepRecordByRef(stepId);
    state.sourceKind = "geometry";
    state.source = step || null;
    state.slots = _bulkGeometrySlots(step);
    resetAssignments();
    render();
  }

  function selectBundle(bundle) {
    state.sourceKind = "bundle";
    state.source = bundle || null;
    state.slots = _bulkBundleSlots(bundle);
    resetAssignments();
    render();
  }

  function setSlotMode(slotId, mode) {
    if (mode === "batch") {
      delete state.slotAssignments[slotId];
      state.batchSlotId = slotId;
      state.batchFilamentIds = [];
    } else if (state.batchSlotId === slotId) {
      state.batchSlotId = null;
      state.batchFilamentIds = [];
    }
    render();
  }

  function renderSlotRow(slot) {
    const isBatch = state.batchSlotId === slot.slot_id;
    const otherBatchActive = !!state.batchSlotId && !isBatch;
    const singleValue = state.slotAssignments[slot.slot_id] || "";
    const batchCount = state.batchFilamentIds.length;
    const roleUseCount = state.sourceKind === "bundle" && state.source?.members
      ? (state.source.members || []).reduce((count, member) => count + (member.roles || []).filter((role) => role.material_slot_id === slot.slot_id).length, 0)
      : 0;
    const titleMain = state.sourceKind === "geometry"
      ? `${compactLayerRoleToken(slot.role_label, Number(slot.role_index || 0), `LR_${String(Number(slot.role_index || 0)).padStart(2, "0")}`)}${slot.role_kind === "fixed" ? ` Fixed${slot.fixed_thickness_mm != null ? ` - ${Number(slot.fixed_thickness_mm).toFixed(2)} mm` : ""}` : ""}`
      : slot.label;
    const titleMeta = state.sourceKind === "bundle"
      ? `${roleUseCount} role use${roleUseCount === 1 ? "" : "s"}`
      : "";
    const colorChip = state.sourceKind === "bundle"
      ? `<span class="bundle-slot-summary-color" style="background:${_escAttr(slot.color || "#cccccc")}"></span>`
      : "";
    const selectorHtml = isBatch
      ? `
        <button type="button" class="filament-selector-field filament-selector-field-multi bulk-slot-filament-btn" data-slot-id="${_escAttr(slot.slot_id)}">
          <span class="filament-selector-field-name">${batchCount ? `${batchCount} filament${batchCount === 1 ? "" : "s"} selected` : "Select filaments"}</span>
        </button>
        ${_bulkSelectedFilamentChips(state.batchFilamentIds)}
      `
      : `
        <button type="button" class="filament-selector-field bulk-slot-filament-btn" data-slot-id="${_escAttr(slot.slot_id)}">
          ${_bulkFilamentButtonHtml(singleValue)}
        </button>
      `;
    return buildDrawerFormModule(`
      <span class="bulk-slot-title">${colorChip}<span class="bulk-slot-title-name">${_escHtml(titleMain)}</span>${titleMeta ? `<span class="bulk-slot-title-meta"> - ${_escHtml(titleMeta)}</span>` : ""}</span>
    `, `
      <div class="bulk-slot-row" data-slot-id="${_escAttr(slot.slot_id)}">
        <div class="bulk-slot-head">
          <div class="bulk-slot-mode" role="group" aria-label="${_escAttr(`${slot.label} selection mode`)}">
            <button type="button" class="bulk-slot-mode-btn${!isBatch ? " is-active" : ""}" data-slot-id="${_escAttr(slot.slot_id)}" data-mode="single">Single</button>
            <button type="button" class="bulk-slot-mode-btn${isBatch ? " is-active" : ""}" data-slot-id="${_escAttr(slot.slot_id)}" data-mode="batch" ${otherBatchActive ? "disabled" : ""}>Multi</button>
          </div>
        </div>
        ${selectorHtml}
      </div>
    `, { density: "form", classes: isBatch ? "bulk-slot-module is-batch" : "bulk-slot-module" });
  }

  function renderSlots() {
    if (!slotFieldsEl) return;
    if (!state.source) {
      slotFieldsEl.innerHTML = "";
      return;
    }
    slotFieldsEl.innerHTML = state.slots.length
      ? state.slots.map(renderSlotRow).join("")
      : buildDrawerFormModule("Role Slots", `<div class="sample-batch-preview-empty small-copy">Selected source has no assignable role slots.</div>`, { density: "table" });
  }

  function previewRows() {
    const validation = _bulkValidation(state);
    if (!state.source) {
      return `<div class="sample-batch-preview-empty small-copy">Select a source to preview records.</div>`;
    }
    if (!validation.valid) {
      return `<div class="sample-batch-preview-empty small-copy">${_escHtml(validation.message)}</div>`;
    }
    let offset = 0;
    if (state.sourceKind === "geometry") {
      const batchIds = state.batchSlotId ? state.batchFilamentIds : [""];
      return batchIds.map((batchId) => {
        const chips = _bulkPreviewChips(_bulkGeometryPreviewChipIds(state, batchId));
        const batchFil = batchId ? filamentMeta(batchId) : null;
        const batchLabel = batchFil ? (batchFil.color_name || batchFil.display_name || batchId) : _geometryLabelForStep(state.source);
        const row = `
          <div class="sample-batch-preview-row sample-bundle-preview-row">
            <span class="mono sample-batch-preview-name">${_escHtml(_sampleBatchPreviewName(_bulkCreateNextId, offset))}</span>
            <span class="mono sample-bundle-preview-step" title="${_escAttr(batchLabel)}">${_escHtml(state.batchSlotId ? batchLabel : "Geometry")}</span>
            <span class="sample-batch-preview-chips">${chips}</span>
          </div>
        `;
        offset += 1;
        return row;
      }).join("");
    }
    const members = state.source?.members || [];
    const batchIds = state.batchSlotId ? state.batchFilamentIds : [""];
    return batchIds.map((batchId) => {
      const batchFil = batchId ? filamentMeta(batchId) : null;
      const groupHeader = state.batchSlotId ? `
        <div class="bulk-preview-group-label">${_escHtml(batchFil?.color_name || batchFil?.display_name || batchId)}</div>
      ` : "";
      const rows = members.map((member, index) => {
        const step = stepRecordByRef(member.geometry_id);
        const chips = _bulkPreviewChips(_bulkBundlePreviewChipIds(state, member, batchId));
        const row = `
          <div class="sample-batch-preview-row sample-bundle-preview-row">
            <span class="mono sample-batch-preview-name">${_escHtml(_sampleBatchPreviewName(_bulkCreateNextId, offset))}</span>
            <span class="mono sample-bundle-preview-step" title="${_escAttr(member.geometry_alias || _geometryLabelForStep(step) || member.geometry_id)}">Geometry ${_escHtml(_bundleStepReferenceLabel(index))}</span>
            <span class="sample-batch-preview-chips">${chips}</span>
          </div>
        `;
        offset += 1;
        return row;
      }).join("");
      return `${groupHeader}${rows}`;
    }).join("");
  }

  function diagramPreviewCards() {
    if (!state.source) {
      return `<div class="sample-set-preview-empty small-copy">Select a source and filaments to preview the samples in this set.</div>`;
    }
    let offset = 0;
    if (state.sourceKind === "geometry") {
      const batchIds = state.batchSlotId ? (state.batchFilamentIds.length ? state.batchFilamentIds : [""]) : [""];
      const slotIdByRoleIndex = _bulkGeometrySlotIdByRoleIndex(state);
      return batchIds.map((batchId) => {
        const payload = _bulkGeometryRolePayload(state, batchId);
        const batchFil = batchId ? filamentMeta(batchId) : null;
        const batchLabel = batchFil ? (batchFil.color_name || batchFil.display_name || batchId) : "";
        const html = `
          <article class="sample-set-preview-card">
            <div class="sample-set-preview-card-head">
              <strong class="mono">${_escHtml(_sampleBatchPreviewName(_bulkCreateNextId, offset))}</strong>
              <span title="${_escAttr(_geometryLabelForStep(state.source))}">${_escHtml(_geometryLabelForStep(state.source))}</span>
              ${batchLabel ? `<small>${_escHtml(batchLabel)}</small>` : ""}
            </div>
            <div class="sample-set-preview-card-body">
              <div class="sample-set-preview-diagram">${_bulkColoredGeometryStripMiniTable(state.source, payload.roleAssignments, { slotIdByRoleIndex })}</div>
              ${_bulkRoleAssignmentListHtml(state.source, payload.roleAssignments)}
            </div>
          </article>
        `;
        offset += 1;
        return html;
      }).join("");
    }
    const members = state.source?.members || [];
    const batchIds = state.batchSlotId ? (state.batchFilamentIds.length ? state.batchFilamentIds : [""]) : [""];
    return batchIds.map((batchId) => {
      const batchFil = batchId ? filamentMeta(batchId) : null;
      const groupHeader = state.batchSlotId ? `
        <div class="sample-set-preview-group">${_escHtml(batchFil?.color_name || batchFil?.display_name || batchId)}</div>
      ` : "";
      const slotAssignments = _bulkBundleSlotAssignments(state, batchId);
      const cards = members.map((member, index) => {
        const step = stepRecordByRef(member.geometry_id);
        const label = member.geometry_alias || _geometryLabelForStep(step) || member.geometry_id;
        const roleAssignments = _bulkBundleMemberRoleAssignments(member, slotAssignments);
        const diagram = step
          ? _bulkColoredGeometryStripMiniTable(step, roleAssignments, {
              slotIdByRoleIndex: _bulkBundleSlotIdByRoleIndex(member),
            })
          : `<div class="strip-diagram-contract-error">Missing geometry record</div>`;
        const html = `
          <article class="sample-set-preview-card">
            <div class="sample-set-preview-card-head">
              <strong class="mono">${_escHtml(_sampleBatchPreviewName(_bulkCreateNextId, offset))}</strong>
              <span title="${_escAttr(label)}">${_escHtml(label)}</span>
              <small>Geometry ${_escHtml(_bundleStepReferenceLabel(index))}</small>
            </div>
            <div class="sample-set-preview-card-body">
              <div class="sample-set-preview-diagram">${diagram}</div>
              ${step ? _bulkRoleAssignmentListHtml(step, roleAssignments) : ""}
            </div>
          </article>
        `;
        offset += 1;
        return html;
      }).join("");
      return `${groupHeader}${cards}`;
    }).join("");
  }

  function renderPreview() {
    const validation = _bulkValidation(state);
    const count = validation.valid ? _bulkCreateCount(state) : 0;
    if (summaryEl) {
      summaryEl.innerHTML = `
        <span>${count} sample${count === 1 ? "" : "s"}</span>
        ${state.batchSlotId ? `<span>${state.batchFilamentIds.length} multi filament${state.batchFilamentIds.length === 1 ? "" : "s"}</span>` : ""}
      `;
    }
    if (recordPreviewEl) {
      recordPreviewEl.innerHTML = previewRows();
    }
    if (diagramPreviewEl) {
      diagramPreviewEl.innerHTML = diagramPreviewCards();
    }
  }

  function render() {
    if (notesEl) notesEl.value = notesEl.value || "";
    if (sourceGeometryModeBtn) sourceGeometryModeBtn.classList.toggle("is-active", state.sourceKind !== "bundle");
    if (sourceBundleModeBtn) sourceBundleModeBtn.classList.toggle("is-active", state.sourceKind === "bundle");
    if (sourcePickerBtn) sourcePickerBtn.innerHTML = _bulkSourcePickerHtml(state);
    if (sourcePreviewEl) sourcePreviewEl.innerHTML = _bulkSourcePreview(state);
    renderSlots();
    renderPreview();
    if (validationEl) {
      validationEl.textContent = "";
      validationEl.className = "filament-validation";
    }
    bindDynamicControls();
  }

  function bindDynamicControls() {
    const setPreviewSlotHighlight = (slotId = "") => {
      diagramPreviewEl?.querySelectorAll(".is-slot-highlight").forEach((node) => node.classList.remove("is-slot-highlight"));
      slotFieldsEl?.querySelectorAll(".bulk-slot-row.is-preview-target").forEach((node) => node.classList.remove("is-preview-target"));
      if (!slotId) return;
      let matchedRows = 0;
      diagramPreviewEl?.querySelectorAll(".bulk-preview-role-row[data-bulk-preview-slot]").forEach((node) => {
        if (node.getAttribute("data-bulk-preview-slot") === slotId) {
          node.classList.add("is-slot-highlight");
          matchedRows += 1;
        }
      });
      if (!matchedRows) {
        diagramPreviewEl?.querySelectorAll("td[data-bulk-preview-slot]").forEach((node) => {
          if (node.getAttribute("data-bulk-preview-slot") === slotId) {
            node.classList.add("is-slot-highlight");
          }
        });
      }
      slotFieldsEl?.querySelectorAll(".bulk-slot-row").forEach((row) => {
        if (row.dataset.slotId === slotId) {
          row.classList.add("is-preview-target");
        }
      });
    };

    slotFieldsEl?.querySelectorAll(".bulk-slot-mode-btn").forEach((button) => {
      button.addEventListener("click", () => {
        setSlotMode(button.dataset.slotId || "", button.dataset.mode || "single");
      });
    });
    slotFieldsEl?.querySelectorAll(".bulk-slot-row").forEach((row) => {
      const slotId = row.dataset.slotId || "";
      row.addEventListener("mouseenter", () => setPreviewSlotHighlight(slotId));
      row.addEventListener("mouseleave", () => setPreviewSlotHighlight(""));
      row.addEventListener("focusin", () => setPreviewSlotHighlight(slotId));
      row.addEventListener("focusout", (event) => {
        if (!row.contains(event.relatedTarget)) setPreviewSlotHighlight("");
      });
    });
    slotFieldsEl?.querySelectorAll(".bulk-slot-filament-btn").forEach((button) => {
      button.addEventListener("click", () => {
        const slotId = button.dataset.slotId || "";
        if (!slotId) return;
        const isBatch = state.batchSlotId === slotId;
        openFilamentSelector({
          mode: isBatch ? "multi" : "single",
          title: isBatch ? "Select Filaments" : "Select Filament",
          selectedIds: isBatch ? state.batchFilamentIds : (state.slotAssignments[slotId] ? [state.slotAssignments[slotId]] : []),
          onApply: (ids) => {
            if (isBatch) {
              state.batchFilamentIds = [...new Set((ids || []).filter(Boolean))];
            } else {
              state.slotAssignments[slotId] = ids[0] || "";
            }
            render();
          },
        });
      });
    });
  }

  sourceGeometryModeBtn?.addEventListener("click", () => setSourceKind("geometry"));
  sourceBundleModeBtn?.addEventListener("click", () => setSourceKind("bundle"));
  sourcePickerBtn?.addEventListener("click", () => {
    if (state.sourceKind === "bundle") {
      openBulkBundleSelector({
        bundles: _bulkCreateBundles,
        selectedBundleId: state.source?.geometry_bundle_id || "",
        onApply: (bundle) => selectBundle(bundle),
      });
      return;
    }
    openGeometrySelector({
      title: "Select Single Geometry",
      selectedStepId: state.source?.step_id || "",
      steps: _sampleCreateSteps,
      onApply: (stepId) => selectGeometry(stepId),
    });
  });

  createBtn?.addEventListener("click", async () => {
    const validation = _bulkValidation(state);
    if (!validation.valid) {
      validationEl.textContent = validation.message;
      validationEl.className = "filament-validation is-error";
      return;
    }
    const notes = (notesEl?.value || "").trim();
    createBtn.disabled = true;
    createBtn.textContent = "Creating...";
    validationEl.textContent = "";
    validationEl.className = "filament-validation";

    try {
      let result;
      if (state.sourceKind === "geometry") {
        if (state.batchSlotId) {
          const firstBatchId = state.batchFilamentIds[0] || "";
          const payload = _bulkGeometryRolePayload(state, firstBatchId);
          const batchSlot = state.slots.find((slot) => slot.slot_id === state.batchSlotId);
          const batchRole = batchSlot?.role_kind === "variable" ? "variable" : `role:${Number(batchSlot?.role_index || 0)}`;
          result = await createSampleBatch(
            state.source.step_id,
            batchRole,
            state.batchFilamentIds,
            payload.variableFilamentId,
            payload.fixedIds,
            payload.fixedThicknesses,
            payload.roleAssignments,
            notes
          );
        } else {
          const payload = _bulkGeometryRolePayload(state);
          const created = await createSample(
            state.source.step_id,
            payload.variableFilamentId,
            payload.fixedIds,
            notes,
            payload.fixedThicknesses,
            payload.roleAssignments
          );
          result = { created: [created], errors: [] };
        }
      } else {
        const materialAssignments = state.slots
          .filter((slot) => slot.slot_id !== state.batchSlotId)
          .map((slot) => ({
            material_slot_id: slot.slot_id,
            filament_id: state.slotAssignments[slot.slot_id],
          }));
        result = await createSamplesFromGeometryBundle({
          bundle_id: state.source.geometry_bundle_id,
          material_slot_assignments: materialAssignments,
          batch_material_slot_id: state.batchSlotId || undefined,
          batch_filament_ids: state.batchSlotId ? state.batchFilamentIds : [],
          notes,
        });
      }
      const count = (result.created || []).length;
      const errCount = (result.errors || []).length;
      let msg = `Created ${count} sample${count === 1 ? "" : "s"}`;
      if (errCount > 0) msg += `, ${errCount} error${errCount === 1 ? "" : "s"}`;
      showProfileToast(msg);
      _sampleDrawerMode = null;
      clearSelectionAndDrawer();
      await handleRefresh();
    } catch (err) {
      validationEl.textContent = err.message || "Failed to create samples";
      validationEl.className = "filament-validation is-error";
      createBtn.disabled = false;
      createBtn.textContent = "Create";
    }
  });

  render();
}

// ── End Unified Bulk Sample drawer ───────────────────────────────────────────

// ── Edit Sample drawer ──────────────────────────────────────────────────────

function buildSampleEditFixedRows(step, selectedByRole = new Map()) {
  const fixedLayers = step?.fixed_layers || [];
  if (fixedLayers.length === 0) return "";

  return fixedLayerDisplayEntries(fixedLayers).map(({ layer: fl, index }, displayIndex) => {
    const roleIndex = Number(fl.role_index || index + 1);
    const curFid = selectedByRole instanceof Map
      ? (selectedByRole.get(roleIndex) || "")
      : (selectedByRole[displayIndex] || "");

    return `
      <div class="drawer-subtitle-fixed">
        <button type="button" class="filament-selector-field sample-edit-fixed-select" data-fixed-index="${index}" data-role-index="${roleIndex}" data-filament-id="${_escAttr(curFid)}"></button>
        <span class="muted-line">${_escHtml(formatLayerRoleLabel({ role_index: roleIndex, role_label: fl.role_label, role_kind: "fixed" }))}</span>
      </div>
    `;
  }).join("");
}

async function openSampleEditDrawer(exp, options = {}) {
  _sampleDrawerMode = "edit";
  _filamentDrawerMode = null;
  _filamentDrawerData = null;
  const expanded = options.expanded != null ? !!options.expanded : _sampleInspectExpanded;
  setSampleInspectExpandedPreference(expanded);
  selectedRecord = { kind: "sample", id: exp.sample_id };
  recordDrawer.classList.remove("narrow-drawer");
  recordDrawer.classList.remove("sample-set-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  recordDrawer.classList.toggle("sample-expanded", expanded);
  try {
    _sampleCreateSteps = (await fetchSteps()) || [];
  } catch (err) {
    console.warn("[sample-edit] Failed to fetch steps:", err);
    if (_sampleCreateSteps.length === 0) {
      syncSampleStepCacheFromData();
    }
  }
  if (expanded && sampleHasMeasurementOutput(exp) && !exp._measurements) {
    await hydrateSampleMeasurements(exp.sample_id);
  }

  _renderSampleDrawerEdit(exp, { expanded });
  openRecordDrawer();
}

function _renderSampleDrawerEdit(exp, options = {}) {
  setDetailSidebarStackMode("form");
  const sampleId = exp.sample_id;
  const expanded = options.expanded != null ? !!options.expanded : _sampleInspectExpanded;
  setSampleInspectExpandedPreference(expanded);
  recordDrawer.classList.remove("narrow-drawer");
  recordDrawer.classList.remove("model-filament-drawer");
  recordDrawer.classList.toggle("sample-expanded", expanded);
  setDrawerHeading(sampleId);
  const status = sampleStatusMeta(exp);
  drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
  detailWindowArea.innerHTML = sampleWindowToggleButtonHtml(expanded);
  detailActionArea.innerHTML = `
    <button class="primary-button xs drawer-header-action" id="sampleSaveBtn">Save</button>
    <button class="ghost-button xs drawer-header-action" id="sampleEditDiscardBtn">Discard</button>
    <button class="delete-button xs drawer-header-action" id="sampleEditDeleteBtn">Delete</button>
  `;

  const currentStep = sampleStepId(exp);
  const currentVarFil = exp.variable_filament_id || "";

  const media = resolveSampleMedia(exp);

  function safeThumb(src, label) {
    if (!src) return placeholderThumb(label);
    return `<img class="drawer-thumb" src="${src}" alt="${label}" onload="this.style.display='block'" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex'"><div class="thumb-placeholder" style="display:none"><span>${label}</span></div>`;
  }

  const selectedStepObj = _sampleCreateSteps.find((s) => s.step_id === currentStep) || null;
  const existingFixedByRole = fixedFilamentIdsByRoleFromSample(exp);
  const fixedEditRows = buildSampleEditFixedRows(selectedStepObj, existingFixedByRole);
  const variableEditRole = variableRoleForStep(selectedStepObj);

  const filamentsModule = buildDrawerFormModule("Filaments", `
      <div class="drawer-subtitle">
        <button type="button" id="sampleEditVarFilSelect" class="filament-selector-field sample-edit-filament-selector" data-filament-id="${_escAttr(currentVarFil)}"></button>
        <span class="muted-line">${_escHtml(variableEditRole ? formatLayerRoleLabel(variableEditRole) : "Variable Layer")}</span>
      </div>
      <div id="sampleEditFixedRows">${fixedEditRows}</div>
    `, { density: "compact" });

  const imagesModule = buildDrawerFormModule("Images", `
      <div class="drawer-image-pair">
        <div class="drawer-image-card">
          <span class="sidebar-label" style="font-size:10px">Source</span>
          ${safeThumb(media.sourceImageFile ? previewUrl(media.sourceImageFile) : null, "No preview")}
          <span class="mono small-copy">${media.sourceName}</span>
        </div>
        <div class="drawer-image-card">
          <span class="sidebar-label" style="font-size:10px">Blank</span>
          ${safeThumb(media.blankObj?.blank_id ? blankPreviewUrl(media.blankObj.blank_id) : null, "No blank")}
          <span class="mono small-copy">${media.blankLabel}</span>
        </div>
      </div>
    `, {
      density: "compact",
      actionsHtml: `
        <button class="ghost-button xs step-copy-button sample-unassign-image-btn" type="button" data-unassign-kind="source" ${exp._assigned_image ? "" : "disabled"} title="Unassign source image">Unassign source</button>
        <button class="ghost-button xs step-copy-button sample-unassign-image-btn" type="button" data-unassign-kind="blank" ${exp._assigned_blank_id ? "" : "disabled"} title="Unassign blank image">Unassign blank</button>
      `,
    });

  const stripModule = buildDrawerFormModule("Strip", `
      <div id="sampleEditStepPreview"></div>
    `, { density: "compact" });

  const stepModule = buildDrawerFormModule("Sample Geometry", `
      <div class="sample-create-field">
        <button type="button" id="sampleEditGeometrySelectBtn" class="filament-selector-field geometry-selector-field"></button>
      </div>
    `, { density: "form" });

  const notesModule = buildDrawerFormModule("Notes", `
      <div class="sample-create-field">
        <textarea id="sampleEditNotes" class="sample-create-textarea sample-notes-input" rows="3" placeholder="Optional notes...">${_escHtml(exp.notes || "")}</textarea>
      </div>
    `, { density: "large", classes: "sample-notes-module" });

  const fitControlsModule = buildDrawerFormModule("Model Fit", `
      <label class="filament-option-row sample-fit-option-row">
        <input id="sampleEditFitExclude" type="checkbox" ${exp._fit_exclude ? "checked" : ""}>
        <span>Exclude this sample from model fits</span>
      </label>
      ${buildSampleSwatchFitHook(exp)}
    `, { density: "compact", classes: "sample-fit-controls-module" });

  const validationBlock = `<div class="filament-validation" id="sampleEditValidation" style="display:none"></div>`;

  if (expanded) {
    detailSidebar.innerHTML = buildSampleInspectFrameHtml(`
      <div class="sample-expanded-shell">
        <div class="sample-expanded-left compact-sidebar-stack">
          ${filamentsModule}
          ${imagesModule}
          ${stripModule}
          ${stepModule}
          ${notesModule}
          ${fitControlsModule}
          ${validationBlock}
        </div>
        <div class="sample-expanded-right">
          ${buildSampleExpandedAnalysisPane(exp)}
        </div>
      </div>
    `, true);
  } else {
    detailSidebar.innerHTML = `
      ${filamentsModule}
      ${imagesModule}
      ${stripModule}
      ${stepModule}
      ${notesModule}
      ${fitControlsModule}
      ${validationBlock}
    `;
  }

  _bindSampleEditControls(exp, { expanded });
}

function _bindSampleEditControls(exp, options = {}) {
  const sampleId = exp.sample_id;
  const geometryBtn = document.getElementById("sampleEditGeometrySelectBtn");
  const varFilSelect = document.getElementById("sampleEditVarFilSelect");
  const stepPreview = document.getElementById("sampleEditStepPreview");
  const fixedContainer = document.getElementById("sampleEditFixedRows");
  const notesEl = document.getElementById("sampleEditNotes");
  const fitExcludeEl = document.getElementById("sampleEditFitExclude");
  const saveBtn = document.getElementById("sampleSaveBtn");
  const discardBtn = document.getElementById("sampleEditDiscardBtn");
  const deleteBtn = document.getElementById("sampleEditDeleteBtn");
  const validation = document.getElementById("sampleEditValidation");
  const expanded = options.expanded != null ? !!options.expanded : _sampleInspectExpanded;

  let selectedStep = _sampleCreateSteps.find((s) => s.step_id === sampleStepId(exp)) || stepRecordByRef(sampleStepId(exp)) || null;
  renderGeometrySelectorField(geometryBtn, selectedStep?.step_id || "", "Select Geometry", selectedStep);
  renderFilamentSelectorField(varFilSelect, varFilSelect?.dataset?.filamentId || exp.variable_filament_id || "");

  function currentVariableFilamentId() {
    return varFilSelect?.dataset?.filamentId || "";
  }

  function bindFixedSelects() {
    detailSidebar.querySelectorAll(".sample-edit-fixed-select").forEach((button) => {
      renderFilamentSelectorField(button, button.dataset.filamentId || "");
      button.addEventListener("click", () => {
        openFilamentSelector({
          mode: "single",
          title: "Select Fixed Filament",
          selectedIds: button.dataset.filamentId ? [button.dataset.filamentId] : [],
          onApply: (ids) => {
            const nextId = ids[0] || "";
            renderFilamentSelectorField(button, nextId);
            updatePreview();
          },
        });
      });
    });
  }

  function bindVariableSelect() {
    varFilSelect?.addEventListener("click", () => {
      openFilamentSelector({
        mode: "single",
        title: "Select Variable Filament",
        selectedIds: currentVariableFilamentId() ? [currentVariableFilamentId()] : [],
        onApply: (ids) => {
          const nextId = ids[0] || "";
          renderFilamentSelectorField(varFilSelect, nextId);
          updatePreview();
        },
      });
    });
  }

  function updatePreview() {
    const varFilId = currentVariableFilamentId();
    const varFil = filamentMeta(varFilId);
    const varHex = varFil ? varFil.hex : "#cccccc";

    if (!selectedStep) {
      stepPreview.innerHTML = `<div class="sample-strip-tight">${buildStripMiniTable(exp)}</div>`;
      return;
    }

    const fixedByRole = collectFixedSelectValuesByRole(".sample-edit-fixed-select");
    const fixedIds = canonicalFixedFilamentIdsFromMap(selectedStep, fixedByRole);
    const fixedThicknesses = fixedLayerCanonicalThicknesses(selectedStep.fixed_layers || []);
    const roleAssignments = buildRoleAssignmentsForStep(selectedStep, varFilId, fixedByRole);

    const expLike = {
      variable_hex: varHex,
      variable_thicknesses_mm: selectedStep.variable_thicknesses_mm || [],
      fixed_thicknesses_mm: fixedThicknesses,
      fixed_filament_ids: fixedIds,
      roles: buildAssignedGeometryRolesFromAssignments(selectedStep, roleAssignments),
    };

    stepPreview.innerHTML = `<div class="sample-strip-tight">${buildStripMiniTable(expLike)}</div>`;
  }

  function selectStep(stepId) {
    const priorFixedByRole = collectFixedSelectValuesByRole(".sample-edit-fixed-select");
    selectedStep = _sampleCreateSteps.find((s) => s.step_id === stepId) || stepRecordByRef(stepId);
    renderGeometrySelectorField(geometryBtn, selectedStep?.step_id || "", "Select Geometry", selectedStep);

    if (selectedStep && (selectedStep.fixed_layers || []).length > 0) {
      const fallbackFixedByRole = priorFixedByRole.size > 0
        ? priorFixedByRole
        : fixedFilamentIdsByRoleFromSample(exp);
      fixedContainer.innerHTML = buildSampleEditFixedRows(selectedStep, fallbackFixedByRole);
      bindFixedSelects();
    } else {
      fixedContainer.innerHTML = "";
    }

    updatePreview();
  }

  geometryBtn.addEventListener("click", () => {
    openGeometrySelector({
      title: "Select Geometry",
      selectedStepId: selectedStep?.step_id || "",
      steps: _sampleCreateSteps,
      onApply: (stepId) => selectStep(stepId),
    });
  });

  bindVariableSelect();
  bindFixedSelects();
  bindSampleSwatchFitToggles(exp);
  updatePreview();

  const hasDerivedOutputs = Boolean(
    exp.processed ||
    ["processed", "failed", "flagged"].includes(exp._processing_status)
  );

  async function unassignSampleImage(kind, btn) {
    btn.disabled = true;
    const originalText = btn.textContent;
    btn.textContent = "Clearing...";
    try {
      if (kind === "blank") {
        await assignBlank(sampleId, null);
        showProfileToast(`Unassigned blank from ${sampleId}`);
      } else {
        await unassignImage(sampleId);
        showProfileToast(`Unassigned source from ${sampleId}`);
      }
      await handleRefresh();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = originalText;
      showProfileToast(err.message || "Failed to unassign image");
    }
  }

  detailSidebar.querySelectorAll(".sample-unassign-image-btn").forEach((btn) => {
    if (btn.disabled) return;
    const kind = btn.dataset.unassignKind;
    if (hasDerivedOutputs) {
      bindConfirmAction(btn, {
        armedText: "Clear data?",
        onConfirm: () => unassignSampleImage(kind, btn),
      });
    } else {
      btn.addEventListener("click", () => unassignSampleImage(kind, btn));
    }
  });

  document.getElementById("toggleSampleInspectBtn")?.addEventListener("click", () => {
    _renderSampleDrawerEdit(exp, { expanded: !expanded });
  });

  discardBtn.addEventListener("click", () => {
    _sampleDrawerMode = null;
    renderSidebarForSample(exp, { expanded: _sampleInspectExpanded });
  });

  if (deleteBtn) {
    bindConfirmAction(deleteBtn, {
      armedText: "Confirm Delete",
      onConfirm: async () => {
        try {
          await deleteSample(exp.sample_id);
          showProfileToast(`Deleted ${exp.sample_id}`);
          clearSelectionAndDrawer();
          await handleRefresh();
        } catch (err) {
          showProfileToast(`Delete failed: ${err.message}`);
        }
      },
    });
  }

  saveBtn.addEventListener("click", async () => {
    const newStepId = selectedStep?.step_id || "";
    const newVarFil = currentVariableFilamentId();

    if (!newStepId) {
      validation.textContent = "Please select a sample geometry.";
      validation.className = "filament-validation is-error";
      validation.style.display = "";
      return;
    }
    if (!newVarFil) {
      validation.textContent = "Please select a variable filament.";
      validation.className = "filament-validation is-error";
      validation.style.display = "";
      return;
    }

    const fixedByRole = collectFixedSelectValuesByRole(".sample-edit-fixed-select");
    const fixedIds = canonicalFixedFilamentIdsFromMap(selectedStep, fixedByRole);
    const fixedThicknesses = fixedLayerCanonicalThicknesses(selectedStep.fixed_layers || []);
    const roleAssignments = buildRoleAssignmentsForStep(selectedStep, newVarFil, fixedByRole);
    let missingFixed = false;
    fixedIds.forEach((fixedId) => {
      if (!fixedId) missingFixed = true;
    });

    if (missingFixed) {
      validation.textContent = "Please select all fixed layer filaments.";
      validation.className = "filament-validation is-error";
      validation.style.display = "";
      return;
    }

    const updates = {};
    const origStep = sampleStepId(exp);
    const origVar = exp.variable_filament_id || "";
    const originalStepObj = _sampleCreateSteps.find((s) => s.step_id === origStep) || stepRecordByRef(origStep);
    const origFixed = canonicalFixedFilamentIdsFromMap(originalStepObj, fixedFilamentIdsByRoleFromSample(exp));
    const origRoleAssignments = sampleRoleAssignmentTuple(exp);
    const newNotes = notesEl ? notesEl.value || "" : "";
    const origNotes = exp.notes || "";
    const newFitExclude = !!(fitExcludeEl && fitExcludeEl.checked);
    const origFitExclude = !!exp._fit_exclude;
    const fitExcludeChanged = !!fitExcludeEl && newFitExclude !== origFitExclude;

    if (newStepId !== origStep) updates.step_id = newStepId;
    if (newVarFil !== origVar) updates.variable_filament_id = newVarFil;
    if (JSON.stringify(fixedIds) !== JSON.stringify(origFixed)) updates.fixed_filament_ids = fixedIds;
    if (newStepId !== origStep) updates.fixed_thicknesses_mm = fixedThicknesses;
    if (
      newStepId !== origStep ||
      JSON.stringify(roleAssignments) !== JSON.stringify(origRoleAssignments)
    ) {
      updates.role_assignments = roleAssignments;
      updates.fixed_filament_ids = fixedIds;
      updates.fixed_thicknesses_mm = fixedThicknesses;
    }
    if (newNotes !== origNotes) updates.notes = newNotes;
    const updateKeys = Object.keys(updates);

    if (updateKeys.length === 0 && !fitExcludeChanged) {
      showProfileToast("No changes");
      _sampleDrawerMode = null;
      renderSidebarForSample(exp, { expanded: _sampleInspectExpanded });
      return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    validation.textContent = "";
    validation.className = "filament-validation";

    let savedSampleFields = false;
    try {
      if (updateKeys.length > 0) {
        await updateSample(sampleId, updates);
        savedSampleFields = true;
      }
      if (fitExcludeChanged) {
        const result = await updateSampleFitExclusion(sampleId, { fit_exclude: newFitExclude });
        applyFitControlMutationResponse(result);
      }
      showProfileToast(`Updated ${sampleId}`);
      _sampleDrawerMode = null;
      await handleRefresh();
    } catch (err) {
      validation.textContent = savedSampleFields
        ? `Sample fields were saved, but model-fit control failed: ${err.message || "Unknown error"}`
        : (err.message || "Failed to update sample");
      validation.className = "filament-validation is-error";
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
    }
  });
}

// ── End Edit Sample drawer ──────────────────────────────────────────────────

function bindStepMetaForm(stepId) {
  const aliasInput = document.getElementById("stepAliasInput");
  const aliasView = document.getElementById("stepAliasView");
  const chipsEl = document.getElementById("stepBundleChips");
  const addSelect = document.getElementById("stepBundleAddSelect");
  if (!aliasInput) return;

  let isEditing = false;

  function stepReturnButtonHtml() {
    return geometryDetailReturnSampleContext
      ? `<button class="secondary-button small drawer-header-action" id="stepReturnSampleBtn" type="button">Return to Sample</button>`
      : "";
  }

  function wireHeaderActions() {
    const returnButton = document.getElementById("stepReturnSampleBtn");
    const editButton = document.getElementById("editStepBtn");
    const saveButton = document.getElementById("saveStepBtn");
    const discardButton = document.getElementById("discardStepBtn");
    const deleteButton = document.getElementById("deleteStepBtn");

    returnButton?.addEventListener("click", () => {
      const context = geometryDetailReturnSampleContext;
      geometryDetailReturnSampleContext = null;
      returnToSampleInspectDrawer(context || {});
    });

    editButton?.addEventListener("click", () => {
      setEditMode(true);
    });

    saveButton?.addEventListener("click", () => {
      const val = aliasInput.value.trim();
      stepMeta(stepId).alias = val;
      if (aliasView) aliasView.textContent = val || "—";
      const displayName = val || stepId.replace(/_/g, "_\u200B");
      setDrawerHeading(displayName, { html: true });
      if (typeof updateStepMetadata === "function") {
        updateStepMetadata(stepId, val, stepMeta(stepId).bundle).catch(() => {});
      }
      setEditMode(false);
      selectedRecord = { kind: null, id: null };
      renderWorkspace();
      const row = tableContainer.querySelector(`.data-row[data-kind="step"][data-id="${CSS.escape(stepId)}"]`);
      if (row) row.click();
    });

    discardButton?.addEventListener("click", () => {
      const meta = stepMeta(stepId);
      aliasInput.value = meta.alias;
      setEditMode(false);
    });

    deleteButton?.addEventListener("click", () => {
      showStepDeleteDialog(stepId);
    });
  }

  // ── Bundle chip management ──
  let _cachedBundles = [];

  async function loadAndRenderBundles() {
    try {
      const result = await fetchBundles();
      _cachedBundles = result.bundles || result || [];
    } catch (_) {
      _cachedBundles = [];
    }
    renderBundleChips();
  }

  function bundlesContainingStep() {
    return _cachedBundles.filter((b) =>
      (b.step_ids || []).includes(stepId)
    );
  }

  function bundlesNotContainingStep() {
    return _cachedBundles.filter((b) =>
      !(b.step_ids || []).includes(stepId)
    );
  }

  function renderBundleChips() {
    const memberBundles = bundlesContainingStep();
    const availableBundles = bundlesNotContainingStep();

    // Render each associated bundle as a row with dropdown-width name + X
    if (chipsEl) {
      if (memberBundles.length === 0) {
        chipsEl.innerHTML = "";
      } else {
        chipsEl.innerHTML = memberBundles.map((b) => `
          <div class="step-bundle-add-row">
            <span class="bundle-chip-name">${b.name}</span>
            <button class="bundle-chip-remove" data-bundle="${b.name}" type="button" title="Remove bundle from this STEP file" aria-label="Remove bundle from this STEP file">
              <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                <path d="M2.5 2.5L9.5 9.5"></path>
                <path d="M9.5 2.5L2.5 9.5"></path>
              </svg>
            </button>
          </div>
        `).join("");

        chipsEl.querySelectorAll(".bundle-chip-remove").forEach((btn) => {
          btn.addEventListener("click", async () => {
            try {
              await removeStepFromBundle(btn.dataset.bundle, stepId);
              await loadAndRenderBundles();
            } catch (err) {
              showImportToast(err.message || "Failed to remove from bundle", "error");
            }
          });
        });
      }
    }

    // Populate dropdown with bundles this step is NOT in
    if (addSelect) {
      addSelect.innerHTML = `<option value="">---none---</option>` +
        availableBundles.map((b) => `<option value="${b.name}">${b.name}</option>`).join("");
    }
  }

  // Selecting a bundle from dropdown adds the step immediately
  if (addSelect) {
    addSelect.addEventListener("change", async () => {
      const bundleName = addSelect.value;
      if (!bundleName) return;
      try {
        await addStepToBundle(bundleName, stepId);
        await loadAndRenderBundles();
      } catch (err) {
        showImportToast(err.message || "Failed to add to bundle", "error");
      }
    });
  }

  // Initial load
  loadAndRenderBundles();

  // ── Edit mode toggle ──
  function setEditMode(editing) {
    isEditing = editing;
    document.querySelectorAll(".step-view-field").forEach((el) => el.style.display = editing ? "none" : "");
    document.querySelectorAll(".step-edit-field").forEach((el) => el.style.display = editing ? "" : "none");
    detailActionArea.innerHTML = editing
      ? `
        <button class="primary-button small drawer-header-action" id="saveStepBtn">Save</button>
        <button class="ghost-button small drawer-header-action" id="discardStepBtn">Discard</button>
        <button class="delete-button small drawer-header-action" id="deleteStepBtn">Delete</button>
      `
      : `
        ${stepReturnButtonHtml()}
        ${isStructuredGeometryBackend() ? `<button class="ghost-button small drawer-header-action" id="exportStepArtifactBtn">Export</button>` : ""}
        <button class="ghost-button small drawer-header-action" id="editStepBtn">Edit</button>
      `;
    wireHeaderActions();
    bindStepArtifactActions(stepId);
  }

  setEditMode(false);
}

function bindStepInlineActions() {
  tableContainer.querySelectorAll("[data-step-action]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const stepId = button.dataset.stepId;
      const action = button.dataset.stepAction;
      const usageCount = stepUsageCount(stepId);

      if (stepEditorState.stepId !== stepId) {
        resetStepEditorState(stepId);
      }

      if (action === "toggle-edit") {
        stepEditorState.isEditing = !stepEditorState.isEditing;
        stepEditorState.confirmDelete = false;
        stepEditorState.deleteMessage = "";
        stepEditorState.deleteMessageKind = "";
        const meta = stepMeta(stepId);
        stepEditorState.draftAlias = meta.alias || "";
        stepEditorState.draftBundle = meta.bundle || "";
        renderWorkspace();
        return;
      }

      if (action === "discard-edit") {
        resetStepEditorState(stepId);
        renderWorkspace();
        return;
      }

      if (action === "save-edit") {
        const aliasInput = document.getElementById("inlineStepAliasInput");
        const bundleInput = document.getElementById("inlineStepBundleInput");
        stepMeta(stepId).alias = (aliasInput?.value || "").trim();
        stepMeta(stepId).bundle = (bundleInput?.value || "").trim();
        if (typeof updateStepMetadata === "function") {
          updateStepMetadata(stepId, stepMeta(stepId).alias, stepMeta(stepId).bundle).catch((err) => {
            showImportToast(err.message || "Failed to save STEP metadata", "error");
          });
        }
        renderBundleOptions();
        resetStepEditorState(stepId);
        renderWorkspace();
        return;
      }

      if (action === "start-delete") {
        if (usageCount > 0) {
          stepEditorState.deleteMessage = `This is currently used by ${usageCount} sample(s). You must reassign those samples before deleting.`;
          stepEditorState.deleteMessageKind = "blocked";
          stepEditorState.confirmDelete = false;
        } else {
          stepEditorState.confirmDelete = true;
          stepEditorState.deleteMessage = "";
          stepEditorState.deleteMessageKind = "";
        }
        renderWorkspace();
        return;
      }

      if (action === "cancel-delete") {
        stepEditorState.confirmDelete = false;
        stepEditorState.deleteMessage = "";
        stepEditorState.deleteMessageKind = "";
        renderWorkspace();
        return;
      }

      if (action === "confirm-delete") {
        if (usageCount > 0) {
          stepEditorState.confirmDelete = false;
          stepEditorState.deleteMessage = `This is currently used by ${usageCount} sample(s). You must reassign those samples before deleting.`;
          stepEditorState.deleteMessageKind = "blocked";
          renderWorkspace();
          return;
        }
        stepMeta(stepId).deleted = true;
        selectedRecord = { kind: null, id: null };
        resetStepEditorState(null);
        renderWorkspace();
      }
    });
  });
}

// ── Import View ───────────────────────────────────────────────────────────────

function formatExifDate(isoStr) {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    const h = String(d.getHours()).padStart(2, "0");
    const m = String(d.getMinutes()).padStart(2, "0");
    return `${month}/${day} ${h}:${m}`;
  } catch { return isoStr.slice(0, 16); }
}

function formatFileSize(bytes) {
  if (bytes == null || isNaN(bytes)) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function buildImageAssignmentMap() {
  const map = {};
  for (const exp of data.samples) {
    const img = exp._assigned_image;
    if (img) {
      map[img] = exp.sample_id;
    }
  }
  importState.imageAssignments = map;
}

function showInfoDialog(message, title = "Warning") {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay";
  overlay.innerHTML = `
    <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="infoDialogTitle">
      ${renderDialogHeader({
        title,
        titleId: "infoDialogTitle",
        closeButtonHtml: renderWindowCloseButton({
          className: "info-dialog-close",
          attributes: "data-info-dialog-close",
        }),
      })}
      <div class="info-dialog-body">
        <p>${message}</p>
      </div>
      <div class="info-dialog-footer">
        <button class="primary-button small" id="infoDialogOk">OK</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const cleanup = () => overlay.remove();
  overlay.querySelector("#infoDialogOk").addEventListener("click", cleanup);
  overlay.querySelector("[data-info-dialog-close]").addEventListener("click", cleanup);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) cleanup(); });
}

function showImportConfirmDialog() {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay";
    overlay.innerHTML = `
      <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="importConfirmTitle">
        ${renderDialogHeader({
          title: "Import Images",
          titleId: "importConfirmTitle",
          closeButtonHtml: renderWindowCloseButton({ id: "importConfirmClose", className: "info-dialog-close" }),
        })}
        <div class="info-dialog-body">
          <p class="info-dialog-lede">By importing, images in the inbox will be moved into Prisma-managed storage. Are you sure you wish to continue?</p>
        </div>
        <div class="info-dialog-footer">
          <button class="primary-button small" id="importConfirmProceed">Import</button>
          <button class="ghost-button small" id="importConfirmCancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = (value) => {
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
      resolve(value);
    };
    const handleKeydown = (event) => {
      if (event.key === "Escape") cleanup(false);
    };
    document.addEventListener("keydown", handleKeydown);
    overlay.querySelector("#importConfirmProceed")?.addEventListener("click", () => cleanup(true));
    overlay.querySelector("#importConfirmCancel")?.addEventListener("click", () => cleanup(false));
    overlay.querySelector("#importConfirmClose")?.addEventListener("click", () => cleanup(false));
    overlay.addEventListener("click", (event) => { if (event.target === overlay) cleanup(false); });
  });
}

function findImageRecord(filename) {
  if (!filename || filename === "—") return null;
  return (data.images || []).find((img) => img.filename === filename || img.original_filename === filename)
    || (importState.images || []).find((img) => img.filename === filename || img.original_filename === filename)
    || null;
}

function imageCustodyBadgeHtml(filename, noun = "Image") {
  const availability = sourceAvailabilityInfo(findImageRecord(filename), noun);
  if (availability.available) return "";
  return `<span class="status-pill source-custody-pill ${_escAttr(importSourceStateClass(availability.state))}" title="${_escAttr(availability.message)}">${_escHtml(availability.label)}</span>`;
}

function showCsvAssignmentBlankRegistrationDialog(pendingBlanks = []) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay csv-assignment-confirm-overlay";
    overlay.innerHTML = `
      <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="csvBlankRegistrationTitle">
        ${renderDialogHeader({
          title: "Register Blank Images",
          titleId: "csvBlankRegistrationTitle",
          closeButtonHtml: renderWindowCloseButton({ id: "csvBlankRegistrationClose", className: "info-dialog-close" }),
        })}
        <div class="info-dialog-body">
          <p class="info-dialog-lede">The following images were used as a blank but are not registered:</p>
          <div class="csv-blank-registration-list">
            ${(pendingBlanks || []).map((blank) => `
              <div class="csv-blank-registration-row">
                <span title="${escapeHtml(blank.filename || "")}">${escapeHtml(blank.filename || blank.image_asset_id || "")}</span>
                <strong>${Number(blank.uses || 0)} use${Number(blank.uses || 0) === 1 ? "" : "s"}</strong>
              </div>
            `).join("")}
          </div>
          <p>Would you like to register these images as blanks and continue?</p>
        </div>
        <div class="info-dialog-footer">
          <button class="primary-button small" type="button" id="csvBlankRegistrationProceed">Register and Continue</button>
          <button class="ghost-button small" type="button" id="csvBlankRegistrationCancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = (value) => {
      overlay.remove();
      document.removeEventListener("keydown", handleKeydown);
      resolve(value);
    };
    const handleKeydown = (event) => {
      if (event.key === "Escape") cleanup(false);
    };
    document.addEventListener("keydown", handleKeydown);
    overlay.querySelector("#csvBlankRegistrationProceed")?.addEventListener("click", () => cleanup(true));
    overlay.querySelector("#csvBlankRegistrationCancel")?.addEventListener("click", () => cleanup(false));
    overlay.querySelector("#csvBlankRegistrationClose")?.addEventListener("click", () => cleanup(false));
    overlay.addEventListener("click", (event) => { if (event.target === overlay) cleanup(false); });
  });
}

function showCsvAssignmentImportDialog() {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay csv-assignment-overlay";
  const state = {
    file: null,
    preview: null,
    validating: false,
    committing: false,
    error: "",
  };

  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", handleKeydown);
  };
  const handleKeydown = (event) => {
    if (event.key === "Escape" && !state.validating && !state.committing) close();
  };

  function previewRows() {
    const preview = state.preview || {};
    return [
      ...(preview.valid_rows || []),
      ...(preview.error_rows || []),
    ].sort((a, b) => Number(a.row_number || 0) - Number(b.row_number || 0));
  }

  function rowHtml(row) {
    const status = row.valid
      ? `<span class="csv-assignment-status is-valid">${row.blank_registration_required ? "Ready + blank" : "Ready"}</span>`
      : `<span class="csv-assignment-status is-error">Blocked</span>`;
    const errors = (row.errors || []).length
      ? `<div class="csv-assignment-errors">${(row.errors || []).map((err) => `<div>${escapeHtml(err)}</div>`).join("")}</div>`
      : "";
    return `
      <tr class="${row.valid ? "is-valid" : "is-error"}">
        <td class="mono">${escapeHtml(String(row.row_number || ""))}</td>
        <td class="mono">${escapeHtml(row.sample_id || "")}</td>
        <td>${escapeHtml(row.sample_image || "")}</td>
        <td>${escapeHtml(row.blank_id || row.blank_image || "")}</td>
        <td class="mono">${escapeHtml(row.orientation || "")}</td>
        <td>${status}${errors}</td>
      </tr>
    `;
  }

  function render() {
    const preview = state.preview;
    const hasValid = Number(preview?.valid_count || 0) > 0;
    const hasErrors = Number(preview?.error_count || 0) > 0;
    const commitLabel = hasErrors ? "Import Valid Rows" : "Import Assignments";
    const selectedName = state.file?.name || "No CSV selected";
    const rows = previewRows();
    const warnings = preview?.warnings || [];
    const pendingBlanks = preview?.pending_blank_registrations || [];
    overlay.innerHTML = `
      <div class="info-dialog csv-assignment-dialog" role="dialog" aria-modal="true" aria-labeledby="csvAssignmentTitle">
        ${renderDialogHeader({
          title: "CSV Bulk Assignment",
          titleId: "csvAssignmentTitle",
          closeButtonHtml: renderWindowCloseButton({
            id: "csvAssignmentClose",
            className: "info-dialog-close",
            disabled: state.validating || state.committing,
          }),
        })}
        <div class="info-dialog-body csv-assignment-body">
          <div class="csv-assignment-instructions">
            <strong>Before you begin</strong>
            <ul>
              <li>Place every sample image and blank image referenced by the CSV in the Calibration Inbox folder.</li>
              <li>Click <strong>Import from Inbox</strong> before validating this CSV so the images are registered in Calibration.</li>
              <li>Use the exact filenames shown in the Inbox, and keep the required columns from the template: Sample ID, Sample Image, Blank Image, and Orientation.</li>
              <li>Validation does not change assignments. Review blocked rows first; only ready rows are committed.</li>
            </ul>
          </div>
          <div class="csv-assignment-controls">
            <a class="ghost-button small" href="${sampleAssignmentTemplateUrl()}" download="prisma_sample_assignment_template.csv">Download CSV Template</a>
            <label class="ghost-button small csv-file-picker">
              <input type="file" id="csvAssignmentFile" accept=".csv,text/csv">
              Choose CSV
            </label>
            <span class="csv-assignment-file" title="${escapeHtml(selectedName)}">${escapeHtml(selectedName)}</span>
            <button class="primary-button small" type="button" id="csvAssignmentValidate" ${!state.file || state.validating || state.committing ? "disabled" : ""}>
              ${state.validating ? "Validating..." : "Validate CSV"}
            </button>
          </div>
          ${state.error ? `<div class="csv-assignment-message is-error">${escapeHtml(state.error)}</div>` : ""}
          ${preview ? `
            <div class="csv-assignment-summary">
              <span><strong>${preview.total_rows || 0}</strong> rows</span>
              <span class="is-valid"><strong>${preview.valid_count || 0}</strong> ready</span>
              <span class="${hasErrors ? "is-error" : ""}"><strong>${preview.error_count || 0}</strong> blocked</span>
              ${pendingBlanks.length ? `<span><strong>${pendingBlanks.length}</strong> blank${pendingBlanks.length === 1 ? "" : "s"} to register</span>` : ""}
              ${warnings.length ? `<span><strong>${warnings.length}</strong> warning${warnings.length === 1 ? "" : "s"}</span>` : ""}
            </div>
            ${hasErrors && hasValid ? `<div class="csv-assignment-message is-warning">Blocked rows will be skipped. Only ready rows will be assigned.</div>` : ""}
            ${pendingBlanks.length ? `<div class="csv-assignment-message is-warning">Some ready rows use images that are not yet registered as blanks. You will be asked to register them before import.</div>` : ""}
            ${warnings.length ? `<div class="csv-assignment-warnings">${warnings.map((warning) => `<div>${escapeHtml(warning.message || String(warning))}</div>`).join("")}</div>` : ""}
            <div class="csv-assignment-table-wrap">
              <table class="csv-assignment-table">
                <thead>
                  <tr>
                    <th>Row</th>
                    <th>Sample</th>
                    <th>Sample Image</th>
                    <th>Blank</th>
                    <th>Ori</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map(rowHtml).join("")}
                </tbody>
              </table>
            </div>
          ` : `<p class="small-copy">Choose a CSV after all referenced images have been imported from the Calibration Inbox.</p>`}
        </div>
        <div class="info-dialog-footer csv-assignment-footer">
          <button class="ghost-button small" type="button" id="csvAssignmentCancel" ${state.validating || state.committing ? "disabled" : ""}>Cancel</button>
          <button class="primary-button small" type="button" id="csvAssignmentCommit" ${!hasValid || state.validating || state.committing ? "disabled" : ""}>
            ${state.committing ? "Importing..." : commitLabel}
          </button>
        </div>
      </div>
    `;
    bind();
  }

  function bind() {
    overlay.querySelector("#csvAssignmentClose")?.addEventListener("click", close);
    overlay.querySelector("#csvAssignmentCancel")?.addEventListener("click", close);
    overlay.querySelector("#csvAssignmentFile")?.addEventListener("change", (event) => {
      state.file = event.target.files?.[0] || null;
      state.preview = null;
      state.error = "";
      render();
    });
    overlay.querySelector("#csvAssignmentValidate")?.addEventListener("click", async () => {
      if (!state.file) return;
      state.validating = true;
      state.error = "";
      render();
      try {
        state.preview = await validateSampleAssignmentCsv(state.file);
      } catch (err) {
        state.preview = null;
        state.error = err.message || "CSV validation failed";
      } finally {
        state.validating = false;
        render();
      }
    });
    overlay.querySelector("#csvAssignmentCommit")?.addEventListener("click", async () => {
      const token = state.preview?.preview_token;
      if (!token) return;
      const pendingBlanks = state.preview?.pending_blank_registrations || [];
      let registerUnregisteredBlanks = false;
      if (pendingBlanks.length) {
        const confirmed = await showCsvAssignmentBlankRegistrationDialog(pendingBlanks);
        if (!confirmed) return;
        registerUnregisteredBlanks = true;
      }
      state.committing = true;
      state.error = "";
      render();
      try {
        const result = await commitSampleAssignmentCsv(token, { registerUnregisteredBlanks });
        const blankCount = Number(result.registered_blank_count || 0);
        const blankMsg = blankCount ? ` and registered ${blankCount} blank${blankCount === 1 ? "" : "s"}` : "";
        showImportToast(`Imported ${result.committed_count || 0} assignment${(result.committed_count || 0) === 1 ? "" : "s"}${blankMsg}`, "success");
        close();
        await handleRefresh();
      } catch (err) {
        state.error = err.message || "CSV assignment import failed";
        state.committing = false;
        render();
      }
    });
  }

  document.body.appendChild(overlay);
  document.addEventListener("keydown", handleKeydown);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && !state.validating && !state.committing) close();
  });
  render();
}

function formatBackupPackageType(packageType = "") {
  const key = String(packageType || "");
  if (key === "core_library") return "Essential Data Only";
  if (key === "working_state_with_raw") return "All Data and Artifacts + Raw Images";
  if (key === "working_state_no_raw") return "All Data and Artifacts, No Raw Images";
  if (key === "raw_image_archive") return "RAW Image Archive";
  if (key === "normal_backup") return "Legacy Backup";
  if (key === "emergency_core_library_backup") return "Emergency Essential Data Backup";
  if (key === "emergency_pre_restore_backup") return "Emergency Pre-Restore Backup";
  return key.replace(/_/g, " ") || "Backup";
}

function normalizeRestoreConfirmation(value = "") {
  return String(value || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function showBackupRestoreDialog() {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay backup-restore-overlay";
  const createFileSource = () => ({
    mode: "path",
    pathText: "",
    file: null,
  });
  const state = {
    packageType: "working_state",
    includeRawImages: true,
    restoreSource: createFileSource(),
    rawArchiveSource: createFileSource(),
    rawArchiveCleanupSource: createFileSource(),
    backupCompactResult: null,
    restoreCompactResult: null,
    rawArchiveCompactResult: null,
    rawArchiveCleanupCompactResult: null,
    rawArchiveRestoreCompactResult: null,
    error: "",
  };

  function cleanupRestorePreview(preview) {
    const token = preview?.restore_token;
    if (!token) return;
    deleteRestorePreview(token).catch((err) => {
      console.warn("Could not clean up restore preview", err);
    });
  }

  function cleanupRawArchivePreview(preview) {
    const token = preview?.archive_token;
    if (!token) return;
    deleteRawArchivePreview(token).catch((err) => {
      console.warn("Could not clean up RAW archive preview", err);
    });
  }

  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", handleKeydown);
  };
  const handleKeydown = (event) => {
    if (document.querySelector(".backup-workflow-overlay")) return;
    if (event.key === "Escape") close();
  };

  function warningHtml(warnings = []) {
    if (!warnings.length) return "";
    return `
      <div class="backup-restore-warnings">
        ${warnings.map((warning) => `<div>${escapeHtml(warning.message || String(warning))}</div>`).join("")}
      </div>
    `;
  }

  function backupSummaryHtml(result) {
    if (!result) return "";
    const manifest = result.manifest || {};
    const download = result.backup_id ? backupDownloadUrl(result.backup_id) : "";
    const packageNoun = manifest.package_type === "raw_image_archive" ? "Archive" : "Backup";
    return `
      <div class="backup-restore-result">
        <div class="backup-restore-row"><span>${packageNoun}</span><strong>${escapeHtml(result.filename || "")}</strong></div>
        <div class="backup-restore-row"><span>Type</span><strong>${escapeHtml(formatBackupPackageType(manifest.package_type || ""))}</strong></div>
        <div class="backup-restore-row"><span>Files</span><strong>${Number(manifest.file_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Size</span><strong>${formatFileSize(Number(manifest.size_bytes || 0))}</strong></div>
        <div class="backup-restore-path mono" title="${escapeHtml(result.path || "")}">${escapeHtml(result.path || "")}</div>
        ${download ? `<a class="ghost-button small backup-restore-download" href="${download}" download="${escapeHtml(result.filename || "prisma_backup.zip")}">Download</a>` : ""}
        ${warningHtml(manifest.warnings || [])}
      </div>
    `;
  }

  function operationProgressHtml(job, active, fallbackMessage) {
    if (!job || !active) return "";
    const progress = job.progress || {};
    const indeterminate = progress.indeterminate === true;
    const percent = Number(progress.percent ?? 0);
    const phase = job.message || progress.message || fallbackMessage;
    const currentPath = progress.current_path || "";
    const currentCount = Number(progress.current_count || 0);
    const totalCount = Number(progress.total_count || 0);
    const currentBytes = Number(progress.current_bytes || 0);
    const totalBytes = Number(progress.total_bytes || 0);
    return `
      <div class="backup-progress" role="status" aria-live="polite">
        <div class="backup-progress-topline">
          <strong>${escapeHtml(phase)}</strong>
          <span>${indeterminate ? "Working" : `${percent.toFixed(1)}%`}</span>
        </div>
        <div class="backup-progress-bar" aria-hidden="true">
          <div class="backup-progress-fill${indeterminate ? " is-indeterminate" : ""}"${indeterminate ? "" : ` style="width: ${Math.max(0, Math.min(100, percent)).toFixed(1)}%;"`}></div>
        </div>
        <div class="backup-progress-meta">
          ${!indeterminate && totalCount ? `<span>${currentCount} / ${totalCount} files</span>` : ""}
          ${!indeterminate && totalBytes ? `<span>${formatFileSize(currentBytes)} / ${formatFileSize(totalBytes)}</span>` : ""}
        </div>
        ${currentPath ? `<div class="backup-progress-path mono" title="${escapeHtml(currentPath)}">${escapeHtml(currentPath)}</div>` : ""}
      </div>
    `;
  }

  function backupErrorMessage(err) {
    const detail = err?.detail || err;
    if (detail && typeof detail === "object") {
      const lines = [detail.message || err.message || "Backup creation failed"];
      if (detail.preserved_temp_path) {
        lines.push(`Validated package preserved at: ${detail.preserved_temp_path}`);
      }
      if (detail.automatic_recovery) {
        lines.push("Prisma will automatically retry moving this package into the Backups folder.");
      }
      if (detail.intended_final_path) {
        lines.push(`Intended final path: ${detail.intended_final_path}`);
      }
      return lines.filter(Boolean).join("\n");
    }
    return err?.message || "Backup creation failed";
  }

  function compactResultHtml(result, fallbackFilename = "prisma_backup.zip") {
    if (!result?.path) return "";
    const download = result.backup_id ? backupDownloadUrl(result.backup_id) : "";
    return `
      <div class="backup-compact-result">
        <input class="backup-compact-path mono" type="text" readonly value="${escapeHtml(result.path || "")}" title="${escapeHtml(result.path || "")}" aria-label="Created package path">
        ${download ? `<a class="ghost-button small backup-compact-download" href="${download}" download="${escapeHtml(result.filename || fallbackFilename)}">Download</a>` : ""}
      </div>
    `;
  }

  function compactStatusHtml(result) {
    if (!result?.message) return "";
    return `<div class="backup-compact-status">${escapeHtml(result.message)}</div>`;
  }

  function sourceDisplayValue(source) {
    if (source.mode === "file" && source.file) return source.file.name || "";
    return source.pathText || "";
  }

  function hasFileSource(source) {
    return Boolean(source.mode === "file" && source.file) || Boolean(String(source.pathText || "").trim());
  }

  function sourceControlHtml(id, label, source, placeholder) {
    const value = sourceDisplayValue(source);
    const note = source.mode === "file" && source.file ? `Selected with file picker: ${source.file.name || ""}` : "";
    return `
      <div class="backup-file-source">
        <label class="sidebar-label" for="${id}Path">${escapeHtml(label)}</label>
        <div class="backup-file-source-row">
          <input class="backup-file-source-input" type="text" id="${id}Path" value="${escapeHtml(value)}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
          <label class="ghost-button small backup-file-picker">
            <input type="file" id="${id}File" accept=".zip,application/zip">
            Open File
          </label>
        </div>
        ${note ? `<span class="backup-file-source-note is-upload">${escapeHtml(note)}</span>` : ""}
      </div>
    `;
  }

  function updateLauncherActionStates() {
    const restoreBtn = overlay.querySelector("#backupRestoreLaunch");
    if (restoreBtn) restoreBtn.disabled = !hasFileSource(state.restoreSource);
    const rawRestoreBtn = overlay.querySelector("#rawArchiveRestoreLaunch");
    if (rawRestoreBtn) rawRestoreBtn.disabled = !hasFileSource(state.rawArchiveSource);
    const rawCleanupBtn = overlay.querySelector("#rawArchiveCleanupLaunch");
    if (rawCleanupBtn) rawCleanupBtn.disabled = !hasFileSource(state.rawArchiveCleanupSource);
  }

  function bindSourceControl(id, source) {
    const input = overlay.querySelector(`#${id}Path`);
    input?.addEventListener("input", (event) => {
      source.pathText = event.target.value || "";
      if (source.mode === "file" || source.file) {
        source.mode = "path";
        source.file = null;
        render();
        const nextInput = overlay.querySelector(`#${id}Path`);
        nextInput?.focus();
        if (nextInput) {
          const caret = nextInput.value.length;
          nextInput.setSelectionRange(caret, caret);
        }
        return;
      }
      source.mode = "path";
      updateLauncherActionStates();
    });
    overlay.querySelector(`#${id}File`)?.addEventListener("change", (event) => {
      const file = event.target.files?.[0] || null;
      source.file = file;
      source.mode = file ? "file" : "path";
      source.pathText = "";
      state.error = "";
      render();
    });
  }

  async function validateRestoreSource(source) {
    if (source.mode === "file" && source.file) {
      return validateRestoreBackup(source.file);
    }
    return validateRestoreBackupPath(String(source.pathText || "").trim());
  }

  async function validateRawArchiveSource(source) {
    if (source.mode === "file" && source.file) {
      return validateRawArchive(source.file);
    }
    return validateRawArchivePath(String(source.pathText || "").trim());
  }

  function createWorkflowHost(title, stateRef, bodyHtml, bindBody, onClose) {
    const workflowOverlay = document.createElement("div");
    workflowOverlay.className = "info-dialog-overlay backup-workflow-overlay";
    const workflow = {
      render() {
        const busy = Boolean(stateRef.busy);
        workflowOverlay.innerHTML = `
          <div class="info-dialog backup-workflow-dialog" role="dialog" aria-modal="true" aria-labeledby="backupWorkflowTitle">
            ${renderDialogHeader({
              title,
              titleId: "backupWorkflowTitle",
              closeButtonHtml: renderWindowCloseButton({
                id: "backupWorkflowClose",
                className: "info-dialog-close",
                disabled: busy,
              }),
            })}
            <div class="info-dialog-body backup-workflow-body">
              ${bodyHtml()}
            </div>
          </div>
        `;
        const closeWorkflow = () => {
          if (stateRef.busy) return;
          if (onClose) onClose();
          workflowOverlay.remove();
        };
        workflowOverlay.querySelector("#backupWorkflowClose")?.addEventListener("click", closeWorkflow);
        bindBody?.(workflow);
      },
      isConnected() {
        return workflowOverlay.isConnected;
      },
    };
    document.body.appendChild(workflowOverlay);
    workflow.render();
    return workflow;
  }

  function restoreSummaryHtml(preview) {
    if (!preview) {
      return "";
    }
    const summary = preview.summary || {};
    const restoreSupported = summary.restore_supported !== false && Boolean(preview.restore_token);
    const restoreImpact = summary.restore_impact || "";
    const safety = summary.safety_backup || {};
    const impactText = (() => {
      if (summary.restore_support_reason) return summary.restore_support_reason;
      if (restoreImpact === "replace_library") return "This restore will replace the current Prisma library, including source images.";
      if (restoreImpact === "replace_library_except_source_images") return "This restore will replace the current Prisma library state but preserve current source images.";
      if (restoreImpact === "replace_core_database") return "This restore will replace only the Prisma database/core semantic state.";
      if (restoreImpact === "raw_archive_import_only") return "This is a RAW image archive. It cannot be used for library restore.";
      return "Review the detected package type before restoring.";
    })();
    const safetyText = safety.required
      ? (safety.recent_available
          ? `Recent core backup found: ${safety.newest_core_created_at || ""}`
          : "No recent core backup was found. Prisma will create a safety backup before restore.")
      : "No pre-restore safety backup is required for this package.";
    return `
      <div class="backup-restore-result">
        <div class="backup-restore-row"><span>Package</span><strong>${escapeHtml(summary.source_filename || "")}</strong></div>
        <div class="backup-restore-row"><span>Type</span><strong>${escapeHtml(formatBackupPackageType(summary.package_type || ""))}</strong></div>
        <div class="backup-restore-row"><span>Raw images</span><strong>${summary.contains_raw_images ? "Included" : "Not included"}</strong></div>
        <div class="backup-restore-row"><span>Assets</span><strong>${Number(summary.asset_file_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>STEP/STL</span><strong>${Number(summary.step_export_file_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>SQLite</span><strong>${formatFileSize(Number(summary.sqlite_size_bytes || 0))}</strong></div>
        ${warningHtml(summary.warnings || [])}
      </div>
      <div class="backup-restore-message ${restoreSupported ? "is-warning" : "is-error"}">${escapeHtml(impactText)}</div>
      <div class="backup-restore-message is-warning">${escapeHtml(safetyText)}</div>
    `;
  }

  function restoreResultHtml(result) {
    if (!result) return "";
    const preserved = result.preserved || {};
    const audit = result.audit || {};
    const warnings = result.warnings || [];
    const currentRaw = Number(preserved.current_raw_file_count || 0);
    const referencedRaw = Number(preserved.referenced_raw_file_count || 0);
    const orphanRaw = Number(preserved.orphan_raw_file_count || 0);
    const missingRefs = Number(audit.missing_referenced_file_count || 0);
    const staleRefs = Number(audit.stale_referenced_file_count || 0);
    return `
      <div class="backup-restore-result">
        <div class="backup-restore-row"><span>Pre-restore backup</span><strong>${escapeHtml(result.pre_restore_backup_id || "")}</strong></div>
        <div class="backup-restore-path mono" title="${escapeHtml(result.pre_restore_backup_path || "")}">${escapeHtml(result.pre_restore_backup_path || "")}</div>
        ${currentRaw || referencedRaw || orphanRaw ? `
          <div class="backup-restore-row"><span>Preserved RAW files</span><strong>${currentRaw}</strong></div>
          <div class="backup-restore-row"><span>Referenced RAW files</span><strong>${referencedRaw}</strong></div>
          <div class="backup-restore-row"><span>Unreferenced RAW files</span><strong>${orphanRaw}</strong></div>
        ` : ""}
        ${missingRefs || staleRefs ? `
          <div class="backup-restore-row"><span>Missing referenced files</span><strong>${missingRefs}</strong></div>
          <div class="backup-restore-row"><span>Hash mismatches</span><strong>${staleRefs}</strong></div>
        ` : ""}
        ${warningHtml(warnings)}
      </div>
    `;
  }

  function rawArchiveSummaryHtml(preview, options = {}) {
    if (!preview) return "";
    const summary = preview.summary || {};
    const counts = summary.reconciliation?.counts || {};
    const warnings = summary.warnings || [];
    const isCleanup = options.mode === "cleanup";
    return `
      <div class="backup-restore-result">
        <div class="backup-restore-row"><span>Package</span><strong>${escapeHtml(summary.source_filename || "")}</strong></div>
        <div class="backup-restore-row"><span>Type</span><strong>${escapeHtml(formatBackupPackageType(summary.package_type || ""))}</strong></div>
        <div class="backup-restore-row"><span>Source images</span><strong>${Number(summary.source_image_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Source bytes</span><strong>${formatFileSize(Number(summary.source_image_bytes || 0))}</strong></div>
        ${isCleanup ? `
          <div class="backup-restore-row"><span>In active library</span><strong>${Number(counts.already_present || 0)}</strong></div>
        ` : `
          <div class="backup-restore-row"><span>Restorable missing</span><strong>${Number(counts.restorable_missing || 0)}</strong></div>
          <div class="backup-restore-row"><span>Already present</span><strong>${Number(counts.already_present || 0)}</strong></div>
        `}
        <div class="backup-restore-row"><span>Conflicts</span><strong>${Number(counts.present_conflict || 0) + Number(counts.archive_conflict || 0)}</strong></div>
        ${isCleanup ? `
          <div class="backup-restore-row"><span>Active not in archive</span><strong>${Number(counts.not_in_archive || 0)}</strong></div>
        ` : `
          <div class="backup-restore-row"><span>Archive-only</span><strong>${Number(counts.archive_only || 0)}</strong></div>
          <div class="backup-restore-row"><span>Current not in archive</span><strong>${Number(counts.not_in_archive || 0)}</strong></div>
        `}
        ${warningHtml(warnings)}
      </div>
    `;
  }

  function rawArchiveImportResultHtml(result) {
    if (!result) return "";
    const thumbnailSummary = result.thumbnail_regeneration || {};
    const thumbnailCandidateCount = Number(thumbnailSummary.candidate_count || 0);
    const missingVisualSamples = Array.isArray(thumbnailSummary.still_missing)
      ? thumbnailSummary.still_missing.length
      : 0;
    return `
      <div class="backup-restore-result">
        <div class="backup-restore-row"><span>Restored</span><strong>${Number(result.restored_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Restored bytes</span><strong>${formatFileSize(Number(result.restored_size_bytes || 0))}</strong></div>
        <div class="backup-restore-row"><span>Already present</span><strong>${Number(result.already_present_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Skipped</span><strong>${Number(result.skipped_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Conflicts</span><strong>${Number(result.conflict_count || 0)}</strong></div>
        ${thumbnailCandidateCount && missingVisualSamples ? `<div class="backup-restore-row"><span>Extraction visuals</span><strong>${missingVisualSamples} samples need Maintenance rebuild</strong></div>` : ""}
        ${warningHtml(result.warnings || [])}
      </div>
    `;
  }

  function rawArchiveReleaseResultHtml(result) {
    if (!result) return "";
    return `
      <div class="backup-restore-result">
        <div class="backup-restore-row"><span>Removed</span><strong>${Number(result.released_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Freed</span><strong>${formatFileSize(Number(result.released_size_bytes || 0))}</strong></div>
        <div class="backup-restore-row"><span>Skipped</span><strong>${Number(result.skipped_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Conflicts</span><strong>${Number(result.conflict_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Failures</span><strong>${Number(result.failure_count || 0)}</strong></div>
        ${warningHtml(result.warnings || [])}
      </div>
    `;
  }

  function render() {
    const isCoreBackup = state.packageType === "core_library";
    overlay.innerHTML = `
      <div class="info-dialog backup-restore-dialog" role="dialog" aria-modal="true" aria-labeledby="backupRestoreTitle">
        ${renderDialogHeader({
          title: "Backup / Restore",
          titleId: "backupRestoreTitle",
          closeButtonHtml: renderWindowCloseButton({ id: "backupRestoreClose", className: "info-dialog-close" }),
        })}
        <div class="info-dialog-body backup-restore-body">
          ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
          <section class="backup-restore-panel">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Create Backup</span>
            </div>
            <div class="backup-restore-panel-body">
              <p class="backup-restore-instruction">Select backup type</p>
              <div class="backup-type-grid" role="radiogroup" aria-label="Backup type">
                <div class="backup-type-card ${isCoreBackup ? "is-active" : "is-inactive"}" id="backupPackageCore" role="radio" aria-checked="${isCoreBackup ? "true" : "false"}" tabindex="0" data-backup-package="core_library">
                  <span class="maintenance-operation-radio" aria-hidden="true"></span>
                  <span class="maintenance-operation-copy">
                    <span class="maintenance-operation-title">Essential Data Only</span>
                    <span class="maintenance-operation-description">Small backup of the Prisma database and essential semantic state.</span>
                    <span class="maintenance-operation-meta">Does not include source images, model artifacts, or generated exports.</span>
                  </span>
                </div>
                <div class="backup-type-card ${!isCoreBackup ? "is-active" : "is-inactive"}" id="backupPackageWorking" role="radio" aria-checked="${!isCoreBackup ? "true" : "false"}" tabindex="0" data-backup-package="working_state">
                  <span class="maintenance-operation-radio" aria-hidden="true"></span>
                  <span class="maintenance-operation-copy">
                    <span class="maintenance-operation-title">All Data and Artifacts</span>
                    <span class="maintenance-operation-description">Preserves the operational Prisma library, including derived artifacts and STEP/STL exports.</span>
                    ${!isCoreBackup ? `
                      <label class="backup-raw-toggle">
                        <input type="checkbox" id="backupIncludeRawImages" ${state.includeRawImages ? "checked" : ""}>
                        <span>Include raw image files</span>
                      </label>
                      <span class="maintenance-operation-meta">Turning this off keeps SQLite, extracted data, generated artifacts, and geometry exports, but omits camera raw source files from the ZIP.</span>
                    ` : `<span class="maintenance-operation-meta">Source image files can be included after selecting this option.</span>`}
                  </span>
                </div>
              </div>
              ${compactResultHtml(state.backupCompactResult)}
              <div class="backup-restore-actions">
                <button class="primary-button small" type="button" id="backupCreateBtn">Create Backup</button>
              </div>
            </div>
          </section>
          <section class="backup-restore-panel">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Restore From Backup</span>
            </div>
            <div class="backup-restore-panel-body">
              <p class="backup-restore-instruction">Enter or select a Prisma backup ZIP. Restore will validate the package before any changes are made.</p>
              ${sourceControlHtml("backupRestoreSource", "Backup File", state.restoreSource, "Type or select the backup .zip file path")}
              ${compactStatusHtml(state.restoreCompactResult)}
              <div class="backup-restore-actions">
                <button class="primary-button small" type="button" id="backupRestoreLaunch" ${!hasFileSource(state.restoreSource) ? "disabled" : ""}>Restore Backup</button>
              </div>
            </div>
          </section>
          <section class="backup-restore-panel">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Archive RAW Images</span>
            </div>
            <div class="backup-restore-panel-body">
              <p class="backup-restore-instruction">Create a source image archive, then optionally remove archived images from the active library after the archive has been verified.</p>
              <div class="raw-archive-operation-grid">
                <div class="raw-archive-operation">
                  <div class="drawer-module-cap">
                    <span class="sidebar-label">Create Archive</span>
                  </div>
                  <div class="raw-archive-operation-body">
                    <p class="small-copy">Package current source images into a RAW image archive without deleting or moving active files.</p>
                    ${compactResultHtml(state.rawArchiveCompactResult, "prisma_raw_image_archive.zip")}
                    <div class="raw-archive-operation-actions">
                      <button class="primary-button small" type="button" id="rawArchiveCreateBtn">Create RAW Image Archive</button>
                    </div>
                  </div>
                </div>
                <div class="raw-archive-operation">
                  <div class="drawer-module-cap">
                    <span class="sidebar-label">Remove Archived Images From Active Library</span>
                  </div>
                  <div class="raw-archive-operation-body">
                    <p class="small-copy">After storing source images somewhere safe, remove matching source image files from the active Prisma library.</p>
                    ${sourceControlHtml("rawArchiveCleanupSource", "Archive or Backup File", state.rawArchiveCleanupSource, "Type or select the RAW archive or all-data backup .zip file path")}
                    ${compactStatusHtml(state.rawArchiveCleanupCompactResult)}
                    <div class="raw-archive-operation-actions">
                      <button class="delete-button small" type="button" id="rawArchiveCleanupLaunch" ${!hasFileSource(state.rawArchiveCleanupSource) ? "disabled" : ""}>Remove Archived Images From Active Library</button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </section>
          <section class="backup-restore-panel">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Restore Archived Images</span>
            </div>
            <div class="backup-restore-panel-body">
              <p class="backup-restore-instruction">Enter or select a RAW image archive ZIP or an all-data backup ZIP that includes RAW images. Prisma will restore only missing compatible source images.</p>
              ${sourceControlHtml("rawArchiveSource", "Archive or Backup File", state.rawArchiveSource, "Type or select the RAW archive or all-data backup .zip file path")}
              ${compactStatusHtml(state.rawArchiveRestoreCompactResult)}
              <div class="backup-restore-actions">
                <button class="primary-button small" type="button" id="rawArchiveRestoreLaunch" ${!hasFileSource(state.rawArchiveSource) ? "disabled" : ""}>Restore Archived Images</button>
              </div>
            </div>
          </section>
        </div>
      </div>
    `;
    bind();
  }

  async function pollBackupWorkflowJob(host, workflowState, jobId, fallbackMessage) {
    return pollJobUntilTerminal({
      jobId,
      fetchStatus: () => fetchBackupJobStatus(jobId),
      isTerminal: (job) => ["succeeded", "failed", "cancelled"].includes(String(job.status || "")),
      shouldContinue: () => host.isConnected(),
      intervalMs: 800,
      onStatus: (job) => {
        workflowState.job = job;
        host.render();
      },
      onTransientError: () => {
        workflowState.job = {
          ...(workflowState.job || {}),
          job_id: jobId,
          message: `Connection interrupted; retrying ${fallbackMessage || "operation"} status...`,
        };
        host.render();
      },
    });
  }

  function showCreateBackupWorkflow() {
    const workflowState = { busy: true, starting: true, job: null, result: null, error: "" };
    const workflow = createWorkflowHost(
      "Create Backup",
      workflowState,
      () => `
        ${workflowState.error ? `<div class="backup-restore-message is-error">${escapeHtml(workflowState.error)}</div>` : ""}
        ${workflowState.result ? `<div class="backup-restore-message is-success">Backup created.</div>` : ""}
        ${operationProgressHtml(workflowState.job, workflowState.busy, "Creating backup")}
        ${workflowState.starting && !workflowState.job ? `<div class="backup-restore-message">Starting backup...</div>` : ""}
        ${backupSummaryHtml(workflowState.result)}
      `,
      null
    );
    async function poll(jobId) {
      try {
        const job = await pollBackupWorkflowJob(workflow, workflowState, jobId, "backup");
        if (!job) return;
        if (job.status === "succeeded") {
          workflowState.result = job.result || null;
          workflowState.busy = false;
          state.backupCompactResult = workflowState.result;
          render();
          workflow.render();
          return;
        }
        workflowState.error = job.status === "cancelled"
          ? "Backup cancelled."
          : backupErrorMessage(job.error || job);
        workflowState.busy = false;
        workflow.render();
      } catch (err) {
        workflowState.error = backupErrorMessage(err);
        workflowState.busy = false;
        workflow.render();
      }
    }
    (async () => {
      try {
        const job = await createBackupJob({
          packageType: state.packageType,
          includeRawImages: state.packageType === "working_state" ? state.includeRawImages : false,
        });
        workflowState.starting = false;
        workflowState.job = job;
        workflow.render();
        const jobId = String(job?.job_id || "");
        if (!jobId) throw new Error("Backup did not return a job id.");
        void poll(jobId);
      } catch (err) {
        workflowState.error = backupErrorMessage(err);
        workflowState.busy = false;
        workflowState.starting = false;
        workflow.render();
      }
    })();
  }

  function showRawArchiveCreateWorkflow() {
    const workflowState = { busy: true, starting: true, job: null, result: null, error: "" };
    const workflow = createWorkflowHost(
      "Archive RAW Images",
      workflowState,
      () => `
        ${workflowState.error ? `<div class="backup-restore-message is-error">${escapeHtml(workflowState.error)}</div>` : ""}
        ${workflowState.result ? `<div class="backup-restore-message is-success">RAW image archive created.</div>` : ""}
        ${operationProgressHtml(workflowState.job, workflowState.busy, "Creating RAW archive")}
        ${workflowState.starting && !workflowState.job ? `<div class="backup-restore-message">Starting RAW image archive...</div>` : ""}
        ${backupSummaryHtml(workflowState.result)}
      `,
      null
    );
    async function poll(jobId) {
      try {
        const job = await pollBackupWorkflowJob(workflow, workflowState, jobId, "RAW archive");
        if (!job) return;
        if (job.status === "succeeded") {
          workflowState.result = job.result || null;
          workflowState.busy = false;
          state.rawArchiveCompactResult = workflowState.result;
          if (workflowState.result?.path) {
            state.rawArchiveCleanupSource = {
              mode: "path",
              pathText: workflowState.result.path,
              file: null,
            };
          }
          render();
          workflow.render();
          return;
        }
        workflowState.error = job.status === "cancelled"
          ? "RAW archive creation cancelled."
          : backupErrorMessage(job.error || job);
        workflowState.busy = false;
        workflow.render();
      } catch (err) {
        workflowState.error = backupErrorMessage(err);
        workflowState.busy = false;
        workflow.render();
      }
    }
    (async () => {
      try {
        const job = await createRawArchiveJob();
        workflowState.starting = false;
        workflowState.job = job;
        workflow.render();
        const jobId = String(job?.job_id || "");
        if (!jobId) throw new Error("RAW archive did not return a job id.");
        void poll(jobId);
      } catch (err) {
        workflowState.error = backupErrorMessage(err);
        workflowState.busy = false;
        workflowState.starting = false;
        workflow.render();
      }
    })();
  }

  function showRawArchiveCleanupWorkflow() {
    const source = { ...state.rawArchiveCleanupSource };
    const cleanupPhrase = "Remove archived images from active library";
    const workflowState = {
      busy: true,
      validating: true,
      removing: false,
      preview: null,
      confirmation: "",
      job: null,
      result: null,
      error: "",
    };
    const workflow = createWorkflowHost(
      "Remove Archived Images From Active Library",
      workflowState,
      () => {
        const counts = workflowState.preview?.summary?.reconciliation?.counts || {};
        const removalCandidateCount = Number(counts.already_present || 0);
        const canRemove = removalCandidateCount > 0
          && normalizeRestoreConfirmation(workflowState.confirmation) === normalizeRestoreConfirmation(cleanupPhrase)
          && !workflowState.busy;
        const noRemovalNeeded = workflowState.preview && removalCandidateCount <= 0;
        return `
          ${workflowState.validating ? `<div class="backup-restore-message">Validating RAW image archive...</div>` : ""}
          ${workflowState.error ? `<div class="backup-restore-message is-error">${escapeHtml(workflowState.error)}</div>` : ""}
          ${rawArchiveSummaryHtml(workflowState.preview, { mode: "cleanup" })}
          ${noRemovalNeeded ? `<div class="backup-restore-message is-success">No matching active-library source images are covered by this archive.</div>` : ""}
          ${workflowState.preview?.archive_token && !workflowState.result && removalCandidateCount > 0 ? `
            <div class="backup-remove-panel">
              <div class="backup-restore-message is-warning">
                This will delete matching local RAW/source image files from the active Prisma library.
                Continue only after this archive has been stored somewhere safe.
              </div>
              <label class="sample-create-field backup-restore-confirm">
                <span class="sidebar-label">Type confirmation phrase</span>
                <span class="backup-restore-confirm-phrase">${escapeHtml(cleanupPhrase)}</span>
                <input type="text" id="rawArchiveRemoveConfirm" value="${escapeHtml(workflowState.confirmation)}" autocomplete="off" spellcheck="false" ${workflowState.busy ? "disabled" : ""}>
              </label>
              <div class="backup-workflow-actions">
                <button class="delete-button small" type="button" id="rawArchiveWorkflowRemoveStart" ${!canRemove ? "disabled" : ""}>Remove Archived Images From Active Library</button>
              </div>
            </div>
          ` : ""}
          ${operationProgressHtml(workflowState.job, workflowState.removing, "Removing archived images")}
          ${workflowState.result ? `<div class="backup-restore-message is-success">Archived images removed from active library.</div>` : ""}
          ${rawArchiveReleaseResultHtml(workflowState.result)}
        `;
      },
      (host) => {
        const confirmationInput = document.getElementById("rawArchiveRemoveConfirm");
        confirmationInput?.addEventListener("input", (event) => {
          workflowState.confirmation = event.target.value || "";
          const button = document.getElementById("rawArchiveWorkflowRemoveStart");
          if (button) {
            button.disabled = normalizeRestoreConfirmation(workflowState.confirmation) !== normalizeRestoreConfirmation(cleanupPhrase);
          }
        });
        document.getElementById("rawArchiveWorkflowRemoveStart")?.addEventListener("click", async () => {
          const token = workflowState.preview?.archive_token;
          if (!token) return;
          workflowState.busy = true;
          workflowState.removing = true;
          workflowState.error = "";
          workflowState.result = null;
          workflowState.job = null;
          host.render();
          try {
            const job = await createRawArchiveReleaseJob(token, workflowState.confirmation);
            workflowState.job = job;
            host.render();
            const jobId = String(job?.job_id || "");
            if (!jobId) throw new Error("Archived image removal did not return a job id.");
            const nextJob = await pollBackupWorkflowJob(host, workflowState, jobId, "archived image removal");
            if (!nextJob) return;
            if (nextJob.status === "succeeded") {
              workflowState.result = nextJob.result || null;
              workflowState.preview = null;
              workflowState.busy = false;
              workflowState.removing = false;
              state.rawArchiveCleanupSource = createFileSource();
              state.rawArchiveCleanupCompactResult = { message: "Archived images removed from active library." };
              await handleRefresh({ reloadImportData: true });
              render();
              host.render();
              return;
            }
            workflowState.error = nextJob.status === "cancelled"
              ? "Archived image removal cancelled."
              : backupErrorMessage(nextJob.error || nextJob);
            workflowState.busy = false;
            workflowState.removing = false;
            host.render();
          } catch (err) {
            workflowState.error = err.message || "Archived image removal failed";
            workflowState.busy = false;
            workflowState.removing = false;
            host.render();
          }
        });
      },
      () => cleanupRawArchivePreview(workflowState.preview)
    );
    (async () => {
      try {
        workflowState.preview = await validateRawArchiveSource(source);
        workflowState.validating = false;
        workflowState.busy = false;
        workflow.render();
      } catch (err) {
        workflowState.error = err.message || "RAW archive validation failed";
        workflowState.validating = false;
        workflowState.busy = false;
        workflow.render();
      }
    })();
  }

  function showRestoreBackupWorkflow() {
    const source = { ...state.restoreSource };
    const workflowState = {
      busy: true,
      validating: true,
      restoring: false,
      preview: null,
      confirmation: "",
      job: null,
      result: null,
      error: "",
    };
    const workflow = createWorkflowHost(
      "Restore Backup",
      workflowState,
      () => {
        const summary = workflowState.preview?.summary || {};
        const requiredConfirmation = summary.required_confirmation || "";
        const canRestore = Boolean(workflowState.preview?.restore_token)
          && summary.restore_supported !== false
          && normalizeRestoreConfirmation(workflowState.confirmation) === normalizeRestoreConfirmation(requiredConfirmation)
          && !workflowState.busy;
        return `
          ${operationProgressHtml(
            { message: "Validating backup...", progress: { indeterminate: true } },
            workflowState.validating,
            "Validating backup...",
          )}
          ${workflowState.error ? `<div class="backup-restore-message is-error">${escapeHtml(workflowState.error)}</div>` : ""}
          ${restoreSummaryHtml(workflowState.preview)}
          ${workflowState.preview?.restore_token && summary.restore_supported !== false && !workflowState.result ? `
            <label class="sample-create-field backup-restore-confirm">
              <span class="sidebar-label">Type confirmation phrase</span>
              <span class="backup-restore-confirm-phrase">${escapeHtml(requiredConfirmation)}</span>
              <input type="text" id="backupWorkflowConfirm" value="${escapeHtml(workflowState.confirmation)}" autocomplete="off" spellcheck="false" ${workflowState.busy ? "disabled" : ""}>
            </label>
            <div class="backup-workflow-actions">
              <button class="primary-button small" type="button" id="backupWorkflowRestoreStart" ${!canRestore ? "disabled" : ""}>Restore Backup</button>
            </div>
          ` : ""}
          ${operationProgressHtml(workflowState.job, workflowState.restoring, "Restoring backup")}
          ${workflowState.result ? `<div class="backup-restore-message is-success">Restore complete. Data refreshed.</div>` : ""}
          ${restoreResultHtml(workflowState.result)}
        `;
      },
      (host) => {
        const input = document.getElementById("backupWorkflowConfirm");
        input?.addEventListener("input", (event) => {
          workflowState.confirmation = event.target.value || "";
          const expected = workflowState.preview?.summary?.required_confirmation || "";
          const button = document.getElementById("backupWorkflowRestoreStart");
          if (button) {
            button.disabled = normalizeRestoreConfirmation(workflowState.confirmation) !== normalizeRestoreConfirmation(expected);
          }
        });
        document.getElementById("backupWorkflowRestoreStart")?.addEventListener("click", async () => {
          const token = workflowState.preview?.restore_token;
          const expected = workflowState.preview?.summary?.required_confirmation || "";
          if (!token || normalizeRestoreConfirmation(workflowState.confirmation) !== normalizeRestoreConfirmation(expected)) return;
          workflowState.busy = true;
          workflowState.restoring = true;
          workflowState.error = "";
          workflowState.result = null;
          workflowState.job = null;
          host.render();
          try {
            const job = await createRestoreJob(token, workflowState.confirmation);
            workflowState.job = job;
            host.render();
            const jobId = String(job?.job_id || "");
            if (!jobId) throw new Error("Restore did not return a job id.");
            const nextJob = await pollBackupWorkflowJob(host, workflowState, jobId, "restore");
            if (!nextJob) return;
            if (nextJob.status === "succeeded") {
              workflowState.result = nextJob.result || null;
              workflowState.preview = null;
              workflowState.busy = false;
              workflowState.restoring = false;
              state.restoreSource = createFileSource();
              state.restoreCompactResult = { message: "Restore complete. Data refreshed." };
              await handleRefresh({ reloadImportData: true });
              render();
              host.render();
              return;
            }
            workflowState.error = nextJob.status === "cancelled"
              ? "Restore cancelled."
              : backupErrorMessage(nextJob.error || nextJob);
            workflowState.busy = false;
            workflowState.restoring = false;
            host.render();
          } catch (err) {
            workflowState.error = err.message || "Restore failed";
            workflowState.busy = false;
            workflowState.restoring = false;
            host.render();
          }
        });
      },
      () => cleanupRestorePreview(workflowState.preview)
    );
    (async () => {
      try {
        workflowState.preview = await validateRestoreSource(source);
        workflowState.validating = false;
        workflowState.busy = false;
        workflow.render();
      } catch (err) {
        workflowState.error = err.message || "Backup validation failed";
        workflowState.validating = false;
        workflowState.busy = false;
        workflow.render();
      }
    })();
  }

  function showRawArchiveRestoreWorkflow() {
    const source = { ...state.rawArchiveSource };
    const workflowState = {
      busy: true,
      validating: true,
      importing: false,
      preview: null,
      job: null,
      result: null,
      error: "",
    };
    const workflow = createWorkflowHost(
      "Restore Archived Images",
      workflowState,
      () => {
        const counts = workflowState.preview?.summary?.reconciliation?.counts || {};
        const canRestore = Number(counts.restorable_missing || 0) > 0 && !workflowState.busy;
        const noRestoreNeeded = workflowState.preview && Number(counts.restorable_missing || 0) <= 0;
        return `
          ${workflowState.validating ? `<div class="backup-restore-message">Validating RAW image archive...</div>` : ""}
          ${workflowState.error ? `<div class="backup-restore-message is-error">${escapeHtml(workflowState.error)}</div>` : ""}
          ${rawArchiveSummaryHtml(workflowState.preview)}
          ${noRestoreNeeded ? `<div class="backup-restore-message is-success">No missing compatible source images need to be restored.</div>` : ""}
          ${workflowState.preview?.archive_token && !workflowState.result && !noRestoreNeeded ? `
            <div class="backup-workflow-actions">
              <button class="primary-button small" type="button" id="rawArchiveWorkflowRestoreStart" ${!canRestore ? "disabled" : ""}>Restore Archived Images</button>
            </div>
          ` : ""}
          ${operationProgressHtml(workflowState.job, workflowState.importing, "Restoring source images")}
          ${workflowState.result ? `<div class="backup-restore-message is-success">Archived image restore complete.</div>` : ""}
          ${rawArchiveImportResultHtml(workflowState.result)}
        `;
      },
      (host) => {
        document.getElementById("rawArchiveWorkflowRestoreStart")?.addEventListener("click", async () => {
          const token = workflowState.preview?.archive_token;
          if (!token) return;
          workflowState.busy = true;
          workflowState.importing = true;
          workflowState.error = "";
          workflowState.result = null;
          workflowState.job = null;
          host.render();
          try {
            const job = await createRawArchiveImportJob(token);
            workflowState.job = job;
            host.render();
            const jobId = String(job?.job_id || "");
            if (!jobId) throw new Error("Archived image restore did not return a job id.");
            const nextJob = await pollBackupWorkflowJob(host, workflowState, jobId, "archived image restore");
            if (!nextJob) return;
            if (nextJob.status === "succeeded") {
              workflowState.result = nextJob.result || null;
              workflowState.preview = null;
              workflowState.busy = false;
              workflowState.importing = false;
              state.rawArchiveSource = createFileSource();
              state.rawArchiveRestoreCompactResult = { message: "Archived image restore complete." };
              await handleRefresh({ reloadImportData: true });
              render();
              host.render();
              return;
            }
            workflowState.error = nextJob.status === "cancelled"
              ? "Archived image restore cancelled."
              : backupErrorMessage(nextJob.error || nextJob);
            workflowState.busy = false;
            workflowState.importing = false;
            host.render();
          } catch (err) {
            workflowState.error = err.message || "RAW archive import failed";
            workflowState.busy = false;
            workflowState.importing = false;
            host.render();
          }
        });
      },
      () => cleanupRawArchivePreview(workflowState.preview)
    );
    (async () => {
      try {
        workflowState.preview = await validateRawArchiveSource(source);
        workflowState.validating = false;
        workflowState.busy = false;
        workflow.render();
      } catch (err) {
        workflowState.error = err.message || "RAW archive validation failed";
        workflowState.validating = false;
        workflowState.busy = false;
        workflow.render();
      }
    })();
  }

  function bind() {
    overlay.querySelector("#backupRestoreClose")?.addEventListener("click", close);
    overlay.querySelectorAll("[data-backup-package]").forEach((card) => {
      const selectPackage = () => {
        const packageType = card.dataset.backupPackage || "";
        if (!packageType || state.packageType === packageType) return;
        state.packageType = packageType;
        state.error = "";
        render();
      };
      card.addEventListener("click", selectPackage);
      card.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        selectPackage();
      });
    });
    overlay.querySelector(".backup-raw-toggle")?.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    overlay.querySelector("#backupIncludeRawImages")?.addEventListener("click", (event) => {
      event.stopPropagation();
    });
    overlay.querySelector("#backupIncludeRawImages")?.addEventListener("change", (event) => {
      event.stopPropagation();
      state.includeRawImages = Boolean(event.target.checked);
      state.error = "";
      render();
    });
    bindSourceControl("backupRestoreSource", state.restoreSource);
    bindSourceControl("rawArchiveSource", state.rawArchiveSource);
    bindSourceControl("rawArchiveCleanupSource", state.rawArchiveCleanupSource);
    overlay.querySelector("#backupCreateBtn")?.addEventListener("click", () => {
      state.error = "";
      showCreateBackupWorkflow();
    });
    overlay.querySelector("#rawArchiveCreateBtn")?.addEventListener("click", () => {
      state.error = "";
      showRawArchiveCreateWorkflow();
    });
    overlay.querySelector("#rawArchiveCleanupLaunch")?.addEventListener("click", () => {
      if (!hasFileSource(state.rawArchiveCleanupSource)) return;
      state.error = "";
      showRawArchiveCleanupWorkflow();
    });
    overlay.querySelector("#backupRestoreLaunch")?.addEventListener("click", () => {
      if (!hasFileSource(state.restoreSource)) return;
      state.error = "";
      showRestoreBackupWorkflow();
    });
    overlay.querySelector("#rawArchiveRestoreLaunch")?.addEventListener("click", () => {
      if (!hasFileSource(state.rawArchiveSource)) return;
      state.error = "";
      showRawArchiveRestoreWorkflow();
    });
  }

  document.body.appendChild(overlay);
  document.addEventListener("keydown", handleKeydown);
  render();
}

function modelPublicationStatusMeta(component = {}) {
  const status = String(component.status || "unavailable").toLowerCase();
  if (status === "current" || status === "ready") {
    return { cls: "processed", label: status === "current" ? "Current" : "Ready" };
  }
  if (status === "stale") return { cls: "stale", label: "Stale" };
  if (status === "missing") return { cls: "failed", label: "Missing" };
  if (status === "invalid") return { cls: "failed", label: "Invalid" };
  return { cls: "failed", label: "Unavailable" };
}

function modelPublicationErrorMessage(error, fallback = "Model publication failed") {
  const detail = error?.detail;
  if (detail && typeof detail === "object" && detail.message) return String(detail.message);
  const message = String(error?.message || "").replace(/^API\s+\d+:\s*/, "").trim();
  return message || fallback;
}

function modelPublicationMetadataPayload(form = {}) {
  return {
    library_name: String(form.libraryName || "").trim(),
    library_version: String(form.libraryVersion || "").trim(),
    publisher: String(form.publisher || "").trim(),
    description: String(form.description || "").trim(),
    release_notes: String(form.releaseNotes || "").trim(),
  };
}

function showModelPublicationDialog() {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay model-publication-overlay";
  const state = {
    readiness: null,
    loading: true,
    working: "",
    error: "",
    refreshWarning: "",
    notice: "",
    result: null,
    resultAction: "",
    form: {
      libraryName: "",
      libraryVersion: "",
      publisher: "",
      description: "",
      releaseNotes: "",
    },
  };

  const isBusy = () => Boolean(state.working);
  const close = () => {
    if (isBusy()) return;
    overlay.remove();
    document.removeEventListener("keydown", handleKeydown);
  };
  const handleKeydown = (event) => {
    if (event.key === "Escape" && !isBusy()) close();
  };

  function readForm() {
    state.form.libraryName = overlay.querySelector("#modelPublicationName")?.value || "";
    state.form.libraryVersion = overlay.querySelector("#modelPublicationVersion")?.value || "";
    state.form.publisher = overlay.querySelector("#modelPublicationPublisher")?.value || "";
    state.form.description = overlay.querySelector("#modelPublicationDescription")?.value || "";
    state.form.releaseNotes = overlay.querySelector("#modelPublicationReleaseNotes")?.value || "";
  }

  function formComplete() {
    const payload = modelPublicationMetadataPayload(state.form);
    return Boolean(payload.library_name && payload.library_version && payload.publisher);
  }

  function readinessHtml() {
    if (state.loading && !state.readiness) {
      return `<div class="model-publication-loading" role="status">Checking current models...</div>`;
    }
    const report = state.readiness || {};
    const components = report.components || {};
    const orderedKeys = ["legacy_spline", "photo_stack_v2", "camera_transform", "filament_catalog"];
    const rows = orderedKeys.map((key) => {
      const component = components[key] || {
        label: key === "filament_catalog" ? "Filament catalog" : key.replace(/_/g, " "),
        status: "unavailable",
        reason: "Readiness could not be determined.",
      };
      const meta = modelPublicationStatusMeta(component);
      const count = key === "filament_catalog" && Number(component.filament_count || 0)
        ? `<span class="model-publication-component-meta">${Number(component.filament_count)} filaments</span>`
        : "";
      return `
        <div class="model-publication-component">
          <div class="model-publication-component-main">
            <strong>${escapeHtml(component.label || key.replace(/_/g, " "))}</strong>
            ${component.reason ? `<span>${escapeHtml(component.reason)}</span>` : ""}
          </div>
          <div class="model-publication-component-status">
            ${count}
            <span class="status-pill ${meta.cls}">${escapeHtml(meta.label)}</span>
          </div>
        </div>
      `;
    }).join("");
    const blockers = report.blocking_reasons || [];
    return `
      <div class="model-publication-readiness-summary ${report.ready ? "is-ready" : "is-blocked"}">
        <strong>${report.ready ? "Ready to publish" : "Publication blocked"}</strong>
        <span>${report.ready
          ? "All required current models and public filament data passed validation."
          : "Resolve the items below, then refresh readiness."}</span>
      </div>
      <div class="model-publication-components">${rows}</div>
      ${blockers.length ? `
        <div class="backup-restore-message is-warning model-publication-blockers">
          ${blockers.map((reason) => `<div>${escapeHtml(reason)}</div>`).join("")}
        </div>
      ` : ""}
    `;
  }

  function resultHtml() {
    const result = state.result || null;
    if (!result) return "";
    const isExport = state.resultAction === "export";
    return `
      <div class="backup-restore-message is-success">
        ${isExport
          ? "Library package created."
          : "Library published to Generator. It is installed but not active; select it from Generator's Models menu when you are ready to use it."}
      </div>
      <div class="backup-restore-result model-publication-result">
        <div class="backup-restore-row"><span>Library</span><strong>${escapeHtml(result.library_name || "")}</strong></div>
        <div class="backup-restore-row"><span>Version</span><strong>${escapeHtml(result.library_version || "")}</strong></div>
        <div class="backup-restore-row"><span>Filaments</span><strong>${Number(result.filament_count || 0)}</strong></div>
        <div class="backup-restore-row"><span>Payload</span><strong>${formatFileSize(Number(result.total_bytes || 0))}</strong></div>
        ${isExport
          ? `<div class="backup-restore-row"><span>Package</span><strong>${escapeHtml(result.package_filename || "")}</strong></div>
             <div class="backup-restore-path mono" title="${escapeHtml(result.package_path || "")}">${escapeHtml(result.package_path || "")}</div>`
          : `<div class="backup-restore-row"><span>Generator status</span><strong>Installed · Not active</strong></div>
             <div class="backup-restore-row"><span>Library ID</span><strong class="mono">${escapeHtml(result.library_id || "")}</strong></div>`}
      </div>
    `;
  }

  function progressHtml() {
    if (!state.working) return "";
    const label = state.working === "export"
      ? "Building and validating library package..."
      : "Building, validating, and installing library...";
    return `
      <div class="backup-progress model-publication-progress" role="status" aria-live="polite">
        <div class="backup-progress-topline"><strong>${label}</strong><span>Working</span></div>
        <div class="backup-progress-bar" aria-hidden="true">
          <div class="backup-progress-fill model-publication-progress-fill"></div>
        </div>
        <div class="backup-progress-meta"><span>Keep this window open until publication finishes.</span></div>
      </div>
    `;
  }

  function render() {
    const ready = Boolean(state.readiness?.ready);
    const canPublish = ready && formComplete() && !state.loading && !isBusy();
    overlay.innerHTML = `
      <div class="info-dialog model-publication-dialog" role="dialog" aria-modal="true" aria-labelledby="modelPublicationTitle">
        ${renderDialogHeader({
          title: "Publish Models",
          titleId: "modelPublicationTitle",
          subtitle: "Create a static Generator library from Calibration's exact current models.",
          closeButtonHtml: renderWindowCloseButton({
            id: "modelPublicationClose",
            className: "info-dialog-close",
            disabled: isBusy(),
          }),
        })}
        <div class="info-dialog-body model-publication-body">
          ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
          ${state.refreshWarning ? `<div class="backup-restore-message is-warning">${escapeHtml(state.refreshWarning)}</div>` : ""}
          ${state.notice ? `<div class="backup-restore-message is-success">${escapeHtml(state.notice)}</div>` : ""}
          <div class="model-publication-layout">
            <section class="backup-restore-panel model-publication-panel">
              <div class="drawer-module-cap model-publication-panel-cap">
                <span class="sidebar-label">Publication Readiness</span>
                <button class="ghost-button small" type="button" id="modelPublicationRefresh" ${state.loading || isBusy() ? "disabled" : ""}>${state.loading ? "Checking..." : "Refresh"}</button>
              </div>
              <div class="backup-restore-panel-body">${readinessHtml()}</div>
            </section>
            <section class="backup-restore-panel model-publication-panel">
              <div class="drawer-module-cap">
                <span class="sidebar-label">Library Details</span>
              </div>
              <div class="backup-restore-panel-body model-publication-form">
                <div class="model-publication-required-grid">
                  <label class="model-publication-field">
                    <span>Library name <em>Required</em></span>
                    <input id="modelPublicationName" maxlength="120" value="${escapeHtml(state.form.libraryName)}" placeholder="My Prisma Model Library" ${isBusy() ? "disabled" : ""}>
                  </label>
                  <label class="model-publication-field">
                    <span>Version <em>Required</em></span>
                    <input id="modelPublicationVersion" maxlength="64" value="${escapeHtml(state.form.libraryVersion)}" placeholder="1.0" ${isBusy() ? "disabled" : ""}>
                  </label>
                </div>
                <label class="model-publication-field">
                  <span>Publisher or author <em>Required</em></span>
                  <input id="modelPublicationPublisher" maxlength="120" value="${escapeHtml(state.form.publisher)}" placeholder="Your name" ${isBusy() ? "disabled" : ""}>
                </label>
                <label class="model-publication-field">
                  <span>Description <small>Optional</small></span>
                  <textarea id="modelPublicationDescription" maxlength="2000" rows="3" placeholder="What this model library contains" ${isBusy() ? "disabled" : ""}>${escapeHtml(state.form.description)}</textarea>
                </label>
                <label class="model-publication-field">
                  <span>Release notes <small>Optional</small></span>
                  <textarea id="modelPublicationReleaseNotes" maxlength="8000" rows="4" placeholder="What changed in this version" ${isBusy() ? "disabled" : ""}>${escapeHtml(state.form.releaseNotes)}</textarea>
                </label>
                <p class="small-copy model-publication-form-note">Prisma assigns a new library identity and compatibility metadata automatically. Published copies do not change when Calibration is edited or refitted later.</p>
              </div>
            </section>
          </div>
          <section class="backup-restore-panel model-publication-panel model-publication-actions-panel">
            <div class="drawer-module-cap"><span class="sidebar-label">Publish</span></div>
            <div class="backup-restore-panel-body">
              <p class="small-copy model-publication-action-help">Publish to Generator installs a new inactive library in this Prisma folder. Export Library Package creates a ZIP for transfer or a GitHub Release.</p>
              ${progressHtml()}
              ${resultHtml()}
              <div class="model-publication-actions">
                <button class="ghost-button small" type="button" id="modelPublicationOpenFolder" ${isBusy() ? "disabled" : ""}>Open Published Models Folder</button>
                <div class="model-publication-primary-actions">
                  <button class="secondary-button small" type="button" id="modelPublicationExport" ${canPublish ? "" : "disabled"}>${state.working === "export" ? "Exporting..." : "Export Library Package"}</button>
                  <button class="primary-button small" type="button" id="modelPublicationInstall" ${canPublish ? "" : "disabled"}>${state.working === "install" ? "Publishing..." : "Publish to Generator"}</button>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>
    `;
    bind();
  }

  async function refreshReadiness({ initial = false } = {}) {
    readForm();
    state.loading = true;
    state.error = "";
    if (!initial) state.refreshWarning = "";
    render();
    try {
      state.readiness = await fetchModelPublicationReadiness();
    } catch (error) {
      state.readiness = null;
      state.error = modelPublicationErrorMessage(error, "Could not check publication readiness");
    } finally {
      state.loading = false;
      render();
    }
  }

  async function runPublication(action) {
    readForm();
    const payload = modelPublicationMetadataPayload(state.form);
    if (!payload.library_name || !payload.library_version || !payload.publisher) {
      state.error = "Enter a library name, version, and publisher before publishing.";
      render();
      return;
    }
    if (!state.readiness?.ready || isBusy()) return;
    state.working = action;
    state.error = "";
    state.refreshWarning = "";
    state.notice = "";
    state.result = null;
    state.resultAction = "";
    render();
    try {
      const response = action === "export"
        ? await exportCurrentModelLibrary(payload)
        : await installCurrentModelLibrary(payload);
      state.result = response?.result || null;
      state.resultAction = action;
      try {
        state.readiness = await fetchModelPublicationReadiness();
      } catch (refreshError) {
        state.refreshWarning = `Publication succeeded, but readiness could not be refreshed: ${modelPublicationErrorMessage(refreshError)}`;
      }
    } catch (error) {
      if (error?.detail?.readiness) state.readiness = error.detail.readiness;
      state.error = modelPublicationErrorMessage(error);
    } finally {
      state.working = "";
      render();
    }
  }

  async function openFolder() {
    if (isBusy()) return;
    readForm();
    state.error = "";
    state.notice = "";
    try {
      await openPublishedModelsFolder();
      state.notice = "Opened the Published Models folder.";
    } catch (error) {
      state.error = modelPublicationErrorMessage(error, "Could not open the Published Models folder");
    }
    render();
  }

  function bind() {
    overlay.querySelector("#modelPublicationClose")?.addEventListener("click", close);
    overlay.querySelector("#modelPublicationRefresh")?.addEventListener("click", () => refreshReadiness());
    overlay.querySelector("#modelPublicationExport")?.addEventListener("click", () => runPublication("export"));
    overlay.querySelector("#modelPublicationInstall")?.addEventListener("click", () => runPublication("install"));
    overlay.querySelector("#modelPublicationOpenFolder")?.addEventListener("click", openFolder);
    overlay.querySelectorAll(".model-publication-field input, .model-publication-field textarea").forEach((field) => {
      field.addEventListener("input", () => {
        readForm();
        const enabled = Boolean(state.readiness?.ready && formComplete() && !state.loading && !isBusy());
        const exportBtn = overlay.querySelector("#modelPublicationExport");
        const installBtn = overlay.querySelector("#modelPublicationInstall");
        if (exportBtn) exportBtn.disabled = !enabled;
        if (installBtn) installBtn.disabled = !enabled;
      });
    });
  }

  overlay.addEventListener("click", (event) => {
    if (event.target === overlay && !isBusy()) close();
  });
  document.body.appendChild(overlay);
  document.addEventListener("keydown", handleKeydown);
  render();
  refreshReadiness({ initial: true });
}

function maintenanceModeLabel(mode = "", operation = null) {
  const key = String(mode || "");
  if (key === "audit") return "Audit";
  if (key === "missing_only") return "Missing Only";
  if (key === "force") {
    if (operation?.operation_id === "export_geometry_files") return "Force Export";
    return "Force Rebuild";
  }
  if (key === "cleanup") return "Quarantine";
  if (key === "stale_only") return "Stale Only";
  if (key === "fit") return "Fit";
  if (key === "reextract") return "Re-extract";
  return key.replace(/_/g, " ") || "Default";
}

function maintenanceRiskLabel(riskClass = "") {
  const key = String(riskClass || "");
  if (key === "read_only") return "Read-only";
  if (key === "writes_derived_files") return "Writes Derived Files";
  if (key === "writes_user_output") return "Writes User Output";
  if (key === "changes_semantic_data") return "Modifies Data";
  if (key === "cleanup") return "Cleanup";
  return key.replace(/_/g, " ") || "Maintenance";
}

function maintenanceWriteLabel(operation = {}) {
  if (!operation?.writes) return "Read-only";
  if (operation.risk_class === "cleanup") return "Moves or removes files";
  if (operation.risk_class === "changes_semantic_data") return "Modifies data";
  if (operation.risk_class === "writes_user_output") return "Writes user-facing files";
  return "Writes derived files";
}

function maintenanceOperationGroup(operation = {}) {
  const id = operation.operation_id || "";
  if (operation.enabled === false) return "Unavailable Operations";
  const category = String(operation.category || "").trim();
  if (category === "Visual Caches") return "Images";
  if (category === "Database Maintenance" || category === "Database Housekeeping") return "System Maintenance";
  if (category === "Cleanup") return "System Maintenance";
  if (category === "Repair / Rebuild" && id === "reextract_sample_images") return "Images";
  if (category) return category;
  if (id === "refit_calibration_models") return "Calibration Models";
  if (operation.risk_class === "cleanup" || id.startsWith("quarantine_")) return "System Maintenance";
  if (operation.risk_class === "writes_user_output") return "Geometry Exports";
  if (id.startsWith("rebuild_") || id.startsWith("regenerate_")) return "Repair / Rebuild";
  return "Health Checks";
}

function maintenanceOperationBrief(operation = {}) {
  if (operation.enabled === false) return "Unavailable";
  return maintenanceWriteLabel(operation);
}

function maintenanceResourceSentence(operation = {}) {
  const claims = operation.resource_claims || [];
  if (!claims.length) return "";
  return claims.map((claim) => claim.replace(/_/g, " ")).join(", ");
}

function maintenanceModeHelp(operation = {}) {
  const modes = operation.modes || [];
  if (modes.includes("missing_only") && modes.includes("force")) {
    if (operation.operation_id === "export_geometry_files") {
      return "Missing Only exports public STEP/STL files that are absent. Force Export can overwrite existing public exports after confirmation.";
    }
    return "Missing Only runs a preflight first and then acts only on missing or repairable targets. Force Rebuild rewrites every repairable target in scope.";
  }
  return "";
}

function maintenanceRunButtonLabel(operation = {}) {
  if (operation.operation_id === "export_geometry_files") return "Export Files";
  if (operation.operation_id === "refit_calibration_models") return "Fit Models";
  return operation.writes ? "Run Maintenance" : "Run Audit";
}

async function loadMaintenanceOperationsForAction() {
  if (Array.isArray(maintenanceState.operations)) return maintenanceState.operations;
  if (maintenanceState.loadPromise) return maintenanceState.loadPromise;
  maintenanceState.loading = true;
  maintenanceState.error = "";
  maintenanceState.loadPromise = fetchMaintenanceOperations()
    .then((payload) => {
      const operations = payload?.operations || [];
      maintenanceState.operations = operations;
      return operations;
    })
    .catch((err) => {
      maintenanceState.error = err.message || "Failed to load maintenance operations";
      throw err;
    })
    .finally(() => {
      maintenanceState.loading = false;
      maintenanceState.loadPromise = null;
    });
  return maintenanceState.loadPromise;
}

async function maintenanceOperationById(operationId) {
  const operations = await loadMaintenanceOperationsForAction();
  return operations.find((operation) => operation.operation_id === operationId) || null;
}

function maintenanceSummaryHtml(summary = {}) {
  const entries = Object.entries(summary || {}).filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!entries.length) return "";
  const formatSummaryValue = (value) => {
    if (Array.isArray(value)) {
      return value.map((item) => typeof item === "object" ? JSON.stringify(item) : String(item)).join(", ");
    }
    if (value && typeof value === "object") {
      return Object.entries(value)
        .map(([childKey, childValue]) => `${childKey.replace(/_/g, " ")}: ${childValue}`)
        .join(", ");
    }
    return String(value);
  };
  return `
    <div class="maintenance-summary-grid">
      ${entries.map(([key, value]) => `
        <div class="maintenance-summary-row">
          <span>${escapeHtml(key.replace(/_/g, " "))}</span>
          <strong>${escapeHtml(formatSummaryValue(value))}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function maintenanceModelResultsHtml(result = {}) {
  const modelResults = result?.model_results || {};
  const keys = ["legacy_spline", "photo_stack_v2", "camera_transform"].filter((key) => modelResults[key]);
  if (!keys.length) return "";
  const labels = {
    legacy_spline: "Color Model v1",
    photo_stack_v2: "Color Model v2",
    camera_transform: "Camera Transform",
  };
  const displayStatus = (item = {}) => {
    const raw = String(item.status || "").toLowerCase();
    if (raw === "skipped") return "Skipped";
    if (raw === "failed" || item.error) return "Failed";
    return "Completed";
  };
  const detailText = (key, item = {}) => {
    if (key === "legacy_spline") {
      return `${Number(item.fitted || 0)} fitted · ${Number(item.failed || 0)} failed · ${Number(item.skipped || 0)} skipped`;
    }
    if (key === "photo_stack_v2") {
      const summary = item.summary || {};
      const bits = [];
      if (item.run_id) bits.push(`run ${item.run_id}`);
      if (summary.swatch_count !== undefined) bits.push(`${summary.swatch_count} swatches`);
      if (summary.sample_count !== undefined) bits.push(`${summary.sample_count} samples`);
      return bits.join(" · ") || "candidate written";
    }
    if (key === "camera_transform") {
      const summary = item.summary || {};
      const bits = [];
      if (item.status === "skipped") bits.push(item.reason || "up to date");
      if (summary.params_sha256) bits.push(`params ${String(summary.params_sha256).slice(0, 12)}`);
      if (summary.usable_swatch_count !== undefined) bits.push(`${summary.usable_swatch_count} swatches`);
      return bits.join(" · ") || (item.status || "complete");
    }
    return item.status || "";
  };
  return `
    <div class="maintenance-model-result-list">
      ${keys.map((key) => {
        const item = modelResults[key] || {};
        return `
          <div class="maintenance-model-result-row">
            <span>
              <strong>${escapeHtml(labels[key] || key.replace(/_/g, " "))}</strong>
              <small>${escapeHtml(detailText(key, item))}</small>
            </span>
            <span class="maintenance-model-result-status">${escapeHtml(displayStatus(item))}</span>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function maintenanceWarningsHtml(warnings = []) {
  const source = Array.isArray(warnings) ? warnings : [warnings];
  const items = source.filter((warning) => warning !== null && warning !== undefined && warning !== "");
  if (!items.length) return "";
  return `
    <div class="backup-restore-warnings maintenance-workflow-warnings">
      ${items.map((warning) => `<div>${escapeHtml(warning.message || String(warning))}</div>`).join("")}
    </div>
  `;
}

function maintenanceFindingsHtml(result = {}) {
  const findings = result.findings || [];
  const rawBlocked = result.blocked || [];
  const manualRequired = [
    ...(result.manual_required || []),
    ...rawBlocked.filter((item) => item?.category === "manual_required"),
  ];
  const blocked = rawBlocked.filter((item) => item?.category !== "manual_required");
  const errors = result.errors || [];
  const findingTargetLabel = (item = {}) => {
    if (item.target) return String(item.target);
    if (item.sample_id && item.kind) return `${item.sample_id}/${item.kind}`;
    if (item.sample_id) return String(item.sample_id);
    if (item.filename) return String(item.filename);
    if (item.blank_id) return String(item.blank_id);
    if (item.geometry_id) return String(item.geometry_id);
    if (item.path) return String(item.path);
    return "";
  };
  const findingMessage = (item = {}) => {
    const direct = item.message || item.reason;
    if (direct && typeof direct === "object") {
      return JSON.stringify(direct);
    }
    if (direct) return String(direct);
    if (item.original_path && item.quarantine_path) {
      return `Moved to ${displayPathFromPrismaRoot(item.quarantine_path)}`;
    }
    if (item.category) return String(item.category).replace(/_/g, " ");
    return String(item);
  };
  const findingItemHtml = (item = {}) => {
    const targetLabel = findingTargetLabel(item);
    return `
      <div class="maintenance-finding">
        ${item.severity ? `<span class="maintenance-finding-severity">${escapeHtml(item.severity)}</span>` : ""}
        ${targetLabel ? `<span class="mono">${escapeHtml(targetLabel)}</span>` : ""}
        <span>${escapeHtml(findingMessage(item))}</span>
      </div>
    `;
  };
  const groups = [
    { title: "Errors", items: errors.map((message) => ({ message })), kind: "error" },
    { title: "Needs Manual Corners", items: manualRequired, kind: "warning" },
    { title: "Blocked", items: blocked, kind: "warning" },
    { title: "Findings", items: findings.slice(0, 80), kind: "" },
  ].filter((group) => group.items.length);
  if (!groups.length) return "";
  return `
    <div class="maintenance-report-sections">
      ${groups.map((group) => `
        <div class="maintenance-report-section ${group.kind ? `is-${group.kind}` : ""}">
          <strong>${escapeHtml(group.title)}</strong>
          <div class="maintenance-finding-list">
            ${group.items.map(findingItemHtml).join("")}
          </div>
        </div>
      `).join("")}
    </div>
  `;
}

function maintenanceProgressHtml(job, active, fallbackMessage) {
  if (!job || !active) return "";
  const progress = job.progress || {};
  const percent = Number(progress.percent || 0);
  const message = job.message || progress.message || fallbackMessage || "Running maintenance";
  const current = Number(progress.current || 0);
  const total = Number(progress.total || 0);
  const target = progress.target || "";
  const summary = progress.summary || {};
  const stageProgress = summary.stage_progress || {};
  const stageCurrent = Number(stageProgress.current || 0);
  const stageTotal = Number(stageProgress.total || 0);
  const stageLabel = summary.stage ? String(summary.stage).replace(/_/g, " ") : "";
  const countText = stageTotal
    ? `${Math.min(stageCurrent, stageTotal)} / ${stageTotal}`
    : (total ? `${Math.min(current, total)} / ${total}` : "");
  return `
    <div class="backup-progress maintenance-progress" role="status" aria-live="polite">
      <div class="backup-progress-topline">
        <strong>${escapeHtml(message)}</strong>
        <span>${Math.max(0, Math.min(100, percent)).toFixed(0)}%</span>
      </div>
      <div class="backup-progress-bar" aria-hidden="true">
        <div class="backup-progress-fill" style="width: ${Math.max(0, Math.min(100, percent)).toFixed(0)}%;"></div>
      </div>
      <div class="backup-progress-meta">
        ${stageLabel ? `<span>${escapeHtml(stageLabel)}</span>` : ""}
        ${countText ? `<span>${escapeHtml(countText)}</span>` : ""}
        ${target ? `<span class="mono">${escapeHtml(target)}</span>` : ""}
      </div>
    </div>
  `;
}

function reextractProgressHtml(job, active, fallbackMessage = "Running re-extraction") {
  if (!job || !active) return "";
  const progress = job.progress || {};
  const percent = Number(progress.percent || 0);
  const message = job.message || progress.message || progress.action_label || fallbackMessage;
  const action = progress.action_label || progress.action || "";
  const sampleId = progress.sample_id || progress.target || job.sample_id || "";
  const current = Number(progress.current || 0);
  const total = Number(progress.total || 0);
  const sampleIndex = Number(progress.sample_index || 0);
  const sampleTotal = Number(progress.sample_total || total || 0);
  const actionIndex = Number(progress.action_index || 0);
  const actionTotal = Number(progress.action_total || 0);
  const elapsed = Number(progress.elapsed_seconds || 0);
  const clampedPercent = Math.max(0, Math.min(100, percent));
  const etaSeconds = elapsed > 3 && clampedPercent > 2 && clampedPercent < 100
    ? Math.max(0, (elapsed / (clampedPercent / 100)) - elapsed)
    : 0;
  const etaText = etaSeconds
    ? (etaSeconds >= 60 ? `${Math.ceil(etaSeconds / 60)}m remaining` : `${Math.ceil(etaSeconds)}s remaining`)
    : "";
  const countText = sampleTotal
    ? `${Math.min(Math.max(1, sampleIndex || Math.ceil(current)), sampleTotal)} / ${sampleTotal} samples`
    : (total ? `${Math.min(current, total)} / ${total}` : "");
  return `
    <div class="backup-progress maintenance-progress reextract-progress" role="status" aria-live="polite">
      <div class="backup-progress-topline">
        <strong>${escapeHtml(message)}</strong>
        <span>${clampedPercent.toFixed(1)}%</span>
      </div>
      <div class="backup-progress-bar" aria-hidden="true">
        <div class="backup-progress-fill" style="width: ${clampedPercent.toFixed(1)}%;"></div>
      </div>
      <div class="backup-progress-meta">
        ${countText ? `<span>${escapeHtml(countText)}</span>` : ""}
        ${sampleId ? `<span class="mono">${escapeHtml(sampleId)}</span>` : ""}
        ${action ? `<span>${escapeHtml(action)}</span>` : ""}
        ${actionTotal ? `<span>${Math.min(actionIndex, actionTotal)} / ${actionTotal} actions</span>` : ""}
        ${elapsed ? `<span>${elapsed.toFixed(1)}s elapsed</span>` : ""}
        ${etaText ? `<span>${escapeHtml(etaText)}</span>` : ""}
      </div>
    </div>
  `;
}

function showMaintenanceDialog() {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay maintenance-overlay";
  const state = {
    operations: maintenanceState.operations || [],
    reports: [],
    loading: !maintenanceState.operations,
    error: "",
    selectedOperationId: null,
  };
  let cardHeightFrame = 0;

  const close = () => {
    if (cardHeightFrame) cancelAnimationFrame(cardHeightFrame);
    overlay.remove();
    document.removeEventListener("keydown", handleKeydown);
    window.removeEventListener("resize", scheduleCardHeightSync);
  };
  const handleKeydown = (event) => {
    if (document.querySelector(".maintenance-workflow-overlay, .maintenance-activity-overlay")) return;
    if (event.key === "Escape") close();
  };

  function syncCardHeights() {
    cardHeightFrame = 0;
    const dialog = overlay.querySelector(".maintenance-dialog");
    const cards = [...overlay.querySelectorAll(".maintenance-operation-card")];
    if (!dialog || !cards.length) return;
    dialog.style.removeProperty("--maintenance-operation-card-height");
    const maxHeight = Math.ceil(Math.max(...cards.map((card) => card.getBoundingClientRect().height)));
    if (Number.isFinite(maxHeight) && maxHeight > 0) {
      dialog.style.setProperty("--maintenance-operation-card-height", `${maxHeight}px`);
    }
  }

  function scheduleCardHeightSync() {
    if (cardHeightFrame) cancelAnimationFrame(cardHeightFrame);
    cardHeightFrame = requestAnimationFrame(syncCardHeights);
  }

  function selectedOperation() {
    return state.operations.find((operation) => operation.operation_id === state.selectedOperationId) || state.operations[0] || null;
  }

  function groupedOperations() {
    const groups = new Map();
    state.operations.forEach((operation) => {
      const category = maintenanceOperationGroup(operation);
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(operation);
    });
    const categoryOrder = new Map([
      ["Audit", 0],
      ["Health Checks", 0],
      ["Images", 10],
      ["Calibration Models", 11],
      ["Geometry Artifacts", 20],
      ["Geometry Exports", 20],
      ["System Maintenance", 21],
      ["Unavailable Operations", 99],
    ]);
    return [...groups.entries()].sort(([categoryA], [categoryB]) => {
      const rankA = categoryOrder.get(categoryA) ?? 50;
      const rankB = categoryOrder.get(categoryB) ?? 50;
      if (rankA !== rankB) return rankA - rankB;
      return String(categoryA).localeCompare(String(categoryB));
    });
  }

  async function refreshReports() {
    try {
      const payload = await fetchMaintenanceReports();
      state.reports = payload?.reports || [];
    } catch (err) {
      console.warn("[maintenance] Failed to load reports:", err);
    }
  }

  function reportTableHtml() {
    if (!state.reports.length) {
      return `<p class="small-copy">No maintenance reports yet.</p>`;
    }
    return `
      <div class="maintenance-activity-table" role="table" aria-label="Maintenance activity log">
        <div class="maintenance-activity-head" role="row">
          <span role="columnheader">Name</span>
          <span role="columnheader">Type</span>
          <span role="columnheader">Status</span>
          <span role="columnheader">File Name</span>
        </div>
        <div class="maintenance-activity-rows">
          ${state.reports.map((report) => {
            const reportOperation = state.operations.find((operation) => operation.operation_id === report.operation_id) || null;
            const reportName = reportOperation?.name || report.operation_id || report.report_id || "";
            return `
            <div class="maintenance-activity-row" role="row">
              <span role="cell" title="${escapeHtml(reportName)}">${escapeHtml(reportName)}</span>
              <span role="cell" title="${escapeHtml(maintenanceModeLabel(report.mode || "", reportOperation))}">${escapeHtml(maintenanceModeLabel(report.mode || "", reportOperation))}</span>
              <span role="cell" title="${escapeHtml(report.status || "")}">${escapeHtml(report.status || "")}</span>
              <span role="cell" title="${escapeHtml(report.report_id || "")}">${escapeHtml(report.report_id || "")}</span>
            </div>
          `;
          }).join("")}
        </div>
      </div>
    `;
  }

  function showActivityLog() {
    const logOverlay = document.createElement("div");
    logOverlay.className = "info-dialog-overlay maintenance-activity-overlay";
    const activityState = {
      clearing: false,
      message: "",
      messageKind: "success",
      childDialogOpen: false,
    };
    const closeLog = () => {
      logOverlay.remove();
      document.removeEventListener("keydown", handleLogKeydown);
    };
    const handleLogKeydown = (event) => {
      if (activityState.childDialogOpen) return;
      if (event.key === "Escape") closeLog();
    };

    const clearConfirmDialog = () => new Promise((resolve) => {
      activityState.childDialogOpen = true;
      const confirmOverlay = document.createElement("div");
      confirmOverlay.className = "info-dialog-overlay maintenance-clear-confirm-overlay";
      confirmOverlay.innerHTML = `
        <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="maintenanceClearTitle">
          ${renderDialogHeader({
            title: "Clear Activity Log",
            titleId: "maintenanceClearTitle",
            closeButtonHtml: renderWindowCloseButton({ id: "maintenanceClearClose", className: "info-dialog-close" }),
          })}
          <div class="info-dialog-body">
            <p class="info-dialog-lede">Clear maintenance activity log?</p>
            <p>This deletes saved maintenance report JSON files. It does not delete backups, RAW archives, source images, generated artifacts, quarantine files, SQLite data, or sample records.</p>
          </div>
          <div class="info-dialog-footer">
            <button class="delete-button small" type="button" id="maintenanceClearConfirm">Clear Log</button>
            <button class="ghost-button small" type="button" id="maintenanceClearCancel">Cancel</button>
          </div>
        </div>
      `;
      const cleanup = (value) => {
        confirmOverlay.remove();
        document.removeEventListener("keydown", handleConfirmKeydown);
        activityState.childDialogOpen = false;
        resolve(value);
      };
      const handleConfirmKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      confirmOverlay.querySelector("#maintenanceClearConfirm")?.addEventListener("click", () => cleanup(true));
      confirmOverlay.querySelector("#maintenanceClearCancel")?.addEventListener("click", () => cleanup(false));
      confirmOverlay.querySelector("#maintenanceClearClose")?.addEventListener("click", () => cleanup(false));
      document.body.appendChild(confirmOverlay);
      document.addEventListener("keydown", handleConfirmKeydown);
    });

    const handleClearLog = async () => {
      if (activityState.clearing || !state.reports.length) return;
      const confirmed = await clearConfirmDialog();
      if (!confirmed) return;
      activityState.clearing = true;
      activityState.message = "";
      renderActivityLog();
      try {
        const result = await clearMaintenanceReports();
        await refreshReports();
        const failed = Number(result?.failed_count || 0);
        const deleted = Number(result?.deleted_count || 0);
        const skipped = Number(result?.skipped_count || 0);
        if (failed > 0) {
          activityState.messageKind = "warning";
          activityState.message = `Deleted ${deleted} report${deleted === 1 ? "" : "s"}; ${failed} could not be deleted.`;
        } else if (skipped > 0) {
          activityState.messageKind = "warning";
          activityState.message = `Deleted ${deleted} report${deleted === 1 ? "" : "s"}; skipped ${skipped} non-report item${skipped === 1 ? "" : "s"}.`;
        } else {
          activityState.messageKind = "success";
          activityState.message = deleted
            ? `Deleted ${deleted} maintenance report${deleted === 1 ? "" : "s"}.`
            : "No maintenance reports to clear.";
        }
      } catch (err) {
        activityState.messageKind = "error";
        activityState.message = err.message || "Could not clear maintenance reports.";
      } finally {
        activityState.clearing = false;
        renderActivityLog();
      }
    };

    function activityMessageHtml() {
      if (!activityState.message) return "";
      const className = activityState.messageKind === "error"
        ? "backup-restore-message is-error"
        : (activityState.messageKind === "warning" ? "backup-restore-message is-warning" : "backup-restore-message is-success");
      return `<div class="${className}">${escapeHtml(activityState.message)}</div>`;
    }

    function renderActivityLog() {
      logOverlay.innerHTML = `
        <div class="info-dialog maintenance-activity-dialog" role="dialog" aria-modal="true" aria-labeledby="maintenanceActivityTitle">
          ${renderDialogHeader({
            title: "Maintenance Activity Log",
            titleId: "maintenanceActivityTitle",
            closeButtonHtml: renderWindowCloseButton({ id: "maintenanceActivityClose", className: "info-dialog-close" }),
          })}
          <div class="maintenance-activity-body">
            ${activityMessageHtml()}
            ${reportTableHtml()}
          </div>
          <div class="info-dialog-footer maintenance-activity-footer">
            <button class="ghost-button small" type="button" id="maintenanceActivityClear" ${activityState.clearing || !state.reports.length ? "disabled" : ""}>${activityState.clearing ? "Clearing..." : "Clear Log"}</button>
            <button class="ghost-button small" type="button" id="maintenanceActivityDone">Close</button>
          </div>
        </div>
      `;
      logOverlay.querySelector("#maintenanceActivityClose")?.addEventListener("click", closeLog);
      logOverlay.querySelector("#maintenanceActivityDone")?.addEventListener("click", closeLog);
      logOverlay.querySelector("#maintenanceActivityClear")?.addEventListener("click", handleClearLog);
    }

    document.body.appendChild(logOverlay);
    document.addEventListener("keydown", handleLogKeydown);
    renderActivityLog();
  }

  function operationListHtml() {
    if (state.loading) {
      return `<div class="maintenance-loading">Loading maintenance operations...</div>`;
    }
    if (state.error) {
      return `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>`;
    }
    const groupHtml = ([category, operations]) => `
      <div class="maintenance-operation-group">
        <div class="drawer-module-cap">
          <span class="sidebar-label">${escapeHtml(category)}</span>
        </div>
        <div class="maintenance-operation-list">
          ${operations.map((operation) => {
            const active = selectedOperation()?.operation_id === operation.operation_id;
            const disabled = operation.enabled === false;
            return `
              <button class="maintenance-operation-card ${active ? "is-active" : ""} ${disabled ? "is-disabled" : ""}" type="button" data-maintenance-operation="${escapeHtml(operation.operation_id)}">
                <span class="maintenance-operation-radio" aria-hidden="true"></span>
                <span class="maintenance-operation-copy">
                  <span class="maintenance-operation-title">${escapeHtml(operation.name || operation.operation_id)}</span>
                  <span class="maintenance-operation-description">${escapeHtml(operation.description || "")}</span>
                  <span class="maintenance-operation-meta">${escapeHtml(maintenanceOperationBrief(operation))}</span>
                </span>
              </button>
            `;
          }).join("")}
        </div>
      </div>
    `;
    const columnIndexForCategory = (category) => {
      const key = String(category || "").toLowerCase();
      if (key === "audit" || key === "health checks") return 0;
      if (key === "images" || key === "calibration models") return 1;
      if (
        key === "system maintenance" ||
        key === "geometry artifacts" ||
        key === "geometry exports"
      ) return 2;
      return 2;
    };
    const columns = [[], [], []];
    groupedOperations().forEach((group) => {
      columns[columnIndexForCategory(group[0])].push(group);
    });
    return columns
      .filter((column) => column.length)
      .map((column) => `
        <div class="maintenance-operation-column">
          ${column.map(groupHtml).join("")}
        </div>
      `)
      .join("");
  }

  function operationDetailHtml(operation) {
    if (!operation) {
      return `<p class="small-copy">Select a maintenance operation.</p>`;
    }
    const unavailableReason = operation.enabled === false
      ? (operation.unavailable_reason || operation.disabled_reason || "")
      : "";
    return `
      <div class="maintenance-detail-card ${operation.enabled === false ? "is-disabled" : ""}">
        <div class="maintenance-detail-body">
          <div class="maintenance-detail-heading maintenance-detail-copy">
            <h4>${escapeHtml(operation.name || operation.operation_id)}</h4>
            ${unavailableReason ? `<p class="maintenance-detail-meta maintenance-disabled-reason">${escapeHtml(unavailableReason)}</p>` : ""}
          </div>
          <div class="maintenance-detail-actions">
            <button class="primary-button small" type="button" id="maintenanceStartWorkflow" ${operation.enabled === false ? "disabled" : ""}>Start Workflow</button>
          </div>
        </div>
      </div>
    `;
  }

  function render() {
    const operation = selectedOperation();
    overlay.innerHTML = `
      <div class="info-dialog maintenance-dialog" role="dialog" aria-modal="true" aria-labeledby="maintenanceTitle">
        ${renderDialogHeader({
          title: "Maintenance",
          titleId: "maintenanceTitle",
          actionsHtml: `<button class="ghost-button small dialog-header-action" type="button" id="maintenanceActivityLog">Activity Log</button>`,
          closeButtonHtml: renderWindowCloseButton({ id: "maintenanceClose", className: "info-dialog-close" }),
        })}
        <div class="maintenance-body">
          <section class="maintenance-operations-surface">
            ${operationListHtml()}
          </section>
          <section class="maintenance-panel maintenance-detail-panel">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Selected Operation</span>
            </div>
            <div class="maintenance-panel-body">
              ${operationDetailHtml(operation)}
            </div>
          </section>
        </div>
      </div>
    `;
    bind();
    scheduleCardHeightSync();
  }

  function bind() {
    overlay.querySelector("#maintenanceClose")?.addEventListener("click", close);
    overlay.querySelector("#maintenanceActivityLog")?.addEventListener("click", async () => {
      await refreshReports();
      showActivityLog();
    });
    overlay.querySelectorAll("[data-maintenance-operation]").forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedOperationId = button.dataset.maintenanceOperation || null;
        render();
      });
    });
    overlay.querySelector("#maintenanceStartWorkflow")?.addEventListener("click", () => {
      const operation = selectedOperation();
      if (!operation || operation.enabled === false) return;
      if (operation.operation_id === "reextract_sample_images") {
        showReextractSampleImagesWorkflow(operation, async () => {
          await refreshReports();
          render();
        });
        return;
      }
      showMaintenanceWorkflow(operation, async () => {
        await refreshReports();
        render();
      });
    });
  }

  document.body.appendChild(overlay);
  document.addEventListener("keydown", handleKeydown);
  window.addEventListener("resize", scheduleCardHeightSync);
  render();
  (async () => {
    try {
      const [operationsPayload] = await Promise.all([
        fetchMaintenanceOperations(),
        refreshReports(),
      ]);
      state.operations = operationsPayload?.operations || [];
      maintenanceState.operations = state.operations;
      if (!state.selectedOperationId && state.operations.length) {
        state.selectedOperationId = state.operations[0].operation_id;
      }
      state.loading = false;
      render();
    } catch (err) {
      state.loading = false;
      state.error = err.message || "Failed to load maintenance operations";
      render();
    }
  })();
}

function showReextractSampleImagesWorkflow(operation, onComplete) {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay maintenance-workflow-overlay reextract-workflow-overlay";
  let reviewOverlay = null;
  const state = {
    operation,
    domainMode: "complete",
    segmentationMode: "existing_coordinates",
    sampleScopeMode: "all_accepted",
    sampleIdsText: "",
    preflight: null,
    preflightExpanded: true,
    candidateSummaryExpanded: true,
    generationReport: null,
    candidateSetId: "",
    candidateSet: null,
    samples: [],
    selectedSampleId: "",
    selectedSample: null,
    reviewDialogMode: "",
    applyReport: null,
    job: null,
    jobKind: "",
    running: false,
    cancelling: false,
    busy: false,
    loading: false,
    error: "",
  };
  const parseReextractSampleIds = () => {
    const seen = new Set();
    return String(state.sampleIdsText || "")
      .split(/[\s,;]+/)
      .map((item) => item.trim())
      .filter((item) => {
        if (!item || seen.has(item)) return false;
        seen.add(item);
        return true;
      });
  };
  const scopePayload = () => ({
    domain_mode: state.domainMode,
    segmentation_mode: state.segmentationMode,
    sample_scope: (() => {
      if (state.sampleScopeMode !== "sample_ids") return { kind: "all_accepted" };
      const sampleIds = parseReextractSampleIds();
      if (!sampleIds.length) throw new Error("Enter at least one sample ID.");
      return { kind: "sample_ids", sample_ids: sampleIds };
    })(),
  });
  const statusLabel = (status = "") => {
    const normalized = String(status || "");
    if (normalized === "ready_changed" || normalized === "ready_unchanged") return "Ready";
    return normalized.replace(/_/g, " ");
  };
  const reextractWorkflowDescription = "Re-extract color data from samples which have already been successfully processed and accepted. Re-extracted data will not replace the existing data until the user accepts the re-extracted results.";
  const reextractDomainLabel = (value = state.domainMode) => {
    if (value === "complete") return "Complete";
    if (value === "transmission_only") return "Transmission only";
    if (value === "appearance_only") return "Appearance only";
    return String(value || "").replace(/_/g, " ") || "Complete";
  };
  const reextractSegmentationLabel = (value = state.segmentationMode) => {
    if (value === "existing_coordinates") return "Use accepted coordinates";
    if (value === "redetect_from_scratch") return "Re-detect strip";
    return String(value || "").replace(/_/g, " ") || "Use accepted coordinates";
  };
  const reextractSummaryRowsHtml = (summary = {}) => {
    const automated = Number(summary.targets || 0);
    const manual = Number(summary.manual_required || 0);
    const blocked = Number(summary.blocked || 0) + Number(summary.unsupported_provenance || 0);
    const total = automated + manual + blocked;
    const row = (label, value, extraClass = "") => `
      <div class="maintenance-summary-row ${extraClass}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(String(value ?? ""))}</strong>
      </div>
    `;
    const rows = [
      ["Samples to Re-extract", total, ""],
      ["Automated", automated, "is-child"],
      ["Manual", manual, "is-child"],
      ["Blocked", blocked, "is-child"],
      ["Data", reextractDomainLabel(summary.domain_mode || state.domainMode), ""],
      ["Coordinates", reextractSegmentationLabel(summary.segmentation_mode || state.segmentationMode), ""],
    ];
    return `<div class="maintenance-summary-grid">${rows.map(([label, value, extraClass]) => row(label, value, extraClass)).join("")}</div>`;
  };
  const reextractManualNoticeHtml = (items = []) => {
    const samples = (items || [])
      .filter((item) => item?.category === "manual_required")
      .map((item) => String(item.target || item.sample_id || "").trim())
      .filter(Boolean);
    if (!samples.length) return "";
    return `
      <div class="reextract-manual-notice">
        The following samples will require manual re-processing: ${samples.map((sampleId) => `<span class="mono">${escapeHtml(sampleId)}</span>`).join(", ")}.
      </div>
    `;
  };
  const reextractManualFindings = (preflight = {}) =>
    (preflight.blocked || []).filter((item) => item?.category === "manual_required");
  const reextractBlockedFindings = (preflight = {}) =>
    (preflight.blocked || []).filter((item) => item?.category !== "manual_required");
  const reextractCandidateSummaryHtml = (summary = {}) => {
    const automated = Number(summary.targets || 0);
    const changed = Number(summary.ready_changed || 0);
    const unchanged = Number(summary.ready_unchanged || 0);
    const manual = Number(summary.manual_required || 0);
    const failed = Number(summary.failed || 0);
    const blocked = Number(summary.blocked || 0);
    const total = automated + manual + failed + blocked;
    const row = (label, value, extraClass = "") => `
      <div class="maintenance-summary-row ${extraClass}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(String(value ?? ""))}</strong>
      </div>
    `;
    return `
      <div class="maintenance-summary-grid">
        ${row("Sample Count", total)}
        ${row("Automated", automated)}
        ${row("Changed", changed, "is-child")}
        ${row("Unchanged", unchanged, "is-child")}
        ${row("Manual (pending)", manual)}
        ${row("Failed", failed)}
        ${row("Blocked", blocked)}
      </div>
    `;
  };
  const workflowSectionCap = (label, expanded, toggleId, toggleEnabled = true) => `
    <div class="drawer-module-cap maintenance-collapsible-cap">
      <span class="sidebar-label">${escapeHtml(label)}</span>
      ${toggleEnabled ? `
        <div class="drawer-module-cap-actions">
          <button class="drawer-utility-button" type="button" id="${_escAttr(toggleId)}" aria-expanded="${expanded ? "true" : "false"}">
            ${expanded ? "Hide" : "Show"}
          </button>
        </div>
      ` : ""}
    </div>
  `;
  const readySamples = () => state.samples.filter((sample) =>
    sample.status === "ready_changed" || sample.status === "ready_unchanged"
  );
  const isReviewableCandidate = (sample) =>
    sample?.status === "ready_changed" || sample?.status === "ready_unchanged";
  const candidateDecision = (sample) => sample?.review?.decision || (isReviewableCandidate(sample) ? "pending" : "skip");
  const candidateDecisionMeta = (sample) => {
    if (!isReviewableCandidate(sample)) {
      if (sample?.status === "manual_required") return { label: "Manual", cls: "flagged" };
      if (sample?.status === "failed") return { label: "Failed", cls: "failed" };
      if (sample?.status === "blocked") return { label: "Blocked", cls: "failed" };
      if (sample?.status === "stale") return { label: "Stale", cls: "stale" };
      return { label: "Not applicable", cls: "none" };
    }
    const decision = candidateDecision(sample);
    if (decision === "save") return { label: "Save", cls: "accepted" };
    if (decision === "skip") return { label: "Skip", cls: "planned" };
    return { label: "Pending", cls: "pending" };
  };
  const candidateDecisionPill = (sample) => {
    const meta = candidateDecisionMeta(sample);
    return `<span class="status-pill ${_escAttr(meta.cls)}">${escapeHtml(meta.label)}</span>`;
  };
  const savedReadySamples = () => readySamples().filter((sample) => candidateDecision(sample) === "save");
  const pendingReadySamples = () => readySamples().filter((sample) => candidateDecision(sample) === "pending");
  const readiness = () => state.candidateSet?.readiness || {};
  const hasManualPending = () => Number(readiness().manual_pending_count || 0) > 0 || state.samples.some((sample) => sample.status === "manual_required");
  const finalReviewReady = () => Boolean(readiness().final_review_ready) && !hasManualPending();
  const saveReady = () => Boolean(readiness().save_ready) && !pendingReadySamples().length;
  const reextractApplyResultMessage = (status) => {
    const normalized = String(status || "");
    if (normalized === "completed") return "Saved results applied.";
    if (normalized === "partial") return "Some saved results could not be applied.";
    if (normalized === "cancelled") return "Save cancelled.";
    if (normalized === "failed") return "Save failed.";
    return `Save ${normalized || "finished"}.`;
  };
  const selectedSample = () => state.selectedSample || state.samples.find((sample) => sample.sample_id === state.selectedSampleId) || null;
  const jobIsActive = (job = state.job) => ["queued", "running", "cancelling"].includes(String(job?.status || ""));
  const terminalJobStatus = (job) => ["succeeded", "failed", "cancelled"].includes(String(job?.status || ""));
  const clearPreflightState = () => {
    state.preflight = null;
    state.preflightExpanded = true;
    state.candidateSummaryExpanded = true;
    state.generationReport = null;
    state.applyReport = null;
    state.error = "";
  };
  const candidateArtifactImg = (sample, kind, label, extraClass = "") => {
    if (!sample?.artifacts?.[kind] || !state.candidateSetId) return "";
    const version = encodeURIComponent(sample.created_at || sample.applied_at || sample.status || "");
    const src = `${reextractCandidateArtifactUrl(state.candidateSetId, sample.sample_id, kind)}${version ? `?v=${version}` : ""}`;
    return `
      <figure class="reextract-artifact ${extraClass}">
        <img src="${src}" alt="${_escAttr(label)}">
        <figcaption>${escapeHtml(label)}</figcaption>
      </figure>
    `;
  };
  const candidateSwatches = (sample) => {
    return [...(sample?.replacement_extraction_result?.measurements?.swatches || [])].sort((a, b) => {
      const ai = Number(a.swatch_index ?? Number.MAX_SAFE_INTEGER);
      const bi = Number(b.swatch_index ?? Number.MAX_SAFE_INTEGER);
      if (ai !== bi) return ai - bi;
      return Number(a.nominal_thickness_mm ?? 0) - Number(b.nominal_thickness_mm ?? 0);
    });
  };
  const candidateChipHex = (sample, swatch, domain) => {
    if (domain === "appearance") {
      const indexed = sample?.colors_by_swatch_index?.[String(swatch.swatch_index)];
      if (Array.isArray(indexed) && indexed.length >= 3) {
        return rgbValuesToHex(indexed[0], indexed[1], indexed[2]);
      }
      const appearance = swatch?.appearance || null;
      return appearance ? rgbValuesToHex(appearance.jpeg_r, appearance.jpeg_g, appearance.jpeg_b) : "";
    }
    return swatch?.display?.hex || "";
  };
  const candidateStripRatio = (sample) => {
    const geometry = sample?.diagnostics?.visual_geometry || {};
    const width = Number(geometry.strip_width);
    const height = Number(geometry.strip_height);
    return width > 0 && height > 0 ? width / height : 4;
  };
  const candidateMockStripHtml = (sample, domain, renderKey) => {
    const swatches = candidateSwatches(sample);
    const n = Math.max(swatches.length || Number(sample?.swatch_count || 0) || 0, 1);
    const geometry = sample?.diagnostics?.visual_geometry || {};
    const boundaries = Array.isArray(geometry.boundaries) ? geometry.boundaries.map(Number).filter(Number.isFinite) : [];
    const columnWidths = boundaries.length >= n + 1
      ? boundaries.slice(0, n).map((value, index) => Math.max(1, Number(boundaries[index + 1]) - Number(value)))
      : [];
    const gridTemplate = columnWidths.length === n
      ? columnWidths.map((value) => `${value}fr`).join(" ")
      : `repeat(${n}, minmax(0, 1fr))`;
    const tiles = swatches.length
      ? swatches.map((sw, index) => {
        const hex = candidateChipHex(sample, sw, domain);
        return `<div class="sample-render-swatch${index > 0 ? " has-divider" : ""}${hex ? "" : " is-missing"}" style="background:${hex || "#d8d5cc"}"></div>`;
      }).join("")
      : `<div class="sample-render-swatch is-missing"></div>`;
    return `
      <div class="sample-strip-frame reextract-strip-frame" data-reextract-strip-render-frame="${_escAttr(renderKey)}" style="--strip-ratio:${candidateStripRatio(sample)}">
        <div class="sample-render-stage sample-render-stage-sync"
             data-reextract-strip-render="${_escAttr(renderKey)}"
             data-inner-x="${_escAttr(geometry.inner_x ?? "")}"
             data-inner-y="${_escAttr(geometry.inner_y ?? "")}"
             data-inner-w="${_escAttr(geometry.inner_w ?? "")}"
             data-inner-h="${_escAttr(geometry.inner_h ?? "")}"
             data-strip-w="${_escAttr(geometry.strip_width ?? "")}"
             data-strip-h="${_escAttr(geometry.strip_height ?? "")}"
             data-swatches="${n}">
          <div class="sample-render-shell" style="grid-template-columns:${_escAttr(gridTemplate)}">
            ${tiles}
          </div>
        </div>
      </div>
    `;
  };
  const candidateStripReviewRow = (sample, kind, label, domain) => {
    if (!sample?.artifacts?.[kind] || !state.candidateSetId) return "";
    const renderKey = `${sample.sample_id}:${kind}`;
    const version = encodeURIComponent(sample.created_at || sample.applied_at || sample.status || "");
    const src = `${reextractCandidateArtifactUrl(state.candidateSetId, sample.sample_id, kind)}${version ? `?v=${version}` : ""}`;
    return `
      <div class="sample-strip-row reextract-strip-review-row">
        <div class="sample-strip-label-bubble">${escapeHtml(label).replace(/\s+/g, "<br>")}</div>
        <div class="sample-strip-row-content" style="--strip-ratio:${candidateStripRatio(sample)}">
          <div class="sample-strip-frame reextract-strip-frame" data-reextract-strip-source-frame="${_escAttr(renderKey)}" style="--strip-ratio:${candidateStripRatio(sample)}">
            <button class="drawer-thumb-button" type="button" data-lightbox-src="${_escAttr(src)}" data-lightbox-title="${_escAttr(`${sample.sample_id} ${label}`)}">
              <img class="drawer-thumb drawer-thumb-strip" src="${_escAttr(src)}" alt="${_escAttr(`${sample.sample_id} ${label}`)}" data-reextract-strip-source="${_escAttr(renderKey)}">
            </button>
          </div>
          ${candidateMockStripHtml(sample, domain, renderKey)}
        </div>
      </div>
    `;
  };
  const reextractCoordinateSourceLabel = (coordinateSpace = "") => {
    const normalized = String(coordinateSpace || "");
    if (normalized.includes("manual_full_image")) return "Accepted manual coordinates";
    if (normalized.includes("automatic_full_image")) return "Accepted automatic coordinates";
    if (state.segmentationMode === "redetect_from_scratch") return "Re-detected strip";
    return "Accepted coordinates";
  };
  const reextractAlignmentLabel = (strategy = "") => {
    const normalized = String(strategy || "");
    if (!normalized) return "";
    if (normalized.includes("legacy_resize_fallback")) return "Matched blank to source with fallback";
    if (normalized.includes("homography")) return "Matched blank to source";
    return normalized.replace(/_/g, " ");
  };
  const candidateReviewSummaryHtml = (sample) => {
    const diagnostics = sample?.diagnostics || {};
    const rows = [];
    const addRow = (label, value) => {
      if (value === null || value === undefined || value === "") return;
      rows.push([label, value]);
    };
    addRow("Data", reextractDomainLabel(diagnostics.domain_mode || sample?.domain_mode || state.domainMode));
    addRow("Coordinates", reextractCoordinateSourceLabel(diagnostics.coordinate_space));
    addRow("Alignment", reextractAlignmentLabel(diagnostics.registration_strategy));
    if (diagnostics.blank_orientation_rotations !== null && diagnostics.blank_orientation_rotations !== undefined) {
      const rotations = Number(diagnostics.blank_orientation_rotations || 0);
      addRow("Blank rotation", rotations ? `${rotations * 90} degrees` : "None");
    }
    if (diagnostics.strip_orientation_flipped !== null && diagnostics.strip_orientation_flipped !== undefined) {
      addRow("Strip order", diagnostics.strip_orientation_flipped ? "Flipped to match swatches" : "Kept as captured");
    }
    if (diagnostics.appearance_orientation_flipped !== null && diagnostics.appearance_orientation_flipped !== undefined) {
      addRow("Appearance order", diagnostics.appearance_orientation_flipped ? "Flipped to match swatches" : "Kept as captured");
    }
    if (Array.isArray(diagnostics.missing_required_artifacts) && diagnostics.missing_required_artifacts.length) {
      addRow("Missing review images", diagnostics.missing_required_artifacts.join(", "));
    }
    if (diagnostics.appearance_error) {
      addRow("Appearance", diagnostics.appearance_error);
    }
    if (!rows.length) return "";
    return `
      <div class="maintenance-summary-grid reextract-review-summary" aria-label="Extraction summary">
        ${rows.map(([label, value]) => `
          <div class="maintenance-summary-row">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(String(value))}</strong>
          </div>
        `).join("")}
      </div>
    `;
  };
  const applyReextractStripGeometry = (img) => {
    const key = img?.dataset?.reextractStripSource;
    if (!key || !img.naturalWidth || !img.naturalHeight || !reviewOverlay?.isConnected) return;
    const escapedKey = CSS.escape(key);
    const sourceFrame = reviewOverlay.querySelector(`[data-reextract-strip-source-frame="${escapedKey}"]`);
    const renderFrames = Array.from(reviewOverlay.querySelectorAll(`[data-reextract-strip-render-frame="${escapedKey}"]`));
    const renderStages = Array.from(reviewOverlay.querySelectorAll(`[data-reextract-strip-render="${escapedKey}"]`));
    if (!sourceFrame || !renderFrames.length || !renderStages.length) return;

    const sw = Number(img.naturalWidth);
    const sh = Number(img.naturalHeight);
    const metricStage = renderStages[0];
    let innerX = Number(metricStage.dataset.innerX);
    let innerY = Number(metricStage.dataset.innerY);
    let innerW = Number(metricStage.dataset.innerW);
    let innerH = Number(metricStage.dataset.innerH);
    if (![innerX, innerY, innerW, innerH].every(Number.isFinite) || innerW <= 0 || innerH <= 0) {
      const n = Number(metricStage.dataset.swatches || 8);
      const borderMm = 3;
      const stepWMm = 12;
      const stepHMm = 20;
      const deskewPad = 6;
      const totalWmm = (2 * borderMm) + (n * stepWMm);
      const plasticWPx = Math.max(1, sw - 2 * deskewPad);
      const pxPerMm = plasticWPx / totalWmm;
      innerX = Math.round(deskewPad + borderMm * pxPerMm);
      innerY = Math.round(deskewPad + borderMm * pxPerMm);
      innerW = Math.round(n * stepWMm * pxPerMm);
      innerH = Math.round(stepHMm * pxPerMm * 0.95);
    } else {
      const sourceW = Number(metricStage.dataset.stripW);
      const sourceH = Number(metricStage.dataset.stripH);
      if (Number.isFinite(sourceW) && sourceW > 0 && Number.isFinite(sourceH) && sourceH > 0) {
        const sx = sw / sourceW;
        const sy = sh / sourceH;
        innerX *= sx;
        innerW *= sx;
        innerY *= sy;
        innerH *= sy;
      }
    }

    renderStages.forEach((stage) => {
      stage.style.setProperty("--render-left", `${(innerX / sw) * 100}%`);
      stage.style.setProperty("--render-top", `${(innerY / sh) * 100}%`);
      stage.style.setProperty("--render-width", `${(innerW / sw) * 100}%`);
      stage.style.setProperty("--render-height", `${(innerH / sh) * 100}%`);
    });
  };
  const bindReextractStripGeometry = () => {
    if (!reviewOverlay?.isConnected) return;
    reviewOverlay.querySelectorAll("img[data-reextract-strip-source]").forEach((img) => {
      if (img.complete && img.naturalWidth) {
        applyReextractStripGeometry(img);
      } else {
        img.addEventListener("load", () => applyReextractStripGeometry(img), { once: true });
      }
    });
  };
  const renderCandidateList = () => {
    if (!state.candidateSetId) return `<div class="mf-placeholder">No extracted images yet.</div>`;
    if (state.loading) return `<div class="maintenance-loading">Loading extracted images...</div>`;
    if (!state.samples.length) return `<div class="mf-placeholder">No extracted images in this run.</div>`;
    return `
      <div class="reextract-list-actions">
        <button class="ghost-button xs" type="button" id="reextractSaveAll" ${state.busy || state.running ? "disabled" : ""}>Save All</button>
        <button class="ghost-button xs" type="button" id="reextractSkipAll" ${state.busy || state.running ? "disabled" : ""}>Skip All</button>
      </div>
      <div class="reextract-candidate-list">
        ${state.samples.map((sample) => {
          const active = sample.sample_id === state.selectedSampleId;
          return `
            <button class="reextract-candidate-row ${active ? "is-active" : ""}" type="button" data-reextract-sample="${_escAttr(sample.sample_id)}">
              <span class="mono">${escapeHtml(sample.sample_id)}</span>
              ${candidateDecisionPill(sample)}
            </button>
          `;
        }).join("")}
      </div>
    `;
  };
  const renderCandidateDetail = () => {
    const sample = selectedSample();
    if (!sample) return `<div class="mf-placeholder">Select a sample.</div>`;
    const canReview = isReviewableCandidate(sample);
    const canRetry = sample.status === "failed" || sample.status === "stale";
    const canManual = state.segmentationMode === "redetect_from_scratch";
    return `
      <div class="reextract-detail">
        <div class="reextract-detail-head">
          <div>
            <strong class="mono">${escapeHtml(sample.sample_id)}</strong>
            <span class="reextract-status is-${_escAttr(sample.status || "unknown")}">${escapeHtml(statusLabel(sample.status))}</span>
          </div>
          <div class="reextract-detail-actions">
            ${canReview ? `
              <button class="ghost-button xs ${candidateDecision(sample) === "save" ? "is-active" : ""}" type="button" data-reextract-decision="save">Save</button>
              <button class="ghost-button xs ${candidateDecision(sample) === "skip" ? "is-active" : ""}" type="button" data-reextract-decision="skip">Skip</button>
            ` : ""}
            ${canRetry ? `<button class="ghost-button xs" type="button" id="reextractRetryCandidate">Retry</button>` : ""}
            ${canManual ? `<button class="ghost-button xs" type="button" id="reextractManualCandidate">Manual Corners</button>` : ""}
          </div>
        </div>
        ${sample.error ? `<div class="backup-restore-message is-error">${escapeHtml(sample.error)}</div>` : ""}
        <div class="reextract-artifact-grid">
          ${candidateArtifactImg(sample, "source", "Source Strip Boundary")}
          ${candidateArtifactImg(sample, "blank", "Blank Strip Boundary")}
        </div>
        <div class="reextract-strip-comparison">
          ${candidateStripReviewRow(sample, sample.artifacts?.transmission_roi ? "transmission_roi" : "strip", "Extracted Transmission", "transmission")}
          ${candidateStripReviewRow(sample, "appearance", "Extracted Appearance", "appearance")}
        </div>
        ${candidateReviewSummaryHtml(sample)}
      </div>
    `;
  };
  async function selectAdjacenterviewSample(delta) {
    if (state.busy || state.running || state.loading || !state.samples.length) return;
    const currentIndex = state.samples.findIndex((sample) => sample.sample_id === state.selectedSampleId);
    const baseIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex = Math.max(0, Math.min(state.samples.length - 1, baseIndex + delta));
    const nextSample = state.samples[nextIndex];
    if (!nextSample || nextSample.sample_id === state.selectedSampleId) return;
    state.selectedSampleId = nextSample.sample_id;
    state.selectedSample = await fetchReextractCandidateSample(state.candidateSetId, state.selectedSampleId);
    renderReviewDialog();
  }
  const renderControls = (disabled) => {
    const modeOption = (group, value, label, active, disabledAttr = "") => `
      <label class="maintenance-mode-option ${active ? "is-active" : ""} ${disabledAttr ? "is-disabled" : ""}">
        <input type="radio" name="${group}" value="${_escAttr(value)}" ${active ? "checked" : ""} ${disabled || disabledAttr ? "disabled" : ""}>
        <span>${escapeHtml(label)}</span>
      </label>
    `;
    const redetectPartial = state.segmentationMode === "redetect_from_scratch";
    const sampleIds = parseReextractSampleIds();
    return `
      <div class="reextract-control-grid">
        <fieldset class="maintenance-mode-fieldset" ${disabled ? "disabled" : ""}>
          <legend>Data</legend>
          <div class="maintenance-mode-options">
            ${modeOption("reextractDomainMode", "complete", "Complete", state.domainMode === "complete")}
            ${modeOption("reextractDomainMode", "transmission_only", "Transmission Only", state.domainMode === "transmission_only", redetectPartial ? "disabled" : "")}
            ${modeOption("reextractDomainMode", "appearance_only", "Appearance Only", state.domainMode === "appearance_only", redetectPartial ? "disabled" : "")}
          </div>
        </fieldset>
        <fieldset class="maintenance-mode-fieldset" ${disabled ? "disabled" : ""}>
          <legend>Coordinates</legend>
          <div class="maintenance-mode-options">
            ${modeOption("reextractSegmentationMode", "existing_coordinates", "Use Accepted Coordinates", state.segmentationMode === "existing_coordinates")}
            ${modeOption("reextractSegmentationMode", "redetect_from_scratch", "Re-detect Strip", state.segmentationMode === "redetect_from_scratch")}
          </div>
        </fieldset>
        <fieldset class="maintenance-mode-fieldset reextract-sample-fieldset" ${disabled ? "disabled" : ""}>
          <legend>Samples</legend>
          <div class="maintenance-mode-options">
            ${modeOption("reextractSampleScope", "all_accepted", "All Accepted", state.sampleScopeMode !== "sample_ids")}
            ${modeOption("reextractSampleScope", "sample_ids", "Selected Samples", state.sampleScopeMode === "sample_ids")}
          </div>
          ${state.sampleScopeMode === "sample_ids" ? `
            <textarea
              id="reextractSampleIds"
              class="reextract-sample-scope-input"
              rows="3"
              placeholder="exp-055, exp-056, exp-165"
              ${disabled ? "disabled" : ""}
            >${escapeHtml(state.sampleIdsText || "")}</textarea>
          ` : ""}
          <p class="small-copy" data-reextract-sample-count>${state.sampleScopeMode === "sample_ids" ? `${sampleIds.length} selected` : "All accepted processed samples."}</p>
        </fieldset>
      </div>
    `;
  };
  async function loadSamples(selectSampleId = "") {
    if (!state.candidateSetId) return;
    state.loading = true;
    renderAll();
    try {
      const [candidateSet, payload] = await Promise.all([
        fetchReextractCandidateSet(state.candidateSetId),
        fetchReextractCandidateSamples(state.candidateSetId),
      ]);
      state.candidateSet = candidateSet || null;
      state.samples = payload?.samples || [];
      const sampleIds = new Set(state.samples.map((sample) => sample.sample_id));
      const preferredSampleId = selectSampleId || state.selectedSampleId || "";
      state.selectedSampleId = sampleIds.has(preferredSampleId)
        ? preferredSampleId
        : (state.samples[0]?.sample_id || "");
      state.selectedSample = state.selectedSampleId
        ? await fetchReextractCandidateSample(state.candidateSetId, state.selectedSampleId)
        : null;
    } finally {
      state.loading = false;
      renderAll();
    }
  }
  async function withBusy(fn) {
    if (state.busy) return;
    state.busy = true;
    state.error = "";
    renderAll();
    try {
      await fn();
    } catch (err) {
      state.error = err.message || String(err || "Re-extraction failed");
    } finally {
      state.busy = false;
      renderAll();
    }
  }
  async function runReextractJob(kind, startFn) {
    if (state.busy || state.running) return;
    state.busy = true;
    state.running = true;
    state.cancelling = false;
    state.job = null;
    state.jobKind = kind;
    state.error = "";
    renderAll();
    try {
      const started = await startFn();
      const jobId = started?.job_id;
      if (!jobId) throw new Error("Re-extraction job did not return a job id.");
      state.job = started;
      state.cancelling = started.status === "cancelling" || Boolean(started.cancel_requested);
      renderAll();
      const nextJob = await pollJobUntilTerminal({
        jobId,
        fetchStatus: () => fetchReextractJobStatus(jobId),
        isTerminal: terminalJobStatus,
        shouldContinue: () => overlay.isConnected && state.running && state.jobKind === kind,
        intervalMs: 700,
        onStatus: (job) => {
          state.job = job;
          state.cancelling = job.status === "cancelling" || Boolean(job.cancel_requested);
          renderAll();
        },
        onTransientError: () => {
          state.job = {
            ...(state.job || {}),
            job_id: jobId,
            message: "Connection interrupted; retrying re-extraction status...",
          };
          renderAll();
        },
      });
      if (!nextJob) return;
      await handleTerminalReextractJob(kind, nextJob);
    } catch (err) {
      const recovered = await recoverReextractCandidateSetAfterJobLoss(kind).catch(() => false);
      state.error = recovered
        ? "The job status was lost, but the extracted images were recovered."
        : (err.message || String(err || "Re-extraction failed"));
      state.busy = false;
      state.running = false;
      state.cancelling = false;
      renderAll();
    }
  }
  async function handleTerminalReextractJob(kind, job) {
    const result = job.result || {};
    const status = String(job.status || "");
    const succeeded = status === "succeeded";
    if (kind === "preflight" && succeeded) {
      state.preflight = result.preflight || null;
      state.preflightExpanded = true;
    }
    if (kind === "generate") {
      state.generationReport = result.report || state.generationReport || null;
      state.candidateSetId = result.candidate_set_id || job.candidate_set_id || state.generationReport?.candidate_set_id || state.candidateSetId || "";
      if (succeeded) {
        state.preflightExpanded = false;
        state.candidateSummaryExpanded = true;
      }
      if (state.candidateSetId) await loadSamples(state.selectedSampleId);
    }
    if (kind === "apply") {
      state.applyReport = result.report || state.applyReport || null;
      const candidateSetDeleted = Boolean(state.applyReport?.candidate_set_deleted);
      if (succeeded) {
        state.candidateSummaryExpanded = false;
      }
      if (succeeded && candidateSetDeleted) {
        closeReviewDialog();
        state.candidateSetId = "";
        state.candidateSet = null;
        state.samples = [];
        state.selectedSample = null;
        state.selectedSampleId = "";
      } else if (state.candidateSetId) {
        await loadSamples(state.selectedSampleId);
      }
      if (succeeded) {
        await handleRefresh({ ensureAssets: false });
        await onComplete?.();
      }
    }
    if (kind === "retry" || kind === "manual") {
      const sampleId = result.sample_id || job.sample_id || state.selectedSampleId;
      if (state.candidateSetId) await loadSamples(sampleId);
    }
    if (!succeeded) {
      state.error = job.error?.message || job.message || `Re-extraction ${status || "failed"}`;
    } else {
      state.error = "";
    }
    state.busy = false;
    state.running = false;
    state.cancelling = false;
    renderAll();
  }
  async function recoverReextractCandidateSetAfterJobLoss(kind) {
    const knownId = state.job?.candidate_set_id || state.job?.progress?.candidate_set_id || state.candidateSetId;
    if (knownId) {
      state.candidateSetId = knownId;
      await loadSamples(state.selectedSampleId);
      return true;
    }
    if (kind !== "generate") return false;
    const payload = await fetchReextractCandidateSets();
    const candidateSet = (payload?.candidate_sets || [])[0] || null;
    if (!candidateSet?.candidate_set_id) return false;
    state.candidateSetId = candidateSet.candidate_set_id;
    state.candidateSet = candidateSet;
    if (!state.generationReport) {
      state.generationReport = {
        status: candidateSet.status || "unknown",
        summary: candidateSet.counts_by_status || {},
      };
    }
    await loadSamples(state.selectedSampleId);
    return true;
  }
  async function cancelActiveReextractJob() {
    if (!state.job?.job_id || !jobIsActive(state.job)) return;
    const cancellationJobId = String(state.job.job_id);
    try {
      state.cancelling = true;
      const response = await cancelReextractJob(cancellationJobId);
      if (String(state.job?.job_id || "") !== cancellationJobId) return;
      assertPolledJobIdentity(response, cancellationJobId);
      state.job = response;
      renderAll();
    } catch (err) {
      if (String(state.job?.job_id || "") !== cancellationJobId) return;
      state.cancelling = state.job?.status === "cancelling" || Boolean(state.job?.cancel_requested);
      state.error = err.message || "Cancel request failed";
      renderAll();
    }
  }
  function confirmDeleteCandidateSetDialog() {
    return new Promise((resolve) => {
      const confirmOverlay = document.createElement("div");
      confirmOverlay.className = "info-dialog-overlay maintenance-clear-confirm-overlay";
      confirmOverlay.innerHTML = `
        <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="reextractDeleteConfirmTitle">
          ${renderDialogHeader({
            title: "Discard Results",
            titleId: "reextractDeleteConfirmTitle",
            closeButtonHtml: renderWindowCloseButton({ id: "reextractDeleteConfirmClose", className: "info-dialog-close" }),
          })}
          <div class="info-dialog-body">
            <div class="maintenance-detail-body">
              <p>Discard the staged re-extraction results?</p>
              <p class="small-copy">Accepted sample data will not be changed.</p>
              ${state.candidateSetId ? `<div class="backup-restore-path mono">${escapeHtml(state.candidateSetId)}</div>` : ""}
            </div>
          </div>
          <div class="info-dialog-footer">
            <button class="delete-button small" type="button" id="reextractDeleteConfirm">Discard Results</button>
            <button class="ghost-button small" type="button" id="reextractDeleteCancel">Cancel</button>
          </div>
        </div>
      `;
      const cleanup = (confirmed) => {
        confirmOverlay.remove();
        document.removeEventListener("keydown", handleKeydown);
        resolve(Boolean(confirmed));
      };
      const handleKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      confirmOverlay.querySelector("#reextractDeleteConfirm")?.addEventListener("click", () => cleanup(true));
      confirmOverlay.querySelector("#reextractDeleteCancel")?.addEventListener("click", () => cleanup(false));
      confirmOverlay.querySelector("#reextractDeleteConfirmClose")?.addEventListener("click", () => cleanup(false));
      document.addEventListener("keydown", handleKeydown);
      document.body.appendChild(confirmOverlay);
    });
  }
  function confirmSaveCandidateSetDialog() {
    return new Promise((resolve) => {
      const confirmOverlay = document.createElement("div");
      confirmOverlay.className = "info-dialog-overlay maintenance-clear-confirm-overlay";
      const r = readiness();
      confirmOverlay.innerHTML = `
        <div class="info-dialog" role="dialog" aria-modal="true" aria-labeledby="reextractSaveConfirmTitle">
          ${renderDialogHeader({
            title: "Save Results",
            titleId: "reextractSaveConfirmTitle",
            closeButtonHtml: renderWindowCloseButton({ id: "reextractSaveConfirmClose", className: "info-dialog-close" }),
          })}
          <div class="info-dialog-body">
            <div class="maintenance-detail-body">
              <p>Save staged re-extraction results for samples marked Save?</p>
              <div class="maintenance-summary-grid">
                <div class="maintenance-summary-row"><span>Save</span><strong>${escapeHtml(String(r.save_count || savedReadySamples().length || 0))}</strong></div>
                <div class="maintenance-summary-row"><span>Skip</span><strong>${escapeHtml(String(r.skip_count || 0))}</strong></div>
                <div class="maintenance-summary-row"><span>Failed</span><strong>${escapeHtml(String(r.failed_count || 0))}</strong></div>
                <div class="maintenance-summary-row"><span>Blocked</span><strong>${escapeHtml(String(r.blocked_count || 0))}</strong></div>
              </div>
              <p class="small-copy">Skipped rows will not change accepted sample data.</p>
            </div>
          </div>
          <div class="info-dialog-footer">
            <button class="primary-button small" type="button" id="reextractSaveConfirm">Save Results</button>
            <button class="ghost-button small" type="button" id="reextractSaveCancel">Cancel</button>
          </div>
        </div>
      `;
      const cleanup = (confirmed) => {
        confirmOverlay.remove();
        document.removeEventListener("keydown", handleKeydown);
        resolve(Boolean(confirmed));
      };
      const handleKeydown = (event) => {
        if (event.key === "Escape") cleanup(false);
      };
      confirmOverlay.querySelector("#reextractSaveConfirm")?.addEventListener("click", () => cleanup(true));
      confirmOverlay.querySelector("#reextractSaveCancel")?.addEventListener("click", () => cleanup(false));
      confirmOverlay.querySelector("#reextractSaveConfirmClose")?.addEventListener("click", () => cleanup(false));
      document.addEventListener("keydown", handleKeydown);
      document.body.appendChild(confirmOverlay);
    });
  }
  function candidateSetSummaryHtml() {
    if (!state.candidateSetId) return "";
    const summary = state.generationReport?.summary || {};
    const manualPending = hasManualPending();
    const actionLabel = manualPending ? "Process Manual Samples" : "Review Results";
    const actionId = manualPending ? "reextractOpenManualStep" : "reextractOpenReview";
    return `
      <section class="maintenance-workflow-section">
        ${workflowSectionCap("Extracted Images", state.candidateSummaryExpanded, "reextractToggleCandidateSummary", Boolean(state.applyReport))}
        ${state.candidateSummaryExpanded ? `
          <div class="maintenance-detail-body">
            <div class="backup-restore-message is-success">Images extracted. Review them before saving staged results.</div>
            <div class="backup-restore-path mono">${escapeHtml(String(state.candidateSetId))}</div>
            ${reextractCandidateSummaryHtml(summary)}
            <div class="backup-workflow-actions">
              <button class="primary-button small" type="button" id="${actionId}" ${state.busy || state.running ? "disabled" : ""}>${actionLabel}</button>
            </div>
          </div>
        ` : ""}
      </section>
    `;
  }
  function closeReviewDialog() {
    if (!reviewOverlay) return;
    reviewOverlay.remove();
    reviewOverlay = null;
    state.reviewDialogMode = "";
  }
  const manualPendingRows = () => state.samples.filter((sample) => sample.status === "manual_required");
  function renderManualStepDialog() {
    if (state.reviewDialogMode !== "manual") return;
    if (!reviewOverlay?.isConnected) return;
    const rows = manualPendingRows();
    const disableClose = state.running;
    reviewOverlay.innerHTML = `
      <div class="info-dialog maintenance-workflow-dialog reextract-review-dialog" role="dialog" aria-modal="true" aria-labeledby="reextractManualTitle">
        ${renderDialogHeader({
          title: "Process Manual Samples",
          titleId: "reextractManualTitle",
          closeButtonHtml: renderWindowCloseButton({
            id: "reextractManualClose",
            className: "info-dialog-close",
            disabled: disableClose,
          }),
        })}
        <div class="info-dialog-body reextract-review-dialog-body">
          ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
          <section class="maintenance-workflow-section reextract-review-section">
            <div class="drawer-module-cap">
              <span class="sidebar-label">Samples</span>
              <span>${rows.length} manual</span>
            </div>
            <div class="maintenance-detail-body">
              <p class="small-copy">These samples need manual corners before the re-extraction results can be reviewed.</p>
              <div class="reextract-candidate-list">
                ${rows.length ? rows.map((sample) => `
                  <button class="reextract-candidate-row" type="button" data-reextract-manual-sample="${_escAttr(sample.sample_id)}">
                    <span class="mono">${escapeHtml(sample.sample_id)}</span>
                    ${candidateDecisionPill(sample)}
                  </button>
                `).join("") : `<div class="mf-placeholder">No manual samples remain.</div>`}
              </div>
            </div>
          </section>
        </div>
        <div class="info-dialog-footer backup-workflow-footer">
          <button class="ghost-button small" type="button" id="reextractManualCloseFooter" ${disableClose ? "disabled" : ""}>Close</button>
          <button class="primary-button small" type="button" id="reextractManualReviewResults" ${!finalReviewReady() ? "disabled" : ""}>Review Results</button>
        </div>
      </div>
    `;
    bindManualStepDialog();
  }
  async function openManualStepDialog() {
    if (!state.candidateSetId) return;
    if (!state.samples.length && !state.loading) {
      await loadSamples(state.selectedSampleId);
    }
    if (!reviewOverlay?.isConnected) {
      reviewOverlay = document.createElement("div");
      reviewOverlay.className = "info-dialog-overlay maintenance-workflow-overlay reextract-review-overlay";
      document.body.appendChild(reviewOverlay);
    }
    state.reviewDialogMode = "manual";
    renderManualStepDialog();
  }
  function bindManualStepDialog() {
    if (!reviewOverlay?.isConnected) return;
    reviewOverlay.querySelector("#reextractManualClose")?.addEventListener("click", closeReviewDialog);
    reviewOverlay.querySelector("#reextractManualCloseFooter")?.addEventListener("click", closeReviewDialog);
    reviewOverlay.querySelector("#reextractManualReviewResults")?.addEventListener("click", () => {
      void openCandidateReviewDialog();
    });
    reviewOverlay.querySelectorAll("[data-reextract-manual-sample]").forEach((button) => {
      button.addEventListener("click", () => {
        const sampleId = button.dataset.reextractManualSample || "";
        if (!sampleId) return;
        openManualProcessing([sampleId], {
          context: "reextract-candidate",
          candidateSetId: state.candidateSetId,
          onCandidateComplete: async () => {
            await loadSamples(sampleId);
            renderManualStepDialog();
          },
        });
      });
    });
  }
  function renderReviewDialog() {
    if (state.reviewDialogMode !== "review") return;
    if (!reviewOverlay?.isConnected) return;
    const activeJob = jobIsActive(state.job);
    const disableClose = state.running;
    const canSave = state.candidateSetId && saveReady() && !state.busy && !state.running && !state.loading;
    const pendingCount = Number(readiness().pending_decision_count || pendingReadySamples().length || 0);
    const applyStatus = String(state.applyReport?.status || "");
    const applyMessage = reextractApplyResultMessage(applyStatus);
    reviewOverlay.innerHTML = `
      <div class="info-dialog maintenance-workflow-dialog reextract-review-dialog" role="dialog" aria-modal="true" aria-labeledby="reextractReviewTitle">
        ${renderDialogHeader({
          title: "Review Extracted Images",
          titleId: "reextractReviewTitle",
          closeButtonHtml: renderWindowCloseButton({
            id: "reextractReviewClose",
            className: "info-dialog-close",
            disabled: disableClose,
          }),
        })}
        <div class="info-dialog-body reextract-review-dialog-body">
          ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
          ${pendingCount ? `<div class="backup-restore-message is-warning">Choose Save or Skip for ${pendingCount} pending sample${pendingCount === 1 ? "" : "s"} before saving results.</div>` : ""}
          ${reextractProgressHtml(state.job, activeJob, "Running re-extraction")}
          ${state.applyReport ? `
            <section class="maintenance-workflow-section">
              <div class="drawer-module-cap"><span class="sidebar-label">Apply Result</span></div>
              <div class="maintenance-detail-body">
                <div class="backup-restore-message ${applyStatus === "completed" ? "is-success" : "is-warning"}">${escapeHtml(applyMessage)}</div>
                ${maintenanceSummaryHtml(state.applyReport.summary || {})}
                ${maintenanceFindingsHtml(state.applyReport)}
              </div>
            </section>
          ` : ""}
          <section class="maintenance-workflow-section reextract-review-section">
            <div class="reextract-review-body">
              <aside class="reextract-review-list-panel">
                <div class="drawer-module-cap">
                  <span class="sidebar-label">Samples</span>
                  <span>${escapeHtml(String(state.samples.length))}</span>
                </div>
                <div class="reextract-review-panel-body">
                  ${renderCandidateList()}
                </div>
              </aside>
              <div class="reextract-review-detail-panel">
                <div class="drawer-module-cap">
                  <span class="sidebar-label">Sample Detail</span>
                  <span>${escapeHtml(String(state.candidateSetId || ""))}</span>
                </div>
                <div class="reextract-review-panel-body">
                  ${renderCandidateDetail()}
                </div>
              </div>
            </div>
          </section>
        </div>
        <div class="info-dialog-footer backup-workflow-footer">
          <button class="delete-button small" type="button" id="reextractReviewDeleteSet" ${state.busy || state.running ? "disabled" : ""}>Discard Results</button>
          ${activeJob ? `<button class="ghost-button small" type="button" id="reextractCancelReviewJob" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
          <span class="dialog-footer-spacer"></span>
          <button class="ghost-button small" type="button" id="reextractReviewCloseFooter" ${disableClose ? "disabled" : ""}>Close</button>
          <button class="primary-button small" type="button" id="reextractReviewApply" ${!canSave ? "disabled" : ""}>Save Results</button>
        </div>
      </div>
    `;
    bindReviewDialog();
  }
  async function openCandidateReviewDialog() {
    if (!state.candidateSetId) return;
    if (!finalReviewReady()) {
      state.error = hasManualPending()
        ? "Process manual samples before reviewing results."
        : "Extracted images are not ready for final review.";
      renderAll();
      return;
    }
    if (!reviewOverlay?.isConnected) {
      reviewOverlay = document.createElement("div");
      reviewOverlay.className = "info-dialog-overlay maintenance-workflow-overlay reextract-review-overlay";
      document.body.appendChild(reviewOverlay);
    }
    state.reviewDialogMode = "review";
    renderReviewDialog();
    if (!state.samples.length && !state.loading) {
      await loadSamples(state.selectedSampleId);
    }
  }
  function bindReviewDialog() {
    if (!reviewOverlay?.isConnected) return;
    bindReextractStripGeometry();
    bindDrawerLightboxButtons(reviewOverlay);
    reviewOverlay.tabIndex = -1;
    reviewOverlay.onkeydown = (event) => {
      const targetTag = String(event.target?.tagName || "").toLowerCase();
      if (["input", "textarea", "select"].includes(targetTag) || event.target?.isContentEditable) return;
      if (event.key === "ArrowDown" || event.key === "ArrowRight") {
        event.preventDefault();
        void selectAdjacenterviewSample(1);
      } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
        event.preventDefault();
        void selectAdjacenterviewSample(-1);
      }
    };
    if (!reviewOverlay.contains(document.activeElement)) {
      reviewOverlay.focus({ preventScroll: true });
    }
    reviewOverlay.querySelector("#reextractReviewClose")?.addEventListener("click", closeReviewDialog);
    reviewOverlay.querySelector("#reextractReviewCloseFooter")?.addEventListener("click", closeReviewDialog);
    reviewOverlay.querySelector("#reextractCancelReviewJob")?.addEventListener("click", cancelActiveReextractJob);
    reviewOverlay.querySelector("#reextractSaveAll")?.addEventListener("click", () => withBusy(async () => {
      await setReextractCandidateDecisionBulk(state.candidateSetId, "save");
      await loadSamples(state.selectedSampleId);
    }));
    reviewOverlay.querySelector("#reextractSkipAll")?.addEventListener("click", () => withBusy(async () => {
      await setReextractCandidateDecisionBulk(state.candidateSetId, "skip");
      await loadSamples(state.selectedSampleId);
    }));
    reviewOverlay.querySelectorAll("[data-reextract-sample]").forEach((button) => {
      button.addEventListener("click", () => withBusy(async () => {
        state.selectedSampleId = button.dataset.reextractSample || "";
        state.selectedSample = await fetchReextractCandidateSample(state.candidateSetId, state.selectedSampleId);
      }));
    });
    reviewOverlay.querySelectorAll("[data-reextract-decision]").forEach((button) => {
      button.addEventListener("click", () => withBusy(async () => {
        const sample = selectedSample();
        if (!sample) return;
        const payload = await setReextractCandidateDecision(state.candidateSetId, sample.sample_id, button.dataset.reextractDecision);
        state.selectedSample = payload?.candidate || null;
        await loadSamples(sample.sample_id);
      }));
    });
    reviewOverlay.querySelector("#reextractRetryCandidate")?.addEventListener("click", async () => {
      const sample = selectedSample();
      if (!sample) return;
      await runReextractJob("retry", () => startRetryReextractCandidateJob(state.candidateSetId, sample.sample_id));
    });
    reviewOverlay.querySelector("#reextractManualCandidate")?.addEventListener("click", () => {
      const sample = selectedSample();
      if (!sample) return;
      openManualProcessing([sample.sample_id], {
        context: "reextract-candidate",
        candidateSetId: state.candidateSetId,
        onCandidateComplete: async () => {
          await loadSamples(sample.sample_id);
        },
      });
    });
    reviewOverlay.querySelector("#reextractReviewApply")?.addEventListener("click", async () => {
      const confirmed = await confirmSaveCandidateSetDialog();
      if (!confirmed) return;
      await runReextractJob("apply", () => startApplyReextractCandidateSetJob(state.candidateSetId));
    });
    reviewOverlay.querySelector("#reextractReviewDeleteSet")?.addEventListener("click", async () => {
      const confirmed = await confirmDeleteCandidateSetDialog();
      if (!confirmed) return;
      await withBusy(async () => {
        await deleteReextractCandidateSet(state.candidateSetId);
        closeReviewDialog();
        state.candidateSetId = "";
        state.samples = [];
        state.selectedSample = null;
        state.selectedSampleId = "";
        state.generationReport = null;
        state.applyReport = null;
        state.preflightExpanded = true;
        state.candidateSummaryExpanded = true;
        await onComplete?.();
      });
    });
  }
  function close() {
    if (state.running) return;
    closeReviewDialog();
    overlay.remove();
  }
  function render() {
    const activeJob = jobIsActive(state.job);
    const disableClose = state.running;
    const canGenerate = state.preflight?.enabled !== false && state.preflight && !state.busy && !state.running;
    const showPreflightAction = !state.preflight && !state.candidateSetId;
    const showGenerateAction = Boolean(state.preflight) && !state.candidateSetId;
    const showInitialActionRow = showPreflightAction || (activeJob && !state.preflight);
    const showPostPreflightActionRow = Boolean(state.preflight) && (showGenerateAction || activeJob);
    const applyStatus = String(state.applyReport?.status || "");
    const applyMessage = reextractApplyResultMessage(applyStatus);
    overlay.innerHTML = `
      <div class="info-dialog maintenance-workflow-dialog reextract-workflow-dialog" role="dialog" aria-modal="true" aria-labeledby="reextractWorkflowTitle">
        ${renderDialogHeader({
          title: operation.name || "Re-extract Sample Images",
          titleId: "reextractWorkflowTitle",
          closeButtonHtml: renderWindowCloseButton({
            id: "reextractWorkflowClose",
            className: "info-dialog-close",
            disabled: disableClose,
          }),
        })}
        <div class="info-dialog-body maintenance-workflow-body reextract-workflow-body">
          ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
          <section class="maintenance-workflow-operation">
            <div class="maintenance-detail-body">
              <p>${escapeHtml(reextractWorkflowDescription)}</p>
              ${renderControls(Boolean(state.candidateSetId || state.busy))}
              <div class="maintenance-workflow-tags">
                <span>${escapeHtml(maintenanceRiskLabel(operation.risk_class))}</span>
              </div>
            </div>
          </section>
          ${showInitialActionRow ? `
            <div class="backup-workflow-actions">
              ${showPreflightAction ? `<button class="primary-button small" type="button" id="reextractPreflight" ${state.busy || state.running ? "disabled" : ""}>Run Preflight</button>` : ""}
              ${activeJob ? `<button class="ghost-button small" type="button" id="reextractCancelJob" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
            </div>
          ` : ""}
          ${state.preflight ? `
            <section class="maintenance-workflow-section">
              ${workflowSectionCap("Preflight", state.preflightExpanded, "reextractTogglePreflight", Boolean(state.candidateSetId || state.generationReport || state.applyReport))}
              ${state.preflightExpanded ? `
                <div class="maintenance-detail-body">
                  ${reextractSummaryRowsHtml(state.preflight.summary || {})}
                  ${reextractManualNoticeHtml(reextractManualFindings(state.preflight))}
                  ${maintenanceFindingsHtml({ blocked: reextractBlockedFindings(state.preflight) })}
                </div>
              ` : ""}
            </section>
          ` : ""}
          ${showPostPreflightActionRow ? `
            <div class="backup-workflow-actions">
              ${showGenerateAction ? `<button class="primary-button small" type="button" id="reextractGenerate" ${!canGenerate ? "disabled" : ""}>Extract Images</button>` : ""}
              ${activeJob ? `<button class="ghost-button small" type="button" id="reextractCancelJob" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
            </div>
          ` : ""}
          ${reextractProgressHtml(state.job, activeJob, "Running re-extraction")}
          ${candidateSetSummaryHtml()}
          ${state.applyReport ? `
            <section class="maintenance-workflow-section">
              <div class="drawer-module-cap"><span class="sidebar-label">Apply Result</span></div>
              <div class="maintenance-detail-body">
                <div class="backup-restore-message ${applyStatus === "completed" ? "is-success" : "is-warning"}">${escapeHtml(applyMessage)}</div>
                ${maintenanceSummaryHtml(state.applyReport.summary || {})}
                ${maintenanceFindingsHtml(state.applyReport)}
              </div>
            </section>
          ` : ""}
        </div>
      </div>
    `;
    bind();
  }
  function bind() {
    overlay.querySelector("#reextractWorkflowClose")?.addEventListener("click", close);
    overlay.querySelector("#reextractCancelJob")?.addEventListener("click", cancelActiveReextractJob);
    overlay.querySelectorAll("input[name='reextractDomainMode']").forEach((input) => {
      input.addEventListener("change", (event) => {
        if (state.candidateSetId || state.busy) return;
        state.domainMode = event.target.value;
        clearPreflightState();
        render();
      });
    });
    overlay.querySelectorAll("input[name='reextractSegmentationMode']").forEach((input) => {
      input.addEventListener("change", (event) => {
        if (state.candidateSetId || state.busy) return;
        state.segmentationMode = event.target.value;
        if (state.segmentationMode === "redetect_from_scratch") state.domainMode = "complete";
        clearPreflightState();
        render();
      });
    });
    overlay.querySelectorAll("input[name='reextractSampleScope']").forEach((input) => {
      input.addEventListener("change", (event) => {
        if (state.candidateSetId || state.busy) return;
        state.sampleScopeMode = event.target.value === "sample_ids" ? "sample_ids" : "all_accepted";
        clearPreflightState();
        render();
      });
    });
    overlay.querySelector("#reextractSampleIds")?.addEventListener("input", (event) => {
      if (state.candidateSetId || state.busy) return;
      state.sampleIdsText = event.target.value;
      if (state.preflight || state.generationReport || state.applyReport) {
        clearPreflightState();
        render();
        return;
      }
      const countNode = overlay.querySelector("[data-reextract-sample-count]");
      if (countNode && state.sampleScopeMode === "sample_ids") {
        countNode.textContent = `${parseReextractSampleIds().length} selected`;
      }
    });
    overlay.querySelector("#reextractPreflight")?.addEventListener("click", () => runReextractJob(
      "preflight",
      () => startReextractPreflightJob(scopePayload()),
    ));
    overlay.querySelector("#reextractGenerate")?.addEventListener("click", () => runReextractJob(
      "generate",
      () => startReextractCandidateSetJob(scopePayload(), state.preflight),
    ));
    overlay.querySelector("#reextractOpenReview")?.addEventListener("click", () => {
      void openCandidateReviewDialog();
    });
    overlay.querySelector("#reextractOpenManualStep")?.addEventListener("click", () => {
      void openManualStepDialog();
    });
    overlay.querySelector("#reextractTogglePreflight")?.addEventListener("click", () => {
      state.preflightExpanded = !state.preflightExpanded;
      render();
    });
    overlay.querySelector("#reextractToggleCandidateSummary")?.addEventListener("click", () => {
      state.candidateSummaryExpanded = !state.candidateSummaryExpanded;
      render();
    });
  }
  function renderAll() {
    render();
    if (state.reviewDialogMode === "manual") {
      renderManualStepDialog();
    } else if (state.reviewDialogMode === "review") {
      renderReviewDialog();
    }
  }
  document.body.appendChild(overlay);
  render();
}

function showMaintenanceWorkflow(operation, onComplete, options = {}) {
  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay maintenance-workflow-overlay";
  const workflowOptions = options || {};
  const modes = operation.modes || [];
  const hasModeChoice = modes.length > 1;
  const defaultMode = operation.default_mode || modes[0] || "audit";
  const isExportGeometryWorkflow = operation.operation_id === "export_geometry_files";
  const isModelFitWorkflow = operation.operation_id === "refit_calibration_models";
  const initialExportGeometryScope = workflowOptions.exportGeometryScope === "all_geometries"
    ? "all_geometries"
    : "used_by_samples";
  const state = {
    busy: false,
    preflighting: false,
    running: false,
    operation,
    mode: defaultMode,
    preflight: null,
    preflightToken: "",
    preflightExpanded: true,
    job: null,
    result: null,
    error: "",
    exportGeometryScope: initialExportGeometryScope,
    exportOutputTypes: new Set(["step", "stl"]),
    forceCameraTransform: Boolean(workflowOptions.forceCameraTransform),
    confirmation: "",
  };

  const close = () => {
    if (state.running) return;
    overlay.remove();
  };

  function exportScopePayload() {
    if (isModelFitWorkflow) {
      return {
        force_camera_transform: Boolean(state.forceCameraTransform),
      };
    }
    if (!isExportGeometryWorkflow) return {};
    return {
      geometry_scope: state.exportGeometryScope,
      output_types: ["step", "stl"].filter((type) => state.exportOutputTypes.has(type)),
    };
  }

  function requiredConfirmation() {
    if (!isExportGeometryWorkflow) return "";
    return state.preflight?.required_confirmation || state.preflight?.summary?.required_confirmation || "";
  }

  function confirmationMatches() {
    const phrase = requiredConfirmation();
    if (!phrase) return true;
    return normalizeRestoreConfirmation(state.confirmation) === normalizeRestoreConfirmation(phrase);
  }

  function canRun() {
    return !state.busy
      && state.preflight?.enabled !== false
      && operation.enabled !== false
      && !state.result
      && confirmationMatches();
  }

  function cancellationRequested() {
    return state.job?.status === "cancelling" || Boolean(state.job?.cancel_requested);
  }

  function cancellationControlHtml() {
    if (!state.running || !state.job?.cancellable) return "";
    const requested = cancellationRequested();
    if (!requested && !state.job?.cancel_available) return "";
    return `<button class="ghost-button small" type="button" id="maintenanceWorkflowCancelJob" ${requested ? "disabled" : ""}>${requested ? "Cancelling..." : "Cancel"}</button>`;
  }

  function modeSelectionHtml(disabled = false) {
    if (!hasModeChoice) return "";
    const help = maintenanceModeHelp(operation);
    return `
      <fieldset class="maintenance-mode-fieldset" ${disabled ? "disabled" : ""}>
        <legend>Mode</legend>
        <div class="maintenance-mode-options">
          ${modes.map((item) => `
            <label class="maintenance-mode-option ${item === state.mode ? "is-active" : ""}">
              <input type="radio" name="maintenanceWorkflowMode" value="${escapeHtml(item)}" ${item === state.mode ? "checked" : ""}>
              <span>${escapeHtml(maintenanceModeLabel(item, operation))}</span>
            </label>
          `).join("")}
        </div>
        ${help ? `<p class="maintenance-detail-meta">${escapeHtml(help)}</p>` : ""}
      </fieldset>
    `;
  }

  function exportControlsHtml(disabled = false) {
    if (!isExportGeometryWorkflow) return "";
    const typeChecked = (type) => state.exportOutputTypes.has(type) ? "checked" : "";
    return `
      <fieldset class="maintenance-mode-fieldset maintenance-export-fieldset" ${disabled ? "disabled" : ""}>
        <legend>Export Options</legend>
        <div class="maintenance-mode-options">
          <label class="maintenance-mode-option ${state.exportGeometryScope === "used_by_samples" ? "is-active" : ""}">
            <input type="radio" name="maintenanceExportGeometryScope" value="used_by_samples" ${state.exportGeometryScope === "used_by_samples" ? "checked" : ""}>
            <span>Only Geometries Used By Samples</span>
          </label>
          <label class="maintenance-mode-option ${state.exportGeometryScope === "all_geometries" ? "is-active" : ""}">
            <input type="radio" name="maintenanceExportGeometryScope" value="all_geometries" ${state.exportGeometryScope === "all_geometries" ? "checked" : ""}>
            <span>All Geometries</span>
          </label>
        </div>
        <div class="maintenance-export-output-types" role="group" aria-label="Geometry export file types">
          <label class="maintenance-export-type ${state.exportOutputTypes.has("step") ? "is-active" : ""}">
            <input type="checkbox" name="maintenanceExportOutputType" value="step" ${typeChecked("step")}>
            <span>STEP</span>
          </label>
          <label class="maintenance-export-type ${state.exportOutputTypes.has("stl") ? "is-active" : ""}">
            <input type="checkbox" name="maintenanceExportOutputType" value="stl" ${typeChecked("stl")}>
            <span>STL</span>
          </label>
        </div>
      </fieldset>
    `;
  }

  function modelFitControlsHtml(disabled = false) {
    if (!isModelFitWorkflow) return "";
    return `
      <fieldset class="maintenance-mode-fieldset maintenance-model-fit-fieldset" ${disabled ? "disabled" : ""}>
        <legend>Fit Options</legend>
        <div class="maintenance-export-output-types" role="group" aria-label="Model fit options">
          <label class="maintenance-export-type ${state.forceCameraTransform ? "is-active" : ""}">
            <input type="checkbox" name="maintenanceForceCameraTransform" ${state.forceCameraTransform ? "checked" : ""}>
            <span>Force Camera Transform</span>
          </label>
        </div>
      </fieldset>
    `;
  }

  function exportConfirmationHtml() {
    const phrase = requiredConfirmation();
    if (!phrase) return "";
    return `
      <label class="sample-create-field backup-restore-confirm maintenance-export-confirm">
        <span>Existing public geometry exports will be overwritten. Type this phrase to continue:</span>
        <span class="backup-restore-confirm-phrase">${escapeHtml(phrase)}</span>
        <input type="text" id="maintenanceExportConfirm" value="${escapeHtml(state.confirmation)}" autocomplete="off">
      </label>
    `;
  }

  function bodyHtml() {
    const preflightSummary = state.preflight?.summary || {};
    const resultSummary = state.result?.summary || {};
    const resourceSentence = maintenanceResourceSentence(operation);
    return `
      ${state.preflighting ? `<div class="backup-restore-message">Running preflight...</div>` : ""}
      ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
      <div class="maintenance-workflow-operation">
        <div class="maintenance-detail-body">
          <p>${escapeHtml(operation.description || "")}</p>
          ${resourceSentence ? `<p class="maintenance-detail-meta"><strong>Touches:</strong> ${escapeHtml(resourceSentence)}.</p>` : ""}
          ${modeSelectionHtml(Boolean(state.preflight || state.running || state.result || state.preflighting))}
          ${exportControlsHtml(Boolean(state.preflight || state.running || state.result || state.preflighting))}
          ${modelFitControlsHtml(Boolean(state.preflight || state.running || state.result || state.preflighting))}
          <div class="maintenance-workflow-tags">
            <span>${escapeHtml(maintenanceModeLabel(state.mode, operation))}</span>
            <span>${escapeHtml(maintenanceRiskLabel(operation.risk_class))}</span>
            <span>${escapeHtml(maintenanceWriteLabel(operation))}</span>
            <span>${operation.cancellable ? "Cancelable between items" : "Runs to completion"}</span>
          </div>
        </div>
      </div>
      ${!state.preflight && !state.preflighting && !state.result ? `
        <div class="backup-workflow-actions">
          <button class="primary-button small" type="button" id="maintenanceWorkflowPreflight">Run Preflight</button>
        </div>
      ` : ""}
      ${state.preflight ? `
        <div class="maintenance-workflow-section">
          <div class="drawer-module-cap maintenance-collapsible-cap">
            <span class="sidebar-label">Preflight</span>
            ${state.result ? `
              <div class="drawer-module-cap-actions">
                <button class="drawer-utility-button" type="button" id="maintenanceWorkflowTogglePreflight" aria-expanded="${state.preflightExpanded ? "true" : "false"}">
                  ${state.preflightExpanded ? "Hide" : "Show"}
                </button>
              </div>
            ` : ""}
          </div>
          ${state.preflightExpanded ? `
            <div class="maintenance-detail-body">
              ${maintenanceSummaryHtml(preflightSummary)}
              ${maintenanceWarningsHtml(state.preflight.warnings || [])}
              ${maintenanceFindingsHtml({ blocked: state.preflight.blocked || [] })}
              ${state.preflight.enabled === false ? `<div class="backup-restore-message is-warning">${escapeHtml((state.preflight.blocked || [])[0]?.reason || "This operation is not available.")}</div>` : ""}
            </div>
          ` : ""}
        </div>
      ` : ""}
      ${state.preflight && !state.result ? `
        ${exportConfirmationHtml()}
        <div class="backup-workflow-actions">
          <button class="primary-button small" type="button" id="maintenanceWorkflowRun" ${!canRun() ? "disabled" : ""}>
            ${escapeHtml(maintenanceRunButtonLabel(operation))}
          </button>
          ${cancellationControlHtml()}
        </div>
      ` : ""}
      ${maintenanceProgressHtml(state.job, state.running, "Running maintenance")}
      ${state.result ? `
        <div class="maintenance-workflow-section" id="maintenanceWorkflowResult">
          <div class="drawer-module-cap">
            <span class="sidebar-label">Result</span>
          </div>
          <div class="maintenance-detail-body">
            <div class="backup-restore-message ${state.result.status === "completed" ? "is-success" : "is-warning"}">${escapeHtml(state.result.status === "completed" ? "Maintenance operation complete." : `Maintenance operation ${state.result.status || "finished"}.`)}</div>
            ${maintenanceSummaryHtml(resultSummary)}
            ${maintenanceModelResultsHtml(state.result)}
            ${maintenanceWarningsHtml(state.result.warnings || [])}
            ${state.result.report_path ? `<div class="backup-restore-path mono" title="${escapeHtml(state.result.report_path)}">${escapeHtml(state.result.report_path)}</div>` : ""}
            ${maintenanceFindingsHtml(state.result)}
          </div>
        </div>
      ` : ""}
    `;
  }

  function render() {
    const disableClose = state.running;
    overlay.innerHTML = `
      <div class="info-dialog maintenance-workflow-dialog" role="dialog" aria-modal="true" aria-labeledby="maintenanceWorkflowTitle">
        ${renderDialogHeader({
          title: operation.name || "Maintenance Workflow",
          titleId: "maintenanceWorkflowTitle",
          closeButtonHtml: renderWindowCloseButton({
            id: "maintenanceWorkflowClose",
            className: "info-dialog-close",
            disabled: disableClose,
          }),
        })}
        <div class="info-dialog-body maintenance-workflow-body">
          ${bodyHtml()}
        </div>
      </div>
    `;
    overlay.querySelector("#maintenanceWorkflowClose")?.addEventListener("click", close);
    overlay.querySelectorAll("input[name='maintenanceWorkflowMode']").forEach((input) => {
      input.addEventListener("change", () => {
        if (state.preflight || state.running || state.result || state.preflighting) return;
        state.mode = input.value || defaultMode;
        state.confirmation = "";
        render();
      });
    });
    overlay.querySelectorAll("input[name='maintenanceExportGeometryScope']").forEach((input) => {
      input.addEventListener("change", () => {
        if (state.preflight || state.running || state.result || state.preflighting) return;
        state.exportGeometryScope = input.value === "all_geometries" ? "all_geometries" : "used_by_samples";
        state.confirmation = "";
        render();
      });
    });
    overlay.querySelectorAll("input[name='maintenanceExportOutputType']").forEach((input) => {
      input.addEventListener("change", () => {
        if (state.preflight || state.running || state.result || state.preflighting) return;
        const type = input.value === "stl" ? "stl" : "step";
        if (input.checked) {
          state.exportOutputTypes.add(type);
        } else if (state.exportOutputTypes.size > 1) {
          state.exportOutputTypes.delete(type);
        }
        state.confirmation = "";
        render();
      });
    });
    overlay.querySelector("input[name='maintenanceForceCameraTransform']")?.addEventListener("change", (event) => {
      if (state.preflight || state.running || state.result || state.preflighting) return;
      state.forceCameraTransform = Boolean(event.target.checked);
      render();
    });
    overlay.querySelector("#maintenanceExportConfirm")?.addEventListener("input", (event) => {
      state.confirmation = event.target.value || "";
      const button = overlay.querySelector("#maintenanceWorkflowRun");
      if (button) button.disabled = !canRun();
    });
    overlay.querySelector("#maintenanceWorkflowPreflight")?.addEventListener("click", runPreflight);
    overlay.querySelector("#maintenanceWorkflowRun")?.addEventListener("click", runJob);
    overlay.querySelector("#maintenanceWorkflowTogglePreflight")?.addEventListener("click", () => {
      state.preflightExpanded = !state.preflightExpanded;
      render();
    });
    overlay.querySelector("#maintenanceWorkflowCancelJob")?.addEventListener("click", async () => {
      if (!state.job?.job_id || !state.job.cancel_available) return;
      const cancellationJobId = String(state.job.job_id);
      try {
        const response = await cancelMaintenanceJob(cancellationJobId);
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        assertPolledJobIdentity(response, cancellationJobId);
        state.job = response;
        render();
      } catch (err) {
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        state.error = err.message || "Cancel request failed";
        render();
      }
    });
  }

  function scrollMaintenanceWorkflowToResult() {
    window.setTimeout(() => {
      const body = overlay.querySelector(".maintenance-workflow-body");
      const result = overlay.querySelector("#maintenanceWorkflowResult");
      if (!body || !result) return;
      const bodyBox = body.getBoundingClientRect();
      const resultBox = result.getBoundingClientRect();
      body.scrollTo({
        top: Math.max(0, body.scrollTop + resultBox.top - bodyBox.top - 8),
        behavior: "smooth",
      });
    }, 0);
  }

  async function runJob() {
    if (!canRun()) return;
    state.busy = true;
    state.running = true;
    state.error = "";
    state.job = null;
    render();
    try {
      const job = await startMaintenanceJob(
        operation.operation_id,
        state.mode,
        state.preflightToken,
        exportScopePayload(),
        isExportGeometryWorkflow ? state.confirmation : "",
      );
      state.job = job;
      render();
      const jobId = String(job?.job_id || "");
      if (!jobId) throw new Error("Maintenance job did not return a job id.");
      const nextJob = await pollJobUntilTerminal({
        jobId,
        fetchStatus: () => fetchMaintenanceJobStatus(jobId),
        isTerminal: (status) => ["succeeded", "failed", "cancelled"].includes(String(status.status || "")),
        shouldContinue: () => overlay.isConnected && state.running,
        intervalMs: 700,
        onStatus: (status) => {
          state.job = status;
          render();
        },
        onTransientError: () => {
          state.job = {
            ...(state.job || {}),
            job_id: jobId,
            message: "Connection interrupted; retrying maintenance status...",
          };
          render();
        },
      });
      if (!nextJob) return;
      if (nextJob.status === "succeeded") {
        state.result = nextJob.result || null;
        state.preflightExpanded = false;
        state.error = "";
        state.running = false;
        state.busy = false;
        await applyMaintenanceRefreshImpact(state.result?.ui_refresh || {});
        if (onComplete) await onComplete(state.result);
        showImportToast(`${operation.name || "Maintenance"} complete`, "success");
        render();
        scrollMaintenanceWorkflowToResult();
        return;
      }
      state.result = nextJob.result || null;
      state.preflightExpanded = false;
      state.error = nextJob.status === "failed"
        ? (nextJob.error?.message || nextJob.message || "Maintenance failed")
        : "";
      state.running = false;
      state.busy = false;
      render();
      scrollMaintenanceWorkflowToResult();
    } catch (err) {
      state.error = err.message || "Maintenance job failed";
      state.running = false;
      state.busy = false;
      render();
    }
  }

  async function runPreflight() {
    if (state.preflighting || state.running || state.result) return;
    state.busy = true;
    state.preflighting = true;
    state.error = "";
    state.preflight = null;
    state.preflightToken = "";
    state.preflightExpanded = true;
    state.confirmation = "";
    render();
    try {
      const payload = await preflightMaintenanceOperation(operation.operation_id, state.mode, exportScopePayload());
      state.preflight = payload?.preflight || null;
      state.preflightToken = payload?.preflight_token || "";
      state.preflighting = false;
      state.busy = false;
      render();
    } catch (err) {
      state.error = err.message || "Maintenance preflight failed";
      state.preflighting = false;
      state.busy = false;
      render();
    }
  }

  document.body.appendChild(overlay);
  render();
}

function showStepDeleteDialog(stepId = "") {
  if (isStructuredGeometryBackend()) {
    const step = stepRecordByRef(stepId);
    const label = step?.alias || stepId;
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay";
    overlay.innerHTML = `
      <div class="info-dialog info-dialog-wide" role="dialog" aria-modal="true" aria-labeledby="geometryDeleteTitle">
        ${renderDialogHeader({
          title: "Delete Sample Geometry",
          titleId: "geometryDeleteTitle",
          closeButtonHtml: renderWindowCloseButton({
            className: "info-dialog-close",
            attributes: "data-geometry-delete-close",
          }),
        })}
        <div class="info-dialog-body">
          <p class="info-dialog-lede">Delete <strong>${escapeHtml(label)}</strong> from the Sample Geometry registry?</p>
          <p class="small-copy">This is allowed only when no samples or bundles reference it.</p>
          <div class="sb-validation-error" id="geometryDeleteError"></div>
        </div>
        <div class="info-dialog-footer">
          <button class="delete-button small" id="geometryDeleteConfirm">Delete</button>
          <button class="ghost-button small" id="infoDialogCancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    const cleanup = () => overlay.remove();
    overlay.querySelector("[data-geometry-delete-close]")?.addEventListener("click", cleanup);
    overlay.querySelector("#infoDialogCancel")?.addEventListener("click", cleanup);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) cleanup(); });
    overlay.querySelector("#geometryDeleteConfirm")?.addEventListener("click", async () => {
      const errorEl = overlay.querySelector("#geometryDeleteError");
      try {
        await deleteGeometry(stepId);
        showImportToast(`Deleted ${label}`, "success");
        cleanup();
        selectedRecord = { kind: null, id: null };
        closeDrawer();
        await handleRefresh();
      } catch (err) {
        const msg = err.message || "Delete failed";
        if (errorEl) { errorEl.style.display = "block"; errorEl.textContent = msg; }
        showImportToast(msg, "error");
      }
    });
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "info-dialog-overlay";

  // Highlight the Refresh Data button with a cutout
  const btn = document.getElementById("refreshDataBtn");
  let highlight = null;
  if (btn) {
    const rect = btn.getBoundingClientRect();
    highlight = document.createElement("div");
    highlight.className = "refresh-highlight";
    highlight.style.cssText = `
      position:fixed; top:${rect.top - 4}px; left:${rect.left - 4}px;
      width:${rect.width + 8}px; height:${rect.height + 8}px;
      border-radius:6px; z-index:1001; pointer-events:none;
      box-shadow: 0 0 0 3px #2563eb, 0 0 12px rgba(37,99,235,0.4);
    `;
    document.body.appendChild(highlight);
  }

  overlay.innerHTML = `
    <div class="info-dialog info-dialog-wide" role="dialog" aria-modal="true" aria-labeledby="geometryDeleteFileTitle">
      ${renderDialogHeader({
        title: "Delete Sample Geometry File",
        titleId: "geometryDeleteFileTitle",
        closeButtonHtml: renderWindowCloseButton({
          className: "info-dialog-close",
          attributes: "data-geometry-delete-file-close",
        }),
      })}
      <div class="info-dialog-body">
        <p class="info-dialog-lede">To remove a sample geometry file from the Sample Geometry library:</p>
        <ol class="info-dialog-list">
        <li>Manually delete the unwanted file from the file system</li>
        <li>Click the <strong>Refresh Data</strong> button to update the Sample Geometry library</li>
        </ol>
      </div>
      <div class="info-dialog-footer">
        <button class="primary-button small" id="infoDialogOk">OK</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const cleanup = () => {
    overlay.remove();
    if (highlight) highlight.remove();
  };
  overlay.querySelector("#infoDialogOk").addEventListener("click", cleanup);
  overlay.querySelector("[data-geometry-delete-file-close]").addEventListener("click", cleanup);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) cleanup(); });
}

function showImportToast(message, kind, options = {}) {
  const existing = document.querySelector(".import-toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.className = `import-toast${kind ? " is-" + kind : ""}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), options.durationMs || 2500);
}

async function loadImportData() {
  importState.loading = true;
  importState.loadingMessage = "Scanning image inbox";
  renderImportView();
  try {
    const [images, blanks] = await Promise.all([
      fetchImages().catch(() => []),
      fetchBlanks().catch(() => []),
    ]);
    importState.loadingMessage = "Organizing image assignments";
    renderImportView();
    syncImportStateFromRecords(images || [], blanks || []);
    importState.loaded = true;
  } catch (err) {
    console.warn("[import] Failed to load import data:", err.message);
    importState.loaded = true;
  }
  importState.loading = false;
  importState.loadingMessage = "";
}

function syncImportStateFromRecords(images = [], blanks = []) {
  importState.images = Array.isArray(images) ? images : [];
  for (const img of importState.images) {
    const rot = Number(img.rotation_cw ?? 0) || 0;
    if (rot) {
      data.image_overrides = data.image_overrides || {};
      data.image_overrides[img.filename] = { rotation_cw: rot };
    }
  }
  importState.blanks = Array.isArray(blanks) ? blanks : [];
  buildImageAssignmentMap();

  if (importState.selectedImage && !importState.images.some((img) => img.filename === importState.selectedImage)) {
    importState.selectedImage = null;
  }
  if (importState.selectedBlank && !importState.blanks.some((blank) =>
    blank.blank_id === importState.selectedBlank ||
    blank.filename === importState.selectedBlank ||
    blank.original_filename === importState.selectedBlank
  )) {
    importState.selectedBlank = null;
  }
  if (importState.selectedSample && !data.samples.some((sample) => sample.sample_id === importState.selectedSample)) {
    importState.selectedSample = null;
  }
}

function syncLoadedImportStateFromAppData() {
  if (!importState.loaded) return;
  syncImportStateFromRecords(data.images || [], data.blanks || []);
}

function importInboxSummaryMessage(result) {
  const imported = Array.isArray(result?.imported) ? result.imported.length : 0;
  const skippedItems = Array.isArray(result?.skipped) ? result.skipped : [];
  const movedDuplicates = skippedItems.filter((item) => item?.reason === "already_imported" && item?.removed_path).length;
  const skipped = skippedItems.length;
  const skippedWithoutMovedDuplicates = skipped - movedDuplicates;
  const errors = Array.isArray(result?.errors) ? result.errors.length : 0;
  const managedLocation = result?.managed_storage_path ? ` in ${result.managed_storage_path}` : "";
  if (errors) {
    const duplicateText = movedDuplicates ? `; moved ${movedDuplicates} duplicate${movedDuplicates === 1 ? "" : "s"} to Removed Images` : "";
    return `Imported ${imported}${managedLocation}${duplicateText}; skipped ${skippedWithoutMovedDuplicates}; ${errors} error${errors === 1 ? "" : "s"}`;
  }
  if (imported === 0 && skipped === 0) {
    return "No new inbox images found";
  }
  if (movedDuplicates && skippedWithoutMovedDuplicates) {
    return `Imported ${imported}${managedLocation}; moved ${movedDuplicates} duplicate${movedDuplicates === 1 ? "" : "s"} to Removed Images; skipped ${skippedWithoutMovedDuplicates}`;
  }
  if (movedDuplicates) {
    return `Imported ${imported}${managedLocation}; moved ${movedDuplicates} duplicate${movedDuplicates === 1 ? "" : "s"} to Removed Images`;
  }
  if (skipped) {
    return `Imported ${imported}${managedLocation}; skipped ${skipped} already in Prisma`;
  }
  return `${imported} image${imported === 1 ? " was" : "s were"} successfully imported and moved to managed storage${managedLocation}`;
}

function cleanupUnusedSummaryMessage(result) {
  const removed = Array.isArray(result?.removed) ? result.removed.length : 0;
  const skipped = Array.isArray(result?.skipped) ? result.skipped.length : 0;
  const errors = Array.isArray(result?.errors) ? result.errors.length : 0;
  if (errors) {
    return `Moved ${removed} to Removed Images; skipped ${skipped}; ${errors} error${errors === 1 ? "" : "s"}`;
  }
  if (removed === 0 && skipped === 0) {
    return "No unused images to move";
  }
  if (skipped) {
    return `Moved ${removed} to Removed Images; skipped ${skipped}`;
  }
  return `Moved ${removed} unused image${removed === 1 ? "" : "s"} to Removed Images`;
}

function importJobIsTerminal(status) {
  return ["succeeded", "failed", "cancelled"].includes(String(status || "").toLowerCase());
}

function importProgressHtml(job) {
  const progress = job?.progress || {};
  const percent = Number(progress.percent || 0);
  const current = Number(progress.current_count || 0);
  const total = Number(progress.total_count || 0);
  const message = job?.message || progress.message || "Importing inbox images";
  const filename = progress.filename || "";
  const countText = total ? `${current} / ${total}` : "";
  return `
    <div class="backup-progress import-progress" role="status" aria-live="polite">
      <div class="backup-progress-topline">
        <strong>${escapeHtml(message)}</strong>
        <span>${escapeHtml(countText)}</span>
      </div>
      <div class="backup-progress-bar" aria-hidden="true">
        <div class="backup-progress-fill" style="width: ${Math.max(0, Math.min(100, percent)).toFixed(0)}%;"></div>
      </div>
      <div class="backup-progress-meta">
        <span>${escapeHtml(filename || progress.phase || job?.status || "")}</span>
        <span>${Number.isFinite(percent) ? percent.toFixed(0) : "0"}%</span>
      </div>
    </div>
  `;
}

function importResultSummaryHtml(result) {
  if (!result) return "";
  const imported = Array.isArray(result.imported) ? result.imported.length : 0;
  const skipped = Array.isArray(result.skipped) ? result.skipped.length : 0;
  const errors = Array.isArray(result.errors) ? result.errors.length : 0;
  const duplicateMoves = (result.skipped || []).filter((item) => item?.reason === "already_imported" && item?.removed_path).length;
  const rows = [
    ["Total files", result.total ?? imported + skipped + errors],
    ["Imported", imported],
    ["Duplicates moved", duplicateMoves],
    ["Other skipped", Math.max(0, skipped - duplicateMoves)],
    ["Errors", errors],
  ];
  return `
    <div class="backup-restore-result import-progress-result">
      ${rows.map(([label, value]) => `
        <div class="backup-restore-row">
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("")}
      ${result.session_label ? `
        <div class="backup-restore-row">
          <span>Import session</span>
          <strong>${escapeHtml(result.session_label)}</strong>
        </div>
      ` : ""}
    </div>
  `;
}

function importFailureListHtml(result, jobError) {
  const errors = Array.isArray(result?.errors) ? result.errors : [];
  const errorMessage = jobError?.message || (typeof jobError === "string" ? jobError : "");
  if (!errors.length && !errorMessage) return "";
  const rows = errors.map((item) => `
    <div class="import-progress-error-row">
      <span title="${_escAttr(item.filename || "")}">${escapeHtml(item.filename || "Unknown file")}</span>
      <strong>${escapeHtml(item.error || item.reason || "Import failed")}</strong>
    </div>
  `).join("");
  return `
    <div class="import-progress-errors">
      <h4>${errors.length ? "Files That Need Attention" : "Import Error"}</h4>
      ${errorMessage ? `<p>${escapeHtml(errorMessage)}</p>` : ""}
      ${rows}
    </div>
  `;
}

function importProgressStatusMessage(job, startError) {
  if (startError) {
    return `<div class="backup-restore-message is-error">${escapeHtml(startError)}</div>`;
  }
  const status = String(job?.status || "");
  const result = job?.result || null;
  if (status === "cancelled") {
    return `<div class="backup-restore-message is-warning">Import cancelled. Files already touched by the import were rolled back when possible.</div>`;
  }
  if (status === "failed") {
    const text = result?.errors?.length
      ? "Import could not complete because one or more inbox files failed validation."
      : (job?.error?.message || job?.message || "Inbox import failed.");
    return `<div class="backup-restore-message is-error">${escapeHtml(text)}</div>`;
  }
  if (status === "succeeded") {
    const kind = result?.ok === false || result?.errors?.length ? "is-error" : "is-success";
    return `<div class="backup-restore-message ${kind}">${escapeHtml(importInboxSummaryMessage(result || {}))}</div>`;
  }
  return "";
}

function showImportProgressDialog() {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "info-dialog-overlay import-progress-overlay";
    const state = {
      job: null,
      startError: "",
      actionError: "",
      cancelling: false,
      closed: false,
    };

    const terminal = () => importJobIsTerminal(state.job?.status) || !!state.startError;
    const cleanup = () => {
      if (!terminal()) return;
      state.closed = true;
      document.removeEventListener("keydown", handleKeydown);
      overlay.remove();
      resolve(state.job || { status: "failed", error: { message: state.startError } });
    };
    const requestCancel = async () => {
      if (!state.job?.job_id || terminal() || state.cancelling) return;
      const cancellationJobId = String(state.job.job_id);
      state.cancelling = true;
      state.actionError = "";
      render();
      try {
        const response = await cancelImportInboxImagesJob(cancellationJobId);
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        assertPolledJobIdentity(response, cancellationJobId);
        state.job = response;
      } catch (err) {
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        if (terminal()) return;
        state.actionError = err.message || "Could not cancel import.";
      } finally {
        if (String(state.job?.job_id || "") !== cancellationJobId) return;
        state.cancelling = state.job?.status === "cancelling" || Boolean(state.job?.cancel_requested);
        render();
      }
    };
    const handleKeydown = (event) => {
      if (event.key === "Escape" && terminal()) cleanup();
    };

    const render = () => {
      const isTerminal = terminal();
      const active = !isTerminal;
      const result = state.job?.result || null;
      overlay.innerHTML = `
        <div class="info-dialog import-progress-dialog" role="dialog" aria-modal="true" aria-labeledby="importProgressTitle">
          ${renderDialogHeader({
            title: "Import Images",
            titleId: "importProgressTitle",
            closeButtonHtml: renderWindowCloseButton({
              id: "importProgressClose",
              className: "info-dialog-close",
              disabled: active,
            }),
          })}
          <div class="info-dialog-body import-progress-body">
            ${importProgressStatusMessage(state.job, state.startError)}
            ${state.actionError ? `<div class="backup-restore-message is-error">${escapeHtml(state.actionError)}</div>` : ""}
            ${state.job ? importProgressHtml(state.job) : `<div class="backup-progress import-progress"><div class="backup-progress-topline"><strong>Starting inbox image import</strong><span></span></div><div class="backup-progress-bar" aria-hidden="true"><div class="backup-progress-fill" style="width:0%;"></div></div></div>`}
            ${result ? importResultSummaryHtml(result) : ""}
            ${importFailureListHtml(result, state.job?.error)}
          </div>
          <div class="info-dialog-footer">
            ${active ? `<button class="ghost-button small" type="button" id="importProgressCancel" ${state.cancelling ? "disabled" : ""}>${state.cancelling ? "Cancelling..." : "Cancel"}</button>` : ""}
            <button class="primary-button small" type="button" id="importProgressDone" ${active ? "disabled" : ""}>Close</button>
          </div>
        </div>
      `;
      overlay.querySelector("#importProgressClose")?.addEventListener("click", cleanup);
      overlay.querySelector("#importProgressDone")?.addEventListener("click", cleanup);
      overlay.querySelector("#importProgressCancel")?.addEventListener("click", requestCancel);
    };

    overlay.addEventListener("click", (event) => {
      if (event.target === overlay && terminal()) cleanup();
    });
    document.addEventListener("keydown", handleKeydown);
    document.body.appendChild(overlay);
    render();

    (async () => {
      try {
        const started = await startImportInboxImagesJob();
        const jobId = String(started?.job_id || "");
        if (!jobId) throw new Error("Inbox import did not return a job id.");
        state.job = started;
        render();
        await pollJobUntilTerminal({
          jobId,
          fetchStatus: () => fetchImportInboxImagesJobStatus(jobId),
          isTerminal: (job) => importJobIsTerminal(job.status),
          shouldContinue: () => (
            !state.closed
            && overlay.isConnected
            && String(state.job?.job_id || "") === jobId
          ),
          intervalMs: 500,
          onStatus: (job) => {
            state.job = job;
            state.cancelling = job.status === "cancelling" || Boolean(job.cancel_requested);
            state.actionError = "";
            render();
          },
          onTransientError: () => {
            state.job = {
              ...(state.job || {}),
              job_id: jobId,
              message: "Connection interrupted; retrying import status...",
            };
            render();
          },
        });
      } catch (err) {
        state.startError = err.message || "Inbox import failed to start.";
        render();
      }
    })();
  });
}

async function handleImportInboxImages() {
  const confirmed = await showImportConfirmDialog();
  if (!confirmed) return;
  try {
    const job = await showImportProgressDialog();
    const result = job?.result || null;
    if (job?.status === "cancelled") {
      showImportToast("Inbox import cancelled", "warning");
    } else if (result) {
      showImportToast(
        importInboxSummaryMessage(result),
        result?.ok === false || result?.errors?.length || job?.status === "failed" ? "error" : "success"
      );
    } else if (job?.error?.message) {
      showImportToast(job.error.message, "error");
    }
    await loadImportData();
  } catch (err) {
    showImportToast(err.message || "Inbox import failed", "error");
    await loadImportData();
  } finally {
    importState.loading = false;
    importState.loadingMessage = "";
    renderImportView();
  }
}

async function handleOpenImageInboxFolder() {
  try {
    await openImageInboxFolder();
    showImportToast("Opened Calibration Inbox folder", "ok");
  } catch (err) {
    showImportToast(err.message || "Could not open the Calibration Inbox folder", "error");
  }
}

async function handleCleanupUnusedImages() {
  importState.loading = true;
  importState.loadingMessage = "Cleaning up unused images";
  renderImportView();
  try {
    const result = await cleanupUnusedImages();
    showImportToast(cleanupUnusedSummaryMessage(result), result?.ok === false || result?.errors?.length ? "error" : "success");
    await loadImportData();
  } catch (err) {
    showImportToast(err.message || "Image cleanup failed", "error");
    await loadImportData();
  } finally {
    importState.loading = false;
    importState.loadingMessage = "";
    renderImportView();
  }
}

function renderImportLoadingState(message, detail = "") {
  const blankList = document.getElementById("importBlankList");
  const imageGrid = document.getElementById("importImageGrid");
  const sampleList = document.getElementById("importSampleList");
  const importBtn = document.getElementById("importInboxImportBtn");
  const cleanupBtn = document.getElementById("importInboxCleanupBtn");
  const sampleChip = document.getElementById("importSampleChip");
  const selectedImage = document.getElementById("importSelImage");
  const selectedBlank = document.getElementById("importSelBlank");
  const selectedSample = document.getElementById("importSelSample");
  const assignBtn = document.getElementById("importAssignBtn");
  const assignBlankBtn = document.getElementById("importAssignBlankBtn");
  const regBlankBtn = document.getElementById("importRegisterBlankBtn");

  if (importBtn) importBtn.disabled = true;
  if (cleanupBtn) cleanupBtn.disabled = true;
  if (sampleChip) sampleChip.textContent = "Loading";
  if (selectedImage) selectedImage.textContent = "None";
  if (selectedBlank) selectedBlank.textContent = "None";
  if (selectedSample) selectedSample.textContent = "None";
  if (assignBtn) assignBtn.disabled = true;
  if (assignBlankBtn) assignBlankBtn.disabled = true;
  if (regBlankBtn) regBlankBtn.disabled = true;

  const panel = `
    <div class="import-loading-panel">
      <div class="import-loading-spinner" aria-hidden="true"></div>
      <div>
        <strong>${escapeHtml(message || "Loading import data")}</strong>
        <p class="small-copy">${escapeHtml(detail || "Preparing sample assignments and image metadata.")}</p>
      </div>
    </div>
  `;
  if (blankList) blankList.innerHTML = panel;
  if (imageGrid) imageGrid.innerHTML = panel;
  if (sampleList) sampleList.innerHTML = panel;
}

function renderImportView() {
  const importView = document.getElementById("importView");
  if (!importView) return;

  const apiState = (typeof getApiLoadingState === "function") ? getApiLoadingState() : { state: "ready" };
  if (apiState.state === "loading") {
    renderImportLoadingState("Loading sample assignments", "Waiting for live sample records before sorting the image inbox.");
    return;
  }

  if (importState.loading || !importState.loaded) {
    renderImportLoadingState(importState.loadingMessage || "Loading image inbox", "Scanning images, blanks, and cached metadata.");
    return;
  }

  // Build assignment map from current sample data
  buildImageAssignmentMap();
  if (importState.selectedImage && !isImportImageSourceAvailable(importState.selectedImage)) {
    importState.selectedImage = null;
  }
  if (importState.selectedBlank && !isImportBlankSourceAvailable(importState.selectedBlank)) {
    importState.selectedBlank = null;
  }

  // Filter out registered blanks and images belonging to already-processed samples
  const registeredBlankFilenames = new Set(importState.blanks.map((b) => b.original_filename));
  const processedImageFilenames = new Set(
    data.samples
      .filter((e) => e._processing_status === "processed" || e._processing_status === "failed" || e._processing_status === "flagged")
      .map((e) => e._assigned_image)
      .filter(Boolean)
  );
  const inboxImages = importState.images.filter((img) => !registeredBlankFilenames.has(img.filename) && !processedImageFilenames.has(img.filename));
  const unassignedImages = inboxImages.filter((img) => !importState.imageAssignments[img.filename]);
  const assignedImages = inboxImages.filter((img) => importState.imageAssignments[img.filename]);

  // Samples — "ready" requires image + blank + orientation, and NOT already processed/failed/flagged
  const isProcessed = (exp) => exp._processing_status === "processed" || exp._processing_status === "failed" || exp._processing_status === "flagged";
  const isReady = (exp) => exp._assigned_image
    && exp._assigned_blank_id
    && exp._orientation_rots != null
    && sampleHasAvailableImportEvidence(exp)
    && !isProcessed(exp);
  const fullyAssigned = data.samples.filter(isReady);
  const pendingSamples = data.samples.filter((exp) => !isReady(exp) && !isProcessed(exp));
  const assignedSamples = fullyAssigned;

  // Update chips
  const sampleChip = document.getElementById("importSampleChip");
  if (sampleChip) sampleChip.textContent = `${pendingSamples.length} pending, ${assignedSamples.length} ready`;

  // Render all three panes
  renderImportBlankPane();
  renderImportImageGrid(unassignedImages, assignedImages);
  renderImportSampleList(pendingSamples, assignedSamples);

  // Bind blank drop zone and footer action buttons AFTER rendering
  bindBlankPaneDropZone();
  bindImportActionButtons();
}

function sourceAvailabilityInfo(source, noun = "Image") {
  if (!source) {
    return { available: true, state: "active", label: "Available", hint: "", message: "" };
  }
  const state = String(source.source_custody_state || "active").toLowerCase();
  const filename = source.filename || source.original_filename || noun;
  if (state === "archived") {
    return {
      available: false,
      state,
      label: "Archived",
      hint: "Restore before use",
      message: `${noun} '${filename}' is archived. Restore archived RAW images before assigning or reprocessing it.`,
    };
  }
  if (state === "external") {
    return {
      available: false,
      state,
      label: "External",
      hint: "Not available locally",
      message: `${noun} '${filename}' is in external custody and is not available locally.`,
    };
  }
  if (state === "missing" || source.path_exists === false) {
    return {
      available: false,
      state: state === "missing" ? state : "missing-file",
      label: state === "missing" ? "Missing" : "Missing File",
      hint: "Restore before use",
      message: `${noun} '${filename}' is not available locally. Restore the source image before assigning or reprocessing it.`,
    };
  }
  return { available: true, state, label: "Available", hint: "", message: "" };
}

function importSourceStateClass(state) {
  return String(state || "active").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

function findImportImage(filename) {
  if (!filename) return null;
  return importState.images.find((img) => img.filename === filename) || null;
}

function findImportBlankByFilename(filename) {
  if (!filename) return null;
  return importState.blanks.find((blank) => blank.original_filename === filename || blank.filename === filename) || null;
}

function findImportBlankById(blankId) {
  if (!blankId) return null;
  return importState.blanks.find((blank) => blank.blank_id === blankId) || null;
}

function importImageSourceAvailability(filename) {
  return sourceAvailabilityInfo(findImportImage(filename), "Image");
}

function importBlankSourceAvailability(filename) {
  return sourceAvailabilityInfo(findImportImage(filename) || findImportBlankByFilename(filename), "Blank image");
}

function isImportImageSourceAvailable(filename) {
  return importImageSourceAvailability(filename).available;
}

function isImportBlankSourceAvailable(filename) {
  return importBlankSourceAvailability(filename).available;
}

function showSourceUnavailableToast(availability) {
  if (!availability || availability.available) return;
  showImportToast(availability.message || "Source image is not available locally. Restore it before continuing.", "error");
}

function sampleHasAvailableImportEvidence(exp) {
  if (exp?._assigned_image && !isImportImageSourceAvailable(exp._assigned_image)) return false;
  if (exp?._assigned_blank_id) {
    const blankInfo = findImportBlankById(exp._assigned_blank_id);
    if (!blankInfo) return false;
    const blankFilename = blankInfo?.original_filename || blankInfo?.filename || "";
    if (blankFilename && !isImportBlankSourceAvailable(blankFilename)) return false;
  }
  return true;
}

function _imageCardHtml(img) {
  const isAssigned = !!importState.imageAssignments[img.filename];
  const assignedTo = importState.imageAssignments[img.filename] || "";
  const isSelected = importState.selectedImage === img.filename;
  const isIgnored = !!img.ignored;
  const ext = (img.filename || "").split(".").pop() || "";
  const rotationCw = Number(img.rotation_cw || 0) % 4;
  const rotationLabel = rotationCw ? ` · rot ${rotationCw * 90}\u00b0` : "";
  const availability = sourceAvailabilityInfo(img, "Image");
  const unavailable = !availability.available;

  let classes = "import-image-card";
  if (isSelected) classes += " is-selected";
  if (isAssigned) classes += " is-assigned";
  if (isIgnored) classes += " is-ignored";
  if (unavailable) classes += ` is-source-unavailable is-source-${importSourceStateClass(availability.state)}`;

  const stem = img.filename.replace(/\.[^.]+$/, "");
  const filenameAttr = _escAttr(img.filename);
  // Only show ignore/restore on unassigned images
  let ignoreBtn = "";
  if (isIgnored) {
    ignoreBtn = `<button class="import-ignore-btn is-ignored" data-ignore-file="${filenameAttr}" title="Unignore image">&#x21A9;</button>`;
  } else if (!isAssigned && !unavailable) {
    ignoreBtn = `<button class="import-ignore-btn" data-ignore-file="${filenameAttr}" title="Ignore image">&#x2715;</button>`;
  }
  const rotateBtn = !isIgnored && !unavailable
    ? `<button class="import-rotate-btn" data-rotate-file="${filenameAttr}" title="Rotate thumbnail 90 degrees clockwise">&#x21BB;</button>`
    : "";
  const unavailableOverlay = unavailable
    ? `<div class="import-source-unavailable-overlay">
         <span class="import-source-badge is-${importSourceStateClass(availability.state)}">${_escHtml(availability.label)}</span>
         <span>${_escHtml(availability.hint)}</span>
       </div>`
    : "";
  const thumbContent = `<img class="import-card-thumb" src="${previewUrl(img.filename)}" alt="${_escAttr(stem)}"
             draggable="false" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
        <div class="import-card-icon" style="display:none">&#128247;</div>
        ${img.exif_timestamp ? `<div class="import-card-exif">${formatExifDate(img.exif_timestamp)}</div>` : ""}
        ${unavailableOverlay}`;

  return `
    <div class="${classes}" data-import-image="${filenameAttr}" data-source-unavailable="${unavailable ? "true" : "false"}" data-source-message="${_escAttr(availability.message)}" draggable="${!isAssigned && !isIgnored && !unavailable}">
      ${ignoreBtn}
      ${rotateBtn}
      <div class="import-card-filename${stem.length > 25 ? " is-long" : ""}" title="${_escAttr(img.filename)}">${_escHtml(stem).replace(/[-_]/g, "$&\u200B")}</div>
      <div class="import-card-thumb-wrap">
        ${thumbContent}
        ${isAssigned ? `<div class="import-card-assigned">${_escHtml(assignedTo)}</div>` : ""}
      </div>
      <div class="import-card-size">.${ext.toUpperCase()} ${formatFileSize(img.size_bytes)}${rotationLabel}</div>
    </div>
  `;
}

function renderImportImageGrid(unassigned, assigned) {
  const grid = document.getElementById("importImageGrid");
  if (!grid) return;

  if (importState.images.length === 0) {
    grid.innerHTML = `<p class="small-copy">No images found in inbox. Place images in the Inbox folder and click Import from Inbox.</p>`;
    return;
  }

  // Split unassigned into active vs ignored
  const activeImages = unassigned.filter((img) => !img.ignored);
  const ignoredImages = unassigned.filter((img) => img.ignored);

  // Initialize collapse state if not set
  if (!importState._collapseState) importState._collapseState = {};

  const unassignedCollapsed = importState._collapseState["img-unassigned"];
  const assignedCollapsed = importState._collapseState["img-assigned"];
  const ignoredCollapsed = importState._collapseState["img-ignored"];

  const activeCards = activeImages.map(_imageCardHtml).join("");
  const assignedCards = assigned.map(_imageCardHtml).join("");
  const ignoredCards = ignoredImages.map(_imageCardHtml).join("");

  const unassignedSection = `<div class="import-section-title" data-collapse-key="img-unassigned">
      <div class="import-section-title-main">
        <span class="collapse-caret">${unassignedCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Unassigned (${activeImages.length})</span>
      </div>
    </div>${unassignedCollapsed ? "" : activeCards}`;

  const assignedSection = `<div class="import-section-title" data-collapse-key="img-assigned">
      <div class="import-section-title-main">
        <span class="collapse-caret">${assignedCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Assigned (${assigned.length})</span>
      </div>
    </div>${assignedCollapsed ? "" : assignedCards}`;

  // Ignore drop zone is a ghost card inside the Ignored section content
  const ignoreGhostCard = `<div class="import-ignore-ghost-card" id="importIgnoreDropZone">
      <span class="ignore-ghost-label">drop here to ignore</span>
    </div>`;

  const ignoredSection = `<div class="import-section-title" data-collapse-key="img-ignored">
      <div class="import-section-title-main">
        <span class="collapse-caret">${ignoredCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Ignored (${ignoredImages.length})</span>
      </div>
    </div>${ignoredCollapsed ? "" : ignoreGhostCard + ignoredCards}`;

  grid.innerHTML = unassignedSection + assignedSection + ignoredSection;
  bindImportImageCards();
  bindIgnoreDropZone();
  bindCollapsibleSections(grid);
}

function renderImportSampleList(pending, assigned) {
  const list = document.getElementById("importSampleList");
  if (!list) return;

  const ORIENTATION_ARROWS = ["↑", "→", "↓", "←"];

  function sampleCardHtml(exp, isFullyAssigned) {
    const materialLines = sampleMaterialLines(exp);
    const isSelected = importState.selectedSample === exp.sample_id;
    const imgName = exp._assigned_image || "";
    const blankId = exp._assigned_blank_id || "";
    const orientRot = exp._orientation_rots;
    const imageAvailability = imgName ? importImageSourceAvailability(imgName) : null;
    const imageUnavailable = !!imageAvailability && !imageAvailability.available;
    const blankInfo = blankId ? findImportBlankById(blankId) : null;
    const blankFilename = blankInfo?.original_filename || "";
    const blankAvailability = blankFilename ? importBlankSourceAvailability(blankFilename) : null;
    const blankUnavailable = !!blankAvailability && !blankAvailability.available;

    let classes = "import-sample-card";
    if (isSelected) classes += " is-selected";
    if (isFullyAssigned) classes += " is-fully-assigned";

    // Photo slot
    const photoSlot = imgName
      ? `<div class="sc-slot sc-slot-filled${imageUnavailable ? " sc-slot-source-unavailable" : ""}" title="${_escAttr(imageUnavailable ? imageAvailability.message : imgName)}">
           ${imageUnavailable
             ? `<div class="sc-slot-source-status"><span>${_escHtml(imageAvailability.label)}</span></div>`
             : `<img src="${previewUrl(imgName)}" alt="photo" onerror="this.style.display='none'">`}
           <button class="sc-slot-unassign" data-unassign-type="image" data-unassign-sample="${exp.sample_id}" title="Unassign"><svg width="8" height="8" viewBox="0 0 10 10"><path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>
           <span class="sc-slot-label">${_escHtml(imgName.replace(/\.[^.]+$/, ""))}</span>
         </div>`
      : `<div class="sc-slot sc-slot-empty sc-drop-image" data-drop-sample="${exp.sample_id}">
           <span class="sc-slot-placeholder">photo</span>
         </div>`;

    // Blank slot
    const blankSlot = blankId
      ? `<div class="sc-slot sc-slot-filled sc-slot-blank${blankUnavailable ? " sc-slot-source-unavailable" : ""}" title="${_escAttr(blankUnavailable ? blankAvailability.message : blankId)}">
           ${blankUnavailable
             ? `<div class="sc-slot-source-status"><span>${_escHtml(blankAvailability.label)}</span></div>`
             : `<img src="${previewUrl(blankFilename)}" alt="blank" onerror="this.style.display='none'">`}
           <button class="sc-slot-unassign" data-unassign-type="blank" data-unassign-sample="${exp.sample_id}" title="Unassign"><svg width="8" height="8" viewBox="0 0 10 10"><path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></button>
           <span class="sc-slot-label">${_escHtml(blankId)}</span>
         </div>`
      : `<div class="sc-slot sc-slot-empty sc-drop-blank" data-drop-sample="${exp.sample_id}">
           <span class="sc-slot-placeholder">blank</span>
         </div>`;

    // Orientation d-pad
    function arrowBtn(rot, arrow) {
      const pos = ["top", "right", "bottom", "left"][rot];
      const tip = ["Open side up", "Open side right", "Open side down", "Open side left"][rot];
      const active = orientRot === rot ? " is-active" : "";
      return `<button class="sc-orient-btn sc-orient-${pos}${active}" data-orient-rot="${rot}" data-orient-sample="${exp.sample_id}" title="${tip}">${arrow}</button>`;
    }
    const orientPad = `
      <div class="sc-orient-pad">
        ${arrowBtn(0, "&#x2191;")}
        ${arrowBtn(3, "&#x2190;")}
        <div class="sc-orient-center"></div>
        ${arrowBtn(1, "&#x2192;")}
        ${arrowBtn(2, "&#x2193;")}
      </div>`;

    // Ready badge
    const readyBadge = isFullyAssigned ? `<span class="import-ready-label">ready</span>` : "";
    const materialList = materialLines.map((line) => `
      <div class="sc-material-line" title="${_escAttr(line.name)}">
        <span class="color-chip sc-material-chip" style="background:${line.hex}"></span>
        <span class="import-sample-filament">${_escHtml(line.name)}</span>
      </div>
    `).join("") || (
      isStructuredGeometryBackend() && !(exp.roles || []).length
        ? `<div class="strip-diagram-contract-error">Missing geometry role data</div>`
        : ""
    );

    return `
      <div class="${classes}" data-import-sample="${exp.sample_id}">
        ${readyBadge}
        <div class="sc-card-main">
          <div class="import-sample-info">
            <div class="import-sample-id">${exp.sample_id}</div>
          </div>
          <div class="sc-material-list">
            ${materialList}
          </div>
        </div>
        <div class="sc-strip-mini">${buildStripMiniTable(exp)}</div>
        <div class="sc-assign-row">
          ${photoSlot}
          ${blankSlot}
          ${orientPad}
        </div>
      </div>
    `;
  }

  if (!importState._collapseState) importState._collapseState = {};

  const readyCollapsed = importState._collapseState["samp-ready"];
  const needsCollapsed = importState._collapseState["samp-needs"];

  const readyCards = assigned.map((exp) => sampleCardHtml(exp, true)).join("");
  const pendingCards = pending.map((exp) => sampleCardHtml(exp, false)).join("");

  const readySection = `<div class="import-section-title" data-collapse-key="samp-ready">
      <div class="import-section-title-main">
        <span class="collapse-caret">${readyCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Ready For Processing (${assigned.length})</span>
      </div>
    </div>${readyCollapsed ? "" : readyCards}`;

  const needsSection = `<div class="import-section-title" data-collapse-key="samp-needs">
      <div class="import-section-title-main">
        <span class="collapse-caret">${needsCollapsed ? "&#x25B6;" : "&#x25BC;"}</span><span>Needs Assignment (${pending.length})</span>
      </div>
    </div>${needsCollapsed ? "" : pendingCards}`;

  list.innerHTML = readySection + needsSection || `<p class="small-copy">No samples loaded.</p>`;

  bindImportSampleCards();
  bindCollapsibleSections(list);
}

function renderImportBlankPane() {
  const list = document.getElementById("importBlankList");
  if (!list) return;

  if (importState.blanks.length === 0) {
    list.innerHTML = `<p class="small-copy" style="margin-top:4px">No blanks registered yet.</p>`;
    return;
  }

  const cards = importState.blanks.map((blank) => {
    const filename = blank.original_filename || blank.filename || "";
    const filenameMatch = filename.match(/^(.*?)(\.[^.]+)?$/);
    const stem = filenameMatch?.[1] || filename;
    const ext = filenameMatch?.[2] || "";
    const availability = importBlankSourceAvailability(filename);
    const unavailable = !availability.available;
    const isSelected = importState.selectedBlank === filename;
    let classes = "import-blank-card";
    if (isSelected) classes += " is-selected";
    if (unavailable) classes += ` is-source-unavailable is-source-${importSourceStateClass(availability.state)}`;
    const unavailableOverlay = unavailable
      ? `<div class="import-source-unavailable-overlay">
           <span class="import-source-badge is-${importSourceStateClass(availability.state)}">${_escHtml(availability.label)}</span>
           <span>${_escHtml(availability.hint)}</span>
         </div>`
      : "";
    const thumbContent = `<img class="import-card-thumb" src="${previewUrl(filename)}" alt="${_escAttr(filename)}"
               draggable="false" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
         <div class="import-card-icon import-card-icon-small" style="display:none">&#128247;</div>
         ${blank.exif_timestamp ? `<div class="import-card-exif">${formatExifDate(blank.exif_timestamp)}</div>` : ""}
         ${unavailableOverlay}`;
    return `
      <div class="${classes}" data-blank-filename="${_escAttr(filename)}" data-source-unavailable="${unavailable ? "true" : "false"}" data-source-message="${_escAttr(availability.message)}" draggable="${unavailable ? "false" : "true"}">
        <span class="import-unregister-x" data-blank-id="${_escAttr(blank.blank_id)}" title="Unregister blank"><svg width="10" height="10" viewBox="0 0 10 10"><path d="M1 1L9 9M9 1L1 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></span>
        <div class="import-blank-card-id">${_escHtml(blank.blank_id)}</div>
        <div class="import-blank-card-filename" title="${_escAttr(filename)}"><span class="import-filename-stem">${_escHtml(stem)}</span><span class="import-filename-ext">${_escHtml(ext)}</span></div>
        <div class="import-card-thumb-wrap">
          ${thumbContent}
        </div>
      </div>
    `;
  }).join("");

  list.innerHTML = cards;

  // Bind blank card interactions
  list.querySelectorAll(".import-blank-card").forEach((card) => {
    const filename = card.dataset.blankFilename;

    // Click to select
    card.addEventListener("click", () => {
      if (card.dataset.sourceUnavailable === "true") {
        showImportToast(card.dataset.sourceMessage || "Blank source image is not available locally. Restore it before assigning.", "error");
        return;
      }
      importState.selectedBlank = importState.selectedBlank === filename ? null : filename;
      list.querySelectorAll(".import-blank-card").forEach((c) => {
        c.classList.toggle("is-selected", c.dataset.blankFilename === importState.selectedBlank);
      });
      updateImportSelectionBar();
    });

    // Drag start — same pattern as image cards
    card.addEventListener("dragstart", (e) => {
      if (card.dataset.sourceUnavailable === "true") {
        e.preventDefault();
        showImportToast(card.dataset.sourceMessage || "Blank source image is not available locally. Restore it before assigning.", "error");
        return;
      }
      e.dataTransfer.setData("text/plain", filename);
      e.dataTransfer.effectAllowed = "all";
      importState.selectedImage = filename;
      updateImportSelectionBar();
    });
  });

  // Unregister buttons
  list.querySelectorAll(".import-unregister-x").forEach((x) => {
    const blankId = x.dataset.blankId;
    bindConfirmAction(x, {
      armedText: "confirm?",
      onConfirm: async () => {
        try {
          await unregisterBlank(blankId);
          await loadImportData();
          renderImportView();
          showImportToast(`Unregistered ${blankId}`, "success");
        } catch (err) {
          showImportToast(err.message || "Unregister failed", "error");
        }
      },
    });
  });
}

function bindBlankPaneDropZone() {
  const dropChip = document.getElementById("importBlankDropZone");
  const panel = document.getElementById("importBlanksPanel");
  if (!dropChip || !panel || panel._dropBound) return;
  panel._dropBound = true;

  let dragDepth = 0;
  const setActive = (active) => {
    panel.classList.toggle("is-drag-over", active);
    dropChip.classList.toggle("is-drag-over", active);
  };

  panel.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth += 1;
    setActive(true);
  });

  // CRITICAL: preventDefault on dragover is REQUIRED for drop to fire
  panel.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "link";
    setActive(true);
  });

  panel.addEventListener("dragleave", (e) => {
    if (!panel.contains(e.relatedTarget)) {
      dragDepth = 0;
      setActive(false);
      return;
    }
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setActive(false);
  });

  panel.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    setActive(false);

    const filename = e.dataTransfer.getData("text/plain");
    // drop on blank zone
    if (!filename) return;
    const sourceAvailability = importImageSourceAvailability(filename);
    if (!sourceAvailability.available) {
      showSourceUnavailableToast(sourceAvailability);
      return;
    }

    if (importState.blanks.some((b) => b.original_filename === filename)) {
      showImportToast(`${filename} is already registered as a blank`, "error");
      return;
    }

    if (importState.imageAssignments[filename]) {
      showImportToast(`${filename} is assigned to ${importState.imageAssignments[filename]} — unassign first`, "error");
      return;
    }

    try {
      const result = await registerBlank(filename);
      showImportToast(`Registered ${filename} as ${result?.blank_id || "blank"}`, "success");
      importState.selectedImage = null;
      await loadImportData();
      renderImportView();
    } catch (err) {
      showImportToast(`Registration failed: ${err.message}`, "error");
    }
  });
}

function bindIgnoreDropZone() {
  const dropZone = document.getElementById("importIgnoreDropZone");
  if (!dropZone) return;

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    dropZone.classList.add("is-drag-over");
  });

  dropZone.addEventListener("dragleave", (e) => {
    if (!dropZone.contains(e.relatedTarget)) {
      dropZone.classList.remove("is-drag-over");
    }
  });

  dropZone.addEventListener("drop", async (e) => {
    e.preventDefault();
    dropZone.classList.remove("is-drag-over");

    const filename = e.dataTransfer.getData("text/plain");
    if (!filename) return;
    const sourceAvailability = importImageSourceAvailability(filename);
    if (!sourceAvailability.available) {
      showSourceUnavailableToast(sourceAvailability);
      return;
    }

    // Don't ignore if it's assigned to a sample
    if (importState.imageAssignments[filename]) {
      showImportToast(`${filename} is assigned to ${importState.imageAssignments[filename]} — unassign first`, "error");
      return;
    }

    // Don't ignore if it's a registered blank
    if (importState.blanks.some((b) => b.original_filename === filename)) {
      showImportToast(`${filename} is a registered blank — unregister first`, "error");
      return;
    }

    try {
      await ignoreImage(filename);
      showImportToast(`Ignored ${filename}`, "success");
      await loadImportData();
      renderImportView();
    } catch (err) {
      showImportToast(`Ignore failed: ${err.message}`, "error");
    }
  });
}

function bindCollapsibleSections(container) {
  container.querySelectorAll(".import-section-title").forEach((title) => {
    title.addEventListener("click", () => {
      const key = title.dataset.collapseKey;
      if (!importState._collapseState) importState._collapseState = {};
      importState._collapseState[key] = !importState._collapseState[key];
      renderImportView();
    });
  });
}

function updateImportSelectionBar() {
  const selImage = document.getElementById("importSelImage");
  const selSample = document.getElementById("importSelSample");
  const assignBtn = document.getElementById("importAssignBtn");
  const assignBlankBtn = document.getElementById("importAssignBlankBtn");

  const selBlank = document.getElementById("importSelBlank");
  if (selImage) selImage.textContent = importState.selectedImage || "None";
  if (selBlank) selBlank.textContent = importState.selectedBlank || "None";
  if (selSample) selSample.textContent = importState.selectedSample || "None";

  // Register as Blank button — only needs an image selected
  const regBlankBtn = document.getElementById("importRegisterBlankBtn");
  const imageBlocked = importState.selectedImage && !isImportImageSourceAvailable(importState.selectedImage);
  const blankBlocked = importState.selectedBlank && !isImportBlankSourceAvailable(importState.selectedBlank);
  if (regBlankBtn) regBlankBtn.disabled = !!imageBlocked;
  if (assignBtn) assignBtn.disabled = !!imageBlocked;
  if (assignBlankBtn) assignBlankBtn.disabled = !!blankBlocked;
}

function bindImportImageCards() {
  const grid = document.getElementById("importImageGrid");
  if (!grid) return;

  grid.querySelectorAll(".import-image-card").forEach((card) => {
    const filename = card.dataset.importImage;

    card.addEventListener("click", () => {
      if (card.dataset.sourceUnavailable === "true") {
        showImportToast(card.dataset.sourceMessage || "Source image is not available locally. Restore it before assigning.", "error");
        return;
      }
      if (importState.selectedImage === filename) {
        importState.selectedImage = null;
      } else {
        importState.selectedImage = filename;
      }
      // Re-highlight without full re-render
      grid.querySelectorAll(".import-image-card").forEach((c) => {
        c.classList.toggle("is-selected", c.dataset.importImage === importState.selectedImage);
      });
      updateImportSelectionBar();
    });

    // Drag start
    card.addEventListener("dragstart", (e) => {
      if (card.dataset.sourceUnavailable === "true") {
        e.preventDefault();
        showImportToast(card.dataset.sourceMessage || "Source image is not available locally. Restore it before assigning.", "error");
        return;
      }
      if (importState.imageAssignments[filename]) {
        e.preventDefault();
        return;
      }
      e.dataTransfer.setData("text/plain", filename);
      e.dataTransfer.effectAllowed = "all";
      importState.selectedImage = filename;
      grid.querySelectorAll(".import-image-card").forEach((c) => {
        c.classList.toggle("is-selected", c.dataset.importImage === filename);
      });
      updateImportSelectionBar();
    });
  });

  // Unregister blank buttons
  grid.querySelectorAll(".import-unregister-x").forEach((x) => {
    const blankId = x.dataset.blankId;
    bindConfirmAction(x, {
      armedText: "confirm?",
      onConfirm: async () => {
        try {
          await unregisterBlank(blankId);
          await loadImportData();
          renderImportView();
          showImportToast(`Unregistered ${blankId}`, "success");
        } catch (err) {
          showImportToast(err.message || "Unregister failed", "error");
        }
      },
    });
  });

  // Ignore / unignore buttons
  grid.querySelectorAll(".import-ignore-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const filename = btn.dataset.ignoreFile;
      const isIgnored = btn.classList.contains("is-ignored");
      try {
        if (isIgnored) {
          await unignoreImage(filename);
        } else {
          await ignoreImage(filename);
        }
        await loadImportData();
        renderImportView();
      } catch (err) {
        showImportToast(err.message || "Failed to update", "error");
      }
    });
  });

  grid.querySelectorAll(".import-rotate-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const filename = btn.dataset.rotateFile;
      const current = getImageRotationCw(filename);
      const next = (current + 1) % 4;
      try {
        const result = await rotateImage(filename, next);
        await handleRefresh({ ensureAssets: false });
        await loadImportData();
        renderWorkspace();
        const affected = Number(result?.affected_samples || 0);
        const extra = affected ? ` ${affected} assigned sample${affected === 1 ? "" : "s"} reset for re-orientation.` : "";
        showImportToast(`Rotated ${filename} to ${next * 90}\u00b0.${extra}`, "success");
      } catch (err) {
        showImportToast(err.message || "Rotate failed", "error");
      }
    });
  });
}

function bindImportSampleCards() {
  const list = document.getElementById("importSampleList");
  if (!list) return;

  list.querySelectorAll(".import-sample-card").forEach((card) => {
    const sampleId = card.dataset.importSample;

    // Click card to select
    card.addEventListener("click", (e) => {
      // Don't toggle selection if clicking a button or slot
      if (e.target.closest(".sc-orient-btn, .sc-slot-unassign, .sc-slot")) return;
      if (importState.selectedSample === sampleId) {
        importState.selectedSample = null;
      } else {
        importState.selectedSample = sampleId;
      }
      list.querySelectorAll(".import-sample-card").forEach((c) => {
        c.classList.toggle("is-selected", c.dataset.importSample === importState.selectedSample);
      });
      updateImportSelectionBar();
    });

    // Drop on individual photo/blank slots
    card.querySelectorAll(".sc-slot-empty, .sc-slot-filled").forEach((slot) => {
      slot.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = "link";
        slot.classList.add("is-drag-over");
      });
      slot.addEventListener("dragleave", (e) => {
        if (!slot.contains(e.relatedTarget)) slot.classList.remove("is-drag-over");
      });
      slot.addEventListener("drop", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        slot.classList.remove("is-drag-over");
        const filename = e.dataTransfer.getData("text/plain");
        if (!filename || !sampleId) return;

        const blankInfo = findImportBlankByFilename(filename);
        const isBlankSlot = slot.classList.contains("sc-drop-blank") || slot.classList.contains("sc-slot-blank");

        if (blankInfo) {
          const blankAvailability = importBlankSourceAvailability(filename);
          if (!blankAvailability.available) {
            showSourceUnavailableToast(blankAvailability);
            return;
          }
          // Dropping a blank
          try {
            await assignBlank(sampleId, blankInfo.blank_id);
            const exp = data.samples.find((x) => x.sample_id === sampleId);
            if (exp) { exp._assigned_blank_id = blankInfo.blank_id; updateProcessingStatus(exp); }
            showImportToast(`Assigned ${blankInfo.blank_id} to ${sampleId}`, "success");
            renderImportView();
          } catch (err) { showImportToast(`Blank assignment failed: ${err.message}`, "error"); }
        } else if (!isBlankSlot) {
          const sourceAvailability = importImageSourceAvailability(filename);
          if (!sourceAvailability.available) {
            showSourceUnavailableToast(sourceAvailability);
            return;
          }
          // Dropping an image on photo slot
          await doAssignImage(sampleId, filename);
        } else {
          showImportToast("That's not a registered blank — drag to the photo slot instead", "error");
        }
      });
    });

    // Also accept drops on the whole card as fallback (auto-detect type)
    card.addEventListener("dragover", (e) => {
      if (e.target.closest(".sc-slot")) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "link";
      card.classList.add("is-drop-target");
    });
    card.addEventListener("dragleave", (e) => {
      if (!card.contains(e.relatedTarget)) card.classList.remove("is-drop-target");
    });
    card.addEventListener("drop", async (e) => {
      if (e.target.closest(".sc-slot")) return;
      e.preventDefault();
      card.classList.remove("is-drop-target");
      const filename = e.dataTransfer.getData("text/plain");
      if (!filename || !sampleId) return;
      const blankInfo = findImportBlankByFilename(filename);
      if (blankInfo) {
        const blankAvailability = importBlankSourceAvailability(filename);
        if (!blankAvailability.available) {
          showSourceUnavailableToast(blankAvailability);
          return;
        }
        try {
          await assignBlank(sampleId, blankInfo.blank_id);
          const exp = data.samples.find((x) => x.sample_id === sampleId);
          if (exp) { exp._assigned_blank_id = blankInfo.blank_id; updateProcessingStatus(exp); }
          showImportToast(`Assigned ${blankInfo.blank_id} to ${sampleId}`, "success");
          renderImportView();
        } catch (err) { showImportToast(`Blank assignment failed: ${err.message}`, "error"); }
      } else {
        const sourceAvailability = importImageSourceAvailability(filename);
        if (!sourceAvailability.available) {
          showSourceUnavailableToast(sourceAvailability);
          return;
        }
        await doAssignImage(sampleId, filename);
      }
    });
  });

  // Unassign × buttons on slots
  list.querySelectorAll(".sc-slot-unassign").forEach((btn) => {
    const sampleId = btn.dataset.unassignSample;
    const type = btn.dataset.unassignType;
    bindConfirmAction(btn, {
      armedText: "?",
      timeout: 2000,
      onConfirm: async () => {
        try {
          if (type === "blank") {
            await assignBlank(sampleId, null);
            const exp = data.samples.find((x) => x.sample_id === sampleId);
            if (exp) { exp._assigned_blank_id = null; updateProcessingStatus(exp); }
            showImportToast(`Unassigned blank from ${sampleId}`, "success");
          } else {
            await unassignImage(sampleId);
            const exp = data.samples.find((x) => x.sample_id === sampleId);
            if (exp) {
              exp._assigned_image = null;
              exp.source_image = null;
              exp._orientation_rots = null;
              updateProcessingStatus(exp);
            }
            buildImageAssignmentMap();
            showImportToast(`Unassigned image from ${sampleId}`, "success");
          }
          renderImportView();
        } catch (err) { showImportToast(`Unassign failed: ${err.message}`, "error"); }
      },
    });
  });

  // Orientation d-pad buttons
  list.querySelectorAll(".sc-orient-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const rot = Number(btn.dataset.orientRot);
      const sid = btn.dataset.orientSample;
      const exp = data.samples.find((x) => x.sample_id === sid);
      if (!exp) return;
      if (exp._assigned_image) {
        const sourceAvailability = importImageSourceAvailability(exp._assigned_image);
        if (!sourceAvailability.available) {
          showSourceUnavailableToast(sourceAvailability);
          return;
        }
      }

      // If already active, deselect
      if (exp._orientation_rots === rot) {
        exp._orientation_rots = null;
      } else {
        exp._orientation_rots = rot;
      }

      // Persist if image is assigned
      if (exp._assigned_image) {
        try {
          await assignImage(sid, exp._assigned_image, exp._orientation_rots);
        } catch (err) {
          showImportToast(`Orientation save failed: ${err.message}`, "error");
        }
      }

      // Update processing status (orientation is required for "assigned")
      updateProcessingStatus(exp);

      // Update buttons locally without full re-render
      const pad = btn.closest(".sc-orient-pad");
      pad.querySelectorAll(".sc-orient-btn").forEach((b) => {
        b.classList.toggle("is-active", Number(b.dataset.orientRot) === exp._orientation_rots);
      });

      // Re-render sample list so ready badge updates
      renderImportView();
    });
  });
}


function bindImportActionButtons() {
  const importBtn = document.getElementById("importInboxImportBtn");
  const openFolderBtn = document.getElementById("importInboxOpenFolderBtn");
  const cleanupBtn = document.getElementById("importInboxCleanupBtn");
  const csvAssignmentBtn = document.getElementById("importCsvAssignmentBtn");
  const assignBtn = document.getElementById("importAssignBtn");
  const assignBlankBtn = document.getElementById("importAssignBlankBtn");

  if (importBtn) {
    importBtn.disabled = !!importState.loading;
    if (!importBtn._importBound) {
      importBtn._importBound = true;
      importBtn.addEventListener("click", handleImportInboxImages);
    }
  }

  if (openFolderBtn && !openFolderBtn._openFolderBound) {
    openFolderBtn._openFolderBound = true;
    openFolderBtn.addEventListener("click", handleOpenImageInboxFolder);
  }

  if (cleanupBtn) {
    cleanupBtn.disabled = !!importState.loading;
    if (!cleanupBtn._cleanupBound) {
      cleanupBtn._cleanupBound = true;
      bindConfirmAction(cleanupBtn, {
        armedText: "Confirm Cleanup",
        onConfirm: handleCleanupUnusedImages,
      });
    }
  }

  if (csvAssignmentBtn) {
    csvAssignmentBtn.disabled = !!importState.loading;
    if (!csvAssignmentBtn._csvAssignmentBound) {
      csvAssignmentBtn._csvAssignmentBound = true;
      csvAssignmentBtn.addEventListener("click", showCsvAssignmentImportDialog);
    }
  }

  // Assign Image button
  if (assignBtn && !assignBtn._importBound) {
    assignBtn._importBound = true;
    assignBtn.addEventListener("click", async () => {
      if (!importState.selectedImage && !importState.selectedSample) {
        showImportToast("No image or sample selected", "error"); return;
      }
      if (!importState.selectedImage) {
        showImportToast("No image selected", "error"); return;
      }
      if (!importState.selectedSample) {
        showImportToast("No sample selected", "error"); return;
      }
      const sourceAvailability = importImageSourceAvailability(importState.selectedImage);
      if (!sourceAvailability.available) {
        showSourceUnavailableToast(sourceAvailability);
        return;
      }
      await doAssignImage(importState.selectedSample, importState.selectedImage);
    });
  }

  // Assign Blank button (from selected blank in left pane)
  if (assignBlankBtn && !assignBlankBtn._importBound) {
    assignBlankBtn._importBound = true;
    assignBlankBtn.addEventListener("click", async () => {
      if (!importState.selectedBlank && !importState.selectedSample) {
        showImportToast("No blank or sample selected", "error"); return;
      }
      if (!importState.selectedBlank) {
        showImportToast("No blank selected", "error"); return;
      }
      if (!importState.selectedSample) {
        showImportToast("No sample selected", "error"); return;
      }
      const blankObj = findImportBlankByFilename(importState.selectedBlank);
      const blankId = blankObj?.blank_id;
      if (!blankId) {
        showImportToast("Selected blank not found", "error"); return;
      }
      const blankAvailability = importBlankSourceAvailability(importState.selectedBlank);
      if (!blankAvailability.available) {
        showSourceUnavailableToast(blankAvailability);
        return;
      }
      try {
        await assignBlank(importState.selectedSample, blankId);
        const exp = data.samples.find((x) => x.sample_id === importState.selectedSample);
        if (exp) {
          exp._assigned_blank_id = blankId;
          updateProcessingStatus(exp);
        }
        showImportToast(`Assigned ${blankId} to ${importState.selectedSample}`, "success");
        renderImportView();
      } catch (err) {
        showImportToast(`Blank assignment failed: ${err.message}`, "error");
      }
    });
  }

  // Register as Blank button (from selected image in middle pane)
  const regBlankBtn = document.getElementById("importRegisterBlankBtn");
  if (regBlankBtn && !regBlankBtn._importBound) {
    regBlankBtn._importBound = true;
    regBlankBtn.addEventListener("click", async () => {
      if (!importState.selectedImage) {
        showImportToast("No image selected", "error"); return;
      }
      const filename = importState.selectedImage;
      const sourceAvailability = importImageSourceAvailability(filename);
      if (!sourceAvailability.available) {
        showSourceUnavailableToast(sourceAvailability);
        return;
      }
      if (importState.blanks.some(b => b.original_filename === filename)) {
        showImportToast(`${filename} is already registered as a blank`, "error"); return;
      }
      if (importState.imageAssignments[filename]) {
        showImportToast(`${filename} is assigned to ${importState.imageAssignments[filename]} — unassign first`, "error"); return;
      }
      try {
        const result = await registerBlank(filename);
        showImportToast(`Registered ${filename} as ${result?.blank_id || "blank"}`, "success");
        importState.selectedImage = null;
        await loadImportData();
        renderImportView();
      } catch (err) {
        showImportToast(`Registration failed: ${err.message}`, "error");
      }
    });
  }
}

function updateProcessingStatus(exp) {
  // Require image + blank + orientation to be "assigned" (ready for processing)
  if (exp._assigned_image && exp._assigned_blank_id && exp._orientation_rots != null) {
    if (exp._processing_status === "unassigned") {
      exp._processing_status = "assigned";
    }
  } else if (!exp._assigned_image || !exp._assigned_blank_id) {
    // If either is cleared, revert to unassigned and clear results
    exp._processing_status = "unassigned";
    exp.processed = false;
    exp._measurements = null;
    exp._flag_reason = null;
  }
}

async function doAssignImage(sampleId, filename) {
  const sourceAvailability = importImageSourceAvailability(filename);
  if (!sourceAvailability.available) {
    showSourceUnavailableToast(sourceAvailability);
    return;
  }
  try {
    const exp = data.samples.find((x) => x.sample_id === sampleId);
    const orientation = exp ? exp._orientation_rots : null;
    await assignImage(sampleId, filename, orientation);
    // Update local state
    if (exp) {
      exp._assigned_image = filename;
      updateProcessingStatus(exp);
    }
    importState.imageAssignments[filename] = sampleId;
    importState.assignedCount++;
    importState.selectedImage = null;
    importState.selectedSample = null;
    showImportToast(`Assigned ${filename} to ${sampleId}`, "success");
    renderImportView();
  } catch (err) {
    showImportToast(`Assignment failed: ${err.message}`, "error");
  }
}

// ── Fitting Inbox (Model Fitting → Sample Data) ─────────────────────────────

function _getProcessedByFilament() {
  // Group processed samples by their variable filament
  const byFil = {};
  for (const exp of data.samples) {
    if (exp._processing_status !== "processed") continue;
    const fid = exp.variable_filament_id;
    if (!byFil[fid]) byFil[fid] = [];
    byFil[fid].push(exp);
  }
  return byFil;
}

/**
 * Build an aligned strip grid container HTML.
 * Uses CSS grid with border+swatch proportional columns so the extracted strip image,
 * mock swatch row, and diagram table all align perfectly.
 *
 * @param {object} opts
 * @param {object} opts.exp - sample object (needs strip_definition.strip_geometry)
 * @param {string} opts.stripImgHtml - HTML for the strip image (goes in asg-strip-img)
 * @param {string} opts.swatchesHtml - HTML for mock swatch tiles (goes in asg-swatches grid)
 * @param {string} opts.diagramHtml - HTML for diagram row (<tr>...</tr> content)
 * @param {number} [opts.n] - number of swatches (auto-detected from geometry if omitted)
 * @param {string} [opts.maxWidth] - CSS max-width for the container (default "500px")
 * @param {string} [opts.extraClass] - additional CSS class(es) for the outer grid
 * @param {Set|null} [opts.excludedSwatchIndexes] - set of swatch indexes to shade on the strip image
 * @returns {string} HTML string
 */
function buildAlignedStripGrid(opts) {
  const geom = opts.exp?.strip_definition?.strip_geometry || {};
  const borderMm = geom.border_mm || 3.0;
  const stepWMm = geom.step_w_mm || 12.0;
  const n = opts.n || geom.num_swatches || 8;
  // Grid: borderCol | swatch1 | swatch2 | ... | swatchN | borderCol
  // Border is borderMm wide, each swatch is stepWMm wide.
  // Use fr units proportional to mm.
  const gridCols = `${borderMm}fr repeat(${n}, ${stepWMm}fr) ${borderMm}fr`;
  const maxW = opts.maxWidth || "500px";
  const cls = "aligned-strip-grid" + (opts.extraClass ? " " + opts.extraClass : "");

  // Exclusion overlay: semi-transparent gray over excluded swatch regions on the strip image.
  // Uses the same proportional grid as the outer container so overlays align exactly with swatches.
  let overlayHtml = "";
  const excSet = opts.excludedSwatchIndexes;
  if (excSet && excSet.size > 0) {
    const overlayTiles = [`<div></div>`]; // left border spacer
    for (let i = 0; i < n; i++) {
      if (excSet.has(i)) {
        overlayTiles.push(`<div class="asg-excl-overlay"></div>`);
      } else {
        overlayTiles.push(`<div></div>`);
      }
    }
    overlayTiles.push(`<div></div>`); // right border spacer
    overlayHtml = `<div class="asg-excl-row" style="grid-template-columns:${gridCols}">${overlayTiles.join("")}</div>`;
  }

  return `<div class="${cls}" style="grid-template-columns:${gridCols};max-width:${maxW}">
    <div class="asg-strip-img">${opts.stripImgHtml}${overlayHtml}</div>
    <div class="asg-swatches" style="grid-template-columns:repeat(${n},1fr)">${opts.swatchesHtml}</div>
    <table class="mini-strip-table asg-diagram"><tr>${opts.diagramHtml}</tr></table>
  </div>`;
}

function _shortSampleDescription(exp) {
  const thicknesses = exp.variable_thicknesses_mm || [];
  if (thicknesses.length === 0) return "—";
  const first = Number(thicknesses[0]);
  const second = thicknesses.length > 1 ? Number(thicknesses[1]) : null;
  const last = Number(thicknesses[thicknesses.length - 1]);
  if (first === 0 || first < 0.001) {
    // Blank in first swatch: "0.00, [swatch2]-[swatch8] mm"
    return second != null
      ? `0.00, ${second.toFixed(2)}\u2013${last.toFixed(2)} mm`
      : `0.00 mm`;
  }
  // No blank: "[swatch1]-[swatch8] mm"
  return `${first.toFixed(2)}\u2013${last.toFixed(2)} mm`;
}

// ══════════════════════════════════════════════════════════════════════════════
// Model Fitting subtab (Images -> Model Fitting)
// ══════════════════════════════════════════════════════════════════════════════

function _mfBuildMockStrip(swatches, label) {
  // Simple mock strip from hex values — no exclusion logic
  if (!swatches || swatches.length === 0) return "";
  const n = swatches.length;
  const tiles = swatches.map(sw => {
    const hex = (label === "Pred." ? sw.predicted_hex : sw.measured_hex) || "#888";
    return `<div class="mf-mock-swatch" style="background:${hex}"></div>`;
  }).join("");
  return `
    <div class="mf-strip-row">
      <span class="mf-strip-label">${label}</span>
      <div class="mf-mock-strip" style="grid-template-columns:repeat(${n},1fr)">${tiles}</div>
    </div>`;
}

function _mfBuildDiagram(exp) {
  // Clean swatch diagram — no exclusion logic
  const variableHex = exp.variable_hex || "#dddddd";
  const variableText = textColor(variableHex);
  const thicknesses = exp.variable_thicknesses_mm || [];
  const cells = thicknesses.map(t =>
    `<td style="background:${variableHex};color:${variableText}">${Number(t).toFixed(2)}</td>`
  ).join("");
  return `<table class="mini-strip-table mf-diagram"><tr>${cells}</tr></table>`;
}

function _mfBuildDeltaEBars(swatches) {
  // Bar graph aligned 1:1 with swatches above, with Y-axis scale
  if (!swatches || swatches.length === 0) return "";
  const n = swatches.length;
  const rawMax = Math.max(...swatches.map(s => s.delta_e || 0), 0.01);
  // Round up to a clean tick value
  const maxDE = rawMax <= 1 ? Math.ceil(rawMax * 10) / 10
              : rawMax <= 5 ? Math.ceil(rawMax)
              : Math.ceil(rawMax / 5) * 5;
  const barH = 32; // max bar height px

  const bars = swatches.map(sw => {
    const de = sw.delta_e || 0;
    const h = Math.max(Math.round(de / maxDE * barH), 1);
    const cls = de < 2 ? "mf-bar-good" : de < 5 ? "mf-bar-ok" : "mf-bar-bad";
    return `<div class="mf-de-bar ${cls}" style="height:${h}px" title="ΔE ${de.toFixed(1)}"></div>`;
  }).join("");

  const midDE = maxDE / 2;

  return `
    <div class="mf-strip-row">
      <span class="mf-strip-label">ΔE</span>
      <div class="mf-de-chart">
        <div class="mf-de-axis">
          <span class="mf-de-tick">${maxDE.toFixed(2)}</span>
          <span class="mf-de-tick">${midDE.toFixed(2)}</span>
          <span class="mf-de-tick">0</span>
        </div>
        <div class="mf-de-bar-row" style="grid-template-columns:repeat(${n},1fr);height:${barH}px">${bars}</div>
      </div>
    </div>`;
}

function _nmEvidenceLabel(key) {
  const labels = {
    single_color_sandwich: "single color sandwich",
    cross_color_multilayer_sandwich: "multicolor sandwich",
    color_over_white: "color over white",
    multicolor_over_white: "multicolor over white",
    naked_single_filament: "naked single",
    white_only: "white only",
    unsupported_or_diagnostic: "diagnostic",
  };
  return labels[key] || String(key || "unknown").replaceAll("_", " ");
}

function _nmSampleSearchText(sample) {
  const parts = [sample.sample_id, sample.evidence_class, sample.stack_signature];
  for (const swatch of sample.swatches || []) {
    for (const layer of swatch.stack || []) {
      const fil = filamentMeta(layer.filament_id) || {};
      parts.push(layer.filament_id, fil.color_name, fil.manufacturer);
    }
  }
  return parts.filter(Boolean).join(" ").toLowerCase();
}

function _nmFilteredSamples() {
  const payload = photoStackModelState.predictions || {};
  const raw = Array.isArray(payload.samples) ? payload.samples : [];
  const q = (photoStackModelState.search || "").trim().toLowerCase();
  const evidenceClass = photoStackModelState.evidenceClass || "all";
  return raw
    .filter((sample) => evidenceClass === "all" || sample.evidence_class === evidenceClass)
    .filter((sample) => !q || _nmSampleSearchText(sample).includes(q))
    .sort((a, b) => _nmSampleNumber(a.sample_id) - _nmSampleNumber(b.sample_id));
}

function _nmBuildChipStrip(swatches, key, label) {
  if (!swatches || swatches.length === 0) return "";
  const tiles = swatches.map((swatch) => {
    const hex = swatch?.[key]?.hex || "#eeeeee";
    return `<div class="nm-chip" style="background:${hex}" title="${_escAttr(hex)}"></div>`;
  }).join("");
  return `
    <div class="nm-chip-row">
      <span class="nm-chip-label">${label}</span>
      <div class="nm-chip-strip" style="grid-template-columns:repeat(${swatches.length},1fr)">${tiles}</div>
    </div>`;
}

function _nmPredictionSpecs() {
  const payload = photoStackModelState.predictions || {};
  const rows = Array.isArray(payload.prediction_rows) ? payload.prediction_rows : [];
  if (rows.length) return rows;
  const sample = (payload.samples || []).find((entry) => (entry.swatches || []).some((swatch) => swatch.predictions));
  const predictions = sample?.swatches?.find((swatch) => swatch.predictions)?.predictions || {};
  const keys = Object.keys(predictions);
  if (keys.length) {
    const labels = {
      photo_stack_corrected: "Photo stack + corrections",
      photo_stack: "Photo stack",
    };
    return keys.map((key) => ({ key, label: labels[key] || key }));
  }
  return [{ key: "predicted", label: "Photo stack" }];
}

function _nmBuildPredictionChipStrip(swatches, spec) {
  if (!swatches || swatches.length === 0) return "";
  const key = spec?.key || "predicted";
  const label = spec?.label || key;
  const tiles = swatches.map((swatch) => {
    const pred = (swatch.predictions && swatch.predictions[key]) || (key === "predicted" ? swatch.predicted : null) || swatch.predicted || {};
    const hex = pred.hex || "#eeeeee";
    return `<div class="nm-chip" style="background:${hex}" title="${_escAttr(hex)}"></div>`;
  }).join("");
  return `
    <div class="nm-chip-row">
      <span class="nm-chip-label">${_escHtml(label)}</span>
      <div class="nm-chip-strip" style="grid-template-columns:repeat(${swatches.length},1fr)">${tiles}</div>
    </div>`;
}

function _nmDeltaClass(delta) {
  const d = Number(delta);
  if (!Number.isFinite(d)) return "";
  if (d <= 0.035) return "is-good";
  if (d <= 0.075) return "is-ok";
  return "is-bad";
}

function _nmBuildDeltaPills(swatches, spec = null) {
  if (!swatches || swatches.length === 0) return "";
  const key = spec?.key || null;
  const label = spec?.label || "d";
  const pills = swatches.map((swatch) => {
    const pred = key && swatch.predictions ? swatch.predictions[key] : null;
    const d = Number(pred?.oklab_delta ?? swatch.oklab_delta);
    const text = Number.isFinite(d) ? d.toFixed(3) : "—";
    return `<span class="nm-delta-pill ${_nmDeltaClass(d)}">${text}</span>`;
  }).join("");
  return `
    <div class="nm-delta-row">
      <span class="nm-chip-label">${_escHtml(label === "d" ? "d" : `d ${label}`)}</span>
      <div class="nm-delta-strip" style="grid-template-columns:repeat(${swatches.length},1fr)">${pills}</div>
    </div>`;
}

function _nmLayerLabel(fid) {
  const fil = filamentMeta(fid) || {};
  return fil.color_name || fid || "unknown";
}

function _nmSampleNumber(sampleId) {
  const match = String(sampleId || "").match(/\d+/);
  return match ? Number(match[0]) : Number.MAX_SAFE_INTEGER;
}

function _nmBuildDiagramExp(sample) {
  return (data.samples || []).find((exp) => exp.sample_id === sample.sample_id) || null;
}

function _nmBuildDiagramLabels(sample) {
  const exp = _nmBuildDiagramExp(sample);
  if (!exp) return "";
  const roles = [...(exp.roles || [])].sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
  return roles.map((role) => {
    const fid = role.filament_id || "";
    if (!fid) return "";
    const fil = filamentMeta(fid) || {};
    const hex = fil.hex || "#dddddd";
    return `
      <div class="nm-diagram-label">
        <span class="color-chip tiny" style="background:${hex}"></span>
        <span>${_escHtml(_nmLayerLabel(fid))}</span>
      </div>`;
  }).filter(Boolean).join("");
}

function _nmBuildStackDiagram(sample) {
  const exp = _nmBuildDiagramExp(sample);
  if (!exp) return `<div class="strip-diagram-contract-error">Missing canonical sample data</div>`;
  const labels = _nmBuildDiagramLabels(sample);
  return `
    <div class="nm-diagram-wrap">
      <div class="sample-strip-tight nm-stack-diagram">${buildStripMiniTable(exp)}</div>
      <div class="nm-diagram-labels">${labels}</div>
    </div>`;
}

function _nmBuildSampleRow(sample) {
  const swatches = sample.swatches || [];
  const predictionSpecs = _nmPredictionSpecs();
  const mean = Number(sample.mean_oklab_delta);
  const max = Number(sample.max_oklab_delta);
  const stats = [
    Number.isFinite(mean) ? `mean ${mean.toFixed(3)}` : null,
    Number.isFinite(max) ? `max ${max.toFixed(3)}` : null,
  ].filter(Boolean).join(" / ");
  return `
    <div class="nm-review-row">
      <div class="nm-review-main">
        <div class="nm-review-head">
          <strong>${_escHtml(sample.sample_id || "")}</strong>
          <span>${_escHtml(_nmEvidenceLabel(sample.evidence_class))}</span>
          ${stats ? `<span class="mono">${stats}</span>` : ""}
        </div>
        ${_nmBuildChipStrip(swatches, "measured", "Measured")}
        ${predictionSpecs.map((spec) => _nmBuildPredictionChipStrip(swatches, spec)).join("")}
      </div>
      <div class="nm-error-cell">${predictionSpecs.map((spec) => _nmBuildDeltaPills(swatches, spec)).join("")}</div>
      <div class="nm-diagram-cell">${_nmBuildStackDiagram(sample)}</div>
    </div>`;
}

function _nmRenderProgressPanel() {
  const job = photoStackModelState.status;
  if (!photoStackModelState.isFitting || !job) return "";
  const progress = job.progress || {};
  const pct = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  const message = progress.message || "Fitting photo stack model";
  const phase = progress.phase || job.status || "running";
  const target = progress.target ? `<span class="mono">${_escHtml(progress.target)}</span>` : "";
  return `
    <div class="nm-progress-panel">
      <div class="validate-progress-head">
        <strong>Fitting Photo Stack Model</strong>
        <span class="small-copy mono">${pct.toFixed(0)}%</span>
      </div>
      <div class="profile-progress-track">
        <div class="profile-progress-fill validate-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="validate-progress-meta">
        <span>${_escHtml(message)}</span>
        <span class="mono">${_escHtml(phase)}</span>
        ${target}
      </div>
    </div>`;
}

function _nmUpdateProgressOnly() {
  const panel = document.querySelector(".nm-progress-panel");
  const html = _nmRenderProgressPanel();
  if (panel && html) {
    panel.outerHTML = html;
    return;
  }
  renderModelFitting();
}

function _nmRenderCandidatePanel() {
  const candidate = photoStackModelState.candidate;
  const predictions = photoStackModelState.predictions;
  const samples = _nmFilteredSamples();
  const evidenceClasses = Array.from(new Set(((predictions?.samples) || []).map((sample) => sample.evidence_class).filter(Boolean))).sort();
  const classOptions = [`<option value="all">All sample types</option>`].concat(
    evidenceClasses.map((cls) => `<option value="${_escAttr(cls)}"${photoStackModelState.evidenceClass === cls ? " selected" : ""}>${_escHtml(_nmEvidenceLabel(cls))}</option>`)
  ).join("");
  const loading = photoStackModelState.loadingCandidate ? `<div class="mf-placeholder">Loading candidate...</div>` : "";
  const error = photoStackModelState.error ? `<div class="mf-placeholder nm-error-text">${_escHtml(photoStackModelState.error)}</div>` : "";
  const rows = samples.map((sample) => _nmBuildSampleRow(sample)).join("");
  const runId = candidate?.run_id || photoStackModelState.latest?.run_id || "";
  const engine = candidate?.review_summary?.engine_status || predictions?.engine_status || "";
  const sampleCount = predictions?.total_samples ?? predictions?.sample_count ?? 0;
  return `
    <div class="nm-panel" data-panel="photo-stack-fit">
      <div class="nm-panel-head">
        <div>
          <div class="nm-title">Photo Stack Model</div>
          <div class="small-copy">Latest Photo Stack model generated by the Fit Models workflow.</div>
        </div>
        <div class="nm-head-actions">
          ${runId ? `<span class="toolbar-chip mono">${_escHtml(runId)}</span>` : ""}
          ${engine ? `<span class="toolbar-chip">${_escHtml(engine)}</span>` : ""}
        </div>
      </div>
      ${_nmRenderProgressPanel()}
      <div class="nm-controls">
        <button class="ghost-button small" id="nmRefreshBtn" ${photoStackModelState.loadingCandidate ? "disabled" : ""}>Reload Latest Model</button>
        <input class="nm-search" id="nmSearchInput" type="search" value="${_escAttr(photoStackModelState.search)}" placeholder="Search samples or filaments">
        <select class="nm-filter" id="nmEvidenceFilter">${classOptions}</select>
        <span class="small-copy">${samples.length} shown / ${sampleCount || 0} samples</span>
      </div>
      ${loading}
      ${error}
      ${!loading && !error && !candidate ? `<div class="mf-placeholder">No photo stack model has been generated yet. Run Fit Models to create one.</div>` : ""}
      ${rows ? `<div class="nm-review-list">${rows}</div>` : (!loading && candidate ? `<div class="mf-placeholder">No samples match the current filters.</div>` : "")}
    </div>`;
}

function _ctRenderProgressPanel() {
  const job = cameraTransformState.status;
  if (!cameraTransformState.isBuilding || !job) return "";
  const progress = job.progress || {};
  const pct = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  const message = progress.message || "Building Camera Transform";
  const phase = progress.phase || job.status || "running";
  return `
    <div class="nm-progress-panel">
      <div class="validate-progress-head">
        <strong>Building Camera Transform</strong>
        <span class="small-copy mono">${pct.toFixed(0)}%</span>
      </div>
      <div class="profile-progress-track">
        <div class="profile-progress-fill validate-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="validate-progress-meta">
        <span>${_escHtml(message)}</span>
        <span class="mono">${_escHtml(phase)}</span>
      </div>
    </div>`;
}

function _ctRenderPanel() {
  const current = cameraTransformState.current || {};
  const status = current.status || "missing";
  const manifest = current.manifest || {};
  const corpus = manifest.corpus || {};
  const validation = manifest.validation_dE76_CIELAB || {};
  const error = cameraTransformState.error ? `<div class="mf-placeholder nm-error-text">${_escHtml(cameraTransformState.error)}</div>` : "";
  const createdAt = current.created_at || manifest.created_at || "";
  const validationMean = Number(current.validation_mean_de76 ?? validation.mean);
  const corpusSize = Number(current.corpus_size ?? corpus.usable_swatch_count);
  const statusText = status === "present" ? "READY" : status === "invalid" ? "INVALID" : "MISSING";
  return `
    <div class="nm-panel" data-panel="camera-transform">
      <div class="nm-panel-head">
        <div>
          <div class="nm-title">Camera Transform</div>
          <div class="small-copy">Transmission to camera-rendered appearance transform used by generator ingress and previews.</div>
        </div>
        <div class="nm-head-actions">
          <span class="toolbar-chip">${_escHtml(statusText)}</span>
          ${createdAt ? `<span class="toolbar-chip mono">${_escHtml(createdAt)}</span>` : ""}
        </div>
      </div>
      ${_ctRenderProgressPanel()}
      <div class="nm-controls">
        <button class="ghost-button small" id="ctRefreshBtn">Reload Status</button>
        ${Number.isFinite(validationMean) ? `<span class="small-copy">cross-validation mean dE76 ${validationMean.toFixed(3)}</span>` : ""}
        ${Number.isFinite(corpusSize) ? `<span class="small-copy">${corpusSize} swatches</span>` : ""}
      </div>
      ${status !== "present" && current.reason ? `<div class="mf-placeholder">${_escHtml(current.reason)}</div>` : ""}
      ${error}
    </div>`;
}

async function _ctLoadCurrent(force = false) {
  if (!force && cameraTransformState.current) return;
  if (typeof fetchCameraTransformCurrent !== "function") {
    cameraTransformState.error = "Camera Transform API is unavailable in static mode.";
    return;
  }
  try {
    cameraTransformState.current = await fetchCameraTransformCurrent();
    cameraTransformState.error = null;
  } catch (err) {
    cameraTransformState.error = String(err?.message || err || "Failed to load Camera Transform status");
  }
}

async function pollCameraTransformJob(jobId) {
  while (true) {
    const status = await fetchCameraTransformJobStatus(jobId);
    cameraTransformState.status = status;
    cameraTransformState.isBuilding = status.status === "queued" || status.status === "running";
    // Targeted panel swap only — NOT renderModelFitting(). A full re-render here
    // rebuilt the entire (heavy) Model Fitting tab incl. the photo-stack candidate
    // chip grid every 700ms for the whole multi-minute build, ballooning browser
    // memory to ~12 GB. _ctRenderPanel() embeds the live progress sub-panel, so
    // this updates the progress bar without rebuilding the rest of the tab.
    _refreshCameraTransformPanel();
    if (status.status === "completed") return status.result || status;
    if (status.status === "failed") throw new Error(status.error || "Camera Transform build failed");
    await sleep(700);
  }
}

async function handleCameraTransformBuild() {
  cameraTransformState.isBuilding = true;
  cameraTransformState.error = null;
  renderModelFitting();
  try {
    const job = await startCameraTransformBuildJob();
    cameraTransformState.jobId = job?.job_id || null;
    cameraTransformState.status = job;
    const result = job?.job_id ? await pollCameraTransformJob(job.job_id) : job;
    await _ctLoadCurrent(true);
    const warning = result?.warnings?.length ? ` (${result.warnings[0]})` : "";
    showImportToast(`Camera Transform ready${warning}`, result?.warnings?.length ? "warning" : "success");
  } catch (err) {
    cameraTransformState.error = err.message || "Camera Transform build failed";
    showImportToast(cameraTransformState.error, "error");
  } finally {
    cameraTransformState.isBuilding = false;
    cameraTransformState.jobId = null;
    renderModelFitting();
  }
}

async function _nmLoadLatestCandidate(force = false) {
  if (photoStackModelState.loadingCandidate) return;
  if (!force && photoStackModelState.latest && photoStackModelState.candidate && photoStackModelState.predictions) return;
  if (!force && photoStackModelState.error) return;
  if (typeof fetchPhotoStackLatest !== "function") {
    photoStackModelState.error = "Photo stack model API is unavailable in static mode.";
    return;
  }
  photoStackModelState.loadingCandidate = true;
  photoStackModelState.error = null;
  try {
    const latest = await fetchPhotoStackLatest();
    const runId = latest?.run_id;
    if (!runId) throw new Error("Latest candidate response did not include run_id");
    const [candidate, predictions] = await Promise.all([
      fetchPhotoStackCandidate(runId),
      fetchPhotoStackSamplePredictions(runId, { limit: 1000 }),
    ]);
    photoStackModelState.latest = latest;
    photoStackModelState.candidate = candidate;
    photoStackModelState.predictions = predictions;
  } catch (err) {
    photoStackModelState.latest = null;
    photoStackModelState.candidate = null;
    photoStackModelState.predictions = null;
    photoStackModelState.error = String(err?.message || err || "Failed to load candidate");
  } finally {
    photoStackModelState.loadingCandidate = false;
  }
}

async function pollPhotoStackJob(jobId) {
  while (true) {
    const status = await fetchPhotoStackJobStatus(jobId);
    photoStackModelState.status = status;
    photoStackModelState.isFitting = status.status === "queued" || status.status === "running";
    _nmUpdateProgressOnly();
    if (status.status === "completed") return status.result || status;
    if (status.status === "failed") throw new Error(status.error || "Photo stack model fit failed");
    await sleep(700);
  }
}

async function handlePhotoStackFit() {
  photoStackModelState.isFitting = true;
  photoStackModelState.error = null;
  renderModelFitting();
  try {
    const job = await startPhotoStackFitJob();
    photoStackModelState.jobId = job?.job_id || null;
    photoStackModelState.status = job;
    const result = job?.job_id ? await pollPhotoStackJob(job.job_id) : job;
    await _nmLoadLatestCandidate(true);
    showImportToast(`Photo stack candidate ready${result?.run_id ? `: ${result.run_id}` : ""}`, "success");
  } catch (err) {
    photoStackModelState.error = err.message || "Photo stack model fit failed";
    showImportToast(photoStackModelState.error, "error");
  } finally {
    photoStackModelState.isFitting = false;
    photoStackModelState.jobId = null;
    renderModelFitting();
  }
}

function _mfBuildSampleCard(pred, exp) {
  // Card for summary view: measured strip, predicted strip, diagram, ΔE bars
  const sid = pred.sample_id;
  const swatches = pred.swatches || [];
  const desc = _shortSampleDescription(exp);
  const nFixed = pred.n_fixed || 0;
  const layerLabel = nFixed === 0 ? "" : `, ${nFixed + 1}L`;

  let cardHtml = `<div class="mf-sample-card" data-mf-sample="${sid}">`;
  cardHtml += `<div class="mf-card-header"><span class="mono mf-card-id">${sid}</span><span class="mf-card-desc">${desc}${layerLabel}</span></div>`;

  if (!pred.can_predict) {
    const missing = (pred.missing_profiles || []).join(", ");
    cardHtml += `<div class="mf-placeholder">Cannot predict — missing profile for: ${missing}</div>`;
  } else {
    cardHtml += _mfBuildMockStrip(swatches, "Meas.");
    cardHtml += _mfBuildMockStrip(swatches, "Pred.");
    cardHtml += `<div class="mf-strip-row"><span class="mf-strip-label"></span>${_mfBuildDiagram(exp)}</div>`;
    cardHtml += _mfBuildDeltaEBars(swatches);
  }

  cardHtml += `</div>`;
  return cardHtml;
}

function _mfBuildDetailView(pred, exp) {
  // Individual sample detail: strip photo, measured, predicted, diagram, ΔE bars
  const sid = pred.sample_id;
  const swatches = pred.swatches || [];
  const desc = _shortSampleDescription(exp);
  const nFixed = pred.n_fixed || 0;

  // Fixed layers info
  let fixedHtml = "";
  const fixedLines = sampleFilamentRoleLines(exp).filter((line) => line.roleKind === "fixed");
  if (fixedLines.length > 0) {
    fixedHtml = fixedLines.map((line) => {
      const ft = line.thicknessMm != null ? line.thicknessMm.toFixed(2) : "?";
      return `<div class="mf-detail-fixed">${_escHtml(line.layerLabel)}: ${ft}mm ${_escHtml(line.name)} <span class="color-chip" style="background:${line.hex || '#ddd'}"></span></div>`;
    }).join("");
  }

  let html = `<div class="mf-detail-view">`;
  html += `<div class="mf-detail-back"><a href="#" id="mfBackToGrid">&larr; All Samples</a></div>`;
  html += `<div class="mf-detail-header">`;
  html += `<span class="mono" style="font-size:14px;font-weight:700">${sid}</span>`;
  html += `<span style="font-size:12px;color:var(--muted)">${desc}${nFixed > 0 ? `, ${nFixed + 1}L` : ""}</span>`;
  html += `</div>`;
  if (fixedHtml) html += `<div class="mf-detail-fixed-block">${fixedHtml}</div>`;

  // Strip image
  html += `<div class="mf-detail-strip-img"><img src="${sampleThumbnailUrl(sid, "strip", true)}" alt="Extracted strip" onerror="this.outerHTML='<span class=small-copy>No strip image</span>'"></div>`;

  if (!pred.can_predict) {
    const missing = (pred.missing_profiles || []).join(", ");
    html += `<div class="mf-placeholder">Cannot predict — missing profile for: ${missing}</div>`;
  } else {
    html += _mfBuildMockStrip(swatches, "Meas.");
    html += _mfBuildMockStrip(swatches, "Pred.");
    html += `<div class="mf-strip-row"><span class="mf-strip-label"></span>${_mfBuildDiagram(exp)}</div>`;
    html += _mfBuildDeltaEBars(swatches);
  }

  html += `</div>`;
  return html;
}

function _mfLoadingPlaceholder(message = "Loading fit results") {
  return `
    <div class="mf-placeholder mf-loading-state">
      <span class="proc-spinner" aria-hidden="true"></span>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function _mfRenderLayout({ filListHtml, sampleCardsHtml, samples, selSample, rightPaneTitle, rightPaneContent }) {
  tableContainer.innerHTML = `
    ${renderProfileFitProgressPanel()}
    ${_ctRenderPanel()}
    ${_nmRenderCandidatePanel()}
    <div class="fi-layout">
      <div class="fi-left-pane">
        <div class="fi-pane-title">Filaments</div>
        <div class="fi-filament-list">${filListHtml}</div>
      </div>
      <div class="fi-center-pane">
        <div class="fi-pane-title">Samples</div>
        <div class="fi-sample-list">
          <div class="fi-sample-card fi-all-samples-card${!selSample ? " is-selected" : ""}" id="mfAllSamplesCard">
            <div class="fi-sample-header">
              <span style="font-size:11px;font-weight:600">All Samples</span>
              <span class="fi-sample-count">${samples.length}</span>
            </div>
          </div>
          ${sampleCardsHtml}
        </div>
      </div>
      <div class="fi-right-pane">
        <div class="fi-pane-title">${rightPaneTitle}</div>
        <div class="mf-right-content">${rightPaneContent}</div>
      </div>
    </div>
  `;
  _bindModelFittingActions();
  _bindCameraTransformActions();
  _bindPhotoStackPanelActions();
}

async function refreshModelFittingReviewAfterWorkflow() {
  modelFittingState.predictionCache = {};
  photoStackModelState.latest = null;
  photoStackModelState.candidate = null;
  photoStackModelState.predictions = null;
  photoStackModelState.error = null;
  cameraTransformState.current = null;
  cameraTransformState.error = null;
  await Promise.allSettled([
    _nmLoadLatestCandidate(true),
    _ctLoadCurrent(true),
  ]);
  if (currentMode === "imageProcessing" && currentSubtab === "model_fitting") {
    renderModelFitting();
  }
}

async function refreshModelsAfterWorkflow() {
  if (typeof handleRefresh === "function") {
    await handleRefresh({ ensureAssets: false });
  }
  invalidateModelingPayloads();
}

async function openFitModelsWorkflow(button = null, onComplete = refreshModelsAfterWorkflow) {
  if (fitModelsWorkflowLaunchBusy) return;
  if (document.querySelector(".maintenance-workflow-overlay")) {
    showImportToast("Close the active maintenance workflow before opening another.", "warning");
    return;
  }
  const originalText = button?.textContent || "";
  fitModelsWorkflowLaunchBusy = true;
  if (button) {
    button.disabled = true;
    button.textContent = "Loading...";
  }
  try {
    const operation = await maintenanceOperationById("refit_calibration_models");
    if (!operation) {
      showImportToast("Fit Models is not available", "error");
      return;
    }
    if (operation.enabled === false) {
      showImportToast(operation.unavailable_reason || operation.disabled_reason || "Fit Models is unavailable", "warning");
      return;
    }
    showMaintenanceWorkflow(operation, onComplete);
  } catch (err) {
    showImportToast(err.message || "Could not open Fit Models workflow", "error");
  } finally {
    fitModelsWorkflowLaunchBusy = false;
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function openFitModelsWorkflowFromModelFitting(button = null) {
  return openFitModelsWorkflow(button);
}

async function renderModelFitting() {
  const renderSeq = ++modelFittingState.renderSeq;
  tableToolbar.className = "toolbar-inline";
  if (!photoStackModelState.requestedInitialLoad && !photoStackModelState.loadingCandidate) {
    photoStackModelState.requestedInitialLoad = true;
    _nmLoadLatestCandidate(false)
      .then(() => {
        if (currentMode === "imageProcessing" && currentSubtab === "model_fitting") {
          _refreshPhotoStackPanel();
        }
      })
      .catch((err) => {
        photoStackModelState.error = err.message || String(err);
        if (currentMode === "imageProcessing" && currentSubtab === "model_fitting") {
          _refreshPhotoStackPanel();
        }
      });
  }
  if (!cameraTransformState.requestedInitialLoad) {
    cameraTransformState.requestedInitialLoad = true;
    _ctLoadCurrent(false)
      .then(() => {
        if (currentMode === "imageProcessing" && currentSubtab === "model_fitting") {
          renderModelFitting();
        }
      })
      .catch((err) => {
        cameraTransformState.error = err.message || String(err);
        if (currentMode === "imageProcessing" && currentSubtab === "model_fitting") {
          renderModelFitting();
        }
      });
  }

  const byFil = _getProcessedByFilament();
  const filamentIds = Object.keys(byFil).sort((a, b) => {
    const fa = filamentMeta(a), fb = filamentMeta(b);
    return (fa?.color_name || a).localeCompare(fb?.color_name || b);
  });

  const totalProcessed = Object.values(byFil).reduce((s, arr) => s + arr.length, 0);
  tableSummary.textContent = `${filamentIds.length} filaments with data, ${totalProcessed} processed samples`;

  const fitAllRunning = isProfileFitJobRunning();
  tableToolbar.innerHTML = `
    <button class="primary-button small" type="button" id="mfFitModelsBtn">Fit Models</button>`;

  // Left pane: filament list
  const selFil = modelFittingState.selectedFilamentId || filamentIds[0] || null;
  if (!modelFittingState.selectedFilamentId && selFil) modelFittingState.selectedFilamentId = selFil;

  const filListHtml = filamentIds.map(fid => {
    const fil = filamentMeta(fid) || {};
    const count = byFil[fid].length;
    const selected = fid === selFil;
    // Check if profile exists
    const hasProfile = fil.has_profile;
    const statusDot = hasProfile
      ? '<span class="mf-profile-dot mf-has-profile" title="Profile saved"></span>'
      : '<span class="mf-profile-dot" title="No profile yet"></span>';
    return `
      <div class="fi-filament-item${selected ? " is-selected" : ""}" data-mf-filament="${fid}">
        <span class="color-chip" style="background:${fil.hex || '#ddd'}"></span>
        <span class="fi-filament-name">${fil.color_name || fid}</span>
        ${statusDot}
        <span class="fi-sample-count">${count}</span>
      </div>`;
  }).join("");

  // Center pane: sample cards (compact list)
  const samples = selFil ? (byFil[selFil] || []) : [];
  const selSample = modelFittingState.selectedSampleId;

  const sampleCardsHtml = samples.map(exp => {
    const sid = exp.sample_id;
    const desc = _shortSampleDescription(exp);
    const selected = sid === selSample;
    const fixedLines = sampleFilamentRoleLines(exp).filter((line) => line.roleKind === "fixed");

    let fixedLayersHtml = "";
    if (fixedLines.length > 0) {
      fixedLayersHtml = fixedLines.map((line) => {
        const ft = line.thicknessMm != null ? line.thicknessMm.toFixed(2) : "?";
        return `<div class="fi-fixed-layer">${_escHtml(line.layerLabel)}: ${ft}mm ${_escHtml(line.name)} <span class="color-chip tiny" style="background:${line.hex || '#ddd'}"></span></div>`;
      }).join("");
    }

    return `
      <div class="fi-sample-card${selected ? " is-selected" : ""}" data-mf-sample="${sid}">
        <div class="fi-sample-header">
          <span class="mono fi-sample-id">${sid}</span>
          <span class="fi-sample-desc">${desc}</span>
        </div>
        ${fixedLayersHtml ? `<div class="fi-fixed-layers">${fixedLayersHtml}</div>` : ""}
        <div class="fi-strip-thumb">
          <img src="${sampleThumbnailUrl(sid, "strip", true)}" alt="" onerror="this.style.display='none'">
        </div>
      </div>`;
  }).join("");

  // Right pane content — depends on whether predictions are loaded
  let rightPaneContent = "";
  let rightPaneTitle = "Fit Results";

  // Fetch predictions for selected filament (cached)
  let predictions = null;
  const needsPredictionFetch = !!(selFil && !modelFittingState.predictionCache[selFil] && !fitAllRunning);
  if (needsPredictionFetch) {
    _mfRenderLayout({
      filListHtml,
      sampleCardsHtml,
      samples,
      selSample,
      rightPaneTitle,
      rightPaneContent: _mfLoadingPlaceholder("Loading fit results"),
    });
  }

  if (selFil) {
    if (modelFittingState.predictionCache[selFil]) {
      predictions = modelFittingState.predictionCache[selFil];
    } else if (!fitAllRunning) {
      try {
        predictions = await fetchSamplePredictions(selFil);
        if (predictions.ok) {
          modelFittingState.predictionCache[selFil] = predictions;
        }
      } catch (e) {
        // No profile yet — that's ok
        predictions = null;
      }
    }
  }

  if (
    renderSeq !== modelFittingState.renderSeq ||
    currentMode !== "imageProcessing" ||
    currentSubtab !== "model_fitting" ||
    modelFittingState.selectedFilamentId !== selFil
  ) {
    return;
  }

  if (selSample && predictions && predictions.ok) {
    // Individual sample detail view
    const allPreds = [...(predictions.groups.single || []), ...(predictions.groups.two_layer || []), ...(predictions.groups.three_layer || [])];
    const pred = allPreds.find(p => p.sample_id === selSample);
    const exp = samples.find(e => e.sample_id === selSample);
    if (pred && exp) {
      rightPaneTitle = `${selSample} Detail`;
      rightPaneContent = _mfBuildDetailView(pred, exp);
    } else if (exp) {
      rightPaneTitle = `${selSample} Detail`;
      rightPaneContent = `<div class="mf-placeholder">No legacy spline prediction data available. Run Fit Models first.</div>`;
    }
  } else if (predictions && predictions.ok) {
    // Summary view — grouped sections
    const sectionOrder = [
      { key: "single", label: "Single Filament + Cross-cal" },
      { key: "two_layer", label: "2-Layer Strips" },
      { key: "three_layer", label: "3-Layer Strips" },
    ];

    let sectionsHtml = "";
    for (const sec of sectionOrder) {
      const preds = predictions.groups[sec.key] || [];
      if (preds.length === 0) continue;

      sectionsHtml += `<div class="mf-section">`;
      sectionsHtml += `<div class="mf-section-header">${sec.label} <span class="muted-line">(${preds.length})</span></div>`;
      sectionsHtml += `<div class="mf-section-cards">`;
      for (const pred of preds) {
        const exp = samples.find(e => e.sample_id === pred.sample_id);
        if (exp) sectionsHtml += _mfBuildSampleCard(pred, exp);
      }
      sectionsHtml += `</div></div>`;
    }

    rightPaneContent = sectionsHtml || '<div class="mf-placeholder">No legacy spline predictions available. Run Fit Models first.</div>';
  } else if (predictions && !predictions.has_profile) {
    rightPaneContent = `<div class="mf-placeholder">No legacy spline profile for this filament. Run Fit Models to generate profiles.</div>`;
  } else if (fitAllRunning) {
    rightPaneContent = `<div class="mf-placeholder">Profile fitting is running. Fit results will update when fitting completes.</div>`;
  } else {
    rightPaneContent = `<div class="mf-placeholder">Select a filament to view fit results.</div>`;
  }

  _mfRenderLayout({ filListHtml, sampleCardsHtml, samples, selSample, rightPaneTitle, rightPaneContent });
}

function _refreshPhotoStackPanel() {
  const panel = document.querySelector('[data-panel="photo-stack-fit"]');
  if (panel) {
    panel.outerHTML = _nmRenderCandidatePanel();
    _bindPhotoStackPanelActions();
  }
}

function _refreshCameraTransformPanel() {
  const panel = document.querySelector('[data-panel="camera-transform"]');
  if (panel) {
    panel.outerHTML = _ctRenderPanel();
    _bindCameraTransformActions();
  }
}

function _bindCameraTransformActions() {
  const refreshBtn = document.getElementById("ctRefreshBtn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      await _ctLoadCurrent(true);
      _refreshCameraTransformPanel();
    });
  }
}

function _bindPhotoStackPanelActions() {
  const nmRefreshBtn = document.getElementById("nmRefreshBtn");
  if (nmRefreshBtn) {
    nmRefreshBtn.addEventListener("click", async () => {
      await _nmLoadLatestCandidate(true);
      _refreshPhotoStackPanel();
    });
  }

  const nmSearchInput = document.getElementById("nmSearchInput");
  if (nmSearchInput) {
    nmSearchInput.addEventListener("input", () => {
      photoStackModelState.search = nmSearchInput.value || "";
      _refreshPhotoStackPanel();
      const refreshed = document.getElementById("nmSearchInput");
      refreshed?.focus();
    });
  }

  const nmEvidenceFilter = document.getElementById("nmEvidenceFilter");
  if (nmEvidenceFilter) {
    nmEvidenceFilter.addEventListener("change", () => {
      photoStackModelState.evidenceClass = nmEvidenceFilter.value || "all";
      _refreshPhotoStackPanel();
    });
  }
}

function _bindModelFittingActions() {
  const fitModelsBtn = document.getElementById("mfFitModelsBtn");
  if (fitModelsBtn) {
    fitModelsBtn.onclick = () => openFitModelsWorkflowFromModelFitting(fitModelsBtn);
  }

  // Filament selection
  tableContainer.querySelectorAll("[data-mf-filament]").forEach(el => {
    el.addEventListener("click", () => {
      modelFittingState.selectedFilamentId = el.dataset.mfFilament;
      modelFittingState.selectedSampleId = null;
      renderModelFitting();
    });
  });

  // Sample selection
  tableContainer.querySelectorAll("[data-mf-sample]").forEach(el => {
    el.addEventListener("click", () => {
      const sid = el.dataset.mfSample;
      modelFittingState.selectedSampleId = modelFittingState.selectedSampleId === sid ? null : sid;
      renderModelFitting();
    });
  });

  // All Samples card
  const allCard = document.getElementById("mfAllSamplesCard");
  if (allCard) {
    allCard.addEventListener("click", () => {
      modelFittingState.selectedSampleId = null;
      renderModelFitting();
    });
  }

  // Back to grid
  const backLink = document.getElementById("mfBackToGrid");
  if (backLink) {
    backLink.addEventListener("click", e => {
      e.preventDefault();
      modelFittingState.selectedSampleId = null;
      renderModelFitting();
    });
  }

}

const MODEL_OVERVIEW_ORDER = [
  { key: "legacy_spline", label: "Color Model v1" },
  { key: "photo_stack_v2", label: "Color Model v2" },
  { key: "camera_transform", label: "Camera Transform" },
];

function modelOverviewStatusMeta(entry = {}) {
  const raw = String(entry.status || entry.model_currentness?.currentness_state || "missing").toLowerCase();
  if (raw === "current") return { label: "Current", cls: "is-current" };
  if (raw === "stale") return { label: "Stale", cls: "is-stale" };
  if (raw === "missing") return { label: "Missing", cls: "is-missing" };
  if (raw === "invalid" || raw === "failed") return { label: "Failed", cls: "is-failed" };
  return { label: "Unknown", cls: "is-unknown" };
}

function modelOverviewDateText(entry = {}) {
  const raw = entry.generated_at || entry.model_currentness?.generated_at || "";
  const text = String(raw || "");
  const isoDate = text.match(/^(\d{4}-\d{2}-\d{2})/);
  if (isoDate) return isoDate[1];
  if (!text) return "";
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return "";
  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function modelOverviewStatusTitle(label, entry = {}) {
  const meta = modelOverviewStatusMeta(entry);
  const parts = [`${label}: ${meta.label}`];
  if (entry.generated_at) parts.push(`Generated ${entry.generated_at}`);
  const staleReason = entry.stale_reason || entry.model_currentness?.stale_reason;
  if (staleReason) parts.push(staleReason);
  return parts.join(" | ");
}

function renderModelOverviewStatusLine(modelStatus = data.model_status) {
  const models = modelStatus?.models || {};
  return MODEL_OVERVIEW_ORDER.map(({ key, label }) => {
    const entry = models[key] || { status: "missing" };
    const meta = modelOverviewStatusMeta(entry);
    const date = modelOverviewDateText(entry);
    return `
      <span class="model-status-text ${meta.cls}" title="${_escAttr(modelOverviewStatusTitle(label, entry))}">
        <strong>${_escHtml(label)}:</strong>
        <span><span class="model-status-state">${_escHtml(meta.label.toUpperCase())}</span>${date ? ` (${_escHtml(date)})` : ""}</span>
      </span>
    `;
  }).join("");
}

function renderModelOverviewHeaderStatus() {
  return renderModelOverviewStatusLine(data.model_status);
}

function renderModelOverviewStatusBlock(modelStatus = data.model_status) {
  return renderModelOverviewStatusLine(modelStatus?.models ? modelStatus : data.model_status);
}

function invalidateModelingPayloads() {
  modelingState.overview = null;
  modelingState.samples = null;
  modelingState.filaments = null;
  modelingState.detailSamplePayload = null;
  modelingState.detailFilamentPayload = null;
  modelingState.error = "";
}

function modelingCurrentTab() {
  return currentSubtab || "overview";
}

function modelingPayloadForTab(tab) {
  if (tab === "samples") return modelingState.samples;
  if (tab === "filaments") return modelingState.filaments;
  return modelingState.overview;
}

function setModelingPayloadForTab(tab, payload) {
  if (tab === "samples") modelingState.samples = payload;
  else if (tab === "filaments") modelingState.filaments = payload;
  else modelingState.overview = payload;
  if (payload?.model_status) data.model_status = payload.model_status;
}

async function fetchAllModelingSamples(options = {}) {
  const firstPage = await fetchModelingSamples({
    ...options,
    offset: 0,
    limit: MODELING_REVIEW_PAGE_SIZE,
  });
  const rows = [...(firstPage.rows || [])];
  const total = Number(firstPage.total || rows.length);
  let offset = rows.length;

  while (offset < total) {
    const page = await fetchModelingSamples({
      ...options,
      offset,
      limit: MODELING_REVIEW_PAGE_SIZE,
    });
    const pageRows = page.rows || [];
    if (!pageRows.length) break;
    rows.push(...pageRows);
    offset += pageRows.length;
  }

  return {
    ...firstPage,
    offset: 0,
    limit: rows.length,
    rows,
  };
}

async function loadModelingTab(tab = modelingCurrentTab(), options = {}) {
  if (modelingState.loadingTab === tab && !options.force) return;
  modelingState.loadingTab = tab;
  modelingState.error = "";
  if (currentMode === "profiles") renderModelsView({ skipEnsure: true });
  try {
    let payload;
    if (tab === "samples") {
      payload = await fetchAllModelingSamples({
        filter: modelingState.samplesFilter,
        filament_ids: modelingState.samplesFilamentIds,
        sort: modelingState.samplesSort,
        sort_dir: modelingState.samplesSortDir,
      });
    } else if (tab === "filaments") {
      payload = await fetchModelingFilaments({
        sort: modelingState.filamentsSort,
        sort_dir: modelingState.filamentsSortDir,
        limit: 500,
      });
    } else {
      payload = await fetchModelingOverview();
    }
    setModelingPayloadForTab(tab, payload);
  } catch (err) {
    modelingState.error = err.message || "Failed to load Modeling data";
  } finally {
    if (modelingState.loadingTab === tab) modelingState.loadingTab = null;
    if (currentMode === "profiles") renderModelsView({ skipEnsure: true });
  }
}

function ensureModelingTabLoaded(tab = modelingCurrentTab()) {
  if (modelingPayloadForTab(tab) || modelingState.loadingTab === tab) return;
  loadModelingTab(tab);
}

function modelStatusAttentionHtml() {
  const models = data.model_status?.models || {};
  const stale = Object.values(models).filter((entry) => entry.status === "stale");
  const missing = Object.values(models).filter((entry) => entry.status === "missing");
  if (!stale.length && !missing.length) return "";
  const parts = [];
  if (stale.length) parts.push(`${stale.length} stale`);
  if (missing.length) parts.push(`${missing.length} missing`);
  return `
    <div class="model-review-alert">
      <strong>Fit Models needed</strong>
      <span>${_escHtml(parts.join(" · "))}</span>
    </div>
  `;
}

function modelingLoadingHtml(tab) {
  if (modelingState.error) {
    return `<div class="model-review-empty is-error">${_escHtml(modelingState.error)}</div>`;
  }
  if (modelingState.loadingTab === tab) {
    return `<div class="model-review-empty">Loading ${_escHtml(tab)}...</div>`;
  }
  return `<div class="model-review-empty">No Modeling data loaded yet.</div>`;
}

function modelReviewOverviewTableHtml(rows = [], emptyMessage = "No rows") {
  const body = rows.length ? rows.map((row) => `
    <tr>
      <td><strong>${_escHtml(row.label || "")}</strong></td>
      <td>${_escHtml(row.value || "")}</td>
      <td>${row.detail ? _escHtml(row.detail) : `<span class="muted-line">None</span>`}</td>
    </tr>
  `).join("") : `<tr><td colspan="3"><div class="model-review-empty">${_escHtml(emptyMessage)}</div></td></tr>`;
  return `
    <table class="data-table compact-table model-review-summary-table model-review-overview-table">
      <thead>
        <tr>
          <th>Item</th>
          <th>Value</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderModelingOverview(payload) {
  if (!payload) return modelingLoadingHtml("overview");
  const summary = payload.inclusion_summary || {};
  const attention = payload.attention || {};
  const stale = attention.stale_models || [];
  const missing = attention.missing_models || [];
  const filamentBlocked = Number(attention.samples_with_excluded_filaments || summary.samples_blocked_by_filament || 0);
  const evidenceRows = [
    {
      label: "Evidence Samples",
      value: `${Number(summary.samples_included || 0)} / ${Number(summary.samples_total || 0)}`,
      detail: "with included swatches",
    },
    {
      label: "Swatches",
      value: `${Number(summary.swatches_included || 0)} / ${Number(summary.swatches_total || 0)}`,
      detail: "included",
    },
    {
      label: "Excluded Samples",
      value: String(Number(summary.samples_excluded || 0)),
      detail: "",
    },
    {
      label: "Excluded Swatches",
      value: String(Number(summary.swatches_excluded || 0)),
      detail: "",
    },
  ];
  const attentionRows = [
    ...stale.map((item) => ({
      label: item.label || item.model_kind,
      value: "Stale",
      detail: item.reason || "Model needs refitting",
    })),
    ...missing.map((item) => ({
      label: item.label || item.model_kind,
      value: "Missing",
      detail: "No current model artifact",
    })),
    ...(filamentBlocked ? [{
      label: "Samples blocked by excluded filaments",
      value: String(filamentBlocked),
      detail: "",
    }] : []),
    ...(Number(attention.samples_without_accepted_extraction || 0) ? [{
      label: "Samples without accepted extraction",
      value: String(Number(attention.samples_without_accepted_extraction || 0)),
      detail: "",
    }] : []),
  ];
  return `
    <div class="model-review-page model-review-overview-page">
      ${modelStatusAttentionHtml()}
      <section class="model-review-section">
        <div class="model-review-section-head">
          <h3>Current Evidence</h3>
        </div>
        ${modelReviewOverviewTableHtml(evidenceRows)}
      </section>
      <section class="model-review-section">
        <div class="model-review-section-head">
          <h3>Needs Attention</h3>
        </div>
        ${modelReviewOverviewTableHtml(attentionRows, "No immediate blockers.")}
      </section>
    </div>
  `;
}

function modelReviewStripHtml(colors = []) {
  const swatches = (colors || []).slice(0, 24);
  if (!swatches.length || !swatches.some(Boolean)) {
    return `<span class="model-review-strip is-empty"></span>`;
  }
  return `
    <span class="model-review-strip" aria-hidden="true">
      ${swatches.map((hex) => `<span style="background:${_escAttr(hex || "#f1f1f1")}"></span>`).join("")}
    </span>
  `;
}

function modelReviewDetailStripHtml(colors = []) {
  const swatches = (colors || []);
  if (!swatches.length || !swatches.some(Boolean)) {
    return `<span class="model-review-detail-strip is-empty"></span>`;
  }
  return `
    <span class="model-review-detail-strip" aria-hidden="true">
      ${swatches.map((hex) => `<span style="background:${_escAttr(hex || "#f1f1f1")}"></span>`).join("")}
    </span>
  `;
}

function modelReviewDetailSeriesHtml(label, series) {
  const payload = series || {};
  const reason = payload.reason ? `<span class="small-copy">${_escHtml(payload.reason)}</span>` : "";
  return `
    <div class="model-review-detail-row">
      <div class="model-review-detail-label">${_escHtml(label)}</div>
      <div class="model-review-detail-strip-cell">
        ${modelReviewDetailStripHtml(payload.hex || [])}
        ${payload.available ? "" : reason}
      </div>
    </div>
  `;
}

function modelReviewDetailSwatchCount(sample = {}, detail = {}) {
  const counts = [
    Number(sample.swatch_count || 0),
    (sample.geometry?.variable_thicknesses_mm || []).length,
  ];
  Object.values(detail.domains || {}).forEach((domainPayload) => {
    ["measured", "photo_stack_v2", "legacy_spline"].forEach((key) => {
      const series = domainPayload?.[key] || {};
      if (Array.isArray(series.hex)) counts.push(series.hex.length);
      ["corrected", "uncorrected"].forEach((variant) => {
        const variantSeries = series?.[variant] || {};
        if (Array.isArray(variantSeries.hex)) counts.push(variantSeries.hex.length);
      });
    });
  });
  return Math.max(1, ...counts.map((count) => Number.isFinite(count) ? count : 0));
}

function modelReviewStripAlignmentStyle(swatchCount) {
  const n = Math.max(1, Number(swatchCount || 0));
  const borderMm = 3;
  const stepWMm = 12;
  const totalMm = (2 * borderMm) + (n * stepWMm);
  const left = (borderMm / totalMm) * 100;
  const width = ((n * stepWMm) / totalMm) * 100;
  return `--model-render-left:${left}%;--model-render-width:${width}%;`;
}

function modelReviewNormalizeHexes(series = {}, swatchCount = 1) {
  const source = Array.isArray(series.hex) ? series.hex : [];
  const count = Math.max(1, Number(swatchCount || source.length || 1));
  return Array.from({ length: count }, (_item, index) => source[index] || "");
}

function modelReviewDomainStripHtml(series = {}, swatchCount = 1) {
  const hexes = modelReviewNormalizeHexes(series, swatchCount);
  if (!hexes.some(Boolean)) {
    return `<span class="model-review-domain-strip is-empty" aria-hidden="true"></span>`;
  }
  return `
    <span class="model-review-domain-strip" style="grid-template-columns:repeat(${hexes.length}, minmax(0, 1fr))" aria-hidden="true">
      ${hexes.map((hex) => `<span class="${hex ? "" : "is-missing"}" style="background:${_escAttr(hex || "#eef1f3")}"></span>`).join("")}
    </span>
  `;
}

const MODEL_REVIEW_OKLAB_ERROR_SCALE_MAX = 0.15;
const MODEL_REVIEW_OKLAB_ERROR_LANDMARKS = [
  { value: 0.02, label: "JND" },
  { value: 0.05, label: "Noticeable" },
  { value: 0.10, label: "Large" },
];

function modelReviewLinearTriplet(value) {
  if (!Array.isArray(value) || value.length < 3) return null;
  const triplet = value.slice(0, 3).map((item) => Number(item));
  if (triplet.some((item) => !Number.isFinite(item))) return null;
  return triplet.map((item) => Math.max(0, Math.min(1, item)));
}

function modelReviewLinearToOklab(value) {
  const rgb = modelReviewLinearTriplet(value);
  if (!rgb) return null;
  const [r, g, b] = rgb;
  const l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b;
  const m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b;
  const s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b;
  const lRoot = Math.cbrt(l);
  const mRoot = Math.cbrt(m);
  const sRoot = Math.cbrt(s);
  return {
    L: 0.2104542553 * lRoot + 0.7936177850 * mRoot - 0.0040720468 * sRoot,
    a: 1.9779984951 * lRoot - 2.4285922050 * mRoot + 0.4505937099 * sRoot,
    b: 0.0259040371 * lRoot + 0.7827717662 * mRoot - 0.8086757660 * sRoot,
  };
}

function modelReviewHexToOklab(hex) {
  const text = String(hex || "").trim();
  const match = text.match(/^#?([0-9a-fA-F]{6})$/);
  if (!match) return null;
  const srgb = [0, 2, 4].map((offset) => parseInt(match[1].slice(offset, offset + 2), 16) / 255);
  const linear = srgb.map((value) => (
    value <= 0.04045
      ? value / 12.92
      : Math.pow((value + 0.055) / 1.055, 2.4)
  ));
  return modelReviewLinearToOklab(linear);
}

function modelReviewOklabDeltaFromLabs(labA, labB) {
  if (!labA || !labB) return null;
  const dL = labA.L - labB.L;
  const da = labA.a - labB.a;
  const db = labA.b - labB.b;
  return Math.sqrt(dL * dL + da * da + db * db);
}

function modelReviewSeriesOklab(series = {}, domain = "appearance", index = 0) {
  if (domain === "transmission") {
    const rgb = Array.isArray(series.rgb) ? series.rgb[index] : null;
    return modelReviewLinearToOklab(rgb);
  }
  const hex = Array.isArray(series.hex) ? series.hex[index] : "";
  return modelReviewHexToOklab(hex);
}

function modelReviewOklabErrors(measuredSeries = {}, predictedSeries = {}, swatchCount = 1, domain = "appearance") {
  const measured = modelReviewNormalizeHexes(measuredSeries, swatchCount);
  return measured.map((_hex, index) => modelReviewOklabDeltaFromLabs(
    modelReviewSeriesOklab(measuredSeries, domain, index),
    modelReviewSeriesOklab(predictedSeries, domain, index),
  ));
}

function modelReviewFormatOklabError(value) {
  return Number.isFinite(value) ? value.toFixed(3) : "-";
}

function modelReviewOklabErrorStats(errors = []) {
  const finite = errors.filter((value) => Number.isFinite(value));
  if (!finite.length) return null;
  return {
    mean: finite.reduce((sum, value) => sum + value, 0) / finite.length,
    max: Math.max(...finite),
  };
}

function modelReviewOklabMeanText(errors = []) {
  const stats = modelReviewOklabErrorStats(errors);
  return stats ? modelReviewFormatOklabError(stats.mean) : "-";
}

function modelReviewOklabErrorGraphHtml(v2Errors = [], v1Errors = [], swatchCount = 1) {
  const count = Math.max(1, Number(swatchCount || v2Errors.length || v1Errors.length || 1));
  const v2Values = Array.from({ length: count }, (_item, index) => v2Errors[index]);
  const v1Values = Array.from({ length: count }, (_item, index) => v1Errors[index]);
  const finite = [...v2Values, ...v1Values].filter((value) => Number.isFinite(value));
  if (!finite.length) {
    return `
      <div class="model-review-domain-error is-empty">
        <div class="model-review-domain-error-summary">
          <span>OKLab error</span>
          <span>unavailable</span>
        </div>
        <div class="model-review-domain-error-empty">No comparable prediction</div>
      </div>
    `;
  }
  const maxValue = MODEL_REVIEW_OKLAB_ERROR_SCALE_MAX;
  const landmarks = MODEL_REVIEW_OKLAB_ERROR_LANDMARKS.map((mark) => {
    const bottomPct = Math.max(0, Math.min(100, (mark.value / maxValue) * 100));
    return `
      <div class="model-review-domain-error-landmark" style="bottom:${bottomPct}%"></div>
    `;
  }).join("");
  const axisLabels = [
    { value: maxValue, label: modelReviewFormatOklabError(maxValue) },
    ...MODEL_REVIEW_OKLAB_ERROR_LANDMARKS,
    { value: 0, label: "0" },
  ].map((mark) => {
    const bottomPct = Math.max(0, Math.min(100, (mark.value / maxValue) * 100));
    return `<span style="bottom:${bottomPct}%">${_escHtml(mark.label)}</span>`;
  }).join("");
  const pairedBars = Array.from({ length: count }, (_item, index) => {
    const v2 = v2Values[index];
    const v1 = v1Values[index];
    const bar = (value, cls, label) => {
      const heightPct = Number.isFinite(value) ? Math.max(4, Math.min(100, (value / maxValue) * 100)) : 0;
      const clipNote = Number.isFinite(value) && value > maxValue ? " (clipped to scale)" : "";
      const title = Number.isFinite(value)
        ? `Swatch ${index + 1}: ${label} OKLab error ${modelReviewFormatOklabError(value)}${clipNote}`
        : `Swatch ${index + 1}: ${label} OKLab error unavailable`;
      return `<span class="model-review-domain-error-bar ${cls}${Number.isFinite(value) ? "" : " is-missing"}" style="height:${heightPct}%" title="${_escAttr(title)}"></span>`;
    };
    return `
      <span class="model-review-domain-error-pair">
        ${bar(v2, "is-v2", "Color Model V2")}
        ${bar(v1, "is-v1", "Color Model V1")}
      </span>
    `;
  }).join("");
  return `
    <div class="model-review-domain-error">
      <div class="model-review-domain-error-summary">
        <span>OKLab error</span>
        <span><i class="model-review-domain-error-key is-v2"></i>V2 · <i class="model-review-domain-error-key is-v1"></i>V1</span>
      </div>
      <div class="model-review-domain-error-plot" aria-label="OKLab prediction error graph">
        <div class="model-review-domain-error-axis" aria-hidden="true">${axisLabels}</div>
        <div class="model-review-domain-error-stage">
          ${landmarks}
          <div class="model-review-domain-error-bars" style="grid-template-columns:repeat(${count}, minmax(0, 1fr))">
            ${pairedBars}
          </div>
        </div>
      </div>
    </div>
  `;
}

function modelReviewDomainSeriesHtml(label, series, swatchCount) {
  const payload = series || {};
  const reason = !payload.available && payload.reason
    ? `<span class="model-review-domain-reason">${_escHtml(payload.reason)}</span>`
    : "";
  return `
    <div class="model-review-domain-series">
      <div class="model-review-domain-series-label">${_escHtml(label)}</div>
      <div class="model-review-domain-strip-track">
        <div class="model-review-domain-strip-inner">
          ${modelReviewDomainStripHtml(payload, swatchCount)}
        </div>
      </div>
      ${reason}
    </div>
  `;
}

function modelReviewDomainErrorSeriesHtml(v2Errors, v1Errors, swatchCount) {
  return `
    <div class="model-review-domain-series model-review-domain-error-series">
      <div class="model-review-domain-series-label">Prediction Error</div>
      <div class="model-review-domain-strip-track">
        <div class="model-review-domain-strip-inner">
          ${modelReviewOklabErrorGraphHtml(v2Errors, v1Errors, swatchCount)}
        </div>
      </div>
    </div>
  `;
}

function modelReviewDomainPanelHtml(sample, detail, domain, variant, swatchCount) {
  const domainPayload = (detail.domains || {})[domain] || {};
  const measured = domainPayload.measured || {};
  const photoStack = (domainPayload.photo_stack_v2 || {})[variant] || {};
  const legacySpline = (domainPayload.legacy_spline || {})[variant] || {};
  const domainName = domain === "appearance" ? "Appearance" : "Transmission";
  const sampleId = sample.sample_id || "";
  const stripSrc = domain === "appearance" && sampleId ? sampleThumbnailUrl(sampleId, "strip") : "";
  const photoStackErrors = modelReviewOklabErrors(measured, photoStack, swatchCount, domain);
  const legacySplineErrors = modelReviewOklabErrors(measured, legacySpline, swatchCount, domain);
  const referenceBlock = stripSrc ? `
        <div class="model-review-domain-image-block">
          <div class="model-review-domain-series-label">Extracted Strip</div>
          <div class="model-review-domain-image">
            <img src="${_escAttr(stripSrc)}"
                 alt="${_escAttr(`${sampleId} extracted strip`)}"
                 data-model-strip-source
                 onerror="this.closest('.model-review-domain-image-block').remove()">
          </div>
        </div>
  ` : `
        <div class="model-review-domain-reference-block">
          <div class="model-review-domain-series-label">Strip</div>
          <div class="model-review-domain-reference-shell">
            <div class="sample-strip-tight">${modelReviewStripDiagramHtml(sample)}</div>
          </div>
        </div>
  `;

  return `
    <section class="model-review-domain-panel" aria-label="${_escAttr(domainName)} Domain">
      <h4 class="model-review-domain-title">${_escHtml(domainName)} Domain</h4>
      <div class="model-review-domain-strip-sync"
           data-model-strip-sync
           data-border-mm="3"
           data-step-w-mm="12"
           data-swatches="${_escAttr(String(swatchCount))}"
           style="${modelReviewStripAlignmentStyle(swatchCount)}">
        ${referenceBlock}
        ${modelReviewDomainSeriesHtml(`Measured ${domainName}`, measured, swatchCount)}
        ${modelReviewDomainSeriesHtml(`Predicted ${domainName} (Color Model V2)`, photoStack, swatchCount)}
        ${modelReviewDomainSeriesHtml(`Predicted ${domainName} (Color Model V1)`, legacySpline, swatchCount)}
        ${modelReviewDomainErrorSeriesHtml(photoStackErrors, legacySplineErrors, swatchCount)}
      </div>
    </section>
  `;
}

function applyModelReviewStripGeometry(img) {
  const sync = img?.closest?.("[data-model-strip-sync]");
  if (!sync || !img.naturalWidth || !img.naturalHeight) return;
  const sw = Number(img.naturalWidth);
  const borderMm = Number(sync.dataset.borderMm || 3);
  const stepWMm = Number(sync.dataset.stepWMm || 12);
  const n = Math.max(1, Number(sync.dataset.swatches || 8));
  const deskewPad = 6;
  const totalWmm = (2 * borderMm) + (n * stepWMm);
  const plasticWPx = Math.max(1, sw - 2 * deskewPad);
  const pxPerMm = plasticWPx / totalWmm;
  const innerX = Math.round(deskewPad + borderMm * pxPerMm);
  const innerW = Math.round(n * stepWMm * pxPerMm);
  sync.style.setProperty("--model-render-left", `${(innerX / sw) * 100}%`);
  sync.style.setProperty("--model-render-width", `${(innerW / sw) * 100}%`);
}

function bindModelReviewStripGeometry(root = detailSidebar) {
  root?.querySelectorAll?.("img[data-model-strip-source]").forEach((img) => {
    if (img.complete && img.naturalWidth) {
      applyModelReviewStripGeometry(img);
    } else {
      img.addEventListener("load", () => applyModelReviewStripGeometry(img), { once: true });
    }
  });
}

function modelReviewFilamentStackHtml(filaments = []) {
  if (!filaments.length) return `<span class="muted-line">No filaments</span>`;
  return filaments.map((fil) => `
    <div class="model-review-stack-line">
      <span class="color-chip" style="background:${_escAttr(fil.hex || "#999999")}"></span>
      <span>${_escHtml(fil.name || fil.filament_id || "")}</span>
    </div>
  `).join("");
}

function modelReviewSampleFilamentRoleLabel(fil = {}) {
  if (fil.role_kind === "variable") return "Variable";
  if (fil.role_kind === "fixed") {
    const fixedThickness = Number(fil.fixed_thickness_mm);
    return Number.isFinite(fixedThickness) ? `Fixed ${fixedThickness.toFixed(2)} mm` : "Fixed";
  }
  return fil.role_label || "";
}

function modelReviewSampleFilamentsHtml(sample = {}) {
  const filaments = sample.filaments || [];
  if (!filaments.length) return "";
  return `
    <div class="model-review-sample-filaments" aria-label="Sample filaments">
      <span class="model-review-sample-filaments-label">Filaments</span>
      <div class="model-review-sample-filament-chips">
        ${filaments.map((fil) => {
          const name = fil.name || fil.display_name || fil.filament_id || "";
          const roleLabel = modelReviewSampleFilamentRoleLabel(fil);
          return `
            <span class="model-review-sample-filament-chip">
              <span class="color-chip" style="background:${_escAttr(fil.hex || "#999999")}"></span>
              <span class="model-review-sample-filament-name">${_escHtml(name)}</span>
              ${roleLabel ? `<span class="model-review-sample-filament-role">${_escHtml(roleLabel)}</span>` : ""}
              ${modelReviewFilamentExclusionPillHtml(fil)}
            </span>
          `;
        }).join("")}
      </div>
    </div>
  `;
}

function modelReviewStripDiagramHtml(row) {
  const expLike = {
    roles: row?.filaments || [],
    variable_thicknesses_mm: row?.geometry?.variable_thicknesses_mm || [],
  };
  return buildStripMiniTable(expLike);
}

function modelReviewExcludedFilaments(row = {}) {
  const payload = Array.isArray(row.excluded_model_filaments) ? row.excluded_model_filaments : [];
  if (payload.length) return payload;
  return (row.filaments || []).filter((fil) => fil.exclude_from_model);
}

function modelReviewExcludedFilamentText(row = {}) {
  const names = modelReviewExcludedFilaments(row)
    .map((fil) => fil.name || fil.display_name || fil.filament_id)
    .filter(Boolean);
  if (!names.length) return "";
  return names.join(", ");
}

function modelReviewFitStateHtml(row) {
  const excludedFilamentText = modelReviewExcludedFilamentText(row);
  if (excludedFilamentText) {
    const sampleNote = row.fit_exclude ? " Sample is also excluded." : "";
    return `<span class="status-pill model-review-state incomplete" title="Model fitting blocked by excluded filament: ${_escAttr(excludedFilamentText)}.${_escAttr(sampleNote)}">Filament Excluded</span>`;
  }
  if (row.fit_exclude) return `<span class="status-pill model-review-state planned">Excluded</span>`;
  if (Number(row.excluded_swatch_count || 0) > 0) {
    return `<span class="status-pill model-review-state stale">${Number(row.excluded_swatch_count || 0)} swatch${Number(row.excluded_swatch_count || 0) === 1 ? "" : "es"} excluded</span>`;
  }
  return `<span class="status-pill model-review-state processed">Included</span>`;
}

function modelReviewExcludedSwatchSet(sample = {}) {
  return new Set((sample.excluded_swatches || []).map((idx) => Number(idx)).filter((idx) => Number.isInteger(idx) && idx >= 0));
}

function modelReviewSwatchThicknessLabel(sample = {}, index = 0) {
  const thicknesses = sample.geometry?.variable_thicknesses_mm || [];
  const value = Number(thicknesses[index]);
  return Number.isFinite(value) ? value.toFixed(2) : String(index + 1);
}

function modelReviewSampleExclusionGeometryHtml(sample = {}, swatchCount = 1) {
  const roles = [...(sample.filaments || [])].sort((a, b) => Number(b.role_index || 0) - Number(a.role_index || 0));
  const variableRole = roles.find((role) => role.role_kind === "variable") || {};
  const variableHex = variableRole.hex || "#dddddd";
  const variableText = textColor(variableHex);
  const excluded = modelReviewExcludedSwatchSet(sample);
  const count = Math.max(1, Number(swatchCount || sample.swatch_count || (sample.geometry?.variable_thicknesses_mm || []).length || 1));
  const variableCells = Array.from({ length: count }, (_item, index) => {
    const isExcluded = excluded.has(index);
    const label = modelReviewSwatchThicknessLabel(sample, index);
    return `
      <td>
        <button class="model-swatch-exclusion-cell ${isExcluded ? "is-excluded" : "is-included"}"
                type="button"
                data-model-toggle-swatch="${index}"
                data-next-excluded="${isExcluded ? "false" : "true"}"
                style="--swatch-bg:${_escAttr(variableHex)};--swatch-fg:${_escAttr(variableText)}"
                title="Swatch ${index + 1}: ${isExcluded ? "excluded" : "included"} in model fits"
                aria-pressed="${isExcluded ? "true" : "false"}">
          <span class="model-swatch-exclusion-label">${_escHtml(label)}</span>
          <span class="model-swatch-exclusion-status ${isExcluded ? "is-excluded" : "is-included"}">${isExcluded ? "Exclude" : "Include"}</span>
        </button>
      </td>
    `;
  }).join("");
  const rows = roles.length ? roles.map((role) => {
    if (role.role_kind === "variable") return `<tr>${variableCells}</tr>`;
    const fixedHex = role.hex || "#eeeeee";
    const fixedText = textColor(fixedHex);
    const fixedThickness = Number(role.fixed_thickness_mm);
    const fixedLabel = Number.isFinite(fixedThickness)
      ? `${fixedThickness.toFixed(2)}mm`
      : (role.role_label || "Fixed");
    return `
      <tr>
        <td colspan="${count}">
          <span class="model-swatch-exclusion-fixed" style="background:${_escAttr(fixedHex)};color:${_escAttr(fixedText)}">${_escHtml(fixedLabel)}</span>
        </td>
      </tr>
    `;
  }).join("") : `<tr>${variableCells}</tr>`;
  return `<table class="model-swatch-exclusion-table" aria-label="Sample swatch fit inclusion">${rows}</table>`;
}

function modelReviewSampleExclusionHtml(sample = {}, swatchCount = 1) {
  const sampleId = sample.sample_id || "";
  const excludedFilamentText = modelReviewExcludedFilamentText(sample);
  const fitExcluded = !!sample.fit_exclude;
  const swatchExcludedCount = Number(sample.excluded_swatch_count || modelReviewExcludedSwatchSet(sample).size || 0);
  return `
    <div class="model-sample-exclusion-panel">
      <button class="model-fit-control-button ${fitExcluded ? "is-include" : "is-exclude"}"
              type="button"
              id="modelSampleFitToggle"
              data-model-toggle-sample="${_escAttr(sampleId)}"
              data-next-fit-exclude="${fitExcluded ? "false" : "true"}">
        ${fitExcluded ? "Include Sample" : "Exclude Sample"}
      </button>
      <div class="model-sample-exclusion-summary">
        <strong>${fitExcluded ? "Sample excluded from fits" : "Sample included in fits"}</strong>
        <span>${swatchExcludedCount ? `${swatchExcludedCount} swatch${swatchExcludedCount === 1 ? "" : "es"} excluded` : "No swatches excluded"}</span>
      </div>
      ${excludedFilamentText ? `<span class="status-pill model-review-state incomplete" title="Blocked by excluded filament: ${_escAttr(excludedFilamentText)}">Filament Excluded</span>` : ""}
    </div>
  `;
}

function modelReviewSwatchExclusionHtml(sample = {}, swatchCount = 1) {
  return `
    <div class="model-swatch-exclusion-panel">
      ${modelReviewSampleExclusionGeometryHtml(sample, swatchCount)}
    </div>
  `;
}

function modelingSortArrow(activeKey, key, direction) {
  if (activeKey !== key) return "";
  return direction === "asc" ? " ↓" : " ↑";
}

function modelingAriaSort(activeKey, key, direction) {
  if (activeKey !== key) return "none";
  return direction === "asc" ? "ascending" : "descending";
}

function modelingSortableHeader(label, key, activeKey, direction, scope) {
  return `
    <th class="sortable" data-model-sort-scope="${_escAttr(scope)}" data-model-sort="${_escAttr(key)}" aria-sort="${_escAttr(modelingAriaSort(activeKey, key, direction))}">
      ${_escHtml(label)}${modelingSortArrow(activeKey, key, direction)}
    </th>
  `;
}

function renderModelingSampleDetail(payload) {
  const sample = payload?.sample || {};
  const detail = sample.model_detail || {};
  const variant = modelingDetailSettings.includeCorrections ? "corrected" : "uncorrected";
  const swatchCount = modelReviewDetailSwatchCount(sample, detail);

  return `
    <div class="model-review-detail">
      <div class="model-review-detail-controls" role="group" aria-label="Model detail display options">
        <button class="model-review-toggle-button${modelingDetailSettings.includeCorrections ? " is-active" : ""}" type="button" id="modelDetailCorrectionsToggle" aria-pressed="${modelingDetailSettings.includeCorrections ? "true" : "false"}">Color Corrections: ${modelingDetailSettings.includeCorrections ? "On" : "Off"}</button>
        ${modelReviewSampleFilamentsHtml(sample)}
      </div>
      <div class="model-review-domain-grid">
        ${modelReviewDomainPanelHtml(sample, detail, "transmission", variant, swatchCount)}
        ${modelReviewDomainPanelHtml(sample, detail, "appearance", variant, swatchCount)}
      </div>
      <div class="model-review-exclusion-grid">
        ${buildDrawerFormModule("Sample Exclusion", modelReviewSampleExclusionHtml(sample, swatchCount), { density: "compact", classes: "model-review-detail-module model-sample-exclusion-module" })}
        ${buildDrawerFormModule("Swatch Exclusion", modelReviewSwatchExclusionHtml(sample, swatchCount), { density: "compact", classes: "model-review-detail-module model-swatch-exclusion-module" })}
      </div>
    </div>
  `;
}

function modelReviewSampleNavigationIds() {
  const rows = (modelingState.samples?.rows?.length)
    ? modelingState.samples.rows
    : (modelingState.sampleDetailReturnFilamentPayload?.samples || []);
  return Array.from(new Set((rows || []).map((row) => row.sample_id).filter(Boolean)));
}

function modelReviewSampleNavigationMeta(sampleId) {
  const ids = modelReviewSampleNavigationIds();
  const index = ids.indexOf(sampleId);
  return {
    ids,
    index,
    previousId: index > 0 ? ids[index - 1] : "",
    nextId: index >= 0 && index < ids.length - 1 ? ids[index + 1] : "",
  };
}

function modelReviewSampleHeaderActionsHtml(sampleId) {
  const nav = modelReviewSampleNavigationMeta(sampleId);
  const returnButton = modelingState.sampleDetailReturnSampleContext
    ? `<button class="secondary-button small drawer-header-action" type="button" id="modelSampleReturnSourceSampleBtn">Return to Sample</button>`
    : (modelingState.sampleDetailReturnFilamentId ? `
      <button class="secondary-button small drawer-header-action" type="button" id="modelSampleReturnFilamentBtn">Return to Filament</button>
    ` : "");
  return `
    ${returnButton}
    <button class="secondary-button small drawer-header-action" type="button" id="modelSamplePrevBtn" ${nav.previousId ? "" : "disabled"}>Previous</button>
    <button class="secondary-button small drawer-header-action" type="button" id="modelSampleNextBtn" ${nav.nextId ? "" : "disabled"}>Next</button>
  `;
}

function bindModelingSampleHeaderActions() {
  document.getElementById("modelSampleReturnSourceSampleBtn")?.addEventListener("click", () => {
    const context = modelingState.sampleDetailReturnSampleContext;
    modelingState.sampleDetailReturnSampleContext = null;
    returnToSampleInspectDrawer(context || {});
  });
  document.getElementById("modelSampleReturnFilamentBtn")?.addEventListener("click", async () => {
    const filamentId = modelingState.sampleDetailReturnFilamentId;
    if (!filamentId) return;
    await openModelingFilamentDetailDrawer(filamentId);
  });
  document.getElementById("modelSamplePrevBtn")?.addEventListener("click", async () => {
    await navigateModelingSampleDetail(-1);
  });
  document.getElementById("modelSampleNextBtn")?.addEventListener("click", async () => {
    await navigateModelingSampleDetail(1);
  });
}

function updateModelingSampleDetailHeaderActions(sampleId) {
  detailActionArea.innerHTML = modelReviewSampleHeaderActionsHtml(sampleId);
  bindModelingSampleHeaderActions();
}

async function navigateModelingSampleDetail(direction) {
  if (selectedRecord.kind !== "model_sample" || !selectedRecord.id) return false;
  const nav = modelReviewSampleNavigationMeta(selectedRecord.id);
  const nextId = direction < 0 ? nav.previousId : nav.nextId;
  if (!nextId) return false;
  await openModelingSampleDetailDrawer(nextId, null, { preserveReturn: true });
  return true;
}

function shouldIgnoreModelingSampleArrowKey(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return true;
  const target = event.target;
  if (!(target instanceof Element)) return false;
  return !!target.closest("input, textarea, select, button, a, [contenteditable='true']");
}

function renderLoadedModelingSampleDetail(payload, fallbackSampleId = "") {
  modelingState.detailSamplePayload = payload;
  if (payload?.model_status) data.model_status = payload.model_status;
  const sample = payload?.sample || {};
  const sampleId = sample.sample_id || fallbackSampleId;
  setDrawerHeading(sampleId);
  drawerStatusPill.innerHTML = modelReviewFitStateHtml(sample);
  updateModelingSampleDetailHeaderActions(sampleId);
  detailSidebar.innerHTML = renderModelingSampleDetail(payload);
  bindModelingSampleDetailControls();
}

async function refreshOpenModelingSampleDetail(sampleId) {
  if (selectedRecord.kind !== "model_sample" || selectedRecord.id !== sampleId) return;
  const requestSeq = ++modelingState.detailRequestSeq;
  const payload = await fetchModelingSample(sampleId);
  if (requestSeq !== modelingState.detailRequestSeq || selectedRecord.kind !== "model_sample" || selectedRecord.id !== sampleId) return;
  renderLoadedModelingSampleDetail(payload, sampleId);
}

function bindModelingSampleDetailControls() {
  document.getElementById("modelDetailCorrectionsToggle")?.addEventListener("click", () => {
    modelingDetailSettings = {
      ...modelingDetailSettings,
      includeCorrections: !modelingDetailSettings.includeCorrections,
    };
    persistModelingDetailSettings();
    if (modelingState.detailSamplePayload) {
      detailSidebar.innerHTML = renderModelingSampleDetail(modelingState.detailSamplePayload);
      bindModelingSampleDetailControls();
    }
  });
  const runSampleFitToggle = async (button) => {
    button.disabled = true;
    const nextFitExclude = button.dataset.nextFitExclude === "true";
    await handleModelingSampleToggle(button.dataset.modelToggleSample, nextFitExclude);
    if (document.body.contains(button)) button.disabled = false;
  };
  const sampleFitToggle = document.getElementById("modelSampleFitToggle");
  if (sampleFitToggle) {
    if (sampleFitToggle.dataset.nextFitExclude === "true") {
      bindConfirmAction(sampleFitToggle, {
        armedText: "Confirm Exclude",
        onConfirm: async () => runSampleFitToggle(sampleFitToggle),
      });
    } else {
      sampleFitToggle.addEventListener("click", async () => runSampleFitToggle(sampleFitToggle));
    }
  }
  const runSwatchFitToggle = async (button) => {
    button.disabled = true;
    const sampleId = modelingState.detailSamplePayload?.sample?.sample_id || selectedRecord.id;
    const swatchIndex = Number(button.dataset.modelToggleSwatch);
    const nextExcluded = button.dataset.nextExcluded === "true";
    await handleModelingSampleSwatchToggle(sampleId, swatchIndex, nextExcluded);
    if (document.body.contains(button)) button.disabled = false;
  };
  detailSidebar.querySelectorAll("[data-model-toggle-swatch]").forEach((button) => {
    if (button.dataset.nextExcluded === "true") {
      bindConfirmAction(button, {
        armedText: "Confirm",
        onConfirm: async () => runSwatchFitToggle(button),
      });
      return;
    }
    button.addEventListener("click", async () => runSwatchFitToggle(button));
  });
  bindModelReviewStripGeometry();
}

async function openModelingSampleDetailDrawer(sampleId, returnFocusEl = null, options = {}) {
  if (!recordDrawer || !detailSidebar) return;
  const requestSeq = ++modelingState.detailRequestSeq;
  selectedRecord = { kind: "model_sample", id: sampleId };
  modelingState.detailSamplePayload = null;
  modelingState.detailFilamentPayload = null;
  if (!options.preserveReturn) {
    modelingState.sampleDetailReturnFilamentId = null;
    modelingState.sampleDetailReturnFilamentPayload = null;
    modelingState.sampleDetailReturnSampleContext = null;
  }
  if (options.returnFilamentId || options.returnFilamentPayload) {
    modelingState.sampleDetailReturnFilamentId = options.returnFilamentId || options.returnFilamentPayload?.filament?.filament_id || null;
    modelingState.sampleDetailReturnFilamentPayload = options.returnFilamentPayload || null;
    modelingState.sampleDetailReturnSampleContext = null;
  }
  if (options.returnSampleContext) {
    modelingState.sampleDetailReturnSampleContext = options.returnSampleContext;
    modelingState.sampleDetailReturnFilamentId = null;
    modelingState.sampleDetailReturnFilamentPayload = null;
  }
  closeLinkedSampleDrawer({ restoreFocus: false });
  recordDrawer.classList.remove("narrow-drawer", "sample-set-drawer", "model-filament-drawer");
  recordDrawer.classList.add("sample-expanded");
  setDetailSidebarStackMode("default");
  _filamentDrawerMode = null;
  _filamentDrawerData = null;
  _sampleDrawerMode = null;
  setDrawerHeading(sampleId);
  drawerStatusPill.innerHTML = "";
  detailActionArea.innerHTML = modelReviewSampleHeaderActionsHtml(sampleId);
  bindModelingSampleHeaderActions();
  detailWindowArea.innerHTML = "";
  detailSidebar.innerHTML = `<div class="model-review-empty">Loading model comparison...</div>`;
  openRecordDrawer();

  try {
    const payload = await fetchModelingSample(sampleId);
    if (requestSeq !== modelingState.detailRequestSeq || selectedRecord.kind !== "model_sample" || selectedRecord.id !== sampleId) return;
    renderLoadedModelingSampleDetail(payload, sampleId);
  } catch (err) {
    if (requestSeq !== modelingState.detailRequestSeq || selectedRecord.kind !== "model_sample" || selectedRecord.id !== sampleId) return;
    drawerStatusPill.innerHTML = `<span class="status-pill failed">Failed</span>`;
    detailSidebar.innerHTML = `<div class="model-review-empty is-error">${_escHtml(err.message || "Failed to load model comparison")}</div>`;
    if (returnFocusEl instanceof HTMLElement) returnFocusEl.focus();
  }
}

function modelReviewHealthStatusClass(state) {
  if (state === "good") return "processed";
  if (state === "partial" || state === "sparse" || state === "stale") return "stale";
  if (state === "excluded_only" || state === "missing") return "planned";
  return "none";
}

function modelReviewWhiteCapStatusHtml(filament = {}) {
  return `<span class="status-pill model-review-state ${filament.white_cap_eligible ? "processed" : "none"}">${filament.white_cap_eligible ? "White Cap" : "No White Cap"}</span>`;
}

function modelReviewFilamentExclusionPillHtml(filament = {}) {
  return filament.exclude_from_model
    ? `<span class="status-pill model-review-state planned">Excluded</span>`
    : "";
}

function modelReviewFilamentLookup(filamentId) {
  const id = String(filamentId || "");
  const sourceFilament = (data.filaments || []).find((fil) => fil.filament_id === id);
  if (sourceFilament) {
    return {
      filament_id: id,
      brand: sourceFilament.manufacturer || sourceFilament.brand || "",
      name: sourceFilament.color_name || sourceFilament.display_name || id,
      display_name: sourceFilament.display_name || sourceFilament.color_name || id,
      hex: sourceFilament.hex || "#999999",
    };
  }
  const reviewFilament = (modelingState.filaments?.rows || []).find((fil) => fil.filament_id === id);
  if (reviewFilament) return reviewFilament;
  return { filament_id: id, brand: "", name: id, display_name: id, hex: "#999999" };
}

function modelReviewFilamentFilterHtml() {
  const selectedIds = modelingState.samplesFilamentIds || [];
  const chips = selectedIds.map((id) => {
    const fil = modelReviewFilamentLookup(id);
    return `
      <span class="model-review-filter-chip">
        <span class="color-chip" style="background:${_escAttr(fil.hex || "#999999")}"></span>
        <span>${_escHtml(fil.name || fil.filament_id)}</span>
        <button type="button" data-model-sample-filter-remove="${_escAttr(id)}" aria-label="Remove ${_escAttr(fil.name || id)}">
          <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M2 2 L10 10 M10 2 L2 10"></path>
          </svg>
        </button>
      </span>
    `;
  }).join("");
  return `
    <div class="model-review-filament-filter">
      <button class="secondary-button small model-filter-picker-button" type="button" id="modelSamplesFilamentFilterBtn">Filaments...</button>
      <div class="model-review-filter-chips" aria-label="Selected filament filters">
        ${chips || `<span class="model-review-filter-placeholder">All filaments</span>`}
      </div>
      ${selectedIds.length ? `<button class="ghost-button small model-filter-clear-button" type="button" id="modelSamplesClearFilaments">Clear</button>` : ""}
    </div>
  `;
}

function modelReviewThicknessHtml(thickness = {}) {
  if (thickness?.min_mm == null || thickness?.max_mm == null) return `<span class="muted-line">None</span>`;
  return `${Number(thickness.min_mm).toFixed(2)}-${Number(thickness.max_mm).toFixed(2)} mm`;
}

function modelReviewModelStateHtml(modelStatus = {}) {
  return `
    <div class="model-filament-model-state model-filament-model-state-list">
      ${renderModelOverviewStatusBlock(modelStatus)}
    </div>
  `;
}

function modelReviewFilamentSummaryHtml(filament = {}) {
  const excluded = !!filament.exclude_from_model;
  return `
    <div class="model-filament-summary">
      <div class="model-filament-title-line">
        <span class="color-chip" style="background:${_escAttr(filament.hex || "#999999")}"></span>
        <strong>${_escHtml(filament.name || filament.filament_id || "")}</strong>
        <span class="muted-line">${_escHtml(filament.brand || "")}</span>
        ${modelReviewFilamentExclusionPillHtml(filament)}
        <span class="model-filament-title-spacer"></span>
        <button class="model-fit-control-button ${excluded ? "is-include" : "is-exclude"}"
                type="button"
                id="modelFilamentFitToggle"
                data-model-toggle-filament="${_escAttr(filament.filament_id || "")}"
                data-next-exclude="${excluded ? "false" : "true"}">
          ${excluded ? "Include in Fits" : "Exclude from Fits"}
        </button>
      </div>
    </div>
  `;
}

function modelReviewFilamentCoverageHtml(filament = {}) {
  const includedSamples = Number(filament.included_sample_count || 0);
  const totalSamples = Number(filament.sample_count || 0);
  const includedSwatches = Number(filament.included_swatch_count || 0);
  const totalSwatches = Number(filament.swatch_count || 0);
  const excludedSwatches = Number(filament.excluded_swatch_count || 0);
  const excludedSwatchNoun = excludedSwatches === 1 ? "swatch" : "swatches";
  return `
    <div class="model-filament-coverage-lines">
      <div class="model-filament-coverage-line"><strong>${includedSamples}/${totalSamples}</strong> Samples included</div>
      <div class="model-filament-coverage-line"><strong>${includedSwatches}/${totalSwatches}</strong> swatches included</div>
      <div class="model-filament-coverage-line"><strong>${excludedSwatches}</strong> ${excludedSwatchNoun} excluded</div>
    </div>
  `;
}

function modelReviewFilamentEvidenceHtml(payload = {}) {
  const filamentId = payload.filament?.filament_id || "";
  const samples = payload.samples || [];
  const colgroup = `
    <colgroup>
      <col class="model-filament-sample-col">
      <col class="model-filament-strip-col">
      <col class="model-filament-role-col">
      <col class="model-filament-state-col">
      <col class="model-filament-appearance-col">
    </colgroup>
  `;
  return `
    <div class="model-filament-samples-table">
      <table class="data-table compact-table model-filament-evidence-table">
        ${colgroup}
        <thead>
          <tr>
            <th>Sample</th>
            <th>Strip</th>
            <th>Roles</th>
            <th>Fit State</th>
            <th>Extracted Appearance</th>
          </tr>
        </thead>
        <tbody>
          ${samples.map((row) => `
            <tr class="data-row" data-model-filament-sample-id="${_escAttr(row.sample_id)}">
              <td><strong>${_escHtml(row.sample_id)}</strong></td>
              <td>${modelReviewStripDiagramHtml(row)}</td>
              <td>${(row.roles_for_filament || []).map((role) => _escHtml(role.role_kind || role.role_label || "role")).join(" · ") || `<span class="muted-line">None</span>`}</td>
              <td>${modelReviewFitStateHtml(row)}</td>
              <td>${modelReviewStripHtml(row.observed_appearance?.hex || [])}</td>
            </tr>
          `).join("") || `<tr><td colspan="5"><div class="model-review-empty">No samples contain ${_escHtml(filamentId)}.</div></td></tr>`}
        </tbody>
      </table>
    </div>
  `;
}

function renderModelingFilamentDetail(payload) {
  const filament = payload?.filament || {};
  return `
    <div class="model-review-detail model-filament-detail">
      ${buildDrawerFormModule("Summary", modelReviewFilamentSummaryHtml(filament), { density: "compact", classes: "model-review-detail-module" })}
      <div class="model-filament-metrics-grid">
        ${buildDrawerFormModule("Coverage", modelReviewFilamentCoverageHtml(filament), { density: "compact", classes: "model-review-detail-module model-filament-metric-module" })}
        ${buildDrawerFormModule("Model State", modelReviewModelStateHtml(payload?.model_status || {}), { density: "compact", classes: "model-review-detail-module model-filament-metric-module" })}
      </div>
      ${buildDrawerFormModule("Samples", modelReviewFilamentEvidenceHtml(payload), { density: "table", classes: "model-review-detail-module model-filament-samples-module" })}
    </div>
  `;
}

function bindModelingFilamentDetailActions(payload) {
  const filamentId = payload?.filament?.filament_id || "";
  const runFilamentFitToggle = async (button) => {
    button.disabled = true;
    const nextExclude = button.dataset.nextExclude === "true";
    await handleModelingFilamentFitToggle(button.dataset.modelToggleFilament, nextExclude);
    if (document.body.contains(button)) button.disabled = false;
  };
  const filamentFitToggle = document.getElementById("modelFilamentFitToggle");
  if (filamentFitToggle) {
    if (filamentFitToggle.dataset.nextExclude === "true") {
      bindConfirmAction(filamentFitToggle, {
        armedText: "Confirm Exclude",
        onConfirm: async () => runFilamentFitToggle(filamentFitToggle),
      });
    } else {
      filamentFitToggle.addEventListener("click", async () => runFilamentFitToggle(filamentFitToggle));
    }
  }
  document.getElementById("modelFilamentShowSamplesBtn")?.addEventListener("click", async () => {
    if (!filamentId) return;
    modelingState.samplesFilamentIds = [filamentId];
    modelingState.samplesFilter = "all";
    modelingState.samples = null;
    currentSubtab = "samples";
    renderWorkspace();
    await loadModelingTab("samples", { force: true });
  });
  detailSidebar.querySelectorAll("[data-model-filament-sample-id]").forEach((row) => {
    row.addEventListener("click", async () => {
      const sampleId = row.dataset.modelFilamentSampleId;
      await openModelingSampleDetailDrawer(sampleId, row, {
        returnFilamentId: filamentId,
        returnFilamentPayload: payload,
      });
    });
  });
}

async function handleModelingFilamentFitToggle(filamentId, nextExclude) {
  if (!filamentId) return;
  try {
    const updated = await updateFilament(filamentId, { exclude_from_model: !!nextExclude });
    const index = data.filaments.findIndex((fil) => fil.filament_id === filamentId);
    if (index >= 0) data.filaments[index] = { ...data.filaments[index], ...updated };
    invalidateModelingPayloads();
    showProfileToast(`${updated.display_name || updated.color_name || filamentId} ${nextExclude ? "excluded" : "included"} for model fits`);
    await loadModelingTab(modelingCurrentTab(), { force: true });
    if (selectedRecord.kind === "model_filament" && selectedRecord.id === filamentId) {
      await openModelingFilamentDetailDrawer(filamentId);
    }
  } catch (err) {
    showProfileToast(err.message || "Failed to update filament model-fit state");
  }
}

async function openModelingFilamentDetailDrawer(filamentId, returnFocusEl = null) {
  if (!recordDrawer || !detailSidebar) return;
  const requestSeq = ++modelingState.detailRequestSeq;
  selectedRecord = { kind: "model_filament", id: filamentId };
  modelingState.detailSamplePayload = null;
  modelingState.detailFilamentPayload = null;
  modelingState.sampleDetailReturnFilamentId = null;
  modelingState.sampleDetailReturnFilamentPayload = null;
  modelingState.sampleDetailReturnSampleContext = null;
  closeLinkedSampleDrawer({ restoreFocus: false });
  recordDrawer.classList.remove("narrow-drawer", "sample-set-drawer", "sample-expanded");
  recordDrawer.classList.add("model-filament-drawer");
  setDetailSidebarStackMode("default");
  _filamentDrawerMode = null;
  _filamentDrawerData = null;
  _sampleDrawerMode = null;
  setDrawerHeading(filamentId);
  drawerStatusPill.innerHTML = "";
  detailActionArea.innerHTML = `<button class="secondary-button small drawer-header-action" type="button" id="modelFilamentShowSamplesBtn" disabled>Show In Samples</button>`;
  detailWindowArea.innerHTML = "";
  detailSidebar.innerHTML = `<div class="model-review-empty">Loading filament modeling detail...</div>`;
  openRecordDrawer();

  try {
    const payload = await fetchModelingFilament(filamentId);
    if (requestSeq !== modelingState.detailRequestSeq || selectedRecord.kind !== "model_filament" || selectedRecord.id !== filamentId) return;
    modelingState.detailFilamentPayload = payload;
    if (payload?.model_status) data.model_status = payload.model_status;
    const filament = payload?.filament || {};
    setDrawerHeading(filament.name || filament.display_name || filament.filament_id || filamentId);
    drawerStatusPill.innerHTML = modelReviewWhiteCapStatusHtml(filament);
    detailActionArea.innerHTML = `<button class="secondary-button small drawer-header-action" type="button" id="modelFilamentShowSamplesBtn">Show In Samples</button>`;
    detailSidebar.innerHTML = renderModelingFilamentDetail(payload);
    bindModelingFilamentDetailActions(payload);
  } catch (err) {
    if (requestSeq !== modelingState.detailRequestSeq || selectedRecord.kind !== "model_filament" || selectedRecord.id !== filamentId) return;
    drawerStatusPill.innerHTML = `<span class="status-pill failed">Failed</span>`;
    detailActionArea.innerHTML = "";
    detailSidebar.innerHTML = `<div class="model-review-empty is-error">${_escHtml(err.message || "Failed to load filament modeling detail")}</div>`;
    if (returnFocusEl instanceof HTMLElement) returnFocusEl.focus();
  }
}

function renderModelingSamples(payload) {
  if (!payload) return modelingLoadingHtml("samples");
  const rows = payload.rows || [];
  const total = Number(payload.total || rows.length);
  const sampleCountLabel = rows.length === total ? `${total} samples` : `${rows.length} / ${total} samples shown`;
  const filterOptions = [
    ["all", "All"],
    ["included", "Included"],
    ["excluded", "Excluded"],
    ["filament_excluded", "Filament Excluded"],
    ["has_excluded_swatches", "Has Excluded Swatches"],
    ["stale", "Stale"],
    ["missing_model", "Missing Model"],
  ];
  return `
    <div class="model-review-page">
      ${modelStatusAttentionHtml()}
      <div class="model-review-controls">
        <div class="model-review-controls-main">
          <label class="model-review-control-field">
            <span>Filter</span>
            <select id="modelSamplesFilter">
              ${filterOptions.map(([value, label]) => `<option value="${value}"${modelingState.samplesFilter === value ? " selected" : ""}>${label}</option>`).join("")}
            </select>
          </label>
          ${modelReviewFilamentFilterHtml()}
        </div>
        <span class="model-review-result-count">${_escHtml(sampleCountLabel)}</span>
      </div>
      <table class="data-table compact-table model-review-table">
        <thead>
          <tr>
            ${modelingSortableHeader("Sample", "sample_id", modelingState.samplesSort, modelingState.samplesSortDir, "samples")}
            <th>Strip</th>
            ${modelingSortableHeader("Filaments", "filament", modelingState.samplesSort, modelingState.samplesSortDir, "samples")}
            ${modelingSortableHeader("Fit State", "status", modelingState.samplesSort, modelingState.samplesSortDir, "samples")}
            <th>Extracted Appearance</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr class="data-row" data-model-sample-id="${_escAttr(row.sample_id)}">
              <td><strong>${_escHtml(row.sample_id)}</strong></td>
              <td>${modelReviewStripDiagramHtml(row)}</td>
              <td>${modelReviewFilamentStackHtml(row.filaments)}</td>
              <td>${modelReviewFitStateHtml(row)}</td>
              <td>${modelReviewStripHtml(row.observed_appearance?.hex || [])}</td>
            </tr>
          `).join("") || `<tr><td colspan="5"><div class="model-review-empty">No samples match this filter.</div></td></tr>`}
        </tbody>
      </table>
    </div>
  `;
}

function renderModelingFilaments(payload) {
  if (!payload) return modelingLoadingHtml("filaments");
  const rows = payload.rows || [];
  return `
    <div class="model-review-page model-review-filaments-page">
      ${modelStatusAttentionHtml()}
      <div class="model-review-filaments-meta">
        <span class="model-review-result-count">${Number(payload.total || 0)} filaments</span>
      </div>
      <div class="model-review-table-scroll model-review-filament-table-scroll">
        <table class="data-table compact-table model-review-table model-review-filament-table">
          <colgroup>
            <col class="model-review-filament-name-col">
            <col class="model-review-filament-brand-col">
            <col class="model-review-filament-total-samples-col">
            <col class="model-review-filament-fit-samples-col">
            <col class="model-review-filament-swatch-count-col">
            <col class="model-review-filament-swatch-count-col">
            <col class="model-review-filament-health-col">
          </colgroup>
          <thead>
            <tr>
              ${modelingSortableHeader("Filament", "name", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
              ${modelingSortableHeader("Brand", "brand", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
              ${modelingSortableHeader("Samples", "sample_count", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
              ${modelingSortableHeader("Fit Samples", "included_sample_count", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
              ${modelingSortableHeader("Incl. Swatches", "included_swatch_count", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
              ${modelingSortableHeader("Excl. Swatches", "excluded_swatch_count", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
              ${modelingSortableHeader("Health", "health", modelingState.filamentsSort, modelingState.filamentsSortDir, "filaments")}
            </tr>
          </thead>
          <tbody>
            ${rows.map((row) => `
              <tr class="data-row" data-model-open-filament="${_escAttr(row.filament_id)}">
                <td>
                  <span class="model-filament-name-cell">
                    <span class="color-chip model-filament-table-chip" style="background:${_escAttr(row.hex || "#999999")}"></span>
                    <strong>${_escHtml(row.name || row.filament_id)}</strong>
                    ${modelReviewFilamentExclusionPillHtml(row)}
                  </span>
                </td>
                <td>${_escHtml(row.brand || "")}</td>
                <td>${Number(row.sample_count || 0)}</td>
                <td>${Number(row.included_sample_count || 0)}</td>
                <td>${Number(row.included_swatch_count || 0)}</td>
                <td>${Number(row.excluded_swatch_count || 0)}</td>
                <td><span class="status-pill model-review-state ${_escAttr(modelReviewHealthStatusClass(row.health?.state || "unknown"))}">${_escHtml(row.health?.label || "Unknown")}</span></td>
              </tr>
            `).join("") || `<tr><td colspan="7"><div class="model-review-empty">No filament coverage rows.</div></td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function applyFitControlMutationResponse(result) {
  if (result?.model_status) data.model_status = result.model_status;
  const sampleId = result?.sample_id;
  if (sampleId) {
    const exp = data.samples.find((item) => item.sample_id === sampleId);
    if (exp) {
      exp._fit_exclude = !!result.fit_exclude;
      exp._excluded_swatches = Array.isArray(result.excluded_swatches) ? result.excluded_swatches : exp._excluded_swatches;
      exp._n_excluded = exp._excluded_swatches.length;
    }
  }
  invalidateModelingPayloads();
}

async function handleModelingSampleToggle(sampleId, nextFitExclude) {
  const exp = data.samples.find((item) => item.sample_id === sampleId);
  try {
    const result = await updateSampleFitExclusion(sampleId, { fit_exclude: !!nextFitExclude });
    applyFitControlMutationResponse(result);
    showProfileToast(`${sampleId} ${nextFitExclude ? "excluded" : "included"} for model fits`);
    await loadModelingTab(modelingCurrentTab(), { force: true });
    if (selectedRecord.kind === "sample" && selectedRecord.id === sampleId && exp) {
      exp._fit_exclude = !!nextFitExclude;
      renderSidebarForSample(exp, { expanded: _sampleInspectExpanded });
    }
    if (selectedRecord.kind === "model_sample" && selectedRecord.id === sampleId) {
      await refreshOpenModelingSampleDetail(sampleId);
    }
  } catch (err) {
    showProfileToast(err.message || "Failed to update model-fit state");
  }
}

async function handleModelingSampleSwatchToggle(sampleId, swatchIndex, nextExcluded) {
  if (!sampleId || !Number.isInteger(swatchIndex) || swatchIndex < 0) return;
  const sample = modelingState.detailSamplePayload?.sample || {};
  const excluded = modelReviewExcludedSwatchSet(sample);
  if (nextExcluded) excluded.add(swatchIndex);
  else excluded.delete(swatchIndex);
  const nextExcludedSwatches = Array.from(excluded).sort((a, b) => a - b);
  try {
    const result = await updateSampleSwatchFitExclusions(sampleId, nextExcludedSwatches);
    applyFitControlMutationResponse(result);
    showProfileToast(`Swatch ${swatchIndex + 1} ${nextExcluded ? "excluded" : "included"} for model fits`);
    await loadModelingTab(modelingCurrentTab(), { force: true });
    if (selectedRecord.kind === "model_sample" && selectedRecord.id === sampleId) {
      await refreshOpenModelingSampleDetail(sampleId);
    }
  } catch (err) {
    showProfileToast(err.message || "Failed to update swatch fit state");
  }
}

async function handleModelingHeaderSort(scope, key) {
  if (scope === "filaments") {
    if (modelingState.filamentsSort === key) {
      modelingState.filamentsSortDir = modelingState.filamentsSortDir === "asc" ? "desc" : "asc";
    } else {
      modelingState.filamentsSort = key;
      modelingState.filamentsSortDir = "asc";
    }
    modelingState.filaments = null;
    currentSubtab = "filaments";
    renderWorkspace();
    await loadModelingTab("filaments", { force: true });
    return;
  }

  if (modelingState.samplesSort === key) {
    modelingState.samplesSortDir = modelingState.samplesSortDir === "asc" ? "desc" : "asc";
  } else {
    modelingState.samplesSort = key;
    modelingState.samplesSortDir = "asc";
  }
  modelingState.samples = null;
  currentSubtab = "samples";
  renderWorkspace();
  await loadModelingTab("samples", { force: true });
}

function bindModelingActions() {
  const fitModelsBtn = document.getElementById("modelOverviewFitModelsBtn");
  if (fitModelsBtn) {
    fitModelsBtn.onclick = () => openFitModelsWorkflow(fitModelsBtn, async () => {
      invalidateModelingPayloads();
      await refreshModelsAfterWorkflow();
      await loadModelingTab(modelingCurrentTab(), { force: true });
    });
  }
  const samplesFilter = document.getElementById("modelSamplesFilter");
  if (samplesFilter) {
    samplesFilter.addEventListener("change", async () => {
      modelingState.samplesFilter = samplesFilter.value || "all";
      modelingState.samples = null;
      await loadModelingTab("samples", { force: true });
    });
  }
  if (!tableContainer) return;
  tableContainer.querySelectorAll("[data-model-sort]").forEach((header) => {
    header.addEventListener("click", async () => {
      await handleModelingHeaderSort(header.dataset.modelSortScope || "samples", header.dataset.modelSort || "sample_id");
    });
  });
  document.getElementById("modelSamplesFilamentFilterBtn")?.addEventListener("click", () => {
    openFilamentSelector({
      title: "Filter Samples by Filament",
      mode: "multi",
      selectedIds: modelingState.samplesFilamentIds || [],
      onApply: async (ids) => {
        modelingState.samplesFilamentIds = Array.from(new Set(ids || []));
        modelingState.samples = null;
        currentSubtab = "samples";
        renderWorkspace();
        await loadModelingTab("samples", { force: true });
      },
    });
  });
  document.getElementById("modelSamplesClearFilaments")?.addEventListener("click", async () => {
    modelingState.samplesFilamentIds = [];
    modelingState.samples = null;
    currentSubtab = "samples";
    renderWorkspace();
    await loadModelingTab("samples", { force: true });
  });
  tableContainer.querySelectorAll("[data-model-sample-filter-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      const removeId = button.dataset.modelSampleFilterRemove;
      modelingState.samplesFilamentIds = (modelingState.samplesFilamentIds || []).filter((id) => id !== removeId);
      modelingState.samples = null;
      currentSubtab = "samples";
      renderWorkspace();
      await loadModelingTab("samples", { force: true });
    });
  });
  tableContainer.querySelectorAll("[data-model-sample-id]").forEach((row) => {
    row.addEventListener("click", async (event) => {
      if (event.target.closest("button, a, input, select, textarea, label, [role='button']")) return;
      const sampleId = row.dataset.modelSampleId;
      await openModelingSampleDetailDrawer(sampleId, row);
    });
  });
  tableContainer.querySelectorAll("[data-model-open-filament]").forEach((row) => {
    row.addEventListener("click", async () => {
      const filamentId = row.dataset.modelOpenFilament || "";
      await openModelingFilamentDetailDrawer(filamentId, row);
    });
  });
}

function renderModelsView(options = {}) {
  const defaultContent = document.getElementById("defaultContent");
  const panel = defaultContent?.querySelector(".main-logbook");
  const sectionHead = panel?.querySelector(".section-head");
  const tab = modelingCurrentTab();
  defaultContent?.classList.add("model-overview-content");
  defaultContent?.classList.toggle("modeling-overview-content", tab === "overview");
  defaultContent?.classList.toggle("modeling-filaments-content", tab === "filaments");
  panel?.classList.add("model-overview-panel", "model-tab-shell");
  panel?.classList.toggle("modeling-overview-panel", tab === "overview");
  panel?.classList.toggle("modeling-filaments-panel", tab === "filaments");
  sectionHead?.classList.add("model-status-section-head");

  tableSummary.textContent = "";
  tableToolbar.className = "toolbar-inline model-status-header";
  tableToolbar.innerHTML = `
    <div class="model-status-list" role="list" aria-label="Calibration model status">
      ${renderModelOverviewHeaderStatus()}
    </div>
    <button class="primary-button small model-status-fit-button" type="button" id="modelOverviewFitModelsBtn">Fit Models</button>
  `;
  if (!options.skipEnsure) ensureModelingTabLoaded(tab);
  if (tab === "samples") tableContainer.innerHTML = renderModelingSamples(modelingState.samples);
  else if (tab === "filaments") tableContainer.innerHTML = renderModelingFilaments(modelingState.filaments);
  else tableContainer.innerHTML = renderModelingOverview(modelingState.overview);
  bindModelingActions();
}

// ── Profiles View (Model Fitting → Fit Review) ──────────────────────────────

// ── Filament slug generation ─────────────────────────────────────────────────

function _generateFilamentSlug(manufacturer, colorName) {
  return (manufacturer + " " + colorName).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function closeFilamentBuilderPanel() {
  // No-op stub — the filament builder panel was removed.
  // Kept as a safe no-op for any remaining calls during mode switches.
}

function showProfileToast(message) {
  let toast = document.getElementById("profileToast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "profileToast";
    toast.className = "profile-toast";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove("is-visible"), 2000);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

function isProfileFitJobRunning() {
  return profileFitJobState.running ||
    profileFitJobState.status?.status === "queued" ||
    profileFitJobState.status?.status === "running";
}

function profileFitResultFromJob(job) {
  const progress = job?.progress || {};
  const summary = progress.summary || {};
  return {
    fitted: Number(summary.fitted || 0),
    failed: Number(summary.failed || 0),
    skipped: Number(summary.skipped || 0),
    results: job?.results || [],
    pair_corrections: job?.pair_corrections || null,
    pair_corrections_error: job?.pair_corrections_error || null,
  };
}

function profileFitToastMessage(result) {
  let msg = `Fitted ${result.fitted} profiles (${result.failed} failed, ${result.skipped} skipped)`;
  if (result.pair_corrections) {
    msg += ` \u00B7 ${result.pair_corrections.n_pairs} pair corrections`;
  } else if (result.pair_corrections_error) {
    msg += ` \u00B7 pair corrections failed: ${result.pair_corrections_error}`;
  }
  return msg;
}

function renderProfileFitProgressPanel() {
  const job = profileFitJobState.status;
  if (!job) return "";

  const progress = job.progress || {};
  const summary = progress.summary || {};
  const total = Math.max(1, Number(progress.total || 1));
  const current = Math.min(total, Math.max(0, Number(progress.current || 0)));
  const pct = Math.max(0, Math.min(100, Number(progress.percent || 0)));
  const status = job.status || (profileFitJobState.running ? "running" : "queued");
  const statusClass = status === "completed" ? "is-complete" : status === "failed" ? "is-failed" : "is-running";
  const title = status === "completed" ? "Profile Fitting Complete" :
    status === "failed" ? "Profile Fitting Failed" : "Fitting Profiles";
  const message = progress.message || "Fitting profiles";
  const phase = progress.phase ? escapeHtml(String(progress.phase).replace(/_/g, " ")) : "";
  const target = progress.target ? `<span class="mono">${escapeHtml(progress.target)}</span>` : "";

  return `
    <div class="profile-fit-progress-panel ${statusClass}">
      <div class="profile-fit-progress-head">
        <strong>${title}</strong>
        <span class="small-copy mono">${current} / ${total} &middot; ${pct}%</span>
      </div>
      <div class="profile-progress-track">
        <div class="profile-progress-fill profile-fit-progress-fill" style="width:${pct}%"></div>
      </div>
      <div class="profile-fit-progress-meta">
        <span>${escapeHtml(message)}</span>
        ${target}
      </div>
      <div class="profile-fit-progress-meta profile-fit-progress-summary mono">
        <span>${Number(summary.fitted || 0)} fitted</span>
        <span>${Number(summary.failed || 0)} failed</span>
        <span>${Number(summary.skipped || 0)} skipped</span>
        ${phase ? `<span>${phase}</span>` : ""}
      </div>
    </div>
  `;
}

function renderProfileFitSurfaces() {
  if (currentMode === "profiles") {
    renderModelsView();
  }
}

function clearProfileFitCaches() {
  modelFittingState.predictionCache = {};
  profilesState.profileCache = {};
  profilesState.curveCache = {};
  profilesState.swatchCache = {};
  profilesState.errorCache = {};
}

async function pollProfileFitJob(jobId) {
  while (true) {
    const status = await fetchFitAllProfilesJobStatus(jobId);
    profileFitJobState.status = status;
    profileFitJobState.running = status.status === "queued" || status.status === "running";
    modelFittingState.isFittingAll = profileFitJobState.running;
    renderProfileFitSurfaces();
    if (status.status === "completed") return status;
    if (status.status === "failed") {
      throw new Error(status.error || "Fit all failed");
    }
    await sleep(700);
  }
}

async function runFitAllProfilesWithProgress() {
  if (isProfileFitJobRunning()) {
    showProfileToast("Profile fitting is already running");
    return profileFitJobState.status;
  }

  profileFitJobState.running = true;
  profileFitJobState.error = null;
  modelFittingState.isFittingAll = true;
  renderProfileFitSurfaces();

  try {
    const job = await startFitAllProfilesJob();
    if (!job?.job_id) throw new Error("Fit job did not return a job id");
    profileFitJobState.jobId = job.job_id;
    profileFitJobState.status = job;
    renderProfileFitSurfaces();

    const finalJob = await pollProfileFitJob(job.job_id);
    const result = profileFitResultFromJob(finalJob);
    profileFitJobState.lastResult = result;
    clearProfileFitCaches();
    if (typeof handleRefresh === "function") await handleRefresh();
    showProfileToast(profileFitToastMessage(result));
    return result;
  } catch (err) {
    profileFitJobState.error = err.message;
    showProfileToast("Fit all failed: " + err.message);
    throw err;
  } finally {
    profileFitJobState.running = false;
    modelFittingState.isFittingAll = false;
    renderProfileFitSurfaces();
  }
}

function selectProfileFilament(filamentId) {
  profilesState.selectedFilamentId = filamentId;
  profilesState.detailSection = "chart"; // reset to chart on selection
  const fil = data.filaments.find((f) => f.filament_id === filamentId);
  const needsLoad = fil && fil.has_profile && !profilesState.profileCache[filamentId];
  const hasStrips = fil && (fil.has_strips || fil.processed_count > 0);

  if (needsLoad || (hasStrips && !profilesState.curveCache[filamentId])) {
    profilesState.loadingProfile = true;
    renderProfilesView();
    const fetches = [];
    if (fil.has_profile && !profilesState.profileCache[filamentId]) {
      fetches.push(
        fetchProfileDetail(filamentId)
          .then((profile) => { profilesState.profileCache[filamentId] = profile; })
          .catch(() => {})
      );
    }
    if (!profilesState.curveCache[filamentId]) {
      fetches.push(
        fetchProfileCurve(filamentId)
          .then((curveData) => { profilesState.curveCache[filamentId] = curveData; })
          .catch(() => {})
      );
    }
    if (!profilesState.swatchCache[filamentId]) {
      fetches.push(
        fetchProfileSwatches(filamentId)
          .then((swData) => { profilesState.swatchCache[filamentId] = swData; })
          .catch(() => {})
      );
    }
    if (!profilesState.errorCache[filamentId]) {
      fetches.push(
        fetchProfileErrors(filamentId)
          .then((errData) => { profilesState.errorCache[filamentId] = errData; })
          .catch(() => {})
      );
    }
    Promise.all(fetches).then(() => {
      profilesState.loadingProfile = false;
      renderProfilesView();
      _drawProfileCanvasCharts(filamentId);
    });
  } else {
    renderProfilesView();
    if (fil && fil.has_profile) _drawProfileCanvasCharts(filamentId);
  }
}

// ── Profile coverage bar ────────────────────────────────────────────────────

function renderProfileCoverageBar() {
  const total = data.filaments.length;
  const profiled = data.filaments.filter((f) => f.has_profile).length;
  const withStrips = data.filaments.filter((f) => f.has_strips || f.processed_count > 0).length;
  const stale = data.filaments.filter((f) => {
    const p = profilesState.profileCache[f.filament_id];
    return p && p.stale;
  }).length;
  const pct = total > 0 ? Math.round((profiled / total) * 100) : 0;
  const staleNote = stale > 0 ? `<span class="prof-stale-count">${stale} stale</span>` : "";
  return `
    <div class="profile-coverage-bar">
      <div class="profile-coverage-stats">
        <span>Coverage: <strong>${profiled}/${total}</strong> filaments profiled</span>
        <span class="muted-line">${withStrips} with strip data ${staleNote}</span>
      </div>
      <div class="profile-progress-track">
        <div class="profile-progress-fill" style="width:${pct}%"></div>
      </div>
    </div>
  `;
}

// ── Sidebar filament list ───────────────────────────────────────────────────

function _profileStatus(fil) {
  const p = profilesState.profileCache[fil.filament_id];
  if (p && p.stale) return "stale";
  if (fil.has_profile) return "fitted";
  if (fil.has_strips || fil.processed_count > 0) return "strips_only";
  return "none";
}

function renderProfileSidebar() {
  const filaments = [...data.filaments].sort((a, b) => {
    const order = { stale: 0, strips_only: 1, fitted: 2, none: 3 };
    const sa = order[_profileStatus(a)] ?? 4;
    const sb = order[_profileStatus(b)] ?? 4;
    if (sa !== sb) return sa - sb;
    return (a.color_name || "").localeCompare(b.color_name || "");
  });

  return filaments.map((fil) => {
    const selected = profilesState.selectedFilamentId === fil.filament_id;
    const status = _profileStatus(fil);
    let icon, iconClass;
    if (status === "stale") { icon = "&#9888;"; iconClass = "profile-icon-stale"; }
    else if (status === "fitted") { icon = "&#10003;"; iconClass = "profile-icon-ok"; }
    else if (status === "strips_only") { icon = "&#9679;"; iconClass = "profile-icon-strips"; }
    else { icon = "&#10007;"; iconClass = "profile-icon-missing"; }
    const stripCount = fil.strip_count || fil.processed_count || 0;
    const countLabel = stripCount > 0 ? `<span class="prof-sidebar-count">${stripCount}</span>` : "";
    return `
      <div class="profile-filament-item${selected ? " is-selected" : ""}" data-profile-filament="${fil.filament_id}">
        <span class="color-chip" style="background:${fil.hex || '#ddd'}"></span>
        <span class="profile-filament-name">${fil.color_name || fil.filament_id}</span>
        ${countLabel}
        <span class="profile-status-icon ${iconClass}">${icon}</span>
      </div>
    `;
  }).join("");
}

// ── Canvas-based transmission chart ─────────────────────────────────────────

function _drawProfileCanvasCharts(filamentId) {
  const canvas = document.getElementById("profTransmissionCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;

  const cached = profilesState.profileCache[filamentId];
  const curveData = profilesState.curveCache?.[filamentId];
  if (!cached && !curveData) return;

  const W = canvas.clientWidth;
  const H = canvas.clientHeight;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  ctx.scale(dpr, dpr);

  const PAD_L = 44, PAD_R = 12, PAD_T = 12, PAD_B = 28;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  const knots = cached?.knots_mm || curveData?.spline?.knots || [];
  const allDs = [];
  if (curveData?.sources) {
    for (const pts of Object.values(curveData.sources)) {
      for (const pt of (pts || [])) allDs.push(pt.d);
    }
  }
  for (const k of knots) allDs.push(k);
  const rawDMax = allDs.length > 0 ? Math.max(...allDs) : 2.0;
  const dMax = Math.min(Math.max(rawDMax, 0.5), 4.0);

  const xScale = (d) => PAD_L + (d / dMax) * plotW;
  const yScale = (t) => PAD_T + (1 - Math.max(0, Math.min(1, t))) * plotH;

  // Clear
  ctx.clearRect(0, 0, W, H);

  // Background
  ctx.fillStyle = "#fff";
  ctx.fillRect(PAD_L, PAD_T, plotW, plotH);

  // Gridlines
  ctx.strokeStyle = "#e4e4df";
  ctx.lineWidth = 0.5;
  ctx.font = "9px 'Segoe UI', Arial, sans-serif";
  ctx.textAlign = "right";
  ctx.fillStyle = "#888";
  for (let t = 0; t <= 1.0; t += 0.2) {
    const y = yScale(t);
    ctx.beginPath();
    ctx.moveTo(PAD_L, y);
    ctx.lineTo(W - PAD_R, y);
    ctx.stroke();
    ctx.fillText(t.toFixed(1), PAD_L - 4, y + 3);
  }
  // X axis labels
  ctx.textAlign = "center";
  const xStep = dMax <= 1.5 ? 0.2 : dMax <= 2.5 ? 0.5 : 1.0;
  for (let d = 0; d <= dMax + 0.001; d += xStep) {
    const x = xScale(d);
    ctx.fillText(d.toFixed(1), x, H - 6);
  }

  // Axis labels
  ctx.save();
  ctx.font = "10px 'Segoe UI', Arial, sans-serif";
  ctx.fillStyle = "#666";
  ctx.textAlign = "center";
  ctx.fillText("thickness (mm)", PAD_L + plotW / 2, H - 0);
  ctx.restore();

  ctx.save();
  ctx.font = "10px 'Segoe UI', Arial, sans-serif";
  ctx.fillStyle = "#666";
  ctx.textAlign = "center";
  ctx.translate(10, PAD_T + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("T (transmission)", 0, 0);
  ctx.restore();

  // Noise floor line
  const noiseFloor = curveData?.noise_floor || cached?.noise_floor_T;
  if (noiseFloor && noiseFloor > 0) {
    const nfY = yScale(noiseFloor);
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = "#999";
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    ctx.moveTo(PAD_L, nfY);
    ctx.lineTo(W - PAD_R, nfY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "8px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = "#999";
    ctx.textAlign = "right";
    ctx.fillText("noise floor", W - PAD_R - 2, nfY - 3);
  }

  // Plot border
  ctx.strokeStyle = "#ccc";
  ctx.lineWidth = 0.5;
  ctx.strokeRect(PAD_L, PAD_T, plotW, plotH);

  const COLORS = { r: "#d32f2f", g: "#388e3c", b: "#1976d2" };
  const CHANNEL_KEYS = ["T_r", "T_g", "T_b"];
  const CHANNEL_COLORS = [COLORS.r, COLORS.g, COLORS.b];

  // Draw measured data points from strip sources
  if (curveData?.sources) {
    const categories = Object.entries(curveData.sources);
    for (const [category, points] of categories) {
      const isCrosscal = category === "thin" || category === "fixed_role" || category === "crosscal";
      for (const pt of (points || [])) {
        if (pt.d > dMax) continue;
        const vals = [pt.T_r, pt.T_g, pt.T_b];
        for (let ci = 0; ci < 3; ci++) {
          if (vals[ci] == null) continue;
          const x = xScale(pt.d);
          const y = yScale(vals[ci]);
          ctx.beginPath();
          if (isCrosscal) {
            // Diamond shape for crosscal points
            ctx.moveTo(x, y - 3);
            ctx.lineTo(x + 3, y);
            ctx.lineTo(x, y + 3);
            ctx.lineTo(x - 3, y);
            ctx.closePath();
            ctx.fillStyle = CHANNEL_COLORS[ci];
            ctx.globalAlpha = 0.45;
            ctx.fill();
          } else {
            // Circle for solo points
            ctx.arc(x, y, 2.5, 0, Math.PI * 2);
            ctx.fillStyle = CHANNEL_COLORS[ci];
            ctx.globalAlpha = 0.55;
            ctx.fill();
            ctx.globalAlpha = 0.3;
            ctx.strokeStyle = "#fff";
            ctx.lineWidth = 0.8;
            ctx.stroke();
          }
          ctx.globalAlpha = 1.0;
        }
      }
    }
  }

  // Draw spline curves
  if (curveData?.spline) {
    const sp = curveData.spline;
    const denseChannels = [
      { d: sp.d, vals: sp.T_r, color: COLORS.r },
      { d: sp.d, vals: sp.T_g, color: COLORS.g },
      { d: sp.d, vals: sp.T_b, color: COLORS.b },
    ];
    for (const ch of denseChannels) {
      ctx.beginPath();
      ctx.strokeStyle = ch.color;
      ctx.lineWidth = 1.8;
      ctx.globalAlpha = 0.9;
      let started = false;
      for (let i = 0; i < ch.d.length; i++) {
        if (ch.d[i] > dMax) break;
        const x = xScale(ch.d[i]);
        const y = yScale(ch.vals[i]);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.globalAlpha = 1.0;
    }
    // Knot markers
    const knotDs = sp.knots || [];
    const knotChannels = [
      { vals: sp.knot_T_r, color: COLORS.r },
      { vals: sp.knot_T_g, color: COLORS.g },
      { vals: sp.knot_T_b, color: COLORS.b },
    ];
    for (const ch of knotChannels) {
      if (!ch.vals) continue;
      for (let i = 0; i < knotDs.length; i++) {
        if (knotDs[i] > dMax) continue;
        const x = xScale(knotDs[i]);
        const y = yScale(ch.vals[i]);
        ctx.beginPath();
        ctx.arc(x, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = ch.color;
        ctx.globalAlpha = 0.7;
        ctx.fill();
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.globalAlpha = 1.0;
      }
    }
  } else if (cached?.knots_mm) {
    // Fallback: knot-to-knot lines
    for (let ci = 0; ci < 3; ci++) {
      const vals = cached[CHANNEL_KEYS[ci]];
      if (!vals) continue;
      ctx.beginPath();
      ctx.strokeStyle = CHANNEL_COLORS[ci];
      ctx.lineWidth = 1.5;
      for (let i = 0; i < knots.length; i++) {
        if (knots[i] > dMax) break;
        const x = xScale(knots[i]);
        const y = yScale(vals[i]);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }

  // Draw error bar chart on secondary canvas
  _drawErrorBarChart(filamentId);
}

function _drawErrorBarChart(filamentId) {
  const canvas = document.getElementById("profErrorCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;

  const errData = profilesState.errorCache?.[filamentId];
  if (!errData || !errData.bars || errData.bars.length === 0) {
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    ctx.font = "12px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = "#888";
    ctx.textAlign = "center";
    ctx.fillText("No error data available", canvas.clientWidth / 2, canvas.clientHeight / 2);
    return;
  }

  const W = canvas.clientWidth;
  const H = canvas.clientHeight;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  const PAD_L = 44, PAD_R = 12, PAD_T = 12, PAD_B = 28;
  const plotW = W - PAD_L - PAD_R;
  const plotH = H - PAD_T - PAD_B;

  const bars = errData.bars;
  const maxDe = Math.max(10, ...bars.map(b => b.dE));
  const barW = Math.max(2, Math.min(12, (plotW - 4) / bars.length - 1));
  const gap = Math.max(1, (plotW - bars.length * barW) / (bars.length + 1));

  const yScale = (de) => PAD_T + plotH - (de / maxDe) * plotH;

  // Background
  ctx.fillStyle = "#fff";
  ctx.fillRect(PAD_L, PAD_T, plotW, plotH);

  // Threshold lines
  const thresholds = [
    { val: 2.0, label: "good", color: "#2e7d3244" },
    { val: 5.0, label: "ok", color: "#b26a0044" },
    { val: 10.0, label: "bad", color: "#d32f2f44" },
  ];
  for (const th of thresholds) {
    if (th.val > maxDe) continue;
    const y = yScale(th.val);
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = th.color.slice(0, 7);
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    ctx.moveTo(PAD_L, y);
    ctx.lineTo(W - PAD_R, y);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = "8px 'Segoe UI', Arial, sans-serif";
    ctx.fillStyle = th.color.slice(0, 7);
    ctx.textAlign = "right";
    ctx.fillText(`dE ${th.val}`, W - PAD_R - 2, y - 2);
  }

  // Y axis labels
  ctx.font = "9px 'Segoe UI', Arial, sans-serif";
  ctx.fillStyle = "#888";
  ctx.textAlign = "right";
  const yStep = maxDe <= 10 ? 2 : 5;
  for (let de = 0; de <= maxDe; de += yStep) {
    const y = yScale(de);
    ctx.fillText(de.toFixed(0), PAD_L - 4, y + 3);
    ctx.strokeStyle = "#e8e8e4";
    ctx.lineWidth = 0.3;
    ctx.beginPath();
    ctx.moveTo(PAD_L, y);
    ctx.lineTo(W - PAD_R, y);
    ctx.stroke();
  }

  // Draw bars
  const severityColors = {
    good: "#4caf50",
    ok: "#ff9800",
    bad: "#f44336",
    awful: "#b71c1c",
  };
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    const x = PAD_L + gap + i * (barW + gap);
    const barH = (b.dE / maxDe) * plotH;
    const y = PAD_T + plotH - barH;
    ctx.fillStyle = severityColors[b.severity] || "#999";
    ctx.globalAlpha = 0.8;
    ctx.fillRect(x, y, barW, barH);
    ctx.globalAlpha = 1.0;
  }

  // Axis labels
  ctx.font = "10px 'Segoe UI', Arial, sans-serif";
  ctx.fillStyle = "#666";
  ctx.textAlign = "center";
  ctx.fillText("swatches (sorted by thickness)", PAD_L + plotW / 2, H - 2);

  // Border
  ctx.strokeStyle = "#ccc";
  ctx.lineWidth = 0.5;
  ctx.strokeRect(PAD_L, PAD_T, plotW, plotH);
}

// ── Profile metadata block ──────────────────────────────────────────────────

function renderProfileMetadata(filamentId) {
  const cached = profilesState.profileCache[filamentId];
  if (profilesState.loadingProfile) {
    return `<div class="profile-meta-block"><span class="muted-line">Loading profile data...</span></div>`;
  }
  if (!cached) {
    return `
      <div class="profile-meta-block">
        <p class="panel-kicker">Profile Metadata</p>
        <div class="profile-meta-grid">
          <div class="profile-meta-item"><span class="sidebar-label">Knots</span><strong>&mdash;</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">D range</span><strong>&mdash;</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Fit date</span><strong>&mdash;</strong></div>
          <div class="profile-meta-item"><span class="sidebar-label">Data sources</span><strong>&mdash;</strong></div>
        </div>
      </div>
    `;
  }
  const knotCount = cached.knots_mm ? cached.knots_mm.length : (cached.n_knots || "&mdash;");
  const dMin = cached.knots_mm ? Number(cached.knots_mm[0]).toFixed(2) : "0.00";
  const dMax = cached.knots_mm ? Number(cached.knots_mm[cached.knots_mm.length - 1]).toFixed(2) : "&mdash;";
  const fitDate = cached.fit_date || cached.created || "&mdash;";
  const sourceStrips = cached.source_strips ? cached.source_strips.length : "&mdash;";
  const noiseFloor = cached.noise_floor_T != null ? cached.noise_floor_T.toFixed(4) : "&mdash;";
  const truncated = cached.n_truncated || 0;
  const isActive = cached.active !== false;
  const isStale = cached.stale === true;

  let staleBanner = "";
  if (isStale) {
    const reason = cached.stale_reason || "Profile data may be outdated";
    staleBanner = `
      <div class="prof-stale-banner">
        <strong>Stale profile</strong> &mdash; ${reason}.
        Refit recommended.
      </div>`;
  }

  return `
    ${staleBanner}
    <div class="profile-meta-block">
      <p class="panel-kicker">Fit Summary</p>
      <div class="profile-meta-grid">
        <div class="profile-meta-item"><span class="sidebar-label">Knots</span><strong>${knotCount}</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">D range</span><strong>${dMin} &ndash; ${dMax} mm</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">Noise floor</span><strong>${noiseFloor}</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">Source strips</span><strong>${sourceStrips}</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">Truncated</span><strong>${truncated} pts</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">Fit date</span><strong>${fitDate}</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">Active</span><strong>${isActive ? "Yes" : "No"}</strong></div>
        <div class="profile-meta-item"><span class="sidebar-label">Model</span><strong>PCHIP spline</strong></div>
      </div>
    </div>
  `;
}

// ── Swatch comparison table ─────────────────────────────────────────────────

function renderSwatchComparisonTable(filamentId) {
  const swatchData = profilesState.swatchCache?.[filamentId];
  if (!swatchData?.swatches || swatchData.swatches.length === 0) {
    return `<div class="profile-crosscal-block"><p class="panel-kicker">Swatch Comparison</p><span class="muted-line">No swatch data available</span></div>`;
  }

  const sortKey = profilesState.crosscalSortKey;
  const sortDir = profilesState.crosscalSortDir;

  const rows = [...swatchData.swatches].sort((a, b) => {
    if (sortKey === "dE") return sortDir === "asc" ? a.dE - b.dE : b.dE - a.dE;
    if (sortKey === "d") return sortDir === "asc" ? a.d - b.d : b.d - a.d;
    return sortDir === "asc"
      ? String(a.strip_label).localeCompare(String(b.strip_label))
      : String(b.strip_label).localeCompare(String(a.strip_label));
  });

  const arrow = (key) => {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " &#9650;" : " &#9660;";
  };

  const rowsHtml = rows.map((sw) => {
    let sevClass = "";
    if (sw.dE < 2.0) sevClass = "prof-de-good";
    else if (sw.dE < 5.0) sevClass = "prof-de-ok";
    else if (sw.dE < 10.0) sevClass = "prof-de-bad";
    else sevClass = "prof-de-awful";

    return `
      <tr>
        <td class="mono">${Number(sw.d).toFixed(2)}</td>
        <td><span class="prof-swatch-chip" style="background:${sw.measured_hex}"></span> ${sw.measured_hex}</td>
        <td><span class="prof-swatch-chip" style="background:${sw.predicted_hex}"></span> ${sw.predicted_hex}</td>
        <td class="mono ${sevClass}">${sw.dE.toFixed(2)}</td>
        <td class="small-copy">${sw.strip_label || ""}</td>
      </tr>`;
  }).join("");

  const meanDe = rows.reduce((s, r) => s + r.dE, 0) / rows.length;
  const maxDe = Math.max(...rows.map(r => r.dE));

  return `
    <div class="profile-crosscal-block">
      <div class="prof-section-head">
        <p class="panel-kicker">Swatch Comparison</p>
        <span class="muted-line">${rows.length} swatches &middot; mean dE ${meanDe.toFixed(2)} &middot; max dE ${maxDe.toFixed(1)}</span>
      </div>
      <table class="data-table compact-table profile-crosscal-table">
        <thead>
          <tr>
            <th class="sortable" data-crosscal-sort="d">d (mm)${arrow("d")}</th>
            <th>Measured</th>
            <th>Predicted</th>
            <th class="sortable" data-crosscal-sort="dE">dE${arrow("dE")}</th>
            <th class="sortable" data-crosscal-sort="strip_label">Strip${arrow("strip_label")}</th>
          </tr>
        </thead>
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  `;
}

// ── Strip-level error summary ───────────────────────────────────────────────

function renderStripErrorSummary(filamentId) {
  const swatchData = profilesState.swatchCache?.[filamentId];
  if (!swatchData?.swatches || swatchData.swatches.length === 0) return "";

  const byStrip = {};
  for (const sw of swatchData.swatches) {
    const label = sw.strip_label || "unknown";
    if (!byStrip[label]) byStrip[label] = [];
    byStrip[label].push(sw.dE || 0);
  }

  const stripRows = Object.entries(byStrip).map(([label, des]) => {
    const mean = des.reduce((a, b) => a + b, 0) / des.length;
    const max = Math.max(...des);
    let sevClass = "";
    if (mean < 2.0) sevClass = "prof-de-good";
    else if (mean < 5.0) sevClass = "prof-de-ok";
    else sevClass = "prof-de-bad";
    return `
      <tr>
        <td>${label}</td>
        <td class="mono">${des.length}</td>
        <td class="mono ${sevClass}">${mean.toFixed(2)}</td>
        <td class="mono">${max.toFixed(1)}</td>
      </tr>`;
  }).join("");

  return `
    <div class="profile-crosscal-block" style="margin-top:0">
      <p class="panel-kicker">Per-Strip Summary</p>
      <table class="data-table compact-table profile-crosscal-table">
        <thead>
          <tr><th>Strip</th><th>Swatches</th><th>Mean dE</th><th>Max dE</th></tr>
        </thead>
        <tbody>${stripRows}</tbody>
      </table>
    </div>
  `;
}

// ── Batch audit summary ─────────────────────────────────────────────────────

function renderBatchAuditSummary() {
  const allDes = [];
  const stripDes = {};
  for (const [fid, swData] of Object.entries(profilesState.swatchCache || {})) {
    if (!swData?.swatches) continue;
    for (const sw of swData.swatches) {
      if (sw.dE != null) {
        allDes.push(sw.dE);
        const label = sw.strip_label || fid;
        if (!stripDes[label]) stripDes[label] = [];
        stripDes[label].push(sw.dE);
      }
    }
  }

  if (allDes.length === 0) {
    return `
      <div class="profile-batch-audit">
        <p class="panel-kicker">Batch Audit Summary</p>
        <div class="profile-batch-stats">
          <span class="muted-line">Select profiled filaments to populate audit data</span>
        </div>
      </div>
    `;
  }

  const mean = allDes.reduce((a, b) => a + b, 0) / allDes.length;
  let worstLabel = "&mdash;", worstDe = 0;
  for (const [label, des] of Object.entries(stripDes)) {
    const stripMean = des.reduce((a, b) => a + b, 0) / des.length;
    if (stripMean > worstDe) { worstDe = stripMean; worstLabel = label; }
  }
  const nStrips = Object.keys(stripDes).length;
  const nOver5 = Object.values(stripDes).filter((des) => {
    const m = des.reduce((a, b) => a + b, 0) / des.length;
    return m > 5;
  }).length;

  return `
    <div class="profile-batch-audit">
      <p class="panel-kicker">Batch Audit Summary</p>
      <div class="profile-batch-stats">
        <div class="profile-batch-stat"><span class="sidebar-label">Overall mean dE</span><strong>${mean.toFixed(2)}</strong></div>
        <div class="profile-batch-stat"><span class="sidebar-label">Worst strip</span><strong>${worstLabel} (dE ${worstDe.toFixed(2)})</strong></div>
        <div class="profile-batch-stat"><span class="sidebar-label">Strips measured</span><strong>${nStrips}</strong></div>
        <div class="profile-batch-stat"><span class="sidebar-label">Strips > dE 5</span><strong>${nOver5}</strong></div>
      </div>
    </div>
  `;
}

// ── Profile detail panel ────────────────────────────────────────────────────

function renderProfileDetailPanel() {
  const filamentId = profilesState.selectedFilamentId;
  if (!filamentId) {
    return `
      <div class="profile-detail-empty">
        <p class="muted-line">Select a filament from the sidebar to view its profile.</p>
      </div>
    `;
  }

  const fil = data.filaments.find((f) => f.filament_id === filamentId);
  if (!fil) return "";

  const stripCount = fil.strip_count || fil.processed_count || 0;
  const hasProfile = fil.has_profile;
  const hasStrips = fil.has_strips || stripCount > 0;
  const cachedProfile = profilesState.profileCache[filamentId] || {};
  const isActive = cachedProfile.active !== false;
  const profileFitBusy = isProfileFitJobRunning();
  const profileFitDisabled = profileFitBusy ? "disabled" : "";

  // Header with filament info
  const statusMeta = profilePillMeta(filamentId);
  const statusPill = statusMeta ? `<span class="status-pill ${statusMeta.cls}">${statusMeta.label}</span>` : "";

  const headerHtml = `
    <div class="profile-detail-header">
      <span class="color-chip large-chip" style="background:${fil.hex || '#ddd'}"></span>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px">
          <h3 style="margin:0">${fil.display_name || fil.color_name}</h3>
          ${statusPill}
        </div>
        <span class="muted-line">${fil.manufacturer || ""} &middot; ${fil.filament_id} &middot; ${stripCount} strip${stripCount !== 1 ? "s" : ""}</span>
      </div>
    </div>
  `;

  if (!hasProfile && !hasStrips) {
    return `
      <div class="profile-detail-content">
        ${headerHtml}
        <div class="profile-no-profile-panel">
          <strong>No data available</strong>
          <p class="small-copy">This filament has no processed strip data and no profile. Process some samples first.</p>
        </div>
      </div>
    `;
  }

  if (!hasProfile && hasStrips) {
    return `
      <div class="profile-detail-content">
        ${headerHtml}
        <div class="profile-no-profile-panel">
          <strong>No profile fitted</strong>
          <p class="small-copy">${stripCount} strip${stripCount !== 1 ? "s" : ""} available for fitting.</p>
          <button class="primary-button small profile-action-btn" data-profile-action="fit" ${profileFitDisabled}>Fit Profile</button>
        </div>
      </div>
    `;
  }

  // Has profile — show detail tabs
  const section = profilesState.detailSection || "chart";
  const tabActive = (t) => t === section ? "is-active" : "";

  const chartSection = `
    <div class="prof-chart-block">
      <div class="prof-section-head">
        <p class="panel-kicker">Transmission vs Thickness</p>
        <div class="profile-chart-legend" style="margin:0;padding:0">
          <span class="profile-legend-item"><span class="profile-legend-line" style="background:#d32f2f"></span> R</span>
          <span class="profile-legend-item"><span class="profile-legend-line" style="background:#388e3c"></span> G</span>
          <span class="profile-legend-item"><span class="profile-legend-line" style="background:#1976d2"></span> B</span>
          <span class="profile-legend-item" style="opacity:0.6"><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#666;margin-right:3px"></span> data</span>
          <span class="profile-legend-item" style="opacity:0.6"><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#666;border:2px solid #fff;margin-right:3px"></span> knot</span>
        </div>
      </div>
      <canvas id="profTransmissionCanvas" class="prof-canvas" style="height:240px"></canvas>
    </div>
    <div class="prof-chart-block">
      <div class="prof-section-head">
        <p class="panel-kicker">Per-Swatch Fit Error (dE)</p>
      </div>
      <canvas id="profErrorCanvas" class="prof-canvas" style="height:140px"></canvas>
    </div>
  `;

  let detailBody = "";
  if (section === "chart") {
    detailBody = `
      ${chartSection}
      ${renderProfileMetadata(filamentId)}
    `;
  } else if (section === "swatches") {
    detailBody = `
      ${renderSwatchComparisonTable(filamentId)}
      ${renderStripErrorSummary(filamentId)}
    `;
  } else if (section === "data") {
    detailBody = renderProfileDataSources(filamentId);
  }

  return `
    <div class="profile-detail-content">
      ${headerHtml}
      <div class="prof-detail-tabs">
        <button class="prof-tab-btn ${tabActive("chart")}" data-prof-tab="chart">Charts</button>
        <button class="prof-tab-btn ${tabActive("swatches")}" data-prof-tab="swatches">Swatches</button>
        <button class="prof-tab-btn ${tabActive("data")}" data-prof-tab="data">Data Sources</button>
      </div>
      <div class="prof-detail-body">
        ${detailBody}
      </div>
      <div class="profile-action-row">
        <button class="primary-button small profile-action-btn" data-profile-action="refit-single" ${profileFitDisabled}>Refit</button>
        <button class="${isActive ? "profile-deactivate-btn" : "profile-activate-btn"} profile-action-btn" data-profile-action="${isActive ? "deactivate" : "activate"}" ${profileFitDisabled}>${isActive ? "Deactivate" : "Activate"}</button>
      </div>
    </div>
  `;
}

// ── Data sources tab ────────────────────────────────────────────────────────

function renderProfileDataSources(filamentId) {
  const curveData = profilesState.curveCache?.[filamentId];
  if (!curveData?.sources) {
    return `<div class="profile-meta-block"><span class="muted-line">No source data loaded</span></div>`;
  }

  const categories = [
    { key: "solo", label: "Solo strips", desc: "Single-filament strips (primary fit data)" },
    { key: "thin", label: "Thin / crosscal strips", desc: "Multi-filament stacked strips" },
    { key: "fixed_role", label: "Fixed role data", desc: "Data from fixed-layer positions" },
    { key: "crosscal", label: "Cross-calibration", desc: "Cross-calibration pair data" },
  ];

  let html = "";
  for (const cat of categories) {
    const pts = curveData.sources[cat.key];
    if (!pts || pts.length === 0) continue;

    // Group by strip label
    const byStrip = {};
    for (const pt of pts) {
      const label = pt.strip_label || "unknown";
      if (!byStrip[label]) byStrip[label] = [];
      byStrip[label].push(pt);
    }

    const stripBlocks = Object.entries(byStrip).map(([label, points]) => {
      const rowsHtml = points.map((pt) => `
        <tr>
          <td class="mono">${Number(pt.d).toFixed(2)}</td>
          <td class="mono">${pt.T_r != null ? pt.T_r.toFixed(4) : "&mdash;"}</td>
          <td class="mono">${pt.T_g != null ? pt.T_g.toFixed(4) : "&mdash;"}</td>
          <td class="mono">${pt.T_b != null ? pt.T_b.toFixed(4) : "&mdash;"}</td>
        </tr>
      `).join("");
      return `
        <div class="prof-source-strip">
          <div class="prof-source-strip-head">${label} <span class="muted-line">(${points.length} pts)</span></div>
          <table class="data-table compact-table">
            <thead><tr><th>d (mm)</th><th>T_r</th><th>T_g</th><th>T_b</th></tr></thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>`;
    }).join("");

    html += `
      <div class="profile-crosscal-block">
        <div class="prof-section-head">
          <p class="panel-kicker">${cat.label}</p>
          <span class="muted-line">${pts.length} points from ${Object.keys(byStrip).length} strip${Object.keys(byStrip).length !== 1 ? "s" : ""}</span>
        </div>
        <p class="small-copy" style="margin-bottom:8px">${cat.desc}</p>
        ${stripBlocks}
      </div>`;
  }

  if (!html) {
    html = `<div class="profile-meta-block"><span class="muted-line">No source data found for this filament</span></div>`;
  }

  return html;
}

// ── Bind profile interactions ───────────────────────────────────────────────

function bindProfileActions() {
  const container = document.getElementById("tableContainer");
  if (!container) return;

  // Sidebar filament selection
  container.querySelectorAll("[data-profile-filament]").forEach((item) => {
    item.addEventListener("click", () => {
      selectProfileFilament(item.dataset.profileFilament);
    });
  });

  // Detail tab switching
  container.querySelectorAll("[data-prof-tab]").forEach((btn) => {
    btn.addEventListener("click", () => {
      profilesState.detailSection = btn.dataset.profTab;
      renderProfilesView();
      const fid = profilesState.selectedFilamentId;
      if (fid && profilesState.detailSection === "chart") {
        requestAnimationFrame(() => _drawProfileCanvasCharts(fid));
      }
    });
  });

  // Sort headers in swatch table
  container.querySelectorAll("[data-crosscal-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.crosscalSort;
      if (profilesState.crosscalSortKey === key) {
        profilesState.crosscalSortDir = profilesState.crosscalSortDir === "asc" ? "desc" : "asc";
      } else {
        profilesState.crosscalSortKey = key;
        profilesState.crosscalSortDir = ["d", "strip_label", "pair"].includes(key) ? "asc" : "desc";
      }
      renderProfilesView();
      const fid = profilesState.selectedFilamentId;
      if (fid && profilesState.detailSection === "chart") {
        requestAnimationFrame(() => _drawProfileCanvasCharts(fid));
      }
    });
  });

  // Profile action buttons
  document.querySelectorAll(".profile-action-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const action = btn.dataset.profileAction;
      const fid = profilesState.selectedFilamentId;
      const profileWriteActions = ["refit-all", "refit-single", "fit", "activate", "deactivate"];
      if (isProfileFitJobRunning() && profileWriteActions.includes(action)) {
        showProfileToast("Profile fitting is already running");
        return;
      }

      if (action === "refit-all") {
        try {
          await runFitAllProfilesWithProgress();
        } catch (err) {
          // The shared runner has already surfaced the failure.
        }
        return;
      }

      if (action === "refit-single" || action === "fit") {
        if (!fid) { showProfileToast("Select a filament first"); return; }
        btn.disabled = true;
        btn.textContent = "Fitting...";
        try {
          const result = await fitProfile(fid);
          showProfileToast(`Fitted ${fid}: ${result.n_knots} knots`);
          delete profilesState.profileCache[fid];
          delete profilesState.curveCache[fid];
          delete profilesState.swatchCache[fid];
          delete profilesState.errorCache[fid];
          if (typeof handleRefresh === "function") await handleRefresh();
          renderProfilesView();
          selectProfileFilament(fid);
        } catch (err) {
          showProfileToast("Fit failed: " + err.message);
        } finally {
          btn.disabled = false;
          btn.textContent = action === "fit" ? "Fit Profile" : "Refit";
        }
        return;
      }

      if (action === "activate") {
        if (!fid) { showProfileToast("Select a filament first"); return; }
        try {
          await activateProfile(fid);
          showProfileToast(`${fid} activated for Prisma`);
          delete profilesState.profileCache[fid];
          selectProfileFilament(fid);
        } catch (err) {
          showProfileToast("Activate failed: " + err.message);
        }
        return;
      }

      if (action === "deactivate") {
        if (!fid) { showProfileToast("Select a filament first"); return; }
        try {
          await deactivateProfile(fid);
          showProfileToast(`${fid} deactivated`);
          delete profilesState.profileCache[fid];
          selectProfileFilament(fid);
        } catch (err) {
          showProfileToast("Deactivate failed: " + err.message);
        }
        return;
      }

      if (action === "refit-selected") {
        showProfileToast("Refit Selected -- batch selection coming soon");
        return;
      }

      showProfileToast("Coming soon");
    });
  });
}

// ── Main profiles view render ───────────────────────────────────────────────

function renderProfilesView() {
  tableToolbar.className = "toolbar-inline";

  const profiled = data.filaments.filter((f) => f.has_profile).length;
  const withStrips = data.filaments.filter((f) => f.has_strips || f.processed_count > 0).length;
  tableSummary.textContent = `${profiled} profiled / ${withStrips} with data / ${data.filaments.length} total`;
  const fitAllRunning = isProfileFitJobRunning();
  tableToolbar.innerHTML = `
    <button class="primary-button small profile-action-btn" data-profile-action="refit-all" ${fitAllRunning ? "disabled" : ""}>${fitAllRunning ? "Fitting\u2026" : "Refit All"}</button>
  `;

  tableContainer.innerHTML = `
    ${renderProfileFitProgressPanel()}
    ${renderProfileCoverageBar()}
    <div class="profile-layout">
      <div class="profile-sidebar-list">
        <div class="prof-sidebar-header">
          <p class="panel-kicker" style="margin:0">Filaments</p>
        </div>
        ${renderProfileSidebar()}
        <div class="profile-sidebar-legend">
          <span class="profile-icon-ok">&#10003;</span> fitted &nbsp;
          <span class="profile-icon-stale">&#9888;</span> stale &nbsp;
          <span class="profile-icon-strips">&#9679;</span> strips &nbsp;
          <span class="profile-icon-missing">&#10007;</span> none
        </div>
      </div>
      <div class="profile-detail-panel panel inset-panel">
        ${renderProfileDetailPanel()}
      </div>
    </div>
    ${renderBatchAuditSummary()}
  `;

  bindProfileActions();
}

// ── Processing Dashboard ──────────────────────────────────────────────────────

function getProcessingCounts() {
  const assigned = data.samples.filter((e) => e._processing_status === "assigned" || (e._assigned_image && e._assigned_blank_id && !e.processed && e._processing_status !== "processed" && e._processing_status !== "failed" && e._processing_status !== "flagged")).length;
  const processed = data.samples.filter((e) => e._processing_status === "processed").length;
  const failed = data.samples.filter((e) => e._processing_status === "failed").length;
  const flagged = data.samples.filter((e) => e._processing_status === "flagged" || e._flag_reason).length;
  return { assigned, processed, failed, flagged };
}

function getProcessingCards() {
  // Only samples that need action — not already-processed ones
  const cards = data.samples.filter((e) => {
    return e._processing_status === "assigned" ||
           e._processing_status === "failed" ||
           e._processing_status === "flagged" || e._flag_reason;
  });
  const statusOrder = (e) => {
    if (e._processing_status === "assigned") return 0;  // ready to process — show first
    if (e._processing_status === "failed") return 1;
    if (e._processing_status === "flagged" || e._flag_reason) return 2;
    return 3;  // processed
  };
  return [...cards].sort((a, b) => statusOrder(a) - statusOrder(b));
}

function processingCardStatus(exp) {
  const status = sampleStatusMeta(exp);
  if (status.cls === "ready") return { label: "ready to process", cls: "ready" };
  return status;
}

function buildSwatchTilesHtml(exp) {
  // Builds swatch tiles matching the compact sample strip style.
  // Uses measured colors when available, falls back to opacity approximation.
  const swatches = exp._measurements?.swatches;
  const thicknesses = exp.variable_thicknesses_mm || [];
  const fallbackHex = exp.variable_hex || "#cccccc";
  const n = swatches?.length || thicknesses.length;
  if (n === 0) return '<span class="small-copy">No swatch data</span>';

  const cols = n;

  if (swatches && swatches.length > 0) {
    const tiles = swatches.map((sw) => {
      const bg = swatchDisplayDomain(sw).hex || fallbackHex;
      const excluded = sw.fit_state === "excluded";
      return `<div class="swatch-tile${excluded ? " is-excluded" : ""}">
        <div class="swatch-chip" style="background:${bg}"></div>
        <div class="swatch-body">
          <span>${bg}</span>
          <span>${Number(sw.nominal_thickness_mm).toFixed(2)} mm</span>
        </div>
      </div>`;
    }).join("");
    return `<div class="swatch-strip" style="grid-template-columns:repeat(${cols},1fr)">${tiles}</div>`;
  }

  // Fallback: no measurements, use opacity approximation
  const maxT = Math.max(...thicknesses.map(Number), 0.01);
  const tiles = thicknesses.map((t) => {
    const opacity = Math.max(0.08, Number(t) / maxT);
    return `<div class="swatch-tile">
      <div class="swatch-chip" style="background:${fallbackHex};opacity:${opacity.toFixed(2)}"></div>
      <div class="swatch-body">
        <span>${Number(t).toFixed(2)} mm</span>
      </div>
    </div>`;
  }).join("");
  return `<div class="swatch-strip" style="grid-template-columns:repeat(${cols},1fr)">${tiles}</div>`;
}

function renderProcessingCard(exp) {
  const status = processingCardStatus(exp);
  const fil = filamentMeta(exp.variable_filament_id);
  const filName = fil ? fil.display_name || fil.color_name : exp.variable_filament_id;
  const hex = fil?.hex || exp.variable_hex || "#ddd";
  const sid = exp.sample_id;
  const isProcessing = processingState.singleRunningSampleIds?.has(sid);
  const disableActions = processingState.batchRunning;
  const hasResults = exp._processing_status === "processed" || exp._processing_status === "flagged" || exp._processing_status === "failed";
  const flagReason = exp._flag_reason ? ` — ${exp._flag_reason}` : "";

  const ORIENT_ARROWS = ["\u2191", "\u2192", "\u2193", "\u2190"];
  const orientRot = exp._orientation_rots;
  const orientLabel = orientRot != null ? ORIENT_ARROWS[orientRot] : "";
  const imgName = exp._assigned_image || "";
  const blankId = exp._assigned_blank_id || "";

  const photoThumb = imgName
    ? `<div class="pq-thumb"><img src="${previewUrl(imgName)}" alt="photo" onerror="this.style.display='none'"><span class="pq-thumb-label">${imgName.replace(/\.[^.]+$/, "")}</span></div>`
    : `<div class="pq-thumb pq-thumb-empty"><span>no photo</span></div>`;
  const blankFilename = blankId && data.blanks
    ? (data.blanks.find((b) => b.blank_id === blankId)?.original_filename || "")
    : "";
  const blankThumb = blankId
    ? `<div class="pq-thumb"><img src="${previewUrl(blankFilename)}" alt="blank" onerror="this.style.display='none'"><span class="pq-thumb-label">${blankId}</span></div>`
    : `<div class="pq-thumb pq-thumb-empty"><span>no blank</span></div>`;

  // For "assigned" (not yet processed) samples, show an info card with strip + thumbnails
  if (!hasResults) {
    return `
      <div class="proc-card" data-sample-id="${sid}">
        <div class="proc-card-header">
          <span class="color-chip" style="background:${hex}"></span>
          <span class="mono">${sid}</span>
          <span class="proc-card-filament">${filName}</span>
          <span class="pq-header-actions" style="margin-left:auto">
            <button class="ghost-button xs proc-reassign-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Reassign</button>
            <button class="ghost-button xs proc-flag-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Flag</button>
            <button class="ghost-button xs proc-reprocess-btn${isProcessing ? " is-busy" : ""}" data-sample-id="${sid}" ${(isProcessing || disableActions) ? "disabled" : ""}>
              ${isProcessing ? '<span class="proc-spinner"></span> Processing...' : "Process"}
            </button>
          </span>
        </div>
        <div class="pq-info-row">
          <div class="pq-strip-col">${buildStripMiniTable(exp)}</div>
          ${photoThumb}
          ${blankThumb}
          ${orientLabel ? `<div class="pq-orient-arrow" title="Open side direction">${orientLabel}</div>` : ""}
        </div>
      </div>
    `;
  }

  return `
    <div class="proc-card" data-sample-id="${sid}">
      <div class="proc-card-header">
        <span class="color-chip" style="background:${hex}"></span>
        <span class="mono">${sid}</span>
        <span class="proc-card-filament">${filName}</span>
        <span class="status-pill ${status.cls}">${status.label}</span>
        <span class="pq-header-actions" style="margin-left:auto">
          <button class="ghost-button xs proc-reassign-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Reassign</button>
          <button class="ghost-button xs proc-unflag-btn" data-sample-id="${sid}" style="color:#2e7d32;border-color:#2e7d3266" ${disableActions ? "disabled" : ""}>Unflag</button>
          <button class="ghost-button xs proc-manual-btn" data-sample-id="${sid}" ${disableActions ? "disabled" : ""}>Manual Process</button>
        </span>
      </div>
      <div class="pq-info-row">
        <div class="pq-strip-col">${buildStripMiniTable(exp)}</div>
        ${photoThumb}
        ${blankThumb}
        ${orientLabel ? `<div class="pq-orient-arrow" title="Open side direction">${orientLabel}</div>` : ""}
      </div>
    </div>
  `;
}

function renderPostProcessingCard(exp) {
  const fil = filamentMeta(exp.variable_filament_id);
  const filName = fil ? fil.display_name || fil.color_name : exp.variable_filament_id;
  const hex = fil?.hex || exp.variable_hex || "#ddd";
  const sid = exp.sample_id;
  const variableBrand = fil?.manufacturer || exp.manufacturer || "";
  const variableName = fil?.color_name || fil?.display_name || exp.variable_color_name || exp.variable_filament_id;
  const fixedHeaderRun = sampleFilamentRoleLines(exp)
    .filter((line) => line.roleKind === "fixed")
    .map((line) => {
    return `
      <span class="proc-filament-run is-fixed" title="${_escAttr(line.layerLabel)}">
        <span class="color-chip" style="background:${line.hex || '#cccccc'}"></span>
        <span class="proc-filament-token">${_escHtml(line.name)}</span>
      </span>
    `;
  }).join("");

  // Thumbnails: source + blank
  const imgName = exp._assigned_image || "";
  const blankId = exp._assigned_blank_id || "";
  const blankFilename = blankId && data.blanks
    ? (data.blanks.find((blank) => blank.blank_id === blankId)?.original_filename || "")
    : "";
  const srcThumb = imgName
    ? `<div class="pq-thumb"><img src="${sampleThumbnailUrl(sid, "source", true)}" alt="source" onerror="this.style.display='none'"><span class="pq-thumb-label">Source</span></div>`
    : "";
  const blankThumb = blankId
    ? `<div class="pq-thumb"><img src="${previewUrl(blankFilename)}" alt="blank" onerror="this.style.display='none'"><span class="pq-thumb-label">Blank</span></div>`
    : "";

  // Build extracted strip with geometry-aligned labels, plus a mock strip for comparison.
  const swatches = exp._measurements?.swatches;
  const thicknesses = exp.variable_thicknesses_mm || [];
  const n = swatches?.length || thicknesses.length;
  let stripPairHtml = "";
  if (n > 0) {
    const metrics = sampleStripMetrics(exp, n);
    const hasMeasuredSwatches = Array.isArray(swatches) && swatches.length > 0;
    const swatchData = (hasMeasuredSwatches ? [...swatches].sort((a, b) => {
      const ai = Number(a.swatch_index ?? Number.MAX_SAFE_INTEGER);
      const bi = Number(b.swatch_index ?? Number.MAX_SAFE_INTEGER);
      if (ai !== bi) return ai - bi;
      return Number(a.nominal_thickness_mm ?? 0) - Number(b.nominal_thickness_mm ?? 0);
    }) : thicknesses.map((t) => ({ nominal_thickness_mm: t, display: null })));
    const displaySwatches = Array.from({ length: metrics.n }, (_, index) => swatchData[index] || null);
    const labelCells = displaySwatches.map((sw) => {
      const thickness = Number(sw?.nominal_thickness_mm);
      return `<span class="ppr-thickness-label">${Number.isFinite(thickness) ? thickness.toFixed(2) : ""}</span>`;
    }).join("");
    const thicknessLabels = `<span class="ppr-thickness-spacer" aria-hidden="true"></span>${labelCells}<span class="ppr-thickness-spacer" aria-hidden="true"></span>`;
    const extractedEl = `<div class="ppr-extracted-stack">
      <div class="ppr-extracted-strip">
        <img src="${sampleThumbnailUrl(sid, "strip", true)}" alt="extracted" onerror="this.parentElement.innerHTML='<span class=small-copy>No strip</span>'">
      </div>
      <div class="ppr-thickness-row" style="grid-template-columns:${metrics.gridCols}">${thicknessLabels}</div>
    </div>`;

    // Mock strip: same swatch count as the physical strip geometry.
    const cols = `repeat(${metrics.n}, 1fr)`;
    const maxThickness = Math.max(...displaySwatches.map((sw) => Number(sw?.nominal_thickness_mm)).filter(Number.isFinite), 0.01);
    const mockTiles = displaySwatches.map((sw) => {
      const displayHex = swatchDisplayDomain(sw).hex;
      const bg = displayHex || hex;
      const thickness = Number(sw?.nominal_thickness_mm);
      const opacity = displayHex ? 1 : Math.max(0.08, Math.min(1, Number.isFinite(thickness) ? thickness / maxThickness : 0.08));
      return `<div class="ppr-mock-swatch" style="background:${bg};${displayHex ? "" : `opacity:${opacity.toFixed(2)};`}"></div>`;
    }).join("");
    const mockEl = `<div class="ppr-mock-strip" style="grid-template-columns:${cols}">${mockTiles}</div>`;
    stripPairHtml = `<div class="ppr-strip-stack">
      <div class="ppr-strip-pair">${extractedEl}${mockEl}</div>
    </div>`;
  }

  return `
    <div class="proc-card" data-sample-id="${sid}">
      <div class="proc-card-header">
        <span class="mono">${sid}</span>
        <div class="proc-card-filament-run">
          <span class="proc-filament-run is-variable">
            <span class="color-chip" style="background:${hex}"></span>
            <span class="proc-filament-token">${variableBrand ? `${variableBrand} ` : ""}${variableName}</span>
          </span>
          ${fixedHeaderRun}
        </div>
        <span class="pq-header-actions" style="margin-left:auto">
          <button class="ghost-button xs pq-reprocess-btn" data-sample-id="${sid}" style="color:#e65100;border-color:#e6510066">Reject</button>
          <button class="ghost-button xs pq-dismiss-btn" data-sample-id="${sid}" style="color:#2e7d32;border-color:#2e7d3266">Accept</button>
        </span>
      </div>
      <div class="ppr-body">
        <div class="ppr-thumbs">${srcThumb}${blankThumb}</div>
        ${stripPairHtml}
      </div>
    </div>
  `;
}

// ── Lazy per-swatch measurement hydration (doc 33 Workstream A) ───────────────
// The slim /api/samples list omits per-swatch color domains. Surfaces that
// render measured swatch domains fetch them on demand from GET /api/samples/{id}
// and cache the result onto
// data.samples[i]._measurements so the existing find(...) consumers keep working.
const _measurementHydrationInFlight = new Set();

async function hydrateSampleMeasurements(sampleId, { force = false } = {}) {
  const exp = data.samples.find((e) => e.sample_id === sampleId);
  if (!exp) return null;
  if (!force && exp._measurements) return exp._measurements;
  if (_measurementHydrationInFlight.has(sampleId)) return null;
  _measurementHydrationInFlight.add(sampleId);
  try {
    const detail = await fetchSampleDetail(sampleId);
    exp._measurements = (detail && detail.measurements) || null;
    // Keep the summary counts coherent with freshly-fetched detail.
    const sw = exp._measurements?.swatches || [];
    exp._n_swatches = sw.length;
    exp._n_excluded = sw.filter((s) => s.fit_state === "excluded").length;
    return exp._measurements;
  } catch (err) {
    console.error(`[detail] hydrate ${sampleId} failed:`, err);
    return null;
  } finally {
    _measurementHydrationInFlight.delete(sampleId);
  }
}

// Hydrate samples that should have measurements, then re-render once any arrive.
// Idempotent: once cached, nothing is pending and no re-render is scheduled.
function ensureMeasurementsThenRerender(sampleIds, rerenderFn) {
  const pending = [];
  for (const sid of sampleIds) {
    const exp = data.samples.find((e) => e.sample_id === sid);
    if (exp && sampleHasMeasurementOutput(exp) && !exp._measurements && !_measurementHydrationInFlight.has(sid)) {
      pending.push(sid);
    }
  }
  if (pending.length === 0) return;
  Promise.all(pending.map((sid) => hydrateSampleMeasurements(sid))).then((results) => {
    if (results.some(Boolean)) rerenderFn();
  });
}

function renderProcessingDashboard() {
  tableToolbar.className = "toolbar-inline";
  tableSummary.textContent = "";
  tableToolbar.innerHTML = "";

  // Left pane data: pre-processing
  const allCards = getProcessingCards();
  const assigned = allCards.filter((e) => e._processing_status === "assigned");
  const failed = allCards.filter((e) => e._processing_status === "failed");
  const flagged = allCards.filter((e) => e._processing_status === "flagged" || (e._flag_reason && e._processing_status !== "processed"));

  // Right pane data: post-processing (exclude accepted/reviewed)
  const processed = data.samples.filter((e) =>
    e._processing_status === "processed" && !e._review_accepted
  );

  if (!processingState._collapseState) processingState._collapseState = {};
  const cs = processingState._collapseState;

  function sectionHtml(key, label, items, titleActionsHtml = "", extraAfterTitle = "") {
    const collapsed = cs[key];
    const caret = collapsed ? "&#x25B6;" : "&#x25BC;";
    const content = collapsed ? "" : (items.length > 0
      ? items.map((cardFn) => cardFn()).join("")
      : `<div class="pq-empty-section"><span class="small-copy">None</span></div>`);
    const extra = (!collapsed && extraAfterTitle) ? extraAfterTitle : "";
    return `<div class="import-section-title" data-collapse-key="${key}">
        <div class="import-section-title-main">
          <span class="collapse-caret">${caret}</span><span>${label} (${items.length})</span>
        </div>
        ${titleActionsHtml ? `<div class="import-section-title-actions">${titleActionsHtml}</div>` : ""}
      </div>${extra}${content}`;
  }

  const processAllBtnLabel = processingState.batchRunning && processingState.batchProgress
    ? `<span class="proc-spinner"></span> Processing ${Math.min(processingState.batchProgress.completed || 0, processingState.batchProgress.total || 0)} / ${processingState.batchProgress.total || assigned.length}`
    : `Process All (${assigned.length})`;
  const processAllBtn = assigned.length > 0
    ? `<button class="primary-button xs" id="processAllBtn" ${processingState.batchRunning ? "disabled" : ""}>${processAllBtnLabel}</button>`
    : "";

  const manualFailedBtn = failed.length > 0 ? `<div class="pq-section-action">
      <button class="ghost-button xs manual-all-btn" data-queue="failed" ${processingState.batchRunning ? "disabled" : ""}>Manual Process All (${failed.length})</button>
    </div>` : "";

  const manualFlaggedBtn = flagged.length > 0 ? `<div class="pq-section-action">
      <button class="ghost-button xs manual-all-btn" data-queue="flagged" ${processingState.batchRunning ? "disabled" : ""}>Manual Process All (${flagged.length})</button>
    </div>` : "";

  const batch = processingState.batchProgress;
  const processedTitleActions = processed.length > 0 ? `
    <button class="ghost-button xs" id="processedRejectAllBtn" ${processingState.batchRunning ? "disabled" : ""}>Reject All</button>
    <button class="ghost-button xs" id="processedAcceptAllBtn" ${processingState.batchRunning ? "disabled" : ""}>Accept All</button>
  ` : "";
  const batchProgressPanel = batch ? (() => {
    const completed = Math.min(batch.completed || 0, batch.total || 0);
    const total = batch.total || 0;
    const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
    const remaining = Math.max(total - completed, 0);
    const heading = batch.done ? "Batch complete" : "Batch processing";
    const currentLabel = batch.currentSampleId
      ? `<span>Current: <span class="mono">${batch.currentSampleId}</span></span>`
      : (batch.done ? `<span>Current: <span class="mono">done</span></span>` : "");
    return `
      <div class="pq-batch-status${batch.done ? " is-complete" : ""}">
        <div class="pq-batch-head">
          <strong>${heading}</strong>
          <span>${completed} / ${total}</span>
        </div>
        <div class="pq-batch-bar">
          <div class="pq-batch-bar-fill" style="width:${percent}%"></div>
        </div>
        <div class="pq-batch-meta">
          ${currentLabel}
          <span>Processed: ${batch.succeeded || 0}</span>
          <span>Flagged: ${batch.flagged || 0}</span>
          <span>Failed: ${batch.failed || 0}</span>
          <span>Remaining: ${remaining}</span>
        </div>
      </div>
    `;
  })() : "";

  const leftSections = [
    sectionHtml("pq-assigned", "Ready to Process", assigned.map((e) => () => renderProcessingCard(e)), processAllBtn),
    sectionHtml("pq-failed", "Failed", failed.map((e) => () => renderProcessingCard(e)), manualFailedBtn),
    sectionHtml("pq-flagged", "Flagged for Manual Processing", flagged.map((e) => () => renderProcessingCard(e)), manualFlaggedBtn),
  ].join("");

  const rightSections = sectionHtml(
    "pq-processed", "Processed", processed.map((e) => () => renderPostProcessingCard(e)), processedTitleActions
  );

  tableContainer.innerHTML = `
    <div class="proc-split-layout">
      <div class="proc-split-pane">
        <div class="proc-pane-title">Pre-Processing Queue</div>
        ${batchProgressPanel}
        <div class="proc-card-list">
          ${leftSections}
        </div>
      </div>
      <div class="proc-split-divider"></div>
      <div class="proc-split-pane proc-review-pane">
        <div class="proc-pane-title">Post-Processing Review</div>
        <div class="proc-card-list">
          ${rightSections}
        </div>
      </div>
    </div>
  `;

  const processAllBtnEl = document.getElementById("processAllBtn");
  if (processAllBtnEl && !processingState.batchRunning) {
    processAllBtnEl.addEventListener("click", (e) => {
      e.stopPropagation();
      handleProcessAll();
    });
  }

  document.getElementById("processedAcceptAllBtn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    let accepted = 0;
    const errors = [];
    for (const exp of processed) {
      try {
        await updateSample(exp.sample_id, { review_accepted: true });
        accepted += 1;
      } catch (err) {
        errors.push(`${exp.sample_id}: ${err.message}`);
      }
    }
    await handleRefresh();
    renderProcessingDashboard();
    if (errors.length) {
      showImportToast(`Accepted ${accepted}; ${errors.length} failed`, "error");
    } else {
      showImportToast(`Accepted ${accepted} processed samples`, "success");
    }
  });

  document.getElementById("processedRejectAllBtn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    let rejected = 0;
    const errors = [];
    for (const exp of processed) {
      try {
        await rejectSample(exp.sample_id);
        rejected += 1;
      } catch (err) {
        errors.push(`${exp.sample_id}: ${err.message}`);
      }
    }
    await handleRefresh();
    renderProcessingDashboard();
    if (errors.length) {
      showImportToast(`Rejected ${rejected}; ${errors.length} failed`, "error");
    } else {
      showImportToast(`Rejected ${rejected} processed samples`, "success");
    }
  });

  // Collapsible section toggling
  tableContainer.querySelectorAll(".import-section-title-actions").forEach((actions) => {
    actions.addEventListener("click", (e) => e.stopPropagation());
  });
  tableContainer.querySelectorAll(".import-section-title[data-collapse-key]").forEach((title) => {
    title.addEventListener("click", () => {
      const key = title.dataset.collapseKey;
      if (!processingState._collapseState) processingState._collapseState = {};
      processingState._collapseState[key] = !processingState._collapseState[key];
      renderProcessingDashboard();
    });
  });

  tableContainer.querySelectorAll(".proc-reprocess-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleReprocessSingle(btn.dataset.sampleId);
    });
  });
  tableContainer.querySelectorAll(".proc-flag-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleFlagSample(btn.dataset.sampleId, "Manual flag");
    });
  });
  tableContainer.querySelectorAll(".proc-unflag-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleUnflagSample(btn.dataset.sampleId);
    });
  });

  // Reassign buttons — unassign image, sending sample back to Assign Images
  tableContainer.querySelectorAll(".proc-reassign-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const sid = btn.dataset.sampleId;
      try {
        await unassignImage(sid);
        await unflagSample(sid).catch(() => {}); // clear flag if any
        await handleRefresh();
        renderProcessingDashboard();
        showImportToast(`${sid} sent back to Assign Images`, "ok");
      } catch (err) {
        showImportToast(err.message || "Failed to reassign", "error");
      }
    });
  });

  // Manual process buttons (single + batch)
  tableContainer.querySelectorAll(".proc-manual-btn").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openManualProcessing([btn.dataset.sampleId]);
    });
  });
  tableContainer.querySelectorAll(".manual-all-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const queue = btn.dataset.queue;
      const items = queue === "failed" ? failed : flagged;
      openManualProcessing(items.map((e) => e.sample_id));
    });
  });

  // Post-processing pane: Reject → clear measurements and return to assigned
  tableContainer.querySelectorAll(".pq-reprocess-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const sid = btn.dataset.sampleId;
      try {
        await rejectSample(sid);
        await handleRefresh();
        renderProcessingDashboard();
        showImportToast(`${sid} sent back for reprocessing`, "ok");
      } catch (err) {
        showImportToast(err.message || "Reject failed", "error");
      }
    });
  });

  // Post-processing pane: Dismiss → accept the result (no action needed, just remove from view)
  tableContainer.querySelectorAll(".pq-dismiss-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const sid = btn.dataset.sampleId;
      // Persist review_accepted to the server so it survives reload
      try {
        await updateSample(sid, { review_accepted: true });
      } catch (err) {
        console.error("[review] Failed to persist accept for", sid, err);
      }
      // Also update local data so the card disappears immediately
      const exp = data.samples.find((x) => x.sample_id === sid);
      if (exp) exp._review_accepted = true;
      renderProcessingDashboard();
    });
  });

  // The "Processed" review cards render per-swatch hex; hydrate their color
  // from the detail endpoint (the slim list omits it) and re-render once it lands.
  ensureMeasurementsThenRerender(processed.map((e) => e.sample_id), renderProcessingDashboard);
}

async function handleProcessAll() {
  const targets = getProcessingCards()
    .filter((exp) => exp._processing_status === "assigned")
    .map((exp) => exp.sample_id);
  if (!targets.length) return;

  processingState.batchRunning = true;
  processingState.batchProgress = {
    total: targets.length,
    completed: 0,
    succeeded: 0,
    flagged: 0,
    failed: 0,
    currentSampleId: null,
    errors: [],
    done: false,
  };
  renderWorkspace();

  for (const sampleId of targets) {
    processingState.batchProgress.currentSampleId = sampleId;
    processingState.singleRunningSampleIds.add(sampleId);
    renderWorkspace();

    try {
      const result = typeof processSingle === "function" ? await processSingle(sampleId) : null;
      const status = result?.status || "failed_detection";
      if (status === "success") {
        processingState.batchProgress.succeeded += 1;
      } else if (status === "low_confidence") {
        processingState.batchProgress.flagged += 1;
      } else {
        processingState.batchProgress.failed += 1;
        processingState.batchProgress.errors.push({
          sample_id: sampleId,
          error: result?.error_detail || status,
        });
      }
    } catch (err) {
      console.error(`[processing] Batch item failed for ${sampleId}:`, err);
      processingState.batchProgress.failed += 1;
      processingState.batchProgress.errors.push({
        sample_id: sampleId,
        error: err.message,
      });
    } finally {
      processingState.batchProgress.completed += 1;
      processingState.batchProgress.currentSampleId = null;
      processingState.singleRunningSampleIds.delete(sampleId);
    }

    if (typeof handleRefresh === "function") {
      await handleRefresh({ ensureAssets: false });
    } else {
      renderWorkspace();
    }
  }

  processingState.batchRunning = false;
  if (processingState.batchProgress) {
    processingState.batchProgress.done = true;
    processingState.batchProgress.currentSampleId = null;
  }
  renderWorkspace();
}

async function handleReprocessSingle(sampleId) {
  processingState.singleRunningSampleIds.add(sampleId);
  renderWorkspace();
  try {
    if (typeof processSingle === "function") await processSingle(sampleId);
    if (typeof handleRefresh === "function") await handleRefresh();
  } catch (err) {
    console.error("[processing] Reprocess failed:", err);
    showImportToast(`Re-process failed: ${err.message}`, "error", { durationMs: 4500 });
  } finally {
    processingState.singleRunningSampleIds.delete(sampleId);
  }
  renderWorkspace();
}

async function handleFlagSample(sampleId, reason) {
  try {
    if (typeof flagSample === "function") await flagSample(sampleId, reason || "Manual flag");
    if (typeof handleRefresh === "function") await handleRefresh();
  } catch (err) {
    console.error("[processing] Flag failed:", err);
  }
  renderWorkspace();
}

async function handleUnflagSample(sampleId) {
  try {
    if (typeof unflagSample === "function") await unflagSample(sampleId);
    if (typeof handleRefresh === "function") await handleRefresh();
  } catch (err) {
    console.error("[processing] Unflag failed:", err);
  }
  renderWorkspace();
  renderProcessingDashboard();
}

async function handleExcludeSwatchFromReview(sampleId, swatchIndex, reason) {
  try {
    if (typeof excludeSwatch === "function") {
      const result = await excludeSwatch(sampleId, swatchIndex, reason || "");
      applyFitControlMutationResponse(result);
    }
    // Targeted single-sample refresh — only this sample's fit-control changed.
    // (Previously reloaded the whole library via handleRefresh.)
    await hydrateSampleMeasurements(sampleId, { force: true });
  } catch (err) {
    console.error("[processing] Exclude swatch failed:", err);
  }
  renderWorkspace();
  renderProcessingDashboard();
}

async function handleIncludeSwatchFromReview(sampleId, swatchIndex) {
  try {
    if (typeof includeSwatch === "function") {
      const result = await includeSwatch(sampleId, swatchIndex);
      applyFitControlMutationResponse(result);
    }
    // Targeted single-sample refresh — only this sample's fit-control changed.
    await hydrateSampleMeasurements(sampleId, { force: true });
  } catch (err) {
    console.error("[processing] Include swatch failed:", err);
  }
  renderWorkspace();
  renderProcessingDashboard();
}

function resetDefaultSurfaceChrome() {
  const defaultContent = document.getElementById("defaultContent");
  const panel = defaultContent?.querySelector(".main-logbook");
  const sectionHead = panel?.querySelector(".section-head");
  defaultContent?.classList.remove("model-overview-content", "modeling-overview-content", "modeling-filaments-content");
  panel?.classList.remove("model-overview-panel", "model-tab-shell", "modeling-overview-panel", "modeling-filaments-panel");
  sectionHead?.classList.remove("model-status-section-head");
}

function renderWorkspace() {
  renderModeButtons();
  renderSubtabs();
  renderStatusSummary();
  resetDefaultSurfaceChrome();
  detailActionArea.innerHTML = "";
  drawerStatusPill.innerHTML = "";
  detailWindowArea.innerHTML = "";

  const defaultContent = document.getElementById("defaultContent");
  const importView = document.getElementById("importView");

  if (currentMode === "imageProcessing" && currentSubtab === "associate") {
    // Associate Images uses the full import view layout
    if (defaultContent) defaultContent.style.display = "none";
    if (importView) importView.classList.remove("is-hidden");
    mountSubtabsInOwnedSurface();
    closeStepBuilderDrawer();
    closeFilamentBuilderPanel();
    renderImportView();
  } else {
    if (defaultContent) defaultContent.style.display = "";
    if (importView) importView.classList.add("is-hidden");
    mountSubtabsInOwnedSurface();

    if (currentMode === "logbook") {
      renderManagementLogbook();
    } else if (currentMode === "filaments") {
      closeStepBuilderDrawer();
      closeBundleMgmtDrawer();
      renderFilamentLibrary();
    } else if (currentMode === "geometries") {
      closeFilamentBuilderPanel();
      renderStepLibrary();
    } else if (currentMode === "profiles") {
      closeStepBuilderDrawer();
      closeFilamentBuilderPanel();
      renderModelsView();
    } else if (currentMode === "imageProcessing") {
      closeStepBuilderDrawer();
      closeFilamentBuilderPanel();
      if (currentSubtab === "queue") renderProcessingDashboard();
    }
  }

  syncModeTabRowWidth();
  syncRecordDrawerPosition();
  bindRowSelection();
}

modeSwitch.querySelectorAll(".mode-button").forEach((button) => {
  button.addEventListener("click", async () => {
    await activateMode(button.dataset.mode);
  });
});

bindArrowTabNavigation(modeSwitch, ".mode-button", {
  activate: (button) => activateMode(button.dataset.mode),
  focusActive: () => focusModeButton(),
  onArrowDown: () => {
    const subtabs = modeConfig[currentMode]?.subtabs || [];
    if (subtabs.length > 0) {
      focusSubtabButton();
    }
  },
});

bindArrowTabNavigation(subtabRow, ".subtab-button", {
  activate: (button) => activateSubtab(button.dataset.subtab),
  focusActive: () => focusSubtabButton(),
  onArrowUp: () => focusModeButton(),
});

function openImageLightbox(src, title) {
  if (!imageLightboxOverlay || !imageLightboxImg) return;
  imageLightboxImg.src = src;
  imageLightboxTitle.textContent = title || "Image Preview";
  imageLightboxOverlay.classList.add("is-open");
  imageLightboxOverlay.setAttribute("aria-hidden", "false");
}

function closeImageLightboxPanel() {
  if (!imageLightboxOverlay || !imageLightboxImg) return;
  imageLightboxOverlay.classList.remove("is-open");
  imageLightboxOverlay.setAttribute("aria-hidden", "true");
  imageLightboxImg.removeAttribute("src");
}

function handleDrawerEscape() {
  if (linkedSampleDrawer?.classList.contains("is-open")) {
    closeLinkedSampleDrawer();
    return true;
  }

  if (recordDrawer?.classList.contains("is-open")) {
    if (_sampleDrawerMode === "edit" && selectedRecord.kind === "sample" && selectedRecord.id) {
      const exp = data.samples.find((item) => item.sample_id === selectedRecord.id);
      if (!exp) return false;
      _sampleDrawerMode = null;
      renderSidebarForSample(exp, { expanded: _sampleInspectExpanded });
      return true;
    }

    if (_filamentDrawerMode === "edit" && selectedRecord.kind === "filament" && selectedRecord.id) {
      const fil = data.filaments.find((item) => item.filament_id === selectedRecord.id) || _filamentDrawerData;
      if (!fil) return false;
      _filamentDrawerMode = "view";
      _filamentDrawerData = fil;
      _renderFilamentDrawerView(fil);
      return true;
    }

    if (selectedRecord.kind === "step" && document.getElementById("discardStepBtn")) {
      return false;
    }

    clearSelectionAndDrawer();
    return true;
  }

  if (isStepBuilderOpen()) {
    closeStepBuilderDrawer();
    return true;
  }

  if (isBundleMgmtOpen()) {
    closeBundleMgmtDrawer();
    return true;
  }

  return false;
}

function bindDrawerLightboxButtons(root = detailSidebar) {
  root?.querySelectorAll("[data-lightbox-src]").forEach((button) => {
    button.addEventListener("click", () => {
      openImageLightbox(button.dataset.lightboxSrc, button.dataset.lightboxTitle || "Image Preview");
    });
  });
}

closeImageLightbox?.addEventListener("click", closeImageLightboxPanel);
imageLightboxOverlay?.addEventListener("click", () => {
  closeImageLightboxPanel();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (imageLightboxOverlay?.classList.contains("is-open")) {
    e.preventDefault();
    closeImageLightboxPanel();
    return;
  }
  if (!handleDrawerEscape()) return;
  e.preventDefault();
});

document.addEventListener("keydown", async (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  if (!recordDrawer?.classList.contains("is-open")) return;
  if (selectedRecord.kind !== "model_sample") return;
  if (shouldIgnoreModelingSampleArrowKey(e)) return;
  const nav = modelReviewSampleNavigationMeta(selectedRecord.id);
  const nextId = e.key === "ArrowLeft" ? nav.previousId : nav.nextId;
  if (!nextId) return;
  e.preventDefault();
  await navigateModelingSampleDetail(e.key === "ArrowLeft" ? -1 : 1);
});

// ── Data source badge ─────────────────────────────────────────────────────────

function renderDataSourceBadge() {
  let badge = document.getElementById("dataSourceBadge");
  if (!badge) {
    badge = document.createElement("span");
    badge.id = "dataSourceBadge";
    badge.className = "toolbar-chip data-source-badge";
    const topbarActions = document.querySelector(".topbar-actions");
    if (topbarActions) topbarActions.appendChild(badge);
  }
  const loadState = (typeof getApiLoadingState === "function") ? getApiLoadingState() : { state: "idle", error: "" };

  if (loadState.state === "loading") {
    badge.textContent = "Loading\u2026";
    badge.className = "toolbar-chip data-source-badge is-loading";
  } else if (_dataSource === "api") {
    badge.textContent = "Live API";
    badge.className = "toolbar-chip data-source-badge is-live";
  } else if (loadState.state === "error") {
    badge.textContent = "API error";
    badge.className = "toolbar-chip data-source-badge is-error";
    badge.title = loadState.error;
  } else {
    badge.textContent = "Waiting for API";
    badge.className = "toolbar-chip data-source-badge is-loading";
  }
}

// ── Maintenance refresh impact ───────────────────────────────────────────────

function nextMaintenanceCacheBust() {
  maintenanceCacheBust.version += 1;
  return maintenanceCacheBust.version;
}

function applyMaintenanceCacheBust(impact = {}) {
  const previewImpact = impact.invalidate_preview_cache || {};
  const sampleImpact = impact.invalidate_sample_thumbnails || {};
  if (previewImpact.all) {
    maintenanceCacheBust.allPreviews = nextMaintenanceCacheBust();
  }
  (previewImpact.filenames || []).forEach((filename) => {
    if (filename) maintenanceCacheBust.previews.set(filename, nextMaintenanceCacheBust());
  });
  (previewImpact.blank_ids || []).forEach((blankId) => {
    if (blankId) maintenanceCacheBust.blankPreviews.set(blankId, nextMaintenanceCacheBust());
  });
  if (sampleImpact.all) {
    maintenanceCacheBust.allSampleThumbnails = nextMaintenanceCacheBust();
  }
  const sampleIds = sampleImpact.sample_ids || [];
  const kinds = sampleImpact.kinds || ["source", "blank", "strip", "appearance"];
  sampleIds.forEach((sampleId) => {
    kinds.forEach((kind) => {
      if (sampleId && kind) {
        maintenanceCacheBust.sampleThumbnails.set(`${sampleId}:${kind}`, nextMaintenanceCacheBust());
      }
    });
  });
}

async function applyMaintenanceRefreshImpact(impact = {}) {
  if (!impact || impact.kind === "none") return;
  applyMaintenanceCacheBust(impact);
  const reloadImportData = impact.reload_import_data === true;
  const geometryImpact = impact.invalidate_geometry_artifacts || {};
  const reloadGeometryArtifacts = geometryImpact.all === true || (geometryImpact.geometry_ids || []).length > 0;
  const reloadAppData = reloadImportData || reloadGeometryArtifacts || impact.reload_app_data === true || impact.reload_library_data === true;
  if (reloadAppData) {
    await handleRefresh({ reloadImportData });
    return;
  }
  if (impact.rerender_workspace) {
    renderWorkspace();
  }
  if (impact.rerender_open_drawers) {
    await rerenderOpenRecordDrawerAfterRefresh();
    rerenderLinkedSampleDrawerAfterRefresh();
  }
  if (reloadImportData && typeof loadImportData === "function") {
    await loadImportData();
    renderWorkspace();
  }
}

// ── Refresh handler ──────────────────────────────────────────────────────────

function normalizeRefreshOptions(options = {}) {
  if (!options || typeof options !== "object") return {};
  if (typeof Event !== "undefined" && options instanceof Event) return { userInitiated: true };
  return options;
}

function setRefreshButtonBusy(isBusy) {
  const button = document.getElementById("refreshDataBtn");
  if (!button) return;
  button.disabled = !!isBusy;
  button.setAttribute("aria-busy", isBusy ? "true" : "false");
  button.textContent = isBusy ? "Refreshing..." : "Refresh Data";
}

function resetRefreshableUiCaches() {
  modelFittingState.predictionCache = {};
  profilesState.profileCache = {};
  profilesState.curveCache = {};
  profilesState.swatchCache = {};
  profilesState.errorCache = {};
  invalidateModelingPayloads();

  if (!photoStackModelState.isFitting && !photoStackModelState.loadingCandidate) {
    photoStackModelState.latest = null;
    photoStackModelState.candidate = null;
    photoStackModelState.predictions = null;
    photoStackModelState.loadingCandidate = false;
    photoStackModelState.requestedInitialLoad = false;
    photoStackModelState.error = null;
  }
  if (!cameraTransformState.isBuilding) {
    cameraTransformState.current = null;
    cameraTransformState.requestedInitialLoad = false;
    cameraTransformState.error = null;
  }
}

async function handleRefresh(_options = {}) {
  const options = normalizeRefreshOptions(_options);
  if (_refreshPromise) {
    await _refreshPromise;
    if (options.reloadImportData === true && !importState.loading) {
      importState.loading = true;
      importState.loaded = false;
      importState.loadingMessage = "Loading image inbox";
      if (currentMode === "imageProcessing" && currentSubtab === "associate") {
        renderWorkspace();
      }
      await loadImportData();
      if (currentMode === "imageProcessing" && currentSubtab === "associate") {
        renderWorkspace();
      }
    }
    return;
  }
  _refreshPromise = runRefresh(options);
  try {
    await _refreshPromise;
  } finally {
    _refreshPromise = null;
  }
}

async function runRefresh(options = {}) {
  if (typeof initializeData !== "function") return;
  const shouldReloadImportData =
    options.reloadImportData === true ||
    (
      currentMode === "imageProcessing" &&
      currentSubtab === "associate" &&
      options.ensureAssets !== false
    );

  setRefreshButtonBusy(true);
  if (shouldReloadImportData) {
    importState.loading = true;
    importState.loaded = false;
    importState.loadingMessage = "Loading image inbox";
    renderWorkspace();
  }

  renderDataSourceBadge();
  try {
    const source = await initializeData();
    _dataSource = source;
    if (typeof loadServerConfig === "function") {
      await loadServerConfig();
    }
    // Re-initialize step metadata from API-returned alias/bundle
    (data.steps || []).forEach((step) => {
      stepMetadata[step.step_id || step.file_name] = {
        alias: step.alias || "",
        bundle: step.bundle || "",
        deleted: false,
      };
    });
    resetRefreshableUiCaches();
    if (!shouldReloadImportData) {
      syncLoadedImportStateFromAppData();
    }
    syncSampleStepCacheFromData();
    renderBundleOptions();
    renderSummaryRail();
    renderWorkspace();
    if (shouldReloadImportData) {
      await loadImportData();
      renderWorkspace();
    }
    await rerenderOpenRecordDrawerAfterRefresh();
    rerenderLinkedSampleDrawerAfterRefresh();
    if (currentMode === "profiles") {
      renderModelsView();
    }
    renderDataSourceBadge();
    if (options.userInitiated) {
      showImportToast("Data refreshed", "ok");
    }
  } catch (err) {
    _dataSource = "static";
    renderDataSourceBadge();
    console.error("[app] Refresh failed:", err);
    if (options.userInitiated) {
      showImportToast(err.message ? `Refresh failed: ${err.message}` : "Refresh failed", "error");
    }
  } finally {
    setRefreshButtonBusy(false);
  }
}

async function rerenderOpenRecordDrawerAfterRefresh() {
  if (!recordDrawer?.classList.contains("is-open")) return;

  if (selectedRecord.kind === "step" && selectedRecord.id) {
    renderStepDetailDrawer(selectedRecord.id, { preserveReturn: true });
    return;
  }

  if (selectedRecord.kind !== "sample" || !selectedRecord.id) return;

  if (_sampleDrawerMode === "create") {
    if (selectedRecord.id === "__bulk__") {
      try {
        const [stepsResp, bundlesResp, idResp] = await Promise.all([
          fetchSteps(),
          fetchBundles(),
          fetchNextSampleId(),
        ]);
        _sampleCreateSteps = stepsResp || _sampleCreateSteps || [];
        _bulkCreateBundles = bundlesResp || _bulkCreateBundles || [];
        _bulkCreateNextId = idResp?.next_id || _bulkCreateNextId || "...";
      } catch (err) {
        console.warn("[refresh] Failed to refresh bulk-create data:", err);
      }
      _renderBulkSampleCreateDrawer();
      return;
    }
  }

  const exp = data.samples.find((item) => item.sample_id === selectedRecord.id);
  if (!exp) return;

  if (_sampleDrawerMode === "edit") {
    _renderSampleDrawerEdit(exp, { expanded: _sampleInspectExpanded });
    return;
  }

  renderSidebarForSample(exp, { expanded: _sampleInspectExpanded });
}

function rerenderLinkedSampleDrawerAfterRefresh() {
  if (!linkedSampleDrawer?.classList.contains("is-open") || !_linkedSampleDrawerState.sampleId) return;
  const exp = data.samples.find((item) => item.sample_id === _linkedSampleDrawerState.sampleId);
  if (!exp) {
    closeLinkedSampleDrawer({ restoreFocus: false });
    return;
  }
  renderLinkedSampleDrawer(exp);
  syncLinkedSampleDrawerPosition();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function formatRestorePointTimestamp(value = "") {
  if (!value) return "Unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function showSqliteRestorePointRecoveryDialog(status = {}) {
  const existing = document.getElementById("sqliteRecoveryOverlay");
  if (existing) existing.remove();
  const overlay = document.createElement("div");
  overlay.id = "sqliteRecoveryOverlay";
  overlay.className = "info-dialog-overlay sqlite-recovery-overlay";
  const points = status.restore_points || [];
  const requiredConfirmation = status.required_confirmation || "Restore the selected SQLite restore point";
  const startupError = status.startup_error || status.startup_status?.error || "The SQLite database did not pass startup checks.";
  const selectedPath = points[0]?.sqlite_path || "";
  let state = {
    selectedPath,
    confirmation: "",
    working: false,
    error: "",
    result: null,
  };

  const closeAfterSuccess = async () => {
    overlay.remove();
    await handleRefresh();
    loadServerConfig();
  };

  const render = () => {
    const isRestoreReady = () => !!state.selectedPath && state.confirmation.trim().toLowerCase() === requiredConfirmation.toLowerCase() && !state.working && !state.result;
    const canRestore = isRestoreReady();
    overlay.innerHTML = `
      <div class="info-dialog sqlite-recovery-dialog" role="dialog" aria-modal="true" aria-labeledby="sqliteRecoveryTitle">
        <div class="info-dialog-header">
          <h3 id="sqliteRecoveryTitle">SQLite Recovery Required</h3>
        </div>
        <div class="info-dialog-body sqlite-recovery-body">
          <section class="drawer-module sqlite-recovery-alert">
            <div class="drawer-module-cap">Startup Check Failed</div>
            <div class="drawer-module-body">
              <p>Prisma could not open the calibration SQLite database safely.</p>
              <p class="small-copy">${escapeHtml(startupError)}</p>
            </div>
          </section>
          <section class="drawer-module">
            <div class="drawer-module-cap">Available Restore Points</div>
            <div class="drawer-module-body">
              ${points.length ? `
                <div class="sqlite-restore-point-list">
                  ${points.map((point, index) => {
                    const path = point.sqlite_path || "";
                    const checked = path === state.selectedPath || (!state.selectedPath && index === 0);
                    return `
                      <label class="sqlite-restore-point-row ${checked ? "is-active" : ""}">
                        <input type="radio" name="sqliteRestorePoint" value="${escapeHtml(path)}" ${checked ? "checked" : ""} ${state.working || state.result ? "disabled" : ""}>
                        <span>
                          <strong>${escapeHtml(formatRestorePointTimestamp(point.created_at))}</strong>
                          <small>${escapeHtml(formatBytes(point.sqlite_size_bytes) || "SQLite restore point")}</small>
                          <code>${escapeHtml(path)}</code>
                        </span>
                      </label>
                    `;
                  }).join("")}
                </div>
              ` : `
                <p>No automatic SQLite restore points are available.</p>
                <p class="small-copy">Close Prisma and restore from a normal Backup / Restore package or a manual database copy.</p>
              `}
            </div>
          </section>
          ${points.length && !state.result ? `
            <section class="drawer-module">
              <div class="drawer-module-cap">Confirm Restore</div>
              <div class="drawer-module-body">
                <p>Restoring replaces only the SQLite database. The current database file is preserved first for inspection.</p>
                <label class="sqlite-recovery-confirm">
                  <span>Type this phrase to continue:</span>
                  <code>${escapeHtml(requiredConfirmation)}</code>
                  <input type="text" id="sqliteRecoveryConfirmation" value="${escapeHtml(state.confirmation)}" ${state.working ? "disabled" : ""}>
                </label>
              </div>
            </section>
          ` : ""}
          ${state.working ? `<div class="backup-restore-message">Restoring SQLite restore point...</div>` : ""}
          ${state.error ? `<div class="backup-restore-message is-error">${escapeHtml(state.error)}</div>` : ""}
          ${state.result ? `
            <section class="drawer-module sqlite-recovery-success">
              <div class="drawer-module-cap">Restore Complete</div>
              <div class="drawer-module-body">
                <p>SQLite was restored and Prisma reloaded the database.</p>
                <div class="backup-result-card">
                  <div><span>Restored From</span><strong>${escapeHtml(state.result.restore_point?.sqlite_path || state.selectedPath)}</strong></div>
                  <div><span>Preserved Previous DB</span><strong>${escapeHtml(state.result.preserved_current_sqlite?.recovery_dir || "")}</strong></div>
                </div>
              </div>
            </section>
          ` : ""}
        </div>
        <div class="info-dialog-footer">
          ${state.result ? `
            <button class="primary-button small" type="button" id="sqliteRecoveryContinue">Continue</button>
          ` : `
            <button class="primary-button small" type="button" id="sqliteRecoveryRestore" ${canRestore ? "" : "disabled"}>${state.working ? "Restoring..." : "Restore SQLite"}</button>
          `}
        </div>
      </div>
    `;
    overlay.querySelectorAll("input[name='sqliteRestorePoint']").forEach((input) => {
      input.addEventListener("change", () => {
        state.selectedPath = input.value || "";
        render();
      });
    });
    overlay.querySelector("#sqliteRecoveryConfirmation")?.addEventListener("input", (event) => {
      state.confirmation = event.target.value || "";
      const restoreButton = overlay.querySelector("#sqliteRecoveryRestore");
      if (restoreButton) {
        restoreButton.disabled = !isRestoreReady();
      }
    });
    overlay.querySelector("#sqliteRecoveryRestore")?.addEventListener("click", async () => {
      if (!isRestoreReady()) return;
      state.working = true;
      state.error = "";
      render();
      try {
        const response = await restoreSqliteRestorePoint(state.selectedPath, state.confirmation);
        state.result = response.result || {};
      } catch (err) {
        state.error = err.message || "Could not restore SQLite restore point.";
      } finally {
        state.working = false;
        render();
      }
    });
    overlay.querySelector("#sqliteRecoveryContinue")?.addEventListener("click", closeAfterSuccess);
  };

  document.body.appendChild(overlay);
  render();
}

async function bootstrapApplication() {
  try {
    if (typeof fetchSqliteRestorePointStatus === "function") {
      const status = await fetchSqliteRestorePointStatus();
      if (status?.recovery_required) {
        renderDataSourceBadge();
        showSqliteRestorePointRecoveryDialog(status);
        return;
      }
    }
  } catch (err) {
    console.warn("[startup] Could not check SQLite restore-point status:", err);
  }
  await handleRefresh();
}

// ── Wire refresh button ──────────────────────────────────────────────────────

const refreshDataBtn = document.getElementById("refreshDataBtn");
if (refreshDataBtn) {
  refreshDataBtn.addEventListener("click", () => handleRefresh({ userInitiated: true }));
}

const maintenanceBtn = document.getElementById("maintenanceBtn");
if (maintenanceBtn) {
  maintenanceBtn.addEventListener("click", showMaintenanceDialog);
}

const backupRestoreBtn = document.getElementById("backupRestoreBtn");
if (backupRestoreBtn) {
  backupRestoreBtn.addEventListener("click", showBackupRestoreDialog);
}

const publishModelsBtn = document.getElementById("publishModelsBtn");
if (publishModelsBtn) {
  publishModelsBtn.addEventListener("click", showModelPublicationDialog);
}

window.addEventListener("resize", () => {
  syncModeTabRowWidth();
  if (recordDrawer?.classList.contains("is-open")) {
    syncRecordDrawerPosition();
    syncLinkedSampleDrawerPosition();
    updateLinkedSampleTriggers(detailSidebar);
  }
});

// ── Initial render (empty shell until API data arrives) ──────────────────────

renderBundleOptions();
renderSummaryRail();
renderWorkspace();
renderDataSourceBadge();

// ── Async API bootstrap ──────────────────────────────────────────────────────

if (typeof initializeData === "function") {
  bootstrapApplication();
}

closeRecordDrawer.addEventListener("click", () => {
  clearSelectionAndDrawer();
});

closeLinkedSampleDrawerBtn?.addEventListener("click", () => {
  closeLinkedSampleDrawer();
});

document.getElementById("closeStepBuilderDrawerBtn")?.addEventListener("click", () => {
  closeStepBuilderDrawer();
});

document.getElementById("closeBundleMgmtDrawer")?.addEventListener("click", () => {
  closeBundleMgmtDrawer();
});

document.addEventListener("click", handleOutsideDrawerDismiss);


// ── Manual Processing Overlay ─────────────────────────────────────────────

const _manualProc = {
  mode: null,           // 'single' | 'batch'
  queue: [],            // array of sample objects
  currentIndex: 0,
  corners: [],          // [{x, y}, ...] up to 4 — in image coordinates
  completed: new Set(), // sample IDs that finished successfully in this session
  previewScale: 1,      // ratio: canvas pixels / image pixels
  sourceImage: null,     // HTMLImageElement
  processing: false,
  currentJobId: "",
  cancelling: false,
  context: null,
};

const CORNER_LABELS = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"];
const CORNER_COLORS = ["#e53935", "#fb8c00", "#43a047", "#1e88e5"];

function openManualProcessing(sampleIds, options = {}) {
  const exps = sampleIds.map((id) =>
    data.samples.find((e) => e.sample_id === id)
  ).filter(Boolean);
  if (exps.length === 0) return;

  _manualProc.mode = exps.length === 1 ? "single" : "batch";
  _manualProc.queue = exps;
  _manualProc.currentIndex = 0;
  _manualProc.completed = new Set();
  _manualProc.context = options?.context ? { ...options } : null;
  _resetManualCorners();

  const overlay = document.getElementById("manualProcOverlay");
  if (overlay) {
    overlay.classList.toggle("is-workflow-modal", _manualProc.context?.context === "reextract-candidate");
    overlay.classList.add("is-open");
    overlay.setAttribute("aria-hidden", "false");
  }
  _loadManualProcSample();
}

function closeManualProcessing() {
  if (_manualProc.currentJobId || _manualProc.processing) return;
  const activeSample = _currentManualSample();
  if (_manualProc.context?.context !== "reextract-candidate" && activeSample?.sample_id) {
    apiDelete(`/process/manual/review/${encodeURIComponent(activeSample.sample_id)}`).catch(() => {});
  }
  const overlay = document.getElementById("manualProcOverlay");
  if (overlay) {
    overlay.classList.remove("is-open");
    overlay.classList.remove("is-workflow-modal");
    overlay.setAttribute("aria-hidden", "true");
  }

  // Batch mode: completed samples stay as "processed" (they were processed server-side).
  // Incomplete samples remain in their current state (flagged/failed).
  // The processing dashboard will re-render and pick them up correctly.
  _manualProc.mode = null;
  _manualProc.queue = [];
  _manualProc.sourceImage = null;
  _manualProc.corners = [];
  _manualProc.currentJobId = "";
  _manualProc.cancelling = false;

  const context = _manualProc.context;
  _manualProc.context = null;
  if (context?.context === "reextract-candidate") {
    if (!context.completed) context.onCandidateComplete?.();
    return;
  }

  // Refresh data and re-render to reflect any server-side status changes
  handleRefresh().then(() => renderProcessingDashboard());
}

function _resetManualCorners() {
  _manualProc.corners = [];
  _manualProc.processing = false;
  _manualProc.currentJobId = "";
  _manualProc.cancelling = false;
}

function _currentManualSample() {
  return _manualProc.queue[_manualProc.currentIndex] || null;
}

function _loadManualProcSample() {
  const exp = _currentManualSample();
  if (!exp) return;

  _resetManualCorners();
  _updateManualProcUI();

  // Load source image onto the canvas
  const imgName = exp._assigned_image;
  if (!imgName) {
    _setManualInstructions("No source image assigned to this sample.");
    return;
  }

  const img = new Image();
  img.crossOrigin = "anonymous";
  img.onload = () => {
    _manualProc.sourceImage = img;
    _drawManualCanvas();
  };
  img.onerror = () => {
    _setManualInstructions("Failed to load source image.");
  };
  // Use full-size preview for accurate corner placement
  img.src = previewUrl(imgName, { size: "full" });
}

function _drawManualCanvas() {
  const canvas = document.getElementById("manualProcCanvas");
  const area = canvas?.parentElement;
  if (!canvas || !_manualProc.sourceImage) return;

  const img = _manualProc.sourceImage;
  const ctx = canvas.getContext("2d");

  // Size canvas to fit the available area while preserving aspect ratio
  const areaW = area.clientWidth;
  const areaH = area.clientHeight;
  const scale = Math.min(areaW / img.naturalWidth, areaH / img.naturalHeight, 1);

  canvas.width = Math.round(img.naturalWidth * scale);
  canvas.height = Math.round(img.naturalHeight * scale);
  _manualProc.previewScale = scale;

  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  // Draw placed corners
  for (let i = 0; i < _manualProc.corners.length; i++) {
    const c = _manualProc.corners[i];
    const cx = c.x * scale;
    const cy = c.y * scale;

    ctx.beginPath();
    ctx.arc(cx, cy, 6, 0, Math.PI * 2);
    ctx.fillStyle = CORNER_COLORS[i];
    ctx.fill();
    ctx.strokeStyle = "#fff";
    ctx.lineWidth = 2;
    ctx.stroke();

    // Label — positioned diagonally outward from quad center
    ctx.font = "bold 11px sans-serif";
    ctx.fillStyle = "#fff";
    ctx.strokeStyle = "rgba(0,0,0,0.6)";
    ctx.lineWidth = 3;
    const label = CORNER_LABELS[i];
    const tw = ctx.measureText(label).width;
    // Offsets: TL→upper-left, TR→upper-right, BR→lower-right, BL→lower-left
    const labelOffsets = [
      { x: -tw - 8, y: -8 },   // TL: text lower-right anchored at 10:30
      { x: 8,       y: -8 },   // TR: text lower-left anchored at 1:30
      { x: 8,       y: 16 },   // BR: text upper-left anchored at 4:30
      { x: -tw - 8, y: 16 },   // BL: text upper-right anchored at 7:30
    ];
    const off = labelOffsets[i] || { x: 10, y: 4 };
    ctx.strokeText(label, cx + off.x, cy + off.y);
    ctx.fillText(label, cx + off.x, cy + off.y);
  }

  // Draw lines connecting corners (dark outline + bright line for visibility on any background)
  if (_manualProc.corners.length >= 2) {
    function _tracePath() {
      ctx.beginPath();
      for (let i = 0; i < _manualProc.corners.length; i++) {
        const c = _manualProc.corners[i];
        const px = c.x * scale;
        const py = c.y * scale;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      }
      if (_manualProc.corners.length === 4) {
        const first = _manualProc.corners[0];
        ctx.lineTo(first.x * scale, first.y * scale);
      }
      ctx.stroke();
    }
    // Dark outline
    ctx.strokeStyle = "rgba(0,0,0,0.6)";
    ctx.lineWidth = 3;
    ctx.setLineDash([]);
    _tracePath();
    // Bright dashed line on top
    ctx.strokeStyle = "#00e5ff";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    _tracePath();
    ctx.setLineDash([]);
  }
}

function _updateManualProcUI() {
  const exp = _currentManualSample();
  const title = document.getElementById("manualProcTitle");
  const subtitle = document.getElementById("manualProcSubtitle");
  const progress = document.getElementById("manualProcProgress");
  const cornerList = document.getElementById("manualProcCornerList");
  const extractBtn = document.getElementById("manualProcExtract");
  const cancelBtn = document.getElementById("manualProcCancel");
  const resetBtn = document.getElementById("manualProcReset");
  const resultBlock = document.getElementById("manualProcResultBlock");

  if (title && exp) {
    const fil = filamentMeta(exp.variable_filament_id);
    const filName = fil ? fil.display_name || fil.color_name : exp.variable_filament_id;
    title.textContent = _manualProc.context?.context === "reextract-candidate"
      ? `Manual Candidate — ${exp.sample_id}`
      : `Manual Processing — ${exp.sample_id}`;
    if (subtitle) subtitle.textContent = filName;
  }

  if (progress) {
    if (_manualProc.mode === "batch") {
      const done = _manualProc.completed.size;
      const total = _manualProc.queue.length;
      progress.textContent = `${_manualProc.currentIndex + 1} of ${total} (${done} done)`;
    } else {
      progress.textContent = "";
    }
  }

  // Corner list
  if (cornerList) {
    cornerList.innerHTML = CORNER_LABELS.map((label, i) => {
      const isSet = i < _manualProc.corners.length;
      const isActive = i === _manualProc.corners.length && i < 4;
      const cls = isSet ? " is-set" : isActive ? " is-active" : "";
      const coords = isSet
        ? `(${Math.round(_manualProc.corners[i].x)}, ${Math.round(_manualProc.corners[i].y)})`
        : isActive ? "click to place" : "—";
      return `<div class="manual-proc-corner-item${cls}">
        <span class="manual-proc-corner-dot" style="background:${CORNER_COLORS[i]}"></span>
        <span>${label}</span>
        <span class="mono small-copy" style="margin-left:auto">${coords}</span>
      </div>`;
    }).join("");
  }

  // Extract button: enable only when all 4 corners placed
  if (extractBtn) {
    extractBtn.disabled = _manualProc.corners.length < 4 || _manualProc.processing;
    extractBtn.textContent = _manualProc.processing
      ? "Processing\u2026"
      : (_manualProc.context?.context === "reextract-candidate" ? "Extract Image" : "Extract & Process");
  }

  if (cancelBtn) {
    const showCancel = _manualProc.processing && !!_manualProc.currentJobId;
    cancelBtn.style.display = showCancel ? "" : "none";
    cancelBtn.disabled = _manualProc.cancelling;
    cancelBtn.textContent = _manualProc.cancelling ? "Cancelling..." : "Cancel";
  }

  if (resetBtn) {
    resetBtn.disabled = _manualProc.processing;
  }

  if (mpCloseBtn) {
    mpCloseBtn.disabled = _manualProc.processing || Boolean(_manualProc.currentJobId);
  }

  // Hide result block when not showing results
  if (resultBlock) resultBlock.style.display = "none";

  // Update instructions
  if (_manualProc.corners.length < 4) {
    _setManualInstructions(`Click corner ${_manualProc.corners.length + 1} of 4: ${CORNER_LABELS[_manualProc.corners.length]}`);
  } else {
    _setManualInstructions(_manualProc.context?.context === "reextract-candidate"
      ? 'All corners placed. Click "Extract Image" or adjust corners.'
      : 'All corners placed. Click "Extract & Process" or adjust corners.');
  }
}

function _setManualInstructions(text) {
  const el = document.getElementById("manualProcInstructions");
  if (el) el.textContent = text;
}

function _showManualResult(success, message, stripUrl) {
  const resultBlock = document.getElementById("manualProcResultBlock");
  const resultDiv = document.getElementById("manualProcResult");
  if (!resultBlock || !resultDiv) return;

  resultBlock.style.display = "";
  let html = `<span class="status-pill ${success ? "processed" : "failed"}">${success ? "Success" : "Failed"}</span>`;
  html += `<p class="small-copy" style="margin-top:4px">${message}</p>`;
  if (stripUrl) {
    html += `<div class="manual-proc-result-strip"><img src="${stripUrl}" alt="extracted strip"></div>`;
  }
  resultDiv.innerHTML = html;
}

async function _handleManualExtract() {
  const exp = _currentManualSample();
  if (!exp || _manualProc.corners.length < 4) return;

  _manualProc.processing = true;
  _updateManualProcUI();
  let candidateContext = null;

  try {
    const corners = _manualProc.corners.map((c) => ({ x: c.x, y: c.y }));
    const orientation = exp._orientation_rots != null ? exp._orientation_rots : 0;
    candidateContext = _manualProc.context?.context === "reextract-candidate" ? _manualProc.context : null;

    if (candidateContext) {
      const started = await startManualReextractCandidateJob(candidateContext.candidateSetId, exp.sample_id, {
        corners,
        orientation,
        preview_width: _manualProc.sourceImage.naturalWidth,
        preview_height: _manualProc.sourceImage.naturalHeight,
      });
      const jobId = String(started?.job_id || "");
      if (!jobId) throw new Error("Manual candidate generation did not return a job id.");
      _manualProc.currentJobId = jobId;
      _updateManualProcUI();
      const finalJob = await pollJobUntilTerminal({
        jobId,
        fetchStatus: () => fetchReextractJobStatus(jobId),
        isTerminal: (job) => ["succeeded", "failed", "cancelled"].includes(String(job.status || "")),
        shouldContinue: () => (
          _manualProc.currentJobId === jobId
          && document.getElementById("manualProcOverlay")?.classList.contains("is-open")
        ),
        intervalMs: 500,
        onStatus: (job) => {
          const progress = job.progress || {};
          const statusText = progress.action_label || job.message || "Generating manual candidate";
          const percent = Number(progress.percent || 0);
          const progressEl = document.getElementById("manualProcProgress");
          if (progressEl) progressEl.textContent = `${statusText} · ${Math.max(0, Math.min(100, percent)).toFixed(1)}%`;
        },
        onTransientError: () => {
          const progressEl = document.getElementById("manualProcProgress");
          if (progressEl) progressEl.textContent = "Connection interrupted; retrying manual extraction status...";
        },
      });
      if (!finalJob) return;
      _manualProc.currentJobId = "";
      _manualProc.processing = false;
      _manualProc.cancelling = false;
      const payload = finalJob?.result || {};
      const candidate = payload?.candidate || null;
      if (finalJob?.status !== "succeeded") {
        if (candidate) {
          await candidateContext.onCandidateComplete?.(candidate);
        }
        throw new Error(candidate?.error || finalJob?.error?.message || finalJob?.message || "Manual candidate generation failed.");
      }
      if (candidate?.status === "failed") {
        await candidateContext.onCandidateComplete?.(candidate);
        throw new Error(candidate.error || "Manual candidate generation failed.");
      }
      _manualProc.completed.add(exp.sample_id);
      candidateContext.completed = true;
      const stripUrl = candidate?.artifacts?.strip
        ? reextractCandidateArtifactUrl(candidateContext.candidateSetId, exp.sample_id, "strip")
        : "";
      _showManualResult(true, "Manual candidate generated.", stripUrl);
      await candidateContext.onCandidateComplete?.(candidate);
      return;
    }

    const result = await apiPost("/process/manual/extract", {
      sample_id: exp.sample_id,
      corners: corners,
      orientation: orientation,
      preview_width: _manualProc.sourceImage.naturalWidth,
      preview_height: _manualProc.sourceImage.naturalHeight,
      commit: false,
    });

    _manualProc.processing = false;
    _updateManualProcUI();
    const nSwatches = result.measurements?.swatches?.length || 0;
    _showManualResult(true, `Extracted ${nSwatches} swatches.`,
      `/api/process/manual/review/${encodeURIComponent(exp.sample_id)}/strip?t=${Date.now()}`);
  } catch (err) {
    _manualProc.processing = false;
    _manualProc.currentJobId = "";
    _manualProc.cancelling = false;
    _updateManualProcUI();
    _showManualResult(false, err.message || "Extraction failed.");
  }
}

async function _handleManualAccept() {
  const exp = _currentManualSample();
  if (_manualProc.context?.context === "reextract-candidate") {
    closeManualProcessing();
    return;
  }
  if (exp) {
    // Commit: re-run extract with commit=true to finalize as "processed", then clear flag
    try {
      const corners = _manualProc.corners.map((c) => ({ x: c.x, y: c.y }));
      const orientation = exp._orientation_rots != null ? exp._orientation_rots : 0;
      await apiPost("/process/manual/extract", {
        sample_id: exp.sample_id,
        corners: corners,
        orientation: orientation,
        preview_width: _manualProc.sourceImage.naturalWidth,
        preview_height: _manualProc.sourceImage.naturalHeight,
        commit: true,
      });
    } catch (err) {
      showImportToast(err.message || "Failed to finalize", "error");
      return;
    }
    _manualProc.completed.add(exp.sample_id);
  }

  // Move to next sample in batch, or close if single/done
  if (_manualProc.mode === "batch" && _manualProc.currentIndex < _manualProc.queue.length - 1) {
    _manualProc.currentIndex++;
    _resetManualCorners();
    _loadManualProcSample();
  } else {
    closeManualProcessing();
  }
}

function _handleManualRetry() {
  _resetManualCorners();
  _drawManualCanvas();
  _updateManualProcUI();
  document.getElementById("manualProcResultBlock").style.display = "none";
}

// ── Manual Processing: canvas click handler ───────────────────────────────

const manualProcCanvas = document.getElementById("manualProcCanvas");
if (manualProcCanvas) {
  let _dragIndex = -1;
  let _didDrag = false;
  const DRAG_HIT_RADIUS = 10; // canvas pixels

  function _canvasToImg(e) {
    const rect = manualProcCanvas.getBoundingClientRect();
    const scale = _manualProc.previewScale || 1;
    return {
      x: (e.clientX - rect.left) / scale,
      y: (e.clientY - rect.top) / scale,
    };
  }

  function _hitTestCorner(canvasX, canvasY) {
    const scale = _manualProc.previewScale || 1;
    for (let i = 0; i < _manualProc.corners.length; i++) {
      const cx = _manualProc.corners[i].x * scale;
      const cy = _manualProc.corners[i].y * scale;
      const dx = canvasX - cx;
      const dy = canvasY - cy;
      if (Math.sqrt(dx * dx + dy * dy) <= DRAG_HIT_RADIUS) return i;
    }
    return -1;
  }

  manualProcCanvas.addEventListener("mousedown", (e) => {
    if (_manualProc.processing) return;
    const rect = manualProcCanvas.getBoundingClientRect();
    const canvasX = e.clientX - rect.left;
    const canvasY = e.clientY - rect.top;

    const hit = _hitTestCorner(canvasX, canvasY);
    if (hit >= 0) {
      _dragIndex = hit;
      _didDrag = false;
      manualProcCanvas.style.cursor = "grabbing";
      e.preventDefault();
    }
  });

  manualProcCanvas.addEventListener("mousemove", (e) => {
    if (_dragIndex >= 0) {
      _didDrag = true;
      const pt = _canvasToImg(e);
      _manualProc.corners[_dragIndex] = { x: pt.x, y: pt.y };
      _drawManualCanvas();
      _updateManualProcUI();
      e.preventDefault();
    } else {
      // Show grab cursor when hovering over a placed corner
      const rect = manualProcCanvas.getBoundingClientRect();
      const hit = _hitTestCorner(e.clientX - rect.left, e.clientY - rect.top);
      manualProcCanvas.style.cursor = hit >= 0 ? "grab" : "crosshair";
    }
  });

  manualProcCanvas.addEventListener("mouseup", (e) => {
    if (_dragIndex >= 0) {
      if (_didDrag) {
        const pt = _canvasToImg(e);
        _manualProc.corners[_dragIndex] = { x: pt.x, y: pt.y };
        _drawManualCanvas();
        _updateManualProcUI();
      }
      _dragIndex = -1;
      _didDrag = false;
      manualProcCanvas.style.cursor = "crosshair";
      return;
    }

    // Not dragging — place a new corner if we have room
    if (_manualProc.corners.length >= 4) return;
    const pt = _canvasToImg(e);
    _manualProc.corners.push({ x: pt.x, y: pt.y });
    _drawManualCanvas();
    _updateManualProcUI();
  });

  manualProcCanvas.addEventListener("mouseleave", () => {
    if (_dragIndex >= 0) {
      _dragIndex = -1;
      _didDrag = false;
      manualProcCanvas.style.cursor = "crosshair";
    }
  });
}

// ── Manual Processing: sidebar button handlers ────────────────────────────

const mpResetBtn = document.getElementById("manualProcReset");
const mpExtractBtn = document.getElementById("manualProcExtract");
const mpCancelBtn = document.getElementById("manualProcCancel");
const mpAcceptBtn = document.getElementById("manualProcAccept");
const mpRetryBtn = document.getElementById("manualProcRetry");
const mpCloseBtn = document.getElementById("closeManualProc");
const mpOverlay = document.getElementById("manualProcOverlay");

if (mpResetBtn) {
  mpResetBtn.addEventListener("click", () => {
    _resetManualCorners();
    _drawManualCanvas();
    _updateManualProcUI();
    document.getElementById("manualProcResultBlock").style.display = "none";
  });
}

if (mpExtractBtn) {
  mpExtractBtn.addEventListener("click", () => _handleManualExtract());
}

if (mpCancelBtn) {
  mpCancelBtn.addEventListener("click", async () => {
    if (!_manualProc.currentJobId) return;
    const cancellationJobId = _manualProc.currentJobId;
    _manualProc.cancelling = true;
    _updateManualProcUI();
    try {
      const response = await cancelReextractJob(cancellationJobId);
      if (_manualProc.currentJobId !== cancellationJobId) return;
      assertPolledJobIdentity(response, cancellationJobId);
    } catch (err) {
      if (_manualProc.currentJobId !== cancellationJobId) return;
      _manualProc.cancelling = false;
      _updateManualProcUI();
      _showManualResult(false, err.message || "Cancel request failed.");
    }
  });
}

if (mpAcceptBtn) {
  mpAcceptBtn.addEventListener("click", () => _handleManualAccept());
}

if (mpRetryBtn) {
  mpRetryBtn.addEventListener("click", () => _handleManualRetry());
}

if (mpCloseBtn) {
  mpCloseBtn.addEventListener("click", () => closeManualProcessing());
}

if (mpOverlay) {
  mpOverlay.addEventListener("click", (e) => {
    if (e.target === mpOverlay) closeManualProcessing();
  });
}
