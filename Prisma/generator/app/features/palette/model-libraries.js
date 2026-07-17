/**
 * Install the model-libraries feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesModelLibraries(app) {
function modelLibraryKey(item) {
    return item?.library_id || `directory:${item?.directory_name || "unknown"}`;
  }

function selectedModelLibrary() {
    const libraries = app.state.session.modelLibraryManager.status?.libraries || [];
    return libraries.find(item => app.commands.modelLibraryKey(item) === app.state.session.modelLibraryManager.selectedKey) || null;
  }

function formatModelLibraryDate(value) {
    if (!value) return "Not available";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return String(value);
    return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

function formatModelLibrarySize(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "Not available";
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

function modelLibraryCompatibility(item) {
    if (!item?.valid) return "Could not be verified";
    const minimum = item.minimum_prisma_version || "unknown";
    const maximum = item.maximum_prisma_version;
    return maximum ? `Prisma ${minimum} through ${maximum}` : `Prisma ${minimum} or newer`;
  }

function setModelLibraryMessage(message = "", kind = "") {
    app.state.session.modelLibraryManager.message = message;
    app.state.session.modelLibraryManager.messageKind = kind;
    app.state.session.modelLibraryManager.error = kind === "error" ? message : "";
  }

function modelLibraryErrorMessage(error, fallback) {
    const message = String(error?.message || fallback || "Model-library operation failed");
    return message.replace(/^API\s+\d+:\s*/i, "").trim();
  }

function modelLibraryDisplayName(item) {
    return item?.library_name || item?.directory_name || item?.library_id || "Unknown library";
  }

function modelLibraryRailSummary(status, { loading = false } = {}) {
    if (!status) {
      return loading
        ? { name: "Checking model library…", state: "Checking", kind: "idle", detail: "Validating installed model libraries" }
        : { name: "Library status unavailable", state: "Unavailable", kind: "warning", detail: "Open Model Library to retry" };
    }

    const libraries = Array.isArray(status.libraries) ? status.libraries : [];
    const runtime = libraries.find(item => item.runtime_active)
      || libraries.find(item => item.valid && item.library_id === status.runtime_active_library_id)
      || null;
    const selected = libraries.find(item => item.selected_for_next_launch)
      || (status.active_library_id
        ? libraries.find(item => item.library_id === status.active_library_id)
        : null)
      || null;

    if (status.active_state_error) {
      return {
        name: selected ? app.commands.modelLibraryDisplayName(selected) : "Library selection is unreadable",
        state: "Invalid",
        kind: "error",
        detail: String(status.active_state_error),
      };
    }
    if (selected && !selected.valid) {
      return {
        name: app.commands.modelLibraryDisplayName(selected),
        state: "Invalid",
        kind: "error",
        detail: selected.error || "The selected model library failed validation",
      };
    }
    if (status.restart_required) {
      const nextName = selected ? app.commands.modelLibraryDisplayName(selected) : "the selected library";
      return {
        name: runtime ? app.commands.modelLibraryDisplayName(runtime) : nextName,
        state: "Restart Required",
        kind: "warning",
        detail: runtime
          ? `${nextName} is selected for the next launch`
          : `${nextName} will be used after Prisma restarts`,
      };
    }
    if (runtime) {
      return {
        name: app.commands.modelLibraryDisplayName(runtime),
        state: "In Use",
        kind: "ok",
        detail: "This running Generator is using this model library",
      };
    }
    if (status.runtime_active_library_id || status.active_library_id) {
      return {
        name: selected ? app.commands.modelLibraryDisplayName(selected) : "Selected library is unavailable",
        state: "Invalid",
        kind: "error",
        detail: "The selected model library is missing or could not be loaded",
      };
    }
    return {
      name: "No library selected",
      state: "No library selected",
      kind: "idle",
      detail: libraries.length
        ? "Choose a valid model library"
        : "Install a published model library to enable generation",
    };
  }

