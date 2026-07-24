/** Initialize owned Calibration state after all feature commands are installed. */
export function initializeApplicationState(app) {
  app.state.session.data = {
    summary: {},
    filaments: [],
    samples: [],
    steps: [],
    processed_samples: [],
    images: [],
    image_overrides: {},
    model_status: {},
  };
  app.state.session._dataSource = "api";
  app.state.session._refreshPromise = null;
  app.state.navigation.currentMode = "logbook";
  app.state.navigation.currentSubtab = "";
  app.state.logbook.sortState = { key: "sample_id", direction: "asc" };
  app.state.modeling.profilesState = {
    selectedFilamentId: null,
    profileCache: {}, // filament_id -> profile data from API
    curveCache: {}, // filament_id -> dense curve data from /curve endpoint
    swatchCache: {}, // filament_id -> swatch error data from API
    errorCache: {}, // filament_id -> per-swatch dE bar data from /errors endpoint
    loadingProfile: false,
    crosscalSortKey: "dE",
    crosscalSortDir: "desc",
    detailSection: "chart", // "chart" | "swatches" | "data"
  };
  app.state.modeling.fitModelsWorkflowLaunchBusy = false;
  app.state.modeling.modelingState = {
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
  app.state.modeling.geometryDetailReturnSampleContext = null;
  app.state.modeling.modelingDetailSettings =
    app.commands.loadModelingDetailSettings();
  app.state.modeling.profileFitJobState = {
    running: false,
    jobId: null,
    status: null,
    lastResult: null,
    error: null,
  };
  app.state.modeling.modelFittingState = {
    selectedFilamentId: null,
    selectedSampleId: null,
    predictionCache: {},
    isFittingAll: false,
    renderSeq: 0,
  };
  app.state.modeling.photoStackModelState = {
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
  app.state.modeling.cameraTransformState = {
    isBuilding: false,
    jobId: null,
    status: null,
    current: null,
    requestedInitialLoad: false,
    error: null,
  };
  app.state.logbook.logbookFilter = "all";
  app.state.logbook.selectedRecord = { kind: null, id: null };
  app.state.session._serverConfig = null;
  app.state.images.importState = {
    images: [], // from /api/images
    blanks: [], // from /api/blanks
    selectedImage: null, // filename string
    selectedBlank: null, // blank filename string
    selectedSample: null, // sample_id string
    assignedCount: 0, // session counter
    loading: false,
    loaded: false,
    loadingMessage: "",
    imageAssignments: {}, // filename -> sample_id (derived from samples)
    hideReady: false, // toggle to hide fully assigned samples
  };
  app.state.geometries.stepBuilderState = {
    values: ["0.20", "0.28", "0.36", "0.44", "0.52", "0.60", "0.68", "0.76"],
    fixedLayers: [],
    layerRoles: [],
    nextLayerRoleId: 1,
    alias: "",
    bundle: "",
  };
  app.state.geometries.stepEditorState = {
    stepId: null,
    isEditing: false,
    draftAlias: "",
    draftBundle: "",
    confirmDelete: false,
    deleteMessage: "",
    deleteMessageKind: "",
  };
  app.state.processing.processingState = {
    batchRunning: false,
    batchProgress: null,
    singleRunningSampleIds: new Set(),
  };
  app.state.operations.maintenanceState = {
    operations: null,
    loading: false,
    error: "",
    loadPromise: null,
  };
  app.state.session.maintenanceCacheBust = {
    version: 0,
    previews: new Map(),
    blankPreviews: new Map(),
    sampleThumbnails: new Map(),
    allPreviews: 0,
    allSampleThumbnails: 0,
  };
  app.state.filaments._filamentDrawerMode = null;
  app.state.filaments._filamentDrawerData = null;
  app.state.logbook._sampleInspectExpanded =
    app.commands.readSampleInspectExpandedPreference();
  app.state.logbook._linkedSampleDrawerState = {
    sampleId: null,
    returnFocusEl: null,
  };
  app.state.logbook.stepMetadata = Object.fromEntries(
    (app.state.session.data.steps || []).map((step) => [
      step.step_id || step.file_name,
      {
        alias: step.alias || "",
        bundle: step.bundle || "",
        deleted: false,
      },
    ]),
  );
  app.state.geometries.activeGeometryExportDialogCleanup = null;
  app.state.geometries._bundleDrawerState = {
    bundles: [],
    selectedBundleName: null,
    showNewInput: false,
    renamingBundleName: null,
  };
  app.state.logbook._sampleDrawerMode = null;
  app.state.logbook._sampleCreateSteps = [];
  app.state.logbook._bulkCreateNextId = "...";
  app.state.logbook._bulkCreateBundles = [];
  app.state.processing._measurementHydrationInFlight = new Set();
  app.state.processing._manualProc = {
    mode: null, // 'single' | 'batch'
    queue: [], // array of sample objects
    currentIndex: 0,
    corners: [], // [{x, y}, ...] up to 4 — in image coordinates
    completed: new Set(), // sample IDs that finished successfully in this session
    previewScale: 1, // ratio: canvas pixels / image pixels
    sourceImage: null, // HTMLImageElement
    processing: false,
    currentJobId: "",
    cancelling: false,
    context: null,
  };
  return app.state;
}
