export function installApiLoader(api) {
  function buildFilamentLookup(filaments) {
    const map = {};
    for (const f of filaments) {
      map[f.filament_id] = f;
    }
    return map;
  }

  function transformSampleToData(sample, filamentMap) {
    // The API returns a Sample object; transforms it into the shape app.js expects.
    const varFilId = sample.filaments?.variable || "";
    const fixedFilIds = sample.filaments?.fixed || [];
    const varFil = filamentMap[varFilId] || {};
    const stripDef = sample.strip_definition || {};

    const photos = sample.photos || [];
    const photoCount = photos.length;
    const photoName = photos[0] || null;

    // Determine source_image — prefer assigned_image, fall back to photo name
    const sourceImage = sample.assigned_image || photoName || null;
    const blankImage = sample.blank_image || null;

    if (sample.has_measurements == null) {
      throw new Error(
        `Sample ${sample.sample_id || "(unknown)"} is missing has_measurements in /api/samples payload`,
      );
    }
    // A processed sample may still be awaiting review. In that state the slim
    // list can report has_measurements=false even though extraction output is
    // available from the detail endpoint.
    const workflowStatus = String(sample.processing_status || "").toLowerCase();
    const processed =
      !!sample.has_measurements ||
      workflowStatus === "processed" ||
      workflowStatus === "flagged";

    // Build variable thicknesses from strip_definition
    const variableThicknesses = stripDef.variable_thicknesses_mm || [];
    const fixedThicknesses = stripDef.fixed_thicknesses_mm || [];

    // Build range label from variable thicknesses
    let rangeLabel = "";
    if (variableThicknesses.length >= 2) {
      const first = variableThicknesses[0];
      const last = variableThicknesses[variableThicknesses.length - 1];
      rangeLabel = `${Number(first).toFixed(2)}-${Number(last).toFixed(2)}`;
    }

    return {
      sample_id: sample.sample_id,
      created: sample.created || "",
      name: sample.name || "",
      notes: sample.notes || "",
      roles: sample.roles || [],
      variable_filament_id: varFilId,
      variable_color_name: varFil.color_name || varFilId,
      variable_hex: varFil.hex || "#dddddd",
      manufacturer: varFil.manufacturer || "",
      fixed_filament_ids: fixedFilIds,
      n_layers: stripDef.n_layers || 1,
      layer_height_mm: stripDef.layer_height_mm || 0.1,
      mode: stripDef.mode || "linear",
      range_label: rangeLabel,
      photo_count: photoCount,
      photo_name: photoName,
      step_id: sample.step_id || "",
      blank_image: blankImage,
      step_file: sample.step_file || "",
      processed: processed,
      strip_count: processed ? 1 : 0,
      source_image: sourceImage,
      variable_thicknesses_mm: variableThicknesses,
      fixed_thicknesses_mm: fixedThicknesses,
      // Preserve API-specific fields for future use:
      _processing_status: sample.processing_status,
      _assigned_image: sample.assigned_image,
      _assigned_blank_id: sample.assigned_blank_id,
      _orientation_rots:
        sample.orientation_rots != null ? sample.orientation_rots : null,
      _flag_reason: sample.flag_reason,
      // Per-swatch color is NOT in the slim list response; it is hydrated lazily
      // from GET /api/samples/{id}. Null here on the API path; the static snapshot
      // path still populates it inline.
      _measurements: sample.measurements || null,
      // Measurement summary from the slim list (drives counts + excluded badges
      // without the per-swatch payload).
      _n_swatches:
        sample.n_swatches != null
          ? sample.n_swatches
          : sample.measurements?.swatches?.length || 0,
      _n_excluded: sample.n_excluded != null ? sample.n_excluded : 0,
      _review_accepted: sample.review_accepted || false,
      _fit_exclude: sample.fit_exclude || false,
      _excluded_swatches: sample.excluded_swatches || [],
    };
  }

  function transformStepToData(step) {
    return {
      ...step,
      step_id: step.step_id,
      file_name: step.file_name || step.artifact_filename || "",
      full_path: step.export_path || step.artifact_path || step.step_id,
      artifact_filename: step.artifact_filename || step.file_name || "",
      artifact_exists: step.artifact_exists !== false,
      artifact_path: step.artifact_path || "",
      export_exists: step.export_exists !== false,
      export_path: step.export_path || "",
      bundle_names: step.bundle_names || [],
      bundle: step.bundle || "",
    };
  }

  function buildProcessedSamples(samples, filamentMap) {
    // Build the processed_samples array from samples that have been processed
    let stripCounter = 0;
    const results = [];
    for (const exp of samples) {
      if (!exp.processed) continue;
      stripCounter++;
      results.push({
        filament_id: exp.variable_filament_id,
        color_name: exp.variable_color_name,
        hex: exp.variable_hex,
        manufacturer: exp.manufacturer,
        sample_id: exp.sample_id,
        strip_id: `strip-${String(stripCounter).padStart(3, "0")}`,
        source_image: exp.source_image || null,
        blank_image: exp.blank_image || null,
        mode: exp.mode || "linear",
        printed_thicknesses_mm: exp.variable_thicknesses_mm || [],
        swatch_count: (exp.variable_thicknesses_mm || []).length,
        fixed_layers: exp.fixed_thicknesses_mm || [],
      });
    }
    return results;
  }

  function buildSummary(samples, filaments, steps, processedSamples) {
    const processedFilaments = new Set();
    for (const ps of processedSamples) {
      processedFilaments.add(ps.filament_id);
    }
    return {
      filament_count: filaments.length,
      sample_count: samples.length,
      step_count: steps.length,
      processed_filament_count: processedFilaments.size,
      processed_sample_count: processedSamples.length,
    };
  }

  function annotateFilaments(filaments, samples) {
    // Add sample_count and processed_count to each filament,
    // matching the aggregate fields app.js expects.
    const sampleCounts = {};
    const procCounts = {};
    for (const exp of samples) {
      const fid = exp.variable_filament_id;
      sampleCounts[fid] = (sampleCounts[fid] || 0) + 1;
      if (exp.processed) {
        procCounts[fid] = (procCounts[fid] || 0) + 1;
      }
      // Also count fixed filaments
      for (const fixedId of exp.fixed_filament_ids || []) {
        sampleCounts[fixedId] = (sampleCounts[fixedId] || 0) + 1;
      }
    }
    return filaments.map((f) => ({
      ...f,
      sample_count: sampleCounts[f.filament_id] || 0,
      processed_count: procCounts[f.filament_id] || 0,
    }));
  }

  function getApiLoadingState() {
    return {
      state: api.state._apiLoadingState,
      error: api.state._apiErrorMessage,
    };
  }

  async function loadFromApi() {
    api.state._apiLoadingState = "loading";
    api.state._apiErrorMessage = "";

    try {
      // Fetch samples, filaments, and steps in parallel
      const [
        samplesRaw,
        filamentsRaw,
        stepsRaw,
        blanksRaw,
        imageOverrides,
        imagesRaw,
        modelsStatus,
      ] = await Promise.all([
        api.fetchSamplesRaw(),
        api.fetchFilamentsRaw(),
        api.fetchSteps(),
        api.fetchBlanks(),
        api.fetchImageOverrides().catch(() => ({})),
        api.fetchImages().catch(() => []),
        api.fetchModelsStatus(),
      ]);

      const filamentMap = api.buildFilamentLookup(filamentsRaw);

      // Transform samples into the shape app.js expects
      const samples = samplesRaw.map((s) =>
        api.transformSampleToData(s, filamentMap),
      );

      // Build processed_samples from samples
      const processedSamples = api.buildProcessedSamples(samples, filamentMap);

      const steps = (stepsRaw || []).map((step) =>
        api.transformStepToData(step),
      );

      // Annotate filaments with sample counts
      const filaments = api.annotateFilaments(filamentsRaw, samples);

      // Build summary
      const summary = api.buildSummary(
        samples,
        filaments,
        steps,
        processedSamples,
      );

      api.state._apiLoadingState = "ready";

      // Build blank lookup for use across tabs
      const blanks = blanksRaw || [];

      return {
        summary,
        filaments,
        samples,
        steps,
        blanks,
        images: imagesRaw || [],
        image_overrides: imageOverrides || {},
        model_status: modelsStatus || {},
        processed_samples: processedSamples,
      };
    } catch (err) {
      api.state._apiLoadingState = "error";
      api.state._apiErrorMessage =
        err.message || "Failed to load data from API";
      throw err;
    }
  }

  async function initializeData(targetData) {
    try {
      const apiData = await api.loadFromApi();
      // Replace the global `data` variable used by app.js
      Object.assign(targetData, apiData);
      console.log("[api] Loaded data from API:", {
        filaments: apiData.filaments.length,
        samples: apiData.samples.length,
        processed: apiData.processed_samples.length,
      });
      return "api";
    } catch (err) {
      console.error(
        "[api] API unavailable; calibration data must be served by the configured backend:",
        err.message,
      );
      throw err;
    }
  }

  Object.assign(api, {
    buildFilamentLookup,
    transformSampleToData,
    transformStepToData,
    buildProcessedSamples,
    buildSummary,
    annotateFilaments,
    getApiLoadingState,
    loadFromApi,
    initializeData,
  });
}