function renderModelLibraryRail() {
    const summaryEl = app.state.ui.$("#railModelLibrarySummary");
    const nameEl = app.state.ui.$("#railModelLibraryName");
    const stateEl = app.state.ui.$("#railModelLibraryState");
    if (!summaryEl || !nameEl || !stateEl) return;
    const summary = app.commands.modelLibraryRailSummary(
      app.state.session.modelLibraryManager.status,
      { loading: app.state.session.modelLibraryManager.loading },
    );
    nameEl.textContent = summary.name;
    stateEl.textContent = summary.state;
    stateEl.className = `rail-model-library-state is-${summary.kind}`;
    summaryEl.title = summary.detail;
  }

function setModelLibraryBusy(busy, footerText = "") {
    app.state.session.modelLibraryManager.busy = !!busy;
    const modal = app.state.ui.$("#modelLibrariesModal .model-libraries-modal");
    if (modal) modal.classList.toggle("is-busy", !!busy);
    const footer = app.state.ui.$("#modelLibrariesFooterStatus");
    if (footer && footerText) footer.textContent = footerText;
  }

function updateModelLibrariesAttention() {
    const status = app.state.session.modelLibraryManager.status;
    const attention = app.state.ui.$("#modelLibrariesAttention");
    const button = app.state.ui.$("#modelLibrariesBtn");
    const noLibraries = !!status && Array.isArray(status.libraries) && status.libraries.length === 0;
    const needsAttention = !status || !!status.active_state_error || noLibraries || !!status.restart_required || !status.runtime_active_library_id;
    if (attention) attention.classList.toggle("is-hidden", !needsAttention);
    if (button) {
      button.classList.toggle("has-attention", needsAttention);
      button.title = noLibraries
        ? "Prisma needs a published model library"
        : status?.restart_required
          ? "A model library change is waiting for restart"
        : !status?.runtime_active_library_id
          ? "Prisma needs a valid model library"
          : "Install or switch published model libraries";
    }
  }

