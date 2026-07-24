/** Install features/geometries/editor commands. */
export function installFeaturesGeometriesEditor(app) {
  function bindStepMetaForm(stepId) {
    const aliasInput = document.getElementById("stepAliasInput");
    const aliasView = document.getElementById("stepAliasView");
    const chipsEl = document.getElementById("stepBundleChips");
    const addSelect = document.getElementById("stepBundleAddSelect");
    if (!aliasInput) return;

    let isEditing = false;

    function stepReturnButtonHtml() {
      return app.state.modeling.geometryDetailReturnSampleContext
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
        const context = app.state.modeling.geometryDetailReturnSampleContext;
        app.state.modeling.geometryDetailReturnSampleContext = null;
        app.commands.returnToSampleInspectDrawer(context || {});
      });

      editButton?.addEventListener("click", () => {
        setEditMode(true);
      });

      saveButton?.addEventListener("click", () => {
        const val = aliasInput.value.trim();
        app.commands.stepMeta(stepId).alias = val;
        if (aliasView) aliasView.textContent = val || "—";
        const displayName = val || stepId.replace(/_/g, "_\u200B");
        app.commands.setDrawerHeading(displayName, { html: true });
        if (typeof app.api.updateStepMetadata === "function") {
          app.api
            .updateStepMetadata(
              stepId,
              val,
              app.commands.stepMeta(stepId).bundle,
            )
            .catch(() => {});
        }
        setEditMode(false);
        app.state.logbook.selectedRecord = { kind: null, id: null };
        app.commands.renderWorkspace();
        const row = app.dom.tableContainer.querySelector(
          `.data-row[data-kind="step"][data-id="${CSS.escape(stepId)}"]`,
        );
        if (row) row.click();
      });

      discardButton?.addEventListener("click", () => {
        const meta = app.commands.stepMeta(stepId);
        aliasInput.value = meta.alias;
        setEditMode(false);
      });

      deleteButton?.addEventListener("click", () => {
        app.commands.showStepDeleteDialog(stepId);
      });
    }

    // ── Bundle chip management ──
    let _cachedBundles = [];

    async function loadAndRenderBundles() {
      try {
        const result = await app.api.fetchBundles();
        _cachedBundles = result.bundles || result || [];
      } catch (_) {
        _cachedBundles = [];
      }
      renderBundleChips();
    }

    function bundlesContainingStep() {
      return _cachedBundles.filter((b) => (b.step_ids || []).includes(stepId));
    }

    function bundlesNotContainingStep() {
      return _cachedBundles.filter((b) => !(b.step_ids || []).includes(stepId));
    }

    function renderBundleChips() {
      const memberBundles = bundlesContainingStep();
      const availableBundles = bundlesNotContainingStep();

      // Render each associated bundle as a row with dropdown-width name + X
      if (chipsEl) {
        if (memberBundles.length === 0) {
          chipsEl.innerHTML = "";
        } else {
          chipsEl.innerHTML = memberBundles
            .map(
              (b) => `
            <div class="step-bundle-add-row">
              <span class="bundle-chip-name">${b.name}</span>
              <button class="bundle-chip-remove" data-bundle="${b.name}" type="button" title="Remove bundle from this STEP file" aria-label="Remove bundle from this STEP file">
                <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                  <path d="M2.5 2.5L9.5 9.5"></path>
                  <path d="M9.5 2.5L2.5 9.5"></path>
                </svg>
              </button>
            </div>
          `,
            )
            .join("");

          chipsEl.querySelectorAll(".bundle-chip-remove").forEach((btn) => {
            btn.addEventListener("click", async () => {
              try {
                await app.api.removeStepFromBundle(btn.dataset.bundle, stepId);
                await loadAndRenderBundles();
              } catch (err) {
                app.commands.showImportToast(
                  err.message || "Failed to remove from bundle",
                  "error",
                );
              }
            });
          });
        }
      }

      // Populate dropdown with bundles this step is NOT in
      if (addSelect) {
        addSelect.innerHTML =
          `<option value="">---none---</option>` +
          availableBundles
            .map((b) => `<option value="${b.name}">${b.name}</option>`)
            .join("");
      }
    }

    // Selecting a bundle from dropdown adds the step immediately
    if (addSelect) {
      addSelect.addEventListener("change", async () => {
        const bundleName = addSelect.value;
        if (!bundleName) return;
        try {
          await app.api.addStepToBundle(bundleName, stepId);
          await loadAndRenderBundles();
        } catch (err) {
          app.commands.showImportToast(
            err.message || "Failed to add to bundle",
            "error",
          );
        }
      });
    }

    // Initial load
    loadAndRenderBundles();

    // ── Edit mode toggle ──
    function setEditMode(editing) {
      isEditing = editing;
      document
        .querySelectorAll(".step-view-field")
        .forEach((el) => (el.style.display = editing ? "none" : ""));
      document
        .querySelectorAll(".step-edit-field")
        .forEach((el) => (el.style.display = editing ? "" : "none"));
      app.dom.detailActionArea.innerHTML = editing
        ? `
          <button class="primary-button small drawer-header-action" id="saveStepBtn">Save</button>
          <button class="ghost-button small drawer-header-action" id="discardStepBtn">Discard</button>
          <button class="delete-button small drawer-header-action" id="deleteStepBtn">Delete</button>
        `
        : `
          ${stepReturnButtonHtml()}
          ${app.commands.isStructuredGeometryBackend() ? `<button class="ghost-button small drawer-header-action" id="exportStepArtifactBtn">Export</button>` : ""}
          <button class="ghost-button small drawer-header-action" id="editStepBtn">Edit</button>
        `;
      wireHeaderActions();
      app.commands.bindStepArtifactActions(stepId);
    }

    setEditMode(false);
  }

  function bindStepInlineActions() {
    app.dom.tableContainer
      .querySelectorAll("[data-step-action]")
      .forEach((button) => {
        button.addEventListener("click", (event) => {
          event.stopPropagation();
          const stepId = button.dataset.stepId;
          const action = button.dataset.stepAction;
          const usageCount = app.commands.stepUsageCount(stepId);

          if (app.state.geometries.stepEditorState.stepId !== stepId) {
            app.commands.resetStepEditorState(stepId);
          }

          if (action === "toggle-edit") {
            app.state.geometries.stepEditorState.isEditing =
              !app.state.geometries.stepEditorState.isEditing;
            app.state.geometries.stepEditorState.confirmDelete = false;
            app.state.geometries.stepEditorState.deleteMessage = "";
            app.state.geometries.stepEditorState.deleteMessageKind = "";
            const meta = app.commands.stepMeta(stepId);
            app.state.geometries.stepEditorState.draftAlias = meta.alias || "";
            app.state.geometries.stepEditorState.draftBundle =
              meta.bundle || "";
            app.commands.renderWorkspace();
            return;
          }

          if (action === "discard-edit") {
            app.commands.resetStepEditorState(stepId);
            app.commands.renderWorkspace();
            return;
          }

          if (action === "save-edit") {
            const aliasInput = document.getElementById("inlineStepAliasInput");
            const bundleInput = document.getElementById(
              "inlineStepBundleInput",
            );
            app.commands.stepMeta(stepId).alias = (
              aliasInput?.value || ""
            ).trim();
            app.commands.stepMeta(stepId).bundle = (
              bundleInput?.value || ""
            ).trim();
            if (typeof app.api.updateStepMetadata === "function") {
              app.api
                .updateStepMetadata(
                  stepId,
                  app.commands.stepMeta(stepId).alias,
                  app.commands.stepMeta(stepId).bundle,
                )
                .catch((err) => {
                  app.commands.showImportToast(
                    err.message || "Failed to save STEP metadata",
                    "error",
                  );
                });
            }
            app.commands.renderBundleOptions();
            app.commands.resetStepEditorState(stepId);
            app.commands.renderWorkspace();
            return;
          }

          if (action === "start-delete") {
            if (usageCount > 0) {
              app.state.geometries.stepEditorState.deleteMessage = `This is currently used by ${usageCount} sample(s). You must reassign those samples before deleting.`;
              app.state.geometries.stepEditorState.deleteMessageKind =
                "blocked";
              app.state.geometries.stepEditorState.confirmDelete = false;
            } else {
              app.state.geometries.stepEditorState.confirmDelete = true;
              app.state.geometries.stepEditorState.deleteMessage = "";
              app.state.geometries.stepEditorState.deleteMessageKind = "";
            }
            app.commands.renderWorkspace();
            return;
          }

          if (action === "cancel-delete") {
            app.state.geometries.stepEditorState.confirmDelete = false;
            app.state.geometries.stepEditorState.deleteMessage = "";
            app.state.geometries.stepEditorState.deleteMessageKind = "";
            app.commands.renderWorkspace();
            return;
          }

          if (action === "confirm-delete") {
            if (usageCount > 0) {
              app.state.geometries.stepEditorState.confirmDelete = false;
              app.state.geometries.stepEditorState.deleteMessage = `This is currently used by ${usageCount} sample(s). You must reassign those samples before deleting.`;
              app.state.geometries.stepEditorState.deleteMessageKind =
                "blocked";
              app.commands.renderWorkspace();
              return;
            }
            app.commands.stepMeta(stepId).deleted = true;
            app.state.logbook.selectedRecord = { kind: null, id: null };
            app.commands.resetStepEditorState(null);
            app.commands.renderWorkspace();
          }
        });
      });
  }

  Object.assign(app.commands, {
    bindStepMetaForm,
    bindStepInlineActions,
  });
}
