/** Install features/logbook/index commands. */
export function installFeaturesLogbookIndex(app) {
  function stepRecordByRef(stepRef) {
    if (!stepRef) return null;
    return (
      (app.state.session.data.steps || []).find(
        (step) =>
          step.step_id === stepRef ||
          step.file_name === stepRef ||
          step.artifact_filename === stepRef ||
          step.full_path === stepRef,
      ) || null
    );
  }

  function stepIdFromRef(stepRef) {
    return app.commands.stepRecordByRef(stepRef)?.step_id || stepRef || "";
  }

  function stepFileNameFromRef(stepRef) {
    const step = app.commands.stepRecordByRef(stepRef);
    if (step?.file_name) return step.file_name;
    const normalized = String(stepRef || "").replace(/\\/g, "/");
    return normalized.split("/").pop() || "";
  }

  function sampleStepId(exp) {
    return exp?.step_id || app.commands.stepIdFromRef(exp?.step_file || "");
  }

  function sampleStepFileName(exp) {
    return app.commands.stepFileNameFromRef(
      app.commands.sampleStepId(exp) || exp?.step_file || "",
    );
  }

  function sampleStatusMeta(exp) {
    const processingStatus =
      exp._processing_status ||
      exp.processing_status ||
      (exp.processed ? "processed" : "unassigned");
    const assignedImage = exp._assigned_image || exp.assigned_image || null;
    const assignedBlankId =
      exp._assigned_blank_id || exp.assigned_blank_id || null;
    const orientation = exp._orientation_rots ?? exp.orientation_rots;
    const reviewAccepted = Boolean(exp._review_accepted ?? exp.review_accepted);
    const hasAnySetup = Boolean(
      assignedImage || assignedBlankId || orientation != null,
    );
    const isReady = Boolean(
      assignedImage && assignedBlankId && orientation != null,
    );

    if (processingStatus === "failed")
      return { label: "failed", cls: "failed" };
    if (processingStatus === "flagged" || exp._flag_reason || exp.flag_reason)
      return { label: "flagged", cls: "flagged" };
    if (processingStatus === "processed" || exp.processed) {
      return reviewAccepted
        ? { label: "accepted", cls: "accepted" }
        : { label: "processed", cls: "processed" };
    }
    if (isReady) return { label: "ready", cls: "ready" };
    if (processingStatus === "assigned" || hasAnySetup)
      return { label: "incomplete", cls: "incomplete" };
    return { label: "unassigned", cls: "unassigned" };
  }

  function sampleHasMeasurementOutput(exp = {}) {
    const workflowStatus = String(
      exp._processing_status || exp.processing_status || "",
    ).toLowerCase();
    return (
      Boolean(exp.processed) ||
      workflowStatus === "processed" ||
      workflowStatus === "flagged"
    );
  }

  function statusForSample(exp) {
    return app.commands.sampleStatusMeta(exp).label;
  }

  function filamentStatusMeta(fil) {
    if (fil.has_profile) return { label: "profiled", cls: "profiled" };
    if (fil.has_strips || (fil.processed_count || 0) > 0)
      return { label: "strips only", cls: "strips_only" };
    return { label: "pending", cls: "pending" };
  }

  function profilePillMeta(filamentId) {
    const fil = app.state.session.data.filaments.find(
      (f) => f.filament_id === filamentId,
    );
    if (!fil) return null;
    const cached = app.state.modeling.profilesState.profileCache[filamentId];
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

  function filamentUsageLabels(exp, filamentId) {
    return app.commands
      .sampleFilamentRoleLines(exp, { filterFilamentId: filamentId })
      .map((line) => line.layerLabel);
  }

  function filamentUsedBySamples(filamentId) {
    return (app.state.session.data.samples || [])
      .map((exp) => ({
        exp,
        usageLabels: app.commands.filamentUsageLabels(exp, filamentId),
      }))
      .filter((entry) => entry.usageLabels.length > 0);
  }

  function normalizeUsageLabel(label) {
    const text = String(label || "").trim();
    const lrMatch = text.match(/^LR_(\d+)\s+(fixed|variable)$/i);
    if (lrMatch)
      return `LR_${lrMatch[1].padStart(2, "0")} ${lrMatch[2].toLowerCase()}`;
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
    const fil = app.commands.filamentMeta(fid);
    return (
      fil?.display_name ||
      [fil?.manufacturer || "", fil?.color_name || ""]
        .filter(Boolean)
        .join(" ") ||
      fallbackName ||
      fid ||
      "—"
    );
  }

  function sampleFilamentColorName(fid, fallbackName = "") {
    const fil = app.commands.filamentMeta(fid);
    return fil?.color_name || fallbackName || fid || "—";
  }

  function sampleFilamentBrandName(fid, fallbackBrand = "") {
    const fil = app.commands.filamentMeta(fid);
    return fil?.manufacturer || fallbackBrand || "—";
  }

  function formatLayerRoleLabel(roleOrIndex, kind = "fixed") {
    const role =
      typeof roleOrIndex === "object"
        ? roleOrIndex
        : { role_index: roleOrIndex, role_kind: kind };
    const roleIndex = Number(role?.role_index || 0);
    const roleKind =
      (role?.role_kind || kind || "").toLowerCase() === "variable"
        ? "variable"
        : "fixed";
    const fallback =
      roleIndex > 0
        ? `LR_${String(roleIndex).padStart(2, "0")}`
        : roleKind === "variable"
          ? "variable"
          : "LR_?";
    const roleLabel = app.commands.compactLayerRoleToken(
      role?.role_label,
      roleIndex,
      fallback,
    );
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
    if (app.commands.isStructuredGeometryBackend()) return [];

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
    return app.commands
      .sampleRoleRows(exp, { order: options.order || "top-to-bottom" })
      .filter(
        (role) => !filterFilamentId || role.filament_id === filterFilamentId,
      )
      .map((role) => {
        const fil = app.commands.filamentMeta(role.filament_id);
        const fallbackColorName =
          role.role_kind === "variable" ? exp?.variable_color_name || "" : "";
        const fallbackBrandName =
          role.role_kind === "variable" ? exp?.manufacturer || "" : "";
        const fallbackDisplayName = [fallbackBrandName, fallbackColorName]
          .filter(Boolean)
          .join(" ");
        const hex =
          fil?.hex ||
          (role.role_kind === "variable" ? exp?.variable_hex : null) ||
          "#cccccc";
        return {
          roleIndex: Number(role.role_index || 0),
          roleKind: role.role_kind,
          layerLabel: app.commands.formatLayerRoleLabel(role),
          thicknessMm:
            role.role_kind === "fixed" && role.fixed_thickness_mm != null
              ? Number(role.fixed_thickness_mm)
              : null,
          filamentId: role.filament_id,
          hex,
          name: app.commands.sampleFilamentDisplayName(
            role.filament_id,
            fallbackDisplayName,
          ),
          colorName: app.commands.sampleFilamentColorName(
            role.filament_id,
            fallbackColorName,
          ),
          brand: app.commands.sampleFilamentBrandName(
            role.filament_id,
            fallbackBrandName,
          ),
          excludeFromModel: !!fil?.exclude_from_model,
        };
      });
  }

  function sampleMaterialLines(exp, options = {}) {
    return app.commands.sampleFilamentRoleLines(exp, options);
  }

  function sampleFilamentStackSortText(exp) {
    const lines = app.commands.sampleFilamentRoleLines(exp);
    if (!lines.length)
      return exp?.variable_color_name || exp?.variable_filament_id || "";
    return lines
      .map((line) => line.colorName || line.filamentId || "")
      .join(" ");
  }

  function sampleBrandStackSortText(exp) {
    const lines = app.commands.sampleFilamentRoleLines(exp);
    if (!lines.length) return exp?.manufacturer || "";
    return lines.map((line) => line.brand || "").join(" ");
  }

  function renderLogbookFilamentCell(exp) {
    const lines = app.commands.sampleFilamentRoleLines(exp);
    if (!lines.length) {
      const fallbackFilament = app.commands.filamentMeta(
        exp.variable_filament_id || "",
      );
      return `
        <div class="filament-cell logbook-filament-stack">
          <div class="logbook-filament-layer">
            <span class="color-chip" style="background:${app.commands._escAttr(exp.variable_hex || "#dddddd")}"></span>
            <span class="logbook-filament-name">${app.commands._escHtml(exp.variable_color_name || exp.variable_filament_id || "—")}</span>
            ${fallbackFilament?.exclude_from_model ? `<span class="status-pill logbook-fit-pill planned">Excluded</span>` : ""}
          </div>
        </div>
      `;
    }
    return `
      <div class="filament-cell logbook-filament-stack" title="${app.commands._escAttr(lines.map((line) => `${line.layerLabel}: ${line.name || line.filamentId || "—"}`).join(" | "))}">
        ${lines
          .map(
            (line) => `
          <div class="logbook-filament-layer">
            <span class="color-chip" style="background:${app.commands._escAttr(line.hex || "#cccccc")}"></span>
            <span class="logbook-filament-name">${app.commands._escHtml(line.colorName || line.filamentId || "—")}</span>
            ${line.excludeFromModel ? `<span class="status-pill logbook-fit-pill planned">Excluded</span>` : ""}
          </div>
        `,
          )
          .join("")}
      </div>
    `;
  }

  function renderLogbookBrandCell(exp) {
    const lines = app.commands.sampleFilamentRoleLines(exp);
    if (!lines.length) return app.commands._escHtml(exp.manufacturer || "—");
    return `
      <div class="logbook-brand-stack" title="${app.commands._escAttr(lines.map((line) => `${line.layerLabel}: ${line.brand || "—"}`).join(" | "))}">
        ${lines
          .map(
            (line) => `
          <div class="logbook-brand-line">${app.commands._escHtml(line.brand || "—")}</div>
        `,
          )
          .join("")}
      </div>
    `;
  }

  function buildCompactUsedByList(entries, emptyMessage) {
    if (!entries.length) {
      return `<div class="used-by-empty muted-line">${emptyMessage}</div>`;
    }

    return `
      <div class="used-by-list">
        ${entries
          .map(
            (entry) => `
          <div class="used-by-row used-by-row-button is-disabled" data-linked-sample="${app.commands._escHtml(entry.sampleId)}" role="button" tabindex="-1" aria-disabled="true">
            <span class="used-by-sample mono">${app.commands._escHtml(entry.sampleId)}</span>
            <span class="used-by-details">
              ${entry.lines
                .map(
                  (line) => `
                <span class="used-by-detail-line">
                  <span class="color-chip used-by-chip" style="background:${line.hex}"></span>
                  <span class="used-by-filament-name">${app.commands._escHtml(line.name)}</span>
                  <span class="used-by-layer">${app.commands._escHtml(app.commands.normalizeUsageLabel(line.layerLabel))}</span>
                </span>
              `,
                )
                .join("")}
            </span>
            <span class="used-by-status"><span class="status-pill ${entry.status.cls}">${entry.status.label}</span></span>
          </div>
        `,
          )
          .join("")}
      </div>
    `;
  }

  function buildFilamentUsedBySection(filamentId) {
    const usedBy = app.commands.filamentUsedBySamples(filamentId);
    const rows = usedBy.map(({ exp }) => ({
      sampleId: exp.sample_id,
      lines: app.commands.sampleMaterialLines(exp),
      status: app.commands.sampleStatusMeta(exp),
    }));

    return {
      count: usedBy.length,
      html: app.commands.buildCompactUsedByList(
        rows,
        "No samples use this filament",
      ),
    };
  }

  function sourceDisplayName(exp) {
    return exp.source_image || exp.photo_name || "—";
  }

  function blankDisplayName(exp) {
    const blankObj = exp._assigned_blank_id
      ? (app.state.session.data.blanks || []).find(
          (b) => b.blank_id === exp._assigned_blank_id,
        )
      : null;
    return (
      blankObj?.original_filename ||
      exp.blank_image ||
      exp._assigned_blank_id ||
      "—"
    );
  }

  function getImageRotationCw(filename) {
    if (!filename) return 0;
    const overrideRot = Number(
      app.state.session.data.image_overrides?.[filename]?.rotation_cw,
    );
    if (Number.isFinite(overrideRot)) {
      return ((overrideRot % 4) + 4) % 4;
    }
    const match = (app.state.images.importState.images || []).find(
      (img) => img.filename === filename,
    );
    const rot = Number(match?.rotation_cw ?? 0);
    return Number.isFinite(rot) ? ((rot % 4) + 4) % 4 : 0;
  }

  function previewUrl(filename, options = {}) {
    if (!filename) return "";
    const params = new URLSearchParams();
    const size = options.size || "small";
    if (size && size !== "small") params.set("size", size);
    params.set("r", String(app.commands.getImageRotationCw(filename)));
    const bump =
      app.state.session.maintenanceCacheBust.allPreviews ||
      app.state.session.maintenanceCacheBust.previews.get(filename);
    if (bump) params.set("maintenance_v", String(bump));
    return `/api/previews/${encodeURIComponent(filename)}?${params.toString()}`;
  }

  function blankPreviewUrl(blankId, options = {}) {
    if (!blankId) return "";
    const params = new URLSearchParams();
    const size = options.size || "small";
    if (size && size !== "small") params.set("size", size);
    const bump =
      app.state.session.maintenanceCacheBust.allPreviews ||
      app.state.session.maintenanceCacheBust.blankPreviews.get(blankId);
    if (bump) params.set("maintenance_v", String(bump));
    return `/api/blanks/${encodeURIComponent(blankId)}/preview?${params.toString()}`;
  }

  function imageRotationPillHtml(filename) {
    const rotationCw = app.commands.getImageRotationCw(filename);
    if (!rotationCw) return "";
    return `<span class="image-rotation-pill" title="Image rotation override: ${rotationCw * 90}\u00b0 clockwise">&#8635; ${rotationCw * 90}\u00b0</span>`;
  }

  function placeholderThumb(label) {
    return `<div class="thumb-placeholder"><span>${label}</span></div>`;
  }

  function filamentMeta(fid) {
    return (
      app.state.session.data.filaments.find((fil) => fil.filament_id === fid) ||
      null
    );
  }

  function renderManagementLogbook() {
    app.dom.tableToolbar.className = "toolbar-inline";
    const processedCount = app.state.session.data.samples.filter(
      (exp) => exp.processed,
    ).length;
    const unprocessedCount = app.state.session.data.samples.filter(
      (exp) => !exp.processed,
    ).length;
    app.dom.tableSummary.textContent = `${app.state.session.data.samples.length} samples, ${processedCount} processed`;
    app.dom.tableToolbar.innerHTML = `
      <button class="primary-button small" id="newSampleBtn">+ New Samples</button>
    `;

    const enriched = [...app.state.session.data.samples].map((exp) => ({
      ...exp,
      _status: app.commands.statusForSample(exp),
      _filament_stack_sort: app.commands.sampleFilamentStackSortText(exp),
      _brand_stack_sort: app.commands.sampleBrandStackSortText(exp),
    }));
    if (app.state.logbook.sortState.key) {
      enriched.sort((a, b) =>
        app.commands.compareRows(
          a,
          b,
          app.state.logbook.sortState.key,
          app.state.logbook.sortState.direction,
        ),
      );
    }

    const rows = enriched
      .map((exp) => {
        const status = app.commands.sampleStatusMeta(exp);
        const imageName = app.commands.sourceDisplayName(exp);
        const blankName = app.commands.blankDisplayName(exp);
        const imageRotationPill = exp._assigned_image
          ? app.commands.imageRotationPillHtml(exp._assigned_image)
          : "";
        const imageCustodyBadge = exp._assigned_image
          ? app.commands.imageCustodyBadgeHtml(exp._assigned_image, "Image")
          : "";
        const blankObj = exp._assigned_blank_id
          ? (app.state.session.data.blanks || []).find(
              (b) => b.blank_id === exp._assigned_blank_id,
            )
          : null;
        const blankCustodyBadge = blankObj?.original_filename
          ? app.commands.imageCustodyBadgeHtml(
              blankObj.original_filename,
              "Blank image",
            )
          : "";
        return `
        <tr class="data-row" data-kind="sample" data-id="${exp.sample_id}">
          <td class="id-cell">${exp.sample_id}</td>
          <td>
            ${app.commands.buildStripMiniTable(exp)}
          </td>
          <td>
            ${app.commands.renderLogbookFilamentCell(exp)}
          </td>
          <td>${app.commands.renderLogbookBrandCell(exp)}</td>
          <td class="mono sample-source-state-cell">${imageName}${imageRotationPill}${imageCustodyBadge}</td>
          <td class="mono sample-source-state-cell">${blankName}${blankCustodyBadge}</td>
          <td><span class="status-pill ${status.cls}">${status.label}</span></td>
        </tr>
      `;
      })
      .join("");

    app.dom.tableContainer.innerHTML = `
      <table class="data-table management-library-table">
        <thead>
          <tr>
            <th class="sortable" data-sort="sample_id">ID${app.commands.sortArrow("sample_id")}</th>
            <th>Strip</th>
            <th class="sortable" data-sort="_filament_stack_sort">Filament${app.commands.sortArrow("_filament_stack_sort")}</th>
            <th class="sortable" data-sort="_brand_stack_sort">Brand${app.commands.sortArrow("_brand_stack_sort")}</th>
            <th>Image</th>
            <th>Blank</th>
            <th class="sortable" data-sort="_status">Status${app.commands.sortArrow("_status")}</th>
          </tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="7" class="empty-cell">No samples yet. Use + New Samples to create the first calibration sample set.</td></tr>`}</tbody>
      </table>
    `;
    app.commands.bindSortHeaders();
    // Bind sample creation entry point.
    const newSampleBtn = document.getElementById("newSampleBtn");
    if (newSampleBtn) {
      newSampleBtn.addEventListener("click", () =>
        app.commands.openBulkSampleCreateDrawer(),
      );
    }
  }

  function renderFilamentLibrary() {
    app.dom.tableToolbar.className = "toolbar-inline";
    app.dom.tableSummary.textContent = `${app.state.session.data.filaments.length} filaments, ${app.state.session.data.filaments.filter((fil) => fil.has_profile).length} profiled`;
    app.dom.tableToolbar.innerHTML = `
      <button class="primary-button small" id="addFilamentBtn">+ New Filament</button>
    `;

    const sorted = [...app.state.session.data.filaments];
    if (app.state.logbook.sortState.key) {
      const key =
        app.state.logbook.sortState.key === "profile"
          ? "_profileSort"
          : app.state.logbook.sortState.key;
      sorted.forEach((f) => {
        f._profileSort = f.has_profile ? "profiled" : "pending";
      });
      sorted.sort((a, b) =>
        app.commands.compareRows(
          a,
          b,
          key,
          app.state.logbook.sortState.direction,
        ),
      );
    }

    const rows = sorted
      .map((fil) => {
        const status = app.commands.filamentStatusMeta(fil);
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
      })
      .join("");

    app.dom.tableContainer.innerHTML = `
      <table class="data-table management-library-table">
        <thead>
          <tr>
            <th class="sortable" data-sort="color_name">Filament${app.commands.sortArrow("color_name")}</th>
            <th class="sortable" data-sort="manufacturer">Manufacturer${app.commands.sortArrow("manufacturer")}</th>
            <th>ID</th>
            <th>Samples</th>
            <th>Processed</th>
            <th class="sortable" data-sort="profile">Profile${app.commands.sortArrow("profile")}</th>
          </tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="6" class="empty-cell">No filaments yet. Use + New Filament to create the first filament.</td></tr>`}</tbody>
      </table>
    `;

    app.commands.bindSortHeaders();
    document.getElementById("addFilamentBtn")?.addEventListener("click", () => {
      app.commands.openFilamentCreateDrawer();
    });
  }

  function renderStepLibrary() {
    app.dom.tableToolbar.className = "toolbar-inline";
    const visibleSteps = app.state.session.data.steps.filter(
      (step) => !app.commands.stepMeta(step.step_id).deleted,
    );
    const stepStoragePath =
      (app.state.session._serverConfig &&
        (app.state.session._serverConfig.step_export_relative ||
          app.state.session._serverConfig.step_library_relative)) ||
      "output/steps/";
    app.dom.tableSummary.textContent = `${visibleSteps.length} sample geometries`;
    app.dom.tableToolbar.innerHTML = `
      <button class="primary-button small" id="openStepBuilderBtn">+ New Sample Geometry</button>
      <button class="ghost-button small" id="createBundleBtn">Manage Bundles</button>
      <button class="ghost-button small" id="exportGeometryFilesBtn">Export Geometry Files</button>
    `;

    const usage = {};
    app.state.session.data.samples.forEach((exp) => {
      const sid = app.commands.sampleStepId(exp);
      if (!sid) return;
      usage[sid] = (usage[sid] || 0) + 1;
    });

    const enriched = visibleSteps.map((step) => {
      const meta = app.commands.stepMeta(step.step_id);
      return {
        ...step,
        _layers: step.layer_count || 1 + (step.fixed_layers || []).length,
        _usage: usage[step.step_id] || 0,
        _alias: meta.alias || "",
        _bundle: (step.bundle_names || []).join(", ") || meta.bundle || "",
      };
    });

    if (app.state.logbook.sortState.key) {
      enriched.sort((a, b) =>
        app.commands.compareRows(
          a,
          b,
          app.state.logbook.sortState.key,
          app.state.logbook.sortState.direction,
        ),
      );
    }

    const rows = enriched
      .map((step) => {
        const meta = app.commands.stepMeta(step.step_id);
        const geometryName = meta.alias || step.alias || step.step_id;
        return `
        <tr class="data-row${app.state.logbook.selectedRecord.kind === "step" && app.state.logbook.selectedRecord.id === step.step_id ? " is-selected" : ""}" data-kind="step" data-id="${step.step_id}">
          <td>${app.commands.buildGeometryStripMiniTable(step)}</td>
          <td>${geometryName || "—"}</td>
          <td>${(step.bundle_names || []).join(", ") || meta.bundle || "—"}</td>
          <td class="sortable" data-sort="_layers">${step._layers}</td>
          <td>${step._usage}</td>
          <td>${step.last_write_time}</td>
        </tr>
      `;
      })
      .join("");

    app.dom.tableContainer.innerHTML = `
      <table class="data-table management-library-table">
        <thead>
          <tr>
            <th>Strip</th>
            <th class="sortable" data-sort="_alias">Alias${app.commands.sortArrow("_alias")}</th>
            <th class="sortable" data-sort="_bundle">Bundle${app.commands.sortArrow("_bundle")}</th>
            <th class="sortable" data-sort="_layers"># Layers${app.commands.sortArrow("_layers")}</th>
            <th class="sortable" data-sort="_usage">Used by${app.commands.sortArrow("_usage")}</th>
            <th class="sortable" data-sort="last_write_time">Last modified${app.commands.sortArrow("last_write_time")}</th>
          </tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="6" class="empty-cell">No sample geometries yet. Use + New Sample Geometry to create the first geometry.</td></tr>`}</tbody>
      </table>
    `;

    app.commands.bindSortHeaders();
    app.commands.bindStepBuilderButton();
    app.commands.bindGeometryLibraryExportButton();
    app.commands.bindStepStoragePathButton(stepStoragePath);
    app.commands.bindStepInlineActions();

    // Manage Bundles button — opens the bundle management drawer
    const createBundleBtn = document.getElementById("createBundleBtn");
    if (createBundleBtn) {
      createBundleBtn.addEventListener("click", () => {
        if (app.commands.isBundleMgmtOpen()) {
          app.commands.closeBundleMgmtDrawer();
        } else {
          app.commands.openBundleManagementDrawer();
        }
      });
    }
  }

  function resolveSampleMedia(exp) {
    const sourceImageFile = exp._assigned_image || exp.source_image || null;
    const blankObj = exp._assigned_blank_id
      ? (app.state.session.data.blanks || []).find(
          (b) => b.blank_id === exp._assigned_blank_id,
        )
      : null;
    const blankImageFile =
      blankObj?.original_filename || exp.blank_image || null;
    return {
      sourceName: app.commands.sourceDisplayName(exp),
      sourceImageFile,
      blankObj,
      blankImageFile,
      blankLabel:
        blankObj?.original_filename ||
        exp.blank_image ||
        exp._assigned_blank_id ||
        "—",
    };
  }

  function buildLightboxThumbButton({
    src,
    alt,
    title,
    emptyLabel,
    imgClass = "drawer-thumb",
    buttonAttrs = "",
    imgAttrs = "",
  }) {
    if (!src) return app.commands.placeholderThumb(emptyLabel);
    return `
        <button class="drawer-thumb-button" type="button" data-lightbox-src="${app.commands._escAttr(src)}" data-lightbox-title="${app.commands._escAttr(title || alt)}" ${buttonAttrs}>
          <img class="${imgClass}" src="${src}" alt="${app.commands._escAttr(alt)}" ${imgAttrs} onload="this.style.display='block'" onerror="this.style.display='none'; this.parentElement.classList.add('is-empty'); this.nextElementSibling.style.display='flex'">
          <span class="thumb-placeholder" style="display:none"><span>${emptyLabel}</span></span>
        </button>
      `;
  }

  function buildCompactSampleMediaPair(exp, media) {
    return `
      <div class="drawer-image-pair">
        <div class="drawer-image-card">
          <span class="sidebar-label" style="font-size:10px">Source</span>
          ${app.commands.buildLightboxThumbButton({ src: media.sourceImageFile ? app.commands.previewUrl(media.sourceImageFile) : null, alt: `${exp.sample_id} source`, title: `${exp.sample_id} source`, emptyLabel: "No preview" })}
          <span class="mono small-copy">${media.sourceName}</span>
        </div>
        <div class="drawer-image-card">
          <span class="sidebar-label" style="font-size:10px">Blank</span>
          ${app.commands.buildLightboxThumbButton({ src: media.blankObj?.blank_id ? app.commands.blankPreviewUrl(media.blankObj.blank_id) : null, alt: `${exp.sample_id} blank`, title: `${exp.sample_id} blank`, emptyLabel: "No blank" })}
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
    const fallbackCount =
      fallbackN ||
      exp._measurements?.swatches?.length ||
      exp._n_swatches ||
      exp.variable_thicknesses_mm?.length ||
      8;
    const borderMm = positiveNumber(geom.border_mm ?? geom.spine_width_mm, 3.0);
    const stepWMm = positiveNumber(
      geom.step_w_mm ?? geom.swatch_width_mm,
      12.0,
    );
    const stepHMm = positiveNumber(
      geom.step_h_mm ?? geom.swatch_height_mm,
      20.0,
    );
    const n = Math.max(
      1,
      Math.round(
        positiveNumber(
          geom.num_swatches ?? geom.swatch_count,
          Number(fallbackCount) || 8,
        ),
      ),
    );
    const totalW = 2 * borderMm + n * stepWMm;
    const totalH = stepHMm + borderMm;
    const ratio = totalW > 0 && totalH > 0 ? totalW / totalH : 4;
    const gridCols = `${borderMm}fr repeat(${n}, ${stepWMm}fr) ${borderMm}fr`;
    const gridRows = `${borderMm}fr ${stepHMm}fr`;
    return {
      borderMm,
      stepWMm,
      stepHMm,
      n,
      totalW,
      totalH,
      ratio,
      gridCols,
      gridRows,
    };
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
    return (
      "#" +
      values
        .map((value) =>
          Math.max(0, Math.min(255, Math.round(value)))
            .toString(16)
            .padStart(2, "0"),
        )
        .join("")
        .toUpperCase()
    );
  }

  function swatchAppearanceHex(sw) {
    const appearance = app.commands.swatchAppearanceDomain(sw);
    return appearance
      ? app.commands.rgbValuesToHex(
          appearance.jpeg_r,
          appearance.jpeg_g,
          appearance.jpeg_b,
        )
      : "";
  }

  function formatSwatchNumber(value, n = 4) {
    const num = Number(value);
    return Number.isFinite(num) ? app.commands.sigfig(num, n) : "—";
  }

  function buildMeasuredSwatchStripHtml(exp, options = {}) {
    const domain = options.domain || "display";
    const swatches = exp._measurements?.swatches || [];
    const { ratio, borderMm, stepWMm, stepHMm, n } =
      app.commands.sampleStripMetrics(exp);
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
    const cells = swatches
      .map((sw, index) => {
        const hex =
          domain === "appearance"
            ? app.commands.swatchAppearanceHex(sw)
            : app.commands.swatchDisplayDomain(sw).hex;
        return `<div class="sample-render-swatch${index > 0 ? " has-divider" : ""}${hex ? "" : " is-missing"}" style="background:${hex || "#d8d5cc"}"></div>`;
      })
      .join("");
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
    const renderFrames = Array.from(
      app.dom.detailSidebar.querySelectorAll(
        `[data-strip-render-frame="${sid}"]`,
      ),
    );
    const sourceFrame = app.dom.detailSidebar.querySelector(
      `[data-strip-source-frame="${sid}"]`,
    );
    const renderStages = Array.from(
      app.dom.detailSidebar.querySelectorAll(`[data-strip-render="${sid}"]`),
    );
    const metricStage = renderStages[0];
    if (!renderFrames.length || !sourceFrame || !metricStage) return;

    const sw = Number(img.naturalWidth);
    const sh = Number(img.naturalHeight);
    const borderMm = Number(metricStage.dataset.borderMm || 3);
    const stepWMm = Number(metricStage.dataset.stepWMm || 12);
    const stepHMm = Number(metricStage.dataset.stepHMm || 20);
    const n = Number(metricStage.dataset.swatches || 8);
    const deskewPad = 6;
    const totalWmm = 2 * borderMm + n * stepWMm;
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
      renderFrame.style.setProperty(
        "--render-frame-top-gap",
        `${topPct * frameHeight}px`,
      );
      renderFrame.style.setProperty(
        "--render-frame-bottom-gap",
        `${bottomPct * frameHeight}px`,
      );
    });
    renderStages.forEach((renderStage) => {
      renderStage.style.setProperty("--render-left", `${(innerX / sw) * 100}%`);
      renderStage.style.setProperty("--render-top", `${(innerY / sh) * 100}%`);
      renderStage.style.setProperty(
        "--render-width",
        `${(innerW / sw) * 100}%`,
      );
      renderStage.style.setProperty(
        "--render-height",
        `${(innerH / sh) * 100}%`,
      );
    });
  }

  function bindSampleStripGeometry() {
    app.dom.detailSidebar
      .querySelectorAll("img[data-strip-source]")
      .forEach((img) => {
        if (img.complete && img.naturalWidth) {
          app.commands.applySampleStripGeometry(img);
        } else {
          img.addEventListener(
            "load",
            () => app.commands.applySampleStripGeometry(img),
            { once: true },
          );
        }
      });
  }

  function buildSampleMeasurementsTable(exp, options = {}) {
    const editableFit = !!options.editableFit;
    const swatches = [...(exp._measurements?.swatches || [])].sort((a, b) => {
      const ai = Number(a.swatch_index ?? Number.MAX_SAFE_INTEGER);
      const bi = Number(b.swatch_index ?? Number.MAX_SAFE_INTEGER);
      if (ai !== bi) return ai - bi;
      return (
        Number(a.nominal_thickness_mm ?? 0) -
        Number(b.nominal_thickness_mm ?? 0)
      );
    });
    if (swatches.length === 0) {
      return `<p class="small-copy">No per-swatch measurements are available for this sample yet.</p>`;
    }

    const headers = swatches
      .map(
        (sw) => `
      <th class="sample-detail-colhead">${Number(sw.nominal_thickness_mm ?? 0).toFixed(2)}</th>
    `,
      )
      .join("");

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
        const hex = app.commands.swatchAppearanceHex(sw);
        return hex
          ? app.commands._escHtml(hex)
          : `<span class="sample-detail-muted">—</span>`;
      }),
      buildDataRow("R", "sample-detail-value", (sw) => {
        const appearance = app.commands.swatchAppearanceDomain(sw);
        return appearance
          ? app.commands.formatSwatchNumber(appearance.jpeg_r)
          : `<span class="sample-detail-muted">—</span>`;
      }),
      buildDataRow("G", "sample-detail-value", (sw) => {
        const appearance = app.commands.swatchAppearanceDomain(sw);
        return appearance
          ? app.commands.formatSwatchNumber(appearance.jpeg_g)
          : `<span class="sample-detail-muted">—</span>`;
      }),
      buildDataRow("B", "sample-detail-value", (sw) => {
        const appearance = app.commands.swatchAppearanceDomain(sw);
        return appearance
          ? app.commands.formatSwatchNumber(appearance.jpeg_b)
          : `<span class="sample-detail-muted">—</span>`;
      }),
      buildSectionRow("Transmission domain"),
      buildDataRow("T<sub>R</sub>", "sample-detail-value", (sw) =>
        app.commands.formatSwatchNumber(
          app.commands.swatchTransmissionDomain(sw).R_linear,
        ),
      ),
      buildDataRow("T<sub>G</sub>", "sample-detail-value", (sw) =>
        app.commands.formatSwatchNumber(
          app.commands.swatchTransmissionDomain(sw).G_linear,
        ),
      ),
      buildDataRow("T<sub>B</sub>", "sample-detail-value", (sw) =>
        app.commands.formatSwatchNumber(
          app.commands.swatchTransmissionDomain(sw).B_linear,
        ),
      ),
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
    const bump =
      app.state.session.maintenanceCacheBust.allSampleThumbnails ||
      app.state.session.maintenanceCacheBust.sampleThumbnails.get(key);
    return bump
      ? `${base}?maintenance_v=${encodeURIComponent(String(bump))}`
      : base;
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
    return app.commands.buildSampleSidebarBlock(title, bodyHtml, {
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
    const totalSwatches = Number(
      exp._n_swatches ||
        swatches.length ||
        (exp.variable_thicknesses_mm || []).length ||
        0,
    );
    const excludedCount = Math.max(
      Number(exp._n_excluded || 0),
      excludedIndexes.length,
    );
    return { totalSwatches, excludedCount, excludedIndexes };
  }

  function buildSampleSwatchFitHook(exp) {
    const summary = app.commands.sampleSwatchFitExclusionSummary(exp);
    const indexes = summary.excludedIndexes.join(",");
    return `<div class="sample-swatch-fit-hook" hidden data-sample-id="${app.commands._escHtml(exp.sample_id || "")}" data-total-swatches="${summary.totalSwatches}" data-excluded-count="${summary.excludedCount}" data-excluded-swatches="${app.commands._escHtml(indexes)}"></div>`;
  }

  function setSwatchFitToggleVisual(button, isExcluded) {
    if (!button) return;
    button.classList.toggle("is-excluded", isExcluded);
    button.classList.toggle("is-included", !isExcluded);
    button.setAttribute("aria-pressed", isExcluded ? "false" : "true");
    button.title = isExcluded
      ? "Excluded from model fits"
      : "Included in model fits";
    button.textContent = isExcluded ? "Excl" : "Incl";
  }

  function syncSampleSwatchFitHooks(exp) {
    const summary = app.commands.sampleSwatchFitExclusionSummary(exp);
    const indexes = summary.excludedIndexes.join(",");
    app.dom.detailSidebar
      .querySelectorAll(".sample-swatch-fit-hook")
      .forEach((hook) => {
        if (hook.dataset.sampleId !== (exp.sample_id || "")) return;
        hook.dataset.totalSwatches = String(summary.totalSwatches);
        hook.dataset.excludedCount = String(summary.excludedCount);
        hook.dataset.excludedSwatches = indexes;
      });
  }

  function bindSampleSwatchFitToggles(exp) {
    app.dom.detailSidebar
      .querySelectorAll(".sample-swatch-fit-toggle")
      .forEach((button) => {
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
              result = await app.api.excludeSwatch(
                exp.sample_id,
                swatchIndex,
                "",
              );
            } else {
              result = await app.api.includeSwatch(exp.sample_id, swatchIndex);
            }
            app.commands.applyFitControlMutationResponse(result);

            const swatches = exp._measurements?.swatches || [];
            const swatch = swatches.find(
              (sw) => Number(sw.swatch_index) === swatchIndex,
            );
            if (swatch) {
              swatch.fit_state = nextExcluded ? "excluded" : "included";
              if (!nextExcluded) swatch.exclusion_reason = "";
            }
            const excluded = new Set(
              (exp._excluded_swatches || []).map((idx) => Number(idx)),
            );
            if (nextExcluded) excluded.add(swatchIndex);
            else excluded.delete(swatchIndex);
            exp._excluded_swatches = Array.from(excluded)
              .filter(Number.isInteger)
              .sort((a, b) => a - b);
            exp._n_excluded = exp._excluded_swatches.length;

            app.commands.setSwatchFitToggleVisual(button, nextExcluded);
            app.commands.syncSampleSwatchFitHooks(exp);
            app.commands.showProfileToast(
              `Swatch ${swatchIndex + 1} ${nextExcluded ? "excluded" : "included"}`,
            );
          } catch (err) {
            button.textContent = originalText;
            app.commands.showProfileToast(
              err.message || "Failed to update swatch fit state",
            );
          } finally {
            button.disabled = false;
          }
        });
      });
  }

  function buildSampleCompactSidebarHtml(exp, media, options = {}) {
    const filamentRows = app.commands.sampleFilamentRoleLines(exp);
    const missingStructuredRoles =
      app.commands.isStructuredGeometryBackend() && !(exp.roles || []).length;
    const filamentsHtml = filamentRows
      .map((line) => {
        return `
        <div class="drawer-subtitle${line.roleKind === "fixed" ? " drawer-subtitle-fixed" : ""}">
          <span class="color-chip" style="background:${line.hex}"></span>
          <span class="filament-name-text"><strong>${app.commands._escHtml(line.name)}</strong></span>
          <span class="muted-line">${app.commands._escHtml(line.layerLabel)}</span>
        </div>
      `;
      })
      .join("");
    const notesText = exp.notes || "";
    const notesDisplay = notesText.trim() ? notesText : "None";
    const sampleFitExcluded = !!exp._fit_exclude;
    const stepId = app.commands.sampleStepId(exp);
    const step = app.commands.stepRecordByRef(stepId);
    const geometryLabel =
      app.commands._geometryAliasFromRef(stepId) ||
      app.commands.sampleStepFileName(exp);
    const artifactSummary = app.commands.sampleGeometryArtifactSummary(step);
    const showInspectLinks = options.inspectLinks !== false;
    const geometryInspectButton =
      showInspectLinks && step
        ? app.commands.drawerInspectButtonHtml({
            title: "Inspect sample geometry",
            attributes: `data-inspect-sample-geometry="${app.commands._escAttr(stepId)}"`,
          })
        : "";
    const modelFitInspectButton =
      showInspectLinks && app.commands.sampleHasModelingReviewDetail(exp)
        ? app.commands.drawerInspectButtonHtml({
            title: "Inspect modeling sample",
            attributes: `data-inspect-sample-model="${app.commands._escAttr(exp.sample_id || "")}"`,
          })
        : "";
    return `
      ${app.commands.buildSampleSidebarBlock(
        "Filaments",
        filamentsHtml ||
          (missingStructuredRoles
            ? `<div class="strip-diagram-contract-error">Missing geometry role data</div>`
            : `<span class="muted-line">No filament assignments</span>`),
        { bodyClass: "drawer-module-body-compact" },
      )}
      ${app.commands.buildSampleSidebarBlock("Images", app.commands.buildCompactSampleMediaPair(exp, media), { bodyClass: "drawer-module-body-compact" })}
      ${app.commands.buildSampleSidebarBlock("Strip", `<div class="sample-strip-tight">${app.commands.buildStripMiniTable(exp)}</div>`, { bodyClass: "drawer-module-body-compact" })}
      ${app.commands.buildSampleSidebarBlock(
        "Sample Geometry",
        `
        <span class="muted-line step-name-display sample-geometry-alias-display">${app.commands._escHtml(geometryLabel)}</span>
        <span class="drawer-form-value sample-geometry-artifact-status">${app.commands._escHtml(artifactSummary)}</span>
      `,
        {
          bodyClass: "drawer-module-body-compact",
          actionsHtml: `${geometryInspectButton}<button class="ghost-button xs step-copy-button" data-copy="folder" data-step="${stepId}">Copy Path</button>`,
          classes: "sample-step-module",
        },
      )}
      ${app.commands.buildSampleSidebarBlock(
        "Notes",
        `<span class="drawer-form-value sample-notes-display">${app.commands._escHtml(notesDisplay)}</span>`,
        {
          bodyClass: "drawer-module-body-compact",
          classes: "sample-notes-module",
        },
      )}
      ${app.commands.buildSampleSidebarBlock(
        "Model Fit",
        `
        <span class="drawer-form-value sample-fit-status ${sampleFitExcluded ? "is-excluded" : "is-included"}">
          ${sampleFitExcluded ? "Excluded from model fits" : "Included in model fits"}
        </span>
        ${app.commands.buildSampleSwatchFitHook(exp)}
      `,
        {
          bodyClass: "drawer-module-body-compact",
          actionsHtml: modelFitInspectButton,
          classes: "sample-fit-controls-module",
        },
      )}
    `;
  }

  function sampleGeometryArtifactSummary(step) {
    if (!step) return "Geometry record missing";
    const summary = step?.artifact_summary || {};
    const stepCount = Array.isArray(summary.step_paths)
      ? summary.step_paths.length
      : step?.artifact_path
        ? 1
        : 0;
    const stlCount = Array.isArray(summary.stl_paths)
      ? summary.stl_paths.length
      : 0;
    if (summary.manifest_error) return "Artifact manifest unreadable";
    const parts = [];
    if (stepCount > 0) parts.push(`${stepCount} STEP`);
    if (stlCount > 0) parts.push(`${stlCount} STL`);
    if (!parts.length) return "No managed artifacts yet";
    const manifestStatus = summary.manifest_exists
      ? "manifest present"
      : "manifest missing";
    return `Managed assets: ${parts.join(", ")}; ${manifestStatus}`;
  }

  function buildSampleExpandedView(exp, media) {
    return `
      <div class="sample-expanded-shell">
        <div class="sample-expanded-left compact-sidebar-stack">
          ${app.commands.buildSampleCompactSidebarHtml(exp, media)}
        </div>
        <div class="sample-expanded-right">
          ${app.commands.buildSampleExpandedAnalysisPane(exp)}
        </div>
      </div>
    `;
  }

  function buildSampleExpandedAnalysisPane(exp) {
    const sid = exp.sample_id;
    const processedLike = ["processed", "flagged", "failed"].includes(
      exp._processing_status,
    );
    const { ratio } = app.commands.sampleStripMetrics(exp);
    const stripSrc = processedLike
      ? app.commands.sampleThumbnailUrl(sid, "strip")
      : null;
    const hasAppearanceSwatches = (exp._measurements?.swatches || []).some(
      (sw) => !!app.commands.swatchAppearanceDomain(sw),
    );

    return `
      ${app.commands.buildSampleSidebarBlock(
        "Swatch Strip Comparison",
        `
        <div class="sample-photo-panel">
          <div class="sample-photo-panel-top">
            <div class="sample-strip-row">
              <div class="sample-strip-label-bubble">Extracted<br>Strip</div>
              <div class="sample-strip-row-content">
                <div class="sample-strip-frame" data-strip-source-frame="${sid}" style="--strip-ratio:${ratio}">
                  ${app.commands.buildLightboxThumbButton({
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
            ${
              hasAppearanceSwatches
                ? `
            <div class="sample-strip-row">
              <div class="sample-strip-label-bubble">Extracted<br>Appearance</div>
              <div class="sample-strip-row-content">
                ${app.commands.buildMeasuredSwatchStripHtml(exp, { domain: "appearance" })}
              </div>
            </div>
            `
                : ""
            }
            <div class="sample-strip-row">
              <div class="sample-strip-label-bubble">Extracted<br>Transmission</div>
              <div class="sample-strip-row-content">
                ${app.commands.buildMeasuredSwatchStripHtml(exp, { domain: "display" })}
              </div>
            </div>
          </div>
        </div>
      `,
        { classes: "sample-evidence-panel" },
      )}
      ${app.commands.buildSampleSidebarBlock(
        "Per-Swatch Data",
        app.commands.buildSampleMeasurementsTable(exp, {
          editableFit: app.state.logbook._sampleDrawerMode === "edit",
        }),
        { classes: "sample-swatches-panel" },
      )}
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

  function resolveStepLibraryFolderPath() {
    if (app.state.session._serverConfig?.step_export_path)
      return app.state.session._serverConfig.step_export_path;
    if (app.state.session._serverConfig?.step_library_path)
      return app.state.session._serverConfig.step_library_path;
    if (app.state.session._serverConfig?.data_root) {
      const prismaRoot = app.state.session._serverConfig.data_root.replace(
        /[\\\/]data$/,
        "",
      );
      if (
        prismaRoot &&
        prismaRoot !== app.state.session._serverConfig.data_root
      ) {
        const sep = prismaRoot.includes("\\") ? "\\" : "/";
        return `${prismaRoot}${sep}output${sep}steps`;
      }
    }
    return "";
  }

  function resolveStepClipboardPath(stepRef, mode = "folder") {
    const folderPath = app.commands.resolveStepLibraryFolderPath();
    if (mode !== "file") return folderPath;
    const filename = app.commands.stepFileNameFromRef(stepRef);
    if (!folderPath || !filename) return "";
    const sep = folderPath.includes("\\") ? "\\" : "/";
    return `${folderPath}${sep}${filename}`;
  }

  function sampleWindowToggleButtonHtml(expanded) {
    const sampleWindowLabel = expanded
      ? "Compact sample drawer"
      : "Expand sample drawer";
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

  function drawerInspectButtonHtml({
    id = "",
    label = "Inspect",
    title = "Inspect related record",
    attributes = "",
  } = {}) {
    const idAttr = id ? ` id="${app.commands._escAttr(id)}"` : "";
    const titleAttr = title
      ? ` title="${app.commands._escAttr(title)}" aria-label="${app.commands._escAttr(title)}"`
      : "";
    return `
      <button class="drawer-inspect-button" type="button"${idAttr}${titleAttr} ${attributes}>
        <svg viewBox="0 0 14 14" aria-hidden="true" focusable="false">
          <circle cx="6" cy="6" r="3.5"></circle>
          <path d="M8.7 8.7L12 12"></path>
        </svg>
        <span>${app.commands._escHtml(label)}</span>
      </button>
    `;
  }

  function sampleInspectReturnContext(exp, expanded = false) {
    return {
      sampleId: exp?.sample_id || "",
      expanded: !!expanded,
      mode: app.state.navigation.currentMode || "logbook",
      subtab: app.state.navigation.currentSubtab || "",
    };
  }

  function sampleHasModelingReviewDetail(exp = {}) {
    const swatchCount = Number(
      exp._n_swatches || exp._measurements?.swatches?.length || 0,
    );
    const processingStatus =
      exp._processing_status ||
      exp.processing_status ||
      (exp.processed ? "processed" : "");
    const acceptedKnown =
      exp._review_accepted != null || exp.review_accepted != null;
    const accepted = acceptedKnown
      ? Boolean(exp._review_accepted ?? exp.review_accepted)
      : Boolean(exp.processed);
    return (
      Boolean(exp.sample_id) &&
      accepted &&
      processingStatus === "processed" &&
      swatchCount > 0
    );
  }

  function returnToSampleInspectDrawer(context = {}) {
    const sampleId = context.sampleId || "";
    const exp = app.state.session.data.samples.find(
      (item) => item.sample_id === sampleId,
    );
    if (!exp) {
      app.commands.showProfileToast("Sample is no longer available");
      return;
    }
    app.state.navigation.currentMode = context.mode || "logbook";
    app.state.navigation.currentSubtab = context.subtab || "";
    app.commands.renderWorkspace();
    app.commands.renderSidebarForSample(exp, { expanded: !!context.expanded });
    const row = app.dom.tableContainer.querySelector(
      `.data-row[data-kind="sample"][data-id="${CSS.escape(sampleId)}"]`,
    );
    row?.classList.add("is-selected");
  }

  function bindStepCopyButtons(root = app.dom.detailSidebar) {
    root
      ?.querySelectorAll(
        ".step-copy-button, .copy-pill[data-step], .copy-pill[data-copy-text]",
      )
      .forEach((btn) => {
        btn.addEventListener("click", async () => {
          const stepFile = btn.dataset.step;
          const text =
            btn.dataset.copyText ||
            app.commands.resolveStepClipboardPath(
              stepFile,
              btn.dataset.copy || "folder",
            );
          if (!text) {
            app.commands.showProfileToast("Path not available");
            return;
          }
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
            setTimeout(() => {
              btn.textContent = orig;
            }, 1200);
          } catch (e) {
            console.error("Clipboard copy failed:", e, "Text was:", text);
            app.commands.showProfileToast(
              "Clipboard copy failed — check console",
            );
          }
        });
      });
  }

  function bindSampleInspectLinks(
    root = app.dom.detailSidebar,
    exp = {},
    options = {},
  ) {
    const expanded = !!options.expanded;
    root
      ?.querySelector("[data-inspect-sample-geometry]")
      ?.addEventListener("click", () => {
        const stepId = app.commands.sampleStepId(exp);
        if (!stepId || !app.commands.stepRecordByRef(stepId)) {
          app.commands.showProfileToast("Geometry record not found");
          return;
        }
        app.state.navigation.currentMode = "geometries";
        app.state.navigation.currentSubtab = "";
        app.commands.renderWorkspace();
        app.commands.renderStepDetailDrawer(stepId, {
          returnSampleContext: app.commands.sampleInspectReturnContext(
            exp,
            expanded,
          ),
        });
      });

    root
      ?.querySelector("[data-inspect-sample-model]")
      ?.addEventListener("click", async (event) => {
        if (!app.commands.sampleHasModelingReviewDetail(exp)) return;
        const button = event.currentTarget;
        button.disabled = true;
        try {
          app.state.navigation.currentMode = "profiles";
          app.state.navigation.currentSubtab = "samples";
          if (!app.state.modeling.modelingState.samples) {
            await app.commands.loadModelingTab("samples", { force: true });
          }
          app.commands.renderWorkspace();
          await app.commands.openModelingSampleDetailDrawer(
            exp.sample_id,
            button,
            {
              returnSampleContext: app.commands.sampleInspectReturnContext(
                exp,
                expanded,
              ),
            },
          );
        } catch (err) {
          app.commands.showProfileToast(
            err.message || "Could not open modeling sample detail",
          );
        } finally {
          if (document.body.contains(button)) button.disabled = false;
        }
      });
  }

  function renderLinkedSampleDrawer(exp) {
    if (!app.dom.linkedSampleDrawer || !app.dom.linkedSampleSidebar) return;
    const media = app.commands.resolveSampleMedia(exp);
    const status = app.commands.sampleStatusMeta(exp);
    app.dom.linkedSampleHeading.textContent = exp.sample_id;
    app.dom.linkedSampleStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
    app.dom.linkedSampleActionArea.innerHTML = "";
    app.dom.linkedSampleWindowArea.innerHTML = "";
    app.dom.linkedSampleSidebar.innerHTML =
      app.commands.buildSampleInspectFrameHtml(
        app.commands.buildSampleCompactSidebarHtml(exp, media, {
          inspectLinks: false,
        }),
        false,
      );
    app.commands.bindStepCopyButtons(app.dom.linkedSampleSidebar);
    app.commands.bindDrawerLightboxButtons(app.dom.linkedSampleSidebar);
  }

  function openLinkedSampleDrawer(sampleId, options = {}) {
    if (
      !app.dom.linkedSampleDrawer ||
      !app.dom.linkedSampleSidebar ||
      !app.commands.canOpenLinkedSampleDrawer()
    )
      return;
    const exp = app.state.session.data.samples.find(
      (item) => item.sample_id === sampleId,
    );
    if (!exp) return;

    app.state.logbook._linkedSampleDrawerState.sampleId = sampleId;
    app.state.logbook._linkedSampleDrawerState.returnFocusEl =
      options.returnFocusEl || document.activeElement;

    app.commands.renderLinkedSampleDrawer(exp);
    app.commands.syncLinkedSampleDrawerPosition();
    app.dom.linkedSampleDrawer.classList.add("is-open");
    app.dom.linkedSampleDrawer.setAttribute("aria-hidden", "false");
    document.body.classList.add("record-drawer-open");
    if (document.body?.classList.contains("using-keyboard-nav")) {
      app.dom.closeLinkedSampleDrawerBtn?.focus();
    }
  }

  function closeLinkedSampleDrawer(options = {}) {
    const restoreFocus = options.restoreFocus !== false;
    if (!app.dom.linkedSampleDrawer) return;

    app.dom.linkedSampleDrawer.classList.remove("is-open");
    app.dom.linkedSampleDrawer.setAttribute("aria-hidden", "true");
    app.dom.linkedSampleDrawer.style.removeProperty("right");
    app.dom.linkedSampleDrawer.style.removeProperty("width");
    app.dom.linkedSampleDrawer.style.removeProperty("--linked-drawer-shift");
    app.dom.linkedSampleStatusPill.innerHTML = "";
    app.dom.linkedSampleActionArea.innerHTML = "";
    app.dom.linkedSampleWindowArea.innerHTML = "";
    app.dom.linkedSampleSidebar.innerHTML = `
      <p class="small-copy">
        Select a sample from a Used By list to inspect it here.
      </p>
    `;

    const focusTarget =
      app.state.logbook._linkedSampleDrawerState.returnFocusEl;
    app.state.logbook._linkedSampleDrawerState.sampleId = null;
    app.state.logbook._linkedSampleDrawerState.returnFocusEl = null;

    if (
      restoreFocus &&
      focusTarget instanceof HTMLElement &&
      focusTarget.isConnected &&
      focusTarget.getAttribute("aria-disabled") !== "true"
    ) {
      focusTarget.focus();
    }
  }

  function bindLinkedSampleTriggers(root = app.dom.detailSidebar) {
    root?.querySelectorAll("[data-linked-sample]").forEach((node) => {
      node.addEventListener("click", () => {
        if (!app.commands.canOpenLinkedSampleDrawer()) return;
        app.commands.openLinkedSampleDrawer(node.dataset.linkedSample, {
          returnFocusEl: node,
        });
      });
      node.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        if (!app.commands.canOpenLinkedSampleDrawer()) return;
        e.preventDefault();
        app.commands.openLinkedSampleDrawer(node.dataset.linkedSample, {
          returnFocusEl: node,
        });
      });
    });
    app.commands.updateLinkedSampleTriggers(root);
  }

  function renderSidebarForSample(exp, options = {}) {
    app.commands.setDetailSidebarStackMode("default");
    app.state.logbook.selectedRecord = { kind: "sample", id: exp.sample_id };
    app.state.modeling.geometryDetailReturnSampleContext = null;
    app.state.modeling.modelingState.sampleDetailReturnSampleContext = null;
    app.state.logbook._sampleDrawerMode = null;
    const expanded =
      options.expanded != null
        ? !!options.expanded
        : app.state.logbook._sampleInspectExpanded;
    app.commands.setSampleInspectExpandedPreference(expanded);
    if (
      expanded &&
      app.commands.sampleHasMeasurementOutput(exp) &&
      !exp._measurements
    ) {
      app.commands.ensureMeasurementsThenRerender([exp.sample_id], () => {
        if (
          app.state.logbook._sampleDrawerMode === null &&
          app.state.logbook.selectedRecord.kind === "sample" &&
          app.state.logbook.selectedRecord.id === exp.sample_id &&
          app.dom.recordDrawer?.classList.contains("is-open")
        ) {
          const hydrated =
            app.state.session.data.samples.find(
              (item) => item.sample_id === exp.sample_id,
            ) || exp;
          app.commands.renderSidebarForSample(hydrated, {
            expanded: app.state.logbook._sampleInspectExpanded,
          });
        }
      });
    }
    app.dom.recordDrawer.classList.remove("narrow-drawer");
    app.dom.recordDrawer.classList.remove("sample-set-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.dom.recordDrawer.classList.toggle("sample-expanded", expanded);
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;
    app.commands.setDrawerHeading(exp.sample_id);
    app.dom.drawerStatusPill.innerHTML = "";
    const status = app.commands.sampleStatusMeta(exp);
    app.dom.drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
    app.dom.detailActionArea.innerHTML = `
      <button class="ghost-button xs drawer-header-action" id="editSampleBtn">Edit</button>
    `;
    app.dom.detailWindowArea.innerHTML =
      app.commands.sampleWindowToggleButtonHtml(expanded);
    const media = app.commands.resolveSampleMedia(exp);
    const sampleBodyHtml = expanded
      ? app.commands.buildSampleExpandedView(exp, media)
      : app.commands.buildSampleCompactSidebarHtml(exp, media);
    app.dom.detailSidebar.innerHTML = app.commands.buildSampleInspectFrameHtml(
      sampleBodyHtml,
      expanded,
    );
    app.commands.openRecordDrawer();
    app.commands.bindStepCopyButtons(app.dom.detailSidebar);
    app.commands.bindDrawerLightboxButtons(app.dom.detailSidebar);
    app.commands.bindSampleInspectLinks(app.dom.detailSidebar, exp, {
      expanded,
    });
    app.commands.bindSampleStripGeometry();

    document
      .getElementById("toggleSampleInspectBtn")
      ?.addEventListener("click", () => {
        app.commands.renderSidebarForSample(exp, { expanded: !expanded });
      });

    document
      .getElementById("editSampleBtn")
      ?.addEventListener("click", () =>
        app.commands.openSampleEditDrawer(exp, { expanded }),
      );
  }

  function renderStepDetailDrawer(stepId, options = {}) {
    app.dom.recordDrawer.classList.remove("narrow-drawer");
    app.dom.recordDrawer.classList.remove("sample-expanded");
    app.dom.recordDrawer.classList.remove("sample-set-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;

    const canonicalStepId = app.commands.stepIdFromRef(stepId);
    app.state.logbook.selectedRecord = { kind: "step", id: canonicalStepId };
    if (options.returnSampleContext) {
      app.state.modeling.geometryDetailReturnSampleContext =
        options.returnSampleContext;
    } else if (!options.preserveReturn) {
      app.state.modeling.geometryDetailReturnSampleContext = null;
    }
    const step = app.commands.stepRecordByRef(stepId);
    const meta = app.commands.stepMeta(canonicalStepId);
    const usedBy = app.state.session.data.samples.filter((exp) => {
      return app.commands.sampleStepId(exp) === canonicalStepId;
    });
    const displayName =
      meta.alias ||
      (step?.file_name || canonicalStepId).replace(/_/g, "_\u200B");
    app.commands.setDetailSidebarStackMode("form");
    app.commands.setDrawerHeading(displayName, { html: true });
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailWindowArea.innerHTML = "";
    const returnButton = app.state.modeling.geometryDetailReturnSampleContext
      ? `
      <button class="secondary-button small drawer-header-action" id="stepReturnSampleBtn" type="button">Return to Sample</button>
    `
      : "";
    app.dom.detailActionArea.innerHTML = `
      ${returnButton}
      ${app.commands.isStructuredGeometryBackend() ? `<button class="ghost-button small drawer-header-action" id="exportStepArtifactBtn">Export</button>` : ""}
      <button class="ghost-button small drawer-header-action" id="editStepBtn">Edit</button>
    `;

    const sampleRows = usedBy.map((exp) => ({
      sampleId: exp.sample_id,
      lines: app.commands.sampleMaterialLines(exp),
      status: app.commands.sampleStatusMeta(exp),
    }));
    const artifactHtml = app.commands.buildGeometryArtifactDetailHtml(
      step,
      canonicalStepId,
    );
    const exportHtml = app.commands.buildGeometryExportDetailHtml(step);

    app.dom.detailSidebar.innerHTML = `
      ${app.commands.buildDrawerFormModule(
        "Alias",
        `
        <span class="step-view-field step-field-slot drawer-form-value" id="stepAliasView">${meta.alias || "—"}</span>
        <input type="text" class="step-edit-field step-field-slot step-detail-input" id="stepAliasInput" value="${meta.alias}" placeholder="e.g. thin over white" style="display:none" />
      `,
        { density: "form" },
      )}
      ${app.commands.buildDrawerFormModule("ID", `<span class="mono muted-line drawer-form-value drawer-break-all">${canonicalStepId}</span>`, { density: "compact" })}
      ${app.commands.buildDrawerFormModule(
        "Bundle",
        `
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
      `,
        { density: "form" },
      )}
            ${app.commands.buildDrawerFormModule("Last Modified", `<span class="drawer-form-value">${step?.last_write_time || "—"}</span>`, { density: "compact" })}
            ${app.commands.buildDrawerFormModule(`Used By ${usedBy.length} Sample${usedBy.length === 1 ? "" : "s"}`, app.commands.buildCompactUsedByList(sampleRows, "No samples use this geometry"), { density: "table" })}
            ${app.commands.buildDrawerFormModule("Exported Files", exportHtml, { density: "compact" })}
            ${app.commands.buildDrawerFormModule("Managed Artifacts", artifactHtml, { density: "compact" })}
            <div class="delete-notice" id="stepDeleteNotice"></div>
          `;
    app.commands.bindStepMetaForm(canonicalStepId);
    app.commands.bindStepArtifactActions(canonicalStepId);
    app.commands.openRecordDrawer();
    app.commands.bindStepCopyButtons(app.dom.detailSidebar);
    app.commands.bindLinkedSampleTriggers(app.dom.detailSidebar);
  }

  function buildGeometryArtifactDetailHtml(step) {
    const summary = step?.artifact_summary || {};
    const stepCount =
      Array.isArray(summary.step_paths) && summary.step_paths.length
        ? summary.step_paths.length
        : step?.artifact_path
          ? 1
          : 0;
    const stlCount = Array.isArray(summary.stl_paths)
      ? summary.stl_paths.length
      : 0;
    const bodyCount = Array.isArray(summary.body_names)
      ? summary.body_names.length
      : 0;
    const rows = [];
    if (stepCount > 0)
      rows.push({
        label: "STEP",
        value: `${stepCount} managed file${stepCount === 1 ? "" : "s"}`,
      });
    if (stlCount > 0)
      rows.push({
        label: "STL",
        value: `${stlCount} managed file${stlCount === 1 ? "" : "s"}`,
      });
    if (bodyCount > 0)
      rows.push({
        label: "Bodies",
        value: `${bodyCount} labeled solid${bodyCount === 1 ? "" : "s"}`,
      });
    if (summary.manifest_error)
      rows.push({ label: "Manifest", value: "Unreadable" });
    else if (stepCount || stlCount)
      rows.push({
        label: "Manifest",
        value: summary.manifest_exists ? "Present" : "Missing",
      });
    if (!rows.length) {
      return `<span class="drawer-form-value">No managed artifacts yet</span>`;
    }
    return `
      <div class="artifact-path-list">
        ${rows
          .map(
            (row) => `
          <div class="artifact-summary-row">
            <span class="artifact-kind">${row.label}</span>
            <span class="muted-line drawer-form-value">${app.commands.escapeHtml(row.value)}</span>
          </div>
        `,
          )
          .join("")}
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
        label:
          stlCount > 1 || summary.latest_stl_export_kind === "folder"
            ? "STLs"
            : "STL",
        path: summary.latest_stl_export_path,
      });
    }
    if (!rows.length) {
      const exportPaths = Array.isArray(summary.export_paths)
        ? summary.export_paths.filter((path) => !!path)
        : [];
      if (!exportPaths.length && step?.export_path)
        exportPaths.push(step.export_path);
      exportPaths.forEach((path) => {
        rows.push({
          label: String(path).toLowerCase().endsWith(".stl") ? "STL" : "STEP",
          path,
        });
      });
    }
    if (!rows.length) {
      return `<span class="drawer-form-value">No user-facing exports yet</span>`;
    }
    return `
      <div class="artifact-path-list">
        ${rows
          .map((row) => {
            const displayPath = app.commands.displayPathFromPrismaRoot(
              row.path,
            );
            return `
          <div class="artifact-path-row">
            <span class="artifact-kind">${row.label}</span>
            <span class="mono muted-line drawer-break-all" title="${app.commands.escapeHtml(row.path)}">${app.commands.escapeHtml(displayPath)}</span>
            <button class="copy-pill drawer-utility-button" type="button" data-copy-text="${app.commands.escapeHtml(row.path)}">Copy Path</button>
          </div>
        `;
          })
          .join("")}
      </div>
    `;
  }

  function displayPathFromPrismaRoot(path) {
    const raw = String(path || "");
    const normalized = raw.replace(/\//g, "\\");
    const marker = "\\Prisma\\";
    const markerIndex = normalized
      .toLowerCase()
      .lastIndexOf(marker.toLowerCase());
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
    const nums = values.map((value) => app.commands.numericValue(value, NaN));
    if (nums.some((value) => !Number.isFinite(value))) return null;
    const increment = nums[1] - nums[0];
    for (let index = 2; index < nums.length; index += 1) {
      if (Math.abs(nums[index] - nums[index - 1] - increment) > 1e-6)
        return null;
    }
    return increment;
  }

  function bindStepArtifactActions(stepId) {
    const exportButton = document.getElementById("exportStepArtifactBtn");
    if (!exportButton) return;
    exportButton.addEventListener("click", () => {
      const step = app.commands.stepRecordByRef(stepId);
      app.commands.openGeometryExportDialog(stepId, step?.alias || stepId);
    });
  }

  function sortArrow(key) {
    if (app.state.logbook.sortState.key !== key) return "";
    return app.state.logbook.sortState.direction === "asc" ? " ↓" : " ↑";
  }

  function compareRows(a, b, key, direction) {
    const av = ((a[key] ?? "") + "").toLowerCase();
    const bv = ((b[key] ?? "") + "").toLowerCase();
    const cmp = av.localeCompare(bv, undefined, { numeric: true });
    return direction === "asc" ? cmp : -cmp;
  }

  function bindSortHeaders() {
    app.dom.tableContainer.querySelectorAll(".sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (app.state.logbook.sortState.key === key) {
          app.state.logbook.sortState.direction =
            app.state.logbook.sortState.direction === "asc" ? "desc" : "asc";
        } else {
          app.state.logbook.sortState.key = key;
          app.state.logbook.sortState.direction = "asc";
        }
        app.commands.renderWorkspace();
      });
    });
  }

  function bindRowSelection() {
    app.dom.tableContainer.querySelectorAll(".data-row").forEach((row) => {
      if (!row.dataset.kind || !row.dataset.id) return;
      row.addEventListener("click", () => {
        const isSameRecord =
          app.state.logbook.selectedRecord.kind === row.dataset.kind &&
          app.state.logbook.selectedRecord.id === row.dataset.id;
        const drawerOpen = app.dom.recordDrawer.classList.contains("is-open");
        const isSameSelection = isSameRecord && drawerOpen;

        if (isSameSelection) {
          app.state.logbook.selectedRecord = { kind: null, id: null };
          app.dom.tableContainer
            .querySelectorAll(".data-row")
            .forEach((node) => node.classList.remove("is-selected"));
          app.commands.closeDrawer();
          return;
        }

        // Close step builder drawer if open — mutually exclusive with record drawer
        if (app.commands.isStepBuilderOpen())
          app.commands.closeStepBuilderDrawer();
        // Close bundle management drawer if open
        if (app.commands.isBundleMgmtOpen())
          app.commands.closeBundleMgmtDrawer();

        app.state.logbook.selectedRecord = {
          kind: row.dataset.kind,
          id: row.dataset.id,
        };
        app.dom.tableContainer
          .querySelectorAll(".data-row")
          .forEach((node) => node.classList.remove("is-selected"));
        row.classList.add("is-selected");

        if (row.dataset.kind === "sample") {
          const exp = app.state.session.data.samples.find(
            (item) => item.sample_id === row.dataset.id,
          );
          if (exp) {
            app.commands.renderSidebarForSample(exp);
          }
        } else if (row.dataset.kind === "filament") {
          app.dom.recordDrawer.classList.remove("sample-expanded");
          app.dom.recordDrawer.classList.remove("model-filament-drawer");
          const fil = app.state.session.data.filaments.find(
            (item) => item.filament_id === row.dataset.id,
          );
          if (fil) app.commands.renderSidebarForFilament(fil);
        } else if (row.dataset.kind === "step") {
          app.commands.renderStepDetailDrawer(row.dataset.id);
        } else {
          app.commands.setDetailSidebarStackMode("default");
          app.dom.recordDrawer.classList.remove("narrow-drawer");
          app.dom.recordDrawer.classList.remove("sample-expanded");
          app.dom.recordDrawer.classList.remove("sample-set-drawer");
          app.dom.recordDrawer.classList.remove("model-filament-drawer");
          app.state.filaments._filamentDrawerMode = null;
          app.state.filaments._filamentDrawerData = null;
          app.commands.setDrawerHeading(row.dataset.id);
          app.dom.detailActionArea.innerHTML = "";
          app.dom.detailWindowArea.innerHTML = "";
          app.dom.detailSidebar.innerHTML = `<p class="small-copy">This row is connected to the real project data snapshot. The detail view can become richer as we decide what belongs on each record type.</p>`;
          app.commands.openRecordDrawer();
        }
      });
    });
  }

  function openRecordDrawer() {
    app.commands.syncRecordDrawerPosition();
    app.commands.syncLinkedSampleDrawerPosition();
    app.dom.recordDrawer.classList.add("is-open");
    app.dom.recordDrawer.setAttribute("aria-hidden", "false");
    document.body.classList.add("record-drawer-open");
    app.commands.updateLinkedSampleTriggers(app.dom.detailSidebar);
  }

  function closeDrawer() {
    app.commands.closeLinkedSampleDrawer({ restoreFocus: false });
    app.dom.recordDrawer.classList.remove("is-open");
    app.dom.recordDrawer.classList.remove("narrow-drawer");
    app.dom.recordDrawer.classList.remove("sample-expanded");
    app.dom.recordDrawer.classList.remove("sample-set-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.commands.setDetailSidebarStackMode("default");
    app.dom.recordDrawer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("record-drawer-open");
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailWindowArea.innerHTML = "";
    app.state.filaments._filamentDrawerMode = null;
    app.state.filaments._filamentDrawerData = null;
    app.state.logbook._sampleDrawerMode = null;
    app.state.modeling.geometryDetailReturnSampleContext = null;
    app.state.modeling.modelingState.sampleDetailReturnSampleContext = null;
  }

  function clearSelectionAndDrawer() {
    app.state.logbook.selectedRecord = { kind: null, id: null };
    app.commands.resetStepEditorState(null);
    app.dom.tableContainer
      .querySelectorAll(".data-row")
      .forEach((node) => node.classList.remove("is-selected"));
    app.commands.closeDrawer();
  }

  Object.assign(app.commands, {
    stepRecordByRef,
    stepIdFromRef,
    stepFileNameFromRef,
    sampleStepId,
    sampleStepFileName,
    sampleStatusMeta,
    sampleHasMeasurementOutput,
    statusForSample,
    filamentStatusMeta,
    profilePillMeta,
    filamentUsageLabels,
    filamentUsedBySamples,
    normalizeUsageLabel,
    compactLayerRoleToken,
    sampleFilamentDisplayName,
    sampleFilamentColorName,
    sampleFilamentBrandName,
    formatLayerRoleLabel,
    sampleRoleRows,
    sampleFilamentRoleLines,
    sampleMaterialLines,
    sampleFilamentStackSortText,
    sampleBrandStackSortText,
    renderLogbookFilamentCell,
    renderLogbookBrandCell,
    buildCompactUsedByList,
    buildFilamentUsedBySection,
    sourceDisplayName,
    blankDisplayName,
    getImageRotationCw,
    previewUrl,
    blankPreviewUrl,
    imageRotationPillHtml,
    placeholderThumb,
    filamentMeta,
    renderManagementLogbook,
    renderFilamentLibrary,
    renderStepLibrary,
    resolveSampleMedia,
    buildLightboxThumbButton,
    buildCompactSampleMediaPair,
    sampleStripMetrics,
    swatchDisplayDomain,
    swatchTransmissionDomain,
    swatchAppearanceDomain,
    rgbValuesToHex,
    swatchAppearanceHex,
    formatSwatchNumber,
    buildMeasuredSwatchStripHtml,
    applySampleStripGeometry,
    bindSampleStripGeometry,
    buildSampleMeasurementsTable,
    sampleThumbnailUrl,
    buildSampleSidebarBlock,
    buildDrawerFormModule,
    sampleSwatchFitExclusionSummary,
    buildSampleSwatchFitHook,
    setSwatchFitToggleVisual,
    syncSampleSwatchFitHooks,
    bindSampleSwatchFitToggles,
    buildSampleCompactSidebarHtml,
    sampleGeometryArtifactSummary,
    buildSampleExpandedView,
    buildSampleExpandedAnalysisPane,
    buildSampleInspectFrameHtml,
    resolveStepLibraryFolderPath,
    resolveStepClipboardPath,
    sampleWindowToggleButtonHtml,
    drawerInspectButtonHtml,
    sampleInspectReturnContext,
    sampleHasModelingReviewDetail,
    returnToSampleInspectDrawer,
    bindStepCopyButtons,
    bindSampleInspectLinks,
    renderLinkedSampleDrawer,
    openLinkedSampleDrawer,
    closeLinkedSampleDrawer,
    bindLinkedSampleTriggers,
    renderSidebarForSample,
    renderStepDetailDrawer,
    buildGeometryArtifactDetailHtml,
    buildGeometryExportDetailHtml,
    displayPathFromPrismaRoot,
    fixedSwatchIncrement,
    bindStepArtifactActions,
    sortArrow,
    compareRows,
    bindSortHeaders,
    bindRowSelection,
    openRecordDrawer,
    closeDrawer,
    clearSelectionAndDrawer,
  });
}