function renderModelLibrariesManager() {
    app.commands.renderModelLibraryRail();
    const status = app.state.session.modelLibraryManager.status;
    const libraries = status?.libraries || [];
    const list = app.state.ui.$("#modelLibrariesList");
    const details = app.state.ui.$("#modelLibraryDetails");
    const notice = app.state.ui.$("#modelLibrariesNotice");
    const footer = app.state.ui.$("#modelLibrariesFooterStatus");
    if (!list || !details || !notice) return;

    const existingKeys = new Set(libraries.map(app.commands.modelLibraryKey));
    if (!existingKeys.has(app.state.session.modelLibraryManager.selectedKey)) {
      const preferred = libraries.find(item => item.runtime_active)
        || libraries.find(item => item.selected_for_next_launch)
        || libraries[0];
      app.state.session.modelLibraryManager.selectedKey = preferred ? app.commands.modelLibraryKey(preferred) : null;
    }

    let noticeMessage = app.state.session.modelLibraryManager.message;
    let noticeKind = app.state.session.modelLibraryManager.messageKind;
    const selectedNextLibrary = libraries.find(item => item.selected_for_next_launch);
    if (!noticeMessage && status?.active_state_error) {
      noticeMessage = `Prisma could not read the selected-library record: ${status.active_state_error}`;
      noticeKind = "error";
    } else if (!noticeMessage && status && !libraries.length) {
      noticeMessage = status.active_library_id
        ? "The selected model library is no longer installed. Install a published model library, then select it for the next launch."
        : "No model libraries are installed. Install a published model library to enable lithophane generation.";
      noticeKind = "warning";
    } else if (!noticeMessage && selectedNextLibrary && !selectedNextLibrary.valid) {
      noticeMessage = "The selected model library is invalid. Choose a valid library before restarting Prisma.";
      noticeKind = "error";
    } else if (!noticeMessage && status?.restart_required) {
      noticeMessage = "A different model library is selected. Restart Prisma to begin using it.";
      noticeKind = "warning";
    } else if (!noticeMessage && status && !status.runtime_active_library_id) {
      noticeMessage = "Prisma is in Library Recovery mode. Install or select a valid library to enable lithophane generation.";
      noticeKind = "warning";
    }
    notice.textContent = noticeMessage || "";
    notice.className = `model-libraries-notice${noticeMessage ? "" : " is-hidden"}${noticeKind ? ` is-${noticeKind}` : ""}`;

    if (app.state.session.modelLibraryManager.loading) {
      list.innerHTML = `<div class="model-libraries-empty"><strong>Checking libraries…</strong><span>Validating installed model files.</span></div>`;
    } else if (!libraries.length) {
      list.innerHTML = `<div class="model-libraries-empty"><strong>No libraries installed</strong><span>Choose Install Library to add a downloaded Prisma model package.</span></div>`;
    } else {
      list.innerHTML = libraries.map((item, index) => {
        const key = app.commands.modelLibraryKey(item);
        const name = item.library_name || item.directory_name || "Unreadable library";
        const meta = item.valid
          ? `${item.library_version || "Unknown version"} · ${item.publisher || "Unknown publisher"}`
          : (item.error || "Validation failed");
        const badges = [
          item.runtime_active ? `<span class="model-library-badge is-runtime">In Use</span>` : "",
          item.selected_for_next_launch && !item.runtime_active ? `<span class="model-library-badge is-next">Selected next launch</span>` : "",
          !item.valid ? `<span class="model-library-badge is-error">Invalid</span>` : "",
        ].join("");
        return `
          <button class="model-library-list-item${key === app.state.session.modelLibraryManager.selectedKey ? " is-selected" : ""}${item.runtime_active ? " is-runtime" : ""}${!item.valid ? " is-invalid" : ""}"
                  type="button" data-model-library-index="${index}">
            <span class="model-library-list-marker" aria-hidden="true"></span>
            <span class="model-library-list-copy">
              <span class="model-library-list-name">${app.commands.esc(name)}</span>
              <span class="model-library-list-meta">${app.commands.esc(meta)}</span>
            </span>
            <span class="model-library-list-badges">${badges}</span>
          </button>`;
      }).join("");
    }

    list.querySelectorAll("[data-model-library-index]").forEach(button => {
      button.addEventListener("click", () => {
        const item = libraries[Number(button.dataset.modelLibraryIndex)];
        if (!item) return;
        app.state.session.modelLibraryManager.selectedKey = app.commands.modelLibraryKey(item);
        app.commands.setModelLibraryMessage();
        app.commands.renderModelLibrariesManager();
      });
    });

    const item = app.commands.selectedModelLibrary();
    if (!item) {
      details.innerHTML = `<div class="model-libraries-empty"><strong>Select a model library</strong><span>Library information and available actions will appear here.</span></div>`;
    } else if (!item.valid) {
      const removable = !!item.library_id && !item.runtime_active && !item.selected_for_next_launch;
      const invalidBadges = [
        item.selected_for_next_launch && !item.runtime_active ? `<span class="model-library-badge is-next">Selected next launch</span>` : "",
        `<span class="model-library-badge is-error">Invalid</span>`,
      ].join("");
      details.innerHTML = `
        <div class="model-library-detail-title-row">
          <div><h4 class="model-library-detail-title">${app.commands.esc(item.directory_name || "Unreadable library")}</h4><div class="model-library-detail-version">This installed library cannot be used.</div></div>
          <div class="model-library-detail-badges">${invalidBadges}</div>
        </div>
        <div class="model-library-validation-error"><strong>Validation failed</strong><br>${app.commands.esc(item.error || "Prisma could not validate this model library.")}</div>
        <div class="model-library-detail-actions">
          <button class="ghost-button small danger" id="modelLibraryRemoveBtn" type="button" ${removable ? "" : "disabled"}>Remove Library</button>
        </div>`;
    } else {
      const description = item.description || "No description was provided for this library.";
      const releaseNotes = item.release_notes
        ? `<div class="model-library-release-notes"><strong>Release notes</strong><br>${app.commands.esc(item.release_notes)}</div>`
        : "";
      const activateLabel = item.runtime_active
        ? "Currently in Use"
        : item.selected_for_next_launch
          ? "Restart Prisma"
          : "Activate and Restart Prisma";
      const activateDisabled = item.runtime_active ? "disabled" : "";
      const removable = !item.runtime_active && !item.selected_for_next_launch;
      const badges = [
        item.runtime_active ? `<span class="model-library-badge is-runtime">In Use</span>` : "",
        item.selected_for_next_launch && !item.runtime_active ? `<span class="model-library-badge is-next">Selected next launch</span>` : "",
        `<span class="model-library-badge">Valid</span>`,
      ].join("");
      details.innerHTML = `
        <div class="model-library-detail-title-row">
          <div><h4 class="model-library-detail-title">${app.commands.esc(item.library_name)}</h4><div class="model-library-detail-version">Version ${app.commands.esc(item.library_version || "unknown")}</div></div>
          <div class="model-library-detail-badges">${badges}</div>
        </div>
        <p class="model-library-detail-description">${app.commands.esc(description)}</p>
        <div class="model-library-detail-grid">
          <div class="model-library-detail-label">Publisher</div><div class="model-library-detail-value">${app.commands.esc(item.publisher || "Not available")}</div>
          <div class="model-library-detail-label">Published</div><div class="model-library-detail-value">${app.commands.esc(app.commands.formatModelLibraryDate(item.created_at))}</div>
          <div class="model-library-detail-label">Filaments</div><div class="model-library-detail-value">${app.commands.esc(String(item.filament_count ?? "Not available"))}</div>
          <div class="model-library-detail-label">Compatibility</div><div class="model-library-detail-value">${app.commands.esc(app.commands.modelLibraryCompatibility(item))}</div>
          <div class="model-library-detail-label">Package size</div><div class="model-library-detail-value">${app.commands.esc(app.commands.formatModelLibrarySize(item.total_bytes))}</div>
          <div class="model-library-detail-label">Validation</div><div class="model-library-detail-value">All model files verified</div>
        </div>
        ${releaseNotes}
        <div class="model-library-detail-actions">
          <button class="ghost-button small danger" id="modelLibraryRemoveBtn" type="button" ${removable ? "" : "disabled"}>Remove Library</button>
          <button class="primary-button small" id="modelLibraryActivateBtn" type="button" ${activateDisabled}>${activateLabel}</button>
        </div>`;
    }

    app.state.ui.$("#modelLibraryActivateBtn")?.addEventListener("click", () => app.commands.handleActivateModelLibrary(item));
    app.state.ui.$("#modelLibraryRemoveBtn")?.addEventListener("click", () => app.commands.handleRemoveModelLibrary(item));
    if (footer) {
      const validCount = libraries.filter(entry => entry.valid).length;
      footer.textContent = app.state.session.modelLibraryManager.busy
        ? footer.textContent
        : `${validCount} valid · ${libraries.length} installed`;
    }
    app.commands.setModelLibraryBusy(app.state.session.modelLibraryManager.busy);
    app.commands.updateModelLibrariesAttention();
  }

function openModelLibrariesModal() {
    const modal = app.state.ui.$("#modelLibrariesModal");
    if (!modal) return;
    modal.classList.remove("is-hidden");
    modal.setAttribute("aria-hidden", "false");
    if (app.state.session.modelLibraryManager.status) {
      app.commands.renderModelLibrariesManager();
    } else {
      app.commands.loadModelLibraries({ silent: true });
    }
  }

function closeModelLibrariesModal() {
    if (app.state.session.modelLibraryManager.busy || app.state.session.modelLibraryManager.restarting) return;
    const modal = app.state.ui.$("#modelLibrariesModal");
    if (!modal) return;
    modal.classList.add("is-hidden");
    modal.setAttribute("aria-hidden", "true");
  }

async function loadModelLibraries({ openOnRecovery = false, silent = false } = {}) {
    app.state.session.modelLibraryManager.loading = true;
    if (!silent) app.commands.setModelLibraryMessage();
    app.commands.renderModelLibrariesManager();
    try {
      const status = await app.api.fetchModelLibraries();
      app.state.session.modelLibraryManager.status = status;
      app.commands.reconcileEnabledFilamentsForRuntimeLibrary();
      const libraries = status.libraries || [];
      const existingKeys = new Set(libraries.map(app.commands.modelLibraryKey));
      if (!existingKeys.has(app.state.session.modelLibraryManager.selectedKey)) {
        const preferred = libraries.find(item => item.runtime_active)
          || libraries.find(item => item.selected_for_next_launch)
          || libraries[0];
        app.state.session.modelLibraryManager.selectedKey = preferred ? app.commands.modelLibraryKey(preferred) : null;
      }
      if (openOnRecovery && !app.state.session.modelLibraryAutoOpened && (!status.runtime_active_library_id || status.active_state_error)) {
        app.state.session.modelLibraryAutoOpened = true;
        const modal = app.state.ui.$("#modelLibrariesModal");
        modal?.classList.remove("is-hidden");
        modal?.setAttribute("aria-hidden", "false");
      }
    } catch (error) {
      app.commands.setModelLibraryMessage(app.commands.modelLibraryErrorMessage(error, "Could not load model libraries"), "error");
    } finally {
      app.state.session.modelLibraryManager.loading = false;
      app.commands.renderModelLibrariesManager();
    }
  }

async function handleInstallModelLibrary(file) {
    if (!file || app.state.session.modelLibraryManager.busy) return;
    app.commands.setModelLibraryMessage();
    app.commands.setModelLibraryBusy(true, `Installing ${file.name}…`);
    try {
      const response = await app.api.installModelLibrary(file);
      app.state.session.modelLibraryManager.status = response.status;
      app.state.session.modelLibraryManager.selectedKey = response.installed?.library_id || app.state.session.modelLibraryManager.selectedKey;
      app.commands.setModelLibraryMessage(
        `${response.installed?.library_name || "Model library"} was installed. Select Activate and Restart Prisma when you are ready to use it.`,
        "success",
      );
      app.commands.showToast("Model library installed", "success");
    } catch (error) {
      app.commands.setModelLibraryMessage(app.commands.modelLibraryErrorMessage(error, "The model library could not be installed"), "error");
      app.commands.showToast("Library installation failed", "error");
    } finally {
      const input = app.state.ui.$("#modelLibraryPackageInput");
      if (input) input.value = "";
      app.commands.setModelLibraryBusy(false);
      app.commands.renderModelLibrariesManager();
    }
  }

async function waitForModelLibraryRestart(targetId) {
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 500));
      try {
        const status = await app.api.fetchModelLibraries();
        if (status.runtime_active_library_id === targetId && !status.restart_required) {
          window.location.reload();
          return;
        }
      } catch { /* the server is expected to disappear briefly */ }
    }
    app.state.session.modelLibraryManager.restarting = false;
    app.commands.setModelLibraryBusy(false);
    app.commands.setModelLibraryMessage(
      "The selected library was saved, but Prisma did not reconnect. Close Prisma and open it again to finish switching libraries.",
      "warning",
    );
    app.commands.renderModelLibrariesManager();
  }

async function requestModelLibraryRestart(targetId) {
    app.state.session.modelLibraryManager.restarting = true;
    app.commands.setModelLibraryMessage("Prisma is restarting with the selected model library…", "success");
    app.commands.setModelLibraryBusy(true, "Restarting Prisma…");
    app.commands.renderModelLibrariesManager();
    try {
      await app.api.restartPrisma();
      await app.commands.waitForModelLibraryRestart(targetId);
    } catch (error) {
      app.state.session.modelLibraryManager.restarting = false;
      app.commands.setModelLibraryBusy(false);
      app.commands.setModelLibraryMessage(
        `The library selection was saved, but automatic restart failed. Close Prisma and open it again. ${app.commands.modelLibraryErrorMessage(error, "")}`.trim(),
        "warning",
      );
      app.commands.renderModelLibrariesManager();
    }
  }

async function handleActivateModelLibrary(item) {
    if (!item?.valid || !item.library_id || app.state.session.modelLibraryManager.busy) return;
    if (item.selected_for_next_launch && !item.runtime_active) {
      await app.commands.requestModelLibraryRestart(item.library_id);
      return;
    }
    const confirmed = await app.commands.appConfirm(
      `Switch to “${item.library_name}”? Prisma will restart, and any unsaved session history will be cleared.`,
      { title: "Activate Model Library", ok: "Activate and Restart" },
    );
    if (!confirmed) return;
    app.commands.setModelLibraryMessage();
    app.commands.setModelLibraryBusy(true, `Selecting ${item.library_name}…`);
    try {
      const response = await app.api.activateModelLibrary(item.library_id);
      app.state.session.modelLibraryManager.status = response.status;
      app.state.session.modelLibraryManager.selectedKey = item.library_id;
      app.commands.renderModelLibrariesManager();
      if (response.restart_required) {
        await app.commands.requestModelLibraryRestart(item.library_id);
      } else {
        app.commands.setModelLibraryMessage(`${item.library_name} is already in use.`, "success");
      }
    } catch (error) {
      app.commands.setModelLibraryMessage(app.commands.modelLibraryErrorMessage(error, "The model library could not be selected"), "error");
    } finally {
      if (!app.state.session.modelLibraryManager.restarting) {
        app.commands.setModelLibraryBusy(false);
        app.commands.renderModelLibrariesManager();
      }
    }
  }

async function handleRemoveModelLibrary(item) {
    if (!item?.library_id || item.runtime_active || item.selected_for_next_launch || app.state.session.modelLibraryManager.busy) return;
    const name = item.library_name || item.directory_name || "this library";
    const confirmed = await app.commands.appConfirm(
      `Remove “${name}” from this copy of Prisma? This cannot be undone, but the original downloaded package is not affected.`,
      { title: "Remove Model Library", ok: "Remove Library" },
    );
    if (!confirmed) return;
    app.commands.setModelLibraryBusy(true, `Removing ${name}…`);
    try {
      const response = await app.api.removeModelLibrary(item.library_id);
      app.state.session.modelLibraryManager.status = response.status;
      app.state.session.modelLibraryManager.selectedKey = null;
      app.commands.setModelLibraryMessage(`${name} was removed.`, "success");
      app.commands.showToast("Model library removed", "success");
    } catch (error) {
      app.commands.setModelLibraryMessage(app.commands.modelLibraryErrorMessage(error, "The model library could not be removed"), "error");
    } finally {
      app.commands.setModelLibraryBusy(false);
      app.commands.renderModelLibrariesManager();
    }
  }

async function handleOpenModelLibrariesFolder() {
    try {
      await app.api.openModelLibrariesFolder();
      app.commands.showToast("Opened Model Libraries folder", "success");
    } catch (error) {
      app.commands.setModelLibraryMessage(app.commands.modelLibraryErrorMessage(error, "The Model Libraries folder could not be opened"), "error");
      app.commands.renderModelLibrariesManager();
    }
  }

  Object.assign(app.commands, {
    modelLibraryKey,
    selectedModelLibrary,
    formatModelLibraryDate,
    formatModelLibrarySize,
    modelLibraryCompatibility,
    setModelLibraryMessage,
    modelLibraryErrorMessage,
    modelLibraryDisplayName,
    modelLibraryRailSummary,
    renderModelLibraryRail,
    setModelLibraryBusy,
    updateModelLibrariesAttention,
    renderModelLibrariesManager,
    openModelLibrariesModal,
    closeModelLibrariesModal,
    loadModelLibraries,
    handleInstallModelLibrary,
    waitForModelLibraryRestart,
    requestModelLibraryRestart,
    handleActivateModelLibrary,
    handleRemoveModelLibrary,
    handleOpenModelLibrariesFolder,
  });
}
